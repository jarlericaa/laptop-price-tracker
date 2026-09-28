"""Raw worksheet schema and lossless normalization helpers."""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

REQUIRED_RAW_COLUMNS = [
    "grass_date",
    "shopid",
    "shop_name",
    "is_cross_border",
    "is_official_store",
    "is_managed",
    "itemid",
    "item_name",
    "modelid",
    "model_name",
    "current_price",
    "total_orders",
    "gmv_usd",
    "amount",
]

INTERNAL_COLUMN_MAP = {
    "grass_date": "period_date",
    "shopid": "shop_id",
    "itemid": "item_id",
    "modelid": "model_id",
    "model_name": "shopee_model_name",
    "current_price": "shopee_current_price_php",
    "gmv_usd": "total_gmv_usd",
}

ID_COLUMNS = ("shop_id", "item_id", "model_id")


class MissingColumnsError(ValueError):
    """Raised when the source layout does not provide the Phase 1 required fields."""


def clean_header(value: object) -> str:
    return str(value).strip()


def validate_headers(headers: Sequence[object]) -> list[str]:
    normalized = [clean_header(header) for header in headers]
    missing = [column for column in REQUIRED_RAW_COLUMNS if column not in normalized]
    if missing:
        raise MissingColumnsError(f"Missing required raw columns: {', '.join(missing)}")
    duplicates = sorted({column for column in normalized if normalized.count(column) > 1})
    if duplicates:
        raise MissingColumnsError(
            f"Duplicate raw headers are not supported: {', '.join(duplicates)}"
        )
    return normalized


def normalize_raw_values(values: Sequence[Sequence[object]], run_id: str) -> pd.DataFrame:
    """Turn ``get_all_values`` output into a lossless, string-first DataFrame.

    Google Sheets returns strings through ``get_all_values``. Explicit conversion here guards
    local fixtures too and prevents integer identifiers (and numeric-looking model names) from
    being converted into floats or missing values.
    """
    if not values:
        raise MissingColumnsError("The raw worksheet is empty and has no header row.")
    headers = validate_headers(values[0])
    width = len(headers)
    rows: list[list[str]] = []
    for row in values[1:]:
        cells = ["" if value is None else str(value) for value in row[:width]]
        rows.append(cells + [""] * (width - len(cells)))

    frame = pd.DataFrame(rows, columns=headers, dtype="string")
    frame.insert(0, "source_row_number", range(2, len(frame) + 2))
    frame["run_id"] = run_id
    frame = frame.rename(columns=INTERNAL_COLUMN_MAP)
    for column in ID_COLUMNS:
        frame[column] = frame[column].astype("string").fillna("")
    frame["shopee_model_name"] = frame["shopee_model_name"].astype("string").fillna("")
    frame["source_key"] = frame["shop_id"] + "|" + frame["item_id"] + "|" + frame["model_id"]
    frame["period_month"] = pd.Series(pd.NA, index=frame.index, dtype="string")
    return frame
