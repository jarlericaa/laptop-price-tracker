"""The versioned discovery benchmark (leads_v1.csv, 2026-10-22) and the measurement script.

The benchmark holds the 2026-10-21 audit's exact/review claims with labels taken only from the
tracker's own saved evidence, plus evidence-only cases from the 2026-10-20 run. Audit claims are
never labels: a claim is CODE_IN_URL_UNVERIFIED, FAMILY_TARGET_UNVERIFIABLE, CATEGORY_ONLY, ...
until a fresh page and the matcher decide.
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401

pytestmark = pytest.mark.usefixtures("no_network")
ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "tests" / "scraping_fixtures" / "discovery_benchmark" / "leads_v1.csv"
CASE_TYPES = {"POSITIVE_DIRECT_PAGE", "CATEGORY_ONLY", "REVIEW_CONFLICT", "DUPLICATE_URL_TARGET",
              "SOLD_OUT_PRICED", "PREORDER", "CPU_MODEL_CONFLICT", "RAM_STORAGE_VARIANT",
              "GPU_OR_SUFFIX", "STALE_PRICE", "FETCH_BUDGET"}


def leads() -> list[dict[str, str]]:
    with BENCHMARK.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_the_benchmark_covers_every_required_case_type() -> None:
    rows = leads()
    assert {r["case_type"] for r in rows} == CASE_TYPES
    assert len({r["lead_id"] for r in rows}) == len(rows)
    assert all(r["target_id"].startswith("TGT-") and r["retailer_id"] for r in rows)


def test_audit_claims_are_never_taken_as_verified_labels() -> None:
    for row in leads():
        if row["url_kind"] == "CATEGORY_PAGE":
            assert row["independent_label"] == "CATEGORY_ONLY"
            assert row["expected_safe_outcome"] == "NOT_A_PRODUCT_PAGE"
        if row["independent_label"].startswith("VERIFIED_EXACT_BY_SAVED_PAGE"):
            assert "observation" in row["label_evidence"] or "OUT_OF_STOCK" in row[
                "label_evidence"]
        if row["identity_kind"] == "FAMILY_NAME" and row["audit_status"]:
            assert not row["independent_label"].startswith("VERIFIED_EXACT")
        if "DUPLICATE_URL_ACROSS_TARGETS" in row["independent_label"]:
            assert row["expected_safe_outcome"] == "MUST_NOT_REUSE_PRICE_ACROSS_TARGETS"


def test_the_three_category_only_audit_urls_stay_category_only() -> None:
    category = [r for r in leads() if r["audit_status"] == "VERIFIED_EXACT_LISTING"
                and r["url_kind"] == "CATEGORY_PAGE"]
    assert len(category) == 3


def test_the_measurement_script_runs_offline_on_minimal_inputs(tmp_path) -> None:
    disc = tmp_path / "discovery"
    (disc / "collection").mkdir(parents=True)
    report = tmp_path / "report"
    report.mkdir()
    (disc / "discovery_summary.json").write_text(json.dumps(
        {"started_at_utc": "2026-10-20T07:00:00Z", "finished_at_utc": "2026-10-20T08:00:00Z",
         "requests_sent": 3}))
    for path, header in ((disc / "search_results.csv", "target_id,retailer_id,product_url"),
                         (disc / "candidates.csv", "target_id,retailer_id,product_url"),
                         (disc / "search_jobs.csv", "target_id,retailer_id,discovery_outcome"),
                         (disc / "search_requests.csv", "target_id,retailer_id,tier,outcome"),
                         (disc / "collection" / "match_results.csv",
                          "offer_url,match_status,target_id,price"),
                         (report / "weekly_price_comparison.csv", "target_id")):
        path.write_text(header + "\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "measure", ROOT / "scripts" / "measure_discovery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "metrics.json"
    assert module.main(["--benchmark", str(BENCHMARK), "--report-dir", str(report),
                        "--discovery-dir", str(disc), "--output", str(out)]) == 0
    metrics = json.loads(out.read_text())
    assert metrics["baseline"]["runtime_seconds"] == 3600.0
    recall = metrics["baseline"]["known_positive_discovery_recall"]["TOTAL"]
    assert recall["found_by_discovery"] == 0 and recall["leads"] > 0
