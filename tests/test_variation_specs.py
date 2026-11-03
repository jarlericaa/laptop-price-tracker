"""The selected variation's own specifications and real disagreement with its target (2026-11-03).

Synthetic text only. The selected variation is read first; a multi-option parent applies to no one
variation; unknown stays unknown; only plainly stated values are compared, at CPU-series level.
"""

from __future__ import annotations

import pytest

from laptop_tracker.variation_specs import (
    AMBIGUOUS,
    PARENT,
    UNKNOWN,
    VARIATION,
    read_variation_specs,
    spec_conflicts,
)

MULTI_OPTION_PARENT = "Hp Laptop | Intel i7/i5/i3 & Celeron 4GB-16GB RAM 128GB-512GB SSD"


def target(**values: str) -> dict[str, str]:
    base = {
        "cpu_series": "Core i5",
        "screen_size_in": "15.6",
        "ram_capacity_gb": "8",
        "storage_capacity_gb": "256",
    }
    return base | values


def test_the_selected_variation_is_read_before_a_multi_option_parent() -> None:
    readings = read_variation_specs(MULTI_OPTION_PARENT, "5:I3-7GEN/4GB/128GB")

    assert readings["cpu_series"].value == "Core i3"
    assert readings["cpu_series"].source == VARIATION
    assert readings["cpu_series"].evidence == "I3"
    found = spec_conflicts(readings, target())
    assert [(c.field, c.variation_value, c.target_value, c.source, c.evidence) for c in found] == [
        ("cpu_series", "Core i3", "Core i5", VARIATION, "I3")
    ]
    assert "variation states 'Core i3'" in found[0].describe()


def test_a_multi_option_parent_is_not_a_conflict_and_its_first_cpu_is_never_taken() -> None:
    parent = "Lenovo ThinkPad Intel Core i7 - i5 - i3 | 16GB / 8GB / 4GB Memory"
    readings = read_variation_specs(parent, "Black")

    assert readings["cpu_series"].value == "" and readings["cpu_series"].source == AMBIGUOUS
    assert readings["ram_capacity_gb"].source in {AMBIGUOUS, UNKNOWN}
    assert spec_conflicts(readings, target()) == []
    assert spec_conflicts(readings, target(cpu_series="Core i7")) == []


def test_a_parent_that_states_one_value_applies_only_when_the_variation_is_silent() -> None:
    parent = 'ASUS Vivobook 15.6" Ryzen 5 7520U 16GB RAM 512GB SSD'

    silent = read_variation_specs(parent, "Silver")
    assert silent["cpu_series"].source == PARENT and silent["cpu_series"].value == "Ryzen 5"
    matching = target(
        cpu_series="Ryzen 5", ram_capacity_gb="16", storage_capacity_gb="512", screen_size_in="15.6"
    )
    assert spec_conflicts(silent, matching) == []
    found = spec_conflicts(silent, {**matching, "cpu_series": "Ryzen 7"})
    assert [(c.field, c.source) for c in found] == [("cpu_series", PARENT)]

    # The variation's own statement takes precedence over the parent's.
    own = read_variation_specs(parent, "Ryzen 7 7735U, 32GB RAM")
    assert own["cpu_series"].value == "Ryzen 7" and own["cpu_series"].source == VARIATION
    assert own["ram_capacity_gb"].value == "32" and own["ram_capacity_gb"].source == VARIATION


def test_a_variation_that_speaks_in_an_unread_form_blocks_the_parent_for_that_field() -> None:
    # "16GB(8x2)" and "R5" are statements the reader does not parse; the parent's single 8GB RAM
    # and Ryzen 7 do not apply to this variation, so nothing conflicts.
    parent = "MSI Thin 15 Gaming Laptop 8GB RAM Ryzen 7 8745H"
    readings = read_variation_specs(parent, "B: R5 16GB(8x2)")

    assert readings["ram_capacity_gb"].source == AMBIGUOUS
    assert readings["cpu_series"].source == AMBIGUOUS
    assert spec_conflicts(readings, target(cpu_series="Ryzen 5", ram_capacity_gb="16")) == []


def test_unknown_specifications_stay_unknown_and_are_never_conflicts() -> None:
    readings = read_variation_specs("Synthetic listing", "Midnight")

    assert {r.source for r in readings.values()} == {UNKNOWN}
    assert all(r.value == "" for r in readings.values())
    assert spec_conflicts(readings, target()) == []
    assert (
        spec_conflicts(read_variation_specs("x", "Core i5, 8GB RAM"), target(ram_capacity_gb=""))
        == []
    )


def test_ranges_and_option_lists_are_not_read_as_one_value() -> None:
    readings = read_variation_specs("Laptop 4GB-16GB RAM 128GB-512GB SSD", "Standard")

    assert readings["ram_capacity_gb"].value == "" and readings["storage_capacity_gb"].value == ""
    assert spec_conflicts(readings, target(ram_capacity_gb="8", storage_capacity_gb="256")) == []


def test_storage_terabytes_and_screen_class_do_not_create_false_conflicts() -> None:
    readings = read_variation_specs("Laptop", '1TB SSD, 15" display')

    assert readings["storage_capacity_gb"].value == "1000"
    assert spec_conflicts(readings, target(storage_capacity_gb="1000", screen_size_in="15.6")) == []
    found = spec_conflicts(readings, target(storage_capacity_gb="512", screen_size_in="13.3"))
    assert {c.field for c in found} == {"storage_capacity_gb", "screen_size_in"}


def test_cpu_comparison_is_series_level_and_never_requires_an_exact_cpu_sku() -> None:
    readings = read_variation_specs("Laptop", "Core i5-1135G7")

    assert readings["cpu_series"].value == "Core i5"
    # A target recording a different SKU of the same series is not a conflict.
    assert spec_conflicts(readings, {**target(), "cpu_model": "i5-1235U"}) == []


@pytest.mark.parametrize("variation", ["", "   ", None])
def test_missing_variation_text_reads_as_unknown(variation) -> None:
    readings = read_variation_specs("", variation)

    assert {r.source for r in readings.values()} == {UNKNOWN}
