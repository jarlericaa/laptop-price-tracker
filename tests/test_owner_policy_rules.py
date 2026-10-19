"""Regression tests for the owner-approved 2026-10-13 extraction rules and research boundaries.

A. Explicit product-description screen sizes may supply inches.
B. Shared CPU family and screen may be inherited only through a bound selection and a complete
   RAM/storage-only variant matrix.
C. Manufacturer research (2026-10-13) adds no extraction input: factory specifications,
   research target hints, OPI meaning, retailer SKUs, and barcode leads never fill a field.

All values are synthetic; the shapes follow the saved Phase 10 pages.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction_layouts import (
    BOUND,
    DBZ_ROWS,
    OCT_ROWS,
    OCT_TITLE,
    PCC_DESCRIPTION,
    PCC_TITLE,
    PCX_ROWS,
    TARGET,
    notes,
    offer,
    primary,
    supporting,
)

from laptop_tracker.collection import ManifestEntry, expected_target_check
from laptop_tracker.extraction import ExtractionStatus, extract_offer_attributes
from laptop_tracker.match_run import read_observations_csv
from laptop_tracker.matching import ExactMatcher, MatchStatus
from laptop_tracker.offer_terms import BUNDLE_FLAG_POLICY
from laptop_tracker.offers import (
    IDENTIFIER,
    OBSERVATION_COLUMNS,
    OFFER_DETAIL_COLUMNS,
    PRODUCT_DESCRIPTION,
    SELECTED_VARIANT,
    VARIANT_DEFINITIONS,
    ObservationValidationError,
    OfferDetails,
    PriceType,
)
from laptop_tracker.replay import price_comparability

pytestmark = pytest.mark.usefixtures("no_network")

E, M, A, U, N, R = (
    "EXTRACTED",
    "MISSING",
    "AMBIGUOUS",
    "UNPARSEABLE",
    "NOT_EXTRACTED",
    "UNRESOLVED",
)
LOCATOR = "product description > p[3] (outside tables)"


def evidence(page, field: str, status: ExtractionStatus = ExtractionStatus.EXTRACTED):
    return next(
        item for item in extract_offer_attributes(page).evidence
        if item.field == field and item.status is status
    )


# A. Product-description screen sizes


def dbz_page(display_rows, prose: tuple[str, ...] = (), **kwargs):
    return offer(
        "datablitz",
        (("Model", "SFG14-75-55MB"), *display_rows, *DBZ_ROWS),
        scoped=tuple((PRODUCT_DESCRIPTION, LOCATOR, text) for text in prose),
        **kwargs,
    )


def test_explicit_product_inches_supply_the_size_with_their_locator() -> None:
    page = dbz_page((), ("The laptop has a 14-inch display with WUXGA resolution.",))
    assert primary(page)["screen_size_in"] == (E, "14")
    item = evidence(page, "screen_size_in")
    assert item.source_label == f"{LOCATOR} [PRODUCT_DESCRIPTION; explicit inches]"
    assert item.source_text == "The laptop has a 14-inch display with WUXGA resolution."
    assert primary(dbz_page((), ("Its display size is 14 inches.",)))["screen_size_in"] == (E, "14")


def test_size_not_tied_to_a_display_noun_falls_back_to_review() -> None:
    # Conservative: an adjective between the size and the noun is not read (known gap).
    page = dbz_page((), ("The laptop has a 14-inch WUXGA display.",))
    assert primary(page)["screen_size_in"] == (M, "")


def test_bare_size_alone_cannot_supply_units() -> None:
    assert primary(dbz_page((("Size", "14.0"),)))["screen_size_in"] == (U, "")


def test_agreeing_specification_and_prose_sizes_succeed() -> None:
    bare = dbz_page((("Size", "14.0"),), ("A 14-inch display.",))
    marked = dbz_page((("Size", '14"'),), ("A 14-inch display.",))
    assert primary(bare)["screen_size_in"] == (E, "14")
    assert primary(marked)["screen_size_in"] == (E, "14")


@pytest.mark.parametrize(
    ("rows", "prose"),
    [
        ((("Size", "16.0"),), ("A 14-inch display.",)),  # bare row contradicts prose
        ((("Size", '15.6"'),), ("A 14-inch display.",)),  # inch-marked row contradicts prose
        ((), ("A 14-inch display.", "A 16-inch display.")),  # two prose statements disagree
        ((), ("Choose a 14-inch or 16-inch display.",)),  # one sentence, two sizes
        ((("Size", '14" / 16"'),), ("A 14-inch display.",)),  # already ambiguous row
    ],
)
def test_conflicting_or_ambiguous_sizes_require_review(rows, prose) -> None:
    page = dbz_page(rows, prose)
    assert primary(page)["screen_size_in"][0] == A
    result = ExactMatcher([TARGET | {"model": "SFG14-75-55MB"}]).match_observation(page)
    assert result.status is MatchStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    "sentence",
    [
        "The included sleeve fits 15.6-inch laptops.",  # accessory
        "Free backpack for laptops up to 15.6 inches.",  # accessory
        "Connect a 15.6-inch external display over HDMI.",  # external display
        "Pair it with a 16-inch portable monitor.",  # external display
        "Larger than the previous 13.3-inch model's display.",  # comparison
        "Compared with a 13.3-inch laptop, the screen feels roomy.",  # comparison
    ],
)
def test_accessory_external_and_comparison_wording_cannot_supply_size(sentence: str) -> None:
    page = dbz_page((("Size", "14.0"),), (sentence,))
    assert primary(page)["screen_size_in"] == (U, "")
    kept = [
        item for item in extract_offer_attributes(page).evidence
        if item.field == "screen_size_in" and item.status is ExtractionStatus.SUPPORTING
    ]
    # The original text and its locator stay available for review.
    assert kept and all(item.source_text == sentence for item in kept)
    assert all(item.source_label.startswith(LOCATOR) for item in kept)


def test_swift_go_bundle_price_follows_its_purchase_terms_after_its_exact_match() -> None:
    page = dbz_page((("Size", "14.0"),), ("A 14-inch display.",), bundle=True,
                    price_type=PriceType.REGULAR)
    result = ExactMatcher([TARGET | {"model": "SFG14-75-55MB"}]).match_observation(page)
    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000001")
    # "Synthetic backpack bundle" alone does not state what the price covers.
    assert price_comparability(page, result) == "NOT_COMPARABLE: PURCHASE_TERMS_UNKNOWN"
    assert price_comparability(page, result, BUNDLE_FLAG_POLICY) == "COMPARABLE"
    free = dbz_page((("Size", "14.0"),), ("A 14-inch display. Free backpack included.",),
                    bundle=True, price_type=PriceType.REGULAR)
    assert price_comparability(free, ExactMatcher(
        [TARGET | {"model": "SFG14-75-55MB"}]).match_observation(free)) == "COMPARABLE"
    assert page.details is not None and page.details.bundle_flag is True


# B. Shared CPU family and screen on PC Central


SELECTED = "50000000000002"
PICKER = "main product variant picker script[type=application/json]; select[name=id]"
VARIANTS = [
    {"id": "50000000000001", "title": "8GB DDR4 RAM / 512GB SDD",
     "options": ["8GB DDR4 RAM", "512GB SDD"]},
    {"id": SELECTED, "title": "16GB (2x8) DDR4 RAM / 512GB SDD",
     "options": ["16GB (2x8) DDR4 RAM", "512GB SDD"]},
    {"id": "50000000000003", "title": "32GB (2x16) DDR4 RAM / 512GB SDD",
     "options": ["32GB (2x16) DDR4 RAM", "512GB SDD"]},
]
MODEL_ROWS = (("Model", ""), ("B13UC-0000PH", ""))


def matrix(variants=None, *, names=("RAM", "SSD"), selected=SELECTED, label=PICKER):
    value = {
        "selected_id": selected,
        "option_names": list(names),
        "variants": VARIANTS if variants is None else variants,
    }
    return (VARIANT_DEFINITIONS, label, json.dumps(value, sort_keys=True))


def pcc(scoped, *, title: str = PCC_TITLE, description=PCC_DESCRIPTION):
    rows = (*MODEL_ROWS, *description)
    joined = " | ".join(f"{label}: {value}" if value else label for label, value in rows)
    return offer(
        "pcc", rows, title=title, configuration=f"16GB (2x8) DDR4 RAM / 512GB SDD | {joined}",
        scoped=scoped,
    )


def test_complete_ram_storage_matrix_permits_shared_cpu_and_screen() -> None:
    page = pcc((*BOUND, matrix()))
    fields, support = primary(page), supporting(page)
    assert fields["cpu_series"] == (E, "Core i5")
    assert fields["screen_size_in"] == (E, "15.6")
    assert fields["ram_capacity_gb"] == (E, "16")
    assert fields["storage_capacity_gb"] == (E, "512")
    # Shared factory-style facts stay separate from the selected installed capacity.
    assert support["ram_capacity_gb"] == ["8"]
    assert support["ram_maximum_gb"] == ["64"]
    assert support["ram_slots"] == ["2"]
    assert "SHARED_CPU_DISPLAY_RAM_STORAGE_ONLY" in notes(page)
    cpu = evidence(page, "cpu_series")
    assert f"SHARED_PRODUCT; selected variant {SELECTED}; {PICKER}" in cpu.source_label
    assert cpu.source_text == "Intel Core i5-13420H Processor"
    assert support["variant_definitions"] == [matrix()[2]]


def test_shared_model_is_inherited_only_with_the_complete_matrix() -> None:
    # Balanced policy (2026-10-15) replaces the 2026-10-13 "never inherited" rule: a full model
    # code in the shared description is inherited under the same complete RAM/storage-only
    # matrix condition as CPU family and screen.
    target = TARGET | {
        "model": "B13UC-0000PH", "screen_size_in": "15.6", "cpu_series": "Core i5",
    }
    page = pcc((*BOUND, matrix()))
    assert primary(page)["model"] == (E, "B13UC-0000PH")
    assert "SHARED_PRODUCT" in evidence(page, "model").source_label
    assert supporting(page)["model"] == ["B13UC-0000PH"]
    result = ExactMatcher([target]).match_observation(page)
    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000001")
    assert "SHARED_PRODUCT" in result.match_basis

    unproven = pcc(BOUND)
    assert primary(unproven)["model"] == (N, "")
    assert "TITLE_MODEL_SCOPE_UNPROVEN" in notes(unproven)
    result = ExactMatcher([target]).match_observation(unproven)
    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None


def _without_selected():
    return [v for v in VARIANTS if v["id"] != SELECTED]


@pytest.mark.parametrize(
    "scoped",
    [
        pytest.param(BOUND, id="missing-matrix"),
        pytest.param((*BOUND, matrix(VARIANTS[1:2])), id="single-variant-matrix"),
        pytest.param((*BOUND, matrix(_without_selected())), id="selected-variant-absent"),
        pytest.param((*BOUND, matrix(selected="50000000000001")), id="other-selected-id"),
        pytest.param((*BOUND, matrix(names=("SSD", "RAM"))), id="names-out-of-order"),
        pytest.param((*BOUND, matrix(names=("RAM",))), id="partial-option-names"),
        pytest.param(
            (*BOUND, matrix([*VARIANTS[:2], {**VARIANTS[2], "options": ["32GB DDR4 RAM"]}])),
            id="variant-missing-an-option",
        ),
        pytest.param(
            (*BOUND, matrix([*VARIANTS[:2], {**VARIANTS[2], "title": "32GB / 1TB"}])),
            id="title-disagrees-with-options",
        ),
        pytest.param(
            (*BOUND, matrix([*VARIANTS, VARIANTS[0]])), id="duplicate-variant-id",
        ),
        pytest.param(
            (
                *BOUND,
                matrix([
                    *VARIANTS[:2],
                    {"id": "50000000000003", "title": "32GB DDR4 RAM i7-13620H / 512GB SDD",
                     "options": ["32GB DDR4 RAM i7-13620H", "512GB SDD"]},
                ]),
            ),
            id="variant-varies-the-cpu",
        ),
        pytest.param(
            (
                (SELECTED_VARIANT, "binding status", "CONFIRMED"),
                (SELECTED_VARIANT, "variant id", SELECTED),
                (SELECTED_VARIANT, "Color", "Black"),
                matrix(
                    [{**v, "options": [v["options"][0].split()[0], "Black"],
                      "title": f"{v['options'][0].split()[0]} / Black"} for v in VARIANTS],
                    names=("Color",),
                ),
            ),
            id="differently-scoped-option",
        ),
        pytest.param((*BOUND, matrix(), matrix()), id="two-matrices"),
        pytest.param(
            (*BOUND, (VARIANT_DEFINITIONS, PICKER, "{not json")), id="malformed-matrix",
        ),
        pytest.param(
            (
                (SELECTED_VARIANT, "binding status", "NOT_CONFIRMED: no checked option"),
                matrix(),
            ),
            id="unconfirmed-selection",
        ),
        pytest.param(
            (
                *BOUND[:2],
                (SELECTED_VARIANT, "RAM", "8GB DDR4 RAM"),
                BOUND[3],
                matrix(),
            ),
            id="selected-options-disagree-with-matrix",
        ),
    ],
)
def test_unproven_matrices_never_establish_inheritance(scoped) -> None:
    fields = primary(pcc(scoped))
    assert fields["cpu_series"] == (N, "")
    assert fields["screen_size_in"] == (N, "")
    assert "SHARED_CPU_DISPLAY_RAM_STORAGE_ONLY" not in notes(pcc(scoped))


def test_title_cpu_conflict_blocks_only_cpu_inheritance() -> None:
    page = pcc((*BOUND, matrix()), title=PCC_TITLE.replace("i5-13420H", "i7-13620H"))
    fields = primary(page)
    assert fields["cpu_series"] == (N, "")
    assert fields["screen_size_in"] == (E, "15.6")
    assert "SHARED_PRODUCT_TITLE_DIFFERS_CPU_SERIES" in notes(page)


def test_title_display_conflict_blocks_only_screen_inheritance() -> None:
    page = pcc((*BOUND, matrix()), title=PCC_TITLE.replace('15.6"', '16"'))
    fields = primary(page)
    assert fields["screen_size_in"] == (N, "")
    assert fields["cpu_series"] == (E, "Core i5")
    assert "SHARED_PRODUCT_TITLE_DIFFERS_SCREEN_SIZE_IN" in notes(page)


def test_conflicting_shared_cpu_rows_are_never_inherited() -> None:
    description = tuple(
        ("Intel Core i5-13420H or Intel Core i7-13620H", "") if label.startswith("Intel Core")
        else (label, value)
        for label, value in PCC_DESCRIPTION
    )
    page = pcc((*BOUND, matrix()), description=description)
    assert primary(page)["cpu_series"][0] == A


def test_legacy_observations_without_scoped_evidence_stay_conservative(tmp_path: Path) -> None:
    legacy = pcc(())
    fields = primary(legacy)
    assert {name: fields[name][0] for name in ("cpu_series", "screen_size_in",
                                              "ram_capacity_gb", "storage_capacity_gb")} == {
        "cpu_series": N, "screen_size_in": N, "ram_capacity_gb": N, "storage_capacity_gb": N,
    }
    # A pcc-2 record with a binding but no matrix also stays conservative.
    assert primary(pcc(BOUND))["cpu_series"] == (N, "")
    path = tmp_path / "legacy.csv"
    columns = [c for c in (*OBSERVATION_COLUMNS, *OFFER_DETAIL_COLUMNS) if c != "scoped_evidence"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerow(legacy.to_full_record())
    [reread] = read_observations_csv(path)
    assert primary(reread.observation) == fields


# C. Research boundaries (manufacturer research, 2026-10-13)


def test_manufacturer_factory_ram_never_overwrites_selected_retailer_ram() -> None:
    factory = (PRODUCT_DESCRIPTION, "manufacturer note", "Factory configuration 8GB*1 DDR4-3200")
    page = pcc((*BOUND, matrix(), factory))
    assert primary(page)["ram_capacity_gb"] == (E, "16")
    assert "8" in supporting(page)["ram_capacity_gb"]


def test_there_is_no_manufacturer_specification_scope() -> None:
    with pytest.raises(ObservationValidationError):
        OfferDetails(scoped_evidence=(("MANUFACTURER_SPECIFICATION", "MSI PDF", "8GB"),))


def test_research_target_hints_never_supply_missing_retailer_fields() -> None:
    page = dbz_page((("Size", "14.0"),))  # no prose: screen stays unparseable
    target = TARGET | {"model": "SFG14-75-55MB"}
    result = ExactMatcher([target]).match_observation(page)
    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert primary(page)["screen_size_in"] == (U, "")
    entry = ManifestEntry(
        row_number=2,
        request_id="research-hint",
        retailer_id="datablitz",
        product_url=page.offer_url,
        expected_target_id="TGT-000001",
        enabled=True,
        request=None,  # type: ignore[arg-type]  # Never fetched in this test.
    )
    assert expected_target_check(entry, result) == "NOT_EXACT"


@pytest.mark.parametrize("model", ["A14-11M-X3BP OPI", "ANV15-42-R2UH OPI"])
def test_opi_designations_remain_unresolved(model: str) -> None:
    page = offer("datablitz", (("Model", model), ("Part Number", "NX.EXAMPLE.001"), *DBZ_ROWS))
    assert primary(page)["model"] == (R, model)
    for code in ("A14-11M-X3BP", "ANV15-42-R2UH"):
        result = ExactMatcher([TARGET | {"model": code}]).match_observation(page)
        assert result.status is not MatchStatus.EXACT


def test_octagon_title_opi_is_not_a_model() -> None:
    assert primary(offer("oct", OCT_ROWS, title=OCT_TITLE))["model"] == (M, "")


def test_retailer_skus_never_become_manufacturer_models() -> None:
    scoped = (
        (IDENTIFIER, "JSON-LD Product block 1 sku", "LT-ASUS-000261"),
        (IDENTIFIER, "JSON-LD Product block 1 mpn", "LT-ASUS-000261"),
        (IDENTIFIER, "JSON-LD Product.gtin13", "4711474944863"),
    )
    page = offer("pcx", PCX_ROWS, scoped=scoped)
    fields = primary(page)
    assert fields["model"] == (M, "")
    assert "manufacturer_part_number" not in fields
    assert "MPN_REPEATS_RETAILER_SKU" in notes(page)


def test_bare_storage_and_throughput_stay_insufficient_even_with_a_barcode_lead() -> None:
    lead = (IDENTIFIER, "JSON-LD Product.gtin13", "4711474944863")
    prose = (PRODUCT_DESCRIPTION, "description", "Another store lists 512GB NVMe SSD.")
    page = offer("oct", OCT_ROWS, title=OCT_TITLE, scoped=(lead, prose))
    fields, support = primary(page), supporting(page)
    assert fields["storage_capacity_gb"] == (U, "")
    assert support["storage_amount_without_unit"] == ["512"]
    assert support["storage_throughput"] == ["16 Gb/s"]
    assert "STORAGE_CAPACITY_UNIT_MISSING" in notes(page)
