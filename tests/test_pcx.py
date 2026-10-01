"""Offline tests for the PC Express (PCX) adapter. Local fixtures and fake transport only."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, FakeTransport, no_network  # noqa: F401

from laptop_tracker.adapters.pcx import (
    DATA_USE_REVIEW_STATUS,
    HOST,
    PCXAdapter,
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

FIXTURES = Path(__file__).resolve().parent / "scraping_fixtures" / "pcx"
REGULAR_URL = f"https://{HOST}/products/example-14-ex0001tu-x0x00pa-intel-core-5-laptop-natural-silver"
SALE_URL = f"https://{HOST}/products/example-gaming-15-eg15-0002ph-geforce-rtx-5050-laptop-black"
NO_JSONLD_URL = f"https://{HOST}/products/example-notebook-15-en15-0003ph-amd-ryzen-5-laptop-grey"
BUNDLE_URL = f"https://{HOST}/products/example-gaming-16-eg16-0004ph-laptop-free-backpack"
HTML = {"content-type": "text/html; charset=utf-8"}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def mutate(text: str, old: str, new: str) -> str:
    assert old in text, f"fixture no longer contains {old!r}"
    return text.replace(old, new)


def run(
    body: str, url: str = REGULAR_URL, *, live: bool = False
) -> tuple[ScrapeResult, FakeTransport]:
    clock = FakeClock()
    response = TransportResponse(200, url, body.encode("utf-8"), HTML)
    transport = FakeTransport(clock, response, live=live)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(PCXAdapter(), [ScrapeRequest("pcx", url)], fetcher)
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


def cut(text: str, start: str, end: str) -> str:
    """Remove ``text`` from the first ``start`` through the next ``end``, inclusive."""
    begin = text.index(start)
    return text[:begin] + text[text.index(end, begin) + len(end) :]


REGULAR = "pcx_product_regular.html"
SALE = "pcx_product_sale_sold_out.html"
NO_JSONLD = "pcx_product_regular_no_jsonld.html"
PRICE_BLOCK = '<div class="t4s-product-price"data-pr-price'


# Regular product page with two Product JSON-LD blocks


def test_regular_page_yields_an_attributed_offer() -> None:
    offer = only_offer(fixture(REGULAR))

    assert offer.retailer_id == "pcx"
    assert offer.offer_url == REGULAR_URL
    assert offer.raw_title == (
        "Example 14-EX0001TU (X0X00PA) Intel® Core™ 5 Laptop (Natural Silver)"
    )
    assert (offer.price, offer.currency) == (Decimal("47990.00"), "PHP")
    assert offer.raw_price_text == "₱47,990.00"
    assert offer.availability is Availability.IN_STOCK
    assert offer.availability_evidence == (
        "JSON-LD availability: InStock; add to cart button: Add to cart (enabled); "
        "notify-me button: hidden"
    )
    assert offer.source.adapter_version == "pcx-1"
    details = offer.details
    assert details.price_type is PriceType.REGULAR
    assert (details.regular_price, details.sale_price) == (Decimal("47990.00"), None)
    assert details.retailer_sku == "LT-EX-000001"
    assert details.retailer_product_id == "9000000000001"
    assert details.brand == "ExampleBrand"
    # The SEO app's "mpn" repeats the retailer SKU and the reviews app's is blank.
    assert details.manufacturer_part_number == ""
    # Product type first, then collection links, as shown; "Website Sale" is not a price claim.
    assert details.raw_categories == ("Laptops", "All Products", "Website Sale")
    assert details.bundle_flag is False and details.bundle_evidence == ()


def test_a_stated_manufacturer_part_number_is_kept() -> None:
    body = mutate(fixture(REGULAR), '"mpn": "", "productID"', '"mpn": "X0X00PA", "productID"')
    assert only_offer(body).details.manufacturer_part_number == "X0X00PA"


def test_header_card_sticky_template_help_text_and_recommendations_are_ignored() -> None:
    # The header card shows PHP 19,990.00; the sticky <template> copy shows PHP 1.00 and
    # "Sold out"; the recommendation card is on sale and "Sold out"; the static help text says
    # "Out of Stock? Contact us."
    offer = only_offer(fixture(REGULAR))
    assert offer.price == Decimal("47990.00")
    assert offer.details.price_type is PriceType.REGULAR
    assert "Sold out" not in offer.availability_evidence
    assert offer.availability is Availability.IN_STOCK
    assert offer.raw_title != "Sticky Copy"


def test_description_table_is_preserved_raw_and_nothing_is_inferred() -> None:
    offer = only_offer(fixture(REGULAR))
    specs = dict(offer.details.raw_specifications)

    assert list(specs) == ["Operating System", "Processor", "Display", "Memory", "Storage"]
    assert specs["Memory"] == "16GB (2x 8GB) 3200MHz DDR4"
    assert not any("refresh" in label.lower() for label in specs)
    # The summary under the title is the retailer's own configuration line.
    assert offer.raw_configuration_text.startswith(
        "EXAMPLE 14-EX0001TU (X0X00PA) NATURAL SILVER INTEL CORE 5 120U/16GB DDR4"
    )


def test_spec_rows_are_the_configuration_text_without_a_summary() -> None:
    body = cut(fixture(REGULAR), '<div class="metafield-rich_text_field">', "</div>")
    assert only_offer(body).raw_configuration_text.startswith(
        "Operating System: Windows 11 Home | Processor: Intel® Core™ 5-120U"
    )


def test_normalization_is_deterministic() -> None:
    first = only_offer(fixture(REGULAR)).to_full_record()
    assert first == only_offer(fixture(REGULAR)).to_full_record()
    assert "target_id" not in first


def test_jsonld_price_alone_gives_no_sale_or_regular_claim() -> None:
    body = cut(fixture(REGULAR), PRICE_BLOCK, "</div>")
    offer = only_offer(body)

    assert (offer.price, offer.raw_price_text) == (Decimal("47990.0"), "47990.0")
    details = offer.details
    assert details.price_type is None
    assert (details.regular_price, details.sale_price) == (None, None)


def test_jsonld_price_alone_requires_php_currency() -> None:
    body = cut(fixture(REGULAR), PRICE_BLOCK, "</div>")
    body = mutate(body, '"price" : "47990.0", "priceCurrency" : "PHP"', '"price" : "47990.0"')
    assert parse_failure(body) == (Stage.PARSE, "CURRENCY_NOT_FOUND")


def test_one_broken_jsonld_block_leaves_the_other() -> None:
    body = mutate(fixture(REGULAR), '"@type": "Product", "@id"', '"@type": "Product", @id')
    offer = only_offer(body)
    assert offer.details.retailer_product_id == "9000000000001"
    assert offer.availability is Availability.IN_STOCK


# Sale and sold-out page


def test_sale_page_separates_prices_and_reads_sold_out_markup() -> None:
    offer = only_offer(fixture(SALE), SALE_URL)

    # The visible heading is used; the SEO block's name carries an HTML entity.
    assert offer.raw_title == (
        'Example Gaming 15 EG15-0002PH 15.6" GeForce RTX™ 5050 Laptop (Black)'
    )
    assert (offer.price, offer.raw_price_text) == (Decimal("62990.00"), "₱62,990.00")
    details = offer.details
    assert details.price_type is PriceType.SALE
    assert (details.regular_price, details.regular_price_text) == (
        Decimal("69990.00"),
        "₱69,990.00",
    )
    assert (details.sale_price, details.sale_price_text) == (Decimal("62990.00"), "₱62,990.00")
    assert offer.availability is Availability.OUT_OF_STOCK
    assert offer.availability_evidence == (
        "JSON-LD availability: OutOfStock; add to cart button: Add to Cart (disabled); "
        "notify-me button: shown"
    )
    assert details.raw_categories == ("Laptops", "Gaming Laptops")


def test_save_badge_is_not_read_as_a_price() -> None:
    offer = only_offer(fixture(SALE), SALE_URL)
    assert Decimal("7000.00") not in (offer.price, offer.details.regular_price)


def test_jsonld_names_are_unescaped_when_there_is_no_heading() -> None:
    body = cut(fixture(SALE), '<h1 class="t4s-product__title">', "</h1>")
    assert only_offer(body, SALE_URL).raw_title.startswith('Example Gaming 15 EG15-0002PH 15.6"')


# Visible-content fallback (no Product JSON-LD)


def test_visible_content_is_the_fallback_without_jsonld() -> None:
    offer = only_offer(fixture(NO_JSONLD), f"{NO_JSONLD_URL}?ref=search")

    assert offer.raw_title == "Example Notebook 15 EN15-0003PH AMD Ryzen™ 5 Laptop (Grey)"
    assert offer.offer_url == NO_JSONLD_URL  # relative canonical link resolved against the page
    assert (offer.price, offer.raw_price_text) == (Decimal("32500.00"), "₱32,500.00")
    details = offer.details
    assert details.price_type is PriceType.REGULAR
    assert (details.retailer_sku, details.brand) == ("LT-EX-000003", "ExampleBrand")
    assert details.retailer_product_id == "9000000000003"
    assert details.manufacturer_part_number == ""
    assert details.raw_specifications == (
        ("Processor", "AMD Ryzen™ 5 7530U"),
        ("Memory", "8GB DDR4"),
        ("Operating System", "No OS"),
    )
    # An enabled "Add to cart" button alone is not an explicit stock signal.
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence == (
        "JSON-LD availability: none; add to cart button: Add to cart (enabled); "
        "notify-me button: hidden"
    )


# Bundles


def test_bundle_listing_is_flagged_with_evidence_and_not_dropped() -> None:
    offer = only_offer(fixture("pcx_bundle.html"), BUNDLE_URL)

    assert offer.raw_title.endswith("+ FREE Example Gaming Backpack")
    assert offer.details.bundle_flag is True
    assert offer.details.bundle_evidence == (
        "title term FREE: + FREE",
        "title term BACKPACK: Backpack",
    )
    assert offer.price == Decimal("89995.00")
    assert offer.details.price_type is PriceType.REGULAR
    assert offer.availability is Availability.IN_STOCK


@pytest.mark.parametrize(
    ("suffix", "evidence"),
    [
        (" + Laptop Bag", "title term BAG: Bag"),
        (" Bundle", "title term BUNDLE: Bundle"),
        (" w/ 24in Monitor", "title term MONITOR: Monitor"),
        (" + Gaming Mouse", "title term MOUSE: Mouse"),
        (" + Headset", "title term HEADSET: Headset"),
    ],
)
def test_bundle_title_terms_are_flagged(suffix: str, evidence: str) -> None:
    heading = "Laptop (Natural Silver)</h1>"
    body = mutate(fixture(REGULAR), heading, heading.replace("</h1>", suffix + "</h1>"))
    offer = only_offer(body)
    assert offer.raw_title.endswith(suffix)
    assert offer.details.bundle_flag is True
    assert evidence in offer.details.bundle_evidence


# Prices and currency


SEO_PRICE = '"price": "47990.0", "priceCurrency": "PHP"'
REVIEWS_PRICE = '"price" : "47990.0", "priceCurrency" : "PHP"'
REGULAR_AMOUNT = "&#8369;47,990.00"
SALE_INS = "<ins>&#8369;62,990.00</ins>"


@pytest.mark.parametrize(
    ("name", "old", "new", "code"),
    [
        (REGULAR, "&#8369;47,990.00\n", "&#8369;45,990.00\n", "PRICE_CONFLICT"),
        (REGULAR, REVIEWS_PRICE, REVIEWS_PRICE.replace("479", "459"), "PRICE_CONFLICT"),
        (SALE, "<del>&#8369;69,990.00</del>", "<del>&#8369;60,000.00</del>", "PRICE_CONFLICT"),
        (REGULAR, SEO_PRICE, SEO_PRICE.replace("PHP", "USD"), "UNSUPPORTED_CURRENCY"),
        (SALE, "<del>&#8369;69,990.00</del>", "", "AMBIGUOUS_PRICE"),
        (SALE, SALE_INS, f"{SALE_INS}{SALE_INS}", "AMBIGUOUS_PRICE"),
        (REGULAR, '"offers": { "@type": "Offer"', '"offers": { "@type": "AggregateOffer"',
         "AMBIGUOUS_PRICE"),
        (NO_JSONLD, "&#8369;32,500.00", "Call for price", "PRICE_NOT_FOUND"),
        (NO_JSONLD, "&#8369;32,500.00", "&#8369;30,000.00 &ndash; &#8369;32,500.00",
         "AMBIGUOUS_PRICE"),
        (NO_JSONLD, "&#8369;32,500.00", "From &#8369;32,500.00", "AMBIGUOUS_PRICE"),
        (NO_JSONLD, "<form ",
         '<div class="t4s-product-price" data-product-price>&#8369;31,000.00</div><form ',
         "AMBIGUOUS_PRICE"),
        (NO_JSONLD, "&#8369;32,500.00", "$32,500.00", "UNSUPPORTED_CURRENCY"),
        (NO_JSONLD, "&#8369;32,500.00", "&#8369;32,500.00 USD", "UNSUPPORTED_CURRENCY"),
        (NO_JSONLD, "&#8369;32,500.00", "32,500.00", "CURRENCY_NOT_FOUND"),
        (NO_JSONLD, "&#8369;32,500.00", "&#8369;0.00", "INVALID_PRICE"),
    ],
)
def test_unclear_prices_are_parse_failures(name: str, old: str, new: str, code: str) -> None:
    url = {REGULAR: REGULAR_URL, SALE: SALE_URL, NO_JSONLD: NO_JSONLD_URL}[name]
    assert parse_failure(mutate(fixture(name), old, new), url) == (Stage.PARSE, code)


def test_several_jsonld_offers_are_ambiguous() -> None:
    body = mutate(
        fixture(REGULAR),
        '"offers": { "@type" : "Offer"',
        '"offers": [{ "@type": "Offer", "price": "52990.0", "priceCurrency": "PHP" }, '
        '{ "@type" : "Offer"',
    )
    body = mutate(body, '"priceValidUntil": "2027-10-01" }', '"priceValidUntil": "2027-10-01" }]')
    assert parse_failure(body) == (Stage.PARSE, "AMBIGUOUS_PRICE")


@pytest.mark.parametrize("amount", ["32,500.00 PHP", "PHP 32,500.00", "&#8369;32,500.00 PHP"])
def test_php_code_is_accepted_as_currency_evidence(amount: str) -> None:
    body = mutate(fixture(NO_JSONLD), "&#8369;32,500.00", amount)
    offer = only_offer(body, NO_JSONLD_URL)
    assert (offer.price, offer.currency) == (Decimal("32500.00"), "PHP")


def test_equal_compare_price_is_a_regular_price() -> None:
    body = mutate(fixture(SALE), "<del>&#8369;69,990.00</del>", "<del>&#8369;62,990.00</del>")
    details = only_offer(body, SALE_URL).details
    assert details.price_type is PriceType.REGULAR
    assert (details.regular_price, details.sale_price) == (Decimal("62990.00"), None)


# Variants


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('data-has-only-default-variant="true"', 'data-has-only-default-variant="false"'),
        ('<input name="id" value="50000000000001" type="hidden">',
         '<input name="id" value="50000000000001" type="hidden">'
         '<input name="id" value="50000000000009" type="hidden">'),
        ('<input name="id" value="50000000000001" type="hidden">',
         '<select name="id"><option value="50000000000001">16GB</option>'
         '<option value="50000000000009">32GB</option></select>'),
    ],
)
def test_variant_markup_is_ambiguous(old: str, new: str) -> None:
    assert parse_failure(mutate(fixture(REGULAR), old, new)) == (Stage.PARSE, "AMBIGUOUS_VARIANT")


# Availability


SEO_IN_STOCK = "https://schema.org/InStock"
REVIEWS_IN_STOCK = "http://schema.org/InStock"
ADD_LABEL = '<span class="t4s-btn-atc_text">Add to cart</span>'
ADD_BUTTON_OPEN = 'class="t4s-product-form__submit t4s-btn t4s-btn-base t4s-w-100"'
NOTIFY_HIDDEN = 'class="t4s-pr__notify-stock t4s-d-none"'


@pytest.mark.parametrize(
    ("old", "new", "expected", "conflict"),
    [
        (SEO_IN_STOCK, "https://schema.org/OutOfStock", Availability.UNKNOWN, True),
        (ADD_BUTTON_OPEN, ADD_BUTTON_OPEN + " disabled", Availability.UNKNOWN, True),
        (ADD_BUTTON_OPEN, ADD_BUTTON_OPEN + ' aria-disabled="true"', Availability.UNKNOWN, True),
        (NOTIFY_HIDDEN, 'class="t4s-pr__notify-stock"', Availability.UNKNOWN, True),
        (ADD_LABEL, ADD_LABEL.replace("Add to cart", "Sold out"), Availability.UNKNOWN, True),
        (ADD_LABEL, ADD_LABEL.replace("Add to cart", "Pre-order"), Availability.UNKNOWN, True),
        ("InStock", "BackOrder", Availability.UNKNOWN, False),
    ],
)
def test_jsonld_buttons_and_notify_are_combined(
    old: str, new: str, expected: Availability, conflict: bool
) -> None:
    offer = only_offer(mutate(fixture(REGULAR), old, new))
    assert offer.availability is expected
    assert ("signals conflict" in offer.availability_evidence) is conflict


def test_preorder_needs_jsonld_and_button_to_agree() -> None:
    body = mutate(fixture(REGULAR), "InStock", "PreOrder")
    body = mutate(body, ADD_LABEL, ADD_LABEL.replace("Add to cart", "Pre-order"))
    offer = only_offer(body)
    assert offer.availability is Availability.PREORDER
    assert "add to cart button: Pre-order (enabled)" in offer.availability_evidence


def test_out_of_stock_jsonld_with_a_disabled_button_is_out_of_stock() -> None:
    # The notify-me button is optional evidence; a disabled add-to-cart is consistent.
    notify = 'class="t4s-pr__notify-stock"'
    body = mutate(fixture(SALE), notify, notify.replace('stock"', 'stock t4s-d-none"'))
    offer = only_offer(body, SALE_URL)
    assert offer.availability is Availability.OUT_OF_STOCK
    assert offer.availability_evidence.endswith("notify-me button: hidden")


def test_out_of_stock_with_an_enabled_button_is_a_conflict() -> None:
    body = mutate(fixture(SALE), ' aria-disabled="true" disabled="disabled"', "")
    offer = only_offer(body, SALE_URL)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.endswith("signals conflict")


def test_missing_button_contradicts_in_stock() -> None:
    body = cut(fixture(REGULAR), '<button type="submit" name="add"', "</button>")
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert "add to cart button: absent" in offer.availability_evidence
    assert offer.availability_evidence.endswith("signals conflict")


def test_no_signals_at_all_is_unknown() -> None:
    body = cut(fixture(NO_JSONLD), '<button type="submit"', "</button>")
    body = cut(body, '<button class="t4s-pr__notify-stock', "</button>")
    offer = only_offer(body, NO_JSONLD_URL)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence == (
        "JSON-LD availability: none; add to cart button: absent; notify-me button: absent"
    )


# Layout, identity, and host guards


def test_unrecognized_layout_is_a_parse_failure() -> None:
    assert parse_failure(fixture("pcx_layout_changed.html")) == (Stage.PARSE, "UNRECOGNIZED_LAYOUT")


def test_product_jsonld_without_a_name_or_heading_has_no_title() -> None:
    body = mutate(
        fixture("pcx_bundle.html"),
        '"name": "Example Gaming 16 EG16-0004PH Laptop + FREE Example Gaming Backpack", ',
        "",
    )
    body = cut(body, '<h1 class="t4s-product__title">', "</h1>")
    assert parse_failure(body, BUNDLE_URL) == (Stage.PARSE, "TITLE_NOT_FOUND")


def test_jsonld_for_another_product_is_ambiguous() -> None:
    other = (
        '<script type="application/ld+json">{"@type":"Product","name":"Other Laptop",'
        '"url":"https://pcx.com.ph/products/other-laptop",'
        '"offers":{"@type":"Offer","price":"1"}}</script>'
    )
    body = mutate(fixture(REGULAR), "</head>", other + "</head>")
    assert parse_failure(body) == (Stage.PARSE, "AMBIGUOUS_PRODUCT")


def test_second_jsonld_product_without_a_url_is_ambiguous() -> None:
    other = '<script type="application/ld+json">{"@type":"Product","name":"Other"}</script>'
    body = mutate(fixture(REGULAR), "</head>", other + "</head>")
    assert parse_failure(body) == (Stage.PARSE, "AMBIGUOUS_PRODUCT")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"sku": "LT-EX-000001", "mpn": ""', '"sku": "LT-EX-000099", "mpn": ""'),
        ('"name": "ExampleBrand" },"offers"', '"name": "OtherBrand" },"offers"'),
        ("</h1>", '</h1><h1 class="t4s-product__title">Other Laptop</h1>'),
    ],
)
def test_disagreeing_product_identity_is_ambiguous(old: str, new: str) -> None:
    body = fixture(REGULAR)
    assert old in body
    assert parse_failure(body.replace(old, new, 1)) == (Stage.PARSE, "AMBIGUOUS_PRODUCT")


def test_offer_links_to_other_hosts_are_rejected() -> None:
    body = fixture("pcx_bundle.html").replace(
        '"url": "https://pcx.com.ph/products/example-gaming-16-eg16-0004ph-laptop-free-backpack" }',
        '"url": "https://elsewhere.example.test/products/example-gaming-16-eg16-0004ph-laptop'
        '-free-backpack" }',
    )
    result, _ = run(body, BUNDLE_URL)
    assert not result.observations
    [failure] = result.failures
    assert (failure.stage, failure.message.split(":")[0]) == (Stage.VALIDATE, "offer_url")


@pytest.mark.parametrize("host", ["www.pcx.com.ph", "pcx.com", "shop.pcx.com.ph", "pcx.ph"])
def test_only_the_exact_official_host_is_requested(host: str) -> None:
    result, transport = run("", f"https://{host}/products/x")
    assert transport.calls == []
    assert result.failures[0].error_code == "HOST_NOT_ALLOWED"


@pytest.mark.parametrize(
    "location", ["https://www.pcx.com.ph/products/x", "https://elsewhere.example.test/"]
)
def test_redirect_off_the_official_host_is_not_followed(location: str) -> None:
    clock = FakeClock()
    moved = TransportResponse(301, REGULAR_URL, b"", {"location": location})
    transport = FakeTransport(clock, moved)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(PCXAdapter(), [product_request(REGULAR_URL)], fetcher)
    assert [call["url"] for call in transport.calls] == [REGULAR_URL]
    assert result.failures[0].error_code == "REDIRECT_HOST_NOT_ALLOWED"


def test_redirect_within_the_official_host_is_followed() -> None:
    old_url = f"https://{HOST}/products/example-14-old-handle"
    clock = FakeClock()
    location = REGULAR_URL.removeprefix(f"https://{HOST}")  # relative Location header
    moved = TransportResponse(301, old_url, b"", {"location": location})
    page = TransportResponse(200, REGULAR_URL, fixture(REGULAR).encode(), HTML)
    transport = FakeTransport(clock, moved, page)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(PCXAdapter(), [product_request(old_url)], fetcher)

    assert result.ok, result.failures
    assert [call["url"] for call in transport.calls] == [old_url, REGULAR_URL]
    assert result.observations[0].source.final_url == REGULAR_URL


def test_product_request_accepts_product_pages_on_the_official_host() -> None:
    for url in (REGULAR_URL, f"{REGULAR_URL}?variant=50000000000001", f"{REGULAR_URL}/"):
        request = product_request(url)
        assert (request.retailer_id, request.url) == ("pcx", url)


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/collections/laptops",
        f"https://{HOST}/collections/laptops/products/example-14",
        f"https://{HOST}/search?q=laptop",
        f"https://{HOST}/products/",
        f"https://{HOST}/products/a/b",
        f"https://{HOST}/cart/add",
        "https://www.pcx.com.ph/products/example-14",
        "https://pcx.com/products/example-14",
    ],
)
def test_product_request_accepts_only_product_pages(url: str) -> None:
    with pytest.raises(ValueError):
        product_request(url)


# Live-use gate


def test_live_use_is_enabled_by_the_explicit_owner_decision() -> None:
    assert DATA_USE_REVIEW_STATUS == "OWNER_APPROVED"
    assert PCXAdapter.live_use_approved is True
    assert PCXAdapter.retailer_code == "PCX"
    result, transport = run(fixture(REGULAR), live=True)
    assert result.ok
    assert len(result.observations) == 1
    assert [call["url"] for call in transport.calls] == [REGULAR_URL]


def test_offline_fake_transport_still_parses() -> None:
    result, transport = run(fixture(REGULAR))
    assert result.ok and len(transport.calls) == 1
