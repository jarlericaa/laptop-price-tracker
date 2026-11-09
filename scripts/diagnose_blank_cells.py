#!/usr/bin/env python3
"""Offline per-cell diagnostics of a weekly report: why each retailer cell is priced or blank."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.cell_diagnostics import (
    AUDIT_COLUMNS,
    DIAGNOSTIC_COLUMNS,
    STAGES,
    diagnose_cells,
    overlay_audit,
    stage_counts,
)
from laptop_tracker.retailer_search import SEARCH_METHODS


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(
        "Explain every configuration x retailer cell of a track_prices.py report: the furthest "
        "stage it reached (not searched, zero results, no relevant result, candidate not "
        "fetched, fetch error, parsed without price, variant unresolved, match review, "
        "configuration mismatch, price known but not comparable, comparable and published), "
        "with queries, limits, request outcomes, observation IDs and the price-selection "
        "reason. Observed listing prices and owner-policy comparable prices are separate "
        "columns. Reads saved files only: no request, no store, no Sheets."))
    parser.add_argument("--report-dir", type=Path, required=True,
                        help="track_prices.py report folder (weekly_price_comparison.csv, ...).")
    parser.add_argument("--discovery-dir", type=Path,
                        help="discover_products.py folder of the same week (search_jobs.csv, "
                             "search_requests.csv, candidates.csv, collection/).")
    parser.add_argument("--coverage", type=Path, help="coverage_report.py coverage.csv.")
    parser.add_argument("--target-master", type=Path,
                        help="Validated TARGET_MASTER CSV (model codes for the audit overlay).")
    parser.add_argument("--audit-leads", type=Path,
                        help="Audit claims CSV (columns 'Target ID', 'Retailer', 'Evidence "
                             "status', 'Retailer URL', ...). Claims are checked, never trusted.")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def read(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    comparison = read(args.report_dir / "weekly_price_comparison.csv")
    selections = read(args.report_dir / "weekly_price_selections.csv")
    if not comparison or not selections:
        print("The report folder has no comparison or selections.", file=sys.stderr)
        return 1
    discovery = args.discovery_dir
    collection = discovery / "collection" if discovery else None
    candidates = read(discovery / "candidates.csv" if discovery else None)
    results = read(collection / "match_results.csv" if collection else None)
    rows = diagnose_cells(
        comparison, selections,
        coverage=read(args.coverage),
        jobs=read(discovery / "search_jobs.csv" if discovery else None),
        requests=read(discovery / "search_requests.csv" if discovery else None),
        candidates=candidates, results=results,
        request_report=read(collection / "request_report.csv" if collection else None),
        search_methods={k: m.kind for k, m in SEARCH_METHODS.items()},
    )
    columns = list(DIAGNOSTIC_COLUMNS)
    summary: dict = {"cells": len(rows), "stages_by_retailer": stage_counts(rows),
                     "inputs": {k: str(v) for k, v in vars(args).items() if v is not None}}
    if args.audit_leads:
        models = {r["target_id"]: r["model"] for r in read(args.target_master)}
        rows = overlay_audit(
            rows, read(args.audit_leads),
            search_results=read(discovery / "search_results.csv" if discovery else None),
            candidates=candidates, results=results, models=models)
        columns += AUDIT_COLUMNS
        audited = [r for r in rows if r["audit_status"]]
        summary["audit"] = {
            "claims_matched_to_cells": len(audited),
            "by_status_and_stage": {
                status: dict(Counter(r["diagnostic_stage"] for r in audited
                                     if r["audit_status"] == status))
                for status in sorted({r["audit_status"] for r in audited})},
            "category_only_urls": sum(r["audit_url_kind"] == "CATEGORY_PAGE" for r in audited),
            "urls_returned_by_search": sum(r["audit_url_in_search_results"] == "TRUE"
                                           for r in audited),
            "urls_shared_by_several_targets": sorted({
                f"{r['target_id']}<->{r['audit_url_shared_with_targets']}"
                for r in audited if r["audit_url_shared_with_targets"]}),
        }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "cell_diagnostics.csv").open("w", newline="", encoding="utf-8") as h:
        writer = csv.DictWriter(h, fieldnames=columns)
        writer.writeheader()
        writer.writerows({c: r.get(c, "") for c in columns} for r in rows)
    (args.output_dir / "cell_diagnostics_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    table = summary["stages_by_retailer"]
    print("stage".ljust(36) + "".join(k[:14].rjust(15) for k in table))
    for stage in STAGES:
        print(stage.ljust(36) + "".join(str(table[k][stage]).rjust(15) for k in table))
    print(f"Wrote {len(rows)} cells to {args.output_dir / 'cell_diagnostics.csv'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
