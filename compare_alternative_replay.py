#!/usr/bin/env python3
"""Before/after comparison of two offline replays of one weekly price report.

``--before`` is a report folder written with ``track_prices.py --no-alternatives`` (matching only),
``--after`` the same inputs with close alternatives. Writes ``before_after_cells.csv`` (one row per
target and retailer), ``before_after_targets.csv`` (matching and alternative minimums per target)
and ``before_after_summary.json``. Reads local files only. Exit status 1 when a matching result
changed: matching cells, matching minimums and matching counts must be identical.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.price_tracking import RETAILERS  # noqa: E402

MATCHING_CELL_FIELDS = ("Price", "Price_Status", "Offer_URL", "Stock", "Observed_At",
                        "Match_Basis")
MATCHING_MINIMUMS = (
    "Comparable_Retailer_Count", "Lowest_Price", "Lowest_Price_Retailers",
    "Lowest_Price_Offer_URL", "Lowest_Price_Observed_At", "Lowest_Price_Stock",
    "Lowest_In_Stock_Price", "Lowest_In_Stock_Price_Retailers", "Lowest_In_Stock_Offer_URL",
    "Comparison_Basis",
)


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--discovery-summary", type=Path,
                        help="discovery_reassessment_summary.json to include (optional).")
    args = parser.parse_args(argv)
    before = {r["target_id"]: r for r in read(args.before / "weekly_price_comparison.csv")}
    after = {r["target_id"]: r for r in read(args.after / "weekly_price_comparison.csv")}
    evaluations = read(args.after / "alternative_evaluations.csv")
    alternatives = read(args.after / "weekly_price_alternatives.csv")
    assert before.keys() == after.keys(), "the replays cover different targets"

    cells, targets = [], []
    changed_matching: list[str] = []
    category: Counter[str] = Counter()
    retailer_categories: dict[str, Counter[str]] = {code: Counter() for code, _ in RETAILERS}
    resolved: Counter[str] = Counter()
    for target_id in sorted(before, key=lambda t: int(after[t]["rank"])):
        b, a = before[target_id], after[target_id]
        for column in MATCHING_MINIMUMS:
            if b[column] != a[column]:
                changed_matching.append(f"{target_id} {column}")
        for code, _ in RETAILERS:
            tag = a.get(f"{code}_Comparison_Tag", "")
            shown_alt = a[f"{code}_Price_Status"] == "ALTERNATIVE_SELECTED"
            if not shown_alt:
                for field in MATCHING_CELL_FIELDS:
                    if b[f"{code}_{field}"] != a[f"{code}_{field}"]:
                        changed_matching.append(f"{target_id} {code}_{field}")
            kind = ("MATCH" if a[f"{code}_Price_Status"] == "SELECTED" else
                    tag if shown_alt else "")
            if kind:
                key = "MATCH" if kind == "MATCH" else kind.split(" · ")[0]
                category[key] += 1
                retailer_categories[code][key] += 1
            if shown_alt:
                resolved[b[f"{code}_Price_Status"]] += 1
            cells.append({
                "rank": a["rank"], "target_id": target_id, "retailer": code,
                "before_status": b[f"{code}_Price_Status"], "before_price": b[f"{code}_Price"],
                "after_status": a[f"{code}_Price_Status"], "after_price": a[f"{code}_Price"],
                "after_tag": tag, "alternative_offers_in_pool": a[f"{code}_Alternative_Offers"],
                "differences": a[f"{code}_Alternative_Differences"],
                "unknown_fields": a[f"{code}_Alternative_Unknowns"],
                "before_reason": b[f"{code}_Reason"][:160],
            })
        targets.append({
            "rank": a["rank"], "target_id": target_id,
            "matching_lowest_before": b["Lowest_Price"], "matching_lowest_after": a["Lowest_Price"],
            "matching_in_stock_before": b["Lowest_In_Stock_Price"],
            "matching_in_stock_after": a["Lowest_In_Stock_Price"],
            "matching_retailers_before": b["Comparable_Retailer_Count"],
            "matching_retailers_after": a["Comparable_Retailer_Count"],
            "alternative_retailers": a["Alternative_Retailer_Count"],
            "retailers_shown": a["Comparison_Retailer_Count"],
            "lowest_alternative": a["Lowest_Alternative_Price"],
            "lowest_alternative_retailers": a["Lowest_Alternative_Price_Retailers"],
            "lowest_alternative_tag": a["Lowest_Alternative_Price_Tag"],
            "lowest_alternative_in_stock": a["Lowest_Alternative_In_Stock_Price"],
            "lowest_alternative_in_stock_retailers": a[
                "Lowest_Alternative_In_Stock_Price_Retailers"],
        })
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("before_after_cells.csv", cells), ("before_after_targets.csv", targets)):
        with (args.output_dir / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    status = Counter(r["status"] for r in evaluations)
    reasons: Counter[str] = Counter()
    for row in evaluations:
        if row["status"] in {"RECHECK", "EXCLUDED_TOO_MANY_DIFFERENCES"}:
            for reason in filter(None, row["reasons"].split(";")):
                reasons[f"{row['status']}:{reason}"] += 1
    summary = {
        "matching_results_changed": changed_matching,
        "cells_by_category_after": dict(category),
        "cells_by_category_and_retailer_after": {
            code: dict(counter) for code, counter in retailer_categories.items()},
        "targets_with_a_matching_price": sum(
            1 for t in targets if t["matching_retailers_after"] != "0"),
        "targets_with_an_alternative_price": sum(
            1 for t in targets if t["alternative_retailers"] != "0"),
        "targets_with_a_price_of_either_kind": sum(
            1 for t in targets if t["retailers_shown"] != "0"),
        "targets_with_no_matching_price_that_now_show_an_alternative": sum(
            1 for t in targets if t["matching_retailers_after"] == "0"
            and t["retailers_shown"] != "0"),
        "cells_resolved_by_an_alternative_by_earlier_status": dict(resolved),
        "alternative_associations": len(alternatives),
        "alternative_associations_by_tag": dict(Counter(
            r["alternative_tag"] for r in alternatives)),
        "alternative_associations_displayed": sum(
            1 for r in alternatives if r["displayed_in_comparison"] == "TRUE"),
        "alternative_associations_hidden_by_a_match": sum(
            1 for r in alternatives if r["retailer_has_matching_offer"] == "TRUE"),
        "evaluations_by_status": dict(status),
        "still_uncertain_or_excluded_by_reason": dict(reasons.most_common()),
        "targets_with_a_lower_alternative_than_matching_minimum": sum(
            1 for t in targets if t["lowest_alternative"] and t["matching_lowest_after"]
            and float(t["lowest_alternative"]) < float(t["matching_lowest_after"])),
    }
    if args.discovery_summary and args.discovery_summary.is_file():
        summary["discovery_reassessment"] = json.loads(
            args.discovery_summary.read_text(encoding="utf-8"))
    (args.output_dir / "before_after_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Matching results changed: {len(changed_matching)}. Cells after: {dict(category)}. "
          f"Targets with a matching price {summary['targets_with_a_matching_price']}, with an "
          f"alternative price {summary['targets_with_an_alternative_price']}, either "
          f"{summary['targets_with_a_price_of_either_kind']}.")
    return 0 if not changed_matching else 1


if __name__ == "__main__":
    raise SystemExit(main())
