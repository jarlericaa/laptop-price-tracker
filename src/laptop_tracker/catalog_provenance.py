"""Where the catalog inputs of a run came from, and whether they are current (2026-11-03).

``check_target_tables.py --from-sheets`` writes ``catalog_retrieval.json`` beside the validated
copies it saves: the retrieval time (UTC), the spreadsheet ID, and per tab the row and column
counts, the SHA-256 of the values as read, and the SHA-256 of the validated copy. A builder that
reads those copies re-hashes them, so a run can say which of three things its catalog is:

- ``FRESH_SHEETS_RETRIEVAL``: copies match a recorded read-only Sheets retrieval made no more
  than ``max_age_hours`` before the build.
- ``STALE_SHEETS_RETRIEVAL``: the same, but older (a cached snapshot of the Sheet).
- ``FILES_CHANGED_SINCE_RETRIEVAL``: a copy no longer matches its recorded fingerprint.
- ``LOCAL_SNAPSHOT_NO_RETRIEVAL_RECORD``: nothing records where the files came from.

Nothing here reads or writes a Sheet.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

MANIFEST_NAME = "catalog_retrieval.json"
MANIFEST_VERSION = "catalog-retrieval-1"
DEFAULT_MAX_AGE_HOURS = 24.0

FRESH = "FRESH_SHEETS_RETRIEVAL"
STALE = "STALE_SHEETS_RETRIEVAL"
CHANGED = "FILES_CHANGED_SINCE_RETRIEVAL"
NO_RECORD = "LOCAL_SNAPSHOT_NO_RETRIEVAL_RECORD"
DIRECT_READ = "FRESH_SHEETS_READ"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_retrieval_manifest(
    path: Path,
    *,
    source: str,
    spreadsheet_id: str | None,
    retrieved_at: datetime,
    raw_tables: Mapping[str, Sequence[Sequence[str]] | None],
    raw_fingerprint,
    validated_files: Mapping[str, Path | None],
    check_passed: bool,
) -> dict[str, object]:
    """Record one retrieval. ``raw_fingerprint`` hashes worksheet values (``sha256_values``)."""
    tabs: dict[str, object] = {}
    for name, values in raw_tables.items():
        copy = validated_files.get(name)
        tabs[name] = {
            "found": values is not None,
            "data_rows": max(len(values) - 1, 0) if values is not None else None,
            "columns": len(values[0]) if values else None,
            "values_sha256": raw_fingerprint(values) if values is not None else None,
            "validated_copy": copy.name if copy else None,
            "validated_sha256": file_sha256(copy) if copy and copy.is_file() else None,
        }
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "source": source,
        "spreadsheet_id": spreadsheet_id,
        "retrieved_at_utc": retrieved_at.astimezone(UTC).isoformat(timespec="seconds"),
        "check_passed": check_passed,
        "tabs": tabs,
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def read_retrieval_manifest(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if found.get("manifest_version") == MANIFEST_VERSION else None


def describe_catalog_inputs(
    files: Mapping[str, Path],
    manifest: Mapping[str, object] | None,
    *,
    now: datetime | None = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> dict[str, object]:
    """Classify the catalog inputs a build reads (see the module docstring)."""
    now = now or datetime.now(UTC)
    fingerprints = {name: file_sha256(path) for name, path in files.items() if path.is_file()}
    if manifest is None:
        return {
            "status": NO_RECORD,
            "detail": "No catalog_retrieval.json beside the catalog files: their origin and age "
            "are unknown (a cached or hand-copied snapshot).",
            "files_sha256": fingerprints,
        }
    tabs = manifest.get("tabs", {})
    mismatched = [
        name
        for name, fingerprint in fingerprints.items()
        if tabs.get(name, {}).get("validated_sha256") != fingerprint
    ]
    retrieved = datetime.fromisoformat(str(manifest["retrieved_at_utc"]))
    age_hours = (now - retrieved).total_seconds() / 3600
    if mismatched:
        status = CHANGED
        detail = f"{', '.join(sorted(mismatched))} differ from the recorded retrieval."
    elif manifest.get("source") != "GOOGLE_SHEETS_READ_ONLY" or not manifest.get("check_passed"):
        status = NO_RECORD
        detail = "The retrieval record is not a passing read-only Sheets retrieval."
    elif age_hours <= max_age_hours:
        status = FRESH
        detail = f"Retrieved {age_hours:.1f} h before this build."
    else:
        status = STALE
        detail = f"Retrieved {age_hours:.1f} h before this build (limit {max_age_hours:g} h)."
    return {
        "status": status,
        "detail": detail,
        "source": manifest.get("source"),
        "spreadsheet_id": manifest.get("spreadsheet_id"),
        "retrieved_at_utc": manifest.get("retrieved_at_utc"),
        "age_hours_at_build": round(age_hours, 2),
        "tabs": {
            name: {
                "data_rows": tabs.get(name, {}).get("data_rows"),
                "values_sha256": tabs.get(name, {}).get("values_sha256"),
                "validated_sha256": fingerprints.get(name),
            }
            for name in files
        },
    }
