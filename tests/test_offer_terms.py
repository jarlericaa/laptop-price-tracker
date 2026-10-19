"""Purchase-terms policy (2026-10-19): what a displayed price buys, from the offer's own wording.

Every page and value is synthetic. A keyword ("FREE", "bundle", "backpack", "Office") is never
enough on its own; a statement of the terms is required. Identity matching is unaffected.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction_layouts import offer

from laptop_tracker.offer_terms import (
    BUNDLE_FLAG_POLICY,
    BUNDLE_NOT_CHECKED,
    CONDITIONAL_PURCHASE,
    INCLUDED_ITEMS,
    PAID_PACKAGE,
    PURCHASE_TERMS_POLICY,
    PURCHASE_TERMS_UNKNOWN,
    RESTRICTED_PRICE,
    STANDARD_OFFER,
    classify_offer_terms,
    terms_price_effect,
)
from laptop_tracker.offers import PRODUCT_DESCRIPTION, PriceType

pytestmark = pytest.mark.usefixtures("no_network")

TITLE = "Acer Swift Go 14 SFG14-75-55MB"


def page(title=TITLE, *, bundle=False, evidence=(), price="PHP 50,000", prose=(), specs=()):
    observation = offer(
        "laptop_factory", specs, title=title, bundle=bundle, bundle_evidence=evidence,
        price_type=PriceType.REGULAR,
        scoped=tuple((PRODUCT_DESCRIPTION, "description paragraph 1", text) for text in prose),
    )
    return replace(observation, raw_price_text=price)


@pytest.mark.parametrize(
    ("title", "evidence", "category"),
    [
        (f"{TITLE} + FREE Backpack", ("title term FREE: + FREE",), INCLUDED_ITEMS),
        (f"{TITLE} with MS Office Home & Student 2021", (), INCLUDED_ITEMS),
        (f"{TITLE} with Gaming Mouse", ("title term MOUSE: Mouse",), INCLUDED_ITEMS),
        (f'{TITLE} + 24" Monitor Bundle',
         ("title term BUNDLE: Bundle", "title term MONITOR: Monitor"), PAID_PACKAGE),
        (f"{TITLE} BUNDLE", ("title term BUNDLE: BUNDLE",), PURCHASE_TERMS_UNKNOWN),
        (f"{TITLE} FREE", ("title term FREE: FREE",), PURCHASE_TERMS_UNKNOWN),
        (f"{TITLE} Mouse", ("title term MOUSE: Mouse",), PURCHASE_TERMS_UNKNOWN),
    ],
)
def test_title_statements_not_keywords_decide_the_terms(title, evidence, category) -> None:
    observation = page(title, bundle=bool(evidence), evidence=evidence)
    terms = classify_offer_terms(observation)
    assert terms.category == category
    assert terms.evidence  # the deciding wording is always kept


@pytest.mark.parametrize(
    "title",
    [
        f"{TITLE} 2-in-1 with tablet mode",  # specification wording, not a package
        f"{TITLE} with 16GB RAM and 512GB SSD",
        f"{TITLE} with Dolby Atmos speakers",
    ],
)
def test_specification_wording_is_not_a_package(title) -> None:
    assert classify_offer_terms(page(title)).category == STANDARD_OFFER


def test_bundle_components_need_a_stated_inclusion_or_are_a_package() -> None:
    laptop = "Acer Swift Go 14 SFG14-75-55MB"
    free = page(f"{TITLE} FREE Backpack", bundle=True,
                evidence=("title term FREE: FREE", f"bundle components: {laptop} ; Acer Backpack"))
    unstated = page(bundle=True, evidence=(f"bundle components: {laptop} ; Acer Backpack",))
    monitor = page(bundle=True,
                   evidence=(f"bundle components: {laptop} ; Acer 24in Monitor KA240Y",))
    nameless = page(bundle=True, evidence=("bundle component markup present",))
    assert classify_offer_terms(free).category == INCLUDED_ITEMS
    assert classify_offer_terms(unstated).category == PURCHASE_TERMS_UNKNOWN
    assert classify_offer_terms(monitor).category == PAID_PACKAGE
    assert classify_offer_terms(nameless).category == PURCHASE_TERMS_UNKNOWN


@pytest.mark.parametrize(
    ("kwargs", "category"),
    [
        ({"price": "Trade-in price PHP 40,000"}, RESTRICTED_PRICE),
        ({"price": "PHP 2,500/mo, installment only"}, RESTRICTED_PRICE),
        ({"prose": ("Members price available for loyalty card holders.",)}, RESTRICTED_PRICE),
        ({"prose": ("Promo price valid with purchase of Microsoft 365.",)}, CONDITIONAL_PURCHASE),
        ({"prose": ("Add PHP 1,500 for this price with an extended warranty.",)},
         CONDITIONAL_PURCHASE),
    ],
)
def test_restricted_and_conditional_prices(kwargs, category) -> None:
    assert classify_offer_terms(page(**kwargs)).category == category


def test_payment_options_and_conditional_freebies_do_not_restrict_the_laptop_price() -> None:
    options = page(prose=("0% installment available via Home Credit.",))
    assert classify_offer_terms(options).category == STANDARD_OFFER
    freebie = page(f"{TITLE} + FREE bag", bundle=True, evidence=("title term FREE: + FREE",),
                   prose=("Free sleeve with purchase of Microsoft 365.",))
    terms = classify_offer_terms(freebie)
    assert terms.category == INCLUDED_ITEMS
    assert any("conditional extra" in item for item in terms.evidence)
    assert all("sleeve" not in item for item in terms.included_items)


def test_unconditional_inclusions_in_the_description_are_recognized() -> None:
    included = page(prose=("Comes with 2 years warranty and Microsoft Office Home 2021.",))
    assert classify_offer_terms(included).category == INCLUDED_ITEMS
    office_word = page(prose=("Office-ready performance for work and play.",))
    assert classify_offer_terms(office_word).category == STANDARD_OFFER


def test_unchecked_bundle_status_stays_unknown() -> None:
    observation = page(bundle=None)
    assert classify_offer_terms(observation).category == BUNDLE_NOT_CHECKED
    assert terms_price_effect(classify_offer_terms(observation), "", PURCHASE_TERMS_POLICY) == (
        ("BUNDLE_STATUS_UNKNOWN",), ()
    )


@pytest.mark.parametrize(
    ("category", "flag", "purchase_terms", "bundle_flag"),
    [
        (STANDARD_OFFER, "FALSE", ((), ()), ((), ())),
        (INCLUDED_ITEMS, "TRUE", ((), ("INCLUDED_ITEMS_OFFER",)), ((), ("BUNDLE_OFFER",))),
        (PAID_PACKAGE, "TRUE", (("PAID_PACKAGE_PRICE",), ()), ((), ("BUNDLE_OFFER",))),
        (PURCHASE_TERMS_UNKNOWN, "TRUE", (("PURCHASE_TERMS_UNKNOWN",), ()),
         ((), ("BUNDLE_OFFER",))),
        (RESTRICTED_PRICE, "FALSE", (("RESTRICTED_PRICE",), ()), ((), ())),
    ],
)
def test_policies_are_configurable_and_separate(category, flag, purchase_terms, bundle_flag):
    from laptop_tracker.offer_terms import OfferTerms

    terms = OfferTerms(category)
    assert terms_price_effect(terms, flag, PURCHASE_TERMS_POLICY) == purchase_terms
    assert terms_price_effect(terms, flag, BUNDLE_FLAG_POLICY) == bundle_flag
    with pytest.raises(ValueError):
        terms_price_effect(terms, flag, "no-such-policy")
