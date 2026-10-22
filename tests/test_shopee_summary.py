"""Shopee snapshot prices (2026-10-20) and the PILOT_PRICE_SUMMARY tab (pilot-sheets-5).

Offline: the reports are built from an in-memory observation store and published to the fake
Sheets API of test_sheet_publication.py. No retailer or Shopee page is fetched.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_sheet_publication import (
    SCOPE,
    FakeSpreadsheet,
    build_reports,
    column,
    latest_report,
    row_for,
    run,
    write_dicts,
)

from laptop_tracker.price_tracking import SOURCE_LEDGER_COLUMNS
from laptop_tracker.sheet_publication import (
    BAND_BACKGROUND,
    COMPARISON_TAB,
    DATA_TABS,
    FILLS,
    NAVY,
    PUBLISHER_TABS,
    RUN_LOG_HEADER,
    RUN_LOG_HEADER_PILOT_SHEETS_4,
    RUN_LOG_TAB,
    SHOPEE_HEADER,
    SHOPEE_NOT_IN_REPORT,
    SUMMARY_HEADER,
    SUMMARY_TAB,
    WHITE,
    Cell,
    PublicationInputError,
    TabPlan,
    _retailer_summary_note,
    build_plan,
    comparison_header,
    configuration_label,
    configuration_labels,
    guard_requests,
    load_pilot_report,
    read_tab,
    summary_issues,
    tab_requests,
    text,
    verify_tab,
)
from laptop_tracker.shopee_snapshot import (
    NO_MAPPED_SOURCE,
    NO_VALID_SNAPSHOT_PRICE,
    SALES_RANKING_NOT_SUPPLIED,
    SNAPSHOT_PRICE,
    listing_url,
    select_shopee_prices,
    snapshot_amount,
)

pytestmark = pytest.mark.usefixtures("no_network")

EXTRACT = "2026-09-28"
RUN_ID = "20260928T112328+0800"


def source(target: str, model_id: str, price: str, *, status: str = "PRICE_OK",
           mapped: bool = True, gmv: str = "5", shop: str = "111", item: str = "") -> dict:
    """One configuration_source_ledger.csv row (a contributing Shopee listing)."""
    return dict.fromkeys(SOURCE_LEDGER_COLUMNS, "") | {
        "target_id": target, "model_id": model_id, "shop_id": shop,
        "item_id": item or f"9{model_id}", "item_name": f"Listing {model_id}",
        "shopee_model_name": f"Variation {model_id}", "ranking_gmv_usd": gmv,
        "currently_mapped_to_target": str(mapped), "shopee_price_php_snapshot": price,
        "shopee_price_status": status, "sales_run_id": RUN_ID, "sales_extract_date": EXTRACT,
        "sales_period_label": "M-1 2026-08 (full month) + MTD 2026-09 as extracted 2026-09-28",
    }


def ledger() -> list[dict[str, str]]:
    return [
        # One source.
        source("TGT-000001", "101", "45999.00"),
        # Several prices: the lowest valid one; a PRICE_CONFLICT listing is never used.
        source("TGT-000002", "201", "52000.00"),
        source("TGT-000002", "202", "49999.50"),
        source("TGT-000002", "203", "30000.00", status="PRICE_CONFLICT"),
        # A tie at 40000: the higher-GMV listing, then the lower model_id.
        source("TGT-000003", "301", "40000.00", gmv="5"),
        source("TGT-000003", "303", "40000.00", gmv="9"),
        source("TGT-000003", "302", "40000.00", gmv="9"),
        # No contributing listing still mapped.
        source("TGT-000004", "401", "100.00", mapped=False),
        # Eligible listings, none with a valid price.
        source("TGT-000005", "501", ""),
        source("TGT-000005", "502", "0"),
        source("TGT-000005", "503", "abc"),
        # A Shopee price where no retailer has a price; an unmapped cheaper listing is ignored.
        source("TGT-000006", "601", "30000.00", shop="222", item="333"),
        source("TGT-000006", "602", "10000.00", mapped=False),
        source("TGT-000007", "701", "", status=SALES_RANKING_NOT_SUPPLIED),
        source("TGT-000008", "801", "20000.00"),
    ]


@pytest.fixture
def shopee_world(tmp_path: Path) -> dict[str, Path]:
    return build_reports(tmp_path / "shopee", shopee_ledger=ledger())


@pytest.fixture
def plain_world(tmp_path: Path) -> dict[str, Path]:
    return build_reports(tmp_path / "plain")


def comparison_rows(world) -> dict[str, dict[str, str]]:
    with (world["report"] / "weekly_price_comparison.csv").open(encoding="utf-8") as handle:
        return {row["target_id"]: row for row in csv.DictReader(handle)}


# Selection


def test_the_lowest_eligible_mapped_listing_price_is_selected_with_its_own_row() -> None:
    selections, annotated = select_shopee_prices(ledger())

    assert len(annotated) == len(ledger())  # every listing is kept
    one = selections["TGT-000001"]
    assert (one.status, one.amount, one.source["model_id"]) == (
        SNAPSHOT_PRICE, Decimal("45999.00"), "101")
    several = selections["TGT-000002"]
    assert (several.amount, several.source["model_id"], several.distinct_prices) == (
        Decimal("49999.50"), "202", 2)
    assert several.excluded == {"PRICE_CONFLICT": 1}
    tie = selections["TGT-000003"]
    assert tie.source["model_id"] == "302" and tie.tied_model_ids == ("302", "303", "301")
    assert "tie at this price" in tie.detail()
    assert selections["TGT-000004"].status == NO_MAPPED_SOURCE
    invalid = selections["TGT-000005"]
    assert invalid.status == NO_VALID_SNAPSHOT_PRICE and invalid.amount is None
    assert invalid.excluded == {"PRICE_MISSING": 1, "PRICE_INVALID": 2}
    assert selections["TGT-000006"].source["model_id"] == "601"  # unmapped 10000 ignored
    assert selections["TGT-000007"].status == SALES_RANKING_NOT_SUPPLIED
    roles = {(row["target_id"], row["model_id"]): row["shopee_price_role"] for row in annotated}
    assert roles[("TGT-000003", "302")] == "SELECTED"
    assert roles[("TGT-000003", "303")] == "TIED_NOT_SHOWN"
    assert roles[("TGT-000002", "201")] == "HIGHER_PRICE"
    assert roles[("TGT-000002", "203")] == "NOT_ELIGIBLE: PRICE_CONFLICT"
    assert roles[("TGT-000006", "602")] == "NOT_ELIGIBLE: NOT_CURRENTLY_MAPPED"


def test_links_come_only_from_verified_numeric_ids() -> None:
    assert listing_url("222", "333") == "https://shopee.ph/product/222/333"
    assert listing_url("222", "") == "" and listing_url("x1", "333") == ""
    assert snapshot_amount({"shopee_price_status": "PRICE_OK",
                            "shopee_price_php_snapshot": "-5"}) == (None, "PRICE_INVALID")
    assert snapshot_amount({"shopee_price_status": "PRICE_OK",
                            "shopee_price_php_snapshot": "NaN"}) == (None, "PRICE_INVALID")


def test_comparison_rows_carry_one_dated_source_and_unchanged_retailer_minimums(
    shopee_world, plain_world
) -> None:
    rows, plain = comparison_rows(shopee_world), comparison_rows(plain_world)

    row = rows["TGT-000006"]
    assert (row["Shopee_Price"], row["Shopee_Model_ID"], row["Shopee_Shop_ID"],
            row["Shopee_Item_ID"]) == ("30000.00", "601", "222", "333")
    assert row["Shopee_Listing_URL"] == "https://shopee.ph/product/222/333"
    assert row["Shopee_Variation"] == "Variation 601"
    assert row["Shopee_Snapshot_Date"] == EXTRACT and RUN_ID in row["Shopee_Snapshot_Label"]
    # A Shopee price with no retailer price: the retailer side stays blank.
    assert row["Lowest_Price"] == "" and row["Comparable_Retailer_Count"] == "0"
    assert rows["TGT-000005"]["Shopee_Price"] == ""
    assert rows["TGT-000005"]["Shopee_Price_Status"] == NO_VALID_SNAPSHOT_PRICE
    # Retailer minimums, in-stock minimums and counts are exactly those without Shopee.
    retailer_side = [c for c in row if not c.startswith("Shopee_")]
    for target_id, row in rows.items():
        assert {c: row[c] for c in retailer_side} == {c: plain[target_id][c]
                                                      for c in retailer_side}


# Detailed comparison tab


def test_the_comparison_tab_shows_dated_shopee_cells_beside_the_configuration(
    shopee_world,
) -> None:
    code, fake, _ = run(shopee_world)

    assert code == 0
    header = fake.values(COMPARISON_TAB)[0]
    storage = header.index("Storage (GB)")
    assert tuple(header[storage + 1: storage + 6]) == SHOPEE_HEADER
    assert header[storage + 6] == "DataBlitz price (PHP)"
    assert "Lowest retailer price (PHP)" in header
    record = row_for(fake, COMPARISON_TAB, "TGT-000003")
    assert record["Shopee price (PHP, snapshot)"] == "40000.0"
    assert record["Shopee variation"] == "Variation 302 (model_id 302)"
    assert record["Shopee snapshot date"].startswith("2026-09-28 (historical Shopee sales extract")
    assert "not a current price" in record["Shopee price status"]
    link_cell = fake.cell(COMPARISON_TAB, 3, storage + 2)
    assert link_cell["hyperlink"] == listing_url("111", "9302")
    price_cell = fake.cell(COMPARISON_TAB, 3, storage + 1)
    assert price_cell["userEnteredFormat"]["numberFormat"]["type"] == "CURRENCY"
    blank = row_for(fake, COMPARISON_TAB, "TGT-000004")
    assert blank["Shopee price (PHP, snapshot)"] == ""
    assert blank["Shopee price status"].startswith("No snapshot price: no contributing listing")
    assert latest_report(shopee_world)["counts"]["shopee_snapshot_prices"] == 4


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"Shopee_Price": "49000.00"}, "lowest eligible mapped listing"),
        ({"Shopee_Listing_URL": "https://shopee.ph/product/1/2"}, "own IDs"),
        ({"Shopee_Snapshot_Date": "2026-10-19"}, "snapshot date differs"),
        ({"Shopee_Price": "", "Shopee_Listing_URL": ""}, "price and status disagree"),
    ],
)
def test_inconsistent_shopee_cells_are_refused(shopee_world, change, message) -> None:
    path = shopee_world["report"] / "weekly_price_comparison.csv"
    rows = list(comparison_rows(shopee_world).values())
    rows = [row | change if row["target_id"] == "TGT-000002" else row for row in rows]
    write_dicts(path, list(rows[0]), rows)

    with pytest.raises(PublicationInputError) as raised:
        load_pilot_report(shopee_world["report"], shopee_world["scope"],
                          selected_list=shopee_world["selected"])
    assert any(message in issue for issue in raised.value.issues)


def test_an_older_report_without_shopee_columns_stays_readable(plain_world) -> None:
    path = plain_world["report"] / "weekly_price_comparison.csv"
    rows = list(comparison_rows(plain_world).values())
    kept = [c for c in rows[0] if not c.startswith("Shopee_") or c in (
        "Shopee_Source_Count", "Shopee_Source_Model_IDs", "Shopee_Total_GMV_USD",
        "Shopee_GMV_Period", "Shopee_Sales_Run_ID", "Shopee_Sales_Extract_Date")]
    write_dicts(path, kept, [{c: row[c] for c in kept} for row in rows])

    code, fake, _ = run(plain_world)

    assert code == 0
    record = row_for(fake, COMPARISON_TAB, "TGT-000001")
    assert record["Shopee price status"] == SHOPEE_NOT_IN_REPORT
    assert record["Shopee price (PHP, snapshot)"] == ""
    note = fake.cell(SUMMARY_TAB, 1, 1)
    assert "userEnteredValue" not in note and SHOPEE_NOT_IN_REPORT in note["note"]


def test_a_pilot_sheets_4_layout_is_migrated_and_stale_cells_cleared(shopee_world) -> None:
    previous = [list(comparison_header(shopee=False)),
                *[["1", f"TGT-0000{i:02d}"] + ["stale"] * 40 for i in range(1, 12)]]
    run_log = [list(RUN_LOG_HEADER_PILOT_SHEETS_4), ["PUB-earlier", "SUCCESS"]]
    fake = FakeSpreadsheet({COMPARISON_TAB: previous, RUN_LOG_TAB: run_log})

    code, fake, _ = run(shopee_world, fake=fake)

    assert code == 0 and latest_report(shopee_world)["status"] == "SUCCESS"
    values = fake.values(COMPARISON_TAB)
    assert values[0] == list(comparison_header())
    assert len(values) == 1 + len(SCOPE)
    assert not any(value == "stale" for row in values for value in row)
    header, *log_rows = fake.values(RUN_LOG_TAB)
    assert header == list(RUN_LOG_HEADER)
    assert [row[0] for row in log_rows] == ["PUB-earlier", latest_report(shopee_world)[
        "publication_id"]]
    record = dict(zip(header, log_rows[-1], strict=True))
    assert record["Summary rows"] == "7.0" and record["Shopee snapshot prices"] == "4.0"
    assert record["Shopee snapshot extract date"] == EXTRACT


# PILOT_PRICE_SUMMARY


def test_the_summary_has_exactly_seven_columns_in_the_comparison_order(shopee_world) -> None:
    code, fake, _ = run(shopee_world)

    assert code == 0
    values = fake.values(SUMMARY_TAB)
    assert values[0] == ["Laptop", "Shopee", "DataBlitz", "Laptop Factory", "PC Express",
                         "PC Central", "Octagon"]
    assert all(len(row) <= 7 for row in values)
    rows = list(comparison_rows(shopee_world).values())
    scoped = [row for row in rows if row["target_id"] in SCOPE]
    assert [row[0] for row in values[1:]] == configuration_labels(scoped)
    assert len(set(row[0] for row in values[1:])) == len(SCOPE)


def test_summary_prices_are_the_detailed_comparison_cells(shopee_world) -> None:
    _, fake, _ = run(shopee_world)

    detailed = fake.values(COMPARISON_TAB)
    summary = fake.values(SUMMARY_TAB)
    for r, target_id in enumerate(SCOPE, start=1):
        record = row_for(fake, COMPARISON_TAB, target_id)
        assert summary[r][1] == record["Shopee price (PHP, snapshot)"]
        for c, name in enumerate(SUMMARY_HEADER[2:], start=2):
            assert summary[r][c] == record[f"{name} price (PHP)"], (target_id, name)
    assert len(detailed) == len(summary)
    # PHP currency cells; a blank stays blank (never zero) and explains itself in its note.
    priced = fake.cell(SUMMARY_TAB, 1, 3)  # TGT-000001 Laptop Factory 1000.00
    assert priced["userEnteredValue"] == {"numberValue": 1000.0}
    assert priced["userEnteredFormat"]["numberFormat"]["type"] == "CURRENCY"
    blank = fake.cell(SUMMARY_TAB, 1, 2)  # TGT-000001 DataBlitz
    assert "userEnteredValue" not in blank
    assert blank["note"].startswith("DataBlitz: no comparable price this week.")


def test_summary_notes_date_the_shopee_snapshot_and_name_its_source(shopee_world) -> None:
    _, fake, _ = run(shopee_world)

    shopee = fake.cell(SUMMARY_TAB, 3, 1)["note"]  # TGT-000003, the tie
    assert shopee.startswith("HISTORICAL SHOPEE SNAPSHOT — not a current price")
    assert f"Shopee sales extract of {EXTRACT}" in shopee and RUN_ID in shopee
    assert "model_id 302" in shopee and "Variation 302" in shopee
    assert "https://shopee.ph/product/111/9302" in shopee and "not the variation" in shopee
    assert "tie at this price: model_id 302, 303, 301" in shopee
    missing = fake.cell(SUMMARY_TAB, 5, 1)["note"]  # TGT-000005
    assert missing.startswith("No Shopee snapshot price.") and "PRICE_INVALID x2" in missing
    header_note = fake.cell(SUMMARY_TAB, 0, 1)["note"]
    assert header_note.startswith("HISTORICAL SHOPEE SNAPSHOT, not a current price")
    assert EXTRACT in header_note and "PILOT_PRICE_REVIEW" in header_note
    # Shopee price with no retailer price (TGT-000006).
    assert fake.values(SUMMARY_TAB)[6][1] == "30000.0"
    assert all(value == "" for value in fake.values(SUMMARY_TAB)[6][2:])


def test_summary_notes_carry_stock_basis_and_blank_explanations(shopee_world) -> None:
    _, fake, _ = run(shopee_world)

    exact = fake.cell(SUMMARY_TAB, 1, 3)["note"]
    assert exact.startswith("Exact product match") and "Stock: in stock." in exact
    assert "Offer: https://" in exact
    out_of_stock = fake.cell(SUMMARY_TAB, 2, 3)["note"]  # TGT-000002 Laptop Factory
    assert "OUT OF STOCK (price kept under the owner-approved policy)" in out_of_stock
    assert fake.values(SUMMARY_TAB)[2][3] == "1500.0"
    review = fake.cell(SUMMARY_TAB, 4, 3)["note"]  # TGT-000004: a match-review candidate
    assert "Needs match review (no price)" in review
    assert not fake.cell(SUMMARY_TAB, 1, 3).get("userEnteredFormat", {}).get(
        "textFormat", {}).get("italic")


def test_listed_spec_bundle_and_disagreement_notes() -> None:
    row = {"target_id": "TGT-000001", "LF_Price": "1000.00", "LF_Match_Basis": "LISTED_SPEC",
           "LF_Offer_Details": "IdeaPad Slim 3 15IRH10; Core i5-13420H",
           "LF_Warnings": "SPECIFIC_IDENTITY_UNRESOLVED;BUNDLE_OFFER;OFFERS_DISAGREE_RESOLUTION",
           "LF_Stock": "OUT_OF_STOCK", "LF_Observed_At": "2026-10-19T10:00:00+08:00",
           "LF_Offer_URL": "https://laptopfactory.com.ph/product/x/", "LF_Price_Status": "SELECTED"}
    note = _retailer_summary_note(SimpleNamespace(selections={}), row, "LF", "")
    assert note.startswith("LISTED-SPEC MATCH — not an exact product match")
    assert "Offer as listed: IdeaPad Slim 3 15IRH10" in note
    assert "Stock: OUT OF STOCK" in note
    assert "Bundle offer (price kept under the owner-approved policy)." in note
    assert "Warning: retailers state different resolutions (see review)." in note
    blank = row | {"LF_Price": "", "LF_Price_Status": "NO_RELEVANT_CANDIDATE",
                   "LF_Candidate_URL": "https://laptopfactory.com.ph/product/y/",
                   "LF_Recheck_Count": "3", "LF_Recheck_Note": "RAM differs"}
    selections = {("TGT-000001", "LF"): {"not_comparable_reasons": ""}}
    note = _retailer_summary_note(SimpleNamespace(selections=selections), blank, "LF", "")
    assert note.startswith("Laptop Factory: no comparable price this week. Searched; no "
                           "relevant candidate found")
    assert ("Recheck (unconfirmed page, not a price): https://laptopfactory.com.ph/product/y/"
            " — RAM differs (+2 more in PILOT_PRICE_REVIEW)") in note


def test_labels_distinguish_configurations_that_share_a_model_name() -> None:
    base = {"Brand": "Acer", "Series": "", "Model": "ANV15-42-R2UH", "Screen Size": "15.6",
            "CPU_Series": "Ryzen 5", "RAM_Capacity": "16", "Storage_Capacity": "512"}
    assert configuration_label(base) == "Acer ANV15-42-R2UH · 15.6″ · Ryzen 5 · 16GB/512GB"
    assert configuration_label(base | {"Brand": "HP", "Model": "HP Laptop 15",
                                       "Storage_Capacity": "1000"}).startswith(
        "HP Laptop 15 · 15.6″")
    assert configuration_label(base | {"Series": "TUF A15", "Model": "FA506"}).startswith(
        "Acer TUF A15 FA506 ·")
    labels = configuration_labels([base, base | {"RAM_Capacity": "8"},
                                   base | {"Resolution": "2560x1600"}, base | {}])
    assert labels[1].endswith("8GB/512GB")
    assert labels[2].endswith("· 2560x1600")
    assert labels[0].endswith("· no resolution · configuration 1 of 2")
    assert labels[3].endswith("· no resolution · configuration 2 of 2")
    assert len(set(labels)) == 4


def test_the_plan_refuses_a_summary_that_differs_from_the_comparison(shopee_world) -> None:
    report = load_pilot_report(shopee_world["report"], shopee_world["scope"],
                               selected_list=shopee_world["selected"])
    plan = build_plan(report)
    summary = plan.tabs[SUMMARY_TAB]
    assert summary_issues(plan.tabs[COMPARISON_TAB], summary) == []
    rows = [list(r) for r in summary.rows]
    rows[0][3] = Cell("number", "999.00", "", "php")
    altered = TabPlan(SUMMARY_TAB, summary.header, tuple(map(tuple, rows)), summary.header_notes)
    assert summary_issues(plan.tabs[COMPARISON_TAB], altered) == [
        f"{SUMMARY_TAB} TGT-000001 Laptop Factory differs from {COMPARISON_TAB}"]
    assert summary_issues(plan.tabs[COMPARISON_TAB], TabPlan(
        SUMMARY_TAB, summary.header, summary.rows[:-1])) != []


# Summary publication: creation, layout, ownership, readback, repeated publication


def test_the_summary_is_created_beside_existing_publisher_tabs(shopee_world, capsys) -> None:
    fake = FakeSpreadsheet()
    assert run(shopee_world, fake=fake)[0] == 0
    del fake.sheets[SUMMARY_TAB]  # a spreadsheet published before pilot-sheets-5
    fake.mutations.clear()

    code, fake, _ = run(shopee_world, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))

    assert code == 0
    assert fake.mutations[0] == f"add_worksheet:{SUMMARY_TAB}"
    sheet = fake.sheets[SUMMARY_TAB]
    assert (sheet["frozen"], sheet["frozen_columns"]) == (1, 1)
    assert sheet["widths"][0] > sheet["widths"][1] > sheet["widths"][2] == sheet["widths"][6]
    assert sheet["layout"][(1, 0)]["wrapStrategy"] == "WRAP"
    assert sheet["layout"][(1, 4)]["horizontalAlignment"] == "RIGHT"
    # House layout (pilot-sheets-6): every second data row is banded; the Laptop column is
    # white otherwise, and the price columns keep their tint on banded rows too.
    assert sheet["layout"][(2, 0)]["backgroundColor"] == BAND_BACKGROUND
    assert sheet["layout"][(1, 0)]["backgroundColor"] == WHITE
    assert sheet["layout"][(2, 3)]["backgroundColor"] == FILLS["retailer"]
    assert sheet["layout"][(0, 0)]["textFormat"]["foregroundColor"] == WHITE
    assert sheet["layout"][(0, 0)]["backgroundColor"] == NAVY
    assert set(latest_report(shopee_world)["tabs_written"]) == set(PUBLISHER_TABS)


def test_preview_lists_the_summary_and_writes_its_notes_plan(shopee_world, capsys) -> None:
    code, fake, opened = run(shopee_world, mode="--preview")

    assert (code, opened, fake.mutations) == (0, [True], [])
    printed = capsys.readouterr().out
    assert f"{SUMMARY_TAB}: new tab; would write 1 header + 7 rows." in printed
    folder = sorted(shopee_world["out"].iterdir())[-1]
    with (folder / f"plan_{SUMMARY_TAB}.csv").open(encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == list(SUMMARY_HEADER)
    with (folder / f"plan_{SUMMARY_TAB}_notes.csv").open(encoding="utf-8") as handle:
        notes = list(csv.DictReader(handle))
    assert len(notes) == 7 + 7 * 6
    # The overview (pilot-sheets-6) notes its twelve headers and each row's target identity.
    assert json.loads((folder / "publication_report.json").read_text())[
        "planned_cell_notes"] == {SUMMARY_TAB: 49, "PILOT_PRICE_OVERVIEW": 12 + 7}


def test_a_summary_tab_with_unrelated_content_is_refused(shopee_world, capsys) -> None:
    fake = FakeSpreadsheet({SUMMARY_TAB: [["My own notes"], ["do not overwrite"]]})

    code, fake, _ = run(shopee_world, fake=fake)

    assert code == 1 and fake.mutations == []
    assert f"{SUMMARY_TAB} has content that this publisher did not write" in (
        capsys.readouterr().err)


@pytest.mark.parametrize("damage", ["note", "italic", "stale_note"])
def test_summary_readback_checks_notes(shopee_world, damage) -> None:
    fake = FakeSpreadsheet()

    def corrupt(sheet: FakeSpreadsheet) -> None:
        if sheet.batches != 3:  # the summary tab write
            return
        grid = sheet.sheets[SUMMARY_TAB]["grid"]
        if damage == "note":
            grid[(1, 1)]["note"] = "edited"
        elif damage == "italic":
            grid[(1, 3)]["userEnteredFormat"].setdefault("textFormat", {})["italic"] = True
        else:
            grid[(30, 2)] = {"note": "stale"}

    fake.after_batch = corrupt
    code, fake, _ = run(shopee_world, fake=fake)

    assert code == 1
    report = latest_report(shopee_world)
    assert report["status"] == "READBACK_FAILED"
    assert all(issue.startswith(SUMMARY_TAB) for issue in report["readback_issues"])


def summary_grid(cells: list[list[Cell]]) -> list[list[Cell]]:
    return TabPlan(SUMMARY_TAB, SUMMARY_HEADER, tuple(map(tuple, cells)),
                   ("header note",) * 7).grid()


def price(value: str, note: str, *, italic: bool = False) -> Cell:
    return Cell("number", value, "", "php", note=note, italic=italic)


def test_repeated_summary_writes_clear_stale_prices_notes_and_bands() -> None:
    fake = FakeSpreadsheet()
    fake.add_worksheet(SUMMARY_TAB, 100, 40)
    blank = [Cell(note="no price")] * 5
    first = summary_grid([
        [text("A"), price("100.00", "listing 1"), *blank],
        [text("B"), Cell(note="no snapshot price"), *blank],
        [text("C"), Cell(note="none"), price("900.00", "listed-spec", italic=True), *blank[1:]],
        [text("D"), Cell(note="none"), *blank],
    ])
    second = summary_grid([
        [text("A"), Cell(note="now blank"), *blank],                              # price -> blank
        [text("B"), price("200.00", "listing 2"), *blank],                        # blank -> price
        [text("C"), Cell(note="none"), price("900.00", "exact product"), *blank[1:]],  # exact
    ])                                                                            # scope shrinks
    third = summary_grid([[text("A"), price("150.00", "listing 3 (selected listing changed)"),
                           *blank], *second[2:]])
    for grid in (first, second, third):
        state = read_tab(fake, SUMMARY_TAB)
        requests = tab_requests(state, grid, 7)
        guard_requests(requests, {state.sheet_id})
        fake.batch_update({"requests": requests})
        assert verify_tab(read_tab(fake, SUMMARY_TAB), grid) == []
    cells = fake.sheets[SUMMARY_TAB]["grid"]
    assert not cells[(3, 2)].get("userEnteredFormat", {}).get("textFormat", {}).get("italic")
    assert all(not cell.get("note") and not cell.get("userEnteredValue")
               for (r, _), cell in cells.items() if r >= len(third))
    assert "backgroundColor" not in fake.sheets[SUMMARY_TAB]["layout"].get((4, 0), {})
    assert cells[(1, 1)]["note"] == "listing 3 (selected listing changed)"


def test_republication_after_a_scope_change_leaves_no_stale_summary_rows(shopee_world) -> None:
    fake = FakeSpreadsheet()
    assert run(shopee_world, fake=fake)[0] == 0
    scope = shopee_world["scope"]
    with scope.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows, columns = list(reader), reader.fieldnames
    write_dicts(scope, columns, rows[:4])

    code, fake, _ = run(shopee_world, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))

    assert code == 0 and latest_report(shopee_world)["status"] == "SUCCESS"
    assert len([row for row in fake.values(SUMMARY_TAB) if any(row)]) == 5
    assert list(filter(None, column(fake, COMPARISON_TAB, "Target ID"))) == list(SCOPE[:4])
    assert all(not cell.get("note") for (r, _), cell in
               fake.sheets[SUMMARY_TAB]["grid"].items() if r >= 5)


def test_the_summary_tab_is_a_data_tab_and_note_requests_stay_on_it() -> None:
    assert SUMMARY_TAB in PUBLISHER_TABS and SUMMARY_TAB in DATA_TABS
    fake = FakeSpreadsheet()
    fake.add_worksheet(SUMMARY_TAB, 10, 7)
    state = read_tab(fake, SUMMARY_TAB)
    requests = tab_requests(state, summary_grid([[text("A"), *[Cell()] * 6]]), 7)
    guard_requests(requests, {state.sheet_id})
    with pytest.raises(Exception, match="outside the publisher tabs"):
        guard_requests(requests, {state.sheet_id + 1})
    comparison = read_tab(FakeSpreadsheet({COMPARISON_TAB: [["Rank"]]}), COMPARISON_TAB)
    plain = tab_requests(comparison, [[text("Rank")]], 1)
    assert all("note" not in json.dumps(r) for r in plain)  # other tabs keep people's notes
