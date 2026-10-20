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
from laptop_tracker.collection_replay import (
    load_saved_collections,
    merge_collection_rounds,
    replay_outcomes,
    write_replayed_collection,
)
from laptop_tracker.extraction import extract_offer_attributes
from laptop_tracker.match_run import (
    ObservationFileError,
    TargetSnapshotError,
    matcher_from_validated,
    read_observations_csv,
)
from laptop_tracker.matching import configuration_outcome, is_model_code
from laptop_tracker.normalization import CORE_MATCH_COLUMNS
from laptop_tracker.observation_store import observation_id
from laptop_tracker.price_tracking import (
    PriceTrackingInputError,
    matching_context,
    utc_text,
    validate_selected_list,
)
from laptop_tracker.product_discovery import (
    ACCESSORY,
    CANDIDATE_COLUMNS,
    COLLECTABLE,
    COVERAGE_COLUMNS,
    FAMILY_NAME_LEAD,
    FAMILY_SPEC_CANDIDATE,
    HISTORICAL_VERIFIED_URL,
    NOT_A_LAPTOP,
    SUPPLIED_URL,
    RankedCandidate,
    SearchTarget,
    candidate_row,
    configuration_difference,
    inspect_product_page,
    normalize_supplied_url,
    plausible_candidate,
    rank_target_hits,
    results_summary,
    seed_candidate,
    strip_variant,
    supplied_candidate,
    url_shows_code,
    variant_decision,
)
from laptop_tracker.product_names import default_registry
from laptop_tracker.retailer_search import (
    BUDGET_CODE,
    FIRST_TIERS,
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
    search_cache_key,
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
DEFAULT_SUPPLIED_URLS = ROOT / "config" / "supplied_candidate_urls.csv"
SUPPLIED_URL_COLUMNS = ("supplied_id", "retailer_id", "url", "search_target_id", "supplied_by",
                        "supplied_on", "notes")
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
# How discovery ended for the configuration that started the job (2026-10-19). Identity only: a
# suitable offer is an exact match for the configuration; its price may still be excluded.
SUITABLE_OFFER_FOUND = "SUITABLE_OFFER_FOUND"
# In-memory only: the fields a collected page established, attached to its result row.
OFFER_ATTRIBUTES_KEY = "__offer_attributes__"
# The planned queries all ran; this never means the retailer's whole catalog was searched
# (result caps and unread pages are reported in search_coverage).
QUERY_PLAN_EXHAUSTED = "QUERY_PLAN_EXHAUSTED"
# The plausible candidate that stopped the search could not be collected (held for review,
# variants unreadable, per-retailer candidate limit) and no tiers were left to resume.
PLAUSIBLE_NOT_COLLECTED = "PLAUSIBLE_CANDIDATE_NOT_COLLECTED"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
PROVIDER_FAILURE = "PROVIDER_FAILURE"
SEARCH_INCOMPLETE_REPLAY_ONLY = "SEARCH_INCOMPLETE_REPLAY_ONLY"
NOT_EVALUATED = "NOT_EVALUATED_SEARCH_ONLY"
SEARCH_JOB_COLUMNS = (
    "target_id", "rank", "retailer_id", "queries_planned", "queries_run", "pages_sent",
    "pages_from_cache", "pages_not_sent", "tiers_run", "stop_reason", "plausible_candidate_url",
    "historical_seeds",
    # Continuation and coverage (2026-10-19).
    "supplied_urls", "search_coverage", "initial_stop_reason", "resumed", "resume_reason",
    "tiers_after_resume", "search_pages_after_resume", "search_pages_not_sent_after_resume",
    "inspections_after_resume", "collection_requests_after_resume", "discovery_rounds",
    "candidate_outcomes", "suitable_offer_url", "suitable_offer_observation_id",
    "discovery_outcome", "identity_kind",
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
    parser.add_argument("--supplied-urls", type=Path, action="append", default=[],
                        help="CSV of known product pages supplied by people (repeatable; default "
                             f"{DEFAULT_SUPPLIED_URLS.relative_to(ROOT)} when it exists). Each URL "
                             "is a discovery seed for the scoped configuration it names "
                             "(search_target_id) or whose model code its URL handle shows; when "
                             "native search misses it, it is checked directly (bounded fallback). "
                             "Never an approved match.")
    parser.add_argument("--no-supplied-urls", action="store_true",
                        help="Do not read the default supplied-URL file.")
    parser.add_argument("--max-supplied-urls-per-target-retailer", type=positive, default=2)
    parser.add_argument("--max-discovery-rounds", type=positive, default=3,
                        help="Search-and-collect rounds (default 3). A search that stopped at a "
                             "plausible candidate resumes its remaining query tiers in the next "
                             "round when collection did not confirm the configuration; every "
                             "round stays within the same request limits.")
    parser.add_argument("--replay-collection", type=Path, action="append", default=[],
                        help="With --replay-only --collect: an earlier collection folder "
                             "(repeatable) whose saved observation records are reused for the "
                             "same URLs; pages saved by --search-cache inspections are parsed. "
                             "No request is sent and nothing is stored.")
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
    if args.replay_only and args.collect and not args.replay_collection:
        parser.error("--replay-only collects only from saved evidence: add --replay-collection "
                     "<earlier collection folder>, or run collect_and_match.py separately.")
    if args.replay_collection and not args.replay_only:
        parser.error("--replay-collection is for offline replays (--replay-only).")
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
            identity_kind=default_registry().kind(str(row.get("brand", "") or ""),
                                                  str(row["model"])),
            recorded_code=recorded_code(row),
            record=dict(row),
        ))
    ranks = {item.target_id: item.rank for item in selected.configurations}
    return targets, excluded, ranks, target_master



def recorded_code(row) -> str:
    """The manufacturer code a family-name target records, when it is one code."""
    value = str(row.get("manufacturer_model_code", "") or "").strip()
    return value if value and value != "nan" and is_model_code(value) else ""


class SearchJob:
    """The search requests for one target at one retailer, in tier order.

    The complete model code always runs first, without specification filters. Each later tier
    (part number, code with RAM/storage, family, family with specifications) runs only while the
    results so far hold no plausible candidate: a page showing the complete code (or verified
    part number) whose title does not state another configuration. Results showing only another
    RAM/storage configuration, an unexplained suffix, or a code only in the URL keep the search
    going (2026-10-19). Every query records why it ran, and the job records why it stopped.

    Continuation (2026-10-19): a plausible title is not suitability. When collection of the
    plausible candidate does not confirm the job's own configuration (another configuration, an
    unresolved or unmatched page, a failed fetch), the job resumes its remaining tiers in the next
    discovery round, ignoring pages already evaluated. The original candidate and its outcome are
    kept; a match to another configuration stays valid for that configuration only.
    """

    def __init__(self, target: SearchTarget, retailer_id: str, max_pages: int,
                 max_queries: int = 5) -> None:
        method = SEARCH_METHODS[retailer_id]
        self.target = target
        self.method = method
        self.retailer_id = retailer_id
        self.all_queries = search_queries(
            target.target_id, retailer_id, model=target.model, part_number=target.part_number,
            brand=target.brand, series=target.series, core=target.core, kind=target.kind,
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
        self.plausible_urls: list[str] = []
        # Continuation state.
        self.initial_stop_reason = ""
        self.resumed = False
        self.resume_reasons: list[str] = []
        self.resume_record_index = 0
        self.evaluated: set[str] = set()  # strip_variant URLs collected for this configuration
        self.rounds = 1
        self.round_limit_reached = False
        self.inspections_after_resume = 0
        self.collection_requests_after_resume = 0
        self.candidate_outcomes: list[str] = []
        self.suitable_url = ""
        self.suitable_observation_id = ""
        self.collected = False
        self.supplied = 0
        self.supplied_missed = 0
        self.outcome = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.target.target_id, self.retailer_id)

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
        if query.tier in FIRST_TIERS:
            return ("first query: complete model code without specification filters"
                    if query.tier == TIER_MODEL_CODE else
                    "first query: brand and catalog product name (no model code recorded)")
        tiers = ", ".join(dict.fromkeys(r.query.tier for r in self.records))
        resumed = (f"resumed after collection: {self.resume_reasons[-1]}; "
                   if self.resumed and len(self.records) == self.resume_record_index else "")
        return (f"{resumed}no plausible candidate after {tiers}; "
                f"{results_summary(self.target, self.records)}")

    def advance(self, record: SearchRecord) -> None:
        self.records.append(record)
        if record.outcome == RESULTS and record.next_url and self.page < self.max_pages:
            self.page += 1
            self.next_url = record.next_url
            return
        if record.outcome == RESULTS and record.next_url:
            # More result pages exist than --max-search-pages reads (2026-10-19, second review:
            # Laptop Factory "HP" page 3 was unread yet reported complete).
            record.possibly_truncated = True
            record.message = (record.message + " " if record.message else "") + (
                "Next result page not read (--max-search-pages).")
        self.page, self.next_url = 1, ""
        self.index += 1
        url = plausible_candidate(self.target, self.records, frozenset(self.evaluated))
        if url:
            self.done, self.stop_reason, self.plausible_url = True, STOP_PLAUSIBLE, url
            self.plausible_urls.append(url)
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

    def can_resume(self) -> bool:
        """Stopped at a plausible candidate that collection evaluated without confirming this
        configuration, with query tiers left."""
        return (
            self.done and self.stop_reason == STOP_PLAUSIBLE and not self.suitable_url
            and self.index < len(self.queries)
            and strip_variant(self.plausible_url) in self.evaluated
        )

    def resume(self, reason: str, round_number: int) -> None:
        if not self.resumed:
            self.initial_stop_reason = self.stop_reason
            self.resume_record_index = len(self.records)
        self.resumed = True
        self.resume_reasons.append(reason)
        self.rounds = round_number
        self.done, self.stop_reason, self.plausible_url = False, "", ""

    def search_coverage(self) -> str:
        """Whether the searches cover the retailer's results: provider failures, unsent pages,
        result caps, and supplied pages the native search did not return."""
        notes = []
        if any(r.outcome in {SEARCH_ERROR, UNSUPPORTED_LAYOUT} for r in self.records):
            notes.append(PROVIDER_FAILURE)
        if any(r.outcome.startswith("NOT_SENT") for r in self.records):
            notes.append("INCOMPLETE_NOT_SENT")
        if any(r.possibly_truncated for r in self.records):
            # Predictive search returns at most its page size and has no pagination; HTML search
            # has unread pages. Either way more matching products may exist than were read.
            notes.append("MORE_PAGES_NOT_READ" if self.method.paginated
                         else "RESULT_CAP_REACHED")
        if self.supplied_missed:
            notes.append("NATIVE_SEARCH_MISSED_SUPPLIED_URL")
        return ";".join(notes) or "COMPLETE"

    def finish_outcome(self) -> str:
        """How discovery ended for this configuration (identity only)."""
        if self.suitable_url:
            return SUITABLE_OFFER_FOUND
        if not self.collected:
            return NOT_EVALUATED
        outcomes = {r.outcome for r in self.records}
        if NOT_SENT_REPLAY_ONLY in outcomes or self.stop_reason == STOP_REPLAY:
            return SEARCH_INCOMPLETE_REPLAY_ONLY
        if self.stop_reason == STOP_RETAILER:
            return PROVIDER_FAILURE
        if self.round_limit_reached or self.stop_reason in {
            STOP_BUDGET, STOP_SEARCH_LIMIT, STOP_QUERY_LIMIT
        }:
            return BUDGET_EXHAUSTED
        if self.stop_reason == STOP_PLAUSIBLE:
            return PLAUSIBLE_NOT_COLLECTED
        sent = [r for r in self.records if r.outcome in {RESULTS, NO_RESULTS, SEARCH_ERROR,
                                                         UNSUPPORTED_LAYOUT}]
        if sent and all(r.outcome in {SEARCH_ERROR, UNSUPPORTED_LAYOUT} for r in sent):
            return PROVIDER_FAILURE
        return QUERY_PLAN_EXHAUSTED

    def job_row(self, seeds: int) -> dict[str, str]:
        sent = [r for r in self.records if r.outcome in {RESULTS, NO_RESULTS, SEARCH_ERROR,
                                                         UNSUPPORTED_LAYOUT} and not r.cache]
        cached = [r for r in self.records if r.cache]
        after = self.records[self.resume_record_index:] if self.resumed else []
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
            "tiers_run": ";".join(
                query.tier for query in self.queries
                if any(r.query.tier == query.tier for r in sent + cached)),
            "stop_reason": self.stop_reason,
            "plausible_candidate_url": ";".join(dict.fromkeys(self.plausible_urls)),
            "historical_seeds": str(seeds),
            "supplied_urls": str(self.supplied),
            "search_coverage": self.search_coverage(),
            "initial_stop_reason": self.initial_stop_reason or self.stop_reason,
            "resumed": "TRUE" if self.resumed else "FALSE",
            "resume_reason": " | ".join(self.resume_reasons),
            "tiers_after_resume": ";".join(
                f"{r.query.tier}:{r.outcome}" + (":cached" if r.cache else "") for r in after
            ),
            "search_pages_after_resume": str(sum(1 for r in after if r in sent)),
            "search_pages_not_sent_after_resume": str(
                sum(1 for r in after if r.outcome.startswith("NOT_SENT"))),
            "inspections_after_resume": str(self.inspections_after_resume),
            "collection_requests_after_resume": str(self.collection_requests_after_resume),
            "discovery_rounds": str(self.rounds),
            "candidate_outcomes": " | ".join(self.candidate_outcomes),
            "suitable_offer_url": self.suitable_url,
            "suitable_offer_observation_id": self.suitable_observation_id,
            "discovery_outcome": self.outcome or self.finish_outcome(),
            "identity_kind": self.target.kind,
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
                 saved: dict | None = None, answered: dict | None = None,
                 sent_before: int = 0) -> int:
    """Round-robin across retailers so per-retailer throttling waits overlap.

    An identical search already answered in this run is reused (``SAME_RUN``), and a saved
    response from an earlier run is replayed (``SAVED:<folder>``) instead of a new request.
    Reused responses count toward no limit, because nothing is sent. ``answered`` is shared by
    every round of a run (2026-10-19, second review: later rounds re-sent 38 identical searches),
    searches are identified by ``search_cache_key`` (query case and spacing ignored), and
    ``sent_before`` makes ``--max-search-requests`` a limit for the whole run, not each round.
    """
    sent = 0
    errors: Counter[str] = Counter()
    stopped: set[str] = set()
    answered = {} if answered is None else answered
    saved = {search_cache_key(url): entry for url, entry in (saved or {}).items()}
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
            key = search_cache_key(url)
            if key in answered:
                record = _copy_record(answered[key], query, reason)
            elif key in saved:
                record = SearchRecord(query, page_number, url, run_reason=reason,
                                      cache=f"SAVED:{saved[key][2]}")
                _store_page(record, job, saved_page(job.retailer_id, url, saved[key]),
                            evidence_dir)
                answered[key] = record
            elif args.replay_only:
                record = SearchRecord(query, page_number, url, NOT_SENT_REPLAY_ONLY,
                                      message="--replay-only: no saved response.",
                                      run_reason=reason)
            elif job.retailer_id in stopped:
                job.skip_rest(NOT_SENT_RETAILER, "Retailer search stopped after repeated errors.",
                              STOP_RETAILER)
                pending.remove(job)
                continue
            elif sent + sent_before >= args.max_search_requests:
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
                        answered[key] = record
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
            in FIRST_TIERS
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
    supplied_count = 0
    max_supplied = getattr(args, "max_supplied_urls_per_target_retailer", 2)
    for candidate in candidates:
        title_differs = bool(configuration_difference(candidate))
        seeded = candidate.category == HISTORICAL_VERIFIED_URL
        supplied = candidate.category == SUPPLIED_URL
        if candidate.category == ACCESSORY:
            candidate.decision = "REJECTED_ACCESSORY"
            candidate.decision_reason = f"{candidate.code_evidence}; no page request."
            continue
        if candidate.category == NOT_A_LAPTOP:
            candidate.decision = "REJECTED_PRODUCT_TYPE"
            candidate.decision_reason = (
                f"Not a laptop ({candidate.code_evidence}); no page request.")
            continue
        if candidate.category == FAMILY_NAME_LEAD:
            candidate.decision = "REVIEW_CATALOG_IDENTITY"
            candidate.decision_reason = (
                "The title names the family but not the target's brand; kept for review. "
                "Not fetched.")
            continue
        if candidate.category == FAMILY_SPEC_CANDIDATE and title_differs:
            # A family listing whose title states another configuration is not fetched; it does
            # not end the search either (2026-10-20).
            candidate.decision = "REVIEW_DIFFERENT_CONFIGURATION_BY_TITLE"
            candidate.decision_reason = ("Title clues state a different configuration: "
                                         + configuration_difference(candidate) + ".")
            continue
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
        elif supplied:
            if supplied_count >= max_supplied:
                candidate.decision = "NOT_SELECTED_LIMIT"
                candidate.decision_reason = "--max-supplied-urls-per-target-retailer reached."
                continue
            supplied_count += 1
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
                else "Supplied known page; checked directly (native search missed it)."
                if supplied else "Product page; collected as listed.")
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
        if inspection.outcome == "SINGLE_VARIANT" and method.multi_variant_collection and (
            inspection.named_options and inspection.variants
            and inspection.variants[0].variant_id
        ):
            # One offer whose URL names its variant (named options such as "8GB DDR5 RAM / 512GB
            # SSD"): collect that exact variant URL, so the adapter binds the selected variant
            # instead of refusing the bare page (2026-10-19, second review).
            variant = inspection.variants[0]
            decision, reason = variant_decision(variant, job.target)
            candidate.decision = "COLLECT_VARIANT"
            candidate.decision_reason = (
                f"Variant {variant.variant_id} (only variant; option '{variant.title}'): "
                f"{reason}" + (f" {why}" if why else ""))
            manifest.append((candidate, variant.url, variant.variant_id))
            continue
        if inspection.outcome == "SINGLE_VARIANT":
            candidate.decision = collect
            candidate.decision_reason = why or (
                "Historical verified URL; single-variant page." if seeded
                else "Supplied known page; single-variant page checked directly (native search "
                     "missed it)." if supplied else "Single-variant product page.")
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
            # Only rows that say EXACT seed; a file without match_status is refused earlier
            # (check_seed_files), so an unverified URL is never labeled historically verified.
            if row.get("match_status") == "EXACT":
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


def check_seed_files(paths) -> None:
    """A seed file must state each row's match status; known pages go to --supplied-urls."""
    for path in paths:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            columns = csv.DictReader(handle).fieldnames or []
        if "match_status" not in columns:
            raise ValueError(
                f"{path} has no match_status column. --seed-urls re-checks earlier EXACT results "
                "only; list known pages without a verified match in --supplied-urls."
            )


def load_supplied(args, targets, retailers):
    """Supplied known pages per (target, retailer), plus one report row per supplied URL.

    A URL is normalized (tracking parameters and fragment removed; ``variant`` and the path
    kept) and must be accepted unchanged by its retailer's adapter. It seeds the scoped
    configuration named by ``search_target_id`` (a search hint, never an assignment), or else
    every scoped configuration whose complete model code its URL handle shows. It is never a
    match: collection and the matcher decide.
    """
    paths = list(args.supplied_urls)
    if not paths and not args.no_supplied_urls and DEFAULT_SUPPLIED_URLS.is_file():
        paths = [DEFAULT_SUPPLIED_URLS]
    by_id = {t.target_id: t for t in targets}
    found: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    report: list[dict[str, str]] = []
    for path in paths:
        for row in read_rows(path):
            missing = [c for c in ("supplied_id", "retailer_id", "url") if c not in row]
            if missing:
                raise ValueError(f"{path} lacks column(s) {', '.join(missing)}.")
            retailer = row["retailer_id"].strip()
            normalized, removed = normalize_supplied_url(row["url"])
            entry = {
                "supplied_id": row["supplied_id"].strip(), "retailer_id": retailer,
                "supplied_url": row["url"].strip(), "normalized_url": normalized,
                "removed_parameters": ";".join(removed),
                "search_target_hint": row.get("search_target_id", "").strip(),
                "source_file": str(path), "assigned_target_ids": "", "assignment_basis": "",
                "found_by_native_search": "", "candidate_decision": "",
                "collection_request_id": "",
            }
            report.append(entry)
            if retailer not in retailers:
                entry["assignment_basis"] = "RETAILER_NOT_IN_THIS_RUN"
                continue
            try:
                accepted = ROUTES[retailer].product_request(normalized).url == normalized
            except (KeyError, ValueError):
                accepted = False
            if not accepted:
                entry["assignment_basis"] = "URL_REJECTED_BY_ADAPTER"
                continue
            hint = entry["search_target_hint"]
            if hint:
                chosen = [by_id[hint]] if hint in by_id else []
                basis = "SEARCH_TARGET_HINT" if chosen else "HINT_NOT_IN_SCOPE"
            else:
                chosen = [t for t in targets if url_shows_code(normalized, t.model)]
                basis = "URL_HANDLE_SHOWS_MODEL_CODE" if chosen else "UNASSIGNED_NO_CODE_IN_URL"
            entry["assignment_basis"] = basis
            entry["assigned_target_ids"] = ";".join(t.target_id for t in chosen)
            for target in chosen:
                found.setdefault((target.target_id, retailer), []).append(
                    (entry["supplied_id"], normalized, basis))
    return found, report


def merge_supplied(ranked, job, supplied, report) -> list[RankedCandidate]:
    """Add supplied pages to a job's candidates; a page native search already returned is only
    flagged. A page the search missed is checked directly (the bounded fallback)."""
    by_url = {strip_variant(c.hit.product_url): c for c in ranked}
    rows = {(row["supplied_id"], row["normalized_url"]): row for row in report}
    for supplied_id, url, basis in supplied:
        row = rows.get((supplied_id, url))
        existing = by_url.get(strip_variant(url))
        job.supplied += 1
        if existing is not None and not existing.records:
            # Already listed only as a historical seed: the native search still missed it.
            job.supplied_missed += 1
            existing.flags.append("ALSO_SUPPLIED_URL")
            existing.reasons.append(f"Also supplied as a known page ({supplied_id}); native "
                                    "search did not return it.")
            if row is not None:
                row["found_by_native_search"] = "FALSE"
            continue
        if existing is not None:
            if "variant=" in url and not existing.seed_url:
                existing.seed_url = url
            existing.flags.append("ALSO_SUPPLIED_URL")
            existing.reasons.append(f"Also supplied as a known page ({supplied_id}).")
            if row is not None:
                row["found_by_native_search"] = "TRUE"
            continue
        job.supplied_missed += 1
        candidate = supplied_candidate(job.target, job.retailer_id, url, supplied_id, basis)
        ranked.append(candidate)
        by_url[strip_variant(url)] = candidate
        if row is not None:
            row["found_by_native_search"] = "FALSE"
    ranked.sort(key=lambda c: c.sort_key)
    for index, candidate in enumerate(ranked, start=1):
        candidate.rank = index
    return ranked


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
        targets, excluded, ranks, target_master = load_targets(args)
        evidence_approvals.read_approvals()
        check_seed_files(args.seed_urls)
        supplied, supplied_report = load_supplied(args, targets, retailers)
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
                    "ALWAYS" if order == 1
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

    # Live search (or replay of saved responses), in bounded discovery rounds.
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
        f"{sum(len(v) for v in seeds.values())}; supplied URLs: {len(supplied_report)}.")
    state = {"product_fetches": 0, "saved_inspections": saved_inspections}
    run = DiscoveryRun(out, jobs, seeds, supplied, supplied_report)
    collection = RoundCollector(args, out, budget, clock, log, ranks, target_master,
                                saved_inspections)
    searches_sent = 0
    answered: dict[str, SearchRecord] = {}  # One search cache for every round of the run.
    manifest_issue = ""
    active = list(jobs)
    for round_number in range(1, args.max_discovery_rounds + 1):
        run.rounds = round_number
        log(f"Discovery round {round_number}: searching {len(active)} job(s)...")
        searches_sent += run_searches(active, fetcher, budget, args, evidence_dir, log,
                                      saved=saved_searches, answered=answered,
                                      sent_before=searches_sent)
        log("Ranking and inspecting candidates...")
        round_manifest: list[tuple[RankedCandidate, str, str]] = []
        for job in active:
            before = state["product_fetches"]
            try:
                ranked = run.round_candidates(job, round_number)
                chosen, inspected, variants = select_and_inspect(
                    ranked, job, fetcher, budget, args, state, evidence_dir, log
                )
            except Exception as exc:  # One retailer's failure must not discard the others.
                log(f"  {job.retailer_id} {job.target.target_id}: candidate step failed "
                    f"({type(exc).__name__}); kept for review.")
                run.inspections.append({"target_id": job.target.target_id,
                                        "retailer_id": job.retailer_id, "outcome": "STEP_FAILED",
                                        "message": f"{type(exc).__name__}: {exc}"})
                continue
            if round_number > 1:
                job.inspections_after_resume += state["product_fetches"] - before
            run.add_candidates(job, ranked)
            round_manifest += chosen
            run.inspections += inspected
            run.variants += variants
        new_rows = run.add_manifest_rows(round_manifest, args.max_product_fetches
                                         - state["product_fetches"])
        if new_rows:
            try:
                validate_manifest(
                    [row | {"_row_number": str(i)}
                     for i, row in enumerate(run.manifest_rows, start=2)],
                    ranks,
                )
            except ManifestError as error:
                manifest_issue = str(error)
        run.write_files()
        if not args.collect or manifest_issue or not new_rows:
            break
        results, statuses = collection.collect(round_number, new_rows)
        run.record_collection(results, statuses)
        resumable = [job for job in jobs if job.can_resume()]
        if not resumable:
            break
        if round_number == args.max_discovery_rounds:
            for job in resumable:
                job.round_limit_reached = True
            log(f"  {len(resumable)} job(s) could resume but --max-discovery-rounds was reached.")
            break
        for job in resumable:
            job.resume(run.resume_reason(job), round_number + 1)
            log(f"  resume {job.retailer_id:15} {job.target.target_id}: {job.resume_reasons[-1]}")
        active = resumable
    for job in jobs:
        job.outcome = job.finish_outcome()
    run.write_files()
    records = [record for job in jobs for record in job.records]

    approvals = evidence_approvals.read_approvals()
    approval_urls = {(row["retailer_id"], row["offer_url"]) for row in approvals}
    all_candidates = run.candidates
    manifest_rows = run.manifest_rows
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
        "discovery_rounds": run.rounds,
        "continuation": {
            "policy": "A search that stopped at a plausible candidate resumes its remaining "
                      "query tiers in the next round when collection did not confirm the "
                      "configuration (another configuration, unresolved, unmatched, or a "
                      "failed fetch). Pages already evaluated are not fetched again; every "
                      "round uses the same request limits; at most --max-discovery-rounds.",
            "jobs_resumed": sum(job.resumed for job in jobs),
            "round_limit_reached": sum(job.round_limit_reached for job in jobs),
        },
        "discovery_outcomes": dict(Counter(job.outcome for job in jobs)),
        "search_coverage": dict(Counter(job.search_coverage() for job in jobs)),
        "historical_seed_urls": sum(len(v) for v in seeds.values()),
        "supplied_urls": dict(Counter(row["assignment_basis"] for row in supplied_report)),
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

    # Collection rounds merged into one collection folder.
    status = "SEARCH_ONLY"
    if collection.round_dirs:
        merged = merge_collection_rounds(collection.round_dirs, out / "collection")
        summary |= collection.summary()
        rows = read_rows(out / "collection" / "match_results.csv")
        summary["match_statuses"] = dict(Counter(r["match_status"] for r in rows))
        summary["expected_target_checks"] = dict(
            Counter(r["expected_target_check"] for r in rows)
        )
        summary["review_rows"] = merged["review_rows"]
        summary["owner_decisions"]["collected_offers_with_listing_approval"] = sorted(
            {r["offer_url"] for r in rows if (r["retailer_id"], r["offer_url"]) in approval_urls}
        )
        status = {"SUCCESS": "SUCCESS", "PARTIAL_SUCCESS": "PARTIAL_SUCCESS",
                  "REPLAY_ONLY": "REPLAY_ONLY"}.get(merged["run_status"], "COLLECTION_FAILED")
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
    log(f"Search jobs stopped: {dict(Counter(job.stop_reason for job in jobs))}; discovery "
        f"outcomes: {summary['discovery_outcomes']}; resumed: "
        f"{summary['continuation']['jobs_resumed']}.")
    log(f"Discovery {status}: {budget.sent} requests sent ({searches_sent} searches, "
        f"{state['product_fetches']} inspections); candidates "
        f"{dict(Counter(c.category for c in all_candidates))}; manifest rows "
        f"{len(manifest_rows)} over {len(collection.round_dirs) or 0} collection round(s).")
    if manifest_issue:
        log(f"  Manifest not collected: {manifest_issue}")
    log(f"Reports written to {out}.")
    return 0 if status in {"SUCCESS", "SEARCH_ONLY", "REPLAY_ONLY"} else 2


class DiscoveryRun:
    """Candidates, manifest rows, and output files of one discovery run, across rounds.

    Lineage: each candidate records its round and the collection request that fetched its page
    (``collection_request_id``; an earlier request when the page was already collected for
    another configuration), so search job -> candidate -> request -> observation stays explicit.
    """

    def __init__(self, out: Path, jobs, seeds, supplied, supplied_report) -> None:
        self.out = out
        self.jobs = jobs
        self.seeds = seeds
        self.supplied = supplied
        self.supplied_report = supplied_report
        self.by_job: dict[tuple[str, str], list[RankedCandidate]] = {}
        self.candidates: list[RankedCandidate] = []
        self.manifest_rows: list[dict[str, str]] = []
        self.requested: dict[tuple[str, str], str] = {}  # (retailer, url) -> request_id
        self.inspections: list[dict[str, str]] = []
        self.variants: list[dict[str, str]] = []
        self.results: dict[str, list[dict[str, str]]] = {}  # request_id -> match result rows
        self.request_status: dict[str, str] = {}
        self.rounds = 1

    def round_candidates(self, job: SearchJob, round_number: int) -> list[RankedCandidate]:
        """Round 1: every search result plus historical and supplied seeds. Later rounds: the
        results new to this job, plus earlier relevant candidates a per-round limit skipped."""
        if round_number == 1:
            ranked = merge_seeds(
                rank_target_hits(job.target, job.retailer_id, job.records), job.target,
                job.retailer_id, self.seeds.get(job.key, []),
            )
            return merge_supplied(ranked, job, self.supplied.get(job.key, []),
                                  self.supplied_report)
        known = {strip_variant(c.hit.product_url): c for c in self.by_job.get(job.key, [])}
        ranked = []
        for candidate in rank_target_hits(job.target, job.retailer_id, job.records):
            earlier = known.get(strip_variant(candidate.hit.product_url))
            if earlier is None:
                candidate.discovery_round = round_number
                ranked.append(candidate)
            elif earlier.decision == "NOT_SELECTED_LIMIT":
                ranked.append(earlier)  # Relevant, skipped by a per-round limit: decided again.
        ranked.sort(key=lambda c: c.sort_key)
        next_rank = len(known)
        for candidate in ranked:
            if candidate.discovery_round == round_number:
                next_rank += 1
                candidate.rank = next_rank
        return ranked

    def add_candidates(self, job: SearchJob, ranked: list[RankedCandidate]) -> None:
        listed = self.by_job.setdefault(job.key, [])
        for candidate in ranked:
            if candidate not in listed:
                listed.append(candidate)
                self.candidates.append(candidate)
            # A relevant candidate held for review or skipped by a limit was looked at without
            # confirming the configuration: it no longer stops the search (2026-10-19, second
            # review: seven jobs stopped on an uncollectable laptop sleeve).
            if candidate.category in COLLECTABLE and candidate.decision and not (
                candidate.decision.startswith("COLLECT")
                or candidate.decision == "DUPLICATE_OF_EARLIER_CANDIDATE"
            ):
                job.evaluated.add(strip_variant(candidate.hit.product_url))

    def add_manifest_rows(self, chosen, fetch_allowance: int) -> list[dict[str, str]]:
        """Manifest rows for this round; a page already requested in this run is not fetched
        again (the candidate names the earlier request)."""
        new_rows: list[dict[str, str]] = []
        for candidate, url, _variant in chosen:
            key = (candidate.hit.retailer_id, url)
            candidate.collection_url = url
            if key in self.requested:
                candidate.decision = "DUPLICATE_OF_EARLIER_CANDIDATE"
                candidate.collection_request_id = self.requested[key]
                continue
            # Inspections so far plus every collection request of this run stay within
            # --max-product-fetches.
            if len(self.manifest_rows) >= fetch_allowance:
                candidate.decision = "NOT_COLLECTED_BUDGET"
                candidate.decision_reason = "Product-fetch budget reached before collection."
                candidate.collection_url = ""
                continue
            request_id = (f"disc-{candidate.hit.retailer_id}-{candidate.target.target_id}-"
                          f"{len(self.manifest_rows) + 1}")
            row = {
                "request_id": request_id,
                "retailer_id": candidate.hit.retailer_id,
                "product_url": url,
                "expected_target_id": candidate.target.target_id,
                "enabled": "TRUE",
            }
            self.requested[key] = request_id
            candidate.collection_request_id = request_id
            self.manifest_rows.append(row)
            new_rows.append(row)
            job = next(j for j in self.jobs if j.key == (candidate.target.target_id,
                                                         candidate.hit.retailer_id))
            if job.resumed:
                job.collection_requests_after_resume += 1
        for row in self.supplied_report:
            matches = [c for c in self.candidates if "SUPPLIED_URL" in c.flags
                       or "ALSO_SUPPLIED_URL" in c.flags]
            for candidate in matches:
                if strip_variant(candidate.hit.product_url) == strip_variant(
                    row["normalized_url"]
                ):
                    row["candidate_decision"] = candidate.decision
                    row["collection_request_id"] = candidate.collection_request_id
        return new_rows

    def record_collection(self, results: list[dict[str, str]], statuses: dict[str, str]) -> None:
        """Results of a collection round: evaluate every job's collected candidates."""
        for row in results:
            self.results.setdefault(row["request_id"], []).append(row)
        self.request_status |= statuses
        for job in self.jobs:
            job.collected = True
            outcomes: list[str] = []
            for candidate in self.by_job.get(job.key, []):
                request_id = candidate.collection_request_id
                if not request_id or request_id not in self.request_status:
                    continue
                rows = self.results.get(request_id, [])
                if self.request_status[request_id] in {"SUCCEEDED", "PARTIAL", "FAILED",
                                                       "NO_OFFERS"}:
                    job.evaluated.add(strip_variant(candidate.hit.product_url))
                if not rows:
                    outcomes.append(f"{candidate.collection_url}: request "
                                    f"{self.request_status[request_id]}")
                for row in rows:
                    attributes = row.get(OFFER_ATTRIBUTES_KEY) or {
                        name: row.get(f"offer_{name}", "") for name in CORE_MATCH_COLUMNS
                        if row.get(f"offer_{name}", "")}
                    outcome, detail = configuration_outcome(
                        row["match_status"], row["target_id"],
                        [c for c in row["reason_codes"].split(";") if c],
                        attributes,
                        job.target.target_id,
                        dict(job.target.record) or (
                            job.target.core | {"model": job.target.model,
                                               "brand": job.target.brand}),
                        title=row.get("raw_title", ""),
                    )
                    # A listed-spec match for a broad-family job is a suitable offer for it
                    # (2026-10-20); the weekly report decides the price under that label.
                    if outcome in {"MATCHED_CONFIGURATION", "LISTED_SPEC_MATCH"} and (
                        not job.suitable_url
                    ):
                        job.suitable_url = row["offer_url"]
                        job.suitable_observation_id = row["observation_id"]
                    outcomes.append(f"{row['offer_url']}: {outcome}"
                                    + (f" ({detail})" if detail else ""))
            job.candidate_outcomes = list(dict.fromkeys(outcomes))

    def resume_reason(self, job: SearchJob) -> str:
        held = next((c for c in self.by_job.get(job.key, [])
                     if strip_variant(c.hit.product_url) == strip_variant(job.plausible_url)
                     and not c.decision.startswith("COLLECT")
                     and c.decision != "DUPLICATE_OF_EARLIER_CANDIDATE"), None)
        if held is not None:
            return (f"{STOP_PLAUSIBLE} at {job.plausible_url}, but it was not collected "
                    f"({held.decision}: {held.decision_reason})")
        return (f"{STOP_PLAUSIBLE} at {job.plausible_url}, but collection did not confirm "
                f"{job.target.target_id}: " + "; ".join(job.candidate_outcomes or ["no result"]))

    def write_files(self) -> None:
        out = self.out
        records = [record for job in self.jobs for record in job.records]
        write_csv(out / "search_jobs.csv", SEARCH_JOB_COLUMNS,
                  (job.job_row(len(self.seeds.get(job.key, []))) for job in self.jobs))
        write_csv(out / "search_requests.csv", SEARCH_RECORD_COLUMNS,
                  (search_record_row(r) for r in records))
        hit_rows = [
            {"target_id": r.query.target_id, "retailer_id": r.query.retailer_id,
             "tier": r.query.tier, "query": r.query.text, "page_number": str(r.page_number),
             "position": str(h.position), "product_url": h.product_url,
             "listed_url": h.listed_url, "title": h.title, "vendor": h.vendor,
             "product_type": h.product_type, "available": h.available, "price": h.price_text,
             "tags": "; ".join(h.tags)}
            for r in records for h in r.hit_items
        ]
        write_csv(out / "search_results.csv", [
            "target_id", "retailer_id", "tier", "query", "page_number", "position",
            "product_url", "listed_url", "title", "vendor", "product_type", "available", "price",
            "tags",
        ], hit_rows)
        inspection_columns = [
            "target_id", "retailer_id", "product_url", "outcome", "variants", "retrieved_at",
            "content_sha256", "evidence_file", "message", "cache",
        ]
        write_csv(out / "product_inspections.csv", inspection_columns, (
            {column: row.get(column, "") for column in inspection_columns}
            for row in self.inspections
        ))
        write_csv(out / "variants.csv", [
            "target_id", "retailer_id", "product_url", "variant_id", "variant_url",
            "option_title", "availability", "price", "decision", "reason",
        ], self.variants)
        write_csv(out / "candidates.csv", CANDIDATE_COLUMNS,
                  (candidate_row(c) for c in self.candidates))
        write_csv(out / "manifest.csv", MANIFEST_COLUMNS,
                  ({k: row[k] for k in MANIFEST_COLUMNS} for row in self.manifest_rows))
        write_csv(out / "supplied_urls.csv", SUPPLIED_REPORT_COLUMNS, self.supplied_report)


SUPPLIED_REPORT_COLUMNS = (
    "supplied_id", "retailer_id", "supplied_url", "normalized_url", "removed_parameters",
    "search_target_hint", "source_file", "assigned_target_ids", "assignment_basis",
    "found_by_native_search", "candidate_decision", "collection_request_id",
)


class RoundCollector:
    """Collects each round's new manifest rows: live through collect_and_match.py (one shared
    request budget), or offline from saved evidence (--replay-only --replay-collection)."""

    def __init__(self, args, out, budget, clock, log, ranks, target_master,
                 saved_inspections) -> None:
        self.args = args
        self.out = out
        self.budget = budget
        self.clock = clock
        self.log = log
        self.ranks = ranks
        self.target_master = target_master
        self.saved_inspections = saved_inspections
        self.round_dirs: list[Path] = []
        self.codes: list[int] = []
        self.backup: dict[str, str] | None = None

    def collect(self, round_number: int, rows: list[dict[str, str]]):
        """``(match result rows, request_id -> request status)`` for this round's rows."""
        args, out = self.args, self.out
        rounds = out / "collection_rounds"
        rounds.mkdir(exist_ok=True)
        manifest_path = rounds / f"manifest_round_{round_number}.csv"
        write_csv(manifest_path, MANIFEST_COLUMNS,
                  ({k: row[k] for k in MANIFEST_COLUMNS} for row in rows))
        round_dir = rounds / f"round_{round_number}"
        if args.replay_only:
            entries = validate_manifest(
                [row | {"_row_number": str(i)} for i, row in enumerate(rows, start=2)],
                self.ranks,
            )
            saved_pages = {
                url: saved_page(entry[0].get("retailer_id", ""), url, entry)
                for url, entry in self.saved_inspections.items()
            }
            outcomes, sources = replay_outcomes(
                entries, load_saved_collections(args.replay_collection), saved_pages
            )
            context = matching_context(
                self.target_master, target_master_sha256=sha256_file(args.target_master),
                source_label=str(args.target_master),
            )
            write_replayed_collection(round_dir, entries, outcomes, sources,
                                      matcher_from_validated(self.target_master), context,
                                      self.ranks)
            self.log(f"Collection round {round_number} replayed offline: "
                     f"{dict(Counter(s.split(':', 1)[0] for s in sources.values()))}.")
        else:
            if self.backup is None:
                backup = out / "store_backup_before_collection.sqlite3"
                self.backup = {"path": str(backup), "sha256": backup_store(args.store, backup)}
            if len(rows) > self.budget.remaining:
                self.log("  Total budget smaller than this round's manifest; some rows may fail "
                         "with REQUEST_BUDGET_EXHAUSTED.")
            # A fresh Fetcher inside the collector: wait one interval so per-retailer spacing
            # holds.
            self.clock.sleep(FetchConfig().min_request_interval_seconds)
            self.log(f"Collecting {len(rows)} candidate pages (round {round_number}) with "
                     "collect_and_match.py...")
            code = load_collector().main([
                "--live", "--manifest", str(manifest_path),
                "--target-master", str(args.target_master),
                "--selected-list", str(args.selected_list),
                "--contributions", str(args.contributions),
                "--store", str(args.store), "--output-dir", str(round_dir),
            ], transport=self.budget, clock=self.clock)
            self.codes.append(code)
        self.round_dirs.append(round_dir)
        results = read_rows(round_dir / "match_results.csv") if (
            round_dir / "match_results.csv").is_file() else []
        # Every field each collected page established (brand and secondary hardware included),
        # for the listed-spec check of broad-family jobs. Re-extracted offline from the round's
        # own observations; never filled from a target.
        observations = round_dir / "observations.csv"
        if results and observations.is_file():
            try:
                items = read_observations_csv(observations)
            except ObservationFileError:
                items = []  # a round whose pages yielded no observation
            attributes = {
                observation_id(item.observation): dict(
                    extract_offer_attributes(item.observation).values)
                for item in items
            }
            for row in results:
                row[OFFER_ATTRIBUTES_KEY] = attributes.get(row.get("observation_id", ""), {})
        statuses = {
            row["request_id"]: row["request_status"]
            for row in (read_rows(round_dir / "request_report.csv")
                        if (round_dir / "request_report.csv").is_file() else [])
        }
        return results, statuses

    def summary(self) -> dict:
        values: dict = {"collection_rounds": len(self.round_dirs)}
        if self.backup is not None:
            values["store_backup"] = self.backup
        if self.codes:
            values["collection_exit_codes"] = self.codes
        if self.args.replay_only:
            values["collection_mode"] = "REPLAY_ONLY (saved observation records and pages)"
        return values


if __name__ == "__main__":
    raise SystemExit(main())
