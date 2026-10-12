"""Manual online collection and matching of explicitly listed product pages.

A small CSV manifest lists the product pages to fetch. The whole manifest is validated against
each retailer adapter's own ``product_request()`` rules and the selected working list before any
request is sent. Collection goes through ``scrape_one()`` with one shared ``Fetcher``, so the
existing host allowlists, live-use gate, timeouts, retries, checked redirects, and per-retailer
throttling all apply unchanged. Valid observations are stored with ``persist_scrape_results()``
and matched under the current matching context with the five-field ``ExactMatcher``.

``expected_target_id`` is a verification hint only. It never decides a match; a different
actual outcome is flagged for review. Nothing here searches, crawls categories, discovers URLs,
guesses variants, or derives a model from a title or URL.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .adapters import datablitz, laptop_factory, oct, pcc, pcx
from .match_run import (
    MATCH_EVIDENCE_COLUMNS,
    MATCH_RESULT_COLUMNS,
    NumberedObservation,
    evidence_records,
    result_record,
)
from .matching import ExactMatcher, MatchResult, MatchStatus
from .observation_store import (
    MatchingContext,
    ObservationStore,
    ObservationStoreError,
    observation_id,
)
from .offers import OfferObservation
from .price_tracking import match_stored_observations
from .scraping import (
    Fetcher,
    RetailerAdapter,
    ScrapeRequest,
    ScrapeResult,
    live_use_approved,
    redact_url,
    scrape_one,
)
from .target_master import TARGET_ID_PATTERN

MANIFEST_COLUMNS: tuple[str, ...] = (
    "request_id",
    "retailer_id",
    "product_url",
    "expected_target_id",
    "enabled",
)
REQUEST_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
ENABLED_VALUES = {"TRUE": True, "FALSE": False}


@dataclass(frozen=True)
class RetailerRoute:
    """How one canonical retailer ID is collected: its adapter and request builder."""

    retailer_id: str
    adapter_factory: Callable[[], RetailerAdapter]
    product_request: Callable[[str], ScrapeRequest]


ROUTES: dict[str, RetailerRoute] = {
    route.retailer_id: route
    for route in (
        RetailerRoute(
            laptop_factory.RETAILER_ID,
            laptop_factory.LaptopFactoryAdapter,
            laptop_factory.product_request,
        ),
        RetailerRoute(datablitz.RETAILER_ID, datablitz.DataBlitzAdapter, datablitz.product_request),
        RetailerRoute(pcx.RETAILER_ID, pcx.PCXAdapter, pcx.product_request),
        RetailerRoute(pcc.RETAILER_ID, pcc.PCCAdapter, pcc.product_request),
        RetailerRoute(oct.RETAILER_ID, oct.OCTAdapter, oct.product_request),
    )
}


# Manifest


@dataclass(frozen=True)
class ManifestIssue:
    row_number: int
    field: str
    code: str


class ManifestError(ValueError):
    """The manifest is unusable; no request is sent. Messages never quote cell values."""

    def __init__(self, issues: Sequence[ManifestIssue]) -> None:
        self.issues = tuple(issues)
        shown = "; ".join(f"row {i.row_number} {i.field}: {i.code}" for i in self.issues[:20])
        more = f"; and {len(self.issues) - 20} more" if len(self.issues) > 20 else ""
        super().__init__(f"Manifest has {len(self.issues)} issue(s): {shown}{more}.")


@dataclass(frozen=True)
class ManifestEntry:
    row_number: int
    request_id: str
    retailer_id: str
    product_url: str
    expected_target_id: str
    enabled: bool
    request: ScrapeRequest


def read_manifest(path: str | Path) -> list[dict[str, str]]:
    """Read every manifest cell as exact text. The header must be exactly the five columns."""
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        if sorted(headers) != sorted(MANIFEST_COLUMNS) or len(headers) != len(set(headers)):
            raise ManifestError([ManifestIssue(1, "header", "HEADER_NOT_MANIFEST_COLUMNS")])
        rows = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ManifestError([ManifestIssue(row_number, "", "EXTRA_CELLS")])
            values = {key: value or "" for key, value in row.items()}
            if all(not value.strip() for value in values.values()):
                continue
            rows.append(values | {"_row_number": str(row_number)})
    return rows


def _request_key(retailer_id: str, url: str) -> tuple[str, ...]:
    """Comparison key for duplicate detection: host case and a trailing slash are ignored."""
    parts = urlsplit(url)
    return (retailer_id, parts.scheme, (parts.hostname or "").lower(), parts.path.rstrip("/"),
            parts.query)


def validate_manifest(
    rows: Sequence[Mapping[str, str]], selected_target_ids: Mapping[str, int] | set[str]
) -> list[ManifestEntry]:
    """Validate every row (enabled or not) and return the entries, or raise ``ManifestError``.

    Each URL must be accepted unchanged by its retailer's ``product_request()``: PC Central and
    Octagon variant URLs are kept exactly, and no variant is ever added. An expected target must
    be in the selected working list, which was itself checked against TARGET_MASTER.
    """
    issues: list[ManifestIssue] = []
    entries: list[ManifestEntry] = []
    for index, row in enumerate(rows, start=2):
        row_number = int(row.get("_row_number", index))

        def issue(field: str, code: str, row_number: int = row_number) -> None:
            issues.append(ManifestIssue(row_number, field, code))

        values = {name: row.get(name, "") for name in MANIFEST_COLUMNS}
        for name, value in values.items():
            if value != value.strip():
                issue(name, "SURROUNDING_WHITESPACE")
        request_id = values["request_id"]
        if not request_id:
            issue("request_id", "MISSING_REQUEST_ID")
        elif not re.fullmatch(REQUEST_ID_PATTERN, request_id):
            issue("request_id", "INVALID_REQUEST_ID")
        enabled = ENABLED_VALUES.get(values["enabled"])
        if enabled is None:
            issue("enabled", "ENABLED_NOT_TRUE_OR_FALSE")
        expected = values["expected_target_id"]
        if expected:
            if not re.fullmatch(TARGET_ID_PATTERN, expected):
                issue("expected_target_id", "INVALID_TARGET_ID")
            elif expected not in selected_target_ids:
                issue("expected_target_id", "EXPECTED_TARGET_NOT_IN_SELECTED_LIST")
        route = ROUTES.get(values["retailer_id"])
        url = values["product_url"]
        request = None
        if not values["retailer_id"]:
            issue("retailer_id", "MISSING_RETAILER_ID")
        elif route is None:
            issue("retailer_id", "UNSUPPORTED_RETAILER_ID")
        if not url:
            issue("product_url", "MISSING_PRODUCT_URL")
        elif route is not None:
            try:
                request = route.product_request(url)
            except ValueError:
                issue("product_url", "URL_REJECTED_BY_ADAPTER")
            else:
                if request.url != url or request.retailer_id != route.retailer_id:
                    issue("product_url", "URL_CHANGED_BY_ADAPTER")
                    request = None
        if request is not None and enabled is not None and request_id:
            entries.append(
                ManifestEntry(
                    row_number, request_id, route.retailer_id, url, expected, enabled, request
                )
            )

    seen_ids: dict[str, int] = {}
    seen_requests: dict[tuple[str, ...], ManifestEntry] = {}
    for entry in entries:
        if entry.request_id in seen_ids:
            issues.append(ManifestIssue(entry.row_number, "request_id", "DUPLICATE_REQUEST_ID"))
        seen_ids.setdefault(entry.request_id, entry.row_number)
        key = _request_key(entry.retailer_id, entry.product_url)
        earlier = seen_requests.get(key)
        if earlier is not None:
            conflicting = (earlier.expected_target_id, earlier.enabled) != (
                entry.expected_target_id,
                entry.enabled,
            )
            code = "CONFLICTING_DUPLICATE_REQUEST" if conflicting else "DUPLICATE_REQUEST"
            issues.append(ManifestIssue(entry.row_number, "product_url", code))
        seen_requests.setdefault(key, entry)
    if not issues and not any(entry.enabled for entry in entries):
        issues.append(ManifestIssue(1, "enabled", "NO_ENABLED_REQUESTS"))
    if issues:
        raise ManifestError(sorted(issues, key=lambda i: (i.row_number, i.field, i.code)))
    return entries


# Collection


@dataclass(frozen=True)
class RequestOutcome:
    entry: ManifestEntry
    result: ScrapeResult

    @property
    def status(self) -> str:
        if self.result.observations and not self.result.failures:
            return "SUCCEEDED"
        if self.result.observations:
            return "PARTIAL"
        return "FAILED" if self.result.failures else "NO_OFFERS"


def collect(entries: Sequence[ManifestEntry], fetcher: Fetcher) -> list[RequestOutcome]:
    """Fetch each enabled entry once, in manifest order, through one shared fetcher.

    ``scrape_one`` applies the request, host, and live-use gates before anything is sent.
    """
    adapters = {retailer_id: route.adapter_factory() for retailer_id, route in ROUTES.items()}
    return [
        RequestOutcome(entry, scrape_one(adapters[entry.retailer_id], entry.request, fetcher))
        for entry in entries
        if entry.enabled
    ]


def adapter_summary() -> dict[str, dict[str, object]]:
    summary = {}
    for retailer_id, route in ROUTES.items():
        adapter = route.adapter_factory()
        summary[retailer_id] = {
            "adapter_version": adapter.adapter_version,
            "allowed_hosts": sorted(adapter.allowed_hosts),
            "live_use_approved": live_use_approved(adapter),
        }
    return summary


# Matching of collected observations


@dataclass(frozen=True)
class CollectedMatch:
    entry: ManifestEntry
    row_number: int  # Row in the exported observation CSV (header is row 1).
    observation: OfferObservation
    observation_id: str
    result: MatchResult


def collected_observations(
    outcomes: Sequence[RequestOutcome],
) -> list[tuple[ManifestEntry, int, OfferObservation]]:
    rows = []
    for outcome in outcomes:
        for observation in outcome.result.observations:
            rows.append((outcome.entry, len(rows) + 2, observation))
    return rows


def match_collected(
    store: ObservationStore,
    context: MatchingContext,
    matcher: ExactMatcher,
    collected: Sequence[tuple[ManifestEntry, int, OfferObservation]],
    *,
    now,
) -> tuple[int, list[CollectedMatch]]:
    """Record results for every unmatched stored observation, then report this run's offers.

    The detailed result (with field evidence) is recomputed for the report and must agree with
    the stored result for the same context; existing results are never replaced.
    """
    added = match_stored_observations(store, context, matcher, now=now)
    stored = store.match_results(context.context_id)
    matched = []
    for entry, row_number, observation in collected:
        key = observation_id(observation)
        result = matcher.match_observation(observation)
        record = stored.get(key)
        target = result.target_id if result.status is MatchStatus.EXACT else ""
        if record is None or (record.match_status, record.target_id) != (
            result.status.value,
            target or "",
        ):
            raise ObservationStoreError(
                "A stored matching result disagrees with the current matcher for this context."
            )
        matched.append(CollectedMatch(entry, row_number, observation, key, result))
    return added, matched


# Report records

REQUEST_COLUMNS: tuple[str, ...] = (
    "request_id",
    "manifest_row",
    "retailer_id",
    "product_url",
    "expected_target_id",
    "enabled",
    "request_status",
    "observations",
    "failures",
    "observation_ids",
    "failure_stages",
    "failure_codes",
)

FAILURE_COLUMNS: tuple[str, ...] = (
    "request_id",
    "manifest_row",
    "retailer_id",
    "product_url",
    "redacted_url",
    "stage",
    "error_code",
    "message",
    "retryable",
    "attempts",
    "http_status",
    "offer_index",
)

RESULT_COLUMNS: tuple[str, ...] = (
    "request_id",
    "expected_target_id",
    "expected_target_check",
    "selected_list_status",
    "selected_rank",
    "observation_id",
    "matching_context_id",
    *MATCH_RESULT_COLUMNS,
    "price",
    "currency",
    "price_type",
    "availability",
)
REVIEW_COLUMNS: tuple[str, ...] = ("review_reasons", *RESULT_COLUMNS)
EVIDENCE_COLUMNS: tuple[str, ...] = ("request_id", "observation_id", *MATCH_EVIDENCE_COLUMNS)


def request_rows(
    entries: Sequence[ManifestEntry],
    outcomes: Sequence[RequestOutcome] | None,
) -> list[dict[str, str]]:
    """One row per manifest entry; ``outcomes`` is None in preview mode."""
    by_id = {outcome.entry.request_id: outcome for outcome in outcomes or ()}
    rows = []
    for entry in entries:
        outcome = by_id.get(entry.request_id)
        if not entry.enabled:
            status = "SKIPPED_DISABLED"
        elif outcomes is None:
            status = "NOT_SENT_PREVIEW"
        else:
            assert outcome is not None
            status = outcome.status
        result = outcome.result if outcome else None
        rows.append(
            {
                "request_id": entry.request_id,
                "manifest_row": str(entry.row_number),
                "retailer_id": entry.retailer_id,
                "product_url": entry.product_url,
                "expected_target_id": entry.expected_target_id,
                "enabled": "TRUE" if entry.enabled else "FALSE",
                "request_status": status,
                "observations": str(len(result.observations)) if result else "",
                "failures": str(len(result.failures)) if result else "",
                "observation_ids": ";".join(observation_id(o) for o in result.observations)
                if result
                else "",
                "failure_stages": ";".join(f.stage.value for f in result.failures)
                if result
                else "",
                "failure_codes": ";".join(f.error_code for f in result.failures) if result else "",
            }
        )
    return rows


def failure_rows(outcomes: Sequence[RequestOutcome]) -> list[dict[str, str]]:
    rows = []
    for outcome in outcomes:
        entry = outcome.entry
        for failure in outcome.result.failures:
            rows.append(
                {
                    "request_id": entry.request_id,
                    "manifest_row": str(entry.row_number),
                    "retailer_id": entry.retailer_id,
                    "product_url": entry.product_url,
                    "redacted_url": redact_url(entry.product_url),
                    "stage": failure.stage.value,
                    "error_code": failure.error_code,
                    "message": failure.message,
                    "retryable": str(failure.retryable),
                    "attempts": str(failure.attempts),
                    "http_status": "" if failure.http_status is None else str(failure.http_status),
                    "offer_index": "" if failure.offer_index is None else str(failure.offer_index),
                }
            )
    return rows


def expected_target_check(entry: ManifestEntry, result: MatchResult) -> str:
    if not entry.expected_target_id:
        return "NO_EXPECTATION"
    if result.status is not MatchStatus.EXACT:
        return "NOT_EXACT"
    return "AS_EXPECTED" if result.target_id == entry.expected_target_id else "DIFFERENT_TARGET"


def result_row(
    match: CollectedMatch, selected_ranks: Mapping[str, int], context: MatchingContext
) -> dict[str, str]:
    result = match.result
    exact = result.status is MatchStatus.EXACT
    in_list = exact and result.target_id in selected_ranks
    observation = match.observation
    details = observation.details
    row = {
        "request_id": match.entry.request_id,
        "expected_target_id": match.entry.expected_target_id,
        "expected_target_check": expected_target_check(match.entry, result),
        "selected_list_status": ("SELECTED" if in_list else "NOT_IN_SELECTED_LIST")
        if exact
        else "",
        "selected_rank": str(selected_ranks[result.target_id]) if in_list else "",
        "observation_id": match.observation_id,
        "matching_context_id": context.context_id,
        **result_record(NumberedObservation(match.row_number, observation), result),
        "price": str(observation.price),
        "currency": observation.currency,
        "price_type": "" if details is None or details.price_type is None else details.price_type,
        "availability": observation.availability.value,
    }
    return {column: str(row[column]) for column in RESULT_COLUMNS}


def review_reasons(row: Mapping[str, str]) -> list[str]:
    reasons = []
    if row["match_status"] != MatchStatus.EXACT.value:
        reasons.append(f"MATCH_{row['match_status']}")
    if row["expected_target_check"] == "DIFFERENT_TARGET":
        reasons.append("EXPECTED_TARGET_DIFFERS")
    elif row["expected_target_check"] == "NOT_EXACT":
        reasons.append("EXPECTED_TARGET_NOT_MATCHED")
    if row["selected_list_status"] == "NOT_IN_SELECTED_LIST":
        reasons.append("EXACT_TARGET_NOT_IN_SELECTED_LIST")
    return reasons


def evidence_rows(match: CollectedMatch) -> list[dict[str, str]]:
    return [
        {"request_id": match.entry.request_id, "observation_id": match.observation_id} | row
        for row in evidence_records(NumberedObservation(match.row_number, match.observation),
                                    match.result)
    ]


def run_status(outcomes: Sequence[RequestOutcome], stored: bool) -> str:
    """SUCCESS only when every enabled request gave observations, without failures, and stored."""
    observations = sum(len(o.result.observations) for o in outcomes)
    if not observations or not stored:
        return "FAILED"
    if all(o.status == "SUCCEEDED" for o in outcomes):
        return "SUCCESS"
    return "PARTIAL_SUCCESS"
