"""Pilot acceptance fixes (2026-10-19): coverage lineage, blank-cell reasons, DataBlitz
extraction, discovery continuation, and supplied candidate URLs.

Synthetic records and fixtures only; the routed fake transport serves every response and the
no-network guard is active. Reporting dates are fixed so replays are deterministic.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_collection import write_dicts
from test_discovery_continuation import run as discover_run
from test_extraction import FIXTURES
from test_extraction_layouts import offer as layout_offer
from test_extraction_layouts import primary, supporting
from test_observation_store import MODELS, T0
from test_observation_store import offer as stored_offer
from test_price_tracking import AS_OF, TARGETS, TM_SHA, WEEK, ledger, list_rows, row
from test_product_discovery import LF_PRODUCT, html, lf_search, rows, standard_routes
from test_target_master import target_values
from test_verified_top import load_script

from laptop_tracker.adapters.laptop_factory import HOST as LF_HOST
from laptop_tracker.conventions import title_identifiers, title_model_codes
from laptop_tracker.extraction import extract_offer_attributes
from laptop_tracker.match_run import matcher_from_validated
from laptop_tracker.matching import (
    DIFFERENT_MODEL,
    IDENTITY_CONFLICT,
    SAME_MODEL_DIFFERENT_CONFIGURATION,
    ExactMatcher,
    MatchStatus,
    configuration_outcome,
)
from laptop_tracker.observation_store import ObservationStore, observation_id
from laptop_tracker.offer_terms import BUNDLE_FLAG_POLICY
from laptop_tracker.pilot_coverage import (
    NO_CANDIDATE_FOUND,
    NO_RELEVANT_CANDIDATE,
    PRICE_AVAILABLE,
    REJECTED_SEARCH_RESULT,
    OfferRecord,
    coverage,
)
from laptop_tracker.price_tracking import (
    PriceTrackingInputError,
    ReportWeek,
    build_price_tracking,
    match_stored_observations,
    matching_context,
    validate_selected_list,
)
from laptop_tracker.product_discovery import normalize_supplied_url
from laptop_tracker.retailer_search import SEARCH_METHODS, TIER_MODEL_SPECS
from laptop_tracker.sheet_publication import _status_text
from laptop_tracker.target_master import validate_target_master

pytestmark = pytest.mark.usefixtures("no_network")
LF = SEARCH_METHODS["laptop_factory"]
discover = load_script("discover_products")
track_prices = load_script("track_prices")

# 1. Coverage associations and blank-cell explanations

T16, T8, TUF, SWIFT = "TGT-000258", "TGT-000219", "TGT-000017", "TGT-000034"
MSI = {"model": "B13UC-3474PH", "screen_size_in": "15.6", "cpu_series": "Core i5",
       "ram_capacity_gb": "16", "storage_capacity_gb": "512"}
CATALOG = {
    T16: MSI,
    T8: MSI | {"ram_capacity_gb": "8"},
    TUF: {"model": "FA506NCQ-HN012W", "screen_size_in": "15.6", "cpu_series": "Ryzen 7",
          "ram_capacity_gb": "8", "storage_capacity_gb": "512"},
    SWIFT: {"model": "SF14-51-553D", "screen_size_in": "14", "cpu_series": "Core Ultra 5",
            "ram_capacity_gb": "32", "storage_capacity_gb": "1000"},
}
PCC_PAGE = "https://pcc.example.test/products/msi-gaming-thin-15-b13uc-3474ph"
SEARCHED = [{"target_id": t, "retailer_id": "pcc", "outcome": "RESULTS",
             "tier": "1_FULL_MODEL_CODE"}
            for t in CATALOG]


def msi_offer(**changes) -> OfferRecord:
    values = dict(
        retailer_id="pcc", offer_url=f"{PCC_PAGE}?variant=7", observation_id="o-msi",
        retrieved_at="2026-10-19T02:47:59.000000Z", match_status="EXACT", target_id=T16,
        candidate_target_ids=(T16,), reason_codes=(), price="58999.00",
        availability="IN_STOCK", price_eligibility="COMPARABLE", offer_attributes=MSI,
        expected_target_id=T16, request_id="disc-pcc-TGT-000258-23",
    )
    return OfferRecord(**(values | changes))


def pair(rows_, target_id, retailer="pcc"):
    return next(r for r in rows_ if (r["target_id"], r["retailer_id"]) == (target_id, retailer))


def test_an_offer_collected_for_another_target_never_explains_a_rejected_search_result() -> None:
    # The PC Central TUF search listed the MSI page as UNRELATED; collection fetched it for the
    # MSI configuration. The TUF cell must not borrow it as "another configuration".
    candidates = [
        {"target_id": TUF, "retailer_id": "pcc", "category": "UNRELATED",
         "decision": "NOT_SELECTED", "product_url": PCC_PAGE},
        {"target_id": T16, "retailer_id": "pcc", "category": "CODE_MATCH",
         "decision": "COLLECT_VARIANT", "decision_reason": "Variant 7: states 16GB",
         "product_url": PCC_PAGE},
    ]
    pairs, details = coverage([TUF, T16], ["pcc"], [msi_offer()], candidates=candidates,
                              search_requests=SEARCHED, target_rows=CATALOG)
    tuf = pair(pairs, TUF)
    assert tuf["coverage_status"] == NO_RELEVANT_CANDIDATE
    assert "none showing this configuration's model code: UNRELATED x1" in tuf["current_reason"]
    assert (tuf["candidate_url"], tuf["differing_fields"], tuf["unconfirmed_candidate_price"]) == (
        "", "", "")
    assert (tuf["rejected_search_results"], tuf["relevant_candidates"]) == ("1", "0")
    audit = next(d for d in details if d["target_id"] == TUF)
    assert (audit["relation"], audit["price_role"]) == (REJECTED_SEARCH_RESULT,
                                                        "AUDIT_ONLY_NOT_RELATED")
    assert pair(pairs, T16)["coverage_status"] == PRICE_AVAILABLE


def test_searches_with_no_results_differ_from_searches_with_only_other_models() -> None:
    nothing, _ = coverage([TUF], ["pcc"], [], search_requests=SEARCHED, candidates=[])
    assert pair(nothing, TUF)["coverage_status"] == NO_CANDIDATE_FOUND
    rejected = [{"target_id": TUF, "retailer_id": "pcc", "category": "DIFFERENT_MODEL_CODE",
                 "decision": "NOT_SELECTED", "product_url": "https://pcc.example.test/p/x"}]
    other, _ = coverage([TUF], ["pcc"], [], search_requests=SEARCHED, candidates=rejected)
    assert pair(other, TUF)["coverage_status"] == NO_RELEVANT_CANDIDATE


def test_one_product_evaluated_for_two_configurations_keeps_both_relations() -> None:
    # The 8GB MSI page was collected for TGT-000219 and selected again (duplicate) for the 16GB
    # search: a legitimate same-model, different-configuration explanation.
    eight = msi_offer(target_id=T8, candidate_target_ids=(T8,), expected_target_id=T8,
                      offer_attributes=MSI | {"ram_capacity_gb": "8"},
                      request_id="disc-pcc-TGT-000219-1")
    candidates = [
        {"target_id": T8, "retailer_id": "pcc", "category": "CODE_MATCH", "decision": "COLLECT",
         "product_url": PCC_PAGE, "collection_request_id": "disc-pcc-TGT-000219-1"},
        {"target_id": T16, "retailer_id": "pcc", "category": "CODE_MATCH",
         "decision": "DUPLICATE_OF_EARLIER_CANDIDATE", "product_url": PCC_PAGE,
         "collection_request_id": "disc-pcc-TGT-000219-1"},
    ]
    pairs, details = coverage([T16, T8], ["pcc"], [eight], candidates=candidates,
                              search_requests=SEARCHED, target_rows=CATALOG)
    sixteen = pair(pairs, T16)
    assert sixteen["coverage_status"] == SAME_MODEL_DIFFERENT_CONFIGURATION
    assert sixteen["differing_fields"] == "ram_capacity_gb: offer 8 vs target 16"
    assert "confirmed as TGT-000219" in sixteen["current_reason"]
    # Requested/search target and confirmed target stay separate; no confirmed price for 16GB.
    detail = next(d for d in details if d["target_id"] == T16)
    assert (detail["relation"], detail["request_id"], detail["assigned_target_id"]) == (
        "SEARCHED_FOR", "disc-pcc-TGT-000219-1", T8)
    assert (sixteen["confirmed_price"], sixteen["unconfirmed_candidate_price"]) == ("", "58999.00")
    assert pair(pairs, T8)["coverage_status"] == PRICE_AVAILABLE


def test_a_near_match_of_another_model_is_not_related_but_a_same_model_one_is() -> None:
    other_model = msi_offer(match_status="UNMATCHED", target_id="", expected_target_id="",
                            reason_codes=("NO_CORE_MATCH", "ONE_CORE_FIELD_DIFFERS"),
                            price_eligibility="NOT_PRICED: MATCH_UNMATCHED",
                            offer_attributes=MSI | {"model": "B13UC-9999PH"})
    pairs, _ = coverage([T16], ["pcc"], [other_model], search_requests=SEARCHED,
                        candidates=[], target_rows=CATALOG)
    assert (pair(pairs, T16)["coverage_status"], pair(pairs, T16)["offers_related"]) == (
        NO_CANDIDATE_FOUND, "0")
    bigger = msi_offer(match_status="UNMATCHED", target_id="", expected_target_id="",
                       reason_codes=("NO_CORE_MATCH", "ONE_CORE_FIELD_DIFFERS"),
                       price_eligibility="NOT_PRICED: MATCH_UNMATCHED",
                       offer_attributes=MSI | {"ram_capacity_gb": "32"})
    pairs, _ = coverage([T16], ["pcc"], [bigger], search_requests=SEARCHED, candidates=[],
                        target_rows=CATALOG)
    assert pair(pairs, T16)["coverage_status"] == SAME_MODEL_DIFFERENT_CONFIGURATION


def test_the_same_model_code_with_another_screen_is_an_unresolved_conflict() -> None:
    # Octagon states 14.5" for SF14-51-553D; the catalog says 14". No tolerance, no catalog change.
    octagon = msi_offer(retailer_id="oct", offer_url="https://oct.example.test/products/sf14",
                        match_status="UNMATCHED", target_id="", expected_target_id=SWIFT,
                        candidate_target_ids=(SWIFT,),
                        reason_codes=("NO_CORE_MATCH", "ONE_CORE_FIELD_DIFFERS"),
                        price_eligibility="NOT_PRICED: MATCH_UNMATCHED",
                        offer_attributes=CATALOG[SWIFT] | {"screen_size_in": "14.5"})
    pairs, _ = coverage([SWIFT], ["oct"], [octagon], target_rows=CATALOG)
    swift = pair(pairs, SWIFT, "oct")
    assert swift["coverage_status"] == IDENTITY_CONFLICT
    assert "conflicting specification: screen_size_in: offer 14.5 vs target 14 (unresolved)" in (
        swift["current_reason"])


@pytest.mark.parametrize(
    ("target_model", "differences", "expected"),
    [
        ("B13UC-3474PH", {"ram_capacity_gb": "8"}, SAME_MODEL_DIFFERENT_CONFIGURATION),
        ("B13UC-3474PH", {"storage_capacity_gb": "1000"}, SAME_MODEL_DIFFERENT_CONFIGURATION),
        ("B13UC-3474PH", {"cpu_series": "Core i7"}, IDENTITY_CONFLICT),
        ("B13UC-3474PH", {"model": "B13VE-1234PH"}, DIFFERENT_MODEL),
        # A family name is not one model code: processor or screen options are configurations.
        ("MacBook Air", {"cpu_series": "Apple M4"}, SAME_MODEL_DIFFERENT_CONFIGURATION),
    ],
)
def test_same_model_different_configuration_is_reserved_for_genuine_options(
    target_model, differences, expected
) -> None:
    target = MSI | {"model": target_model, "cpu_series": "Apple M5" if "Mac" in target_model
                    else "Core i5"}
    offered = target | differences
    status, _ = configuration_outcome("UNMATCHED", "", ("NO_CORE_MATCH",), offered, T16, target)
    assert status == expected


# 1b. The weekly comparison takes the same reasons.


def build(tmp_path, observations, *, search_outcomes=None, search_context=None, week=WEEK,
          as_of=AS_OF, policy=BUNDLE_FLAG_POLICY, discovery_rechecks=None):
    tm = validate_target_master(target_values(*TARGETS))
    selected = validate_selected_list(list_rows(), ledger(), tm, target_master_sha256=TM_SHA,
                                      list_sha256="e" * 64)
    with ObservationStore.open(tmp_path / "store.sqlite3") as store:
        if observations:
            store.import_observations(observations, source_label="test", imported_at=AS_OF)
        context = matching_context(tm, target_master_sha256=TM_SHA, source_label="test")
        match_stored_observations(store, context, matcher_from_validated(tm), now=AS_OF)
        stored, matches = store.observations(), store.match_results(context.context_id)
    return build_price_tracking(
        stored, matches, selected, context, week=week, as_of=as_of, offer_terms_policy=policy,
        search_context=search_context, search_outcomes=search_outcomes,
        discovery_rechecks=discovery_rechecks,
    )


SEARCH_TEXT = "4 search page(s) over 4 query tier(s) returned 11 result(s), none showing ..."


def test_a_blank_cell_takes_the_weeks_recorded_search_outcome(tmp_path) -> None:
    result = build(tmp_path, [], search_outcomes={
        ("TGT-000001", "LF"): ("NO_RELEVANT_CANDIDATE", SEARCH_TEXT),
        ("TGT-000001", "PCX"): ("SEARCH_FAILED", "SEARCH_ERROR: HTTP_403"),
    })
    first = row(result, "TGT-000001")
    assert (first["LF_Price_Status"], first["LF_Reason"]) == (
        "NO_RELEVANT_CANDIDATE", f"NO_RELEVANT_CANDIDATE: {SEARCH_TEXT}")
    assert first["PCX_Price_Status"] == "SEARCH_FAILED"
    # Without a recorded search the cell stays honestly unexplained.
    assert row(result, "TGT-000002")["LF_Price_Status"] == "NO_OBSERVATION_IN_WEEK"
    assert result.counts["cells_NO_RELEVANT_CANDIDATE"] == 1
    assert _status_text("NO_RELEVANT_CANDIDATE", "") == "Searched; no relevant candidate found"


def test_search_outcomes_never_override_offers_and_never_reach_other_weeks(tmp_path) -> None:
    earlier = stored_offer(at=T0, price="900.00")  # comparable, week of 2026-10-05
    unresolved = stored_offer(
        slug="eb14-no-ram", at=datetime(2026, 10, 13, 4, tzinfo=UTC), price="400.00",
        specs=tuple(r for r in MODELS["EB14-0001"] if r[0] != "RAM"),
    )
    week = ReportWeek(date(2026, 10, 12))
    as_of = datetime(2026, 10, 14, tzinfo=UTC)
    outcomes = {("TGT-000001", "LF"): ("NO_RELEVANT_CANDIDATE", SEARCH_TEXT)}
    result = build(tmp_path, [earlier, unresolved], search_outcomes=outcomes, week=week,
                   as_of=as_of, search_context={observation_id(unresolved): ["TGT-000001"]})
    cell = row(result, "TGT-000001")
    # The unconfirmed candidate found this week explains the cell, not the search summary.
    assert cell["LF_Price_Status"] == "CANDIDATE_NOT_CONFIRMED"
    assert (cell["LF_Price"], cell["Lowest_Price"]) == ("", "")
    series = {(r["report_week_start"], r["target_id"], r["retailer_code"]): r
              for r in result.series}
    # The earlier week keeps its own selected price; the search outcome is this week's only.
    assert series[("2026-10-05", "TGT-000001", "LF")]["price_status"] == "SELECTED"
    without = build(tmp_path / "b", [earlier], search_outcomes=outcomes, week=week, as_of=as_of)
    cell = row(without, "TGT-000001")
    assert cell["LF_Price_Status"] == "NO_RELEVANT_CANDIDATE"
    assert "last comparable 900.00" in cell["LF_Reason"] and "not carried forward" in cell[
        "LF_Reason"]
    assert cell["LF_Price"] == ""


def test_a_near_match_hint_for_another_model_is_not_a_cell_candidate(tmp_path) -> None:
    specs = tuple((label, value.replace("EB14-0001", "EB14-0009")) for label, value in
                  MODELS["EB14-0001"])
    other = stored_offer("EB14-0009", specs=specs)
    result = build(tmp_path, [other])
    kept = next(h for h in result.history if h["observation_id"] == observation_id(other))
    # The matcher's near-match hint names TGT-000001 (only the model differs)...
    assert (kept["match_status"], kept["candidate_target_ids"]) == ("UNMATCHED", "TGT-000001")
    # ...but another model is never offered as this configuration's candidate.
    assert row(result, "TGT-000001")["LF_Price_Status"] == "NO_OBSERVATION_IN_WEEK"
    linked = build(tmp_path / "b", [other],
                   search_context={observation_id(other): ["TGT-000001"]})
    cell = row(linked, "TGT-000001")
    assert (cell["LF_Price_Status"], cell["LF_Candidate_Status"]) == (
        "CANDIDATE_NOT_CONFIRMED", DIFFERENT_MODEL)


def test_price_rules_are_unchanged_beside_search_outcomes(tmp_path) -> None:
    from laptop_tracker.offers import Availability

    sold_out = stored_offer(slug="eb14-0001-bundle", price="900.00", bundle=True,
                            availability=Availability.OUT_OF_STOCK)
    in_stock = stored_offer(slug="eb14-0001", price="950.00", content="1")
    result = build(tmp_path, [sold_out, in_stock], search_outcomes={
        ("TGT-000001", "PCX"): ("NO_RELEVANT_CANDIDATE", SEARCH_TEXT)})
    cell = row(result, "TGT-000001")
    # A comparable sold-out price stays visible and labeled; the in-stock minimum excludes it.
    assert (cell["LF_Price"], cell["LF_Stock"], cell["LF_Reason"]) == (
        "900.00", "OUT_OF_STOCK", "PRICE_SHOWN_WHILE_OUT_OF_STOCK")
    assert (cell["Lowest_Price"], cell["Lowest_Price_Stock"]) == ("900.00", "OUT_OF_STOCK")
    assert cell["Lowest_Price_Notes"] == "LF:BUNDLE_OFFER"
    assert (cell["Lowest_In_Stock_Price"], cell["Lowest_In_Stock_Price_Retailers"]) == (
        "950.00", "LF")
    assert (cell["Comparable_Retailer_Count"], cell["PCX_Price_Status"]) == (
        "1", "NO_RELEVANT_CANDIDATE")
    # The reviewed run's week: Monday 2026-10-19 to 2026-10-26 in Asia/Singapore.
    reviewed = ReportWeek(date(2026, 10, 19))
    assert (reviewed.start_utc, reviewed.end_utc) == (
        datetime(2026, 10, 18, 16, tzinfo=UTC), datetime(2026, 10, 25, 16, tzinfo=UTC))


def write_discovery(folder: Path, started: str) -> Path:
    folder.mkdir(parents=True)
    (folder / "discovery_summary.json").write_text(json.dumps({"started_at_utc": started}))
    write_dicts(folder / "search_requests.csv", ("target_id", "retailer_id", "tier", "outcome"),
                [{"target_id": "TGT-000001", "retailer_id": "pcx", "tier": "1_FULL_MODEL_CODE",
                  "outcome": "RESULTS"}])
    write_dicts(folder / "candidates.csv",
                ("target_id", "retailer_id", "category", "decision", "product_url"),
                [{"target_id": "TGT-000001", "retailer_id": "pcx", "category": "UNRELATED",
                  "decision": "NOT_SELECTED", "product_url": "https://pcx.com.ph/products/x"}])
    return folder


def test_discovery_logs_explain_only_their_own_report_week(tmp_path) -> None:
    week = ReportWeek(date(2026, 10, 19))
    current = write_discovery(tmp_path / "now", "2026-10-19T02:40:52.239778Z")
    _, outcomes, _ = track_prices.read_discovery([current], week)
    assert outcomes[("TGT-000001", "PCX")][0] == "NO_RELEVANT_CANDIDATE"
    stale = write_discovery(tmp_path / "old", "2026-10-13T07:57:14Z")
    with pytest.raises(PriceTrackingInputError, match="outside the report week"):
        track_prices.read_discovery([stale], week)


# 2. DataBlitz extraction

MSI_TITLE = ('MSI Thin 15 B13UC-3474PH Gaming Laptop (Cosmos Gray) | 15.6" FHD (1920x1080) 144Hz '
             "IPS | i5-13420H | 8GB RAM | 512GB SSD | RTX 3050 | Windows 11 Home | MSI Essential "
             "Backpack")
MSI_ROWS = (
    ("CPU", "Raptor Lake i5-13420H"),
    ("Display", '15.6" FHD (1920x1080), 144Hz, IPS-Level'),
    ("Memory", "DDR4-3200 8GB RAM Max up to 64GB 2 Slots"),
    ("Storage Capability", '1x M.2 SSD slot (NVMe PCIe Gen4) 512GB SSD 1x 2.5" SATA HDD '
                           "”Please consult service center for upgrading SSD.”"),
)
SWIFT_TITLE = ('Acer Swift 14 AI SF14-51-553D Laptop (Frost Blue) | 14" WQXGA+ (2880x1800) OLED | '
               "Ultra 5 228V | 32GB RAM | 1TB SSD | Intel Arc Graphics | Windows 11 Home | MS "
               "Office Home 2024 + MS 365 Basic + Acer Entry Run Rate Backpack E-1620-P")
SWIFT_ROWS = (
    ("System & Processor", ""),
    ("Processor", "Intel® Core™ Ultra 5 228V (2.0 GHz, up to 4.5 GHz Turbo)"),
    ("Memory & Storage", ""),
    ("Standard Memory", "32 GB"),
    ("Storage Capacity", "1 TB NVMe SSD"),
    ("Display & Graphics", ""),
    ("Display", '14" WQXGA+ (2880 x 1800) OLED'),
    ("Color", "Frost Blue"),
)
E, M, A = "EXTRACTED", "MISSING", "AMBIGUOUS"
SWIFT_TARGET = {"target_id": SWIFT, "status": "active", **CATALOG[SWIFT]}


def dbz(title: str, specs) -> object:
    return layout_offer("datablitz", specs, title=title,
                        url="https://ecommerce.datablitz.com.ph/products/synthetic?variant=1")


def test_flattened_memory_row_keeps_installed_ram_and_reports_the_maximum() -> None:
    page = dbz(MSI_TITLE, MSI_ROWS)
    fields = primary(page)
    assert fields["ram_capacity_gb"] == (E, "8")
    assert fields["storage_capacity_gb"] == (E, "512")
    # The title code is accepted only because the four other fields stand on their own.
    assert fields["model"] == (E, "B13UC-3474PH")
    assert "TITLE_MODEL_CORROBORATED" in extract_offer_attributes(page).notes
    extra = supporting(page)
    assert (extra["ram_maximum_gb"], extra["ram_slots"]) == (["64"], ["2"])
    targets = [{"target_id": t, "status": "active", **CATALOG[t]} for t in (T8, T16)]
    result = ExactMatcher(targets).match_observation(page)
    assert (result.status, result.target_id) == (MatchStatus.EXACT, T8)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Max up to 64GB", "not installed"),  # a lone limit never becomes installed capacity
        ("8GB RAM Maximum 32GB", (E, "8")),
        ("DDR5 up to 5600MHz 16GB", (E, "16")),  # a speed limit is not a capacity maximum
        ("8GB RAM 16GB RAM", (A, "8; 16")),  # two installed amounts stay ambiguous
        ("16GB (8GB + 8GB up to 32GB)", (A, "16; 8; 8; 32")),  # inside a breakdown: inseparable
    ],
)
def test_inline_maximum_clauses_are_split_only_when_they_are_separable(value, expected) -> None:
    rows_ = tuple((label, value if label == "Memory" else text) for label, text in MSI_ROWS)
    status = primary(dbz("Synthetic laptop", rows_))["ram_capacity_gb"]
    if expected == "not installed":
        assert status[0] != E
    else:
        assert status == expected


def test_acer_spec_table_labels_and_the_backpack_code() -> None:
    page = dbz(SWIFT_TITLE, SWIFT_ROWS)
    fields = primary(page)
    assert (fields["ram_capacity_gb"], fields["storage_capacity_gb"]) == ((E, "32"), (E, "1000"))
    assert fields["model"] == (E, "SF14-51-553D")
    notes = extract_offer_attributes(page).notes
    assert "TITLE_MULTIPLE_MODEL_CODES" not in notes and "TITLE_MODEL_CORROBORATED" in notes
    assert supporting(page)["accessory_identifier"] == ["E-1620-P"]
    result = ExactMatcher([SWIFT_TARGET]).match_observation(page)
    assert (result.status, result.target_id) == (MatchStatus.EXACT, SWIFT)


def test_identifier_kinds_stay_distinct() -> None:
    found = title_identifiers(SWIFT_TITLE + " | 90NR0QE7-M001U0")
    assert found.model_codes == ("SF14-51-553D",)
    assert found.accessory_codes == ("E-1620-P",)
    assert found.part_numbers == ("90NR0QE7-M001U0",)


@pytest.mark.parametrize(
    "title",
    [
        # Two genuine laptop codes still need review.
        "Acer Swift 14 AI SF14-51-553D / SF14-51-55XX Laptop",
        # A code in an item segment that names a laptop is a laptop code.
        "Acer Swift 14 AI SF14-51-553D Laptop + Acer Laptop SF14-51-55XX",
    ],
)
def test_conflicting_laptop_codes_still_require_review(title) -> None:
    page = dbz(title, SWIFT_ROWS)
    assert "TITLE_MULTIPLE_MODEL_CODES" in extract_offer_attributes(page).notes
    assert primary(page)["model"] == (M, "")


def test_context_not_the_catalog_decides_what_an_accessory_code_is() -> None:
    # A code before the accessory noun is the laptop's.
    assert title_model_codes("Acer Swift SF14-51-553D Backpack Bundle")[0] == ["SF14-51-553D"]
    # A code after an accessory noun is the accessory's, even if a catalog model has that code:
    # it is never chosen because it equals an expected target.
    page = dbz("Laptop Bundle + Acer Backpack SF14-51-553D", SWIFT_ROWS)
    assert primary(page)["model"] == (M, "")


def test_a_title_model_still_needs_the_four_other_fields_established() -> None:
    # Neither a specification row nor the title states the RAM: the model stays unresolved.
    rows_ = tuple(r for r in SWIFT_ROWS if r[0] != "Standard Memory")
    page = dbz(SWIFT_TITLE.replace(" | 32GB RAM", ""), rows_)
    assert primary(page)["model"] == (M, "")
    result = ExactMatcher([SWIFT_TARGET]).match_observation(page)
    assert result.status is MatchStatus.REVIEW_REQUIRED


def test_the_swift_screen_discrepancy_is_kept_without_approximation() -> None:
    rows_ = tuple((label, '14.5" WQXGA+ (2880 x 1800) OLED' if label == "Display" else value)
                  for label, value in SWIFT_ROWS)
    page = dbz(SWIFT_TITLE.replace('14"', '14.5"'), rows_)
    result = ExactMatcher([SWIFT_TARGET]).match_observation(page)
    assert result.status is MatchStatus.UNMATCHED and result.target_id is None


# 3. Discovery continuation

EIGHT_PAGE = f"https://{LF_HOST}/product/acer-aspire-ai-14-a14-11m-x3bp-ver2/"
CODE_URL = LF.first_page_url("A14-11M-X3BP")
SPEC_URL = LF.first_page_url("A14-11M-X3BP 16GB 512GB")
TITLE = "Acer Aspire AI 14 A14-11M-X3BP"  # plausible: the full code, no contrary clue


def eight_gb_page():
    body = (FIXTURES / "laptop_factory" / "lf_product_sale.html").read_text(encoding="utf-8")
    body = body.replace("acer-aspire-ai-14-a14-11m-x3bp/", "acer-aspire-ai-14-a14-11m-x3bp-ver2/")
    return html(EIGHT_PAGE, body.replace("16 GB of onboard", "8 GB of onboard"))


def continuation_routes(spec_hits=((EIGHT_PAGE, TITLE), (LF_PRODUCT, TITLE))):
    routes = standard_routes()
    routes[CODE_URL] = html(CODE_URL, lf_search([(EIGHT_PAGE, TITLE)]))
    routes[SPEC_URL] = html(SPEC_URL, lf_search(list(spec_hits)) if spec_hits
                            else lf_search(no_results=True))
    routes[EIGHT_PAGE] = eight_gb_page()
    return routes


def job(out: Path) -> dict[str, str]:
    return next(r for r in rows(out / "search_jobs.csv") if r["target_id"] == "TGT-000001")


def test_an_unsuitable_plausible_candidate_resumes_the_remaining_tiers(tmp_path) -> None:
    code, transport, out = discover_run(tmp_path, continuation_routes(), "--collect",
                                        "--max-total-requests", "20")
    assert code == 0
    row_ = job(out)
    assert (row_["initial_stop_reason"], row_["resumed"], row_["discovery_rounds"]) == (
        "PLAUSIBLE_CANDIDATE_FOUND", "TRUE", "2")
    assert "SAME_MODEL_DIFFERENT_CONFIGURATION" in row_["resume_reason"]
    assert "ram_capacity_gb: offer 8 vs target 16" in row_["resume_reason"]
    assert row_["tiers_after_resume"].startswith(f"{TIER_MODEL_SPECS}:RESULTS")
    assert (row_["discovery_outcome"], row_["suitable_offer_url"]) == (
        "SUITABLE_OFFER_FOUND", LF_PRODUCT)
    assert row_["collection_requests_after_resume"] == "1"
    # Bounded: the first page is never fetched twice; nothing beyond the suitable offer runs.
    assert transport.calls.count(EIGHT_PAGE) == 1 and transport.calls.count(LF_PRODUCT) == 1
    assert LF.first_page_url("ExampleBook") not in transport.calls
    candidates = {r["product_url"]: r for r in rows(out / "candidates.csv")}
    assert (candidates[EIGHT_PAGE]["discovery_round"], candidates[EIGHT_PAGE]["decision"]) == (
        "1", "COLLECT")
    assert candidates[LF_PRODUCT]["discovery_round"] == "2"
    results = {r["offer_url"]: r for r in rows(out / "collection" / "match_results.csv")}
    # The original candidate keeps its actual outcome; only the right page confirms the target.
    assert results[EIGHT_PAGE]["match_status"] != "EXACT"
    assert (results[LF_PRODUCT]["match_status"], results[LF_PRODUCT]["target_id"]) == (
        "EXACT", "TGT-000001")
    assert {r["request_id"] for r in rows(out / "collection" / "request_report.csv")} == {
        candidates[EIGHT_PAGE]["collection_request_id"],
        candidates[LF_PRODUCT]["collection_request_id"],
    }
    summary = json.loads((out / "discovery_summary.json").read_text())
    assert summary["continuation"]["jobs_resumed"] == 1
    assert summary["collection_rounds"] == 2


def test_exhausted_tiers_after_resuming_are_recorded(tmp_path) -> None:
    _, _, out = discover_run(tmp_path, continuation_routes(spec_hits=()), "--collect",
                             "--max-total-requests", "20")
    row_ = job(out)
    assert (row_["resumed"], row_["discovery_outcome"]) == ("TRUE", "QUERY_PLAN_EXHAUSTED")
    assert row_["suitable_offer_url"] == ""
    assert EIGHT_PAGE in row_["candidate_outcomes"]


@pytest.mark.parametrize(
    ("extra", "outcome"),
    [
        (("--max-discovery-rounds", "1", "--max-total-requests", "20"), "BUDGET_EXHAUSTED"),
        (("--max-total-requests", "2"), "BUDGET_EXHAUSTED"),
    ],
)
def test_continuation_stays_within_the_configured_budget(tmp_path, extra, outcome) -> None:
    _, transport, out = discover_run(tmp_path, continuation_routes(), "--collect", *extra)
    assert job(out)["discovery_outcome"] == outcome
    assert SPEC_URL not in transport.calls and LF_PRODUCT not in transport.calls


def test_a_replayed_continuation_sends_nothing_and_reports_the_gap(tmp_path) -> None:
    _, _, first = discover_run(tmp_path, continuation_routes(), "--collect",
                               "--max-discovery-rounds", "1", "--max-total-requests", "20")
    code, transport, replay = discover_run(
        tmp_path, {}, "--max-total-requests", "1", "--replay-only", "--collect",
        "--search-cache", str(first), "--replay-collection", str(first / "collection"),
        out="replay",
    )
    assert code == 0 and transport.calls == []
    row_ = job(replay)
    assert (row_["resumed"], row_["discovery_outcome"]) == ("TRUE",
                                                            "SEARCH_INCOMPLETE_REPLAY_ONLY")
    assert "NOT_SENT_REPLAY_ONLY" in row_["tiers_after_resume"]
    report = rows(replay / "collection" / "request_report.csv")
    assert {r["replay_source"].split(":", 1)[0] for r in report} == {"SAVED_OBSERVATION_RECORD"}
    # The reused record keeps its observation identity.
    assert {r["observation_id"] for r in rows(replay / "collection" / "match_results.csv")} == {
        r["observation_id"] for r in rows(first / "collection" / "match_results.csv")}


# 4. Supplied candidate URLs


def supplied(tmp_path, *entries) -> Path:
    return write_dicts(tmp_path / "supplied.csv",
                       ("supplied_id", "retailer_id", "url", "search_target_id", "supplied_by",
                        "supplied_on", "notes"),
                       [{"supplied_id": f"SUP-{i}", "retailer_id": "laptop_factory", "url": url,
                         "search_target_id": hint} for i, (url, hint) in enumerate(entries, 1)])


def no_results_routes():
    routes = standard_routes()
    for url in (CODE_URL, SPEC_URL):
        routes[url] = html(url, lf_search(no_results=True))
    return routes


def test_supplied_url_normalization_keeps_product_and_variant_identity() -> None:
    assert normalize_supplied_url(
        "HTTPS://PCX.com.ph/products/acer-nitro-v-15-anv15-42-r2uh?srsltid=AU7g&utm_source=x#top"
    ) == ("https://pcx.com.ph/products/acer-nitro-v-15-anv15-42-r2uh", ("srsltid", "utm_source"))
    assert normalize_supplied_url(
        "https://pcc.example.test/products/Nitro-V?variant=51013538775207&_pos=1"
    ) == ("https://pcc.example.test/products/Nitro-V?variant=51013538775207", ("_pos",))


def test_a_known_page_missed_by_search_is_checked_directly_and_its_evidence_decides(tmp_path):
    path = supplied(tmp_path, (LF_PRODUCT + "?srsltid=abc", ""))
    code, transport, out = discover_run(tmp_path, no_results_routes(), "--collect",
                                        "--max-total-requests", "20", "--supplied-urls",
                                        str(path))
    assert code == 0 and transport.calls.count(LF_PRODUCT) == 1
    report = rows(out / "supplied_urls.csv")[0]
    assert (report["assignment_basis"], report["assigned_target_ids"],
            report["found_by_native_search"], report["removed_parameters"]) == (
        "URL_HANDLE_SHOWS_MODEL_CODE", "TGT-000001", "FALSE", "srsltid")
    candidate = next(r for r in rows(out / "candidates.csv") if r["category"] == "SUPPLIED_URL")
    assert candidate["decision"] == "COLLECT"
    assert candidate["collection_request_id"] == report["collection_request_id"]
    row_ = job(out)
    assert "NATIVE_SEARCH_MISSED_SUPPLIED_URL" in row_["search_coverage"]
    assert row_["discovery_outcome"] == "SUITABLE_OFFER_FOUND"
    result = rows(out / "collection" / "match_results.csv")[0]
    assert (result["match_status"], result["target_id"]) == ("EXACT", "TGT-000001")


def test_a_supplied_page_is_a_seed_never_a_match(tmp_path) -> None:
    routes = no_results_routes() | {EIGHT_PAGE: eight_gb_page()}
    path = supplied(tmp_path, (EIGHT_PAGE, "TGT-000001"))
    _, _, out = discover_run(tmp_path, routes, "--collect", "--max-total-requests", "20",
                             "--supplied-urls", str(path))
    assert rows(out / "supplied_urls.csv")[0]["assignment_basis"] == "SEARCH_TARGET_HINT"
    result = rows(out / "collection" / "match_results.csv")[0]
    assert result["match_status"] != "EXACT"
    assert job(out)["discovery_outcome"] == "QUERY_PLAN_EXHAUSTED"


def test_a_supplied_page_found_by_search_is_flagged_not_fetched_twice(tmp_path) -> None:
    path = supplied(tmp_path, (LF_PRODUCT, ""))
    _, transport, out = discover_run(tmp_path, standard_routes(), "--collect",
                                     "--max-total-requests", "20", "--supplied-urls", str(path))
    assert transport.calls.count(LF_PRODUCT) == 1
    assert rows(out / "supplied_urls.csv")[0]["found_by_native_search"] == "TRUE"
    candidate = next(r for r in rows(out / "candidates.csv") if r["product_url"] == LF_PRODUCT)
    assert "ALSO_SUPPLIED_URL" in candidate["flags"] and candidate["category"] == "CODE_MATCH"


def test_a_supplied_url_without_code_or_hint_is_not_assigned(tmp_path) -> None:
    path = supplied(tmp_path, (f"https://{LF_HOST}/product/some-laptop/", ""))
    _, transport, out = discover_run(tmp_path, no_results_routes(), "--max-total-requests", "20",
                                     "--supplied-urls", str(path))
    assert rows(out / "supplied_urls.csv")[0]["assignment_basis"] == "UNASSIGNED_NO_CODE_IN_URL"
    assert f"https://{LF_HOST}/product/some-laptop/" not in transport.calls


def test_a_seed_file_must_state_match_status(tmp_path) -> None:
    path = write_dicts(tmp_path / "seeds.csv", ("retailer_id", "offer_url", "target_id"),
                       [{"retailer_id": "laptop_factory", "offer_url": LF_PRODUCT,
                         "target_id": "TGT-000001"}])
    code, transport, _ = discover_run(tmp_path, {}, "--max-total-requests", "5",
                                      "--seed-urls", str(path))
    assert code == 1 and transport.calls == []
