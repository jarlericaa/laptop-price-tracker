"""Offline tests for the DataBlitz (DBZ) adapter. Local fixtures and fake transport only."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, FakeTransport, no_network  # noqa: F401

from laptop_tracker.adapters.datablitz import (
    DATA_USE_REVIEW_STATUS,
    HOST,
    DataBlitzAdapter,
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

FIXTURES = Path(__file__).resolve().parent / "scraping_fixtures" / "datablitz"
SALE_URL = f"https://{HOST}/products/example-modern-14-em14-001ph-laptop-urban-silver"
REGULAR_URL = f"https://{HOST}/products/example-notebook-15-en15-01ph-laptop"
BUNDLE_URL = f"https://{HOST}/products/example-gaming-16-eg16-02-laptop-free-backpack"
HTML = {"content-type": "text/html; charset=utf-8"}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def mutate(text: str, old: str, new: str) -> str:
    assert old in text, f"fixture no longer contains {old!r}"
    return text.replace(old, new)


def run(
    body: str, url: str = SALE_URL, *, live: bool = False
) -> tuple[ScrapeResult, FakeTransport]:
    clock = FakeClock()
    response = TransportResponse(200, url, body.encode("utf-8"), HTML)
    transport = FakeTransport(clock, response, live=live)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(DataBlitzAdapter(), [ScrapeRequest("datablitz", url)], fetcher)
    return result, transport


def only_offer(body: str, url: str = SALE_URL):
    result, _ = run(body, url)
    assert result.ok, result.failures
    [observation] = result.observations
    return observation


def parse_failure(body: str, url: str = SALE_URL) -> tuple[Stage, str]:
    result, _ = run(body, url)
    assert not result.observations
    [failure] = result.failures
    return failure.stage, failure.error_code


def cut(text: str, start: str, end: str) -> str:
    """Remove ``text`` from the first ``start`` through the next ``end``, inclusive."""
    begin = text.index(start)
    return text[:begin] + text[text.index(end, begin) + len(end) :]


# JSON-LD product page


def test_jsonld_page_yields_an_attributed_sale_offer() -> None:
    offer = only_offer(fixture("dbz_product_sale.html"))

    assert offer.retailer_id == "datablitz"
    assert offer.offer_url == f"{SALE_URL}?variant=64000000000001"
    assert offer.raw_title == (
        'Example Modern 14 EM14-001PH Laptop (Urban Silver) | 14" FHD+ (1920x1200) IPS | '
        "Core 5 320 | 16GB RAM | 512GB SSD | Windows 11 Home"
    )
    assert (offer.price, offer.currency) == (Decimal("64875.00"), "PHP")
    assert offer.raw_price_text == "₱64,875.00"
    assert offer.availability is Availability.IN_STOCK
    assert offer.availability_evidence == (
        "JSON-LD availability: http://schema.org/InStock; "
        "add to cart button: Add to cart (enabled)"
    )
    assert offer.source.adapter_version == "dbz-1"
    details = offer.details
    assert details.price_type is PriceType.SALE
    assert (details.regular_price, details.regular_price_text) == (
        Decimal("68695.00"),
        "₱68,695.00",
    )
    assert (details.sale_price, details.sale_price_text) == (Decimal("64875.00"), "₱64,875.00")
    assert details.retailer_sku == "4710000000001"
    assert details.retailer_product_id == "15000000000001"
    assert details.brand == "ExampleBrand"
    assert details.manufacturer_part_number == ""  # not stated on the page
    assert details.raw_categories == ("Laptops",)
    assert details.bundle_flag is False and details.bundle_evidence == ()


def test_quick_buy_template_and_recommendations_are_ignored() -> None:
    # The <template> copy has its own Product JSON-LD, PHP 1.00, and "Sold out"; the
    # recommendation card shows PHP 29,995.00 and "Sold out".
    offer = only_offer(fixture("dbz_product_sale.html"))
    assert offer.price == Decimal("64875.00")
    assert "Sold out" not in offer.availability_evidence
    assert "Quick-buy" not in offer.raw_title


def test_description_table_is_preserved_raw_and_nothing_is_inferred() -> None:
    offer = only_offer(fixture("dbz_product_sale.html"))
    specs = dict(offer.details.raw_specifications)

    assert list(specs) == ["CPU", "Operating Systems", "Display", "Memory", "Storage"]
    assert specs["CPU"] == "Example Core 5 320 6 cores, 6 threads"
    assert not any("refresh" in label.lower() for label in specs)
    assert offer.raw_configuration_text.startswith(
        "CPU: Example Core 5 320 6 cores, 6 threads | Operating Systems: Windows 11 Home"
    )


def test_normalization_is_deterministic() -> None:
    first = only_offer(fixture("dbz_product_sale.html")).to_full_record()
    assert first == only_offer(fixture("dbz_product_sale.html")).to_full_record()
    assert "target_id" not in first


def test_jsonld_price_alone_gives_no_sale_or_regular_claim() -> None:
    body = cut(fixture("dbz_product_sale.html"), '<div class="price-list">', "</div>")
    offer = only_offer(body)

    assert offer.availability is Availability.IN_STOCK  # the main form is still intact

    assert (offer.price, offer.raw_price_text) == (Decimal("64875.00"), "64875.00")
    details = offer.details
    assert details.price_type is None
    assert (details.regular_price, details.sale_price) == (None, None)


def test_jsonld_price_alone_requires_php_currency() -> None:
    body = cut(fixture("dbz_product_sale.html"), '<div class="price-list">', "</div>")
    body = mutate(body, '"price":"64875.00","priceCurrency":"PHP"', '"price":"64875.00"')
    assert parse_failure(body) == (Stage.PARSE, "CURRENCY_NOT_FOUND")


def test_short_description_is_used_when_there_is_no_description_table() -> None:
    body = cut(fixture("dbz_product_sale.html"), "<table>", "</table>")
    specs = only_offer(body).details.raw_specifications
    assert specs == (
        ("Processor", "Example Core 5 320"),
        ("Display", '14" FHD+ (1920x1200) IPS'),
        ("Memory", "16GB DDR5"),
    )


# Visible-content fallback (no Product JSON-LD)


def test_visible_content_is_the_fallback_without_jsonld() -> None:
    offer = only_offer(fixture("dbz_product_regular_no_jsonld.html"), f"{REGULAR_URL}?ref=search")

    assert offer.raw_title == (
        'Example Notebook 15 EN15-01PH Laptop | 15.6" FHD | 8GB RAM | 256GB SSD'
    )
    assert offer.offer_url == REGULAR_URL  # relative canonical link resolved against the page
    assert (offer.price, offer.raw_price_text) == (Decimal("32500.00"), "₱32,500.00")
    details = offer.details
    assert details.price_type is PriceType.REGULAR
    assert (details.regular_price, details.sale_price) == (Decimal("32500.00"), None)
    assert (details.retailer_sku, details.brand) == ("EN15-01PH", "ExampleBrand")
    assert details.retailer_product_id == "15000000000002"
    assert details.raw_categories == ()
    # "See full description" is an in-page link, not a specification line.
    assert details.raw_specifications == (
        ("PROCESSOR", "Example CPU 5"),
        ("DISPLAY", '15.6" FHD (1920x1080)'),
        ("OS", "No OS"),
    )
    assert offer.availability is Availability.OUT_OF_STOCK
    assert offer.availability_evidence == (
        "JSON-LD availability: none; add to cart button: Sold out (disabled)"
    )


def test_broken_jsonld_falls_back_to_visible_content() -> None:
    body = mutate(
        fixture("dbz_product_sale.html"), '"@type":"Product","brand"', '"@type":"Product",brand'
    )
    offer = only_offer(body)

    assert offer.raw_title.startswith("Example Modern 14 EM14-001PH Laptop")
    assert offer.offer_url == SALE_URL  # canonical link
    assert offer.details.price_type is PriceType.SALE
    assert offer.details.retailer_sku == "4710000000001"  # visible SKU
    # Without JSON-LD, an enabled "Add to cart" button is not an explicit stock signal.
    assert offer.availability is Availability.UNKNOWN


# Bundles


def test_bundle_listing_is_flagged_with_evidence_and_not_dropped() -> None:
    offer = only_offer(fixture("dbz_bundle.html"), BUNDLE_URL)

    assert offer.raw_title.endswith("+ FREE Example Gaming Backpack")
    assert offer.details.bundle_flag is True
    assert offer.details.bundle_evidence == (
        "title term FREE: + FREE",
        "title term BACKPACK: Backpack",
    )
    assert offer.price == Decimal("89995.00")
    assert offer.details.price_type is PriceType.REGULAR
    assert offer.availability is Availability.IN_STOCK
    assert offer.details.retailer_sku == "4710000000003"  # JSON-LD integer SKU kept as text


@pytest.mark.parametrize(
    ("suffix", "evidence"),
    [
        (' + HP Everyday 16" Laptop Briefcase', "title term BRIEFCASE: Briefcase"),
        (" BUNDLE", "title term BUNDLE: BUNDLE"),
        (" w/ 24in Monitor", "title term MONITOR: Monitor"),
        (" + Gaming Mouse", "title term MOUSE: Mouse"),
        (" + Sleeve Bag", "title term BAG: Bag"),
        (" + Headset", "title term HEADSET: Headset"),
    ],
)
def test_bundle_title_terms_are_flagged(suffix: str, evidence: str) -> None:
    name = '| Windows 11 Home","offers"'
    escaped = suffix.replace('"', '\\"')
    body = mutate(fixture("dbz_product_sale.html"), name, name.replace('"', escaped + '"', 1))
    offer = only_offer(body)
    assert offer.raw_title.endswith(suffix)
    assert offer.details.bundle_flag is True
    assert evidence in offer.details.bundle_evidence


# Prices and currency


SALE_LD_PRICE = '"price":"64875.00","priceCurrency":"PHP"'
SALE_COMPARE = (
    '<span class="price price--compare">\n'
    '              <span class="visually-hidden">Regular price</span>&#8369;68,695.00</span>'
)
REGULAR_AMOUNT = "&#8369;32,500.00"
REGULAR_SPAN = (
    '<span class="price">\n          <span class="visually-hidden">Sale price</span>'
    f"{REGULAR_AMOUNT}</span>"
)


@pytest.mark.parametrize(
    ("name", "old", "new", "code"),
    [
        ("dbz_product_sale.html", SALE_LD_PRICE, SALE_LD_PRICE.replace("648", "638"),
         "PRICE_CONFLICT"),
        ("dbz_product_sale.html", "&#8369;68,695.00", "&#8369;60,000.00", "PRICE_CONFLICT"),
        ("dbz_product_sale.html", SALE_LD_PRICE, SALE_LD_PRICE.replace("PHP", "USD"),
         "UNSUPPORTED_CURRENCY"),
        ("dbz_product_sale.html", SALE_COMPARE, "", "AMBIGUOUS_PRICE"),
        ("dbz_product_sale.html", '"@type":"Offer","availability"',
         '"@type":"AggregateOffer","availability"',
         "AMBIGUOUS_PRICE"),
        ("dbz_product_regular_no_jsonld.html", REGULAR_AMOUNT, "Call for price",
         "PRICE_NOT_FOUND"),
        ("dbz_product_regular_no_jsonld.html", REGULAR_AMOUNT,
         "&#8369;30,000.00 &ndash; &#8369;32,500.00", "AMBIGUOUS_PRICE"),
        ("dbz_product_regular_no_jsonld.html", REGULAR_AMOUNT, f"From {REGULAR_AMOUNT}",
         "AMBIGUOUS_PRICE"),
        ("dbz_product_regular_no_jsonld.html", REGULAR_SPAN, REGULAR_SPAN * 2,
         "AMBIGUOUS_PRICE"),
        ("dbz_product_regular_no_jsonld.html", '<div class="product-form__buy-buttons">',
         f'<div class="price-list">{REGULAR_SPAN}</div><div class="product-form__buy-buttons">',
         "AMBIGUOUS_PRICE"),
        ("dbz_product_regular_no_jsonld.html", REGULAR_AMOUNT, "$32,500.00",
         "UNSUPPORTED_CURRENCY"),
        ("dbz_product_regular_no_jsonld.html", REGULAR_AMOUNT, "&#8369;32,500.00 USD",
         "UNSUPPORTED_CURRENCY"),
        ("dbz_product_regular_no_jsonld.html", REGULAR_AMOUNT, "32,500.00",
         "CURRENCY_NOT_FOUND"),
        ("dbz_product_regular_no_jsonld.html", REGULAR_AMOUNT, "&#8369;0.00", "INVALID_PRICE"),
    ],
)
def test_unclear_prices_are_parse_failures(name: str, old: str, new: str, code: str) -> None:
    url = SALE_URL if "sale" in name else REGULAR_URL
    assert parse_failure(mutate(fixture(name), old, new), url) == (Stage.PARSE, code)


def test_several_jsonld_offers_are_ambiguous() -> None:
    body = mutate(
        fixture("dbz_product_sale.html"),
        '"offers":{"@id"',
        '"offers":[{"@type":"Offer","price":"70000.00","priceCurrency":"PHP"},{"@id"',
    )
    body = mutate(body, '},"sku":"4710000000001"', '}],"sku":"4710000000001"')
    assert parse_failure(body) == (Stage.PARSE, "AMBIGUOUS_PRICE")


@pytest.mark.parametrize("amount", ["32,500.00 PHP", "PHP 32,500.00", "&#8369;32,500.00 PHP"])
def test_php_code_is_accepted_as_currency_evidence(amount: str) -> None:
    body = mutate(fixture("dbz_product_regular_no_jsonld.html"), REGULAR_AMOUNT, amount)
    offer = only_offer(body, REGULAR_URL)
    assert (offer.price, offer.currency) == (Decimal("32500.00"), "PHP")


def test_equal_compare_price_is_a_regular_price() -> None:
    body = mutate(fixture("dbz_product_sale.html"), "&#8369;68,695.00", "&#8369;64,875.00")
    details = only_offer(body).details
    assert details.price_type is PriceType.REGULAR
    assert (details.regular_price, details.sale_price) == (Decimal("64875.00"), None)


def test_on_sale_label_is_not_read_as_a_price() -> None:
    # The "Save PHP 3,820.00" label sits outside .price-list and must not count as an amount.
    offer = only_offer(fixture("dbz_product_sale.html"))
    assert Decimal("3820.00") not in (offer.price, offer.details.regular_price)


# Availability


IN_STOCK_LD = "http:\\/\\/schema.org\\/InStock"
ADD_BUTTON = (
    '<button type="submit" class="product-form__add-button button button--primary">'
    "Add to cart</button>"
)
SOLD_OUT_BUTTON = (
    '<button type="submit" class="product-form__add-button button button--disabled" disabled>'
    "Sold out</button>"
)


@pytest.mark.parametrize(
    ("old", "new", "expected", "conflict"),
    [
        (IN_STOCK_LD, "http:\\/\\/schema.org\\/OutOfStock", Availability.UNKNOWN, True),
        (ADD_BUTTON, ADD_BUTTON.replace("button--primary", "button--disabled"),
         Availability.UNKNOWN, True),
        (ADD_BUTTON, SOLD_OUT_BUTTON, Availability.UNKNOWN, True),
        (ADD_BUTTON, ADD_BUTTON.replace("Add to cart", "Pre-order"), Availability.UNKNOWN, True),
        (ADD_BUTTON, ADD_BUTTON * 2, Availability.IN_STOCK, False),  # identical copies agree
        (ADD_BUTTON, ADD_BUTTON + SOLD_OUT_BUTTON, Availability.UNKNOWN, True),
        (IN_STOCK_LD, "http:\\/\\/schema.org\\/BackOrder", Availability.UNKNOWN, False),
        (IN_STOCK_LD, "http:\\/\\/schema.org\\/SoldOut", Availability.UNKNOWN, True),
    ],
)
def test_jsonld_and_button_are_combined(
    old: str, new: str, expected: Availability, conflict: bool
) -> None:
    offer = only_offer(mutate(fixture("dbz_product_sale.html"), old, new))
    assert offer.availability is expected
    assert ("signals conflict" in offer.availability_evidence) is conflict


def test_preorder_needs_jsonld_and_button_to_agree() -> None:
    body = mutate(fixture("dbz_product_sale.html"), IN_STOCK_LD, IN_STOCK_LD[:-7] + "PreOrder")
    body = mutate(body, ADD_BUTTON, ADD_BUTTON.replace("Add to cart", "Pre-order"))
    offer = only_offer(body)
    assert offer.availability is Availability.PREORDER
    assert offer.availability_evidence.endswith("add to cart button: Pre-order (enabled)")


def test_out_of_stock_jsonld_with_sold_out_button_is_out_of_stock() -> None:
    body = mutate(fixture("dbz_product_sale.html"), IN_STOCK_LD, IN_STOCK_LD[:-7] + "OutOfStock")
    offer = only_offer(mutate(body, ADD_BUTTON, SOLD_OUT_BUTTON))
    assert offer.availability is Availability.OUT_OF_STOCK
    assert "signals conflict" not in offer.availability_evidence


def test_missing_button_contradicts_in_stock() -> None:
    offer = only_offer(mutate(fixture("dbz_product_sale.html"), ADD_BUTTON, ""))
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.endswith("add to cart button: absent; signals conflict")


@pytest.mark.parametrize(
    ("label", "disabled"),
    [("Add to cart", False), ("Unavailable", True), ("Notify me", False)],
)
def test_no_explicit_stock_signal_is_unknown_even_with_a_price(label: str, disabled: bool) -> None:
    button = (
        f'<button type="submit" class="product-form__add-button button"'
        f"{' disabled' if disabled else ''}>{label}</button>"
    )
    body = mutate(fixture("dbz_product_regular_no_jsonld.html"), SOLD_OUT_BUTTON, button)
    offer = only_offer(body, REGULAR_URL)
    assert offer.availability is Availability.UNKNOWN
    assert f"add to cart button: {label}" in offer.availability_evidence


def test_no_signals_at_all_is_unknown() -> None:
    body = mutate(fixture("dbz_product_regular_no_jsonld.html"), SOLD_OUT_BUTTON, "")
    offer = only_offer(body, REGULAR_URL)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence == "JSON-LD availability: none; add to cart button: absent"


# Layout and host guards


def test_unrecognized_layout_is_a_parse_failure() -> None:
    assert parse_failure(fixture("dbz_layout_changed.html")) == (Stage.PARSE, "UNRECOGNIZED_LAYOUT")


def test_product_jsonld_without_a_name_or_heading_has_no_title() -> None:
    body = cut(fixture("dbz_bundle.html"), '"name":"Example Gaming', '",')
    body = cut(body, '<h1 class="product-meta__title', "</h1>")
    assert parse_failure(body, BUNDLE_URL) == (Stage.PARSE, "TITLE_NOT_FOUND")


def test_two_products_in_jsonld_are_ambiguous() -> None:
    second = (
        '<script type="application/ld+json">{"@type":"Product","name":"Other Laptop",'
        '"offers":{"@type":"Offer","price":"1"}}</script>'
    )
    body = mutate(fixture("dbz_product_sale.html"), "</head>", second + "</head>")
    assert parse_failure(body) == (Stage.PARSE, "AMBIGUOUS_PRODUCT")


def test_offer_links_to_other_hosts_are_rejected() -> None:
    body = mutate(
        fixture("dbz_product_sale.html"),
        '"url":"https:\\/\\/ecommerce.datablitz.com.ph\\/products\\/example-modern-14-em14-001ph-'
        'laptop-urban-silver?variant',
        '"url":"https:\\/\\/elsewhere.example.test\\/products\\/x?variant',
    )
    result, _ = run(body)
    assert not result.observations
    [failure] = result.failures
    assert (failure.stage, failure.message.split(":")[0]) == (Stage.VALIDATE, "offer_url")


@pytest.mark.parametrize(
    "host", ["datablitz.com.ph", "www.datablitz.com.ph", "ecommerce.datablitz.com"]
)
def test_only_the_exact_official_host_is_requested(host: str) -> None:
    result, transport = run("", f"https://{host}/products/x")
    assert transport.calls == []
    assert result.failures[0].error_code == "HOST_NOT_ALLOWED"


def test_redirect_off_the_official_host_is_not_followed() -> None:
    clock = FakeClock()
    moved = TransportResponse(301, SALE_URL, b"", {"location": "https://datablitz.com.ph/"})
    transport = FakeTransport(clock, moved)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(DataBlitzAdapter(), [product_request(SALE_URL)], fetcher)
    assert [call["url"] for call in transport.calls] == [SALE_URL]
    assert result.failures[0].error_code == "REDIRECT_HOST_NOT_ALLOWED"


def test_redirect_within_the_official_host_is_followed() -> None:
    old_url = f"https://{HOST}/products/example-modern-14-old-handle"
    clock = FakeClock()
    location = SALE_URL.removeprefix(f"https://{HOST}")  # relative Location header
    moved = TransportResponse(301, old_url, b"", {"location": location})
    page = TransportResponse(200, SALE_URL, fixture("dbz_product_sale.html").encode(), HTML)
    transport = FakeTransport(clock, moved, page)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(DataBlitzAdapter(), [product_request(old_url)], fetcher)

    assert result.ok, result.failures
    assert [call["url"] for call in transport.calls] == [old_url, SALE_URL]
    assert result.observations[0].source.final_url == SALE_URL


def test_product_request_accepts_product_pages_on_the_official_host() -> None:
    url = f"{SALE_URL}?variant=64000000000001"
    assert (product_request(url).retailer_id, product_request(url).url) == ("datablitz", url)


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/collections/laptops",
        f"https://{HOST}/collections/laptops/products/example-modern-14",
        f"https://{HOST}/search?q=laptop",
        f"https://{HOST}/products/",
        f"https://{HOST}/products/a/b",
        "https://datablitz.com.ph/products/example-modern-14",
        "https://www.datablitz.com.ph/products/example-modern-14",
    ],
)
def test_product_request_accepts_only_product_pages(url: str) -> None:
    with pytest.raises(ValueError):
        product_request(url)


# Live-use gate


def test_live_use_is_enabled_by_owner_policy() -> None:
    assert DATA_USE_REVIEW_STATUS == "OWNER_APPROVED"
    assert DataBlitzAdapter.live_use_approved is True
    assert DataBlitzAdapter.retailer_code == "DBZ"
    result, transport = run(fixture("dbz_product_sale.html"), live=True)
    assert result.ok
    assert len(transport.calls) == 1
