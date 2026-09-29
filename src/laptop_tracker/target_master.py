"""Proposed TARGET_MASTER schema and a local, credential-free configuration validator.

The column set, required fields, and configuration identity key below are a PROPOSAL pending
business approval (see docs/TARGET_MASTER_SCHEMA.md). They are defined as data so an approved
change is a one-place edit. Values are never coerced: every cell stays the exact string that was
read, and comparison-only normalization is used solely to detect duplicate identities.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class TargetColumn:
    name: str
    required: bool
    description: str
    pattern: str | None = None
    format_hint: str = ""
    allowed: tuple[str, ...] = ()
    free_text: bool = False
    is_date: bool = False


TARGET_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
POSITIVE_INTEGER_PATTERN = r"[1-9][0-9]*"

# Order is the proposed worksheet header order (config/target_master_template.csv).
TARGET_MASTER_COLUMNS: tuple[TargetColumn, ...] = (
    TargetColumn(
        "target_id",
        True,
        "Stable configuration identifier.",
        TARGET_ID_PATTERN,
        "1-64 letters, digits, '.', '_' or '-', starting with a letter or digit",
    ),
    TargetColumn(
        "status", True, "Whether the configuration is tracked.", allowed=("ACTIVE", "INACTIVE")
    ),
    TargetColumn("brand", True, "Manufacturer brand."),
    TargetColumn("series", False, "Product line or series."),
    TargetColumn("model", True, "Model name or number."),
    TargetColumn("cpu", True, "Processor as the manufacturer names it."),
    TargetColumn(
        "ram_gb", True, "Installed memory in GB.", POSITIVE_INTEGER_PATTERN, "positive integer"
    ),
    TargetColumn(
        "storage_gb", True, "Total storage in GB.", POSITIVE_INTEGER_PATTERN, "positive integer"
    ),
    TargetColumn("storage_type", False, "Storage technology."),
    TargetColumn(
        "display_size_in",
        True,
        "Diagonal display size in inches.",
        r"[1-9][0-9]?(\.[0-9]{1,2})?",
        "positive number such as 14 or 15.6",
    ),
    TargetColumn(
        "display_resolution",
        False,
        "Native resolution.",
        r"[1-9][0-9]{2,4}x[1-9][0-9]{2,4}",
        "WIDTHxHEIGHT such as 1920x1080",
    ),
    TargetColumn("gpu", False, "Discrete or named graphics, if a distinguishing variant."),
    TargetColumn("os", False, "Preinstalled operating system."),
    TargetColumn("color", False, "Color variant."),
    TargetColumn("variant_notes", False, "Other distinguishing variant details.", free_text=True),
    TargetColumn("manufacturer_part_number", False, "Manufacturer SKU or part number."),
    TargetColumn(
        "source_model_id",
        False,
        "Shopee model_id from the reviewed Phase 4 targets.",
        r"[0-9]+",
        "digits only",
    ),
    TargetColumn("reviewer", True, "Person who approved the row."),
    TargetColumn("reviewed_at", True, "Approval date.", format_hint="YYYY-MM-DD", is_date=True),
    TargetColumn("notes", False, "Free-text notes.", free_text=True),
)

TARGET_MASTER_HEADERS = [column.name for column in TARGET_MASTER_COLUMNS]
REQUIRED_TARGET_COLUMNS = [column.name for column in TARGET_MASTER_COLUMNS if column.required]

# Proposed exact-configuration identity: attributes that distinguish a separately priced
# configuration. Color, OS, part number, status, and review fields are deliberately excluded
# pending business approval.
PROPOSED_IDENTITY_COLUMNS: tuple[str, ...] = (
    "brand",
    "series",
    "model",
    "cpu",
    "ram_gb",
    "storage_gb",
    "storage_type",
    "display_size_in",
    "display_resolution",
    "gpu",
)

TARGET_ERROR_COLUMNS = [
    "source_row_number",
    "column_name",
    "raw_value",
    "error_code",
    "error_message",
]


class TargetMasterSchemaError(ValueError):
    """Raised when the header row cannot be validated, so no row check is meaningful."""


@dataclass
class TargetMasterResult:
    rows: pd.DataFrame
    valid_rows: pd.DataFrame
    errors: pd.DataFrame

    @property
    def is_valid(self) -> bool:
        return self.errors.empty and not self.rows.empty


def identity_text(value: str) -> str:
    """Comparison-only form: case-insensitive with collapsed whitespace; never stored."""
    return re.sub(r"\s+", " ", value).strip().casefold()


def validate_target_headers(headers: Sequence[object]) -> list[str]:
    normalized = ["" if header is None else str(header).strip() for header in headers]
    if not normalized or all(header == "" for header in normalized):
        raise TargetMasterSchemaError("TARGET_MASTER has no header row.")
    if "" in normalized:
        raise TargetMasterSchemaError("TARGET_MASTER headers must not be blank.")
    duplicates = sorted({header for header in normalized if normalized.count(header) > 1})
    if duplicates:
        raise TargetMasterSchemaError(
            f"Duplicate TARGET_MASTER headers are not supported: {', '.join(duplicates)}"
        )
    missing = [column for column in REQUIRED_TARGET_COLUMNS if column not in normalized]
    if missing:
        raise TargetMasterSchemaError(
            f"Missing required TARGET_MASTER columns: {', '.join(missing)}"
        )
    unknown = [header for header in normalized if header not in TARGET_MASTER_HEADERS]
    if unknown:
        raise TargetMasterSchemaError(
            f"Unknown TARGET_MASTER columns (check spelling): {', '.join(unknown)}"
        )
    return normalized


def validate_target_master(
    values: Sequence[Sequence[object]],
    identity_columns: Sequence[str] = PROPOSED_IDENTITY_COLUMNS,
) -> TargetMasterResult:
    """Validate worksheet-shaped values (header row first), e.g. ``get_all_values`` or a CSV.

    Omitted optional columns are treated as blank. Entirely blank rows are ignored. Rows with
    any error, including every row of a duplicate group, are excluded from ``valid_rows``.
    """
    if not values:
        raise TargetMasterSchemaError("TARGET_MASTER has no header row.")
    headers = validate_target_headers(values[0])
    unknown_identity = [
        column for column in identity_columns if column not in TARGET_MASTER_HEADERS
    ]
    if unknown_identity:
        raise ValueError(f"Unknown identity columns: {', '.join(unknown_identity)}")
    width = len(headers)
    errors: list[dict[str, object]] = []

    def add_error(row_number: int, column: str, raw_value: str, code: str, message: str) -> None:
        errors.append(
            {
                "source_row_number": row_number,
                "column_name": column,
                "raw_value": raw_value,
                "error_code": code,
                "error_message": message,
            }
        )

    records: list[dict[str, object]] = []
    for row_number, row in enumerate(values[1:], start=2):
        cells = ["" if value is None else str(value) for value in row]
        if all(cell.strip() == "" for cell in cells):
            continue
        extra = [cell for cell in cells[width:] if cell.strip() != ""]
        if extra:
            add_error(
                row_number,
                "",
                "",
                "EXTRA_CELLS",
                f"Row has {len(extra)} non-blank cell(s) beyond the header columns.",
            )
        cells = cells[:width] + [""] * (width - len(cells))
        by_header = dict(zip(headers, cells, strict=True))
        record: dict[str, object] = {"source_row_number": row_number}
        record |= {name: by_header.get(name, "") for name in TARGET_MASTER_HEADERS}
        records.append(record)

    for record in records:
        row_number = int(record["source_row_number"])  # type: ignore[arg-type]
        for column in TARGET_MASTER_COLUMNS:
            raw = str(record[column.name])
            if raw.strip() == "":
                if column.required:
                    add_error(
                        row_number,
                        column.name,
                        raw,
                        "MISSING_VALUE",
                        f"{column.name} is required.",
                    )
                continue
            if not column.free_text and raw != raw.strip():
                add_error(
                    row_number,
                    column.name,
                    raw,
                    "SURROUNDING_WHITESPACE",
                    f"{column.name} has leading or trailing whitespace; values are not trimmed.",
                )
                continue
            if column.allowed and raw not in column.allowed:
                add_error(
                    row_number,
                    column.name,
                    raw,
                    "INVALID_VALUE",
                    f"{column.name} must be one of: {', '.join(column.allowed)}.",
                )
            if column.pattern and not re.fullmatch(column.pattern, raw):
                add_error(
                    row_number,
                    column.name,
                    raw,
                    "INVALID_FORMAT",
                    f"{column.name} must be {column.format_hint}.",
                )
            if column.is_date and not _is_iso_date(raw):
                add_error(
                    row_number,
                    column.name,
                    raw,
                    "INVALID_DATE",
                    f"{column.name} must be a valid {column.format_hint} date.",
                )

    _add_duplicate_errors(
        records,
        ["target_id"],
        "target_id",
        "DUPLICATE_TARGET_ID",
        "target_id is not unique (case-insensitive)",
        add_error,
    )
    _add_duplicate_errors(
        records,
        list(identity_columns),
        "configuration_identity",
        "DUPLICATE_CONFIGURATION",
        "Configuration identity (" + ", ".join(identity_columns) + ") is not unique",
        add_error,
    )

    rows = pd.DataFrame(records, columns=["source_row_number", *TARGET_MASTER_HEADERS])
    for column in TARGET_MASTER_HEADERS:
        rows[column] = rows[column].astype("string")
    error_frame = (
        pd.DataFrame(errors, columns=TARGET_ERROR_COLUMNS)
        .sort_values(["source_row_number", "column_name"], kind="stable")
        .reset_index(drop=True)
    )
    if rows.empty:
        error_frame = pd.DataFrame(
            [
                {
                    "source_row_number": 1,
                    "column_name": "",
                    "raw_value": "",
                    "error_code": "NO_DATA_ROWS",
                    "error_message": "TARGET_MASTER has headers but no configuration rows.",
                }
            ],
            columns=TARGET_ERROR_COLUMNS,
        )
    invalid_rows = set(error_frame["source_row_number"].astype(int))
    valid_rows = rows.loc[~rows["source_row_number"].isin(invalid_rows)].reset_index(drop=True)
    return TargetMasterResult(rows=rows, valid_rows=valid_rows, errors=error_frame)


def _is_iso_date(value: str) -> bool:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _add_duplicate_errors(
    records: list[dict[str, object]],
    key_columns: list[str],
    column_label: str,
    code: str,
    message: str,
    add_error,
) -> None:
    """Flag every row of a duplicate group; no row is chosen over another.

    Rows missing a required key value are skipped here because they already fail validation.
    Blank optional key values participate as blanks, so "blank" and "SSD" are distinct.
    """
    required = set(REQUIRED_TARGET_COLUMNS)
    groups: dict[tuple[str, ...], list[int]] = {}
    for record in records:
        values = [str(record[column]) for column in key_columns]
        if any(
            column in required and value.strip() == ""
            for column, value in zip(key_columns, values, strict=True)
        ):
            continue
        key = tuple(identity_text(value) for value in values)
        groups.setdefault(key, []).append(int(record["source_row_number"]))  # type: ignore[arg-type]
    for row_numbers in groups.values():
        if len(row_numbers) < 2:
            continue
        for row_number in row_numbers:
            others = ";".join(str(number) for number in row_numbers if number != row_number)
            add_error(row_number, column_label, "", code, f"{message}; also on row(s) {others}.")


def read_target_master_csv(path: str | Path) -> list[list[str]]:
    """Read a local CSV as exact strings (no type inference); a UTF-8 BOM is tolerated."""
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return [list(row) for row in csv.reader(handle)]
