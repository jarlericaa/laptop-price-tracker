import pandas as pd

from laptop_tracker.data_quality import DUPLICATE_DECISION_COLUMNS
from laptop_tracker.duplicate_policy import (
    REVIEWER,
    merge_decisions,
    propose_latest_row_decisions,
)

KEY = {"period_month": "2026-01", "shop_id": "1", "item_id": "2"}


def _review(model_id, fingerprints, dates="2026-01-01", decision=""):
    return {
        **KEY,
        "model_id": model_id,
        "conflict_dates": dates,
        "differing_fields": "total_orders",
        "record_fingerprints": ";".join(fingerprints),
        "fingerprint_version": "content-v1",
        "decision": decision,
    }


def _log(model_id, fingerprint, row):
    return {
        **KEY,
        "model_id": model_id,
        "record_fingerprint": fingerprint,
        "current_source_row_number": str(row),
    }


def test_keeps_record_on_latest_row_regardless_of_fingerprint_order():
    review = pd.DataFrame([_review("m1", ["aaa", "zzz"])])
    log = pd.DataFrame([_log("m1", "zzz", 10), _log("m1", "aaa", 11)])
    decisions, skipped = propose_latest_row_decisions(review, log, "2026-11-03")
    assert skipped.empty
    row = decisions.iloc[0]
    assert row["decision"] == "KEEP_RECORD"
    assert row["selected_record_fingerprint"] == "aaa"
    assert row["reviewer"] == REVIEWER
    assert row["reviewed_at"] == "2026-11-03"


def test_three_records_pick_highest_row():
    review = pd.DataFrame([_review("m1", ["a", "b", "c"])])
    log = pd.DataFrame([_log("m1", "a", 5), _log("m1", "b", 99), _log("m1", "c", 7)])
    decisions, _ = propose_latest_row_decisions(review, log, "2026-11-03")
    assert decisions.iloc[0]["selected_record_fingerprint"] == "b"


def test_already_decided_keys_are_left_alone():
    review = pd.DataFrame([_review("m1", ["a", "b"], decision="KEEP_RECORD")])
    log = pd.DataFrame([_log("m1", "a", 1), _log("m1", "b", 2)])
    decisions, skipped = propose_latest_row_decisions(review, log, "2026-11-03")
    assert decisions.empty and skipped.empty


def test_several_conflict_dates_or_missing_records_are_not_decided():
    review = pd.DataFrame(
        [
            _review("m1", ["a", "b"], dates="2026-01-01;2026-01-02"),
            _review("m2", ["c", "d"]),
        ]
    )
    log = pd.DataFrame([_log("m1", "a", 1), _log("m1", "b", 2), _log("m2", "c", 3)])
    decisions, skipped = propose_latest_row_decisions(review, log, "2026-11-03")
    assert decisions.empty
    assert sorted(skipped["model_id"]) == ["m1", "m2"]


def test_merge_replaces_invalidated_decision_for_the_same_key_only():
    def decision(model_id, selected):
        return {column: "" for column in DUPLICATE_DECISION_COLUMNS} | {
            **KEY,
            "model_id": model_id,
            "decision": "KEEP_RECORD",
            "selected_record_fingerprint": selected,
        }

    existing = pd.DataFrame([decision("m1", "old"), decision("m2", "keep")])
    proposed = pd.DataFrame([decision("m1", "new")])
    merged = merge_decisions(existing, proposed)
    selected = zip(merged["model_id"], merged["selected_record_fingerprint"], strict=True)
    assert dict(selected) == {
        "m1": "new",
        "m2": "keep",
    }
