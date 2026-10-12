#!/usr/bin/env python3
"""Validate TARGET_MASTER, SOURCE_MODEL_MAP, and MATCH_REVIEW snapshots or tabs (read-only)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.config import Settings
from laptop_tracker.sheets_source import (
    TARGET_TABLE_TABS,
    SheetsSourceError,
    read_target_table_values,
)
from laptop_tracker.supporting_tables import (
    mapped_source_ids_under_review,
    validate_match_review,
    validate_source_model_map,
)
from laptop_tracker.target_master import (
    TARGET_ERROR_COLUMNS,
    TargetMasterSchemaError,
    read_table_snapshot_csv,
    validate_target_master,
)

ROOT = Path(__file__).resolve().parent
TEMPLATES = {
    "TARGET_MASTER": "config/target_master_template.csv",
    "SOURCE_MODEL_MAP": "config/source_model_map_template.csv",
    "MATCH_REVIEW": "config/match_review_template.csv",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check the Phase 5 target tables. Reads local CSV snapshots, or with --from-sheets "
            "reads the master Google Sheet tabs using the read-only scope. Writes local error "
            "reports and, per passing table, a validated copy. Prints counts only. Never writes "
            "to, creates, or modifies any Google Sheet, and never promotes review rows."
        )
    )
    parser.add_argument("--target-master", type=Path, help="Local TARGET_MASTER CSV snapshot.")
    parser.add_argument(
        "--source-model-map",
        type=Path,
        help="Local SOURCE_MODEL_MAP CSV snapshot (requires --target-master).",
    )
    parser.add_argument("--match-review", type=Path, help="Local MATCH_REVIEW CSV snapshot.")
    parser.add_argument(
        "--from-sheets",
        action="store_true",
        help="Read all three tabs from the configured spreadsheet instead of local CSVs.",
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
        parser.error("use either --from-sheets or local CSV paths, not both")
    if not args.from_sheets and not any(local):
        parser.error("supply at least one local CSV path, or --from-sheets")
    if args.source_model_map and not args.target_master:
        parser.error("--source-model-map requires --target-master to check target_id references")
    return args


def load_tables(args: argparse.Namespace) -> dict[str, list[list[str]] | None]:
    if args.from_sheets:
        return read_target_table_values(Settings.from_env(), TARGET_TABLE_TABS)
    paths = {
        "TARGET_MASTER": args.target_master,
        "SOURCE_MODEL_MAP": args.source_model_map,
        "MATCH_REVIEW": args.match_review,
    }
    return {name: read_table_snapshot_csv(path) for name, path in paths.items() if path}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        tables = load_tables(args)
    except (FileNotFoundError, UnicodeDecodeError, SheetsSourceError, ValueError) as exc:
        print(f"Target table check failed safely: {exc}", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    passed = True
    results = {}
    for name, values in tables.items():
        prefix = name.lower()
        # Remove a previous validated copy so a failing check never leaves stale "valid" output.
        (args.output_dir / f"{prefix}_validated.csv").unlink(missing_ok=True)
        if values is None:
            print(
                f"{name}: worksheet not found. Create it manually with the {TEMPLATES[name]} "
                "headers; this command never creates tabs.",
                file=sys.stderr,
            )
            passed = False
            continue
        try:
            if name == "TARGET_MASTER":
                result = validate_target_master(values)
            elif name == "SOURCE_MODEL_MAP":
                if results.get("TARGET_MASTER") is None:
                    print(
                        "SOURCE_MODEL_MAP: not checked because TARGET_MASTER could not be read.",
                        file=sys.stderr,
                    )
                    passed = False
                    continue
                result = validate_source_model_map(values, results["TARGET_MASTER"])
            else:
                result = validate_match_review(values)
        except TargetMasterSchemaError as exc:
            print(f"{name} check failed: {exc}", file=sys.stderr)
            passed = False
            continue
        results[name] = result
        passed = report(name, result, args.output_dir) and passed

    if "SOURCE_MODEL_MAP" in results and "MATCH_REVIEW" in results:
        overlap = mapped_source_ids_under_review(
            results["SOURCE_MODEL_MAP"], results["MATCH_REVIEW"]
        )
        print(f"Mapped source_model_ids still open in MATCH_REVIEW (informational): {overlap}")
    print("Target table check " + ("passed." if passed else "failed."))
    return 0 if passed else 1


def report(name: str, result, output_dir: Path) -> bool:
    """Write local reports and print counts and codes only; row values stay in the files."""
    prefix = name.lower()
    errors_path = output_dir / f"{prefix}_validation_errors.csv"
    result.errors.reindex(columns=TARGET_ERROR_COLUMNS).to_csv(errors_path, index=False)
    print(f"{name}: rows checked {len(result.rows)}; rows passing {len(result.valid_rows)}.")
    for key, count in getattr(result, "info", {}).items():
        print(f"  info {key}: {count}")
    if not result.is_valid:
        for code, count in result.errors["error_code"].value_counts().sort_index().items():
            print(f"  {code}: {count}")
        print(f"{name} failed; see {errors_path}.", file=sys.stderr)
        return False
    validated_path = output_dir / f"{prefix}_validated.csv"
    result.valid_rows.to_csv(validated_path, index=False)
    print(f"{name} passed; validated copy saved to {validated_path}.")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
