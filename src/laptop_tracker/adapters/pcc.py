"""PC Central (retailer code PCC) product-page adapter (overall Phase 7).

Parses one already-fetched PC Central product page into one ``ParsedOffer``. It makes no HTTP
requests; the shared ``Fetcher`` does all retrieval. Live use is enabled under the project-wide
owner decision for this internal tool. The retailer's own published pages neither grant nor
prohibit automated collection, so the external-policy finding is UNKNOWN. See
docs/RETAILER_DATA_USE_REVIEW.md and docs/DECISIONS.md. Page assumptions, versioning, and
limitations are in docs/adapters/PCC.md.

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
    ENUMERATED_VARIANT,
    IDENTIFIER,
    SELECTED_VARIANT,
    VARIANT_DEFINITIONS,
    Availability,
    ObservationValidationError,
    OfferDetails,
    PriceType,
    clean_text,
    parse_price,
    url_host,
)
from ..scraping import FetchedPage, ParsedOffer, ParseError, ScrapeRequest
from ..url_paths import normalized_path

RETAILER_ID = "pcc"
RETAILER_CODE = "PCC"
# The PC Central Shopify storefront (shop pc-central-computer-store.myshopify.com).
# www.pccentralcomputerstore.com redirects here (HTTP 301, checked 2026-10-01); it is not
# allowed, so a request must name this host directly. The older pccentral.com.ph domain
# redirected to an unrelated site on 2026-10-01 and must never be allowed.
HOST = "pccentralcomputerstore.com"
# Project status: enabled under the project-wide owner decision of 2026-10-01. The retailer's
# external-policy finding (UNKNOWN) is recorded separately and is not retailer consent.
DATA_USE_REVIEW_STATUS = "OWNER_APPROVED"

_JSONLD_AVAILABILITY = {
    "instock": Availability.IN_STOCK,
    "outofstock": Availability.OUT_OF_STOCK,
    "soldout": Availability.OUT_OF_STOCK,
    "preorder": Availability.PREORDER,
}
# Dawn add-to-cart button labels. "Add to cart" is not stock wording, so it only confirms
# another signal; the other labels state availability explicitly.
_BUTTON_LABELS: dict[str, Availability | None] = {
    "add to cart": None,
    "sold out": Availability.OUT_OF_STOCK,
    "pre-order": Availability.PREORDER,
    "preorder": Availability.PREORDER,
    "unavailable": Availability.UNKNOWN,
}
# Title wording that suggests a bundle or freebie. Matches are flagged for downstream review;
# the offer is never dropped. Same terms as the DataBlitz and PC Express adapters.
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
# An internal marker (ASCII unit separator) never present in page text.
_SPAN_BOUNDARY = "\x1f"


def _is_short_label(text: str) -> bool:
    """A specification label: 1-4 words, no colon, no digits except a trailing footnote."""
    words = text.split()
    core = re.sub(r"[0-9]+(?:\s*,\s*[0-9]+)*\Z", "", text)
    return 0 < len(words) <= 4 and ":" not in text and not re.search(r"[0-9]", core)
_HIDDEN_CLASSES = frozenset({"visually-hidden", "hidden", "badge", "unit-price"})
_FOREIGN_CURRENCY = re.compile(r"[$€£¥]|\b(?!PHP\b)[A-Z]{3}\b")
_DEFAULT_VARIANT_TITLE = "Default Title"


def product_request(url: str, variant_id: str | None = None) -> ScrapeRequest:
    """Build a request for one PC Central product page (``/products/<handle>`` on ``HOST``).

    The only query allowed is ``variant=<digits>``, which selects one variant of a product
    sold in several configurations. Pass it in the URL or as ``variant_id``, not both.
    """
    parts = urlsplit(url)
    path = parts.path if url_host(url) == HOST else ""
    if not re.fullmatch(r"/products/[^/]+/?", path) or parts.fragment:
        raise ValueError(f"Not a PC Central product URL on {HOST}.")
    if parts.query and not re.fullmatch(r"variant=[0-9]+", parts.query):
        raise ValueError("Only a single numeric variant query is allowed.")
    if variant_id is not None:
        if parts.query or not re.fullmatch(r"[0-9]+", variant_id):
            raise ValueError("variant_id must be digits and the URL must have no query.")
        url = f"{url}?variant={variant_id}"
    return ScrapeRequest(RETAILER_ID, url)


class PCCAdapter:
    retailer_id = RETAILER_ID
    retailer_code = RETAILER_CODE
    # Bump when parsing rules change; recorded in every observation's source metadata.
    # pcc-2 (2026-10-13): adds bound selected-variant options, enumerated variants, and
    # identifiers as scoped evidence. Price, availability, and description parsing unchanged.
    # pcc-3: complete bound variant-definition matrices for approved shared CPU/screen evidence.
    # pcc-4 (2026-10-15): a description paragraph laid out as <span>Label </span><span>value
    # </span> keeps its label/value boundary (reviewed 2026-10-15 Nitro page). Other text is
    # byte-for-byte as before.
    # pcc-7 (2026-11-03): the variant-definition matrix of a product whose complete picker lists
    # exactly one variant carries ``complete_sole_variant`` (saved X1504VA and AG14-71M pages).
    # Other output is unchanged.
    adapter_version = "pcc-7"
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
        _check_identity(parsed, product, meta)

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
        variants = _variants(parsed.variant_json) or _meta_variants(meta)
        selection = _select_variant(page, parsed, ld_offers, variants)
        price = _resolve_price(selection, parsed.price_blocks)
        availability, evidence = _resolve_availability(
            selection, parsed.buttons, parsed.price_blocks
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
        details = OfferDetails(
            retailer_product_id=_first(list(filter(None, parsed.product_ids)))
            or _text(meta.get("id")),
            retailer_sku=selection.sku,
            brand=_brand(product) or _text(meta.get("vendor")),
            price_type=price.price_type,
            regular_price=price.regular,
            regular_price_text=price.regular_text,
            sale_price=price.current if price.price_type is PriceType.SALE else None,
            sale_price_text=price.current_text if price.price_type is PriceType.SALE else "",
            raw_specifications=tuple(specs),
            raw_categories=tuple(value for value in (_text(meta.get("type")),) if value),
            bundle_flag=bool(bundle_evidence),
            bundle_evidence=tuple(dict.fromkeys(bundle_evidence)),
            scoped_evidence=_variant_evidence(selection, parsed, variants, ld_offers),
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
        paths = [_path(node.get("url")) for node in products]
        if not all(paths) or len(set(paths)) != 1:
            raise ParseError("AMBIGUOUS_PRODUCT", "The page describes more than one Product.")
        if len({json.dumps(node, sort_keys=True, default=str) for node in products}) > 1:
            raise ParseError("AMBIGUOUS_PRODUCT", "The page's Product JSON-LD blocks disagree.")
    return products


def _ld_offers(product: dict) -> list[dict]:
    """Every concrete Offer of the Product node; Dawn lists one per variant."""
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


def _check_identity(parsed: _ProductPageParser, product: dict, meta: dict) -> None:
    product_ids = list(dict.fromkeys(value for value in parsed.product_ids if value))
    if len(product_ids) > 1:
        raise ParseError("AMBIGUOUS_PRODUCT", "The main block names more than one product ID.")
    meta_id = _text(meta.get("id"))
    if product_ids and meta_id and product_ids[0] != meta_id:
        raise ParseError("AMBIGUOUS_PRODUCT", "The product form and analytics IDs disagree.")
    ld_path, canonical_path = _path(product.get("url")), _path(parsed.canonical)
    if ld_path and canonical_path and ld_path != canonical_path:
        raise ParseError("AMBIGUOUS_PRODUCT", "JSON-LD and the canonical link name other pages.")
    brand, vendor = _brand(product), _text(meta.get("vendor"))
    if brand and vendor and brand != vendor:
        raise ParseError("AMBIGUOUS_PRODUCT", "JSON-LD brand and analytics vendor disagree.")


# Variants


@dataclass(frozen=True)
class _Variant:
    id: str
    title: str
    price: Decimal | None
    compare_at: Decimal | None
    available: bool | None
    sku: str
    # The variant's own option values in order ("16GB (2x8) DDR4 RAM", "512GB SDD").
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Selection:
    multi: bool
    variant_id: str
    title: str
    sku: str
    offer: dict | None
    variant: _Variant | None


def _variants(blocks: list[str]) -> list[_Variant]:
    """Variants from the Dawn selector's JSON (``variant-radios``/``variant-selects``)."""
    variants: list[_Variant] = []
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            raise ParseError("AMBIGUOUS_VARIANT", "The variant data could not be read.") from None
        if not isinstance(data, list):
            raise ParseError("AMBIGUOUS_VARIANT", "The variant data is not a list.")
        variants += [_variant(item) for item in data if isinstance(item, dict)]
    return _distinct_variants(variants)


def _meta_variants(meta: dict) -> list[_Variant]:
    items = meta.get("variants")
    if not isinstance(items, list):
        return []
    # The analytics object has no compare-at price or availability.
    return _distinct_variants(
        [
            _variant(
                {**item, "title": item.get("public_title"), "compare_at_price": None,
                 "available": None}
            )
            for item in items
            if isinstance(item, dict)
        ]
    )


def _variant(item: dict) -> _Variant:
    available = item.get("available")
    options = item.get("options")
    if not (isinstance(options, list) and all(isinstance(o, str) for o in options)):
        options = [item.get(f"option{n}") for n in (1, 2, 3)]
    return _Variant(
        id=_text(item.get("id")),
        title=_text(item.get("title")),
        price=_cents(item.get("price")),
        compare_at=_cents(item.get("compare_at_price")),
        available=available if isinstance(available, bool) else None,
        sku=_text(item.get("sku")),
        options=tuple(clean_text(o) for o in options if isinstance(o, str) and o.strip()),
    )


def _distinct_variants(variants: list[_Variant]) -> list[_Variant]:
    by_id: dict[str, _Variant] = {}
    for variant in variants:
        if variant.id in by_id and by_id[variant.id] != variant:
            raise ParseError("AMBIGUOUS_VARIANT", "The variant data disagrees with itself.")
        by_id[variant.id] = variant
    return list(by_id.values())


def _cents(value: object) -> Decimal | None:
    """Shopify variant JSON prices are integer cents (``4199900`` is ``41999.00``)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return Decimal(value) / 100


def _select_variant(
    page: FetchedPage, parsed: _ProductPageParser, ld_offers: list[dict], variants: list[_Variant]
) -> _Selection:
    """The one variant whose price the page shows, or ``AMBIGUOUS_VARIANT``.

    A product with several variants is a set of different configurations (for example RAM
    upgrades). The page's price is then only attributable when the request named the variant
    (``?variant=<id>``) and the page's product form selected that same variant.
    """
    form_ids = list(dict.fromkeys(value for value in parsed.form_variant_ids if value))
    if len(form_ids) > 1:
        raise ParseError("AMBIGUOUS_VARIANT", "The product form selects more than one variant.")
    form_id = _first(form_ids)
    requested = {
        value
        for url in (page.final_url, page.requested_url)
        for value in parse_qs(urlsplit(url).query).get("variant", [])
    }
    if len(requested) > 1:
        raise ParseError("AMBIGUOUS_VARIANT", "The request names more than one variant.")
    requested_id = _first(sorted(requested))
    multi = (
        len(variants) > 1
        or len(ld_offers) > 1
        or len(set(parsed.option_values)) > 1
        or parsed.only_default_variant == "false"
    )
    if multi and not requested_id:
        raise ParseError(
            "AMBIGUOUS_VARIANT",
            "The product has several variants; request a URL with ?variant=<id>.",
        )
    if requested_id and form_id != requested_id:
        raise ParseError(
            "AMBIGUOUS_VARIANT", "The page did not select the requested variant."
        )
    selected_id = requested_id or form_id
    variant = next((v for v in variants if v.id == selected_id), None)
    if variants and variant is None and (multi or selected_id):
        raise ParseError("AMBIGUOUS_VARIANT", "The selected variant is not in the variant data.")
    if variant is None and len(variants) == 1:
        variant = variants[0]
    offer = _selected_offer(ld_offers, selected_id)
    title = variant.title if variant else ""
    return _Selection(
        multi=multi,
        variant_id=selected_id,
        title="" if title == _DEFAULT_VARIANT_TITLE else title,
        sku=variant.sku if variant else "",
        offer=offer,
        variant=variant,
    )


def _variant_evidence(
    selection: _Selection,
    parsed: _ProductPageParser,
    variants: list[_Variant],
    ld_offers: list[dict],
) -> tuple[tuple[str, str, str], ...]:
    """Selected-variant option values, other enumerated variants, and identifiers.

    Option values are emitted as SELECTED_VARIANT evidence only when the selection is bound:
    the requested variant is the form's variant (checked in ``_select_variant``), every
    checked option control and any selected ``<option>`` agree with that variant's own option
    values in the product's variant data. Otherwise a ``binding status`` row says why not, and
    no option value is emitted. Another variant is listed only with the URL the page's own
    JSON-LD gives it; no URL or variant ID is ever constructed.
    """
    items: list[tuple[str, str, str]] = []
    variant = selection.variant
    if selection.multi and variant is not None:
        checked = parsed.checked_options
        names = [name for name, _ in checked]
        problem = ""
        if not variant.options:
            problem = "the variant data has no option values"
        elif not checked:
            problem = "no checked option control was found"
        elif len(set(names)) != len(names):
            problem = "an option has more than one checked value"
        elif [value for _, value in checked] != list(variant.options):
            problem = "the checked option values differ from the variant data"
        elif parsed.selected_option_ids and set(parsed.selected_option_ids) != {variant.id}:
            problem = "the selected <option> names another variant"
        if problem:
            items.append((SELECTED_VARIANT, "binding status", f"NOT_CONFIRMED: {problem}"))
        else:
            items.append((SELECTED_VARIANT, "binding status", "CONFIRMED"))
            items.append((SELECTED_VARIANT, "variant id", variant.id))
            items += [(SELECTED_VARIANT, name, value) for name, value in checked]
            # A complete picker matrix is needed before CPU/display can be shared. Analytics
            # fallback or a partial array cannot establish which dimensions vary.
            raw_arrays = [json.loads(block) for block in parsed.variant_json]
            ids = {v.id for v in variants}
            complete = (
                bool(raw_arrays) and len(ids) == len(variants) and "" not in ids
                and all(
                    isinstance(array, list) and len(array) == len(variants)
                    and all(isinstance(row, dict) for row in array)
                    and {_text(row.get("id")) for row in array} == ids
                    for array in raw_arrays
                )
                and bool(parsed.option_values) and set(parsed.option_values) == ids
                and (not ld_offers or {_offer_variant(o) for o in ld_offers} == ids)
                and all(len(v.options) == len(names) for v in variants)
            )
            if complete:
                matrix = {
                    "selected_id": variant.id,
                    "option_names": names,
                    "variants": [
                        {"id": v.id, "title": v.title, "options": list(v.options)}
                        for v in variants
                    ],
                }
                if len(variants) == 1:
                    # The checks above (picker array, option values and JSON-LD offers all name
                    # this one variant) prove no other variant exists.
                    matrix["complete_sole_variant"] = True
                items.append((
                    VARIANT_DEFINITIONS,
                    "main product variant picker script[type=application/json]; select[name=id]",
                    json.dumps(matrix, ensure_ascii=False, sort_keys=True),
                ))
    if selection.multi:
        urls = {_offer_variant(offer): offer.get("url") for offer in ld_offers}
        for other in variants:
            url = urls.get(other.id)
            if other.id != selection.variant_id and isinstance(url, str) and url.strip():
                items.append((ENUMERATED_VARIANT, other.title or other.id, url.strip()))
    if variant is not None:
        items.append((IDENTIFIER, "selected variant sku", variant.sku or "(blank)"))
    for value in dict.fromkeys(v for v in parsed.product_ids if v):
        items.append((IDENTIFIER, "product form product-id", value))
    return tuple(items)


def _selected_offer(ld_offers: list[dict], variant_id: str) -> dict | None:
    if not ld_offers:
        return None
    matches = [offer for offer in ld_offers if _offer_variant(offer) == variant_id]
    if len(ld_offers) == 1:
        offer_id = _offer_variant(ld_offers[0])
        if offer_id and variant_id and offer_id != variant_id:
            raise ParseError("AMBIGUOUS_VARIANT", "The JSON-LD offer is for another variant.")
        return ld_offers[0]
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
    for other, what in ((ld_current, "JSON-LD"), (variant.price if variant else None, "variant")):
        if other is not None and shown is not None and other != shown.current:
            raise ParseError("PRICE_CONFLICT", f"Displayed price and {what} price disagree.")
    if ld_current is not None and variant and variant.price is not None:
        if ld_current != variant.price:
            raise ParseError("PRICE_CONFLICT", "JSON-LD and variant prices disagree.")

    if shown is None:
        if ld_current is None:
            raise ParseError("PRICE_NOT_FOUND", "No current selling price was found.")
        if offer.get("priceCurrency") != "PHP":
            raise ParseError("CURRENCY_NOT_FOUND", "JSON-LD price has no PHP priceCurrency.")
        # Without the displayed block, sale versus regular is not known.
        return _Price(ld_current, ld_text, None, "", None)

    compare_at = variant.compare_at if variant else None
    variant_on_sale = compare_at is not None and compare_at > shown.current
    if shown.regular is None:
        if variant_on_sale:
            raise ParseError(
                "PRICE_CONFLICT", "The variant has a higher compare-at price that is not shown."
            )
        return _Price(
            shown.current, shown.current_text, shown.current, shown.current_text, PriceType.REGULAR
        )
    if compare_at is not None and compare_at != shown.regular:
        raise ParseError("PRICE_CONFLICT", "Displayed and variant compare-at prices disagree.")
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
    """The main product's Dawn ``div.price`` block.

    Dawn renders both a regular container (``.price__regular``) and a sale container
    (``.price__sale``: ``<s>`` compare-at price plus ``.price-item--sale``); the theme's CSS
    shows the sale container only when the block has ``price--on-sale``. Without that class
    the regular amount is the price and any compare-at amount is not shown, so it is not a
    regular-price claim. Text without digits (for example "Call for price") is not an amount.
    """
    blocks = [block for block in blocks if block.has_amount()]
    if not blocks:
        return None
    if len(blocks) > 1:
        raise ParseError("AMBIGUOUS_PRICE", "More than one product price is displayed.")
    block = blocks[0]
    regular, sale, compare = (block.single(kind) for kind in ("regular", "sale", "compare"))
    if block.on_sale:
        if not sale or not compare:
            raise ParseError(
                "AMBIGUOUS_PRICE", "The sale price block lacks a sale or compare-at price."
            )
        current = _amount(sale, "displayed sale price")
        if regular and _amount(regular, "displayed price") != current:
            raise ParseError("PRICE_CONFLICT", "The regular and sale containers disagree.")
        was = _amount(compare, "displayed compare-at price")
        if was < current:
            raise ParseError("PRICE_CONFLICT", "The compare-at price is below the current price.")
        if was == current:
            return _ShownPrice(current, sale, None, "")
        return _ShownPrice(current, sale, was, compare)
    if not regular:
        raise ParseError("AMBIGUOUS_PRICE", "The regular price container has no amount.")
    current = _amount(regular, "displayed price")
    if sale and _amount(sale, "displayed sale price") != current:
        raise ParseError("PRICE_CONFLICT", "The regular and sale containers disagree.")
    if compare and _amount(compare, "displayed compare-at price") > current:
        raise ParseError(
            "PRICE_CONFLICT", "A higher compare-at price is present but not marked as a sale."
        )
    return _ShownPrice(current, regular, None, "")


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
    selection: _Selection, buttons: list[_Button], blocks: list[_PriceBlock]
) -> tuple[str, str]:
    """Combine JSON-LD availability, the variant's ``available`` flag, the add-to-cart button,
    and Dawn's ``price--sold-out`` state.

    JSON-LD, the variant flag, the sold-out price state, and the "Sold out", "Pre-order", and
    "Unavailable" labels are explicit signals and must agree. The add-to-cart button and the
    price state must also be consistent: enabled "Add to cart" and no sold-out state for in
    stock, enabled "Pre-order" or "Add to cart" for preorder, absent or disabled or "Sold out"
    for out of stock. Disagreement, several different buttons, or no explicit signal gives
    UNKNOWN; price visibility and an "Add to cart" label alone never decide stock.
    """
    token = ""
    if selection.offer:
        token = _text(selection.offer.get("availability")).rstrip("/").rsplit("/", 1)[-1]
    variant = selection.variant
    flag = variant.available if variant else None
    sold_out_state = any(block.sold_out for block in blocks)
    distinct = list(dict.fromkeys(buttons))
    conflict = len(distinct) > 1
    button = distinct[0] if len(distinct) == 1 else None
    label = button.label.lower() if button else ""

    explicit: list[Availability] = []
    if token:
        # Unmapped values (BackOrder, LimitedAvailability, and the like) count as UNKNOWN.
        explicit.append(_JSONLD_AVAILABILITY.get(token.lower(), Availability.UNKNOWN))
    if flag is not None:
        explicit.append(Availability.IN_STOCK if flag else Availability.OUT_OF_STOCK)
    if sold_out_state:
        explicit.append(Availability.OUT_OF_STOCK)
    if button is not None:
        from_label = _BUTTON_LABELS.get(label, Availability.UNKNOWN)
        if from_label is not None:
            explicit.append(from_label)
    value = Availability.UNKNOWN
    if explicit and not conflict and len(set(explicit)) == 1:
        value = explicit[0]
        enabled = button is not None and not button.disabled
        consistent = {
            Availability.IN_STOCK: enabled and label == "add to cart" and not sold_out_state,
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
        "variant available: " + ("none" if flag is None else str(flag).lower()),
        "add to cart button: " + (shown or "absent"),
        "price sold-out state: " + ("yes" if sold_out_state else "no"),
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


def _brand(product: dict) -> str:
    """Brand only when JSON-LD states it; never guessed from the title."""
    brand = product.get("brand")
    if isinstance(brand, dict):
        return _text(brand.get("name"))
    return _text(brand)


def _path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    # RFC 3986 normal form (url_paths.py): ``%e2`` and ``%E2`` escapes of one handle compare
    # equal; an escaped "/" stays distinct from a separator (2026-10-19, independent review).
    return normalized_path(value)


def _first(values: list[str]) -> str:
    return values[0] if values else ""


# HTML parsing


@dataclass
class _PriceBlock:
    on_sale: bool
    sold_out: bool
    amounts: dict[str, list[str]] = field(
        default_factory=lambda: {"regular": [], "sale": [], "compare": []}
    )

    def has_amount(self) -> bool:
        return any(re.search(r"[0-9]", text) for texts in self.amounts.values() for text in texts)

    def single(self, kind: str) -> str:
        """The one amount of ``kind`` with digits; blank when none; ambiguous when several."""
        texts = [text for text in self.amounts[kind] if re.search(r"[0-9]", text)]
        if len(texts) > 1:
            raise ParseError("AMBIGUOUS_PRICE", "A price container shows several amounts.")
        return _first(texts)


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
    container: str = ""
    variant_picker: bool = False
    buffer: list[str] | None = None
    on_close: Callable[[str], None] | None = None
    # Child <span> elements closed so far (description paragraphs only).
    spans_closed: int = 0


class _ProductPageParser(HTMLParser):
    """Collects only the main product's markup.

    "Main" means inside Dawn's ``<product-info>`` (``.product__info-container``). The
    description is ``.product__description`` inside it. Everything inside ``<template>``,
    hidden text (``visually-hidden``, ``hidden``), badges, and the unit-price line is excluded;
    header, cart drawer, and recommendation cards are outside the main block.
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
        # (option name, value) of every checked radio or selected option-select in the picker.
        self.checked_options: list[tuple[str, str]] = []
        # Values of <option selected> in the product form's variant <select name="id">.
        self.selected_option_ids: list[str] = []
        self.only_default_variant = ""
        self.price_blocks: list[_PriceBlock] = []
        self.buttons: list[_Button] = []
        self.description: list[str] = []
        self.canonical = ""
        self._block: _PriceBlock | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        classes = frozenset(attributes.get("class", "").split())
        parent = self.stack[-1] if self.stack else None
        is_main = tag == "product-info" or "product__info-container" in classes
        element = _Element(
            tag,
            classes,
            attributes,
            template=tag == "template" or bool(parent and parent.template),
            main=is_main or bool(parent and parent.main),
            description="product__description" in classes or bool(parent and parent.description),
            hidden=bool(classes & _HIDDEN_CLASSES)
            or "hidden" in attributes
            or bool(parent and parent.hidden),
            container=(
                "regular" if "price__regular" in classes
                else "sale" if "price__sale" in classes
                else parent.container if parent else ""
            ),
            variant_picker=tag in {"variant-radios", "variant-selects"}
            or bool(parent and parent.variant_picker),
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
        elif element.main:
            self._main_tag(element)

    def _main_tag(self, element: _Element) -> None:
        tag, classes, attributes = element.tag, element.classes, element.attrs
        if "data-has-only-default-variant" in attributes:
            self.only_default_variant = (
                self.only_default_variant or attributes["data-has-only-default-variant"]
            )
        is_json = attributes.get("type") == "application/json"
        if tag == "script" and element.variant_picker and is_json:
            self._capture(element, self.variant_json.append)
        elif tag == "h1" and any("product__title" in e.classes for e in self.stack):
            self._capture(element, lambda text: self.titles.append(clean_text(text)))
        elif tag == "div" and "price" in classes and self._block is None:
            block = _PriceBlock("price--on-sale" in classes, "price--sold-out" in classes)
            self.price_blocks.append(block)
            self._block = block
            element.on_close = self._end_price_block
        elif self._block is not None and "price-item" in classes and not element.hidden:
            kind = {
                ("regular", "price-item--regular"): "regular",
                ("sale", "price-item--regular"): "compare",
                ("sale", "price-item--sale"): "sale",
            }.get((element.container, _item_kind(classes)))
            if kind:
                texts = self._block.amounts[kind]
                self._capture(element, lambda text: texts.append(clean_text(text)))
        elif tag == "button" and "product-form__submit" in classes:
            disabled = "disabled" in attributes or attributes.get("aria-disabled") == "true"
            self._capture(
                element, lambda text: self.buttons.append(_Button(clean_text(text), disabled))
            )
        elif tag == "input" and attributes.get("name") == "product-id":
            self.product_ids.append(attributes.get("value", "").strip())
        elif tag == "input" and attributes.get("name") == "id":
            self.form_variant_ids.append(attributes.get("value", "").strip())
        elif tag == "option" and any(
            e.tag == "select" and e.attrs.get("name") == "id" for e in self.stack
        ):
            self.option_values.append(attributes.get("value", "").strip())
            if "selected" in attributes:
                self.selected_option_ids.append(attributes.get("value", "").strip())
        elif (
            tag == "input"
            and element.variant_picker
            and attributes.get("type") == "radio"
            and "checked" in attributes
        ):
            self.checked_options.append(
                (clean_text(attributes.get("name", "")), clean_text(attributes.get("value", "")))
            )
        elif tag == "option" and element.variant_picker and "selected" in attributes:
            select = next((e for e in reversed(self.stack) if e.tag == "select"), None)
            name = select.attrs.get("name", "") if select else ""
            match = re.fullmatch(r"options\[(.+)\]", name)
            if match:
                self.checked_options.append(
                    (clean_text(match[1]), clean_text(attributes.get("value", "")))
                )
        elif element.description and tag == "div" and "product__description" in classes:
            self._capture(element, self.description.append)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                closing = self.stack[index]
                parent = self.stack[index - 1] if index else None
                if tag in _LINE_END_TAGS:
                    self.handle_data("\n")
                elif tag in _CELL_TAGS:
                    self.handle_data(" ")
                # Close this element and any unclosed elements nested inside it.
                while len(self.stack) > index:
                    closed = self.stack.pop()
                    if closed.on_close is not None:
                        closed.on_close("".join(closed.buffer or []))
                if (
                    tag == "span" and closing.description
                    and parent is not None and parent.tag == "p"
                ):
                    parent.spans_closed += 1
                    if parent.spans_closed == 1:
                        # Marks a possible label/value boundary; see description_lines.
                        self.handle_data(_SPAN_BOUNDARY)
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

        A paragraph written as ``<span>Label </span><span>value</span>`` is split at that span
        boundary when the first span is a short label (up to four words, no digits other than a
        trailing footnote, no colon) and the rest is not empty. A line without either is kept
        with an empty value. Nothing is renamed or inferred.
        """
        rows: list[tuple[str, str]] = []
        for line in "\n".join(self.description[:1]).split("\n"):
            if _SPAN_BOUNDARY in line:
                label, _, value = line.partition(_SPAN_BOUNDARY)
                label, value = clean_text(label), clean_text(value.replace(_SPAN_BOUNDARY, ""))
                # "<span>Label</span>: value" keeps the earlier colon split below.
                if value and not value.startswith(":") and _is_short_label(label):
                    rows.append((label, value))
                    continue
                line = line.replace(_SPAN_BOUNDARY, "")
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


def _item_kind(classes: frozenset[str]) -> str:
    if "price-item--sale" in classes:
        return "price-item--sale"
    if "price-item--regular" in classes:
        return "price-item--regular"
    return ""
