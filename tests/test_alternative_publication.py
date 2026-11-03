"""Close alternatives in the Sheets publication (pilot-sheets-7, 2026-10-27).

Reports are built with the real Phase 9 pipeline (``build_price_tracking`` with the alternative
evaluator) from title-only MSI offers (``test_alternative_reports``), published to the in-memory
fake of the Sheets API: headers, numeric prices, tags, notes, fills, the alternatives tab, the
review relabelling, validation of the new report files and compatibility with older reports.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_alternative_reports import catalog, msi
from test_price_overview import comparison_row, comparison_tab, report_for
from test_price_tracking import AS_OF, TM_SHA, WEEK, ledger, list_rows
from test_sheet_publication import FakeSpreadsheet, row_for, run, write_dicts
from test_target_master import target_values

from laptop_tracker.alternatives import AlternativeEvaluator
from laptop_tracker.listed_spec import ListedSpecMatcher
from laptop_tracker.match_run import matcher_from_validated
from laptop_tracker.observation_store import ObservationStore, observation_id
from laptop_tracker.offers import Availability
from laptop_tracker.price_tracking import (
    ALTERNATIVE_COLUMNS,
    COMPARISON_COLUMNS,
    EVALUATION_COLUMNS,
    HISTORY_COLUMNS,
    LEGACY_COMPARISON_COLUMNS,
    LEGACY_HISTORY_COLUMNS,
    LEGACY_SELECTION_COLUMNS,
    REVIEW_COLUMNS,
    SELECTION_COLUMNS,
    build_price_tracking,
    match_stored_observations,
    matching_context,
    validate_selected_list,
)
from laptop_tracker.sheet_publication import (
    ALTERNATIVES_HEADER,
    ALTERNATIVES_TAB,
    COMPARISON_TAB,
    EARLIER_HEADERS,
    OVERVIEW_HEADER,
    OVERVIEW_HEADER_PILOT_SHEETS_6,
    OVERVIEW_LABELS,
    OVERVIEW_TAB,
    PUBLISHER_TABS,
    PUBLISHER_VERSION,
    REVIEW_TAB,
    RUN_LOG_HEADER,
    RUN_LOG_HEADER_PILOT_SHEETS_6,
    RUN_LOG_TAB,
    STATUS_FILLS,
    PublicationInputError,
    TabState,
    build_plan,
    check_ownership,
    comparison_header,
    fill_requests,
    guard_requests,
    load_pilot_report,
    overview_plan,
    read_tab,
    tab_requests,
    verify_tab,
)
from laptop_tracker.target_master import validate_target_master
from laptop_tracker.verified_top import CONFIGURATION_COLUMNS

pytestmark = pytest.mark.usefixtures("no_network")

IDS = ("TGT-000001", "TGT-000002")
SUMMARY = {
    "report_week": {
        "start": "2026-10-05", "end_exclusive": "2026-10-12", "timezone": "Asia/Singapore",
        "start_utc": "2026-10-04T16:00:00.000000Z",
        "end_utc_exclusive": "2026-10-11T16:00:00.000000Z", "status": "CLOSED_HISTORICAL",
    },
    "as_of_utc": "2026-10-12T01:00:00.000000Z",
    "sales_snapshot": {"run_id": "20260928T112328+0800", "label": "snapshot"},
}


def build_world(base: Path, offers, *, alternatives: bool = True, context=None) -> dict:
    """Reports for the two MSI targets from ``offers``; the pilot scope is both targets."""
    targets = catalog()
    tm = validate_target_master(target_values(*targets))
    rows = list_rows(IDS, targets)
    inputs = base / "inputs"
    inputs.mkdir(parents=True)
    selected_path = write_dicts(inputs / "top.csv", CONFIGURATION_COLUMNS, rows)
    list_sha = hashlib.sha256(selected_path.read_bytes()).hexdigest()
    selected = validate_selected_list(rows, ledger(), tm, target_master_sha256=TM_SHA,
                                      list_sha256=list_sha)
    with ObservationStore.open(base / "store.sqlite3") as store:
        store.import_observations(offers, source_label="test", imported_at=AS_OF)
        match_context = matching_context(tm, target_master_sha256=TM_SHA, source_label="test")
        match_stored_observations(store, match_context, matcher_from_validated(tm), now=AS_OF)
        stored, matches = store.observations(), store.match_results(match_context.context_id)
    result = build_price_tracking(
        stored, matches, selected, match_context, week=WEEK, as_of=AS_OF,
        listed_spec=ListedSpecMatcher(tm.valid_rows),
        search_context=context,
        alternatives=AlternativeEvaluator([i.target for i in selected.configurations])
        if alternatives else None)
    report = base / "report"
    report.mkdir()
    write_dicts(report / "weekly_price_comparison.csv", COMPARISON_COLUMNS, result.comparison)
    write_dicts(report / "weekly_price_selections.csv", SELECTION_COLUMNS, result.selections)
    write_dicts(report / "price_history.csv", HISTORY_COLUMNS, result.history)
    write_dicts(report / "price_review_queue.csv", REVIEW_COLUMNS, result.review)
    if alternatives:
        write_dicts(report / "weekly_price_alternatives.csv", ALTERNATIVE_COLUMNS,
                    result.alternatives)
        write_dicts(report / "alternative_evaluations.csv", EVALUATION_COLUMNS,
                    result.evaluations)
    (report / "price_tracking_run_summary.json").write_text(json.dumps(SUMMARY | {
        "selected_list": {"configurations": 2, "sha256": list_sha,
                          "built_on_target_master_sha256": TM_SHA},
        "matching_context": {"context_id": match_context.context_id,
                             "target_master_sha256": TM_SHA},
        "input_sha256": {str(selected_path): list_sha},
    }))
    scope = write_dicts(base / "scope.csv", CONFIGURATION_COLUMNS, rows)
    return {"report": report, "scope": scope, "selected": selected_path,
            "out": base / "publications", "result": result}


def plan_of(world):
    return build_plan(load_pilot_report(world["report"], world["scope"],
                                        selected_list=world["selected"]))


def publish_world(world):
    code, fake, _ = run(world)
    assert code == 0
    return fake


def locate(fake: FakeSpreadsheet, title: str, name: str, key: str, key_column: str = "Target ID"):
    """``(row index, column index)`` of the cell under ``name`` in the row whose ``key_column``
    equals ``key``."""
    values = fake.values(title)
    column = values[0].index(name)
    key_index = values[0].index(key_column)
    for r, row in enumerate(values):
        if r and len(row) > key_index and row[key_index] == key:
            return r, column
    raise AssertionError(key)


OFFERS_HIDDEN = [
    msi("lf-16gb", price="1000.00"),
    msi("lf-8gb", price="700.00", ram="8"),
]
OFFERS_POOL = [
    msi("dbz-alt1", retailer="datablitz", price="900.00", ram="8", storage="512"),
    msi("dbz-alt2", retailer="datablitz", price="500.00", ram="8", storage="256"),
    msi("lf-oos", price="450.00", ram="8", availability=Availability.OUT_OF_STOCK),
    msi("pcx", retailer="pcx", price="650.00", ram="8", storage="256"),
]


@pytest.fixture
def hidden(tmp_path):
    return build_world(tmp_path, OFFERS_HIDDEN)


@pytest.fixture
def pool(tmp_path):
    return build_world(tmp_path, OFFERS_POOL)


# Compatibility


def test_the_new_columns_are_added_only_at_the_right_of_every_earlier_layout() -> None:
    header, earlier = comparison_header(), comparison_header(alternatives=False)
    assert header[:len(earlier)] == earlier and len(header) == len(earlier) + 26
    added = header[len(earlier):]
    assert [name for name in added[:5]] == [
        f"{name} comparison tag" for name in ("DataBlitz", "Laptop Factory", "PC Express",
                                              "PC Central", "Octagon")]
    assert all(("alternative" in name or "comparison tag" in name
                or name.startswith("Retailers shown")) for name in added)
    # The earlier headers of every publisher tab stay recognised as this publisher's.
    assert earlier in EARLIER_HEADERS[COMPARISON_TAB]
    assert OVERVIEW_HEADER[:12] == OVERVIEW_HEADER_PILOT_SHEETS_6
    assert OVERVIEW_HEADER_PILOT_SHEETS_6 in EARLIER_HEADERS[OVERVIEW_TAB]
    assert RUN_LOG_HEADER[:len(RUN_LOG_HEADER_PILOT_SHEETS_6)] == RUN_LOG_HEADER_PILOT_SHEETS_6
    assert RUN_LOG_HEADER_PILOT_SHEETS_6 in EARLIER_HEADERS[RUN_LOG_TAB]
    assert PUBLISHER_VERSION == "pilot-sheets-7" and ALTERNATIVES_TAB in PUBLISHER_TABS


def test_a_live_tab_with_the_previous_headers_is_still_owned_and_replaced(hidden) -> None:
    tabs = {COMPARISON_TAB: [list(comparison_header(alternatives=False))],
            OVERVIEW_TAB: [list(OVERVIEW_HEADER_PILOT_SHEETS_6)],
            RUN_LOG_TAB: [list(RUN_LOG_HEADER_PILOT_SHEETS_6)]}
    fake = FakeSpreadsheet(tabs)
    for title in tabs:
        state = read_tab(fake, title)
        assert check_ownership(state, ("something else",)) == "", title
    code, fake, _ = run(hidden, fake=fake)
    assert code == 0
    assert fake.values(COMPARISON_TAB)[0] == list(comparison_header())
    assert fake.values(OVERVIEW_TAB)[0] == list(OVERVIEW_HEADER)
    assert fake.values(RUN_LOG_TAB)[0] == list(RUN_LOG_HEADER)


def test_an_older_report_without_the_new_files_or_columns_still_publishes(tmp_path) -> None:
    world = build_world(tmp_path, OFFERS_HIDDEN, alternatives=False)
    for name, legacy in (("weekly_price_comparison.csv", LEGACY_COMPARISON_COLUMNS),
                         ("weekly_price_selections.csv", LEGACY_SELECTION_COLUMNS),
                         ("price_history.csv", LEGACY_HISTORY_COLUMNS)):
        path = world["report"] / name
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        write_dicts(path, legacy, [{k: r[k] for k in legacy} for r in rows])
    assert not (world["report"] / "weekly_price_alternatives.csv").exists()
    fake = publish_world(world)
    report = load_pilot_report(world["report"], world["scope"], selected_list=world["selected"])
    assert report.alternatives == [] and report.counts["alternative_prices"] == 0
    # Every priced cell of an older report reads as a matching configuration.
    tag = locate(fake, COMPARISON_TAB, "Laptop Factory comparison tag", "TGT-000001")
    assert fake.values(COMPARISON_TAB)[tag[0]][tag[1]] == "MATCH"
    assert fake.values(ALTERNATIVES_TAB) == [list(ALTERNATIVES_HEADER)]


def test_the_alternatives_tab_publishes_its_header_even_with_no_rows(tmp_path) -> None:
    world = build_world(tmp_path, [])
    fake = publish_world(world)
    assert fake.values(ALTERNATIVES_TAB) == [list(ALTERNATIVES_HEADER)]
    assert fake.sheets[ALTERNATIVES_TAB]["frozen_columns"] == 2


# Comparison tab


def test_an_alternative_is_a_numeric_php_price_with_a_tag_and_an_explanation(tmp_path) -> None:
    world = build_world(tmp_path, [msi("lf-8gb", price="700.00", ram="8")])
    fake = publish_world(world)
    r, c = locate(fake, COMPARISON_TAB, "Laptop Factory price (PHP)", "TGT-000001")
    cell = fake.cell(COMPARISON_TAB, r, c)
    assert cell["userEnteredValue"] == {"numberValue": 700.0}
    assert cell["userEnteredFormat"]["numberFormat"]["type"] == "CURRENCY"
    record = row_for(fake, COMPARISON_TAB, "TGT-000001")
    assert record["Laptop Factory comparison tag"] == "ALT 1 · RAM"
    status = record["Laptop Factory status"]
    assert status.startswith("Alternative price — ALT 1 · RAM: RAM: target 16GB → retailer 8GB.")
    assert "Not a matching configuration" in status and "unknown:" not in status
    # The retailer of the matching 8GB target shows a plain match.
    other = row_for(fake, COMPARISON_TAB, "TGT-000002")
    assert other["Laptop Factory comparison tag"] == "MATCH"
    assert other["Laptop Factory status"].startswith("Comparable price")
    # Matching-only columns do not move: no matching price for TGT-000001.
    assert record["Lowest retailer price (PHP)"] == ""
    assert record["Comparable retailers"] == "0.0"
    assert record["Retailers shown (matching or alternative)"] == "1.0"
    assert record["Alternative retailers (eligible alternatives)"] == "1.0"
    # The tag cell is filled yellow; the price cell too (an alternative).
    tag_r, tag_c = locate(fake, COMPARISON_TAB, "Laptop Factory comparison tag", "TGT-000001")
    assert fake.sheets[COMPARISON_TAB]["layout"][(tag_r, tag_c)]["backgroundColor"] == (
        STATUS_FILLS["alt1"])
    assert fake.sheets[COMPARISON_TAB]["layout"][(r, c)]["backgroundColor"] == STATUS_FILLS["alt1"]


def test_a_matching_offer_outranks_a_cheaper_alternative_but_the_tab_lists_both(hidden) -> None:
    fake = publish_world(hidden)
    record = row_for(fake, COMPARISON_TAB, "TGT-000001")
    assert record["Laptop Factory price (PHP)"] == "1000.0"
    assert record["Laptop Factory comparison tag"] == "MATCH"
    # The hidden 700.00 alternative is in the alternative minimum and in the alternatives tab.
    assert record["Lowest alternative price (PHP, alternatives only)"] == "700.0"
    assert record["Lowest alternative price retailer(s)"] == "Laptop Factory"
    assert record["Lowest alternative price difference count"] == "Laptop Factory: 1"
    assert record["Lowest alternative price differences"] == (
        "Laptop Factory: RAM: target 16GB → retailer 8GB.")
    assert record["Lowest alternative price observation ID"].startswith("Laptop Factory: ")
    assert record["Lowest retailer price (PHP)"] == "1000.0"
    alt = row_for(fake, ALTERNATIVES_TAB, "TGT-000001")
    assert alt["Price (PHP)"] == "700.0"
    assert (alt["Shown in main comparison"], alt["Matching offer at this retailer"]) == (
        "No", "Yes")
    assert alt["Comparison tag"] == "ALT 1 · RAM"


def test_alternative_minimums_use_the_full_pool_and_the_in_stock_one_excludes_sold_out(
        pool) -> None:
    fake = publish_world(pool)
    record = row_for(fake, COMPARISON_TAB, "TGT-000001")
    assert record["Lowest alternative price (PHP, alternatives only)"] == "450.0"
    assert record["Lowest alternative price retailer(s)"] == "Laptop Factory"
    assert record["Lowest alternative price stock"] == "OUT_OF_STOCK"
    assert record["Lowest in-stock alternative price (PHP, alternatives only)"] == "500.0"
    assert record["Lowest in-stock alternative retailer(s)"] == "DataBlitz"
    assert record["Lowest in-stock alternative tag"] == "DataBlitz: ALT 2 · RAM, Storage"
    # Each retailer once, whatever its number of offers: DataBlitz has two.
    assert record["Alternative retailers (eligible alternatives)"] == "3.0"
    assert record["DataBlitz comparison tag"] == "ALT 1 · RAM"  # ALT 1 beats the cheaper ALT 2
    assert record["DataBlitz price (PHP)"] == "900.0"


# Overview and summary


def test_overview_fills_notes_links_and_text_labels(hidden) -> None:
    fake = publish_world(hidden)
    plan = plan_of(hidden)
    overview = plan.tabs[OVERVIEW_TAB]
    laptop = OVERVIEW_HEADER.index("Laptop Factory (PHP)")
    labels = OVERVIEW_HEADER.index(OVERVIEW_LABELS)
    first, second = overview.rows
    assert first[laptop].fill == "match" and second[laptop].fill == "match"
    assert first[labels].value == "Laptop Factory: MATCH"
    note = first[laptop].note
    for expected in ("MATCH — matching configuration (exact product match)",
                     "Matching basis: exact product",
                     "Stock: in stock",
                     "close alternative offer(s) at this retailer are also listed in "
                     "PILOT_PRICE_ALTERNATIVES",
                     "Detailed record: PILOT_PRICE_COMPARISON row for TGT-000001"):
        assert expected in note
    # The legend is written as text in the header notes.
    legend = overview.header_notes[laptop]
    assert "green = MATCH" in legend and "gray = RECHECK" in legend
    assert "orange = ALT 2" in overview.header_notes[labels]
    # Fills reach the sheet, after the layout.
    r, c = 1, laptop
    assert fake.sheets[OVERVIEW_TAB]["layout"][(r, c)]["backgroundColor"] == STATUS_FILLS["match"]


def test_an_alternative_in_the_overview_is_yellow_noted_linked_and_labelled(tmp_path) -> None:
    offer = replace(
        msi("lf-8gb", price="700.00", ram="8"),
        raw_title='MSI Thin 15 B13UC-3474PH 15.6" Intel Core i5-13420H 8GB RAM 512GB SSD '
                  "Gaming Laptop")
    world = build_world(tmp_path, [offer])
    plan = plan_of(world)
    overview = plan.tabs[OVERVIEW_TAB]
    column = OVERVIEW_HEADER.index("Laptop Factory (PHP)")
    cell = overview.rows[0][column]
    assert (cell.kind, cell.value, cell.fill) == ("number", "700.00", "alt1")
    assert cell.link == offer.offer_url
    for expected in ("ALT 1 · RAM — close alternative, NOT a matching configuration",
                     "Matching basis: none (ALTERNATIVE",
                     "Actual configuration: model B13UC-3474PH; 15.6in; CPU Core i5 "
                     "(Core i5-13420H); RAM 8GB; storage 512GB",
                     "Differences: RAM: target 16GB → retailer 8GB.",
                     "Unknown / limited: GPU: GPU not stated by the retailer",
                     "Stock: in stock", f"Offer: {offer.offer_url}",
                     "Detailed record: PILOT_PRICE_ALTERNATIVES row for TGT-000001, observation "
                     f"{observation_id(offer)}"):
        assert expected in cell.note, expected
    assert overview.rows[0][OVERVIEW_HEADER.index(OVERVIEW_LABELS)].value == (
        "Laptop Factory: ALT 1 · RAM")
    # The second row (the 8GB target) is a plain match with its own green fill.
    assert overview.rows[1][column].fill == "match"
    # The summary repeats the price with an alternative-aware note and the same fill.
    summary = plan.tabs["PILOT_PRICE_SUMMARY"]
    price = summary.rows[0][summary.header.index("Laptop Factory")]
    assert price.fill == "alt1"
    assert "close alternative, not a matching configuration" in price.note
    assert "PILOT_PRICE_ALTERNATIVES" in price.note


def test_recheck_without_a_price_is_gray_with_a_note_and_text_label(tmp_path) -> None:
    # No RAM statement: unresolved evidence, collected for TGT-000001 (search lineage only).
    unresolved = msi("lf-unclear", price="640.00")
    unresolved = replace(
        unresolved, raw_title='MSI Thin 15 B13UC-3474PH 15.6" Intel Core i5-13420H 512GB SSD')
    world = build_world(tmp_path, [unresolved],
                        context={observation_id(unresolved): ["TGT-000001"]})
    row = next(r for r in world["result"].comparison if r["target_id"] == "TGT-000001")
    assert row["LF_Price"] == "" and row["LF_Comparison_Tag"] == "RECHECK"
    plan = plan_of(world)
    cell = plan.tabs[OVERVIEW_TAB].rows[0][OVERVIEW_HEADER.index("Laptop Factory (PHP)")]
    assert (cell.kind, cell.fill) == ("blank", "recheck")
    assert cell.note.startswith("Laptop Factory: RECHECK — unresolved evidence; no confirmed")
    assert plan.tabs[OVERVIEW_TAB].rows[0][OVERVIEW_HEADER.index(OVERVIEW_LABELS)].value == (
        "Laptop Factory: RECHECK")


def test_overview_unit_cells_follow_their_comparison_tags() -> None:
    rows = [comparison_row(1, model="A1-X", prices={"DBZ": "100.00", "LF": "200.00", "PCX": ""},
                           **{"DBZ_Comparison_Tag": "ALT 2 · CPU, GPU",
                              "LF_Comparison_Tag": "MATCH", "PCX_Comparison_Tag": "RECHECK",
                              "PCX_Price_Status": "CANDIDATE_NOT_CONFIRMED",
                              "DBZ_Offer_URL": "https://x/y"})]
    overview = overview_plan(report_for(rows), comparison_tab(rows))
    by_name = dict(zip(OVERVIEW_HEADER, overview.rows[0], strict=True))
    assert by_name["DataBlitz (PHP)"].fill == "alt2" and by_name["DataBlitz (PHP)"].link == (
        "https://x/y")
    assert by_name["Laptop Factory (PHP)"].fill == "match"
    assert by_name["PC Express (PHP)"].fill == "recheck"
    assert by_name["PC Central (PHP)"].fill == "" and by_name["PC Central (PHP)"].note == ""
    assert by_name[OVERVIEW_LABELS].value == (
        "DataBlitz: ALT 2 · CPU, GPU; PC Express: RECHECK; Laptop Factory: MATCH")


# Alternatives tab


def test_alternatives_tab_rows_columns_and_order(pool) -> None:
    fake = publish_world(pool)
    values = fake.values(ALTERNATIVES_TAB)
    assert values[0] == list(ALTERNATIVES_HEADER)
    records = [dict(zip(values[0], row + [""] * (len(values[0]) - len(row)), strict=True))
               for row in values[1:]]
    t1 = [r for r in records if r["Target ID"] == "TGT-000001"]
    # Rank, retailer order (DataBlitz, Laptop Factory, PC Express), difference count, price.
    assert [(r["Retailer"], r["Difference count"], r["Price (PHP)"]) for r in t1] == [
        ("DataBlitz", "1.0", "900.0"), ("DataBlitz", "2.0", "500.0"),
        ("Laptop Factory", "1.0", "450.0"), ("PC Express", "2.0", "650.0")]
    first = t1[0]
    assert first["Target configuration"].startswith("MSI Thin 15 B13UC-3474PH · 15.6″")
    assert first["Target configuration"].endswith("RTX 3050")
    assert first["Actual product / model code"] == "B13UC-3474PH"
    assert (first["Actual RAM (GB)"], first["Actual storage (GB)"], first["Actual GPU"]) == (
        "8.0", "512.0", "RTX 3050")
    assert first["Actual CPU (exact processor)"] == "Core i5-13420H"
    assert first["Differences (target vs retailer)"] == "RAM: target 16GB → retailer 8GB."
    assert (first["RAM comparison"], first["Storage comparison"]) == ("differs", "agrees")
    assert first["Price eligibility"] == "Comparable"
    assert first["Shown in main comparison"] == "Yes"
    assert t1[1]["Shown in main comparison"] == "No"
    assert t1[1]["Comparison tag"] == "ALT 2 · RAM, Storage"
    assert t1[2]["Stock"] == "OUT_OF_STOCK"
    assert first["Observation ID"] and first["Evidence references"].count("ram_capacity_gb") >= 1
    assert first["Policy version"] == "close-alternatives/2026-10-27"
    # Prices are numbers in PHP format, links are links, tags are filled.
    r, c = locate(fake, ALTERNATIVES_TAB, "Price (PHP)", "TGT-000001")
    cell = fake.cell(ALTERNATIVES_TAB, r, c)
    assert cell["userEnteredFormat"]["numberFormat"]["type"] == "CURRENCY"
    link_r, link_c = locate(fake, ALTERNATIVES_TAB, "Product link", "TGT-000001")
    assert fake.cell(ALTERNATIVES_TAB, link_r, link_c)["hyperlink"].startswith("https://")
    tag_r, tag_c = locate(fake, ALTERNATIVES_TAB, "Comparison tag", "TGT-000001")
    assert fake.sheets[ALTERNATIVES_TAB]["layout"][(tag_r, tag_c)]["backgroundColor"] == (
        STATUS_FILLS["alt1"])


# Review


def test_a_confirmed_alternative_is_not_a_matching_problem_in_review(tmp_path) -> None:
    world = build_world(tmp_path, [msi("lf-b12", code="B12UC-1435PH", price="800.00")])
    fake = publish_world(world)
    values = fake.values(REVIEW_TAB)
    rows = [dict(zip(values[0], row + [""] * (len(values[0]) - len(row)), strict=True))
            for row in values[1:]]
    close = [r for r in rows if r["Category"] == "Close alternative (confirmed)"]
    assert len(close) == 1 and close[0]["Review reason"] == "CLOSE_ALTERNATIVE"
    assert "ALT 1 · Model for TGT-000001" in close[0]["Details"]
    assert close[0]["Retrieved (Asia/Singapore)"].startswith("2026-10-07T12:00:00")
    assert not [r for r in rows if r["Category"] == "Matching problem"
                and r["Observation ID"] == close[0]["Observation ID"]]


# Layout, requests and readback


def test_fill_requests_follow_the_layout_and_pass_the_guard(hidden) -> None:
    plan = plan_of(hidden)
    tab = plan.tabs[OVERVIEW_TAB]
    state = TabState(OVERVIEW_TAB, True, 7, 100, 20)
    requests = tab_requests(state, tab.grid(), tab.width)
    guard_requests(requests, {7})
    expected = fill_requests(7, tab.grid())
    assert expected and requests[-len(expected):] == expected
    # Everything before the fills is the content and layout; the fills only paint backgrounds.
    assert all("repeatCell" in r and r["repeatCell"]["fields"] == (
        "userEnteredFormat.backgroundColor,userEnteredFormat.backgroundColorStyle")
        for r in expected)
    assert len(requests) > len(expected) + 5
    # Adjacent cells of one fill share a request.
    grid = [[plan.tabs[OVERVIEW_TAB].rows[0][0]]]
    assert fill_requests(7, grid) == []


def test_readback_verifies_fills_and_notes(hidden) -> None:
    fake = publish_world(hidden)
    plan = plan_of(hidden)
    tab = plan.tabs[OVERVIEW_TAB]
    assert verify_tab(read_tab(fake, OVERVIEW_TAB), tab.grid()) == []
    column = OVERVIEW_HEADER.index("Laptop Factory (PHP)")
    fake.sheets[OVERVIEW_TAB]["layout"][(1, column)]["backgroundColor"] = STATUS_FILLS["alt2"]
    issues = verify_tab(read_tab(fake, OVERVIEW_TAB), tab.grid())
    assert issues == [f"{OVERVIEW_TAB} row 2 column {column + 1} fill differs"]
    fake.sheets[OVERVIEW_TAB]["layout"][(1, column)]["backgroundColor"] = STATUS_FILLS["match"]
    fake.sheets[OVERVIEW_TAB]["grid"][(1, column)]["note"] = "changed"
    assert verify_tab(read_tab(fake, OVERVIEW_TAB), tab.grid()) == [
        f"{OVERVIEW_TAB} row 2 column {column + 1} note differs"]


def test_publication_logs_the_alternative_counts(pool) -> None:
    fake = publish_world(pool)
    values = fake.values(RUN_LOG_TAB)
    record = dict(zip(values[0], values[1], strict=True))
    assert values[0][-3:] == ["Alternative prices shown", "Alternative association rows",
                              "Alternatives rows published"]
    assert record["Publisher version"] == "pilot-sheets-7"
    assert record["Alternative prices shown"] == "4.0"  # TGT-000001: three; TGT-000002: one
    assert float(record["Alternative association rows"]) == float(
        record["Alternatives rows published"])


# Validation of the new report files


def _edit_csv(path: Path, change) -> None:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns, rows = reader.fieldnames, list(reader)
    change(rows)
    write_dicts(path, columns, rows)


@pytest.mark.parametrize(
    ("file", "change", "message"),
    [
        ("weekly_price_alternatives.csv",
         lambda rows: rows[0].update(price="1.00"), "price or link is not the observation's own"),
        ("weekly_price_alternatives.csv",
         lambda rows: rows[0].update(difference_count="3"), "difference count"),
        ("weekly_price_alternatives.csv",
         lambda rows: rows[0].update(alternative_tag="ALT 2 · CPU"), "tag does not match"),
        ("weekly_price_alternatives.csv",
         lambda rows: rows[0].update(observation_id="0" * 64), "no history observation"),
        ("weekly_price_alternatives.csv",
         lambda rows: rows[0].update(retrieved_at_utc="2026-10-20T00:00:00.000000Z"),
         "observation time differs"),
        ("weekly_price_alternatives.csv",
         lambda rows: rows.append(dict(rows[0])), "is repeated"),
        ("weekly_price_comparison.csv",
         lambda rows: rows[0].update(Lowest_Alternative_Price="1.00"), "disagrees with the "
         "alternatives report"),
        ("weekly_price_comparison.csv",
         lambda rows: rows[0].update(Alternative_Retailer_Count="2"), "alternative retailer"),
        ("weekly_price_comparison.csv",
         lambda rows: rows[0].update(Comparison_Retailer_Count="5"), "retailers-shown"),
        ("weekly_price_comparison.csv",
         lambda rows: rows[0].update(LF_Price_Status="SOMETHING_NEW"), "disagrees with the "
         "selections"),
    ],
)
def test_tampered_alternative_reports_are_refused(tmp_path, file, change, message) -> None:
    world = build_world(tmp_path, [msi("lf-8gb", price="700.00", ram="8")])
    _edit_csv(world["report"] / file, change)
    with pytest.raises(PublicationInputError) as caught:
        load_pilot_report(world["report"], world["scope"], selected_list=world["selected"])
    assert message in " ".join(caught.value.issues)


def test_an_alternative_cell_must_be_backed_by_its_association(tmp_path) -> None:
    world = build_world(tmp_path, [msi("lf-8gb", price="700.00", ram="8")])
    _edit_csv(world["report"] / "weekly_price_alternatives.csv",
              lambda rows: [r.update(displayed_in_comparison="FALSE") for r in rows])
    with pytest.raises(PublicationInputError) as caught:
        load_pilot_report(world["report"], world["scope"], selected_list=world["selected"])
    assert "comparable, displayed alternative association" in " ".join(caught.value.issues)


def test_the_preview_prints_the_alternative_counts_and_never_mutates(hidden, capsys) -> None:
    code, fake, opened = run(hidden, mode="--preview")
    out = capsys.readouterr().out
    assert code == 0 and opened == [True] and fake.mutations == []
    assert "Close alternatives (not matching configurations" in out
    assert "PILOT_PRICE_ALTERNATIVES: new tab; would write 1 header + 2 rows" in out
    folder = sorted(hidden["out"].iterdir())[-1]
    assert (folder / f"plan_{ALTERNATIVES_TAB}.csv").is_file()
    assert (folder / f"plan_{ALTERNATIVES_TAB}_notes.csv").is_file()
    report = json.loads((folder / "publication_report.json").read_text())
    assert report["planned_rows"][ALTERNATIVES_TAB] == 2
    assert report["counts"]["alternative_rows"] == 2


def test_repeating_the_publication_does_not_duplicate_alternative_rows(hidden) -> None:
    fake = FakeSpreadsheet()
    assert run(hidden, fake=fake)[0] == 0
    first = fake.values(ALTERNATIVES_TAB)
    assert run(hidden, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))[0] == 0
    assert fake.values(ALTERNATIVES_TAB) == first and len(first) == 3
