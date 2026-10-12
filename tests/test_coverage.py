"""Phase 5 Top 100 coverage audit tests. Synthetic IDs and tables only."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import settings
from test_supporting_tables import map_row, review_row, top_100_values
from test_target_master import target_row, target_values

from laptop_tracker.coverage import (
    COMPLETE_WITHOUT_MAPPING_COLUMNS,
    TOP_100_COVERAGE_COLUMNS,
    CoverageInputError,
    audit_coverage,
)
from laptop_tracker.supporting_tables import (
    MATCH_REVIEW_HEADERS,
    SOURCE_MODEL_MAP_HEADERS,
    validate_match_review,
    validate_source_model_map,
)
from laptop_tracker.target_master import read_table_snapshot_csv, validate_target_master

ROOT = Path(__file__).resolve().parents[1]

CORE = {
    "model": "ExampleBook 14",
    "screen_size_in": "14",
    "cpu_series": "Example Series 5",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "1000",
}

TARGET_VALUES = target_values(
    target_row(),
    target_row(target_id="TGT-000002", status="INACTIVE", storage_capacity_gb="2000"),
)
MAP_VALUES = [
    SOURCE_MODEL_MAP_HEADERS,
    map_row(source_model_id="0042", target_id="TGT-000001"),
    map_row(source_model_id="7", target_id="TGT-000002"),
    map_row(source_model_id="900", target_id="TGT-000001"),  # Not in the Top 100.
]
REVIEW_VALUES = [
    MATCH_REVIEW_HEADERS,
    review_row(status="COMPLETE", source_model_id="0042", **CORE),  # Mapped audit trail.
    review_row(status="INCOMPLETE", source_model_id="11"),
    review_row(status="REVIEW_REQUIRED", source_model_id="12"),
    review_row(status="INCOMPLETE", source_model_id="13"),
    review_row(status="REVIEW_REQUIRED", source_model_id="13"),
    review_row(status="COMPLETE", source_model_id="42", **CORE),  # Not "0042": unmapped.
    review_row(status="COMPLETE", source_model_id="7", **CORE),  # Mapped to INACTIVE.
    review_row(status="COMPLETE", source_model_id="", **CORE),
]
TOP_IDS = ("0042", "7", "11", "12", "13", "42", "99", "0042")


def tables():
    master = validate_target_master(TARGET_VALUES)
    return (
        master,
        validate_source_model_map(MAP_VALUES, master),
        validate_match_review(REVIEW_VALUES),
    )


def by_id(report) -> dict[str, dict[str, str]]:
    return {row["model_id"]: row for row in report.rows}


def test_each_top_100_id_is_classified_with_exact_text_ids() -> None:
    report = audit_coverage(top_100_values(*TOP_IDS), *tables())
    rows = by_id(report)

    assert report.top_100_rows == 8
    assert report.duplicate_top_100_rows == 1
    assert [row["model_id"] for row in report.rows] == ["0042", "7", "11", "12", "13", "42", "99"]
    assert {model_id: row["coverage_status"] for model_id, row in rows.items()} == {
        "0042": "MAPPED_CONFIGURATION",
        "7": "MAPPED_INACTIVE_TARGET",
        "11": "UNMAPPED_IN_REVIEW",
        "12": "UNMAPPED_IN_REVIEW",
        "13": "UNMAPPED_IN_REVIEW",
        "42": "UNMAPPED_IN_REVIEW",  # Leading zeroes matter: "42" is not mapped.
        "99": "MISSING",
    }
    assert rows["0042"]["target_id"] == "TGT-000001"
    assert rows["0042"]["review_statuses"] == "COMPLETE"
    assert rows["7"]["target_status"] == "INACTIVE"
    assert rows["13"]["review_statuses"] == "INCOMPLETE;REVIEW_REQUIRED"
    assert rows["13"]["review_row_count"] == "2"
    assert dict(report.counts) == {
        "MAPPED_CONFIGURATION": 1,
        "MAPPED_INACTIVE_TARGET": 1,
        "UNMAPPED_IN_REVIEW": 4,
        "MISSING": 1,
    }
    assert dict(report.unmapped_review_status_counts) == {
        "COMPLETE": 1,
        "INCOMPLETE": 1,
        "REVIEW_REQUIRED": 1,
        "INCOMPLETE;REVIEW_REQUIRED": 1,
    }
    assert list(report.rows[0]) == list(TOP_100_COVERAGE_COLUMNS)


def test_complete_review_rows_without_an_active_mapping_are_listed() -> None:
    report = audit_coverage(top_100_values(*TOP_IDS), *tables())

    assert [
        (row["source_model_id"], row["issue"], row["target_id"])
        for row in report.complete_without_mapping
    ] == [
        ("42", "NOT_MAPPED", ""),
        ("7", "MAPPED_TO_INACTIVE_TARGET", "TGT-000002"),
        ("", "NO_SOURCE_MODEL_ID", ""),
    ]
    assert report.complete_without_mapping[0]["review_source_row_number"] == "7"
    assert list(report.complete_without_mapping[0]) == list(COMPLETE_WITHOUT_MAPPING_COLUMNS)


def test_mapping_to_a_target_absent_from_the_master_snapshot_is_invalid() -> None:
    # Snapshots taken at different times: the map was validated against a newer master.
    newer = validate_target_master(
        target_values(target_row(), target_row(target_id="TGT-000003", storage_capacity_gb="512"))
    )
    mapping = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, map_row(source_model_id="5", target_id="TGT-000003")], newer
    )
    review = validate_match_review(
        [MATCH_REVIEW_HEADERS, review_row(status="COMPLETE", source_model_id="5", **CORE)]
    )
    master = validate_target_master(target_values(target_row()))

    report = audit_coverage(top_100_values("5"), master, mapping, review)

    assert report.rows[0]["coverage_status"] == "MAPPED_INVALID_TARGET"
    assert report.complete_without_mapping[0]["issue"] == "MAPPED_TO_INVALID_TARGET"


def test_any_table_failing_validation_stops_the_audit() -> None:
    master, mapping, _ = tables()
    bad_review = validate_match_review(
        [MATCH_REVIEW_HEADERS, review_row(status="COMPLETE", source_model_id="11")]
    )
    with pytest.raises(CoverageInputError, match="MATCH_REVIEW did not pass validation"):
        audit_coverage(top_100_values("11"), master, mapping, bad_review)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([], "no header row"),
        ([["rank", "model_id"]], "missing columns"),
        (top_100_values("12a"), "row 2 has a blank or non-digit model_id"),
        ([top_100_values("1")[0]], "no data rows"),
    ],
)
def test_unusable_top_100_input_fails_clearly(values, message: str) -> None:
    with pytest.raises(CoverageInputError, match=message):
        audit_coverage(values, *tables())


def test_validated_copies_are_read_as_their_template_tables(tmp_path: Path) -> None:
    path = tmp_path / "source_model_map_validated.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source_row_number", *SOURCE_MODEL_MAP_HEADERS])
        writer.writerow(["2", *map_row(source_model_id="0042")])
    expected = [SOURCE_MODEL_MAP_HEADERS, map_row(source_model_id="0042")]
    assert read_table_snapshot_csv(path) == expected


# audit_top_100_coverage.py


def load_cli():
    spec = importlib.util.spec_from_file_location(
        "audit_top_100_coverage", ROOT / "audit_top_100_coverage.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, rows: list[list[str]], *, row_numbers: bool = False) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if row_numbers:  # The shape of a check_target_tables.py validated copy.
            writer.writerow(["source_row_number", *rows[0]])
            writer.writerows([str(number), *row] for number, row in enumerate(rows[1:], start=2))
        else:
            writer.writerows(rows)
    return path


def read_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def local_args(tmp_path: Path, *, row_numbers: bool = False) -> list[str]:
    top = write_csv(tmp_path / "top.csv", top_100_values(*TOP_IDS))
    master = write_csv(tmp_path / "tm.csv", TARGET_VALUES, row_numbers=row_numbers)
    mapping = write_csv(tmp_path / "map.csv", MAP_VALUES, row_numbers=row_numbers)
    review = write_csv(tmp_path / "review.csv", REVIEW_VALUES, row_numbers=row_numbers)
    return [
        "--top-100", str(top), "--target-master", str(master), "--source-model-map",
        str(mapping), "--match-review", str(review), "--output-dir", str(tmp_path / "out"),
    ]


@pytest.mark.parametrize("row_numbers", [False, True])
def test_cli_writes_local_reports_and_prints_counts_only(
    tmp_path: Path, capsys, row_numbers: bool
) -> None:
    assert load_cli().main(local_args(tmp_path, row_numbers=row_numbers)) == 0

    out = tmp_path / "out"
    coverage = read_dicts(out / "top_100_coverage.csv")
    assert [row["model_id"] for row in coverage] == ["0042", "7", "11", "12", "13", "42", "99"]
    assert len(read_dicts(out / "complete_review_without_mapping.csv")) == 3
    printed = capsys.readouterr().out
    assert "MAPPED_CONFIGURATION: 1" in printed
    assert "UNMAPPED_IN_REVIEW: 4" in printed
    assert "unmapped, review status INCOMPLETE;REVIEW_REQUIRED: 1" in printed
    assert "MISSING: 1" in printed
    assert "COMPLETE review rows without a valid ACTIVE mapping: 3" in printed
    assert "0042" not in printed and "Listing" not in printed and "TGT-" not in printed


def test_cli_invalid_table_stops_and_removes_stale_reports(tmp_path: Path, capsys) -> None:
    args = local_args(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "top_100_coverage.csv").write_text("stale\n", encoding="utf-8")
    write_csv(tmp_path / "review.csv", [MATCH_REVIEW_HEADERS, review_row(status="APPROVED")])

    assert load_cli().main(args) == 1

    captured = capsys.readouterr()
    assert "MATCH_REVIEW INVALID_VALUE: 1" in captured.out
    assert "did not pass validation" in captured.err
    assert not (out / "top_100_coverage.csv").exists()


def test_cli_missing_top_100_or_headers_fails_clearly(tmp_path: Path, capsys) -> None:
    args = local_args(tmp_path)
    (tmp_path / "top.csv").unlink()
    assert load_cli().main(args) == 1
    assert "Top 100 CSV not found" in capsys.readouterr().err

    write_csv(tmp_path / "top.csv", top_100_values(*TOP_IDS))
    write_csv(tmp_path / "map.csv", [["source_model_id", "target_id"]])
    assert load_cli().main(args) == 1
    assert "SOURCE_MODEL_MAP headers must exactly match" in capsys.readouterr().err


def test_cli_requires_all_three_tables_or_from_sheets(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load_cli().parse_args(["--target-master", str(tmp_path / "tm.csv")])
    with pytest.raises(SystemExit):
        load_cli().parse_args(["--from-sheets", "--target-master", str(tmp_path / "tm.csv")])


def test_cli_from_sheets_reads_only_the_three_target_tabs(tmp_path: Path, capsys) -> None:
    top = write_csv(tmp_path / "top.csv", top_100_values(*TOP_IDS))
    live = {
        "TARGET_MASTER": TARGET_VALUES,
        "SOURCE_MODEL_MAP": MAP_VALUES,
        "MATCH_REVIEW": REVIEW_VALUES,
    }
    cli = load_cli()
    argv = ["--from-sheets", "--top-100", str(top), "--output-dir", str(tmp_path / "out")]
    with (
        patch.object(cli.Settings, "from_env", return_value=settings()),
        patch.object(cli, "read_target_table_values", return_value=live) as reader,
    ):
        assert cli.main(argv) == 0
    assert reader.call_args.args[1] == ("TARGET_MASTER", "SOURCE_MODEL_MAP", "MATCH_REVIEW")
    assert "MAPPED_CONFIGURATION: 1" in capsys.readouterr().out

    with (
        patch.object(cli.Settings, "from_env", return_value=settings()),
        patch.object(cli, "read_target_table_values", return_value=live | {"MATCH_REVIEW": None}),
    ):
        assert cli.main(argv) == 1
    assert "Worksheet(s) not found: MATCH_REVIEW" in capsys.readouterr().err
