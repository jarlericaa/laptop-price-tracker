"""Manual online collection and matching, tested offline with fake transports and clocks."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from scraping_fakes import FakeClock, FakeTransport, no_network  # noqa: F401
from test_extraction import FIXTURE_PAGES, FIXTURES, HTML
from test_match_run import TARGET_ROWS
from test_match_run import load_cli as load_match_offers
from test_price_tracking import ledger, list_rows
from test_target_master import target_row, target_values
from test_verified_top import load_script

from laptop_tracker.adapters.laptop_factory import LaptopFactoryAdapter
from laptop_tracker.collection import (
    MANIFEST_COLUMNS,
    ManifestError,
    collect,
    collected_observations,
    match_collected,
    read_manifest,
    result_row,
    review_reasons,
    validate_manifest,
)
from laptop_tracker.match_run import read_observations_csv
from laptop_tracker.matching import ExactMatcher
from laptop_tracker.observation_store import MatchingContext, ObservationStore
from laptop_tracker.scraping import FetchConfig, Fetcher, TransportError, TransportResponse
from laptop_tracker.verified_top import CONFIGURATION_COLUMNS, LEDGER_COLUMNS

pytestmark = pytest.mark.usefixtures("no_network")

ROOT = Path(__file__).resolve().parents[1]
SELECTED = ("TGT-000001", "TGT-000002")


def page(name: str, url: str | None = None) -> TransportResponse:
    _, path, fixture_url = FIXTURE_PAGES[name]
    return TransportResponse(200, url or fixture_url, (FIXTURES / path).read_bytes(), HTML)


def row(request_id: str, name: str, expected: str = "", enabled: str = "TRUE", **changes):
    adapter_class, _, url = FIXTURE_PAGES[name]
    values = {
        "request_id": request_id,
        "retailer_id": adapter_class.retailer_id,
        "product_url": url,
        "expected_target_id": expected,
        "enabled": enabled,
    }
    return values | changes


def write_dicts(path: Path, columns, rows) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)
    return path


def read_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def args_for(tmp_path: Path, manifest_rows, *, targets=TARGET_ROWS, ids=SELECTED, mode="--live"):
    with (tmp_path / "tm.csv").open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(target_values(*targets))
    return [
        mode,
        "--manifest",
        str(write_dicts(tmp_path / "manifest.csv", MANIFEST_COLUMNS, manifest_rows)),
        "--target-master",
        str(tmp_path / "tm.csv"),
        "--selected-list",
        str(write_dicts(tmp_path / "top.csv", CONFIGURATION_COLUMNS, list_rows(ids, targets))),
        "--contributions",
        str(write_dicts(tmp_path / "ledger.csv", LEDGER_COLUMNS, ledger())),
        "--store",
        str(tmp_path / "store" / "history.sqlite3"),
        "--output-root",
        str(tmp_path / "runs"),
    ]


def run(tmp_path, manifest_rows, *script, clock=None, live_transport=False, **kwargs):
    clock = clock or FakeClock()
    transport = FakeTransport(clock, *script, live=live_transport)
    code = load_script("collect_and_match").main(
        args_for(tmp_path, manifest_rows, **kwargs), transport=transport, clock=clock
    )
    return code, transport, clock


def only_run_dir(tmp_path: Path) -> Path:
    [run_dir] = sorted((tmp_path / "runs").iterdir())
    return run_dir


# Preview and up-front validation


def test_preview_sends_no_request_and_does_not_open_the_store(tmp_path: Path, capsys) -> None:
    rows = [row("a", "lf_sale", "TGT-000001"), row("b", "dbz_sale", enabled="FALSE")]
    code, transport, _ = run(tmp_path, rows, mode="--preview")

    assert code == 0
    assert transport.calls == []
    assert not (tmp_path / "store").exists()
    run_dir = only_run_dir(tmp_path)
    assert run_dir.name.endswith("_preview")
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "collection_run_summary.json",
        "request_report.csv",
    ]
    statuses = [r["request_status"] for r in read_dicts(run_dir / "request_report.csv")]
    assert statuses == ["NOT_SENT_PREVIEW", "SKIPPED_DISABLED"]
    summary = json.loads((run_dir / "collection_run_summary.json").read_text())
    assert (summary["run_status"], summary["requests_sent"]) == ("PREVIEW_ONLY", 0)
    assert "No retailer request was made" in capsys.readouterr().out


SELECTED_SET = {"TGT-000001": 1, "TGT-000002": 2}


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        ([row("a", "lf_sale", retailer_id="laptopfactory")], "UNSUPPORTED_RETAILER_ID"),
        ([row("a", "lf_sale", retailer_id="")], "MISSING_RETAILER_ID"),
        (
            [row("a", "lf_sale", product_url=FIXTURE_PAGES["lf_sale"][2] + "?add-to-cart=1")],
            "URL_REJECTED_BY_ADAPTER",
        ),
        (
            [row("a", "lf_sale", product_url=FIXTURE_PAGES["lf_sale"][2].replace("https", "http"))],
            "URL_REJECTED_BY_ADAPTER",
        ),
        (
            [row("a", "lf_sale", product_url="https://laptopfactory.com.ph/shop/?s=acer")],
            "URL_REJECTED_BY_ADAPTER",
        ),
        (
            [row("a", "oct_variants",
                 product_url=FIXTURE_PAGES["oct_variants"][2].replace("www.", ""))],
            "URL_REJECTED_BY_ADAPTER",
        ),
        (
            [row("a", "pcc_variants",
                 product_url=FIXTURE_PAGES["pcc_variants"][2].split("?")[0] + "?variant=abc")],
            "URL_REJECTED_BY_ADAPTER",
        ),
        ([row("a", "lf_sale", product_url="")], "MISSING_PRODUCT_URL"),
        ([row("a", "lf_sale"), row("a", "dbz_sale")], "DUPLICATE_REQUEST_ID"),
        ([row("", "lf_sale")], "MISSING_REQUEST_ID"),
        ([row("bad id", "lf_sale")], "INVALID_REQUEST_ID"),
        ([row("a", "lf_sale", enabled="yes")], "ENABLED_NOT_TRUE_OR_FALSE"),
        ([row("a", "lf_sale", expected="TGT-000003")], "EXPECTED_TARGET_NOT_IN_SELECTED_LIST"),
        ([row("a", "lf_sale", expected="tgt-1")], "INVALID_TARGET_ID"),
        ([row(" a", "lf_sale")], "SURROUNDING_WHITESPACE"),
        ([row("a", "lf_sale"), row("b", "lf_sale")], "DUPLICATE_REQUEST"),
        (
            [row("a", "lf_sale", "TGT-000001"), row("b", "lf_sale", "TGT-000002")],
            "CONFLICTING_DUPLICATE_REQUEST",
        ),
        (
            [row("a", "lf_sale"),
             row("b", "lf_sale", product_url=FIXTURE_PAGES["lf_sale"][2].rstrip("/"))],
            "DUPLICATE_REQUEST",
        ),
        ([row("a", "lf_sale", enabled="FALSE")], "NO_ENABLED_REQUESTS"),
    ],
)
def test_invalid_manifests_are_refused_as_a_whole(rows, code: str) -> None:
    rows = [r | {"_row_number": str(n)} for n, r in enumerate(rows, start=2)]
    with pytest.raises(ManifestError) as raised:
        validate_manifest(rows, SELECTED_SET)
    assert code in {issue.code for issue in raised.value.issues}
    # Messages name rows, fields, and codes, never cell values.
    assert "http" not in str(raised.value)


def test_selected_variant_urls_are_kept_exactly_and_never_added() -> None:
    entries = validate_manifest(
        [row("pcc", "pcc_variants"), row("oct", "oct_variants"), row("bare", "pcc_single")],
        SELECTED_SET,
    )
    assert [e.request.url for e in entries] == [
        FIXTURE_PAGES["pcc_variants"][2],
        FIXTURE_PAGES["oct_variants"][2],
        FIXTURE_PAGES["pcc_single"][2],
    ]
    assert "?variant=" not in entries[2].request.url


def test_the_template_is_well_formed_and_disabled(tmp_path: Path) -> None:
    template = ROOT / "config" / "collection_manifest_template.csv"
    rows = read_manifest(template)
    assert {r["retailer_id"] for r in rows} == {"laptop_factory", "datablitz", "pcx", "pcc", "oct"}
    with pytest.raises(ManifestError, match="NO_ENABLED_REQUESTS"):
        validate_manifest(rows, SELECTED_SET)
    enabled = validate_manifest([r | {"enabled": "TRUE"} for r in rows], SELECTED_SET)
    assert len(enabled) == 5


def test_a_wrong_manifest_header_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "m.csv"
    path.write_text("request_id,retailer,product_url,expected_target_id,enabled\n")
    with pytest.raises(ManifestError, match="HEADER_NOT_MANIFEST_COLUMNS"):
        read_manifest(path)


@pytest.mark.parametrize(
    "case", ["manifest", "duplicate_catalog", "selected_identity_changed", "inactive_selected"]
)
def test_invalid_inputs_stop_before_any_request_or_import(tmp_path, capsys, case) -> None:
    rows = [row("a", "lf_sale", "TGT-000001")]
    targets = list(TARGET_ROWS)
    ids = SELECTED
    if case == "manifest":
        rows = [row("a", "lf_sale", "TGT-000003")]
    elif case == "duplicate_catalog":
        targets = [*TARGET_ROWS, TARGET_ROWS[0]]
    clock = FakeClock()
    transport = FakeTransport(clock, page("lf_sale"))
    script = load_script("collect_and_match")
    args = args_for(tmp_path, rows, targets=targets, ids=ids)
    if case == "selected_identity_changed":
        top = list_rows(ids, TARGET_ROWS)
        top[0] = top[0] | {"ram_capacity_gb": "8"}
        write_dicts(tmp_path / "top.csv", CONFIGURATION_COLUMNS, top)
    elif case == "inactive_selected":
        write_dicts(
            tmp_path / "top.csv",
            CONFIGURATION_COLUMNS,
            list_rows(("TGT-000001", "TGT-000003"), TARGET_ROWS),
        )

    assert script.main(args, transport=transport, clock=clock) == 1
    assert "before any retailer request" in capsys.readouterr().err
    assert transport.calls == []
    assert not (tmp_path / "store").exists()
    assert not (tmp_path / "runs").exists()


def test_an_existing_run_directory_is_never_reused(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    clock = FakeClock()
    transport = FakeTransport(clock, page("lf_sale"))
    args = args_for(tmp_path, [row("a", "lf_sale")]) + ["--output-dir", str(tmp_path / "taken")]
    assert load_script("collect_and_match").main(args, transport=transport, clock=clock) == 1
    assert transport.calls == []


# Collection, persistence, and matching


def test_live_run_routes_adapters_stores_and_matches(tmp_path: Path, capsys) -> None:
    rows = [
        row("lf", "lf_sale", "TGT-000001"),
        row("pcc", "pcc_single", "TGT-000002"),
        row("dbz", "dbz_sale"),
        row("oct", "oct_variants"),
        row("off", "pcx_regular", enabled="FALSE"),
    ]
    code, transport, _ = run(
        tmp_path,
        rows,
        page("lf_sale"),
        page("pcc_single"),
        page("dbz_sale"),
        page("oct_variants"),
    )

    assert code == 0
    assert [call["url"] for call in transport.calls] == [
        FIXTURE_PAGES[name][2] for name in ("lf_sale", "pcc_single", "dbz_sale", "oct_variants")
    ]
    run_dir = only_run_dir(tmp_path)
    requests = {r["request_id"]: r for r in read_dicts(run_dir / "request_report.csv")}
    assert requests["off"]["request_status"] == "SKIPPED_DISABLED"
    assert {requests[k]["request_status"] for k in ("lf", "pcc", "dbz", "oct")} == {"SUCCEEDED"}

    results = {r["request_id"]: r for r in read_dicts(run_dir / "match_results.csv")}
    lf = results["lf"]
    assert (lf["match_status"], lf["target_id"], lf["expected_target_check"]) == (
        "EXACT",
        "TGT-000001",
        "AS_EXPECTED",
    )
    assert (lf["selected_list_status"], lf["selected_rank"]) == ("SELECTED", "1")
    assert lf["retrieved_at"] == "2026-09-29T04:00:00.000000Z"
    assert lf["adapter_version"] == "lf-1" and len(lf["content_sha256"]) == 64
    # A secondary conflict stays in review, even though it was the expected target.
    pcc = results["pcc"]
    assert (pcc["match_status"], pcc["target_id"]) == ("REVIEW_REQUIRED", "")
    assert "SECONDARY_CONFLICT" in pcc["reason_codes"]
    assert pcc["expected_target_check"] == "NOT_EXACT"
    # No model label: kept with its reason, and no model is manufactured from the title.
    dbz = results["dbz"]
    assert dbz["match_status"] == "REVIEW_REQUIRED"
    assert "MISSING_MODEL" in dbz["reason_codes"] and dbz["offer_model"] == ""
    # Shared multi-variant specifications stay untrusted.
    assert "MULTI_VARIANT_SHARED_DESCRIPTION" in results["oct"]["reason_codes"]

    review = {r["request_id"]: r["review_reasons"] for r in read_dicts(
        run_dir / "match_review_queue.csv"
    )}
    assert review == {
        "pcc": "MATCH_REVIEW_REQUIRED;EXPECTED_TARGET_NOT_MATCHED",
        "dbz": "MATCH_REVIEW_REQUIRED",
        "oct": "MATCH_REVIEW_REQUIRED",
    }
    evidence = read_dicts(run_dir / "match_evidence.csv")
    lf_model = [
        e for e in evidence
        if e["request_id"] == "lf" and e["stage"] == "EXTRACTION" and e["field"] == "model"
    ]
    assert lf_model and lf_model[0]["source_label"] == "Model Name"

    with ObservationStore.open(tmp_path / "store" / "history.sqlite3") as store:
        counts = store.counts()
        [context] = store.contexts()
        stored = store.match_results(context["context_id"])
    assert (counts["observations"], counts["match_results"], counts["import_batches"]) == (4, 4, 1)
    assert stored[lf["observation_id"]].target_id == "TGT-000001"
    assert lf["matching_context_id"] == context["context_id"]

    printed = capsys.readouterr().out
    assert "SUCCESS" in printed
    assert "http" not in printed and "TGT-" not in printed


def test_expected_target_is_only_a_hint(tmp_path: Path) -> None:
    code, _, _ = run(tmp_path, [row("lf", "lf_sale", "TGT-000002")], page("lf_sale"))

    assert code == 0
    [result] = read_dicts(only_run_dir(tmp_path) / "match_results.csv")
    assert (result["target_id"], result["expected_target_check"]) == (
        "TGT-000001",
        "DIFFERENT_TARGET",
    )
    [review] = read_dicts(only_run_dir(tmp_path) / "match_review_queue.csv")
    assert review["review_reasons"] == "EXPECTED_TARGET_DIFFERS"


def test_exact_matches_outside_the_selected_list_are_labelled(tmp_path: Path) -> None:
    code, _, _ = run(tmp_path, [row("lf", "lf_sale")], page("lf_sale"), ids=("TGT-000002",))

    assert code == 0
    [result] = read_dicts(only_run_dir(tmp_path) / "match_results.csv")
    assert (result["match_status"], result["selected_list_status"]) == (
        "EXACT",
        "NOT_IN_SELECTED_LIST",
    )
    [review] = read_dicts(only_run_dir(tmp_path) / "match_review_queue.csv")
    assert review["review_reasons"] == "EXACT_TARGET_NOT_IN_SELECTED_LIST"


def test_ambiguous_outcomes_are_kept(tmp_path: Path) -> None:
    clock = FakeClock()
    entries = validate_manifest([row("lf", "lf_sale")], SELECTED_SET)
    fetcher = Fetcher(FakeTransport(clock, page("lf_sale")), FetchConfig(), clock)
    outcomes = collect(entries, fetcher)
    target = {
        "status": "ACTIVE",
        "model": "A14-11M-X3BP",
        "screen_size_in": "14",
        "cpu_series": "Snapdragon X",
        "ram_capacity_gb": "16",
        "storage_capacity_gb": "512",
    }
    matcher = ExactMatcher(
        [target | {"target_id": "TGT-000001"}, target | {"target_id": "TGT-000002"}]
    )
    context = MatchingContext.create(
        target_master_sha256="a" * 64,
        target_master_source="synthetic",
        configuration_targets=2,
        listing_only_targets=0,
        matching_rules_version="test",
        matching_rules_sha256="b" * 64,
    )
    with ObservationStore.open(tmp_path / "s.sqlite3") as store:
        store.import_observations(
            [o for _, _, o in collected_observations(outcomes)],
            source_label="t",
            imported_at=clock.now(),
        )
        _, [match] = match_collected(
            store, context, matcher, collected_observations(outcomes), now=clock.now()
        )
    record = result_row(match, SELECTED_SET, context)
    assert (record["match_status"], record["candidate_target_ids"]) == (
        "AMBIGUOUS",
        "TGT-000001;TGT-000002",
    )
    assert review_reasons(record) == ["MATCH_AMBIGUOUS"]


# Failures and partial success


def test_failures_are_reported_and_never_become_prices(tmp_path: Path, capsys) -> None:
    rows = [row("ok", "lf_sale"), row("missing", "dbz_sale"), row("slow", "pcx_regular")]
    timeout = TransportError("TIMEOUT", "The request timed out.")
    code, transport, clock = run(
        tmp_path,
        rows,
        page("lf_sale"),
        TransportResponse(404, FIXTURE_PAGES["dbz_sale"][2], b"", HTML),
        timeout,
        timeout,
        timeout,
    )

    assert code == 2  # partial success
    run_dir = only_run_dir(tmp_path)
    requests = {r["request_id"]: r for r in read_dicts(run_dir / "request_report.csv")}
    assert [requests[k]["request_status"] for k in ("ok", "missing", "slow")] == [
        "SUCCEEDED",
        "FAILED",
        "FAILED",
    ]
    failures = {r["request_id"]: r for r in read_dicts(run_dir / "request_failures.csv")}
    assert (failures["missing"]["stage"], failures["missing"]["error_code"]) == (
        "fetch",
        "HTTP_STATUS",
    )
    assert failures["missing"]["http_status"] == "404"
    assert (failures["slow"]["error_code"], failures["slow"]["attempts"]) == ("TIMEOUT", "3")
    assert failures["slow"]["redacted_url"].startswith("https://pcx.com.ph/products/")
    # Only the real observation exists: no zero price, stock status, or retrieval is invented.
    observations = read_observations_csv(run_dir / "observations.csv")
    assert [item.observation.retailer_id for item in observations] == ["laptop_factory"]
    assert len(read_dicts(run_dir / "match_results.csv")) == 1
    summary = json.loads((run_dir / "collection_run_summary.json").read_text())
    assert summary["run_status"] == "PARTIAL_SUCCESS"
    assert summary["failure_codes"] == {"fetch:HTTP_STATUS": 1, "fetch:TIMEOUT": 1}
    assert "PARTIAL_SUCCESS" in capsys.readouterr().out


def test_a_run_without_valid_observations_is_unsuccessful(tmp_path: Path, capsys) -> None:
    code, _, _ = run(
        tmp_path,
        [row("bare", "pcc_single", product_url=FIXTURE_PAGES["pcc_variants"][2].split("?")[0])],
        page("pcc_variants", FIXTURE_PAGES["pcc_variants"][2].split("?")[0]),
    )

    assert code == 1
    run_dir = only_run_dir(tmp_path)
    [failure] = read_dicts(run_dir / "request_failures.csv")
    # A bare multi-variant URL fails safely; no variant is guessed.
    assert (failure["stage"], failure["error_code"]) == ("parse", "AMBIGUOUS_VARIANT")
    assert read_dicts(run_dir / "observations.csv") == []
    assert read_dicts(run_dir / "match_results.csv") == []
    assert not (tmp_path / "store").exists()
    summary = json.loads((run_dir / "collection_run_summary.json").read_text())
    assert (summary["run_status"], summary["store"]["opened"]) == ("FAILED", False)
    assert "FAILED" in capsys.readouterr().out


# Existing guards


def test_the_live_use_gate_still_blocks_unapproved_adapters(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(LaptopFactoryAdapter, "live_use_approved", False)
    code, transport, _ = run(tmp_path, [row("lf", "lf_sale")], live_transport=True)

    assert code == 1
    assert transport.calls == []
    [failure] = read_dicts(only_run_dir(tmp_path) / "request_failures.csv")
    assert (failure["stage"], failure["error_code"]) == ("request", "LIVE_USE_NOT_APPROVED")


def test_redirects_to_other_hosts_are_not_followed(tmp_path: Path) -> None:
    redirect = TransportResponse(
        301, FIXTURE_PAGES["lf_sale"][2], b"", {"location": "https://evil.example/product/x/"}
    )
    code, transport, _ = run(tmp_path, [row("lf", "lf_sale")], redirect)

    assert code == 1
    assert len(transport.calls) == 1
    [failure] = read_dicts(only_run_dir(tmp_path) / "request_failures.csv")
    assert failure["error_code"] == "REDIRECT_HOST_NOT_ALLOWED"


def test_throttling_is_shared_across_the_run(tmp_path: Path) -> None:
    rows = [row("lf1", "lf_sale"), row("dbz", "dbz_sale"), row("lf2", "lf_regular")]
    code, transport, clock = run(
        tmp_path, rows, page("lf_sale"), page("dbz_sale"), page("lf_regular")
    )

    assert code == 0
    times = [call["at"] for call in transport.calls]
    # A different retailer is not delayed; the second Laptop Factory request waits its interval.
    assert times == [0.0, 0.0, FetchConfig().min_request_interval_seconds]
    assert clock.sleeps == [FetchConfig().min_request_interval_seconds]


# History: re-imports, later retrievals, offline re-matching, changed catalogs


def test_exports_reimport_without_duplicates_and_later_retrievals_are_new(tmp_path) -> None:
    clock = FakeClock()
    code, _, _ = run(tmp_path, [row("lf", "lf_sale")], page("lf_sale"), clock=clock)
    assert code == 0
    first_dir = only_run_dir(tmp_path)
    exported = first_dir / "observations.csv"

    with ObservationStore.open(tmp_path / "store" / "history.sqlite3") as store:
        again = store.import_observations(
            [item.observation for item in read_observations_csv(exported)],
            source_label="re-import",
            imported_at=clock.now(),
        )
        assert (again.new, again.duplicates_in_store) == (0, 1)
        assert store.counts()["observations"] == 1

    # The export passes through the existing offline matcher unchanged.
    out = tmp_path / "offline"
    assert load_match_offers().main(
        ["--target-master", str(tmp_path / "tm.csv"), "--observations", str(exported),
         "--output-dir", str(out)]
    ) == 0
    [offline] = read_dicts(out / "offer_match_results.csv")
    assert (offline["match_status"], offline["target_id"]) == ("EXACT", "TGT-000001")

    clock.elapsed += 3600
    code, _, _ = run(tmp_path, [row("lf", "lf_sale")], page("lf_sale"), clock=clock)
    assert code == 0
    assert len(list((tmp_path / "runs").iterdir())) == 2  # the earlier capture is kept
    assert (first_dir / "observations.csv").read_bytes() == exported.read_bytes()
    with ObservationStore.open(tmp_path / "store" / "history.sqlite3") as store:
        counts = store.counts()
    assert (counts["observations"], counts["offers"]) == (2, 1)


def test_a_changed_catalog_adds_a_context_and_keeps_earlier_results(tmp_path) -> None:
    clock = FakeClock()
    assert run(tmp_path, [row("lf", "lf_sale")], page("lf_sale"), clock=clock)[0] == 0
    changed = [
        *TARGET_ROWS,
        target_row(
            target_id="TGT-000004",
            model="ZZ99-0001",
            screen_size_in="13",
            cpu_series="Core 7",
            ram_capacity_gb="32",
            storage_capacity_gb="2000",
        ),
    ]
    clock.elapsed += 60
    code, _, _ = run(tmp_path, [row("lf", "lf_sale")], page("lf_sale"), clock=clock,
                     targets=changed)
    assert code == 0
    with ObservationStore.open(tmp_path / "store" / "history.sqlite3") as store:
        contexts = store.contexts()
        first = store.match_results(contexts[0]["context_id"])
        second = store.match_results(contexts[1]["context_id"])
    assert len(contexts) == 2
    # The first context keeps its one result; the second matched both stored observations.
    assert (len(first), len(second)) == (1, 2)
    assert {m.target_id for m in second.values()} == {"TGT-000001"}
    assert len(list((tmp_path / "runs").iterdir())) == 2
