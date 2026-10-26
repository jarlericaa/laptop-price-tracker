"""Apple model presentation and part numbers (2026-10-22, stream B1).

PC Express writes the model row as ``MacBook Neo (MHFC4PP/A)``: the registered product name and,
in parentheses, the retailer's Apple part number. The part number is kept as separate supporting
evidence, the registered name is compared with the catalog, and nothing else in the wording is
removed. DataBlitz titles on disk (``... (Silver) (MHFC4PP/A)``, saved 2026-10-19 and 2026-10-20)
show the same part numbers; no PC Express Neo page was saved, so the row layout here is the
reviewed PC Express ``Model`` row with the wording the research reported.
"""

from __future__ import annotations

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction_layouts import offer, primary, supporting

from laptop_tracker.conventions import apple_part_numbers, is_apple_part_number
from laptop_tracker.extraction import extract_offer_attributes
from laptop_tracker.matching import ExactMatcher, MatchStatus
from laptop_tracker.product_names import default_registry

pytestmark = pytest.mark.usefixtures("no_network")

TITLE = "APPLE MacBook Neo A18 Pro 512GB (MHFC4PP/A) Laptop (Silver)"
NEO_ROWS = (
    ("Processor", "Apple A18 Pro chip with 6-core CPU"),
    ("Display", '13" Liquid Retina display'),
    ("Memory", "8GB unified memory"),
    ("Storage", "512GB SSD"),
)


def neo(model: str, *, storage: str = "512GB SSD", title: str = TITLE):
    rows = tuple((label, storage if label == "Storage" else value) for label, value in NEO_ROWS)
    return offer("pcx", (("Model", model),) + rows, title=title)


def target(target_id: str, storage: str, **changes):
    return {
        "target_id": target_id, "status": "ACTIVE", "brand": "Apple", "model": "MacBook Neo",
        "screen_size_in": "13", "cpu_series": "Apple A18 Pro", "ram_capacity_gb": "8",
        "storage_capacity_gb": storage,
    } | changes


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MacBook Neo (MHFC4PP/A)", ["MHFC4PP/A"]),
        ('Apple MacBook Neo 13" (Silver) (MHFE4PP/A)', ["MHFE4PP/A"]),
        ("MacBook Air 13 (MC6T4X/A)", ["MC6T4X/A"]),
        ("MacBook Neo", []),
        ("ASUS Vivobook S3407VA-LY135WSM", []),
        ("Wi-Fi 6E/A", []),
    ],
)
def test_apple_part_numbers_are_found_as_written(text: str, expected: list[str]) -> None:
    assert apple_part_numbers(text) == expected


def test_only_a_whole_token_is_an_apple_part_number() -> None:
    assert is_apple_part_number("MHFC4PP/A")
    assert not is_apple_part_number("MHFC4PP")
    assert not is_apple_part_number("FA607NUQ-RL011W")
    assert not is_apple_part_number("xMHFC4PP/A")


def test_model_row_with_part_number_is_the_registered_name_and_keeps_the_part_number() -> None:
    page = neo("MacBook Neo (MHFC4PP/A)")
    assert primary(page)["model"] == ("EXTRACTED", "MacBook Neo")
    kept = supporting(page)
    assert kept["model"] == ["MacBook Neo (MHFC4PP/A)"]  # the raw wording stays
    assert kept["part_number_candidate"] == ["MHFC4PP/A"]
    extracted = extract_offer_attributes(page)
    evidence = [item for item in extracted.evidence if item.field == "part_number_candidate"]
    assert evidence[0].source_label.startswith("Model [Apple part number")
    assert "MODEL_NAME_PRESENTATION_NORMALIZED" in extracted.notes


def test_brand_colour_and_part_number_are_all_presentation() -> None:
    page = neo("Apple Macbook Neo (Citrus) (MHFE4PP/A)")
    assert primary(page)["model"] == ("EXTRACTED", "MacBook Neo")
    assert supporting(page)["part_number_candidate"] == ["MHFE4PP/A"]


@pytest.mark.parametrize(
    "wording",
    [
        "MacBook Neo (13-inch) (MHFC4PP/A)",  # a size is identity, never removed
        "MacBook Neo (Foo)",  # an unregistered parenthetical is never stripped
        "MacBook Neo Pro (MHFC4PP/A)",  # an unknown suffix word
        "MacBook Neo 2 (MHFC4PP/A)",
        "MacBook Neo (Purple)",  # a colour not in the registry
    ],
)
def test_unknown_wording_stays_as_written(wording: str) -> None:
    page = neo(wording)
    assert primary(page)["model"] == ("EXTRACTED", wording)
    result = ExactMatcher([target("TGT-000002", "512")]).match_observation(page)
    assert result.status is not MatchStatus.EXACT


def test_neo_page_matches_its_own_storage_only() -> None:
    matcher = ExactMatcher([target("TGT-000002", "512"), target("TGT-000003", "256")])
    result = matcher.match_observation(neo("MacBook Neo (MHFC4PP/A)"))
    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000002")
    small = neo("MacBook Neo (MHFA4PP/A)", storage="256GB SSD",
                title=TITLE.replace("512GB", "256GB").replace("MHFC4PP", "MHFA4PP"))
    result = matcher.match_observation(small)
    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000003")


def test_the_part_number_never_decides_identity() -> None:
    # Another colour has another part number; a recorded part number must not block here.
    page = neo("MacBook Neo (MHFE4PP/A)")
    row = target("TGT-000002", "512", manufacturer_part_number="MHFC4PP/A")
    assert ExactMatcher([row]).match_observation(page).status is MatchStatus.EXACT


@pytest.mark.parametrize("chip", ["Apple M4 Pro", "Apple M4 Max", "Apple M4", "Apple A18 Pro"])
def test_chip_modifiers_are_kept(chip: str) -> None:
    page = offer("pcx", (("Model", "MacBook Pro (MX2E3X/A)"), ("Processor", f"{chip} chip"),
                         ("Display", '14" Liquid Retina XDR'), ("Memory", "24GB unified memory"),
                         ("Storage", "1TB SSD")),
                 title="APPLE MacBook Pro 14")
    assert primary(page)["cpu_series"] == ("EXTRACTED", chip)


def test_registry_normalizes_part_number_wording_and_reports_what_was_removed() -> None:
    normalized = default_registry().normalize_model_name("MacBook Neo (MHFC4PP/A)")
    assert normalized is not None and normalized.value == "MacBook Neo"
    assert normalized.part_numbers == ("MHFC4PP/A",)
    assert any("MHFC4PP/A" in item for item in normalized.removed)
    assert default_registry().normalize_model_name("MacBook Neo (13-inch)") is None
