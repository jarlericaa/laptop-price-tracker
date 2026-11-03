"""Top 100 eligible mapped Shopee variations: a source-level ranking (owner decisions 2026-10-27
and 2026-11-03).

One ranked row is one eligible Shopee seller variation, identified by the Phase 1 ``source_key``
(``shop_id|item_id|model_id``) and its three exact-text identifiers. The ranking metric is the one
Phase 1 established: each variation's own ``ranking_gmv_usd`` (M-1 full calendar month plus MTD as
extracted, never summed across overlapping cumulative snapshots). Phase 1 eligibility, date
interpretation, quarantine and duplicate decisions are inherited unchanged through the eligible
ranking this module reads; that full eligible ranking stays available for audit.

Pool (owner decision 2026-11-03): the eligible variations present in **either** reporting month.
An MTD record is not required; a month with no record is not an observed zero. It contributes 0
to ``ranking_gmv_usd`` only as labelled arithmetic (``mtd_status`` / ``m_minus_1_status`` =
``NO_RECORD_TREATED_AS_ZERO``) and the presence flags are kept on every row. Each row's price is
its own latest valid price with its actual period, record date and basis.

Selection (owner decision 2026-11-03, superseding the 2026-10-27 rule that let unmapped variations
enter the list): the verified-mapping filter runs *before* the 100-row limit. A variation qualifies
only when it is ``MAPPED_VERIFIED``: its source model ID resolves to exactly one ACTIVE, complete,
non-LISTING_ONLY CONFIGURATION target that passes the existing target validator, with no open
target-level or source-level identity conflict. The qualifying pool is ranked by the Phase 1 metric
with its deterministic tie-break and the first ``top_n`` are selected, so a lower-ranked qualifying
variation fills the place of an excluded one; nothing is padded when fewer qualify. MATCH_REVIEW
completion and retailer coverage never gate selection. Several selected variations may share a
target; each stays its own row with its own GMV, seller, listing and price. Contexts (distinct
targets of the selected rows) are what the retailer discovery / observation layer is keyed by, so
sharing variations repeat no fetches. Nothing here infers specifications, promotes candidates,
creates mappings or writes Sheets.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .normalization import CORE_MATCH_COLUMNS, text_key
from .shopee_snapshot import listing_url, price_text, snapshot_amount
from .supporting_tables import TableResult
from .target_master import TargetMasterResult
from .variation_specs import (
    SpecConflict,
    evidence_summary,
    read_variation_specs,
    spec_conflicts,
)
from .verified_top import (
    CONFIGURATION_COLUMNS,
    CONTRIBUTING,
    DEFAULT_TOP_N,
    EXCLUDED,
    LEDGER_COLUMNS,
    RANKING_REQUIRED_COLUMNS,
    RegisterEntry,
    SalesSnapshot,
    SourceRow,
    VerifiedRankingInputError,
    _target_issue,
    build_sales_snapshot,
    money,
    read_csv_rows,
)

SCHEMA_VERSION = "shopee-variation-sheets-1"
RANKING_GRAIN = "SHOPEE_VARIATION"
ROW_GRAIN = "SHOPEE_VARIATION"
# Grain label of the earlier configuration-based outputs (never rewritten to look variation-based).
LEGACY_ROW_GRAIN = "TARGET_CONFIGURATION"
RANK_METRIC = "ranking_gmv_usd"

# Columns the source ranking must provide for this grain (the verified-list set plus the key).
VARIATION_REQUIRED_COLUMNS: tuple[str, ...] = (*RANKING_REQUIRED_COLUMNS, "source_key")

# Mapping statuses. ``MAPPED_<target issue code>`` covers every reason a mapped target is not a
# trusted comparison context (see verified_top._target_issue).
MAPPED_VERIFIED = "MAPPED_VERIFIED"
UNMAPPED = "UNMAPPED"
SOURCE_ID_NOT_UNIQUE = "SOURCE_ID_NOT_UNIQUE"
MAPPED_TARGET_NOT_VALID = "MAPPED_TARGET_NOT_VALID"

# Comparison statuses of a ranked row (listing eligibility is separate from these).
CONTEXT_READY = "COMPARISON_CONTEXT_READY"
REVIEW_PENDING = "CONFIGURATION_REVIEW_PENDING"
SOURCE_CONFLICT_BLOCKED = "SOURCE_CONFLICT_COMPARISON_BLOCKED"
SPEC_CONFLICT_BLOCKED = "VARIATION_SPEC_CONFLICT_COMPARISON_BLOCKED"

# Why a MAPPED_VERIFIED target still blocks a variation: an open source-level identity conflict.
SOURCE_UNRESOLVED_CONFLICT = "SOURCE_UNRESOLVED_CONFLICT"
# The selected variation itself states a value its mapped target contradicts (variation_specs).
VARIATION_SPEC_CONFLICT = "VARIATION_TARGET_SPEC_CONFLICT"

# Selection statuses of an eligible variation (the Top 100 builder applies them, the eligible
# dataset itself is never reduced).
SELECTED = "SELECTED"
QUALIFIED_NOT_SELECTED = "QUALIFIED_NOT_SELECTED"  # verified mapping, ranked below the top N
EXCLUDED_NOT_VERIFIED = "EXCLUDED_NOT_VERIFIED_MAPPED"

# Policy label written to the run summary; the publisher refuses a run built under another one.
SELECTION_POLICY = "ELIGIBLE_MAPPED_VERIFIED_ONLY"
LIST_NAME = "Top 100 eligible mapped Shopee variations"

# Ledger reason codes for the context contribution ledger.
NOT_SELECTED = "OUTSIDE_SELECTED_VARIATIONS"

SALES_DATE_LABEL = "SALES_EXTRACT_DATE"
# The price comes from the M-1 monthly record (the model has no MTD price): dated by that record.
M_MINUS_1_DATE_LABEL = "M_MINUS_1_RECORD_DATE"
MTD_PRICE = "MTD_PRICE"
M_MINUS_1_PRICE = "M_MINUS_1_PRICE"
NO_RECORD_AS_ZERO = "NO_RECORD_TREATED_AS_ZERO"
PRESENT = "PRESENT"
SNAPSHOT_PRICE = "SNAPSHOT_PRICE"

# Columns of the selected / full variation lists. The first block is the TOP_100_SHOPEE_VARIATIONS
# header contract (exact order); the rest is traceability that the sheet does not show.
TOP_VARIATION_CONTRACT_COLUMNS: tuple[str, ...] = (
    "rank",
    "comparison_row_id",
    "source_key",
    "shop_id",
    "shop_name",
    "item_id",
    "source_model_id",
    "item_name",
    "shopee_model_name",
    "brand",
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
    "shopee_current_price_php",
    "shopee_price_observed_at",
    "target_id",
    "mapping_status",
    "comparison_status",
    "sales_extract_date",
    "sales_run_id",
    "m_minus_1_month",
    "mtd_month",
    "rank_metric",
    "rank_metric_scope",
)
VARIATION_COLUMNS: tuple[str, ...] = (
    *TOP_VARIATION_CONTRACT_COLUMNS,
    "series",
    "eligible_rank",
    "selection_status",
    "exclusion_reason",
    "variation_spec_evidence",
    "variation_spec_conflicts",
    "present_in_m_minus_1",
    "present_in_mtd",
    "m_minus_1_status",
    "mtd_status",
    "price_period_month",
    "price_basis",
    "shopee_price_status",
    "shopee_price_detail",
    "shopee_price_date_basis",
    "shopee_listing_url",
    "mapping_detail",
    "comparison_detail",
    "context_id",
    "context_fingerprint",
    "listing_in_selected_top",
    "source_rank",
    "target_master_sha256",
    "schema_version",
    "row_grain",
)

CONTEXT_COLUMNS: tuple[str, ...] = (
    "context_order",
    "context_id",
    "target_id",
    "context_fingerprint",
    "brand",
    "series",
    "model",
    "screen_size_in",
    "cpu_series",
    "cpu_model",
    "ram_capacity_gb",
    "storage_capacity_gb",
    "gpu_model",
    "selected_variation_count",
    "selected_source_keys",
    "best_variation_rank",
    "variation_ranking_gmv_usd",
    "fingerprint_twin_context_ids",
    "schema_version",
)

# Fields whose agreement makes two catalog targets the same comparison context. They are the five
# core fields plus every secondary hardware field a retailer page can distinguish. A blank is
# "unknown" and equals only another blank: unknown data is never proof that two contexts agree.
CONTEXT_IDENTITY_FIELDS: tuple[str, ...] = (
    "brand",
    "model",
    "screen_size_in",
    "cpu_brand",
    "cpu_series",
    "cpu_generation",
    "cpu_model",
    "cpu_cores",
    "ram_capacity_gb",
    "ram_type",
    "storage_capacity_gb",
    "storage_type",
    "gpu_type",
    "gpu_brand",
    "gpu_model",
    "vram",
    "resolution",
    "display_type",
    "refresh_rate_hz",
)

_SOURCE_KEY = re.compile(r"[0-9]+\|[0-9]+\|[0-9]+\Z")


@dataclass
class VariationRanking:
    ranked: list[dict[str, str]]  # every eligible variation, in Phase 1 order (audit dataset)
    top: list[dict[str, str]]  # the first ``top_n`` verified-mapped variations
    exclusions: list[dict[str, str]]  # eligible variations excluded for want of a verified mapping
    spec_conflicts: list[dict[str, str]]  # one row per field where variation and target disagree
    contexts: list[dict[str, str]]
    context_list: list[dict[str, str]]  # CONFIGURATION_COLUMNS rows for the existing pipeline
    context_ledger: list[dict[str, str]]  # LEDGER_COLUMNS rows for the existing pipeline
    counts: dict[str, object] = field(default_factory=dict)


def comparison_row_id(source_key: str) -> str:
    """Stable, human-readable row identity: ``SV-<shop>-<item>-<model>`` (exact text)."""
    return "SV-" + source_key.replace("|", "-")


def rank_metric_scope(snapshot: SalesSnapshot) -> str:
    return (
        f"{RANKING_GRAIN}: each variation's own ranking_gmv_usd = M-1 {snapshot.m_minus_1_month} "
        f"(full calendar month) + MTD {snapshot.mtd_month} as extracted {snapshot.extract_date}; "
        "eligible variations present in either month rank, and a month with no record is not an "
        "observed zero (it adds 0 to the arithmetic only, flagged in m_minus_1_status / "
        f"mtd_status); Phase 1 run {snapshot.run_id}; one Shopee seller variation per row, not "
        "summed across variations, sellers or overlapping cumulative snapshots"
    )


def in_pool(values: Mapping[str, str]) -> bool:
    """Present in either reporting month (an MTD record is not required)."""
    return values.get("present_in_mtd") == "True" or values.get("present_in_m_minus_1") == "True"


def month_statuses(values: Mapping[str, str]) -> tuple[str, str]:
    """``(m_minus_1_status, mtd_status)``; derived from the flags for an older ranking file."""

    def one(status_name: str, flag_name: str) -> str:
        if values.get(status_name):
            return values[status_name]
        return {"True": PRESENT, "False": NO_RECORD_AS_ZERO}.get(values.get(flag_name, ""), "")

    return one("m_minus_1_status", "present_in_m_minus_1"), one("mtd_status", "present_in_mtd")


def own_price_dating(
    values: Mapping[str, str], snapshot: SalesSnapshot
) -> tuple[str, str, str, str]:
    """``(observed_at, date basis, price period month, price basis)`` of a variation's price.

    An MTD price is dated by the sales extract (the established rule). A price that comes from
    the M-1 record is dated by that monthly source record and never relabelled as an observation
    of the extract month; the exact snapshot time within the month is unknown.
    """
    if values.get("price_basis") == M_MINUS_1_PRICE:
        return (
            values.get("price_record_date", ""),
            M_MINUS_1_DATE_LABEL,
            values.get("price_period_month", "") or snapshot.m_minus_1_month,
            M_MINUS_1_PRICE,
        )
    return (
        snapshot.extract_date,
        SALES_DATE_LABEL,
        values.get("price_period_month", "") or snapshot.mtd_month,
        values.get("price_basis", "") or MTD_PRICE,
    )


# Reading and validating the source ranking


def build_variation_snapshot(records: Sequence[Mapping[str, str]]) -> SalesSnapshot:
    """Validate the eligible ranking at source grain.

    The base validation (one run and period pair, sound GMV text, unique ``model_id``) is the
    Phase 1 contract: Phase 1 quarantines every model ID tied to several shop/item identities
    (``find_model_identity_conflicts``), so ``model_id`` is unique here. This grain additionally
    requires a unique ``source_key`` equal to ``shop_id|item_id|model_id``; a violation is an
    input error (the key is what the rows, prices and publisher records are joined on).
    """
    missing = [name for name in VARIATION_REQUIRED_COLUMNS if records and name not in records[0]]
    if missing:
        raise VerifiedRankingInputError(
            f"Eligible ranking is missing columns: {', '.join(missing)}."
        )
    snapshot = build_sales_snapshot(records)
    seen: set[str] = set()
    for row_number, record in enumerate(records, start=2):
        expected = f"{record['shop_id']}|{record['item_id']}|{record['model_id']}"
        key = record["source_key"]
        if key != expected or not _SOURCE_KEY.fullmatch(key):
            raise VerifiedRankingInputError(
                f"Eligible ranking row {row_number} source_key is not shop_id|item_id|model_id."
            )
        if key in seen:
            raise VerifiedRankingInputError(
                f"Eligible ranking repeats source_key at row {row_number}; each variation must "
                "appear once."
            )
        seen.add(key)
    return snapshot


def read_variation_snapshot(path) -> SalesSnapshot:
    return build_variation_snapshot(
        read_csv_rows(path, VARIATION_REQUIRED_COLUMNS, "Eligible ranking")
    )


# Contexts


def context_fingerprint(target: Mapping[str, object]) -> str:
    """Hash of the target's normalized identity including known secondary hardware."""
    parts = [f"{name}={text_key(target.get(name))}" for name in CONTEXT_IDENTITY_FIELDS]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def contexts_compatible(first: Mapping[str, object], second: Mapping[str, object]) -> bool:
    """True only when every identity field agrees exactly (blank equals blank only)."""
    return all(
        text_key(first.get(name)) == text_key(second.get(name)) for name in CONTEXT_IDENTITY_FIELDS
    )


def context_id_for(target_id: str) -> str:
    """The retailer layer is keyed by catalog target, so a context is its target."""
    return target_id


# Ranking


def _target_context_trust(
    link: Mapping[str, object] | None,
    targets: Mapping[str, Mapping[str, object]],
    target_issues: Mapping[str, tuple[str, str] | None],
    ambiguous: bool,
) -> tuple[str, str, str]:
    """``(mapping_status, mapping_detail, target_id)``."""
    if ambiguous:
        return (
            SOURCE_ID_NOT_UNIQUE,
            "The source model ID is shared by several shop/item identities, so a model-ID mapping "
            "cannot be trusted for this variation.",
            "",
        )
    if link is None:
        return UNMAPPED, "No approved SOURCE_MODEL_MAP row.", ""
    target_id = str(link["target_id"])
    if target_id not in targets:
        return (
            MAPPED_TARGET_NOT_VALID,
            "Mapped target_id is not a valid TARGET_MASTER row.",
            target_id,
        )
    if issue := target_issues[target_id]:
        return f"MAPPED_{issue[0]}", issue[1], target_id
    return (
        MAPPED_VERIFIED,
        "Mapped to an ACTIVE CONFIGURATION target with a complete identity.",
        target_id,
    )


def build_variation_ranking(
    snapshot: SalesSnapshot,
    target_master: TargetMasterResult,
    source_model_map: TableResult,
    *,
    source_top_100_ids: Iterable[str] = (),
    register: Sequence[RegisterEntry] = (),
    top_n: int = DEFAULT_TOP_N,
    target_master_sha256: str = "",
    shop_names: Mapping[str, str] | None = None,
) -> VariationRanking:
    if top_n < 1:
        raise VerifiedRankingInputError("top_n must be at least 1.")
    for name, table in (("TARGET_MASTER", target_master), ("SOURCE_MODEL_MAP", source_model_map)):
        if not table.is_valid:
            raise VerifiedRankingInputError(f"{name} did not pass validation.")

    targets = {
        str(row["target_id"]): row for row in target_master.valid_rows.to_dict(orient="records")
    }
    links = {
        str(row["source_model_id"]): row
        for row in source_model_map.valid_rows.to_dict(orient="records")
    }
    open_register = [entry for entry in register if entry.status == "OPEN"]
    target_issues: dict[str, tuple[str, str] | None] = {}
    for target_id, row in targets.items():
        issue = _target_issue(row)
        target_entries = [
            entry
            for entry in open_register
            if entry.target_id == target_id and not entry.source_model_id
        ]
        if issue is None and target_entries:
            issue = (
                "KNOWN_UNRESOLVED_CONFLICT",
                " | ".join(entry.describe() for entry in target_entries),
            )
        target_issues[target_id] = issue
    original = set(source_top_100_ids)
    ids_per_model: dict[str, set[str]] = defaultdict(set)
    for source in snapshot.rows:
        ids_per_model[source.model_id].add(source.values["source_key"])
    names = shop_names or {}
    sha = target_master_sha256
    scope = rank_metric_scope(snapshot)

    def source_order(source: SourceRow) -> tuple:
        return (-source.ranking_gmv, -source.mtd_gmv, source.model_id)

    eligible: list[SourceRow] = []
    ineligible: Counter[str] = Counter()
    for source in snapshot.rows:
        values = source.values
        if values["eligibility_status"] != "ELIGIBLE" or not in_pool(values):
            ineligible["SOURCE_NOT_ELIGIBLE"] += 1
            continue
        eligible.append(source)
    eligible.sort(key=source_order)

    ranked: list[dict[str, str]] = []
    spec_rows: list[dict[str, str]] = []
    for eligible_rank, source in enumerate(eligible, start=1):
        values = source.values
        key = values["source_key"]
        link = links.get(source.model_id)
        status, detail, target_id = _target_context_trust(
            link, targets, target_issues, len(ids_per_model[source.model_id]) > 1
        )
        conflicts = [e for e in open_register if e.source_model_id == source.model_id]
        readings = read_variation_specs(values["item_name"], values["shopee_model_name"])
        # A disagreement is evidence for any row with a resolvable target; it excludes a row only
        # when nothing else already has (the reason precedence is in _exclusion_reason).
        spec_found = spec_conflicts(readings, targets[target_id]) if target_id in targets else []
        if status == MAPPED_VERIFIED and conflicts:
            comparison = SOURCE_CONFLICT_BLOCKED
            comparison_detail = (
                "Open source-level identity conflict (not selected; comparison blocked): "
                + " | ".join(entry.describe() for entry in conflicts)
            )
        elif spec_found and status == MAPPED_VERIFIED:
            comparison = SPEC_CONFLICT_BLOCKED
            comparison_detail = (
                f"The selected variation disagrees with its mapped target {target_id} (not "
                "selected; comparison blocked): " + " | ".join(c.describe() for c in spec_found)
            )
        elif status == MAPPED_VERIFIED:
            comparison, comparison_detail = CONTEXT_READY, "Trusted comparison context."
        else:
            comparison = REVIEW_PENDING
            comparison_detail = "Configuration review pending: " + detail
            if conflicts:
                comparison_detail += " Open source conflict: " + " | ".join(
                    entry.describe() for entry in conflicts
                )
        if spec_found and comparison != SPEC_CONFLICT_BLOCKED:
            comparison_detail += (
                " | The selected variation also disagrees with its mapped target "
                f"{target_id}: " + " | ".join(c.describe() for c in spec_found)
            )
        for found in spec_found:
            spec_rows.append(
                {
                    "eligible_rank": str(eligible_rank),
                    "comparison_row_id": comparison_row_id(key),
                    "source_key": key,
                    "source_model_id": source.model_id,
                    "target_id": target_id,
                    "field": found.field,
                    "variation_value": found.variation_value,
                    "target_value": found.target_value,
                    "reading_source": found.source,
                    "evidence": found.evidence,
                    "item_name": values["item_name"],
                    "shopee_model_name": values["shopee_model_name"],
                    "ranking_gmv_usd": values["ranking_gmv_usd"],
                }
            )
        trusted = comparison == CONTEXT_READY
        price_dating = own_price_dating(values, snapshot)
        target = targets[target_id] if trusted else {}
        amount, reason = snapshot_amount(
            {
                "shopee_price_status": values.get("price_status", ""),
                "shopee_price_php_snapshot": values.get("shopee_current_price_php", ""),
            }
        )
        ranked.append(
            {
                "rank": "",  # Assigned below, within the verified-mapped pool only.
                "comparison_row_id": comparison_row_id(key),
                "source_key": key,
                "shop_id": values["shop_id"],
                "shop_name": names.get(key, values.get("shop_name", "")),
                "item_id": values["item_id"],
                "source_model_id": source.model_id,
                "item_name": values["item_name"],
                "shopee_model_name": values["shopee_model_name"],
                "brand": str(target.get("brand") or ""),
                "model": str(target.get("model") or ""),
                "screen_size_in": str(target.get("screen_size_in") or ""),
                "cpu_series": str(target.get("cpu_series") or ""),
                "cpu_model": str(target.get("cpu_model") or ""),
                "ram_capacity_gb": str(target.get("ram_capacity_gb") or ""),
                "storage_capacity_gb": str(target.get("storage_capacity_gb") or ""),
                "gpu_model": str(target.get("gpu_model") or ""),
                "m_minus_1_gmv_usd": values["m_minus_1_gmv_usd"],
                "mtd_gmv_usd": values["mtd_gmv_usd"],
                "ranking_gmv_usd": values["ranking_gmv_usd"],
                "shopee_current_price_php": price_text(amount) if amount is not None else "",
                "shopee_price_observed_at": price_dating[0],
                "target_id": target_id,
                "mapping_status": status,
                "comparison_status": comparison,
                "sales_extract_date": snapshot.extract_date,
                "sales_run_id": snapshot.run_id,
                "m_minus_1_month": snapshot.m_minus_1_month,
                "mtd_month": snapshot.mtd_month,
                "rank_metric": RANK_METRIC,
                "rank_metric_scope": scope,
                "series": str(target.get("series") or ""),
                "eligible_rank": str(eligible_rank),
                "selection_status": SELECTED if trusted else EXCLUDED_NOT_VERIFIED,
                "exclusion_reason": (
                    "" if trusted else _exclusion_reason(status, conflicts, spec_found)
                ),
                "variation_spec_evidence": evidence_summary(readings),
                "variation_spec_conflicts": " | ".join(c.describe() for c in spec_found),
                "shopee_price_status": SNAPSHOT_PRICE if amount is not None else reason,
                "shopee_price_detail": (
                    (
                        "own variation's snapshot price from the sales extract"
                        if price_dating[1] == SALES_DATE_LABEL
                        else f"own variation's price from its M-1 {price_dating[2]} source record "
                        "(no MTD price); dated by that monthly record, not an observation of the "
                        "extract month"
                    )
                    if amount is not None
                    else f"no usable own-variation price ({reason}); "
                    f"MTD price values: {values.get('mtd_price_values', '') or 'none'}; "
                    f"M-1 price values: {values.get('m_minus_1_price_values', '') or 'none'}"
                ),
                "shopee_price_date_basis": price_dating[1],
                "present_in_m_minus_1": values.get("present_in_m_minus_1", ""),
                "present_in_mtd": values.get("present_in_mtd", ""),
                "m_minus_1_status": month_statuses(values)[0],
                "mtd_status": month_statuses(values)[1],
                "price_period_month": price_dating[2],
                "price_basis": price_dating[3],
                "shopee_listing_url": listing_url(values["shop_id"], values["item_id"]),
                "mapping_detail": detail,
                "comparison_detail": comparison_detail,
                "context_id": context_id_for(target_id) if trusted else "",
                "context_fingerprint": context_fingerprint(target) if trusted else "",
                "listing_in_selected_top": "False",  # Set below with the selection.
                "source_rank": values["rank"],
                "target_master_sha256": sha,
                "schema_version": SCHEMA_VERSION,
                "row_grain": ROW_GRAIN,
            }
        )
    # The verified-mapping filter comes first; the top_n limit applies to the qualifying pool.
    qualifying = [row for row in ranked if not row["exclusion_reason"]]
    for position, row in enumerate(qualifying, start=1):
        row["rank"] = str(position)
        if position > top_n:
            row["selection_status"] = QUALIFIED_NOT_SELECTED
        row["listing_in_selected_top"] = str(position <= top_n)
    top = qualifying[:top_n]
    contexts, context_list, context_ledger = _build_contexts(
        snapshot, targets, ranked, top, links, original, sha
    )
    exclusions = _exclusion_rows(ranked)
    twin_count = sum(1 for context in contexts if context["fingerprint_twin_context_ids"])
    mapping_counts = Counter(row["mapping_status"] for row in top)
    comparison_counts = Counter(row["comparison_status"] for row in top)
    excluded_by_reason = Counter(row["exclusion_reason"] for row in exclusions)
    priced = [row for row in top if row["shopee_price_status"] == SNAPSHOT_PRICE]
    shortfall = max(0, top_n - len(qualifying))
    counts: dict[str, object] = {
        "selection_policy": SELECTION_POLICY,
        "eligible_variations": len(ranked),
        **_month_counts(ranked, qualifying, top),
        "eligible_variations_with_verified_mapping": len(qualifying),
        "excluded_from_top_consideration": len(exclusions),
        "excluded_by_mapping_failure": dict(sorted(excluded_by_reason.items())),
        "variation_spec_conflict_rows": len({r["source_key"] for r in spec_rows}),
        "qualified_not_selected": len(qualifying) - len(top),
        "ineligible_source_rows": sum(ineligible.values()),
        "requested_top_n": top_n,
        "selected_variations": len(top),
        "shortfall": shortfall,
        "shortfall_reason": (
            ""
            if not shortfall
            else f"only {len(qualifying)} of {len(ranked)} eligible variations have a verified "
            f"mapping ({len(exclusions)} excluded: "
            + ", ".join(f"{code} {n}" for code, n in sorted(excluded_by_reason.items()))
            + "); none is padded"
        ),
        "selected_with_lower_eligible_rank_than_top_n": sum(
            1 for r in top if int(r["eligible_rank"]) > top_n
        ),
        "distinct_configuration_contexts": len(contexts),
        "mapped_selected_variations": sum(1 for r in top if r["mapping_status"] == MAPPED_VERIFIED),
        "unmapped_selected_variations": mapping_counts[UNMAPPED],
        "selected_with_context": sum(1 for r in top if r["context_id"]),
        "selected_without_context": sum(1 for r in top if not r["context_id"]),
        "selected_with_own_price": len(priced),
        "selected_without_own_price": len(top) - len(priced),
        "selected_in_original_source_top_100": sum(
            1 for r in top if r["source_model_id"] in original
        ),
        "fingerprint_twin_contexts": twin_count,
        "selected_by_mapping_status": dict(sorted(mapping_counts.items())),
        "selected_by_comparison_status": dict(sorted(comparison_counts.items())),
        "variations_in_shared_contexts": sum(
            int(c["selected_variation_count"])
            for c in contexts
            if int(c["selected_variation_count"]) > 1
        ),
        "contexts_with_several_variations": sum(
            1 for c in contexts if int(c["selected_variation_count"]) > 1
        ),
    }
    return VariationRanking(
        ranked, top, exclusions, spec_rows, contexts, context_list, context_ledger, counts
    )


def _month_counts(
    ranked: Sequence[Mapping[str, str]],
    qualifying: Sequence[Mapping[str, str]],
    top: Sequence[Mapping[str, str]],
) -> dict[str, int]:
    """Eligible, qualifying and selected variations by reporting month and their union."""

    def tally(rows: Sequence[Mapping[str, str]], prefix: str) -> dict[str, int]:
        in_m1 = [r for r in rows if r["present_in_m_minus_1"] == "True"]
        in_mtd = [r for r in rows if r["present_in_mtd"] == "True"]
        both = [r for r in in_m1 if r["present_in_mtd"] == "True"]
        return {
            f"{prefix}_in_m_minus_1": len(in_m1),
            f"{prefix}_in_mtd": len(in_mtd),
            f"{prefix}_in_both_months": len(both),
            f"{prefix}_m_minus_1_only": len(in_m1) - len(both),
            f"{prefix}_mtd_only": len(in_mtd) - len(both),
            f"{prefix}_union": len(rows),
        }

    return {
        **tally(ranked, "eligible"),
        **tally(qualifying, "qualifying"),
        **tally(top, "selected"),
        "selected_with_m_minus_1_price": sum(1 for r in top if r["price_basis"] == M_MINUS_1_PRICE),
    }


def _exclusion_reason(
    status: str, conflicts: Sequence[RegisterEntry], spec_found: Sequence[SpecConflict] = ()
) -> str:
    """Why a variation is not ``MAPPED_VERIFIED`` for selection, or "" when it qualifies."""
    if status != MAPPED_VERIFIED:
        return status
    if conflicts:
        return SOURCE_UNRESOLVED_CONFLICT
    return VARIATION_SPEC_CONFLICT if spec_found else ""


def _build_contexts(
    snapshot: SalesSnapshot,
    targets: Mapping[str, Mapping[str, object]],
    ranked: Sequence[Mapping[str, str]],
    top: Sequence[Mapping[str, str]],
    links: Mapping[str, Mapping[str, object]],
    original: set[str],
    target_master_sha256: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    members: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in top:
        if row["context_id"]:
            members[row["context_id"]].append(row)
    unselected_mapped = Counter(
        row["target_id"]
        for row in ranked
        if row["target_id"] and row["listing_in_selected_top"] != "True"
    )
    # Context order is the best (lowest) variation rank among its selected rows: a presentation
    # order for the retailer layer, never a variation rank.
    order = sorted(members, key=lambda cid: int(members[cid][0]["rank"]))
    fingerprints = {cid: members[cid][0]["context_fingerprint"] for cid in order}
    by_fingerprint: dict[str, list[str]] = defaultdict(list)
    for cid, fingerprint in fingerprints.items():
        by_fingerprint[fingerprint].append(cid)
    contexts, context_list = [], []
    for context_order, cid in enumerate(order, start=1):
        rows = members[cid]
        target = targets[cid]
        m1 = sum((Decimal(r["m_minus_1_gmv_usd"]) for r in rows), Decimal(0))
        mtd = sum((Decimal(r["mtd_gmv_usd"]) for r in rows), Decimal(0))
        ordered = sorted(rows, key=lambda r: int(r["rank"]))
        twins = [other for other in by_fingerprint[fingerprints[cid]] if other != cid]
        contexts.append(
            {
                "context_order": str(context_order),
                "context_id": cid,
                "target_id": cid,
                "context_fingerprint": fingerprints[cid],
                "brand": str(target.get("brand") or ""),
                "series": str(target.get("series") or ""),
                "model": str(target.get("model") or ""),
                "screen_size_in": str(target.get("screen_size_in") or ""),
                "cpu_series": str(target.get("cpu_series") or ""),
                "cpu_model": str(target.get("cpu_model") or ""),
                "ram_capacity_gb": str(target.get("ram_capacity_gb") or ""),
                "storage_capacity_gb": str(target.get("storage_capacity_gb") or ""),
                "gpu_model": str(target.get("gpu_model") or ""),
                "selected_variation_count": str(len(rows)),
                "selected_source_keys": ";".join(r["source_key"] for r in ordered),
                "best_variation_rank": ordered[0]["rank"],
                "variation_ranking_gmv_usd": money(m1 + mtd),
                "fingerprint_twin_context_ids": ";".join(twins),
                "schema_version": SCHEMA_VERSION,
            }
        )
        context_list.append(
            {
                "rank": str(context_order),
                "target_id": cid,
                "brand": str(target.get("brand") or ""),
                "series": str(target.get("series") or ""),
                **{name: str(target[name]) for name in CORE_MATCH_COLUMNS},
                "m_minus_1_gmv_usd": money(m1),
                "mtd_gmv_usd": money(mtd),
                "ranking_gmv_usd": money(m1 + mtd),
                "contributing_source_count": str(len(rows)),
                "contributing_source_model_ids": ";".join(r["source_model_id"] for r in ordered),
                "original_source_top_100_contributors": str(
                    sum(r["source_model_id"] in original for r in rows)
                ),
                "excluded_mapped_source_count": str(unselected_mapped[cid]),
                "sales_run_id": snapshot.run_id,
                "sales_extract_date": snapshot.extract_date,
                "sales_m_minus_1_month": snapshot.m_minus_1_month,
                "sales_mtd_month": snapshot.mtd_month,
                "target_master_sha256": target_master_sha256,
            }
        )
    context_list = [{name: row[name] for name in CONFIGURATION_COLUMNS} for row in context_list]
    selected_keys = {r["source_key"] for r in top if r["context_id"]}
    ledger = []
    for row in ranked:
        contributing = row["source_key"] in selected_keys
        if contributing:
            code, detail = "", ""
        elif row["selection_status"] == QUALIFIED_NOT_SELECTED:
            code, detail = NOT_SELECTED, "Verified mapping, ranked below the selected variations."
        else:
            code, detail = row["exclusion_reason"], row["mapping_detail"]
        ledger.append(
            {
                "source_rank": row["eligible_rank"],
                "model_id": row["source_model_id"],
                "shop_id": row["shop_id"],
                "item_id": row["item_id"],
                "item_name": row["item_name"],
                "shopee_model_name": row["shopee_model_name"],
                "m_minus_1_gmv_usd": row["m_minus_1_gmv_usd"],
                "mtd_gmv_usd": row["mtd_gmv_usd"],
                "ranking_gmv_usd": row["ranking_gmv_usd"],
                "in_original_source_top_100": str(row["source_model_id"] in original),
                "target_id": row["target_id"],
                "contribution_status": CONTRIBUTING if contributing else EXCLUDED,
                "reason_code": code,
                "reason_detail": detail,
            }
        )
    ledger = [{name: row[name] for name in LEDGER_COLUMNS} for row in ledger]
    return contexts, context_list, ledger


def _exclusion_rows(ranked: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    """Eligible variations outside Top 100 consideration, in eligible order, for catalog work."""
    actions = {
        UNMAPPED: "Research the configuration identity; add it to TARGET_MASTER / SOURCE_MODEL_MAP "
        "through the catalog review (nothing is mapped automatically).",
        SOURCE_ID_NOT_UNIQUE: "Resolve the conflicting shop/item identities for this source ID.",
        MAPPED_TARGET_NOT_VALID: "Fix the mapped target_id so it names a valid TARGET_MASTER row.",
        SOURCE_UNRESOLVED_CONFLICT: "Resolve the open identity-register conflict for this source.",
        VARIATION_SPEC_CONFLICT: "Check the variation against its target: correct the mapping or "
        "the target (catalog review); nothing is changed automatically.",
    }
    rows = []
    for row in ranked:
        reason = row["exclusion_reason"]
        if not reason:
            continue
        rows.append(
            {
                "eligible_rank": row["eligible_rank"],
                "comparison_row_id": row["comparison_row_id"],
                "source_key": row["source_key"],
                "shop_name": row["shop_name"],
                "item_name": row["item_name"],
                "shopee_model_name": row["shopee_model_name"],
                "ranking_gmv_usd": row["ranking_gmv_usd"],
                "target_id": row["target_id"],
                "mapping_status": row["mapping_status"],
                "exclusion_reason": reason,
                "detail": row["comparison_detail"],
                "suggested_action": actions.get(
                    reason,
                    "Review the mapped target or the recorded conflict; the variation stays out "
                    "of the Top 100 until it is resolved.",
                ),
            }
        )
    return rows


SPEC_CONFLICT_COLUMNS: tuple[str, ...] = (
    "eligible_rank",
    "comparison_row_id",
    "source_key",
    "source_model_id",
    "target_id",
    "field",
    "variation_value",
    "target_value",
    "reading_source",
    "evidence",
    "item_name",
    "shopee_model_name",
    "ranking_gmv_usd",
)

EXCLUSION_COLUMNS: tuple[str, ...] = (
    "eligible_rank",
    "comparison_row_id",
    "source_key",
    "shop_name",
    "item_name",
    "shopee_model_name",
    "ranking_gmv_usd",
    "target_id",
    "mapping_status",
    "exclusion_reason",
    "detail",
    "suggested_action",
)


# Audit


def audit_variation_ranking(
    ranked: Sequence[Mapping[str, str]],
    top: Sequence[Mapping[str, str]],
    context_list: Sequence[Mapping[str, str]],
    context_ledger: Sequence[Mapping[str, str]],
    snapshot: SalesSnapshot,
    target_master: TargetMasterResult,
    source_model_map: TableResult,
    *,
    top_n: int = DEFAULT_TOP_N,
    register: Sequence[RegisterEntry] = (),
) -> list[str]:
    """Independent checks of the written outputs; an empty list means the audit passed.

    Qualification is recomputed here from the catalog tables and the register, never read back from
    the written ``exclusion_reason`` cells, and the selection is checked to be the first ``top_n``
    of that independently computed pool (the filter runs before the limit).
    """
    issues: list[str] = []
    sources = {row.values["source_key"]: row for row in snapshot.rows}
    targets = {
        str(row["target_id"]): row for row in target_master.valid_rows.to_dict(orient="records")
    }
    links = {
        str(row["source_model_id"]): str(row["target_id"])
        for row in source_model_map.valid_rows.to_dict(orient="records")
    }
    eligible = {
        key
        for key, row in sources.items()
        if row.values["eligibility_status"] == "ELIGIBLE" and in_pool(row.values)
    }
    open_register = [entry for entry in register if entry.status == "OPEN"]
    keys_per_model: dict[str, set[str]] = defaultdict(set)
    sources_by_model: dict[str, SourceRow] = {}
    for key, source in sources.items():
        keys_per_model[source.model_id].add(key)
        sources_by_model[source.model_id] = source

    def expected_reason(model_id: str) -> str:
        if len(keys_per_model[model_id]) > 1:
            return SOURCE_ID_NOT_UNIQUE
        target_id = links.get(model_id)
        if target_id is None:
            return UNMAPPED
        if target_id not in targets:
            return MAPPED_TARGET_NOT_VALID
        if issue := _target_issue(targets[target_id]):
            return f"MAPPED_{issue[0]}"
        if any(e.target_id == target_id and not e.source_model_id for e in open_register):
            return "MAPPED_KNOWN_UNRESOLVED_CONFLICT"
        if any(e.source_model_id == model_id for e in open_register):
            return SOURCE_UNRESOLVED_CONFLICT
        own = sources_by_model[model_id].values
        readings = read_variation_specs(own["item_name"], own["shopee_model_name"])
        if spec_conflicts(readings, targets[target_id]):
            return VARIATION_SPEC_CONFLICT
        return ""

    keys = [row["source_key"] for row in ranked]
    if len(keys) != len(set(keys)):
        issues.append("Variation ranking repeats a source_key.")
    if len({row["comparison_row_id"] for row in ranked}) != len(ranked):
        issues.append("Variation ranking repeats a comparison_row_id.")
    if set(keys) != eligible:
        issues.append("Variation ranking does not cover exactly the eligible source variations.")
    if [row["eligible_rank"] for row in ranked] != [str(n) for n in range(1, len(ranked) + 1)]:
        issues.append("Eligible ranks are not 1..n in order.")

    order_keys = []
    qualifying: list[Mapping[str, str]] = []
    for row in ranked:
        source = sources.get(row["source_key"])
        if source is None:
            issues.append(f"{row['source_key']} is not in the sales ranking.")
            continue
        values = source.values
        if row["comparison_row_id"] != comparison_row_id(row["source_key"]):
            issues.append(f"{row['source_key']} comparison_row_id is wrong.")
        for name, source_name in (
            ("shop_id", "shop_id"),
            ("item_id", "item_id"),
            ("source_model_id", "model_id"),
            ("item_name", "item_name"),
            ("shopee_model_name", "shopee_model_name"),
            ("m_minus_1_gmv_usd", "m_minus_1_gmv_usd"),
            ("mtd_gmv_usd", "mtd_gmv_usd"),
            ("ranking_gmv_usd", "ranking_gmv_usd"),
        ):
            if row[name] != values[source_name]:
                issues.append(f"{row['source_key']} {name} differs from its own sales row.")
        amount, _ = snapshot_amount(
            {
                "shopee_price_status": values.get("price_status", ""),
                "shopee_price_php_snapshot": values.get("shopee_current_price_php", ""),
            }
        )
        expected_price = price_text(amount) if amount is not None else ""
        if row["shopee_current_price_php"] != expected_price:
            issues.append(f"{row['source_key']} price is not its own variation's price.")
        expected_date, expected_basis, _, _ = own_price_dating(values, snapshot)
        if row["shopee_price_observed_at"] != expected_date:
            issues.append(f"{row['source_key']} price date is not its own price record's date.")
        if row["shopee_price_date_basis"] != expected_basis:
            issues.append(f"{row['source_key']} price date basis is wrong.")
        for name, expected in (
            ("sales_run_id", snapshot.run_id),
            ("sales_extract_date", snapshot.extract_date),
            ("m_minus_1_month", snapshot.m_minus_1_month),
            ("mtd_month", snapshot.mtd_month),
            ("rank_metric", RANK_METRIC),
            ("rank_metric_scope", rank_metric_scope(snapshot)),
        ):
            if row[name] != expected:
                issues.append(f"{row['source_key']} {name} is wrong.")
        order_keys.append((-source.ranking_gmv, -source.mtd_gmv, source.model_id))

        reason = expected_reason(source.model_id)
        link_target = links.get(row["source_model_id"], "")
        if row["exclusion_reason"] != reason:
            issues.append(
                f"{row['source_key']} exclusion_reason {row['exclusion_reason']!r} differs from "
                f"the recomputed mapping check {reason!r}."
            )
        if row["mapping_status"] == UNMAPPED and link_target:
            issues.append(f"{row['source_key']} is marked unmapped but SOURCE_MODEL_MAP maps it.")
        if reason:
            if row["rank"] or row["context_id"] or row["selection_status"] != EXCLUDED_NOT_VERIFIED:
                issues.append(f"{row['source_key']} is not verified-mapped but is ranked/selected.")
            continue
        qualifying.append(row)
        if row["target_id"] != link_target or row["context_id"] != link_target:
            issues.append(f"{row['source_key']} context does not match SOURCE_MODEL_MAP.")
        if row["mapping_status"] != MAPPED_VERIFIED or row["comparison_status"] != CONTEXT_READY:
            issues.append(f"{row['source_key']} qualifies but is not MAPPED_VERIFIED / ready.")
    if order_keys != sorted(order_keys):
        issues.append("Variation ranking is not in deterministic order.")

    # Filter before limit: the selection is exactly the first top_n of the qualifying pool.
    if [row["rank"] for row in qualifying] != [str(n) for n in range(1, len(qualifying) + 1)]:
        issues.append("Verified-mapped variations are not ranked 1..n in order.")
    if [dict(row) for row in top] != [dict(row) for row in qualifying[:top_n]]:
        issues.append(
            f"Selected list is not the first {top_n} verified-mapped variations of the "
            "eligible ranking."
        )
    if len(top) != min(top_n, len(qualifying)):
        issues.append("Selected variation count is neither top_n nor every verified variation.")
    for position, row in enumerate(qualifying, start=1):
        wanted = SELECTED if position <= top_n else QUALIFIED_NOT_SELECTED
        if row["selection_status"] != wanted or row["listing_in_selected_top"] != str(
            position <= top_n
        ):
            issues.append(f"{row['source_key']} selection_status is not {wanted}.")

    # Context layer: distinct trusted targets of the selected rows, with exact contributors.
    selected_with_context: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in top:
        if row["context_id"]:
            selected_with_context[row["context_id"]].append(row)
    listed = [row["target_id"] for row in context_list]
    if len(listed) != len(set(listed)):
        issues.append("Context list repeats a target_id.")
    if set(listed) != set(selected_with_context):
        issues.append("Context list differs from the selected variations' contexts.")
    if [row["rank"] for row in context_list] != [str(n) for n in range(1, len(listed) + 1)]:
        issues.append("Context order is not 1..n in order.")
    for row in context_list:
        members = selected_with_context.get(row["target_id"], [])
        m1 = sum((Decimal(m["m_minus_1_gmv_usd"]) for m in members), Decimal(0))
        mtd = sum((Decimal(m["mtd_gmv_usd"]) for m in members), Decimal(0))
        if (row["m_minus_1_gmv_usd"], row["mtd_gmv_usd"], row["ranking_gmv_usd"]) != (
            money(m1),
            money(mtd),
            money(m1 + mtd),
        ):
            issues.append(f"Context {row['target_id']} GMV differs from its selected variations.")
        if set(filter(None, row["contributing_source_model_ids"].split(";"))) != {
            m["source_model_id"] for m in members
        }:
            issues.append(f"Context {row['target_id']} contributors are wrong.")
    contributing = {
        r["model_id"] for r in context_ledger if r["contribution_status"] == CONTRIBUTING
    }
    if contributing != {r["source_model_id"] for r in top if r["context_id"]}:
        issues.append("Context ledger contributors differ from the selected variations.")
    if len({r["model_id"] for r in context_ledger}) != len(context_ledger):
        issues.append("Context ledger repeats a model_id.")
    return issues


# Output files

OUTPUT_FILES = {
    "ranking": "shopee_variation_ranking.csv",
    "top": "top_100_shopee_variations.csv",
    "exclusions": "mapping_exclusions.csv",
    "spec_conflicts": "variation_spec_conflicts.csv",
    "contexts": "variation_contexts.csv",
    "context_list": "context_selected_list.csv",
    "context_ledger": "context_contributions.csv",
    "context_scope": "context_scope.csv",
    "summary": "shopee_variation_run_summary.json",
}


def write_variation_outputs(output_dir, result: VariationRanking, summary: Mapping[str, object]):
    """Write the full eligible ranking, selected list, exclusions, contexts, the context
    list/ledger/scope in the formats ``discover_products.py`` and ``track_prices.py`` read, and
    the run summary."""
    import json
    from pathlib import Path

    from .verified_top import write_csv

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {key: output_dir / name for key, name in OUTPUT_FILES.items()}
    write_csv(paths["ranking"], VARIATION_COLUMNS, result.ranked)
    write_csv(paths["top"], VARIATION_COLUMNS, result.top)
    write_csv(paths["exclusions"], EXCLUSION_COLUMNS, result.exclusions)
    write_csv(paths["spec_conflicts"], SPEC_CONFLICT_COLUMNS, result.spec_conflicts)
    write_csv(paths["contexts"], CONTEXT_COLUMNS, result.contexts)
    write_csv(paths["context_list"], CONFIGURATION_COLUMNS, result.context_list)
    write_csv(paths["context_ledger"], LEDGER_COLUMNS, result.context_ledger)
    write_csv(paths["context_scope"], CONFIGURATION_COLUMNS, result.context_list)
    paths["summary"].write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return paths
