"""Offline tests for the Octagon (OCT) adapter. Local fixtures and fake transport only."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, FakeTransport, no_network  # noqa: F401

from laptop_tracker.adapters.oct import (
    DATA_USE_REVIEW_STATUS,
    HOST,
    STORE_NAME,
    OCTAdapter,
    product_request,
)
from laptop_tracker.offers import Availability, PriceType
from laptop_tracker.scraping import (
    FetchConfig,
    Fetcher,
    ScrapeRequest,
    ScrapeResult,
    Stage,
    TransportResponse,
    scrape,
)

pytestmark = pytest.mark.usefixtures("no_network")

FIXTURES = Path(__file__).resolve().parent / "scraping_fixtures" / "oct"
REGULAR_URL = (
    f"https://{HOST}/products/example-book-14-ex14-0001ph-core-i5-13420h-8gb-512gb-ssd-14-win-11-gry"
)
REGULAR_VARIANT = "50000000000101"
SALE_URL = (
    f"https://{HOST}/products/"
    "example-gamer-16-eg16-0002ph-ryzen-7-7735hs-16gb-1tb-ssd-rtx-4050-6gb-16-win-11"
)
VARIANTS_URL = (
    f"https://{HOST}/products/example-slim-15-es15-0003ph-core-5-120u-512gb-ssd-15-6-fhd-win-11"
)
V1, V2 = "50000000000301", "50000000000302"
V1_URL = f"{VARIANTS_URL}?variant={V1}"
V2_URL = f"{VARIANTS_URL}?variant={V2}"
HTML = {"content-type": "text/html; charset=utf-8"}

REGULAR = "oct_product_regular.html"
SALE = "oct_product_sale.html"
VARIANTS = "oct_product_variants.html"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def mutate(text: str, old: str, new: str, count: int = -1) -> str:
    assert old in text, f"fixture no longer contains {old!r}"
    return text.replace(old, new, count)


def cut(text: str, start: str, end: str) -> str:
    """Remove ``text`` from the first ``start`` through the next ``end``, inclusive."""
    begin = text.index(start)
    return text[:begin] + text[text.index(end, begin) + len(end) :]


def run(
    body: str, url: str = REGULAR_URL, *, live: bool = False, adapter: object | None = None
) -> tuple[ScrapeResult, FakeTransport]:
    clock = FakeClock()
    response = TransportResponse(200, url, body.encode("utf-8"), HTML)
    transport = FakeTransport(clock, response, live=live)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(adapter or OCTAdapter(), [ScrapeRequest("oct", url)], fetcher)
    return result, transport


def only_offer(body: str, url: str = REGULAR_URL):
    result, _ = run(body, url)
    assert result.ok, result.failures
    [observation] = result.observations
    return observation


def parse_failure(body: str, url: str = REGULAR_URL) -> tuple[Stage, str]:
    result, _ = run(body, url)
    assert not result.observations
    [failure] = result.failures
    return failure.stage, failure.error_code


IN_STOCK_EVIDENCE = (
    "JSON-LD availability: InStock; stock alert: Only 2 left in stock!; "
    "add to cart button: Add to cart (enabled)"
)
PRODUCT_LD = '<script type="application/ld+json">\n    {\n        "@context": "http://schema.org/"'
PRICE_BLOCK = '<dl class="#price ">'
ALERT = '<div class="#product-stock-alert-text">Only 2 left in stock!</div>'
BUTTON = 'type="submit" data-element="to-cart-button"'
ADD_LABEL = 'data-element="to-cart-button">Add to cart '
SHOWN = '<span class="#price-value">₱44,999</span>'


# Single default variant at a regular price


def test_regular_product_yields_an_attributed_offer() -> None:
    offer = only_offer(fixture(REGULAR))

    assert offer.retailer_id == "oct"
    assert offer.offer_url == f"{REGULAR_URL}?variant={REGULAR_VARIANT}"
    assert offer.raw_title == (
        "EXAMPLE BOOK 14 (EX14-0001PH)/ CORE I5-13420H/ 8GB/ 512GB SSD/ 14/ WIN 11/ GRY"
    )
    assert (offer.price, offer.currency, offer.raw_price_text) == (
        Decimal("44999"),
        "PHP",
        "₱44,999",
    )
    assert offer.availability is Availability.IN_STOCK
    assert offer.availability_evidence == IN_STOCK_EVIDENCE
    assert offer.source.adapter_version == "oct-1"
    details = offer.details
    assert details.price_type is PriceType.REGULAR
    assert (details.regular_price, details.regular_price_text) == (Decimal("44999"), "₱44,999")
    assert (details.sale_price, details.sale_price_text) == (None, "")
    assert details.retailer_product_id == "9000000000101"
    assert details.retailer_sku == "190000000101"
    assert details.manufacturer_part_number == ""
    assert details.raw_categories == ("Laptop",)
    assert details.bundle_flag is False and details.bundle_evidence == ()


def test_store_name_in_jsonld_brand_and_vendor_is_never_the_brand() -> None:
    offer = only_offer(fixture(REGULAR))
    assert offer.details.brand == ""
    # Even a maker name in JSON-LD is not trusted: the theme fills it from the vendor field.
    body = mutate(fixture(REGULAR), f'"name": "{STORE_NAME}"', '"name": "ExampleBrand"')
    assert only_offer(body).details.brand == ""


def test_tech_specification_lines_are_the_raw_specifications() -> None:
    offer = only_offer(fixture(REGULAR))
    specs = offer.details.raw_specifications
    assert specs[:4] == (
        ("EXAMPLE BOOK 14 (EX14-0001PH)/ CORE I5-13420H/ 8GB/ 512GB SSD/ 14/ WIN 11/ GRY", ""),
        ("Processor", "Intel® Core™ i5-13420H, 8C / 12T, 12MB"),
        ("Graphics", "Integrated Intel® UHD Graphics"),
        ("Chipset", "Intel® SoC Platform"),
    )
    assert ("DESIGN", "") in specs
    assert dict(specs)["Display"] == '14" WUXGA (1920x1200) IPS 300nits Anti-glare'
    # The recommendations section's rich text is not a specification.
    assert not any("Recommended" in label for label, _ in specs)
    assert offer.raw_configuration_text.startswith(
        "EXAMPLE BOOK 14 (EX14-0001PH)/ CORE I5-13420H/ 8GB/ 512GB SSD/ 14/ WIN 11/ GRY | "
        "Processor: Intel® Core™ i5-13420H"
    )
    # Nothing is inferred: no RAM or refresh-rate field is created.
    assert not any("refresh" in label.lower() for label, _ in specs)


def test_cards_badges_and_hidden_text_are_ignored() -> None:
    # A sold-out, on-sale recommendation card (PHP 1,499 / 1,699) and visually hidden share
    # text must not affect the main offer.
    offer = only_offer(fixture(REGULAR))
    assert offer.availability is Availability.IN_STOCK
    assert "sold out" not in offer.availability_evidence.lower()
    assert Decimal("1499") not in (offer.price, offer.details.regular_price)
    assert offer.details.price_type is PriceType.REGULAR


def test_description_falls_back_to_jsonld_without_the_spec_section() -> None:
    body = mutate(fixture(REGULAR), "Tech Specification", "About this item")
    offer = only_offer(body)
    assert offer.details.raw_specifications == ()
    assert offer.raw_configuration_text.endswith("Memory: 8GB Soldered DDR5-4800")


def test_normalization_is_deterministic() -> None:
    first = only_offer(fixture(REGULAR)).to_full_record()
    assert first == only_offer(fixture(REGULAR)).to_full_record()
    assert "target_id" not in first


# Sale price and the theme's JSON-LD defect


def test_sale_product_records_sale_and_regular_prices() -> None:
    offer = only_offer(fixture(SALE), SALE_URL)

    assert (offer.price, offer.raw_price_text) == (Decimal("59999"), "₱59,999")
    details = offer.details
    assert details.price_type is PriceType.SALE
    assert (details.sale_price, details.sale_price_text) == (Decimal("59999"), "₱59,999")
    assert (details.regular_price, details.regular_price_text) == (Decimal("64999"), "₱64,999")
    # The "You save 8%" item is neither a price nor an amount.
    assert "8%" not in (offer.raw_price_text, details.regular_price_text)
    assert details.raw_specifications[1] == ("✅ AMD RYZEN 7 7735HS", "")


def test_leading_zero_barcode_in_jsonld_is_read_and_kept_as_text() -> None:
    # The theme writes "gtin12": 012300000202, which is invalid JSON. The adapter quotes it,
    # so JSON-LD still supplies availability and the leading-zero SKU is kept as text.
    offer = only_offer(fixture(SALE), SALE_URL)
    assert offer.details.retailer_sku == "012300000202"
    assert offer.availability is Availability.IN_STOCK
    assert offer.availability_evidence.startswith(
        "JSON-LD availability: InStock; stock alert: none"
    )
    assert offer.offer_url == f"{SALE_URL}?variant=50000000000202"


COMPARE = '<span class="#price-value">₱64,999</span>'


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        (COMPARE, COMPARE.replace("64,999", "55,000"), "PRICE_CONFLICT"),  # below the sale price
        (COMPARE, '<span class="#price-value"></span>', "AMBIGUOUS_PRICE"),  # no regular price
        ("@regular @on-sale ", "@regular ", "PRICE_CONFLICT"),  # higher price, not on sale
        ('"price" : 59999.0,', '"price" : 64999.0,', "PRICE_CONFLICT"),
        ('"price":5999900', '"price":6499900', "PRICE_CONFLICT"),
        (COMPARE, COMPARE + COMPARE.replace("64,999", "69,999"), "AMBIGUOUS_PRICE"),
    ],
)
def test_unclear_sale_prices_are_parse_failures(old: str, new: str, code: str) -> None:
    assert parse_failure(mutate(fixture(SALE), old, new), SALE_URL) == (Stage.PARSE, code)


def test_regular_price_equal_to_the_sale_price_is_regular() -> None:
    body = mutate(fixture(SALE), COMPARE, COMPARE.replace("64,999", "59,999"))
    details = only_offer(body, SALE_URL).details
    assert details.price_type is PriceType.REGULAR
    assert (details.regular_price, details.sale_price) == (Decimal("59999"), None)


def test_unrepairable_jsonld_is_ignored_and_visible_content_is_used() -> None:
    body = mutate(fixture(REGULAR), '"@type": "Product",', '"@type": "Product"')
    offer = only_offer(body)
    assert offer.price == Decimal("44999")
    # The stock alert and the button still agree without JSON-LD.
    assert offer.availability is Availability.IN_STOCK
    assert offer.availability_evidence.startswith("JSON-LD availability: none; stock alert: Only")


# Prices and currency


def test_jsonld_price_alone_gives_no_sale_or_regular_claim() -> None:
    offer = only_offer(cut(fixture(REGULAR), PRICE_BLOCK, "</dl>"))
    assert (offer.price, offer.raw_price_text) == (Decimal("44999.0"), "44999.0")
    assert offer.details.price_type is None
    assert (offer.details.regular_price, offer.details.sale_price) == (None, None)


def test_jsonld_price_alone_requires_php_currency() -> None:
    body = cut(fixture(REGULAR), PRICE_BLOCK, "</dl>")
    body = mutate(body, '"priceCurrency" : "PHP",', "")
    assert parse_failure(body) == (Stage.PARSE, "CURRENCY_NOT_FOUND")


def test_no_price_anywhere_is_price_not_found() -> None:
    body = cut(fixture(REGULAR), PRODUCT_LD, "</script>")
    body = mutate(body, SHOWN, '<span class="#price-value">Call for price</span>')
    assert parse_failure(body) == (Stage.PARSE, "PRICE_NOT_FOUND")


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        (SHOWN, SHOWN.replace("44,999", "45,999"), "PRICE_CONFLICT"),  # vs JSON-LD/analytics
        ('"price" : 44999.0,', '"price" : 45999.0,', "PRICE_CONFLICT"),
        ('"price":4499900', '"price":4599900', "PRICE_CONFLICT"),
        ('"priceCurrency" : "PHP"', '"priceCurrency" : "USD"', "UNSUPPORTED_CURRENCY"),
        (SHOWN, SHOWN.replace("₱", "$"), "UNSUPPORTED_CURRENCY"),
        (SHOWN, SHOWN.replace("₱44,999", "₱44,999 USD"), "UNSUPPORTED_CURRENCY"),
        (SHOWN, SHOWN.replace("₱", ""), "CURRENCY_NOT_FOUND"),
        (SHOWN, SHOWN.replace("₱", "From ₱"), "AMBIGUOUS_PRICE"),
        (SHOWN, SHOWN.replace("₱44,999", "₱39,999 – ₱44,999"), "AMBIGUOUS_PRICE"),
        (SHOWN, SHOWN.replace("44,999", "0"), "INVALID_PRICE"),
        (SHOWN, SHOWN * 2, "AMBIGUOUS_PRICE"),
        ('<div class="#product-price-tax-note">',
         '<dl class="#price "><div class="#price-item @regular"><span class="#price-value">'
         '₱1,999</span></div></dl><div class="#product-price-tax-note">', "AMBIGUOUS_PRICE"),
        ('"@type" : "Offer"', '"@type" : "AggregateOffer"', "AMBIGUOUS_PRICE"),
        ('<div class="#price-item @regular  ">', '<div class="#price-item @unit">',
         "AMBIGUOUS_PRICE"),
    ],
)
def test_unclear_prices_are_parse_failures(old: str, new: str, code: str) -> None:
    assert parse_failure(mutate(fixture(REGULAR), old, new)) == (Stage.PARSE, code)


@pytest.mark.parametrize(
    ("amount", "price"),
    [("44,999 PHP", "44999"), ("PHP 44,999", "44999"), ("₱44,999.00", "44999.00")],
)
def test_php_code_and_decimals_are_accepted(amount: str, price: str) -> None:
    offer = only_offer(mutate(fixture(REGULAR), SHOWN, SHOWN.replace("₱44,999", amount)))
    assert (offer.price, offer.currency) == (Decimal(price), "PHP")


# Variants


def test_selected_variant_yields_an_attributed_offer() -> None:
    offer = only_offer(fixture(VARIANTS), V1_URL)

    assert offer.offer_url == V1_URL
    assert offer.price == Decimal("38999")
    assert offer.details.retailer_sku == "190000000301"
    assert offer.availability is Availability.IN_STOCK
    # The variant title comes first: the page title does not say which memory option.
    assert offer.raw_configuration_text.startswith("8GB | Processor: Intel® Core™ 5 120U")


def test_bare_url_on_a_multi_variant_page_is_ambiguous() -> None:
    assert parse_failure(fixture(VARIANTS), VARIANTS_URL) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


def test_requested_variant_must_be_the_one_the_page_selected() -> None:
    assert parse_failure(fixture(VARIANTS), V2_URL) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


def rendered_for_v2(body: str) -> str:
    body = mutate(body, f'name="id" value="{V1}"', f'name="id" value="{V2}"')
    body = mutate(body, "SKU: 190000000301", "SKU: 190000000302")
    return mutate(body, "₱38,999", "₱42,999")


def test_second_variant_page_is_parsed_for_that_variant() -> None:
    body = rendered_for_v2(fixture(VARIANTS))
    body = mutate(body, BUTTON + ">Add to cart", BUTTON + " disabled>Sold out")
    offer = only_offer(body, V2_URL)
    assert (offer.offer_url, offer.price) == (V2_URL, Decimal("42999"))
    assert offer.details.retailer_sku == "190000000302"
    assert offer.raw_configuration_text.startswith("16GB | ")
    assert offer.availability is Availability.OUT_OF_STOCK
    assert offer.availability_evidence == (
        "JSON-LD availability: OutOfStock; stock alert: none; "
        "add to cart button: Sold out (disabled)"
    )


def test_out_of_stock_variant_with_an_enabled_add_to_cart_button_is_a_conflict() -> None:
    offer = only_offer(rendered_for_v2(fixture(VARIANTS)), V2_URL)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.endswith("signals conflict")


def test_requested_variant_missing_from_the_variant_data_is_ambiguous() -> None:
    unknown = "50000000000309"
    body = mutate(fixture(VARIANTS), f'name="id" value="{V1}"', f'name="id" value="{unknown}"')
    assert parse_failure(body, f"{VARIANTS_URL}?variant={unknown}") == (
        Stage.PARSE,
        "AMBIGUOUS_VARIANT",
    )


def test_forms_selecting_two_variants_are_ambiguous() -> None:
    body = mutate(fixture(VARIANTS), f'name="id" value="{V1}"', f'name="id" value="{V2}"', 1)
    assert parse_failure(body, V1_URL) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


def test_url_and_redirect_naming_different_variants_is_ambiguous() -> None:
    clock = FakeClock()
    moved = TransportResponse(301, V2_URL, b"", {"location": V1_URL})
    page = TransportResponse(200, V1_URL, fixture(VARIANTS).encode(), HTML)
    transport = FakeTransport(clock, moved, page)
    [result] = scrape(OCTAdapter(), [product_request(V2_URL)], Fetcher(transport, None, clock))
    assert (result.failures[0].stage, result.failures[0].error_code) == (
        Stage.PARSE,
        "AMBIGUOUS_VARIANT",
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"options": ["Default Title"],\n    "imagePosition": null\n  }',
         '"options": ["Default Title"],\n    "imagePosition": null\n  }, '
         '"50000000000102": {"options": ["16GB"]}'),
        ('"sku":"190000000101"}]', '"sku":"190000000101"},{"id":50000000000102,"price":4899900}]'),
        ('"price" : 44999.0,', '"price" : 44999.0, "priceCurrency" : "PHP"}, {"@type" : "Offer", '
         '"price" : 48999.0,'),
    ],
)
def test_any_variant_signal_on_a_bare_url_is_ambiguous(old: str, new: str) -> None:
    assert parse_failure(mutate(fixture(REGULAR), old, new)) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('{"50000000000101": {', '{"50000000000101": {,'),  # unreadable variants-data
        ('"options": ["Default Title"]', '"options": "Default Title"'),  # unknown shape
        ("SKU: 190000000101", "SKU: 190000000199"),  # shown SKU names another variant
        ('"@type" : "Offer","sku": "190000000101"', '"@type" : "Offer","sku": "190000000199"'),
        ("SKU: 190000000101", "Item code 190000000101"),
    ],
)
def test_inconsistent_variant_data_is_ambiguous(old: str, new: str) -> None:
    assert parse_failure(mutate(fixture(REGULAR), old, new)) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


def test_empty_sku_line_states_no_sku() -> None:
    body = mutate(fixture(REGULAR), "SKU: 190000000101", "SKU:")
    assert only_offer(body).details.retailer_sku == "190000000101"  # from JSON-LD and analytics


def test_matching_variant_query_on_a_single_variant_page_is_accepted() -> None:
    url = f"{REGULAR_URL}?variant={REGULAR_VARIANT}"
    assert only_offer(fixture(REGULAR), url).price == Decimal("44999")


def test_other_variant_query_on_a_single_variant_page_is_ambiguous() -> None:
    url = f"{REGULAR_URL}?variant=50000000000199"
    assert parse_failure(fixture(REGULAR), url) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


# Availability


def sold_out(body: str) -> str:
    """The theme's sold-out rendering of the regular fixture, as its strings suggest."""
    body = mutate(body, "InStock", "OutOfStock")
    body = cut(body, '<div class="#product-meta-block">\n          <product-variant-listener '
               'id="b-stock_alert">', "</product-variant-listener>")
    return mutate(body, ADD_LABEL, 'disabled data-element="to-cart-button">Sold out ')


def test_sold_out_markup_is_out_of_stock() -> None:
    offer = only_offer(sold_out(fixture(REGULAR)))
    assert offer.availability is Availability.OUT_OF_STOCK
    assert offer.availability_evidence == (
        "JSON-LD availability: OutOfStock; stock alert: none; "
        "add to cart button: Sold out (disabled)"
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("InStock", "OutOfStock"),
        (ALERT, ALERT.replace("Only 2 left in stock!", "Out of stock")),
        (ALERT, ALERT.replace("Only 2 left in stock!", "Low stock - order soon")),
        (ALERT, ALERT + ALERT.replace("Only 2", "Only 5")),
        (BUTTON, BUTTON + " disabled"),
        (BUTTON, BUTTON + ' aria-disabled="true"'),
        (ADD_LABEL, 'data-element="to-cart-button">Sold out '),
        (ADD_LABEL, 'data-element="to-cart-button">Unavailable '),
        (ADD_LABEL, 'data-element="to-cart-button">Pre-order '),
    ],
)
def test_conflicting_stock_signals_are_unknown(old: str, new: str) -> None:
    offer = only_offer(mutate(fixture(REGULAR), old, new))
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.endswith("signals conflict")


def test_unrecognized_stock_alert_is_kept_as_evidence() -> None:
    body = mutate(fixture(REGULAR), "Only 2 left in stock!", "Ships in 3-5 days")
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert "stock alert: Ships in 3-5 days" in offer.availability_evidence


def test_unmapped_jsonld_availability_is_unknown() -> None:
    body = cut(mutate(fixture(REGULAR), "InStock", "BackOrder"), ALERT, ALERT)
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert "JSON-LD availability: BackOrder" in offer.availability_evidence


def test_missing_button_contradicts_in_stock() -> None:
    body = cut(fixture(REGULAR), "<button ", "</button>")
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert "add to cart button: absent" in offer.availability_evidence


def test_out_of_stock_with_an_enabled_add_to_cart_button_is_a_conflict() -> None:
    body = mutate(sold_out(fixture(REGULAR)), 'disabled data-element="to-cart-button">Sold out ',
                  ADD_LABEL)
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.endswith("signals conflict")


def test_preorder_needs_jsonld_and_button_to_agree() -> None:
    body = cut(mutate(fixture(REGULAR), "InStock", "PreOrder"), ALERT, ALERT)
    body = mutate(body, ADD_LABEL, 'data-element="to-cart-button">Pre-order ')
    offer = only_offer(body)
    assert offer.availability is Availability.PREORDER
    assert "add to cart button: Pre-order (enabled)" in offer.availability_evidence


def test_several_different_buttons_are_a_conflict() -> None:
    extra = '</product-to-cart><button type="submit" data-element="to-cart-button" disabled>' \
            "Sold out</button>"
    body = mutate(fixture(REGULAR), "</product-to-cart>", extra)
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.endswith("signals conflict")


def test_add_to_cart_alone_is_not_a_stock_signal() -> None:
    # The sale fixture has no stock alert; without JSON-LD only the button remains.
    body = cut(fixture(SALE), '<script type="application/ld+json">', "</script>")
    offer = only_offer(body, SALE_URL)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence == (
        "JSON-LD availability: none; stock alert: none; add to cart button: Add to cart (enabled)"
    )


# Bundles


def test_bundle_listing_is_flagged_with_evidence_and_not_dropped() -> None:
    body = mutate(fixture(REGULAR), "WIN 11/ GRY</h1>", "WIN 11/ GRY + FREE EXAMPLE BACKPACK</h1>")
    # The analytics variant name and the JSON-LD name carry the same title.
    body = mutate(body, 'WIN 11\\/ GRY"', 'WIN 11\\/ GRY + FREE EXAMPLE BACKPACK"')
    offer = only_offer(body)
    assert offer.raw_title.endswith("+ FREE EXAMPLE BACKPACK")
    assert offer.details.bundle_flag is True
    assert offer.details.bundle_evidence == (
        "title term FREE: + FREE",
        "title term BACKPACK: BACKPACK",
    )
    assert offer.price == Decimal("44999")


def test_bundle_wording_in_the_variant_title_is_flagged() -> None:
    body = mutate(fixture(VARIANTS), '"options": ["8GB"]', '"options": ["8GB + Mouse"]')
    offer = only_offer(body, V1_URL)
    assert offer.details.bundle_flag is True
    assert offer.details.bundle_evidence == ("variant term MOUSE: Mouse",)


# Layout and identity


def test_unrecognized_layout_is_a_parse_failure() -> None:
    assert parse_failure(fixture("oct_layout_changed.html")) == (
        Stage.PARSE,
        "UNRECOGNIZED_LAYOUT",
    )


TITLE_H1 = (
    '<h1 class="#heading"> EXAMPLE BOOK 14 (EX14-0001PH)/ CORE I5-13420H/ 8GB/ 512GB SSD/ 14/ '
    "WIN 11/ GRY</h1>"
)
LD_NAME = (
    '"name": "EXAMPLE BOOK 14 (EX14-0001PH)\\/ CORE I5-13420H\\/ 8GB\\/ 512GB SSD\\/ 14\\/ '
    'WIN 11\\/ GRY",'
)


def test_no_heading_and_no_jsonld_name_has_no_title() -> None:
    body = mutate(mutate(fixture(REGULAR), TITLE_H1, ""), LD_NAME, "")
    assert parse_failure(body) == (Stage.PARSE, "TITLE_NOT_FOUND")


def test_jsonld_name_is_the_title_without_a_heading() -> None:
    body = mutate(fixture(REGULAR), TITLE_H1, "")
    assert only_offer(body).raw_title.startswith("EXAMPLE BOOK 14 (EX14-0001PH)/ CORE I5")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("WIN 11/ GRY</h1>", "WIN 11/ GRY</h1><h1>OTHER LAPTOP</h1>"),
        ("WIN 11/ GRY</h1>", "WIN 11/ GRY (REFURBISHED)</h1>"),  # heading vs JSON-LD name
        ('name="product-id" value="9000000000101"', 'name="product-id" value="9000000000109"'),
        ('<link rel="canonical" href="https://www.octagon.com.ph/products/example-book',
         '<link rel="canonical" href="https://www.octagon.com.ph/products/other-book'),
        ('"handle":"example-book-14', '"handle":"other-book-14'),
        ("</product-variant-provider>", '<div class="#product"><h1>OTHER</h1></div>'
                                        "</product-variant-provider>"),
        ("</head>", '<script type="application/ld+json">{"@type":"Product","name":"Other",'
                    '"url":"https://www.octagon.com.ph/products/other"}</script></head>'),
        ("</head>", '<script>var meta = {"product":{"id":9000000000109}};</script></head>'),
    ],
)
def test_disagreeing_product_identity_is_ambiguous(old: str, new: str) -> None:
    assert parse_failure(mutate(fixture(REGULAR), old, new, 1)) == (
        Stage.PARSE,
        "AMBIGUOUS_PRODUCT",
    )


def test_page_for_another_product_url_is_ambiguous() -> None:
    # For example a redirect or theme fallback that serves another product's page.
    other = f"https://{HOST}/products/example-book-14-other-handle"
    assert parse_failure(fixture(REGULAR), other) == (Stage.PARSE, "AMBIGUOUS_PRODUCT")


def test_offer_links_to_other_hosts_are_rejected() -> None:
    body = mutate(
        fixture(REGULAR),
        '"url" : "https:\\/\\/www.octagon.com.ph\\/products\\/',
        '"url" : "https:\\/\\/elsewhere.example.test\\/products\\/',
    )
    result, _ = run(body)
    assert not result.observations
    [failure] = result.failures
    assert (failure.stage, failure.message.split(":")[0]) == (Stage.VALIDATE, "offer_url")


# Hosts, redirects, and request building


@pytest.mark.parametrize(
    "host",
    [
        "octagon.com.ph",
        "octagoncomputer-superstore.myshopify.com",
        "shop.octagon.com.ph",
        "www.octagon.com.ph.example.test",
        "www.lazada.com.ph",
    ],
)
def test_only_the_exact_confirmed_host_is_requested(host: str) -> None:
    result, transport = run("", f"https://{host}/products/x")
    assert transport.calls == []
    assert result.failures[0].error_code == "HOST_NOT_ALLOWED"


@pytest.mark.parametrize(
    "location",
    [
        "https://octagon.com.ph/products/x",
        "https://octagoncomputer-superstore.myshopify.com/products/x",
        "https://elsewhere.example.test/landing/",
    ],
)
def test_redirect_off_the_confirmed_host_is_not_followed(location: str) -> None:
    clock = FakeClock()
    moved = TransportResponse(301, REGULAR_URL, b"", {"location": location})
    transport = FakeTransport(clock, moved)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(OCTAdapter(), [product_request(REGULAR_URL)], fetcher)
    assert [call["url"] for call in transport.calls] == [REGULAR_URL]
    assert result.failures[0].error_code == "REDIRECT_HOST_NOT_ALLOWED"


def test_redirect_within_the_confirmed_host_is_followed() -> None:
    old_url = f"https://{HOST}/products/example-book-14-old-handle"
    clock = FakeClock()
    location = REGULAR_URL.removeprefix(f"https://{HOST}")  # relative Location header
    moved = TransportResponse(301, old_url, b"", {"location": location})
    page = TransportResponse(200, REGULAR_URL, fixture(REGULAR).encode(), HTML)
    transport = FakeTransport(clock, moved, page)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(OCTAdapter(), [product_request(old_url)], fetcher)

    assert result.ok, result.failures
    assert [call["url"] for call in transport.calls] == [old_url, REGULAR_URL]
    assert result.observations[0].source.final_url == REGULAR_URL
    assert clock.sleeps == [10.0]  # the redirected hop waits for the per-retailer interval


def test_product_request_accepts_product_pages_on_the_confirmed_host() -> None:
    for url in (REGULAR_URL, f"{REGULAR_URL}/", V1_URL):
        request = product_request(url)
        assert (request.retailer_id, request.url) == ("oct", url)
    assert product_request(VARIANTS_URL, V1).url == V1_URL


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/",
        f"https://{HOST}/collections/laptop",
        f"https://{HOST}/collections/laptop/products/example-book-14",
        f"https://{HOST}/search?q=laptop",
        f"https://{HOST}/index.php?route=product/search&search=ASUS",
        f"https://{HOST}/products/",
        f"https://{HOST}/products/a/b",
        f"https://{HOST}/products/example-book-14.js",
        f"https://{HOST}/cart/add",
        f"https://{HOST}/checkout",
        f"https://{HOST}/account/login",
        f"https://{HOST}/policies/terms-of-service",
        f"https://{HOST}/products/example-book-14?ref=home",
        f"https://{HOST}/products/example-book-14?variant=1&ref=home",
        f"https://{HOST}/products/example-book-14?variant=abc",
        f"https://{HOST}/products/example-book-14#specs",
        f"http://{HOST}/products/example-book-14",
        "https://octagon.com.ph/products/example-book-14",
        "https://www.lazada.com.ph/products/example-book-14",
    ],
)
def test_product_request_accepts_only_product_pages(url: str) -> None:
    with pytest.raises(ValueError):
        product_request(url)


@pytest.mark.parametrize(("url", "variant"), [(V1_URL, V1), (REGULAR_URL, "12a")])
def test_product_request_rejects_bad_variant_arguments(url: str, variant: str) -> None:
    with pytest.raises(ValueError):
        product_request(url, variant)


# Live-use gate


def test_live_use_is_enabled_by_the_project_owner_decision() -> None:
    assert DATA_USE_REVIEW_STATUS == "OWNER_APPROVED"
    assert OCTAdapter.live_use_approved is True
    assert OCTAdapter.retailer_code == "OCT"
    result, transport = run(fixture(REGULAR), live=True)
    assert result.ok
    assert len(result.observations) == 1
    assert [call["url"] for call in transport.calls] == [REGULAR_URL]


def test_gate_would_block_a_live_request_without_project_approval() -> None:
    class Unapproved(OCTAdapter):
        live_use_approved = False

    result, transport = run(fixture(REGULAR), live=True, adapter=Unapproved())
    assert transport.calls == []
    assert (result.failures[0].stage, result.failures[0].error_code) == (
        Stage.REQUEST,
        "LIVE_USE_NOT_APPROVED",
    )


def test_offline_fake_transport_still_parses() -> None:
    result, transport = run(fixture(REGULAR))
    assert result.ok and len(transport.calls) == 1
