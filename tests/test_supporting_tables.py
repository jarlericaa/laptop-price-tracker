from __future__ import annotations

import csv
import importlib.util
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import gspread
import pytest
from conftest import settings
from test_target_master import target_row, target_values

from laptop_tracker import sheets_source
from laptop_tracker.supporting_tables import (
    LEGACY_MATCH_REVIEW_HEADERS,
    MATCH_REVIEW_EVIDENCE_COLUMNS,
    MATCH_REVIEW_HEADERS,
    SOURCE_MODEL_MAP_HEADERS,
    TOP_100_SEED_COLUMNS,
    SeedError,
    build_match_review_candidates,
    mapped_source_ids_under_review,
    validate_match_review,
    validate_source_model_map,
)
from laptop_tracker.target_master import (
    TargetMasterSchemaError,
    read_target_master_csv,
    validate_target_master,
)

ROOT = Path(__file__).resolve().parents[1]

# Synthetic data only; no ID, name, or specification here describes a real product.
TARGETS = validate_target_master(
    target_values(
        target_row(),
        target_row(target_id="TGT-000002", storage_capacity_gb="2000", status="INACTIVE"),
        target_row(target_id="TGT-000003", storage_capacity_gb="4000", cpu_series=""),
    )
)


def map_row(**overrides: str) -> list[str]:
    values = {
        "source_model_id": "0012345",
        "target_id": "TGT-000001",
        "reviewer": "synthetic reviewer",
        "reviewed_at": "2026-09-29",
        "notes": "",
    } | overrides
    return [values[column] for column in SOURCE_MODEL_MAP_HEADERS]


def review_row(**overrides: str) -> list[str]:
    values = dict.fromkeys(MATCH_REVIEW_HEADERS, "")
    values |= {"status": "NEW", "source_model_id": "0012345", "candidate_name": "Example 14"}
    values |= overrides
    return [values[column] for column in MATCH_REVIEW_HEADERS]


def codes(result) -> list[tuple[int, str, str]]:
    return [
        (int(row.source_row_number), row.column_name, row.error_code)
        for row in result.errors.itertuples(index=False)
    ]


@pytest.mark.parametrize(
    ("template", "headers"),
    [
        ("source_model_map_template.csv", SOURCE_MODEL_MAP_HEADERS),
        ("match_review_template.csv", MATCH_REVIEW_HEADERS),
    ],
)
def test_templates_are_the_exact_header_contracts(template: str, headers: list[str]) -> None:
    assert read_target_master_csv(ROOT / "config" / template) == [headers]


# SOURCE_MODEL_MAP


def test_mapping_passes_and_keeps_ids_as_strings() -> None:
    result = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, map_row(), map_row(source_model_id="0099")], TARGETS
    )
    assert result.is_valid
    # Several source IDs may point to one target.
    assert result.valid_rows["source_model_id"].tolist() == ["0012345", "0099"]
    assert result.valid_rows["target_id"].tolist() == ["TGT-000001", "TGT-000001"]


def test_header_only_mapping_is_valid() -> None:
    result = validate_source_model_map([SOURCE_MODEL_MAP_HEADERS], TARGETS)
    assert result.is_valid
    assert result.rows.empty


def test_source_model_id_maps_to_at_most_one_target() -> None:
    result = validate_source_model_map(
        [
            SOURCE_MODEL_MAP_HEADERS,
            map_row(),
            map_row(target_id="TGT-000002"),
            map_row(source_model_id="777"),
        ],
        TARGETS,
    )
    assert codes(result) == [
        (2, "source_model_id", "DUPLICATE_SOURCE_MODEL_ID"),
        (3, "source_model_id", "DUPLICATE_SOURCE_MODEL_ID"),
    ]
    assert result.valid_rows["source_model_id"].tolist() == ["777"]


def test_leading_zeroes_make_distinct_source_ids() -> None:
    result = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, map_row(source_model_id="0099"), map_row(source_model_id="99")],
        TARGETS,
    )
    assert result.is_valid


def test_target_references_must_exist_and_be_valid() -> None:
    result = validate_source_model_map(
        [
            SOURCE_MODEL_MAP_HEADERS,
            map_row(source_model_id="1", target_id="TGT-000999"),
            map_row(source_model_id="2", target_id="TGT-000003"),
        ],
        TARGETS,
    )
    assert codes(result) == [
        (2, "target_id", "UNKNOWN_TARGET_ID"),
        (3, "target_id", "TARGET_NOT_VALID"),
    ]


def test_links_to_inactive_targets_are_counted_not_rejected() -> None:
    result = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, map_row(target_id="TGT-000002")], TARGETS
    )
    assert result.is_valid
    assert result.info["links_to_inactive_targets"] == 1


@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("source_model_id", "", "MISSING_VALUE"),
        ("source_model_id", "12a", "INVALID_FORMAT"),
        ("source_model_id", "1.2E+11", "INVALID_FORMAT"),
        ("source_model_id", " 123", "SURROUNDING_WHITESPACE"),
        ("target_id", "", "MISSING_VALUE"),
        ("target_id", "tgt-000001", "INVALID_FORMAT"),
        ("reviewer", "", "MISSING_VALUE"),
        ("reviewed_at", "", "MISSING_VALUE"),
        ("reviewed_at", "2026-02-30", "INVALID_DATE"),
    ],
)
def test_mapping_fields_are_checked(column: str, value: str, code: str) -> None:
    result = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, map_row(**{column: value})], TARGETS
    )
    assert (2, column, code) in codes(result)
    assert result.valid_rows.empty


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ([], "no header row"),
        (SOURCE_MODEL_MAP_HEADERS[:-1], "missing: notes"),
        ([*SOURCE_MODEL_MAP_HEADERS, "extra"], "unknown or blank: extra"),
        (list(reversed(SOURCE_MODEL_MAP_HEADERS)), "out of template order"),
    ],
)
def test_mapping_headers_must_match_template_exactly(headers: list[str], message: str) -> None:
    with pytest.raises(TargetMasterSchemaError, match=message):
        validate_source_model_map([headers] if headers else [], TARGETS)


# MATCH_REVIEW


def test_review_allows_blank_specs_and_header_only_tables() -> None:
    assert validate_match_review([MATCH_REVIEW_HEADERS]).is_valid
    result = validate_match_review([MATCH_REVIEW_HEADERS, review_row()])
    assert result.is_valid
    assert result.info["rows_with_complete_core_identity"] == 0


def test_missing_specs_never_match_each_other() -> None:
    # Identical blank candidates are not grouped, deduplicated, or treated as one configuration.
    result = validate_match_review(
        [MATCH_REVIEW_HEADERS, review_row(), review_row(), review_row(source_model_id="9")]
    )
    assert result.is_valid
    assert len(result.valid_rows) == 3


@pytest.mark.parametrize("status", ["new", "APPROVED", "ACTIVE", " NEW"])
def test_review_accepts_only_approved_statuses(status: str) -> None:
    result = validate_match_review([MATCH_REVIEW_HEADERS, review_row(status=status)])
    assert [entry[1] for entry in codes(result)] == ["status"]


@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("ram_capacity_gb", "16GB", "INVALID_FORMAT"),
        ("storage_capacity_gb", "1TB", "INVALID_FORMAT"),
        ("screen_size_in", "15,6", "INVALID_FORMAT"),
        ("resolution", "FHD", "INVALID_FORMAT"),
        ("refresh_rate_hz", "144Hz", "INVALID_FORMAT"),
        ("source_url", "example.com/p", "INVALID_FORMAT"),
        ("cpu_series", "Example Series ", "SURROUNDING_WHITESPACE"),
        ("source_model_id", "abc", "INVALID_FORMAT"),
        ("reviewed_at", "29/09/2026", "INVALID_DATE"),
    ],
)
def test_nonblank_review_values_use_target_master_formats(
    column: str, value: str, code: str
) -> None:
    extra = {"reviewer": "synthetic reviewer"} if column == "reviewed_at" else {}
    result = validate_match_review([MATCH_REVIEW_HEADERS, review_row(**{column: value}, **extra)])
    assert codes(result) == [(2, column, code)]


def test_rejection_requires_reviewer_and_date() -> None:
    result = validate_match_review([MATCH_REVIEW_HEADERS, review_row(status="REJECTED")])
    assert codes(result) == [(2, "reviewed_at", "MISSING_VALUE"), (2, "reviewer", "MISSING_VALUE")]
    recorded = validate_match_review(
        [
            MATCH_REVIEW_HEADERS,
            review_row(status="REJECTED", reviewer="synthetic reviewer", reviewed_at="2026-09-29"),
        ]
    )
    assert recorded.is_valid


def test_reviewer_and_date_are_recorded_together() -> None:
    result = validate_match_review(
        [
            MATCH_REVIEW_HEADERS,
            review_row(reviewer="synthetic reviewer"),
            review_row(reviewed_at="2026-09-29"),
        ]
    )
    assert codes(result) == [(2, "reviewed_at", "MISSING_VALUE"), (3, "reviewer", "MISSING_VALUE")]


def test_duplicate_review_ids_are_rejected_but_blank_ids_allowed() -> None:
    result = validate_match_review(
        [
            MATCH_REVIEW_HEADERS,
            review_row(review_id="R1"),
            review_row(review_id="r1"),
            review_row(),
            review_row(),
        ]
    )
    assert codes(result) == [
        (2, "review_id", "DUPLICATE_REVIEW_ID"),
        (3, "review_id", "DUPLICATE_REVIEW_ID"),
    ]


def test_complete_candidates_are_counted_but_never_promoted() -> None:
    specs = dict(zip(target_values()[0], target_row(), strict=True))
    evidence = {name: specs[name] for name in MATCH_REVIEW_EVIDENCE_COLUMNS}
    result = validate_match_review(
        [MATCH_REVIEW_HEADERS, review_row(status="REVIEW_REQUIRED", **evidence)]
    )
    assert result.is_valid
    assert result.info["rows_with_complete_core_identity"] == 1
    assert result.valid_rows.loc[0, "status"] == "REVIEW_REQUIRED"
    assert "target_id" not in result.valid_rows.columns


def test_review_evidence_columns_are_the_five_core_fields() -> None:
    assert MATCH_REVIEW_EVIDENCE_COLUMNS == [
        "model",
        "screen_size_in",
        "cpu_series",
        "ram_capacity_gb",
        "storage_capacity_gb",
    ]


def test_complete_status_needs_core_fields_but_not_secondary_or_source_url() -> None:
    core = {
        "model": "ExampleBook 14",
        "screen_size_in": "14",
        "cpu_series": "Example Series 5",
        "ram_capacity_gb": "16",
        "storage_capacity_gb": "1000",
    }
    result = validate_match_review([MATCH_REVIEW_HEADERS, review_row(status="COMPLETE", **core)])
    assert result.is_valid
    assert result.info["status_COMPLETE"] == 1

    partial = validate_match_review(
        [MATCH_REVIEW_HEADERS, review_row(status="COMPLETE", **(core | {"cpu_series": ""}))]
    )
    assert codes(partial) == [(2, "cpu_series", "MISSING_VALUE")]


def reorder(values: list[list[str]], headers: list[str]) -> list[list[str]]:
    """Rearrange worksheet columns to ``headers`` order, moving every cell with its header."""
    positions = [values[0].index(name) for name in headers]
    return [[row[position] for position in positions] for row in values]


REVIEW_CORE = {
    "model": "ExampleBook 14",
    "screen_size_in": "14",
    "cpu_series": "Example Series 5",
    "ram_capacity_gb": "16",
    "storage_capacity_gb": "1000",
}
LISTING_CANDIDATE = {
    "tracking_mode": "LISTING_ONLY",
    "shop_id": "10",
    "item_id": "20",
    "tracking_mode_reason": "Seller and listing show no product model name or code.",
    "classification_evidence": "Listing title, variation names, and seller specs checked.",
}


def review_sample() -> list[list[str]]:
    """Rows that exercise each rule: valid, a core gap, a listing candidate, a bad value."""
    return [
        MATCH_REVIEW_HEADERS,
        review_row(review_id="R1", status="COMPLETE", **REVIEW_CORE),
        review_row(review_id="R2", status="COMPLETE", **(REVIEW_CORE | {"model": ""})),
        review_row(review_id="R3", status="COMPLETE", source_model_id="0042", **LISTING_CANDIDATE),
        review_row(review_id="R4", ram_capacity_gb="16GB", reviewer="synthetic reviewer"),
    ]


@pytest.mark.parametrize(
    "order",
    [
        list(reversed(MATCH_REVIEW_HEADERS)),
        # The tracking-mode columns placed before notes, as a worksheet edit could leave them.
        [
            *[name for name in MATCH_REVIEW_HEADERS if name not in LISTING_CANDIDATE],
            *LISTING_CANDIDATE,
        ],
        sorted(MATCH_REVIEW_HEADERS),
    ],
    ids=["reversed", "tracking-columns-moved", "alphabetical"],
)
def test_review_columns_are_read_by_header_name(order: list[str]) -> None:
    expected = validate_match_review(review_sample())
    result = validate_match_review(reorder(review_sample(), order))

    # Same row checks and tracking-mode rules, and the output stays in template order.
    assert codes(result) == codes(expected) == [
        (3, "model", "MISSING_VALUE"),
        (5, "ram_capacity_gb", "INVALID_FORMAT"),
        (5, "reviewed_at", "MISSING_VALUE"),
    ]
    assert list(result.rows.columns) == ["source_row_number", *MATCH_REVIEW_HEADERS]
    assert result.rows.equals(expected.rows)
    assert result.valid_rows.equals(expected.valid_rows)
    assert result.info == expected.info
    assert result.valid_rows.loc[1, "tracking_mode"] == "LISTING_ONLY"
    assert result.valid_rows.loc[1, "item_id"] == "20"


def test_reordered_legacy_review_header_is_read_as_configuration_candidates() -> None:
    legacy = [MATCH_REVIEW_HEADERS[:-5]] + [row[:-5] for row in review_sample()[1:3]]
    assert legacy[0] == LEGACY_MATCH_REVIEW_HEADERS
    result = validate_match_review(reorder(legacy, list(reversed(LEGACY_MATCH_REVIEW_HEADERS))))
    assert codes(result) == [(3, "model", "MISSING_VALUE")]
    assert list(result.rows.columns) == ["source_row_number", *MATCH_REVIEW_HEADERS]
    assert result.valid_rows.loc[0, "model"] == "ExampleBook 14"
    assert result.rows["tracking_mode"].tolist() == ["", ""]


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ([], "no header row"),
        (["", "  "], "no header row"),
        ([name for name in MATCH_REVIEW_HEADERS if name != "model"], "missing: model"),
        (
            [name for name in LEGACY_MATCH_REVIEW_HEADERS if name != "status"][::-1],
            "missing: status",
        ),
        ([*MATCH_REVIEW_HEADERS, "notes"], "duplicate: notes"),
        ([*reversed(LEGACY_MATCH_REVIEW_HEADERS), "review_id"], "duplicate: review_id"),
        (
            [*MATCH_REVIEW_HEADERS[:5], "", *MATCH_REVIEW_HEADERS[5:]],
            r"blank header in column\(s\) 6",
        ),
        ([*MATCH_REVIEW_HEADERS, None], r"blank header in column\(s\) 44"),
        ([*MATCH_REVIEW_HEADERS, "extra"], "unknown: extra"),
        # A misspelt header is unknown and leaves its real column missing.
        (
            ["Status" if name == "status" else name for name in MATCH_REVIEW_HEADERS],
            "unknown: Status; missing: status",
        ),
        # A TARGET_MASTER-only column is not a MATCH_REVIEW column.
        ([*MATCH_REVIEW_HEADERS, "target_id"], "unknown: target_id"),
        # Some, but not all, tracking-mode columns: neither the legacy nor the current schema.
        ([*LEGACY_MATCH_REVIEW_HEADERS, "tracking_mode"], "missing: shop_id, item_id"),
    ],
)
def test_malformed_review_headers_are_rejected(headers: list, message: str) -> None:
    with pytest.raises(TargetMasterSchemaError, match=message):
        validate_match_review([headers] if headers else [])


def test_mapped_ids_still_open_in_review_are_counted() -> None:
    mapping = validate_source_model_map([SOURCE_MODEL_MAP_HEADERS, map_row()], TARGETS)
    review = validate_match_review(
        [MATCH_REVIEW_HEADERS, review_row(), review_row(source_model_id="5")]
    )
    assert mapped_source_ids_under_review(mapping, review) == 1


# Top 100 seeding


def top_100_values(*model_ids: str) -> list[list[str]]:
    headers = ["rank", *TOP_100_SEED_COLUMNS[1:], "ranking_gmv_usd"]
    rows = [
        [str(rank), "run-1", "10", "20", model_id, f"Listing {rank}", f"Variant {rank}", "1.5"]
        for rank, model_id in enumerate(model_ids, start=1)
    ]
    return [headers, *rows]


def test_seed_creates_one_new_candidate_per_distinct_model_without_guessing() -> None:
    values = build_match_review_candidates(top_100_values("0042", "7", "0042"))
    assert values[0] == MATCH_REVIEW_HEADERS
    rows = [dict(zip(MATCH_REVIEW_HEADERS, row, strict=True)) for row in values[1:]]
    assert [row["source_model_id"] for row in rows] == ["0042", "7"]
    assert {row["status"] for row in rows} == {"NEW"}
    assert rows[0]["candidate_name"] == "Listing 1"
    assert "Variant 1" in rows[0]["notes"] and "rank 1" in rows[0]["notes"]
    guessed = [
        "review_id",
        "brand",
        "model",
        "screen_size_in",
        "resolution",
        "display_type",
        "refresh_rate_hz",
        "cpu_series",
        "ram_capacity_gb",
        "storage_capacity_gb",
        "gpu_model",
        "os",
        "source_url",
        "prepared_by",
        "reviewer",
        "reviewed_at",
    ]
    assert all(row[column] == "" for row in rows for column in guessed)
    assert validate_match_review(values).is_valid


@pytest.mark.parametrize("model_id", ["", "12a", "1.2E+11"])
def test_seed_fails_closed_on_unusable_model_ids(model_id: str) -> None:
    with pytest.raises(SeedError, match="row 2"):
        build_match_review_candidates(top_100_values(model_id))


def test_seed_requires_top_100_columns() -> None:
    with pytest.raises(SeedError, match="missing columns: model_id"):
        build_match_review_candidates([["rank", "run_id", "shop_id", "item_id"]])


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    return path


def test_seed_cli_writes_new_file_and_prints_counts_only(tmp_path: Path, capsys) -> None:
    source = write_csv(tmp_path / "top.csv", top_100_values("1", "2"))
    output = tmp_path / "review" / "candidates.csv"
    seed = load_script("seed_match_review")
    assert seed.main(["--top-100", str(source), "--output", str(output)]) == 0
    assert len(read_target_master_csv(output)) == 3
    printed = capsys.readouterr().out
    assert "NEW candidates written: 2" in printed
    assert "Listing" not in printed and "Variant" not in printed

    # A second run must not overwrite review work.
    assert seed.main(["--top-100", str(source), "--output", str(output)]) == 1
    assert "Refusing to overwrite" in capsys.readouterr().err


def test_seed_cli_without_top_100_writes_nothing(tmp_path: Path, capsys) -> None:
    output = tmp_path / "candidates.csv"
    seed = load_script("seed_match_review")
    assert seed.main(["--top-100", str(tmp_path / "absent.csv"), "--output", str(output)]) == 1
    assert "No candidate file was written" in capsys.readouterr().err
    assert not output.exists()


# check_target_tables.py


def test_check_cli_local_snapshots_pass_and_hide_values(tmp_path: Path, capsys) -> None:
    master = write_csv(tmp_path / "tm.csv", target_values(target_row()))
    mapping = write_csv(tmp_path / "map.csv", [SOURCE_MODEL_MAP_HEADERS, map_row()])
    review = write_csv(tmp_path / "review.csv", [MATCH_REVIEW_HEADERS, review_row()])
    out = tmp_path / "out"
    cli = load_script("check_target_tables")
    argv = ["--target-master", str(master), "--source-model-map", str(mapping)]
    argv += ["--match-review", str(review), "--output-dir", str(out)]
    assert cli.main(argv) == 0
    printed = capsys.readouterr().out
    assert "Target table check passed." in printed
    assert "ExampleBrand" not in printed and "0012345" not in printed
    for prefix in ("target_master", "source_model_map", "match_review"):
        assert (out / f"{prefix}_validation_errors.csv").is_file()
        assert (out / f"{prefix}_validated.csv").is_file()
    assert read_target_master_csv(out / "source_model_map_validated.csv")[1][1] == "0012345"


def test_check_cli_failure_reports_codes_and_removes_stale_copy(tmp_path: Path, capsys) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "match_review_validated.csv").write_text("stale\n", encoding="utf-8")
    review = write_csv(
        tmp_path / "review.csv", [MATCH_REVIEW_HEADERS, review_row(status="SECRET_STATUS")]
    )
    cli = load_script("check_target_tables")
    assert cli.main(["--match-review", str(review), "--output-dir", str(out)]) == 1
    captured = capsys.readouterr()
    assert "INVALID_VALUE: 1" in captured.out
    assert "SECRET_STATUS" not in captured.out + captured.err
    assert not (out / "match_review_validated.csv").exists()


def test_check_cli_requires_target_master_for_mapping(tmp_path: Path) -> None:
    mapping = write_csv(tmp_path / "map.csv", [SOURCE_MODEL_MAP_HEADERS])
    cli = load_script("check_target_tables")
    with pytest.raises(SystemExit):
        cli.main(["--source-model-map", str(mapping)])
    with pytest.raises(SystemExit):
        cli.main(["--from-sheets", "--match-review", str(mapping)])


def test_check_cli_from_sheets_reports_missing_tabs(tmp_path: Path, capsys) -> None:
    tables = {
        "TARGET_MASTER": target_values(target_row()),
        "SOURCE_MODEL_MAP": None,
        "MATCH_REVIEW": [MATCH_REVIEW_HEADERS],
    }
    cli = load_script("check_target_tables")
    with (
        patch.object(cli.Settings, "from_env", return_value=settings()),
        patch.object(cli, "read_target_table_values", return_value=tables) as reader,
    ):
        assert cli.main(["--from-sheets", "--output-dir", str(tmp_path)]) == 1
    assert reader.call_count == 1
    captured = capsys.readouterr()
    assert "SOURCE_MODEL_MAP: worksheet not found" in captured.err
    assert "never creates tabs" in captured.err
    assert "TARGET_MASTER passed" in captured.out


# Read-only sheet reader


class ReadOnlyWorksheet:
    """Exposes only get_all_values; any write method would raise AttributeError."""

    def __init__(self, values: list[list[object]]) -> None:
        self.values = values
        self.reads = 0

    def get_all_values(self) -> list[list[object]]:
        self.reads += 1
        return self.values


class ReadOnlySpreadsheet:
    def __init__(self, worksheets: dict[str, ReadOnlyWorksheet]) -> None:
        self.worksheets = worksheets

    def worksheet(self, name: str) -> ReadOnlyWorksheet:
        if name not in self.worksheets:
            raise gspread.exceptions.WorksheetNotFound(name)
        return self.worksheets[name]


class ReadOnlyClient:
    def __init__(self, spreadsheet: ReadOnlySpreadsheet) -> None:
        self.spreadsheet = spreadsheet

    def open_by_key(self, _key: str) -> ReadOnlySpreadsheet:
        return self.spreadsheet


def test_sheet_reader_uses_read_only_access_and_never_creates_tabs() -> None:
    master = ReadOnlyWorksheet([["target_id"], ["TGT-000001"], [None]])
    review = ReadOnlyWorksheet([MATCH_REVIEW_HEADERS])
    client = ReadOnlyClient(ReadOnlySpreadsheet({"TARGET_MASTER": master, "MATCH_REVIEW": review}))
    configured = replace(settings(), spreadsheet_id="sheet-id")
    with patch.object(sheets_source, "connect", return_value=client) as connect:
        tables = sheets_source.read_target_table_values(configured)
    assert connect.call_args.kwargs == {"read_only": True}
    assert tables["TARGET_MASTER"] == [["target_id"], ["TGT-000001"], [""]]
    assert tables["SOURCE_MODEL_MAP"] is None
    assert tables["MATCH_REVIEW"] == [MATCH_REVIEW_HEADERS]
    assert master.reads == review.reads == 1
