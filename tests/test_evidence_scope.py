"""Evidence scope (2026-10-19): applicability is resolved before any value is compared.

Every page and value is synthetic. Family capabilities, maximums, and optional or alternative
configurations keep their raw wording, locator, and scope, and never stand for the installed
configuration. Genuine conflicts within one scope still require review.
"""

from __future__ import annotations

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction_layouts import TARGET, notes, offer, primary
from test_owner_policy_rules import BOUND, matrix, pcc

from laptop_tracker.extraction import (
    SCOPE_FAMILY_CAPABILITY,
    SCOPE_MAXIMUM,
    SCOPE_OPTIONAL,
    SCOPE_PRODUCT_SPECIFICATION,
    SCOPE_SELECTED_VARIANT,
    SCOPE_SHARED_PRODUCT,
    SCOPE_TITLE,
    ExtractionStatus,
    extract_offer_attributes,
)
from laptop_tracker.matching import ExactMatcher, MatchStatus

pytestmark = pytest.mark.usefixtures("no_network")

E, A, U = "EXTRACTED", "AMBIGUOUS", "UNPARSEABLE"

ROWS = (
    ("Model", "A14-11M-X3BP"),
    ("Display", '14" WUXGA'),
    ("CPU", "Snapdragon X X1-26-100"),
    ("Memory", "16GB LPDDR5X"),
    ("Storage", "512GB SSD"),
)


def page(**replace_rows: str):
    rows = tuple((label, replace_rows.get(label, value)) for label, value in ROWS)
    return offer("datablitz", rows)


def scoped_items(observation, field: str) -> list[tuple[str, str, str]]:
    return [
        (item.status.value, item.value, item.scope)
        for item in extract_offer_attributes(observation).evidence
        if item.field == field
    ]


# Family capability versus the installed processor


@pytest.mark.parametrize(
    ("cpu", "capability"),
    [
        ("Intel Core i5-1335U (up to Core i7)", "up to Core i7"),
        ("Intel Core i5-1335U / up to Intel Core i7-1355U", "up to Intel Core i7-1355U"),
        ("Intel Core i5-1335U; configurable to Core i7", "configurable to Core i7"),
        ("Intel Core i5-1335U, options up to Intel Core i7", "options up to Intel Core i7"),
    ],
)
def test_family_capability_never_conflicts_with_the_installed_processor(cpu, capability) -> None:
    observation = page(CPU=cpu)
    assert primary(observation)["cpu_series"] == (E, "Core i5")
    items = scoped_items(observation, "cpu_series")
    assert ("SUPPORTING", capability, SCOPE_FAMILY_CAPABILITY) in items
    # The installed value keeps the complete raw wording as its source text.
    installed = next(
        item for item in extract_offer_attributes(observation).evidence
        if item.field == "cpu_series" and item.status is ExtractionStatus.EXTRACTED
    )
    assert (installed.source_text, installed.scope) == (cpu, SCOPE_PRODUCT_SPECIFICATION)


@pytest.mark.parametrize(
    "cpu",
    [
        "Intel Core i5-13420H / i7-13620H",  # an alternative, not a capability
        "Intel Core i5-1335U or Core i7-1355U",
        "Intel Core i5-1335U (Core i7)",
    ],
)
def test_processor_alternatives_without_capability_wording_stay_ambiguous(cpu) -> None:
    assert primary(page(CPU=cpu))["cpu_series"][0] == A


def test_a_title_capability_clause_is_not_a_title_conflict() -> None:
    observation = offer(
        "pcx",
        (("Display", '16" WUXGA'), ("Processor", "Intel Core i5-13420H"),
         ("Memory", "16GB"), ("Storage", "512GB SSD")),
        title="ASUS Vivobook S16 S3607VA-RP181WSM Core i5 (upgradable to Core i7) Laptop",
        url="https://pcx.com.ph/products/asus-vivobook-s16",
    )
    assert not [code for code in notes(observation) if code.startswith("TITLE_SPEC_CONFLICT")]
    assert primary(observation)["model"] == (E, "S3607VA-RP181WSM")


# Maximum and optional capacities versus installed capacities


@pytest.mark.parametrize(
    ("label", "value", "field", "installed", "kept_field", "kept_value", "scope"),
    [
        ("Memory", "16GB LPDDR5X (upgradable up to 32GB)", "ram_capacity_gb", "16",
         "ram_maximum_gb", "32", SCOPE_MAXIMUM),
        ("Memory", "8GB, expandable to 32GB", "ram_capacity_gb", "8",
         "ram_maximum_gb", "32", SCOPE_MAXIMUM),
        ("Memory", "16GB; Optional 32GB", "ram_capacity_gb", "16",
         "ram_optional_gb", "32", SCOPE_OPTIONAL),
        ("Storage", "512GB SSD | Up to 2TB", "storage_capacity_gb", "512",
         "storage_maximum_gb", "2000", SCOPE_MAXIMUM),
        ("Storage", "512GB PCIe SSD (expandable up to 1TB)", "storage_capacity_gb", "512",
         "storage_maximum_gb", "1000", SCOPE_MAXIMUM),
        ("Storage", "512GB SSD; Optional 1TB HDD", "storage_capacity_gb", "512",
         "storage_optional_gb", "1000", SCOPE_OPTIONAL),
    ],
)
def test_maximum_and_optional_capacities_never_replace_installed_capacity(
    label, value, field, installed, kept_field, kept_value, scope
) -> None:
    observation = page(**{label: value})
    assert primary(observation)[field] == (E, installed)
    assert ("SUPPORTING", kept_value, scope) in scoped_items(observation, kept_field)
    result = ExactMatcher([TARGET | {field: installed}]).match_observation(observation)
    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000001")


@pytest.mark.parametrize(
    ("label", "value", "expected"),
    [
        ("Memory", "Up to 32GB", (U, "")),  # a maximum alone states no installed amount
        ("Storage", "Up to 2TB", (U, "")),
        # "(optional)" states no amount of its own, so the second drive is not split off.
        ("Storage", "512GB SSD + 1TB HDD (optional)", (A, "512; 1000")),
        ("Storage", "512GB SSD + optional 1TB HDD", (A, "512; 1000")),
        # A limit inside a component breakdown is not separable.
        ("Memory", "16GB (8GB + 8GB up to 32GB)", (A, "16; 8; 8; 32")),
    ],
)
def test_inseparable_or_missing_installed_capacity_stays_in_review(label, value, expected):
    field = "ram_capacity_gb" if label == "Memory" else "storage_capacity_gb"
    assert primary(page(**{label: value}))[field] == expected


def test_repeated_agreeing_rows_are_one_fact_never_a_sum_or_two_confirmations() -> None:
    observation = offer(
        "datablitz", (*ROWS, ("Total System Memory", "16GB LPDDR5X")),
    )
    [installed] = [
        item for item in extract_offer_attributes(observation).evidence
        if item.field == "ram_capacity_gb" and item.status is ExtractionStatus.EXTRACTED
    ]
    assert installed.value == "16"
    assert "EQUIVALENT_REPEATED_FACTS_DEDUPLICATED" in notes(observation)


# Selected variant, shared product, and title scopes


def test_selected_variant_supersedes_the_shared_default_and_keeps_the_discrepancy() -> None:
    observation = pcc((*BOUND, matrix()))
    evidence = extract_offer_attributes(observation).evidence
    ram = [(i.status.value, i.value, i.scope) for i in evidence if i.field == "ram_capacity_gb"]
    assert (E, "16", SCOPE_SELECTED_VARIANT) in ram
    # The shared description's default amount is kept, scoped, and reported.
    assert ("SUPPORTING", "8", SCOPE_SHARED_PRODUCT) in ram
    assert "SHARED_DESCRIPTION_DIFFERS_RAM_CAPACITY_GB" in notes(observation)
    cpu = next(i for i in evidence if i.field == "cpu_series" and i.status.value == E)
    assert cpu.scope == SCOPE_SHARED_PRODUCT
    maximum = next(i for i in evidence if i.field == "ram_maximum_gb")
    assert maximum.scope == SCOPE_MAXIMUM


def test_shared_fields_without_a_proven_constant_matrix_do_not_apply() -> None:
    observation = pcc(BOUND)
    fields = primary(observation)
    assert fields["cpu_series"][0] == "NOT_EXTRACTED"
    assert fields["ram_capacity_gb"] == (E, "16")  # the selected variant still applies


def test_title_corroborated_model_is_scoped_as_title() -> None:
    observation = offer(
        "pcx",
        (("Display", '16" WUXGA'), ("Processor", "Intel Core i5-13420H"),
         ("Memory", "16GB"), ("Storage", "512GB SSD")),
        title="ASUS Vivobook S16 S3607VA-RP181WSM Laptop",
    )
    model = next(
        item for item in extract_offer_attributes(observation).evidence
        if item.field == "model" and item.status is ExtractionStatus.EXTRACTED
    )
    assert model.scope == SCOPE_TITLE


# Model-name rows never drop an unexplained designation (2026-10-19)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Acer Aspire AI 14 A14-11M-X3BP OPI",
         ("UNRESOLVED", "Acer Aspire AI 14 A14-11M-X3BP OPI")),
        ("Acer Aspire AI 14 A14-11M-X3BP (Steel Gray)", (E, "A14-11M-X3BP")),
        ("ASUS Vivobook S16 S3607VA-RP181WSM OLED", (E, "S3607VA-RP181WSM")),
        ("Acer Nitro V 15 ANV15-42-R2UH", (E, "ANV15-42-R2UH")),
    ],
)
def test_model_name_rows_keep_unexplained_suffixes_unresolved(value, expected) -> None:
    observation = offer("laptop_factory", (("Model Name", value),))
    assert primary(observation)["model"] == expected
