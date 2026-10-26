"""Option-axis interpretation of Shopify variants (2026-10-22): PC Central MacBook Neo.

The saved Neo page has ONE option, named after the product, whose values are a colour and a
bare capacity ("McbookNeo SILVER 512GB"); no value says SSD or RAM. The capacity is storage only
because the page's own statements say so, and RAM is shared only when one page-level statement
covers every variant. Page: ``scraping_fixtures/pcc_neo/pcc_neo_variants.html`` (see
``pcc_neo_pages``).
"""

from __future__ import annotations

import pytest
from pcc_neo_pages import HOST, PRODUCT_URL, saved_page

from laptop_tracker.product_discovery import (
    SearchTarget,
    inspect_product_page,
    variant_decision,
)
from laptop_tracker.retailer_search import SEARCH_METHODS

PCC = SEARCH_METHODS["pcc"]
SILVER_256, SILVER_512, CITRUS_512 = "48865301463207", "48865301561511", "48865301594279"


def neo_target(storage: str, ram: str = "8") -> SearchTarget:
    return SearchTarget(
        "TGT-000002", "1", "Apple", "", "MacBook Neo", "",
        {"model": "MacBook Neo", "screen_size_in": "13", "cpu_series": "Apple A18 Pro",
         "ram_capacity_gb": ram, "storage_capacity_gb": storage},
        identity_kind="PRODUCT_NAME",
    )


def decisions(text: str, target: SearchTarget) -> dict[str, tuple[str, str]]:
    inspection = inspect_product_page(PCC, PRODUCT_URL, text)
    assert inspection.outcome == "MULTI_VARIANT"
    return {v.variant_id: variant_decision(v, target) for v in inspection.variants}


def test_option_axis_reads_a_bare_capacity_as_storage_when_the_page_says_so() -> None:
    result = decisions(saved_page(), neo_target("512"))
    assert [d for d, _ in result.values()].count("COLLECT") == 2
    assert result[SILVER_512][0] == result[CITRUS_512][0] == "COLLECT"
    assert result[SILVER_256][0] == "ALTERNATIVE"  # 256GB and 512GB never merge
    assert "storage" in result[SILVER_512][1].casefold()


def test_the_other_capacity_selects_the_other_colours() -> None:
    result = decisions(saved_page(), neo_target("256"))
    assert sorted(d for d, _ in result.values()) == ["ALTERNATIVE"] * 2 + ["COLLECT"] * 4


def test_shared_ram_statement_that_disagrees_with_the_target_is_an_alternative() -> None:
    # One uniform page-level "Memory: 8GB" statement covers every variant: a 16GB target is
    # a different configuration of the same product.
    result = decisions(saved_page(), neo_target("512", ram="16"))
    assert {d for d, _ in result.values()} == {"ALTERNATIVE"}
    assert "RAM" in result[SILVER_512][1]


def test_capacity_with_no_page_statement_of_its_role_stays_in_review() -> None:
    text = saved_page()
    for statement in ("Storage:", "Storage: "):
        text = text.replace(f"<span>{statement}</span>", "<span>Notes:</span>")
    text = text.replace("Storage:  256GB &amp;amp; 512gbSSD. ", "").replace(
        "Storage:  256GB &amp; 512gbSSD. ", "")
    result = decisions(text, neo_target("512"))
    assert {d for d, _ in result.values()} == {"REVIEW"}
    assert "role" in result[SILVER_512][1].casefold()


def test_a_capacity_the_storage_statement_does_not_list_is_not_interpreted() -> None:
    text = saved_page().replace("256GB &amp; 512gbSSD", "256GB SSD").replace(
        "256GB &amp;amp; 512gbSSD", "256GB SSD")
    result = decisions(text, neo_target("512"))
    assert result[SILVER_512][0] == "REVIEW"  # 512GB is not among the stated storage options


def test_named_storage_axis_needs_no_page_statement() -> None:
    text = saved_page()
    for rename in ('<legend class="form__label">MACBOOK NEO</legend>',):
        text = text.replace(rename, '<legend class="form__label">Storage</legend>')
    text = text.replace('name="MACBOOK NEO"', 'name="Storage"')
    for title in ("McbookNeo Silver 256GB", "McbookNeo SILVER 512GB"):
        assert title in text
    result = decisions(text, neo_target("512"))
    assert result[SILVER_512][0] == "COLLECT"


def test_product_url_host_is_the_pc_central_store() -> None:
    assert PRODUCT_URL.startswith(f"https://{HOST}/products/")


@pytest.mark.parametrize("ram_statement", ["Memory: 8GB or 16GB Unified Memory."])
def test_ram_listed_as_alternatives_is_not_one_shared_value(ram_statement: str) -> None:
    text = saved_page().replace("<span> 8GB Unified Memory.</span>",
                                f"<span> {ram_statement.split(': ', 1)[1]}</span>")
    inspection = inspect_product_page(PCC, PRODUCT_URL, text)
    readings = {v.variant_id: v.reading for v in inspection.variants}
    assert readings[SILVER_512].ram == ""
    assert readings[SILVER_512].ram_scope == "NOT_ESTABLISHED"
    assert readings[SILVER_512].storage == "512"
