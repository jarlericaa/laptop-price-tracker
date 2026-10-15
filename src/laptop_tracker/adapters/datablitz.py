"""DataBlitz (retailer code DBZ) product-page adapter (overall Phase 7).

Parses one already-fetched DataBlitz product page into one ``ParsedOffer``. It makes no HTTP
requests; the shared ``Fetcher`` does all retrieval. Live use is enabled under the
project-owner policy in docs/DECISIONS.md. The retailer's published permission remains
unverified; see docs/RETAILER_DATA_USE_REVIEW.md. Page assumptions, versioning, and limitations
are in docs/adapters/DATABLITZ.md.

The adapter preserves retailer text. It does not infer laptop specifications, assign target
IDs, or decide whether an offer matches a tracked configuration (Phase 8).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ..offers import (
    IDENTIFIER,
    PRODUCT_DESCRIPTION,
    Availability,
    ObservationValidationError,
    OfferDetails,
    PriceType,
    clean_text,
    parse_price,
    url_host,
)
from ..scraping import FetchedPage, ParsedOffer, ParseError, ScrapeRequest

RETAILER_ID = "datablitz"
RETAILER_CODE = "DBZ"
# The Shopify storefront. datablitz.com.ph and www.datablitz.com.ph redirect here (checked
# 2026-10-01); they are not allowed, so a request must name this host directly.
HOST = "ecommerce.datablitz.com.ph"
# Project owner authorizes internal collection under the assumption recorded in DECISIONS.md.
# The retailer's own policy remains unverified; OWNER_APPROVED does not mean CLEAR.
DATA_USE_REVIEW_STATUS = "OWNER_APPROVED"

_JSONLD_AVAILABILITY = {
    "instock": Availability.IN_STOCK,
    "outofstock": Availability.OUT_OF_STOCK,
    "soldout": Availability.OUT_OF_STOCK,
    "preorder": Availability.PREORDER,
}
# Add-to-cart button labels of the storefront theme. "Add to cart" is not stock wording, so it
# only confirms another signal; the other labels state availability explicitly.
_BUTTON_LABELS: dict[str, Availability | None] = {
    "add to cart": None,
    "sold out": Availability.OUT_OF_STOCK,
    "pre-order": Availability.PREORDER,
    "preorder": Availability.PREORDER,
    "unavailable": Availability.UNKNOWN,
}
# Title wording that suggests a laptop bundle or freebie. Matches are flagged for downstream
# review; the offer is never dropped.
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
_FOREIGN_CURRENCY = re.compile(r"[$€£¥]|\b(?!PHP\b)[A-Z]{3}\b")


def product_request(url: str) -> ScrapeRequest:
    """Build a request for one DataBlitz product page (``/products/<handle>`` on ``HOST``)."""
    path = urlsplit(url).path if url_host(url) == HOST else ""
    if not re.fullmatch(r"/products/[^/]+/?", path):
        raise ValueError(f"Not a DataBlitz product URL on {HOST}.")
    return ScrapeRequest(RETAILER_ID, url)


class DataBlitzAdapter:
    retailer_id = RETAILER_ID
    retailer_code = RETAILER_CODE
    # Bump when parsing rules change; recorded in every observation's source metadata.
    # dbz-2 (2026-10-13): adds description prose and identifier provenance as scoped evidence.
    # dbz-3: preserve visible paragraph locators for approved explicit screen-size prose.
    adapter_version = "dbz-3"
    allowed_hosts = frozenset({HOST})
    # Project-owner policy; retailer permission is not independently verified.
    live_use_approved = True

    def parse(self, page: FetchedPage) -> list[ParsedOffer]:
        parsed = _ProductPageParser()
        parsed.feed(page.text())
        parsed.close()
        product = _single_product(parsed.jsonld_blocks)
        if product is None and not parsed.titles:
            raise ParseError(
                "UNRECOGNIZED_LAYOUT",
                "No Product JSON-LD and no product title found; the layout may have changed.",
            )
        title = (_text(product.get("name")) if product else "") or _first(parsed.titles)
        if not title:
            raise ParseError("TITLE_NOT_FOUND", "The product title could not be found.")
        offer = _single_offer(product)
        price = _resolve_price(offer, parsed.price_blocks)
        availability, evidence = _resolve_availability(offer, parsed.buttons)
        specs = parsed.description_rows or _label_lines(parsed.short_description)
        raw_configuration = " | ".join(
            f"{label}: {value}" if value else label for label, value in specs
        ) or (_text(product.get("description")) if product else "")
        bundle_evidence = _bundle_evidence(title)
        category = _text(product.get("category")) if product else ""
        scoped = _description_evidence(parsed.description_paragraphs, product) + (
            _identifier_evidence(product, parsed)
        )
        details = OfferDetails(
            retailer_product_id=_digits(parsed.product_ids),
            retailer_sku=(_text(product.get("sku")) if product else "") or _first(parsed.skus),
            brand=_brand(product) or _first(parsed.vendors),
            manufacturer_part_number=_text(product.get("mpn")) if product else "",
            price_type=price.price_type,
            regular_price=price.regular,
            regular_price_text=price.regular_text,
            sale_price=price.current if price.price_type is PriceType.SALE else None,
            sale_price_text=price.current_text if price.price_type is PriceType.SALE else "",
            raw_specifications=tuple(specs),
            raw_categories=(category,) if category else (),
            bundle_flag=bool(bundle_evidence),
            bundle_evidence=bundle_evidence,
            scoped_evidence=scoped,
        )
        url = ""
        for candidate in (offer.get("url") if offer else None, product and product.get("url")):
            if isinstance(candidate, str) and candidate.strip():
                url = candidate.strip()
                break
        return [
            ParsedOffer(
                raw_title=title,
                price_text=price.current_text,
                currency="PHP",
                availability=availability,
                availability_evidence=evidence,
                raw_configuration_text=raw_configuration,
                offer_url=url or parsed.canonical,
                details=details,
            )
        ]


# JSON-LD


def _single_product(blocks: list[str]) -> dict | None:
    """The page's one schema.org Product node; several distinct products are ambiguous."""
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
        raise ParseError("AMBIGUOUS_PRODUCT", "The page describes more than one Product.")
    return products[0] if products else None


def _single_offer(product: dict | None) -> dict | None:
    """The one concrete Offer, or None when absent.

    Several offers (variants) or an AggregateOffer cannot be tied to one displayed price, so
    they are ambiguous rather than guessed.
    """
    offers = product.get("offers") if product else None
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


def _types(node: dict) -> list[str]:
    kind = node.get("@type", [])
    return [kind] if isinstance(kind, str) else [k for k in kind if isinstance(k, str)]


def _text(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int, Decimal)):
        return clean_text(str(value))
    return ""


def _brand(product: dict | None) -> str:
    """Brand only when JSON-LD states it; never guessed from the title."""
    if not product:
        return ""
    brand = product.get("brand")
    if isinstance(brand, dict):
        return _text(brand.get("name"))
    return _text(brand)


# Price


@dataclass(frozen=True)
class _Price:
    current: Decimal
    current_text: str
    regular: Decimal | None
    regular_text: str
    price_type: PriceType | None


def _resolve_price(offer: dict | None, blocks: list[_PriceBlock]) -> _Price:
    shown = _visible_price(blocks)
    ld_current = None
    ld_current_text = ""
    currency = offer.get("priceCurrency") if offer else None
    if currency not in (None, "") and currency != "PHP":
        raise ParseError("UNSUPPORTED_CURRENCY", "JSON-LD priceCurrency is not PHP.")
    if offer is not None and "price" in offer:
        ld_current_text = _text(offer.get("price"))
        ld_current = _price_value(ld_current_text, "JSON-LD price")

    if shown is None:
        if ld_current is None:
            raise ParseError("PRICE_NOT_FOUND", "No current selling price was found.")
        if currency != "PHP":
            raise ParseError("CURRENCY_NOT_FOUND", "JSON-LD price has no PHP priceCurrency.")
        # JSON-LD has no compare-at price, so sale versus regular is not known.
        return _Price(ld_current, ld_current_text, None, "", None)
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
    """The main product's ``.price-list``.

    A lone ``span.price`` is the current price. On sale, ``price--highlight`` is the current
    price and ``price--compare`` the regular price. Anything else is ambiguous. Text without
    digits (for example "Call for price") is not an amount.
    """
    amounts = [
        [(kind, text) for kind, text in block.amounts if re.search(r"[0-9]", text)]
        for block in blocks
    ]
    amounts = [block for block in amounts if block]
    if not amounts:
        return None
    if len(amounts) > 1:
        raise ParseError("AMBIGUOUS_PRICE", "More than one product price is displayed.")
    kinds = {kind: [text for k, text in amounts[0] if k == kind] for kind in _KINDS}
    highlight, compare, plain = kinds["highlight"], kinds["compare"], kinds["plain"]
    if len(plain) == 1 and not highlight and not compare:
        return _ShownPrice(_amount(plain[0], "displayed price"), plain[0], None, "")
    if len(highlight) == 1 and len(compare) == 1 and not plain:
        return _ShownPrice(
            _amount(highlight[0], "displayed sale price"),
            highlight[0],
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


def _resolve_availability(offer: dict | None, buttons: list[_Button]) -> tuple[str, str]:
    """Combine JSON-LD availability with the main add-to-cart button.

    JSON-LD and the "Sold out", "Pre-order", and "Unavailable" labels are explicit signals and
    must agree. The button must also be consistent: enabled "Add to cart" for in stock, enabled
    "Pre-order" or "Add to cart" for preorder, absent or disabled or "Sold out" for out of
    stock. Disagreement, several different buttons, or no explicit signal gives UNKNOWN; price
    visibility and an "Add to cart" label alone never decide stock.
    """
    ld_text = _text(offer.get("availability")) if offer else ""
    ld = _JSONLD_AVAILABILITY.get(ld_text.rstrip("/").rsplit("/", 1)[-1].lower())
    if ld_text and ld is None:
        ld = Availability.UNKNOWN  # Includes BackOrder, LimitedAvailability, and the like.
    distinct = list(dict.fromkeys(buttons))
    conflict = len(distinct) > 1
    button = distinct[0] if len(distinct) == 1 else None
    label = button.label.lower() if button else ""
    from_label = _BUTTON_LABELS.get(label, Availability.UNKNOWN) if button else None

    explicit = [value for value in (ld, from_label) if value is not None]
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
        "JSON-LD availability: " + (ld_text or "none"),
        "add to cart button: " + (shown or "absent"),
    ]
    if conflict:
        parts.append("signals conflict")
    return value, "; ".join(parts)


# Specifications, bundles, and small helpers


def _label_lines(text: str) -> list[tuple[str, str]]:
    """Short-description lines such as ``PROCESSOR: AMD Ryzen 5`` as (label, value) pairs."""
    rows = []
    for line in text.split("\n"):
        line = clean_text(line)
        if line:
            label, colon, value = line.partition(":")
            rows.append((clean_text(label), clean_text(value)) if colon else (line, ""))
    return rows


def _description_evidence(
    paragraphs: list[str], product: dict | None
) -> tuple[tuple[str, str, str], ...]:
    """Description prose kept as PRODUCT_DESCRIPTION evidence, never as specification rows.

    The visible paragraphs outside the specification table and the JSON-LD
    ``Product.description`` lead paragraph (before its copied specification table) are
    retained word for word so a reviewer can see, for example, a stated screen size whose
    specification row has no unit. An explicit product screen diagonal may supply inches
    under the owner-approved prose rule; other prose remains supporting evidence.
    """
    items = [
        (PRODUCT_DESCRIPTION, f"product description > p[{index}] (outside tables)", text)
        for index, paragraph in enumerate(paragraphs, 1)
        for text in [clean_text(paragraph)]
        # A bare heading such as "Specifications:" introduces the table; it is not prose.
        if text and not re.fullmatch(r"[A-Za-z ]{1,40}:", text)
    ]
    description = _text(product.get("description")) if product else ""
    if product and isinstance(product.get("description"), str):
        lead = re.split(r"\n\s*\n|\bSpecifications\s*:", product["description"])[0]
        if clean_text(lead):
            items.append((PRODUCT_DESCRIPTION, "JSON-LD Product.description", clean_text(lead)))
    elif description:
        items.append((PRODUCT_DESCRIPTION, "JSON-LD Product.description", description))
    return tuple(items)


def _identifier_evidence(
    product: dict | None, parsed: _ProductPageParser
) -> tuple[tuple[str, str, str], ...]:
    """Identifiers with the place each was read from; their role is not verified here."""
    items = []
    if product:
        for key in ("sku", "gtin", "gtin13", "mpn", "productID"):
            value = _text(product.get(key))
            if value:
                items.append((IDENTIFIER, f"JSON-LD Product.{key}", value))
    for sku in dict.fromkeys(parsed.skus):
        if sku:
            items.append((IDENTIFIER, "visible SKU", sku))
    for product_id in dict.fromkeys(parsed.product_ids):
        if product_id:
            items.append((IDENTIFIER, "product form product-id", product_id))
    return tuple(items)


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

_KINDS = ("highlight", "compare", "plain")


@dataclass
class _PriceBlock:
    amounts: list[tuple[str, str]] = field(default_factory=list)


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
    buffer: list[str] | None = None
    on_close: Callable[[str], None] | None = None


class _ProductPageParser(HTMLParser):
    """Collects only the main product's markup.

    "Main" means inside the main product form (``form#product-form-main-…``); the full
    description is read from the description card. Everything inside ``<template>`` (the
    quick-buy modal copy), recommendation sections, and visually hidden text is excluded.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Element] = []
        self.jsonld_blocks: list[str] = []
        self.titles: list[str] = []
        self.vendors: list[str] = []
        self.skus: list[str] = []
        self.product_ids: list[str] = []
        self.price_blocks: list[_PriceBlock] = []
        self.buttons: list[_Button] = []
        self.short_description = ""
        self.description_rows: list[tuple[str, str]] = []
        # Visible description paragraphs outside any table (prose, not specification rows).
        self.description_paragraphs: list[str] = []
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
            main=(tag == "form" and attributes.get("id", "").startswith("product-form-main-"))
            or bool(parent and parent.main),
            description="product-block-list__item--description" in classes
            or bool(parent and parent.description),
            hidden="visually-hidden" in classes
            or (tag == "a" and attributes.get("href", "").startswith("#"))
            or bool(parent and parent.hidden),
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
            self._main_tag(element)
        elif element.description:
            self._description_tag(element)

    def _main_tag(self, element: _Element) -> None:
        tag, classes, attributes = element.tag, element.classes, element.attrs
        if tag == "h1" and "product-meta__title" in classes:
            self._capture(element, lambda text: self.titles.append(clean_text(text)))
        elif tag == "a" and "product-meta__vendor" in classes:
            self._capture(element, lambda text: self.vendors.append(clean_text(text)))
        elif "product-meta__sku-number" in classes:
            self._capture(element, lambda text: self.skus.append(clean_text(text)))
        elif tag == "input" and attributes.get("name") == "product-id":
            self.product_ids.append(attributes.get("value", "").strip())
        elif "price-list" in classes and self._block is None:
            block = _PriceBlock()
            self.price_blocks.append(block)
            self._block = block
            element.on_close = self._end_price_block
        elif self._block is not None and tag == "span" and "price" in classes:
            kind = (
                "highlight" if "price--highlight" in classes
                else "compare" if "price--compare" in classes
                else "plain"
            )
            block = self._block
            self._capture(element, lambda text: block.amounts.append((kind, clean_text(text))))
        elif tag == "button" and "product-form__add-button" in classes:
            disabled = "disabled" in attributes or "button--disabled" in classes
            self._capture(
                element, lambda text: self.buttons.append(_Button(clean_text(text), disabled))
            )
        elif "product-meta__short-description" in classes:
            self._capture(element, self._set_short_description)

    def _description_tag(self, element: _Element) -> None:
        if element.tag == "p" and not any(e.tag in {"table", "p"} for e in self.stack[:-1]):
            self._capture(element, self.description_paragraphs.append)
        elif element.tag == "tr":
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
        for element in self.stack:
            if element.buffer is not None:
                element.buffer.append(data)

    def _end_price_block(self, _text: str) -> None:
        self._block = None

    def _set_short_description(self, text: str) -> None:
        self.short_description = self.short_description or text

    def _capture(self, element: _Element, sink: Callable[[str], None]) -> None:
        element.buffer = []
        element.on_close = sink

    def _finish_row(self, _text: str) -> None:
        cells, self._row = self._row or [], None
        cells = [cell for cell in cells if cell]
        if cells:
            self.description_rows.append((cells[0], " ".join(cells[1:])))
