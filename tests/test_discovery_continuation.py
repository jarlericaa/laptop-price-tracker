"""Discovery continuation, stop reasons, caching, and historical seeds (2026-10-19).

Synthetic responses through the routed fake transport; the no-network guard is active. A search
result is a candidate only: identity is decided by collection and matching.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, no_network  # noqa: F401
from test_collection import write_dicts
from test_product_discovery import (
    LF_PRODUCT,
    Routes,
    discover,
    html,
    inputs,
    lf_search,
    rows,
    standard_routes,
)

from laptop_tracker.adapters.laptop_factory import HOST as LF_HOST
from laptop_tracker.retailer_search import (
    SEARCH_METHODS,
    TIER_FAMILY,
    TIER_MODEL_CODE,
    TIER_MODEL_SPECS,
)

pytestmark = pytest.mark.usefixtures("no_network")
LF = SEARCH_METHODS["laptop_factory"]
EIGHT = f"https://{LF_HOST}/product/acer-aspire-ai-14-a14-11m-x3bp-8gb/"
OPI = f"https://{LF_HOST}/product/acer-aspire-ai-14-a14-11m-x3bp-opi/"


def run(tmp_path: Path, routes, *extra: str, targets="TGT-000001", out="discovery"):
    transport = Routes(routes)
    output = tmp_path / out
    # These scripted routes predate the identifier-spelling queries (2026-10-22): they are off
    # unless a test passes --max-identifier-variant-queries itself (the later value wins).
    code = discover.main(
        ["--live", "--targets", targets, "--retailers", "laptop_factory", *inputs(tmp_path),
         "--output-dir", str(output), "--max-identifier-variant-queries", "0", *extra],
        transport=transport, clock=FakeClock(),
    )
    return code, transport, output


def job(out: Path, target="TGT-000001") -> dict[str, str]:
    return next(r for r in rows(out / "search_jobs.csv") if r["target_id"] == target)


def test_a_plausible_code_result_stops_the_fallbacks(tmp_path) -> None:
    code, _, out = run(tmp_path, standard_routes(), "--max-total-requests", "10")
    assert code == 0
    row = job(out)
    assert (row["stop_reason"], row["tiers_run"]) == ("PLAUSIBLE_CANDIDATE_FOUND",
                                                       TIER_MODEL_CODE)
    assert row["plausible_candidate_url"] == LF_PRODUCT
    first = rows(out / "search_requests.csv")[0]
    assert first["run_reason"].startswith("first query: complete model code")


@pytest.mark.parametrize(
    ("title", "url", "summary"),
    [
        ("Acer Aspire AI 14 A14-11M-X3BP 8GB RAM 256GB SSD", EIGHT,
         "same code, other configuration x1"),
        ("Acer Aspire AI 14 A14-11M-X3BP OPI", OPI, "unresolved code match x1"),
    ],
)
def test_other_configurations_and_unresolved_candidates_keep_the_search_going(
    tmp_path, title, url, summary
) -> None:
    routes = standard_routes()
    code_url = LF.first_page_url("A14-11M-X3BP")
    routes[code_url] = html(code_url, lf_search([(url, title)]))
    spec_url = LF.first_page_url("A14-11M-X3BP 16GB 512GB")
    routes[spec_url] = html(spec_url, lf_search([(LF_PRODUCT, "Acer Aspire AI 14 A14-11M-X3BP")]))
    routes[url] = routes[LF_PRODUCT]  # the candidate page is fetched by collection only
    _, _, out = run(tmp_path, routes, "--max-total-requests", "10")
    row = job(out)
    assert row["tiers_run"] == f"{TIER_MODEL_CODE};{TIER_MODEL_SPECS}"
    assert row["stop_reason"] == "PLAUSIBLE_CANDIDATE_FOUND"
    spec = next(r for r in rows(out / "search_requests.csv") if r["tier"] == TIER_MODEL_SPECS)
    assert summary in spec["run_reason"]
    candidates = {r["product_url"]: r for r in rows(out / "candidates.csv")}
    # The first result is retained and explained, not discarded.
    assert candidates[url]["plausible"] == "FALSE"
    if "8GB" in title:
        assert candidates[url]["configuration_difference"] == (
            "ram_capacity_gb: title 8 vs target 16; storage_capacity_gb: title 256 vs target 512"
        )
        assert candidates[url]["decision"] == "COLLECT_CHECK_CONFIGURATION"


def test_all_tiers_tried_without_a_plausible_candidate_is_recorded(tmp_path) -> None:
    routes = standard_routes()
    code_url = LF.first_page_url("A14-11M-X3BP")
    routes[code_url] = html(code_url, lf_search(no_results=True))
    _, _, out = run(tmp_path, routes, "--max-total-requests", "10")
    row = job(out)
    assert row["stop_reason"] == "ALL_QUERY_TIERS_TRIED"
    assert row["queries_run"] == "4"
    family = next(r for r in rows(out / "search_requests.csv") if r["tier"] == TIER_FAMILY)
    assert "no results so far" in family["run_reason"]


def test_query_limit_is_configurable_and_recorded(tmp_path) -> None:
    routes = standard_routes()
    code_url = LF.first_page_url("A14-11M-X3BP")
    routes[code_url] = html(code_url, lf_search(no_results=True))
    _, transport, out = run(tmp_path, routes, "--max-total-requests", "10",
                            "--max-queries-per-target-retailer", "1")
    assert transport.calls == [code_url]
    row = job(out)
    assert (row["stop_reason"], row["pages_not_sent"]) == ("QUERY_LIMIT_PER_TARGET_RETAILER", "3")
    outcomes = {r["tier"]: r["outcome"] for r in rows(out / "search_requests.csv")}
    assert outcomes[TIER_FAMILY] == "NOT_SENT_QUERY_LIMIT"


def test_identical_searches_are_answered_once_per_run(tmp_path) -> None:
    # Two catalog configurations share one model code (an 8GB and a 16GB MSI, for example):
    # the code query is sent once and reused; each configuration keeps its own record.
    from argparse import Namespace

    from laptop_tracker.product_discovery import SearchTarget
    from laptop_tracker.retailer_search import BudgetedTransport
    from laptop_tracker.scraping import FetchConfig, Fetcher

    def target(target_id: str, ram: str) -> SearchTarget:
        return SearchTarget(target_id, "1", "MSI", "Thin", "B13UC-3474PH", "",
                            {"model": "B13UC-3474PH", "screen_size_in": "15.6",
                             "cpu_series": "Core i5", "ram_capacity_gb": ram,
                             "storage_capacity_gb": "512"})

    code_url = LF.first_page_url("B13UC-3474PH")
    page = f"https://{LF_HOST}/product/msi-thin-15-b13uc-3474ph/"
    transport = Routes({code_url: html(code_url, lf_search(
        [(page, "MSI Thin 15 B13UC-3474PH 16GB RAM 512GB SSD")]))})
    budget = BudgetedTransport(transport, 10)
    eight, sixteen = (discover.SearchJob(target("TGT-000219", "8"), "laptop_factory", 1),
                      discover.SearchJob(target("TGT-000258", "16"), "laptop_factory", 1))
    eight.queries = eight.all_queries = eight.queries[:1]  # code tier only, for this check
    args = Namespace(max_search_requests=10, replay_only=False)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    sent = discover.run_searches([eight, sixteen], Fetcher(budget, FetchConfig(), FakeClock()),
                                 budget, args, evidence, lambda _: None)
    assert (sent, transport.calls) == (1, [code_url])
    assert [r.cache for r in sixteen.records][:1] == ["SAME_RUN"]
    # The 16GB configuration finds a plausible page; the 8GB one does not (title says 16GB).
    assert sixteen.stop_reason == "PLAUSIBLE_CANDIDATE_FOUND"
    assert eight.stop_reason == "ALL_QUERY_TIERS_TRIED"


def test_saved_responses_replay_without_any_request(tmp_path) -> None:
    _, first_transport, first = run(tmp_path, standard_routes(), "--max-total-requests", "10")
    assert first_transport.calls
    code, transport, replay = run(
        tmp_path, {}, "--max-total-requests", "1", "--search-cache", str(first),
        "--replay-only", out="replay",
    )
    assert code == 0 and transport.calls == []
    searches = rows(replay / "search_requests.csv")
    assert searches[0]["cache"] == f"SAVED:{first}"
    assert {r["product_url"] for r in rows(replay / "candidates.csv")} == {
        r["product_url"] for r in rows(first / "candidates.csv")
    }
    summary = json.loads((replay / "discovery_summary.json").read_text())
    assert summary["run_status"] == "REPLAY_ONLY" and summary["requests_sent"] == 0


def test_replay_only_refuses_collection(tmp_path) -> None:
    with pytest.raises(SystemExit):
        run(tmp_path, {}, "--max-total-requests", "1", "--search-cache", str(tmp_path),
            "--replay-only", "--collect")


def test_historical_verified_urls_seed_candidates_without_assigning_them(tmp_path) -> None:
    # Laptop Factory search omits out-of-stock pages; an earlier verified URL is re-checked.
    routes = standard_routes()
    code_url = LF.first_page_url("A14-11M-X3BP")
    routes[code_url] = html(code_url, lf_search(no_results=True))
    seeds = write_dicts(
        tmp_path / "earlier_match_results.csv",
        ("retailer_id", "offer_url", "target_id", "match_status"),
        [{"retailer_id": "laptop_factory", "offer_url": LF_PRODUCT, "target_id": "TGT-000001",
          "match_status": "EXACT"},
         {"retailer_id": "laptop_factory", "offer_url": EIGHT, "target_id": "",
          "match_status": "REVIEW_REQUIRED"}],
    )
    code, _, out = run(tmp_path, routes, "--max-total-requests", "20", "--collect",
                       "--seed-urls", str(seeds))
    assert code == 0
    candidates = {r["product_url"]: r for r in rows(out / "candidates.csv")}
    assert candidates[LF_PRODUCT]["category"] == "HISTORICAL_VERIFIED_URL"
    assert candidates[LF_PRODUCT]["decision"] == "COLLECT"
    assert EIGHT not in candidates  # only verified (EXACT) rows seed
    assert job(out)["historical_seeds"] == "1"
    # Collection re-matches the page; the seed itself assigns nothing.
    results = rows(out / "collection" / "match_results.csv")
    assert [(r["expected_target_id"], r["match_status"]) for r in results] == [
        ("TGT-000001", "EXACT")
    ]


def test_rate_limit_cannot_be_lowered(tmp_path) -> None:
    with pytest.raises(SystemExit):
        run(tmp_path, {}, "--max-total-requests", "1", "--min-seconds-between-requests", "1")
