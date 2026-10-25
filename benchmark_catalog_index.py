"""Compare saved index results with all bounded native query tiers, without matching.

This deliberately gives native search every planned tier, even if the production
search job would stop early. It is a discovery benchmark, not a Top 100 rerun.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import re
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.catalog_index import CatalogIndex, product_url, queries, response_record
from laptop_tracker.catalog_leads import SitemapEntry, catalog_leads, lead_targets
from laptop_tracker.product_names import default_registry
from laptop_tracker.retailer_search import SEARCH_METHODS, search_queries
from laptop_tracker.scraping import Fetcher
from search_catalog_index import native_search, write_reports


def measure_cached_search(run, output, repeats):
    started = time.perf_counter()
    index = CatalogIndex.from_run(Path(run["index_run"]),
                                 feed_runs=tuple(Path(p) for p in run.get("feed_runs", [])))
    load_seconds = time.perf_counter() - started
    planned = [queries(case, 1)[0] for case in run["cases"]]
    started = time.perf_counter()
    calls = sum(index.search(planned[i % len(planned)]).search_calls for i in range(repeats))
    performance = {"indexed_product_urls": len(index.entries), "query_count": repeats,
                   "cold_load_seconds": round(load_seconds, 6),
                   "query_seconds": round(time.perf_counter() - started, 6),
                   "query_network_requests": calls}
    (output / "performance.json").write_text(json.dumps(performance, indent=2) + "\n")
    rows = [{"target_id": c["case_id"], "model": c["model"], "brand": c.get("brand", "")}
            for c in run["cases"]]
    targets = lead_targets(rows, {r["target_id"]: default_registry().kind(r["brand"], r["model"])
                                 for r in rows})
    by_retailer = {}
    for retailer in {c["retailer_id"] for c in run["cases"]}:
        entries = [(e["sitemap"], SitemapEntry(e["url"], e["lastmod"], e["title"]))
                   for e in index.entries if e["retailer_id"] == retailer]
        by_retailer[retailer] = {lead["url"] for lead in catalog_leads(
            retailer, entries, targets, supplied_on=index.retrieved_at)}
    comparison = []
    for case in run["cases"]:
        expected = product_url(case["expected_url"], case["retailer_id"])
        native = {lead["url"] for r in run["searches"][case["case_id"]]["native"]
                  if r["outcome"] == "RESULTS" for lead in r["results"]}
        old = expected in by_retailer[case["retailer_id"]]
        comparison.append({"case_id": case["case_id"], "existing_catalog_expected_found": old,
                           "native_plus_existing_catalog_found": old or expected in native})
    with (output / "existing_catalog_baseline.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(comparison[0]))
        writer.writeheader()
        writer.writerows(comparison)
    print(json.dumps({"performance": performance,
                      "existing_catalog_expected_found": sum(
                          c["existing_catalog_expected_found"] for c in comparison),
                      "native_plus_existing_catalog_found": sum(
                          c["native_plus_existing_catalog_found"] for c in comparison)}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--native-run", type=Path,
                      help="Reuse a recorded native scan on the identical declared cohort")
    parser.add_argument("--repeat-queries", type=int, default=10000)
    args = parser.parse_args(argv)
    if not 1 <= args.repeat_queries <= 100000:
        parser.error("Repeat query count must be between 1 and 100000")
    run = copy.deepcopy(json.loads((args.run / "run.json").read_text(encoding="utf-8")))
    if not 1 <= len(run["cases"]) <= 100:
        parser.error("The saved cohort must contain between 1 and 100 cases")
    if any(not case.get("expected_url") for case in run["cases"]):
        parser.error("Every benchmark case needs a predeclared expected_url")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Output must be empty")
    args.output.mkdir(parents=True, exist_ok=True)
    plans = {}
    for case in run["cases"]:
        amounts = re.findall(r"(\d+)\s*(GB|TB)", case.get("specs", ""), re.I)
        core = {}
        if len(amounts) == 2:
            core = {key: str(int(value) * (1000 if unit.upper() == "TB" else 1))
                    for key, (value, unit) in zip(
                        ("ram_capacity_gb", "storage_capacity_gb"), amounts, strict=True)}
        planned = search_queries(
            case["case_id"], case["retailer_id"], model=case["model"], part_number="",
            brand=case.get("brand", ""), series=case.get("family", ""), core=core,
            kind=default_registry().kind(case.get("brand", ""), case["model"]),
        )
        plans[case["case_id"]] = list(dict.fromkeys(query.text for query in planned))[:7]
    (args.output / "native_plan.json").write_text(json.dumps(plans, indent=2) + "\n")
    if not args.live and not args.native_run:
        print(f"Preview: {sum(map(len, plans.values()))} native query invocations; no requests.")
        return 0
    # Preserve response bytes and observation times for self-contained offline replay.
    for record in run["pages"].values():
        if record.get("body_file"):
            body_file = Path(record["body_file"])
            if body_file.is_absolute() or ".." in body_file.parts:
                raise ValueError("Unsafe saved response path")
            shutil.copyfile(args.run / body_file, args.output / body_file)
    if args.native_run:
        original = json.loads((args.native_run / "run.json").read_text(encoding="utf-8"))
        if original["cases"] != run["cases"]:
            raise ValueError("Native evidence cohort differs")
        for case in run["cases"]:
            records = original["searches"][case["case_id"]]["native"]
            scopes = {f"site:{SEARCH_METHODS[case['retailer_id']].host}",
                      f"site:{case['retailer_id']}"}
            if (any(record["query"].partition(" ")[0] not in scopes for record in records)
                    or [record["query"].partition(" ")[2] for record in records]
                    != plans[case["case_id"]]):
                raise ValueError("Native evidence query plan differs")
            run["searches"][case["case_id"]]["native"] = records
        run["native_evidence"] = str(args.native_run.resolve())
        run["native_expanded_started_at"] = original["native_expanded_started_at"]
        run["native_expanded_finished_at"] = original["native_expanded_finished_at"]
        (args.output / "run.json").write_text(json.dumps(run, indent=2) + "\n")
        summary = write_reports(args.output, run)
        print(json.dumps({k: v for k, v in summary.items() if k != "per_case"}))
        measure_cached_search(run, args.output, args.repeat_queries)
        return 0
    run["native_expanded_started_at"] = datetime.now(UTC).isoformat()
    run["original_evidence"] = str(args.run.resolve())
    fetcher = Fetcher()
    for case in run["cases"]:
        records = []
        for words in plans[case["case_id"]]:
            query = f"site:{SEARCH_METHODS[case['retailer_id']].host} {words}"
            records.append(response_record(native_search(query, case["retailer_id"], fetcher)))
        run["searches"][case["case_id"]]["native"] = records
        (args.output / "run.json").write_text(json.dumps(run, indent=2) + "\n")
        print(f"Completed {case['case_id']}: {len(records)} native tiers", flush=True)
    run["native_expanded_finished_at"] = datetime.now(UTC).isoformat()
    (args.output / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    summary = write_reports(args.output, run)
    print(json.dumps({k: v for k, v in summary.items() if k != "per_case"}))
    measure_cached_search(run, args.output, args.repeat_queries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
