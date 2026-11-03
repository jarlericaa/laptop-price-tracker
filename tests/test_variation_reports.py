"""Variation-level price reports (schema shopee-variation-sheets-1): rows join their contexts.

The retailer layer is keyed by context (a catalog target); every ranked Shopee variation keeps its
own row, own price and own associations. Synthetic reports only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_variation_ranking import build, vs
from test_verified_top import target

from laptop_tracker.price_tracking import (
    ALTERNATIVE_COLUMNS,
    ALTERNATIVE_SELECTED,
    COMPARISON_COLUMNS,
    SELECTED,
    SELECTION_COLUMNS,
)
from laptop_tracker.variation_ranking import ROW_GRAIN, SCHEMA_VERSION
from laptop_tracker.variation_reports import (
    CONTEXT_COPY_COLUMNS,
    SNAPSHOT_AFTER_CUTOFF,
    SNAPSHOT_PRICE_STALE,
    ContextReport,
    VariationReportError,
    build_variation_reports,
    validate_variation_reports,
    write_reports,
)

pytestmark = pytest.mark.usefixtures("no_network")

LIST_SHA = "c" * 64
AS_OF = "2026-10-27T03:00:00.000000Z"
CODES = ("DBZ", "LF", "PCX", "PCC", "OCT")
RETAILER_IDS = {"DBZ": "datablitz", "LF": "laptop_factory", "PCX": "pcx", "PCC": "pcc",
                "OCT": "oct"}


def comparison_row(target_id: str, rank: str, prices: dict[str, str] | None = None,
                   alternatives: dict[str, str] | None = None) -> dict[str, str]:
    row = dict.fromkeys(COMPARISON_COLUMNS, "")
    row |= {"report_week_start": "2026-10-26", "report_week_end": "2026-11-02", "rank": rank,
            "target_id": target_id, "matching_context_id": "ctx-1", "Brand": "ExampleBrand",
            "Shopee_Price": "1.00", "Shopee_Price_Status": "SNAPSHOT_PRICE",
            "Shopee_Source_Count": "9"}
    for code in CODES:
        row[f"{code}_Price_Status"] = "NO_RELEVANT_CANDIDATE"
        row[f"{code}_Comparison_Tag"] = ""
    for code, price in (prices or {}).items():
        row |= {f"{code}_Price": price, f"{code}_Price_Status": SELECTED,
                f"{code}_Offer_URL": f"https://example.test/{code}/{target_id}",
                f"{code}_Stock": "IN_STOCK", f"{code}_Comparison_Tag": "MATCH",
                f"{code}_Observed_At": "2026-10-27T10:00:00+08:00"}
    for code, price in (alternatives or {}).items():
        row |= {f"{code}_Price": price, f"{code}_Price_Status": ALTERNATIVE_SELECTED,
                f"{code}_Comparison_Tag": "ALT 1 · RAM",
                f"{code}_Alternative_Differences": "RAM: target 16 GB, retailer 8 GB"}
    if prices:
        row |= {"Lowest_Price": min(prices.values()), "Comparable_Retailer_Count": str(len(prices))}
    if alternatives:
        row |= {"Lowest_Alternative_Price": min(alternatives.values()),
                "Lowest_Alternative_Price_Retailers": ";".join(alternatives)}
    return row


def selection_row(target_id: str, code: str, status: str, observation: str = "",
                  price: str = "") -> dict[str, str]:
    return dict.fromkeys(SELECTION_COLUMNS, "") | {
        "target_id": target_id, "retailer_code": code, "retailer_id": RETAILER_IDS[code],
        "price_status": status, "observation_id": observation, "price": price,
        "report_week_start": "2026-10-26", "rank": "1",
    }


def alternative_row(target_id: str, code: str, observation: str, level: str,
                    price: str = "900.00", eligible: str = "COMPARABLE") -> dict[str, str]:
    return dict.fromkeys(ALTERNATIVE_COLUMNS, "") | {
        "target_id": target_id, "retailer_code": code, "retailer_id": RETAILER_IDS[code],
        "observation_id": observation, "difference_count": level,
        "alternative_tag": f"ALT {level} · RAM", "price": price, "price_eligibility": eligible,
        "differences": "RAM: target 16 GB, retailer 8 GB", "differing_dimensions": "ram",
        "offer_url": f"https://example.test/{code}/alt", "rank": "1", "offer_key": f"{code}-alt",
    }


def make_report(
    tmp_path: Path, targets: list[str], rows, selections, alternatives=()
) -> ContextReport:
    return ContextReport(
        tmp_path,
        {r["target_id"]: r for r in rows},
        {(s["target_id"], s["retailer_code"]): s for s in selections},
        list(alternatives), [], [], [],
        {
            "selected_list": {"sha256": LIST_SHA},
            "sales_snapshot": {"run_id": "20261027T090000+0800"},
            "report_week": {"start": "2026-10-26", "end_exclusive": "2026-11-02",
                            "status": "IN_PROGRESS"},
            "as_of_utc": AS_OF,
            "matching_context": {"context_id": "ctx-1"},
        },
        {},
    )


def full_selections(target_id: str, matched: dict[str, str]) -> list[dict[str, str]]:
    return [
        selection_row(target_id, code, SELECTED if code in matched else "NO_RELEVANT_CANDIDATE",
                      observation=matched.get(code, ""), price="1000.00" if code in matched else "")
        for code in CODES
    ]


def scenario(tmp_path: Path, **kwargs):
    """Three verified-mapped variations of TGT-000001 (one shared context), the last without a
    usable own price, plus a higher-ranked unmapped one that never reaches the Top 100."""
    result, *_ = build(
        [
            vs("100", "900", "0", shop="9", item="9", price="1.0"),  # unmapped: not selected
            vs("101", "300", "0", shop="10", item="1", price="48990.0"),
            vs("102", "200", "0", shop="11", item="2", price="45990.0"),
            vs("103", "100", "0", shop="12", item="3", price=""),
        ],
        [target("TGT-000001")],
        [("101", "TGT-000001"), ("102", "TGT-000001"), ("103", "TGT-000001")],
    )
    rows = [comparison_row("TGT-000001", "1", prices={"DBZ": "1000.00", "PCX": "1100.00"})]
    selections = full_selections("TGT-000001", {"DBZ": "obs-dbz", "PCX": "obs-pcx"})
    report = make_report(tmp_path, ["TGT-000001"], rows, selections, **kwargs)
    return result, report


def build_reports(result, report):
    return build_variation_reports(result.top, result.contexts, LIST_SHA, report)


def test_rows_sharing_a_context_show_its_prices_but_keep_their_own_identity_and_shopee_price(
    tmp_path,
) -> None:
    result, report = scenario(tmp_path)
    reports = build_reports(result, report)

    first, second, third = reports.comparison
    assert (first["shop_id"], second["shop_id"], third["shop_id"]) == ("10", "11", "12")
    assert (first["shopee_price_php"], second["shopee_price_php"]) == ("48990.00", "45990.00")
    assert first["DBZ_Price"] == second["DBZ_Price"] == "1000.00"
    assert first["context_id"] == second["context_id"] == "TGT-000001"
    assert [r["rank"] for r in reports.comparison] == ["1", "2", "3"]
    assert all(r["row_grain"] == ROW_GRAIN and r["schema_version"] == SCHEMA_VERSION
               for r in reports.comparison)
    assert validate_variation_reports(reports) == []


def test_the_configuration_level_shopee_snapshot_is_never_copied_into_variation_rows(
    tmp_path,
) -> None:
    result, report = scenario(tmp_path)
    reports = build_reports(result, report)

    assert not [name for name in CONTEXT_COPY_COLUMNS if name.startswith("Shopee_")]
    assert "Shopee_Price" not in reports.comparison[0]
    # The row's own price (not the context report's configuration-level 1.00) is shown.
    assert reports.comparison[0]["shopee_price_php"] == "48990.00"


def test_a_missing_own_price_keeps_the_row_and_never_borrows_another_sellers_price(
    tmp_path,
) -> None:
    result, report = scenario(tmp_path)
    reports = build_reports(result, report)

    third = reports.comparison[2]
    assert third["shopee_price_php"] == "" and third["shopee_price_status"] == "PRICE_MISSING"
    assert len(reports.comparison) == 3
    assert reports.coverage["counts"]["rows_with_own_shopee_price"] == 2
    assert reports.coverage["counts"]["rows_without_own_shopee_price"] == 1
    assert third["DBZ_Price"] == "1000.00"  # retailer cells are the context's, not another price


def test_the_report_builder_refuses_an_unmapped_variation_in_the_selected_list(tmp_path) -> None:
    result, report = scenario(tmp_path)
    unmapped = next(r for r in result.ranked if r["source_model_id"] == "100")
    assert unmapped["exclusion_reason"] == "UNMAPPED" and unmapped not in result.top

    with pytest.raises(VariationReportError, match="not a verified-mapped variation"):
        build_variation_reports([unmapped, *result.top], result.contexts, LIST_SHA, report)


def test_coverage_separates_rows_contexts_and_unique_observations(tmp_path) -> None:
    result, report = scenario(tmp_path)
    counts = build_reports(result, report).coverage["counts"]

    assert counts["selected_variations"] == 3
    assert counts["distinct_configuration_contexts"] == 1
    assert counts["rows_with_trusted_context"] == 3 and counts["rows_without_trusted_context"] == 0
    assert counts["mapped_selected_variations"] == 3 and counts["unmapped_selected_variations"] == 0
    assert counts["matching_priced_variation_rows"] == 3
    assert counts["contexts_with_matching_price"] == 1
    # Two offers (DataBlitz, PC Express) behind six row associations: not six fetched offers.
    assert counts["unique_matching_observations"] == 2
    assert counts["row_associations_matching"] == 6
    assert counts["rows_by_comparison_status"] == {"MATCHING_PRICES_FOUND": 3}


def test_alternative_associations_are_kept_per_row_and_offers_are_counted_once(tmp_path) -> None:
    alternatives = [
        alternative_row("TGT-000001", "LF", "obs-lf", "1"),
        alternative_row("TGT-000001", "OCT", "obs-oct", "2"),
        alternative_row("TGT-000001", "PCC", "obs-pcc", "1", eligible="NOT_COMPARABLE"),
    ]
    result, report = scenario(tmp_path, alternatives=alternatives)
    reports = build_reports(result, report)

    assert len(reports.alternatives) == 9  # three associations x three rows of the context
    assert len({a["association_id"] for a in reports.alternatives}) == 9
    assert {a["comparison_row_id"] for a in reports.alternatives} == {
        r["comparison_row_id"] for r in reports.comparison}
    counts = reports.coverage["counts"]
    assert (counts["alt_1_offers"], counts["alt_2_offers"]) == (1, 1)  # eligible, unique offers
    assert counts["alternative_row_associations"] == 9
    assert counts["alternative_row_associations_eligible"] == 6
    assert counts["unique_alternative_offers_eligible"] == 2
    # Both minima families come from the context row, unchanged.
    assert reports.comparison[0]["Lowest_Price"] == "1000.00"
    assert reports.comparison[0]["Lowest_Alternative_Price"] == ""


def test_matching_and_alternative_cells_keep_their_own_statuses(tmp_path) -> None:
    result, *_ = build(
        [vs("101", "300", "0", price="1.0")], [target("TGT-000001")], [("101", "TGT-000001")])
    rows = [comparison_row("TGT-000001", "1", prices={"DBZ": "1000.00"},
                           alternatives={"PCX": "900.00"})]
    selections = [
        selection_row("TGT-000001", "DBZ", SELECTED, "obs-dbz", "1000.00"),
        selection_row("TGT-000001", "PCX", ALTERNATIVE_SELECTED, "obs-pcx", "900.00"),
        *(selection_row("TGT-000001", c, "NO_RELEVANT_CANDIDATE") for c in ("LF", "PCC", "OCT")),
    ]
    reports = build_reports(result, make_report(
        tmp_path, ["TGT-000001"], rows, selections,
        alternatives=[alternative_row("TGT-000001", "PCX", "obs-pcx", "1")]))

    row = reports.comparison[0]
    assert row["row_comparison_status"] == "MATCHING_PRICES_FOUND"
    kinds = {s["retailer_code"]: s["association_kind"] for s in reports.selections}
    assert kinds["DBZ"] == "CONTEXT_MATCH" and kinds["PCX"] == "CONTEXT_ALTERNATIVE"
    history = {h["retailer_code"]: h for h in reports.history}
    assert history["DBZ"]["difference_tag"] == "MATCH" and history["DBZ"]["difference_count"] == "0"
    assert history["PCX"]["difference_tag"] == "ALT 1 · RAM"
    assert "RAM" in history["PCX"]["difference_details"]
    counts = reports.coverage["counts"]
    assert counts["matching_priced_variation_rows"] == 1
    assert counts["alternative_priced_variation_rows"] == 1


# Shopee price as of the report cutoff


def test_a_price_extracted_after_the_cutoff_is_not_valid_as_of_the_cutoff(tmp_path) -> None:
    result, report = scenario(tmp_path)
    for row in result.top:
        row["shopee_price_observed_at"] = "2026-10-28"
    reports = build_reports(result, report)

    first = reports.comparison[0]
    assert first["shopee_price_php"] == "" and first["shopee_price_status"] == SNAPSHOT_AFTER_CUTOFF
    assert first["shopee_price_observed_at"] == "2026-10-28"  # the actual date is kept


def test_an_old_extract_is_shown_but_labelled_stale_with_its_actual_date(tmp_path) -> None:
    result, report = scenario(tmp_path)
    for row in result.top:
        row["shopee_price_observed_at"] = "2026-10-10"
    reports = build_reports(result, report)

    first = reports.comparison[0]
    assert first["shopee_price_php"] == "48990.00"
    assert first["shopee_price_status"] == SNAPSHOT_PRICE_STALE
    assert "17 days" in first["shopee_price_detail"]
    assert "no per-price observation time" not in first["shopee_price_detail"]
    fresh = build_reports(*scenario(tmp_path)).comparison[0]
    assert "no per-price observation time" in fresh["shopee_price_detail"]


# Rejections


def test_a_context_report_from_another_list_or_snapshot_is_rejected(tmp_path) -> None:
    result, report = scenario(tmp_path)
    with pytest.raises(VariationReportError, match="SHA-256 differs"):
        build_variation_reports(result.top, result.contexts, "d" * 64, report)
    report.summary["sales_snapshot"]["run_id"] = "another"
    with pytest.raises(VariationReportError, match="different sales snapshot"):
        build_reports(result, report)


def test_contexts_and_selected_rows_must_agree(tmp_path) -> None:
    result, report = scenario(tmp_path)
    report.comparison["TGT-999999"] = comparison_row("TGT-999999", "9")
    with pytest.raises(VariationReportError, match="targets differ"):
        build_reports(result, report)


def test_a_variation_list_of_another_grain_or_with_duplicate_keys_is_rejected(tmp_path) -> None:
    result, report = scenario(tmp_path)
    legacy = [dict(r, row_grain="TARGET_CONFIGURATION") for r in result.top]
    with pytest.raises(VariationReportError, match="shopee-variation-sheets-1 list"):
        build_variation_reports(legacy, result.contexts, LIST_SHA, report)
    with pytest.raises(VariationReportError, match="repeats a source_key"):
        build_variation_reports([result.top[0], result.top[0]], result.contexts, LIST_SHA, report)


def test_validation_catches_a_retailer_price_on_a_row_without_a_context(tmp_path) -> None:
    result, report = scenario(tmp_path)
    reports = build_reports(result, report)
    reports.comparison[2]["context_id"] = ""
    reports.comparison[2]["DBZ_Price"] = "1.00"

    assert any("without a context" in issue for issue in validate_variation_reports(reports))


def test_reports_are_written_for_every_artifact_and_reload(tmp_path) -> None:
    result, report = scenario(tmp_path)
    paths = write_reports(tmp_path / "out", build_reports(result, report))

    assert {p.name for p in paths.values()} == {
        "weekly_variation_comparison.csv", "weekly_variation_selections.csv",
        "weekly_variation_alternatives.csv", "weekly_variation_rechecks.csv",
        "weekly_variation_history_associations.csv", "variation_coverage.json",
        "variation_run_summary.json"}
    assert all(p.is_file() for p in paths.values())


def test_an_october_price_is_shown_stale_with_its_own_record_date_not_as_a_later_observation(
    tmp_path,
) -> None:
    result, report = scenario(tmp_path)
    for row in result.top:
        row["shopee_price_observed_at"] = "2026-10-01"
        row["shopee_price_date_basis"] = "M_MINUS_1_RECORD_DATE"
        row["price_period_month"] = "2026-10"
    reports = build_reports(result, report)

    first = reports.comparison[0]
    assert first["shopee_price_php"] == "48990.00"
    assert first["shopee_price_status"] == SNAPSHOT_PRICE_STALE
    assert first["shopee_price_observed_at"] == "2026-10-01"  # never relabelled
    assert "M-1 2026-10 source record of 2026-10-01" in first["shopee_price_detail"]
    assert "not an observation of a later month" in first["shopee_price_detail"]
