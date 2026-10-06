"""Offline batch matching of saved offer observations against a validated TARGET_MASTER snapshot.

Inputs are local CSV files: observations in ``OfferObservation.to_full_record()`` columns and a
TARGET_MASTER export. Outputs are flat, traceable records for local CSV reports. Nothing here
fetches pages, reads or writes Google Sheets, reads SOURCE_MODEL_MAP, or creates targets.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .matching import ExactMatcher, MatchResult, MatchStatus
from .normalization import CORE_MATCH_COLUMNS
from .offers import (
    OBSERVATION_COLUMNS,
    ObservationValidationError,
    OfferObservation,
    observation_from_record,
)
from .target_master import TARGET_MASTER_HEADERS, TargetMasterResult

MATCH_RESULT_COLUMNS: tuple[str, ...] = (
    "source_row_number",
    "retailer_id",
    "offer_url",
    "retrieved_at",
    "offer_index",
    "raw_title",
    "adapter_version",
    "content_sha256",
    "bundle_flag",
    "extractor",
    "match_status",
    "target_id",
    "candidate_target_ids",
    "reason_codes",
    *(f"offer_{field}" for field in CORE_MATCH_COLUMNS),
)

MATCH_EVIDENCE_COLUMNS: tuple[str, ...] = (
    "source_row_number",
    "retailer_id",
    "offer_url",
    "stage",
    "field",
    "status",
    "offer_value",
    "target_id",
    "target_value",
    "source_label",
    "source_text",
)


class ObservationFileError(ValueError):
    """The observation file cannot be used; no result is produced from a partial read."""


class TargetSnapshotError(ValueError):
    """The TARGET_MASTER snapshot did not pass validation, so nothing is matched against it."""


@dataclass(frozen=True)
class NumberedObservation:
    source_row_number: int
    observation: OfferObservation


def matcher_from_validated(result: TargetMasterResult) -> ExactMatcher:
    """Build a matcher only from a TARGET_MASTER snapshot in which every row passed.

    A partly invalid snapshot is refused rather than filtered: a dropped duplicate or malformed
    row could otherwise turn an ambiguous offer into an apparently clean match.
    """
    if not result.is_valid:
        raise TargetSnapshotError(
            "TARGET_MASTER snapshot did not pass validation; run check_target_tables.py first."
        )
    return ExactMatcher(result.valid_rows[list(TARGET_MASTER_HEADERS)])


def read_observations_csv(path: str | Path) -> list[NumberedObservation]:
    """Read every observation row or fail; errors name the row and field, never the value."""
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        missing = [column for column in OBSERVATION_COLUMNS if column not in headers]
        if missing:
            raise ObservationFileError(
                f"Observation file is missing columns: {', '.join(missing)}"
            )
        observations: list[NumberedObservation] = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ObservationFileError(f"Observation row {row_number} has extra cells.")
            if all(not (value or "").strip() for value in row.values()):
                continue
            try:
                observation = observation_from_record(row)
            except ObservationValidationError as error:
                raise ObservationFileError(
                    f"Observation row {row_number} is invalid ({error.field_name})."
                ) from error
            observations.append(NumberedObservation(row_number, observation))
    if not observations:
        raise ObservationFileError("Observation file has no data rows.")
    return observations


def match_all(
    matcher: ExactMatcher, observations: Iterable[NumberedObservation]
) -> list[tuple[NumberedObservation, MatchResult]]:
    return [(item, matcher.match_observation(item.observation)) for item in observations]


def result_record(item: NumberedObservation, result: MatchResult) -> dict[str, str]:
    offer = item.observation
    full = offer.to_full_record()
    attributes = result.offer_attributes or {}
    record = {
        "source_row_number": str(item.source_row_number),
        "retailer_id": result.retailer_id,
        "offer_url": result.offer_url,
        "retrieved_at": full["retrieved_at"],
        "offer_index": full["offer_index"],
        "raw_title": offer.raw_title,
        "adapter_version": offer.source.adapter_version,
        "content_sha256": offer.source.content_sha256,
        "bundle_flag": full["bundle_flag"],
        "extractor": result.extractor,
        "match_status": result.status.value,
        "target_id": result.target_id or "",
        "candidate_target_ids": ";".join(result.candidate_target_ids),
        "reason_codes": ";".join(result.reason_codes),
    }
    record |= {f"offer_{field}": attributes.get(field, "") for field in CORE_MATCH_COLUMNS}
    return {column: record[column] for column in MATCH_RESULT_COLUMNS}


def evidence_records(item: NumberedObservation, result: MatchResult) -> list[dict[str, str]]:
    base = {
        "source_row_number": str(item.source_row_number),
        "retailer_id": result.retailer_id,
        "offer_url": result.offer_url,
    }
    rows = [
        base
        | {
            "stage": "EXTRACTION",
            "field": entry.field,
            "status": entry.status.value,
            "offer_value": entry.value,
            "target_id": "",
            "target_value": "",
            "source_label": entry.source_label,
            "source_text": entry.source_text,
        }
        for entry in result.extraction_evidence
    ]
    rows += [
        base
        | {
            "stage": "MATCH",
            "field": entry.field,
            "status": entry.status.value,
            "offer_value": entry.offer_value,
            "target_id": entry.target_id,
            "target_value": entry.target_value,
            "source_label": "",
            "source_text": "",
        }
        for entry in result.evidence
    ]
    return [{column: row[column] for column in MATCH_EVIDENCE_COLUMNS} for row in rows]


def is_review_item(result: MatchResult) -> bool:
    """Everything except a clean EXACT match belongs in the review queue."""
    return result.status is not MatchStatus.EXACT


def write_records(path: Path, columns: Sequence[str], records: Iterable[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(records)
