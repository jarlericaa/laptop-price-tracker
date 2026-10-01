"""Offline tests for the PC Central (PCC) adapter. Local fixtures and fake transport only."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, FakeTransport, no_network  # noqa: F401

from laptop_tracker.adapters.pcc import (
    DATA_USE_REVIEW_STATUS,
    HOST,
    PCCAdapter,
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

FIXTURES = Path(__file__).resolve().parent / "scraping_fixtures" / "pcc"
VARIANTS_URL = (
    f"https://{HOST}/products/example-15-ex0001au-ryzen-7-8gb-256gb-ssd-win-11-home-pc-central"
)
V1, V2 = "50000000000001", "50000000000002"
V1_URL = f"{VARIANTS_URL}?variant={V1}"
V2_URL = f"{VARIANTS_URL}?variant={V2}"
SINGLE_URL = (
    f"https://{HOST}/products/example-nitro-15-en15-0002-core-5-16gb-512gb-rtx-5050-pc-central"
)
SINGLE_VARIANT = "50000000000011"
HTML = {"content-type": "text/html; charset=utf-8"}

VARIANTS = "pcc_product_variants_sale.html"
SINGLE = "pcc_product_single_regular.html"


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
    body: str, url: str = SINGLE_URL, *, live: bool = False
) -> tuple[ScrapeResult, FakeTransport]:
    clock = FakeClock()
    response = TransportResponse(200, url, body.encode("utf-8"), HTML)
    transport = FakeTransport(clock, response, live=live)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(PCCAdapter(), [ScrapeRequest("pcc", url)], fetcher)
    return result, transport


def only_offer(body: str, url: str = SINGLE_URL):
    result, _ = run(body, url)
    assert result.ok, result.failures
    [observation] = result.observations
    return observation


def parse_failure(body: str, url: str = SINGLE_URL) -> tuple[Stage, str]:
    result, _ = run(body, url)
    assert not result.observations
    [failure] = result.failures
    return failure.stage, failure.error_code


IN_STOCK_EVIDENCE = (
    "JSON-LD availability: InStock; variant available: true; "
    "add to cart button: Add to cart (enabled); price sold-out state: no"
)


# Multi-variant page with the requested variant selected and on sale


def test_selected_variant_on_sale_yields_an_attributed_offer() -> None:
    offer = only_offer(fixture(VARIANTS), V1_URL)

    assert offer.retailer_id == "pcc"
    assert offer.offer_url == V1_URL
    assert offer.raw_title == (
        'Example 15-EX0001AU | 15.6" | Ryzen 7 7730U | 8GB | 256GB SSD | Win 11 Home | PC Central'
    )
    assert (offer.price, offer.currency) == (Decimal("41999.00"), "PHP")
    assert offer.raw_price_text == "₱41,999.00"
    assert offer.availability is Availability.IN_STOCK
    assert offer.availability_evidence == IN_STOCK_EVIDENCE
    assert offer.source.adapter_version == "pcc-1"
    details = offer.details
    assert details.price_type is PriceType.SALE
    assert (details.regular_price, details.regular_price_text) == (
        Decimal("42999.00"),
        "₱42,999.00",
    )
    assert (details.sale_price, details.sale_price_text) == (Decimal("41999.00"), "₱41,999.00")
    assert details.retailer_product_id == "9000000000001"
    assert details.retailer_sku == ""  # Variant SKUs are null on reviewed pages.
    assert details.brand == "ExampleBrand"
    assert details.manufacturer_part_number == ""
    assert details.raw_categories == ("Laptop",)
    assert details.bundle_flag is False and details.bundle_evidence == ()


def test_variant_title_and_description_are_the_raw_configuration_text() -> None:
    offer = only_offer(fixture(VARIANTS), V1_URL)
    # The variant title comes first: the page title alone does not say which RAM option.
    assert offer.raw_configuration_text.startswith(
        "8GB DDR4 RAM / 256GB SSD | Example Laptop 15-EX0001AU (X0X00PA) | "
        "Operating system: Windows 11 Home Single Language"
    )
    assert offer.details.raw_specifications == (
        ("Example Laptop 15-EX0001AU (X0X00PA)", ""),
        ("Operating system", "Windows 11 Home Single Language"),
        ("Processor", "AMD Ryzen™ 7 7730U"),
        ("Memory", "8GB DDR4-3200 MT/s (1 x 8 GB)"),
        ("Internal Storage", "256 GB PCIe® NVMe™ M.2 SSD"),
        ("Display", '15.6" diagonal, FHD (1920 x 1080), anti-glare'),
    )


def test_hidden_badges_labels_and_recommendations_are_ignored() -> None:
    # The price block's "Sold out" badge, the visually hidden "Variant sold out or
    # unavailable" labels, the hidden error message, and a sold-out recommendation card at
    # PHP 19,999.00 must not affect the main offer.
    offer = only_offer(fixture(VARIANTS), V1_URL)
    assert offer.availability is Availability.IN_STOCK
    assert "Sold out" not in offer.availability_evidence
    assert Decimal("19999.00") not in (offer.price, offer.details.regular_price)


def test_bare_url_on_a_multi_variant_page_is_ambiguous() -> None:
    assert parse_failure(fixture(VARIANTS), VARIANTS_URL) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


def test_requested_variant_must_be_the_one_the_page_selected() -> None:
    # Requesting the second variant while the page's form still selects the first.
    assert parse_failure(fixture(VARIANTS), V2_URL) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


def test_requested_variant_missing_from_the_variant_data_is_ambiguous() -> None:
    unknown = "50000000000009"
    body = mutate(fixture(VARIANTS), f'name="id" value="{V1}"', f'name="id" value="{unknown}"')
    assert parse_failure(body, f"{VARIANTS_URL}?variant={unknown}") == (
        Stage.PARSE,
        "AMBIGUOUS_VARIANT",
    )


def test_second_variant_page_with_a_lower_compare_at_price_is_regular() -> None:
    # Rendered for variant 2: its compare-at (42,999) is below its price (46,999), so Dawn
    # does not mark the block on sale and the compare-at amount is not shown.
    body = mutate(fixture(VARIANTS), f'name="id" value="{V1}"', f'name="id" value="{V2}"')
    body = mutate(body, "price--on-sale ", "")
    body = mutate(body, "₱41,999.00", "₱46,999.00")
    offer = only_offer(body, V2_URL)

    assert offer.price == Decimal("46999.00")
    assert offer.offer_url == V2_URL
    assert offer.details.price_type is PriceType.REGULAR
    assert (offer.details.regular_price, offer.details.sale_price) == (Decimal("46999.00"), None)
    assert offer.raw_configuration_text.startswith("16GB (2x8) DDR4 RAM / 256GB SSD | ")


def test_form_selecting_two_variants_is_ambiguous() -> None:
    body = mutate(fixture(VARIANTS), f'value="{V1}" disabled', f'value="{V2}" disabled')
    assert parse_failure(body, V1_URL) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


def test_url_and_redirect_naming_different_variants_is_ambiguous() -> None:
    clock = FakeClock()
    moved = TransportResponse(301, V2_URL, b"", {"location": V1_URL})
    page = TransportResponse(200, V1_URL, fixture(VARIANTS).encode(), HTML)
    transport = FakeTransport(clock, moved, page)
    [result] = scrape(PCCAdapter(), [product_request(V2_URL)], Fetcher(transport, None, clock))
    assert (result.failures[0].stage, result.failures[0].error_code) == (
        Stage.PARSE,
        "AMBIGUOUS_VARIANT",
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('data-has-only-default-variant="true"', 'data-has-only-default-variant="false"'),
        (
            "</product-form>",
            '</product-form><noscript><select name="id"><option value="50000000000011">A'
            '</option><option value="50000000000012">B</option></select></noscript>',
        ),
        (
            '"price" : 69999.0,',
            '"price" : 69999.0, "priceCurrency" : "PHP"}, {"@type" : "Offer", '
            '"price" : 74999.0,',
        ),
        (
            '"public_title":null,"sku":"EX-NB-0002"}',
            '"public_title":null,"sku":"EX-NB-0002"},{"id":50000000000012,"price":7499900}',
        ),
    ],
)
def test_any_variant_signal_on_a_bare_url_is_ambiguous(old: str, new: str) -> None:
    assert parse_failure(mutate(fixture(SINGLE), old, new)) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


def test_unreadable_variant_data_is_ambiguous() -> None:
    body = mutate(fixture(VARIANTS), '[{"id":50000000000001,', '[{"id":50000000000001')
    assert parse_failure(body, V1_URL) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


# Single default variant at a regular price


def test_single_variant_page_needs_no_variant_query() -> None:
    offer = only_offer(fixture(SINGLE))

    assert offer.offer_url == f"{SINGLE_URL}?variant={SINGLE_VARIANT}"
    assert offer.raw_title.startswith("Example Nitro 15 EN15-0002 | Core 5 210H")
    assert (offer.price, offer.raw_price_text) == (Decimal("69999.00"), "₱69,999.00")
    details = offer.details
    assert details.price_type is PriceType.REGULAR
    assert (details.regular_price, details.sale_price) == (Decimal("69999.00"), None)
    assert details.retailer_sku == "EX-NB-0002"
    assert details.retailer_product_id == "9000000000002"
    # The analytics object has no availability, so only JSON-LD and the button speak.
    assert offer.availability is Availability.IN_STOCK
    assert offer.availability_evidence == IN_STOCK_EVIDENCE.replace("true", "none")


def test_line_breaks_in_the_description_become_raw_rows() -> None:
    offer = only_offer(fixture(SINGLE))
    specs = offer.details.raw_specifications
    assert specs[:3] == (
        ("SPECIFICATIONS", ""),
        ("Model", "Example Nitro 15 EN15-0002"),
        ("OPI / SKU", "XX.XXXXX.000"),
    )
    assert dict(specs)["Display"] == '15.6" FHD IPS 165Hz'
    assert not any("refresh" in label.lower() for label, _ in specs)
    # No variant title is added for a single default variant.
    assert offer.raw_configuration_text.startswith("SPECIFICATIONS | Model: Example Nitro 15")


def test_matching_variant_query_on_a_single_variant_page_is_accepted() -> None:
    url = f"{SINGLE_URL}?variant={SINGLE_VARIANT}"
    assert only_offer(fixture(SINGLE), url).price == Decimal("69999.00")


def test_other_variant_query_on_a_single_variant_page_is_ambiguous() -> None:
    url = f"{SINGLE_URL}?variant=50000000000099"
    assert parse_failure(fixture(SINGLE), url) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


def test_normalization_is_deterministic() -> None:
    first = only_offer(fixture(VARIANTS), V1_URL).to_full_record()
    assert first == only_offer(fixture(VARIANTS), V1_URL).to_full_record()
    assert "target_id" not in first


# Fallbacks


PRICE_BLOCK = '<div class="no-js-hidden" id="price-template--1__main"'
SINGLE_LD = '<script type="application/ld+json">\n{\n  "@context": "http://schema.org/"'


def test_jsonld_price_alone_gives_no_sale_or_regular_claim() -> None:
    body = cut(fixture(SINGLE), PRICE_BLOCK, '<span class="badge price__badge-sold-out')
    offer = only_offer(body)
    assert (offer.price, offer.raw_price_text) == (Decimal("69999.0"), "69999.0")
    assert offer.details.price_type is None
    assert (offer.details.regular_price, offer.details.sale_price) == (None, None)


def test_jsonld_price_alone_requires_php_currency() -> None:
    body = cut(fixture(SINGLE), PRICE_BLOCK, '<span class="badge price__badge-sold-out')
    body = mutate(body, '"priceCurrency" : "PHP",', "")
    assert parse_failure(body) == (Stage.PARSE, "CURRENCY_NOT_FOUND")


def test_visible_content_is_the_fallback_without_jsonld() -> None:
    body = cut(fixture(SINGLE), SINGLE_LD, "</script>")
    offer = only_offer(body)

    assert offer.raw_title.startswith("Example Nitro 15 EN15-0002")
    assert offer.offer_url == SINGLE_URL  # the canonical link
    assert offer.price == Decimal("69999.00")
    assert offer.details.price_type is PriceType.REGULAR
    # Brand falls back to the analytics vendor, never to the title.
    assert offer.details.brand == "ExampleBrand"
    # An enabled "Add to cart" button alone is not an explicit stock signal.
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.startswith("JSON-LD availability: none; variant ")


def test_broken_jsonld_is_ignored_and_visible_content_is_used() -> None:
    body = mutate(fixture(SINGLE), '"@type": "Product",', '"@type": "Product"')
    offer = only_offer(body)
    assert offer.price == Decimal("69999.00")
    assert offer.availability is Availability.UNKNOWN


def test_no_price_anywhere_is_price_not_found() -> None:
    body = cut(fixture(SINGLE), SINGLE_LD, "</script>")
    body = mutate(body, " ₱69,999.00 ", " Call for price ")
    assert parse_failure(body) == (Stage.PARSE, "PRICE_NOT_FOUND")


# Prices and currency


SINGLE_REGULAR = '<span class="price-item price-item--regular"> ₱69,999.00 </span>'
SINGLE_SALE = '<span class="price-item price-item--sale price-item--last"> ₱69,999.00 </span>'
EMPTY_COMPARE = '<s class="price-item price-item--regular">\n</s>'


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        (" ₱69,999.00 ", " ₱65,999.00 ", "PRICE_CONFLICT"),  # displayed vs JSON-LD/variant
        ('"price" : 69999.0,', '"price" : 65999.0,', "PRICE_CONFLICT"),
        ('"price":6999900', '"price":6599900', "PRICE_CONFLICT"),
        (SINGLE_SALE, SINGLE_SALE.replace("69,999", "65,999"), "PRICE_CONFLICT"),
        (EMPTY_COMPARE, EMPTY_COMPARE.replace("\n", " ₱74,999.00 "), "PRICE_CONFLICT"),
        ('"priceCurrency" : "PHP"', '"priceCurrency" : "USD"', "UNSUPPORTED_CURRENCY"),
        (" ₱69,999.00 ", " $69,999.00 ", "UNSUPPORTED_CURRENCY"),
        (" ₱69,999.00 ", " ₱69,999.00 USD ", "UNSUPPORTED_CURRENCY"),
        (" ₱69,999.00 ", " 69,999.00 ", "CURRENCY_NOT_FOUND"),
        (" ₱69,999.00 ", " From ₱69,999.00 ", "AMBIGUOUS_PRICE"),
        (" ₱69,999.00 ", " ₱59,999.00 – ₱69,999.00 ", "AMBIGUOUS_PRICE"),
        (" ₱69,999.00 ", " ₱0.00 ", "INVALID_PRICE"),
        (SINGLE_REGULAR, SINGLE_REGULAR * 2, "AMBIGUOUS_PRICE"),
        ("<product-form ", '<div class="price"><div class="price__regular"><span class="'
         'price-item price-item--regular">₱1,999.00</span></div></div><product-form ',
         "AMBIGUOUS_PRICE"),
        ('"@type" : "Offer"', '"@type" : "AggregateOffer"', "AMBIGUOUS_PRICE"),
    ],
)
def test_unclear_single_variant_prices_are_parse_failures(old: str, new: str, code: str) -> None:
    assert parse_failure(mutate(fixture(SINGLE), old, new)) == (Stage.PARSE, code)


SALE_COMPARE = '<s class="price-item price-item--regular"> ₱42,999.00 </s>'


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        (SALE_COMPARE, SALE_COMPARE.replace("42,999", "40,000"), "PRICE_CONFLICT"),
        (SALE_COMPARE, SALE_COMPARE.replace("42,999", "44,999"), "PRICE_CONFLICT"),  # vs variant
        (SALE_COMPARE, "<s></s>", "AMBIGUOUS_PRICE"),
        ("price--on-sale ", "", "PRICE_CONFLICT"),  # variant compare-at higher, not shown
        ('"price" : 41999.0,', '"price" : 41000.0,', "PRICE_CONFLICT"),
        ('"url" : "https:\\/\\/pccentralcomputerstore.com\\/products\\/example-15-ex0001au-ryzen-'
         '7-8gb-256gb-ssd-win-11-home-pc-central?variant=50000000000002"',
         '"url" : "https:\\/\\/pccentralcomputerstore.com\\/products\\/example-15-ex0001au-ryzen-'
         '7-8gb-256gb-ssd-win-11-home-pc-central?variant=50000000000001"', "AMBIGUOUS_PRICE"),
    ],
)
def test_unclear_sale_prices_are_parse_failures(old: str, new: str, code: str) -> None:
    assert parse_failure(mutate(fixture(VARIANTS), old, new), V1_URL) == (Stage.PARSE, code)


def test_equal_compare_at_price_on_sale_is_regular() -> None:
    body = mutate(fixture(VARIANTS), SALE_COMPARE, SALE_COMPARE.replace("42,999", "41,999"))
    body = mutate(body, '"compare_at_price":4299900}', '"compare_at_price":null}', 1)
    details = only_offer(body, V1_URL).details
    assert details.price_type is PriceType.REGULAR
    assert (details.regular_price, details.sale_price) == (Decimal("41999.00"), None)


@pytest.mark.parametrize("amount", [" 69,999.00 PHP ", " PHP 69,999.00 ", " ₱69,999.00 PHP "])
def test_php_code_is_accepted_as_currency_evidence(amount: str) -> None:
    offer = only_offer(mutate(fixture(SINGLE), " ₱69,999.00 ", amount))
    assert (offer.price, offer.currency) == (Decimal("69999.00"), "PHP")


# Availability


SINGLE_BUTTON = 'class="product-form__submit button button--full-width button--secondary"'
ADD_LABEL = "<span>Add to cart </span>"


def sold_out(body: str) -> str:
    """Dawn's sold-out rendering of the single-variant fixture."""
    body = mutate(body, "InStock", "OutOfStock")
    body = mutate(body, " price--large ", " price--large price--sold-out ")
    body = mutate(body, SINGLE_BUTTON, SINGLE_BUTTON + " disabled")
    return mutate(body, ADD_LABEL, "<span>Sold out </span>")


def test_dawn_sold_out_markup_is_out_of_stock() -> None:
    offer = only_offer(sold_out(fixture(SINGLE)))
    assert offer.availability is Availability.OUT_OF_STOCK
    assert offer.availability_evidence == (
        "JSON-LD availability: OutOfStock; variant available: none; "
        "add to cart button: Sold out (disabled); price sold-out state: yes"
    )


def test_unavailable_selected_variant_is_out_of_stock() -> None:
    body = mutate(fixture(VARIANTS), "InStock", "OutOfStock", 1)
    body = mutate(body, '"available":true', '"available":false', 1)
    body = mutate(body, " price--show-badge", " price--sold-out price--show-badge")
    body = mutate(body, ADD_LABEL, "<span>Sold out </span>")
    body = mutate(body, 'button--secondary"', 'button--secondary" disabled')
    offer = only_offer(body, V1_URL)
    assert offer.availability is Availability.OUT_OF_STOCK
    assert "variant available: false" in offer.availability_evidence


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("InStock", "OutOfStock"),
        (" price--large ", " price--large price--sold-out "),
        (SINGLE_BUTTON, SINGLE_BUTTON + " disabled"),
        (SINGLE_BUTTON, SINGLE_BUTTON + ' aria-disabled="true"'),
        (ADD_LABEL, "<span>Sold out </span>"),
        (ADD_LABEL, "<span>Unavailable </span>"),
        (ADD_LABEL, "<span>Pre-order </span>"),
    ],
)
def test_conflicting_stock_signals_are_unknown(old: str, new: str) -> None:
    offer = only_offer(mutate(fixture(SINGLE), old, new))
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.endswith("signals conflict")


def test_variant_flag_contradicting_jsonld_is_a_conflict() -> None:
    body = mutate(fixture(VARIANTS), '"available":true', '"available":false', 1)
    offer = only_offer(body, V1_URL)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.endswith("signals conflict")


def test_analytics_variants_never_supply_availability() -> None:
    body = mutate(fixture(SINGLE), '"sku":"EX-NB-0002"', '"sku":"EX-NB-0002","available":false')
    offer = only_offer(body)
    assert offer.availability is Availability.IN_STOCK
    assert "variant available: none" in offer.availability_evidence


def test_unmapped_jsonld_availability_is_unknown() -> None:
    body = mutate(fixture(SINGLE), "InStock", "BackOrder")
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert "JSON-LD availability: BackOrder" in offer.availability_evidence


def test_missing_button_contradicts_in_stock() -> None:
    body = cut(fixture(SINGLE), '<button id="ProductSubmitButton', "</button>")
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert "add to cart button: absent" in offer.availability_evidence


def test_out_of_stock_with_an_enabled_add_to_cart_button_is_a_conflict() -> None:
    body = mutate(sold_out(fixture(SINGLE)), SINGLE_BUTTON + " disabled", SINGLE_BUTTON)
    body = mutate(body, "<span>Sold out </span>", ADD_LABEL)
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.endswith("signals conflict")


def test_preorder_needs_jsonld_and_button_to_agree() -> None:
    body = mutate(fixture(SINGLE), "InStock", "PreOrder")
    body = mutate(body, ADD_LABEL, "<span>Pre-order </span>")
    offer = only_offer(body)
    assert offer.availability is Availability.PREORDER
    assert "add to cart button: Pre-order (enabled)" in offer.availability_evidence


def test_several_different_buttons_are_a_conflict() -> None:
    extra = (
        '<button type="submit" class="product-form__submit button" disabled>'
        "<span>Sold out</span></button></product-form>"
    )
    body = mutate(fixture(SINGLE), "</product-form>", extra)
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.endswith("signals conflict")


# Bundles


def test_bundle_listing_is_flagged_with_evidence_and_not_dropped() -> None:
    heading = "RTX 5050 8GB | PC CENTRAL</h1>"
    body = mutate(
        fixture(SINGLE), heading, heading.replace("</h1>", " + FREE Example Backpack</h1>")
    )
    body = cut(body, SINGLE_LD, "</script>")  # Keep the heading and JSON-LD name consistent.
    offer = only_offer(body)
    assert offer.raw_title.endswith("+ FREE Example Backpack")
    assert offer.details.bundle_flag is True
    assert offer.details.bundle_evidence == (
        "title term FREE: + FREE",
        "title term BACKPACK: Backpack",
    )
    assert offer.price == Decimal("69999.00")


def test_bundle_wording_in_the_variant_title_is_flagged() -> None:
    body = mutate(
        fixture(VARIANTS),
        '"title":"8GB DDR4 RAM \\/ 256GB SSD"',
        '"title":"8GB DDR4 RAM \\/ 256GB SSD + Mouse"',
    )
    offer = only_offer(body, V1_URL)
    assert offer.details.bundle_flag is True
    assert offer.details.bundle_evidence == ("variant term MOUSE: Mouse",)


# Layout and identity


def test_unrecognized_layout_is_a_parse_failure() -> None:
    assert parse_failure(fixture("pcc_layout_changed.html")) == (
        Stage.PARSE,
        "UNRECOGNIZED_LAYOUT",
    )


def test_no_heading_and_no_jsonld_name_has_no_title() -> None:
    body = cut(fixture(SINGLE), "<h1>", "</h1>")
    body = mutate(body, '"name": "Example Nitro 15 EN15-0002 | Core 5 210H | 16GB DDR4 | '
                  '512GB SSD | RTX 5050 8GB | PC CENTRAL",', "")
    assert parse_failure(body) == (Stage.PARSE, "TITLE_NOT_FOUND")


def test_jsonld_name_is_the_title_without_a_heading() -> None:
    body = cut(fixture(SINGLE), "<h1>", "</h1>")
    assert only_offer(body).raw_title.startswith("Example Nitro 15 EN15-0002 | Core 5")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("</h1>", "</h1><h1>Other Laptop</h1>"),
        ("| PC CENTRAL</h1>", "| PC CENTRAL (Refurbished)</h1>"),  # heading vs JSON-LD name
        ('name="product-id" value="9000000000002"', 'name="product-id" value="9000000000003"'),
        ('"name": "ExampleBrand"', '"name": "OtherBrand"'),
        ('<link rel="canonical" href="https://pccentralcomputerstore.com/products/example-nitro',
         '<link rel="canonical" href="https://pccentralcomputerstore.com/products/other-nitro'),
        ("</main>", '<product-info class="product__info-container"><h1>Other</h1>'
                    "</product-info></main>"),
        ("</head>", '<script type="application/ld+json">{"@type":"Product","name":"Other",'
                    '"url":"https://pccentralcomputerstore.com/products/other"}</script></head>'),
        ("</head>", '<script>var meta = {"product":{"id":9000000000009}};</script></head>'),
    ],
)
def test_disagreeing_product_identity_is_ambiguous(old: str, new: str) -> None:
    assert parse_failure(mutate(fixture(SINGLE), old, new, 1)) == (
        Stage.PARSE,
        "AMBIGUOUS_PRODUCT",
    )


def test_offer_links_to_other_hosts_are_rejected() -> None:
    body = mutate(
        fixture(SINGLE),
        '"url" : "https:\\/\\/pccentralcomputerstore.com\\/products\\/',
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
        "www.pccentralcomputerstore.com",
        "pccentral.com.ph",
        "www.pccentral.com.ph",
        "pc-central-computer-store.myshopify.com",
        "shop.pccentralcomputerstore.com",
    ],
)
def test_only_the_exact_confirmed_host_is_requested(host: str) -> None:
    result, transport = run("", f"https://{host}/products/x")
    assert transport.calls == []
    assert result.failures[0].error_code == "HOST_NOT_ALLOWED"


@pytest.mark.parametrize(
    "location",
    [
        "https://www.pccentralcomputerstore.com/products/x",
        "https://pccentral.com.ph/products/x",
        "https://elsewhere.example.test/landing/",
    ],
)
def test_redirect_off_the_confirmed_host_is_not_followed(location: str) -> None:
    clock = FakeClock()
    moved = TransportResponse(301, SINGLE_URL, b"", {"location": location})
    transport = FakeTransport(clock, moved)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(PCCAdapter(), [product_request(SINGLE_URL)], fetcher)
    assert [call["url"] for call in transport.calls] == [SINGLE_URL]
    assert result.failures[0].error_code == "REDIRECT_HOST_NOT_ALLOWED"


def test_redirect_within_the_confirmed_host_is_followed() -> None:
    old_url = f"https://{HOST}/products/example-nitro-15-old-handle"
    clock = FakeClock()
    location = SINGLE_URL.removeprefix(f"https://{HOST}")  # relative Location header
    moved = TransportResponse(301, old_url, b"", {"location": location})
    page = TransportResponse(200, SINGLE_URL, fixture(SINGLE).encode(), HTML)
    transport = FakeTransport(clock, moved, page)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(PCCAdapter(), [product_request(old_url)], fetcher)

    assert result.ok, result.failures
    assert [call["url"] for call in transport.calls] == [old_url, SINGLE_URL]
    assert result.observations[0].source.final_url == SINGLE_URL
    assert clock.sleeps == [10.0]  # the redirected hop waits for the per-retailer interval


def test_product_request_accepts_product_pages_on_the_confirmed_host() -> None:
    for url in (SINGLE_URL, f"{SINGLE_URL}/", V1_URL):
        request = product_request(url)
        assert (request.retailer_id, request.url) == ("pcc", url)
    assert product_request(VARIANTS_URL, V1).url == V1_URL


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/",
        f"https://{HOST}/collections/laptops",
        f"https://{HOST}/collections/laptops/products/example-15",
        f"https://{HOST}/search?q=laptop",
        f"https://{HOST}/products/",
        f"https://{HOST}/products/a/b",
        f"https://{HOST}/cart/add",
        f"https://{HOST}/policies/terms-of-service",
        f"https://{HOST}/products/example-15?ref=home",
        f"https://{HOST}/products/example-15?variant=1&ref=home",
        f"https://{HOST}/products/example-15?variant=abc",
        f"https://{HOST}/products/example-15#reviews",
        "https://www.pccentralcomputerstore.com/products/example-15",
        "https://pccentral.com.ph/products/example-15",
    ],
)
def test_product_request_accepts_only_product_pages(url: str) -> None:
    with pytest.raises(ValueError):
        product_request(url)


@pytest.mark.parametrize(("url", "variant"), [(V1_URL, V1), (SINGLE_URL, "12a")])
def test_product_request_rejects_bad_variant_arguments(url: str, variant: str) -> None:
    with pytest.raises(ValueError):
        product_request(url, variant)


# Live-use gate


def test_live_use_is_enabled_by_the_project_owner_decision() -> None:
    assert DATA_USE_REVIEW_STATUS == "OWNER_APPROVED"
    assert PCCAdapter.live_use_approved is True
    assert PCCAdapter.retailer_code == "PCC"
    result, transport = run(fixture(SINGLE), live=True)
    assert result.ok
    assert len(result.observations) == 1
    assert [call["url"] for call in transport.calls] == [SINGLE_URL]


def test_offline_fake_transport_still_parses() -> None:
    result, transport = run(fixture(SINGLE))
    assert result.ok and len(transport.calls) == 1
