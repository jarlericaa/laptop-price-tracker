#!/usr/bin/env python3
"""Validate a local TARGET_MASTER CSV export against the proposed schema (no Google access)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.target_master import (
    TARGET_ERROR_COLUMNS,
    TARGET_MASTER_HEADERS,
    TargetMasterSchemaError,
    read_target_master_csv,
    validate_target_master,
)

ERRORS_FILENAME = "target_master_validation_errors.csv"
VALIDATED_FILENAME = "target_master_validated.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check a local TARGET_MASTER CSV. Writes a local error report and, only when every "
            "row passes, a validated copy with exact string values. Never contacts Google Sheets."
        )
    )
    parser.add_argument("csv_path", type=Path, help="Local CSV with the TARGET_MASTER header row.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "output",
        help="Directory for local reports (default: data/output, ignored by Git).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = validate_target_master(read_target_master_csv(args.csv_path))
    except (FileNotFoundError, UnicodeDecodeError, TargetMasterSchemaError) as exc:
        print(f"TARGET_MASTER check failed: {exc}", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    result.errors.reindex(columns=TARGET_ERROR_COLUMNS).to_csv(
        args.output_dir / ERRORS_FILENAME, index=False
    )
    validated_path = args.output_dir / VALIDATED_FILENAME
    # Remove a previous validated copy so a failing check never leaves stale "valid" output.
    validated_path.unlink(missing_ok=True)

    # Counts only: row values stay in the local report, never on the console.
    print(f"Rows checked: {len(result.rows)}; rows passing: {len(result.valid_rows)}.")
    if not result.is_valid:
        for code, count in result.errors["error_code"].value_counts().sort_index().items():
            print(f"  {code}: {count}")
        print(
            f"TARGET_MASTER check failed; see {args.output_dir / ERRORS_FILENAME}.",
            file=sys.stderr,
        )
        return 1
    result.valid_rows[["source_row_number", *TARGET_MASTER_HEADERS]].to_csv(
        validated_path, index=False
    )
    print(f"TARGET_MASTER check passed; validated copy saved to {validated_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
