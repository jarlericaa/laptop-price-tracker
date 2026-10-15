"""Balanced matching policy (2026-10-15): positive and negative boundaries.

Every page and value is synthetic. The shapes follow the saved 2026-10-13 and 2026-10-15 pilot
pages (title-only model codes, ASUS part numbers, repeated rows, comma maximum clauses, R2UH vs
R2AUH URLs, out-of-stock prices), but no real page content is stored here.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_extraction_layouts import notes, offer, primary, supporting
from test_oct import fixture as oct_fixture
from test_oct import mutate as oct_mutate
from test_oct import only_offer as oct_offer
from test_pcc import SINGLE
from test_pcc import fixture as pcc_fixture
from test_pcc import mutate as pcc_mutate
from test_pcc import only_offer as pcc_offer

from laptop_tracker.conventions import is_part_number_shape, model_codes
from laptop_tracker.extraction import (
    ExtractionStatus,
    _screen_size_row,
    extract_offer_attributes,
)
from laptop_tracker.matching import (
    DIFFERENT_CONFIGURATION,
    IDENTITY_CONFLICT,
    MODEL_UNVERIFIED,
    EvidenceStatus,
    ExactMatcher,
    MatchStatus,
    match_basis_from_codes,
    offer_outcome,
)
from laptop_tracker.observation_store import observation_id
from laptop_tracker.offers import IDENTIFIER, Availability, PriceType
from laptop_tracker.pilot_coverage import (
    CANDIDATE_NOT_FETCHED,
    DISCOVERY_NOT_ATTEMPTED,
    EXACT_PRICE_EXCLUDED,
    FETCH_FAILED,
    NO_CANDIDATE_FOUND,
    PRICE_AVAILABLE,
    UNKNOWN_COVERAGE,
    OfferRecord,
    coverage,
)
from laptop_tracker.replay import price_comparability

pytestmark = pytest.mark.usefixtures("no_network")

E, M, A, U, N, R = (
    "EXTRACTED", "MISSING", "AMBIGUOUS", "UNPARSEABLE", "NOT_EXTRACTED", "UNRESOLVED",
)

VIVO = {
    "target_id": "TGT-000031",
    "status": "ACTIVE",
    "model": "S3607VA-RP181WSM",
    "screen_size_in": "16",
    "cpu_series": "Core 5",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "512",
}
# Four independently labeled fields, in PC Express layout; no model row.
PCX_SPECS = (
    ("Processor", "Intel Core 5-210H"),
    ("Display", '16" WUXGA 144Hz'),
    ("Memory", "16GB DDR5"),
    ("Storage", "512GB M.2 PCIe NVMe SSD"),
)
VIVO_URL = "https://pcx.com.ph/products/asus-vivobook-s16-s3607va-rp181wsm-intel-core-5"


def pcx(title: str, specs=PCX_SPECS, *, url: str = VIVO_URL, scoped=()):
    return offer("pcx", specs, title=title, url=url, scoped=scoped)


def match(page, *targets):
    return ExactMatcher(list(targets or (VIVO,))).match_observation(page)


def model_evidence(page):
    return next(
        item for item in extract_offer_attributes(page).evidence
        if item.field == "model" and item.status is not ExtractionStatus.SUPPORTING
    )


# B. Title model corroboration


def test_one_full_title_code_with_four_labeled_fields_matches_with_provenance() -> None:
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM (90NB1672-M00DD0) Intel Core 5 Laptop")
    result = match(page)

    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000031")
    assert result.match_basis == "TITLE_MODEL_CORROBORATED"
    assert "TITLE_MODEL_CORROBORATED" in result.reason_codes
    model = model_evidence(page)
    assert model.source_text == page.raw_title
    for field, label in (("screen_size_in", "Display"), ("cpu_series", "Processor"),
                         ("ram_capacity_gb", "Memory"), ("storage_capacity_gb", "Storage")):
        assert f"{field}: {label}" in model.source_label
    # The ASUS part number is recorded as such, not as a second model code.
    assert supporting(page)["part_number_candidate"] == ["90NB1672-M00DD0"]


def test_all_five_values_only_in_the_title_go_to_review() -> None:
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM | 16\" | Core 5 | 16GB RAM | 512GB SSD",
               specs=())
    result = match(page)
    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.target_id is None
    assert primary(page)["model"] == (M, "")


@pytest.mark.parametrize(
    ("title", "url", "note"),
    [
        # Two different model codes in one heading.
        ("ASUS S3607VA-RP181WSM / S3607VA-RP999WSM Laptop", VIVO_URL,
         "TITLE_MULTIPLE_MODEL_CODES"),
        # An unexplained designation after the code is never stripped.
        ("ASUS Vivobook S3607VA-RP181WSM OPI Laptop", VIVO_URL, "TITLE_MODEL_UNEXPLAINED_SUFFIX"),
        # The product URL names the same stem with another final segment.
        ("ASUS Vivobook S3607VA-RP181WSM Laptop",
         "https://pcx.com.ph/products/asus-vivobook-s3607va-rp181awsm-laptop",
         "MODEL_URL_CONFLICT"),
    ],
)
def test_unsafe_title_codes_are_not_used(title: str, url: str, note: str) -> None:
    page = pcx(title, url=url)
    assert note in notes(page)
    assert primary(page)["model"] == (M, "")
    result = match(page)
    assert result.status is MatchStatus.REVIEW_REQUIRED and result.target_id is None


@pytest.mark.parametrize(
    ("offer_code", "target_code"),
    [("ANV15-42-R2UH", "ANV15-42-R2AUH"), ("ANV15-42-R2AUH", "ANV15-42-R2UH"),
     ("S3607VA-RP181WSM", "S3607VA"), ("S3607VA-RP181WSM", "S3607VA-RP181")],
)
def test_a_different_suffix_or_a_substring_never_matches(offer_code: str, target_code: str) -> None:
    page = pcx(f"Example {offer_code} Laptop", url="https://pcx.com.ph/products/example")
    result = match(page, VIVO | {"model": target_code})
    assert result.status is MatchStatus.UNMATCHED
    assert result.target_id is None
    assert offer_outcome(result.status, result.reason_codes) == DIFFERENT_CONFIGURATION


@pytest.mark.parametrize(
    ("claim", "outcome"),
    [
        ("FA506NC", "MODEL_CLAIM_CONFLICT"),  # a different designation
        ("S3607VA-RP999WSM", "MODEL_TITLE_CONFLICT"),  # a different full code in a trusted row
    ],
)
def test_a_conflicting_page_model_claim_requires_review(claim: str, outcome: str) -> None:
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM Laptop", specs=(("Model", claim), *PCX_SPECS))
    assert outcome in notes(page)
    result = match(page)
    assert result.status is MatchStatus.REVIEW_REQUIRED and result.target_id is None
    assert offer_outcome(result.status, result.reason_codes) == IDENTITY_CONFLICT


@pytest.mark.parametrize("claim", ["S3607VA", "ASUS Vivobook S16", "Vivobook S16 S3607VA"])
def test_a_less_specific_page_model_claim_does_not_block(claim: str) -> None:
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM Laptop", specs=(("Model", claim), *PCX_SPECS))
    result = match(page)
    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000031")
    assert claim in supporting(page)["model"]


def test_a_title_spec_contradiction_blocks_the_title_route() -> None:
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM | 8GB RAM | 512GB SSD")
    assert "TITLE_SPEC_CONFLICT_RAM_CAPACITY_GB" in notes(page)
    assert match(page).status is MatchStatus.REVIEW_REQUIRED


def test_narrow_normalization_reads_unicode_hyphens_but_keeps_spacing() -> None:
    assert model_codes("S3607VA‑RP181WSM") == ["S3607VA-RP181WSM"]
    assert model_codes("S3607VA RP181WSM") == []  # internal spaces are never removed
    assert model_codes("S3607VA–RP181WSM") == []  # an en dash is other punctuation
    assert is_part_number_shape("90NR0QE7-M001U0") and not is_part_number_shape("FA506NCQ-HN012W")


# Identifiers


def test_an_mpn_repeating_the_retailer_sku_is_a_warning_only() -> None:
    scoped = (
        (IDENTIFIER, "JSON-LD Product block 1 sku", "LT-ASUS-000261"),
        (IDENTIFIER, "JSON-LD Product block 1 mpn", "LT-ASUS-000261"),
    )
    result = match(pcx("ASUS Vivobook S16 S3607VA-RP181WSM Laptop", scoped=scoped))
    assert (result.status, result.target_id) == (MatchStatus.EXACT, "TGT-000031")
    assert "MPN_REPEATS_RETAILER_SKU" in result.warnings


def test_a_genuine_conflicting_manufacturer_code_requires_review() -> None:
    scoped = (
        (IDENTIFIER, "JSON-LD Product block 1 sku", "LT-ASUS-000261"),
        (IDENTIFIER, "JSON-LD Product block 1 mpn", "S3607VA-RP999WSM"),
    )
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM Laptop", scoped=scoped)
    assert "MODEL_IDENTIFIER_CONFLICT" in notes(page)
    assert "MPN_REPEATS_RETAILER_SKU" not in notes(page)
    assert match(page).status is MatchStatus.REVIEW_REQUIRED


# D. Field rules


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("i5", (E, "Core i5")),
        ("Intel i7 12th Gen", (E, "Core i7")),
        ("Intel Core 5-210H", (E, "Core 5")),
        ("Intel Core Ultra 5 226V", (E, "Core Ultra 5")),
        ("Intel N100", (E, "Intel N100")),
        ("Intel Core", (U, "")),
        ("Intel Core processor", (U, "")),
        ("i5 / Ryzen 5", (A, "Core i5 | Ryzen 5")),
    ],
)
def test_labeled_cpu_family(value: str, expected: tuple[str, str]) -> None:
    page = pcx("Synthetic laptop", specs=(("Processor", value),))
    assert primary(page)["cpu_series"] == expected


@pytest.mark.parametrize(
    ("offer_cpu", "target_cpu"),
    [("Intel Core 5-210H", "Core i5"), ("Intel Core i5-13420H", "Core 5"),
     ("Intel Core Ultra 5 226V", "Core 5"), ("Intel N100", "Intel N150")],
)
def test_distinct_cpu_families_never_match(offer_cpu: str, target_cpu: str) -> None:
    specs = (("Processor", offer_cpu), *PCX_SPECS[1:])
    result = match(pcx("ASUS Vivobook S16 S3607VA-RP181WSM Laptop", specs=specs),
                   VIVO | {"cpu_series": target_cpu})
    assert result.status is MatchStatus.UNMATCHED and result.target_id is None


def test_a_generic_up_to_cpu_row_is_not_this_processor() -> None:
    specs = (*PCX_SPECS, ("Processor", "Up to 13th Gen Intel Core i7 Processor"))
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM Laptop", specs=specs)
    assert primary(page)["cpu_series"] == (E, "Core 5")
    assert "Up to 13th Gen Intel Core i7 Processor" in supporting(page)["cpu_series"]


def test_same_family_processor_numbers_match_with_a_visible_difference() -> None:
    specs = (("Processor", "Intel Core i5-1335U"), *PCX_SPECS[1:])
    page = pcx("Example Thin B13UC-3474PH Intel Core i5-13420H Laptop", specs=specs,
               url="https://pcx.com.ph/products/example-b13uc-3474ph")
    result = match(page, VIVO | {"model": "B13UC-3474PH", "cpu_series": "Core i5"})
    assert result.status is MatchStatus.EXACT
    assert "CPU_NUMBER_TITLE_DIFFERS" in result.warnings
    assert supporting(page)["cpu_model"] == ["title: 13420H; specification: 1335U"]


@pytest.mark.parametrize(
    ("label", "value", "expected"),
    [
        ("Display", "14.5in WQXGA+ OLED", (E, "14.5")),
        ("Display", "14 in stock", (U, "")),
        ("Display", "14.0 display", (U, "")),
    ],
)
def test_screen_units(label: str, value: str, expected: tuple[str, str]) -> None:
    assert primary(pcx("Synthetic laptop", specs=((label, value),)))["screen_size_in"] == expected


def test_bare_size_cannot_create_its_own_unit() -> None:
    assert primary(offer("datablitz", (("Size", "14.0"),)))["screen_size_in"] == (U, "")


@pytest.mark.parametrize(
    ("label", "value", "expected"),
    [
        ("Size", "14.0", set()),
        ("Screen Size (inches)", "14.0", {"14"}),  # the label states the unit
        ("Display Size (in)", "15.6", {"15.6"}),
        ("Screen Size (inches)", "65", set()),  # not a laptop diagonal
    ],
)
def test_a_unit_bearing_label_supplies_inches(label: str, value: str, expected) -> None:
    # Applies to mapped display labels; no retailer label carries a unit today.
    assert _screen_size_row(label, value) == expected


def test_a_bare_size_is_resolved_by_unit_bearing_evidence_elsewhere() -> None:
    page = offer("datablitz", (("Size", "14.0"), ("Display Panel/ Resolution", '14" WUXGA')))
    assert primary(page)["screen_size_in"] == (E, "14")


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ((("Memory", "16GB DDR5"), ("Total System Memory", "16GB DDR5")), (E, "16")),
        ((("Memory", "8GB + 8GB"),), (E, "16")),
        ((("Memory", "2x 8GB DDR5"),), (E, "16")),
        ((("Memory", "16GB (2x 8GB)"),), (E, "16")),
        ((("Total System Memory", "DDR5 16GB (8GB on board + 8GB SO-DIMM)"),), (E, "16")),
        ((("Memory", "8GB DDR5-4800 SO-DIMM, Max Capacity:64GB"),), (E, "8")),
        ((("Memory", "Up to 32GB"),), (U, "")),
        ((("Memory", "2x DDR5 SO-DIMM slots"),), (U, "")),
        ((("Memory", "4800 MT/s"),), (U, "")),
        ((("Memory", "8GB or 16GB"),), (A, "8; 16")),
        ((("Memory", "16GB"), ("Total System Memory", "8GB")), (A, "16; 8")),
    ],
)
def test_installed_memory(rows, expected) -> None:
    assert primary(offer("datablitz", rows))["ram_capacity_gb"] == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("512GB SSD + 512GB HDD", (E, "1024")),
        ("512GB SSD + 1TB HDD", (E, "1512")),
        ("1x 512 PCIe Gen4, 16 Gb/s, NVMe", (U, "")),
        ("16 Gb/s", (U, "")),
        ("512GB SSD + 1TB external HDD", (A, "512; 1000")),
        ("512GB SSD + optional 1TB HDD", (A, "512; 1000")),
        ("512GB SSD / 1TB HDD", (A, "512; 1000")),
    ],
)
def test_installed_storage(value: str, expected) -> None:
    assert primary(offer("datablitz", (("Storage", value),)))["storage_capacity_gb"] == expected


def test_unitless_storage_stays_unknown_without_the_exact_approval() -> None:
    page = offer("oct", (("Storage", "1x 512 PCIe Gen4, 16 Gb/s, NVMe"),),
                 url="https://www.octagon.com.ph/products/sibling-listing?variant=1")
    assert primary(page)["storage_capacity_gb"] == (U, "")
    assert "STORAGE_CAPACITY_UNIT_MISSING" in notes(page)
    assert supporting(page)["storage_throughput"] == ["16 Gb/s"]


def test_an_approved_title_designation_is_not_reused_on_a_sibling_url() -> None:
    title = "ACER NITRO V ANV15-42-R2UH OPI/RYZEN 5 7640HS/ 16GB DDR5/ 512GB SSD/ 15.6 FHD IPS"
    specs = (("CPU and chipset", "AMD Ryzen 5 6600H"), ("Memory", "16GB"),
             ("Display", '15.6" FHD IPS'), ("Storage", "512GB SSD"))
    page = offer("oct", specs, title=title,
                 url="https://www.octagon.com.ph/products/acer-nitro-v-anv15-42-r2uh-opi?variant=2")
    # The OPI suffix keeps the model unresolved; the owner approval names another exact URL.
    assert primary(page)["model"] == (M, "")
    assert "TITLE_MODEL_UNEXPLAINED_SUFFIX" in notes(page)
    assert not any(code.startswith("OWNER_APPROVED") for code in notes(page))


# F. Secondary severity


def test_missing_optional_detail_never_invents_a_conflict() -> None:
    result = ExactMatcher([VIVO | {"gpu_model": "RTX 3050"}]).match(
        pcx("x"), {**{k: v for k, v in VIVO.items() if k not in {"target_id", "status"}}}
    )
    assert result.status is MatchStatus.EXACT


def test_a_verified_material_hardware_conflict_blocks() -> None:
    attributes = {k: v for k, v in VIVO.items() if k not in {"target_id", "status"}}
    result = ExactMatcher([VIVO | {"gpu_model": "RTX 3050"}]).match(
        pcx("x"), attributes | {"gpu_model": "RTX 4050"}
    )
    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert "SECONDARY_CONFLICT_GPU_MODEL" in result.reason_codes


def test_os_and_processor_number_differences_are_warnings() -> None:
    attributes = {k: v for k, v in VIVO.items() if k not in {"target_id", "status"}}
    target = VIVO | {"os": "Windows 11 Home", "cpu_model": "Core 5 210H"}
    result = ExactMatcher([target]).match(
        pcx("x"), attributes | {"os": "Windows 11 Pro", "cpu_model": "Core 5 220H"}
    )
    assert result.status is MatchStatus.EXACT
    assert set(result.warnings) == {"SECONDARY_WARNING_OS", "SECONDARY_WARNING_CPU_MODEL"}
    conflicts = {e.field for e in result.evidence if e.status is EvidenceStatus.CONFLICT}
    assert conflicts == {"os", "cpu_model"}


# E. Selected variants and shared evidence


def test_title_route_on_a_shared_page_needs_the_complete_matrix() -> None:
    from test_owner_policy_rules import BOUND, matrix, pcc

    with_matrix = pcc((*BOUND, matrix()))
    without = pcc(BOUND)
    assert primary(with_matrix)["model"][0] == E
    assert primary(without)["model"] == (N, "")
    assert "TITLE_MODEL_SCOPE_UNPROVEN" in notes(without)


def test_selected_capacity_governs_and_two_selected_values_conflict() -> None:
    from test_owner_policy_rules import BOUND, matrix, pcc

    page = pcc((*BOUND, matrix()))
    assert primary(page)["ram_capacity_gb"] == (E, "16")
    assert "SHARED_DESCRIPTION_DIFFERS_RAM_CAPACITY_GB" in notes(page)
    conflicting = pcc((*BOUND, ("SELECTED_VARIANT", "RAM", "8GB DDR4 RAM")))
    assert primary(conflicting)["ram_capacity_gb"] == (A, "16; 8")


# Adapter layout recovery (oct-3, pcc-4)


def test_octagon_no_break_space_layout_keeps_label_and_value() -> None:
    body = oct_mutate(
        oct_fixture("oct_product_regular.html"),
        "Processor: Intel® Core™ i5-13420H, 8C / 12T, 12MB<br>",
        "Model    EX14<br>Processor    Intel® Core™ i5-13420H, 8C / 12T<br>",
    )
    rows = dict(oct_offer(body).details.raw_specifications)
    assert rows["Model"] == "EX14"
    assert rows["Processor"] == "Intel® Core™ i5-13420H, 8C / 12T"


def test_octagon_single_no_break_space_is_not_a_gap() -> None:
    body = oct_mutate(
        oct_fixture("oct_product_regular.html"),
        "Graphics: Integrated Intel® UHD Graphics<br>",
        "Integrated graphics shared memory<br>",
    )
    labels = [label for label, _ in oct_offer(body).details.raw_specifications]
    assert "Integrated graphics shared memory" in labels


def test_pc_central_span_layout_keeps_label_and_value() -> None:
    body = pcc_mutate(
        pcc_fixture(SINGLE),
        "<p>Model: Example Nitro 15 EN15-0002<br>",
        "<p><span>Model </span><span>Example Nitro 15 EN15-0002</span></p>"
        "<p><span>16GB</span><span> DDR4</span></p><p>",
    )
    rows = pcc_offer(body).details.raw_specifications
    assert ("Model", "Example Nitro 15 EN15-0002") in rows
    # A first span that is not a short label stays joined exactly as before.
    assert ("16GB DDR4", "") in rows


# H. Price eligibility stays separate from identity


def test_exact_bundle_is_comparable_and_unknown_package_keeps_its_price_reason() -> None:
    page = replace(pcx("ASUS Vivobook S16 S3607VA-RP181WSM Laptop"))
    bundled = offer("pcx", PCX_SPECS, title=page.raw_title, url=VIVO_URL, bundle=True)
    unknown = offer("pcx", PCX_SPECS, title=page.raw_title, url=VIVO_URL, bundle=None)
    for observation, expected in (
        (bundled, "COMPARABLE"), (unknown, "NOT_COMPARABLE: BUNDLE_STATUS_UNKNOWN")
    ):
        result = match(observation)
        assert result.status is MatchStatus.EXACT
        assert price_comparability(observation, result) == expected


def test_out_of_stock_exact_offer_remains_comparable() -> None:
    page = offer("pcx", PCX_SPECS, title="ASUS Vivobook S16 S3607VA-RP181WSM Laptop",
                 url=VIVO_URL, bundle=False)
    page = replace(
        page,
        availability=Availability.OUT_OF_STOCK,
        details=replace(page.details, price_type=PriceType.REGULAR),
    )
    assert price_comparability(page, match(page)) == "COMPARABLE"


# Provenance stays outside observation identity


def test_new_extraction_never_changes_an_observation_identity() -> None:
    page = pcx("ASUS Vivobook S16 S3607VA-RP181WSM Laptop")
    before = observation_id(page)
    match(page)
    assert observation_id(page) == before


def test_match_basis_is_recovered_from_stored_reason_codes() -> None:
    assert match_basis_from_codes(()) == "LABELED_SPECIFICATION"
    assert match_basis_from_codes(("TITLE_MODEL_CORROBORATED",)) == "TITLE_MODEL_CORROBORATED"
    assert match_basis_from_codes(
        ("SHARED_CPU_DISPLAY_RAM_STORAGE_ONLY", "OWNER_APPROVED_MODEL")
    ) == "OWNER_APPROVED_INTERPRETATION+SELECTED_VARIANT+SHARED_PRODUCT"
    assert offer_outcome("REVIEW_REQUIRED", ("MISSING_CORE_FIELDS", "MISSING_MODEL")) == (
        MODEL_UNVERIFIED
    )


# Coverage states


def record(**changes) -> OfferRecord:
    values = dict(
        retailer_id="pcx", offer_url=VIVO_URL, observation_id="o1",
        retrieved_at="2026-10-15T06:40:00.000000Z", match_status="EXACT",
        target_id="TGT-000031", candidate_target_ids=("TGT-000031",), reason_codes=(),
        price="55995", availability="IN_STOCK", price_eligibility="COMPARABLE",
    )
    return OfferRecord(**(values | changes))


def status(rows, retailer="pcx") -> str:
    return next(r["coverage_status"] for r in rows if r["retailer_id"] == retailer)


def test_without_logs_coverage_is_unknown_never_no_candidate() -> None:
    rows, _ = coverage(["TGT-000031"], ["pcx"], [])
    assert status(rows) == UNKNOWN_COVERAGE


def test_planned_executed_and_candidate_stages_are_distinct() -> None:
    plan = [{"target_id": "TGT-000031", "retailer_id": "pcx", "query": "S3607VA-RP181WSM"}]
    rows, _ = coverage(["TGT-000031"], ["pcx"], [], search_plan=plan, search_requests=[])
    assert status(rows) == DISCOVERY_NOT_ATTEMPTED
    executed = [{"target_id": "TGT-000031", "retailer_id": "pcx", "outcome": "NO_RESULTS"}]
    rows, _ = coverage(["TGT-000031"], ["pcx"], [], search_plan=plan, search_requests=executed)
    assert status(rows) == NO_CANDIDATE_FOUND
    listed = [{"target_id": "TGT-000031", "retailer_id": "pcx", "category": "CODE_MATCH",
               "decision": "NOT_COLLECTED_BUDGET", "product_url": VIVO_URL}]
    rows, _ = coverage(["TGT-000031"], ["pcx"], [], search_plan=plan, search_requests=executed,
                       candidates=listed)
    assert status(rows) == CANDIDATE_NOT_FETCHED


def test_one_failed_candidate_never_hides_an_eligible_offer() -> None:
    review = record(observation_id="o2", match_status="REVIEW_REQUIRED", target_id="",
                    reason_codes=("MISSING_CORE_FIELDS", "MISSING_MODEL"),
                    price_eligibility="NOT_PRICED: MATCH_REVIEW_REQUIRED")
    rows, details = coverage(["TGT-000031"], ["pcx"], [review, record()])
    assert status(rows) == PRICE_AVAILABLE
    assert len(details) == 2
    excluded = record(price_eligibility="NOT_COMPARABLE: BUNDLE_STATUS_UNKNOWN")
    rows, _ = coverage(["TGT-000031"], ["pcx"], [excluded])
    assert status(rows) == EXACT_PRICE_EXCLUDED


def test_an_older_eligible_price_and_a_newer_failure_are_both_disclosed() -> None:
    manifest = [{"request_id": "r1", "retailer_id": "pcx", "expected_target_id": "TGT-000031",
                 "product_url": VIVO_URL}]
    report = [{"request_id": "r1", "request_status": "FAILED", "failure_codes": "HTTP_503"}]
    history = [{"target_id": "TGT-000031", "retailer_id": "pcx", "match_status": "EXACT",
                "comparability_status": "COMPARABLE", "price": "55995.00",
                "retrieved_at_utc": "2026-10-13T08:00:00.000000Z"}]
    rows, _ = coverage(["TGT-000031"], ["pcx"], [], manifest=manifest, request_report=report,
                       history=history)
    row = rows[0]
    assert row["coverage_status"] == FETCH_FAILED
    assert row["last_eligible_retrieved_at"] == "2026-10-13T08:00:00.000000Z"
    assert "not refreshed by this run" in row["detail"]
    assert row["best_offer_price"] == ""  # no fabricated fresh price
