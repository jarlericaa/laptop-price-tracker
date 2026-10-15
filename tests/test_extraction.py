"""Phase 8 extraction tests: synthetic adapter fixtures and synthetic observations only."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, FakeTransport, no_network  # noqa: F401

from laptop_tracker.adapters.datablitz import HOST as DBZ_HOST
from laptop_tracker.adapters.datablitz import DataBlitzAdapter
from laptop_tracker.adapters.laptop_factory import HOST as LF_HOST
from laptop_tracker.adapters.laptop_factory import LaptopFactoryAdapter
from laptop_tracker.adapters.oct import HOST as OCT_HOST
from laptop_tracker.adapters.oct import OCTAdapter
from laptop_tracker.adapters.pcc import HOST as PCC_HOST
from laptop_tracker.adapters.pcc import PCCAdapter
from laptop_tracker.adapters.pcx import HOST as PCX_HOST
from laptop_tracker.adapters.pcx import PCXAdapter
from laptop_tracker.extraction import ExtractionStatus, extract_offer_attributes
from laptop_tracker.matching import EvidenceStatus, ExactMatcher, MatchStatus
from laptop_tracker.offers import (
    Availability,
    OfferDetails,
    OfferObservation,
    RetrievalMetadata,
)
from laptop_tracker.scraping import (
    FetchConfig,
    Fetcher,
    ScrapeRequest,
    TransportResponse,
    scrape,
)

pytestmark = pytest.mark.usefixtures("no_network")

FIXTURES = Path(__file__).resolve().parent / "scraping_fixtures"
HTML = {"content-type": "text/html; charset=utf-8"}

FIXTURE_PAGES = {
    "dbz_sale": (
        DataBlitzAdapter,
        "datablitz/dbz_product_sale.html",
        f"https://{DBZ_HOST}/products/example-modern-14-em14-001ph-laptop-urban-silver",
    ),
    "dbz_regular": (
        DataBlitzAdapter,
        "datablitz/dbz_product_regular_no_jsonld.html",
        f"https://{DBZ_HOST}/products/example-notebook-15-en15-01ph-laptop",
    ),
    "dbz_bundle": (
        DataBlitzAdapter,
        "datablitz/dbz_bundle.html",
        f"https://{DBZ_HOST}/products/example-gaming-16-eg16-02-laptop-free-backpack",
    ),
    "lf_sale": (
        LaptopFactoryAdapter,
        "laptop_factory/lf_product_sale.html",
        f"https://{LF_HOST}/product/acer-aspire-ai-14-a14-11m-x3bp/",
    ),
    "lf_regular": (
        LaptopFactoryAdapter,
        "laptop_factory/lf_product_regular_no_jsonld.html",
        f"https://{LF_HOST}/product/example-notebook-15-regular/",
    ),
    "lf_bundle": (
        LaptopFactoryAdapter,
        "laptop_factory/lf_bundle.html",
        f"https://{LF_HOST}/product/example-megabook-k15-free-ergochair/",
    ),
    "oct_regular": (
        OCTAdapter,
        "oct/oct_product_regular.html",
        f"https://{OCT_HOST}/products/"
        "example-book-14-ex14-0001ph-core-i5-13420h-8gb-512gb-ssd-14-win-11-gry",
    ),
    "oct_sale": (
        OCTAdapter,
        "oct/oct_product_sale.html",
        f"https://{OCT_HOST}/products/"
        "example-gamer-16-eg16-0002ph-ryzen-7-7735hs-16gb-1tb-ssd-rtx-4050-6gb-16-win-11",
    ),
    "oct_variants": (
        OCTAdapter,
        "oct/oct_product_variants.html",
        f"https://{OCT_HOST}/products/"
        "example-slim-15-es15-0003ph-core-5-120u-512gb-ssd-15-6-fhd-win-11?variant=50000000000301",
    ),
    "pcc_variants": (
        PCCAdapter,
        "pcc/pcc_product_variants_sale.html",
        f"https://{PCC_HOST}/products/"
        "example-15-ex0001au-ryzen-7-8gb-256gb-ssd-win-11-home-pc-central?variant=50000000000001",
    ),
    "pcc_single": (
        PCCAdapter,
        "pcc/pcc_product_single_regular.html",
        f"https://{PCC_HOST}/products/"
        "example-nitro-15-en15-0002-core-5-16gb-512gb-rtx-5050-pc-central",
    ),
    "pcx_regular": (
        PCXAdapter,
        "pcx/pcx_product_regular.html",
        f"https://{PCX_HOST}/products/"
        "example-14-ex0001tu-x0x00pa-intel-core-5-laptop-natural-silver",
    ),
    "pcx_sale": (
        PCXAdapter,
        "pcx/pcx_product_sale_sold_out.html",
        f"https://{PCX_HOST}/products/"
        "example-gaming-15-eg15-0002ph-geforce-rtx-5050-laptop-black",
    ),
    "pcx_regular_no_jsonld": (
        PCXAdapter,
        "pcx/pcx_product_regular_no_jsonld.html",
        f"https://{PCX_HOST}/products/example-notebook-15-en15-0003ph-amd-ryzen-5-laptop-grey",
    ),
}


def fixture_offer(name: str) -> OfferObservation:
    """Run the real adapter over a synthetic fixture through the offline fake transport."""
    adapter_class, path, url = FIXTURE_PAGES[name]
    clock = FakeClock()
    body = (FIXTURES / path).read_bytes()
    transport = FakeTransport(clock, TransportResponse(200, url, body, HTML))
    adapter = adapter_class()
    [result] = scrape(adapter, [ScrapeRequest(adapter.retailer_id, url)], Fetcher(
        transport, FetchConfig(), clock
    ))
    assert result.ok, result.failures
    [observation] = result.observations
    return observation


def synthetic_offer(
    retailer_id: str,
    specs: tuple[tuple[str, str], ...],
    *,
    raw_title: str = "Synthetic laptop",
    configuration: str | None = None,
    brand: str = "",
) -> OfferObservation:
    joined = " | ".join(f"{label}: {value}" if value else label for label, value in specs)
    return OfferObservation(
        retailer_id=retailer_id,
        offer_url="https://shop.example.test/product",
        retrieved_at=datetime(2026, 10, 6, tzinfo=UTC),
        raw_title=raw_title,
        raw_configuration_text=joined if configuration is None else configuration,
        price=Decimal("1000"),
        currency="PHP",
        raw_price_text="PHP 1,000",
        availability=Availability.IN_STOCK,
        availability_evidence="Synthetic fixture",
        offer_index=0,
        source=RetrievalMetadata(
            requested_url="https://shop.example.test/product",
            final_url="https://shop.example.test/product",
            http_status=200,
            attempts=1,
            content_sha256="0" * 64,
            content_type="text/html",
            adapter_version="fixture-1",
        ),
        details=OfferDetails(raw_specifications=specs, brand=brand),
    )


def statuses(name_or_offer: str | OfferObservation) -> dict[str, tuple[str, str]]:
    """Primary evidence per field; SUPPORTING items are checked separately."""
    offer = fixture_offer(name_or_offer) if isinstance(name_or_offer, str) else name_or_offer
    return {
        item.field: (item.status.value, item.value)
        for item in extract_offer_attributes(offer).evidence
        if item.status is not ExtractionStatus.SUPPORTING
    }


E, M, A, U, N = "EXTRACTED", "MISSING", "AMBIGUOUS", "UNPARSEABLE", "NOT_EXTRACTED"


# Per-retailer extractors over each adapter's synthetic fixtures


def test_laptop_factory_extracts_a_complete_labeled_core() -> None:
    extracted = extract_offer_attributes(fixture_offer("lf_sale"))
    assert extracted.extractor == "laptop_factory"
    assert extracted.values == {
        "model": "A14-11M-X3BP",
        "screen_size_in": "14",
        "cpu_series": "Snapdragon X",
        "ram_capacity_gb": "16",
        "storage_capacity_gb": "512",
        "resolution": "1920x1200",
        "display_type": "IPS",
    }
    assert extracted.reason_codes == ()
    cpu = next(item for item in extracted.evidence if item.field == "cpu_series")
    assert cpu.source_label == "Processor"
    assert "X1-26-100" in cpu.source_text


def test_laptop_factory_unrecognized_cpu_and_missing_rows_stay_missing() -> None:
    assert statuses("lf_regular") == {
        "model": (E, "EN15-01"),
        "screen_size_in": (E, "15.6"),
        "cpu_series": (U, ""),
        "ram_capacity_gb": (E, "8"),
        "storage_capacity_gb": (E, "256"),
    }
    bundle = statuses("lf_bundle")
    assert bundle["cpu_series"] == (M, "")
    assert bundle["storage_capacity_gb"] == (M, "")


def title_model_source(name: str) -> str:
    extracted = extract_offer_attributes(fixture_offer(name))
    return next(item.source_label for item in extracted.evidence if item.field == "model")


def test_datablitz_extracts_labeled_rows_and_a_corroborated_title_model() -> None:
    # No model row: the title's one full code is accepted because the other four fields come
    # from labeled rows (balanced policy, 2026-10-15).
    assert title_model_source("dbz_sale").startswith("title [TITLE_MODEL_CORROBORATED;")
    assert statuses("dbz_sale") == {
        "model": (E, "EM14-001PH"),
        "screen_size_in": (E, "14"),
        "cpu_series": (E, "Core 5"),
        "ram_capacity_gb": (E, "16"),
        "storage_capacity_gb": (E, "512"),
        "brand": (E, "ExampleBrand"),
        "resolution": (E, "1920x1200"),
        "display_type": (E, "IPS-Level"),
    }
    regular = statuses("dbz_regular")
    assert regular["cpu_series"] == (U, "")
    assert regular["ram_capacity_gb"] == (M, "")


def test_datablitz_title_specs_are_never_used() -> None:
    # The bundle fixture's title states RAM and storage, but it has no labeled rows.
    extracted = extract_offer_attributes(fixture_offer("dbz_bundle"))
    assert "16GB" in fixture_offer("dbz_bundle").raw_title
    assert extracted.values == {"brand": "ExampleBrand"}
    assert set(extracted.reason_codes) == {
        "MISSING_MODEL",
        "MISSING_SCREEN_SIZE_IN",
        "MISSING_CPU_SERIES",
        "MISSING_RAM_CAPACITY_GB",
        "MISSING_STORAGE_CAPACITY_GB",
    }


def test_octagon_single_variant_uses_tech_specification_rows() -> None:
    assert title_model_source("oct_regular").startswith("title [TITLE_MODEL_CORROBORATED;")
    assert statuses("oct_regular") == {
        "model": (E, "EX14-0001PH"),
        "screen_size_in": (E, "14"),
        "cpu_series": (E, "Core i5"),
        "ram_capacity_gb": (E, "8"),
        "storage_capacity_gb": (E, "512"),
        "resolution": (E, "1920x1200"),
        "display_type": (E, "IPS"),
    }


def test_octagon_unlabeled_bullets_are_not_evidence() -> None:
    assert {status for status, _ in statuses("oct_sale").values()} == {M}


def test_octagon_multi_variant_shared_description_is_not_trusted() -> None:
    extracted = extract_offer_attributes(fixture_offer("oct_variants"))
    assert extracted.values == {}
    assert statuses("oct_variants") == {
        "model": (M, ""),
        "screen_size_in": (N, ""),
        "cpu_series": (N, ""),
        "ram_capacity_gb": (A, "8; 16"),
        "storage_capacity_gb": (M, ""),
    }
    assert extracted.reason_codes[0] == "MULTI_VARIANT_SHARED_DESCRIPTION"


def test_pc_central_single_variant_extracts_model_and_secondary_details() -> None:
    extracted = extract_offer_attributes(fixture_offer("pcc_single"))
    assert extracted.values == {
        "model": "EN15-0002",
        "screen_size_in": "15.6",
        "cpu_series": "Core 5",
        "ram_capacity_gb": "16",
        "storage_capacity_gb": "512",
        "brand": "ExampleBrand",
        "display_type": "IPS",
        "refresh_rate_hz": "165",
    }


def test_pc_central_selected_variant_does_not_use_the_shared_description() -> None:
    extracted = extract_offer_attributes(fixture_offer("pcc_variants"))
    assert extracted.values == {"brand": "ExampleBrand"}
    assert "MULTI_VARIANT_SHARED_DESCRIPTION" in extracted.reason_codes
    assert statuses("pcc_variants")["ram_capacity_gb"] == (N, "")


def test_pc_express_layout_and_total_capacity_must_agree() -> None:
    assert title_model_source("pcx_regular").startswith("title [TITLE_MODEL_CORROBORATED;")
    assert statuses("pcx_regular") == {
        "model": (E, "14-EX0001TU"),
        "screen_size_in": (E, "14"),
        "cpu_series": (E, "Core 5"),
        "ram_capacity_gb": (E, "16"),
        "storage_capacity_gb": (E, "512"),
        "brand": (E, "ExampleBrand"),
    }
    assert statuses("pcx_sale")["cpu_series"] == (E, "Core i5")
    no_jsonld = statuses("pcx_regular_no_jsonld")
    assert no_jsonld["cpu_series"] == (E, "Ryzen 5")
    assert no_jsonld["ram_capacity_gb"] == (E, "8")


# Parsing rules on synthetic labeled rows


@pytest.mark.parametrize(
    ("label", "value", "field", "expected"),
    [
        ("Storage", "1TB NVMe SSD", "storage_capacity_gb", (E, "1000")),
        ("Storage", "2 x 512GB SSD", "storage_capacity_gb", (E, "1024")),
        # Installed drives are summed (1 TB = 1000 GB); media stay separate evidence.
        ("Storage", "512GB SSD + 1TB HDD", "storage_capacity_gb", (E, "1512")),
        ("Storage", "SSD", "storage_capacity_gb", (U, "")),
        ("RAM", "16GB (2x 16GB)", "ram_capacity_gb", (A, "16; 32")),
        ("RAM", "0.5GB", "ram_capacity_gb", (U, "")),
        ("RAM", "1TB", "ram_capacity_gb", (U, "")),
        ("RAM", "8GB, expandable to 32GB", "ram_capacity_gb", (A, "8; 32")),
        ("Display Size", "15.6-inch FHD", "screen_size_in", (E, "15.6")),
        ("Display Size", "14.0'' 2-in-1 touch", "screen_size_in", (E, "14")),
        ("Display Size", "FHD touch", "screen_size_in", (U, "")),
        ("Display Size", '14" or 16"', "screen_size_in", (A, "14 | 16")),
        ("Processor", "Intel® Core™ Ultra 7 155H", "cpu_series", (E, "Core Ultra 7")),
        ("Processor", "AMD Ryzen™ AI 9 HX 370", "cpu_series", (E, "Ryzen AI 9")),
        ("Processor", "Snapdragon® X Elite X1E-78-100", "cpu_series", (E, "Snapdragon X Elite")),
        ("Processor", "Apple M3 Pro chip", "cpu_series", (E, "Apple M3 Pro")),
        ("Processor", "Intel Celeron N4500", "cpu_series", (E, "Celeron N4500")),
        ("Processor", "Intel Celeron processor", "cpu_series", (U, "")),
        ("Processor", "Core i5 or Core i7", "cpu_series", (A, "Core i5 | Core i7")),
        ("Model Name", "ExampleBook™ 14", "model", (E, "ExampleBook 14")),
    ],
)
def test_labeled_value_rules(label: str, value: str, field: str, expected: tuple[str, str]) -> None:
    offer = synthetic_offer("laptop_factory", ((label, value),))
    assert statuses(offer)[field] == expected


def test_conflicting_rows_for_one_field_are_ambiguous() -> None:
    offer = synthetic_offer("laptop_factory", (("RAM", "8GB"), ("RAM", "16GB")))
    assert statuses(offer)["ram_capacity_gb"] == (A, "8; 16")


# Components: an explicit "+" list of installed parts is summed (balanced policy, 2026-10-15);
# any other repetition of amounts inside one row stays in review, never deduplicated.


@pytest.mark.parametrize(
    ("label", "value", "field", "expected"),
    [
        ("Storage", "512GB SSD + 512GB HDD", "storage_capacity_gb", (E, "1024")),
        ("Storage", "512GB SSD, 512GB SSD", "storage_capacity_gb", (A, "512; 512")),
        ("Storage", "512GB SSD + 512GB external HDD", "storage_capacity_gb", (A, "512; 512")),
        ("Storage", "512GB + 512GB", "storage_capacity_gb", (A, "512; 512")),
        ("RAM", "8GB + 8GB", "ram_capacity_gb", (E, "16")),
        ("RAM", "8GB DDR5 8GB DDR5", "ram_capacity_gb", (A, "8; 8")),
        # A layout plus a separate amount is two components, even when the totals agree.
        ("RAM", "2x 8GB + 16GB", "ram_capacity_gb", (A, "16; 16")),
        ("RAM", "16GB + (2x 8GB)", "ram_capacity_gb", (A, "16; 16")),
        ("RAM", "(2x 8GB) 16GB", "ram_capacity_gb", (A, "16; 16")),
        ("RAM", "16GB 2x 8GB", "ram_capacity_gb", (A, "16; 16")),
        # Accepted single-total shapes still extract.
        ("RAM", "16GB (2x 8GB) DDR4", "ram_capacity_gb", (E, "16")),
        ("RAM", "16GB*1 DDR5", "ram_capacity_gb", (E, "16")),
        ("Storage", "2 x 512GB SSD", "storage_capacity_gb", (E, "1024")),
    ],
)
def test_multiple_capacity_components_go_to_review(
    label: str, value: str, field: str, expected: tuple[str, str]
) -> None:
    offer = synthetic_offer("laptop_factory", ((label, value),))
    assert statuses(offer)[field] == expected


@pytest.mark.parametrize(
    ("label", "field"), [("RAM", "ram_capacity_gb"), ("Storage", "storage_capacity_gb")]
)
def test_repeated_equivalent_capacity_rows_are_one_fact(label: str, field: str) -> None:
    offer = synthetic_offer("laptop_factory", ((label, "512GB"), (label, "512GB")))
    assert statuses(offer)[field] == (E, "512")
    extracted = extract_offer_attributes(offer)
    assert "EQUIVALENT_REPEATED_FACTS_DEDUPLICATED" in extracted.notes


def test_equal_storage_rows_for_different_media_stay_ambiguous() -> None:
    offer = synthetic_offer("laptop_factory", (("Storage", "512GB SSD"), ("Storage", "512GB HDD")))
    assert statuses(offer)["storage_capacity_gb"] == (A, "512; 512")


def test_mixed_drives_keep_their_layout_and_media() -> None:
    offer = synthetic_offer("laptop_factory", (("Storage", "512GB SSD + 512GB HDD"),))
    extracted = extract_offer_attributes(offer)
    supporting = {item.field: item.value for item in extracted.evidence
                  if item.status is ExtractionStatus.SUPPORTING}
    assert extracted.values["storage_capacity_gb"] == "1024"
    # Never "1024GB SSD": the layout and both media stay visible.
    assert extracted.values["storage_type"] == "HDD+SSD"
    assert supporting["storage_layout"] == "512GB SSD + 512GB HDD"
    assert "STORAGE_MIXED_MEDIA" in extracted.notes


def test_mixed_drive_media_conflict_with_a_target_blocks_assignment() -> None:
    specs = {
        "Model Name": "Acer Aspire AI 14 A14-11M-X3BP (Steam Blue)",
        "Processor": "Snapdragon X X1-26-100",
        "RAM": "16GB",
        "Storage": "512GB SSD + 512GB HDD",
        "Display Size": '14"',
    }
    offer = synthetic_offer("laptop_factory", tuple(specs.items()))
    target = LF_TARGET | {"storage_capacity_gb": "1024", "storage_type": "SSD"}

    result = ExactMatcher([target]).match_observation(offer)

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert "SECONDARY_CONFLICT_STORAGE_TYPE" in result.reason_codes


@pytest.mark.parametrize(
    ("label", "value", "field", "target_override"),
    [
        ("Storage", "512GB SSD + 512GB HDD", "storage_capacity_gb", {}),
        ("RAM", "8GB + 8GB", "ram_capacity_gb", {"ram_capacity_gb": "8"}),
    ],
)
def test_multiple_components_never_match_a_single_component_target(
    label: str, value: str, field: str, target_override: dict[str, str]
) -> None:
    # The summed total differs from the single-component target: a different configuration,
    # never the target and never a near-miss assignment.
    specs = {
        "Model Name": "Acer Aspire AI 14 A14-11M-X3BP (Steam Blue)",
        "Processor": "Snapdragon X X1-26-100",
        "RAM": "16GB",
        "Storage": "512GB SSD",
        "Display Size": '14"',
    } | {label: value}
    offer = synthetic_offer("laptop_factory", tuple(specs.items()))
    target = LF_TARGET | target_override

    result = ExactMatcher([target]).match_observation(offer)

    assert result.status is MatchStatus.UNMATCHED
    assert result.target_id is None
    assert result.reason_codes[0] == "NO_CORE_MATCH"
    differing = [e.field for e in result.evidence if e.status is EvidenceStatus.CONFLICT]
    assert differing == [field]


def test_unlisted_labels_and_titles_are_ignored() -> None:
    offer = synthetic_offer(
        "laptop_factory",
        (("Model Number", "EB14"), ("Memory", "16GB")),  # Not LF labels in reviewed pages.
        raw_title="ExampleBook 14 | 14\" | Core i5 | 16GB | 512GB SSD",
    )
    assert {status for status, _ in statuses(offer).values()} == {M}


def test_unknown_retailer_has_no_extractor() -> None:
    extracted = extract_offer_attributes(synthetic_offer("example_shop", (("RAM", "16GB"),)))
    assert extracted.values == {}
    assert extracted.extractor == ""
    assert extracted.reason_codes[0] == "NO_RETAILER_EXTRACTOR"


def test_multi_variant_detection_applies_only_to_shared_description_retailers() -> None:
    specs = (("Processor", "Intel Core 5 120U"),)
    prefixed = "16GB | Processor: Intel Core 5 120U"
    shared = synthetic_offer("pcc", specs, configuration=prefixed)
    single = synthetic_offer("pcx", specs, configuration=prefixed)
    assert statuses(shared)["cpu_series"] == (N, "")
    assert statuses(single)["cpu_series"] == (E, "Core 5")


# Extraction feeding the matcher

LF_TARGET = {
    "target_id": "TGT-000001",
    "status": "ACTIVE",
    "model": "A14-11M-X3BP",
    "screen_size_in": "14.0",
    "cpu_series": "Snapdragon X",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "512",
}


def test_extracted_offer_matches_exactly_with_missing_target_secondary_details() -> None:
    result = ExactMatcher([LF_TARGET]).match_observation(fixture_offer("lf_sale"))
    assert result.status is MatchStatus.EXACT
    assert result.target_id == "TGT-000001"
    assert result.extractor == "laptop_factory"
    assert all(item.status is ExtractionStatus.EXTRACTED for item in result.extraction_evidence)


def test_extracted_secondary_conflict_goes_to_review() -> None:
    target = LF_TARGET | {"resolution": "1920x1080"}
    result = ExactMatcher([target]).match_observation(fixture_offer("lf_sale"))
    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert result.reason_codes == ("SECONDARY_CONFLICT", "SECONDARY_CONFLICT_RESOLUTION")


def test_extracted_ambiguous_secondary_goes_to_review() -> None:
    offer = synthetic_offer(
        "laptop_factory",
        (
            ("Model Name", "Acer Aspire AI 14 A14-11M-X3BP (Steam Blue)"),
            ("Processor", "Snapdragon X X1-26-100"),
            ("RAM", "16GB"),
            ("Storage", "512GB SSD"),
            ("Display Size", '14" 1920x1200 or 2560x1600'),
        ),
    )
    result = ExactMatcher([LF_TARGET]).match_observation(offer)
    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.reason_codes == ("SECONDARY_CONFLICT", "SECONDARY_CONFLICT_RESOLUTION")


def test_incomplete_extraction_reports_field_reasons() -> None:
    result = ExactMatcher([LF_TARGET]).match_observation(fixture_offer("pcc_variants"))
    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.candidate_target_ids == ()
    assert result.reason_codes[:2] == ("MISSING_CORE_FIELDS", "MULTI_VARIANT_SHARED_DESCRIPTION")
    assert "NOT_EXTRACTED_RAM_CAPACITY_GB" in result.reason_codes
