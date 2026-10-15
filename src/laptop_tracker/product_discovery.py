"""Ranking of live search results, product-page variant inspection, and collection manifests.

Ranking chooses which pages to inspect and collect. It never establishes configuration identity
and never fills a specification: titles, URLs, tags, and search summaries are discovery
evidence only. Identity is decided later by the retailer adapter, the extractor, and the
five-field matcher, exactly as for manually listed URLs. Expected target IDs written into the
manifest are hints that the collector checks, never assignments.

Variant pages: a product's variants are listed only from the page's own JSON-LD offers (their
``url`` values). No variant URL or ID is constructed. A variant is collected only when its own
option title states the target's RAM and storage and the retailer's adapter can bind a
``?variant=`` page; otherwise the variants are kept for review. The cheapest, default, or first
variant is never chosen on its own.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from .collection import ROUTES
from .conventions import cpu_series_forms, model_codes
from .discovery import _NOT_A_SUFFIX, _compact, normalize
from .normalization import CORE_MATCH_COLUMNS, core_value_key
from .offers import clean_text
from .retailer_search import SearchHit, SearchMethod, SearchRecord

# Candidate categories, in priority order.
CODE_MATCH = "CODE_MATCH"
CODE_WITH_SUFFIX = "CODE_MATCH_WITH_UNEXPLAINED_SUFFIX"
PART_NUMBER_MATCH = "VERIFIED_PART_NUMBER_MATCH"
CODE_SPELLING = "CODE_SPELLING_VARIANT"
CODE_URL_ONLY = "CODE_IN_URL_ONLY"
FAMILY_ONLY = "FAMILY_ONLY"
DIFFERENT_MODEL = "DIFFERENT_MODEL_CODE"
UNRELATED = "UNRELATED"
CATEGORY_PRIORITY = {
    CODE_MATCH: 0, CODE_WITH_SUFFIX: 1, PART_NUMBER_MATCH: 2, CODE_SPELLING: 3,
    CODE_URL_ONLY: 4, FAMILY_ONLY: 5, DIFFERENT_MODEL: 6, UNRELATED: 7,
}
# Categories whose page may be inspected and collected automatically (within the limits).
COLLECTABLE = frozenset({CODE_MATCH, CODE_WITH_SUFFIX, PART_NUMBER_MATCH, CODE_SPELLING,
                         CODE_URL_ONLY})

_BUNDLE = re.compile(r"\b(?:free|bundle|backpack|printer|monitor|bag|mouse|headset|chair)\b",
                     re.I)
_LAPTOP_WORDS = re.compile(r"\b(?:laptop|notebook|ultrabook|chromebook|2-in-1)\b", re.I)
_SCREEN = re.compile(r"(?<![0-9.])(1[0-8](?:\.[0-9])?)\s*(?:\"|”|″|''|-?\s*inch(?:es)?\b|in\b)",
                     re.I)
_CAPACITY = re.compile(r"(?<![0-9.])([0-9]{1,4})\s*(GB|TB)\b(?!/s|ps)", re.I)
# One unrecognized word may precede the RAM word ("32GB (2x16) MDDR5 RAM", a retailer typo).
_RAM_WORDS = re.compile(
    r"^\W{0,3}(?:[A-Za-z0-9]+\s+)?(?:(?:LP)?DDR[0-9]X?|RAM\b|memory|unified)", re.I
)
# Two capacities joined by "+" are two components (two drives), never one stated capacity.
_JOINED = re.compile(r"(?<![0-9.])([0-9]{1,4})\s*(GB|TB)\s*\+\s*([0-9]{1,4})\s*(GB|TB)\b", re.I)
# "SDD" is a retailer typo for SSD seen on a saved PC Central option title (2026-10-13).
_STORAGE_WORDS = re.compile(
    r"^\W{0,3}(?:(?:PCIe\s*(?:Gen\s*[0-9]\s*)?)?(?:NVMe\s*)?(?:M\.2\s*)?(?:SSD|SDD|HDD|eMMC|"
    r"storage|NVMe))", re.I,
)
_LAYOUT_GROUP = re.compile(r"^\s*\([^()]*\)")


@dataclass(frozen=True)
class SearchTarget:
    """A verified configuration to search for (CONFIGURATION targets only)."""

    target_id: str
    rank: str
    brand: str
    series: str
    model: str
    part_number: str
    core: Mapping[str, str]


def title_clues(text: str) -> dict[str, set[str]]:
    """Configuration clues stated in a title or option label. Discovery hints only."""
    clues: dict[str, set[str]] = {name: set() for name in CORE_MATCH_COLUMNS if name != "model"}
    plain = re.sub(r"[®™℠]", " ", text)
    for match in _SCREEN.finditer(plain):
        clues["screen_size_in"].add(format(float(match[1]), "g"))
    for joined in _JOINED.finditer(plain):
        after = plain[joined.end(): joined.end() + 40]
        name = "ram_capacity_gb" if _RAM_WORDS.match(after) else "storage_capacity_gb"
        clues[name].add(f"{joined[1]}{joined[2].upper()} + {joined[3]}{joined[4].upper()}")
    plain = _JOINED.sub(" ", plain)
    for match in _CAPACITY.finditer(plain):
        amount = int(match[1]) * (1000 if match[2].upper() == "TB" else 1)
        after = _LAYOUT_GROUP.sub("", plain[match.end(): match.end() + 40])  # "(2x8) DDR4 RAM"
        if _RAM_WORDS.match(after):
            clues["ram_capacity_gb"].add(str(amount))
        elif _STORAGE_WORDS.match(after):
            clues["storage_capacity_gb"].add(str(amount))
    clues["cpu_series"] = set(cpu_series_forms(plain))
    return clues


def _same(name: str, left: str, right: str) -> bool:
    try:
        return core_value_key(name, left) == core_value_key(name, right)
    except ValueError:
        return False


def clue_comparison(clues: Mapping[str, set[str]], target: SearchTarget) -> dict[str, str]:
    """``AGREES`` / ``DIFFERS (...)`` / ``NOT_STATED`` per field, from discovery clues only."""
    result: dict[str, str] = {}
    for name, values in clues.items():
        wanted = target.core[name]
        if not values:
            result[name] = "NOT_STATED"
        elif any(_same(name, value, wanted) for value in values) and len(values) == 1:
            result[name] = "AGREES"
        elif any(_same(name, value, wanted) for value in values):
            result[name] = f"MIXED ({'; '.join(sorted(values))})"
        else:
            result[name] = f"DIFFERS ({'; '.join(sorted(values))})"
    return result


def _suffix(title: str, code: str) -> str:
    """An unexplained designation directly after ``code`` in the same title segment."""
    for segment in re.split(r"[|/,()\[\]]", title):
        words = segment.split()
        upper = [word.upper() for word in words]
        if code.upper() in upper:
            index = upper.index(code.upper())
            if index + 1 < len(words):
                word = words[index + 1]
                if re.fullmatch(r"[A-Za-z]{2,5}", word) and word.upper() not in _NOT_A_SUFFIX:
                    return word
    return ""


def _spelled(title: str, compact_code: str) -> str:
    """A spacing/punctuation spelling of the code in the title (``ANV15 42 R2UH``)."""
    words = re.sub(r"[|/,()\[\]]", " ", title).split()
    for start in range(len(words)):
        for end in range(start + 1, min(len(words), start + 5) + 1):
            token = " ".join(words[start:end])
            if _compact(token) == compact_code:
                return token
    return ""


@dataclass
class RankedCandidate:
    target: SearchTarget
    hit: SearchHit
    records: list[SearchRecord]
    category: str
    code_evidence: str
    suffix: str
    family_context: str
    clues: dict[str, str]
    flags: list[str]
    reasons: list[str]
    rank: int = 0
    decision: str = ""
    decision_reason: str = ""

    @property
    def sort_key(self) -> tuple:
        agree = sum(value == "AGREES" for value in self.clues.values())
        differ = sum(value.startswith("DIFFERS") for value in self.clues.values())
        first = min((record.page_number, hit_position(record, self.hit.product_url))
                    for record in self.records)
        tier = min(record.query.tier for record in self.records)
        bundle = "BUNDLE_WORDING_IN_TITLE" in self.flags
        return (CATEGORY_PRIORITY[self.category], differ, bundle, -agree, tier, first,
                self.hit.product_url)


def hit_position(record: SearchRecord, product_url: str) -> int:
    for hit in record.hit_items:
        if hit.product_url == product_url:
            return hit.position
    return 999


def rank_hit(target: SearchTarget, hit: SearchHit, records: list[SearchRecord]) -> RankedCandidate:
    """Classify and explain one search result for one target (no identity decision)."""
    code = target.model.strip()
    compact_code = _compact(code)
    title = hit.title
    title_codes = model_codes(title)
    handle = urlsplit(hit.product_url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    reasons: list[str] = []
    suffix = ""
    evidence = ""
    if any(_compact(token) == compact_code for token in title_codes):
        token = next(t for t in title_codes if _compact(t) == compact_code)
        suffix = _suffix(title, token)
        category = CODE_WITH_SUFFIX if suffix else CODE_MATCH
        evidence = f"title token {token}"
        reasons.append(f"Title shows the complete model code {token}"
                       + (f" followed by '{suffix}' (meaning unknown; kept, not stripped)"
                          if suffix else "") + ".")
    elif target.part_number and normalize(target.part_number) in normalize(
        f"{title} {hit.summary}"
    ):
        category = PART_NUMBER_MATCH
        evidence = f"verified part number {target.part_number}"
        reasons.append("Title or search summary shows the verified manufacturer part number.")
    elif compact_code and (spelled := _spelled(title, compact_code)):
        category = CODE_SPELLING
        evidence = f"title spelling '{spelled}'"
        reasons.append(f"Title shows the code with different spacing/punctuation: '{spelled}'.")
    elif code and re.search(rf"(?:^|-){re.escape(code.lower())}(?:-|$)", handle):
        category = CODE_URL_ONLY
        evidence = "product URL handle"
        reasons.append("Only the product URL handle shows the model code.")
    else:
        category = ""
    words = set(normalize(f"{title} {hit.vendor}").replace("|", " ").split())
    brand_ok = bool(target.brand) and normalize(target.brand) in words
    series_ok = bool(target.series) and all(
        part in words for part in normalize(target.series).split()
    )
    family = ("brand+series" if brand_ok and series_ok else "brand" if brand_ok
              else "series" if series_ok else "none")
    prefix = code.split("-", 1)[0].casefold()
    same_prefix = [t for t in title_codes if t.split("-", 1)[0].casefold() == prefix]
    if not category:
        if title_codes and (brand_ok or series_ok or same_prefix):
            category = DIFFERENT_MODEL
            reasons.append(f"Title shows a different model code ({', '.join(title_codes)})"
                           + (" sharing the code prefix" if same_prefix else "") + ".")
        elif brand_ok and series_ok:
            category = FAMILY_ONLY
            reasons.append("Brand and series match, but no model code is shown.")
        else:
            category = UNRELATED
            reasons.append("Neither the model code nor the brand and series appear.")
    # Clues come from the title only: retailer tags carry ranges ("13\" To 14.9\"").
    clues = clue_comparison(title_clues(title), target)
    for name, value in clues.items():
        if value != "NOT_STATED":
            reasons.append(f"{name} clue {value.lower()} (discovery only).")
    flags: list[str] = []
    if _BUNDLE.search(title):
        flags.append("BUNDLE_WORDING_IN_TITLE")
    if not _LAPTOP_WORDS.search(f"{title} {hit.product_type}") and category == UNRELATED:
        flags.append("NOT_OBVIOUSLY_A_LAPTOP")
    if hit.available == "FALSE":
        flags.append("SEARCH_SAYS_UNAVAILABLE")
    return RankedCandidate(target, hit, records, category, evidence, suffix, family, clues,
                           flags, reasons)


def rank_target_hits(
    target: SearchTarget, retailer_id: str, records: Sequence[SearchRecord]
) -> list[RankedCandidate]:
    """All distinct results for one target at one retailer, best first."""
    from .retailer_search import unique_hits

    own = [r for r in records
           if r.query.target_id == target.target_id and r.query.retailer_id == retailer_id]
    ranked = [rank_hit(target, hit, recs) for hit, recs in unique_hits(own)]
    ranked.sort(key=lambda c: c.sort_key)
    for index, candidate in enumerate(ranked, start=1):
        candidate.rank = index
    return ranked


def has_code_signal(target: SearchTarget, records: Sequence[SearchRecord]) -> bool:
    """True when any result so far shows the model code or verified part number."""
    from .retailer_search import unique_hits

    return any(
        rank_hit(target, hit, recs).category in {CODE_MATCH, CODE_WITH_SUFFIX, PART_NUMBER_MATCH,
                                                 CODE_SPELLING}
        for hit, recs in unique_hits(records)
    )


# Product-page variant inspection (Shopify retailers)


@dataclass(frozen=True)
class VariantInfo:
    variant_id: str
    url: str  # Exactly as the page's JSON-LD offer gives it.
    title: str
    available: str
    price: str


@dataclass
class ProductInspection:
    product_url: str
    outcome: str  # SINGLE_VARIANT, MULTI_VARIANT, NO_STRUCTURED_OFFERS, UNREADABLE
    variants: list[VariantInfo] = field(default_factory=list)
    message: str = ""


def _jsonld_offers(text: str) -> list[dict]:
    offers: list[dict] = []
    for block in re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', text, re.S | re.I
    ):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in list(nodes):
            if isinstance(node, dict) and isinstance(node.get("@graph"), list):
                nodes.extend(node["@graph"])
        for node in nodes:
            if not isinstance(node, dict):
                continue
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types:
                continue
            found = node.get("offers")
            found = found if isinstance(found, list) else [found] if found else []
            offers += [offer for offer in found if isinstance(offer, dict)]
    return offers


def _variant_titles(text: str) -> dict[str, str]:
    """Variant ID → option title from the page's variant JSON or analytics object."""
    titles: dict[str, str] = {}
    for block in re.findall(
        r'<script[^>]+type="application/json"[^>]*>(.*?)</script>', text, re.S | re.I
    ):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        rows = data if isinstance(data, list) else data.get("variants") if isinstance(
            data, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("id") is not None and isinstance(
                row.get("title"), str
            ):
                titles.setdefault(str(row["id"]), clean_text(row["title"]))
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\bvar meta = ", text):
        try:
            data, _ = decoder.raw_decode(text, match.end())
        except ValueError:
            continue
        product = data.get("product") if isinstance(data, dict) else None
        for row in (product or {}).get("variants", []) if isinstance(product, dict) else []:
            if isinstance(row, dict) and row.get("id") is not None:
                title = row.get("public_title") or row.get("name") or ""
                if isinstance(title, str) and title:
                    titles.setdefault(str(row["id"]), clean_text(title))
    return titles


def inspect_product_page(method: SearchMethod, product_url: str, text: str) -> ProductInspection:
    """Variants offered by a product page, from its own structured data only."""
    offers = _jsonld_offers(text)
    titles = _variant_titles(text)
    route = ROUTES[method.retailer_id]
    variants: list[VariantInfo] = []
    for offer in offers:
        url = offer.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        variant_id = (parse_qs(urlsplit(url).query).get("variant") or [""])[0]
        try:
            accepted = route.product_request(url).url == url
        except ValueError:
            accepted = False
        if not accepted or urlsplit(url).path.rstrip("/") != urlsplit(
            product_url
        ).path.rstrip("/"):
            continue
        availability = str(offer.get("availability", "")).rstrip("/").rsplit("/", 1)[-1]
        variants.append(VariantInfo(variant_id, url, titles.get(variant_id, ""), availability,
                                    str(offer.get("price", ""))))
    unique = {v.variant_id or v.url: v for v in variants}
    variants = list(unique.values())
    # Some pages repeat the same Product JSON-LD (PC Express: head and body blocks), so offers
    # are counted once per distinct offer URL (or once when they carry no URL at all).
    distinct = {str(offer.get("url", "")).strip() for offer in offers}
    if not offers:
        return ProductInspection(product_url, "NO_STRUCTURED_OFFERS",
                                 message="No JSON-LD Product offers on the page.")
    if len(distinct) == 1:
        return ProductInspection(product_url, "SINGLE_VARIANT", variants)
    if len(variants) < len(distinct):
        return ProductInspection(product_url, "UNREADABLE", variants,
                                 "Some JSON-LD offers have no usable variant URL.")
    return ProductInspection(product_url, "MULTI_VARIANT", variants)


def variant_decision(variant: VariantInfo, target: SearchTarget) -> tuple[str, str]:
    """Collect only a variant whose own option title states the target RAM and storage."""
    clues = clue_comparison(title_clues(variant.title), target) if variant.title else {}
    ram = clues.get("ram_capacity_gb", "NOT_STATED")
    storage = clues.get("storage_capacity_gb", "NOT_STATED")
    if not variant.title:
        return "REVIEW", "Variant has no option title on the page."
    if ram.startswith("DIFFERS") or storage.startswith("DIFFERS"):
        return "ALTERNATIVE", f"Different configuration: RAM {ram}, storage {storage}."
    if ram == "AGREES" and storage == "AGREES":
        return "COLLECT", "Option title states the target RAM and storage."
    if ram == "AGREES" and storage == "NOT_STATED":
        return "COLLECT", "Option title states the target RAM; storage is not an option."
    if storage == "AGREES" and ram == "NOT_STATED":
        return "COLLECT", "Option title states the target storage; RAM is not an option."
    return "REVIEW", f"Option title does not identify the configuration (RAM {ram}, storage " \
                     f"{storage})."


# Manifest and coverage


CANDIDATE_COLUMNS: tuple[str, ...] = (
    "target_id", "rank", "retailer_id", "candidate_rank", "category", "product_url",
    "title", "code_evidence", "retained_suffix", "family_context",
    *(f"clue_{name}" for name in CORE_MATCH_COLUMNS if name != "model"),
    "flags", "queries", "search_positions", "vendor", "product_type", "search_available",
    "search_price", "decision", "decision_reason", "reasons",
)


def candidate_row(candidate: RankedCandidate) -> dict[str, str]:
    hit = candidate.hit
    row = {
        "target_id": candidate.target.target_id,
        "rank": candidate.target.rank,
        "retailer_id": hit.retailer_id,
        "candidate_rank": str(candidate.rank),
        "category": candidate.category,
        "product_url": hit.product_url,
        "title": hit.title,
        "code_evidence": candidate.code_evidence,
        "retained_suffix": candidate.suffix,
        "family_context": candidate.family_context,
        **{f"clue_{name}": value for name, value in candidate.clues.items()},
        "flags": ";".join(candidate.flags),
        "queries": "; ".join(dict.fromkeys(f"{r.query.tier}: {r.query.text}"
                                           for r in candidate.records)),
        "search_positions": "; ".join(
            f"p{r.page_number}#{hit_position(r, hit.product_url)}" for r in candidate.records
        ),
        "vendor": hit.vendor,
        "product_type": hit.product_type,
        "search_available": hit.available,
        "search_price": hit.price_text,
        "decision": candidate.decision,
        "decision_reason": candidate.decision_reason,
        "reasons": " ".join(candidate.reasons),
    }
    return {column: row.get(column, "") for column in CANDIDATE_COLUMNS}


def strip_variant(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")


COVERAGE_COLUMNS: tuple[str, ...] = (
    "retailer_id", "expected_target_id", "baseline_url", "found_by_search",
    "discovered_product_url", "candidate_category", "candidate_decision",
    "in_new_manifest", "new_manifest_url", "note",
)
