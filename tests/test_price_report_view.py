# ruff: noqa: F401, F811  (the ``world`` fixture is imported, then named as a test parameter)
"""PRICE REPORT, the detailed side-by-side business tab (layout variation-business-2, 2026-11-12).

Unit tests of the short result labels and the coverage text, then the plan and the publisher on a
report whose first rows are given every situation the report can hold (a match in stock and out of
stock, a tie, a close alternative in and out of stock, a capped search, a candidate to review).
The fake workbook shows what Google would store: values, links, number formats and fills.
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
from laptop_tracker import price_report_view as rv
from laptop_tracker.result_labels import retailer_result
from laptop_tracker.sheet_publication import NUMBER_FORMATS, STATUS_FILLS
from laptop_tracker.variation_publication import (
    BUSINESS_TABS,
    COMPARISON_TAB,
    PRICE_TAB,
    REPORT_TAB,
    TECHNICAL_TABS,
    build_variation_plan,
    load_contract,
    load_variation_report,
    publish_variation,
    report_view_issues,
)

CODES = [code for code, _ in rv.REPORT_RETAILERS]
NAMES = dict(rv.REPORT_RETAILERS)
INDEX = {name: i for i, name in enumerate(rv.REPORT_HEADER)}

# ---- the columns ----


def test_the_report_has_exactly_the_specified_22_columns_in_order() -> None:
    assert rv.REPORT_HEADER == (
        "Rank", "Laptop", "Shopee Seller", "Variation", "Target Specs", "Shopee Price",
        "Shopee Price Date",
        "DataBlitz Price", "DataBlitz Result", "Laptop Factory Price", "Laptop Factory Result",
        "PC Express Price", "PC Express Result", "PC Central Price", "PC Central Result",
        "Octagon Price", "Octagon Result",
        "Lowest Matching Price", "Lowest Matching Retailer", "Lowest Alternative Price",
        "Lowest Alternative Retailer", "Coverage",
    )  # fmt: skip
    assert len(rv.REPORT_HEADER) == 22
    # A:G product and Shopee, H:Q the five retailers, R:V the findings
    assert [(label, a, b) for label, a, b in rv.REPORT_SECTIONS] == [
        ("Product & Shopee", 0, 7),
        ("Retailer Comparison", 7, 17),
        ("Comparison Findings (in-stock prices only)", 17, 22),
    ]
    assert load_contract().headers[REPORT_TAB] == rv.REPORT_HEADER


def test_the_retailers_follow_the_technical_comparisons_order() -> None:
    from laptop_tracker.variation_publication import RETAILER_ORDER, comparison_header

    assert tuple(code for code, _ in rv.REPORT_RETAILERS) == RETAILER_ORDER
    technical = comparison_header()
    # the first 22 technical columns are the ones the report projects (7 + 10 + 5)
    pairs = [f"{n} {kind}" for _, n in rv.REPORT_RETAILERS for kind in ("price (PHP)", "result")]
    assert list(technical[7:17]) == pairs
    assert list(technical[:3]) == ["Rank", "Laptop", "Shopee seller"]
    assert technical[17] == "Lowest in-stock matching retailer price (PHP)"
    assert technical[21] == "Row coverage"


# ---- the short result labels ----


def row(**fields: str) -> dict[str, str]:
    base = {"context_id": "CTX-1", "comparison_row_id": "SV-1", "shopee_price_php": ""}
    for code in CODES:
        base |= {
            f"{code}_{name}": ""
            for name in (
                "Price", "Price_Status", "Stock", "Offer_URL", "Comparison_Tag", "Match_Basis",
                "Reason", "Alternative_Unknowns",
            )
        }  # fmt: skip
    return base | fields


def match(price: str = "50000.0", stock: str = "IN_STOCK", **extra: str) -> dict[str, str]:
    return {
        "DBZ_Price": price,
        "DBZ_Price_Status": "SELECTED",
        "DBZ_Stock": stock,
        "DBZ_Offer_URL": "https://dbz.example/m",
        "DBZ_Comparison_Tag": "MATCH",
    } | extra


def alt(tag: str, price: str = "46000.0", stock: str = "IN_STOCK", **extra: str):
    return {
        "DBZ_Price": price,
        "DBZ_Price_Status": "ALTERNATIVE_SELECTED",
        "DBZ_Stock": stock,
        "DBZ_Offer_URL": "https://dbz.example/a",
        "DBZ_Comparison_Tag": tag,
    } | extra


def label_of(r: dict[str, str]) -> tuple[str, str]:
    return rv.result_label(r, "DBZ", retailer_result(r, "DBZ"))


CAPPED = (
    "NO_RELEVANT_CANDIDATE: 3 search page(s) returned 10 result(s), none showing this "
    "configuration's model code; 1 result page(s) at the retailer's result cap or with unread "
    "next pages; the query plan ran, but the retailer's catalog was not exhaustively searched; "
    "discovery ended: QUERY_PLAN_EXHAUSTED; search coverage RESULT_CAP_REACHED"
)
FALLBACK_OFF = (
    "NO_RELEVANT_CANDIDATE: 7 search page(s) over 7 query tier(s) returned 8 result(s), none "
    "showing this configuration's model code; discovery ended: QUERY_PLAN_EXHAUSTED; search "
    "coverage FALLBACK_NOT_TRIED_DISABLED"
)


@pytest.mark.parametrize(
    ("fields", "label", "fill"),
    [
        # a verified matching configuration, available or not
        (match(), "Match", "match"),
        (match(stock="OUT_OF_STOCK"), "Match · OOS", "rpt_oos"),
        (match(DBZ_Match_Basis="LISTED_SPEC"), "Match · Listed specs", "match"),
        (match(stock="OUT_OF_STOCK", DBZ_Match_Basis="LISTED_SPEC"),
         "Match · Listed specs · OOS", "rpt_oos"),
        # unknown or unstated availability is never read as available
        (match(stock="UNKNOWN"), "Match · Stock unknown", "rpt_amber"),
        (match(stock=""), "Match · Stock unknown", "rpt_amber"),
        (match(stock="PREORDER"), "Match · Preorder", "rpt_amber"),
        # a verified close alternative: what differs, and its stock
        (alt("ALT 1"), "Alternative", "rpt_amber"),
        (alt("ALT 1 · Storage"), "Alt · Storage", "rpt_amber"),
        (alt("ALT 2 · Screen, Storage"), "Alt · Screen, Storage", "rpt_amber"),
        (alt("ALT 1", stock="OUT_OF_STOCK"), "Alt · OOS", "rpt_oos"),
        (alt("ALT 2 · Screen, Storage", stock="OUT_OF_STOCK"),
         "Alt · Screen, Storage · OOS", "rpt_oos"),
        (alt("ALT 1 · Model", DBZ_Alternative_Unknowns="limited basis: model family only"),
         "Alt · Model · Limited", "rpt_amber"),
        # a candidate or an excluded offer needs a person
        ({"DBZ_Price_Status": "CANDIDATE_NOT_CONFIRMED", "DBZ_Comparison_Tag": "RECHECK"},
         "Review", "rpt_amber"),
        ({"DBZ_Price_Status": "REVIEW_REQUIRED_ONLY"}, "Review", "rpt_amber"),
        ({"DBZ_Price_Status": "NO_COMPARABLE_OFFER"}, "Review", "rpt_amber"),
        ({"DBZ_Price_Status": "CATALOG_IDENTITY_UNRESOLVED"}, "Review", "rpt_amber"),
        # the search finished: no verified candidate
        ({"DBZ_Price_Status": "NO_RELEVANT_CANDIDATE"}, "No match", "missing"),
        ({"DBZ_Price_Status": "NO_CANDIDATE_FOUND"}, "No match", "missing"),
        ({"DBZ_Price_Status": "NO_RELEVANT_CANDIDATE", "DBZ_Reason": FALLBACK_OFF},
         "No match", "missing"),  # the optional fallback being off is a choice, not a gap
        # the search was capped or incomplete: not a conclusive no match
        ({"DBZ_Price_Status": "NO_RELEVANT_CANDIDATE", "DBZ_Reason": CAPPED},
         "Search limited", "missing"),
        ({"DBZ_Price_Status": "NO_CANDIDATE_FOUND", "DBZ_Reason": CAPPED},
         "Search limited", "missing"),
        ({"DBZ_Price_Status": "NO_RELEVANT_CANDIDATE",
          "DBZ_Reason": "discovery ended: BUDGET_EXHAUSTED"}, "Search limited", "missing"),
        ({"DBZ_Price_Status": "NO_CANDIDATE_FOUND",
          "DBZ_Reason": FALLBACK_OFF + "; 1 result page(s) at the retailer's result cap"},
         "Search limited", "missing"),
        ({"DBZ_Price_Status": "DISCOVERY_INCOMPLETE"}, "Search limited", "missing"),
        # the search or the observation never completed
        ({"DBZ_Price_Status": "SEARCH_FAILED"}, "Not checked", "missing"),
        ({"DBZ_Price_Status": "FETCH_FAILED"}, "Not checked", "missing"),
        ({"DBZ_Price_Status": "CANDIDATE_NOT_FETCHED"}, "Not checked", "missing"),
        ({"DBZ_Price_Status": "DISCOVERY_NOT_ATTEMPTED"}, "Not checked", "missing"),
        ({"DBZ_Price_Status": "NO_OBSERVATION_IN_WEEK"}, "Not checked", "missing"),
        ({"DBZ_Price_Status": "SEARCH_FAILED", "DBZ_Reason": "HTTP 429 RATE_LIMITED"},
         "Not checked", "missing"),
        ({}, "Not checked", "missing"),  # no record at all
        # a row with no trusted context compares no retailer
        ({"context_id": ""}, "Not compared", "missing"),
    ],
)  # fmt: skip
def test_a_retailer_result_is_a_short_standard_label(fields, label, fill) -> None:
    assert label_of(row(**fields)) == (label, fill)


def test_a_label_is_never_longer_than_its_parts_and_has_no_sentence() -> None:
    longest = label_of(
        row(**alt("ALT 2 · Screen, Storage", stock="OUT_OF_STOCK",
                  DBZ_Alternative_Unknowns="limited basis: model family only"))
    )[0]  # fmt: skip
    assert longest == "Alt · Screen, Storage · Limited · OOS"
    assert "\n" not in longest and len(longest) <= 40
    for fields in ({"DBZ_Price_Status": "NO_RELEVANT_CANDIDATE", "DBZ_Reason": CAPPED}, match()):
        assert "—" not in label_of(row(**fields))[0]


def test_no_label_says_the_retailer_does_not_sell_the_laptop() -> None:
    forbidden = ("not sold", "does not sell", "doesn't sell", "unavailable", "no stock")
    for fields in (
        {"DBZ_Price_Status": "NO_RELEVANT_CANDIDATE"},
        {"DBZ_Price_Status": "NO_CANDIDATE_FOUND", "DBZ_Reason": CAPPED},
        {"DBZ_Price_Status": "SEARCH_FAILED"},
        {},
    ):
        assert not any(word in label_of(row(**fields))[0].lower() for word in forbidden)


# ---- retailer cells: the price travels with its label ----


def cells_of(r: dict[str, str]):
    return rv.retailer_cells(r, "DBZ", retailer_result(r, "DBZ"))


def test_a_matching_price_is_numeric_linked_and_labelled() -> None:
    price, label, kind = cells_of(row(**match("52990.0", "OUT_OF_STOCK")))
    assert kind == "match" and price.kind == "number" and Decimal(price.value) == Decimal("52990")
    assert pv.display_text(price) == "₱52,990 (OOS)" and price.link == "https://dbz.example/m"
    assert NUMBER_FORMATS[price.number_format]["type"] == "CURRENCY"
    assert (label.value, label.fill, label.note) == ("Match · OOS", "rpt_oos", "")


def test_an_alternative_price_is_shown_as_an_alternative_never_as_a_match() -> None:
    price, label, kind = cells_of(row(**alt("ALT 2 · Screen, Storage", "85990.0", "OUT_OF_STOCK")))
    assert kind == "alt" and pv.display_text(price) == "₱85,990 (OOS)"
    assert price.link == "https://dbz.example/a"
    assert label.value == "Alt · Screen, Storage · OOS" and not label.value.startswith("Match")
    in_stock = cells_of(row(**alt("ALT 1 · Storage", "85990.0")))
    assert pv.display_text(in_stock[0]) == "₱85,990" and in_stock[1].value == "Alt · Storage"


def test_a_cell_without_a_selected_price_shows_the_placeholder_and_no_link() -> None:
    r = row(DBZ_Price_Status="CANDIDATE_NOT_CONFIRMED", DBZ_Candidate_URL="https://dbz.example/c",
            DBZ_Offer_URL="https://dbz.example/c")  # fmt: skip
    price, label, kind = cells_of(r)
    assert kind == "none" and price.kind == "text" and price.value == "—" and price.link == ""
    assert label.value == "Review"


def test_no_cell_built_for_the_report_carries_a_note() -> None:
    cells = [
        *cells_of(row(**match("1.0"))),
        *cells_of(row(**alt("ALT 1 · RAM"))),
        *cells_of(row()),
        *rv.finding_cells(
            pv.LowestOffer("1.0", ("DataBlitz",), "https://x"), "DataBlitz", None, "", "1 match"
        ),
        rv.text(rv.shopee_date_text(row(), stale=False)),
    ]
    cells = [c for c in cells if not isinstance(c, str)]
    assert cells and all(cell.note == "" for cell in cells)


# ---- the coverage text ----


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        (["match", "match", "alt", "none", "none"], "2 matches · 1 alt · 2 not priced"),
        (["match", "none", "none", "none", "none"], "1 match · 4 not priced"),
        (["alt", "none", "none", "none", "none"], "1 alt · 4 not priced"),
        (["alt", "alt", "match", "match", "match"], "3 matches · 2 alts"),
        (["match"] * 5, "5 matches"),
        (["none"] * 5, "No verified prices"),
    ],
)
def test_the_coverage_text_counts_matches_and_alternatives(kinds, expected) -> None:
    assert rv.coverage_text(kinds) == expected


def test_coverage_never_implies_a_retailer_does_not_sell_the_laptop() -> None:
    for kinds in (["none"] * 5, ["match"] + ["none"] * 4):
        text = rv.coverage_text(kinds).lower()
        assert "unavailable" not in text and "not sold" not in text and "does not" not in text
    assert rv.coverage_text(["none"] * 5, compared=False) == "Not compared"


# ---- a report holding every situation ----


@pytest.fixture
def report(world):
    return load_variation_report(world["variation"], world["reports"], world["context"])


def reset(r: dict[str, str], code: str, **fields: str) -> None:
    for key in list(r):
        if key.startswith(f"{code}_"):
            r[key] = ""
    r |= {f"{code}_{k}": v for k, v in fields.items()}


def rich(report):
    """Give the first three rows every situation. Returns the plan built from them."""
    rows = report.comparison
    for r in rows[:3]:
        for key in list(r):
            if key.startswith("Lowest"):
                r[key] = ""
    first, second, third = rows[:3]
    # row 1: a tie at the in-stock matching minimum; a CHEAPER alternative out of stock; a capped
    # search; a candidate to review
    reset(first, "DBZ", Price="54950.0", Price_Status="SELECTED", Stock="IN_STOCK",
          Offer_URL="https://dbz.example/m", Comparison_Tag="MATCH")  # fmt: skip
    reset(first, "PCX", Price="54950.0", Price_Status="SELECTED", Stock="IN_STOCK",
          Offer_URL="https://pcx.example/m", Comparison_Tag="MATCH")  # fmt: skip
    reset(first, "LF", Price="39000.0", Price_Status="ALTERNATIVE_SELECTED", Stock="OUT_OF_STOCK",
          Offer_URL="https://lf.example/a", Comparison_Tag="ALT 2 · Screen, Storage")  # fmt: skip
    reset(first, "PCC", Price_Status="NO_RELEVANT_CANDIDATE", Reason=CAPPED)
    reset(first, "OCT", Price_Status="CANDIDATE_NOT_CONFIRMED", Comparison_Tag="RECHECK")
    first |= {
        "Lowest_In_Stock_Price": "54950.0",
        "Lowest_In_Stock_Price_Retailers": "DBZ;PCX",
        "Lowest_In_Stock_Offer_URL": "https://dbz.example/m;https://pcx.example/m",
    }
    # row 2: only an out-of-stock match, and a close alternative in stock
    reset(second, "DBZ", Price="48000.0", Price_Status="SELECTED", Stock="OUT_OF_STOCK",
          Offer_URL="https://dbz.example/m2", Comparison_Tag="MATCH")  # fmt: skip
    reset(second, "LF", Price="47000.0", Price_Status="ALTERNATIVE_SELECTED", Stock="IN_STOCK",
          Offer_URL="https://lf.example/a2", Comparison_Tag="ALT 1 · Storage")  # fmt: skip
    for code in ("PCX", "PCC", "OCT"):
        reset(second, code, Price_Status="NO_RELEVANT_CANDIDATE")
    second |= {
        "Lowest_Alternative_In_Stock_Price": "47000.0",
        "Lowest_Alternative_In_Stock_Price_Retailers": "LF",
        "Lowest_Alternative_In_Stock_Price_Offer_URL": "https://lf.example/a2",
        "Lowest_Alternative_In_Stock_Price_Tag": "LF:ALT 1 · Storage",
    }
    # row 3: nothing priced, a finished search and a failed one
    reset(third, "DBZ", Price_Status="NO_RELEVANT_CANDIDATE")
    reset(third, "LF", Price_Status="SEARCH_FAILED")
    for code in ("PCX", "PCC", "OCT"):
        reset(third, code, Price_Status="NO_CANDIDATE_FOUND")
    return build_variation_plan(report, load_contract())


def shown_row(plan, n: int) -> dict[str, str]:
    tab = plan.tabs[REPORT_TAB]
    return {name: pv.display_text(cell) for name, cell in zip(tab.header, tab.rows[n], strict=True)}


def test_the_report_shows_each_situation_with_its_price_and_label(report) -> None:
    plan = rich(report)
    one, two, three = (shown_row(plan, n) for n in range(3))
    assert one["DataBlitz Price"] == "₱54,950" and one["DataBlitz Result"] == "Match"
    assert one["PC Express Price"] == "₱54,950" and one["PC Express Result"] == "Match"
    assert one["Laptop Factory Price"] == "₱39,000 (OOS)"
    assert one["Laptop Factory Result"] == "Alt · Screen, Storage · OOS"
    assert (one["PC Central Price"], one["PC Central Result"]) == ("—", "Search limited")
    assert (one["Octagon Price"], one["Octagon Result"]) == ("—", "Review")
    assert one["Coverage"] == "2 matches · 1 alt · 2 not priced"
    assert two["DataBlitz Price"] == "₱48,000 (OOS)" and two["DataBlitz Result"] == "Match · OOS"
    assert two["Laptop Factory Price"] == "₱47,000"
    assert two["Laptop Factory Result"] == "Alt · Storage"
    assert two["PC Express Result"] == "No match"
    assert two["Coverage"] == "1 match · 1 alt · 3 not priced"
    assert three["DataBlitz Result"] == "No match"
    assert three["Laptop Factory Result"] == "Not checked"
    assert three["Coverage"] == "No verified prices"
    assert {three[f"{n} Price"] for n in NAMES.values()} == {"—"}


def test_ties_are_preserved_and_the_matching_minimum_is_the_reports(report) -> None:
    plan = rich(report)
    one = shown_row(plan, 0)
    assert one["Lowest Matching Price"] == "₱54,950"
    assert one["Lowest Matching Retailer"] == "DataBlitz, PC Express"  # both winners of the tie
    cell = plan.tabs[REPORT_TAB].rows[0][INDEX["Lowest Matching Price"]]
    assert cell.link == "https://dbz.example/m"  # the first winner's offer
    # the retailers come in the technical comparison's order wherever the report lists them
    report.comparison[0]["Lowest_In_Stock_Price_Retailers"] = "DBZ;PCX"
    assert [c for c, _ in rv.REPORT_RETAILERS if c in {"DBZ", "PCX"}] == ["DBZ", "PCX"]


def test_an_alternative_never_enters_the_lowest_matching_price_or_the_gap(report) -> None:
    plan = rich(report)
    one, two, three = (shown_row(plan, n) for n in range(3))
    # row 1: the out-of-stock alternative (₱39,000) is far below the matching minimum (₱54,950)
    assert one["Lowest Matching Price"] == "₱54,950"
    assert one["Lowest Alternative Price"] == "—" and one["Lowest Alternative Retailer"] == "—"
    # row 2: an in-stock alternative and an out-of-stock match: no matching minimum at all
    assert two["Lowest Matching Price"] == "—" and two["Lowest Matching Retailer"] == "—"
    assert two["Lowest Alternative Price"] == "₱47,000"
    assert two["Lowest Alternative Retailer"] == "Laptop Factory"
    assert (three["Lowest Matching Price"], three["Lowest Alternative Price"]) == ("—", "—")
    # PRICE COMPARISON: no alternative price, no out-of-stock minimum, no comparison from them
    simple = plan.tabs[PRICE_TAB]
    index = {name: i for i, name in enumerate(simple.header)}
    for n in (0, 1):
        cells = {k: pv.display_text(c) for k, c in zip(simple.header, simple.rows[n], strict=True)}
        assert cells["Laptop Factory"] == "—"
    two_simple = {k: pv.display_text(c) for k, c in zip(simple.header, simple.rows[1], strict=True)}
    assert two_simple["DataBlitz"] == "₱48,000 (OOS)"  # shown, but never the lowest price
    assert two_simple["Lowest Price"] == "—" and two_simple["Competitiveness"] == "No comparison"
    one_simple = {k: pv.display_text(c) for k, c in zip(simple.header, simple.rows[0], strict=True)}
    assert one_simple["Lowest Price"] == "₱54,950"
    assert one_simple["Lowest Retailer"] == "DataBlitz, PC Express"
    assert index["Competitiveness"] == 18


def test_the_report_agrees_with_the_simple_view_and_the_technical_tab(report) -> None:
    plan = rich(report)
    assert report_view_issues(report, plan.tabs) == []
    simple, detailed = plan.tabs[PRICE_TAB], plan.tabs[REPORT_TAB]
    assert [r[0].value for r in simple.rows] == [r[0].value for r in detailed.rows]
    assert [r["rank"] for r in report.top] == [
        str(int(Decimal(r[0].value))) for r in detailed.rows
    ]
    for plan_name in (PRICE_TAB, REPORT_TAB):  # no notes anywhere on the two tabs
        tab = plan.tabs[plan_name]
        assert not any(tab.header_notes) and not any(c.note for r in tab.grid() for c in r)


def test_the_consistency_checks_catch_a_label_that_disagrees_with_its_price(report) -> None:
    plan = rich(report)
    tab = plan.tabs[REPORT_TAB]
    rows = [list(r) for r in tab.rows]
    # a "Match" label beside no price; a price beside a "No match"; a wrong coverage text
    rows[2][INDEX["DataBlitz Result"]] = rows[0][INDEX["DataBlitz Result"]]
    rows[1][INDEX["Octagon Result"]] = rows[1][INDEX["DataBlitz Result"]]
    rows[0][INDEX["Coverage"]] = rv.text("3 matches")
    rows[0][INDEX["Lowest Matching Price"]] = rv.lowest_price_cell(
        pv.LowestOffer("1000.0", ("DataBlitz",), "")
    )
    broken = {**plan.tabs, REPORT_TAB: type(tab)(tab.title, tab.header, tuple(map(tuple, rows)),
                                                 tab.header_notes, tab.preface)}  # fmt: skip
    issues = report_view_issues(report, broken)
    assert any("label" in i and "price disagree" in i for i in issues)
    assert any("Coverage does not reconcile" in i for i in issues)
    assert any("is not the report's Lowest_In_Stock_Price" in i for i in issues)


def test_a_tampered_alternative_cannot_become_a_matching_winner(report) -> None:
    rich(report)
    row_ = report.comparison[1]  # the in-stock alternative row: LF is ALTERNATIVE_SELECTED
    row_ |= {"Lowest_In_Stock_Price": "47000.0", "Lowest_In_Stock_Price_Retailers": "LF"}
    with pytest.raises(Exception) as caught:
        build_variation_plan(report, load_contract())
    assert "no selected matching price" in str(caught.value)


# ---- what Google stores ----


def publish(report, plan, tmp_path, fake=None):
    fake = fake or SpyingSpreadsheet(template_tabs())
    outcome = publish_variation(fake, plan, published_at=NOW, backup_dir=tmp_path / "backups")
    return fake, outcome


def stored(fake, n: int, column: str) -> dict:
    return fake.sheets[REPORT_TAB]["grid"][(4 + n, INDEX[column])]


def fill_of(fake, n: int, column: str):
    return fake.sheets[REPORT_TAB]["layout"].get((4 + n, INDEX[column]), {}).get("backgroundColor")


def same_colour(found, key: str) -> bool:
    want = STATUS_FILLS[key]
    return bool(found) and all(abs(found[k] - want[k]) < 0.002 for k in ("red", "green", "blue"))


def test_the_published_report_keeps_numbers_links_formats_and_fills(report, tmp_path) -> None:
    plan = rich(report)
    fake, outcome = publish(report, plan, tmp_path)
    assert outcome.status == "SUCCESS" and outcome.readback_issues == []

    price = stored(fake, 0, "DataBlitz Price")
    assert price["userEnteredValue"] == {"numberValue": 54950.0}  # a number, never text
    assert price["hyperlink"] == "https://dbz.example/m"
    assert price["userEnteredFormat"]["numberFormat"]["type"] == "CURRENCY"
    oos = stored(fake, 0, "Laptop Factory Price")
    assert oos["userEnteredFormat"]["numberFormat"]["pattern"] == '"₱"#,##0" (OOS)"'
    assert oos["hyperlink"] == "https://lf.example/a"
    assert "numberValue" in oos["userEnteredValue"]  # the alternative price is numeric too
    assert stored(fake, 0, "PC Central Price")["userEnteredValue"] == {"stringValue": "—"}
    assert "hyperlink" not in stored(fake, 0, "PC Central Price")
    tie = stored(fake, 0, "Lowest Matching Price")
    assert tie["userEnteredValue"] == {"numberValue": 54950.0} and tie["hyperlink"]

    # fills sit on the result cells only: green match, soft red out of stock, amber alternative or
    # review, grey for no price; price cells carry no result colour
    assert same_colour(fill_of(fake, 0, "DataBlitz Result"), "match")
    assert same_colour(fill_of(fake, 0, "Laptop Factory Result"), "rpt_oos")
    assert same_colour(fill_of(fake, 0, "Octagon Result"), "rpt_amber")
    assert same_colour(fill_of(fake, 0, "PC Central Result"), "missing")
    assert same_colour(fill_of(fake, 1, "Laptop Factory Result"), "rpt_amber")
    for name in NAMES.values():
        for n in range(3):
            assert not any(
                same_colour(fill_of(fake, n, f"{name} Price"), key)
                for key in ("match", "rpt_oos", "rpt_amber")
            )


def test_the_published_report_is_compact_frozen_and_free_of_filters_and_notes(
    report, tmp_path
) -> None:
    fake, outcome = publish(report, rich(report), tmp_path)
    sheet = fake.sheets[REPORT_TAB]
    assert sheet["filter"] is None and sheet["cf"] == [] and sheet["groups"] == []
    assert sheet["merges"] == [] and sheet["gridlines_hidden"]
    assert (sheet["frozen"], sheet["frozen_columns"]) == (4, 3)  # banner + header; 3 key columns
    assert len(sheet["widths"]) == 22
    assert not any("note" in cell for cell in sheet["grid"].values())
    assert len(fake.values(REPORT_TAB)) == 4 + len(report.top)
    banner = fake.values(REPORT_TAB)
    assert banner[0][0] == "LAPTOP PRICE REPORT"
    assert "Price Data As Of: " in banner[1][0] and banner[1][0].startswith("Reporting Week: ")
    assert [v for v in banner[2] if v] == [label for label, _, _ in rv.REPORT_SECTIONS]
    assert banner[3] == list(rv.REPORT_HEADER)


def test_the_stale_shopee_snapshot_is_flagged_on_the_price_and_the_date(report, tmp_path) -> None:
    report.comparison[0] |= {
        "shopee_price_php": "49990.0",
        "shopee_price_status": "SNAPSHOT_PRICE_STALE",
        "shopee_price_observed_at": "2026-10-20",
        "shopee_price_date_basis": "M_MINUS_1_RECORD_DATE",
        "shopee_listing_url": "https://shopee.example/i",
    }
    report.top[0]["shopee_current_price_php"] = "49990.0"  # the Top list agrees on the price
    plan = build_variation_plan(report, load_contract())
    one = shown_row(plan, 0)
    assert one["Shopee Price"] == "₱49,990 ⚠ Stale"
    assert one["Shopee Price Date"] == "2026-10-20 · monthly record · Stale"
    fake, _ = publish(report, plan, tmp_path)
    assert same_colour(fill_of(fake, 0, "Shopee Price"), "stale")
    assert same_colour(fill_of(fake, 0, "Shopee Price Date"), "stale")
    assert stored(fake, 0, "Shopee Price")["hyperlink"] == "https://shopee.example/i"


# ---- the publisher with real report rows ----


def published(world, **kwargs):
    code, fake, _ = run(world, **kwargs)
    assert code == 0, latest_report(world)
    return fake


ALLOWED = (
    r"^(Match|Alt|Alternative|Review|No match|Search limited|Not checked|Not compared)"
    r"( · (Listed specs|Limited|OOS|Preorder|Stock unknown|[A-Za-z, ]+))*$"
)


def test_every_published_result_is_a_short_label_and_every_price_has_one(world) -> None:
    import re

    fake = published(world, fake=SpyingSpreadsheet(template_tabs()))
    values = fake.values(REPORT_TAB)[4:]
    assert len(values) == 12
    for line in values:
        for name in NAMES.values():
            label = line[INDEX[f"{name} Result"]]
            assert re.match(ALLOWED, label), label
            assert line[INDEX[f"{name} Price"]] != "" and len(label) <= 40


def test_the_report_tab_is_created_next_to_an_existing_simple_tab(world) -> None:
    tabs = template_tabs()
    tabs[PRICE_TAB] = [[], [], [], list(pv.PRICE_HEADER)]
    fake = published(world, fake=SpyingSpreadsheet(tabs))
    order = list(fake.sheets)
    assert order.index(REPORT_TAB) == order.index(PRICE_TAB) + 1
    assert [m for m in fake.mutations if m.startswith("add_worksheet")] == [
        f"add_worksheet:{REPORT_TAB}"
    ]
    assert not fake.sheets[REPORT_TAB]["tab_hidden"] and not fake.sheets[PRICE_TAB]["tab_hidden"]


def test_a_failed_readback_of_the_report_keeps_both_business_tabs_hidden(world) -> None:
    fake = SpyingSpreadsheet(template_tabs())
    state = {"done": False}

    def corrupt(sheet: FakeSpreadsheet) -> None:
        cells = sheet.sheets.get(REPORT_TAB, {}).get("grid", {})
        if not state["done"] and (4, 0) in cells and cells[(4, 0)]:
            cells[(4, 0)] = {"userEnteredValue": {"numberValue": 999.0}}
            state["done"] = True

    fake.after_batch = corrupt
    code, fake, _ = run(world, fake=fake)
    report = latest_report(world)
    assert code == 1 and report["status"] == "READBACK_FAILED" and state["done"]
    for title in BUSINESS_TABS:  # never an unverified report in front of readers
        assert fake.sheets[title]["tab_hidden"]
    assert not any(fake.sheets[t]["tab_hidden"] for t in TECHNICAL_TABS)
    # the same command repairs it
    code, fake, _ = run(world, fake=fake, now=NOW + timedelta(minutes=6))
    assert code == 0 and latest_report(world)["status"] == "SUCCESS"
    assert all(fake.sheets[t]["tab_hidden"] for t in TECHNICAL_TABS)
    assert not any(fake.sheets[t]["tab_hidden"] for t in BUSINESS_TABS)


def test_the_technical_comparison_is_unchanged_by_the_new_tabs(world) -> None:
    fake = published(world, fake=SpyingSpreadsheet(template_tabs()))
    technical = fake.values(COMPARISON_TAB)
    assert len(technical[2]) == 140  # the hidden source keeps all its columns
    assert technical[2][:3] == ["Rank", "Laptop", "Shopee seller"]
    assert technical[2][7:9] == ["DataBlitz price (PHP)", "DataBlitz result"]
    # its notes (the technical explanations) stay where they were
    assert any("note" in cell for cell in fake.sheets[COMPARISON_TAB]["grid"].values())
    assert fake.sheets[COMPARISON_TAB]["tab_hidden"]
