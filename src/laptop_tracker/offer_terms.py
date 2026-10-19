"""Commercial terms behind a displayed laptop price (purchase-terms policy, 2026-10-19).

Price eligibility asks a different question from identity matching: does the displayed price
buy this laptop on ordinary terms? Adapters flag bundle *wording* (``FREE``, ``BUNDLE``,
``BACKPACK``, bundle markup); a keyword alone does not say what the price covers. This module
reads the offer's own wording for evidence of the purchase terms and classifies the offer:

- ``STANDARD_OFFER``: no bundle or purchase-condition wording.
- ``INCLUDED_ITEMS``: the laptop at its displayed price with stated inclusions (free or
  included Office, warranty, backpack, mouse, ...). Comparable, with a visible note.
- ``PAID_PACKAGE``: the price covers another product (``+ 24" Monitor``, bundle components that
  are not ordinary inclusions, a paid upgrade).
- ``CONDITIONAL_PURCHASE``: the displayed price requires buying something else.
- ``RESTRICTED_PRICE``: the price applies only to financing, card, trade-in, or member terms.
- ``PURCHASE_TERMS_UNKNOWN``: bundle wording without evidence of what the price covers.
- ``BUNDLE_NOT_CHECKED``: the adapter did not check for bundles and no wording says anything.

Nothing here changes identity, a target assignment, or the stored observation; the effect is
reported separately from matching. ``BUNDLE_FLAG_POLICY`` reproduces the earlier rule (every
checked bundle comparable at its package price with a ``BUNDLE_OFFER`` note).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .conventions import model_codes
from .offers import PRODUCT_DESCRIPTION, OfferObservation, clean_text

PURCHASE_TERMS_POLICY = "purchase-terms/2026-10-19"
BUNDLE_FLAG_POLICY = "bundle-flag/2026-10-15"
OFFER_TERMS_POLICIES: tuple[str, ...] = (PURCHASE_TERMS_POLICY, BUNDLE_FLAG_POLICY)
DEFAULT_OFFER_TERMS_POLICY = PURCHASE_TERMS_POLICY

STANDARD_OFFER = "STANDARD_OFFER"
INCLUDED_ITEMS = "INCLUDED_ITEMS"
PAID_PACKAGE = "PAID_PACKAGE"
CONDITIONAL_PURCHASE = "CONDITIONAL_PURCHASE"
RESTRICTED_PRICE = "RESTRICTED_PRICE"
PURCHASE_TERMS_UNKNOWN = "PURCHASE_TERMS_UNKNOWN"
BUNDLE_NOT_CHECKED = "BUNDLE_NOT_CHECKED"

# Exclusion reason and price note per category under the purchase-terms policy.
TERMS_EXCLUSION: dict[str, str] = {
    PAID_PACKAGE: "PAID_PACKAGE_PRICE",
    CONDITIONAL_PURCHASE: "PURCHASE_CONDITION_REQUIRED",
    RESTRICTED_PRICE: "RESTRICTED_PRICE",
    PURCHASE_TERMS_UNKNOWN: "PURCHASE_TERMS_UNKNOWN",
    BUNDLE_NOT_CHECKED: "BUNDLE_STATUS_UNKNOWN",
}
TERMS_NOTE: dict[str, str] = {INCLUDED_ITEMS: "INCLUDED_ITEMS_OFFER"}

# Ordinary inclusions: software, service, and small accessories that come with a laptop.
_ORDINARY = (
    r"(?:back\s?pack|bag|sleeve|pouch|(?:carrying\s+)?case|mouse\s?pad|mouse|headsets?|"
    r"earphones?|earbuds|keyboard\s+(?:cover|protector)|screen\s+protector|stylus|pen|"
    r"(?:ms|microsoft)\s+office(?:\s+(?:home(?:\s*(?:&|and)\s*student)?|h&s|2021|2024|365))*|"
    r"office\s+(?:home(?:\s*(?:&|and)\s*student)?|h&s|2021|2024|365)|microsoft\s+365|m365|"
    r"office|anti-?virus|mcafee|norton|kaspersky|(?:[0-9]+[- ]years?\s+)?(?:extended\s+)?"
    r"warranty|windows\s*11(?:\s+(?:home|pro))?|win\s*11|charger|adapter|usb\s+hub)"
)
# Products that make the displayed price a package price when sold with the laptop. "with" is
# also ordinary specification wording ("with 16GB RAM", "with tablet mode", "with Dolby
# speakers"), so only unmistakable products count after "with"; "+" joins any of them.
_MAJOR_STRONG = (
    r"(?:monitor|printer|chair|desk|projector|tv|television|router|console|docking\s+station|"
    r"smart\s?watch|ipad)"
)
_MAJOR_PLUS = rf"(?:{_MAJOR_STRONG}|tablet|phone|smartphone|speakers?|upgrade)"
_SIZE = r"(?:[0-9]{2}(?:\.[0-9])?\s*(?:\"|”|″|''|-?\s*inch(?:es)?|in)\s*)?"
_FILLER = r"(?:[A-Za-z0-9&'.\-]+\s+){0,3}?"
_ARTICLE = r"(?:a\s+|an\s+|1\s*x?\s*)?"
_INCLUSION_OPENER = (
    r"(?:\b(?:free|freebies?|complimentary|inclusive\s+of|includes?|included|including|"
    r"inclusions?|(?:what'?s\s+)?in\s+the\s+box|comes\s+with|bundled\s+with|with|plus)\b"
    r"\s*:?|\bw/|\+)"
)
_INCLUDED_STATEMENT = re.compile(
    rf"{_INCLUSION_OPENER}\s*{_ARTICLE}(?:free\s+)?{_FILLER}{_ORDINARY}\b", re.I
)
_FREE_STATEMENT = re.compile(
    rf"\b(?:free|freebies?|complimentary)\b\s*:?\s*{_ARTICLE}{_FILLER}{_SIZE}"
    rf"(?:{_ORDINARY}|{_MAJOR_PLUS})\b",
    re.I,
)
_PACKAGE_STATEMENT = re.compile(
    rf"\+\s*(?!free\b){_ARTICLE}{_FILLER}{_SIZE}{_MAJOR_PLUS}\b(?!\s*mode)"
    rf"|\b(?:with|w/|plus|bundled\s+with)\s+(?!free\b){_ARTICLE}{_FILLER}{_SIZE}"
    rf"{_MAJOR_STRONG}\b"
    rf"|\b{_MAJOR_PLUS}\s+(?:bundle|package|combo|set)\b"
    rf"|\b(?:bundle|package|combo)\s+(?:price\s+)?(?:with|w/|\+)\s*(?!free\b){_FILLER}"
    rf"{_SIZE}{_MAJOR_PLUS}\b",
    re.I,
)
_RESTRICTED = re.compile(
    r"\btrade[\s-]?in\b|\binstall?ments?\s+(?:only|price)\b|\b(?:straight|0\s*%)\s+"
    r"install?ment\s+price\b|\bfinancing\s+(?:only|price)\b|\b(?:credit\s+)?card\s+(?:only|"
    r"price)\b|\bmembers?(?:hip)?\s+(?:price|only)\b|\b(?:with|via|thru|through)\s+(?:home\s+"
    r"credit|billease|atome|tendopay)\b|\bloyalty\s+price\b",
    re.I,
)
_CONDITION = re.compile(
    r"\b(?:with|upon)\s+(?:the\s+)?(?:minimum\s+)?purchase\s+of\s+(?!(?:this|the|any|each|every"
    r"|our)\s+(?:laptop|unit|item|product|notebook|order)\b)"
    r"|\bwhen\s+(?:bought|purchased)\s+(?:together\s+)?with\b|\bmin(?:imum)?\.?\s+(?:purchase|"
    r"spend)\b|\badd\s+(?:₱|php|p)\s?[0-9]|\bfor\s+an?\s+additional\s+(?:₱|php|p)\s?[0-9]"
    r"|\bwhen\s+you\s+(?:also\s+)?(?:buy|purchase)\s+(?!(?:this|the)\s+(?:laptop|unit|item|"
    r"product|notebook)\b)",
    re.I,
)
_PRICE_WORDS = re.compile(r"\bprice\b|₱|\bphp\b|\bdiscount\w*\b|\bless\b|\boff\b|\bpromo\b",
                          re.I)
_FREE_WORDS = re.compile(r"\b(?:free|freebies?|complimentary|gift)\b", re.I)
_LAPTOP_WORDS = re.compile(r"\b(?:laptop|notebook|ultrabook|chromebook)\b", re.I)
_PROMO_LABEL = re.compile(
    r"\b(?:freebies?|inclusions?|inclusive|package|in\s+the\s+box|promo\w*|bundle|offer|"
    r"free\s+items?|gifts?)\b",
    re.I,
)
_TERM = re.compile(r"^(?:title|variant)\s+term\s+([A-Z]+):\s*(.*)$")


@dataclass(frozen=True)
class OfferTerms:
    category: str
    # "source: wording" for each statement that decided the category, raw text kept.
    evidence: tuple[str, ...] = ()
    # Inclusions the page states, as written.
    included_items: tuple[str, ...] = ()


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+|\s\|\s|\n", text) if part.strip()]


def _sources(observation: OfferObservation) -> list[tuple[str, str]]:
    """Wording that can state purchase terms, as ``(source, text)``."""
    details = observation.details
    sources = [("title", observation.raw_title), ("price text", observation.raw_price_text)]
    if details is not None:
        sources += [
            (f"specification '{label}'", f"{label}: {value}")
            for label, value in details.raw_specifications
            if _PROMO_LABEL.search(label) and value
        ]
        sources += [
            (f"description '{label}'", sentence)
            for scope, label, value in details.scoped_evidence
            if scope == PRODUCT_DESCRIPTION
            for sentence in _sentences(value)
        ]
        sources += [
            ("bundle evidence", item) for item in details.bundle_evidence
            if not _TERM.match(item) and not item.startswith("bundle component")
        ]
        if any(item.startswith("variant term") for item in details.bundle_evidence):
            # PC Central / Octagon put the selected variant title first.
            variant = observation.raw_configuration_text.split(" | ", 1)[0]
            sources.append(("variant title", variant))
    return [(source, clean_text(text)) for source, text in sources if text.strip()]


def _components(evidence: tuple[str, ...]) -> list[str] | None:
    """Bundle component names from the adapter's bundle markup, or None when there is none."""
    for item in evidence:
        if item.startswith("bundle components:"):
            return [clean_text(part) for part in item.split(":", 1)[1].split(" ; ") if part]
        if item == "bundle component markup present":
            return []
    return None


def classify_offer_terms(observation: OfferObservation) -> OfferTerms:
    """Classify what the displayed price buys, from the offer's own wording only."""
    details = observation.details
    flag = None if details is None else details.bundle_flag
    bundle_evidence = () if details is None else details.bundle_evidence
    sources = _sources(observation)
    price_bound = [(s, t) for s, t in sources if not s.startswith("description")]

    restricted = [f"{s}: {t}" for s, t in price_bound if _RESTRICTED.search(t)] + [
        f"{s}: {t}" for s, t in sources
        if s.startswith("description") and _RESTRICTED.search(t) and _PRICE_WORDS.search(t)
    ]
    if restricted:
        return OfferTerms(RESTRICTED_PRICE, tuple(restricted))
    # A condition on the price excludes it; a condition on a freebie only means that item is
    # not part of the price ("Free bag with purchase of Microsoft 365").
    conditional, extras = [], []
    for s, t in sources:
        if not _CONDITION.search(t):
            continue
        if _FREE_WORDS.search(t) and not re.search(r"\bprice\b|₱|\bphp\b", t, re.I):
            extras.append((s, t))
        elif _PRICE_WORDS.search(t) or s == "price text":
            conditional.append(f"{s}: {t}")
    if conditional:
        return OfferTerms(CONDITIONAL_PURCHASE, tuple(conditional))
    sources = [item for item in sources if item not in extras]
    extra_notes = tuple(f"conditional extra, not part of the price: {s}: {t}" for s, t in extras)

    package = [
        f"{s}: {m[0]}" for s, t in price_bound for m in _PACKAGE_STATEMENT.finditer(t)
        if not _FREE_STATEMENT.search(t[max(0, m.start() - 12): m.end()])
    ]
    components = _components(bundle_evidence)
    included = [
        (s, clean_text(m[0])) for s, t in sources for m in _INCLUDED_STATEMENT.finditer(t)
    ] + [(s, clean_text(m[0])) for s, t in sources for m in _FREE_STATEMENT.finditer(t)]
    included = [
        (s, t) for s, t in dict.fromkeys(included)
        # One statement found by two patterns is one inclusion ("+ FREE Backpack").
        if not any(s == s2 and t != t2 and t.casefold() in t2.casefold() for s2, t2 in included)
    ]
    if components:
        others = [
            name for name in components
            if not model_codes(name) and not _LAPTOP_WORDS.search(name)
        ]
        major = [name for name in others if not re.fullmatch(
            rf"(?:free\s+|freebie:?\s+)?{_FILLER}{_ORDINARY}(?:\s+[\w&'.\-]+){{0,3}}", name,
            re.I,
        )]
        if major:
            package.append("bundle components: " + " ; ".join(major))
        elif others and not any(_FREE_WORDS.search(text) for _, text in included) and not any(
            _FREE_WORDS.search(name) for name in others
        ):
            return OfferTerms(
                PURCHASE_TERMS_UNKNOWN,
                ("bundle components without a stated inclusion: " + " ; ".join(others),),
            )
        else:
            included += [("bundle components", name) for name in others]
    if package:
        return OfferTerms(PAID_PACKAGE, tuple(dict.fromkeys(package)))
    if components == []:
        return OfferTerms(PURCHASE_TERMS_UNKNOWN, ("bundle component markup without names",))

    # Every bundle term the adapter flagged must be explained by a stated inclusion.
    unexplained = []
    for item in bundle_evidence:
        match = _TERM.match(item)
        if not match:
            continue
        term = match[1]
        if term in {"FREE", "BUNDLE"}:
            if not included:
                unexplained.append(item)
        elif not any(term.casefold() in text.casefold() for _, text in included):
            unexplained.append(item)
    if unexplained:
        return OfferTerms(PURCHASE_TERMS_UNKNOWN, tuple(unexplained))
    if included:
        return OfferTerms(
            INCLUDED_ITEMS,
            (*(f"{s}: {t}" for s, t in included), *extra_notes),
            tuple(dict.fromkeys(t for _, t in included)),
        )
    if flag is None:
        return OfferTerms(BUNDLE_NOT_CHECKED, extra_notes)
    if flag:
        return OfferTerms(PURCHASE_TERMS_UNKNOWN, (*bundle_evidence, *extra_notes))
    return OfferTerms(STANDARD_OFFER, extra_notes)


def terms_price_effect(
    terms: OfferTerms | None, bundle_flag: str, policy: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(exclusion reasons, price notes)`` from the commercial terms under ``policy``."""
    if policy not in OFFER_TERMS_POLICIES:
        raise ValueError(f"Unknown offer terms policy: {policy}")
    if policy == BUNDLE_FLAG_POLICY or terms is None:
        reasons = () if bundle_flag in {"TRUE", "FALSE"} else ("BUNDLE_STATUS_UNKNOWN",)
        return reasons, ("BUNDLE_OFFER",) if bundle_flag == "TRUE" else ()
    reason = TERMS_EXCLUSION.get(terms.category)
    note = TERMS_NOTE.get(terms.category)
    return ((reason,) if reason else ()), ((note,) if note else ())
