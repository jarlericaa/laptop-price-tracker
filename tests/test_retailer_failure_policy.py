# ruff: noqa: F811  (the ``world`` fixture is imported, then named as a test parameter)
"""Retailer fetching failures are an availability problem, never a reason to withhold the report.

2026-11-22: one failed Octagon page (run 20261122T071112Z) and Laptop Factory answering HTTP 500 and
being stopped (run 20261122T084002Z) each left a correct, saved, previewed report unpublished. These
tests pin what is now tolerated (every retailer failure, up to all five retailers, as a warning),
what still blocks (any pipeline-integrity problem), and that a failure is never presented as a
price, a zero, a confirmed absence or a fresh older price.

The request rows are synthetic: they follow the shape of ``search_requests.csv``,
``product_inspections.csv`` and ``discovery_summary.json``. The two saved bundles are encrypted with
a key held only by the workflow, so the real files are not used (see docs/GITHUB_ACTIONS_SETUP.md).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from laptop_tracker import release_store as rs
from laptop_tracker import retailer_coverage as rc
from laptop_tracker import retailer_failure_policy as rfp
from laptop_tracker import run_gate
from laptop_tracker import state_crypto as sc
from laptop_tracker import state_store as ss
from laptop_tracker.retailer_failure_policy import FailurePolicy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_ci_gate import NOW, make_run, write_csv  # noqa: E402
from test_variation_publication import NOW as PUBLISHER_NOW  # noqa: E402
from test_variation_publication import world  # noqa: E402, F401  (fixture)

import ci_publish as publish_script  # noqa: E402
import ci_run_gate  # noqa: E402

RETAILERS = ("datablitz", "laptop_factory", "pcx", "pcc", "oct")
ALL = frozenset(RETAILERS)


def srow(retailer, outcome="RESULTS", target="TGT-000001", http="", cache="", message=""):
    return {
        "target_id": target,
        "retailer_id": retailer,
        "outcome": outcome,
        "http_status": http,
        "message": message,
        "cache": cache,
    }


def irow(retailer, outcome="OK", target="TGT-000001", cache="", http="", message=""):
    return {
        "target_id": target,
        "retailer_id": retailer,
        "outcome": outcome,
        "message": message,
        "cache": cache,
        "http_status": http,
    }


def tgt(n: int) -> str:
    return f"TGT-{n:06d}"


def healthy_searches(skip=frozenset()) -> list[dict]:
    return [  # 600 attempts over five retailers
        srow(r, target=tgt(i)) for r in RETAILERS if r not in skip for i in range(1, 121)
    ]


def healthy_inspections(skip=frozenset()) -> list[dict]:
    return [
        irow(r, target=tgt(i))
        for r in RETAILERS
        if r != "laptop_factory" and r not in skip
        for i in range(1, 21)
    ]


def build(
    tmp_path,
    *,
    searches=(),
    inspections=(),
    replace_searches=None,
    replace_inspections=None,
    jobs=None,
    summary=None,
    name="20261110T010000Z",
    with_target=True,
    job_outcomes=True,
) -> Path:
    """A run folder over a healthy 680-request collection plus the given rows. ``jobs`` maps
    (target, retailer) to a terminal outcome; every other pair ended as ``NO_RESULTS_RETURNED``."""
    run = make_run(tmp_path, name=name, discovery_code=2, run_status="PARTIAL_SUCCESS")
    search_rows = (
        replace_searches if replace_searches is not None else [*healthy_searches(), *searches]
    )
    inspection_rows = (
        replace_inspections
        if replace_inspections is not None
        else [*healthy_inspections(), *inspections]
    )
    if not with_target:
        search_rows = [{k: v for k, v in r.items() if k != "target_id"} for r in search_rows]
        inspection_rows = [
            {k: v for k, v in r.items() if k != "target_id"} for r in inspection_rows
        ]
    write_csv(run / "discovery/search_requests.csv", search_rows)
    if inspection_rows:
        write_csv(run / "discovery/product_inspections.csv", inspection_rows)
    else:  # a run in which nothing was inspected still writes its header
        (run / "discovery/product_inspections.csv").write_text("retailer_id,outcome,cache\n")
    write_csv(run / "discovery/search_jobs.csv", [{"target_id": tgt(1)}])
    pairs = set(jobs or {})
    if with_target:
        pairs |= {(r["target_id"], r["retailer_id"]) for r in [*search_rows, *inspection_rows]}
    body = {
        "run_status": "PARTIAL_SUCCESS",
        "mode": "LIVE",
        "requests_sent": 770,
        "scope": {"retailers": list(RETAILERS)},
        "search_outcomes": dict(Counter(f"{r['retailer_id']}:{r['outcome']}" for r in search_rows)),
        **(summary or {}),
    }
    if job_outcomes:
        body["job_outcomes"] = [
            {
                "target_id": t,
                "retailer_id": r,
                "terminal_outcome": (jobs or {}).get((t, r), "NO_RESULTS_RETURNED"),
                "discovery_outcome": (jobs or {}).get((t, r), "QUERY_PLAN_EXHAUSTED"),
            }
            for t, r in sorted(pairs)
        ]
    (run / "discovery/discovery_summary.json").write_text(json.dumps(body))
    return run


def outage(tmp_path, down, *, searches=120, name="20261110T010000Z", **kw) -> Path:
    """The named retailers answer HTTP 500 to two searches and are then stopped: everything else
    planned for them is never sent (the shape of the Laptop Factory run of 2026-11-22)."""
    rows = healthy_searches(skip=down)
    jobs = {}
    for retailer in sorted(down):
        rows += [srow(retailer, "SEARCH_ERROR", tgt(i), http="500", message="HTTP_STATUS: 500")
                 for i in (1, 2)]  # fmt: skip
        rows += [
            srow(retailer, "NOT_SENT_RETAILER_FAILING", tgt(i)) for i in range(3, searches + 1)
        ]
        jobs |= {(tgt(i), retailer): "PROVIDER_FAILURE" for i in range(1, searches + 1)}
    summary = {
        "search_stop_reasons": {"RETAILER_STOPPED_AFTER_ERRORS": (searches - 2) * len(down)},
        "discovery_outcomes": {"PROVIDER_FAILURE": searches * len(down)},
    } | kw.pop("summary", {})
    return build(
        tmp_path,
        replace_searches=rows,
        replace_inspections=healthy_inspections(skip=down),
        jobs=jobs | kw.pop("jobs", {}),
        summary=summary,
        name=name,
        **kw,
    )


def write_collection(run: Path, rounds, *, status="COLLECTION_FAILED", rounds_run=None) -> None:
    """The merged collection folder of a discovery run whose collection stored nothing."""
    folder = run / "discovery/collection"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "collection_run_summary.json").write_text(json.dumps({"rounds": rounds}))
    (folder / "match_results.csv").write_text("observation_id,expected_target_id\n")
    summary_path = run / "discovery/discovery_summary.json"
    summary = json.loads(summary_path.read_text())
    summary |= {"run_status": status, "collection_rounds": rounds_run or len(rounds)}
    summary_path.write_text(json.dumps(summary))


def evaluate(run, **kw):
    return run_gate.evaluate(run, now=NOW, **kw)


def policy_of(result) -> dict:
    return result.metrics["retailer_failure_policy"]


def notice_of(result) -> str:
    return result.metrics["retailer_coverage"]["notice"]


def oct_inspection_failure(n=7):
    return irow("oct", "FETCH_FAILED", tgt(n), message="HTTP_STATUS", http="500")


# --- failures are warnings -------------------------------------------------------------------


def test_all_retailers_succeeding_is_a_plain_pass(tmp_path) -> None:
    extra = [
        srow("oct", cache="SAVED:old"),
        srow("oct", cache="SAME_RUN"),
        srow("oct", "NOT_SENT_QUERY_LIMIT"),
    ]
    result = evaluate(build(tmp_path, searches=extra, inspections=[irow("oct", cache="SAVED:x")]))
    assert result.decision == "PASS" and result.ok and not result.warnings
    requests = policy_of(result)["requests"]
    assert requests["attempted"] == 680 and requests["failed"] == 0  # cached/unsent are no attempt
    assert result.metrics["retailer_coverage"]["complete"] is True
    assert notice_of(result) == ""


def test_one_octagon_page_failure_is_a_warning_and_leaves_coverage_complete(tmp_path) -> None:
    run = build(
        tmp_path, inspections=[oct_inspection_failure()], jobs={(tgt(7), "oct"): "FETCH_FAILED"}
    )
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS" and result.ok and not result.blocking
    assert "oct: 1 page fetch(es) failed" in " ".join(result.warnings)
    p = policy_of(result)
    assert p["severity"] == "minor" and p["requests"]["inspection_failures"] == 1
    assert p["requests"]["failure_classes"] == {"SERVER_ERROR": 1}
    assert p["manual_acceptance"] == {"requested": False, "used": False, "overrode_block": False}
    assert p["affected_jobs"][0]["impact"] == "visible_in_report"
    # One page of 121 Octagon jobs does not make Octagon "incomplete": no notice on the report.
    assert notice_of(result) == ""
    assert {"ok", "blocking", "warnings", "metrics", "decision"} <= set(result.as_dict())


def test_several_product_inspections_failing_are_still_only_a_warning(tmp_path) -> None:
    pages = [oct_inspection_failure(n) for n in range(1, 6)]
    run = build(
        tmp_path,
        inspections=pages,
        jobs={(tgt(n), "oct"): "FETCH_FAILED" for n in range(1, 6)},
    )
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS" and result.ok
    assert policy_of(result)["severity"] == "significant"  # over the warning thresholds
    assert any("limit 3" in w for w in result.warnings)


def test_failures_concentrated_on_one_unpriced_target_are_noted_not_blocking(tmp_path) -> None:
    run = build(
        tmp_path,
        inspections=[irow("oct", "FETCH_FAILED", tgt(10)), irow("oct", "FETCH_FAILED", tgt(10))],
        jobs={(tgt(10), "oct"): "FETCH_FAILED"},
    )
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS"
    ids = {f["id"] for f in policy_of(result)["findings"]}
    assert "concentrated_unpriced" in ids


def test_the_laptop_factory_outage_of_run_20261122t084002z_is_publishable(tmp_path) -> None:
    """Two HTTP 500 searches, then the retailer is stopped: 145 planned searches never sent."""
    run = outage(tmp_path, {"laptop_factory"}, searches=147)
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS" and result.ok and not result.blocking
    requests = policy_of(result)["requests"]
    assert requests["searches_not_sent_retailer_failing"] == 145
    assert requests["search_failures"] == 2 and requests["failure_classes"] == {"SERVER_ERROR": 2}
    lf = policy_of(result)["retailers"]["laptop_factory"]
    assert lf["unavailable_for_the_run"] and lf["stopped_after_repeated_errors"]
    text = " ".join(result.warnings)
    assert "laptop_factory: unavailable" in text and "145 planned search(es) never sent" in text
    assert "2× server error (HTTP 500)" in text
    assert policy_of(result)["severity"] == "significant"
    assert notice_of(result) == "Coverage: 4/5 retailers checked | Laptop Factory incomplete"
    assert result.metrics["retailer_coverage"]["retailers"]["laptop_factory"]["status"] == (
        "UNAVAILABLE"
    )
    # A person is not needed, and the same files give the same answer every time.
    assert evaluate(run).as_dict() == result.as_dict()


def test_a_large_unexecuted_part_of_the_plan_is_a_warning(tmp_path) -> None:
    unsent = [srow("oct", "NOT_SENT_BUDGET_EXHAUSTED", tgt(i)) for i in range(60)]
    run = build(
        tmp_path,
        searches=unsent,
        inspections=[oct_inspection_failure()],
        jobs={(tgt(7), "oct"): "FETCH_FAILED"},
    )
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS"
    assert any("planned searches were never executed" in w for w in result.warnings)
    # Without a failed request the unexecuted searches are not a retailer failure, but the report
    # still says the retailer was not fully checked.
    quiet = evaluate(build(tmp_path, name="20261110T020000Z", searches=unsent))
    assert policy_of(quiet)["requests"]["failed"] == 0
    assert notice_of(quiet) == "Coverage: 4/5 retailers checked | Octagon incomplete"
    assert not any("planned searches were never executed" in w for w in quiet.warnings)


@pytest.mark.parametrize(
    ("down", "notice"),
    [
        ({"laptop_factory"}, "Coverage: 4/5 retailers checked | Laptop Factory incomplete"),
        ({"pcx", "oct"}, "Coverage: 3/5 retailers checked | PC Express, Octagon incomplete"),
        (
            {"datablitz", "pcx", "oct"},
            "Coverage: 2/5 retailers checked | Partial comparison",
        ),
    ],
)
def test_one_to_three_retailers_unavailable_still_publishes_with_a_coverage_notice(
    tmp_path, down, notice
) -> None:
    result = evaluate(outage(tmp_path, down))
    assert result.decision == "PASS_WITH_WARNINGS" and result.ok and not result.blocking
    assert notice_of(result) == notice
    assert f"Report notice: {notice}." in result.warnings


def test_all_five_retailers_unavailable_still_gives_an_explicitly_incomplete_report(
    tmp_path,
) -> None:
    run = outage(tmp_path, ALL)
    write_collection(
        run,
        [{"run_status": "FAILED", "observations_collected": 0, "request_statuses": {"FAILED": 9},
          "store": {"opened": False}}],
    )  # fmt: skip
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS" and result.ok, result.blocking
    assert notice_of(result) == (
        "Coverage: 0/5 retailers checked | Retailer comparisons could not be verified this period"
    )
    assert any("stored no observation because no retailer page could be collected" in w
               for w in result.warnings)  # fmt: skip
    # The report still states its provenance and the rest of the gate is unchanged.
    assert result.metrics["publication_id"] == "PUB-abc"


def test_a_live_search_only_run_with_nothing_to_collect_is_an_outage_not_a_failure(
    tmp_path,
) -> None:
    run = outage(tmp_path, ALL)
    summary_path = run / "discovery/discovery_summary.json"
    summary = json.loads(summary_path.read_text()) | {"run_status": "SEARCH_ONLY_WITH_ERRORS"}
    summary_path.write_text(json.dumps(summary))
    result = evaluate(run)
    assert result.ok and any("no retailer page to collect" in w for w in result.warnings)


def test_a_provider_failure_job_is_visible_never_a_confirmed_absence(tmp_path) -> None:
    run = build(
        tmp_path,
        searches=[srow("pcx", "SEARCH_ERROR", tgt(50), http="503")],
        jobs={(tgt(50), "pcx"): "PROVIDER_FAILURE"},
        summary={"discovery_outcomes": {"PROVIDER_FAILURE": 1}},
    )
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS"
    assert policy_of(result)["affected_jobs"][0]["impact"] == "visible_in_report"
    # A failed search that left a job reading as "searched, nothing found" is flagged for a look.
    silent = build(
        tmp_path,
        name="20261110T020000Z",
        searches=[srow("oct", "SEARCH_ERROR", tgt(11), http="503")],
        jobs={(tgt(11), "oct"): "NO_RESULTS_RETURNED"},
    )
    flagged = evaluate(silent)
    assert flagged.ok and {f["id"] for f in policy_of(flagged)["findings"]} >= {"silent_negative"}


def test_a_failure_is_counted_once_however_many_files_mention_it(tmp_path) -> None:
    run = build(
        tmp_path,
        searches=[srow("pcx", "SEARCH_ERROR", tgt(50), http="429")],
        inspections=[irow("pcc", "FETCH_FAILED", tgt(9))],
        jobs={(tgt(50), "pcx"): "PROVIDER_FAILURE", (tgt(9), "pcc"): "FETCH_FAILED"},
        summary={"discovery_outcomes": {"PROVIDER_FAILURE": 1}},
    )
    write_csv(
        run / "discovery/collection_rounds/round_1/rate_limit_events.csv", [{"retailer_id": "pcx"}]
    )
    result = evaluate(run)
    assert policy_of(result)["requests"]["failed"] == 2  # one search + one page, not four
    assert result.metrics["retailers"]["pcx"]["http_429"] == 2  # existing monitoring, unchanged
    assert result.metrics["retailers"]["pcc"]["other_http_errors"] == 1


@pytest.mark.parametrize(
    ("outcome", "status", "message", "expected"),
    [
        ("SEARCH_ERROR", "500", "HTTP_STATUS: Retailer returned HTTP 500.", "SERVER_ERROR"),
        ("SEARCH_ERROR", "503", "", "SERVER_ERROR"),
        ("SEARCH_ERROR", "429", "RATE_LIMITED: cooldown", "RATE_LIMIT"),
        ("SEARCH_ERROR", "", "RETAILER_DEFERRED: Nothing was sent.", "RATE_LIMIT"),
        ("SEARCH_ERROR", "", "TIMEOUT: The request timed out.", "TIMEOUT"),
        ("SEARCH_ERROR", "", "CONNECTION_ERROR: Connection failed.", "NETWORK_ERROR"),
        ("SEARCH_ERROR", "403", "HTTP_STATUS: HTTP 403", "ACCESS_DENIED"),
        ("SEARCH_ERROR", "404", "HTTP_STATUS: HTTP 404", "NOT_FOUND"),
        ("SEARCH_ERROR", "418", "HTTP_STATUS: HTTP 418", "CLIENT_ERROR"),
        ("SEARCH_ERROR", "", "HTTP 502", "SERVER_ERROR"),
        ("UNSUPPORTED_LAYOUT", "200", "", "UNSUPPORTED_LAYOUT"),
        ("SEARCH_ERROR", "", "REDIRECT_HOST_NOT_ALLOWED: nope", "REQUEST_REFUSED"),
        ("FETCH_FAILED", "", "HTTP_STATUS", "HTTP_ERROR"),
        ("FETCH_FAILED", "500", "HTTP_STATUS", "SERVER_ERROR"),
        ("SEARCH_ERROR", "", "something else", "OTHER_ERROR"),
    ],
)
def test_failures_are_classified_so_a_server_error_is_never_a_missing_product(
    outcome, status, message, expected
) -> None:
    assert rfp.classify_failure(outcome, status, message) == expected


def test_the_diagnostics_separate_search_and_inspection_failures_and_their_causes(tmp_path) -> None:
    run = build(
        tmp_path,
        searches=[
            srow("pcx", "SEARCH_ERROR", tgt(50), http="500", message="HTTP_STATUS: 500"),
            srow("pcx", "SEARCH_ERROR", tgt(51), message="TIMEOUT: timed out"),
        ],
        inspections=[irow("oct", "FETCH_FAILED", tgt(9), message="RATE_LIMITED", http="429")],
        jobs={
            (tgt(50), "pcx"): "PROVIDER_FAILURE",
            (tgt(51), "pcx"): "PROVIDER_FAILURE",
            (tgt(9), "oct"): "FETCH_DEFERRED",
        },
        summary={"discovery_outcomes": {"PROVIDER_FAILURE": 2}},
    )
    p = policy_of(evaluate(run))
    assert p["requests"]["search_failures"] == 2 and p["requests"]["inspection_failures"] == 1
    assert p["retailers"]["pcx"]["failure_classes"] == {"SERVER_ERROR": 1, "TIMEOUT": 1}
    assert p["retailers"]["oct"]["failure_classes"] == {"RATE_LIMIT": 1}
    assert {j["failure_class"] for j in p["affected_jobs"]} == {
        "SERVER_ERROR",
        "TIMEOUT",
        "RATE_LIMIT",
    }


# --- what still blocks: pipeline integrity ----------------------------------------------------


def test_missing_or_inconsistent_discovery_files_block_even_when_a_person_accepts(tmp_path) -> None:
    missing = build(tmp_path)
    (missing / "discovery/search_requests.csv").unlink()
    one = build(tmp_path, name="20261110T020000Z")
    write_csv(one / "discovery/product_inspections.csv", [{"retailer_id": "oct", "x": "1"}])
    disagree = build(
        tmp_path, name="20261110T030000Z", summary={"search_outcomes": {"oct:SEARCH_ERROR": 2}}
    )
    ghosts = build(
        tmp_path, name="20261110T040000Z", summary={"discovery_outcomes": {"PROVIDER_FAILURE": 30}}
    )
    stopped = build(
        tmp_path,
        name="20261110T050000Z",
        summary={"search_stop_reasons": {"RETAILER_STOPPED_AFTER_ERRORS": 30}},
    )
    for run in (missing, one, disagree, ghosts, stopped):
        plain = evaluate(run)
        accepted = evaluate(run, accept_retailer_failures=True)
        assert not plain.ok and not accepted.ok, run.name
        assert policy_of(accepted)["integrity_problems"]
        assert policy_of(accepted)["manual_acceptance"]["used"] is False


def test_a_summary_that_records_no_search_outcomes_cannot_state_coverage(tmp_path) -> None:
    run = build(tmp_path)
    summary_path = run / "discovery/discovery_summary.json"
    summary = json.loads(summary_path.read_text())
    del summary["search_outcomes"]
    summary_path.write_text(json.dumps(summary))
    result = evaluate(run)
    assert not result.ok and any("records no search outcomes" in b for b in result.blocking)


def test_the_discovery_summary_and_its_status_are_still_required(tmp_path) -> None:
    run = build(tmp_path)
    (run / "discovery/discovery_summary.json").unlink()
    assert not evaluate(run, accept_retailer_failures=True).ok
    for n, status in enumerate(("FAILED", "MANIFEST_INVALID", "REPLAY_ONLY", "PREVIEW", "")):
        bad = build(tmp_path, name=f"2026111{n + 1}T010000Z", summary={"run_status": status})
        assert not evaluate(bad, accept_retailer_failures=True).ok, status


def test_a_search_only_status_outside_a_live_run_is_not_an_outage(tmp_path) -> None:
    run = build(tmp_path, summary={"run_status": "SEARCH_ONLY_WITH_ERRORS", "mode": "PREVIEW"})
    assert not evaluate(run).ok


def test_a_collection_that_failed_inside_the_pipeline_still_blocks(tmp_path) -> None:
    store_error = [
        {"run_status": "FAILED", "observations_collected": 3,
         "store": {"opened": False, "error": "ObservationStoreError"}}
    ]  # fmt: skip
    broken = outage(tmp_path, ALL)
    write_collection(broken, store_error)
    result = evaluate(broken, accept_retailer_failures=True)
    assert not result.ok and any("failed inside the pipeline" in b for b in result.blocking)
    nosummary = outage(tmp_path, ALL, name="20261110T020000Z")
    write_collection(nosummary, [])
    assert any("cannot be" in b for b in evaluate(nosummary).blocking)
    claimed = outage(tmp_path, ALL, name="20261110T030000Z")
    write_collection(claimed, [{"run_status": "FAILED"}])
    (claimed / "discovery/collection/match_results.csv").unlink()
    assert any("match_results.csv is missing" in b for b in evaluate(claimed).blocking)


def test_a_discovery_step_that_raised_is_surfaced_not_hidden(tmp_path) -> None:
    run = build(tmp_path, jobs={(tgt(5), "pcx"): "DISCOVERY_STEP_FAILED"})
    result = evaluate(run)
    assert result.ok and result.metrics["discovery_step_failures"] == 1
    assert any("raised an internal error" in w for w in result.warnings)


def break_status(run):
    (run / "status.json").write_text(json.dumps({"status": "FAILED_AT_discovery"}))


def break_stage(run):
    lines = (run / "stages.jsonl").read_text().splitlines()
    (run / "stages.jsonl").write_text("\n".join(lines[:-1]))


def break_preview(run):
    for report in run.glob("publications/*/publication_report.json"):
        data = json.loads(report.read_text())
        report.write_text(json.dumps(data | {"status": "DESTINATION_REFUSED"}))


def drop_preview(run):
    for report in run.glob("publications/*/publication_report.json"):
        report.unlink()


def drop_reports(run):
    (run / "variation_reports/f.csv").unlink()


@pytest.mark.parametrize(
    "damage", [break_status, break_stage, break_preview, drop_preview, drop_reports]
)
def test_an_outage_never_hides_an_integrity_failure(tmp_path, damage) -> None:
    run = outage(tmp_path, {"laptop_factory"})
    assert evaluate(run).ok
    damage(run)
    assert not evaluate(run).ok and not evaluate(run, accept_retailer_failures=True).ok


def test_a_stale_run_is_still_superseded_during_an_outage(tmp_path) -> None:
    run = outage(tmp_path, ALL)
    write_collection(run, [{"run_status": "FAILED", "request_statuses": {"FAILED": 1}}])
    late = run_gate.evaluate(run, now=NOW + timedelta(days=9), accept_retailer_failures=True)
    assert not late.ok and any("superseded" in b for b in late.blocking)


def test_a_configured_coverage_regression_guard_still_blocks(tmp_path) -> None:
    run = outage(tmp_path, {"laptop_factory"})
    previous = {"matching_priced_variation_rows": 60}  # make_run's preview has 30 matching rows
    assert evaluate(run, previous_counts=previous).ok  # the guard is off unless configured
    guarded = evaluate(run, previous_counts=previous, max_matching_drop_pct=20)
    assert not guarded.ok and any("fell from 60 to 30" in b for b in guarded.blocking)


# --- legacy bundles ----------------------------------------------------------------------------


def test_a_bundle_saved_without_request_files_needs_an_explicit_acceptance(tmp_path) -> None:
    run = build(tmp_path)
    for name in ("search_requests.csv", "product_inspections.csv", "search_jobs.csv"):
        (run / "discovery" / name).unlink()
    blocked = evaluate(run)
    assert not blocked.ok and any("saved before request files" in b for b in blocked.blocking)
    accepted = evaluate(run, accept_retailer_failures=True)
    assert accepted.ok and accepted.decision == "PASS_WITH_WARNINGS"
    assert policy_of(accepted)["manual_acceptance"]["overrode_block"] is True
    assert any("Accepted explicitly" in w for w in accepted.warnings)
    # The summary alone still tells the report which retailers were checked.
    assert accepted.metrics["retailer_coverage"]["complete"] is True


def test_a_legacy_outage_bundle_still_carries_its_coverage_notice(tmp_path) -> None:
    run = outage(tmp_path, {"laptop_factory"}, searches=147)
    for name in ("search_requests.csv", "product_inspections.csv", "search_jobs.csv"):
        (run / "discovery" / name).unlink()
    accepted = evaluate(run, accept_retailer_failures=True)
    assert accepted.ok
    assert notice_of(accepted) == "Coverage: 4/5 retailers checked | Laptop Factory incomplete"


# --- configuration -----------------------------------------------------------------------------


def test_defaults_and_valid_overrides() -> None:
    assert FailurePolicy.from_environment({}) == FailurePolicy()
    assert FailurePolicy.from_environment({"RETAILER_FAILURE_MAX_TOTAL": "  "}) == FailurePolicy()
    custom = FailurePolicy.from_environment(
        {
            "RETAILER_FAILURE_MAX_TOTAL": "5",
            "RETAILER_FAILURE_MAX_PER_RETAILER": "0",
            "RETAILER_FAILURE_MAX_RETAILER_PCT": "7.5",
            "RETAILER_FAILURE_MAX_TOTAL_PCT": "0",
            "RETAILER_FAILURE_MAX_UNSENT_PCT": "100",
        }
    )
    assert custom == FailurePolicy(5, 0, 7.5, 0.0, 100.0)
    assert rfp.load_policy({"RETAILER_FAILURE_MAX_TOTAL": "5"}) == (FailurePolicy(max_total=5), [])


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("RETAILER_FAILURE_MAX_TOTAL", "-1"),
        ("RETAILER_FAILURE_MAX_TOTAL", "2.5"),
        ("RETAILER_FAILURE_MAX_TOTAL", "three"),
        ("RETAILER_FAILURE_MAX_TOTAL", "١"),  # a non-ASCII digit
        ("RETAILER_FAILURE_MAX_PER_RETAILER", "1e1"),
        ("RETAILER_FAILURE_MAX_RETAILER_PCT", "101"),
        ("RETAILER_FAILURE_MAX_RETAILER_PCT", "-0.1"),
        ("RETAILER_FAILURE_MAX_TOTAL_PCT", "nan"),
        ("RETAILER_FAILURE_MAX_TOTAL_PCT", "inf"),
        ("RETAILER_FAILURE_MAX_UNSENT_PCT", "5%"),
    ],
)
def test_an_invalid_threshold_is_reported_and_never_stops_the_weekly_publication(
    variable, value
) -> None:
    with pytest.raises(rfp.PolicyConfigError, match=variable):
        FailurePolicy.from_environment({variable: value})
    policy, problems = rfp.load_policy({variable: value})
    assert policy == FailurePolicy() and len(problems) == 1 and variable in problems[0]


def test_percentages_are_compared_exactly() -> None:
    assert not rfp._exceeds(1, 100, 1.0) and rfp._exceeds(2, 100, 1.0)
    assert not rfp._exceeds(0, 0, 0.0) and rfp._exceeds(1, 3, 33.3) and not rfp._exceeds(1, 3, 34)


def test_thresholds_only_grade_the_warning(tmp_path) -> None:
    run = build(
        tmp_path, inspections=[oct_inspection_failure()], jobs={(tgt(7), "oct"): "FETCH_FAILED"}
    )
    assert policy_of(evaluate(run))["severity"] == "minor"
    for policy in (
        FailurePolicy(max_total=0),
        FailurePolicy(max_per_retailer=0),
        FailurePolicy(max_total_pct=0.1),
        FailurePolicy(max_retailer_pct=0.5),
    ):
        strict = evaluate(run, policy=policy)
        assert strict.ok and strict.decision == "PASS_WITH_WARNINGS"
        assert policy_of(strict)["severity"] == "significant"


def test_the_gate_scripts_report_warnings_and_a_bad_threshold_without_stopping(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    run = outage(tmp_path, {"laptop_factory"}, searches=147)
    out = tmp_path / "gate.json"
    argv = ["--run-dir", str(run), "--output", str(out), "--max-age-hours", "100000"]
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RETAILER_FAILURE_MAX_TOTAL", "-4")
    assert ci_run_gate.main(argv) == 0  # a warning is not a failure, and nor is a typo'd threshold
    captured = capsys.readouterr()
    assert json.loads(out.read_text())["decision"] == "PASS_WITH_WARNINGS"
    assert "Publication gate: PASS_WITH_WARNINGS" in captured.out
    assert "::warning title=Retailer request failures::" in captured.out
    assert "RETAILER_FAILURE_MAX_TOTAL" in captured.err  # the typo is shown, not hidden
    assert "**Gate:** PASS_WITH_WARNINGS" in captured.out
    assert "Coverage: 4/5 retailers checked | Laptop Factory incomplete" in captured.out
    assert "TGT-" not in captured.out and "TGT-" not in captured.err  # no target in a public log
    assert "TGT-000001" in out.read_text()  # but the diagnostics file keeps the affected pair


# --- one policy for both jobs ------------------------------------------------------------------


def test_a_stored_run_is_judged_by_the_same_files_policy_and_warnings(tmp_path) -> None:
    run = outage(tmp_path, {"laptop_factory"}, searches=147)
    store = rs.LocalReleaseStore(tmp_path / "releases")
    ring = sc.KeyRing(sc.parse_key(sc.generate_key()))
    ss.save_bundle(store, ring, run, run.name)
    loaded = tmp_path / "publish_job" / run.name
    ss.load_bundle(store, ring, run.name, loaded)
    before, after = evaluate(run), evaluate(loaded)
    assert before.decision == after.decision == "PASS_WITH_WARNINGS"
    assert before.warnings == after.warnings  # nothing disappears between Collect and Publish
    assert policy_of(before)["requests"] == policy_of(after)["requests"]
    assert before.metrics["retailer_coverage"] == after.metrics["retailer_coverage"]


def test_a_publication_with_warnings_goes_ahead_and_records_them(tmp_path, monkeypatch) -> None:
    from test_ci_publish import PUB, FakeSheet, Publisher, good_report

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    store = rs.LocalReleaseStore(tmp_path / "releases")
    ring = sc.KeyRing(sc.parse_key(sc.generate_key()))
    run = outage(tmp_path, {"laptop_factory"}, searches=147)
    publisher = Publisher(tmp_path, report=good_report())
    code = publish_script.main(
        ["--run-dir", str(run), "--max-age-hours", "100000", "--write"],
        store=store,
        ring=ring,
        publisher=publisher,
        open_spreadsheet=lambda read_only: FakeSheet(),
        sleep=lambda s: None,
    )
    assert code == 0 and len(publisher.writes()) == 1
    # The publisher is told which discovery run states the coverage, every time it is invoked.
    assert all("--discovery-dir" in call for call in publisher.calls)
    (attempt,) = ss.read_publication_attempts(store, ring, PUB)
    readiness = attempt["readiness"]
    assert readiness["decision"] == "PASS_WITH_WARNINGS" and readiness["warnings"]
    assert readiness["retailer_coverage"]["notice"].endswith("Laptop Factory incomplete")
    assert readiness["manual_acceptance"]["requested"] is False


def test_a_failed_readback_after_an_outage_run_still_fails_safely(tmp_path, monkeypatch) -> None:
    from test_ci_publish import PUB, FakeSheet, Publisher, good_report

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    store = rs.LocalReleaseStore(tmp_path / "releases")
    ring = sc.KeyRing(sc.parse_key(sc.generate_key()))
    run = outage(tmp_path, {"laptop_factory"})
    publisher = Publisher(
        tmp_path, report=good_report(status="READBACK_FAILED", readback_issues=["a"]), code=1
    )
    code = publish_script.main(
        ["--run-dir", str(run), "--max-age-hours", "100000", "--write"],
        store=store,
        ring=ring,
        publisher=publisher,
        open_spreadsheet=lambda read_only: FakeSheet(),
        sleep=lambda s: None,
    )
    assert code == 1  # warnings never turn a failed write into a success
    (attempt,) = ss.read_publication_attempts(store, ring, PUB)
    assert (
        attempt["status"] != "PUBLISHED"
        and attempt["readiness"]["decision"] == "PASS_WITH_WARNINGS"
    )


def test_an_earlier_unfinished_attempt_still_blocks_a_run_with_warnings(
    tmp_path, monkeypatch
) -> None:
    from test_ci_publish import FakeSheet, Publisher, good_report, row

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    store = rs.LocalReleaseStore(tmp_path / "releases")
    ring = sc.KeyRing(sc.parse_key(sc.generate_key()))
    run = outage(tmp_path, {"laptop_factory"})
    publisher = Publisher(tmp_path, report=good_report())
    code = publish_script.main(
        ["--run-dir", str(run), "--max-age-hours", "100000", "--write"],
        store=store,
        ring=ring,
        publisher=publisher,
        open_spreadsheet=lambda read_only: FakeSheet([row("IN_PROGRESS", "")]),
        sleep=lambda s: None,
    )
    assert code == 1 and not publisher.writes()  # the Sheet may be half written: nothing more


# --- the report never claims what was not verified ---------------------------------------------


def test_an_unverified_retailer_is_never_a_price_a_zero_or_a_confirmed_absence() -> None:
    from test_price_comparison_view import row, shown

    for status in (
        "SEARCH_FAILED",
        "DISCOVERY_INCOMPLETE",
        "FETCH_FAILED",
        "CANDIDATE_NOT_FETCHED",
    ):
        assert shown(row(LF_Price_Status=status), "LF") == "Not checked", status
    # A search cut short by a provider failure is Review, never No match.
    cut = (
        "NO_CANDIDATE_FOUND: 1 search page(s) returned no results; search coverage PROVIDER_FAILURE"
    )
    assert shown(row(LF_Price_Status="NO_CANDIDATE_FOUND", LF_Reason=cut), "LF") == "Review"
    assert shown(row(LF_Price_Status="NO_CANDIDATE_FOUND", LF_Reason=""), "LF") == "No match"


def test_the_lowest_price_and_competitiveness_use_only_verified_prices() -> None:
    from test_price_comparison_view import lowest, priced

    from laptop_tracker import price_comparison_view as pv

    # Laptop Factory was not checked: Shopee is compared with the verified DataBlitz price only.
    r = lowest(
        shopee_price_php="50000.0",
        Lowest_In_Stock_Price="52000.0",
        Lowest_In_Stock_Price_Retailers="DBZ",
        LF_Price_Status="SEARCH_FAILED",
        **priced("DBZ", "52000.0"),
    )
    offer = pv.lowest_in_stock(r)
    assert offer.names == ("DataBlitz",) and offer.price == "52000.0"
    assert pv.display_text(pv.competitiveness_cells(r, offer, lowest_note="")[0]) == "DataBlitz"
    # No verified price at all: nothing is computed, and no zero is shown.
    none = lowest(shopee_price_php="50000.0", LF_Price_Status="SEARCH_FAILED")
    cells = pv.competitiveness_cells(none, pv.lowest_in_stock(none), lowest_note="")
    assert [pv.display_text(c) for c in cells] == ["—", "—", "—", "—", "No comparison"]
    # An unverified price cannot be smuggled in below the report's minimum.
    cheat = lowest(
        Lowest_In_Stock_Price="52000.0",
        Lowest_In_Stock_Price_Retailers="DBZ",
        **priced("DBZ", "52000.0"),
        **priced("LF", "40000.0"),
    )
    assert any("below the minimum" in i for i in pv.lowest_in_stock_issues(cheat))


def test_an_older_price_is_never_presented_as_current(tmp_path) -> None:
    from test_acceptance_fixes import build as track
    from test_acceptance_fixes import stored_offer
    from test_price_tracking import row as report_row

    # Laptop Factory was observed at 500.00 the week before; this week its search failed.
    old = stored_offer(at=datetime(2026, 9, 30, 4, tzinfo=UTC), price="500.00")
    result = track(
        tmp_path,
        [old],
        search_outcomes={("TGT-000001", "LF"): ("SEARCH_FAILED", "SEARCH_ERROR: HTTP_500")},
    )
    cell = report_row(result, "TGT-000001")
    assert cell["LF_Price"] == "" and cell["LF_Price_Status"] == "SEARCH_FAILED"
    assert (
        "last comparable 500.00" in cell["LF_Reason"] and "not carried forward" in cell["LF_Reason"]
    )
    # The older price is a labelled reference only: it never reaches a lowest price.
    assert cell["Lowest_In_Stock_Price"] == "" and cell["Lowest_Price"] == ""


def test_coverage_is_derived_from_the_summary_alone_and_says_nothing_when_unknown() -> None:
    assert rc.from_summary({}) is None and rc.read(Path("/nonexistent")) is None
    complete = rc.from_summary(
        {
            "search_outcomes": {f"{r}:RESULTS": 4 for r in RETAILERS},
            "scope": {"retailers": RETAILERS},
        }
    )
    assert complete.complete and complete.notice() == "" and complete.checked == 5
    # Isolated failures leave a retailer checked; a retailer with nothing recorded is not.
    mixed = rc.from_summary(
        {
            "search_outcomes": {"oct:RESULTS": 50, "oct:SEARCH_ERROR": 1},
            "scope": {"retailers": ["oct", "pcx"]},
        }
    )
    assert [(r.retailer_id, r.status) for r in mixed.retailers] == [
        ("pcx", rc.INCOMPLETE),
        ("oct", rc.CHECKED),
    ]
    assert mixed.notice() == "Coverage: 1/2 retailers checked | PC Express incomplete"
    # A retailer whose jobs were mostly left unverified is incomplete although its searches ran.
    jobs = [
        {"retailer_id": "oct", "target_id": tgt(i), "terminal_outcome": "FETCH_FAILED"}
        for i in range(5)
    ] + [{"retailer_id": "oct", "target_id": tgt(9), "terminal_outcome": "SUITABLE_OFFER_FOUND"}]
    bad = rc.from_summary(
        {
            "search_outcomes": {"oct:RESULTS": 6},
            "job_outcomes": jobs,
            "scope": {"retailers": ["oct"]},
        }
    )
    assert bad.retailers[0].status == rc.INCOMPLETE and bad.notice().startswith("Coverage: 0/1")


# --- the notice on PRICE COMPARISON ------------------------------------------------------------


def discovery_dir(tmp_path: Path, down=frozenset(), name="discovery") -> Path:
    folder = tmp_path / name
    folder.mkdir()
    outcomes = {f"{r}:RESULTS": 10 for r in RETAILERS if r not in down}
    for retailer in down:
        outcomes |= {f"{retailer}:SEARCH_ERROR": 2, f"{retailer}:NOT_SENT_RETAILER_FAILING": 8}
    (folder / "discovery_summary.json").write_text(
        json.dumps({"search_outcomes": outcomes, "scope": {"retailers": list(RETAILERS)}})
    )
    return folder


def test_the_business_tab_shows_coverage_beside_the_week_not_inside_it(tmp_path, world) -> None:
    from test_variation_publication import SpyingSpreadsheet, latest_report, run, template_tabs

    from laptop_tracker.price_comparison_view import PRICE_TAB, SECTIONS

    chip = SECTIONS[1][1]  # the first retailer column: the coverage sits over the retailers
    clock = iter(range(1, 50))

    def banner(**kw):
        _, fake, _ = run(
            world,
            fake=SpyingSpreadsheet(template_tabs()),
            now=PUBLISHER_NOW + timedelta(seconds=next(clock)),
            **kw,
        )
        return fake.values(PRICE_TAB)[1], fake

    plain, _ = banner()
    assert plain[0].startswith("Week: ") and not any(plain[1:])  # no discovery folder: no chip
    ok, _ = banner(extra=("--discovery-dir", str(discovery_dir(tmp_path, name="ok"))))
    assert ok[0] == plain[0] and ok[chip] == "Coverage: 5/5 retailers checked"
    one, fake = banner(
        extra=("--discovery-dir", str(discovery_dir(tmp_path, {"laptop_factory"}, "lf")))
    )
    # The week and the coverage are two separate cells, never one long line.
    assert one[0] == plain[0] and "Coverage" not in one[0]
    assert one[chip] == "Coverage: 4/5 retailers checked | Laptop Factory incomplete"
    note = fake.cell(PRICE_TAB, 1, chip)["note"]
    assert "never assumed" in note and "verified in-stock prices" in note
    assert fake.cell(PRICE_TAB, 1, 0).get("note", "") == ""
    assert latest_report(world)["retailer_coverage"]["checked"] == 4
    everything, fake = banner(extra=("--discovery-dir", str(discovery_dir(tmp_path, ALL, "all"))))
    assert everything[chip] == (
        "Coverage: 0/5 retailers checked | Retailer comparisons could not be verified this period"
    )
    # Still three rows above the table.
    values = fake.values(PRICE_TAB)
    assert values[3][0] == "Rank" and not any(v for v in values[1][1:] if v != everything[chip])


def test_the_coverage_notice_does_not_change_the_publication_id(tmp_path, world) -> None:
    from test_variation_publication import SpyingSpreadsheet, latest_report, run, template_tabs

    run(world, "--preview", fake=SpyingSpreadsheet(template_tabs()), now=PUBLISHER_NOW)
    without = latest_report(world)["publication_id"]
    run(
        world,
        "--preview",
        fake=SpyingSpreadsheet(template_tabs()),
        now=PUBLISHER_NOW + timedelta(seconds=1),
        extra=("--discovery-dir", str(discovery_dir(tmp_path, {"laptop_factory"}))),
    )
    assert latest_report(world)["publication_id"] == without  # a run saved earlier stays valid


def test_an_unreadable_discovery_summary_stops_the_publisher_before_any_write(
    tmp_path, world
) -> None:
    from test_variation_publication import SpyingSpreadsheet, run, template_tabs

    empty = tmp_path / "nothing"
    empty.mkdir()
    fake = SpyingSpreadsheet(template_tabs())
    code, _, _ = run(world, fake=fake, extra=("--discovery-dir", str(empty)))
    assert code == 1 and not fake.mutations


# --- the driver and the price stage survive a run with nothing collected ------------------------


def test_the_price_stage_runs_when_no_retailer_page_was_collected(tmp_path, capsys) -> None:
    from test_observation_store import T0
    from test_price_tracking import cli_args, load_script, offer

    script = load_script("track_prices")
    folder = tmp_path / "discovery"
    folder.mkdir()
    (folder / "discovery_summary.json").write_text(
        json.dumps({"started_at_utc": "2026-10-06T02:00:00Z"})
    )
    write_csv(
        folder / "search_requests.csv",
        [
            {"target_id": "TGT-000001", "retailer_id": r, "tier": "1_FULL_MODEL_CODE",
             "outcome": "SEARCH_ERROR"}
            for r in ("laptop_factory", "pcx")
        ],
    )  # fmt: skip
    earlier = offer(price="1000.00", at=T0 - timedelta(days=7))  # nothing observed this week
    args = cli_args(tmp_path, [earlier]) + ["--discovery-dir", str(folder)]
    assert script.main(args) == 0  # no --search-context: there was no collection
    capsys.readouterr()
    with (tmp_path / "out" / "weekly_price_comparison.csv").open(encoding="utf-8") as handle:
        import csv

        first = next(csv.DictReader(handle))
    assert first["LF_Price"] == "" and first["PCX_Price"] == ""
    assert first["LF_Price_Status"] == first["PCX_Price_Status"] == "SEARCH_FAILED"
    assert first["Lowest_In_Stock_Price"] == ""


def test_the_driver_passes_collection_inputs_only_when_a_collection_exists() -> None:
    text = (Path(__file__).resolve().parents[1] / "scripts/run_variation_week.sh").read_text()
    assert '[ -f "$weekly_run/discovery/collection/match_results.csv" ]' in text
    assert '[ -d "$weekly_run/discovery/collection" ]' in text
    assert '--discovery-dir "$weekly_run/discovery"' in text  # the publisher states the coverage
    assert "stage discovery 0,2 s_discovery" in text and "stage prices 0 s_prices" in text


# --- the real discovery command's own output ----------------------------------------------------


class EveryRetailerDown:
    """A transport on which every request is answered HTTP 500."""

    live = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url, *, timeout, headers, max_bytes):
        from laptop_tracker.scraping import TransportResponse

        self.calls.append(url)
        return TransportResponse(500, url, b"", {})


def test_the_gate_reads_what_a_real_discovery_run_writes_when_every_retailer_is_down(
    tmp_path,
) -> None:
    """Not synthetic rows: discover_products.py itself, against retailers that all answer HTTP 500,
    with one supplied page so that a collection round runs and fails too."""
    import shutil

    from scraping_fakes import FakeClock
    from test_product_discovery import LF_PRODUCT, discover, inputs

    supplied = tmp_path / "supplied.csv"
    supplied.write_text(
        "supplied_id,retailer_id,url,search_target_id,supplied_by,supplied_on,notes\n"
        f"S1,laptop_factory,{LF_PRODUCT},TGT-000001,test,2026-11-01,\n"
    )
    transport = EveryRetailerDown()
    code = discover.main(
        ["--live", "--collect", "--max-total-requests", "60", "--targets", "TGT-000001,TGT-000002",
         "--retailers", "laptop_factory,pcc", *inputs(tmp_path), "--output-dir",
         str(tmp_path / "real"), "--max-identifier-variant-queries", "0",
         "--supplied-urls", str(supplied)],
        transport=transport,
        clock=FakeClock(),
    )  # fmt: skip
    assert code == 2 and transport.calls
    summary = json.loads((tmp_path / "real/discovery_summary.json").read_text())
    assert summary["run_status"] == "COLLECTION_FAILED"  # the status the old gate refused

    run = make_run(
        tmp_path, name="20261110T010000Z", discovery_code=2, run_status="COLLECTION_FAILED"
    )
    shutil.rmtree(run / "discovery")
    shutil.copytree(tmp_path / "real", run / "discovery")
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS" and result.ok, result.blocking
    assert notice_of(result) == (
        "Coverage: 0/2 retailers checked | Retailer comparisons could not be verified this period"
    )
    requests = policy_of(result)["requests"]
    assert requests["failure_classes"] == {"SERVER_ERROR": 4}
    assert requests["searches_not_sent_retailer_failing"] == 12
    assert any("collection stored no observation" in w for w in result.warnings)
    # The same folder with the observation store reporting an error is not an outage.
    path = run / "discovery/collection/collection_run_summary.json"
    data = json.loads(path.read_text())
    data["rounds"][0]["store"] = {"opened": False, "error": "ObservationStoreError: boom"}
    path.write_text(json.dumps(data))
    assert not evaluate(run).ok
