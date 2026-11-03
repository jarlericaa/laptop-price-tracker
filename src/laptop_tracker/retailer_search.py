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

import dataclasses
import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit

from .adapters import datablitz, laptop_factory, oct, pcc, pcx
from .offers import clean_text, url_host
from .scraping import FetchedPage, Transport, TransportError, TransportResponse

RESULTS = "RESULTS"
NO_RESULTS = "NO_RESULTS"
SEARCH_ERROR = "SEARCH_ERROR"
UNSUPPORTED_LAYOUT = "UNSUPPORTED_LAYOUT"
NOT_SENT_BUDGET = "NOT_SENT_BUDGET_EXHAUSTED"
NOT_SENT_LIMIT = "NOT_SENT_SEARCH_LIMIT"
NOT_SENT_REPLAY_ONLY = "NOT_SENT_REPLAY_ONLY"
NOT_SENT_QUERY_LIMIT = "NOT_SENT_QUERY_LIMIT"
SEARCH_OUTCOMES = (RESULTS, NO_RESULTS, SEARCH_ERROR, UNSUPPORTED_LAYOUT, NOT_SENT_BUDGET,
                   NOT_SENT_LIMIT, NOT_SENT_REPLAY_ONLY, NOT_SENT_QUERY_LIMIT)


class SearchLayoutError(ValueError):
    """The response is not a search result page this module recognizes."""


# Routes (2026-11-03). The native route is the retailer's own search (WooCommerce results page,
# Shopify predictive search). The storefront route is the Shopify product-only search results page
# (``/search?q=<query>&type=product``), an explicit opt-in second route with its own request cap.
NATIVE_ROUTE = ""
STOREFRONT_ROUTE = "STOREFRONT_SEARCH_PAGE"
# How Shopify predictive search treats products that cannot be bought (2026-11-03). ``last`` lists
# them after the available ones, so a page capped at ten results can cut them off; ``show`` keeps
# them in relevance order. Neither makes the cap go away: the response is never exhaustive.
UNAVAILABLE_LAST = "last"
UNAVAILABLE_SHOW = "show"
UNAVAILABLE_MODES = (UNAVAILABLE_LAST, UNAVAILABLE_SHOW)
_UNAVAILABLE_PARAMETER = "resources[options][unavailable_products]"


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
    # The ``variant`` the result link names, when it names one (2026-11-03). Predictive search
    # normally lists products, not variants; when a link does name a variant, two links to the
    # same product with different variants stay two leads (``unique_hits``).
    variant_id: str = ""


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

    @property
    def is_shopify(self) -> bool:
        return self.kind == "shopify_predictive_json"

    def first_page_url(self, query: str, route: str = NATIVE_ROUTE,
                       unavailable: str = UNAVAILABLE_LAST) -> str:
        if self.kind == "woocommerce_html":
            return f"https://{self.host}/?s={quote_plus(query)}&post_type=product"
        if route == STOREFRONT_ROUTE:
            return f"https://{self.host}/search?{urlencode({'q': query, 'type': 'product'})}"
        params = {
            "q": query, "resources[type]": "product", "resources[limit]": str(self.page_size),
            _UNAVAILABLE_PARAMETER: unavailable,
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

    def parse(self, page: FetchedPage, route: str = NATIVE_ROUTE) -> ParsedSearchPage:
        if self.kind == "woocommerce_html":
            return _parse_woocommerce(self, page)
        if route == STOREFRONT_ROUTE:
            return _parse_shopify_search_page(self, page)
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
            variant_id=_variant_of(item["url"]),
        ))
    return ParsedSearchPage(tuple(hits), "", filtered, len(products) >= method.page_size)


def _variant_of(link: str) -> str:
    """The ``variant`` query value of a result link, or ``""``."""
    values = dict(parse_qsl(urlsplit(html.unescape(link.strip())).query)).get("variant", "")
    return values.strip()


# Shopify storefront search page (opt-in second route)


class _SearchPageParser(HTMLParser):
    """Product links, their visible titles, and the ``next`` page link of a Shopify search page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.link: str | None = None
        self.text: list[str] = []
        self.title_attr = ""
        self.cards: list[tuple[str, str]] = []
        self.next_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        rel = attributes.get("rel", "").split()
        classes = attributes.get("class", "").split()
        if tag in {"a", "link"} and attributes.get("href") and (
            "next" in rel or any(c in {"pagination__next", "next"} for c in classes)
        ):
            self.next_links.append(attributes["href"])
        if tag == "a" and attributes.get("href") and "/products/" in attributes["href"]:
            self.link = attributes["href"]
            self.title_attr = attributes.get("title", "")
            self.text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.link is not None:
            self.cards.append((self.link, clean_text("".join(self.text)) or self.title_attr))
            self.link = None

    def handle_data(self, data: str) -> None:
        if self.link is not None:
            self.text.append(data)


_NO_PRODUCT_RESULTS = re.compile(
    r"no\s+(?:results|products)\s+found|did\s+not\s+match\s+any|no\s+results\s+for", re.I)


def _parse_shopify_search_page(method: SearchMethod, page: FetchedPage) -> ParsedSearchPage:
    """Product links of the product-only storefront search page, in listed order.

    Several anchors point at one product (image, title); the longest visible title wins. A page
    with neither product links nor a "no results" statement is not readable (DataBlitz renders
    its search page in the browser, so it has no result links): ``SearchLayoutError``.
    """
    if "html" not in page.content_type.lower():
        raise SearchLayoutError("The storefront search page is not HTML.")
    text = page.text()
    parser = _SearchPageParser()
    parser.feed(text)
    parser.close()
    merged: dict[str, tuple[str, str, str]] = {}
    filtered = 0
    for link, title in parser.cards:
        product = method.canonical_product_url(link, page.final_url)
        if not product:
            filtered += 1
            continue
        variant = _variant_of(link)
        key = product + (f"?variant={variant}" if variant else "")
        if key not in merged or len(title) > len(merged[key][1]):
            merged[key] = (product, title, urljoin(page.final_url, html.unescape(link)))
    if not merged:
        if _NO_PRODUCT_RESULTS.search(text):
            return ParsedSearchPage(())
        raise SearchLayoutError("The storefront search page lists no product links and no "
                                "'no results' statement (it may be rendered in the browser).")
    page_number = int(dict(parse_qsl(urlsplit(page.requested_url).query)).get("page", "1") or 1)
    hits = [
        SearchHit(method.retailer_id, product, title or product.rsplit("/", 1)[-1], listed,
                  page_number, number, variant_id=_variant_of(listed))
        for number, (product, title, listed) in enumerate(merged.values(), start=1)
    ]
    next_url = ""
    for link in parser.next_links:
        candidate = urljoin(page.final_url, html.unescape(link))
        if url_host(candidate) == method.host and "q=" in urlsplit(candidate).query:
            next_url = candidate
            break
    return ParsedSearchPage(tuple(hits), next_url, filtered)


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
                retryable=False, sent=False,
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
    route: str = NATIVE_ROUTE  # NATIVE_ROUTE or STOREFRONT_ROUTE.


TIER_MODEL_CODE = "1_FULL_MODEL_CODE"
TIER_PART_NUMBER = "2_VERIFIED_PART_NUMBER"
TIER_MODEL_SPECS = "3_MODEL_CODE_WITH_SPECS"
TIER_FAMILY = "4_BRAND_FAMILY"
TIER_FAMILY_SPECS = "5_FAMILY_WITH_SPECS"
# Targets identified by a marketed product name (2026-10-19, second review): no code to search.
TIER_PRODUCT_NAME = "1_PRODUCT_NAME"
TIER_NAME_SPECS = "3_PRODUCT_NAME_WITH_SPECS"
# Identifier spellings (2026-10-22). On 2026-10-20 the complete hyphenated code returned no
# result at all on Laptop Factory and PC Express for listed products (B12UC-1432PH,
# E1504FA-NJ1527W, X1404VA-EB2264WSM, 14-ep0445TU, G614PM-TS179WSM), whose titles or URLs show
# the code. These tiers search the same verified code with spaces for punctuation, then its most
# distinctive segment. They are search text only: the page must still show the complete code.
TIER_MODEL_CODE_SPACED = "1_FULL_MODEL_CODE_SPACED"
TIER_CODE_SEGMENT = "3_MODEL_CODE_SEGMENT"
# Punctuation and spacing spellings of one code (2026-11-03): ``15-fc0933AU`` is also searched as
# ``15 fc0933AU`` (spaced), ``15fc0933AU`` (compact) and, for a spaced catalog value,
# ``15-fc0933AU`` (hyphenated). Search text only, like the spaced spelling.
TIER_MODEL_CODE_COMPACT = "1_FULL_MODEL_CODE_COMPACT"
TIER_MODEL_CODE_HYPHENATED = "1_FULL_MODEL_CODE_HYPHENATED"
# A product-name target's name with its processor and no capacities (2026-11-03). Retailer titles
# often omit RAM (``APPLE MacBook Neo A18 Pro 256GB``), so the name-with-capacities query returns
# nothing; the narrower query also keeps the result count under the predictive-search cap.
TIER_NAME_CPU = "2_PRODUCT_NAME_CPU"
# The product-only storefront search page of a Shopify store: an explicit second route, off by
# default, with its own request cap (``--max-storefront-search-requests``).
TIER_STOREFRONT = "6_STOREFRONT_SEARCH_PAGE"
# A code target without a recorded series (2026-10-22): brand, recorded processor model and
# capacities instead of the skipped family tiers.
TIER_BRAND_CPU_SPECS = "5_BRAND_CPU_MODEL_WITH_SPECS"
FALLBACK_TIERS = frozenset({TIER_PART_NUMBER, TIER_MODEL_SPECS, TIER_FAMILY, TIER_FAMILY_SPECS,
                            TIER_NAME_SPECS, TIER_MODEL_CODE_SPACED, TIER_CODE_SEGMENT,
                            TIER_BRAND_CPU_SPECS, TIER_MODEL_CODE_COMPACT,
                            TIER_MODEL_CODE_HYPHENATED, TIER_NAME_CPU, TIER_STOREFRONT})
FIRST_TIERS = frozenset({TIER_MODEL_CODE, TIER_PRODUCT_NAME})
# Tiers added to a job only when the complete code returned no result (discover_products.py).
VARIANT_TIERS = frozenset({TIER_MODEL_CODE_SPACED, TIER_MODEL_CODE_COMPACT,
                           TIER_MODEL_CODE_HYPHENATED, TIER_CODE_SEGMENT})
_CODE_PUNCTUATION = re.compile(r"(?<=[0-9A-Za-z])[-‐-―−/_.](?=[0-9A-Za-z])")


def spaced_code(code: str) -> str:
    """``B12UC-1432PH`` -> ``B12UC 1432PH``; ``""`` when the code has no inner punctuation."""
    spaced = _CODE_PUNCTUATION.sub(" ", code.strip())
    return spaced if spaced != code.strip() else ""


def compact_code(code: str) -> str:
    """``15-fc0933AU`` -> ``15fc0933AU``; ``""`` when the code has no inner punctuation or space."""
    value = " ".join(code.split())
    compact = re.sub(r"(?<=[0-9A-Za-z])[-‐-―−/_.\s](?=[0-9A-Za-z])", "", value)
    return compact if compact != value else ""


def hyphenated_code(code: str) -> str:
    """``15 fc0933au`` -> ``15-fc0933au``: a spaced catalog value joined with hyphens, or ``""``
    when it has no spaces (a code that already has punctuation is not re-punctuated)."""
    value = " ".join(code.split())
    if " " not in value or _CODE_PUNCTUATION.search(value):
        return ""
    return value.replace(" ", "-")


def code_spellings(code: str) -> list[tuple[str, str]]:
    """``[(tier, spelling)]``: the safe spacing and punctuation spellings of one verified code.

    Case is presentation (search is case-insensitive) and dash style is folded by ``_query_key``;
    only hyphen/space/none differ. Never a made-up code: every spelling is the same characters.
    """
    found = []
    if spaced := spaced_code(code):
        found.append((TIER_MODEL_CODE_SPACED, spaced))
    if compact := compact_code(code):
        found.append((TIER_MODEL_CODE_COMPACT, compact))
    if hyphen := hyphenated_code(code):
        found.append((TIER_MODEL_CODE_HYPHENATED, hyphen))
    return found


def distinctive_segment(code: str) -> str:
    """The most specific segment of a punctuated code that can stand alone as search text.

    The last segment of at least five characters mixing letters and digits (``1432PH`` of
    ``B12UC-1432PH``, ``ep0445TU`` of ``14-ep0445TU``, ``ANV15`` of ``ANV15-42-R2UH``), or ``""``.
    It never replaces the code: a result is a candidate only when it shows the complete code.
    """
    segments = [s for s in _CODE_PUNCTUATION.split(code.strip()) if s]
    if len(segments) < 2:
        return ""
    for segment in reversed(segments):
        if len(segment) >= 5 and re.search(r"[A-Za-z]", segment) and re.search(r"\d", segment):
            return segment
    return ""


def distinctive_cpu_model(cpu_model: str, cpu_series: str) -> str:
    """A recorded processor model worth searching (``i3-1315U``), not a bare series name."""
    value = clean_text(cpu_model or "")
    if not value or value.casefold() == clean_text(cpu_series or "").casefold():
        return ""
    return value if re.search(r"\d{3,}", value) else ""


def _capacity_words(ram: str, storage: str) -> str:
    """``16GB 512GB`` / ``16GB 1TB`` search words from catalog GB values (search text only)."""
    words = []
    if ram.strip().isdigit():
        words.append(f"{int(ram)}GB")
    if storage.strip().isdigit():
        amount = int(storage)
        words.append(f"{amount // 1000}TB" if amount >= 1000 and amount % 1000 == 0
                     else f"{amount}GB")
    return " ".join(words)


def _without_repeated_words(text: str) -> str:
    """``Apple MacBook Neo Apple A18 Pro`` -> ``Apple MacBook Neo A18 Pro``: a word already
    present earlier is dropped (search text only; never an identity value)."""
    seen: set[str] = set()
    kept = []
    for word in text.split():
        if word.casefold() in seen:
            continue
        seen.add(word.casefold())
        kept.append(word)
    return " ".join(kept)


without_repeated_words = _without_repeated_words


def _query_key(text: str) -> str:
    """Case, spacing and dash style are presentation: ``FA607NUQ-RL011W`` and the same code with
    an en dash are one search. Punctuation that the store may tokenize differently (hyphen
    versus space) is not folded: the spaced spelling is a deliberate second search."""
    dashes = re.sub("[‐-―−]", "-", text)
    return " ".join(dashes.casefold().split())


# Query tiers of a recorded manufacturer code on a family- or product-name target.
_RECORDED_CODE_TIERS = frozenset({TIER_MODEL_CODE, TIER_MODEL_CODE_SPACED, TIER_PART_NUMBER,
                                  TIER_CODE_SEGMENT})


def search_queries(
    target_id: str, retailer_id: str, *, model: str, part_number: str, brand: str, series: str,
    core: Mapping[str, str] | None = None, kind: str = "MODEL_CODE", cpu_model: str = "",
    recorded_code: str = "", storefront: bool = False,
) -> list[SearchQuery]:
    """Ordered, de-duplicated queries for one target (see ``_code_queries`` for code targets).

    A target whose catalog model is a product or family name (``kind`` other than
    ``MODEL_CODE``) has no code to search from its name: its queries are the brand and name, the
    name with processor and capacities, then the brand and series, each said to be a name, never
    a code. No code is ever made up from a name.

    A broad display name can still record one verified manufacturer code (``recorded_code``,
    TARGET_MASTER ``manufacturer_model_code``: ``TUF A16`` records ``FA607NUQ-RL011W``). That
    code is searched first, with the same spellings as a code target (2026-10-22), before any
    name query; it is the only code ever taken from the catalog, never derived from a name.
    Queries identical up to case, spacing and dash style are sent once. With ``storefront`` a
    Shopify retailer also gets one product-only storefront search query (the complete code, or the
    name with its processor) after every native query: a second route, never a replacement.
    """
    recorded = clean_text(recorded_code or "")
    if kind == "MODEL_CODE":
        queries = _code_queries(target_id, retailer_id, model=model, part_number=part_number,
                                brand=brand, series=series, core=core, cpu_model=cpu_model)
    else:
        queries = []
        if recorded:
            queries = [q for q in _code_queries(
                target_id, retailer_id, model=recorded, part_number=part_number, brand=brand,
                series="", core=core, cpu_model="")
                if q.tier in _RECORDED_CODE_TIERS]
            queries = [dataclasses.replace(q, basis=q.basis.replace(
                "from TARGET_MASTER", "recorded in TARGET_MASTER manufacturer_model_code "
                "(the catalog model is a broad name)")) for q in queries]
        queries += _name_queries(target_id, retailer_id, model=model, part_number=part_number,
                                 brand=brand, series=series, core=core or {})
    if kind == "FAMILY_NAME":
        # A family name cannot establish one product: two queries gather review leads for the
        # catalog reconciliation; broader family tiers would only repeat them.
        queries = [q for q in queries if q.tier in {TIER_PRODUCT_NAME, TIER_NAME_SPECS,
                                                    TIER_PART_NUMBER} | _RECORDED_CODE_TIERS]
    if storefront and SEARCH_METHODS[retailer_id].is_shopify and queries:
        first = queries[0]
        basis = ("complete model code on the Shopify product-only storefront search page"
                 if first.tier == TIER_MODEL_CODE
                 else "product name with processor on the Shopify product-only storefront "
                      "search page")
        text = first.text
        if first.tier != TIER_MODEL_CODE:
            text = next((q.text for q in queries if q.tier == TIER_NAME_CPU), first.text)
        queries.append(SearchQuery(target_id, retailer_id, TIER_STOREFRONT, text,
                                   first.verified_identifier, basis, STOREFRONT_ROUTE))
    unique: list[SearchQuery] = []
    seen: set[tuple[str, str]] = set()
    for query in queries:
        key = (query.route, _query_key(query.text))
        if key[1] and key not in seen:
            seen.add(key)
            unique.append(query)
    return unique


def _name_queries(target_id, retailer_id, *, model, part_number, brand, series, core):
    capacities = _capacity_words(core.get("ram_capacity_gb", ""),
                                 core.get("storage_capacity_gb", ""))
    name = model.strip()
    branded = name if name.casefold().startswith(brand.casefold()) or not brand else (
        f"{brand} {name}")
    queries = [SearchQuery(target_id, retailer_id, TIER_PRODUCT_NAME, clean_text(branded), False,
                           "brand and catalog product name (no manufacturer code recorded)")]
    if part_number.strip():
        queries.append(SearchQuery(target_id, retailer_id, TIER_PART_NUMBER, part_number.strip(),
                                   True, "manufacturer_part_number in TARGET_MASTER"))
    named_cpu = _without_repeated_words(clean_text(f"{branded} {core.get('cpu_series', '')}"))
    if named_cpu.casefold() != clean_text(branded).casefold():
        queries.append(SearchQuery(
            target_id, retailer_id, TIER_NAME_CPU, named_cpu, False,
            "product name plus processor, no capacities (retailer titles often omit RAM; "
            "a narrower search keeps the result count under the predictive-search cap)"))
    detailed = _without_repeated_words(clean_text(
        f"{branded} {core.get('cpu_series', '')} {capacities}"))
    queries.append(SearchQuery(target_id, retailer_id, TIER_NAME_SPECS, detailed, False,
                               "product name plus processor, RAM, and storage (fallback)"))
    family = clean_text(f"{brand} {series}")
    if series.strip():
        queries.append(SearchQuery(target_id, retailer_id, TIER_FAMILY, family, False,
                                   "brand and series (broader fallback)"))
        queries.append(SearchQuery(
            target_id, retailer_id, TIER_FAMILY_SPECS, _without_repeated_words(clean_text(
                f"{family} {core.get('cpu_series', '')} {capacities}")), False,
            "family plus CPU, RAM, and storage (fallback)"))
    return queries


def _code_queries(
    target_id: str, retailer_id: str, *, model: str, part_number: str, brand: str, series: str,
    core: Mapping[str, str] | None = None, cpu_model: str = "",
) -> list[SearchQuery]:
    """Ordered queries: the complete model code first, then fallbacks.

    1. The complete model code from TARGET_MASTER, with no specification filter.
    2. The same code with spaces for its punctuation (2026-10-22), when it has any.
    3. The verified part number, when TARGET_MASTER records it (owner-verified). Retailer SKUs
       and unverified identifiers are never search terms here.
    4. The code's most distinctive segment (2026-10-22), e.g. ``1432PH`` of ``B12UC-1432PH``.
    5. The model code with the configuration's RAM and storage (2026-10-19), to narrow a code
       search whose results were truncated or showed only other configurations.
    6. The broader brand and series; 7. the family with CPU, RAM, and storage; or, with no
       series recorded, the brand with the recorded processor model and capacities.

    Every query after the first runs only while no plausible candidate has been found (decided
    by the caller). Search words are discovery hints; a result is only a candidate, and the
    configuration's values never fill an offer's missing specification. A spelling or segment
    query relaxes the search text only: a result still needs the complete code in its title to
    be a plausible candidate, and the matcher decides identity from the page.
    """
    core = core or {}
    model = model.strip()
    capacities = _capacity_words(core.get("ram_capacity_gb", ""),
                                 core.get("storage_capacity_gb", ""))
    queries = [SearchQuery(target_id, retailer_id, TIER_MODEL_CODE, model, True,
                           "complete model code from TARGET_MASTER")]
    for tier, spelling in code_spellings(model):
        queries.append(SearchQuery(
            target_id, retailer_id, tier, spelling, True,
            {TIER_MODEL_CODE_SPACED: "complete model code with spaces for punctuation (store "
                                     "search tokenization)",
             TIER_MODEL_CODE_COMPACT: "complete model code without punctuation or spaces (store "
                                      "search tokenization)",
             TIER_MODEL_CODE_HYPHENATED: "complete model code with hyphens for spaces (store "
                                         "search tokenization)"}[tier]))
    if part_number.strip():
        queries.append(SearchQuery(target_id, retailer_id, TIER_PART_NUMBER, part_number.strip(),
                                   True, "manufacturer_part_number in TARGET_MASTER"))
    if segment := distinctive_segment(model):
        queries.append(SearchQuery(target_id, retailer_id, TIER_CODE_SEGMENT, segment, False,
                                   "most distinctive segment of the verified model code (search "
                                   "text only; the complete code must appear on the result)"))
    if capacities:
        queries.append(SearchQuery(target_id, retailer_id, TIER_MODEL_SPECS,
                                   clean_text(f"{model} {capacities}"), False,
                                   "model code plus RAM and storage (fallback)"))
    family = clean_text(f"{brand} {series}")
    # A brand alone ("Lenovo") is not a family: with a blank series these broad queries filled
    # the 10-result cap with other products and found nothing (2026-10-19, second review).
    if series.strip() and family.casefold() != model.casefold():
        queries.append(SearchQuery(target_id, retailer_id, TIER_FAMILY, family, False,
                                   "brand and series (broader fallback)"))
        detailed = _without_repeated_words(clean_text(
            f"{family} {core.get('cpu_series', '')} {capacities}"))
        if detailed.casefold() != family.casefold():
            queries.append(SearchQuery(target_id, retailer_id, TIER_FAMILY_SPECS, detailed,
                                       False, "family plus CPU, RAM, and storage (fallback)"))
    elif brand.strip() and (processor := distinctive_cpu_model(cpu_model,
                                                                core.get("cpu_series", ""))):
        queries.append(SearchQuery(
            target_id, retailer_id, TIER_BRAND_CPU_SPECS,
            clean_text(f"{brand} {processor} {capacities}"), False,
            "brand, recorded processor model, RAM and storage (no series recorded; fallback)"))
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
    # Where the response came from when no request was sent: "SAME_RUN" (an identical search
    # earlier in this run) or "SAVED:<folder>" (a saved response replayed from an earlier run).
    cache: str = ""
    # Why this query ran (or was skipped), e.g. which earlier results left no plausible candidate.
    run_reason: str = ""

    @property
    def completeness(self) -> str:
        """Whether this response can be read as the retailer's full answer to the query.

        Predictive search lists at most ten products and has no next page: ten results mean more
        may exist (``INCOMPLETE_RESULT_CAP``) and fewer than ten are still a relevance-ranked
        suggestion list, never a catalog scan. A results page whose next page was not read is
        ``INCOMPLETE_PAGES_NOT_READ``. A response that was not read (error, not sent) says so.
        """
        if self.outcome in {RESULTS, NO_RESULTS}:
            if self.possibly_truncated:
                return ("INCOMPLETE_PAGES_NOT_READ" if self.next_url else "INCOMPLETE_RESULT_CAP")
            return "COMPLETE_FOR_QUERY"
        return "NOT_READ"


SEARCH_RECORD_COLUMNS: tuple[str, ...] = (
    "target_id", "retailer_id", "tier", "query", "verified_identifier", "page_number", "url",
    "outcome", "http_status", "final_url", "retrieved_at", "attempts", "content_sha256",
    "evidence_file", "hits", "filtered_links", "possibly_truncated", "next_url", "message",
    "cache", "run_reason",
    # Route and completeness (2026-11-03): additive columns.
    "route", "completeness",
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
        "cache": record.cache,
        "run_reason": record.run_reason,
        "route": query.route or "NATIVE",
        "completeness": record.completeness,
    }


def search_cache_key(url: str, *, legacy: bool = False) -> str:
    """One key per distinct search: host case, the search words' case and spacing ignored
    (``IdeaPad`` and ``Ideapad`` are one search on these stores). ``legacy`` also drops the
    predictive search's ``unavailable_products`` option, the key of a response saved before that
    option was sent (2026-11-03; those stores already listed unavailable products last)."""
    parts = urlsplit(url)
    query = [
        (name, " ".join(value.casefold().split()) if name in {"q", "s"} else value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not (legacy and name == _UNAVAILABLE_PARAMETER)
    ]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(query), ""))


def unique_hits(records: Sequence[SearchRecord]) -> list[tuple[SearchHit, list[SearchRecord]]]:
    """Each product URL once, with every search request that listed it (duplicates merged).

    A result link that names a ``variant`` is a lead of its own: the same product listed with two
    variants stays two entries, and a variant link never merges into the bare product (2026-11-03).
    """
    merged: dict[tuple[str, str], tuple[SearchHit, list[SearchRecord]]] = {}
    for record in records:
        for hit in record.hit_items:
            key = (hit.product_url, hit.variant_id)
            if key in merged:
                merged[key][1].append(record)
            else:
                merged[key] = (hit, [record])
    return list(merged.values())
