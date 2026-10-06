from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from laptop_tracker.matching import (
    EvidenceStatus,
    ExactMatcher,
    MatchStatus,
)
from laptop_tracker.offers import Availability, OfferObservation, RetrievalMetadata

BASE_TARGET = {
    "target_id": "TGT-000001",
    "status": "ACTIVE",
    "brand": "ExampleBrand",
    "model": "ExampleBook 14",
    "screen_size_in": "14",
    "cpu_series": "Example Series 5",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "1000",
    "resolution": "1920x1080",
    "display_type": "IPS",
    "refresh_rate_hz": "60",
    "gpu_model": "Example GPU",
    "os": "ExampleOS Home",
}

BASE_ATTRIBUTES = {
    "model": "ExampleBook 14",
    "screen_size_in": "14",
    "cpu_series": "Example Series 5",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "1000",
    "resolution": "1920x1080",
    "display_type": "IPS",
    "refresh_rate_hz": "60",
    "gpu_model": "Example GPU",
    "os": "ExampleOS Home",
}

_SOURCE = RetrievalMetadata(
    requested_url="https://shop.example.test/product",
    final_url="https://shop.example.test/product",
    http_status=200,
    attempts=1,
    content_sha256="0" * 64,
    content_type="text/html",
    adapter_version="fixture-1",
)


def offer() -> OfferObservation:
    return OfferObservation(
        retailer_id="example_shop",
        offer_url="https://shop.example.test/product",
        retrieved_at=datetime(2026, 10, 6, tzinfo=UTC),
        raw_title="Synthetic ExampleBook 14",
        raw_configuration_text="synthetic fixture",
        price=Decimal("1000"),
        currency="PHP",
        raw_price_text="PHP 1,000",
        availability=Availability.IN_STOCK,
        availability_evidence="Synthetic fixture",
        offer_index=0,
        source=_SOURCE,
    )


def test_exact_core_match_normalizes_case_whitespace_and_numeric_format() -> None:
    target = BASE_TARGET | {"model": " ExampleBook   14 ", "screen_size_in": "14.00"}
    attributes = BASE_ATTRIBUTES | {
        "model": "examplebook 14",
        "screen_size_in": "14.0",
        "cpu_series": "example   series 5",
        "ram_capacity_gb": "016",
    }

    result = ExactMatcher([target]).match(offer(), attributes)

    assert result.status is MatchStatus.EXACT
    assert result.target_id == "TGT-000001"
    assert result.reason_codes == ()
    assert all(item.status is EvidenceStatus.MATCH for item in result.evidence)


def test_missing_core_value_is_review_required_even_when_target_has_same_gap() -> None:
    attributes = BASE_ATTRIBUTES | {"cpu_series": ""}

    result = ExactMatcher([BASE_TARGET]).match(offer(), attributes)

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert result.candidate_target_ids == ()
    assert result.reason_codes == ("MISSING_CORE_FIELDS", "MISSING_CPU_SERIES")


def test_invalid_core_value_does_not_match() -> None:
    attributes = BASE_ATTRIBUTES | {"screen_size_in": "14 inches"}

    result = ExactMatcher([BASE_TARGET]).match(offer(), attributes)

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert result.reason_codes == ("INCOMPLETE_OR_INVALID_CORE_FIELDS", "INVALID_SCREEN_SIZE_IN")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "ExampleBook 14 Pro"),
        ("screen_size_in", "14.1"),
        ("cpu_series", "Example Series 7"),
        ("ram_capacity_gb", "32"),
        ("storage_capacity_gb", "1024"),
    ],
)
def test_one_field_near_match_is_unmatched_and_lists_the_near_candidate(
    field: str, value: str
) -> None:
    result = ExactMatcher([BASE_TARGET]).match(offer(), BASE_ATTRIBUTES | {field: value})

    assert result.status is MatchStatus.UNMATCHED
    assert result.target_id is None
    assert result.reason_codes == ("NO_CORE_MATCH", "ONE_CORE_FIELD_DIFFERS")
    assert result.candidate_target_ids == ("TGT-000001",)
    conflicts = [item.field for item in result.evidence if item.status is EvidenceStatus.CONFLICT]
    assert conflicts == [field]


def test_two_differing_core_fields_are_not_near_candidates() -> None:
    attributes = BASE_ATTRIBUTES | {"ram_capacity_gb": "32", "storage_capacity_gb": "512"}

    result = ExactMatcher([BASE_TARGET]).match(offer(), attributes)

    assert result.status is MatchStatus.UNMATCHED
    assert result.reason_codes == ("NO_CORE_MATCH",)
    assert result.candidate_target_ids == ()


def test_trademark_marks_and_unicode_form_are_harmless_formatting() -> None:
    target = BASE_TARGET | {"cpu_series": "Example Series™ 5", "model": "ExampléBook 14"}
    attributes = BASE_ATTRIBUTES | {"cpu_series": "Example® Series 5", "model": "ExampléBook 14"}

    result = ExactMatcher([target]).match(offer(), attributes)

    assert result.status is MatchStatus.EXACT


def test_unit_suffixes_are_not_stripped_by_the_matcher() -> None:
    attributes = BASE_ATTRIBUTES | {"ram_capacity_gb": "16GB"}

    result = ExactMatcher([BASE_TARGET]).match(offer(), attributes)

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert "INVALID_RAM_CAPACITY_GB" in result.reason_codes


def test_duplicate_core_targets_are_ambiguous() -> None:
    targets = [BASE_TARGET, BASE_TARGET | {"target_id": "TGT-000002", "status": "INACTIVE"}]

    result = ExactMatcher(targets).match(offer(), BASE_ATTRIBUTES)

    assert result.status is MatchStatus.AMBIGUOUS
    assert result.target_id is None
    assert result.candidate_target_ids == ("TGT-000001", "TGT-000002")
    assert result.reason_codes == ("DUPLICATE_CORE_TARGETS",)


def test_secondary_conflict_requires_review_and_does_not_publish_target_id() -> None:
    attributes = BASE_ATTRIBUTES | {"gpu_model": "Different GPU"}

    result = ExactMatcher([BASE_TARGET]).match(offer(), attributes)

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert result.candidate_target_ids == ("TGT-000001",)
    assert result.reason_codes == ("SECONDARY_CONFLICT", "SECONDARY_CONFLICT_GPU_MODEL")
    conflicts = [
        (item.field, item.status, item.target_id)
        for item in result.evidence
        if item.status is EvidenceStatus.CONFLICT
    ]
    assert conflicts == [("gpu_model", EvidenceStatus.CONFLICT, "TGT-000001")]


def test_ambiguous_secondary_evidence_is_treated_as_a_conflict() -> None:
    result = ExactMatcher([BASE_TARGET]).match(
        offer(), BASE_ATTRIBUTES, ambiguous_secondary=("resolution",)
    )

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert result.reason_codes == ("SECONDARY_CONFLICT", "SECONDARY_CONFLICT_RESOLUTION")


def test_matching_secondary_details_corroborate_without_changing_the_outcome() -> None:
    result = ExactMatcher([BASE_TARGET]).match(
        offer(), BASE_ATTRIBUTES | {"resolution": "1920 × 1080", "display_type": "ips"}
    )

    assert result.status is MatchStatus.EXACT
    matched = {item.field for item in result.evidence if item.status is EvidenceStatus.MATCH}
    assert {"resolution", "display_type", "refresh_rate_hz", "gpu_model", "os"} <= matched


def test_missing_secondary_values_do_not_block_exact_match() -> None:
    attributes = BASE_ATTRIBUTES | {"resolution": "", "gpu_model": None, "os": ""}

    result = ExactMatcher([BASE_TARGET]).match(offer(), attributes)

    assert result.status is MatchStatus.EXACT
    assert result.target_id == "TGT-000001"
    assert all(item.field not in {"resolution", "gpu_model", "os"} for item in result.evidence)


def test_duplicate_active_core_targets_are_ambiguous_despite_secondary_differences() -> None:
    targets = [BASE_TARGET, BASE_TARGET | {"target_id": "TGT-000002", "gpu_model": "Other GPU"}]

    result = ExactMatcher(targets).match(offer(), BASE_ATTRIBUTES)

    assert result.status is MatchStatus.AMBIGUOUS
    assert result.target_id is None
    assert result.evidence == ()


def test_inactive_targets_are_not_near_candidates() -> None:
    target = BASE_TARGET | {"status": "INACTIVE"}

    result = ExactMatcher([target]).match(offer(), BASE_ATTRIBUTES | {"ram_capacity_gb": "32"})

    assert result.status is MatchStatus.UNMATCHED
    assert result.candidate_target_ids == ()


def test_unique_inactive_target_is_not_matched() -> None:
    target = BASE_TARGET | {"status": "INACTIVE"}

    result = ExactMatcher([target]).match(offer(), BASE_ATTRIBUTES)

    assert result.status is MatchStatus.UNMATCHED
    assert result.target_id is None
    assert result.candidate_target_ids == ("TGT-000001",)
    assert result.reason_codes == ("INACTIVE_TARGET",)


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": ""},
        {"screen_size_in": "unknown"},
        {"cpu_series": None},
        {"ram_capacity_gb": "16 GB"},
        {"storage_capacity_gb": "0"},
    ],
)
def test_invalid_or_incomplete_target_rows_fail_closed(overrides: dict[str, str | None]) -> None:
    with pytest.raises(ValueError, match="incomplete or invalid core fields"):
        ExactMatcher([BASE_TARGET | overrides])


def test_target_ids_must_be_unique_case_insensitively() -> None:
    targets = [BASE_TARGET, BASE_TARGET | {"target_id": "tgt-000001", "model": "Other"}]

    with pytest.raises(ValueError, match="duplicate target_id"):
        ExactMatcher(targets)


def test_match_result_carries_offer_provenance() -> None:
    result = ExactMatcher([BASE_TARGET]).match(offer(), BASE_ATTRIBUTES)

    assert result.retailer_id == "example_shop"
    assert result.offer_url == "https://shop.example.test/product"
    assert result.retrieved_at == datetime(2026, 10, 6, tzinfo=UTC)
