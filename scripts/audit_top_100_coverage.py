#!/usr/bin/env python3
"""Audit accepted Top 100 source-ID coverage in the validated Phase 5 tables (read-only)."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.config import Settings
from laptop_tracker.coverage import (
    COMPLETE_WITHOUT_MAPPING_COLUMNS,
    TOP_100_COVERAGE_COLUMNS,
    CoverageInputError,
    CoverageStatus,
    audit_coverage,
)
from laptop_tracker.sheets_source import (
    TARGET_TABLE_TABS,
    SheetsSourceError,
    read_target_table_values,
)
from laptop_tracker.supporting_tables import validate_match_review, validate_source_model_map
from laptop_tracker.target_master import (
    TargetMasterSchemaError,
    read_table_snapshot_csv,
    read_target_master_csv,
    validate_target_master,
)

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_FILENAME = "top_100_coverage.csv"
COMPLETE_FILENAME = "complete_review_without_mapping.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Report how the accepted Phase 4 Top 100 model IDs are covered by SOURCE_MODEL_MAP "
            "and MATCH_REVIEW, and list COMPLETE review rows without a mapping to an ACTIVE "
            "target. Reads local CSV snapshots (exports or *_validated.csv copies), or with "
            "--from-sheets the three target tabs using the read-only scope. Never reads raw or "
            "P1_TOP_100_TARGETS, never writes to Sheets, and never promotes or maps rows."
        )
    )
    parser.add_argument(
        "--top-100",
        type=Path,
        default=ROOT / "data" / "output" / "top_100_targets.csv",
        help="Accepted Phase 4 Top 100 CSV (default: data/output/top_100_targets.csv).",
    )
    parser.add_argument("--target-master", type=Path, help="TARGET_MASTER CSV snapshot.")
    parser.add_argument("--source-model-map", type=Path, help="SOURCE_MODEL_MAP CSV snapshot.")
    parser.add_argument("--match-review", type=Path, help="MATCH_REVIEW CSV snapshot.")
    parser.add_argument(
        "--from-sheets",
        action="store_true",
        help="Read the three target tabs from the configured spreadsheet (read-only).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "output",
        help="Directory for local reports (default: data/output, ignored by Git).",
    )
    args = parser.parse_args(argv)
    local = [args.target_master, args.source_model_map, args.match_review]
    if args.from_sheets and any(local):
        parser.error("use either --from-sheets or local CSV snapshots, not both")
    if not args.from_sheets and not all(local):
        parser.error(
            "supply --target-master, --source-model-map, and --match-review, or --from-sheets"
        )
    return args


def load_tables(args: argparse.Namespace) -> dict[str, list[list[str]]]:
    if args.from_sheets:
        tables = read_target_table_values(Settings.from_env(), TARGET_TABLE_TABS)
    else:
        tables = {
            "TARGET_MASTER": read_table_snapshot_csv(args.target_master),
            "SOURCE_MODEL_MAP": read_table_snapshot_csv(args.source_model_map),
            "MATCH_REVIEW": read_table_snapshot_csv(args.match_review),
        }
    missing = [name for name, values in tables.items() if values is None]
    if missing:
        raise CoverageInputError(f"Worksheet(s) not found: {', '.join(missing)}.")
    return tables  # type: ignore[return-value]


def write_rows(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = (args.output_dir / COVERAGE_FILENAME, args.output_dir / COMPLETE_FILENAME)
    # Remove previous reports so a failed run never leaves stale coverage behind.
    for path in outputs:
        path.unlink(missing_ok=True)

    try:
        if not args.top_100.is_file():
            raise CoverageInputError(f"Top 100 CSV not found at {args.top_100}.")
        top_100 = read_target_master_csv(args.top_100)
        tables = load_tables(args)
        target_master = validate_target_master(tables["TARGET_MASTER"])
        results = {
            "TARGET_MASTER": target_master,
            "SOURCE_MODEL_MAP": validate_source_model_map(
                tables["SOURCE_MODEL_MAP"], target_master
            ),
            "MATCH_REVIEW": validate_match_review(tables["MATCH_REVIEW"]),
        }
        for name, result in results.items():
            if not result.is_valid:
                for code, count in result.errors["error_code"].value_counts().sort_index().items():
                    print(f"  {name} {code}: {count}")
        report = audit_coverage(
            top_100,
            results["TARGET_MASTER"],
            results["SOURCE_MODEL_MAP"],
            results["MATCH_REVIEW"],
        )
    except (
        FileNotFoundError,
        UnicodeDecodeError,
        ValueError,
        SheetsSourceError,
        TargetMasterSchemaError,
        CoverageInputError,
    ) as exc:
        print(f"Top 100 coverage audit stopped: {exc}", file=sys.stderr)
        return 1

    write_rows(outputs[0], TOP_100_COVERAGE_COLUMNS, report.rows)
    write_rows(outputs[1], COMPLETE_WITHOUT_MAPPING_COLUMNS, report.complete_without_mapping)

    # Counts only: IDs and names stay in the local reports.
    print(
        f"Top 100 rows read: {report.top_100_rows}; distinct model IDs: {len(report.rows)}; "
        f"repeated model ID rows skipped: {report.duplicate_top_100_rows}."
    )
    for status in CoverageStatus:
        print(f"  {status.value}: {report.counts.get(status.value, 0)}")
    for statuses, count in sorted(report.unmapped_review_status_counts.items()):
        print(f"    unmapped, review status {statuses}: {count}")
    unmapped_complete = len(report.complete_without_mapping)
    print(f"COMPLETE review rows without a valid ACTIVE mapping: {unmapped_complete}")
    for issue, count in sorted(
        Counter(row["issue"] for row in report.complete_without_mapping).items()
    ):
        print(f"    {issue}: {count}")
    print(f"Reports written to {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
