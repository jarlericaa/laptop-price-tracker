"""Bound colour/storage options: accept evidence, preserve hardware boundaries."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pandas as pd
import pytest
from test_extraction_layouts import offer

from laptop_tracker.extraction import extract_offer_attributes
from laptop_tracker.matching import ExactMatcher, MatchStatus, match_basis_from_codes
from laptop_tracker.offers import SELECTED_VARIANT, VARIANT_DEFINITIONS, PriceType
from laptop_tracker.replay import price_comparability

OPTIONS = ["McbookNeo INDIGO 256GB", "McbookNeo Silver 256GB", "McbookNeo BLUSH 256GB",
           "McbookNeo CITRUS 256GB", "McbookNeo SILVER 512GB", "McbookNeo Citrus 512GB"]
TITLE = 'Apple MacBook Neo | Apple A18 Pro Chip | 13" Retina IPS Display | 8GB Unified Memory'
ROWS = (("Chip", "Apple A18 Pro (6-core CPU)."), ("Memory", "8GB Unified Memory."),
        ("Storage", "256GB & 512gbSSD."), ("Display", '13-inch Liquid Retina display.'))


def page(selected=0, *, matrix_change=None, rows=ROWS, title=TITLE, binding="CONFIRMED"):
    variants = [{"id": str(50000000000000 + i), "title": value, "options": [value]}
                for i, value in enumerate(OPTIONS)]
    matrix = {"selected_id": variants[selected]["id"], "option_names": ["MACBOOK NEO"],
              "variants": copy.deepcopy(variants)}
    if matrix_change:
        matrix_change(matrix)
    scoped = ((SELECTED_VARIANT, "binding status", binding),
              (SELECTED_VARIANT, "variant id", variants[selected]["id"]),
              (SELECTED_VARIANT, "MACBOOK NEO", OPTIONS[selected]),
              (VARIANT_DEFINITIONS, "complete main-product picker", json.dumps(matrix)))
    joined = " | ".join(f"{label}: {value}" for label, value in rows)
    observation = offer("pcc", rows, title=title,
                        configuration=OPTIONS[selected] + " | " + joined,
                        url=("https://pccentralcomputerstore.com/products/example-neo?variant="
                             + variants[selected]["id"]),
                        scoped=scoped, bundle=False, price_type=PriceType.REGULAR)
    return replace(observation, details=replace(observation.details, brand="Apple"))


def matcher(**overrides):
    base = {"status": "ACTIVE", "brand": "Apple", "model": "MacBook Neo",
            "screen_size_in": "13", "cpu_series": "Apple A18 Pro", "ram_capacity_gb": "8"}
    return ExactMatcher(pd.DataFrame([
        {**base, "target_id": "T-256", "storage_capacity_gb": "256", **overrides},
        {**base, "target_id": "T-512", "storage_capacity_gb": "512", **overrides},
    ]))


@pytest.mark.parametrize("selected", range(6))
def test_every_confirmed_colour_keeps_its_own_storage_target_and_price(selected):
    observation = page(selected)
    result = matcher().match_observation(observation)
    assert result.status is MatchStatus.EXACT
    assert result.target_id == ("T-256" if selected < 4 else "T-512")
    assert price_comparability(observation, result) == "COMPARABLE"
    assert "SHARED_FIXED_RAM_COLOR_STORAGE_ONLY" in result.reason_codes
    assert "SELECTED_VARIANT+SHARED_PRODUCT" in match_basis_from_codes(result.reason_codes)
    extracted = extract_offer_attributes(observation)
    assert extracted.values["ram_capacity_gb"] == "8"
    storage = next(e for e in extracted.evidence if e.field == "storage_capacity_gb")
    assert storage.source_text == OPTIONS[selected] and storage.scope == "SELECTED_VARIANT"


@pytest.mark.parametrize("change", [
    lambda m: m.update(selected_id="different"),
    lambda m: m["variants"].pop(0),
    lambda m: m["variants"][1].update(id=m["variants"][0]["id"]),
    lambda m: m["variants"][1].update(title="Different title"),
    lambda m: m["variants"][1].update(options=["McbookNeo Silver 1024GB"],
                                      title="McbookNeo Silver 1024GB"),
    lambda m: m["variants"][1].update(options=["DifferentModel Silver 256GB"],
                                      title="DifferentModel Silver 256GB"),
    lambda m: m["variants"][1].update(options=["McbookNeo Silver 8GB"],
                                      title="McbookNeo Silver 8GB"),
    lambda m: m["variants"][1].update(options=["McbookNeo Silver 256GB RTX4060"],
                                      title="McbookNeo Silver 256GB RTX4060"),
    lambda m: m.update(variants=m["variants"][:1]),
])
def test_partial_conflicting_or_unexplained_matrix_never_fills_missing_hardware(change):
    result = matcher().match_observation(page(matrix_change=change))
    assert result.status is MatchStatus.REVIEW_REQUIRED and result.target_id is None


@pytest.mark.parametrize("storage", ["Up to 512GB SSD", "256GB SSD", "Optional 512GB SSD"])
def test_shared_storage_must_explicitly_support_all_picker_capacities(storage):
    rows = tuple((label, storage if label == "Storage" else value) for label, value in ROWS)
    assert matcher().match_observation(page(rows=rows)).status is MatchStatus.REVIEW_REQUIRED


def test_unconfirmed_selection_cannot_inherit_shared_fields():
    assert matcher().match_observation(page(binding="NOT_CONFIRMED: mismatch")).status is (
        MatchStatus.REVIEW_REQUIRED)


def test_extra_ram_requirement_is_not_forgiven_as_a_cosmetic_difference():
    result = matcher(ram_capacity_gb="16").match_observation(page())
    assert result.status is MatchStatus.UNMATCHED and result.target_id is None


def test_title_cpu_conflict_remains_unresolved():
    result = matcher().match_observation(page(title=TITLE.replace("A18 Pro", "M5")))
    assert result.status is MatchStatus.REVIEW_REQUIRED and result.target_id is None


def test_missing_shared_ram_is_never_taken_from_the_target_or_parent_title():
    rows = tuple(row for row in ROWS if row[0] != "Memory")
    assert matcher().match_observation(page(rows=rows)).status is MatchStatus.REVIEW_REQUIRED
