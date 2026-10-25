"""Cached retailer catalog search and opt-in product retrieval.

Search text and snippets are leads. Product facts come only from a fresh retailer
response; a successful discovery is never configuration or price-comparison proof.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from .catalog_leads import CATALOG_SOURCES, parse_sitemap, product_entries
from .retailer_search import SEARCH_METHODS
from .scraping import Fetcher, FetchError, ScrapeRequest


class DiscoveryError(ValueError):
    """A search or URL could not be safely used; never an absence claim."""


def product_url(url: str, retailer_id: str) -> str:
    """Normalize actual retailer links, including Shopify collection routes.

    Preserve variant selection. Strip tracking; never manufacture product handles.
    Hosts, ports, userinfo and paths are checked structurally, not by substring.
    """
    method = SEARCH_METHODS[retailer_id]
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != method.host or parts.username
            or parts.password or parts.port not in (None, 443)):
        raise DiscoveryError("URL is outside the approved retailer origin")
    path = parts.path
    collection = re.fullmatch(r"/collections/[^/]+/products/([^/]+)", path)
    if collection and method.kind == "shopify_predictive_json":
        path = "/products/" + collection[1]
    if not re.fullmatch(method.product_path, path):
        raise DiscoveryError("URL is not a supported retailer product page")
    variants = [(key, value) for key, value in parse_qsl(parts.query) if key == "variant"]
    if len(variants) > 1 or any(not value.isdecimal() for _, value in variants):
        raise DiscoveryError("Ambiguous or invalid variant selection")
    return urlunsplit(("https", method.host, path, urlencode(variants), ""))


def queries(case: dict[str, str], limit: int = 3) -> list[str]:
    """Exact identifier, punctuation variation, then family/specification clues."""
    if not 1 <= limit <= 3:
        raise DiscoveryError("Query limit must be between 1 and 3")
    host = SEARCH_METHODS[case["retailer_id"]].host
    model = " ".join(case.get("model", "").split())
    if not model or len(model) > 200 or any(c in model for c in '\n\r"'):
        raise DiscoveryError("A short model or product name is required")
    alternatives = [f'site:{host} "{model}"']
    spaced = re.sub(r"[-–—/]", " ", model)
    alternatives.append(f"site:{host} {case.get('brand', '')} {spaced}".strip())
    broader = " ".join(case.get(k, "") for k in ("brand", "family", "specs")).strip()
    if broader:
        alternatives.append(f"site:{host} {broader}")
    result = []
    seen = set()
    for query in alternatives:
        query = " ".join(query.split())
        key = query.replace('"', '').casefold()
        if key not in seen:
            result.append(query)
            seen.add(key)
    return result[:limit]


@dataclass
class SearchResponse:
    query: str
    outcome: str
    retrieved_at: str
    results: list[dict[str, str]] = field(default_factory=list)
    search_calls: int = 0
    elapsed_seconds: float = 0
    error: str = ""


def _tokens(text: str) -> list[str]:
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(GB|TB)\b", lambda m: (
        format((Decimal(m[1]) * (1000 if m[2].lower() == "tb" else 1)).normalize(), "f") + "GB"
    ), text, flags=re.I)
    return re.findall(r"[a-z0-9]+", unquote(text).casefold())


def parse_product_feed(body: bytes, retailer_id: str) -> list[dict[str, str]]:
    """Public WooCommerce product metadata for discovery, excluding prices/stock."""
    if retailer_id != "laptop_factory":
        raise DiscoveryError("No public product feed configured for this retailer")
    data = json.loads(body)
    if not isinstance(data, list) or len(data) > 100:
        raise DiscoveryError("Unexpected product-feed response")
    entries = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise DiscoveryError("Malformed product-feed item")
        url = product_url(item.get("permalink", ""), retailer_id)
        text = " ".join(str(item.get(key) or "") for key in (
            "sku", "short_description", "description"))
        text = " ".join(unescape(re.sub(r"<[^>]*>", " ", text)).split())[:4000]
        entries.append({"retailer_id": retailer_id, "url": url,
                        "title": unescape(item["name"]), "search_text": text, "lastmod": ""})
    return entries


class CatalogIndex:
    """Search one saved retailer sitemap snapshot using an in-memory inverted index.

    No network, models, external search service or credentials. All terms must be
    present; joined adjacent tokens support punctuation variations of model codes.
    A result is a candidate URL, never a configuration match or a current price.
    """

    backend = "retailer_sitemap_index"

    def __init__(self, entries: list[dict[str, str]], *, retrieved_at: str,
                 coverage: dict | None = None, max_results: int = 5):
        if not 1 <= max_results <= 10:
            raise DiscoveryError("Result limit must be between 1 and 10")
        self.retrieved_at, self.max_results = retrieved_at, max_results
        self.coverage = coverage or {}
        self.entries, self.postings = [], {}
        merged = {}
        for entry in entries:
            retailer = entry["retailer_id"]
            url = product_url(entry["url"], retailer)
            key = (retailer, url)
            # A public feed enriches the same sitemap URL; it cannot replace identity.
            previous = merged.get(key, {})
            merged[key] = {**previous, **{k: v for k, v in entry.items() if v or k not in previous},
                           "url": url}
        for entry in merged.values():
            retailer, url = entry["retailer_id"], entry["url"]
            index = len(self.entries)
            self.entries.append({**entry, "url": url})
            words = _tokens(" ".join([urlsplit(url).path, entry.get("title", ""),
                                      entry.get("search_text", "")]))
            terms = {"".join(words[i:i + width]) for i in range(len(words))
                     for width in range(1, 4) if i + width <= len(words)}
            for term in terms:
                self.postings.setdefault((retailer, term), set()).add(index)

    @classmethod
    def from_run(cls, folder: Path, max_results: int = 5, *,
                 feed_runs: tuple[Path, ...] = ()) -> CatalogIndex:
        """Reparse hash-checked raw responses from discover_catalog_leads.py.

        Ignore derived lead/entry CSVs: the caller's target scope and expected URLs
        cannot enter this index. Partial coverage stays visible in the manifest.
        """
        summary = json.loads((folder / "catalog_summary.json").read_text(encoding="utf-8"))
        if summary.get("mode") != "LIVE":
            raise DiscoveryError("Index source must be an original live catalog snapshot")
        entries = []
        with (folder / "catalog_requests.csv").open(encoding="utf-8-sig", newline="") as handle:
            for record in csv.DictReader(handle):
                if record["outcome"] != "FETCHED" or record["kind"] not in {
                    "sitemap_index", "product_sitemap", "product_feed"
                }:
                    continue
                retailer = record["retailer_id"]
                source = CATALOG_SOURCES[retailer]
                origin = urlsplit(record["url"])
                if (origin.scheme != "https"
                        or origin.netloc != urlsplit(source.root_sitemap).netloc):
                    raise DiscoveryError("Saved sitemap is outside the retailer origin")
                relative = Path(record["evidence_file"])
                resolved = (folder / relative).resolve()
                if (relative.is_absolute() or ".." in relative.parts
                        or not resolved.is_relative_to(folder.resolve())):
                    raise DiscoveryError("Unsafe saved sitemap path")
                body = resolved.read_bytes()
                if hashlib.sha256(body).hexdigest() != record["content_sha256"]:
                    raise DiscoveryError("Saved sitemap hash differs")
                if record["kind"] == "product_feed":
                    for entry in parse_product_feed(body, retailer):
                        entries.append({**entry, "sitemap": record["url"],
                                        "source_retrieved_at": record["retrieved_at"]})
                    continue
                sitemap = parse_sitemap(body)
                if sitemap.kind == "urlset":
                    for entry in product_entries(source, sitemap):
                        entries.append({"retailer_id": retailer, "url": entry.loc,
                                        "title": entry.title, "lastmod": entry.lastmod,
                                        "sitemap": record["url"],
                                        "source_retrieved_at": record["retrieved_at"]})
        coverage = summary["retailers"]
        retrieved_at = summary["finished_at_utc"]
        for feed_run in feed_runs:
            extra = cls.from_run(feed_run, max_results)
            entries.extend(extra.entries)
            coverage.update(extra.coverage)
            retrieved_at = max(retrieved_at, extra.retrieved_at)
        return cls(entries, retrieved_at=retrieved_at, coverage=coverage, max_results=max_results)

    def search(self, query: str, *, clues: str = "") -> SearchResponse:
        started = time.monotonic()
        response = SearchResponse(query, "SEARCH_ERROR", self.retrieved_at)
        scope, separator, words = query.partition(" ")
        host = scope.removeprefix("site:")
        retailers = [r for r, method in SEARCH_METHODS.items() if method.host == host]
        if not scope.startswith("site:") or not separator or not retailers or not _tokens(words):
            response.error = "A supported retailer and query words are required"
            return response
        retailer = retailers[0]
        status = self.coverage.get(retailer, {})
        if not status.get("product_sitemaps_read", 0):
            response.error = "Retailer sitemap snapshot is unavailable; absence is unproven"
            return response
        terms = set(_tokens(words))
        postings = [self.postings.get((retailer, term), set()) for term in terms]
        matches = set.intersection(*postings)
        # Model/name words remain required. Other recorded facts only rank leads;
        # missing metadata never becomes a configuration match or inferred value.
        clue_terms = set(_tokens(clues)) - terms
        ordered = sorted(matches, key=lambda i: (
            -sum(i in self.postings.get((retailer, term), ()) for term in clue_terms),
            self.entries[i]["url"],
        ))
        for index in ordered[:self.max_results]:
            entry = self.entries[index]
            response.results.append({"url": entry["url"], "title": entry.get("title", ""),
                                     "snippet": "Sitemap candidate; current details unverified",
                                     "source_retrieved_at": entry.get("source_retrieved_at", "")})
        complete = status.get("product_sitemaps_read") == status.get("product_sitemaps_listed")
        response.outcome = "RESULTS" if response.results else (
            "NO_RESULTS" if complete else "INDEX_INCOMPLETE")
        if not response.results and not complete:
            response.error = "Only part of this retailer sitemap was indexed"
        response.elapsed_seconds = round(time.monotonic() - started, 6)
        return response


def leads(responses: list[SearchResponse], retailer_id: str) -> list[dict[str, str]]:
    result, seen = [], set()
    for response in responses:
        if response.outcome != "RESULTS":
            continue
        for item in response.results:
            try:
                url = product_url(item["url"], retailer_id)
            except (ValueError, KeyError):
                continue
            if url not in seen:
                result.append({**item, "url": url, "query": response.query,
                               "search_retrieved_at": item.get("source_retrieved_at")
                               or response.retrieved_at})
                seen.add(url)
    return result


class _ProductHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.canonical = ""
        self.title = []
        self.in_title = False
        self.in_json = False
        self.json_text = []
        self.nodes = []
        self.product_marker = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "link" and attributes.get("rel") == "canonical":
            self.canonical = attributes.get("href", "")
        if tag == "meta" and attributes.get("property") == "og:type":
            self.product_marker |= "product" in (attributes.get("content") or "").lower()
        if tag == "title":
            self.in_title = True
        if tag == "script" and attributes.get("type") == "application/ld+json":
            self.in_json, self.json_text = True, []

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if self.in_json:
            self.json_text.append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "script" and self.in_json:
            self.in_json = False
            try:
                data = json.loads("".join(self.json_text))
                self.nodes.extend(data if isinstance(data, list) else [data])
            except ValueError:
                pass


def inspect_html(body: bytes, url: str, retailer_id: str, retrieved_at: str) -> dict:
    """Retain independent page facts, including each separately stated offer.

    No inferred specifications, selected variants or target associations are added.
    """
    url = product_url(url, retailer_id)
    parser = _ProductHTML()
    parser.feed(body.decode("utf-8", errors="replace"))
    canonical = (product_url(urljoin(url, parser.canonical), retailer_id)
                 if parser.canonical else url)
    # A canonical usually omits the variant; keep the actual requested selection.
    variant = parse_qsl(urlsplit(url).query)
    if variant:
        canonical = urlunsplit((*urlsplit(canonical)[:3], urlencode(variant), ""))
    nodes = list(parser.nodes)
    products = []
    while nodes:
        node = nodes.pop(0)
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("@graph"), list):
            nodes.extend(node["@graph"])
        types = node.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if not isinstance(types, list):
            continue
        if "ProductGroup" in types and isinstance(node.get("hasVariant"), list):
            nodes.extend(node["hasVariant"])
        if "Product" in types:
            products.append({k: node[k] for k in (
                "name", "brand", "sku", "mpn", "description", "offers", "additionalProperty"
            ) if k in node})
    return {
        "url": url, "canonical_url": canonical, "retrieved_at": retrieved_at,
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "title": "".join(parser.title).strip(), "products": products,
        "outcome": "PRODUCT_PAGE" if products or parser.product_marker else "UNCONFIRMED_PAGE",
    }


def fetch_product(url: str, retailer_id: str, fetcher: Fetcher | None = None) -> tuple[dict, bytes]:
    started = time.monotonic()
    record = {"url": url, "retrieved_at": datetime.now(UTC).isoformat(),
              "outcome": "FETCH_ERROR"}
    body = b""
    try:
        url = product_url(url, retailer_id)
        host = SEARCH_METHODS[retailer_id].host
        page = (fetcher or Fetcher()).fetch(ScrapeRequest(retailer_id, url), frozenset({host}))
        record["http_status"] = page.status_code
        record["retrieved_at"] = page.retrieved_at.isoformat()
        if "html" not in page.content_type.lower():
            raise DiscoveryError("Response is not HTML")
        body = page.body
        record["content_sha256"] = hashlib.sha256(body).hexdigest()
        record.update(inspect_html(body, page.final_url, retailer_id, record["retrieved_at"]))
    except FetchError as error:
        record.update(http_status=error.http_status, error=error.code, attempts=error.attempts)
    except (ValueError, OSError) as error:
        record["error"] = type(error).__name__ + ": retailer fetch failed"
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return record, body


def response_record(response: SearchResponse) -> dict:
    return asdict(response)
