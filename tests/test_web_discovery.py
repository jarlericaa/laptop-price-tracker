from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from laptop_tracker import web_discovery as web

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


def fake_cli(monkeypatch, *, events=True, exit_code=0, malformed=False, timeout=False):
    observed = {}

    class Process:
        pid = 12345
        returncode = exit_code

        def __init__(self, command, **kwargs):
            observed.update(command=command, options=kwargs)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text("not json" if malformed else json.dumps({"results": [
                {"url": URL, "title": "Found on retailer", "snippet": "Page text"},
            ]}), encoding="utf-8")
            self.first = True

        def communicate(self, prompt=None, timeout=None):
            observed["prompt"] = prompt
            if self.first and timeout is not None and timeout_flag:
                self.first = False
                raise subprocess.TimeoutExpired("codex", 1)
            event = {"type": "item.completed", "item": {"type": "web_search"}}
            return (json.dumps(event) if events else "", "authentication failed SECRET")

        def kill(self):
            observed["killed"] = self.pid

    timeout_flag = timeout
    monkeypatch.setattr(web.subprocess, "Popen", Process)
    monkeypatch.setenv("OPENAI_API_KEY", "SECRET")
    monkeypatch.setenv("CODEX_API_KEY", "SECRET")
    monkeypatch.setattr(web.os, "killpg", lambda *args: observed.update(killed=args), raising=False)
    return observed


def test_subscription_search_disables_commands_and_records_real_search_events(monkeypatch):
    observed = fake_cli(monkeypatch)
    response = web.CodexSearch().search("site:pcx.com.ph laptop")
    assert response.outcome == "RESULTS" and response.search_calls == 1
    assert "features.shell_tool=false" in observed["command"]
    assert 'forced_login_method="chatgpt"' in observed["command"]
    assert "--ignore-user-config" in observed["command"]
    assert "OPENAI_API_KEY" not in observed["options"]["env"]
    assert "CODEX_API_KEY" not in observed["options"]["env"]


@pytest.mark.parametrize("options,outcome", [
    ({"events": False}, "SEARCH_ERROR"),
    ({"exit_code": 1}, "AUTH_ERROR"),
    ({"malformed": True}, "SEARCH_ERROR"),
    ({"timeout": True}, "SEARCH_TIMEOUT"),
])
def test_search_failures_are_not_no_results_or_secret_disclosures(monkeypatch, options, outcome):
    fake_cli(monkeypatch, **options)
    response = web.CodexSearch().search("query")
    assert response.outcome == outcome
    assert response.results == [] and "SECRET" not in response.error


def cli():
    spec = importlib.util.spec_from_file_location(
        "discover_web_leads_test", Path(__file__).parents[1] / "discover_web_leads.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preview_sends_no_search_or_page_requests(tmp_path, monkeypatch):
    module = cli()
    path = tmp_path / "cases.csv"
    path.write_text("case_id,retailer_id,model\nC1,pcx,FA607NUQ-RL011W\n")
    monkeypatch.setattr(web.CodexSearch, "search", lambda *args: pytest.fail("Search in preview"))
    assert module.main(["--cases", str(path), "--output", str(tmp_path / "preview")]) == 0


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
    run = {"cases": [CASE], "searches": {"C1": {"web": [response], "native": []}},
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
    assert report["web_expected_found"] == 0
    assert report["per_case"][0]["expected_fetch_outcome"] == "FETCH_ERROR"


def test_export_never_promotes_a_ground_truth_only_url_to_a_supplied_lead(tmp_path):
    run = {"cases": [CASE], "searches": {"C1": {"web": [], "native": []}},
           "pages": {URL: {"outcome": "PRODUCT_PAGE"}}}
    cli().write_reports(tmp_path, run)
    assert len((tmp_path / "supplied_urls.csv").read_text().splitlines()) == 1


def test_cli_runs_from_a_fresh_checkout_without_an_editable_install(tmp_path):
    cases = tmp_path / "cases.csv"
    cases.write_text("case_id,retailer_id,model\nC1,pcx,FA607NUQ-RL011W\n")
    process = subprocess.run([
        sys.executable, str(Path(__file__).parents[1] / "discover_web_leads.py"),
        "--cases", str(cases), "--output", str(tmp_path / "preview"),
    ], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert process.returncode == 0, process.stderr
    assert "no searches or retailer requests sent" in process.stdout


def test_expanded_benchmark_predeclares_bounded_native_queries_without_network(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "benchmark_web_discovery_test", Path(__file__).parents[1] / "benchmark_web_discovery.py")
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
