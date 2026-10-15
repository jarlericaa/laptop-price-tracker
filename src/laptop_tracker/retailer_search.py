"""Bounded retailer product search: search URLs, result parsing, and search outcomes.

Each retailer's mechanism was inspected live on 2026-10-13 before this module was written. The
responses (robots.txt, agents.md, and sample searches) are kept with their SHA-256 hashes in the
ignored ``data/collection/search_inspection_2026-10-13/``.

- **Laptop Factory** (WooCommerce, Flatsome theme): ``GET /?s=<query>&post_type=product``
  renders result cards on the server (``p.name.product-title a``), 12 per page, with
  ``a.next.page-number`` pagination and an explicit ``woocommerce-no-products-found`` block.
  The store appeared to omit out-of-stock products from search (a known out-of-stock page was
  not found by its exact code). robots.txt allows the path.
- **DataBlitz, PC Express, PC Central, Octagon** (Shopify): ``GET /search/suggest.json?q=<query>
  &resources[type]=product&resources[limit]=10`` (Shopify predictive search) returns structured
  product results: handle, title, vendor, type, tags, availability, price, and a URL carrying
  tracking parameters. It has no pagination and lists no variants. DataBlitz's HTML ``/search``
  page is rendered client-side by a search app and contains no result links, so the HTML search
  page is used for no Shopify store. robots.txt allows the path on all four hosts.

A failed request (``SEARCH_ERROR``) or a response this module cannot read
(``UNSUPPORTED_LAYOUT``) is never reported as ``NO_RESULTS``. No request is made here: callers
pass fetched pages in, so the shared ``Fetcher`` keeps its timeouts, retries, and throttling.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote_plus, urlencode, urljoin, urlsplit

from .adapters import datablitz, laptop_factory, oct, pcc, pcx
from .offers import clean_text, url_host
from .scraping import FetchedPage, Transport, TransportError, TransportResponse

RESULTS = "RESULTS"
NO_RESULTS = "NO_RESULTS"
SEARCH_ERROR = "SEARCH_ERROR"
UNSUPPORTED_LAYOUT = "UNSUPPORTED_LAYOUT"
NOT_SENT_BUDGET = "NOT_SENT_BUDGET_EXHAUSTED"
NOT_SENT_LIMIT = "NOT_SENT_SEARCH_LIMIT"
SEARCH_OUTCOMES = (RESULTS, NO_RESULTS, SEARCH_ERROR, UNSUPPORTED_LAYOUT, NOT_SENT_BUDGET,
                   NOT_SENT_LIMIT)


class SearchLayoutError(ValueError):
    """The response is not a search result page this module recognizes."""


@dataclass(frozen=True)
class SearchHit:
    """One product result as the retailer's search listed it (discovery evidence only)."""

    retailer_id: str
    product_url: str  # Canonical product page URL: the retailer host and path, no query.
    title: str
    listed_url: str  # The result link as listed, made absolute (may carry tracking queries).
    page_number: int
    position: int
    vendor: str = ""
    product_type: str = ""
    tags: tuple[str, ...] = ()
    available: str = ""
    price_text: str = ""
    summary: str = ""


@dataclass(frozen=True)
class ParsedSearchPage:
    hits: tuple[SearchHit, ...]
    next_url: str = ""
    filtered_links: int = 0  # Result links that were not product pages on the allowed host.
    possibly_truncated: bool = False  # The page returned exactly its maximum result count.


@dataclass(frozen=True)
class SearchMethod:
    """How one retailer is searched. ``kind`` selects the parser."""

    retailer_id: str
    host: str
    kind: str  # "woocommerce_html" or "shopify_predictive_json"
    product_path: str  # Regular expression for a product page path on ``host``.
    page_size: int
    paginated: bool
    notes: str
    multi_variant_collection: bool = False  # The adapter can bind a ``?variant=`` page.

    def first_page_url(self, query: str) -> str:
        if self.kind == "woocommerce_html":
            return f"https://{self.host}/?s={quote_plus(query)}&post_type=product"
        params = {
            "q": query, "resources[type]": "product", "resources[limit]": str(self.page_size),
        }
        return f"https://{self.host}/search/suggest.json?{urlencode(params)}"

    def canonical_product_url(self, link: str, base: str) -> str:
        """``https://host/<product path>`` for a result link, or ``""`` when it is not one."""
        absolute = urljoin(base, html.unescape(link.strip()))
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"} or url_host(absolute) != self.host:
            return ""
        if not re.fullmatch(self.product_path, parts.path):
            return ""
        return f"https://{self.host}{parts.path}"

    def parse(self, page: FetchedPage) -> ParsedSearchPage:
        if self.kind == "woocommerce_html":
            return _parse_woocommerce(self, page)
        return _parse_shopify_predictive(self, page)


SEARCH_METHODS: dict[str, SearchMethod] = {
    method.retailer_id: method
    for method in (
        SearchMethod(
            laptop_factory.RETAILER_ID, laptop_factory.HOST, "woocommerce_html",
            r"/product/[^/?#]+/", 12, True,
            "WooCommerce product search; out-of-stock products appeared absent from results.",
        ),
        SearchMethod(
            datablitz.RETAILER_ID, datablitz.HOST, "shopify_predictive_json",
            r"/products/[^/?#.]+", 10, False,
            "Shopify predictive search; at most 10 results, no pagination, no variant list.",
        ),
        SearchMethod(
            pcx.RETAILER_ID, pcx.HOST, "shopify_predictive_json",
            r"/products/[^/?#.]+", 10, False,
            "Shopify predictive search; at most 10 results, no pagination, no variant list.",
        ),
        SearchMethod(
            pcc.RETAILER_ID, pcc.HOST, "shopify_predictive_json",
            r"/products/[^/?#.]+", 10, False,
            "Shopify predictive search; at most 10 results, no pagination, no variant list.",
            multi_variant_collection=True,
        ),
        SearchMethod(
            oct.RETAILER_ID, oct.HOST, "shopify_predictive_json",
            r"/products/[^/?#.]+", 10, False,
            "Shopify predictive search; at most 10 results, no pagination, no variant list.",
            multi_variant_collection=True,
        ),
    )
}


# Laptop Factory (WooCommerce)


class _WooResultsParser(HTMLParser):
    """Result cards inside the shop's ``div.products`` grid, and the ``next`` page link."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.products_depth: int | None = None
        self.saw_products = False
        self.saw_no_results = False
        self.title_depth: int | None = None
        self.link: str | None = None
        self.text: list[str] = []
        self.cards: list[tuple[str, str]] = []
        self.next_links: list[str] = []

    _VOID = frozenset({"img", "br", "hr", "input", "meta", "link", "source", "wbr", "col"})

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "a" and {"next", "page-number"} <= classes and attributes.get("href"):
            self.next_links.append(attributes["href"])
        if "woocommerce-no-products-found" in classes:
            self.saw_no_results = True
        if tag in self._VOID:
            return
        self.depth += 1
        if tag == "div" and "products" in classes and self.products_depth is None:
            self.products_depth = self.depth
            self.saw_products = True
        if self.products_depth is not None and tag == "p" and "product-title" in classes:
            self.title_depth = self.depth
        if self.title_depth is not None and tag == "a" and self.link is None:
            self.link = attributes.get("href", "")
            self.text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID:
            return
        if tag == "a" and self.link is not None:
            self.cards.append((self.link, clean_text("".join(self.text))))
            self.link = None
        if self.title_depth is not None and self.depth == self.title_depth:
            self.title_depth = None
        if self.products_depth is not None and self.depth == self.products_depth:
            self.products_depth = None
        self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.link is not None:
            self.text.append(data)


def _single_product_title(text: str) -> str:
    """Title of a product page (WooCommerce redirects a search with exactly one result)."""
    for pattern in (
        r'<meta\s+property="og:title"\s+content="([^"]+)"',
        r'<h1[^>]*class="[^"]*product[_-]title[^"]*"[^>]*>(.*?)</h1>',
    ):
        match = re.search(pattern, text, re.I | re.S)
        if match:
            return clean_text(html.unescape(re.sub(r"<[^>]+>", " ", match[1])))
    return ""


def _parse_woocommerce(method: SearchMethod, page: FetchedPage) -> ParsedSearchPage:
    text = page.text()
    landed = method.canonical_product_url(page.final_url, page.final_url)
    if landed:
        title = _single_product_title(text)
        if not title:
            raise SearchLayoutError("Search redirected to a product page without a title.")
        return ParsedSearchPage((SearchHit(method.retailer_id, landed, title, page.final_url,
                                           1, 1),))
    parser = _WooResultsParser()
    parser.feed(text)
    parser.close()
    if parser.saw_no_results and not parser.cards:
        return ParsedSearchPage(())
    if not parser.saw_products:
        raise SearchLayoutError("No product result grid and no 'no products' message.")
    if not parser.cards:
        raise SearchLayoutError("The product result grid has no recognizable result cards.")
    hits: list[SearchHit] = []
    filtered = 0
    page_number = _page_number(page.requested_url)
    for link, title in parser.cards:
        product = method.canonical_product_url(link, page.final_url)
        if not product:
            filtered += 1
            continue
        hits.append(SearchHit(method.retailer_id, product, title,
                              urljoin(page.final_url, html.unescape(link)), page_number,
                              len(hits) + 1))
    next_url = ""
    for link in parser.next_links:
        candidate = urljoin(page.final_url, html.unescape(link))
        if url_host(candidate) == method.host and "s=" in urlsplit(candidate).query:
            next_url = candidate
            break
    return ParsedSearchPage(tuple(hits), next_url, filtered)


def _page_number(url: str) -> int:
    match = re.search(r"/page/([0-9]+)/", urlsplit(url).path)
    return int(match[1]) if match else 1


# Shopify predictive search


def _plain(value: object, limit: int = 600) -> str:
    if not isinstance(value, str):
        return ""
    text = clean_text(html.unescape(re.sub(r"<[^>]+>", " ", value)))
    return text[:limit]


def _parse_shopify_predictive(method: SearchMethod, page: FetchedPage) -> ParsedSearchPage:
    if "json" not in page.content_type.lower():
        raise SearchLayoutError("Predictive search did not return JSON.")
    try:
        data = json.loads(page.body)
        products = data["resources"]["results"]["products"]
    except (ValueError, KeyError, TypeError) as error:
        raise SearchLayoutError("Predictive search JSON has no product result list.") from error
    if not isinstance(products, list):
        raise SearchLayoutError("Predictive search product results are not a list.")
    hits: list[SearchHit] = []
    filtered = 0
    for item in products:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            filtered += 1
            continue
        product = method.canonical_product_url(item["url"], page.final_url)
        title = _plain(item.get("title"), 400)
        if not product or not title:
            filtered += 1
            continue
        available = item.get("available")
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        hits.append(SearchHit(
            method.retailer_id, product, title, urljoin(page.final_url, item["url"]), 1,
            len(hits) + 1,
            vendor=_plain(item.get("vendor"), 120),
            product_type=_plain(item.get("type"), 120),
            tags=tuple(_plain(tag, 80) for tag in tags if isinstance(tag, str)),
            available={True: "TRUE", False: "FALSE"}.get(available, ""),
            price_text=_plain(item.get("price"), 40),
            summary=_plain(item.get("body")),
        ))
    return ParsedSearchPage(tuple(hits), "", filtered, len(products) >= method.page_size)


# Request budget


class BudgetedTransport:
    """Wraps a transport and refuses any HTTP call beyond a fixed total.

    Every call counts: retries and redirect hops too, so the limit is a hard ceiling on
    requests sent to retailers in one run. A refused call raises a non-retryable
    ``TransportError`` (``REQUEST_BUDGET_EXHAUSTED``); nothing is sent.
    """

    def __init__(self, inner: Transport, limit: int) -> None:
        if limit < 0:
            raise ValueError("The request budget must not be negative.")
        self.inner = inner
        self.limit = limit
        self.sent = 0
        self.live = getattr(inner, "live", True)

    @property
    def remaining(self) -> int:
        return self.limit - self.sent

    def fetch(self, url, *, timeout, headers, max_bytes) -> TransportResponse:
        if self.sent >= self.limit:
            raise TransportError(
                BUDGET_CODE, "The run's request budget is used up; nothing was sent.",
                retryable=False,
            )
        self.sent += 1
        return self.inner.fetch(url, timeout=timeout, headers=headers, max_bytes=max_bytes)


BUDGET_CODE = "REQUEST_BUDGET_EXHAUSTED"


# Search queries


@dataclass(frozen=True)
class SearchQuery:
    target_id: str
    retailer_id: str
    tier: str
    text: str
    verified_identifier: bool
    basis: str


TIER_MODEL_CODE = "1_FULL_MODEL_CODE"
TIER_PART_NUMBER = "2_VERIFIED_PART_NUMBER"
TIER_FAMILY = "3_BRAND_FAMILY"


def search_queries(
    target_id: str, retailer_id: str, *, model: str, part_number: str, brand: str, series: str
) -> list[SearchQuery]:
    """Ordered queries: full model code, verified part number, then the broader family.

    The part number is used only when TARGET_MASTER records it (owner-verified). Retailer SKUs
    and unverified identifiers are never search terms here. The family query runs only when the
    earlier tiers found no result showing the model code (decided by the caller).
    """
    queries = [SearchQuery(target_id, retailer_id, TIER_MODEL_CODE, model.strip(), True,
                           "complete model code from TARGET_MASTER")]
    if part_number.strip():
        queries.append(SearchQuery(target_id, retailer_id, TIER_PART_NUMBER, part_number.strip(),
                                   True, "manufacturer_part_number in TARGET_MASTER"))
    family = clean_text(f"{brand} {series}")
    if family and family.casefold() != model.strip().casefold():
        queries.append(SearchQuery(target_id, retailer_id, TIER_FAMILY, family, False,
                                   "brand and series (broader; runs only if needed)"))
    return queries


@dataclass
class SearchRecord:
    """One planned or sent search request and its outcome."""

    query: SearchQuery
    page_number: int
    url: str
    outcome: str = ""
    http_status: str = ""
    final_url: str = ""
    retrieved_at: str = ""
    attempts: str = ""
    content_sha256: str = ""
    evidence_file: str = ""
    hits: int = 0
    filtered_links: int = 0
    possibly_truncated: bool = False
    next_url: str = ""
    message: str = ""
    hit_items: list[SearchHit] = field(default_factory=list, repr=False)


SEARCH_RECORD_COLUMNS: tuple[str, ...] = (
    "target_id", "retailer_id", "tier", "query", "verified_identifier", "page_number", "url",
    "outcome", "http_status", "final_url", "retrieved_at", "attempts", "content_sha256",
    "evidence_file", "hits", "filtered_links", "possibly_truncated", "next_url", "message",
)


def search_record_row(record: SearchRecord) -> dict[str, str]:
    query = record.query
    return {
        "target_id": query.target_id,
        "retailer_id": query.retailer_id,
        "tier": query.tier,
        "query": query.text,
        "verified_identifier": "TRUE" if query.verified_identifier else "FALSE",
        "page_number": str(record.page_number),
        "url": record.url,
        "outcome": record.outcome,
        "http_status": record.http_status,
        "final_url": record.final_url,
        "retrieved_at": record.retrieved_at,
        "attempts": record.attempts,
        "content_sha256": record.content_sha256,
        "evidence_file": record.evidence_file,
        "hits": str(record.hits),
        "filtered_links": str(record.filtered_links),
        "possibly_truncated": "TRUE" if record.possibly_truncated else "FALSE",
        "next_url": record.next_url,
        "message": record.message,
    }


def unique_hits(records: Sequence[SearchRecord]) -> list[tuple[SearchHit, list[SearchRecord]]]:
    """Each product URL once, with every search request that listed it (duplicates merged)."""
    merged: dict[str, tuple[SearchHit, list[SearchRecord]]] = {}
    for record in records:
        for hit in record.hit_items:
            if hit.product_url in merged:
                merged[hit.product_url][1].append(record)
            else:
                merged[hit.product_url] = (hit, [record])
    return list(merged.values())
