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
    HISTORICAL_VERIFIED_URL,
    RankedCandidate,
    SearchTarget,
    candidate_row,
    configuration_difference,
    inspect_product_page,
    plausible_candidate,
    rank_target_hits,
    results_summary,
    seed_candidate,
    strip_variant,
    variant_decision,
)
from laptop_tracker.retailer_search import (
    BUDGET_CODE,
    NO_RESULTS,
    NOT_SENT_BUDGET,
    NOT_SENT_LIMIT,
    NOT_SENT_QUERY_LIMIT,
    NOT_SENT_REPLAY_ONLY,
    RESULTS,
    SEARCH_ERROR,
    SEARCH_METHODS,
    SEARCH_RECORD_COLUMNS,
    TIER_MODEL_CODE,
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
    FetchedPage,
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

# Why a target/retailer search job stopped (search_jobs.csv).
STOP_PLAUSIBLE = "PLAUSIBLE_CANDIDATE_FOUND"
STOP_ALL_TIERS = "ALL_QUERY_TIERS_TRIED"
STOP_QUERY_LIMIT = "QUERY_LIMIT_PER_TARGET_RETAILER"
STOP_SEARCH_LIMIT = "SEARCH_REQUEST_LIMIT_REACHED"
STOP_BUDGET = "TOTAL_REQUEST_BUDGET_REACHED"
STOP_RETAILER = "RETAILER_STOPPED_AFTER_ERRORS"
STOP_REPLAY = "REPLAY_ONLY_NO_SAVED_RESPONSE"
SEARCH_JOB_COLUMNS = (
    "target_id", "rank", "retailer_id", "queries_planned", "queries_run", "pages_sent",
    "pages_from_cache", "pages_not_sent", "tiers_run", "stop_reason", "plausible_candidate_url",
    "historical_seeds",
)


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
    parser.add_argument("--max-queries-per-target-retailer", type=positive, default=5,
                        help="Query tiers tried per target and retailer (default 5: code, part "
                             "number, code with RAM/storage, family, family with specs).")
    parser.add_argument("--max-product-fetches", type=positive, default=20,
                        help="Product-page inspections plus collection fetches (default 20).")
    parser.add_argument("--min-seconds-between-requests", type=float,
                        default=FetchConfig().min_request_interval_seconds,
                        help="Per-retailer spacing between requests; never below the default "
                             f"{FetchConfig().min_request_interval_seconds:g} seconds.")
    parser.add_argument("--search-cache", type=Path, action="append", default=[],
                        help="An earlier discovery folder (repeatable). Its saved search and "
                             "inspection responses are replayed instead of sending the same "
                             "request again.")
    parser.add_argument("--cache-max-age-hours", type=float,
                        help="Replay saved responses only when retrieved within this many hours.")
    parser.add_argument("--replay-only", action="store_true",
                        help="With --live: send no request at all; use only --search-cache "
                             "responses and record every other request as not sent.")
    parser.add_argument("--seed-urls", type=Path, action="append", default=[],
                        help="CSV of earlier verified offers (repeatable), e.g. a collection's "
                             "match_results.csv: rows with match_status EXACT seed their offer "
                             "URL as a candidate for that target. Re-checked, never assumed.")
    parser.add_argument("--seed-store", type=Path,
                        help="History store whose EXACT results seed offer URLs as candidates.")
    parser.add_argument("--max-seed-urls-per-target-retailer", type=positive, default=2)
    parser.add_argument("--no-collect-title-differences", action="store_true",
                        help="Do not collect same-code pages whose title states another "
                             "configuration (default: collect them so the page's own "
                             "specifications decide, within the candidate limit).")
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
    if args.replay_only and args.collect:
        parser.error("--replay-only cannot collect pages; run collect_and_match.py separately.")
    if args.replay_only and not args.search_cache:
        parser.error("--replay-only needs at least one --search-cache folder.")
    if args.min_seconds_between_requests < FetchConfig().min_request_interval_seconds:
        parser.error("--min-seconds-between-requests may not be below the default spacing.")
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
    """The search requests for one target at one retailer, in tier order.

    The complete model code always runs first, without specification filters. Each later tier
    (part number, code with RAM/storage, family, family with specifications) runs only while the
    results so far hold no plausible candidate: a page showing the complete code (or verified
    part number) whose title does not state another configuration. Results showing only another
    RAM/storage configuration, an unexplained suffix, or a code only in the URL keep the search
    going (2026-10-19). Every query records why it ran, and the job records why it stopped.
    """

    def __init__(self, target: SearchTarget, retailer_id: str, max_pages: int,
                 max_queries: int = 5) -> None:
        method = SEARCH_METHODS[retailer_id]
        self.target = target
        self.method = method
        self.retailer_id = retailer_id
        self.all_queries = search_queries(
            target.target_id, retailer_id, model=target.model, part_number=target.part_number,
            brand=target.brand, series=target.series, core=target.core,
        )
        self.queries = self.all_queries[:max_queries]
        self.max_pages = max_pages if method.paginated else 1
        self.index = 0
        self.page = 1
        self.next_url = ""
        self.records: list[SearchRecord] = []
        self.done = False
        self.stop_reason = ""
        self.plausible_url = ""

    def current(self) -> tuple[SearchQuery, int, str] | None:
        if self.done:
            return None
        if self.index >= len(self.queries):
            self._finish()
            return None
        query = self.queries[self.index]
        url = self.next_url if self.page > 1 else self.method.first_page_url(query.text)
        return query, self.page, url

    def run_reason(self, query: SearchQuery, page_number: int) -> str:
        if page_number > 1:
            return f"result page {page_number} of the same query"
        if query.tier == TIER_MODEL_CODE:
            return "first query: complete model code without specification filters"
        tiers = ", ".join(dict.fromkeys(r.query.tier for r in self.records))
        return (f"no plausible candidate after {tiers}; "
                f"{results_summary(self.target, self.records)}")

    def advance(self, record: SearchRecord) -> None:
        self.records.append(record)
        if record.outcome == RESULTS and record.next_url and self.page < self.max_pages:
            self.page += 1
            self.next_url = record.next_url
            return
        self.page, self.next_url = 1, ""
        self.index += 1
        url = plausible_candidate(self.target, self.records)
        if url:
            self.done, self.stop_reason, self.plausible_url = True, STOP_PLAUSIBLE, url
        elif self.index >= len(self.queries):
            self._finish()

    def _finish(self) -> None:
        if self.done:
            return
        self.done = True
        if len(self.all_queries) > len(self.queries):
            self.stop_reason = self.stop_reason or STOP_QUERY_LIMIT
            for query in self.all_queries[len(self.queries):]:
                self.records.append(SearchRecord(
                    query, 1, self.method.first_page_url(query.text), NOT_SENT_QUERY_LIMIT,
                    message="--max-queries-per-target-retailer reached.",
                    run_reason="not run: query limit",
                ))
        self.stop_reason = self.stop_reason or STOP_ALL_TIERS

    def skip_rest(self, outcome: str, message: str, stop_reason: str = "") -> None:
        while (planned := self.current()) is not None:
            query, page, url = planned
            self.records.append(SearchRecord(query, page, url, outcome, message=message,
                                             run_reason=self.run_reason(query, page)))
            self.page, self.next_url = 1, ""
            self.index += 1
        self.done = True
        self.stop_reason = stop_reason or self.stop_reason or STOP_ALL_TIERS

    def job_row(self, seeds: int) -> dict[str, str]:
        sent = [r for r in self.records if r.outcome in {RESULTS, NO_RESULTS, SEARCH_ERROR,
                                                         UNSUPPORTED_LAYOUT} and not r.cache]
        cached = [r for r in self.records if r.cache]
        return {
            "target_id": self.target.target_id,
            "rank": self.target.rank,
            "retailer_id": self.retailer_id,
            "queries_planned": str(len(self.all_queries)),
            "queries_run": str(len({r.query.tier for r in sent + cached})),
            "pages_sent": str(len(sent)),
            "pages_from_cache": str(len(cached)),
            "pages_not_sent": str(sum(1 for r in self.records if r.outcome.startswith(
                "NOT_SENT"))),
            "tiers_run": ";".join(dict.fromkeys(r.query.tier for r in sent + cached)),
            "stop_reason": self.stop_reason,
            "plausible_candidate_url": self.plausible_url,
            "historical_seeds": str(seeds),
        }


def evidence_name(body: bytes, content_type: str) -> str:
    ext = "json" if "json" in content_type.lower() else "html"
    return f"{hashlib.sha256(body).hexdigest()}.{ext}"


def _parse_time(text: str):
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def load_saved_responses(folders, csv_name, url_column, max_age_hours, now) -> dict:
    """``url -> (row, body path, folder)`` for saved responses that can be replayed.

    Only successful, readable responses are replayed (searches: RESULTS or NO_RESULTS;
    inspections: anything but FETCH_FAILED). The latest saved copy of each URL wins.
    """
    saved: dict[str, tuple[dict[str, str], Path, Path]] = {}
    for folder in folders:
        path = folder / csv_name
        if not path.is_file():
            continue
        for row in read_rows(path):
            if csv_name == "search_requests.csv" and row.get("outcome") not in {
                RESULTS, NO_RESULTS
            }:
                continue
            if csv_name == "product_inspections.csv" and row.get("outcome") in {
                "FETCH_FAILED", "STEP_FAILED", ""
            }:
                continue
            body = folder / row.get("evidence_file", "")
            moment = _parse_time(row.get("retrieved_at", ""))
            if not row.get("evidence_file") or not body.is_file() or moment is None:
                continue
            if max_age_hours is not None and (now - moment).total_seconds() > max_age_hours * 3600:
                continue
            url = row.get(url_column, "")
            if url and (url not in saved or row["retrieved_at"] > saved[url][0]["retrieved_at"]):
                saved[url] = (row, body, folder)
    return saved


def saved_page(retailer_id: str, url: str, entry) -> FetchedPage:
    row, body, _ = entry
    content_type = ("application/json; charset=utf-8" if body.suffix == ".json"
                    else "text/html; charset=utf-8")
    return FetchedPage(retailer_id, url, row.get("final_url") or url,
                       int(row.get("http_status") or 200), body.read_bytes(), content_type,
                       _parse_time(row["retrieved_at"]), 1)


def _copy_record(source: SearchRecord, query: SearchQuery, run_reason: str) -> SearchRecord:
    return SearchRecord(
        query, source.page_number, source.url, source.outcome, source.http_status,
        source.final_url, source.retrieved_at, source.attempts, source.content_sha256,
        source.evidence_file, source.hits, source.filtered_links, source.possibly_truncated,
        source.next_url, source.message, list(source.hit_items), cache="SAME_RUN",
        run_reason=run_reason,
    )


def _store_page(record: SearchRecord, job, page: FetchedPage, evidence_dir: Path) -> None:
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


def run_searches(jobs, fetcher, transport, args, evidence_dir: Path, log, *,
                 saved: dict | None = None) -> int:
    """Round-robin across retailers so per-retailer throttling waits overlap.

    An identical search already answered in this run is reused (``SAME_RUN``), and a saved
    response from an earlier run is replayed (``SAVED:<folder>``) instead of a new request.
    Reused responses count toward no limit, because nothing is sent.
    """
    sent = 0
    errors: Counter[str] = Counter()
    stopped: set[str] = set()
    answered: dict[str, SearchRecord] = {}
    saved = saved or {}
    pending = list(jobs)
    while pending:
        progressed = False
        for job in list(pending):
            planned = job.current()
            if planned is None:
                pending.remove(job)
                continue
            query, page_number, url = planned
            reason = job.run_reason(query, page_number)
            if url in answered:
                record = _copy_record(answered[url], query, reason)
            elif url in saved:
                record = SearchRecord(query, page_number, url, run_reason=reason,
                                      cache=f"SAVED:{saved[url][2]}")
                _store_page(record, job, saved_page(job.retailer_id, url, saved[url]),
                            evidence_dir)
                answered[url] = record
            elif args.replay_only:
                record = SearchRecord(query, page_number, url, NOT_SENT_REPLAY_ONLY,
                                      message="--replay-only: no saved response.",
                                      run_reason=reason)
            elif job.retailer_id in stopped:
                job.skip_rest(NOT_SENT_RETAILER, "Retailer search stopped after repeated errors.",
                              STOP_RETAILER)
                pending.remove(job)
                continue
            elif sent >= args.max_search_requests:
                job.skip_rest(NOT_SENT_LIMIT, "--max-search-requests reached.", STOP_SEARCH_LIMIT)
                pending.remove(job)
                continue
            elif transport.remaining <= 0:
                job.skip_rest(NOT_SENT_BUDGET, "--max-total-requests reached.", STOP_BUDGET)
                pending.remove(job)
                continue
            else:
                record = SearchRecord(query, page_number, url, run_reason=reason)
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
                    _store_page(record, job, page, evidence_dir)
                    if record.outcome in {RESULTS, NO_RESULTS}:
                        answered[url] = record
            log(f"  search {job.retailer_id:15} {job.target.target_id} {query.tier} "
                f"p{page_number}: {record.outcome} ({record.hits} results)"
                + (f" [{record.cache.split(':', 1)[0]}]" if record.cache else ""))
            job.advance(record)
            progressed = True
            if record.outcome == NOT_SENT_BUDGET:
                job.skip_rest(NOT_SENT_BUDGET, "--max-total-requests reached.", STOP_BUDGET)
        if not progressed:
            break
    for job in jobs:
        if args.replay_only and job.stop_reason in {STOP_ALL_TIERS, STOP_QUERY_LIMIT} and all(
            r.outcome == NOT_SENT_REPLAY_ONLY for r in job.records if r.query.tier
            == TIER_MODEL_CODE
        ):
            job.stop_reason = STOP_REPLAY
    return sent


def _inspect(job, url, fetcher, transport, args, state, evidence_dir, log):
    """``(inspection, row)`` for a product page, reusing this run's or a saved inspection.

    Returns ``(None, row)`` when the page could not be fetched or the budget is used up.
    """
    method = job.method
    row = {"target_id": job.target.target_id, "retailer_id": job.retailer_id,
           "product_url": url}
    cache = state.setdefault("inspected", {})
    if url in cache:
        inspection, meta = cache[url]
        return inspection, row | meta | {"cache": "SAME_RUN"}
    saved = state.get("saved_inspections", {}).get(url)
    if saved is not None:
        page = saved_page(job.retailer_id, url, saved)
        source = f"SAVED:{saved[2]}"
    elif getattr(args, "replay_only", False):
        return None, row | {"outcome": "NOT_SENT_REPLAY_ONLY", "message": "no saved page"}
    elif state["product_fetches"] >= args.max_product_fetches or transport.remaining <= 0:
        return None, row | {"outcome": "NOT_INSPECTED_BUDGET",
                            "message": "Product-fetch or total request budget reached."}
    else:
        try:
            page = fetcher.fetch(ScrapeRequest(job.retailer_id, url),
                                 allowed_hosts=frozenset({method.host}))
        except FetchError as error:
            state["product_fetches"] += error.code != BUDGET_CODE
            log(f"  inspect {job.retailer_id:14} {job.target.target_id}: FETCH_FAILED "
                f"{error.code}")
            return None, row | {"outcome": "FETCH_FAILED", "message": error.code,
                                "error": f"{error.code}: {error.message}"}
        state["product_fetches"] += 1
        source = ""
    name = evidence_name(page.body, page.content_type)
    (evidence_dir / name).write_bytes(page.body)
    inspection = inspect_product_page(method, url, page.text())
    meta = {
        "outcome": inspection.outcome, "variants": str(len(inspection.variants)),
        "retrieved_at": utc_text(page.retrieved_at), "content_sha256": page.content_sha256,
        "evidence_file": f"evidence/{name}", "message": inspection.message, "cache": source,
    }
    cache[url] = (inspection, meta)
    log(f"  inspect {job.retailer_id:14} {job.target.target_id}: {inspection.outcome} "
        f"({len(inspection.variants)} variants)" + (" [saved]" if source else ""))
    return inspection, row | meta


def select_and_inspect(candidates, job, fetcher, transport, args, state, evidence_dir, log):
    """Decide each candidate; inspect selected Shopify pages for their variants.

    A historical verified URL is re-checked like any candidate. A page whose title states
    another configuration of the same code is collected too (``COLLECT_CHECK_CONFIGURATION``),
    so its own specifications decide, unless --no-collect-title-differences is given.
    """
    inspections: list[dict[str, str]] = []
    variants: list[dict[str, str]] = []
    manifest: list[tuple[RankedCandidate, str, str]] = []
    chosen = 0
    seeds = 0
    method = job.method
    collect_differences = not getattr(args, "no_collect_title_differences", False)
    max_seeds = getattr(args, "max_seed_urls_per_target_retailer", 2)
    for candidate in candidates:
        title_differs = bool(configuration_difference(candidate))
        seeded = candidate.category == HISTORICAL_VERIFIED_URL
        if candidate.category not in COLLECTABLE:
            candidate.decision = "NOT_SELECTED"
            candidate.decision_reason = f"Category {candidate.category} is never collected " \
                                        "automatically; listed for review."
            continue
        if seeded:
            if seeds >= max_seeds:
                candidate.decision = "NOT_SELECTED_LIMIT"
                candidate.decision_reason = "--max-seed-urls-per-target-retailer reached."
                continue
            seeds += 1
        else:
            if chosen >= args.max_candidates_per_target_retailer:
                candidate.decision = "NOT_SELECTED_LIMIT"
                candidate.decision_reason = "--max-candidates-per-target-retailer reached."
                continue
            if title_differs and not collect_differences:
                candidate.decision = "REVIEW_DIFFERENT_CONFIGURATION_BY_TITLE"
                candidate.decision_reason = "Title clues state a different configuration: " \
                                            + configuration_difference(candidate) + "."
                continue
            chosen += 1
        collect = "COLLECT_CHECK_CONFIGURATION" if title_differs else "COLLECT"
        why = (f"Title states another configuration ({configuration_difference(candidate)}); "
               "collected so the page's own specifications decide."
               if title_differs else "")
        if method.kind == "woocommerce_html":  # No variant inspection: one page per product.
            candidate.decision = collect
            candidate.decision_reason = why or (
                "Historical verified URL; re-checked." if seeded
                else "Product page; collected as listed.")
            manifest.append((candidate, candidate.seed_url or candidate.hit.product_url, ""))
            continue
        url = candidate.hit.product_url
        inspection, row = _inspect(job, url, fetcher, transport, args, state, evidence_dir, log)
        inspections.append(row)
        if inspection is None:
            if row["outcome"] == "FETCH_FAILED":
                candidate.decision = "REVIEW_INSPECTION_FAILED"
                candidate.decision_reason = row.get("error", row["outcome"])
            else:
                candidate.decision = "NOT_INSPECTED_BUDGET" if row["outcome"] == (
                    "NOT_INSPECTED_BUDGET") else row["outcome"]
                candidate.decision_reason = row.get("message", "")
            continue
        if inspection.outcome == "SINGLE_VARIANT":
            candidate.decision = collect
            candidate.decision_reason = why or (
                "Historical verified URL; single-variant page." if seeded
                else "Single-variant product page.")
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
        collect_variants = [(v, reason) for v, decision, reason in decisions
                            if decision == "COLLECT"]
        seed_variant = next((v for v in inspection.variants
                             if candidate.seed_url and v.url == candidate.seed_url), None)
        if not method.multi_variant_collection:
            candidate.decision = "REVIEW_MULTI_VARIANT_NOT_SUPPORTED"
            candidate.decision_reason = (
                f"{len(inspection.variants)} variants; this retailer's adapter collects "
                "single-variant pages only. Variants are listed in variants.csv."
            )
        elif seed_variant is not None and not collect_variants:
            # The variant that matched before is still offered; its page decides again.
            candidate.decision = "COLLECT_VARIANT"
            candidate.decision_reason = f"Variant {seed_variant.variant_id}: historical verified " \
                                        "variant still offered; re-checked."
            manifest.append((candidate, seed_variant.url, seed_variant.variant_id))
        elif len(collect_variants) == 1:
            variant, reason = collect_variants[0]
            candidate.decision = "COLLECT_VARIANT"
            candidate.decision_reason = f"Variant {variant.variant_id}: {reason}"
            manifest.append((candidate, variant.url, variant.variant_id))
        elif collect_variants:
            candidate.decision = "REVIEW_SEVERAL_MATCHING_VARIANTS"
            candidate.decision_reason = "More than one variant states the target RAM/storage."
        else:
            candidate.decision = "REVIEW_NO_MATCHING_VARIANT"
            candidate.decision_reason = (
                f"{len(inspection.variants)} variants listed; none states the target RAM and "
                "storage. All are kept in variants.csv with their differences."
            )
    return manifest, inspections, variants


def load_seeds(args, targets, retailers) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Earlier verified offer URLs per (target, retailer): EXACT results only.

    A seed is re-fetched and re-matched like any candidate; it never carries a match forward.
    """
    wanted = {t.target_id for t in targets}
    found: dict[tuple[str, str], dict[str, str]] = {}

    def add(retailer_id: str, url: str, target_id: str, source: str) -> None:
        if target_id not in wanted or retailer_id not in retailers or not url:
            return
        try:
            ROUTES[retailer_id].product_request(url)
        except ValueError:
            return  # Only URLs the retailer's adapter accepts.
        found.setdefault((target_id, retailer_id), {}).setdefault(url, source)

    for path in args.seed_urls:
        for row in read_rows(path):
            if row.get("match_status", "EXACT") == "EXACT":
                add(row.get("retailer_id", ""), row.get("offer_url") or row.get("product_url", ""),
                    row.get("target_id") or row.get("expected_target_id", ""), path.name)
    if args.seed_store and args.seed_store.exists():
        connection = sqlite3.connect(f"file:{args.seed_store}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT DISTINCT o.retailer_id, o.offer_url, m.target_id FROM match_results m "
                "JOIN observations o ON o.observation_id = m.observation_id "
                "WHERE m.match_status = 'EXACT' AND m.target_id != ''"
            ).fetchall()
        finally:
            connection.close()
        for retailer_id, url, target_id in rows:
            add(retailer_id, url, target_id, args.seed_store.name)
    return {key: list(urls.items()) for key, urls in found.items()}


def merge_seeds(ranked, target, retailer_id, seeds) -> list[RankedCandidate]:
    """Add historical seeds to a job's ranked candidates; a seed already found is flagged."""
    by_url = {strip_variant(c.hit.product_url): c for c in ranked}
    for url, source in seeds:
        existing = by_url.get(strip_variant(url))
        if existing is not None:
            existing.seed_url = existing.seed_url or url
            existing.flags.append("ALSO_HISTORICAL_VERIFIED_URL")
            existing.reasons.append(f"Also matched this configuration in {source}.")
        else:
            candidate = seed_candidate(target, retailer_id, url, source)
            ranked.append(candidate)
            by_url[strip_variant(url)] = candidate
    ranked.sort(key=lambda c: c.sort_key)
    for index, candidate in enumerate(ranked, start=1):
        candidate.rank = index
    return ranked


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

    jobs = [SearchJob(target, retailer, args.max_search_pages,
                      args.max_queries_per_target_retailer)
            for target in targets for retailer in retailers]
    plan_rows = []
    worst_search = 0
    for job in jobs:
        for order, query in enumerate(job.all_queries, start=1):
            pages = job.max_pages
            limited = order > len(job.queries)
            worst_search += 0 if limited else pages
            plan_rows.append({
                "target_id": query.target_id, "rank": job.target.rank,
                "retailer_id": query.retailer_id, "order": str(order), "tier": query.tier,
                "query": query.text,
                "verified_identifier": "TRUE" if query.verified_identifier else "FALSE",
                "runs": "NOT_RUN_QUERY_LIMIT" if limited else (
                    "ALWAYS" if query.tier == TIER_MODEL_CODE
                    else "ONLY_IF_NO_PLAUSIBLE_CANDIDATE"),
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
        "max_queries_per_target_retailer": args.max_queries_per_target_retailer,
        "max_seed_urls_per_target_retailer": args.max_seed_urls_per_target_retailer,
        "retry_attempts_per_request": FetchConfig().max_attempts,
        "min_seconds_between_requests_per_retailer": args.min_seconds_between_requests,
        "search_cache_folders": [str(path) for path in args.search_cache],
        "cache_max_age_hours": args.cache_max_age_hours,
        "replay_only": args.replay_only,
        "collect_title_differences": not args.no_collect_title_differences,
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

    # Live search (or replay of saved responses).
    evidence_dir = out / "evidence"
    evidence_dir.mkdir()
    budget = BudgetedTransport(transport or UrllibTransport(), args.max_total_requests)
    config = FetchConfig(min_request_interval_seconds=args.min_seconds_between_requests)
    fetcher = Fetcher(budget, config, clock)
    now = clock.now().astimezone(UTC)
    saved_searches = load_saved_responses(args.search_cache, "search_requests.csv", "url",
                                          args.cache_max_age_hours, now)
    saved_inspections = load_saved_responses(args.search_cache, "product_inspections.csv",
                                             "product_url", args.cache_max_age_hours, now)
    seeds = load_seeds(args, targets, retailers)
    log(f"Saved responses available: {len(saved_searches)} searches, "
        f"{len(saved_inspections)} product pages; historical seed URLs: "
        f"{sum(len(v) for v in seeds.values())}.")
    log("Searching...")
    searches_sent = run_searches(jobs, fetcher, budget, args, evidence_dir, log,
                                 saved=saved_searches)
    records = [record for job in jobs for record in job.records]
    write_csv(out / "search_jobs.csv", SEARCH_JOB_COLUMNS,
              (job.job_row(len(seeds.get((job.target.target_id, job.retailer_id), [])))
               for job in jobs))
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
    state = {"product_fetches": 0, "saved_inspections": saved_inspections}
    all_candidates: list[RankedCandidate] = []
    manifest: list[tuple[RankedCandidate, str, str]] = []
    inspections: list[dict[str, str]] = []
    variant_rows: list[dict[str, str]] = []
    for job in jobs:
        try:
            ranked = merge_seeds(
                rank_target_hits(job.target, job.retailer_id, job.records), job.target,
                job.retailer_id, seeds.get((job.target.target_id, job.retailer_id), []),
            )
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
    inspection_columns = [
        "target_id", "retailer_id", "product_url", "outcome", "variants", "retrieved_at",
        "content_sha256", "evidence_file", "message", "cache",
    ]
    write_csv(out / "product_inspections.csv", inspection_columns, (
        {column: row.get(column, "") for column in inspection_columns} for row in inspections
    ))
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
        "search_responses_reused": dict(Counter(
            r.cache.split(":", 1)[0] for r in records if r.cache
        )),
        "product_inspections": state["product_fetches"],
        "search_outcomes": dict(Counter(f"{r.query.retailer_id}:{r.outcome}" for r in records)),
        "search_tiers_run": dict(Counter(
            r.query.tier for r in records if r.outcome in {RESULTS, NO_RESULTS}
        )),
        "search_stop_reasons": dict(Counter(job.stop_reason for job in jobs)),
        "historical_seed_urls": sum(len(v) for v in seeds.values()),
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
    if status == "SEARCH_ONLY" and args.replay_only:
        status = "REPLAY_ONLY"
    summary["run_status"] = status
    summary["finished_at_utc"] = utc_text(clock.now())
    (out / "discovery_summary.json").write_text(json.dumps(summary, indent=2) + "\n",
                                                encoding="utf-8")
    log(f"Search jobs stopped: {dict(Counter(job.stop_reason for job in jobs))}.")
    log(f"Discovery {status}: {budget.sent} requests sent ({searches_sent} searches, "
        f"{state['product_fetches']} inspections); candidates "
        f"{dict(Counter(c.category for c in all_candidates))}; manifest rows "
        f"{len(manifest_rows)}.")
    if manifest_issue:
        log(f"  Manifest not collected: {manifest_issue}")
    log(f"Reports written to {out}.")
    return 0 if status in {"SUCCESS", "SEARCH_ONLY", "REPLAY_ONLY"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
