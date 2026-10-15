#!/usr/bin/env python3
"""Automatic product discovery: bounded retailer search, ranking, and candidate collection."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from laptop_tracker import evidence_approvals
from laptop_tracker.collection import (
    MANIFEST_COLUMNS,
    ROUTES,
    ManifestError,
    validate_manifest,
)
from laptop_tracker.match_run import TargetSnapshotError, matcher_from_validated
from laptop_tracker.normalization import CORE_MATCH_COLUMNS
from laptop_tracker.price_tracking import (
    PriceTrackingInputError,
    utc_text,
    validate_selected_list,
)
from laptop_tracker.product_discovery import (
    CANDIDATE_COLUMNS,
    COLLECTABLE,
    COVERAGE_COLUMNS,
    RankedCandidate,
    SearchTarget,
    candidate_row,
    has_code_signal,
    inspect_product_page,
    rank_target_hits,
    strip_variant,
    variant_decision,
)
from laptop_tracker.retailer_search import (
    BUDGET_CODE,
    NO_RESULTS,
    NOT_SENT_BUDGET,
    NOT_SENT_LIMIT,
    RESULTS,
    SEARCH_ERROR,
    SEARCH_METHODS,
    SEARCH_RECORD_COLUMNS,
    TIER_FAMILY,
    UNSUPPORTED_LAYOUT,
    BudgetedTransport,
    SearchLayoutError,
    SearchQuery,
    SearchRecord,
    search_queries,
    search_record_row,
)
from laptop_tracker.scraping import (
    Clock,
    FetchConfig,
    Fetcher,
    FetchError,
    ScrapeRequest,
    SystemClock,
    Transport,
    UrllibTransport,
    live_use_approved,
)
from laptop_tracker.target_master import (
    TargetMasterSchemaError,
    effective_tracking_mode,
    read_table_snapshot_csv,
    validate_target_master,
)
from laptop_tracker.verified_top import (
    CONFIGURATION_COLUMNS,
    LEDGER_COLUMNS,
    VerifiedRankingInputError,
    read_csv_rows,
    sha256_file,
)

ROOT = Path(__file__).resolve().parent
NOT_SENT_RETAILER = "NOT_SENT_RETAILER_FAILING"
CONSECUTIVE_ERRORS_TO_STOP = 2


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Automatic product discovery for verified configurations. --preview validates the "
            "inputs and writes the search plan and request limits without any request. --live "
            "searches the retailers within explicit limits, ranks the results, inspects the best "
            "candidates' own structured data for variants, and writes a collection manifest. "
            "With --collect it then runs collect_and_match.py on that manifest (fetch, extract, "
            "store, match, reports). Ranking selects pages to inspect; it never assigns a "
            "target. Never writes Google Sheets, the catalog, or mappings."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Plan only; no requests.")
    mode.add_argument("--live", action="store_true", help="Send bounded search requests.")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--scope-file", type=Path, help="CSV with a target_id column.")
    scope.add_argument("--targets", help="Comma-separated target IDs.")
    parser.add_argument("--retailers", default=",".join(SEARCH_METHODS),
                        help="Comma-separated retailer IDs (default: all five).")
    parser.add_argument("--target-master", type=Path, required=True,
                        help="Validated TARGET_MASTER snapshot CSV.")
    parser.add_argument("--selected-list", type=Path, required=True,
                        help="top_100_verified_configurations.csv")
    parser.add_argument("--contributions", type=Path, required=True,
                        help="verified_configuration_contributions.csv")
    parser.add_argument("--max-total-requests", type=positive,
                        help="Hard ceiling on all HTTP requests (required with --live).")
    parser.add_argument("--max-search-requests", type=positive, default=40)
    parser.add_argument("--max-search-pages", type=positive, default=2,
                        help="Result pages per query where pagination exists (default 2).")
    parser.add_argument("--max-candidates-per-target-retailer", type=positive, default=2)
    parser.add_argument("--max-product-fetches", type=positive, default=20,
                        help="Product-page inspections plus collection fetches (default 20).")
    parser.add_argument("--collect", action="store_true",
                        help="Collect and match the manifest with collect_and_match.py.")
    parser.add_argument("--store", type=Path,
                        default=ROOT / "data" / "history" / "price_history.sqlite3")
    parser.add_argument("--baseline-manifest", type=Path,
                        help="Earlier manually supplied manifest, for a coverage comparison.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New run folder.")
    args = parser.parse_args(argv)
    if args.live and args.max_total_requests is None:
        parser.error("--live requires --max-total-requests.")
    return args


def write_csv(path: Path, columns, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_targets(args):
    """Validated catalog, selected list, and the scoped CONFIGURATION targets."""
    target_master = validate_target_master(read_table_snapshot_csv(args.target_master))
    matcher_from_validated(target_master)  # Same validation the collector applies.
    selected = validate_selected_list(
        read_csv_rows(args.selected_list, CONFIGURATION_COLUMNS, "Selected list"),
        read_csv_rows(args.contributions, LEDGER_COLUMNS, "Contribution ledger"),
        target_master,
        target_master_sha256=sha256_file(args.target_master),
        list_sha256=sha256_file(args.selected_list),
    )
    wanted = (
        [row["target_id"].strip() for row in read_rows(args.scope_file)]
        if args.scope_file else [t.strip() for t in args.targets.split(",") if t.strip()]
    )
    wanted = list(dict.fromkeys(wanted))
    by_target = selected.by_target
    catalog = {
        str(row["target_id"]): row
        for row in target_master.valid_rows.to_dict(orient="records")
    }
    missing = [t for t in wanted if t not in by_target]
    if missing:
        raise ValueError(f"Scope targets not in the verified selected list: {', '.join(missing)}")
    targets: list[SearchTarget] = []
    excluded: list[str] = []
    for target_id in wanted:
        row = catalog[target_id]
        if effective_tracking_mode(row.get("tracking_mode", "")) != "CONFIGURATION":
            excluded.append(target_id)  # LISTING_ONLY never enters configuration discovery.
            continue
        item = by_target[target_id]
        targets.append(SearchTarget(
            target_id=target_id,
            rank=str(item.rank),
            brand=str(row.get("brand", "") or ""),
            series=str(row.get("series", "") or ""),
            model=str(row["model"]),
            part_number=str(row.get("manufacturer_part_number", "") or "").strip(),
            core={name: str(item.list_row[name]) for name in CORE_MATCH_COLUMNS},
        ))
    ranks = {item.target_id: item.rank for item in selected.configurations}
    return targets, excluded, ranks


class SearchJob:
    """The search requests for one target at one retailer, in tier order."""

    def __init__(self, target: SearchTarget, retailer_id: str, max_pages: int) -> None:
        method = SEARCH_METHODS[retailer_id]
        self.target = target
        self.method = method
        self.retailer_id = retailer_id
        self.queries = search_queries(
            target.target_id, retailer_id, model=target.model, part_number=target.part_number,
            brand=target.brand, series=target.series,
        )
        self.max_pages = max_pages if method.paginated else 1
        self.index = 0
        self.page = 1
        self.next_url = ""
        self.records: list[SearchRecord] = []
        self.done = False

    def current(self) -> tuple[SearchQuery, int, str] | None:
        while not self.done and self.index < len(self.queries):
            query = self.queries[self.index]
            if query.tier == TIER_FAMILY and has_code_signal(self.target, self.records):
                self.done = True  # The family query runs only when it is needed.
                return None
            url = self.next_url if self.page > 1 else self.method.first_page_url(query.text)
            return query, self.page, url
        self.done = True
        return None

    def advance(self, record: SearchRecord) -> None:
        self.records.append(record)
        if record.outcome == RESULTS and record.next_url and self.page < self.max_pages:
            self.page += 1
            self.next_url = record.next_url
            return
        self.page, self.next_url = 1, ""
        self.index += 1
        if self.index < len(self.queries) and self.queries[self.index].tier != TIER_FAMILY:
            return
        if has_code_signal(self.target, self.records):
            self.done = True

    def skip_rest(self, outcome: str, message: str) -> None:
        while (planned := self.current()) is not None:
            query, page, url = planned
            self.records.append(SearchRecord(query, page, url, outcome, message=message))
            self.page, self.next_url = 1, ""
            self.index += 1
        self.done = True


def evidence_name(body: bytes, content_type: str) -> str:
    ext = "json" if "json" in content_type.lower() else "html"
    return f"{hashlib.sha256(body).hexdigest()}.{ext}"


def run_searches(jobs, fetcher, transport, args, evidence_dir: Path, log) -> int:
    """Round-robin across retailers so per-retailer throttling waits overlap."""
    sent = 0
    errors: Counter[str] = Counter()
    stopped: set[str] = set()
    pending = list(jobs)
    while pending:
        progressed = False
        for job in list(pending):
            planned = job.current()
            if planned is None:
                pending.remove(job)
                continue
            query, page_number, url = planned
            if job.retailer_id in stopped:
                job.skip_rest(NOT_SENT_RETAILER, "Retailer search stopped after repeated errors.")
                pending.remove(job)
                continue
            if sent >= args.max_search_requests:
                job.skip_rest(NOT_SENT_LIMIT, "--max-search-requests reached.")
                pending.remove(job)
                continue
            if transport.remaining <= 0:
                job.skip_rest(NOT_SENT_BUDGET, "--max-total-requests reached.")
                pending.remove(job)
                continue
            record = SearchRecord(query, page_number, url)
            try:
                page = fetcher.fetch(ScrapeRequest(job.retailer_id, url),
                                     allowed_hosts=frozenset({job.method.host}))
            except FetchError as error:
                if error.code == BUDGET_CODE:
                    record.outcome = NOT_SENT_BUDGET
                else:
                    record.outcome = SEARCH_ERROR
                    errors[job.retailer_id] += 1
                    if errors[job.retailer_id] >= CONSECUTIVE_ERRORS_TO_STOP:
                        stopped.add(job.retailer_id)
                record.http_status = str(error.http_status or "")
                record.attempts = str(error.attempts)
                record.message = f"{error.code}: {error.message}"
            else:
                sent += 1
                errors[job.retailer_id] = 0
                name = evidence_name(page.body, page.content_type)
                (evidence_dir / name).write_bytes(page.body)
                record.http_status = str(page.status_code)
                record.final_url = page.final_url
                record.retrieved_at = utc_text(page.retrieved_at)
                record.attempts = str(page.attempts)
                record.content_sha256 = page.content_sha256
                record.evidence_file = f"evidence/{name}"
                try:
                    parsed = job.method.parse(page)
                except SearchLayoutError as error:
                    record.outcome = UNSUPPORTED_LAYOUT
                    record.message = str(error)
                else:
                    record.hit_items = list(parsed.hits)
                    record.hits = len(parsed.hits)
                    record.filtered_links = parsed.filtered_links
                    record.possibly_truncated = parsed.possibly_truncated
                    record.next_url = parsed.next_url
                    record.outcome = RESULTS if parsed.hits else NO_RESULTS
            log(f"  search {job.retailer_id:15} {job.target.target_id} {query.tier} "
                f"p{page_number}: {record.outcome} ({record.hits} results)")
            job.advance(record)
            progressed = True
            if record.outcome == NOT_SENT_BUDGET:
                job.skip_rest(NOT_SENT_BUDGET, "--max-total-requests reached.")
        if not progressed:
            break
    return sent


def select_and_inspect(candidates, job, fetcher, transport, args, state, evidence_dir, log):
    """Decide each candidate; inspect selected Shopify pages for their variants."""
    inspections: list[dict[str, str]] = []
    variants: list[dict[str, str]] = []
    manifest: list[tuple[RankedCandidate, str, str]] = []
    chosen = 0
    method = job.method
    for candidate in candidates:
        clues = candidate.clues
        title_differs = any(
            clues[name].startswith("DIFFERS")
            for name in ("ram_capacity_gb", "storage_capacity_gb", "screen_size_in", "cpu_series")
        )
        if candidate.category not in COLLECTABLE:
            candidate.decision = "NOT_SELECTED"
            candidate.decision_reason = f"Category {candidate.category} is never collected " \
                                        "automatically; listed for review."
            continue
        if chosen >= args.max_candidates_per_target_retailer:
            candidate.decision = "NOT_SELECTED_LIMIT"
            candidate.decision_reason = "--max-candidates-per-target-retailer reached."
            continue
        chosen += 1
        if method.kind == "woocommerce_html":  # No variant inspection: one page per product.
            if title_differs:
                candidate.decision = "REVIEW_DIFFERENT_CONFIGURATION_BY_TITLE"
                candidate.decision_reason = "Title clues state a different configuration."
                continue
            candidate.decision = "COLLECT"
            candidate.decision_reason = "Product page; collected as listed."
            manifest.append((candidate, candidate.hit.product_url, ""))
            continue
        if state["product_fetches"] >= args.max_product_fetches or transport.remaining <= 0:
            candidate.decision = "NOT_INSPECTED_BUDGET"
            candidate.decision_reason = "Product-fetch or total request budget reached."
            continue
        url = candidate.hit.product_url
        row = {"target_id": job.target.target_id, "retailer_id": job.retailer_id,
               "product_url": url}
        try:
            page = fetcher.fetch(ScrapeRequest(job.retailer_id, url),
                                 allowed_hosts=frozenset({method.host}))
        except FetchError as error:
            state["product_fetches"] += error.code != BUDGET_CODE
            candidate.decision = "REVIEW_INSPECTION_FAILED"
            candidate.decision_reason = f"{error.code}: {error.message}"
            inspections.append(row | {"outcome": "FETCH_FAILED", "message": error.code})
            log(f"  inspect {job.retailer_id:14} {job.target.target_id}: FETCH_FAILED "
                f"{error.code}")
            continue
        state["product_fetches"] += 1
        name = evidence_name(page.body, page.content_type)
        (evidence_dir / name).write_bytes(page.body)
        inspection = inspect_product_page(method, url, page.text())
        inspections.append(row | {
            "outcome": inspection.outcome, "variants": str(len(inspection.variants)),
            "retrieved_at": utc_text(page.retrieved_at), "content_sha256": page.content_sha256,
            "evidence_file": f"evidence/{name}", "message": inspection.message,
        })
        log(f"  inspect {job.retailer_id:14} {job.target.target_id}: {inspection.outcome} "
            f"({len(inspection.variants)} variants)")
        if inspection.outcome == "SINGLE_VARIANT":
            if title_differs:
                candidate.decision = "REVIEW_DIFFERENT_CONFIGURATION_BY_TITLE"
                candidate.decision_reason = "Single variant; title clues state a different " \
                                            "configuration."
                continue
            candidate.decision = "COLLECT"
            candidate.decision_reason = "Single-variant product page."
            manifest.append((candidate, url, ""))
            continue
        if inspection.outcome != "MULTI_VARIANT":
            candidate.decision = "REVIEW_VARIANTS_UNREADABLE"
            candidate.decision_reason = inspection.message or "Variants could not be listed."
            continue
        decisions = []
        for variant in inspection.variants:
            decision, reason = variant_decision(variant, job.target)
            if decision == "COLLECT" and not method.multi_variant_collection:
                decision, reason = "REVIEW", "This retailer's adapter collects single-variant " \
                                             "pages only."
            decisions.append((variant, decision, reason))
            variants.append({
                "target_id": job.target.target_id, "retailer_id": job.retailer_id,
                "product_url": url, "variant_id": variant.variant_id,
                "variant_url": variant.url, "option_title": variant.title,
                "availability": variant.available, "price": variant.price,
                "decision": decision, "reason": reason,
            })
        collect = [(v, reason) for v, decision, reason in decisions if decision == "COLLECT"]
        if not method.multi_variant_collection:
            candidate.decision = "REVIEW_MULTI_VARIANT_NOT_SUPPORTED"
            candidate.decision_reason = (
                f"{len(inspection.variants)} variants; this retailer's adapter collects "
                "single-variant pages only. Variants are listed in variants.csv."
            )
        elif len(collect) == 1:
            variant, reason = collect[0]
            candidate.decision = "COLLECT_VARIANT"
            candidate.decision_reason = f"Variant {variant.variant_id}: {reason}"
            manifest.append((candidate, variant.url, variant.variant_id))
        elif collect:
            candidate.decision = "REVIEW_SEVERAL_MATCHING_VARIANTS"
            candidate.decision_reason = "More than one variant states the target RAM/storage."
        else:
            candidate.decision = "REVIEW_NO_MATCHING_VARIANT"
            candidate.decision_reason = (
                f"{len(inspection.variants)} variants listed; none states the target RAM and "
                "storage. All are kept in variants.csv."
            )
    return manifest, inspections, variants


def backup_store(store: Path, destination: Path) -> str:
    if not store.exists():
        return "NO_STORE_YET"
    source = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    return sha256_file(destination)


def load_collector():
    spec = importlib.util.spec_from_file_location("collect_and_match",
                                                  ROOT / "collect_and_match.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def coverage_rows(baseline: Path, candidates, manifest_rows, targets, retailers):
    """Compare with the earlier manual manifest, for in-scope targets and retailers only.

    A baseline URL counts as found only when it was a candidate for its own expected target.
    """
    rows: list[dict[str, str]] = []
    in_manifest = {strip_variant(r["product_url"]): r["product_url"] for r in manifest_rows}
    for row in read_rows(baseline):
        if row.get("expected_target_id") not in targets or row["retailer_id"] not in retailers:
            continue
        url = row["product_url"]
        same = [c for c in candidates if c.hit.retailer_id == row["retailer_id"]
                and c.target.target_id == row.get("expected_target_id")
                and strip_variant(c.hit.product_url) == strip_variant(url)]
        found = same[0] if same else None
        rows.append({
            "retailer_id": row["retailer_id"],
            "expected_target_id": row.get("expected_target_id", ""),
            "baseline_url": url,
            "found_by_search": "TRUE" if found else "FALSE",
            "discovered_product_url": found.hit.product_url if found else "",
            "candidate_category": found.category if found else "",
            "candidate_decision": found.decision if found else "",
            "in_new_manifest": "TRUE" if strip_variant(url) in in_manifest else "FALSE",
            "new_manifest_url": in_manifest.get(strip_variant(url), ""),
            "note": "" if found else "Not returned by the bounded searches of this run.",
        })
    known = {(r["retailer_id"], strip_variant(r["baseline_url"])) for r in rows}
    for row in manifest_rows:
        if (row["retailer_id"], strip_variant(row["product_url"])) not in known:
            rows.append({
                "retailer_id": row["retailer_id"],
                "expected_target_id": row["expected_target_id"],
                "baseline_url": "", "found_by_search": "TRUE",
                "discovered_product_url": row["product_url"], "candidate_category": "",
                "candidate_decision": "COLLECT", "in_new_manifest": "TRUE",
                "new_manifest_url": row["product_url"],
                "note": "Newly discovered URL (not in the baseline manifest).",
            })
    return rows


def main(argv: list[str] | None = None, *, transport: Transport | None = None,
         clock: Clock | None = None) -> int:
    args = parse_args(argv)
    clock = clock or SystemClock()
    started = clock.now().astimezone(UTC)
    retailers = [r.strip() for r in args.retailers.split(",") if r.strip()]
    unknown = [r for r in retailers if r not in SEARCH_METHODS]
    if unknown:
        print(f"Discovery stopped: no search support for {', '.join(unknown)}.", file=sys.stderr)
        return 1
    try:
        targets, excluded, ranks = load_targets(args)
        evidence_approvals.read_approvals()
    except (FileNotFoundError, KeyError, ValueError, TargetMasterSchemaError,
            TargetSnapshotError, VerifiedRankingInputError, PriceTrackingInputError) as exc:
        print(f"Discovery stopped before any request: {exc}", file=sys.stderr)
        return 1
    if not targets:
        print("Discovery stopped: no CONFIGURATION target in scope.", file=sys.stderr)
        return 1
    blocked = [r for r in retailers if not live_use_approved(ROUTES[r].adapter_factory())]
    if args.live and blocked:
        print(f"Discovery stopped: live use not approved for {', '.join(blocked)}.",
              file=sys.stderr)
        return 1
    try:
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"Discovery stopped: {args.output_dir} already exists.", file=sys.stderr)
        return 1
    out = args.output_dir
    log = print

    jobs = [SearchJob(target, retailer, args.max_search_pages)
            for target in targets for retailer in retailers]
    plan_rows = []
    worst_search = 0
    for job in jobs:
        for order, query in enumerate(job.queries, start=1):
            pages = job.max_pages
            worst_search += pages
            plan_rows.append({
                "target_id": query.target_id, "rank": job.target.rank,
                "retailer_id": query.retailer_id, "order": str(order), "tier": query.tier,
                "query": query.text,
                "verified_identifier": "TRUE" if query.verified_identifier else "FALSE",
                "runs": "ONLY_IF_NO_MODEL_CODE_RESULT" if query.tier == TIER_FAMILY else "ALWAYS",
                "max_pages": str(pages), "first_page_url": job.method.first_page_url(query.text),
                "mechanism": job.method.kind, "notes": job.method.notes,
            })
    write_csv(out / "search_plan.csv", list(plan_rows[0]), plan_rows)
    shopify_jobs = sum(job.method.kind != "woocommerce_html" for job in jobs)
    worst_product = (len(jobs) * args.max_candidates_per_target_retailer
                     + shopify_jobs * args.max_candidates_per_target_retailer)
    limits = {
        "max_total_requests": args.max_total_requests,
        "max_search_requests": args.max_search_requests,
        "max_search_pages": args.max_search_pages,
        "max_candidates_per_target_retailer": args.max_candidates_per_target_retailer,
        "max_product_fetches": args.max_product_fetches,
        "retry_attempts_per_request": FetchConfig().max_attempts,
        "min_seconds_between_requests_per_retailer": FetchConfig().min_request_interval_seconds,
    }
    summary: dict = {
        "description": "Automatic product discovery (bounded search, ranking, collection).",
        "mode": "LIVE" if args.live else "PREVIEW",
        "started_at_utc": utc_text(started),
        "scope": {"targets": [t.target_id for t in targets], "retailers": retailers,
                  "listing_only_excluded": excluded},
        "limits": limits,
        "plan": {
            "search_jobs": len(jobs),
            "worst_case_search_requests": worst_search,
            "search_requests_cap": min(worst_search, args.max_search_requests),
            "worst_case_product_fetches": worst_product,
            "product_fetch_cap": min(worst_product, args.max_product_fetches),
        },
        "input_sha256": {str(p): sha256_file(p) for p in (
            args.target_master, args.selected_list, args.contributions,
            evidence_approvals.APPROVALS_PATH, *( [args.scope_file] if args.scope_file else []),
        )},
    }
    log(f"Scope: {len(targets)} targets x {len(retailers)} retailers = {len(jobs)} search jobs"
        + (f"; LISTING_ONLY excluded: {', '.join(excluded)}" if excluded else "") + ".")
    log(f"Searches: up to {summary['plan']['search_requests_cap']} requests (worst case "
        f"{worst_search}); product fetches: up to {summary['plan']['product_fetch_cap']} "
        f"(worst case {worst_product}); total ceiling: "
        f"{args.max_total_requests if args.max_total_requests else 'n/a (preview)'}.")
    if not args.live:
        summary["run_status"] = "PREVIEW_ONLY"
        summary["requests_sent"] = 0
        (out / "discovery_summary.json").write_text(json.dumps(summary, indent=2) + "\n",
                                                    encoding="utf-8")
        log(f"Preview only: no request was made. Plan written to {out}.")
        return 0

    # Live search.
    evidence_dir = out / "evidence"
    evidence_dir.mkdir()
    budget = BudgetedTransport(transport or UrllibTransport(), args.max_total_requests)
    fetcher = Fetcher(budget, FetchConfig(), clock)
    log("Searching...")
    searches_sent = run_searches(jobs, fetcher, budget, args, evidence_dir, log)
    records = [record for job in jobs for record in job.records]
    write_csv(out / "search_requests.csv", SEARCH_RECORD_COLUMNS,
              (search_record_row(r) for r in records))
    hit_rows = [
        {"target_id": r.query.target_id, "retailer_id": r.query.retailer_id,
         "tier": r.query.tier, "query": r.query.text, "page_number": str(r.page_number),
         "position": str(h.position), "product_url": h.product_url, "listed_url": h.listed_url,
         "title": h.title, "vendor": h.vendor, "product_type": h.product_type,
         "available": h.available, "price": h.price_text, "tags": "; ".join(h.tags)}
        for r in records for h in r.hit_items
    ]
    write_csv(out / "search_results.csv", [
        "target_id", "retailer_id", "tier", "query", "page_number", "position", "product_url",
        "listed_url", "title", "vendor", "product_type", "available", "price", "tags",
    ], hit_rows)

    # Rank, inspect, and decide.
    log("Ranking and inspecting candidates...")
    state = {"product_fetches": 0}
    all_candidates: list[RankedCandidate] = []
    manifest: list[tuple[RankedCandidate, str, str]] = []
    inspections: list[dict[str, str]] = []
    variant_rows: list[dict[str, str]] = []
    for job in jobs:
        try:
            ranked = rank_target_hits(job.target, job.retailer_id, job.records)
            chosen, inspected, variants = select_and_inspect(
                ranked, job, fetcher, budget, args, state, evidence_dir, log
            )
        except Exception as exc:  # One retailer's failure must not discard the others.
            log(f"  {job.retailer_id} {job.target.target_id}: candidate step failed "
                f"({type(exc).__name__}); kept for review.")
            inspections.append({"target_id": job.target.target_id,
                                "retailer_id": job.retailer_id, "outcome": "STEP_FAILED",
                                "message": f"{type(exc).__name__}: {exc}"})
            continue
        all_candidates += ranked
        manifest += chosen
        inspections += inspected
        variant_rows += variants
    write_csv(out / "product_inspections.csv", [
        "target_id", "retailer_id", "product_url", "outcome", "variants", "retrieved_at",
        "content_sha256", "evidence_file", "message",
    ], ({**{k: "" for k in ("variants", "retrieved_at", "content_sha256", "evidence_file",
                            "product_url")}, **row} for row in inspections))
    write_csv(out / "variants.csv", [
        "target_id", "retailer_id", "product_url", "variant_id", "variant_url", "option_title",
        "availability", "price", "decision", "reason",
    ], variant_rows)

    # Manifest: one row per collected URL; a URL found for two targets is kept once.
    remaining_fetches = args.max_product_fetches - state["product_fetches"]
    manifest_rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate, url, _variant in manifest:
        key = (candidate.hit.retailer_id, url)
        if key in seen:
            candidate.decision = "DUPLICATE_OF_EARLIER_CANDIDATE"
            continue
        if len(manifest_rows) >= remaining_fetches:
            candidate.decision = "NOT_COLLECTED_BUDGET"
            candidate.decision_reason = "Product-fetch budget reached before collection."
            continue
        seen.add(key)
        manifest_rows.append({
            "request_id": f"disc-{candidate.hit.retailer_id}-{candidate.target.target_id}-"
                          f"{len(manifest_rows) + 1}",
            "retailer_id": candidate.hit.retailer_id,
            "product_url": url,
            "expected_target_id": candidate.target.target_id,
            "enabled": "TRUE",
        })
    write_csv(out / "candidates.csv", CANDIDATE_COLUMNS,
              (candidate_row(c) for c in all_candidates))
    manifest_path = out / "manifest.csv"
    write_csv(manifest_path, MANIFEST_COLUMNS, manifest_rows)
    manifest_issue = ""
    if manifest_rows:
        try:
            validate_manifest(
                [row | {"_row_number": str(i)} for i, row in enumerate(manifest_rows, start=2)],
                ranks,
            )
        except ManifestError as error:
            manifest_issue = str(error)

    approvals = evidence_approvals.read_approvals()
    approval_urls = {(row["retailer_id"], row["offer_url"]) for row in approvals}
    summary |= {
        "requests_sent": budget.sent,
        "search_requests_sent": searches_sent,
        "product_inspections": state["product_fetches"],
        "search_outcomes": dict(Counter(f"{r.query.retailer_id}:{r.outcome}" for r in records)),
        "candidates": dict(Counter(c.category for c in all_candidates)),
        "decisions": dict(Counter(c.decision for c in all_candidates)),
        "manifest_rows": len(manifest_rows),
        "manifest_by_retailer": dict(Counter(r["retailer_id"] for r in manifest_rows)),
        "manifest_issue": manifest_issue,
        "retailer_evidence": {
            "search_pages_saved": sum(1 for r in records if r.evidence_file),
            "product_pages_inspected": state["product_fetches"],
            "evidence_folder": str(evidence_dir),
        },
        "manufacturer_evidence": {
            "catalog_part_numbers_used_as_queries": sum(1 for t in targets if t.part_number),
            "manufacturer_pages_fetched": 0,
        },
        "owner_decisions": {
            "approvals_file_sha256": sha256_file(evidence_approvals.APPROVALS_PATH),
            "collected_offers_with_listing_approval": [],
            "note": "Approvals apply only to their exact retailer offer URL (including any "
                    "?variant=); never to other search results.",
        },
    }

    # Collection and matching through the existing collector.
    status = "SEARCH_ONLY"
    if args.collect and manifest_rows and not manifest_issue:
        collection_budget = min(len(manifest_rows), budget.remaining)
        if collection_budget < len(manifest_rows):
            summary["collection_note"] = "Total budget smaller than the manifest; some rows " \
                                         "may fail with REQUEST_BUDGET_EXHAUSTED."
        backup = out / "store_backup_before_collection.sqlite3"
        summary["store_backup"] = {"path": str(backup), "sha256": backup_store(args.store,
                                                                                backup)}
        # A fresh Fetcher inside the collector: wait one interval so per-retailer spacing holds.
        clock.sleep(FetchConfig().min_request_interval_seconds)
        log(f"Collecting {len(manifest_rows)} candidate pages with collect_and_match.py...")
        code = load_collector().main([
            "--live", "--manifest", str(manifest_path),
            "--target-master", str(args.target_master),
            "--selected-list", str(args.selected_list),
            "--contributions", str(args.contributions),
            "--store", str(args.store), "--output-dir", str(out / "collection"),
        ], transport=budget, clock=clock)
        summary["collection_exit_code"] = code
        results = out / "collection" / "match_results.csv"
        if results.exists():
            rows = read_rows(results)
            summary["match_statuses"] = dict(Counter(r["match_status"] for r in rows))
            summary["expected_target_checks"] = dict(
                Counter(r["expected_target_check"] for r in rows)
            )
            review = out / "collection" / "match_review_queue.csv"
            summary["review_rows"] = len(read_rows(review)) if review.exists() else 0
            summary["owner_decisions"]["collected_offers_with_listing_approval"] = sorted(
                {r["offer_url"] for r in rows if (r["retailer_id"], r["offer_url"])
                 in approval_urls}
            )
        status = {0: "SUCCESS", 2: "PARTIAL_SUCCESS"}.get(code, "COLLECTION_FAILED")
    elif args.collect and manifest_issue:
        status = "MANIFEST_INVALID"
    summary["requests_sent"] = budget.sent
    if args.baseline_manifest:
        coverage = coverage_rows(args.baseline_manifest, all_candidates, manifest_rows,
                                 {t.target_id for t in targets}, set(retailers))
        write_csv(out / "coverage_vs_baseline.csv", COVERAGE_COLUMNS, coverage)
        baseline = [r for r in coverage if r["baseline_url"]]
        summary["coverage_vs_baseline"] = {
            "baseline_rows": len(baseline),
            "baseline_found_by_search": sum(r["found_by_search"] == "TRUE" for r in baseline),
            "baseline_in_new_manifest": sum(r["in_new_manifest"] == "TRUE" for r in baseline),
            "new_urls_not_in_baseline": len(coverage) - len(baseline),
        }
    errors = sum(1 for r in records if r.outcome in {SEARCH_ERROR, UNSUPPORTED_LAYOUT})
    if status == "SEARCH_ONLY" and errors:
        status = "SEARCH_ONLY_WITH_ERRORS"
    summary["run_status"] = status
    summary["finished_at_utc"] = utc_text(clock.now())
    (out / "discovery_summary.json").write_text(json.dumps(summary, indent=2) + "\n",
                                                encoding="utf-8")
    log(f"Discovery {status}: {budget.sent} requests sent ({searches_sent} searches, "
        f"{state['product_fetches']} inspections); candidates "
        f"{dict(Counter(c.category for c in all_candidates))}; manifest rows "
        f"{len(manifest_rows)}.")
    if manifest_issue:
        log(f"  Manifest not collected: {manifest_issue}")
    log(f"Reports written to {out}.")
    return 0 if status in {"SUCCESS", "SEARCH_ONLY"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
