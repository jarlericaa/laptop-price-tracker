"""Shopee-variation Sheets publisher (shopee-variation-sheets-1), tested against an in-memory fake.

The fake workbook starts with the prepared (empty) template tabs, plus source/archive tabs that must
never be written. Reports are built through the real CLIs from synthetic inputs.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import test_sheet_publication as tsp
from scraping_fakes import no_network  # noqa: F401
from test_sheet_publication import TARGETS, FakeSpreadsheet
from test_supporting_tables import map_row
from test_target_master import target_values
from test_variation_ranking import vs
from test_verified_top import load_script

from laptop_tracker.price_comparison_view import PRICE_HEADER, PRICE_TAB
from laptop_tracker.sheet_publication import (
    RETIRED_TABS,
    STATUS_FILLS,
    DestinationError,
    assert_publisher_tab,
    rollback,
)
from laptop_tracker.supporting_tables import SOURCE_MODEL_MAP_HEADERS, validate_source_model_map
from laptop_tracker.target_master import validate_target_master
from laptop_tracker.variation_publication import (
    ALTERNATIVES_TAB,
    COMPARISON_TAB,
    CONTRACT_PATH,
    DATA_TABS,
    HISTORY_TAB,
    LAYOUT_VERSION,
    LEGACY_HISTORY_HEADER,
    LEGACY_REVIEW_HEADER,
    OVERVIEW_TAB,
    PUBLISHER_TABS,
    REVIEW_TAB,
    RUN_LOG_TAB,
    TOP_TAB,
    build_variation_plan,
    comparison_header,
    legacy_run_log_header,
    load_contract,
    load_variation_report,
)
from laptop_tracker.variation_ranking import (
    SCHEMA_VERSION,
    SELECTION_POLICY,
    build_variation_ranking,
    build_variation_snapshot,
    write_variation_outputs,
)

pytestmark = pytest.mark.usefixtures("no_network")

SALES = {
    "run_id": "20260928T112328+0800",
    "extract_date": "2026-09-28",
    "m_minus_1_month": "2026-08",
    "mtd_month": "2026-09",
}
NOW = datetime(2026, 10, 12, 2, tzinfo=UTC)
CONTRACT = load_contract()


def sources() -> list[dict[str, str]]:
    rows = []
    for i in range(1, 11):
        rows.append(
            vs(f"{i}01", "100", "10", shop="10", item=str(i), price=f"{1000 + i}.0", **SALES)
        )
    # Two more sellers of targets 1 and 2 (shared contexts) and two unmapped variations, which
    # are eligible but never reach the Top 100 (owner decision 2026-11-03).
    rows.append(vs("102", "90", "9", shop="11", item="21", price="990.0", **SALES))
    rows.append(vs("202", "80", "8", shop="12", item="22", price="1900.0", **SALES))
    rows.append(vs("9901", "70", "7", shop="13", item="31", price="777.0", **SALES))
    rows.append(vs("9902", "60", "6", shop="14", item="32", price="", **SALES))
    return rows


def build_world(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """Variation ranking -> context report (real Phase 9 pipeline) -> variation reports."""
    target_master = validate_target_master(target_values(*TARGETS))
    links = [(f"{i}01", f"TGT-0000{i:02d}") for i in range(1, 11)] + [
        ("102", "TGT-000001"),
        ("202", "TGT-000002"),
    ]
    mapping = validate_source_model_map(
        [SOURCE_MODEL_MAP_HEADERS, *(map_row(source_model_id=s, target_id=t) for s, t in links)],
        target_master,
    )
    snapshot = build_variation_snapshot(sources())
    result = build_variation_ranking(
        snapshot,
        target_master,
        mapping,
        target_master_sha256=tsp.TM_SHA,
        shop_names={"10|1|101": "Alpha Store", "11|21|102": "Beta Store"},
    )
    variation_dir = tmp_path / "variation"
    write_variation_outputs(
        variation_dir,
        result,
        {
            "schema_version": SCHEMA_VERSION,
            "ranking_grain": "SHOPEE_VARIATION",
            "selection_policy": SELECTION_POLICY,
            "counts": result.counts,
        },
    )
    monkeypatch.setattr(tsp, "list_rows", lambda: result.context_list)
    monkeypatch.setattr(tsp, "ledger_rows", lambda: result.context_ledger)
    context = tsp.build_reports(tmp_path / "context")
    assert (variation_dir / "context_selected_list.csv").read_bytes() == (
        context["selected"].read_bytes()
    )
    code = load_script("build_variation_reports").main(
        [
            "--variation-dir",
            str(variation_dir),
            "--context-report-dir",
            str(context["report"]),
            "--output-dir",
            str(tmp_path / "vreports"),
        ]
    )
    assert code == 0
    return {
        "variation": variation_dir,
        "reports": tmp_path / "vreports",
        "context": context["report"],
        "collection": context["report"].parent / "collection",
        "out": tmp_path / "publications",
    }


@pytest.fixture
def world(tmp_path, monkeypatch):
    return build_world(tmp_path, monkeypatch)


def template_tabs(**extra: list[list[str]]) -> dict[str, list[list[str]]]:
    """The prepared workbook: empty templates carrying the contract headers, plus other tabs."""
    tabs = {
        TOP_TAB: [list(CONTRACT.headers[TOP_TAB])],
        COMPARISON_TAB: [list(CONTRACT.headers[COMPARISON_TAB])],
        OVERVIEW_TAB: [list(CONTRACT.headers[OVERVIEW_TAB])],
        ALTERNATIVES_TAB: [list(CONTRACT.headers[ALTERNATIVES_TAB])],
        HISTORY_TAB: [[*LEGACY_HISTORY_HEADER, *CONTRACT.appended[HISTORY_TAB]]],
        REVIEW_TAB: [[*LEGACY_REVIEW_HEADER, *CONTRACT.appended[REVIEW_TAB]]],
        RUN_LOG_TAB: [[*legacy_run_log_header(), *CONTRACT.appended[RUN_LOG_TAB]]],
        "raw": [["grass_date", "shopid"], ["2026-10-01", "1"]],
        "TARGET_MASTER": [["target_id"], ["TGT-000001"]],
        "ARCHIVE_PILOT_PRICE_COMPARISON_20261027": [["Rank", "Target ID"], ["1", "TGT-000001"]],
    }
    return tabs | extra


class SpyingSpreadsheet(FakeSpreadsheet):
    """Records which tabs are read and every title that is mutated."""

    def __init__(self, tabs=None) -> None:
        super().__init__(tabs)
        self.read_titles: list[str] = []

    def fetch_sheet_metadata(self, params=None):
        if "ranges" in params:
            self.read_titles.append(params["ranges"].strip("'"))
        return super().fetch_sheet_metadata(params)


def run(world, mode="--write-sheets", fake=None, *, now=NOW, reports=None, extra=()):
    fake = fake if fake is not None else SpyingSpreadsheet(template_tabs())
    opened: list[bool] = []

    def factory(read_only: bool):
        opened.append(read_only)
        fake.read_only = read_only
        return fake

    code = load_script("publish_variation_prices").main(
        [
            mode,
            "--variation-dir",
            str(world["variation"]),
            "--reports-dir",
            str(reports or world["reports"]),
            "--context-report-dir",
            str(world["context"]),
            "--collection-dir",
            str(world["collection"]),
            "--output-root",
            str(world["out"]),
            *extra,
        ],
        spreadsheet_factory=factory,
        now=now,
    )
    return code, fake, opened


def latest_report(world) -> dict:
    folder = sorted(world["out"].iterdir())[-1]
    return json.loads((folder / "publication_report.json").read_text())


# Banner rows (reporting week and cutoff, colour legend) precede the header of these two tabs.
HEADER_ROW = {COMPARISON_TAB: 2, OVERVIEW_TAB: 2}


def header_row(title: str) -> int:
    return HEADER_ROW.get(title, 0)


def header_of(fake, title: str) -> list[str]:
    return fake.values(title)[header_row(title)]


def data_cell(fake, title: str, n: int, name: str) -> dict:
    """The ``n``-th data row's (1-based) cell in the column called ``name``."""
    return fake.cell(title, header_row(title) + n, header_of(fake, title).index(name))


def column(fake, title: str, name: str) -> list[str]:
    values = fake.values(title)
    top = header_row(title)
    index = values[top].index(name)
    return [row[index] if index < len(row) else "" for row in values[top + 1 :]]


# Contract


def test_the_contract_headers_are_the_exact_owner_supplied_headers(world) -> None:
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    plan = build_variation_plan(report, CONTRACT)

    assert CONTRACT.schema_version == "shopee-variation-sheets-1" == SCHEMA_VERSION
    for title in (TOP_TAB, COMPARISON_TAB, OVERVIEW_TAB, ALTERNATIVES_TAB):
        assert plan.tabs[title].header == CONTRACT.headers[title]
    assert plan.tabs[HISTORY_TAB].header == (
        *LEGACY_HISTORY_HEADER,
        *CONTRACT.appended[HISTORY_TAB],
    )
    assert plan.tabs[REVIEW_TAB].header == (*LEGACY_REVIEW_HEADER, *CONTRACT.appended[REVIEW_TAB])
    assert plan.run_log_header[-10:] == CONTRACT.appended[RUN_LOG_TAB]
    # The compact comparison (layout variation-compact-1): the publisher's one definition, the
    # contract file and the plan agree, and the 80-column header of variation-sheets-1 is kept as
    # an earlier layout so a live tab still carrying it is recognised as this publisher's own.
    assert CONTRACT.headers[COMPARISON_TAB] == comparison_header()
    assert CONTRACT.layout_version == LAYOUT_VERSION == "variation-business-1"
    # The business tab: one definition, the contract file and the plan agree; it is the last data
    # tab (written after the technical tabs it is projected from).
    assert CONTRACT.headers[PRICE_TAB] == PRICE_HEADER and len(PRICE_HEADER) == 19
    assert plan.tabs[PRICE_TAB].header == PRICE_HEADER and DATA_TABS[-1] == PRICE_TAB
    (earlier,) = CONTRACT.earlier_headers[COMPARISON_TAB]
    assert len(earlier) == 80
    assert set(earlier) - set(comparison_header()) == {"Shopee price (PHP, snapshot)"}
    assert CONTRACT.headers[TOP_TAB][:3] == ("rank", "comparison_row_id", "source_key")
    assert json.loads(CONTRACT_PATH.read_text())["retired_tabs"] == list(RETIRED_TABS)
    assert not set(PUBLISHER_TABS) & set(RETIRED_TABS)


# 12. Preview accepts the new headers and rejects the wrong grain


def test_preview_accepts_the_prepared_headers_and_changes_nothing(world, capsys) -> None:
    code, fake, opened = run(world, mode="--preview")

    assert code == 0 and opened == [True]  # read-only scope
    assert fake.mutations == []
    printed = capsys.readouterr().out
    assert "shopee-variation-sheets-1" in printed and "SHOPEE_VARIATION" in printed
    assert "Selected variations: 12" in printed
    assert "distinct configuration contexts: 10" in printed
    assert "Retired tabs (PILOT_PRICE_SUMMARY, P1_TOP_100_CONFIGURATIONS)" in printed
    assert "never written or recreated" in printed
    report = latest_report(world)
    assert report["status"] == "PREVIEW_ONLY" and report["ranking_grain"] == "SHOPEE_VARIATION"
    folder = sorted(world["out"].iterdir())[-1]
    assert (folder / "live_schema.json").is_file()
    for title in DATA_TABS:
        assert (folder / f"plan_{title}.csv").is_file()
    live = json.loads((folder / "live_schema.json").read_text())
    assert live[COMPARISON_TAB]["header"] == list(CONTRACT.headers[COMPARISON_TAB])


def test_a_live_header_that_differs_from_the_contract_is_refused(world, capsys) -> None:
    tabs = template_tabs()
    tabs[OVERVIEW_TAB] = [[*CONTRACT.headers[OVERVIEW_TAB][:-1], "Something else"]]
    code, fake, _ = run(world, mode="--preview", fake=SpyingSpreadsheet(tabs))

    assert code == 1
    assert "live header differs from the contract" in capsys.readouterr().err
    assert fake.mutations == []
    assert latest_report(world)["status"] == "DESTINATION_REFUSED"


def test_legacy_configuration_reports_are_rejected_before_any_sheets_access(
    world, tmp_path, capsys
) -> None:
    legacy = tsp.build_reports(tmp_path / "legacy")
    code, fake, opened = run(world, mode="--preview", reports=legacy["report"])

    assert code == 1 and opened == [] and fake.mutations == []  # the workbook is never opened
    err = capsys.readouterr().err
    assert "legacy configuration-grain outputs" in err and "TARGET_CONFIGURATION" in err


def test_a_variation_report_with_another_schema_or_grain_is_rejected(world, capsys) -> None:
    summary = world["reports"] / "variation_run_summary.json"
    data = json.loads(summary.read_text())
    summary.write_text(json.dumps(data | {"schema_version": "pilot-sheets-7"}))
    code, _, opened = run(world, mode="--preview")

    assert code == 1 and opened == []
    assert "is not shopee-variation-sheets-1" in capsys.readouterr().err


def test_a_template_still_holding_legacy_configuration_content_is_refused(world, capsys) -> None:
    tabs = template_tabs()
    tabs[COMPARISON_TAB] = [["Rank", "Target ID", "Brand", "Model"], ["1", "TGT-000001", "X", "Y"]]
    code, fake, _ = run(world, fake=SpyingSpreadsheet(tabs))

    assert code == 1 and fake.mutations == []
    assert "legacy configuration-grain content" in capsys.readouterr().err


def test_a_missing_template_tab_is_refused_and_never_created(world, capsys) -> None:
    tabs = template_tabs()
    del tabs[ALTERNATIVES_TAB]
    code, fake, _ = run(world, fake=SpyingSpreadsheet(tabs))

    assert code == 1 and fake.mutations == []
    assert "never created" in capsys.readouterr().err
    assert ALTERNATIVES_TAB not in fake.sheets


# 13. Retired tabs are not recreated; source, catalog and archive tabs are not written


def test_write_publishes_the_variation_tabs_and_touches_no_other_tab(world) -> None:
    fake = SpyingSpreadsheet(template_tabs())
    before = {
        t: fake.values(t)
        for t in ("raw", "TARGET_MASTER", "ARCHIVE_PILOT_PRICE_COMPARISON_20261027")
    }
    fake.read_titles.clear()
    code, fake, opened = run(world, fake=fake)
    read_titles = set(fake.read_titles)  # before the assertions below read tabs themselves

    assert code == 0 and opened == [False]
    assert latest_report(world)["status"] == "SUCCESS"
    assert latest_report(world)["readback_issues"] == []
    # The business tab is the one thing the publisher creates; every other tab is as it was.
    assert set(fake.sheets) == set(template_tabs()) | {PRICE_TAB}
    for retired in RETIRED_TABS:
        assert retired not in fake.sheets and retired not in read_titles
    assert [m for m in fake.mutations if m.startswith("add_worksheet")] == [
        f"add_worksheet:{PRICE_TAB}"
    ]
    for title, values in before.items():
        assert fake.values(title) == values  # source, catalog and archive tabs untouched
    assert read_titles <= set(PUBLISHER_TABS)


def test_retired_tabs_that_still_exist_are_neither_read_nor_written(world) -> None:
    tabs = template_tabs(
        PILOT_PRICE_SUMMARY=[["Laptop", "Shopee"], ["old", "1"]],
        P1_TOP_100_CONFIGURATIONS=[["rank"], ["1"]],
    )
    fake = SpyingSpreadsheet(tabs)
    code, fake, _ = run(world, fake=fake)
    read_titles = set(fake.read_titles)

    assert code == 0
    assert fake.values("PILOT_PRICE_SUMMARY") == [["Laptop", "Shopee"], ["old", "1"]]
    assert fake.values("P1_TOP_100_CONFIGURATIONS") == [["rank"], ["1"]]
    assert not read_titles & set(RETIRED_TABS)


def test_no_publisher_allowlist_or_rollback_can_name_a_retired_tab() -> None:
    for retired in RETIRED_TABS:
        with pytest.raises(DestinationError):
            assert_publisher_tab(retired, PUBLISHER_TABS)
    with pytest.raises(DestinationError, match="retired"):
        rollback(
            FakeSpreadsheet(),
            Path("."),
            tabs=("PILOT_PRICE_SUMMARY",),
            allowed=(*PUBLISHER_TABS, "PILOT_PRICE_SUMMARY"),
        )


def test_the_legacy_publisher_never_writes_into_a_variation_workbook(
    world, tmp_path, capsys
) -> None:
    legacy = tsp.build_reports(tmp_path / "legacy")
    fake = FakeSpreadsheet(template_tabs())
    for mode in ("--preview", "--write-sheets"):
        code, fake, _ = tsp.run(legacy, mode=mode, fake=fake)
        assert code == 1
    assert fake.mutations == []
    assert "Shopee-variation grain" in capsys.readouterr().err
    assert "PILOT_PRICE_SUMMARY" not in fake.sheets


# Content of the published tabs


def test_published_rows_are_one_per_variation_with_exact_ids_numbers_and_grain(world) -> None:
    code, fake, _ = run(world)
    assert code == 0

    ids = column(fake, TOP_TAB, "comparison_row_id")
    assert len(ids) == 12 and len(set(ids)) == 12
    assert column(fake, COMPARISON_TAB, "Comparison row ID") == ids
    assert column(fake, OVERVIEW_TAB, "Comparison row ID") == ids
    assert set(column(fake, COMPARISON_TAB, "Row grain")) == {"SHOPEE_VARIATION"}
    # Equal GMV ties break on mtd then model_id (text order): 1001 sorts before 101.
    assert column(fake, TOP_TAB, "source_key")[:2] == ["10|10|1001", "10|1|101"]
    assert column(fake, TOP_TAB, "source_model_id")[:2] == ["1001", "101"]
    # Two sellers of one target are two rows with their own seller names and GMV.
    sellers = column(fake, COMPARISON_TAB, "Shopee seller")
    assert "Alpha Store" in sellers and "Beta Store" in sellers
    assert sellers.count("Shop 10") == 9
    ranking_gmv = column(fake, COMPARISON_TAB, "Ranking GMV (USD)")
    assert len(set(ranking_gmv)) >= 3
    # Prices are numeric cells (never text), tags and notes are separate.
    cell = data_cell(fake, COMPARISON_TAB, 1, "Shopee snapshot price (PHP)")
    assert "numberValue" in cell["userEnteredValue"]
    assert cell["userEnteredFormat"]["numberFormat"]["type"] == "CURRENCY"
    # The two unmapped eligible variations are not published: only verified mappings are listed.
    assert "SV-13-31-9901" not in ids and "SV-14-32-9902" not in ids
    statuses = column(fake, COMPARISON_TAB, "Row comparison status")
    assert "CONFIGURATION_REVIEW_PENDING" not in statuses
    assert set(column(fake, TOP_TAB, "mapping_status")) == {"MAPPED_VERIFIED"}
    assert set(column(fake, TOP_TAB, "comparison_status")) == {"COMPARISON_CONTEXT_READY"}
    # Every row keeps its own price.
    top_price = column(fake, TOP_TAB, "shopee_current_price_php")
    assert top_price[ids.index("SV-10-1-101")] == "1001.0"
    assert top_price[ids.index("SV-11-21-102")] == "990.0"


def test_overview_cells_carry_notes_text_labels_and_status_fills(world) -> None:
    code, fake, _ = run(world)
    assert code == 0

    top = header_row(OVERVIEW_TAB)
    lf = header_of(fake, OVERVIEW_TAB).index("Laptop Factory (PHP)")
    priced = [
        n
        for n in range(1, 13)
        if "numberValue" in (data_cell(fake, OVERVIEW_TAB, n, "Laptop Factory (PHP)").get(
            "userEnteredValue") or {})
    ]
    assert priced, "the synthetic report prices at least one Laptop Factory cell"
    n = priced[0]
    cell = data_cell(fake, OVERVIEW_TAB, n, "Laptop Factory (PHP)")
    note = cell["note"]
    assert "MATCH" in note and "Stock:" in note and "Selected offer:" in note
    assert "association" in note
    colour = fake.sheets[OVERVIEW_TAB]["layout"][(top + n, lf)]["backgroundColor"]
    assert all(abs(colour[k] - STATUS_FILLS["match"][k]) < 0.002 for k in ("red", "green", "blue"))
    # The colour has accompanying text: the price stays numeric and its display carries the
    # label, and the comparison's result cell says the same.
    assert cell["userEnteredFormat"]["numberFormat"]["pattern"].endswith('· MATCH"')
    assert column(fake, COMPARISON_TAB, "Laptop Factory result")[n - 1].startswith("MATCH")
    # House layout: banner rows then the frozen header, frozen identifiers, widths, Roboto.
    assert fake.sheets[OVERVIEW_TAB]["frozen"] == 3
    assert fake.sheets[OVERVIEW_TAB]["frozen_columns"] == 4
    assert fake.sheets[COMPARISON_TAB]["frozen"] == 3
    assert fake.sheets[COMPARISON_TAB]["frozen_columns"] == 3
    assert fake.sheets[TOP_TAB]["frozen_columns"] == 2
    assert fake.sheets[COMPARISON_TAB]["filter"]["startRowIndex"] == 2  # banner rows excluded
    assert fake.sheets[COMPARISON_TAB]["widths"]
    header_format = fake.sheets[COMPARISON_TAB]["layout"][(2, 0)]
    assert header_format["textFormat"]["fontFamily"] == "Roboto"


def test_history_and_review_keep_earlier_columns_with_legacy_grain_and_new_associations(
    world,
) -> None:
    code, fake, _ = run(world)
    assert code == 0

    history = fake.values(HISTORY_TAB)
    head = history[0]
    assert head[: len(LEGACY_HISTORY_HEADER)] == list(LEGACY_HISTORY_HEADER)
    assert head[len(LEGACY_HISTORY_HEADER) :] == list(CONTRACT.appended[HISTORY_TAB])
    grains = column(fake, HISTORY_TAB, "Row grain")
    assert set(grains) == {"TARGET_CONFIGURATION", "SHOPEE_VARIATION"}
    legacy_rows = [i for i, g in enumerate(grains) if g == "TARGET_CONFIGURATION"]
    assert legacy_rows
    for i in legacy_rows:  # no source identity is guessed for earlier records
        for name in ("Comparison row ID", "Source key", "Association ID", "Schema version"):
            assert column(fake, HISTORY_TAB, name)[i] == ""
        assert column(fake, HISTORY_TAB, "Rank")[i] == ""  # target ranks are not variation ranks
    association_rows = [i for i, g in enumerate(grains) if g == "SHOPEE_VARIATION"]
    assert association_rows
    for i in association_rows:
        assert column(fake, HISTORY_TAB, "Schema version")[i] == SCHEMA_VERSION
        assert column(fake, HISTORY_TAB, "Source key")[i]
        assert column(fake, HISTORY_TAB, "Association ID")[i].startswith("ASSOC-")
        assert column(fake, HISTORY_TAB, "Difference tag")[i] == "MATCH"
    # The same observation supports several rows without being imported again.
    observations = [column(fake, HISTORY_TAB, "Observation ID")[i] for i in association_rows]
    assert len(observations) > len(set(observations))
    review_grains = column(fake, REVIEW_TAB, "Row grain")
    # No unmapped variation is published, so no row awaits configuration review; the synthetic
    # run has no recheck candidates or source conflicts either.
    reasons = [
        column(fake, REVIEW_TAB, "Review reason")[i]
        for i, g in enumerate(review_grains)
        if g == "SHOPEE_VARIATION"
    ]
    assert "CONFIGURATION_REVIEW_PENDING" not in reasons


def test_run_log_records_schema_grain_and_the_new_metrics_and_keeps_earlier_rows(world) -> None:
    tabs = template_tabs()
    old = [*legacy_run_log_header()]
    tabs[RUN_LOG_TAB] = [old, ["PUB-OLD"] + ["x"] * (len(old) - 1)]  # an earlier-header run log
    code, fake, _ = run(world, fake=SpyingSpreadsheet(tabs))
    assert code == 0

    values = fake.values(RUN_LOG_TAB)
    assert values[0] == [*legacy_run_log_header(), *CONTRACT.appended[RUN_LOG_TAB]]
    assert values[1][0] == "PUB-OLD" and values[1][-1] == ""  # kept; new columns legitimately empty
    new = dict(zip(values[0], values[-1], strict=False))
    assert new["Schema version"] == SCHEMA_VERSION and new["Ranking grain"] == "SHOPEE_VARIATION"
    assert new["Selected Shopee variations"] == "12.0"
    assert new["Distinct configuration contexts"] == "10.0"
    assert new["Mapped selected variations"] == "12.0"
    assert new["Unmapped selected variations"] == "0.0"
    assert new["Publisher version"] == "variation-sheets-3"
    assert new["Publication status"] == "SUCCESS" and new["Readback"] == "PASSED"


def test_republishing_replaces_this_publications_run_log_row_and_rewrites_whole_tabs(world) -> None:
    code, fake, _ = run(world)
    assert code == 0
    first = fake.values(COMPARISON_TAB)
    code, fake, _ = run(world, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))

    assert code == 0 and fake.values(COMPARISON_TAB) == first
    assert len(fake.values(RUN_LOG_TAB)) == 2  # header + one publication
    attempts = column(fake, RUN_LOG_TAB, "Publication attempts")
    assert attempts == ["2.0"]


def test_rollback_restores_the_data_tabs_exactly_as_before_a_publication(world) -> None:
    code, fake, _ = run(world)
    assert code == 0
    # A later manual change, then a second publication that backs it up and overwrites it.
    header = header_of(fake, COMPARISON_TAB)
    brand_row = header_row(COMPARISON_TAB) + 1
    fake.sheets[COMPARISON_TAB]["grid"][(brand_row, header.index("Brand"))] = {
        "userEnteredValue": {"stringValue": "MANUAL EDIT"}
    }
    before = {t: fake.values(t) for t in DATA_TABS}
    groups_before = fake.sheets[COMPARISON_TAB]["groups"]
    code, fake, _ = run(world, fake=fake, now=datetime(2026, 10, 12, 4, tzinfo=UTC))
    assert code == 0 and fake.values(COMPARISON_TAB) != before[COMPARISON_TAB]
    folder = [p for p in sorted(world["out"].iterdir()) if p.name.endswith("_write")][-1]

    load_script("publish_variation_prices").main(
        ["--rollback", str(folder), "--output-root", str(world["out"])],
        spreadsheet_factory=lambda read_only: fake,
        now=datetime(2026, 10, 12, 5, tzinfo=UTC),
    )

    for title in DATA_TABS:
        assert fake.values(title) == before[title]
    assert (
        fake.cell(COMPARISON_TAB, brand_row, header.index("Brand"))["userEnteredValue"][
            "stringValue"
        ]
        == "MANUAL EDIT"
    )
    # Groups and conditional formats are restored as snapshotted, never doubled.
    assert fake.sheets[COMPARISON_TAB]["groups"] == groups_before


def test_a_partial_failure_is_logged_and_the_rerun_repairs_it(world) -> None:
    fake = SpyingSpreadsheet(template_tabs())
    fake.fail_on_batch = 4
    code, fake, _ = run(world, fake=fake)
    assert code == 1 and latest_report(world)["status"] == "PARTIAL_FAILURE"

    fake.fail_on_batch = None
    code, fake, _ = run(world, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))
    assert code == 0 and latest_report(world)["status"] == "SUCCESS"


def test_the_plan_is_reproducible_from_saved_inputs(world, tmp_path) -> None:
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    first = build_variation_plan(report, CONTRACT)
    copied = tmp_path / "copy"
    shutil.copytree(world["reports"], copied)
    second = build_variation_plan(
        load_variation_report(world["variation"], copied, world["context"]), CONTRACT
    )

    assert first.content_sha256 == second.content_sha256


def test_earlier_history_records_keep_their_values_and_dates_under_the_legacy_grain(world) -> None:
    from laptop_tracker.sheet_publication import history_plan

    code, fake, _ = run(world)
    assert code == 0
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    legacy = history_plan(report.legacy)
    published = fake.values(HISTORY_TAB)[1:]
    width = len(LEGACY_HISTORY_HEADER)
    rank = LEGACY_HISTORY_HEADER.index("Rank")
    kept = [row for row in published if row[len(LEGACY_HISTORY_HEADER)] == "TARGET_CONFIGURATION"]

    assert len(kept) == len(legacy.rows)
    for row, original in zip(kept, legacy.rows, strict=True):
        for index in range(width):
            if index == rank:
                assert row[index] == ""  # a target-based rank is not republished as a rank
            elif original[index].kind == "text":
                assert row[index] == original[index].value  # dates, IDs, outcomes: unchanged


def test_contexts_and_associations_leave_the_observation_layer_untouched(world) -> None:
    import laptop_tracker.variation_publication as publication
    import laptop_tracker.variation_ranking as ranking
    import laptop_tracker.variation_reports as reports

    for module in (ranking, reports, publication):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "ObservationStore" not in source and "import_observations" not in source
        assert "requests" not in source.split("import", 1)[0]
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    selections = [s for s in report.selections.values() if s["association_kind"] == "CONTEXT_MATCH"]
    observations = {s["observation_id"] for s in selections}
    # Several rows share one observation; the observation count is the unique count.
    assert len(selections) > len(observations)
    assert report.counts["unique_matching_observations"] == len(observations)
    assert report.counts["row_associations_matching"] == len(selections)


def test_row_cells_equal_their_contexts_reviewed_cells_for_every_copied_column(world) -> None:
    from laptop_tracker.variation_reports import CONTEXT_COPY_COLUMNS, load_context_report

    context = load_context_report(world["context"])
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    shared = 0
    for row in report.comparison:
        if not row["context_id"]:
            continue
        reviewed = context.comparison[row["context_id"]]
        shared += 1
        for name in CONTEXT_COPY_COLUMNS:  # prices, status, stock, both minima, tags, rechecks
            assert row[name] == reviewed[name], name
    assert shared == 12


def test_the_run_log_header_is_read_from_the_live_tab_and_rows_are_filled_by_name(world) -> None:
    # The live workbook's run log has the pilot-sheets-6 columns plus the contract's appended ones
    # (no pilot-sheets-7 alternative counts); earlier rows are kept under their own columns.
    from laptop_tracker.sheet_publication import RUN_LOG_HEADER_PILOT_SHEETS_6

    live_header = [*RUN_LOG_HEADER_PILOT_SHEETS_6, *CONTRACT.appended[RUN_LOG_TAB]]
    earlier = dict.fromkeys(live_header, "") | {
        "Publication ID": "PUB-OLD",
        "Publisher version": "pilot-sheets-6",
        "Overview rows": "7",
    }
    tabs = template_tabs()
    tabs[RUN_LOG_TAB] = [live_header, [earlier[name] for name in live_header]]
    code, fake, _ = run(world, fake=SpyingSpreadsheet(tabs))
    assert code == 0

    values = fake.values(RUN_LOG_TAB)
    assert values[0] == live_header  # the live header is kept exactly
    old, new = (dict(zip(values[0], row, strict=False)) for row in values[1:])
    assert old["Publication ID"] == "PUB-OLD" and old["Publisher version"] == "pilot-sheets-6"
    assert old["Overview rows"] in {"7", "7.0"}
    assert old["Schema version"] == ""  # legitimately empty for an earlier publication
    assert new["Schema version"] == SCHEMA_VERSION and new["Matching-priced variation rows"]
    assert "Alternative prices shown" not in new  # a column the tab lacks is not invented


def test_an_unrecognised_run_log_header_is_refused(world, capsys) -> None:
    tabs = template_tabs()
    tabs[RUN_LOG_TAB] = [["Publication ID", "Something else"], ["PUB-X", "y"]]
    code, fake, _ = run(world, fake=SpyingSpreadsheet(tabs))

    assert code == 1 and fake.mutations == []
    assert "PILOT_PRICE_RUN_LOG has content that this publisher did not write" in (
        capsys.readouterr().err
    )


def _live_history(rows: list[dict[str, str]]) -> list[list[str]]:
    header = [*LEGACY_HISTORY_HEADER, *CONTRACT.appended[HISTORY_TAB]]
    return [header, *([row.get(name, "") for name in header] for row in rows)]


def test_earlier_live_history_outside_this_scope_is_retained_not_dropped(world, capsys) -> None:
    older = {
        "Week start": "2026-10-19",
        "Retrieved (Asia/Singapore)": "2026-10-22T10:00:00+08:00",
        "Retrieved (UTC)": "2026-10-22T02:00:00.000000Z",
        "Rank": "41",
        "Target ID": "TGT-000999",
        "Retailer": "DataBlitz",
        "Outcome": "Matched, comparable",
        "Observation ID": "obs-old",
    }
    stale_recheck = {"Category": "Recheck candidate (unconfirmed)", "Observation ID": "obs-stale"}
    tabs = template_tabs()
    tabs[HISTORY_TAB] = _live_history([older])
    tabs[REVIEW_TAB] = [
        [*LEGACY_REVIEW_HEADER, *CONTRACT.appended[REVIEW_TAB]],
        [stale_recheck.get(n, "") for n in [*LEGACY_REVIEW_HEADER, *CONTRACT.appended[REVIEW_TAB]]],
        [
            {
                "Category": "Matching problem",
                "Review reason": "old",
                "Observation ID": "obs-keep",
            }.get(n, "")
            for n in [*LEGACY_REVIEW_HEADER, *CONTRACT.appended[REVIEW_TAB]]
        ],
    ]
    code, fake, _ = run(world, mode="--preview", fake=SpyingSpreadsheet(tabs))
    assert code == 0 and "1 earlier live records retained" in capsys.readouterr().out
    assert latest_report(world)["retained_earlier_records"] == {
        HISTORY_TAB: 1,
        REVIEW_TAB: 1,
    }  # the stale target-grain recheck is not kept

    code, fake, _ = run(
        world, fake=SpyingSpreadsheet(tabs), now=datetime(2026, 10, 12, 3, tzinfo=UTC)
    )
    assert code == 0

    ids = column(fake, HISTORY_TAB, "Observation ID")
    assert ids[0] == "obs-old"  # retained first, exactly as it was
    assert column(fake, HISTORY_TAB, "Rank")[0] in {"41", "41.0"}  # its own, old rank
    assert column(fake, HISTORY_TAB, "Target ID")[0] == "TGT-000999"
    assert column(fake, HISTORY_TAB, "Row grain")[0] == "TARGET_CONFIGURATION"  # explicit label
    assert column(fake, HISTORY_TAB, "Comparison row ID")[0] == ""  # nothing is guessed
    assert column(fake, HISTORY_TAB, "Schema version")[0] == ""
    assert "obs-keep" in column(fake, REVIEW_TAB, "Observation ID")
    assert "obs-stale" not in column(fake, REVIEW_TAB, "Observation ID")


def test_republishing_never_duplicates_retained_or_regenerated_records(world) -> None:
    code, fake, _ = run(world)
    assert code == 0
    first = {t: fake.values(t) for t in (HISTORY_TAB, REVIEW_TAB, COMPARISON_TAB)}
    code, fake, _ = run(world, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))

    assert code == 0
    for title, values in first.items():
        assert fake.values(title) == values


def test_a_gmv_cell_for_a_month_without_a_record_says_the_zero_is_arithmetic() -> None:
    from laptop_tracker.variation_publication import _missing_month_note

    row = {"m_minus_1_status": "PRESENT", "mtd_status": "NO_RECORD_TREATED_AS_ZERO",
           "m_minus_1_month": "2026-10", "mtd_month": "2026-11"}

    assert _missing_month_note("m_minus_1_gmv_usd", row) == ""
    note = _missing_month_note("mtd_gmv_usd", row)
    assert "No 2026-11 record" in note and "not an observed zero" in note
    assert _missing_month_note("ranking_gmv_usd", row) == ""
