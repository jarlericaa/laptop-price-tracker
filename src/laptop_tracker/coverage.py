"""Phase 5 coverage audit: accepted Top 100 source IDs against the validated target tables.

Inputs are the accepted Phase 4 Top 100 CSV and validated TARGET_MASTER, SOURCE_MODEL_MAP, and
MATCH_REVIEW snapshots. IDs are compared as exact text (``0042`` and ``42`` differ). The audit
only reports: it never promotes candidates, assigns target IDs, creates mappings, or writes Sheets.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .supporting_tables import TOP_100_SEED_COLUMNS, TableResult
from .target_master import (
    LISTING_ONLY,
    TargetMasterResult,
    effective_tracking_mode,
)


class CoverageStatus(StrEnum):
    MAPPED_CONFIGURATION = "MAPPED_CONFIGURATION"  # ACTIVE CONFIGURATION target.
    MAPPED_LISTING_ONLY = "MAPPED_LISTING_ONLY"  # ACTIVE LISTING_ONLY target for this listing.
    MAPPED_INACTIVE_TARGET = "MAPPED_INACTIVE_TARGET"
    MAPPED_INVALID_TARGET = "MAPPED_INVALID_TARGET"  # Target absent from the snapshot.
    LISTING_IDENTITY_MISMATCH = "LISTING_IDENTITY_MISMATCH"  # Listing IDs differ from Top 100.
    UNMAPPED_IN_REVIEW = "UNMAPPED_IN_REVIEW"  # Unresolved candidate.
    MISSING = "MISSING"


class CompleteReviewIssue(StrEnum):
    NO_SOURCE_MODEL_ID = "NO_SOURCE_MODEL_ID"
    NOT_MAPPED = "NOT_MAPPED"
    MAPPED_TO_INACTIVE_TARGET = "MAPPED_TO_INACTIVE_TARGET"
    MAPPED_TO_INVALID_TARGET = "MAPPED_TO_INVALID_TARGET"
    TRACKING_MODE_MISMATCH = "TRACKING_MODE_MISMATCH"  # Review mode differs from target mode.
    LISTING_IDENTITY_MISMATCH = "LISTING_IDENTITY_MISMATCH"


TOP_100_COVERAGE_COLUMNS: tuple[str, ...] = (
    "rank",
    "run_id",
    "shop_id",
    "item_id",
    "model_id",
    "coverage_status",
    "target_id",
    "target_status",
    "target_tracking_mode",
    "review_statuses",
    "review_row_count",
)

COMPLETE_WITHOUT_MAPPING_COLUMNS: tuple[str, ...] = (
    "review_source_row_number",
    "review_id",
    "source_model_id",
    "review_tracking_mode",
    "issue",
    "target_id",
)


class CoverageInputError(ValueError):
    """An input is missing, malformed, or failed validation; no coverage is reported."""


@dataclass(frozen=True)
class TopEntry:
    rank: str
    run_id: str
    shop_id: str
    item_id: str
    model_id: str


@dataclass(frozen=True)
class _Target:
    status: str
    mode: str
    listing: tuple[str, str, str]  # (shop_id, item_id, source_model_id) as exact text.


@dataclass
class CoverageReport:
    top_100_rows: int
    duplicate_top_100_rows: int
    rows: list[dict[str, str]]
    complete_without_mapping: list[dict[str, str]]
    counts: Counter[str] = field(default_factory=Counter)
    unmapped_review_status_counts: Counter[str] = field(default_factory=Counter)


def read_top_100(values: Sequence[Sequence[object]]) -> tuple[list[TopEntry], int, int]:
    """Distinct Top 100 model IDs in rank order (first occurrence kept), rows read, duplicates.

    Uses the same required columns and digit-only rule as ``seed_match_review.py``. shop_id and
    item_id are kept as exact text to check LISTING_ONLY targets.
    """
    if not values:
        raise CoverageInputError("Top 100 input has no header row.")
    headers = ["" if header is None else str(header).strip() for header in values[0]]
    missing = [column for column in TOP_100_SEED_COLUMNS if column not in headers]
    if missing:
        raise CoverageInputError(f"Top 100 input is missing columns: {', '.join(missing)}")
    index = {
        name: headers.index(name) for name in ("rank", "run_id", "shop_id", "item_id", "model_id")
    }
    entries: list[TopEntry] = []
    seen: set[str] = set()
    rows = duplicates = 0
    for row_number, row in enumerate(values[1:], start=2):
        cells = ["" if value is None else str(value) for value in row]
        if all(cell.strip() == "" for cell in cells):
            continue
        cells += [""] * (len(headers) - len(cells))
        rows += 1
        model_id = cells[index["model_id"]]
        if not model_id.isdigit() or not model_id.isascii():
            raise CoverageInputError(f"Top 100 row {row_number} has a blank or non-digit model_id.")
        if model_id in seen:
            duplicates += 1
            continue
        seen.add(model_id)
        entries.append(TopEntry(*(cells[index[name]] for name in index)))
    if not entries:
        raise CoverageInputError("Top 100 input has no data rows.")
    return entries, rows, duplicates


def audit_coverage(
    top_100_values: Sequence[Sequence[object]],
    target_master: TargetMasterResult,
    source_model_map: TableResult,
    match_review: TableResult,
) -> CoverageReport:
    """Classify each distinct Top 100 model ID and list COMPLETE review rows without a mapping.

    Every table must have passed its validator; a partly valid snapshot is refused because an
    excluded row would silently change the coverage counts. A Top 100 ID mapped to a
    LISTING_ONLY target must be that target's own listing: its shop_id, item_id, and model_id
    must equal the target's shop_id, item_id, and source_model_id exactly.
    """
    for name, valid in (
        ("TARGET_MASTER", target_master.is_valid),
        ("SOURCE_MODEL_MAP", source_model_map.is_valid),
        ("MATCH_REVIEW", match_review.is_valid),
    ):
        if not valid:
            raise CoverageInputError(f"{name} did not pass validation; run check_target_tables.py.")
    entries, row_count, duplicates = read_top_100(top_100_values)

    targets = {
        str(row.target_id): _Target(
            str(row.status),
            effective_tracking_mode(row.tracking_mode),
            (str(row.shop_id), str(row.item_id), str(row.source_model_id)),
        )
        for row in target_master.valid_rows.itertuples(index=False)
    }
    mapping = {
        str(row.source_model_id): str(row.target_id)
        for row in source_model_map.valid_rows.itertuples(index=False)
    }
    review_statuses: dict[str, list[str]] = {}
    for row in match_review.valid_rows.itertuples(index=False):
        if str(row.source_model_id):
            review_statuses.setdefault(str(row.source_model_id), []).append(str(row.status))

    def mapped_status(
        target_id: str, listing: tuple[str, str, str] | None
    ) -> tuple[CoverageStatus, _Target | None]:
        """``listing`` is the Top 100 (shop_id, item_id, model_id) when known."""
        target = targets.get(target_id)
        if target is None:
            return CoverageStatus.MAPPED_INVALID_TARGET, None
        if target.mode == LISTING_ONLY and listing is not None and listing != target.listing:
            return CoverageStatus.LISTING_IDENTITY_MISMATCH, target
        if target.status != "ACTIVE":
            return CoverageStatus.MAPPED_INACTIVE_TARGET, target
        if target.mode == LISTING_ONLY:
            return CoverageStatus.MAPPED_LISTING_ONLY, target
        return CoverageStatus.MAPPED_CONFIGURATION, target

    report = CoverageReport(row_count, duplicates, [], [])
    for entry in entries:
        statuses = review_statuses.get(entry.model_id, [])
        target_id = mapping.get(entry.model_id, "")
        target: _Target | None = None
        if target_id:
            listing = (entry.shop_id, entry.item_id, entry.model_id)
            coverage, target = mapped_status(target_id, listing)
        else:
            coverage = CoverageStatus.UNMAPPED_IN_REVIEW if statuses else CoverageStatus.MISSING
        status_text = ";".join(sorted(set(statuses)))
        report.counts[coverage.value] += 1
        if coverage is CoverageStatus.UNMAPPED_IN_REVIEW:
            report.unmapped_review_status_counts[status_text] += 1
        report.rows.append(
            {
                "rank": entry.rank,
                "run_id": entry.run_id,
                "shop_id": entry.shop_id,
                "item_id": entry.item_id,
                "model_id": entry.model_id,
                "coverage_status": coverage.value,
                "target_id": target_id,
                "target_status": target.status if target else "",
                "target_tracking_mode": target.mode if target else "",
                "review_statuses": status_text,
                "review_row_count": str(len(statuses)),
            }
        )

    for row in match_review.valid_rows.itertuples(index=False):
        if str(row.status) != "COMPLETE":
            continue
        source_id = str(row.source_model_id)
        review_mode = effective_tracking_mode(row.tracking_mode)
        target_id = mapping.get(source_id, "") if source_id else ""
        if not source_id:
            issue = CompleteReviewIssue.NO_SOURCE_MODEL_ID
        elif not target_id:
            issue = CompleteReviewIssue.NOT_MAPPED
        else:
            # A LISTING_ONLY review row names its listing; check it against the target exactly.
            listing = (
                (str(row.shop_id), str(row.item_id), source_id)
                if review_mode == LISTING_ONLY
                else None
            )
            coverage, target = mapped_status(target_id, listing)
            if target is not None and target.mode != review_mode:
                issue = CompleteReviewIssue.TRACKING_MODE_MISMATCH
            elif coverage in (
                CoverageStatus.MAPPED_CONFIGURATION,
                CoverageStatus.MAPPED_LISTING_ONLY,
            ):
                continue
            elif coverage is CoverageStatus.MAPPED_INACTIVE_TARGET:
                issue = CompleteReviewIssue.MAPPED_TO_INACTIVE_TARGET
            elif coverage is CoverageStatus.LISTING_IDENTITY_MISMATCH:
                issue = CompleteReviewIssue.LISTING_IDENTITY_MISMATCH
            else:
                issue = CompleteReviewIssue.MAPPED_TO_INVALID_TARGET
        report.complete_without_mapping.append(
            {
                "review_source_row_number": str(row.source_row_number),
                "review_id": str(row.review_id),
                "source_model_id": source_id,
                "review_tracking_mode": review_mode,
                "issue": issue.value,
                "target_id": target_id,
            }
        )
    return report
