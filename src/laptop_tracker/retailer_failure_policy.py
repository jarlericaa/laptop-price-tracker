"""Retailer request failures in a weekly discovery run: counted, classified, reported, never fatal.

A failed search or page fetch is an *external data-availability problem*: the report is then less
complete, not less true. Retailer failures therefore never block publication. What the report must
not do is pretend: the affected products stay ``Not checked`` or ``Review`` (never ``No match``,
never a zero, never an older price presented as current), and the readiness gate says exactly what
was not verified. Pipeline-integrity problems are a different category and still block: discovery
files that are missing, unreadable or contradict each other (``Assessment.integrity``).

History: until 2026-11-22 any failed request blocked; the first risk-based version (PR #7) still
blocked on thresholds and on stopped or unavailable retailers. Both runs of 2026-11-22 (one failed
Octagon page; Laptop Factory answering HTTP 500 and being stopped) collected, saved and previewed
correctly and were still withheld. Here the former thresholds only grade the warning's severity.

What counts as one failed request (each failure is counted exactly once)
------------------------------------------------------------------------
* a row of ``search_requests.csv`` whose outcome is ``SEARCH_ERROR`` or ``UNSUPPORTED_LAYOUT`` (a
  search that was sent and gave no readable answer; its HTTP status, 429 included, is an attribute
  of the same row, not a second failure);
* a row of ``product_inspections.csv`` whose outcome is ``FETCH_FAILED``.

Rows answered from a saved or same-run copy (``cache`` is set) and rows never sent (``NOT_SENT_*``,
``NOT_INSPECTED_BUDGET``) are not attempts and stay out of every denominator. The summary's
``PROVIDER_FAILURE`` and ``RETAILER_STOPPED_AFTER_ERRORS`` counters are job-level views of the same
search errors: they are cross-checked against the rows, never added to them.

Each failure is classified (``classify_failure``) so that a temporary server error, a timeout, a
rate limit, a refused request and an unsupported page layout stay distinguishable in diagnostics.

Severity of the warning (it never changes the decision)
-------------------------------------------------------
* ``minor``: every threshold of :class:`FailurePolicy` holds and no retailer was stopped or
  unavailable;
* ``moderate``: the failures touch jobs in a way worth a look (concentrated on one target, a failed
  search whose job still ended as "nothing found", a job outcome that cannot be read);
* ``significant``: a threshold is exceeded, a retailer was stopped after repeated errors or
  answered nothing, or much of the plan was never executed.
"""

from __future__ import annotations

import csv
import math
import os
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

SEARCH_ANSWERED = frozenset({"RESULTS", "NO_RESULTS"})
SEARCH_FAILED = frozenset({"SEARCH_ERROR", "UNSUPPORTED_LAYOUT"})
SEARCH_ATTEMPTED = SEARCH_ANSWERED | SEARCH_FAILED
NOT_SENT_RETAILER_FAILING = "NOT_SENT_RETAILER_FAILING"
# Planned searches the run did not execute although nothing in the plan said not to. A query limit
# (NOT_SENT_QUERY_LIMIT) is a design choice of the plan and is not counted.
SEARCH_NOT_EXECUTED = frozenset(
    {
        NOT_SENT_RETAILER_FAILING,
        "NOT_SENT_BUDGET_EXHAUSTED",
        "NOT_SENT_SEARCH_LIMIT",
        "NOT_SENT_REPLAY_ONLY",
    }
)
INSPECTION_FAILED = "FETCH_FAILED"
INSPECTION_NOT_ATTEMPTED = frozenset(
    {"NOT_SENT_REPLAY_ONLY", "NOT_INSPECTED_BUDGET", "STEP_FAILED"}
)

# Job outcomes (discovery_outcomes.py, discover_products.py) by what the report shows for the job.
PRICED_OUTCOMES = frozenset(
    {
        "SUITABLE_OFFER_FOUND",
        "SUITABLE_OFFER_EXCLUDED_BY_STOCK",
        "SUITABLE_OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS",
    }
)
# The report shows the pair as priced, as an unresolved lead or as an incomplete/failed search,
# never as a confirmed absence.
VISIBLE_TERMINAL = PRICED_OUTCOMES | {
    "PAGE_FETCHED_IDENTITY_UNRESOLVED",
    "FETCH_FAILED",
    "FETCH_DEFERRED",
    "PLAUSIBLE_CANDIDATE_NOT_COLLECTED",
    "PROVIDER_FAILURE",
    "BUDGET_EXHAUSTED",
    "SEARCH_INCOMPLETE_REPLAY_ONLY",
}
VISIBLE_LEGACY = frozenset(
    {
        "SUITABLE_OFFER_FOUND",
        "PLAUSIBLE_CANDIDATE_NOT_COLLECTED",
        "PROVIDER_FAILURE",
        "BUDGET_EXHAUSTED",
        "SEARCH_INCOMPLETE_REPLAY_ONLY",
    }
)
# A job that ended here although a request failed would read as "searched; nothing found".
ABSENCE_TERMINAL = frozenset(
    {
        "NO_RESULTS_RETURNED",
        "RESULTS_ALL_REJECTED",
        "SEARCH_INCOMPLETE_RESULT_CAP",
        "SEARCH_INCOMPLETE_PAGES_UNREAD",
    }
)

# Failed requests on one target (all retailers together) that trigger the impact note.
CONCENTRATION_MIN_FAILURES = 2

SEVERITY_NONE, SEVERITY_MINOR, SEVERITY_MODERATE, SEVERITY_SIGNIFICANT = (
    "none",
    "minor",
    "moderate",
    "significant",
)

# Failure classes. ``code`` is the fetcher's error code (scraping.FetchError.code).
RATE_LIMIT, SERVER_ERROR, TIMEOUT, NETWORK_ERROR = (
    "RATE_LIMIT",
    "SERVER_ERROR",
    "TIMEOUT",
    "NETWORK_ERROR",
)
ACCESS_DENIED, NOT_FOUND, CLIENT_ERROR, HTTP_ERROR = (
    "ACCESS_DENIED",
    "NOT_FOUND",
    "CLIENT_ERROR",
    "HTTP_ERROR",
)
UNSUPPORTED_LAYOUT, REQUEST_REFUSED, OTHER_ERROR = (
    "UNSUPPORTED_LAYOUT",
    "REQUEST_REFUSED",
    "OTHER_ERROR",
)
FAILURE_LABELS = {
    RATE_LIMIT: "rate limited",
    SERVER_ERROR: "server error",
    TIMEOUT: "timeout",
    NETWORK_ERROR: "connection error",
    ACCESS_DENIED: "access denied",
    NOT_FOUND: "page not found",
    CLIENT_ERROR: "HTTP client error",
    HTTP_ERROR: "HTTP error",
    UNSUPPORTED_LAYOUT: "unsupported page layout",
    REQUEST_REFUSED: "request refused by a safeguard",
    OTHER_ERROR: "other error",
}
_REFUSED_CODES = frozenset(
    {
        "INVALID_URL",
        "HOST_NOT_ALLOWED",
        "REDIRECT_HOST_NOT_ALLOWED",
        "INVALID_REDIRECT",
        "TOO_MANY_REDIRECTS",
        "RESPONSE_TOO_LARGE",
    }
)

ENV_NAMES = {
    "max_total": "RETAILER_FAILURE_MAX_TOTAL",
    "max_per_retailer": "RETAILER_FAILURE_MAX_PER_RETAILER",
    "max_retailer_pct": "RETAILER_FAILURE_MAX_RETAILER_PCT",
    "max_total_pct": "RETAILER_FAILURE_MAX_TOTAL_PCT",
    "max_unsent_pct": "RETAILER_FAILURE_MAX_UNSENT_PCT",
}
_COUNTS = frozenset({"max_total", "max_per_retailer"})


class PolicyConfigError(ValueError):
    """A threshold is malformed or out of range."""


@dataclass(frozen=True)
class FailurePolicy:
    """Warning-severity thresholds. Exceeding one marks the warning ``significant``; it never blocks
    publication. Defaults are documented in docs/GITHUB_ACTIONS_SETUP.md."""

    max_total: int = 3  # failed requests across all retailers
    max_per_retailer: int = 2  # failed requests of any one retailer
    max_retailer_pct: float = 10.0  # failed / attempted of any one retailer
    max_total_pct: float = 1.0  # failed / attempted over all retailers
    max_unsent_pct: float = 5.0  # planned searches never executed / planned searches

    def __post_init__(self) -> None:
        for name in _COUNTS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PolicyConfigError(f"{ENV_NAMES[name]} must be a whole number >= 0.")
        for name in ("max_retailer_pct", "max_total_pct", "max_unsent_pct"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not 0 <= value <= 100
            ):
                raise PolicyConfigError(f"{ENV_NAMES[name]} must be a number from 0 to 100.")

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> FailurePolicy:
        """Defaults overridden by non-empty ``RETAILER_FAILURE_*`` variables. A set but invalid
        value raises :class:`PolicyConfigError`."""
        environ = os.environ if environ is None else environ
        values = {}
        for name in ENV_NAMES:
            parsed = _parse_setting(name, environ)
            if parsed is not None:
                values[name] = parsed
        return cls(**values)


def _parse_setting(name: str, environ: Mapping[str, str]) -> int | float | None:
    variable = ENV_NAMES[name]
    text = (environ.get(variable) or "").strip()
    if not text:
        return None
    try:
        if name in _COUNTS:
            if not text.isascii() or not text.isdigit():
                raise ValueError(text)
            value: int | float = int(text)
        else:
            value = float(text)
        FailurePolicy(**{name: value})  # range check
    except (ValueError, PolicyConfigError):
        kind = "whole number >= 0" if name in _COUNTS else "number from 0 to 100"
        raise PolicyConfigError(f"{variable} is {text!r}, not a {kind}.") from None
    return value


def load_policy(environ: Mapping[str, str] | None = None) -> tuple[FailurePolicy, list[str]]:
    """``(policy, problems)``: each invalid threshold falls back to its default and is reported.
    The thresholds only grade a warning, so a typo must neither stop the weekly publication nor
    pass unnoticed: the caller shows every problem as a warning."""
    environ = os.environ if environ is None else environ
    values: dict[str, int | float] = {}
    problems: list[str] = []
    for name in ENV_NAMES:
        try:
            parsed = _parse_setting(name, environ)
        except PolicyConfigError as exc:
            problems.append(f"{exc} The default ({getattr(FailurePolicy(), name):g}) is used.")
            continue
        if parsed is not None:
            values[name] = parsed
    return FailurePolicy(**values), problems


def classify_failure(outcome: str, http_status: str = "", message: str = "") -> str:
    """The class of one failed request from its recorded outcome, HTTP status and error text
    (``CODE: text``, or the bare code). A server error is never read as a missing product."""
    if outcome == "UNSUPPORTED_LAYOUT":
        return UNSUPPORTED_LAYOUT
    code = message.split(":", 1)[0].strip()
    status = int(http_status) if http_status.isdigit() else 0
    if not status:
        found = re.search(r"HTTP (\d{3})", message)
        status = int(found.group(1)) if found else 0
    if code in {"RATE_LIMITED", "RETAILER_DEFERRED"} or status == 429:
        return RATE_LIMIT
    if code == "TIMEOUT":
        return TIMEOUT
    if code == "CONNECTION_ERROR":
        return NETWORK_ERROR
    if status >= 500:
        return SERVER_ERROR
    if status in (401, 403):
        return ACCESS_DENIED
    if status in (404, 410):
        return NOT_FOUND
    if 400 <= status < 500:
        return CLIENT_ERROR
    if code in _REFUSED_CODES:
        return REQUEST_REFUSED
    if code == "HTTP_STATUS":
        return HTTP_ERROR
    return OTHER_ERROR


def _exceeds(part: int, whole: int, pct: float) -> bool:
    """``part / whole`` is more than ``pct`` percent, in exact arithmetic."""
    return whole > 0 and Fraction(part * 100, whole) > Fraction(str(pct))


def _pct(part: int, whole: int) -> float | None:
    return round(part * 100 / whole, 2) if whole else None


def _blank_retailer() -> dict[str, int]:
    return {
        "searches_attempted": 0,
        "searches_answered": 0,
        "search_failures": 0,
        "searches_not_sent_retailer_failing": 0,
        "searches_not_executed": 0,
        "search_rows_planned": 0,
        "inspections_attempted": 0,
        "inspection_failures": 0,
    }


@dataclass
class Ledger:
    """Counted facts of one run's request files. ``problems`` are integrity failures."""

    retailers: dict[str, dict[str, int]] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    request_files_not_stored: bool = False

    def retailer(self, retailer_id: str) -> dict[str, int]:
        return self.retailers.setdefault(retailer_id, _blank_retailer())


def _read_csv(path: Path, required: tuple[str, ...], problems: list[str]) -> list[dict[str, str]]:
    if not path.is_file():
        problems.append(f"discovery/{path.name} is missing: request failures cannot be counted.")
        return []
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [name for name in required if name not in (reader.fieldnames or [])]
            if missing:
                problems.append(f"discovery/{path.name} has no column {', '.join(missing)}.")
                return []
            return list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        problems.append(f"discovery/{path.name} is unreadable ({type(exc).__name__}).")
        return []


def read_ledger(run_dir: Path) -> Ledger:
    """Count attempts and failures per retailer from the discovery run's request files."""
    ledger = Ledger()
    discovery = run_dir / "discovery"
    # A bundle saved by the first version of the workflow holds the summary and the collection
    # but no request files. Every fresh run folder also has search_jobs.csv, so this layout is
    # that old bundle and not a run whose files were lost.
    if not any(
        (discovery / name).exists()
        for name in ("search_requests.csv", "product_inspections.csv", "search_jobs.csv")
    ):
        ledger.request_files_not_stored = True
        return ledger
    for row in _read_csv(
        discovery / "search_requests.csv", ("retailer_id", "outcome"), ledger.problems
    ):
        entry = ledger.retailer(row["retailer_id"])
        outcome = row["outcome"]
        if outcome in SEARCH_ATTEMPTED or (
            outcome.startswith("NOT_SENT") and outcome != "NOT_SENT_QUERY_LIMIT"
        ):
            entry["search_rows_planned"] += 1
        if outcome == NOT_SENT_RETAILER_FAILING:
            entry["searches_not_sent_retailer_failing"] += 1
        if outcome in SEARCH_NOT_EXECUTED:
            entry["searches_not_executed"] += 1
        if outcome in SEARCH_ATTEMPTED and not row.get("cache"):
            entry["searches_attempted"] += 1
            entry["searches_answered"] += outcome in SEARCH_ANSWERED
            if outcome in SEARCH_FAILED:
                entry["search_failures"] += 1
                status = row.get("http_status", "")
                ledger.failures.append(
                    {
                        "kind": "search",
                        "retailer_id": row["retailer_id"],
                        "target_id": row.get("target_id", ""),
                        "outcome": outcome,
                        "http_status": status,
                        "class": classify_failure(outcome, status, row.get("message", "")),
                    }
                )
    for row in _read_csv(
        discovery / "product_inspections.csv", ("retailer_id", "outcome"), ledger.problems
    ):
        outcome = row["outcome"]
        if row.get("cache") or outcome in INSPECTION_NOT_ATTEMPTED or not outcome:
            continue
        entry = ledger.retailer(row["retailer_id"])
        entry["inspections_attempted"] += 1
        if outcome == INSPECTION_FAILED:
            entry["inspection_failures"] += 1
            status = row.get("http_status", "")
            ledger.failures.append(
                {
                    "kind": "inspection",
                    "retailer_id": row["retailer_id"],
                    "target_id": row.get("target_id", ""),
                    "outcome": outcome,
                    "http_status": status,
                    "class": classify_failure(outcome, status, row.get("message", "")),
                }
            )
    return ledger


@dataclass
class Assessment:
    """Result of :func:`assess`. ``warnings`` never block. ``blocking`` holds the one finding a
    person may accept (a legacy bundle whose request files were not stored); ``integrity`` holds
    discovery files that cannot be trusted, which nothing overrides."""

    severity: str = SEVERITY_NONE
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    integrity: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def decision(self) -> str:
        if self.blocking or self.integrity:
            return "BLOCKED"
        return "PASS_WITH_WARNINGS" if self.warnings else "PASS"


def _jobs(summary: Mapping) -> dict[tuple[str, str], Mapping] | None:
    rows = summary.get("job_outcomes")
    if not isinstance(rows, list):
        return None
    return {
        (str(row.get("target_id", "")), str(row.get("retailer_id", ""))): row
        for row in rows
        if isinstance(row, Mapping)
    }


def _job_is_visible(job: Mapping, kind: str) -> bool | None:
    """True: the report shows the pair as priced, unverified or incomplete. False: it would read
    as a confirmed absence. None: not determinable. A job whose search coverage records a provider
    failure is displayed as ``Review`` (``result_labels.search_limited``), never ``No match``."""
    terminal, legacy = job.get("terminal_outcome", ""), job.get("discovery_outcome", "")
    if terminal in VISIBLE_TERMINAL or legacy in VISIBLE_LEGACY:
        return True
    if kind == "search" and "PROVIDER_FAILURE" in str(job.get("search_coverage", "")):
        return True
    if terminal in ABSENCE_TERMINAL or legacy == "QUERY_PLAN_EXHAUSTED":
        return False
    return None


def _causes(failures: list[dict[str, str]]) -> str:
    """``2× server error (HTTP 500), 1× timeout``."""
    counts: Counter[tuple[str, str]] = Counter(
        (f["class"], f["http_status"] if f["http_status"].isdigit() else "") for f in failures
    )
    return ", ".join(
        f"{n}× {FAILURE_LABELS[cls]}" + (f" (HTTP {status})" if status else "")
        for (cls, status), n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )


def assess(
    ledger: Ledger,
    summary: Mapping,
    policy: FailurePolicy,
    *,
    plain_language: list[str] | None = None,
) -> Assessment:
    """Describe the run's retailer request failures. Pure and deterministic. Retailer failures only
    produce warnings; only unreadable or contradictory discovery files produce ``integrity``."""
    result = Assessment()
    result.integrity.extend(ledger.problems)
    retailers = ledger.retailers
    failed = len(ledger.failures)
    search_failed = sum(v["search_failures"] for v in retailers.values())
    inspection_failed = sum(v["inspection_failures"] for v in retailers.values())
    search_attempted = sum(v["searches_attempted"] for v in retailers.values())
    inspection_attempted = sum(v["inspections_attempted"] for v in retailers.values())
    attempted = search_attempted + inspection_attempted
    unsent_failing = sum(v["searches_not_sent_retailer_failing"] for v in retailers.values())
    not_executed = sum(v["searches_not_executed"] for v in retailers.values())
    planned = sum(v["search_rows_planned"] for v in retailers.values())
    findings: list[dict] = []
    lines: dict[str, list[str]] = {}  # retailer -> warning fragments
    notes: list[str] = []  # warnings about the whole run
    severities: list[str] = []

    def add(severity: str, key: str, message: str, retailer: str = "") -> None:
        findings.append({"severity": severity, "id": key, "retailer": retailer, "message": message})
        severities.append(severity)
        if retailer:
            lines.setdefault(retailer, []).append(message)
        else:
            notes.append(message)

    if ledger.request_files_not_stored:
        findings.append(
            {
                "severity": SEVERITY_SIGNIFICANT,
                "id": "request_files_not_stored",
                "retailer": "",
                "message": "request files not stored",
            }
        )
        severities.append(SEVERITY_SIGNIFICANT)
        result.blocking.append(
            "This run bundle was saved before request files were kept with it, so its retailer "
            "request failures cannot be counted here; review the collection job's gate result "
            "for this run."
        )
        summary = {}  # Nothing in the rows to compare the summary's counters with.

    # --- Cross-checks of the summary against the rows: inconsistent files are never accepted.
    outcomes = summary.get("search_outcomes") or {}
    for key, count in outcomes.items():
        retailer_id, _, outcome = str(key).partition(":")
        known = ledger.retailers.get(retailer_id, {}).get("search_failures", 0)
        if outcome in SEARCH_FAILED and count > known:
            result.integrity.append(
                f"discovery_summary.json reports {count} {outcome} for {retailer_id} but "
                "search_requests.csv shows fewer: the discovery files disagree."
            )
    jobs = _jobs(summary)
    provider_jobs = (summary.get("discovery_outcomes") or {}).get("PROVIDER_FAILURE", 0)
    stop_reasons = summary.get("search_stop_reasons") or {}
    stopped_jobs = stop_reasons.get("RETAILER_STOPPED_AFTER_ERRORS", 0)
    if stopped_jobs and not unsent_failing:
        result.integrity.append(
            f"discovery_summary.json reports {stopped_jobs} search(es) stopped after errors but "
            "search_requests.csv has no search that was not sent: the discovery files disagree."
        )
    # Every PROVIDER_FAILURE job has at least one failed or never-sent search request behind it.
    if provider_jobs > failed + unsent_failing:
        result.integrity.append(
            f"discovery_summary.json reports {provider_jobs} PROVIDER_FAILURE job(s) but "
            f"search_requests.csv shows only {failed} failed request(s) and {unsent_failing} "
            "search(es) never sent: the discovery files disagree."
        )

    # --- Per-retailer figures.
    per_retailer: dict[str, dict] = {}
    for retailer_id, v in sorted(retailers.items()):
        mine = [f for f in ledger.failures if f["retailer_id"] == retailer_id]
        count = v["search_failures"] + v["inspection_failures"]
        tried = v["searches_attempted"] + v["inspections_attempted"]
        classes = Counter(f["class"] for f in mine)
        per_retailer[retailer_id] = {
            "attempted": tried,
            "failed": count,
            "failure_rate_pct": _pct(count, tried),
            "searches_attempted": v["searches_attempted"],
            "search_failures": v["search_failures"],
            "inspections_attempted": v["inspections_attempted"],
            "inspection_failures": v["inspection_failures"],
            "searches_not_sent_retailer_failing": v["searches_not_sent_retailer_failing"],
            "searches_not_executed": v["searches_not_executed"],
            "failure_classes": dict(sorted(classes.items())),
            "unavailable_for_the_run": bool(v["searches_attempted"] and not v["searches_answered"]),
            "stopped_after_repeated_errors": bool(v["searches_not_sent_retailer_failing"]),
        }

    # --- What happened to each retailer (warnings; severity grades them).
    for retailer_id, v in sorted(retailers.items()):
        p = per_retailer[retailer_id]
        if p["unavailable_for_the_run"]:
            add(
                SEVERITY_SIGNIFICANT,
                "retailer_unavailable",
                f"unavailable (none of its {v['searches_attempted']} search request(s) was "
                "answered)",
                retailer_id,
            )
        if p["stopped_after_repeated_errors"]:
            add(
                SEVERITY_SIGNIFICANT,
                "retailer_stopped",
                "searching stopped after repeated errors "
                f"({v['searches_not_sent_retailer_failing']} planned search(es) never sent)",
                retailer_id,
            )
        if p["failed"] > policy.max_per_retailer:
            add(
                SEVERITY_SIGNIFICANT,
                "retailer_count",
                f"{p['failed']} request(s) failed (limit {policy.max_per_retailer})",
                retailer_id,
            )
        if _exceeds(p["failed"], p["attempted"], policy.max_retailer_pct):
            add(
                SEVERITY_SIGNIFICANT,
                "retailer_rate",
                f"{p['failed']} of {p['attempted']} requests failed "
                f"({p['failure_rate_pct']}%; limit {policy.max_retailer_pct:g}%)",
                retailer_id,
            )
    if failed or unsent_failing:
        if _exceeds(not_executed, planned, policy.max_unsent_pct):
            add(
                SEVERITY_SIGNIFICANT,
                "plan_not_executed",
                f"{not_executed} of {planned} planned searches were never executed "
                f"({_pct(not_executed, planned)}%; limit {policy.max_unsent_pct:g}%).",
            )
    if failed > policy.max_total:
        add(
            SEVERITY_SIGNIFICANT,
            "total_count",
            f"{failed} request(s) failed across all retailers (limit {policy.max_total}).",
        )
    if _exceeds(failed, attempted, policy.max_total_pct):
        add(
            SEVERITY_SIGNIFICANT,
            "total_rate",
            f"{failed} of {attempted} requests failed ({_pct(failed, attempted)}%; limit "
            f"{policy.max_total_pct:g}%).",
        )
    # Say how each retailer failed, once, after its own findings.
    for retailer_id in lines:
        causes = _causes([f for f in ledger.failures if f["retailer_id"] == retailer_id])
        if causes:
            lines[retailer_id].append(f"causes: {causes}")

    # --- Impact of the failures on the jobs they touched (information for the reader).
    affected: list[dict] = []
    if failed:
        affected, impact_findings = _impact(ledger, jobs)
        for severity, key, message in impact_findings:
            add(severity, key, message)

    # --- A failure inside every threshold: one plain line.
    if failed and not any(f["severity"] != SEVERITY_MODERATE for f in findings):
        listed = "; ".join(plain_language or []) or f"{failed} failed request(s)"
        add(
            SEVERITY_MINOR,
            "within_policy",
            f"Retailer request failures within the warning thresholds ({failed} of {attempted} "
            f"requests, {_pct(failed, attempted)}%): {listed}.",
        )

    result.warnings.extend(
        f"{retailer}: " + "; ".join(parts) + "." for retailer, parts in lines.items()
    )
    result.warnings.extend(notes)
    if result.warnings:
        result.warnings.append(
            "Products that could not be verified are shown as Not checked or Review, never as No "
            "match or a zero; competitiveness uses only verified prices."
        )

    order = [SEVERITY_NONE, SEVERITY_MINOR, SEVERITY_MODERATE, SEVERITY_SIGNIFICANT]
    if result.integrity:
        severities.append(SEVERITY_SIGNIFICANT)
    result.severity = max(severities or [SEVERITY_NONE], key=order.index)
    if result.integrity:
        reason = "The discovery request files are missing or inconsistent."
    elif result.blocking:
        reason = " ".join(result.blocking)
    elif not failed and not unsent_failing:
        reason = "No retailer request failed."
    else:
        reason = " ".join(result.warnings[:-1] or result.warnings)
    result.metrics = {
        "severity": result.severity,
        "automated_decision": result.decision,
        "reason": reason,
        "thresholds": policy.as_dict(),
        "requests": {
            "attempted": attempted,
            "failed": failed,
            "failure_rate_pct": _pct(failed, attempted),
            "searches_attempted": search_attempted,
            "search_failures": search_failed,
            "inspections_attempted": inspection_attempted,
            "inspection_failures": inspection_failed,
            "searches_not_sent_retailer_failing": unsent_failing,
            "searches_not_executed": not_executed,
            "searches_planned": planned,
            "failure_classes": dict(sorted(Counter(f["class"] for f in ledger.failures).items())),
        },
        "retailers": per_retailer,
        "findings": findings,
        "integrity_problems": list(result.integrity),
        "affected_jobs": affected,
    }
    return result


def _impact(ledger: Ledger, jobs: Mapping | None):
    """``(affected jobs, findings)``: what the failed requests did to the jobs they touched. A
    finding is ``(severity, id, message)``; none of them blocks."""
    findings: list[tuple[str, str, str]] = []
    affected: list[dict] = []
    if jobs is None:
        findings.append(
            (
                SEVERITY_MODERATE,
                "impact_unknown",
                "The discovery summary has no per-job outcomes, so the effect of "
                f"{len(ledger.failures)} failed request(s) on individual products cannot be read.",
            )
        )
        return affected, findings
    unattributed = [f for f in ledger.failures if not f["target_id"]]
    if unattributed:
        findings.append(
            (
                SEVERITY_MODERATE,
                "impact_unknown",
                f"{len(unattributed)} failed request(s) name no target in the discovery files, "
                "so their effect on individual products cannot be read.",
            )
        )
    silent: set[tuple[str, str]] = set()
    unknown: set[tuple[str, str]] = set()
    per_target: dict[str, list[dict]] = {}
    for failure in ledger.failures:
        if not failure["target_id"]:
            continue
        per_target.setdefault(failure["target_id"], []).append(failure)
        key = (failure["target_id"], failure["retailer_id"])
        job = jobs.get(key)
        visible = _job_is_visible(job, failure["kind"]) if job is not None else None
        affected.append(
            {
                "target_id": failure["target_id"],
                "retailer_id": failure["retailer_id"],
                "request": failure["kind"],
                "failure_class": failure["class"],
                "http_status": failure["http_status"],
                "terminal_outcome": (job or {}).get("terminal_outcome", ""),
                "discovery_outcome": (job or {}).get("discovery_outcome", ""),
                "impact": {True: "visible_in_report", False: "reads_as_absence", None: "unknown"}[
                    visible
                ],
            }
        )
        if visible is False:
            silent.add(key)
        elif visible is None:
            unknown.add(key)
    if silent:
        findings.append(
            (
                SEVERITY_MODERATE,
                "silent_negative",
                f"{len(silent)} target/retailer pair(s) had a failed request but ended as "
                "'searched, nothing found' without a recorded provider failure: check that the "
                "report does not show them as No match.",
            )
        )
    if unknown:
        findings.append(
            (
                SEVERITY_MODERATE,
                "impact_unknown",
                f"{len(unknown)} target/retailer pair(s) with a failed request have no readable "
                "job outcome, so their effect cannot be read.",
            )
        )
    priced = {t for (t, _), job in jobs.items() if job.get("terminal_outcome") in PRICED_OUTCOMES}
    for target_id, failures in sorted(per_target.items()):
        if len(failures) < CONCENTRATION_MIN_FAILURES:
            continue
        if target_id in priced:
            findings.append(
                (
                    SEVERITY_MODERATE,
                    "concentrated_priced",
                    f"{len(failures)} failed requests concentrated on one target that still has "
                    "a retailer price from another page.",
                )
            )
        else:
            findings.append(
                (
                    SEVERITY_MODERATE,
                    "concentrated_unpriced",
                    f"{len(failures)} failed requests concentrated on one target that has no "
                    "retailer price.",
                )
            )
    return affected, findings
