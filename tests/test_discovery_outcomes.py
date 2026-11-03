"""Distinct terminal outcomes of a discovery job (2026-11-03).

The 2026-11-03 run ended 177 of 200 jobs as QUERY_PLAN_EXHAUSTED although they had ended for
different reasons. ``discovery_outcomes.classify_job`` keeps the reasons apart; search_jobs.csv and
discovery_summary.json carry the outcome per target and retailer for a diagnostics script.
"""

from __future__ import annotations

import json

import pytest
from scraping_fakes import FakeClock, no_network  # noqa: F401
from test_product_discovery import (
    JSON as SHOPIFY_JSON,
)
from test_product_discovery import (
    LF_PRODUCT,
    Routes,
    discover,
    html,
    inputs,
    lf_search,
    rows,
    shopify,
    standard_routes,
)

from laptop_tracker.discovery_outcomes import (
    BUDGET_EXHAUSTED,
    CLASS_EXCLUDED,
    CLASS_FETCH,
    CLASS_INCOMPLETE,
    CLASS_NO_RESULTS,
    CLASS_NOT_COLLECTED,
    CLASS_OTHER,
    CLASS_REJECTED,
    CLASS_SUITABLE,
    CLASS_UNRESOLVED,
    DISCOVERY_STEP_FAILED,
    FETCH_DEFERRED,
    FETCH_FAILED,
    NO_RESULTS_RETURNED,
    NOT_EVALUATED_SEARCH_ONLY,
    OUTCOME_CLASS,
    PAGE_FETCHED_IDENTITY_UNRESOLVED,
    PLAUSIBLE_CANDIDATE_NOT_COLLECTED,
    RESULTS_ALL_REJECTED,
    SEARCH_INCOMPLETE_PAGES_UNREAD,
    SEARCH_INCOMPLETE_RESULT_CAP,
    SUITABLE_OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS,
    SUITABLE_OFFER_EXCLUDED_BY_STOCK,
    SUITABLE_OFFER_FOUND,
    TERMINAL_OUTCOMES,
    CandidateFact,
    JobEvidence,
    OfferFact,
    classify_job,
    outcome_summary,
    rejection_reasons,
    result_classes,
)
from laptop_tracker.retailer_search import SEARCH_METHODS

pytestmark = pytest.mark.usefixtures("no_network")
LF = SEARCH_METHODS["laptop_factory"]
PCC = SEARCH_METHODS["pcc"]
ANSWERED = ("RESULTS",)
RELEVANT = CandidateFact("CODE_MATCH", "COLLECT", True)


def test_every_outcome_has_one_class() -> None:
    assert set(TERMINAL_OUTCOMES) == set(OUTCOME_CLASS)
    # The umbrella code the earlier column used is never a terminal outcome.
    assert "QUERY_PLAN_EXHAUSTED" not in TERMINAL_OUTCOMES


@pytest.mark.parametrize(
    ("evidence", "code", "outcome_class"),
    [
        # 1. every attempted query was answered and none listed a product
        (
            JobEvidence(search_outcomes=["NO_RESULTS", "NO_RESULTS"], legacy_outcome="QUERY_PLAN"),
            NO_RESULTS_RETURNED,
            CLASS_NO_RESULTS,
        ),
        # 2. results were found and every one was rejected
        (
            JobEvidence(
                search_outcomes=["RESULTS"],
                candidates=[
                    CandidateFact("ACCESSORY", "REJECTED_ACCESSORY"),
                    CandidateFact("NOT_A_LAPTOP", "REJECTED_PRODUCT_TYPE"),
                    CandidateFact("UNRELATED", "NOT_SELECTED"),
                ],
            ),
            RESULTS_ALL_REJECTED,
            CLASS_REJECTED,
        ),
        # 3. a capped answer, unread pages, a budget
        (
            JobEvidence(
                search_outcomes=["RESULTS"],
                capped=True,
                candidates=[CandidateFact("UNRELATED", "NOT_SELECTED")],
            ),
            SEARCH_INCOMPLETE_RESULT_CAP,
            CLASS_INCOMPLETE,
        ),
        (
            JobEvidence(search_outcomes=["RESULTS"], pages_unread=True),
            SEARCH_INCOMPLETE_PAGES_UNREAD,
            CLASS_INCOMPLETE,
        ),
        (
            JobEvidence(
                search_outcomes=["RESULTS", "NOT_SENT_BUDGET_EXHAUSTED"],
                legacy_outcome="BUDGET_EXHAUSTED",
            ),
            BUDGET_EXHAUSTED,
            CLASS_INCOMPLETE,
        ),
        # 4. a plausible candidate found and not collected
        (
            JobEvidence(
                search_outcomes=["RESULTS"],
                candidates=[CandidateFact("CODE_MATCH", "NOT_SELECTED_LIMIT", True)],
            ),
            PLAUSIBLE_CANDIDATE_NOT_COLLECTED,
            CLASS_NOT_COLLECTED,
        ),
        # 5. a fetch deferred, a fetch failed
        (
            JobEvidence(
                search_outcomes=["RESULTS"],
                candidates=[CandidateFact("CODE_MATCH", "NOT_COLLECTED_BUDGET", True)],
            ),
            FETCH_DEFERRED,
            CLASS_FETCH,
        ),
        (
            JobEvidence(
                search_outcomes=["RESULTS"],
                collected=True,
                candidates=[CandidateFact("CODE_MATCH", "COLLECT", True, "FAILED")],
            ),
            FETCH_FAILED,
            CLASS_FETCH,
        ),
        # 6. the page was fetched and its identity did not resolve
        (
            JobEvidence(
                search_outcomes=["RESULTS"],
                collected=True,
                offers=[OfferFact("https://x.test/p", "MODEL_UNVERIFIED", "IN_STOCK")],
            ),
            PAGE_FETCHED_IDENTITY_UNRESOLVED,
            CLASS_UNRESOLVED,
        ),
        (
            JobEvidence(
                search_outcomes=["RESULTS"],
                collected=True,
                candidates=[CandidateFact("CODE_MATCH", "COLLECT", True, "NO_OFFERS")],
            ),
            PAGE_FETCHED_IDENTITY_UNRESOLVED,
            CLASS_UNRESOLVED,
        ),
        # 7. confirmed, but not buyable as listed
        (
            JobEvidence(
                search_outcomes=["RESULTS"],
                collected=True,
                offers=[OfferFact("https://x.test/p", "MATCHED_CONFIGURATION", "OUT_OF_STOCK")],
            ),
            SUITABLE_OFFER_EXCLUDED_BY_STOCK,
            CLASS_EXCLUDED,
        ),
        (
            JobEvidence(
                search_outcomes=["RESULTS"],
                collected=True,
                offers=[
                    OfferFact(
                        "https://x.test/p",
                        "MATCHED_CONFIGURATION",
                        "IN_STOCK",
                        ("RESTRICTED_PRICE",),
                    )
                ],
            ),
            SUITABLE_OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS,
            CLASS_EXCLUDED,
        ),
        # success
        (
            JobEvidence(
                search_outcomes=["RESULTS"],
                collected=True,
                offers=[OfferFact("https://x.test/p", "MATCHED_CONFIGURATION", "IN_STOCK")],
            ),
            SUITABLE_OFFER_FOUND,
            CLASS_SUITABLE,
        ),
        # a search-only run that selected pages but collected none
        (
            JobEvidence(search_outcomes=["RESULTS"], manifest_rows=1, candidates=[RELEVANT]),
            NOT_EVALUATED_SEARCH_ONLY,
            CLASS_OTHER,
        ),
        (JobEvidence(step_failed=True), DISCOVERY_STEP_FAILED, CLASS_OTHER),
    ],
)
def test_each_terminal_state_has_its_own_outcome(evidence, code, outcome_class) -> None:
    outcome = classify_job(evidence)
    assert (outcome.code, outcome.outcome_class) == (code, outcome_class)
    assert outcome.code != "QUERY_PLAN_EXHAUSTED"


def test_a_buyable_offer_wins_over_an_excluded_one_and_stock_is_separate_from_terms() -> None:
    offers = [
        OfferFact("https://x.test/a", "MATCHED_CONFIGURATION", "OUT_OF_STOCK"),
        OfferFact("https://x.test/b", "MATCHED_CONFIGURATION", "IN_STOCK"),
    ]
    assert classify_job(JobEvidence(offers=offers)).code == SUITABLE_OFFER_FOUND
    # In stock but blocked by its terms is a purchase-condition exclusion, not a stock one.
    blocked = [
        OfferFact("https://x.test/c", "MATCHED_CONFIGURATION", "IN_STOCK", ("PAID_PACKAGE",)),
        OfferFact("https://x.test/d", "MATCHED_CONFIGURATION", "OUT_OF_STOCK"),
    ]
    assert classify_job(JobEvidence(offers=blocked)).code == (
        SUITABLE_OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS
    )


def test_a_capped_search_is_never_reported_as_no_results_or_all_rejected() -> None:
    capped = JobEvidence(
        search_outcomes=["NO_RESULTS", "RESULTS"],
        capped=True,
        candidates=[CandidateFact("UNRELATED", "NOT_SELECTED")] * 10,
    )
    assert classify_job(capped).code == SEARCH_INCOMPLETE_RESULT_CAP
    # The result classes are still counted, so the diagnostics see what filled the cap.
    assert classify_job(capped).result_classes == "UNRELATED:10"


def test_result_and_rejection_classes_are_counted_by_reason() -> None:
    facts = [
        CandidateFact("NOT_A_LAPTOP", "REJECTED_PRODUCT_TYPE"),
        CandidateFact("NOT_A_LAPTOP", "REJECTED_PRODUCT_TYPE"),
        CandidateFact("ACCESSORY", "REJECTED_ACCESSORY"),
        CandidateFact("DIFFERENT_MODEL_CODE", "SKIPPED_TOO_MANY_TITLE_DIFFERENCES", True),
        CandidateFact("UNRELATED", "NOT_SELECTED"),
        CandidateFact("SUPPLIED_URL", "COLLECT", True),
    ]
    assert result_classes(facts) == (
        "ACCESSORY:1;DIFFERENT_MODEL_CODE:1;NOT_A_LAPTOP:2;SUPPLIED_URL:1;UNRELATED:1"
    )
    reasons = rejection_reasons(facts, [OfferFact("u", "DIFFERENT_CONFIGURATION")])
    assert reasons == (
        "ACCESSORY:1;NOT_A_LAPTOP:2;PAGE_DIFFERENT_CONFIGURATION:1;"
        "TOO_MANY_TITLE_DIFFERENCES:1;UNRELATED:1"
    )


def test_outcome_summary_groups_by_outcome_and_class() -> None:
    rows_ = [{"terminal_outcome": "NO_RESULTS_RETURNED", "outcome_class": CLASS_NO_RESULTS}] * 2
    rows_ += [{"terminal_outcome": "FETCH_FAILED", "outcome_class": CLASS_FETCH}]
    summary = outcome_summary(rows_)
    assert summary["by_terminal_outcome"] == {"FETCH_FAILED": 1, "NO_RESULTS_RETURNED": 2}
    assert summary["by_outcome_class"] == {CLASS_FETCH: 1, CLASS_NO_RESULTS: 2}


# The command: search_jobs.csv and discovery_summary.json carry the outcome per job


def run_discovery(
    tmp_path, routes, *extra, targets="TGT-000001", retailers="laptop_factory", mode="--live"
):
    transport = Routes(routes)
    out = tmp_path / "discovery"
    code = discover.main(
        [
            mode,
            "--targets",
            targets,
            "--retailers",
            retailers,
            *inputs(tmp_path),
            "--output-dir",
            str(out),
            "--max-total-requests",
            "30",
            "--max-identifier-variant-queries",
            "0",
            *extra,
        ],
        transport=transport,
        clock=FakeClock(),
    )
    return code, transport, out


def job_row(out, target="TGT-000001", retailer="laptop_factory"):
    return next(
        r
        for r in rows(out / "search_jobs.csv")
        if (r["target_id"], r["retailer_id"]) == (target, retailer)
    )


def test_no_result_job_says_no_results_returned_not_query_plan_exhausted(tmp_path) -> None:
    routes = standard_routes()
    code_url = LF.first_page_url("A14-11M-X3BP")
    routes[code_url] = html(code_url, lf_search(no_results=True))
    _, _, out = run_discovery(tmp_path, routes)
    row = job_row(out)
    # The earlier column keeps its own vocabulary (a search-only run never collected anything).
    assert row["discovery_outcome"] == "NOT_EVALUATED_SEARCH_ONLY"
    assert (row["terminal_outcome"], row["outcome_class"]) == (
        NO_RESULTS_RETURNED,
        CLASS_NO_RESULTS,
    )
    assert row["search_completeness"] == "COMPLETE_FOR_QUERY"
    summary = json.loads((out / "discovery_summary.json").read_text())
    assert summary["job_outcomes"][0]["terminal_outcome"] == NO_RESULTS_RETURNED
    assert summary["terminal_outcomes"]["by_terminal_outcome"] == {NO_RESULTS_RETURNED: 1}
    assert summary["terminal_outcomes"]["by_outcome_class"] == {CLASS_NO_RESULTS: 1}


def test_a_job_whose_results_were_all_rejected_names_the_rejection_classes(tmp_path) -> None:
    routes = standard_routes()
    code_url = LF.first_page_url("A14-11M-X3BP")
    routes[code_url] = html(
        code_url,
        lf_search(
            [
                ("/product/tomtoc-sleeve-for-macbook-air-13/", "Tomtoc Sleeve for MacBook Air 13"),
                ("/product/yoga-tab-zag60109ph/", "Lenovo Yoga Tab ZAG60109PH Gen 3 12GB 256GB"),
                ("/product/acer-aspire-ai-14-a14-11m-x9zz/", "Acer Aspire AI 14 A14-11M-X9ZZ"),
            ]
        ),
    )
    _, _, out = run_discovery(tmp_path, routes)
    row = job_row(out)
    assert row["terminal_outcome"] == RESULTS_ALL_REJECTED
    assert "ACCESSORY:1" in row["rejection_reasons"]
    assert "NOT_A_LAPTOP:1" in row["rejection_reasons"]
    assert "DIFFERENT_MODEL_CODE:1" in row["rejection_reasons"]
    # The rejected results never reach a page request (nothing but searches was sent).
    assert not [r for r in rows(out / "manifest.csv")]


def test_a_job_that_hit_the_predictive_cap_is_incomplete_not_empty(tmp_path) -> None:
    ten = shopify(*[(f"acer-nitro-{n}", f"Acer Nitro {n} Laptop") for n in range(10)])
    routes = {}
    for query in (
        "A14-11M-X3BP",
        "A14-11M-X3BP 16GB 512GB",
        "ExampleBrand ExampleBook",
        "ExampleBrand ExampleBook Core 5 16GB 512GB",
        "ExampleBook",
        "ExampleBook Snapdragon X 16GB 512GB",
    ):
        url = PCC.first_page_url(query)
        routes[url] = TransportResponseJson(url, ten)
    _, _, out = run_discovery(tmp_path, routes, retailers="pcc")
    row = job_row(out, retailer="pcc")
    assert row["search_coverage"].startswith("RESULT_CAP_REACHED")
    assert row["terminal_outcome"] == SEARCH_INCOMPLETE_RESULT_CAP
    assert row["search_completeness"] == "INCOMPLETE_RESULT_CAP"
    requests = rows(out / "search_requests.csv")
    assert {r["completeness"] for r in requests} == {"INCOMPLETE_RESULT_CAP"}
    assert {r["route"] for r in requests} == {"NATIVE"}


def TransportResponseJson(url, body):  # noqa: N802 - a test helper that reads like a constructor
    from laptop_tracker.scraping import TransportResponse

    return TransportResponse(200, url, body.encode(), SHOPIFY_JSON)


def test_a_failing_candidate_step_keeps_its_results_and_says_so(tmp_path, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise TypeError("'VariantEvaluation' object is not subscriptable")

    monkeypatch.setattr(discover, "select_and_inspect", broken)
    _, _, out = run_discovery(tmp_path, standard_routes())
    row = job_row(out)
    assert row["terminal_outcome"] == DISCOVERY_STEP_FAILED
    candidates = rows(out / "candidates.csv")
    # Not one result of the job was dropped: they are listed, marked, and kept for review.
    assert candidates and {c["decision"] for c in candidates} == {"STEP_FAILED"}
    assert LF_PRODUCT in {c["product_url"] for c in candidates}
    inspections = rows(out / "product_inspections.csv")
    assert [r["outcome"] for r in inspections] == ["STEP_FAILED"]
