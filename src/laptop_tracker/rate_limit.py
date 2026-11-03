"""One request scheduler per retailer: spacing, ``Retry-After`` cooldowns, pauses and probes.

Every HTTP request to a retailer (search, inspection, collection, fallback, retries and redirect
hops) passes through one ``RetailerRateLimiter``. It owns, per retailer:

- one in-flight request at a time and a minimum spacing between request starts;
- a cooldown after HTTP 429 (or a 503 with ``Retry-After``) that applies to the retailer, not to
  the failed URL, so the next URL cannot start before the server's requested wait is over;
- a pause after N consecutive 429 responses (default 2, the second sent only after the cooldown),
  a bounded recovery probe once the pause is over, and conservative spacing after a good probe;
- persistent state (UTC timestamps) so restarting a command does not reset a cooldown, a pause or a
  failure streak. Waits inside a process use the monotonic clock; the file stores UTC instants.

A wait the run cannot afford (longer than ``max_single_wait_seconds`` or than the remaining run
budget) is never shortened and never slept: the request is refused with ``RetailerDeferred`` and
the caller records the work as deferred. Nothing here knows about targets or matching.
"""

from __future__ import annotations

import email.utils
import json
import os
import random
import re
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

STATE_VERSION = 1
MAX_COOLDOWN_SECONDS = 366 * 24 * 3600.0  # Arithmetic guard only; real waits are deferred first.

# Hosts verified to belong to one retailer (docs/adapters/*.md, checked 2026-10-01). Aliases are
# the redirecting domains; the adapters still refuse requests to them, but spacing and cooldowns
# must not be split by the spelling of a host. A commerce platform is never an alias: two shops on
# Shopify are two retailers. ``pccentral.com.ph`` is an unrelated site and is deliberately absent.
RETAILER_HOSTS: Mapping[str, str] = {
    "laptopfactory.com.ph": "laptop_factory",
    "ecommerce.datablitz.com.ph": "datablitz",
    "datablitz.com.ph": "datablitz",
    "www.datablitz.com.ph": "datablitz",
    "pcx.com.ph": "pcx",
    "www.pcx.com.ph": "pcx",
    "pccentralcomputerstore.com": "pcc",
    "www.pccentralcomputerstore.com": "pcc",
    "www.octagon.com.ph": "oct",
    "octagon.com.ph": "oct",
}


def retailer_for_host(host: str) -> str | None:
    return RETAILER_HOSTS.get(host.strip().lower().rstrip("."))


def retailer_for_url(url: str, default: str = "") -> str:
    """The retailer a URL belongs to by verified host, else ``default``."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    return retailer_for_host(host) or default


# Retry-After

_DELAY_SECONDS = re.compile(r"\d{1,12}")


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Seconds to wait from a ``Retry-After`` header, or ``None`` when absent or invalid.

    Both RFC 9110 forms: ``delay-seconds`` (digits only) and an HTTP-date (IMF-fixdate and the two
    obsolete forms). A date in the past is a valid request to retry now (0). Signs, decimals,
    words and empty values are invalid and the caller falls back to its own backoff.
    """
    if value is None:
        return None
    text = value.strip()
    if _DELAY_SECONDS.fullmatch(text):
        return min(float(int(text)), MAX_COOLDOWN_SECONDS)
    if not text or not re.search(r"[A-Za-z]", text) or not re.search(r"\d", text):
        return None
    try:
        moment = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)  # HTTP-dates are GMT.
    return min(max(0.0, (moment - now).total_seconds()), MAX_COOLDOWN_SECONDS)


# Policy, state, errors


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class RateLimitPolicy:
    """Pacing and pause rules. Defaults are experimental, not published retailer limits."""

    min_interval_seconds: float = 10.0
    retailer_min_intervals: Mapping[str, float] = field(default_factory=dict)
    backoff_initial_seconds: float = 60.0  # 429 without a valid Retry-After
    backoff_multiplier: float = 2.0
    backoff_max_seconds: float = 900.0
    jitter_fraction: float = 0.25  # Added on top of the backoff, never subtracted.
    pause_after_consecutive_429: int = 2
    pause_seconds: float = 1800.0
    pause_max_seconds: float = 21600.0
    max_probes_per_pause: int = 2
    recovery_spacing_multiplier: float = 2.0
    recovery_requests: int = 3
    max_single_wait_seconds: float = 600.0
    max_run_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.min_interval_seconds < 0 or any(
            v < 0 for v in self.retailer_min_intervals.values()
        ):
            raise ValueError("Request intervals must not be negative.")
        if self.backoff_initial_seconds < 0 or self.backoff_max_seconds < 0:
            raise ValueError("Backoff seconds must not be negative.")
        if self.backoff_multiplier < 1 or self.recovery_spacing_multiplier < 1:
            raise ValueError("Multipliers must be at least 1.")
        if not 0 <= self.jitter_fraction <= 1:
            raise ValueError("jitter_fraction must be between 0 and 1.")
        if self.pause_after_consecutive_429 < 1:
            raise ValueError("pause_after_consecutive_429 must be at least 1.")
        if self.pause_seconds < 0 or self.pause_max_seconds < self.pause_seconds:
            raise ValueError("pause_seconds must be between 0 and pause_max_seconds.")
        if self.max_probes_per_pause < 1 or self.recovery_requests < 0:
            raise ValueError("Probe and recovery counts must not be negative (probes: >= 1).")
        if self.max_single_wait_seconds < 0 or (
            self.max_run_seconds is not None and self.max_run_seconds < 0
        ):
            raise ValueError("Wait and run budgets must not be negative.")

    def interval_for(self, retailer_id: str) -> float:
        return self.retailer_min_intervals.get(retailer_id, self.min_interval_seconds)


class DeferReason(StrEnum):
    PAUSED = "RETAILER_PAUSED"
    COOLDOWN_EXCEEDS_BUDGET = "COOLDOWN_EXCEEDS_BUDGET"
    PROBE_LIMIT = "PROBE_LIMIT_REACHED"


class RateLimitStateError(Exception):
    """The state file is unreadable. Never silently reset: that would drop a live cooldown."""


class RetailerDeferred(Exception):
    """No request was sent: the retailer is paused or the required wait is not affordable."""

    def __init__(self, retailer_id: str, reason: DeferReason, retry_not_before: datetime,
                 wait_seconds: float) -> None:
        super().__init__(
            f"{retailer_id} deferred ({reason.value}); not before "
            f"{retry_not_before.strftime('%Y-%m-%dT%H:%M:%SZ')}."
        )
        self.retailer_id = retailer_id
        self.reason = reason
        self.retry_not_before = retry_not_before
        self.wait_seconds = wait_seconds


@dataclass
class RetailerState:
    consecutive_429: int = 0
    pause_count: int = 0
    probes_this_pause: int = 0
    recovery_left: int = 0
    last_429_at: datetime | None = None
    last_retry_after: str = ""
    last_success_at: datetime | None = None
    attempts_total: int = 0
    rate_limited_total: int = 0
    # Monotonic deadlines (in-process truth); the file stores their UTC equivalents.
    cooldown_deadline: float = 0.0
    pause_deadline: float | None = None
    next_start: float = 0.0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


@dataclass(frozen=True)
class RateLimitDecision:
    cooldown_seconds: float
    retry_after_seconds: float | None
    source: str  # RETRY_AFTER_SECONDS | RETRY_AFTER_DATE | BACKOFF
    paused: bool
    retry_not_before: datetime
    streak: int


@dataclass
class RunStats:
    attempts: int = 0
    successes: int = 0
    rate_limited: int = 0
    other_failures: int = 0
    deferred: int = 0
    waited_seconds: float = 0.0
    probes: int = 0
    pauses: int = 0


def _iso(moment: datetime | None) -> str:
    return "" if moment is None else moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(text: object) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        raise RateLimitStateError(f"Unreadable timestamp {text!r}.") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class Slot:
    """One permitted request. Report its outcome once, before leaving the ``with`` block."""

    def __init__(self, limiter: RetailerRateLimiter, retailer_id: str, context: Mapping[str, str]):
        self._limiter = limiter
        self.retailer_id = retailer_id
        self._context = dict(context)
        self.recorded = False
        self.decision: RateLimitDecision | None = None
        self.probe = False

    def record(
        self,
        status_code: int,
        retry_after: str | None = None,
        evidence: Mapping[str, str] | None = None,
    ) -> RateLimitDecision | None:
        self.recorded = True
        self.decision = self._limiter._record(self, status_code, retry_after, evidence)
        return self.decision

    def record_transport_error(self) -> None:
        self.recorded = True
        self._limiter._record(self, 0, None)

    def cancel(self) -> None:
        """Nothing reached the network (refused URL, exhausted budget): not an attempt."""
        self.recorded = True

    @property
    def context(self) -> Mapping[str, str]:
        return self._context


class RetailerRateLimiter:
    """The single scheduling path for every retailer request in a process."""

    def __init__(
        self,
        policy: RateLimitPolicy | None = None,
        clock: Clock | None = None,
        state_path: Path | str | None = None,
        rng: random.Random | None = None,
        run_started: float | None = None,
    ) -> None:
        from .scraping import SystemClock  # Local import: scraping imports this module.

        self.policy = policy or RateLimitPolicy()
        self.clock: Clock = clock or SystemClock()
        self.state_path = Path(state_path) if state_path else None
        self.rng = rng or random.Random()
        self._states: dict[str, RetailerState] = {}
        self._stats: dict[str, RunStats] = {}
        self._guard = threading.RLock()
        self.events: list[dict[str, str]] = []
        started = self.clock.monotonic() if run_started is None else run_started
        self.run_deadline = (
            None if self.policy.max_run_seconds is None else started + self.policy.max_run_seconds
        )
        self.loaded_from_state = False
        if self.state_path is not None and self.state_path.is_file():
            self._load()

    # Keys and state

    @staticmethod
    def key_for(url: str, retailer_id: str) -> str:
        """Retailer key: a verified host decides, else the caller's retailer ID."""
        return retailer_for_url(url, retailer_id)

    def _state(self, retailer_id: str) -> RetailerState:
        with self._guard:
            return self._states.setdefault(retailer_id, RetailerState())

    def stats(self, retailer_id: str) -> RunStats:
        with self._guard:
            return self._stats.setdefault(retailer_id, RunStats())

    def context_scope(self, **fields: str):
        return _ContextScope(fields)

    @property
    def _context(self) -> dict[str, str]:
        return getattr(_LOCAL, "context", {})

    # Persistence

    def _load(self) -> None:
        assert self.state_path is not None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data.get("version") != STATE_VERSION or not isinstance(data.get("retailers"), dict):
                raise ValueError("unsupported layout")
        except (OSError, ValueError) as error:
            raise RateLimitStateError(
                f"Rate-limit state {self.state_path} is unreadable ({error}); fix or remove it "
                "deliberately: silently resetting would drop a live cooldown."
            ) from None
        now_utc, now_mono = self.clock.now().astimezone(UTC), self.clock.monotonic()
        for retailer_id, raw in data["retailers"].items():
            state = RetailerState()
            try:
                state.consecutive_429 = int(raw.get("consecutive_429", 0))
                state.pause_count = int(raw.get("pause_count", 0))
                state.probes_this_pause = int(raw.get("probes_this_pause", 0))
                state.recovery_left = int(raw.get("recovery_left", 0))
                state.attempts_total = int(raw.get("attempts_total", 0))
                state.rate_limited_total = int(raw.get("rate_limited_total", 0))
                state.last_retry_after = str(raw.get("last_retry_after", ""))
                state.last_429_at = _parse_iso(raw.get("last_429_at"))
                state.last_success_at = _parse_iso(raw.get("last_success_at"))
                cooldown = _parse_iso(raw.get("cooldown_until"))
                paused = _parse_iso(raw.get("paused_until"))
            except (TypeError, ValueError, AttributeError) as error:
                raise RateLimitStateError(
                    f"Rate-limit state {self.state_path} is invalid for {retailer_id}: {error}"
                ) from None
            if cooldown is not None:
                state.cooldown_deadline = now_mono + max(0.0, (cooldown - now_utc).total_seconds())
            if paused is not None:
                state.pause_deadline = now_mono + max(0.0, (paused - now_utc).total_seconds())
            self._states[retailer_id] = state
        self.loaded_from_state = True

    def _save(self) -> None:
        if self.state_path is None:
            return
        now_utc, now_mono = self.clock.now().astimezone(UTC), self.clock.monotonic()

        def utc(deadline: float | None) -> str:
            if deadline is None or deadline <= now_mono:
                return ""
            return _iso(now_utc + timedelta(seconds=min(deadline - now_mono, MAX_COOLDOWN_SECONDS)))

        retailers = {}
        for retailer_id, state in sorted(self._states.items()):
            retailers[retailer_id] = {
                "consecutive_429": state.consecutive_429,
                "pause_count": state.pause_count,
                "probes_this_pause": state.probes_this_pause,
                "recovery_left": state.recovery_left,
                "cooldown_until": utc(state.cooldown_deadline),
                # A pause whose time has passed stays recorded as paused (a probe is due).
                "paused": state.pause_deadline is not None,
                "paused_until": _iso(now_utc + timedelta(seconds=max(
                    0.0, (state.pause_deadline or now_mono) - now_mono)))
                if state.pause_deadline is not None else "",
                "last_429_at": _iso(state.last_429_at),
                "last_retry_after": state.last_retry_after,
                "last_success_at": _iso(state.last_success_at),
                "attempts_total": state.attempts_total,
                "rate_limited_total": state.rate_limited_total,
            }
        payload = {"version": STATE_VERSION, "saved_at_utc": _iso(now_utc),
                   "retailers": retailers}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(self.state_path.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    # Status (for schedulers; sends nothing)

    def wait_needed(self, retailer_id: str) -> tuple[str, float]:
        """``(status, seconds)``: READY, WAIT, PAUSED or PROBE_DUE, and the wait before a send."""
        state = self._state(retailer_id)
        now = self.clock.monotonic()
        if state.pause_deadline is not None:
            if now < state.pause_deadline:
                return "PAUSED", state.pause_deadline - now
            return "PROBE_DUE", max(0.0, state.next_start - now, state.cooldown_deadline - now)
        wait = max(0.0, state.next_start - now, state.cooldown_deadline - now)
        return ("WAIT", wait) if wait > 0 else ("READY", 0.0)

    def not_before_utc(self, retailer_id: str) -> datetime:
        state = self._state(retailer_id)
        now = self.clock.monotonic()
        deadline = max(state.cooldown_deadline, state.pause_deadline or 0.0, state.next_start)
        return self.clock.now().astimezone(UTC) + timedelta(seconds=max(0.0, deadline - now))

    def is_paused(self, retailer_id: str) -> bool:
        return self.wait_needed(retailer_id)[0] == "PAUSED"

    # Scheduling

    @contextmanager
    def slot(self, retailer_id: str):
        """Wait as required, then hold the retailer's single in-flight slot for one request."""
        state = self._state(retailer_id)
        with state.lock:
            slot = self._acquire(retailer_id, state)
            try:
                yield slot
            finally:
                if not slot.recorded:
                    slot.record_transport_error()

    def _defer(self, retailer_id: str, reason: DeferReason, wait: float) -> RetailerDeferred:
        self.stats(retailer_id).deferred += 1
        until = self.clock.now().astimezone(UTC) + timedelta(seconds=min(wait,
                                                                       MAX_COOLDOWN_SECONDS))
        return RetailerDeferred(retailer_id, reason, until, wait)

    def _acquire(self, retailer_id: str, state: RetailerState) -> Slot:
        policy = self.policy
        now = self.clock.monotonic()
        probe = False
        if state.pause_deadline is not None:
            if now < state.pause_deadline:
                raise self._defer(retailer_id, DeferReason.PAUSED, state.pause_deadline - now)
            if state.probes_this_pause >= policy.max_probes_per_pause:
                # Probes that proved nothing extend the pause instead of probing forever.
                state.pause_count += 1
                state.probes_this_pause = 0
                length = self._pause_length(state, 0.0)
                state.pause_deadline = now + length
                self._save()
                raise self._defer(retailer_id, DeferReason.PROBE_LIMIT, length)
            probe = True
        ready = max(state.next_start, state.cooldown_deadline)
        wait = max(0.0, ready - now)
        if wait > 0:
            over_run = self.run_deadline is not None and now + wait > self.run_deadline
            if wait > policy.max_single_wait_seconds or over_run:
                raise self._defer(retailer_id, DeferReason.COOLDOWN_EXCEEDS_BUDGET, wait)
            self.clock.sleep(wait)
            self.stats(retailer_id).waited_seconds += wait
        started = self.clock.monotonic()
        interval = policy.interval_for(retailer_id)
        if state.recovery_left > 0:
            interval *= policy.recovery_spacing_multiplier
        state.next_start = started + interval
        slot = Slot(self, retailer_id, self._context)
        slot.probe = probe
        if probe:
            state.probes_this_pause += 1
            self.stats(retailer_id).probes += 1
        return slot

    def note_backoff(self, retailer_id: str, seconds: float) -> None:
        """A retailer-wide wait after a retryable failure (5xx, timeout) before the next try."""
        state = self._state(retailer_id)
        with self._guard:
            state.cooldown_deadline = max(
                state.cooldown_deadline,
                self.clock.monotonic() + min(max(0.0, seconds), MAX_COOLDOWN_SECONDS))
            self._save()

    def _pause_length(self, state: RetailerState, requested: float) -> float:
        policy = self.policy
        escalated = min(policy.pause_max_seconds,
                        policy.pause_seconds * 2 ** max(0, state.pause_count - 1))
        return max(requested, escalated)

    def _backoff(self, streak: int) -> float:
        policy = self.policy
        base = min(policy.backoff_max_seconds,
                   policy.backoff_initial_seconds * policy.backoff_multiplier ** (streak - 1))
        return base * (1 + self.rng.uniform(0, policy.jitter_fraction))

    def _record(
        self,
        slot: Slot,
        status: int,
        retry_after: str | None,
        evidence: Mapping[str, str] | None = None,
    ):
        retailer_id = slot.retailer_id
        state = self._state(retailer_id)
        stats = self.stats(retailer_id)
        decision: RateLimitDecision | None = None
        with self._guard:
            now_utc = self.clock.now().astimezone(UTC)
            now = self.clock.monotonic()
            state.attempts_total += 1
            stats.attempts += 1
            if status == 429:
                stats.rate_limited += 1
                state.rate_limited_total += 1
                state.consecutive_429 += 1
                state.last_429_at = now_utc
                state.last_retry_after = (retry_after or "").strip()[:80]
                requested = parse_retry_after(retry_after, now_utc)
                if requested is not None:
                    seconds = requested
                    source = ("RETRY_AFTER_SECONDS"
                              if _DELAY_SECONDS.fullmatch((retry_after or "").strip())
                              else "RETRY_AFTER_DATE")
                else:
                    seconds, source = self._backoff(state.consecutive_429), "BACKOFF"
                state.cooldown_deadline = max(state.cooldown_deadline, now + seconds)
                paused = (state.consecutive_429 >= self.policy.pause_after_consecutive_429
                          or slot.probe)
                if paused:
                    state.pause_count += 1
                    state.probes_this_pause = 0
                    length = self._pause_length(state, seconds)
                    state.pause_deadline = now + length
                    state.recovery_left = 0
                    stats.pauses += 1
                    until = now_utc + timedelta(seconds=length)
                else:
                    until = now_utc + timedelta(seconds=seconds)
                decision = RateLimitDecision(seconds, requested, source, paused, until,
                                             state.consecutive_429)
                self.events.append({
                    "at_utc": _iso(now_utc), "retailer_id": retailer_id,
                    "purpose": slot.context.get("purpose", ""),
                    "url": slot.context.get("url", ""),
                    "retry_after_header": state.last_retry_after,
                    "retry_after_seconds": "" if requested is None else f"{requested:g}",
                    "cooldown_source": source, "cooldown_seconds": f"{seconds:.1f}",
                    "consecutive_429": str(state.consecutive_429),
                    "probe": "TRUE" if slot.probe else "FALSE",
                    "paused": "TRUE" if paused else "FALSE",
                    "retry_not_before_utc": _iso(until),
                    "response_evidence": json.dumps(dict(evidence or {}), sort_keys=True),
                })
            elif 200 <= status < 300:
                stats.successes += 1
                state.last_success_at = now_utc
                state.consecutive_429 = 0
                if state.pause_deadline is not None or slot.probe:
                    # A good probe resumes work, conservatively.
                    state.pause_deadline = None
                    state.probes_this_pause = 0
                    state.recovery_left = self.policy.recovery_requests
                elif state.recovery_left > 0:
                    state.recovery_left -= 1
                    if state.recovery_left == 0:
                        state.pause_count = 0
            else:
                stats.other_failures += 1 if status >= 400 or status == 0 else 0
                if status == 503 and retry_after is not None:
                    seconds = parse_retry_after(retry_after, now_utc)
                    if seconds is not None:
                        state.cooldown_deadline = max(state.cooldown_deadline, now + seconds)
            self._save()
        return decision

    # Reporting

    def snapshot(self) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        now = self.clock.monotonic()
        for retailer_id in sorted(set(self._states) | set(self._stats)):
            state, stats = self._state(retailer_id), self.stats(retailer_id)
            status, wait = self.wait_needed(retailer_id)
            out[retailer_id] = {
                "status": status,
                "seconds_until_ready": round(wait, 1),
                "not_before_utc": _iso(self.not_before_utc(retailer_id)) if status != "READY"
                else "",
                "consecutive_429": state.consecutive_429,
                "pause_count": state.pause_count,
                "paused": state.pause_deadline is not None,
                "paused_for_seconds": round(max(0.0, (state.pause_deadline or now) - now), 1),
                "recovery_requests_left": state.recovery_left,
                "last_retry_after_header": state.last_retry_after,
                "run": {
                    "http_attempts": stats.attempts, "successes": stats.successes,
                    "rate_limited_429": stats.rate_limited,
                    "other_failures": stats.other_failures,
                    "requests_refused_not_sent": stats.deferred,
                    "seconds_waited": round(stats.waited_seconds, 1),
                    "probes": stats.probes, "pauses": stats.pauses,
                },
                "lifetime": {"http_attempts": state.attempts_total,
                             "rate_limited_429": state.rate_limited_total},
            }
        return out


_LOCAL = threading.local()


class _ContextScope:
    def __init__(self, fields: Mapping[str, str]) -> None:
        self._fields = dict(fields)
        self._previous: dict[str, str] = {}

    def __enter__(self) -> None:
        self._previous = getattr(_LOCAL, "context", {})
        _LOCAL.context = {**self._previous, **self._fields}

    def __exit__(self, *exc: object) -> None:
        _LOCAL.context = self._previous


EVENT_COLUMNS = (
    "at_utc", "retailer_id", "purpose", "url", "retry_after_header", "retry_after_seconds",
    "cooldown_source", "cooldown_seconds", "consecutive_429", "probe", "paused",
    "retry_not_before_utc", "response_evidence",
)


# Command-line wiring shared by the scripts that send requests


def add_arguments(parser) -> None:
    group = parser.add_argument_group("rate limiting (shared per-retailer limiter)")
    group.add_argument("--rate-limit-state", type=Path,
                       help="Persistent per-retailer cooldown, pause and streak state (JSON). "
                            "Default for live runs: data/history/retailer_rate_limits.json. "
                            "Offline transports use no state file unless this is given.")
    group.add_argument("--rate-limit-pause-after", type=int, default=2,
                       help="Pause a retailer after this many consecutive HTTP 429 (default 2; "
                            "the next attempt waits for the full cooldown).")
    group.add_argument("--rate-limit-backoff-initial", type=float, default=60.0,
                       help="Cooldown seconds after a 429 without a valid Retry-After "
                            "(default 60; doubles per consecutive 429 with added jitter).")
    group.add_argument("--rate-limit-backoff-max", type=float, default=900.0)
    group.add_argument("--rate-limit-pause-seconds", type=float, default=1800.0,
                       help="Minimum pause once a retailer hits the 429 limit (default 1800; "
                            "doubles per repeated pause, never below the server's Retry-After).")
    group.add_argument("--max-single-wait-seconds", type=float, default=600.0,
                       help="A required wait longer than this is not slept: the retailer is "
                            "deferred and the work saved (default 600).")
    group.add_argument("--max-run-seconds", type=float,
                       help="Wall-clock budget for the run's waits; a wait that would pass it "
                            "defers the retailer instead.")


def policy_from_args(args, min_interval: float) -> RateLimitPolicy:
    return RateLimitPolicy(
        min_interval_seconds=min_interval,
        backoff_initial_seconds=args.rate_limit_backoff_initial,
        backoff_max_seconds=args.rate_limit_backoff_max,
        pause_after_consecutive_429=args.rate_limit_pause_after,
        pause_seconds=args.rate_limit_pause_seconds,
        pause_max_seconds=max(args.rate_limit_pause_seconds, 21600.0),
        max_single_wait_seconds=args.max_single_wait_seconds,
        max_run_seconds=args.max_run_seconds,
    )


def default_state_path(root: Path) -> Path:
    return root / "data" / "history" / "retailer_rate_limits.json"


def resolve_state_path(args, root: Path, live_transport: bool) -> Path | None:
    """An explicit path always; the default file only for a live transport."""
    if getattr(args, "rate_limit_state", None):
        return args.rate_limit_state
    return default_state_path(root) if live_transport else None


def limiter_summary(limiter: RetailerRateLimiter) -> dict[str, object]:
    return {
        "policy": {
            "min_interval_seconds": limiter.policy.min_interval_seconds,
            "pause_after_consecutive_429": limiter.policy.pause_after_consecutive_429,
            "backoff_initial_seconds": limiter.policy.backoff_initial_seconds,
            "backoff_max_seconds": limiter.policy.backoff_max_seconds,
            "jitter_fraction": limiter.policy.jitter_fraction,
            "pause_seconds": limiter.policy.pause_seconds,
            "max_single_wait_seconds": limiter.policy.max_single_wait_seconds,
            "max_run_seconds": limiter.policy.max_run_seconds,
            "in_flight_per_retailer": 1,
        },
        "state_file": str(limiter.state_path) if limiter.state_path else "",
        "state_loaded": limiter.loaded_from_state,
        "retailers": limiter.snapshot(),
        "rate_limit_events": len(limiter.events),
    }


