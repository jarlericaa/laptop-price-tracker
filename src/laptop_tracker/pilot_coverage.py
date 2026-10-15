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
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .matching import (
    AMBIGUOUS_TARGET,
    DIFFERENT_CONFIGURATION,
    EXTRACTION_INCOMPLETE,
    IDENTITY_CONFLICT,
    INACTIVE_TARGET,
    MATCHED_CONFIGURATION,
    MODEL_UNVERIFIED,
    is_warning_code,
    match_basis_from_codes,
    offer_outcome,
)
from .price_tracking import price_note_codes

PRICE_AVAILABLE = "PRICE_AVAILABLE"
EXACT_PRICE_EXCLUDED = "EXACT_PRICE_EXCLUDED"
FETCH_FAILED = "FETCH_FAILED"
CANDIDATE_NOT_FETCHED = "CANDIDATE_NOT_FETCHED"
NO_CANDIDATE_FOUND = "NO_CANDIDATE_FOUND"
DISCOVERY_NOT_ATTEMPTED = "DISCOVERY_NOT_ATTEMPTED"
UNKNOWN_COVERAGE = "UNKNOWN_COVERAGE"

# Most informative first. Offer outcomes outrank discovery stages because an offer was fetched.
COVERAGE_PRIORITY: tuple[str, ...] = (
    PRICE_AVAILABLE,
    EXACT_PRICE_EXCLUDED,
    AMBIGUOUS_TARGET,
    IDENTITY_CONFLICT,
    MODEL_UNVERIFIED,
    EXTRACTION_INCOMPLETE,
    DIFFERENT_CONFIGURATION,
    INACTIVE_TARGET,
    FETCH_FAILED,
    CANDIDATE_NOT_FETCHED,
    NO_CANDIDATE_FOUND,
    DISCOVERY_NOT_ATTEMPTED,
    UNKNOWN_COVERAGE,
)
COVERAGE_TEXT = {
    PRICE_AVAILABLE: "Eligible price",
    EXACT_PRICE_EXCLUDED: "Matched configuration, price excluded",
    AMBIGUOUS_TARGET: "Identity ambiguous between catalog targets",
    IDENTITY_CONFLICT: "Identity conflict on the page",
    MODEL_UNVERIFIED: "Model not verified (other four fields present)",
    EXTRACTION_INCOMPLETE: "Extraction incomplete",
    DIFFERENT_CONFIGURATION: "Different configuration",
    INACTIVE_TARGET: "Only an inactive target has this identity",
    FETCH_FAILED: "Product page fetch failed",
    CANDIDATE_NOT_FETCHED: "Candidate found, not fetched",
    NO_CANDIDATE_FOUND: "Searched; no candidate found",
    DISCOVERY_NOT_ATTEMPTED: "Search not executed",
    UNKNOWN_COVERAGE: "No search record; coverage unknown",
}

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

    @property
    def outcome(self) -> str:
        return offer_outcome(self.match_status, self.reason_codes)

    def relation(self, target_id: str) -> str:
        """How this offer relates to ``target_id``; '' when it does not."""
        if self.match_status == "EXACT" and self.target_id == target_id:
            return "MATCHED"
        if target_id in self.candidate_target_ids:
            return "CANDIDATE"
        if self.expected_target_id == target_id:
            return "COLLECTED_FOR"
        return ""


def _offer_status(offer: OfferRecord, target_id: str) -> str:
    if offer.match_status == "EXACT":
        if offer.target_id != target_id:
            return DIFFERENT_CONFIGURATION  # Matched another catalog configuration.
        return (
            PRICE_AVAILABLE if offer.price_eligibility == "COMPARABLE" else EXACT_PRICE_EXCLUDED
        )
    outcome = offer.outcome
    return outcome if outcome != MATCHED_CONFIGURATION else EXACT_PRICE_EXCLUDED


def _rank(status: str) -> int:
    return COVERAGE_PRIORITY.index(status)


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
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """``(pair rows, offer detail rows)``; every input except ``offers`` is optional.

    ``history`` is a price-history report (``price_history.csv``) used only to disclose the last
    eligible observation with its real date when the current status is not a price.
    """
    offers = list(offers)
    discovery_logged = search_plan is not None or search_requests is not None

    def by_pair(rows, target_key="target_id", retailer_key="retailer_id"):
        grouped: dict[tuple[str, str], list[Mapping[str, str]]] = defaultdict(list)
        for row in rows or ():
            grouped[(row.get(target_key, ""), row.get(retailer_key, ""))].append(row)
        return grouped

    planned = by_pair(search_plan)
    executed = by_pair(search_requests)
    listed = by_pair(candidates)
    requested = by_pair(manifest, target_key="expected_target_id")
    report = {row.get("request_id", ""): row for row in request_report or ()}
    eligible_history: dict[tuple[str, str], Mapping[str, str]] = {}
    for row in history or ():
        if row.get("match_status") == "EXACT" and row.get("comparability_status") == "COMPARABLE":
            key = (row.get("target_id", ""), row.get("retailer_id", ""))
            if key not in eligible_history or (
                row.get("retrieved_at_utc", "") > eligible_history[key].get("retrieved_at_utc", "")
            ):
                eligible_history[key] = row

    pair_rows: list[dict[str, str]] = []
    detail_rows: list[dict[str, str]] = []
    for target_id in targets:
        for retailer_id in retailers:
            key = (target_id, retailer_id)
            related = [
                (offer, offer.relation(target_id)) for offer in offers
                if offer.retailer_id == retailer_id and offer.relation(target_id)
            ]
            for offer, relation in related:
                detail_rows.append(_detail_row(target_id, offer, relation))
            requests = requested.get(key, [])
            # FAILED (fetch or parse failure) and NO_OFFERS are failed requests; a disabled
            # (SKIPPED_DISABLED) row was never attempted.
            failed = [
                r for r in requests
                if report.get(r.get("request_id", ""), {}).get("request_status")
                in {"FAILED", "NO_OFFERS"}
            ]
            statuses = [(_offer_status(offer, target_id), offer) for offer, _ in related]
            notes: list[str] = []
            if statuses:
                statuses.sort(key=lambda item: (
                    _rank(item[0]),
                    _price_key(item[1]) if item[0] == PRICE_AVAILABLE else 0,
                ))
                status, best = statuses[0]
                if status == DIFFERENT_CONFIGURATION:
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
                    or row.get("decision", "") in {"COLLECT", "COLLECT_VARIANT"}
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
                elif executed.get(key):
                    status = NO_CANDIDATE_FOUND
                    categories = Counter(row.get("category", "") for row in listed.get(key, []))
                    if categories:
                        notes.append("results were: " + ", ".join(
                            f"{name} x{count}" for name, count in sorted(categories.items())
                        ))
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
            outcomes = Counter(s for s, _ in statuses)
            executed_rows = executed.get(key, [])
            pair_rows.append({
                "target_id": target_id,
                "retailer_id": retailer_id,
                "coverage_status": status,
                "coverage_text": COVERAGE_TEXT[status],
                "offers_related": str(len(related)),
                "offer_outcomes": ";".join(f"{s}:{n}" for s, n in sorted(outcomes.items())),
                "best_offer_url": best.offer_url if best else "",
                "best_offer_price": best.price if best else "",
                "best_offer_availability": best.availability if best else "",
                "best_offer_retrieved_at": best.retrieved_at if best else "",
                "best_offer_match_basis": match_basis_from_codes(best.reason_codes)
                if best and best.match_status == "EXACT" else "",
                "best_offer_warnings": ";".join(
                    [c for c in best.reason_codes if is_warning_code(c)]
                    + list(price_note_codes(best.bundle_flag))
                ) if best else "",
                "best_offer_reasons": ";".join(best.reason_codes) if best else "",
                "searches_planned": str(len(planned.get(key, []))),
                "searches_executed": str(len(executed_rows)),
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
            })
    return pair_rows, detail_rows


def _price_key(offer: OfferRecord) -> object:
    try:
        return float(offer.price)
    except ValueError:
        return float("inf")


def _detail_row(target_id: str, offer: OfferRecord, relation: str) -> dict[str, str]:
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
        "price_notes": ";".join(price_note_codes(offer.bundle_flag)),
    }
