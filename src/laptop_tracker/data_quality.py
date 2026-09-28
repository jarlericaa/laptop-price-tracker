"""Validation, duplicate quarantine, and model identity checks."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .schema import ID_COLUMNS, INTERNAL_COLUMN_MAP, REQUIRED_RAW_COLUMNS

VALIDATION_ERROR_COLUMNS = [
    "run_id",
    "source_row_number",
    "column_name",
    "raw_value",
    "error_code",
    "error_message",
]
CONFLICT_COLUMNS = [
    "period_month",
    "shop_id",
    "item_id",
    "model_id",
    "duplicate_count",
    "differing_fields",
    "gmv_values",
    "price_values",
    "source_row_numbers",
    "resolution_status",
    "notes",
]
DUPLICATE_KEY_COLUMNS = ["period_month", "shop_id", "item_id", "model_id"]
# The review template and config/duplicate_decisions.csv share one redacted layout: keys,
# row numbers, field names, and a content fingerprint only; never GMV, prices, or names.
DUPLICATE_DECISION_COLUMNS = [
    *DUPLICATE_KEY_COLUMNS,
    "duplicate_count",
    "differing_fields",
    "source_row_numbers",
    "conflict_fingerprint",
    "decision",
    "keep_source_row_number",
    "reviewer",
    "reviewed_at",
    "reason",
]
VALID_DUPLICATE_DECISIONS = {"EXCLUDE_KEY", "KEEP_SOURCE_ROW"}
# Conflicts in these statuses have no applicable human decision and block publication.
UNRESOLVED_CONFLICT_STATUSES = {"EXCLUDED", "MODEL_IDENTITY_CONFLICT"}
COMPARE_COLUMNS = [INTERNAL_COLUMN_MAP.get(column, column) for column in REQUIRED_RAW_COLUMNS]


@dataclass
class ValidationResult:
    valid_rows: pd.DataFrame
    errors: pd.DataFrame


def _text(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _numeric(series: pd.Series) -> pd.Series:
    cleaned = series.astype("string").str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(cleaned, errors="coerce")


def validate_rows(frame: pd.DataFrame) -> ValidationResult:
    """Validate individual rows and return invalid rows as explicit quarantine records."""
    working = frame.copy()
    error_records: list[dict[str, object]] = []
    invalid = pd.Series(False, index=working.index)

    def add_error(index: object, column: str, raw_value: object, code: str, message: str) -> None:
        nonlocal invalid
        row = working.loc[index]
        error_records.append(
            {
                "run_id": row["run_id"],
                "source_row_number": row["source_row_number"],
                "column_name": column,
                "raw_value": "" if pd.isna(raw_value) else str(raw_value),
                "error_code": code,
                "error_message": message,
            }
        )
        invalid.loc[index] = True

    parsed_dates = pd.to_datetime(working["period_date"], errors="coerce")
    for index in working.index[parsed_dates.isna()]:
        add_error(
            index,
            "grass_date",
            working.at[index, "period_date"],
            "INVALID_DATE",
            "Date is not parseable.",
        )
    working["_parsed_period_date"] = parsed_dates
    working["period_month"] = parsed_dates.dt.to_period("M").astype("string")

    for column in ID_COLUMNS:
        raw_name = next(raw for raw, internal in INTERNAL_COLUMN_MAP.items() if internal == column)
        values = working[column].astype("string").fillna("").str.strip()
        for index in working.index[values.eq("")]:
            add_error(index, raw_name, working.at[index, column], "MISSING_ID", "ID is required.")
        for index in working.index[values.ne("") & ~values.str.fullmatch(r"[0-9]+")]:
            add_error(
                index,
                raw_name,
                working.at[index, column],
                "INVALID_ID",
                "ID must be an integer-like string.",
            )
        working[column] = values

    for column, raw_name in (("item_name", "item_name"), ("shopee_model_name", "model_name")):
        empty = working[column].astype("string").fillna("").str.strip().eq("")
        for index in working.index[empty]:
            add_error(
                index,
                raw_name,
                working.at[index, column],
                "MISSING_VALUE",
                f"{raw_name} is required.",
            )

    numeric_specs = (
        ("shopee_current_price_php", "current_price", "_price_numeric"),
        ("total_gmv_usd", "gmv_usd", "_gmv_numeric"),
    )
    for column, raw_name, numeric_name in numeric_specs:
        numeric = _numeric(working[column])
        working[numeric_name] = numeric
        non_finite = numeric.notna() & ~numeric.map(math.isfinite)
        invalid_numeric = numeric.isna() | non_finite | numeric.lt(0)
        for index in working.index[invalid_numeric]:
            raw_value = working.at[index, column]
            code = "MISSING_NUMERIC" if _text(raw_value) == "" else "INVALID_NUMERIC"
            add_error(
                index,
                raw_name,
                raw_value,
                code,
                f"{raw_name} must be a finite, non-negative number.",
            )

    working["source_key"] = (
        working["shop_id"] + "|" + working["item_id"] + "|" + working["model_id"]
    )
    errors = pd.DataFrame(error_records, columns=VALIDATION_ERROR_COLUMNS)
    return ValidationResult(valid_rows=working.loc[~invalid].copy(), errors=errors)


def collapse_duplicates(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Retain an exact copy once and quarantine conflicting composite duplicate keys."""
    if frame.empty:
        return frame.copy(), pd.DataFrame(columns=CONFLICT_COLUMNS)

    retained: list[pd.DataFrame] = []
    conflicts: list[dict[str, object]] = []

    for key, group in frame.groupby(DUPLICATE_KEY_COLUMNS, sort=True, dropna=False):
        group = group.sort_values("source_row_number", kind="stable")
        count = len(group)
        differing = [
            column for column in COMPARE_COLUMNS if group[column].nunique(dropna=False) > 1
        ]
        if count == 1:
            row = group.iloc[[0]].copy()
            row["duplicate_status"] = "UNIQUE"
            row["duplicate_count"] = 1
            retained.append(row)
        elif not differing:
            row = group.iloc[[0]].copy()
            row["duplicate_status"] = "EXACT_DUPLICATE"
            row["duplicate_count"] = count
            retained.append(row)
        else:
            period_month, shop_id, item_id, model_id = key
            conflicts.append(
                {
                    "period_month": period_month,
                    "shop_id": shop_id,
                    "item_id": item_id,
                    "model_id": model_id,
                    "duplicate_count": count,
                    "differing_fields": ";".join(differing),
                    "gmv_values": ";".join(sorted(group["total_gmv_usd"].astype(str).unique())),
                    "price_values": ";".join(
                        sorted(group["shopee_current_price_php"].astype(str).unique())
                    ),
                    "source_row_numbers": ";".join(group["source_row_number"].astype(str)),
                    "resolution_status": "EXCLUDED",
                    "notes": "Conflicting duplicate key; excluded from aggregation.",
                }
            )

    clean = pd.concat(retained, ignore_index=True) if retained else frame.iloc[0:0].copy()
    return clean, pd.DataFrame(conflicts, columns=CONFLICT_COLUMNS)


def _conflict_fingerprint(group: pd.DataFrame) -> str:
    """Hash the conflicting rows so a decision applies only to the exact rows reviewed."""
    ordered = group.sort_values("source_row_number", kind="stable")
    payload = [
        [_text(value) for value in row]
        for row in ordered[["source_row_number", *COMPARE_COLUMNS]].itertuples(index=False)
    ]
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()[:16]


def load_duplicate_decisions(path: str | Path) -> pd.DataFrame:
    """Load recorded human decisions for conflicting duplicate keys; blank rows are ignored."""
    file_path = Path(path)
    if not file_path.exists():
        return pd.DataFrame(columns=DUPLICATE_DECISION_COLUMNS)
    decisions = pd.read_csv(file_path, dtype="string", keep_default_na=False)
    missing = [column for column in DUPLICATE_DECISION_COLUMNS if column not in decisions.columns]
    if missing:
        raise ValueError(f"Duplicate decisions are missing columns: {', '.join(missing)}")
    decisions = decisions[DUPLICATE_DECISION_COLUMNS].copy()
    for column in DUPLICATE_DECISION_COLUMNS:
        decisions[column] = decisions[column].astype("string").fillna("").str.strip()
    decisions["decision"] = decisions["decision"].str.upper()
    recorded = decisions.loc[decisions["decision"].ne("")].reset_index(drop=True)
    invalid = set(recorded["decision"]) - VALID_DUPLICATE_DECISIONS
    if invalid:
        raise ValueError(f"Unsupported duplicate decisions: {', '.join(sorted(invalid))}")
    required = [*DUPLICATE_KEY_COLUMNS, "conflict_fingerprint", "reviewer", "reviewed_at", "reason"]
    if recorded[required].eq("").to_numpy().any():
        raise ValueError("Recorded duplicate decisions require: " + ", ".join(required) + ".")
    keep = recorded["decision"].eq("KEEP_SOURCE_ROW")
    has_row = recorded["keep_source_row_number"].ne("")
    if (keep & ~has_row).any() or (~keep & has_row).any():
        raise ValueError(
            "keep_source_row_number is required for KEEP_SOURCE_ROW and must be blank otherwise."
        )
    # Never pick one of several decisions for the same key; that choice belongs to a human.
    if recorded.duplicated(DUPLICATE_KEY_COLUMNS).any():
        raise ValueError("Each duplicate key may have only one recorded decision.")
    return recorded


def apply_duplicate_decisions(
    frame: pd.DataFrame, conflicts: pd.DataFrame, decisions: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Apply recorded decisions whose fingerprint matches; every other key stays quarantined.

    Returns the rows retained by KEEP_SOURCE_ROW decisions, the conflict report with updated
    resolution statuses, and a redacted review template covering every conflicting key.
    """
    conflicts = conflicts.copy()
    if conflicts.empty:
        return frame.iloc[0:0].copy(), conflicts, pd.DataFrame(columns=DUPLICATE_DECISION_COLUMNS)

    conflict_keys = pd.MultiIndex.from_frame(conflicts[DUPLICATE_KEY_COLUMNS].astype(str))
    in_conflict = pd.MultiIndex.from_frame(frame[DUPLICATE_KEY_COLUMNS].astype(str)).isin(
        conflict_keys
    )
    groups = {
        tuple(str(part) for part in key): group
        for key, group in frame.loc[in_conflict].groupby(DUPLICATE_KEY_COLUMNS, sort=False)
    }
    decision_map = {
        tuple(record[column] for column in DUPLICATE_KEY_COLUMNS): record
        for record in decisions.to_dict("records")
    }
    retained: list[pd.DataFrame] = []
    review: list[dict[str, object]] = []

    for index, conflict in conflicts.iterrows():
        key = tuple(str(conflict[column]) for column in DUPLICATE_KEY_COLUMNS)
        group = groups[key]
        record: dict[str, object] = {
            column: conflict[column] for column in DUPLICATE_KEY_COLUMNS
        } | {
            "duplicate_count": conflict["duplicate_count"],
            "differing_fields": conflict["differing_fields"],
            "source_row_numbers": conflict["source_row_numbers"],
            "conflict_fingerprint": _conflict_fingerprint(group),
        }
        decision = decision_map.get(key)
        if decision is not None and decision["conflict_fingerprint"] != record[
            "conflict_fingerprint"
        ]:
            conflicts.at[index, "notes"] = (
                "Recorded decision does not match the current source rows; re-review required."
            )
            decision = None
        if decision is not None and decision["decision"] == "KEEP_SOURCE_ROW":
            kept = group.loc[
                group["source_row_number"].astype(str).eq(decision["keep_source_row_number"])
            ]
            if len(kept) != 1:
                conflicts.at[index, "notes"] = (
                    "Recorded keep_source_row_number is not one of the conflicting rows; "
                    "re-review required."
                )
                decision = None
            else:
                kept = kept.copy()
                kept["duplicate_status"] = "RESOLVED_BY_DECISION"
                kept["duplicate_count"] = len(group)
                retained.append(kept)
                conflicts.at[index, "resolution_status"] = "RESOLVED_BY_DECISION"
                conflicts.at[index, "notes"] = (
                    f"Source row {decision['keep_source_row_number']} retained by recorded "
                    "human decision."
                )
        elif decision is not None:
            conflicts.at[index, "resolution_status"] = "EXCLUDED_BY_DECISION"
            conflicts.at[index, "notes"] = (
                "Conflicting duplicate key excluded by recorded human decision."
            )
        for column in ("decision", "keep_source_row_number", "reviewer", "reviewed_at", "reason"):
            record[column] = decision[column] if decision is not None else ""
        review.append(record)

    resolved = pd.concat(retained, ignore_index=True) if retained else frame.iloc[0:0].copy()
    return resolved, conflicts, pd.DataFrame(review, columns=DUPLICATE_DECISION_COLUMNS)


def find_model_identity_conflicts(frame: pd.DataFrame) -> tuple[set[str], pd.DataFrame]:
    """Find model IDs associated with more than one shop/item source identity."""
    records: list[dict[str, object]] = []
    conflict_models: set[str] = set()
    for model_id, group in frame.groupby("model_id", sort=True, dropna=False):
        identities = group[["shop_id", "item_id"]].drop_duplicates()
        if len(identities) <= 1:
            continue
        conflict_models.add(str(model_id))
        records.append(
            {
                "period_month": ";".join(
                    sorted(group["period_month"].dropna().astype(str).unique())
                ),
                "shop_id": ";".join(sorted(identities["shop_id"].astype(str))),
                "item_id": ";".join(sorted(identities["item_id"].astype(str))),
                "model_id": str(model_id),
                "duplicate_count": len(identities),
                "differing_fields": "shop_id;item_id",
                "gmv_values": "",
                "price_values": "",
                "source_row_numbers": ";".join(
                    group.sort_values("source_row_number")["source_row_number"].astype(str)
                ),
                "resolution_status": "MODEL_IDENTITY_CONFLICT",
                "notes": (
                    "Model ID maps to multiple shop_id/item_id identities; "
                    "excluded from publication."
                ),
            }
        )
    return conflict_models, pd.DataFrame(records, columns=CONFLICT_COLUMNS)
