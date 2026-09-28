"""Explicit, guarded writing of Phase 1 output worksheets only."""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from .eligibility import DECISION_COLUMNS

ALLOWED_OUTPUT_TABS = {
    "P1_TOP_100_TARGETS",
    "P1_ELIGIBLE_RANKING",
    "P1_EXCLUDED_LISTINGS",
    "P1_ELIGIBILITY_REVIEW",
    "P1_ELIGIBILITY_DECISIONS",
    "P1_CONFLICTING_DUPLICATES",
    "P1_VALIDATION_ERRORS",
    "P1_PERIOD_SUMMARY",
    "P1_RUN_LOG",
}


def assert_safe_output_tab(worksheet_name: str) -> None:
    """Reject `raw` (including casing/whitespace variants) and non-Phase-1 destinations."""
    normalized = worksheet_name.strip()
    if normalized.lower() == "raw":
        raise ValueError("Safety guard: the raw worksheet is read-only and cannot be written.")
    if normalized not in ALLOWED_OUTPUT_TABS:
        raise ValueError(f"Worksheet {worksheet_name!r} is not an approved Phase 1 output tab.")


def _values(frame: pd.DataFrame) -> list[list[object]]:
    prepared = frame.copy().astype(object).where(pd.notna(frame), "")
    return [list(prepared.columns)] + prepared.values.tolist()


def _worksheet_or_create(spreadsheet, worksheet_name: str):
    assert_safe_output_tab(worksheet_name)
    try:
        return spreadsheet.worksheet(worksheet_name)
    except Exception as exc:  # gspread's WorksheetNotFound can be version-specific in mocks.
        if exc.__class__.__name__ != "WorksheetNotFound":
            raise
        return spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=30)


def replace_output_tab(spreadsheet, worksheet_name: str, frame: pd.DataFrame) -> None:
    """Replace a permitted output tab. This function cannot target `raw`."""
    worksheet = _worksheet_or_create(spreadsheet, worksheet_name)
    worksheet.clear()
    worksheet.update(_values(frame), value_input_option="RAW")


def preserve_manual_decisions(spreadsheet, local_decisions: pd.DataFrame) -> pd.DataFrame:
    """Merge remote decisions with local config; an existing sheet decision wins unchanged."""
    worksheet = _worksheet_or_create(spreadsheet, "P1_ELIGIBILITY_DECISIONS")
    # Fail closed if the existing decisions cannot be read. Treating a transient API or
    # schema error as an empty sheet could otherwise erase human decisions below.
    existing = worksheet.get_all_values()
    if existing:
        headers = [str(value).strip() for value in existing[0]]
        if headers != DECISION_COLUMNS:
            raise ValueError(
                "P1_ELIGIBILITY_DECISIONS headers must exactly match: "
                + ", ".join(DECISION_COLUMNS)
            )
        width = len(headers)
        rows = [
            ["" if value is None else str(value) for value in row[:width]]
            + [""] * (width - len(row))
            for row in existing[1:]
        ]
        remote = pd.DataFrame(rows, columns=DECISION_COLUMNS, dtype="string")
    else:
        remote = pd.DataFrame(columns=DECISION_COLUMNS)
    remote = remote.reindex(columns=DECISION_COLUMNS).fillna("")
    local = local_decisions.reindex(columns=DECISION_COLUMNS).fillna("")
    merged = pd.concat([local, remote], ignore_index=True)
    merged = merged.loc[merged["model_id"].astype(str).str.strip().ne("")]
    merged = merged.drop_duplicates("model_id", keep="last").sort_values("model_id", kind="stable")
    worksheet.clear()
    worksheet.update(_values(merged), value_input_option="RAW")
    return merged.reset_index(drop=True)


def append_run_log(spreadsheet, summary: Mapping[str, object]) -> None:
    worksheet = _worksheet_or_create(spreadsheet, "P1_RUN_LOG")
    existing = worksheet.get_all_values()
    headers = list(summary.keys())
    if not existing:
        worksheet.update([headers], value_input_option="RAW")
    elif existing[0] != headers:
        raise ValueError("P1_RUN_LOG headers do not match the current run summary schema.")
    worksheet.append_row([summary.get(header, "") for header in headers], value_input_option="RAW")


def write_phase1_outputs(
    spreadsheet,
    outputs: Mapping[str, pd.DataFrame],
    summary: Mapping[str, object],
    local_decisions: pd.DataFrame,
) -> None:
    """Write only known P1 output tabs after the caller explicitly opted in."""
    for worksheet_name in outputs:
        assert_safe_output_tab(worksheet_name)
        if worksheet_name in {"P1_ELIGIBILITY_DECISIONS", "P1_RUN_LOG"}:
            raise ValueError(f"{worksheet_name} has dedicated safe handling.")
    # Read and preserve decisions before replacing any generated output. A remote read or
    # schema failure therefore stops the write operation before generated tabs are touched.
    preserve_manual_decisions(spreadsheet, local_decisions)
    for worksheet_name, frame in outputs.items():
        replace_output_tab(spreadsheet, worksheet_name, frame)
    append_run_log(spreadsheet, summary)
