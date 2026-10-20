"""Configuration x retailer coverage from recorded discovery and collection stages (2026-10-15).

For every configuration in scope and every compared retailer, one row says what actually
happened, using only recorded evidence:

- discovery logs: planned queries (``search_plan.csv``), executed searches
  (``search_requests.csv``), and ranked candidates with their decisions (``candidates.csv``);
- collection logs: the manifest, the per-request report, and the matched offers.

An offer relates to a configuration only through recorded lineage (2026-10-19 acceptance fixes):
it was matched to it (``MATCHED``), collected by a manifest request made for it
(``COLLECTED_FOR``; ``expected_target_id`` is a discovery hint, never an assignment), selected for
collection as one of its search candidates (``SEARCHED_FOR``, which keeps one product evaluated
for several configurations), or is a matcher near-match of the same model (``CANDIDATE``). A
search result that discovery rejected for the configuration (another model, unrelated) is kept as
``REJECTED_SEARCH_RESULT`` for audit and never explains the cell, even when the same page was
collected for another configuration. Several related offers for one pair are aggregated by status
priority, so one failed candidate never hides another eligible offer; every offer stays in the
detail rows.

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

import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .matching import (
    AMBIGUOUS_TARGET,
    CATALOG_IDENTITY_UNRESOLVED,
    DIFFERENT_CONFIGURATION,
    DIFFERENT_MODEL,
    EXTRACTION_INCOMPLETE,
    IDENTITY_CONFLICT,
    INACTIVE_TARGET,
    LISTED_SPEC_MATCH,
    MATCHED_CONFIGURATION,
    MODEL_UNVERIFIED,
    SAME_MODEL_DIFFERENT_CONFIGURATION,
    configuration_outcome,
    core_differences,
    describe_differences,
    is_warning_code,
    match_basis_from_codes,
    offer_outcome,
)
from .price_tracking import price_note_codes

PRICE_AVAILABLE = "PRICE_AVAILABLE"
EXACT_PRICE_EXCLUDED = "EXACT_PRICE_EXCLUDED"
# A listed-spec match for a broad-family target (listed_spec.py, 2026-10-20): a price under that
# limited basis; the specific product identity stays unresolved.
LISTED_SPEC_PRICE_AVAILABLE = "LISTED_SPEC_PRICE_AVAILABLE"
LISTED_SPEC_PRICE_EXCLUDED = "LISTED_SPEC_PRICE_EXCLUDED"
CONFIRMED_OUTSIDE_SCOPE = "CONFIRMED_OUTSIDE_SCOPE"
FETCH_FAILED = "FETCH_FAILED"
CANDIDATE_NOT_FETCHED = "CANDIDATE_NOT_FETCHED"
# Searches returned results, but every one was another model or unrelated (2026-10-19).
NO_RELEVANT_CANDIDATE = "NO_RELEVANT_CANDIDATE"
NO_CANDIDATE_FOUND = "NO_CANDIDATE_FOUND"
# The searches stopped before the query plan finished (budget, round limit, provider failure,
# replay without saved responses) and found no relevant candidate (2026-10-19, second review).
DISCOVERY_INCOMPLETE = "DISCOVERY_INCOMPLETE"
_INCOMPLETE_OUTCOMES = frozenset({"BUDGET_EXHAUSTED", "PROVIDER_FAILURE",
                                  "SEARCH_INCOMPLETE_REPLAY_ONLY"})
SEARCH_FAILED = "SEARCH_FAILED"
DISCOVERY_NOT_ATTEMPTED = "DISCOVERY_NOT_ATTEMPTED"
UNKNOWN_COVERAGE = "UNKNOWN_COVERAGE"

# Most informative first. Offer outcomes outrank discovery stages because an offer was fetched.
COVERAGE_PRIORITY: tuple[str, ...] = (
    PRICE_AVAILABLE,
    LISTED_SPEC_PRICE_AVAILABLE,
    EXACT_PRICE_EXCLUDED,
    LISTED_SPEC_PRICE_EXCLUDED,
    CONFIRMED_OUTSIDE_SCOPE,
    AMBIGUOUS_TARGET,
    IDENTITY_CONFLICT,
    CATALOG_IDENTITY_UNRESOLVED,
    MODEL_UNVERIFIED,
    EXTRACTION_INCOMPLETE,
    SAME_MODEL_DIFFERENT_CONFIGURATION,
    DIFFERENT_CONFIGURATION,
    DIFFERENT_MODEL,
    INACTIVE_TARGET,
    FETCH_FAILED,
    CANDIDATE_NOT_FETCHED,
    NO_RELEVANT_CANDIDATE,
    NO_CANDIDATE_FOUND,
    DISCOVERY_INCOMPLETE,
    SEARCH_FAILED,
    DISCOVERY_NOT_ATTEMPTED,
    UNKNOWN_COVERAGE,
)
# The discovery-stage statuses: why there is no related offer at all.
SEARCH_STAGE_STATUSES: tuple[str, ...] = (
    FETCH_FAILED, CANDIDATE_NOT_FETCHED, CATALOG_IDENTITY_UNRESOLVED, NO_RELEVANT_CANDIDATE,
    NO_CANDIDATE_FOUND, DISCOVERY_INCOMPLETE, SEARCH_FAILED, DISCOVERY_NOT_ATTEMPTED,
    UNKNOWN_COVERAGE,
)
COVERAGE_TEXT = {
    PRICE_AVAILABLE: "Exact-product price available",
    LISTED_SPEC_PRICE_AVAILABLE: "Listed-spec price available; specific identity unresolved",
    EXACT_PRICE_EXCLUDED: "Confirmed match, price excluded",
    LISTED_SPEC_PRICE_EXCLUDED: "Listed-spec match, price excluded",
    CONFIRMED_OUTSIDE_SCOPE: "Confirmed match outside the reporting scope",
    AMBIGUOUS_TARGET: "Identity ambiguous between catalog targets",
    IDENTITY_CONFLICT: "Conflicting evidence",
    CATALOG_IDENTITY_UNRESOLVED: "Catalog identity unresolved (broad family name)",
    MODEL_UNVERIFIED: "Model not verified (other four fields present)",
    EXTRACTION_INCOMPLETE: "Required specifications missing or extraction failed",
    SAME_MODEL_DIFFERENT_CONFIGURATION: "Same model, different configuration",
    DIFFERENT_CONFIGURATION: "Different configuration",
    DIFFERENT_MODEL: "Collected candidate is a different model",
    INACTIVE_TARGET: "Only an inactive target has this identity",
    FETCH_FAILED: "Candidate found, fetching failed",
    CANDIDATE_NOT_FETCHED: "Candidate found, not fetched",
    NO_RELEVANT_CANDIDATE: "Searched; no relevant candidate found",
    NO_CANDIDATE_FOUND: "Searched; no candidate found",
    DISCOVERY_INCOMPLETE: "Search incomplete; no relevant candidate in the searches run",
    SEARCH_FAILED: "Search failed",
    DISCOVERY_NOT_ATTEMPTED: "Search not attempted",
    UNKNOWN_COVERAGE: "No search record; coverage unknown",
}
_SENT = frozenset({"RESULTS", "NO_RESULTS"})
_FAILED = frozenset({"SEARCH_ERROR", "UNSUPPORTED_LAYOUT"})
# Candidate decisions that selected a page for collection for the configuration. A URL found for
# two configurations is collected once; the second keeps DUPLICATE_OF_EARLIER_CANDIDATE.
COLLECTED_DECISIONS = frozenset({
    "COLLECT", "COLLECT_VARIANT", "COLLECT_CHECK_CONFIGURATION", "DUPLICATE_OF_EARLIER_CANDIDATE",
})
# Candidate categories that may be the configuration's page (collectable categories).
RELEVANT_CATEGORIES = frozenset({
    "HISTORICAL_VERIFIED_URL", "SUPPLIED_URL", "CODE_MATCH",
    "CODE_MATCH_WITH_UNEXPLAINED_SUFFIX", "VERIFIED_PART_NUMBER_MATCH", "CODE_SPELLING_VARIANT",
    "CODE_IN_URL_ONLY",
    # A registered product name with no contrary clue (2026-10-19, second review).
    "PRODUCT_NAME_MATCH",
    # A listing stating a broad-family target's brand and family (2026-10-20).
    "FAMILY_LISTED_SPEC_CANDIDATE",
})
REJECTED_SEARCH_RESULT = "REJECTED_SEARCH_RESULT"
_VARIANT_REASON = re.compile(r"^Variant ([0-9]+):")

COVERAGE_COLUMNS: tuple[str, ...] = (
    "target_id",
    "retailer_id",
    "coverage_status",
    "coverage_text",
    "offers_related",
    "offer_outcomes",
    "best_offer_url",
    "best_offer_price",
    "price_basis",
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
    # Acceptance fixes (2026-10-19): lineage counts and the discovery end state.
    "relevant_candidates",
    "rejected_search_results",
    "discovery_outcome",
)
OFFER_DETAIL_COLUMNS: tuple[str, ...] = (
    "target_id",
    "retailer_id",
    "relation",
    "request_id",
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
    # The collection request that fetched this offer (match_results.csv), when known.
    request_id: str = ""
    # The offer's title (listed-spec family check) and its listed-spec association with its
    # price eligibility under the same price rules (2026-10-20). None: not evaluated.
    raw_title: str | None = None
    listed_target_ids: tuple[str, ...] = ()
    listed_ambiguous_ids: tuple[str, ...] = ()
    listed_price_eligibility: str = ""

    @property
    def outcome(self) -> str:
        return offer_outcome(self.match_status, self.reason_codes)

    @property
    def offer_key(self) -> tuple[str, str, str]:
        return (self.retailer_id, self.offer_url, self.offer_index)

    @property
    def notes(self) -> tuple[str, ...]:
        return price_note_codes(self.bundle_flag) if self.price_notes is None else self.price_notes

    def relation(
        self,
        target_id: str,
        *,
        collected: bool = False,
        rejected: bool = False,
        target_row: Mapping[str, object] | None = None,
    ) -> str:
        """How this offer relates to ``target_id``; '' when it does not.

        ``MATCHED`` is the confirmed assignment. ``COLLECTED_FOR`` (a manifest request made for
        the configuration), ``SEARCHED_FOR`` (selected for collection as its search candidate,
        ``collected``), and ``CANDIDATE`` (a matcher near-match of the same model) are search
        context only. ``REJECTED_SEARCH_RESULT`` (``rejected``: listed but not selected for the
        configuration) is kept for audit and never explains a cell.
        """
        if self.match_status == "EXACT" and self.target_id == target_id:
            return "MATCHED"
        if target_id in self.listed_target_ids:
            return "LISTED_SPEC"
        if target_id in self.listed_ambiguous_ids:
            return "LISTED_SPEC_AMBIGUOUS"
        if self.expected_target_id == target_id:
            return "COLLECTED_FOR"
        if collected:
            return "SEARCHED_FOR"
        if target_id in self.candidate_target_ids and self.outcome_for(
            target_id, target_row
        )[0] != DIFFERENT_MODEL:
            return "CANDIDATE"
        if rejected:
            return REJECTED_SEARCH_RESULT
        return ""

    def outcome_for(
        self, target_id: str, target_row: Mapping[str, object] | None
    ) -> tuple[str, str]:
        return configuration_outcome(
            self.match_status, self.target_id, self.reason_codes, self.offer_attributes,
            target_id, target_row, title=self.raw_title,
        )


def _offer_status(
    offer: OfferRecord, target_id: str, target_row: Mapping[str, object] | None = None
) -> tuple[str, str, str]:
    """``(status, detail, differing fields)`` of one related offer for one configuration (the
    rule shared with the weekly comparison: ``matching.configuration_outcome``)."""
    outcome, detail = offer.outcome_for(target_id, target_row)
    if outcome == MATCHED_CONFIGURATION:
        status = (
            PRICE_AVAILABLE if offer.price_eligibility == "COMPARABLE" else EXACT_PRICE_EXCLUDED
        )
        return status, "", ""
    if target_id in offer.listed_target_ids:
        status = (LISTED_SPEC_PRICE_AVAILABLE if offer.listed_price_eligibility == "COMPARABLE"
                  else LISTED_SPEC_PRICE_EXCLUDED)
        return status, detail, ""
    if target_id in offer.listed_ambiguous_ids:
        return AMBIGUOUS_TARGET, ("listed specifications fit several broad-family targets "
                                  "equally: " + ", ".join(offer.listed_ambiguous_ids)), ""
    if outcome == LISTED_SPEC_MATCH:
        # The listed specs fit, but the offer is associated with a more specific family target.
        return CATALOG_IDENTITY_UNRESOLVED, (
            "listed specifications fit, but the offer is associated with the more specific "
            "family target " + ", ".join(offer.listed_target_ids or ("(none)",))), ""
    differing = describe_differences(core_differences(offer.offer_attributes, target_row or {}))
    return outcome, detail, differing


def _rank(status: str) -> int:
    return COVERAGE_PRIORITY.index(status)


def _bare(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.netloc.lower()}{parts.path}".rstrip("/")


def _variant(url: str) -> str:
    return (parse_qs(urlsplit(url).query).get("variant") or [""])[0]


def candidate_collection_url(row: Mapping[str, str]) -> tuple[str, str]:
    """``(bare product URL, variant ID or '')`` a candidate row selected for collection.

    The recorded ``collection_url`` decides (runs from 2026-10-19 on); older rows fall back to the
    chosen variant named in the decision reason, then the seed URL. No variant means the page
    itself (one offer), so any variant of that page is the same collected product.
    """
    url = row.get("collection_url", "") or ""
    if url:
        return _bare(url), _variant(url)
    found = _VARIANT_REASON.match(row.get("decision_reason", "") or "")
    if found:
        return _bare(row.get("product_url", "")), found[1]
    seed = row.get("seed_url", "") or ""
    if seed and _variant(seed):
        return _bare(seed), _variant(seed)
    return _bare(row.get("product_url", "")), ""


@dataclass(frozen=True)
class CandidateLineage:
    """Which collected pages were selected for each (configuration, retailer), and which search
    results were rejected for it. Built from ``candidates.csv`` only; assigns nothing."""

    collected: Mapping[tuple[str, str], frozenset[tuple[str, str]]]
    collected_requests: Mapping[tuple[str, str], frozenset[str]]
    rejected: Mapping[tuple[str, str], frozenset[str]]

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, str]] | None) -> CandidateLineage:
        collected: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        requests: dict[tuple[str, str], set[str]] = defaultdict(set)
        rejected: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in rows or ():
            key = (row.get("target_id", ""), row.get("retailer_id", ""))
            # Selected for collection, or showing the configuration's own code (a relevant page
            # that a limit kept from being collected for it, collected for another configuration).
            if row.get("decision", "") in COLLECTED_DECISIONS or row.get(
                "category", ""
            ) in RELEVANT_CATEGORIES:
                collected[key].add(candidate_collection_url(row))
                if row.get("collection_request_id"):
                    requests[key].add(row["collection_request_id"])
            else:
                rejected[key].add(_bare(row.get("product_url", "")))
        return cls(
            {k: frozenset(v) for k, v in collected.items()},
            {k: frozenset(v) for k, v in requests.items()},
            {k: frozenset(v) for k, v in rejected.items()},
        )

    def was_collected_for(self, key: tuple[str, str], offer_url: str, request_id: str = "") -> bool:
        if request_id and request_id in self.collected_requests.get(key, ()):
            return True
        bare, variant = _bare(offer_url), _variant(offer_url)
        return any(
            bare == url and (not wanted or wanted == variant)
            for url, wanted in self.collected.get(key, ())
        )

    def was_rejected_for(self, key: tuple[str, str], offer_url: str) -> bool:
        return _bare(offer_url) in self.rejected.get(key, ())


def related_offers(
    target_id: str,
    retailer_id: str,
    offers: Iterable[OfferRecord],
    lineage: CandidateLineage,
    target_row: Mapping[str, object] | None,
) -> list[tuple[OfferRecord, str]]:
    """Every offer with a recorded relation to the pair, including audit-only rejected rows."""
    key = (target_id, retailer_id)
    related = []
    for offer in offers:
        if offer.retailer_id != retailer_id:
            continue
        relation = offer.relation(
            target_id,
            collected=lineage.was_collected_for(key, offer.offer_url, offer.request_id),
            rejected=lineage.was_rejected_for(key, offer.offer_url),
            target_row=target_row,
        )
        if relation:
            related.append((offer, relation))
    return related


def _current(offers: Sequence[OfferRecord]) -> set[str]:
    """Observation IDs of the latest retrieval of each offer; earlier ones are history."""
    latest: dict[tuple[str, str, str], str] = {}
    for offer in offers:
        latest[offer.offer_key] = max(latest.get(offer.offer_key, ""), offer.retrieved_at)
    return {o.observation_id for o in offers if o.retrieved_at == latest[o.offer_key]}


def _reason_text(status: str, best: OfferRecord | None, detail: str, notes: list[str]) -> str:
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
    if best:
        return f"{COVERAGE_TEXT[status]}: {detail or best.match_status}{url}."
    text = "; ".join(note for note in notes if note)
    return f"{COVERAGE_TEXT[status]}" + (f": {text}" if text else "") + "."


def _by_pair(rows, target_key="target_id", retailer_key="retailer_id"):
    grouped: dict[tuple[str, str], list[Mapping[str, str]]] = defaultdict(list)
    for row in rows or ():
        grouped[(row.get(target_key, ""), row.get(retailer_key, ""))].append(row)
    return grouped


class DiscoveryLogs:
    """Recorded discovery and collection stages, by (configuration, retailer).

    ``stage`` says why a pair has no related offer: a failed fetch, a relevant candidate that was
    not fetched, searches that found only other models (``NO_RELEVANT_CANDIDATE``) or nothing,
    failed or unsent searches, or no record. The coverage report and the weekly comparison use
    this one rule, so a blank cell and its coverage row give the same reason.
    """

    def __init__(
        self,
        *,
        search_plan: Sequence[Mapping[str, str]] | None = None,
        search_requests: Sequence[Mapping[str, str]] | None = None,
        candidates: Sequence[Mapping[str, str]] | None = None,
        manifest: Sequence[Mapping[str, str]] | None = None,
        request_report: Sequence[Mapping[str, str]] | None = None,
        search_jobs: Sequence[Mapping[str, str]] | None = None,
    ) -> None:
        self.logged = search_plan is not None or search_requests is not None
        self.planned = _by_pair(search_plan)
        self.executed = _by_pair(search_requests)
        self.listed = _by_pair(candidates)
        self.jobs = _by_pair(search_jobs)
        self.requested = _by_pair(manifest, target_key="expected_target_id")
        self.report = {row.get("request_id", ""): row for row in request_report or ()}
        self.lineage = CandidateLineage.from_rows(candidates)

    def searched(self, key: tuple[str, str]) -> bool:
        """True when the pair belongs to the recorded discovery (planned or searched)."""
        return bool(self.planned.get(key) or self.executed.get(key) or self.jobs.get(key))

    def sent(self, key: tuple[str, str]) -> list[Mapping[str, str]]:
        return [row for row in self.executed.get(key, [])
                if row.get("outcome", "") in _SENT or row.get("cache", "")]

    def search_failures(self, key: tuple[str, str]) -> list[Mapping[str, str]]:
        return [row for row in self.executed.get(key, []) if row.get("outcome", "") in _FAILED]

    def not_sent(self, key: tuple[str, str]) -> list[Mapping[str, str]]:
        return [row for row in self.executed.get(key, [])
                if row.get("outcome", "").startswith("NOT_SENT")]

    def failed_requests(self, key: tuple[str, str]) -> list[Mapping[str, str]]:
        # FAILED (fetch or parse failure) and NO_OFFERS are failed requests; a disabled
        # (SKIPPED_DISABLED) row was never attempted.
        return [
            r for r in self.requested.get(key, [])
            if self.report.get(r.get("request_id", ""), {}).get("request_status")
            in {"FAILED", "NO_OFFERS"}
        ]

    def relevant(self, key: tuple[str, str]) -> list[Mapping[str, str]]:
        return [row for row in self.listed.get(key, [])
                if row.get("category", "") in RELEVANT_CATEGORIES
                or row.get("decision", "") in COLLECTED_DECISIONS]

    def rejected(self, key: tuple[str, str]) -> list[Mapping[str, str]]:
        relevant = {id(row) for row in self.relevant(key)}
        return [row for row in self.listed.get(key, []) if id(row) not in relevant]

    def stop_reason(self, key: tuple[str, str]) -> str:
        return "; ".join(dict.fromkeys(
            row.get("stop_reason", "") for row in self.jobs.get(key, []) if row.get("stop_reason")
        ))

    def identity_kind(self, key: tuple[str, str]) -> str:
        return next((row.get("identity_kind", "") for row in self.jobs.get(key, [])
                     if row.get("identity_kind")), "")

    def discovery_outcome(self, key: tuple[str, str]) -> str:
        return "; ".join(dict.fromkeys(
            row.get("discovery_outcome", "") for row in self.jobs.get(key, [])
            if row.get("discovery_outcome")
        ))

    def _job_notes(self, key: tuple[str, str]) -> list[str]:
        notes = []
        for row in self.jobs.get(key, []):
            if row.get("resumed") == "TRUE":
                notes.append(
                    f"discovery resumed ({row.get('resume_reason', '')}); remaining tiers "
                    f"{row.get('tiers_after_resume', '') or 'none'}"
                )
            if row.get("discovery_outcome"):
                notes.append(f"discovery ended: {row['discovery_outcome']}")
            if row.get("search_coverage", "") not in {"", "COMPLETE"}:
                notes.append(f"search coverage {row['search_coverage']}")
        return notes

    def stage(self, key: tuple[str, str]) -> tuple[str, list[str]]:
        """``(status, notes)`` for a pair without any related offer."""
        notes: list[str] = []
        failed = self.failed_requests(key)
        leads = [row for row in self.relevant(key)
                 if row.get("decision", "") not in {"DUPLICATE_OF_EARLIER_CANDIDATE"}]
        sent = self.sent(key)
        search_failures = self.search_failures(key)
        not_sent = self.not_sent(key)
        if failed:
            status = FETCH_FAILED
            notes.append("; ".join(
                f"{r.get('product_url', '')}: "
                f"{self.report[r['request_id']].get('failure_codes', '')}"
                for r in failed if r.get("request_id") in self.report
            ))
        elif leads:
            # A candidate showing the configuration's code that was not fetched (budget, or
            # judged a different configuration by its title) is a lead, not an absence.
            status = CANDIDATE_NOT_FETCHED
            notes.append("; ".join(
                f"{row.get('product_url', '')} ({row.get('decision', '')}: "
                f"{row.get('decision_reason', '')})"
                for row in leads
            ))
        elif self.identity_kind(key) == "FAMILY_NAME" and (family_leads := [
            row for row in self.listed.get(key, [])
            if row.get("category", "") == "FAMILY_NAME_LEAD_CATALOG_UNRESOLVED"
        ]):
            # Since 2026-10-20 listings stating the brand and family are collected for a
            # listed-spec comparison; only listings without the brand remain review leads.
            status = CATALOG_IDENTITY_UNRESOLVED
            notes.append(
                f"{len(family_leads)} listing(s) name the family but not the target's brand; "
                "kept for review (not fetched)")
        elif sent and self.discovery_outcome(key) in _INCOMPLETE_OUTCOMES and not any(
            row.get("category", "") in RELEVANT_CATEGORIES for row in self.listed.get(key, [])
        ):
            status = DISCOVERY_INCOMPLETE
            notes.append(f"searches stopped before the query plan finished "
                         f"({self.discovery_outcome(key)}); {len(sent)} search page(s) read")
        elif sent:
            rejected = self.rejected(key)
            tiers = list(dict.fromkeys(row.get("tier", "") for row in sent if row.get("tier")))
            searched = f"{len(sent)} search page(s)" + (
                f" over {len(tiers)} query tier(s)" if tiers else "")
            if rejected:
                status = NO_RELEVANT_CANDIDATE
                categories = Counter(row.get("category", "") for row in rejected)
                notes.append(
                    f"{searched} returned {len(rejected)} result(s), none showing this "
                    "configuration's model code: " + ", ".join(
                        f"{name} x{count}" for name, count in sorted(categories.items()))
                    + " (rejected results kept in candidates.csv for audit)"
                )
            else:
                status = NO_CANDIDATE_FOUND
                notes.append(f"{searched} returned no results")
            if search_failures:
                notes.append(f"{len(search_failures)} other search(es) failed")
            if not_sent:
                notes.append(f"{len(not_sent)} search(es) not sent: " + "; ".join(dict.fromkeys(
                    row.get("outcome", "") for row in not_sent)))
            truncated = [row for row in sent if row.get("possibly_truncated") == "TRUE"]
            if truncated:
                notes.append(f"{len(truncated)} result page(s) at the retailer's result cap or "
                             "with unread next pages; the query plan ran, but the retailer's "
                             "catalog was not exhaustively searched")
        elif search_failures:
            status = SEARCH_FAILED
            notes.append("; ".join(dict.fromkeys(
                f"{row.get('outcome', '')}: {row.get('message', '')}".rstrip(": ")
                for row in search_failures
            )))
        elif not_sent:
            status = DISCOVERY_NOT_ATTEMPTED
            notes.append("; ".join(dict.fromkeys(row.get("outcome", "") for row in not_sent)))
        elif self.executed.get(key) and not self.logged:
            status = NO_CANDIDATE_FOUND
        elif self.planned.get(key):
            status = DISCOVERY_NOT_ATTEMPTED
            notes.append("queries planned but no search request recorded")
        elif self.logged:
            status = DISCOVERY_NOT_ATTEMPTED
            notes.append("not in the recorded search plan")
        else:
            status = UNKNOWN_COVERAGE
        return status, notes + self._job_notes(key)

    def lead_url(self, key: tuple[str, str]) -> str:
        for row in self.relevant(key):
            return row.get("product_url", "")
        return ""


def _read_csv(path: Path) -> list[dict[str, str]] | None:
    if not path.is_file():
        return None
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_discovery_logs(discovery_dir: Path, collection_dir: Path | None = None) -> DiscoveryLogs:
    """The recorded stages of one discovery folder (and its collection, by default inside it)."""
    collection_dir = collection_dir or discovery_dir / "collection"
    return DiscoveryLogs(
        search_plan=_read_csv(discovery_dir / "search_plan.csv"),
        search_requests=_read_csv(discovery_dir / "search_requests.csv"),
        candidates=_read_csv(discovery_dir / "candidates.csv"),
        manifest=_read_csv(discovery_dir / "manifest.csv"),
        request_report=_read_csv(collection_dir / "request_report.csv"),
        search_jobs=_read_csv(discovery_dir / "search_jobs.csv"),
    )


def discovery_started_at(discovery_dir: Path) -> str:
    """``started_at_utc`` of a discovery run (its summary), or '' when not recorded."""
    path = discovery_dir / "discovery_summary.json"
    if not path.is_file():
        return ""
    return str(json.loads(path.read_text(encoding="utf-8")).get("started_at_utc", ""))


def collection_lineage(
    logs: DiscoveryLogs, match_results: Iterable[Mapping[str, str]]
) -> dict[str, list[str]]:
    """``observation_id -> configurations`` each collected offer was requested or selected for.

    The manifest request's expected target, plus every configuration whose search selected the
    same page (or listed it with the configuration's own code). Rejected search results add
    nothing. Context only: never an assignment.
    """
    context: dict[str, list[str]] = {}
    for row in match_results:
        observation, retailer = row.get("observation_id", ""), row.get("retailer_id", "")
        if not observation:
            continue
        targets = [row.get("expected_target_id", "")]
        targets += [
            target for target, key_retailer in logs.lineage.collected
            if key_retailer == retailer and logs.lineage.was_collected_for(
                (target, key_retailer), row.get("offer_url", ""), row.get("request_id", ""))
        ]
        context.setdefault(observation, [])
        context[observation] += [t for t in dict.fromkeys(targets) if t
                                 and t not in context[observation]]
    return context


def search_stage_outcomes(logs: DiscoveryLogs) -> dict[tuple[str, str], tuple[str, str]]:
    """``(target_id, retailer_id) -> (status, explanation)`` for every searched pair, as the
    coverage report would explain it without a related offer."""
    keys = {*logs.planned, *logs.executed, *logs.jobs}
    outcomes = {}
    for key in keys:
        status, notes = logs.stage(key)
        outcomes[key] = (status, "; ".join(note for note in notes if note))
    return outcomes


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
    target_rows = target_rows or {}
    logs = DiscoveryLogs(
        search_plan=search_plan, search_requests=search_requests, candidates=candidates,
        manifest=manifest, request_report=request_report, search_jobs=search_jobs,
    )
    eligible_history: dict[tuple[str, str], Mapping[str, str]] = {}
    for row in history or ():
        eligible_for = [row.get("target_id", "")] if row.get("match_status") == "EXACT" and (
            row.get("comparability_status") == "COMPARABLE") else []
        if row.get("listed_spec_comparability") == "COMPARABLE":
            eligible_for += [t for t in row.get("listed_spec_target_ids", "").split(";") if t]
        for target_id in eligible_for:
            key = (target_id, row.get("retailer_id", ""))
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
        target_row = target_rows.get(target_id)
        for retailer_id in retailers:
            key = (target_id, retailer_id)
            related = [
                (offer, relation)
                for offer, relation in related_offers(
                    target_id, retailer_id, offers, logs.lineage, target_row)
                if in_scope or relation == "MATCHED"
            ]
            if not in_scope and not related:
                continue
            for offer, relation in related:
                detail_rows.append(_detail_row(
                    target_id, offer, relation, offer.observation_id in current_ids,
                    _offer_status(offer, target_id, target_row)[2],
                ))
            # Rejected search results are audit rows only; they never explain the cell.
            current = [(o, r) for o, r in related
                       if o.observation_id in current_ids and r != REJECTED_SEARCH_RESULT]
            failed = logs.failed_requests(key)
            statuses = [
                (*_offer_status(offer, target_id, target_row), offer) for offer, _ in current
            ]
            if not in_scope:
                statuses = [
                    (CONFIRMED_OUTSIDE_SCOPE, detail, differing, offer)
                    for _, detail, differing, offer in statuses
                ]
            notes: list[str] = []
            detail = differing = ""
            if statuses:
                statuses.sort(key=lambda item: (
                    _rank(item[0]),
                    _price_key(item[3]) if item[0] in {
                        PRICE_AVAILABLE, LISTED_SPEC_PRICE_AVAILABLE} else 0,
                ))
                status, detail, differing, best = statuses[0]
                if failed:
                    notes.append(f"{len(failed)} other request(s) failed")
            else:
                best = None
                status, notes = logs.stage(key)
            last = eligible_history.get(key)
            if status not in {PRICE_AVAILABLE, LISTED_SPEC_PRICE_AVAILABLE} and (
                last is not None
            ):
                notes.append(
                    f"last eligible price {last.get('price', '')} was observed "
                    f"{last.get('retrieved_at_utc', '')}; not refreshed by this run"
                )
            outcomes = Counter(s for s, _, _, _ in statuses)
            confirmed = best is not None and best.match_status == "EXACT" and (
                best.target_id == target_id
            )
            listed_price = best is not None and target_id in best.listed_target_ids and (
                status in {LISTED_SPEC_PRICE_AVAILABLE, LISTED_SPEC_PRICE_EXCLUDED})
            priced = confirmed or listed_price
            executed_rows = logs.executed.get(key, [])
            pair_rows.append({
                "target_id": target_id,
                "retailer_id": retailer_id,
                "coverage_status": status,
                "coverage_text": COVERAGE_TEXT[status],
                "offers_related": str(len(related)),
                "offer_outcomes": ";".join(f"{s}:{n}" for s, n in sorted(outcomes.items())),
                "best_offer_url": best.offer_url if best else "",
                # A confirmed or listed-spec price only; an unconfirmed offer's price has its
                # own column.
                "best_offer_price": best.price if priced else "",
                "price_basis": "EXACT_PRODUCT" if confirmed else (
                    "LISTED_SPEC" if listed_price else ""),
                "best_offer_availability": best.availability if best else "",
                "best_offer_retrieved_at": best.retrieved_at if best else "",
                "best_offer_match_basis": "LISTED_SPEC" if listed_price else (
                    match_basis_from_codes(best.reason_codes)
                    if best and best.match_status == "EXACT" else ""),
                "best_offer_warnings": ";".join(
                    [c for c in best.reason_codes if is_warning_code(c)] + list(best.notes)
                ) if best else "",
                "best_offer_reasons": ";".join(best.reason_codes) if best else "",
                "searches_planned": str(len(logs.planned.get(key, []))),
                "searches_executed": str(len(logs.sent(key))),
                "search_outcomes": ";".join(
                    f"{name}:{count}" for name, count in sorted(
                        Counter(row.get("outcome", "") for row in executed_rows).items()
                    )
                ),
                "candidates_listed": str(len(logs.listed.get(key, []))),
                "candidate_decisions": ";".join(
                    f"{name}:{count}" for name, count in sorted(
                        Counter(row.get("decision", "") for row in logs.listed.get(key, [])
                                ).items()
                    )
                ),
                "manifest_requests": str(len(logs.requested.get(key, []))),
                "failed_requests": str(len(failed)),
                "last_eligible_price": last.get("price", "") if last else "",
                "last_eligible_retrieved_at": last.get("retrieved_at_utc", "") if last else "",
                "detail": "; ".join(note for note in notes if note),
                "in_reporting_scope": "TRUE" if in_scope else "FALSE",
                "current_reason": _reason_text(status, best, detail, notes),
                "candidate_url": best.offer_url if best and not priced else (
                    logs.lead_url(key) if best is None else ""
                ),
                "candidate_outcome": status if best and not priced else "",
                "differing_fields": differing,
                "confirmed_price": best.price if priced and status in {
                    PRICE_AVAILABLE, EXACT_PRICE_EXCLUDED, LISTED_SPEC_PRICE_AVAILABLE,
                    LISTED_SPEC_PRICE_EXCLUDED} else "",
                "unconfirmed_candidate_price": best.price if best and not priced else "",
                "searches_failed": str(len(logs.search_failures(key))),
                "searches_not_sent": str(len(logs.not_sent(key))),
                "search_stop_reason": logs.stop_reason(key),
                "current_offers": str(len(current)),
                "historical_observations": str(sum(
                    1 for o, r in related
                    if o.observation_id not in current_ids and r != REJECTED_SEARCH_RESULT)),
                "relevant_candidates": str(len(logs.relevant(key))),
                "rejected_search_results": str(len(logs.rejected(key))),
                "discovery_outcome": logs.discovery_outcome(key),
            })
    return pair_rows, detail_rows


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
        "request_id": offer.request_id,
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
        "price_role": "CONFIRMED" if confirmed else "LISTED_SPEC" if relation == (
            "LISTED_SPEC") else (
            "AUDIT_ONLY_NOT_RELATED" if relation == REJECTED_SEARCH_RESULT
            else "UNCONFIRMED_NOT_COMPARED"
        ),
        "differing_fields": differing,
        "offer_terms_category": offer.offer_terms_category,
    }
