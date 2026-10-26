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

Purchase-block restrictions (2026-10-22). A correct product match does not establish an
unrestricted price. Adapters record statements such as "Promo Price Applicable for Cash Payments
Only" as ``PURCHASE_TERMS`` scoped evidence (kind, scope ``PRICE`` or ``PAGE``, wording). A
``PRICE``-scoped restriction excludes the price from comparison (reason ``RESTRICTED_PRICE``)
under every policy; a ``PAGE``-scoped one is kept as evidence only. Whether a cash-only promo
price should count is an owner policy question: until it is decided the default is exclusion,
with the wording kept.

Nothing here changes identity, a target assignment, or the stored observation; the effect is
reported separately from matching. ``BUNDLE_FLAG_POLICY`` reproduces the earlier rule (every
checked bundle comparable at its package price with a ``BUNDLE_OFFER`` note).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .conventions import model_codes
from .offers import PRODUCT_DESCRIPTION, PURCHASE_TERMS, OfferObservation, clean_text

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

# Purchase-block restriction kinds (2026-10-22) and where a statement applies.
CASH_ONLY = "CASH_ONLY"
PAYMENT_METHOD_LIMITED = "PAYMENT_METHOD_LIMITED"
MEMBER_ONLY = "MEMBER_ONLY"
TRADE_IN_REQUIRED = "TRADE_IN_REQUIRED"
RESTRICTION_KINDS = (CASH_ONLY, PAYMENT_METHOD_LIMITED, MEMBER_ONLY, TRADE_IN_REQUIRED)
PRICE_SCOPE = "PRICE"  # The statement is about the displayed price: it blocks comparison.
PAGE_SCOPE = "PAGE"  # A page-level remark: recorded, never blocking.
RESTRICTED_REASON = "RESTRICTED_PRICE"

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
# "Promo Price Applicable for Cash Payments Only" (PC Express, 2026-10-22). Plain mentions of
# cash ("cash on delivery", "cash back") are not restrictions.
_CASH_ONLY = (
    r"\bcash[\s-]?only\b|\bcash\s+(?:payments?|purchases?|transactions?|basis)\s+only\b"
    r"|\b(?:applicable|valid|available|good|effective)\s+(?:for|with|on|to)\s+cash\b[^.;|]{0,30}"
    r"\bonly\b|\bonly\s+(?:for|with|on|to)\s+cash\b|\bpay(?:ments?)?\s+(?:in\s+)?cash\s+only\b"
)
_PAYMENT_LIMITED = (
    r"\binstall?ments?\s+(?:only|price)\b|\b(?:straight|0\s*%)\s+install?ment\s+price\b"
    r"|\bfinancing\s+(?:only|price)\b|\b(?:credit\s+)?card\s+(?:only|price)\b"
    r"|\b(?:with|via|thru|through)\s+(?:home\s+credit|billease|atome|tendopay)\b"
)
_MEMBER_ONLY = r"\bmembers?(?:hip)?\s+(?:price|only)\b|\bloyalty\s+price\b"
_TRADE_IN = r"\btrade[\s-]?in\b"
_RESTRICTED = re.compile(
    "|".join((_TRADE_IN, _PAYMENT_LIMITED, _MEMBER_ONLY, _CASH_ONLY)), re.I
)
_RESTRICTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (CASH_ONLY, re.compile(_CASH_ONLY, re.I)),
    (PAYMENT_METHOD_LIMITED, re.compile(_PAYMENT_LIMITED, re.I)),
    (MEMBER_ONLY, re.compile(_MEMBER_ONLY, re.I)),
    (TRADE_IN_REQUIRED, re.compile(_TRADE_IN, re.I)),
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
class PurchaseRestriction:
    """One purchase-block statement a page makes (2026-10-22): its kind, whether it is about the
    displayed price or only the page, and the retailer's wording as written."""

    kind: str
    scope: str
    evidence_text: str

    @property
    def label(self) -> str:
        return f"{self.kind} ({self.scope})"


@dataclass(frozen=True)
class OfferTerms:
    category: str
    # "source: wording" for each statement that decided the category, raw text kept.
    evidence: tuple[str, ...] = ()
    # Inclusions the page states, as written.
    included_items: tuple[str, ...] = ()
    # Restrictions an adapter recorded as PURCHASE_TERMS evidence (all scopes).
    restrictions: tuple[PurchaseRestriction, ...] = ()


_RESTRICTION_LABEL = re.compile(r"^([A-Z_]+) \((PRICE|PAGE)\)$")


def scan_purchase_restrictions(text: str) -> tuple[PurchaseRestriction, ...]:
    """Restriction statements in page prose, one per kind found (an adapter's capture helper).

    A statement that names a price (``Promo Price Applicable for Cash Payments Only``) is
    ``PRICE``-scoped and blocks comparison; one that does not is a ``PAGE`` remark. Plain
    mentions of cash or installments ("cash on delivery", "0% installment available") are not
    restrictions.
    """
    found = []
    for sentence in map(clean_text, _sentences(text)):
        for kind, pattern in _RESTRICTION_PATTERNS:
            if pattern.search(sentence):
                scope = PRICE_SCOPE if _PRICE_WORDS.search(sentence) else PAGE_SCOPE
                found.append(PurchaseRestriction(kind, scope, sentence))
    return tuple(dict.fromkeys(found))


def purchase_restriction_items(
    restrictions: tuple[PurchaseRestriction, ...],
) -> tuple[tuple[str, str, str], ...]:
    """``scoped_evidence`` items (``PURCHASE_TERMS``, label, wording) for an adapter to keep."""
    return tuple((PURCHASE_TERMS, item.label, item.evidence_text) for item in restrictions)


def purchase_restrictions(observation: OfferObservation) -> tuple[PurchaseRestriction, ...]:
    """The restrictions an adapter recorded for the offer, read back from its own evidence."""
    details = observation.details
    found = []
    for scope, label, value in () if details is None else details.scoped_evidence:
        match = _RESTRICTION_LABEL.match(label) if scope == PURCHASE_TERMS else None
        if match and match[1] in RESTRICTION_KINDS:
            found.append(PurchaseRestriction(match[1], match[2], value))
    return tuple(dict.fromkeys(found))


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
    """Classify what the displayed price buys, from the offer's own wording only.

    A ``PRICE``-scoped purchase restriction recorded by the adapter (2026-10-22) makes the
    price ``RESTRICTED_PRICE`` whatever else the wording says; the other evidence is kept.
    """
    terms = _classify_wording(observation)
    captured = purchase_restrictions(observation)
    blocking = [item for item in captured if item.scope == PRICE_SCOPE]
    if blocking and terms.category != RESTRICTED_PRICE:
        evidence = (*(f"{item.label}: {item.evidence_text}" for item in blocking),
                    *terms.evidence)
        terms = replace(terms, category=RESTRICTED_PRICE, evidence=evidence)
    elif blocking:
        evidence = (*(f"{item.label}: {item.evidence_text}" for item in blocking),
                    *terms.evidence)
        terms = replace(terms, evidence=tuple(dict.fromkeys(evidence)))
    return replace(terms, restrictions=captured)


def _classify_wording(observation: OfferObservation) -> OfferTerms:
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
        if terms is not None and any(r.scope == PRICE_SCOPE for r in terms.restrictions):
            # An adapter-recorded price restriction applies under every policy (2026-10-22).
            reasons = (*reasons, RESTRICTED_REASON)
        return reasons, ("BUNDLE_OFFER",) if bundle_flag == "TRUE" else ()
    reason = TERMS_EXCLUSION.get(terms.category)
    note = TERMS_NOTE.get(terms.category)
    return ((reason,) if reason else ()), ((note,) if note else ())
