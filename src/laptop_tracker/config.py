"""Configuration loaded from the environment without exposing secret values."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    spreadsheet_id: str | None
    raw_tab_name: str
    service_account_file: Path | None
    service_account_json: str | None
    max_validation_errors: int
    skip_freshness_check: bool

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> Settings:
        if env_file:
            load_dotenv(env_file, override=False)
        file_value = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        return cls(
            spreadsheet_id=os.getenv("SPREADSHEET_ID", "").strip() or None,
            raw_tab_name=os.getenv("RAW_TAB_NAME", "raw").strip() or "raw",
            service_account_file=Path(file_value).expanduser() if file_value else None,
            service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or None,
            max_validation_errors=int(os.getenv("MAX_VALIDATION_ERRORS", "1000")),
            skip_freshness_check=_as_bool(os.getenv("SKIP_FRESHNESS_CHECK")),
        )

    def require_sheet_access(self) -> None:
        if not self.spreadsheet_id:
            raise ValueError("SPREADSHEET_ID is required; set it in .env or the environment.")
        if self.service_account_json:
            return
        if not self.service_account_file:
            raise ValueError(
                "Google credentials are required. Set GOOGLE_SERVICE_ACCOUNT_FILE or "
                "GOOGLE_SERVICE_ACCOUNT_JSON."
            )
        if not self.service_account_file.is_file():
            raise FileNotFoundError(
                "GOOGLE_SERVICE_ACCOUNT_FILE does not point to a readable credential file."
            )
