#!/usr/bin/env python3
"""Audit the published Top 100 verified configurations against their inputs (read-only)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.sheets_source import SheetsSourceError
from laptop_tracker.supporting_tables import validate_source_model_map
from laptop_tracker.target_master import TargetMasterSchemaError, validate_target_master
from laptop_tracker.verified_top import (
    CONFIGURATION_COLUMNS,
    DEFAULT_TOP_N,
    LEDGER_COLUMNS,
    OUTPUT_FILES,
    VerifiedRankingInputError,
    audit_verified_ranking,
    load_catalog_tables,
    read_csv_rows,
    read_sales_snapshot,
)

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "output"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check a published Top 100 verified configurations run. The checks cover unique "
            "target IDs; ACTIVE CONFIGURATION targets with complete core identities matching "
            "TARGET_MASTER; contributors that are eligible, present in MTD, and mapped to their "
            "target; GMV totals equal to contributor sums; deterministic order; and the top list "
            "being the head of the full ranking. Also checks that the catalog is the one the run "
            "used. Read-only; prints issues only. The source-level coverage audit "
            "(audit_top_100_coverage.py) is separate."
        )
    )
    parser.add_argument("--run-dir", type=Path, default=OUTPUT / "verified_top_100")
    parser.add_argument("--ranking", type=Path, default=OUTPUT / "eligible_model_ranking.csv")
    parser.add_argument(
        "--target-master", type=Path, default=OUTPUT / "target_master_validated.csv"
    )
    parser.add_argument(
        "--source-model-map", type=Path, default=OUTPUT / "source_model_map_validated.csv"
    )
    parser.add_argument("--match-review", type=Path, default=OUTPUT / "match_review_validated.csv")
    parser.add_argument("--from-sheets", action="store_true")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        configurations = read_csv_rows(
            args.run_dir / OUTPUT_FILES["ranking"], CONFIGURATION_COLUMNS, "Configuration ranking"
        )
        top = read_csv_rows(args.run_dir / OUTPUT_FILES["top"], CONFIGURATION_COLUMNS, "Top list")
        ledger = read_csv_rows(
            args.run_dir / OUTPUT_FILES["ledger"], LEDGER_COLUMNS, "Contribution ledger"
        )
        snapshot = read_sales_snapshot(args.ranking)
        tables, fingerprints = load_catalog_tables(
            {
                "TARGET_MASTER": args.target_master,
                "SOURCE_MODEL_MAP": args.source_model_map,
                "MATCH_REVIEW": args.match_review,
            },
            from_sheets=args.from_sheets,
        )
        target_master = validate_target_master(tables["TARGET_MASTER"])
        source_model_map = validate_source_model_map(tables["SOURCE_MODEL_MAP"], target_master)
    except (
        FileNotFoundError,
        UnicodeDecodeError,
        KeyError,
        SheetsSourceError,
        TargetMasterSchemaError,
        VerifiedRankingInputError,
    ) as exc:
        print(f"Verified configuration audit stopped: {exc}", file=sys.stderr)
        return 1

    issues = []
    if not target_master.is_valid or not source_model_map.is_valid:
        issues.append("TARGET_MASTER or SOURCE_MODEL_MAP did not pass validation.")
    used = {row["target_master_sha256"] for row in configurations}
    if used and used != {fingerprints["TARGET_MASTER"][1]}:
        issues.append("TARGET_MASTER differs from the catalog this run was built from.")
    if {row["sales_run_id"] for row in configurations} - {snapshot.run_id}:
        issues.append("The run was built from a different sales snapshot.")
    issues += audit_verified_ranking(
        configurations,
        top,
        ledger,
        snapshot,
        target_master,
        source_model_map,
        top_n=args.top_n,
    )
    print(
        f"Configurations audited: {len(configurations)}; selected: {len(top)}; "
        f"ledger sources: {len(ledger)}."
    )
    for issue in issues:
        print(f"  ISSUE: {issue}")
    print("Verified configuration audit " + ("failed." if issues else "passed."))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
