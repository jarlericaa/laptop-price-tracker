"""Catalog leads from retailer sitemaps (2026-10-22): product URLs the store search misses.

Retailer search is capped (Shopify predictive search returns at most 10 results, unpaginated) and
Laptop Factory's search omitted out-of-stock products and returned nothing for some complete
hyphenated codes. Every store's robots.txt (saved 2026-10-13) advertises a sitemap and does not
disallow it: ``wp-sitemap.xml`` (WordPress core) for Laptop Factory, ``sitemap.xml`` (Shopify)
for the four others. Sitemaps list product pages whether or not they are in stock.

This module only parses: robots rules, sitemap XML, and which listed product URLs show a scoped
target's complete model code (in the URL handle, or in a Shopify sitemap's image title). The
caller fetches through the shared ``Fetcher`` within explicit limits, after checking robots.txt.
A lead is a URL to check, written in the supplied-URL format so that ``discover_products.py``
re-fetches and re-matches it like any candidate. It is never a match, an alias or a price.
Family-name targets get no leads: a family name in a URL cannot establish one product.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from .adapters import datablitz, laptop_factory, oct, pcc, pcx
from .conventions import model_codes
from .discovery import _compact
from .offers import clean_text
from .product_discovery import url_shows_code
from .retailer_search import SEARCH_METHODS


@dataclass(frozen=True)
class CatalogSource:
    retailer_id: str
    root_sitemap: str  # As advertised by the retailer's robots.txt.
    product_sitemap: str  # Regular expression for a product sitemap URL listed by the root.
    kind: str  # "wordpress_core" or "shopify"


CATALOG_SOURCES: dict[str, CatalogSource] = {
    source.retailer_id: source for source in (
        CatalogSource(laptop_factory.RETAILER_ID, f"https://{laptop_factory.HOST}/wp-sitemap.xml",
                      r"/wp-sitemap-posts-product-\d+\.xml$", "wordpress_core"),
        *(CatalogSource(module.RETAILER_ID, f"https://{module.HOST}/sitemap.xml",
                        r"/sitemap_products_\d+\.xml(?:\?.*)?$", "shopify")
          for module in (datablitz, pcx, pcc, oct)),
    )
}

# Robots.txt (Google semantics: the longest matching rule wins, Allow wins a tie, ``*`` matches
# any characters and a trailing ``$`` anchors the end).


@dataclass(frozen=True)
class RobotsRules:
    rules: tuple[tuple[bool, str], ...]  # (allow, pattern) for the applicable group
    sitemaps: tuple[str, ...]


def parse_robots(text: str, user_agent: str = "") -> RobotsRules:
    """Rules of the most specific group naming ``user_agent`` (a product token), else ``*``."""
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    sitemaps: list[str] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        name = name.lower()
        if name == "sitemap":
            sitemaps.append(value)
        elif name == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif name in {"allow", "disallow"} and agents:
            if value:
                rules.append((name == "allow", value))
    if agents:
        groups.append((agents, rules))
    token = user_agent.lower()
    named = [r for a, r in groups if token and any(t != "*" and t in token for t in a)]
    chosen = named or [r for a, r in groups if "*" in a]
    return RobotsRules(tuple(rule for group in chosen for rule in group), tuple(sitemaps))


def _pattern(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = re.escape(pattern[:-1] if anchored else pattern).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


def robots_allows(rules: RobotsRules, url: str) -> bool:
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    best: tuple[int, bool] | None = None
    for allow, pattern in rules.rules:
        if _pattern(pattern).match(path):
            candidate = (len(pattern), allow)
            if best is None or candidate[0] > best[0] or (
                    candidate[0] == best[0] and allow and not best[1]):
                best = candidate
    return True if best is None else best[1]


# Sitemaps

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
       "image": "http://www.google.com/schemas/sitemap-image/1.1"}


class SitemapError(ValueError):
    """The response is not a sitemap index or URL set."""


@dataclass(frozen=True)
class SitemapEntry:
    loc: str
    lastmod: str = ""
    title: str = ""  # Shopify product sitemaps carry the product title as image:title.


@dataclass(frozen=True)
class Sitemap:
    kind: str  # "index" or "urlset"
    entries: tuple[SitemapEntry, ...]


def parse_sitemap(body: bytes) -> Sitemap:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise SitemapError("The response is not well-formed XML.") from error
    tag = root.tag.rsplit("}", 1)[-1]
    if tag not in {"sitemapindex", "urlset"}:
        raise SitemapError(f"Unexpected sitemap root <{tag}>.")
    child = "sitemap" if tag == "sitemapindex" else "url"
    entries = []
    for node in root.findall(f"sm:{child}", _NS):
        loc = (node.findtext("sm:loc", "", _NS) or "").strip()
        if not loc:
            continue
        title = node.findtext("image:image/image:title", "", _NS) or ""
        entries.append(SitemapEntry(loc, (node.findtext("sm:lastmod", "", _NS) or "").strip(),
                                    clean_text(title)))
    return Sitemap("index" if tag == "sitemapindex" else "urlset", tuple(entries))


def product_sitemaps(source: CatalogSource, index: Sitemap) -> list[str]:
    """Product sitemap URLs a root index lists, on the retailer's own host, in order."""
    host = urlsplit(source.root_sitemap).netloc
    return [entry.loc for entry in index.entries
            if urlsplit(entry.loc).netloc == host and re.search(source.product_sitemap,
                                                                entry.loc)]


def product_entries(source: CatalogSource, urlset: Sitemap) -> list[SitemapEntry]:
    """Entries whose ``loc`` is a product page the retailer's adapter path accepts."""
    method = SEARCH_METHODS[source.retailer_id]
    kept = []
    for entry in urlset.entries:
        canonical = method.canonical_product_url(entry.loc, source.root_sitemap)
        if canonical:
            kept.append(SitemapEntry(entry.loc, entry.lastmod, entry.title))
    return kept


# Leads


@dataclass(frozen=True)
class LeadTarget:
    target_id: str
    model: str  # A complete manufacturer model code (MODEL_CODE targets only).


LEAD_COLUMNS: tuple[str, ...] = (
    "supplied_id", "retailer_id", "url", "search_target_id", "supplied_by", "supplied_on",
    "notes",
)


def lead_basis(entry: SitemapEntry, target: LeadTarget) -> str:
    """``URL_HANDLE_SHOWS_MODEL_CODE``, ``SITEMAP_TITLE_SHOWS_MODEL_CODE`` or ``""``."""
    if url_shows_code(entry.loc, target.model):
        return "URL_HANDLE_SHOWS_MODEL_CODE"
    wanted = _compact(target.model)
    if entry.title and any(_compact(code) == wanted for code in model_codes(entry.title)):
        return "SITEMAP_TITLE_SHOWS_MODEL_CODE"
    return ""


def catalog_leads(
    retailer_id: str,
    entries: Iterable[tuple[str, SitemapEntry]],
    targets: Sequence[LeadTarget],
    *,
    supplied_on: str,
) -> list[dict[str, str]]:
    """Supplied-URL rows for product URLs that show a target's complete code.

    ``entries`` are ``(product sitemap URL, entry)``. A URL whose handle shows the code is left
    without ``search_target_id``: discovery assigns it by the same URL rule to every scoped target
    whose code it shows. A URL found only through its sitemap title names its target.
    """
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for sitemap, entry in entries:
        for target in targets:
            basis = lead_basis(entry, target)
            key = (entry.loc, "" if basis.startswith("URL") else target.target_id)
            if not basis or key in seen:
                continue
            seen.add(key)
            rows.append({
                "supplied_id": f"CAT-{retailer_id}-{len(rows) + 1:04d}",
                "retailer_id": retailer_id,
                "url": entry.loc,
                "search_target_id": "" if basis.startswith("URL") else target.target_id,
                "supplied_by": "catalog_sitemap",
                "supplied_on": supplied_on,
                "notes": "; ".join(filter(None, (
                    f"{basis} ({target.target_id} {target.model})", f"sitemap {sitemap}",
                    f"lastmod {entry.lastmod}" if entry.lastmod else "",
                    f"title {entry.title}" if entry.title else "",
                    "a lead to re-check, never a match"))),
            })
    return rows


def lead_targets(rows: Iterable[Mapping[str, str]], kinds: Mapping[str, str]) -> list[LeadTarget]:
    """Scoped targets whose catalog model is one complete code (``MODEL_CODE``)."""
    return [LeadTarget(row["target_id"], row["model"].strip()) for row in rows
            if kinds.get(row["target_id"]) == "MODEL_CODE" and row.get("model", "").strip()]
