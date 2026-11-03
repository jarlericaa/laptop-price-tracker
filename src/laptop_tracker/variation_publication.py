"""Manual Google Sheets publication of the Shopee-variation reports (``shopee-variation-sheets-1``).

Row grain ``SHOPEE_VARIATION``: one row per selected Shopee seller variation. The publisher reads
saved, reviewed report files only (never fetches, rematches, imports or opens the observation
store) and writes exactly the six data tabs and the run log named in
``config/shopee_variation_sheet_contract.json`` -- the exact headers the owner prepared in the live
workbook. The contract, not the legacy publisher's column positions, defines every header; the live
headers are read before anything is planned for a write and must equal the contract.

Hard rules:

- Only existing, prepared template tabs are written. A missing tab is refused, never created, and
  the retired tabs (``PILOT_PRICE_SUMMARY``, ``P1_TOP_100_CONFIGURATIONS``) are never read as a
  destination, written, restored or recreated. ``raw``, ``MAIN``, ``TOP SHOPS``, the catalog tabs,
  Phase 1 tabs and the ``ARCHIVE_*`` tabs are not in the allowlist.
- Reports of another grain (the legacy configuration outputs) are rejected before any Sheets
  access, and a template tab that still holds legacy configuration-grain content is refused.
- PILOT_PRICE_HISTORY and PILOT_PRICE_REVIEW keep their earlier columns and values; the contract's
  grain/association columns are appended. Old records carry the explicit legacy grain
  ``TARGET_CONFIGURATION`` and empty association fields; no source identity is guessed for them.
- Prices stay numeric. Tags, notes and actual configuration differences are separate cells.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .price_tracking import (
    ALTERNATIVE_SELECTED,
    COMPARABLE,
    SELECTED,
)
from .sheet_publication import (
    BASIS_TEXT,
    BASIS_WORDS,
    BLANK,
    NUMBER_FORMATS,
    RETAILER_NAMES,
    RETIRED_TABS,
    STATUS_LEGEND,
    UNPRICED_STATUS_TEXT,
    Cell,
    ColumnStyle,
    DestinationError,
    PublicationInputError,
    TabLayout,
    TabPlan,
    TabState,
    _alternative_configuration,
    _codes,
    _local,
    _minimum_basis,
    _minimum_retailers,
    _named,
    _reason_text,
    _status_text,
    _stock_text,
    _utc,
    cell_tag,
    grid_sha256,
    guard_requests,
    history_plan,
    load_pilot_report,
    number,
    read_tab,
    review_plan,
    snapshot_tab,
    tab_requests,
    tag_fill,
    text,
    verify_layout,
    verify_tab,
    write_backup,
)
from .variation_ranking import (
    CONTEXT_COLUMNS,
    LEGACY_ROW_GRAIN,
    LIST_NAME,
    MAPPED_VERIFIED,
    ROW_GRAIN,
    SCHEMA_VERSION,
    SELECTION_POLICY,
    VARIATION_COLUMNS,
)
from .variation_reports import (
    HISTORY_ASSOCIATION_COLUMNS,
    REPORT_FILES,
    VARIATION_ALTERNATIVE_COLUMNS,
    VARIATION_COMPARISON_COLUMNS,
    VARIATION_RECHECK_COLUMNS,
    VARIATION_SELECTION_COLUMNS,
    sha256_file,
)
from .verified_top import VerifiedRankingInputError, read_csv_rows

CONTRACT_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "shopee_variation_sheet_contract.json"
)
PUBLISHER_VERSION = "variation-sheets-1"

TOP_TAB = "TOP_100_SHOPEE_VARIATIONS"
COMPARISON_TAB = "PILOT_PRICE_COMPARISON"
OVERVIEW_TAB = "PILOT_PRICE_OVERVIEW"
ALTERNATIVES_TAB = "PILOT_PRICE_ALTERNATIVES"
HISTORY_TAB = "PILOT_PRICE_HISTORY"
REVIEW_TAB = "PILOT_PRICE_REVIEW"
RUN_LOG_TAB = "PILOT_PRICE_RUN_LOG"
# The exact destination allowlist, compared exactly (no trimming or case folding).
DATA_TABS: tuple[str, ...] = (
    TOP_TAB,
    COMPARISON_TAB,
    OVERVIEW_TAB,
    ALTERNATIVES_TAB,
    HISTORY_TAB,
    REVIEW_TAB,
)
PUBLISHER_TABS: tuple[str, ...] = (*DATA_TABS, RUN_LOG_TAB)
assert not set(PUBLISHER_TABS) & set(RETIRED_TABS)

RETAILER_ORDER = ("DBZ", "LF", "PCX", "PCC", "OCT")
OVERVIEW_ORDER = ("DBZ", "PCX", "LF", "PCC", "OCT")

NUMBER_FORMATS.setdefault("usd", {"type": "CURRENCY", "pattern": '"$"#,##0.00'})


# Contract


@dataclass(frozen=True)
class Contract:
    schema_version: str
    ranking_grain: str
    headers: Mapping[str, tuple[str, ...]]  # tab -> header the new/reset tabs carry
    appended: Mapping[str, tuple[str, ...]]  # tab -> columns appended to the existing header
    retired: tuple[str, ...]


def load_contract(path: Path = CONTRACT_PATH) -> Contract:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["schema_version"] != SCHEMA_VERSION or data["ranking_grain"] != ROW_GRAIN:
        raise PublicationInputError(["the sheet contract is not shopee-variation-sheets-1"])
    retired = tuple(data["retired_tabs"])
    if set(retired) != set(RETIRED_TABS):
        raise PublicationInputError(["the contract's retired tabs differ from the publisher's"])
    return Contract(
        data["schema_version"],
        data["ranking_grain"],
        {tab: tuple(header) for tab, header in data["new_or_reset_headers"].items()},
        {tab: tuple(header) for tab, header in data["append_to_existing_headers"].items()},
        retired,
    )


# Legacy headers of the tabs whose earlier columns are kept (the pilot-sheets-7 layouts).


LEGACY_HISTORY_HEADER: tuple[str, ...] = (
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
LEGACY_REVIEW_HEADER: tuple[str, ...] = (
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


def legacy_run_log_header() -> tuple[str, ...]:
    from .sheet_publication import RUN_LOG_HEADER

    return tuple(RUN_LOG_HEADER)


def known_run_log_bases() -> tuple[tuple[str, ...], ...]:
    """Every run-log header an earlier publisher version wrote (pilot-sheets-4 to -7)."""
    from .sheet_publication import (
        RUN_LOG_HEADER,
        RUN_LOG_HEADER_PILOT_SHEETS_4,
        RUN_LOG_HEADER_PILOT_SHEETS_5,
        RUN_LOG_HEADER_PILOT_SHEETS_6,
    )

    return (
        tuple(RUN_LOG_HEADER_PILOT_SHEETS_4),
        tuple(RUN_LOG_HEADER_PILOT_SHEETS_5),
        tuple(RUN_LOG_HEADER_PILOT_SHEETS_6),
        tuple(RUN_LOG_HEADER),
    )


def resolve_run_log_header(state: TabState, contract: Contract) -> tuple[str, ...] | None:
    """The run-log header to write, read from the live tab instead of assumed positions.

    The contract appends its columns to the *existing* header, which can be any earlier publisher
    layout (the live workbook still has the pilot-sheets-6 columns, without the pilot-sheets-7
    alternative counts). A live header that is a known layout plus the appended columns is kept
    as it is; a known layout without them gets them appended; an empty tab gets the newest layout.
    Rows are filled by column name, so a column the tab does not have is simply not written.
    Returns None for an unrecognised header.
    """
    appended = tuple(contract.appended[RUN_LOG_TAB])
    live = state.header() if state.exists else []
    while live and not live[-1]:
        live.pop()
    if not live:
        return (*legacy_run_log_header(), *appended)
    for base in known_run_log_bases():
        if tuple(live) == base:
            return (*base, *appended)
        if tuple(live) == (*base, *appended):
            return tuple(live)
    return None


# Loading the reports


@dataclass
class VariationPublicationReport:
    variation_dir: Path
    reports_dir: Path
    top: list[dict[str, str]]
    contexts: list[dict[str, str]]
    comparison: list[dict[str, str]]
    selections: dict[tuple[str, str], dict[str, str]]
    alternatives: list[dict[str, str]]
    rechecks: list[dict[str, str]]
    associations: list[dict[str, str]]
    coverage: dict
    summary: dict
    top_summary: dict
    legacy: object  # sheet_publication.PilotReport of the context report (history and review rows)
    fingerprints: dict[str, str]

    @property
    def week_start(self) -> date:
        return date.fromisoformat(self.summary["report_week"]["start"])

    @property
    def week_end(self) -> date:
        return date.fromisoformat(self.summary["report_week"]["end_exclusive"])

    @property
    def as_of(self) -> datetime:
        return _utc(self.summary["as_of_utc"])

    @property
    def counts(self) -> dict:
        return self.coverage["counts"]


def load_variation_report(
    variation_dir: Path,
    reports_dir: Path,
    context_report_dir: Path,
    *,
    collection_dir: Path | None = None,
) -> VariationPublicationReport:
    """Read and validate every input; nothing is partly accepted.

    Raises ``PublicationInputError`` for a wrong grain (legacy configuration outputs), a different
    schema, inconsistent files or a context report that was not built from this run's contexts.
    """
    issues: list[str] = []
    files = {key: reports_dir / name for key, name in REPORT_FILES.items()}
    legacy_marker = reports_dir / "weekly_price_comparison.csv"
    if not files["summary"].is_file() and legacy_marker.is_file():
        raise PublicationInputError(
            [
                f"{reports_dir} holds legacy configuration-grain outputs ({LEGACY_ROW_GRAIN}); the "
                f"{SCHEMA_VERSION} templates take only variation-grain reports built by "
                "build_variation_reports.py"
            ]
        )
    for path in files.values():
        if not path.is_file():
            issues.append(f"missing variation report file {path.name}")
    for name in (
        "top_100_shopee_variations.csv",
        "variation_contexts.csv",
        "context_scope.csv",
        "context_selected_list.csv",
        "shopee_variation_run_summary.json",
    ):
        if not (variation_dir / name).is_file():
            issues.append(f"missing variation ranking file {name}")
    if issues:
        raise PublicationInputError(issues)
    try:
        summary = json.loads(files["summary"].read_text(encoding="utf-8"))
        top_summary = json.loads(
            (variation_dir / "shopee_variation_run_summary.json").read_text(encoding="utf-8")
        )
        coverage = json.loads(files["coverage"].read_text(encoding="utf-8"))
        top = read_csv_rows(
            variation_dir / "top_100_shopee_variations.csv",
            VARIATION_COLUMNS,
            "Selected variations",
        )
        contexts = read_csv_rows(
            variation_dir / "variation_contexts.csv", CONTEXT_COLUMNS, "Variation contexts"
        )
        comparison = read_csv_rows(
            files["comparison"], VARIATION_COMPARISON_COLUMNS, "Variation comparison"
        )
        selection_rows = read_csv_rows(
            files["selections"], VARIATION_SELECTION_COLUMNS, "Variation selections"
        )
        alternatives = read_csv_rows(
            files["alternatives"], VARIATION_ALTERNATIVE_COLUMNS, "Variation alternatives"
        )
        rechecks = read_csv_rows(files["rechecks"], VARIATION_RECHECK_COLUMNS, "Variation rechecks")
        associations = read_csv_rows(
            files["history"], HISTORY_ASSOCIATION_COLUMNS, "Variation history associations"
        )
    except (VerifiedRankingInputError, ValueError, KeyError) as exc:
        raise PublicationInputError([f"unreadable variation report ({exc})"]) from None
    for label, found in (
        ("variation report summary", summary),
        ("selected-variation run summary", top_summary),
    ):
        if found.get("schema_version") != SCHEMA_VERSION or found.get("ranking_grain") != ROW_GRAIN:
            issues.append(f"the {label} is not {SCHEMA_VERSION} / {ROW_GRAIN}")
    for name, rows in (
        ("comparison", comparison),
        ("selections", selection_rows),
        ("alternatives", alternatives),
        ("rechecks", rechecks),
        ("history associations", associations),
    ):
        bad = {row["row_grain"] for row in rows} - {ROW_GRAIN}
        if bad:
            issues.append(f"the {name} report has rows of grain {', '.join(sorted(bad))}")
    # Owner decision 2026-11-03: the published list holds only eligible mapped variations.
    if top_summary.get("selection_policy") != SELECTION_POLICY:
        issues.append(
            f"the selected-variation run was not built under the {SELECTION_POLICY} policy "
            f"(rebuild it with build_top_shopee_variations.py); {LIST_NAME} take only "
            "verified-mapped variations"
        )
    unverified = [r["source_key"] for r in top if r["mapping_status"] != MAPPED_VERIFIED]
    if unverified:
        issues.append(
            f"{len(unverified)} selected variations are not MAPPED_VERIFIED "
            f"(first: {unverified[0]})"
        )
    if issues:
        raise PublicationInputError(issues)

    if len(comparison) != len(top) or [r["source_key"] for r in comparison] != [
        r["source_key"] for r in top
    ]:
        issues.append("comparison rows differ from the selected variations (rows or order)")
    if len({r["comparison_row_id"] for r in comparison}) != len(comparison):
        issues.append("comparison report repeats a comparison_row_id")
    list_sha = sha256_file(variation_dir / "context_selected_list.csv")
    if summary.get("context_list_sha256") != list_sha:
        issues.append("the variation report was built from a different context list")
    if {r["selected_list_sha256"] for r in comparison} != {list_sha}:
        issues.append("comparison rows name a different context list")
    if issues:
        raise PublicationInputError(issues)

    try:
        legacy = load_pilot_report(
            context_report_dir,
            variation_dir / "context_scope.csv",
            collection_dir=collection_dir,
            selected_list=variation_dir / "context_selected_list.csv",
        )
    except PublicationInputError as exc:
        raise PublicationInputError([f"context report: {issue}" for issue in exc.issues]) from None
    if (
        sha256_file(context_report_dir / "price_tracking_run_summary.json")
        != summary["context_report_sha256"]["price_tracking_run_summary.json"]
    ):
        raise PublicationInputError(
            ["the context report changed since the variation report was built"]
        )
    selections = {(r["comparison_row_id"], r["retailer_code"]): r for r in selection_rows}
    if len(selections) != len(selection_rows):
        raise PublicationInputError(["selections repeat a row and retailer"])
    fingerprints = {f"variation/{path.name}": sha256_file(path) for path in files.values()}
    fingerprints["variation/top_100_shopee_variations.csv"] = sha256_file(
        variation_dir / "top_100_shopee_variations.csv"
    )
    fingerprints["variation/context_selected_list.csv"] = list_sha
    fingerprints |= {f"context/{k}": v for k, v in summary["context_report_sha256"].items()}
    return VariationPublicationReport(
        variation_dir,
        reports_dir,
        top,
        contexts,
        comparison,
        selections,
        alternatives,
        rechecks,
        associations,
        coverage,
        summary,
        top_summary,
        legacy,
        fingerprints,
    )


# Cell helpers


def seller_name(row: Mapping[str, str]) -> str:
    """The Shopee seller as recorded, else the shop ID (never an invented name)."""
    return row.get("shop_name") or f"Shop {row['shop_id']}"


def laptop_name(row: Mapping[str, str]) -> str:
    """Brand and model of the context when there is one; otherwise the listing's own title."""
    named = " ".join(part for part in (row["brand"], row["series"], row["model"]) if part)
    return named or row["item_name"]


def week_text(report: VariationPublicationReport) -> str:
    return f"{report.week_start.isoformat()} to {(report.week_end - timedelta(days=1)).isoformat()}"


_SHOPEE_STATUS_TEXT = {
    "SNAPSHOT_PRICE": "Own variation's snapshot price (sales extract)",
    "SNAPSHOT_PRICE_STALE": "Own variation's snapshot price, STALE (older than 7 days at cutoff)",
    "SNAPSHOT_AFTER_CUTOFF": "No price: the sales extract is later than the report cutoff",
    "PRICE_CONFLICT": "No price: conflicting month-to-date prices in the extract",
    "PRICE_MISSING": "No price: blank in the extract",
    "PRICE_INVALID": "No price: invalid in the extract",
}

_ROW_STATUS_TEXT = {
    "CONFIGURATION_REVIEW_PENDING": "Configuration review pending — no trusted comparison "
    "context; no retailer price",
    "SOURCE_CONFLICT_COMPARISON_BLOCKED": "Source conflict — retailer comparison blocked; the "
    "listing stays ranked",
}


def _shopee_status(row: Mapping[str, str]) -> str:
    status = _SHOPEE_STATUS_TEXT.get(row["shopee_price_status"], row["shopee_price_status"])
    detail = row["shopee_price_detail"]
    if row["shopee_price_php"]:
        detail = "; ".join(
            filter(
                None,
                (detail, "not a current price; stock not verified; this variation's own price"),
            )
        )
    return f"{status} ({detail})" if detail else status


def _price_observed(row: Mapping[str, str]) -> str:
    when = row["shopee_price_observed_at"]
    return f"{when} (sales extract date; no price time recorded)" if when else ""


def retailer_status_text(row: Mapping[str, str], code: str, selection: Mapping[str, str]) -> str:
    """The status text of one retailer cell, led by its comparison tag (colours repeat it)."""
    status = row[f"{code}_Price_Status"]
    if not row["context_id"]:
        return _ROW_STATUS_TEXT.get(row["row_comparison_status"], row["row_comparison_status"]) + (
            f": {row['row_comparison_detail']}" if row["row_comparison_detail"] else ""
        )
    tag = cell_tag(row, code)
    body = _status_text(
        status,
        selection.get("not_comparable_reasons", ""),
        row.get(f"{code}_Stock", ""),
        row.get(f"{code}_Match_Basis", ""),
        row.get(f"{code}_Warnings", ""),
        row.get(f"{code}_Offer_Details", ""),
        tag,
        row.get(f"{code}_Alternative_Differences", ""),
        row.get(f"{code}_Alternative_Unknowns", ""),
    )
    if status in UNPRICED_STATUS_TEXT and row.get(f"{code}_Reason"):
        body += f": {row[f'{code}_Reason']}"
    return (
        f"{tag} — {body}"
        if tag and (status in {SELECTED, ALTERNATIVE_SELECTED} or tag == "RECHECK")
        else body
    )


def _recheck_cells(row: Mapping[str, str], code: str) -> list[Cell]:
    url = row.get(f"{code}_Candidate_URL", "")
    if row[f"{code}_Price"] or not url:
        return [BLANK, BLANK]
    note = row.get(f"{code}_Recheck_Note") or row.get(f"{code}_Candidate_Detail", "")
    count = int(row.get(f"{code}_Recheck_Count") or 1)
    if count > 1:
        note += f" (+{count - 1} more in {REVIEW_TAB})"
    return [text(url, link=url), text(note)]


def _alternative_basis(row: Mapping[str, str], prefix: str) -> str:
    """Basis text of an alternatives-only minimum: label, tags, differences and unknowns."""
    if not row.get(prefix):
        return ""
    label = row.get(f"{prefix}_Label", "")
    parts = [
        label,
        _named(row.get(f"{prefix}_Tag", "")),
        "differences: " + _named(row.get(f"{prefix}_Differences", ""), " | ")
        if row.get(f"{prefix}_Differences")
        else "",
        "unknown: " + _named(row.get(f"{prefix}_Unknowns", ""), " | ")
        if row.get(f"{prefix}_Unknowns")
        else "",
    ]
    return "; ".join(part for part in parts if part)


def _alternative_retailers(
    report: VariationPublicationReport,
) -> dict[str, list[str]]:
    """``comparison_row_id -> retailer names`` with an eligible alternative (the full pool)."""
    found: dict[str, set[str]] = defaultdict(set)
    for alt in report.alternatives:
        if alt["price_eligibility"] == COMPARABLE:
            found[alt["comparison_row_id"]].add(alt["retailer_code"])
    return {
        key: [RETAILER_NAMES[c] for c in RETAILER_ORDER if c in codes]
        for key, codes in found.items()
    }


# Tab plans


def _missing_month_note(name: str, row: Mapping[str, str]) -> str:
    """A note on a GMV cell whose month has no record: the 0 is ranking arithmetic, not data."""
    status = {
        "m_minus_1_gmv_usd": row.get("m_minus_1_status"),
        "mtd_gmv_usd": row.get("mtd_status"),
    }
    if status.get(name) != "NO_RECORD_TREATED_AS_ZERO":
        return ""
    month = row["m_minus_1_month"] if name == "m_minus_1_gmv_usd" else row["mtd_month"]
    return (
        f"No {month} record for this variation in the sales extract. The 0 is ranking arithmetic "
        "only; it is not an observed zero and does not show the listing was inactive."
    )


def top_plan(report: VariationPublicationReport, header: Sequence[str]) -> TabPlan:
    integer = {"rank", "ram_capacity_gb", "storage_capacity_gb"}
    decimal = {"screen_size_in"}
    usd = {"m_minus_1_gmv_usd", "mtd_gmv_usd", "ranking_gmv_usd"}
    rows = []
    for row in report.top:
        cells = []
        for name in header:
            value = row[name]
            if name in integer and value.isdigit():
                cells.append(number(value, "integer"))
            elif name in decimal and value:
                cells.append(number(value, "decimal"))
            elif name in usd:
                cells.append(number(value, "usd", note=_missing_month_note(name, row)))
            elif name == "shopee_current_price_php":
                cells.append(number(value, "php"))
            elif name == "shop_name":
                cells.append(text(seller_name(row)))
            else:
                cells.append(text(value))
        rows.append(tuple(cells))
    scope = report.top[0]["rank_metric_scope"] if report.top else ""
    notes = {
        "rank": f"{LIST_NAME}: one row per eligible Shopee seller variation with a verified "
        "configuration mapping, ranked within that pool by its own ranking_gmv_usd (M-1 + MTD, "
        "the Phase 1 metric). Not a configuration ranking.",
        "ranking_gmv_usd": scope,
        "shopee_current_price_php": "This variation's own latest valid snapshot price: the MTD "
        "record's (dated by the sales extract) or, when it has no MTD price, its M-1 record's. "
        "Never a current price or another seller's.",
        "shopee_price_observed_at": "Date of the price record: the sales extract date for an MTD "
        "price, the M-1 monthly record date for an M-1 price (price_basis in the local files). "
        "No per-price observation time exists.",
        "mapping_status": "MAPPED_VERIFIED for every listed row: the source model ID resolves to "
        "one active, complete configuration target. Unmapped or untrusted variations are "
        "excluded before the 100-row limit.",
        "comparison_status": "COMPARISON_CONTEXT_READY for every listed row.",
        "brand": "Specifications are the mapped target's, shown only for a trusted context.",
    }
    return TabPlan(TOP_TAB, tuple(header), tuple(rows), tuple(notes.get(n, "") for n in header))


def comparison_plan(report: VariationPublicationReport, header: Sequence[str]) -> TabPlan:
    alt_retailers = _alternative_retailers(report)
    week, status, cutoff = (
        week_text(report),
        report.summary["report_week"]["status"],
        _local(report.as_of),
    )
    rows = []
    for row in report.comparison:
        cells: dict[str, Cell] = {
            "Rank": number(row["rank"], "integer"),
            "Shopee seller": text(seller_name(row)),
            "Shopee item": text(row["item_name"]),
            "Shopee variation": text(row["shopee_model_name"]),
            "Brand": text(row["brand"]),
            "Series": text(row["series"]),
            "Model": text(row["model"]),
            "Screen (in)": number(row["screen_size_in"], "decimal"),
            "CPU family": text(row["cpu_series"]),
            "CPU model": text(row["cpu_model"]),
            "RAM (GB)": number(row["ram_capacity_gb"], "integer"),
            "Storage (GB)": number(row["storage_capacity_gb"], "integer"),
            "GPU": text(row["gpu_model"]),
            "Shopee price (PHP, snapshot)": number(row["shopee_price_php"], "php"),
            "Shopee listing link": text(row["shopee_listing_url"], link=row["shopee_listing_url"]),
            "Shopee price status": text(_shopee_status(row)),
            "Shopee price observed (Asia/Singapore)": text(_price_observed(row)),
        }
        for code in RETAILER_ORDER:
            name = RETAILER_NAMES[code]
            selection = report.selections[(row["comparison_row_id"], code)]
            url = row[f"{code}_Offer_URL"]
            tag = cell_tag(row, code) if row["context_id"] else ""
            priced = bool(row[f"{code}_Price"])
            fill = tag_fill(tag, priced=priced)
            price_cell = number(row[f"{code}_Price"], "php")
            if fill in {"match", "alt1", "alt2"} and priced:
                price_cell = replace(price_cell, fill=fill)
            recheck_fill = "recheck" if tag == "RECHECK" and not priced else ""
            cells |= {
                f"{name} price (PHP)": price_cell,
                f"{name} product link": text(url, link=url),
                f"{name} status": text(
                    retailer_status_text(row, code, selection),
                    fill=fill if priced else recheck_fill,
                ),
                f"{name} observed (Asia/Singapore)": text(row[f"{code}_Observed_At"]),
            }
            link, note = _recheck_cells(row, code)
            cells |= {f"{name} recheck link (unconfirmed)": link, f"{name} recheck note": note}
        cells |= {
            "Lowest matching retailer price (PHP)": number(row["Lowest_Price"], "php"),
            "Lowest matching retailer(s)": text(
                _minimum_retailers(row["Lowest_Price_Retailers"], row.get("Lowest_Price_Notes", ""))
            ),
            "Lowest matching retailer price basis": text(
                _minimum_basis(row.get("Lowest_Price_Label", ""), row.get("Lowest_Price_Basis", ""))
            ),
            "Lowest in-stock matching retailer price (PHP)": number(
                row["Lowest_In_Stock_Price"], "php"
            ),
            "Lowest in-stock matching retailer(s)": text(
                _minimum_retailers(
                    row["Lowest_In_Stock_Price_Retailers"], row.get("Lowest_In_Stock_Notes", "")
                )
            ),
            "Lowest in-stock matching retailer price basis": text(
                _minimum_basis(
                    row.get("Lowest_In_Stock_Label", ""), row.get("Lowest_In_Stock_Basis", "")
                )
            ),
            "Lowest alternative retailer price (PHP)": number(
                row["Lowest_Alternative_Price"], "php"
            ),
            "Lowest alternative retailer(s)": text(
                _minimum_retailers(
                    row["Lowest_Alternative_Price_Retailers"],
                    row.get("Lowest_Alternative_Price_Notes", ""),
                )
            ),
            "Lowest alternative retailer price basis": text(
                _alternative_basis(row, "Lowest_Alternative_Price")
            ),
            "Lowest in-stock alternative retailer price (PHP)": number(
                row["Lowest_Alternative_In_Stock_Price"], "php"
            ),
            "Lowest in-stock alternative retailer(s)": text(
                _minimum_retailers(
                    row["Lowest_Alternative_In_Stock_Price_Retailers"],
                    row.get("Lowest_Alternative_In_Stock_Price_Notes", ""),
                )
            ),
            "Lowest in-stock alternative retailer price basis": text(
                _alternative_basis(row, "Lowest_Alternative_In_Stock_Price")
            ),
            "Matching retailers": text(
                ", ".join(
                    RETAILER_NAMES[c]
                    for c in RETAILER_ORDER
                    if row[f"{c}_Price_Status"] == SELECTED
                )
            ),
            "Alternative retailers": text(
                ", ".join(alt_retailers.get(row["comparison_row_id"], []))
            ),
            "Comparison basis": text(BASIS_WORDS.get(row.get("Comparison_Basis", ""), "")),
            "Comparison row ID": text(row["comparison_row_id"]),
            "Source key": text(row["source_key"]),
            "Shop ID": text(row["shop_id"]),
            "Item ID": text(row["item_id"]),
            "Source model ID": text(row["source_model_id"]),
            "Target ID": text(row["target_id"]),
            "Mapping status": text(row["mapping_status"]),
            "Row comparison status": text(row["row_comparison_status"]),
            "M-1 GMV (USD)": number(row["m_minus_1_gmv_usd"], "usd"),
            "MTD GMV (USD)": number(row["mtd_gmv_usd"], "usd"),
            "Ranking GMV (USD)": number(row["ranking_gmv_usd"], "usd"),
            "Shopee sales snapshot (extract date)": text(row["sales_extract_date"]),
            "Sales run ID": text(row["sales_run_id"]),
            "Ranking metric scope": text(row["rank_metric_scope"]),
            "Reporting week (Asia/Singapore)": text(week),
            "Week status": text(status),
            "Report cutoff (Asia/Singapore)": text(cutoff),
            "Row grain": text(row["row_grain"]),
        }
        missing = [name for name in header if name not in cells]
        if missing:
            raise PublicationInputError([f"comparison plan has no value for {missing[0]!r}"])
        rows.append(tuple(cells[name] for name in header))
    return TabPlan(COMPARISON_TAB, tuple(header), tuple(rows))


def _overview_note(
    report: VariationPublicationReport,
    row: Mapping[str, str],
    code: str,
    alternatives: Mapping[tuple[str, str, str], Mapping[str, str]],
) -> str:
    """Note of one overview retailer cell: the tag and its meaning, basis, the actual
    configuration, differences, unknowns, stock, evidence and links. No price, no recheck: none."""
    if not row["context_id"]:
        return ""
    name, tag = RETAILER_NAMES[code], cell_tag(row, code)
    priced = bool(row[f"{code}_Price"])
    if not priced and tag != "RECHECK":
        return ""
    url = row.get(f"{code}_Offer_URL", "")
    selection = report.selections[(row["comparison_row_id"], code)]
    obs = selection.get("observation_id", "")
    row_ref = f"Comparison row {row['comparison_row_id']} (rank {row['rank']})"
    if tag == "RECHECK":
        lines = [
            f"{name}: RECHECK — unresolved evidence; no confirmed comparison price.",
            retailer_status_text(row, code, selection) + ".",
        ]
        note = row.get(f"{code}_Recheck_Note") or row.get(f"{code}_Candidate_Detail", "")
        if note:
            lines.append(f"Why: {note}")
        if url:
            lines.append(f"Page to recheck (unconfirmed, not a price): {url}")
        lines.append(f"Detailed record: {COMPARISON_TAB} and {REVIEW_TAB} ({row_ref}).")
        return "\n".join(lines)
    alt = alternatives.get((row["comparison_row_id"], code, obs)) if tag.startswith("ALT") else None
    if tag.startswith("ALT"):
        lines = [
            f"{name}: {tag} — close alternative, NOT a matching configuration.",
            "Matching basis: none (ALTERNATIVE; at most two established differences from the "
            f"mapped configuration: {laptop_name(row)}).",
            "Actual configuration: "
            + (
                _alternative_configuration(alt)
                if alt
                else row.get(f"{code}_Offer_Details", "") or "see the link"
            ),
            f"Differences: {row.get(f'{code}_Alternative_Differences', '')}",
        ]
        if row.get(f"{code}_Alternative_Unknowns"):
            lines.append(f"Unknown / limited: {row[f'{code}_Alternative_Unknowns']}")
        if alt and alt["warnings"]:
            lines.append("Notes: " + _reason_text(_codes(alt["warnings"])))
    else:
        if row[f"{code}_Match_Basis"] == "LISTED_SPEC":
            lines = [
                f"{name}: MATCH — matching configuration (listed-spec match).",
                "Matching basis: listed specs — brand, family, screen, CPU series, installed "
                "RAM and storage agree; the specific Shopee model is unknown.",
            ]
        else:
            detail = ", ".join(
                BASIS_TEXT[t] for t in row[f"{code}_Match_Basis"].split("+") if t in BASIS_TEXT
            )
            lines = [
                f"{name}: MATCH — matching configuration (exact product match).",
                "Matching basis: exact product" + (f" ({detail})" if detail else "") + ".",
            ]
        lines.append(f"Configuration: the mapped target {row['target_id']}, {laptop_name(row)}.")
        warnings = [
            w
            for w in _codes(row.get(f"{code}_Warnings", ""))
            if w != "SPECIFIC_IDENTITY_UNRESOLVED"
        ]
        if warnings:
            lines.append("Notes: " + _reason_text(warnings) + ".")
        hidden = row.get(f"{code}_Alternative_Offers", "")
        if hidden not in {"", "0"}:
            lines.append(
                f"{hidden} close alternative offer(s) at this retailer are also listed "
                f"in {ALTERNATIVES_TAB}."
            )
    lines += [
        f"Stock: {_stock_text(row.get(f'{code}_Stock', ''))}.",
        f"Observed: {row.get(f'{code}_Observed_At', '')} (Asia/Singapore).",
        f"Offer: {url}",
        f"Evidence: observation {obs}; association {selection.get('association_id', '')}; "
        f"retailer context {row['context_id']} (shared by every variation row of that "
        "configuration; the observation is stored once).",
        f"Detailed record: {COMPARISON_TAB}"
        + (f" and {ALTERNATIVES_TAB}" if alt else "")
        + f" ({row_ref}).",
    ]
    return "\n".join(lines)


def overview_labels(row: Mapping[str, str]) -> str:
    """``DataBlitz: ALT 1 · RAM; PC Express: MATCH``: the text of every colour."""
    return "; ".join(
        f"{RETAILER_NAMES[c]}: {cell_tag(row, c)}"
        for c in OVERVIEW_ORDER
        if row["context_id"] and cell_tag(row, c)
    )


def overview_plan(report: VariationPublicationReport, header: Sequence[str]) -> TabPlan:
    alternatives = {
        (a["comparison_row_id"], a["retailer_code"], a["observation_id"]): a
        for a in report.alternatives
    }
    week = week_text(report)
    code_of = {f"{RETAILER_NAMES[c]} (PHP)": c for c in OVERVIEW_ORDER}
    rows = []
    for row in report.comparison:
        status_note = (
            f"{row['comparison_row_id']} — source key {row['source_key']}; target "
            f"{row['target_id'] or 'none'}; mapping {row['mapping_status']}; "
            f"{row['row_comparison_status']}: {row['row_comparison_detail']}\n"
            f"Retailer prices are this row's context cells in {COMPARISON_TAB} (several variation "
            "rows can share one context and its observations); the Shopee price is this "
            "variation's own historical snapshot, never a current price."
        )
        cells: dict[str, Cell] = {
            "Rank": number(row["rank"], "integer"),
            "Shopee seller": text(seller_name(row)),
            "Laptop": text(laptop_name(row), note=status_note),
            "Shopee variation": text(row["shopee_model_name"]),
            "Screen (in)": number(row["screen_size_in"], "decimal"),
            "CPU family": text(row["cpu_series"]),
            "RAM (GB)": number(row["ram_capacity_gb"], "integer"),
            "Storage (GB)": number(row["storage_capacity_gb"], "integer"),
            "GPU": text(row["gpu_model"]),
            "Shopee (PHP, snapshot)": number(
                row["shopee_price_php"], "php", note=_shopee_status(row)
            ),
            "Mapping status": text(row["mapping_status"]),
            "Row comparison status": text(row["row_comparison_status"]),
            "Shopee listing link": text(row["shopee_listing_url"], link=row["shopee_listing_url"]),
            "Shopee price date": text(row["shopee_price_observed_at"]),
            "Comparison row ID": text(row["comparison_row_id"]),
            "Source key": text(row["source_key"]),
            "Target ID": text(row["target_id"]),
            "Sales snapshot date": text(row["sales_extract_date"]),
            "Ranking GMV (USD)": number(row["ranking_gmv_usd"], "usd"),
            "Reporting week": text(week),
            "Row grain": text(row["row_grain"]),
        }
        for label, code in code_of.items():
            tag = cell_tag(row, code) if row["context_id"] else ""
            priced = bool(row[f"{code}_Price"])
            link = row.get(f"{code}_Offer_URL", "") if priced and tag.startswith("ALT") else ""
            cells[label] = number(
                row[f"{code}_Price"],
                "php",
                note=_overview_note(report, row, code, alternatives),
                italic=priced and row[f"{code}_Match_Basis"] == "LISTED_SPEC",
                fill=tag_fill(tag, priced=priced),
            )
            if link:
                cells[label] = replace(cells[label], link=link)
        rows.append(tuple(cells[name] for name in header))
    retailer_note = (
        f"The price shown in {COMPARISON_TAB} for the week {week}: a matching configuration when "
        "one is comparable, otherwise the best close alternative (ALT 1 before ALT 2, then the "
        "lowest price). Blank means no comparable price (not 'not sold'). Italic = listed-spec "
        f"match. {STATUS_LEGEND} Why a cell is blank, stock and evidence: cell notes and "
        f"{COMPARISON_TAB}; every alternative: {ALTERNATIVES_TAB}."
    )
    notes = {
        "Rank": "Shopee variation rank (own ranking GMV, M-1 + MTD); one row per variation.",
        "Shopee (PHP, snapshot)": "HISTORICAL SNAPSHOT of this variation's own price in the sales "
        "extract; never a current price, never another seller's.",
        **{label: retailer_note for label in code_of},
    }
    return TabPlan(
        OVERVIEW_TAB, tuple(header), tuple(rows), tuple(notes.get(n, "") for n in header)
    )


def overview_labels_column(report: VariationPublicationReport) -> list[str]:
    return [overview_labels(row) for row in report.comparison]


def alternatives_plan(report: VariationPublicationReport, header: Sequence[str]) -> TabPlan:
    by_row = {row["comparison_row_id"]: row for row in report.comparison}
    order = {code: i for i, code in enumerate(RETAILER_ORDER)}

    def key(alt: Mapping[str, str]) -> tuple:
        return (
            int(alt["rank"]),
            order[alt["retailer_code"]],
            int(alt["difference_count"]),
            Decimal(alt["price"]),
            alt["observation_id"],
            alt["association_id"],
        )

    rows = []
    for alt in sorted(report.alternatives, key=key):
        base = by_row[alt["comparison_row_id"]]
        fill = tag_fill(alt["alternative_tag"], priced=True)
        cpu = " ".join(
            filter(
                None,
                (
                    alt["offer_cpu_series"],
                    f"({alt['offer_cpu_model']})" if alt["offer_cpu_model"] else "",
                ),
            )
        )
        url = alt["offer_url"]
        eligibility = "eligible" if alt["price_eligibility"] == COMPARABLE else "excluded"
        values = {
            "Rank": number(alt["rank"], "integer"),
            "Shopee seller": text(seller_name(base)),
            "Shopee variation": text(base["shopee_model_name"]),
            "Comparison row ID": text(alt["comparison_row_id"]),
            "Source key": text(alt["source_key"]),
            "Target ID": text(alt["target_id"]),
            "Retailer": text(RETAILER_NAMES.get(alt["retailer_code"], alt["retailer_id"])),
            "Retailer offer ID": text(alt["offer_key"]),
            "Retailer model": text(alt["offer_model"] or alt["title"]),
            "Retailer screen (in)": _numeric(alt["offer_screen_in"], "decimal"),
            "Retailer CPU": text(cpu),
            "Retailer RAM (GB)": _numeric(alt["offer_ram_gb"], "integer"),
            "Retailer storage (GB)": _numeric(alt["offer_storage_gb"], "integer"),
            "Retailer GPU": text(alt["offer_gpu"]),
            "Price (PHP)": number(alt["price"], "php"),
            "Currency": text(alt["currency"]),
            "Availability": text(alt["availability"]),
            "Product link": text(url, link=url),
            "Difference count": number(alt["difference_count"], "integer", fill=fill),
            "Difference tag": text(alt["alternative_tag"], fill=fill),
            "Differing specifications": text(alt["differing_dimensions"].replace(";", ", ")),
            "Difference details": text(alt["differences"]),
            "Unknown fields": text(alt["unknown_fields"]),
            "Warnings": text(alt["warnings"].replace(";", ", ")),
            "Match basis": text(
                f"ALTERNATIVE (price {eligibility}; "
                f"{alt['comparison_basis'] or 'close alternative'})"
            ),
            "Observed (Asia/Singapore)": text(alt["retrieved_at_local"]),
            "Observation ID": text(alt["observation_id"]),
            "Evidence reference": text(alt["evidence_references"]),
            "Association ID": text(alt["association_id"]),
            "Policy version": text(alt["policy_version"]),
            "Row grain": text(alt["row_grain"]),
        }
        rows.append(tuple(values[name] for name in header))
    intro = (
        f"Close alternatives for the reporting week {week_text(report)} (Asia/Singapore), one row "
        "per Shopee variation row and retailer offer: offers that are NOT matching configurations "
        "but differ in at most two established specifications (model, screen, CPU, installed RAM, "
        "installed storage and GPU share that budget). A reused retailer observation appears once "
        f"per variation row it supports (Association ID). {STATUS_LEGEND}"
    )
    notes = {
        "Rank": intro,
        "Unknown fields": "Unknown fields are never counted as agreement or as a difference.",
        "Association ID": "Identifies this row-to-offer association; the retailer "
        "observation itself is stored once.",
    }
    return TabPlan(
        ALTERNATIVES_TAB, tuple(header), tuple(rows), tuple(notes.get(n, "") for n in header)
    )


def _numeric(value: str, kind: str) -> Cell:
    import re

    return number(value, kind) if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value or "") else text(value)


def _appended(
    report: VariationPublicationReport, extra: Sequence[str], values: Mapping[str, str]
) -> list[Cell]:
    return [text(values.get(name, "")) for name in extra]


def history_plan_variation(report: VariationPublicationReport, contract: Contract) -> TabPlan:
    """Earlier history rows (legacy grain, association empty) plus one association row per
    variation row and selected/alternative observation (grain SHOPEE_VARIATION)."""
    extra = contract.appended[HISTORY_TAB]
    base = history_plan(report.legacy)
    header = (*base.header, *extra)
    observation_column = base.header.index("Observation ID")
    rank_column = base.header.index("Rank")
    by_observation = {row[observation_column].value: row for row in base.rows}
    rows = []
    for row in base.rows:  # earlier records: unchanged values, explicit legacy grain
        rows.append(
            (
                *row[:rank_column],
                BLANK,
                *row[rank_column + 1 :],
                *_appended(report, extra, {"Row grain": LEGACY_ROW_GRAIN}),
            )
        )
    names = {r["comparison_row_id"]: seller_name(r) for r in report.comparison}
    for assoc in sorted(
        report.associations,
        key=lambda a: (
            int(a["rank"]),
            RETAILER_ORDER.index(a["retailer_code"]),
            a["association_id"],
        ),
    ):
        source = by_observation.get(assoc["observation_id"])
        if source is None:
            raise PublicationInputError(
                [
                    f"association {assoc['association_id']} names observation "
                    f"{assoc['observation_id']} that is not in the history report"
                ]
            )
        cells = list(source)
        cells[rank_column] = number(assoc["rank"], "integer")
        rows.append(
            (
                *cells,
                *_appended(
                    report,
                    extra,
                    {
                        "Row grain": ROW_GRAIN,
                        "Comparison row ID": assoc["comparison_row_id"],
                        "Source key": assoc["source_key"],
                        "Shop ID": assoc["shop_id"],
                        "Item ID": assoc["item_id"],
                        "Source model ID": assoc["source_model_id"],
                        "Shopee seller": names[assoc["comparison_row_id"]],
                        "Association ID": assoc["association_id"],
                        "Difference count": assoc["difference_count"],
                        "Difference tag": assoc["difference_tag"],
                        "Difference details": assoc["difference_details"],
                        "Unknown fields": assoc["unknown_fields"],
                        "Schema version": SCHEMA_VERSION,
                    },
                ),
            )
        )
    return TabPlan(HISTORY_TAB, header, tuple(rows))


def review_plan_variation(report: VariationPublicationReport, contract: Contract) -> TabPlan:
    """Earlier review rows (legacy grain) plus row associations: rows awaiting configuration
    review, blocked source conflicts, and each variation row's recheck candidates."""
    extra = contract.appended[REVIEW_TAB]
    base = review_plan(report.legacy)
    header = (*base.header, *extra)
    rank_column = base.header.index("Rank")
    category_column = base.header.index("Category")
    rows = []
    for row in base.rows:
        if row[category_column].value == "Recheck candidate (unconfirmed)":
            continue  # target-grain rechecks are republished per variation row below
        rows.append(
            (
                *row[:rank_column],
                BLANK,
                *row[rank_column + 1 :],
                *_appended(report, extra, {"Row grain": LEGACY_ROW_GRAIN}),
            )
        )
    width = len(base.header)
    names = {r["comparison_row_id"]: seller_name(r) for r in report.comparison}

    def association(base_cells: list[Cell], row: Mapping[str, str], assoc: str, detail=None):
        ident = {
            "Row grain": ROW_GRAIN,
            "Comparison row ID": row["comparison_row_id"],
            "Source key": row["source_key"],
            "Shop ID": row["shop_id"],
            "Item ID": row["item_id"],
            "Source model ID": row["source_model_id"],
            "Shopee seller": names[row["comparison_row_id"]],
            "Association ID": assoc,
            "Schema version": SCHEMA_VERSION,
        } | (detail or {})
        return (*base_cells, *_appended(report, extra, ident))

    blank = [BLANK] * width
    for row in report.comparison:
        if row["context_id"]:
            continue
        cells = list(blank)
        cells[0] = text("Configuration review (variation row)")
        cells[1] = text(row["row_comparison_status"])
        cells[2] = text(f"{row['mapping_status']}: {row['row_comparison_detail']}")
        cells[3] = text(row["report_week_start"])
        cells[rank_column] = number(row["rank"], "integer")
        cells[base.header.index("Target ID")] = text(row["target_id"])
        cells[base.header.index("Comparison status")] = text("not compared (no trusted context)")
        rows.append(
            association(
                cells,
                row,
                "ASSOC-"
                + hashlib.sha256(
                    f"{row['comparison_row_id']}|review|{row['row_comparison_status']}".encode()
                ).hexdigest()[:16],
            )
        )
    ranks = {r["comparison_row_id"]: r for r in report.comparison}
    for recheck in sorted(
        report.rechecks,
        key=lambda r: (
            int(r["rank"]),
            RETAILER_ORDER.index(r["retailer_code"]),
            int(r["recheck_rank"]),
            r["association_id"],
        ),
    ):
        details = "; ".join(
            filter(
                None,
                (
                    recheck["explanation"],
                    recheck["stock"].replace("_", " ").lower(),
                    f"observed {recheck['observed_at_local']}"
                    if recheck["observed_at_local"]
                    else "",
                    f"unconfirmed price PHP {recheck['unconfirmed_price']}"
                    if recheck["unconfirmed_price"]
                    else "",
                    f"listed title: {recheck['title']}" if recheck["title"] else "",
                ),
            )
        )
        cells = list(blank)
        cells[0] = text("Recheck candidate (unconfirmed)")
        cells[1] = text(recheck["stage"])
        cells[2] = text(details)
        cells[3] = text(recheck["report_week_start"])
        cells[4] = text(recheck["observed_at_local"])
        cells[rank_column] = number(recheck["rank"], "integer")
        cells[7] = text(recheck["target_id"])
        cells[8] = text(RETAILER_NAMES.get(recheck["retailer_code"], recheck["retailer_id"]))
        cells[10] = text(recheck["url"], link=recheck["url"])
        cells[12] = text(recheck["stock"])
        cells[13] = text(recheck["status"])
        cells[15] = text(f"not priced (cell: {recheck['cell_price_status']})")
        cells[17] = text(recheck["observation_id"])
        rows.append(
            association(cells, ranks[recheck["comparison_row_id"]], recheck["association_id"])
        )
    return TabPlan(REVIEW_TAB, header, tuple(rows))


# Run log


def run_log_header(contract: Contract) -> tuple[str, ...]:
    return (*legacy_run_log_header(), *contract.appended[RUN_LOG_TAB])


def publication_id(report: VariationPublicationReport) -> str:
    payload = json.dumps(
        {"inputs": report.fingerprints, "publisher": PUBLISHER_VERSION, "schema": SCHEMA_VERSION},
        sort_keys=True,
    )
    return "PUB-" + hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class VariationPublicationPlan:
    report: VariationPublicationReport
    contract: Contract
    tabs: dict[str, TabPlan]
    publication_id: str
    content_sha256: str
    # Set from the live tab by ``check_destinations`` (see ``resolve_run_log_header``).
    run_log_columns: tuple[str, ...] | None = None
    # Live history/review records kept because this publication does not regenerate them.
    retained: dict[str, int] = field(default_factory=dict)

    @property
    def run_log_header(self) -> tuple[str, ...]:
        return self.run_log_columns or run_log_header(self.contract)

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
        context = report.summary.get("matching_context", {})
        cells = [s for s in report.selections.values()]

        def status_count(*statuses: str) -> int:
            return sum(1 for s in cells if s["price_status"] in statuses)

        week_counts = report.legacy.summary.get("counts", {})
        values = {
            "Publication ID": text(self.publication_id),
            "Publication status": text(status),
            "First published (UTC)": text(first_published),
            "Last published (UTC)": text(
                published_at.astimezone(UTC).isoformat(timespec="seconds")
            ),
            "Last published (Asia/Singapore)": text(_local(published_at)),
            "Publication attempts": number(str(attempts), "integer"),
            "Reporting week start": text(week["start"]),
            "Reporting week end (exclusive)": text(week["end_exclusive"]),
            "Week status": text(week["status"]),
            "Report cutoff (UTC)": text(report.summary["as_of_utc"]),
            "Report cutoff (Asia/Singapore)": text(_local(report.as_of)),
            "Pilot configurations": number(
                str(counts["distinct_configuration_contexts"]), "integer"
            ),
            "Comparison rows": number(str(len(self.tabs[COMPARISON_TAB].rows)), "integer"),
            "History rows": number(str(len(self.tabs[HISTORY_TAB].rows)), "integer"),
            "Review rows": number(str(len(self.tabs[REVIEW_TAB].rows)), "integer"),
            "Comparable prices": number(str(status_count(SELECTED)), "integer"),
            "Matched but excluded (stock or price rules)": number(
                str(status_count("NO_COMPARABLE_OFFER")), "integer"
            ),
            "Needs match review (no price)": number(
                str(status_count("REVIEW_REQUIRED_ONLY")), "integer"
            ),
            "Retailer cells not observed": number(
                str(
                    status_count(
                        "NO_OBSERVATION_IN_WEEK",
                        "NO_RELEVANT_CANDIDATE",
                        "NO_CANDIDATE_FOUND",
                        "DISCOVERY_NOT_ATTEMPTED",
                        "CANDIDATE_NOT_FETCHED",
                        "FETCH_FAILED",
                        "SEARCH_FAILED",
                        "CONFIGURATION_REVIEW_PENDING",
                        "SOURCE_CONFLICT_COMPARISON_BLOCKED",
                    )
                ),
                "integer",
            ),
            "Configurations with a price": number(
                str(counts["matching_priced_variation_rows"]), "integer"
            ),
            "Week observations": number(str(week_counts.get("observations_in_week", 0)), "integer"),
            "Week exact matches": number(str(week_counts.get("week_match_EXACT", 0)), "integer"),
            "Week matching problems": number(
                str(
                    sum(
                        week_counts.get(f"week_match_{s}", 0)
                        for s in ("UNMATCHED", "AMBIGUOUS", "REVIEW_REQUIRED")
                    )
                ),
                "integer",
            ),
            "Shopee sales snapshot": text(sales.get("label", "")),
            "Shopee sales run ID": text(sales.get("run_id", "")),
            "Tabs written": text(", ".join(tabs_written)),
            "Readback": text(readback),
            "Notes": text(notes),
            "Report directory": text(str(report.reports_dir)),
            "Selected list SHA-256": text(
                report.fingerprints["variation/top_100_shopee_variations.csv"]
            ),
            "Catalog (TARGET_MASTER) SHA-256": text(context.get("target_master_sha256", "")),
            "Matching context ID": text(context.get("context_id", "")),
            "Input fingerprints": text(json.dumps(report.fingerprints, sort_keys=True)),
            "Published content SHA-256": text(self.content_sha256),
            "Publisher version": text(PUBLISHER_VERSION),
            "Shopee snapshot prices": number(str(counts["rows_with_own_shopee_price"]), "integer"),
            "Shopee snapshot extract date": text(sales.get("extract_date", "")),
            "Overview rows": number(str(len(self.tabs[OVERVIEW_TAB].rows)), "integer"),
            "Alternative prices shown": number(
                str(counts["row_associations_alternative_displayed"]), "integer"
            ),
            "Alternative association rows": number(
                str(counts["alternative_row_associations"]), "integer"
            ),
            "Alternatives rows published": number(
                str(len(self.tabs[ALTERNATIVES_TAB].rows)), "integer"
            ),
            "Schema version": text(SCHEMA_VERSION),
            "Ranking grain": text(ROW_GRAIN),
            "Selected Shopee variations": number(str(counts["selected_variations"]), "integer"),
            "Distinct configuration contexts": number(
                str(counts["distinct_configuration_contexts"]), "integer"
            ),
            "Mapped selected variations": number(
                str(counts["mapped_selected_variations"]), "integer"
            ),
            "Unmapped selected variations": number(
                str(counts["unmapped_selected_variations"]), "integer"
            ),
            "ALT 1 offers": number(str(counts["alt_1_offers"]), "integer"),
            "ALT 2 offers": number(str(counts["alt_2_offers"]), "integer"),
            "Alternative-priced variation rows": number(
                str(counts["alternative_priced_variation_rows"]), "integer"
            ),
            "Matching-priced variation rows": number(
                str(counts["matching_priced_variation_rows"]), "integer"
            ),
        }
        # Columns this publication does not fill (collection times, the retired summary's row
        # count, adopted tabs) stay blank, as in earlier rows.
        return {name: values.get(name, BLANK) for name in self.run_log_header}


def build_variation_plan(
    report: VariationPublicationReport, contract: Contract | None = None
) -> VariationPublicationPlan:
    contract = contract or load_contract()
    tabs = {
        TOP_TAB: top_plan(report, contract.headers[TOP_TAB]),
        COMPARISON_TAB: comparison_plan(report, contract.headers[COMPARISON_TAB]),
        OVERVIEW_TAB: overview_plan(report, contract.headers[OVERVIEW_TAB]),
        ALTERNATIVES_TAB: alternatives_plan(report, contract.headers[ALTERNATIVES_TAB]),
        HISTORY_TAB: history_plan_variation(report, contract),
        REVIEW_TAB: review_plan_variation(report, contract),
    }
    for plan in tabs.values():
        for row in plan.rows:
            if len(row) != plan.width:
                raise PublicationInputError([f"{plan.title} row width differs from its header"])
    issues = plan_issues(report, tabs)
    if issues:
        raise PublicationInputError(issues)
    return VariationPublicationPlan(
        report, contract, tabs, publication_id(report), grid_sha256(list(tabs.values()))
    )


def plan_issues(report: VariationPublicationReport, tabs: Mapping[str, TabPlan]) -> list[str]:
    """Cross-checks of the planned tabs: same rows everywhere, overview = comparison, no tag
    without text, prices numeric, grain labels and IDs consistent."""
    issues: list[str] = []
    comparison, overview, top = tabs[COMPARISON_TAB], tabs[OVERVIEW_TAB], tabs[TOP_TAB]
    n = len(report.comparison)
    if not (len(comparison.rows) == len(overview.rows) == len(top.rows) == n):
        return ["Top list, comparison and overview have different numbers of rows"]
    c_idx = {name: i for i, name in enumerate(comparison.header)}
    o_idx = {name: i for i, name in enumerate(overview.header)}
    t_idx = {name: i for i, name in enumerate(top.header)}
    visible: dict[tuple[str, ...], str] = {}
    for row, c, o, t in zip(
        report.comparison, comparison.rows, overview.rows, top.rows, strict=True
    ):
        key = row["comparison_row_id"]
        if not (
            c[c_idx["Comparison row ID"]].value
            == o[o_idx["Comparison row ID"]].value
            == t[t_idx["comparison_row_id"]].value
            == key
        ):
            issues.append(f"{key}: tabs disagree on the row identity")
        for label, left, right in (
            ("rank", c[c_idx["Rank"]], t[t_idx["rank"]]),
            (
                "Shopee price",
                c[c_idx["Shopee price (PHP, snapshot)"]],
                t[t_idx["shopee_current_price_php"]],
            ),
            (
                "Shopee price",
                c[c_idx["Shopee price (PHP, snapshot)"]],
                o[o_idx["Shopee (PHP, snapshot)"]],
            ),
        ):
            if (left.kind, left.value) != (right.kind, right.value):
                issues.append(f"{key}: {label} differs between tabs")
        for code in OVERVIEW_ORDER:
            name = RETAILER_NAMES[code]
            mine, theirs = o[o_idx[f"{name} (PHP)"]], c[c_idx[f"{name} price (PHP)"]]
            if (mine.kind, mine.value, mine.number_format) != (
                theirs.kind,
                theirs.value,
                theirs.number_format,
            ):
                issues.append(f"{key}: overview {name} price differs from the comparison")
            tag = cell_tag(row, code) if row["context_id"] else ""
            if mine.fill != tag_fill(tag, priced=mine.kind == "number"):
                issues.append(f"{key}: overview {name} fill differs from its tag")
            if mine.kind == "number" and not mine.note:
                issues.append(f"{key}: overview {name} price has no cell note")
            status_cell = c[c_idx[f"{name} status"]]
            if status_cell.fill and not (tag and tag in status_cell.value):
                issues.append(f"{key}: {name} colour has no matching text")
            if not row["context_id"] and (theirs.kind == "number" or row[f"{code}_Price"]):
                issues.append(f"{key}: a row without a context shows a retailer price")
        if t[t_idx["comparison_status"]].value != row["comparison_status"]:
            issues.append(f"{key}: comparison status differs between tabs")
        sig = tuple(cell.value for cell in o[1:5]) + (key,)
        if sig in visible:
            issues.append(f"{key}: duplicate overview row")
        visible[sig] = key
    for title in (HISTORY_TAB, REVIEW_TAB, ALTERNATIVES_TAB):
        plan = tabs[title]
        if "Association ID" in plan.header:
            index = plan.header.index("Association ID")
            ids = [row[index].value for row in plan.rows if row[index].value]
            if len(ids) != len(set(ids)) and title != HISTORY_TAB:
                issues.append(f"{title} repeats an association ID")
    return issues


def write_plan_csv(path: Path, plan: TabPlan) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for row in plan.grid():
            writer.writerow([cell.value for cell in row])


# Retaining earlier history and review records


def _live_cell(cell: Mapping) -> Cell:
    """A live cell as a plan cell (text or number with its format, and its link)."""
    value = cell.get("userEnteredValue") or {}
    fmt = cell.get("userEnteredFormat") or {}
    link = cell.get("hyperlink") or (fmt.get("textFormat") or {}).get("link", {}).get("uri", "")
    if "numberValue" in value:
        pattern = (fmt.get("numberFormat") or {}).get("pattern", "")
        kind = "php" if "\u20b1" in pattern else "decimal" if pattern == "0.0" else "integer"
        return Cell("number", repr(value["numberValue"]), link or "", kind)
    if "stringValue" in value:
        return Cell("text", value["stringValue"], link or "")
    return BLANK


def _norm(value: str) -> str:
    try:
        return format(Decimal(value).normalize(), "f")
    except InvalidOperation:
        return value.strip()


def _record_key(cells: Sequence[Cell], header: Sequence[str], legacy: Sequence[str]) -> tuple:
    """Identity of a record: its association ID, else its earlier columns without the rank."""
    by_name = dict(zip(header, cells, strict=False))
    association = by_name.get("Association ID", BLANK).value
    if association:
        return ("A", association)
    return ("L", *(_norm(by_name[name].value) for name in legacy if name != "Rank"))


def apply_live_retention(
    plan: VariationPublicationPlan, states: Mapping[str, TabState]
) -> VariationPublicationPlan:
    """Keep every earlier PILOT_PRICE_HISTORY / PILOT_PRICE_REVIEW record this publication does
    not regenerate.

    A publication regenerates the records of its own scope from the local cumulative history. The
    live tabs can hold earlier records outside that scope (earlier pilots); replacing the tab
    would silently drop them, so they stay exactly as they are (values, formats, links, their own
    ranks), placed before the regenerated records. A record with no grain gets the explicit
    legacy grain label; no association is guessed for it. Stale target-grain recheck rows are not
    kept: rechecks are republished per variation row.
    """
    tabs = dict(plan.tabs)
    retained: dict[str, int] = {}
    for title, legacy in ((HISTORY_TAB, LEGACY_HISTORY_HEADER), (REVIEW_TAB, LEGACY_REVIEW_HEADER)):
        state = states[title]
        tab = plan.tabs[title]
        if not state.exists or len(state.grid) < 2:
            retained[title] = 0
            continue
        live_names = state.header()
        regenerated = {_record_key(row, tab.header, legacy) for row in tab.rows}
        index = {name: i for i, name in enumerate(tab.header) if name}
        kept = []
        for live_row in state.grid[1:]:
            by_name = {
                name: _live_cell(cell)
                for name, cell in zip(live_names, live_row, strict=False)
                if name
            }
            if not any(cell.kind != "blank" for cell in by_name.values()):
                continue
            if by_name.get("Category", BLANK).value == "Recheck candidate (unconfirmed)":
                continue
            cells = [by_name.get(name, BLANK) for name in tab.header]
            if _record_key(cells, tab.header, legacy) in regenerated:
                continue
            if not cells[index["Row grain"]].value:
                cells[index["Row grain"]] = text(LEGACY_ROW_GRAIN)
            kept.append(tuple(cells))
        retained[title] = len(kept)
        tabs[title] = replace(tab, rows=(*kept, *tab.rows))
    return replace(
        plan,
        tabs=tabs,
        content_sha256=grid_sha256(list(tabs.values())),
        retained=retained,
    )


# Destination checks


def retired_tab_titles(spreadsheet) -> list[str]:
    return [ws.title for ws in spreadsheet.worksheets() if ws.title in RETIRED_TABS]


def check_destinations(
    states: Mapping[str, TabState],
    plan: VariationPublicationPlan,
) -> tuple[list[str], dict]:
    """``(refusals, live_schema)``. Reads what the workbook holds against the contract: every
    template tab must exist, an empty or header-only tab must carry exactly the contract header
    (the live schema is the authority, never positions), and a tab with content must be this
    publisher's own earlier output; legacy configuration content is refused."""
    contract = plan.contract
    problems: list[str] = []
    live: dict[str, dict] = {}
    expected = {title: plan.tabs[title].header for title in DATA_TABS} | {
        RUN_LOG_TAB: plan.run_log_header
    }
    legacy_prefix = {
        HISTORY_TAB: LEGACY_HISTORY_HEADER,
        REVIEW_TAB: LEGACY_REVIEW_HEADER,
    }
    for title in PUBLISHER_TABS:
        state = states[title]
        header = state.header() if state.exists else []
        live[title] = {
            "exists": state.exists,
            "used_rows": state.used_rows,
            "header": header,
            "header_notes": [
                cell.get("note", "") for cell in (state.grid[0] if state.grid else ())
            ],
        }
        if not state.exists:
            problems.append(
                f"{title} is missing; the prepared template tab is required and is "
                "never created by this publisher"
            )
            continue
        want = list(expected[title])
        trimmed = list(header)
        while trimmed and not trimmed[-1]:
            trimmed.pop()
        if title == RUN_LOG_TAB:
            resolved = resolve_run_log_header(state, contract)
            if resolved is None:
                problems.append(
                    f"{title} has content that this publisher did not write (unrecognised header)"
                )
            else:
                plan.run_log_columns = resolved
            continue
        if title in legacy_prefix and title in contract.appended:
            old = list(legacy_prefix[title])
            if trimmed not in (old, want) and state.used_rows > 0 and trimmed[: len(old)] != old:
                problems.append(f"{title} has content that this publisher did not write")
            elif trimmed and trimmed[: len(old)] == old and trimmed != old and trimmed != want:
                problems.append(
                    f"{title} live header differs from the contract's appended columns "
                    f"({trimmed[len(old) :]!r} vs {want[len(old) :]!r})"
                )
            continue
        if state.used_rows == 0:
            continue
        if trimmed == want:
            continue
        # Header present: say whether it is the wrong grain or something else.
        if "Target ID" in trimmed and "Comparison row ID" not in trimmed:
            problems.append(
                f"{title} still holds legacy configuration-grain content ({LEGACY_ROW_GRAIN}); "
                "refusing to write variation-grain reports over it (archive or clear it first)"
            )
        elif trimmed[: len(want)] != want or any(trimmed[len(want) :]):
            problems.append(f"{title} live header differs from the contract; not overwritten")
    for title in retired_tab_titles_in(states):
        live[title] = {"exists": True, "note": "retired tab present; never written"}
    return problems, live


def retired_tab_titles_in(states: Mapping[str, TabState]) -> list[str]:
    return [t for t in states if t in RETIRED_TABS]


# Layout


_NUMBER = {"align": "RIGHT", "mono": True}
_LINK = {"wrap": "CLIP", "muted": True}
_IDENTITY = {
    "Rank": ColumnStyle(52, **_NUMBER),
    "rank": ColumnStyle(52, **_NUMBER),
    "Shopee seller": ColumnStyle(150),
    "Shopee item": ColumnStyle(260),
    "Shopee variation": ColumnStyle(210),
    "Laptop": ColumnStyle(260),
    "Brand": ColumnStyle(92),
    "Series": ColumnStyle(112),
    "Model": ColumnStyle(200),
    "Screen (in)": ColumnStyle(82, **_NUMBER),
    "CPU family": ColumnStyle(124),
    "CPU model": ColumnStyle(130),
    "RAM (GB)": ColumnStyle(78, **_NUMBER),
    "Storage (GB)": ColumnStyle(88, **_NUMBER),
    "GPU": ColumnStyle(130),
    "Comparison row ID": ColumnStyle(300, **_LINK),
    "Source key": ColumnStyle(280, **_LINK),
    "Target ID": ColumnStyle(120, **_LINK),
    "Shop ID": ColumnStyle(120, **_LINK),
    "Item ID": ColumnStyle(140, **_LINK),
    "Source model ID": ColumnStyle(150, **_LINK),
    "Association ID": ColumnStyle(180, **_LINK),
}


def variation_tab_layout(title: str, header: Sequence[str]) -> TabLayout | None:
    """The house layout (navy/orange headers, Roboto, banding, frozen identifiers, widths) of the
    variation tabs; history, review and the run log keep the legacy layout rules (none)."""
    if title == TOP_TAB:
        styles = {
            "rank": ColumnStyle(52, **_NUMBER),
            "comparison_row_id": ColumnStyle(300, **_LINK),
            "source_key": ColumnStyle(280, **_LINK),
            "shop_id": ColumnStyle(120, **_LINK),
            "item_id": ColumnStyle(140, **_LINK),
            "source_model_id": ColumnStyle(150, **_LINK),
            "shop_name": ColumnStyle(160),
            "item_name": ColumnStyle(280),
            "shopee_model_name": ColumnStyle(210),
            "screen_size_in": ColumnStyle(90, **_NUMBER),
            "ram_capacity_gb": ColumnStyle(90, **_NUMBER),
            "storage_capacity_gb": ColumnStyle(100, **_NUMBER),
            "m_minus_1_gmv_usd": ColumnStyle(130, "navy_alt", **_NUMBER),
            "mtd_gmv_usd": ColumnStyle(130, "navy_alt", **_NUMBER),
            "ranking_gmv_usd": ColumnStyle(140, "navy_alt", **_NUMBER),
            "shopee_current_price_php": ColumnStyle(140, "orange", "shopee", **_NUMBER),
            "shopee_price_observed_at": ColumnStyle(140, "orange"),
            "rank_metric_scope": ColumnStyle(420),
        }
        return TabLayout(tuple(styles.get(n, ColumnStyle(130)) for n in header), 2, 56, 40)
    if title == COMPARISON_TAB:
        styles = dict(_IDENTITY)
        styles |= {
            "Shopee price (PHP, snapshot)": ColumnStyle(140, "orange", "shopee", **_NUMBER),
            "Shopee listing link": ColumnStyle(175, "orange", **_LINK),
            "Shopee price status": ColumnStyle(330, "orange"),
            "Shopee price observed (Asia/Singapore)": ColumnStyle(210, "orange"),
        }
        for index, code in enumerate(RETAILER_ORDER):
            name, head = RETAILER_NAMES[code], ("navy", "navy_alt")[index % 2]
            styles |= {
                f"{name} price (PHP)": ColumnStyle(136, head, "retailer", **_NUMBER),
                f"{name} product link": ColumnStyle(175, head, **_LINK),
                f"{name} status": ColumnStyle(320, head),
                f"{name} observed (Asia/Singapore)": ColumnStyle(175, head),
                f"{name} recheck link (unconfirmed)": ColumnStyle(180, head, **_LINK),
                f"{name} recheck note": ColumnStyle(350, head),
            }
        for name in header:
            if name.startswith("Lowest") and "(PHP)" in name:
                styles[name] = ColumnStyle(
                    155, "navy", "in_stock" if "in-stock" in name else "retailer", **_NUMBER
                )
            elif name.startswith("Lowest"):
                styles[name] = ColumnStyle(320, "navy_alt")
            elif name.endswith("GMV (USD)"):
                styles[name] = ColumnStyle(130, "navy_alt", **_NUMBER)
            elif name not in styles:
                styles[name] = ColumnStyle(160, "navy_alt")
        styles["Ranking metric scope"] = ColumnStyle(420, "navy_alt")
        return TabLayout(tuple(styles.get(n, ColumnStyle(160)) for n in header), 4, 72, 72)
    if title == OVERVIEW_TAB:
        styles = dict(_IDENTITY) | {
            "Shopee (PHP, snapshot)": ColumnStyle(150, "orange", "shopee", **_NUMBER),
            "Shopee listing link": ColumnStyle(175, "orange", **_LINK),
            "Shopee price date": ColumnStyle(120, "orange"),
            "Mapping status": ColumnStyle(190),
            "Row comparison status": ColumnStyle(210),
            "Ranking GMV (USD)": ColumnStyle(130, "navy_alt", **_NUMBER),
        }
        price = ColumnStyle(120, fill="retailer", **_NUMBER)
        return TabLayout(
            tuple(
                styles.get(n, price if n.endswith("(PHP)") else ColumnStyle(150)) for n in header
            ),
            4,
            64,
            48,
        )
    if title == ALTERNATIVES_TAB:
        styles = dict(_IDENTITY) | {
            "Retailer": ColumnStyle(120),
            "Retailer offer ID": ColumnStyle(180, **_LINK),
            "Retailer model": ColumnStyle(200),
            "Retailer screen (in)": ColumnStyle(90, **_NUMBER),
            "Retailer CPU": ColumnStyle(170),
            "Retailer RAM (GB)": ColumnStyle(90, **_NUMBER),
            "Retailer storage (GB)": ColumnStyle(100, **_NUMBER),
            "Retailer GPU": ColumnStyle(120),
            "Price (PHP)": ColumnStyle(136, "navy", "retailer", **_NUMBER),
            "Currency": ColumnStyle(80),
            "Availability": ColumnStyle(110),
            "Product link": ColumnStyle(175, **_LINK),
            "Difference count": ColumnStyle(90, **_NUMBER),
            "Difference tag": ColumnStyle(170),
            "Differing specifications": ColumnStyle(180),
            "Difference details": ColumnStyle(330),
            "Unknown fields": ColumnStyle(330),
            "Warnings": ColumnStyle(260),
            "Match basis": ColumnStyle(280),
            "Observed (Asia/Singapore)": ColumnStyle(175),
            "Observation ID": ColumnStyle(190, **_LINK),
            "Evidence reference": ColumnStyle(420),
            "Policy version": ColumnStyle(170),
            "Row grain": ColumnStyle(150),
        }
        return TabLayout(tuple(styles.get(n, ColumnStyle(130)) for n in header), 4, 64, 52)
    return None


# Publishing


@dataclass
class PublicationOutcome:
    status: str
    tabs_written: list[str]
    readback_issues: list[str]
    tab_ids: dict[str, int]
    error: str = ""


def existing_run_log_rows(state: TabState, header: Sequence[str]) -> list[list[Cell]]:
    """Earlier run-log rows kept as written, placed by column name in ``header`` (a column the
    earlier row never had stays blank), excluding the header row."""
    names = state.header()
    rows = []
    for row in state.grid[1:]:
        by_name: dict[str, Cell] = {}
        for name, cell in zip(names, row, strict=False):
            value = cell.get("userEnteredValue") or {}
            if not name:
                continue
            if "numberValue" in value:
                by_name[name] = Cell("number", repr(value["numberValue"]), "", "integer")
            elif "stringValue" in value:
                by_name[name] = Cell("text", value["stringValue"])
        cells = [by_name.get(name, BLANK) for name in header]
        if any(c.kind != "blank" for c in cells):
            rows.append(cells)
    return rows


def publish_variation(
    spreadsheet,
    plan: VariationPublicationPlan,
    *,
    published_at: datetime,
    backup_dir: Path,
    progress=None,
) -> PublicationOutcome:
    """Back up, write, read back and log. ``SUCCESS`` only after every check passes.

    Only the prepared template tabs of ``PUBLISHER_TABS`` are written; a missing tab or a tab with
    content this publisher did not write is refused before any change, and no tab is created."""

    def step(message: str) -> None:
        if progress is not None:
            progress(message)

    step("inspecting destination tabs")
    states = {title: read_tab(spreadsheet, title, PUBLISHER_TABS) for title in PUBLISHER_TABS}
    problems, _ = check_destinations(states, plan)
    if problems:
        raise DestinationError("; ".join(problems))
    plan = apply_live_retention(plan, states)
    backup_dir.mkdir(parents=True, exist_ok=True)
    for title, state in states.items():
        if state.used_rows:
            write_backup(backup_dir, state, snapshot_tab(spreadsheet, title, PUBLISHER_TABS))
    allowed = {states[t].sheet_id for t in PUBLISHER_TABS}
    tab_ids = {t: states[t].sheet_id for t in PUBLISHER_TABS}
    header = plan.run_log_header
    earlier = existing_run_log_rows(states[RUN_LOG_TAB], header)
    first, attempts = "", 0
    for row in earlier:
        if row[0].value == plan.publication_id:
            first = row[header.index("First published (UTC)")].value
            try:
                attempts = int(Decimal(row[header.index("Publication attempts")].value or "0"))
            except InvalidOperation:
                attempts = 0
    first = first or published_at.astimezone(UTC).isoformat(timespec="seconds")

    def write(title: str, grid: Sequence[Sequence[Cell]], width: int) -> None:
        requests = tab_requests(states[title], grid, width, layout_fn=variation_tab_layout)
        guard_requests(requests, allowed)
        step(f"writing {title} ({len(grid) - 1} data rows)")
        spreadsheet.batch_update({"requests": requests})

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
        kept = [list(r) for r in earlier if r[0].value != plan.publication_id]
        grid = [
            [text(name, bold=True) for name in header],
            *kept,
            [values[name] for name in header],
        ]
        write(RUN_LOG_TAB, grid, len(header))
        return grid

    written: list[str] = []
    try:
        log("IN_PROGRESS", written, "NOT_RUN", "Publication started; data tabs pending.")
        for title in DATA_TABS:
            write(title, plan.tabs[title].grid(), plan.tabs[title].width)
            written.append(title)
    except Exception as exc:  # Record what happened, then report partial failure.
        error = f"{type(exc).__name__}: {exc}"
        try:
            log(
                "PARTIAL_FAILURE",
                written,
                "NOT_RUN",
                "Rerun the same --write-sheets command; it replaces all publisher tabs.",
            )
        except Exception as log_exc:  # pragma: no cover - reported to the caller
            error += f"; run-log update also failed ({type(log_exc).__name__})"
        return PublicationOutcome("PARTIAL_FAILURE", written, [], tab_ids, error)

    issues: list[str] = []
    for title in DATA_TABS:
        step(f"reading back {title}")
        issues += verify_tab(read_tab(spreadsheet, title, PUBLISHER_TABS), plan.tabs[title].grid())
        issues += verify_layout(
            spreadsheet, plan.tabs[title], layout_fn=variation_tab_layout, allowed=PUBLISHER_TABS
        )
    status = "SUCCESS" if not issues else "READBACK_FAILED"
    readback = "PASSED" if not issues else f"FAILED ({len(issues)} issue(s))"
    try:
        grid = log(
            status,
            written,
            readback,
            "" if not issues else "Readback found differences; see the local report.",
        )
        issues += verify_tab(read_tab(spreadsheet, RUN_LOG_TAB, PUBLISHER_TABS), grid)
    except Exception as exc:
        return PublicationOutcome(
            "PARTIAL_FAILURE",
            written,
            issues,
            tab_ids,
            f"Run-log update failed: {type(exc).__name__}: {exc}",
        )
    return PublicationOutcome(
        "SUCCESS" if not issues else "READBACK_FAILED", written, issues, tab_ids
    )
