"""Octagon Computer Superstore (retailer code OCT) product-page adapter (overall Phase 7).

Parses one already-fetched Octagon product page into one ``ParsedOffer``. It makes no HTTP
requests; the shared ``Fetcher`` does all retrieval. Live use is enabled under the project-wide
owner decision for this internal tool. Octagon's published terms of service prohibit using the
site or its content to "spider, crawl, or scrape", so the external-policy finding is RESTRICTED;
the owner decision is not Octagon's consent. See docs/RETAILER_DATA_USE_REVIEW.md and
docs/DECISIONS.md. Page assumptions, versioning, and limitations are in docs/adapters/OCT.md.

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
from urllib.parse import parse_qs, urlsplit

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

RETAILER_ID = "oct"
RETAILER_CODE = "OCT"
# The Octagon Computer Superstore Shopify storefront (shop
# octagoncomputer-superstore.myshopify.com). The apex octagon.com.ph redirects here (HTTP 301,
# checked 2026-10-01); it is not allowed, so a request must name this host directly.
HOST = "www.octagon.com.ph"
# Project status: enabled under the project-wide owner decision of 2026-10-01. The retailer's
# external-policy finding (RESTRICTED) is recorded separately and is not retailer consent.
DATA_USE_REVIEW_STATUS = "OWNER_APPROVED"
# The storefront's own name. The theme puts it in JSON-LD ``brand`` and the analytics
# ``vendor`` of every reviewed product, so neither field is a laptop brand.
STORE_NAME = "Octagon Computer Superstore"

_JSONLD_AVAILABILITY = {
    "instock": Availability.IN_STOCK,
    "outofstock": Availability.OUT_OF_STOCK,
    "soldout": Availability.OUT_OF_STOCK,
    "preorder": Availability.PREORDER,
}
# "Multi" theme add-to-cart button labels (its ``soldOut`` and ``unavailable`` strings).
# "Add to cart" is not stock wording, so it only confirms another signal.
_BUTTON_LABELS: dict[str, Availability | None] = {
    "add to cart": None,
    "sold out": Availability.OUT_OF_STOCK,
    "pre-order": Availability.PREORDER,
    "preorder": Availability.PREORDER,
    "unavailable": Availability.UNKNOWN,
}
# Stock-alert wording. Anything else is kept as evidence and makes availability UNKNOWN.
_STOCK_ALERTS: tuple[tuple[re.Pattern[str], Availability], ...] = (
    (re.compile(r"only [0-9]+ left in stock!?", re.I), Availability.IN_STOCK),
    (re.compile(r"in stock!?", re.I), Availability.IN_STOCK),
    (re.compile(r"(?:sold out|out of stock)!?", re.I), Availability.OUT_OF_STOCK),
)
# Title wording that suggests a bundle or freebie. Matches are flagged for downstream review;
# the offer is never dropped. Same terms as the DataBlitz, PC Express, and PC Central adapters.
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
_CELL_TAGS = frozenset({"td", "th"})
_HIDDEN_CLASSES = frozenset({"visually-hidden", "hidden"})
_FOREIGN_CURRENCY = re.compile(r"[$€£¥]|\b(?!PHP\b)[A-Z]{3}\b")
_DEFAULT_VARIANT_TITLE = "Default Title"
_SPEC_HEADING = "tech specification"
# The theme writes barcodes as bare JSON numbers, so one with a leading zero
# (``"gtin12": 097855145710``) makes the whole block invalid JSON. Such values are quoted
# before parsing; nothing else in the block is changed.
_BARE_GTIN = re.compile(r'("gtin(?:8|12|13|14)?"\s*:\s*)([0-9]+)')
# A label/value gap: three or more whitespace characters including at least one no-break
# space. A single "word&nbsp; word" spacing is not a gap.
_LABEL_GAP = re.compile(r"(?=[ \t\xa0]{3,})[ \t]*\xa0[ \t\xa0]*")


def _is_short_label(text: str) -> bool:
    """A specification label: 1-5 words, no digits except a trailing footnote marker."""
    words = text.split()
    core = re.sub(r"[0-9]+(?:\s*,\s*[0-9]+)*\Z", "", text)
    return 0 < len(words) <= 5 and not re.search(r"[0-9]", core)


def product_request(url: str, variant_id: str | None = None) -> ScrapeRequest:
    """Build a request for one Octagon product page (``/products/<handle>`` on ``HOST``).

    The only query allowed is ``variant=<digits>``, which selects one variant of a product
    sold in several configurations. Pass it in the URL or as ``variant_id``, not both.
    """
    parts = urlsplit(url)
    path = parts.path if url_host(url) == HOST and parts.scheme == "https" else ""
    # A dot would name a data endpoint such as /products/<handle>.js, not the product page.
    if not re.fullmatch(r"/products/[^/.]+/?", path) or parts.fragment:
        raise ValueError(f"Not an Octagon product URL on https://{HOST}.")
    if parts.query and not re.fullmatch(r"variant=[0-9]+", parts.query):
        raise ValueError("Only a single numeric variant query is allowed.")
    if variant_id is not None:
        if parts.query or not re.fullmatch(r"[0-9]+", variant_id):
            raise ValueError("variant_id must be digits and the URL must have no query.")
        url = f"{url}?variant={variant_id}"
    return ScrapeRequest(RETAILER_ID, url)


class OCTAdapter:
    retailer_id = RETAILER_ID
    retailer_code = RETAILER_CODE
    # Bump when parsing rules change; recorded in every observation's source metadata.
    # oct-2 (2026-10-13): adds identifier provenance as scoped evidence; parsing is unchanged.
    # oct-3 (2026-10-15): description lines laid out as label + no-break-space gap + value keep
    # their label/value boundary. Other lines are unchanged.
    adapter_version = "oct-3"
    allowed_hosts = frozenset({HOST})
    # Project-owner authorization under the project-wide decision; not retailer consent.
    live_use_approved = True

    def parse(self, page: FetchedPage) -> list[ParsedOffer]:
        text = page.text()
        parsed = _ProductPageParser()
        parsed.feed(text)
        parsed.close()
        products = _products(parsed.jsonld_blocks)
        if not products and not parsed.main_count:
            raise ParseError(
                "UNRECOGNIZED_LAYOUT",
                "No Product JSON-LD and no main product block found; the layout may have changed.",
            )
        if parsed.main_count > 1:
            raise ParseError("AMBIGUOUS_PRODUCT", "The page has more than one main product block.")
        product = products[0] if products else {}
        meta = _analytics_product(text)
        _check_identity(page, parsed, product, meta)

        titles = list(dict.fromkeys(title for title in parsed.titles if title))
        if len(titles) > 1:
            raise ParseError("AMBIGUOUS_PRODUCT", "The main block shows more than one title.")
        name = _unescaped(product.get("name"))
        title = _first(titles) or name
        if not title:
            raise ParseError("TITLE_NOT_FOUND", "The product title could not be found.")
        if name and titles and name != title:
            raise ParseError("AMBIGUOUS_PRODUCT", "The heading and JSON-LD name disagree.")

        ld_offers = _ld_offers(product)
        variants = _meta_variants(meta)
        options = _variant_options(parsed.variant_json)
        selection = _select_variant(page, parsed, ld_offers, variants, options)
        sku = _resolve_sku(selection, parsed.sku_texts)
        price = _resolve_price(selection, parsed.price_blocks)
        availability, evidence = _resolve_availability(
            selection, parsed.buttons, parsed.stock_alerts
        )

        specs = parsed.description_lines()
        variant_title = selection.title if selection.multi else ""
        raw_configuration = " | ".join(
            part
            for part in (
                variant_title,
                " | ".join(f"{label}: {value}" if value else label for label, value in specs)
                or _unescaped(product.get("description")),
            )
            if part
        )
        bundle_evidence = _bundle_evidence(title, "title") + _bundle_evidence(
            variant_title, "variant"
        )
        # Identifiers with their source. A barcode or SKU is not a model code, and the JSON-LD
        # brand names the store; both are kept for review, never used as identity fields.
        identifiers: list[tuple[str, str, str]] = []
        for key in ("sku", "mpn", "gtin12", "gtin13", "productID"):
            if key in product:
                identifiers.append(
                    (IDENTIFIER, f"JSON-LD Product.{key}", _text(product.get(key)) or "(blank)")
                )
        for index, offer in enumerate(ld_offers, start=1):
            for key in ("sku", "gtin12", "gtin13", "mpn"):
                if key in offer:
                    identifiers.append(
                        (IDENTIFIER, f"JSON-LD Offer {index} {key}", _text(offer.get(key)))
                    )
        if _brand_name(product):
            identifiers.append(
                (IDENTIFIER, "JSON-LD Product.brand (store name; not used)", _brand_name(product))
            )
        details = OfferDetails(
            retailer_product_id=_first(list(filter(None, parsed.product_ids)))
            or _text(meta.get("id")),
            retailer_sku=sku,
            # Never set: JSON-LD brand and the analytics vendor name the store, not the maker.
            brand="",
            price_type=price.price_type,
            regular_price=price.regular,
            regular_price_text=price.regular_text,
            sale_price=price.current if price.price_type is PriceType.SALE else None,
            sale_price_text=price.current_text if price.price_type is PriceType.SALE else "",
            raw_specifications=tuple(specs),
            raw_categories=tuple(value for value in (_text(meta.get("type")),) if value),
            bundle_flag=bool(bundle_evidence),
            bundle_evidence=tuple(dict.fromkeys(bundle_evidence)),
            scoped_evidence=tuple(item for item in dict.fromkeys(identifiers) if item[2]),
        )
        return [
            ParsedOffer(
                raw_title=title,
                price_text=price.current_text,
                currency="PHP",
                availability=availability,
                availability_evidence=evidence,
                raw_configuration_text=raw_configuration,
                offer_url=_offer_url(selection, product, parsed.canonical),
                details=details,
            )
        ]


# JSON-LD and the Shopify analytics object


def _products(blocks: list[str]) -> list[dict]:
    """The page's schema.org Product nodes. Several must all name the same product path."""
    products: list[dict] = []
    for block in blocks:
        try:
            data = json.loads(_BARE_GTIN.sub(r'\1"\2"', block), parse_float=Decimal)
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
        paths = [_path(node.get("url")) for node in products]
        if not all(paths) or len(set(paths)) != 1:
            raise ParseError("AMBIGUOUS_PRODUCT", "The page describes more than one Product.")
        if len({json.dumps(node, sort_keys=True, default=str) for node in products}) > 1:
            raise ParseError("AMBIGUOUS_PRODUCT", "The page's Product JSON-LD blocks disagree.")
    return products


def _ld_offers(product: dict) -> list[dict]:
    """Every concrete Offer of the Product node; the theme lists one per variant."""
    offers = product.get("offers")
    if offers is None:
        return []
    offers = offers if isinstance(offers, list) else [offers]
    result = []
    for offer in offers:
        if not isinstance(offer, dict) or "Offer" not in _types(offer):
            raise ParseError("AMBIGUOUS_PRICE", "JSON-LD offer is not a single Offer.")
        result.append(offer)
    return result


def _analytics_product(text: str) -> dict:
    """``ShopifyAnalytics`` ``var meta = {...}`` product object, or empty when absent."""
    found: list[dict] = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\bvar meta = ", text):
        try:
            data, _ = decoder.raw_decode(text, match.end())
        except ValueError:
            continue
        product = data.get("product") if isinstance(data, dict) else None
        if isinstance(product, dict) and product not in found:
            found.append(product)
    if len(found) > 1:
        raise ParseError("AMBIGUOUS_PRODUCT", "The page's analytics data names several products.")
    return _first(found) or {}


def _check_identity(
    page: FetchedPage, parsed: _ProductPageParser, product: dict, meta: dict
) -> None:
    product_ids = list(dict.fromkeys(value for value in parsed.product_ids if value))
    if len(product_ids) > 1:
        raise ParseError("AMBIGUOUS_PRODUCT", "The main block names more than one product ID.")
    meta_id = _text(meta.get("id"))
    if product_ids and meta_id and product_ids[0] != meta_id:
        raise ParseError("AMBIGUOUS_PRODUCT", "The product form and analytics IDs disagree.")
    ld_path, canonical_path = _path(product.get("url")), _path(parsed.canonical)
    if ld_path and canonical_path and ld_path != canonical_path:
        raise ParseError("AMBIGUOUS_PRODUCT", "JSON-LD and the canonical link name other pages.")
    # The page must describe the product that was fetched, not, say, a default product page.
    page_path = _path(page.final_url)
    if any(path and path != page_path for path in (ld_path, canonical_path)):
        raise ParseError("AMBIGUOUS_PRODUCT", "The page describes a product at another URL.")
    handle = _text(meta.get("handle"))
    if handle and page_path != f"/products/{handle.lower()}":
        raise ParseError("AMBIGUOUS_PRODUCT", "The analytics handle names another product.")


# Variants


@dataclass(frozen=True)
class _Variant:
    id: str
    title: str
    price: Decimal | None
    sku: str


@dataclass(frozen=True)
class _Selection:
    multi: bool
    variant_id: str
    title: str
    offer: dict | None
    variant: _Variant | None


def _meta_variants(meta: dict) -> list[_Variant]:
    """Variants from ``var meta``: ID, ``public_title``, price in integer cents, and SKU."""
    items = meta.get("variants")
    if items is None:
        return []
    if not isinstance(items, list):
        raise ParseError("AMBIGUOUS_VARIANT", "The analytics variant data is not a list.")
    by_id: dict[str, _Variant] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        variant = _Variant(
            id=_text(item.get("id")),
            title=_text(item.get("public_title")),
            price=_cents(item.get("price")),
            sku=_text(item.get("sku")),
        )
        if not variant.id or (variant.id in by_id and by_id[variant.id] != variant):
            raise ParseError("AMBIGUOUS_VARIANT", "The analytics variant data is inconsistent.")
        by_id[variant.id] = variant
    return list(by_id.values())


def _variant_options(blocks: list[str]) -> dict[str, str]:
    """Variant ID to option title from the theme's ``data-element="variants-data"`` JSON."""
    options: dict[str, str] = {}
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            raise ParseError("AMBIGUOUS_VARIANT", "The variant data could not be read.") from None
        if not isinstance(data, dict):
            raise ParseError("AMBIGUOUS_VARIANT", "The variant data is not an object.")
        for variant_id, item in data.items():
            values = item.get("options") if isinstance(item, dict) else None
            if not re.fullmatch(r"[0-9]+", variant_id) or not isinstance(values, list):
                raise ParseError("AMBIGUOUS_VARIANT", "The variant data is not in the known form.")
            title = " / ".join(clean_text(str(value)) for value in values)
            if options.get(variant_id, title) != title:
                raise ParseError("AMBIGUOUS_VARIANT", "The variant data disagrees with itself.")
            options[variant_id] = title
    return options


def _cents(value: object) -> Decimal | None:
    """Shopify analytics prices are integer cents (``4499900`` is ``44999.00``)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return Decimal(value) / 100


def _select_variant(
    page: FetchedPage,
    parsed: _ProductPageParser,
    ld_offers: list[dict],
    variants: list[_Variant],
    options: dict[str, str],
) -> _Selection:
    """The one variant whose price the page shows, or ``AMBIGUOUS_VARIANT``.

    A product with several variants is a set of different configurations. The page's price is
    then only attributable when the request named the variant (``?variant=<id>``) and the
    page's product forms selected that same variant.
    """
    form_ids = list(dict.fromkeys(value for value in parsed.form_variant_ids if value))
    if len(form_ids) > 1:
        raise ParseError("AMBIGUOUS_VARIANT", "The product forms select more than one variant.")
    form_id = _first(form_ids)
    requested = {
        value
        for url in (page.final_url, page.requested_url)
        for value in parse_qs(urlsplit(url).query).get("variant", [])
    }
    if len(requested) > 1:
        raise ParseError("AMBIGUOUS_VARIANT", "The request names more than one variant.")
    requested_id = _first(sorted(requested))
    known_ids = {variant.id for variant in variants} | set(options)
    if variants and options and {variant.id for variant in variants} != set(options):
        raise ParseError("AMBIGUOUS_VARIANT", "The analytics and theme variant lists disagree.")
    multi = len(known_ids) > 1 or len(ld_offers) > 1 or len(set(parsed.option_values)) > 1
    if multi and not requested_id:
        raise ParseError(
            "AMBIGUOUS_VARIANT",
            "The product has several variants; request a URL with ?variant=<id>.",
        )
    if requested_id and form_id != requested_id:
        raise ParseError("AMBIGUOUS_VARIANT", "The page did not select the requested variant.")
    selected_id = requested_id or form_id
    if known_ids and selected_id and selected_id not in known_ids:
        raise ParseError("AMBIGUOUS_VARIANT", "The selected variant is not in the variant data.")
    variant = next((v for v in variants if v.id == selected_id), None)
    if variant is None and len(variants) == 1 and not selected_id:
        variant = variants[0]
    title = options.get(selected_id) or (variant.title if variant else "")
    return _Selection(
        multi=multi,
        variant_id=selected_id,
        title="" if title == _DEFAULT_VARIANT_TITLE else title,
        offer=_selected_offer(ld_offers, selected_id),
        variant=variant,
    )


def _selected_offer(ld_offers: list[dict], variant_id: str) -> dict | None:
    if not ld_offers:
        return None
    if len(ld_offers) == 1:
        offer_id = _offer_variant(ld_offers[0])
        if offer_id and variant_id and offer_id != variant_id:
            raise ParseError("AMBIGUOUS_VARIANT", "The JSON-LD offer is for another variant.")
        return ld_offers[0]
    matches = [offer for offer in ld_offers if _offer_variant(offer) == variant_id]
    if len(matches) != 1:
        raise ParseError(
            "AMBIGUOUS_PRICE", "No single JSON-LD offer matches the selected variant."
        )
    return matches[0]


def _offer_variant(offer: dict) -> str:
    url = offer.get("url")
    if not isinstance(url, str):
        return ""
    return _first(parse_qs(urlsplit(url.strip()).query).get("variant", []))


def _resolve_sku(selection: _Selection, sku_texts: list[str]) -> str:
    """The selected variant's SKU. The visible ``SKU:`` line, JSON-LD, and analytics agree."""
    shown = []
    for text in dict.fromkeys(sku_texts):
        match = re.fullmatch(r"SKU:(.*)", text, re.I)
        if not match:
            raise ParseError("AMBIGUOUS_VARIANT", "The SKU line is not in the known form.")
        shown.append(clean_text(match.group(1)))  # An empty "SKU:" line states no SKU.
    candidates = [
        *shown,
        _text(selection.offer.get("sku")) if selection.offer else "",
        selection.variant.sku if selection.variant else "",
    ]
    distinct = list(dict.fromkeys(value for value in candidates if value))
    if len(distinct) > 1:
        raise ParseError("AMBIGUOUS_VARIANT", "The shown, JSON-LD, and analytics SKUs disagree.")
    return _first(distinct)


def _offer_url(selection: _Selection, product: dict, canonical: str) -> str:
    for candidate in (
        selection.offer.get("url") if selection.offer else None,
        product.get("url"),
        canonical,
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


# Price


@dataclass(frozen=True)
class _Price:
    current: Decimal
    current_text: str
    regular: Decimal | None
    regular_text: str
    price_type: PriceType | None


def _resolve_price(selection: _Selection, blocks: list[_PriceBlock]) -> _Price:
    shown = _visible_price(blocks)
    offer, variant = selection.offer, selection.variant
    ld_text = _text(offer.get("price")) if offer and "price" in offer else ""
    ld_current = _price_value(ld_text, "JSON-LD price") if ld_text else None
    if offer and offer.get("priceCurrency") not in (None, "", "PHP"):
        raise ParseError("UNSUPPORTED_CURRENCY", "JSON-LD priceCurrency is not PHP.")
    for other, what in ((ld_current, "JSON-LD"), (variant.price if variant else None, "analytics")):
        if other is not None and shown is not None and other != shown.current:
            raise ParseError("PRICE_CONFLICT", f"Displayed price and {what} price disagree.")
    if ld_current is not None and variant and variant.price is not None:
        if ld_current != variant.price:
            raise ParseError("PRICE_CONFLICT", "JSON-LD and analytics prices disagree.")

    if shown is None:
        if ld_current is None:
            raise ParseError("PRICE_NOT_FOUND", "No current selling price was found.")
        if offer.get("priceCurrency") != "PHP":
            raise ParseError("CURRENCY_NOT_FOUND", "JSON-LD price has no PHP priceCurrency.")
        # Without the displayed block, sale versus regular is not known.
        return _Price(ld_current, ld_text, None, "", None)
    if shown.regular is None:
        return _Price(
            shown.current, shown.current_text, shown.current, shown.current_text, PriceType.REGULAR
        )
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
    """The main product's ``dl.#price`` block.

    The current price is the ``#price-item @regular`` amount. On a sale the theme adds
    ``@on-sale`` to that item ("Sale price") and renders a ``#price-item @compare`` amount
    ("Regular price"). The ``@save`` percentage is not a price and is ignored. Text without
    digits (for example "Call for price") is not an amount.
    """
    blocks = [block for block in blocks if block.has_amount()]
    if not blocks:
        return None
    if len(blocks) > 1:
        raise ParseError("AMBIGUOUS_PRICE", "More than one product price is displayed.")
    block = blocks[0]
    if block.other:
        raise ParseError("AMBIGUOUS_PRICE", "The price block shows an amount of unknown kind.")
    if len(block.current) > 1 or len(block.compare) > 1:
        raise ParseError("AMBIGUOUS_PRICE", "A price item shows several amounts.")
    if not block.current:
        raise ParseError("AMBIGUOUS_PRICE", "The price block has no current price item.")
    current_text, compare_text = block.current[0], _first(block.compare)
    current = _amount(current_text, "displayed price")
    was = _amount(compare_text, "displayed regular price") if compare_text else None
    if block.on_sale:
        if was is None:
            raise ParseError("AMBIGUOUS_PRICE", "The sale price has no regular price beside it.")
        if was < current:
            raise ParseError("PRICE_CONFLICT", "The regular price is below the sale price.")
        if was == current:
            return _ShownPrice(current, current_text, None, "")
        return _ShownPrice(current, current_text, was, compare_text)
    if was is not None and was > current:
        raise ParseError(
            "PRICE_CONFLICT", "A higher regular price is shown but the price is not on sale."
        )
    return _ShownPrice(current, current_text, None, "")


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
    selection: _Selection, buttons: list[_Button], alerts: list[str]
) -> tuple[str, str]:
    """Combine JSON-LD availability, the stock alert, and the add-to-cart button.

    JSON-LD, the stock alert ("Only 2 left in stock!"), and the "Sold out", "Pre-order", and
    "Unavailable" labels are explicit signals and must agree. An unrecognized stock alert is
    an UNKNOWN signal. The button must also be consistent: enabled "Add to cart" for in stock,
    enabled "Pre-order" or "Add to cart" for preorder, absent or disabled or "Sold out" for
    out of stock. Disagreement, several different buttons or alerts, or no explicit signal
    gives UNKNOWN; price visibility and an "Add to cart" label alone never decide stock.
    """
    token = ""
    if selection.offer:
        token = _text(selection.offer.get("availability")).rstrip("/").rsplit("/", 1)[-1]
    distinct = list(dict.fromkeys(buttons))
    shown_alerts = list(dict.fromkeys(alert for alert in alerts if alert))
    conflict = len(distinct) > 1 or len(shown_alerts) > 1
    button = distinct[0] if len(distinct) == 1 else None
    label = button.label.lower() if button else ""

    explicit: list[Availability] = []
    if token:
        # Unmapped values (BackOrder, LimitedAvailability, and the like) count as UNKNOWN.
        explicit.append(_JSONLD_AVAILABILITY.get(token.lower(), Availability.UNKNOWN))
    for alert in shown_alerts:
        explicit.append(
            next(
                (value for pattern, value in _STOCK_ALERTS if pattern.fullmatch(alert)),
                Availability.UNKNOWN,
            )
        )
    if button is not None:
        from_label = _BUTTON_LABELS.get(label, Availability.UNKNOWN)
        if from_label is not None:
            explicit.append(from_label)
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
        "JSON-LD availability: " + (token or "none"),
        "stock alert: " + (" / ".join(shown_alerts) or "none"),
        "add to cart button: " + (shown or "absent"),
    ]
    if conflict:
        parts.append("signals conflict")
    return value, "; ".join(parts)


# Bundles and small helpers


def _bundle_evidence(text: str, source: str) -> tuple[str, ...]:
    evidence = [
        f"{source} term {term}: {clean_text(match.group(0))}"
        for term, pattern in BUNDLE_TITLE_TERMS
        for match in pattern.finditer(text)
    ]
    return tuple(dict.fromkeys(evidence))


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
    return clean_text(html.unescape(_text(value)))


def _path(value: object) -> str:
    """Lowercased URL path without a trailing slash; Shopify handles are lowercase."""
    if not isinstance(value, str) or not value.strip():
        return ""
    return urlsplit(value.strip()).path.rstrip("/").lower()


def _brand_name(product: dict) -> str:
    brand = product.get("brand")
    return _text(brand.get("name")) if isinstance(brand, dict) else _text(brand)


def _first(values: list[str]) -> str:
    return values[0] if values else ""


# HTML parsing


@dataclass
class _PriceBlock:
    on_sale: bool = False
    current: list[str] = field(default_factory=list)
    compare: list[str] = field(default_factory=list)
    # Amounts in ``#price-value`` items that are neither ``@regular`` nor ``@compare``.
    other: list[str] = field(default_factory=list)

    def has_amount(self) -> bool:
        texts = (*self.current, *self.compare, *self.other)
        return any(re.search(r"[0-9]", text) for text in texts)


@dataclass
class _Element:
    tag: str
    classes: frozenset[str]
    attrs: dict[str, str]
    # Context inherited from ancestors, so no stack scan is needed per tag.
    template: bool = False
    main: bool = False
    provider: bool = False
    hidden: bool = False
    price_item: str = ""
    section: _Element | None = None
    spec_section: bool = False
    buffer: list[str] | None = None
    on_close: Callable[[str], None] | None = None


class _ProductPageParser(HTMLParser):
    """Collects the main product's markup and the "Tech Specification" description.

    "Main" means inside the theme's ``div.#product`` (``productPageContainer-…``). Everything
    inside ``<template>`` and hidden text (``visually-hidden``, ``hidden``) is excluded; the
    header, cart drawer, and product cards (``#product-card``) are outside the main block. The
    description is the first ``div.#rte`` in a ``#section-body`` whose ``#hero-heading`` reads
    "Tech Specification"; it is a separate section after the main block.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Element] = []
        self.jsonld_blocks: list[str] = []
        self.variant_json: list[str] = []
        self.main_count = 0
        self.titles: list[str] = []
        self.product_ids: list[str] = []
        self.form_variant_ids: list[str] = []
        self.option_values: list[str] = []
        self.price_blocks: list[_PriceBlock] = []
        self.buttons: list[_Button] = []
        self.stock_alerts: list[str] = []
        self.sku_texts: list[str] = []
        self.description: list[str] = []
        self.canonical = ""
        self._block: _PriceBlock | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = frozenset(attributes.get("class", "").split())
        parent = self.stack[-1] if self.stack else None
        is_main = tag == "div" and "#product" in classes
        element = _Element(
            tag,
            classes,
            attributes,
            template=tag == "template" or bool(parent and parent.template),
            main=is_main or bool(parent and parent.main),
            provider=tag == "product-variant-provider" or bool(parent and parent.provider),
            hidden=bool(classes & _HIDDEN_CLASSES)
            or "hidden" in attributes
            or bool(parent and parent.hidden),
            price_item=(
                _price_item_kind(classes) if "#price-item" in classes
                else parent.price_item if parent else ""
            ),
            section=None,
        )
        element.section = (
            element if "#section-body" in classes else parent.section if parent else None
        )
        if is_main and not (parent and parent.main):
            self.main_count += 1
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
        elif (
            tag == "script"
            and element.provider
            and attributes.get("type") == "application/json"
            and attributes.get("data-element") == "variants-data"
        ):
            # The theme puts this beside, not inside, the main block.
            self._capture(element, self.variant_json.append)
        elif element.main:
            self._main_tag(element)
        else:
            self._section_tag(element)

    def _main_tag(self, element: _Element) -> None:
        tag, classes, attributes = element.tag, element.classes, element.attrs
        if tag == "h1" and any("#product-title" in e.classes for e in self.stack):
            self._capture(element, lambda text: self.titles.append(clean_text(text)))
        elif tag == "dl" and "#price" in classes and self._block is None:
            block = _PriceBlock()
            self.price_blocks.append(block)
            self._block = block
            element.on_close = self._end_price_block
        elif tag == "div" and "#price-item" in classes and self._block is not None:
            if element.price_item == "current" and "@on-sale" in classes:
                self._block.on_sale = True
        elif "#price-value" in classes and self._block is not None and not element.hidden:
            texts = {
                "current": self._block.current, "compare": self._block.compare
            }.get(element.price_item, self._block.other)
            self._capture(element, lambda text: texts.append(clean_text(text)))
        elif "#product-stock-alert-text" in classes and not element.hidden:
            self._capture(element, lambda text: self.stock_alerts.append(clean_text(text)))
        elif tag == "button" and attributes.get("data-element") == "to-cart-button":
            disabled = "disabled" in attributes or attributes.get("aria-disabled") == "true"
            self._capture(
                element, lambda text: self.buttons.append(_Button(clean_text(text), disabled))
            )
        elif attributes.get("id", "").startswith("b-sku") and not element.hidden:
            self._capture(element, lambda text: self.sku_texts.append(clean_text(text)))
        elif tag == "input" and attributes.get("name") == "product-id":
            self.product_ids.append(attributes.get("value", "").strip())
        elif tag == "input" and attributes.get("name") == "id":
            self.form_variant_ids.append(attributes.get("value", "").strip())
        elif tag == "option" and any(
            e.tag == "select" and e.attrs.get("name") == "id" for e in self.stack
        ):
            self.option_values.append(attributes.get("value", "").strip())

    def _section_tag(self, element: _Element) -> None:
        section = element.section
        if section is None:
            return
        if element.tag == "h2" and "#hero-heading" in element.classes:
            element.on_close = lambda text: self._mark_spec_section(section, text)
            element.buffer = []
        elif (
            element.tag == "div"
            and "#rte" in element.classes
            and section.spec_section
            and not self.description
        ):
            self._capture(element, self.description.append)

    def _mark_spec_section(self, section: _Element, text: str) -> None:
        if clean_text(text).lower() == _SPEC_HEADING:
            section.spec_section = True

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                if tag in _LINE_END_TAGS:
                    self.handle_data("\n")
                elif tag in _CELL_TAGS:
                    self.handle_data(" ")
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
            # Script text only feeds its own capture (JSON), never the visible text.
            if innermost.buffer is not None:
                innermost.buffer.append(data)
            return
        if innermost.hidden or innermost.template:
            return
        for element in self.stack:
            if element.buffer is not None:
                element.buffer.append(data)

    def description_lines(self) -> list[tuple[str, str]]:
        """Description lines as shown, split at the first colon into ``(label, value)``.

        A line laid out as ``Label`` + a run of no-break spaces + ``value`` (reviewed 2026-10-15
        ASUS page: ``Model \\xa0 \\xa0FA506NCQ``) is split at that gap when it comes before any
        colon and the label is short (up to five words, no digits other than a trailing
        footnote). A line without either is kept with an empty value. Nothing is renamed or
        inferred.
        """
        rows: list[tuple[str, str]] = []
        for line in "\n".join(self.description[:1]).split("\n"):
            gap = _LABEL_GAP.search(line)
            colon = line.find(":")
            if gap and (colon < 0 or gap.start() < colon):
                label, value = clean_text(line[: gap.start()]), clean_text(line[gap.end():])
                if value and _is_short_label(label):
                    rows.append((label, value))
                    continue
            line = clean_text(line)
            if not line:
                continue
            label, colon, value = line.partition(":")
            if colon and clean_text(label):
                rows.append((clean_text(label), clean_text(value)))
            else:
                rows.append((line, ""))
        return rows

    def _end_price_block(self, _text: str) -> None:
        self._block = None

    def _capture(self, element: _Element, sink: Callable[[str], None]) -> None:
        element.buffer = []
        element.on_close = sink


def _price_item_kind(classes: frozenset[str]) -> str:
    if "@compare" in classes:
        return "compare"
    if "@regular" in classes:
        return "current"
    return "other"  # For example ``@save``, a percentage, or a unit price.
