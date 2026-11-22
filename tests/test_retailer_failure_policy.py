"""Risk-based readiness policy for retailer request failures (2026-11-22).

One failed Octagon page among ~770 requests used to block an otherwise complete weekly report.
These tests pin what the replacement policy tolerates (isolated, visible failures within the
thresholds) and what it still blocks, and that a person's acceptance never reaches any other check.

The request rows are synthetic: they follow the shape of ``search_requests.csv``,
``product_inspections.csv`` and ``discovery_summary.json`` ``job_outcomes``.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from laptop_tracker import release_store as rs
from laptop_tracker import retailer_failure_policy as rfp
from laptop_tracker import run_gate
from laptop_tracker import state_crypto as sc
from laptop_tracker import state_store as ss
from laptop_tracker.retailer_failure_policy import FailurePolicy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_ci_gate import NOW, make_run, write_csv  # noqa: E402

import ci_publish as publish_script  # noqa: E402
import ci_run_gate  # noqa: E402

RETAILERS = ("datablitz", "laptop_factory", "pcx", "pcc", "oct")
OPEN = FailurePolicy(max_total=999, max_per_retailer=999, max_retailer_pct=100, max_total_pct=100)


def srow(retailer, outcome="RESULTS", target="TGT-000001", http="", cache=""):
    return {
        "target_id": target,
        "retailer_id": retailer,
        "outcome": outcome,
        "http_status": http,
        "message": "",
        "cache": cache,
    }


def irow(retailer, outcome="OK", target="TGT-000001", cache=""):
    return {
        "target_id": target,
        "retailer_id": retailer,
        "outcome": outcome,
        "message": "",
        "cache": cache,
    }


def tgt(n: int) -> str:
    return f"TGT-{n:06d}"


def healthy_searches() -> list[dict]:
    return [srow(r, target=tgt(i)) for r in RETAILERS for i in range(1, 121)]  # 600 attempts


def healthy_inspections() -> list[dict]:
    return [
        irow(r, target=tgt(i)) for r in RETAILERS if r != "laptop_factory" for i in range(1, 21)
    ]


def build(
    tmp_path,
    *,
    searches=(),
    inspections=(),
    jobs=None,
    summary=None,
    name="20261110T010000Z",
    with_target=True,
    job_outcomes=True,
) -> Path:
    """A run folder over a healthy 680-request collection plus the given extra rows. ``jobs`` maps
    (target, retailer) to a terminal outcome; every other pair ended as ``NO_RESULTS_RETURNED``."""
    run = make_run(tmp_path, name=name, discovery_code=2, run_status="PARTIAL_SUCCESS")
    search_rows = [*healthy_searches(), *searches]
    inspection_rows = [*healthy_inspections(), *inspections]
    if not with_target:
        search_rows = [{k: v for k, v in r.items() if k != "target_id"} for r in search_rows]
        inspection_rows = [
            {k: v for k, v in r.items() if k != "target_id"} for r in inspection_rows
        ]
    write_csv(run / "discovery/search_requests.csv", search_rows)
    write_csv(run / "discovery/product_inspections.csv", inspection_rows)
    write_csv(run / "discovery/search_jobs.csv", [{"target_id": tgt(1)}])
    pairs = (
        {(r["target_id"], r["retailer_id"]) for r in [*search_rows, *inspection_rows]}
        if (with_target)
        else set()
    )
    pairs |= set(jobs or {})
    body = {
        "run_status": "PARTIAL_SUCCESS",
        "requests_sent": 770,
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


def evaluate(run, **kw):
    return run_gate.evaluate(run, now=NOW, **kw)


def policy_of(result) -> dict:
    return result.metrics["retailer_failure_policy"]


def oct_inspection_failure(n=7):
    return irow("oct", "FETCH_FAILED", tgt(n))


# severity A: tolerated --------------------------------------------------------------------------


def test_no_failures_passes_and_cached_or_unsent_rows_are_not_attempts(tmp_path) -> None:
    extra = [
        srow("oct", cache="SAVED:old"),
        srow("oct", cache="SAME_RUN"),
        srow("oct", "NOT_SENT_QUERY_LIMIT"),
    ]
    result = evaluate(build(tmp_path, searches=extra, inspections=[irow("oct", cache="SAVED:x")]))
    assert result.decision == "PASS" and result.ok and not result.warnings
    requests = policy_of(result)["requests"]
    assert requests["attempted"] == 680 and requests["failed"] == 0
    assert policy_of(result)["automated_decision"] == "PASS"


def test_one_octagon_page_failure_in_a_healthy_collection_is_a_warning(tmp_path) -> None:
    run = build(
        tmp_path,
        inspections=[oct_inspection_failure()],
        jobs={(tgt(7), "oct"): "FETCH_FAILED"},
    )
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS" and result.ok and not result.blocking
    assert len(result.warnings) == 1
    assert "oct: 1 page fetch(es) failed" in result.warnings[0]
    p = policy_of(result)
    assert p["severity"] == "minor" and p["automated_decision"] == "PASS_WITH_WARNINGS"
    assert p["requests"]["inspection_failures"] == 1 and p["requests"]["search_failures"] == 0
    assert p["requests"]["attempted"] == 681 and p["requests"]["failure_rate_pct"] == 0.15
    assert p["retailers"]["oct"]["failed"] == 1 and p["retailers"]["oct"]["attempted"] == 141
    assert p["thresholds"]["max_total"] == 3
    assert p["manual_acceptance"] == {"requested": False, "used": False, "overrode_block": False}
    assert p["affected_jobs"][0]["impact"] == "visible_in_report"
    # Compatibility: the fields existing consumers read are all still there.
    as_dict = result.as_dict()
    assert {"ok", "blocking", "warnings", "metrics", "decision"} <= set(as_dict)
    assert as_dict["decision"] == "PASS_WITH_WARNINGS" and as_dict["ok"] is True
    assert result.metrics["retailer_failures"] == ["oct: 1 page fetch(es) failed"]


def test_two_isolated_page_failures_within_the_thresholds_are_warnings(tmp_path) -> None:
    run = build(
        tmp_path,
        inspections=[oct_inspection_failure(7), oct_inspection_failure(8)],
        jobs={(tgt(7), "oct"): "FETCH_FAILED", (tgt(8), "oct"): "FETCH_FAILED"},
    )
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS", result.blocking
    assert policy_of(result)["requests"]["failed"] == 2


def test_an_isolated_failed_search_that_the_report_shows_as_incomplete_is_a_warning(
    tmp_path,
) -> None:
    run = build(
        tmp_path,
        searches=[srow("pcx", "SEARCH_ERROR", tgt(50), http="503")],
        jobs={(tgt(50), "pcx"): "PROVIDER_FAILURE"},
        summary={
            "search_outcomes": {"pcx:SEARCH_ERROR": 1},
            "discovery_outcomes": {"PROVIDER_FAILURE": 1},
        },
    )
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS", result.blocking
    assert policy_of(result)["requests"]["search_failures"] == 1


def test_a_failure_is_counted_once_however_many_files_mention_it(tmp_path) -> None:
    run = build(
        tmp_path,
        searches=[srow("pcx", "SEARCH_ERROR", tgt(50), http="429")],
        inspections=[irow("pcc", "FETCH_FAILED", tgt(9))],
        jobs={(tgt(50), "pcx"): "PROVIDER_FAILURE", (tgt(9), "pcc"): "FETCH_FAILED"},
        summary={
            "search_outcomes": {"pcx:SEARCH_ERROR": 1},
            "discovery_outcomes": {"PROVIDER_FAILURE": 1},
        },
    )
    write_csv(
        run / "discovery/collection_rounds/round_1/rate_limit_events.csv",
        [{"retailer_id": "pcx"}],
    )
    result = evaluate(run)
    assert policy_of(result)["requests"]["failed"] == 2  # one search + one page, not four
    assert result.metrics["retailers"]["pcx"]["http_429"] == 2  # existing monitoring, unchanged
    assert result.metrics["retailers"]["pcc"]["other_http_errors"] == 1


# severity B: impact -----------------------------------------------------------------------------


def concentrated(tmp_path, *, priced_elsewhere):
    jobs = {(tgt(10), "oct"): "FETCH_FAILED"}
    if priced_elsewhere:
        jobs[(tgt(10), "datablitz")] = "SUITABLE_OFFER_FOUND"
    return build(
        tmp_path,
        inspections=[irow("oct", "FETCH_FAILED", tgt(10)), irow("oct", "FETCH_FAILED", tgt(10))],
        jobs=jobs,
    )


def test_failures_concentrated_on_one_target_without_any_price_block(tmp_path) -> None:
    result = evaluate(concentrated(tmp_path, priced_elsewhere=False))
    assert result.decision == "BLOCKED" and not result.ok
    assert any(
        "concentrated on one target that has no retailer price" in b for b in result.blocking
    )
    ids = {f["id"] for f in policy_of(result)["findings"]}
    assert "concentrated_unpriced" in ids and policy_of(result)["severity"] == "moderate"


def test_failures_concentrated_on_a_target_that_is_priced_elsewhere_only_warn(tmp_path) -> None:
    result = evaluate(concentrated(tmp_path, priced_elsewhere=True))
    assert result.decision == "PASS_WITH_WARNINGS", result.blocking
    assert any("concentrated on one target" in w for w in result.warnings)


def test_a_failed_search_that_would_read_as_a_confirmed_absence_blocks(tmp_path) -> None:
    run = build(
        tmp_path,
        searches=[srow("oct", "SEARCH_ERROR", tgt(11), http="503")],
        jobs={(tgt(11), "oct"): "NO_RESULTS_RETURNED"},
        summary={"search_outcomes": {"oct:SEARCH_ERROR": 1}},
    )
    result = evaluate(run)
    assert not result.ok and any("confirmed absence" in b for b in result.blocking)
    assert policy_of(result)["affected_jobs"][0]["impact"] == "reads_as_absence"


@pytest.mark.parametrize("case", ["job_missing", "no_job_outcomes", "no_target_column"])
def test_impact_that_cannot_be_established_requires_manual_review(tmp_path, case) -> None:
    kw = {"inspections": [oct_inspection_failure(7)]}
    if case == "job_missing":
        kw["jobs"] = {(tgt(300), "oct"): "FETCH_FAILED"}  # a different pair than the failed one
        kw["inspections"] = [irow("oct", "FETCH_FAILED", tgt(301))]
    elif case == "no_job_outcomes":
        kw["job_outcomes"] = False
    else:
        kw["with_target"] = False
    run = build(tmp_path, **kw)
    blocked = evaluate(run)
    assert not blocked.ok and any("manual review required" in b for b in blocked.blocking)
    assert evaluate(run, accept_retailer_failures=True).ok  # a person may accept it


# severity C: significant ------------------------------------------------------------------------


def spread(retailers):
    return [irow(r, "FETCH_FAILED", tgt(20 + i)) for i, r in enumerate(retailers)]


def spread_jobs(retailers):
    return {(tgt(20 + i), r): "FETCH_FAILED" for i, r in enumerate(retailers)}


def test_more_failures_than_the_total_limit_block(tmp_path) -> None:
    four = ["datablitz", "pcx", "pcc", "oct"]
    result = evaluate(build(tmp_path, inspections=spread(four), jobs=spread_jobs(four)))
    assert not result.ok and any(
        "4 request(s) failed across all retailers" in b for b in result.blocking
    )


def test_more_failures_than_the_per_retailer_limit_block(tmp_path) -> None:
    three = ["oct", "oct", "oct"]
    result = evaluate(build(tmp_path, inspections=spread(three), jobs=spread_jobs(three)))
    assert not result.ok and any("oct: 3 request(s) failed (limit 2)" in b for b in result.blocking)


def test_the_configured_percentage_limits_apply(tmp_path) -> None:
    run = build(
        tmp_path, inspections=[oct_inspection_failure()], jobs={(tgt(7), "oct"): "FETCH_FAILED"}
    )
    assert evaluate(run).ok
    overall = evaluate(run, policy=FailurePolicy(max_total_pct=0.1))
    assert any("1 of 681 requests failed" in b for b in overall.blocking)
    retailer = evaluate(run, policy=FailurePolicy(max_retailer_pct=0.5))
    assert any("oct: 1 of 141 requests failed" in b for b in retailer.blocking)
    assert evaluate(run, policy=FailurePolicy(max_total=0)).ok is False
    assert evaluate(run, policy=FailurePolicy(max_per_retailer=0)).ok is False


def test_percentages_are_compared_exactly() -> None:
    assert not rfp._exceeds(1, 100, 1.0) and rfp._exceeds(2, 100, 1.0)
    assert not rfp._exceeds(0, 0, 0.0) and rfp._exceeds(1, 3, 33.3) and not rfp._exceeds(1, 3, 34)


def test_a_retailer_that_answered_nothing_is_unavailable(tmp_path) -> None:
    run = build(tmp_path)
    rows = [r for r in healthy_searches() if r["retailer_id"] != "oct"]
    rows += [srow("oct", "SEARCH_ERROR", tgt(i), http="503") for i in (1, 2, 3)]
    write_csv(run / "discovery/search_requests.csv", rows)
    result = evaluate(run, policy=OPEN)
    assert not result.ok and any("oct" in b and "unavailable" in b for b in result.blocking)


def test_repeated_search_errors_that_stopped_the_retailer_block(tmp_path) -> None:
    searches = [srow("pcx", "SEARCH_ERROR", tgt(60 + i), http="503") for i in range(2)]
    searches += [srow("pcx", "NOT_SENT_RETAILER_FAILING", tgt(70 + i)) for i in range(5)]
    jobs = {(tgt(70 + i), "pcx"): "PROVIDER_FAILURE" for i in range(5)}
    jobs |= {(tgt(60 + i), "pcx"): "PROVIDER_FAILURE" for i in range(2)}
    run = build(
        tmp_path,
        searches=searches,
        jobs=jobs,
        summary={
            "search_outcomes": {"pcx:SEARCH_ERROR": 2},
            "search_stop_reasons": {"RETAILER_STOPPED_AFTER_ERRORS": 5},
            "discovery_outcomes": {"PROVIDER_FAILURE": 7},
        },
    )
    for policy in (None, OPEN):
        result = evaluate(run, policy=policy)
        assert not result.ok
        assert any("pcx: searching stopped after repeated errors" in b for b in result.blocking)


def test_a_large_unexecuted_part_of_the_plan_blocks_when_requests_also_failed(tmp_path) -> None:
    unsent = lambda n: [srow("oct", "NOT_SENT_BUDGET_EXHAUSTED", tgt(i)) for i in range(n)]  # noqa: E731
    failure = {"inspections": [oct_inspection_failure()], "jobs": {(tgt(7), "oct"): "FETCH_FAILED"}}
    assert evaluate(build(tmp_path, searches=unsent(20), **failure)).ok  # 3.1% of the plan
    blocked = evaluate(build(tmp_path, name="20261110T020000Z", searches=unsent(40), **failure))
    assert any("planned searches were never executed" in b for b in blocked.blocking)
    # Without any failed request, a bounded search is the normal PARTIAL_SUCCESS: unchanged.
    quiet = evaluate(build(tmp_path, name="20261110T030000Z", searches=unsent(100)))
    assert quiet.decision == "PASS"
    # A query limit is a choice of the plan, never an unexecuted search.
    limited = [srow("oct", "NOT_SENT_QUERY_LIMIT", tgt(i)) for i in range(100)]
    assert evaluate(build(tmp_path, name="20261110T040000Z", searches=limited, **failure)).ok


# discovery files that cannot be trusted: never accepted -----------------------------------------


def test_missing_or_invalid_request_files_block_even_when_accepted(tmp_path) -> None:
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


def test_a_bundle_saved_without_request_files_needs_an_explicit_acceptance(tmp_path) -> None:
    run = build(tmp_path)
    for name in ("search_requests.csv", "product_inspections.csv", "search_jobs.csv"):
        (run / "discovery" / name).unlink()
    blocked = evaluate(run)
    assert not blocked.ok and any("saved before request files" in b for b in blocked.blocking)
    accepted = evaluate(run, accept_retailer_failures=True)
    assert accepted.ok and accepted.decision == "PASS_WITH_WARNINGS"
    assert policy_of(accepted)["manual_acceptance"]["overrode_block"] is True


def test_the_failure_policy_does_not_replace_the_discovery_status_check(tmp_path) -> None:
    run = build(tmp_path)
    (run / "discovery/discovery_summary.json").unlink()
    assert not evaluate(run, accept_retailer_failures=True).ok


# manual acceptance ------------------------------------------------------------------------------


def test_a_minor_failure_with_a_manual_acceptance_stays_publishable_and_is_recorded(
    tmp_path,
) -> None:
    run = build(
        tmp_path, inspections=[oct_inspection_failure()], jobs={(tgt(7), "oct"): "FETCH_FAILED"}
    )
    result = evaluate(run, accept_retailer_failures=True)
    assert result.decision == "PASS_WITH_WARNINGS" and result.ok
    assert policy_of(result)["manual_acceptance"] == {
        "requested": True,
        "used": True,
        "overrode_block": False,
    }
    assert all("Accepted explicitly" in w for w in result.warnings)


def test_a_serious_failure_is_published_only_with_a_manual_acceptance(tmp_path) -> None:
    four = ["datablitz", "pcx", "pcc", "oct"]
    run = build(tmp_path, inspections=spread(four), jobs=spread_jobs(four))
    assert not evaluate(run).ok
    accepted = evaluate(run, accept_retailer_failures=True)
    assert accepted.ok and accepted.decision == "PASS_WITH_WARNINGS"
    assert policy_of(accepted)["manual_acceptance"]["overrode_block"] is True
    assert policy_of(accepted)["automated_decision"] == "BLOCKED"  # what the policy said itself
    assert any("limit 3" in w and "Accepted explicitly" in w for w in accepted.warnings)


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
def test_an_acceptance_never_overrides_another_safeguard(tmp_path, damage) -> None:
    four = ["datablitz", "pcx", "pcc", "oct"]
    run = build(tmp_path, inspections=spread(four), jobs=spread_jobs(four))
    damage(run)
    assert not evaluate(run, accept_retailer_failures=True).ok


def test_an_acceptance_never_overrides_a_stale_run(tmp_path) -> None:
    four = ["datablitz", "pcx", "pcc", "oct"]
    run = build(tmp_path, inspections=spread(four), jobs=spread_jobs(four))
    assert evaluate(run, accept_retailer_failures=True).ok
    late = run_gate.evaluate(run, now=NOW + timedelta(days=9), accept_retailer_failures=True)
    assert not late.ok and any("superseded" in b for b in late.blocking)


def test_a_coverage_regression_blocks_whatever_the_failures_and_acceptance(tmp_path) -> None:
    run = build(
        tmp_path, inspections=[oct_inspection_failure()], jobs={(tgt(7), "oct"): "FETCH_FAILED"}
    )
    previous = {"matching_priced_variation_rows": 60}  # make_run's preview has 30 matching rows
    assert evaluate(run, previous_counts=previous).ok  # the guard is off unless configured
    for accept in (False, True):
        guarded = evaluate(
            run, previous_counts=previous, max_matching_drop_pct=20, accept_retailer_failures=accept
        )
        assert not guarded.ok and any("fell from 60 to 30" in b for b in guarded.blocking)


# configuration ----------------------------------------------------------------------------------


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
    assert (FailurePolicy().max_total, FailurePolicy().max_per_retailer) == (3, 2)
    assert (FailurePolicy().max_retailer_pct, FailurePolicy().max_total_pct) == (10.0, 1.0)


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
def test_an_invalid_threshold_is_refused_never_replaced_by_a_default(variable, value) -> None:
    with pytest.raises(rfp.PolicyConfigError, match=variable):
        FailurePolicy.from_environment({variable: value})


def test_the_gate_script_refuses_an_invalid_threshold_and_reports_warnings(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    run = build(
        tmp_path, inspections=[oct_inspection_failure()], jobs={(tgt(7), "oct"): "FETCH_FAILED"}
    )
    out = tmp_path / "gate.json"
    argv = ["--run-dir", str(run), "--output", str(out), "--max-age-hours", "100000"]
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert ci_run_gate.main(argv) == 0  # PASS_WITH_WARNINGS does not fail the job
    captured = capsys.readouterr()
    assert json.loads(out.read_text())["decision"] == "PASS_WITH_WARNINGS"
    assert "Publication gate: PASS_WITH_WARNINGS" in captured.out
    assert "::warning title=Retailer request failures::" in captured.out
    assert "WARNING: " in captured.err and "oct: 1 page fetch(es) failed" in captured.out
    assert "**Gate:** PASS_WITH_WARNINGS" in captured.out and "| oct | 141 | 1 |" in captured.out
    assert "TGT-" not in captured.out and "TGT-" not in captured.err  # no target in a public log
    assert "TGT-000007" in out.read_text()  # but the diagnostics file keeps the affected pair

    out.unlink()
    monkeypatch.setenv("RETAILER_FAILURE_MAX_TOTAL", "-4")
    assert ci_run_gate.main(argv) == 1 and not out.exists()
    assert "invalid retailer-failure policy" in capsys.readouterr().err
    monkeypatch.setenv("RETAILER_FAILURE_MAX_TOTAL", "0")  # a valid, stricter limit applies
    assert ci_run_gate.main(argv) == 1


def test_the_publish_script_applies_the_same_policy_and_refuses_an_invalid_one(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    run = build(tmp_path)
    called = []

    def publisher(argv):
        called.append(argv)
        return 1

    argv = ["--run-dir", str(run), "--max-age-hours", "100000", "--no-state"]
    monkeypatch.setenv("RETAILER_FAILURE_MAX_PER_RETAILER", "-1")
    assert publish_script.main(argv, publisher=publisher, open_spreadsheet=lambda r: None) == 1
    assert not called and "invalid retailer-failure policy" in capsys.readouterr().err


# one policy for both jobs -----------------------------------------------------------------------


def test_a_stored_run_is_judged_by_the_same_files_and_policy_as_the_collected_one(
    tmp_path,
) -> None:
    run = build(
        tmp_path, inspections=[oct_inspection_failure()], jobs={(tgt(7), "oct"): "FETCH_FAILED"}
    )
    store = rs.LocalReleaseStore(tmp_path / "releases")
    ring = sc.KeyRing(sc.parse_key(sc.generate_key()))
    ss.save_bundle(store, ring, run, run.name)
    loaded = tmp_path / "publish_job" / run.name
    ss.load_bundle(store, ring, run.name, loaded)
    before, after = evaluate(run), evaluate(loaded)
    assert before.decision == after.decision == "PASS_WITH_WARNINGS"
    assert policy_of(before)["requests"] == policy_of(after)["requests"]
    assert policy_of(before)["affected_jobs"] == policy_of(after)["affected_jobs"]


# the 2026-11-22 run -----------------------------------------------------------------------------


def test_the_shape_of_the_2026_11_22_run_is_a_warning(tmp_path) -> None:
    """Request counts per retailer as the failed run's own gate.json recorded them (searches 136,
    154, 127, 172, 171; page inspections 43, 0, 65, 20, 33; one failed Octagon inspection). The
    rows are synthetic: the real request files are not in any stored bundle."""
    searches = {"datablitz": 136, "laptop_factory": 154, "pcx": 127, "pcc": 172, "oct": 171}
    inspections = {"datablitz": 43, "laptop_factory": 0, "pcx": 65, "pcc": 20, "oct": 33}
    search_rows = [srow(r, target=tgt(i + 1)) for r, n in searches.items() for i in range(n)]
    inspection_rows = [
        irow(r, "FETCH_FAILED" if (r, i) == ("oct", 32) else "OK", tgt(i + 1))
        for r, n in inspections.items()
        for i in range(n)
    ]
    run = make_run(tmp_path, discovery_code=2, run_status="PARTIAL_SUCCESS")
    write_csv(run / "discovery/search_requests.csv", search_rows)
    write_csv(run / "discovery/product_inspections.csv", inspection_rows)
    write_csv(run / "discovery/search_jobs.csv", [{"target_id": tgt(1)}])
    (run / "discovery/discovery_summary.json").write_text(
        json.dumps(
            {
                "run_status": "PARTIAL_SUCCESS",
                "requests_sent": 770,
                "job_outcomes": [
                    {
                        "target_id": tgt(33),
                        "retailer_id": "oct",
                        "terminal_outcome": "FETCH_FAILED",
                        "discovery_outcome": "QUERY_PLAN_EXHAUSTED",
                    }
                ],
            }
        )
    )
    result = evaluate(run)
    assert result.decision == "PASS_WITH_WARNINGS", result.blocking
    assert "oct: 1 page fetch(es) failed" in result.warnings[0]
    assert policy_of(result)["requests"]["failed"] == 1
    assert policy_of(result)["retailers"]["oct"]["attempted"] == 204


def test_a_publication_with_warnings_goes_ahead_and_records_the_gate_decision(
    tmp_path, monkeypatch
) -> None:
    from test_ci_publish import PUB, FakeSheet, Publisher, good_report

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    store = rs.LocalReleaseStore(tmp_path / "releases")
    ring = sc.KeyRing(sc.parse_key(sc.generate_key()))
    run = build(
        tmp_path, inspections=[oct_inspection_failure()], jobs={(tgt(7), "oct"): "FETCH_FAILED"}
    )
    publisher = Publisher(tmp_path, report=good_report())
    code = publish_script.main(
        [
            "--run-dir",
            str(run),
            "--max-age-hours",
            "100000",
            "--write",
            "--accept-retailer-failures",
        ],
        store=store,
        ring=ring,
        publisher=publisher,
        open_spreadsheet=lambda read_only: FakeSheet(),
        sleep=lambda s: None,
    )
    assert code == 0 and len(publisher.writes()) == 1
    (attempt,) = ss.read_publication_attempts(store, ring, PUB)
    readiness = attempt["readiness"]
    assert readiness["decision"] == "PASS_WITH_WARNINGS"
    assert readiness["requests"]["failed"] == 1 and readiness["thresholds"]["max_total"] == 3
    assert readiness["manual_acceptance"] == {
        "requested": True,
        "used": True,
        "overrode_block": False,
    }
