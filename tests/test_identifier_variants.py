"""Identifier spellings and the no-series fallback in retailer search (2026-10-22).

On 2026-10-20 complete hyphenated codes (B12UC-1432PH, E1504FA-NJ1527W, 14-ep0445TU, ...) returned
no result at all on Laptop Factory and PC Express although the stores list those products. The
job then retries the same verified code with spaces, then its most distinctive segment. These
are search text only: a result is plausible only when its title shows the complete code.
"""

from __future__ import annotations

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_discovery_continuation import LF, job, run
from test_product_discovery import LF_PRODUCT, html, lf_search, rows, standard_routes

from laptop_tracker.retailer_search import (
    TIER_BRAND_CPU_SPECS,
    TIER_CODE_SEGMENT,
    TIER_FAMILY,
    TIER_MODEL_CODE,
    TIER_MODEL_CODE_SPACED,
    TIER_MODEL_SPECS,
    TIER_PART_NUMBER,
    distinctive_cpu_model,
    distinctive_segment,
    search_queries,
    spaced_code,
)

pytestmark = pytest.mark.usefixtures("no_network")
CORE = {"cpu_series": "Core i3", "ram_capacity_gb": "8", "storage_capacity_gb": "512"}


@pytest.mark.parametrize(("code", "spaced", "segment"), [
    ("B12UC-1432PH", "B12UC 1432PH", "1432PH"),
    ("14-ep0445TU", "14 ep0445TU", "ep0445TU"),
    ("E1504FA-NJ1527W", "E1504FA NJ1527W", "NJ1527W"),
    ("G614PM-TS179WSM", "G614PM TS179WSM", "TS179WSM"),
    ("ANV15-42-R2UH", "ANV15 42 R2UH", "ANV15"),  # R2UH is too short to stand alone
    ("A14-11M-X3BP", "A14 11M X3BP", ""),  # no segment is distinctive enough
    ("MacBook Air", "", ""),  # a name, not a punctuated code
    ("FX608JHR", "", ""),
])
def test_spellings_come_only_from_the_verified_code(code, spaced, segment) -> None:
    assert spaced_code(code) == spaced
    assert distinctive_segment(code) == segment
    if segment:
        assert segment in code  # never invented: a literal part of the recorded code


def test_variant_queries_follow_the_code_and_are_marked() -> None:
    queries = search_queries("T", "pcx", model="B12UC-1432PH", part_number="", brand="MSI",
                             series="Thin 15", core=CORE)
    assert [(q.tier, q.text, q.verified_identifier) for q in queries] == [
        (TIER_MODEL_CODE, "B12UC-1432PH", True),
        (TIER_MODEL_CODE_SPACED, "B12UC 1432PH", True),
        (TIER_CODE_SEGMENT, "1432PH", False),
        (TIER_MODEL_SPECS, "B12UC-1432PH 8GB 512GB", False),
        (TIER_FAMILY, "MSI Thin 15", False),
        ("5_FAMILY_WITH_SPECS", "MSI Thin 15 Core i3 8GB 512GB", False),
    ]
    assert "complete code must appear" in queries[2].basis


def test_a_code_without_series_falls_back_to_brand_and_processor_model() -> None:
    queries = search_queries("T", "laptop_factory", model="14-ep0445TU", part_number="D07C0PA",
                             brand="HP", series="", core=CORE, cpu_model="i3-1315U")
    assert [q.tier for q in queries] == [TIER_MODEL_CODE, TIER_MODEL_CODE_SPACED,
                                         TIER_PART_NUMBER, TIER_CODE_SEGMENT, TIER_MODEL_SPECS,
                                         TIER_BRAND_CPU_SPECS]
    assert queries[-1].text == "HP i3-1315U 8GB 512GB"
    # A bare series ("Ryzen 3") repeats the CPU family and is not searched on its own.
    assert distinctive_cpu_model("Ryzen 3", "Ryzen 3") == ""
    assert distinctive_cpu_model("Intel i7", "Core i7") == ""
    assert distinctive_cpu_model("Ryzen 7 7735HS", "Ryzen 7") == "Ryzen 7 7735HS"
    with_series = search_queries("T", "pcx", model="14-ep0445TU", part_number="", brand="HP",
                                 series="Laptop 14", core=CORE, cpu_model="i3-1315U")
    assert TIER_BRAND_CPU_SPECS not in [q.tier for q in with_series]


def variant_run(tmp_path, routes, *extra):
    return run(tmp_path, routes, "--max-total-requests", "10",
               "--max-identifier-variant-queries", "2", *extra)


def test_a_code_with_no_result_is_retried_with_spaces(tmp_path) -> None:
    routes = standard_routes()
    code_url = LF.first_page_url("A14-11M-X3BP")
    routes[code_url] = html(code_url, lf_search(no_results=True))
    spaced_url = LF.first_page_url("A14 11M X3BP")
    routes[spaced_url] = html(spaced_url, lf_search([(LF_PRODUCT,
                                                      "Acer Aspire AI 14 A14-11M-X3BP")]))
    code, transport, out = variant_run(tmp_path, routes)
    assert code == 0
    row = job(out)
    assert row["stop_reason"] == "PLAUSIBLE_CANDIDATE_FOUND"
    assert row["tiers_run"] == f"{TIER_MODEL_CODE};{TIER_MODEL_CODE_SPACED}"
    assert row["identifier_variant_queries"] == f"{TIER_MODEL_CODE_SPACED}:A14 11M X3BP:RESULTS"
    spaced = next(r for r in rows(out / "search_requests.csv")
                  if r["tier"] == TIER_MODEL_CODE_SPACED)
    assert "returned no result" in spaced["run_reason"]
    candidate = next(r for r in rows(out / "candidates.csv") if r["product_url"] == LF_PRODUCT)
    assert candidate["category"] == "CODE_MATCH" and candidate["plausible"] == "TRUE"


def test_a_code_that_finds_results_costs_no_extra_request(tmp_path) -> None:
    code, transport, out = variant_run(tmp_path, standard_routes())
    assert code == 0
    assert not any("A14+11M+X3BP" in url for url in transport.calls)
    assert job(out)["identifier_variant_queries"] == ""


def test_a_spelling_result_with_another_code_is_not_plausible(tmp_path) -> None:
    routes = standard_routes()
    code_url = LF.first_page_url("A14-11M-X3BP")
    routes[code_url] = html(code_url, lf_search(no_results=True))
    other = LF_PRODUCT.replace("x3bp", "x3bq")
    spaced_url = LF.first_page_url("A14 11M X3BP")
    routes[spaced_url] = html(spaced_url, lf_search([(other, "Acer Aspire AI 14 A14-11M-X3BQ")]))
    _, _, out = variant_run(tmp_path, routes)
    row = job(out)
    assert TIER_MODEL_SPECS in row["tiers_run"]  # the search went on
    candidate = next(r for r in rows(out / "candidates.csv") if r["product_url"] == other)
    assert candidate["plausible"] == "FALSE" and candidate["category"] != "CODE_MATCH"


def test_variants_can_be_switched_off(tmp_path) -> None:
    routes = standard_routes()
    code_url = LF.first_page_url("A14-11M-X3BP")
    routes[code_url] = html(code_url, lf_search(no_results=True))
    _, transport, out = run(tmp_path, routes, "--max-total-requests", "10")  # default here: 0
    assert not any("A14+11M+X3BP" in url for url in transport.calls)
    assert TIER_MODEL_CODE_SPACED not in job(out)["tiers_run"]
