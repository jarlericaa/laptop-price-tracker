"""Before/after report: unrecorded is never zero, recovery statuses, and the output location.

Synthetic run folders only; no retailer, Sheet or history store is touched.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401

from laptop_tracker.before_after import (
    IDENTITY_CONFIRMED_EXCLUDED,
    JOB_NOT_RUN,
    NOT_RECORDED,
    PAGE_UNRESOLVED,
    RECOVERED_OFFER,
    STILL_MISSED,
    build_report,
    load_run,
    render_markdown,
)

pytestmark = pytest.mark.usefixtures("no_network")
ROOT = Path(__file__).resolve().parents[1]


def write_run(
    folder: Path,
    *,
    summary: dict | None = None,
    coverage: dict | None = None,
    reuse: dict | None = None,
    jobs: list[dict[str, str]] | None = None,
) -> Path:
    if summary is not None:
        (folder / "shopee_variations").mkdir(parents=True)
        (folder / "shopee_variations" / "shopee_variation_run_summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )
    if coverage is not None:
        (folder / "variation_reports").mkdir(parents=True)
        (folder / "variation_reports" / "variation_coverage.json").write_text(
            json.dumps({"counts": coverage}), encoding="utf-8"
        )
    if reuse is not None or jobs is not None:
        (folder / "discovery").mkdir(parents=True)
    if reuse is not None:
        (folder / "discovery" / "discovery_summary.json").write_text(
            json.dumps({"request_reuse": reuse}), encoding="utf-8"
        )
    if jobs is not None:
        columns = sorted({key for job in jobs for key in job})
        with (folder / "discovery" / "search_jobs.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(jobs)
    return folder


def job(target: str, retailer: str, outcome: str, klass: str = "") -> dict[str, str]:
    row = {"target_id": target, "retailer_id": retailer, "terminal_outcome": outcome}
    if klass:
        row["outcome_class"] = klass
    return row


def old_job(target: str, outcome: str) -> dict[str, str]:
    """A row of a run before the 2026-11-03 terminal vocabulary: only discovery_outcome."""
    return {"target_id": target, "retailer_id": "pcc", "discovery_outcome": outcome}


def metric(rows: list[dict], name: str) -> dict:
    return next(r for r in rows if r["metric"] == name)


def test_a_figure_the_earlier_run_never_recorded_is_not_zero(tmp_path) -> None:
    before = write_run(
        tmp_path / "b", summary={"selected_variations": 53, "eligible_variations": 187}
    )
    after = write_run(
        tmp_path / "a",
        summary={"selected_variations": 100, "eligible_union": 1571, "eligible_variations": 1571},
    )
    report = build_report(load_run("b", before), load_run("a", after))
    union = metric(report["selection_universe"], "eligible_union")
    assert (union["before"], union["after"], union["change"]) == (NOT_RECORDED, 1571, NOT_RECORDED)
    selected = metric(report["selection"], "selected_variations")
    assert (selected["before"], selected["after"], selected["change"]) == (53, 100, 47)


def test_matching_and_alternative_coverage_are_separate_sections(tmp_path) -> None:
    counts = {
        "matching_priced_variation_rows": 10,
        "alternative_priced_variation_rows": 4,
        "alt_1_offers": 3,
    }
    run = write_run(tmp_path / "r", coverage=counts)
    report = build_report(load_run("r", run), load_run("r", run))
    matching = {r["metric"] for r in report["matching_coverage"]}
    alternative = {r["metric"] for r in report["alternative_coverage"]}
    assert "matching_priced_variation_rows" in matching.difference(alternative)
    assert "alt_1_offers" in alternative.difference(matching)
    assert metric(report["alternative_coverage"], "alt_1_offers")["after"] == 3


def test_exclusion_reasons_are_zero_only_when_the_run_recorded_the_mapping(tmp_path) -> None:
    before = write_run(tmp_path / "b", summary={"selected_variations": 1})
    after = write_run(
        tmp_path / "a", summary={"excluded_by_mapping_failure": {"UNMAPPED": 1366, "X": 5}}
    )
    report = build_report(load_run("b", before), load_run("a", after))
    assert metric(report["exclusions_by_reason"], "UNMAPPED")["before"] == NOT_RECORDED
    both = write_run(
        tmp_path / "c", summary={"excluded_by_mapping_failure": {"UNMAPPED": 1}}
    )
    report = build_report(load_run("c", both), load_run("a", after))
    assert metric(report["exclusions_by_reason"], "X")["before"] == 0


def test_recovery_statuses_keep_the_distinct_terminal_states(tmp_path) -> None:
    before = write_run(
        tmp_path / "b",
        jobs=[
            old_job("TGT-000293", "QUERY_PLAN_EXHAUSTED"),
            old_job("TGT-000095", "SUITABLE_OFFER_FOUND"),
        ],
    )
    after = write_run(
        tmp_path / "a",
        jobs=[
            job("TGT-000293", "pcc", "SUITABLE_OFFER_FOUND"),
            job("TGT-000095", "pcc", "PAGE_FETCHED_IDENTITY_UNRESOLVED"),
            job("TGT-000003", "pcx", "SUITABLE_OFFER_EXCLUDED_BY_STOCK"),
            job("TGT-000002", "pcx", "FETCH_DEFERRED"),
            job("TGT-000001", "datablitz", "NO_RESULTS_RETURNED"),
            job("TGT-000002", "pcc", "SUITABLE_OFFER_FOUND"),  # another retailer: never a pcx row
        ],
    )
    recovery = build_report(load_run("b", before), load_run("a", after))["discovery_recovery"]
    by_target = {r["target_id"]: r for r in recovery["rows"]}
    assert by_target["TGT-000293"]["before"]["status"] == STILL_MISSED  # QUERY_PLAN_EXHAUSTED
    assert by_target["TGT-000293"]["after"]["status"] == RECOVERED_OFFER
    assert by_target["TGT-000095"]["before"]["status"] == RECOVERED_OFFER
    assert by_target["TGT-000095"]["after"]["status"] == PAGE_UNRESOLVED
    assert by_target["TGT-000003"]["after"]["status"] == IDENTITY_CONFIRMED_EXCLUDED
    assert by_target["TGT-000002"]["after"]["terminal_outcome"] == "FETCH_DEFERRED"
    assert by_target["TGT-000001"]["after"]["status"] == STILL_MISSED
    assert by_target["TGT-000006"]["after"]["status"] == JOB_NOT_RUN
    # Only a confirmed offer counts as recovered; an unresolved page and a deferred fetch do not.
    assert recovery["recovered_with_offer"] == 1
    assert recovery["unresolved_pages"] == 1
    assert recovery["fetch_deferred_or_failed"] == 1
    assert recovery["no_job_recorded"] == 1


def test_a_run_without_discovery_files_reports_not_recorded_for_recovery(tmp_path) -> None:
    empty = write_run(tmp_path / "e", summary={"selected_variations": 1})
    report = build_report(load_run("e", empty), load_run("e", empty))
    assert report["discovery_recovery"]["rows"][0]["after"]["status"] == NOT_RECORDED
    assert report["unresolved_pages"]["after"] == NOT_RECORDED
    assert "search_jobs.csv" in report["missing_artifacts"]["after"]


def test_unresolved_pages_are_counted_over_the_whole_run(tmp_path) -> None:
    after = write_run(
        tmp_path / "a",
        jobs=[
            job("TGT-1", "pcc", "PAGE_FETCHED_IDENTITY_UNRESOLVED"),
            job("TGT-2", "pcx", "PAGE_FETCHED_IDENTITY_UNRESOLVED"),
            job("TGT-3", "pcx", "NO_RESULTS_RETURNED"),
        ],
    )
    old = write_run(
        tmp_path / "b",
        jobs=[old_job("TGT-1", "QUERY_PLAN_EXHAUSTED")],
    )
    report = build_report(load_run("b", old), load_run("a", after))
    pages = report["unresolved_pages"]
    assert (pages["before"], pages["after"], pages["change"]) == (NOT_RECORDED, 2, NOT_RECORDED)


def test_requests_avoided_come_from_the_runs_own_reuse_counters(tmp_path) -> None:
    before = write_run(tmp_path / "b", reuse={"requests_avoided_total": 0, "searches_sent": 311})
    after = write_run(tmp_path / "a", reuse={"requests_avoided_total": 348, "searches_sent": 200})
    report = build_report(load_run("b", before), load_run("a", after))
    avoided = metric(report["requests_avoided_by_reuse"], "requests_avoided_total")
    assert (avoided["before"], avoided["after"], avoided["change"]) == (0, 348, 348)
    assert metric(report["requests_avoided_by_reuse"], "searches_reused_saved_cache")["after"] == (
        NOT_RECORDED
    )


def test_markdown_states_unrecorded_figures_and_the_recovery_rows(tmp_path) -> None:
    run = write_run(tmp_path / "r", summary={"selected_variations": 100},
                    jobs=[job("TGT-000293", "pcc", "SUITABLE_OFFER_FOUND")])
    text = render_markdown(build_report(load_run("r", run), load_run("r", run)))
    assert "`NOT_RECORDED` means the run never wrote the figure" in text
    assert "TGT-000293 PC Central ASUS X1504VA-BQ4301WSM" in text and RECOVERED_OFFER in text


def test_the_cli_writes_only_under_data_output_and_never_into_config(tmp_path, capsys) -> None:
    sys.path.insert(0, str(ROOT))
    import build_before_after_report as cli

    run = write_run(tmp_path / "r", summary={"selected_variations": 1})
    assert cli.main(["--before-dir", str(run), "--after-dir", str(run),
                     "--output-dir", str(tmp_path / "elsewhere")]) == 2
    assert cli.main(["--before-dir", str(run), "--after-dir", str(run),
                     "--output-dir", str(ROOT / "config" / "x")]) == 2
    assert cli.main(["--before-dir", str(run), "--after-dir", str(tmp_path / "missing")]) == 2
    assert cli.main(["--before-dir", str(run), "--after-dir", str(run), "--dry-run"]) == 0
    assert "Before / after" in capsys.readouterr().out
    assert not (ROOT / "config" / "x").exists()
