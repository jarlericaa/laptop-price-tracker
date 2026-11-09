"""P1 publication diagnostics (2026-11-03): eligibility, mapping exclusions, search coverage.

Synthetic files only. The diagnostics are complete local CSVs plus a preview plan; the search
coverage groups by whatever outcome columns and values a run records, old or new.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import sys
from pathlib import Path

import pytest

from laptop_tracker import publication_diagnostics as pd_


def eligible_row(model_id: str, **values: str) -> dict[str, str]:
    base = {
        "rank": "1",
        "source_key": f"1|2|{model_id}",
        "shop_id": "1",
        "item_id": "2",
        "model_id": model_id,
        "item_name": "Item",
        "shopee_model_name": f"Var {model_id}",
        "m_minus_1_gmv_usd": "10",
        "mtd_gmv_usd": "0",
        "ranking_gmv_usd": "10",
        "shopee_current_price_php": "999",
        "eligibility_status": "ELIGIBLE",
        "eligibility_reason": "Inclusion rule matched.",
        "present_in_mtd": "False",
        "price_status": "PRICE_OK",
        "present_in_m_minus_1": "True",
        "m_minus_1_status": "PRESENT",
        "mtd_status": "NO_RECORD_TREATED_AS_ZERO",
        "price_period_month": "2026-10",
        "price_record_date": "2026-10-01",
        "price_basis": "M_MINUS_1_PRICE",
    }
    return base | values


def ranked_row(model_id: str, status: str, reason: str = "", **values: str) -> dict[str, str]:
    base = {
        "eligible_rank": "1",
        "comparison_row_id": f"SV-1-2-{model_id}",
        "source_key": f"1|2|{model_id}",
        "source_model_id": model_id,
        "shop_name": "Shop",
        "item_name": "Item",
        "shopee_model_name": f"Var {model_id}",
        "ranking_gmv_usd": "10",
        "selection_status": status,
        "exclusion_reason": reason,
        "target_id": "",
        "mapping_status": "UNMAPPED",
        "comparison_detail": "Configuration review pending: No approved SOURCE_MODEL_MAP row.",
    }
    return base | values


# Eligibility --------------------------------------------------------------------------------


def test_every_eligible_variation_is_listed_with_months_price_basis_and_selection():
    eligible = [eligible_row("10"), eligible_row("11", present_in_mtd="True", mtd_status="PRESENT")]
    ranking = [
        ranked_row("10", "SELECTED", target_id="TGT-1", mapping_status="MAPPED_VERIFIED"),
        ranked_row("11", "EXCLUDED_NOT_VERIFIED_MAPPED", "UNMAPPED"),
    ]

    rows = pd_.build_eligibility_variations(eligible, ranking, {"1": "Fallback"})

    assert [r["model_id"] for r in rows] == ["10", "11"]
    first = rows[0]
    assert first["selection_status"] == "SELECTED" and first["target_id"] == "TGT-1"
    assert first["present_in_m_minus_1"] == "True" and first["mtd_status"].startswith("NO_RECORD")
    assert first["price_basis"] == "M_MINUS_1_PRICE" and first["price_record_date"] == "2026-10-01"
    assert rows[1]["exclusion_reason"] == "UNMAPPED" and rows[1]["present_in_mtd"] == "True"
    assert set(first) == set(pd_.ELIGIBILITY_COLUMNS)


def test_an_eligible_variation_missing_from_the_ranking_is_shown_not_dropped():
    rows = pd_.build_eligibility_variations([eligible_row("10")], [], {"1": "Shop One"})

    assert rows[0]["selection_status"] == pd_.NOT_IN_RANKING and rows[0]["shop_name"] == "Shop One"
    checks = pd_.build_checks([eligible_row("10")], rows, [], [], [])
    assert not next(c for c in checks if "variation ranking" in c["check"])["passed"]


def test_review_pending_and_excluded_listings_carry_their_reasons():
    review = [
        {
            "model_id": "20",
            "shop_id": "1",
            "item_name": "Dell 15",
            "eligibility_status": "REVIEW",
            "eligibility_reason": "Insufficient evidence for automatic classification.",
            "review_decision": "",
        }
    ]
    excluded = [
        {
            "model_id": "21",
            "shop_id": "1",
            "item_name": "Camera",
            "eligibility_status": "EXCLUDED",
            "eligibility_reason": "Product-level exclusion rule matched.",
            "matched_exclude_rule": "camera",
            "present_in_mtd": "True",
        }
    ]

    rows = pd_.build_not_eligible_listings(review, excluded, {"1": "Shop One"})

    assert [r["disposition"] for r in rows] == [pd_.REVIEW_PENDING, pd_.EXCLUDED]
    assert rows[0]["reason"] == "Insufficient evidence for automatic classification."
    assert rows[1]["reason"] == "Product-level exclusion rule matched. [camera]"
    assert rows[1]["shop_name"] == "Shop One" and rows[1]["present_in_mtd"] == "True"


# Mapping exclusions --------------------------------------------------------------------------


def test_mapping_exclusions_give_the_exact_reason_and_evidence_for_each_unselected_variation():
    ranking = [
        ranked_row("10", "SELECTED"),
        ranked_row("11", "EXCLUDED_NOT_VERIFIED_MAPPED", "UNMAPPED"),
        ranked_row(
            "12",
            "EXCLUDED_NOT_VERIFIED_MAPPED",
            "MAPPED_TARGET_CPU_MODEL_CONFLICT",
            target_id="TGT-9",
            comparison_detail="cpu_model 'Intel i7' indicates Core i7, but cpu_series 'Core i5'.",
            variation_spec_conflicts="cpu_series: variation states 'Core i3'",
        ),
        ranked_row("13", "QUALIFIED_NOT_SELECTED", comparison_detail="Trusted comparison context."),
    ]
    exclusions = [
        {"comparison_row_id": "SV-1-2-11", "suggested_action": "Research the configuration."}
    ]
    reviews = [
        {
            "source_model_id": "11",
            "candidate_name": "Var 11",
            "status": "REVIEW_REQUIRED",
            "review_reason": "STILL_UNMAPPED.",
        }
    ]
    audit = {"representative_exclusions": {"UNMAPPED": [{"source_key": "1|2|11"}]}}
    queue = [
        {"scope": "VARIATION_BINDING", "source_model_id": "12", "queue_id": "CC-VAR-12"},
        {"scope": "TARGET", "source_model_id": "12", "queue_id": "CC-TGT-TGT-9"},
    ]

    rows = pd_.build_mapping_exclusions(ranking, exclusions, reviews, audit, queue)

    assert [r["source_key"].split("|")[-1] for r in rows] == ["11", "12", "13"]  # SELECTED omitted
    unmapped, conflict, below = rows
    assert unmapped["exclusion_reason"] == "UNMAPPED" and unmapped["audit_representative"] == "TRUE"
    assert unmapped["match_review_status"] == "REVIEW_REQUIRED"
    assert unmapped["match_review_variation_state"] == "CURRENT"
    assert unmapped["suggested_action"] == "Research the configuration."
    assert conflict["exclusion_reason"] == "MAPPED_TARGET_CPU_MODEL_CONFLICT"
    assert "Intel i7" in conflict["evidence"] and "Core i3" in conflict["variation_spec_conflicts"]
    assert conflict["correction_queue_id"] == "CC-VAR-12"
    assert conflict["match_review_status"] == "NO_REVIEW_ROW"
    assert below["exclusion_reason"] == pd_.QUALIFIED_BELOW_LIMIT


# Search coverage -----------------------------------------------------------------------------


def job(target: str, retailer: str, **values: str) -> dict[str, str]:
    base = {
        "target_id": target,
        "rank": "1",
        "retailer_id": retailer,
        "queries_planned": "2",
        "queries_run": "2",
        "pages_sent": "1",
        "pages_from_cache": "1",
        "pages_not_sent": "0",
        "tiers_run": "1_PRODUCT_NAME",
        "stop_reason": "ALL_QUERY_TIERS_TRIED",
        "search_coverage": "COMPLETE",
        "discovery_outcome": "QUERY_PLAN_EXHAUSTED",
        "candidate_outcomes": "x" * 300,  # free text, not a category
    }
    return base | values


def candidate(target: str, retailer: str, decision: str, plausible: str = "FALSE"):
    return {
        "target_id": target,
        "retailer_id": retailer,
        "decision": decision,
        "plausible": plausible,
    }


def test_one_row_per_target_and_retailer_with_counts_caps_and_reuse():
    jobs = [
        job("TGT-1", "pcx", search_coverage="RESULT_CAP_REACHED", pages_not_sent="1"),
        job("TGT-1", "oct"),
    ]
    results = [{"target_id": "TGT-1", "retailer_id": "pcx"}] * 3
    candidates = [
        candidate("TGT-1", "pcx", "REJECTED_ACCESSORY"),
        candidate("TGT-1", "pcx", "COLLECT", plausible="TRUE"),
        candidate("TGT-1", "pcx", "NOT_SELECTED_LIMIT"),
        candidate("TGT-1", "pcx", "SOMETHING_ELSE"),
    ]
    inspections = [
        {
            "target_id": "TGT-1",
            "retailer_id": "pcx",
            "outcome": "SINGLE_VARIANT",
            "cache": "SAVED:x",
        },
        {"target_id": "TGT-1", "retailer_id": "pcx", "outcome": "STEP_FAILED", "cache": ""},
    ]
    requests = [
        {"target_id": "TGT-1", "retailer_id": "pcx", "cache": "SAVED:x"},
        {"target_id": "TGT-1", "retailer_id": "pcx", "cache": "SAME_RUN"},
        {"target_id": "TGT-1", "retailer_id": "pcx", "cache": ""},
    ]

    rows, columns = pd_.build_search_coverage(jobs, results, candidates, inspections, requests)

    assert [(r["target_id"], r["retailer_id"]) for r in rows] == [
        ("TGT-1", "pcx"),
        ("TGT-1", "oct"),
    ]
    pcx, oct_ = rows
    assert pcx["terminal_outcome"] == "QUERY_PLAN_EXHAUSTED"
    assert pcx["terminal_outcome_column"] == "discovery_outcome"
    assert (pcx["queries_planned"], pcx["queries_run"], pcx["pages_sent"]) == ("2", "2", "1")
    assert pcx["search_results"] == "3" and pcx["candidates"] == "4"
    assert pcx["candidates_rejected"] == "1" and pcx["candidates_collected"] == "1"
    assert pcx["candidates_not_selected"] == "1" and pcx["candidates_other_decision"] == "1"
    assert pcx["plausible_candidates"] == "1" and pcx["inspections"] == "2"
    assert pcx["inspection_outcomes"] == "SINGLE_VARIANT=1; STEP_FAILED=1"
    assert (
        pcx["search_requests_reused_saved"] == "1" and pcx["search_requests_reused_same_run"] == "1"
    )
    assert pcx["requests_avoided_by_reuse"] == "2"  # 1 search page from cache + 1 inspection reused
    assert pcx["cap_or_budget_reached"] == "TRUE"
    assert set(pcx["caps_reached"].split("; ")) == {
        "RESULT_CAP_REACHED",
        "SEARCH_PAGES_NOT_SENT",
        "CANDIDATE_LIMIT",
    }
    assert oct_["cap_or_budget_reached"] == "FALSE" and oct_["candidates"] == "0"
    assert columns[: len(pd_.SEARCH_COVERAGE_COLUMNS)] == list(pd_.SEARCH_COVERAGE_COLUMNS)


def test_search_coverage_tolerates_an_unknown_outcome_vocabulary():
    jobs = [
        job("TGT-1", "pcx", terminal_outcome="PAGE_FETCHED_IDENTITY_UNRESOLVED", extra_note="n1"),
        job("TGT-2", "pcx", terminal_outcome="OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS"),
        job("TGT-3", "oct", terminal_outcome="SOME_FUTURE_VALUE_NOBODY_LISTED", extra_note="n3"),
    ]

    rows, columns = pd_.build_search_coverage(jobs)
    summary = pd_.search_coverage_summary(rows, jobs)

    assert [r["terminal_outcome"] for r in rows] == [
        "PAGE_FETCHED_IDENTITY_UNRESOLVED",
        "OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS",
        "SOME_FUTURE_VALUE_NOBODY_LISTED",
    ]
    assert rows[0]["terminal_outcome_column"] == "terminal_outcome"
    assert summary["terminal_outcome_column"] == "terminal_outcome"
    assert summary["terminal_outcomes"]["SOME_FUTURE_VALUE_NOBODY_LISTED"] == 1
    assert summary["terminal_outcomes_by_retailer"]["pcx"] == {
        "PAGE_FETCHED_IDENTITY_UNRESOLVED": 1,
        "OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS": 1,
    }
    # Every outcome-like categorical column is reported, free text is not, and extras are kept.
    assert {"terminal_outcome", "discovery_outcome", "stop_reason"} <= set(
        summary["outcome_columns"]
    )
    assert "candidate_outcomes" not in summary["outcome_columns"]
    assert "extra_note" in columns and rows[2]["extra_note"] == "n3"
    assert "terminal_outcome=SOME_FUTURE_VALUE_NOBODY_LISTED" in rows[2]["outcome_columns"]


def test_search_coverage_still_reads_the_old_vocabulary_without_an_outcome_column():
    jobs = [job("TGT-1", "pcx"), job("TGT-2", "pcx", stop_reason="PLAUSIBLE_CANDIDATE_FOUND")]
    for old in jobs:
        del old["discovery_outcome"]

    rows, _ = pd_.build_search_coverage(jobs)

    assert [r["terminal_outcome"] for r in rows] == [
        "ALL_QUERY_TIERS_TRIED",
        "PLAUSIBLE_CANDIDATE_FOUND",
    ]
    assert rows[0]["terminal_outcome_column"] == "stop_reason"


def test_search_coverage_with_no_recorded_outcome_says_unrecorded_and_blank_reuse():
    jobs = [{"target_id": "TGT-1", "retailer_id": "pcx", "queries_run": "1"}]

    rows, _ = pd_.build_search_coverage(jobs)

    assert rows[0]["terminal_outcome"] == "UNRECORDED"
    assert rows[0]["requests_avoided_by_reuse"] == ""  # not recorded, so not guessed
    assert rows[0]["cap_or_budget_reached"] == "FALSE"


def test_run_budget_use_is_read_from_the_discovery_summary():
    jobs = [job("TGT-1", "pcx")]
    rows, _ = pd_.build_search_coverage(jobs)
    run = {
        "requests_sent": 100,
        "search_requests_sent": 80,
        "product_inspections": 20,
        "limits": {
            "max_total_requests": 100,
            "max_search_requests": 4000,
            "max_product_fetches": 3000,
        },
        "search_responses_reused": {"SAVED": 5},
        "discovery_outcomes": {"X": 1},
    }

    summary = pd_.search_coverage_summary(rows, jobs, run)

    assert summary["run_budget"]["requests_sent"] == {"used": 100, "limit": 100, "reached": True}
    assert summary["run_budget"]["search_requests_sent"]["reached"] is False
    assert summary["run_search_responses_reused"] == {"SAVED": 5}
    assert summary["run_outcome_summaries"] == {"discovery_outcomes": {"X": 1}}


def test_builders_never_mutate_their_inputs():
    eligible, ranking = (
        [eligible_row("10")],
        [ranked_row("10", "EXCLUDED_NOT_VERIFIED_MAPPED", "UNMAPPED")],
    )
    jobs = [job("TGT-1", "pcx")]
    before = copy.deepcopy((eligible, ranking, jobs))

    pd_.build_eligibility_variations(eligible, ranking, {})
    pd_.build_mapping_exclusions(ranking, [], [], {}, [])
    pd_.build_search_coverage(jobs)

    assert (eligible, ranking, jobs) == before


# Plan and the script ---------------------------------------------------------------------------


def test_the_plan_marks_new_tabs_as_needing_approval_and_writes_nothing():
    plan = pd_.publication_plan(
        {
            "P1_ELIGIBLE_RANKING": ("phase1/eligible_model_ranking.csv", 3, ["rank"]),
            "P1_SEARCH_COVERAGE": ("search_coverage.csv", 4, ["target_id"]),
        },
        {"P1_ELIGIBLE_RANKING"},
    )

    assert [p["write_status"] for p in plan] == ["PREVIEW_ONLY_NOT_WRITTEN"] * 2
    assert plan[0]["allowed_by_sheets_writer"] is True
    assert plan[1]["allowed_by_sheets_writer"] is False
    assert "ALLOWED_OUTPUT_TABS" in plan[1]["before_any_write"]


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = columns or list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def script(monkeypatch, tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    try:
        import build_publication_diagnostics as module
    finally:
        sys.path.remove(str(root / "scripts"))
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def make_run(tmp_path: Path) -> tuple[Path, Path]:
    run = tmp_path / "data" / "output" / "run"
    discovery = tmp_path / "data" / "output" / "weekly_runs" / "R1" / "discovery"
    inputs, variations = run / "inputs", run / "shopee_variations"
    write_csv(inputs / "eligible_model_ranking.csv", [eligible_row("10"), eligible_row("11")])
    write_csv(
        inputs / "eligibility_review.csv",
        [
            {
                "model_id": "20",
                "shop_id": "1",
                "item_name": "x",
                "eligibility_status": "REVIEW",
                "eligibility_reason": "r",
            }
        ],
    )
    write_csv(
        inputs / "excluded_listings.csv",
        [
            {
                "model_id": "21",
                "shop_id": "1",
                "item_name": "y",
                "eligibility_status": "EXCLUDED",
                "eligibility_reason": "e",
                "matched_exclude_rule": "camera",
            }
        ],
    )
    write_csv(
        inputs / "source_shop_names.csv",
        [{"source_key": "1|2|10", "shop_id": "1", "shop_name": "Shop"}],
    )
    write_csv(
        variations / "shopee_variation_ranking.csv",
        [
            ranked_row("10", "SELECTED", target_id="TGT-1"),
            ranked_row("11", "EXCLUDED_NOT_VERIFIED_MAPPED", "UNMAPPED"),
        ],
    )
    write_csv(
        variations / "mapping_exclusions.csv",
        [{"comparison_row_id": "SV-1-2-11", "suggested_action": "Research."}],
    )
    (variations / "selection_audit.json").write_text(json.dumps({"representative_exclusions": {}}))
    write_csv(
        discovery / "search_jobs.csv", [job("TGT-1", "pcx", terminal_outcome="FUTURE_OUTCOME")]
    )
    return run, discovery


def digests(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_the_script_writes_only_into_its_output_dir_and_leaves_every_input_alone(
    script, tmp_path, capsys
):
    run, discovery = make_run(tmp_path)
    before = digests(tmp_path)
    out = run / "diagnostics"

    status = script.main(["--run-dir", str(run), "--discovery-dir", str(discovery)])

    assert status == 0
    after = digests(tmp_path)
    created = sorted(set(after) - set(before))
    assert created and all(
        Path(name).parts[:4] == ("data", "output", "run", "diagnostics") for name in created
    )
    assert {k: v for k, v in after.items() if k in before} == before  # no input changed
    assert {p.name for p in out.iterdir()} == {
        "README.md",
        "diagnostics_summary.json",
        "eligibility_not_eligible_listings.csv",
        "eligibility_variations.csv",
        "mapping_exclusions_diagnostic.csv",
        "publication_plan.json",
        "search_coverage.csv",
    }
    plan = json.loads((out / "publication_plan.json").read_text())
    assert plan["mode"] == "PREVIEW_ONLY"
    assert {t["tab"]: t["rows"] for t in plan["tabs"]}["P1_SEARCH_COVERAGE"] == 1
    summary = json.loads((out / "diagnostics_summary.json").read_text())
    assert summary["checks_passed"] is True
    assert summary["search_coverage"]["terminal_outcomes"] == {"FUTURE_OUTCOME": 1}
    assert "Preview only" in capsys.readouterr().out


def test_the_script_dry_run_writes_nothing(script, tmp_path):
    run, discovery = make_run(tmp_path)
    before = digests(tmp_path)

    status = script.main(["--run-dir", str(run), "--discovery-dir", str(discovery), "--dry-run"])

    assert status == 0 and digests(tmp_path) == before


def test_the_script_refuses_an_output_folder_outside_data_output(script, tmp_path, capsys):
    run, discovery = make_run(tmp_path)

    status = script.main(
        [
            "--run-dir",
            str(run),
            "--discovery-dir",
            str(discovery),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert status == 1 and not (tmp_path / "out").exists()
    assert "Output must be under" in capsys.readouterr().err
