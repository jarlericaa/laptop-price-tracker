#!/usr/bin/env python3
"""Propose model / cpu_series convention corrections per target_id (local, read-only)."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.catalog_conventions import (
    DUPLICATE_COLUMNS,
    PROPOSAL_COLUMNS,
    CatalogInputError,
    apply_ready_proposals,
    build_report,
    correction_report_markdown,
)
from laptop_tracker.match_run import ObservationFileError, read_observations_csv
from laptop_tracker.supporting_tables import validate_match_review, validate_source_model_map
from laptop_tracker.target_master import (
    TargetMasterSchemaError,
    read_table_snapshot_csv,
    validate_target_master,
)

ROOT = Path(__file__).resolve().parent
PROPOSALS = "catalog_convention_proposals.csv"
DUPLICATES = "proposed_duplicate_core_keys.csv"
DIAGNOSTIC = "DIAGNOSTIC_target_master_ready_proposals_applied.csv"
REPORT = "SHEET_CORRECTION_REPORT.md"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Propose model and cpu_series values under the 2026-10-08 conventions for every "
            "target_id in a validated TARGET_MASTER snapshot (a sheet export or "
            "check_target_tables.py's validated copy). Optional mapping/review snapshots add "
            "review-recorded codes and hints; optional Top 100 coverage and saved observations "
            "add context. Writes local reports only. Never reads raw or Sheets, never edits the "
            "catalog, and never changes target IDs or mappings."
        )
    )
    parser.add_argument("--target-master", type=Path, required=True)
    parser.add_argument("--source-model-map", type=Path, help="Requires --match-review.")
    parser.add_argument("--match-review", type=Path, help="Requires --source-model-map.")
    parser.add_argument("--top-100-coverage", type=Path, help="audit_top_100_coverage.py output.")
    parser.add_argument("--observations", type=Path, help="Saved offer observation CSV.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "output" / "catalog_conventions",
        help="Directory for local reports (default: data/output/catalog_conventions, ignored).",
    )
    args = parser.parse_args(argv)
    if bool(args.source_model_map) != bool(args.match_review):
        parser.error("--source-model-map and --match-review must be given together")
    return args


def write_rows(path: Path, columns, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = [args.output_dir / name for name in (PROPOSALS, DUPLICATES, DIAGNOSTIC, REPORT)]
    for path in outputs:
        path.unlink(missing_ok=True)  # Never leave stale proposals behind a failed run.

    try:
        target_master = validate_target_master(read_table_snapshot_csv(args.target_master))
        mapping = review = None
        if args.source_model_map:
            mapping = validate_source_model_map(
                read_table_snapshot_csv(args.source_model_map), target_master
            )
            review = validate_match_review(read_table_snapshot_csv(args.match_review))
        top_100: set[str] = set()
        if args.top_100_coverage:
            with args.top_100_coverage.open(newline="", encoding="utf-8-sig") as handle:
                top_100 = {row["target_id"] for row in csv.DictReader(handle) if row["target_id"]}
        observations = (
            [item.observation for item in read_observations_csv(args.observations)]
            if args.observations
            else []
        )
        report = build_report(
            target_master,
            source_model_map=mapping,
            match_review=review,
            top_100_target_ids=top_100,
            observations=observations,
        )
    except (
        FileNotFoundError,
        KeyError,
        UnicodeDecodeError,
        TargetMasterSchemaError,
        CatalogInputError,
        ObservationFileError,
    ) as exc:
        print(f"Convention proposal stopped: {exc}", file=sys.stderr)
        return 1

    write_rows(outputs[0], PROPOSAL_COLUMNS, report.proposals)
    write_rows(outputs[1], DUPLICATE_COLUMNS, report.duplicates)
    diagnostic = apply_ready_proposals(target_master.valid_rows, report.proposals)
    with outputs[2].open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(diagnostic)
    outputs[3].write_text(
        correction_report_markdown(report, args.target_master.name), encoding="utf-8"
    )
    diagnostic_check = validate_target_master(diagnostic)

    # Counts only: target values stay in the local files.
    print(f"Targets reviewed: {len(report.proposals)}")
    for key, count in report.counts.items():
        print(f"  {key}: {count}")
    print(
        "DIAGNOSTIC copy (READY proposals applied; not authoritative): "
        f"{len(diagnostic_check.valid_rows)}/{len(diagnostic_check.rows)} rows pass validation."
    )
    for code, count in diagnostic_check.errors["error_code"].value_counts().sort_index().items():
        print(f"  DIAGNOSTIC {code}: {count}")
    print(f"Reports written to {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
