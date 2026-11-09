#!/usr/bin/env python3
"""Seed a local MATCH_REVIEW candidate file from the Phase 4 Top 100 (no Google access)."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.supporting_tables import (
    SeedError,
    build_match_review_candidates,
    validate_match_review,
)
from laptop_tracker.target_master import read_target_master_csv

ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create one NEW MATCH_REVIEW candidate per distinct Top 100 model_id. Specifications, "
            "source URLs, approvals, target IDs, and mappings are left blank for human research. "
            "Never overwrites an existing file and never contacts Google Sheets."
        )
    )
    parser.add_argument(
        "--top-100",
        type=Path,
        default=ROOT / "data" / "output" / "top_100_targets.csv",
        help="Phase 4 Top 100 CSV (default: data/output/top_100_targets.csv).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "review" / "match_review_candidates.csv",
        help="New candidate CSV (default: data/review/match_review_candidates.csv; Git-ignored).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.output.exists():
        print(
            f"Refusing to overwrite {args.output}; it may contain review work. "
            "Choose a new --output path.",
            file=sys.stderr,
        )
        return 1
    if not args.top_100.is_file():
        print(
            f"Top 100 output not found at {args.top_100}. Run "
            "`python scripts/run_phase1.py --dry-run` "
            "first. No candidate file was written.",
            file=sys.stderr,
        )
        return 1
    try:
        top_100 = read_target_master_csv(args.top_100)
        candidates = build_match_review_candidates(top_100)
    except (UnicodeDecodeError, SeedError) as exc:
        print(f"Seeding failed safely: {exc}", file=sys.stderr)
        return 1

    # Self-check against the MATCH_REVIEW contract before anything is written.
    check = validate_match_review(candidates)
    if not check.is_valid:
        codes = ", ".join(sorted(set(check.errors["error_code"])))
        print(f"Seeding failed safely: candidates did not validate ({codes}).", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(candidates)
    source_rows = sum(1 for row in top_100[1:] if any(str(cell).strip() for cell in row))
    # Counts only: listing names stay in the local file.
    print(f"Top 100 rows read: {source_rows}; NEW candidates written: {len(candidates) - 1}.")
    print(f"Candidate file saved to {args.output}. No TARGET_MASTER or SOURCE_MODEL_MAP rows made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
