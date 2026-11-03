"""Search routes, code spellings, capped answers, pagination and plan-only routes (2026-11-03).

Fakes only: every response comes from a routed fake transport and the no-network guard is active.
"""

from __future__ import annotations

import csv
import json

import pytest
from scraping_fakes import FakeClock, no_network  # noqa: F401
from test_product_discovery import (
    JSON,
    LF,
    LF_HOST,
    NITRO,
    PCC,
    Routes,
    discover,
    fetched,
    html,
    inputs,
    lf_search,
    rows,
    shopify,
    standard_routes,
)

from laptop_tracker.alternate_routes import WEB_SEARCH_ROUTE, web_search_plan_rows
from laptop_tracker.discovery_outcomes import SEARCH_INCOMPLETE_PAGES_UNREAD
from laptop_tracker.product_discovery import rank_hit
from laptop_tracker.retailer_search import (
    NATIVE_ROUTE,
    STOREFRONT_ROUTE,
    TIER_CODE_SEGMENT,
    TIER_MODEL_CODE,
    TIER_MODEL_CODE_COMPACT,
    TIER_MODEL_CODE_HYPHENATED,
    TIER_MODEL_CODE_SPACED,
    TIER_NAME_CPU,
    TIER_STOREFRONT,
    UNAVAILABLE_SHOW,
    SearchLayoutError,
    SearchRecord,
    code_spellings,
    compact_code,
    hyphenated_code,
    search_cache_key,
    search_queries,
)
from laptop_tracker.scraping import TransportResponse

pytestmark = pytest.mark.usefixtures("no_network")
PCC_HOST = PCC.host


# Safe spacing and punctuation spellings of one code


@pytest.mark.parametrize(
    ("code", "spellings"),
    [
        (
            "15-fc0933AU",
            [(TIER_MODEL_CODE_SPACED, "15 fc0933AU"), (TIER_MODEL_CODE_COMPACT, "15fc0933AU")],
        ),
        (
            "15 fc0933au",
            [(TIER_MODEL_CODE_COMPACT, "15fc0933au"), (TIER_MODEL_CODE_HYPHENATED, "15-fc0933au")],
        ),
        ("15fc0933AU", []),
        (
            "X1504VA-BQ4301WSM",
            [
                (TIER_MODEL_CODE_SPACED, "X1504VA BQ4301WSM"),
                (TIER_MODEL_CODE_COMPACT, "X1504VABQ4301WSM"),
            ],
        ),
        (
            "AG14-71M-58JD",
            [(TIER_MODEL_CODE_SPACED, "AG14 71M 58JD"), (TIER_MODEL_CODE_COMPACT, "AG1471M58JD")],
        ),
    ],
)
def test_one_code_has_only_its_own_safe_spellings(code, spellings) -> None:
    assert code_spellings(code) == spellings
    for _, text in spellings:
        # Every spelling is the same characters: nothing is invented, only separators change.
        assert "".join(ch for ch in text if ch.isalnum()).casefold() == (
            "".join(ch for ch in code if ch.isalnum()).casefold()
        )


def test_compact_and_hyphenated_forms_only_change_separators() -> None:
    assert compact_code("14-ep0445TU") == "14ep0445TU"
    assert compact_code("FX608JHR") == ""
    assert hyphenated_code("14 ep0445TU") == "14-ep0445TU"
    assert hyphenated_code("14-ep0445TU") == ""  # already punctuated: never re-punctuated


def test_spellings_are_variant_queries_after_the_complete_code_and_dedupe_by_case() -> None:
    queries = search_queries(
        "T",
        "pcc",
        model="15-fc0933AU",
        part_number="",
        brand="HP",
        series="Laptop 15",
        core={"cpu_series": "Ryzen 5"},
    )
    tiers = [q.tier for q in queries]
    assert tiers[:4] == [
        TIER_MODEL_CODE,
        TIER_MODEL_CODE_SPACED,
        TIER_MODEL_CODE_COMPACT,
        TIER_CODE_SEGMENT,
    ]
    # A dash style or letter case that differs is one search, not two.
    again = search_queries(
        "T",
        "pcc",
        model="15–FC0933AU",
        part_number="",
        brand="HP",
        series="Laptop 15",
        core={"cpu_series": "Ryzen 5"},
    )
    assert [q.text.casefold().replace("–", "-") for q in again[:3]] == [
        q.text.casefold() for q in queries[:3]
    ]


def test_a_name_target_gets_a_narrow_name_and_processor_query_without_capacities() -> None:
    core = {"cpu_series": "Apple A18 Pro", "ram_capacity_gb": "8", "storage_capacity_gb": "512"}
    queries = search_queries(
        "TGT-000002",
        "pcx",
        model="MacBook Neo",
        part_number="",
        brand="Apple",
        series="",
        core=core,
        kind="PRODUCT_NAME",
    )
    assert [(q.tier, q.text) for q in queries] == [
        ("1_PRODUCT_NAME", "Apple MacBook Neo"),
        (TIER_NAME_CPU, "Apple MacBook Neo A18 Pro"),
        ("3_PRODUCT_NAME_WITH_SPECS", "Apple MacBook Neo A18 Pro 8GB 512GB"),
    ]
    assert "no capacities" in queries[1].basis


# Routes: predictive search keeps unavailable products; storefront search is opt-in


def test_predictive_search_names_how_unavailable_products_are_treated() -> None:
    last = PCC.first_page_url("Acer Aspire")
    assert "resources%5Boptions%5D%5Bunavailable_products%5D=last" in last
    assert "unavailable_products%5D=show" in PCC.first_page_url(
        "Acer Aspire", NATIVE_ROUTE, UNAVAILABLE_SHOW
    )
    assert "unavailable_products" not in LF.first_page_url("Acer Aspire")
    assert PCC.first_page_url("MacBook Neo", STOREFRONT_ROUTE) == (
        f"https://{PCC_HOST}/search?q=MacBook+Neo&type=product"
    )
    # An earlier saved response (no unavailable option) is the same search under the default.
    old = (
        f"https://{PCC_HOST}/search/suggest.json?q=Acer+Aspire&resources%5Btype%5D=product"
        "&resources%5Blimit%5D=10"
    )
    assert search_cache_key(last, legacy=True) == search_cache_key(old)
    assert search_cache_key(last) != search_cache_key(old)


def test_the_storefront_route_is_off_unless_asked_and_never_used_on_woocommerce() -> None:
    common = {"model": "A14-11M-X3BP", "part_number": "", "brand": "Acer", "series": "Aspire"}
    plain = search_queries("T", "pcc", **common)
    assert TIER_STOREFRONT not in [q.tier for q in plain]
    opted = search_queries("T", "pcc", storefront=True, **common)
    assert opted[-1].tier == TIER_STOREFRONT and opted[-1].route == STOREFRONT_ROUTE
    assert opted[-1].text == "A14-11M-X3BP"
    lf = search_queries("T", "laptop_factory", storefront=True, **common)
    assert TIER_STOREFRONT not in [q.tier for q in lf]


STORE_PAGE = (
    "<html><body><ul>"
    '<li><a href="/products/apple-macbook-neo-a18-pro-mhfa4pp-a-laptop-silver?_pos=1"><img></a>'
    '<a href="/products/apple-macbook-neo-a18-pro-mhfa4pp-a-laptop-silver?_pos=1">APPLE MacBook '
    "Neo A18 Pro 256GB (MHFA4PP/A) Laptop (Silver)</a></li>"
    '<li><a href="/products/other-laptop">Other Laptop</a></li>'
    '<li><a href="/collections/laptops/products/x">Collection-scoped</a></li></ul>'
    '<a rel="next" href="/search?page=2&amp;q=MacBook+Neo&amp;type=product">Next</a>'
    "</body></html>"
)


def test_storefront_results_page_is_parsed_with_pagination_and_filtered_links() -> None:
    url = PCC.first_page_url("MacBook Neo", STOREFRONT_ROUTE)
    parsed = PCC.parse(fetched(url, STORE_PAGE), STOREFRONT_ROUTE)
    assert [(h.product_url, h.title) for h in parsed.hits] == [
        (
            f"https://{PCC_HOST}/products/apple-macbook-neo-a18-pro-mhfa4pp-a-laptop-silver",
            "APPLE MacBook Neo A18 Pro 256GB (MHFA4PP/A) Laptop (Silver)",
        ),
        (f"https://{PCC_HOST}/products/other-laptop", "Other Laptop"),
    ]
    assert parsed.next_url == f"https://{PCC_HOST}/search?page=2&q=MacBook+Neo&type=product"
    assert parsed.filtered_links == 1
    empty = PCC.parse(fetched(url, "<p>No results found for “zzz”.</p>"), STOREFRONT_ROUTE)
    assert empty.hits == ()
    with pytest.raises(SearchLayoutError):  # a page rendered in the browser has no result links
        PCC.parse(fetched(url, '<div id="search-app"></div>'), STOREFRONT_ROUTE)


# Predictive search is capped at ten results: never an exhaustive search


def test_ten_predictive_results_are_marked_capped_and_fewer_are_complete_for_the_query() -> None:
    url = PCC.first_page_url("Acer")
    for count, capped, completeness in (
        (10, True, "INCOMPLETE_RESULT_CAP"),
        (3, False, "COMPLETE_FOR_QUERY"),
    ):
        parsed = PCC.parse(
            fetched(
                url,
                shopify(*[(f"acer-{n}", f"Acer {n} Laptop") for n in range(count)]),
                JSON["content-type"],
            )
        )
        assert parsed.possibly_truncated is capped
        search = SearchRecord(None, 1, url, "RESULTS", possibly_truncated=parsed.possibly_truncated)
        assert search.completeness == completeness
    assert SearchRecord(None, 1, url, "SEARCH_ERROR").completeness == "NOT_READ"


def test_an_unavailable_result_is_a_candidate_and_stock_is_classified_separately() -> None:
    body = json.loads(shopify(("acer-nitro-v-15-anv15-42-r2uh", "Acer Nitro V 15 ANV15-42-R2UH")))
    body["resources"]["results"]["products"][0]["available"] = False
    parsed = PCC.parse(fetched(PCC.first_page_url("x"), json.dumps(body), JSON["content-type"]))
    [only] = parsed.hits
    assert only.available == "FALSE"
    candidate = rank_hit(NITRO, only, [])
    # Sold out is not "not relevant": the candidate keeps its category and is collected so the
    # product page (and the adapter) classify stock; the search only flags it.
    assert candidate.category == "CODE_MATCH"
    assert "SEARCH_SAYS_UNAVAILABLE" in candidate.flags


# Saved responses and the option the predictive search sends now


def saved_folder(tmp_path, url: str, body: str):
    folder = tmp_path / "saved"
    (folder / "evidence").mkdir(parents=True)
    (folder / "evidence" / "a.json").write_text(body, encoding="utf-8")
    with (folder / "search_requests.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, ["url", "outcome", "retrieved_at", "evidence_file", "http_status", "final_url"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "url": url,
                "outcome": "RESULTS",
                "retrieved_at": "2026-11-03T05:00:00Z",
                "evidence_file": "evidence/a.json",
                "http_status": "200",
                "final_url": url,
            }
        )
    return folder


def replay(tmp_path, folder, *extra, name="out"):
    transport = Routes({})
    out = tmp_path / name
    code = discover.main(
        [
            "--live",
            "--replay-only",
            "--targets",
            "TGT-000001",
            "--retailers",
            "pcc",
            *inputs(tmp_path),
            "--output-dir",
            str(out),
            "--max-total-requests",
            "5",
            "--max-identifier-variant-queries",
            "0",
            "--search-cache",
            str(folder),
            *extra,
        ],
        transport=transport,
        clock=FakeClock(),
    )
    return code, transport, rows(out / "search_requests.csv")


def test_a_response_saved_before_the_unavailable_option_still_answers_the_default(tmp_path) -> None:
    old = (
        f"https://{PCC_HOST}/search/suggest.json?q=A14-11M-X3BP&resources%5Btype%5D=product"
        "&resources%5Blimit%5D=10"
    )
    folder = saved_folder(tmp_path, old, shopify(("acer-a14-11m-x3bp", "Acer A14-11M-X3BP")))
    code, transport, requests = replay(tmp_path, folder)
    assert code == 0 and transport.calls == []  # a replay sends nothing
    first = requests[0]
    assert first["outcome"] == "RESULTS" and first["cache"].startswith("SAVED:")
    assert "predates the unavailable_products option" in first["message"]
    # Under another mode the old response is not the same search: nothing is replayed.
    _, _, shown = replay(tmp_path, folder, "--unavailable-products", "show", name="shown")
    assert shown[0]["outcome"] == "NOT_SENT_REPLAY_ONLY"


# Pagination within a bounded request budget


def test_pagination_is_followed_only_up_to_the_page_limit_and_the_rest_is_reported(
    tmp_path,
) -> None:
    base = LF.first_page_url("A14-11M-X3BP")
    page2 = f"https://{LF_HOST}/page/2/?s=A14-11M-X3BP&post_type=product"
    page3 = f"https://{LF_HOST}/page/3/?s=A14-11M-X3BP&post_type=product"
    other = "/product/acer-aspire-ai-14-a14-11m-x9zz/"
    routes = {
        base: html(base, lf_search([(other, "Acer Aspire AI 14 A14-11M-X9ZZ")], next_href=page2)),
        page2: html(
            page2,
            lf_search(
                [("/product/acer-aspire-ai-14-a14-11m-x8yy/", "Acer Aspire AI 14 A14-11M-X8YY")],
                next_href=page3,
            ),
        ),
    }
    transport = Routes(routes)
    out = tmp_path / "out"
    code = discover.main(
        [
            "--live",
            "--targets",
            "TGT-000001",
            "--retailers",
            "laptop_factory",
            *inputs(tmp_path),
            "--output-dir",
            str(out),
            "--max-total-requests",
            "10",
            "--max-search-pages",
            "2",
            "--max-queries-per-target-retailer",
            "1",
            "--max-identifier-variant-queries",
            "0",
        ],
        transport=transport,
        clock=FakeClock(),
    )
    assert code == 0
    assert transport.calls == [base, page2]  # page 3 exists and is never requested
    requests = rows(out / "search_requests.csv")
    assert [(r["page_number"], r["completeness"]) for r in requests[:2]] == [
        ("1", "COMPLETE_FOR_QUERY"),
        ("2", "INCOMPLETE_PAGES_NOT_READ"),
    ]
    assert "Next result page not read" in requests[1]["message"]
    job = rows(out / "search_jobs.csv")[0]
    assert "MORE_PAGES_NOT_READ" in job["search_coverage"]
    assert job["terminal_outcome"] == SEARCH_INCOMPLETE_PAGES_UNREAD


def test_the_storefront_route_stays_within_its_own_request_cap(tmp_path) -> None:
    routes = standard_routes()
    for query in ("A14-11M-X3BP", "EN15-0002"):
        url = PCC.first_page_url(query)
        routes[url] = TransportResponse(200, url, shopify().encode(), JSON)
    page_one = PCC.first_page_url("A14-11M-X3BP", STOREFRONT_ROUTE)
    routes[page_one] = html(page_one, STORE_PAGE)  # lists a next page; the cap leaves it unread
    transport = Routes(routes)
    out = tmp_path / "out"
    code = discover.main(
        [
            "--live",
            "--targets",
            "TGT-000001,TGT-000002",
            "--retailers",
            "pcc",
            *inputs(tmp_path),
            "--output-dir",
            str(out),
            "--max-total-requests",
            "30",
            "--max-search-pages",
            "2",
            "--max-identifier-variant-queries",
            "0",
            "--max-storefront-search-requests",
            "1",
        ],
        transport=transport,
        clock=FakeClock(),
    )
    assert code == 0
    storefront = [u for u in transport.calls if "/search?" in u]
    assert storefront == [page_one]  # one request: not page 2, and not the second target's
    requests = [
        r for r in rows(out / "search_requests.csv") if r["route"] == "STOREFRONT_SEARCH_PAGE"
    ]
    assert [(r["target_id"], r["outcome"]) for r in requests] == [
        ("TGT-000001", "RESULTS"),
        ("TGT-000001", "NOT_SENT_SEARCH_LIMIT"),
        ("TGT-000002", "NOT_SENT_SEARCH_LIMIT"),
    ]
    assert all("--max-storefront-search-requests" in r["message"] for r in requests[1:])
    summary = json.loads((out / "discovery_summary.json").read_text())
    assert summary["limits"]["max_storefront_search_requests"] == 1


# Retailer-restricted web search is a plan, never a request


def test_web_search_queries_are_planned_for_a_person_and_never_sent(tmp_path) -> None:
    code = {
        "target_id": "TGT-000293",
        "brand": "ASUS",
        "model": "X1504VA-BQ4301WSM",
        "kind": "MODEL_CODE",
        "recorded_code": "",
        "cpu_series": "Core 3",
    }
    name = {
        "target_id": "TGT-000001",
        "brand": "Apple",
        "model": "MacBook Air",
        "kind": "PRODUCT_NAME",
        "recorded_code": "",
        "cpu_series": "Apple M5",
    }
    plan = web_search_plan_rows([code, name], ["pcc", "datablitz"])
    queries = {(r["target_id"], r["retailer_id"], r["order"]): r["query"] for r in plan}
    assert queries[("TGT-000293", "pcc", "1")] == (f'site:{PCC_HOST} "X1504VA-BQ4301WSM"')
    assert queries[("TGT-000293", "pcc", "3")] == f'site:{PCC_HOST} "X1504VABQ4301WSM"'
    assert queries[("TGT-000001", "datablitz", "2")] == (
        'site:ecommerce.datablitz.com.ph "Apple MacBook Air M5"'
    )
    assert {r["route"] for r in plan} == {WEB_SEARCH_ROUTE}
    assert {r["status"] for r in plan} == {"PLAN_ONLY_NOT_SENT"}
    transport = Routes({})
    out = tmp_path / "out"
    discover.main(
        [
            "--preview",
            "--targets",
            "TGT-000001",
            "--retailers",
            "pcc",
            *inputs(tmp_path),
            "--output-dir",
            str(out),
            "--web-search-plan",
        ],
        transport=transport,
        clock=FakeClock(),
    )
    assert transport.calls == []
    written = rows(out / "web_search_plan.csv")
    assert written and all(r["status"] == "PLAN_ONLY_NOT_SENT" for r in written)
    assert all(r["restricted_host"] == PCC_HOST for r in written)
