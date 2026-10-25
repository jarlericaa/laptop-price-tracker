from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from laptop_tracker import catalog_index as web

URL = "https://pcx.com.ph/products/example-laptop"
WHEN = "2026-10-25T05:00:00+00:00"
CASE = {"case_id": "C1", "retailer_id": "pcx", "model": "FA607NUQ-RL011W",
        "brand": "ASUS", "family": "TUF A16", "specs": "16GB 512GB", "expected_url": URL}
HTML = b'''<title>Actual retailer laptop</title><link rel="canonical"
href="https://pcx.com.ph/products/example-laptop"><script type="application/ld+json">
{"@type":"Product","name":"Retailer name","sku":"retailer-sku",
"offers":[{"price":"12345","priceCurrency":"PHP","availability":"OutOfStock"}]}
</script>'''


@pytest.mark.parametrize("url", [
    "https://pcx.com.ph.evil.invalid/products/laptop",
    "https://evil.invalid/products/laptop", "http://pcx.com.ph/products/laptop",
    "https://user:password@pcx.com.ph/products/laptop",
    "https://pcx.com.ph:8443/products/laptop", "https://pcx.com.ph/collections/laptops",
    URL + "?variant=1&variant=2", URL + "?variant=anything",
])
def test_only_actual_supported_retailer_product_urls_are_accepted(url):
    with pytest.raises(web.DiscoveryError):
        web.product_url(url, "pcx")


def test_collection_product_links_keep_selection_and_remove_tracking():
    url = "https://pcx.com.ph/collections/laptops/products/example-laptop?utm_source=x&variant=123#x"
    assert web.product_url(url, "pcx") == URL + "?variant=123"


def test_query_reformulation_does_not_use_the_expected_answer():
    planned = web.queries(CASE)
    assert planned[0] == 'site:pcx.com.ph "FA607NUQ-RL011W"'
    assert "FA607NUQ RL011W" in planned[1]
    assert "TUF A16" in planned[2]
    assert all(URL not in query for query in planned)
    assert len(web.queries(CASE, 1)) == 1


def test_leads_deduplicate_tracking_but_keep_distinct_variants():
    response = web.SearchResponse("q", "RESULTS", WHEN, [
        {"url": URL + "?utm_source=x", "title": "A", "snippet": ""},
        {"url": URL, "title": "A duplicate", "snippet": ""},
        {"url": URL + "?variant=123", "title": "A variant", "snippet": ""},
        {"url": "https://elsewhere.invalid/products/another", "title": "X", "snippet": ""},
    ])
    found = web.leads([response], "pcx")
    assert [lead["url"] for lead in found] == [URL, URL + "?variant=123"]
    assert found[0]["search_retrieved_at"] == WHEN


def test_page_facts_keep_actual_offers_and_never_invent_configuration_fields():
    result = web.inspect_html(HTML, URL + "?variant=123", "pcx", WHEN)
    assert result["outcome"] == "PRODUCT_PAGE"
    assert result["canonical_url"] == URL + "?variant=123"
    assert result["retrieved_at"] == WHEN
    assert result["products"][0]["offers"][0]["price"] == "12345"
    assert "target_id" not in result and "ram_capacity_gb" not in result


def test_off_domain_canonical_is_refused():
    body = HTML.replace(URL.encode(), b"https://elsewhere.invalid/products/example")
    with pytest.raises(web.DiscoveryError):
        web.inspect_html(body, URL, "pcx", WHEN)


def test_non_product_pages_do_not_count_as_verified_products():
    assert web.inspect_html(b"<title>Website</title>", URL, "pcx", WHEN)[
        "outcome"] == "UNCONFIRMED_PAGE"


def test_incomplete_metadata_and_invalid_jsonld_types_do_not_crash():
    body = b'''<meta property="og:type" content><script type="application/ld+json">
    {"@type":null}</script>'''
    assert web.inspect_html(body, URL, "pcx", WHEN)["outcome"] == "UNCONFIRMED_PAGE"


def test_refused_canonical_keeps_hashable_fetch_evidence():
    from datetime import datetime
    from types import SimpleNamespace

    body = HTML.replace(URL.encode(), b"https://elsewhere.invalid/products/example")

    class Fetcher:
        def fetch(self, request, hosts):
            return SimpleNamespace(status_code=200, retrieved_at=datetime.fromisoformat(WHEN),
                                   content_type="text/html", body=body, final_url=URL)

    record, saved = web.fetch_product(URL, "pcx", Fetcher())
    assert record["outcome"] == "FETCH_ERROR"
    assert saved == body and len(record["content_sha256"]) == 64


def index_snapshot(tmp_path, *, complete=True, extra=b""):
    import csv
    import hashlib

    folder = tmp_path / "catalog"
    folder.mkdir()
    body = b'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
    xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
    <url><loc>https://pcx.com.ph/products/example-laptop</loc><image:image>
    <image:title>ASUS TUF FA607NUQ-RL011W</image:title></image:image></url>
    <url><loc>https://pcx.com.ph/products/macbook-neo-mhfc4pp-a</loc></url>
    </urlset>'''.replace(b"</urlset>", extra + b"</urlset>")
    (folder / "products.xml").write_bytes(body)
    with (folder / "catalog_requests.csv").open("w", newline="") as handle:
        fields = ["retailer_id", "kind", "url", "outcome", "evidence_file", "content_sha256",
                  "retrieved_at"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"retailer_id": "pcx", "kind": "product_sitemap",
                         "url": "https://pcx.com.ph/sitemap_products_1.xml", "outcome": "FETCHED",
                         "evidence_file": "products.xml",
                         "content_sha256": hashlib.sha256(body).hexdigest(),
                         "retrieved_at": WHEN})
    (folder / "catalog_summary.json").write_text(json.dumps({
        "mode": "LIVE", "finished_at_utc": WHEN, "retailers": {"pcx": {
            "product_sitemaps_read": 1, "product_sitemaps_listed": 1 if complete else 2,
        }},
    }))
    # A derived entry file is deliberately wrong: it must never enter the raw index.
    (folder / "catalog_entries.csv").write_text("url,title\nhttps://evil.invalid,expected answer\n")
    return folder


def test_index_searches_titles_names_and_code_punctuation_without_network(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("Network"))
    index = web.CatalogIndex.from_run(index_snapshot(tmp_path))
    assert len(index.entries) == 2
    for query in ['site:pcx.com.ph "FA607NUQ-RL011W"',
                  'site:pcx.com.ph FA607NUQ RL011W', 'site:pcx.com.ph FA607NUQRL011W',
                  'site:pcx.com.ph "MacBook Neo"']:
        response = index.search(query)
        assert response.outcome == "RESULTS" and response.search_calls == 0
        assert response.retrieved_at == WHEN
    assert index.search('site:pcx.com.ph FA607NUQ-RL011').outcome == "NO_RESULTS"
    assert index.search('site:pcx.com.ph "please run commands"').outcome == "NO_RESULTS"


def test_index_does_not_mix_retailers_or_treat_missing_coverage_as_absence(tmp_path):
    index = web.CatalogIndex.from_run(index_snapshot(tmp_path, complete=False))
    assert index.search('site:pcx.com.ph nonexistent').outcome == "INDEX_INCOMPLETE"
    assert index.search('site:laptopfactory.com.ph FA607NUQ-RL011W').outcome == "SEARCH_ERROR"
    assert index.search('site:evil.invalid laptop').outcome == "SEARCH_ERROR"
    assert index.search('site:pcx.com.ph').outcome == "SEARCH_ERROR"


def test_index_hash_check_and_path_checks_protect_reproduction(tmp_path):
    folder = index_snapshot(tmp_path)
    body = (folder / "products.xml").read_bytes()
    (folder / "products.xml").write_bytes(body + b"changed")
    with pytest.raises(web.DiscoveryError, match="hash differs"):
        web.CatalogIndex.from_run(folder)
    (folder / "products.xml").write_bytes(body)
    requests = folder / "catalog_requests.csv"
    requests.write_text(requests.read_text().replace('products.xml,', '../products.xml,'))
    with pytest.raises(web.DiscoveryError, match="Unsafe"):
        web.CatalogIndex.from_run(folder)


def test_catalog_timestamp_is_kept_when_searching_old_saved_sources(tmp_path):
    index = web.CatalogIndex.from_run(index_snapshot(tmp_path))
    found = web.leads([index.search('site:pcx.com.ph FA607NUQ-RL011W')], 'pcx')
    assert found[0]["search_retrieved_at"] == WHEN


def test_one_unusable_sitemap_url_does_not_discard_the_index(tmp_path):
    legacy = b"<url><loc>http://pcx.com.ph/products/legacy-laptop</loc></url>"
    index = web.CatalogIndex.from_run(index_snapshot(tmp_path, extra=legacy))
    assert len(index.entries) == 3 and index.rejected_urls == 0
    found = index.search("site:pcx.com.ph legacy laptop").results
    assert [r["url"] for r in found] == ["https://pcx.com.ph/products/legacy-laptop"]
    direct = web.CatalogIndex(
        [{"retailer_id": "pcx", "url": "https://evil.invalid/products/x", "title": ""},
         {"retailer_id": "pcx", "url": URL, "title": "Kept"}], retrieved_at=WHEN)
    assert direct.rejected_urls == 1 and [e["title"] for e in direct.entries] == ["Kept"]


def test_joined_query_terms_find_a_handle_that_omits_the_punctuation(tmp_path):
    handle = b"<url><loc>https://pcx.com.ph/products/hp-15fd0001wm</loc></url>"
    index = web.CatalogIndex.from_run(index_snapshot(tmp_path, extra=handle))
    for query in ['site:pcx.com.ph "15-FD0001WM"', "site:pcx.com.ph HP 15 FD0001WM"]:
        assert [r["url"] for r in index.search(query).results] == [
            "https://pcx.com.ph/products/hp-15fd0001wm"]
    assert index.search("site:pcx.com.ph 15 FD0001").outcome == "NO_RESULTS"


def test_brand_alone_is_not_a_fallback_query():
    case = {"retailer_id": "pcx", "model": "X1", "brand": "HP"}
    assert len(web.queries(case)) == 2
    assert len(web.queries({**case, "specs": "16GB"})) == 3


def test_feed_enriches_a_sitemap_url_without_replacing_its_identity_or_provenance():
    sitemap = {"retailer_id": "pcx", "url": URL, "title": "Sitemap title",
               "sitemap": "sitemap-1", "source_retrieved_at": "early"}
    feed = {"retailer_id": "pcx", "url": URL, "title": "Feed title", "search_text": "extra clue",
            "sitemap": "feed-1", "source_retrieved_at": "late"}
    index = web.CatalogIndex([sitemap, feed], retrieved_at=WHEN, coverage={
        "pcx": {"product_sitemaps_read": 1, "product_sitemaps_listed": 1}})
    (entry,) = index.entries
    assert (entry["title"], entry["sitemap"], entry["source_retrieved_at"]) == (
        "Sitemap title", "sitemap-1", "early")
    for query in ("site:pcx.com.ph feed title", "site:pcx.com.ph extra clue"):
        assert len(index.search(query).results) == 1


def test_narrower_feed_run_does_not_replace_proven_sitemap_coverage(tmp_path):
    (tmp_path / "main").mkdir()
    (tmp_path / "feed").mkdir()
    main = index_snapshot(tmp_path / "main")
    feed = index_snapshot(tmp_path / "feed", complete=False)
    index = web.CatalogIndex.from_run(main, feed_runs=(feed,))
    assert index.search("site:pcx.com.ph nonexistent").outcome == "NO_RESULTS"


def test_rejected_start_leaves_the_output_folder_reusable(tmp_path):
    cases = tmp_path / "cases.csv"
    cases.write_text("case_id,retailer_id,model\nC1,pcx,FA607NUQ-RL011W\n")
    output = tmp_path / "out"
    with pytest.raises(SystemExit):
        cli().main(["--cases", str(cases), "--output", str(output)])
    assert not any(output.iterdir())
    folder = index_snapshot(tmp_path)
    (folder / "products.xml").write_bytes(b"changed")
    with pytest.raises(web.DiscoveryError):
        cli().main(["--cases", str(cases), "--index-run", str(folder), "--output", str(output)])
    assert not any(output.iterdir())


def test_target_with_an_unusable_name_is_reported_and_other_targets_still_search(tmp_path):
    from types import SimpleNamespace

    from test_verified_top import load_script
    discover = load_script("discover_products")
    folder = index_snapshot(tmp_path)

    def target(target_id, model, brand="ASUS"):
        return SimpleNamespace(target_id=target_id, model=model, recorded_code="", series="",
                               brand=brand, core={"cpu_series": "", "ram_capacity_gb": "",
                                                  "storage_capacity_gb": ""})

    args = SimpleNamespace(catalog_index_run=folder, catalog_feed_run=[])
    rows, skipped = discover.indexed_candidate_rows(
        args, [target("T1", 'Apple MacBook Air 13" M4'), target("T2", "nan"),
               target("T3", "FA607NUQ-RL011W")], ["pcx"])
    assert [(s["target_id"], s["retailer_id"]) for s in skipped] == [("T1", "pcx"), ("T2", "pcx")]
    assert [r["search_target_id"] for r in rows] == ["T3"] and rows[0]["url"] == URL


def cli():
    spec = importlib.util.spec_from_file_location(
        "search_catalog_index_test", Path(__file__).parents[1] / "search_catalog_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_search_is_offline_and_exports_only_discovered_leads(tmp_path, monkeypatch):
    module = cli()
    folder = index_snapshot(tmp_path)
    path = tmp_path / "cases.csv"
    path.write_text("case_id,retailer_id,model,expected_url\nC1,pcx,FA607NUQ-RL011W," + URL + "\n")
    monkeypatch.setattr(module, "fetch_product", lambda *a: pytest.fail("Page fetch offline"))
    monkeypatch.setattr(module, "native_search", lambda *a: pytest.fail("Native fetch offline"))
    output = tmp_path / "offline"
    assert module.main(["--cases", str(path), "--index-run", str(folder),
                        "--output", str(output)]) == 0
    run = json.loads((output / "run.json").read_text())
    report = json.loads((output / "metrics.json").read_text())
    assert run["pages"] == {} and report["index_expected_found"] == 1
    assert report["search_backend"] == "retailer_sitemap_index"
    assert all(r["search_calls"] == 0 for r in run["searches"]["C1"]["index"])
    assert URL in (output / "supplied_urls.csv").read_text()
    assert WHEN in (output / "supplied_urls.csv").read_text()


def test_replay_preserves_observation_times_and_checks_saved_body_hash(tmp_path, monkeypatch):
    module = cli()
    original, replay = tmp_path / "original", tmp_path / "replay"
    original.mkdir()
    page = web.inspect_html(HTML, URL, "pcx", WHEN)
    page.update(body_file="page.html", retailer_id="pcx")
    (original / "page.html").write_bytes(HTML)
    response = asdict(web.SearchResponse("q", "RESULTS", WHEN, [
        {"url": URL, "title": "Found", "snippet": ""},
    ], 1))
    run = {"cases": [CASE], "searches": {"C1": {"index": [response], "native": []}},
           "pages": {URL: page}}
    (original / "run.json").write_text(json.dumps(run))
    monkeypatch.setattr(module, "fetch_product", lambda *args: pytest.fail("Network in replay"))
    assert module.main(["--replay", str(original), "--output", str(replay)]) == 0
    assert json.loads((replay / "metrics.json").read_text())["new_expected_pages"] == 1
    assert json.loads((original / "run.json").read_text())["pages"][URL]["retrieved_at"] == WHEN
    (original / "page.html").write_bytes(b"changed")
    with pytest.raises(web.DiscoveryError, match="hash differs"):
        module.main(["--replay", str(original), "--output", str(tmp_path / "bad")])


def test_benchmark_does_not_remove_unreachable_expected_pages_from_denominator():
    report = cli().metrics([CASE], {}, {URL: {"outcome": "FETCH_ERROR"}})
    assert report["labelled_cases"] == 1
    assert report["index_expected_found"] == 0
    assert report["per_case"][0]["expected_fetch_outcome"] == "FETCH_ERROR"


def test_export_never_promotes_a_ground_truth_only_url_to_a_supplied_lead(tmp_path):
    run = {"cases": [CASE], "searches": {"C1": {"index": [], "native": []}},
           "pages": {URL: {"outcome": "PRODUCT_PAGE"}}}
    cli().write_reports(tmp_path, run)
    assert len((tmp_path / "supplied_urls.csv").read_text().splitlines()) == 1


def test_cli_runs_from_a_fresh_checkout_without_an_editable_install(tmp_path):
    folder = index_snapshot(tmp_path)
    cases = tmp_path / "cases.csv"
    cases.write_text("case_id,retailer_id,model\nC1,pcx,FA607NUQ-RL011W\n")
    process = subprocess.run([
        sys.executable, str(Path(__file__).parents[1] / "search_catalog_index.py"),
        "--cases", str(cases), "--index-run", str(folder), "--output", str(tmp_path / "search"),
    ], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert process.returncode == 0, process.stderr
    assert '"search_backend": "retailer_sitemap_index"' in process.stdout


def test_live_fetch_round_trip_keeps_unconfirmed_configurations(tmp_path, monkeypatch):
    module = cli()
    folder = index_snapshot(tmp_path)
    cases = tmp_path / "cases.csv"
    cases.write_text("case_id,retailer_id,model\nC1,pcx,FA607NUQ-RL011W\n")
    calls = []
    def fetch(url, retailer, fetcher):
        calls.append(url)
        return web.inspect_html(HTML, url, retailer, WHEN), HTML
    monkeypatch.setattr(module, "fetch_product", fetch)
    output = tmp_path / "live"
    assert module.main(["--live", "--cases", str(cases), "--index-run", str(folder),
                        "--output", str(output)]) == 0
    assert calls == [URL]
    assert module.main(["--replay", str(output), "--output", str(tmp_path / "replay")]) == 0
    assert len(calls) == 1
    page = json.loads((output / "run.json").read_text())["pages"][URL]
    assert "target_id" not in page and "ram_capacity_gb" not in page


def test_expanded_benchmark_predeclares_bounded_native_queries_without_network(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "benchmark_catalog_index_test", Path(__file__).parents[1] / "benchmark_catalog_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    source.mkdir()
    (source / "run.json").write_text(json.dumps({"cases": [CASE]}))
    output = tmp_path / "plan"
    assert module.main(["--run", str(source), "--output", str(output)]) == 0
    planned = json.loads((output / "native_plan.json").read_text())["C1"]
    assert 1 <= len(planned) <= 7
    assert any("FA607NUQ RL011W" in query for query in planned)
    assert all(URL not in query for query in planned)


def test_benchmark_reuses_only_identical_native_cohort_and_query_words(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "benchmark_index_reuse", Path(__file__).parents[1] / "benchmark_catalog_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = index_snapshot(tmp_path)
    source, native = tmp_path / "source", tmp_path / "native"
    source.mkdir()
    native.mkdir()
    run = {"cases": [CASE], "pages": {}, "index_run": str(catalog),
           "searches": {"C1": {"index": [], "native": []}}}
    (source / "run.json").write_text(json.dumps(run))
    plan = tmp_path / "plan"
    module.main(["--run", str(source), "--output", str(plan)])
    words = json.loads((plan / "native_plan.json").read_text())["C1"]
    original = {**run, "searches": {"C1": {"native": [asdict(web.SearchResponse(
        "site:pcx " + query, "RESULTS", WHEN,
        [{"url": URL, "title": "Retailer", "snippet": ""}], 1,
    )) for query in words]}}, "native_expanded_started_at": WHEN,
        "native_expanded_finished_at": WHEN}
    (native / "run.json").write_text(json.dumps(original))
    output = tmp_path / "combined"
    assert module.main(["--run", str(source), "--native-run", str(native),
                        "--repeat-queries", "20", "--output", str(output)]) == 0
    performance = json.loads((output / "performance.json").read_text())
    assert performance["query_count"] == 20 and performance["query_network_requests"] == 0
    assert json.loads((output / "metrics.json").read_text())["native_expected_found"] == 1
    original["searches"]["C1"]["native"][0]["query"] = "site:pcx different model"
    (native / "run.json").write_text(json.dumps(original))
    with pytest.raises(ValueError, match="query plan differs"):
        module.main(["--run", str(source), "--native-run", str(native),
                     "--output", str(tmp_path / "bad_queries")])
    original["cases"] = [{**CASE, "model": "Different model"}]
    (native / "run.json").write_text(json.dumps(original))
    with pytest.raises(ValueError, match="cohort differs"):
        module.main(["--run", str(source), "--native-run", str(native),
                     "--output", str(tmp_path / "bad_cohort")])
