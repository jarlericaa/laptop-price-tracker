"""PC Express (retailer code PCX) product-page adapter (overall Phase 7).

Parses one already-fetched PC Express product page into one ``ParsedOffer``. It makes no HTTP
requests; the shared ``Fetcher`` does all retrieval. Live use is enabled by the owner's
explicit PCX decision for this internal tool. The retailer's published terms still restrict
automated collection without written consent; the external-policy finding remains RESTRICTED.
See docs/RETAILER_DATA_USE_REVIEW.md and docs/DECISIONS.md. Page
assumptions, versioning, and limitations are in docs/adapters/PCX.md.

The adapter preserves retailer text. It does not infer laptop specifications, assign target
IDs, or decide whether an offer matches a tracked configuration (Phase 8).
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ..offers import (
    IDENTIFIER,
    Availability,
    ObservationValidationError,
    OfferDetails,
    PriceType,
    clean_text,
    parse_price,
    url_host,
)
from ..scraping import FetchedPage, ParsedOffer, ParseError, ScrapeRequest

RETAILER_ID = "pcx"
RETAILER_CODE = "PCX"
# The Shopify storefront. www.pcx.com.ph redirects here (HTTP 301, checked 2026-10-01); it is
# not allowed, so a request must name this host directly.
HOST = "pcx.com.ph"
# Project status: the owner explicitly enabled PCX for internal use on 2026-10-01.
# The retailer's published restriction and absence of written consent remain documented.
DATA_USE_REVIEW_STATUS = "OWNER_APPROVED"

_JSONLD_AVAILABILITY = {
    "instock": Availability.IN_STOCK,
    "outofstock": Availability.OUT_OF_STOCK,
    "soldout": Availability.OUT_OF_STOCK,
    "preorder": Availability.PREORDER,
}
# Add-to-cart button labels. "Add to cart" is not stock wording, so it only confirms another
# signal; the other labels state availability explicitly.
_BUTTON_LABELS: dict[str, Availability | None] = {
    "add to cart": None,
    "sold out": Availability.OUT_OF_STOCK,
    "pre-order": Availability.PREORDER,
    "preorder": Availability.PREORDER,
    "unavailable": Availability.UNKNOWN,
}
# Title wording that suggests a bundle or freebie. Matches are flagged for downstream review;
# the offer is never dropped.
BUNDLE_TITLE_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("FREE", re.compile(r"(?:\+\s*)?\bFREE\b", re.I)),
    *(
        (term, re.compile(rf"\b{term}S?\b", re.I))
        for term in (
            "BUNDLE", "MONITOR", "TABLET", "CHAIR", "BACKPACK", "BRIEFCASE", "BAG", "MOUSE",
            "HEADSET",
        )
    ),
)
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
     "track", "wbr"}
)
_LINE_END_TAGS = frozenset({"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})
_HIDDEN_CLASSES = frozenset({"visually-hidden", "t4s-d-none"})
_FOREIGN_CURRENCY = re.compile(r"[$€£¥]|\b(?!PHP\b)[A-Z]{3}\b")


def product_request(url: str) -> ScrapeRequest:
    """Build a request for one PC Express product page (``/products/<handle>`` on ``HOST``)."""
    path = urlsplit(url).path if url_host(url) == HOST else ""
    if not re.fullmatch(r"/products/[^/]+/?", path):
        raise ValueError(f"Not a PC Express product URL on {HOST}.")
    return ScrapeRequest(RETAILER_ID, url)


class PCXAdapter:
    retailer_id = RETAILER_ID
    retailer_code = RETAILER_CODE
    # Bump when parsing rules change; recorded in every observation's source metadata.
    # pcx-2 (2026-10-13): adds identifier provenance as scoped evidence; parsing is unchanged.
    adapter_version = "pcx-2"
    allowed_hosts = frozenset({HOST})
    # Explicit project-owner authorization; not a claim of retailer consent.
    live_use_approved = True

    def parse(self, page: FetchedPage) -> list[ParsedOffer]:
        parsed = _ProductPageParser()
        parsed.feed(page.text())
        parsed.close()
        products = _products(parsed.jsonld_blocks)
        if not products and not parsed.saw_main:
            raise ParseError(
                "UNRECOGNIZED_LAYOUT",
                "No Product JSON-LD and no main product block found; the layout may have changed.",
            )
        if parsed.variant_markup:
            raise ParseError(
                "AMBIGUOUS_VARIANT", "The page offers several variants of the product."
            )
        titles = list(dict.fromkeys(title for title in parsed.titles if title))
        if len(titles) > 1:
            raise ParseError("AMBIGUOUS_PRODUCT", "The main block shows more than one title.")
        names = _agreed([_unescaped(node.get("name")) for node in products], "name")
        title = _first(titles) or names
        if not title:
            raise ParseError("TITLE_NOT_FOUND", "The product title could not be found.")
        offers = [offer for offer in map(_node_offer, products) if offer is not None]
        price = _resolve_price(offers, parsed.price_blocks)
        availability, evidence = _resolve_availability(offers, parsed.buttons, parsed.notify)
        specs = parsed.description_rows
        raw_configuration = (
            clean_text(parsed.summary)
            or " | ".join(f"{label}: {value}" if value else label for label, value in specs)
            or _agreed([_unescaped(node.get("description")) for node in products], "description")
        )
        bundle_evidence = _bundle_evidence(title)
        sku = _agreed(
            [_text(node.get("sku")) for node in products]
            + [_text(offer.get("sku")) for offer in offers],
            "SKU",
        ) or _first(parsed.skus)
        # The SEO app's block copies the retailer's internal SKU into "mpn"; that is not a
        # manufacturer part number, so a value equal to the SKU is not kept.
        mpns = [_text(node.get("mpn")) for node in products]
        mpns += [_text(offer.get("mpn")) for offer in offers]
        # Every identifier is kept with its source. A JSON-LD "mpn" key is not proof of a
        # manufacturer role (one block repeats the retailer SKU, another is blank), and the
        # model code in the title or the unlabeled summary is not a labeled model field.
        identifiers: list[tuple[str, str, str]] = []
        for index, node in enumerate(products, start=1):
            for key in ("sku", "mpn", "productID", "gtin", "gtin13"):
                if key in node:
                    value = _text(node.get(key))
                    label = f"JSON-LD Product block {index} {key}"
                    identifiers.append((IDENTIFIER, label, value or "(blank)"))
        for index, offer in enumerate(offers, start=1):
            for key in ("sku", "mpn"):
                if key in offer:
                    value = _text(offer.get(key))
                    label = f"JSON-LD Offer {index} {key}"
                    identifiers.append((IDENTIFIER, label, value or "(blank)"))
        for value in dict.fromkeys(parsed.skus):
            identifiers.append((IDENTIFIER, "visible SKU", value))
        for value in dict.fromkeys(parsed.product_ids):
            identifiers.append((IDENTIFIER, "product form product-id", value))
        for value in dict.fromkeys(parsed.form_variant_ids):
            identifiers.append((IDENTIFIER, "product form variant id", value))
        details = OfferDetails(
            retailer_product_id=_digits(parsed.product_ids)
            or _agreed([_text(node.get("productID")) for node in products], "product ID"),
            retailer_sku=sku,
            brand=_agreed([_brand(node) for node in products], "brand") or _first(parsed.vendors),
            manufacturer_part_number=_agreed([mpn for mpn in mpns if mpn != sku], "MPN"),
            price_type=price.price_type,
            regular_price=price.regular,
            regular_price_text=price.regular_text,
            sale_price=price.current if price.price_type is PriceType.SALE else None,
            sale_price_text=price.current_text if price.price_type is PriceType.SALE else "",
            raw_specifications=tuple(specs),
            raw_categories=tuple(dict.fromkeys(parsed.types + parsed.categories)),
            bundle_flag=bool(bundle_evidence),
            bundle_evidence=bundle_evidence,
            scoped_evidence=tuple(dict.fromkeys(identifiers)),
        )
        return [
            ParsedOffer(
                raw_title=title,
                price_text=price.current_text,
                currency="PHP",
                availability=availability,
                availability_evidence=evidence,
                raw_configuration_text=raw_configuration,
                offer_url=_offer_url(offers, products) or parsed.canonical,
                details=details,
            )
        ]


# JSON-LD


def _products(blocks: list[str]) -> list[dict]:
    """The page's schema.org Product nodes, all describing the same product page.

    Reviewed pages carry two Product blocks for one product: the AVADA SEO Suite app's and the
    Shopify Product Reviews app's. They must name the same product path; otherwise the page is
    ambiguous.
    """
    products: list[dict] = []
    for block in blocks:
        try:
            data = json.loads(block, parse_float=Decimal)
        except ValueError:
            continue  # A broken block is ignored; visible content is the fallback.
        nodes = data if isinstance(data, list) else [data]
        for node in list(nodes):
            if isinstance(node, dict) and isinstance(node.get("@graph"), list):
                nodes.extend(node["@graph"])
        for node in nodes:
            if isinstance(node, dict) and "Product" in _types(node) and node not in products:
                products.append(node)
    if len(products) > 1:
        paths = [_node_paths(node) for node in products]
        if not all(paths) or len(set().union(*paths)) != 1:
            raise ParseError("AMBIGUOUS_PRODUCT", "The page describes more than one Product.")
    return products


def _node_paths(node: dict) -> set[str]:
    offers = node.get("offers")
    candidates = [node.get("@id"), node.get("url")]
    if isinstance(offers, dict):
        candidates.append(offers.get("url"))
    return {
        urlsplit(value.strip()).path.rstrip("/")
        for value in candidates
        if isinstance(value, str) and value.strip()
    }


def _node_offer(product: dict) -> dict | None:
    """The one concrete Offer of a Product node, or None when absent.

    Several offers (variants) or an AggregateOffer cannot be tied to one displayed price, so
    they are ambiguous rather than guessed.
    """
    offers = product.get("offers")
    if offers is None:
        return None
    if isinstance(offers, list):
        if len(offers) > 1:
            raise ParseError("AMBIGUOUS_PRICE", "JSON-LD lists more than one offer.")
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None
    if "Offer" not in _types(offers):
        raise ParseError("AMBIGUOUS_PRICE", "JSON-LD offer is not a single Offer.")
    return offers


def _offer_url(offers: list[dict], products: list[dict]) -> str:
    for candidate in [offer.get("url") for offer in offers] + [
        node.get(key) for node in products for key in ("url", "@id")
    ]:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _types(node: dict) -> list[str]:
    kind = node.get("@type", [])
    return [kind] if isinstance(kind, str) else [k for k in kind if isinstance(k, str)]


def _text(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int, Decimal)):
        return clean_text(str(value))
    return ""


def _unescaped(value: object) -> str:
    """JSON-LD text with HTML entities decoded; the SEO app's block encodes them (``&quot;``)."""
    text = _text(value)
    for _ in range(2):  # Some entities are double-encoded (``&amp;amp;``).
        text = html.unescape(text)
    return clean_text(text)


def _brand(product: dict) -> str:
    """Brand only when JSON-LD states it; never guessed from the title."""
    brand = product.get("brand")
    if isinstance(brand, dict):
        return _text(brand.get("name"))
    return _text(brand)


def _agreed(values: list[str], what: str) -> str:
    """The one value the JSON-LD blocks agree on; blank when none states it."""
    distinct = list(dict.fromkeys(value for value in values if value))
    if len(distinct) > 1:
        raise ParseError("AMBIGUOUS_PRODUCT", f"JSON-LD blocks disagree on the product {what}.")
    return _first(distinct)


# Price


@dataclass(frozen=True)
class _Price:
    current: Decimal
    current_text: str
    regular: Decimal | None
    regular_text: str
    price_type: PriceType | None


def _resolve_price(offers: list[dict], blocks: list[_PriceBlock]) -> _Price:
    shown = _visible_price(blocks)
    currencies = {offer.get("priceCurrency") for offer in offers} - {None, ""}
    if currencies - {"PHP"}:
        raise ParseError("UNSUPPORTED_CURRENCY", "JSON-LD priceCurrency is not PHP.")
    priced = [offer for offer in offers if "price" in offer]
    ld_texts = [_text(offer.get("price")) for offer in priced]
    ld_values = {_price_value(text, "JSON-LD price") for text in ld_texts}
    if len(ld_values) > 1:
        raise ParseError("PRICE_CONFLICT", "JSON-LD blocks disagree on the price.")
    ld_current = next(iter(ld_values), None)

    if shown is None:
        if ld_current is None:
            raise ParseError("PRICE_NOT_FOUND", "No current selling price was found.")
        if any(offer.get("priceCurrency") != "PHP" for offer in priced):
            raise ParseError("CURRENCY_NOT_FOUND", "JSON-LD price has no PHP priceCurrency.")
        # JSON-LD has no compare-at price, so sale versus regular is not known.
        return _Price(ld_current, ld_texts[0], None, "", None)
    if ld_current is not None and shown.current != ld_current:
        raise ParseError("PRICE_CONFLICT", "Displayed current price and JSON-LD price disagree.")
    if shown.regular is None or shown.regular == shown.current:
        return _Price(
            shown.current, shown.current_text, shown.current, shown.current_text, PriceType.REGULAR
        )
    if shown.regular < shown.current:
        raise ParseError("PRICE_CONFLICT", "The regular price is below the current price.")
    return _Price(
        shown.current, shown.current_text, shown.regular, shown.regular_text, PriceType.SALE
    )


@dataclass(frozen=True)
class _ShownPrice:
    current: Decimal
    current_text: str
    regular: Decimal | None
    regular_text: str


def _visible_price(blocks: list[_PriceBlock]) -> _ShownPrice | None:
    """The main product's ``div.t4s-product-price``.

    Bare text in the block is the regular price. On sale, ``<ins>`` is the current price and
    ``<del>`` the regular price; the "SAVE" badge is ignored. Anything else is ambiguous. Text
    without digits (for example "Call for price") is not an amount.
    """
    amounts = [
        [(kind, text) for kind, text in block.amounts() if re.search(r"[0-9]", text)]
        for block in blocks
    ]
    amounts = [block for block in amounts if block]
    if not amounts:
        return None
    if len(amounts) > 1:
        raise ParseError("AMBIGUOUS_PRICE", "More than one product price is displayed.")
    kinds = {kind: [text for k, text in amounts[0] if k == kind] for kind in _KINDS}
    sale, compare, plain = kinds["sale"], kinds["compare"], kinds["plain"]
    if len(plain) == 1 and not sale and not compare:
        return _ShownPrice(_amount(plain[0], "displayed price"), plain[0], None, "")
    if len(sale) == 1 and len(compare) == 1 and not plain:
        return _ShownPrice(
            _amount(sale[0], "displayed sale price"),
            sale[0],
            _amount(compare[0], "displayed regular price"),
            compare[0],
        )
    raise ParseError(
        "AMBIGUOUS_PRICE", "The current selling price cannot be identified unambiguously."
    )


def _amount(text: str, what: str) -> Decimal:
    """One displayed amount; it must carry ``₱`` or ``PHP`` and nothing but the number."""
    if len(re.findall(r"[0-9][0-9.,]*", text)) > 1:
        raise ParseError("AMBIGUOUS_PRICE", f"The {what} shows more than one number.")
    rest = re.sub(r"₱|\bPHP\b", " ", text)
    if _FOREIGN_CURRENCY.search(rest):
        raise ParseError("UNSUPPORTED_CURRENCY", f"The {what} is not in pesos.")
    if rest == text:
        raise ParseError("CURRENCY_NOT_FOUND", f"The {what} has no peso sign or PHP code.")
    if re.sub(r"[0-9.,\s]", "", rest):
        raise ParseError("AMBIGUOUS_PRICE", f"The {what} has wording besides the amount.")
    return _price_value(text, what)


def _price_value(text: str, what: str) -> Decimal:
    try:
        return parse_price(text)
    except ObservationValidationError:
        raise ParseError("INVALID_PRICE", f"The {what} is not a single valid amount.") from None


# Availability


@dataclass(frozen=True)
class _Button:
    label: str
    disabled: bool


def _resolve_availability(
    offers: list[dict], buttons: list[_Button], notify: list[bool]
) -> tuple[str, str]:
    """Combine JSON-LD availability, the main add-to-cart button, and the notify-me button.

    JSON-LD, the "Sold out", "Pre-order", and "Unavailable" labels, and a visible "Notify Me
    When Available" button (out of stock) are explicit signals and must agree. The add-to-cart
    button must also be consistent: enabled "Add to cart" for in stock, enabled "Pre-order" or
    "Add to cart" for preorder, absent or disabled or "Sold out" for out of stock.
    Disagreement, several different buttons, or no explicit signal gives UNKNOWN; price
    visibility and an "Add to cart" label alone never decide stock.
    """
    tokens = list(
        dict.fromkeys(
            _text(offer.get("availability")).rstrip("/").rsplit("/", 1)[-1]
            for offer in offers
            if _text(offer.get("availability"))
        )
    )
    # Unmapped values (BackOrder, LimitedAvailability, and the like) count as UNKNOWN.
    from_ld = {_JSONLD_AVAILABILITY.get(token.lower(), Availability.UNKNOWN) for token in tokens}
    distinct = list(dict.fromkeys(buttons))
    conflict = len(distinct) > 1 or len(from_ld) > 1
    button = distinct[0] if len(distinct) == 1 else None
    label = button.label.lower() if button else ""
    from_label = _BUTTON_LABELS.get(label, Availability.UNKNOWN) if button else None
    notify_shown = any(notify)

    explicit = [*from_ld]
    if from_label is not None:
        explicit.append(from_label)
    if notify_shown:
        explicit.append(Availability.OUT_OF_STOCK)
    value = Availability.UNKNOWN
    if explicit and not conflict and len(set(explicit)) == 1:
        value = explicit[0]
        enabled = button is not None and not button.disabled
        consistent = {
            Availability.IN_STOCK: enabled and label == "add to cart",
            Availability.PREORDER: enabled and label in {"add to cart", "pre-order", "preorder"},
            Availability.OUT_OF_STOCK: button is None or button.disabled or label == "sold out",
            Availability.UNKNOWN: True,
        }[value]
        if not consistent:
            value, conflict = Availability.UNKNOWN, True
    elif len(set(explicit)) > 1:
        conflict = True

    shown = " / ".join(
        f"{b.label or 'no label'} ({'disabled' if b.disabled else 'enabled'})" for b in distinct
    )
    parts = [
        "JSON-LD availability: " + (" / ".join(tokens) or "none"),
        "add to cart button: " + (shown or "absent"),
        "notify-me button: " + ("shown" if notify_shown else "hidden" if notify else "absent"),
    ]
    if conflict:
        parts.append("signals conflict")
    return value, "; ".join(parts)


# Bundles and small helpers


def _bundle_evidence(title: str) -> tuple[str, ...]:
    evidence = [
        f"title term {term}: {clean_text(match.group(0))}"
        for term, pattern in BUNDLE_TITLE_TERMS
        for match in pattern.finditer(title)
    ]
    return tuple(dict.fromkeys(evidence))


def _digits(values: list[str]) -> str:
    return next((value for value in values if value.isdigit()), "")


def _first(values: list[str]) -> str:
    return values[0] if values else ""


# HTML parsing

_KINDS = ("sale", "compare", "plain")


@dataclass
class _PriceBlock:
    marked: list[tuple[str, str]] = field(default_factory=list)
    plain: list[str] = field(default_factory=list)

    def amounts(self) -> list[tuple[str, str]]:
        """``<del>``/``<ins>`` amounts, plus the block's bare text as one plain amount."""
        bare = clean_text("".join(self.plain))
        return self.marked + ([("plain", bare)] if bare else [])


@dataclass
class _Element:
    tag: str
    classes: frozenset[str]
    attrs: dict[str, str]
    # Context inherited from ancestors, so no stack scan is needed per tag.
    template: bool = False
    main: bool = False
    description: bool = False
    hidden: bool = False
    price_role: str = ""
    buffer: list[str] | None = None
    on_close: Callable[[str], None] | None = None


class _ProductPageParser(HTMLParser):
    """Collects only the main product's markup.

    "Main" means inside the product info container (``.t4s-product__info-container``); the full
    description is read from the Description tab (``.t4s-tab-content`` with an id starting
    ``t4s-tab-des``). Everything inside ``<template>`` (such as the sticky add-to-cart copy),
    header and recommendation product cards, and hidden text is excluded.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Element] = []
        self.jsonld_blocks: list[str] = []
        self.saw_main = False
        self.titles: list[str] = []
        self.summary = ""
        self.vendors: list[str] = []
        self.types: list[str] = []
        self.categories: list[str] = []
        self.skus: list[str] = []
        self.product_ids: list[str] = []
        self.form_variant_ids: list[str] = []
        self.price_blocks: list[_PriceBlock] = []
        self.buttons: list[_Button] = []
        self.notify: list[bool] = []
        self.variant_ids = 0
        self.variant_markup = False
        self.description_rows: list[tuple[str, str]] = []
        self.canonical = ""
        self._block: _PriceBlock | None = None
        self._row: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = frozenset(attributes.get("class", "").split())
        parent = self.stack[-1] if self.stack else None
        element = _Element(
            tag,
            classes,
            attributes,
            template=tag == "template" or bool(parent and parent.template),
            main="t4s-product__info-container" in classes or bool(parent and parent.main),
            description=(
                "t4s-tab-content" in classes and attributes.get("id", "").startswith("t4s-tab-des")
            )
            or bool(parent and parent.description),
            hidden=bool(classes & _HIDDEN_CLASSES)
            or "hidden" in attributes
            or "t4s-badge-price" in classes
            or bool(parent and parent.hidden),
            price_role=(
                "sale" if tag == "ins" else "compare" if tag == "del" else ""
            ) or (parent.price_role if parent else ""),
        )
        if tag not in _VOID_TAGS:
            self.stack.append(element)
        if element.template:
            return
        if tag == "br":
            self.handle_data("\n")
        elif tag == "link" and "canonical" in attributes.get("rel", "").split():
            self.canonical = self.canonical or attributes.get("href", "").strip()
        elif tag == "script" and attributes.get("type", "") == "application/ld+json":
            self._capture(element, self.jsonld_blocks.append)
        elif element.main:
            self.saw_main = True
            self._main_tag(element)
        elif element.description:
            self._description_tag(element)

    def _main_tag(self, element: _Element) -> None:
        tag, classes, attributes = element.tag, element.classes, element.attrs
        if attributes.get("data-has-only-default-variant") == "false":
            self.variant_markup = True
        if tag == "h1" and "t4s-product__title" in classes:
            self._capture(element, lambda text: self.titles.append(clean_text(text)))
        elif "metafield-rich_text_field" in classes:
            self._capture(element, self._set_summary)
        elif tag == "div" and "t4s-product-price" in classes and self._block is None:
            block = _PriceBlock()
            self.price_blocks.append(block)
            self._block = block
            element.on_close = self._end_price_block
        elif self._block is not None and tag in {"ins", "del"} and not element.hidden:
            block, role = self._block, element.price_role
            self._capture(element, lambda text: block.marked.append((role, clean_text(text))))
        elif tag == "button" and "t4s-product-form__submit" in classes:
            disabled = "disabled" in attributes or attributes.get("aria-disabled") == "true"
            self._capture(
                element, lambda text: self.buttons.append(_Button(clean_text(text), disabled))
            )
        elif tag == "button" and "t4s-pr__notify-stock" in classes:
            self.notify.append(not element.hidden)
        elif tag == "input" and attributes.get("name") == "product-id":
            self.product_ids.append(attributes.get("value", "").strip())
        elif tag == "input" and attributes.get("name") == "id":
            self.variant_ids += 1
            self.variant_markup = self.variant_markup or self.variant_ids > 1
            self.form_variant_ids.append(attributes.get("value", "").strip())
        elif tag == "select" and attributes.get("name") == "id":
            self.variant_markup = True
        elif "t4s-vendor-value" in classes:
            self._capture(element, lambda text: self.vendors.append(clean_text(text)))
        elif "t4s-type-value" in classes:
            self._capture(element, lambda text: self.types.append(clean_text(text)))
        elif "data-product__sku-number" in attributes:
            self._capture(element, lambda text: self.skus.append(clean_text(text)))
        elif tag == "a" and any(
            "t4s-collections-wrapper" in e.classes for e in reversed(self.stack)
        ):
            self._capture(element, lambda text: self.categories.append(clean_text(text)))

    def _description_tag(self, element: _Element) -> None:
        if element.tag == "tr":
            self._row = []
            element.on_close = self._finish_row
        elif element.tag in {"td", "th"} and self._row is not None:
            row = self._row
            self._capture(element, lambda text: row.append(clean_text(text)))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                if tag in _LINE_END_TAGS:
                    self.handle_data("\n")
                # Close this element and any unclosed elements nested inside it.
                while len(self.stack) > index:
                    closed = self.stack.pop()
                    if closed.on_close is not None:
                        closed.on_close("".join(closed.buffer or []))
                return

    def handle_data(self, data: str) -> None:
        if not self.stack:
            return
        innermost = self.stack[-1]
        if innermost.tag in {"script", "style"}:
            # Script text only feeds its own capture (JSON-LD), never the visible text.
            if innermost.buffer is not None:
                innermost.buffer.append(data)
            return
        if innermost.hidden or innermost.template:
            return
        if self._block is not None and innermost.main and not innermost.price_role:
            self._block.plain.append(data)
        for element in self.stack:
            if element.buffer is not None:
                element.buffer.append(data)

    def _end_price_block(self, _text: str) -> None:
        self._block = None

    def _set_summary(self, text: str) -> None:
        self.summary = self.summary or text

    def _capture(self, element: _Element, sink: Callable[[str], None]) -> None:
        element.buffer = []
        element.on_close = sink

    def _finish_row(self, _text: str) -> None:
        cells, self._row = self._row or [], None
        cells = [cell for cell in cells if cell]
        if cells:
            self.description_rows.append((cells[0], " ".join(cells[1:])))
