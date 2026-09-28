from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from conftest import raw_row, raw_values, settings

from laptop_tracker.data_quality import (
    DUPLICATE_DECISION_COLUMNS,
    collapse_duplicates,
    load_duplicate_decisions,
    validate_rows,
)
from laptop_tracker.pipeline import PipelineSafetyError, process_raw_values
from laptop_tracker.schema import MissingColumnsError, normalize_raw_values


def test_missing_columns_stop_the_run() -> None:
    with pytest.raises(MissingColumnsError, match="Missing required"):
        normalize_raw_values([["grass_date", "shopid"], ["2026-09-01", "1"]], "run")


def test_ids_and_numeric_looking_model_names_remain_exact_strings() -> None:
    frame = normalize_raw_values(
        raw_values(
            raw_row(shopid="000012345678901234", itemid="0002", modelid="0003", model_name="12345")
        ),
        "run",
    )
    assert frame.loc[0, "shop_id"] == "000012345678901234"
    assert frame.loc[0, "model_id"] == "0003"
    assert frame.loc[0, "shopee_model_name"] == "12345"


def test_zero_gmv_is_valid() -> None:
    result = validate_rows(normalize_raw_values(raw_values(raw_row(gmv_usd="0")), "run"))
    assert result.errors.empty
    assert result.valid_rows.loc[0, "_gmv_numeric"] == 0


def test_exact_duplicates_are_collapsed() -> None:
    row = raw_row()
    result = validate_rows(normalize_raw_values(raw_values(row, row.copy()), "run"))
    clean, conflicts = collapse_duplicates(result.valid_rows)
    assert len(clean) == 1
    assert clean.loc[0, "duplicate_status"] == "EXACT_DUPLICATE"
    assert clean.loc[0, "duplicate_count"] == 2
    assert conflicts.empty


def test_conflicting_duplicates_are_reported() -> None:
    result = validate_rows(
        normalize_raw_values(raw_values(raw_row(), raw_row(gmv_usd="700")), "run")
    )
    clean, conflicts = collapse_duplicates(result.valid_rows)
    assert clean.empty
    assert len(conflicts) == 1
    assert conflicts.loc[0, "resolution_status"] == "EXCLUDED"
    assert "total_gmv_usd" in conflicts.loc[0, "differing_fields"]


def test_all_invalid_input_still_writes_validation_diagnostics(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No valid monthly periods"):
        process_raw_values(
            raw_values(raw_row(gmv_usd="not-a-number")),
            settings(),
            output_dir=tmp_path,
            check_freshness=False,
        )
    report = (tmp_path / "validation_errors.csv").read_text(encoding="utf-8")
    assert "INVALID_NUMERIC" in report
    assert (tmp_path / "conflicting_duplicates.csv").is_file()


# Two rows sharing a composite key that differ only in orders, GMV, and amount — the shape
# of the conflicts found in the live dry run. An unrelated model keeps the ranking non-empty.
CONFLICT_ROWS = [
    raw_row(total_orders="1", gmv_usd="500", amount="500"),
    raw_row(modelid="301", model_name="ExampleBook 15"),
    raw_row(total_orders="3", gmv_usd="1500", amount="1500"),
]


def run_with_decisions(tmp_path: Path, decisions: list[dict[str, str]] | None = None):
    path = tmp_path / "duplicate_decisions.csv"
    pd.DataFrame(decisions or [], columns=DUPLICATE_DECISION_COLUMNS).to_csv(path, index=False)
    return process_raw_values(
        raw_values(*CONFLICT_ROWS),
        settings(),
        output_dir=tmp_path / "out",
        check_freshness=False,
        fail_on_conflicts=True,
        duplicate_decisions_path=path,
    )


def review_row(tmp_path: Path) -> dict[str, str]:
    with pytest.raises(PipelineSafetyError, match="publication is blocked"):
        run_with_decisions(tmp_path)
    review = pd.read_csv(
        tmp_path / "out" / "duplicate_review.csv", dtype=str, keep_default_na=False
    )
    assert len(review) == 1
    return review.iloc[0].to_dict()


def decided(template: dict[str, str], **fields: str) -> dict[str, str]:
    return template | {"reviewer": "analyst", "reviewed_at": "2026-09-28", "reason": "ok"} | fields


def test_undecided_conflict_blocks_and_writes_redacted_review_template(tmp_path: Path) -> None:
    template = review_row(tmp_path)
    assert list(template) == DUPLICATE_DECISION_COLUMNS
    assert template["differing_fields"] == "total_orders;total_gmv_usd;amount"
    assert template["source_row_numbers"] == "2;4"
    assert template["decision"] == ""
    assert len(template["conflict_fingerprint"]) == 16
    text = (tmp_path / "out" / "duplicate_review.csv").read_text(encoding="utf-8")
    for business_value in ("1500", "29999", "Example Shop", "Example Laptop 14"):
        assert business_value not in text
    conflicts = pd.read_csv(tmp_path / "out" / "conflicting_duplicates.csv", dtype=str)
    assert conflicts["resolution_status"].tolist() == ["EXCLUDED"]


def test_recorded_exclusion_keeps_key_quarantined_and_unblocks(tmp_path: Path) -> None:
    template = review_row(tmp_path)
    result = run_with_decisions(tmp_path, [decided(template, decision="EXCLUDE_KEY")])
    conflicts = result.outputs["P1_CONFLICTING_DUPLICATES"]
    assert conflicts["resolution_status"].tolist() == ["EXCLUDED_BY_DECISION"]
    assert result.outputs["P1_ELIGIBLE_RANKING"]["model_id"].tolist() == ["301"]


def test_recorded_keep_retains_only_the_chosen_row_without_summing(tmp_path: Path) -> None:
    template = review_row(tmp_path)
    result = run_with_decisions(
        tmp_path, [decided(template, decision="KEEP_SOURCE_ROW", keep_source_row_number="4")]
    )
    ranking = result.outputs["P1_ELIGIBLE_RANKING"].set_index("model_id")
    assert ranking.loc["300", "mtd_gmv_usd"] == 1500
    conflicts = result.outputs["P1_CONFLICTING_DUPLICATES"]
    assert conflicts["resolution_status"].tolist() == ["RESOLVED_BY_DECISION"]
    assert result.summary["conflicting_duplicate_keys"] == 1


def test_stale_or_invalid_decisions_do_not_unblock(tmp_path: Path) -> None:
    template = review_row(tmp_path)
    for decision in (
        decided(template, decision="EXCLUDE_KEY", conflict_fingerprint="0" * 16),
        decided(template, decision="KEEP_SOURCE_ROW", keep_source_row_number="3"),
    ):
        with pytest.raises(PipelineSafetyError, match="publication is blocked"):
            run_with_decisions(tmp_path, [decision])
        report = pd.read_csv(tmp_path / "out" / "conflicting_duplicates.csv", dtype=str)
        assert report["resolution_status"].tolist() == ["EXCLUDED"]
        assert "re-review required" in report.loc[0, "notes"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"decision": "KEEP_LATEST"}, "Unsupported duplicate decisions"),
        ({"decision": "EXCLUDE_KEY", "reviewer": ""}, "require"),
        ({"decision": "KEEP_SOURCE_ROW"}, "keep_source_row_number"),
        ({"decision": "EXCLUDE_KEY", "keep_source_row_number": "2"}, "keep_source_row_number"),
    ],
)
def test_malformed_duplicate_decisions_are_rejected(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    row = decided(dict.fromkeys(DUPLICATE_DECISION_COLUMNS, "1")) | {
        "keep_source_row_number": ""
    }
    path = tmp_path / "decisions.csv"
    pd.DataFrame([row | overrides], columns=DUPLICATE_DECISION_COLUMNS).to_csv(path, index=False)
    with pytest.raises(ValueError, match=message):
        load_duplicate_decisions(path)


def test_conflicting_decisions_for_one_key_are_rejected(tmp_path: Path) -> None:
    row = decided(dict.fromkeys(DUPLICATE_DECISION_COLUMNS, "1")) | {
        "decision": "EXCLUDE_KEY",
        "keep_source_row_number": "",
    }
    path = tmp_path / "decisions.csv"
    pd.DataFrame([row, row | {"reason": "other"}], columns=DUPLICATE_DECISION_COLUMNS).to_csv(
        path, index=False
    )
    with pytest.raises(ValueError, match="only one recorded decision"):
        load_duplicate_decisions(path)


def test_model_identity_conflicts_still_block_publication(tmp_path: Path) -> None:
    with pytest.raises(PipelineSafetyError, match="publication is blocked"):
        process_raw_values(
            raw_values(raw_row(), raw_row(shopid="101")),
            settings(),
            output_dir=tmp_path,
            check_freshness=False,
            fail_on_conflicts=True,
            duplicate_decisions_path=tmp_path / "missing.csv",
        )


