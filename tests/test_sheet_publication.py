"""Manual pilot Sheets publisher, tested offline against an in-memory fake of the Sheets API."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_observation_store import NOW, T0, offer
from test_price_tracking import AS_OF, TM_SHA, WEEK, target
from test_target_master import target_values
from test_verified_top import load_script

from laptop_tracker.match_run import matcher_from_validated
from laptop_tracker.observation_store import ObservationStore, observation_id
from laptop_tracker.offers import Availability
from laptop_tracker.price_tracking import (
    COMPARISON_COLUMNS,
    HISTORY_COLUMNS,
    REVIEW_COLUMNS,
    SELECTION_COLUMNS,
    SOURCE_LEDGER_COLUMNS,
    build_price_tracking,
    match_stored_observations,
    matching_context,
    validate_selected_list,
)
from laptop_tracker.sheet_publication import (
    COMPARISON_TAB,
    HISTORY_TAB,
    PILOT_SHEETS_1_COMPARISON_HEADER,
    PUBLISHER_TABS,
    REVIEW_TAB,
    RUN_LOG_TAB,
    Cell,
    DestinationError,
    TabState,
    _cell_data,
    assert_publisher_tab,
    guard_requests,
    text,
    verify_tab,
)
from laptop_tracker.shopee_snapshot import select_shopee_prices
from laptop_tracker.target_master import validate_target_master
from laptop_tracker.verified_top import CONFIGURATION_COLUMNS, LEDGER_COLUMNS, money

pytestmark = pytest.mark.usefixtures("no_network")

TARGETS = [
    target(f"TGT-0000{i:02d}", f"EX{i:02d}-0001", "14", "Core 5", "16", str(256 * i))
    for i in range(1, 11)
]
SCOPE = tuple(f"TGT-0000{i:02d}" for i in range(1, 8))
PCC_URL = "https://pccentralcomputerstore.com/products/example-01?variant=777"
CANDIDATE_SLUG = "ex04-candidate"


# Fake Google Sheets


class FakeWorksheet:
    def __init__(self, sheet: dict) -> None:
        self.title = sheet["title"]
        self.id = sheet["id"]
        self.row_count = sheet["rows"]
        self.col_count = sheet["cols"]


class FakeSpreadsheet:
    """Implements the four calls the publisher uses, with Sheets ``updateCells`` semantics."""

    id = "fake-spreadsheet-id"
    title = "Fake Tracker"

    def __init__(self, tabs: dict[str, list[list[str]]] | None = None) -> None:
        self.sheets: dict[str, dict] = {}
        self.mutations: list[str] = []
        self.read_only = False
        self.fail_on_batch: int | None = None
        self.after_batch = None
        self.batches = 0
        for title, values in (tabs or {}).items():
            sheet = self._new(title, 1000, 26)
            for r, row in enumerate(values):
                for c, value in enumerate(row):
                    sheet["grid"][(r, c)] = {"userEnteredValue": {"stringValue": value}}

    def _new(self, title: str, rows: int, cols: int) -> dict:
        # IDs are never reused, as in Sheets (a deleted tab does not free its ID).
        next_id = max((s["id"] for s in self.sheets.values()), default=999) + 1
        sheet = {"title": title, "id": next_id, "rows": rows, "cols": cols,
                 "grid": {}, "frozen": 0, "frozen_columns": 0, "filter": None, "widths": {},
                 "heights": {}, "layout": {}, "gridlines_hidden": False,
                 # column groups ({range, depth, collapsed}), conditional-format rules, merged
                 # ranges and hidden columns (a collapsed group hides its columns)
                 "groups": [], "cf": [], "merges": [], "hidden": set()}
        self.sheets[title] = sheet
        return sheet

    def worksheets(self):
        return [FakeWorksheet(sheet) for sheet in self.sheets.values()]

    def add_worksheet(self, title, rows, cols):
        if self.read_only:
            raise PermissionError("read-only scope")
        self.mutations.append(f"add_worksheet:{title}")
        return FakeWorksheet(self._new(title, rows, cols))

    def batch_update(self, body):
        if self.read_only:
            raise PermissionError("read-only scope")
        self.batches += 1
        self.mutations.append(f"batch_update:{self.batches}")
        if self.fail_on_batch == self.batches:
            raise RuntimeError("simulated API failure")
        by_id = {sheet["id"]: sheet for sheet in self.sheets.values()}
        for request in body["requests"]:
            (kind, payload), = request.items()
            if kind == "updateSheetProperties":
                props = payload["properties"]
                grid = props.get("gridProperties", {})
                sheet = by_id[props["sheetId"]]
                sheet["rows"] = grid.get("rowCount", sheet["rows"])
                sheet["cols"] = grid.get("columnCount", sheet["cols"])
                sheet["frozen"] = grid.get("frozenRowCount", sheet["frozen"])
                sheet["frozen_columns"] = grid.get("frozenColumnCount", sheet["frozen_columns"])
                sheet["gridlines_hidden"] = grid.get("hideGridlines", sheet["gridlines_hidden"])
            elif kind == "setBasicFilter":
                rng = payload["filter"]["range"]
                by_id[rng["sheetId"]]["filter"] = rng
            elif kind == "clearBasicFilter":
                by_id[payload["sheetId"]]["filter"] = None
            elif kind == "updateCells" and "range" in payload:
                fields = payload["fields"]
                for cell in by_id[payload["range"]["sheetId"]]["grid"].values():
                    cell.pop("userEnteredValue", None)
                    cell.pop("hyperlink", None)
                    if "userEnteredFormat" in fields.split(","):
                        cell.pop("userEnteredFormat", None)  # the whole format is cleared
                    fmt = cell.get("userEnteredFormat", {})
                    fmt.pop("numberFormat", None)
                    fmt.get("textFormat", {}).pop("link", None)
                    fmt.get("textFormat", {}).pop("bold", None)
                    if "italic" in fields:
                        fmt.get("textFormat", {}).pop("italic", None)
                    if "note" in fields:
                        cell.pop("note", None)
            elif kind == "updateCells":
                start = payload["start"]
                fields = payload["fields"]
                grid = by_id[start["sheetId"]]["grid"]
                for r, row in enumerate(payload["rows"]):
                    for c, value in enumerate(row["values"]):
                        cell = grid.setdefault((start["rowIndex"] + r, start["columnIndex"] + c),
                                               {})
                        cell.pop("userEnteredValue", None)
                        cell.pop("hyperlink", None)
                        if "userEnteredValue" in value:
                            cell["userEnteredValue"] = dict(value["userEnteredValue"])
                        fmt = value.get("userEnteredFormat", {})
                        # A format is replaced only when the field mask names one (a partial
                        # update, such as a guide row's text, leaves the owner's format alone).
                        if "userEnteredFormat" in fields:
                            cell["userEnteredFormat"] = copy.deepcopy(fmt)
                        link = fmt.get("textFormat", {}).get("link", {}).get("uri")
                        if link:
                            cell["hyperlink"] = link
                        # A note is set or cleared only when the field mask names it.
                        if "note" in fields:
                            cell.pop("note", None)
                            if value.get("note"):
                                cell["note"] = value["note"]
                        elif "note" in value:
                            raise AssertionError("a note outside the field mask")
            elif kind == "repeatCell":
                # Layout only (backgrounds, alignment, wrapping): kept apart from cell content.
                rng = payload["range"]
                layout = by_id[rng["sheetId"]]["layout"]
                fmt = payload["cell"].get("userEnteredFormat", {})
                if "startRowIndex" not in rng:
                    layout.clear()
                for r in range(rng.get("startRowIndex", 0), rng.get("endRowIndex", 0)):
                    for c in range(rng.get("startColumnIndex", 0), rng.get("endColumnIndex", 0)):
                        layout.setdefault((r, c), {}).update(copy.deepcopy(fmt))
            elif kind == "updateDimensionProperties":
                rng = payload["range"]
                sheet = by_id[rng["sheetId"]]
                sizes = sheet["widths" if rng["dimension"] == "COLUMNS" else "heights"]
                assert rng["dimension"] in {"COLUMNS", "ROWS"}
                for c in range(rng["startIndex"], rng["endIndex"]):
                    if "pixelSize" in payload["properties"]:
                        sizes[c] = payload["properties"]["pixelSize"]
                    if "hiddenByUser" in payload["properties"]:
                        (sheet["hidden"].add if payload["properties"]["hiddenByUser"]
                         else sheet["hidden"].discard)(c)
            elif kind in {"addDimensionGroup", "deleteDimensionGroup"}:
                rng = payload["range"]
                sheet = by_id[rng["sheetId"]]
                span = (rng["startIndex"], rng["endIndex"])
                same = [g for g in sheet["groups"]
                        if (g["range"]["startIndex"], g["range"]["endIndex"]) == span]
                if kind == "addDimensionGroup":
                    inside = [g for g in sheet["groups"]
                              if g["range"]["startIndex"] <= span[0]
                              and g["range"]["endIndex"] >= span[1]]
                    sheet["groups"].append({"range": dict(rng), "depth": len(inside) + 1,
                                            "collapsed": False})
                else:
                    if not same:
                        raise AssertionError("no such column group to delete")
                    sheet["groups"].remove(max(same, key=lambda g: g["depth"]))
            elif kind == "updateDimensionGroup":
                group = payload["dimensionGroup"]
                sheet = by_id[group["range"]["sheetId"]]
                span = (group["range"]["startIndex"], group["range"]["endIndex"])
                found = next(g for g in sheet["groups"]
                             if (g["range"]["startIndex"], g["range"]["endIndex"]) == span
                             and g["depth"] == group["depth"])
                found["collapsed"] = group["collapsed"]
                for c in range(*span):
                    (sheet["hidden"].add if group["collapsed"] else sheet["hidden"].discard)(c)
            elif kind == "deleteConditionalFormatRule":
                del by_id[payload["sheetId"]]["cf"][payload["index"]]
            elif kind == "addConditionalFormatRule":
                by_id[payload["rule"]["ranges"][0]["sheetId"]]["cf"].insert(
                    payload["index"], copy.deepcopy(payload["rule"]))
            elif kind in {"mergeCells", "unmergeCells"}:
                rng = payload["range"]
                merges = by_id[rng["sheetId"]]["merges"]
                if kind == "mergeCells":
                    merges.append(dict(rng))
                else:
                    merges[:] = [m for m in merges if "startRowIndex" in rng
                                 and m["startRowIndex"] != rng["startRowIndex"]]
            else:
                raise AssertionError(f"unexpected request {kind}")
        if self.after_batch:
            self.after_batch(self)

    def fetch_sheet_metadata(self, params=None):
        title = params["ranges"].strip("'")
        sheet = self.sheets[title]
        cells = sheet["grid"]
        used = [key for key, cell in cells.items() if cell]
        height = max((r for r, _ in used), default=-1) + 1
        width = max((c for _, c in used), default=-1) + 1
        layout = sheet["layout"]

        def read(r: int, c: int) -> dict:
            # Cell content as stored, with the background a layout request painted over it (the
            # real API reads both from ``userEnteredFormat``).
            cell = copy.deepcopy(cells.get((r, c), {}))
            colour = layout.get((r, c), {}).get("backgroundColor")
            if colour:
                cell.setdefault("userEnteredFormat", {})["backgroundColor"] = dict(colour)
            return cell

        rows = [
            {"values": [read(r, c) for c in range(width)]}
            for r in range(height)
        ]
        columns = max([width, *(c + 1 for c in sheet["widths"])])
        return {
            "sheets": [
                {
                    "properties": {
                        "sheetId": sheet["id"],
                        "title": title,
                        "gridProperties": {"rowCount": sheet["rows"],
                                           "columnCount": sheet["cols"],
                                           "frozenRowCount": sheet["frozen"],
                                           "frozenColumnCount": sheet["frozen_columns"],
                                           "hideGridlines": sheet["gridlines_hidden"]},
                    },
                    **({"basicFilter": {"range": sheet["filter"]}} if sheet["filter"] else {}),
                    "columnGroups": copy.deepcopy(sheet["groups"]),
                    "conditionalFormats": copy.deepcopy(sheet["cf"]),
                    "merges": copy.deepcopy(sheet["merges"]),
                    "data": [{
                        "rowData": rows,
                        "columnMetadata": [
                            {"pixelSize": sheet["widths"].get(c, 100),
                             **({"hiddenByUser": True} if c in sheet["hidden"] else {})}
                            for c in range(columns)],
                        "rowMetadata": [{"pixelSize": sheet["heights"].get(r, 21)}
                                        for r in range(height)],
                    }],
                }
            ]
        }

    def values(self, title: str) -> list[list[str]]:
        state = self.fetch_sheet_metadata({"ranges": f"'{title}'"})["sheets"][0]
        out = []
        for row in state["data"][0]["rowData"]:
            out.append([
                (v.get("userEnteredValue") or {}).get("stringValue",
                                                      repr((v.get("userEnteredValue") or {})
                                                           .get("numberValue", "")))
                if v.get("userEnteredValue") else ""
                for v in row["values"]
            ])
        return out

    def cell(self, title: str, row: int, column: int) -> dict:
        return self.sheets[title]["grid"].get((row, column), {})


# Synthetic reviewed reports (built with the real Phase 9 pipeline)


def write_dicts(path: Path, columns, rows) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)
    return path


def ledger_rows() -> list[dict[str, str]]:
    return [
        dict.fromkeys(LEDGER_COLUMNS, "")
        | {"model_id": f"{i}01", "target_id": f"TGT-0000{i:02d}", "m_minus_1_gmv_usd": "10",
           "mtd_gmv_usd": "1", "ranking_gmv_usd": "11", "contribution_status": "CONTRIBUTING",
           "in_original_source_top_100": "True"}
        for i in range(1, 11)
    ]


def list_rows() -> list[dict[str, str]]:
    header = target_values(*TARGETS)[0]
    rows = []
    for rank, values in enumerate(TARGETS, start=1):
        record = dict(zip(header, values, strict=True))
        rows.append(
            dict.fromkeys(CONFIGURATION_COLUMNS, "")
            | {name: record[name] for name in ("target_id", "brand", "series", "model",
                                               "screen_size_in", "cpu_series",
                                               "ram_capacity_gb", "storage_capacity_gb")}
            | {"rank": str(rank), "m_minus_1_gmv_usd": "10", "mtd_gmv_usd": "1",
               "ranking_gmv_usd": money(Decimal("11")), "contributing_source_count": "1",
               "contributing_source_model_ids": f"{rank}01",
               "sales_run_id": "20260928T112328+0800", "sales_extract_date": "2026-09-28",
               "sales_m_minus_1_month": "2026-08", "sales_mtd_month": "2026-09",
               "target_master_sha256": TM_SHA}
        )
    return rows


def obs(index: int, *, retailer="laptop_factory", url=None, **kwargs):
    observation = offer(f"EX{index:02d}-0001", retailer=retailer, slug=f"ex{index:02d}",
                        specs=(), **kwargs)
    if url:
        observation = replace(
            observation,
            offer_url=url,
            source=replace(observation.source, requested_url=url, final_url=url),
        )
    return observation


def build_reports(
    base: Path, *, shopee_ledger: list[dict[str, str]] | None = None
) -> dict[str, Path]:
    """Ten selected targets; reports for week 2026-10-05; pilot scope is the first seven. With
    ``shopee_ledger`` (configuration_source_ledger.csv rows) the report carries Shopee snapshot
    prices selected from it, and the ledger is written beside the report."""
    exact = {
        obs(1, price="1000.00"): "TGT-000001",
        obs(1, retailer="pcc", url=PCC_URL, price="990.50"): "TGT-000001",
        obs(2, price="1500.00", availability=Availability.OUT_OF_STOCK): "TGT-000002",
        obs(3, price="2000.00"): "TGT-000003",
        obs(3, price="2100.00", at=T0 - timedelta(days=7)): "TGT-000003",
        obs(8, price="800.00"): "TGT-000008",
    }
    candidate = offer("EX04-0001", slug=CANDIDATE_SLUG, specs=(), price="10.00")
    inputs = base / "inputs"
    inputs.mkdir(parents=True)
    selected_path = write_dicts(inputs / "top.csv", CONFIGURATION_COLUMNS, list_rows())
    list_sha = hashlib.sha256(selected_path.read_bytes()).hexdigest()
    tm = validate_target_master(target_values(*TARGETS))
    selected = validate_selected_list(list_rows(), ledger_rows(), tm,
                                      target_master_sha256=TM_SHA, list_sha256=list_sha)
    with ObservationStore.open(base / "store.sqlite3") as store:
        store.import_observations([*exact, candidate], source_label="t", imported_at=NOW)
        context = matching_context(tm, target_master_sha256=TM_SHA, source_label="t")
        match_stored_observations(store, context, matcher_from_validated(tm), now=NOW)
        stored = store.observations()
        matches = store.match_results(context.context_id)
    for observation, target_id in exact.items():
        key = observation_id(observation)
        matches[key] = replace(matches[key], match_status="EXACT", target_id=target_id,
                               candidate_target_ids=(target_id,), reason_codes=())
    key = observation_id(candidate)
    matches[key] = replace(matches[key], match_status="REVIEW_REQUIRED", target_id="",
                           candidate_target_ids=("TGT-000004",),
                           reason_codes=("SECONDARY_CONFLICT",))
    shopee = None
    if shopee_ledger is not None:
        shopee, shopee_ledger = select_shopee_prices(shopee_ledger)
    result = build_price_tracking(stored, matches, selected, context, week=WEEK, as_of=AS_OF,
                                  shopee=shopee)

    report = base / "cycle" / "report"
    report.mkdir(parents=True)
    if shopee_ledger is not None:
        write_dicts(report / "configuration_source_ledger.csv", SOURCE_LEDGER_COLUMNS,
                    shopee_ledger)
    write_dicts(report / "weekly_price_comparison.csv", COMPARISON_COLUMNS, result.comparison)
    write_dicts(report / "weekly_price_selections.csv", SELECTION_COLUMNS, result.selections)
    write_dicts(report / "price_history.csv", HISTORY_COLUMNS, result.history)
    write_dicts(report / "price_review_queue.csv", REVIEW_COLUMNS, result.review)
    summary = {
        "report_week": {
            "start": "2026-10-05", "end_exclusive": "2026-10-12", "timezone": "Asia/Singapore",
            "start_utc": "2026-10-04T16:00:00.000000Z",
            "end_utc_exclusive": "2026-10-11T16:00:00.000000Z", "status": "CLOSED_HISTORICAL",
        },
        "as_of_utc": "2026-10-12T01:00:00.000000Z",
        "sales_snapshot": {"run_id": "20260928T112328+0800",
                           "label": "M-1 2026-08 (full month) + MTD 2026-09 as extracted "
                                    "2026-09-28; historical Shopee snapshot"},
        "selected_list": {"configurations": 10, "sha256": list_sha,
                          "built_on_target_master_sha256": TM_SHA},
        "matching_context": {"context_id": context.context_id,
                             "target_master_sha256": TM_SHA},
        "input_sha256": {str(selected_path): list_sha},
    }
    (report / "price_tracking_run_summary.json").write_text(json.dumps(summary))

    collection = base / "cycle" / "collection"
    collection.mkdir()
    (collection / "collection_run_summary.json").write_text(json.dumps({
        "mode": "LIVE", "run_status": "SUCCESS",
        "started_at_utc": "2026-10-07T03:59:00.000000Z",
        "finished_at_utc": "2026-10-07T04:01:00.000000Z",
    }))
    current = [o for o, t in exact.items() if o.retrieved_at == T0 and t in SCOPE]
    write_dicts(collection / "match_results.csv",
                ("observation_id", "match_status", "target_id", "expected_target_id"),
                [{"observation_id": observation_id(o), "match_status": "EXACT",
                  "target_id": exact[o], "expected_target_id": exact[o]} for o in current])
    write_dicts(collection / "match_review_queue.csv",
                ("review_reasons", "observation_id", "target_id", "candidate_target_ids",
                 "expected_target_id"), [])
    scope = write_dicts(base / "scope.csv", CONFIGURATION_COLUMNS,
                        [r for r in list_rows() if r["target_id"] in SCOPE])
    return {"report": report, "scope": scope, "selected": selected_path,
            "store": base / "store.sqlite3", "out": base / "publications"}


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    return build_reports(tmp_path)


def run(world, mode="--write-sheets", fake=None, now=None, extra=()):
    fake = fake if fake is not None else FakeSpreadsheet()
    opened = []

    def factory(read_only: bool):
        opened.append(read_only)
        fake.read_only = read_only
        return fake

    code = load_script("publish_prices").main(
        [mode, "--report-dir", str(world["report"]), "--pilot-scope", str(world["scope"]),
         "--selected-list", str(world["selected"]), "--output-root", str(world["out"]),
         *extra],
        spreadsheet_factory=factory,
        now=now or datetime(2026, 10, 12, 2, tzinfo=UTC),
    )
    return code, fake, opened


def latest_report(world) -> dict:
    folder = sorted(world["out"].iterdir())[-1]
    return json.loads((folder / "publication_report.json").read_text())


def column(fake: FakeSpreadsheet, title: str, name: str) -> list[str]:
    values = fake.values(title)
    index = values[0].index(name)
    return [row[index] if index < len(row) else "" for row in values[1:]]


def row_for(fake: FakeSpreadsheet, title: str, target_id: str) -> dict[str, str]:
    values = fake.values(title)
    for row in values[1:]:
        padded = row + [""] * (len(values[0]) - len(row))
        record = dict(zip(values[0], padded, strict=True))
        if record.get("Target ID") == target_id:
            return record
    raise AssertionError(target_id)


# Preview and scope


def test_preview_validates_and_makes_no_sheet_mutation(world, capsys) -> None:
    code, fake, opened = run(world, mode="--preview")

    assert code == 0
    assert opened == [True]  # read-only scope
    assert fake.mutations == [] and fake.sheets == {}
    printed = capsys.readouterr().out
    assert "2026-10-05" in printed and "2026-10-12T01:00:00.000000Z" in printed
    for title in PUBLISHER_TABS:
        assert f"{title}: new tab" in printed
    assert "1 header + 7 rows" in printed
    assert latest_report(world)["status"] == "PREVIEW_ONLY"


def test_publication_covers_only_the_seven_scoped_configurations(world) -> None:
    code, fake, opened = run(world)

    assert code == 0 and opened == [False]
    assert column(fake, COMPARISON_TAB, "Target ID") == list(SCOPE)
    assert column(fake, COMPARISON_TAB, "Rank") == [repr(float(i)) for i in range(1, 8)]
    # TGT-000008 is selected but outside the pilot scope: no comparison or history row.
    assert "TGT-000008" not in json.dumps(fake.values(HISTORY_TAB))
    history_ids = column(fake, HISTORY_TAB, "Target ID")
    assert sorted(filter(None, history_ids)) == [
        "TGT-000001", "TGT-000001", "TGT-000002", "TGT-000003", "TGT-000003"
    ]
    assert column(fake, HISTORY_TAB, "Candidate target(s)").count("TGT-000004") == 1
    report = latest_report(world)
    assert report["status"] == "SUCCESS"
    assert report["counts"]["pilot_configurations"] == 7
    assert set(report["tabs_written"]) == set(PUBLISHER_TABS)
    assert fake.sheets[COMPARISON_TAB]["frozen"] == 1
    assert fake.sheets[COMPARISON_TAB]["filter"]["endRowIndex"] == 8


# Input validation before any Sheets access


def _rewrite(path: Path, change) -> None:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows, columns = list(reader), reader.fieldnames
    rows = change(rows)
    write_dicts(path, columns, rows)


def _set(rows, which, **values):
    return [r | values if r["target_id"] == which else r for r in rows]


@pytest.mark.parametrize(
    ("file", "change"),
    [
        ("scope", lambda rows: rows + rows[:1]),
        ("scope", lambda rows: _set(rows, "TGT-000001", target_id="TGT-000099")),
        ("scope", lambda rows: _set(rows, "TGT-000001", rank="5")),
        ("scope", lambda rows: _set(rows, "TGT-000001", ram_capacity_gb="8")),
        ("scope", lambda rows: []),
        ("comparison", lambda rows: _set(rows, "TGT-000001", LF_Price="999.00")),
        ("comparison", lambda rows: _set(rows, "TGT-000002", LF_Price="0")),
        ("comparison", lambda rows: _set(
            rows, "TGT-000004",
            LF_Offer_URL=f"https://laptopfactory.com.ph/product/{CANDIDATE_SLUG}/")),
        ("comparison", lambda rows: _set(rows, "TGT-000005",
                                         LF_Offer_URL="https://laptopfactory.com.ph/product/x/")),
        ("comparison", lambda rows: _set(rows, "TGT-000001", Lowest_Price="1000.00")),
        ("comparison", lambda rows: _set(rows, "TGT-000003", report_week_start="2026-09-28")),
        ("comparison", lambda rows: rows[:-1]),
    ],
)
def test_inconsistent_inputs_fail_before_sheets_access(world, file, change, capsys) -> None:
    path = world["scope"] if file == "scope" else world["report"] / "weekly_price_comparison.csv"
    _rewrite(path, change)

    code, fake, opened = run(world)

    assert code == 1
    assert opened == [] and fake.mutations == []
    assert "before any Sheets access" in capsys.readouterr().err


def test_a_different_selected_list_or_missing_report_is_refused(world) -> None:
    world["selected"].write_text(world["selected"].read_text() + "\n")
    assert run(world)[0] == 1
    (world["report"] / "price_history.csv").unlink()
    code, fake, opened = run(world)
    assert (code, opened) == (1, [])


# Destination safety


@pytest.mark.parametrize(
    "title",
    ["raw", "TARGET_MASTER", "SOURCE_MODEL_MAP", "MATCH_REVIEW", "P1_TOP_100_CONFIGURATIONS",
     "P1_TOP_100_TARGETS", "pilot_price_comparison", " PILOT_PRICE_COMPARISON"],
)
def test_only_the_five_publisher_tabs_are_allowed(title: str) -> None:
    with pytest.raises(DestinationError):
        assert_publisher_tab(title)


def test_requests_for_other_sheets_or_operations_are_refused() -> None:
    allowed = {11, 12}
    guard_requests([{"updateCells": {"range": {"sheetId": 11}, "fields": "*"}}], allowed)
    for request in (
        {"updateCells": {"range": {"sheetId": 99}, "fields": "*"}},
        {"deleteSheet": {"sheetId": 11}},
        {"updateCells": {"range": {}, "fields": "*"}},
    ):
        with pytest.raises(DestinationError):
            guard_requests([request], allowed)


def test_other_tabs_are_never_touched(world) -> None:
    protected = {
        "raw": [["grass_date"], ["2026-09-01"]],
        "TARGET_MASTER": [["target_id"], ["TGT-000001"]],
        "P1_TOP_100_CONFIGURATIONS": [["rank"], ["1"]],
    }
    fake = FakeSpreadsheet(protected)
    before = {t: copy.deepcopy(fake.sheets[t]) for t in protected}

    assert run(world, fake=fake)[0] == 0
    for title in protected:
        assert fake.sheets[title] == before[title]


def test_a_destination_tab_with_foreign_content_is_refused(world, capsys) -> None:
    fake = FakeSpreadsheet({COMPARISON_TAB: [["Something else"], ["manual note"]]})

    code, fake, _ = run(world, fake=fake)

    assert code == 1
    assert fake.mutations == []
    assert latest_report(world)["status"] == "DESTINATION_REFUSED"
    assert "did not write" in capsys.readouterr().err


# Cell content


def test_blank_prices_stay_blank_with_clear_statuses(world) -> None:
    code, fake, _ = run(world)
    assert code == 0
    out_of_stock = row_for(fake, COMPARISON_TAB, "TGT-000002")
    assert out_of_stock["Laptop Factory price (PHP)"] == "1500.0"
    assert out_of_stock["Laptop Factory status"] == "Comparable price (OUT OF STOCK)"
    # The inclusive minimum is labeled "observed"; with no in-stock offer the in-stock minimum
    # is blank, never the out-of-stock price.
    assert out_of_stock["Lowest retailer price (PHP)"] == "1500.0"
    assert out_of_stock["Lowest in-stock retailer price (PHP)"] == ""
    assert out_of_stock["Lowest in-stock retailer(s)"] == ""
    assert out_of_stock["Laptop Factory observed (Asia/Singapore)"].startswith("20")
    unobserved = row_for(fake, COMPARISON_TAB, "TGT-000005")
    assert unobserved["Laptop Factory status"] == "No confirmed matched offer this week"
    assert unobserved["Laptop Factory product link"] == ""
    review_only = row_for(fake, COMPARISON_TAB, "TGT-000004")
    assert review_only["Laptop Factory status"] == "Needs match review (no price)"
    # A blank price cell has no value at all, so it can never read as zero.
    values = fake.values(COMPARISON_TAB)
    price_col = values[0].index("Laptop Factory price (PHP)")
    assert fake.cell(COMPARISON_TAB, 5, price_col) == {} or not fake.cell(
        COMPARISON_TAB, 5, price_col
    ).get("userEnteredValue")
    # The earlier week's TGT-000003 price is history only, never this week's comparison.
    assert row_for(fake, COMPARISON_TAB, "TGT-000003")["Laptop Factory price (PHP)"] == "2000.0"


def test_prices_are_php_numbers_and_ids_are_text(world) -> None:
    _, fake, _ = run(world)
    values = fake.values(COMPARISON_TAB)
    pcc = values[0].index("PC Central price (PHP)")
    cell = fake.cell(COMPARISON_TAB, 1, pcc)
    assert cell["userEnteredValue"] == {"numberValue": 990.5}
    assert cell["userEnteredFormat"]["numberFormat"]["type"] == "CURRENCY"
    target_cell = fake.cell(COMPARISON_TAB, 1, values[0].index("Target ID"))
    assert target_cell["userEnteredValue"] == {"stringValue": "TGT-000001"}
    assert row_for(fake, COMPARISON_TAB, "TGT-000001")["Lowest retailer price retailer(s)"] == (
        "PC Central"
    )


def test_a_tab_with_the_previous_publisher_header_is_still_owned(world) -> None:
    previous = [list(PILOT_SHEETS_1_COMPARISON_HEADER), ["1", "TGT-000001"]]
    code, fake, _ = run(world, fake=FakeSpreadsheet({COMPARISON_TAB: previous}))

    assert code == 0
    header = fake.values(COMPARISON_TAB)[0]
    assert "Lowest retailer price (PHP)" in header
    # pilot-sheets-4 (2026-10-20): the matching basis of each minimum and of the comparison;
    # pilot-sheets-5: the minimums are labelled as retailer minimums.
    assert header[header.index("Lowest retailer price retailer(s)") + 1] == (
        "Lowest retailer price basis")
    assert "Lowest observed price (PHP)" not in header
    assert "Comparison basis" in header


def test_text_is_never_sent_as_a_formula() -> None:
    assert _cell_data(text("=IMPORTXML(\"x\")")) == {
        "userEnteredValue": {"stringValue": "=IMPORTXML(\"x\")"}
    }


def test_links_are_exact_offers_and_candidates_are_never_confirmed_links(world) -> None:
    _, fake, _ = run(world)
    values = fake.values(COMPARISON_TAB)
    pcc_link = values[0].index("PC Central product link")
    cell = fake.cell(COMPARISON_TAB, 1, pcc_link)
    assert cell["userEnteredValue"] == {"stringValue": PCC_URL}  # variant query preserved
    assert cell["hyperlink"] == PCC_URL
    lf_link = values[0].index("Laptop Factory product link")
    out_of_stock = fake.cell(COMPARISON_TAB, 2, lf_link)
    assert out_of_stock["hyperlink"] == "https://laptopfactory.com.ph/product/ex02/"
    # 2026-10-19 (second review): an unconfirmed page appears in the comparison only in the
    # labelled recheck columns beside a blank price, never as a product link or a price.
    header = fake.values(COMPARISON_TAB)[0]
    for row in fake.values(COMPARISON_TAB)[1:]:
        for name, value in zip(header, row, strict=False):
            if CANDIDATE_SLUG in str(value):
                assert name.endswith("recheck link (unconfirmed)") or name.endswith(
                    "recheck note"), name
    # The candidate observation appears in history and review only as an unconfirmed page.
    for title in (HISTORY_TAB, REVIEW_TAB):
        header = fake.values(title)[0]
        rows = [r for r in fake.values(title)[1:] if any(CANDIDATE_SLUG in v for v in r)]
        assert len(rows) == 1
        record = dict(zip(header, rows[0] + [""] * (len(header) - len(rows[0])), strict=True))
        assert record["Target ID"] == "" and record["Candidate target(s)"] == "TGT-000004"
        assert record["Product link"] == ""
        assert CANDIDATE_SLUG in record["Observed page (unconfirmed match)"]
        unconfirmed = header.index("Observed page (unconfirmed match)")
        r = fake.values(title).index(rows[0])
        assert "hyperlink" not in fake.cell(title, r, unconfirmed)


def test_review_retains_matching_problems_and_stock_is_comparable(world) -> None:
    _, fake, _ = run(world)
    categories = sorted(column(fake, REVIEW_TAB, "Category"))
    assert categories == ["Matching problem"]
    outcomes = column(fake, HISTORY_TAB, "Outcome")
    assert "Matched, comparable (OUT OF STOCK)" in outcomes
    assert "Matching problem: REVIEW_REQUIRED" in outcomes


def test_run_log_records_times_counts_labels_and_status(world) -> None:
    _, fake, _ = run(world)
    header, row = fake.values(RUN_LOG_TAB)
    record = dict(zip(header, row, strict=True))
    assert record["Publication status"] == "SUCCESS" and record["Readback"] == "PASSED"
    assert record["Collection started (UTC)"] == "2026-10-07T03:59:00.000000Z"
    assert record["Report cutoff (UTC)"] == "2026-10-12T01:00:00.000000Z"
    assert record["Last published (UTC)"] == "2026-10-12T02:00:00+00:00"
    assert record["Comparable prices"] == "4.0"  # Includes TGT-000002 out-of-stock price.
    assert record["Matched but excluded (stock or price rules)"] == "0.0"
    assert "historical Shopee snapshot" in record["Shopee sales snapshot"]


# Repeatability, partial failure, readback


def test_repeated_publication_does_not_duplicate_rows_or_touch_the_store(world) -> None:
    store_bytes = world["store"].read_bytes()
    fake = FakeSpreadsheet()
    assert run(world, fake=fake)[0] == 0
    first = {t: fake.values(t) for t in (COMPARISON_TAB, HISTORY_TAB, REVIEW_TAB)}
    assert run(world, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))[0] == 0

    assert {t: fake.values(t) for t in first} == first
    header, *rows = fake.values(RUN_LOG_TAB)
    assert len(rows) == 1
    record = dict(zip(header, rows[0], strict=True))
    assert record["Publication attempts"] == "2.0"
    assert record["First published (UTC)"] == "2026-10-12T02:00:00+00:00"
    assert world["store"].read_bytes() == store_bytes
    # The replaced publisher tabs were backed up locally before the second write.
    second = sorted(world["out"].iterdir())[-1]
    assert {p.name for p in second.glob("backup_*.json")} == {
        f"backup_{t}.json" for t in PUBLISHER_TABS
    }


def test_partial_failure_is_reported_and_a_retry_recovers(world, capsys) -> None:
    fake = FakeSpreadsheet()
    fake.fail_on_batch = 3  # 1 run log, 2 comparison, 3 summary

    code, fake, _ = run(world, fake=fake)

    assert code == 1
    report = latest_report(world)
    assert report["status"] == "PARTIAL_FAILURE"
    assert report["tabs_written"] == [COMPARISON_TAB]
    assert "simulated API failure" in report["error"]
    header, row = fake.values(RUN_LOG_TAB)
    record = dict(zip(header, row, strict=True))
    assert record["Publication status"] == "PARTIAL_FAILURE"
    assert record["Tabs written"] == COMPARISON_TAB
    assert "PARTIAL_FAILURE" in capsys.readouterr().out

    fake.fail_on_batch = None
    assert run(world, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))[0] == 0
    assert latest_report(world)["status"] == "SUCCESS"


@pytest.mark.parametrize("damage", ["value", "link", "extra", "missing"])
def test_readback_detects_missing_or_altered_cells(world, damage) -> None:
    fake = FakeSpreadsheet()

    def corrupt(sheet: FakeSpreadsheet) -> None:
        if sheet.batches != 2:  # the comparison tab write
            return
        grid = sheet.sheets[COMPARISON_TAB]["grid"]
        if damage == "value":
            grid[(1, 1)]["userEnteredValue"] = {"stringValue": "TGT-999999"}
        elif damage == "link":
            cell = next(c for c in grid.values() if c.get("hyperlink"))
            cell["hyperlink"] = "https://evil.example/"
        elif damage == "extra":
            grid[(40, 0)] = {"userEnteredValue": {"stringValue": "stray"}}
        else:
            grid.pop((7, 0))

    fake.after_batch = corrupt
    code, fake, _ = run(world, fake=fake)

    assert code == 1
    report = latest_report(world)
    assert report["status"] == "READBACK_FAILED"
    assert report["readback_issues"]
    header, row = fake.values(RUN_LOG_TAB)
    assert dict(zip(header, row, strict=True))["Publication status"] == "READBACK_FAILED"


def test_verify_tab_compares_values_links_and_formats() -> None:
    planned = [[text("Header", bold=True)], [Cell("number", "1000.00", "", "php")],
               [text("https://x.example/a?variant=1", link="https://x.example/a?variant=1")]]
    good = TabState("T", True, 1, 10, 1, (
        ({"userEnteredValue": {"stringValue": "Header"}},),
        ({"userEnteredValue": {"numberValue": 1000.0},
          "userEnteredFormat": {"numberFormat": {"type": "CURRENCY"}}},),
        ({"userEnteredValue": {"stringValue": "https://x.example/a?variant=1"},
          "hyperlink": "https://x.example/a?variant=1"},),
    ))
    assert verify_tab(good, planned) == []
    stripped = TabState("T", True, 1, 10, 1, (good.grid[0], good.grid[1],
                                              ({"userEnteredValue": {"stringValue":
                                                                     "https://x.example/a"}},)))
    assert len(verify_tab(stripped, planned)) == 2  # text and link both differ
    zero = TabState("T", True, 1, 10, 1, (good.grid[0], ({"userEnteredValue":
                                                          {"numberValue": 0}},), good.grid[2]))
    assert any("number differs" in issue for issue in verify_tab(zero, planned))
