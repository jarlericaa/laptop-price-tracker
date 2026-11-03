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
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from .alternatives import MAX_DIFFERENCES
from .collection import ROUTES
from .conventions import (
    canonical_gpu_models,
    cpu_series_forms,
    is_model_code,
    is_part_number_shape,
    model_codes,
    product_name_tokens,
)
from .discovery import _NOT_A_SUFFIX, _compact, normalize
from .matching import verified_secondary_difference
from .normalization import CORE_MATCH_COLUMNS, core_value_key, is_blank
from .offers import clean_text
from .product_families import default_family_registry
from .retailer_search import SearchHit, SearchMethod, SearchRecord
from .variant_axes import (
    PageStatements,
    VariantAxes,
    VariantReading,
    known_colors,
    option_names_from_html,
    read_axes,
    statements_from_html,
    variant_rows_from_html,
)

# Candidate categories, in priority order. A historical verified URL is a page that matched this
# configuration exactly in an earlier collection; it is a seed to re-check, never a match.
HISTORICAL_VERIFIED_URL = "HISTORICAL_VERIFIED_URL"
# A known product page supplied by a person (config/supplied_candidate_urls.csv, 2026-10-19): a
# discovery seed checked like any candidate, never an approved match or a target assignment.
SUPPLIED_URL = "SUPPLIED_URL"
CODE_MATCH = "CODE_MATCH"
CODE_WITH_SUFFIX = "CODE_MATCH_WITH_UNEXPLAINED_SUFFIX"
PART_NUMBER_MATCH = "VERIFIED_PART_NUMBER_MATCH"
CODE_SPELLING = "CODE_SPELLING_VARIANT"
CODE_URL_ONLY = "CODE_IN_URL_ONLY"
FAMILY_ONLY = "FAMILY_ONLY"
DIFFERENT_MODEL = "DIFFERENT_MODEL_CODE"
UNRELATED = "UNRELATED"
# Name-identified targets (2026-10-19, second review). A catalog model that is a marketed product
# name (``MacBook Neo``) is found by that name; a broad family name (``IdeaPad``) cannot establish
# one product, so results showing it stay review leads until the catalog is reconciled.
NAME_MATCH = "PRODUCT_NAME_MATCH"
FAMILY_NAME_LEAD = "FAMILY_NAME_LEAD_CATALOG_UNRESOLVED"
# A listing that states a broad-family target's brand and family name (2026-10-20): collected
# within the limits so its own page decides a listed-spec match (listed_spec.py). Never an exact
# product match. Without the brand it stays a review lead (FAMILY_NAME_LEAD).
FAMILY_SPEC_CANDIDATE = "FAMILY_LISTED_SPEC_CANDIDATE"
# Not a laptop: a desktop, all-in-one, mini PC, handheld console, router, monitor or standalone
# component. Rejected before any page request (2026-10-20).
NOT_A_LAPTOP = "NOT_A_LAPTOP"
# A bag, sleeve, charger, ... whose title names a laptop only for compatibility. Rejected before
# any product-page request; never laptop identity.
ACCESSORY = "ACCESSORY"
CATEGORY_PRIORITY = {
    HISTORICAL_VERIFIED_URL: -1, SUPPLIED_URL: -1,
    CODE_MATCH: 0, CODE_WITH_SUFFIX: 1, PART_NUMBER_MATCH: 2, CODE_SPELLING: 3, NAME_MATCH: 3,
    FAMILY_SPEC_CANDIDATE: 3, CODE_URL_ONLY: 4, FAMILY_NAME_LEAD: 5, FAMILY_ONLY: 5,
    DIFFERENT_MODEL: 6, UNRELATED: 7, ACCESSORY: 8, NOT_A_LAPTOP: 8,
}
# Categories whose page may be inspected and collected automatically (within the limits).
COLLECTABLE = frozenset({HISTORICAL_VERIFIED_URL, SUPPLIED_URL, CODE_MATCH, CODE_WITH_SUFFIX,
                         PART_NUMBER_MATCH, CODE_SPELLING, CODE_URL_ONLY, NAME_MATCH,
                         FAMILY_SPEC_CANDIDATE})
# Categories that, without contrary title clues, make a result plausible enough to stop the
# fallback searches. An unexplained suffix or a code only in the URL is unresolved: searching
# continues (2026-10-19).
PLAUSIBLE = frozenset({CODE_MATCH, PART_NUMBER_MATCH, CODE_SPELLING, NAME_MATCH})
# Kinds of catalog model value (identity of the search target).
MODEL_CODE_KIND = "MODEL_CODE"
PRODUCT_NAME_KIND = "PRODUCT_NAME"
FAMILY_NAME_KIND = "FAMILY_NAME"
_CONFIGURATION_CLUES = ("screen_size_in", "cpu_series", "ram_capacity_gb", "storage_capacity_gb")

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
    """A verified configuration to search for (CONFIGURATION targets only).

    ``identity_kind`` says what the catalog ``model`` is: one manufacturer model code, a
    marketed product name, or a broad family name (from the catalog reconciliation). Blank
    means: a code when the value is exactly one code, otherwise an unreviewed name, treated as a
    family name (review leads only) so that no family name is ever searched as if it were one
    product.
    """

    target_id: str
    rank: str
    brand: str
    series: str
    model: str
    part_number: str
    core: Mapping[str, str]
    identity_kind: str = ""
    # A manufacturer code the catalog records for a family-name target (2026-10-20): ranks
    # listings stating it first; a listing stating another code is not collected for it.
    recorded_code: str = ""
    # The target's catalog row (brand, recorded codes, secondary fields): the listed-spec check
    # of collected pages. Empty in older callers; never used to fill an offer's fields.
    record: Mapping[str, object] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        if self.identity_kind:
            return self.identity_kind
        return MODEL_CODE_KIND if is_model_code(self.model) else FAMILY_NAME_KIND


def title_clues(text: str) -> dict[str, set[str]]:
    """Configuration clues stated in a title or option label. Discovery hints only."""
    clues: dict[str, set[str]] = {name: set() for name in CORE_MATCH_COLUMNS if name != "model"}
    plain = re.sub(r"[®™℠]", " ", text)
    for match in _SCREEN.finditer(plain):
        clues["screen_size_in"].add(format(float(match[1]), "g"))
    for joined in _JOINED.finditer(plain):
        after = plain[joined.end(): joined.end() + 40]
        first = int(joined[1]) * (1000 if joined[2].upper() == "TB" else 1)
        second = int(joined[3]) * (1000 if joined[4].upper() == "TB" else 1)
        if 0 < first <= 128 <= second and not _RAM_WORDS.match(after):
            # The joined RAM+storage form ("8GB+256GB"), read as in extraction (2026-10-19).
            clues["ram_capacity_gb"].add(str(first))
            clues["storage_capacity_gb"].add(str(second))
            continue
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
    # An earlier verified offer URL for this product (may name a ?variant=), when known.
    seed_url: str = ""
    # Lineage (2026-10-19): the discovery round that listed the candidate, and the collection
    # request that fetched it (an earlier request when the same page was already collected).
    discovery_round: int = 1
    collection_url: str = ""
    collection_request_id: str = ""

    @property
    def sort_key(self) -> tuple:
        agree = sum(value == "AGREES" for value in self.clues.values())
        differ = sum(value.startswith("DIFFERS") for value in self.clues.values())
        first = min((record.page_number, hit_position(record, self.hit.product_url))
                    for record in self.records) if self.records else (0, 0)
        tier = min(record.query.tier for record in self.records) if self.records else "0"
        bundle = "BUNDLE_WORDING_IN_TITLE" in self.flags
        own_code = self.category == FAMILY_SPEC_CANDIDATE and "recorded code" in (
            self.code_evidence)
        # Matching configurations first, then one-difference and two-difference close
        # alternatives, then relevant candidates with incomplete title evidence; pages that are
        # not relevant keep their earlier relative order after them (2026-10-27).
        return (title_alternative_potential(self).sort_tier, CATEGORY_PRIORITY[self.category],
                not own_code, differ, bundle, -agree, tier, first, self.hit.product_url)


# Accessory products (2026-10-19, second review). The product noun of an accessory listing comes
# first ("Tomtoc Sleeve for MacBook Air 13"); a laptop listing names the laptop first and may
# list a bundled bag later ("... Laptop | ASUS VP4700 Backpack").
_ACCESSORY_NOUN = re.compile(
    r"\b(?:sleeves?|bags?|handbags?|briefcases?|pouch(?:es)?|totes?|backpacks?|cases?|covers?|"
    r"skins?|protectors?|films?|shells?|chargers?|adapters?|power\s+suppl(?:y|ies)|stands?|"
    r"docks?|docking\s+stations?|hubs?|mouse|mice|keyboards?|cooling\s+pads?|cables?|"
    r"batter(?:y|ies)|monitors?|headsets?|webcams?)\b",
    re.I,
)
_COMPATIBILITY = re.compile(r"\b(?:for|compatible\s+(?:with|for)|fits?|designed\s+for)\b", re.I)
_LAPTOP_NOUN = re.compile(r"\b(?:laptops?|notebooks?|ultrabooks?|chromebooks?)\b", re.I)
_ACCESSORY_TYPE = re.compile(
    r"\b(?:bags?|sleeves?|cases?|accessor(?:y|ies)|peripherals?|chargers?|adapters?|mouse|"
    r"keyboards?|monitors?|stands?)\b", re.I,
)


def accessory_reason(title: str, product_type: str = "") -> str:
    """Why a search result is an accessory rather than a laptop, or ''.

    The title's leading item (before ``|``, `` + ``, or a parenthesis) names an accessory and
    states no processor, RAM, or storage of its own, and either uses compatibility wording after
    the accessory noun ("sleeve for MacBook Air") or names no laptop before it. A retailer
    product type naming an accessory category is also enough when the title states no laptop
    specification. Compatibility wording never establishes laptop identity.
    """
    head = re.split(r"\s\|\s|\s\+\s|\(|\|", title, maxsplit=1)[0]
    clues = title_clues(head)
    specified = bool(clues["cpu_series"] or clues["ram_capacity_gb"]
                     or clues["storage_capacity_gb"])
    noun = _ACCESSORY_NOUN.search(head)
    if noun and not specified:
        before, after = head[: noun.start()], head[noun.end():]
        if _COMPATIBILITY.match(after.lstrip()) or _COMPATIBILITY.search(after[:40]):
            return f"accessory '{noun[0]}' with compatibility wording"
        if not _LAPTOP_NOUN.search(before):
            return f"accessory '{noun[0]}' named before any laptop"
    if product_type and _ACCESSORY_TYPE.search(product_type) and not _LAPTOP_NOUN.search(
        product_type
    ) and not specified:
        return f"retailer product type '{product_type}'"
    return ""


# Products that are not laptops (2026-10-20): matched on the listing's leading item or its
# retailer product type, only when the title does not call the product a laptop or notebook.
_NOT_LAPTOP = re.compile(
    r"\b(?:desktop|all[\s-]?in[\s-]?one|AIO|mini\s?PC|tower\s+PC|gaming\s+PC|"
    r"handheld|ROG\s+Ally|Legion\s+Go|Steam\s+Deck|MSI\s+Claw|router|mesh\s+wi-?fi|"
    r"access\s+point|monitor|graphics\s+card|motherboard|power\s+supply|"
    r"(?:internal|external|portable)\s+(?:SSD|HDD|hard\s+drive)|memory\s+module|"
    r"tablet|smartphone|printer|projector)\b",
    re.I,
)


def non_laptop_reason(title: str, product_type: str = "") -> str:
    """Why a search result is not a laptop (desktop, handheld, router, component, ...), or ''."""
    head = re.split(r"\s\|\s|\s\+\s|\(|\|", title, maxsplit=1)[0]
    if _LAPTOP_WORDS.search(f"{head} {product_type}"):
        return ""
    found = _NOT_LAPTOP.search(head) or _NOT_LAPTOP.search(product_type or "")
    return f"'{found[0]}'" if found else ""


def name_in_title(title: str, name: str) -> str:
    """The catalog product name as it appears in ``title`` (whole words, in order; case,
    spacing and punctuation between words are presentation only), or ''."""
    wanted = product_name_tokens(name)
    if not wanted:
        return ""
    words = product_name_tokens(title)
    for start in range(len(words) - len(wanted) + 1):
        if words[start:start + len(wanted)] == wanted:
            return " ".join(wanted)
    return ""


def hit_position(record: SearchRecord, product_url: str) -> int:
    for hit in record.hit_items:
        if hit.product_url == product_url:
            return hit.position
    return 999


def rank_hit(target: SearchTarget, hit: SearchHit, records: list[SearchRecord]) -> RankedCandidate:
    """Classify and explain one search result for one target (no identity decision)."""
    code = target.model.strip()
    kind = target.kind
    # Code rules (exact token, spelling variant, URL handle) apply only to a code target: a
    # family or product name is never treated as a code spelling (2026-10-19, second review).
    compact_code = _compact(code) if kind == MODEL_CODE_KIND else ""
    title = hit.title
    title_codes = model_codes(title)
    handle = urlsplit(hit.product_url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    reasons: list[str] = []
    suffix = ""
    evidence = ""
    accessory = accessory_reason(title, hit.product_type)
    not_laptop = non_laptop_reason(title, hit.product_type)
    named = name_in_title(title, code) if kind != MODEL_CODE_KIND else ""
    # A broad name that records one verified manufacturer code (TUF A16 -> FA607NUQ-RL011W,
    # 2026-10-22): a title, spelling or URL handle showing the complete recorded code is
    # evidence for it, whatever words surround the name. Another code stays rejected below.
    recorded = target.recorded_code.strip() if kind != MODEL_CODE_KIND else ""
    recorded_compact = _compact(recorded) if recorded else ""
    if not_laptop:
        category = NOT_A_LAPTOP
        evidence = not_laptop
        reasons.append(f"Not a laptop ({not_laptop}). Rejected before any page request.")
    elif accessory:
        category = ACCESSORY
        evidence = accessory
        reasons.append(f"Accessory listing ({accessory}); compatibility wording never "
                       "establishes laptop identity. Rejected before any page request.")
    elif compact_code and any(_compact(token) == compact_code for token in title_codes):
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
    elif compact_code and re.search(rf"(?:^|-){re.escape(code.lower())}(?:-|$)", handle):
        category = CODE_URL_ONLY
        evidence = "product URL handle"
        reasons.append("Only the product URL handle shows the model code.")
    elif recorded_compact and any(_compact(t) == recorded_compact for t in title_codes):
        token = next(t for t in title_codes if _compact(t) == recorded_compact)
        suffix = _suffix(title, token)
        category = CODE_WITH_SUFFIX if suffix else FAMILY_SPEC_CANDIDATE
        evidence = f"title shows this target's recorded code {token}"
        reasons.append(f"Title shows the complete recorded model code {token}"
                       + (f" followed by '{suffix}' (meaning unknown; kept, not stripped)"
                          if suffix else "")
                       + ". The page's own specifications decide the match.")
    elif recorded_compact and (spelled := _spelled(title, recorded_compact)):
        category = CODE_SPELLING
        evidence = f"title spelling '{spelled}' of the recorded code {recorded}"
        reasons.append("Title shows the recorded code with different spacing/punctuation: "
                       f"'{spelled}'.")
    elif recorded_compact and re.search(rf"(?:^|-){re.escape(recorded.lower())}(?:-|$)", handle):
        category = CODE_URL_ONLY
        evidence = "product URL handle shows the recorded code"
        reasons.append("Only the product URL handle shows the recorded model code.")
    elif named and kind == PRODUCT_NAME_KIND:
        category = NAME_MATCH
        evidence = f"title shows product name '{named}'"
        reasons.append(f"Title shows the catalog product name '{code}' (presentation only: case, "
                       "spacing, punctuation); the page's own evidence decides the match.")
    elif named:
        stated = [c for c in title_codes if not is_part_number_shape(c)]
        recorded = target.recorded_code
        own = recorded and any(c.casefold() == recorded.casefold() for c in stated)
        brand_stated = bool(target.brand) and name_in_title(
            f"{title} {hit.vendor}", target.brand) != ""
        if recorded and stated and not own:
            category = DIFFERENT_MODEL
            evidence = f"title code {', '.join(stated)}"
            reasons.append(f"Title shows {', '.join(stated)}; the target records {recorded}. A "
                           "recorded code is never bypassed by the listed-spec comparison.")
        elif brand_stated:
            category = FAMILY_SPEC_CANDIDATE
            evidence = (f"title shows this target's recorded code {recorded}" if own
                        else f"title shows brand and family '{named}'")
            reasons.append(f"Title shows the brand and the family '{code}'. The page's own "
                           "specifications decide a listed-spec comparison (never an exact "
                           "product match).")
        else:
            category = FAMILY_NAME_LEAD
            evidence = f"title shows family name '{named}'"
            reasons.append(f"Title shows '{code}' but not the brand {target.brand or '(none)'}: "
                           "kept for review, not fetched.")
    else:
        category = ""
    words = set(normalize(f"{title} {hit.vendor}").replace("|", " ").split())
    brand_ok = bool(target.brand) and normalize(target.brand) in words
    series_ok = bool(target.series) and all(
        part in words for part in normalize(target.series).split()
    )
    family = ("brand+series" if brand_ok and series_ok else "brand" if brand_ok
              else "series" if series_ok else "none")
    prefix = code.split("-", 1)[0].casefold() if kind == MODEL_CODE_KIND else "\0"
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


def configuration_difference(candidate: RankedCandidate) -> str:
    """The configuration fields a title clue states differently from the target, in words."""
    parts = []
    for name in _CONFIGURATION_CLUES:
        value = candidate.clues.get(name, "NOT_STATED")
        if value.startswith(("DIFFERS", "MIXED")):
            stated = value.split("(", 1)[-1].rstrip(")")
            parts.append(f"{name}: title {stated} vs target {candidate.target.core[name]}")
    return "; ".join(parts)


# Close-alternative potential of a search title (2026-10-27). A page whose *title* states one or
# two differences from the target is worth inspecting: the page's own evidence decides whether it
# is an alternative (alternatives.py). The title only prioritises and gates fetching; it never
# supplies a specification or a price. A field the title does not state is unknown, never
# agreement and never a difference.
TITLE_MATCH_TIER = "MATCH_CONFIGURATION"
TITLE_ALT_1_TIER = "ALTERNATIVE_1"
TITLE_ALT_2_TIER = "ALTERNATIVE_2"
TITLE_INCOMPLETE_TIER = "INCOMPLETE_TITLE"
TITLE_TOO_MANY_TIER = "TOO_MANY_DIFFERENCES"
_CLUE_DIMENSIONS = {"screen_size_in": "screen", "cpu_series": "cpu", "ram_capacity_gb": "ram",
                    "storage_capacity_gb": "storage"}
_DIMENSION_ORDER = ("model", "screen", "cpu", "ram", "storage", "gpu")
_SORT_TIERS = {TITLE_MATCH_TIER: 0, TITLE_ALT_1_TIER: 1, TITLE_ALT_2_TIER: 2,
               TITLE_INCOMPLETE_TIER: 3}
_SEED_CATEGORIES = frozenset({HISTORICAL_VERIFIED_URL, SUPPLIED_URL})


@dataclass(frozen=True)
class TitleAlternativePotential:
    """The established title differences of one search result, one per dimension."""

    differences: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    agrees: tuple[str, ...] = ()
    # False for a page that is not a laptop of this target's brand and family (no tier).
    relevant: bool = False
    seed: bool = False
    incomplete: bool = False

    @property
    def count(self) -> int:
        return len(self.differences)

    @property
    def over_budget(self) -> bool:
        return self.count > MAX_DIFFERENCES

    @property
    def model_difference(self) -> bool:
        return "model" in self.differences

    @property
    def tier(self) -> str:
        if not self.relevant:
            return ""
        if self.over_budget:
            return TITLE_TOO_MANY_TIER
        if self.count:
            return TITLE_ALT_1_TIER if self.count == 1 else TITLE_ALT_2_TIER
        return TITLE_INCOMPLETE_TIER if self.incomplete else TITLE_MATCH_TIER

    @property
    def sort_tier(self) -> int:
        if self.seed:
            return -1  # A page that matched before, or a person supplied, is re-checked first.
        return _SORT_TIERS.get(self.tier, 9)

    def describe(self) -> str:
        return ", ".join(self.differences)


def assess_title_alternative(
    clues: Mapping[str, str], *, category: str, title: str, vendor: str = "",
    brand: str = "", series: str = "", model: str = "", gpu_model: str = "",
) -> TitleAlternativePotential:
    """Count the established differences between a search title and a target.

    ``clues`` are the ``AGREES`` / ``DIFFERS (...)`` / ``MIXED (...)`` / ``NOT_STATED`` states
    of ``clue_comparison``. A GPU is a difference only when the title states one canonical GPU
    and the target records another. A model difference exists only for a ``DIFFERENT_MODEL_CODE``
    result of the target's own brand and recognised product family (``product_families``): the
    same brand with another family, or an unrelated brand, is never a close alternative.
    """
    differences, unknown, agrees = [], [], []
    for name, dimension in _CLUE_DIMENSIONS.items():
        state = clues.get(name, "NOT_STATED")
        if state.startswith(("DIFFERS", "MIXED")):
            differences.append(dimension)
        elif state == "AGREES":
            agrees.append(dimension)
        else:
            unknown.append(dimension)
    stated = canonical_gpu_models(title)
    if len(stated) == 1 and not is_blank(gpu_model):
        if verified_secondary_difference("gpu_model", next(iter(stated)), gpu_model):
            differences.append("gpu")
        else:
            agrees.append("gpu")
    else:
        unknown.append("gpu")
    family_model = False
    if category == DIFFERENT_MODEL and brand and name_in_title(f"{title} {vendor}", brand):
        registry = default_family_registry()
        target_family = registry.recognize(brand, series, model).family
        offer_family = registry.recognize(brand, title).family
        family_model = target_family is not None and offer_family is not None and (
            registry.same_family(target_family, offer_family))
        if family_model:
            differences.append("model")
    seed = category in _SEED_CATEGORIES
    relevant = seed or category in COLLECTABLE or family_model
    incomplete = category == FAMILY_SPEC_CANDIDATE and sum(
        value == "AGREES" for value in clues.values()) < 2

    def ordered(names: list[str]) -> tuple[str, ...]:
        return tuple(d for d in _DIMENSION_ORDER if d in names)

    return TitleAlternativePotential(
        ordered(differences), ordered(unknown), ordered(agrees), relevant, seed, incomplete)


def title_alternative_potential(candidate: RankedCandidate) -> TitleAlternativePotential:
    target = candidate.target
    return assess_title_alternative(
        candidate.clues, category=candidate.category, title=candidate.hit.title,
        vendor=candidate.hit.vendor, brand=target.brand, series=target.series,
        model=target.model, gpu_model=str(target.record.get("gpu_model", "") or ""))


def title_alternative_potential_from_row(
    row: Mapping[str, str], target: Mapping[str, object] | None = None
) -> TitleAlternativePotential:
    """The same assessment from a saved ``candidates.csv`` row (its ``clue_*`` columns, category
    and title) and the target's catalog row, so earlier runs can be reassessed offline."""
    target = target or {}
    clues = {name: row.get(f"clue_{name}", "") or "NOT_STATED" for name in _CLUE_DIMENSIONS}
    return assess_title_alternative(
        clues, category=row.get("category", ""), title=row.get("title", ""),
        vendor=row.get("vendor", ""), brand=str(target.get("brand", "") or ""),
        series=str(target.get("series", "") or ""), model=str(target.get("model", "") or ""),
        gpu_model=str(target.get("gpu_model", "") or ""))


def is_plausible(candidate: RankedCandidate) -> bool:
    """Shows the complete code (or verified part number) and no contrary configuration clue. A
    family listing needs at least two agreeing title clues as well (2026-10-20): an incomplete
    title is still collected, but does not stop the fallback searches."""
    if candidate.category == FAMILY_SPEC_CANDIDATE and "recorded code" in candidate.code_evidence:
        # The complete recorded code is stated: an incomplete title needs no further clues.
        return not configuration_difference(candidate)
    if candidate.category == FAMILY_SPEC_CANDIDATE:
        agree = sum(value == "AGREES" for value in candidate.clues.values())
        return agree >= 2 and not configuration_difference(candidate)
    return candidate.category in PLAUSIBLE and not configuration_difference(candidate)


def plausible_candidate(
    target: SearchTarget, records: Sequence[SearchRecord], exclude: frozenset[str] = frozenset()
) -> str:
    """The first plausible candidate's URL among the results so far, or ''.

    Results showing only another RAM/storage configuration, an unexplained suffix, a code only
    in the URL, or another model do not stop discovery: the fallback searches continue. Pages in
    ``exclude`` (``strip_variant`` form) were already collected and found unsuitable for the
    configuration, so they no longer stop a resumed search (2026-10-19).
    """
    from .retailer_search import unique_hits

    for hit, recs in unique_hits(records):
        if strip_variant(hit.product_url) in exclude:
            continue
        if is_plausible(rank_hit(target, hit, recs)):
            return hit.product_url
    return ""


def results_summary(target: SearchTarget, records: Sequence[SearchRecord]) -> str:
    """Why the results so far leave no plausible candidate, for the next query's run reason."""
    from .retailer_search import unique_hits

    hits = unique_hits(records)
    if not hits:
        return "no results so far"
    kinds = Counter()
    for hit, recs in hits:
        candidate = rank_hit(target, hit, recs)
        if candidate.category in PLAUSIBLE and configuration_difference(candidate):
            kinds["same code, other configuration"] += 1
        elif candidate.category in {CODE_WITH_SUFFIX, CODE_URL_ONLY}:
            kinds["unresolved code match"] += 1
        else:
            kinds[candidate.category.lower().replace("_", " ")] += 1
    return "results so far: " + ", ".join(f"{name} x{n}" for name, n in sorted(kinds.items()))


def seed_candidate(
    target: SearchTarget, retailer_id: str, seed_url: str, source: str
) -> RankedCandidate:
    """A historical verified URL as a candidate to re-check (no search result behind it)."""
    product_url = strip_variant(seed_url) + ("/" if seed_url.split("?")[0].endswith("/") else "")
    hit = SearchHit(retailer_id, product_url, "(historical verified URL)", seed_url, 0, 0)
    reason = (f"Matched this configuration exactly in an earlier collection ({source}); "
              "re-checked as a seed, never assumed to match.")
    clues = dict.fromkeys(_CONFIGURATION_CLUES, "NOT_STATED")
    return RankedCandidate(target, hit, [], HISTORICAL_VERIFIED_URL, "historical exact match",
                           "", "", clues, ["HISTORICAL_VERIFIED_URL"], [reason],
                           seed_url=seed_url)


# Supplied candidate URLs (2026-10-19). Tracking parameters added by search engines and shops are
# removed; every other query parameter (``variant`` above all) is kept, so a supplied variant URL
# keeps its product and variant identity. The adapter must then accept the URL unchanged.
_TRACKING_PARAMETERS = re.compile(
    r"^(?:utm_[a-z]+|srsltid|gclid|gbraid|wbraid|fbclid|msclkid|_pos|_sid|_ss|_psq|_fid|"
    r"ref|ref_src)$",
    re.I,
)


def normalize_supplied_url(url: str) -> tuple[str, tuple[str, ...]]:
    """``(normalized URL, removed parameter names)``: scheme and host lowercased, fragment and
    tracking parameters removed, path and remaining query kept exactly (variant identity)."""
    from urllib.parse import parse_qsl, urlencode, urlunsplit

    parts = urlsplit(url.strip())
    kept, removed = [], []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        (removed if _TRACKING_PARAMETERS.match(name) else kept).append((name, value))
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                             urlencode(kept), ""))
    return normalized, tuple(dict.fromkeys(name for name, _ in removed))


def url_shows_code(url: str, code: str) -> bool:
    """True when the product URL handle contains the complete model code as whole segments."""
    handle = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    code = code.strip().lower()
    return bool(code) and re.search(rf"(?:^|-){re.escape(code)}(?:-|$)", handle) is not None


def supplied_candidate(
    target: SearchTarget, retailer_id: str, url: str, supplied_id: str, basis: str
) -> RankedCandidate:
    """A supplied known page the native search did not return: a seed to check directly (the
    bounded fallback), never an approved match. ``basis`` says why it belongs to this job."""
    product_url = strip_variant(url) + ("/" if url.split("?")[0].endswith("/") else "")
    hit = SearchHit(retailer_id, product_url, "(supplied candidate URL)", url, 0, 0)
    reason = (f"Supplied known page {supplied_id} ({basis}); the retailer's native search did not "
              "return it, so it is checked directly as a bounded fallback. A seed only: the "
              "page's own evidence and the matcher decide.")
    clues = dict.fromkeys(_CONFIGURATION_CLUES, "NOT_STATED")
    return RankedCandidate(target, hit, [], SUPPLIED_URL, f"supplied URL {supplied_id}", "", "",
                           clues, [SUPPLIED_URL, "NATIVE_SEARCH_MISSED_SUPPLIED_URL"], [reason],
                           seed_url=url if "variant=" in url else "")


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
    # Option values (option1..3) and the page-metadata reading of them (2026-10-22): installed
    # capacities and colour as the option axes establish them, with basis and scope.
    options: tuple[str, ...] = ()
    reading: VariantReading | None = None


@dataclass
class ProductInspection:
    product_url: str
    outcome: str  # SINGLE_VARIANT, MULTI_VARIANT, NO_STRUCTURED_OFFERS, UNREADABLE
    variants: list[VariantInfo] = field(default_factory=list)
    message: str = ""
    # Option axes of the page and its page-level memory/storage statements (2026-10-22).
    axes: VariantAxes | None = None
    statements: PageStatements | None = None
    # The page says its variant options are named (Shopify ``data-has-only-default-variant=
    # "false"``): its adapter needs the ``?variant=`` URL even for a single variant.
    named_options: bool = False


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
        for node in list(nodes):
            # A ProductGroup lists its variants as Products in ``hasVariant`` (DataBlitz
            # multi-variant pages, 2026-10-19, second review).
            if isinstance(node, dict) and "ProductGroup" in (
                node.get("@type") if isinstance(node.get("@type"), list)
                else [node.get("@type")]
            ) and isinstance(node.get("hasVariant"), list):
                nodes.extend(v for v in node["hasVariant"] if isinstance(v, dict))
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
        return ProductInspection(product_url, "SINGLE_VARIANT", variants, named_options=bool(
            re.search(r"data-has-only-default-variant=[\"']false[\"']", text)))
    if len(variants) < len(distinct):
        return ProductInspection(product_url, "UNREADABLE", variants,
                                 "Some JSON-LD offers have no usable variant URL.")
    statements = statements_from_html(text)
    rows = variant_rows_from_html(text)
    axes = read_axes(
        option_names_from_html(text),
        [(v.variant_id, rows.get(v.variant_id, ("", ()))[1]) for v in variants],
        statements, known_colors(),
    )
    variants = [
        VariantInfo(v.variant_id, v.url, v.title, v.available, v.price,
                    rows.get(v.variant_id, ("", ()))[1], axes.readings.get(v.variant_id))
        for v in variants
    ]
    return ProductInspection(product_url, "MULTI_VARIANT", variants, axes=axes,
                             statements=statements)


@dataclass(frozen=True)
class VariantEvaluation:
    """One variant judged against one target, with a stable reason code (2026-10-22)."""

    decision: str  # COLLECT, ALTERNATIVE, REVIEW
    reason: str
    code: str
    ram: str  # AGREES / DIFFERS (...) / NOT_STATED, from option text or option axes
    storage: str


def _combine(field_name: str, title_state: str, reading_value: str, target: SearchTarget,
             ) -> tuple[str, str]:
    """``(state, source)`` of one field: the option title's own words, else the axis reading."""
    if not reading_value:
        return title_state, "option title"
    wanted = target.core[field_name]
    state = "AGREES" if _same(field_name, reading_value, wanted) else f"DIFFERS ({reading_value})"
    if title_state == "NOT_STATED":
        return state, "option axis"
    if title_state.startswith("DIFFERS") == state.startswith("DIFFERS"):
        return title_state, "option title and axis"
    return "CONFLICT", "option title and axis"


def variant_evaluation(variant: VariantInfo, target: SearchTarget) -> VariantEvaluation:
    """Decide one variant. Its own option text, or the option axes the page's metadata
    establishes (bare capacities under an option whose role the page states), name its RAM and
    storage; a page-level memory statement may rule a configuration out, but never selects a
    variant on its own."""
    if not variant.title and not variant.options:
        return VariantEvaluation("REVIEW", "Variant has no option title on the page.",
                                 "REVIEW_NO_OPTION_TITLE", "NOT_STATED", "NOT_STATED")
    clues = clue_comparison(title_clues(variant.title), target) if variant.title else {}
    reading = variant.reading
    ram, ram_source = _combine("ram_capacity_gb", clues.get("ram_capacity_gb", "NOT_STATED"),
                               reading.ram if reading else "", target)
    storage, storage_source = _combine(
        "storage_capacity_gb", clues.get("storage_capacity_gb", "NOT_STATED"),
        reading.storage if reading else "", target)
    shared_ram = bool(reading and reading.ram_scope == "PRODUCT_LEVEL_UNIFORM"
                      and ram_source == "option axis")
    if "CONFLICT" in {ram, storage}:
        return VariantEvaluation(
            "REVIEW", "The option title and the option axes read a capacity differently "
            f"(RAM {ram}, storage {storage}).", "REVIEW_AXIS_CONFLICT", ram, storage)
    if ram.startswith("DIFFERS") or storage.startswith("DIFFERS"):
        return VariantEvaluation("ALTERNATIVE", (
            f"Different configuration: RAM {ram}, storage {storage} (target RAM "
            f"{target.core['ram_capacity_gb']}, storage {target.core['storage_capacity_gb']}); "
            "kept as a same-product alternative."), "ALTERNATIVE_DIFFERENT_CONFIGURATION",
            ram, storage)
    via = f" (read from {storage_source}: {reading.storage_basis})" if (
        reading and reading.storage and storage_source == "option axis") else ""
    if ram == "AGREES" and storage == "AGREES" and not shared_ram:
        return VariantEvaluation("COLLECT", "Option title states the target RAM and storage."
                                 + via, "COLLECT_RAM_AND_STORAGE", ram, storage)
    if storage == "AGREES":
        # RAM may also be a page-level statement that agrees: it never selects, it rules out.
        note = (" RAM " + ("is stated for every variant by the page and agrees"
                           if shared_ram and ram == "AGREES" else "is not an option"))
        return VariantEvaluation("COLLECT", "Option "
                                 + ("axis" if storage_source == "option axis" else "title")
                                 + " states the target storage;" + note + "." + via,
                                 "COLLECT_STORAGE_" + ("AXIS" if storage_source == "option axis"
                                                       else "TITLE"), ram, storage)
    if ram == "AGREES" and not shared_ram and storage == "NOT_STATED":
        return VariantEvaluation("COLLECT", "Option title states the target RAM; storage is not "
                                 "an option.", "COLLECT_RAM_TITLE", ram, storage)
    unresolved = reading.codes if reading else ()
    if any(code.startswith("CAPACITY_") for code in unresolved):
        return VariantEvaluation(
            "REVIEW", "The option's bare capacity has no established role: "
            + ", ".join(c for c in unresolved if c.startswith("CAPACITY_"))
            + " (the page's Storage statement must list it and no Memory statement may).",
            "REVIEW_CAPACITY_ROLE_UNESTABLISHED", ram, storage)
    return VariantEvaluation(
        "REVIEW", f"Option title does not identify the configuration (RAM {ram}, storage "
        f"{storage}).", "REVIEW_NOT_IDENTIFYING", ram, storage)


def variant_decision(variant: VariantInfo, target: SearchTarget) -> tuple[str, str]:
    """``(decision, reason)`` of a variant: see ``variant_evaluation``."""
    evaluation = variant_evaluation(variant, target)
    return evaluation.decision, evaluation.reason


# Manifest and coverage


CANDIDATE_COLUMNS: tuple[str, ...] = (
    "target_id", "rank", "retailer_id", "candidate_rank", "category", "product_url",
    "title", "code_evidence", "retained_suffix", "family_context",
    *(f"clue_{name}" for name in CORE_MATCH_COLUMNS if name != "model"),
    "flags", "queries", "search_positions", "vendor", "product_type", "search_available",
    "search_price", "decision", "decision_reason", "reasons",
    "configuration_difference", "plausible", "seed_url",
    # Lineage (2026-10-19): search job -> candidate -> collection request -> observation.
    "discovery_round", "collection_url", "collection_request_id",
    # Close-alternative assessment of the search title (2026-10-27).
    "title_difference_count", "title_difference_dimensions", "title_unknown_dimensions",
    "alternative_tier",
)


def candidate_row(candidate: RankedCandidate) -> dict[str, str]:
    hit = candidate.hit
    potential = title_alternative_potential(candidate)
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
        "configuration_difference": configuration_difference(candidate),
        "plausible": "TRUE" if is_plausible(candidate) else "FALSE",
        "seed_url": candidate.seed_url,
        "discovery_round": str(candidate.discovery_round),
        "collection_url": candidate.collection_url,
        "collection_request_id": candidate.collection_request_id,
        "title_difference_count": str(potential.count),
        "title_difference_dimensions": ";".join(potential.differences),
        "title_unknown_dimensions": ";".join(potential.unknown),
        "alternative_tier": potential.tier,
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
