"""Coverage ledger (2026-10-19): every blank comparison cell has a current, specific reason.

Synthetic records only. Search context is kept apart from the confirmed target assignment, and
unconfirmed prices never fill the confirmed price column.
"""

from __future__ import annotations

import pytest
from scraping_fakes import no_network  # noqa: F401

from laptop_tracker.matching import SAME_MODEL_DIFFERENT_CONFIGURATION
from laptop_tracker.pilot_coverage import (
    CANDIDATE_NOT_FETCHED,
    CONFIRMED_OUTSIDE_SCOPE,
    COVERAGE_COLUMNS,
    DISCOVERY_NOT_ATTEMPTED,
    EXACT_PRICE_EXCLUDED,
    FETCH_FAILED,
    NO_CANDIDATE_FOUND,
    PRICE_AVAILABLE,
    SEARCH_FAILED,
    OfferRecord,
    coverage,
)

pytestmark = pytest.mark.usefixtures("no_network")

T, R = "TGT-000059", "pcc"
URL = "https://pcc.example.test/products/acer-nitro-v-anv15-42-r2auh"
CORE = {"model": "ANV15-42-R2UH", "screen_size_in": "15.6", "cpu_series": "Ryzen 5",
        "ram_capacity_gb": "16", "storage_capacity_gb": "512"}
TARGETS = {T: CORE, "TGT-000219": CORE | {"ram_capacity_gb": "8"}}
PLAN = [{"target_id": T, "retailer_id": R, "query": "ANV15-42-R2UH"}]


def record(**changes) -> OfferRecord:
    values = dict(
        retailer_id=R, offer_url=URL, observation_id="o1",
        retrieved_at="2026-10-19T06:00:00.000000Z", match_status="EXACT", target_id=T,
        candidate_target_ids=(T,), reason_codes=(), price="55995.00", availability="IN_STOCK",
        price_eligibility="COMPARABLE", offer_attributes=CORE,
    )
    return OfferRecord(**(values | changes))


def ledger(offers=(), **logs):
    rows, details = coverage([T], [R], list(offers), target_rows=TARGETS, **logs)
    return next(row for row in rows if row["target_id"] == T), rows, details


def searched(outcome="RESULTS", **extra):
    return [{"target_id": T, "retailer_id": R, "outcome": outcome, **extra}]


def test_columns_are_stable_and_extended() -> None:
    row, _, _ = ledger([record()])
    assert list(row) == list(COVERAGE_COLUMNS)


@pytest.mark.parametrize(
    ("logs", "status", "phrase"),
    [
        ({"search_plan": PLAN, "search_requests": searched("NOT_SENT_SEARCH_LIMIT")},
         DISCOVERY_NOT_ATTEMPTED, "NOT_SENT_SEARCH_LIMIT"),
        ({"search_plan": PLAN, "search_requests": []}, DISCOVERY_NOT_ATTEMPTED, "planned"),
        ({"search_plan": PLAN,
          "search_requests": searched("SEARCH_ERROR", message="HTTP_403: blocked")},
         SEARCH_FAILED, "HTTP_403"),
        ({"search_plan": PLAN, "search_requests": searched("NO_RESULTS")},
         NO_CANDIDATE_FOUND, "Searched; no candidate found"),
        ({"search_plan": PLAN, "search_requests": searched(),
          "candidates": [{"target_id": T, "retailer_id": R, "category": "CODE_MATCH",
                          "decision": "NOT_INSPECTED_BUDGET", "product_url": URL}]},
         CANDIDATE_NOT_FETCHED, "not fetched"),
        ({"manifest": [{"request_id": "r1", "retailer_id": R, "expected_target_id": T,
                        "product_url": URL}],
          "request_report": [{"request_id": "r1", "request_status": "FAILED",
                              "failure_codes": "HTTP_503"}]},
         FETCH_FAILED, "HTTP_503"),
    ],
)
def test_discovery_stages_are_distinct(logs, status, phrase) -> None:
    row, _, _ = ledger(**logs)
    assert row["coverage_status"] == status
    assert phrase in row["current_reason"]
    assert row["confirmed_price"] == row["unconfirmed_candidate_price"] == ""


def test_unsent_searches_are_not_reported_as_no_candidate() -> None:
    # A search that was never sent (budget or limit) proves nothing about the retailer.
    row, _, _ = ledger(search_plan=PLAN, search_requests=searched("NOT_SENT_BUDGET_EXHAUSTED"))
    assert row["coverage_status"] == DISCOVERY_NOT_ATTEMPTED
    assert (row["searches_executed"], row["searches_not_sent"]) == ("0", "1")


@pytest.mark.parametrize(
    ("changes", "status", "phrase"),
    [
        ({"match_status": "REVIEW_REQUIRED", "target_id": "", "candidate_target_ids": (),
          "reason_codes": ("MISSING_CORE_FIELDS", "MISSING_RAM_CAPACITY_GB"),
          "offer_attributes": {k: v for k, v in CORE.items() if k != "ram_capacity_gb"},
          "price_eligibility": "NOT_PRICED: MATCH_REVIEW_REQUIRED", "expected_target_id": T},
         "EXTRACTION_INCOMPLETE", "MISSING_RAM_CAPACITY_GB"),
        ({"match_status": "REVIEW_REQUIRED", "target_id": "", "candidate_target_ids": (),
          "reason_codes": ("MODEL_CLAIM_CONFLICT", "MISSING_MODEL"),
          "price_eligibility": "NOT_PRICED: MATCH_REVIEW_REQUIRED", "expected_target_id": T},
         "IDENTITY_CONFLICT", "MODEL_CLAIM_CONFLICT"),
        ({"match_status": "UNMATCHED", "target_id": "", "candidate_target_ids": (T,),
          "reason_codes": ("NO_CORE_MATCH", "ONE_CORE_FIELD_DIFFERS"),
          "offer_attributes": CORE | {"ram_capacity_gb": "32"},
          "price_eligibility": "NOT_PRICED: MATCH_UNMATCHED"},
         SAME_MODEL_DIFFERENT_CONFIGURATION, "ram_capacity_gb: offer 32 vs target 16"),
        ({"price_eligibility": "NOT_COMPARABLE: PAID_PACKAGE_PRICE"},
         EXACT_PRICE_EXCLUDED, "PAID_PACKAGE_PRICE"),
        ({}, PRICE_AVAILABLE, "Comparable price 55995.00"),
    ],
)
def test_offer_outcomes_give_a_current_reason_and_link(changes, status, phrase) -> None:
    row, _, details = ledger([record(**changes)])
    assert row["coverage_status"] == status
    assert phrase in row["current_reason"]
    assert URL in row["current_reason"]
    confirmed = status in {PRICE_AVAILABLE, EXACT_PRICE_EXCLUDED}
    # Unconfirmed prices stay in their own column, never in the confirmed price.
    assert row["confirmed_price"] == ("55995.00" if confirmed else "")
    assert row["best_offer_price"] == ("55995.00" if confirmed else "")
    assert row["unconfirmed_candidate_price"] == ("" if confirmed else "55995.00")
    assert row["candidate_url"] == ("" if confirmed else URL)
    assert details[0]["price_role"] == ("CONFIRMED" if confirmed else "UNCONFIRMED_NOT_COMPARED")


def test_confirmed_match_for_another_configuration_is_explained_on_both_rows() -> None:
    eight = record(target_id="TGT-000219", candidate_target_ids=("TGT-000219",),
                   offer_attributes=CORE | {"ram_capacity_gb": "8"}, expected_target_id=T)
    row, rows, _ = ledger([eight])
    assert row["coverage_status"] == SAME_MODEL_DIFFERENT_CONFIGURATION
    assert row["differing_fields"] == "ram_capacity_gb: offer 8 vs target 16"
    assert "confirmed as TGT-000219" in row["current_reason"]
    outside = next(r for r in rows if r["target_id"] == "TGT-000219")
    assert (outside["coverage_status"], outside["in_reporting_scope"]) == (
        CONFIRMED_OUTSIDE_SCOPE, "FALSE"
    )
    assert outside["confirmed_price"] == ""  # outside scope: not a comparison value


def test_search_context_is_separate_from_assignment() -> None:
    unresolved = record(match_status="REVIEW_REQUIRED", target_id="", candidate_target_ids=(),
                        reason_codes=("MISSING_CORE_FIELDS", "MISSING_MODEL"),
                        price_eligibility="NOT_PRICED: MATCH_REVIEW_REQUIRED")
    candidates = [{"target_id": T, "retailer_id": R, "category": "CODE_MATCH",
                   "decision": "COLLECT_VARIANT", "product_url": URL + "?variant=1"}]
    row, _, details = ledger([unresolved], search_plan=PLAN, search_requests=searched(),
                             candidates=candidates)
    # An unresolved offer is not "not observed" just because no target was assigned.
    assert row["coverage_status"] == "MODEL_UNVERIFIED"
    assert details[0]["relation"] == "SEARCHED_FOR"
    assert details[0]["assigned_target_id"] == ""


def test_history_is_kept_while_the_current_observation_decides() -> None:
    old = record(observation_id="o0", retrieved_at="2026-10-13T06:00:00.000000Z")
    new = record(observation_id="o1", match_status="REVIEW_REQUIRED", target_id="",
                 candidate_target_ids=(), reason_codes=("MISSING_CORE_FIELDS", "MISSING_MODEL"),
                 price_eligibility="NOT_PRICED: MATCH_REVIEW_REQUIRED", expected_target_id=T)
    row, _, details = ledger([old, new])
    assert row["coverage_status"] == "MODEL_UNVERIFIED"
    assert (row["current_offers"], row["historical_observations"]) == ("1", "1")
    assert {d["observation_id"]: d["is_current"] for d in details} == {"o0": "FALSE", "o1": "TRUE"}


def test_search_stop_reason_is_shown() -> None:
    jobs = [{"target_id": T, "retailer_id": R, "stop_reason": "ALL_QUERY_TIERS_TRIED"}]
    row, _, _ = ledger(search_plan=PLAN, search_requests=searched("NO_RESULTS"), search_jobs=jobs)
    assert row["search_stop_reason"] == "ALL_QUERY_TIERS_TRIED"
