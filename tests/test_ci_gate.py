"""Publication readiness gate: decided from a run folder's own metadata, never console text."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from laptop_tracker import run_gate
from laptop_tracker.run_gate import EXPECTED_STAGES

NOW = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["x"])
        writer.writeheader()
        writer.writerows(rows)


def make_run(
    tmp_path: Path,
    *,
    discovery_code=0,
    run_status="SUCCESS",
    matching=30,
    selected=100,
    preview_status="PREVIEW_ONLY",
    name="20261110T010000Z",
    failures=False,
) -> Path:
    run = tmp_path / name
    run.mkdir(parents=True)
    stages = []
    minute = 0
    for stage, accepted in EXPECTED_STAGES:
        code = discovery_code if stage == "discovery" else accepted[0]
        stages.append(
            {
                "stage": stage,
                "started_at": f"2026-11-10T01:{minute:02d}:00Z",
                "finished_at": f"2026-11-10T01:{minute + 1:02d}:00Z",
                "exit_code": code,
            }
        )
        minute += 1
    (run / "stages.jsonl").write_text("\n".join(json.dumps(s) for s in stages) + "\n")
    (run / "status.json").write_text(
        json.dumps({"status": "READY_TO_PUBLISH", "finished_at": "2026-11-10T01:20:00Z"})
    )
    for folder in run_gate.REQUIRED_DIRS:
        (run / folder).mkdir()
        (run / folder / "f.csv").write_text("a\n1\n")
    (run / "discovery").mkdir()
    (run / "discovery/discovery_summary.json").write_text(
        json.dumps({"run_status": run_status, "requests_sent": 841})
    )
    if failures:
        write_csv(
            run / "discovery/search_requests.csv",
            [
                {"retailer_id": "pcx", "outcome": "RESULTS", "http_status": "200", "message": ""},
                {
                    "retailer_id": "pcx",
                    "outcome": "SEARCH_ERROR",
                    "http_status": "429",
                    "message": "",
                },
                {
                    "retailer_id": "pcx",
                    "outcome": "NOT_SENT_RETAILER_FAILING",
                    "http_status": "",
                    "message": "",
                },
                {
                    "retailer_id": "oct",
                    "outcome": "SEARCH_ERROR",
                    "http_status": "",
                    "message": "HTTP 429",
                },
                {
                    "retailer_id": "datablitz",
                    "outcome": "SEARCH_ERROR",
                    "http_status": "503",
                    "message": "",
                },
            ],
        )
        write_csv(
            run / "discovery/product_inspections.csv",
            [
                {
                    "retailer_id": "pcc",
                    "outcome": "FETCH_FAILED",
                    "message": "429 Too Many Requests",
                },
                {"retailer_id": "pcc", "outcome": "OK", "message": ""},
            ],
        )
    else:
        write_csv(
            run / "discovery/search_requests.csv",
            [
                {"retailer_id": r, "outcome": "RESULTS", "http_status": "200", "message": ""}
                for r in ("pcx", "oct", "datablitz", "pcc", "laptop_factory")
            ],
        )
        write_csv(
            run / "discovery/product_inspections.csv",
            [{"retailer_id": "pcc", "outcome": "OK", "message": ""}],
        )
    if failures:
        events = run / "discovery/collection_rounds/round_1"
        write_csv(
            events / "rate_limit_events.csv", [{"retailer_id": "pcc"}, {"retailer_id": "oct"}]
        )
    preview = run / "publications/20261110T012100000000Z_preview"
    preview.mkdir(parents=True)
    (preview / "publication_report.json").write_text(
        json.dumps(
            {
                "status": preview_status,
                "publication_id": "PUB-abc",
                "published_content_sha256": "f" * 64,
                "report_cutoff_utc": "2026-11-10T01:19:00Z",
                "counts": {
                    "selected_variations": selected,
                    "distinct_configuration_contexts": 58,
                    "matching_priced_variation_rows": matching,
                    "variation_rows_with_any_retailer_price": 45,
                },
                "selection_scope": {"shortfall": 0},
            }
        )
    )
    return run


def evaluate(run: Path, **kw):
    return run_gate.evaluate(run, now=NOW, **kw)


def test_a_complete_run_passes_and_reports_the_monitoring_metrics(tmp_path) -> None:
    result = evaluate(make_run(tmp_path, failures=True), accept_retailer_failures=True)
    assert result.ok, result.blocking
    m = result.metrics
    assert m["publication_id"] == "PUB-abc" and m["verified_targets"] == 100
    assert m["comparable_price_coverage"]["matching_rows"] == 30
    assert m["duration_minutes"] == 20.0 and m["pipeline_status"] == "READY_TO_PUBLISH"
    assert m["retailers"]["pcx"] | {} == {
        "searches": 3,
        "search_errors": 1,
        "not_sent_failing": 1,
        "inspections": 0,
        "inspection_failures": 0,
        "http_429": 1,
        "other_http_errors": 0,
    }
    assert m["retailers"]["oct"]["http_429"] == 1  # from rate_limit_events.csv
    assert (
        m["retailers"]["pcc"]["http_429"] == 1 and m["retailers"]["pcc"]["inspection_failures"] == 1
    )
    assert m["retailers"]["datablitz"]["other_http_errors"] == 1
    assert m["http_429_total"] == 3


def test_a_failed_pipeline_status_blocks(tmp_path) -> None:
    run = make_run(tmp_path)
    (run / "status.json").write_text(json.dumps({"status": "FAILED_AT_discovery", "exit_code": 1}))
    assert any("FAILED_AT_discovery" in b for b in evaluate(run).blocking)


def test_a_missing_status_file_means_the_run_did_not_finish(tmp_path) -> None:
    run = make_run(tmp_path)
    (run / "status.json").unlink()
    assert any("did not finish" in b for b in evaluate(run).blocking)


def test_a_stage_with_an_unaccepted_exit_code_blocks_even_if_status_says_ready(tmp_path) -> None:
    run = make_run(tmp_path)
    lines = [json.loads(line) for line in (run / "stages.jsonl").read_text().splitlines()]
    lines[3]["exit_code"] = 1
    (run / "stages.jsonl").write_text("\n".join(json.dumps(s) for s in lines))
    assert any("variation_audit exited with 1" in b for b in evaluate(run).blocking)


def test_a_missing_stage_blocks(tmp_path) -> None:
    run = make_run(tmp_path)
    lines = (run / "stages.jsonl").read_text().splitlines()
    (run / "stages.jsonl").write_text("\n".join(lines[:-1]))
    assert any("publish_preview did not run" in b for b in evaluate(run).blocking)


def test_the_normal_partial_success_of_a_bounded_search_does_not_block(tmp_path) -> None:
    # Exit 2 / PARTIAL_SUCCESS was the result of every real run on record, including the
    # published 2026-11-05 run; the driver accepts it, so the gate does too.
    result = evaluate(make_run(tmp_path, discovery_code=2, run_status="PARTIAL_SUCCESS"))
    assert result.ok, result.blocking
    assert not result.metrics["retailer_failures"]


def test_retailer_request_failures_block_until_a_person_accepts_them(tmp_path) -> None:
    run = make_run(tmp_path, discovery_code=2, run_status="PARTIAL_SUCCESS", failures=True)
    blocked = evaluate(run)
    assert not blocked.ok
    assert any("pcx: searching stopped after repeated errors" in b for b in blocked.blocking)
    assert any("failed across all retailers" in b for b in blocked.blocking)
    # The plain-language list of what failed stays in the metrics.
    assert any(
        "pcx: 1 search error(s), 1 search(es) not sent" in f
        for f in blocked.metrics["retailer_failures"]
    )
    assert any(
        "pcc: 1 page fetch(es) failed (1 HTTP 429)" in f
        for f in blocked.metrics["retailer_failures"]
    )
    accepted = evaluate(run, accept_retailer_failures=True)
    assert accepted.ok and any("Accepted explicitly" in w for w in accepted.warnings)
    assert accepted.metrics["retailer_failure_policy"]["manual_acceptance"]["overrode_block"]


def test_provider_failures_in_the_summary_without_a_failed_request_block(tmp_path) -> None:
    run = make_run(tmp_path)
    (run / "discovery/discovery_summary.json").write_text(
        json.dumps(
            {
                "run_status": "PARTIAL_SUCCESS",
                "discovery_outcomes": {"PROVIDER_FAILURE": 30},
                "search_stop_reasons": {"RETAILER_STOPPED_AFTER_ERRORS": 30},
            }
        )
    )
    result = evaluate(run)
    # The request files show no failed or unsent search behind these counters: the files disagree,
    # and a person's acceptance does not repair that.
    assert not result.ok and any("PROVIDER_FAILURE" in b for b in result.blocking)
    assert not evaluate(run, accept_retailer_failures=True).ok


def test_a_failed_discovery_status_always_blocks(tmp_path) -> None:
    run = make_run(tmp_path, run_status="FAILED")
    assert not evaluate(run, accept_retailer_failures=True).ok


@pytest.mark.parametrize("status", ["DESTINATION_REFUSED", "ACCESS_FAILED", ""])
def test_a_preview_that_is_not_preview_only_blocks(tmp_path, status) -> None:
    run = make_run(tmp_path, preview_status=status)
    assert any("preview status" in b for b in evaluate(run).blocking)


def test_a_missing_preview_or_report_folder_blocks(tmp_path) -> None:
    run = make_run(tmp_path)
    for report in run.glob("publications/*/publication_report.json"):
        report.unlink()
    assert any("preview report is missing" in b for b in evaluate(run).blocking)
    run2 = make_run(tmp_path, name="20261110T020000Z")
    (run2 / "variation_reports/f.csv").unlink()
    assert any("variation_reports" in b for b in evaluate(run2).blocking)


def test_a_missing_discovery_summary_blocks(tmp_path) -> None:
    run = make_run(tmp_path)
    (run / "discovery/discovery_summary.json").unlink()
    assert any("discovery_summary" in b for b in evaluate(run).blocking)


def test_a_superseded_run_is_not_published(tmp_path) -> None:
    run = make_run(tmp_path)
    late = run_gate.evaluate(run, now=NOW + timedelta(days=9))
    assert any("superseded" in b for b in late.blocking)
    assert run_gate.evaluate(run, now=NOW + timedelta(days=9), max_age_hours=24 * 14).ok


def test_the_optional_coverage_drop_guard_is_off_unless_configured(tmp_path) -> None:
    run = make_run(tmp_path, matching=30)
    previous = {"matching_priced_variation_rows": 41}
    assert evaluate(run, previous_counts=previous).ok  # no threshold: no invented rule
    guarded = evaluate(run, previous_counts=previous, max_matching_drop_pct=20)
    assert any("fell from 41 to 30" in b for b in guarded.blocking)
    assert evaluate(run, previous_counts=previous, max_matching_drop_pct=30).ok
    assert evaluate(run, previous_counts=None, max_matching_drop_pct=20).ok  # nothing to compare


def test_the_summary_is_markdown_without_secret_looking_content(tmp_path) -> None:
    result = evaluate(make_run(tmp_path, failures=True))
    text = run_gate.render_summary("Collection result", result, {"Extra": "value"})
    assert "PUB-abc" in text and "| pcx |" in text and "BLOCKED" in text and "**Extra:**" in text
    assert "Spreadsheet" not in text and "token" not in text.lower()
