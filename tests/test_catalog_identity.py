"""Catalog identity, product-name normalization and discovery relevance (2026-10-19, second
review): reviewed product names, broad family names, search planning per identity kind, the
catalog reconciliation, and the extraction repairs found in the top-100 run."""

from __future__ import annotations

from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction_layouts import offer as layout_offer
from test_extraction_layouts import primary
from test_product_discovery import hit, record

from laptop_tracker.adapters import pcc, pcx
from laptop_tracker.catalog_reconciliation import (
    CONFLICTING_SOURCE_SPECS,
    DUPLICATE_OF_EXISTING_TARGET,
    INSUFFICIENT_EVIDENCE,
    MULTIPLE_PRODUCTS,
    NO_CHANGE_MODEL_CODE,
    NO_CHANGE_PRODUCT_NAME,
    SUPPORTED_CORRECTION,
    apply_corrections,
    is_seller_descriptor,
    reconcile,
)
from laptop_tracker.conventions import normalize_product_name
from laptop_tracker.matching import CATALOG_IDENTITY_UNRESOLVED, configuration_outcome
from laptop_tracker.product_discovery import (
    ACCESSORY,
    FAMILY_NAME_LEAD,
    FAMILY_SPEC_CANDIDATE,
    NAME_MATCH,
    SearchTarget,
    rank_hit,
)
from laptop_tracker.product_names import (
    FAMILY_NAME,
    MODEL_CODE,
    PRODUCT_NAME,
    ProductNameRegistry,
    RegistryError,
    default_registry,
)
from laptop_tracker.retailer_search import (
    TIER_NAME_SPECS,
    TIER_PRODUCT_NAME,
    search_queries,
)

pytestmark = pytest.mark.usefixtures("no_network")
REGISTRY = default_registry()

# Product-name normalization: presentation only


@pytest.mark.parametrize(
    ("text", "brand", "color", "expected"),
    [
        ("Apple Macbook Neo (Citrus)", "Apple", "Citrus", "Macbook Neo"),
        ("APPLE  MacBook   Neo", "Apple", "", "MacBook Neo"),
        ("MacBook Neo", "Apple", "", "MacBook Neo"),
        # A code is returned unchanged: codes keep their own rules.
        ("Apple FA607NUQ-RL011W (Black)", "Apple", "Black", "Apple FA607NUQ-RL011W (Black)"),
    ],
)
def test_presentation_differences_are_removed(text, brand, color, expected) -> None:
    result = normalize_product_name(text, brand=brand, color=color)
    assert result.value == expected and result.original == text


@pytest.mark.parametrize(
    "wording",
    [
        "Apple MacBook Neo 13-inch",  # the size is kept, so it is not the bare name
        "MacBook Neo Pro",  # a suffix is identity: never stripped
        "MacBook",  # a prefix is never enough
        "Apple MacBook Air (Citrus)",  # another product name
        "Apple MacBook Neo (Purple)",  # an unregistered colour is not removed
        "MacBookNeo",  # words are never joined or split
    ],
)
def test_other_wordings_are_not_the_registered_name(wording) -> None:
    normalized = REGISTRY.normalize_model_name(wording)
    assert normalized is None or normalized.value != "MacBook Neo"


def test_the_registry_names_kinds_and_defaults_unreviewed_names_to_family() -> None:
    assert REGISTRY.kind("Apple", "MacBook Neo") == PRODUCT_NAME
    assert REGISTRY.kind("Lenovo", "IdeaPad") == FAMILY_NAME
    assert REGISTRY.kind("Acer", "AL14-45P-R10U") == MODEL_CODE
    assert REGISTRY.kind("Dell", "Inspiron") == FAMILY_NAME  # unreviewed: never one product
    assert REGISTRY.normalize_model_name("Apple Macbook Neo (Citrus)").value == "MacBook Neo"


def test_a_registry_row_needs_a_known_kind_and_evidence(tmp_path: Path) -> None:
    path = tmp_path / "registry.csv"
    path.write_text("brand,name,identity_kind,colours,evidence,reviewed_on\n"
                    "Apple,MacBook Neo,ALIAS,,x,2026-10-19\n")
    with pytest.raises(RegistryError):
        ProductNameRegistry.read(path)
    path.write_text("brand,name,identity_kind,colours,evidence,reviewed_on\n"
                    "Apple,MacBook Neo,PRODUCT_NAME,,,2026-10-19\n")
    with pytest.raises(RegistryError):
        ProductNameRegistry.read(path)


# Search planning per identity kind


CORE = {"screen_size_in": "15.6", "cpu_series": "Ryzen 5", "ram_capacity_gb": "8",
        "storage_capacity_gb": "512"}


def queries(model: str, kind: str, series: str = "") -> list:
    return search_queries("TGT-000007", "pcx", model=model, part_number="", brand="Lenovo",
                          series=series, core={"model": model, **CORE}, kind=kind)


def test_a_family_name_is_searched_by_name_and_specifications_only() -> None:
    planned = queries("IdeaPad", FAMILY_NAME)
    assert {q.tier for q in planned} <= {TIER_PRODUCT_NAME, TIER_NAME_SPECS}
    assert planned and all("IdeaPad" in q.text for q in planned)
    # No code is ever manufactured from the name, and no query is sent twice.
    texts = [" ".join(q.text.casefold().split()) for q in planned]
    assert len(texts) == len(set(texts))


def test_a_code_target_keeps_its_code_tiers_first() -> None:
    planned = queries("82XM00ABPH", MODEL_CODE)
    assert planned[0].text.startswith("82XM00ABPH")
    assert TIER_PRODUCT_NAME not in {q.tier for q in planned}


# Discovery relevance


NEO = SearchTarget("TGT-000002", "2", "Apple", "", "MacBook Neo", "",
                   {"model": "MacBook Neo", "screen_size_in": "13", "cpu_series": "Apple A18 Pro",
                    "ram_capacity_gb": "8", "storage_capacity_gb": "512"},
                   identity_kind=PRODUCT_NAME)
IDEAPAD = SearchTarget("TGT-000007", "7", "Lenovo", "", "IdeaPad", "",
                       {"model": "IdeaPad", **CORE}, identity_kind=FAMILY_NAME)


def ranked(target, title: str):
    found = hit(title)
    return rank_hit(target, found, [record(target, found)])


def test_product_names_family_leads_and_accessories_are_told_apart() -> None:
    assert ranked(NEO, 'Apple MacBook Neo 13" 8GB+512GB A18 Pro (Blush)').category == NAME_MATCH
    assert ranked(NEO, "Laptop Sleeve for MacBook Neo 13-inch").category == ACCESSORY
    # A family name never identifies one product. Since 2026-10-20 a listing stating the brand
    # and family is collected for a listed-spec comparison; without the brand it stays a lead.
    assert ranked(IDEAPAD, "Lenovo IdeaPad Slim 3 15ARP10 Ryzen 5 8GB 512GB").category == (
        FAMILY_SPEC_CANDIDATE)
    assert ranked(IDEAPAD, "IdeaPad Slim 3 15ARP10 Ryzen 5 8GB 512GB").category == (
        FAMILY_NAME_LEAD)


def test_a_model_difference_against_a_family_target_is_a_catalog_identity_issue() -> None:
    target = {"brand": "Lenovo", "model": "IdeaPad", **CORE}
    offered = {"model": "15ARP10", **CORE}
    status, detail = configuration_outcome("UNMATCHED", "", ("NO_CORE_MATCH",), offered,
                                           "TGT-000007", target)
    assert status == CATALOG_IDENTITY_UNRESOLVED
    assert "broad family name" in detail and "15ARP10" in detail
    coded = {"brand": "ASUS", "model": "X1504VA-BQ4301WSM", **CORE}
    status, _ = configuration_outcome("UNMATCHED", "", ("NO_CORE_MATCH",), offered,
                                      "TGT-000007", coded)
    assert status == "DIFFERENT_MODEL"


# Extraction repairs


def test_an_lpddr5x_memory_type_is_not_read_as_a_capacity() -> None:
    page = layout_offer("datablitz", (("Memory", "16GB LPDDR5X"), ("Storage", "512GB SSD")))
    assert primary(page)["ram_capacity_gb"] == ("EXTRACTED", "16")


def test_percent_escapes_of_one_handle_compare_equal() -> None:
    lower = "https://pcx.com.ph/products/asus-vivobook%e2%84%a2-14"
    upper = "https://pcx.com.ph/products/asus-vivobook%E2%84%A2-14"
    assert pcx._node_paths({"url": lower}) == pcx._node_paths({"url": upper})
    assert pcc._path(lower) == pcc._path(upper)


# Catalog reconciliation


def catalog_row(target_id: str, model: str, brand: str = "Acer", mmc: str = "", **core):
    values = {"screen_size_in": "14", "cpu_series": "Ryzen 3", "ram_capacity_gb": "8",
              "storage_capacity_gb": "512"} | core
    return {"target_id": target_id, "brand": brand, "model": model, "series": "",
            "manufacturer_model_code": mmc, "gpu_model": "", "notes": "", **values}


def source(target_id: str, model_id: str, item_name: str, status: str = "CONTRIBUTING"):
    return {"target_id": target_id, "model_id": model_id, "shop_id": "1", "item_id": model_id,
            "item_name": item_name, "shopee_model_name": "", "contribution_status": status}


def test_the_reconciliation_corrects_only_what_the_sources_establish() -> None:
    catalog = [
        catalog_row("TGT-000182", "Aspire Lite"),
        catalog_row("TGT-000007", "IdeaPad", brand="Lenovo"),
        catalog_row("TGT-000026", "Aspire Lite"),
        catalog_row("TGT-000013", "HP Laptop 14", brand="HP", cpu_series="Core i5"),
        catalog_row("TGT-000233", "14-ep0445TU", brand="HP", cpu_series="Core i5"),
        catalog_row("TGT-000002", "MacBook Neo", brand="Apple"),
        catalog_row("TGT-000060", "ThinkPad", brand="Lenovo"),
    ]
    selected = [{"target_id": row["target_id"], "rank": str(n), "model": row["model"],
                 "series": ""} for n, row in enumerate(catalog, start=1)]
    contributions = [
        source("TGT-000182", "1", 'Acer Aspire Lite AL14-45P-R10U 14" Ryzen 3 8GB + 512GB'),
        source("TGT-000182", "2", "Acer Aspire Lite AL14-45P-R10U Ryzen 3 5400U 8GB 512GB SSD"),
        source("TGT-000182", "3", "Acer Aspire Lite AL14-42P-R8ST", status="EXCLUDED"),
        source("TGT-000007", "4", "Lenovo IdeaPad Slim 3 Ryzen 5 8GB 512GB"),
        source("TGT-000026", "5", "Acer Aspire Lite AL15-33P-332M / AL15-33P-34FY"),
        source("TGT-000013", "6", "HP Laptop 14-ep0445TU Core i5 8GB 512GB"),
        source("TGT-000060", "7", "Lenovo ThinkPad X280-i5 8GB 512GB"),
    ]
    # A retailer offer with the same specifications is a lead, never evidence.
    offers = [{"retailer_id": "pcx", "offer_model": "82XM00ABPH", "expected_target_id":
               "TGT-000007", "offer_url": "https://pcx.example.test/p",
               "match_status": "UNMATCHED"}]
    result = reconcile(catalog, selected, contributions, REGISTRY, offers)
    by_id = {row["target_id"]: row for row in result.rows}
    assert by_id["TGT-000182"]["status"] == SUPPORTED_CORRECTION
    assert by_id["TGT-000182"]["proposed_model"] == "AL14-45P-R10U"
    assert "AL14-42P-R8ST" in by_id["TGT-000182"]["flags"]  # excluded mapping kept for review
    assert by_id["TGT-000007"]["status"] == INSUFFICIENT_EVIDENCE
    assert by_id["TGT-000007"]["proposed_model"] == ""
    assert "82XM00ABPH" in by_id["TGT-000007"]["retailer_leads_not_evidence"]
    assert by_id["TGT-000026"]["status"] == MULTIPLE_PRODUCTS
    assert by_id["TGT-000013"]["status"] == DUPLICATE_OF_EXISTING_TARGET
    assert by_id["TGT-000233"]["status"] == NO_CHANGE_MODEL_CODE
    assert by_id["TGT-000002"]["status"] == NO_CHANGE_PRODUCT_NAME
    # A refurbished seller descriptor is not a manufacturer code.
    assert is_seller_descriptor("X280-i5") and not is_seller_descriptor("AL14-45P-R10U")
    assert by_id["TGT-000060"]["status"] == INSUFFICIENT_EVIDENCE
    assert set(result.corrections) == {"TGT-000182"}

    corrected, listed = apply_corrections(catalog, selected, result.corrections)
    fixed = next(row for row in corrected if row["target_id"] == "TGT-000182")
    assert (fixed["model"], fixed["manufacturer_model_code"], fixed["series"]) == (
        "AL14-45P-R10U", "AL14-45P-R10U", "Aspire Lite")
    assert "model 'Aspire Lite' -> 'AL14-45P-R10U'" in fixed["notes"]
    assert [row["target_id"] for row in corrected] == [row["target_id"] for row in catalog]
    assert next(r for r in listed if r["target_id"] == "TGT-000182")["model"] == "AL14-45P-R10U"
    untouched = [row for row in corrected if row["target_id"] != "TGT-000182"]
    assert untouched == [row for row in catalog if row["target_id"] != "TGT-000182"]


def test_one_code_supported_for_two_targets_is_never_applied() -> None:
    catalog = [catalog_row("TGT-000014", "HP Laptop 15", brand="HP", cpu_series="Core i5"),
               catalog_row("TGT-000099", "HP Laptop 15", brand="HP", cpu_series="Core i5")]
    selected = [{"target_id": r["target_id"], "rank": "1", "model": r["model"], "series": ""}
                for r in catalog]
    contributions = [source("TGT-000014", "1", "HP 15-fd1541TU Core i5 8GB 512GB"),
                     source("TGT-000099", "2", "HP 15-fd1541TU Core i5 8GB 512GB")]
    result = reconcile(catalog, selected, contributions, REGISTRY)
    assert result.corrections == {}
    assert {row["status"] for row in result.rows} == {DUPLICATE_OF_EXISTING_TARGET}


def test_a_source_contradicting_the_catalog_blocks_the_correction() -> None:
    catalog = [catalog_row("TGT-000014", "HP Laptop 15", brand="HP")]  # catalog: Ryzen 3
    selected = [{"target_id": "TGT-000014", "rank": "1", "model": "HP Laptop 15", "series": ""}]
    contributions = [source("TGT-000014", "1", "HP 15-fd1541TU Core i5 8GB 512GB")]
    result = reconcile(catalog, selected, contributions, REGISTRY)
    assert result.rows[0]["status"] == CONFLICTING_SOURCE_SPECS
    assert "cpu_series" in result.rows[0]["conflicts"] and result.corrections == {}
