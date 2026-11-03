from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from conftest import raw_row, raw_values, settings

from laptop_tracker.data_quality import (
    DUPLICATE_DECISION_COLUMNS,
    FINGERPRINT_VERSION,
    LEGACY_DECISION_COLUMNS,
    collapse_duplicates,
    legacy_conflict_fingerprint,
    load_duplicate_decisions,
    migrate_legacy_decisions,
    record_fingerprint,
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


def run_with_decisions(
    tmp_path: Path,
    decisions: list[dict[str, str]] | None = None,
    rows: list[list[str]] | None = None,
):
    path = tmp_path / "duplicate_decisions.csv"
    pd.DataFrame(decisions or [], columns=DUPLICATE_DECISION_COLUMNS).to_csv(path, index=False)
    return process_raw_values(
        raw_values(*(rows if rows is not None else CONFLICT_ROWS)),
        settings(),
        output_dir=tmp_path / "out",
        check_freshness=False,
        fail_on_conflicts=True,
        duplicate_decisions_path=path,
    )


def review_row(tmp_path: Path, rows: list[list[str]] | None = None) -> dict[str, str]:
    with pytest.raises(PipelineSafetyError, match="publication is blocked"):
        run_with_decisions(tmp_path, rows=rows)
    review = pd.read_csv(
        tmp_path / "out" / "duplicate_review.csv", dtype=str, keep_default_na=False
    )
    assert len(review) == 1
    return review.iloc[0].to_dict()


def resolution_log(tmp_path: Path) -> pd.DataFrame:
    return pd.read_csv(
        tmp_path / "out" / "duplicate_resolution_log.csv", dtype=str, keep_default_na=False
    )


def fingerprint_at(tmp_path: Path, row_number: int) -> str:
    log = resolution_log(tmp_path)
    return log.loc[log["current_source_row_number"].eq(str(row_number)), "record_fingerprint"].iloc[
        0
    ]


def decided(template: dict[str, str], **fields: str) -> dict[str, str]:
    return template | {"reviewer": "analyst", "reviewed_at": "2026-09-28", "reason": "ok"} | fields


def keep_decision(tmp_path: Path, row_number: int) -> dict[str, str]:
    """A KEEP_RECORD decision selecting the record that sits on `row_number` in CONFLICT_ROWS."""
    template = review_row(tmp_path)
    return decided(
        template,
        decision="KEEP_RECORD",
        selected_record_fingerprint=fingerprint_at(tmp_path, row_number),
    )


def test_undecided_conflict_blocks_and_writes_redacted_review_template(tmp_path: Path) -> None:
    template = review_row(tmp_path)
    assert list(template) == DUPLICATE_DECISION_COLUMNS
    assert template["differing_fields"] == "total_orders;total_gmv_usd;amount"
    assert template["conflict_dates"] == "2026-09-01"
    assert template["fingerprint_version"] == FINGERPRINT_VERSION
    assert template["decision"] == ""
    fingerprints = template["record_fingerprints"].split(";")
    assert len(fingerprints) == 2 and all(len(item) == 24 for item in fingerprints)
    # Decisions carry no sheet row numbers: those live only in the run log.
    assert not any("row" in column and "selected" not in column for column in template)
    text = (tmp_path / "out" / "duplicate_review.csv").read_text(encoding="utf-8")
    for business_value in ("1500", "29999", "Example Shop", "Example Laptop 14"):
        assert business_value not in text
    log = resolution_log(tmp_path)
    assert log["current_source_row_number"].tolist() == ["2", "4"]
    assert set(log["record_role"]) == {"UNDECIDED"} and set(log["outcome"]) == {"UNRESOLVED"}
    assert sorted(log["record_fingerprint"]) == sorted(fingerprints)
    conflicts = pd.read_csv(tmp_path / "out" / "conflicting_duplicates.csv", dtype=str)
    assert conflicts["resolution_status"].tolist() == ["EXCLUDED"]


def test_recorded_exclusion_keeps_key_quarantined_and_unblocks(tmp_path: Path) -> None:
    template = review_row(tmp_path)
    result = run_with_decisions(tmp_path, [decided(template, decision="EXCLUDE_KEY")])
    conflicts = result.outputs["P1_CONFLICTING_DUPLICATES"]
    assert conflicts["resolution_status"].tolist() == ["EXCLUDED_BY_DECISION"]
    assert result.outputs["P1_ELIGIBLE_RANKING"]["model_id"].tolist() == ["301"]
    assert set(resolution_log(tmp_path)["record_role"]) == {"EXCLUDED"}


def test_recorded_keep_retains_only_the_chosen_record_without_summing(tmp_path: Path) -> None:
    decision = keep_decision(tmp_path, row_number=4)
    result = run_with_decisions(tmp_path, [decision])
    ranking = result.outputs["P1_ELIGIBLE_RANKING"].set_index("model_id")
    assert ranking.loc["300", "mtd_gmv_usd"] == 1500
    conflicts = result.outputs["P1_CONFLICTING_DUPLICATES"]
    assert conflicts["resolution_status"].tolist() == ["RESOLVED_BY_DECISION"]
    assert result.summary["conflicting_duplicate_keys"] == 1
    log = resolution_log(tmp_path).set_index("current_source_row_number")
    assert log.loc["4", "record_role"] == "SELECTED"
    assert log.loc["2", "record_role"] == "NOT_SELECTED"


def test_reordering_and_shifting_rows_preserves_the_decision(tmp_path: Path) -> None:
    decision = keep_decision(tmp_path, row_number=4)  # the 1500 GMV record
    # Same records, new sheet positions: the pair is swapped and unrelated rows are inserted.
    moved = [
        raw_row(modelid="302", model_name="ExampleBook 16"),
        CONFLICT_ROWS[2],
        raw_row(modelid="303", model_name="ExampleBook 17"),
        CONFLICT_ROWS[1],
        CONFLICT_ROWS[0],
    ]
    result = run_with_decisions(tmp_path, [decision], rows=moved)
    ranking = result.outputs["P1_ELIGIBLE_RANKING"].set_index("model_id")
    assert ranking.loc["300", "mtd_gmv_usd"] == 1500  # the same record wins, not the later row
    log = resolution_log(tmp_path).set_index("current_source_row_number")
    assert sorted(log.index) == ["3", "6"]
    assert log.loc["3", "record_role"] == "SELECTED"  # now the earlier of the two rows


def test_changed_selected_record_values_require_re_review(tmp_path: Path) -> None:
    decision = keep_decision(tmp_path, row_number=4)
    changed = raw_row(total_orders="3", gmv_usd="1500.5", amount="1500")
    edited = [CONFLICT_ROWS[0], CONFLICT_ROWS[1], changed]
    with pytest.raises(PipelineSafetyError, match="publication is blocked"):
        run_with_decisions(tmp_path, [decision], rows=edited)
    report = pd.read_csv(tmp_path / "out" / "conflicting_duplicates.csv", dtype=str)
    assert report["resolution_status"].tolist() == ["EXCLUDED"]
    assert "re-review required" in report.loc[0, "notes"]
    assert set(resolution_log(tmp_path)["outcome"]) == {"DECISION_INVALIDATED"}


def test_changed_unselected_record_invalidates_even_though_the_winner_still_exists(
    tmp_path: Path,
) -> None:
    decision = keep_decision(tmp_path, row_number=4)
    edited = [raw_row(total_orders="1", gmv_usd="505", amount="500"), *CONFLICT_ROWS[1:]]
    with pytest.raises(PipelineSafetyError, match="publication is blocked"):
        run_with_decisions(tmp_path, [decision], rows=edited)
    assert "changed" in pd.read_csv(tmp_path / "out" / "conflicting_duplicates.csv", dtype=str).loc[
        0, "notes"
    ]


def test_a_newly_introduced_conflicting_record_requires_review(tmp_path: Path) -> None:
    decision = keep_decision(tmp_path, row_number=4)
    third = raw_row(total_orders="2", gmv_usd="900", amount="900")
    with pytest.raises(PipelineSafetyError, match="publication is blocked"):
        run_with_decisions(tmp_path, [decision], rows=[*CONFLICT_ROWS, third])
    log = resolution_log(tmp_path)
    assert len(log) == 3 and set(log["outcome"]) == {"DECISION_INVALIDATED"}


def test_a_removed_conflicting_record_is_no_longer_a_conflict(tmp_path: Path) -> None:
    # Nothing to decide when only one record remains: the key is simply unique again.
    result = run_with_decisions(tmp_path, rows=[CONFLICT_ROWS[0], CONFLICT_ROWS[1]])
    assert result.summary["conflicting_duplicate_keys"] == 0


def test_unsupported_fingerprint_version_does_not_unblock(tmp_path: Path) -> None:
    decision = keep_decision(tmp_path, row_number=4) | {"fingerprint_version": "content-v0"}
    with pytest.raises(PipelineSafetyError, match="publication is blocked"):
        run_with_decisions(tmp_path, [decision])
    notes = pd.read_csv(tmp_path / "out" / "conflicting_duplicates.csv", dtype=str).loc[0, "notes"]
    assert "content-v0" in notes and "re-review required" in notes


def test_record_fingerprints_ignore_row_numbers_and_keep_exact_text() -> None:
    def fingerprints(*rows: list[str]) -> list[str]:
        frame = normalize_raw_values(raw_values(*rows), "run")
        return [record_fingerprint(row) for _, row in frame.iterrows()]

    first, second = fingerprints(raw_row(), raw_row())
    assert first == second  # identical content, different rows
    base = fingerprints(raw_row(gmv_usd="1500.10"))[0]
    assert base != fingerprints(raw_row(gmv_usd="1500.1"))[0]  # precision text is significant
    assert base != fingerprints(raw_row(gmv_usd="1500.10", itemid="0200"))[0]  # exact identifiers
    long_id = "000012345678901234"
    assert (
        fingerprints(raw_row(shopid=long_id))[0]
        != fingerprints(raw_row(shopid="12345678901234"))[0]
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"decision": "KEEP_LATEST"}, "Unsupported duplicate decisions"),
        ({"decision": "EXCLUDE_KEY", "reviewer": ""}, "require"),
        ({"decision": "KEEP_RECORD", "selected_record_fingerprint": ""}, "selected_record"),
        ({"decision": "EXCLUDE_KEY", "selected_record_fingerprint": "a"}, "selected_record"),
        ({"decision": "KEEP_RECORD", "selected_record_fingerprint": "c"}, "one of the reviewed"),
    ],
)
def test_malformed_duplicate_decisions_are_rejected(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    row = decided(dict.fromkeys(DUPLICATE_DECISION_COLUMNS, "1")) | {
        "record_fingerprints": "a;b",
        "selected_record_fingerprint": "",
    }
    path = tmp_path / "decisions.csv"
    pd.DataFrame([row | overrides], columns=DUPLICATE_DECISION_COLUMNS).to_csv(path, index=False)
    with pytest.raises(ValueError, match=message):
        load_duplicate_decisions(path)


def test_conflicting_decisions_for_one_key_are_rejected(tmp_path: Path) -> None:
    row = decided(dict.fromkeys(DUPLICATE_DECISION_COLUMNS, "1")) | {
        "decision": "EXCLUDE_KEY",
        "selected_record_fingerprint": "",
    }
    path = tmp_path / "decisions.csv"
    pd.DataFrame([row, row | {"reason": "other"}], columns=DUPLICATE_DECISION_COLUMNS).to_csv(
        path, index=False
    )
    with pytest.raises(ValueError, match="only one recorded decision"):
        load_duplicate_decisions(path)


def test_retired_row_number_layout_is_refused_with_migration_guidance(tmp_path: Path) -> None:
    path = tmp_path / "legacy.csv"
    legacy = dict.fromkeys(LEGACY_DECISION_COLUMNS, "1") | {"decision": "KEEP_SOURCE_ROW"}
    pd.DataFrame([legacy], columns=LEGACY_DECISION_COLUMNS).to_csv(path, index=False)
    with pytest.raises(ValueError, match="migrate_duplicate_decisions"):
        load_duplicate_decisions(path)
    pd.DataFrame([], columns=LEGACY_DECISION_COLUMNS).to_csv(path, index=False)
    assert load_duplicate_decisions(path).empty  # a header-only legacy file holds no decisions


# Migration of recorded row-number decisions


def legacy_decision(rows: list[list[str]], keep_row: int | None, **fields: str) -> pd.DataFrame:
    """A recorded legacy decision for the conflict in `rows`, as the old pipeline wrote it."""
    frame = normalize_raw_values(raw_values(*rows), "run")
    valid = validate_rows(frame).valid_rows
    conflict = valid.loc[valid["model_id"].eq("300")].sort_values("source_row_number")
    record = {
        "period_month": "2026-09",
        "shop_id": "100",
        "item_id": "200",
        "model_id": "300",
        "duplicate_count": str(len(conflict)),
        "differing_fields": "total_orders;total_gmv_usd;amount",
        "source_row_numbers": ";".join(conflict["source_row_number"].astype(str)),
        "conflict_fingerprint": legacy_conflict_fingerprint(conflict),
        "decision": "KEEP_SOURCE_ROW" if keep_row is not None else "EXCLUDE_KEY",
        "keep_source_row_number": "" if keep_row is None else str(keep_row),
        "reviewer": "JG Ricaforte",
        "reviewed_at": "2026-10-22",
        "reason": "owner rule",
    } | fields
    return pd.DataFrame([record], columns=LEGACY_DECISION_COLUMNS)


def valid_rows_of(rows: list[list[str]]) -> pd.DataFrame:
    return validate_rows(normalize_raw_values(raw_values(*rows), "run")).valid_rows


def fingerprint_of_gmv(rows: list[list[str]], gmv: str) -> str:
    valid = valid_rows_of(rows)
    match = valid.loc[valid["total_gmv_usd"].eq(gmv)].iloc[0]
    return record_fingerprint(match)


def test_migration_follows_the_reviewed_record_when_only_row_numbers_moved() -> None:
    legacy = legacy_decision(CONFLICT_ROWS, keep_row=4)  # reviewed: 1500 GMV on row 4
    moved = [CONFLICT_ROWS[2], CONFLICT_ROWS[1], CONFLICT_ROWS[0]]  # now 1500 is the EARLIER row
    migrated, report = migrate_legacy_decisions(legacy, valid_rows_of(moved), note="Migrated.")
    # The current records are placed on the old row numbers to reproduce the stored fingerprint,
    # so the sheet's present row order does not matter; the winner is followed by content.
    assert report["outcome"].tolist() == ["MIGRATED"]
    row = migrated.iloc[0]
    assert row["decision"] == "KEEP_RECORD"
    assert row["selected_record_fingerprint"] == fingerprint_of_gmv(moved, "1500")
    assert (row["reviewer"], row["reviewed_at"]) == ("JG Ricaforte", "2026-10-22")
    assert row["reason"] == "owner rule Migrated."
    assert row["fingerprint_version"] == FINGERPRINT_VERSION


def test_migrated_decision_survives_a_later_reorder(tmp_path: Path) -> None:
    legacy = legacy_decision(CONFLICT_ROWS, keep_row=2)  # reviewed: the 500 GMV record, row 2
    migrated, _ = migrate_legacy_decisions(legacy, valid_rows_of(CONFLICT_ROWS))
    moved = [CONFLICT_ROWS[2], CONFLICT_ROWS[1], CONFLICT_ROWS[0]]
    result = run_with_decisions(tmp_path, migrated.to_dict("records"), rows=moved)
    ranking = result.outputs["P1_ELIGIBLE_RANKING"].set_index("model_id")
    assert ranking.loc["300", "mtd_gmv_usd"] == 500  # the previously selected record, not "later"


def test_migration_refuses_when_reviewed_values_changed() -> None:
    legacy = legacy_decision(CONFLICT_ROWS, keep_row=4)
    changed = raw_row(total_orders="3", gmv_usd="1500.5", amount="1500")
    edited = [CONFLICT_ROWS[0], CONFLICT_ROWS[1], changed]
    migrated, report = migrate_legacy_decisions(legacy, valid_rows_of(edited))
    assert migrated.empty and report["outcome"].tolist() == ["NOT_MIGRATED"]
    assert "cannot be established" in report.loc[0, "detail"]


def test_migration_refuses_when_the_selected_row_was_not_reviewed() -> None:
    legacy = legacy_decision(CONFLICT_ROWS, keep_row=4, keep_source_row_number="9")
    migrated, report = migrate_legacy_decisions(legacy, valid_rows_of(CONFLICT_ROWS))
    assert migrated.empty and "not one of the reviewed rows" in report.loc[0, "detail"]


def test_migration_carries_exclusions_and_reports_keys_that_are_no_longer_conflicts() -> None:
    legacy = pd.concat(
        [
            legacy_decision(CONFLICT_ROWS, keep_row=None),
            legacy_decision(CONFLICT_ROWS, keep_row=None, model_id="999"),
        ],
        ignore_index=True,
    )
    migrated, report = migrate_legacy_decisions(legacy, valid_rows_of(CONFLICT_ROWS))
    assert report["outcome"].tolist() == ["MIGRATED", "NOT_MIGRATED"]
    assert migrated["decision"].tolist() == ["EXCLUDE_KEY"]
    assert migrated["selected_record_fingerprint"].tolist() == [""]


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


