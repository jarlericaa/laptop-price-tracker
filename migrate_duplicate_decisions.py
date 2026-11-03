#!/usr/bin/env python3
"""Convert row-number duplicate decisions to content-based decisions (read-only on Sheets).

A recorded decision is migrated only where the previously reviewed records and the selected
winner can be established from the current raw rows (see ``migrate_legacy_decisions``); anything
else is reported and left for review. ``--preview`` (default) writes the proposal and report to
``--output-dir``; ``--write`` first backs up the decision file, then replaces it.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.config import Settings
from laptop_tracker.data_quality import (
    DUPLICATE_DECISION_COLUMNS,
    load_legacy_decisions,
    migrate_legacy_decisions,
    validate_rows,
)
from laptop_tracker.schema import normalize_raw_values
from laptop_tracker.sheets_source import read_raw_values

NOTE = (
    "Migrated {date} to the content-based decision format (fingerprint content-v1): the "
    "previously reviewed records and the selected record were established from their content; "
    "original reviewer and review date retained."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="Write a proposal only (default).")
    mode.add_argument("--write", action="store_true", help="Back up and replace the decisions.")
    parser.add_argument("--decisions", default="config/duplicate_decisions.csv")
    parser.add_argument(
        "--raw-csv", help="A CSV dump of the raw tab (default: read it from Sheets)."
    )
    parser.add_argument("--output-dir", default="data/output/duplicate_decision_migration")
    parser.add_argument("--backup-dir", default="data/backups")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="With --write, migrate the established decisions even if others are not.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    decisions_path = Path(args.decisions)
    legacy = load_legacy_decisions(decisions_path)
    if args.raw_csv:
        raw = pd.read_csv(args.raw_csv, dtype=str, keep_default_na=False)
        values = [list(raw.columns), *raw.values.tolist()]
    else:
        _title, _worksheet, values = read_raw_values(Settings.from_env())
    valid = validate_rows(normalize_raw_values(list(values), "migration")).valid_rows
    today = datetime.now(UTC).date().isoformat()
    migrated, report = migrate_legacy_decisions(legacy, valid, note=NOTE.format(date=today))

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    migrated.reindex(columns=DUPLICATE_DECISION_COLUMNS).to_csv(
        output / "duplicate_decisions_content_based.csv", index=False
    )
    report.to_csv(output / "migration_report.csv", index=False)
    counts = report["outcome"].value_counts().to_dict()
    print(f"Legacy decisions: {len(legacy)}; outcomes: {counts}. Proposal in {output}.")
    unmigrated = int((report["outcome"] != "MIGRATED").sum())
    if not args.write:
        print("Preview only: nothing was changed.")
        return 0
    if unmigrated and not args.allow_partial:
        print(
            f"{unmigrated} decision(s) could not be established; nothing was written "
            "(use --allow-partial to migrate the rest).",
            file=sys.stderr,
        )
        return 1
    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"duplicate_decisions.{stamp}.row_number_layout.csv"
    shutil.copy2(decisions_path, backup)
    migrated.reindex(columns=DUPLICATE_DECISION_COLUMNS).to_csv(
        decisions_path, index=False, lineterminator="\n"
    )
    print(f"Backed up the row-number file to {backup}; wrote {len(migrated)} decision(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
