"""Recheck candidates for blank comparison cells (2026-10-19, second review).

When a retailer price is blank, the relevant pages found for the configuration that could not
become a confirmed comparable offer are listed for a manual recheck, each with a short reason:

- **fetched, not confirmed**: a page collected for the configuration (or a same-model near
  match) whose extraction was incomplete, whose identity conflicts, that is another
  configuration, or that matches another target;
- **fetch failed**: a collection request for the configuration failed;
- **listed, not fetched**: a relevant search result (the configuration's code, part number, a
  supplied or historical page) that a limit, a variant review or the replay left unfetched;
- **family-name leads**: listings under a broad family name with no contrary configuration clue,
  best supported first (the title names the target's own recorded code, then agreeing clues),
  with any unitless number that differs from the target's screen shown as unverified,
  when the catalog model cannot identify one product (lowest priority, at most three).

Accessories, unrelated results and other models are never listed: a search result is not
attached to a configuration merely because its query returned it. A recheck candidate is never a
price: an amount shown is labelled unconfirmed and never fills the comparable-price cell, the
comparable-retailer count, or a minimum. Selection is deterministic: most useful evidence first,
then URL.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

FETCHED_NOT_CONFIRMED = "FETCHED_NOT_CONFIRMED"
FETCH_FAILED = "FETCH_FAILED"
LISTED_NOT_FETCHED = "LISTED_NOT_FETCHED"
FAMILY_NAME_LEAD = "FAMILY_NAME_LEAD"
STAGE_TEXT = {
    FETCHED_NOT_CONFIRMED: "page fetched; not a confirmed match",
    FETCH_FAILED: "fetch failed",
    LISTED_NOT_FETCHED: "search result; not fetched",
    FAMILY_NAME_LEAD: "listing under the family name; not fetched",
}
MAX_FAMILY_LEADS = 3

RECHECK_COLUMNS: tuple[str, ...] = (
    "report_week_start", "rank", "target_id", "retailer_code", "retailer_id", "recheck_rank",
    "stage", "status", "explanation", "url", "title", "stock", "observed_at_local",
    "unconfirmed_price", "observation_id", "cell_price_status",
)

_FIELD_WORDS = {
    "MODEL": "model", "SCREEN_SIZE_IN": "screen size", "CPU_SERIES": "CPU",
    "RAM_CAPACITY_GB": "RAM", "STORAGE_CAPACITY_GB": "storage",
}
_FIELD_CODE = re.compile(
    r"(MISSING|NOT_EXTRACTED|UNPARSEABLE|AMBIGUOUS|UNRESOLVED)_(MODEL|SCREEN_SIZE_IN|CPU_SERIES|"
    r"RAM_CAPACITY_GB|STORAGE_CAPACITY_GB)"
)
_STATE_WORDS = {"MISSING": "not established", "NOT_EXTRACTED": "not established",
                "UNPARSEABLE": "unreadable", "AMBIGUOUS": "ambiguous", "UNRESOLVED": "unresolved"}
_UNITS = {"ram_capacity_gb": ("GB", "RAM"), "storage_capacity_gb": ("GB", "storage"),
          "screen_size_in": ('"', "screen"), "cpu_series": ("", "CPU"), "model": ("", "model")}
_MODEL_CONFLICTS = frozenset({"MODEL_CLAIM_CONFLICT", "MODEL_TITLE_CONFLICT",
                              "MODEL_URL_CONFLICT", "MODEL_IDENTIFIER_CONFLICT"})


@dataclass(frozen=True)
class RecheckCandidate:
    """One page to recheck for one configuration at one retailer (never a confirmed price)."""

    priority: int
    stage: str
    status: str
    explanation: str
    url: str
    title: str = ""
    stock: str = ""
    observed_at_local: str = ""
    unconfirmed_price: str = ""
    observation_id: str = ""

    @property
    def sort_key(self) -> tuple:
        return (self.priority, self.url)


def url_key(url: str) -> tuple[str, str]:
    """``(host+path, variant)``: one product page, its selected variant kept apart."""
    parts = urlsplit(url)
    variant = (parse_qs(parts.query).get("variant") or [""])[0]
    return f"{parts.netloc.lower()}{parts.path}".rstrip("/"), variant


def _amount(field: str, value: str) -> str:
    unit, _ = _UNITS.get(field, ("", field))
    if field == "storage_capacity_gb" and value.isdigit() and int(value) >= 1000 and int(
        value
    ) % 1000 == 0:
        return f"{int(value) // 1000}TB"
    return f"{value}{unit}"


def difference_text(differences: Iterable[tuple[str, str, str]]) -> str:
    """``256GB storage; target requires 512GB`` for each differing core field."""
    parts = []
    for field, offer, target in differences:
        _, word = _UNITS.get(field, ("", field))
        parts.append(f"{_amount(field, offer)} {word}; target requires {_amount(field, target)}")
    return "; ".join(parts)


def offer_explanation(
    status: str,
    reason_codes: Sequence[str],
    differences: Sequence[tuple[str, str, str]],
    assigned_target: str = "",
) -> str:
    """A short reason a fetched page is not this configuration's confirmed match."""
    codes = set(reason_codes)
    if assigned_target:
        return f"Matches another target: {assigned_target}" + (
            f" — {difference_text(differences)}" if differences else "")
    if status in {"SAME_MODEL_DIFFERENT_CONFIGURATION", "DIFFERENT_CONFIGURATION"}:
        return "Different configuration" + (
            f" — {difference_text(differences)}" if differences else "")
    if status == "DIFFERENT_MODEL":
        model = next((offer for field, offer, _ in differences if field == "model"), "")
        return "Different model" + (f" — page states {model}" if model else "")
    if status == "CATALOG_IDENTITY_UNRESOLVED":
        model = next((offer for field, offer, _ in differences if field == "model"), "")
        rest = difference_text(d for d in differences if d[0] != "model")
        return ("Catalog identity unresolved — target model is a family name"
                + (f"; page states {model}" if model else "") + (f"; {rest}" if rest else ""))
    if status == "IDENTITY_CONFLICT":
        if codes & _MODEL_CONFLICTS:
            return "Conflicting model statements"
        title = sorted(c.removeprefix("TITLE_SPEC_CONFLICT_") for c in codes
                       if c.startswith("TITLE_SPEC_CONFLICT_"))
        if title:
            return "Conflicting title and specifications — " + ", ".join(
                _FIELD_WORDS.get(name, name.lower()) for name in title)
        if "SECONDARY_CONFLICT" in codes:
            fields = sorted(c.removeprefix("SECONDARY_CONFLICT_").lower().replace("_", " ")
                            for c in codes if c.startswith("SECONDARY_CONFLICT_"))
            return "Conflicting secondary specification" + (
                f" — {', '.join(fields)}" if fields else "")
        if differences:
            return f"Same model code, conflicting specification — {difference_text(differences)}"
        return "Conflicting identity evidence"
    if status in {"EXTRACTION_INCOMPLETE", "MODEL_UNVERIFIED"}:
        found = []
        for code in sorted(codes):
            match = _FIELD_CODE.fullmatch(code)
            if match:
                found.append(f"{_FIELD_WORDS[match[2]]} {_STATE_WORDS[match[1]]}")
        prefix = "Model not established" if status == "MODEL_UNVERIFIED" else (
            "Extraction incomplete")
        return prefix + (f" — {'; '.join(dict.fromkeys(found))}" if found else "")
    if status == "AMBIGUOUS_TARGET":
        return "Identity ambiguous between catalog targets"
    if status == "LISTED_SPEC_MATCH":
        return ("Listed specs fit, but the offer is associated with a more specific family "
                "target")
    if status == "INACTIVE_TARGET":
        return "Only an inactive catalog target has this identity"
    return status.replace("_", " ").capitalize()


_DECISION_TEXT = {
    "NOT_SELECTED_LIMIT": "Not fetched — candidate limit per retailer reached",
    "NOT_COLLECTED_BUDGET": "Not fetched — collection budget reached",
    "NOT_INSPECTED_BUDGET": "Not fetched — collection budget reached",
    "NOT_SENT_REPLAY_ONLY": "Not fetched — offline replay, no saved page",
    "REVIEW_VARIANTS_UNREADABLE": "Not fetched — variants could not be read",
    "REVIEW_NO_MATCHING_VARIANT": "Not fetched — no listed variant states the target RAM/storage",
    "REVIEW_SEVERAL_MATCHING_VARIANTS": "Not fetched — several variants state the configuration",
    "REVIEW_MULTI_VARIANT_NOT_SUPPORTED":
        "Not fetched — multi-variant page; this retailer's variants need review",
    "REVIEW_DIFFERENT_CONFIGURATION_BY_TITLE": "Not fetched — title states another configuration",
}
_RELEVANT = frozenset({
    "HISTORICAL_VERIFIED_URL", "SUPPLIED_URL", "CODE_MATCH", "CODE_MATCH_WITH_UNEXPLAINED_SUFFIX",
    "VERIFIED_PART_NUMBER_MATCH", "CODE_SPELLING_VARIANT", "CODE_IN_URL_ONLY",
    "PRODUCT_NAME_MATCH",
    # A listing stating a broad-family target's brand and family (2026-10-20).
    "FAMILY_LISTED_SPEC_CANDIDATE",
})
_COLLECTED = frozenset({"COLLECT", "COLLECT_VARIANT", "COLLECT_CHECK_CONFIGURATION",
                        "DUPLICATE_OF_EARLIER_CANDIDATE"})


# A bare number in a title that could be read as a screen size but has no unit ("VIVOBOOK 16",
# "16 WUXGA"). It is never converted to inches; it is reported when it differs from the target.
_UNITLESS_SIZE = re.compile(
    r"(?<![\w.\-/])(1[0-8](?:\.[0-9])?)(?![\w.])"
    r"(?!\s*(?:[\"″”']|-?\s*inch|-?\s*in\b|cores?\b|threads?\b|hours?\b|hrs?\b|%))",
    re.I,
)
_NOT_A_SIZE_BEFORE = re.compile(r"\b(?:windows|win|office|gen|generation|ios|android)\s*$", re.I)
_CLUE_WORDS = {"screen_size_in": "screen", "cpu_series": "CPU", "ram_capacity_gb": "RAM",
               "storage_capacity_gb": "storage"}


def unitless_sizes(title: str) -> list[str]:
    """Bare numbers in screen-size range stated without a unit (never treated as inches)."""
    return list(dict.fromkeys(
        match[1] for match in _UNITLESS_SIZE.finditer(title)
        if not _NOT_A_SIZE_BEFORE.search(title[: match.start()])
    ))


def _same_number(left: str, right: str) -> bool:
    try:
        return float(left) == float(right)
    except ValueError:
        return False


def family_lead(
    row: Mapping[str, str], target: Mapping[str, object] | None
) -> RecheckCandidate:
    """A listing under the family name, ranked and explained by its own evidence.

    Better-supported leads come first: the title names this target's own recorded manufacturer
    code or part number (a catalog field, not a confirmed match), then more agreeing title clues.
    A bare number that could be a screen size but has no unit, and differs from the target's
    screen, is shown as unverified and ranks the lead lower. Nothing here is matching evidence.
    """
    title = row.get("title", "")
    url = row.get("collection_url") or row.get("product_url", "")
    parts = ["Catalog identity unresolved — listing under the family name (not fetched)"]
    priority = 60
    target = target or {}
    own = [
        str(value) for name in ("manufacturer_model_code", "manufacturer_part_number")
        if (value := target.get(name)) and str(value).strip() and str(value) != "nan"
        and re.search(rf"(?<![\w-]){re.escape(str(value).strip())}(?![\w-])", title, re.I)
    ]
    if own:
        priority = 50
        parts.append(f"title names this target's recorded code {', '.join(own)} "
                     "(catalog field; not a confirmed match)")
    agrees = [word for name, word in _CLUE_WORDS.items()
              if row.get(f"clue_{name}", "") == "AGREES"]
    if agrees:
        parts.append("title agrees on " + ", ".join(agrees))
    priority -= len(agrees)
    screen = str(target.get("screen_size_in", "") or "")
    stated_screen = row.get("clue_screen_size_in", "") == "AGREES"
    uncertain = [
        number for number in unitless_sizes(title)
        if screen and not stated_screen and not _same_number(number, screen)
    ]
    if uncertain:
        priority += 5
        parts.append("title has " + ", ".join(f"'{n}'" for n in uncertain) + " without a unit "
                     f"(target screen {screen}\"; not a verified screen size)")
    return RecheckCandidate(priority, FAMILY_NAME_LEAD, "CATALOG_IDENTITY_UNRESOLVED",
                            "; ".join(parts), url, title)


def discovery_rechecks(
    candidates: Sequence[Mapping[str, str]],
    manifest: Sequence[Mapping[str, str]],
    request_report: Mapping[str, Mapping[str, str]],
    targets: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[tuple[str, str], list[RecheckCandidate]]:
    """``(target_id, retailer_id) -> recheck candidates`` from one discovery's records.

    Pages whose collection produced an observation are not listed here (the report lists the
    observation itself, with its outcome); failed requests and relevant unfetched results are.
    ``targets`` (target_id -> catalog row) lets family-name leads be ranked and explained by the
    target's own recorded identifiers and screen (``family_lead``).
    """
    by_url_request = {
        (row.get("retailer_id", ""), row.get("product_url", "")): row.get("request_id", "")
        for row in manifest
    }
    found: dict[tuple[str, str], list[RecheckCandidate]] = {}
    family_leads: dict[tuple[str, str], list[RecheckCandidate]] = {}
    for row in candidates:
        key = (row.get("target_id", ""), row.get("retailer_id", ""))
        category, decision = row.get("category", ""), row.get("decision", "")
        url = row.get("collection_url") or row.get("product_url", "")
        title = row.get("title", "")
        if category == "FAMILY_NAME_LEAD_CATALOG_UNRESOLVED":
            if not row.get("configuration_difference"):  # a stated difference is not a lead
                family_leads.setdefault(key, []).append(
                    family_lead(row, (targets or {}).get(key[0])))
            continue
        if category not in _RELEVANT:
            continue
        if decision in _COLLECTED:
            request = row.get("collection_request_id") or by_url_request.get(
                (row.get("retailer_id", ""), url), "")
            report = request_report.get(request, {})
            state = report.get("request_status", "")
            if state in {"FAILED", "NO_OFFERS"}:
                codes = report.get("failure_codes", "") or state
                found.setdefault(key, []).append(RecheckCandidate(
                    30, FETCH_FAILED, "FETCH_FAILED", f"Fetch failed — {codes}",
                    report.get("product_url", url), title))
            elif state == "NOT_SENT_REPLAY_ONLY":
                found.setdefault(key, []).append(RecheckCandidate(
                    40, LISTED_NOT_FETCHED, "CANDIDATE_NOT_FETCHED",
                    _DECISION_TEXT["NOT_SENT_REPLAY_ONLY"], url, title))
            continue  # Fetched: the observation itself is listed with its outcome.
        explanation = _DECISION_TEXT.get(decision)
        if decision == "REVIEW_INSPECTION_FAILED":
            found.setdefault(key, []).append(RecheckCandidate(
                30, FETCH_FAILED, "FETCH_FAILED",
                f"Fetch failed — {row.get('decision_reason', '') or decision}", url, title))
            continue
        if explanation is None:
            continue
        if decision == "REVIEW_DIFFERENT_CONFIGURATION_BY_TITLE" and row.get(
            "configuration_difference"
        ):
            explanation += f" ({row['configuration_difference']})"
        found.setdefault(key, []).append(RecheckCandidate(
            45 if decision.startswith("REVIEW_") else 40, LISTED_NOT_FETCHED,
            "CANDIDATE_NOT_FETCHED", explanation, url, title))
    for key, leads in family_leads.items():
        best = sorted(merge_rechecks(leads), key=lambda c: c.sort_key)[:MAX_FAMILY_LEADS]
        found.setdefault(key, []).extend(best)
    return found


def merge_rechecks(*groups: Iterable[RecheckCandidate]) -> list[RecheckCandidate]:
    """One entry per page (variant kept apart), the most informative first."""
    best: dict[tuple[str, str], RecheckCandidate] = {}
    for group in groups:
        for candidate in group:
            key = url_key(candidate.url)
            if key not in best or candidate.sort_key < best[key].sort_key:
                best[key] = candidate
    return sorted(best.values(), key=lambda c: c.sort_key)


def recheck_note(candidate: RecheckCandidate) -> str:
    """The explanation with what is actually known: stock, observation time, unconfirmed
    price (always labelled unconfirmed)."""
    parts = [candidate.explanation]
    if candidate.stock:
        parts.append(candidate.stock.replace("_", " ").lower())
    if candidate.observed_at_local:
        parts.append(f"observed {candidate.observed_at_local}")
    if candidate.unconfirmed_price:
        parts.append(f"unconfirmed price PHP {candidate.unconfirmed_price}")
    return "; ".join(parts)
