"""Local, credential-free validators for the SOURCE_MODEL_MAP and MATCH_REVIEW tables.

Both tables support TARGET_MASTER (see docs/TARGET_MASTER_SCHEMA.md). Their header templates in
config/ are exact contracts. Values are never coerced, trimmed, or promoted: these validators only
report errors, and nothing here creates TARGET_MASTER or SOURCE_MODEL_MAP rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

import pandas as pd

from .target_master import (
    TARGET_ERROR_COLUMNS,
    TARGET_ID_PATTERN,
    TARGET_MASTER_COLUMNS,
    TARGET_PRODUCT_REQUIRED_COLUMNS,
    TargetColumn,
    TargetMasterResult,
    TargetMasterSchemaError,
    _add_duplicate_errors,
    check_cell,
)

# Shopee model IDs are digit-only strings in `raw` (see schema.py); leading zeroes are kept.
SOURCE_MODEL_ID_PATTERN = r"[0-9]+"

SOURCE_MODEL_MAP_COLUMNS: tuple[TargetColumn, ...] = (
    TargetColumn(
        "source_model_id",
        True,
        "Shopee model ID, kept as text.",
        SOURCE_MODEL_ID_PATTERN,
        "digits only",
    ),
    TargetColumn(
        "target_id",
        True,
        "Existing TARGET_MASTER target_id.",
        TARGET_ID_PATTERN,
        "TGT- followed by exactly six digits, for example TGT-000001",
    ),
    TargetColumn("reviewer", True, "Person who reviewed this link."),
    TargetColumn("reviewed_at", True, "Review date.", format_hint="YYYY-MM-DD", is_date=True),
    TargetColumn("notes", False, "Free-text notes.", free_text=True),
)

# COMPLETE (added for the 2026-10-05 five-core-field rule) marks a candidate whose core fields are
# populated and unambiguous; it stays in MATCH_REVIEW as the trail after manual promotion.
MATCH_REVIEW_STATUSES = ("NEW", "INCOMPLETE", "COMPLETE", "REVIEW_REQUIRED", "REJECTED")

_TARGET_COLUMNS_BY_NAME = {column.name: column for column in TARGET_MASTER_COLUMNS}
# Identity specs and source_url reuse the TARGET_MASTER formats but may be blank during research.
_REVIEW_PRODUCT_COLUMNS = tuple(
    replace(column, required=False)
    for column in TARGET_MASTER_COLUMNS
    if column.name
    not in {"target_id", "status", "source_url", "reviewer", "reviewed_at", "notes"}
)
# TARGET_PRODUCT_REQUIRED_COLUMNS is exactly the five-field core identity.
_REVIEW_EVIDENCE_COLUMNS = tuple(
    replace(_TARGET_COLUMNS_BY_NAME[name], required=False)
    for name in TARGET_PRODUCT_REQUIRED_COLUMNS
)

MATCH_REVIEW_COLUMNS: tuple[TargetColumn, ...] = (
    # No review_id scheme has been approved, so any text is accepted; nonblank IDs must be unique.
    TargetColumn("review_id", False, "Review row identifier; scheme not yet approved."),
    TargetColumn("status", True, "Review state.", allowed=MATCH_REVIEW_STATUSES),
    TargetColumn(
        "source_model_id",
        False,
        "Shopee model ID under review, kept as text.",
        SOURCE_MODEL_ID_PATTERN,
        "digits only",
    ),
    TargetColumn("candidate_name", False, "Source listing or model description.", free_text=True),
    *_REVIEW_PRODUCT_COLUMNS,
    replace(_TARGET_COLUMNS_BY_NAME["source_url"], required=False),
    TargetColumn("review_reason", False, "Why the candidate is under review.", free_text=True),
    TargetColumn("prepared_by", False, "Person who prepared the candidate."),
    TargetColumn("reviewer", False, "Person who recorded the review decision."),
    TargetColumn("reviewed_at", False, "Review date.", format_hint="YYYY-MM-DD", is_date=True),
    TargetColumn("notes", False, "Free-text notes.", free_text=True),
)

SOURCE_MODEL_MAP_HEADERS = [column.name for column in SOURCE_MODEL_MAP_COLUMNS]
MATCH_REVIEW_HEADERS = [column.name for column in MATCH_REVIEW_COLUMNS]
MATCH_REVIEW_EVIDENCE_COLUMNS = [column.name for column in _REVIEW_EVIDENCE_COLUMNS]


@dataclass
class TableResult:
    rows: pd.DataFrame
    valid_rows: pd.DataFrame
    errors: pd.DataFrame
    # Non-blocking counts for reviewers; they never change validity.
    info: dict[str, int] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """Header-only tables are valid: no reviewed links or open candidates is a real state."""
        return self.errors.empty


def validate_source_model_map(
    values: Sequence[Sequence[object]], target_master: TargetMasterResult
) -> TableResult:
    """Validate mapping rows against a TARGET_MASTER result from ``validate_target_master``.

    A source_model_id may appear on only one row; several may share a target_id. A target_id must
    exist in TARGET_MASTER, and a link to a TARGET_MASTER row that failed validation is rejected.
    Links to INACTIVE targets are counted but not rejected, because no policy has been approved.
    """
    headers = _validate_exact_headers("SOURCE_MODEL_MAP", values, SOURCE_MODEL_MAP_HEADERS)
    errors: list[dict[str, object]] = []
    add_error = _error_collector(errors)
    records = _records(values, headers, add_error)
    for record in records:
        row_number = int(record["source_row_number"])  # type: ignore[arg-type]
        for column in SOURCE_MODEL_MAP_COLUMNS:
            check_cell(row_number, column, str(record[column.name]), add_error)

    _add_duplicate_errors(
        records,
        ["source_model_id"],
        "source_model_id",
        "DUPLICATE_SOURCE_MODEL_ID",
        "source_model_id maps to more than one row; each may map to at most one target",
        add_error,
    )

    all_targets = set(target_master.rows["target_id"].astype(str))
    valid_targets = set(target_master.valid_rows["target_id"].astype(str))
    inactive_targets = set(
        target_master.valid_rows.loc[
            target_master.valid_rows["status"].eq("INACTIVE"), "target_id"
        ].astype(str)
    )
    inactive_links = 0
    for record in records:
        target_id = str(record["target_id"])
        row_number = int(record["source_row_number"])  # type: ignore[arg-type]
        if target_id.strip() == "" or target_id != target_id.strip():
            continue  # Already reported as missing or whitespace.
        if target_id not in all_targets:
            add_error(
                row_number,
                "target_id",
                target_id,
                "UNKNOWN_TARGET_ID",
                "target_id does not exist in TARGET_MASTER.",
            )
        elif target_id not in valid_targets:
            add_error(
                row_number,
                "target_id",
                target_id,
                "TARGET_NOT_VALID",
                "target_id refers to a TARGET_MASTER row that failed validation.",
            )
        elif target_id in inactive_targets:
            inactive_links += 1

    info = {"links_to_inactive_targets": inactive_links}
    return _result(records, SOURCE_MODEL_MAP_HEADERS, errors, info)


def validate_match_review(values: Sequence[Sequence[object]]) -> TableResult:
    """Validate candidate rows; blank specs are allowed, nonblank ones use TARGET_MASTER formats.

    A ``COMPLETE`` row must have all five core fields; secondary fields and source_url may stay
    blank. Complete evidence is only counted: no row is ever promoted, matched, or mapped here.
    """
    headers = _validate_exact_headers("MATCH_REVIEW", values, MATCH_REVIEW_HEADERS)
    errors: list[dict[str, object]] = []
    add_error = _error_collector(errors)
    records = _records(values, headers, add_error)
    complete_evidence = 0
    for record in records:
        row_number = int(record["source_row_number"])  # type: ignore[arg-type]
        errors_before = len(errors)
        for column in MATCH_REVIEW_COLUMNS:
            check_cell(row_number, column, str(record[column.name]), add_error)
        evidence_errors = any(
            error["column_name"] in MATCH_REVIEW_EVIDENCE_COLUMNS
            for error in errors[errors_before:]
        )

        if record["status"] == "COMPLETE":
            for name in MATCH_REVIEW_EVIDENCE_COLUMNS:
                if str(record[name]).strip() == "":
                    add_error(
                        row_number,
                        name,
                        "",
                        "MISSING_VALUE",
                        f"{name} is a core field and is required when status is COMPLETE.",
                    )

        reviewer = str(record["reviewer"]).strip()
        reviewed_at = str(record["reviewed_at"]).strip()
        if record["status"] == "REJECTED":
            # A rejection is a recorded decision, so it needs the decision-maker and date.
            for name, value in (("reviewer", reviewer), ("reviewed_at", reviewed_at)):
                if value == "":
                    add_error(
                        row_number,
                        name,
                        "",
                        "MISSING_VALUE",
                        f"{name} is required when status is REJECTED.",
                    )
        elif bool(reviewer) != bool(reviewed_at):
            missing = "reviewed_at" if reviewer else "reviewer"
            add_error(
                row_number,
                missing,
                "",
                "MISSING_VALUE",
                "reviewer and reviewed_at must be recorded together.",
            )

        if not evidence_errors and all(
            str(record[name]).strip() != "" for name in MATCH_REVIEW_EVIDENCE_COLUMNS
        ):
            complete_evidence += 1

    _add_duplicate_errors(
        records,
        ["review_id"],
        "review_id",
        "DUPLICATE_REVIEW_ID",
        "review_id is not unique (case-insensitive)",
        add_error,
    )
    info = {"rows_with_complete_core_identity": complete_evidence}
    for status in MATCH_REVIEW_STATUSES:
        info[f"status_{status}"] = sum(1 for record in records if record["status"] == status)
    return _result(records, MATCH_REVIEW_HEADERS, errors, info)


def mapped_source_ids_under_review(
    source_model_map: TableResult, match_review: TableResult
) -> int:
    """Count validated mapped IDs that still have a non-REJECTED review row (informational)."""
    mapped = set(source_model_map.valid_rows["source_model_id"].astype(str))
    open_rows = match_review.rows.loc[match_review.rows["status"].ne("REJECTED")]
    return len(mapped & set(open_rows["source_model_id"].astype(str)))


TOP_100_SEED_COLUMNS = (
    "rank",
    "run_id",
    "shop_id",
    "item_id",
    "model_id",
    "item_name",
    "shopee_model_name",
)


class SeedError(ValueError):
    """Raised when Top 100 input cannot seed candidates without guessing."""


def build_match_review_candidates(top_100_values: Sequence[Sequence[object]]) -> list[list[str]]:
    """Return MATCH_REVIEW values (header first) with one NEW row per distinct Top 100 model_id.

    Only source facts are copied: the model ID, the listing and model names, and traceability
    details. Specifications, source_url, review_id, reviewer, and dates are left blank because
    none can be derived from Shopee titles. No TARGET_MASTER or SOURCE_MODEL_MAP row is created.
    """
    if not top_100_values:
        raise SeedError("Top 100 input has no header row.")
    headers = ["" if header is None else str(header).strip() for header in top_100_values[0]]
    missing = [column for column in TOP_100_SEED_COLUMNS if column not in headers]
    if missing:
        raise SeedError(f"Top 100 input is missing columns: {', '.join(missing)}")
    index = {name: headers.index(name) for name in TOP_100_SEED_COLUMNS}
    seen: set[str] = set()
    rows: list[list[str]] = []
    for row_number, row in enumerate(top_100_values[1:], start=2):
        cells = ["" if value is None else str(value) for value in row]
        if all(cell.strip() == "" for cell in cells):
            continue
        cells += [""] * (len(headers) - len(cells))
        source = {name: cells[position] for name, position in index.items()}
        model_id = source["model_id"]
        if not model_id.isdigit() or not model_id.isascii():
            raise SeedError(
                f"Top 100 row {row_number} has a blank or non-digit model_id; "
                "no candidates were produced."
            )
        if model_id in seen:
            continue
        seen.add(model_id)
        candidate = dict.fromkeys(MATCH_REVIEW_HEADERS, "")
        candidate |= {
            "status": "NEW",
            "source_model_id": model_id,
            "candidate_name": source["item_name"],
            "review_reason": (
                "Phase 4 Top 100 candidate; identity specifications, source evidence, and "
                "approval have not been researched."
            ),
            "notes": (
                f"Top 100 run {source['run_id']}, rank {source['rank']}; "
                f"Shopee shop_id {source['shop_id']}, item_id {source['item_id']}; "
                f"Shopee model name: {source['shopee_model_name']}"
            ),
        }
        rows.append([candidate[column] for column in MATCH_REVIEW_HEADERS])
    return [list(MATCH_REVIEW_HEADERS), *rows]


def _validate_exact_headers(
    table: str, values: Sequence[Sequence[object]], expected: list[str]
) -> list[str]:
    if not values:
        raise TargetMasterSchemaError(f"{table} has no header row.")
    headers = ["" if header is None else str(header).strip() for header in values[0]]
    if headers == expected:
        return headers
    if not headers or all(header == "" for header in headers):
        raise TargetMasterSchemaError(f"{table} has no header row.")
    missing = [column for column in expected if column not in headers]
    unknown = [header for header in headers if header not in expected]
    details = []
    if missing:
        details.append(f"missing: {', '.join(missing)}")
    if unknown:
        details.append(f"unknown or blank: {', '.join(header or '(blank)' for header in unknown)}")
    if not details:
        details.append("columns are repeated or out of template order")
    raise TargetMasterSchemaError(
        f"{table} headers must exactly match its config template ({'; '.join(details)})."
    )


def _error_collector(errors: list[dict[str, object]]):
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

    return add_error


def _records(
    values: Sequence[Sequence[object]], headers: list[str], add_error
) -> list[dict[str, object]]:
    """Convert worksheet-shaped rows to exact strings; entirely blank rows are ignored."""
    width = len(headers)
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
        records.append({"source_row_number": row_number, **dict(zip(headers, cells, strict=True))})
    return records


def _result(
    records: list[dict[str, object]],
    headers: list[str],
    errors: list[dict[str, object]],
    info: dict[str, int],
) -> TableResult:
    rows = pd.DataFrame(records, columns=["source_row_number", *headers])
    for column in headers:
        rows[column] = rows[column].astype("string")
    error_frame = (
        pd.DataFrame(errors, columns=TARGET_ERROR_COLUMNS)
        .sort_values(["source_row_number", "column_name"], kind="stable")
        .reset_index(drop=True)
    )
    invalid_rows = set(error_frame["source_row_number"].astype(int))
    valid_rows = rows.loc[~rows["source_row_number"].isin(invalid_rows)].reset_index(drop=True)
    return TableResult(rows=rows, valid_rows=valid_rows, errors=error_frame, info=info)
