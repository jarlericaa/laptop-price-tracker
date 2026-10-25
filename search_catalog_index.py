"""Search a cached retailer catalog; optionally fetch pages or benchmark native search."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker.catalog_index import (
    CatalogIndex,
    DiscoveryError,
    SearchResponse,
    fetch_product,
    inspect_html,
    leads,
    product_url,
    queries,
    response_record,
)
from laptop_tracker.retailer_search import SEARCH_METHODS
from laptop_tracker.scraping import Fetcher, FetchError, ScrapeRequest


def read_cases(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    seen = set()
    if not rows:
        raise DiscoveryError("No cases supplied")
    for row in rows:
        if not row.get("case_id") or row["case_id"] in seen:
            raise DiscoveryError("Case IDs must be nonblank and unique")
        seen.add(row["case_id"])
        if row.get("retailer_id") not in SEARCH_METHODS:
            raise DiscoveryError("Unknown retailer")
        queries(row)
        if row.get("expected_url"):
            product_url(row["expected_url"], row["retailer_id"])
    return rows


def native_search(query: str, retailer_id: str, fetcher: Fetcher) -> SearchResponse:
    """Same query words through the existing native store-search parser."""
    method = SEARCH_METHODS[retailer_id]
    words = query.split(" ", 1)[1].replace('"', '')
    url = method.first_page_url(words)
    started = time.monotonic()
    result = SearchResponse(query, "SEARCH_ERROR", datetime.now(UTC).isoformat())
    try:
        page = fetcher.fetch(ScrapeRequest(retailer_id, url), frozenset({method.host}))
        parsed = method.parse(page)
        result.results = [{"url": hit.product_url, "title": hit.title,
                           "snippet": hit.summary} for hit in parsed.hits]
        result.outcome = "RESULTS" if result.results else "NO_RESULTS"
        result.search_calls = page.attempts
    except (FetchError, ValueError) as error:
        result.search_calls = getattr(error, "attempts", 0)
        result.error = type(error).__name__ + ": native search failed"
    result.elapsed_seconds = round(time.monotonic() - started, 3)
    return result


def metrics(cases: list[dict], searches: dict, pages: dict) -> dict:
    results = []
    for case in cases:
        retailer, case_id = case["retailer_id"], case["case_id"]
        expected = product_url(case["expected_url"], retailer) if case.get("expected_url") else ""
        result = {"case_id": case_id, "retailer_id": retailer, "model": case["model"],
                  "expected_url": expected}
        for provider in ("native", "index"):
            responses = [SearchResponse(**r) for r in searches.get(case_id, {}).get(provider, [])]
            found = leads(responses, retailer)
            urls = [lead["url"] for lead in found]
            result[provider + "_leads"] = len(urls)
            result[provider + "_expected_found"] = bool(expected and expected in urls)
            result[provider + "_search_calls"] = sum(r.search_calls for r in responses)
            result[provider + "_seconds"] = round(sum(r.elapsed_seconds for r in responses), 3)
            result[provider + "_errors"] = sum(r.outcome not in {"RESULTS", "NO_RESULTS"}
                                               for r in responses)
        result["new_expected_page"] = result["index_expected_found"] and not result[
            "native_expected_found"]
        result["expected_fetch_outcome"] = pages.get(expected, {}).get("outcome", "NOT_FETCHED")
        result["index_expected_fetched"] = result["index_expected_found"] and result[
            "expected_fetch_outcome"] == "PRODUCT_PAGE"
        result["native_expected_fetched"] = result["native_expected_found"] and result[
            "expected_fetch_outcome"] == "PRODUCT_PAGE"
        results.append(result)
    labelled = [row for row in results if row["expected_url"]]
    return {"case_count": len(cases), "labelled_cases": len(labelled),
            "native_expected_found": sum(r["native_expected_found"] for r in labelled),
            "index_expected_found": sum(r["index_expected_found"] for r in labelled),
            "combined_expected_found": sum(r["native_expected_found"] or r["index_expected_found"]
                                            for r in labelled),
            "new_expected_pages": sum(r["new_expected_page"] for r in labelled),
            "index_expected_fetched": sum(r["index_expected_fetched"] for r in labelled),
            "native_expected_fetched": sum(r["native_expected_fetched"] for r in labelled),
            "index_errors": sum(r["index_errors"] for r in results),
            "native_errors": sum(r["native_errors"] for r in results),
            "fetched_product_pages": sum(p.get("outcome") == "PRODUCT_PAGE"
                                         for p in pages.values()),
            "per_case": results}


def write_reports(output: Path, run: dict) -> dict:
    report = metrics(run["cases"], run["searches"], run["pages"])
    report["search_backend"] = run.get("search_backend", "legacy_unspecified")
    (output / "metrics.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with (output / "benchmark.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report["per_case"][0]))
        writer.writeheader()
        writer.writerows(report["per_case"])
    with (output / "leads.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["case_id", "retailer_id", "provider", "url", "title", "query",
                  "search_retrieved_at"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for case in run["cases"]:
            for provider, records in run["searches"].get(case["case_id"], {}).items():
                for lead in leads([SearchResponse(**r) for r in records], case["retailer_id"]):
                    writer.writerow({**lead, "case_id": case["case_id"],
                                     "retailer_id": case["retailer_id"], "provider": provider})
    with (output / "supplied_urls.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "supplied_id", "retailer_id", "url", "search_target_id", "supplied_by",
            "supplied_on", "notes",
        ])
        writer.writeheader()
        for case in run["cases"]:
            records = run["searches"].get(case["case_id"], {}).get("index", [])
            for index, lead in enumerate(leads(
                [SearchResponse(**r) for r in records], case["retailer_id"]
            ), 1):
                writer.writerow({
                    "supplied_id": f"IDX-{case['case_id']}-{index}",
                    "retailer_id": case["retailer_id"], "url": lead["url"],
                    "search_target_id": case.get("target_id", ""),
                    "supplied_by": "catalog_index", "supplied_on": lead["search_retrieved_at"],
                    "notes": "Unconfirmed catalog-index lead; query: " + lead["query"],
                })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true")
    modes.add_argument("--replay", type=Path)
    parser.add_argument("--benchmark-native", action="store_true")
    parser.add_argument("--max-queries", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--max-cases", type=int, default=20)
    parser.add_argument("--max-pages-per-case", type=int, default=3)
    parser.add_argument("--index-run", type=Path,
                        help="Saved discover_catalog_leads.py live run; searched without network")
    args = parser.parse_args(argv)
    if not 1 <= args.max_cases <= 100 or not 0 <= args.max_pages_per_case <= 10:
        parser.error("Case/page budgets are outside the supported bounds")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Output directory must be empty; evidence is never overwritten")
    args.output.mkdir(parents=True, exist_ok=True)
    if args.replay:
        run = json.loads((args.replay / "run.json").read_text(encoding="utf-8"))
        for url, record in run["pages"].items():
            if not record.get("body_file"):
                continue
            body_file = Path(record["body_file"])
            if body_file.is_absolute() or ".." in body_file.parts:
                raise DiscoveryError("Unsafe body-file path in replay")
            body = (args.replay / body_file).read_bytes()
            if hashlib.sha256(body).hexdigest() != record["content_sha256"]:
                raise DiscoveryError("Saved retailer response hash differs")
            if record["outcome"] == "FETCH_ERROR":
                continue
            parsed = inspect_html(body, url, record["retailer_id"], record["retrieved_at"])
            if parsed["outcome"] != record["outcome"]:
                raise DiscoveryError("Saved page interpretation differs")
        summary = write_reports(args.output, run)
        print(json.dumps({"mode": "REPLAY", **{k: v for k, v in summary.items()
                                               if k != "per_case"}}))
        return 0
    if not args.cases:
        parser.error("--cases is required except in replay")
    cases = read_cases(args.cases)
    if len(cases) > args.max_cases:
        parser.error("Cases exceed --max-cases; choose the cohort explicitly")
    plan = [{"case_id": c["case_id"], "queries": queries(c, args.max_queries)} for c in cases]
    (args.output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    if not args.index_run:
        parser.error("--index-run is required except in replay")
    if args.benchmark_native and not args.live:
        parser.error("--benchmark-native requires --live")
    source = CatalogIndex.from_run(args.index_run)
    run = {"started_at": datetime.now(UTC).isoformat(), "cases": cases,
           "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
           "searches": {}, "pages": {}, "benchmark_native": args.benchmark_native,
           "search_backend": source.backend, "index_run": str(args.index_run.resolve()),
           "index_entries": len(source.entries), "index_coverage": source.coverage}
    # Save the declared cohort before any search. Expected URLs never enter search requests.
    def save():
        (args.output / "run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    save()
    fetcher = Fetcher()
    for case in cases:
        case_id, retailer = case["case_id"], case["retailer_id"]
        records = run["searches"][case_id] = {"index": [], "native": []}
        for query in queries(case, args.max_queries):
            if args.benchmark_native:
                records["native"].append(response_record(native_search(query, retailer, fetcher)))
                save()
            records["index"].append(response_record(source.search(query)))
            save()
        found = []
        for provider in ("index", "native"):
            found.extend(leads([SearchResponse(**r) for r in records[provider]], retailer))
        urls = list(dict.fromkeys(lead["url"] for lead in found))[:args.max_pages_per_case]
        # Ground-truth reachability is evaluated separately, never seeded into discovered leads.
        if case.get("expected_url"):
            urls.append(product_url(case["expected_url"], retailer))
        for url in dict.fromkeys(urls) if args.live else ():
            if url in run["pages"]:
                continue
            page, body = fetch_product(url, retailer, fetcher)
            page["retailer_id"] = retailer
            if body:
                name = "page-" + hashlib.sha256(url.encode()).hexdigest() + ".html"
                (args.output / name).write_bytes(body)
                page["body_file"] = name
            run["pages"][url] = page
            save()
        print(f"Completed {case_id}: " + ", ".join(
            f"{p} {len(leads([SearchResponse(**r) for r in records[p]], retailer))} leads"
            for p in ("native", "index")), flush=True)
    run["finished_at"] = datetime.now(UTC).isoformat()
    save()
    summary = write_reports(args.output, run)
    print(json.dumps({k: v for k, v in summary.items() if k != "per_case"}))
    return 1 if all(r["outcome"] not in {"RESULTS", "NO_RESULTS"}
                    for c in run["searches"].values() for r in c["index"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
