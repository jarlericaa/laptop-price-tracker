from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import pytest

from laptop_tracker.target_master import (
    REQUIRED_TARGET_COLUMNS,
    TARGET_IDENTITY_COLUMNS,
    TARGET_MASTER_HEADERS,
    TargetMasterSchemaError,
    read_target_master_csv,
    validate_target_master,
)

ROOT = Path(__file__).resolve().parents[1]

# Synthetic configurations only; no brand, model, or value here describes a real target.
BASE_ROW = {
    "target_id": "TGT-000001",
    "status": "ACTIVE",
    "brand": "ExampleBrand",
    "model": "ExampleBook 14",
    "screen_size_in": "14",
    "resolution": "1920x1080",
    "display_type": "IPS",
    "refresh_rate_hz": "60",
    "cpu_brand": "ExampleCPU",
    "cpu_series": "Example Series 5",
    "cpu_generation": "Example Gen 5",
    "cpu_cores": "8",
    "ram_capacity_gb": "16",
    "ram_type": "DDR5",
    "storage_capacity_gb": "1000",
    "storage_type": "SSD",
    "storage_layout": "1x1000GB NVMe SSD",
    "gpu_type": "Integrated",
    "gpu_brand": "Example Graphics",
    "gpu_model": "Example Integrated Graphics",
    "vram": "Shared",
    "os": "ExampleOS Home",
    "battery_capacity": "50Wh",
    "build_material": "Aluminum",
    "series": "ExampleBook",
    "manufacturer_model_code": "EB14",
    "cpu_model": "Example CPU 5 Model X",
    "color": "",
    "variant_notes": "",
    "manufacturer_part_number": "",
    "source_url": "https://example.com/products/eb14",
    "reviewer": "analyst",
    "reviewed_at": "2026-09-28",
    "notes": "",
    # Blank tracking mode: a CONFIGURATION row, exactly like every pre-2026-10-08 row.
    "tracking_mode": "",
    "shop_id": "",
    "item_id": "",
    "source_model_id": "",
    "tracking_mode_reason": "",
    "classification_evidence": "",
}


def target_row(headers: list[str] | None = None, **overrides: str) -> list[str]:
    values = BASE_ROW | overrides
    return [values[column] for column in headers or TARGET_MASTER_HEADERS]


def target_values(*rows: list[str], headers: list[str] | None = None) -> list[list[str]]:
    return [list(headers or TARGET_MASTER_HEADERS), *rows]


def error_codes(result) -> list[tuple[int, str, str]]:
    return [
        (int(row.source_row_number), row.column_name, row.error_code)
        for row in result.errors.itertuples(index=False)
    ]


def test_valid_rows_pass() -> None:
    result = validate_target_master(
        target_values(
            target_row(), target_row(target_id="TGT-000002", storage_capacity_gb="2000")
        )
    )
    assert result.is_valid
    assert result.errors.empty
    assert result.valid_rows["target_id"].tolist() == ["TGT-000001", "TGT-000002"]
    assert result.valid_rows["source_row_number"].tolist() == [2, 3]


def test_template_header_matches_schema() -> None:
    values = read_target_master_csv(ROOT / "config" / "target_master_template.csv")
    assert values == [TARGET_MASTER_HEADERS]


def test_values_remain_exact_strings() -> None:
    result = validate_target_master(
        target_values(
            target_row(
                target_id="TGT-000007",
                manufacturer_model_code="00123",
                cpu_model="1.10",
                manufacturer_part_number="1E+05",
                screen_size_in="15.60",
                source_url="https://example.com/specs?id=0007",
                variant_notes="  keep  spacing  ",
            )
        )
    )
    assert result.is_valid
    row = result.valid_rows.iloc[0]
    assert row["target_id"] == "TGT-000007"
    assert row["manufacturer_model_code"] == "00123"
    assert row["cpu_model"] == "1.10"
    assert row["manufacturer_part_number"] == "1E+05"
    assert row["screen_size_in"] == "15.60"
    assert row["source_url"] == "https://example.com/specs?id=0007"
    assert row["variant_notes"] == "  keep  spacing  "


def test_csv_reader_preserves_strings_and_tolerates_bom(tmp_path: Path) -> None:
    path = tmp_path / "targets.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        csv.writer(handle).writerows(target_values(target_row()))
    values = read_target_master_csv(path)
    assert values[0][0] == "target_id"
    result = validate_target_master(values)
    assert result.is_valid
    assert result.valid_rows.loc[0, "manufacturer_model_code"] == "EB14"


def test_missing_required_values_are_reported() -> None:
    result = validate_target_master(
        target_values(target_row(model="", cpu_series="   ", ram_capacity_gb="", status=""))
    )
    assert not result.is_valid
    assert error_codes(result) == [
        (2, "cpu_series", "MISSING_VALUE"),
        (2, "model", "MISSING_VALUE"),
        (2, "ram_capacity_gb", "MISSING_VALUE"),
        (2, "status", "MISSING_VALUE"),
    ]
    assert result.valid_rows.empty


def test_only_the_five_core_fields_and_row_control_are_required() -> None:
    core = ["model", "screen_size_in", "cpu_series", "ram_capacity_gb", "storage_capacity_gb"]
    assert list(TARGET_IDENTITY_COLUMNS) == core
    assert REQUIRED_TARGET_COLUMNS == ["target_id", "status", *core]


def test_missing_secondary_details_and_audit_fields_do_not_block_validation() -> None:
    secondary = ("brand", "resolution", "display_type", "refresh_rate_hz", "gpu_model", "os")
    audit = ("source_url", "reviewer", "reviewed_at")
    result = validate_target_master(
        target_values(target_row(**dict.fromkeys((*secondary, *audit), "")))
    )
    assert result.is_valid


@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("target_id", "TGT-1", "INVALID_FORMAT"),
        ("target_id", "TM-000001", "INVALID_FORMAT"),
        ("status", "active", "INVALID_VALUE"),
        ("ram_capacity_gb", "16GB", "INVALID_FORMAT"),
        ("ram_capacity_gb", "0", "INVALID_FORMAT"),
        ("ram_capacity_gb", "016", "INVALID_FORMAT"),
        ("storage_capacity_gb", "1.5", "INVALID_FORMAT"),
        ("storage_capacity_gb", "-512", "INVALID_FORMAT"),
        ("refresh_rate_hz", "60Hz", "INVALID_FORMAT"),
        ("screen_size_in", "15,6", "INVALID_FORMAT"),
        ("screen_size_in", "14 inch", "INVALID_FORMAT"),
        ("resolution", "1920 x 1080", "INVALID_FORMAT"),
        ("resolution", "FHD", "INVALID_FORMAT"),
        ("source_url", "example.com/product", "INVALID_FORMAT"),
        ("source_url", "ftp://example.com/product", "INVALID_FORMAT"),
        ("reviewed_at", "2026-02-30", "INVALID_DATE"),
        ("reviewed_at", "28/09/2026", "INVALID_DATE"),
        ("brand", " ExampleBrand", "SURROUNDING_WHITESPACE"),
        ("ram_capacity_gb", "16 ", "SURROUNDING_WHITESPACE"),
    ],
)
def test_malformed_values_are_reported(column: str, value: str, code: str) -> None:
    result = validate_target_master(target_values(target_row(**{column: value})))
    assert error_codes(result) == [(2, column, code)]
    assert result.errors.loc[0, "raw_value"] == value
    assert result.valid_rows.empty


def test_optional_fields_may_be_blank_or_omitted() -> None:
    optional = {
        column
        for column in TARGET_MASTER_HEADERS
        if column
        not in {
            "target_id",
            "status",
            "model",
            "screen_size_in",
            "cpu_series",
            "ram_capacity_gb",
            "storage_capacity_gb",
        }
    }
    blank = validate_target_master(target_values(target_row(**dict.fromkeys(optional, ""))))
    assert blank.is_valid

    required_only = [column for column in TARGET_MASTER_HEADERS if column not in optional]
    omitted = validate_target_master(
        target_values(target_row(required_only), headers=required_only)
    )
    assert omitted.is_valid
    assert omitted.valid_rows.loc[0, "color"] == ""
    assert list(omitted.valid_rows.columns) == ["source_row_number", *TARGET_MASTER_HEADERS]


def test_optional_secondary_fields_are_still_format_checked_when_filled() -> None:
    result = validate_target_master(target_values(target_row(resolution="1920X1080")))
    assert error_codes(result) == [(2, "resolution", "INVALID_FORMAT")]


def test_secondary_differences_do_not_create_a_second_configuration() -> None:
    result = validate_target_master(
        target_values(
            target_row(),
            target_row(target_id="TGT-000002", gpu_model="Other GPU", os="", resolution=""),
        )
    )
    assert [entry[2] for entry in error_codes(result)] == ["DUPLICATE_CONFIGURATION"] * 2


def test_duplicate_core_keys_use_the_matcher_normalization() -> None:
    result = validate_target_master(
        target_values(
            target_row(screen_size_in="14"),
            target_row(target_id="TGT-000002", screen_size_in="14.0", model="ExampleBook™ 14"),
        )
    )
    assert [entry[2] for entry in error_codes(result)] == ["DUPLICATE_CONFIGURATION"] * 2


def test_duplicate_configuration_flags_every_row_without_choosing_one() -> None:
    result = validate_target_master(
        target_values(
            target_row(),
            target_row(target_id="TGT-000002", color="Silver", brand="examplebrand"),
            target_row(target_id="TGT-000003", notes="unrelated"),
            target_row(target_id="TGT-000004", storage_capacity_gb="2000"),
        )
    )
    # Color, brand casing, and notes are not identity, so rows 2-4 are one configuration.
    duplicates = [entry for entry in error_codes(result) if entry[2] == "DUPLICATE_CONFIGURATION"]
    assert duplicates == [
        (2, "configuration_identity", "DUPLICATE_CONFIGURATION"),
        (3, "configuration_identity", "DUPLICATE_CONFIGURATION"),
        (4, "configuration_identity", "DUPLICATE_CONFIGURATION"),
    ]
    assert "also on row(s) 3;4" in result.errors.loc[0, "error_message"]
    assert result.valid_rows["target_id"].tolist() == ["TGT-000004"]


def test_missing_identity_values_do_not_match_each_other() -> None:
    result = validate_target_master(
        target_values(
            target_row(cpu_series=""),
            target_row(target_id="TGT-000002", cpu_series=""),
        )
    )
    assert not result.is_valid
    assert result.errors["error_code"].tolist() == ["MISSING_VALUE", "MISSING_VALUE"]


def test_duplicate_target_ids_are_rejected_case_insensitively() -> None:
    result = validate_target_master(
        target_values(
            target_row(),
            target_row(target_id="tgt-000001", storage_capacity_gb="2000"),
        )
    )
    # Lowercase IDs also fail the TGT- format, but still collide with the uppercase ID.
    assert error_codes(result) == [
        (2, "target_id", "DUPLICATE_TARGET_ID"),
        (3, "target_id", "INVALID_FORMAT"),
        (3, "target_id", "DUPLICATE_TARGET_ID"),
    ]
    assert result.valid_rows.empty


def test_identity_key_is_configurable_for_an_approved_change() -> None:
    values = target_values(target_row(), target_row(target_id="TGT-000002", color="Silver"))
    assert not validate_target_master(values).is_valid
    with_color = validate_target_master(
        values,
        identity_columns=(
            "brand",
            "manufacturer_model_code",
            "cpu_model",
            "gpu_model",
            "color",
        ),
    )
    assert with_color.is_valid
    with pytest.raises(ValueError, match="Unknown identity columns"):
        validate_target_master(values, identity_columns=("brand", "colour"))


def test_blank_rows_are_ignored_and_extra_cells_are_reported() -> None:
    result = validate_target_master(
        target_values(
            target_row(),
            ["", " "],
            [*target_row(target_id="TGT-000002", storage_capacity_gb="2000"), "stray"],
        )
    )
    assert error_codes(result) == [(4, "", "EXTRA_CELLS")]
    assert result.valid_rows["source_row_number"].tolist() == [2]


def test_header_only_table_is_not_valid() -> None:
    result = validate_target_master(target_values())
    assert not result.is_valid
    assert result.errors["error_code"].tolist() == ["NO_DATA_ROWS"]


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ([], "no header row"),
        (["", ""], "no header row"),
        ([*TARGET_MASTER_HEADERS, ""], "must not be blank"),
        ([*TARGET_MASTER_HEADERS, "brand"], "Duplicate"),
        ([h for h in TARGET_MASTER_HEADERS if h != "cpu_series"], "Missing required.*cpu_series"),
        ([*TARGET_MASTER_HEADERS, "ram_gbs"], "Unknown.*ram_gbs"),
    ],
)
def test_invalid_headers_stop_validation(headers: list[str], message: str) -> None:
    with pytest.raises(TargetMasterSchemaError, match=message):
        validate_target_master([headers] if headers else [])


def load_cli():
    spec = importlib.util.spec_from_file_location(
        "check_target_master", ROOT / "scripts" / "check_target_master.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    return path


def test_cli_passing_check_writes_exact_validated_copy(tmp_path: Path, capsys) -> None:
    source = write_csv(tmp_path / "in.csv", target_values(target_row(target_id="TGT-000007")))
    out = tmp_path / "out"
    assert load_cli().main([str(source), "--output-dir", str(out)]) == 0
    validated = read_target_master_csv(out / "target_master_validated.csv")
    assert validated[0] == ["source_row_number", *TARGET_MASTER_HEADERS]
    assert validated[1][1] == "TGT-000007"
    assert (out / "target_master_validation_errors.csv").is_file()
    assert "ExampleBrand" not in capsys.readouterr().out


def test_cli_failing_check_reports_counts_only_and_removes_stale_copy(
    tmp_path: Path, capsys
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "target_master_validated.csv").write_text("stale\n", encoding="utf-8")
    source = write_csv(
        tmp_path / "in.csv",
        target_values(target_row(), target_row(target_id="TM-0002", brand="EXAMPLEBRAND")),
    )
    assert load_cli().main([str(source), "--output-dir", str(out)]) == 1
    captured = capsys.readouterr()
    assert "DUPLICATE_CONFIGURATION: 2" in captured.out
    assert "EXAMPLEBRAND" not in captured.out + captured.err
    assert not (out / "target_master_validated.csv").exists()
    report = read_target_master_csv(out / "target_master_validation_errors.csv")
    assert len(report) == 4  # header, two duplicate rows, and the TM-0002 format error


def test_cli_schema_error_fails_safely(tmp_path: Path, capsys) -> None:
    source = write_csv(tmp_path / "in.csv", [["target_id", "brand"]])
    assert load_cli().main([str(source), "--output-dir", str(tmp_path / "out")]) == 1
    assert "Missing required TARGET_MASTER columns" in capsys.readouterr().err
