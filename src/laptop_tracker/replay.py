"""Offline replay of saved collections through the current extractor, matcher, and adapters.

Two paths are kept apart:

1. **Observation replay.** The original saved observation records (``to_full_record()`` CSV)
   are matched again with the current extractor and matcher. The records are not changed.
2. **HTML replay.** Saved response bodies are re-parsed with the current adapters to verify
   page layouts. Each body is paired with its original observation by content SHA-256, and the
   original retrieval time and attempt count are reused; no retrieval time is invented. A
   re-parsed record can differ from the stored one (a newer adapter adds evidence) while keeping
   the same observation identity, so it is written to its own CSV and is never imported into the
   append-only store. ``conflict_check`` shows that the store refuses such a record.

Nothing here makes a request, writes Google Sheets, or assigns a target.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .collection import ROUTES
from .extraction import ExtractionStatus
from .match_run import NumberedObservation
from .matching import ExactMatcher, MatchResult, MatchStatus
from .normalization import CORE_MATCH_COLUMNS
from .observation_store import ObservationConflictError, ObservationStore, observation_id
from .offer_terms import DEFAULT_OFFER_TERMS_POLICY, classify_offer_terms
from .offers import OfferObservation
from .price_tracking import price_rule_reasons
from .scraping import FetchedPage, ParseError, build_observation


@dataclass(frozen=True)
class ReparseFailure:
    source_row_number: int
    retailer_id: str
    offer_url: str
    code: str
    message: str


def reparse_saved_pages(
    observations: Sequence[NumberedObservation], html_dir: Path
) -> tuple[list[NumberedObservation], list[ReparseFailure]]:
    """Re-parse each observation's saved body (``<content_sha256>.html``) with its adapter."""
    replayed: list[NumberedObservation] = []
    failures: list[ReparseFailure] = []
    for item in observations:
        original = item.observation
        source = original.source

        def fail(code: str, message: str, original=original, item=item) -> None:
            failures.append(
                ReparseFailure(
                    item.source_row_number, original.retailer_id, original.offer_url, code, message
                )
            )

        route = ROUTES.get(original.retailer_id)
        body_path = html_dir / f"{source.content_sha256}.html"
        if route is None:
            fail("NO_ADAPTER", "No adapter is registered for this retailer.")
            continue
        if not body_path.is_file():
            fail("BODY_MISSING", "No saved body has this observation's content SHA-256.")
            continue
        body = body_path.read_bytes()
        if hashlib.sha256(body).hexdigest() != source.content_sha256:
            fail("BODY_HASH_MISMATCH", "The saved body does not match the recorded SHA-256.")
            continue
        page = FetchedPage(
            retailer_id=original.retailer_id,
            requested_url=source.requested_url,
            final_url=source.final_url,
            status_code=source.http_status,
            body=body,
            content_type=source.content_type,
            retrieved_at=original.retrieved_at,
            attempts=source.attempts,
        )
        adapter = route.adapter_factory()
        try:
            offers = list(adapter.parse(page))
        except ParseError as error:
            fail(error.code, error.message)
            continue
        if original.offer_index >= len(offers):
            fail("OFFER_INDEX_MISSING", "The re-parsed page has no offer at the saved index.")
            continue
        try:
            observation = build_observation(
                adapter, page, offers[original.offer_index], original.offer_index
            )
        except ValueError as error:
            fail("INVALID_OFFER", str(error))
            continue
        replayed.append(NumberedObservation(item.source_row_number, observation))
    return replayed, failures


def record_differences(original: OfferObservation, replayed: OfferObservation) -> list[str]:
    """Record columns whose text differs between the stored and the re-parsed observation."""
    before, after = original.to_full_record(), replayed.to_full_record()
    return [key for key in after if before.get(key, "") != after[key]]


def price_comparability(
    observation: OfferObservation,
    result: MatchResult,
    policy: str = DEFAULT_OFFER_TERMS_POLICY,
) -> str:
    """The weekly-comparison price rule applied to one offer, as a status plus reasons."""
    if result.status is not MatchStatus.EXACT:
        return f"NOT_PRICED: MATCH_{result.status.value}"
    details = observation.details
    reasons = price_rule_reasons(
        terms=classify_offer_terms(observation),
        policy=policy,
        retailer_id=observation.retailer_id,
        currency=observation.currency,
        availability=observation.availability.value,
        bundle_flag={None: "", True: "TRUE", False: "FALSE"}[
            details.bundle_flag if details else None
        ],
        price_type=details.price_type.value if details and details.price_type else "",
        price=observation.price,
    )
    return "COMPARABLE" if not reasons else "NOT_COMPARABLE: " + ";".join(reasons)


def core_summary(result: MatchResult) -> dict[str, str]:
    """``status:value`` per core field from the primary extraction evidence."""
    primary: dict[str, str] = {}
    for item in result.extraction_evidence:
        if item.field in CORE_MATCH_COLUMNS and item.status is not ExtractionStatus.SUPPORTING:
            primary.setdefault(item.field, f"{item.status.value}:{item.value}")
    return {field: primary.get(field, "MISSING:") for field in CORE_MATCH_COLUMNS}


COMPARISON_COLUMNS: tuple[str, ...] = (
    "source_row_number",
    "case_id",
    "retailer_id",
    "offer_url",
    "observation_id",
    "stage",
    "adapter_version",
    "match_status",
    "target_id",
    "reason_codes",
    *(f"core_{field}" for field in CORE_MATCH_COLUMNS),
    "supporting_fields",
    "price_comparability",
)


def comparison_row(
    stage: str,
    item: NumberedObservation,
    result: MatchResult,
    case_ids: Mapping[str, str],
) -> dict[str, str]:
    observation = item.observation
    supporting = sorted(
        {e.field for e in result.extraction_evidence if e.status is ExtractionStatus.SUPPORTING}
    )
    row = {
        "source_row_number": str(item.source_row_number),
        "case_id": case_ids.get(observation.source.content_sha256, ""),
        "retailer_id": observation.retailer_id,
        "offer_url": observation.offer_url,
        "observation_id": observation_id(observation),
        "stage": stage,
        "adapter_version": observation.source.adapter_version,
        "match_status": result.status.value,
        "target_id": result.target_id or "",
        "reason_codes": ";".join(result.reason_codes),
        **{f"core_{field}": value for field, value in core_summary(result).items()},
        "supporting_fields": ";".join(supporting),
        "price_comparability": price_comparability(observation, result),
    }
    return {column: row[column] for column in COMPARISON_COLUMNS}


def before_row(record: Mapping[str, str], case_ids: Mapping[str, str]) -> dict[str, str]:
    """A saved (pre-change) match result row in the comparison layout."""
    row = dict.fromkeys(COMPARISON_COLUMNS, "")
    row |= {
        "source_row_number": record.get("source_row_number", ""),
        "case_id": case_ids.get(record.get("content_sha256", ""), ""),
        "retailer_id": record.get("retailer_id", ""),
        "offer_url": record.get("offer_url", ""),
        "observation_id": record.get("observation_id", ""),
        "stage": "BEFORE (saved result)",
        "adapter_version": record.get("adapter_version", ""),
        "match_status": record.get("match_status", ""),
        "target_id": record.get("target_id", ""),
        "reason_codes": record.get("reason_codes", ""),
    }
    for field in CORE_MATCH_COLUMNS:
        value = record.get(f"offer_{field}", "")
        row[f"core_{field}"] = f"EXTRACTED:{value}" if value else "not extracted"
    return row


def match_items(
    matcher: ExactMatcher, items: Sequence[NumberedObservation]
) -> list[tuple[NumberedObservation, MatchResult]]:
    return [(item, matcher.match_observation(item.observation)) for item in items]


def conflict_check(store_path: Path, replayed: Sequence[OfferObservation], work_dir: Path) -> str:
    """Try to import re-parsed records into a throwaway copy of the store.

    The live store is opened read-only through SQLite's backup API and never written. The copy
    is deleted afterwards. Returns a short outcome description.
    """
    copy = work_dir / "conflict_check_copy.sqlite3"
    source = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    try:
        with ObservationStore.open(copy) as store:
            before = store.counts()["observations"]
            try:
                report = store.import_observations(
                    list(replayed),
                    source_label="replay conflict check (throwaway copy)",
                    imported_at=datetime.now(UTC),
                )
            except ObservationConflictError as error:
                after = store.counts()["observations"]
                return f"REFUSED: {error} Observations before/after: {before}/{after}."
            return (
                f"ACCEPTED: {report.new} new, {report.duplicates_in_store} already stored "
                "(no changed record shares an identity)."
            )
    finally:
        copy.unlink(missing_ok=True)
        Path(f"{copy}-journal").unlink(missing_ok=True)
