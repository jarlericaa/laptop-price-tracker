"""The compact Sheets dashboard (layout variation-compact-1, 2026-11-05), tested against the
in-memory fake workbook and the synthetic variation reports of ``test_variation_publication``.

The redesign is presentation only: these tests check the layout (visible block, collapsed groups,
banner rows), that no information is dropped (full statuses, differences, links, IDs), that prices
stay numeric and linked, that a repeated publication leaves exactly one set of groups, rules, notes
and guide rows, and that the published numbers agree with the saved reports.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from scraping_fakes import no_network  # noqa: F401
from test_sheet_publication import FakeSpreadsheet
from test_variation_publication import (
    CONTRACT,
    NOW,
    SpyingSpreadsheet,
    column,
    data_cell,
    header_of,
    header_row,
    latest_report,
    run,
    template_tabs,
    world,  # noqa: F401
)
from test_verified_top import load_script

from laptop_tracker.result_labels import retailer_result
from laptop_tracker.sheet_guide import GUIDE_TAB, plan_guide
from laptop_tracker.sheet_publication import (
    NUMBER_FORMATS,
    STATUS_FILLS,
    Cell,
    TabState,
    labelled_php,
    verify_tab,
)
from laptop_tracker.variation_publication import (
    ALTERNATIVES_TAB,
    COMPARISON_TAB,
    OVERVIEW_TAB,
    RETAILER_NAMES,
    RETAILER_ORDER,
    REVIEW_TAB,
    TOP_TAB,
    apply_navigation,
    build_variation_plan,
    comparison_groups,
    comparison_header,
    comparison_layout,
    comparison_visible_width,
    guide_entries,
    load_variation_report,
    read_tab_with_retry,
    retailer_status_text,
    source_agreement_issues,
)

pytestmark = pytest.mark.usefixtures("no_network")


def built(world):  # noqa: F811
    report = load_variation_report(world["variation"], world["reports"], world["context"])
    return report, build_variation_plan(report, CONTRACT)


def guide_tab() -> list[list[str]]:
    """The owner's guide in miniature: label in A, text in B, an archive link row, a template."""
    return [
        ["LAPTOP PRICE TRACKER"],
        ["STATUS", "Layouts ready — awaiting the first variation-level publication."],
        ["MATCH", "old"],
        [
            "Current results",
            "No Top 100 variation rows have been published under this new structure.",
        ],
        ["Previous comparison", '=HYPERLINK("#gid=1","Open archived configuration comparison")'],
        ["Data dates", "A Shopee sales-extract snapshot is not automatically a live price."],
    ]


def guide_workbook() -> SpyingSpreadsheet:
    fake = SpyingSpreadsheet(template_tabs() | {GUIDE_TAB: guide_tab()})
    sheet = fake.sheets[GUIDE_TAB]
    owner_format = {"backgroundColor": {"red": 1, "green": 1, "blue": 1}, "wrapStrategy": "WRAP"}
    for row in range(1, 6):
        for col in range(2):
            sheet["grid"].setdefault((row, col), {})["userEnteredFormat"] = copy.deepcopy(
                owner_format | {"textFormat": {"bold": col == 0}}
            )
        sheet["merges"].append(
            {
                "sheetId": sheet["id"],
                "startRowIndex": row,
                "endRowIndex": row + 1,
                "startColumnIndex": 1,
                "endColumnIndex": 6,
            }
        )
    return fake


# Layout: a short visible block, then collapsed groups that keep every column


def test_the_visible_block_is_the_requested_compact_layout() -> None:
    header = list(comparison_header())
    visible = header[: comparison_visible_width()]
    assert comparison_visible_width() == 22
    assert visible[:7] == [
        "Rank",
        "Laptop",
        "Shopee seller",
        "Shopee variation",
        "Target specs",
        "Shopee snapshot price (PHP)",
        "Shopee snapshot date",
    ]
    # Five retailer blocks, each Price / Result, in the existing retailer order.
    pairs = visible[7:17]
    assert pairs == [
        name
        for code in RETAILER_ORDER
        for name in (f"{RETAILER_NAMES[code]} price (PHP)", f"{RETAILER_NAMES[code]} result")
    ]
    assert [RETAILER_NAMES[c] for c in RETAILER_ORDER] == [
        "DataBlitz",
        "Laptop Factory",
        "PC Express",
        "PC Central",
        "Octagon",
    ]
    assert visible[17:] == [
        "Lowest in-stock matching retailer price (PHP)",
        "Lowest in-stock matching retailer(s)",
        "Lowest in-stock alternative retailer price (PHP)",
        "Lowest in-stock alternative retailer(s)",
        "Row coverage",
    ]
    assert len(set(header)) == len(header)


def test_the_groups_tile_every_column_after_the_visible_block_exactly_once() -> None:
    header = list(comparison_header())
    layout = comparison_layout(header)
    position = comparison_visible_width()
    for first, end in layout.groups:
        assert first == position  # contiguous, no gap, no overlap
        position = end
    assert position == len(header)
    labels = [label for label, _ in comparison_groups()]
    assert labels[0] == "Expanded specifications" and "Other price minima and bases" in labels
    assert [lab for lab in labels if lab.endswith(" details")] == [
        "Shopee details",
        *(f"{RETAILER_NAMES[c]} details" for c in RETAILER_ORDER),
    ]
    assert layout.frozen_columns == 3 and layout.header_row == 2 and layout.manage_columns


def test_publication_collapses_the_groups_and_uses_no_merged_cells(world) -> None:  # noqa: F811
    code, fake, _ = run(world)
    assert code == 0 and latest_report(world)["readback_issues"] == []

    sheet = fake.sheets[COMPARISON_TAB]
    layout = comparison_layout(comparison_header())
    assert [(g["range"]["startIndex"], g["range"]["endIndex"]) for g in sheet["groups"]] == list(
        layout.groups
    )
    assert all(g["collapsed"] and g["depth"] == 1 for g in sheet["groups"])
    assert sheet["hidden"] == set(range(comparison_visible_width(), len(comparison_header())))
    assert all(not fake.sheets[t]["merges"] for t in (COMPARISON_TAB, OVERVIEW_TAB))
    # Header and two identity columns frozen; banner rows sit above the sortable table.
    assert (sheet["frozen"], sheet["frozen_columns"]) == (3, 3)
    assert sheet["filter"]["startRowIndex"] == 2


def test_banner_rows_show_the_week_cutoff_and_legend_once(world) -> None:  # noqa: F811
    code, fake, _ = run(world)
    assert code == 0
    for title in (COMPARISON_TAB, OVERVIEW_TAB):
        values = fake.values(title)
        assert "Week 2026-10-05 to 2026-10-11" in values[0][1] and "cutoff" in values[0][1]
        assert "never live prices" in values[0][1]
        assert values[1][1:7] == ["Legend", "MATCH", "ALT 1", "ALT 2", "REVIEW", "NO PRICE"]
        assert values[2][0] == "Rank"
        assert not any("Legend" in row for row in values[3:])  # not repeated in the rows
        legend_fills = [fake.sheets[title]["layout"][(1, c)] for c in range(2, 7)]
        for fill, key in zip(
            legend_fills, ("match", "alt1", "alt2", "review", "missing"), strict=True
        ):
            colour = fill["backgroundColor"]
            assert all(abs(colour[k] - STATUS_FILLS[key][k]) < 0.002 for k in colour)


# Prices, links, labels and fills


def test_prices_stay_numeric_and_linked_and_a_missing_price_is_blank_not_zero(world) -> None:  # noqa: F811
    report, plan = built(world)
    code, fake, _ = run(world)
    assert code == 0
    priced = blank = 0
    for n, row in enumerate(report.comparison, start=1):
        for c in RETAILER_ORDER:
            name = RETAILER_NAMES[c]
            price = data_cell(fake, COMPARISON_TAB, n, f"{name} price (PHP)")
            label = column(fake, COMPARISON_TAB, f"{name} result")[n - 1]
            if row[f"{c}_Price"]:
                priced += 1
                assert "numberValue" in price["userEnteredValue"]
                assert price["userEnteredValue"]["numberValue"] == float(row[f"{c}_Price"])
                assert price["userEnteredFormat"]["numberFormat"]["type"] == "CURRENCY"
                assert price["hyperlink"] == row[f"{c}_Offer_URL"]  # native link, not text
                assert label.startswith(("MATCH", "ALT"))
            else:
                blank += 1
                assert "userEnteredValue" not in price  # blank underneath, never zero
                assert not label.startswith(("MATCH", "ALT"))
    assert priced and blank


def test_an_out_of_stock_matching_price_is_kept_and_labelled_but_not_an_in_stock_minimum(
    world,  # noqa: F811
) -> None:
    report, plan = built(world)
    comparison = plan.tabs[COMPARISON_TAB]
    index = {name: i for i, name in enumerate(comparison.header)}
    found = 0
    for source, cells in zip(report.comparison, comparison.rows, strict=True):
        for c in RETAILER_ORDER:
            if source[f"{c}_Stock"] != "OUT_OF_STOCK" or not source[f"{c}_Price"]:
                continue
            found += 1
            name = RETAILER_NAMES[c]
            assert cells[index[f"{name} price (PHP)"]].value == source[f"{c}_Price"]
            assert "Out of stock" in cells[index[f"{name} result"]].value
            in_stock = source["Lowest_In_Stock_Price"]
            assert (
                in_stock == ""
                or in_stock != source[f"{c}_Price"]
                or (source["Lowest_In_Stock_Price_Retailers"] != c)
            )
    assert found, "the synthetic report has an out-of-stock matching price"


def test_fills_never_stand_alone_and_banding_never_overrides_a_result_colour(world) -> None:  # noqa: F811
    code, fake, _ = run(world)
    assert code == 0
    layout = fake.sheets[COMPARISON_TAB]["layout"]
    top = header_row(COMPARISON_TAB)
    header = header_of(fake, COMPARISON_TAB)
    expected = {"MATCH": "match", "ALT 1": "alt1", "ALT 2": "alt2"}
    seen = set()
    for n in range(1, 13):
        for c in RETAILER_ORDER:
            name = RETAILER_NAMES[c]
            label = column(fake, COMPARISON_TAB, f"{name} result")[n - 1]
            colour = layout[(top + n, header.index(f"{name} result"))]["backgroundColor"]
            key = next((v for k, v in expected.items() if label.startswith(k)), "")
            if key:  # a comparison fill, with its text label, on odd and even rows alike
                seen.add((n % 2, key))
                assert all(abs(colour[ch] - STATUS_FILLS[key][ch]) < 0.002 for ch in colour)
            elif label:  # no price: neutral or review, still with text
                assert colour != STATUS_FILLS["match"]
    assert {parity for parity, _ in seen} == {0, 1}


# Complete detail preservation


def test_every_retailer_detail_is_kept_in_its_group_and_in_the_result_note(world) -> None:  # noqa: F811
    report, plan = built(world)
    comparison = plan.tabs[COMPARISON_TAB]
    index = {name: i for i, name in enumerate(comparison.header)}
    for source, cells in zip(report.comparison, comparison.rows, strict=True):
        for c in RETAILER_ORDER:
            name = RETAILER_NAMES[c]
            selection = report.selections[(source["comparison_row_id"], c)]
            full = retailer_status_text(source, c, selection)
            assert cells[index[f"{name} status"]].value == full  # the full explanation, intact
            assert (
                cells[index[f"{name} observed (Asia/Singapore)"]].value
                == source[f"{c}_Observed_At"]
            )
            assert cells[index[f"{name} product link"]].link == source[f"{c}_Offer_URL"]
            assert cells[index[f"{name} offer details"]].value == source[f"{c}_Offer_Details"]
            assert cells[index[f"{name} search / fetch outcome"]].value == source[f"{c}_Reason"]
            assert cells[index[f"{name} association ID"]].value == selection["association_id"]
            note = cells[index[f"{name} result"]].note
            assert full in note and selection["association_id"] in note
            assert source["context_id"] in note
            if source[f"{c}_Price"]:
                assert source[f"{c}_Offer_URL"] in note and "Stock:" in note
                assert source[f"{c}_Observed_At"] in note
            machine = cells[index[f"{name} machine status"]].value
            assert source[f"{c}_Price_Status"] in machine  # the machine status, not redefined


def test_alternative_differences_keep_their_target_to_retailer_direction() -> None:
    from laptop_tracker.variation_publication import _detail_cells, result_note

    row = {
        "comparison_row_id": "SV-1",
        "context_id": "CTX",
        "target_id": "TGT-000001",
        "brand": "Acme",
        "series": "",
        "model": "Aero",
        "DBZ_Price": "500.00",
        "DBZ_Price_Status": "ALTERNATIVE_SELECTED",
        "DBZ_Comparison_Tag": "ALT 2 · CPU, Storage",
        "DBZ_Stock": "IN_STOCK",
        "DBZ_Match_Basis": "ALTERNATIVE",
        "DBZ_Offer_URL": "https://example.test/dbz",
        "DBZ_Observed_At": "2026-11-05T10:00:00+08:00",
        "DBZ_Warnings": "SPECIFIC_MODEL_UNKNOWN;GPU_UNKNOWN",
        "DBZ_Reason": "CLOSE_ALTERNATIVE: ALT 2 · CPU, Storage",
        "DBZ_Offer_Details": "offer: model X; CPU Core 3",
        "DBZ_Alternative_Differences": "CPU: target Ryzen 3 → retailer Core 3; storage: target "
        "128GB → retailer 512GB.",
        "DBZ_Alternative_Unknowns": "GPU: GPU not stated by the retailer; limited basis: model "
        "FAMILY_ONLY",
        "DBZ_Alternative_Offers": "1",
        "DBZ_Candidate_URL": "",
        "DBZ_Candidate_Status": "",
        "DBZ_Recheck_Count": "",
        "DBZ_Recheck_Note": "",
        "DBZ_Candidate_Detail": "",
        "DBZ_Alternative_Observation_ID": "OBS-1",
        "rank": "1",
        "row_comparison_status": "ALTERNATIVE_PRICES_ONLY",
        "row_comparison_detail": "",
        "source_key": "k",
        "mapping_status": "MAPPED_VERIFIED",
    }
    selection = {
        "association_id": "ASSOC-1",
        "observation_id": "OBS-1",
        "not_comparable_reasons": "",
    }
    alt = {
        "comparison_basis": "model FAMILY_ONLY",
        "warnings": "GPU_UNKNOWN",
        "offer_model": "X",
        "offer_cpu_series": "Core 3",
        "offer_cpu_model": "",
        "offer_screen_in": "",
        "offer_ram_gb": "",
        "offer_storage_gb": "512",
        "offer_gpu": "",
        "title": "",
        "state": "",
    }
    result = retailer_result(row, "DBZ", selection, alt)
    assert result.label == "ALT 2 · CPU, Storage · Limited"
    details = _detail_cells(None, row, "DBZ", result, selection, alt, {("SV-1", "DBZ"): 1})
    assert (
        details["DataBlitz differences (target → retailer)"].value
        == row["DBZ_Alternative_Differences"]
    )
    assert "limited basis" in details["DataBlitz unknown fields and limits"].value
    assert details["DataBlitz alternatives / review link"].value == "Alternatives (1)"
    note = result_note(None, row, "DBZ", result, selection, alt)
    assert "Differences (target → retailer): CPU: target Ryzen 3 → retailer Core 3" in note
    assert "Limited comparison basis: model FAMILY_ONLY" in note
    assert "Unknown fields and limits" in note and "GPU not stated by the retailer" in note


# The visible minimum-retailer cells keep every word the earlier layout had (found by comparing the
# new plan with the previous publisher's on the saved 2026-11-05 run).


def test_the_matching_minimum_keeps_its_package_note_and_marks_a_listed_spec_winner() -> None:
    from laptop_tracker.variation_publication import _minimum_text

    row = {
        "Lowest_In_Stock_Price_Retailers": "LF;PCX",
        "Lowest_In_Stock_Notes": "LF:INCLUDED_ITEMS_OFFER",
        "Lowest_In_Stock_Basis": "EXACT_PRODUCT",
    }
    keys = ("Lowest_In_Stock_Price_Retailers", "Lowest_In_Stock_Notes", "Lowest_In_Stock_Basis")
    assert _minimum_text(row, *keys) == "Laptop Factory (includes stated items), PC Express"
    listed = row | {"Lowest_In_Stock_Basis": "LISTED_SPEC"}
    assert _minimum_text(listed, *keys).endswith(" · Listed specs")
    mixed = row | {"Lowest_In_Stock_Basis": "MIXED"}
    assert _minimum_text(mixed, *keys).endswith(" · Listed specs (some)")


def test_the_alternative_minimum_names_retailer_tag_limited_basis_and_package_note() -> None:
    from laptop_tracker.variation_publication import _alternative_minimum_text

    prefix = "Lowest_Alternative_In_Stock_Price"
    row = {
        "comparison_row_id": "SV-1",
        prefix: "500.00",
        f"{prefix}_Retailers": "DBZ",
        f"{prefix}_Notes": "DBZ:INCLUDED_ITEMS_OFFER",
        f"{prefix}_Tag": "DBZ:ALT 2 · Screen, CPU",
        f"{prefix}_Observation_IDs": "DBZ:OBS-1",
    }
    alternatives = {("SV-1", "DBZ", "OBS-1"): {"comparison_basis": "CPU SERIES_ONLY"}}
    assert _alternative_minimum_text(row, prefix, alternatives) == (
        "DataBlitz: ALT 2 · Screen, CPU · Limited (includes stated items)"
    )
    complete = {("SV-1", "DBZ", "OBS-1"): {"comparison_basis": ""}}
    assert "Limited" not in _alternative_minimum_text(row, prefix, complete)
    # Without a tag the report's own retailer wording is kept.
    assert _alternative_minimum_text(row | {f"{prefix}_Tag": ""}, prefix, alternatives) == (
        "DataBlitz (includes stated items)"
    )
    assert _alternative_minimum_text({"comparison_row_id": "SV-1"}, prefix, {}) == ""


# Snapshot price


def test_the_snapshot_price_is_labelled_snapshot_dated_and_flagged_stale(world) -> None:  # noqa: F811
    report, plan = built(world)
    code, fake, _ = run(world)
    assert code == 0
    header = header_of(fake, COMPARISON_TAB)
    assert "Shopee snapshot price (PHP)" in header and "Shopee snapshot date" in header
    assert not any("live" in h.lower() or "current" in h.lower() for h in header)
    stale_rows = 0
    for n, row in enumerate(report.comparison, start=1):
        date = column(fake, COMPARISON_TAB, "Shopee snapshot date")[n - 1]
        assert date.startswith(row["shopee_price_observed_at"])  # the source date, not today's
        overview = data_cell(fake, OVERVIEW_TAB, n, "Shopee (PHP, snapshot)")
        if row["shopee_price_status"] == "SNAPSHOT_PRICE_STALE":
            stale_rows += 1
            assert date.endswith("STALE")
            assert overview["userEnteredFormat"]["numberFormat"]["pattern"].endswith('STALE"')
        else:
            assert "STALE" not in date
        assert "numberValue" in overview["userEnteredValue"]  # still numeric
    note = data_cell(fake, COMPARISON_TAB, 1, "Shopee snapshot date").get("note", "")
    assert "date is the source's" in note
    assert stale_rows, "the synthetic extract is older than seven days at the cutoff"


# Repeat publication, notes and rules


def snapshot_of(fake) -> dict:
    out = {}
    for title in (COMPARISON_TAB, OVERVIEW_TAB):
        sheet = fake.sheets[title]
        out[title] = {
            "values": fake.values(title),
            "notes": {k: v["note"] for k, v in sheet["grid"].items() if v.get("note")},
            "links": {k: v["hyperlink"] for k, v in sheet["grid"].items() if v.get("hyperlink")},
            "groups": copy.deepcopy(sheet["groups"]),
            "cf": copy.deepcopy(sheet["cf"]),
            "hidden": set(sheet["hidden"]),
        }
    return out


def test_a_repeated_publication_leaves_exactly_the_same_groups_notes_rules_and_guide(
    world,  # noqa: F811
) -> None:
    fake = guide_workbook()
    code, fake, _ = run(world, fake=fake)
    assert code == 0
    first = snapshot_of(fake)
    first_guide = fake.values(GUIDE_TAB)
    guide_merges = copy.deepcopy(fake.sheets[GUIDE_TAB]["merges"])

    code, fake, _ = run(world, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))
    assert code == 0 and latest_report(world)["readback_issues"] == []
    assert snapshot_of(fake) == first
    assert fake.values(GUIDE_TAB) == first_guide  # appended rows are found, not appended again
    assert fake.sheets[GUIDE_TAB]["merges"] == guide_merges
    assert len(fake.sheets[COMPARISON_TAB]["groups"]) == len(comparison_groups())
    assert fake.sheets[COMPARISON_TAB]["cf"] == []


def test_notes_from_an_earlier_run_do_not_survive_on_changed_or_cleared_cells(world) -> None:  # noqa: F811
    code, fake, _ = run(world)
    assert code == 0
    sheet = fake.sheets[COMPARISON_TAB]
    top = header_row(COMPARISON_TAB)
    header = header_of(fake, COMPARISON_TAB)
    result_col = header.index("DataBlitz result")
    detail_col = header.index("DataBlitz machine status")  # never carries a note
    sheet["grid"][(top + 1, result_col)]["note"] = "NOTE FROM A PREVIOUS RUN"
    sheet["grid"][(top + 1, detail_col)]["note"] = "STRAY NOTE"

    code, fake, _ = run(world, fake=fake, now=datetime(2026, 10, 12, 3, tzinfo=UTC))
    assert code == 0
    cell = fake.cell(COMPARISON_TAB, top + 1, result_col)
    assert "PREVIOUS RUN" not in cell["note"] and cell["note"].startswith("DataBlitz: ")
    assert "note" not in fake.cell(COMPARISON_TAB, top + 1, detail_col)


def legacy_workbook(world) -> SpyingSpreadsheet:  # noqa: F811
    """The live workbook as the 2026-11-05 run left it: the 80-column comparison under its stale
    header note, and owner-built conditional formats keyed to those column positions."""
    (earlier,) = CONTRACT.earlier_headers[COMPARISON_TAB]
    tabs = template_tabs()
    tabs[COMPARISON_TAB] = [list(earlier), ["1", "Old Store", *[""] * (len(earlier) - 2)]]
    fake = SpyingSpreadsheet(tabs)
    stale = "Layout prepared 2026-10-27; no variation-level data has been published."
    fake.sheets[COMPARISON_TAB]["grid"][(0, 0)]["note"] = stale
    for title in (COMPARISON_TAB, OVERVIEW_TAB):
        sheet = fake.sheets[title]
        for column_index in (17, 23, 29):
            sheet["cf"].append(
                {
                    "ranges": [
                        {
                            "sheetId": sheet["id"],
                            "startRowIndex": 1,
                            "endRowIndex": 151,
                            "startColumnIndex": column_index,
                            "endColumnIndex": column_index + 1,
                        }
                    ],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": '=REGEXMATCH(TO_TEXT($T2),"^MATCH")'}],
                        },
                        "format": {"backgroundColor": {"red": 0.9, "green": 1, "blue": 0.9}},
                    },
                }
            )
    fake.sheets[ALTERNATIVES_TAB]["cf"].append({"ranges": [{"sheetId": 1}], "booleanRule": {}})
    return fake


def test_an_earlier_live_layout_is_replaced_with_no_stale_rules_notes_or_headers(world) -> None:  # noqa: F811
    fake = legacy_workbook(world)
    code, fake, _ = run(world, fake=fake)
    assert code == 0 and latest_report(world)["readback_issues"] == []

    # The owner's rules were keyed to the old column positions: gone from the two tabs this layout
    # moves, untouched elsewhere.
    assert fake.sheets[COMPARISON_TAB]["cf"] == [] and fake.sheets[OVERVIEW_TAB]["cf"] == []
    assert len(fake.sheets[ALTERNATIVES_TAB]["cf"]) == 1
    assert header_of(fake, COMPARISON_TAB) == list(comparison_header())
    notes = [c["note"] for c in fake.sheets[COMPARISON_TAB]["grid"].values() if c.get("note")]
    assert notes and not any("no variation-level data" in n for n in notes)
    assert "Old Store" not in sum(fake.values(COMPARISON_TAB), [])  # earlier cells replaced


def test_preview_reads_the_earlier_layout_and_changes_nothing(world) -> None:  # noqa: F811
    fake = legacy_workbook(world)
    code, fake, opened = run(world, mode="--preview", fake=fake)
    assert code == 0 and opened == [True] and fake.mutations == []
    folder = sorted(world["out"].iterdir())[-1]
    assert (folder / f"plan_{COMPARISON_TAB}_notes.csv").is_file()
    assert latest_report(world)["layout_version"] == "variation-compact-1"


def test_a_tab_with_unrecognised_content_is_still_refused(world, capsys) -> None:  # noqa: F811
    tabs = template_tabs()
    tabs[COMPARISON_TAB] = [["Rank", "Something unexpected"], ["1", "x"]]
    code, fake, _ = run(world, fake=SpyingSpreadsheet(tabs))
    assert code == 1 and fake.mutations == []
    assert "live header differs from the contract" in capsys.readouterr().err


def test_rollback_restores_the_earlier_layout_with_its_rules_and_groups(world) -> None:  # noqa: F811
    fake = legacy_workbook(world)
    before_cf = copy.deepcopy(fake.sheets[COMPARISON_TAB]["cf"])
    before_values = fake.values(COMPARISON_TAB)
    code, fake, _ = run(world, fake=fake)
    assert code == 0 and fake.sheets[COMPARISON_TAB]["groups"]
    folder = [p for p in sorted(world["out"].iterdir()) if p.name.endswith("_write")][-1]

    load_script("publish_variation_prices").main(
        ["--rollback", str(folder), "--output-root", str(world["out"])],
        spreadsheet_factory=lambda read_only: fake,
        now=datetime(2026, 10, 12, 5, tzinfo=UTC),
    )
    sheet = fake.sheets[COMPARISON_TAB]
    assert fake.values(COMPARISON_TAB) == before_values
    assert sheet["cf"] == before_cf and sheet["groups"] == [] and sheet["hidden"] == set()


# TRACKER_GUIDE


def test_the_guide_is_updated_in_place_and_new_rows_copy_the_template_row(world) -> None:  # noqa: F811
    fake = guide_workbook()
    code, fake, _ = run(world, fake=fake)
    assert code == 0 and latest_report(world)["tracker_guide"] == "UPDATED"

    rows = fake.values(GUIDE_TAB)
    by_label = {row[0]: row[1] for row in rows if len(row) > 1}
    assert by_label["STATUS"].startswith("Published 2026-10-12: 12 Top 100 Shopee variation rows")
    assert "awaiting the first variation-level publication" not in " ".join(by_label.values())
    assert "No Top 100 variation rows have been published" not in by_label["Current results"]
    assert by_label["Previous comparison"].startswith("=HYPERLINK")  # an owner row: untouched
    for label in ("Compact view", "Expanded columns", "Result colours"):
        assert [r[0] for r in rows].count(label) == 1
    sheet = fake.sheets[GUIDE_TAB]
    appended = next(i for i, r in enumerate(rows) if r and r[0] == "Compact view")
    template = next(i for i, r in enumerate(rows) if r and r[0] == "Data dates")
    assert (
        sheet["grid"][(appended, 1)]["userEnteredFormat"]
        == sheet["grid"][(template, 1)]["userEnteredFormat"]
    )
    assert any(m["startRowIndex"] == appended and m["endColumnIndex"] == 6 for m in sheet["merges"])
    assert not any("TRACKER_GUIDE" in m for m in fake.mutations)  # only the guide's own sheet ID


def test_the_guide_is_skipped_with_a_reason_when_missing_or_unrecognisable(world) -> None:  # noqa: F811
    code, fake, _ = run(world)  # no guide tab in the default workbook
    assert code == 0 and latest_report(world)["tracker_guide"].startswith("SKIPPED")
    odd = SpyingSpreadsheet(template_tabs() | {GUIDE_TAB: [["just prose"], [""]]})
    odd.sheets[GUIDE_TAB]["grid"][(0, 0)] = {"userEnteredValue": {"numberValue": 3.0}}
    plan = plan_guide(odd, [("STATUS", "x")])
    assert plan.skipped and not plan.requests


def test_a_guide_failure_is_reported_but_never_undoes_the_data_tabs(world, monkeypatch) -> None:  # noqa: F811
    import laptop_tracker.variation_publication as module

    monkeypatch.setattr(module, "write_guide", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    code, fake, _ = run(world, fake=guide_workbook())
    assert code == 0 and latest_report(world)["status"] == "SUCCESS"
    assert latest_report(world)["tracker_guide"].startswith("FAILED")


def test_rollback_restores_the_guide_text_and_merges(world) -> None:  # noqa: F811
    fake = guide_workbook()
    before = fake.values(GUIDE_TAB)
    before_merges = copy.deepcopy(fake.sheets[GUIDE_TAB]["merges"])
    code, fake, _ = run(world, fake=fake)
    assert code == 0 and fake.values(GUIDE_TAB) != before
    folder = [p for p in sorted(world["out"].iterdir()) if p.name.endswith("_write")][-1]
    assert (folder / f"backup_{GUIDE_TAB}.json").is_file()
    load_script("publish_variation_prices").main(
        ["--rollback", str(folder), "--output-root", str(world["out"])],
        spreadsheet_factory=lambda read_only: fake,
        now=datetime(2026, 10, 12, 5, tzinfo=UTC),
    )
    assert fake.values(GUIDE_TAB) == before
    assert fake.sheets[GUIDE_TAB]["merges"] == before_merges


def test_the_guide_text_documents_the_compact_view_labels_and_snapshot_semantics() -> None:
    entries = dict(
        guide_entries(
            layout_version="variation-compact-1",
            publication_id="PUB-1",
            week="2026-11-02 to 2026-11-08",
            cutoff="2026-11-05 12:14",
            rows=100,
            published="2026-11-05",
        )
    )
    assert "not a current or live price" in entries["Shopee prices"] or (
        "not a" in entries["Shopee prices"] and "live price" in entries["Shopee prices"]
    )
    assert "STALE" in entries["Shopee prices"] and "monthly" in entries["Shopee prices"]
    assert "Listed specs" in entries["MATCH"] and "Out of stock" in entries["MATCH"]
    assert "Limited" in entries["ALT 1"] and "Variant ambiguous" in entries["RECHECK"]
    assert "never mean the retailer does not sell" in entries["Data dates"]
    assert "collapsed" in entries["Expanded columns"].lower() and "Compact view" in entries
    assert "awaiting" not in " ".join(entries.values())


# Navigation


def test_navigation_links_are_found_by_row_id_and_retailer_not_by_rank_or_position(
    world,  # noqa: F811
) -> None:
    _, plan = built(world)
    rid = (
        plan.tabs[COMPARISON_TAB]
        .rows[3][plan.tabs[COMPARISON_TAB].header.index("Comparison row ID")]
        .value
    )
    header = plan.tabs[ALTERNATIVES_TAB].header
    blank = [Cell()] * len(header)

    def alt_row(row_id: str, retailer: str) -> tuple[Cell, ...]:
        cells = list(blank)
        cells[header.index("Comparison row ID")] = Cell("text", row_id)
        cells[header.index("Retailer")] = Cell("text", retailer)
        return tuple(cells)

    # Two other rows come first: the destination is the row with this ID, wherever it sits.
    alternatives = replace(
        plan.tabs[ALTERNATIVES_TAB],
        rows=(
            alt_row("SV-OTHER", "DataBlitz"),
            alt_row("SV-OTHER-2", "DataBlitz"),
            alt_row(rid, "DataBlitz"),
        ),
    )
    comparison = plan.tabs[COMPARISON_TAB]
    index = {n: i for i, n in enumerate(comparison.header)}
    cells = list(comparison.rows[3])
    cells[index["DataBlitz alternatives / review link"]] = Cell("text", "Alternatives (1)")
    cells[index["DataBlitz result"]] = replace(
        cells[index["DataBlitz result"]], value="ALT 1 · CPU"
    )
    rows = list(comparison.rows)
    rows[3] = tuple(cells)
    edited = replace(
        plan,
        tabs={
            **plan.tabs,
            COMPARISON_TAB: replace(comparison, rows=tuple(rows)),
            ALTERNATIVES_TAB: alternatives,
        },
    )
    states = {
        ALTERNATIVES_TAB: TabState(ALTERNATIVES_TAB, True, 777),
        REVIEW_TAB: TabState(REVIEW_TAB, True, 888),
    }
    linked = apply_navigation(edited, states)

    out = linked.tabs[COMPARISON_TAB].rows[3]
    assert out[index["DataBlitz alternatives / review link"]].link == "#gid=777&range=A4"  # row 4
    assert out[index["DataBlitz result"]].link == "#gid=777&range=A4"
    assert linked.content_sha256 != edited.content_sha256
    # Linking again changes nothing (idempotent): links are recomputed, never accumulated.
    assert apply_navigation(linked, states).tabs[COMPARISON_TAB].rows == (
        linked.tabs[COMPARISON_TAB].rows
    )


# Presentation only


def test_the_published_numbers_equal_the_saved_reports(world) -> None:  # noqa: F811
    report, plan = built(world)
    assert source_agreement_issues(report, plan.tabs) == []

    tampered = copy.deepcopy(plan.tabs[COMPARISON_TAB])
    index = tampered.header.index("DataBlitz price (PHP)")
    row = list(tampered.rows[0])
    row[index] = Cell("number", "1.00", "", "php")
    bad = replace(tampered, rows=(tuple(row), *tampered.rows[1:]))
    issues = source_agreement_issues(report, {**plan.tabs, COMPARISON_TAB: bad})
    assert any("DataBlitz price (PHP)" in issue for issue in issues)


def test_selected_variations_ranks_and_ids_are_those_of_the_ranking_inputs(world) -> None:  # noqa: F811
    report, plan = built(world)
    code, fake, _ = run(world)
    assert code == 0
    assert column(fake, COMPARISON_TAB, "Comparison row ID") == [
        r["comparison_row_id"] for r in report.comparison
    ]
    assert column(fake, COMPARISON_TAB, "Rank") == [
        f"{float(r['rank'])!r}" for r in report.comparison
    ]
    # The ranked list the comparison was built from lists the same variations in the same order.
    assert column(fake, COMPARISON_TAB, "Comparison row ID") == column(
        fake, TOP_TAB, "comparison_row_id"
    )


# Number formats and readback


def test_a_labelled_price_stays_numeric_and_its_label_is_read_back() -> None:
    key = labelled_php("MATCH · Out of stock")
    assert NUMBER_FORMATS[key]["type"] == "CURRENCY"
    assert NUMBER_FORMATS[key]["pattern"] == '"₱"#,##0.00" · MATCH · Out of stock"'
    cell = Cell("number", "19990.00", "", key)
    from laptop_tracker.sheet_publication import _cell_data

    data = _cell_data(cell)
    assert data["userEnteredValue"] == {"numberValue": 19990.0}  # the amount, never text
    state = TabState("T", True, 1, 10, 1, ((data,),))
    assert verify_tab(state, [[cell]]) == []
    wrong = {**data, "userEnteredFormat": {"numberFormat": NUMBER_FORMATS["php"]}}
    assert any(
        "label format differs" in issue
        for issue in verify_tab(TabState("T", True, 1, 10, 1, ((wrong,),)), [[cell]])
    )


def test_a_read_refused_for_the_per_minute_quota_is_retried_not_reported_as_a_failure() -> None:
    fake = FakeSpreadsheet(template_tabs())
    calls = []
    real = fake.fetch_sheet_metadata

    def flaky(params=None):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("APIError [429]: Quota exceeded for 'Read requests per minute'")
        return real(params)

    fake.fetch_sheet_metadata = flaky
    slept: list[int] = []
    state = read_tab_with_retry(fake, COMPARISON_TAB, sleep=slept.append)
    assert state.exists and slept == [20, 30]
    fake.fetch_sheet_metadata = lambda params=None: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        read_tab_with_retry(fake, COMPARISON_TAB, sleep=slept.append)  # other errors are raised


def test_the_run_is_deterministic_for_the_same_inputs(world) -> None:  # noqa: F811
    code, _, _ = run(world, now=NOW)
    first = latest_report(world)["published_content_sha256"]
    code, _, _ = run(world, now=datetime(2026, 10, 12, 3, tzinfo=UTC))
    assert code == 0 and latest_report(world)["published_content_sha256"] == first
