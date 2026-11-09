#!/usr/bin/env python3
"""Reconcile name-identified catalog targets with their source evidence (local, read-only).

Writes a reconciliation CSV (supported corrections and unresolved entries), a corrected local
TARGET_MASTER snapshot and selected list for the supported corrections only (DIAGNOSTIC: the
owner applies them in the Google Sheet), and an owner migration report. Nothing is written to
Google Sheets, the catalog inputs, the mappings or the approvals.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from laptop_tracker.catalog_reconciliation import (  # noqa: E402
    RECONCILIATION_COLUMNS,
    SUPPORTED_CORRECTION,
    UNRESOLVED_STATUSES,
    apply_corrections,
    reconcile,
)
from laptop_tracker.product_names import default_registry  # noqa: E402


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _write(path: Path, columns, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target-master", type=Path, required=True,
                        help="Validated TARGET_MASTER snapshot (target_master_validated.csv).")
    parser.add_argument("--selected-list", type=Path, required=True,
                        help="top_100_verified_configurations.csv")
    parser.add_argument("--contributions", type=Path, required=True,
                        help="verified_configuration_contributions.csv")
    parser.add_argument("--match-results", type=Path, action="append", default=[],
                        help="Retailer match_results.csv (corroboration and leads only).")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    catalog_columns, catalog = _read(args.target_master)
    selected_columns, selected = _read(args.selected_list)
    _, contributions = _read(args.contributions)
    offers = [row for path in args.match_results for row in _read(path)[1]]
    result = reconcile(catalog, selected, contributions, default_registry(), offers)
    corrected_catalog, corrected_selected = apply_corrections(
        catalog, selected, result.corrections)

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "catalog_reconciliation.csv", RECONCILIATION_COLUMNS, result.rows)
    _write(out / "target_master_reconciled_DIAGNOSTIC.csv", catalog_columns, corrected_catalog)
    _write(out / "selected_list_reconciled_DIAGNOSTIC.csv", selected_columns, corrected_selected)
    statuses = Counter(row["status"] for row in result.rows)
    lines = [
        "# Catalog reconciliation: owner migration report",
        "",
        "Local proposal only: no Google Sheet, mapping or approval was changed. Apply each "
        "supported correction in TARGET_MASTER (same target_id), then re-export and rebuild "
        "the selected list.",
        "",
        "| Status | Targets |",
        "|---|---|",
        *[f"| {status} | {count} |" for status, count in sorted(statuses.items())],
        "",
        "## Supported corrections (applied in the DIAGNOSTIC snapshot)",
        "",
        "| Target | Original model | Proposed model | Source references | Flags |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['target_id']} | {r['original_model']} | {r['proposed_model']} | "
        f"{r['source_references']} | {r['flags'] or '-'} |"
        for r in result.rows if r["status"] == SUPPORTED_CORRECTION
    ]
    lines += [
        "", "## Unresolved (owner action needed)", "",
        "INSUFFICIENT_EVIDENCE means unresolved under the current evidence and parser, not "
        "permanently unpriceable: the parser recognizes hyphenated manufacturer model codes and "
        "the registered product names only. Some source listings already carry typed "
        "manufacturer identifiers or generation-specific names that a reviewer can confirm; "
        "others genuinely mix products or generations. Each needs individual evidence review. "
        "A duplicate proposal requires the same complete configuration (code, screen, CPU "
        "family, RAM, storage) and no material secondary difference; nothing is merged "
        "automatically.",
        "",
        "| Target | Model | Status | Conflicts / flags | Action |", "|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['target_id']} | {r['original_model']} | {r['status']} | "
        f"{' | '.join(filter(None, (r['conflicts'], r['flags']))) or '-'} | {r['action']} |"
        for r in result.rows if r["status"] in UNRESOLVED_STATUSES
    ]
    (out / "owner_migration_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = {
        "inputs": {name: str(getattr(args, name)) for name in
                   ("target_master", "selected_list", "contributions")},
        "match_results": [str(p) for p in args.match_results],
        "statuses": dict(sorted(statuses.items())),
        "supported_corrections": sorted(result.corrections),
        "sheets_written": False,
    }
    (out / "reconciliation_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary["statuses"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
