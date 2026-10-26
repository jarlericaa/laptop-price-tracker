"""Exact processor models (2026-10-22): extraction with provenance, presentation-only
normalization, and the matcher rule that a verified exact-processor disagreement blocks automatic
comparison while a missing or family-only value stays unknown.

Pages are synthetic; their wording follows saved retailer rows (PC Express ``Intel Core 5-210H``,
Laptop Factory ``Apple A18 Pro chip (6-core CPU ...)``, the IdeaPad ``Ryzen 7 170`` versus
``Ryzen 7 7735HS`` listings).
"""

from __future__ import annotations

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_balanced_policy import PCX_SPECS, VIVO, pcx
from test_extraction_layouts import notes, offer, primary, supporting

from laptop_tracker.conventions import processor_models
from laptop_tracker.extraction import ExtractionStatus, extract_offer_attributes
from laptop_tracker.listed_spec import SECONDARY_CONFLICT, check_target
from laptop_tracker.matching import (
    SECONDARY_SEVERITY,
    BLOCKING,
    EvidenceStatus,
    ExactMatcher,
    MatchStatus,
    verified_secondary_difference,
)

pytestmark = pytest.mark.usefixtures("no_network")
ATTRIBUTES = {k: v for k, v in VIVO.items() if k not in {"target_id", "status"}}


# The parser: exact models, never family names


@pytest.mark.parametrize(
    ("text", "display"),
    [
        ("Intel Core i3-1315U", "Core i3-1315U"),
        ("13th Gen Intel® Core™ i5-13420H Processor", "Core i5-13420H"),
        ("Intel Core i5 12450H", "Core i5-12450H"),  # no hyphen
        ("i7-1355U", "Core i7-1355U"),
        ("Intel Core 5-210H", "Core 5 210H"),
        ("Intel Core 5 processor 220H", "Core 5 220H"),
        ("Intel® Core™ Ultra 7 155H", "Core Ultra 7 155H"),
        ("Intel Core Ultra 5 Processor 125H", "Core Ultra 5 125H"),
        ("AMD Ryzen 7 170", "Ryzen 7 170"),  # short, three digits, no suffix
        ("AMD Ryzen™ 7 7735HS", "Ryzen 7 7735HS"),
        ("Ryzen 5 7520U", "Ryzen 5 7520U"),
        ("AMD Ryzen AI 9 HX 370", "Ryzen AI 9 HX 370"),
        ("Apple M4 Pro", "Apple M4 Pro"),
        ("M4 Max chip", "Apple M4 Max"),
        ("Apple A18 Pro chip (6-core CPU, 5-core GPU)", "Apple A18 Pro"),
        ("Intel Processor N150", "Intel N150"),
        ("Celeron N4020", "Celeron N4020"),
    ],
)
def test_explicit_exact_processor_models_have_one_canonical_form(text, display) -> None:
    found = processor_models(text, bare_apple=False)
    assert [model.display for model in found] == [display]
    assert found[0].text  # the wording as stated is kept for provenance


@pytest.mark.parametrize(
    "text",
    [
        "Intel Core i5", "Core i5 12th Gen", "AMD Ryzen 7", "Ryzen 5 512GB SSD",
        "Ryzen 7 16GB RAM 512GB", "Core i5 1024GB", "Core 5 512GB", "Apple M4", "Apple M",
        "Core Ultra 7", "Intel Core", "i5 / Ryzen 5", "Up to Intel Core i7",
    ],
)
def test_a_family_or_a_capacity_is_never_read_as_an_exact_model(text) -> None:
    found = processor_models(text, bare_apple=False)
    # "Apple M4" is a chip family without a modifier: it is an exact model only for Apple.
    assert [m.display for m in found if not m.display.startswith("Apple")] == []


def test_a_bare_apple_chip_is_a_model_only_in_a_processor_context() -> None:
    assert [m.display for m in processor_models("M5", bare_apple=True)] == ["Apple M5"]
    assert processor_models("M5", bare_apple=False) == []
    assert [m.display for m in processor_models("A18 Pro", bare_apple=True)] == ["Apple A18 Pro"]


def test_actual_processor_distinctions_are_preserved() -> None:
    keys = {m.key for text in ("Core 5 210H", "Core 5 220H", "Core i5-1235U", "Core i5-1235P",
                               "Ryzen 7 7735HS", "Ryzen 7 7735H", "Ryzen 7 170",
                               "Apple M4", "Apple M4 Pro", "Apple M4 Max", "Apple A18",
                               "Apple A18 Pro")
            for m in processor_models(text, bare_apple=True)}
    assert len(keys) == 12


def test_several_models_in_one_statement_are_all_reported() -> None:
    found = processor_models("Intel Core i5-13420H / Core i7-13620H", bare_apple=False)
    assert [m.display for m in found] == ["Core i5-13420H", "Core i7-13620H"]


# The matcher: a verified disagreement blocks; unknown stays unknown


def test_the_exact_processor_is_a_blocking_field_by_default() -> None:
    assert SECONDARY_SEVERITY["cpu_model"] == BLOCKING


def test_a_verified_exact_processor_disagreement_blocks_automatic_comparison() -> None:
    target = VIVO | {"cpu_model": "Core 5 210H"}
    result = ExactMatcher([target]).match(pcx("x"), ATTRIBUTES | {"cpu_model": "Core 5 220H"})
    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert "SECONDARY_CONFLICT_CPU_MODEL" in result.reason_codes
    conflict = next(e for e in result.evidence if e.field == "cpu_model")
    assert (conflict.status, conflict.offer_value, conflict.target_value) == (
        EvidenceStatus.CONFLICT, "Core 5 220H", "Core 5 210H")


def test_the_same_processor_written_differently_is_one_processor() -> None:
    target = VIVO | {"cpu_model": "i5-12450H", "cpu_series": "Core i5"}
    attributes = ATTRIBUTES | {"cpu_series": "Core i5"}
    for stated in ("Intel Core i5 12450H", "12th Gen Intel(R) Core(TM) i5-12450H", "i5-12450h"):
        result = ExactMatcher([target]).match(pcx("x"), attributes | {"cpu_model": stated})
        assert result.status is MatchStatus.EXACT, stated
        assert not result.warnings


def test_missing_or_family_only_processor_information_stays_unknown() -> None:
    matcher = ExactMatcher([VIVO | {"cpu_model": "Core 5 210H"}])
    # The offer states no exact processor.
    assert matcher.match(pcx("x"), ATTRIBUTES).status is MatchStatus.EXACT
    # The offer states a family only.
    assert matcher.match(pcx("x"), ATTRIBUTES | {"cpu_model": "Core 5"}).status is (
        MatchStatus.EXACT)
    # The target records a family only: nothing to contradict.
    family = ExactMatcher([VIVO | {"cpu_model": "Core 5"}])
    result = family.match(pcx("x"), ATTRIBUTES | {"cpu_model": "Core 5 220H"})
    assert result.status is MatchStatus.EXACT and not result.warnings
    assert not verified_secondary_difference("cpu_model", "Ryzen 7", "Ryzen 7 170")


def test_a_short_amd_name_disagrees_with_the_longer_one_of_the_same_family() -> None:
    assert verified_secondary_difference("cpu_model", "Ryzen 7 7735HS", "Ryzen 7 170")
    assert not verified_secondary_difference("cpu_model", "AMD Ryzen 7 170", "Ryzen 7 170")


def test_apple_chip_modifiers_are_not_presentation() -> None:
    assert verified_secondary_difference("cpu_model", "Apple M4 Pro", "M4")
    assert verified_secondary_difference("cpu_model", "Apple A18 Pro", "A18")
    assert not verified_secondary_difference("cpu_model", "Apple M4 Pro", "M4 Pro")


def test_the_legacy_warning_is_reachable_only_by_an_explicit_override() -> None:
    from laptop_tracker.matching import LEGACY_CPU_MODEL_WARNING

    target = VIVO | {"cpu_model": "Core 5 210H"}
    legacy = ExactMatcher([target], severity_overrides=LEGACY_CPU_MODEL_WARNING)
    result = legacy.match(pcx("x"), ATTRIBUTES | {"cpu_model": "Core 5 220H"})
    assert result.status is MatchStatus.EXACT
    assert "SECONDARY_WARNING_CPU_MODEL" in result.warnings


# Extraction with provenance


def test_a_labeled_processor_row_supplies_the_exact_model_with_its_source() -> None:
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM", PCX_SPECS)
    item = next(i for i in extract_offer_attributes(page).evidence
                if i.field == "cpu_model" and i.status is ExtractionStatus.EXTRACTED)
    assert item.value == "Core 5 210H"
    assert item.source_label == "Processor"
    assert item.source_text == "Intel Core 5-210H"
    # The family stays the identity value; the model is secondary.
    assert primary(page)["cpu_series"] == ("EXTRACTED", "Core 5")


def test_two_installed_models_in_one_processor_row_are_ambiguous() -> None:
    specs = (("Processor", "Intel Core i5-13420H, Core i5-13500H"),) + PCX_SPECS[1:]
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM", specs)
    assert primary(page)["cpu_model"][0] == "AMBIGUOUS"
    target = VIVO | {"cpu_series": "Core i5", "cpu_model": "Core i5-13420H"}
    result = ExactMatcher([target]).match_observation(page)
    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert "SECONDARY_CONFLICT_CPU_MODEL" in result.reason_codes


def test_a_capability_clause_is_not_the_installed_processor() -> None:
    specs = (("Processor", "Intel Core i5-1335U (up to Core i7-1355U)"),)
    page = offer("pcx", specs, title="Synthetic")
    assert primary(page)["cpu_model"] == ("EXTRACTED", "Core i5-1335U")
    assert "Core i7-1355U" in " ".join(supporting(page).get("cpu_series", []))


def test_a_title_states_the_processor_only_when_the_page_rows_do_not() -> None:
    title = "ASUS Vivobook S16 S3607VA-RP181WSM Core 5 220H 16GB 512GB"
    page = pcx(title, (("Processor", "Intel Core 5"),) + PCX_SPECS[1:])
    assert primary(page)["cpu_model"] == ("EXTRACTED", "Core 5 220H")
    item = next(i for i in extract_offer_attributes(page).evidence if i.field == "cpu_model")
    assert item.source_label.startswith("title [")
    # A row that states a model wins; the existing title/spec note reports the difference and
    # the page's specification value is the one compared.
    specs = (("Processor", "Intel Core 5-210H"),) + PCX_SPECS[1:]
    both = pcx(title, specs)
    assert primary(both)["cpu_model"] == ("EXTRACTED", "Core 5 210H")
    assert "CPU_NUMBER_TITLE_DIFFERS" in notes(both)


def test_the_page_value_is_compared_with_the_target_record_end_to_end() -> None:
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM", PCX_SPECS)
    agree = ExactMatcher([VIVO | {"cpu_model": "Core 5 210H"}]).match_observation(page)
    assert agree.status is MatchStatus.EXACT and not agree.warnings
    differ = ExactMatcher([VIVO | {"cpu_model": "Core 5 220H"}]).match_observation(page)
    assert differ.status is MatchStatus.REVIEW_REQUIRED
    assert "SECONDARY_CONFLICT_CPU_MODEL" in differ.reason_codes


# Listed-spec path (the same matcher rule, no parallel engine)

FAMILY = dict(target_id="TGT-000001", status="ACTIVE", brand="Lenovo", model="IdeaPad",
              screen_size_in="15.3", cpu_series="Ryzen 7", ram_capacity_gb="16",
              storage_capacity_gb="512", manufacturer_model_code="", gpu_model="",
              display_type="", resolution="", cpu_model="Ryzen 7 170")
OFFER = {"brand": "Lenovo", "model": "83K700HXPH", "screen_size_in": "15.3",
         "cpu_series": "Ryzen 7", "ram_capacity_gb": "16", "storage_capacity_gb": "512"}


def test_listed_spec_does_not_compare_a_contradicted_exact_processor() -> None:
    title = "Lenovo IdeaPad Slim 3 15ARP10 (83K700HXPH) AMD Ryzen 7 7735HS Laptop"
    result = check_target(FAMILY, OFFER, title)
    assert result.outcome == SECONDARY_CONFLICT
    assert "cpu_model" in result.detail and "7735HS" in result.detail
    # The same page with the processor the target records still qualifies.
    ok = check_target(FAMILY, OFFER, title.replace("7735HS", "170"))
    assert ok.qualifies and not ok.warnings
    # A title without an exact processor stays unknown.
    assert check_target(FAMILY, OFFER, "Lenovo IdeaPad Slim 3 (83K700HXPH) AMD Ryzen 7").qualifies
    # An extracted page value is compared in the same way.
    conflict = check_target(FAMILY, OFFER | {"cpu_model": "Ryzen 7 7735HS"},
                            "Lenovo IdeaPad Slim 3 (83K700HXPH)")
    assert conflict.outcome == SECONDARY_CONFLICT
