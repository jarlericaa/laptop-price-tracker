"""Automatic product discovery: search parsing, ranking, variants, budgets, and the pipeline.

Synthetic fixtures only; a routed fake transport serves every response and the no-network guard
is active. Shapes follow the search responses inspected on 2026-10-13.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, no_network  # noqa: F401
from test_collection import write_dicts
from test_extraction import FIXTURE_PAGES, FIXTURES, HTML
from test_extraction_layouts import OCT_ROWS, offer, primary
from test_match_run import TARGET_ROWS
from test_price_tracking import ledger, list_rows
from test_target_master import target_values
from test_verified_top import load_script

from laptop_tracker.adapters.laptop_factory import HOST as LF_HOST
from laptop_tracker.adapters.pcc import HOST as PCC_HOST
from laptop_tracker.collection import read_manifest, validate_manifest
from laptop_tracker.product_discovery import (
    CODE_MATCH,
    CODE_SPELLING,
    CODE_URL_ONLY,
    CODE_WITH_SUFFIX,
    DIFFERENT_MODEL,
    FAMILY_ONLY,
    PART_NUMBER_MATCH,
    UNRELATED,
    SearchTarget,
    VariantInfo,
    inspect_product_page,
    rank_hit,
    rank_target_hits,
    title_clues,
    variant_decision,
)
from laptop_tracker.retailer_search import (
    SEARCH_METHODS,
    TIER_CODE_SEGMENT,
    TIER_FAMILY,
    TIER_FAMILY_SPECS,
    TIER_MODEL_CODE,
    TIER_MODEL_CODE_SPACED,
    TIER_MODEL_SPECS,
    TIER_PART_NUMBER,
    BudgetedTransport,
    SearchHit,
    SearchLayoutError,
    SearchQuery,
    SearchRecord,
    search_queries,
    unique_hits,
)
from laptop_tracker.scraping import FetchedPage, TransportError, TransportResponse
from laptop_tracker.verified_top import CONFIGURATION_COLUMNS, LEDGER_COLUMNS

pytestmark = pytest.mark.usefixtures("no_network")
discover = load_script("discover_products")

LF = SEARCH_METHODS["laptop_factory"]
PCC = SEARCH_METHODS["pcc"]
NITRO = SearchTarget(
    "TGT-000059", "16", "Acer", "Nitro", "ANV15-42-R2UH", "",
    {"model": "ANV15-42-R2UH", "screen_size_in": "15.6", "cpu_series": "Ryzen 5",
     "ram_capacity_gb": "16", "storage_capacity_gb": "512"},
)
JSON = {"content-type": "application/json; charset=utf-8"}


def fetched(url: str, body: str | bytes, content_type: str = "text/html; charset=utf-8",
            final_url: str | None = None) -> FetchedPage:
    data = body.encode() if isinstance(body, str) else body
    return FetchedPage("x", url, final_url or url, 200, data, content_type,
                       datetime(2026, 10, 13, tzinfo=UTC), 1)


def lf_search(cards=(), *, next_href="", no_results=False, grid=True) -> str:
    items = "".join(
        f'<div class="product-small col product"><div class="box-image"><a href="{href}">'
        f'<img src="x.jpg"></a></div><div class="box-text"><p class="category">Acer</p>'
        f'<p class="name product-title"><a href="{href}">{title}</a></p></div></div>'
        for href, title in cards
    )
    nav = (f'<nav class="woocommerce-pagination"><ul class="page-numbers nav-pagination">'
           f'<li><a class="next page-number" href="{next_href}">&gt;</a></li></ul></nav>'
           if next_href else "")
    body = (
        '<div class="woocommerce-no-products-found"><div class="woocommerce-info">'
        "No products were found matching your selection.</div></div>"
        if no_results else
        (f'<div class="products row">{items}</div>{nav}' if grid else "<div>maintenance</div>")
    )
    return (f'<html><body class="search search-results"><div class="shop-container">{body}'
            '</div><h4>Pages found</h4><a href="/product/not-a-card/">Not a card</a>'
            "</body></html>")


def shopify(*products: tuple[str, str], **extra) -> str:
    return json.dumps({"resources": {"results": {"products": [
        {"title": title, "url": f"/products/{handle}?_pos=1&_psq=q&_ss=e",
         "handle": handle, "vendor": extra.get("vendor", "Acer"), "type": "Laptop",
         "tags": ["13\" To 14.9\""], "available": True, "price": "1000.00",
         "body": "<p>Specs</p>", "variants": []}
        for handle, title in products
    ]}}})


def hit(title: str, url: str = f"https://{LF_HOST}/product/x/", **kwargs) -> SearchHit:
    return SearchHit("laptop_factory", url, title, url, 1, 1, **kwargs)


def record(target: SearchTarget, *hits: SearchHit, tier: str = TIER_MODEL_CODE) -> SearchRecord:
    query = SearchQuery(target.target_id, hits[0].retailer_id if hits else "laptop_factory", tier,
                        target.model, True, "")
    return SearchRecord(query, 1, "https://example.test/search", "RESULTS",
                        hit_items=list(hits))


# Search result parsing


def test_woocommerce_results_resolve_relative_links_and_pagination() -> None:
    body = lf_search(
        [("/product/acer-nitro-v-15-anv15-42-r2uh/", "Acer Nitro V 15 ANV15-42-R2UH"),
         (f"https://{LF_HOST}/product/acer-nitro-v-15-anv15-42-r5ep/", "Acer Nitro V 15 R5EP"),
         ("https://elsewhere.test/product/x/", "Off-host link")],
        next_href=f"https://{LF_HOST}/page/2/?s=ANV15&#038;post_type=product",
    )
    url = LF.first_page_url("ANV15-42-R2UH")
    assert url == f"https://{LF_HOST}/?s=ANV15-42-R2UH&post_type=product"
    parsed = LF.parse(fetched(url, body))
    assert [h.product_url for h in parsed.hits] == [
        f"https://{LF_HOST}/product/acer-nitro-v-15-anv15-42-r2uh/",
        f"https://{LF_HOST}/product/acer-nitro-v-15-anv15-42-r5ep/",
    ]
    assert parsed.filtered_links == 1  # The off-host card; "Pages found" links are not cards.
    assert parsed.next_url == f"https://{LF_HOST}/page/2/?s=ANV15&post_type=product"


def test_woocommerce_empty_results_differ_from_an_unknown_layout() -> None:
    url = LF.first_page_url("NOPE")
    assert LF.parse(fetched(url, lf_search(no_results=True))).hits == ()
    with pytest.raises(SearchLayoutError):
        LF.parse(fetched(url, lf_search(grid=False)))
    with pytest.raises(SearchLayoutError):
        LF.parse(fetched(url, lf_search([], grid=True)))  # Grid without readable cards.


def test_woocommerce_single_result_redirect_is_one_hit() -> None:
    product = f"https://{LF_HOST}/product/acer-nitro-v-15-anv15-42-r2uh/"
    page = fetched(LF.first_page_url("ANV15-42-R2UH"),
                   '<h1 class="product-title product_title">Acer Nitro V 15 ANV15-42-R2UH</h1>',
                   final_url=product)
    [only] = LF.parse(page).hits
    assert (only.product_url, only.title) == (product, "Acer Nitro V 15 ANV15-42-R2UH")


def test_shopify_predictive_results_drop_tracking_queries_and_filter_non_products() -> None:
    url = PCC.first_page_url("B13UC-3474PH")
    assert "resources%5Btype%5D=product" in url and "q=B13UC-3474PH" in url
    body = json.loads(shopify(("msi-thin-15-b13uc-3474ph", "MSI Thin 15 B13UC-3474PH")))
    body["resources"]["results"]["products"].append({"title": "Gift card",
                                                     "url": "/collections/gifts"})
    parsed = PCC.parse(fetched(url, json.dumps(body), JSON["content-type"]))
    [only] = parsed.hits
    assert only.product_url == f"https://{PCC_HOST}/products/msi-thin-15-b13uc-3474ph"
    assert "_psq" in only.listed_url and parsed.filtered_links == 1
    assert only.tags == ('13" To 14.9"',)


@pytest.mark.parametrize("body,ctype", [
    ("<html>client-rendered search</html>", "text/html"),
    ("{not json", "application/json"),
    ('{"resources": {}}', "application/json"),
])
def test_shopify_unreadable_responses_are_layout_errors(body: str, ctype: str) -> None:
    with pytest.raises(SearchLayoutError):
        PCC.parse(fetched(PCC.first_page_url("x"), body, ctype))


def test_duplicate_results_across_queries_are_merged() -> None:
    a = hit("Acer Nitro V 15 ANV15-42-R2UH")
    first, second = record(NITRO, a), record(NITRO, a, tier=TIER_FAMILY)
    [(only, records)] = unique_hits([first, second])
    assert only.product_url == a.product_url and records == [first, second]


# Queries and ranking


def test_queries_run_code_then_verified_part_number_then_family() -> None:
    tiers = [q.tier for q in search_queries("T", "pcx", model="S3607VA-RP181WSM",
                                            part_number="90NB1672-M00DD0", brand="ASUS",
                                            series="Vivobook")]
    # The identifier spellings (2026-10-22) follow the complete code in the plan; a job runs
    # them only when the complete code returned no result.
    assert tiers == [TIER_MODEL_CODE, TIER_MODEL_CODE_SPACED, TIER_PART_NUMBER,
                     TIER_CODE_SEGMENT, TIER_FAMILY]
    no_mpn = search_queries("T", "pcx", model="S3607VA-RP181WSM", part_number="",
                            brand="ASUS", series="Vivobook")
    assert [q.text for q in no_mpn] == ["S3607VA-RP181WSM", "S3607VA RP181WSM", "RP181WSM",
                                        "ASUS Vivobook"]
    # The complete code first, without filters; specifications only in later fallbacks.
    core = {"cpu_series": "Core 5", "ram_capacity_gb": "16", "storage_capacity_gb": "1000"}
    full = search_queries("T", "pcx", model="S3607VA-RP181WSM", part_number="", brand="ASUS",
                          series="Vivobook", core=core)
    assert [(q.tier, q.text) for q in full] == [
        (TIER_MODEL_CODE, "S3607VA-RP181WSM"),
        (TIER_MODEL_CODE_SPACED, "S3607VA RP181WSM"),
        (TIER_CODE_SEGMENT, "RP181WSM"),
        (TIER_MODEL_SPECS, "S3607VA-RP181WSM 16GB 1TB"),
        (TIER_FAMILY, "ASUS Vivobook"),
        (TIER_FAMILY_SPECS, "ASUS Vivobook Core 5 16GB 1TB"),
    ]


@pytest.mark.parametrize(
    ("title", "category", "suffix"),
    [
        ("Acer Nitro V 15 ANV15-42-R2UH", CODE_MATCH, ""),
        ("ACER NITRO V ANV15-42-R2UH OPI/RYZEN 5 7640HS/ 16GB DDR5/ 512GB SSD", CODE_WITH_SUFFIX,
         "OPI"),
        ("Acer Nitro V 15 (anv15-42-r2uh) Gaming Laptop", CODE_MATCH, ""),
        ("Acer Nitro V ANV15 42 R2UH gaming laptop", CODE_SPELLING, ""),
        ("Acer Nitro V 15 ANV15-42-R5EP", DIFFERENT_MODEL, ""),
        ("Acer Nitro V 15 ANV15-42-R2UHX", DIFFERENT_MODEL, ""),
        ("Acer Nitro V 15 Gaming Laptop", FAMILY_ONLY, ""),
        ("Acerpure Cozy Smart Fan", UNRELATED, ""),
    ],
)
def test_title_wording_is_ranked_without_deciding_identity(title, category, suffix) -> None:
    ranked = rank_hit(NITRO, hit(title), [record(NITRO, hit(title))])
    assert (ranked.category, ranked.suffix) == (category, suffix)
    assert ranked.reasons


def test_code_only_in_the_url_and_verified_part_numbers() -> None:
    url_only = hit("Acer gaming laptop", f"https://{LF_HOST}/product/acer-anv15-42-r2uh/")
    assert rank_hit(NITRO, url_only, [record(NITRO, url_only)]).category == CODE_URL_ONLY
    with_mpn = SearchTarget(**{**NITRO.__dict__, "part_number": "NH.QUCSP.001"})
    by_part = hit("Acer Nitro V NH.QUCSP.001")
    assert rank_hit(with_mpn, by_part, [record(with_mpn, by_part)]).category == PART_NUMBER_MATCH
    # Without a verified catalog part number the same title is only a family result.
    assert rank_hit(NITRO, by_part, [record(NITRO, by_part)]).category == FAMILY_ONLY


def test_configuration_clues_order_candidates_but_never_fill_fields() -> None:
    good = hit("Acer Nitro V ANV15-42-R2UH 16GB DDR5 512GB SSD",
               f"https://{LF_HOST}/product/a/")
    wrong = hit("Acer Nitro V ANV15-42-R2UH 8GB RAM 512GB SSD", f"https://{LF_HOST}/product/b/")
    bundle = hit("Acer Nitro V ANV15-42-R2UH + FREE Backpack", f"https://{LF_HOST}/product/c/")
    ranked = rank_target_hits(NITRO, "laptop_factory",
                              [record(NITRO, wrong, bundle, good)])
    assert [c.hit.product_url[-2:] for c in ranked] == ["a/", "c/", "b/"]
    assert ranked[2].clues["ram_capacity_gb"] == "DIFFERS (8)"
    assert "BUNDLE_WORDING_IN_TITLE" in ranked[1].flags
    assert title_clues("16GB (2x8) DDR4 RAM / 512GB SDD")["ram_capacity_gb"] == {"16"}
    assert title_clues("1x 512 PCIe Gen4, 16 Gb/s")["storage_capacity_gb"] == set()


# Variants


def target(ram: str, storage: str) -> SearchTarget:
    return SearchTarget("TGT-000001", "1", "ExampleBrand", "Example", "15-EX0001AU", "",
                        {"model": "15-EX0001AU", "screen_size_in": "15.6",
                         "cpu_series": "Ryzen 7", "ram_capacity_gb": ram,
                         "storage_capacity_gb": storage})


def test_multi_variant_pages_list_variants_from_their_own_structured_data() -> None:
    _, path, url = FIXTURE_PAGES["pcc_variants"]
    bare = url.split("?")[0]
    inspection = inspect_product_page(PCC, bare, (FIXTURES / path).read_text(encoding="utf-8"))
    assert inspection.outcome == "MULTI_VARIANT"
    assert [(v.variant_id, v.title) for v in inspection.variants] == [
        ("50000000000001", "8GB DDR4 RAM / 256GB SSD"),
        ("50000000000002", "16GB (2x8) DDR4 RAM / 256GB SSD"),
    ]
    assert all(v.url == f"{bare}?variant={v.variant_id}" for v in inspection.variants)
    decisions = [variant_decision(v, target("16", "256"))[0] for v in inspection.variants]
    assert decisions == ["ALTERNATIVE", "COLLECT"]  # Never the cheaper default.
    assert [variant_decision(v, target("32", "256"))[0] for v in inspection.variants] == [
        "ALTERNATIVE", "ALTERNATIVE",
    ]


@pytest.mark.parametrize(
    ("retailer", "ram", "decision", "manifest_url_suffix"),
    [
        ("pcc", "16", "COLLECT_VARIANT", "?variant=50000000000002"),
        ("pcc", "32", "REVIEW_NO_MATCHING_VARIANT", None),
        ("datablitz", "16", "REVIEW_MULTI_VARIANT_NOT_SUPPORTED", None),
    ],
)
def test_variant_selection_respects_the_adapter_and_the_target(
    tmp_path: Path, retailer: str, ram: str, decision: str, manifest_url_suffix
) -> None:
    from argparse import Namespace

    from laptop_tracker.scraping import FetchConfig, Fetcher

    _, path, fixture_url = FIXTURE_PAGES["pcc_variants"]
    method = SEARCH_METHODS[retailer]
    handle = fixture_url.split("?")[0].rsplit("/", 1)[-1]
    url = f"https://{method.host}/products/{handle}"
    body = (FIXTURES / path).read_text(encoding="utf-8").replace(PCC_HOST, method.host)
    transport = Routes({url: html(url, body)})
    job = discover.SearchJob(target(ram, "256"), retailer, 1)
    found = SearchHit(retailer, url, "ExampleBrand 15-EX0001AU 8GB 256GB SSD", url, 1, 1)
    ranked = rank_target_hits(job.target, retailer, [record(job.target, found)])
    args = Namespace(max_candidates_per_target_retailer=2, max_product_fetches=5)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    manifest, inspections, variants = discover.select_and_inspect(
        ranked, job, Fetcher(transport, FetchConfig(), FakeClock()),
        BudgetedTransport(transport, 5), args, {"product_fetches": 0}, evidence, lambda _: None,
    )
    assert ranked[0].decision == decision
    assert inspections[0]["outcome"] == "MULTI_VARIANT" and len(variants) == 2
    if manifest_url_suffix:
        [(_, manifest_url, variant_id)] = manifest
        assert manifest_url == url + manifest_url_suffix and variant_id == "50000000000002"
    else:
        assert manifest == []


@pytest.mark.parametrize(
    ("title", "decision"),
    [
        ("16GB DDR5 RAM / 512GB SSD", "COLLECT"),
        ("16GB DDR5 RAM / 512GB + 512GB SSD", "ALTERNATIVE"),  # Two drives, not 512GB.
        ("16GB DDR5 RAM / 512GB + 1TB SSD", "ALTERNATIVE"),
        ("32GB (2x16) MDDR5 RAM / 512GB SSD", "ALTERNATIVE"),  # Retailer typo still RAM.
    ],
)
def test_variant_option_shapes_seen_on_live_pages(title: str, decision: str) -> None:
    option = VariantInfo("1", f"https://{PCC_HOST}/products/x?variant=1", title, "InStock", "1")
    assert variant_decision(option, NITRO)[0] == decision


def test_repeated_product_json_ld_blocks_are_one_offer() -> None:
    # PC Express pages repeat the Product JSON-LD in the head and the body (seen 2026-10-13).
    url = "https://pcx.com.ph/products/example-14"
    block = ('<script type="application/ld+json">{"@type": "Product", "offers": '
             f'[{{"@type": "Offer", "url": "{url}", "price": "1"}}]}}</script>')
    inspection = inspect_product_page(SEARCH_METHODS["pcx"], url, block + block)
    assert inspection.outcome == "SINGLE_VARIANT"


def test_variant_without_an_option_title_needs_review() -> None:
    plain = VariantInfo("1", f"https://{PCC_HOST}/products/x?variant=1", "", "InStock", "1")
    assert variant_decision(plain, target("16", "256"))[0] == "REVIEW"


# Budgets


def test_budget_is_a_hard_ceiling_on_every_http_call() -> None:
    class Inner:
        live = False
        calls = 0

        def fetch(self, url, **kwargs):
            Inner.calls += 1
            return TransportResponse(200, url, b"", {})

    budget = BudgetedTransport(Inner(), 2)
    for _ in range(2):
        budget.fetch("https://a.test/", timeout=1, headers={}, max_bytes=1)
    with pytest.raises(TransportError) as raised:
        budget.fetch("https://a.test/", timeout=1, headers={}, max_bytes=1)
    assert raised.value.code == "REQUEST_BUDGET_EXHAUSTED" and not raised.value.retryable
    assert (Inner.calls, budget.remaining) == (2, 0)


# Listing-specific owner approvals stay with their exact listing


APPROVED = ("https://www.octagon.com.ph/products/acer-nitro-v-anv15-42-r2uh-opi-ryzen-5-7640hs-"
            "16gb-ddr5-512gb-ssd-15-6-fhd-ips-win-11-rtx4050-6gb?variant=49704880308480")
OPI_TITLE = "ACER NITRO V ANV15-42-R2UH OPI/RYZEN 5 6600H/ 16GB DDR5/ 512GB SSD/ 15.6 FHD"


def test_owner_approval_applies_only_to_its_recorded_listing() -> None:
    approved = offer("oct", OCT_ROWS, title=OPI_TITLE, url=APPROVED)
    other = offer("oct", OCT_ROWS, title=OPI_TITLE,
                  url="https://www.octagon.com.ph/products/acer-nitro-v-anv15-42-r2uh-opi-new")
    other_variant = offer("oct", OCT_ROWS, title=OPI_TITLE,
                          url=APPROVED.replace("49704880308480", "49704880308481"))
    assert primary(approved)["model"] == ("EXTRACTED", "ANV15-42-R2UH")
    assert primary(other)["model"] == ("MISSING", "")
    assert primary(other_variant)["model"] == ("MISSING", "")


# The command: preview, live search, collection, budgets, and partial failures


class Routes:
    """Serves fixed responses by exact URL; any other URL fails the test."""

    live = False

    def __init__(self, routes: dict[str, TransportResponse | Exception]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def fetch(self, url, *, timeout, headers, max_bytes) -> TransportResponse:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected request {url}")
        item = self.routes[url]
        if isinstance(item, Exception):
            raise item
        return item


LF_PRODUCT = FIXTURE_PAGES["lf_sale"][2]
PCC_PRODUCT = FIXTURE_PAGES["pcc_single"][2]


def html(url: str, body: str | bytes) -> TransportResponse:
    return TransportResponse(200, url, body.encode() if isinstance(body, str) else body, HTML)


def fixture(name: str, url: str | None = None) -> TransportResponse:
    _, path, fixture_url = FIXTURE_PAGES[name]
    return html(url or fixture_url, (FIXTURES / path).read_bytes())


def inputs(tmp_path: Path) -> list[str]:
    with (tmp_path / "tm.csv").open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(target_values(*TARGET_ROWS))
    ids = ("TGT-000001", "TGT-000002")
    return [
        "--target-master", str(tmp_path / "tm.csv"),
        "--selected-list",
        str(write_dicts(tmp_path / "top.csv", CONFIGURATION_COLUMNS, list_rows(ids, TARGET_ROWS))),
        "--contributions", str(write_dicts(tmp_path / "ledger.csv", LEDGER_COLUMNS, ledger())),
        "--store", str(tmp_path / "store" / "history.sqlite3"),
    ]


def standard_routes() -> dict[str, TransportResponse | Exception]:
    lf, pcc = SEARCH_METHODS["laptop_factory"], SEARCH_METHODS["pcc"]
    lf_a = lf.first_page_url("A14-11M-X3BP")
    lf_b = lf.first_page_url("EN15-0002")
    pcc_a = pcc.first_page_url("A14-11M-X3BP")
    pcc_b = pcc.first_page_url("EN15-0002")
    pcc_bare = PCC_PRODUCT
    # TGT-000001 has a blank brand in the shared fixtures, so its family query is the series.
    empty_family = {
        method.first_page_url("ExampleBook"): (
            html(method.first_page_url("ExampleBook"), lf_search(no_results=True))
            if method is lf else
            TransportResponse(200, method.first_page_url("ExampleBook"), shopify().encode(), JSON)
        )
        for method in (lf, pcc)
    }
    # Fallback tiers (2026-10-19): code with RAM/storage and family with specifications.
    fallbacks = {}
    for query in ("A14-11M-X3BP 16GB 512GB", "EN15-0002 16GB 512GB",
                  "ExampleBook Snapdragon X 16GB 512GB",
                  "ExampleBrand ExampleBook Core 5 16GB 512GB"):
        for method in (lf, pcc):
            url = method.first_page_url(query)
            fallbacks[url] = (
                html(url, lf_search(no_results=True)) if method is lf
                else TransportResponse(200, url, shopify().encode(), JSON)
            )
    return empty_family | fallbacks | {
        lf_a: html(lf_a, lf_search([(LF_PRODUCT, "Acer Aspire AI 14 A14-11M-X3BP"),
                                    ("/product/acer-aspire-ai-14-a14-11m-x9zz/",
                                     "Acer Aspire AI 14 A14-11M-X9ZZ")])),
        lf_b: html(lf_b, lf_search(no_results=True)),
        lf.first_page_url("ExampleBrand ExampleBook"): html(
            lf.first_page_url("ExampleBrand ExampleBook"), lf_search(no_results=True)),
        pcc_a: TransportResponse(200, pcc_a, shopify().encode(), JSON),
        pcc.first_page_url("ExampleBrand ExampleBook"): TransportResponse(
            200, pcc.first_page_url("ExampleBrand ExampleBook"), shopify().encode(), JSON),
        pcc_b: TransportResponse(200, pcc_b, shopify(
            (pcc_bare.rsplit("/", 1)[-1], "Example Nitro 15 EN15-0002 | Core 5 | 16GB | 512GB SSD"),
        ).encode(), JSON),
        pcc_bare: fixture("pcc_single", pcc_bare),
        LF_PRODUCT: fixture("lf_sale"),
    }


def run(tmp_path: Path, mode: str, routes, *extra: str) -> tuple[int, Routes, Path]:
    transport = Routes(routes)
    out = tmp_path / "discovery"
    # Identifier-spelling queries (2026-10-22) are off for these scripted routes unless a test
    # passes the option itself.
    variants = () if mode == "--preview" else ("--max-identifier-variant-queries", "0")
    code = discover.main(
        [mode, "--targets", "TGT-000001,TGT-000002", "--retailers", "laptop_factory,pcc",
         *inputs(tmp_path), "--output-dir", str(out), *variants, *extra],
        transport=transport, clock=FakeClock(),
    )
    return code, transport, out


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_preview_makes_no_request_and_shows_the_plan(tmp_path: Path, capsys) -> None:
    code, transport, out = run(tmp_path, "--preview", {})
    assert code == 0 and transport.calls == []
    plan = rows(out / "search_plan.csv")
    assert [(r["target_id"], r["retailer_id"], r["tier"]) for r in plan][:4] == [
        ("TGT-000001", "laptop_factory", TIER_MODEL_CODE),
        ("TGT-000001", "laptop_factory", TIER_MODEL_SPECS),
        ("TGT-000001", "laptop_factory", TIER_FAMILY),
        ("TGT-000001", "laptop_factory", TIER_FAMILY_SPECS),
    ]
    assert {r["runs"] for r in plan} == {"ALWAYS", "ONLY_IF_NO_PLAUSIBLE_CANDIDATE",
                                         "ONLY_IF_COMPLETE_CODE_RETURNED_NO_RESULT"}
    summary = json.loads((out / "discovery_summary.json").read_text())
    assert summary["run_status"] == "PREVIEW_ONLY" and summary["requests_sent"] == 0
    assert "Preview only" in capsys.readouterr().out


def test_live_requires_an_explicit_total_budget(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run(tmp_path, "--live", {})


def test_search_rank_collect_and_match_end_to_end(tmp_path: Path) -> None:
    code, transport, out = run(tmp_path, "--live", standard_routes(),
                               "--max-total-requests", "20", "--collect",
                               "--baseline-manifest", str(write_dicts(
                                   tmp_path / "old.csv",
                                   ("request_id", "retailer_id", "product_url",
                                    "expected_target_id", "enabled"),
                                   [{"request_id": "old-1", "retailer_id": "laptop_factory",
                                     "product_url": LF_PRODUCT,
                                     "expected_target_id": "TGT-000001", "enabled": "TRUE"}],
                               )))
    assert code == 0
    searches = rows(out / "search_requests.csv")
    by_job = {(r["target_id"], r["retailer_id"], r["tier"]): r["outcome"] for r in searches}
    assert by_job[("TGT-000001", "laptop_factory", TIER_MODEL_CODE)] == "RESULTS"
    # The family query runs only where the code search found nothing.
    assert ("TGT-000001", "laptop_factory", TIER_FAMILY) not in by_job
    assert by_job[("TGT-000002", "laptop_factory", TIER_FAMILY)] == "NO_RESULTS"
    candidates = {r["product_url"]: r for r in rows(out / "candidates.csv")}
    assert candidates[LF_PRODUCT]["decision"] == "COLLECT"
    wrong = f"https://{LF_HOST}/product/acer-aspire-ai-14-a14-11m-x9zz/"
    assert candidates[wrong]["category"] == DIFFERENT_MODEL
    assert candidates[wrong]["decision"] == "NOT_SELECTED"
    manifest = rows(out / "manifest.csv")
    assert {(r["retailer_id"], r["product_url"], r["expected_target_id"]) for r in manifest} == {
        ("laptop_factory", LF_PRODUCT, "TGT-000001"),
        ("pcc", PCC_PRODUCT, "TGT-000002"),
    }
    results = rows(out / "collection" / "match_results.csv")
    # The expected target is only a hint: TGT-000002's fixture states a conflicting refresh rate,
    # so the discovered page goes to review instead of being assigned.
    assert {(r["target_id"], r["match_status"], r["expected_target_check"]) for r in results} == {
        ("TGT-000001", "EXACT", "AS_EXPECTED"), ("", "REVIEW_REQUIRED", "NOT_EXACT"),
    }
    summary = json.loads((out / "discovery_summary.json").read_text())
    assert summary["run_status"] == "SUCCESS" and summary["review_rows"] == 1
    assert summary["requests_sent"] == len(transport.calls) <= 20
    assert summary["store_backup"]["sha256"] == "NO_STORE_YET"
    coverage = rows(out / "coverage_vs_baseline.csv")
    assert coverage[0]["found_by_search"] == "TRUE" and coverage[0]["in_new_manifest"] == "TRUE"
    assert coverage[1]["note"].startswith("Newly discovered")
    saved = {r["evidence_file"] for r in searches if r["evidence_file"]}
    saved |= {r["evidence_file"] for r in rows(out / "product_inspections.csv")}
    assert saved == {f"evidence/{p.name}" for p in (out / "evidence").iterdir()}


def test_generated_manifest_is_accepted_by_the_existing_collector(tmp_path: Path) -> None:
    code, _, out = run(tmp_path, "--live", standard_routes(), "--max-total-requests", "20")
    assert code == 0
    entries = validate_manifest(read_manifest(out / "manifest.csv"),
                                {"TGT-000001": 1, "TGT-000002": 2})
    assert {e.retailer_id for e in entries} == {"laptop_factory", "pcc"}
    collector = load_script("collect_and_match")
    assert collector.main(["--preview", "--manifest", str(out / "manifest.csv"),
                           *inputs(tmp_path), "--output-dir", str(tmp_path / "preview")]) == 0


def test_request_budget_stops_searching_without_calling_it_not_found(tmp_path: Path) -> None:
    code, transport, out = run(tmp_path, "--live", standard_routes(), "--max-total-requests", "2")
    assert len(transport.calls) == 2
    outcomes = [r["outcome"] for r in rows(out / "search_requests.csv")]
    assert outcomes[:2] == ["RESULTS", "NO_RESULTS"]
    assert set(outcomes[2:]) == {"NOT_SENT_BUDGET_EXHAUSTED"}
    assert "NO_RESULTS" not in outcomes[2:]
    assert code == 0


def test_one_failing_retailer_does_not_discard_the_others(tmp_path: Path) -> None:
    routes = standard_routes()
    for query in ("A14-11M-X3BP", "EN15-0002", "ExampleBrand ExampleBook"):
        url = SEARCH_METHODS["pcc"].first_page_url(query)
        routes[url] = TransportResponse(403, url, b"", {})
    code, _, out = run(tmp_path, "--live", routes, "--max-total-requests", "20", "--collect")
    searches = rows(out / "search_requests.csv")
    pcc = [r["outcome"] for r in searches if r["retailer_id"] == "pcc"]
    # Two consecutive errors stop that retailer; the rest are recorded as not sent.
    assert pcc.count("SEARCH_ERROR") == 2
    assert set(pcc) == {"SEARCH_ERROR", "NOT_SENT_RETAILER_FAILING"}
    assert [r["retailer_id"] for r in rows(out / "manifest.csv")] == ["laptop_factory"]
    assert {r["match_status"] for r in rows(out / "collection" / "match_results.csv")} == {"EXACT"}
    assert code == 0


def test_unsupported_layout_is_recorded_separately(tmp_path: Path) -> None:
    routes = standard_routes()
    url = SEARCH_METHODS["laptop_factory"].first_page_url("A14-11M-X3BP")
    routes[url] = html(url, lf_search(grid=False))
    _, _, out = run(tmp_path, "--live", routes, "--max-total-requests", "20")
    first = rows(out / "search_requests.csv")[0]
    assert (first["tier"], first["outcome"]) == (TIER_MODEL_CODE, "UNSUPPORTED_LAYOUT")


def test_listing_only_or_unknown_targets_are_refused_before_any_request(tmp_path: Path,
                                                                     capsys) -> None:
    transport = Routes({})
    code = discover.main(
        ["--live", "--targets", "TGT-000003", *inputs(tmp_path), "--max-total-requests", "5",
         "--output-dir", str(tmp_path / "out")],
        transport=transport, clock=FakeClock(),
    )
    assert code == 1 and transport.calls == []
    assert "not in the verified selected list" in capsys.readouterr().err
