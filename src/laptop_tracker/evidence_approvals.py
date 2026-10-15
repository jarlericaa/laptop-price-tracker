"""Owner-reviewed, listing-scoped interpretation decisions, never target assignments.

Each approval has exact retailer/variant URL scope and independently extracted core guards.
An approval is not manufacturer verification. Its bytes are included in matching context hashes.
"""
from __future__ import annotations

import csv
from pathlib import Path

APPROVALS_PATH = Path(__file__).resolve().parents[2] / "config/retailer_evidence_approvals.csv"
APPROVAL_COLUMNS = (
    "decision_id", "retailer_id", "offer_url", "field", "observed_value", "approved_value",
    "cpu_series", "screen_size_in", "ram_capacity_gb", "required_part_number", "approved_by",
    "approved_on", "notes",
)


def read_approvals() -> tuple[dict[str, str], ...]:
    """Fail on a broken policy file rather than silently change the matching contract."""
    with APPROVALS_PATH.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != APPROVAL_COLUMNS:
            raise ValueError("retailer evidence approvals: unexpected headers")
        rows = tuple(reader)
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("retailer evidence approvals: malformed row")
        required = set(APPROVAL_COLUMNS) - {"required_part_number", "notes"}
        if any(not row[name].strip() for name in required):
            raise ValueError("retailer evidence approvals: missing required field")
        if row["field"] not in {"model", "storage_capacity_gb"}:
            raise ValueError("retailer evidence approvals: unsupported field")
        key = row["retailer_id"], row["offer_url"], row["field"]
        if key in seen:
            raise ValueError("retailer evidence approvals: duplicate decision scope")
        seen.add(key)
    return rows
