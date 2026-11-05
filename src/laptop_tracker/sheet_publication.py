"""Manual Google Sheets publication of reviewed Phase 10 pilot price reports.

The publisher reads saved, reviewed report files only. It never fetches retailer pages, reruns
matching, imports observations, or opens the observation store. It writes exactly six
publisher-owned tabs (``PUBLISHER_TABS``) and refuses any other destination, including ``raw``,
the catalog tabs, and the legacy ``P1_*`` outputs. ``PILOT_PRICE_SUMMARY`` (2026-10-20) is a
minimal price grid built from the detailed comparison's own cells, with context in cell notes.
``PILOT_PRICE_OVERVIEW`` (pilot-sheets-6, 2026-10-22) is a rank/spec/price view built from the
same cells. A pre-existing, owner-built overview is replaced only when the caller explicitly
adopts it (``--adopt-overview``), after a full local snapshot that ``--rollback`` can restore.
The publisher also applies one house layout (header colours, widths, banding, frozen panes) on
every write, so a publication reproduces the owner's look instead of resetting it.

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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .alternatives import DIMENSIONS, alternative_tag
from .price_tracking import (
    ALTERNATIVE_COLUMNS,
    ALTERNATIVE_SELECTED,
    CANDIDATE_NOT_CONFIRMED,
    CLOSE_ALTERNATIVE_REVIEW,
    COMPARABLE,
    COMPARISON_COLUMNS,
    EVALUATION_COLUMNS,
    HISTORY_COLUMNS,
    LEGACY_COMPARISON_COLUMNS,
    LEGACY_HISTORY_COLUMNS,
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

PUBLISHER_VERSION = "pilot-sheets-7"
COMPARISON_TAB = "PILOT_PRICE_COMPARISON"
# The minimal side-by-side price grid (pilot-sheets-5, 2026-10-20): the detailed comparison's
# own selected prices, one row per configuration, context in cell notes.
SUMMARY_TAB = "PILOT_PRICE_SUMMARY"
# Rank, specifications and prices in twelve columns (pilot-sheets-6, 2026-10-22): the detailed
# comparison's own cells, one row per configuration in comparison-rank order.
OVERVIEW_TAB = "PILOT_PRICE_OVERVIEW"
# One row per target-retailer-offer association that qualifies as a close alternative
# (pilot-sheets-7, 2026-10-27): at most two established differences, clearly labelled.
ALTERNATIVES_TAB = "PILOT_PRICE_ALTERNATIVES"
HISTORY_TAB = "PILOT_PRICE_HISTORY"
REVIEW_TAB = "PILOT_PRICE_REVIEW"
RUN_LOG_TAB = "PILOT_PRICE_RUN_LOG"
# The exact destination allowlist. Names are compared exactly: no trimming or case folding.
PUBLISHER_TABS: tuple[str, ...] = (
    COMPARISON_TAB, SUMMARY_TAB, OVERVIEW_TAB, ALTERNATIVES_TAB, HISTORY_TAB, REVIEW_TAB,
    RUN_LOG_TAB)
DATA_TABS: tuple[str, ...] = (
    COMPARISON_TAB, SUMMARY_TAB, OVERVIEW_TAB, ALTERNATIVES_TAB, HISTORY_TAB, REVIEW_TAB)
# Tabs whose cells carry notes and italics: their field mask, clearing and readback include them.
NOTE_TABS: tuple[str, ...] = (SUMMARY_TAB, OVERVIEW_TAB, ALTERNATIVES_TAB,
                              "TOP_100_SHOPEE_VARIATIONS")

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
    "CPU_EXACT_PROCESSOR_UNKNOWN": "exact processor not stated; CPU compared by series only",
    "GPU_UNKNOWN": "GPU unknown on the target or the retailer (not compared)",
    "SPECIFIC_MODEL_UNKNOWN": "specific model unknown (compared at family level)",
    "SECONDARY_WARNING_OS": "operating system differs",
    "SECONDARY_WARNING_COLOR": "colour differs",
}
NUMBER_FORMATS = {
    "php": {"type": "CURRENCY", "pattern": '"₱"#,##0.00'},
    "integer": {"type": "NUMBER", "pattern": "0"},
    "decimal": {"type": "NUMBER", "pattern": "0.0"},
}
PHP_LABEL_PREFIX = "php|"


def labelled_php(label: str) -> str:
    """The number-format key of a PHP price that displays a short text label after the amount
    (``₱19,990.00 · MATCH``). The cell stays numeric; only its display carries the label, so
    sorting, filtering and sums still see the price. Blank cells have no amount to label."""
    cleaned = label.replace('"', "").replace("\n", " · ")
    key = f"{PHP_LABEL_PREFIX}{cleaned}"
    NUMBER_FORMATS.setdefault(
        key, {"type": "CURRENCY", "pattern": f'"₱"#,##0.00" · {cleaned}"'})
    return key


def is_php(number_format: str) -> bool:
    return number_format == "php" or number_format.startswith(PHP_LABEL_PREFIX)
CELL_FIELDS = (
    "userEnteredValue,userEnteredFormat.numberFormat,"
    "userEnteredFormat.textFormat.link,userEnteredFormat.textFormat.bold"
)
# NOTE_TABS also clear, write and verify notes and italics (other tabs keep any notes people add).
NOTE_CELL_FIELDS = CELL_FIELDS + ",userEnteredFormat.textFormat.italic,note"
READ_FIELDS = (
    "sheets(properties(sheetId,title,gridProperties(rowCount,columnCount,frozenRowCount,"
    "frozenColumnCount)),columnGroups,conditionalFormats,"
    "data(startRow,startColumn,rowData(values(userEnteredValue,hyperlink,note,"
    "userEnteredFormat(numberFormat,backgroundColor,textFormat(link,bold,italic))))))"
)


class PublicationInputError(ValueError):
    """The reports or scope are not consistent; nothing is published."""

    def __init__(self, issues: Sequence[str]) -> None:
        self.issues = tuple(issues)
        super().__init__(f"{len(self.issues)} input issue(s): " + "; ".join(self.issues[:15]))


class DestinationError(RuntimeError):
    """A destination tab is not safe to write (not allowlisted, or not publisher-owned)."""


# Tabs the owner retired on 2026-10-27 (shopee-variation-sheets-1): no publisher may write or
# recreate them, and no allowlist may name them.
RETIRED_TABS: tuple[str, ...] = (SUMMARY_TAB, "P1_TOP_100_CONFIGURATIONS")
# Present only in a workbook prepared for the Shopee-variation grain.
VARIATION_WORKBOOK_MARKER = "TOP_100_SHOPEE_VARIATIONS"


def assert_publisher_tab(title: str, allowed: Sequence[str] = PUBLISHER_TABS) -> None:
    if title not in allowed:
        raise DestinationError(f"Tab {title!r} is not a pilot publisher destination.")


def is_variation_workbook(spreadsheet) -> bool:
    """True for a workbook prepared for the Shopee-variation grain (its Top list tab exists)."""
    return any(ws.title == VARIATION_WORKBOOK_MARKER for ws in spreadsheet.worksheets())


def refuse_variation_workbook(spreadsheet) -> None:
    """The configuration-grain (pilot-sheets-7) publisher never writes a variation workbook: its
    outputs are the wrong grain for the new templates and it would recreate a retired tab."""
    if is_variation_workbook(spreadsheet):
        raise DestinationError(
            "This workbook uses the Shopee-variation grain (shopee-variation-sheets-1); legacy "
            "configuration outputs are not published into it. Use publish_variation_prices.py. "
            f"Retired tabs ({', '.join(RETIRED_TABS)}) are never recreated."
        )


# Cells and tab plans


@dataclass(frozen=True)
class Cell:
    kind: str = "blank"  # "text", "number", or "blank"
    value: str = ""  # exact text; numbers as exact decimal text
    link: str = ""
    number_format: str = ""
    bold: bool = False
    # A cell note (pilot-sheets-5, PILOT_PRICE_SUMMARY only): context kept out of the visible
    # grid. A blank cell may carry a note that explains the blank.
    note: str = ""
    italic: bool = False
    # A comparison-status fill (pilot-sheets-7): "match", "alt1", "alt2" or "recheck" (see
    # ``STATUS_FILLS``); colour is never the only signal, the tag text and note say the same.
    fill: str = ""

    def plain(self) -> list[str]:
        plain = [self.kind, self.value, self.link, self.number_format]
        # Notes, italics and fills are part of the published content (and its SHA-256) when
        # present.
        if self.note or self.italic or self.fill:
            plain += [self.note, "italic" if self.italic else ""]
        if self.fill:
            plain.append(self.fill)
        return plain


BLANK = Cell()


def text(value: object, *, link: str = "", bold: bool = False, note: str = "",
         fill: str = "") -> Cell:
    value = "" if value is None else str(value)
    if not value:
        return Cell(note=note, fill=fill) if note or fill else BLANK
    return Cell("text", value, link, "", bold, note, fill=fill)


def number(value: str, number_format: str, *, note: str = "", italic: bool = False,
           fill: str = "") -> Cell:
    """A numeric cell from exact decimal text; blank text stays blank, never zero."""
    if value == "":
        return Cell(note=note, fill=fill) if note or fill else BLANK
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise PublicationInputError([f"{value!r} is not a decimal number"]) from None
    if not parsed.is_finite():
        raise PublicationInputError(["a number is not finite"])
    return Cell("number", value, "", number_format, note=note, italic=italic, fill=fill)


@dataclass(frozen=True)
class TabPlan:
    title: str
    header: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]
    # Header-cell notes, aligned with ``header`` (empty: no notes).
    header_notes: tuple[str, ...] = ()
    # Banner rows above the header (reporting week, legend), each as wide as the header. They
    # are not part of the sortable table: the filter starts at the header row.
    preface: tuple[tuple[Cell, ...], ...] = ()

    @property
    def header_row(self) -> int:
        """Zero-based sheet row of the header (0 unless banner rows precede it)."""
        return len(self.preface)

    def grid(self) -> list[list[Cell]]:
        notes = self.header_notes or ("",) * len(self.header)
        return [*(list(r) for r in self.preface),
                [text(name, bold=True, note=note) for name, note in zip(self.header, notes,
                                                                       strict=True)],
                *(list(r) for r in self.rows)]

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
OPTIONAL_REPORT_FILES = {"rechecks": "recheck_candidates.csv",
                         "ledger": "configuration_source_ledger.csv",
                         # Close alternatives (2026-10-27): reports saved before have neither.
                         "alternatives": "weekly_price_alternatives.csv",
                         "evaluations": "alternative_evaluations.csv"}
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
    # Selected offers priced in several targets' cells (shared_offers), for review.
    shared: dict[str, list[str]] = field(default_factory=dict)
    # Close-alternative associations of the pilot scope (weekly_price_alternatives.csv; absent
    # before 2026-10-27) and every evaluation (alternative_evaluations.csv, audit only).
    alternatives: list[dict[str, str]] = field(default_factory=list)
    evaluations: list[dict[str, str]] = field(default_factory=list)

    def displayed_alternative(self, target_id: str, code: str) -> dict[str, str] | None:
        """The alternative a retailer cell shows (``displayed_in_comparison``), if any."""
        return next((row for row in self.alternatives
                     if row["target_id"] == target_id and row["retailer_code"] == code
                     and row["displayed_in_comparison"] == "TRUE"), None)

    def alternatives_of(self, target_id: str, code: str) -> list[dict[str, str]]:
        return [row for row in self.alternatives
                if row["target_id"] == target_id and row["retailer_code"] == code]

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
        # Reports saved before 2026-10-27 lack the close-alternative columns: blank, never
        # invented.
        history = [
            dict.fromkeys(HISTORY_COLUMNS, "") | row for row in read_csv_rows(
                paths["history"], LEGACY_HISTORY_COLUMNS, "History report")
        ]
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
    alternatives_path = report_dir / OPTIONAL_REPORT_FILES["alternatives"]
    evaluations_path = report_dir / OPTIONAL_REPORT_FILES["evaluations"]
    all_alternatives: list[dict[str, str]] = []
    all_evaluations: list[dict[str, str]] = []
    try:
        if alternatives_path.is_file():
            all_alternatives = read_csv_rows(alternatives_path, ALTERNATIVE_COLUMNS,
                                             "Alternatives report")
            fingerprints[f"report/{alternatives_path.name}"] = sha256_file(alternatives_path)
        if evaluations_path.is_file():
            all_evaluations = read_csv_rows(evaluations_path, EVALUATION_COLUMNS,
                                            "Alternative evaluations")
            fingerprints[f"report/{evaluations_path.name}"] = sha256_file(evaluations_path)
    except VerifiedRankingInputError as exc:
        raise PublicationInputError([str(exc)]) from None

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

    # Close alternatives of the pilot scope; every row is checked against the history report.
    alternatives = [row for row in all_alternatives if row["target_id"] in scope_set]
    evaluations = [row for row in all_evaluations if row["target_id"] in scope_set]
    issues += _alternative_issues(alternatives, by_observation, start_utc, end_utc, as_of)
    alternative_index: dict[str, dict[str, dict[str, str]]] = {}
    for alt in alternatives:
        alternative_index.setdefault(alt["target_id"], {})[alt["observation_id"]] = alt
    alternative_observations = {alt["observation_id"] for alt in alternatives}

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
            if status not in {SELECTED, ALTERNATIVE_SELECTED, NO_COMPARABLE_OFFER,
                              REVIEW_REQUIRED_ONLY, NO_OBSERVATION_IN_WEEK,
                              *UNPRICED_STATUS_TEXT}:
                issues.append(f"{where} has an unknown price status")
                continue
            if status == ALTERNATIVE_SELECTED:
                issues += _alternative_cell_issues(
                    row, code, selection, by_observation, alternative_index.get(target_id, {}),
                    start_utc, end_utc, as_of)
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
        issues += _alternative_minimum_issues(row, alternative_index.get(target_id, {}))

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
        if listed & scope_set or row["observation_id"] in alternative_observations:
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
    snapshot_date = str(sales.get("extract_date") or (scope[0].get("sales_extract_date", "")
                                                         if scope else ""))
    issues += _shopee_issues(report_dir, pilot_comparison, snapshot_date, fingerprints)
    if issues:
        raise PublicationInputError(sorted(set(issues), key=issues.index))

    week_history = [r for r in pilot_history if r["report_week_start"] == week["start"]]
    cells = [(row, code) for row in pilot_comparison for code, _ in RETAILERS]
    shared = shared_offers(selection_index.values(), scope_set)
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
        "shopee_snapshot_prices": sum(1 for r in pilot_comparison if r.get("Shopee_Price")),
        "shopee_without_snapshot_price": sum(
            1 for r in pilot_comparison if not r.get("Shopee_Price")),
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
        # One retailer offer priced in several configurations' cells (e.g. an exact match for a
        # code target and a listed-spec match for a family target with the same specifications).
        # Allowed by the listed-spec policy, but a possible duplicate target: owner review.
        "offers_pricing_several_targets": len(shared),
        # Close alternatives (2026-10-27); the matching counts above never include them.
        "alternative_prices": sum(
            1 for r, c in cells if r[f"{c}_Price_Status"] == ALTERNATIVE_SELECTED),
        "alternative_prices_alt_1": sum(
            1 for r, c in cells if r[f"{c}_Price_Status"] == ALTERNATIVE_SELECTED
            and r.get(f"{c}_Alternative_Level") == "1"),
        "alternative_prices_alt_2": sum(
            1 for r, c in cells if r[f"{c}_Price_Status"] == ALTERNATIVE_SELECTED
            and r.get(f"{c}_Alternative_Level") == "2"),
        "alternative_rows": len(alternatives),
        "alternative_rows_eligible": sum(
            1 for a in alternatives if a["price_eligibility"] == COMPARABLE),
        "alternative_recheck_cells": sum(
            1 for r, c in cells if r.get(f"{c}_Comparison_Tag") == "RECHECK"),
        "configurations_with_alternative": sum(
            1 for r in pilot_comparison if r.get("Alternative_Retailer_Count") not in {"", "0"}),
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
        shared=shared,
        alternatives=alternatives,
        evaluations=evaluations,
    )


def _decimal_or_none(value: str) -> Decimal | None:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount > 0 else None


def _alternative_issues(
    alternatives: Sequence[Mapping[str, str]],
    by_observation: Mapping[str, Mapping[str, str]],
    start_utc: datetime,
    end_utc: datetime,
    as_of: datetime,
) -> list[str]:
    """Every alternative association must be internally consistent and backed by the history
    report: its observation is in the week and not after the cutoff, its price and link are the
    observation's own, 1 or 2 differences name that many differing dimensions, and its tag says
    the same."""
    issues: list[str] = []
    seen: set[tuple[str, str]] = set()
    for alt in alternatives:
        where = (f"{alt['target_id']} {alt['retailer_code']} alternative "
                 f"{alt['observation_id'][:12]}")
        key = (alt["target_id"], alt["observation_id"])
        if key in seen:
            issues.append(f"{where} is repeated")
        seen.add(key)
        observed = by_observation.get(alt["observation_id"])
        if observed is None:
            issues.append(f"{where} has no history observation")
        else:
            if (observed["price"], observed["offer_url"]) != (alt["price"], alt["offer_url"]):
                issues.append(f"{where} price or link is not the observation's own")
            if observed["retrieved_at_utc"] != alt["retrieved_at_utc"]:
                issues.append(f"{where} observation time differs from the history report")
        try:
            retrieved = _utc(alt["retrieved_at_utc"])
        except ValueError:
            issues.append(f"{where} has no valid observation time")
        else:
            if not (start_utc <= retrieved < end_utc and retrieved <= as_of):
                issues.append(f"{where} was not observed in the report week")
        if _decimal_or_none(alt["price"]) is None:
            issues.append(f"{where} price is not a positive decimal")
        if alt["price_eligibility"] not in {COMPARABLE, "NOT_COMPARABLE"}:
            issues.append(f"{where} has an unknown price eligibility")
        dimensions = [d for d in alt["differing_dimensions"].split(";") if d]
        if (alt["difference_count"] not in {"1", "2"}
                or alt["difference_count"] != str(len(dimensions))
                or len(set(dimensions)) != len(dimensions)
                or not set(dimensions) <= set(DIMENSIONS)):
            issues.append(f"{where} difference count does not match its differing dimensions")
        elif alt["alternative_tag"] != alternative_tag(len(dimensions), dimensions):
            issues.append(f"{where} tag does not match its differences")
        if alt["displayed_in_comparison"] not in {"TRUE", "FALSE"}:
            issues.append(f"{where} has an unknown display flag")
    return issues


def _alternative_cell_issues(
    row: Mapping[str, str],
    code: str,
    selection: Mapping[str, str],
    by_observation: Mapping[str, Mapping[str, str]],
    alternatives: Mapping[str, Mapping[str, str]],
    start_utc: datetime,
    end_utc: datetime,
    as_of: datetime,
) -> list[str]:
    """A cell that shows a close alternative: the selections row supplies the same price and
    link, the offer is a comparable alternative association displayed for that cell, observed in
    the week, and the cell's tag agrees with it."""
    where = f"{row['target_id']} {code}"
    issues = []
    price, url = row[f"{code}_Price"], row[f"{code}_Offer_URL"]
    if _decimal_or_none(price) is None:
        return [f"{where} alternative price is not a positive decimal"]
    if (price, url) != (selection["price"], selection["offer_url"]):
        issues.append(f"{where} alternative price or link is not the offer that supplied it")
    if selection["currency"] != REPORTING_CURRENCY:
        issues.append(f"{where} alternative price is not PHP")
    try:
        retrieved = _utc(selection["retrieved_at_utc"])
    except ValueError:
        return [*issues, f"{where} alternative has no valid observation time"]
    if not (start_utc <= retrieved < end_utc and retrieved <= as_of):
        issues.append(f"{where} alternative price was not observed in the report week")
    alt = alternatives.get(selection["observation_id"])
    if (alt is None or alt["retailer_code"] != code or alt["price_eligibility"] != COMPARABLE
            or alt["displayed_in_comparison"] != "TRUE"
            or (alt["price"], alt["offer_url"]) != (price, url)):
        issues.append(f"{where} alternative price is not a comparable, displayed alternative "
                      "association")
    elif row.get(f"{code}_Comparison_Tag") != alt["alternative_tag"] or (
            selection.get("alternative_tag") not in {"", alt["alternative_tag"]}):
        issues.append(f"{where} tag differs from its alternative association")
    if by_observation.get(selection["observation_id"]) is None:
        issues.append(f"{where} alternative has no history observation")
    return issues


def _alternative_minimum_issues(
    row: Mapping[str, str], alternatives: Mapping[str, Mapping[str, str]]
) -> list[str]:
    """The alternative counts and minimums must be those of the full eligible alternative pool
    in the alternatives report: each retailer once, hidden offers included, out-of-stock offers
    never in the in-stock minimum. Reports without the columns (earlier than 2026-10-27) skip."""
    if not row.get("Comparison_Retailer_Count"):
        return []
    target_id = row["target_id"]
    issues = []
    pool = [a for a in alternatives.values() if a["price_eligibility"] == COMPARABLE]
    for prefix, count_column, pool_rows in (
        ("Lowest_Alternative_Price", "Alternative_Retailer_Count", pool),
        ("Lowest_Alternative_In_Stock_Price", "",
         [a for a in pool if a["availability"] == "IN_STOCK"]),
    ):
        per_retailer: dict[str, Decimal] = {}
        for alt in pool_rows:
            amount = _decimal_or_none(alt["price"])
            if amount is not None and (alt["retailer_code"] not in per_retailer
                                       or amount < per_retailer[alt["retailer_code"]]):
                per_retailer[alt["retailer_code"]] = amount
        lowest = min(per_retailer.values(), default=None)
        stated = row[prefix]
        if (Decimal(stated) if stated else None) != lowest:
            issues.append(f"{target_id} {prefix} disagrees with the alternatives report")
        tied = ";".join(code for code, _ in RETAILERS if per_retailer.get(code) == lowest)
        if row[f"{prefix}_Retailers"] != (tied if lowest is not None else ""):
            issues.append(f"{target_id} {prefix} retailers are wrong")
        if count_column and row[count_column] != str(len(per_retailer)):
            issues.append(f"{target_id} alternative retailer count is wrong")
    shown = sum(1 for code, _ in RETAILERS if row[f"{code}_Price_Status"] in
                {SELECTED, ALTERNATIVE_SELECTED})
    if row["Comparison_Retailer_Count"] != str(shown):
        issues.append(f"{target_id} retailers-shown count is wrong")
    return issues


def shared_offers(
    selections: Iterable[Mapping[str, str]], scope: set[str] | None = None
) -> dict[str, list[str]]:
    """``{"<retailer code> <offer URL>": [target IDs]}`` for selected offers priced in more than
    one configuration's cell (2026-10-22). Reported for review; nothing is changed."""
    by_offer: dict[str, list[str]] = {}
    for row in selections:
        if row.get("price_status") == SELECTED and row.get("offer_url") and (
                scope is None or row["target_id"] in scope):
            by_offer.setdefault(f"{row['retailer_code']} {row['offer_url']}", []).append(
                row["target_id"])
    return {offer: sorted(targets) for offer, targets in by_offer.items() if len(targets) > 1}


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
    details: str = "", tag: str = "", differences: str = "", unknowns: str = "",
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
    elif status == ALTERNATIVE_SELECTED:
        # A close alternative (2026-10-27): the tag, the differences, and a plain statement that
        # it is not a matching configuration.
        text_value = "Alternative price" + (" (OUT OF STOCK)" if availability == "OUT_OF_STOCK"
                                             else "")
        text_value += f" — {tag}: {differences}".rstrip(": ")
        text_value += (" Not a matching configuration; shown because no matching offer is "
                       "comparable at this retailer")
        if unknowns:
            text_value += f"; unknown: {unknowns}"
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


# Close-alternative columns (pilot-sheets-7, 2026-10-27), strictly at the right of the earlier
# layout. Each retailer's cell is tagged (MATCH, ALT 1 · RAM, RECHECK); the minimums below are
# ALTERNATIVES ONLY (the retailer minimums stay matching-only) and are taken from the full
# eligible alternative pool, including offers a retailer's main display does not show.
ALTERNATIVE_MINIMUM_HEADER: tuple[str, ...] = (
    "Lowest alternative price (PHP, alternatives only)",
    "Lowest alternative price retailer(s)",
    "Lowest alternative price tag",
    "Lowest alternative price difference count",
    "Lowest alternative price differences",
    "Lowest alternative price unknown fields",
    "Lowest alternative price stock",
    "Lowest alternative price link",
    "Lowest alternative price observed (Asia/Singapore)",
    "Lowest alternative price observation ID",
    "Lowest in-stock alternative price (PHP, alternatives only)",
    "Lowest in-stock alternative retailer(s)",
    "Lowest in-stock alternative tag",
    "Lowest in-stock alternative difference count",
    "Lowest in-stock alternative differences",
    "Lowest in-stock alternative unknown fields",
    "Lowest in-stock alternative link",
    "Lowest in-stock alternative observed (Asia/Singapore)",
    "Lowest in-stock alternative observation ID",
)
ALTERNATIVE_COMPARISON_HEADER: tuple[str, ...] = (
    *(f"{RETAILER_NAMES[code]} comparison tag" for code, _ in RETAILERS),
    "Alternative retailers (eligible alternatives)",
    "Retailers shown (matching or alternative)",
    *ALTERNATIVE_MINIMUM_HEADER,
)


def comparison_header(
    *, recheck: bool = True, basis: bool = True, shopee: bool = True,
    alternatives: bool = True,
) -> tuple[str, ...]:
    """The comparison tab header. ``recheck=False`` is the pilot-sheets-2 layout (before the
    recheck columns of 2026-10-19), ``basis=False`` the pilot-sheets-3 layout (before the
    matching-basis columns of 2026-10-20) and ``shopee=False`` the pilot-sheets-4 layout (before
    the Shopee snapshot columns and the retailer-minimum headers) and ``alternatives=False`` the
    pilot-sheets-6 layout (before the close-alternative columns of 2026-10-27, appended at the
    right); all are recognised as this publisher's, so the next publication migrates the
    layout."""
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
    if shopee:
        # The configuration's historical Shopee snapshot (sales extract), never a current price.
        header += list(SHOPEE_HEADER)
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
        *(RETAILER_MINIMUM_HEADER if shopee else (
            "Lowest observed price (PHP)",
            "Lowest observed price retailer(s)",
            *(["Lowest observed price basis"] if basis else []),
            "Lowest in-stock price (PHP)",
            "Lowest in-stock retailer(s)",
            *(["Lowest in-stock price basis"] if basis else []),
        )),
        "Comparable retailers",
        *(["Comparison basis"] if basis else []),
        "Shopee sales snapshot (extract date)",
        "Reporting week (Asia/Singapore)",
        "Week status",
        "Report cutoff (Asia/Singapore)",
    ]
    if alternatives:
        header += list(ALTERNATIVE_COMPARISON_HEADER)
    return tuple(header)


# The pilot-sheets-2 and pilot-sheets-3 comparison layouts are this publisher's own too.
EARLIER_HEADERS[COMPARISON_TAB] += (
    comparison_header(recheck=False, basis=False, shopee=False, alternatives=False),
    comparison_header(basis=False, shopee=False, alternatives=False),
    comparison_header(shopee=False, alternatives=False),
)
BASIS_WORDS = {"EXACT_PRODUCT": "exact product match", "LISTED_SPEC": "listed-spec match",
               "MIXED": "exact product and listed-spec matches"}


def _minimum_basis(label: str, basis: str) -> str:
    """'Lowest price matching listed specs — winning offer: listed-spec match'."""
    if not label:
        return ""
    return f"{label} — winning offer: {BASIS_WORDS.get(basis, basis)}" if basis else label


# Shopee snapshot columns (pilot-sheets-5, 2026-10-20). The price is the lowest eligible
# mapped-listing snapshot price of the sales extract (shopee_snapshot.py), dated by that extract.
SHOPEE_HEADER: tuple[str, ...] = (
    "Shopee price (PHP, snapshot)",
    "Shopee listing link",
    "Shopee variation",
    "Shopee price status",
    "Shopee snapshot date",
)
# The existing minimums are retailer minimums: the Shopee snapshot is never part of them.
RETAILER_MINIMUM_HEADER: tuple[str, ...] = (
    "Lowest retailer price (PHP)",
    "Lowest retailer price retailer(s)",
    "Lowest retailer price basis",
    "Lowest in-stock retailer price (PHP)",
    "Lowest in-stock retailer(s)",
    "Lowest in-stock retailer price basis",
)
# The pilot-sheets-6 comparison header (before the close-alternative columns) is this
# publisher's too; it needs the Shopee and minimum headers above.
EARLIER_HEADERS[COMPARISON_TAB] += (comparison_header(alternatives=False),)
SHOPEE_STATUS_TEXT = {
    "SNAPSHOT_PRICE": "Lowest eligible mapped-listing snapshot price",
    "NO_MAPPED_SOURCE": "No snapshot price: no contributing listing is still mapped",
    "NO_VALID_SNAPSHOT_PRICE": "No snapshot price: no valid price in the extract",
    "SALES_RANKING_NOT_SUPPLIED": "No snapshot price: sales ranking not supplied",
    "SHOPEE_SNAPSHOT_NOT_SUPPLIED": "No snapshot price: not supplied to this report",
    "NO_CONTRIBUTING_SOURCE": "No snapshot price: no contributing listing",
}
SHOPEE_NOT_IN_REPORT = "Shopee snapshot not in this report (generated before 2026-10-20)"


def _shopee_cells(row: Mapping[str, str]) -> list[Cell]:
    """Price, product-page link, variation (with model_id), status and snapshot date. A report
    written before the Shopee columns (loaded with those columns blank; a current report always
    records a status) says so instead of inventing values."""
    if not row.get("Shopee_Price_Status"):
        return [BLANK, BLANK, BLANK, text(SHOPEE_NOT_IN_REPORT), text("not in this report")]
    url = row["Shopee_Listing_URL"]
    variation = row["Shopee_Variation"] or row["Shopee_Item_Name"]
    if row["Shopee_Model_ID"]:
        variation = f"{variation} (model_id {row['Shopee_Model_ID']})".strip()
    status = SHOPEE_STATUS_TEXT.get(row["Shopee_Price_Status"], row["Shopee_Price_Status"])
    detail = row["Shopee_Price_Detail"]
    if row["Shopee_Price"]:
        detail = "; ".join(filter(None, (
            detail, "not a current price; stock not verified; the link opens the product page, "
            "not the variation")))
    return [
        number(row["Shopee_Price"], "php"),
        text(url, link=url) if url else BLANK,
        text(variation),
        text(f"{status} ({detail})" if detail else status),
        text(f"{row['Shopee_Snapshot_Date']} (historical Shopee sales extract; "
             f"{row['Shopee_Snapshot_Label']})" if row["Shopee_Snapshot_Date"] else ""),
    ]


def _shopee_issues(
    report_dir: Path,
    comparison: Sequence[Mapping[str, str]],
    snapshot_date: str,
    fingerprints: dict[str, str],
) -> list[str]:
    """The Shopee columns must agree with the sales snapshot and with the report's own source
    ledger: the same selection recomputed, one source row for price, link and variation."""
    from .shopee_snapshot import SNAPSHOT_PRICE, listing_url, select_shopee_prices

    issues: list[str] = []
    rows = [row for row in comparison if row.get("Shopee_Price_Status")]
    if not rows:
        return issues
    if len(rows) != len(comparison):
        issues.append("some configurations have no Shopee snapshot status")
    for row in rows:
        where = f"{row['target_id']} Shopee"
        if row["Shopee_Snapshot_Date"] != snapshot_date:
            issues.append(f"{where} snapshot date differs from the report's sales snapshot")
        if bool(row["Shopee_Price"]) != (row["Shopee_Price_Status"] == SNAPSHOT_PRICE):
            issues.append(f"{where} price and status disagree")
        if row["Shopee_Price"]:
            try:
                amount = Decimal(row["Shopee_Price"])
            except InvalidOperation:
                issues.append(f"{where} price is not a decimal")
                continue
            if not amount.is_finite() or amount <= 0:
                issues.append(f"{where} price is not positive")
            if row["Shopee_Listing_URL"] != listing_url(row["Shopee_Shop_ID"],
                                                        row["Shopee_Item_ID"]):
                issues.append(f"{where} link is not built from the listing's own IDs")
    ledger_path = report_dir / OPTIONAL_REPORT_FILES["ledger"]
    if not ledger_path.is_file():
        return issues
    fingerprints[f"report/{ledger_path.name}"] = sha256_file(ledger_path)
    with ledger_path.open(newline="", encoding="utf-8") as handle:
        ledger = list(csv.DictReader(handle))
    selections, _ = select_shopee_prices(ledger)
    for row in rows:
        selection = selections.get(row["target_id"])
        expected_price = selection.amount if selection else None
        expected_model = selection.source["model_id"] if selection and selection.source else ""
        try:
            shown = Decimal(row["Shopee_Price"]) if row["Shopee_Price"] else None
        except InvalidOperation:
            shown = None
        if (shown, row["Shopee_Model_ID"]) != (expected_price, expected_model):
            issues.append(f"{row['target_id']} Shopee price or listing is not the source ledger's "
                          "lowest eligible mapped listing")
    return issues


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


def cell_tag(row: Mapping[str, str], code: str) -> str:
    """The comparison tag of one retailer cell: MATCH, ``ALT 1 · RAM``, RECHECK or blank. A report
    saved before the tags (2026-10-27) tags every priced cell MATCH."""
    tag = row.get(f"{code}_Comparison_Tag", "")
    if not tag and row.get(f"{code}_Price"):
        return "MATCH"
    return tag


def tag_fill(tag: str, *, priced: bool) -> str:
    """The status fill of a tag: green MATCH, yellow ALT 1, orange ALT 2, gray RECHECK (only
    where no comparison price is shown)."""
    if tag == "MATCH":
        return "match"
    if tag.startswith("ALT 1"):
        return "alt1"
    if tag.startswith("ALT 2"):
        return "alt2"
    return "recheck" if tag == "RECHECK" and not priced else ""


def _named(value: str, separator: str = ";") -> str:
    """``DBZ:ALT 1 · RAM;LF:ALT 2 · …`` -> ``DataBlitz: ALT 1 · RAM; Laptop Factory: …``."""
    parts = []
    for part in value.split(separator):
        code, _, rest = part.strip().partition(":")
        parts.append(f"{RETAILER_NAMES[code]}: {rest.strip()}" if code in RETAILER_NAMES
                     else part.strip())
    joiner = "; " if separator == ";" else separator
    return joiner.join(part for part in parts if part)


def _alternative_minimum_cells(row: Mapping[str, str], prefix: str, *, stock: bool) -> list[Cell]:
    """The cells of one alternative-only minimum group, in ``ALTERNATIVE_MINIMUM_HEADER`` order.
    The offer link is a cell link only when one retailer wins."""
    url = row.get(f"{prefix}_Offer_URL", "")
    cells = [
        number(row.get(prefix, ""), "php"),
        text(_minimum_retailers(row.get(f"{prefix}_Retailers", ""),
                                row.get(f"{prefix}_Notes", ""))),
        text(_named(row.get(f"{prefix}_Tag", ""))),
        text(_named(row.get(f"{prefix}_Difference_Count", ""))),
        text(_named(row.get(f"{prefix}_Differences", ""), " | ")),
        text(_named(row.get(f"{prefix}_Unknowns", ""), " | ")),
    ]
    if stock:
        cells.append(text(row.get(f"{prefix}_Stock", "")))
    cells += [
        text(url, link=url if ";" not in url else ""),
        text(row.get(f"{prefix}_Observed_At", "")),
        text(_named(row.get(f"{prefix}_Observation_IDs", ""))),
    ]
    return cells


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
            *_shopee_cells(row),
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
                                  row.get(f"{code}_Offer_Details", ""),
                                  cell_tag(row, code),
                                  row.get(f"{code}_Alternative_Differences", ""),
                                  row.get(f"{code}_Alternative_Unknowns", ""))),
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
            # Close alternatives (pilot-sheets-7), at the right of the earlier layout.
            *(text(cell_tag(row, code), fill=tag_fill(
                cell_tag(row, code), priced=bool(row[f"{code}_Price"])))
              for code, _ in RETAILERS),
            number(row.get("Alternative_Retailer_Count", ""), "integer"),
            number(row.get("Comparison_Retailer_Count", ""), "integer"),
            *_alternative_minimum_cells(row, "Lowest_Alternative_Price", stock=True),
            *_alternative_minimum_cells(row, "Lowest_Alternative_In_Stock_Price", stock=False),
        ]
        # An alternative's price is filled like its tag (not a matching configuration).
        for code, _ in RETAILERS:
            price_at = header.index(f"{RETAILER_NAMES[code]} price (PHP)")
            fill = tag_fill(cell_tag(row, code), priced=True)
            if fill in {"alt1", "alt2"} and cells[price_at].kind == "number":
                cells[price_at] = replace(cells[price_at], fill=fill)
        rows.append(tuple(cells))
    return TabPlan(COMPARISON_TAB, tuple(header), tuple(rows))


# PILOT_PRICE_SUMMARY (pilot-sheets-5, 2026-10-20): exactly these seven visible columns. Every
# price is the detailed comparison's own cell (build_plan checks it); nothing is reselected,
# rematched or combined, and no minimum mixes the historical Shopee snapshot with retailer
# observations. Context lives in cell notes; listed-spec prices are italic.
SUMMARY_HEADER: tuple[str, ...] = (
    "Laptop", "Shopee", *(RETAILER_NAMES[code] for code, _ in RETAILERS))
SUMMARY_SHOPEE_PRICE = "Shopee price (PHP, snapshot)"
# Nonblocking extraction warnings in words for the summary notes (the code stays in brackets).
NOTE_WARNING_TEXT = {
    "CPU_NUMBER_TITLE_DIFFERS": "the title states a different processor number",
    "COLOR_TITLE_DIFFERS": "the title states a different colour",
    "MODEL_URL_DIFFERS": "the link wording names a different model",
    "MPN_REPEATS_RETAILER_SKU": "the part number repeats the retailer SKU",
    "SHARED_TITLE_DIFFERS_RAM_CAPACITY_GB": "a shared title states different RAM",
    "SHARED_TITLE_DIFFERS_STORAGE_CAPACITY_GB": "a shared title states different storage",
    "SHARED_DESCRIPTION_DIFFERS_RAM_CAPACITY_GB": "a shared description states different RAM",
    "SHARED_DESCRIPTION_DIFFERS_STORAGE_CAPACITY_GB": (
        "a shared description states different storage"),
}
# The colour legend, also written as text: colour is never the only signal.
STATUS_LEGEND = ("Fill: green = MATCH (matching configuration), yellow = ALT 1, orange = ALT 2 "
                 "(close alternatives, not matching configurations), gray = RECHECK (unresolved, "
                 "no comparison price), none = no price. The tag is also written in "
                 f"{COMPARISON_TAB} and in each cell's note.")
DETAIL_TABS_TEXT = (f"Detailed evidence: {COMPARISON_TAB}, {ALTERNATIVES_TAB} and "
                    f"{REVIEW_TAB}.")
_LABEL_TIEBREAK_FIELDS = ("Resolution", "Display Type", "Refresh Rate", "GPU_Model", "RAM_Type",
                          "Storage_Type", "CPU_Generation")


def _capacity_text(value: str) -> str:
    """'512' -> '512GB', '1000' or '1024' -> '1TB'."""
    try:
        amount = int(Decimal(value))
    except (InvalidOperation, ValueError):
        return value
    for unit in (1000, 1024):
        if amount >= unit and amount % unit == 0:
            return f"{amount // unit}TB"
    return f"{amount}GB"


def configuration_name(row: Mapping[str, str]) -> str:
    """'Acer Nitro V ANV15-42-R2UH': brand, series and model, without repeating words already in
    the model."""
    brand, series, model = (row.get(k, "").strip() for k in ("Brand", "Series", "Model"))
    name = [] if model.lower().startswith(brand.lower()) else [brand]
    if series and series.lower() not in model.lower():
        name.append(series)
    name.append(model)
    return " ".join(p for p in name if p)


def configuration_label(row: Mapping[str, str]) -> str:
    """'Acer ANV15-42-R2UH · 15.6″ · Ryzen 5 · 16GB/512GB': brand, series and model (without
    repeating words already in the model), screen, CPU family, installed RAM and storage."""
    screen = row.get("Screen Size", "").strip()
    if screen.endswith(".0"):
        screen = screen[:-2]
    parts = [configuration_name(row), f"{screen}″" if screen else "",
             row.get("CPU_Series", "").strip(),
             "/".join(_capacity_text(row.get(k, "").strip())
                      for k in ("RAM_Capacity", "Storage_Capacity") if row.get(k, "").strip())]
    return " · ".join(p for p in parts if p)


def configuration_labels(rows: Sequence[Mapping[str, str]]) -> list[str]:
    """Labels that distinguish every configuration: a repeated label gains the catalog fields
    that differ (resolution, panel, refresh rate, GPU, …), then its position if still equal."""
    labels = [configuration_label(row) for row in rows]
    groups: dict[str, list[int]] = {}
    for index, label in enumerate(labels):
        groups.setdefault(label, []).append(index)
    for label, indexes in groups.items():
        if len(indexes) < 2:
            continue
        differing = [f for f in _LABEL_TIEBREAK_FIELDS
                     if len({rows[i].get(f, "") for i in indexes}) > 1]
        for i in indexes:
            extra = [rows[i].get(f, "") or f"no {f.lower()}" for f in differing]
            labels[i] = " · ".join([label, *extra])
        repeated: dict[str, list[int]] = {}
        for i in indexes:
            repeated.setdefault(labels[i], []).append(i)
        for same in repeated.values():
            if len(same) > 1:
                for n, i in enumerate(same, start=1):
                    labels[i] += f" · configuration {n} of {len(same)}"
    return labels


def _shopee_summary_note(row: Mapping[str, str]) -> str:
    """Snapshot date, selection, listing, variation and link of the Shopee cell, or why blank."""
    if not row.get("Shopee_Price_Status"):
        return f"{SHOPEE_NOT_IN_REPORT}. {DETAIL_TABS_TEXT}"
    status = SHOPEE_STATUS_TEXT.get(row["Shopee_Price_Status"], row["Shopee_Price_Status"])
    extract = (f"Shopee sales extract of {row['Shopee_Snapshot_Date']} "
               f"({row['Shopee_Snapshot_Label']})" if row["Shopee_Snapshot_Date"]
               else "Shopee sales extract date not recorded")
    if not row["Shopee_Price"]:
        lines = [f"No Shopee snapshot price. {status}.",
                 *([row["Shopee_Price_Detail"]] if row["Shopee_Price_Detail"] else []),
                 f"Snapshot: {extract}."]
    else:
        variation = row["Shopee_Variation"] or "(no variation name)"
        lines = [
            "HISTORICAL SHOPEE SNAPSHOT — not a current price; stock not verified.",
            f"Snapshot: {extract}.",
            f"Rule: {status.lower()} ({row['Shopee_Price_Detail']}).",
            f"Listing: {row['Shopee_Item_Name']}",
            f"Variation: {variation} (model_id {row['Shopee_Model_ID']}; shop_id "
            f"{row['Shopee_Shop_ID']}; item_id {row['Shopee_Item_ID']}).",
            f"Link (opens the product page, not the variation): {row['Shopee_Listing_URL']}",
        ]
    return "\n".join([*lines, DETAIL_TABS_TEXT])


def _retailer_summary_note(
    report: PilotReport, row: Mapping[str, str], code: str, stock: str
) -> str:
    """Basis, stock, bundle, warnings and link of a price; the explanation of a blank."""
    name = RETAILER_NAMES[code]
    warnings = _codes(row[f"{code}_Warnings"])
    tag = cell_tag(row, code)
    if not row[f"{code}_Price"]:
        lines = [f"{name}: no comparable price this week. " + _status_text(
            row[f"{code}_Price_Status"],
            report.selections[(row["target_id"], code)]["not_comparable_reasons"], stock,
            row[f"{code}_Match_Basis"], row[f"{code}_Warnings"],
            row.get(f"{code}_Offer_Details", "")) + "."]
        if tag == "RECHECK":
            lines.insert(0, "RECHECK — unresolved evidence; there is no confirmed comparison "
                         "price for this cell.")
        url = row.get(f"{code}_Candidate_URL", "")
        if url:
            count = int(row.get(f"{code}_Recheck_Count") or 1)
            note = row.get(f"{code}_Recheck_Note") or row.get(f"{code}_Candidate_Detail", "")
            lines.append(f"Recheck (unconfirmed page, not a price): {url}"
                         + (f" — {note}" if note else "")
                         + (f" (+{count - 1} more in {REVIEW_TAB})" if count > 1 else ""))
        return "\n".join([*lines, DETAIL_TABS_TEXT])
    if row[f"{code}_Price_Status"] == ALTERNATIVE_SELECTED:
        lines = [f"{tag} — close alternative, not a matching configuration (at most two "
                 "established differences from the target).",
                 f"Differences: {row.get(f'{code}_Alternative_Differences', '')}"]
        if row.get(f"{code}_Alternative_Unknowns"):
            lines.append(f"Unknown / limited: {row[f'{code}_Alternative_Unknowns']}")
        lines.append(f"Detailed record: {ALTERNATIVES_TAB}, observation "
                     f"{row.get(f'{code}_Alternative_Observation_ID', '')}.")
    elif row[f"{code}_Match_Basis"] == "LISTED_SPEC":
        lines = ["LISTED-SPEC MATCH — not an exact product match: brand, family, screen, CPU "
                 "series, installed RAM and storage agree; the specific Shopee model is unknown."]
        if row.get(f"{code}_Offer_Details"):
            lines.append(f"Offer as listed: {row[f'{code}_Offer_Details']}")
    else:
        detail = ", ".join(BASIS_TEXT[tag] for tag in row[f"{code}_Match_Basis"].split("+")
                           if tag in BASIS_TEXT)
        lines = ["Exact product match" + (f" ({detail})." if detail else ".")]
    stock = row.get(f"{code}_Stock") or stock
    lines.append({
        "IN_STOCK": "Stock: in stock.",
        "OUT_OF_STOCK": "Stock: OUT OF STOCK (price kept under the owner-approved policy).",
    }.get(stock, f"Stock: {REASON_TEXT.get('AVAILABILITY_' + stock, stock or 'not stated')}."))
    if "BUNDLE_OFFER" in warnings:
        lines.append("Bundle offer (price kept under the owner-approved policy).")
    disagreements = [w for w in warnings if w.startswith("OFFERS_DISAGREE_")]
    if disagreements:
        lines.append("Warning: " + _reason_text(disagreements) + ".")
    limits = {"CPU_EXACT_PROCESSOR_UNKNOWN", "GPU_UNKNOWN", "SPECIFIC_MODEL_UNKNOWN"}
    other = [w for w in warnings if w != "BUNDLE_OFFER" and w not in disagreements
             and w != "SPECIFIC_IDENTITY_UNRESOLVED"
             and not (row[f"{code}_Price_Status"] == ALTERNATIVE_SELECTED and w in limits)]
    if other:
        lines.append("Notes: " + ", ".join(
            REASON_TEXT.get(w) or (f"{NOTE_WARNING_TEXT[w]} ({w})" if w in NOTE_WARNING_TEXT
                                   else w) for w in other) + ".")
    lines += [f"Observed: {row[f'{code}_Observed_At']} (Asia/Singapore).",
              f"Offer: {row[f'{code}_Offer_URL']}"]
    return "\n".join([*lines, DETAIL_TABS_TEXT])


def summary_plan(report: PilotReport) -> TabPlan:
    """One row per configuration, in the detailed comparison's order and scope."""
    stock_by_observation = {row["observation_id"]: row["availability"] for row in report.history}
    week = (f"{report.week_start.isoformat()} to "
            f"{(report.week_end - timedelta(days=1)).isoformat()}")
    sales = report.summary["sales_snapshot"]
    extract = str(sales.get("extract_date") or next(
        (r.get("Shopee_Snapshot_Date", "") for r in report.comparison), ""))
    rows = []
    for row, label in zip(report.comparison, configuration_labels(report.comparison),
                          strict=True):
        cells = [text(label),
                 number(row.get("Shopee_Price", ""), "php", note=_shopee_summary_note(row))]
        for code, _ in RETAILERS:
            selection = report.selections[(row["target_id"], code)]
            stock = stock_by_observation.get(selection["observation_id"], "")
            cells.append(number(row[f"{code}_Price"], "php",
                                note=_retailer_summary_note(report, row, code, stock),
                                italic=row[f"{code}_Match_Basis"] == "LISTED_SPEC",
                                fill=tag_fill(cell_tag(row, code),
                                              priced=bool(row[f"{code}_Price"]))))
        rows.append(tuple(cells))
    snapshot = (f"the Shopee sales extract of {extract} (sales run {sales.get('run_id', '')}; "
                f"{sales.get('label', '')})" if extract else "the Shopee sales extract")
    notes = (
        f"One row per configuration, in the order and scope of {COMPARISON_TAB} (reporting week "
        f"{week}, Asia/Singapore; cutoff {_local(report.as_of)}). Label: brand and model · "
        f"screen · CPU family · installed RAM/storage. {DETAIL_TABS_TEXT}",
        f"HISTORICAL SHOPEE SNAPSHOT, not a current price: the lowest eligible mapped-listing "
        f"snapshot price from {snapshot}. Stock is not verified, and it is never compared with "
        f"or included in the retailer prices. Each cell's note gives the snapshot date, the "
        f"source listing and its variation (model_id); a blank cell's note says why. "
        f"{DETAIL_TABS_TEXT}",
        *(f"{RETAILER_NAMES[code]}: the price selected in {COMPARISON_TAB} for the reporting week "
          f"{week} (Asia/Singapore). Italic = listed-spec match, not an exact product match. "
          f"Out-of-stock and bundle prices are kept under the owner-approved policy. Each cell's "
          f"note gives the matching basis, stock, bundle terms and warnings; a blank cell's note "
          f"explains the blank and links an unconfirmed page to recheck when one exists. "
          f"{DETAIL_TABS_TEXT} {STATUS_LEGEND}" for code, _ in RETAILERS),
    )
    return TabPlan(SUMMARY_TAB, SUMMARY_HEADER, tuple(rows), notes)


def summary_issues(comparison: TabPlan, summary: TabPlan) -> list[str]:
    """The summary repeats the comparison exactly: same rows in the same order, and every price
    cell (value, kind and PHP format) identical to the detailed comparison's cell."""
    if len(comparison.rows) != len(summary.rows):
        return [f"{SUMMARY_TAB} has a different number of rows from {COMPARISON_TAB}"]
    if summary.header != SUMMARY_HEADER:
        return [f"{SUMMARY_TAB} header differs from its seven columns"]
    sources = [comparison.header.index(SUMMARY_SHOPEE_PRICE), *(
        comparison.header.index(f"{RETAILER_NAMES[code]} price (PHP)") for code, _ in RETAILERS)]
    target = comparison.header.index("Target ID")
    issues = []
    for detailed, brief in zip(comparison.rows, summary.rows, strict=True):
        for column, source in enumerate(sources, start=1):
            mine, theirs = brief[column], detailed[source]
            if (mine.kind, mine.value, mine.number_format) != (
                    theirs.kind, theirs.value, theirs.number_format):
                issues.append(f"{SUMMARY_TAB} {detailed[target].value} "
                              f"{SUMMARY_HEADER[column]} differs from {COMPARISON_TAB}")
    return issues


# PILOT_PRICE_OVERVIEW (pilot-sheets-6, 2026-10-22): exactly these twelve columns, in the
# retailer order of the owner's earlier formula-linked overview. Every value is a cell of the
# detailed comparison (overview_issues checks it); a blank comparable price stays blank.
OVERVIEW_RETAILERS: tuple[str, ...] = ("DBZ", "PCX", "LF", "PCC", "OCT")
OVERVIEW_SHOPEE = "Shopee (PHP, historical snapshot)"
# The twelve columns of pilot-sheets-6 (2026-10-22); a live tab with exactly this header is still
# this publisher's. pilot-sheets-7 appends one text column at the right: colour is never the
# only signal for a MATCH, an alternative or a recheck.
OVERVIEW_LABELS = "Comparison labels (text)"
OVERVIEW_HEADER_PILOT_SHEETS_6: tuple[str, ...] = (
    "Rank", "Laptop", "Screen (in)", "CPU family", "RAM (GB)", "Storage (GB)", OVERVIEW_SHOPEE,
    *(f"{RETAILER_NAMES[code]} (PHP)" for code in OVERVIEW_RETAILERS),
)
OVERVIEW_HEADER: tuple[str, ...] = (*OVERVIEW_HEADER_PILOT_SHEETS_6, OVERVIEW_LABELS)
EARLIER_HEADERS[OVERVIEW_TAB] = (OVERVIEW_HEADER_PILOT_SHEETS_6,)
# Overview columns and the comparison columns they repeat.
_OVERVIEW_SOURCES: dict[str, str] = {
    "Rank": "Rank", "Screen (in)": "Screen (in)", "CPU family": "CPU family",
    "RAM (GB)": "RAM (GB)", "Storage (GB)": "Storage (GB)",
    OVERVIEW_SHOPEE: SUMMARY_SHOPEE_PRICE,
    **{f"{RETAILER_NAMES[code]} (PHP)": f"{RETAILER_NAMES[code]} price (PHP)"
       for code in OVERVIEW_RETAILERS},
}
# The owner-built overview found on the live sheet on 2026-10-22: twelve FILTER/INDEX formulas
# over PILOT_PRICE_COMPARISON, not written by this publisher. It is replaced only when the caller
# explicitly adopts it (--adopt-overview); the full tab is snapshotted locally first.
MANUAL_OVERVIEW_HEADER: tuple[str, ...] = (
    "Rank", "Laptop", "Screen (in)", "CPU family", "RAM (GB)", "Storage (GB)",
    "Shopee (PHP, snapshot)", "DataBlitz (PHP)", "PC Express (PHP)", "Laptop Factory (PHP)",
    "PC Central (PHP)", "Octagon (PHP)",
)
ADOPTABLE_HEADERS: dict[str, tuple[tuple[str, ...], ...]] = {
    OVERVIEW_TAB: (MANUAL_OVERVIEW_HEADER,),
}


def _identity_note(row: Mapping[str, str], label: str) -> str:
    """The target identity behind an overview row, kept out of the visible grid."""
    return (f"Target ID {row['target_id']} (rank {row['rank']}): {label}.\n"
            f"Prices are this target's cells in {COMPARISON_TAB}; the Shopee price is a "
            "historical sales-extract snapshot, never a current price. Cell context (stock, "
            f"match basis, why a price is blank) is in {SUMMARY_TAB} notes.")


def _stock_text(stock: str) -> str:
    return {
        "IN_STOCK": "in stock",
        "OUT_OF_STOCK": "OUT OF STOCK (price kept under the owner-approved policy)",
    }.get(stock, REASON_TEXT.get("AVAILABILITY_" + stock, stock or "not stated"))


def _alternative_configuration(alt: Mapping[str, str]) -> str:
    """The configuration an alternative offer actually has, as its page states it."""
    cpu = " ".join(filter(None, (alt["offer_cpu_series"],
                                 f"({alt['offer_cpu_model']})" if alt["offer_cpu_model"] else "")))
    parts = [
        f"model {alt['offer_model']}" if alt["offer_model"] else "",
        f"{alt['offer_screen_in']}in" if alt["offer_screen_in"] else "",
        f"CPU {cpu}" if cpu else "",
        f"RAM {_capacity_text(alt['offer_ram_gb'])}" if alt["offer_ram_gb"] else "",
        f"storage {_capacity_text(alt['offer_storage_gb'])}" if alt["offer_storage_gb"] else "",
        f"GPU {alt['offer_gpu']}" if alt["offer_gpu"] else "",
    ]
    return "; ".join(part for part in parts if part)


def _overview_note(report: PilotReport, row: Mapping[str, str], code: str, label: str) -> str:
    """The note of one overview price (or recheck) cell: the tag and its meaning, the matching
    basis, the actual configuration, differences, unknowns, stock and a reference to the
    detailed record. A cell with neither a price nor a recheck has no note."""
    name, tag = RETAILER_NAMES[code], cell_tag(row, code)
    priced = bool(row[f"{code}_Price"])
    if not priced and tag != "RECHECK":
        return ""
    target_id, url = row["target_id"], row.get(f"{code}_Offer_URL", "")
    alt = report.displayed_alternative(target_id, code) if priced and tag.startswith("ALT") \
        else None
    lines = []
    if tag == "RECHECK":
        lines = [
            f"{name}: RECHECK — unresolved evidence; no confirmed comparison price.",
            _status_text(row[f"{code}_Price_Status"], "", "", "", "") + ".",
        ]
        note = row.get(f"{code}_Recheck_Note") or row.get(f"{code}_Candidate_Detail", "")
        if note:
            lines.append(f"Why: {note}")
        if url:
            lines.append(f"Page to recheck (unconfirmed, not a price): {url}")
        lines.append(f"Detailed record: {COMPARISON_TAB} row for {target_id}; {REVIEW_TAB}.")
        return "\n".join(lines)
    if tag.startswith("ALT"):
        lines = [
            f"{name}: {tag} — close alternative, NOT a matching configuration.",
            "Matching basis: none (ALTERNATIVE; at most two established differences from the "
            f"target: {label}).",
            "Actual configuration: " + (_alternative_configuration(alt) if alt else
                                        row.get(f"{code}_Offer_Details", "") or "see the link"),
            f"Differences: {row.get(f'{code}_Alternative_Differences', '')}",
        ]
        unknown = row.get(f"{code}_Alternative_Unknowns", "")
        if unknown:
            lines.append(f"Unknown / limited: {unknown}")
        if alt and alt["warnings"]:
            lines.append("Notes: " + _reason_text(_codes(alt["warnings"])))
    else:
        if row[f"{code}_Match_Basis"] == "LISTED_SPEC":
            lines = [f"{name}: MATCH — matching configuration (listed-spec match).",
                     "Matching basis: listed specs — brand, family, screen, CPU series, installed "
                     "RAM and storage agree; the specific Shopee model is unknown."]
            if row.get(f"{code}_Offer_Details"):
                lines.append(f"Offer as listed: {row[f'{code}_Offer_Details']}")
        else:
            detail = ", ".join(BASIS_TEXT[tag_] for tag_ in row[f"{code}_Match_Basis"].split("+")
                               if tag_ in BASIS_TEXT)
            lines = [f"{name}: MATCH — matching configuration (exact product match).",
                     "Matching basis: exact product" + (f" ({detail})" if detail else "") + "."]
        lines.append(f"Configuration: the target, {label}.")
        warnings = [w for w in _codes(row.get(f"{code}_Warnings", ""))
                    if w not in {"SPECIFIC_IDENTITY_UNRESOLVED"}]
        if warnings:
            lines.append("Notes: " + _reason_text(warnings) + ".")
        hidden = row.get(f"{code}_Alternative_Offers", "")
        if hidden not in {"", "0"}:
            lines.append(f"{hidden} close alternative offer(s) at this retailer are also listed "
                         f"in {ALTERNATIVES_TAB}.")
    lines += [f"Stock: {_stock_text(row.get(f'{code}_Stock', ''))}.",
              f"Observed: {row.get(f'{code}_Observed_At', '')} (Asia/Singapore).",
              f"Offer: {url}"]
    if alt:
        lines.append(f"Detailed record: {ALTERNATIVES_TAB} row for {target_id}, observation "
                     f"{alt['observation_id']}.")
    else:
        lines.append(f"Detailed record: {COMPARISON_TAB} row for {target_id}.")
    return "\n".join(lines)


def _overview_labels(row: Mapping[str, str]) -> str:
    """``DataBlitz: ALT 1 · RAM; PC Express: MATCH``: the text of every colour."""
    return "; ".join(f"{RETAILER_NAMES[code]}: {cell_tag(row, code)}"
                     for code in OVERVIEW_RETAILERS if cell_tag(row, code))


def overview_plan(report: PilotReport, comparison: TabPlan) -> TabPlan:
    """One row per configuration in the comparison's order; values copied from its cells.

    The Laptop column names brand, series and model; screen, CPU and capacities have their own
    columns. Where two configurations would still read the same, ``configuration_labels`` adds
    the catalog fields that differ, so rows are never visually merged. The target ID is kept in
    the Laptop cell's note. Listed-spec prices are italic, as in the summary. Each retailer
    cell is filled by its comparison tag (green MATCH, yellow ALT 1, orange ALT 2, gray RECHECK
    without a price), carries a note, and an alternative's price links to its offer; the last
    column writes the tags as text."""
    column = {name: comparison.header.index(name) for name in set(_OVERVIEW_SOURCES.values())}
    labels = configuration_labels(report.comparison)
    rows = []
    for row, label, detailed in zip(report.comparison, labels, comparison.rows, strict=True):
        base = configuration_label(row)
        laptop = configuration_name(row) + label[len(base):]
        cells: list[Cell] = []
        for name in OVERVIEW_HEADER:
            if name == "Laptop":
                cells.append(text(laptop, note=_identity_note(row, label)))
                continue
            if name == OVERVIEW_LABELS:
                cells.append(text(_overview_labels(row)))
                continue
            source = detailed[column[_OVERVIEW_SOURCES[name]]]
            code = next((c for c in OVERVIEW_RETAILERS
                         if name == f"{RETAILER_NAMES[c]} (PHP)"), "")
            if not code:
                cells.append(replace(source, note="", italic=False, bold=False))
                continue
            tag = cell_tag(row, code)
            priced = source.kind == "number"
            italic = priced and row[f"{code}_Match_Basis"] == "LISTED_SPEC"
            link = row.get(f"{code}_Offer_URL", "") if priced and tag.startswith("ALT") else ""
            cells.append(replace(
                source, note=_overview_note(report, row, code, label), italic=italic,
                bold=False, fill=tag_fill(tag, priced=priced), link=link))
        rows.append(tuple(cells))
    week = (f"{report.week_start.isoformat()} to "
            f"{(report.week_end - timedelta(days=1)).isoformat()}")
    sales = report.summary["sales_snapshot"]
    extract = str(sales.get("extract_date") or next(
        (r.get("Shopee_Snapshot_Date", "") for r in report.comparison), ""))
    identity = (f"Publisher-managed: one row per configuration, in the rank order and scope of "
                f"{COMPARISON_TAB} (reporting week {week}, Asia/Singapore). The target ID is in "
                "each Laptop cell's note. Screen, CPU family, installed RAM and storage have "
                "their own columns.")
    retailer = (f"The price shown in {COMPARISON_TAB} for the week {week}: a matching "
                "configuration when one is comparable, otherwise the best close alternative "
                "(ALT 1 before ALT 2, then the lowest price). Blank means no comparable price "
                "(not 'not sold'); out-of-stock and bundle prices are kept under the "
                "owner-approved policy. Italic = listed-spec match. " + STATUS_LEGEND +
                f" Why a cell is blank, stock and match basis: {SUMMARY_TAB} notes; every "
                f"alternative: {ALTERNATIVES_TAB}.")
    labels_note = ("The tag of every retailer cell, as text (the fills repeat it): MATCH, "
                   "ALT 1 · …, ALT 2 · … (the differing specifications) or RECHECK. "
                   + STATUS_LEGEND)
    notes = tuple(
        identity if name in {"Rank", "Laptop", "Screen (in)", "CPU family", "RAM (GB)",
                             "Storage (GB)"}
        else labels_note if name == OVERVIEW_LABELS
        else (f"HISTORICAL SHOPEE SNAPSHOT, not a current price: the lowest eligible "
              f"mapped-listing price of the Shopee sales extract of {extract or 'unknown date'}. "
              f"Stock is not verified and it is never part of the retailer prices. Listing, "
              f"variation and date: {COMPARISON_TAB} and {SUMMARY_TAB} notes.")
        if name == OVERVIEW_SHOPEE else retailer
        for name in OVERVIEW_HEADER
    )
    return TabPlan(OVERVIEW_TAB, OVERVIEW_HEADER, tuple(rows), notes)


def overview_issues(
    report: PilotReport, comparison: TabPlan, overview: TabPlan
) -> list[str]:
    """The overview repeats the comparison: same rows, order and targets, every repeated cell
    (value, kind and format) identical, every status fill and the text labels agreeing with the
    comparison tags, and no two rows that read the same."""
    if overview.header != OVERVIEW_HEADER:
        return [f"{OVERVIEW_TAB} header differs from its thirteen columns"]
    if len(overview.rows) != len(comparison.rows) or len(overview.rows) != len(
            report.comparison):
        return [f"{OVERVIEW_TAB} has a different number of rows from {COMPARISON_TAB}"]
    issues = []
    target = comparison.header.index("Target ID")
    visible: dict[tuple[str, ...], str] = {}
    for row, detailed, brief in zip(report.comparison, comparison.rows, overview.rows,
                                    strict=True):
        target_id = detailed[target].value
        if target_id != row["target_id"] or f"Target ID {target_id} " not in brief[1].note:
            issues.append(f"{OVERVIEW_TAB} row for {target_id} names another target")
        for index, name in enumerate(OVERVIEW_HEADER):
            if name == "Laptop":
                continue
            mine = brief[index]
            if name == OVERVIEW_LABELS:
                if mine.value != _overview_labels(row):
                    issues.append(f"{OVERVIEW_TAB} {target_id} labels differ from the "
                                  f"{COMPARISON_TAB} tags")
                continue
            theirs = detailed[comparison.header.index(_OVERVIEW_SOURCES[name])]
            if (mine.kind, mine.value, mine.number_format) != (
                    theirs.kind, theirs.value, theirs.number_format):
                issues.append(f"{OVERVIEW_TAB} {target_id} {name} differs from {COMPARISON_TAB}")
            code = next((c for c in OVERVIEW_RETAILERS
                         if name == f"{RETAILER_NAMES[c]} (PHP)"), "")
            if code:
                tag = cell_tag(row, code)
                if mine.fill != tag_fill(tag, priced=mine.kind == "number"):
                    issues.append(f"{OVERVIEW_TAB} {target_id} {name} fill differs from its tag")
                if (mine.kind == "number" or tag == "RECHECK") and not mine.note:
                    issues.append(f"{OVERVIEW_TAB} {target_id} {name} has no cell note")
                if tag.startswith("ALT") and mine.kind == "number" and (
                        mine.link != row.get(f"{code}_Offer_URL", "")):
                    issues.append(f"{OVERVIEW_TAB} {target_id} {name} alternative has no link")
        key = tuple(cell.value for cell in brief[1:6])
        if key in visible:
            issues.append(f"{OVERVIEW_TAB} rows for {visible[key]} and {target_id} read the same")
        visible[key] = target_id
    return issues


# PILOT_PRICE_ALTERNATIVES (pilot-sheets-7, 2026-10-27): one row per target-retailer-offer
# association that qualifies as a close alternative (at most two established differences across
# model, screen, CPU, installed RAM, installed storage and GPU), including alternatives of
# retailers that also have a matching offer and offers the main comparison does not show.
ALTERNATIVES_HEADER: tuple[str, ...] = (
    "Rank", "Target ID", "Target configuration", "Retailer", "Actual product / model code",
    "Actual screen (in)", "Actual CPU series", "Actual CPU (exact processor)", "Actual RAM (GB)",
    "Actual storage (GB)", "Actual GPU", "Price (PHP)", "Currency", "Stock", "Product link",
    "Difference count", "Comparison tag", "Differences (target vs retailer)",
    "Model comparison", "Screen comparison", "CPU comparison", "RAM comparison",
    "Storage comparison", "GPU comparison", "Unknown fields", "Limitations", "Warnings",
    "Price eligibility", "Shown in main comparison", "Matching offer at this retailer",
    "Observed (Asia/Singapore)", "Observation ID", "Evidence references",
    "Initiating target(s)", "Exact target of this offer", "Policy version",
)
STATE_TEXT = {"AGREES": "agrees", "DIFFERS": "differs", "UNKNOWN": "unknown",
              "CONFLICTING_EVIDENCE": "conflicting evidence"}
_NUMERIC = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


def _numeric(value: str, kind: str) -> Cell:
    """A number when the value is one (an unparseable value stays text, never zero)."""
    return number(value, kind) if _NUMERIC.fullmatch(value) else text(value)


def _alternative_sort_key(alt: Mapping[str, str]) -> tuple:
    codes = [code for code, _ in RETAILERS]
    return (int(alt["rank"]), codes.index(alt["retailer_code"]), int(alt["difference_count"]),
            Decimal(alt["price"]), alt["observation_id"])


def alternatives_plan(report: PilotReport) -> TabPlan:
    """The close-alternative associations of the pilot scope in rank, retailer, difference-count
    and price order. Prices are numbers; every row says what the offer actually is, how it
    differs, what stays unknown, and whether its price is eligible and shown."""
    labels = {row["target_id"]: configuration_label(row) + (
        f" · {row['GPU_Model']}" if row.get("GPU_Model") else "") for row in report.comparison}
    rows = []
    for alt in sorted(report.alternatives, key=_alternative_sort_key):
        states = {dimension: alt[f"{dimension}_state"] for dimension in DIMENSIONS}
        fill = tag_fill(alt["alternative_tag"], priced=True)
        eligible = ("Comparable" if alt["price_eligibility"] == COMPARABLE else
                    "Excluded: " + (_reason_text(_codes(alt["price_exclusion_reasons"]))
                                    or "not comparable"))
        url = alt["offer_url"]
        rows.append((
            number(alt["rank"], "integer"),
            text(alt["target_id"]),
            text(labels.get(alt["target_id"], "")),
            text(RETAILER_NAMES.get(alt["retailer_code"], alt["retailer_id"])),
            text(alt["offer_model"] or alt["title"]),
            _numeric(alt["offer_screen_in"], "decimal"),
            text(alt["offer_cpu_series"]),
            text(alt["offer_cpu_model"]),
            _numeric(alt["offer_ram_gb"], "integer"),
            _numeric(alt["offer_storage_gb"], "integer"),
            text(alt["offer_gpu"]),
            number(alt["price"], "php"),
            text(alt["currency"]),
            text(alt["availability"]),
            text(url, link=url),
            number(alt["difference_count"], "integer", fill=fill),
            text(alt["alternative_tag"], fill=fill),
            text(alt["differences"]),
            *(text(STATE_TEXT.get(states[d], states[d])) for d in DIMENSIONS),
            text(alt["unknown_fields"]),
            text(alt["limitations"].replace(";", ", ")),
            text(alt["warnings"].replace(";", ", ")),
            text(eligible),
            text("Yes" if alt["displayed_in_comparison"] == "TRUE" else "No"),
            text("Yes" if alt["retailer_has_matching_offer"] == "TRUE" else "No"),
            text(alt["retrieved_at_local"]),
            text(alt["observation_id"]),
            text(alt["evidence_references"]),
            text(alt["initiating_target_ids"].replace(";", ", ")),
            text(alt["exact_target_id"]),
            text(alt["policy_version"]),
        ))
    week = (f"{report.week_start.isoformat()} to "
            f"{(report.week_end - timedelta(days=1)).isoformat()}")
    intro = (f"Close alternatives for the reporting week {week} (Asia/Singapore): offers that are "
             "NOT matching configurations of the target but differ in at most two established "
             "specifications (model, screen, CPU, installed RAM, installed storage, GPU share "
             "that budget). One row per target-retailer-offer, including alternatives of "
             f"retailers that also have a matching offer. The main comparison ({COMPARISON_TAB}) "
             "shows an alternative only where no matching offer is comparable. "
             f"{STATUS_LEGEND}")
    notes = tuple(
        intro if name == "Rank" else
        "Fewest differences first within a retailer, then the lowest price." if name ==
        "Price (PHP)" else
        "Unknown fields are never counted as agreement or as a difference; limitations say "
        "what could not be compared (for example the exact processor or the GPU)."
        if name == "Unknown fields" else
        "Shown in main comparison = this offer is the one displayed for the target and "
        "retailer in the comparison and overview." if name == "Shown in main comparison" else ""
        for name in ALTERNATIVES_HEADER)
    return TabPlan(ALTERNATIVES_TAB, ALTERNATIVES_HEADER, tuple(rows), notes)


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
        close = row["review_reason"].startswith(CLOSE_ALTERNATIVE_REVIEW)
        category = (
            "Close alternative (confirmed)"
            if close
            else "Matched, excluded from comparison"
            if exact and row["comparability_status"] == "NOT_COMPARABLE"
            else "Matching problem"
            if not exact
            else "Other review"
        )
        details = _reason_text(_codes(row["comparability_reasons"])) if exact else ""
        if close:
            # A qualifying alternative (at most two established differences) is not an
            # unresolved "different configuration"; its tags and differences are the details.
            details = row["review_reason"][len(CLOSE_ALTERNATIVE_REVIEW):].strip()
        if extra:
            details = "; ".join(filter(None, [details, f"collection: {extra}"]))
        rows.append(
            (
                text(category),
                text(CLOSE_ALTERNATIVE_REVIEW if close else row["review_reason"]),
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
# pilot-sheets-5 (2026-10-20) appends these; earlier rows are kept with them blank, and a run
# log with the earlier header is still this publisher's.
RUN_LOG_HEADER_PILOT_SHEETS_4 = RUN_LOG_HEADER
RUN_LOG_HEADER = (
    *RUN_LOG_HEADER_PILOT_SHEETS_4,
    "Summary rows",
    "Shopee snapshot prices",
    "Shopee snapshot extract date",
)
# pilot-sheets-6 (2026-10-22) appends the overview row count and any adopted tab.
RUN_LOG_HEADER_PILOT_SHEETS_5 = RUN_LOG_HEADER
RUN_LOG_HEADER_PILOT_SHEETS_6 = (*RUN_LOG_HEADER_PILOT_SHEETS_5, "Overview rows", "Adopted tabs")
# pilot-sheets-7 (2026-10-27) appends the close-alternative counts.
RUN_LOG_HEADER = (*RUN_LOG_HEADER_PILOT_SHEETS_6, "Alternative prices shown",
                  "Alternative association rows", "Alternatives rows published")
EARLIER_HEADERS[RUN_LOG_TAB] = (RUN_LOG_HEADER_PILOT_SHEETS_4, RUN_LOG_HEADER_PILOT_SHEETS_5,
                                RUN_LOG_HEADER_PILOT_SHEETS_6)


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
    # Existing tabs this publication replaces with the caller's explicit consent (adoption).
    adopted: tuple[str, ...] = ()

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
            "Summary rows": number(str(len(self.tabs[SUMMARY_TAB].rows)), "integer"),
            "Shopee snapshot prices": number(str(counts.get("shopee_snapshot_prices", 0)),
                                             "integer"),
            "Shopee snapshot extract date": text(next(
                (r["Shopee_Snapshot_Date"] for r in report.comparison
                 if r.get("Shopee_Snapshot_Date")), "")),
            "Overview rows": number(str(len(self.tabs[OVERVIEW_TAB].rows)), "integer"),
            "Adopted tabs": text(", ".join(self.adopted)),
            "Alternative prices shown": number(str(counts.get("alternative_prices", 0)),
                                               "integer"),
            "Alternative association rows": number(str(counts.get("alternative_rows", 0)),
                                                   "integer"),
            "Alternatives rows published": number(
                str(len(self.tabs[ALTERNATIVES_TAB].rows)), "integer"),
        }
        return values


def build_plan(report: PilotReport) -> PublicationPlan:
    comparison = comparison_plan(report)
    tabs = {
        COMPARISON_TAB: comparison,
        SUMMARY_TAB: summary_plan(report),
        OVERVIEW_TAB: overview_plan(report, comparison),
        ALTERNATIVES_TAB: alternatives_plan(report),
        HISTORY_TAB: history_plan(report),
        REVIEW_TAB: review_plan(report),
    }
    for plan in tabs.values():
        for row in plan.rows:
            if len(row) != plan.width:
                raise PublicationInputError([f"{plan.title} row width differs from its header"])
    issues = summary_issues(comparison, tabs[SUMMARY_TAB]) + overview_issues(
        report, comparison, tabs[OVERVIEW_TAB])
    if issues:
        raise PublicationInputError(issues)
    return PublicationPlan(report, tabs, publication_id(report), grid_sha256(list(tabs.values())))


def write_plan_csv(path: Path, plan: TabPlan) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for row in plan.grid():
            writer.writerow([cell.value for cell in row])


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def write_plan_notes_csv(path: Path, plan: TabPlan) -> int:
    """The planned cell notes and italics of a tab (one row per annotated cell); the count."""
    count = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["cell", "column", "row_label", "value", "italic", "note"])
        for r, row in enumerate(plan.grid()):
            for c, cell in enumerate(row):
                if cell.note or cell.italic:
                    label = ("(banner)" if r < plan.header_row else "(header)"
                             if r == plan.header_row else row[0].value)
                    writer.writerow([f"{_column_letter(c)}{r + 1}", plan.header[c], label,
                                     cell.value,
                                     "TRUE" if cell.italic else "", cell.note])
                    count += 1
    return count


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
    if cell.italic:
        text_format["italic"] = True
    if text_format:
        data.setdefault("userEnteredFormat", {})["textFormat"] = text_format
    if cell.note:
        data["note"] = cell.note
    return data


@dataclass(frozen=True)
class TabState:
    title: str
    exists: bool
    sheet_id: int | None = None
    row_count: int = 0
    column_count: int = 0
    grid: tuple[tuple[dict, ...], ...] = ()
    # Column groups and conditional-format rules as the API returned them (empty when none).
    column_groups: tuple[dict, ...] = ()
    conditional_formats: tuple[dict, ...] = ()

    @property
    def used_rows(self) -> int:
        notes = self.title in NOTE_TABS
        return sum(1 for row in self.grid if any(_has_content(c, notes=notes) for c in row))

    def header(self, row: int = 0) -> list[str]:
        if row >= len(self.grid):
            return []
        return [_cell_text(cell) for cell in self.grid[row]]


def _has_content(cell: Mapping, *, notes: bool = False) -> bool:
    """A value; with ``notes``, a note counts too (a stale note on a note tab)."""
    return bool(cell.get("userEnteredValue")) or (notes and bool(cell.get("note")))


def _cell_text(cell: Mapping) -> str:
    value = cell.get("userEnteredValue") or {}
    if "stringValue" in value:
        return value["stringValue"]
    if "numberValue" in value:
        return repr(value["numberValue"])
    return str(next(iter(value.values()), "")) if value else ""


def read_tab(spreadsheet, title: str, allowed: Sequence[str] = PUBLISHER_TABS) -> TabState:
    """Read one destination tab (values, links, formats); a missing tab is reported, not made."""
    assert_publisher_tab(title, allowed)
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
        tuple(sheet.get("columnGroups", [])),
        tuple(sheet.get("conditionalFormats", [])),
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


def adoptable(state: TabState) -> bool:
    """True for an owner-built tab this publisher may replace, but only with explicit consent:
    its exact known header and nothing outside that header's columns."""
    if not state.exists or state.used_rows == 0:
        return False
    for known in ADOPTABLE_HEADERS.get(state.title, ()):
        if state.header()[: len(known)] == list(known) and not any(
            _has_content(cell, notes=True) for row in state.grid for cell in row[len(known):]
        ):
            return True
    return False


ADOPTION_HINT = ("rerun with --adopt-overview to replace it; the whole tab (values, formulas, "
                 "notes, formats, sizes) is saved locally first and --rollback restores it")


def destination_problems(
    states: Mapping[str, TabState], headers: Mapping[str, Sequence[str]],
    adopt: Sequence[str] = (),
) -> tuple[list[str], list[str]]:
    """``(refusals, adopted tabs)``. A tab with foreign content is refused; a recognised
    owner-built tab is adopted only when named in ``adopt``, and refused otherwise."""
    problems, adopted = [], []
    for title in PUBLISHER_TABS:
        problem = check_ownership(states[title], headers[title])
        if not problem:
            continue
        if adoptable(states[title]):
            if title in adopt:
                adopted.append(title)
                continue
            problem = f"{title} is the owner-built layout, not this publisher's: {ADOPTION_HINT}"
        problems.append(problem)
    return problems, adopted


# Full tab snapshots (pilot-sheets-6): values and formulas, displayed values, notes, every
# user-entered format, column widths, row heights, frozen panes, gridlines, the basic filter and
# (for information) conditional formats and merges. Taken before any write and used by rollback.
SNAPSHOT_FIELDS = (
    "sheets(properties(sheetId,title,gridProperties),basicFilter,conditionalFormats,merges,"
    "columnGroups,"
    "data(startRow,startColumn,rowMetadata(pixelSize),columnMetadata(pixelSize,hiddenByUser),"
    "rowData(values(userEnteredValue,formattedValue,hyperlink,note,userEnteredFormat))))"
)
RESTORE_CELL_FIELDS = "userEnteredValue,userEnteredFormat,note"


def snapshot_tab(
    spreadsheet, title: str, allowed: Sequence[str] = PUBLISHER_TABS
) -> dict | None:
    """The whole destination tab as the Sheets API returns it, or None when it is missing."""
    assert_publisher_tab(title, allowed)
    if not any(ws.title == title for ws in spreadsheet.worksheets()):
        return None
    metadata = spreadsheet.fetch_sheet_metadata(
        params={"ranges": f"'{title}'", "includeGridData": "true", "fields": SNAPSHOT_FIELDS})
    return next(s for s in metadata["sheets"] if s["properties"]["title"] == title)


def snapshot_rows(snapshot: Mapping) -> list[list[dict]]:
    return [list(row.get("values", [])) for block in snapshot.get("data", [])
            for row in block.get("rowData", [])]


def _dimension_requests(sheet_id: int, dimension: str, sizes: Sequence[Mapping]) -> list[dict]:
    return [{"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": dimension, "startIndex": index,
                  "endIndex": index + 1},
        "properties": {"pixelSize": size["pixelSize"]}, "fields": "pixelSize"}}
        for index, size in enumerate(sizes) if size.get("pixelSize")]


def _group_range(sheet_id: int, group: Mapping) -> dict:
    rng = group["range"]
    return {"sheetId": sheet_id, "dimension": rng.get("dimension", "COLUMNS"),
            "startIndex": rng["startIndex"], "endIndex": rng["endIndex"]}


def clear_group_requests(sheet_id: int, groups: Sequence[Mapping]) -> list[dict]:
    """Requests that remove every column group, deepest first, and show every column again
    (a collapsed group hides its columns). Idempotent: a tab without groups needs only the
    unhide."""
    requests: list[dict] = [{"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0,
                  "endIndex": max([g["range"]["endIndex"] for g in groups], default=0) or 1},
        "properties": {"hiddenByUser": False}, "fields": "hiddenByUser"}}] if groups else []
    for group in sorted(groups, key=lambda g: -g.get("depth", 1)):
        requests.append({"deleteDimensionGroup": {"range": _group_range(sheet_id, group)}})
    return requests


def add_group_requests(sheet_id: int, groups: Sequence[Mapping]) -> list[dict]:
    """Requests that create column groups (``depth`` order) and set each one's collapsed state."""
    requests: list[dict] = []
    for group in sorted(groups, key=lambda g: g.get("depth", 1)):
        rng = _group_range(sheet_id, group)
        requests.append({"addDimensionGroup": {"range": rng}})
        if group.get("collapsed"):
            requests.append({"updateDimensionGroup": {
                "dimensionGroup": {"range": rng, "depth": group.get("depth", 1),
                                   "collapsed": True},
                "fields": "collapsed"}})
    return requests


def clear_conditional_format_requests(sheet_id: int, count: int) -> list[dict]:
    """Delete every conditional-format rule of a tab (highest index first, so indexes hold)."""
    return [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": index}}
            for index in reversed(range(count))]


def restore_requests(snapshot: Mapping, sheet_id: int,
                     current: TabState | None = None) -> list[dict]:
    """Requests that put a tab back exactly as snapshotted: cells (including formulas, notes
    and formats), frozen panes, gridlines, sizes, hidden columns, column groups, conditional
    formats and the basic filter. ``current`` is the tab as it is now: the groups and rules a
    publication added are removed before the snapshotted ones are put back."""
    requests: list[dict] = []
    if current is not None:
        requests += clear_group_requests(sheet_id, current.column_groups)
        requests += clear_conditional_format_requests(sheet_id, len(current.conditional_formats))
    requests.append({"updateCells": {"range": {"sheetId": sheet_id},
                                     "fields": RESTORE_CELL_FIELDS}})
    if current is not None or snapshot.get("merges"):
        requests.append({"unmergeCells": {"range": {"sheetId": sheet_id}}})
    rows = snapshot_rows(snapshot)
    if rows:
        requests.append({"updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
            "rows": [{"values": [{k: v for k, v in cell.items()
                                  if k in {"userEnteredValue", "userEnteredFormat", "note"}}
                                 for cell in row]} for row in rows],
            "fields": RESTORE_CELL_FIELDS}})
    # Merged ranges as snapshotted (the guide tab has them; data tabs have none), put back after
    # the cells so no value is written into a merged range.
    requests += [{"mergeCells": {"range": dict(merge, sheetId=sheet_id),
                                 "mergeType": "MERGE_ALL"}}
                 for merge in snapshot.get("merges", [])]
    grid = snapshot.get("properties", {}).get("gridProperties", {})
    restored = {"frozenRowCount": grid.get("frozenRowCount", 0),
                "frozenColumnCount": grid.get("frozenColumnCount", 0),
                "hideGridlines": grid.get("hideGridlines", False)}
    requests.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": restored},
        "fields": ",".join(f"gridProperties.{name}" for name in restored)}})
    data = (snapshot.get("data") or [{}])[0]
    requests += _dimension_requests(sheet_id, "COLUMNS", data.get("columnMetadata", []))
    requests += _dimension_requests(sheet_id, "ROWS", data.get("rowMetadata", []))
    requests += [{"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": index,
                  "endIndex": index + 1},
        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}}
        for index, meta in enumerate(data.get("columnMetadata", [])) if meta.get("hiddenByUser")]
    # Groups of the snapshot (a backup taken before 2026-11-05 has none); collapsing re-hides.
    requests += add_group_requests(sheet_id, snapshot.get("columnGroups", []))
    requests += [{"addConditionalFormatRule": {
        "rule": {**rule, "ranges": [dict(r, sheetId=sheet_id) for r in rule.get("ranges", [])]},
        "index": index}} for index, rule in enumerate(snapshot.get("conditionalFormats", []))]
    if snapshot.get("basicFilter"):
        requests.append({"setBasicFilter": {"filter": {
            "range": dict(snapshot["basicFilter"]["range"], sheetId=sheet_id)}}})
    else:
        requests.append({"clearBasicFilter": {"sheetId": sheet_id}})
    return requests


def _display_key(value: str) -> str:
    """A displayed value compared without currency symbol, grouping or trailing zeros."""
    plain = value.strip().replace("₱", "").replace(",", "")
    try:
        return format(Decimal(plain).normalize(), "f") if plain else ""
    except InvalidOperation:
        return value.strip()


def displayed_differences(snapshot: Mapping, plan: TabPlan) -> dict:
    """How the tab's currently displayed values compare with the planned ones (migration
    preview). Formulas are compared by what they display."""
    current = snapshot_rows(snapshot)
    planned = plan.grid()
    differences = []
    height = max(len(current), len(planned))
    width = max([plan.width, *(len(row) for row in current)])
    compared = 0
    for r in range(height):
        for c in range(width):
            shown = current[r][c].get("formattedValue", "") if r < len(current) and c < len(
                current[r]) else ""
            wanted = planned[r][c].value if r < len(planned) and c < plan.width else ""
            if not shown and not wanted:
                continue
            compared += 1
            if _display_key(str(shown)) != _display_key(wanted):
                differences.append({"cell": f"{_column_letter(c)}{r + 1}", "current": shown,
                                    "planned": wanted})
    formulas = sum(1 for row in current for cell in row
                   if "formulaValue" in (cell.get("userEnteredValue") or {}))
    return {"cells_compared": compared, "cells_differing": len(differences),
            "differences": differences[:200], "current_formula_cells": formulas,
            "current_note_cells": sum(1 for row in current for cell in row if cell.get("note"))}


def write_displayed_csv(path: Path, snapshot: Mapping) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for row in snapshot_rows(snapshot):
            writer.writerow([cell.get("formattedValue", "") for cell in row])


def write_backup(backup_dir: Path, state: TabState, snapshot: Mapping | None) -> Path:
    """``backup_<tab>.json``: the earlier row format plus the full snapshot (pilot-sheets-6)."""
    path = backup_dir / f"backup_{state.title}.json"
    path.write_text(json.dumps({"title": state.title, "sheet_id": state.sheet_id,
                                "rows": [list(r) for r in state.grid],
                                "snapshot": snapshot}, ensure_ascii=False), encoding="utf-8")
    return path


@dataclass
class RollbackOutcome:
    status: str
    restored: list[str]
    skipped: list[str]
    issues: list[str]


def rollback(
    spreadsheet,
    backup_dir: Path,
    *,
    progress=None,
    tabs: Sequence[str] = DATA_TABS,
    allowed: Sequence[str] = PUBLISHER_TABS,
) -> RollbackOutcome:
    """Restore every data tab backed up in ``backup_dir`` (a publication folder) to its snapshot.

    Only publisher tabs are touched, each only when it is still the same sheet (same ID) that
    was backed up; the run log is never rewritten (it remains the record of publications). A
    tab the publication created has no backup and is left as it is (reported as skipped).
    ``tabs`` / ``allowed`` select the data tabs and the destination allowlist of another
    publisher schema (the Shopee-variation publisher); retired tabs are never restorable."""
    restored, skipped, issues = [], [], []
    if allowed is not PUBLISHER_TABS and (set(tabs) | set(allowed)) & set(RETIRED_TABS):
        raise DestinationError("A retired tab cannot be a rollback destination.")
    for title in tabs:
        path = backup_dir / f"backup_{title}.json"
        if not path.is_file():
            skipped.append(f"{title}: no backup in this folder (left as it is)")
            continue
        backup = json.loads(path.read_text(encoding="utf-8"))
        if backup.get("title") != title:
            raise DestinationError(f"{path.name} does not hold {title}.")
        state = read_tab(spreadsheet, title, allowed)
        if not state.exists or state.sheet_id != backup.get("sheet_id"):
            raise DestinationError(f"{title} is not the sheet that was backed up; not restored.")
        snapshot = backup.get("snapshot") or {
            "properties": {"gridProperties": {"frozenRowCount": 1}},
            "data": [{"rowData": [{"values": row} for row in backup.get("rows", [])]}]}
        requests = restore_requests(snapshot, state.sheet_id, state)
        guard_requests(requests, {state.sheet_id})
        if progress is not None:
            progress(f"restoring {title}")
        spreadsheet.batch_update({"requests": requests})
        after = read_tab(spreadsheet, title, allowed)
        for r, row in enumerate(snapshot_rows(snapshot)):
            for c, cell in enumerate(row):
                actual = after.grid[r][c] if r < len(after.grid) and c < len(after.grid[r]) else {}
                if (actual.get("userEnteredValue") or {}) != (cell.get("userEnteredValue") or {}) \
                        or (actual.get("note") or "") != (cell.get("note") or ""):
                    issues.append(f"{title} {_column_letter(c)}{r + 1} differs after restore")
        restored.append(title)
    return RollbackOutcome("SUCCESS" if not issues else "READBACK_FAILED", restored, skipped,
                           issues)


def tab_requests(
    state: TabState, plan_grid: Sequence[Sequence[Cell]], width: int, *, layout_fn=None,
    header_row: int = 0, notes: bool | None = None,
) -> list[dict]:
    """Requests that replace this tab's publisher content, keep other formatting, and add a
    frozen header and filter. Every request targets only this tab's sheet ID. ``layout_fn``
    replaces ``tab_layout`` for another publisher schema. ``header_row`` is the zero-based grid
    row of the header when banner rows precede it. ``notes`` says whether the publisher owns the
    tab's cell notes (cleared and rewritten); by default the tab's membership in ``NOTE_TABS``."""
    sheet_id = state.sheet_id
    rows = len(plan_grid)
    owns_notes = state.title in NOTE_TABS if notes is None else notes
    fields = NOTE_CELL_FIELDS if owns_notes else CELL_FIELDS
    layout = (layout_fn or tab_layout)(
        state.title, [cell.value for cell in plan_grid[header_row]] if plan_grid else [])
    header_row = layout.header_row if layout is not None else 0
    grid_properties: dict = {"frozenRowCount": header_row + 1}
    if layout is not None:
        grid_properties |= {"frozenColumnCount": layout.frozen_columns, "hideGridlines": True}
    requests: list[dict] = []
    if layout is not None and layout.manage_columns:
        # An earlier layout's groups and conditional-format rules are keyed to its own column
        # positions. They are removed before anything is written and only this layout's groups
        # are added afterwards, so a repeated publication leaves exactly one set.
        requests += clear_group_requests(sheet_id, state.column_groups)
        requests += clear_conditional_format_requests(sheet_id, len(state.conditional_formats))
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
        # Clear values, number formats, links and bold (and, on a note tab, notes and italics)
        # on the whole tab; other formatting stays.
        {"updateCells": {"range": {"sheetId": sheet_id}, "fields": fields}},
        {
            "updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
                "rows": [{"values": [_cell_data(c) for c in row]} for row in plan_grid],
                "fields": fields,
            }
        },
        {
            # The price tabs also freeze their key columns and hide gridlines (house layout).
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": grid_properties},
                "fields": ",".join(f"gridProperties.{name}" for name in grid_properties),
            }
        },
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": header_row,
                        "endRowIndex": rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": width,
                    }
                }
            }
        },
    ]
    if layout is not None:
        requests += layout_requests(sheet_id, rows, layout)
    # Status fills go last: the layout resets backgrounds and then bands every second row.
    requests += fill_requests(sheet_id, plan_grid)
    if layout is not None and layout.manage_columns:
        requests += add_group_requests(sheet_id, layout.planned_groups())
    return requests


# House layout of the three price tabs (pilot-sheets-6, 2026-10-22). It reproduces the look the
# owner applied by hand to the live tabs (read-only inspection, 2026-10-22): navy headers with
# white Roboto text and an orange rule, orange Shopee headers, tinted price columns, monospaced
# numbers, muted links, alternate-row banding, hidden gridlines and frozen key columns. Before
# pilot-sheets-6 the summary layout reset backgrounds to a light header, which would have left
# the owner's white header text unreadable. Layout requests never touch values, links, bold,
# italics or notes, and conditional-format rules are left as they are.
def _rgb(code: str) -> dict:
    # Exact n/255: Sheets keeps 8 bits and truncates, so a value rounded down to 6 decimals
    # (0.835294 for 213) is stored one level low and fails the fill readback.
    return {name: int(code[i:i + 2], 16) / 255
            for name, i in (("red", 1), ("green", 3), ("blue", 5))}


NAVY, NAVY_ALT, ORANGE = _rgb("#1B3265"), _rgb("#284774"), _rgb("#E68834")
WHITE, INK, MUTED = _rgb("#FFFFFF"), _rgb("#24334B"), _rgb("#64748B")
FILLS = {"shopee": _rgb("#FFF2DF"), "retailer": _rgb("#E8EEF8"), "in_stock": _rgb("#E8F3ED")}
BAND_BACKGROUND = _rgb("#F5F7FB")
HEADER_FILLS = {"navy": NAVY, "navy_alt": NAVY_ALT, "orange": ORANGE}
# Comparison-status fills (pilot-sheets-7): light tints that keep the dark ink text readable and
# stay apart from the house palette (navy and orange headers, Shopee and retailer tints).
STATUS_FILLS = {"match": _rgb("#D5EFDB"), "alt1": _rgb("#FFF2A8"), "alt2": _rgb("#FFCF99"),
                "recheck": _rgb("#DADDE3"),
                # Variation dashboard (2026-11-05): review/conflict is a distinct violet; results
                # without a price are a quiet neutral; a stale Shopee snapshot is a soft amber.
                "review": _rgb("#E4DAF4"), "missing": _rgb("#EEF1F5"), "stale": _rgb("#FFE3B8")}
PADDING = {"top": 6, "right": 8, "bottom": 6, "left": 8}
LAYOUT_FIELDS = ",".join(f"userEnteredFormat.{name}" for name in (
    "backgroundColor", "backgroundColorStyle", "horizontalAlignment", "verticalAlignment",
    "wrapStrategy", "padding", "borders", "textFormat.foregroundColor",
    "textFormat.foregroundColorStyle", "textFormat.fontFamily", "textFormat.fontSize",
))


@dataclass(frozen=True)
class ColumnStyle:
    width: int
    header: str = "navy"  # HEADER_FILLS key
    fill: str = ""  # FILLS key; blank = white with banding
    wrap: str = "WRAP"
    align: str = "LEFT"
    mono: bool = False  # Roboto Mono for numbers
    muted: bool = False  # grey text for IDs and links


@dataclass(frozen=True)
class TabLayout:
    columns: tuple[ColumnStyle, ...]
    frozen_columns: int
    header_height: int
    row_height: int
    # Banner rows above the header (pixel heights); the header row follows them.
    preface_heights: tuple[int, ...] = ()
    # Collapsed column groups as ``(first, end)`` column indexes (end exclusive), depth 1.
    groups: tuple[tuple[int, int], ...] = ()
    # The publisher owns this tab's column positions: rules and groups left by an earlier layout
    # would point at the wrong columns, so it removes them all and writes only its own.
    manage_columns: bool = False

    @property
    def header_row(self) -> int:
        return len(self.preface_heights)

    def planned_groups(self) -> list[dict]:
        return [{"range": {"dimension": "COLUMNS", "startIndex": first, "endIndex": end},
                 "depth": 1, "collapsed": True} for first, end in self.groups]


_NUMBER = {"align": "RIGHT", "mono": True}
_LINK = {"wrap": "CLIP", "muted": True}
_IDENTITY_STYLES = {
    "Rank": ColumnStyle(52, **_NUMBER), "Target ID": ColumnStyle(120, **_LINK),
    "Brand": ColumnStyle(92), "Series": ColumnStyle(112), "Model": ColumnStyle(200),
    "Screen (in)": ColumnStyle(82, **_NUMBER), "CPU family": ColumnStyle(124),
    "RAM (GB)": ColumnStyle(78, **_NUMBER), "Storage (GB)": ColumnStyle(88, **_NUMBER),
}


def _comparison_styles() -> dict[str, ColumnStyle]:
    styles = dict(_IDENTITY_STYLES)
    shopee = ColumnStyle(140, "orange", "shopee", **_NUMBER), ColumnStyle(175, "orange", **_LINK)
    styles |= dict(zip(SHOPEE_HEADER, (*shopee, ColumnStyle(220, "orange"),
                                       ColumnStyle(390, "orange"), ColumnStyle(300, "orange")),
                       strict=True))
    for index, (code, _) in enumerate(RETAILERS):
        name, header = RETAILER_NAMES[code], ("navy", "navy_alt")[index % 2]
        styles |= {
            f"{name} price (PHP)": ColumnStyle(136, header, "retailer", **_NUMBER),
            f"{name} product link": ColumnStyle(175, header, **_LINK),
            f"{name} status": ColumnStyle(300, header),
            f"{name} observed (Asia/Singapore)": ColumnStyle(175, header),
            f"{name} recheck link (unconfirmed)": ColumnStyle(180, header, **_LINK),
            f"{name} recheck note": ColumnStyle(350, header),
        }
    minimums = (ColumnStyle(155, "orange", "shopee", **_NUMBER), ColumnStyle(200, "orange"),
                ColumnStyle(300, "orange"), ColumnStyle(155, "navy", "in_stock", **_NUMBER),
                ColumnStyle(200), ColumnStyle(300))
    styles |= dict(zip(RETAILER_MINIMUM_HEADER, minimums, strict=True))
    styles |= {"Comparable retailers": ColumnStyle(112, "navy_alt", **_NUMBER)}
    for code, _ in RETAILERS:
        styles[f"{RETAILER_NAMES[code]} comparison tag"] = ColumnStyle(150, "navy_alt")
    for name in ALTERNATIVE_COMPARISON_HEADER[len(RETAILERS):]:
        number_column = "(PHP" in name or name.endswith("retailers (eligible alternatives)") or (
            "shown (matching" in name)
        styles[name] = ColumnStyle(
            155 if number_column else 300 if "differences" in name or "unknown" in name
            else 200, "navy_alt", **(_NUMBER if number_column else {}))
    for name, width in (("Comparison basis", 200), ("Shopee sales snapshot (extract date)", 155),
                        ("Reporting week (Asia/Singapore)", 210), ("Week status", 130),
                        ("Report cutoff (Asia/Singapore)", 200)):
        styles[name] = ColumnStyle(width, "navy_alt")
    return styles


def tab_layout(title: str, header: Sequence[str]) -> TabLayout | None:
    """The house layout of a price tab for its header, or None (history, review, run log)."""
    if title == COMPARISON_TAB:
        styles = _comparison_styles()
        # Rank to Model stay visible while scrolling across the retailer groups.
        return TabLayout(tuple(styles.get(n, ColumnStyle(160)) for n in header), 5, 72, 64)
    if title == SUMMARY_TAB:
        columns = [ColumnStyle(450), ColumnStyle(150, "orange", "shopee", **_NUMBER)]
        columns += [ColumnStyle(140, fill="retailer", **_NUMBER)] * (len(header) - 2)
        return TabLayout(tuple(columns[:len(header)]), 1, 64, 44)
    if title == OVERVIEW_TAB:
        styles = dict(_IDENTITY_STYLES) | {
            "Laptop": ColumnStyle(290), "Screen (in)": ColumnStyle(76, **_NUMBER),
            "CPU family": ColumnStyle(135), "RAM (GB)": ColumnStyle(72, **_NUMBER),
            "Storage (GB)": ColumnStyle(86, **_NUMBER),
            OVERVIEW_SHOPEE: ColumnStyle(150, "orange", "shopee", **_NUMBER),
            OVERVIEW_LABELS: ColumnStyle(340),
        }
        price = ColumnStyle(120, fill="retailer", **_NUMBER)
        # Rank and Laptop stay visible.
        return TabLayout(tuple(styles.get(n, price) for n in header), 2, 64, 44)
    if title == ALTERNATIVES_TAB:
        wide = ColumnStyle(330)
        styles = dict(_IDENTITY_STYLES) | {
            "Target configuration": ColumnStyle(330), "Retailer": ColumnStyle(120),
            "Actual product / model code": ColumnStyle(200), "Actual CPU series": ColumnStyle(120),
            "Actual CPU (exact processor)": ColumnStyle(150), "Actual GPU": ColumnStyle(110),
            "Price (PHP)": ColumnStyle(136, "navy", "retailer", **_NUMBER),
            "Currency": ColumnStyle(80), "Stock": ColumnStyle(110),
            "Product link": ColumnStyle(175, **_LINK),
            "Difference count": ColumnStyle(90, **_NUMBER),
            "Comparison tag": ColumnStyle(170), "Differences (target vs retailer)": wide,
            "Unknown fields": wide, "Limitations": wide, "Warnings": wide,
            "Price eligibility": ColumnStyle(220), "Observed (Asia/Singapore)": ColumnStyle(175),
            "Observation ID": ColumnStyle(190, **_LINK), "Evidence references": ColumnStyle(420),
            "Initiating target(s)": ColumnStyle(150),
            "Exact target of this offer": ColumnStyle(130),
            "Policy version": ColumnStyle(170),
        }
        # Rank and Target ID stay visible while scrolling across the specifications.
        return TabLayout(tuple(styles.get(n, ColumnStyle(130)) for n in header), 2, 64, 52)
    return None


def _format(*, background: dict, text_color: dict, mono: bool = False, align: str = "LEFT",
            wrap: str = "WRAP", border: bool = False) -> dict:
    fmt = {
        "backgroundColor": background, "backgroundColorStyle": {"rgbColor": background},
        "horizontalAlignment": align, "verticalAlignment": "MIDDLE", "wrapStrategy": wrap,
        "padding": PADDING,
        "textFormat": {"foregroundColor": text_color,
                       "foregroundColorStyle": {"rgbColor": text_color},
                       "fontFamily": "Roboto Mono" if mono else "Roboto", "fontSize": 10},
    }
    if border:
        fmt["borders"] = {"bottom": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": ORANGE},
                                     "color": ORANGE}}
    return fmt


def layout_requests(sheet_id: int | None, rows: int, layout: TabLayout) -> list[dict]:
    """The house layout of one tab; every request is scoped to its sheet ID. The managed fields
    are reset on the whole tab first, so a shorter publication leaves no stale bands."""
    width = len(layout.columns)

    def area(top: int, bottom: int, left: int, right: int) -> dict:
        return {"sheetId": sheet_id, "startRowIndex": top, "endRowIndex": bottom,
                "startColumnIndex": left, "endColumnIndex": right}

    def style(rng: dict, fmt: dict, fields: str = LAYOUT_FIELDS) -> dict:
        return {"repeatCell": {"range": rng, "cell": {"userEnteredFormat": fmt},
                               "fields": fields}}

    def groups(key) -> list[tuple[int, int, object]]:
        """Runs of adjacent columns with the same key: (left, right, key)."""
        runs: list[tuple[int, int, object]] = []
        for index, column in enumerate(layout.columns):
            value = key(column)
            if runs and runs[-1][1] == index and runs[-1][2] == value:
                runs[-1] = (runs[-1][0], index + 1, value)
            else:
                runs.append((index, index + 1, value))
        return runs

    top = layout.header_row  # banner rows come first, then the header, then the data
    requests = [style({"sheetId": sheet_id}, {})]
    if top:
        # Banner text overflows into the empty cells beside it; chips are filled by the plan.
        requests.append(style(area(0, top, 0, width), _format(
            background=WHITE, text_color=INK, wrap="OVERFLOW_CELL")))
    for left, right, header in groups(lambda c: c.header):
        requests.append(style(area(top, top + 1, left, right), _format(
            background=HEADER_FILLS[header], border=True,
            text_color=NAVY if header == "orange" else WHITE)))
    if rows > top + 1:
        for left, right, (fill, wrap, align, mono, muted) in groups(
                lambda c: (c.fill, c.wrap, c.align, c.mono, c.muted)):
            requests.append(style(area(top + 1, rows, left, right), _format(
                background=FILLS.get(fill, WHITE), text_color=MUTED if muted else INK,
                mono=mono, align=align, wrap=wrap)))
        # Muted banding on every second data row, then the price tints over it again. The
        # result fills (``fill_requests``) are written after everything here, so banding never
        # overrides a meaningful colour.
        background = "userEnteredFormat.backgroundColor,userEnteredFormat.backgroundColorStyle"
        requests += [style(area(row, row + 1, 0, width), {
            "backgroundColor": BAND_BACKGROUND,
            "backgroundColorStyle": {"rgbColor": BAND_BACKGROUND}}, background)
            for row in range(top + 2, rows, 2)]
        requests += [style(area(top + 1, rows, left, right), {
            "backgroundColor": FILLS[fill], "backgroundColorStyle": {"rgbColor": FILLS[fill]}},
            background) for left, right, fill in groups(lambda c: c.fill) if fill]
    for left, right, pixels in groups(lambda c: c.width):
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": left,
                      "endIndex": right},
            "properties": {"pixelSize": pixels}, "fields": "pixelSize"}})
    heights = [(index, index + 1, pixels) for index, pixels in enumerate(layout.preface_heights)]
    heights += [(top, top + 1, layout.header_height), (top + 1, rows, layout.row_height)]
    for first, end, pixels in heights:
        if end > first:
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": first,
                          "endIndex": end},
                "properties": {"pixelSize": pixels}, "fields": "pixelSize"}})
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
        if set(request) - {"updateCells", "updateSheetProperties", "setBasicFilter",
                           "clearBasicFilter", "repeatCell", "updateDimensionProperties",
                           "addDimensionGroup", "deleteDimensionGroup", "updateDimensionGroup",
                           "addConditionalFormatRule", "deleteConditionalFormatRule",
                           "mergeCells", "unmergeCells"}:
            raise DestinationError("A write request uses an unexpected operation.")


def _fill_matches(actual: Mapping, fill: str) -> bool:
    """The cell's read-back background is the planned status fill (to rounding)."""
    colour = (actual.get("userEnteredFormat") or {}).get("backgroundColor") or {}
    wanted = STATUS_FILLS[fill]
    return all(abs(colour.get(channel, 0) - wanted[channel]) < 0.002
               for channel in ("red", "green", "blue"))


def fill_requests(sheet_id: int | None, plan_grid: Sequence[Sequence[Cell]]) -> list[dict]:
    """``repeatCell`` requests that colour the status-filled cells, one per run of adjacent
    cells of one fill in a row. They follow the layout requests, which reset backgrounds."""
    requests = []
    for r, row in enumerate(plan_grid):
        c = 0
        while c < len(row):
            fill = row[c].fill
            if not fill:
                c += 1
                continue
            end = c
            while end + 1 < len(row) and row[end + 1].fill == fill:
                end += 1
            colour = STATUS_FILLS[fill]
            requests.append({"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                          "startColumnIndex": c, "endColumnIndex": end + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": colour,
                                               "backgroundColorStyle": {"rgbColor": colour}}},
                "fields": "userEnteredFormat.backgroundColor,"
                          "userEnteredFormat.backgroundColorStyle"}})
            c = end + 1
    return requests


def verify_tab(
    state: TabState, plan_grid: Sequence[Sequence[Cell]], *, notes: bool | None = None
) -> list[str]:
    """Compare a read-back tab with the planned cells: values, links, PHP formats, extras.
    ``notes`` overrides whether notes and italics are compared (default: ``NOTE_TABS``)."""
    issues: list[str] = []
    notes = state.title in NOTE_TABS if notes is None else notes
    for r, planned_row in enumerate(plan_grid):
        actual_row = state.grid[r] if r < len(state.grid) else ()
        for c, planned in enumerate(planned_row):
            actual = actual_row[c] if c < len(actual_row) else {}
            where = f"{state.title} row {r + 1} column {c + 1}"
            value = actual.get("userEnteredValue") or {}
            if notes and (actual.get("note") or "") != planned.note:
                issues.append(f"{where} note differs")
            if notes and bool(actual.get("userEnteredFormat", {}).get("textFormat", {}).get(
                    "italic")) != planned.italic:
                issues.append(f"{where} italic differs")
            if planned.fill and not _fill_matches(actual, planned.fill):
                issues.append(f"{where} fill differs")
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
            elif is_php(planned.number_format):
                shown = actual.get("userEnteredFormat", {}).get("numberFormat", {})
                if shown.get("type") != "CURRENCY":
                    issues.append(f"{where} lost its PHP format")
                elif planned.number_format != "php" and shown.get("pattern") != (
                        NUMBER_FORMATS[planned.number_format]["pattern"]):
                    issues.append(f"{where} price label format differs")
            link = actual.get("hyperlink") or actual.get("userEnteredFormat", {}).get(
                "textFormat", {}
            ).get("link", {}).get("uri", "")
            # Sheets can answer an internal link (``#gid=..&range=..``) with its full address.
            same = (link or "") == planned.link or (
                planned.link.startswith("#gid=") and (link or "").endswith(planned.link))
            if not same:
                issues.append(f"{where} link differs")
        for c in range(len(planned_row), len(actual_row)):
            if _has_content(actual_row[c], notes=notes):
                issues.append(f"{state.title} row {r + 1} has an unexpected extra cell")
    for r in range(len(plan_grid), len(state.grid)):
        if any(_has_content(cell, notes=notes) for cell in state.grid[r]):
            issues.append(f"{state.title} row {r + 1} has unexpected content")
    return issues


def verify_layout(
    spreadsheet, plan: TabPlan, *, layout_fn=None, allowed: Sequence[str] = PUBLISHER_TABS
) -> list[str]:
    """Frozen panes and column widths of a house-layout tab, as read back."""
    layout = (layout_fn or tab_layout)(plan.title, plan.header)
    if layout is None:
        return []
    snapshot = snapshot_tab(spreadsheet, plan.title, allowed) or {}
    grid = snapshot.get("properties", {}).get("gridProperties", {})
    issues = []
    if (grid.get("frozenRowCount", 0), grid.get("frozenColumnCount", 0)) != (
            layout.header_row + 1, layout.frozen_columns):
        issues.append(f"{plan.title} frozen rows/columns differ from the layout")
    if layout.manage_columns:
        found = sorted(
            (g["range"]["startIndex"], g["range"]["endIndex"], g.get("depth", 1),
             bool(g.get("collapsed")))
            for g in snapshot.get("columnGroups", []))
        planned = sorted((g["range"]["startIndex"], g["range"]["endIndex"], 1, True)
                         for g in layout.planned_groups())
        if found != planned:
            issues.append(f"{plan.title} column groups differ from the layout "
                          f"({len(found)} found, {len(planned)} planned)")
        if snapshot.get("conditionalFormats"):
            issues.append(f"{plan.title} still has {len(snapshot['conditionalFormats'])} "
                          "conditional-format rule(s) from an earlier layout")
    widths = (snapshot.get("data") or [{}])[0].get("columnMetadata", [])
    for index, column in enumerate(layout.columns):
        if index < len(widths) and widths[index].get("pixelSize") not in (None, column.width):
            issues.append(f"{plan.title} column {_column_letter(index)} width differs")
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
    adopt: Sequence[str] = (),
) -> PublicationOutcome:
    """Back up, write, read back, and log. ``SUCCESS`` only after every check passes.

    ``adopt`` names owner-built tabs (``ADOPTABLE_HEADERS``) the caller explicitly allows this
    publication to replace; any other tab with content this publisher did not write is refused.
    """

    def step(message: str) -> None:
        if progress is not None:
            progress(message)

    step("inspecting destination tabs")
    refuse_variation_workbook(spreadsheet)
    states = {title: read_tab(spreadsheet, title) for title in PUBLISHER_TABS}
    headers = {title: plan.tabs[title].header for title in DATA_TABS} | {
        RUN_LOG_TAB: RUN_LOG_HEADER
    }
    problems, adopted = destination_problems(states, headers, adopt)
    if problems:
        raise DestinationError("; ".join(problems))
    plan.adopted = tuple(adopted)

    # Back up every existing destination tab locally (a full snapshot) before any change.
    backup_dir.mkdir(parents=True, exist_ok=True)
    for title, state in states.items():
        if state.exists and state.used_rows:
            write_backup(backup_dir, state, snapshot_tab(spreadsheet, title))

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
                "Rerun the same --write-sheets command; it replaces all publisher tabs.")
        except Exception as log_exc:  # pragma: no cover - reported to the caller
            error += f"; run-log update also failed ({type(log_exc).__name__})"
        return PublicationOutcome("PARTIAL_FAILURE", written, [], tab_ids, error)

    issues = []
    for title in DATA_TABS:
        step(f"reading back {title}")
        issues += verify_tab(read_tab(spreadsheet, title), plan.tabs[title].grid())
        issues += verify_layout(spreadsheet, plan.tabs[title])
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
