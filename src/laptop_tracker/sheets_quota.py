"""Quota-aware access to the Google Sheets API: one retry policy, one throttle, one metadata cache.

Why this exists. A publication makes dozens of read requests in a few minutes (a worksheet listing
before every tab read and every snapshot, the tab contents, the readback). Google answers more than
60 reads per minute per user with HTTP 429, and on 2026-11-12 that stopped a hosted publication
while it was taking its pre-publication backups. Three pieces keep that from recurring:

* :class:`QuotaAwareHTTPClient` is gspread's HTTP layer with a throttle and a bounded retry in
  front of it, so *every* Sheets call (``worksheets()``, ``fetch_sheet_metadata``,
  ``get_all_values``, ``batch_update`` ...) is protected by the same policy.
* :func:`call_with_retry` is that policy, usable on its own and tested without a network.
* :class:`CachedSpreadsheet` lists the worksheets once per publication phase instead of once per
  tab. Only the *listing* is cached; tab contents are always read fresh.

Retry policy (see :class:`RetryPolicy`): at most 6 attempts; the delay before retry ``n`` is
``min(60, 4 * 2**(n-1))`` seconds plus up to 25 % random jitter (so 4, 8, 16, 32, 60 s); a
``Retry-After`` header is honoured as a minimum wait and a request is abandoned when the server
asks for more than 120 s; the total wait is capped at 240 s.

What is retried:

* HTTP 429 and quota-style 403 (``rateLimitExceeded``): always. Google rejects such a request
  before running it, so repeating even a write cannot apply it twice.
* HTTP 500/502/503/504/408 and network errors: **reads only**. The outcome of a write that failed
  that way is unknown, so a write is never repeated blindly; the caller's own recovery (run-log
  status, backups, rollback) decides what to do.
* Authentication (401), permission (403 without a quota reason), validation (400), missing
  resources (404) and every other error: never retried, raised unchanged.

When the budget is spent :class:`SheetsRetryExhausted` says how many attempts were made, how long
was waited and what Google answered. Log lines carry the method, a redacted endpoint, the status,
the delay and the attempt number; never headers, tokens, request bodies or the spreadsheet ID.
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

import requests
from google.auth.exceptions import RefreshError, TransportError
from gspread.exceptions import APIError
from gspread.http_client import HTTPClient

LOG = logging.getLogger("laptop_tracker.sheets_quota")

T = TypeVar("T")

# Failure kinds.
QUOTA = "QUOTA"
SERVER = "SERVER"
NETWORK = "NETWORK"
PERMANENT = "PERMANENT"

_QUOTA_REASONS = {
    "ratelimitexceeded",
    "userratelimitexceeded",
    "quotaexceeded",
    "resource_exhausted",
}
_SERVER_STATUSES = {408, 500, 502, 503, 504}
_NETWORK_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    TransportError,
    ConnectionError,
    TimeoutError,
)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with jitter."""

    max_attempts: int = 6  # the first try plus five retries
    base_delay: float = 4.0
    multiplier: float = 2.0
    max_delay: float = 60.0
    jitter: float = 0.25  # up to +25 % of the computed delay
    max_total_wait: float = 240.0
    max_retry_after: float = 120.0

    def backoff(self, retry_number: int, rng: random.Random | Any = random) -> float:
        """Seconds to wait before retry ``retry_number`` (1 for the first retry)."""
        raw = min(self.max_delay, self.base_delay * self.multiplier ** (retry_number - 1))
        return raw + rng.uniform(0.0, self.jitter * raw)


DEFAULT_POLICY = RetryPolicy()


@dataclass(frozen=True)
class Failure:
    kind: str
    status: int | None = None
    retry_after: float | None = None
    reason: str = ""

    @property
    def transient(self) -> bool:
        return self.kind != PERMANENT


class SheetsRetryExhausted(RuntimeError):
    """A transient Sheets error that outlasted the retry policy (or asked for too long a wait)."""

    def __init__(
        self,
        description: str,
        *,
        attempts: int,
        waited: float,
        failure: Failure,
        detail: str = "",
    ) -> None:
        self.description = description
        self.attempts = attempts
        self.waited = waited
        self.failure = failure
        status = f"HTTP {failure.status}" if failure.status else failure.kind.lower()
        super().__init__(
            f"{description}: still failing after {attempts} attempt(s) and {waited:.0f}s of "
            f"waiting ({status}, {failure.kind.lower()}): {failure.reason or 'no detail'}."
            + (f" {detail}" if detail else "")
        )


def _response(exc: BaseException) -> Any:
    return getattr(exc, "response", None)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """``Retry-After`` as seconds from now: a number of seconds or an HTTP date; else None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def _error_body(response: Any) -> Mapping:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - an unreadable body is simply no detail
        return {}
    error = body.get("error") if isinstance(body, Mapping) else None
    return error if isinstance(error, Mapping) else {}


def classify(exc: BaseException) -> Failure:
    """Sort an exception into transient (quota, server, network) or permanent."""
    if isinstance(exc, SheetsRetryExhausted):
        return Failure(PERMANENT, exc.failure.status, reason="retry policy already exhausted")
    if isinstance(exc, RefreshError):
        return Failure(PERMANENT, None, reason="authentication failed")
    response = _response(exc)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None) if isinstance(exc, APIError) else None
    if isinstance(status, int) and status > 0:
        error = _error_body(response) if response is not None else {}
        message = str(error.get("message") or "")[:200]
        reasons = {
            str(item.get("reason", "")).lower()
            for item in error.get("errors", []) or []
            if isinstance(item, Mapping)
        } | {str(error.get("status", "")).lower()}
        headers = getattr(response, "headers", None) or {}
        retry_after = parse_retry_after(headers.get("Retry-After") if headers else None)
        if status == 429 or (status == 403 and reasons & _QUOTA_REASONS):
            return Failure(QUOTA, status, retry_after, message or "quota exceeded")
        if status in _SERVER_STATUSES:
            return Failure(SERVER, status, retry_after, message or "server error")
        return Failure(PERMANENT, status, None, message)
    if isinstance(exc, _NETWORK_ERRORS):
        return Failure(NETWORK, None, None, type(exc).__name__)
    return Failure(PERMANENT, None, None, type(exc).__name__)


def call_with_retry(
    operation: Callable[[], T],
    *,
    description: str,
    idempotent: bool,
    policy: RetryPolicy = DEFAULT_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | Any = random,
    log: logging.Logger = LOG,
) -> T:
    """Run ``operation``; retry transient failures within ``policy``.

    ``idempotent`` is True for reads. A write (False) is retried only after a quota rejection,
    which Google issues before executing the request; any other write failure is raised at once
    because its outcome is unknown. Permanent errors are raised unchanged, never retried.
    """
    waited = 0.0
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            failure = classify(exc)
            if not failure.transient:
                raise
            if failure.kind != QUOTA and not idempotent:
                log.warning(
                    "%s failed (%s %s); a write is not repeated because its outcome is unknown.",
                    description,
                    failure.kind.lower(),
                    failure.status or failure.reason,
                )
                raise
            if attempt == policy.max_attempts:
                raise SheetsRetryExhausted(
                    description, attempts=attempt, waited=waited, failure=failure
                ) from exc
            delay = policy.backoff(attempt, rng)
            if failure.retry_after is not None:
                if failure.retry_after > policy.max_retry_after:
                    raise SheetsRetryExhausted(
                        description,
                        attempts=attempt,
                        waited=waited,
                        failure=failure,
                        detail=(
                            f"Google asked to wait {failure.retry_after:.0f}s, more than the "
                            f"{policy.max_retry_after:.0f}s this job will wait."
                        ),
                    ) from exc
                delay = max(delay, failure.retry_after)
            if waited + delay > policy.max_total_wait:
                raise SheetsRetryExhausted(
                    description,
                    attempts=attempt,
                    waited=waited,
                    failure=failure,
                    detail=f"The {policy.max_total_wait:.0f}s retry budget would be exceeded.",
                ) from exc
            log.warning(
                "%s: %s (HTTP %s); retry %d of %d in %.1fs.",
                description,
                failure.kind.lower(),
                failure.status or "-",
                attempt,
                policy.max_attempts - 1,
                delay,
            )
            sleep(delay)
            waited += delay
    raise AssertionError("unreachable")  # pragma: no cover


class RequestThrottle:
    """At most ``max_requests`` in any ``window`` seconds (sliding window); blocks when full."""

    def __init__(
        self,
        max_requests: int,
        window: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        self.max_requests = max_requests
        self.window = window
        self._clock = clock
        self._sleep = sleep
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()
        self.total_waited = 0.0

    def acquire(self) -> float:
        """Reserve one request slot; returns the seconds spent waiting for it."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= self.window:
                    self._stamps.popleft()
                if len(self._stamps) < self.max_requests:
                    self._stamps.append(now)
                    self.total_waited += waited
                    return waited
                pause = self.window - (now - self._stamps[0])
            self._sleep(pause)
            waited += pause


# Google's default is 60 read and 60 write requests per minute per user; stay well under both.
DEFAULT_READS_PER_MINUTE = 40
DEFAULT_WRITES_PER_MINUTE = 40


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 1 else default


class QuotaGuard:
    """The process-wide throttles and retry policy shared by every Sheets client."""

    def __init__(
        self,
        *,
        policy: RetryPolicy = DEFAULT_POLICY,
        reads_per_minute: int | None = None,
        writes_per_minute: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | Any = random,
    ) -> None:
        self.policy = policy
        self.sleep = sleep
        self.rng = rng
        self.reads = RequestThrottle(
            reads_per_minute or _env_int("SHEETS_READS_PER_MINUTE", DEFAULT_READS_PER_MINUTE),
            clock=clock,
            sleep=sleep,
        )
        self.writes = RequestThrottle(
            writes_per_minute or _env_int("SHEETS_WRITES_PER_MINUTE", DEFAULT_WRITES_PER_MINUTE),
            clock=clock,
            sleep=sleep,
        )
        self.requests = {"read": 0, "write": 0}
        self.retries = 0

    def run(self, operation: Callable[[], T], *, description: str, read: bool) -> T:
        throttle = self.reads if read else self.writes
        kind = "read" if read else "write"

        def attempt() -> T:
            throttle.acquire()
            self.requests[kind] += 1
            return operation()

        def counted_sleep(seconds: float) -> None:
            self.retries += 1
            self.sleep(seconds)

        return call_with_retry(
            attempt,
            description=description,
            idempotent=read,
            policy=self.policy,
            sleep=counted_sleep,
            rng=self.rng,
        )


_default_guard: QuotaGuard | None = None
_guard_lock = threading.Lock()


def default_guard() -> QuotaGuard:
    global _default_guard
    with _guard_lock:
        if _default_guard is None:
            _default_guard = QuotaGuard()
        return _default_guard


def reset_default_guard(guard: QuotaGuard | None = None) -> None:
    """Replace (or clear) the process-wide guard; for tests."""
    global _default_guard
    with _guard_lock:
        _default_guard = guard


_ID_SEGMENT = re.compile(r"(/spreadsheets/)[^/:?#]+")


def redact_endpoint(endpoint: str) -> str:
    """The endpoint without scheme, host, query or the spreadsheet ID."""
    path = re.sub(r"^https?://[^/]+", "", str(endpoint)).split("?", 1)[0]
    return _ID_SEGMENT.sub(r"\1<id>", path)


class QuotaAwareHTTPClient(HTTPClient):
    """gspread's HTTP client with the shared throttle and retry in front of every request.

    Pass it as ``http_client=`` to ``gspread.authorize`` / ``gspread.Client``. ``GET`` requests are
    reads (retried on quota, server and network errors); every other method is a write (retried on
    quota rejections only)."""

    def __init__(self, auth, session=None, *, guard: QuotaGuard | None = None) -> None:
        super().__init__(auth, session)
        self._guard = guard

    def request(  # type: ignore[override]
        self,
        method,
        endpoint,
        params=None,
        data=None,
        json=None,
        files=None,
        headers=None,
    ):
        guard = self._guard or default_guard()
        read = str(method).lower() == "get"
        return guard.run(
            lambda: HTTPClient.request(self, method, endpoint, params, data, json, files, headers),
            description=f"Sheets {str(method).upper()} {redact_endpoint(endpoint)}",
            read=read,
        )


# Worksheet listing cache -------------------------------------------------------------------


def _changes_sheet_list(body: Mapping) -> bool:
    """True when a ``batchUpdate`` body can add, remove, rename, hide or reorder worksheets."""
    for request in body.get("requests", []) or []:
        for kind, payload in request.items():
            if kind in {"addSheet", "deleteSheet", "duplicateSheet"}:
                return True
            if kind == "updateSheetProperties":
                fields = str((payload or {}).get("fields", ""))
                if fields in {"", "*"} or any(
                    f in fields.split(",") for f in ("title", "index", "hidden", "sheetId")
                ):
                    return True
    return False


class CachedSpreadsheet:
    """A spreadsheet whose worksheet *listing* is fetched once per phase.

    Everything else (tab contents via ``fetch_sheet_metadata``, ``batch_update``, ``worksheet``,
    ``id``, ``title`` ...) goes straight to the wrapped spreadsheet. The listing is dropped when a
    ``batch_update`` can change the set of worksheets, after ``add_worksheet`` / ``del_worksheet``,
    and at every phase boundary (``begin_phase``), so a decision based on it is at most one phase
    old. A tab the listing lacks is looked up once more from the API before it is reported
    missing, unless the listing is only seconds old (``find_worksheet``), so a stale listing
    cannot invent a missing tab."""

    def __init__(self, inner, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._inner = inner
        self._clock = clock
        self._listing: list | None = None
        self._listed_at = 0.0
        self.listing_requests = 0

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    @property
    def inner(self):
        return self._inner

    def worksheets(self, exclude_hidden: bool = False):
        if exclude_hidden:
            return self._inner.worksheets(exclude_hidden=True)
        if self._listing is None:
            self.refresh_worksheets()
        return list(self._listing or [])

    def refresh_worksheets(self, *, min_age: float = 0.0) -> list:
        """List the worksheets again, unless the cached listing is younger than ``min_age`` s."""
        if self._listing is not None and min_age > 0 and self._clock() - self._listed_at < min_age:
            return list(self._listing)
        self.listing_requests += 1
        self._listing = list(self._inner.worksheets())
        self._listed_at = self._clock()
        return list(self._listing)

    def invalidate(self) -> None:
        self._listing = None

    def begin_phase(self) -> CachedSpreadsheet:
        """A new publication phase: read the worksheet listing again on first use."""
        self.invalidate()
        return self

    def batch_update(self, body):
        try:
            return self._inner.batch_update(body)
        finally:
            if _changes_sheet_list(body):
                self.invalidate()

    def add_worksheet(self, *args, **kwargs):
        try:
            return self._inner.add_worksheet(*args, **kwargs)
        finally:
            self.invalidate()

    def del_worksheet(self, *args, **kwargs):
        try:
            return self._inner.del_worksheet(*args, **kwargs)
        finally:
            self.invalidate()

    def duplicate_sheet(self, *args, **kwargs):
        try:
            return self._inner.duplicate_sheet(*args, **kwargs)
        finally:
            self.invalidate()


def cached(spreadsheet, *, new_phase: bool = False) -> CachedSpreadsheet:
    """``spreadsheet`` wrapped in a :class:`CachedSpreadsheet` (itself, if it already is one)."""
    wrapped = (
        spreadsheet
        if isinstance(spreadsheet, CachedSpreadsheet)
        else CachedSpreadsheet(spreadsheet)
    )
    return wrapped.begin_phase() if new_phase else wrapped


# A cached listing younger than this is trusted even for a miss: a tab is not created or renamed
# between two requests of the same moment. An older one is listed afresh before a tab is called
# missing.
STALE_LISTING_SECONDS = 30.0


def find_worksheet(spreadsheet, title: str):
    """The worksheet called ``title`` or None. A cached listing that lacks it and is not fresh is
    refreshed once before the tab is reported missing."""
    for ws in spreadsheet.worksheets():
        if ws.title == title:
            return ws
    refresh = getattr(spreadsheet, "refresh_worksheets", None)
    if refresh is not None:
        for ws in refresh(min_age=STALE_LISTING_SECONDS):
            if ws.title == title:
                return ws
    return None


__all__ = [
    "DEFAULT_POLICY",
    "NETWORK",
    "PERMANENT",
    "QUOTA",
    "SERVER",
    "CachedSpreadsheet",
    "Failure",
    "QuotaAwareHTTPClient",
    "QuotaGuard",
    "RequestThrottle",
    "RetryPolicy",
    "SheetsRetryExhausted",
    "cached",
    "call_with_retry",
    "classify",
    "default_guard",
    "find_worksheet",
    "parse_retry_after",
    "redact_endpoint",
    "reset_default_guard",
]
