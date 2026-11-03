"""Variation-level price reports (schema ``shopee-variation-sheets-1``, 2026-10-27).

The retailer layer (discovery, collection, matching, ``track_prices.py``) is keyed by configuration
*context*: a distinct trusted catalog target. This module joins each ranked Shopee variation row to
the reviewed price report of its context, without touching that layer:

- Every selected variation keeps its own row, identifiers, GMV and its **own** Shopee snapshot price
  (never another seller's, a minimum across sellers, or the configuration-level snapshot).
- The retailer cells of a row are its context's reviewed cells. Several rows can show one retailer
  observation; the association between a row and an observation is a separate record (association
  ID), and a reused observation is counted once in the observation counts.
- A row without a trusted context (unmapped, inactive, conflicted or incomplete mapping, or a
  blocking source conflict) stays present with blank retailer prices and an explanation.
- Nothing is rematched, repriced, replayed or fetched here. Alternatives, tags, both minima and
  stock rules are exactly the context report's.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .price_tracking import (
    ALTERNATIVE_COLUMNS,
    ALTERNATIVE_SELECTED,
    COMPARABLE,
    COMPARISON_COLUMNS,
    HISTORY_COLUMNS,
    LEGACY_COMPARISON_COLUMNS,
    LEGACY_HISTORY_COLUMNS,
    LEGACY_SELECTION_COLUMNS,
    PRODUCT_FIELDS,
    REPORTING_TIMEZONE,
    RETAILERS,
    REVIEW_COLUMNS,
    SELECTED,
    SELECTION_COLUMNS,
)
from .recheck import RECHECK_COLUMNS
from .variation_ranking import (
    CONTEXT_READY,
    MAPPED_VERIFIED,
    ROW_GRAIN,
    SCHEMA_VERSION,
    SNAPSHOT_PRICE,
    VARIATION_COLUMNS,
)
from .verified_top import VerifiedRankingInputError, read_csv_rows

# Shopee price statuses at the report cutoff (in addition to the ranking's own reasons).
SNAPSHOT_PRICE_STALE = "SNAPSHOT_PRICE_STALE"
SNAPSHOT_AFTER_CUTOFF = "SNAPSHOT_AFTER_CUTOFF"
# A snapshot extracted more than this many days before the report cutoff is shown, labelled stale.
STALE_SNAPSHOT_DAYS = 7

# Row comparison statuses.
MATCHING_PRICES_FOUND = "MATCHING_PRICES_FOUND"
ALTERNATIVE_PRICES_ONLY = "ALTERNATIVE_PRICES_ONLY"
NO_RETAILER_PRICE = "NO_RETAILER_PRICE"

CODES = tuple(code for code, _ in RETAILERS)

# Context-report columns copied to every variation row of that context: the retailer cells, the
# minima, counts and bases. Product identity, rank, the configuration-level Shopee snapshot and
# week fields are *not* copied (the row has its own).
_NOT_COPIED = frozenset(
    {
        "report_week_start",
        "report_week_end",
        "reporting_timezone",
        "week_status",
        "as_of",
        "rank",
        "target_id",
        "Currency",
        "Shopee_Source_Count",
        "Shopee_Source_Model_IDs",
        "Shopee_Total_GMV_USD",
        "Shopee_GMV_Period",
        "Shopee_Sales_Run_ID",
        "Shopee_Sales_Extract_Date",
        "matching_context_id",
        "selected_list_sha256",
    }
    | {name for name in COMPARISON_COLUMNS if name.startswith("Shopee_")}
    | {name for name, _ in PRODUCT_FIELDS}
)
CONTEXT_COPY_COLUMNS: tuple[str, ...] = tuple(
    name for name in COMPARISON_COLUMNS if name not in _NOT_COPIED
)

ROW_IDENTITY_COLUMNS: tuple[str, ...] = (
    "report_week_start",
    "report_week_end",
    "week_status",
    "as_of",
    "rank",
    "comparison_row_id",
    "source_key",
    "shop_id",
    "shop_name",
    "item_id",
    "source_model_id",
    "item_name",
    "shopee_model_name",
    "target_id",
    "context_id",
    "mapping_status",
    "comparison_status",
    "row_comparison_status",
    "row_comparison_detail",
    "brand",
    "series",
    "model",
    "screen_size_in",
    "cpu_series",
    "cpu_model",
    "ram_capacity_gb",
    "storage_capacity_gb",
    "gpu_model",
    "m_minus_1_gmv_usd",
    "mtd_gmv_usd",
    "ranking_gmv_usd",
    "shopee_price_php",
    "shopee_price_status",
    "shopee_price_detail",
    "shopee_price_observed_at",
    "shopee_price_date_basis",
    "shopee_listing_url",
    "sales_extract_date",
    "sales_run_id",
    "m_minus_1_month",
    "mtd_month",
    "rank_metric",
    "rank_metric_scope",
    "matching_context_id",
    "selected_list_sha256",
    "context_rank",
)
VARIATION_COMPARISON_COLUMNS: tuple[str, ...] = (
    *ROW_IDENTITY_COLUMNS,
    *CONTEXT_COPY_COLUMNS,
    "schema_version",
    "row_grain",
)
_ASSOCIATION_COLUMNS: tuple[str, ...] = (
    "comparison_row_id",
    "source_key",
    "shop_id",
    "item_id",
    "source_model_id",
    "shop_name",
    "context_id",
    "association_id",
)
VARIATION_SELECTION_COLUMNS: tuple[str, ...] = (
    *_ASSOCIATION_COLUMNS,
    "association_kind",
    *(name for name in SELECTION_COLUMNS),
    "schema_version",
    "row_grain",
)
VARIATION_ALTERNATIVE_COLUMNS: tuple[str, ...] = (
    *_ASSOCIATION_COLUMNS,
    *(name for name in ALTERNATIVE_COLUMNS),
    "schema_version",
    "row_grain",
)
VARIATION_RECHECK_COLUMNS: tuple[str, ...] = (
    *_ASSOCIATION_COLUMNS,
    *RECHECK_COLUMNS,
    "schema_version",
    "row_grain",
)
HISTORY_ASSOCIATION_COLUMNS: tuple[str, ...] = (
    *_ASSOCIATION_COLUMNS,
    "rank",
    "retailer_code",
    "observation_id",
    "association_kind",
    "difference_count",
    "difference_tag",
    "difference_details",
    "unknown_fields",
    "price",
    "schema_version",
    "row_grain",
)

REPORT_FILES = {
    "comparison": "weekly_variation_comparison.csv",
    "selections": "weekly_variation_selections.csv",
    "alternatives": "weekly_variation_alternatives.csv",
    "rechecks": "weekly_variation_rechecks.csv",
    "history": "weekly_variation_history_associations.csv",
    "coverage": "variation_coverage.json",
    "summary": "variation_run_summary.json",
}


class VariationReportError(ValueError):
    """The variation or context inputs are inconsistent; no report is produced."""


@dataclass
class ContextReport:
    """The reviewed target-grain report of the contexts (``track_prices.py`` output)."""

    report_dir: Path
    comparison: dict[str, dict[str, str]]
    selections: dict[tuple[str, str], dict[str, str]]
    alternatives: list[dict[str, str]]
    rechecks: list[dict[str, str]]
    history: list[dict[str, str]]
    review: list[dict[str, str]]
    summary: dict
    sha256: dict[str, str] = field(default_factory=dict)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_context_report(report_dir: Path) -> ContextReport:
    names = {
        "comparison": "weekly_price_comparison.csv",
        "selections": "weekly_price_selections.csv",
        "history": "price_history.csv",
        "review": "price_review_queue.csv",
        "summary": "price_tracking_run_summary.json",
    }
    missing = [name for name in names.values() if not (report_dir / name).is_file()]
    if missing:
        raise VariationReportError(f"context report is missing {', '.join(missing)}")
    try:
        comparison_rows = [
            dict.fromkeys(COMPARISON_COLUMNS, "") | row
            for row in read_csv_rows(
                report_dir / names["comparison"], LEGACY_COMPARISON_COLUMNS, "Comparison report"
            )
        ]
        selection_rows = [
            dict.fromkeys(SELECTION_COLUMNS, "") | row
            for row in read_csv_rows(
                report_dir / names["selections"], LEGACY_SELECTION_COLUMNS, "Selections report"
            )
        ]
        history = [
            dict.fromkeys(HISTORY_COLUMNS, "") | row
            for row in read_csv_rows(
                report_dir / names["history"], LEGACY_HISTORY_COLUMNS, "History report"
            )
        ]
        review = read_csv_rows(report_dir / names["review"], REVIEW_COLUMNS, "Review report")
        alternatives: list[dict[str, str]] = []
        path = report_dir / "weekly_price_alternatives.csv"
        if path.is_file():
            alternatives = read_csv_rows(path, ALTERNATIVE_COLUMNS, "Alternatives report")
        rechecks: list[dict[str, str]] = []
        path = report_dir / "recheck_candidates.csv"
        if path.is_file():
            rechecks = read_csv_rows(path, RECHECK_COLUMNS, "Recheck candidates")
    except VerifiedRankingInputError as exc:
        raise VariationReportError(str(exc)) from None
    try:
        summary = json.loads((report_dir / names["summary"]).read_text(encoding="utf-8"))
    except ValueError:
        raise VariationReportError("context run summary is unreadable") from None
    by_target = {row["target_id"]: row for row in comparison_rows}
    if len(by_target) != len(comparison_rows):
        raise VariationReportError("context comparison repeats a target_id")
    selections = {(row["target_id"], row["retailer_code"]): row for row in selection_rows}
    fingerprints = {name: sha256_file(report_dir / name) for name in names.values()}
    return ContextReport(
        report_dir,
        by_target,
        selections,
        alternatives,
        rechecks,
        history,
        review,
        summary,
        fingerprints,
    )


@dataclass
class VariationReports:
    comparison: list[dict[str, str]]
    selections: list[dict[str, str]]
    alternatives: list[dict[str, str]]
    rechecks: list[dict[str, str]]
    history: list[dict[str, str]]
    coverage: dict
    summary: dict


def association_id(*parts: str) -> str:
    return "ASSOC-" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _shopee_price_at_cutoff(row: Mapping[str, str], cutoff: date) -> tuple[str, str, str]:
    """``(price, status, detail)`` of the row's *own* snapshot price as of the report cutoff.

    The price is the variation's own sales-extract price. It is not valid before it was extracted:
    an extract dated after the cutoff yields no price. An older extract is shown, labelled stale
    beyond ``STALE_SNAPSHOT_DAYS``. Only the extract date is known, never a price time.
    """
    status = row["shopee_price_status"]
    price = row.get("shopee_current_price_php", "")
    if status != SNAPSHOT_PRICE or not price:
        return "", status or "PRICE_STATUS_MISSING", row["shopee_price_detail"]
    try:
        extracted = date.fromisoformat(row["shopee_price_observed_at"])
    except ValueError:
        return "", "PRICE_DATE_INVALID", "the price record date is not a valid date"
    from_m_minus_1 = row.get("shopee_price_date_basis") == "M_MINUS_1_RECORD_DATE"
    source = (
        f"its M-1 {row.get('price_period_month', '')} source record of {extracted}"
        if from_m_minus_1
        else f"the sales extract of {extracted}"
    )
    if extracted > cutoff:
        return (
            "",
            SNAPSHOT_AFTER_CUTOFF,
            f"the price record ({extracted}) is later than the report cutoff ({cutoff})",
        )
    age = (cutoff - extracted).days
    if age > STALE_SNAPSHOT_DAYS:
        return (
            price,
            SNAPSHOT_PRICE_STALE,
            f"own variation's snapshot price from {source}, {age} days before the report cutoff "
            f"(older than {STALE_SNAPSHOT_DAYS} days)"
            + (
                "; the record is monthly, so the exact snapshot time is unknown, and it is not an "
                "observation of a later month"
                if from_m_minus_1
                else ""
            ),
        )
    return (
        price,
        SNAPSHOT_PRICE,
        (
            f"own variation's historical snapshot price from {source} "
            "(record date only; no per-price observation time is known)"
        ),
    )


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value) if value else None
    except InvalidOperation:
        return None


def build_variation_reports(
    top: Sequence[Mapping[str, str]],
    contexts: Sequence[Mapping[str, str]],
    context_list_sha256: str,
    report: ContextReport,
) -> VariationReports:
    """Join the ranked variations to their contexts' reviewed report (see the module docstring)."""
    if not top:
        raise VariationReportError("the selected variation list has no rows")
    for name in VARIATION_COLUMNS:
        if name not in top[0]:
            raise VariationReportError(f"the selected variation list lacks column {name}")
    keys = [row["source_key"] for row in top]
    if len(keys) != len(set(keys)):
        raise VariationReportError("the selected variation list repeats a source_key")
    if {row["schema_version"] for row in top} != {SCHEMA_VERSION} or {
        row["row_grain"] for row in top
    } != {ROW_GRAIN}:
        raise VariationReportError("the selected list is not a shopee-variation-sheets-1 list")
    selected_info = report.summary.get("selected_list", {})
    if selected_info.get("sha256") != context_list_sha256:
        raise VariationReportError(
            "the context report was not built from this run's context selected list "
            "(SHA-256 differs); rebuild it with track_prices.py from context_selected_list.csv"
        )
    sales = report.summary.get("sales_snapshot", {})
    if {row["sales_run_id"] for row in top} != {sales.get("run_id")}:
        raise VariationReportError("the context report is from a different sales snapshot")
    context_ids = [context["context_id"] for context in contexts]
    if set(context_ids) != set(report.comparison):
        raise VariationReportError("the context report's targets differ from the run's contexts")
    row_contexts = {row["context_id"] for row in top if row["context_id"]}
    if row_contexts != set(context_ids):
        raise VariationReportError("selected rows and the run's contexts disagree")
    for row in top:
        if row["mapping_status"] != MAPPED_VERIFIED or not row["context_id"]:
            raise VariationReportError(
                f"{row['source_key']} is not a verified-mapped variation; the Top 100 holds only "
                "eligible mapped Shopee variations (rebuild with build_top_shopee_variations.py)"
            )
        if row["comparison_status"] != CONTEXT_READY:
            raise VariationReportError(f"{row['source_key']} has a context but is not ready")
    week = report.summary["report_week"]
    as_of = report.summary["as_of_utc"]
    cutoff = datetime.fromisoformat(as_of).astimezone(ZoneInfo(REPORTING_TIMEZONE)).date()
    context_rank = {c["context_id"]: c["context_order"] for c in contexts}

    comparison, selections, alternatives, rechecks, history = [], [], [], [], []
    alternatives_by_target: dict[str, list[dict[str, str]]] = defaultdict(list)
    for alt in report.alternatives:
        alternatives_by_target[alt["target_id"]].append(alt)
    rechecks_by_target: dict[str, list[dict[str, str]]] = defaultdict(list)
    for recheck in report.rechecks:
        rechecks_by_target[recheck["target_id"]].append(recheck)

    for row in top:
        context_id = row["context_id"]
        context = report.comparison.get(context_id) if context_id else None
        price, price_status, price_detail = _shopee_price_at_cutoff(row, cutoff)
        statuses = {code: context[f"{code}_Price_Status"] for code in CODES} if context else {}
        if context is None:
            row_status = row["comparison_status"]
            detail = row["comparison_detail"]
        elif any(status == SELECTED for status in statuses.values()):
            row_status, detail = (
                MATCHING_PRICES_FOUND,
                "At least one retailer shows a matching price.",
            )
        elif any(status == ALTERNATIVE_SELECTED for status in statuses.values()):
            row_status = ALTERNATIVE_PRICES_ONLY
            detail = "No matching retailer price; at least one retailer shows a close alternative."
        else:
            row_status = NO_RETAILER_PRICE
            detail = "Comparison context ready; no retailer shows a comparable price this week."
        out = dict.fromkeys(VARIATION_COMPARISON_COLUMNS, "")
        out |= {
            "report_week_start": week["start"],
            "report_week_end": week["end_exclusive"],
            "week_status": week["status"],
            "as_of": as_of,
            "rank": row["rank"],
            "comparison_row_id": row["comparison_row_id"],
            "source_key": row["source_key"],
            "shop_id": row["shop_id"],
            "shop_name": row["shop_name"],
            "item_id": row["item_id"],
            "source_model_id": row["source_model_id"],
            "item_name": row["item_name"],
            "shopee_model_name": row["shopee_model_name"],
            "target_id": row["target_id"],
            "context_id": context_id,
            "mapping_status": row["mapping_status"],
            "comparison_status": row["comparison_status"],
            "row_comparison_status": row_status,
            "row_comparison_detail": detail,
            "brand": row["brand"],
            "series": row["series"],
            "model": row["model"],
            "screen_size_in": row["screen_size_in"],
            "cpu_series": row["cpu_series"],
            "cpu_model": row["cpu_model"],
            "ram_capacity_gb": row["ram_capacity_gb"],
            "storage_capacity_gb": row["storage_capacity_gb"],
            "gpu_model": row["gpu_model"],
            "m_minus_1_gmv_usd": row["m_minus_1_gmv_usd"],
            "mtd_gmv_usd": row["mtd_gmv_usd"],
            "ranking_gmv_usd": row["ranking_gmv_usd"],
            "shopee_price_php": price,
            "shopee_price_status": price_status,
            "shopee_price_detail": price_detail,
            "shopee_price_observed_at": row["shopee_price_observed_at"],
            "shopee_price_date_basis": row["shopee_price_date_basis"],
            "shopee_listing_url": row["shopee_listing_url"],
            "sales_extract_date": row["sales_extract_date"],
            "sales_run_id": row["sales_run_id"],
            "m_minus_1_month": row["m_minus_1_month"],
            "mtd_month": row["mtd_month"],
            "rank_metric": row["rank_metric"],
            "rank_metric_scope": row["rank_metric_scope"],
            "matching_context_id": context["matching_context_id"] if context else "",
            "selected_list_sha256": context_list_sha256,
            "context_rank": context_rank.get(context_id, ""),
            "schema_version": SCHEMA_VERSION,
            "row_grain": ROW_GRAIN,
        }
        if context is not None:
            for name in CONTEXT_COPY_COLUMNS:
                out[name] = context[name]
        else:
            for code in CODES:
                out[f"{code}_Price_Status"] = row_status
                out[f"{code}_Reason"] = detail
        comparison.append(out)

        row_identity = {
            "comparison_row_id": row["comparison_row_id"],
            "source_key": row["source_key"],
            "shop_id": row["shop_id"],
            "item_id": row["item_id"],
            "source_model_id": row["source_model_id"],
            "shop_name": row["shop_name"],
            "context_id": context_id,
            "schema_version": SCHEMA_VERSION,
            "row_grain": ROW_GRAIN,
        }
        for code, retailer_id in RETAILERS:
            base = report.selections.get((context_id, code)) if context else None
            if base is not None:
                kind = {SELECTED: "CONTEXT_MATCH", ALTERNATIVE_SELECTED: "CONTEXT_ALTERNATIVE"}.get(
                    base["price_status"], "NO_OFFER"
                )
                selection = dict(base)
                selection["rank"] = row["rank"]
            else:
                kind = "NO_CONTEXT"
                selection = dict.fromkeys(SELECTION_COLUMNS, "") | {
                    "report_week_start": week["start"],
                    "report_week_end": week["end_exclusive"],
                    "week_status": week["status"],
                    "rank": row["rank"],
                    "target_id": row["target_id"],
                    "retailer_code": code,
                    "retailer_id": retailer_id,
                    "price_status": row_status,
                    "selection_reason": detail,
                }
            assoc = association_id(
                row["comparison_row_id"], code, selection["observation_id"], kind
            )
            selections.append(
                {name: "" for name in VARIATION_SELECTION_COLUMNS}
                | selection
                | row_identity
                | {"association_id": assoc, "association_kind": kind}
            )
            if kind in {"CONTEXT_MATCH", "CONTEXT_ALTERNATIVE"} and selection["observation_id"]:
                alt_row = _displayed_alternative(
                    alternatives_by_target[context_id], code, selection["observation_id"]
                )
                history.append(
                    dict.fromkeys(HISTORY_ASSOCIATION_COLUMNS, "")
                    | row_identity
                    | {
                        "association_id": assoc,
                        "rank": row["rank"],
                        "retailer_code": code,
                        "observation_id": selection["observation_id"],
                        "association_kind": kind,
                        "price": selection["price"],
                        "difference_count": "0"
                        if kind == "CONTEXT_MATCH"
                        else alt_row.get("difference_count", ""),
                        "difference_tag": "MATCH"
                        if kind == "CONTEXT_MATCH"
                        else alt_row.get("alternative_tag", ""),
                        "difference_details": ""
                        if kind == "CONTEXT_MATCH"
                        else alt_row.get("differences", ""),
                        "unknown_fields": ""
                        if kind == "CONTEXT_MATCH"
                        else alt_row.get("unknown_fields", ""),
                    }
                )
        if context is None:
            continue
        for alt in alternatives_by_target[context_id]:
            assoc = association_id(
                row["comparison_row_id"],
                alt["retailer_code"],
                alt["observation_id"],
                alt["alternative_tag"],
            )
            alternatives.append(
                dict.fromkeys(VARIATION_ALTERNATIVE_COLUMNS, "")
                | alt
                | row_identity
                | {"association_id": assoc, "rank": row["rank"]}
            )
        for recheck in rechecks_by_target[context_id]:
            assoc = association_id(
                row["comparison_row_id"], recheck["retailer_code"], recheck["url"], "RECHECK"
            )
            rechecks.append(
                dict.fromkeys(VARIATION_RECHECK_COLUMNS, "")
                | recheck
                | row_identity
                | {"association_id": assoc, "rank": row["rank"]}
            )

    coverage = _coverage(top, comparison, selections, alternatives, contexts)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "ranking_grain": ROW_GRAIN,
        "row_grain": ROW_GRAIN,
        "report_week": week,
        "as_of_utc": as_of,
        "sales_snapshot": sales,
        "context_report_dir": str(report.report_dir),
        "context_report_sha256": report.sha256,
        "context_list_sha256": context_list_sha256,
        "matching_context": report.summary.get("matching_context", {}),
        "counts": coverage["counts"],
        "shopee_price_rule": (
            "Each row shows its own variation's snapshot price from the sales extract, as of the "
            "report cutoff (extract date only). No other seller, parent, default or cheapest "
            "price is used, and the configuration-level snapshot minimum is not copied."
        ),
    }
    return VariationReports(
        comparison, selections, alternatives, rechecks, history, coverage, summary
    )


def _displayed_alternative(
    alternatives: Sequence[Mapping[str, str]], code: str, observation_id: str
) -> Mapping[str, str]:
    return next(
        (
            alt
            for alt in alternatives
            if alt["retailer_code"] == code and alt["observation_id"] == observation_id
        ),
        {},
    )


def _coverage(
    top: Sequence[Mapping[str, str]],
    comparison: Sequence[Mapping[str, str]],
    selections: Sequence[Mapping[str, str]],
    alternatives: Sequence[Mapping[str, str]],
    contexts: Sequence[Mapping[str, str]],
) -> dict:
    """Variation-row coverage, configuration-context coverage and unique observation counts, kept
    apart: one observation can fill several rows and is counted once."""
    rows = len(comparison)
    by_status = Counter(row["row_comparison_status"] for row in comparison)
    matching_rows = [
        r for r in comparison if any(r[f"{c}_Price_Status"] == SELECTED for c in CODES)
    ]
    alternative_rows = [
        r for r in comparison if any(r[f"{c}_Price_Status"] == ALTERNATIVE_SELECTED for c in CODES)
    ]
    any_price = [
        r
        for r in comparison
        if any(r[f"{c}_Price_Status"] in {SELECTED, ALTERNATIVE_SELECTED} for c in CODES)
    ]
    context_status: dict[str, set[str]] = defaultdict(set)
    for row in comparison:
        if row["context_id"]:
            for code in CODES:
                context_status[row["context_id"]].add(row[f"{code}_Price_Status"])
    contexts_matching = sum(1 for s in context_status.values() if SELECTED in s)
    contexts_alternative_only = sum(
        1 for s in context_status.values() if SELECTED not in s and ALTERNATIVE_SELECTED in s
    )
    match_obs = {
        s["observation_id"]
        for s in selections
        if s["price_status"] == SELECTED and s["observation_id"]
    }
    alt_obs_selected = {
        s["observation_id"]
        for s in selections
        if s["price_status"] == ALTERNATIVE_SELECTED and s["observation_id"]
    }
    eligible_alternatives = [a for a in alternatives if a["price_eligibility"] == COMPARABLE]
    unique_alt_offers = {a["observation_id"]: a["difference_count"] for a in eligible_alternatives}
    alt_levels = Counter(unique_alt_offers.values())
    match_assoc = sum(1 for s in selections if s["association_kind"] == "CONTEXT_MATCH")
    alt_assoc = sum(1 for s in selections if s["association_kind"] == "CONTEXT_ALTERNATIVE")
    per_retailer = {}
    for code in CODES:
        cells = [s for s in selections if s["retailer_code"] == code]
        per_retailer[code] = {
            "rows_with_matching_price": sum(1 for s in cells if s["price_status"] == SELECTED),
            "rows_with_alternative_price": sum(
                1 for s in cells if s["price_status"] == ALTERNATIVE_SELECTED
            ),
            "unique_matching_observations": len(
                {s["observation_id"] for s in cells if s["price_status"] == SELECTED}
            ),
            "unique_alternative_observations": len(
                {s["observation_id"] for s in cells if s["price_status"] == ALTERNATIVE_SELECTED}
            ),
            "contexts_with_matching_price": len(
                {s["context_id"] for s in cells if s["price_status"] == SELECTED}
            ),
        }
    own_price = sum(1 for r in comparison if r["shopee_price_php"])
    counts = {
        "selected_variations": rows,
        "distinct_configuration_contexts": len(contexts),
        "mapped_selected_variations": sum(
            1 for r in comparison if r["mapping_status"] == MAPPED_VERIFIED
        ),
        "unmapped_selected_variations": sum(
            1 for r in comparison if r["mapping_status"] == "UNMAPPED"
        ),
        "rows_with_trusted_context": sum(1 for r in comparison if r["context_id"]),
        "rows_without_trusted_context": sum(1 for r in comparison if not r["context_id"]),
        "rows_by_comparison_status": dict(sorted(by_status.items())),
        "rows_with_own_shopee_price": own_price,
        "rows_without_own_shopee_price": rows - own_price,
        "matching_priced_variation_rows": len(matching_rows),
        "alternative_priced_variation_rows": len(alternative_rows),
        "variation_rows_with_any_retailer_price": len(any_price),
        "contexts_with_matching_price": contexts_matching,
        "contexts_with_alternative_only": contexts_alternative_only,
        "contexts_without_retailer_price": len(contexts)
        - contexts_matching
        - contexts_alternative_only,
        "unique_matching_observations": len(match_obs),
        "unique_alternative_observations_displayed": len(alt_obs_selected),
        "row_associations_matching": match_assoc,
        "row_associations_alternative_displayed": alt_assoc,
        "alt_1_offers": alt_levels.get("1", 0),
        "alt_2_offers": alt_levels.get("2", 0),
        "alternative_row_associations": len(alternatives),
        "alternative_row_associations_eligible": len(eligible_alternatives),
        "unique_alternative_offers_eligible": len(unique_alt_offers),
    }
    return {"counts": counts, "per_retailer": per_retailer}


def validate_variation_reports(reports: VariationReports) -> list[str]:
    """Independent checks of a built report (an empty list means it is consistent)."""
    issues = []
    rows = reports.comparison
    ids = [r["comparison_row_id"] for r in rows]
    if len(ids) != len(set(ids)) or len({r["source_key"] for r in rows}) != len(rows):
        issues.append("comparison rows repeat a comparison_row_id or source_key")
    if [r["rank"] for r in rows] != [str(n) for n in range(1, len(rows) + 1)]:
        issues.append("variation ranks are not 1..n in order")
    if len(reports.selections) != len(rows) * len(CODES):
        issues.append("selections are not one row per variation and retailer")
    assoc = [s["association_id"] for s in reports.selections]
    if len(assoc) != len(set(assoc)):
        issues.append("selection association IDs repeat")
    for r in rows:
        if r["shopee_price_php"] and _decimal(r["shopee_price_php"]) is None:
            issues.append(f"{r['source_key']} price is not numeric")
        if not r["context_id"]:
            for code in CODES:
                if r[f"{code}_Price"]:
                    issues.append(f"{r['source_key']} has a retailer price without a context")
        if r["row_grain"] != ROW_GRAIN or r["schema_version"] != SCHEMA_VERSION:
            issues.append(f"{r['source_key']} has the wrong grain or schema")
    return issues


def write_reports(output_dir: Path, reports: VariationReports) -> dict[str, Path]:
    from .verified_top import write_csv

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {key: output_dir / name for key, name in REPORT_FILES.items()}
    write_csv(paths["comparison"], VARIATION_COMPARISON_COLUMNS, reports.comparison)
    write_csv(paths["selections"], VARIATION_SELECTION_COLUMNS, reports.selections)
    write_csv(paths["alternatives"], VARIATION_ALTERNATIVE_COLUMNS, reports.alternatives)
    write_csv(paths["rechecks"], VARIATION_RECHECK_COLUMNS, reports.rechecks)
    write_csv(paths["history"], HISTORY_ASSOCIATION_COLUMNS, reports.history)
    paths["coverage"].write_text(json.dumps(reports.coverage, indent=2) + "\n", encoding="utf-8")
    paths["summary"].write_text(json.dumps(reports.summary, indent=2) + "\n", encoding="utf-8")
    return paths
