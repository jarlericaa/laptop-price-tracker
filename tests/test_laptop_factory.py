"""Offline tests for the Laptop Factory (LF) adapter. Local fixtures and fake transport only."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, FakeTransport, no_network  # noqa: F401

from laptop_tracker.adapters.laptop_factory import (
    DATA_USE_REVIEW_STATUS,
    HOST,
    LaptopFactoryAdapter,
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

FIXTURES = Path(__file__).resolve().parent / "scraping_fixtures" / "laptop_factory"
SALE_URL = f"https://{HOST}/product/acer-aspire-ai-14-a14-11m-x3bp/"
REGULAR_URL = f"https://{HOST}/product/example-notebook-15-regular/"
BUNDLE_URL = f"https://{HOST}/product/example-megabook-k15-free-ergochair/"
HTML = {"content-type": "text/html; charset=UTF-8"}


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
    [result] = scrape(LaptopFactoryAdapter(), [ScrapeRequest("laptop_factory", url)], fetcher)
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


# JSON-LD product page


def test_jsonld_page_yields_an_attributed_sale_offer() -> None:
    offer = only_offer(fixture("lf_product_sale.html"))

    assert offer.retailer_id == "laptop_factory"
    assert offer.offer_url == SALE_URL
    assert offer.raw_title == "Acer Aspire AI 14 A14-11M-X3BP"
    assert (offer.price, offer.currency) == (Decimal("45999.00"), "PHP")
    assert offer.raw_price_text == "₱45,999.00"
    assert offer.availability is Availability.IN_STOCK
    assert offer.availability_evidence == (
        "stock text: In stock; JSON-LD availability: https://schema.org/InStock; "
        "add to cart: enabled"
    )
    assert offer.source.adapter_version == "lf-2"
    details = offer.details
    assert details.price_type is PriceType.SALE
    assert (details.regular_price, details.regular_price_text) == (
        Decimal("50999.00"),
        "₱50,999.00",
    )
    assert (details.sale_price, details.sale_price_text) == (Decimal("45999.00"), "₱45,999.00")
    assert details.retailer_sku == "Acer Aspire 14 A14-11M-X3BP - Steam Blue"
    assert details.retailer_product_id == "42342"
    assert details.raw_categories == ("Acer", "Mainstream Laptops")
    assert (details.brand, details.manufacturer_part_number) == ("", "")  # not stated on page
    assert details.bundle_flag is False and details.bundle_evidence == ()


def test_related_products_do_not_affect_price_or_stock() -> None:
    # The fixture's related product shows PHP 29,999.00 and "Out of stock".
    offer = only_offer(fixture("lf_product_sale.html"))
    assert offer.price == Decimal("45999.00")
    assert "Out of stock" not in offer.availability_evidence


def test_tech_specs_are_preserved_raw_and_refresh_rate_is_not_inferred() -> None:
    offer = only_offer(fixture("lf_product_sale.html"))
    specs = dict(offer.details.raw_specifications)

    assert list(specs)[:2] == ["Model Name", "Processor"]
    assert specs["Display Size"] == "14″ WUXGA (1920×1200) IPS"
    assert specs["Inclusions"] == "Acer Backpack MS Office Home and Student"
    assert not any("refresh" in label.lower() for label in specs)
    assert offer.raw_configuration_text.startswith(
        "Model Name: Acer Aspire AI 14 A14-11M-X3BP (Steam Blue) | Processor: "
    )


def test_normalization_is_deterministic() -> None:
    first = only_offer(fixture("lf_product_sale.html")).to_full_record()
    assert first == only_offer(fixture("lf_product_sale.html")).to_full_record()
    assert "target_id" not in first


def test_jsonld_price_is_used_when_no_price_is_displayed() -> None:
    body = fixture("lf_product_sale.html")
    start, end = body.index('<div class="price-wrapper">'), body.index('<div class="product-short')
    offer = only_offer(body[:start] + body[end:])

    assert (offer.price, offer.raw_price_text) == (Decimal("45999.00"), "45999.00")
    assert offer.details.price_type is PriceType.SALE
    assert offer.details.regular_price_text == "50999.00"


# Visible-content fallback (no JSON-LD)


def test_visible_content_and_tech_specs_are_the_fallback_without_jsonld() -> None:
    offer = only_offer(fixture("lf_product_regular_no_jsonld.html"), f"{REGULAR_URL}?ref=search")

    assert offer.raw_title == "Example Notebook 15 EN15-01"
    assert offer.offer_url == REGULAR_URL  # relative canonical link resolved against the page
    assert offer.price == Decimal("32500.00")
    details = offer.details
    assert details.price_type is PriceType.REGULAR
    assert (details.regular_price, details.sale_price) == (Decimal("32500.00"), None)
    assert details.retailer_product_id == "50001" and details.retailer_sku == ""
    assert dict(details.raw_specifications)["Operating System"] == "No OS"
    assert offer.availability is Availability.OUT_OF_STOCK
    assert offer.availability_evidence == (
        "stock text: Out of stock; JSON-LD availability: none; add to cart: absent"
    )


# Bundles


def test_bundle_listing_is_flagged_with_evidence_and_not_dropped() -> None:
    offer = only_offer(fixture("lf_bundle.html"), BUNDLE_URL)

    assert offer.raw_title == "Example Megabook K15 + FREE ERGOCHAIR AIRLITE (BLACK)"
    assert offer.details.bundle_flag is True
    assert offer.details.bundle_evidence == (
        "title term FREE: + FREE",
        "title term CHAIR: ERGOCHAIR",
        "bundle components: Example Megabook K15 ; AIRLITE ERGOCHAIR - Black",
    )
    # Component prices and a component's "Out of stock" row are not the offer's.
    assert offer.price == Decimal("49999.00")
    assert offer.details.regular_price == Decimal("52998.00")
    assert offer.availability is Availability.IN_STOCK
    assert offer.details.retailer_sku == "60001"  # JSON-LD integer SKU kept as text


@pytest.mark.parametrize(
    ("suffix", "evidence"),
    [
        (" BUNDLE", "title term BUNDLE: BUNDLE"),
        (" w/ 24in Monitor", "title term MONITOR: Monitor"),
        (" + Tablet", "title term TABLET: Tablet"),
        (" with Gaming Chair", "title term CHAIR: Chair"),
        (" w/ FREE Backpack", "title term FREE: FREE"),
    ],
)
def test_bundle_title_terms_are_flagged(suffix: str, evidence: str) -> None:
    name = '"name":"Acer Aspire AI 14 A14-11M-X3BP"'
    body = mutate(fixture("lf_product_sale.html"), name, name[:-1] + suffix + '"')
    offer = only_offer(body)
    assert offer.details.bundle_flag is True
    assert evidence in offer.details.bundle_evidence


# Price failures


SALE_JSONLD_PRICE = '"price":"45999.00","priceCurrency":"PHP"}]'
REGULAR_AMOUNT = (
    '<span class="woocommerce-Price-amount amount"><bdi><span class="woocommerce-Price-'
    'currencySymbol">&#8369;</span>32,500.00</bdi></span>'
)


@pytest.mark.parametrize(
    ("name", "old", "new", "code"),
    [
        ("lf_product_sale.html", SALE_JSONLD_PRICE, SALE_JSONLD_PRICE.replace("45", "44"),
         "PRICE_CONFLICT"),
        ("lf_product_sale.html", "50,999.00</bdi></span></del>", "51,999.00</bdi></span></del>",
         "PRICE_CONFLICT"),
        ("lf_product_sale.html", SALE_JSONLD_PRICE, SALE_JSONLD_PRICE.replace("PHP", "USD"),
         "UNSUPPORTED_CURRENCY"),
        ("lf_product_regular_no_jsonld.html", REGULAR_AMOUNT, "Call for price", "PRICE_NOT_FOUND"),
        ("lf_product_regular_no_jsonld.html", REGULAR_AMOUNT,
         REGULAR_AMOUNT.replace("32,500", "30,000") + " &ndash; " + REGULAR_AMOUNT,
         "AMBIGUOUS_PRICE"),
        ("lf_product_regular_no_jsonld.html", REGULAR_AMOUNT, f"<del>{REGULAR_AMOUNT}</del>",
         "AMBIGUOUS_PRICE"),
        ("lf_product_regular_no_jsonld.html", "<p class=\"stock",
         f'<p class="price">{REGULAR_AMOUNT}</p><p class="stock', "AMBIGUOUS_PRICE"),
        ("lf_product_regular_no_jsonld.html", "&#8369;</span>32,500", "$</span>32,500",
         "UNSUPPORTED_CURRENCY"),
    ],
)
def test_unclear_prices_are_parse_failures(name: str, old: str, new: str, code: str) -> None:
    url = SALE_URL if "sale" in name else REGULAR_URL
    assert parse_failure(mutate(fixture(name), old, new), url) == (Stage.PARSE, code)


# Availability


IN_STOCK_LD = "https://schema.org/InStock"
CART_BUTTON = 'class="single_add_to_cart_button button alt"'


@pytest.mark.parametrize(
    ("old", "new", "expected", "conflict"),
    [
        (IN_STOCK_LD, "https://schema.org/OutOfStock", Availability.UNKNOWN, True),
        (CART_BUTTON, CART_BUTTON + " disabled", Availability.UNKNOWN, True),
        ('<p class="stock in-stock">In stock</p>', '<p class="stock in-stock">5 in stock</p>',
         Availability.IN_STOCK, False),
        ('<p class="stock in-stock">In stock</p>', "", Availability.IN_STOCK, False),
    ],
)
def test_stock_wording_jsonld_and_cart_are_combined(
    old: str, new: str, expected: Availability, conflict: bool
) -> None:
    offer = only_offer(mutate(fixture("lf_product_sale.html"), old, new))
    assert offer.availability is expected
    assert ("signals conflict" in offer.availability_evidence) is conflict


def test_missing_cart_button_contradicts_in_stock() -> None:
    body = fixture("lf_product_sale.html")
    start = body.index("<button")
    body = body[:start] + body[body.index("</button>") + len("</button>") :]
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence.endswith("add to cart: absent; signals conflict")


def test_backorder_wording_is_unknown_with_evidence() -> None:
    body = mutate(fixture("lf_product_sale.html"), "In stock</p>", "Available on backorder</p>")
    body = mutate(body, IN_STOCK_LD, "https://schema.org/BackOrder")
    offer = only_offer(body)
    assert offer.availability is Availability.UNKNOWN
    assert "stock text: Available on backorder" in offer.availability_evidence


def test_no_explicit_stock_signal_is_unknown_even_with_a_price() -> None:
    body = mutate(fixture("lf_product_regular_no_jsonld.html"), "Out of stock</p>", "</p>")
    offer = only_offer(body, REGULAR_URL)
    assert offer.availability is Availability.UNKNOWN
    assert offer.availability_evidence == (
        "stock text: none; JSON-LD availability: none; add to cart: absent"
    )


# Layout and host guards


def test_unrecognized_layout_is_a_parse_failure() -> None:
    assert parse_failure(fixture("lf_layout_changed.html")) == (Stage.PARSE, "UNRECOGNIZED_LAYOUT")


def test_two_products_in_jsonld_are_ambiguous() -> None:
    body = fixture("lf_product_sale.html")
    second = '{"@type":"Product","name":"Other Laptop","offers":{"@type":"Offer","price":"1"}}'
    body = mutate(body, '"itemListElement":[]},', '"itemListElement":[]},' + second + ",")
    assert parse_failure(body) == (Stage.PARSE, "AMBIGUOUS_PRODUCT")


def test_offer_links_to_other_hosts_are_rejected() -> None:
    body = mutate(
        fixture("lf_product_sale.html"),
        '"availability":"https://schema.org/InStock","url":"https://laptopfactory.com.ph/',
        '"availability":"https://schema.org/InStock","url":"https://elsewhere.example.test/',
    )
    result, _ = run(body)
    assert not result.observations
    [failure] = result.failures
    assert (failure.stage, failure.message.split(":")[0]) == (Stage.VALIDATE, "offer_url")


@pytest.mark.parametrize("host", ["www.laptopfactory.com.ph", "laptopfactory.com"])
def test_only_the_exact_official_host_is_requested(host: str) -> None:
    result, transport = run("", f"https://{host}/product/x/")
    assert transport.calls == []
    assert result.failures[0].error_code == "HOST_NOT_ALLOWED"


def test_redirect_off_the_official_host_is_not_followed() -> None:
    clock = FakeClock()
    moved = TransportResponse(301, SALE_URL, b"", {"location": "https://www.laptopfactory.com.ph/"})
    transport = FakeTransport(clock, moved)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(LaptopFactoryAdapter(), [product_request(SALE_URL)], fetcher)
    assert [call["url"] for call in transport.calls] == [SALE_URL]
    assert result.failures[0].error_code == "REDIRECT_HOST_NOT_ALLOWED"


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/product-category/mainstream-laptops/",
        f"https://{HOST}/?s=laptop",
        "https://www.laptopfactory.com.ph/product/x/",
        f"http://{HOST}/product/x/",
        f"https://{HOST}/product/",
        f"https://{HOST}/product/x/y/",
        f"https://{HOST}/product/x/?add-to-cart=42342",
        f"https://{HOST}/product/x/?utm_source=example",
        f"https://{HOST}/product/x/#tab-description",
        f"[https://{HOST}/product/x/](https://{HOST}/product/x/)",
        f" https://{HOST}/product/x/",
    ],
)
def test_product_request_accepts_only_product_pages(url: str) -> None:
    with pytest.raises(ValueError):
        product_request(url)


@pytest.mark.parametrize("url", [SALE_URL, f"https://{HOST}/product/example-notebook-15"])
def test_product_request_keeps_a_bare_product_url_unchanged(url: str) -> None:
    request = product_request(url)
    assert (request.retailer_id, request.url) == ("laptop_factory", url)


# Live-use gate


def test_live_use_is_enabled_by_owner_policy() -> None:
    assert DATA_USE_REVIEW_STATUS == "OWNER_APPROVED"
    assert LaptopFactoryAdapter.live_use_approved is True
    result, transport = run(fixture("lf_product_sale.html"), live=True)
    assert result.ok
    assert len(transport.calls) == 1


def test_default_fetcher_is_live_but_do_not_make_a_network_request() -> None:
    assert Fetcher().is_live is True
    assert LaptopFactoryAdapter.live_use_approved is True


# Additional-information specification table (layout seen on live pages 2026-10-27)

ADDITIONAL_INFO_ROWS = (
    ("Model Name", "Acer Aspire AI 14 A14-11M-X3BP (Steam Blue)"),
    ("Processor", "Qualcomm Snapdragon X, X1-26-100 processor"),
    ("RAM", "16GB of onboard LPDDR5X"),
    ("Storage", "512GB NVMe SSD"),
)


def additional_info_tabs(rows: tuple[tuple[str, str], ...] = ADDITIONAL_INFO_ROWS) -> str:
    """The WooCommerce Additional-information tab with its attribute table."""
    item = "woocommerce-product-attributes-item"
    body = "".join(
        f'<tr class="{item}"><th class="{item}__label" scope="row">{label}</th>'
        f'<td class="{item}__value"><p>{value}</p></td></tr>\n'
        for label, value in rows
    )
    return (
        '<div class="woocommerce-tabs wc-tabs-wrapper"><div class="tab-panels">\n'
        '<div class="woocommerce-Tabs-panel woocommerce-Tabs-panel--additional_information '
        'panel entry-content active" id="tab-additional_information" role="tabpanel">\n'
        '<table class="woocommerce-product-attributes shop_attributes" '
        f'aria-label="Product Details">\n{body}</table>\n</div></div></div>\n'
    )


def additional_info_page() -> str:
    """The sale fixture with its specifications moved from the short description to the tab."""
    body = fixture("lf_product_sale.html")
    start = body.index('    <div class="product-short-description">')
    end = body.index('    <p class="stock in-stock">')
    return body[:start] + additional_info_tabs() + body[end:]


def test_additional_information_table_supplies_the_specifications() -> None:
    offer = only_offer(additional_info_page())

    assert offer.source.adapter_version == "lf-2"
    assert offer.details.raw_specifications == (
        ("Model Name", "Acer Aspire AI 14 A14-11M-X3BP (Steam Blue)"),
        ("Processor", "Qualcomm Snapdragon X, X1-26-100 processor"),
        ("RAM", "16GB of onboard LPDDR5X"),
        ("Storage", "512GB NVMe SSD"),
    )
    assert offer.raw_configuration_text == (
        "Model Name: Acer Aspire AI 14 A14-11M-X3BP (Steam Blue) | "
        "Processor: Qualcomm Snapdragon X, X1-26-100 processor | "
        "RAM: 16GB of onboard LPDDR5X | Storage: 512GB NVMe SSD"
    )
    # Price, stock and bundle signals are unaffected by where the specifications sit.
    assert (offer.price, offer.availability) == (Decimal("45999.00"), Availability.IN_STOCK)
    assert offer.details.bundle_flag is False


def test_short_description_specifications_win_over_the_additional_information_table() -> None:
    both = mutate(
        fixture("lf_product_sale.html"),
        '    <p class="stock in-stock">',
        additional_info_tabs((*ADDITIONAL_INFO_ROWS[:3], ("Storage", "1TB NVMe SSD")))
        + '    <p class="stock in-stock">',
    )
    offer = only_offer(both)

    assert ("Storage", "512GB NVMe SSD") in offer.details.raw_specifications
    assert ("Storage", "1TB NVMe SSD") not in offer.details.raw_specifications


def test_a_page_without_any_specification_table_keeps_the_json_ld_description() -> None:
    body = fixture("lf_product_sale.html")
    start = body.index('    <div class="product-short-description">')
    end = body.index('    <p class="stock in-stock">')
    offer = only_offer(body[:start] + body[end:])

    assert offer.details.raw_specifications == ()
    assert offer.raw_configuration_text.startswith("Tech Specs: Model Name")
