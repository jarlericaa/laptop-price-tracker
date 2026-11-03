from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from conftest import raw_row, raw_values, settings

from laptop_tracker.data_quality import validate_rows
from laptop_tracker.eligibility import apply_eligibility, load_manual_decisions, load_rules
from laptop_tracker.pipeline import process_raw_values
from laptop_tracker.ranking import aggregate_models, rank_eligible_models
from laptop_tracker.schema import normalize_raw_values
from laptop_tracker.sheets_writer import assert_safe_output_tab

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=ZoneInfo("Asia/Manila"))


def run(rows: list[list[str]], tmp_path: Path):
    return process_raw_values(
        raw_values(*rows), settings(), output_dir=tmp_path, now=NOW, check_freshness=False
    )


def test_m_minus_1_only_models_do_not_qualify_and_mtd_models_do(tmp_path: Path) -> None:
    result = run(
        [
            raw_row(grass_date="2026-08-01", modelid="1", itemid="1"),
            raw_row(grass_date="2026-09-01", modelid="2", itemid="2"),
        ],
        tmp_path,
    )
    ranking = result.outputs["P1_ELIGIBLE_RANKING"]
    assert ranking["model_id"].tolist() == ["2"]
    assert ranking.loc[0, "present_in_mtd"]


def test_gmv_math_and_deterministic_order(tmp_path: Path) -> None:
    rows = [
        raw_row(grass_date="2026-08-01", modelid="10", itemid="10", gmv_usd="20"),
        raw_row(grass_date="2026-09-01", modelid="10", itemid="10", gmv_usd="30"),
        raw_row(grass_date="2026-09-01", modelid="2", itemid="2", gmv_usd="50"),
        raw_row(grass_date="2026-09-01", modelid="3", itemid="3", gmv_usd="50"),
    ]
    result = run(rows, tmp_path)
    ranking = result.outputs["P1_ELIGIBLE_RANKING"]
    assert ranking.loc[ranking.model_id.eq("10"), "ranking_gmv_usd"].iloc[0] == 50
    assert ranking["model_id"].tolist() == ["2", "3", "10"]


def test_many_model_aggregation_is_complete_fast_and_deterministic() -> None:
    model_count = 12_000
    mtd_model_count = 11_000
    rows: list[dict[str, object]] = []
    source_row_number = 2
    for index in range(model_count):
        model_id = f"{index:05d}"
        m1_gmv = 20.0 if index == 10 else 15.0 if index in {20, 30, 40} else 1.0
        m1_status = "EXCLUDED" if index == 50 else "REVIEW" if index == 51 else "ELIGIBLE"
        m1_reason = (
            "Product-level exclusion rule matched."
            if index == 50
            else "Manual review required."
            if index == 51
            else "Inclusion rule matched."
        )
        rows.append(
            {
                "source_row_number": source_row_number,
                "run_id": "local-test",
                "period_month": "2026-08",
                "source_key": f"1|{index}|{model_id}",
                "shop_id": "1",
                "item_id": str(index),
                "model_id": model_id,
                "item_name": "M-1 laptop",
                "shopee_model_name": f"Model {model_id}",
                "_gmv_numeric": m1_gmv,
                "_price_numeric": 900.0,
                "eligibility_status": m1_status,
                "eligibility_reason": m1_reason,
                "matched_include_rule": "laptop",
                "matched_exclude_rule": "",
            }
        )
        source_row_number += 1
        if index < mtd_model_count:
            mtd_status = (
                "EXCLUDED" if index == 50 else "REVIEW" if index == 51 else "ELIGIBLE"
            )
            mtd_reason = (
                "Product-level exclusion rule matched."
                if index == 50
                else "Manual review required."
                if index == 51
                else "Inclusion rule matched."
            )
            if index == 52:
                mtd_status = "EXCLUDED"
                mtd_reason = "Product-level exclusion rule matched."
            rows.append(
                {
                    "source_row_number": source_row_number,
                    "run_id": "local-test",
                    "period_month": "2026-09",
                    "source_key": f"1|{index}|{model_id}",
                    "shop_id": "1",
                    "item_id": str(index),
                    "model_id": model_id,
                    "item_name": "MTD laptop",
                    "shopee_model_name": f"Model {model_id}",
                    "_gmv_numeric": 20.0 if index == 20 else 10.0,
                    "_price_numeric": 1_000.0,
                    "eligibility_status": mtd_status,
                    "eligibility_reason": mtd_reason,
                    "matched_include_rule": "laptop",
                    "matched_exclude_rule": "charger" if index == 50 else "",
                }
            )
            source_row_number += 1

    frame = pd.DataFrame(rows)
    started = time.perf_counter()
    summary = aggregate_models(frame, "2026-08", "2026-09", "2026-09-22")
    ranking = rank_eligible_models(summary)
    elapsed = time.perf_counter() - started

    assert len(summary) == model_count
    assert summary["model_id"].tolist() == [f"{index:05d}" for index in range(model_count)]
    assert summary["present_in_mtd"].sum() == mtd_model_count
    assert summary.loc[summary["model_id"].eq("00000"), "item_name"].item() == "MTD laptop"
    assert not summary.loc[summary["model_id"].eq("11999"), "present_in_mtd"].item()
    assert (
        summary["ranking_gmv_usd"]
        == summary["m_minus_1_gmv_usd"] + summary["mtd_gmv_usd"]
    ).all()
    assert summary.loc[summary["model_id"].eq("00050"), "eligibility_status"].item() == "EXCLUDED"
    assert summary.loc[summary["model_id"].eq("00051"), "eligibility_status"].item() == "REVIEW"
    conflict = summary.loc[summary["model_id"].eq("00052")].iloc[0]
    assert conflict["eligibility_status"] == "REVIEW"
    assert conflict["eligibility_reason"] == (
        "Conflicting listing-level eligibility classifications for the same model."
    )
    expected_ids = ["00020", "00010", "00030", "00040"] + [
        f"{index:05d}"
        for index in range(mtd_model_count)
        if index not in {10, 20, 30, 40, 50, 51, 52}
    ]
    assert ranking["model_id"].tolist() == expected_ids
    pd.testing.assert_frame_equal(ranking, rank_eligible_models(summary))
    assert elapsed < 10, f"12,000 model groups took {elapsed:.2f} seconds"


def test_top_100_and_fewer_than_100_are_handled(tmp_path: Path) -> None:
    hundred_rows = [
        raw_row(modelid=str(index), itemid=str(index), gmv_usd=str(index))
        for index in range(1, 101)
    ]
    result = run(hundred_rows, tmp_path)
    assert len(result.outputs["P1_TOP_100_TARGETS"]) == 100
    smaller = run([raw_row(modelid="1", itemid="1")], tmp_path / "smaller")
    assert len(smaller.outputs["P1_TOP_100_TARGETS"]) == 1


def test_mtd_price_is_selected_and_conflicts_are_flagged(tmp_path: Path) -> None:
    selected = run([raw_row(current_price="12345")], tmp_path / "selected")
    row = selected.outputs["P1_ELIGIBLE_RANKING"].iloc[0]
    assert row["shopee_current_price_php"] == 12345
    raw = validate_rows(
        normalize_raw_values(
            raw_values(raw_row(current_price="12345"), raw_row(current_price="13000")), "run"
        )
    ).valid_rows
    root = Path(__file__).resolve().parents[1]
    classified = apply_eligibility(
        raw,
        load_rules(root / "config" / "eligibility_rules.csv"),
        load_manual_decisions(root / "config" / "eligibility_decisions.csv"),
    )
    conflict_row = aggregate_models(classified, "2026-08", "2026-09", "2026-09-22").iloc[0]
    assert conflict_row["price_status"] == "PRICE_CONFLICT"
    assert pd.isna(conflict_row["shopee_current_price_php"])


def test_writer_refuses_raw_and_identical_input_has_identical_ranking(tmp_path: Path) -> None:
    try:
        assert_safe_output_tab("raw")
    except ValueError:
        pass
    else:
        raise AssertionError("raw should never be an output destination")
    rows = [raw_row(), raw_row(modelid="301", itemid="201", gmv_usd="300")]
    first = run(rows, tmp_path / "first").outputs["P1_ELIGIBLE_RANKING"]
    second = run(rows, tmp_path / "second").outputs["P1_ELIGIBLE_RANKING"]
    pd.testing.assert_frame_equal(first, second)


def test_required_local_output_files_are_created(tmp_path: Path) -> None:
    run([raw_row()], tmp_path)
    expected = {
        "top_100_targets.csv",
        "eligible_model_ranking.csv",
        "excluded_listings.csv",
        "eligibility_review.csv",
        "conflicting_duplicates.csv",
        "duplicate_review.csv",
        "duplicate_resolution_log.csv",
        "validation_errors.csv",
        "period_summary.csv",
        "phase1_run_summary.json",
        # Local-only sidecar for the Shopee-variation ranking (seller name per source_key).
        "source_shop_names.csv",
    }
    assert expected == {path.name for path in tmp_path.iterdir()}


def test_all_models_quarantined_produces_safe_empty_outputs(tmp_path: Path) -> None:
    result = run(
        [
            raw_row(modelid="7", itemid="1"),
            raw_row(modelid="7", itemid="2"),
        ],
        tmp_path,
    )
    assert result.outputs["P1_ELIGIBLE_RANKING"].empty
    assert result.outputs["P1_TOP_100_TARGETS"].empty
    conflicts = result.outputs["P1_CONFLICTING_DUPLICATES"]
    assert conflicts["resolution_status"].tolist() == ["MODEL_IDENTITY_CONFLICT"]


def test_the_seller_name_sidecar_prefers_the_mtd_name_and_never_uses_a_blank(
    tmp_path: Path,
) -> None:
    rows = [
        raw_row(grass_date="2026-08-01", shopid="1", itemid="1", modelid="1", shop_name="Old Name"),
        raw_row(grass_date="2026-09-01", shopid="1", itemid="1", modelid="1", shop_name="New Name"),
        raw_row(grass_date="2026-08-01", shopid="2", itemid="2", modelid="2", shop_name="Only M-1"),
        raw_row(grass_date="2026-09-01", shopid="3", itemid="3", modelid="3", shop_name="  "),
    ]
    run(rows, tmp_path)

    names = pd.read_csv(tmp_path / "source_shop_names.csv", dtype=str, keep_default_na=False)
    assert names.columns.tolist() == ["source_key", "shop_id", "shop_name"]
    lookup = dict(zip(names["source_key"], names["shop_name"], strict=True))
    assert lookup["1|1|1"] == "New Name"  # the MTD row wins over M-1
    assert lookup["2|2|2"] == "Only M-1"  # a source without MTD rows falls back to its M-1 name
    assert "3|3|3" not in lookup  # a blank name is never used or invented
