"""Controlled end-to-end matching safeguards: Laptop Factory extraction, then the exact matcher.

These are synthetic, clearly controlled cases. Product names and codes are invented; only the
specification *wording* follows the shapes seen on reviewed Laptop Factory pages (trademark marks,
the ″ inch sign, a color in parentheses, a RAM total with its on-board/SO-DIMM split). They are
kept separate from the real-offer pilot, whose evidence stays in the ignored local output folder.

Targets follow the 2026-10-08 convention: ``model`` is the verified manufacturer model code and
the family name is in ``series``.
"""

from __future__ import annotations

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction import synthetic_offer

from laptop_tracker.matching import EvidenceStatus, ExactMatcher, MatchStatus

pytestmark = pytest.mark.usefixtures("no_network")

TARGET = {
    "target_id": "TGT-000001",
    "status": "ACTIVE",
    "tracking_mode": "",
    "brand": "ExampleBrand",
    "model": "EB15-0001",
    "series": "ExampleBook 15",
    "screen_size_in": "15.60",
    "cpu_series": "Core 5",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "512",
    "refresh_rate_hz": "165",
    "color": "Example Blue",
}

SPECS = {
    "Model Name": "ExampleBrand ExampleBook™ 15 eb15-0001 (Example Blue)",
    "Processor": "Intel® Core™ 5 processor 210H",
    "RAM": "16GB DDR4",
    "Storage": "512GB PCIe Gen4 NVMe SSD",
    "Display Size": "15.6″ Full HD 1920×1080 IPS, 165 Hz",
}


def lf_offer(**changes: str | None):
    """A synthetic Laptop Factory observation; a ``None`` change removes that labeled row."""
    specs = SPECS | changes
    return synthetic_offer(
        "laptop_factory",
        tuple((label, value) for label, value in specs.items() if value is not None),
    )


def match(offer, *targets: dict[str, str]):
    return ExactMatcher(list(targets or (TARGET,))).match_observation(offer)


def model_evidence(result):
    return next(item for item in result.extraction_evidence if item.field == "model")


def test_harmless_case_whitespace_trademark_color_and_screen_size_forms_match() -> None:
    # The code's letter case, the ™ mark, the brand/family words, the color in parentheses, and
    # 15.6 versus 15.60 are harmless. Observations are whitespace-normalized on construction, so
    # the extra space is on the target side.
    result = match(lf_offer(), TARGET | {"model": " EB15-0001 "})

    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000001")
    assert result.offer_attributes is not None
    assert result.offer_attributes["model"] == "eb15-0001"
    assert result.offer_attributes["screen_size_in"] == "15.6"
    assert result.offer_attributes["cpu_series"] == "Core 5"
    # The retailer's full original wording is kept as evidence.
    evidence = model_evidence(result)
    assert (evidence.source_label, evidence.source_text) == ("Model Name", SPECS["Model Name"])


@pytest.mark.parametrize(
    ("label", "value", "field"),
    [
        ("Model Name", "ExampleBook 15 EB15-0002", "model"),
        # A meaningful code suffix is never stripped.
        ("Model Name", "ExampleBook 15 EB15-0001W", "model"),
        ("Display Size", "16″ WUXGA 1920×1200 IPS, 165 Hz", "screen_size_in"),
        ("Processor", "Intel® Core™ 7 processor 240H", "cpu_series"),
        ("RAM", "8GB DDR4", "ram_capacity_gb"),
        ("Storage", "1TB PCIe Gen4 NVMe SSD", "storage_capacity_gb"),
    ],
)
def test_a_different_core_field_never_matches_the_same_target(
    label: str, value: str, field: str
) -> None:
    result = match(lf_offer(**{label: value}))

    assert result.status is MatchStatus.UNMATCHED
    assert result.target_id is None
    assert result.reason_codes == ("NO_CORE_MATCH", "ONE_CORE_FIELD_DIFFERS")
    assert result.candidate_target_ids == ("TGT-000001",)
    conflicts = [item.field for item in result.evidence if item.status is EvidenceStatus.CONFLICT]
    assert conflicts == [field]


def test_a_model_name_without_a_code_is_kept_as_stated() -> None:
    offer = lf_offer(**{"Model Name": "ExampleBook 15"})

    coded = match(offer)
    assert coded.status is MatchStatus.UNMATCHED
    assert coded.offer_attributes is not None
    assert coded.offer_attributes["model"] == "ExampleBook 15"

    # A target with no verified code keeps its verified name, which can still match exactly.
    named = match(offer, TARGET | {"model": "ExampleBook 15", "series": ""})
    assert (named.status, named.target_id) == (MatchStatus.EXACT, "TGT-000001")


@pytest.mark.parametrize(
    "label_value",
    [
        "ExampleBook 15 EB15-0001 / EB15-0002",
        "ExampleBook 15 EB15-0001 (EB15-0001-KIT)",
    ],
)
def test_two_model_codes_in_one_label_require_review(label_value: str) -> None:
    result = match(lf_offer(**{"Model Name": label_value}))

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert result.reason_codes == ("MISSING_CORE_FIELDS", "AMBIGUOUS_MODEL")


def test_processor_numbers_and_part_numbers_are_not_model_codes() -> None:
    label = "ExampleBook 15 EB15-0001 i5-13420H DDR5-4800 NH.EXAMP.001 C4FW3PA"
    result = match(lf_offer(**{"Model Name": label}))

    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000001")


@pytest.mark.parametrize(
    ("changes", "reasons"),
    [
        ({"Model Name": None}, ("MISSING_CORE_FIELDS", "MISSING_MODEL")),
        ({"Display Size": "15.6, FHD 1920 x 1080"}, (
            "MISSING_CORE_FIELDS",
            "UNPARSEABLE_SCREEN_SIZE_IN",
        )),
        ({"Processor": "Intel Raptor Lake H-series processor"}, (
            "MISSING_CORE_FIELDS",
            "UNPARSEABLE_CPU_SERIES",
        )),
        ({"RAM": "DDR5 16GB (8GB on board + 4GB SO-DIMM)"}, (
            "MISSING_CORE_FIELDS",
            "AMBIGUOUS_RAM_CAPACITY_GB",
        )),
        ({"Storage": "512GB SSD + 512GB HDD"}, (
            "MISSING_CORE_FIELDS",
            "AMBIGUOUS_STORAGE_CAPACITY_GB",
        )),
    ],
    ids=["missing-model", "no-unit-no-display-word", "no-cpu-number", "ram-sum-mismatch",
         "two-drives"],
)
def test_missing_or_ambiguous_core_evidence_requires_review(
    changes: dict[str, str | None], reasons: tuple[str, ...]
) -> None:
    result = match(lf_offer(**changes))

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert result.candidate_target_ids == ()
    assert result.reason_codes == reasons


def test_secondary_conflict_keeps_the_core_candidate_but_requires_review() -> None:
    result = match(lf_offer(**{"Display Size": "15.6″ Full HD 1920×1080 IPS, 144 Hz"}))

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert result.candidate_target_ids == ("TGT-000001",)
    assert result.reason_codes == ("SECONDARY_CONFLICT", "SECONDARY_CONFLICT_REFRESH_RATE_HZ")
    core = [item for item in result.evidence if item.status is EvidenceStatus.MATCH]
    assert {"model", "screen_size_in", "cpu_series", "ram_capacity_gb"} <= {
        item.field for item in core
    }


def test_color_conflict_keeps_the_core_candidate_but_requires_review() -> None:
    # The LF extractor does not extract color, so a structured color is supplied directly.
    offer = lf_offer()
    attributes = dict(match(offer).offer_attributes or {}) | {"color": "Example Grey"}

    result = ExactMatcher([TARGET]).match(offer, attributes)

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.candidate_target_ids == ("TGT-000001",)
    assert result.reason_codes == ("SECONDARY_CONFLICT", "SECONDARY_CONFLICT_COLOR")


def test_listing_only_target_never_receives_a_cross_retailer_match() -> None:
    listing = TARGET | {
        "tracking_mode": "LISTING_ONLY",
        "shop_id": "10",
        "item_id": "20",
        "source_model_id": "0042",
    }

    result = match(lf_offer(), listing)

    assert result.status is MatchStatus.UNMATCHED
    assert result.target_id is None
    assert result.candidate_target_ids == ()
    assert result.reason_codes == ("NO_CORE_MATCH",)


def test_inactive_target_never_receives_a_match() -> None:
    result = match(lf_offer(), TARGET | {"status": "INACTIVE"})

    assert result.status is MatchStatus.UNMATCHED
    assert result.target_id is None
    assert result.reason_codes == ("INACTIVE_TARGET",)
