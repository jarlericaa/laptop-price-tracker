"""Safe verification regressions for the 2026-10-21 audit traps, the proposed CPU-model gate,
offers shared by several targets, and the per-cell diagnostics (2026-10-22)."""

from __future__ import annotations

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_balanced_policy import VIVO, notes, pcx

from laptop_tracker.cell_diagnostics import (
    CONFIGURATION_MISMATCH,
    NO_RELEVANT_CANDIDATE,
    NOT_COMPARABLE,
    NOT_SEARCHED,
    PUBLISHED,
    VARIANT_UNRESOLVED,
    ZERO_RESULTS,
    diagnose_cells,
    overlay_audit,
    stage_counts,
    url_kind,
)
from laptop_tracker.matching import STRICT_CPU_MODEL, ExactMatcher, MatchStatus
from laptop_tracker.price_tracking import RETAILERS
from laptop_tracker.product_discovery import SearchTarget, VariantInfo, variant_decision
from laptop_tracker.sheet_publication import shared_offers

pytestmark = pytest.mark.usefixtures("no_network")
ATTRIBUTES = {k: v for k, v in VIVO.items() if k not in {"target_id", "status"}}


# The proposed exact-CPU gate (owner decision pending; default behaviour unchanged)


def test_a_processor_number_contradiction_is_only_a_warning_under_the_approved_policy() -> None:
    target = VIVO | {"cpu_model": "Core 5 210H"}
    result = ExactMatcher([target]).match(pcx("x"), ATTRIBUTES | {"cpu_model": "Core 5 220H"})
    # The contradiction is exposed: EXACT, with the warning and a CONFLICT evidence row.
    assert result.status is MatchStatus.EXACT
    assert "SECONDARY_WARNING_CPU_MODEL" in result.warnings


def test_the_opt_in_strict_gate_sends_the_same_offer_to_review() -> None:
    target = VIVO | {"cpu_model": "Core 5 210H"}
    strict = ExactMatcher([target], severity_overrides=STRICT_CPU_MODEL)
    result = strict.match(pcx("x"), ATTRIBUTES | {"cpu_model": "Core 5 220H"})
    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert "SECONDARY_CONFLICT_CPU_MODEL" in result.reason_codes
    agreeing = strict.match(pcx("x"), ATTRIBUTES | {"cpu_model": "Core 5 210H"})
    assert agreeing.status is MatchStatus.EXACT
    unstated = strict.match(pcx("x"), ATTRIBUTES)  # a missing value never blocks
    assert unstated.status is MatchStatus.EXACT


def test_strict_gate_separates_targets_that_differ_only_by_processor_number() -> None:
    one = VIVO | {"cpu_model": "Core 5 210H"}
    two = VIVO | {"target_id": "TGT-000032", "cpu_model": "Core 5 220H"}
    offer = ATTRIBUTES | {"cpu_model": "Core 5 220H"}
    assert ExactMatcher([one, two]).match(pcx("x"), offer).status is MatchStatus.AMBIGUOUS
    narrowed = ExactMatcher([one, two], severity_overrides=STRICT_CPU_MODEL).match(pcx("x"), offer)
    assert (narrowed.status, narrowed.target_id) == (MatchStatus.EXACT, "TGT-000032")


def test_invalid_severity_overrides_are_refused() -> None:
    with pytest.raises(ValueError):
        ExactMatcher([VIVO], severity_overrides={"price": "BLOCKING"})
    with pytest.raises(ValueError):
        ExactMatcher([VIVO], severity_overrides={"cpu_model": "IGNORE"})


# Audit traps


TUF = {"target_id": "TGT-000023", "status": "ACTIVE", "model": "FA607NUQ-RL011W",
       "screen_size_in": "16", "cpu_series": "Ryzen 7", "ram_capacity_gb": "16",
       "storage_capacity_gb": "512"}
TUF_SPECS = (("Processor", "AMD Ryzen 7 170"), ("Display", '16" FHD 144Hz'),
             ("Memory", "16GB DDR5"), ("Storage", "512GB M.2 PCIe NVMe SSD"))


def test_a_title_code_contradicted_by_the_detail_table_stays_in_review() -> None:
    """PC Express FA607NUQ-RL011W: the title and five specifications agree, but the detail
    table's model row says FA607NUG (audit 2026-10-21). A similar title never wins."""
    page = pcx("ASUS TUF Gaming A16 (FA607NUQ-RL011W) (90NR0QF3-M002X0) GeForce RTX 4050 AMD "
               "Ryzen 7 Laptop", specs=(("Model", "FA607NUG"), *TUF_SPECS),
               url="https://pcx.com.ph/products/asus-tuf-gaming-a16-fa607nuq-rl011w")
    assert "MODEL_CLAIM_CONFLICT" in notes(page)
    result = ExactMatcher([TUF]).match_observation(page)
    assert result.status is MatchStatus.REVIEW_REQUIRED and result.target_id is None


def test_a_title_naming_another_suffix_never_matches_the_target() -> None:
    page = pcx("ASUS TUF Gaming A16 (FA607NUG-RL011W) Laptop", specs=TUF_SPECS,
               url="https://pcx.com.ph/products/asus-tuf-gaming-a16-fa607nug-rl011w")
    assert ExactMatcher([TUF]).match_observation(page).status is not MatchStatus.EXACT


NEO = SearchTarget("TGT-000002", "2", "Apple", "", "MacBook Neo", "",
                   {"model": "MacBook Neo", "screen_size_in": "13", "cpu_series": "Apple A18 Pro",
                    "ram_capacity_gb": "8", "storage_capacity_gb": "512"})


@pytest.mark.parametrize("title", ["McbookNeo SILVER 512GB", "McbookNeo Citrus 512GB",
                                   "McbookNeo INDIGO 256GB", ""])
def test_pc_central_neo_options_never_choose_a_variant_or_its_price(title) -> None:
    """The saved PC Central MacBook Neo page lists six colour/storage options; an option title
    without RAM or storage words cannot bind the selected variant, so no variant (and no 'From'
    price) is assigned to either the 256GB or the 512GB configuration."""
    decision, _ = variant_decision(VariantInfo("48865301561511", "u", title, "InStock",
                                               "52999.0"), NEO)
    assert decision == "REVIEW"


def test_one_offer_pricing_several_targets_is_reported() -> None:
    url = "https://pcx.com.ph/products/hp-14-ep0445tu-d07c0pa"
    rows = [{"target_id": t, "retailer_code": "PCX", "price_status": "SELECTED",
             "offer_url": url} for t in ("TGT-000233", "TGT-000013")]
    rows.append({"target_id": "TGT-000001", "retailer_code": "PCX",
                 "price_status": "SELECTED", "offer_url": "https://pcx.com.ph/products/other"})
    assert shared_offers(rows) == {f"PCX {url}": ["TGT-000013", "TGT-000233"]}
    assert shared_offers(rows, {"TGT-000233"}) == {}


# Per-cell diagnostics


def comparison(statuses: dict[str, str]) -> list[dict[str, str]]:
    row = {"rank": "1", "target_id": "TGT-000001"}
    for code, _ in RETAILERS:
        row[f"{code}_Price_Status"] = statuses.get(code, "NO_RELEVANT_CANDIDATE")
    return [row]


def selections(**prices: str) -> list[dict[str, str]]:
    return [{"target_id": "TGT-000001", "retailer_code": code,
             "price_status": "SELECTED" if code in prices else "",
             "price": prices.get(code, ""), "offer_url": f"https://x/{code}" if code in prices
             else "", "selection_reason": "LOWEST_COMPARABLE" if code in prices else ""}
            for code, _ in RETAILERS]


def test_diagnostics_place_each_cell_at_its_furthest_stage() -> None:
    rows = diagnose_cells(
        comparison({"DBZ": "SELECTED", "LF": "NO_COMPARABLE_OFFER", "PCX": "NO_CANDIDATE_FOUND",
                    "PCC": "CANDIDATE_NOT_FETCHED"}),
        selections(DBZ="1000.00"),
        coverage=[{"target_id": "TGT-000001", "retailer_id": "oct",
                   "coverage_status": "DIFFERENT_CONFIGURATION",
                   "unconfirmed_candidate_price": "999.00"}],
        requests=[{"target_id": "TGT-000001", "retailer_id": "pcx", "outcome": "NO_RESULTS",
                   "hits": "0", "tier": "1_FULL_MODEL_CODE", "query": "X-1"},
                  {"target_id": "TGT-000001", "retailer_id": "datablitz",
                   "outcome": "NOT_SENT_QUERY_LIMIT", "hits": "0"}],
        candidates=[{"target_id": "TGT-000001", "retailer_id": "pcc", "candidate_rank": "1",
                     "category": "PRODUCT_NAME_MATCH",
                     "decision": "REVIEW_NO_MATCHING_VARIANT", "product_url": "https://p"},
                    {"target_id": "TGT-000001", "retailer_id": "pcc", "candidate_rank": "2",
                     "category": "PRODUCT_NAME_MATCH", "decision": "NOT_SELECTED_LIMIT",
                     "product_url": "https://q"}],
    )
    stages = {r["retailer_code"]: r["diagnostic_stage"] for r in rows}
    assert stages == {"DBZ": PUBLISHED, "LF": NOT_COMPARABLE, "PCX": ZERO_RESULTS,
                      "PCC": VARIANT_UNRESOLVED, "OCT": CONFIGURATION_MISMATCH}
    by_code = {r["retailer_code"]: r for r in rows}
    # Observed listing prices never become owner-policy comparable prices.
    assert by_code["OCT"]["observed_listing_price"] == "999.00"
    assert by_code["OCT"]["owner_policy_comparable_price"] == ""
    assert by_code["DBZ"]["owner_policy_comparable_price"] == "1000.00"
    assert "CANDIDATE_LIMIT" in by_code["PCC"]["budget_flags"]
    assert "QUERY_LIMIT" in by_code["DBZ"]["budget_flags"]
    assert stage_counts(rows)["TOTAL"][PUBLISHED] == 1


def test_results_without_a_relevant_candidate_are_not_zero_results() -> None:
    rows = diagnose_cells(comparison({}), selections(), requests=[
        {"target_id": "TGT-000001", "retailer_id": "oct", "outcome": "RESULTS", "hits": "10"},
        {"target_id": "TGT-000001", "retailer_id": "pcc", "outcome": "NOT_SENT_BUDGET_EXHAUSTED",
         "hits": "0"}])
    stages = {r["retailer_code"]: r["diagnostic_stage"] for r in rows}
    assert stages["OCT"] == NO_RELEVANT_CANDIDATE
    assert stages["PCC"] == NOT_SEARCHED  # planned, never sent: not a negative


def test_audit_claims_are_checked_not_trusted() -> None:
    rows = diagnose_cells(comparison({}), selections())
    product = "https://laptopfactory.com.ph/product/hp-laptop-14-ep0445tu/"
    audit = [
        {"Target ID": "TGT-000001", "Retailer": "Laptop Factory",
         "Evidence status": "VERIFIED_EXACT_LISTING", "Retailer URL": product},
        {"Target ID": "TGT-000002", "Retailer": "Laptop Factory",
         "Evidence status": "VERIFIED_EXACT_LISTING", "Retailer URL": product},
        {"Target ID": "TGT-000001", "Retailer": "DataBlitz",
         "Evidence status": "VERIFIED_EXACT_LISTING",
         "Retailer URL": "https://ecommerce.datablitz.com.ph/collections/pc-mac-laptops?page=6"},
    ]
    out = {r["retailer_id"]: r for r in overlay_audit(rows, audit,
                                                      models={"TGT-000001": "14-ep0445TU"})}
    assert out["laptop_factory"]["audit_url_shared_with_targets"] == "TGT-000002"
    assert out["laptop_factory"]["audit_url_shows_model_code"] == "TRUE"
    assert out["laptop_factory"]["audit_url_in_search_results"] == "FALSE"
    assert out["datablitz"]["audit_url_kind"] == "CATEGORY_PAGE"
    assert out["laptop_factory"]["diagnostic_stage"] == NO_RELEVANT_CANDIDATE  # unchanged
    assert url_kind("") == "NONE"
