"""Top 100 eligible mapped Shopee variations end to end: builder CLI, written files, audit CLI.

Synthetic data only. The catalog inputs are local CSV snapshots, as in an offline build.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_supporting_tables import map_row, review_row
from test_target_master import target_values
from test_variation_ranking import vs
from test_verified_top import load_script, target, write_rows

from laptop_tracker.catalog_provenance import (
    CHANGED,
    FRESH,
    MANIFEST_NAME,
    NO_RECORD,
    STALE,
    describe_catalog_inputs,
    write_retrieval_manifest,
)
from laptop_tracker.selection_audit import review_evidence, selected_review_evidence
from laptop_tracker.supporting_tables import MATCH_REVIEW_HEADERS, SOURCE_MODEL_MAP_HEADERS
from laptop_tracker.variation_ranking import LIST_NAME, SELECTION_POLICY, VARIATION_COLUMNS
from laptop_tracker.verified_top import sha256_values

pytestmark = pytest.mark.usefixtures("no_network")


def write_ranking(path: Path, rows: list[dict[str, str]]) -> Path:
    columns = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def inputs(tmp_path: Path) -> dict[str, Path]:
    ranking = write_ranking(
        tmp_path / "eligible_model_ranking.csv",
        [
            vs("101", "900", "0", item="1"),  # unmapped, highest GMV
            vs("102", "800", "0", item="2", shop="11", price="45990.0"),
            vs("103", "700", "0", item="3", shop="12", price=""),  # missing own price
            vs("104", "600", "0", item="4", eligibility_status="EXCLUDED"),
            vs("105", "500", "0", item="5"),  # mapped to an inactive target
        ],
    )
    return {
        "ranking": ranking,
        "target_master": write_rows(
            tmp_path / "target_master.csv",
            target_values(
                target("TGT-000001"),
                target("TGT-000002", model="ExampleBook 16", status="INACTIVE"),
            ),
        ),
        "source_model_map": write_rows(
            tmp_path / "source_model_map.csv",
            [
                SOURCE_MODEL_MAP_HEADERS,
                map_row(source_model_id="102", target_id="TGT-000001"),
                map_row(source_model_id="103", target_id="TGT-000001"),
                map_row(source_model_id="105", target_id="TGT-000002"),
            ],
        ),
        # MATCH_REVIEW holds only an INCOMPLETE row: it never gates selection.
        "match_review": write_rows(
            tmp_path / "match_review.csv",
            [
                MATCH_REVIEW_HEADERS,
                review_row(status="INCOMPLETE", source_model_id="102", candidate_name="x"),
            ],
        ),
    }


def build_args(tmp_path: Path, inputs: dict[str, Path], out: Path, *extra: str) -> list[str]:
    return [
        "--ranking", str(inputs["ranking"]),
        "--source-top-100", str(tmp_path / "absent_top_100_targets.csv"),
        "--target-master", str(inputs["target_master"]),
        "--source-model-map", str(inputs["source_model_map"]),
        "--match-review", str(inputs["match_review"]),
        "--identity-register", str(tmp_path / "absent_register.csv"),
        "--output-dir", str(out),
        *extra,
    ]  # fmt: skip


def test_the_builder_reports_scope_exclusions_and_an_honest_shortfall(
    tmp_path, inputs, capsys
) -> None:
    out = tmp_path / "variations"
    code = load_script("build_top_shopee_variations").main(build_args(tmp_path, inputs, out))
    printed = capsys.readouterr().out

    assert code == 0
    assert f"{LIST_NAME}: 4 eligible variations, 2 with a verified mapping, 2 excluded" in printed
    assert "excluded UNMAPPED: 1" in printed
    assert "excluded MAPPED_TARGET_INACTIVE: 1" in printed
    assert "Selected: 2 of 100 (shortfall 98)" in printed
    assert "only 2 of 4 eligible variations have a verified mapping" in printed

    top = read(out / "top_100_shopee_variations.csv")
    assert [r["source_model_id"] for r in top] == ["102", "103"]
    assert [(r["rank"], r["eligible_rank"]) for r in top] == [("1", "2"), ("2", "3")]
    assert {r["mapping_status"] for r in top} == {"MAPPED_VERIFIED"}
    assert top[0]["shopee_current_price_php"] == "45990.00"
    assert top[1]["shopee_current_price_php"] == ""  # a missing own price stays explicit
    assert top[1]["shopee_price_status"] == "PRICE_MISSING"

    ranked = read(out / "shopee_variation_ranking.csv")
    assert [r["source_model_id"] for r in ranked] == ["101", "102", "103", "105"]  # audit dataset
    assert [r["selection_status"] for r in ranked] == [
        "EXCLUDED_NOT_VERIFIED_MAPPED",
        "SELECTED",
        "SELECTED",
        "EXCLUDED_NOT_VERIFIED_MAPPED",
    ]
    exclusions = read(out / "mapping_exclusions.csv")
    assert [(r["source_key"], r["exclusion_reason"]) for r in exclusions] == [
        ("10|1|101", "UNMAPPED"),
        ("10|5|105", "MAPPED_TARGET_INACTIVE"),
    ]

    summary = json.loads((out / "shopee_variation_run_summary.json").read_text())
    assert summary["list_name"] == LIST_NAME and summary["selection_policy"] == SELECTION_POLICY
    counts = summary["counts"]
    assert (counts["eligible_variations"], counts["eligible_variations_with_verified_mapping"]) == (
        4,
        2,
    )
    assert counts["excluded_by_mapping_failure"] == {"MAPPED_TARGET_INACTIVE": 1, "UNMAPPED": 1}
    assert counts["selected_variations"] == 2 and counts["distinct_configuration_contexts"] == 1
    assert counts["shortfall"] == 98
    assert list(top[0]) == list(VARIATION_COLUMNS)


def test_the_audit_cli_passes_and_catches_a_list_that_kept_an_unmapped_row(
    tmp_path, inputs, capsys
) -> None:
    out = tmp_path / "variations"
    assert load_script("build_top_shopee_variations").main(build_args(tmp_path, inputs, out)) == 0
    audit_args = [
        "--variation-dir", str(out),
        "--ranking", str(inputs["ranking"]),
        "--target-master", str(inputs["target_master"]),
        "--source-model-map", str(inputs["source_model_map"]),
        "--match-review", str(inputs["match_review"]),
        "--identity-register", str(tmp_path / "absent_register.csv"),
    ]  # fmt: skip
    audit = load_script("audit_top_shopee_variations")

    assert audit.main(audit_args) == 0
    report = json.loads((out / "grain_audit.json").read_text())
    assert report["audit_passed"] is True
    assert report["after"]["eligible_variations"] == 4
    assert report["after"]["excluded_by_mapping_failure"] == {
        "MAPPED_TARGET_INACTIVE": 1,
        "UNMAPPED": 1,
    }

    # Tamper: the written selection now starts with the unmapped high-ranked variation.
    ranked = read(out / "shopee_variation_ranking.csv")
    forged = [ranked[0], *read(out / "top_100_shopee_variations.csv")]
    with (out / "top_100_shopee_variations.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(VARIATION_COLUMNS))
        writer.writeheader()
        writer.writerows(forged)
    capsys.readouterr()

    assert audit.main(audit_args) == 1
    assert "verified-mapped" in capsys.readouterr().err


# Catalog retrieval record: current Sheets retrieval versus cached snapshot


def record_retrieval(inputs: dict[str, Path], when: datetime, *, source="GOOGLE_SHEETS_READ_ONLY"):
    names = {
        "TARGET_MASTER": "target_master",
        "SOURCE_MODEL_MAP": "source_model_map",
        "MATCH_REVIEW": "match_review",
    }
    raw = {tab: [["a", "b"], ["1", "2"]] for tab in names}
    return write_retrieval_manifest(
        inputs["target_master"].parent / MANIFEST_NAME,
        source=source,
        spreadsheet_id="SHEET-ID",
        retrieved_at=when,
        raw_tables=raw,
        raw_fingerprint=sha256_values,
        validated_files={t: inputs[n] for t, n in names.items()},
        check_passed=True,
    )


def catalog_files(inputs):
    return {
        "TARGET_MASTER": inputs["target_master"],
        "SOURCE_MODEL_MAP": inputs["source_model_map"],
        "MATCH_REVIEW": inputs["match_review"],
    }


def test_catalog_inputs_are_classified_as_fresh_stale_changed_or_unrecorded(inputs) -> None:
    now = datetime(2026, 11, 3, 12, tzinfo=UTC)
    files = catalog_files(inputs)

    assert describe_catalog_inputs(files, None, now=now)["status"] == NO_RECORD
    manifest = record_retrieval(inputs, now - timedelta(hours=2))
    fresh = describe_catalog_inputs(files, manifest, now=now)
    assert fresh["status"] == FRESH and fresh["retrieved_at_utc"] == "2026-11-03T10:00:00+00:00"
    assert fresh["spreadsheet_id"] == "SHEET-ID" and fresh["age_hours_at_build"] == 2.0
    old = record_retrieval(inputs, now - timedelta(hours=60))
    assert describe_catalog_inputs(files, old, now=now)["status"] == STALE
    local = record_retrieval(inputs, now, source="LOCAL_SNAPSHOT")
    assert describe_catalog_inputs(files, local, now=now)["status"] == NO_RECORD
    manifest = record_retrieval(inputs, now)
    inputs["source_model_map"].write_text(inputs["source_model_map"].read_text() + "\n")
    changed = describe_catalog_inputs(files, manifest, now=now)
    assert changed["status"] == CHANGED and "SOURCE_MODEL_MAP" in changed["detail"]


def test_the_builder_records_where_the_catalog_came_from(tmp_path, inputs, capsys) -> None:
    out = tmp_path / "variations"
    build_top = load_script("build_top_shopee_variations")

    assert build_top.main(build_args(tmp_path, inputs, out)) == 0
    summary = json.loads((out / "shopee_variation_run_summary.json").read_text())
    assert summary["catalog_inputs"]["status"] == NO_RECORD  # a bare local snapshot
    assert "LOCAL_SNAPSHOT_NO_RETRIEVAL_RECORD" in capsys.readouterr().out

    record_retrieval(inputs, datetime.now(UTC) - timedelta(minutes=5))
    assert build_top.main(build_args(tmp_path, inputs, out)) == 0
    recorded = json.loads((out / "shopee_variation_run_summary.json").read_text())["catalog_inputs"]
    assert recorded["status"] == FRESH and recorded["spreadsheet_id"] == "SHEET-ID"
    assert set(recorded["tabs"]) == {"TARGET_MASTER", "SOURCE_MODEL_MAP", "MATCH_REVIEW"}
    assert all(t["validated_sha256"] for t in recorded["tabs"].values())


# Selection audit


def audit_args(tmp_path, inputs, out, *extra):
    return [
        "--variation-dir", str(out), "--ranking", str(inputs["ranking"]),
        "--target-master", str(inputs["target_master"]),
        "--source-model-map", str(inputs["source_model_map"]),
        "--match-review", str(inputs["match_review"]), *extra,
    ]  # fmt: skip


def test_the_selection_audit_reports_counts_reasons_and_evidence(tmp_path, inputs, capsys) -> None:
    out = tmp_path / "variations"
    record_retrieval(inputs, datetime.now(UTC) - timedelta(minutes=5))
    assert load_script("build_top_shopee_variations").main(build_args(tmp_path, inputs, out)) == 0
    audit = load_script("audit_variation_selection")

    code = audit.main(audit_args(tmp_path, inputs, out, "--expect-spreadsheet-id", "SHEET-ID"))
    report = json.loads((out / "selection_audit.json").read_text())

    assert code == 0 and report["checks_passed"] is True
    sel = report["selection"]
    assert (sel["eligible_pool"], sel["qualifying_mapped_variations"]) == (4, 2)
    assert sel["selected_variations"] == 2 and sel["distinct_target_contexts"] == 1
    assert sel["shortfall"] == 98 and sel["the_limit_bound"] is False
    assert sel["excluded_by_reason"] == {"MAPPED_TARGET_INACTIVE": 1, "UNMAPPED": 1}
    trace = report["pool_trace"]
    assert trace["eligible_ranking_rows"] == 4 and trace["eligible_ranking_file_rows"] == 5
    assert trace["file_rows_not_eligible_or_not_in_mtd"] == 1
    assert (
        trace["mapped_ids_in_eligible_pool"] == 3 and trace["mapped_ids_outside_eligible_pool"] == 0
    )
    reps = report["representative_exclusions"]
    assert reps["UNMAPPED"][0]["source_key"] == "10|1|101"
    assert reps["UNMAPPED"][0]["match_review"] == {"review_row": "NONE"}
    assert reps["MAPPED_TARGET_INACTIVE"][0]["target_id"] == "TGT-000002"
    assert "Target status is not ACTIVE" in reps["MAPPED_TARGET_INACTIVE"][0]["evidence"]
    assert "## Representative excluded rows" in (out / "selection_audit.md").read_text()

    # An unrecorded (cached) catalog fails the freshness check; a wrong spreadsheet fails too.
    (inputs["target_master"].parent / MANIFEST_NAME).unlink()
    assert load_script("build_top_shopee_variations").main(build_args(tmp_path, inputs, out)) == 0
    assert audit.main(audit_args(tmp_path, inputs, out)) == 1
    record_retrieval(inputs, datetime.now(UTC))
    assert load_script("build_top_shopee_variations").main(build_args(tmp_path, inputs, out)) == 0
    assert audit.main(audit_args(tmp_path, inputs, out, "--expect-spreadsheet-id", "OTHER")) == 1


def test_the_selection_audit_lists_the_catalog_changes_it_picked_up(tmp_path, inputs) -> None:
    out = tmp_path / "variations"
    previous = tmp_path / "previous"
    previous.mkdir()
    write_rows(previous / "target_master_validated.csv", target_values(target("TGT-000001")))
    write_rows(
        previous / "source_model_map_validated.csv",
        [SOURCE_MODEL_MAP_HEADERS, map_row(source_model_id="102", target_id="TGT-000001")],
    )
    record_retrieval(inputs, datetime.now(UTC))
    assert load_script("build_top_shopee_variations").main(build_args(tmp_path, inputs, out)) == 0
    audit = load_script("audit_variation_selection")
    args = audit_args(tmp_path, inputs, out, "--previous-catalog-dir", str(previous))

    assert audit.main([*args, "--expect-new-mappings", "2", "--expect-new-targets", "1"]) == 0
    changes = json.loads((out / "selection_audit.json").read_text())["catalog_changes"]
    assert changes["targets_added"] == ["TGT-000002"] and changes["targets_removed"] == []
    assert [
        (m["source_model_id"], m["selection_status"], m["exclusion_reason"])
        for m in changes["mappings_added"]
    ] == [
        ("103", "SELECTED", ""),
        ("105", "EXCLUDED_NOT_VERIFIED_MAPPED", "MAPPED_TARGET_INACTIVE"),
    ]
    # A catalog update that did not arrive (fewer new mappings than expected) fails the audit.
    assert audit.main([*args, "--expect-new-mappings", "3"]) == 1


def test_match_review_is_evidence_and_a_stale_review_row_is_recognised() -> None:
    def row(**values):
        return dict(zip(MATCH_REVIEW_HEADERS, review_row(**values), strict=True))

    by_source = {
        "1": row(source_model_id="1", candidate_name="Black 8GB", status="COMPLETE"),
        "2": row(
            source_model_id="2",
            candidate_name="Old name",
            status="REVIEW_REQUIRED",
            review_reason="2026-11-03: STILL_UNMAPPED. Need the product label.",
        ),
        "3": row(source_model_id="3", candidate_name="", variant_notes="Selected variation: Blue"),
        "4": row(source_model_id="4", candidate_name=""),
    }

    assert review_evidence("1", "Black 8GB", by_source)["variation_state"] == "CURRENT"
    stale = review_evidence("2", "New name", by_source)
    assert stale["variation_state"] == "STALE" and stale["recorded_variation"] == "Old name"
    assert "STILL_UNMAPPED" in stale["unresolved_note"]
    assert review_evidence("3", "Blue", by_source)["variation_state"] == "CURRENT"
    assert review_evidence("4", "Blue", by_source)["variation_state"] == "UNRECORDED"
    assert review_evidence("9", "Blue", by_source) == {"review_row": "NONE"}
    top = [{"source_model_id": "2", "source_key": "k", "shopee_model_name": "New name"}]
    found = selected_review_evidence(top, by_source)
    assert found["stale_review_rows_not_used"][0]["review_recorded_variation"] == "Old name"
