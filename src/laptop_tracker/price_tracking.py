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
  in PHP, with a known price type and checked bundle status. Confirmed bundles participate
  at their displayed package price and carry a BUNDLE_OFFER note (owner policy, 2026-10-15).
  Everything else is kept in history with its status and reasons.
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
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

from . import conventions, evidence_approvals, extraction, matching, normalization
from .matching import ExactMatcher, MatchStatus, is_warning_code, match_basis_from_codes
from .normalization import CORE_MATCH_COLUMNS, core_key
from .observation_store import (
    MatchingContext,
    ObservationStore,
    StoredMatch,
    StoredObservation,
)
from .offers import Availability, PriceType
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
MATCHING_RULES_VERSION = "five-field-exact/balanced-evidence/2026-10-15"
# Modules whose code defines matching outcomes; their bytes fingerprint the rule version.
RULE_MODULES: tuple[ModuleType, ...] = (
    normalization, conventions, evidence_approvals, extraction, matching
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
)

HISTORY_COLUMNS: tuple[str, ...] = (
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
)

# Comparability statuses.
COMPARABLE = "COMPARABLE"
NOT_COMPARABLE = "NOT_COMPARABLE"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
NOT_MATCHED = "NOT_MATCHED"

# Price statuses for one target and retailer in one week.
SELECTED = "SELECTED"
NO_OBSERVATION_IN_WEEK = "NO_OBSERVATION_IN_WEEK"
NO_COMPARABLE_OFFER = "NO_COMPARABLE_OFFER"
REVIEW_REQUIRED_ONLY = "REVIEW_REQUIRED_ONLY"

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
                    "shopee_price_status": snapshot["price_status"]
                    if snapshot
                    else "SALES_RANKING_NOT_SUPPLIED",
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

    @property
    def target_id(self) -> str:
        return self.match.target_id

    @property
    def is_exact(self) -> bool:
        return self.match.match_status == MatchStatus.EXACT.value


def comparability(stored: StoredObservation, match: StoredMatch) -> tuple[str, tuple[str, ...]]:
    """Whether an observation's price can stand for its matched target, and why not."""
    if match.match_status == MatchStatus.REVIEW_REQUIRED.value:
        return REVIEW_REQUIRED, ("MATCH_REVIEW_REQUIRED",)
    if match.match_status != MatchStatus.EXACT.value or not match.target_id:
        return NOT_MATCHED, (f"MATCH_{match.match_status}",)
    reasons = price_rule_reasons(
        retailer_id=stored.retailer_id,
        currency=stored.currency,
        availability=stored.availability,
        bundle_flag=stored.bundle_flag,
        price_type=stored.price_type,
        price=stored.price,
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
) -> tuple[str, ...]:
    """Why an exactly matched offer's price is not comparable; empty when it is."""
    reasons = []
    if retailer_id not in RETAILER_CODE_BY_ID:
        reasons.append("RETAILER_NOT_IN_COMPARISON")
    if currency != REPORTING_CURRENCY:
        reasons.append("CURRENCY_NOT_PHP")
    if availability not in {Availability.IN_STOCK.value, Availability.OUT_OF_STOCK.value}:
        reasons.append(f"AVAILABILITY_{availability}")
    if bundle_flag not in {"TRUE", "FALSE"}:
        reasons.append("BUNDLE_STATUS_UNKNOWN")
    if price_type not in {PriceType.REGULAR.value, PriceType.SALE.value}:
        reasons.append("PRICE_TYPE_UNKNOWN")
    if not price.is_finite() or price <= 0:
        reasons.append("PRICE_INVALID")
    return tuple(reasons)


def price_note_codes(bundle_flag: str) -> tuple[str, ...]:
    """Nonblocking price notes, kept separate from identity and exclusion reasons."""
    return ("BUNDLE_OFFER",) if bundle_flag == "TRUE" else ()


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


def select_week(
    week: ReportWeek,
    views: Sequence[ObservationView],
    selected: SelectedList,
) -> dict[tuple[str, str], Selection]:
    """One selection per selected target and retailer for ``week`` (views already cut at as_of)."""
    in_week = [view for view in views if view.week_start == week.start]
    by_offer: dict[str, list[ObservationView]] = defaultdict(list)
    for view in in_week:
        by_offer[view.stored.offer_key].append(view)
    latest = [_latest(group) for group in by_offer.values()]

    exact_latest: dict[tuple[str, str], list[ObservationView]] = defaultdict(list)
    for view in latest:
        if view.is_exact and view.retailer_code:
            exact_latest[(view.target_id, view.retailer_code)].append(view)
    exact_all: Counter[tuple[str, str]] = Counter(
        (view.target_id, view.retailer_code) for view in in_week if view.is_exact
    )
    review: Counter[tuple[str, str]] = Counter(
        (candidate, view.retailer_code)
        for view in in_week
        if view.status == REVIEW_REQUIRED
        for candidate in view.match.candidate_target_ids
    )
    earlier: dict[tuple[str, str], ObservationView] = {}
    for view in views:
        if view.week_start < week.start and view.status == COMPARABLE:
            key = (view.target_id, view.retailer_code)
            if key not in earlier or _latest([earlier[key], view]) is view:
                earlier[key] = view

    selections = {}
    for item in selected.configurations:
        for code, _ in RETAILERS:
            key = (item.target_id, code)
            offers = exact_latest.get(key, [])
            comparable = sorted((v for v in offers if v.status == COMPARABLE), key=_selection_key)
            reasons = Counter(reason for v in offers for reason in v.reasons)
            if comparable:
                status, chosen = SELECTED, comparable[0]
                tied = sum(1 for v in comparable if v.stored.price == chosen.stored.price)
            else:
                chosen, tied = None, 0
                if offers:
                    status = NO_COMPARABLE_OFFER
                elif review[key]:
                    status = REVIEW_REQUIRED_ONLY
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
                observations_in_week=exact_all[key],
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
            )
    return selections


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


def build_views(
    observations: Sequence[StoredObservation],
    matches: Mapping[str, StoredMatch],
    *,
    as_of: datetime,
    last_week: ReportWeek,
) -> tuple[list[ObservationView], int]:
    """Classify observations retrieved no later than ``as_of`` and before ``last_week`` ends.

    Returns the views and how many stored observations were left out by that cut-off.
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
        status, reasons = comparability(stored, match)
        views.append(
            ObservationView(
                stored,
                match,
                week_start_of(stored.retrieved_at),
                status,
                reasons,
                RETAILER_CODE_BY_ID.get(stored.retailer_id, ""),
            )
        )
    return views, excluded


def build_price_tracking(
    observations: Sequence[StoredObservation],
    matches: Mapping[str, StoredMatch],
    selected: SelectedList,
    context: MatchingContext,
    *,
    week: ReportWeek,
    as_of: datetime,
    history_start: date | None = None,
) -> PriceTrackingResult:
    if as_of < week.start_utc:
        raise PriceTrackingInputError("--as-of is before the report week starts.")
    views, excluded = build_views(observations, matches, as_of=as_of, last_week=week)
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
    by_week = {w.start: select_week(w, views, selected) for w in weeks}
    current = by_week[week.start]
    week_fields = {
        "report_week_start": week.start.isoformat(),
        "report_week_end": week.end.isoformat(),
        "week_status": week.status(as_of),
    }

    comparison = [
        _comparison_row(item, current, selected, context, week_fields, as_of)
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
    for status in (SELECTED, NO_COMPARABLE_OFFER, REVIEW_REQUIRED_ONLY, NO_OBSERVATION_IN_WEEK):
        counts[f"cells_{status}"] = sum(1 for row in selections if row["price_status"] == status)
    return PriceTrackingResult(comparison, selections, history, series, review, counts)


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
    for code, _ in RETAILERS:
        selection = selections[(item.target_id, code)]
        chosen = selection.chosen
        row[f"{code}_Price"] = chosen.stored.price_text if chosen else ""
        linked_offer = chosen or selection.observed
        row[f"{code}_Offer_URL"] = linked_offer.stored.offer_url if linked_offer else ""
        row[f"{code}_Price_Status"] = selection.status
        row |= {
            f"{code}_Stock": linked_offer.stored.availability if linked_offer else "",
            f"{code}_Observed_At": local_text(linked_offer.stored.retrieved_at)
            if linked_offer else "",
            f"{code}_Match_Basis": match_basis_from_codes(linked_offer.match.reason_codes)
            if linked_offer else "",
            f"{code}_Warnings": ";".join(filter(None, (
                _warnings(linked_offer),
                ";".join(price_note_codes(linked_offer.stored.bundle_flag))
                if linked_offer else "",
            ))),
            f"{code}_Reason": cell_reason(selection),
        }
        if chosen:
            observed[code] = chosen
        if selection.chosen_in_stock:
            in_stock[code] = selection.chosen_in_stock
    row |= {
        "Comparable_Retailer_Count": str(len(observed)),
        **_minimum("Lowest_Price", "Lowest_Price", observed, stock=True),
        **_minimum("Lowest_In_Stock_Price", "Lowest_In_Stock", in_stock, stock=False),
        "Shopee_Sales_Extract_Date": selected.sales_extract_date,
        "matching_context_id": context.context_id,
        "selected_list_sha256": selected.sha256,
    }
    return {column: row[column] for column in COMPARISON_COLUMNS}


def _minimum(
    price_column: str, prefix: str, views: Mapping[str, ObservationView], *, stock: bool
) -> dict[str, str]:
    """The lowest price among per-retailer choices, its retailer(s), link(s), time(s) and stock.

    Ties keep every tied retailer, in retailer column order. Blank when there is no price.
    """
    lowest = min((view.stored.price for view in views.values()), default=None)
    tied = [(code, view) for code, view in views.items() if view.stored.price == lowest]
    values = {
        price_column: money(lowest) if lowest is not None else "",
        f"{price_column}_Retailers": ";".join(code for code, _ in tied),
        f"{prefix}_Offer_URL": ";".join(view.stored.offer_url for _, view in tied),
        f"{prefix}_Observed_At": ";".join(
            local_text(view.stored.retrieved_at) for _, view in tied
        ),
        f"{prefix}_Notes": ";".join(
            f"{code}:{note}" for code, view in tied
            for note in price_note_codes(view.stored.bundle_flag)
        ),
    }
    if stock:
        values[f"{prefix}_Stock"] = ";".join(view.stored.availability for _, view in tied)
    return values


def _warnings(view: ObservationView | None) -> str:
    return ";".join(code for code in view.match.reason_codes if is_warning_code(code)) if (
        view
    ) else ""


def cell_reason(selection: Selection) -> str:
    """Why a retailer cell has this price, or why it is blank. Never implies a fresh fetch."""
    if selection.status == SELECTED:
        assert selection.chosen is not None
        if selection.chosen.stored.availability == Availability.OUT_OF_STOCK.value:
            return "PRICE_SHOWN_WHILE_OUT_OF_STOCK"
        return ""
    if selection.status == NO_COMPARABLE_OFFER:
        return "EXACT_PRICE_EXCLUDED: " + selection.not_comparable_reasons
    if selection.status == REVIEW_REQUIRED_ONLY:
        return f"MATCH_REVIEW_REQUIRED: {selection.review_required} observation(s)"
    if selection.stale is not None:
        stale = selection.stale.stored
        return (
            "NO_EXACT_OFFER_THIS_WEEK; last comparable "
            f"{stale.price_text} on {local_text(stale.retrieved_at)} (not carried forward)"
        )
    return "NO_EXACT_OFFER_THIS_WEEK"


def _selection_row(
    item: SelectedConfiguration,
    selection: Selection,
    week_fields: Mapping[str, str],
    context: MatchingContext,
) -> dict[str, str]:
    chosen = selection.chosen.stored if selection.chosen else None
    stale = selection.stale.stored if selection.stale else None
    row = {
        **week_fields,
        "rank": str(item.rank),
        "target_id": item.target_id,
        "retailer_code": selection.retailer_code,
        "retailer_id": dict(RETAILERS)[selection.retailer_code],
        "price_status": selection.status,
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
        "selection_reason": _selection_reason(selection),
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
        "match_basis": match_basis_from_codes(selection.chosen.match.reason_codes)
        if selection.chosen else "",
        "match_warnings": _warnings(selection.chosen),
        "price_notes": ";".join(price_note_codes(chosen.bundle_flag)) if chosen else "",
        "in_stock_price": in_stock.price_text if in_stock else "",
        "in_stock_offer_url": in_stock.offer_url if in_stock else "",
        "in_stock_retrieved_at_utc": utc_text(in_stock.retrieved_at) if in_stock else "",
        "in_stock_observation_id": in_stock.observation_id if in_stock else "",
        "in_stock_price_notes": ";".join(price_note_codes(in_stock.bundle_flag))
        if in_stock else "",
        "latest_exact_offer_url": latest.offer_url if latest else "",
        "latest_exact_availability": latest.availability if latest else "",
        "latest_exact_retrieved_at_utc": utc_text(latest.retrieved_at) if latest else "",
    }
    return {column: row[column] for column in SELECTION_COLUMNS}


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


def _review_reason(view: ObservationView, ranks: Mapping[str, int]) -> str:
    if not view.is_exact:
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
    }
    return {column: row[column] for column in REVIEW_COLUMNS}


def _change_status(delta: Decimal) -> str:
    return "UNCHANGED" if delta == 0 else "INCREASED" if delta > 0 else "DECREASED"


def _group(items, key):
    groups = defaultdict(list)
    for item in items:
        groups[key(item)].append(item)
    return groups
