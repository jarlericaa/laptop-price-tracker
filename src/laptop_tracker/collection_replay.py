"""Offline collection for discovery replays and collection rounds (2026-10-19 acceptance fixes).

Discovery continuation needs collection results to decide whether a search may stop, so an
offline replay of a saved discovery must be able to "collect" too, without any request:

- a manifest URL that an earlier collection already fetched reuses that collection's saved
  observation records unchanged (same observation identity; matched again with the current
  rules) or its recorded failure;
- a URL with a saved page body (a saved product inspection in a ``--search-cache`` folder) is
  parsed by the retailer adapter with the body's own retrieval time and hash: new evidence from
  that saved retrieval, never a rewritten earlier observation;
- anything else is recorded ``NOT_SENT_REPLAY_ONLY``.

Collection rounds of one discovery run are merged into one collection folder with the usual file
names, so coverage, reporting, and publication read a single place. Nothing here assigns a
target: the matcher decides, exactly as in a live collection.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .collection import (
    EVIDENCE_COLUMNS,
    FAILURE_COLUMNS,
    REQUEST_COLUMNS,
    RESULT_COLUMNS,
    REVIEW_COLUMNS,
    ROUTES,
    CollectedMatch,
    ManifestEntry,
    RequestOutcome,
    _request_key,
    evidence_rows,
    failure_rows,
    request_rows,
    result_row,
    review_reasons,
)
from .match_run import read_observations_csv
from .matching import ExactMatcher
from .observation_store import MatchingContext, observation_id
from .offers import (
    OBSERVATION_COLUMNS,
    OFFER_DETAIL_COLUMNS,
    ObservationValidationError,
    OfferObservation,
    redact_url,
)
from .scraping import FetchedPage, ParseError, ScrapeFailure, ScrapeResult, Stage, build_observation

SAVED_RECORD = "SAVED_OBSERVATION_RECORD"
SAVED_PAGE = "SAVED_PAGE_BODY"
NOT_SENT_REPLAY_ONLY = "NOT_SENT_REPLAY_ONLY"
COLLECTION_FILES = {
    "requests": ("request_report.csv", REQUEST_COLUMNS),
    "failures": ("request_failures.csv", FAILURE_COLUMNS),
    "observations": ("observations.csv", (*OBSERVATION_COLUMNS, *OFFER_DETAIL_COLUMNS)),
    "results": ("match_results.csv", RESULT_COLUMNS),
    "review": ("match_review_queue.csv", REVIEW_COLUMNS),
    "evidence": ("match_evidence.csv", EVIDENCE_COLUMNS),
}


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _write(path: Path, columns: Sequence[str], rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in columns} for row in rows)


@dataclass(frozen=True)
class SavedRequest:
    """One request of an earlier collection: its saved observations or failures, unchanged."""

    folder: Path
    request_id: str
    status: str
    observations: tuple[OfferObservation, ...]
    failures: tuple[ScrapeFailure, ...]


def load_saved_collections(folders: Sequence[Path]) -> dict[tuple[str, ...], SavedRequest]:
    """Saved requests by request key (retailer, URL without host case or trailing slash).

    The first folder that recorded a URL wins, so the order of ``folders`` is a priority.
    """
    saved: dict[tuple[str, ...], SavedRequest] = {}
    for folder in folders:
        observations = {
            observation_id(item.observation): item.observation
            for item in (read_observations_csv(folder / "observations.csv")
                         if (folder / "observations.csv").is_file() else [])
        }
        failures: dict[str, list[ScrapeFailure]] = {}
        for row in _rows(folder / "request_failures.csv"):
            failures.setdefault(row["request_id"], []).append(ScrapeFailure(
                row["retailer_id"], row.get("redacted_url") or redact_url(row["product_url"]),
                Stage(row["stage"]), row["error_code"], row.get("message", ""),
                retryable=row.get("retryable") == "True",
                attempts=int(row.get("attempts") or 0),
                http_status=int(row["http_status"]) if row.get("http_status") else None,
                offer_index=int(row["offer_index"]) if row.get("offer_index") else None,
            ))
        for row in _rows(folder / "request_report.csv"):
            if row.get("request_status") in {"SKIPPED_DISABLED", "NOT_SENT_PREVIEW",
                                             NOT_SENT_REPLAY_ONLY}:
                continue
            ids = [oid for oid in row.get("observation_ids", "").split(";") if oid]
            if any(oid not in observations for oid in ids):
                continue  # An incomplete saved record is never half-reused.
            key = _request_key(row["retailer_id"], row["product_url"])
            saved.setdefault(key, SavedRequest(
                folder, row["request_id"], row["request_status"],
                tuple(observations[oid] for oid in ids),
                tuple(failures.get(row["request_id"], ())),
            ))
    return saved


def parse_saved_page(entry: ManifestEntry, page: FetchedPage) -> ScrapeResult:
    """The adapter's offers from a saved page body, as a live fetch of it would have given."""
    adapter = ROUTES[entry.retailer_id].adapter_factory()
    safe_url = redact_url(entry.product_url)
    try:
        offers = list(adapter.parse(page))
    except ParseError as error:
        failure = ScrapeFailure(entry.retailer_id, safe_url, Stage.PARSE, error.code,
                                error.message, retryable=False, attempts=page.attempts)
        return ScrapeResult(entry.request, (), (failure,))
    observations, failures = [], []
    for index, offer in enumerate(offers):
        try:
            observations.append(build_observation(adapter, page, offer, index))
        except ObservationValidationError as error:
            failures.append(ScrapeFailure(
                entry.retailer_id, safe_url, Stage.VALIDATE, "INVALID_OFFER", str(error),
                retryable=False, attempts=page.attempts, http_status=page.status_code,
                offer_index=index,
            ))
    return ScrapeResult(entry.request, tuple(observations), tuple(failures))


def replay_outcomes(
    entries: Sequence[ManifestEntry],
    saved: Mapping[tuple[str, ...], SavedRequest],
    saved_pages: Mapping[str, FetchedPage],
) -> tuple[list[RequestOutcome], dict[str, str]]:
    """``(outcomes, request_id -> source)`` for the enabled entries; never sends a request.

    An entry with neither a saved request nor a saved page gets no outcome (not sent).
    """
    outcomes: list[RequestOutcome] = []
    sources: dict[str, str] = {}
    for entry in entries:
        if not entry.enabled:
            continue
        record = saved.get(_request_key(entry.retailer_id, entry.product_url))
        if record is not None:
            outcomes.append(RequestOutcome(entry, ScrapeResult(
                entry.request, record.observations, record.failures)))
            sources[entry.request_id] = f"{SAVED_RECORD}:{record.folder}#{record.request_id}"
            continue
        page = saved_pages.get(entry.product_url)
        if page is not None:
            outcomes.append(RequestOutcome(entry, parse_saved_page(entry, page)))
            sources[entry.request_id] = f"{SAVED_PAGE}:{page.content_sha256}"
            continue
        sources[entry.request_id] = NOT_SENT_REPLAY_ONLY
    return outcomes, sources


def write_replayed_collection(
    run_dir: Path,
    entries: Sequence[ManifestEntry],
    outcomes: Sequence[RequestOutcome],
    sources: Mapping[str, str],
    matcher: ExactMatcher,
    context: MatchingContext,
    ranks: Mapping[str, int],
) -> dict[str, object]:
    """Match the replayed observations with the current rules and write the collection files.

    No store is opened: reused records keep their identity and are imported (if at all) later
    by the report step; nothing here writes history.
    """
    run_dir.mkdir(parents=True, exist_ok=False)
    by_id = {outcome.entry.request_id: outcome for outcome in outcomes}
    requests = request_rows([e for e in entries if not e.enabled or e.request_id in by_id],
                            outcomes)
    for entry in entries:
        if entry.enabled and entry.request_id not in by_id:
            requests.append({
                "request_id": entry.request_id, "manifest_row": str(entry.row_number),
                "retailer_id": entry.retailer_id, "product_url": entry.product_url,
                "expected_target_id": entry.expected_target_id, "enabled": "TRUE",
                "request_status": NOT_SENT_REPLAY_ONLY, "observations": "", "failures": "",
                "observation_ids": "", "failure_stages": "", "failure_codes": "",
            })
    order = {entry.request_id: index for index, entry in enumerate(entries)}
    requests.sort(key=lambda row: order[row["request_id"]])
    for row in requests:
        row["replay_source"] = sources.get(row["request_id"], "")
    matches: list[CollectedMatch] = []
    observations: list[OfferObservation] = []
    for outcome in outcomes:
        for observation in outcome.result.observations:
            observations.append(observation)
            matches.append(CollectedMatch(
                outcome.entry, len(observations) + 1, observation, observation_id(observation),
                matcher.match_observation(observation),
            ))
    results = [result_row(match, ranks, context) for match in matches]
    _write(run_dir / "request_report.csv", (*REQUEST_COLUMNS, "replay_source"), requests)
    _write(run_dir / "request_failures.csv", FAILURE_COLUMNS, failure_rows(outcomes))
    _write(run_dir / "observations.csv", (*OBSERVATION_COLUMNS, *OFFER_DETAIL_COLUMNS),
           [observation.to_full_record() for observation in observations])
    _write(run_dir / "match_results.csv", RESULT_COLUMNS, results)
    _write(run_dir / "match_review_queue.csv", REVIEW_COLUMNS,
           [{"review_reasons": ";".join(r)} | row for row in results if (r := review_reasons(row))])
    _write(run_dir / "match_evidence.csv", EVIDENCE_COLUMNS,
           [row for match in matches for row in evidence_rows(match)])
    statuses = Counter(row["request_status"] for row in requests)
    summary = {
        "description": "Offline collection replay: saved observation records and saved page "
                       "bodies only; no request, no store write.",
        "mode": "REPLAY",
        "run_status": "REPLAY_ONLY",
        "requests_sent": 0,
        "request_statuses": dict(sorted(statuses.items())),
        "replay_sources": dict(sorted(Counter(
            source.split(":", 1)[0] for source in sources.values()).items())),
        "observations_collected": len(observations),
        "observations_csv_sha256": hashlib.sha256(
            (run_dir / "observations.csv").read_bytes()).hexdigest(),
        "matching_context": {"context_id": context.context_id,
                             "matching_rules_sha256": context.matching_rules_sha256},
        "match_statuses": dict(sorted(Counter(r["match_status"] for r in results).items())),
        "expected_target_checks": dict(sorted(Counter(
            r["expected_target_check"] for r in results).items())),
        "review_rows": sum(1 for row in results if review_reasons(row)),
    }
    (run_dir / "collection_run_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def merge_collection_rounds(round_dirs: Sequence[Path], out_dir: Path) -> dict[str, object]:
    """One collection folder from the rounds of a discovery run, in round order.

    CSV rows are concatenated; observation row numbers are renumbered so each result still names
    its own row in the merged ``observations.csv``. The summary keeps every round's summary.
    """
    out_dir.mkdir(parents=True, exist_ok=False)
    summaries = [
        json.loads((path / "collection_run_summary.json").read_text(encoding="utf-8"))
        if (path / "collection_run_summary.json").is_file() else {}
        for path in round_dirs
    ]
    offset = 0
    merged: dict[str, list[dict[str, str]]] = {key: [] for key in COLLECTION_FILES}
    request_columns = list(REQUEST_COLUMNS)
    for path in round_dirs:
        observations = _rows(path / "observations.csv")
        for key, (name, _) in COLLECTION_FILES.items():
            for row in _rows(path / name):
                if key in {"results", "review", "evidence"} and row.get("source_row_number"):
                    row["source_row_number"] = str(int(row["source_row_number"]) + offset)
                merged[key].append(row)
        for column in (_rows(path / "request_report.csv")[:1] or [{}])[0]:
            if column not in request_columns:
                request_columns.append(column)
        offset += len(observations)
    for key, (name, columns) in COLLECTION_FILES.items():
        _write(out_dir / name, request_columns if key == "requests" else columns, merged[key])
    modes = {summary.get("mode", "") for summary in summaries if summary}
    statuses = [summary.get("run_status", "") for summary in summaries if summary]
    if statuses and all(status == "SUCCESS" for status in statuses):
        status = "SUCCESS"
    elif "REPLAY_ONLY" in statuses:
        status = "REPLAY_ONLY"
    elif any(status in {"SUCCESS", "PARTIAL_SUCCESS"} for status in statuses):
        status = "PARTIAL_SUCCESS"
    else:
        status = "FAILED"
    results = merged["results"]
    summary = {
        "description": "Collection of a discovery run, merged from its collection rounds.",
        "mode": modes.pop() if len(modes) == 1 else ";".join(sorted(modes)),
        "run_status": status,
        "rounds": [{"folder": str(path), **summary}
                   for path, summary in zip(round_dirs, summaries, strict=True)],
        "requests_sent": sum(int(summary.get("requests_sent", 0) or 0) for summary in summaries),
        "observations_collected": len(merged["observations"]),
        "observations_csv_sha256": hashlib.sha256(
            (out_dir / "observations.csv").read_bytes()).hexdigest(),
        "match_statuses": dict(sorted(Counter(r["match_status"] for r in results).items())),
        "expected_target_checks": dict(sorted(Counter(
            r["expected_target_check"] for r in results).items())),
        "review_rows": len(merged["review"]),
    }
    if summaries and "matching_context" in summaries[0]:
        summary["matching_context"] = summaries[0]["matching_context"]
    (out_dir / "collection_run_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary
