"""Weekly retailer price comparisons and price history for the verified working list (Phase 9).

Policy (starting rules, 2026-10-12):

- Currency is PHP. Prices stay exact decimal text from the observation; nothing is converted,
  averaged, or mixed with the USD Shopee GMV.
- Reporting is weekly. A report week runs Monday 00:00 to the next Monday 00:00 in
  Asia/Singapore (end exclusive). Only observations retrieved inside the week, and no later than
  the run's explicit ``as_of`` time, can supply that week's prices. Earlier prices are never
  carried forward; they are shown separately as stale references.
- The price basis is the trusted displayed price (``REGULAR`` or ``SALE`` price type).
  Vouchers and shipping are not part of it; they are not observed and stay unknown.
- A price is comparable only for an ``EXACT`` match to a selected target that is ``IN_STOCK``
  or ``OUT_OF_STOCK``, with its actual availability retained,
  in PHP, with a known price type and acceptable purchase terms. Under the purchase-terms
  policy (2026-10-19, ``offer_terms.py``) an ordinary offer with stated inclusions (Office,
  warranty, backpack) is comparable with an INCLUDED_ITEMS_OFFER note; a paid package, a price
  that requires another purchase, a restricted (financing, card, trade-in, member) price, and
  bundle wording without evidence of the terms are excluded with their reason. The earlier
  bundle-flag policy (2026-10-15: every checked bundle comparable with a BUNDLE_OFFER note) can
  still be selected. Everything else is kept in history with its status and reasons.
- Each offer is represented in a week by its latest observation in that week. For one target and
  retailer, the lowest comparable price is selected; ties go to the latest retrieval, then the
  offer URL, offer index, and observation ID. All underlying observations stay in history.
- Two minimums are reported from the same comparable set (2026-10-15): ``Lowest_Price`` is the
  lowest *observed* comparable price, out-of-stock included (current owner policy), and
  ``Lowest_In_Stock_Price`` uses only comparable offers recorded ``IN_STOCK``; it is blank when
  there is none. Each names its retailer(s), link, observation time, and stock state.
- A missing price is blank with a reason code, never zero.

The working scope is the selected target list. The full catalog is still used for matching, so
offers for other catalog targets are kept and labelled out of scope.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

from . import (
    conventions,
    evidence_approvals,
    extraction,
    listed_spec,
    matching,
    normalization,
    product_names,
)
from .alternatives import (
    ALTERNATIVE,
    DIMENSIONS,
    NO_DIFFERENCE,
    RECHECK,
    AlternativeEvaluation,
    AlternativeEvaluator,
    OfferEvidence,
    offer_evidence,
)
from .conventions import canonical_gpu_models
from .extraction import ExtractionStatus, extract_offer_attributes
from .listed_spec import (
    EXACT_PRODUCT_BASIS,
    IDENTITY_UNRESOLVED_WARNING,
    LISTED_SPEC_AMBIGUOUS,
    LISTED_SPEC_BASIS,
    LISTED_SPEC_MATCH,
    ListedSpecMatcher,
    ListedSpecResult,
    offer_details,
)
from .matching import (
    DIFFERENT_MODEL,
    SAME_MODEL_DIFFERENT_CONFIGURATION,
    ExactMatcher,
    MatchStatus,
    configuration_outcome,
    core_differences,
    is_warning_code,
    match_basis_from_codes,
)
from .normalization import CORE_MATCH_COLUMNS, core_key
from .observation_store import (
    MatchingContext,
    ObservationStore,
    StoredMatch,
    StoredObservation,
)
from .offer_terms import (
    BUNDLE_FLAG_POLICY,
    DEFAULT_OFFER_TERMS_POLICY,
    OFFER_TERMS_POLICIES,
    OfferTerms,
    classify_offer_terms,
    terms_price_effect,
)
from .offers import Availability, PriceType
from .recheck import (
    FETCHED_NOT_CONFIRMED,
    RecheckCandidate,
    merge_rechecks,
    offer_explanation,
    recheck_note,
    url_key,
)
from .shopee_snapshot import ShopeeSelection, price_text
from .supporting_tables import TableResult
from .target_master import (
    CONFIGURATION,
    LISTING_ONLY,
    TargetMasterResult,
    effective_tracking_mode,
)
from .verified_top import CONTRIBUTING, money

REPORTING_TIMEZONE = "Asia/Singapore"
REPORTING_CURRENCY = "PHP"
MATCHING_RULES_VERSION = "five-field-exact+listed-spec-fallback+bound-color-storage/2026-10-25"
# Modules whose code defines matching outcomes; their bytes fingerprint the rule version.
RULE_MODULES: tuple[ModuleType, ...] = (
    normalization, conventions, evidence_approvals, extraction, matching, product_names,
    listed_spec,
)

# Retailer price columns in the requested order: code, adapter retailer_id.
RETAILERS: tuple[tuple[str, str], ...] = (
    ("DBZ", "datablitz"),
    ("LF", "laptop_factory"),
    ("PCX", "pcx"),
    ("PCC", "pcc"),
    ("OCT", "oct"),
)
RETAILER_CODE_BY_ID = {retailer_id: code for code, retailer_id in RETAILERS}
PRICE_COLUMNS = tuple(f"{code}_Price" for code, _ in RETAILERS)
PRICE_STATUS_COLUMNS = tuple(f"{code}_Price_Status" for code, _ in RETAILERS)

# Requested product fields and their TARGET_MASTER columns (units: inches, Hz, GB).
PRODUCT_FIELDS: tuple[tuple[str, str], ...] = (
    ("Brand", "brand"),
    ("Series", "series"),
    ("Model", "model"),
    ("Screen Size", "screen_size_in"),
    ("Resolution", "resolution"),
    ("Display Type", "display_type"),
    ("Refresh Rate", "refresh_rate_hz"),
    ("CPU_Brand", "cpu_brand"),
    ("CPU_Series", "cpu_series"),
    ("CPU_Generation", "cpu_generation"),
    ("CPU_Cores", "cpu_cores"),
    ("RAM_Capacity", "ram_capacity_gb"),
    ("RAM_Type", "ram_type"),
    ("Storage_Capacity", "storage_capacity_gb"),
    ("Storage_Type", "storage_type"),
    ("GPU Type", "gpu_type"),
    ("GPU_Brand", "gpu_brand"),
    ("GPU_Model", "gpu_model"),
    ("VRAM", "vram"),
    ("OS", "os"),
    ("Battery_Capacity", "battery_capacity"),
    ("Build_Material", "build_material"),
)

# The comparison layout before 2026-10-15. Readers of saved reports require only these columns,
# so earlier report folders still load; the added columns below are optional for them.
LEGACY_COMPARISON_COLUMNS: tuple[str, ...] = (
    "report_week_start",
    "report_week_end",
    "reporting_timezone",
    "week_status",
    "as_of",
    "rank",
    "target_id",
    *(name for name, _ in PRODUCT_FIELDS),
    "Shopee_Source_Count",
    "Shopee_Source_Model_IDs",
    "Shopee_Total_GMV_USD",
    "Shopee_GMV_Period",
    "Shopee_Sales_Run_ID",
    "Currency",
    *(column for code, _ in RETAILERS for column in (f"{code}_Price", f"{code}_Offer_URL")),
    *PRICE_STATUS_COLUMNS,
    "Comparable_Retailer_Count",
    "Lowest_Price",
    "Lowest_Price_Retailers",
    "matching_context_id",
    "selected_list_sha256",
)
# Per retailer: stock state and observation time of the offer behind the cell, how its identity
# was supported, nonblocking warnings, and why a price is blank (2026-10-15).
RETAILER_DETAIL_SUFFIXES: tuple[str, ...] = (
    "Stock", "Observed_At", "Match_Basis", "Warnings", "Reason",
    # An offer found for this configuration that is not a confirmed match (2026-10-19): its
    # link, outcome, and the differing values. Its price is never shown in the comparison.
    "Candidate_URL", "Candidate_Status", "Candidate_Detail",
    # Recheck candidates (2026-10-19, second review): how many relevant unconfirmed pages are
    # listed for a blank cell (recheck_candidates.csv), and the first one's reason in words.
    "Recheck_Count", "Recheck_Note",
    # The chosen offer's own model, processor and material hardware, as it states them
    # (2026-10-20): a listed-spec price never claims full configuration equivalence.
    "Offer_Details",
)
# Per retailer (2026-10-27): the cell's comparison tag (MATCH, ALT 1 | RAM, RECHECK), the
# difference count, differences and unknown fields of an alternative shown in the cell, its
# observation, and how many eligible alternative offers the retailer has in all.
ALTERNATIVE_RETAILER_SUFFIXES: tuple[str, ...] = (
    "Comparison_Tag", "Alternative_Level", "Alternative_Differences", "Alternative_Unknowns",
    "Alternative_Observation_ID", "Alternative_Offers",
)
_ALT_MINIMUM_FIELDS: tuple[str, ...] = (
    "", "_Retailers", "_Offer_URL", "_Observed_At", "_Stock", "_Notes", "_Tag",
    "_Difference_Count", "_Differences", "_Unknowns", "_Observation_IDs",
)
# Alternative-only minimums over the full eligible alternative pool (hidden offers included).
ALTERNATIVE_MINIMUM_COLUMNS: tuple[str, ...] = (
    *(f"Lowest_Alternative_Price{suffix}" for suffix in _ALT_MINIMUM_FIELDS),
    "Lowest_Alternative_Price_Label",
    *(f"Lowest_Alternative_In_Stock_Price{suffix}" for suffix in _ALT_MINIMUM_FIELDS
      if suffix != "_Stock"),
    "Lowest_Alternative_In_Stock_Price_Label",
)
SHOPEE_COMPARISON_COLUMNS: tuple[str, ...] = (
    "Shopee_Price", "Shopee_Listing_URL", "Shopee_Variation", "Shopee_Item_Name",
    "Shopee_Model_ID", "Shopee_Shop_ID", "Shopee_Item_ID", "Shopee_Price_Status",
    "Shopee_Price_Detail", "Shopee_Snapshot_Date", "Shopee_Snapshot_Label",
)
COMPARISON_COLUMNS: tuple[str, ...] = (
    *LEGACY_COMPARISON_COLUMNS[: LEGACY_COMPARISON_COLUMNS.index("Comparable_Retailer_Count")],
    *(f"{code}_{suffix}" for code, _ in RETAILERS for suffix in RETAILER_DETAIL_SUFFIXES),
    "Comparable_Retailer_Count",
    # Lowest_Price is the lowest *observed* comparable price (out-of-stock included).
    "Lowest_Price",
    "Lowest_Price_Retailers",
    "Lowest_Price_Offer_URL",
    "Lowest_Price_Observed_At",
    "Lowest_Price_Stock",
    "Lowest_Price_Notes",
    "Lowest_In_Stock_Price",
    "Lowest_In_Stock_Price_Retailers",
    "Lowest_In_Stock_Offer_URL",
    "Lowest_In_Stock_Observed_At",
    "Lowest_In_Stock_Notes",
    "Shopee_Sales_Extract_Date",
    "matching_context_id",
    "selected_list_sha256",
    # Matching basis of the configuration's prices and of each minimum (2026-10-20):
    # EXACT_PRODUCT, LISTED_SPEC or MIXED; the labels say "matching listed specs" whenever a
    # listed-spec price is among those compared.
    "Comparison_Basis",
    "Lowest_Price_Basis",
    "Lowest_Price_Label",
    "Lowest_In_Stock_Basis",
    "Lowest_In_Stock_Label",
    # The configuration's Shopee snapshot price (2026-10-20, shopee_snapshot.py): the lowest
    # eligible mapped-listing price of the sales extract, dated by that extract. Never a current
    # price, never stock-verified, never part of a retailer minimum or count.
    *SHOPEE_COMPARISON_COLUMNS,
    # Close alternatives (2026-10-27, alternatives.py), appended at the right: a tag for every
    # retailer cell, the detail of an alternative shown in a cell, and the alternative-only
    # counts and minimums. The columns above stay matching-only.
    *(f"{code}_{suffix}" for code, _ in RETAILERS for suffix in ALTERNATIVE_RETAILER_SUFFIXES),
    "Alternative_Retailer_Count",
    "Comparison_Retailer_Count",
    *ALTERNATIVE_MINIMUM_COLUMNS,
)

LEGACY_SELECTION_COLUMNS: tuple[str, ...] = (
    "report_week_start",
    "report_week_end",
    "week_status",
    "rank",
    "target_id",
    "retailer_code",
    "retailer_id",
    "price_status",
    "price",
    "currency",
    "price_type",
    "regular_price",
    "sale_price",
    "offer_url",
    "offer_index",
    "retailer_product_id",
    "retailer_sku",
    "retrieved_at_utc",
    "retrieved_at_local",
    "observation_id",
    "offer_key",
    "selection_reason",
    "comparable_offers",
    "tied_offers",
    "offers_observed",
    "observations_in_week",
    "not_comparable_reasons",
    "review_required_observations",
    "stale_last_comparable_retrieved_at_utc",
    "stale_last_comparable_price",
    "stale_last_comparable_offer_url",
    "adapter_version",
    "content_sha256",
    "matching_context_id",
)
SELECTION_COLUMNS: tuple[str, ...] = (
    *LEGACY_SELECTION_COLUMNS,
    "availability",
    "match_basis",
    "match_warnings",
    "price_notes",
    "in_stock_price",
    "in_stock_offer_url",
    "in_stock_retrieved_at_utc",
    "in_stock_observation_id",
    "in_stock_price_notes",
    "latest_exact_offer_url",
    "latest_exact_availability",
    "latest_exact_retrieved_at_utc",
    "candidate_offer_url",
    "candidate_status",
    "candidate_detail",
    "candidate_observation_id",
    "recheck_count",
    "recheck_note",
    "comparison_basis",
    "offer_details",
    # Close alternatives (2026-10-27). The columns above describe the offer shown in the cell
    # (a match, or an alternative when ``selection_kind`` says so); the match-only in-stock and
    # latest-exact columns are unchanged.
    "selection_kind",
    "alternative_tag",
    "alternative_level",
    "alternative_differences",
    "alternative_observation_id",
    "alternative_pool_size",
    "alternative_lowest_price",
    "alternative_lowest_observation_id",
    "alternative_lowest_in_stock_price",
    "alternative_lowest_in_stock_observation_id",
)

LEGACY_HISTORY_COLUMNS: tuple[str, ...] = (
    "observation_id",
    "offer_key",
    "retailer_code",
    "retailer_id",
    "offer_url",
    "offer_index",
    "retailer_product_id",
    "retailer_sku",
    "retrieved_at_utc",
    "retrieved_at_local",
    "report_week_start",
    "price",
    "currency",
    "price_type",
    "regular_price",
    "sale_price",
    "availability",
    "bundle_flag",
    "match_status",
    "target_id",
    "candidate_target_ids",
    "match_reason_codes",
    "in_selected_list",
    "selected_rank",
    "comparability_status",
    "comparability_reasons",
    "latest_for_offer_in_week",
    "previous_comparable_observation_id",
    "previous_comparable_retrieved_at_utc",
    "previous_comparable_price",
    "observations_since_previous_comparable",
    "price_change",
    "price_change_status",
    "adapter_version",
    "content_sha256",
    "first_import_batch_id",
    "matching_context_id",
    "target_master_sha256",
    "matching_rules_version",
    "offer_terms_category",
    "offer_terms_evidence",
    "price_notes",
    "search_context_target_ids",
    # Listed-spec association (2026-10-20): never an exact assignment; kept beside it.
    "listed_spec_status",
    "listed_spec_target_ids",
    "listed_spec_ambiguous_target_ids",
    "listed_spec_comparability",
    "listed_spec_comparability_reasons",
)
# Close-alternative associations of the observation (2026-10-27), kept beside its exact
# assignment and listed-spec association; an exact assignment is never rewritten. Report folders
# written before this change lack them and stay readable (``LEGACY_HISTORY_COLUMNS``).
HISTORY_COLUMNS: tuple[str, ...] = (
    *LEGACY_HISTORY_COLUMNS,
    "alternative_target_ids",
    "alternative_tags",
    "alternative_recheck_target_ids",
    "alternative_comparability",
    "alternative_comparability_reasons",
)

# One row per target-retailer-offer association that qualifies as a close alternative
# (weekly_price_alternatives.csv). Price eligibility is stated per row: the minimums and main
# displays use eligible rows only, the sheet lists every qualifying association.
ALTERNATIVE_COLUMNS: tuple[str, ...] = (
    "report_week_start", "report_week_end", "week_status", "rank", "target_id",
    "target_brand", "target_model", "target_screen_in", "target_cpu_series", "target_cpu_model",
    "target_ram_gb", "target_storage_gb", "target_gpu",
    "retailer_code", "retailer_id", "offer_key", "observation_id", "offer_url", "offer_index",
    "retailer_product_id", "retailer_sku", "title",
    "retrieved_at_utc", "retrieved_at_local", "content_sha256", "adapter_version",
    "price", "currency", "price_type", "regular_price", "sale_price", "availability",
    "bundle_flag", "price_eligibility", "price_exclusion_reasons", "price_notes",
    "alternative_tag", "difference_count", "differing_dimensions", "differences",
    "model_state", "screen_state", "cpu_state", "ram_state", "storage_state", "gpu_state",
    "offer_brand", "offer_model", "offer_screen_in", "offer_cpu_series", "offer_cpu_model",
    "offer_ram_gb", "offer_storage_gb", "offer_gpu",
    "unknown_fields", "limitations", "warnings", "comparison_basis", "family", "family_basis",
    "displayed_in_comparison", "retailer_has_matching_offer", "exact_target_id",
    "listed_spec_target_ids", "initiating_target_ids", "evidence_references",
    "comparison_detail_json", "policy_version", "matching_context_id",
    "target_master_sha256",
)

# Every offer-target evaluation of the report week (alternative_evaluations.csv): alternatives,
# rechecks (unresolved evidence), exclusions and zero-difference pairs, for audit.
EVALUATION_COLUMNS: tuple[str, ...] = (
    "report_week_start", "rank", "target_id", "retailer_code", "observation_id", "offer_url",
    "retrieved_at_utc", "status", "alternative_tag", "difference_count", "differing_dimensions",
    "conflicting_dimensions", "unknown_dimensions", "reasons", "material_conflicts",
    "limitations", "family", "family_basis", "exact_target_id", "initiating_target_ids",
    "differences", "policy_version",
)

SERIES_COLUMNS: tuple[str, ...] = (
    "report_week_start",
    "report_week_end",
    "week_status",
    "rank",
    "target_id",
    "retailer_code",
    "price_status",
    "price",
    "offer_key",
    "offer_url",
    "retrieved_at_utc",
    "observation_id",
    "previous_week_price_status",
    "previous_week_price",
    "price_change",
    "change_status",
    "representative_offer_changed",
)

REVIEW_COLUMNS: tuple[str, ...] = (
    "report_week_start",
    "review_reason",
    "observation_id",
    "retailer_code",
    "retailer_id",
    "offer_url",
    "retrieved_at_utc",
    "price",
    "currency",
    "availability",
    "bundle_flag",
    "match_status",
    "target_id",
    "candidate_target_ids",
    "match_reason_codes",
    "in_selected_list",
    "comparability_status",
    "comparability_reasons",
    "matching_context_id",
    "offer_terms_category",
    "offer_terms_evidence",
    "search_context_target_ids",
)

SOURCE_LEDGER_COLUMNS: tuple[str, ...] = (
    "rank",
    "target_id",
    "model_id",
    "shop_id",
    "item_id",
    "item_name",
    "shopee_model_name",
    "m_minus_1_gmv_usd",
    "mtd_gmv_usd",
    "ranking_gmv_usd",
    "in_original_source_top_100",
    "currently_mapped_to_target",
    "shopee_price_php_snapshot",
    "shopee_price_status",
    "sales_run_id",
    "sales_extract_date",
    "sales_period_label",
    # The listing's role in the configuration's Shopee snapshot price (2026-10-20) and its
    # product-page link from the verified shop/item IDs (the link does not select a variation).
    "shopee_price_role",
    "shopee_listing_url",
)

# Comparability statuses.
COMPARABLE = "COMPARABLE"
NOT_COMPARABLE = "NOT_COMPARABLE"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
NOT_MATCHED = "NOT_MATCHED"

# Price statuses for one target and retailer in one week.
SELECTED = "SELECTED"
# The cell shows a close alternative (alternatives.py, 2026-10-27) because no matching
# configuration is comparable. ``SELECTED`` stays matching-only.
ALTERNATIVE_SELECTED = "ALTERNATIVE_SELECTED"
# The basis of a view that stands for one close-alternative association.
ALTERNATIVE_BASIS = "ALTERNATIVE"
# Review-queue reason of an unassigned offer that qualifies as a close alternative.
CLOSE_ALTERNATIVE_REVIEW = "CLOSE_ALTERNATIVE"
NO_OBSERVATION_IN_WEEK = "NO_OBSERVATION_IN_WEEK"
NO_COMPARABLE_OFFER = "NO_COMPARABLE_OFFER"
REVIEW_REQUIRED_ONLY = "REVIEW_REQUIRED_ONLY"
# Offers were found for the configuration (search context or near-match candidates) but none is
# a confirmed match: an unresolved model, missing specifications, or another configuration.
CANDIDATE_NOT_CONFIRMED = "CANDIDATE_NOT_CONFIRMED"

CLOSED_HISTORICAL = "CLOSED_HISTORICAL"
IN_PROGRESS = "IN_PROGRESS"
TIE_BREAK = "LATEST_RETRIEVAL_THEN_OFFER_URL_THEN_OFFER_INDEX_THEN_OBSERVATION_ID"


class PriceTrackingInputError(ValueError):
    """An input is missing, inconsistent, or invalid; nothing is processed."""


# Week windows


@dataclass(frozen=True)
class ReportWeek:
    """One Monday-to-Monday week in the reporting timezone (end exclusive)."""

    start: date

    def __post_init__(self) -> None:
        if self.start.weekday() != 0:
            raise PriceTrackingInputError("A report week must start on a Monday.")

    @property
    def end(self) -> date:
        return self.start + timedelta(days=7)

    @property
    def start_utc(self) -> datetime:
        return datetime.combine(self.start, time(), ZoneInfo(REPORTING_TIMEZONE)).astimezone(UTC)

    @property
    def end_utc(self) -> datetime:
        return datetime.combine(self.end, time(), ZoneInfo(REPORTING_TIMEZONE)).astimezone(UTC)

    def previous(self) -> ReportWeek:
        return ReportWeek(self.start - timedelta(days=7))

    def status(self, as_of: datetime) -> str:
        return CLOSED_HISTORICAL if as_of >= self.end_utc else IN_PROGRESS


def local_time(moment: datetime) -> datetime:
    return moment.astimezone(ZoneInfo(REPORTING_TIMEZONE))


def week_start_of(moment: datetime) -> date:
    """The Monday (reporting timezone) of the week containing ``moment``."""
    local = local_time(moment).date()
    return local - timedelta(days=local.weekday())


def parse_week_start(text: str) -> ReportWeek:
    try:
        start = date.fromisoformat(text)
    except ValueError:
        raise PriceTrackingInputError("--week-start must be a YYYY-MM-DD date.") from None
    return ReportWeek(start)


def parse_as_of(text: str) -> datetime:
    """An explicit ISO 8601 time with a UTC offset, such as 2026-10-12T09:00:00+08:00."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise PriceTrackingInputError("--as-of must be an ISO 8601 date and time.") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise PriceTrackingInputError("--as-of must include a UTC offset, e.g. +08:00.")
    return moment.astimezone(UTC)


def utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def local_text(moment: datetime) -> str:
    return local_time(moment).isoformat(timespec="seconds")


# Selected list and source ledger


@dataclass(frozen=True)
class SelectedConfiguration:
    rank: int
    target_id: str
    list_row: Mapping[str, str]
    target: Mapping[str, object]
    source_model_ids: tuple[str, ...]


@dataclass(frozen=True)
class SelectedList:
    configurations: tuple[SelectedConfiguration, ...]
    sha256: str
    sales_run_id: str
    sales_extract_date: str
    sales_m_minus_1_month: str
    sales_mtd_month: str
    built_on_target_master_sha256: str
    catalog_matches_build: bool

    @property
    def by_target(self) -> dict[str, SelectedConfiguration]:
        return {item.target_id: item for item in self.configurations}

    def gmv_period_label(self) -> str:
        return (
            f"M-1 {self.sales_m_minus_1_month} (full month) + MTD {self.sales_mtd_month} "
            f"as extracted {self.sales_extract_date}; historical Shopee snapshot"
        )


def _decimal(text: str, label: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise PriceTrackingInputError(f"{label} is not a decimal amount.") from None
    if not value.is_finite():
        raise PriceTrackingInputError(f"{label} is not a finite amount.")
    return value


def validate_selected_list(
    rows: Sequence[Mapping[str, str]],
    contributions: Sequence[Mapping[str, str]],
    target_master: TargetMasterResult,
    *,
    target_master_sha256: str,
    list_sha256: str,
) -> SelectedList:
    """Check the working list against the validated catalog and its contribution ledger.

    Every selected target must be a valid ACTIVE ``CONFIGURATION`` row whose five core values
    still equal the list's exact text, and the ledger must show exactly the listed contributors
    and totals. A catalog that differs from the one the list was built on is accepted only when
    every selected identity is unchanged; the difference is reported.
    """
    if not target_master.is_valid:
        raise PriceTrackingInputError("TARGET_MASTER did not pass validation.")
    if not rows:
        raise PriceTrackingInputError("The selected list has no rows.")
    for name in ("sales_run_id", "sales_extract_date", "sales_m_minus_1_month", "sales_mtd_month",
                 "target_master_sha256"):
        if len({row[name] for row in rows}) != 1:
            raise PriceTrackingInputError(f"The selected list mixes values of {name}.")
    if [row["rank"] for row in rows] != [str(n) for n in range(1, len(rows) + 1)]:
        raise PriceTrackingInputError("Selected-list ranks are not 1..n in order.")
    ids = [row["target_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise PriceTrackingInputError("The selected list repeats a target_id.")

    targets = {
        str(row["target_id"]): row for row in target_master.valid_rows.to_dict(orient="records")
    }
    ledger_ids = [row["model_id"] for row in contributions]
    if len(ledger_ids) != len(set(ledger_ids)):
        raise PriceTrackingInputError("The contribution ledger repeats a model_id.")
    contributors: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in contributions:
        if row["contribution_status"] == CONTRIBUTING:
            contributors[row["target_id"]].append(row)

    selected = []
    for row in rows:
        target_id = row["target_id"]
        target = targets.get(target_id)
        if target is None:
            raise PriceTrackingInputError(f"Selected target {target_id} is not in TARGET_MASTER.")
        if str(target["status"]) != "ACTIVE":
            raise PriceTrackingInputError(f"Selected target {target_id} is not ACTIVE.")
        if effective_tracking_mode(target.get("tracking_mode")) != CONFIGURATION:
            raise PriceTrackingInputError(f"Selected target {target_id} is not a CONFIGURATION.")
        if core_key(target)[0] is None:
            raise PriceTrackingInputError(f"Selected target {target_id} has an incomplete core.")
        if any(row[name] != str(target[name]) for name in CORE_MATCH_COLUMNS):
            raise PriceTrackingInputError(
                f"Selected target {target_id} core identity differs from TARGET_MASTER."
            )
        listed = tuple(filter(None, row["contributing_source_model_ids"].split(";")))
        members = contributors.get(target_id, [])
        if set(listed) != {member["model_id"] for member in members} or len(listed) != len(
            members
        ):
            raise PriceTrackingInputError(
                f"Selected target {target_id} contributors differ from the contribution ledger."
            )
        if row["contributing_source_count"] != str(len(members)):
            raise PriceTrackingInputError(f"Selected target {target_id} source count is wrong.")
        # Same rule as the verified-list audit: period sums from the ledger text, and the total
        # is M-1 + MTD (the per-source stored totals can carry float artifacts).
        sums = [
            sum(
                (_decimal(m[name], f"Ledger {m['model_id']} {name}") for m in members),
                Decimal(0),
            )
            for name in ("m_minus_1_gmv_usd", "mtd_gmv_usd")
        ]
        published = [
            _decimal(row[name], f"Selected target {target_id} {name}")
            for name in ("m_minus_1_gmv_usd", "mtd_gmv_usd", "ranking_gmv_usd")
        ]
        if published != [*sums, sums[0] + sums[1]]:
            raise PriceTrackingInputError(
                f"Selected target {target_id} GMV totals differ from its contributors' sums."
            )
        selected.append(SelectedConfiguration(int(row["rank"]), target_id, row, target, listed))

    first = rows[0]
    return SelectedList(
        configurations=tuple(selected),
        sha256=list_sha256,
        sales_run_id=first["sales_run_id"],
        sales_extract_date=first["sales_extract_date"],
        sales_m_minus_1_month=first["sales_m_minus_1_month"],
        sales_mtd_month=first["sales_mtd_month"],
        built_on_target_master_sha256=first["target_master_sha256"],
        catalog_matches_build=first["target_master_sha256"] == target_master_sha256,
    )



def _snapshot_price_status(snapshot: Mapping[str, str] | None) -> str:
    """Status of a source's sales-extract price for the configuration snapshot.

    The configuration layer dates every price by the sales extract. A price that comes from the
    M-1 record (a variation with no MTD record, 2026-11-03) is not an extract-dated price and is
    never shown as one.
    """
    if not snapshot:
        return "SALES_RANKING_NOT_SUPPLIED"
    if snapshot.get("price_basis") == "M_MINUS_1_PRICE":
        return "PRICE_FROM_M_MINUS_1_RECORD"
    return snapshot["price_status"]

def build_source_ledger(
    selected: SelectedList,
    contributions: Sequence[Mapping[str, str]],
    source_model_map: TableResult,
    sales_rows: Sequence[Mapping[str, str]] | None = None,
) -> list[dict[str, str]]:
    """One row per selected configuration and contributing Shopee source.

    A configuration keeps every contributing listing; none is chosen as its identity. The Shopee
    GMV and price values are the historical snapshot named in each row, not current prices.
    """
    if not source_model_map.is_valid:
        raise PriceTrackingInputError("SOURCE_MODEL_MAP did not pass validation.")
    links = {
        str(row["source_model_id"]): str(row["target_id"])
        for row in source_model_map.valid_rows.to_dict(orient="records")
    }
    ledger = {row["model_id"]: row for row in contributions}
    sales: dict[str, Mapping[str, str]] = {}
    if sales_rows is not None:
        for row in sales_rows:
            sales[row["model_id"]] = row
    label = selected.gmv_period_label()
    rows = []
    for item in selected.configurations:
        for model_id in item.source_model_ids:
            source = ledger[model_id]
            snapshot = sales.get(model_id)
            if snapshot is not None:
                if (
                    snapshot["run_id"] != selected.sales_run_id
                    or snapshot["extract_date"] != selected.sales_extract_date
                    or any(
                        snapshot[name] != source[name]
                        for name in ("m_minus_1_gmv_usd", "mtd_gmv_usd", "ranking_gmv_usd")
                    )
                ):
                    raise PriceTrackingInputError(
                        f"The sales ranking disagrees with the ledger for source {model_id}; "
                        "they are not from the same run."
                    )
            elif sales_rows is not None:
                raise PriceTrackingInputError(
                    f"Contributing source {model_id} is missing from the sales ranking."
                )
            rows.append(
                {
                    "rank": str(item.rank),
                    "target_id": item.target_id,
                    "model_id": model_id,
                    "shop_id": source["shop_id"],
                    "item_id": source["item_id"],
                    "item_name": source["item_name"],
                    "shopee_model_name": source["shopee_model_name"],
                    "m_minus_1_gmv_usd": source["m_minus_1_gmv_usd"],
                    "mtd_gmv_usd": source["mtd_gmv_usd"],
                    "ranking_gmv_usd": source["ranking_gmv_usd"],
                    "in_original_source_top_100": source["in_original_source_top_100"],
                    "currently_mapped_to_target": str(links.get(model_id) == item.target_id),
                    "shopee_price_php_snapshot": snapshot["shopee_current_price_php"]
                    if snapshot
                    else "",
                    "shopee_price_status": _snapshot_price_status(snapshot),
                    "sales_run_id": selected.sales_run_id,
                    "sales_extract_date": selected.sales_extract_date,
                    "sales_period_label": label,
                }
            )
    return rows


# Matching context


def matching_rules_sha256(modules: Sequence[ModuleType] = RULE_MODULES) -> str:
    """Fingerprint of the matching code: each rule module's name and file bytes, in order."""
    digest = hashlib.sha256()
    for module in modules:
        path = Path(str(module.__file__))
        digest.update(module.__name__.encode() + b"\0" + path.read_bytes() + b"\0")
        if module is evidence_approvals:
            digest.update(
                b"retailer_evidence_approvals.csv\0"
                + evidence_approvals.APPROVALS_PATH.read_bytes() + b"\0"
            )
        if module is product_names and product_names.REGISTRY_PATH.is_file():
            # The reviewed product-name registry decides name normalization and kinds.
            digest.update(
                b"product_name_registry.csv\0" + product_names.REGISTRY_PATH.read_bytes() + b"\0"
            )
    return digest.hexdigest()


def matching_context(
    target_master: TargetMasterResult, *, target_master_sha256: str, source_label: str
) -> MatchingContext:
    modes = Counter(
        effective_tracking_mode(row.get("tracking_mode"))
        for row in target_master.valid_rows.to_dict(orient="records")
    )
    return MatchingContext.create(
        target_master_sha256=target_master_sha256,
        target_master_source=source_label,
        configuration_targets=modes[CONFIGURATION],
        listing_only_targets=modes[LISTING_ONLY],
        matching_rules_version=MATCHING_RULES_VERSION,
        matching_rules_sha256=matching_rules_sha256(),
    )


def match_stored_observations(
    store: ObservationStore,
    context: MatchingContext,
    matcher: ExactMatcher,
    *,
    now: datetime,
) -> int:
    """Match every stored observation that has no result under ``context``; returns the count.

    Results under earlier contexts are left untouched.
    """
    store.register_context(context, first_used_at=now)
    pending = set(store.unmatched_observation_ids(context.context_id))
    results = [
        (stored.observation_id, matcher.match_observation(stored.observation()))
        for stored in store.observations()
        if stored.observation_id in pending
    ]
    return store.record_match_results(context.context_id, results)


# Comparability and selection


@dataclass(frozen=True)
class ObservationView:
    stored: StoredObservation
    match: StoredMatch
    week_start: date
    status: str
    reasons: tuple[str, ...]
    retailer_code: str
    # Commercial terms and nonblocking price notes (INCLUDED_ITEMS_OFFER, BUNDLE_OFFER).
    terms: OfferTerms | None = None
    notes: tuple[str, ...] = ()
    # Targets this offer was searched or collected for: context only, never an assignment.
    search_targets: tuple[str, ...] = ()
    # Listed-spec association across broad-family targets (2026-10-20) and the offer's price
    # comparability under that basis (the same price rules as an exact match).
    listed: ListedSpecResult | None = None
    listed_status: str = ""
    listed_reasons: tuple[str, ...] = ()
    # EXACT_PRODUCT, or LISTED_SPEC for the copy of a view that stands for one listed-spec
    # association (``listed_target``) in a selection. The stored match is never changed.
    basis: str = EXACT_PRODUCT_BASIS
    listed_target: str = ""
    # Close-alternative evaluations of this offer against the selected targets (2026-10-27;
    # report week only): every status, kept beside the exact assignment, which they never
    # change. ``alternative_status``/``alternative_reasons`` are the offer's price
    # comparability under the same price rules. A copy standing for one association
    # (``alternative_views``) carries it in ``alt``.
    evaluations: tuple[AlternativeEvaluation, ...] = ()
    alternative_status: str = ""
    alternative_reasons: tuple[str, ...] = ()
    evidence: OfferEvidence | None = None
    alt: AlternativeEvaluation | None = None

    @property
    def target_id(self) -> str:
        return self.match.target_id

    @property
    def title(self) -> str:
        return self.stored.record.get("raw_title", "")

    @property
    def is_exact(self) -> bool:
        return self.match.match_status == MatchStatus.EXACT.value


def stored_offer_terms(stored: StoredObservation) -> OfferTerms:
    """The purchase terms of a stored observation, from its own saved wording."""
    return classify_offer_terms(stored.observation())


def comparability(
    stored: StoredObservation,
    match: StoredMatch,
    *,
    terms: OfferTerms | None = None,
    policy: str = DEFAULT_OFFER_TERMS_POLICY,
) -> tuple[str, tuple[str, ...]]:
    """Whether an observation's price can stand for its matched target, and why not."""
    if match.match_status == MatchStatus.REVIEW_REQUIRED.value:
        return REVIEW_REQUIRED, ("MATCH_REVIEW_REQUIRED",)
    if match.match_status != MatchStatus.EXACT.value or not match.target_id:
        return NOT_MATCHED, (f"MATCH_{match.match_status}",)
    if terms is None and policy != BUNDLE_FLAG_POLICY:
        terms = stored_offer_terms(stored)
    reasons = price_rule_reasons(
        retailer_id=stored.retailer_id,
        currency=stored.currency,
        availability=stored.availability,
        bundle_flag=stored.bundle_flag,
        price_type=stored.price_type,
        price=stored.price,
        terms=terms,
        policy=policy,
    )
    return (COMPARABLE, ()) if not reasons else (NOT_COMPARABLE, reasons)


def price_rule_reasons(
    *,
    retailer_id: str,
    currency: str,
    availability: str,
    bundle_flag: str,
    price_type: str,
    price: Decimal,
    terms: OfferTerms | None = None,
    policy: str = DEFAULT_OFFER_TERMS_POLICY,
) -> tuple[str, ...]:
    """Why an exactly matched offer's price is not comparable; empty when it is.

    Without ``terms`` the purchase terms fall back to the checked bundle flag (the 2026-10-15
    rule), whatever the policy.
    """
    reasons = []
    if retailer_id not in RETAILER_CODE_BY_ID:
        reasons.append("RETAILER_NOT_IN_COMPARISON")
    if currency != REPORTING_CURRENCY:
        reasons.append("CURRENCY_NOT_PHP")
    if availability not in {Availability.IN_STOCK.value, Availability.OUT_OF_STOCK.value}:
        reasons.append(f"AVAILABILITY_{availability}")
    reasons += terms_price_effect(terms, bundle_flag, policy)[0]
    if price_type not in {PriceType.REGULAR.value, PriceType.SALE.value}:
        reasons.append("PRICE_TYPE_UNKNOWN")
    if not price.is_finite() or price <= 0:
        reasons.append("PRICE_INVALID")
    return tuple(reasons)


def price_note_codes(
    bundle_flag: str, terms: OfferTerms | None = None, policy: str = BUNDLE_FLAG_POLICY
) -> tuple[str, ...]:
    """Nonblocking price notes, kept separate from identity and exclusion reasons."""
    return terms_price_effect(terms, bundle_flag, policy)[1]


def _selection_key(view: ObservationView) -> tuple[object, ...]:
    stored = view.stored
    return (
        stored.price,
        -stored.retrieved_at.timestamp(),
        stored.offer_url,
        stored.offer_index,
        stored.observation_id,
    )


def _latest(views: Sequence[ObservationView]) -> ObservationView:
    return max(views, key=lambda v: (v.stored.retrieved_at, v.stored.observation_id))


@dataclass(frozen=True)
class Selection:
    target_id: str
    retailer_code: str
    status: str
    chosen: ObservationView | None
    comparable_offers: int
    tied_offers: int
    offers_observed: int
    observations_in_week: int
    not_comparable_reasons: str
    review_required: int
    stale: ObservationView | None
    # Latest exact offer in this week, retained for rechecking even with no comparable price.
    observed: ObservationView | None = None
    # Lowest comparable offer recorded IN_STOCK (same set and tie-break as ``chosen``).
    chosen_in_stock: ObservationView | None = None
    # The most informative unconfirmed offer found for this cell, why it is not a match, and the
    # differing values. Never priced, never part of a minimum.
    candidate: ObservationView | None = None
    candidate_status: str = ""
    candidate_detail: str = ""
    # Why the recorded discovery of this week found no related offer (search-stage status).
    search_detail: str = ""
    # Relevant unconfirmed pages to recheck manually, most useful first (never priced).
    rechecks: tuple[RecheckCandidate, ...] = ()
    # Close alternatives (2026-10-27): every offer of this retailer that qualifies for the
    # target, fewest differences first and then by price (any price eligibility, for the
    # detailed output); the offer a hidden minimum or main display would take is chosen from
    # the comparable ones. ``chosen`` and ``status`` stay matching-only.
    alternatives: tuple[ObservationView, ...] = ()
    alternative_best: ObservationView | None = None
    alternative_lowest: ObservationView | None = None
    alternative_lowest_in_stock: ObservationView | None = None

    @property
    def display(self) -> ObservationView | None:
        """The main displayed offer: a matching configuration, otherwise the best alternative
        (ALT 1 before ALT 2, then the lowest price)."""
        return self.chosen if self.chosen is not None else self.alternative_best

    @property
    def display_status(self) -> str:
        if self.chosen is None and self.alternative_best is not None:
            return ALTERNATIVE_SELECTED
        return self.status

    @property
    def alternative_pool(self) -> tuple[ObservationView, ...]:
        """The comparable alternatives: the full pool the alternative minimums are taken from."""
        return tuple(v for v in self.alternatives if v.status == COMPARABLE)


# Search-stage statuses a blank cell can take from the recorded discovery of the report week
# (2026-10-19), shared with the coverage report (pilot_coverage.DiscoveryLogs.stage).
SEARCH_STAGE_STATUSES: tuple[str, ...] = (
    "FETCH_FAILED", "CANDIDATE_NOT_FETCHED", "CATALOG_IDENTITY_UNRESOLVED",
    "NO_RELEVANT_CANDIDATE", "NO_CANDIDATE_FOUND", "DISCOVERY_INCOMPLETE", "SEARCH_FAILED",
    "DISCOVERY_NOT_ATTEMPTED",
)


def select_week(
    week: ReportWeek,
    views: Sequence[ObservationView],
    selected: SelectedList,
    search_outcomes: Mapping[tuple[str, str], tuple[str, str]] | None = None,
    discovery_rechecks: Mapping[tuple[str, str], Sequence[RecheckCandidate]] | None = None,
) -> dict[tuple[str, str], Selection]:
    """One selection per selected target and retailer for ``week`` (views already cut at as_of).

    ``discovery_rechecks`` (``(target_id, retailer_code)``) are this week's relevant unfetched
    or failed pages; with the related unconfirmed offers they form a blank cell's recheck list.

    ``search_outcomes`` maps ``(target_id, retailer_code)`` to the recorded discovery outcome of
    this week (status, explanation); it explains a cell only when no offer relates to it.
    """
    in_week = [view for view in views if view.week_start == week.start]
    by_offer: dict[str, list[ObservationView]] = defaultdict(list)
    for view in in_week:
        by_offer[view.stored.offer_key].append(view)
    latest = [_latest(group) for group in by_offer.values()]

    exact_latest: dict[tuple[str, str], list[ObservationView]] = defaultdict(list)
    listed_latest: dict[tuple[str, str], list[ObservationView]] = defaultdict(list)
    for view in latest:
        if view.is_exact and view.retailer_code:
            exact_latest[(view.target_id, view.retailer_code)].append(view)
        if view.retailer_code:
            for copy in listed_views(view):
                listed_latest[(copy.listed_target, view.retailer_code)].append(copy)
    alternative_latest: dict[tuple[str, str], list[ObservationView]] = defaultdict(list)
    for view in latest:
        for copy in alternative_views(view):
            assert copy.alt is not None
            alternative_latest[(copy.alt.target_id, view.retailer_code)].append(copy)
    exact_all: Counter[tuple[str, str]] = Counter(
        (view.target_id, view.retailer_code) for view in in_week if view.is_exact
    )
    review: Counter[tuple[str, str]] = Counter(
        (candidate, view.retailer_code)
        for view in in_week
        if view.status == REVIEW_REQUIRED
        for candidate in view.match.candidate_target_ids
    )
    # Search context (collection lineage) and near-match hints link unconfirmed offers to a cell;
    # they never assign. A near-match hint for another model is not related (2026-10-19).
    related: dict[tuple[str, str], list[tuple[ObservationView, bool]]] = defaultdict(list)
    for view in latest:
        ambiguous = view.listed.ambiguous_ids if view.listed is not None else ()
        associated = view.listed.target_ids if view.listed is not None and (
            view.listed.status == LISTED_SPEC_MATCH) else ()
        for target_id in dict.fromkeys(
            (*view.search_targets, *view.match.candidate_target_ids, *ambiguous)
        ):
            if not (view.is_exact and view.target_id == target_id) and view.retailer_code and (
                target_id not in associated
            ):
                related[(target_id, view.retailer_code)].append(
                    (view, target_id in view.search_targets)
                )
    earlier: dict[tuple[str, str], ObservationView] = {}
    for view in views:
        if view.week_start >= week.start:
            continue
        for stand_in, target_id in (
            [(view, view.target_id)] if view.status == COMPARABLE and view.is_exact else []
        ) + [(copy, copy.listed_target) for copy in listed_views(view)
             if copy.status == COMPARABLE]:
            key = (target_id, view.retailer_code)
            if key not in earlier or _latest([earlier[key], stand_in]) is stand_in:
                earlier[key] = stand_in

    selections = {}
    for item in selected.configurations:
        for code, _ in RETAILERS:
            key = (item.target_id, code)
            # An exact product match is preferred; a listed-spec match prices the cell only
            # when no exact offer is comparable (2026-10-20). One retailer, one price.
            exact_offers = exact_latest.get(key, [])
            offers = [*exact_offers, *listed_latest.get(key, [])]
            comparable = sorted((v for v in exact_offers if v.status == COMPARABLE),
                                key=_selection_key) or sorted(
                (v for v in offers if v.status == COMPARABLE), key=_selection_key)
            reasons = Counter(reason for v in offers for reason in v.reasons)
            candidate, candidate_status, candidate_detail = _best_candidate(
                item, related.get(key, []), selected
            )
            search_status, search_detail = (search_outcomes or {}).get(key, ("", ""))
            alternatives = sorted(alternative_latest.get(key, []), key=_alternative_order)
            alternative_pool = [v for v in alternatives if v.status == COMPARABLE]
            rechecks: list[RecheckCandidate] = []
            if not comparable and not alternative_pool:
                rechecks = _rechecks(item, related.get(key, []), selected, offers,
                                     (discovery_rechecks or {}).get(key, ()), candidate)
            if comparable:
                status, chosen = SELECTED, comparable[0]
                tied = sum(1 for v in comparable if v.stored.price == chosen.stored.price)
            else:
                chosen, tied = None, 0
                if offers:
                    status = NO_COMPARABLE_OFFER
                elif review[key]:
                    status = REVIEW_REQUIRED_ONLY
                elif candidate is not None:
                    status = CANDIDATE_NOT_CONFIRMED
                elif search_status:
                    # No related offer: the recorded discovery outcome of this week says why.
                    status = search_status
                else:
                    status = NO_OBSERVATION_IN_WEEK
            selections[key] = Selection(
                target_id=item.target_id,
                retailer_code=code,
                status=status,
                chosen=chosen,
                comparable_offers=len(comparable),
                tied_offers=tied,
                offers_observed=len(offers),
                observations_in_week=exact_all[key] + len(listed_latest.get(key, [])),
                not_comparable_reasons=";".join(
                    f"{reason}:{count}" for reason, count in sorted(reasons.items())
                ),
                review_required=review[key],
                stale=None if comparable else earlier.get(key),
                observed=_latest(offers) if offers else None,
                chosen_in_stock=next(
                    (v for v in comparable if v.stored.availability == Availability.IN_STOCK.value),
                    None,
                ),
                candidate=candidate,
                candidate_status=candidate_status,
                candidate_detail=candidate_detail,
                search_detail=search_detail if status == search_status else "",
                rechecks=tuple(rechecks),
                alternatives=tuple(alternatives),
                # Fewest differences first, then the lowest price (existing tie-breaking).
                alternative_best=alternative_pool[0] if alternative_pool else None,
                # The minimums take the lowest eligible price of the whole pool, whatever the
                # level and whatever the main display shows.
                alternative_lowest=min(alternative_pool, key=_selection_key, default=None),
                alternative_lowest_in_stock=min(
                    (v for v in alternative_pool
                     if v.stored.availability == Availability.IN_STOCK.value),
                    key=_selection_key, default=None),
            )
    return selections


def _alternative_order(view: ObservationView) -> tuple[object, ...]:
    """ALT 1 before ALT 2, then the existing price tie-breaking."""
    assert view.alt is not None
    return (view.alt.difference_count, *_selection_key(view))


def _rechecks(
    item: SelectedConfiguration,
    related: Sequence[tuple[ObservationView, bool]],
    selected: SelectedList,
    offers: Sequence[ObservationView],
    discovered: Sequence[RecheckCandidate],
    best: ObservationView | None,
) -> list[RecheckCandidate]:
    """The cell's recheck list: related unconfirmed offers (same filter as the candidate) and
    this week's relevant unfetched or failed pages; the cell's own exact offers are excluded."""
    fetched = []
    for view, collected in related:
        outcome, _ = candidate_outcome(view, item.target, selected, item.target_id)
        if not collected and outcome == DIFFERENT_MODEL:
            continue
        differences = core_differences(view.match.offer_attributes, item.target)
        priority = 10 + (_CANDIDATE_ORDER.index(outcome) if outcome in _CANDIDATE_ORDER else 19)
        if view is best:
            priority = 0  # The cell's named candidate is listed first.
        stored = view.stored
        fetched.append(RecheckCandidate(
            priority, FETCHED_NOT_CONFIRMED, outcome,
            offer_explanation(outcome, view.match.reason_codes, differences,
                              view.target_id if view.is_exact else ""),
            stored.offer_url, stored.record.get("raw_title", ""), stored.availability,
            local_text(stored.retrieved_at), stored.price_text, stored.observation_id,
        ))
    own = {url_key(view.stored.offer_url) for view in offers}
    return [c for c in merge_rechecks(fetched, discovered) if url_key(c.url) not in own]


# Candidate outcomes for a blank cell, most informative first (the coverage report's order).
_CANDIDATE_ORDER = (
    "AMBIGUOUS_TARGET", "IDENTITY_CONFLICT", "CATALOG_IDENTITY_UNRESOLVED", "MODEL_UNVERIFIED",
    "EXTRACTION_INCOMPLETE",
    SAME_MODEL_DIFFERENT_CONFIGURATION, "DIFFERENT_CONFIGURATION", DIFFERENT_MODEL,
    "INACTIVE_TARGET",
)


def candidate_outcome(
    view: ObservationView,
    target_row: Mapping[str, object],
    selected: SelectedList,
    target_id: str = "",
) -> tuple[str, str]:
    """Why an offer related to a configuration is not its confirmed match, with the differing
    values where the offer's verified fields show them. The rule is shared with the coverage
    report (``matching.configuration_outcome``): "same model, different configuration" only for
    RAM/storage options of one model code; another model is ``DIFFERENT_MODEL``."""
    match = view.match
    target_id = target_id or str(target_row.get("target_id", "") or "")
    if view.listed is not None and target_id in view.listed.ambiguous_ids:
        return "AMBIGUOUS_TARGET", view.listed.detail()
    outcome, detail = configuration_outcome(
        match.match_status, match.target_id, match.reason_codes, match.offer_attributes,
        target_id, target_row, title=view.title,
    )
    if view.is_exact and match.target_id:
        scope = "in the selected list" if match.target_id in selected.by_target else (
            "outside the selected list"
        )
        detail = detail.replace(
            f"confirmed as {match.target_id}", f"confirmed as {match.target_id} ({scope})", 1
        )
    return outcome, detail


def _best_candidate(
    item: SelectedConfiguration,
    views: Sequence[tuple[ObservationView, bool]],
    selected: SelectedList,
) -> tuple[ObservationView | None, str, str]:
    """The most informative related offer. ``views`` pairs each offer with whether collection
    lineage links it to the cell; a near-match hint alone never relates another model."""
    outcomes = []
    for view, collected in views:
        outcome = candidate_outcome(view, item.target, selected, item.target_id)
        if collected or outcome[0] != DIFFERENT_MODEL:
            outcomes.append((outcome, view))
    if not outcomes:
        return None, "", ""
    ranked = sorted(
        outcomes,
        key=lambda pair: (
            _CANDIDATE_ORDER.index(pair[0][0]) if pair[0][0] in _CANDIDATE_ORDER else 99,
            -pair[1].stored.retrieved_at.timestamp(),
            pair[1].stored.offer_url,
        ),
    )
    (status, detail), view = ranked[0]
    return view, status, detail


def _selection_reason(selection: Selection) -> str:
    if selection.chosen is None:
        return ""
    if selection.comparable_offers == 1:
        return "ONLY_COMPARABLE_OFFER"
    reason = f"LOWEST_OF_{selection.comparable_offers}_COMPARABLE_OFFERS"
    if selection.tied_offers > 1:
        reason += f";TIE_OF_{selection.tied_offers}_BROKEN_BY_{TIE_BREAK}"
    return reason


# Result assembly


@dataclass
class PriceTrackingResult:
    comparison: list[dict[str, str]]
    selections: list[dict[str, str]]
    history: list[dict[str, str]]
    series: list[dict[str, str]]
    review: list[dict[str, str]]
    counts: dict[str, int] = field(default_factory=dict)
    terms_effect: dict[str, object] = field(default_factory=dict)
    rechecks: list[dict[str, str]] = field(default_factory=list)
    # Close alternatives (2026-10-27): qualifying associations and every evaluation (audit).
    alternatives: list[dict[str, str]] = field(default_factory=list)
    evaluations: list[dict[str, str]] = field(default_factory=list)


def build_views(
    observations: Sequence[StoredObservation],
    matches: Mapping[str, StoredMatch],
    *,
    as_of: datetime,
    last_week: ReportWeek,
    offer_terms_policy: str = DEFAULT_OFFER_TERMS_POLICY,
    search_context: Mapping[str, Sequence[str]] | None = None,
    listed_spec: ListedSpecMatcher | None = None,
    alternatives: AlternativeEvaluator | None = None,
) -> tuple[list[ObservationView], int]:
    """Classify observations retrieved no later than ``as_of`` and before ``last_week`` ends.

    Returns the views and how many stored observations were left out by that cut-off. With
    ``listed_spec``, each view also carries its listed-spec association (2026-10-20). With
    ``alternatives``, each observation of the report week also carries its close-alternative
    evaluations (2026-10-27); earlier weeks are never evaluated and keep their timestamps.
    """
    views = []
    excluded = 0
    for stored in observations:
        if stored.retrieved_at > as_of or stored.retrieved_at >= last_week.end_utc:
            excluded += 1
            continue
        match = matches.get(stored.observation_id)
        if match is None:
            raise PriceTrackingInputError(
                "A stored observation has no result under the current matching context."
            )
        terms = stored_offer_terms(stored)
        status, reasons = comparability(stored, match, terms=terms, policy=offer_terms_policy)
        listed = None
        listed_status, listed_reasons = "", ()
        if listed_spec is not None:
            listed = listed_spec.associate(dict(match.offer_attributes or {}),
                                           stored.record.get("raw_title", ""), match.reason_codes)
            if listed.status:
                listed_reasons = price_rule_reasons(
                    retailer_id=stored.retailer_id, currency=stored.currency,
                    availability=stored.availability, bundle_flag=stored.bundle_flag,
                    price_type=stored.price_type, price=stored.price, terms=terms,
                    policy=offer_terms_policy,
                )
                listed_status = NOT_COMPARABLE if listed_reasons else COMPARABLE
        evaluations: tuple[AlternativeEvaluation, ...] = ()
        evidence = None
        alt_status, alt_reasons = "", ()
        if alternatives is not None and week_start_of(stored.retrieved_at) == last_week.start:
            evidence, evaluations = _alternative_evaluations(
                alternatives, stored, match, listed, tuple(dict.fromkeys(
                    (search_context or {}).get(stored.observation_id, ()))))
            if any(e.is_alternative for e in evaluations):
                alt_reasons = price_rule_reasons(
                    retailer_id=stored.retailer_id, currency=stored.currency,
                    availability=stored.availability, bundle_flag=stored.bundle_flag,
                    price_type=stored.price_type, price=stored.price, terms=terms,
                    policy=offer_terms_policy,
                )
                alt_status = NOT_COMPARABLE if alt_reasons else COMPARABLE
        views.append(
            ObservationView(
                stored,
                match,
                week_start_of(stored.retrieved_at),
                status,
                reasons,
                RETAILER_CODE_BY_ID.get(stored.retailer_id, ""),
                terms=terms,
                notes=price_note_codes(stored.bundle_flag, terms, offer_terms_policy),
                search_targets=tuple(dict.fromkeys(
                    (search_context or {}).get(stored.observation_id, ())
                )),
                listed=listed,
                listed_status=listed_status,
                listed_reasons=tuple(listed_reasons),
                evaluations=evaluations,
                alternative_status=alt_status,
                alternative_reasons=tuple(alt_reasons),
                evidence=evidence,
            )
        )
    return views, excluded


def _alternative_evaluations(
    evaluator: AlternativeEvaluator,
    stored: StoredObservation,
    match: StoredMatch,
    listed: ListedSpecResult | None,
    lineage: Sequence[str],
) -> tuple[OfferEvidence, tuple[AlternativeEvaluation, ...]]:
    """The offer's close-alternative evaluations against the selected targets.

    The values are the stored match's own extracted attributes (what the matcher compared);
    the evidence references and the ambiguous fields come from the same extraction of the saved
    observation. Pairs the offer already matches (its exact target, a listed-spec target) are not
    alternatives. Targets of another brand appear only when lineage names them, so that their
    exclusion is visible.
    """
    observation = stored.observation()
    extracted = extract_offer_attributes(observation)
    base = offer_evidence(extracted, stored.record.get("raw_title", ""),
                          observation_id=stored.observation_id,
                          content_sha256=stored.content_sha256)
    evidence = replace(base, values=dict(match.offer_attributes or base.values))
    skip = set()
    if match.match_status == MatchStatus.EXACT.value and match.target_id:
        skip.add(match.target_id)
    if listed is not None and listed.status == LISTED_SPEC_MATCH:
        skip.update(listed.target_ids)
    always = [*lineage, *match.candidate_target_ids]
    found = evaluator.evaluate_offer(evidence, always=always, skip=skip)
    return evidence, tuple(found)


def alternative_views(view: ObservationView) -> list[ObservationView]:
    """One view per close-alternative association, standing for that comparison only.

    Like ``listed_views``: the copy keeps the stored observation and its stored match (an exact
    assignment to another target is never rewritten); its status and reasons are the price
    comparability of the offer under the same price rules.
    """
    return [
        replace(view, status=view.alternative_status, reasons=view.alternative_reasons,
                basis=ALTERNATIVE_BASIS, alt=evaluation)
        for evaluation in view.evaluations
        if evaluation.is_alternative and view.retailer_code
    ]


def listed_views(view: ObservationView) -> list[ObservationView]:
    """One view per unambiguous listed-spec association, standing for that comparison only.

    The copy keeps the stored observation and its stored match (an exact assignment to another
    target is not rewritten); its status and reasons are the listed-spec price comparability.
    """
    if view.listed is None or view.listed.status != LISTED_SPEC_MATCH:
        return []
    return [
        replace(view, status=view.listed_status, reasons=view.listed_reasons,
                basis=LISTED_SPEC_BASIS, listed_target=target_id)
        for target_id in view.listed.target_ids
        if not (view.is_exact and view.target_id == target_id)
    ]


def offer_terms_effect(
    views: Sequence[ObservationView], week: ReportWeek, policy: str
) -> dict[str, object]:
    """Price eligibility of this week's exact matches under each offer-terms policy.

    Identity is held fixed (the same stored matches), so the difference is the effect of the
    commercial-terms policy alone.
    """
    exact = [view for view in views if view.week_start == week.start and view.is_exact]
    by_policy: dict[str, dict[str, str]] = {}
    for name in OFFER_TERMS_POLICIES:
        by_policy[name] = {
            view.stored.observation_id: comparability(
                view.stored, view.match, terms=view.terms, policy=name
            )[0]
            for view in exact
        }
    changes = [
        {
            "observation_id": view.stored.observation_id,
            "offer_url": view.stored.offer_url,
            "target_id": view.target_id,
            "offer_terms_category": view.terms.category if view.terms else "",
            "offer_terms_evidence": list(view.terms.evidence) if view.terms else [],
            **{name: by_policy[name][view.stored.observation_id] for name in by_policy},
        }
        for view in exact
        if len({by_policy[name][view.stored.observation_id] for name in by_policy}) > 1
    ]
    return {
        "active_policy": policy,
        "week_exact_observations": len(exact),
        "comparable_by_policy": {
            name: sum(1 for status in result.values() if status == COMPARABLE)
            for name, result in by_policy.items()
        },
        "terms_categories": dict(sorted(Counter(
            view.terms.category for view in exact if view.terms
        ).items())),
        "observations_whose_eligibility_depends_on_the_policy": changes,
    }


def build_price_tracking(
    observations: Sequence[StoredObservation],
    matches: Mapping[str, StoredMatch],
    selected: SelectedList,
    context: MatchingContext,
    *,
    week: ReportWeek,
    as_of: datetime,
    history_start: date | None = None,
    offer_terms_policy: str = DEFAULT_OFFER_TERMS_POLICY,
    search_context: Mapping[str, Sequence[str]] | None = None,
    search_outcomes: Mapping[tuple[str, str], tuple[str, str]] | None = None,
    discovery_rechecks: Mapping[tuple[str, str], Sequence[RecheckCandidate]] | None = None,
    listed_spec: ListedSpecMatcher | None = None,
    shopee: Mapping[str, ShopeeSelection] | None = None,
    alternatives: AlternativeEvaluator | None = None,
) -> PriceTrackingResult:
    """``search_outcomes`` (``(target_id, retailer_code) -> (status, explanation)``) come from the
    report week's recorded discovery and explain blank cells of that week only; earlier weeks in
    the series never take them.

    ``alternatives`` (2026-10-27) adds close-alternative comparisons to the report week: they
    never change a stored match, a matching price, a matching minimum or a count; without it the
    report is the matching-only report and the alternative columns are blank."""
    if as_of < week.start_utc:
        raise PriceTrackingInputError("--as-of is before the report week starts.")
    if offer_terms_policy not in OFFER_TERMS_POLICIES:
        raise PriceTrackingInputError(f"Unknown offer terms policy: {offer_terms_policy}")
    views, excluded = build_views(
        observations, matches, as_of=as_of, last_week=week,
        offer_terms_policy=offer_terms_policy, search_context=search_context,
        listed_spec=listed_spec, alternatives=alternatives,
    )
    first_week = ReportWeek(
        history_start
        if history_start is not None
        else min([view.week_start for view in views] + [week.start])
    )
    if first_week.start > week.start:
        raise PriceTrackingInputError("--history-start is after the report week.")
    excluded += sum(1 for view in views if view.week_start < first_week.start)
    views = [view for view in views if view.week_start >= first_week.start]
    weeks = []
    cursor = first_week
    while cursor.start <= week.start:
        weeks.append(cursor)
        cursor = ReportWeek(cursor.end)

    ranks = {item.target_id: item.rank for item in selected.configurations}
    by_week = {
        w.start: select_week(w, views, selected, search_outcomes if w == week else None,
                             discovery_rechecks if w == week else None)
        for w in weeks
    }
    current = by_week[week.start]
    week_fields = {
        "report_week_start": week.start.isoformat(),
        "report_week_end": week.end.isoformat(),
        "week_status": week.status(as_of),
    }

    comparison = [
        _in_column_order(
            _comparison_row(item, current, selected, context, week_fields, as_of)
            | _shopee_cells(selected, (shopee or {}).get(item.target_id), shopee is not None))
        for item in selected.configurations
    ]
    selections = [
        _selection_row(item, current[(item.target_id, code)], week_fields, context)
        for item in selected.configurations
        for code, _ in RETAILERS
    ]
    history = _history_rows(views, ranks, context)
    series = _series_rows(weeks, by_week, selected, as_of)
    review = [
        _review_row(view, ranks, context, week)
        for view in sorted(views, key=lambda v: (v.stored.retrieved_at, v.stored.observation_id))
        if view.week_start == week.start and _review_reason(view, ranks)
    ]

    week_views = [view for view in views if view.week_start == week.start]
    priced = [row for row in comparison if row["Comparable_Retailer_Count"] != "0"]
    exact_selected = {
        view.target_id for view in week_views if view.is_exact and view.target_id in ranks
    }
    counts: dict[str, int] = {
        "selected_configurations": len(selected.configurations),
        "observations_in_history": len(views),
        "observations_outside_history_window": excluded,
        "observations_in_week": len(week_views),
        "offers_in_week": len({view.stored.offer_key for view in week_views}),
        "selected_targets_with_exact_observation_in_week": len(exact_selected),
        "selected_targets_with_comparable_price": len(priced),
        "selected_targets_without_comparable_price": len(comparison) - len(priced),
        "selected_retailer_prices": sum(1 for row in selections if row["price_status"] == SELECTED),
        "history_weeks": len(weeks),
        "review_rows": len(review),
    }
    for status in (MatchStatus.EXACT, MatchStatus.UNMATCHED, MatchStatus.AMBIGUOUS,
                   MatchStatus.REVIEW_REQUIRED):
        counts[f"week_match_{status.value}"] = sum(
            1 for view in week_views if view.match.match_status == status.value
        )
    counts["week_exact_out_of_selected_list"] = sum(
        1 for view in week_views if view.is_exact and view.target_id not in ranks
    )
    for status in (COMPARABLE, NOT_COMPARABLE, REVIEW_REQUIRED, NOT_MATCHED):
        counts[f"week_{status}"] = sum(1 for view in week_views if view.status == status)
    for code, _ in RETAILERS:
        counts[f"prices_{code}"] = sum(
            1
            for row in selections
            if row["retailer_code"] == code and row["price_status"] == SELECTED
        )
    for status in (SELECTED, ALTERNATIVE_SELECTED, NO_COMPARABLE_OFFER, REVIEW_REQUIRED_ONLY,
                   CANDIDATE_NOT_CONFIRMED, *SEARCH_STAGE_STATUSES, NO_OBSERVATION_IN_WEEK):
        counts[f"cells_{status}"] = sum(1 for row in selections if row["price_status"] == status)
    # Offers priced for one configuration that state different display or GPU evidence are
    # flagged, never resolved: the catalog may not record the field (2026-10-19, independent
    # review: PCX "Full-HD" vs DataBlitz/Laptop Factory WUXGA for AL14-45P-R10U).
    disagreeing = 0
    for item, row in zip(selected.configurations, comparison, strict=True):
        chosen = {code: current[(item.target_id, code)].chosen for code, _ in RETAILERS
                  if current[(item.target_id, code)].chosen is not None}
        found = offer_disagreements(chosen)
        disagreeing += bool(found)
        for name, detail in found.items():
            code_text = f"OFFERS_DISAGREE_{name.upper()}"
            for code, view in chosen.items():
                row[f"{code}_Warnings"] = ";".join(filter(None, (row[f"{code}_Warnings"],
                                                                 code_text)))
                review.append(_review_row(view, ranks, context, week)
                              | {"review_reason": f"{code_text} ({detail})"})
    counts["review_rows"] = len(review)
    counts["configurations_with_offer_disagreement"] = disagreeing
    chosen_views = [s.chosen for s in current.values() if s.chosen is not None]
    counts["selected_retailer_prices_exact_product"] = sum(
        1 for view in chosen_views if view.basis == EXACT_PRODUCT_BASIS)
    counts["selected_retailer_prices_listed_spec"] = sum(
        1 for view in chosen_views if view.basis == LISTED_SPEC_BASIS)
    for basis in (EXACT_PRODUCT_BASIS, LISTED_SPEC_BASIS, "MIXED"):
        counts[f"configurations_priced_{basis.lower()}"] = sum(
            1 for row in comparison if row["Comparison_Basis"] == basis)
    counts["week_listed_spec_associations"] = sum(
        1 for view in week_views if view.listed is not None
        and view.listed.status == LISTED_SPEC_MATCH)
    counts["week_listed_spec_ambiguous"] = sum(
        1 for view in week_views if view.listed is not None
        and view.listed.status == LISTED_SPEC_AMBIGUOUS)
    rechecks = recheck_rows(current, selected, week_fields)
    counts["recheck_candidates"] = len(rechecks)
    counts["blank_cells_with_recheck_link"] = sum(
        1 for selection in current.values() if selection.display is None and selection.rechecks)
    alternative_output = alternative_rows(current, selected, week_fields, context)
    evaluation_output = evaluation_rows(views, selected, week)
    counts.update(_alternative_counts(current, comparison, selections, week_views, ranks,
                                      alternative_output, evaluation_output))
    return PriceTrackingResult(
        comparison, selections, history, series, review, counts,
        offer_terms_effect(views, week, offer_terms_policy), rechecks,
        alternative_output, evaluation_output,
    )


def _in_column_order(row: Mapping[str, str]) -> dict[str, str]:
    return {column: row[column] for column in COMPARISON_COLUMNS}


def _alternative_counts(
    current: Mapping[tuple[str, str], Selection],
    comparison: Sequence[Mapping[str, str]],
    selections: Sequence[Mapping[str, str]],
    week_views: Sequence[ObservationView],
    ranks: Mapping[str, int],
    alternative_output: Sequence[Mapping[str, str]],
    evaluation_output: Sequence[Mapping[str, str]],
) -> dict[str, int]:
    """Counts of the close-alternative layer (2026-10-27). Each retailer is counted once per
    coverage category; ``selected_retailer_prices`` and the matching counts are unchanged."""
    counts = {
        "selected_retailer_alternative_prices": sum(
            1 for row in selections if row["price_status"] == ALTERNATIVE_SELECTED),
        "configurations_with_alternative_price": sum(
            1 for row in comparison if row["Alternative_Retailer_Count"] not in {"", "0"}),
        "configurations_with_alternative_displayed": sum(
            1 for row in comparison if any(
                row[f"{code}_Price_Status"] == ALTERNATIVE_SELECTED for code, _ in RETAILERS)),
        "week_alternative_associations": len(alternative_output),
        "week_alternative_associations_eligible": sum(
            1 for row in alternative_output if row["price_eligibility"] == COMPARABLE),
    }
    for level in (1, 2):
        counts[f"week_alt_{level}_associations"] = sum(
            1 for row in alternative_output if row["difference_count"] == str(level))
        counts[f"cells_displaying_alt_{level}"] = sum(
            1 for selection in current.values() if selection.chosen is None
            and selection.alternative_best is not None
            and selection.alternative_best.alt.difference_count == level)
    for code, _ in RETAILERS:
        counts[f"alternative_prices_{code}"] = sum(
            1 for row in selections
            if row["retailer_code"] == code and row["price_status"] == ALTERNATIVE_SELECTED)
    for status in (ALTERNATIVE, RECHECK, NO_DIFFERENCE, "EXCLUDED_TOO_MANY_DIFFERENCES",
                   "EXCLUDED_BRAND_OR_FAMILY"):
        counts[f"week_alternative_evaluations_{status.lower()}"] = sum(
            1 for row in evaluation_output if row["status"] == status)
    return counts


def _comparison_row(
    item: SelectedConfiguration,
    selections: Mapping[tuple[str, str], Selection],
    selected: SelectedList,
    context: MatchingContext,
    week_fields: Mapping[str, str],
    as_of: datetime,
) -> dict[str, str]:
    target = item.target
    row: dict[str, str] = {
        **week_fields,
        "reporting_timezone": REPORTING_TIMEZONE,
        "as_of": utc_text(as_of),
        "rank": str(item.rank),
        "target_id": item.target_id,
    }
    for name, column in PRODUCT_FIELDS:
        value = target.get(column)
        row[name] = "" if value is None else str(value)
    row |= {
        "Shopee_Source_Count": str(len(item.source_model_ids)),
        "Shopee_Source_Model_IDs": ";".join(item.source_model_ids),
        "Shopee_Total_GMV_USD": item.list_row["ranking_gmv_usd"],
        "Shopee_GMV_Period": selected.gmv_period_label(),
        "Shopee_Sales_Run_ID": selected.sales_run_id,
        "Currency": REPORTING_CURRENCY,
    }
    observed: dict[str, ObservationView] = {}
    in_stock: dict[str, ObservationView] = {}
    alternative_lowest: dict[str, ObservationView] = {}
    alternative_in_stock: dict[str, ObservationView] = {}
    displayed = 0
    for code, _ in RETAILERS:
        selection = selections[(item.target_id, code)]
        chosen = selection.chosen
        # The main display: a matching configuration, else the best close alternative.
        shown = selection.display
        row[f"{code}_Price"] = shown.stored.price_text if shown else ""
        linked_offer = shown or selection.observed
        row[f"{code}_Offer_URL"] = linked_offer.stored.offer_url if linked_offer else ""
        row[f"{code}_Price_Status"] = selection.display_status
        row |= {
            f"{code}_Stock": linked_offer.stored.availability if linked_offer else "",
            f"{code}_Observed_At": local_text(linked_offer.stored.retrieved_at)
            if linked_offer else "",
            f"{code}_Match_Basis": _match_basis(linked_offer),
            f"{code}_Warnings": ";".join(filter(None, (
                _warnings(linked_offer),
                ";".join(linked_offer.notes) if linked_offer else "",
            ))),
            f"{code}_Offer_Details": _offer_details(linked_offer),
            f"{code}_Reason": cell_reason(selection),
            f"{code}_Candidate_URL": _candidate_url(selection) if not shown else "",
            f"{code}_Candidate_Status": _candidate_status(selection) if not shown else "",
            f"{code}_Candidate_Detail": _candidate_detail(selection) if not shown else "",
            f"{code}_Recheck_Count": str(len(selection.rechecks)) if not shown else "",
            f"{code}_Recheck_Note": recheck_note(selection.rechecks[0])
            if selection.rechecks and not shown else "",
            **_alternative_cells(code, selection),
        }
        displayed += shown is not None
        if chosen:
            observed[code] = chosen
        if selection.chosen_in_stock:
            in_stock[code] = selection.chosen_in_stock
        if selection.alternative_lowest:
            alternative_lowest[code] = selection.alternative_lowest
        if selection.alternative_lowest_in_stock:
            alternative_in_stock[code] = selection.alternative_lowest_in_stock
    observed_basis = _basis_of(observed.values())
    in_stock_basis = _basis_of(in_stock.values())
    row |= {
        "Alternative_Retailer_Count": str(len(alternative_lowest)),
        "Comparison_Retailer_Count": str(displayed),
        **_alternative_minimum("Lowest_Alternative_Price", alternative_lowest, stock=True),
        **_alternative_minimum("Lowest_Alternative_In_Stock_Price", alternative_in_stock,
                               stock=False),
        "Comparable_Retailer_Count": str(len(observed)),
        **_minimum("Lowest_Price", "Lowest_Price", observed, stock=True),
        **_minimum("Lowest_In_Stock_Price", "Lowest_In_Stock", in_stock, stock=False),
        "Shopee_Sales_Extract_Date": selected.sales_extract_date,
        "matching_context_id": context.context_id,
        "selected_list_sha256": selected.sha256,
        "Comparison_Basis": observed_basis,
        "Lowest_Price_Label": minimum_label(observed_basis, in_stock=False),
        "Lowest_In_Stock_Label": minimum_label(in_stock_basis, in_stock=True),
    }
    return {column: row[column] for column in COMPARISON_COLUMNS
            if column not in SHOPEE_COMPARISON_COLUMNS}


def _shopee_cells(
    selected: SelectedList, selection: ShopeeSelection | None, supplied: bool
) -> dict[str, str]:
    """The Shopee snapshot columns of one configuration: the selected listing's price, link,
    variation and IDs from one source row, dated by the sales extract."""
    source = selection.source if selection and selection.source else {}
    status = selection.status if selection else (
        "NO_CONTRIBUTING_SOURCE" if supplied else "SHOPEE_SNAPSHOT_NOT_SUPPLIED")
    return {
        "Shopee_Price": price_text(selection.amount)
        if selection and selection.amount is not None else "",
        "Shopee_Listing_URL": selection.url if selection else "",
        "Shopee_Variation": source.get("shopee_model_name", ""),
        "Shopee_Item_Name": source.get("item_name", ""),
        "Shopee_Model_ID": source.get("model_id", ""),
        "Shopee_Shop_ID": source.get("shop_id", ""),
        "Shopee_Item_ID": source.get("item_id", ""),
        "Shopee_Price_Status": status,
        "Shopee_Price_Detail": selection.detail() if selection else "",
        "Shopee_Snapshot_Date": selected.sales_extract_date,
        "Shopee_Snapshot_Label": f"sales run {selected.sales_run_id}; "
        f"{selected.gmv_period_label()}",
    }


def _minimum(
    price_column: str, prefix: str, views: Mapping[str, ObservationView], *, stock: bool
) -> dict[str, str]:
    """The lowest price among per-retailer choices, its retailer(s), link(s), time(s) and stock.

    Ties keep every tied retailer, in retailer column order. Blank when there is no price.
    """
    lowest = min((view.stored.price for view in views.values()), default=None)
    tied = [(code, view) for code, view in views.items() if view.stored.price == lowest]
    basis_column = "Lowest_Price_Basis" if stock else "Lowest_In_Stock_Basis"
    values = {
        # The matching basis of the winning offer(s): MIXED when tied offers differ.
        basis_column: _basis_of(view for _, view in tied),
        price_column: money(lowest) if lowest is not None else "",
        f"{price_column}_Retailers": ";".join(code for code, _ in tied),
        f"{prefix}_Offer_URL": ";".join(view.stored.offer_url for _, view in tied),
        f"{prefix}_Observed_At": ";".join(
            local_text(view.stored.retrieved_at) for _, view in tied
        ),
        f"{prefix}_Notes": ";".join(
            f"{code}:{note}" for code, view in tied for note in view.notes
        ),
    }
    if stock:
        values[f"{prefix}_Stock"] = ";".join(view.stored.availability for _, view in tied)
    return values


def _basis_of(views) -> str:
    """EXACT_PRODUCT, LISTED_SPEC, MIXED, or '' for no offer."""
    bases = {view.basis for view in views}
    return "" if not bases else bases.pop() if len(bases) == 1 else "MIXED"


def minimum_label(basis: str, *, in_stock: bool) -> str:
    """How a minimum is labelled: "matching listed specs" whenever listed-spec prices are
    among those compared (2026-10-20)."""
    if basis in {LISTED_SPEC_BASIS, "MIXED"}:
        return ("Lowest in-stock price matching listed specs" if in_stock
                else "Lowest price matching listed specs")
    if not basis:
        return ""
    return "Lowest in-stock price" if in_stock else "Lowest observed price"


def _match_basis(view: ObservationView | None) -> str:
    if view is None:
        return ""
    if view.basis == LISTED_SPEC_BASIS:
        return LISTED_SPEC_BASIS
    if view.basis == ALTERNATIVE_BASIS:
        return ALTERNATIVE_BASIS
    return match_basis_from_codes(view.match.reason_codes)


def _alternative_cells(code: str, selection: Selection) -> dict[str, str]:
    """The comparison tag of a retailer cell and the detail of the alternative it shows.

    ``MATCH`` keeps its exact-product or listed-spec basis in the match-basis column; a cell with
    no price but unconfirmed pages to recheck is ``RECHECK``.
    """
    shown = selection.display
    alt = shown.alt if shown is not None and shown.basis == ALTERNATIVE_BASIS else None
    if shown is not None and alt is None:
        tag = "MATCH"
    elif alt is not None:
        tag = alt.tag
    elif selection.rechecks or selection.candidate is not None or selection.status in (
        REVIEW_REQUIRED_ONLY, CANDIDATE_NOT_CONFIRMED,
    ):
        tag = "RECHECK"
    else:
        tag = ""
    return {
        f"{code}_Comparison_Tag": tag,
        f"{code}_Alternative_Level": str(alt.difference_count) if alt else "",
        f"{code}_Alternative_Differences": alt.differences_text if alt else "",
        f"{code}_Alternative_Unknowns": _unknown_text(alt) if alt else "",
        f"{code}_Alternative_Observation_ID": shown.stored.observation_id if alt else "",
        f"{code}_Alternative_Offers": str(len(selection.alternative_pool)),
    }


def _unknown_text(evaluation: AlternativeEvaluation) -> str:
    """Unknown fields and remaining limitations of an alternative, in words."""
    parts = [evaluation.unknown_text] if evaluation.unknown else []
    if evaluation.comparison_basis:
        parts.append(f"limited basis: {evaluation.comparison_basis}")
    return "; ".join(parts)


def _alternative_minimum(
    prefix: str, views: Mapping[str, ObservationView], *, stock: bool
) -> dict[str, str]:
    """The lowest alternative price over every retailer's full eligible alternative pool, with
    the winning offer's tag, difference count, differences and observation. Ties keep every tied
    retailer, in retailer order; blank when there is none."""
    lowest = min((view.stored.price for view in views.values()), default=None)
    tied = [(code, view) for code, view in views.items() if view.stored.price == lowest]
    values = {
        prefix: money(lowest) if lowest is not None else "",
        f"{prefix}_Retailers": ";".join(code for code, _ in tied),
        f"{prefix}_Offer_URL": ";".join(view.stored.offer_url for _, view in tied),
        f"{prefix}_Observed_At": ";".join(
            local_text(view.stored.retrieved_at) for _, view in tied),
        f"{prefix}_Notes": ";".join(f"{code}:{note}" for code, view in tied
                                    for note in view.notes),
        f"{prefix}_Tag": ";".join(f"{code}:{view.alt.tag}" for code, view in tied
                                  if view.alt is not None),
        f"{prefix}_Difference_Count": ";".join(
            f"{code}:{view.alt.difference_count}" for code, view in tied
            if view.alt is not None),
        f"{prefix}_Differences": " | ".join(
            f"{code}: {view.alt.differences_text}" for code, view in tied
            if view.alt is not None),
        f"{prefix}_Unknowns": " | ".join(
            f"{code}: {_unknown_text(view.alt)}" for code, view in tied
            if view.alt is not None and _unknown_text(view.alt)),
        f"{prefix}_Observation_IDs": ";".join(
            f"{code}:{view.stored.observation_id}" for code, view in tied),
        f"{prefix}_Label": (
            ("Lowest alternative price" if stock else "Lowest in-stock alternative price")
            + " (close alternatives only; not matching configurations)"
        ) if lowest is not None else "",
    }
    if stock:
        values[f"{prefix}_Stock"] = ";".join(view.stored.availability for _, view in tied)
    return values


def _offer_details(view: ObservationView | None) -> str:
    if view is None:
        return ""
    return offer_details(dict(view.match.offer_attributes or {}), view.title)


def _warnings(view: ObservationView | None) -> str:
    if view is None:
        return ""
    codes = [code for code in view.match.reason_codes if is_warning_code(code)]
    if view.basis == ALTERNATIVE_BASIS and view.alt is not None:
        # The offer is not a matching configuration: its limitations stay beside the price.
        codes = [*view.alt.limitations, *view.alt.warnings, *codes]
    if view.basis == LISTED_SPEC_BASIS and view.listed is not None:
        # The specific product identity stays unresolved; known processor differences show.
        check = view.listed.checks.get(view.listed_target)
        codes = [IDENTITY_UNRESOLVED_WARNING, *(check.warnings if check else ()), *codes]
    return ";".join(dict.fromkeys(codes))


def cell_reason(selection: Selection) -> str:
    """Why a retailer cell has this price, or why it is blank. Never implies a fresh fetch."""
    if selection.status == SELECTED:
        assert selection.chosen is not None
        if selection.chosen.stored.availability == Availability.OUT_OF_STOCK.value:
            return "PRICE_SHOWN_WHILE_OUT_OF_STOCK"
        return ""
    if selection.display_status == ALTERNATIVE_SELECTED:
        shown = selection.alternative_best
        assert shown is not None and shown.alt is not None
        parts = [f"CLOSE_ALTERNATIVE: {shown.alt.tag}; {shown.alt.differences_text}"]
        if shown.stored.availability == Availability.OUT_OF_STOCK.value:
            parts.append("PRICE_SHOWN_WHILE_OUT_OF_STOCK")
        return "; ".join(parts)
    if selection.status == NO_COMPARABLE_OFFER:
        listed = selection.observed is not None and selection.observed.basis == LISTED_SPEC_BASIS
        prefix = "LISTED_SPEC_PRICE_EXCLUDED" if listed else "EXACT_PRICE_EXCLUDED"
        return f"{prefix}: " + selection.not_comparable_reasons
    if selection.status == REVIEW_REQUIRED_ONLY:
        return f"MATCH_REVIEW_REQUIRED: {selection.review_required} observation(s)" + (
            f"; {selection.candidate_status}: {selection.candidate_detail}"
            if selection.candidate is not None else ""
        )
    if selection.status == CANDIDATE_NOT_CONFIRMED:
        candidate = selection.candidate
        assert candidate is not None
        return (
            f"CANDIDATE_NOT_CONFIRMED: {selection.candidate_status}"
            + (f" ({selection.candidate_detail})" if selection.candidate_detail else "")
            + f"; observed {local_text(candidate.stored.retrieved_at)}"
        )
    stale = ""
    if selection.stale is not None:
        stale = (
            f"last comparable {selection.stale.stored.price_text} on "
            f"{local_text(selection.stale.stored.retrieved_at)} (not carried forward)"
        )
    if selection.status in SEARCH_STAGE_STATUSES:
        return "; ".join(filter(None, (
            f"{selection.status}: {selection.search_detail}".rstrip(": "), stale,
        )))
    return "; ".join(filter(None, ("NO_EXACT_OFFER_THIS_WEEK", stale)))


def _selection_row(
    item: SelectedConfiguration,
    selection: Selection,
    week_fields: Mapping[str, str],
    context: MatchingContext,
) -> dict[str, str]:
    # The offer shown in the cell: a matching configuration, else the best close alternative.
    shown = selection.display
    chosen = shown.stored if shown else None
    stale = selection.stale.stored if selection.stale else None
    row = {
        **week_fields,
        "rank": str(item.rank),
        "target_id": item.target_id,
        "retailer_code": selection.retailer_code,
        "retailer_id": dict(RETAILERS)[selection.retailer_code],
        "price_status": selection.display_status,
        "price": chosen.price_text if chosen else "",
        "currency": chosen.currency if chosen else "",
        "price_type": chosen.price_type if chosen else "",
        "regular_price": chosen.regular_price if chosen else "",
        "sale_price": chosen.sale_price if chosen else "",
        "offer_url": chosen.offer_url if chosen else "",
        "offer_index": str(chosen.offer_index) if chosen else "",
        "retailer_product_id": chosen.retailer_product_id if chosen else "",
        "retailer_sku": chosen.retailer_sku if chosen else "",
        "retrieved_at_utc": utc_text(chosen.retrieved_at) if chosen else "",
        "retrieved_at_local": local_text(chosen.retrieved_at) if chosen else "",
        "observation_id": chosen.observation_id if chosen else "",
        "offer_key": chosen.offer_key if chosen else "",
        "selection_reason": _selection_reason(selection) or _alternative_selection_reason(
            selection),
        "comparable_offers": str(selection.comparable_offers),
        "tied_offers": str(selection.tied_offers),
        "offers_observed": str(selection.offers_observed),
        "observations_in_week": str(selection.observations_in_week),
        "not_comparable_reasons": selection.not_comparable_reasons,
        "review_required_observations": str(selection.review_required),
        "stale_last_comparable_retrieved_at_utc": utc_text(stale.retrieved_at) if stale else "",
        "stale_last_comparable_price": stale.price_text if stale else "",
        "stale_last_comparable_offer_url": stale.offer_url if stale else "",
        "adapter_version": chosen.adapter_version if chosen else "",
        "content_sha256": chosen.content_sha256 if chosen else "",
        "matching_context_id": context.context_id,
    }
    in_stock = selection.chosen_in_stock.stored if selection.chosen_in_stock else None
    latest = selection.observed.stored if selection.observed else None
    row |= {
        "availability": chosen.availability if chosen else "",
        "match_basis": _match_basis(shown),
        "match_warnings": _warnings(shown),
        "price_notes": ";".join(shown.notes) if shown else "",
        "in_stock_price": in_stock.price_text if in_stock else "",
        "in_stock_offer_url": in_stock.offer_url if in_stock else "",
        "in_stock_retrieved_at_utc": utc_text(in_stock.retrieved_at) if in_stock else "",
        "in_stock_observation_id": in_stock.observation_id if in_stock else "",
        "in_stock_price_notes": ";".join(selection.chosen_in_stock.notes)
        if selection.chosen_in_stock else "",
        "latest_exact_offer_url": latest.offer_url if latest else "",
        "latest_exact_availability": latest.availability if latest else "",
        "latest_exact_retrieved_at_utc": utc_text(latest.retrieved_at) if latest else "",
        "candidate_offer_url": selection.candidate.stored.offer_url
        if selection.candidate and not chosen else "",
        "candidate_status": selection.candidate_status if not chosen else "",
        "candidate_detail": selection.candidate_detail if not chosen else "",
        "candidate_observation_id": selection.candidate.stored.observation_id
        if selection.candidate and not chosen else "",
        "recheck_count": str(len(selection.rechecks)) if not chosen else "",
        "recheck_note": recheck_note(selection.rechecks[0])
        if selection.rechecks and not chosen else "",
        "comparison_basis": shown.basis if shown else "",
        "offer_details": _offer_details(shown),
    }
    alt = shown.alt if shown is not None and shown.basis == ALTERNATIVE_BASIS else None
    lowest, lowest_stock = selection.alternative_lowest, selection.alternative_lowest_in_stock
    row |= {
        "selection_kind": "MATCH" if shown is not None and alt is None else (
            f"ALT_{alt.difference_count}" if alt else ""),
        "alternative_tag": alt.tag if alt else "",
        "alternative_level": str(alt.difference_count) if alt else "",
        "alternative_differences": alt.differences_text if alt else "",
        "alternative_observation_id": shown.stored.observation_id if alt else "",
        "alternative_pool_size": str(len(selection.alternative_pool)),
        "alternative_lowest_price": lowest.stored.price_text if lowest else "",
        "alternative_lowest_observation_id": lowest.stored.observation_id if lowest else "",
        "alternative_lowest_in_stock_price": lowest_stock.stored.price_text
        if lowest_stock else "",
        "alternative_lowest_in_stock_observation_id": lowest_stock.stored.observation_id
        if lowest_stock else "",
    }
    return {column: row[column] for column in SELECTION_COLUMNS}


def _alternative_selection_reason(selection: Selection) -> str:
    """Why the cell shows this alternative: no matching configuration is comparable, and this
    is the lowest eligible price of the fewest-differences level."""
    shown = selection.alternative_best
    if selection.chosen is not None or shown is None or shown.alt is None:
        return ""
    level = shown.alt.difference_count
    same_level = sum(1 for v in selection.alternative_pool
                     if v.alt is not None and v.alt.difference_count == level)
    return (f"NO_COMPARABLE_MATCH;ALT_{level}_LOWEST_OF_{same_level}_ELIGIBLE_ALTERNATIVES"
            f"_OF_{len(selection.alternative_pool)}_IN_POOL")


def _candidate_url(selection: Selection) -> str:
    if selection.candidate is not None:
        return selection.candidate.stored.offer_url
    return selection.rechecks[0].url if selection.rechecks else ""


def _candidate_status(selection: Selection) -> str:
    if selection.candidate is not None:
        return selection.candidate_status
    return selection.rechecks[0].status if selection.rechecks else ""


def _candidate_detail(selection: Selection) -> str:
    if selection.candidate is not None:
        return selection.candidate_detail
    return selection.rechecks[0].explanation if selection.rechecks else ""


def recheck_rows(
    selections: Mapping[tuple[str, str], Selection],
    selected: SelectedList,
    week_fields: Mapping[str, str],
) -> list[dict[str, str]]:
    """Every recheck candidate of the report week's blank cells (recheck_candidates.csv)."""
    ranks = {item.target_id: item.rank for item in selected.configurations}
    retailer_ids = dict(RETAILERS)
    rows = []
    for item in selected.configurations:
        for code, _ in RETAILERS:
            selection = selections[(item.target_id, code)]
            if selection.display is not None:
                continue
            for index, candidate in enumerate(selection.rechecks, start=1):
                rows.append({
                    "report_week_start": week_fields["report_week_start"],
                    "rank": str(ranks[item.target_id]),
                    "target_id": item.target_id,
                    "retailer_code": code,
                    "retailer_id": retailer_ids[code],
                    "recheck_rank": str(index),
                    "stage": candidate.stage,
                    "status": candidate.status,
                    "explanation": candidate.explanation,
                    "url": candidate.url,
                    "title": candidate.title,
                    "stock": candidate.stock,
                    "observed_at_local": candidate.observed_at_local,
                    "unconfirmed_price": candidate.unconfirmed_price,
                    "observation_id": candidate.observation_id,
                    "cell_price_status": selection.status,
                })
    return rows


def _evidence_text(evaluation: AlternativeEvaluation) -> str:
    """The field-level evidence behind every dimension, in the fixed dimension order."""
    return " | ".join(
        f"{c.dimension}: " + "; ".join(c.evidence) for c in evaluation.comparisons if c.evidence)


def alternative_rows(
    selections: Mapping[tuple[str, str], Selection],
    selected: SelectedList,
    week_fields: Mapping[str, str],
    context: MatchingContext,
) -> list[dict[str, str]]:
    """Every close-alternative association of the report week, one row per target-retailer-offer
    (weekly_price_alternatives.csv), including alternatives of retailers that also have a
    matching offer and offers hidden by the main display."""
    rows: list[dict[str, str]] = []
    for item in selected.configurations:
        target = item.target
        for code, retailer_id in RETAILERS:
            selection = selections[(item.target_id, code)]
            best = selection.alternative_best
            for view in selection.alternatives:
                evaluation, stored = view.alt, view.stored
                assert evaluation is not None
                offer = dict(view.match.offer_attributes or {})
                shown = selection.chosen is None and best is not None and (
                    best.stored.observation_id == stored.observation_id)
                row = {
                    **week_fields,
                    "rank": str(item.rank), "target_id": item.target_id,
                    "target_brand": str(target.get("brand") or ""),
                    "target_model": str(target.get("model") or ""),
                    "target_screen_in": str(target.get("screen_size_in") or ""),
                    "target_cpu_series": str(target.get("cpu_series") or ""),
                    "target_cpu_model": str(target.get("cpu_model") or ""),
                    "target_ram_gb": str(target.get("ram_capacity_gb") or ""),
                    "target_storage_gb": str(target.get("storage_capacity_gb") or ""),
                    "target_gpu": str(target.get("gpu_model") or ""),
                    "retailer_code": code, "retailer_id": retailer_id,
                    "offer_key": stored.offer_key, "observation_id": stored.observation_id,
                    "offer_url": stored.offer_url, "offer_index": str(stored.offer_index),
                    "retailer_product_id": stored.retailer_product_id,
                    "retailer_sku": stored.retailer_sku, "title": view.title,
                    "retrieved_at_utc": utc_text(stored.retrieved_at),
                    "retrieved_at_local": local_text(stored.retrieved_at),
                    "content_sha256": stored.content_sha256,
                    "adapter_version": stored.adapter_version,
                    "price": stored.price_text, "currency": stored.currency,
                    "price_type": stored.price_type, "regular_price": stored.regular_price,
                    "sale_price": stored.sale_price, "availability": stored.availability,
                    "bundle_flag": stored.bundle_flag, "price_eligibility": view.status,
                    "price_exclusion_reasons": ";".join(view.reasons),
                    "price_notes": ";".join(view.notes),
                    "alternative_tag": evaluation.tag,
                    "difference_count": str(evaluation.difference_count),
                    "differing_dimensions": ";".join(evaluation.differing),
                    "differences": evaluation.differences_text,
                    **{f"{name}_state": (evaluation.dimension(name).state
                                         if evaluation.dimension(name) else "")
                       for name in DIMENSIONS},
                    "offer_brand": offer.get("brand", ""), "offer_model": offer.get("model", ""),
                    "offer_screen_in": offer.get("screen_size_in", ""),
                    "offer_cpu_series": offer.get("cpu_series", ""),
                    "offer_cpu_model": offer.get("cpu_model", ""),
                    "offer_ram_gb": offer.get("ram_capacity_gb", ""),
                    "offer_storage_gb": offer.get("storage_capacity_gb", ""),
                    "offer_gpu": offer.get("gpu_model", ""),
                    "unknown_fields": _unknown_text(evaluation),
                    "limitations": ";".join(evaluation.limitations),
                    "warnings": ";".join(evaluation.warnings),
                    "comparison_basis": evaluation.comparison_basis,
                    "family": evaluation.family, "family_basis": evaluation.family_basis,
                    "displayed_in_comparison": str(shown).upper(),
                    "retailer_has_matching_offer": str(selection.chosen is not None).upper(),
                    "exact_target_id": view.match.target_id if view.is_exact else "",
                    "listed_spec_target_ids": ";".join(view.listed.target_ids)
                    if view.listed is not None and view.listed.status == LISTED_SPEC_MATCH else "",
                    "initiating_target_ids": ";".join(view.search_targets),
                    "evidence_references": _evidence_text(evaluation),
                    "comparison_detail_json": evaluation.to_json(),
                    "policy_version": evaluation.policy_version,
                    "matching_context_id": context.context_id,
                    "target_master_sha256": context.target_master_sha256,
                }
                rows.append({column: row[column] for column in ALTERNATIVE_COLUMNS})
    return rows


def evaluation_rows(
    views: Sequence[ObservationView], selected: SelectedList, week: ReportWeek
) -> list[dict[str, str]]:
    """Every close-alternative evaluation of the report week's latest offers against the selected
    targets (alternative_evaluations.csv): alternatives, rechecks, exclusions and zero-difference
    pairs, with the reasons, for audit."""
    ranks = {item.target_id: item.rank for item in selected.configurations}
    latest = [_latest(group) for group in _group(
        [v for v in views if v.week_start == week.start],
        lambda v: v.stored.offer_key).values()]
    rows = []
    for view in sorted(latest, key=lambda v: (v.stored.retrieved_at, v.stored.observation_id)):
        for evaluation in view.evaluations:
            if evaluation.target_id not in ranks:
                continue
            rows.append({
                "report_week_start": week.start.isoformat(),
                "rank": str(ranks[evaluation.target_id]),
                "target_id": evaluation.target_id,
                "retailer_code": view.retailer_code,
                "observation_id": view.stored.observation_id,
                "offer_url": view.stored.offer_url,
                "retrieved_at_utc": utc_text(view.stored.retrieved_at),
                "status": evaluation.status,
                "alternative_tag": evaluation.tag,
                "difference_count": str(evaluation.difference_count),
                "differing_dimensions": ";".join(evaluation.differing),
                "conflicting_dimensions": ";".join(evaluation.conflicting),
                "unknown_dimensions": ";".join(evaluation.unknown),
                "reasons": ";".join(evaluation.reasons),
                "material_conflicts": ";".join(evaluation.material_conflicts),
                "limitations": ";".join(evaluation.limitations),
                "family": evaluation.family,
                "family_basis": evaluation.family_basis,
                "exact_target_id": view.match.target_id if view.is_exact else "",
                "initiating_target_ids": ";".join(view.search_targets),
                "differences": evaluation.differences_text or evaluation.detail,
                "policy_version": evaluation.policy_version,
            })
    return sorted(rows, key=lambda r: (int(r["rank"]), r["retailer_code"], r["observation_id"]))


def _history_rows(
    views: Sequence[ObservationView], ranks: Mapping[str, int], context: MatchingContext
) -> list[dict[str, str]]:
    ordered = sorted(views, key=lambda v: (v.stored.retrieved_at, v.stored.observation_id))
    latest_in_week = {
        _latest(group).stored.observation_id
        for group in _group(ordered, lambda v: (v.stored.offer_key, v.week_start)).values()
    }
    previous: dict[tuple[str, str], ObservationView] = {}
    since: Counter[str] = Counter()
    rows = []
    for view in ordered:
        stored, match = view.stored, view.match
        key = (stored.offer_key, view.target_id)
        prior = previous.get(key) if view.status == COMPARABLE else None
        if view.status != COMPARABLE:
            change, change_status = "", "NOT_CALCULATED"
        elif prior is None:
            change, change_status = "", "FIRST_COMPARABLE"
        else:
            delta = stored.price - prior.stored.price
            change, change_status = money(delta), _change_status(delta)
        rows.append(
            {
                "observation_id": stored.observation_id,
                "offer_key": stored.offer_key,
                "retailer_code": view.retailer_code,
                "retailer_id": stored.retailer_id,
                "offer_url": stored.offer_url,
                "offer_index": str(stored.offer_index),
                "retailer_product_id": stored.retailer_product_id,
                "retailer_sku": stored.retailer_sku,
                "retrieved_at_utc": utc_text(stored.retrieved_at),
                "retrieved_at_local": local_text(stored.retrieved_at),
                "report_week_start": view.week_start.isoformat(),
                "price": stored.price_text,
                "currency": stored.currency,
                "price_type": stored.price_type,
                "regular_price": stored.regular_price,
                "sale_price": stored.sale_price,
                "availability": stored.availability,
                "bundle_flag": stored.bundle_flag,
                "match_status": match.match_status,
                "target_id": match.target_id,
                "candidate_target_ids": ";".join(match.candidate_target_ids),
                "match_reason_codes": ";".join(match.reason_codes),
                "in_selected_list": str(view.is_exact and view.target_id in ranks),
                "selected_rank": str(ranks[view.target_id]) if view.target_id in ranks else "",
                "comparability_status": view.status,
                "comparability_reasons": ";".join(view.reasons),
                "latest_for_offer_in_week": str(stored.observation_id in latest_in_week),
                "previous_comparable_observation_id": prior.stored.observation_id if prior else "",
                "previous_comparable_retrieved_at_utc": utc_text(prior.stored.retrieved_at)
                if prior
                else "",
                "previous_comparable_price": prior.stored.price_text if prior else "",
                "observations_since_previous_comparable": str(since[stored.offer_key])
                if prior
                else "",
                "price_change": change,
                "price_change_status": change_status,
                "adapter_version": stored.adapter_version,
                "content_sha256": stored.content_sha256,
                "first_import_batch_id": str(stored.first_batch_id),
                "matching_context_id": context.context_id,
                "target_master_sha256": context.target_master_sha256,
                "matching_rules_version": context.matching_rules_version,
                "offer_terms_category": view.terms.category if view.terms else "",
                "offer_terms_evidence": " | ".join(view.terms.evidence) if view.terms else "",
                "price_notes": ";".join(view.notes),
                "search_context_target_ids": ";".join(view.search_targets),
                "listed_spec_status": view.listed.status if view.listed else "",
                "listed_spec_target_ids": ";".join(view.listed.target_ids) if view.listed
                else "",
                "listed_spec_ambiguous_target_ids": ";".join(view.listed.ambiguous_ids)
                if view.listed else "",
                "listed_spec_comparability": view.listed_status,
                "listed_spec_comparability_reasons": ";".join(view.listed_reasons),
                "alternative_target_ids": ";".join(
                    e.target_id for e in view.evaluations if e.is_alternative),
                "alternative_tags": ";".join(
                    f"{e.target_id}:{e.tag}" for e in view.evaluations if e.is_alternative),
                "alternative_recheck_target_ids": ";".join(
                    e.target_id for e in view.evaluations if e.status == RECHECK),
                "alternative_comparability": view.alternative_status,
                "alternative_comparability_reasons": ";".join(view.alternative_reasons),
            }
        )
        if view.status == COMPARABLE:
            previous[key] = view
            since[stored.offer_key] = 0
        else:
            since[stored.offer_key] += 1
    return rows


def _series_rows(
    weeks: Sequence[ReportWeek],
    by_week: Mapping[date, Mapping[tuple[str, str], Selection]],
    selected: SelectedList,
    as_of: datetime,
) -> list[dict[str, str]]:
    rows = []
    for item in selected.configurations:
        for code, _ in RETAILERS:
            prior: Selection | None = None
            for index, week in enumerate(weeks):
                selection = by_week[week.start][(item.target_id, code)]
                chosen = selection.chosen.stored if selection.chosen else None
                before = prior.chosen.stored if prior and prior.chosen else None
                if chosen is None:
                    change, status, offer_changed = "", "NO_PRICE", ""
                elif index == 0:
                    change, status, offer_changed = "", "FIRST_WEEK_IN_RANGE", ""
                elif before is None:
                    change, status, offer_changed = "", "NO_PREVIOUS_WEEK_PRICE", ""
                else:
                    delta = chosen.price - before.price
                    change, status = money(delta), _change_status(delta)
                    offer_changed = str(chosen.offer_key != before.offer_key)
                rows.append(
                    {
                        "report_week_start": week.start.isoformat(),
                        "report_week_end": week.end.isoformat(),
                        "week_status": week.status(as_of),
                        "rank": str(item.rank),
                        "target_id": item.target_id,
                        "retailer_code": code,
                        "price_status": selection.status,
                        "price": chosen.price_text if chosen else "",
                        "offer_key": chosen.offer_key if chosen else "",
                        "offer_url": chosen.offer_url if chosen else "",
                        "retrieved_at_utc": utc_text(chosen.retrieved_at) if chosen else "",
                        "observation_id": chosen.observation_id if chosen else "",
                        "previous_week_price_status": prior.status if prior else "NOT_IN_RANGE",
                        "previous_week_price": before.price_text if before else "",
                        "price_change": change,
                        "change_status": status,
                        "representative_offer_changed": offer_changed,
                    }
                )
                prior = selection
    return rows


DISAGREEMENT_FIELDS: tuple[str, ...] = ("resolution", "display_type", "gpu_model")
_NOMINAL = re.compile(r"nominal ([0-9]+x[0-9]+)")


def _display_statements(view: ObservationView) -> dict[str, tuple[str, str]]:
    """``field -> (as stated, comparison key)`` for one offer's own display and GPU evidence.

    Extracted values are used; when no resolution is extracted, a single marketing resolution
    name is used by its nominal size and shown as such ("Full HD (nominal 1920x1080)").
    """
    extracted = extract_offer_attributes(view.stored.observation())
    found: dict[str, tuple[str, str]] = {}
    for item in extracted.evidence:
        if item.field in DISAGREEMENT_FIELDS and item.status is ExtractionStatus.EXTRACTED:
            key = item.value.casefold()
            if item.field == "gpu_model":
                forms = canonical_gpu_models(item.value)
                key = forms.pop().casefold() if len(forms) == 1 else key
            elif item.field == "display_type":
                key = key.removesuffix("-level")
            found[item.field] = (item.value, key)
    names = [item.value for item in extracted.evidence if item.field == "resolution_name"]
    nominal = _NOMINAL.search(names[0]) if len(set(names)) == 1 else None
    if "resolution" not in found and nominal:
        found["resolution"] = (names[0], nominal[1])
    return found


def offer_disagreements(chosen: Mapping[str, ObservationView]) -> dict[str, str]:
    """``field -> "DBZ 1920x1200; PCX Full HD (nominal 1920x1080)"`` where the offers chosen for
    one configuration state different values. Missing statements are never a disagreement."""
    statements = {code: _display_statements(view) for code, view in chosen.items()}
    found: dict[str, str] = {}
    for name in DISAGREEMENT_FIELDS:
        stated = {code: values[name] for code, values in statements.items() if name in values}
        if len({key for _, key in stated.values()}) > 1:
            found[name] = "; ".join(f"{code} {shown}" for code, (shown, _) in sorted(
                stated.items()))
    return found


def _review_reason(view: ObservationView, ranks: Mapping[str, int]) -> str:
    listed = view.listed
    if listed is not None and listed.status == LISTED_SPEC_AMBIGUOUS and (
        set(listed.ambiguous_ids) & set(ranks)
    ):
        return f"{LISTED_SPEC_AMBIGUOUS} ({listed.detail()})"
    if not view.is_exact and listed is not None and listed.status == LISTED_SPEC_MATCH and (
        set(listed.target_ids) & set(ranks)
    ):
        return (f"{LISTED_SPEC_MATCH} {';'.join(listed.target_ids)} "
                f"({listed.detail()})")
    if not view.is_exact:
        close = [e for e in view.evaluations if e.is_alternative and e.target_id in ranks]
        if close:
            # A confirmed qualifying alternative is not an unresolved "different configuration"
            # error (2026-10-27); it stays listed, labelled, for traceability.
            return CLOSE_ALTERNATIVE_REVIEW + " " + "; ".join(
                f"{e.tag} for {e.target_id} ({e.differences_text})" for e in close)
        return f"MATCH_{view.match.match_status}"
    if view.target_id not in ranks:
        return "EXACT_TARGET_NOT_IN_SELECTED_LIST"
    if view.status != COMPARABLE:
        return "NOT_COMPARABLE"
    return ""


def _review_row(
    view: ObservationView, ranks: Mapping[str, int], context: MatchingContext, week: ReportWeek
) -> dict[str, str]:
    stored, match = view.stored, view.match
    row = {
        "report_week_start": week.start.isoformat(),
        "review_reason": _review_reason(view, ranks),
        "observation_id": stored.observation_id,
        "retailer_code": view.retailer_code,
        "retailer_id": stored.retailer_id,
        "offer_url": stored.offer_url,
        "retrieved_at_utc": utc_text(stored.retrieved_at),
        "price": stored.price_text,
        "currency": stored.currency,
        "availability": stored.availability,
        "bundle_flag": stored.bundle_flag,
        "match_status": match.match_status,
        "target_id": match.target_id,
        "candidate_target_ids": ";".join(match.candidate_target_ids),
        "match_reason_codes": ";".join(match.reason_codes),
        "in_selected_list": str(view.is_exact and view.target_id in ranks),
        "comparability_status": view.status,
        "comparability_reasons": ";".join(view.reasons),
        "matching_context_id": context.context_id,
        "offer_terms_category": view.terms.category if view.terms else "",
        "offer_terms_evidence": " | ".join(view.terms.evidence) if view.terms else "",
        "search_context_target_ids": ";".join(view.search_targets),
    }
    return {column: row[column] for column in REVIEW_COLUMNS}


def _change_status(delta: Decimal) -> str:
    return "UNCHANGED" if delta == 0 else "INCREASED" if delta > 0 else "DECREASED"


def _group(items, key):
    groups = defaultdict(list)
    for item in items:
        groups[key(item)].append(item)
    return groups
