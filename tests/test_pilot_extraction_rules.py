"""Narrow extraction rules for the three 2026-10-08 pilot review cases. Synthetic data only.

Each rule is pinned by positive and negative cases, and is also exercised through the real
Laptop Factory adapter (an HTML fixture whose spec cells take the reviewed wording shapes),
the extractor, and the exact matcher. Product names and codes here are invented.
"""

from __future__ import annotations

import html
import re

import pytest
from scraping_fakes import FakeClock, FakeTransport, no_network  # noqa: F401
from test_extraction import FIXTURES, HTML, statuses, synthetic_offer

from laptop_tracker.adapters.laptop_factory import HOST as LF_HOST
from laptop_tracker.adapters.laptop_factory import LaptopFactoryAdapter
from laptop_tracker.matching import EvidenceStatus, ExactMatcher, MatchStatus
from laptop_tracker.scraping import FetchConfig, Fetcher, ScrapeRequest, TransportResponse, scrape

pytestmark = pytest.mark.usefixtures("no_network")

E, A, U = "EXTRACTED", "AMBIGUOUS", "UNPARSEABLE"


def lf(label: str, value: str):
    return synthetic_offer("laptop_factory", ((label, value),))


# 1. Screen size: a size-labeled row that opens with a unit-less laptop diagonal


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("14.0 display, WUXGA 1920 x 1200", (E, "14")),
        ("16.0 display WUXGA", (E, "16")),
        ("15.6 Display", (E, "15.6")),
        # An inch-marked size still wins and is read as before.
        ('14.0" display, WUXGA', (E, "14")),
        # Not accepted: the number must open the value and be followed by "display".
        ("WUXGA display 14.0", (U, "")),
        ("14.0, WUXGA 1920 x 1200", (U, "")),
        ("14.0 WUXGA display", (U, "")),
        ("1920 display", (U, "")),  # resolution width
        ("144 display", (U, "")),  # refresh rate
        ("9.7 display", (U, "")),  # outside the laptop range
        ("20 display", (U, "")),
        ("FHD display", (U, "")),
    ],
)
def test_unit_less_diagonal_only_under_a_size_label(value: str, expected: tuple[str, str]) -> None:
    assert statuses(lf("Display Size", value))["screen_size_in"] == expected


@pytest.mark.parametrize(
    "value",
    [
        # Unit-less alternatives: the separator and spacing must not hide the second size.
        "14.0 display or 16.0 display",
        "14.0 display/16.0 display",
        "14.0 display / 16.0 display",
        "14.0 display,16.0 display",
        "14.0 display; 16.0 display",
        "14.0 display|16.0 display",
        "14.0 display (16.0 display)",
        "14.0 display, WUXGA 1920 x 1200/16.0 display, WQXGA 2560 x 1600",
        # Mixed: an inch marker must not hide a conflicting unit-less size, in either order.
        '14" / 16.0 display',
        '14″/16.0 display',
        '14.0 display/16"',
        "14.0 display / 16-inch",
    ],
)
def test_conflicting_sizes_in_a_size_row_are_ambiguous(value: str) -> None:
    assert statuses(lf("Display Size", value))["screen_size_in"] == (A, "14; 16")


@pytest.mark.parametrize("value", ['14"/16"', '14" / 16"', '14",16"'])
def test_two_inch_marked_sizes_stay_ambiguous_as_before(value: str) -> None:
    assert statuses(lf("Display Size", value))["screen_size_in"] == (A, "14 | 16")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The genuine single-size pilot row.
        ("14.0 display, WUXGA 1920 x 1200", (E, "14")),
        # The same size stated twice is one size.
        ("14.0 display (14 display)", (E, "14")),
        ('14" / 14.0 display', (E, "14")),
        # Resolutions and refresh rates beside a size are not second sizes.
        ("14.0 display, 1920 x 1200 display, 144Hz", (E, "14")),
        ('15.6" display, 144 display modes', (E, "15.6")),
    ],
)
def test_one_stated_size_is_still_extracted(value: str, expected: tuple[str, str]) -> None:
    assert statuses(lf("Display Size", value))["screen_size_in"] == expected


def test_unit_less_diagonal_is_not_read_under_a_generic_display_label() -> None:
    # PC Central labels the row "Display", which does not name a size.
    offer = synthetic_offer("pcc", (("Display", "14.0 display, WUXGA 1920 x 1200"),))
    assert statuses(offer)["screen_size_in"] == (U, "")


# 2. RAM: an explicit total followed by its own component breakdown


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("DDR5 16GB (8GB on board + 8GB SO-DIMM)", (E, "16")),
        ("16GB (8GB + 8GB) DDR5", (E, "16")),
        ("24GB DDR5 (8GB onboard + 16GB SO-DIMM)", (E, "24")),
        # Earlier accepted shapes still work.
        ("16GB DDR4", (E, "16")),
        ("16GB (2x 8GB) DDR4", (E, "16")),
        ("16GB*1 DDR5", (E, "16")),
        # Explicit installed components in a memory row are summed (balanced policy, 2026-10-15).
        ("8GB + 8GB", (E, "16")),
        ("8GB onboard + 8GB SO-DIMM", (E, "16")),
        # Still unresolved: sum mismatch, extra or alternative amounts, malformed.
        ("(8GB on board + 8GB SO-DIMM)", (A, "8; 8")),
        ("8GB + 8GB optional", (A, "8; 8")),
        ("8GB + 8GB slot", (A, "8; 8")),
        ("16GB (8GB + 4GB)", (A, "16; 8; 4")),
        ("16GB (8GB on board)", (A, "16; 8")),
        ("16GB (8GB + 8GB), expandable to 32GB", (A, "16; 8; 8; 32")),
        ("16GB (8GB + 8GB) or 32GB (16GB + 16GB)", (A, "16; 8; 8; 32; 16; 16")),
        ("16GB (8GB/8GB)", (A, "16; 8; 8")),
        ("16GB + (8GB + 8GB)", (A, "16; 8; 8")),
        ("16GB (8GB + 8GB) + 8GB", (A, "16; 8; 8; 8")),
        ("(8GB + 8GB) 16GB", (A, "8; 8; 16")),
        ("16GB (8GB + 8GB", (A, "16; 8; 8")),
        ("16GB (8GB + 8GB) (16GB)", (A, "16; 8; 8; 16")),
        ("16GB (2x 8GB + 8GB)", (A, "16; 16; 8")),
        ("16GB (8GB + 8GB up to 32GB)", (A, "16; 8; 8; 32")),
    ],
)
def test_ram_total_with_component_breakdown(value: str, expected: tuple[str, str]) -> None:
    assert statuses(lf("RAM", value))["ram_capacity_gb"] == expected


def test_repeated_equivalent_ram_rows_are_one_fact() -> None:
    # The same installed amount stated twice is one fact, never 32GB or an ambiguity.
    offer = synthetic_offer(
        "laptop_factory", (("RAM", "16GB (8GB + 8GB)"), ("RAM", "16GB (8GB + 8GB)"))
    )
    assert statuses(offer)["ram_capacity_gb"] == (E, "16")


def test_equal_ram_rows_for_different_components_stay_ambiguous() -> None:
    # 8GB onboard and 8GB SO-DIMM are two parts, not a repeated fact.
    offer = synthetic_offer(
        "laptop_factory", (("RAM", "8GB onboard"), ("RAM", "8GB SO-DIMM"))
    )
    assert statuses(offer)["ram_capacity_gb"] == (A, "8; 8")


@pytest.mark.parametrize("value", ["1TB (512GB SSD + 512GB SSD)", "1TB (512GB + 512GB)"])
def test_the_breakdown_rule_does_not_apply_to_storage(value: str) -> None:
    # A stated total with a parenthesized drive list stays in review (no double counting).
    assert statuses(lf("Storage", value))["storage_capacity_gb"][0] == A


# 3. CPU: a complete Intel Core processor number in a processor field


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Raptor Lake i5-13420H", (E, "Core i5")),
        ("Intel i7-1355U", (E, "Core i7")),
        ("i3-1115G4 processor", (E, "Core i3")),
        ("Intel Core i5-13420H", (E, "Core i5")),
        ("13th Gen i9-13980HX", (E, "Core i9")),
        # An isolated tier token in a labeled processor field (balanced policy, 2026-10-15).
        ("Intel i5", (E, "Core i5")),
        ("i5 processor", (E, "Core i5")),
        # Not enough: generic wording, a non-Core SKU, a glued or lower-case number, a short
        # number.
        ("Intel Core processor", (U, "")),
        ("i3-N305", (U, "")),
        ("Inteli5-13450HX", (U, "")),
        ("EXi5-13420H", (U, "")),
        ("i5-13420h", (U, "")),
        ("i5-123H", (U, "")),
        # Matching is by CPU family (owner policy, 2026-10-12): processor numbers in the same
        # family give one cpu_series; the full wording stays in the evidence.
        ("i5-13420H or i5-1335U", (E, "Core i5")),
        ("Intel Core i5-13420H / i5-1335U", (E, "Core i5")),
        # Different families are ambiguous.
        ("i5-13420H or i7-13620H", (A, "Core i5 | Core i7")),
        ("Intel Core i5-13420H / Core i7-13620H", (A, "Core i5 | Core i7")),
        ("Raptor Lake i5-13420H / AMD Ryzen 5 7530U", (A, "Core i5 | Ryzen 5")),
    ],
)
def test_intel_core_processor_numbers(value: str, expected: tuple[str, str]) -> None:
    assert statuses(lf("Processor", value))["cpu_series"] == expected


# Through the real adapter, extractor, and matcher

CELLS = {
    "Model Name": "ExampleBrand Example Go EXG14-75-55MB (Example Blue)",
    "Processor": "Intel Core Ultra 5 226V processor",
    "RAM": "16GB of onboard LPDDR5X",
    "Storage": "512GB NVMe SSD",
    "Display Size": "14&#8243; WUXGA (1920&#215;1200) IPS",
}
TARGET = {
    "target_id": "TGT-000001",
    "status": "ACTIVE",
    "model": "EXG14-75-55MB",
    "screen_size_in": "14",
    "cpu_series": "Core Ultra 5",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "512",
}


def adapter_offer(**cells: str):
    """The LF sale fixture with its spec cells replaced, scraped through the fake transport."""
    body = (FIXTURES / "laptop_factory" / "lf_product_sale.html").read_text(encoding="utf-8")
    for label, value in (CELLS | cells).items():
        body, count = re.subn(
            rf"(<td>{re.escape(label)}</td><td>)[^<]*(</td>)",
            lambda m, v=value: m[1] + v + m[2],
            body,
        )
        assert count == 1, label
    # Keep the title consistent with the replaced Model Name: a title naming another model
    # code is a genuine model conflict (MODEL_TITLE_CONFLICT), which is not under test here.
    body = body.replace("Acer Aspire AI 14 A14-11M-X3BP", "ExampleBrand Example Go EXG14-75-55MB")
    url = f"https://{LF_HOST}/product/acer-aspire-ai-14-a14-11m-x3bp/"
    clock = FakeClock()
    transport = FakeTransport(clock, TransportResponse(200, url, body.encode(), HTML))
    [result] = scrape(
        LaptopFactoryAdapter(),
        [ScrapeRequest("laptop_factory", url)],
        Fetcher(transport, FetchConfig(), clock),
    )
    assert result.ok, result.failures
    [observation] = result.observations
    return observation


def match(offer, *targets):
    return ExactMatcher(list(targets or (TARGET,))).match_observation(offer)


def evidence(result, field: str):
    return next(item for item in result.extraction_evidence if item.field == field)


def test_adapter_path_unit_less_size_row_matches_with_its_evidence() -> None:
    result = match(adapter_offer(**{"Display Size": "14.0 display, WUXGA 1920 x 1200"}))

    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000001")
    screen = evidence(result, "screen_size_in")
    assert (screen.value, screen.source_label, screen.source_text) == (
        "14",
        "Display Size",
        "14.0 display, WUXGA 1920 x 1200",
    )


def test_adapter_path_ram_breakdown_matches_with_its_evidence() -> None:
    offer = adapter_offer(RAM="DDR5 16GB (8GB on board + 8GB SO-DIMM)")
    result = match(offer)

    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000001")
    assert evidence(result, "ram_capacity_gb").source_text == (
        "DDR5 16GB (8GB on board + 8GB SO-DIMM)"
    )


def test_adapter_path_installed_ram_components_are_summed() -> None:
    result = match(adapter_offer(RAM="8GB + 8GB"))

    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000001")
    components = evidence(result, "ram_components")
    assert (components.value, components.source_text) == ("8GB + 8GB", "8GB + 8GB")


@pytest.mark.parametrize(
    "ram", ["16GB (8GB + 4GB)", "8GB + 8GB optional", "16GB (8GB + 8GB), expandable to 32GB"]
)
def test_adapter_path_unresolved_ram_never_gets_a_target(ram: str) -> None:
    result = match(adapter_offer(RAM=html.escape(ram)))

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert "AMBIGUOUS_RAM_CAPACITY_GB" in result.reason_codes


def test_adapter_path_cpu_number_parses_but_a_ram_difference_still_blocks_the_target() -> None:
    # Mirrors the MSI case: the processor now gives Core i5, the page states 16GB, and the
    # target records 8GB. The CPU fix must not turn that into an exact match.
    target = TARGET | {"cpu_series": "Core i5", "ram_capacity_gb": "8", "screen_size_in": "15.6"}
    offer = adapter_offer(
        Processor="Raptor Lake i5-13420H",
        RAM="16GB RAM DDR4-3200",
        **{"Display Size": "15.6&#8243; FHD (1920&#215;1080), 144Hz, IPS-Level"},
    )
    result = match(offer, target)

    assert result.status is MatchStatus.UNMATCHED
    assert result.target_id is None
    assert result.candidate_target_ids == ("TGT-000001",)
    conflicts = [item.field for item in result.evidence if item.status is EvidenceStatus.CONFLICT]
    assert conflicts == ["ram_capacity_gb"]
    cpu = evidence(result, "cpu_series")
    assert (cpu.value, cpu.source_text) == ("Core i5", "Raptor Lake i5-13420H")


@pytest.mark.parametrize(
    "display",
    [
        "14.0 display/16.0 display",
        "14.0 display / 16.0 display, WUXGA 1920 x 1200",
        "14&#8243; / 16.0 display",
        "14.0 display/16&#8243;",
    ],
)
def test_adapter_path_conflicting_sizes_go_to_review_without_a_target(display: str) -> None:
    # Two targets share the rest of the key at 14 and 16 inches; neither may be chosen.
    targets = [TARGET, TARGET | {"target_id": "TGT-000002", "screen_size_in": "16"}]
    result = match(adapter_offer(**{"Display Size": display}), *targets)

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert result.candidate_target_ids == ()
    assert result.reason_codes == ("MISSING_CORE_FIELDS", "AMBIGUOUS_SCREEN_SIZE_IN")
    screen = evidence(result, "screen_size_in")
    assert (screen.value, screen.source_label) == ("14; 16", "Display Size")


def test_adapter_path_same_family_processor_numbers_match_by_family() -> None:
    target = TARGET | {"cpu_series": "Core i5"}
    result = match(adapter_offer(Processor="Intel Core i5-13420H or i5-1335U"), target)

    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000001")
    cpu = evidence(result, "cpu_series")
    # Matching uses the canonical family; the full processor wording stays in the evidence.
    assert (cpu.value, cpu.source_text) == ("Core i5", "Intel Core i5-13420H or i5-1335U")


def test_adapter_path_two_processors_stay_in_review() -> None:
    result = match(adapter_offer(Processor="i5-13420H or i7-13620H"))

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert "AMBIGUOUS_CPU_SERIES" in result.reason_codes
