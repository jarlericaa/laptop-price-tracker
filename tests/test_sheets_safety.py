from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest
from conftest import settings

from laptop_tracker.eligibility import DECISION_COLUMNS
from laptop_tracker.sheets_source import (
    READ_ONLY_SCOPES,
    READ_WRITE_SCOPES,
    load_credentials,
)
from laptop_tracker.sheets_writer import preserve_manual_decisions


class FailingDecisionWorksheet:
    def __init__(self) -> None:
        self.clear_called = False

    def get_all_values(self):
        raise RuntimeError("simulated read failure")

    def clear(self) -> None:
        self.clear_called = True


class ExistingDecisionSpreadsheet:
    def __init__(self, worksheet: FailingDecisionWorksheet) -> None:
        self.worksheet_object = worksheet

    def worksheet(self, name: str) -> FailingDecisionWorksheet:
        assert name == "P1_ELIGIBILITY_DECISIONS"
        return self.worksheet_object


class InMemoryDecisionWorksheet:
    def __init__(self, values: list[list[str]]) -> None:
        self.values = values

    def get_all_values(self) -> list[list[str]]:
        return self.values

    def clear(self) -> None:
        self.values = []

    def update(self, values, **_kwargs) -> None:
        self.values = values


def test_manual_decisions_are_not_cleared_when_the_remote_read_fails() -> None:
    worksheet = FailingDecisionWorksheet()
    spreadsheet = ExistingDecisionSpreadsheet(worksheet)
    with pytest.raises(RuntimeError, match="simulated read failure"):
        preserve_manual_decisions(spreadsheet, pd.DataFrame(columns=DECISION_COLUMNS))
    assert not worksheet.clear_called


def test_remote_manual_decision_ids_remain_exact_strings() -> None:
    values = [DECISION_COLUMNS, ["0007", "ELIGIBLE", "verified", "A", "2026-09-22", ""]]
    worksheet = InMemoryDecisionWorksheet(values)
    spreadsheet = ExistingDecisionSpreadsheet(worksheet)  # type: ignore[arg-type]
    merged = preserve_manual_decisions(spreadsheet, pd.DataFrame(columns=DECISION_COLUMNS))
    assert merged.loc[0, "model_id"] == "0007"


def test_source_credentials_are_read_only_and_writer_credentials_are_explicit() -> None:
    configured = settings()
    configured = configured.__class__(
        spreadsheet_id="sheet-id",
        raw_tab_name=configured.raw_tab_name,
        service_account_file=None,
        service_account_json="{}",
        max_validation_errors=configured.max_validation_errors,
        skip_freshness_check=configured.skip_freshness_check,
    )
    with patch(
        "laptop_tracker.sheets_source.Credentials.from_service_account_info",
        return_value=object(),
    ) as loader:
        load_credentials(configured)
        assert loader.call_args.kwargs["scopes"] == READ_ONLY_SCOPES
        load_credentials(configured, read_only=False)
        assert loader.call_args.kwargs["scopes"] == READ_WRITE_SCOPES
