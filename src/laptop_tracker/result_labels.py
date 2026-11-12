"""Short, truthful display labels for the Google Sheets dashboard (2026-11-05).

The comparison tabs show one short *result label* per retailer cell (``MATCH``, ``ALT 2 · CPU,
Storage · Limited``, ``NO RELEVANT · Search limited``, ...) and keep the full machine status and
explanation elsewhere (cell notes and the collapsed detail columns). This module is the one rule
that turns a report row's structured fields into that label. It is pure: it reads fields the
reports already carry, never truncates a sentence, and never changes a business outcome (the price
eligibility, tag, stock rule and minima all come from the reports; a label only names them).

Principles the tests pin down:

- ``NO RESULTS`` and ``NO RELEVANT`` describe what the searches returned; they never say the
  retailer does not sell the laptop, and finishing the query plan is not exhaustive coverage.
- Material qualifiers stay on the label: stock (``Out of stock``), listed-spec basis, a limited
  comparison basis (``Limited``) and an incomplete or capped search (``Search limited``).
- Unknown fields are never agreements: they are listed in details and notes, not turned into
  differences or matches.
- An excluded or unresolved result is never a comparable price (its price cell stays blank).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .recheck import _DECISION_TEXT

# Fill keys of a result (``STATUS_FILLS`` in ``sheet_publication``): green MATCH, yellow ALT 1,
# orange ALT 2, violet review/conflict, neutral for results without a price.
FILL_MATCH, FILL_ALT1, FILL_ALT2, FILL_REVIEW, FILL_MISSING = (
    "match",
    "alt1",
    "alt2",
    "review",
    "missing",
)

# The kind of result, for counting and styling (never for deciding a price).
KIND_MATCH, KIND_ALT, KIND_REVIEW, KIND_EXCLUDED, KIND_PENDING, KIND_NONE = (
    "MATCH",
    "ALT",
    "REVIEW",
    "EXCLUDED",
    "PENDING",
    "NONE",
)

ONE_LINE = 30  # characters a label may take before a stock qualifier moves to a second line

STOCK_QUALIFIER = {
    "OUT_OF_STOCK": "Out of stock",
    "PREORDER": "Preorder",
    "UNKNOWN": "Stock unknown",
}

# Reasons a matched offer is not a comparable price, as a reader would say them.
EXCLUSION_LABEL = {
    "BUNDLE_OFFER": "Bundle terms",
    "BUNDLE_STATUS_UNKNOWN": "Bundle terms",
    "INCLUDED_ITEMS_OFFER": "Bundle terms",
    "PAID_PACKAGE_PRICE": "Bundle terms",
    "PURCHASE_CONDITION_REQUIRED": "Bundle terms",
    "PURCHASE_TERMS_UNKNOWN": "Bundle terms",
    "RESTRICTED_PRICE": "Restricted price",
    "AVAILABILITY_OUT_OF_STOCK": "Out of stock",
    "AVAILABILITY_PREORDER": "Preorder",
    "AVAILABILITY_UNKNOWN": "Stock unknown",
    "CURRENCY_NOT_PHP": "Not PHP",
    "PRICE_TYPE_UNKNOWN": "Price type unknown",
    "PRICE_INVALID": "Price invalid",
}

# Fetched-but-unconfirmed candidates (``recheck.offer_explanation`` statuses).
RECHECK_STATUS_LABEL = {
    "EXTRACTION_INCOMPLETE": "Specs incomplete",
    "MODEL_UNVERIFIED": "Specs incomplete",
    "IDENTITY_CONFLICT": "Spec conflict",
    "AMBIGUOUS_TARGET": "Target ambiguous",
    "DIFFERENT_CONFIGURATION": "Different config",
    "SAME_MODEL_DIFFERENT_CONFIGURATION": "Different config",
    "DIFFERENT_MODEL": "Different model",
    "CATALOG_IDENTITY_UNRESOLVED": "Identity unresolved",
    "LISTED_SPEC_MATCH": "Family target",
    "INACTIVE_TARGET": "Inactive target",
}

# Candidates found but not collected: the discovery decision that kept them apart.
_NOT_COLLECTED_LABEL = {
    "REVIEW_SEVERAL_MATCHING_VARIANTS": ("RECHECK", "Variant ambiguous"),
    "REVIEW_NO_MATCHING_VARIANT": ("RECHECK", "No matching variant"),
    "REVIEW_VARIANTS_UNREADABLE": ("RECHECK", "Variants unreadable"),
    "REVIEW_MULTI_VARIANT_NOT_SUPPORTED": ("RECHECK", "Variants unreadable"),
    "REVIEW_DIFFERENT_CONFIGURATION_BY_TITLE": ("RECHECK", "Different config"),
}
_DECISION_BY_TEXT = {text: code for code, text in _DECISION_TEXT.items()}

_RATE_LIMITED = re.compile(r"RATE_LIMITED|RETAILER_DEFERRED|DEFERRED_RATE_LIMITED|\bHTTP 429\b")
_NOT_EXHAUSTIVE = ("result cap", "unread next pages", "not exhaustively searched")
_INCOMPLETE_ENDINGS = frozenset(
    {
        "BUDGET_EXHAUSTED",
        "PROVIDER_FAILURE",
        "SEARCH_INCOMPLETE_REPLAY_ONLY",
        "SEARCH_INCOMPLETE_RESULT_CAP",
        "SEARCH_INCOMPLETE_PAGES_UNREAD",
    }
)

NO_RESULT_CAVEAT = (
    "This describes what the searches returned. It does not mean the retailer does not sell this "
    "laptop, and finishing the query plan does not establish exhaustive catalog coverage."
)

TAG_MEANING = {
    "MATCH": "Matching configuration with an eligible price.",
    "ALT 1": "Close alternative: one established difference. Not a matching configuration.",
    "ALT 2": "Close alternative: two established differences. Not a matching configuration.",
    "RECHECK": "Unresolved evidence. An unconfirmed candidate is not a comparable price.",
    "EXCLUDED": "A matching offer exists but its price is excluded by purchase terms or stock "
    "rules, so no comparable price is shown.",
    "PENDING": "A candidate or search is waiting to be collected. No price yet.",
    "DEFERRED": "Collection was blocked by retailer throttling. No price yet.",
    "NO RESULTS": "The searches returned no products. " + NO_RESULT_CAVEAT,
    "NO RELEVANT": "Products were returned but none was relevant to this laptop. "
    + NO_RESULT_CAVEAT,
    "INCOMPLETE": "The search stopped before the query plan finished, so the retailer's "
    "catalog was not fully searched. No price. " + NO_RESULT_CAVEAT,
    "SEARCH FAILED": "A search request failed. No price. " + NO_RESULT_CAVEAT,
    "NOT OBSERVED": "No observation of this retailer fell in the reporting week. No price.",
    "NO DATA": "No search or collection record exists for this retailer. No price.",
    "NOT COMPARED": "The row has no trusted comparison context, so no retailer is compared.",
}


@dataclass(frozen=True)
class RetailerResult:
    """One retailer cell's display label and the structured facts behind it."""

    label: str  # up to two lines, joined with a newline
    head: str  # the leading tag: MATCH, ALT 1 · CPU, RECHECK, NO RESULTS, ...
    kind: str  # KIND_*
    fill: str  # FILL_*
    priced: bool
    qualifiers: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()  # exclusion or recheck reasons in words

    @property
    def one_line(self) -> str:
        return self.label.replace("\n", " · ")

    @property
    def tag(self) -> str:
        """The leading tag word(s) without the dimensions (``ALT 2 · CPU`` -> ``ALT 2``)."""
        return self.head.split(" · ")[0]


def search_limited(reason: str) -> bool:
    """True when the recorded search was capped, stopped early or left result pages unread."""
    if any(phrase in reason for phrase in _NOT_EXHAUSTIVE):
        return True
    ended = re.search(r"discovery ended: ([A-Z_]+)", reason)
    if ended and ended.group(1) in _INCOMPLETE_ENDINGS:
        return True
    coverage = re.search(r"search coverage ([A-Z_]+)", reason)
    return bool(coverage and coverage.group(1) != "COMPLETE")


# The one coverage token that does not make a finished search uncertain: the optional category
# fallback was switched off by configuration, while the query plan itself ran to its end.
_FALLBACK_OFF = "search coverage FALLBACK_NOT_TRIED_DISABLED"


def search_unsettled(reason: str) -> bool:
    """True when the recorded search cannot show that no matching configuration exists: a result
    page hit the retailer's cap or has unread pages, the search stopped early (budget, provider
    failure, replay only), or the fallback was not tried for a reason other than being switched
    off. Reads the same recorded wording as ``search_limited``; it changes no business outcome."""
    return search_limited(reason.replace(_FALLBACK_OFF, ""))


def rate_limited(*texts: str) -> bool:
    return any(_RATE_LIMITED.search(text) for text in texts if text)


def compose(head: str, basis: Sequence[str] = (), stock: Sequence[str] = ()) -> str:
    """``head · basis · stock`` on one line when short, else the stock qualifier on a second."""
    one = " · ".join([head, *basis, *stock])
    if len(one) <= ONE_LINE or not stock:
        return one
    return " · ".join([head, *basis]) + "\n" + " · ".join(stock)


def _stock_qualifiers(stock: str) -> tuple[str, ...]:
    return (STOCK_QUALIFIER[stock],) if stock in STOCK_QUALIFIER else ()


def _exclusion_reasons(codes: str) -> tuple[str, ...]:
    labels = []
    for item in codes.split(";"):
        code = item.split(":")[0].strip()
        if code:
            labels.append(EXCLUSION_LABEL.get(code, code.replace("_", " ").capitalize()))
    return tuple(dict.fromkeys(labels))


def comparison_tag(row: Mapping[str, str], code: str) -> str:
    """The retailer cell's comparison tag; a priced cell saved before the tags is a MATCH."""
    tag = row.get(f"{code}_Comparison_Tag", "")
    if not tag and row.get(f"{code}_Price"):
        return "MATCH"
    return tag


def _result(
    head: str,
    kind: str,
    fill: str,
    *,
    priced: bool = False,
    basis: Sequence[str] = (),
    stock: Sequence[str] = (),
    reasons: Sequence[str] = (),
) -> RetailerResult:
    return RetailerResult(
        compose(head, basis, stock),
        head,
        kind,
        fill,
        priced,
        (*basis, *stock),
        tuple(reasons),
    )


def _priced(
    row: Mapping[str, str], code: str, tag: str, alt: Mapping[str, str] | None
) -> RetailerResult:
    stock = _stock_qualifiers(row.get(f"{code}_Stock", ""))
    if tag.startswith("ALT"):
        # A limited comparison basis (model family only, CPU series only) is a structured field of
        # the alternative; the unknown-fields text carries the same words for a report without it.
        limited = bool(
            (alt or {}).get("comparison_basis")
            or "limited basis" in row.get(f"{code}_Alternative_Unknowns", "")
        )
        fill = FILL_ALT1 if tag.startswith("ALT 1") else FILL_ALT2
        return _result(
            tag,
            KIND_ALT,
            fill,
            priced=True,
            basis=("Limited",) if limited else (),
            stock=stock,
        )
    listed = row.get(f"{code}_Match_Basis", "") == "LISTED_SPEC"
    return _result(
        "MATCH",
        KIND_MATCH,
        FILL_MATCH,
        priced=True,
        basis=("Listed specs",) if listed else (),
        stock=stock,
    )


def _recheck_result(row: Mapping[str, str], code: str, status: str) -> RetailerResult:
    """The label of an unconfirmed candidate: why it needs a recheck or is still pending."""
    candidate = row.get(f"{code}_Candidate_Status", "") or status
    note = row.get(f"{code}_Recheck_Note", "") or row.get(f"{code}_Candidate_Detail", "")
    reason = row.get(f"{code}_Reason", "")
    if rate_limited(note, reason):
        return _result("DEFERRED", KIND_PENDING, FILL_MISSING, basis=("Rate limited",))
    if candidate == "FETCH_FAILED" or status == "FETCH_FAILED":
        return _result("PENDING", KIND_PENDING, FILL_MISSING, basis=("Fetch failed",))
    if candidate == "CANDIDATE_NOT_FETCHED" or status == "CANDIDATE_NOT_FETCHED":
        for text, decision in _DECISION_BY_TEXT.items():
            if text in note and decision in _NOT_COLLECTED_LABEL:
                head, why = _NOT_COLLECTED_LABEL[decision]
                return _result(head, KIND_REVIEW, FILL_REVIEW, basis=(why,), reasons=(note,))
        return _result("PENDING", KIND_PENDING, FILL_MISSING, basis=("Not fetched",))
    why = RECHECK_STATUS_LABEL.get(candidate)
    if why is None:
        why = candidate.replace("_", " ").capitalize() if candidate else "Unconfirmed"
    return _result("RECHECK", KIND_REVIEW, FILL_REVIEW, basis=(why,), reasons=(note,))


def _unpriced(
    row: Mapping[str, str], code: str, status: str, selection: Mapping[str, str] | None
) -> RetailerResult:
    reason = row.get(f"{code}_Reason", "")
    limited = ("Search limited",) if search_limited(reason) else ()
    if status == "NO_COMPARABLE_OFFER":
        reasons = _exclusion_reasons((selection or {}).get("not_comparable_reasons", ""))
        shown = ", ".join(reasons[:2]) + (f" +{len(reasons) - 2}" if len(reasons) > 2 else "")
        return _result(
            "EXCLUDED",
            KIND_EXCLUDED,
            FILL_REVIEW,
            basis=(shown or "Price rules",),
            reasons=reasons,
        )
    if status == "REVIEW_REQUIRED_ONLY":
        return _result("RECHECK", KIND_REVIEW, FILL_REVIEW, basis=("Match review",))
    if status in {"CANDIDATE_NOT_CONFIRMED", "CANDIDATE_NOT_FETCHED", "FETCH_FAILED"}:
        return _recheck_result(row, code, status)
    if status == "CATALOG_IDENTITY_UNRESOLVED":
        return _result("RECHECK", KIND_REVIEW, FILL_REVIEW, basis=("Identity unresolved",))
    if status == "NO_CANDIDATE_FOUND":
        return _result("NO RESULTS", KIND_NONE, FILL_MISSING, basis=limited)
    if status == "NO_RELEVANT_CANDIDATE":
        return _result("NO RELEVANT", KIND_NONE, FILL_MISSING, basis=limited)
    if status == "DISCOVERY_INCOMPLETE":
        return _result("INCOMPLETE", KIND_NONE, FILL_MISSING, basis=("Search limited",))
    if status == "SEARCH_FAILED":
        if rate_limited(reason):
            return _result("DEFERRED", KIND_PENDING, FILL_MISSING, basis=("Rate limited",))
        return _result("SEARCH FAILED", KIND_NONE, FILL_MISSING)
    if status == "DISCOVERY_NOT_ATTEMPTED":
        return _result("PENDING", KIND_PENDING, FILL_MISSING, basis=("Not searched",))
    if status == "NO_OBSERVATION_IN_WEEK":
        return _result("NOT OBSERVED", KIND_NONE, FILL_MISSING, basis=("This week",))
    return _result("NO DATA", KIND_NONE, FILL_MISSING)


def retailer_result(
    row: Mapping[str, str],
    code: str,
    selection: Mapping[str, str] | None = None,
    alt: Mapping[str, str] | None = None,
) -> RetailerResult:
    """The display label of one retailer cell of a comparison row.

    ``selection`` is the retailer's selection record (its ``not_comparable_reasons`` explain an
    excluded matching offer) and ``alt`` the alternative record of the displayed ALT offer (its
    ``comparison_basis`` says whether the comparison basis was limited); both are optional.
    """
    if not row.get("context_id"):
        why = {
            "CONFIGURATION_REVIEW_PENDING": "Config review",
            "SOURCE_CONFLICT_COMPARISON_BLOCKED": "Source conflict",
        }.get(row.get("row_comparison_status", ""), "No context")
        return _result("NOT COMPARED", KIND_REVIEW, FILL_REVIEW, basis=(why,))
    status = row.get(f"{code}_Price_Status", "")
    tag = comparison_tag(row, code)
    if row.get(f"{code}_Price") and status in {"SELECTED", "ALTERNATIVE_SELECTED"}:
        return _priced(row, code, tag, alt)
    return _unpriced(row, code, status, selection)


def coverage_summary(results: Sequence[RetailerResult]) -> str:
    """``2 match · 1 alt · 1 review · 1 none``: a short row summary of the five cells."""
    counts = Counter(r.kind for r in results)
    parts = [
        f"{counts[kind]} {word}"
        for kind, word in (
            (KIND_MATCH, "match"),
            (KIND_ALT, "alt"),
            (KIND_REVIEW, "recheck"),
            (KIND_EXCLUDED, "excluded"),
            (KIND_PENDING, "pending"),
            (KIND_NONE, "no price"),
        )
        if counts[kind]
    ]
    return " · ".join(parts)


# Shopee snapshot price: its source date and freshness (the 7-day rule is the report's).

STALE = "STALE"
_DATE_BASIS_WORDS = {
    "SALES_EXTRACT_DATE": "sales extract",
    "M_MINUS_1_RECORD_DATE": "monthly record",
}


def shopee_freshness(row: Mapping[str, str]) -> tuple[str, bool]:
    """``(text, stale)``: the snapshot's source date, what kind of date it is, and ``STALE``.

    A monthly source record is dated by the record, not by an exact price observation; the text
    says which. The date is the report's (never today's), and ``STALE`` is the report's own
    freshness status (``SNAPSHOT_PRICE_STALE``: older than seven days at the cutoff).
    """
    date = row.get("shopee_price_observed_at", "")
    if not row.get("shopee_price_php") or not date:
        return "", False
    basis = _DATE_BASIS_WORDS.get(row.get("shopee_price_date_basis", ""), "source record")
    stale = row.get("shopee_price_status", "") == "SNAPSHOT_PRICE_STALE"
    first = f"{date} · {basis}"
    return (f"{first}\n{STALE}" if stale else first), stale


def _storage_text(value: str) -> str:
    if value.isdigit() and int(value) >= 1000 and int(value) % 1000 == 0:
        return f"{int(value) // 1000}TB"
    return f"{value}GB" if value else ""


def compact_specs(row: Mapping[str, str]) -> str:
    """``15.6" · Ryzen 5 · 16GB · 512GB · RTX 3050``: the target's specifications on one line.

    Only recorded fields appear (a blank field is left out, never guessed); the full columns are
    in the collapsed specification group.
    """
    screen = row.get("screen_size_in", "")
    parts = [
        f'{screen}"' if screen else "",
        row.get("cpu_model") or row.get("cpu_series", ""),
        f"{row['ram_capacity_gb']}GB" if row.get("ram_capacity_gb") else "",
        _storage_text(row.get("storage_capacity_gb", "")),
        row.get("gpu_model", ""),
    ]
    return " · ".join(part for part in parts if part)
