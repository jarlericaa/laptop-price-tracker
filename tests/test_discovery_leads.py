"""Known URLs as leads, variant-preserving de-duplication, reuse accounting and the 2026-11-03
recovery leads.

A known URL (a supplied page, a historical EXACT result, a sitemap entry) is a lead to
revalidate: it is fetched, extracted and matched like any candidate and is never an approved
match. A ``?variant=`` URL is never folded into the bare product or into another variant.
"""

from __future__ import annotations

import csv
import dataclasses
import json
from argparse import Namespace
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, no_network  # noqa: F401
from test_product_discovery import (
    JSON,
    LF,
    LF_PRODUCT,
    NITRO,
    PCC,
    PCC_PRODUCT,
    Routes,
    discover,
    fetched,
    html,
    inputs,
    rows,
    run,
    shopify,
    standard_routes,
)

from laptop_tracker.collection import ROUTES
from laptop_tracker.product_discovery import (
    HISTORICAL_VERIFIED_URL,
    SUPPLIED_URL,
    SearchTarget,
    add_lead_url,
    candidate_row,
    inspect_product_page,
    lead_url_key,
    lead_variant,
    normalize_supplied_url,
    seed_candidate,
    strip_variant,
    supplied_candidate,
    variant_evaluation,
)
from laptop_tracker.retailer_search import (
    BudgetedTransport,
    SearchQuery,
    SearchRecord,
    unique_hits,
)
from laptop_tracker.scraping import FetchConfig, Fetcher, TransportResponse

pytestmark = pytest.mark.usefixtures("no_network")
ROOT = Path(__file__).resolve().parents[1]
PCC_HOST = PCC.host
BASE = f"https://{PCC_HOST}/products/example-nitro-15-en15-0002"


# Lead identity keeps the variant


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        (f"{BASE}?variant=111", f"{BASE}?variant=222", False),
        (f"{BASE}?variant=111", BASE, False),
        (f"{BASE}?variant=111", f"{BASE}?variant=111&utm_source=news&srsltid=x", True),
        (f"{BASE}?variant=111", f"{BASE}/?variant=111#reviews", True),
        (
            f"HTTPS://{PCC_HOST.upper()}/products/example-nitro-15-en15-0002?variant=111",
            f"{BASE}?variant=111",
            True,
        ),
        (f"{BASE}?_pos=1&_psq=en15&_ss=e", BASE, True),
    ],
)
def test_lead_identity_keeps_the_variant_and_ignores_only_tracking(left, right, same) -> None:
    assert (lead_url_key(left) == lead_url_key(right)) is same
    # The product page identity (one inspection serves every variant) is a different thing.
    assert strip_variant(f"{BASE}?variant=111") == strip_variant(f"{BASE}?variant=222")


def test_a_supplied_variant_url_is_kept_exactly_by_normalization() -> None:
    url, removed = normalize_supplied_url(f"{BASE}?variant=111&utm_medium=x&srsltid=abc#frag")
    assert url == f"{BASE}?variant=111" and set(removed) == {"utm_medium", "srsltid"}
    assert lead_variant(url) == "111" and lead_variant(BASE) == ""


def predictive_hit_for(variant: str):
    link = (
        f"/products/example-nitro-15-en15-0002?variant={variant}&_pos=1"
        if variant
        else ("/products/example-nitro-15-en15-0002?_pos=1")
    )
    body = json.loads(shopify(("example-nitro-15-en15-0002", "Example Nitro 15 EN15-0002")))
    body["resources"]["results"]["products"][0]["url"] = link
    return PCC.parse(
        fetched(PCC.first_page_url("EN15-0002"), json.dumps(body), JSON["content-type"])
    ).hits[0]


def test_two_search_links_to_one_product_with_different_variants_stay_two_hits() -> None:
    first, second, bare = (
        predictive_hit_for("111"),
        predictive_hit_for("222"),
        predictive_hit_for(""),
    )
    assert (first.variant_id, second.variant_id, bare.variant_id) == ("111", "222", "")
    query = SearchQuery("T", "pcc", "1_FULL_MODEL_CODE", "EN15-0002", True, "")
    records = [
        SearchRecord(query, 1, "u", "RESULTS", hit_items=[first]),
        SearchRecord(query, 1, "u", "RESULTS", hit_items=[second, bare, first]),
    ]
    merged = unique_hits(records)
    assert [h.variant_id for h, _ in merged] == ["111", "222", ""]
    assert len(merged[0][1]) == 2  # the same variant link listed twice is one hit, two requests


def test_historical_seeds_of_two_variants_are_two_leads_of_one_candidate() -> None:
    seeds = [
        (f"{BASE}?variant=111", "match_results.csv"),
        (f"{BASE}?variant=222", "store"),
        (f"{BASE}?variant=111&utm_source=a", "store"),
        (BASE, "match_results.csv"),
    ]
    ranked = discover.merge_seeds([], NITRO, "pcc", seeds)
    [candidate] = ranked
    assert candidate.category == HISTORICAL_VERIFIED_URL
    assert [lead_variant(u) for u in candidate.lead_urls] == ["111", "222", ""]
    # A known URL is a lead: the row says it must be revalidated, and no match is carried over.
    row = candidate_row(candidate)
    assert row["lead_status"] == "LEAD_REVALIDATE_REQUIRED" and row["lead_variants"] == "111;222"
    assert "never assumed to match" in row["reasons"]
    assert candidate.decision == ""  # nothing is decided by having been matched before


def test_supplied_variants_of_a_product_native_search_missed_each_stay_a_lead() -> None:
    job = discover.SearchJob(NITRO, "pcc", 1)
    entries = [
        ("S-1", f"{BASE}?variant=111", "SEARCH_TARGET_HINT"),
        ("S-2", f"{BASE}?variant=222", "SEARCH_TARGET_HINT"),
    ]
    report = [
        {"supplied_id": i, "normalized_url": u, "found_by_native_search": ""} for i, u, _ in entries
    ]
    ranked = discover.merge_supplied([], job, entries, report)
    [candidate] = ranked
    assert candidate.category == SUPPLIED_URL
    assert [lead_variant(u) for u in candidate.lead_urls] == ["111", "222"]
    assert candidate_row(candidate)["lead_variants"] == "111;222"
    assert job.supplied == 2 and job.supplied_missed == 2


def test_the_same_url_with_other_tracking_is_one_collection_request_but_variants_are_not() -> None:
    job = discover.SearchJob(NITRO, "pcc", 1)
    candidate = seed_candidate(NITRO, "pcc", f"{BASE}?variant=111", "store")
    other = supplied_candidate(NITRO, "pcc", f"{BASE}?variant=222", "S-2", "hint")
    bare = supplied_candidate(NITRO, "pcc", BASE, "S-3", "hint")
    again = supplied_candidate(NITRO, "pcc", f"{BASE}?variant=111&utm_source=a", "S-4", "hint")
    run_state = discover.DiscoveryRun(Path("."), [job], {}, {}, [])
    new = run_state.add_manifest_rows(
        [
            (candidate, f"{BASE}?variant=111", "111"),
            (other, f"{BASE}?variant=222", "222"),
            (bare, BASE, ""),
            (again, f"{BASE}?variant=111&utm_source=a", "111"),
        ],
        10,
    )
    assert [r["product_url"] for r in new] == [f"{BASE}?variant=111", f"{BASE}?variant=222", BASE]
    assert again.decision == "DUPLICATE_OF_EARLIER_CANDIDATE"
    assert again.collection_request_id == new[0]["request_id"]
    assert add_lead_url(candidate, f"{BASE}?variant=222") and not add_lead_url(
        candidate, f"{BASE}?variant=222&utm_source=z"
    )


def test_a_supplied_url_is_checked_and_matched_and_its_hint_never_decides(tmp_path) -> None:
    """The page is TGT-000001's. The lead names TGT-000002, so the hint only says where to look:
    the matcher assigns the page to the target its own specifications confirm."""
    supplied = tmp_path / "leads.csv"
    supplied.write_text(
        "supplied_id,retailer_id,url,search_target_id,supplied_by,supplied_on,notes\n"
        f"LEAD-1,laptop_factory,{LF_PRODUCT},TGT-000002,test,2026-11-03,a lead\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    code = discover.main(
        [
            "--live",
            "--targets",
            "TGT-000002",
            "--retailers",
            "laptop_factory",
            *inputs(tmp_path),
            "--output-dir",
            str(out),
            "--max-total-requests",
            "30",
            "--collect",
            "--max-identifier-variant-queries",
            "0",
            "--supplied-urls",
            str(supplied),
        ],
        transport=Routes(standard_routes()),
        clock=FakeClock(),
    )
    assert code == 0
    [lead] = rows(out / "collection" / "match_results.csv")
    assert (lead["offer_url"], lead["target_id"], lead["expected_target_id"]) == (
        LF_PRODUCT,
        "TGT-000001",
        "TGT-000002",
    )
    assert lead["expected_target_check"] == "DIFFERENT_TARGET"
    [candidate] = rows(out / "candidates.csv")
    assert candidate["lead_status"] == "LEAD_REVALIDATE_REQUIRED"
    assert candidate["decision"].startswith("COLLECT")
    [job] = rows(out / "search_jobs.csv")
    # TGT-000002 was not satisfied by a lead that merely named it.
    assert job["terminal_outcome"] != "SUITABLE_OFFER_FOUND" and not job["suitable_offer_url"]


# Reuse accounting: the same search or page is requested once


def test_one_search_for_two_targets_is_requested_once_and_counted(tmp_path) -> None:
    twin = dataclasses.replace(NITRO, target_id="TGT-000098", rank="17")
    jobs = [
        discover.SearchJob(NITRO, "laptop_factory", 1, 1, 0),
        discover.SearchJob(twin, "laptop_factory", 1, 1, 0),
    ]
    url = LF.first_page_url("ANV15-42-R2UH")
    transport = Routes(
        {
            url: html(
                url,
                "<html><body><div class='products row'><div "
                "class='product-small'><p class='name product-title'>"
                "<a href='/product/x/'>Acer ANV15-42-R2UH</a></p></div>"
                "</div></body></html>",
            )
        }
    )
    budget = BudgetedTransport(transport, 5)
    fetcher = Fetcher(budget, FetchConfig(), FakeClock())
    args = Namespace(replay_only=False, max_search_requests=5, max_storefront_search_requests=0)
    (tmp_path / "evidence").mkdir()
    sent = discover.run_searches(jobs, fetcher, budget, args, tmp_path / "evidence", lambda *_: 0)
    assert sent == 1 and transport.calls == [url]
    assert [r.cache for job in jobs for r in job.records] == ["", "SAME_RUN"]
    assert jobs[1].records[0].hits == 1  # the second target still reads the answer


def test_the_discovery_summary_counts_every_request_it_avoided(tmp_path) -> None:
    code, transport, out = run(
        tmp_path, "--live", standard_routes(), "--max-total-requests", "30", "--collect"
    )
    assert code == 0
    summary = json.loads((out / "discovery_summary.json").read_text())
    reuse = summary["request_reuse"]
    assert reuse["searches_sent"] == summary["search_requests_sent"]
    assert reuse["requests_avoided_total"] == (
        reuse["searches_reused_same_run"]
        + reuse["searches_reused_saved_cache"]
        + reuse["product_inspections_reused_same_run"]
        + reuse["product_inspections_reused_saved_cache"]
        + reuse["collection_requests_deduplicated"]
        + reuse["collection_pages_reused_from_inspection"]
    )
    # The product page that inspection fetched answers the collector's identical request.
    assert transport.calls.count(PCC_PRODUCT) == 1
    assert reuse["collection_pages_reused_from_inspection"] >= 1


def test_a_saved_inspection_page_is_never_the_price_source_of_a_new_collection(tmp_path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    code, _, first = run(
        one, "--live", standard_routes(), "--max-total-requests", "30", "--collect"
    )
    assert code == 0
    code, transport, out = run(
        two,
        "--live",
        standard_routes(),
        "--max-total-requests",
        "30",
        "--collect",
        "--search-cache",
        str(first),
    )
    assert code == 0
    saved = [r for r in rows(out / "product_inspections.csv") if r["cache"].startswith("SAVED")]
    assert saved  # the inspection came from the earlier run's saved page ...
    # ... but the price comes from a page fetched now: the collector requested it again.
    assert transport.calls.count(PCC_PRODUCT) == 1


# Regression: a PC Central single-variant page with named options crashed the candidate step


def named_options_page(handle: str, variant: str = "51411673481383") -> str:
    offer = {
        "@type": "Offer",
        "url": f"https://{PCC_HOST}/products/{handle}?variant={variant}",
        "price": "36999.00",
        "availability": "https://schema.org/InStock",
    }
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Example Nitro 15 EN15-0002 | Core 5 | 16GB RAM | 512GB SSD",
        "offers": [offer],
    }
    return (
        '<html><head><script type="application/ld+json">'
        + json.dumps(product)
        + '</script></head><body><div data-has-only-default-variant="false"></div></body>'
        "</html>"
    )


def test_a_single_variant_page_with_named_options_is_collected_by_its_variant_url(
    tmp_path,
) -> None:
    handle = "example-nitro-15-en15-0002"
    search = PCC.first_page_url("EN15-0002")
    page = f"https://{PCC_HOST}/products/{handle}"
    routes = {
        search: TransportResponse(
            200,
            search,
            shopify((handle, "Example Nitro 15 EN15-0002 | Core 5 | 16GB | 512GB SSD")).encode(),
            JSON,
        ),
        page: html(page, named_options_page(handle)),
    }
    transport = Routes(routes)
    out = tmp_path / "out"
    code = discover.main(
        [
            "--live",
            "--targets",
            "TGT-000002",
            "--retailers",
            "pcc",
            *inputs(tmp_path),
            "--output-dir",
            str(out),
            "--max-total-requests",
            "10",
            "--max-identifier-variant-queries",
            "0",
        ],
        transport=transport,
        clock=FakeClock(),
    )
    assert code == 0
    inspection = rows(out / "product_inspections.csv")
    assert [r["outcome"] for r in inspection] == ["SINGLE_VARIANT"]  # never STEP_FAILED
    [candidate] = rows(out / "candidates.csv")
    assert candidate["decision"] == "COLLECT_VARIANT"
    assert "Variant 51411673481383 (only variant" in candidate["decision_reason"]
    assert [r["product_url"] for r in rows(out / "manifest.csv")] == [
        f"{page}?variant=51411673481383"
    ]


def saved_evidence(name: str) -> str:
    path = (
        ROOT
        / "data"
        / "output"
        / "weekly_runs"
        / "20261103T062304Z"
        / "discovery"
        / "evidence"
        / name
    )
    if not path.is_file():
        pytest.skip("the 2026-11-03 saved discovery evidence is not in this checkout")
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize(
    ("evidence", "slug", "variant"),
    [
        (
            "fd4e5ba7f0c4cc0e28ba02aba8a9aa3d06696a04af20b0460f839a2e78f78ed3.html",
            "asus-vivobook-15-x1504va-bq4301wsm-intel-core-3-100u-8gb-ddr5-ram-512gb-ssd-uhd-"
            "graphics-pc-central",
            "51411673481383",
        ),
        (
            "832733bd450e07a46b84e7a735f5448373d85f4579ddd4a4c7bb241ef9fcbc6f.html",
            "acer-aspire-go-14-ai-ag14-71m-58jd-steel-gray-14in-wuxga-ips-core-ultra-5-16gb-ram-"
            "512gb-ssd-intel-graphics-win11-home",
            "50641693868199",
        ),
    ],
)
def test_the_saved_pc_central_pages_that_crashed_the_run_inspect_cleanly(
    evidence, slug, variant
) -> None:
    text = saved_evidence(evidence)
    url = f"https://{PCC_HOST}/products/{slug}"
    inspection = inspect_product_page(PCC, url, text)
    assert inspection.outcome == "SINGLE_VARIANT" and inspection.named_options
    [only] = inspection.variants
    assert only.variant_id == variant
    # The call that raised TypeError (a dataclass indexed as a tuple) now reads its fields.
    target = SearchTarget(
        "T",
        "1",
        "ASUS",
        "Vivobook",
        "X1504VA-BQ4301WSM",
        "",
        {
            "model": "X1504VA-BQ4301WSM",
            "screen_size_in": "15.6",
            "cpu_series": "Core 3",
            "ram_capacity_gb": "8",
            "storage_capacity_gb": "512",
        },
    )
    assert variant_evaluation(only, target).reason


# The 2026-11-03 recovery leads


RECOVERY = {
    "REC-pcc-0001": ("pcc", "TGT-000293"),
    "REC-pcc-0002": ("pcc", "TGT-000095"),
    "REC-pcx-0001": ("pcx", "TGT-000003"),
    "REC-pcx-0002": ("pcx", "TGT-000002"),
    "REC-datablitz-0001": ("datablitz", "TGT-000001"),
}


def recovery_rows() -> list[dict[str, str]]:
    with (ROOT / "config" / "recovery_leads_2026-11-03.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        return list(csv.DictReader(handle))


def test_recovery_leads_parse_in_the_supplied_format_and_are_accepted_by_their_adapters() -> None:
    leads = recovery_rows()
    assert {r["supplied_id"]: (r["retailer_id"], r["search_target_id"]) for r in leads} == RECOVERY
    for row in leads:
        normalized, removed = normalize_supplied_url(row["url"])
        assert normalized == row["url"] and not removed
        assert ROUTES[row["retailer_id"]].product_request(normalized).url == normalized
        assert "Provenance:" in row["notes"]
        assert row["supplied_by"].startswith("Recovery 2026-11-03")


def test_recovery_leads_resolve_to_their_targets_through_the_hint() -> None:
    targets = [SearchTarget(t, "1", "B", "S", "M", "", {}) for _, t in RECOVERY.values()]
    args = Namespace(
        supplied_urls=[ROOT / "config" / "recovery_leads_2026-11-03.csv"],
        no_supplied_urls=False,
        catalog_index_run=None,
        catalog_feed_run=[],
    )
    found, report = discover.load_supplied(args, targets, ["pcc", "pcx", "datablitz"])
    assert {key: [item[0] for item in value] for key, value in found.items()} == {
        ("TGT-000293", "pcc"): ["REC-pcc-0001"],
        ("TGT-000095", "pcc"): ["REC-pcc-0002"],
        ("TGT-000003", "pcx"): ["REC-pcx-0001"],
        ("TGT-000002", "pcx"): ["REC-pcx-0002"],
        ("TGT-000001", "datablitz"): ["REC-datablitz-0001"],
    }
    assert {r["assignment_basis"] for r in report} == {"SEARCH_TARGET_HINT"}


def test_recovery_scope_and_targets_match_the_catalog() -> None:
    with (ROOT / "config" / "recovery_scope_2026-11-03.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        scope = {r["target_id"] for r in csv.DictReader(handle)}
    assert scope == {
        "TGT-000293",
        "TGT-000095",
        "TGT-000002",
        "TGT-000003",
        "TGT-000001",
        "TGT-000006",
    }
    master = (
        ROOT / "data" / "output" / "union_2026-11-03" / "inputs" / "target_master_validated.csv"
    )
    if not master.is_file():
        pytest.skip("the 2026-11-03 validated target master is not in this checkout")
    with master.open(newline="", encoding="utf-8-sig") as handle:
        catalog = {r["target_id"]: r for r in csv.DictReader(handle)}
    expect = {
        "TGT-000293": ("ASUS", "X1504VA-BQ4301WSM", "Core 3", "8", "512"),
        "TGT-000095": ("Acer", "AG14-71M-58JD", "Core Ultra 5", "16", "512"),
        "TGT-000003": ("Apple", "MacBook Neo", "Apple A18 Pro", "8", "256"),
        "TGT-000002": ("Apple", "MacBook Neo", "Apple A18 Pro", "8", "512"),
        "TGT-000001": ("Apple", "MacBook Air", "Apple M5", "16", "512"),
        "TGT-000006": ("Apple", "MacBook Air", "Apple M5", "16", "1000"),
    }
    for target, values in expect.items():
        row = catalog[target]
        assert (
            row["brand"],
            row["model"],
            row["cpu_series"],
            row["ram_capacity_gb"],
            row["storage_capacity_gb"],
        ) == values
