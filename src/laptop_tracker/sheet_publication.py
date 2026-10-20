"""Manual Google Sheets publication of reviewed Phase 10 pilot price reports.

The publisher reads saved, reviewed report files only. It never fetches retailer pages, reruns
matching, imports observations, or opens the observation store. It writes exactly four
publisher-owned tabs (``PUBLISHER_TABS``) and refuses any other destination, including ``raw``,
the catalog tabs, and the legacy ``P1_*`` outputs.

Cells are written with the Sheets ``updateCells`` request. Text is sent as literal string
values, so nothing a retailer or the catalog wrote can become a formula. Prices are numbers
with PHP formatting. Retailer links are full URL text with a cell link, never a formula. Each
publication replaces the whole content of its tabs, so repeating it never duplicates rows, and
success is reported only after every planned cell has been read back and compared.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .price_tracking import (
    CANDIDATE_NOT_CONFIRMED,
    COMPARISON_COLUMNS,
    HISTORY_COLUMNS,
    LEGACY_COMPARISON_COLUMNS,
    LEGACY_SELECTION_COLUMNS,
    NO_COMPARABLE_OFFER,
    NO_OBSERVATION_IN_WEEK,
    REPORTING_CURRENCY,
    REPORTING_TIMEZONE,
    RETAILERS,
    REVIEW_COLUMNS,
    REVIEW_REQUIRED_ONLY,
    SEARCH_STAGE_STATUSES,
    SELECTED,
    SELECTION_COLUMNS,
)
from .recheck import RECHECK_COLUMNS
from .recheck import STAGE_TEXT as RECHECK_STAGE_TEXT
from .target_master import TARGET_ID_PATTERN
from .verified_top import CONFIGURATION_COLUMNS, VerifiedRankingInputError, read_csv_rows

# Blank-cell statuses that name an unconfirmed candidate or the week's recorded search outcome
# (2026-10-19); none carries a price or a link.
UNPRICED_STATUS_TEXT = {
    CANDIDATE_NOT_CONFIRMED: "Candidate found, not a confirmed match (no price)",
    "FETCH_FAILED": "Candidate found, fetching failed",
    "CANDIDATE_NOT_FETCHED": "Candidate found, not fetched",
    "NO_RELEVANT_CANDIDATE": "Searched; no relevant candidate found",
    "NO_CANDIDATE_FOUND": "Searched; no candidate found",
    "CATALOG_IDENTITY_UNRESOLVED": "Catalog identity unresolved (broad family name)",
    "DISCOVERY_INCOMPLETE": "Search incomplete; no relevant candidate in the searches run",
    "SEARCH_FAILED": "Search failed",
    "DISCOVERY_NOT_ATTEMPTED": "Search not attempted",
}

PUBLISHER_VERSION = "pilot-sheets-4"
COMPARISON_TAB = "PILOT_PRICE_COMPARISON"
HISTORY_TAB = "PILOT_PRICE_HISTORY"
REVIEW_TAB = "PILOT_PRICE_REVIEW"
RUN_LOG_TAB = "PILOT_PRICE_RUN_LOG"
# The exact destination allowlist. Names are compared exactly: no trimming or case folding.
PUBLISHER_TABS: tuple[str, ...] = (COMPARISON_TAB, HISTORY_TAB, REVIEW_TAB, RUN_LOG_TAB)
DATA_TABS: tuple[str, ...] = (COMPARISON_TAB, HISTORY_TAB, REVIEW_TAB)

RETAILER_NAMES = {
    "DBZ": "DataBlitz",
    "LF": "Laptop Factory",
    "PCX": "PC Express",
    "PCC": "PC Central",
    "OCT": "Octagon",
}
REASON_TEXT = {
    "AVAILABILITY_OUT_OF_STOCK": "out of stock",
    "AVAILABILITY_PREORDER": "preorder",
    "AVAILABILITY_UNKNOWN": "stock unknown",
    "BUNDLE_OFFER": "bundle offer",
    "SPECIFIC_IDENTITY_UNRESOLVED": "listed-spec match; specific Shopee model unknown",
    "CPU_NUMBER_DIFFERS_FROM_TARGET_RECORD": "processor number differs from the target record",
    "OFFERS_DISAGREE_RESOLUTION": "retailers state different resolutions (see review)",
    "OFFERS_DISAGREE_DISPLAY_TYPE": "retailers state different panel types (see review)",
    "OFFERS_DISAGREE_GPU_MODEL": "retailers state different GPUs (see review)",
    "BUNDLE_STATUS_UNKNOWN": "bundle status unknown",
    "INCLUDED_ITEMS_OFFER": "includes stated items",
    "PAID_PACKAGE_PRICE": "paid package price",
    "PURCHASE_CONDITION_REQUIRED": "price requires another purchase",
    "PURCHASE_TERMS_UNKNOWN": "bundle terms not stated",
    "RESTRICTED_PRICE": "restricted price (financing, card, trade-in or member)",
    "CURRENCY_NOT_PHP": "not priced in PHP",
    "PRICE_TYPE_UNKNOWN": "price type unknown",
    "PRICE_INVALID": "invalid price",
    "RETAILER_NOT_IN_COMPARISON": "retailer not compared",
}
NUMBER_FORMATS = {
    "php": {"type": "CURRENCY", "pattern": '"₱"#,##0.00'},
    "integer": {"type": "NUMBER", "pattern": "0"},
    "decimal": {"type": "NUMBER", "pattern": "0.0"},
}
CELL_FIELDS = (
    "userEnteredValue,userEnteredFormat.numberFormat,"
    "userEnteredFormat.textFormat.link,userEnteredFormat.textFormat.bold"
)
READ_FIELDS = (
    "sheets(properties(sheetId,title,gridProperties(rowCount,columnCount,frozenRowCount)),"
    "data(startRow,startColumn,rowData(values(userEnteredValue,hyperlink,"
    "userEnteredFormat(numberFormat,textFormat(link,bold))))))"
)


class PublicationInputError(ValueError):
    """The reports or scope are not consistent; nothing is published."""

    def __init__(self, issues: Sequence[str]) -> None:
        self.issues = tuple(issues)
        super().__init__(f"{len(self.issues)} input issue(s): " + "; ".join(self.issues[:15]))


class DestinationError(RuntimeError):
    """A destination tab is not safe to write (not allowlisted, or not publisher-owned)."""


def assert_publisher_tab(title: str) -> None:
    if title not in PUBLISHER_TABS:
        raise DestinationError(f"Tab {title!r} is not a pilot publisher destination.")


# Cells and tab plans


@dataclass(frozen=True)
class Cell:
    kind: str = "blank"  # "text", "number", or "blank"
    value: str = ""  # exact text; numbers as exact decimal text
    link: str = ""
    number_format: str = ""
    bold: bool = False

    def plain(self) -> list[str]:
        return [self.kind, self.value, self.link, self.number_format]


BLANK = Cell()


def text(value: object, *, link: str = "", bold: bool = False) -> Cell:
    value = "" if value is None else str(value)
    if not value:
        return BLANK
    return Cell("text", value, link, "", bold)


def number(value: str, number_format: str) -> Cell:
    """A numeric cell from exact decimal text; blank text stays blank, never zero."""
    if value == "":
        return BLANK
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise PublicationInputError([f"{value!r} is not a decimal number"]) from None
    if not parsed.is_finite():
        raise PublicationInputError(["a number is not finite"])
    return Cell("number", value, "", number_format)


@dataclass(frozen=True)
class TabPlan:
    title: str
    header: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]

    def grid(self) -> list[list[Cell]]:
        return [[text(name, bold=True) for name in self.header], *(list(r) for r in self.rows)]

    @property
    def width(self) -> int:
        return len(self.header)


def grid_sha256(plans: Sequence[TabPlan]) -> str:
    payload = [[plan.title, [[cell.plain() for cell in row] for row in plan.grid()]]
               for plan in plans]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


# Reading and validating the reviewed reports

REPORT_FILES = {
    "comparison": "weekly_price_comparison.csv",
    "selections": "weekly_price_selections.csv",
    "history": "price_history.csv",
    "review": "price_review_queue.csv",
    "summary": "price_tracking_run_summary.json",
}
# Report files that older report folders do not have (read when present).
OPTIONAL_REPORT_FILES = {"rechecks": "recheck_candidates.csv"}
COLLECTION_FILES = {
    "summary": "collection_run_summary.json",
    "results": "match_results.csv",
    "review": "match_review_queue.csv",
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc(text_value: str) -> datetime:
    return datetime.fromisoformat(text_value).astimezone(UTC)


def _local(moment: datetime) -> str:
    return moment.astimezone(ZoneInfo(REPORTING_TIMEZONE)).isoformat(timespec="seconds")


def _codes(counted: str) -> list[str]:
    """``CODE:1;OTHER:2`` (selection reason counts) or ``A;B`` -> ordered codes."""
    return [item.split(":")[0] for item in counted.split(";") if item]


def _reason_text(codes: Sequence[str]) -> str:
    return ", ".join(REASON_TEXT.get(code, code) for code in codes)


@dataclass
class PilotReport:
    """Validated, pilot-filtered report content plus provenance."""

    report_dir: Path
    scope: list[dict[str, str]]
    comparison: list[dict[str, str]]
    selections: dict[tuple[str, str], dict[str, str]]
    history: list[dict[str, str]]
    review: list[dict[str, str]]
    collection_review: list[dict[str, str]]
    summary: dict
    collection_summary: dict | None
    fingerprints: dict[str, str]
    counts: dict[str, int] = field(default_factory=dict)
    # Recheck candidates of blank pilot cells (recheck_candidates.csv; optional before
    # 2026-10-19). Never prices: links and reasons for a manual recheck.
    rechecks: list[dict[str, str]] = field(default_factory=list)

    @property
    def week_start(self) -> date:
        return date.fromisoformat(self.summary["report_week"]["start"])

    @property
    def week_end(self) -> date:
        return date.fromisoformat(self.summary["report_week"]["end_exclusive"])

    @property
    def as_of(self) -> datetime:
        return _utc(self.summary["as_of_utc"])


def load_pilot_report(
    report_dir: Path,
    pilot_scope: Path,
    *,
    collection_dir: Path | None = None,
    selected_list: Path | None = None,
) -> PilotReport:
    """Read every input, validate it as a whole, and keep only pilot configurations.

    Raises ``PublicationInputError`` listing every issue found; nothing is partly accepted.
    """
    issues: list[str] = []
    paths = {key: report_dir / name for key, name in REPORT_FILES.items()}
    for path in paths.values():
        if not path.is_file():
            issues.append(f"missing report file {path.name}")
    if not pilot_scope.is_file():
        issues.append("missing pilot scope file")
    if issues:
        raise PublicationInputError(issues)

    try:
        # Reports saved before 2026-10-15 lack the added columns; they stay readable and the
        # added cells are blank (never invented).
        comparison = [
            dict.fromkeys(COMPARISON_COLUMNS, "") | row for row in read_csv_rows(
                paths["comparison"], LEGACY_COMPARISON_COLUMNS, "Comparison report")
        ]
        selections = [
            dict.fromkeys(SELECTION_COLUMNS, "") | row for row in read_csv_rows(
                paths["selections"], LEGACY_SELECTION_COLUMNS, "Selections report")
        ]
        history = read_csv_rows(paths["history"], HISTORY_COLUMNS, "History report")
        review = read_csv_rows(paths["review"], REVIEW_COLUMNS, "Review report")
        scope = read_csv_rows(pilot_scope, CONFIGURATION_COLUMNS, "Pilot scope")
    except VerifiedRankingInputError as exc:
        raise PublicationInputError([str(exc)]) from None
    try:
        summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
        week = summary["report_week"]
        as_of = _utc(summary["as_of_utc"])
        week_start = date.fromisoformat(week["start"])
        week_end = date.fromisoformat(week["end_exclusive"])
        start_utc, end_utc = _utc(week["start_utc"]), _utc(week["end_utc_exclusive"])
        sales = summary["sales_snapshot"]
        selected_info = summary["selected_list"]
    except (ValueError, KeyError, TypeError) as exc:
        raise PublicationInputError([f"run summary is unreadable ({type(exc).__name__})"]) from None

    fingerprints = {f"report/{name}": sha256_file(path) for name, path in
                    ((p.name, p) for p in paths.values())}
    fingerprints["pilot_scope"] = sha256_file(pilot_scope)

    # Report-level consistency.
    if week_end - week_start != timedelta(days=7) or week_start.weekday() != 0:
        issues.append("report week is not a Monday-to-Monday week")
    if as_of < start_utc:
        issues.append("report cutoff is before the report week")
    if len(comparison) != selected_info.get("configurations"):
        issues.append("comparison row count differs from the selected-list size in the summary")
    for row in comparison:
        if (row["report_week_start"], row["report_week_end"]) != (week["start"],
                                                                  week["end_exclusive"]):
            issues.append(f"comparison row {row['target_id']} is for a different week")
            break
        if _utc(row["as_of"]) != as_of:
            issues.append(f"comparison row {row['target_id']} has a different cutoff")
            break
        if row["selected_list_sha256"] != selected_info.get("sha256"):
            issues.append("comparison rows name a different selected list than the summary")
            break
    by_target = {row["target_id"]: row for row in comparison}
    if len(by_target) != len(comparison):
        issues.append("comparison report repeats a target_id")

    # Scope: unique, well-formed IDs that are members of the verified selected list.
    scope_ids = [row["target_id"] for row in scope]
    if not scope:
        issues.append("pilot scope has no rows")
    if len(scope_ids) != len(set(scope_ids)):
        issues.append("pilot scope repeats a target_id")
    for row in scope:
        target_id = row["target_id"]
        if not re.fullmatch(TARGET_ID_PATTERN, target_id):
            issues.append(f"pilot scope target_id {target_id!r} is malformed")
            continue
        listed = by_target.get(target_id)
        if listed is None:
            issues.append(f"{target_id} is not in the reviewed comparison (selected list)")
            continue
        pairs = {
            "rank": "rank",
            "brand": "Brand",
            "series": "Series",
            "model": "Model",
            "screen_size_in": "Screen Size",
            "cpu_series": "CPU_Series",
            "ram_capacity_gb": "RAM_Capacity",
            "storage_capacity_gb": "Storage_Capacity",
        }
        for scope_name, report_name in pairs.items():
            if row[scope_name] != listed[report_name]:
                issues.append(f"{target_id} {scope_name} differs between scope and report")
        if row["sales_run_id"] != sales.get("run_id"):
            issues.append(f"{target_id} scope row is from a different sales snapshot")
        if row["target_master_sha256"] != selected_info.get("built_on_target_master_sha256"):
            issues.append(f"{target_id} scope row was built on a different catalog")
    if selected_list is not None:
        if not selected_list.is_file():
            issues.append("selected list file named by the summary is missing")
        elif sha256_file(selected_list) != selected_info.get("sha256"):
            issues.append("selected list file differs from the one used for the report")
        else:
            verified = {r["target_id"]: r for r in read_csv_rows(
                selected_list, CONFIGURATION_COLUMNS, "Selected list")}
            for row in scope:
                if verified.get(row["target_id"]) != row:
                    issues.append(f"{row['target_id']} scope row differs from the selected list")
            fingerprints["selected_list"] = sha256_file(selected_list)
    if issues:
        raise PublicationInputError(issues)

    scope_set = set(scope_ids)
    selection_index = {(r["target_id"], r["retailer_code"]): r for r in selections}
    history_ids = {row["observation_id"] for row in history}
    if len(history_ids) != len(history):
        issues.append("history report repeats an observation_id")
    by_observation = {row["observation_id"]: row for row in history}

    # Comparison cells: blank prices stay blank; links come only from exact current-week offers.
    pilot_comparison = sorted(
        (by_target[target_id] for target_id in scope_ids), key=lambda r: int(r["rank"])
    )
    for row in pilot_comparison:
        target_id = row["target_id"]
        prices: dict[str, Decimal] = {}
        for code, retailer_id in RETAILERS:
            price, url = row[f"{code}_Price"], row[f"{code}_Offer_URL"]
            status = row[f"{code}_Price_Status"]
            selection = selection_index.get((target_id, code))
            where = f"{target_id} {code}"
            if selection is None or selection["price_status"] != status:
                issues.append(f"{where} status disagrees with the selections report")
                continue
            if status not in {SELECTED, NO_COMPARABLE_OFFER, REVIEW_REQUIRED_ONLY,
                              NO_OBSERVATION_IN_WEEK, *UNPRICED_STATUS_TEXT}:
                issues.append(f"{where} has an unknown price status")
                continue
            if status == SELECTED:
                try:
                    amount = Decimal(price)
                except InvalidOperation:
                    issues.append(f"{where} selected price is not a decimal")
                    continue
                if amount <= 0 or not amount.is_finite():
                    issues.append(f"{where} selected price is not positive")
                if (price, url) != (selection["price"], selection["offer_url"]):
                    issues.append(f"{where} price or link is not the offer that supplied it")
                if selection["currency"] != REPORTING_CURRENCY:
                    issues.append(f"{where} selected price is not PHP")
                retrieved = _utc(selection["retrieved_at_utc"])
                if not (start_utc <= retrieved < end_utc and retrieved <= as_of):
                    issues.append(f"{where} selected price was not observed in the report week")
                observed = by_observation.get(selection["observation_id"])
                if selection.get("comparison_basis") == "LISTED_SPEC":
                    # A listed-spec price (2026-10-20): the observation's own listed-spec
                    # association for this target, comparable under the same price rules.
                    if observed is None or target_id not in observed.get(
                        "listed_spec_target_ids", ""
                    ).split(";") or observed.get("listed_spec_comparability") != "COMPARABLE":
                        issues.append(f"{where} listed-spec price is not a comparable listed-spec"
                                      " association")
                elif observed is None or (observed["match_status"], observed["target_id"]) != (
                    "EXACT", target_id
                ):
                    issues.append(f"{where} selected price is not an exact observation")
                prices[code] = amount
            else:
                if price != "":
                    issues.append(f"{where} has a price without a comparable offer")
                if url and status != NO_COMPARABLE_OFFER:
                    issues.append(f"{where} has a link without an exact offer this week")
                if url:
                    fallback = [
                        h for h in history
                        if h["offer_url"] == url
                        and ((h["match_status"] == "EXACT" and h["target_id"] == target_id)
                             or target_id in h.get("listed_spec_target_ids", "").split(";"))
                        and h["retailer_id"] == retailer_id
                        and h["report_week_start"] == week["start"]
                        and h["latest_for_offer_in_week"] == "True"
                    ]
                    if not fallback:
                        issues.append(f"{where} link is not a current-week exact offer")
        lowest = min(prices.values()) if prices else None
        if (Decimal(row["Lowest_Price"]) if row["Lowest_Price"] else None) != lowest:
            issues.append(f"{target_id} lowest price disagrees with its retailer prices")
        retailers = ";".join(code for code, _ in RETAILERS if prices.get(code) == lowest)
        if row["Lowest_Price_Retailers"] != (retailers if lowest is not None else ""):
            issues.append(f"{target_id} lowest-price retailers are wrong")
        if row["Comparable_Retailer_Count"] != str(len(prices)):
            issues.append(f"{target_id} comparable retailer count is wrong")
        # The in-stock minimum must come from the same selections, filtered to IN_STOCK offers.
        in_stock: dict[str, Decimal] = {}
        for code, _ in RETAILERS:
            selection = selection_index.get((target_id, code))
            if selection and selection["in_stock_price"]:
                try:
                    in_stock[code] = Decimal(selection["in_stock_price"])
                except InvalidOperation:
                    issues.append(f"{target_id} {code} in-stock price is not a decimal")
        lowest_in_stock = min(in_stock.values()) if in_stock else None
        stated = row["Lowest_In_Stock_Price"]
        if (Decimal(stated) if stated else None) != lowest_in_stock:
            issues.append(f"{target_id} lowest in-stock price disagrees with its selections")

    # Manifest expectations scope the pilot without assigning an uncertain offer to a target.
    pilot_observation_ids: set[str] = set()
    pilot_offers: set[tuple[str, str, str]] = set()
    if collection_dir is not None:
        results_path = collection_dir / COLLECTION_FILES["results"]
        if results_path.is_file():
            collection_results = read_csv_rows(
                results_path,
                ("observation_id", "match_status", "target_id", "expected_target_id"),
                "Collection results",
            )
            pilot_observation_ids = {
                row["observation_id"]
                for row in collection_results
                if row["expected_target_id"] in scope_set or row["target_id"] in scope_set
            }
            pilot_offers = {
                (row.get("retailer_id", ""), row["offer_url"], row.get("offer_index", "0"))
                for row in collection_results
                if row.get("offer_url")
                and (row["expected_target_id"] in scope_set or row["target_id"] in scope_set)
            }

    # History and review include unassigned observations collected for this pilot scope.
    def in_scope(row: Mapping[str, str]) -> bool:
        listed = set(filter(None, ";".join((
            row.get("listed_spec_target_ids", ""), row.get("listed_spec_ambiguous_target_ids", ""),
        )).split(";")))
        if listed & scope_set:
            return True
        if row["match_status"] == "EXACT":
            return row["target_id"] in scope_set
        candidates = set(filter(None, row["candidate_target_ids"].split(";")))
        offer = (row.get("retailer_id", ""), row.get("offer_url", ""),
                 row.get("offer_index", "0"))
        return (
            bool(candidates & scope_set)
            or row["observation_id"] in pilot_observation_ids
            or offer in pilot_offers
        )

    pilot_history = [row for row in history if in_scope(row)]
    for row in pilot_history:
        if _utc(row["retrieved_at_utc"]) > as_of:
            issues.append(f"history observation {row['observation_id'][:12]} is after the cutoff")
        if row["match_status"] != "EXACT" and row["target_id"]:
            issues.append("a non-exact history row carries a target_id")
    pilot_review = [row for row in review if in_scope(row)]
    for row in pilot_review:
        if row["observation_id"] not in history_ids:
            issues.append("a review row has no history observation")

    collection_summary = None
    collection_review: list[dict[str, str]] = []
    collection_exact_outside_scope = 0
    if collection_dir is not None:
        files = {key: collection_dir / name for key, name in COLLECTION_FILES.items()}
        missing = [p.name for p in files.values() if not p.is_file()]
        if missing:
            issues.append("collection evidence is missing " + ", ".join(missing))
        else:
            collection_summary = json.loads(files["summary"].read_text(encoding="utf-8"))
            if collection_summary.get("mode") != "LIVE" or collection_summary.get(
                "run_status"
            ) not in {"SUCCESS", "PARTIAL_SUCCESS"}:
                issues.append("collection evidence is not a successful live run")
            results = read_csv_rows(files["results"], ("observation_id", "match_status",
                                    "target_id", "expected_target_id"), "Collection results")
            for result in results:
                if result["observation_id"] not in history_ids:
                    issues.append("a collected observation is missing from the history report")
                if result["match_status"] == "EXACT":
                    if not result["target_id"]:
                        issues.append("a collected exact match has no target_id")
                    elif result["target_id"] not in scope_set:
                        # Discovery can find a different configuration while searching for a
                        # scoped target. Keep its evidence in the full history; in_scope()
                        # already excludes it from pilot comparison/history. A scoped search
                        # mismatch remains in collection_review below.
                        collection_exact_outside_scope += 1
            collection_review = [
                row for row in read_csv_rows(
                    files["review"], ("review_reasons", "observation_id", "target_id",
                                      "candidate_target_ids", "expected_target_id"),
                    "Collection review")
                if row["target_id"] in scope_set
                or row["expected_target_id"] in scope_set
                or set(filter(None, row["candidate_target_ids"].split(";"))) & scope_set
            ]
            for path in files.values():
                fingerprints[f"collection/{path.name}"] = sha256_file(path)
    rechecks: list[dict[str, str]] = []
    recheck_path = report_dir / OPTIONAL_REPORT_FILES["rechecks"]
    if recheck_path.is_file():
        try:
            rechecks = [
                row for row in read_csv_rows(recheck_path, RECHECK_COLUMNS, "Recheck candidates")
                if row["target_id"] in scope_set
            ]
        except VerifiedRankingInputError as exc:
            issues.append(str(exc))
        fingerprints[f"report/{recheck_path.name}"] = sha256_file(recheck_path)
        for row in rechecks:
            cell = by_target.get(row["target_id"], {})
            if row["report_week_start"] != week["start"]:
                issues.append("a recheck candidate is for a different week")
            # An unconfirmed page never sits beside a comparable price for the same cell.
            if cell.get(f"{row['retailer_code']}_Price"):
                issues.append(f"{row['target_id']} {row['retailer_code']} has a recheck "
                              "candidate beside a comparable price")
    if issues:
        raise PublicationInputError(sorted(set(issues), key=issues.index))

    week_history = [r for r in pilot_history if r["report_week_start"] == week["start"]]
    cells = [(row, code) for row in pilot_comparison for code, _ in RETAILERS]
    counts = {
        "pilot_configurations": len(scope),
        "collection_exact_outside_scope": collection_exact_outside_scope,
        "comparison_rows": len(pilot_comparison),
        "history_rows": len(pilot_history),
        "review_rows": len(pilot_review) + sum(
            1 for r in collection_review
            if r["observation_id"] not in {x["observation_id"] for x in pilot_review}
        ),
        "comparable_prices": sum(1 for r, c in cells if r[f"{c}_Price_Status"] == SELECTED),
        "listed_spec_prices": sum(
            1 for r, c in cells
            if r[f"{c}_Price_Status"] == SELECTED and r.get(f"{c}_Match_Basis") == "LISTED_SPEC"
        ),
        "matched_excluded_cells": sum(
            1 for r, c in cells if r[f"{c}_Price_Status"] == NO_COMPARABLE_OFFER
        ),
        "review_required_cells": sum(
            1 for r, c in cells if r[f"{c}_Price_Status"] == REVIEW_REQUIRED_ONLY
        ),
        "not_observed_cells": sum(
            1 for r, c in cells if r[f"{c}_Price_Status"] == NO_OBSERVATION_IN_WEEK
        ),
        "unconfirmed_candidate_cells": sum(
            1 for r, c in cells if r[f"{c}_Price_Status"] == CANDIDATE_NOT_CONFIRMED
        ),
        "search_outcome_cells": sum(
            1 for r, c in cells if r[f"{c}_Price_Status"] in SEARCH_STAGE_STATUSES
        ),
        "configurations_with_price": sum(
            1 for r in pilot_comparison if r["Comparable_Retailer_Count"] != "0"
        ),
        "recheck_candidates": len(rechecks),
        "blank_cells_with_recheck_link": sum(
            1 for r, c in cells if not r[f"{c}_Price"] and r.get(f"{c}_Candidate_URL")
        ),
        "week_observations": len(week_history),
        "week_exact": sum(1 for r in week_history if r["match_status"] == "EXACT"),
        "week_matching_problems": sum(1 for r in week_history if r["match_status"] != "EXACT"),
        "week_matched_excluded": sum(
            1 for r in week_history
            if r["match_status"] == "EXACT" and r["comparability_status"] == "NOT_COMPARABLE"
        ),
    }
    return PilotReport(
        report_dir=report_dir,
        scope=scope,
        comparison=pilot_comparison,
        selections=selection_index,
        history=pilot_history,
        review=pilot_review,
        collection_review=collection_review,
        summary=summary,
        collection_summary=collection_summary,
        fingerprints=fingerprints,
        counts=counts,
        rechecks=rechecks,
    )


# Building the tab plans


BASIS_TEXT = {
    "TITLE_MODEL_CORROBORATED": "model from title, other specs from the page",
    "TITLE_PRODUCT_NAME_CORROBORATED": "product name from title, other specs from the page",
    "MODEL_NAME_PRESENTATION_NORMALIZED": "product name with brand/colour wording removed",
    "OWNER_APPROVED_INTERPRETATION": "owner-approved listing interpretation",
    "SELECTED_VARIANT": "selected variant",
    "SHARED_PRODUCT": "shared product specs",
}


def _status_text(
    status: str, reasons: str, availability: str = "", basis: str = "", warnings: str = "",
    details: str = "",
) -> str:
    """Short status for one retailer cell. "Matched configuration" means the five-field policy
    matched; it does not claim an identical factory SKU or processor number. A listed-spec price
    says so and shows the offer's own hardware (2026-10-20)."""
    if status == SELECTED:
        text_value = (
            "Comparable price (OUT OF STOCK)"
            if availability == "OUT_OF_STOCK" else "Comparable price"
        )
        if basis == "LISTED_SPEC":
            text_value += ("; listed-spec match — brand, family, screen, CPU series, installed "
                           "RAM and storage agree; specific Shopee model unknown")
            if details:
                text_value += f"; {details}"
        detail = ", ".join(BASIS_TEXT[tag] for tag in basis.split("+") if tag in BASIS_TEXT)
        if detail:
            text_value += f"; matched configuration ({detail})"
    elif status == NO_COMPARABLE_OFFER:
        text_value = "Matched, not comparable: " + (_reason_text(_codes(reasons)) or "excluded")
    elif status == REVIEW_REQUIRED_ONLY:
        text_value = "Needs match review (no price)"
    elif status in UNPRICED_STATUS_TEXT:
        text_value = UNPRICED_STATUS_TEXT[status]
    else:
        text_value = "No confirmed matched offer this week"
    if warnings:
        text_value += "; notes: " + _reason_text(_codes(warnings))
    return text_value


# The comparison header written by publisher pilot-sheets-1 (before 2026-10-15). A live tab that
# still carries it is this publisher's own content and may be replaced by the current layout.
PILOT_SHEETS_1_COMPARISON_HEADER: tuple[str, ...] = (
    "Rank", "Target ID", "Brand", "Series", "Model", "Screen (in)", "CPU family", "RAM (GB)",
    "Storage (GB)",
    *(
        column
        for code, _ in RETAILERS
        for column in (
            f"{RETAILER_NAMES[code]} price (PHP)",
            f"{RETAILER_NAMES[code]} product link",
            f"{RETAILER_NAMES[code]} status",
        )
    ),
    "Lowest comparable price (PHP)", "Lowest-price retailer(s)", "Comparable retailers",
    "Reporting week (Asia/Singapore)", "Week status", "Report cutoff (Asia/Singapore)",
)
EARLIER_HEADERS: dict[str, tuple[tuple[str, ...], ...]] = {
    COMPARISON_TAB: (PILOT_SHEETS_1_COMPARISON_HEADER,),
}


def _outcome(row: Mapping[str, str]) -> str:
    listed = row.get("listed_spec_target_ids", "")
    if listed:
        state = ("comparable" if row.get("listed_spec_comparability") == "COMPARABLE"
                 else "excluded: " + _reason_text(_codes(
                     row.get("listed_spec_comparability_reasons", ""))))
        prefix = (f"Listed-spec match for {listed} (specific identity unresolved), {state}")
        if row["match_status"] != "EXACT":
            return prefix
        return prefix + f"; exact product match for {row['target_id']}"
    if row.get("listed_spec_ambiguous_target_ids"):
        return ("Listed specs fit several targets equally ("
                + row["listed_spec_ambiguous_target_ids"] + "); not priced")
    if row["match_status"] != "EXACT":
        return f"Matching problem: {row['match_status']}"
    if row["comparability_status"] == "COMPARABLE":
        outcome = (
            "Matched, comparable (OUT OF STOCK)"
            if row["availability"] == "OUT_OF_STOCK" else "Matched, comparable"
        )
        return outcome + ("; bundle offer" if row["bundle_flag"] == "TRUE" else "")
    return "Matched, excluded from comparison: " + _reason_text(
        _codes(row["comparability_reasons"])
    )


def _minimum_retailers(retailers: str, notes: str) -> str:
    """Label the actual minimum's package, including ties and in-stock alternatives."""
    labels = {"BUNDLE_OFFER": "bundle offer", "INCLUDED_ITEMS_OFFER": "includes stated items"}
    noted = {
        note.split(":", 1)[0]: labels[note.split(":", 1)[1]]
        for note in notes.split(";") if ":" in note and note.split(":", 1)[1] in labels
    }
    return ", ".join(
        RETAILER_NAMES[code] + (f" ({noted[code]})" if code in noted else "")
        for code in retailers.split(";") if code
    )


def comparison_header(*, recheck: bool = True, basis: bool = True) -> tuple[str, ...]:
    """The comparison tab header. ``recheck=False`` is the pilot-sheets-2 layout (before the
    recheck columns of 2026-10-19) and ``basis=False`` the pilot-sheets-3 layout (before the
    matching-basis columns of 2026-10-20); both are recognised as this publisher's."""
    header = [
        "Rank",
        "Target ID",
        "Brand",
        "Series",
        "Model",
        "Screen (in)",
        "CPU family",
        "RAM (GB)",
        "Storage (GB)",
    ]
    for code, _ in RETAILERS:
        name = RETAILER_NAMES[code]
        header += [
            f"{name} price (PHP)", f"{name} product link", f"{name} status",
            f"{name} observed (Asia/Singapore)",
        ]
        if recheck:
            # Unconfirmed pages to recheck manually when the price is blank: never a price.
            header += [f"{name} recheck link (unconfirmed)", f"{name} recheck note"]
    header += [
        # Out-of-stock prices are comparable under the current owner policy, so this is the
        # lowest *observed* price; the in-stock minimum is calculated separately.
        "Lowest observed price (PHP)",
        "Lowest observed price retailer(s)",
        *(["Lowest observed price basis"] if basis else []),
        "Lowest in-stock price (PHP)",
        "Lowest in-stock retailer(s)",
        *(["Lowest in-stock price basis"] if basis else []),
        "Comparable retailers",
        *(["Comparison basis"] if basis else []),
        "Shopee sales snapshot (extract date)",
        "Reporting week (Asia/Singapore)",
        "Week status",
        "Report cutoff (Asia/Singapore)",
    ]
    return tuple(header)


# The pilot-sheets-2 and pilot-sheets-3 comparison layouts are this publisher's own too.
EARLIER_HEADERS[COMPARISON_TAB] += (
    comparison_header(recheck=False, basis=False), comparison_header(basis=False),
)
BASIS_WORDS = {"EXACT_PRODUCT": "exact product match", "LISTED_SPEC": "listed-spec match",
               "MIXED": "exact product and listed-spec matches"}


def _minimum_basis(label: str, basis: str) -> str:
    """'Lowest price matching listed specs — winning offer: listed-spec match'."""
    if not label:
        return ""
    return f"{label} — winning offer: {BASIS_WORDS.get(basis, basis)}" if basis else label


def _recheck_cells(report: PilotReport, row: Mapping[str, str], code: str) -> list[Cell]:
    """The first recheck link and its reason, only beside a blank price."""
    url = row.get(f"{code}_Candidate_URL", "")
    if row[f"{code}_Price"] or not url:
        return [BLANK, BLANK]
    note = row.get(f"{code}_Recheck_Note") or row.get(f"{code}_Candidate_Detail", "")
    count = int(row.get(f"{code}_Recheck_Count") or 1)
    if count > 1:
        note += f" (+{count - 1} more in {REVIEW_TAB})"
    return [text(url, link=url), text(note)]


def comparison_plan(report: PilotReport) -> TabPlan:
    header = list(comparison_header())
    stock_by_observation = {row["observation_id"]: row["availability"] for row in report.history}
    week = (
        f"{report.week_start.isoformat()} to "
        f"{(report.week_end - timedelta(days=1)).isoformat()}"
    )
    rows = []
    for row in report.comparison:
        cells = [
            number(row["rank"], "integer"),
            text(row["target_id"]),
            text(row["Brand"]),
            text(row["Series"]),
            text(row["Model"]),
            number(row["Screen Size"], "decimal"),
            text(row["CPU_Series"]),
            number(row["RAM_Capacity"], "integer"),
            number(row["Storage_Capacity"], "integer"),
        ]
        for code, _ in RETAILERS:
            url = row[f"{code}_Offer_URL"]
            selection = report.selections[(row["target_id"], code)]
            cells += [
                number(row[f"{code}_Price"], "php"),
                text(url, link=url),
                text(_status_text(row[f"{code}_Price_Status"],
                                  selection["not_comparable_reasons"],
                                  stock_by_observation.get(selection["observation_id"], ""),
                                  row[f"{code}_Match_Basis"], row[f"{code}_Warnings"],
                                  row.get(f"{code}_Offer_Details", ""))),
                text(row[f"{code}_Observed_At"]),
                *_recheck_cells(report, row, code),
            ]
        lowest = row["Lowest_Price_Retailers"]
        in_stock = row["Lowest_In_Stock_Price_Retailers"]
        cells += [
            number(row["Lowest_Price"], "php"),
            text(_minimum_retailers(lowest, row.get("Lowest_Price_Notes", ""))),
            text(_minimum_basis(row.get("Lowest_Price_Label", ""),
                                row.get("Lowest_Price_Basis", ""))),
            number(row["Lowest_In_Stock_Price"], "php"),
            text(_minimum_retailers(in_stock, row.get("Lowest_In_Stock_Notes", ""))),
            text(_minimum_basis(row.get("Lowest_In_Stock_Label", ""),
                                row.get("Lowest_In_Stock_Basis", ""))),
            number(row["Comparable_Retailer_Count"], "integer"),
            text(BASIS_WORDS.get(row.get("Comparison_Basis", ""), "")),
            text(row["Shopee_Sales_Extract_Date"]),
            text(week),
            text(report.summary["report_week"]["status"]),
            text(_local(report.as_of)),
        ]
        rows.append(tuple(cells))
    return TabPlan(COMPARISON_TAB, tuple(header), tuple(rows))


def history_plan(report: PilotReport) -> TabPlan:
    header = (
        "Week start",
        "Retrieved (Asia/Singapore)",
        "Retrieved (UTC)",
        "Rank",
        "Target ID",
        "Candidate target(s)",
        "Model",
        "Retailer",
        "Product link",
        "Observed page (unconfirmed match)",
        "Observed price (PHP)",
        "Price type",
        "Regular price (PHP)",
        "Availability",
        "Bundle",
        "Match status",
        "Match reasons",
        "Outcome",
        "Comparison status",
        "Comparison reasons",
        "Change vs previous comparable (PHP)",
        "Change status",
        "Latest for offer in week",
        "Observation ID",
    )
    models = {row["target_id"]: row["model"] for row in report.scope}
    ranks = {row["target_id"]: row["rank"] for row in report.scope}
    rows = []
    ordered = sorted(report.history, key=lambda r: (r["retrieved_at_utc"], r["observation_id"]))
    for row in ordered:
        exact = row["match_status"] == "EXACT"
        url = row["offer_url"]
        rows.append(
            (
                text(row["report_week_start"]),
                text(_local(_utc(row["retrieved_at_utc"]))),
                text(row["retrieved_at_utc"]),
                number(ranks.get(row["target_id"], "") if exact else "", "integer"),
                text(row["target_id"] if exact else ""),
                text("" if exact else row["candidate_target_ids"]),
                text(models.get(row["target_id"], "") if exact else ""),
                text(RETAILER_NAMES.get(row["retailer_code"], row["retailer_id"])),
                text(url, link=url) if exact else BLANK,
                BLANK if exact else text(url),
                number(row["price"], "php"),
                text(row["price_type"]),
                number(row["regular_price"], "php"),
                text(row["availability"]),
                text(row["bundle_flag"]),
                text(row["match_status"]),
                text(row["match_reason_codes"]),
                text(_outcome(row)),
                text(row["comparability_status"]),
                text(row["comparability_reasons"]),
                number(row["price_change"], "php"),
                text(row["price_change_status"]),
                text(row["latest_for_offer_in_week"]),
                text(row["observation_id"]),
            )
        )
    return TabPlan(HISTORY_TAB, header, tuple(rows))


def review_plan(report: PilotReport) -> TabPlan:
    header = (
        "Category",
        "Review reason",
        "Details",
        "Week start",
        "Retrieved (Asia/Singapore)",
        "Rank",
        "Target ID",
        "Candidate target(s)",
        "Retailer",
        "Product link",
        "Observed page (unconfirmed match)",
        "Observed price (PHP)",
        "Availability",
        "Match status",
        "Match reasons",
        "Comparison status",
        "Comparison reasons",
        "Observation ID",
    )
    ranks = {row["target_id"]: row["rank"] for row in report.scope}
    collection = {row["observation_id"]: row for row in report.collection_review}
    rows = []
    seen = set()
    for row in sorted(report.review, key=lambda r: (r["retrieved_at_utc"], r["observation_id"])):
        exact = row["match_status"] == "EXACT"
        extra = collection.get(row["observation_id"], {}).get("review_reasons", "")
        seen.add(row["observation_id"])
        category = (
            "Matched, excluded from comparison"
            if exact and row["comparability_status"] == "NOT_COMPARABLE"
            else "Matching problem"
            if not exact
            else "Other review"
        )
        details = _reason_text(_codes(row["comparability_reasons"])) if exact else ""
        if extra:
            details = "; ".join(filter(None, [details, f"collection: {extra}"]))
        rows.append(
            (
                text(category),
                text(row["review_reason"]),
                text(details),
                text(row["report_week_start"]),
                text(_local(_utc(row["retrieved_at_utc"]))),
                number(ranks.get(row["target_id"], "") if exact else "", "integer"),
                text(row["target_id"] if exact else ""),
                text("" if exact else row["candidate_target_ids"]),
                text(RETAILER_NAMES.get(row["retailer_code"], row["retailer_id"])),
                text(row["offer_url"], link=row["offer_url"]) if exact else BLANK,
                BLANK if exact else text(row["offer_url"]),
                number(row["price"], "php"),
                text(row["availability"]),
                text(row["match_status"]),
                text(row["match_reason_codes"]),
                text(row["comparability_status"]),
                text(row["comparability_reasons"]),
                text(row["observation_id"]),
            )
        )
    ranks_by_target = {row["target_id"]: row["rank"] for row in report.scope}
    for row in sorted(report.rechecks, key=lambda r: (
        int(r["rank"]), [code for code, _ in RETAILERS].index(r["retailer_code"]),
        int(r["recheck_rank"]),
    )):
        details = "; ".join(filter(None, (
            row["explanation"],
            row["stock"].replace("_", " ").lower(),
            f"observed {row['observed_at_local']}" if row["observed_at_local"] else "",
            f"unconfirmed price PHP {row['unconfirmed_price']}"
            if row["unconfirmed_price"] else "",
            f"listed title: {row['title']}" if row["title"] else "",
        )))
        rows.append(
            (
                text("Recheck candidate (unconfirmed)"),
                text(RECHECK_STAGE_TEXT.get(row["stage"], row["stage"])),
                text(details),
                text(row["report_week_start"]),
                text(row["observed_at_local"]),
                number(ranks_by_target.get(row["target_id"], ""), "integer"),
                BLANK,
                text(row["target_id"]),
                text(RETAILER_NAMES.get(row["retailer_code"], row["retailer_id"])),
                BLANK,
                text(row["url"], link=row["url"]),
                BLANK,
                text(row["stock"]),
                text(row["status"]),
                BLANK,
                text(f"not priced (cell: {row['cell_price_status']})"),
                BLANK,
                text(row["observation_id"]),
            )
        )
    for observation_id, row in collection.items():
        if observation_id in seen:
            continue
        exact = row.get("match_status") == "EXACT"
        rows.append(
            (
                text("Matching problem"),
                text(row["review_reasons"]),
                text(f"expected {row['expected_target_id']}" if row["expected_target_id"]
                     else ""),
                BLANK,
                text(_local(_utc(row["retrieved_at"]))) if row.get("retrieved_at") else BLANK,
                BLANK,
                text(row["target_id"] if exact else ""),
                text("" if exact else row["candidate_target_ids"]),
                text(RETAILER_NAMES.get(_code_for(row.get("retailer_id", "")), "")),
                BLANK,
                text(row.get("offer_url", "")),
                number(row.get("price", ""), "php"),
                text(row.get("availability", "")),
                text(row.get("match_status", "")),
                text(row.get("reason_codes", "")),
                BLANK,
                BLANK,
                text(observation_id),
            )
        )
    return TabPlan(REVIEW_TAB, header, tuple(rows))


def _code_for(retailer_id: str) -> str:
    return next((code for code, rid in RETAILERS if rid == retailer_id), "")


RUN_LOG_HEADER: tuple[str, ...] = (
    "Publication ID",
    "Publication status",
    "First published (UTC)",
    "Last published (UTC)",
    "Last published (Asia/Singapore)",
    "Publication attempts",
    "Reporting week start",
    "Reporting week end (exclusive)",
    "Week status",
    "Report cutoff (UTC)",
    "Report cutoff (Asia/Singapore)",
    "Collection started (UTC)",
    "Collection finished (UTC)",
    "Pilot configurations",
    "Comparison rows",
    "History rows",
    "Review rows",
    "Comparable prices",
    "Matched but excluded (stock or price rules)",
    "Needs match review (no price)",
    "Retailer cells not observed",
    "Configurations with a price",
    "Week observations",
    "Week exact matches",
    "Week matching problems",
    "Shopee sales snapshot",
    "Shopee sales run ID",
    "Tabs written",
    "Readback",
    "Notes",
    "Report directory",
    "Selected list SHA-256",
    "Catalog (TARGET_MASTER) SHA-256",
    "Matching context ID",
    "Input fingerprints",
    "Published content SHA-256",
    "Publisher version",
)


def publication_id(report: PilotReport) -> str:
    payload = json.dumps(
        {"inputs": report.fingerprints, "publisher": PUBLISHER_VERSION}, sort_keys=True
    )
    return "PUB-" + hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class PublicationPlan:
    report: PilotReport
    tabs: dict[str, TabPlan]
    publication_id: str
    content_sha256: str

    def run_log_values(
        self,
        *,
        status: str,
        published_at: datetime,
        first_published: str,
        attempts: int,
        tabs_written: Sequence[str],
        readback: str,
        notes: str = "",
    ) -> dict[str, Cell]:
        report = self.report
        week = report.summary["report_week"]
        sales = report.summary["sales_snapshot"]
        counts = report.counts
        collection = report.collection_summary or {}
        context = report.summary.get("matching_context", {})
        values = {
            "Publication ID": text(self.publication_id),
            "Publication status": text(status),
            "First published (UTC)": text(first_published),
            "Last published (UTC)": text(published_at.astimezone(UTC).isoformat(
                timespec="seconds")),
            "Last published (Asia/Singapore)": text(_local(published_at)),
            "Publication attempts": number(str(attempts), "integer"),
            "Reporting week start": text(week["start"]),
            "Reporting week end (exclusive)": text(week["end_exclusive"]),
            "Week status": text(week["status"]),
            "Report cutoff (UTC)": text(report.summary["as_of_utc"]),
            "Report cutoff (Asia/Singapore)": text(_local(report.as_of)),
            "Collection started (UTC)": text(collection.get("started_at_utc", "")),
            "Collection finished (UTC)": text(collection.get("finished_at_utc", "")),
            "Pilot configurations": number(str(counts["pilot_configurations"]), "integer"),
            "Comparison rows": number(str(len(self.tabs[COMPARISON_TAB].rows)), "integer"),
            "History rows": number(str(len(self.tabs[HISTORY_TAB].rows)), "integer"),
            "Review rows": number(str(len(self.tabs[REVIEW_TAB].rows)), "integer"),
            "Comparable prices": number(str(counts["comparable_prices"]), "integer"),
            "Matched but excluded (stock or price rules)": number(
                str(counts["matched_excluded_cells"]), "integer"
            ),
            "Needs match review (no price)": number(
                str(counts["review_required_cells"]), "integer"
            ),
            "Retailer cells not observed": number(str(counts["not_observed_cells"]), "integer"),
            "Configurations with a price": number(
                str(counts["configurations_with_price"]), "integer"
            ),
            "Week observations": number(str(counts["week_observations"]), "integer"),
            "Week exact matches": number(str(counts["week_exact"]), "integer"),
            "Week matching problems": number(str(counts["week_matching_problems"]), "integer"),
            "Shopee sales snapshot": text(sales.get("label", "")),
            "Shopee sales run ID": text(sales.get("run_id", "")),
            "Tabs written": text(", ".join(tabs_written)),
            "Readback": text(readback),
            "Notes": text(notes),
            "Report directory": text(str(report.report_dir)),
            "Selected list SHA-256": text(report.summary["selected_list"].get("sha256", "")),
            "Catalog (TARGET_MASTER) SHA-256": text(context.get("target_master_sha256", "")),
            "Matching context ID": text(context.get("context_id", "")),
            "Input fingerprints": text(json.dumps(report.fingerprints, sort_keys=True)),
            "Published content SHA-256": text(self.content_sha256),
            "Publisher version": text(PUBLISHER_VERSION),
        }
        return values


def build_plan(report: PilotReport) -> PublicationPlan:
    tabs = {
        COMPARISON_TAB: comparison_plan(report),
        HISTORY_TAB: history_plan(report),
        REVIEW_TAB: review_plan(report),
    }
    for plan in tabs.values():
        for row in plan.rows:
            if len(row) != plan.width:
                raise PublicationInputError([f"{plan.title} row width differs from its header"])
    return PublicationPlan(report, tabs, publication_id(report), grid_sha256(list(tabs.values())))


def write_plan_csv(path: Path, plan: TabPlan) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for row in plan.grid():
            writer.writerow([cell.value for cell in row])


# Sheets access (gspread Spreadsheet-compatible object)


def _cell_data(cell: Cell) -> dict:
    data: dict = {}
    text_format: dict = {}
    if cell.kind == "text":
        data["userEnteredValue"] = {"stringValue": cell.value}
    elif cell.kind == "number":
        data["userEnteredValue"] = {"numberValue": float(Decimal(cell.value))}
        data["userEnteredFormat"] = {"numberFormat": NUMBER_FORMATS[cell.number_format]}
    if cell.link:
        text_format["link"] = {"uri": cell.link}
    if cell.bold:
        text_format["bold"] = True
    if text_format:
        data.setdefault("userEnteredFormat", {})["textFormat"] = text_format
    return data


@dataclass(frozen=True)
class TabState:
    title: str
    exists: bool
    sheet_id: int | None = None
    row_count: int = 0
    column_count: int = 0
    grid: tuple[tuple[dict, ...], ...] = ()

    @property
    def used_rows(self) -> int:
        return sum(1 for row in self.grid if any(_has_content(cell) for cell in row))

    def header(self) -> list[str]:
        if not self.grid:
            return []
        return [_cell_text(cell) for cell in self.grid[0]]


def _has_content(cell: Mapping) -> bool:
    return bool(cell.get("userEnteredValue"))


def _cell_text(cell: Mapping) -> str:
    value = cell.get("userEnteredValue") or {}
    if "stringValue" in value:
        return value["stringValue"]
    if "numberValue" in value:
        return repr(value["numberValue"])
    return str(next(iter(value.values()), "")) if value else ""


def read_tab(spreadsheet, title: str) -> TabState:
    """Read one destination tab (values, links, formats); a missing tab is reported, not made."""
    assert_publisher_tab(title)
    found = next((ws for ws in spreadsheet.worksheets() if ws.title == title), None)
    if found is None:
        return TabState(title, False)
    metadata = spreadsheet.fetch_sheet_metadata(
        params={"ranges": f"'{title}'", "includeGridData": "true", "fields": READ_FIELDS}
    )
    sheet = next(s for s in metadata["sheets"] if s["properties"]["title"] == title)
    rows: list[tuple[dict, ...]] = []
    for block in sheet.get("data", []):
        for row in block.get("rowData", []):
            rows.append(tuple(row.get("values", [])))
    grid_props = sheet["properties"].get("gridProperties", {})
    return TabState(
        title,
        True,
        sheet["properties"]["sheetId"],
        grid_props.get("rowCount", 0),
        grid_props.get("columnCount", 0),
        tuple(rows),
    )


def check_ownership(state: TabState, expected_header: Sequence[str]) -> str:
    """'' when the tab is missing, empty, or carries exactly this publisher's header."""
    if not state.exists or state.used_rows == 0:
        return ""
    header = state.header()
    for known in (tuple(expected_header), *EARLIER_HEADERS.get(state.title, ())):
        if header[: len(known)] == list(known) and not any(header[len(known):]):
            return ""
    return f"{state.title} has content that this publisher did not write"


def tab_requests(state: TabState, plan_grid: Sequence[Sequence[Cell]], width: int) -> list[dict]:
    """Requests that replace this tab's publisher content, keep other formatting, and add a
    frozen header and filter. Every request targets only this tab's sheet ID."""
    sheet_id = state.sheet_id
    rows = len(plan_grid)
    requests: list[dict] = []
    if rows > state.row_count or width > state.column_count:
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {
                            "rowCount": max(rows + 1, state.row_count),
                            "columnCount": max(width, state.column_count),
                        },
                    },
                    "fields": "gridProperties.rowCount,gridProperties.columnCount",
                }
            }
        )
    requests += [
        # Clear values, number formats, links and bold on the whole tab; other formatting stays.
        {"updateCells": {"range": {"sheetId": sheet_id}, "fields": CELL_FIELDS}},
        {
            "updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
                "rows": [{"values": [_cell_data(c) for c in row]} for row in plan_grid],
                "fields": CELL_FIELDS,
            }
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": width,
                    }
                }
            }
        },
    ]
    return requests


def guard_requests(requests: Sequence[Mapping], allowed_sheet_ids: set[int]) -> None:
    """Refuse any request that could touch a sheet outside the publisher tabs."""

    def sheet_ids(node: object) -> list[int]:
        found = []
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key == "sheetId":
                    found.append(value)
                else:
                    found += sheet_ids(value)
        elif isinstance(node, list):
            for item in node:
                found += sheet_ids(item)
        return found

    for request in requests:
        ids = sheet_ids(request)
        if not ids or any(sheet_id not in allowed_sheet_ids for sheet_id in ids):
            raise DestinationError("A write request targets a sheet outside the publisher tabs.")
        if set(request) - {"updateCells", "updateSheetProperties", "setBasicFilter"}:
            raise DestinationError("A write request uses an unexpected operation.")


def verify_tab(state: TabState, plan_grid: Sequence[Sequence[Cell]]) -> list[str]:
    """Compare a read-back tab with the planned cells: values, links, PHP formats, extras."""
    issues: list[str] = []
    for r, planned_row in enumerate(plan_grid):
        actual_row = state.grid[r] if r < len(state.grid) else ()
        for c, planned in enumerate(planned_row):
            actual = actual_row[c] if c < len(actual_row) else {}
            where = f"{state.title} row {r + 1} column {c + 1}"
            value = actual.get("userEnteredValue") or {}
            if planned.kind == "blank":
                if value:
                    issues.append(f"{where} should be blank")
            elif planned.kind == "text":
                if value.get("stringValue") != planned.value:
                    issues.append(f"{where} text differs or is missing")
            elif "numberValue" not in value or Decimal(repr(value["numberValue"])) != Decimal(
                planned.value
            ):
                issues.append(f"{where} number differs or is missing")
            elif planned.number_format == "php" and (
                actual.get("userEnteredFormat", {}).get("numberFormat", {}).get("type")
                != "CURRENCY"
            ):
                issues.append(f"{where} lost its PHP format")
            link = actual.get("hyperlink") or actual.get("userEnteredFormat", {}).get(
                "textFormat", {}
            ).get("link", {}).get("uri", "")
            if (link or "") != planned.link:
                issues.append(f"{where} link differs")
        for c in range(len(planned_row), len(actual_row)):
            if _has_content(actual_row[c]):
                issues.append(f"{state.title} row {r + 1} has an unexpected extra cell")
    for r in range(len(plan_grid), len(state.grid)):
        if any(_has_content(cell) for cell in state.grid[r]):
            issues.append(f"{state.title} row {r + 1} has unexpected content")
    return issues


def existing_run_log_rows(state: TabState) -> list[list[Cell]]:
    """Earlier run-log rows, kept as written (text or number), excluding the header."""
    rows = []
    for row in state.grid[1:]:
        cells = []
        for cell in row[: len(RUN_LOG_HEADER)]:
            value = cell.get("userEnteredValue") or {}
            if "numberValue" in value:
                cells.append(Cell("number", repr(value["numberValue"]), "", "integer"))
            elif "stringValue" in value:
                cells.append(Cell("text", value["stringValue"]))
            else:
                cells.append(BLANK)
        cells += [BLANK] * (len(RUN_LOG_HEADER) - len(cells))
        if any(c.kind != "blank" for c in cells):
            rows.append(cells)
    return rows


def run_log_grid(
    plan: PublicationPlan, earlier: Sequence[Sequence[Cell]], values: Mapping[str, Cell]
) -> list[list[Cell]]:
    """Earlier rows keyed by publication ID; this publication's row is replaced, not appended."""
    kept = [list(row) for row in earlier if row[0].value != plan.publication_id]
    header = [text(name, bold=True) for name in RUN_LOG_HEADER]
    return [header, *kept, [values[name] for name in RUN_LOG_HEADER]]


def earlier_attempts(earlier: Sequence[Sequence[Cell]], publication: str) -> tuple[str, int]:
    for row in earlier:
        if row[0].value == publication:
            first = row[RUN_LOG_HEADER.index("First published (UTC)")].value
            attempts = row[RUN_LOG_HEADER.index("Publication attempts")].value
            try:
                return first, int(Decimal(attempts or "0"))
            except InvalidOperation:
                return first, 0
    return "", 0


@dataclass
class PublicationOutcome:
    status: str
    tabs_written: list[str]
    readback_issues: list[str]
    tab_ids: dict[str, int]
    error: str = ""


def publish(
    spreadsheet,
    plan: PublicationPlan,
    *,
    published_at: datetime,
    backup_dir: Path,
    progress=None,
) -> PublicationOutcome:
    """Back up, write, read back, and log. ``SUCCESS`` only after every check passes."""

    def step(message: str) -> None:
        if progress is not None:
            progress(message)

    step("inspecting destination tabs")
    states = {title: read_tab(spreadsheet, title) for title in PUBLISHER_TABS}
    headers = {title: plan.tabs[title].header for title in DATA_TABS} | {
        RUN_LOG_TAB: RUN_LOG_HEADER
    }
    problems = [p for t in PUBLISHER_TABS if (p := check_ownership(states[t], headers[t]))]
    if problems:
        raise DestinationError("; ".join(problems))

    # Back up every existing destination tab locally before any change.
    backup_dir.mkdir(parents=True, exist_ok=True)
    for title, state in states.items():
        if state.exists and state.used_rows:
            (backup_dir / f"backup_{title}.json").write_text(
                json.dumps({"title": title, "sheet_id": state.sheet_id,
                            "rows": [list(r) for r in state.grid]}, ensure_ascii=False),
                encoding="utf-8",
            )

    for title in PUBLISHER_TABS:
        if not states[title].exists:
            assert_publisher_tab(title)
            spreadsheet.add_worksheet(title=title, rows=100, cols=40)
            states[title] = read_tab(spreadsheet, title)
    allowed = {states[t].sheet_id for t in PUBLISHER_TABS}
    tab_ids = {t: states[t].sheet_id for t in PUBLISHER_TABS}

    earlier = existing_run_log_rows(states[RUN_LOG_TAB])
    first, attempts = earlier_attempts(earlier, plan.publication_id)
    first = first or published_at.astimezone(UTC).isoformat(timespec="seconds")

    def log(status: str, written: Sequence[str], readback: str, notes: str = "") -> list:
        values = plan.run_log_values(
            status=status,
            published_at=published_at,
            first_published=first,
            attempts=attempts + 1,
            tabs_written=written,
            readback=readback,
            notes=notes,
        )
        grid = run_log_grid(plan, earlier, values)
        write(RUN_LOG_TAB, grid, len(RUN_LOG_HEADER))
        return grid

    def write(title: str, grid: Sequence[Sequence[Cell]], width: int) -> None:
        assert_publisher_tab(title)
        requests = tab_requests(states[title], grid, width)
        guard_requests(requests, allowed)
        step(f"writing {title} ({len(grid) - 1} data rows)")
        spreadsheet.batch_update({"requests": requests})

    written: list[str] = []
    try:
        log("IN_PROGRESS", written, "NOT_RUN", "Publication started; data tabs pending.")
        for title in DATA_TABS:
            write(title, plan.tabs[title].grid(), plan.tabs[title].width)
            written.append(title)
    except Exception as exc:  # Record what happened, then report partial failure.
        error = f"{type(exc).__name__}: {exc}"
        try:
            log("PARTIAL_FAILURE", written, "NOT_RUN",
                "Rerun the same --write-sheets command; it replaces all four tabs.")
        except Exception as log_exc:  # pragma: no cover - reported to the caller
            error += f"; run-log update also failed ({type(log_exc).__name__})"
        return PublicationOutcome("PARTIAL_FAILURE", written, [], tab_ids, error)

    issues = []
    for title in DATA_TABS:
        step(f"reading back {title}")
        issues += verify_tab(read_tab(spreadsheet, title), plan.tabs[title].grid())
    status = "SUCCESS" if not issues else "READBACK_FAILED"
    readback = "PASSED" if not issues else f"FAILED ({len(issues)} issue(s))"
    try:
        grid = log(status, written, readback,
                   "" if not issues else "Readback found differences; see the local report.")
        step(f"reading back {RUN_LOG_TAB}")
        issues += verify_tab(read_tab(spreadsheet, RUN_LOG_TAB), grid)
    except Exception as exc:
        return PublicationOutcome("PARTIAL_FAILURE", written, issues, tab_ids,
                                  f"Run-log update failed: {type(exc).__name__}: {exc}")
    if issues:
        status = "READBACK_FAILED"
    else:
        written.append(RUN_LOG_TAB)
    return PublicationOutcome(status, written, issues, tab_ids)
