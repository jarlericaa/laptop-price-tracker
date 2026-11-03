"""Complete P1 audit diagnostics, prepared locally for a later publication (2026-11-03).

The live P1 audit tabs hold headers only. This module turns the run's local files into the three
diagnostics the owner needs, each a plain CSV with a header and one row per audited item, plus a
publication *plan* (preview only: nothing here opens, reads or writes a Sheet):

- eligibility: every eligible variation of the union pool with month presence, month statuses, GMV
  per month, price basis and date and its selection status; and the Phase 1 listings that are not
  eligible (review pending, excluded) with their reasons;
- mapping exclusions: every eligible variation that is not selected, with its exact reason and the
  evidence behind it (the ranking's own detail, the variation's stated specifications, and what
  ``MATCH_REVIEW`` holds beside it, which never selects or excludes anything);
- search coverage: one row per target x retailer of a discovery run with its terminal outcome, the
  counts of queries, pages, results, rejected and collected candidates and inspections, the caps
  that were reached and the requests that reuse avoided.

Outcome columns are read dynamically: any column of ``search_jobs.csv`` whose name says outcome,
stop reason, coverage or terminal (and whose values are short categories) is reported and grouped
by whatever values it holds, so an older or a newer outcome vocabulary needs no change here. Extra
columns are carried through unchanged. Pure functions over CSV rows; the caller owns file access.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from .selection_audit import review_evidence

DIAGNOSTICS_VERSION = "p1-publication-diagnostics-1"
SELECTED = "SELECTED"
QUALIFIED_NOT_SELECTED = "QUALIFIED_NOT_SELECTED"
QUALIFIED_BELOW_LIMIT = "QUALIFIED_BELOW_SELECTION_LIMIT"
NOT_IN_RANKING = "NOT_IN_VARIATION_RANKING"
REVIEW_PENDING = "REVIEW_PENDING"
EXCLUDED = "EXCLUDED"

ELIGIBILITY_COLUMNS: tuple[str, ...] = (
    "p1_rank",
    "source_key",
    "shop_id",
    "shop_name",
    "item_id",
    "model_id",
    "item_name",
    "shopee_model_name",
    "present_in_m_minus_1",
    "present_in_mtd",
    "m_minus_1_status",
    "mtd_status",
    "m_minus_1_gmv_usd",
    "mtd_gmv_usd",
    "ranking_gmv_usd",
    "price_status",
    "shopee_current_price_php",
    "price_basis",
    "price_period_month",
    "price_record_date",
    "eligibility_status",
    "eligibility_reason",
    "matched_include_rule",
    "matched_exclude_rule",
    "selection_status",
    "exclusion_reason",
    "target_id",
    "mapping_status",
    "variation_eligible_rank",
)

NOT_ELIGIBLE_COLUMNS: tuple[str, ...] = (
    "disposition",
    "reason",
    "model_id",
    "shop_id",
    "shop_name",
    "item_id",
    "item_name",
    "shopee_model_name",
    "m_minus_1_gmv_usd",
    "mtd_gmv_usd",
    "ranking_gmv_usd",
    "shopee_current_price_php",
    "eligibility_status",
    "eligibility_reason",
    "matched_keyword",
    "matched_include_rule",
    "matched_exclude_rule",
    "present_in_m_minus_1",
    "present_in_mtd",
    "m_minus_1_status",
    "mtd_status",
    "price_basis",
    "price_record_date",
    "review_decision",
    "review_notes",
)

MAPPING_EXCLUSION_COLUMNS: tuple[str, ...] = (
    "eligible_rank",
    "source_key",
    "shop_name",
    "item_name",
    "shopee_model_name",
    "ranking_gmv_usd",
    "present_in_m_minus_1",
    "present_in_mtd",
    "m_minus_1_status",
    "mtd_status",
    "selection_status",
    "exclusion_reason",
    "target_id",
    "mapping_status",
    "evidence",
    "variation_spec_evidence",
    "variation_spec_conflicts",
    "match_review_status",
    "match_review_variation_state",
    "match_review_unresolved_note",
    "audit_representative",
    "correction_queue_id",
    "suggested_action",
)

SEARCH_COVERAGE_COLUMNS: tuple[str, ...] = (
    "target_id",
    "rank",
    "retailer_id",
    "terminal_outcome",
    "terminal_outcome_column",
    "outcome_columns",
    "queries_planned",
    "queries_run",
    "tiers_run",
    "pages_sent",
    "pages_from_cache",
    "pages_not_sent",
    "search_results",
    "candidates",
    "plausible_candidates",
    "candidates_rejected",
    "candidates_not_selected",
    "candidates_collected",
    "candidates_other_decision",
    "inspections",
    "inspection_outcomes",
    "inspections_reused_from_cache",
    "search_requests_reused_saved",
    "search_requests_reused_same_run",
    "requests_avoided_by_reuse",
    "cap_or_budget_reached",
    "caps_reached",
)

_OUTCOME_NAME = re.compile(r"outcome|stop_reason|coverage|terminal", re.I)
_TERMINAL_PREFERENCE = (
    "terminal_outcome",
    "final_outcome",
    "discovery_terminal_outcome",
    "discovery_outcome",
)
_MAX_CATEGORY_LENGTH = 120
_MAX_CATEGORIES = 60


def _t(row: Mapping[str, object] | None, name: str) -> str:
    value = (row or {}).get(name)
    return "" if value is None else str(value).strip()


def _number(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# Eligibility -------------------------------------------------------------------------------


def shop_name_lookup(rows: Iterable[Mapping[str, str]]) -> dict[str, str]:
    """``shop_id -> shop_name`` from ``source_shop_names.csv`` (first name seen wins)."""
    names: dict[str, str] = {}
    for row in rows:
        names.setdefault(_t(row, "shop_id"), _t(row, "shop_name"))
    return names


def build_eligibility_variations(
    eligible: Sequence[Mapping[str, str]],
    variation_ranking: Sequence[Mapping[str, str]],
    shop_names: Mapping[str, str],
) -> list[dict[str, str]]:
    """Every eligible variation of the union pool with its months, price basis and selection."""
    ranking = {_t(row, "source_key"): row for row in variation_ranking}
    rows: list[dict[str, str]] = []
    for source in eligible:
        key = _t(source, "source_key")
        ranked = ranking.get(key)
        row = {name: _t(source, name) for name in ELIGIBILITY_COLUMNS if name in source}
        row |= {
            "p1_rank": _t(source, "rank"),
            "shop_name": _t(ranked, "shop_name") or shop_names.get(_t(source, "shop_id"), ""),
            "selection_status": _t(ranked, "selection_status") if ranked else NOT_IN_RANKING,
            "exclusion_reason": _t(ranked, "exclusion_reason"),
            "target_id": _t(ranked, "target_id"),
            "mapping_status": _t(ranked, "mapping_status"),
            "variation_eligible_rank": _t(ranked, "eligible_rank"),
        }
        rows.append({name: row.get(name, "") for name in ELIGIBILITY_COLUMNS})
    return rows


def build_not_eligible_listings(
    review: Sequence[Mapping[str, str]],
    excluded: Sequence[Mapping[str, str]],
    shop_names: Mapping[str, str],
) -> list[dict[str, str]]:
    """Phase 1 review-pending and excluded listings with the reason each was not eligible."""
    rows: list[dict[str, str]] = []
    for disposition, source_rows in ((REVIEW_PENDING, review), (EXCLUDED, excluded)):
        for source in source_rows:
            row = {name: _t(source, name) for name in NOT_ELIGIBLE_COLUMNS}
            rule = _t(source, "matched_exclude_rule") or _t(source, "matched_keyword")
            row["disposition"] = disposition
            row["shop_name"] = shop_names.get(_t(source, "shop_id"), "")
            row["reason"] = _t(source, "eligibility_reason") + (f" [{rule}]" if rule else "")
            rows.append(row)
    return rows


# Mapping exclusions --------------------------------------------------------------------


def build_mapping_exclusions(
    variation_ranking: Sequence[Mapping[str, str]],
    exclusions: Sequence[Mapping[str, str]],
    review_rows: Sequence[Mapping[str, str]] = (),
    audit: Mapping[str, object] | None = None,
    correction_queue: Sequence[Mapping[str, str]] = (),
) -> list[dict[str, str]]:
    """Eligible variations that are not selected, each with its exact reason and evidence."""
    reviews = {_t(row, "source_model_id"): row for row in review_rows}
    action = {_t(row, "comparison_row_id"): _t(row, "suggested_action") for row in exclusions}
    representative = {
        _t(item, "source_key")
        for items in (audit or {}).get("representative_exclusions", {}).values()  # type: ignore[union-attr]
        for item in items
    }
    queued: dict[str, str] = {}
    for row in correction_queue:
        if _t(row, "scope") == "VARIATION_BINDING":
            queued[_t(row, "source_model_id")] = _t(row, "queue_id")
    rows: list[dict[str, str]] = []
    for ranked in variation_ranking:
        status = _t(ranked, "selection_status")
        if status == SELECTED:
            continue
        reason = _t(ranked, "exclusion_reason") or (
            QUALIFIED_BELOW_LIMIT if status == QUALIFIED_NOT_SELECTED else status
        )
        model_id = _t(ranked, "source_model_id")
        review = (
            review_evidence(model_id, _t(ranked, "shopee_model_name"), reviews)
            if reviews
            else {"review_row": "NONE"}
        )
        found = review.get("review_row") == "FOUND"
        rows.append(
            {
                "eligible_rank": _t(ranked, "eligible_rank"),
                "source_key": _t(ranked, "source_key"),
                "shop_name": _t(ranked, "shop_name"),
                "item_name": _t(ranked, "item_name"),
                "shopee_model_name": _t(ranked, "shopee_model_name"),
                "ranking_gmv_usd": _t(ranked, "ranking_gmv_usd"),
                "present_in_m_minus_1": _t(ranked, "present_in_m_minus_1"),
                "present_in_mtd": _t(ranked, "present_in_mtd"),
                "m_minus_1_status": _t(ranked, "m_minus_1_status"),
                "mtd_status": _t(ranked, "mtd_status"),
                "selection_status": status,
                "exclusion_reason": reason,
                "target_id": _t(ranked, "target_id"),
                "mapping_status": _t(ranked, "mapping_status"),
                "evidence": _t(ranked, "comparison_detail"),
                "variation_spec_evidence": _t(ranked, "variation_spec_evidence"),
                "variation_spec_conflicts": _t(ranked, "variation_spec_conflicts"),
                "match_review_status": review.get("status", "") if found else "NO_REVIEW_ROW",
                "match_review_variation_state": review.get("variation_state", "") if found else "",
                "match_review_unresolved_note": review.get("unresolved_note", "") if found else "",
                "audit_representative": "TRUE"
                if _t(ranked, "source_key") in representative
                else "FALSE",
                "correction_queue_id": queued.get(model_id, ""),
                "suggested_action": action.get(_t(ranked, "comparison_row_id"), ""),
            }
        )
    return rows


# Search coverage ---------------------------------------------------------------------------


def outcome_columns(fieldnames: Sequence[str], rows: Sequence[Mapping[str, str]]) -> list[str]:
    """Columns that carry an outcome-like category, read from the data (no fixed vocabulary)."""
    found = []
    for name in fieldnames:
        if not _OUTCOME_NAME.search(name):
            continue
        values = {_t(row, name) for row in rows} - {""}
        if (
            values
            and len(values) <= _MAX_CATEGORIES
            and all(len(v) <= _MAX_CATEGORY_LENGTH for v in values)
        ):
            found.append(name)
    return found


def terminal_outcome_column(columns: Sequence[str]) -> str:
    for name in _TERMINAL_PREFERENCE:
        if name in columns:
            return name
    for name in columns:
        if "outcome" in name.casefold() and "candidate" not in name.casefold():
            return name
    return "stop_reason" if "stop_reason" in columns else ""


def _group(rows: Iterable[Mapping[str, str]]) -> dict[tuple[str, str], list[Mapping[str, str]]]:
    grouped: dict[tuple[str, str], list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(_t(row, "target_id"), _t(row, "retailer_id"))].append(row)
    return grouped


def build_search_coverage(
    jobs: Sequence[Mapping[str, str]],
    results: Sequence[Mapping[str, str]] = (),
    candidates: Sequence[Mapping[str, str]] = (),
    inspections: Sequence[Mapping[str, str]] = (),
    requests: Sequence[Mapping[str, str]] = (),
) -> tuple[list[dict[str, str]], list[str]]:
    """One row per target x retailer; returns the rows and the column order (extras last)."""
    fieldnames = list(dict.fromkeys(name for job in jobs for name in job))
    outcomes = outcome_columns(fieldnames, jobs)
    terminal_column = terminal_outcome_column(outcomes)
    by_results, by_candidates = _group(results), _group(candidates)
    by_inspections, by_requests = _group(inspections), _group(requests)
    consumed = {
        "target_id", "rank", "retailer_id", "queries_planned", "queries_run", "tiers_run",
        "pages_sent", "pages_from_cache", "pages_not_sent",
    }  # fmt: skip
    extras = [
        name for name in fieldnames if name not in consumed and name not in SEARCH_COVERAGE_COLUMNS
    ]
    rows: list[dict[str, str]] = []
    for job in jobs:
        key = (_t(job, "target_id"), _t(job, "retailer_id"))
        decided = Counter(_t(c, "decision") for c in by_candidates.get(key, []))
        rejected = sum(n for d, n in decided.items() if d.startswith("REJECTED"))
        not_selected = sum(n for d, n in decided.items() if d.startswith("NOT_SELECTED"))
        collected = sum(n for d, n in decided.items() if d.startswith("COLLECT"))
        total = sum(decided.values())
        inspected = by_inspections.get(key, [])
        reused = sum(1 for i in inspected if _t(i, "cache").startswith(("SAVED", "SAME_RUN")))
        sent = by_requests.get(key, [])
        saved = sum(1 for r in sent if _t(r, "cache").startswith("SAVED"))
        same_run = sum(1 for r in sent if _t(r, "cache").startswith("SAME_RUN"))
        recorded = "pages_from_cache" in job or bool(sent) or "cache" in (inspected[:1] or [{}])[0]
        caps = _caps(job, decided)
        row = {
            "target_id": key[0],
            "rank": _t(job, "rank"),
            "retailer_id": key[1],
            "terminal_outcome": _t(job, terminal_column) or "UNRECORDED",
            "terminal_outcome_column": terminal_column,
            "outcome_columns": "; ".join(f"{n}={_t(job, n)}" for n in outcomes if _t(job, n)),
            "queries_planned": _t(job, "queries_planned"),
            "queries_run": _t(job, "queries_run"),
            "tiers_run": _t(job, "tiers_run"),
            "pages_sent": _t(job, "pages_sent"),
            "pages_from_cache": _t(job, "pages_from_cache"),
            "pages_not_sent": _t(job, "pages_not_sent"),
            "search_results": str(len(by_results.get(key, []))),
            "candidates": str(total),
            "plausible_candidates": str(
                sum(1 for c in by_candidates.get(key, []) if _t(c, "plausible").upper() == "TRUE")
            ),
            "candidates_rejected": str(rejected),
            "candidates_not_selected": str(not_selected),
            "candidates_collected": str(collected),
            "candidates_other_decision": str(total - rejected - not_selected - collected),
            "inspections": str(len(inspected)),
            "inspection_outcomes": "; ".join(
                f"{name}={count}"
                for name, count in sorted(Counter(_t(i, "outcome") for i in inspected).items())
            ),
            "inspections_reused_from_cache": str(reused),
            "search_requests_reused_saved": str(saved),
            "search_requests_reused_same_run": str(same_run),
            "requests_avoided_by_reuse": str(_number(_t(job, "pages_from_cache")) + reused)
            if recorded
            else "",
            "cap_or_budget_reached": "TRUE" if caps else "FALSE",
            "caps_reached": "; ".join(caps),
        }
        for name in extras:
            row[name] = _t(job, name)
        rows.append(row)
    return rows, [*SEARCH_COVERAGE_COLUMNS, *extras]


def _caps(job: Mapping[str, str], decided: Mapping[str, int]) -> list[str]:
    caps: list[str] = []
    for name in ("search_coverage", "initial_stop_reason", "stop_reason"):
        for token in re.split(r"[;|,]", _t(job, name)):
            token = token.strip()
            if "CAP" in token.upper() or "LIMIT" in token.upper() or "BUDGET" in token.upper():
                caps.append(token)
    if _number(_t(job, "pages_not_sent")) > 0:
        caps.append("SEARCH_PAGES_NOT_SENT")
    if any(d.endswith("_LIMIT") and n for d, n in decided.items()):
        caps.append("CANDIDATE_LIMIT")
    if "per-round limit" in _t(job, "second_pass"):
        caps.append("PER_ROUND_CANDIDATE_LIMIT")
    return list(dict.fromkeys(caps))


def search_coverage_summary(
    rows: Sequence[Mapping[str, str]],
    jobs: Sequence[Mapping[str, str]],
    run_summary: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Counts of every outcome column by value and retailer, plus the run's budget usage."""
    fieldnames = list(dict.fromkeys(name for job in jobs for name in job))
    columns = outcome_columns(fieldnames, jobs)
    summary = run_summary or {}
    limits = summary.get("limits", {}) if isinstance(summary.get("limits"), dict) else {}
    usage = {
        "requests_sent": (summary.get("requests_sent"), limits.get("max_total_requests")),
        "search_requests_sent": (
            summary.get("search_requests_sent"),
            limits.get("max_search_requests"),
        ),
        "product_inspections": (
            summary.get("product_inspections"),
            limits.get("max_product_fetches"),
        ),
    }
    return {
        "target_retailer_rows": len(rows),
        "distinct_targets": len({_t(r, "target_id") for r in rows}),
        "retailers": sorted({_t(r, "retailer_id") for r in rows}),
        "terminal_outcome_column": rows[0]["terminal_outcome_column"] if rows else "",
        "terminal_outcomes": dict(Counter(_t(r, "terminal_outcome") for r in rows).most_common()),
        "terminal_outcomes_by_retailer": {
            retailer: dict(
                Counter(_t(r, "terminal_outcome") for r in rows if _t(r, "retailer_id") == retailer)
            )
            for retailer in sorted({_t(r, "retailer_id") for r in rows})
        },
        "outcome_columns": {
            name: dict(Counter(_t(j, name) for j in jobs if _t(j, name)).most_common())
            for name in columns
        },
        "rows_with_cap_or_budget_reached": sum(
            1 for r in rows if r["cap_or_budget_reached"] == "TRUE"
        ),
        "caps_reached": dict(
            Counter(
                cap for r in rows for cap in _t(r, "caps_reached").split("; ") if cap
            ).most_common()
        ),
        "requests_avoided_by_reuse": sum(_number(_t(r, "requests_avoided_by_reuse")) for r in rows),
        "run_budget": {
            name: {"used": used, "limit": limit, "reached": bool(limit) and (used or 0) >= limit}
            for name, (used, limit) in usage.items()
        },
        "run_search_responses_reused": summary.get("search_responses_reused", {}),
        "run_outcome_summaries": {
            key: value for key, value in summary.items() if "outcome" in key.casefold()
        },
    }


# Output and plan ---------------------------------------------------------------------------


def write_rows(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, str]]) -> int:
    count = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def publication_plan(
    files: Mapping[str, tuple[str, int, Sequence[str]]],
    allowed_tabs: Iterable[str],
) -> list[dict[str, object]]:
    """The intended tabs, preview only. ``files`` maps tab -> (file name, rows, header)."""
    allowed = set(allowed_tabs)
    plan = []
    for tab, (file_name, rows, header) in files.items():
        approved = tab in allowed
        plan.append(
            {
                "tab": tab,
                "source_file": file_name,
                "rows": rows,
                "columns": list(header),
                "allowed_by_sheets_writer": approved,
                "write_status": "PREVIEW_ONLY_NOT_WRITTEN",
                "before_any_write": (
                    "Owner approval to replace this approved P1 tab."
                    if approved
                    else "Owner approval of the new tab name, and an update of "
                    "sheets_writer.ALLOWED_OUTPUT_TABS (the writer refuses unapproved tabs)."
                ),
            }
        )
    return plan


def readme_text(summary: Mapping[str, object], plan: Sequence[Mapping[str, object]]) -> str:
    lines = [
        "# P1 audit diagnostics (preview)",
        "",
        "Local files prepared from the run's inputs. Nothing was written to a Sheet; the live "
        "P1 tabs still hold headers only until the owner approves a publication.",
        "",
        "| File | Rows | Intended tab | Tab approved in the writer |",
        "|---|---:|---|---|",
        *[
            f"| `{p['source_file']}` | {p['rows']} | {p['tab']} | "
            f"{'yes' if p['allowed_by_sheets_writer'] else 'no (new tab)'} |"
            for p in plan
        ],
        "",
        "## Checks",
        "",
        *[
            f"- {'PASS' if c['passed'] else 'FAIL'}: {c['check']} ({c['found']})"
            for c in summary["checks"]  # type: ignore[union-attr]
        ],
        "",
        "See `docs/CATALOG_BINDINGS_AND_DIAGNOSTICS_2026-11-03.md` for what each column means.",
        "",
    ]
    return "\n".join(lines)


def build_checks(
    eligible: Sequence[Mapping[str, str]],
    eligibility_rows: Sequence[Mapping[str, str]],
    mapping_rows: Sequence[Mapping[str, str]],
    coverage_rows: Sequence[Mapping[str, str]],
    jobs: Sequence[Mapping[str, str]],
) -> list[dict[str, object]]:
    """Self-checks: the files are complete and consistent with their sources."""
    selected = sum(1 for r in eligibility_rows if r["selection_status"] == SELECTED)
    unjoined = sum(1 for r in eligibility_rows if r["selection_status"] == NOT_IN_RANKING)
    unselected = len(eligibility_rows) - selected - unjoined
    keys = [(_t(j, "target_id"), _t(j, "retailer_id")) for j in jobs]
    one_per_target_retailer = len(coverage_rows) == len(set(keys)) == len(jobs)
    checks = (
        ("one eligibility row per eligible variation", len(eligibility_rows) == len(eligible)),
        ("every eligible variation is in the variation ranking", unjoined == 0),
        ("mapping exclusions cover every unselected variation", len(mapping_rows) == unselected),
        ("search coverage has one row per target x retailer", one_per_target_retailer),
    )
    found = (len(eligibility_rows), unjoined, len(mapping_rows), len(coverage_rows))
    return [
        {"check": name, "passed": ok, "found": count}
        for (name, ok), count in zip(checks, found, strict=True)
    ]


def write_diagnostics(
    out_dir: Path,
    *,
    eligibility: Sequence[Mapping[str, str]],
    not_eligible: Sequence[Mapping[str, str]],
    mapping: Sequence[Mapping[str, str]],
    coverage: Sequence[Mapping[str, str]],
    coverage_columns: Sequence[str],
    summary: dict[str, object],
    plan_files: Mapping[str, tuple[str, int, Sequence[str]]],
    allowed_tabs: Iterable[str],
) -> list[dict[str, object]]:
    """Writes the four CSVs, ``diagnostics_summary.json``, ``publication_plan.json`` and README."""
    out_dir.mkdir(parents=True, exist_ok=True)
    write_rows(out_dir / "eligibility_variations.csv", ELIGIBILITY_COLUMNS, eligibility)
    write_rows(
        out_dir / "eligibility_not_eligible_listings.csv", NOT_ELIGIBLE_COLUMNS, not_eligible
    )
    write_rows(out_dir / "mapping_exclusions_diagnostic.csv", MAPPING_EXCLUSION_COLUMNS, mapping)
    write_rows(out_dir / "search_coverage.csv", coverage_columns, coverage)
    plan = publication_plan(plan_files, allowed_tabs)
    (out_dir / "diagnostics_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / "publication_plan.json").write_text(
        json.dumps({"version": DIAGNOSTICS_VERSION, "mode": "PREVIEW_ONLY", "tabs": plan}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (out_dir / "README.md").write_text(readme_text(summary, plan), encoding="utf-8")
    return plan
