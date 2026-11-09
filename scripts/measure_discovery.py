#!/usr/bin/env python3
"""Offline discovery/matching measurement against a labelled lead benchmark (2026-10-22).

Reads a benchmark (``tests/scraping_fixtures/discovery_benchmark/leads_v1.csv``), one weekly
report and its discovery folder, and writes measured baseline metrics. With ``--proposed`` it
also reports what the 2026-10-22 mechanisms *could* reach on the same evidence: identifier
spellings (planned where the complete code returned nothing), sitemap catalog leads (a URL whose
handle shows the complete code), and second-pass collection (relevant candidates skipped by the
per-round limit). Those are reachability estimates, never measured recall: only a live run shows
what the stores return. No request, store write or Sheets access.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.price_tracking import RETAILERS
from laptop_tracker.product_discovery import COLLECTABLE, url_shows_code

# Independent labels that mean "must not be accepted as an exact match for this target".
NEGATIVE = ("CONFLICT_CONFIRMED_BY_SAVED_PAGE", "ANOTHER_TARGET_EXACT", "CATEGORY_ONLY",
            "DIFFERENT_CONFIGURATION_BY_SAVED_PAGE", "UNEXPLAINED_SUFFIX")


def read(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def key(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--discovery-dir", type=Path, required=True)
    parser.add_argument("--proposed", action="store_true",
                        help="Add reachability estimates for the 2026-10-22 mechanisms.")
    parser.add_argument("--output", type=Path, required=True, help="JSON metrics file.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    leads = read(args.benchmark)
    disc = args.discovery_dir
    hits = {(r["target_id"], r["retailer_id"], key(r["product_url"]))
            for r in read(disc / "search_results.csv")}
    candidates = read(disc / "candidates.csv")
    listed = {(c["target_id"], c["retailer_id"], key(c["product_url"])) for c in candidates}
    results = defaultdict(list)
    for row in read(disc / "collection" / "match_results.csv"):
        results[key(row["offer_url"])].append(row)
    jobs = {(j["target_id"], j["retailer_id"]): j for j in read(disc / "search_jobs.csv")}
    requests = read(disc / "search_requests.csv")
    comparison = read(args.report_dir / "weekly_price_comparison.csv")
    summary = json.loads((disc / "discovery_summary.json").read_text(encoding="utf-8"))

    # Known-positive discovery recall: audit leads with a product-page URL (claims to check).
    positives = [lead for lead in leads if lead["audit_status"] and
                 lead["url_kind"] == "PRODUCT_PAGE"]
    recall: dict[str, dict[str, int]] = defaultdict(lambda: Counter())
    for lead in positives:
        k = (lead["target_id"], lead["retailer_id"], key(lead["url"]))
        found = k in hits or k in listed
        for bucket in (lead["retailer_id"], "TOTAL"):
            recall[bucket]["leads"] += 1
            recall[bucket]["found_by_discovery"] += found
    # Accepted-match safety on independently labelled negatives.
    accepted_negative = []
    for lead in leads:
        if not any(lead["independent_label"].startswith(n) for n in NEGATIVE):
            continue
        for row in results.get(key(lead["url"]), []):
            if row["match_status"] == "EXACT" and row["target_id"] == lead["target_id"]:
                accepted_negative.append(lead["lead_id"])
    human_review = [lead for lead in leads if lead["audit_status"] == "CANDIDATE_REVIEW_REQUIRED"]
    accepted_review = [lead["lead_id"] for lead in human_review
                       for row in results.get(key(lead["url"]), [])
                       if row["match_status"] == "EXACT" and row["target_id"] == lead["target_id"]]
    observations = [row for rows in results.values() for row in rows]
    exact = [row for row in observations if row["match_status"] == "EXACT"]
    cells = [(row, code) for row in comparison for code, _ in RETAILERS]
    published = Counter(retailer for row, code in cells for c, retailer in RETAILERS
                        if c == code and row[f"{code}_Price_Status"] == "SELECTED")
    started = datetime.fromisoformat(summary["started_at_utc"].replace("Z", "+00:00"))
    finished = summary.get("finished_at_utc") or summary.get("completed_at_utc")
    runtime = ((datetime.fromisoformat(finished.replace("Z", "+00:00")) - started)
               .total_seconds() if finished else None)
    metrics: dict = {
        "benchmark": {"file": str(args.benchmark), "leads": len(leads),
                      "case_types": dict(Counter(lead["case_type"] for lead in leads)),
                      "labels": dict(Counter(lead["independent_label"] for lead in leads))},
        "baseline": {
            "known_positive_discovery_recall": {k: dict(v) for k, v in recall.items()},
            "accepted_matches_on_independent_negative_labels": accepted_negative,
            "accepted_matches_on_human_review_items": {
                "items": len(human_review), "accepted_exact": accepted_review},
            "price_extraction_on_collected_pages": {
                "observations": len(observations),
                "with_price": sum(1 for row in observations if row.get("price")),
                "exact": len(exact),
                "exact_with_price": sum(1 for row in exact if row.get("price"))},
            "price_coverage": {"cells": len(cells), "published": sum(published.values()),
                               "by_retailer": dict(published)},
            "unresolved_cells": len(cells) - sum(published.values()),
            "requests_sent": summary.get("requests_sent"),
            "search_requests_sent": summary.get("search_requests_sent"),
            "runtime_seconds": runtime,
        },
    }
    if args.proposed:
        code_jobs_no_result = {
            (r["target_id"], r["retailer_id"]) for r in requests
            if r["tier"] == "1_FULL_MODEL_CODE" and r["outcome"] == "NO_RESULTS"
            and not any(x["tier"] == "1_FULL_MODEL_CODE" and x["outcome"] == "RESULTS"
                        and (x["target_id"], x["retailer_id"]) == (r["target_id"],
                                                                   r["retailer_id"])
                        for x in requests)}
        reach = defaultdict(lambda: Counter())
        for lead in positives:
            if (lead["target_id"], lead["retailer_id"], key(lead["url"])) in hits | listed:
                continue
            bucket = reach[lead["retailer_id"]]
            bucket["missed_by_baseline"] += 1
            if lead["identity_kind"] == "MODEL_CODE":
                if (lead["target_id"], lead["retailer_id"]) in code_jobs_no_result:
                    bucket["identifier_variants_would_run"] += 1
                if url_shows_code(lead["url"], lead["target_model"]):
                    bucket["catalog_lead_assignable_if_sitemap_lists_url"] += 1
        unsuitable = {k for k, j in jobs.items()
                      if j["discovery_outcome"] != "SUITABLE_OFFER_FOUND"}
        second = Counter()
        for c in candidates:
            if ((c["target_id"], c["retailer_id"]) in unsuitable
                    and c["decision"] == "NOT_SELECTED_LIMIT" and c["category"] in COLLECTABLE
                    and not c["configuration_difference"] and not c["collection_request_id"]):
                second[c["retailer_id"]] += 1
        metrics["proposed_reachability_unverified"] = {
            "note": "Estimates on saved evidence; a live run decides what stores return.",
            "missed_known_positives": {k: dict(v) for k, v in reach.items()},
            "extra_search_requests_identifier_variants_max": 2 * len(code_jobs_no_result),
            "model_code_jobs_whose_code_returned_nothing": len(code_jobs_no_result),
            "second_pass_eligible_candidates": dict(second),
            "catalog_requests_max": 5 * (2 + 5),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
