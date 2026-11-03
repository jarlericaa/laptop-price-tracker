"""Terminal outcome of one discovery job (one target at one retailer), 2026-11-03.

``QUERY_PLAN_EXHAUSTED`` used to cover everything that ended without a suitable offer: the last
2026-11-03 run reported 177 of 200 jobs that way, although they ended for different reasons (no
result, only rejected results, a capped search, an uncollected candidate, ...). This module keeps
the reasons apart. It is pure: it reads facts a job recorded and returns one outcome, so the
discovery command, a diagnostics script and the tests all use the same rule.

Seven ways a job can end without a price, plus success (``outcome_class``):

1. ``NO_RESULTS_RETURNED``: every attempted query was answered and none listed a product.
2. ``RESULTS_ALL_REJECTED``: results were found and every one was rejected (a non-laptop, an
   accessory, another model, another configuration, too many differences); the rejection classes
   are counted in ``result_classes``.
3. ``SEARCH_INCOMPLETE_*``: the answer cannot be read as complete: a capped result list (the
   predictive search returns at most ten products), result pages not read, a request or query
   budget, a provider failure, or an offline replay with no saved response. The legacy codes
   ``BUDGET_EXHAUSTED``, ``PROVIDER_FAILURE`` and ``SEARCH_INCOMPLETE_REPLAY_ONLY`` are kept.
4. ``PLAUSIBLE_CANDIDATE_NOT_COLLECTED``: a relevant candidate was found and not collected (a
   per-retailer limit, held for review, variants unreadable).
5. ``FETCH_DEFERRED`` / ``FETCH_FAILED``: a candidate page was not fetched yet (budget, rate limit)
   or could not be fetched.
6. ``PAGE_FETCHED_IDENTITY_UNRESOLVED``: a page was fetched and its identity or specifications did
   not resolve to the configuration (missing model, conflicting specifications, review required).
7. ``SUITABLE_OFFER_EXCLUDED_BY_STOCK`` / ``..._BY_PURCHASE_CONDITIONS``: the configuration was
   confirmed on a page whose offer cannot be bought as listed (out of stock, preorder, a bundle).
   The price decision itself stays with the weekly price report.

Success is ``SUITABLE_OFFER_FOUND``. ``NOT_EVALUATED_SEARCH_ONLY`` (a search-only run that never
collected the candidates it selected) and ``DISCOVERY_STEP_FAILED`` (the candidate step raised
an error; the candidates are kept for review) are the two remaining codes.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

# Terminal outcome codes. Existing codes keep their earlier meaning.
SUITABLE_OFFER_FOUND = "SUITABLE_OFFER_FOUND"
PLAUSIBLE_CANDIDATE_NOT_COLLECTED = "PLAUSIBLE_CANDIDATE_NOT_COLLECTED"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
PROVIDER_FAILURE = "PROVIDER_FAILURE"
SEARCH_INCOMPLETE_REPLAY_ONLY = "SEARCH_INCOMPLETE_REPLAY_ONLY"
NOT_EVALUATED_SEARCH_ONLY = "NOT_EVALUATED_SEARCH_ONLY"
NO_RESULTS_RETURNED = "NO_RESULTS_RETURNED"
RESULTS_ALL_REJECTED = "RESULTS_ALL_REJECTED"
SEARCH_INCOMPLETE_RESULT_CAP = "SEARCH_INCOMPLETE_RESULT_CAP"
SEARCH_INCOMPLETE_PAGES_UNREAD = "SEARCH_INCOMPLETE_PAGES_UNREAD"
FETCH_DEFERRED = "FETCH_DEFERRED"
FETCH_FAILED = "FETCH_FAILED"
PAGE_FETCHED_IDENTITY_UNRESOLVED = "PAGE_FETCHED_IDENTITY_UNRESOLVED"
SUITABLE_OFFER_EXCLUDED_BY_STOCK = "SUITABLE_OFFER_EXCLUDED_BY_STOCK"
SUITABLE_OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS = "SUITABLE_OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS"
DISCOVERY_STEP_FAILED = "DISCOVERY_STEP_FAILED"
# The legacy umbrella code. ``discovery_outcome`` still says it for jobs that ended without any of
# the reasons above, so earlier readers of that column keep working; ``terminal_outcome`` never
# does.
QUERY_PLAN_EXHAUSTED = "QUERY_PLAN_EXHAUSTED"

TERMINAL_OUTCOMES = (
    SUITABLE_OFFER_FOUND,
    SUITABLE_OFFER_EXCLUDED_BY_STOCK,
    SUITABLE_OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS,
    PAGE_FETCHED_IDENTITY_UNRESOLVED,
    FETCH_FAILED,
    FETCH_DEFERRED,
    PLAUSIBLE_CANDIDATE_NOT_COLLECTED,
    SEARCH_INCOMPLETE_RESULT_CAP,
    SEARCH_INCOMPLETE_PAGES_UNREAD,
    BUDGET_EXHAUSTED,
    PROVIDER_FAILURE,
    SEARCH_INCOMPLETE_REPLAY_ONLY,
    RESULTS_ALL_REJECTED,
    NO_RESULTS_RETURNED,
    NOT_EVALUATED_SEARCH_ONLY,
    DISCOVERY_STEP_FAILED,
)

# Outcome classes (the seven terminal states of the owner's list, numbered, plus success).
CLASS_SUITABLE = "0_SUITABLE_OFFER"
CLASS_NO_RESULTS = "1_NO_RESULTS_RETURNED"
CLASS_REJECTED = "2_RESULTS_REJECTED"
CLASS_INCOMPLETE = "3_SEARCH_INCOMPLETE"
CLASS_NOT_COLLECTED = "4_CANDIDATE_NOT_COLLECTED"
CLASS_FETCH = "5_FETCH_DEFERRED_OR_FAILED"
CLASS_UNRESOLVED = "6_PAGE_IDENTITY_UNRESOLVED"
CLASS_EXCLUDED = "7_OFFER_EXCLUDED_BY_STOCK_OR_TERMS"
CLASS_OTHER = "8_NOT_EVALUATED_OR_ERROR"

OUTCOME_CLASS = {
    SUITABLE_OFFER_FOUND: CLASS_SUITABLE,
    NO_RESULTS_RETURNED: CLASS_NO_RESULTS,
    RESULTS_ALL_REJECTED: CLASS_REJECTED,
    SEARCH_INCOMPLETE_RESULT_CAP: CLASS_INCOMPLETE,
    SEARCH_INCOMPLETE_PAGES_UNREAD: CLASS_INCOMPLETE,
    BUDGET_EXHAUSTED: CLASS_INCOMPLETE,
    PROVIDER_FAILURE: CLASS_INCOMPLETE,
    SEARCH_INCOMPLETE_REPLAY_ONLY: CLASS_INCOMPLETE,
    PLAUSIBLE_CANDIDATE_NOT_COLLECTED: CLASS_NOT_COLLECTED,
    FETCH_DEFERRED: CLASS_FETCH,
    FETCH_FAILED: CLASS_FETCH,
    PAGE_FETCHED_IDENTITY_UNRESOLVED: CLASS_UNRESOLVED,
    SUITABLE_OFFER_EXCLUDED_BY_STOCK: CLASS_EXCLUDED,
    SUITABLE_OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS: CLASS_EXCLUDED,
    NOT_EVALUATED_SEARCH_ONLY: CLASS_OTHER,
    DISCOVERY_STEP_FAILED: CLASS_OTHER,
}

# Search request outcomes (retailer_search) as this module reads them.
_ANSWERED = frozenset({"RESULTS", "NO_RESULTS"})
_NOT_READ = frozenset({"SEARCH_ERROR", "UNSUPPORTED_LAYOUT"})

# Candidate decisions (discover_products.select_and_inspect and the manifest step).
_REJECTED_DECISIONS = {
    "REJECTED_ACCESSORY": "ACCESSORY",
    "REJECTED_PRODUCT_TYPE": "NOT_A_LAPTOP",
    "SKIPPED_TOO_MANY_TITLE_DIFFERENCES": "TOO_MANY_TITLE_DIFFERENCES",
    "REVIEW_CATALOG_IDENTITY": "FAMILY_NAME_WITHOUT_BRAND",
}
_FETCH_DEFERRED_DECISIONS = frozenset(
    {"NOT_INSPECTED_BUDGET", "NOT_COLLECTED_BUDGET", "NOT_SENT_REPLAY_ONLY"}
)
_FETCH_FAILED_DECISIONS = frozenset({"REVIEW_INSPECTION_FAILED"})
_NOT_COLLECTED_DECISIONS = frozenset(
    {
        "NOT_SELECTED_LIMIT",
        "REVIEW_VARIANTS_UNREADABLE",
        "REVIEW_NO_MATCHING_VARIANT",
        "REVIEW_SEVERAL_MATCHING_VARIANTS",
        "REVIEW_MULTI_VARIANT_NOT_SUPPORTED",
        "REVIEW_DIFFERENT_CONFIGURATION_BY_TITLE",
    }
)
# Page-level outcomes (matching.configuration_outcome) after collection. A page that resolved to
# another configuration or model was read and rejected for this target; the others did not resolve.
_RESOLVED_OTHER = frozenset(
    {
        "DIFFERENT_CONFIGURATION",
        "SAME_MODEL_DIFFERENT_CONFIGURATION",
        "DIFFERENT_MODEL",
        "INACTIVE_TARGET",
    }
)
_SUITABLE_PAGE = frozenset({"MATCHED_CONFIGURATION", "LISTED_SPEC_MATCH"})
_REQUEST_DEFERRED = frozenset({"DEFERRED_RATE_LIMITED"})
_REQUEST_FAILED = frozenset({"FAILED"})
_REQUEST_NO_OFFERS = frozenset({"NO_OFFERS"})
PURCHASABLE = "IN_STOCK"


@dataclass(frozen=True)
class CandidateFact:
    """What one search result or lead came to: its category, decision and (when collected) its
    request status. ``relevant`` marks a result that could have been this configuration."""

    category: str
    decision: str = ""
    relevant: bool = False
    request_status: str = ""


@dataclass(frozen=True)
class OfferFact:
    """One collected offer judged for this job's configuration."""

    url: str
    outcome: str  # configuration_outcome(): MATCHED_CONFIGURATION, MODEL_UNVERIFIED, ...
    availability: str = ""
    # Price-eligibility reasons of the offer's own purchase terms (offer_terms.terms_price_effect:
    # RESTRICTED_PRICE, PAID_PACKAGE, ...), empty for an ordinary offer.
    purchase_exclusions: tuple[str, ...] = ()


@dataclass
class JobEvidence:
    """The facts of one finished job. Build with the job's records; nothing here is a request."""

    search_outcomes: Sequence[str] = ()
    capped: bool = False  # Some answered search was cut at its result cap (predictive: ten).
    pages_unread: bool = False  # A result page existed that was not read (--max-search-pages).
    legacy_outcome: str = ""  # The job's discovery_outcome before this module (kept for budgets).
    stop_reason: str = ""
    candidates: Sequence[CandidateFact] = ()
    offers: Sequence[OfferFact] = ()
    step_failed: bool = False
    collected: bool = False  # A collection round ran after this job's candidates were chosen.
    manifest_rows: int = 0  # Candidate pages this job asked the collector for.
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TerminalOutcome:
    code: str
    detail: str = ""
    result_classes: str = ""  # "ACCESSORY:2;UNRELATED:7": what the results were, by class.
    rejection_reasons: str = ""  # Why results were rejected, counted.

    @property
    def outcome_class(self) -> str:
        return OUTCOME_CLASS[self.code]


def _counts(values: Iterable[str]) -> str:
    return ";".join(f"{name}:{n}" for name, n in sorted(Counter(v for v in values if v).items()))


def result_classes(candidates: Sequence[CandidateFact]) -> str:
    """Candidate categories of a job's results, counted (the raw classes, before any decision)."""
    return _counts(c.category for c in candidates)


def rejection_reasons(candidates: Sequence[CandidateFact], offers: Sequence[OfferFact] = ()) -> str:
    """Why results were rejected, counted: a non-laptop or accessory class, another model, too many
    title differences, a page that resolved to another configuration."""
    reasons: list[str] = []
    for fact in candidates:
        if fact.decision in _REJECTED_DECISIONS:
            reasons.append(_REJECTED_DECISIONS[fact.decision])
        elif not fact.relevant and fact.category not in {"HISTORICAL_VERIFIED_URL", "SUPPLIED_URL"}:
            reasons.append(fact.category)
    reasons += [f"PAGE_{offer.outcome}" for offer in offers if offer.outcome in _RESOLVED_OTHER]
    return _counts(reasons)


def classify_job(evidence: JobEvidence) -> TerminalOutcome:
    """The single terminal outcome of a job. Order is nearest-to-a-price first: a confirmed offer,
    a fetched page that did not resolve, a candidate page not fetched, a relevant candidate not
    collected, a search that cannot be read as complete, results that were all rejected, and
    finally no results at all."""
    classes = result_classes(evidence.candidates)
    rejected = rejection_reasons(evidence.candidates, evidence.offers)

    def done(code: str, detail: str = "") -> TerminalOutcome:
        return TerminalOutcome(code, detail, classes, rejected)

    if evidence.step_failed:
        return done(
            DISCOVERY_STEP_FAILED,
            "the candidate step raised an error; the job's "
            "results are kept in candidates.csv for review",
        )
    suitable = [o for o in evidence.offers if o.outcome in _SUITABLE_PAGE]
    if suitable:
        # Stock is classified apart from identity and from purchase terms. An offer that is in
        # stock on ordinary terms is the success; one in stock whose terms block its price is
        # excluded by those terms; the rest are not buyable now. The weekly price policy may
        # still show an out-of-stock price with a note: this outcome says only what a customer
        # could buy today.
        buyable = [
            o for o in suitable if o.availability == PURCHASABLE and not o.purchase_exclusions
        ]
        if buyable:
            return done(SUITABLE_OFFER_FOUND, buyable[0].url)
        terms = [o for o in suitable if o.availability == PURCHASABLE and o.purchase_exclusions]
        if terms:
            return done(
                SUITABLE_OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS,
                f"{terms[0].url}: {', '.join(terms[0].purchase_exclusions)}",
            )
        states = sorted({o.availability or "UNKNOWN" for o in suitable})
        return done(
            SUITABLE_OFFER_EXCLUDED_BY_STOCK, f"{suitable[0].url}: availability {', '.join(states)}"
        )
    unresolved = [
        o
        for o in evidence.offers
        if o.outcome not in _SUITABLE_PAGE and o.outcome not in _RESOLVED_OTHER
    ]
    if unresolved:
        return done(
            PAGE_FETCHED_IDENTITY_UNRESOLVED,
            "; ".join(dict.fromkeys(f"{o.outcome}" for o in unresolved)),
        )
    relevant = [c for c in evidence.candidates if c.relevant]
    if any(c.request_status in _REQUEST_NO_OFFERS for c in relevant):
        return done(
            PAGE_FETCHED_IDENTITY_UNRESOLVED,
            "the page was fetched but no offer could be read from it",
        )
    failed = [
        c
        for c in relevant
        if c.decision in _FETCH_FAILED_DECISIONS or c.request_status in _REQUEST_FAILED
    ]
    deferred = [
        c
        for c in relevant
        if c.decision in _FETCH_DEFERRED_DECISIONS or c.request_status in _REQUEST_DEFERRED
    ]
    if failed:
        return done(FETCH_FAILED, f"{len(failed)} candidate page(s) could not be fetched")
    if deferred:
        return done(
            FETCH_DEFERRED,
            f"{len(deferred)} candidate page(s) not fetched yet (budget, rate limit or replay)",
        )
    not_collected = [c for c in relevant if c.decision in _NOT_COLLECTED_DECISIONS]
    if not_collected or evidence.legacy_outcome == PLAUSIBLE_CANDIDATE_NOT_COLLECTED:
        return done(
            PLAUSIBLE_CANDIDATE_NOT_COLLECTED,
            _counts(c.decision for c in not_collected)
            or "stopped at a plausible candidate that was never collected",
        )
    if evidence.manifest_rows and not evidence.collected:
        return done(
            NOT_EVALUATED_SEARCH_ONLY,
            f"{evidence.manifest_rows} candidate page(s) "
            "selected for collection; no collection ran",
        )
    outcomes = set(evidence.search_outcomes)
    if evidence.legacy_outcome in {
        SEARCH_INCOMPLETE_REPLAY_ONLY,
        BUDGET_EXHAUSTED,
        PROVIDER_FAILURE,
    }:
        return done(evidence.legacy_outcome, evidence.stop_reason)
    if evidence.capped:
        return done(
            SEARCH_INCOMPLETE_RESULT_CAP,
            "an answered search returned its maximum "
            "result count: more matching products may exist (never an exhaustive search)",
        )
    if evidence.pages_unread:
        return done(
            SEARCH_INCOMPLETE_PAGES_UNREAD, "result pages beyond --max-search-pages were not read"
        )
    if outcomes and not outcomes & _ANSWERED and outcomes <= _NOT_READ:
        return done(PROVIDER_FAILURE, "no search request was answered")
    if any(o.startswith("NOT_SENT") for o in outcomes):
        return done(BUDGET_EXHAUSTED, "some planned searches were not sent")
    if evidence.candidates:
        return done(RESULTS_ALL_REJECTED, rejected or "no candidate was relevant")
    if outcomes & _ANSWERED:
        return done(NO_RESULTS_RETURNED, "every attempted query was answered with no product")
    return done(NOT_EVALUATED_SEARCH_ONLY, "no search request was answered or planned to run")


def outcome_summary(rows: Iterable[dict[str, str]]) -> dict[str, dict[str, int]]:
    """Counts by terminal outcome and by outcome class for ``search_jobs.csv`` rows (or the
    ``job_outcomes`` list of ``discovery_summary.json``): what a diagnostics script groups by."""
    rows = list(rows)
    return {
        "by_terminal_outcome": dict(sorted(Counter(r["terminal_outcome"] for r in rows).items())),
        "by_outcome_class": dict(sorted(Counter(r["outcome_class"] for r in rows).items())),
    }
