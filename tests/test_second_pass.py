"""Second-pass collection of candidates a per-round limit skipped (2026-10-22, opt-in).

On 2026-10-20 two Laptop Factory family listings (hp-laptop-15-fc0826au, hp-laptop-15-fc0756au)
were relevant but never fetched: ``--max-candidates-per-target-retailer`` stopped at two. A second
pass collects such skipped candidates only for configurations still without a suitable offer,
within the same fetch budget and rounds, instead of raising the limit for every configuration.
"""

from __future__ import annotations

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_acceptance_fixes import CODE_URL, EIGHT_PAGE, SPEC_URL, eight_gb_page, job
from test_discovery_continuation import run
from test_product_discovery import LF_PRODUCT, html, lf_search, rows, standard_routes

pytestmark = pytest.mark.usefixtures("no_network")
# Neither title shows the code: both are CODE_IN_URL_ONLY candidates, collectable but not
# plausible, so the searches run to the end and the per-round limit decides what is fetched.
PLAIN = "Acer Aspire AI 14 Laptop"


def routes():
    served = standard_routes()
    served[CODE_URL] = html(CODE_URL, lf_search([(EIGHT_PAGE, PLAIN), (LF_PRODUCT, PLAIN)]))
    served[SPEC_URL] = html(SPEC_URL, lf_search(no_results=True))
    served[EIGHT_PAGE] = eight_gb_page()
    return served


def test_without_a_second_pass_the_skipped_candidate_stays_unfetched(tmp_path) -> None:
    code, transport, out = run(tmp_path, routes(), "--collect", "--max-total-requests", "20",
                               "--max-candidates-per-target-retailer", "1")
    assert code in {0, 2}
    row = job(out)
    assert row["discovery_outcome"] != "SUITABLE_OFFER_FOUND" and row["second_pass"] == ""
    assert LF_PRODUCT not in transport.calls
    candidates = {r["product_url"]: r for r in rows(out / "candidates.csv")}
    assert candidates[LF_PRODUCT]["decision"] == "NOT_SELECTED_LIMIT"


def test_a_second_pass_collects_the_skipped_candidate_once(tmp_path) -> None:
    code, transport, out = run(tmp_path, routes(), "--collect", "--max-total-requests", "20",
                               "--max-candidates-per-target-retailer", "1",
                               "--max-second-pass-candidates-per-target-retailer", "1")
    assert code == 0
    row = job(out)
    assert row["second_pass"] == ("round 2: up to 1 of 1 relevant candidate(s) skipped by the "
                                  "per-round limit")
    assert (row["discovery_outcome"], row["suitable_offer_url"]) == (
        "SUITABLE_OFFER_FOUND", LF_PRODUCT)
    assert transport.calls.count(EIGHT_PAGE) == 1 and transport.calls.count(LF_PRODUCT) == 1
    candidates = {r["product_url"]: r for r in rows(out / "candidates.csv")}
    assert candidates[LF_PRODUCT]["decision"] == "COLLECT"
    results = {r["offer_url"]: r for r in rows(out / "collection" / "match_results.csv")}
    assert results[EIGHT_PAGE]["match_status"] != "EXACT"  # its own outcome is kept
    assert (results[LF_PRODUCT]["match_status"], results[LF_PRODUCT]["target_id"]) == (
        "EXACT", "TGT-000001")


def test_the_round_limit_makes_an_unrun_second_pass_explicit(tmp_path) -> None:
    _, transport, out = run(tmp_path, routes(), "--collect", "--max-total-requests", "20",
                            "--max-candidates-per-target-retailer", "1",
                            "--max-second-pass-candidates-per-target-retailer", "1",
                            "--max-discovery-rounds", "1")
    assert job(out)["second_pass"].startswith("NOT_RUN: --max-discovery-rounds reached")
    assert LF_PRODUCT not in transport.calls


def test_a_candidate_whose_title_states_another_configuration_gets_no_second_pass(
    tmp_path,
) -> None:
    served = routes()
    served[CODE_URL] = html(CODE_URL, lf_search([
        (EIGHT_PAGE, PLAIN), (LF_PRODUCT, "Acer Aspire AI 14 Laptop 8GB RAM 256GB SSD")]))
    _, transport, out = run(tmp_path, served, "--collect", "--max-total-requests", "20",
                            "--max-candidates-per-target-retailer", "1",
                            "--max-second-pass-candidates-per-target-retailer", "1")
    assert LF_PRODUCT not in transport.calls and job(out)["second_pass"] == ""
