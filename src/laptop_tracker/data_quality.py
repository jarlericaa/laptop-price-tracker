"""Validation, duplicate quarantine, and model identity checks."""

from __future__ import annotations

import hashlib
import itertools
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
# The review template and config/duplicate_decisions.csv share one redacted, content-based layout:
# source identifiers, the conflicting date(s), field names, and one content fingerprint per
# reviewed record; never GMV, prices, names, or sheet row numbers. A decision is identified by
# what the conflicting records say, so re-sorting the raw sheet cannot invalidate it. Current
# row positions are resolved at run time and written to duplicate_resolution_log.csv.
FINGERPRINT_VERSION = "content-v1"
DUPLICATE_DECISION_COLUMNS = [
    *DUPLICATE_KEY_COLUMNS,
    "conflict_dates",
    "differing_fields",
    "record_fingerprints",
    "fingerprint_version",
    "decision",
    "selected_record_fingerprint",
    "reviewer",
    "reviewed_at",
    "reason",
]
DUPLICATE_RESOLUTION_LOG_COLUMNS = [
    *DUPLICATE_KEY_COLUMNS,
    "record_fingerprint",
    "current_source_row_number",
    "record_role",
    "outcome",
    "detail",
]
# The pre-2026-10-27 layout identified a decision by sheet row numbers; it is only read by the
# migration (`migrate_legacy_decisions`).
LEGACY_DECISION_COLUMNS = [
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
VALID_DUPLICATE_DECISIONS = {"EXCLUDE_KEY", "KEEP_RECORD"}
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


def record_fingerprint(row: pd.Series) -> str:
    """Content fingerprint (``FINGERPRINT_VERSION``) of one source record.

    Hashes the exact text of every compared raw column (identifiers, date, names, price, orders,
    GMV, amount) as read from the sheet, so numeric precision, leading zeros and long
    identifiers are preserved and the sheet row number plays no part.
    """
    payload = [FINGERPRINT_VERSION, [[column, _text(row[column])] for column in COMPARE_COLUMNS]]
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()[:24]


def _record_table(group: pd.DataFrame) -> list[tuple[str, int]]:
    """(fingerprint, current sheet row) of each record, in current row order."""
    ordered = group.sort_values("source_row_number", kind="stable")
    return [
        (record_fingerprint(row), int(row["source_row_number"]))
        for _, row in ordered.iterrows()
    ]


def _conflict_dates(group: pd.DataFrame) -> str:
    return ";".join(sorted({_text(value) for value in group["period_date"]}))


def legacy_conflict_fingerprint(group: pd.DataFrame) -> str:
    """The pre-2026-10-27 row-number fingerprint; used only to verify a migration."""
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
    if {"source_row_numbers", "keep_source_row_number"} & set(decisions.columns):
        recorded_legacy = decisions.get("decision", pd.Series(dtype="string"))
        if recorded_legacy.astype("string").fillna("").str.strip().ne("").any():
            raise ValueError(
                "config/duplicate_decisions.csv uses the retired row-number layout. Convert it "
                "with migrate_duplicate_decisions.py (content-based decisions are not tied to "
                "sheet rows)."
            )
        return pd.DataFrame(columns=DUPLICATE_DECISION_COLUMNS)
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
    required = [
        *DUPLICATE_KEY_COLUMNS,
        "conflict_dates",
        "record_fingerprints",
        "fingerprint_version",
        "reviewer",
        "reviewed_at",
        "reason",
    ]
    if recorded[required].eq("").to_numpy().any():
        raise ValueError("Recorded duplicate decisions require: " + ", ".join(required) + ".")
    keep = recorded["decision"].eq("KEEP_RECORD")
    has_selected = recorded["selected_record_fingerprint"].ne("")
    if (keep & ~has_selected).any() or (~keep & has_selected).any():
        raise ValueError(
            "selected_record_fingerprint is required for KEEP_RECORD and must be blank otherwise."
        )
    for _, record in recorded.loc[keep].iterrows():
        if record["selected_record_fingerprint"] not in record["record_fingerprints"].split(";"):
            raise ValueError(
                "selected_record_fingerprint must be one of the reviewed record_fingerprints."
            )
    # Never pick one of several decisions for the same key; that choice belongs to a human.
    if recorded.duplicated(DUPLICATE_KEY_COLUMNS).any():
        raise ValueError("Each duplicate key may have only one recorded decision.")
    return recorded


def apply_duplicate_decisions(
    frame: pd.DataFrame, conflicts: pd.DataFrame, decisions: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Apply recorded decisions whose reviewed content still matches; the rest stay quarantined.

    A decision applies only when the key's current records are exactly the records that were
    reviewed (same fingerprints, same dates), however the sheet rows have moved. If a reviewed
    record changed, or a record was added or removed, the decision is invalidated even when the
    selected record still exists. Returns the rows retained by KEEP_RECORD decisions, the
    conflict report with updated resolution statuses, a redacted review template covering every
    conflicting key, and a log of the sheet row each record occupies in this run.
    """
    conflicts = conflicts.copy()
    if conflicts.empty:
        return (
            frame.iloc[0:0].copy(),
            conflicts,
            pd.DataFrame(columns=DUPLICATE_DECISION_COLUMNS),
            pd.DataFrame(columns=DUPLICATE_RESOLUTION_LOG_COLUMNS),
        )

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
    log: list[dict[str, object]] = []

    for index, conflict in conflicts.iterrows():
        key = tuple(str(conflict[column]) for column in DUPLICATE_KEY_COLUMNS)
        group = groups[key]
        table = _record_table(group)
        current = sorted(fingerprint for fingerprint, _ in table)
        dates = _conflict_dates(group)
        record: dict[str, object] = {
            column: conflict[column] for column in DUPLICATE_KEY_COLUMNS
        } | {
            "conflict_dates": dates,
            "differing_fields": conflict["differing_fields"],
            "record_fingerprints": ";".join(current),
            "fingerprint_version": FINGERPRINT_VERSION,
        }
        decision = decision_map.get(key)
        outcome = "UNRESOLVED"
        detail = "No recorded decision."
        if decision is not None:
            problem = ""
            if decision["fingerprint_version"] != FINGERPRINT_VERSION:
                problem = (
                    f"Recorded decision uses fingerprint format {decision['fingerprint_version']}, "
                    f"not {FINGERPRINT_VERSION}; re-review required."
                )
            elif sorted(decision["record_fingerprints"].split(";")) != current:
                problem = (
                    "The reviewed conflicting records changed (a record was edited, added or "
                    "removed); re-review required."
                )
            elif decision["conflict_dates"] != dates:
                problem = "The reviewed conflict date changed; re-review required."
            if problem:
                conflicts.at[index, "notes"] = problem
                outcome, detail, decision = "DECISION_INVALIDATED", problem, None

        selected_row: int | None = None
        if decision is not None and decision["decision"] == "KEEP_RECORD":
            selected = decision["selected_record_fingerprint"]
            holders = [row for fingerprint, row in table if fingerprint == selected]
            selected_row = holders[0]  # identical copies are interchangeable; keep the first
            kept = group.loc[group["source_row_number"].astype(int).eq(selected_row)].copy()
            kept["duplicate_status"] = "RESOLVED_BY_DECISION"
            kept["duplicate_count"] = len(group)
            retained.append(kept)
            conflicts.at[index, "resolution_status"] = "RESOLVED_BY_DECISION"
            conflicts.at[index, "notes"] = (
                f"Record {selected} (source row {selected_row} in this run) retained by recorded "
                "human decision."
            )
            outcome, detail = "RESOLVED_BY_DECISION", "Reviewed records unchanged."
        elif decision is not None:
            conflicts.at[index, "resolution_status"] = "EXCLUDED_BY_DECISION"
            conflicts.at[index, "notes"] = (
                "Conflicting duplicate key excluded by recorded human decision."
            )
            outcome, detail = "EXCLUDED_BY_DECISION", "Reviewed records unchanged."

        for column in (
            "decision",
            "selected_record_fingerprint",
            "reviewer",
            "reviewed_at",
            "reason",
        ):
            record[column] = decision[column] if decision is not None else ""
        review.append(record)
        for fingerprint, row in table:
            if outcome == "RESOLVED_BY_DECISION":
                role = "SELECTED" if row == selected_row else "NOT_SELECTED"
            elif outcome == "EXCLUDED_BY_DECISION":
                role = "EXCLUDED"
            else:
                role = "UNDECIDED"
            log.append(
                {column: conflict[column] for column in DUPLICATE_KEY_COLUMNS}
                | {
                    "record_fingerprint": fingerprint,
                    "current_source_row_number": row,
                    "record_role": role,
                    "outcome": outcome,
                    "detail": detail,
                }
            )

    resolved = pd.concat(retained, ignore_index=True) if retained else frame.iloc[0:0].copy()
    return (
        resolved,
        conflicts,
        pd.DataFrame(review, columns=DUPLICATE_DECISION_COLUMNS),
        pd.DataFrame(log, columns=DUPLICATE_RESOLUTION_LOG_COLUMNS),
    )


def load_legacy_decisions(path: str | Path) -> pd.DataFrame:
    """Read a retired row-number decision file for migration (recorded decisions only)."""
    decisions = pd.read_csv(path, dtype="string", keep_default_na=False)
    missing = [column for column in LEGACY_DECISION_COLUMNS if column not in decisions.columns]
    if missing:
        raise ValueError(f"Legacy duplicate decisions are missing columns: {', '.join(missing)}")
    decisions = decisions[LEGACY_DECISION_COLUMNS].copy()
    for column in LEGACY_DECISION_COLUMNS:
        decisions[column] = decisions[column].astype("string").fillna("").str.strip()
    return decisions.loc[decisions["decision"].ne("")].reset_index(drop=True)


MIGRATION_REPORT_COLUMNS = [
    *DUPLICATE_KEY_COLUMNS,
    "legacy_decision",
    "outcome",
    "detail",
    "selected_record_fingerprint",
]
MAX_MIGRATION_GROUP = 6


def migrate_legacy_decisions(
    legacy: pd.DataFrame, valid_rows: pd.DataFrame, *, note: str = ""
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Convert recorded row-number decisions to content-based ones where that is established.

    A decision is migrated only when the previously reviewed records are proven to be the
    current records: placing the key's current records on the old row numbers (in some order)
    must reproduce the stored legacy fingerprint, and every such order must give the same
    selected record content. The reviewer, review date and reason are kept; ``note`` is appended
    to the reason. Anything not established is reported and left for review, never guessed.
    """
    _, conflicts = collapse_duplicates(valid_rows)
    groups: dict[tuple[str, ...], pd.DataFrame] = {}
    for _, conflict in conflicts.iterrows():
        mask = pd.Series(True, index=valid_rows.index)
        for column in DUPLICATE_KEY_COLUMNS:
            mask &= valid_rows[column].astype(str).eq(str(conflict[column]))
        groups[tuple(str(conflict[column]) for column in DUPLICATE_KEY_COLUMNS)] = valid_rows.loc[
            mask
        ]
    migrated: list[dict[str, object]] = []
    report: list[dict[str, object]] = []

    def outcome(row: pd.Series, result: str, detail: str, selected: str = "") -> None:
        report.append(
            {column: row[column] for column in DUPLICATE_KEY_COLUMNS}
            | {
                "legacy_decision": row["decision"],
                "outcome": result,
                "detail": detail,
                "selected_record_fingerprint": selected,
            }
        )

    for _, row in legacy.iterrows():
        key = tuple(str(row[column]) for column in DUPLICATE_KEY_COLUMNS)
        group = groups.get(key)
        if group is None:
            outcome(row, "NOT_MIGRATED", "The key is not a current conflict.")
            continue
        group = group.sort_values("source_row_number", kind="stable")
        old_numbers = [int(part) for part in row["source_row_numbers"].split(";") if part.strip()]
        if len(group) != len(old_numbers) or not 2 <= len(group) <= MAX_MIGRATION_GROUP:
            outcome(
                row,
                "NOT_MIGRATED",
                f"{len(group)} current records versus {len(old_numbers)} reviewed rows.",
            )
            continue
        slots = sorted(old_numbers)
        keep_row = (
            int(row["keep_source_row_number"]) if row["decision"] == "KEEP_SOURCE_ROW" else None
        )
        if keep_row is not None and keep_row not in slots:
            outcome(row, "NOT_MIGRATED", "The selected row is not one of the reviewed rows.")
            continue
        prints = [record_fingerprint(record) for _, record in group.iterrows()]
        winners: set[str] = set()
        reproduced = 0
        for order in itertools.permutations(range(len(group))):
            trial = group.copy()
            trial["source_row_number"] = [slots[position] for position in order]
            if legacy_conflict_fingerprint(trial) != row["conflict_fingerprint"]:
                continue
            reproduced += 1
            if keep_row is not None:
                winners.add(prints[order[slots.index(keep_row)]])
        if not reproduced:
            outcome(
                row,
                "NOT_MIGRATED",
                "The reviewed records cannot be established: the stored fingerprint is not "
                "reproduced by the current records (values changed since the review).",
            )
            continue
        if keep_row is not None and len(winners) != 1:
            outcome(row, "NOT_MIGRATED", "The previously selected record is ambiguous.")
            continue
        selected = next(iter(winners)) if keep_row is not None else ""
        reason = (row["reason"] + (" " + note if note else "")).strip()
        migrated.append(
            {column: row[column] for column in DUPLICATE_KEY_COLUMNS}
            | {
                "conflict_dates": _conflict_dates(group),
                "differing_fields": ";".join(
                    column for column in COMPARE_COLUMNS if group[column].nunique(dropna=False) > 1
                ),
                "record_fingerprints": ";".join(sorted(prints)),
                "fingerprint_version": FINGERPRINT_VERSION,
                "decision": "KEEP_RECORD" if keep_row is not None else "EXCLUDE_KEY",
                "selected_record_fingerprint": selected,
                "reviewer": row["reviewer"],
                "reviewed_at": row["reviewed_at"],
                "reason": reason,
            }
        )
        outcome(row, "MIGRATED", "Reviewed records and selected winner established.", selected)
    return (
        pd.DataFrame(migrated, columns=DUPLICATE_DECISION_COLUMNS),
        pd.DataFrame(report, columns=MIGRATION_REPORT_COLUMNS),
    )


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
