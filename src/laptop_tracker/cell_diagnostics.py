"""Per-cell diagnostics: why each configuration x retailer cell is priced or blank (2026-10-22).

Offline and read-only. One row per (target, retailer) of a weekly report joins the published
comparison and selections, the pair coverage (``coverage_report.py``), the discovery search jobs,
requests and candidates, and the collection results, and places the cell at the furthest stage
it reached. Existing status codes are kept beside the stage; nothing is re-matched or re-priced.

Two prices are kept apart on purpose: ``observed_listing_price`` is any amount a related page
displayed (also for unconfirmed or excluded offers), ``owner_policy_comparable_price`` is the
price the report selected under the owner-approved policy. Only the latter is ever published.
A blank cell is not evidence that a retailer does not sell a configuration: the stage says how
far discovery got, ``search_coverage`` whether the searches could have been truncated.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .price_tracking import RETAILERS

# Stages, from "nothing happened" to "published". Two existing reason codes are kept as their own
# stages: NO_RELEVANT_CANDIDATE (searches returned results, none relevant) and
# CATALOG_IDENTITY_UNRESOLVED (a broad family-name target whose leads are not fetched).
NOT_SEARCHED = "NOT_SEARCHED"
SEARCH_FAILED = "SEARCH_FAILED"
ZERO_RESULTS = "ZERO_RESULTS_AFTER_BOUNDED_SEARCH"
NO_RELEVANT_CANDIDATE = "NO_RELEVANT_CANDIDATE"
CATALOG_IDENTITY_UNRESOLVED = "CATALOG_IDENTITY_UNRESOLVED"
NOT_FETCHED = "CANDIDATE_DISCOVERED_NOT_FETCHED"
FETCH_ERROR = "PAGE_FETCH_ERROR"
PARSED_NO_PRICE = "PAGE_PARSED_NO_PRICE"
VARIANT_UNRESOLVED = "VARIANT_UNRESOLVED"
MATCH_REVIEW = "MATCH_REVIEW_REQUIRED"
CONFIGURATION_MISMATCH = "CONFIGURATION_MISMATCH"
NOT_COMPARABLE = "PRICE_KNOWN_NOT_COMPARABLE"
PUBLISHED = "COMPARABLE_PUBLISHED"
# The cell shows a close alternative (2026-10-27): a labelled price, not a matching configuration.
ALTERNATIVE_PUBLISHED = "ALTERNATIVE_PUBLISHED"
STAGES: tuple[str, ...] = (
    NOT_SEARCHED, SEARCH_FAILED, ZERO_RESULTS, NO_RELEVANT_CANDIDATE,
    CATALOG_IDENTITY_UNRESOLVED, NOT_FETCHED, FETCH_ERROR, PARSED_NO_PRICE, VARIANT_UNRESOLVED,
    MATCH_REVIEW, CONFIGURATION_MISMATCH, NOT_COMPARABLE, PUBLISHED, ALTERNATIVE_PUBLISHED,
)

# coverage_report.py pair statuses -> stage.
_COVERAGE_STAGE = {
    "PRICE_AVAILABLE": PUBLISHED,
    "LISTED_SPEC_PRICE_AVAILABLE": PUBLISHED,
    "EXACT_PRICE_EXCLUDED": NOT_COMPARABLE,
    "AMBIGUOUS_TARGET": MATCH_REVIEW,
    "IDENTITY_CONFLICT": MATCH_REVIEW,
    "MODEL_UNVERIFIED": MATCH_REVIEW,
    "EXTRACTION_INCOMPLETE": MATCH_REVIEW,
    "SAME_MODEL_DIFFERENT_CONFIGURATION": CONFIGURATION_MISMATCH,
    "DIFFERENT_CONFIGURATION": CONFIGURATION_MISMATCH,
    "DIFFERENT_MODEL": CONFIGURATION_MISMATCH,
    "FETCH_FAILED": FETCH_ERROR,
    "CANDIDATE_NOT_FETCHED": NOT_FETCHED,
    "CATALOG_IDENTITY_UNRESOLVED": CATALOG_IDENTITY_UNRESOLVED,
    "NO_RELEVANT_CANDIDATE": NO_RELEVANT_CANDIDATE,
    "NO_CANDIDATE_FOUND": ZERO_RESULTS,
    "SEARCH_FAILED": SEARCH_FAILED,
    "DISCOVERY_NOT_ATTEMPTED": NOT_SEARCHED,
}
# Weekly-report cell statuses -> stage (used when no coverage row explains the pair).
_REPORT_STAGE = {
    "SELECTED": PUBLISHED,
    "NO_COMPARABLE_OFFER": NOT_COMPARABLE,
    "REVIEW_REQUIRED_ONLY": MATCH_REVIEW,
    "CANDIDATE_NOT_CONFIRMED": MATCH_REVIEW,
    "FETCH_FAILED": FETCH_ERROR,
    "CANDIDATE_NOT_FETCHED": NOT_FETCHED,
    "CATALOG_IDENTITY_UNRESOLVED": CATALOG_IDENTITY_UNRESOLVED,
    "NO_RELEVANT_CANDIDATE": NO_RELEVANT_CANDIDATE,
    "NO_CANDIDATE_FOUND": ZERO_RESULTS,
    "DISCOVERY_INCOMPLETE": NO_RELEVANT_CANDIDATE,
    "SEARCH_FAILED": SEARCH_FAILED,
    "DISCOVERY_NOT_ATTEMPTED": NOT_SEARCHED,
    "NO_OBSERVATION_IN_WEEK": NOT_SEARCHED,
}
VARIANT_DECISIONS = frozenset({
    "REVIEW_NO_MATCHING_VARIANT", "REVIEW_SEVERAL_MATCHING_VARIANTS",
    "REVIEW_VARIANTS_UNREADABLE", "REVIEW_MULTI_VARIANT_NOT_SUPPORTED",
})
# Candidate decisions that left a relevant page unfetched because of a limit or budget.
LIMIT_DECISIONS = frozenset({"NOT_SELECTED_LIMIT", "NOT_COLLECTED_BUDGET", "NOT_INSPECTED_BUDGET"})
FAILED_REQUESTS = frozenset({"FAILED"})

DIAGNOSTIC_COLUMNS: tuple[str, ...] = (
    "rank", "target_id", "retailer_id", "retailer_code", "identity_kind", "diagnostic_stage",
    "report_price_status", "coverage_status", "stage_detail",
    "search_method", "queries", "tiers_run", "search_pages_sent", "search_pages_not_sent",
    "search_hits", "stop_reason", "discovery_outcome", "search_coverage", "budget_flags",
    "candidates_listed", "collectable_candidates", "collectable_not_selected_by_limit",
    "best_candidate_url", "best_candidate_category", "best_candidate_decision",
    "request_outcome", "observation_id", "match_status", "match_reason_codes",
    "observed_listing_price", "observed_availability", "owner_policy_comparable_price",
    "price_selection_reason", "comparable_offer_url", "offer_also_prices_targets",
)
_COLLECTABLE = frozenset({
    "HISTORICAL_VERIFIED_URL", "SUPPLIED_URL", "CODE_MATCH", "CODE_MATCH_WITH_UNEXPLAINED_SUFFIX",
    "VERIFIED_PART_NUMBER_MATCH", "CODE_SPELLING_VARIANT", "CODE_IN_URL_ONLY",
    "PRODUCT_NAME_MATCH", "FAMILY_LISTED_SPEC_CANDIDATE",
})


def _key(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"


def _group(rows: Iterable[Mapping[str, str]], *names: str) -> dict[tuple, list[Mapping]]:
    grouped: dict[tuple, list[Mapping]] = {}
    for row in rows:
        grouped.setdefault(tuple(row.get(n, "") for n in names), []).append(row)
    return grouped


def diagnose_cells(
    comparison: Sequence[Mapping[str, str]],
    selections: Sequence[Mapping[str, str]],
    *,
    coverage: Sequence[Mapping[str, str]] = (),
    jobs: Sequence[Mapping[str, str]] = (),
    requests: Sequence[Mapping[str, str]] = (),
    candidates: Sequence[Mapping[str, str]] = (),
    results: Sequence[Mapping[str, str]] = (),
    request_report: Sequence[Mapping[str, str]] = (),
    search_methods: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """One diagnostic row per (target, retailer) of ``comparison``, in rank and retailer order."""
    selection = {(r["target_id"], r["retailer_code"]): r for r in selections}
    pairs = {(r["target_id"], r["retailer_id"]): r for r in coverage}
    job = {(r["target_id"], r["retailer_id"]): r for r in jobs}
    sent = _group(requests, "target_id", "retailer_id")
    listed = _group(candidates, "target_id", "retailer_id")
    by_request = _group(results, "request_id")
    status_by_request = {r["request_id"]: r.get("request_status", "") for r in request_report}
    priced: dict[tuple[str, str], set[str]] = {}
    for row in selections:
        if row.get("price_status") == "SELECTED" and row.get("offer_url"):
            priced.setdefault((row["retailer_code"], row["offer_url"]), set()).add(
                row["target_id"])
    rows: list[dict[str, str]] = []
    for item in sorted(comparison, key=lambda r: int(r["rank"])):
        target = item["target_id"]
        for code, retailer in RETAILERS:
            chosen = selection.get((target, code), {})
            pair = pairs.get((target, retailer), {})
            plan = job.get((target, retailer), {})
            records = sent.get((target, retailer), [])
            mine = listed.get((target, retailer), [])
            report_status = item.get(f"{code}_Price_Status", "")
            stage = _COVERAGE_STAGE.get(pair.get("coverage_status", "")) or _REPORT_STAGE.get(
                report_status, NOT_SEARCHED)
            if report_status == "SELECTED":
                stage = PUBLISHED
            elif report_status == "ALTERNATIVE_SELECTED":
                stage = ALTERNATIVE_PUBLISHED
            elif report_status == "NO_COMPARABLE_OFFER":
                stage = NOT_COMPARABLE
            sent_records = [r for r in records if not r.get("outcome", "").startswith("NOT_SENT")]
            hits = sum(int(r.get("hits") or 0) for r in sent_records)
            if stage == NO_RELEVANT_CANDIDATE and sent_records and hits == 0:
                stage = ZERO_RESULTS
            if stage == ZERO_RESULTS and hits:
                stage = NO_RELEVANT_CANDIDATE
            if stage in {NO_RELEVANT_CANDIDATE, ZERO_RESULTS} and not sent_records and records:
                stage = NOT_SEARCHED  # planned searches were never sent (limits)
            variant = [c for c in mine if c.get("decision") in VARIANT_DECISIONS]
            if stage in {NOT_FETCHED, MATCH_REVIEW, NO_RELEVANT_CANDIDATE} and variant and not any(
                    c.get("decision", "").startswith("COLLECT") for c in mine):
                stage = VARIANT_UNRESOLVED
            collected = [row for c in mine for row in by_request.get(
                c.get("collection_request_id", ""), [])]
            relevant = [r for r in collected if r.get("target_id") in {"", target}
                        or target in r.get("candidate_target_ids", "").split(";")]
            observation = next((r for r in relevant if r.get("observation_id") ==
                                chosen.get("observation_id")), None) or (
                relevant[0] if relevant else {})
            if stage == MATCH_REVIEW and observation and not observation.get("price"):
                stage = PARSED_NO_PRICE
            request_ids = [c.get("collection_request_id", "") for c in mine
                           if c.get("collection_request_id")]
            outcomes = [status_by_request.get(i, "") for i in dict.fromkeys(request_ids)]
            if stage in {NOT_FETCHED, MATCH_REVIEW} and outcomes and all(
                    o in FAILED_REQUESTS for o in outcomes if o) and any(outcomes):
                stage = FETCH_ERROR
            best = next((c for c in sorted(mine, key=lambda c: int(c.get("candidate_rank") or 0))
                         if c.get("category") in _COLLECTABLE), mine[0] if mine else {})
            collectable = [c for c in mine if c.get("category") in _COLLECTABLE]
            limited = [c for c in collectable if c.get("decision") in LIMIT_DECISIONS]
            coverage_flags = plan.get("search_coverage", "")
            budget = sorted({flag for flag in (
                *coverage_flags.split(";"),
                "QUERY_LIMIT" if any(r.get("outcome") == "NOT_SENT_QUERY_LIMIT"
                                     for r in records) else "",
                "REQUEST_BUDGET" if any(r.get("outcome") in {"NOT_SENT_BUDGET_EXHAUSTED",
                                                             "NOT_SENT_SEARCH_LIMIT"}
                                        for r in records) else "",
                "CANDIDATE_LIMIT" if limited else "",
            ) if flag and flag != "COMPLETE"})
            observed = pair.get("unconfirmed_candidate_price") or observation.get("price", "")
            rows.append({
                "rank": item["rank"], "target_id": target, "retailer_id": retailer,
                "retailer_code": code, "identity_kind": plan.get("identity_kind", ""),
                "diagnostic_stage": stage, "report_price_status": report_status,
                "coverage_status": pair.get("coverage_status", ""),
                "stage_detail": (pair.get("current_reason") or chosen.get("candidate_detail")
                                 or item.get(f"{code}_Reason", "")),
                "search_method": (search_methods or {}).get(retailer, ""),
                "queries": " | ".join(
                    f"{r.get('tier', '')}: {r.get('query', '')} -> {r.get('outcome', '')}"
                    f"({r.get('hits', '0')})" for r in records),
                "tiers_run": plan.get("tiers_run", ""),
                "search_pages_sent": str(len(sent_records)),
                "search_pages_not_sent": str(len(records) - len(sent_records)),
                "search_hits": str(hits),
                "stop_reason": plan.get("stop_reason", ""),
                "discovery_outcome": plan.get("discovery_outcome", ""),
                "search_coverage": coverage_flags,
                "budget_flags": ";".join(budget),
                "candidates_listed": str(len(mine)),
                "collectable_candidates": str(len(collectable)),
                "collectable_not_selected_by_limit": str(len(limited)),
                "best_candidate_url": best.get("product_url", "") or pair.get("candidate_url", ""),
                "best_candidate_category": best.get("category", ""),
                "best_candidate_decision": best.get("decision", ""),
                "request_outcome": ";".join(o for o in outcomes if o),
                "observation_id": chosen.get("observation_id") or observation.get(
                    "observation_id", ""),
                "match_status": observation.get("match_status", ""),
                "match_reason_codes": observation.get("reason_codes", ""),
                "observed_listing_price": observed if stage not in {
                    PUBLISHED, ALTERNATIVE_PUBLISHED} else chosen.get("price", ""),
                "observed_availability": (chosen.get("availability")
                                          or observation.get("availability", "")),
                "owner_policy_comparable_price": (chosen.get("price", "")
                                                  if report_status == "SELECTED" else ""),
                "price_selection_reason": (chosen.get("selection_reason", "")
                                           or chosen.get("not_comparable_reasons", "")),
                "comparable_offer_url": (chosen.get("offer_url", "")
                                         if report_status == "SELECTED" else ""),
                "offer_also_prices_targets": ";".join(sorted(
                    priced.get((code, chosen.get("offer_url", "")), set()) - {target}))
                if report_status == "SELECTED" else "",
            })
    return rows


def stage_counts(rows: Sequence[Mapping[str, str]]) -> dict[str, dict[str, int]]:
    """``{retailer_id: {stage: count}}`` plus a ``TOTAL`` row, stages in pipeline order."""
    counts: dict[str, Counter] = {}
    for row in rows:
        counts.setdefault(row["retailer_id"], Counter())[row["diagnostic_stage"]] += 1
        counts.setdefault("TOTAL", Counter())[row["diagnostic_stage"]] += 1
    return {retailer: {stage: counter.get(stage, 0) for stage in STAGES}
            for retailer, counter in counts.items()}


# Audit leads (e.g. the 2026-10-21 manual follow-up). They are claims to check, never truth.

AUDIT_COLUMNS: tuple[str, ...] = (
    "audit_status", "audit_url", "audit_url_kind", "audit_url_in_search_results",
    "audit_url_listed_for_this_target", "audit_url_candidate_decision", "audit_url_collected",
    "audit_url_shows_model_code", "audit_url_shared_with_targets", "audit_price_claim",
    "audit_stock_claim",
)
_RETAILER_IDS = {"DataBlitz": "datablitz", "Laptop Factory": "laptop_factory",
                 "PC Express": "pcx", "PC Central": "pcc", "Octagon": "oct"}


def url_kind(url: str) -> str:
    """PRODUCT_PAGE, CATEGORY_PAGE (a listing, never product-detail verification) or NONE."""
    if not url:
        return "NONE"
    path = urlsplit(url).path
    if "/products/" in path or path.startswith("/product/"):
        return "PRODUCT_PAGE"
    return "CATEGORY_PAGE"


def overlay_audit(
    rows: Sequence[dict[str, str]],
    audit: Sequence[Mapping[str, str]],
    *,
    search_results: Sequence[Mapping[str, str]] = (),
    candidates: Sequence[Mapping[str, str]] = (),
    results: Sequence[Mapping[str, str]] = (),
    models: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Add the audit's claim for each cell and where its URL appears in this run's evidence.

    ``audit`` rows use the follow-up workbook's columns (``Target ID``, ``Retailer``,
    ``Evidence status``, ``Retailer URL``, ``Original recheck URL``, ``Retailer price PHP``,
    ``Stock / offer indicator``). A URL claimed for several targets is reported, not resolved.
    """
    from .product_discovery import url_shows_code

    claims: dict[tuple[str, str], Mapping[str, str]] = {}
    targets_by_url: dict[str, set[str]] = {}
    for claim in audit:
        retailer = _RETAILER_IDS.get(claim.get("Retailer", ""), claim.get("Retailer", ""))
        url = claim.get("Retailer URL") or claim.get("Original recheck URL") or ""
        claims[(claim.get("Target ID", ""), retailer)] = dict(claim) | {"_url": url}
        if url and url_kind(url) == "PRODUCT_PAGE" and claim.get("Retailer URL"):
            targets_by_url.setdefault(_key(url), set()).add(claim.get("Target ID", ""))
    found = _group(({**r, "_key": _key(r.get("product_url", ""))} for r in search_results),
                   "_key")
    listed = _group(({**r, "_key": _key(r.get("product_url", ""))} for r in candidates), "_key")
    fetched = _group(({**r, "_key": _key(r.get("offer_url", ""))} for r in results), "_key")
    out = []
    for row in rows:
        claim = claims.get((row["target_id"], row["retailer_id"]))
        extra = dict.fromkeys(AUDIT_COLUMNS, "")
        if claim:
            url = claim["_url"]
            key = _key(url) if url else ""
            shared = sorted(targets_by_url.get(key, set()) - {row["target_id"]})
            hits = found.get((key,), []) if key else []
            seen = listed.get((key,), []) if key else []
            mine = [c for c in seen if c.get("target_id") == row["target_id"]]
            model = (models or {}).get(row["target_id"], "")
            extra |= {
                "audit_status": claim.get("Evidence status", ""),
                "audit_url": url,
                "audit_url_kind": url_kind(url),
                "audit_url_in_search_results": "TRUE" if hits else "FALSE" if key else "",
                "audit_url_listed_for_this_target": "TRUE" if any(
                    h.get("target_id") == row["target_id"] for h in hits) else "FALSE"
                if key else "",
                "audit_url_candidate_decision": ";".join(
                    f"{c.get('category')}|{c.get('decision')}" for c in mine),
                "audit_url_collected": ";".join(
                    f"{r.get('match_status')}|{r.get('target_id') or '-'}"
                    for r in fetched.get((key,), [])) if key else "",
                "audit_url_shows_model_code": (
                    "TRUE" if model and url and url_shows_code(url, model) else "FALSE"
                    if model and url else ""),
                "audit_url_shared_with_targets": ";".join(shared),
                "audit_price_claim": str(claim.get("Retailer price PHP", "") or ""),
                "audit_stock_claim": str(claim.get("Stock / offer indicator", "") or ""),
            }
        out.append(row | extra)
    return out
