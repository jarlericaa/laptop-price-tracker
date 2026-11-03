"""Top 100 eligible mapped Shopee variations (owner decisions 2026-10-27 and 2026-11-03).

Synthetic data only. One ranked row is one eligible Shopee seller variation (``source_key``). Only
variations with a verified mapping qualify, that filter runs before the 100-row limit, and the
full eligible ranking stays available; contexts are built after the selection.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_supporting_tables import map_row
from test_target_master import target_values
from test_verified_top import target

from laptop_tracker.price_tracking import validate_selected_list
from laptop_tracker.supporting_tables import SOURCE_MODEL_MAP_HEADERS, validate_source_model_map
from laptop_tracker.target_master import validate_target_master
from laptop_tracker.variation_ranking import (
    CONTEXT_READY,
    EXCLUDED_NOT_VERIFIED,
    OUTPUT_FILES,
    QUALIFIED_NOT_SELECTED,
    ROW_GRAIN,
    SCHEMA_VERSION,
    SELECTED,
    SELECTION_POLICY,
    SOURCE_CONFLICT_BLOCKED,
    SOURCE_ID_NOT_UNIQUE,
    SOURCE_UNRESOLVED_CONFLICT,
    SPEC_CONFLICT_BLOCKED,
    UNMAPPED,
    VARIATION_SPEC_CONFLICT,
    audit_variation_ranking,
    build_variation_ranking,
    build_variation_snapshot,
    comparison_row_id,
    context_fingerprint,
    contexts_compatible,
    write_variation_outputs,
)
from laptop_tracker.verified_top import (
    CONTRIBUTING,
    RegisterEntry,
    SourceRow,
    VerifiedRankingInputError,
    read_csv_rows,
)

pytestmark = pytest.mark.usefixtures("no_network")


def vs(
    model_id: str, m1: str, mtd: str, *, shop: str = "10", item: str = "20", price: str = "1000.0",
    **overrides: str,
) -> dict[str, str]:
    """One row of the Phase 1 eligible ranking (with its source_key and price columns)."""
    return {
        "rank": "1",
        "run_id": "20261027T090000+0800",
        "extract_date": "2026-10-27",
        "m_minus_1_month": "2026-09",
        "mtd_month": "2026-10",
        "source_key": f"{shop}|{item}|{model_id}",
        "shop_id": shop,
        "item_id": item,
        "model_id": model_id,
        "item_name": f"Synthetic listing {item}",
        "shopee_model_name": f"Variation {model_id}",
        "m_minus_1_gmv_usd": m1,
        "mtd_gmv_usd": mtd,
        "ranking_gmv_usd": str(Decimal(m1) + Decimal(mtd)),
        "shopee_current_price_php": price,
        "eligibility_status": "ELIGIBLE",
        "present_in_mtd": "True",
        "price_status": "PRICE_OK" if price else "PRICE_MISSING",
        "mtd_price_values": price,
    } | overrides


def build(sources, targets, links, **kwargs):
    target_master = validate_target_master(target_values(*targets))
    mapping = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, *(map_row(source_model_id=s, target_id=t) for s, t in links)],
        target_master,
    )
    assert target_master.is_valid and mapping.is_valid
    snapshot = build_variation_snapshot(sources)
    result = build_variation_ranking(snapshot, target_master, mapping, **kwargs)
    return result, snapshot, target_master, mapping


def row_of(result, model_id: str) -> dict[str, str]:
    return next(r for r in result.ranked if r["source_model_id"] == model_id)


# 1-2. One row per variation; shared and repeated target IDs never collapse report rows


def test_two_sellers_sharing_a_target_are_two_rows_with_their_own_gmv_and_price() -> None:
    result, *_ = build(
        [
            vs("101", "100", "50", shop="10", item="20", price="48990.0"),
            vs("201", "70", "30", shop="11", item="21", price="45990.0"),
        ],
        [target("TGT-000001")],
        [("101", "TGT-000001"), ("201", "TGT-000001")],
    )

    first, second = result.top
    assert (first["shop_id"], first["item_id"], first["source_model_id"]) == ("10", "20", "101")
    assert (second["shop_id"], second["item_id"], second["source_model_id"]) == ("11", "21", "201")
    assert (first["ranking_gmv_usd"], second["ranking_gmv_usd"]) == ("150", "100")
    assert (first["shopee_current_price_php"], second["shopee_current_price_php"]) == (
        "48990.00",
        "45990.00",
    )
    assert first["target_id"] == second["target_id"] == "TGT-000001"
    assert first["comparison_row_id"] != second["comparison_row_id"]
    # One configuration context behind both rows.
    assert [c["context_id"] for c in result.contexts] == ["TGT-000001"]
    assert result.contexts[0]["selected_variation_count"] == "2"
    assert result.counts["distinct_configuration_contexts"] == 1
    assert result.counts["selected_variations"] == 2


def test_repeated_target_ids_do_not_collapse_rows_and_ranks_follow_own_gmv() -> None:
    # The target totals would rank TGT-1 (60+60) above TGT-2 (100); variations rank on their own.
    result, *_ = build(
        [
            vs("101", "60", "0", item="20"),
            vs("102", "60", "0", item="21"),
            vs("201", "100", "0", item="22"),
        ],
        [target("TGT-000001"), target("TGT-000002", model="ExampleBook 16")],
        [("101", "TGT-000001"), ("102", "TGT-000001"), ("201", "TGT-000002")],
    )

    assert [r["source_model_id"] for r in result.ranked] == ["201", "101", "102"]
    assert [r["rank"] for r in result.ranked] == ["1", "2", "3"]
    assert [r["target_id"] for r in result.ranked] == ["TGT-000002", "TGT-000001", "TGT-000001"]
    assert len({r["comparison_row_id"] for r in result.ranked}) == 3
    assert len({r["source_key"] for r in result.ranked}) == 3


# 3. The verified-mapping filter runs before the 100-row limit


def test_an_unmapped_high_ranked_variation_is_skipped_and_a_lower_one_takes_its_place() -> None:
    result, snapshot, target_master, mapping = build(
        [
            vs("101", "900", "0", item="1"),  # highest GMV but unmapped
            vs("102", "800", "0", item="2"),
            vs("103", "700", "0", item="3"),
            vs("104", "600", "0", item="4"),  # beyond the first 3 eligible, fills the freed place
        ],
        [target("TGT-000001"), target("TGT-000002", model="ExampleBook 16")],
        [("102", "TGT-000001"), ("103", "TGT-000001"), ("104", "TGT-000002")],
        top_n=3,
    )

    # Not "the first 3 eligible minus the unmapped one": the pool is filtered first.
    assert [r["source_model_id"] for r in result.top] == ["102", "103", "104"]
    assert [r["rank"] for r in result.top] == ["1", "2", "3"]
    assert [r["eligible_rank"] for r in result.top] == ["2", "3", "4"]
    skipped = row_of(result, "101")
    assert skipped["rank"] == "" and skipped["eligible_rank"] == "1"
    assert skipped["selection_status"] == EXCLUDED_NOT_VERIFIED
    assert skipped["exclusion_reason"] == UNMAPPED and skipped["listing_in_selected_top"] == "False"
    # The complete eligible ranking is kept for audit.
    assert [r["source_model_id"] for r in result.ranked] == ["101", "102", "103", "104"]
    assert result.counts["eligible_variations"] == 4
    assert result.counts["eligible_variations_with_verified_mapping"] == 3
    assert result.counts["excluded_from_top_consideration"] == 1
    assert result.counts["excluded_by_mapping_failure"] == {UNMAPPED: 1}
    assert result.counts["selected_with_lower_eligible_rank_than_top_n"] == 1
    assert result.counts["shortfall"] == 0
    assert [e["source_key"] for e in result.exclusions] == ["10|1|101"]
    assert audit_variation_ranking(
        result.ranked, result.top, result.context_list, result.context_ledger, snapshot,
        target_master, mapping, top_n=3,
    ) == []


def test_invalid_conflicted_and_listing_only_mappings_are_excluded() -> None:
    listing = target(
        "TGT-000101", model="", manufacturer_model_code="", brand="", resolution="",
        screen_size_in="", cpu_series="", ram_capacity_gb="", storage_capacity_gb="",
        tracking_mode="LISTING_ONLY", shop_id="10", item_id="5", source_model_id="105",
        tracking_mode_reason="No product model name or code.",
        classification_evidence="Listing title and seller specs checked.",
    )
    register = [
        RegisterEntry("", "TGT-000003", "cpu_series", "Core 5", "Core 7", "OPEN", "",
                      "2026-10-20"),  # target-level conflict
        RegisterEntry("106", "TGT-000001", "cpu_series", "Core 5", "Core 7", "OPEN",
                      "stated differently", "2026-10-20"),  # source-level conflict
    ]
    result, snapshot, target_master, mapping = build(
        [vs(str(100 + n), str(1000 - n), "0", item=str(n)) for n in range(1, 9)],
        [
            target("TGT-000001"),
            target("TGT-000002", status="INACTIVE", model="ExampleBook 15"),
            target("TGT-000003", model="ExampleBook 17"),
            target(
                "TGT-000004", model="ExampleBook 16", cpu_series="Core i5", cpu_model="Intel i7"
            ),
            target("TGT-000005", model="ExampleBook 18"),
            listing,
        ],
        [
            ("102", "TGT-000002"),  # inactive target
            ("103", "TGT-000003"),  # open target conflict
            ("104", "TGT-000004"),  # cpu_model contradicts cpu_series
            ("105", "TGT-000101"),  # LISTING_ONLY target
            ("106", "TGT-000001"),  # verified target but open source-level conflict
            ("107", "TGT-000005"),  # verified
            ("108", "TGT-000001"),  # verified, shares a target with the conflicted 106
        ],
        register=register,
    )

    reasons = {r["source_model_id"]: r["exclusion_reason"] for r in result.ranked}
    assert reasons == {
        "101": UNMAPPED,
        "102": "MAPPED_TARGET_INACTIVE",
        "103": "MAPPED_KNOWN_UNRESOLVED_CONFLICT",
        "104": "MAPPED_TARGET_CPU_MODEL_CONFLICT",
        "105": "MAPPED_TARGET_LISTING_ONLY",
        "106": SOURCE_UNRESOLVED_CONFLICT,
        "107": "",
        "108": "",
    }
    assert [r["source_model_id"] for r in result.top] == ["107", "108"]
    assert {r["mapping_status"] for r in result.top} == {"MAPPED_VERIFIED"}
    assert {r["comparison_status"] for r in result.top} == {CONTEXT_READY}
    conflicted = row_of(result, "106")
    assert conflicted["mapping_status"] == "MAPPED_VERIFIED"
    assert conflicted["comparison_status"] == SOURCE_CONFLICT_BLOCKED
    assert conflicted["context_id"] == "" and conflicted["rank"] == ""
    assert row_of(result, "102")["model"] == "" and row_of(result, "102")["context_id"] == ""
    assert row_of(result, "102")["target_id"] == "TGT-000002"  # visible for audit only
    assert [c["context_id"] for c in result.contexts] == ["TGT-000005", "TGT-000001"]
    assert result.context_list[1]["contributing_source_model_ids"] == "108"
    assert result.counts["excluded_by_mapping_failure"] == dict(
        sorted(
            {
                UNMAPPED: 1,
                "MAPPED_TARGET_INACTIVE": 1,
                "MAPPED_KNOWN_UNRESOLVED_CONFLICT": 1,
                "MAPPED_TARGET_CPU_MODEL_CONFLICT": 1,
                "MAPPED_TARGET_LISTING_ONLY": 1,
                SOURCE_UNRESOLVED_CONFLICT: 1,
            }.items()
        )
    )
    assert result.counts["shortfall"] == 98
    assert len(result.exclusions) == 6
    assert audit_variation_ranking(
        result.ranked, result.top, result.context_list, result.context_ledger, snapshot,
        target_master, mapping, register=register,
    ) == []


def test_mapped_variations_sharing_a_target_stay_separate_when_others_are_skipped() -> None:
    result, *_ = build(
        [
            vs("101", "500", "0", shop="10", item="1"),  # unmapped
            vs("102", "400", "0", shop="11", item="2", price="45000.0"),
            vs("103", "300", "0", shop="12", item="3", price="43000.0"),
        ],
        [target("TGT-000001")],
        [("102", "TGT-000001"), ("103", "TGT-000001")],
    )

    first, second = result.top
    assert (first["source_key"], second["source_key"]) == ("11|2|102", "12|3|103")
    assert first["target_id"] == second["target_id"] == "TGT-000001"
    assert first["comparison_row_id"] != second["comparison_row_id"]
    assert (first["shopee_current_price_php"], second["shopee_current_price_php"]) == (
        "45000.00", "43000.00")
    assert [(r["rank"], r["eligible_rank"]) for r in result.top] == [("1", "2"), ("2", "3")]
    assert result.contexts[0]["selected_variation_count"] == "2"
    assert result.counts["distinct_configuration_contexts"] == 1


# 4. Eligibility and duplicate rules apply at the source grain


def test_only_eligible_present_in_mtd_variations_are_ranked() -> None:
    result, *_ = build(
        [
            vs("101", "100", "0", item="20"),
            vs("102", "900", "0", item="21", eligibility_status="EXCLUDED"),
            vs("103", "800", "0", item="22", present_in_mtd="False"),
        ],
        [target("TGT-000001")],
        [("101", "TGT-000001")],
    )

    assert [r["source_model_id"] for r in result.ranked] == ["101"]
    assert result.counts["ineligible_source_rows"] == 2
    assert result.counts["eligible_variations"] == 1


def test_source_id_contract_model_id_and_source_key_are_unique_in_the_eligible_ranking() -> None:
    # Phase 1 quarantines a model ID tied to several shop/item identities, so the ranking holds
    # each model_id once; the grain additionally requires a unique source_key.
    with pytest.raises(VerifiedRankingInputError, match="repeats model_id"):
        build_variation_snapshot([vs("101", "1", "0", shop="1"), vs("101", "1", "0", shop="2")])
    with pytest.raises(VerifiedRankingInputError, match="source_key is not"):
        build_variation_snapshot([vs("101", "1", "0", source_key="10|20|999")])
    with pytest.raises(VerifiedRankingInputError, match="missing columns"):
        build_variation_snapshot([{k: v for k, v in vs("101", "1", "0").items()
                                   if k != "source_key"}])


def test_a_model_id_shared_by_two_sources_is_never_mapped_by_model_id_alone() -> None:
    # Reached only if the Phase 1 quarantine were bypassed: the composite key still separates the
    # rows and neither trusts a model-ID mapping.
    snapshot = build_variation_snapshot([vs("101", "10", "0"), vs("102", "5", "0", item="21")])
    twin = snapshot.rows[1]
    forged = SourceRow(
        {**twin.values, "model_id": "101", "source_key": "10|21|101"},
        twin.m_minus_1_gmv, twin.mtd_gmv, twin.ranking_gmv,
    )
    snapshot = replace(snapshot, rows=(snapshot.rows[0], forged))
    target_master = validate_target_master(target_values(target("TGT-000001")))
    mapping = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, map_row(source_model_id="101", target_id="TGT-000001")],
        target_master,
    )

    result = build_variation_ranking(snapshot, target_master, mapping)

    assert [r["source_key"] for r in result.ranked] == ["10|20|101", "10|21|101"]
    assert {r["mapping_status"] for r in result.ranked} == {SOURCE_ID_NOT_UNIQUE}
    assert {r["exclusion_reason"] for r in result.ranked} == {SOURCE_ID_NOT_UNIQUE}
    assert result.top == [] and result.contexts == []


# 5-6. Own price only; a missing price never drops a ranked row


def test_each_row_uses_only_its_own_price_and_never_falls_back_to_another_seller() -> None:
    result, *_ = build(
        [
            vs("101", "300", "0", shop="10", item="20", price=""),  # missing own price
            vs("102", "200", "0", shop="11", item="21", price="40000.0"),
            vs("103", "100", "0", shop="12", item="22", price="39000.0",
               price_status="PRICE_CONFLICT", mtd_price_values="39000.0;41000.0"),
        ],
        [target("TGT-000001")],
        [("101", "TGT-000001"), ("102", "TGT-000001"), ("103", "TGT-000001")],
    )

    by_id = {r["source_model_id"]: r for r in result.top}
    assert len(result.top) == 3  # a missing or conflicting price never drops the row
    assert by_id["101"]["shopee_current_price_php"] == ""
    assert by_id["101"]["shopee_price_status"] == "PRICE_MISSING"
    assert by_id["102"]["shopee_current_price_php"] == "40000.00"
    assert by_id["103"]["shopee_current_price_php"] == ""  # a conflict is never averaged or picked
    assert by_id["103"]["shopee_price_status"] == "PRICE_CONFLICT"
    assert "39000.0;41000.0" in by_id["103"]["shopee_price_detail"]
    assert {r["shopee_price_observed_at"] for r in result.top} == {"2026-10-27"}
    assert {r["shopee_price_date_basis"] for r in result.top} == {"SALES_EXTRACT_DATE"}
    assert result.counts["selected_with_own_price"] == 1


# 6b. Selection size, shortfall and tie-breaking


def test_exactly_top_n_distinct_variations_are_selected_by_own_gmv() -> None:
    sources = [vs(str(100 + n), str(1000 - n), "0", item=str(n)) for n in range(1, 8)]
    result, *_ = build(
        sources, [target("TGT-000001")], [(str(100 + n), "TGT-000001") for n in range(1, 8)],
        top_n=5,
    )

    assert len(result.top) == 5 and len(result.ranked) == 7
    assert [r["source_model_id"] for r in result.top] == ["101", "102", "103", "104", "105"]
    assert result.counts["shortfall"] == 0 and result.counts["selected_variations"] == 5
    assert result.counts["qualified_not_selected"] == 2
    assert [r["listing_in_selected_top"] for r in result.ranked] == ["True"] * 5 + ["False"] * 2
    assert [r["selection_status"] for r in result.ranked] == [SELECTED] * 5 + [
        QUALIFIED_NOT_SELECTED
    ] * 2
    assert [r["rank"] for r in result.ranked] == ["1", "2", "3", "4", "5", "6", "7"]


def test_a_shortfall_is_reported_without_padding_or_duplicating_rows() -> None:
    result, *_ = build(
        [
            vs("101", "10", "0", item="1"),
            vs("102", "9", "0", item="2"),
            vs("103", "8", "0", item="3"),  # unmapped: never pads the list
        ],
        [target("TGT-000001")], [("101", "TGT-000001"), ("102", "TGT-000001")], top_n=100,
    )

    assert len(result.top) == 2
    assert len({r["source_key"] for r in result.top}) == 2
    assert result.counts["shortfall"] == 98
    reason = result.counts["shortfall_reason"]
    assert "only 2 of 3 eligible variations have a verified mapping" in reason
    assert "UNMAPPED 1" in reason and "none is padded" in reason
    assert result.counts["eligible_variations_with_verified_mapping"] == 2
    assert result.counts["selection_policy"] == SELECTION_POLICY


def test_match_review_and_retailer_coverage_do_not_gate_selection() -> None:
    # The builder takes no MATCH_REVIEW and no retailer data: a mapped variation qualifies without
    # a COMPLETE review row and without any retailer observation.
    import inspect

    parameters = inspect.signature(build_variation_ranking).parameters
    assert "match_review" not in parameters and "reviews" not in parameters
    result, *_ = build([vs("101", "10", "0", item="1")], [target("TGT-000001")],
                       [("101", "TGT-000001")])
    assert [r["source_model_id"] for r in result.top] == ["101"]
    assert result.top[0]["mapping_status"] == "MAPPED_VERIFIED"


def test_ties_break_on_mtd_gmv_then_model_id_deterministically() -> None:
    sources = [
        vs("103", "100", "0", item="3"),
        vs("102", "50", "50", item="2"),  # same total, higher MTD
        vs("101", "100", "0", item="1"),
    ]
    links = [("101", "TGT-000001"), ("102", "TGT-000001"), ("103", "TGT-000001")]
    first, *_ = build(sources, [target("TGT-000001")], links)
    second, *_ = build(list(reversed(sources)), [target("TGT-000001")], links)

    order = [r["source_model_id"] for r in first.ranked]
    assert order == ["102", "101", "103"]
    assert order == [r["source_model_id"] for r in second.ranked]
    assert order == [r["source_model_id"] for r in first.top]
    assert order == [r["source_model_id"] for r in second.top]
    assert {r["rank_metric"] for r in first.ranked} == {"ranking_gmv_usd"}
    assert "M-1 2026-09" in first.ranked[0]["rank_metric_scope"]
    assert "MTD 2026-10" in first.ranked[0]["rank_metric_scope"]


# Contexts feed the existing retailer layer


def test_contexts_are_built_after_selection_and_fit_the_existing_selected_list_format() -> None:
    result, snapshot, target_master, _ = build(
        [
            vs("101", "100", "10", item="1"),
            vs("102", "90", "10", item="2"),
            vs("201", "50", "0", item="3"),
            vs("202", "5", "0", item="4"),  # ranked below the selection
        ],
        [target("TGT-000001"), target("TGT-000002", model="ExampleBook 16")],
        [("101", "TGT-000001"), ("102", "TGT-000001"), ("201", "TGT-000002"),
         ("202", "TGT-000002")],
        top_n=3,
        target_master_sha256="a" * 64,
    )

    assert [c["target_id"] for c in result.context_list] == ["TGT-000001", "TGT-000002"]
    first = result.context_list[0]
    assert (first["m_minus_1_gmv_usd"], first["mtd_gmv_usd"], first["ranking_gmv_usd"]) == (
        "190", "20", "210")
    assert first["contributing_source_count"] == "2"
    # Context GMV only covers the selected variations; the unselected one is counted as excluded.
    assert result.context_list[1]["contributing_source_model_ids"] == "201"
    assert result.context_list[1]["excluded_mapped_source_count"] == "1"
    reasons = {r["model_id"]: r["reason_code"] for r in result.context_ledger}
    assert reasons["202"] == "OUTSIDE_SELECTED_VARIATIONS"
    assert [r["model_id"] for r in result.context_ledger
            if r["contribution_status"] == CONTRIBUTING] == ["101", "102", "201"]
    # The existing validator for the retailer layer accepts it unchanged.
    selected = validate_selected_list(
        result.context_list, result.context_ledger, target_master,
        target_master_sha256="a" * 64, list_sha256="b" * 64,
    )
    assert [c.target_id for c in selected.configurations] == ["TGT-000001", "TGT-000002"]
    # The context order is a presentation order, never a variation rank.
    assert "rank" in result.context_list[0] and result.top[0]["rank"] == "1"
    mapping = _mapping(target_master, [("101", "TGT-000001"), ("102", "TGT-000001"),
                                       ("201", "TGT-000002"), ("202", "TGT-000002")])
    assert audit_variation_ranking(
        result.ranked, result.top, result.context_list, result.context_ledger, snapshot,
        target_master, mapping, top_n=3,
    ) == []


def _mapping(target_master, links):
    return validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, *(map_row(source_model_id=s, target_id=t) for s, t in links)],
        target_master,
    )


# 8. Contexts that differ, or are unknown, are not shared


def test_known_distinguishing_hardware_and_unknowns_keep_contexts_apart() -> None:
    gpu_a = {"model": "ExampleBook 14", "gpu_model": "RTX 4050", "gpu_type": "DISCRETE"}
    gpu_b = {**gpu_a, "gpu_model": "RTX 4060"}
    unknown = {"model": "ExampleBook 14", "gpu_model": "", "gpu_type": ""}
    same = dict(gpu_a)

    assert not contexts_compatible(gpu_a, gpu_b)
    assert not contexts_compatible(gpu_a, unknown)  # unknown is not proof of agreement
    assert contexts_compatible(gpu_a, same)
    assert context_fingerprint(gpu_a) == context_fingerprint(same)
    assert context_fingerprint(gpu_a) != context_fingerprint(unknown)
    assert context_fingerprint(gpu_a) != context_fingerprint(gpu_b)


def test_the_catalog_never_holds_two_targets_with_one_core_so_contexts_are_targets() -> None:
    # A context is a catalog target: TARGET_MASTER refuses a second target with the same five core
    # fields, so no two contexts can share a configuration, and an unmapped variation never joins a
    # context by resemblance. (The fingerprint above also separates known secondary hardware.)
    twins = validate_target_master(target_values(
        target("TGT-000001"),
        target("TGT-000002", color="Blue", manufacturer_part_number="PN-2"),
    ))

    assert not twins.is_valid


# Audit


def test_the_audit_passes_and_detects_tampering() -> None:
    result, snapshot, target_master, mapping = build(
        [vs("101", "100", "0", item="1"), vs("102", "90", "0", item="2")],
        [target("TGT-000001")], [("101", "TGT-000001"), ("102", "TGT-000001")],
    )

    def audit(ranked=None, top=None, ledger=None):
        return audit_variation_ranking(
            ranked or result.ranked, top or result.top, result.context_list,
            ledger or result.context_ledger, snapshot, target_master, mapping)

    assert audit() == []
    swapped = [dict(result.ranked[1]), dict(result.ranked[0])]
    assert any("1..n" in i or "order" in i for i in audit(ranked=swapped, top=swapped))
    tampered = [dict(result.ranked[0], ranking_gmv_usd="999"), dict(result.ranked[1])]
    assert any("own sales row" in i for i in audit(ranked=tampered, top=tampered))
    wrong_price = [dict(result.ranked[0], shopee_current_price_php="1.00"), result.ranked[1]]
    assert any("own variation's price" in i for i in audit(ranked=wrong_price, top=wrong_price))
    assert any("exactly the eligible" in i for i in audit(ranked=result.ranked[:1],
                                                          top=result.ranked[:1]))


def test_the_audit_rejects_a_list_that_limited_first_and_filtered_afterwards() -> None:
    result, snapshot, target_master, mapping = build(
        [
            vs("101", "900", "0", item="1"),  # unmapped
            vs("102", "800", "0", item="2"),
            vs("103", "700", "0", item="3"),
        ],
        [target("TGT-000001")], [("102", "TGT-000001"), ("103", "TGT-000001")], top_n=2,
    )

    def audit(**changes):
        return audit_variation_ranking(
            changes.get("ranked", result.ranked), changes.get("top", result.top),
            result.context_list, result.context_ledger, snapshot, target_master, mapping, top_n=2)

    assert audit() == []
    # The pre-2026-11-03 behaviour: the first 2 eligible rows, unmapped one included.
    assert any("first 2 verified-mapped" in i for i in audit(top=result.ranked[:2]))
    # An unmapped row dressed up as qualifying is caught by the recomputed mapping check.
    forged = [dict(result.ranked[0], exclusion_reason="", rank="1"), *result.ranked[1:]]
    assert any("recomputed mapping check" in i for i in audit(ranked=forged))


def test_outputs_are_written_with_schema_grain_and_metric_provenance(tmp_path: Path) -> None:
    result, snapshot, *_ = build(
        [vs("101", "100", "0", item="1")], [target("TGT-000001")], [("101", "TGT-000001")],
        shop_names={"10|1|101": "Synthetic Seller"},
    )

    paths = write_variation_outputs(tmp_path, result, {"schema_version": SCHEMA_VERSION})

    assert set(paths) == set(OUTPUT_FILES)
    assert paths["exclusions"].name == "mapping_exclusions.csv"
    top = read_csv_rows(paths["top"], ("rank", "shop_name", "schema_version", "row_grain"), "top")
    assert top[0]["shop_name"] == "Synthetic Seller"
    assert (top[0]["schema_version"], top[0]["row_grain"]) == (SCHEMA_VERSION, ROW_GRAIN)
    assert json.loads(paths["summary"].read_text())["schema_version"] == SCHEMA_VERSION
    assert comparison_row_id("10|1|101") == "SV-10-1-101"
    assert result.top[0]["comparison_row_id"] == comparison_row_id("10|1|101")
    assert snapshot.m_minus_1_month == "2026-09"


# Selected-variation precedence (owner decision 2026-11-03)

PARENT = "Hp Laptop | Intel i7/i5/i3 & Celeron 4GB-16GB RAM 128GB-512GB SSD"


def test_a_variation_that_contradicts_its_target_is_excluded_with_the_exact_fields() -> None:
    # CORE targets record "Core 5"; the first variation states Core i3, the second states nothing.
    result, snapshot, target_master, mapping = build(
        [
            vs("101", "300", "0", item="1", item_name=PARENT, shopee_model_name="5:I3-7GEN"),
            vs("102", "200", "0", item="1", item_name=PARENT, shopee_model_name="Black"),
        ],
        [target("TGT-000001")],
        [("101", "TGT-000001"), ("102", "TGT-000001")],
    )

    # The sibling's silence is not a conflict and the first variation's text is never inherited.
    assert [r["source_model_id"] for r in result.top] == ["102"]
    blocked = row_of(result, "101")
    assert blocked["exclusion_reason"] == VARIATION_SPEC_CONFLICT
    assert blocked["comparison_status"] == SPEC_CONFLICT_BLOCKED
    assert blocked["mapping_status"] == "MAPPED_VERIFIED" and blocked["rank"] == ""
    assert "cpu_series: variation states 'Core i3'" in blocked["variation_spec_conflicts"]
    assert "target records 'Core 5'" in blocked["comparison_detail"]
    assert "cpu_series=Core i3 [variation: I3]" in blocked["variation_spec_evidence"]
    assert [(c["field"], c["variation_value"], c["target_value"], c["evidence"])
            for c in result.spec_conflicts] == [("cpu_series", "Core i3", "Core 5", "I3")]
    assert result.counts["excluded_by_mapping_failure"] == {VARIATION_SPEC_CONFLICT: 1}
    assert row_of(result, "102")["variation_spec_conflicts"] == ""
    assert audit_variation_ranking(
        result.ranked, result.top, result.context_list, result.context_ledger, snapshot,
        target_master, mapping,
    ) == []


def test_a_multi_option_parent_alone_never_blocks_a_variation() -> None:
    result, *_ = build(
        [vs("101", "300", "0", item="1", item_name="Lenovo Intel Core i7 - i5 - i3 16GB / 8GB",
            shopee_model_name="Black")],
        [target("TGT-000001")], [("101", "TGT-000001")],
    )

    assert [r["source_model_id"] for r in result.top] == ["101"]
    assert result.top[0]["variation_spec_conflicts"] == ""
    assert "cpu_series=unknown (ambiguous)" in result.top[0]["variation_spec_evidence"]


def test_a_disagreement_is_still_shown_for_a_row_excluded_for_another_reason() -> None:
    result, *_ = build(
        [vs("101", "300", "0", item="1", shopee_model_name="Core i3")],
        [target("TGT-000001", status="INACTIVE")], [("101", "TGT-000001")],
    )

    row = row_of(result, "101")
    assert row["exclusion_reason"] == "MAPPED_TARGET_INACTIVE"  # the first reason keeps precedence
    assert "cpu_series: variation states 'Core i3'" in row["variation_spec_conflicts"]
    assert len(result.spec_conflicts) == 1 and result.top == []


def test_the_spec_comparison_does_not_depend_on_review_rows_or_other_variations() -> None:
    import inspect

    assert "match_review" not in inspect.signature(build_variation_ranking).parameters
    first, *_ = build(
        [vs("101", "300", "0", item="1", shopee_model_name="Core i3"),
         vs("102", "200", "0", item="1", shopee_model_name="Core 5")],
        [target("TGT-000001")], [("101", "TGT-000001"), ("102", "TGT-000001")],
    )
    alone, *_ = build(
        [vs("102", "200", "0", item="1", shopee_model_name="Core 5")],
        [target("TGT-000001")], [("102", "TGT-000001")],
    )
    together, separate = row_of(first, "102"), row_of(alone, "102")
    assert together["variation_spec_evidence"] == separate["variation_spec_evidence"]
    assert row_of(first, "102")["exclusion_reason"] == ""


# Union of the two reporting months (owner decision 2026-11-03)

M1_ONLY = {
    "present_in_m_minus_1": "True", "present_in_mtd": "False",
    "m_minus_1_status": "PRESENT", "mtd_status": "NO_RECORD_TREATED_AS_ZERO",
    "price_basis": "M_MINUS_1_PRICE", "price_record_date": "2026-09-01",
    "price_period_month": "2026-09",
}
MTD_ONLY = {
    "present_in_m_minus_1": "False", "present_in_mtd": "True",
    "m_minus_1_status": "NO_RECORD_TREATED_AS_ZERO", "mtd_status": "PRESENT",
    "price_basis": "MTD_PRICE", "price_period_month": "2026-10",
}
BOTH = {
    "present_in_m_minus_1": "True", "present_in_mtd": "True",
    "m_minus_1_status": "PRESENT", "mtd_status": "PRESENT",
    "price_basis": "MTD_PRICE", "price_period_month": "2026-10",
}


def test_a_variation_with_only_an_m_minus_1_record_qualifies_and_ranks_by_its_own_gmv() -> None:
    result, snapshot, target_master, mapping = build(
        [
            vs("101", "0", "50", item="1", **MTD_ONLY),
            vs("102", "300", "0", item="2", **M1_ONLY),  # no current-month record, highest GMV
            vs("103", "20", "30", item="3", **BOTH),
            vs("104", "10", "0", item="4", **M1_ONLY),  # unmapped, still counted as eligible
        ],
        [target("TGT-000001")],
        [("101", "TGT-000001"), ("102", "TGT-000001"), ("103", "TGT-000001")],
    )

    assert [r["source_model_id"] for r in result.top] == ["102", "101", "103"]
    first = result.top[0]
    assert (first["present_in_m_minus_1"], first["present_in_mtd"]) == ("True", "False")
    # A missing month is flagged; it adds 0 to the arithmetic but is not an observed zero.
    assert first["mtd_status"] == "NO_RECORD_TREATED_AS_ZERO" and first["mtd_gmv_usd"] == "0"
    assert first["m_minus_1_status"] == "PRESENT" and first["ranking_gmv_usd"] == "300"
    assert "not an observed zero" in first["rank_metric_scope"]
    assert "either month" in first["rank_metric_scope"]
    counts = result.counts
    assert (counts["eligible_in_m_minus_1"], counts["eligible_in_mtd"]) == (3, 2)
    assert (counts["eligible_m_minus_1_only"], counts["eligible_mtd_only"]) == (2, 1)
    assert (counts["eligible_in_both_months"], counts["eligible_union"]) == (1, 4)
    assert counts["qualifying_union"] == 3 and counts["qualifying_m_minus_1_only"] == 1
    assert counts["selected_m_minus_1_only"] == 1 and counts["selected_with_m_minus_1_price"] == 1
    assert row_of(result, "104")["exclusion_reason"] == UNMAPPED
    assert audit_variation_ranking(
        result.ranked, result.top, result.context_list, result.context_ledger, snapshot,
        target_master, mapping,
    ) == []


def test_an_m_minus_1_price_keeps_its_own_period_record_date_and_basis() -> None:
    result, snapshot, target_master, mapping = build(
        [
            vs("101", "300", "0", item="1", price="48990.0", **M1_ONLY),
            vs("102", "0", "200", item="2", price="45990.0", **MTD_ONLY),
        ],
        [target("TGT-000001")], [("101", "TGT-000001"), ("102", "TGT-000001")],
    )

    october, november = row_of(result, "101"), row_of(result, "102")
    # The earlier month's price is dated by its own record, never by the later extract.
    assert october["shopee_current_price_php"] == "48990.00"
    assert october["shopee_price_observed_at"] == "2026-09-01"
    assert october["shopee_price_date_basis"] == "M_MINUS_1_RECORD_DATE"
    assert (october["price_basis"], october["price_period_month"]) == ("M_MINUS_1_PRICE", "2026-09")
    assert "M-1 2026-09 source record" in october["shopee_price_detail"]
    assert november["shopee_price_observed_at"] == snapshot.extract_date
    assert november["shopee_price_date_basis"] == "SALES_EXTRACT_DATE"
    # Each row keeps its own seller's price: nothing is borrowed across variations.
    assert november["shopee_current_price_php"] == "45990.00"
    assert audit_variation_ranking(
        result.ranked, result.top, result.context_list, result.context_ledger, snapshot,
        target_master, mapping,
    ) == []
    relabelled = [dict(r, shopee_price_observed_at=snapshot.extract_date) if r is october else r
                  for r in result.ranked]
    assert any("price date is not its own price record" in i for i in audit_variation_ranking(
        relabelled, relabelled, result.context_list, result.context_ledger, snapshot,
        target_master, mapping))


def test_a_missing_price_never_removes_an_otherwise_qualifying_variation() -> None:
    result, *_ = build(
        [
            vs("101", "300", "0", item="1", price="", price_status="PRICE_MISSING",
               **{**M1_ONLY, "price_basis": ""}),
            vs("102", "100", "0", item="2", price="900.0", **M1_ONLY),
        ],
        [target("TGT-000001")], [("101", "TGT-000001"), ("102", "TGT-000001")],
    )

    assert [r["source_model_id"] for r in result.top] == ["101", "102"]
    assert result.top[0]["shopee_current_price_php"] == ""
    assert result.top[0]["shopee_price_status"] == "PRICE_MISSING"
    assert result.top[1]["shopee_current_price_php"] == "900.00"  # not copied to the first row
    assert result.counts["selected_without_own_price"] == 1


def test_m_minus_1_only_variations_sharing_a_target_stay_separate_rows() -> None:
    result, *_ = build(
        [
            vs("101", "200", "0", item="1", shop="10", **M1_ONLY),
            vs("102", "100", "0", item="2", shop="11", **M1_ONLY),
        ],
        [target("TGT-000001")], [("101", "TGT-000001"), ("102", "TGT-000001")],
    )

    assert [r["source_key"] for r in result.top] == ["10|1|101", "11|2|102"]
    assert result.counts["distinct_configuration_contexts"] == 1
    assert result.contexts[0]["selected_variation_count"] == "2"


def test_the_month_flags_survive_an_older_ranking_file_without_them() -> None:
    result, *_ = build([vs("101", "100", "0", item="1")], [target("TGT-000001")],
                       [("101", "TGT-000001")])

    row = result.top[0]  # a file written before the flags existed: present in MTD, flags derived
    assert row["present_in_mtd"] == "True" and row["present_in_m_minus_1"] == ""
    assert row["mtd_status"] == "PRESENT" and row["shopee_price_date_basis"] == "SALES_EXTRACT_DATE"
