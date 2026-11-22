"""Which retailers a weekly run actually checked, from the discovery run's own summary.

A retailer that cannot be reached is an availability problem, not a reason to withhold the report:
the published comparison simply has fewer verified prices. This module turns the discovery summary
into the facts the report must state so that reduced coverage is never read as a complete
comparison: how many retailers were checked, and which were not.

Pure and stdlib-only: the readiness gate (diagnostics) and the Sheets publisher (the notice under
the ``PRICE COMPARISON`` title) use the same function over the same file, so they cannot disagree.

Per retailer, from ``search_outcomes`` (every search record of every job, ``retailer:OUTCOME``) and
``job_outcomes`` (one terminal outcome per target and retailer):

* ``UNAVAILABLE``: searches were attempted or stopped and none was answered;
* ``INCOMPLETE``: its searching stopped after repeated errors, or more than
  ``INCOMPLETE_SHARE_PCT`` percent of its jobs ended unverified (a failed page, a provider failure,
  a deferred fetch) or of its planned searches were never executed;
* ``CHECKED``: otherwise. A few isolated failures leave a retailer ``CHECKED``; their products show
  ``Not checked`` or ``Review`` in the report, never a price and never ``No match``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

CHECKED, INCOMPLETE, UNAVAILABLE = "CHECKED", "INCOMPLETE", "UNAVAILABLE"
# A retailer is incomplete when more than this share of its jobs or planned searches failed.
INCOMPLETE_SHARE_PCT = 10

# Visible column order of PRICE COMPARISON.
ORDER = ("datablitz", "pcx", "laptop_factory", "pcc", "oct")
NAMES = {
    "datablitz": "DataBlitz",
    "pcx": "PC Express",
    "laptop_factory": "Laptop Factory",
    "pcc": "PC Central",
    "oct": "Octagon",
}

_ANSWERED = ("RESULTS", "NO_RESULTS")
_FAILED = ("SEARCH_ERROR", "UNSUPPORTED_LAYOUT")
_NOT_EXECUTED = ("NOT_SENT_BUDGET_EXHAUSTED", "NOT_SENT_SEARCH_LIMIT", "NOT_SENT_REPLAY_ONLY")
_STOPPED = "NOT_SENT_RETAILER_FAILING"
# Jobs whose price the report could not verify because a request failed or was deferred.
_UNVERIFIED_TERMINAL = frozenset({"PROVIDER_FAILURE", "FETCH_FAILED", "FETCH_DEFERRED"})
_PRICED_TERMINAL = frozenset(
    {
        "SUITABLE_OFFER_FOUND",
        "SUITABLE_OFFER_EXCLUDED_BY_STOCK",
        "SUITABLE_OFFER_EXCLUDED_BY_PURCHASE_CONDITIONS",
    }
)

NOTE = (
    "Lowest price and competitiveness use only the verified in-stock prices of the retailers that "
    "were checked. A retailer that was not checked is never assumed to be cheaper, dearer or out "
    "of stock; its cells say Not checked or Review."
)


@dataclass(frozen=True)
class RetailerStatus:
    retailer_id: str
    name: str
    status: str
    searches_answered: int
    search_failures: int
    searches_stopped: int  # planned searches never sent because the retailer was failing
    searches_not_executed: int  # never sent for any failure or budget reason
    searches_planned: int
    jobs: int
    unverified_jobs: int


@dataclass(frozen=True)
class Coverage:
    retailers: tuple[RetailerStatus, ...]

    @property
    def total(self) -> int:
        return len(self.retailers)

    @property
    def checked(self) -> int:
        return sum(r.status == CHECKED for r in self.retailers)

    @property
    def not_checked(self) -> tuple[RetailerStatus, ...]:
        return tuple(r for r in self.retailers if r.status != CHECKED)

    @property
    def complete(self) -> bool:
        return not self.not_checked

    def notice(self) -> str:
        """The line under the report title, or ``''`` when every retailer was checked."""
        if self.complete or not self.total:
            return ""
        head = f"Coverage: {self.checked}/{self.total} retailers checked"
        if not self.checked:
            return f"{head} | Retailer comparisons could not be verified this period"
        if len(self.not_checked) > 2:
            return f"{head} | Partial comparison"
        names = ", ".join(r.name for r in self.not_checked)
        return f"{head} | {names} incomplete"

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "total": self.total,
            "complete": self.complete,
            "notice": self.notice(),
            "retailers": {r.retailer_id: asdict(r) for r in self.retailers},
        }


def _count(mapping: Mapping, retailer_id: str, outcomes: tuple[str, ...]) -> int:
    total = 0
    for outcome in outcomes:
        value = mapping.get(f"{retailer_id}:{outcome}", 0)
        total += value if isinstance(value, int) and not isinstance(value, bool) else 0
    return total


def _over(part: int, whole: int) -> bool:
    return whole > 0 and part * 100 > INCOMPLETE_SHARE_PCT * whole


def from_summary(summary: Mapping) -> Coverage | None:
    """Coverage of one discovery run, or ``None`` when its summary records no search outcomes
    (nothing can be said about the retailers then, and nothing is invented)."""
    outcomes = summary.get("search_outcomes")
    if not isinstance(outcomes, Mapping):
        return None
    scope = (summary.get("scope") or {}).get("retailers")
    seen = {str(key).partition(":")[0] for key in outcomes}
    ids = [str(r) for r in scope] if isinstance(scope, list) and scope else sorted(seen)
    ids.sort(key=lambda r: (ORDER.index(r) if r in ORDER else len(ORDER), r))
    jobs = summary.get("job_outcomes")
    jobs = [j for j in jobs if isinstance(j, Mapping)] if isinstance(jobs, list) else []
    result = []
    for retailer_id in ids:
        answered = _count(outcomes, retailer_id, _ANSWERED)
        failed = _count(outcomes, retailer_id, _FAILED)
        stopped = _count(outcomes, retailer_id, (_STOPPED,))
        unexecuted = stopped + _count(outcomes, retailer_id, _NOT_EXECUTED)
        planned = answered + failed + unexecuted
        mine = [j for j in jobs if j.get("retailer_id") == retailer_id]
        unverified = sum(
            1
            for j in mine
            if j.get("terminal_outcome") in _UNVERIFIED_TERMINAL
            or (
                "PROVIDER_FAILURE" in str(j.get("search_coverage", ""))
                and j.get("terminal_outcome") not in _PRICED_TERMINAL
            )
        )
        if not planned:
            status = INCOMPLETE  # nothing was recorded for a retailer that was in scope
        elif not answered and (failed or stopped):
            status = UNAVAILABLE
        elif stopped or _over(unverified, len(mine)) or _over(unexecuted, planned):
            status = INCOMPLETE
        else:
            status = CHECKED
        result.append(
            RetailerStatus(
                retailer_id=retailer_id,
                name=NAMES.get(retailer_id, retailer_id),
                status=status,
                searches_answered=answered,
                search_failures=failed,
                searches_stopped=stopped,
                searches_not_executed=unexecuted,
                searches_planned=planned,
                jobs=len(mine),
                unverified_jobs=unverified,
            )
        )
    return Coverage(tuple(result))


def read(discovery_dir: Path) -> Coverage | None:
    """Coverage from ``discovery_summary.json`` of a discovery folder. ``None``: no readable
    summary or no search outcomes in it."""
    try:
        summary = json.loads((Path(discovery_dir) / "discovery_summary.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return from_summary(summary) if isinstance(summary, Mapping) else None
