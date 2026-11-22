"""Risk-based policy for retailer request failures in a weekly discovery run.

The readiness gate used to block publication whenever *any* search or page fetch failed. One failed
Octagon product page among about 770 requests (2026-11-22) therefore stopped an otherwise complete
weekly report. This module replaces that all-or-nothing rule with a deterministic, unit-tested
assessment of how much the failures can matter. It reads only the discovery run's own files
(``search_requests.csv``, ``product_inspections.csv``, ``discovery_summary.json``) and never sends a
request.

What counts as one failed request (each failure is counted exactly once)
------------------------------------------------------------------------
* a row of ``search_requests.csv`` whose outcome is ``SEARCH_ERROR`` or ``UNSUPPORTED_LAYOUT`` (a
  search request that was sent and gave no readable answer; its HTTP status, 429 included, is
  an attribute of the same row, not a second failure);
* a row of ``product_inspections.csv`` whose outcome is ``FETCH_FAILED``.

Rows answered from a saved or same-run copy (``cache`` is set) and rows that were never sent
(``NOT_SENT_*``, ``NOT_INSPECTED_BUDGET``) are not attempts and stay out of every denominator. The
summary's ``PROVIDER_FAILURE`` and ``RETAILER_STOPPED_AFTER_ERRORS`` counters are job-level views of
the same search errors: they are cross-checked against the rows, never added to them.

Severity
--------
* **A, minor** (warn, publish): every threshold of :class:`FailurePolicy` holds, no retailer was
  stopped or unavailable, and every failed request is visible in the report as an unresolved
  candidate or an incomplete search rather than as a confirmed absence.
* **B, moderate** (warn or block by impact): failures concentrated on one target, a failed search
  whose job still ended as "no result" (a silent negative), or a job whose impact cannot be read
  from the discovery diagnostics. Impact that cannot be established blocks.
* **C, significant** (block): a threshold is exceeded, a retailer was stopped after repeated errors
  or answered nothing, too much of the plan was never sent, or provider failures exceed what the
  failed requests explain.

Blocks of this policy may be accepted by a person (``accept_retailer_failures``). Missing,
unreadable or mutually inconsistent discovery files are not retailer failures: they are reported as
``integrity`` problems that nothing overrides.
"""

from __future__ import annotations

import csv
import math
import os
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

# Failed requests on one target (all retailers together) that trigger the impact check.
CONCENTRATION_MIN_FAILURES = 2

SEVERITY_NONE, SEVERITY_MINOR, SEVERITY_MODERATE, SEVERITY_SIGNIFICANT = (
    "none",
    "minor",
    "moderate",
    "significant",
)

ENV_NAMES = {
    "max_total": "RETAILER_FAILURE_MAX_TOTAL",
    "max_per_retailer": "RETAILER_FAILURE_MAX_PER_RETAILER",
    "max_retailer_pct": "RETAILER_FAILURE_MAX_RETAILER_PCT",
    "max_total_pct": "RETAILER_FAILURE_MAX_TOTAL_PCT",
    "max_unsent_pct": "RETAILER_FAILURE_MAX_UNSENT_PCT",
}


class PolicyConfigError(ValueError):
    """A threshold is malformed or out of range. The caller must refuse to publish."""


@dataclass(frozen=True)
class FailurePolicy:
    """Thresholds of severity A. Defaults are documented in docs/GITHUB_ACTIONS_SETUP.md."""

    max_total: int = 3  # failed requests across all retailers
    max_per_retailer: int = 2  # failed requests of any one retailer
    max_retailer_pct: float = 10.0  # failed / attempted of any one retailer
    max_total_pct: float = 1.0  # failed / attempted over all retailers
    max_unsent_pct: float = 5.0  # planned searches never executed / planned searches

    def __post_init__(self) -> None:
        for name in ("max_total", "max_per_retailer"):
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
        value raises :class:`PolicyConfigError`; it is never silently replaced by a default."""
        environ = os.environ if environ is None else environ
        values: dict[str, int | float] = {}
        for name, variable in ENV_NAMES.items():
            text = (environ.get(variable) or "").strip()
            if not text:
                continue
            try:
                if name in {"max_total", "max_per_retailer"}:
                    if not text.isascii() or not text.isdigit():
                        raise ValueError(text)
                    values[name] = int(text)
                else:
                    values[name] = float(text)
            except ValueError:
                raise PolicyConfigError(
                    f"{variable} is {text!r}, which is not a valid "
                    f"{'whole number' if name in {'max_total', 'max_per_retailer'} else 'number'}."
                ) from None
        return cls(**values)


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
                ledger.failures.append(
                    {
                        "kind": "search",
                        "retailer_id": row["retailer_id"],
                        "target_id": row.get("target_id", ""),
                        "outcome": outcome,
                        "http_status": row.get("http_status", ""),
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
            ledger.failures.append(
                {
                    "kind": "inspection",
                    "retailer_id": row["retailer_id"],
                    "target_id": row.get("target_id", ""),
                    "outcome": outcome,
                    "http_status": "",
                }
            )
    return ledger


@dataclass
class Assessment:
    """Result of :func:`assess`. ``blocking`` may be accepted by a person; ``integrity`` may not."""

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


def _job_is_visible(job: Mapping) -> bool | None:
    """True: the report shows the pair as priced, unresolved or incomplete. False: it would read
    as a confirmed absence. None: not determinable."""
    terminal, legacy = job.get("terminal_outcome", ""), job.get("discovery_outcome", "")
    if terminal in VISIBLE_TERMINAL or legacy in VISIBLE_LEGACY:
        return True
    if terminal in ABSENCE_TERMINAL or legacy == "QUERY_PLAN_EXHAUSTED":
        return False
    return None


def assess(
    ledger: Ledger,
    summary: Mapping,
    policy: FailurePolicy,
    *,
    plain_language: list[str] | None = None,
) -> Assessment:
    """Classify the run's retailer request failures. Pure and deterministic."""
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
    severities: list[str] = []

    def add(severity: str, key: str, message: str, *, blocks: bool) -> None:
        findings.append({"severity": severity, "id": key, "blocks": blocks, "message": message})
        severities.append(severity)
        (result.blocking if blocks else result.warnings).append(message)

    if ledger.request_files_not_stored:
        add(
            SEVERITY_SIGNIFICANT,
            "request_files_not_stored",
            "This run bundle was saved before request files were kept with it, so its retailer "
            "request failures cannot be counted here; review the collection job's gate result "
            "for this run.",
            blocks=True,
        )
        summary = {}  # Nothing in the rows to compare the summary's counters with.
    # --- Cross-checks of the summary against the rows: inconsistent files are never accepted.
    outcomes = summary.get("search_outcomes") or {}
    for key, count in outcomes.items():
        retailer_id, _, outcome = str(key).partition(":")
        if outcome in SEARCH_FAILED and count > _failed_search_rows(ledger, retailer_id):
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
        failures = v["search_failures"] + v["inspection_failures"]
        tried = v["searches_attempted"] + v["inspections_attempted"]
        per_retailer[retailer_id] = {
            "attempted": tried,
            "failed": failures,
            "failure_rate_pct": _pct(failures, tried),
            "searches_attempted": v["searches_attempted"],
            "search_failures": v["search_failures"],
            "inspections_attempted": v["inspections_attempted"],
            "inspection_failures": v["inspection_failures"],
            "searches_not_sent_retailer_failing": v["searches_not_sent_retailer_failing"],
            "searches_not_executed": v["searches_not_executed"],
        }

    # --- Severity C: significant.
    for retailer_id, v in sorted(retailers.items()):
        if v["searches_not_sent_retailer_failing"]:
            add(
                SEVERITY_SIGNIFICANT,
                "retailer_stopped",
                f"{retailer_id}: searching stopped after repeated errors "
                f"({v['searches_not_sent_retailer_failing']} planned search(es) never sent).",
                blocks=True,
            )
        if v["searches_attempted"] and not v["searches_answered"]:
            add(
                SEVERITY_SIGNIFICANT,
                "retailer_unavailable",
                f"{retailer_id}: none of its {v['searches_attempted']} search request(s) was "
                "answered; the retailer was unavailable.",
                blocks=True,
            )
    if failed or unsent_failing:
        if _exceeds(not_executed, planned, policy.max_unsent_pct):
            add(
                SEVERITY_SIGNIFICANT,
                "plan_not_executed",
                f"{not_executed} of {planned} planned searches were never executed "
                f"({_pct(not_executed, planned)}%; limit {policy.max_unsent_pct:g}%).",
                blocks=True,
            )
    if failed > policy.max_total:
        add(
            SEVERITY_SIGNIFICANT,
            "total_count",
            f"{failed} request(s) failed across all retailers (limit {policy.max_total}).",
            blocks=True,
        )
    if _exceeds(failed, attempted, policy.max_total_pct):
        add(
            SEVERITY_SIGNIFICANT,
            "total_rate",
            f"{failed} of {attempted} requests failed ({_pct(failed, attempted)}%; limit "
            f"{policy.max_total_pct:g}%).",
            blocks=True,
        )
    for retailer_id, p in per_retailer.items():
        if p["failed"] > policy.max_per_retailer:
            add(
                SEVERITY_SIGNIFICANT,
                "retailer_count",
                f"{retailer_id}: {p['failed']} request(s) failed "
                f"(limit {policy.max_per_retailer}).",
                blocks=True,
            )
        if _exceeds(p["failed"], p["attempted"], policy.max_retailer_pct):
            add(
                SEVERITY_SIGNIFICANT,
                "retailer_rate",
                f"{retailer_id}: {p['failed']} of {p['attempted']} requests failed "
                f"({p['failure_rate_pct']}%; limit {policy.max_retailer_pct:g}%).",
                blocks=True,
            )

    # --- Severity B: impact of the failures on the jobs they touched.
    affected: list[dict] = []
    if failed:
        affected, impact_findings = _impact(ledger, jobs)
        for severity, key, message, blocks in impact_findings:
            add(severity, key, message, blocks=blocks)

    # --- Severity A, or none.
    if failed and not result.integrity and not any(f["blocks"] for f in findings):
        listed = "; ".join(plain_language or []) or f"{failed} failed request(s)"
        add(
            SEVERITY_MINOR,
            "within_policy",
            f"Retailer request failures within policy ({failed} of {attempted} requests, "
            f"{_pct(failed, attempted)}%): {listed}. Affected searches or pages are shown in the "
            "report as unresolved candidates or incomplete searches, not as confirmed absences.",
            blocks=False,
        )
    order = [SEVERITY_NONE, SEVERITY_MINOR, SEVERITY_MODERATE, SEVERITY_SIGNIFICANT]
    if result.integrity:
        severities.append(SEVERITY_SIGNIFICANT)
    result.severity = max(severities or [SEVERITY_NONE], key=order.index)
    if result.integrity:
        reason = "The discovery request files are missing or inconsistent."
    elif result.blocking:
        reason = " ".join(f["message"] for f in findings if f["blocks"])
    elif not failed:
        reason = "No retailer request failed."
    else:
        reason = next(f["message"] for f in findings if f["id"] == "within_policy")
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
        },
        "retailers": per_retailer,
        "findings": findings,
        "integrity_problems": list(result.integrity),
        "affected_jobs": affected,
    }
    return result


def _failed_search_rows(ledger: Ledger, retailer_id: str) -> int:
    return ledger.retailers.get(retailer_id, {}).get("search_failures", 0)


def _impact(ledger: Ledger, jobs: Mapping | None):
    """``(affected jobs, findings)``: what the failed requests did to the jobs they touched."""
    findings: list[tuple[str, str, str, bool]] = []
    affected: list[dict] = []
    if jobs is None:
        findings.append(
            (
                SEVERITY_MODERATE,
                "impact_unknown",
                "The discovery summary has no per-job outcomes, so the effect of "
                f"{len(ledger.failures)} failed request(s) on coverage cannot be established; "
                "manual review required.",
                True,
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
                "so their effect on coverage cannot be established; manual review required.",
                True,
            )
        )
    silent: list[tuple[str, str]] = []
    unknown: list[tuple[str, str]] = []
    per_target: dict[str, list[dict]] = {}
    for failure in ledger.failures:
        if not failure["target_id"]:
            continue
        per_target.setdefault(failure["target_id"], []).append(failure)
        key = (failure["target_id"], failure["retailer_id"])
        job = jobs.get(key)
        visible = _job_is_visible(job) if job is not None else None
        affected.append(
            {
                "target_id": failure["target_id"],
                "retailer_id": failure["retailer_id"],
                "request": failure["kind"],
                "http_status": failure["http_status"],
                "terminal_outcome": (job or {}).get("terminal_outcome", ""),
                "discovery_outcome": (job or {}).get("discovery_outcome", ""),
                "impact": {True: "visible_in_report", False: "reads_as_absence", None: "unknown"}[
                    visible
                ],
            }
        )
        if visible is False:
            silent.append(key)
        elif visible is None:
            unknown.append(key)
    if silent:
        findings.append(
            (
                SEVERITY_MODERATE,
                "silent_negative",
                f"{len(set(silent))} target/retailer pair(s) had a failed request but ended as "
                "'searched, nothing found': the failure would read as a confirmed absence; "
                "manual review required.",
                True,
            )
        )
    if unknown:
        findings.append(
            (
                SEVERITY_MODERATE,
                "impact_unknown",
                f"{len(set(unknown))} target/retailer pair(s) with a failed request have no "
                "readable job outcome, so the impact cannot be established; manual review "
                "required.",
                True,
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
                    f"{len(failures)} failed requests concentrated on one target "
                    "that still has a retailer price from another page.",
                    False,
                )
            )
        else:
            findings.append(
                (
                    SEVERITY_MODERATE,
                    "concentrated_unpriced",
                    f"{len(failures)} failed requests concentrated on one target that has no "
                    "retailer price: the failures may be why; manual review required.",
                    True,
                )
            )
    return affected, findings
