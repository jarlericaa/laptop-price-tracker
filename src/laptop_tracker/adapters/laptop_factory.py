"""Laptop Factory (retailer code LF) product-page adapter (overall Phase 7).

Parses one already-fetched Laptop Factory product page into one ``ParsedOffer``. It makes no
HTTP requests; the shared ``Fetcher`` does all retrieval. Live use is enabled under the
project-owner policy in docs/DECISIONS.md. The retailer's published permission remains
unverified; see docs/RETAILER_DATA_USE_REVIEW.md. Page assumptions, versioning, and limitations
are in docs/adapters/LAPTOP_FACTORY.md.

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
    Availability,
    ObservationValidationError,
    OfferDetails,
    PriceType,
    clean_text,
    parse_price,
    url_host,
)
from ..scraping import FetchedPage, ParsedOffer, ParseError, ScrapeRequest

RETAILER_ID = "laptop_factory"
RETAILER_CODE = "LF"
HOST = "laptopfactory.com.ph"
# Project owner authorizes internal collection under the assumption recorded in DECISIONS.md.
# The retailer's own policy remains unverified; OWNER_APPROVED does not mean CLEAR.
DATA_USE_REVIEW_STATUS = "OWNER_APPROVED"

PHP_SYMBOLS = frozenset({"₱", "PHP"})

_JSONLD_AVAILABILITY = {
    "instock": Availability.IN_STOCK,
    "outofstock": Availability.OUT_OF_STOCK,
    "soldout": Availability.OUT_OF_STOCK,
    "preorder": Availability.PREORDER,
}
_STOCK_WORDING = (
    (re.compile(r"(?:[0-9]+ )?in stock", re.I), Availability.IN_STOCK),
    (re.compile(r"out of stock", re.I), Availability.OUT_OF_STOCK),
)
# Title wording that suggests a laptop bundle. Matches are flagged for downstream review; the
# offer is never dropped. Word-internal matches (for example ERGOCHAIR) are intended.
_TOKEN = r"[^\s()\[\],]*"
BUNDLE_TITLE_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("FREE", re.compile(r"(?:\+\s*)?\bFREE\b", re.I)),
    *(
        (term, re.compile(_TOKEN + term + _TOKEN, re.I))
        for term in ("BUNDLE", "MONITOR", "TABLET", "CHAIR")
    ),
)
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
     "track", "wbr"}
)


def product_request(url: str) -> ScrapeRequest:
    """Build a request for one Laptop Factory product page (``/product/...`` on ``HOST``)."""
    if url_host(url) != HOST or not urlsplit(url).path.startswith("/product/"):
        raise ValueError(f"Not a Laptop Factory product URL on {HOST}.")
    return ScrapeRequest(RETAILER_ID, url)


class LaptopFactoryAdapter:
    retailer_id = RETAILER_ID
    retailer_code = RETAILER_CODE
    # Bump when parsing rules change; recorded in every observation's source metadata.
    adapter_version = "lf-1"
    allowed_hosts = frozenset({HOST})
    live_use_approved = True  # Project-owner policy; retailer permission is not independently verified.

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
        title = _text(product.get("name")) if product else ""
        title = title or (parsed.titles[0] if parsed.titles else "")
        if not title:
            raise ParseError("TITLE_NOT_FOUND", "The product title could not be found.")
        offer = _single_offer(product)
        price = _resolve_price(offer, parsed.price_blocks)
        availability, evidence = _resolve_availability(offer, parsed)
        specs = parsed.short_description_rows or parsed.description_tab_rows
        raw_configuration = " | ".join(
            f"{label}: {value}" if value else label for label, value in specs
        ) or (_text(product.get("description")) if product else "")
        bundle_evidence = _bundle_evidence(title, parsed.bundle_components, parsed.has_bundle)
        details = OfferDetails(
            retailer_product_id=parsed.product_id or _digits(parsed.cart_values),
            retailer_sku=(_text(product.get("sku")) if product else "") or _first(parsed.skus),
            brand=_brand(product),
            manufacturer_part_number=_text(product.get("mpn")) if product else "",
            price_type=price.price_type,
            regular_price=price.regular,
            regular_price_text=price.regular_text,
            sale_price=price.current if price.price_type is PriceType.SALE else None,
            sale_price_text=price.current_text if price.price_type is PriceType.SALE else "",
            raw_specifications=tuple(specs),
            raw_categories=tuple(parsed.categories),
            bundle_flag=bool(bundle_evidence),
            bundle_evidence=bundle_evidence,
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
    """One concrete Offer with a price, or None (absent, AggregateOffer, or several offers)."""
    if not product:
        return None
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if len(offers) == 1 else None
    if not isinstance(offers, dict) or "Offer" not in _types(offers):
        return None
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
    """Brand only when JSON-LD states it; never guessed from categories or the title."""
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
    regular: Decimal
    regular_text: str
    price_type: PriceType


def _resolve_price(offer: dict | None, blocks: list[_PriceBlock]) -> _Price:
    shown = _visible_price(blocks)
    ld_current = ld_regular = None
    ld_current_text = ld_regular_text = ""
    if offer is not None:
        currency = offer.get("priceCurrency")
        if currency is not None and currency != "PHP":
            raise ParseError("UNSUPPORTED_CURRENCY", "JSON-LD priceCurrency is not PHP.")
        if "price" in offer:
            ld_current_text = _text(offer.get("price"))
            ld_current = _price_value(ld_current_text, "JSON-LD price")
        specs = offer.get("priceSpecification", [])
        for spec in specs if isinstance(specs, list) else [specs]:
            if isinstance(spec, dict) and str(spec.get("priceType", "")).endswith("ListPrice"):
                if spec.get("priceCurrency", "PHP") != "PHP":
                    raise ParseError("UNSUPPORTED_CURRENCY", "JSON-LD list price is not PHP.")
                ld_regular_text = _text(spec.get("price"))
                ld_regular = _price_value(ld_regular_text, "JSON-LD list price")

    if shown is None and ld_current is None:
        raise ParseError("PRICE_NOT_FOUND", "No current selling price was found.")
    if shown is not None and ld_current is not None and shown.current != ld_current:
        raise ParseError(
            "PRICE_CONFLICT", "Displayed current price and JSON-LD price disagree."
        )
    current, current_text = (
        (shown.current, shown.current_text) if shown is not None else (ld_current, ld_current_text)
    )
    regular, regular_text = (
        (shown.regular, shown.regular_text)
        if shown is not None and shown.regular is not None
        else (ld_regular, ld_regular_text)
    )
    if shown is not None and shown.regular is not None and ld_regular is not None:
        if shown.regular != ld_regular:
            raise ParseError(
                "PRICE_CONFLICT", "Displayed regular price and JSON-LD list price disagree."
            )
    if regular is None or regular == current:
        return _Price(current, current_text, current, current_text, PriceType.REGULAR)
    if regular < current:
        raise ParseError("PRICE_CONFLICT", "The regular price is below the current price.")
    return _Price(current, current_text, regular, regular_text, PriceType.SALE)


@dataclass(frozen=True)
class _ShownPrice:
    current: Decimal
    current_text: str
    regular: Decimal | None
    regular_text: str


def _visible_price(blocks: list[_PriceBlock]) -> _ShownPrice | None:
    """The main product price block: ``<del>`` is the regular price, ``<ins>`` the current."""
    blocks = [block for block in blocks if block.amounts]
    if not blocks:
        return None
    if len(blocks) > 1:
        raise ParseError("AMBIGUOUS_PRICE", "More than one product price is displayed.")
    block = blocks[0]
    if any(symbol not in PHP_SYMBOLS for symbol in block.symbols):
        raise ParseError("UNSUPPORTED_CURRENCY", "The displayed currency is not the peso.")
    if not block.symbols:
        raise ParseError("CURRENCY_NOT_FOUND", "The displayed price has no currency symbol.")
    by_kind = {kind: [text for k, text in block.amounts if k == kind] for kind in _AMOUNT_KINDS}
    inserted, deleted, plain = by_kind["ins"], by_kind["del"], by_kind["plain"]
    if inserted and len(inserted) == 1 and len(deleted) <= 1 and not plain:
        regular_text = deleted[0] if deleted else ""
        return _ShownPrice(
            _price_value(inserted[0], "displayed sale price"),
            inserted[0],
            _price_value(regular_text, "displayed regular price") if deleted else None,
            regular_text,
        )
    if not inserted and not deleted and len(plain) == 1:
        return _ShownPrice(_price_value(plain[0], "displayed price"), plain[0], None, "")
    raise ParseError(
        "AMBIGUOUS_PRICE", "The current selling price cannot be identified unambiguously."
    )


def _price_value(text: str, what: str) -> Decimal:
    try:
        return parse_price(text)
    except ObservationValidationError:
        raise ParseError("INVALID_PRICE", f"The {what} is not a single valid amount.") from None


# Availability


def _resolve_availability(offer: dict | None, parsed: _ProductPageParser) -> tuple[str, str]:
    """Combine stock wording, JSON-LD availability, and Add to Cart state.

    Stock wording and JSON-LD are the explicit signals. Add to Cart must agree with them. Any
    disagreement, or no explicit signal at all, gives UNKNOWN; price visibility is never used.
    """
    stock_texts = list(dict.fromkeys(parsed.stock_texts))
    conflict = len(stock_texts) > 1
    stock = _stock_value(stock_texts[0]) if len(stock_texts) == 1 else None
    ld_text = _text(offer.get("availability")) if offer else ""
    ld = _JSONLD_AVAILABILITY.get(ld_text.rstrip("/").rsplit("/", 1)[-1].lower(), None)
    if ld_text and ld is None:
        ld = Availability.UNKNOWN
    cart = parsed.cart_state()

    explicit = [value for value in (stock, ld) if value is not None]
    value = Availability.UNKNOWN
    if explicit and not conflict and len(set(explicit)) == 1:
        value = explicit[0]
        cart_ok = {
            Availability.IN_STOCK: cart == "enabled",
            Availability.PREORDER: cart == "enabled",
            Availability.OUT_OF_STOCK: cart in {"absent", "disabled"},
            Availability.UNKNOWN: True,
        }[value]
        if not cart_ok:
            value, conflict = Availability.UNKNOWN, True
    elif len(set(explicit)) > 1:
        conflict = True

    parts = [
        "stock text: " + (" / ".join(stock_texts) if stock_texts else "none"),
        "JSON-LD availability: " + (ld_text or "none"),
        f"add to cart: {cart}",
    ]
    if conflict:
        parts.append("signals conflict")
    return value, "; ".join(parts)


def _stock_value(text: str) -> Availability:
    for pattern, value in _STOCK_WORDING:
        if pattern.fullmatch(text):
            return value
    return Availability.UNKNOWN  # Includes backorder and unrecognized wording.


# Bundles and small helpers


def _bundle_evidence(title: str, components: list[str], has_bundle: bool) -> tuple[str, ...]:
    evidence = [
        f"title term {term}: {clean_text(match.group(0))}"
        for term, pattern in BUNDLE_TITLE_TERMS
        for match in pattern.finditer(title)
    ]
    if has_bundle:
        evidence.append(
            "bundle components: " + " ; ".join(components)
            if components
            else "bundle component markup present"
        )
    return tuple(dict.fromkeys(evidence))


def _digits(values: list[str]) -> str:
    return next((value for value in values if value.isdigit()), "")


def _first(values: list[str]) -> str:
    return values[0] if values else ""


# HTML parsing

_AMOUNT_KINDS = ("del", "ins", "plain")


@dataclass
class _PriceBlock:
    amounts: list[tuple[str, str]] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)


@dataclass
class _Element:
    tag: str
    classes: frozenset[str]
    attrs: dict[str, str]
    buffer: list[str] | None = None
    on_close: Callable[[str], None] | None = None


class _ProductPageParser(HTMLParser):
    """Collects only the main product's markup, scoped to the product summary.

    Related products, bundle-component rows, and screen-reader text are excluded from price,
    stock, and cart signals.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Element] = []
        self.jsonld_blocks: list[str] = []
        self.titles: list[str] = []
        self.price_blocks: list[_PriceBlock] = []
        self.stock_texts: list[str] = []
        self.cart_values: list[str] = []
        self.cart_buttons: list[bool] = []  # True when disabled
        self.short_description_rows: list[tuple[str, str]] = []
        self.description_tab_rows: list[tuple[str, str]] = []
        self.skus: list[str] = []
        self.categories: list[str] = []
        self.bundle_components: list[str] = []
        self.has_bundle = False
        self.product_id = ""
        self.canonical = ""
        self._block: _PriceBlock | None = None
        self._row: list[str] | None = None

    def cart_state(self) -> str:
        if not self.cart_buttons:
            return "absent"
        return "disabled" if any(self.cart_buttons) else "enabled"

    def _inside(self, *, classes: set[str] = frozenset(), ids: set[str] = frozenset()) -> bool:
        return any(
            element.classes & classes or element.attrs.get("id", "") in ids
            for element in self.stack
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = frozenset(attributes.get("class", "").split())
        element = _Element(tag, classes, attributes)
        in_summary = self._inside(classes={"summary", "entry-summary"})
        in_bundle = self._inside(classes={"woosb-wrap"})
        main = in_summary and not in_bundle

        if tag == "br":
            self.handle_data("\n")
        elif tag == "link" and "canonical" in attributes.get("rel", "").split():
            self.canonical = self.canonical or attributes.get("href", "").strip()
        elif tag == "script" and attributes.get("type", "") == "application/ld+json":
            self._capture(element, self.jsonld_blocks.append)
        elif tag == "div" and re.fullmatch(r"product-[0-9]+", attributes.get("id", "")):
            self.product_id = self.product_id or attributes["id"].split("-", 1)[1]
        if "woosb-wrap" in classes:
            self.has_bundle = True
        if "woosb-product" in classes and attributes.get("data-name", "").strip():
            self.bundle_components.append(clean_text(attributes["data-name"]))

        if main and tag == "h1" and "product_title" in classes:
            self._capture(element, lambda text: self.titles.append(clean_text(text)))
        elif main and tag == "p" and "price" in classes and self._block is None:
            block = _PriceBlock()
            self.price_blocks.append(block)
            self._block = block
            element.on_close = self._end_price_block
        elif self._block is not None and "woocommerce-Price-amount" in classes:
            kind = next(
                (k for k in ("del", "ins") if any(e.tag == k for e in self._stack_in_block())),
                "plain",
            )
            block = self._block
            self._capture(element, lambda text: block.amounts.append((kind, clean_text(text))))
        elif self._block is not None and "woocommerce-Price-currencySymbol" in classes:
            block = self._block
            self._capture(element, lambda text: block.symbols.append(clean_text(text)))
        elif main and tag == "p" and "stock" in classes:
            self._capture(element, self._add_stock_text)
        elif main and tag == "button" and "single_add_to_cart_button" in classes:
            disabled = "disabled" in attributes or "disabled" in classes
            self.cart_buttons.append(disabled)
            self.cart_values.append(attributes.get("value", "").strip())
        elif main and tag == "span" and "sku" in classes:
            self._capture(element, lambda text: self.skus.append(clean_text(text)))
        elif main and tag == "a" and attributes.get("rel") == "tag" and self._inside(
            classes={"posted_in"}
        ):
            self._capture(element, lambda text: self.categories.append(clean_text(text)))
        elif tag == "tr" and self._spec_table_source() is not None:
            self._row = []
            source = self._spec_table_source()
            element.on_close = lambda _text: self._finish_row(source)
        elif tag in {"td", "th"} and self._row is not None:
            row = self._row
            self._capture(element, lambda text: row.append(clean_text(text)))

        if tag not in _VOID_TAGS:
            self.stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                # Close this element and any unclosed elements nested inside it.
                while len(self.stack) > index:
                    closed = self.stack.pop()
                    if closed.on_close is not None:
                        closed.on_close("".join(closed.buffer or []))
                return

    def handle_data(self, data: str) -> None:
        if self._inside(classes={"screen-reader-text"}):
            return
        for element in self.stack:
            if element.buffer is not None:
                element.buffer.append(data)

    def _end_price_block(self, _text: str) -> None:
        self._block = None

    def _add_stock_text(self, text: str) -> None:
        # Blank stock markup is not explicit wording, so it is not a signal.
        if clean_text(text):
            self.stock_texts.append(clean_text(text))

    def _capture(self, element: _Element, sink: Callable[[str], None]) -> None:
        element.buffer = []
        element.on_close = sink

    def _stack_in_block(self) -> list[_Element]:
        """Open elements inside the current price block's ``<p>``."""
        for index in range(len(self.stack) - 1, -1, -1):
            element = self.stack[index]
            if element.tag == "p" and "price" in element.classes:
                return self.stack[index + 1 :]
        return []

    def _spec_table_source(self) -> str | None:
        if not any(element.tag == "table" for element in self.stack):
            return None
        if self._inside(classes={"woosb-wrap"}):
            return None
        if self._inside(
            classes={"product-short-description", "woocommerce-product-details__short-description"}
        ):
            return "short"
        if self._inside(classes={"woocommerce-Tabs-panel--description"}, ids={"tab-description"}):
            return "tab"
        return None

    def _finish_row(self, source: str | None) -> None:
        cells, self._row = self._row or [], None
        cells = [cell for cell in cells if cell]
        if not cells:
            return
        pair = (cells[0], " ".join(cells[1:]))
        target = self.short_description_rows if source == "short" else self.description_tab_rows
        target.append(pair)
