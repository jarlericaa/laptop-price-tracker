"""Selection audit for the Top 100 eligible mapped Shopee variations (2026-11-03).

Explains, from the written outputs and the catalog inputs, how many variations were eligible, how
many qualified, why the others did not, which catalog changes were picked up and whether the pool
itself is what the Phase 1 policies say it is. It reads local files only, never raw data or Sheets,
and changes nothing. ``MATCH_REVIEW`` is evidence shown beside a finding (status, date, whether
its recorded variation is still the selected one, its own unresolved note); it never selects,
excludes or overrides anything.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

from .variation_ranking import (
    UNMAPPED,
    build_variation_ranking,
    build_variation_snapshot,
)
from .variation_specs import COMPARED_FIELDS, read_variation_specs
from .verified_top import VerifiedRankingInputError

AUDIT_VERSION = "variation-selection-audit-1"
REPRESENTATIVES_PER_REASON = 5
EXCERPT = 240
_CORE = ("model", "screen_size_in", "cpu_series", "ram_capacity_gb", "storage_capacity_gb")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _excerpt(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:EXCERPT]


def review_evidence(
    model_id: str,
    variation_name: str,
    review_by_source: Mapping[str, Mapping[str, str]],
) -> dict[str, str]:
    """What MATCH_REVIEW holds for one source variation, and whether it describes *this* selection.

    ``variation_state`` is ``CURRENT`` when the review row records the selected variation's name
    (as its candidate name or in its notes), ``STALE`` when it records a different one (the
    variation changed since the review: its specification fields describe another selection and
    are not used), and ``UNRECORDED`` when it records no variation name.
    """
    row = review_by_source.get(model_id)
    if row is None:
        return {"review_row": "NONE"}
    wanted = (variation_name or "").strip()
    recorded = (row.get("candidate_name") or "").strip()
    notes = f"{row.get('variant_notes', '')}\n{row.get('notes', '')}"
    if wanted and (wanted == recorded or wanted in notes):
        state = "CURRENT"
    elif recorded:
        state = "STALE"
    else:
        state = "UNRECORDED"
    return {
        "review_row": "FOUND",
        "review_id": row.get("review_id", ""),
        "status": row.get("status", ""),
        "reviewed_at": row.get("reviewed_at", ""),
        "variation_state": state,
        "recorded_variation": recorded[:80],
        "complete_core_identity": str(all((row.get(f) or "").strip() for f in _CORE)),
        "unresolved_note": _excerpt(row.get("review_reason", "")),
    }


def catalog_changes(
    previous_dir: Path,
    current: Mapping[str, Sequence[Mapping[str, str]]],
    ranked: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    """Targets and mappings added, changed or removed against an earlier validated catalog."""
    old_targets = {
        r["target_id"]: r for r in read_rows(previous_dir / "target_master_validated.csv")
    }
    old_maps = {
        r["source_model_id"]: r["target_id"]
        for r in read_rows(previous_dir / "source_model_map_validated.csv")
    }
    new_targets = {r["target_id"]: r for r in current["TARGET_MASTER"]}
    new_maps = {r["source_model_id"]: r["target_id"] for r in current["SOURCE_MODEL_MAP"]}
    by_model = {r["source_model_id"]: r for r in ranked}
    added_maps = sorted(set(new_maps) - set(old_maps))
    return {
        "targets_added": sorted(set(new_targets) - set(old_targets)),
        "targets_removed": sorted(set(old_targets) - set(new_targets)),
        "targets_changed": sorted(
            t for t in new_targets if t in old_targets and new_targets[t] != old_targets[t]
        ),
        "mappings_added": [
            {
                "source_model_id": m,
                "target_id": new_maps[m],
                "in_eligible_pool": m in by_model,
                "selection_status": by_model[m]["selection_status"] if m in by_model else "",
                "eligible_rank": by_model[m]["eligible_rank"] if m in by_model else "",
                "exclusion_reason": by_model[m]["exclusion_reason"] if m in by_model else "",
            }
            for m in added_maps
        ],
        "mappings_removed": sorted(set(old_maps) - set(new_maps)),
        "mappings_retargeted": sorted(
            m for m in new_maps if m in old_maps and new_maps[m] != old_maps[m]
        ),
    }


def pool_trace(
    ranking_path: Path,
    ranked: Sequence[Mapping[str, str]],
    mapped_ids: set[str],
    valid_target_by_source: Mapping[str, bool],
) -> dict[str, object]:
    """Where the eligible pool comes from, with the checks the brief asks for."""
    folder = ranking_path.parent
    summary_path = folder / "phase1_run_summary.json"
    phase1 = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    file_rows = read_rows(ranking_path)
    # The pool is what the builder keeps: ELIGIBLE and present in the MTD month.
    pool = [
        r
        for r in file_rows
        if r["eligibility_status"] == "ELIGIBLE" and r["present_in_mtd"] == "True"
    ]
    excluded = (
        {r["model_id"] for r in read_rows(folder / "excluded_listings.csv")}
        if (folder / "excluded_listings.csv").is_file()
        else set()
    )
    review = (
        {r["model_id"] for r in read_rows(folder / "eligibility_review.csv")}
        if (folder / "eligibility_review.csv").is_file()
        else set()
    )
    pool_ids = {r["model_id"] for r in pool}
    outside = sorted(mapped_ids - pool_ids)
    classified = Counter(
        "PHASE1_EXCLUDED_BY_ELIGIBILITY_RULES"
        if m in excluded
        else "PHASE1_ELIGIBILITY_REVIEW_PENDING"
        if m in review
        else "NOT_IN_ELIGIBLE_RANKING_NO_MTD_SALES_OR_ABSENT"
        for m in outside
    )
    normalised = lambda value: value.strip().removesuffix(".0").lstrip("0")  # noqa: E731
    mapped_norm = {normalised(m) for m in mapped_ids}
    near = [
        r["model_id"]
        for r in pool
        if r["model_id"] not in mapped_ids and normalised(r["model_id"]) in mapped_norm
    ]
    eligible_models = phase1.get("eligible_models")
    return {
        "phase1_run_summary": {
            key: phase1.get(key)
            for key in (
                "run_id",
                "input_rows",
                "m_minus_1_month",
                "m_minus_1_rows",
                "mtd_month",
                "mtd_rows",
                "mtd_active_models",
                "eligible_models",
                "excluded_models",
                "review_required_models",
                "conflicting_duplicate_keys",
            )
        },
        "eligible_ranking_file_rows": len(file_rows),
        "file_rows_not_eligible_or_not_in_mtd": len(file_rows) - len(pool),
        "eligible_ranking_rows": len(pool),
        "unique_source_keys": len({r["source_key"] for r in pool}) == len(pool),
        "unique_model_ids": len(pool_ids) == len(pool),
        "rows_with_zero_mtd_gmv": sum(1 for r in pool if float(r["mtd_gmv_usd"]) == 0),
        "rows_with_zero_m_minus_1_gmv": sum(1 for r in pool if float(r["m_minus_1_gmv_usd"]) == 0),
        "eligible_models_absent_from_the_mtd_month": (
            eligible_models - len(pool) if isinstance(eligible_models, int) else None
        ),
        "matches_selection_input": len(pool) == len(ranked),
        "mapped_source_ids": len(mapped_ids),
        "mapped_ids_in_eligible_pool": len(mapped_ids & pool_ids),
        "mapped_ids_outside_eligible_pool": len(outside),
        "mapped_ids_outside_pool_by_phase1_state": dict(sorted(classified.items())),
        "mapped_ids_outside_pool_with_valid_target": sum(
            1 for m in outside if valid_target_by_source.get(m)
        ),
        "identifier_checks": {
            "pool_ids_not_all_digits": [r["model_id"] for r in pool if not r["model_id"].isdigit()],
            "mapped_ids_not_all_digits": sorted(m for m in mapped_ids if not m.isdigit()),
            "unmapped_ids_equal_to_a_mapped_id_after_normalising": near,
        },
    }


def earlier_pools(
    comparisons: Mapping[str, Path], target_master, source_model_map, top_n: int
) -> list[dict[str, object]]:
    """Earlier eligible pools, re-evaluated against the *current* catalog (never published data)."""
    found = []
    for label, path in comparisons.items():
        rows = read_rows(path)
        entry: dict[str, object] = {"label": label, "eligible_ranking_rows": len(rows)}
        if rows:
            entry["extract_date"] = rows[0].get("extract_date", "")
            entry["mtd_month"] = rows[0].get("mtd_month", "")
        try:
            result = build_variation_ranking(
                build_variation_snapshot(rows), target_master, source_model_map, top_n=top_n
            )
        except (VerifiedRankingInputError, KeyError) as exc:
            entry["note"] = f"not evaluable at variation grain: {exc}"
        else:
            entry["verified_mapped_under_current_catalog"] = result.counts[
                "eligible_variations_with_verified_mapping"
            ]
            entry["selected_under_current_catalog"] = result.counts["selected_variations"]
            entry["shortfall_under_current_catalog"] = result.counts["shortfall"]
        found.append(entry)
    return found


def representatives(
    ranked: Sequence[Mapping[str, str]],
    review_by_source: Mapping[str, Mapping[str, str]],
    per_reason: int = REPRESENTATIVES_PER_REASON,
) -> dict[str, list[dict[str, object]]]:
    """The highest-GMV excluded rows of each reason with the evidence supporting exclusion."""
    by_reason: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in ranked:
        if row["exclusion_reason"]:
            by_reason[row["exclusion_reason"]].append(row)
    out: dict[str, list[dict[str, object]]] = {}
    for reason, rows in sorted(by_reason.items()):
        picked = sorted(rows, key=lambda r: -float(r["ranking_gmv_usd"]))[:per_reason]
        out[reason] = [
            {
                "eligible_rank": r["eligible_rank"],
                "source_key": r["source_key"],
                "item_name": r["item_name"][:90],
                "shopee_model_name": r["shopee_model_name"][:60],
                "ranking_gmv_usd": r["ranking_gmv_usd"],
                "target_id": r["target_id"],
                "evidence": r["comparison_detail"][:400],
                "variation_specs": r["variation_spec_evidence"],
                "variation_spec_conflicts": r["variation_spec_conflicts"],
                "match_review": review_evidence(
                    r["source_model_id"], r["shopee_model_name"], review_by_source
                ),
            }
            for r in picked
        ]
    return out


def unmapped_review_state(
    ranked: Sequence[Mapping[str, str]], review_by_source: Mapping[str, Mapping[str, str]]
) -> dict[str, object]:
    """For the unmapped variations: what MATCH_REVIEW holds (no mapping is ever proposed here)."""
    rows = [r for r in ranked if r["exclusion_reason"] == UNMAPPED]
    state = Counter()
    complete_core = []
    for r in rows:
        ev = review_evidence(r["source_model_id"], r["shopee_model_name"], review_by_source)
        state[
            ev.get("status", "NO_REVIEW_ROW") if ev["review_row"] == "FOUND" else "NO_REVIEW_ROW"
        ] += 1
        if ev.get("complete_core_identity") == "True":
            complete_core.append(r["source_key"])
    return {
        "unmapped_variations": len(rows),
        "by_match_review_status": dict(sorted(state.items())),
        "with_a_complete_core_identity_in_match_review_but_no_mapping": complete_core,
        "note": "Listed for owner review only; nothing here maps, promotes or approves a row.",
    }


def selected_review_evidence(
    top: Sequence[Mapping[str, str]], review_by_source: Mapping[str, Mapping[str, str]]
) -> dict[str, object]:
    """MATCH_REVIEW coverage of the selected rows, and stale review rows that were not used."""
    states = Counter()
    stale = []
    for r in top:
        ev = review_evidence(r["source_model_id"], r["shopee_model_name"], review_by_source)
        states[ev.get("variation_state", "NO_REVIEW_ROW")] += 1
        if ev.get("variation_state") == "STALE":
            stale.append(
                {
                    "source_key": r["source_key"],
                    "current_variation": r["shopee_model_name"][:60],
                    "review_recorded_variation": ev["recorded_variation"],
                }
            )
    return {
        "selected": len(top),
        "review_row_state": dict(sorted(states.items())),
        "stale_review_rows_not_used": stale,
        "note": "A review row that records another variation describes another selection; its "
        "specification fields never override the current selected variation.",
    }


def spec_reading_coverage(top: Sequence[Mapping[str, str]]) -> dict[str, dict[str, int]]:
    """How much of each compared field the selected variations state themselves.

    ``VARIATION`` is read from the variation's own name, ``PARENT`` from a single-valued parent
    title, ``AMBIGUOUS`` is a stated-but-unreadable or multi-option text (kept unknown), and
    ``UNKNOWN`` is not stated at all. Only stated values can reveal a disagreement.
    """
    found = {field: Counter() for field in COMPARED_FIELDS}
    for row in top:
        readings = read_variation_specs(row["item_name"], row["shopee_model_name"])
        for field, reading in readings.items():
            found[field][reading.source] += 1
    return {field: dict(sorted(counts.items())) for field, counts in found.items()}


def build_selection_audit(
    *,
    summary: Mapping[str, object],
    ranked: Sequence[Mapping[str, str]],
    top: Sequence[Mapping[str, str]],
    spec_conflict_rows: Sequence[Mapping[str, str]],
    review_rows: Sequence[Mapping[str, str]],
    ranking_path: Path,
    current_catalog: Mapping[str, Sequence[Mapping[str, str]]],
    target_master,
    source_model_map,
    previous_catalog_dir: Path | None = None,
    comparisons: Mapping[str, Path] | None = None,
    top_n: int = 100,
    expectations: Mapping[str, object] | None = None,
) -> dict[str, object]:
    counts = summary["counts"]
    review_by_source = {r["source_model_id"]: r for r in review_rows}
    targets = {r["target_id"]: r for r in current_catalog["TARGET_MASTER"]}
    maps = {r["source_model_id"]: r["target_id"] for r in current_catalog["SOURCE_MODEL_MAP"]}
    valid_target = {
        m: targets.get(t, {}).get("status") == "ACTIVE"
        and targets.get(t, {}).get("tracking_mode", "CONFIGURATION") != "LISTING_ONLY"
        for m, t in maps.items()
    }
    changes = (
        catalog_changes(previous_catalog_dir, current_catalog, ranked)
        if previous_catalog_dir
        else None
    )
    checks: list[dict[str, object]] = []
    expectations = expectations or {}
    inputs = summary.get("catalog_inputs", {})
    expected_id = expectations.get("spreadsheet_id")
    if expected_id:
        checks.append(
            {
                "check": "catalog read from the expected spreadsheet",
                "passed": inputs.get("spreadsheet_id") == expected_id,
                "found": inputs.get("spreadsheet_id"),
            }
        )
    if "new_mappings" in expectations and changes:
        checks.append(
            {
                "check": "new source mappings present",
                "passed": len(changes["mappings_added"]) >= int(expectations["new_mappings"]),
                "found": len(changes["mappings_added"]),
                "expected_at_least": expectations["new_mappings"],
            }
        )
    if "new_targets" in expectations and changes:
        checks.append(
            {
                "check": "new targets present",
                "passed": len(changes["targets_added"]) >= int(expectations["new_targets"]),
                "found": len(changes["targets_added"]),
                "expected_at_least": expectations["new_targets"],
            }
        )
    checks.append(
        {
            "check": "catalog inputs are a current Sheets retrieval",
            "passed": inputs.get("status") in {"FRESH_SHEETS_RETRIEVAL", "FRESH_SHEETS_READ"},
            "found": inputs.get("status"),
        }
    )
    trace = pool_trace(ranking_path, ranked, set(maps), valid_target)
    qualifying = counts["eligible_variations_with_verified_mapping"]
    return {
        "audit_version": AUDIT_VERSION,
        "list_name": summary.get("list_name"),
        "selection_policy": summary.get("selection_policy"),
        "sales_snapshot": summary.get("sales_snapshot"),
        "catalog_inputs": inputs,
        "catalog_changes": changes,
        "selection": {
            "eligible_pool": counts["eligible_variations"],
            "qualifying_mapped_variations": qualifying,
            "excluded_by_reason": counts["excluded_by_mapping_failure"],
            "selected_variations": counts["selected_variations"],
            "qualified_but_beyond_the_limit": counts["qualified_not_selected"],
            "distinct_target_contexts": counts["distinct_configuration_contexts"],
            "requested_top_n": top_n,
            "the_limit_bound": qualifying > top_n,
            "shortfall": counts["shortfall"],
            "shortfall_reason": counts["shortfall_reason"],
            "selected_from_eligible_rank_beyond_the_limit": counts[
                "selected_with_lower_eligible_rank_than_top_n"
            ],
            "variation_spec_conflict_rows": counts["variation_spec_conflict_rows"],
        },
        "pool_trace": trace,
        "earlier_pools_under_current_catalog": earlier_pools(
            comparisons or {}, target_master, source_model_map, top_n
        ),
        "representative_exclusions": representatives(ranked, review_by_source),
        "unmapped_variations": unmapped_review_state(ranked, review_by_source),
        "selected_review_evidence": selected_review_evidence(top, review_by_source),
        "selected_spec_reading_coverage": spec_reading_coverage(top),
        "variation_spec_conflicts": [dict(r) for r in spec_conflict_rows],
        "checks": checks,
        "checks_passed": all(c["passed"] for c in checks),
    }


def audit_markdown(audit: Mapping[str, object]) -> str:
    sel = audit["selection"]
    trace = audit["pool_trace"]
    p1 = trace["phase1_run_summary"]
    lines = [
        f"# Selection audit: {audit['list_name']}",
        "",
        f"Policy `{audit['selection_policy']}`. Sales snapshot: "
        f"{(audit.get('sales_snapshot') or {}).get('labels', '')}",
        "",
        "## Catalog inputs",
        "",
        f"- Status: **{audit['catalog_inputs'].get('status')}**, "
        f"retrieved {audit['catalog_inputs'].get('retrieved_at_utc')} "
        f"(spreadsheet {audit['catalog_inputs'].get('spreadsheet_id')}).",
        f"- {audit['catalog_inputs'].get('detail', '')}",
    ]
    for tab, info in (audit["catalog_inputs"].get("tabs") or {}).items():
        lines.append(
            f"- {tab}: {info.get('data_rows')} rows, validated copy {info['validated_sha256']}"
        )
    changes = audit.get("catalog_changes")
    if changes:
        lines += [
            "",
            f"Against the previous catalog: {len(changes['targets_added'])} targets added "
            f"({', '.join(changes['targets_added'])}), {len(changes['targets_changed'])} changed "
            f"({', '.join(changes['targets_changed'])}), {len(changes['mappings_added'])} mappings "
            f"added, {len(changes['mappings_retargeted'])} retargeted, "
            f"{len(changes['mappings_removed'])} removed.",
        ]
        for m in changes["mappings_added"]:
            lines.append(
                f"  - {m['source_model_id']} -> {m['target_id']}: "
                + (
                    f"{m['selection_status']} (eligible rank {m['eligible_rank']}"
                    + (f", {m['exclusion_reason']}" if m["exclusion_reason"] else "")
                    + ")"
                    if m["in_eligible_pool"]
                    else "not in this period's eligible pool"
                )
            )
    lines += [
        "",
        "## Where the eligible pool comes from",
        "",
        f"- Phase 1 run {p1['run_id']}: {p1['input_rows']} raw rows; "
        f"{p1['m_minus_1_rows']} rows in "
        f"M-1 {p1['m_minus_1_month']}, {p1['mtd_rows']} in MTD {p1['mtd_month']} "
        f"({p1['mtd_active_models']} models active in MTD).",
        f"- {p1['eligible_models']} models are ELIGIBLE over all periods; the ranking keeps those "
        f"present in MTD: **{trace['eligible_ranking_rows']}** "
        f"({trace['eligible_models_absent_from_the_mtd_month']} eligible models have no sales in "
        f"the MTD month). Established rule (DECISIONS.md): only eligible models present in MTD "
        "rank.",
        f"- Ranking file rows {trace['eligible_ranking_file_rows']}, of which "
        f"{trace['file_rows_not_eligible_or_not_in_mtd']} not ELIGIBLE or not in MTD; "
        f"unique source keys: {trace['unique_source_keys']}; unique model IDs: "
        f"{trace['unique_model_ids']}; pool equals the selection input: "
        f"{trace['matches_selection_input']}.",
        f"- Mapped source IDs in the catalog: {trace['mapped_source_ids']}; in this pool "
        f"{trace['mapped_ids_in_eligible_pool']}, outside it "
        f"{trace['mapped_ids_outside_eligible_pool']} "
        f"({trace['mapped_ids_outside_pool_with_valid_target']} of them with an active, "
        f"non-listing-only target): {trace['mapped_ids_outside_pool_by_phase1_state']}.",
        f"- Identifier checks: {trace['identifier_checks']}.",
    ]
    for pool in audit["earlier_pools_under_current_catalog"]:
        lines.append(
            f"- Earlier pool `{pool['label']}` (extract {pool.get('extract_date')}, MTD "
            f"{pool.get('mtd_month')}): {pool['eligible_ranking_rows']} eligible rows; under the "
            f"current catalog {pool.get('verified_mapped_under_current_catalog', 'n/a')} verified, "
            f"{pool.get('selected_under_current_catalog', 'n/a')} selected "
            f"(shortfall {pool.get('shortfall_under_current_catalog', 'n/a')}). "
            + pool.get("note", "")
        )
    lines += [
        "",
        "## Selection",
        "",
        f"- Eligible pool {sel['eligible_pool']}; qualifying mapped variations "
        f"{sel['qualifying_mapped_variations']}; selected {sel['selected_variations']} of "
        f"{sel['requested_top_n']} across {sel['distinct_target_contexts']} target contexts; the "
        f"limit bound: {sel['the_limit_bound']}.",
        f"- Shortfall {sel['shortfall']}: {sel['shortfall_reason']}",
        "- Exclusions by precise reason: "
        + ", ".join(f"{k} {v}" for k, v in sel["excluded_by_reason"].items()),
        "",
        "## Representative excluded rows (highest GMV per reason)",
    ]
    for reason, rows in audit["representative_exclusions"].items():
        lines += ["", f"### {reason}"]
        for r in rows:
            review = r["match_review"]
            lines.append(
                f"- eligible rank {r['eligible_rank']} `{r['source_key']}` GMV "
                f"{r['ranking_gmv_usd']}: "
                f"{r['item_name']} / {r['shopee_model_name']}. Evidence: {r['evidence']} "
                + (
                    f"MATCH_REVIEW {review['status']} ({review['reviewed_at']}, variation "
                    f"{review['variation_state']}): {review['unresolved_note']}"
                    if review["review_row"] == "FOUND"
                    else "MATCH_REVIEW: no row."
                )
            )
    un = audit["unmapped_variations"]
    lines += [
        "",
        "## Unmapped variations and MATCH_REVIEW",
        "",
        f"- {un['unmapped_variations']} unmapped; by review status {un['by_match_review_status']}; "
        f"complete core identity but no mapping: "
        f"{un['with_a_complete_core_identity_in_match_review_but_no_mapping']}. {un['note']}",
        "",
        "## Checks",
        "",
    ]
    lines += [
        f"- {'PASS' if c['passed'] else 'FAIL'}: {c['check']} (found {c['found']})"
        for c in audit["checks"]
    ]
    return "\n".join(lines) + "\n"
