"""Before/after report over two runs' own artifacts (2026-11-04).

Compares an earlier run (*before*) with a later one (*after*) using only files the pipeline already
wrote; nothing is recomputed from Sheets or inferred. A figure a run never recorded is reported as
``NOT_RECORDED``, never as zero. Sections:

- selection universe: eligible, qualifying and selected variations per reporting month and their
  union, and the exclusions by reason;
- selection: selected variations, distinct contexts, shortfall, own prices dated by the M-1 record;
- matching coverage and alternative coverage, kept apart (one observation can fill several rows);
- discovery: terminal outcomes, the owner-named recovery targets and the requests avoided by reuse.

Pure functions over parsed files; ``load_run`` is the only file access (read only).
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .discovery_outcomes import (
    CLASS_EXCLUDED,
    CLASS_FETCH,
    CLASS_SUITABLE,
    CLASS_UNRESOLVED,
    OUTCOME_CLASS,
)

REPORT_VERSION = "before-after-1"
NOT_RECORDED = "NOT_RECORDED"

# Owner-named discovery misses (2026-11-03 audit): target -> retailer id and the owner's label.
RECOVERY_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("TGT-000293", "pcc", "PC Central ASUS X1504VA-BQ4301WSM, Core 3 100U, 8GB/512GB"),
    ("TGT-000095", "pcc", "PC Central Acer AG14-71M-58JD, Core Ultra 5 115U, 16GB/512GB"),
    ("TGT-000003", "pcx", "PC Express MacBook Neo A18 Pro 8GB/256GB"),
    ("TGT-000002", "pcx", "PC Express MacBook Neo A18 Pro 8GB/512GB"),
    ("TGT-000001", "datablitz", "DataBlitz MacBook Air M5 16GB/512GB"),
    ("TGT-000006", "datablitz", "DataBlitz MacBook Air M5 16GB/1TB"),
)

# Status of a recovery target in a run (nearest to a price first).
RECOVERED_OFFER = "RECOVERED_OFFER_FOUND"
IDENTITY_CONFIRMED_EXCLUDED = "IDENTITY_CONFIRMED_OFFER_EXCLUDED"
PAGE_UNRESOLVED = "PAGE_FETCHED_IDENTITY_UNRESOLVED"
FETCH_NOT_COMPLETED = "FETCH_DEFERRED_OR_FAILED"
STILL_MISSED = "NOT_RECOVERED"
JOB_NOT_RUN = "NO_JOB_RECORDED"

SELECTION_UNIVERSE_KEYS = (
    "eligible_in_m_minus_1",
    "eligible_in_mtd",
    "eligible_in_both_months",
    "eligible_m_minus_1_only",
    "eligible_mtd_only",
    "eligible_union",
    "eligible_variations_with_verified_mapping",
    "qualifying_in_m_minus_1",
    "qualifying_in_mtd",
    "qualifying_m_minus_1_only",
    "qualifying_mtd_only",
    "qualifying_union",
    "excluded_from_top_consideration",
    "qualified_not_selected",
)
SELECTION_KEYS = (
    "requested_top_n",
    "selected_variations",
    "shortfall",
    "distinct_configuration_contexts",
    "selected_in_m_minus_1",
    "selected_in_mtd",
    "selected_in_both_months",
    "selected_m_minus_1_only",
    "selected_mtd_only",
    "selected_with_m_minus_1_price",
    "selected_with_own_price",
    "selected_without_own_price",
    "selected_with_lower_eligible_rank_than_top_n",
    "contexts_with_several_variations",
    "variations_in_shared_contexts",
)
MATCHING_KEYS = (
    "matching_priced_variation_rows",
    "contexts_with_matching_price",
    "unique_matching_observations",
    "row_associations_matching",
)
ALTERNATIVE_KEYS = (
    "alternative_priced_variation_rows",
    "contexts_with_alternative_only",
    "unique_alternative_observations_displayed",
    "row_associations_alternative_displayed",
    "alt_1_offers",
    "alt_2_offers",
    "unique_alternative_offers_eligible",
)
REUSE_KEYS = (
    "searches_sent",
    "searches_reused_same_run",
    "searches_reused_saved_cache",
    "product_inspections_sent",
    "product_inspections_reused_same_run",
    "product_inspections_reused_saved_cache",
    "collection_requests_deduplicated",
    "collection_pages_reused_from_inspection",
    "requests_avoided_total",
)


@dataclass(frozen=True)
class RunArtifacts:
    """The parsed artifacts of one run; an absent file is ``None`` and listed in ``missing``."""

    label: str
    ranking_summary: Mapping[str, object] | None = None
    coverage_counts: Mapping[str, object] | None = None
    discovery_summary: Mapping[str, object] | None = None
    search_jobs: Sequence[Mapping[str, str]] | None = None
    missing: tuple[str, ...] = field(default_factory=tuple)


def _json(path: Path) -> Mapping[str, object] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def load_run(
    label: str,
    run_dir: Path,
    *,
    variation_dir: Path | None = None,
    reports_dir: Path | None = None,
    discovery_dir: Path | None = None,
) -> RunArtifacts:
    """Read the artifacts of a weekly run folder (default sub-folders of ``run_dir``)."""
    variation_dir = variation_dir or run_dir / "shopee_variations"
    reports_dir = reports_dir or run_dir / "variation_reports"
    discovery_dir = discovery_dir or run_dir / "discovery"
    summary = _json(variation_dir / "shopee_variation_run_summary.json")
    coverage = _json(reports_dir / "variation_coverage.json")
    discovery = _json(discovery_dir / "discovery_summary.json")
    jobs_path = discovery_dir / "search_jobs.csv"
    jobs = None
    if jobs_path.is_file():
        with jobs_path.open(newline="", encoding="utf-8") as handle:
            jobs = list(csv.DictReader(handle))
    named = {
        "shopee_variation_run_summary.json": summary,
        "variation_coverage.json": coverage,
        "discovery_summary.json": discovery,
        "search_jobs.csv": jobs,
    }
    counts = coverage.get("counts") if coverage else None
    return RunArtifacts(
        label,
        summary,
        counts if isinstance(counts, Mapping) else None,
        discovery,
        jobs,
        tuple(name for name, value in named.items() if value is None),
    )


def _value(source: Mapping[str, object] | None, key: str) -> object:
    if source is None or key not in source:
        return NOT_RECORDED
    return source[key]


def _delta(before: object, after: object) -> object:
    numeric = (int, float)
    if (
        isinstance(before, numeric)
        and isinstance(after, numeric)
        and not isinstance(before, bool)
        and not isinstance(after, bool)
    ):
        return after - before
    return NOT_RECORDED


def _metrics(
    keys: Sequence[str], before: Mapping[str, object] | None, after: Mapping[str, object] | None
) -> list[dict[str, object]]:
    rows = []
    for key in keys:
        b, a = _value(before, key), _value(after, key)
        rows.append({"metric": key, "before": b, "after": a, "change": _delta(b, a)})
    return rows


def _by_reason(
    before: Mapping[str, object] | None, after: Mapping[str, object] | None
) -> list[dict[str, object]]:
    """Exclusions by reason; a reason absent from a *recorded* mapping is 0, else NOT_RECORDED."""
    key = "excluded_by_mapping_failure"
    maps = [
        s[key] if s is not None and isinstance(s.get(key), Mapping) else None
        for s in (before, after)
    ]
    reasons = sorted({r for m in maps if m for r in m})
    rows = []
    for reason in reasons:
        b = maps[0].get(reason, 0) if maps[0] is not None else NOT_RECORDED
        a = maps[1].get(reason, 0) if maps[1] is not None else NOT_RECORDED
        rows.append({"metric": reason, "before": b, "after": a, "change": _delta(b, a)})
    return rows


def job_class(row: Mapping[str, str]) -> tuple[str, str]:
    """``(terminal outcome, outcome class)`` of a ``search_jobs.csv`` row. Runs before the
    2026-11-03 vocabulary have only ``discovery_outcome``; its class is then unknown."""
    outcome = row.get("terminal_outcome") or row.get("discovery_outcome") or NOT_RECORDED
    klass = row.get("outcome_class") or OUTCOME_CLASS.get(outcome, NOT_RECORDED)
    return outcome, klass


def terminal_counts(jobs: Sequence[Mapping[str, str]] | None) -> dict[str, int] | None:
    if jobs is None:
        return None
    return dict(sorted(Counter(job_class(r)[0] for r in jobs).items()))


def _terminal_table(before: RunArtifacts, after: RunArtifacts) -> list[dict[str, object]]:
    b, a = terminal_counts(before.search_jobs), terminal_counts(after.search_jobs)
    names = sorted(set(b or {}) | set(a or {}))
    rows = []
    for name in names:
        bv = (b or {}).get(name, 0) if b is not None else NOT_RECORDED
        av = (a or {}).get(name, 0) if a is not None else NOT_RECORDED
        rows.append({"metric": name, "before": bv, "after": av, "change": _delta(bv, av)})
    return rows


def recovery_status(klass: str, outcome: str) -> str:
    if outcome == JOB_NOT_RUN:
        return JOB_NOT_RUN
    if klass == CLASS_SUITABLE:
        return RECOVERED_OFFER
    if klass == CLASS_EXCLUDED:
        return IDENTITY_CONFIRMED_EXCLUDED
    if klass == CLASS_UNRESOLVED:
        return PAGE_UNRESOLVED
    if klass == CLASS_FETCH:
        return FETCH_NOT_COMPLETED
    return STILL_MISSED


def _recovery_side(run: RunArtifacts, target: str, retailer: str) -> dict[str, str]:
    if run.search_jobs is None:
        return {"terminal_outcome": NOT_RECORDED, "outcome_class": NOT_RECORDED,
                "status": NOT_RECORDED}
    row = next(
        (r for r in run.search_jobs if r.get("target_id") == target
         and r.get("retailer_id") == retailer),
        None,
    )
    if row is None:
        return {"terminal_outcome": JOB_NOT_RUN, "outcome_class": JOB_NOT_RUN,
                "status": JOB_NOT_RUN}
    outcome, klass = job_class(row)
    status = recovery_status(klass, outcome) if klass != NOT_RECORDED else STILL_MISSED
    return {"terminal_outcome": outcome, "outcome_class": klass, "status": status}


def recovery_table(before: RunArtifacts, after: RunArtifacts) -> dict[str, object]:
    rows = []
    for target, retailer, label in RECOVERY_TARGETS:
        b, a = _recovery_side(before, target, retailer), _recovery_side(after, target, retailer)
        rows.append({"target_id": target, "retailer_id": retailer, "label": label,
                     "before": b, "after": a})
    tally = Counter(r["after"]["status"] for r in rows)
    return {
        "rows": rows,
        "after_status_counts": dict(sorted(tally.items())),
        "recovered_with_offer": tally.get(RECOVERED_OFFER, 0),
        "identity_confirmed_but_excluded": tally.get(IDENTITY_CONFIRMED_EXCLUDED, 0),
        "unresolved_pages": tally.get(PAGE_UNRESOLVED, 0),
        "fetch_deferred_or_failed": tally.get(FETCH_NOT_COMPLETED, 0),
        "not_recovered": tally.get(STILL_MISSED, 0),
        "no_job_recorded": tally.get(JOB_NOT_RUN, 0),
    }


def unresolved_pages(run: RunArtifacts) -> object:
    """Jobs of the whole run that ended with a fetched page whose identity did not resolve."""
    if run.search_jobs is None:
        return NOT_RECORDED
    if not any(r.get("outcome_class") or r.get("terminal_outcome") for r in run.search_jobs):
        return NOT_RECORDED  # a run before the 2026-11-03 terminal vocabulary
    return sum(1 for r in run.search_jobs if job_class(r)[1] == CLASS_UNRESOLVED)


def build_report(before: RunArtifacts, after: RunArtifacts) -> dict[str, object]:
    reuse_b = before.discovery_summary.get("request_reuse") if before.discovery_summary else None
    reuse_a = after.discovery_summary.get("request_reuse") if after.discovery_summary else None
    reuse_b = reuse_b if isinstance(reuse_b, Mapping) else None
    reuse_a = reuse_a if isinstance(reuse_a, Mapping) else None
    ub, ua = unresolved_pages(before), unresolved_pages(after)
    return {
        "report_version": REPORT_VERSION,
        "before_label": before.label,
        "after_label": after.label,
        "missing_artifacts": {"before": list(before.missing), "after": list(after.missing)},
        "selection_policy": {
            "before": _value(before.ranking_summary, "selection_policy"),
            "after": _value(after.ranking_summary, "selection_policy"),
        },
        "selection_universe": _metrics(
            SELECTION_UNIVERSE_KEYS, before.ranking_summary, after.ranking_summary
        ),
        "exclusions_by_reason": _by_reason(before.ranking_summary, after.ranking_summary),
        "selection": _metrics(SELECTION_KEYS, before.ranking_summary, after.ranking_summary),
        "matching_coverage": _metrics(MATCHING_KEYS, before.coverage_counts, after.coverage_counts),
        "alternative_coverage": _metrics(
            ALTERNATIVE_KEYS, before.coverage_counts, after.coverage_counts
        ),
        "discovery_terminal_outcomes": _terminal_table(before, after),
        "discovery_recovery": recovery_table(before, after),
        "unresolved_pages": {"before": ub, "after": ua, "change": _delta(ub, ua)},
        "requests_avoided_by_reuse": _metrics(REUSE_KEYS, reuse_b, reuse_a),
    }


def _cell(value: object) -> str:
    return str(value)


def _table(rows: Sequence[Mapping[str, object]]) -> list[str]:
    if not rows:
        return ["_none recorded_", ""]
    lines = ["| Metric | Before | After | Change |", "|---|---:|---:|---:|"]
    lines += [
        f"| {r['metric']} | {_cell(r['before'])} | {_cell(r['after'])} | {_cell(r['change'])} |"
        for r in rows
    ]
    return [*lines, ""]


def render_markdown(report: Mapping[str, object]) -> str:
    lines = [
        f"# Before / after: {report['before_label']} -> {report['after_label']}",
        "",
        "Figures come from each run's own artifacts. `NOT_RECORDED` means the run never wrote the "
        "figure; it is not zero.",
        "",
    ]
    missing = report["missing_artifacts"]
    for side in ("before", "after"):
        if missing[side]:  # type: ignore[index]
            lines.append(f"- {side} run is missing: {', '.join(missing[side])}")  # type: ignore[index]
    policy = report["selection_policy"]
    lines += [
        f"- selection policy: before `{policy['before']}`, after `{policy['after']}`",  # type: ignore[index]
        "",
    ]
    sections = (
        ("Selection universe (eligible and qualifying variations by month and union)",
         "selection_universe"),
        ("Exclusions by reason", "exclusions_by_reason"),
        ("Selected variations and contexts", "selection"),
        ("Matching coverage", "matching_coverage"),
        ("Alternative coverage (separate from matching)", "alternative_coverage"),
        ("Discovery terminal outcomes (jobs)", "discovery_terminal_outcomes"),
        ("Requests avoided by reuse", "requests_avoided_by_reuse"),
    )
    for title, key in sections:
        lines += [f"## {title}", "", *_table(report[key])]  # type: ignore[arg-type]
    pages = report["unresolved_pages"]
    lines += [
        "## Unresolved pages",
        "",
        *_table([{"metric": "jobs ending PAGE_FETCHED_IDENTITY_UNRESOLVED", **pages}]),  # type: ignore[dict-item]
    ]
    recovery = report["discovery_recovery"]
    lines += [
        "## Owner-named discovery misses",
        "",
        "| Target | Retailer | Before | After | Status after |",
        "|---|---|---|---|---|",
    ]
    for r in recovery["rows"]:  # type: ignore[index]
        lines.append(
            f"| {r['target_id']} {r['label']} | {r['retailer_id']} | "
            f"{r['before']['terminal_outcome']} | {r['after']['terminal_outcome']} | "
            f"{r['after']['status']} |"
        )
    tally = recovery["after_status_counts"]  # type: ignore[index]
    lines += ["", "After: " + (", ".join(f"{k} {v}" for k, v in tally.items()) or "no jobs"), ""]
    return "\n".join(lines)
