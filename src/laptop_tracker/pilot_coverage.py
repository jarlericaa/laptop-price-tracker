"""Configuration x retailer coverage from recorded discovery and collection stages (2026-10-15).

For every configuration in scope and every compared retailer, one row says what actually
happened, using only recorded evidence:

- discovery logs: planned queries (``search_plan.csv``), executed searches
  (``search_requests.csv``), and ranked candidates with their decisions (``candidates.csv``);
- collection logs: the manifest, the per-request report, and the matched offers.

An offer relates to a configuration when it was matched to it, or listed as a candidate, or
collected for it by the manifest (``expected_target_id`` is a discovery hint, never an
assignment). Several offers for one pair are aggregated by status priority, so one failed
candidate never hides another eligible offer; every offer stays in the detail rows.

Nothing here searches, fetches, matches, or assigns a target. Without logs that prove a search
ran, the status is ``UNKNOWN_COVERAGE``, never ``NO_CANDIDATE_FOUND``. A product that was not
found is never reported as out of stock.

Coverage ledger (2026-10-19). Search context (why an offer was looked at) is kept apart from the
confirmed target assignment. Each row gives one current reason in words, the candidate link
behind a blank price, and the differing field values when an offer's verified fields show them.
Unconfirmed prices stay in their own column and never fill the confirmed price. Several
collections can be combined: the latest observation of each offer is current, earlier ones stay
listed as history. Configurations outside the reporting scope appear only when an offer was
confirmed for them (``CONFIRMED_OUTSIDE_SCOPE``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .matching import (
    AMBIGUOUS_TARGET,
    DIFFERENT_CONFIGURATION,
    EXTRACTION_INCOMPLETE,
    IDENTITY_CONFLICT,
    INACTIVE_TARGET,
    MATCHED_CONFIGURATION,
    MODEL_UNVERIFIED,
    SAME_MODEL_DIFFERENT_CONFIGURATION,
    core_differences,
    describe_differences,
    is_warning_code,
    match_basis_from_codes,
    offer_outcome,
    review_codes,
)
from .price_tracking import price_note_codes

PRICE_AVAILABLE = "PRICE_AVAILABLE"
EXACT_PRICE_EXCLUDED = "EXACT_PRICE_EXCLUDED"
CONFIRMED_OUTSIDE_SCOPE = "CONFIRMED_OUTSIDE_SCOPE"
FETCH_FAILED = "FETCH_FAILED"
CANDIDATE_NOT_FETCHED = "CANDIDATE_NOT_FETCHED"
NO_CANDIDATE_FOUND = "NO_CANDIDATE_FOUND"
SEARCH_FAILED = "SEARCH_FAILED"
DISCOVERY_NOT_ATTEMPTED = "DISCOVERY_NOT_ATTEMPTED"
UNKNOWN_COVERAGE = "UNKNOWN_COVERAGE"

# Most informative first. Offer outcomes outrank discovery stages because an offer was fetched.
COVERAGE_PRIORITY: tuple[str, ...] = (
    PRICE_AVAILABLE,
    EXACT_PRICE_EXCLUDED,
    CONFIRMED_OUTSIDE_SCOPE,
    AMBIGUOUS_TARGET,
    IDENTITY_CONFLICT,
    MODEL_UNVERIFIED,
    EXTRACTION_INCOMPLETE,
    SAME_MODEL_DIFFERENT_CONFIGURATION,
    DIFFERENT_CONFIGURATION,
    INACTIVE_TARGET,
    FETCH_FAILED,
    CANDIDATE_NOT_FETCHED,
    NO_CANDIDATE_FOUND,
    SEARCH_FAILED,
    DISCOVERY_NOT_ATTEMPTED,
    UNKNOWN_COVERAGE,
)
COVERAGE_TEXT = {
    PRICE_AVAILABLE: "Comparable offer",
    EXACT_PRICE_EXCLUDED: "Confirmed match, price excluded",
    CONFIRMED_OUTSIDE_SCOPE: "Confirmed match outside the reporting scope",
    AMBIGUOUS_TARGET: "Identity ambiguous between catalog targets",
    IDENTITY_CONFLICT: "Identity evidence conflicts",
    MODEL_UNVERIFIED: "Model not verified (other four fields present)",
    EXTRACTION_INCOMPLETE: "Specifications missing or extraction failed",
    SAME_MODEL_DIFFERENT_CONFIGURATION: "Same model, different configuration",
    DIFFERENT_CONFIGURATION: "Different configuration",
    INACTIVE_TARGET: "Only an inactive target has this identity",
    FETCH_FAILED: "Candidate found, fetching failed",
    CANDIDATE_NOT_FETCHED: "Candidate found, not fetched",
    NO_CANDIDATE_FOUND: "Searched; no candidate found",
    SEARCH_FAILED: "Search failed",
    DISCOVERY_NOT_ATTEMPTED: "Search not attempted",
    UNKNOWN_COVERAGE: "No search record; coverage unknown",
}
_SENT = frozenset({"RESULTS", "NO_RESULTS"})
_FAILED = frozenset({"SEARCH_ERROR", "UNSUPPORTED_LAYOUT"})

COVERAGE_COLUMNS: tuple[str, ...] = (
    "target_id",
    "retailer_id",
    "coverage_status",
    "coverage_text",
    "offers_related",
    "offer_outcomes",
    "best_offer_url",
    "best_offer_price",
    "best_offer_availability",
    "best_offer_retrieved_at",
    "best_offer_match_basis",
    "best_offer_warnings",
    "best_offer_reasons",
    "searches_planned",
    "searches_executed",
    "search_outcomes",
    "candidates_listed",
    "candidate_decisions",
    "manifest_requests",
    "failed_requests",
    "last_eligible_price",
    "last_eligible_retrieved_at",
    "detail",
    # Coverage ledger (2026-10-19).
    "in_reporting_scope",
    "current_reason",
    "candidate_url",
    "candidate_outcome",
    "differing_fields",
    "confirmed_price",
    "unconfirmed_candidate_price",
    "searches_failed",
    "searches_not_sent",
    "search_stop_reason",
    "current_offers",
    "historical_observations",
)
OFFER_DETAIL_COLUMNS: tuple[str, ...] = (
    "target_id",
    "retailer_id",
    "relation",
    "offer_url",
    "observation_id",
    "retrieved_at",
    "match_status",
    "outcome",
    "assigned_target_id",
    "match_basis",
    "reason_codes",
    "price",
    "availability",
    "price_eligibility",
    "bundle_flag",
    "price_notes",
    "is_current",
    "price_role",
    "differing_fields",
    "offer_terms_category",
)


@dataclass(frozen=True)
class OfferRecord:
    """One matched offer, as recorded (status and reason codes from the matcher)."""

    retailer_id: str
    offer_url: str
    observation_id: str
    retrieved_at: str
    match_status: str
    target_id: str
    candidate_target_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    price: str
    availability: str
    # "COMPARABLE", "NOT_COMPARABLE: <reasons>", or "NOT_PRICED: <status>".
    price_eligibility: str
    expected_target_id: str = ""
    bundle_flag: str = ""
    # The offer's verified (EXTRACTED) canonical fields, to explain a non-match.
    offer_attributes: Mapping[str, str] = field(default_factory=dict)
    # Nonblocking price notes under the active offer-terms policy; None = bundle-flag notes.
    price_notes: tuple[str, ...] | None = None
    offer_terms_category: str = ""
    offer_index: str = "0"

    @property
    def outcome(self) -> str:
        return offer_outcome(self.match_status, self.reason_codes)

    @property
    def offer_key(self) -> tuple[str, str, str]:
        return (self.retailer_id, self.offer_url, self.offer_index)

    @property
    def notes(self) -> tuple[str, ...]:
        return price_note_codes(self.bundle_flag) if self.price_notes is None else self.price_notes

    def relation(self, target_id: str, searched: Iterable[str] = ()) -> str:
        """How this offer relates to ``target_id``; '' when it does not.

        ``MATCHED`` is the confirmed assignment. ``CANDIDATE`` (a near-match hint),
        ``COLLECTED_FOR`` (the manifest's expected target), and ``SEARCHED_FOR`` (listed as a
        search candidate) are search context only.
        """
        if self.match_status == "EXACT" and self.target_id == target_id:
            return "MATCHED"
        if target_id in self.candidate_target_ids:
            return "CANDIDATE"
        if self.expected_target_id == target_id:
            return "COLLECTED_FOR"
        if target_id in searched:
            return "SEARCHED_FOR"
        return ""


def _same_model(differences: Sequence[tuple[str, str, str]], offer: OfferRecord) -> bool:
    return bool(differences) and "model" in offer.offer_attributes and all(
        name != "model" for name, _, _ in differences
    )


def _offer_status(
    offer: OfferRecord, target_id: str, target_row: Mapping[str, object] | None = None
) -> tuple[str, str]:
    """``(status, differing fields)`` of one offer for one configuration."""
    differences = core_differences(offer.offer_attributes, target_row or {})
    detail = describe_differences(differences)
    if offer.match_status == "EXACT":
        if offer.target_id != target_id:
            # Matched another catalog configuration.
            status = (SAME_MODEL_DIFFERENT_CONFIGURATION if _same_model(differences, offer)
                      else DIFFERENT_CONFIGURATION)
            return status, detail
        status = (
            PRICE_AVAILABLE if offer.price_eligibility == "COMPARABLE" else EXACT_PRICE_EXCLUDED
        )
        return status, ""
    outcome = offer.outcome
    if outcome == MATCHED_CONFIGURATION:
        return EXACT_PRICE_EXCLUDED, ""
    if outcome == DIFFERENT_CONFIGURATION and _same_model(differences, offer):
        outcome = SAME_MODEL_DIFFERENT_CONFIGURATION
    return outcome, detail


def _rank(status: str) -> int:
    return COVERAGE_PRIORITY.index(status)


def _bare(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.netloc.lower()}{parts.path}".rstrip("/")


def _current(offers: Sequence[OfferRecord]) -> set[str]:
    """Observation IDs of the latest retrieval of each offer; earlier ones are history."""
    latest: dict[tuple[str, str, str], str] = {}
    for offer in offers:
        latest[offer.offer_key] = max(latest.get(offer.offer_key, ""), offer.retrieved_at)
    return {o.observation_id for o in offers if o.retrieved_at == latest[o.offer_key]}


def _reason_text(status: str, best: OfferRecord | None, differing: str, notes: list[str]) -> str:
    """One sentence a reader can act on, without codes they must look up."""
    url = f" ({best.offer_url})" if best else ""
    if status == PRICE_AVAILABLE and best:
        return f"Comparable price {best.price} ({best.availability.lower()}){url}."
    if status == EXACT_PRICE_EXCLUDED and best:
        why = best.price_eligibility.split(":", 1)[-1].strip()
        return f"Confirmed match; price excluded: {why}{url}."
    if status == CONFIRMED_OUTSIDE_SCOPE and best:
        return f"Confirmed match at {best.price}, but this configuration is outside the " \
               f"reporting scope{url}."
    if status in {SAME_MODEL_DIFFERENT_CONFIGURATION, DIFFERENT_CONFIGURATION} and best:
        assigned = f"; confirmed as {best.target_id}" if best.match_status == "EXACT" else ""
        return f"{COVERAGE_TEXT[status]}: {differing or 'no verified differing field'}" \
               f"{assigned}{url}."
    if best:
        blocking = review_codes(best.reason_codes)
        return f"{COVERAGE_TEXT[status]}: {';'.join(blocking) or best.match_status}{url}."
    detail = "; ".join(note for note in notes if note)
    return f"{COVERAGE_TEXT[status]}" + (f": {detail}" if detail else "") + "."


def coverage(
    targets: Sequence[str],
    retailers: Sequence[str],
    offers: Iterable[OfferRecord],
    *,
    search_plan: Sequence[Mapping[str, str]] | None = None,
    search_requests: Sequence[Mapping[str, str]] | None = None,
    candidates: Sequence[Mapping[str, str]] | None = None,
    manifest: Sequence[Mapping[str, str]] | None = None,
    request_report: Sequence[Mapping[str, str]] | None = None,
    history: Sequence[Mapping[str, str]] | None = None,
    target_rows: Mapping[str, Mapping[str, object]] | None = None,
    search_jobs: Sequence[Mapping[str, str]] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """``(pair rows, offer detail rows)``; every input except ``offers`` is optional.

    ``targets`` is the reporting scope. ``history`` is a price-history report
    (``price_history.csv``) used only to disclose the last eligible observation with its real
    date when the current status is not a price. ``target_rows`` (catalog rows by target ID)
    lets a non-match name its differing values. ``search_jobs`` gives why discovery stopped.
    """
    offers = list(offers)
    current_ids = _current(offers)
    discovery_logged = search_plan is not None or search_requests is not None
    target_rows = target_rows or {}

    def by_pair(rows, target_key="target_id", retailer_key="retailer_id"):
        grouped: dict[tuple[str, str], list[Mapping[str, str]]] = defaultdict(list)
        for row in rows or ():
            grouped[(row.get(target_key, ""), row.get(retailer_key, ""))].append(row)
        return grouped

    planned = by_pair(search_plan)
    executed = by_pair(search_requests)
    listed = by_pair(candidates)
    jobs = by_pair(search_jobs)
    requested = by_pair(manifest, target_key="expected_target_id")
    report = {row.get("request_id", ""): row for row in request_report or ()}
    searched_urls: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (target_id, retailer_id), rows in listed.items():
        for row in rows:
            searched_urls[(target_id, retailer_id)].add(_bare(row.get("product_url", "")))
    eligible_history: dict[tuple[str, str], Mapping[str, str]] = {}
    for row in history or ():
        if row.get("match_status") == "EXACT" and row.get("comparability_status") == "COMPARABLE":
            key = (row.get("target_id", ""), row.get("retailer_id", ""))
            if key not in eligible_history or (
                row.get("retrieved_at_utc", "") > eligible_history[key].get("retrieved_at_utc", "")
            ):
                eligible_history[key] = row

    scope = list(dict.fromkeys(targets))
    outside = sorted({
        offer.target_id for offer in offers
        if offer.match_status == "EXACT" and offer.target_id and offer.target_id not in scope
        and offer.observation_id in current_ids
    })
    pair_rows: list[dict[str, str]] = []
    detail_rows: list[dict[str, str]] = []
    for target_id in [*scope, *outside]:
        in_scope = target_id in scope
        for retailer_id in retailers:
            key = (target_id, retailer_id)
            urls = searched_urls.get(key, set())
            related = []
            for offer in offers:
                if offer.retailer_id != retailer_id:
                    continue
                searched = (target_id,) if _bare(offer.offer_url) in urls else ()
                relation = offer.relation(target_id, searched)
                if relation and (in_scope or relation == "MATCHED"):
                    related.append((offer, relation))
            if not in_scope and not related:
                continue
            target_row = target_rows.get(target_id)
            for offer, relation in related:
                detail_rows.append(_detail_row(
                    target_id, offer, relation, offer.observation_id in current_ids,
                    _offer_status(offer, target_id, target_row)[1],
                ))
            current = [(o, r) for o, r in related if o.observation_id in current_ids]
            requests = requested.get(key, [])
            # FAILED (fetch or parse failure) and NO_OFFERS are failed requests; a disabled
            # (SKIPPED_DISABLED) row was never attempted.
            failed = [
                r for r in requests
                if report.get(r.get("request_id", ""), {}).get("request_status")
                in {"FAILED", "NO_OFFERS"}
            ]
            statuses = [
                (*_offer_status(offer, target_id, target_row), offer) for offer, _ in current
            ]
            if not in_scope:
                statuses = [
                    (CONFIRMED_OUTSIDE_SCOPE, differing, offer)
                    for _, differing, offer in statuses
                ]
            executed_rows = executed.get(key, [])
            sent = [row for row in executed_rows if row.get("outcome", "") in _SENT
                    or row.get("cache", "")]
            search_failures = [row for row in executed_rows if row.get("outcome", "") in _FAILED]
            not_sent = [row for row in executed_rows if row.get("outcome", "").startswith(
                "NOT_SENT")]
            notes: list[str] = []
            differing = ""
            if statuses:
                statuses.sort(key=lambda item: (
                    _rank(item[0]),
                    _price_key(item[2]) if item[0] == PRICE_AVAILABLE else 0,
                ))
                status, differing, best = statuses[0]
                if status in {DIFFERENT_CONFIGURATION, SAME_MODEL_DIFFERENT_CONFIGURATION}:
                    notes.append(
                        f"offer matched {best.target_id}" if best.match_status == "EXACT"
                        else "no catalog target has the offer's verified five fields"
                        + (f"; one field differs from {', '.join(best.candidate_target_ids)}"
                           if best.candidate_target_ids else "")
                    )
                if failed:
                    notes.append(f"{len(failed)} other request(s) failed")
            else:
                best = None
                # A candidate showing the configuration's code that discovery did not fetch
                # (budget, or judged a different configuration by its title) is a lead, not
                # an absence.
                leads = [
                    row for row in listed.get(key, [])
                    if row.get("category", "").startswith("CODE_MATCH")
                    or row.get("category", "") == "HISTORICAL_VERIFIED_URL"
                    or row.get("decision", "").startswith("COLLECT")
                ]
                if failed:
                    status = FETCH_FAILED
                    notes.append("; ".join(
                        f"{r.get('product_url', '')}: "
                        f"{report[r['request_id']].get('failure_codes', '')}"
                        for r in failed if r.get("request_id") in report
                    ))
                elif leads:
                    status = CANDIDATE_NOT_FETCHED
                    notes.append("; ".join(
                        f"{row.get('product_url', '')} ({row.get('decision', '')}: "
                        f"{row.get('decision_reason', '')})"
                        for row in leads
                    ))
                elif sent:
                    status = NO_CANDIDATE_FOUND
                    categories = Counter(row.get("category", "") for row in listed.get(key, []))
                    if categories:
                        notes.append("results were: " + ", ".join(
                            f"{name} x{count}" for name, count in sorted(categories.items())
                        ))
                    if search_failures:
                        notes.append(f"{len(search_failures)} other search(es) failed")
                elif search_failures:
                    status = SEARCH_FAILED
                    notes.append("; ".join(dict.fromkeys(
                        f"{row.get('outcome', '')}: {row.get('message', '')}".rstrip(": ")
                        for row in search_failures
                    )))
                elif not_sent:
                    status = DISCOVERY_NOT_ATTEMPTED
                    notes.append("; ".join(dict.fromkeys(
                        row.get("outcome", "") for row in not_sent)))
                elif executed_rows and not discovery_logged:
                    status = NO_CANDIDATE_FOUND
                elif planned.get(key):
                    status = DISCOVERY_NOT_ATTEMPTED
                    notes.append("queries planned but no search request recorded")
                elif discovery_logged:
                    status = DISCOVERY_NOT_ATTEMPTED
                    notes.append("not in the recorded search plan")
                else:
                    status = UNKNOWN_COVERAGE
            last = eligible_history.get(key)
            if status != PRICE_AVAILABLE and last is not None:
                notes.append(
                    f"last eligible price {last.get('price', '')} was observed "
                    f"{last.get('retrieved_at_utc', '')}; not refreshed by this run"
                )
            outcomes = Counter(s for s, _, _ in statuses)
            confirmed = best is not None and best.match_status == "EXACT" and (
                best.target_id == target_id
            )
            stop = "; ".join(dict.fromkeys(
                row.get("stop_reason", "") for row in jobs.get(key, []) if row.get("stop_reason")
            ))
            pair_rows.append({
                "target_id": target_id,
                "retailer_id": retailer_id,
                "coverage_status": status,
                "coverage_text": COVERAGE_TEXT[status],
                "offers_related": str(len(related)),
                "offer_outcomes": ";".join(f"{s}:{n}" for s, n in sorted(outcomes.items())),
                "best_offer_url": best.offer_url if best else "",
                # A confirmed price only; an unconfirmed offer's price has its own column.
                "best_offer_price": best.price if confirmed else "",
                "best_offer_availability": best.availability if best else "",
                "best_offer_retrieved_at": best.retrieved_at if best else "",
                "best_offer_match_basis": match_basis_from_codes(best.reason_codes)
                if best and best.match_status == "EXACT" else "",
                "best_offer_warnings": ";".join(
                    [c for c in best.reason_codes if is_warning_code(c)] + list(best.notes)
                ) if best else "",
                "best_offer_reasons": ";".join(best.reason_codes) if best else "",
                "searches_planned": str(len(planned.get(key, []))),
                "searches_executed": str(len(sent)),
                "search_outcomes": ";".join(
                    f"{name}:{count}" for name, count in sorted(
                        Counter(row.get("outcome", "") for row in executed_rows).items()
                    )
                ),
                "candidates_listed": str(len(listed.get(key, []))),
                "candidate_decisions": ";".join(
                    f"{name}:{count}" for name, count in sorted(
                        Counter(row.get("decision", "") for row in listed.get(key, [])).items()
                    )
                ),
                "manifest_requests": str(len(requests)),
                "failed_requests": str(len(failed)),
                "last_eligible_price": last.get("price", "") if last else "",
                "last_eligible_retrieved_at": last.get("retrieved_at_utc", "") if last else "",
                "detail": "; ".join(note for note in notes if note),
                "in_reporting_scope": "TRUE" if in_scope else "FALSE",
                "current_reason": _reason_text(status, best, differing, notes),
                "candidate_url": best.offer_url if best and not confirmed else (
                    _lead_url(listed.get(key, [])) if best is None else ""
                ),
                "candidate_outcome": best.outcome if best and not confirmed else "",
                "differing_fields": differing,
                "confirmed_price": best.price if confirmed and status in {
                    PRICE_AVAILABLE, EXACT_PRICE_EXCLUDED} else "",
                "unconfirmed_candidate_price": best.price if best and not confirmed else "",
                "searches_failed": str(len(search_failures)),
                "searches_not_sent": str(len(not_sent)),
                "search_stop_reason": stop,
                "current_offers": str(len(current)),
                "historical_observations": str(len(related) - len(current)),
            })
    return pair_rows, detail_rows


def _lead_url(rows: Sequence[Mapping[str, str]]) -> str:
    for row in rows:
        if row.get("category", "").startswith("CODE_MATCH") or row.get(
            "decision", ""
        ).startswith("COLLECT") or row.get("category", "") == "HISTORICAL_VERIFIED_URL":
            return row.get("product_url", "")
    return ""


def _price_key(offer: OfferRecord) -> object:
    try:
        return float(offer.price)
    except ValueError:
        return float("inf")


def _detail_row(
    target_id: str, offer: OfferRecord, relation: str, current: bool, differing: str
) -> dict[str, str]:
    confirmed = offer.match_status == "EXACT" and offer.target_id == target_id
    return {
        "target_id": target_id,
        "retailer_id": offer.retailer_id,
        "relation": relation,
        "offer_url": offer.offer_url,
        "observation_id": offer.observation_id,
        "retrieved_at": offer.retrieved_at,
        "match_status": offer.match_status,
        "outcome": offer.outcome,
        "assigned_target_id": offer.target_id,
        "match_basis": match_basis_from_codes(offer.reason_codes)
        if offer.match_status == "EXACT" else "",
        "reason_codes": ";".join(offer.reason_codes),
        "price": offer.price,
        "availability": offer.availability,
        "price_eligibility": offer.price_eligibility,
        "bundle_flag": offer.bundle_flag,
        "price_notes": ";".join(offer.notes),
        "is_current": "TRUE" if current else "FALSE",
        "price_role": "CONFIRMED" if confirmed else "UNCONFIRMED_NOT_COMPARED",
        "differing_fields": differing,
        "offer_terms_category": offer.offer_terms_category,
    }
