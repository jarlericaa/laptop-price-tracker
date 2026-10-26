"""Purchase-block restrictions (2026-10-22): a matched product is not yet an unrestricted price.

The PC Express page of ASUS Vivobook Go 15 E1504FA-NJ1527W (saved in the 2026-10-22 rerun)
states "*Website Exclusive, Promo Price Applicable for Cash Payments Only" under its price. The
fixture ``pcx_cash_only_promo.html`` keeps that block's structure with synthetic product data.
Identity matching, stock and the extracted price stay as they were; only the comparison
eligibility of the price changes, and its reason is kept.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, FakeTransport, no_network  # noqa: F401
from test_offer_terms import page

from laptop_tracker.adapters.pcx import HOST, PCXAdapter
from laptop_tracker.offer_terms import (
    BUNDLE_FLAG_POLICY,
    CASH_ONLY,
    PRICE_SCOPE,
    PURCHASE_TERMS_POLICY,
    PURCHASE_TERMS_UNKNOWN,
    RESTRICTED_PRICE,
    STANDARD_OFFER,
    classify_offer_terms,
    scan_purchase_restrictions,
    terms_price_effect,
)
from laptop_tracker.offers import PURCHASE_TERMS
from laptop_tracker.scraping import (
    FetchConfig,
    Fetcher,
    ScrapeRequest,
    TransportResponse,
    scrape,
)

pytestmark = pytest.mark.usefixtures("no_network")

FIXTURES = Path(__file__).resolve().parent / "scraping_fixtures" / "pcx"
URL = f"https://{HOST}/products/example-14-ex0001tu-x0x00pa-intel-core-5-laptop-natural-silver"
NOTE = "*Website Exclusive, Promo Price Applicable for Cash Payments Only"


def parse(name: str):
    clock = FakeClock()
    body = (FIXTURES / name).read_text(encoding="utf-8").encode("utf-8")
    response = TransportResponse(200, URL, body, {"content-type": "text/html; charset=utf-8"})
    fetcher = Fetcher(FakeTransport(clock, response), FetchConfig(), clock)
    [result] = scrape(PCXAdapter(), [ScrapeRequest("pcx", URL)], fetcher)
    assert result.ok, result.failures
    return result.observations[0]


def test_scan_finds_a_cash_only_price_and_keeps_the_wording() -> None:
    [found] = scan_purchase_restrictions(NOTE)
    assert (found.kind, found.scope, found.evidence_text) == (CASH_ONLY, PRICE_SCOPE, NOTE)


@pytest.mark.parametrize(
    "text",
    [
        "Cash on delivery available",
        "Pay by cash, credit card or GCash",
        "0% installment available with selected banks",
        "Prices are subject to change without prior notice.",
        "Cash back up to PHP 500 on selected cards",
    ],
)
def test_ordinary_payment_wording_is_not_a_restriction(text) -> None:
    assert scan_purchase_restrictions(text) == ()


def test_pcx_adapter_records_the_cash_only_footnote_as_purchase_terms() -> None:
    observation = parse("pcx_cash_only_promo.html")
    items = [item for item in observation.details.scoped_evidence if item[0] == PURCHASE_TERMS]
    assert items == [(PURCHASE_TERMS, f"{CASH_ONLY} ({PRICE_SCOPE})", NOTE)]
    # Nothing else about the offer changes: the same price, type and availability as without it.
    plain = parse("pcx_product_regular.html")
    assert (observation.price, observation.details.price_type) == (
        plain.price, plain.details.price_type)
    assert not [i for i in plain.details.scoped_evidence if i[0] == PURCHASE_TERMS]


def test_a_page_without_the_footnote_is_unchanged() -> None:
    observation = parse("pcx_product_regular.html")
    assert classify_offer_terms(observation).category == STANDARD_OFFER


def test_a_cash_only_price_is_excluded_under_both_policies_with_its_wording() -> None:
    terms = classify_offer_terms(parse("pcx_cash_only_promo.html"))
    assert terms.category == RESTRICTED_PRICE
    assert any(CASH_ONLY in item and NOTE in item for item in terms.evidence)
    assert [r.kind for r in terms.restrictions] == [CASH_ONLY]
    for policy in (PURCHASE_TERMS_POLICY, BUNDLE_FLAG_POLICY):
        reasons, _ = terms_price_effect(terms, "FALSE", policy)
        assert reasons == ("RESTRICTED_PRICE",), policy


def test_a_page_scope_restriction_is_recorded_but_does_not_block_the_price() -> None:
    observation = parse("pcx_product_regular.html")
    note = "Trade-in program available in store"
    details = replace(observation.details, scoped_evidence=(
        *observation.details.scoped_evidence,
        (PURCHASE_TERMS, "TRADE_IN_REQUIRED (PAGE)", note),
    ))
    terms = classify_offer_terms(replace(observation, details=details))
    assert terms.category == STANDARD_OFFER
    assert [r.scope for r in terms.restrictions] == ["PAGE"]


def test_description_prose_with_the_cash_wording_is_restricted_too() -> None:
    observation = page(prose=(NOTE,))
    assert classify_offer_terms(observation).category == RESTRICTED_PRICE
    assert classify_offer_terms(page(prose=("Cash on delivery available",))).category == (
        STANDARD_OFFER)


# DataBlitz backpack titles (2026-10-22 rerun): the same page data was priced on 2026-10-20 and
# blank on 2026-10-22. The saved observations of both runs carry the identical title and the
# identical adapter evidence; only the offer-terms policy differs (the 2026-10-20 report passed
# --offer-terms-policy bundle-flag/2026-10-15, the rerun used the default purchase-terms
# policy). A bare "Backpack" title segment states no inclusion, so it is PURCHASE_TERMS_UNKNOWN.
DBZ_TITLE = (
    'ASUS TUF Gaming FA506NCQ-HN012W Laptop (Graphite Black) | 15.6" FHD (1920x1080) 144Hz IPS '
    "| Ryzen 7 170 | 8GB RAM | 512GB SSD | RTX 3050 | Windows 11 Home | ASUS VP4700 Backpack"
)


def test_datablitz_backpack_title_differs_only_by_policy() -> None:
    observation = page(DBZ_TITLE, bundle=True, evidence=("title term BACKPACK: Backpack",))
    terms = classify_offer_terms(observation)
    assert terms.category == PURCHASE_TERMS_UNKNOWN
    assert terms_price_effect(terms, "TRUE", PURCHASE_TERMS_POLICY) == (
        ("PURCHASE_TERMS_UNKNOWN",), ())
    assert terms_price_effect(terms, "TRUE", BUNDLE_FLAG_POLICY) == ((), ("BUNDLE_OFFER",))
