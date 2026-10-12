"""Approved TARGET_MASTER schema and local, credential-free catalog validator.

Schema decisions are documented in docs/TARGET_MASTER_SCHEMA.md. Values are never coerced: every
cell stays the exact string that was read, and comparison-only normalization is used solely to
detect duplicate identities.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd

from .normalization import CORE_MATCH_COLUMNS, core_value_key, is_blank


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
    is_url: bool = False


TARGET_ID_PATTERN = r"TGT-[0-9]{6}"
POSITIVE_INTEGER_PATTERN = r"[1-9][0-9]*"

CONFIGURATION = "CONFIGURATION"
LISTING_ONLY = "LISTING_ONLY"
TRACKING_MODES = (CONFIGURATION, LISTING_ONLY)
# A LISTING_ONLY target is identified by its Shopee listing variation, exactly as text.
LISTING_IDENTITY_COLUMNS: tuple[str, ...] = ("shop_id", "item_id", "source_model_id")
LISTING_CLASSIFICATION_COLUMNS: tuple[str, ...] = (
    "tracking_mode_reason",
    "classification_evidence",
)
# A LISTING_ONLY product has no verifiable model name/code, so these must stay blank.
LISTING_ONLY_BLANK_COLUMNS: tuple[str, ...] = ("model", "manufacturer_model_code")
# Placeholder words are not product models; never accept them as one (policy 2026-10-08).
PLACEHOLDER_MODEL_TEXT = frozenset(
    {
        "generic",
        "unbranded",
        "no brand",
        "nobrand",
        "non-branded",
        "oem",
        "none",
        "n/a",
        "na",
        "unknown",
        "no model",
        "-",
    }
)


def effective_tracking_mode(value: object) -> str:
    """Blank (legacy) means CONFIGURATION; any other text is returned unchanged for checking."""
    return CONFIGURATION if is_blank(value) else str(value)


def is_placeholder_model(value: str) -> bool:
    return re.sub(r"\s+", " ", value).strip().casefold() in PLACEHOLDER_MODEL_TEXT

# Order is the approved worksheet header order (config/target_master_template.csv).
TARGET_MASTER_COLUMNS: tuple[TargetColumn, ...] = (
    TargetColumn(
        "target_id",
        True,
        "Stable configuration identifier; never reused after retirement.",
        TARGET_ID_PATTERN,
        "TGT- followed by exactly six digits, for example TGT-000001",
    ),
    TargetColumn(
        "status", True, "Whether the configuration is tracked.", allowed=("ACTIVE", "INACTIVE")
    ),
    TargetColumn("brand", False, "Manufacturer brand, when known; context, not core identity."),
    TargetColumn("model", True, "Exact product model name/code; core identity."),
    TargetColumn(
        "screen_size_in",
        True,
        "Diagonal display size in inches; core identity.",
        r"[1-9][0-9]?(\.[0-9]{1,2})?",
        "positive number such as 14 or 15.6",
    ),
    TargetColumn(
        "resolution",
        False,
        "Native display resolution, when known; secondary corroboration.",
        r"[1-9][0-9]{2,4}x[1-9][0-9]{2,4}",
        "WIDTHxHEIGHT such as 1920x1080",
    ),
    TargetColumn("display_type", False, "Display panel type, when known; secondary."),
    TargetColumn(
        "refresh_rate_hz",
        False,
        "Display refresh rate in Hz, when known; secondary.",
        POSITIVE_INTEGER_PATTERN,
        "positive integer",
    ),
    TargetColumn("cpu_brand", False, "CPU manufacturer/brand."),
    TargetColumn("cpu_series", True, "Exact CPU series; core identity."),
    TargetColumn("cpu_generation", False, "CPU generation, if verified."),
    TargetColumn(
        "cpu_cores",
        False,
        "CPU core count, if verified.",
        POSITIVE_INTEGER_PATTERN,
        "positive integer",
    ),
    TargetColumn(
        "ram_capacity_gb",
        True,
        "Installed memory capacity in GB; core identity.",
        POSITIVE_INTEGER_PATTERN,
        "positive integer",
    ),
    TargetColumn("ram_type", False, "Memory type as shown by the product evidence."),
    TargetColumn(
        "storage_capacity_gb",
        True,
        "Total storage capacity in decimal GB (1 TB = 1000 GB); core identity.",
        POSITIVE_INTEGER_PATTERN,
        "positive integer",
    ),
    TargetColumn("storage_type", False, "Storage technology or combination, as verified."),
    TargetColumn(
        "storage_layout",
        False,
        "Per-drive capacities and types, for example 1x512GB SSD + 1x1000GB HDD.",
    ),
    TargetColumn("gpu_type", False, "Graphics type, if verified."),
    TargetColumn("gpu_brand", False, "GPU brand, if verified."),
    TargetColumn("gpu_model", False, "Exact GPU model, when known; secondary."),
    TargetColumn("vram", False, "GPU memory amount/type as stated by the evidence."),
    TargetColumn("os", False, "Exact preinstalled OS edition, when known; secondary."),
    TargetColumn("battery_capacity", False, "Battery capacity as stated by the evidence."),
    TargetColumn("build_material", False, "Chassis/build material as stated by the evidence."),
    TargetColumn("manufacturer_model_code", False, "Manufacturer model code/SKU, useful evidence."),
    TargetColumn("series", False, "Product line or series; descriptive, not identity."),
    TargetColumn("cpu_model", False, "Full CPU model string, including suffix, if available."),
    TargetColumn("color", False, "Color variant."),
    TargetColumn("variant_notes", False, "Other descriptive variant details.", free_text=True),
    TargetColumn("manufacturer_part_number", False, "Manufacturer SKU or part number."),
    TargetColumn(
        "source_url",
        False,
        "Evidence URL a reviewer can use to verify this configuration, when available.",
        is_url=True,
    ),
    TargetColumn(
        "reviewer",
        False,
        "Approval source, e.g. the owner-approved five-core-field rule label.",
    ),
    TargetColumn(
        "reviewed_at", False, "Promotion date, if recorded.", format_hint="YYYY-MM-DD", is_date=True
    ),
    TargetColumn("notes", False, "Free-text notes.", free_text=True),
    # Tracking-mode columns (approved 2026-10-08). They follow `notes` so existing sheet columns
    # keep their positions; older snapshots without them are read as CONFIGURATION rows.
    TargetColumn(
        "tracking_mode",
        False,
        "CONFIGURATION or LISTING_ONLY; blank means CONFIGURATION (legacy rows).",
        allowed=TRACKING_MODES,
    ),
    TargetColumn(
        "shop_id", False, "LISTING_ONLY: Shopee shop ID, kept as text.", r"[0-9]+", "digits only"
    ),
    TargetColumn(
        "item_id", False, "LISTING_ONLY: Shopee item ID, kept as text.", r"[0-9]+", "digits only"
    ),
    TargetColumn(
        "source_model_id",
        False,
        "LISTING_ONLY: Shopee variation/model ID, kept as text.",
        r"[0-9]+",
        "digits only",
    ),
    TargetColumn(
        "tracking_mode_reason",
        False,
        "LISTING_ONLY: why no verifiable product model name/code exists.",
        free_text=True,
    ),
    TargetColumn(
        "classification_evidence",
        False,
        "LISTING_ONLY: evidence supporting the classification (what was checked).",
        free_text=True,
    ),
)

TARGET_MASTER_HEADERS = [column.name for column in TARGET_MASTER_COLUMNS]
TRACKING_MODE_COLUMNS: tuple[str, ...] = (
    "tracking_mode",
    "shop_id",
    "item_id",
    "source_model_id",
    "tracking_mode_reason",
    "classification_evidence",
)
# Header order of every TARGET_MASTER snapshot made before tracking modes existed.
LEGACY_TARGET_MASTER_HEADERS = [
    name for name in TARGET_MASTER_HEADERS if name not in TRACKING_MODE_COLUMNS
]
REQUIRED_TARGET_COLUMNS = [column.name for column in TARGET_MASTER_COLUMNS if column.required]
TARGET_PRODUCT_REQUIRED_COLUMNS = [
    column.name
    for column in TARGET_MASTER_COLUMNS
    if column.required
    and column.name
    not in {"target_id", "status", "source_url", "reviewer", "reviewed_at"}
]

# Five-field core identity set by the user on 2026-10-05 (supersedes the 2026-09-29 ten-field
# key). Secondary details are optional and never part of this key. Missing key values are
# invalid and are never grouped as a matching identity.
TARGET_IDENTITY_COLUMNS: tuple[str, ...] = CORE_MATCH_COLUMNS

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
    identity_columns: Sequence[str] = TARGET_IDENTITY_COLUMNS,
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
        mode = effective_tracking_mode(record["tracking_mode"])
        for column in TARGET_MASTER_COLUMNS:
            if mode == LISTING_ONLY and column.name in CORE_MATCH_COLUMNS:
                # Listing-only products keep unavailable specifications blank.
                column = replace(column, required=False)
            check_cell(row_number, column, str(record[column.name]), add_error)
        check_tracking_mode(row_number, record, add_error, configuration_allows_listing=False)

    _add_duplicate_errors(
        records,
        ["target_id"],
        "target_id",
        "DUPLICATE_TARGET_ID",
        "target_id is not unique (case-insensitive)",
        add_error,
    )
    # Only CONFIGURATION rows share the five-field key; listing-only rows never take part.
    _add_duplicate_errors(
        [r for r in records if effective_tracking_mode(r["tracking_mode"]) == CONFIGURATION],
        list(identity_columns),
        "configuration_identity",
        "DUPLICATE_CONFIGURATION",
        "Configuration identity (" + ", ".join(identity_columns) + ") is not unique",
        add_error,
    )
    _add_listing_duplicate_errors(
        [r for r in records if effective_tracking_mode(r["tracking_mode"]) == LISTING_ONLY],
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
                    "error_message": "TARGET_MASTER has headers but no data rows.",
                }
            ],
            columns=TARGET_ERROR_COLUMNS,
        )
    invalid_rows = set(error_frame["source_row_number"].astype(int))
    valid_rows = rows.loc[~rows["source_row_number"].isin(invalid_rows)].reset_index(drop=True)
    return TargetMasterResult(rows=rows, valid_rows=valid_rows, errors=error_frame)


def check_cell(row_number: int, column: TargetColumn, raw: str, add_error) -> None:
    """Report blank-required, whitespace, allowed-value, format, date, and URL errors.

    Shared with the supporting-table validators so nonblank values use identical formats.
    """
    if raw.strip() == "":
        if column.required:
            add_error(row_number, column.name, raw, "MISSING_VALUE", f"{column.name} is required.")
        return
    if not column.free_text and raw != raw.strip():
        add_error(
            row_number,
            column.name,
            raw,
            "SURROUNDING_WHITESPACE",
            f"{column.name} has leading or trailing whitespace; values are not trimmed.",
        )
        return
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
    if column.is_url and not _is_http_url(raw):
        add_error(
            row_number,
            column.name,
            raw,
            "INVALID_FORMAT",
            f"{column.name} must be an absolute HTTP or HTTPS URL.",
        )


def _is_iso_date(value: str) -> bool:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_http_url(value: str) -> bool:
    if any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _add_duplicate_errors(
    records: list[dict[str, object]],
    key_columns: list[str],
    column_label: str,
    code: str,
    message: str,
    add_error,
) -> None:
    """Flag every row of a duplicate group; no row is chosen over another.

    Rows missing any key value are skipped because incomplete identity must go to review, not
    match another incomplete row. Values are normalized for comparison only.
    """
    groups: dict[tuple[str, ...], list[int]] = {}
    for record in records:
        values = [str(record[column]) for column in key_columns]
        if any(value.strip() == "" for value in values):
            continue
        key = tuple(
            _identity_key(column, value) for column, value in zip(key_columns, values, strict=True)
        )
        groups.setdefault(key, []).append(int(record["source_row_number"]))  # type: ignore[arg-type]
    for row_numbers in groups.values():
        if len(row_numbers) < 2:
            continue
        for row_number in row_numbers:
            others = ";".join(str(number) for number in row_numbers if number != row_number)
            add_error(row_number, column_label, "", code, f"{message}; also on row(s) {others}.")


def check_tracking_mode(
    row_number: int,
    record: dict[str, object],
    add_error,
    *,
    configuration_allows_listing: bool,
) -> None:
    """Mode-specific requirements and contradictions for one TARGET_MASTER or review record.

    CONFIGURATION: a placeholder such as "Generic" is never a model. In TARGET_MASTER the listing
    identity columns must be blank, because source listings link through SOURCE_MODEL_MAP;
    review candidates (``configuration_allows_listing=True``) may keep them as trace evidence.
    LISTING_ONLY: listing identity, reason, and evidence are required, and model fields must stay
    blank. A blank model alone never makes a record LISTING_ONLY. Unknown modes are reported by
    the cell check and skipped here.
    """
    mode = effective_tracking_mode(record.get("tracking_mode"))

    def value(name: str) -> str:
        return str(record.get(name, ""))

    if mode == CONFIGURATION:
        model = value("model")
        if model.strip() and is_placeholder_model(model):
            add_error(
                row_number,
                "model",
                model,
                "PLACEHOLDER_MODEL",
                "model is a placeholder, not a verified product model; research it or classify "
                "the product as LISTING_ONLY with evidence.",
            )
        if not configuration_allows_listing:
            for name in LISTING_IDENTITY_COLUMNS:
                if value(name).strip():
                    add_error(
                        row_number,
                        name,
                        value(name),
                        "CONTRADICTORY_TRACKING_MODE",
                        f"{name} must be blank for CONFIGURATION targets; link source listings "
                        "in SOURCE_MODEL_MAP.",
                    )
    elif mode == LISTING_ONLY:
        for name in (*LISTING_IDENTITY_COLUMNS, *LISTING_CLASSIFICATION_COLUMNS):
            if not value(name).strip():
                add_error(
                    row_number, name, "", "MISSING_VALUE", f"{name} is required for LISTING_ONLY."
                )
        for name in LISTING_ONLY_BLANK_COLUMNS:
            if value(name).strip():
                add_error(
                    row_number,
                    name,
                    value(name),
                    "CONTRADICTORY_TRACKING_MODE",
                    f"{name} must be blank for LISTING_ONLY; a verified model means CONFIGURATION.",
                )


def _add_listing_duplicate_errors(records: list[dict[str, object]], add_error) -> None:
    """Flag LISTING_ONLY rows sharing a source_model_id, exactly as text.

    Rows with the same shop_id, item_id, and source_model_id are DUPLICATE_LISTING; the same
    source_model_id with a different shop or item is CONFLICTING_LISTING_IDENTITY. No row is
    chosen over another.
    """
    groups: dict[str, list[dict[str, object]]] = {}
    for record in records:
        source_id = str(record["source_model_id"])
        if source_id.strip():
            groups.setdefault(source_id, []).append(record)
    for group in groups.values():
        if len(group) < 2:
            continue
        identities = {tuple(str(r[name]) for name in LISTING_IDENTITY_COLUMNS) for r in group}
        code = "DUPLICATE_LISTING" if len(identities) == 1 else "CONFLICTING_LISTING_IDENTITY"
        numbers = [int(r["source_row_number"]) for r in group]  # type: ignore[arg-type]
        for number in numbers:
            others = ";".join(str(other) for other in numbers if other != number)
            add_error(
                number,
                "listing_identity",
                "",
                code,
                f"LISTING_ONLY source_model_id is used by more than one target; also on row(s) "
                f"{others}.",
            )


def _identity_key(column: str, value: str) -> object:
    """Core fields use the matcher's comparison key, so ``14`` and ``14.0`` collide here too."""
    if column in CORE_MATCH_COLUMNS:
        try:
            return core_value_key(column, value)
        except ValueError:
            pass  # The malformed cell is reported separately; compare its text form instead.
    return identity_text(value)


def read_target_master_csv(path: str | Path) -> list[list[str]]:
    """Read a local CSV as exact strings (no type inference); a UTF-8 BOM is tolerated."""
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return [list(row) for row in csv.reader(handle)]


def read_table_snapshot_csv(path: str | Path) -> list[list[str]]:
    """Read a Phase 5 table snapshot: a sheet export or a ``*_validated.csv`` copy.

    Validated copies written by ``check_target_tables.py``/``check_target_master.py`` start with
    a ``source_row_number`` column, which is dropped so the table validators see the template
    headers. The remaining cells are kept as exact strings and are validated again by the caller.
    """
    values = read_target_master_csv(path)
    if values and values[0] and values[0][0].strip() == "source_row_number":
        return [row[1:] for row in values]
    return values
