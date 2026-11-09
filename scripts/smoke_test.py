#!/usr/bin/env python3
"""Read-only connection smoke test for the configured raw worksheet."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from laptop_tracker.config import Settings
from laptop_tracker.schema import REQUIRED_RAW_COLUMNS, validate_headers
from laptop_tracker.sheets_source import SheetsSourceError, read_raw_values


def main() -> int:
    try:
        title, worksheet_name, values = read_raw_values(Settings.from_env())
        headers = validate_headers(values[0] if values else [])
    except (FileNotFoundError, SheetsSourceError, ValueError) as exc:
        print(f"Smoke test failed safely: {exc}", file=sys.stderr)
        return 1
    sample_cells = [cell for row in values[1:4] for cell in row[:3]]
    strings_ok = all(isinstance(cell, str) for cell in sample_cells)
    print(f"Spreadsheet title: {title}")
    print(f"Worksheet name: {worksheet_name}")
    print(f"Data rows: {max(len(values) - 1, 0)}")
    print(f"Headers: {', '.join(headers)}")
    print(f"Expected headers present: {set(REQUIRED_RAW_COLUMNS).issubset(headers)}")
    print(f"Sample values are strings: {strings_ok}")
    return 0 if strings_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
