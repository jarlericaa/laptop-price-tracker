# ruff: noqa: F401, F811  (the ``world`` fixture is imported, then named as a test parameter)
"""PRICE COMPARISON, the business-facing tab (layout variation-business-1, 2026-11-12).

Unit tests of the display rules on report-shaped rows, then the whole publisher against the fake
workbook: one visible tab, the technical tabs hidden, the same rows in the same order, links,
formats and idempotency.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from test_sheet_publication import FakeSpreadsheet
from test_variation_publication import (
    NOW,
    SpyingSpreadsheet,
    latest_report,
    run,
    template_tabs,
    world,
)

from laptop_tracker import price_comparison_view as pv
from laptop_tracker.result_labels import retailer_result
from laptop_tracker.sheet_publication import NUMBER_FORMATS, STATUS_FILLS, DestinationError
from laptop_tracker.variation_publication import (
    COMPARISON_TAB,
    DATA_TABS,
    OVERVIEW_TAB,
    PUBLISHER_TABS,
    TECHNICAL_TABS,
    TOP_TAB,
    build_variation_plan,
    check_destinations,
    load_contract,
    load_variation_report,
)

PRICE_TAB = pv.PRICE_TAB
CODES = [code for code, _ in pv.RETAILER_COLUMNS]

# ---- display rules on report-shaped rows ----


def row(**fields: str) -> dict[str, str]:
    """A comparison row with a trusted context and no retailer result."""
    base = {"context_id": "CTX-1", "comparison_row_id": "SV-1", "shopee_price_php": ""}
    for code in CODES:
        base |= {
            f"{code}_Price": "",
            f"{code}_Price_Status": "",
            f"{code}_Stock": "",
            f"{code}_Offer_URL": "",
            f"{code}_Comparison_Tag": "",
            f"{code}_Match_Basis": "",
        }
    return base | fields


def priced(code: str, price: str, stock: str = "IN_STOCK", **extra: str) -> dict[str, str]:
    return {
        f"{code}_Price": price,
        f"{code}_Price_Status": "SELECTED",
        f"{code}_Stock": stock,
        f"{code}_Offer_URL": f"https://{code.lower()}.example/p",
        f"{code}_Comparison_Tag": "MATCH",
    } | extra


def shown(r: dict[str, str], code: str = "DBZ") -> str:
    result = retailer_result(r, code)
    return pv.display_text(pv.retailer_cell(r, code, "Retailer", result)[0])


def test_the_visible_columns_are_exactly_the_specified_nineteen_in_order() -> None:
    assert pv.PRICE_HEADER == (
        "Rank", "Shopee Seller", "Laptop Model", "Variation", "CPU", "RAM", "Storage", "GPU",
        "Shopee Price", "DataBlitz", "PC Express", "Laptop Factory", "PC Central", "Octagon",
        "Lowest Retailer", "Lowest Price", "Price Gap (₱)", "Price Gap (%)", "Competitiveness",
    )  # fmt: skip
    # Product Details (A-I) -> Retailer Prices (J-N) -> Price Competitiveness (O-S)
    assert [(label, a, b) for label, a, b in pv.SECTIONS] == [
        ("Product Details", 0, 9),
        ("Retailer Prices", 9, 14),
        ("Price Competitiveness", 14, 19),
    ]
    assert load_contract().headers[PRICE_TAB] == pv.PRICE_HEADER


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        # a verified matching price in stock, and out of stock (shown, labelled)
        (priced("DBZ", "54950.0"), "₱54,950"),
        (priced("DBZ", "52990.0", "OUT_OF_STOCK"), "₱52,990 (OOS)"),
        (priced("DBZ", "52990.50"), "₱52,990.50"),  # centavos are never rounded away
        # a close alternative is a different configuration: never a price
        (
            {
                "DBZ_Price": "50000.0",
                "DBZ_Price_Status": "ALTERNATIVE_SELECTED",
                "DBZ_Stock": "IN_STOCK",
                "DBZ_Comparison_Tag": "ALT 1",
            },
            "No match",
        ),
        # the search finished without a verified matching configuration
        ({"DBZ_Price_Status": "NO_RELEVANT_CANDIDATE"}, "No match"),
        ({"DBZ_Price_Status": "NO_CANDIDATE_FOUND"}, "No match"),
        # a candidate or an excluded matching offer needs verification
        ({"DBZ_Price_Status": "CANDIDATE_NOT_CONFIRMED"}, "Review"),
        ({"DBZ_Price_Status": "REVIEW_REQUIRED_ONLY"}, "Review"),
        ({"DBZ_Price_Status": "NO_COMPARABLE_OFFER"}, "Review"),
        ({"DBZ_Price_Status": "CATALOG_IDENTITY_UNRESOLVED"}, "Review"),
        # the search or observation never completed
        ({"DBZ_Price_Status": "CANDIDATE_NOT_FETCHED"}, "Not checked"),
        ({"DBZ_Price_Status": "FETCH_FAILED"}, "Not checked"),
        ({"DBZ_Price_Status": "SEARCH_FAILED"}, "Not checked"),
        ({"DBZ_Price_Status": "DISCOVERY_INCOMPLETE"}, "Not checked"),
        ({"DBZ_Price_Status": "DISCOVERY_NOT_ATTEMPTED"}, "Not checked"),
        ({"DBZ_Price_Status": "NO_OBSERVATION_IN_WEEK"}, "Not checked"),
        ({}, "Not checked"),  # no record at all
    ],
)
def test_a_retailer_cell_shows_a_price_only_for_a_verified_matching_configuration(
    fields, expected
) -> None:
    assert shown(row(**fields)) == expected


def test_a_row_without_a_trusted_context_compares_no_retailer() -> None:
    assert shown(row(context_id="", **priced("DBZ", "1000.0"))) == "—"


def test_missing_data_is_not_collapsed_into_no_match() -> None:
    """The three short labels stay distinct for the distinct situations."""
    labels = {
        shown(row(DBZ_Price_Status=status))
        for status in ("NO_RELEVANT_CANDIDATE", "CANDIDATE_NOT_CONFIRMED", "SEARCH_FAILED")
    }
    assert labels == {"No match", "Review", "Not checked"}


CAPPED_NOTE = (
    "NO_RELEVANT_CANDIDATE: 3 search page(s) over 3 query tier(s) returned 10 result(s), none "
    "showing this configuration's model code: UNRELATED x10; 1 result page(s) at the retailer's "
    "result cap or with unread next pages; the query plan ran, but the retailer's catalog was not "
    "exhaustively searched; discovery ended: QUERY_PLAN_EXHAUSTED; search coverage "
    "RESULT_CAP_REACHED"
)
FALLBACK_OFF_NOTE = (
    "NO_RELEVANT_CANDIDATE: 7 search page(s) over 7 query tier(s) returned 8 result(s), none "
    "showing this configuration's model code: DIFFERENT_MODEL_CODE x8; discovery ended: "
    "QUERY_PLAN_EXHAUSTED; search coverage FALLBACK_NOT_TRIED_DISABLED"
)


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        # a search that hit the result cap has not shown that no match exists
        ("NO_RELEVANT_CANDIDATE", CAPPED_NOTE, "Review"),
        ("NO_CANDIDATE_FOUND", CAPPED_NOTE, "Review"),
        ("NO_RELEVANT_CANDIDATE", "result page(s) with unread next pages", "Review"),
        ("NO_RELEVANT_CANDIDATE", "discovery ended: BUDGET_EXHAUSTED", "Review"),
        ("NO_CANDIDATE_FOUND", "search coverage FALLBACK_NOT_TRIED_BUDGET", "Review"),
        # a finished query plan (the optional fallback being off is a configuration choice)
        ("NO_RELEVANT_CANDIDATE", FALLBACK_OFF_NOTE, "No match"),
        ("NO_RELEVANT_CANDIDATE", "discovery ended: QUERY_PLAN_EXHAUSTED", "No match"),
        ("NO_CANDIDATE_FOUND", "", "No match"),
        # the fallback being off does not hide a cap recorded beside it
        (
            "NO_RELEVANT_CANDIDATE",
            FALLBACK_OFF_NOTE + "; 1 result page(s) at the retailer's result cap",
            "Review",
        ),
        # an incomplete or failed search stays Not checked, never Review or No match
        ("DISCOVERY_INCOMPLETE", CAPPED_NOTE, "Not checked"),
        ("SEARCH_FAILED", "", "Not checked"),
    ],
)
def test_a_capped_or_cut_short_search_is_review_and_a_finished_one_is_no_match(
    status, reason, expected
) -> None:
    assert shown(row(DBZ_Price_Status=status, DBZ_Reason=reason)) == expected


def test_a_capped_search_cannot_demote_a_verified_price_or_promote_a_close_alternative() -> None:
    # A verified matching offer is a price whatever else the search left unread.
    assert shown(row(**priced("DBZ", "54950.0", DBZ_Reason=CAPPED_NOTE))) == "₱54,950"
    # A close alternative is a different configuration (No match); capped, it is uncertain.
    alt = {
        "DBZ_Price": "50000.0",
        "DBZ_Price_Status": "ALTERNATIVE_SELECTED",
        "DBZ_Stock": "IN_STOCK",
        "DBZ_Comparison_Tag": "ALT 1",
    }
    assert shown(row(**alt)) == "No match"
    assert shown(row(**alt, DBZ_Reason=CAPPED_NOTE)) == "Review"
    # Never a price, never a link, and no repeated note: the header says what Review means.
    r = row(DBZ_Price_Status="NO_RELEVANT_CANDIDATE", DBZ_Reason=CAPPED_NOTE)
    cell, shows_price = pv.retailer_cell(r, "DBZ", "DataBlitz", retailer_result(r, "DBZ"))
    assert not shows_price and cell.kind == "text" and cell.link == ""
    assert cell.value == "Review" and cell.fill == "d_review" and cell.note == ""


def test_a_displayed_price_stays_a_number_with_its_words_in_the_format() -> None:
    r = row(**priced("PCX", "52990.0", "OUT_OF_STOCK"))
    cell, shows_price = pv.retailer_cell(r, "PCX", "PC Express", retailer_result(r, "PCX"))
    assert shows_price and cell.kind == "number" and Decimal(cell.value) == Decimal("52990")
    assert NUMBER_FORMATS[cell.number_format]["pattern"] == '"₱"#,##0" (OOS)"'
    assert NUMBER_FORMATS[cell.number_format]["type"] == "CURRENCY"
    assert cell.link == "https://pcx.example/p"  # the verified offer's page
    # A verified price without a usable link is kept, and no link is invented.
    r2 = row(**priced("PCX", "52990.0") | {"PCX_Offer_URL": ""})
    cell2, _ = pv.retailer_cell(r2, "PCX", "PC Express", retailer_result(r2, "PCX"))
    assert cell2.kind == "number" and cell2.link == ""


def test_a_status_cell_never_links_to_a_candidate() -> None:
    r = row(
        DBZ_Price_Status="CANDIDATE_NOT_CONFIRMED",
        DBZ_Candidate_URL="https://dbz.example/unconfirmed",
        DBZ_Offer_URL="https://dbz.example/unconfirmed",
    )
    cell, shows_price = pv.retailer_cell(r, "DBZ", "DataBlitz", retailer_result(r, "DBZ"))
    assert not shows_price and cell.kind == "text" and cell.link == ""


# ---- the lowest in-stock matching price ----


def lowest(**fields: str) -> dict[str, str]:
    empty = {
        "Lowest_In_Stock_Price": "",
        "Lowest_In_Stock_Price_Retailers": "",
        "Lowest_In_Stock_Offer_URL": "",
    }
    return row(**(empty | fields))


def test_the_lowest_price_is_the_reports_in_stock_matching_minimum() -> None:
    r = lowest(
        Lowest_In_Stock_Price="54950.0",
        Lowest_In_Stock_Price_Retailers="DBZ",
        Lowest_In_Stock_Offer_URL="https://dbz.example/p",
        **priced("DBZ", "54950.0"),
    )
    offer = pv.lowest_in_stock(r)
    assert offer and offer.names == ("DataBlitz",) and offer.price == "54950.0"
    assert offer.link == "https://dbz.example/p"


def test_tied_lowest_retailers_are_listed_together_in_visible_column_order() -> None:
    # The report lists ties in its own order (DBZ, LF, PCX ...); the tab uses its column order.
    r = lowest(
        Lowest_In_Stock_Price="50000.0",
        Lowest_In_Stock_Price_Retailers="LF;DBZ;PCX",
        Lowest_In_Stock_Offer_URL="https://lf.example;https://dbz.example;https://pcx.example",
    )
    offer = pv.lowest_in_stock(r)
    assert offer.names == ("DataBlitz", "PC Express", "Laptop Factory")
    assert offer.link == "https://dbz.example"  # the first winner in column order


def test_no_in_stock_matching_offer_means_no_lowest_price_and_no_comparison() -> None:
    # Only an out-of-stock matching price exists: the report's in-stock minimum is empty, so the
    # tab shows no lowest price and never falls back to the out-of-stock one.
    r = lowest(shopee_price_php="50000.0", **priced("DBZ", "40000.0", "OUT_OF_STOCK"))
    assert pv.lowest_in_stock(r) is None
    names, price, gap, pct, label = pv.competitiveness_cells(r, None, lowest_note="")
    assert [pv.display_text(c) for c in (names, price, gap, pct, label)] == [
        "—", "—", "—", "—", "No comparison",
    ]  # fmt: skip
    assert price.kind == "text" and gap.kind == "text"  # no zero is ever shown


def test_the_report_cannot_name_a_winner_that_is_not_a_selected_matching_price() -> None:
    bad = lowest(Lowest_In_Stock_Price="1.0", Lowest_In_Stock_Price_Retailers="DBZ")
    assert any("no selected matching price" in i for i in pv.lowest_in_stock_issues(bad))
    half = lowest(Lowest_In_Stock_Price="1.0")
    assert any("disagree" in i for i in pv.lowest_in_stock_issues(half))


def test_an_in_stock_matching_price_below_the_minimum_is_a_contradiction() -> None:
    r = lowest(
        Lowest_In_Stock_Price="60000.0",
        Lowest_In_Stock_Price_Retailers="PCX",
        **priced("PCX", "60000.0"),
        **priced("LF", "55000.0"),
    )
    assert any("below the minimum" in i for i in pv.lowest_in_stock_issues(r))
    # An out-of-stock cell below the minimum is fine (it never competes).
    ok = lowest(
        Lowest_In_Stock_Price="60000.0",
        Lowest_In_Stock_Price_Retailers="PCX",
        **priced("PCX", "60000.0"),
        **priced("LF", "55000.0", "OUT_OF_STOCK"),
    )
    assert pv.lowest_in_stock_issues(ok) == []


# ---- price gap and competitiveness ----


@pytest.mark.parametrize(
    ("shopee", "retailer", "label", "gap", "percent"),
    [
        ("56390.0", "54950.0", "Retailer cheaper", "1440.0", "0.0262"),  # +₱1,440, +2.62%
        ("48990.0", "49999.0", "Shopee cheaper", "-1009.0", "-0.0202"),  # -₱1,009, -2.02%
        ("50000.0", "50000.0", "Same price", "0.0", "0.0000"),
    ],
)
def test_the_gap_is_shopee_minus_the_lowest_retailer_over_the_retailer_price(
    shopee, retailer, label, gap, percent
) -> None:
    result = pv.compare(shopee, retailer)
    assert result.label == label
    assert result.gap == Decimal(gap) and result.percent == Decimal(percent)


def test_the_percentage_uses_the_retailer_price_as_its_denominator() -> None:
    # 1000 over 2000 is 50 %; over the Shopee price it would be 33.33 %.
    assert pv.compare("3000", "2000").percent == Decimal("0.5000")
    assert pv.compare("1000", "2000").percent == Decimal("-0.5000")


def test_a_tiny_gap_keeps_its_direction_after_the_percentage_rounds_to_zero() -> None:
    result = pv.compare("42990", "42995")  # -0.0116 % rounds to -0.01 %
    assert result.label == "Shopee cheaper" and result.gap == Decimal("-5")
    assert result.percent == Decimal("-0.0001")


@pytest.mark.parametrize(
    ("shopee", "retailer"),
    [
        ("", "50000"),
        ("50000", ""),
        ("", ""),
        ("0", "50000"),
        ("50000", "0"),
        ("x", "5"),
        ("-1", "5"),
    ],
)
def test_a_missing_or_invalid_price_is_no_comparison_never_zero(shopee, retailer) -> None:
    result = pv.compare(shopee, retailer)
    assert result.label == "No comparison" and result.gap is None and result.percent is None


def test_the_competitiveness_cells_format_and_colour_the_result() -> None:
    r = row(shopee_price_php="56390.0")
    offer = pv.LowestOffer("54950.0", ("DataBlitz",), "https://dbz.example/p")
    names, price, gap, pct, label = pv.competitiveness_cells(r, offer, lowest_note="n")
    assert [pv.display_text(c) for c in (names, price, gap, pct, label)] == [
        "DataBlitz", "₱54,950", "+₱1,440", "+2.62%", "Retailer cheaper",
    ]  # fmt: skip
    assert price.link == "https://dbz.example/p"
    assert gap.kind == pct.kind == "number" and gap.value == "1440.0"  # numeric, not a string
    assert label.fill == "d_cmp_red" and gap.fill == pct.fill == "d_gap_red"
    cheaper = pv.competitiveness_cells(
        row(shopee_price_php="48990.0"), pv.LowestOffer("49999.0", ("PC Central",), ""),
        lowest_note="",
    )  # fmt: skip
    assert [pv.display_text(c) for c in cheaper[2:]] == ["-₱1,009", "-2.02%", "Shopee cheaper"]
    assert cheaper[4].fill == "d_cmp_green" and cheaper[2].fill == cheaper[3].fill == "d_gap_green"
    same = pv.competitiveness_cells(
        row(shopee_price_php="1000"), pv.LowestOffer("1000", ("Octagon",), ""), lowest_note=""
    )
    assert [pv.display_text(c) for c in same[2:]] == ["₱0", "0.00%", "Same price"]
    assert same[4].fill == "d_cmp_same" and same[2].fill == same[3].fill == "d_gap_same"
    none = pv.competitiveness_cells(row(shopee_price_php="1000"), None, lowest_note="")
    # With no comparison only the label is slate; the gaps are quiet dashes, never zero.
    assert none[4].fill == "d_cmp_none" and none[2].fill == none[3].fill == "d_dash"
    assert {"d_cmp_green", "d_cmp_red", "d_cmp_same", "d_cmp_none"} <= set(STATUS_FILLS)


# ---- the Shopee price and its stale warning ----


def test_a_stale_shopee_price_is_flagged_beside_the_price_and_still_compared() -> None:
    r = row(
        shopee_price_php="49990.0",
        shopee_listing_url="https://shopee.example/i",
        shopee_price_status="SNAPSHOT_PRICE_STALE",
    )
    cell = pv.shopee_cell(r, stale=True, note="n")
    assert pv.display_text(cell) == "₱49,990 ⚠ Stale" and cell.kind == "number"
    assert cell.fill == "d_stale" and cell.link == "https://shopee.example/i"
    assert Decimal(cell.value) == Decimal("49990")  # the price itself is untouched
    assert pv.compare("49990.0", "46990.0").label == "Retailer cheaper"  # still compared
    fresh = pv.shopee_cell(r, stale=False, note="n")
    assert pv.display_text(fresh) == "₱49,990" and fresh.fill == ""
    assert pv.display_text(pv.shopee_cell(row(), stale=False, note="")) == "—"


# ---- product detail text ----


@pytest.mark.parametrize(
    ("series", "model", "expected"),
    [
        ("Core i5", "i5-13420H", "Intel Core i5-13420H"),
        ("Core i3", "i3-1315U", "Intel Core i3-1315U"),
        ("Ryzen 7", "Ryzen 7 7730U", "AMD Ryzen 7 7730U"),
        ("Ryzen 5", "R5-3450U", "AMD Ryzen 5 3450U"),
        ("Ryzen 7", "Ryzen 7 PRO 4750U", "AMD Ryzen 7 PRO 4750U"),
        ("Apple M5", "M5", "Apple M5"),
        ("Apple A18 Pro", "A18 Pro", "Apple A18 Pro"),
        ("Core i5", "Intel i5", "Intel Core i5"),  # the model adds nothing: no invented number
        ("Core 3", "Core 3", "Intel Core 3"),
        ("Intel N150", "", "Intel N150"),
        ("Celeron N4020", "", "Intel Celeron N4020"),
        ("Celeron N4000", "N4000", "Intel Celeron N4000"),
        ("Ryzen AI 5", "", "AMD Ryzen AI 5"),
        ("Snapdragon X", "X1-26-100", "Snapdragon X X1-26-100"),
        ("", "", ""),  # nothing recorded: nothing shown
    ],
)
def test_the_cpu_reads_the_recorded_family_and_model_without_inventing_either(
    series, model, expected
) -> None:
    assert pv.cpu_display(series, model) == expected


def test_ram_storage_gpu_and_model_names_are_short_and_never_invented() -> None:
    assert pv.ram_display("16") == "16GB" and pv.ram_display("") == ""
    assert [pv.storage_display(v) for v in ("512", "1024", "1000", "2048", "256", "")] == [
        "512GB", "1TB", "1TB", "2TB", "256GB", "",
    ]  # fmt: skip
    assert pv.gpu_display("RTX4050") == "RTX 4050" and pv.gpu_display("RTX 3050") == "RTX 3050"
    assert pv.gpu_display("") == "" and pv.gpu_display("Radeon 610M") == "Radeon 610M"
    assert pv.model_display("HP", "", "HP Laptop 14") == "HP Laptop 14"
    assert pv.model_display("Acer", "Aspire", "Aspire Lite") == "Acer Aspire Lite"
    assert pv.model_display("", "", "", "Listing title") == "Listing title"


# ---- the publisher against the fake workbook ----


def published(world, **kwargs):
    code, fake, _ = run(world, **kwargs)
    assert code == 0, latest_report(world)
    return fake


def grid(fake, title: str = PRICE_TAB) -> list[list[dict]]:
    sheet = fake.sheets[title]
    height = max(r for r, _ in sheet["grid"]) + 1
    width = len(pv.PRICE_HEADER)
    return [[sheet["grid"].get((r, c), {}) for c in range(width)] for r in range(height)]


def test_one_visible_business_tab_is_created_and_the_technical_tabs_are_hidden(world) -> None:
    fake = SpyingSpreadsheet(template_tabs())
    fake = published(world, fake=fake)

    assert [m for m in fake.mutations if m.startswith("add_worksheet")] == [
        f"add_worksheet:{PRICE_TAB}"
    ]
    assert list(fake.sheets).count(PRICE_TAB) == 1
    assert PRICE_TAB in fake.visible_tabs()
    for title in TECHNICAL_TABS:
        assert fake.sheets[title]["tab_hidden"], f"{title} must be hidden"
    # The workbook's other tabs keep whatever visibility they had.
    for title in ("raw", "TARGET_MASTER", "ARCHIVE_PILOT_PRICE_COMPARISON_20261027"):
        assert not fake.sheets[title]["tab_hidden"]
    assert latest_report(world)["status"] == "SUCCESS"
    # It sits next to the pilot tabs (before the Top list), not at the end of the workbook.
    order = list(fake.sheets)
    assert order.index(PRICE_TAB) == order.index(TOP_TAB) - 1


def test_the_business_tab_carries_the_header_banner_and_dynamic_reporting_dates(world) -> None:
    fake = published(world, fake=SpyingSpreadsheet(template_tabs()))
    values = fake.values(PRICE_TAB)
    assert values[0][0] == "LAPTOP PRICE COMPARISON"
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    last_day = report.week_end - timedelta(days=1)
    from laptop_tracker.variation_publication import cutoff_label, week_label

    assert values[1][0] == (
        f"Week: {week_label(report.week_start, last_day)}  |  "
        f"Prices as of: {cutoff_label(report.as_of)}"
    )
    # The timestamp is the report cutoff, so it is labelled as such (never "Last Updated").
    assert "Prices as of: " in values[1][0] and values[1][0].endswith("SGT")
    assert "Last Updated" not in values[1][0] and "Coverage" not in values[1][0]
    assert [v for v in values[2] if v] == [
        "Product Details", "Retailer Prices", "Price Competitiveness",
    ]  # fmt: skip
    assert values[3] == list(pv.PRICE_HEADER)  # the table header follows immediately
    assert len(values[4:]) == len(report.top)
    # Nothing but the three rows above the table: no KPI, summary or explanatory text.
    assert all(not any(v) for v in values[0][1:]) and all(not any(v) for v in values[1][1:])


def test_one_row_per_selected_variation_in_the_top_lists_order_with_its_rank(world) -> None:
    fake = published(world, fake=SpyingSpreadsheet(template_tabs()))
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    top_ranks = [r["rank"] for r in report.top]
    tab = fake.values(PRICE_TAB)[4:]
    assert [row_[0] for row_ in tab] == [f"{float(rank)!r}" for rank in top_ranks]
    assert len(tab) == len(report.top) == len(report.comparison)
    top_tab = fake.values(TOP_TAB)
    top_header = top_tab[0]
    assert [r[top_header.index("comparison_row_id")] for r in top_tab[1:]] == [
        r["comparison_row_id"] for r in report.top
    ]


def test_the_plan_does_not_change_the_selected_rows(world) -> None:
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    before = copy.deepcopy((report.top, report.comparison, report.selections))
    plan = build_variation_plan(report, load_contract())
    assert (report.top, report.comparison, report.selections) == before
    assert len(plan.tabs[PRICE_TAB].rows) == len(report.top)  # nothing grouped or dropped


def test_prices_stay_numeric_links_are_kept_and_numbers_are_not_text(world) -> None:
    fake = published(world, fake=SpyingSpreadsheet(template_tabs()))
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    cells = grid(fake)[4:]
    index = {name: i for i, name in enumerate(pv.PRICE_HEADER)}
    saw_price = saw_link = 0
    for source, row_cells in zip(report.comparison, cells, strict=True):
        shopee = row_cells[index["Shopee Price"]]
        assert "numberValue" in shopee["userEnteredValue"]
        assert shopee["userEnteredFormat"]["numberFormat"]["type"] == "CURRENCY"
        assert shopee.get("hyperlink") == source["shopee_listing_url"]
        for code, name in pv.RETAILER_COLUMNS:
            cell = row_cells[index[name]]
            value = cell.get("userEnteredValue", {})
            if source[f"{code}_Price"] and source[f"{code}_Price_Status"] == "SELECTED":
                saw_price += 1
                assert Decimal(repr(value["numberValue"])) == Decimal(source[f"{code}_Price"])
                assert cell.get("hyperlink") == source[f"{code}_Offer_URL"]
                saw_link += bool(cell.get("hyperlink"))
            else:
                assert "numberValue" not in value  # a status label, never a number
                assert not cell.get("hyperlink")
        # the gap cells are real numbers too
        gap = row_cells[index["Price Gap (₱)"]].get("userEnteredValue", {})
        assert ("numberValue" in gap) == (
            row_cells[index["Competitiveness"]]["userEnteredValue"]["stringValue"]
            != "No comparison"
        )
    assert saw_price and saw_link


def test_the_published_tab_has_no_filter_rules_groups_or_merges_and_freezes_the_header(
    world,
) -> None:
    fake = published(world, fake=SpyingSpreadsheet(template_tabs()))
    sheet = fake.sheets[PRICE_TAB]
    assert sheet["filter"] is None and sheet["cf"] == [] and sheet["groups"] == []
    assert sheet["merges"] == []
    assert (sheet["frozen"], sheet["frozen_columns"]) == (4, 3)  # title rows + header; 3 id columns
    assert sheet["gridlines_hidden"]
    assert len(sheet["widths"]) == 19


def test_competitiveness_cells_are_the_only_emphasised_result_cells(world) -> None:
    fake = published(world, fake=SpyingSpreadsheet(template_tabs()))
    layout = fake.sheets[PRICE_TAB]["layout"]
    index = {name: i for i, name in enumerate(pv.PRICE_HEADER)}

    def background(r: int, c: int):
        return layout.get((r, c), {}).get("backgroundColor")

    for n in range(len(fake.values(PRICE_TAB)[4:])):
        r = 4 + n
        label = fake.values(PRICE_TAB)[r][index["Competitiveness"]]
        expected = STATUS_FILLS[pv.COMPETITIVENESS_FILL[label]]
        colour = background(r, index["Competitiveness"])
        assert all(abs(colour[k] - expected[k]) < 0.002 for k in ("red", "green", "blue"))
        # retailer cells are never one of the result colours
        for _, name in pv.RETAILER_COLUMNS:
            retailer = background(r, index[name]) or {}
            for fill in ("d_cmp_green", "d_cmp_red", "d_gap_green", "d_gap_red", "match", "alt1"):
                want = STATUS_FILLS[fill]
                assert not (retailer and all(abs(retailer[k] - want[k]) < 0.002 for k in want)), (
                    f"retailer cell coloured {fill}"
                )


def test_republishing_is_idempotent_and_never_duplicates_tabs_formats_or_headers(world) -> None:
    fake = published(world, fake=SpyingSpreadsheet(template_tabs()))
    first = (fake.values(PRICE_TAB), list(fake.sheets), copy.deepcopy(fake.sheets[PRICE_TAB]))
    tabs = {t: fake.values(t) for t in DATA_TABS}
    fake = published(world, fake=fake, now=NOW + timedelta(minutes=7))

    assert list(fake.sheets) == first[1]  # no tab added or removed
    assert fake.values(PRICE_TAB) == first[0]  # the same table, one header
    assert sum(r[0] == "Rank" for r in fake.values(PRICE_TAB)) == 1
    assert fake.sheets[PRICE_TAB]["cf"] == [] and fake.sheets[PRICE_TAB]["filter"] is None
    assert {t: fake.values(t) for t in DATA_TABS} == tabs
    assert [m for m in fake.mutations if m.startswith("add_worksheet")] == [
        f"add_worksheet:{PRICE_TAB}"
    ]  # created once, ever
    for title in TECHNICAL_TABS:
        assert fake.sheets[title]["tab_hidden"]  # still hidden, never re-shown
    assert fake.visible_tabs().count(PRICE_TAB) == 1


def test_an_existing_business_tab_is_replaced_in_place_not_recreated_or_appended(world) -> None:
    tabs = template_tabs()
    tabs[PRICE_TAB] = [[], [], [], list(pv.PRICE_HEADER), ["9", "Old seller"]]
    fake = published(world, fake=SpyingSpreadsheet(tabs))
    assert not any(m.startswith("add_worksheet") for m in fake.mutations)
    values = fake.values(PRICE_TAB)
    assert "Old seller" not in sum(values, [])  # earlier cells replaced, not appended to
    assert sum(r[0] == "Rank" for r in values) == 1


def test_a_business_tab_name_holding_someone_elses_content_is_refused(world, capsys) -> None:
    tabs = template_tabs()
    tabs[PRICE_TAB] = [["Our own notes"], ["do not overwrite"]]
    code, fake, _ = run(world, fake=SpyingSpreadsheet(tabs))
    assert code == 1 and fake.mutations == []
    assert "live header differs from the contract" in capsys.readouterr().err


def test_visibility_requests_only_touch_this_publishers_tabs(world) -> None:
    fake = published(world, fake=SpyingSpreadsheet(template_tabs()))
    for title in ("raw", "TARGET_MASTER"):
        assert not fake.sheets[title]["tab_hidden"]
    assert {t for t, s in fake.sheets.items() if s["tab_hidden"]} == set(TECHNICAL_TABS)


def test_the_tab_stays_as_it_was_when_the_readback_fails(world) -> None:
    fake = SpyingSpreadsheet(template_tabs())
    state = {"done": False}

    def corrupt(sheet: FakeSpreadsheet) -> None:
        # after the business tab is written, damage one of its cells
        cells = sheet.sheets.get(PRICE_TAB, {}).get("grid", {})
        if not state["done"] and (4, 0) in cells and cells[(4, 0)]:
            cells[(4, 0)] = {"userEnteredValue": {"numberValue": 999.0}}
            state["done"] = True

    fake.after_batch = corrupt
    code, fake, _ = run(world, fake=fake)
    report = latest_report(world)
    assert code == 1 and report["status"] == "READBACK_FAILED"
    # No visibility switch: the tabs people were using stay visible until a verified publication,
    # and the new (unverified) business tab was created hidden and stays hidden.
    assert not any(fake.sheets[t]["tab_hidden"] for t in TECHNICAL_TABS)
    assert fake.sheets[PRICE_TAB]["tab_hidden"] and PRICE_TAB not in fake.visible_tabs()
    assert state["done"]


def test_rollback_restores_the_earlier_visibility_of_the_technical_tabs(world, tmp_path) -> None:
    fake = SpyingSpreadsheet(template_tabs())
    published(world, fake=fake)
    assert all(fake.sheets[t]["tab_hidden"] for t in TECHNICAL_TABS)
    folder = sorted(world["out"].iterdir())[-1]
    from test_verified_top import load_script

    code = load_script("publish_variation_prices").main(
        ["--rollback", str(folder), "--output-root", str(world["out"])],
        spreadsheet_factory=lambda read_only: fake,
        now=NOW + timedelta(minutes=9),
    )
    assert code == 0
    # The tabs backed up were visible before the publication, so they are visible again. The run
    # log is never restored (it is the record of publications) and a tab the publication created
    # has no earlier state: both are left as they are.
    restored = [t for t in TECHNICAL_TABS if t != "PILOT_PRICE_RUN_LOG"]
    assert not any(fake.sheets[t]["tab_hidden"] for t in restored)
    assert fake.sheets["PILOT_PRICE_RUN_LOG"]["tab_hidden"]
    assert PRICE_TAB in fake.sheets and PRICE_TAB in fake.visible_tabs()


def test_hidden_technical_tabs_keep_their_data_and_headers(world) -> None:
    fake = published(world, fake=SpyingSpreadsheet(template_tabs()))
    # The business tab replaces nothing: the technical comparison keeps its compact layout.
    header = fake.values(COMPARISON_TAB)[2]
    assert header[:3] == ["Rank", "Laptop", "Shopee seller"] and len(header) == 140
    assert fake.values(OVERVIEW_TAB)[2][0] == "Rank"
    assert len(fake.values(TOP_TAB)) == 13  # header + the 12 selected variations


def test_the_preview_lists_the_new_tab_as_to_be_created_and_writes_nothing(world, capsys) -> None:
    code, fake, opened = run(world, mode="--preview", fake=SpyingSpreadsheet(template_tabs()))
    assert code == 0 and opened == [True] and fake.mutations == []
    printed = capsys.readouterr().out
    assert "PRICE COMPARISON: does not exist yet; will be created" in printed
    folder = sorted(world["out"].iterdir())[-1]
    preview = (folder / "preview_PRICE_COMPARISON.csv").read_text(encoding="utf-8").splitlines()
    assert preview[0].startswith("LAPTOP PRICE COMPARISON") and preview[3].startswith("Rank,")
    assert (
        json.loads((folder / "publication_report.json").read_text())["planned_rows"][PRICE_TAB]
        == 12
    )


def test_the_new_tab_is_the_only_one_the_publisher_may_create(world) -> None:
    states = {t: pv_state(t, exists=False) for t in PUBLISHER_TABS}
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    plan = build_variation_plan(report, load_contract())
    problems, _ = check_destinations(states, plan)
    assert len(problems) == len(PUBLISHER_TABS) - 1
    assert not any(PRICE_TAB in p for p in problems)
    assert all("never created" in p for p in problems)
    with pytest.raises(DestinationError):
        from laptop_tracker.sheet_publication import assert_publisher_tab

        assert_publisher_tab("Some other tab", PUBLISHER_TABS)


def pv_state(title: str, *, exists: bool):
    from laptop_tracker.sheet_publication import TabState

    return TabState(title, exists)


def test_a_failure_while_showing_and_hiding_tabs_is_reported_and_changes_no_visibility(
    world,
) -> None:
    fake = SpyingSpreadsheet(template_tabs())
    # batches: the run log (1), the seven data tabs (2-8), then the visibility change (9)
    fake.fail_on_batch = 1 + len(DATA_TABS) + 1
    code, fake, _ = run(world, fake=fake)
    report = latest_report(world)
    assert code == 1 and report["status"] == "READBACK_FAILED"
    assert any("Tab visibility could not be applied" in i for i in report["readback_issues"])
    assert not any(fake.sheets[t]["tab_hidden"] for t in TECHNICAL_TABS)
    assert PRICE_TAB not in fake.visible_tabs()  # verified data, but never shown half-switched
    # The data itself was written and verified; the run log records the unfinished publication.
    assert len(fake.values(PRICE_TAB)) == 4 + 12
    status = [r for r in fake.values("PILOT_PRICE_RUN_LOG")[-1]]
    assert "READBACK_FAILED" in status
    # The same command repairs it: still one business tab, now visible and the rest hidden.
    fake.fail_on_batch = None
    code, fake, _ = run(world, fake=fake, now=NOW + timedelta(minutes=6))
    assert code == 0 and latest_report(world)["status"] == "SUCCESS"
    assert all(fake.sheets[t]["tab_hidden"] for t in TECHNICAL_TABS)
    assert list(fake.sheets).count(PRICE_TAB) == 1


def test_the_tracker_guide_text_points_at_the_business_tab() -> None:
    from laptop_tracker.sheet_guide import guide_entries

    entries = dict(
        guide_entries(
            layout_version="variation-business-1",
            publication_id="PUB-x",
            week="2026-11-09 to 2026-11-15",
            cutoff="2026-11-10 19:07",
            rows=100,
            published="2026-11-12",
        )
    )
    assert "PRICE COMPARISON is the business view" in entries["Current results"]
    assert "hidden technical" in entries["Compact view"].lower()
    for phrase in ("Price Gap", "(OOS)", "No match", "Review", "Not checked", "Stale"):
        assert phrase in entries["Price comparison"]
