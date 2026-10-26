"""Bounded discovery fallback: the retailer's public catalog index (2026-10-22).

Native search is one discovery channel, not a catalog search. Shopify predictive search returns at
most ten unpaginated results, and Laptop Factory's WooCommerce search returned nothing for the
complete, spaced and segment spellings of listed products (HP ``14-ep0445TU``, ASUS
``X1404VA-EB2264WSM``; the 2026-10-22 rerun's ``search_requests.csv``) while their product pages
exist. When every native query tier of a target that has a *verified* manufacturer code ended
without a plausible candidate, this module looks the code up in the retailer's own public
sitemap, the index its robots.txt advertises (``wp-sitemap.xml`` for Laptop Factory,
``sitemap.xml`` for the four Shopify stores).

What it is, and is not:

- It supplies candidate *locations*. An entry whose URL handle (or, on Shopify, sitemap title)
  shows the target's complete code becomes a search hit; the usual ranking, collection, adapter
  extraction and matcher then decide. Specifications, prices and stock come from the product
  page only.
- The code searched is the target's own: a manufacturer code recorded as its model, or the
  ``manufacturer_model_code`` of a broad-name target. A name or family never becomes a code, so
  family-name targets without a recorded code get no fallback.
- It is bounded: robots.txt is read and obeyed first, one index is fetched once per retailer and
  shared by every job, and ``--max-fallback-requests-per-retailer`` caps the requests (robots.txt,
  the root sitemap and the product sitemaps together). Requests go through the shared ``Fetcher``
  (throttling, bounded retries, host check) and the run's ``BudgetedTransport`` ceiling. Saved
  responses from an earlier run are replayed without a request.
- Every outcome is distinct and recorded (``fallback_requests.csv``, the per-job record and the
  ``fallback`` column): not tried (and why), tried and found, tried and empty, index truncated,
  request budget, robots disallowed, access error, layout not recognized.

Evidence status of the channels (see docs): the robots.txt of all five stores was saved on
2026-10-13 and advertises these sitemaps; no sitemap response has been saved yet, so the response
shapes follow the sitemap protocol and WordPress/Shopify documentation and are UNVERIFIED live.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .catalog_leads import (
    CATALOG_SOURCES,
    LeadTarget,
    SitemapEntry,
    SitemapError,
    lead_basis,
    parse_robots,
    parse_sitemap,
    product_entries,
    product_sitemaps,
    robots_allows,
)
from .retailer_search import (
    BUDGET_CODE,
    NO_RESULTS,
    NOT_SENT_BUDGET,
    NOT_SENT_REPLAY_ONLY,
    RESULTS,
    SEARCH_ERROR,
    SEARCH_METHODS,
    UNSUPPORTED_LAYOUT,
    SearchHit,
    SearchQuery,
    SearchRecord,
)
from .scraping import FetchConfig, Fetcher, FetchError, ScrapeRequest

TIER_CATALOG_INDEX = "9_CATALOG_INDEX_FALLBACK"
NOT_SENT_FALLBACK_DISABLED = "NOT_SENT_FALLBACK_DISABLED"
NOT_SENT_FALLBACK_LIMIT = "NOT_SENT_FALLBACK_LIMIT"
ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"

# ``search_coverage`` tokens (search_jobs.csv): what the fallback did for the job.
FALLBACK_NOT_TRIED_DISABLED = "FALLBACK_NOT_TRIED_DISABLED"
FALLBACK_NOT_TRIED_REPLAY_ONLY = "FALLBACK_NOT_TRIED_REPLAY_ONLY"
FALLBACK_NOT_TRIED_BUDGET = "FALLBACK_NOT_TRIED_BUDGET"
FALLBACK_NOT_TRIED_RETAILER_FAILING = "FALLBACK_NOT_TRIED_RETAILER_FAILING"
FALLBACK_FOUND_CANDIDATE = "FALLBACK_FOUND_CANDIDATE"
FALLBACK_TRIED_EMPTY = "FALLBACK_TRIED_EMPTY"
FALLBACK_INDEX_TRUNCATED = "FALLBACK_INDEX_TRUNCATED"
FALLBACK_BUDGET_LIMIT = "FALLBACK_BUDGET_LIMIT"
FALLBACK_ACCESS_ERROR = "FALLBACK_ACCESS_ERROR"
FALLBACK_ROBOTS_DISALLOWED = "FALLBACK_ROBOTS_DISALLOWED"
FALLBACK_LAYOUT_UNRECOGNIZED = "FALLBACK_LAYOUT_UNRECOGNIZED"
# Tokens that mean the fallback never ran for the job; the others mean it ran (or tried to).
NOT_TRIED_TOKENS = frozenset({FALLBACK_NOT_TRIED_DISABLED, FALLBACK_NOT_TRIED_REPLAY_ONLY,
                              FALLBACK_NOT_TRIED_BUDGET, FALLBACK_NOT_TRIED_RETAILER_FAILING})

FALLBACK_REQUEST_COLUMNS: tuple[str, ...] = (
    "retailer_id", "kind", "url", "outcome", "http_status", "retrieved_at", "content_sha256",
    "evidence_file", "entries", "message", "cache",
)
HITS_PER_JOB = 5  # Entries kept as search hits per job (a code shows in one or two URLs).
INDEX_TITLE = "(catalog index entry; the product page has not been read)"


def fallback_code(target) -> str:
    """The verified manufacturer code a fallback may look for, or ``""``.

    A code target's model, or the recorded ``manufacturer_model_code`` of a broad-name target.
    Names and families are never turned into codes.
    """
    if target.kind == "MODEL_CODE":
        return target.model.strip()
    return target.recorded_code.strip()


@dataclass
class CatalogIndex:
    """One retailer's product-URL index, fetched at most once per run."""

    retailer_id: str
    entries: list[SitemapEntry] = field(default_factory=list)
    status: str = ""  # READY, or the coverage token that stopped it.
    listed_sitemaps: int = 0
    read_sitemaps: int = 0
    unread_sitemaps: int = 0
    message: str = ""
    failures: list[str] = field(default_factory=list)  # Coverage tokens of unread sitemaps.
    sitemap_of: dict[str, str] = field(default_factory=dict)  # product URL -> its sitemap

    @property
    def usable(self) -> bool:
        return self.status == "READY"

    @property
    def complete(self) -> bool:
        return self.usable and self.unread_sitemaps == 0


def load_saved_index(folders: Iterable[Path], max_age_hours: float | None,
                     now: datetime) -> dict[str, tuple[dict[str, str], Path]]:
    """``url -> (row, body path)`` for fallback responses saved by an earlier run."""
    saved: dict[str, tuple[dict[str, str], Path]] = {}
    for folder in folders:
        path = folder / "fallback_requests.csv"
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                body = folder / row.get("evidence_file", "")
                if row.get("outcome") not in {"FETCHED", "SAVED"} or not body.is_file():
                    continue
                try:
                    moment = datetime.fromisoformat(row["retrieved_at"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                if moment.tzinfo is None:
                    continue
                if max_age_hours is not None and (
                        now - moment).total_seconds() > max_age_hours * 3600:
                    continue
                url = row["url"]
                if url not in saved or row["retrieved_at"] > saved[url][0]["retrieved_at"]:
                    saved[url] = (row, body)
    return saved


class FallbackCoordinator:
    """Builds each retailer's index on demand and answers per-job lookups from it."""

    def __init__(self, *, fetcher: Fetcher, transport, evidence_dir: Path, per_retailer_limit: int,
                 saved: Mapping[str, tuple[dict[str, str], Path]] | None = None,
                 replay_only: bool = False, user_agent: str = FetchConfig().user_agent,
                 log=print) -> None:
        self.fetcher = fetcher
        self.transport = transport
        self.evidence_dir = evidence_dir
        self.limit = per_retailer_limit
        self.saved = dict(saved or {})
        self.replay_only = replay_only
        self.user_agent = user_agent
        self.log = log
        self.indexes: dict[str, CatalogIndex] = {}
        self.requests: list[dict[str, str]] = []
        self.sent: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @property
    def requests_sent(self) -> int:
        return sum(self.sent.values())

    # Fetching

    def _get(self, retailer_id: str, kind: str, url: str) -> tuple[bytes | None, str]:
        """``(body, outcome)``; every attempt, saved replay or refusal is one request row."""
        row = {"retailer_id": retailer_id, "kind": kind, "url": url}
        saved = self.saved.get(url)
        if saved is not None:
            meta, path = saved
            body = path.read_bytes()
            self.requests.append(row | {
                "outcome": "SAVED", "http_status": meta.get("http_status", ""),
                "retrieved_at": meta.get("retrieved_at", ""),
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "evidence_file": meta.get("evidence_file", ""), "cache": f"SAVED:{path.parent}"})
            return body, "SAVED"
        if self.replay_only:
            self.requests.append(row | {"outcome": NOT_SENT_REPLAY_ONLY,
                                        "message": "--replay-only: no saved response."})
            return None, NOT_SENT_REPLAY_ONLY
        if self.sent.get(retailer_id, 0) >= self.limit:
            self.requests.append(row | {
                "outcome": NOT_SENT_FALLBACK_LIMIT,
                "message": "--max-fallback-requests-per-retailer reached."})
            return None, NOT_SENT_FALLBACK_LIMIT
        if self.transport.remaining <= 0:
            self.requests.append(row | {"outcome": NOT_SENT_BUDGET,
                                        "message": "--max-total-requests reached."})
            return None, NOT_SENT_BUDGET
        self.sent[retailer_id] = self.sent.get(retailer_id, 0) + 1
        try:
            page = self.fetcher.fetch(ScrapeRequest(retailer_id, url),
                                      allowed_hosts=frozenset({urlsplit(url).netloc.lower()}))
        except FetchError as error:
            outcome = NOT_SENT_BUDGET if error.code == BUDGET_CODE else "FETCH_FAILED"
            self.requests.append(row | {
                "outcome": outcome, "http_status": str(getattr(error, "http_status", "") or ""),
                "message": f"{error.code}: {error.message}"[:300]})
            return None, outcome
        extension = "txt" if kind == "robots" else "xml"
        name = f"evidence/fallback_{page.content_sha256}.{extension}"
        (self.evidence_dir.parent / name).write_bytes(page.body)
        self.requests.append(row | {
            "outcome": "FETCHED", "http_status": str(page.status_code),
            "retrieved_at": page.retrieved_at.astimezone(UTC).isoformat(),
            "content_sha256": page.content_sha256, "evidence_file": name})
        return page.body, "FETCHED"

    # Index

    def index(self, retailer_id: str) -> CatalogIndex:
        if retailer_id not in self.indexes:
            self.indexes[retailer_id] = self._build(retailer_id)
        return self.indexes[retailer_id]

    def _build(self, retailer_id: str) -> CatalogIndex:
        source = CATALOG_SOURCES[retailer_id]
        index = CatalogIndex(retailer_id)
        host = urlsplit(source.root_sitemap).netloc
        robots, outcome = self._get(retailer_id, "robots", f"https://{host}/robots.txt")
        if robots is None:
            index.status = _failure_token(outcome)
            index.message = f"robots.txt not read ({outcome}); nothing else was requested."
            return index
        rules = parse_robots(robots.decode("utf-8", "replace"), self.user_agent)
        if not robots_allows(rules, source.root_sitemap):
            self.requests.append({"retailer_id": retailer_id, "kind": "sitemap_index",
                                  "url": source.root_sitemap, "outcome": ROBOTS_DISALLOWED})
            index.status = FALLBACK_ROBOTS_DISALLOWED
            index.message = "robots.txt disallows the sitemap; nothing else was requested."
            return index
        body, outcome = self._get(retailer_id, "sitemap_index", source.root_sitemap)
        if body is None:
            index.status = _failure_token(outcome)
            index.message = f"The sitemap index was not read ({outcome})."
            return index
        try:
            root = parse_sitemap(body)
        except SitemapError as error:
            self.requests[-1]["message"] = str(error)
            index.status = FALLBACK_LAYOUT_UNRECOGNIZED
            index.message = f"The sitemap index is not a sitemap: {error}"
            return index
        children = [source.root_sitemap] if root.kind == "urlset" else product_sitemaps(
            source, root)
        index.listed_sitemaps = len(children)
        index.status = "READY"
        for position, child in enumerate(children):
            if not robots_allows(rules, child):
                self.requests.append({"retailer_id": retailer_id, "kind": "product_sitemap",
                                      "url": child, "outcome": ROBOTS_DISALLOWED})
                index.unread_sitemaps += 1
                index.failures.append(FALLBACK_ROBOTS_DISALLOWED)
                continue
            if child == source.root_sitemap:
                data = root
            else:
                raw, outcome = self._get(retailer_id, "product_sitemap", child)
                if raw is None:
                    index.unread_sitemaps += 1
                    index.failures.append(_failure_token(outcome))
                    if outcome in {NOT_SENT_FALLBACK_LIMIT, NOT_SENT_BUDGET,
                                   NOT_SENT_REPLAY_ONLY}:
                        index.unread_sitemaps += len(children) - position - 1  # Not tried.
                        break
                    continue
                try:
                    data = parse_sitemap(raw)
                except SitemapError as error:
                    self.requests[-1]["message"] = str(error)
                    index.unread_sitemaps += 1
                    index.failures.append(FALLBACK_LAYOUT_UNRECOGNIZED)
                    continue
            products = product_entries(source, data)
            if self.requests and self.requests[-1]["url"] == child:
                self.requests[-1]["entries"] = str(len(products))
            index.read_sitemaps += 1
            index.entries += products
            for entry in products:
                index.sitemap_of.setdefault(entry.loc, child)
        index.message = (f"{index.listed_sitemaps} product sitemap(s) listed, "
                         f"{index.read_sitemaps} read, {len(index.entries)} product URL(s)"
                         + (f"; {index.unread_sitemaps} unread "
                            f"({', '.join(sorted(set(index.failures)))})"
                            if index.unread_sitemaps else ""))
        if index.listed_sitemaps == 0:
            index.status = FALLBACK_LAYOUT_UNRECOGNIZED
            index.message = "The sitemap index lists no product sitemap."
        elif index.read_sitemaps == 0:
            index.status = _truncation_token(index.failures)
        return index

    # Lookup

    def lookup(self, target, retailer_id: str, *, native_stop: str, native_coverage: str,
               retailer_failing: bool = False) -> tuple[str, SearchRecord | None]:
        """``(coverage token, record)`` for one job whose native search found no plausible
        candidate. The record carries the hits (candidate locations) and says why it ran."""
        code = fallback_code(target)
        reason = (f"native search ended without a plausible candidate ({native_stop}; native "
                  f"coverage {native_coverage})")
        query = SearchQuery(target.target_id, retailer_id, TIER_CATALOG_INDEX, code, True,
                            "complete verified model code looked up in the retailer's public "
                            "sitemap index (candidate locations only)")
        source = CATALOG_SOURCES[retailer_id]
        record = SearchRecord(query, 1, source.root_sitemap, run_reason=f"fallback: {reason}")
        if not self.enabled:
            # Opt-in and off: the job says so in its coverage and ``fallback`` column; no
            # request row is written for a request that was never planned.
            return FALLBACK_NOT_TRIED_DISABLED, None
        if retailer_failing:
            record.outcome = NOT_SENT_FALLBACK_LIMIT
            record.message = "The retailer's searches stopped after repeated errors."
            return FALLBACK_NOT_TRIED_RETAILER_FAILING, record
        robots_url = f"https://{urlsplit(source.root_sitemap).netloc}/robots.txt"
        if (retailer_id not in self.indexes and not self.replay_only
                and robots_url not in self.saved and self.transport.remaining <= 0):
            record.outcome = NOT_SENT_BUDGET
            record.message = "--max-total-requests reached before the index was fetched."
            return FALLBACK_NOT_TRIED_BUDGET, record
        index = self.index(retailer_id)
        if not index.usable:
            return _index_failure(index, record)
        hits = self._hits(index, target, retailer_id, code)
        record.http_status = "200"
        record.message = index.message
        if hits:
            record.outcome = RESULTS
            record.hit_items = hits
            record.hits = len(hits)
            record.possibly_truncated = index.unread_sitemaps > 0
            return FALLBACK_FOUND_CANDIDATE, record
        record.outcome = NO_RESULTS
        if index.unread_sitemaps:
            record.possibly_truncated = True
            record.message += ("; no entry read shows the complete code, but the index is "
                               "incomplete")
            return _truncation_token(index.failures), record
        record.message += "; no entry shows the complete code"
        return FALLBACK_TRIED_EMPTY, record

    def _hits(self, index: CatalogIndex, target, retailer_id: str, code: str) -> list[SearchHit]:
        method = SEARCH_METHODS[retailer_id]
        lead = LeadTarget(target.target_id, code)
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for entry in index.entries:
            basis = lead_basis(entry, lead)
            product = method.canonical_product_url(entry.loc, CATALOG_SOURCES[
                retailer_id].root_sitemap)
            if not basis or not product or product in seen:
                continue
            seen.add(product)
            handle = urlsplit(product).path.rstrip("/").rsplit("/", 1)[-1]
            # A sitemap title is the retailer's product title; a URL handle alone is not a title.
            title = entry.title or f"{INDEX_TITLE} {handle}"
            hits.append(SearchHit(retailer_id, product, title, entry.loc, 1, len(hits) + 1,
                                  summary=f"{basis}; sitemap {index.sitemap_of.get(entry.loc, '')}"
                                          + (f"; lastmod {entry.lastmod}" if entry.lastmod
                                             else "")))
            if len(hits) >= HITS_PER_JOB:
                break
        return hits


def _truncation_token(failures: Sequence[str]) -> str:
    """Why an index could not be read completely: a real problem outranks a request limit."""
    for token in (FALLBACK_BUDGET_LIMIT, FALLBACK_ACCESS_ERROR, FALLBACK_LAYOUT_UNRECOGNIZED,
                  FALLBACK_ROBOTS_DISALLOWED, FALLBACK_NOT_TRIED_REPLAY_ONLY):
        if token in failures:
            return token
    return FALLBACK_INDEX_TRUNCATED


def _failure_token(outcome: str) -> str:
    if outcome == NOT_SENT_BUDGET:
        return FALLBACK_BUDGET_LIMIT
    if outcome == NOT_SENT_REPLAY_ONLY:
        return FALLBACK_NOT_TRIED_REPLAY_ONLY
    if outcome == NOT_SENT_FALLBACK_LIMIT:
        return FALLBACK_INDEX_TRUNCATED
    return FALLBACK_ACCESS_ERROR


def _index_failure(index: CatalogIndex, record: SearchRecord) -> tuple[str, SearchRecord]:
    token = index.status
    record.message = index.message
    if token == FALLBACK_NOT_TRIED_REPLAY_ONLY:
        record.outcome = NOT_SENT_REPLAY_ONLY
    elif token == FALLBACK_BUDGET_LIMIT:
        record.outcome = NOT_SENT_BUDGET
    elif token == FALLBACK_INDEX_TRUNCATED:
        record.outcome = NOT_SENT_FALLBACK_LIMIT
    elif token == FALLBACK_ROBOTS_DISALLOWED:
        record.outcome = ROBOTS_DISALLOWED
    elif token == FALLBACK_LAYOUT_UNRECOGNIZED:
        record.outcome = UNSUPPORTED_LAYOUT
    else:
        record.outcome = SEARCH_ERROR
    return token, record
