"""Read-only Google Sheets source access for `raw` and the Phase 5 target tables."""

from __future__ import annotations

import json
from collections.abc import Sequence

import gspread
from google.auth import exceptions as google_exceptions
from google.oauth2.service_account import Credentials

from .config import Settings

READ_ONLY_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
READ_WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Master copies of the Phase 5 tables; local CSV exports are validation snapshots.
TARGET_TABLE_TABS = ("TARGET_MASTER", "SOURCE_MODEL_MAP", "MATCH_REVIEW")


class SheetsSourceError(RuntimeError):
    """A safe, actionable Sheets source error that never includes credentials."""


def load_credentials(settings: Settings, *, read_only: bool = True) -> Credentials:
    """Load credentials with the least privilege needed by the caller."""
    scopes = READ_ONLY_SCOPES if read_only else READ_WRITE_SCOPES
    try:
        if settings.service_account_json:
            info = json.loads(settings.service_account_json)
            return Credentials.from_service_account_info(info, scopes=scopes)
        settings.require_sheet_access()
        if settings.service_account_file is None:  # GOOGLE_USE_ADC: Workload Identity Federation
            import google.auth

            credentials, _ = google.auth.default(scopes=scopes)
            return credentials
        return Credentials.from_service_account_file(settings.service_account_file, scopes=scopes)
    except (json.JSONDecodeError, OSError, ValueError, google_exceptions.GoogleAuthError) as exc:
        raise SheetsSourceError(
            "Unable to load Google service-account credentials. Check the configured source."
        ) from exc


def connect(settings: Settings, *, read_only: bool = True) -> gspread.Client:
    settings.require_sheet_access()
    return gspread.authorize(load_credentials(settings, read_only=read_only))


def open_spreadsheet(client: gspread.Client, spreadsheet_id: str):
    if not spreadsheet_id:
        raise SheetsSourceError("SPREADSHEET_ID is required.")
    try:
        return client.open_by_key(spreadsheet_id)
    except gspread.exceptions.APIError as exc:
        raise SheetsSourceError(
            "Unable to open the spreadsheet. Check SPREADSHEET_ID and service-account sharing."
        ) from exc
    except gspread.exceptions.SpreadsheetNotFound as exc:
        raise SheetsSourceError(
            "Spreadsheet not found. Check SPREADSHEET_ID and grant the service account access."
        ) from exc


def open_raw_worksheet(spreadsheet, raw_tab_name: str):
    if not raw_tab_name:
        raise SheetsSourceError("RAW_TAB_NAME is required.")
    try:
        return spreadsheet.worksheet(raw_tab_name)
    except gspread.exceptions.WorksheetNotFound as exc:
        raise SheetsSourceError(f"Raw worksheet {raw_tab_name!r} was not found.") from exc


def read_raw_values(settings: Settings) -> tuple[str, str, Sequence[Sequence[str]]]:
    """Read raw values without numeric coercion and without any write-capable operation."""
    client = connect(settings)
    spreadsheet = open_spreadsheet(client, settings.spreadsheet_id or "")
    worksheet = open_raw_worksheet(spreadsheet, settings.raw_tab_name)
    try:
        values = worksheet.get_all_values()
    except gspread.exceptions.APIError as exc:
        raise SheetsSourceError("Unable to read the raw worksheet values.") from exc
    return spreadsheet.title, worksheet.title, values


def read_target_table_values(
    settings: Settings, tab_names: Sequence[str] = TARGET_TABLE_TABS
) -> dict[str, list[list[str]] | None]:
    """Read the named tabs with the read-only scope; a missing tab maps to ``None``.

    Missing tabs are reported, never created. Only ``get_all_values`` is called.
    """
    client = connect(settings, read_only=True)
    spreadsheet = open_spreadsheet(client, settings.spreadsheet_id or "")
    tables: dict[str, list[list[str]] | None] = {}
    for name in tab_names:
        try:
            worksheet = spreadsheet.worksheet(name)
        except gspread.exceptions.WorksheetNotFound:
            tables[name] = None
            continue
        try:
            values = worksheet.get_all_values()
        except gspread.exceptions.APIError as exc:
            raise SheetsSourceError(f"Unable to read the {name} worksheet values.") from exc
        tables[name] = [["" if cell is None else str(cell) for cell in row] for row in values]
    return tables
