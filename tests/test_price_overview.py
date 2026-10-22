"""PILOT_PRICE_OVERVIEW (pilot-sheets-6): a publisher-managed rank/spec/price view, the safe
adoption of the owner-built formula overview, full snapshots, rollback and the house layout."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_sheet_publication import (
    FakeSpreadsheet,
    build_reports,
    column,
    latest_report,
    run,
)
from test_shopee_summary import ledger

from laptop_tracker.price_tracking import RETAILERS
from laptop_tracker.sheet_publication import (
    BLANK,
    COMPARISON_TAB,
    DATA_TABS,
    MANUAL_OVERVIEW_HEADER,
    OVERVIEW_HEADER,
    OVERVIEW_TAB,
    PUBLISHER_TABS,
    RETAILER_NAMES,
    RUN_LOG_TAB,
    SUMMARY_TAB,
    PilotReport,
    PublicationInputError,
    TabPlan,
    TabState,
    adoptable,
    build_plan,
    comparison_header,
    destination_problems,
    displayed_differences,
    load_pilot_report,
    number,
    overview_issues,
    overview_plan,
    restore_requests,
    tab_layout,
    text,
    verify_layout,
)

pytestmark = pytest.mark.usefixtures("no_network")


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    return build_reports(tmp_path, shopee_ledger=ledger())


def plan_for(world) -> object:
    return build_plan(load_pilot_report(world["report"], world["scope"],
                                        selected_list=world["selected"]))


# Synthetic comparison rows for unit tests (no report pipeline needed).


def comparison_row(rank: int, *, model: str, series: str = "", screen: str = "14.0",
                   prices: dict[str, str] | None = None, basis: dict[str, str] | None = None,
                   **extra: str) -> dict[str, str]:
    row = {"rank": str(rank), "target_id": f"TGT-{rank:06d}", "Brand": "Acme", "Series": series,
           "Model": model, "Screen Size": screen, "CPU_Series": "Core 5", "RAM_Capacity": "16",
           "Storage_Capacity": "512", "Shopee_Price": "", **extra}
    for code, _ in RETAILERS:
        row[f"{code}_Price"] = (prices or {}).get(code, "")
        row[f"{code}_Match_Basis"] = (basis or {}).get(code, "")
    return row


def comparison_tab(rows: list[dict[str, str]]) -> TabPlan:
    header = comparison_header()
    cells = []
    for row in rows:
        values = dict.fromkeys(header, BLANK) | {
            "Rank": number(row["rank"], "integer"), "Target ID": text(row["target_id"]),
            "Brand": text(row["Brand"]), "Series": text(row["Series"]),
            "Model": text(row["Model"]), "Screen (in)": number(row["Screen Size"], "decimal"),
            "CPU family": text(row["CPU_Series"]),
            "RAM (GB)": number(row["RAM_Capacity"], "integer"),
            "Storage (GB)": number(row["Storage_Capacity"], "integer"),
            "Shopee price (PHP, snapshot)": number(row["Shopee_Price"], "php"),
        }
        for code, _ in RETAILERS:
            values[f"{RETAILER_NAMES[code]} price (PHP)"] = number(row[f"{code}_Price"], "php")
        cells.append(tuple(values[name] for name in header))
    return TabPlan(COMPARISON_TAB, header, tuple(cells))


def report_for(rows: list[dict[str, str]]) -> PilotReport:
    return PilotReport(
        report_dir=Path("."), scope=[], comparison=rows, selections={}, history=[], review=[],
        collection_review=[], collection_summary=None, fingerprints={},
        summary={"report_week": {"start": "2026-10-19", "end_exclusive": "2026-10-26"},
                 "sales_snapshot": {"extract_date": "2026-09-28"}},
    )


# The overview plan


def test_overview_has_twelve_columns_in_the_requested_order(world) -> None:
    plan = plan_for(world)
    overview = plan.tabs[OVERVIEW_TAB]
    assert overview.header == OVERVIEW_HEADER == (
        "Rank", "Laptop", "Screen (in)", "CPU family", "RAM (GB)", "Storage (GB)",
        "Shopee (PHP, historical snapshot)", "DataBlitz (PHP)", "PC Express (PHP)",
        "Laptop Factory (PHP)", "PC Central (PHP)", "Octagon (PHP)")
    assert len(plan.tabs[SUMMARY_TAB].header) == 7


def test_overview_rows_follow_the_comparison_and_repeat_its_cells(world) -> None:
    plan = plan_for(world)
    comparison, overview = plan.tabs[COMPARISON_TAB], plan.tabs[OVERVIEW_TAB]
    assert len(overview.rows) == len(comparison.rows) == 7
    target = comparison.header.index("Target ID")
    for detailed, brief in zip(comparison.rows, overview.rows, strict=True):
        assert f"Target ID {detailed[target].value} " in brief[1].note
        assert brief[0] == replace(detailed[0], note="", italic=False)
        for code in ("DBZ", "PCX", "LF", "PCC", "OCT"):
            mine = brief[OVERVIEW_HEADER.index(f"{RETAILER_NAMES[code]} (PHP)")]
            theirs = detailed[comparison.header.index(f"{RETAILER_NAMES[code]} price (PHP)")]
            assert (mine.kind, mine.value, mine.number_format) == (
                theirs.kind, theirs.value, theirs.number_format)
            if theirs.kind == "blank":
                assert mine.kind == "blank"  # a blank comparable price stays blank
            else:
                assert mine.kind == "number" and mine.number_format == "php"
    ranks = [int(row[0].value) for row in overview.rows]
    assert ranks == sorted(ranks)
    assert overview_issues(plan.report, comparison, overview) == []


def test_a_hundred_row_overview_keeps_order_identity_and_blanks() -> None:
    rows = [comparison_row(rank, model=f"M{rank:03d}-X",
                           prices={"DBZ": f"{1000 + rank}.00"} if rank % 3 == 0 else {})
            for rank in range(1, 101)]
    comparison = comparison_tab(rows)
    overview = overview_plan(report_for(rows), comparison)
    assert len(overview.rows) == 100 and overview.width == 12
    assert [r[0].value for r in overview.rows] == [str(n) for n in range(1, 101)]
    assert [r[1].note.split(" ")[2] for r in overview.rows] == [row["target_id"] for row in rows]
    priced = [r for r in overview.rows if r[OVERVIEW_HEADER.index("DataBlitz (PHP)")].value]
    assert len(priced) == 33
    blank = sum(1 for r in overview.rows for cell in r[7:] if cell.kind == "blank")
    assert blank == 100 * 5 - 33
    assert overview_issues(report_for(rows), comparison, overview) == []


def test_repeated_labels_are_distinguished_and_never_merged() -> None:
    rows = [comparison_row(1, model="IdeaPad", series="Slim 3", Resolution="1920x1080"),
            comparison_row(2, model="IdeaPad", series="Slim 3", Resolution="1920x1200"),
            comparison_row(3, model="IdeaPad", series="Slim 3", Resolution="1920x1200")]
    comparison = comparison_tab(rows)
    overview = overview_plan(report_for(rows), comparison)
    laptops = [r[1].value for r in overview.rows]
    assert len(set(laptops)) == 3
    assert laptops[0] == "Acme Slim 3 IdeaPad · 1920x1080"
    assert laptops[1].endswith("configuration 1 of 2") and laptops[2].endswith(
        "configuration 2 of 2")
    # Rows that read the same are refused, so two targets are never shown as one.
    merged = TabPlan(OVERVIEW_TAB, OVERVIEW_HEADER, (overview.rows[1], replace_note(
        overview.rows[1], overview.rows[2][1].note), overview.rows[0]))
    issues = overview_issues(report_for(rows), comparison, merged)
    assert any("read the same" in issue for issue in issues)


def replace_note(row: tuple, note: str) -> tuple:
    return (row[0], replace(row[1], note=note), *row[2:])


def test_listed_spec_prices_are_italic_and_prices_are_not_reused_across_targets() -> None:
    rows = [comparison_row(1, model="A1", prices={"OCT": "48999.00"}, basis={"OCT": "LISTED_SPEC"}),
            comparison_row(2, model="A2", prices={"LF": "500.00"},
                           basis={"LF": "TITLE_MODEL_CORROBORATED"})]
    overview = overview_plan(report_for(rows), comparison_tab(rows))
    octagon = OVERVIEW_HEADER.index("Octagon (PHP)")
    factory = OVERVIEW_HEADER.index("Laptop Factory (PHP)")
    assert overview.rows[0][octagon].italic and overview.rows[0][octagon].value == "48999.00"
    assert not overview.rows[1][factory].italic
    assert overview.rows[1][octagon].kind == "blank" and overview.rows[0][factory].kind == "blank"


def test_a_shifted_overview_is_refused() -> None:
    rows = [comparison_row(1, model="A1", prices={"DBZ": "1.00"}),
            comparison_row(2, model="A2", prices={"DBZ": "2.00"})]
    comparison = comparison_tab(rows)
    overview = overview_plan(report_for(rows), comparison)
    swapped = TabPlan(OVERVIEW_TAB, OVERVIEW_HEADER, (overview.rows[1], overview.rows[0]))
    assert overview_issues(report_for(rows), comparison, swapped)
    with pytest.raises(PublicationInputError):
        bad = comparison_tab(rows[:1])
        issues = overview_issues(report_for(rows), bad, overview)
        raise PublicationInputError(issues)


# Publication: creation, adoption of the owner-built overview, idempotency, rollback


def manual_overview() -> list[list[str]]:
    """The owner-built overview as found live on 2026-10-22: header plus a formula row."""
    return [list(MANUAL_OVERVIEW_HEADER), ["=IFNA(ARRAYFORMULA(FILTER(...)),\"\")"]]


def with_formulas(fake: FakeSpreadsheet) -> FakeSpreadsheet:
    grid = fake.sheets[OVERVIEW_TAB]["grid"]
    for c in range(12):
        grid[(1, c)] = {"userEnteredValue": {"formulaValue": f"=FORMULA_{c}()"},
                        "formattedValue": "1"}
        grid[(0, c)]["note"] = "Linked to PILOT_PRICE_COMPARISON"
    return fake


def test_a_new_overview_tab_is_created_and_published(world) -> None:
    code, fake, _ = run(world)
    assert code == 0
    values = fake.values(OVERVIEW_TAB)
    assert values[0] == list(OVERVIEW_HEADER)
    assert len([r for r in values if any(r)]) == 8
    assert column(fake, OVERVIEW_TAB, "Rank") == column(fake, COMPARISON_TAB, "Rank")
    assert set(latest_report(world)["tabs_written"]) == set(PUBLISHER_TABS)


def test_the_owner_built_overview_is_refused_without_explicit_adoption(world, capsys) -> None:
    fake = with_formulas(FakeSpreadsheet({OVERVIEW_TAB: manual_overview()}))
    code, fake, _ = run(world, fake=fake)
    assert code == 1 and fake.mutations == []
    assert "--adopt-overview" in capsys.readouterr().err
    assert fake.sheets[OVERVIEW_TAB]["grid"][(1, 0)]["userEnteredValue"]["formulaValue"]


def test_preview_shows_the_overview_migration_without_writing(world, capsys) -> None:
    fake = with_formulas(FakeSpreadsheet({OVERVIEW_TAB: manual_overview()}))
    code, fake, opened = run(world, mode="--preview", fake=fake)
    assert (code, opened, fake.mutations) == (0, [True], [])
    report = latest_report(world)
    assert report["status"] == "PREVIEW_ONLY_ADOPTION_REQUIRED"
    assert report["overview_migration"]["current_formula_cells"] == 12
    folder = sorted(world["out"].iterdir())[-1]
    assert (folder / f"current_{OVERVIEW_TAB}.csv").is_file()
    assert (folder / f"plan_{OVERVIEW_TAB}.csv").is_file()
    assert "owner-built layout (needs --adopt-overview)" in capsys.readouterr().out


def test_adoption_snapshots_replaces_and_rolls_back(world) -> None:
    fake = with_formulas(FakeSpreadsheet({OVERVIEW_TAB: manual_overview()}))
    code, fake, _ = run(world, fake=fake, extra=("--adopt-overview",))
    assert code == 0
    report = latest_report(world)
    assert report["status"] == "SUCCESS" and report["adopted_tabs"] == [OVERVIEW_TAB]
    folder = sorted(world["out"].iterdir())[-1]
    backup = json.loads((folder / f"backup_{OVERVIEW_TAB}.json").read_text())
    cells = backup["snapshot"]["data"][0]["rowData"][1]["values"]
    assert cells[0]["userEnteredValue"]["formulaValue"] == "=FORMULA_0()"
    # Static, publisher-written values now; no formula is left.
    grid = fake.sheets[OVERVIEW_TAB]["grid"]
    assert not any("formulaValue" in (cell.get("userEnteredValue") or {})
                   for cell in grid.values())
    assert fake.values(OVERVIEW_TAB)[0] == list(OVERVIEW_HEADER)
    log = fake.values(RUN_LOG_TAB)
    assert log[-1][log[0].index("Adopted tabs")] == OVERVIEW_TAB

    # The tab is the publisher's now: the next cycle needs no flag and is idempotent.
    before = {t: fake.values(t) for t in DATA_TABS}
    code, fake, _ = run(world, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))
    assert code == 0 and {t: fake.values(t) for t in DATA_TABS} == before


def run_rollback(world, fake: FakeSpreadsheet, folder: Path) -> int:
    from test_verified_top import load_script

    return load_script("publish_prices").main(
        ["--rollback", str(folder), "--output-root", str(world["out"])],
        spreadsheet_factory=lambda read_only: fake,
        now=datetime(2026, 10, 12, 4, tzinfo=UTC))


def test_rollback_restores_the_owner_built_overview(world) -> None:
    fake = with_formulas(FakeSpreadsheet({OVERVIEW_TAB: manual_overview()}))
    assert run(world, fake=fake, extra=("--adopt-overview",))[0] == 0
    adopted = sorted(world["out"].iterdir())[-1]
    log_before = fake.values(RUN_LOG_TAB)
    assert run_rollback(world, fake, adopted) == 0
    grid = fake.sheets[OVERVIEW_TAB]["grid"]
    assert grid[(1, 3)]["userEnteredValue"] == {"formulaValue": "=FORMULA_3()"}
    assert grid[(0, 6)]["note"] == "Linked to PILOT_PRICE_COMPARISON"
    assert fake.values(OVERVIEW_TAB)[0] == list(MANUAL_OVERVIEW_HEADER)
    assert fake.values(RUN_LOG_TAB) == log_before  # the run log is never rewritten
    report = json.loads((sorted(world["out"].iterdir())[-1] / "rollback_report.json")
                        .read_text())
    assert report["status"] == "SUCCESS" and OVERVIEW_TAB in report["restored"]
    # Tabs the adopting publication created had no backup and are reported, not deleted.
    assert any(SUMMARY_TAB in line for line in report["skipped"])


def test_rollback_refuses_a_different_sheet(world) -> None:
    fake = with_formulas(FakeSpreadsheet({OVERVIEW_TAB: manual_overview()}))
    assert run(world, fake=fake, extra=("--adopt-overview",))[0] == 0
    adopted = sorted(world["out"].iterdir())[-1]
    del fake.sheets[OVERVIEW_TAB]
    fake.add_worksheet(OVERVIEW_TAB, 10, 12)  # recreated tab: a new sheet ID
    assert run_rollback(world, fake, adopted) == 1
    report = json.loads((sorted(world["out"].iterdir())[-1] / "rollback_report.json")
                        .read_text())
    assert report["status"] == "DESTINATION_REFUSED"


def test_foreign_overview_content_is_refused_even_with_the_adoption_flag(world, capsys) -> None:
    fake = FakeSpreadsheet({OVERVIEW_TAB: [["My own analysis"], ["keep me"]]})
    code, fake, _ = run(world, fake=fake, extra=("--adopt-overview",))
    assert code == 1 and fake.mutations == []
    assert f"{OVERVIEW_TAB} has content that this publisher did not write" in (
        capsys.readouterr().err)


def test_extra_columns_make_an_owner_overview_unrecognised() -> None:
    fake = FakeSpreadsheet({OVERVIEW_TAB: [[*MANUAL_OVERVIEW_HEADER, "My column"]]})
    from laptop_tracker.sheet_publication import read_tab

    state = read_tab(fake, OVERVIEW_TAB)
    assert not adoptable(state)
    states = {t: TabState(t, False) for t in PUBLISHER_TABS} | {OVERVIEW_TAB: state}
    headers = dict.fromkeys(PUBLISHER_TABS, ("x",))
    problems, adopted = destination_problems(states, headers, (OVERVIEW_TAB,))
    assert adopted == [] and problems


def test_other_tabs_are_never_touched_by_publication_or_rollback(world) -> None:
    fake = with_formulas(FakeSpreadsheet({OVERVIEW_TAB: manual_overview(),
                                          "raw": [["grass_date"], ["2026-09-28"]],
                                          "TARGET_MASTER": [["target_id"], ["TGT-000001"]]}))
    protected = {t: copy.deepcopy(fake.sheets[t]) for t in ("raw", "TARGET_MASTER")}
    assert run(world, fake=fake, extra=("--adopt-overview",))[0] == 0
    assert run_rollback(world, fake, sorted(world["out"].iterdir())[-1]) == 0
    assert {t: fake.sheets[t] for t in protected} == protected


def test_migration_preview_compares_displayed_values() -> None:
    plan = TabPlan(OVERVIEW_TAB, ("Rank", "Price"), ((number("1", "integer"),
                                                      number("89990.00", "php")),))
    snapshot = {"data": [{"rowData": [
        {"values": [{"formattedValue": "Rank"}, {"formattedValue": "Price"}]},
        {"values": [{"formattedValue": "1", "userEnteredValue": {"formulaValue": "=A"}},
                    {"formattedValue": "₱89,990.00"}]}]}]}
    assert displayed_differences(snapshot, plan)["cells_differing"] == 0
    snapshot["data"][0]["rowData"][1]["values"][1]["formattedValue"] = "₱1.00"
    result = displayed_differences(snapshot, plan)
    assert result["cells_differing"] == 1 and result["differences"][0]["cell"] == "B2"
    assert result["current_formula_cells"] == 1


def test_restore_requests_cover_cells_panes_sizes_and_filter() -> None:
    snapshot = {"properties": {"gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2,
                                                  "hideGridlines": True}},
                "basicFilter": {"range": {"sheetId": 7, "startRowIndex": 0}},
                "data": [{"rowData": [{"values": [{"userEnteredValue": {"formulaValue": "=1"},
                                                   "formattedValue": "1", "note": "n"}]}],
                          "columnMetadata": [{"pixelSize": 52}],
                          "rowMetadata": [{"pixelSize": 64}]}]}
    requests = restore_requests(snapshot, 9)
    kinds = [next(iter(r)) for r in requests]
    assert kinds == ["updateCells", "updateCells", "updateSheetProperties",
                     "updateDimensionProperties", "updateDimensionProperties",
                     "setBasicFilter"]
    written = requests[1]["updateCells"]["rows"][0]["values"][0]
    assert written == {"userEnteredValue": {"formulaValue": "=1"}, "note": "n"}
    assert requests[-1]["setBasicFilter"]["filter"]["range"]["sheetId"] == 9
    assert all(9 == r[next(iter(r))].get("range", r[next(iter(r))].get("properties", {}))
               .get("sheetId", 9) for r in requests)


# House layout


def test_house_layout_freezes_key_columns_and_reads_back(world) -> None:
    code, fake, _ = run(world)
    assert code == 0
    expect = {COMPARISON_TAB: 5, SUMMARY_TAB: 1, OVERVIEW_TAB: 2}
    for title, frozen in expect.items():
        sheet = fake.sheets[title]
        assert (sheet["frozen"], sheet["frozen_columns"], sheet["gridlines_hidden"]) == (
            1, frozen, True)
        layout = tab_layout(title, fake.values(title)[0])
        assert [sheet["widths"][c] for c in range(len(layout.columns))] == [
            c.width for c in layout.columns]
        assert sheet["heights"][0] == layout.header_height
    comparison = fake.sheets[COMPARISON_TAB]
    assert comparison["widths"][0] < comparison["widths"][4]  # rank narrow, model wide
    assert comparison["filter"]["endColumnIndex"] == 56
    # Readback catches a width someone changed after the write.
    plan = plan_for(world)
    comparison["widths"][4] = 999
    assert verify_layout(fake, plan.tabs[COMPARISON_TAB]) == [
        f"{COMPARISON_TAB} column E width differs"]


def test_history_review_and_run_log_keep_their_plain_layout() -> None:
    for title in (*[t for t in DATA_TABS if t not in {COMPARISON_TAB, SUMMARY_TAB,
                                                      OVERVIEW_TAB}], RUN_LOG_TAB):
        assert tab_layout(title, ["A"]) is None


def test_comparison_columns_used_by_live_conditional_formats_stay_put() -> None:
    """The owner's conditional formats on the live comparison address fixed columns (status in
    Q, W, AC, AI, AO; prices in O, U, AA, AG, AM; lowest retailer price in AS). Moving them
    would silently break those rules."""
    header = comparison_header()
    letters = {"O": 14, "Q": 16, "U": 20, "W": 22, "AA": 26, "AC": 28, "AG": 32, "AI": 34,
               "AM": 38, "AO": 40, "AS": 44}
    names = [RETAILER_NAMES[code] for code, _ in RETAILERS]
    expected = {"AS": "Lowest retailer price (PHP)"}
    for name, (price, status) in zip(names, (("O", "Q"), ("U", "W"), ("AA", "AC"),
                                             ("AG", "AI"), ("AM", "AO")), strict=True):
        expected |= {price: f"{name} price (PHP)", status: f"{name} status"}
    assert {letter: header[index] for letter, index in letters.items()} == expected
    assert len(header) == 56
