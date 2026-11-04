#!/usr/bin/env python3
"""Apply the standing owner rule "keep the latest sheet row" to quarantined duplicate conflicts.

Reads the local ``duplicate_review.csv`` and ``duplicate_resolution_log.csv`` that a Phase 1 run
wrote (no Sheets access). ``--preview`` (default) writes the proposal; ``--write`` backs up
``config/duplicate_decisions.csv`` and then replaces the decisions of the proposed keys. Keys the
rule does not cover (several conflict dates, records not locatable) are listed and left for review.
Model-identity conflicts are never handled here.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.data_quality import DUPLICATE_DECISION_COLUMNS, load_duplicate_decisions
from laptop_tracker.duplicate_policy import merge_decisions, propose_latest_row_decisions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="Write a proposal only (default).")
    mode.add_argument("--write", action="store_true", help="Back up and update the decisions.")
    parser.add_argument("--decisions", default="config/duplicate_decisions.csv")
    parser.add_argument("--review", default="data/output/duplicate_review.csv")
    parser.add_argument("--resolution-log", default="data/output/duplicate_resolution_log.csv")
    parser.add_argument("--output-dir", default="data/output/latest_row_decisions")
    parser.add_argument("--backup-dir", default="data/backups")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    review = pd.read_csv(args.review, dtype=str, keep_default_na=False)
    log = pd.read_csv(args.resolution_log, dtype=str, keep_default_na=False)
    today = datetime.now(UTC).date().isoformat()
    proposed, skipped = propose_latest_row_decisions(review, log, today)
    decisions_path = Path(args.decisions)
    existing = load_duplicate_decisions(decisions_path)
    merged = merge_decisions(existing, proposed)

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    proposed.to_csv(output / "proposed_decisions.csv", index=False)
    skipped.to_csv(output / "not_covered_by_rule.csv", index=False)
    print(
        f"Undecided keys: {len(proposed) + len(skipped)}; proposed KEEP_RECORD (latest sheet row): "
        f"{len(proposed)}; not covered by the rule: {len(skipped)}. Proposal in {output}."
    )
    if not args.write:
        print("Preview only: nothing was changed.")
        return 0
    if proposed.empty:
        print("No decisions to write.")
        return 0 if skipped.empty else 1
    backup_note = "no earlier decisions file, nothing to back up"
    if decisions_path.is_file():
        backup_dir = Path(args.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / f"duplicate_decisions.{stamp}.pre_latest_row.csv"
        shutil.copy2(decisions_path, backup)
        backup_note = f"backed up to {backup}"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    merged.reindex(columns=DUPLICATE_DECISION_COLUMNS).to_csv(
        decisions_path, index=False, lineterminator="\n"
    )
    print(f"{backup_note.capitalize()}; decisions file now has {len(merged)} row(s).")
    return 0 if skipped.empty else 1


if __name__ == "__main__":
    raise SystemExit(main())
