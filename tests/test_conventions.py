"""Model-code and CPU-series conventions (approved 2026-10-08). Synthetic values only."""

from __future__ import annotations

import pytest

from laptop_tracker.conventions import (
    apple_chip_form,
    cpu_series_forms,
    is_canonical_cpu_series,
    is_model_code,
    model_codes,
)


@pytest.mark.parametrize(
    ("text", "codes"),
    [
        # Code shapes seen in reviewed retailer model labels and catalog codes (synthetic here).
        ("ExampleBrand Example 15 EXV15-42-R2UH", ["EXV15-42-R2UH"]),
        ("Example AI 14 E14-11M-X3BP (Steam Blue)", ["E14-11M-X3BP"]),
        ("Example S16 S3607VA-RP181WSM", ["S3607VA-RP181WSM"]),  # suffix kept
        # Segments ending in MB or W are code segments, not units.
        ("Example Go EXG14-75-55MB", ["EXG14-75-55MB"]),
        ("Example A16 EX607NUQ-RL011W", ["EX607NUQ-RL011W"]),
        ("Example Laptop 15-fc0413au", ["15-fc0413au"]),  # case kept
        ("EXV15-42-R2UH", ["EXV15-42-R2UH"]),
        ("EXV15-42-R2UH exv15-42-r2uh", ["EXV15-42-R2UH"]),  # same code twice
        ("A1-B2 / EXV15-42-R2UH / EXV16-71-524U", ["EXV15-42-R2UH", "EXV16-71-524U"]),
        # Not codes: names, short fragments, part and machine-type numbers, en dashes.
        ("Example MacBook Air", []),
        ("Example Megabook K15", []),
        ("Example Flip 2-in-1 14", []),
        ("Example core 5-120U, Ryzen 7-170", []),
        ("Example NH.QUCSP.001 C4FW3PA 83K0007XPH", []),
        ("Example EXV15–42–R2UH", []),
        ("Wi-Fi USB-C", []),
        # Processor, memory, and graphics numbers are never model codes.
        ("i5-13420H R5-5500U X1-26-100 X1E-78-100 DDR5-4800 LPDDR5X-7500", []),
        ("GTX1650-1660ti120Hz RTX-4050 RTX4050-6GB", []),
        # A segment with a unit is a specification, not a code.
        ("16GB-512GB 15.6in-FHD 120Hz-IPS EXV15-42-R2UH", ["EXV15-42-R2UH"]),
    ],
)
def test_model_codes(text: str, codes: list[str]) -> None:
    assert model_codes(text) == codes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("EXV15-42-R2UH", True),
        (" EXV15-42-R2UH ", True),
        ("Example EXV15-42-R2UH", False),
        ("ThinkPad X390", False),
        ("", False),
    ],
)
def test_is_model_code(value: str, expected: bool) -> None:
    assert is_model_code(value) is expected


@pytest.mark.parametrize(
    ("text", "forms"),
    [
        # Existing family conventions are unchanged.
        ("Intel® Core™ i5-13420H", {"Core i5"}),
        ("Intel Core Ultra 5 226V", {"Core Ultra 5"}),
        ("Intel Core 5 210H", {"Core 5"}),
        ("AMD Ryzen 7 170", {"Ryzen 7"}),
        ("AMD Ryzen AI 5 340", {"Ryzen AI 5"}),
        ("Qualcomm®Snapdragon X, X1-26-100", {"Snapdragon X"}),
        ("Intel Core i3-N305", {"Core i3"}),
        # Apple chips keep the brand and their tier.
        ("Apple M5 chip", {"Apple M5"}),
        ("Apple M4 Max chip", {"Apple M4 Max"}),
        ("Apple A18 Pro chip", {"Apple A18 Pro"}),
        ("Apple A18 chip", {"Apple A18"}),
        # Untiered Intel lines keep the processor number; distinct processors stay distinct.
        ("Intel® Processor N150", {"Intel N150"}),
        ("Intel N100", {"Intel N100"}),
        ("Intel Celeron N4020", {"Celeron N4020"}),
        ("Intel Celeron n4500", {"Celeron N4500"}),
        # Not supported: no brand on an Apple chip, no processor number, bare model numbers.
        ("M5 chip", set()),
        ("A18 Pro", set()),
        ("Intel Celeron processor", set()),
        ("Intel Pentium Silver N5030", set()),
        # A complete Intel Core processor number is enough; a bare tier or other SKU is not.
        ("Raptor Lake i5-13420H", {"Core i5"}),
        ("i3-1115G4", {"Core i3"}),
        ("Intel i5", set()),
        ("i3-N305", set()),
        ("Inteli5-13450HX", set()),
        ("N150", set()),
        # Distinct forms are reported separately; callers treat more than one as ambiguous.
        ("Intel N100 or Intel N150", {"Intel N100", "Intel N150"}),
        ("i5-13420H or i7-13620H", {"Core i5", "Core i7"}),
        # Processor numbers in one tiered family give one family form (match by family).
        ("i5-13420H or i5-1335U", {"Core i5"}),
    ],
)
def test_cpu_series_forms(text: str, forms: set[str]) -> None:
    assert cpu_series_forms(text) == forms


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Core i5", True),
        ("Apple M5 Pro", True),
        ("Intel N150", True),
        ("Celeron N4020", True),
        ("M5", False),
        ("A18 Pro", False),
        ("Intel Core i5", False),
        ("core i5", False),
    ],
)
def test_is_canonical_cpu_series(value: str, expected: bool) -> None:
    assert is_canonical_cpu_series(value) is expected


@pytest.mark.parametrize(
    ("value", "form"),
    [
        ("M5", "Apple M5"),
        ("M5 Pro", "Apple M5 Pro"),
        ("M4 Max", "Apple M4 Max"),
        ("A18 Pro", "Apple A18 Pro"),
        ("Apple M5", None),  # already prefixed: nothing to add
        ("Core i5", None),
        ("M5 Extreme", None),
        ("N150", None),
    ],
)
def test_apple_chip_form(value: str, form: str | None) -> None:
    assert apple_chip_form(value) == form
