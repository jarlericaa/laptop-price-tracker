"""Phase 1 orchestration: read-only source data to local (and opt-in Sheets) outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Settings
from .data_quality import (
    CONFLICT_COLUMNS,
    DUPLICATE_DECISION_COLUMNS,
    DUPLICATE_KEY_COLUMNS,
    UNRESOLVED_CONFLICT_STATUSES,
    VALIDATION_ERROR_COLUMNS,
    apply_duplicate_decisions,
    collapse_duplicates,
    find_model_identity_conflicts,
    load_duplicate_decisions,
    validate_rows,
)
from .eligibility import apply_eligibility, load_manual_decisions, load_rules
from .periods import MANILA_TIMEZONE, detect_periods, ensure_fresh, period_summary
from .ranking import (
    aggregate_models,
    excluded_listings,
    rank_eligible_models,
    review_queue,
    top_100,
)
from .schema import normalize_raw_values
from .sheets_source import connect, open_spreadsheet, read_raw_values
from .sheets_writer import write_phase1_outputs


class PipelineSafetyError(RuntimeError):
    """Raised after local diagnostics are saved when source quality prevents publication."""


@dataclass
class Phase1Result:
    summary: dict[str, object]
    outputs: dict[str, pd.DataFrame]
    local_output_dir: Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _empty_conflicts() -> pd.DataFrame:
    return pd.DataFrame(columns=CONFLICT_COLUMNS)


def _run_metadata(now: datetime | None = None) -> tuple[str, str]:
    timestamp = (
        now.astimezone(ZoneInfo(MANILA_TIMEZONE))
        if now
        else datetime.now(ZoneInfo(MANILA_TIMEZONE))
    )
    return timestamp.strftime("%Y%m%dT%H%M%S%z"), timestamp.date().isoformat()


def _save_local_outputs(
    output_dir: Path,
    outputs: dict[str, pd.DataFrame],
    summary: dict[str, object],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    names = {
        "P1_TOP_100_TARGETS": "top_100_targets.csv",
        "P1_ELIGIBLE_RANKING": "eligible_model_ranking.csv",
        "P1_EXCLUDED_LISTINGS": "excluded_listings.csv",
        "P1_ELIGIBILITY_REVIEW": "eligibility_review.csv",
        "P1_CONFLICTING_DUPLICATES": "conflicting_duplicates.csv",
        "P1_VALIDATION_ERRORS": "validation_errors.csv",
        "P1_PERIOD_SUMMARY": "period_summary.csv",
    }
    for tab_name, filename in names.items():
        outputs[tab_name].to_csv(output_dir / filename, index=False)
    (output_dir / "phase1_run_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def process_raw_values(
    values: list[list[object]],
    settings: Settings,
    *,
    output_dir: Path | None = None,
    now: datetime | None = None,
    check_freshness: bool | None = None,
    fail_on_conflicts: bool = False,
    duplicate_decisions_path: Path | None = None,
) -> Phase1Result:
    """Process already-read worksheet values; suitable for credential-free local tests."""
    run_id, extract_date = _run_metadata(now)
    root = project_root()
    destination = output_dir or root / "data" / "output"
    destination.mkdir(parents=True, exist_ok=True)
    normalized = normalize_raw_values(values, run_id)
    validation = validate_rows(normalized)
    # Persist diagnostics before any later safety check can stop the run. This also
    # replaces reports from a previous run instead of leaving stale diagnostics behind.
    validation.errors.reindex(columns=VALIDATION_ERROR_COLUMNS).to_csv(
        destination / "validation_errors.csv", index=False
    )
    if len(validation.errors) > settings.max_validation_errors:
        raise PipelineSafetyError(
            f"Validation error count {len(validation.errors)} exceeds configured maximum "
            f"{settings.max_validation_errors}."
        )
    clean, duplicate_conflicts = collapse_duplicates(validation.valid_rows)
    duplicate_decisions = load_duplicate_decisions(
        duplicate_decisions_path or root / "config" / "duplicate_decisions.csv"
    )
    decided_rows, duplicate_conflicts, duplicate_review = apply_duplicate_decisions(
        validation.valid_rows, duplicate_conflicts, duplicate_decisions
    )
    if not decided_rows.empty:
        clean = (
            pd.concat([clean, decided_rows], ignore_index=True)
            .sort_values(DUPLICATE_KEY_COLUMNS, kind="stable")
            .reset_index(drop=True)
        )
    duplicate_conflicts.reindex(columns=CONFLICT_COLUMNS).to_csv(
        destination / "conflicting_duplicates.csv", index=False
    )
    # Local-only, redacted decision template; it is never published to Sheets.
    duplicate_review.reindex(columns=DUPLICATE_DECISION_COLUMNS).to_csv(
        destination / "duplicate_review.csv", index=False
    )
    selection = detect_periods(clean)
    freshness_enabled = (
        not settings.skip_freshness_check if check_freshness is None else check_freshness
    )
    ensure_fresh(selection, enabled=freshness_enabled, now=now)
    identity_conflict_models, identity_conflicts = find_model_identity_conflicts(clean)
    publication_rows = clean.loc[~clean["model_id"].isin(identity_conflict_models)].copy()

    rules = load_rules(root / "config" / "eligibility_rules.csv")
    decisions = load_manual_decisions(root / "config" / "eligibility_decisions.csv")
    classified = apply_eligibility(publication_rows, rules, decisions)
    model_summary = aggregate_models(
        classified, selection.m_minus_1_month, selection.mtd_month, extract_date
    )
    ranking = rank_eligible_models(model_summary)
    all_conflicts = pd.concat([duplicate_conflicts, identity_conflicts], ignore_index=True)
    if all_conflicts.empty:
        all_conflicts = _empty_conflicts()
    summary = {
        "run_id": run_id,
        "input_rows": len(normalized),
        "m_minus_1_month": selection.m_minus_1_month,
        "mtd_month": selection.mtd_month,
        "m_minus_1_rows": int((clean["period_month"] == selection.m_minus_1_month).sum()),
        "mtd_rows": int((clean["period_month"] == selection.mtd_month).sum()),
        "eligible_models": int((model_summary["eligibility_status"] == "ELIGIBLE").sum()),
        "excluded_models": int((model_summary["eligibility_status"] == "EXCLUDED").sum()),
        "review_required_models": int((model_summary["eligibility_status"] == "REVIEW").sum()),
        "conflicting_duplicate_keys": len(all_conflicts),
        "mtd_active_models": int(model_summary["present_in_mtd"].sum()),
        "top_100_count": len(top_100(ranking)),
    }
    outputs = {
        "P1_TOP_100_TARGETS": top_100(ranking),
        "P1_ELIGIBLE_RANKING": ranking,
        "P1_EXCLUDED_LISTINGS": excluded_listings(model_summary),
        "P1_ELIGIBILITY_REVIEW": review_queue(model_summary, decisions),
        "P1_CONFLICTING_DUPLICATES": all_conflicts,
        "P1_VALIDATION_ERRORS": validation.errors.reindex(columns=VALIDATION_ERROR_COLUMNS),
        "P1_PERIOD_SUMMARY": period_summary(clean, selection, run_id),
    }
    _save_local_outputs(destination, outputs, summary)
    result = Phase1Result(summary=summary, outputs=outputs, local_output_dir=destination)
    unresolved = int(all_conflicts["resolution_status"].isin(UNRESOLVED_CONFLICT_STATUSES).sum())
    if fail_on_conflicts and unresolved:
        raise PipelineSafetyError(
            f"{unresolved} conflicting duplicate or model identity records were quarantined "
            "without a recorded decision; publication is blocked. Review duplicate_review.csv "
            "and record decisions in config/duplicate_decisions.csv."
        )
    return result


def run_phase1(
    settings: Settings,
    *,
    write_sheets: bool = False,
    output_dir: Path | None = None,
) -> Phase1Result:
    """Read `raw`, create local outputs, and write only approved outputs when explicitly asked."""
    _title, _worksheet_name, values = read_raw_values(settings)
    result = process_raw_values(
        list(values), settings, output_dir=output_dir, fail_on_conflicts=True
    )
    if write_sheets:
        client = connect(settings, read_only=False)
        spreadsheet = open_spreadsheet(client, settings.spreadsheet_id or "")
        write_phase1_outputs(
            spreadsheet,
            result.outputs,
            result.summary,
            load_manual_decisions(project_root() / "config" / "eligibility_decisions.csv"),
        )
    return result
