# ruff: noqa: F811  (the ``world`` fixture is imported, then named as a test parameter)
"""The PRICE COMPARISON dashboard design (2026-11-22): colours, hierarchy, notes, sizes.

A presentation change only: the values, links, number formats, statuses, eligibility and
competitiveness of every cell are the ones ``test_price_comparison_view`` pins down. These tests pin
what the dashboard adds: which semantic style each state gets, that colour never decides a state,
how the header and coverage chip look, that the few remaining notes stay few, that sizes and
alignment are as designed, and that a repeated publication leaves the same formatting.

What they cannot show is how Google Sheets renders it: they read the requests the publisher sends
and the formats the offline workbook keeps (see docs/PRICE_COMPARISON_TAB_2026-11-12.md).
"""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta

import pytest
from test_price_comparison_view import lowest, priced, row, shown
from test_variation_publication import NOW, SpyingSpreadsheet, run, template_tabs, world  # noqa: F401

from laptop_tracker import price_comparison_view as pv
from laptop_tracker import retailer_coverage as rc
from laptop_tracker import sheet_publication as sp
from laptop_tracker import variation_publication as vp
from laptop_tracker.result_labels import retailer_result

CODES = [code for code, _ in pv.RETAILER_COLUMNS]
INDEX = {name: i for i, name in enumerate(pv.PRICE_HEADER)}
FIRST_RETAILER, FIRST_RESULT = INDEX["DataBlitz"], INDEX["Lowest Retailer"]


def hexcode(colour: dict) -> str:
    red, green, blue = (round(colour[k] * 255) for k in ("red", "green", "blue"))
    return f"#{red:02X}{green:02X}{blue:02X}"


def hex_of(key: str, registry: dict) -> str | None:
    return hexcode(registry[key]) if key in registry else None


# A small simulator of Sheets' field-mask semantics over the publisher's own requests.


def simulate(requests):
    formats: dict[tuple[int, int], dict] = {}
    widths: dict[int, int] = {}
    heights: dict[int, int] = {}

    def merge(target: dict, source: dict) -> None:
        for key, value in source.items():
            if key in ("textFormat", "borders") and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    for request in requests:
        if "repeatCell" in request:
            rng = request["repeatCell"]["range"]
            fmt = request["repeatCell"]["cell"].get("userEnteredFormat", {})
            if "startRowIndex" not in rng:
                formats.clear()
                continue
            for r in range(rng["startRowIndex"], rng["endRowIndex"]):
                for c in range(rng["startColumnIndex"], rng["endColumnIndex"]):
                    merge(formats.setdefault((r, c), {}), fmt)
        elif "updateDimensionProperties" in request:
            rng = request["updateDimensionProperties"]["range"]
            size = request["updateDimensionProperties"]["properties"]["pixelSize"]
            target = widths if rng["dimension"] == "COLUMNS" else heights
            for i in range(rng["startIndex"], rng["endIndex"]):
                target[i] = size
    return formats, widths, heights


class Report:
    week_start, week_end = date(2026, 11, 16), date(2026, 11, 23)
    as_of = datetime(2026, 11, 22, 10, 58, tzinfo=UTC)

    def __init__(self, coverage=None) -> None:
        self.retailer_coverage = coverage


def coverage_of(down=(), partial=False):
    ids = ["datablitz", "pcx", "laptop_factory", "pcc", "oct"]
    outcomes = {f"{r}:RESULTS": 10 for r in ids if r not in down}
    for retailer in down:
        outcomes |= {f"{retailer}:SEARCH_ERROR": 2, f"{retailer}:NOT_SENT_RETAILER_FAILING": 8}
    return rc.from_summary({"search_outcomes": outcomes, "scope": {"retailers": ids}})


def cells_of(fields, *, stale=False, note="") -> tuple[list, dict]:
    """The 19 display cells of one comparison row, built by the publisher's own cell functions."""
    r = lowest(**fields)
    cells = [
        pv.shopee_cell(r | {"shopee_listing_url": "https://s.example/i"}, stale=stale, note=note)
    ]
    cells += [
        pv.retailer_cell(r, code, name, retailer_result(r, code))[0]
        for code, name in pv.RETAILER_COLUMNS
    ]
    cells += pv.competitiveness_cells(r, pv.lowest_in_stock(r), lowest_note="")
    return cells, r


def plan_of(row_cells, report=None) -> sp.TabPlan:
    return sp.TabPlan(
        pv.PRICE_TAB,
        pv.PRICE_HEADER,
        tuple(row_cells),
        tuple(pv.COLUMN_NOTES.get(name, "") for name in pv.PRICE_HEADER),
        vp.price_banner(report or Report(coverage_of())),
    )


def full_row(index: int, fields, **kw) -> tuple:
    cells, _ = cells_of(fields, **kw)
    product = [sp.number(str(index), "integer")] + [sp.text(f"p{n}") for n in range(7)]
    return tuple(product + cells)


def requests_of(plan: sp.TabPlan):
    grid = plan.grid()
    layout = pv.price_layout(pv.PRICE_HEADER)
    return grid, layout, [*sp.layout_requests(1, len(grid), layout), *sp.fill_requests(1, grid)]


# ---- palette and hierarchy ------------------------------------------


def test_the_palette_is_the_designed_one_and_every_style_has_one_meaning() -> None:
    palette = {
        "d_price": ("#E5F3F0", "#1F5F53", "RIGHT"),
        "d_oos": ("#FCE5D3", "#8A4B1B", "RIGHT"),
        "d_review": ("#FFF0CE", "#8A5B12", "CENTER"),
        "d_unchecked": ("#EDF1F5", "#64748B", "CENTER"),
        "d_nomatch": (None, "#94A3B8", "CENTER"),
        "d_cmp_green": ("#DDF2E4", "#207348", "CENTER"),
        "d_cmp_red": ("#F9E0DE", "#AD3E39", "CENTER"),
        "d_cmp_none": ("#EDF1F5", "#64748B", "CENTER"),
        "d_gap_green": ("#DDF2E4", "#207348", "RIGHT"),
        "d_gap_red": ("#F9E0DE", "#AD3E39", "RIGHT"),
        "d_cov_warn": ("#FFF0CE", "#8A5B12", None),
    }
    for key, (background, ink, align) in palette.items():
        assert hex_of(key, sp.STATUS_FILLS) == background, key
        assert hex_of(key, sp.STATUS_INK) == ink, key
        assert sp.STATUS_ALIGN.get(key) == align, key
    # The same colour means the same thing wherever it appears: amber is only ever "look at this".
    amber = {k for k in sp.STATUS_FILLS if hex_of(k, sp.STATUS_FILLS) == "#FFF0CE"}
    assert amber == {"d_review", "d_cov_warn", "d_stale"}
    green = {k for k in sp.STATUS_FILLS if hex_of(k, sp.STATUS_FILLS) == "#DDF2E4"}
    assert green == {"d_cmp_green", "d_gap_green"}  # green is only ever Shopee cheaper


def test_title_header_and_section_colours_form_one_hierarchy() -> None:
    grid, layout, requests = requests_of(plan_of([full_row(1, {})]))
    formats, _, _ = simulate(requests)

    def bg(r, c):
        return hexcode(formats[(r, c)]["backgroundColor"])

    def ink(r, c):
        return hexcode(formats[(r, c)]["textFormat"]["foregroundColor"])

    # Row 1: the title, white on the primary navy, large.
    assert {bg(0, c) for c in range(19)} == {"#1B365D"} and ink(0, 0) == "#FFFFFF"
    assert formats[(0, 0)]["textFormat"]["fontSize"] == 18 and grid[0][0].bold
    # Row 2: a quiet strip.
    assert {bg(1, c) for c in range(19) if c < FIRST_RETAILER} == {"#F6F8FC"}
    # Row 3: three distinct section bands, each over exactly its columns.
    bands = {(c): bg(2, c) for c in range(19)}
    assert {bands[c] for c in range(0, FIRST_RETAILER)} == {"#D6E2F1"}
    assert {bands[c] for c in range(FIRST_RETAILER, FIRST_RESULT)} == {"#D8ECE7"}
    assert {bands[c] for c in range(FIRST_RESULT, 19)} == {"#E3E8F0"}
    # Row 4: navy headers, a lighter navy over the retailers, orange for the Shopee price.
    header = {name: bg(3, c) for name, c in INDEX.items()}
    assert header["Rank"] == header["Laptop Model"] == header["Lowest Retailer"] == "#1B365D"
    assert {header[n] for _, n in pv.RETAILER_COLUMNS} == {"#294D73"}
    assert header["Shopee Price"] == "#EE8B35" and ink(3, INDEX["Shopee Price"]) == "#1B365D"
    assert ink(3, INDEX["Rank"]) == ink(3, INDEX["Octagon"]) == "#FFFFFF"
    assert hexcode(formats[(3, 0)]["borders"]["bottom"]["color"]) == "#EE8B35"
    # Only the Shopee price column carries the orange accent in the body.
    body = {name: bg(4, INDEX[name]) for name in ("Shopee Price", "Variation")}
    assert body["Shopee Price"] == "#FFF4E8" and body["Variation"] != "#FFF4E8"


def test_a_thin_rule_separates_the_three_sections_below_the_title() -> None:
    _, layout, requests = requests_of(plan_of([full_row(1, {}), full_row(2, {})]))
    formats, _, _ = simulate(requests)
    assert layout.separators == (FIRST_RETAILER, FIRST_RESULT)
    for column in range(19):
        edge = formats[(5, column)].get("borders", {}).get("left")
        assert bool(edge) == (column in layout.separators), column
        if edge:
            assert hexcode(edge["color"]) == "#C9D3E0" and edge["style"] == "SOLID"
    assert "borders" not in formats[(0, FIRST_RETAILER)]  # the title bar is not cut
    assert all("borders" in formats[(r, FIRST_RETAILER)] for r in (2, 3, 4, 5))


def test_rows_alternate_and_a_status_colour_always_beats_the_banding() -> None:
    rows = [full_row(n, priced("DBZ", "50000.0") | {"PCX_Price_Status": "CANDIDATE_NOT_CONFIRMED"})
            for n in range(1, 7)]  # fmt: skip
    plan = plan_of(rows)
    _, _, requests = requests_of(plan)
    formats, _, _ = simulate(requests)
    first = 4  # the first data row
    bands = [
        hexcode(formats[(first + n, INDEX["Laptop Model"])]["backgroundColor"]) for n in range(6)
    ]
    assert bands == ["#FFFFFF", "#F6F8FC"] * 3
    for n in range(6):  # whichever band, a price stays teal and Review stays amber
        r = first + n
        assert hexcode(formats[(r, INDEX["DataBlitz"])]["backgroundColor"]) == "#E5F3F0"
        assert hexcode(formats[(r, INDEX["PC Express"])]["backgroundColor"]) == "#FFF0CE"


# ---- retailer states ------------------------------------------------


CAP = "NO_RELEVANT_CANDIDATE: 3 search page(s); search coverage RESULT_CAP_REACHED"


@pytest.mark.parametrize(
    ("fields", "shows", "style"),
    [
        (priced("DBZ", "54950.0"), "₱54,950", "d_price"),
        (priced("DBZ", "52990.0", "OUT_OF_STOCK"), "₱52,990 (OOS)", "d_oos"),
        (priced("DBZ", "52990.0", "UNKNOWN"), "₱52,990 (Stock unknown)", "d_stock_unknown"),
        ({"DBZ_Price_Status": "CANDIDATE_NOT_CONFIRMED"}, "Review", "d_review"),
        ({"DBZ_Price_Status": "NO_RELEVANT_CANDIDATE", "DBZ_Reason": CAP}, "Review", "d_review"),
        ({"DBZ_Price_Status": "SEARCH_FAILED"}, "Not checked", "d_unchecked"),
        ({"DBZ_Price_Status": "CANDIDATE_NOT_FETCHED"}, "Not checked", "d_unchecked"),
        ({"DBZ_Price_Status": "NO_CANDIDATE_FOUND"}, "No match", "d_nomatch"),
    ],
)
def test_every_retailer_state_has_its_own_style_and_its_text_is_unchanged(
    fields, shows, style
) -> None:
    r = row(**fields)
    cell, shows_price = pv.retailer_cell(r, "DBZ", "DataBlitz", retailer_result(r, "DBZ"))
    assert pv.display_text(cell) == shows == shown(r)  # the style never changes what is said
    assert cell.fill == style and shows_price == (cell.kind == "number")
    assert cell.note == ""  # no repeated explanation on ordinary cells
    if cell.kind == "number":
        assert cell.link == r["DBZ_Offer_URL"] and cell.number_format.startswith("php|view|")


def test_no_unverified_state_is_ever_dressed_as_a_price() -> None:
    price_styles = {"d_price", "d_oos", "d_stock_unknown"}
    for status in ("SEARCH_FAILED", "CANDIDATE_NOT_FETCHED", "CANDIDATE_NOT_CONFIRMED",
                   "NO_CANDIDATE_FOUND", "NO_RELEVANT_CANDIDATE", "REVIEW_REQUIRED_ONLY",
                   "NO_OBSERVATION_IN_WEEK", "DISCOVERY_INCOMPLETE"):  # fmt: skip
        r = row(DBZ_Price_Status=status, DBZ_Price="40000.0", DBZ_Stock="IN_STOCK")
        cell, shows_price = pv.retailer_cell(r, "DBZ", "DataBlitz", retailer_result(r, "DBZ"))
        assert not shows_price and cell.kind == "text" and cell.fill not in price_styles, status
        assert "40" not in cell.value  # a price that is not a verified match is never printed
    # Not checked is never turned into No match (or the reverse) by a colour.
    assert shown(row(DBZ_Price_Status="SEARCH_FAILED")) == "Not checked"
    assert shown(row(DBZ_Price_Status="NO_CANDIDATE_FOUND")) == "No match"
    assert sp.STATUS_FILLS["d_unchecked"] != sp.STATUS_FILLS["d_review"]
    # A row with no trusted context is a dash, not a state.
    dash = pv.retailer_cell(row(context_id=""), "DBZ", "DataBlitz", retailer_result({}, "DBZ"))[0]
    assert dash.value == "—" and dash.fill == "d_dash"


def test_alignment_follows_content_prices_right_short_statuses_centred() -> None:
    for key in ("d_price", "d_oos", "d_stock_unknown", "d_stale", "d_gap_green", "d_gap_red"):
        assert sp.STATUS_ALIGN[key] == "RIGHT", key
    for key in ("d_review", "d_unchecked", "d_nomatch", "d_dash", "d_cmp_green", "d_cmp_none"):
        assert sp.STATUS_ALIGN[key] == "CENTER", key
    fields = priced("DBZ", "5000.0") | {
        "Lowest_In_Stock_Price": "5000.0",
        "Lowest_In_Stock_Price_Retailers": "DBZ",
        "shopee_price_php": "5200.0",
    }
    _, _, requests = requests_of(plan_of([full_row(1, fields)]))
    formats, _, _ = simulate(requests)
    at = lambda name: formats[(4, INDEX[name])]["horizontalAlignment"]  # noqa: E731
    assert at("DataBlitz") == "RIGHT" and at("PC Express") == "CENTER"  # a price; a status
    assert at("Shopee Price") == "RIGHT" and at("Lowest Price") == "RIGHT"
    assert at("Rank") == "CENTER" and at("RAM") == at("Storage") == "CENTER"
    assert at("Laptop Model") == "LEFT" and at("Lowest Retailer") == "LEFT"


# ---- competitiveness ------------------------------------------------


@pytest.mark.parametrize(
    ("shopee", "lowest_price", "label", "label_style", "gap_style"),
    [
        ("48990.0", "49999.0", "Shopee cheaper", "d_cmp_green", "d_gap_green"),
        ("56390.0", "54950.0", "Retailer cheaper", "d_cmp_red", "d_gap_red"),
        ("1000", "1000", "Same price", "d_cmp_same", "d_gap_same"),
    ],
)
def test_the_three_result_cells_of_a_row_share_one_colour(
    shopee, lowest_price, label, label_style, gap_style
) -> None:
    offer = pv.LowestOffer(lowest_price, ("DataBlitz",), "")
    *_, gap, percent, result = pv.competitiveness_cells(
        row(shopee_price_php=shopee), offer, lowest_note=""
    )
    assert result.value == label and result.fill == label_style and result.bold
    assert gap.fill == percent.fill == gap_style
    assert hex_of(label_style, sp.STATUS_FILLS) == hex_of(gap_style, sp.STATUS_FILLS)
    assert hex_of(label_style, sp.STATUS_INK) == hex_of(gap_style, sp.STATUS_INK)


def test_no_comparison_is_muted_and_never_green_red_or_zero() -> None:
    names, price, gap, percent, result = pv.competitiveness_cells(
        row(shopee_price_php="50000"), None, lowest_note=""
    )
    assert [c.value for c in (names, price, gap, percent)] == ["—"] * 4
    assert {c.fill for c in (names, price, gap, percent)} == {"d_dash"}
    assert result.value == "No comparison" and result.fill == "d_cmp_none"
    assert hex_of("d_cmp_none", sp.STATUS_INK) == "#64748B"
    assert "d_cmp_none" not in (pv.COMPETITIVENESS_FILL[pv.SHOPEE_CHEAPER],
                                pv.COMPETITIVENESS_FILL[pv.RETAILER_CHEAPER])  # fmt: skip


def test_the_result_colours_are_confined_to_the_last_three_columns() -> None:
    rows = [full_row(1, priced("PCX", "54950.0") | {
        "shopee_price_php": "56390.0", "Lowest_In_Stock_Price": "54950.0",
        "Lowest_In_Stock_Price_Retailers": "PCX"})]  # fmt: skip
    _, _, requests = requests_of(plan_of(rows))
    formats, _, _ = simulate(requests)
    red = sp.STATUS_FILLS["d_cmp_red"]
    tinted = [
        c for c in range(19)
        if formats.get((4, c), {}).get("backgroundColor") == red
    ]  # fmt: skip
    assert tinted == [INDEX["Price Gap (₱)"], INDEX["Price Gap (%)"], INDEX["Competitiveness"]]


# ---- coverage chip --------------------------------------------------


@pytest.mark.parametrize(
    ("down", "fill", "bold", "text_in"),
    [
        ((), "d_cov_ok", False, "Coverage: 5/5 retailers checked"),
        (("laptop_factory",), "d_cov_warn", True, "Laptop Factory incomplete"),
        (("pcx", "oct", "pcc"), "d_cov_warn", True, "Partial comparison"),
        (("datablitz", "pcx", "laptop_factory", "pcc", "oct"), "d_cov_bad", True, "0/5 retailers"),
    ],
)
def test_the_coverage_chip_is_neutral_amber_or_red_and_only_a_gap_explains_itself(
    down, fill, bold, text_in
) -> None:
    second = vp.price_banner(Report(coverage_of(down)))[1]
    chip = second[FIRST_RETAILER]
    assert text_in in chip.value and chip.fill == fill and chip.bold == bold
    assert bool(chip.note) == bool(down)  # one note, and only when coverage is incomplete
    # The chip colours exactly the retailer columns; the week stays a separate, plain cell.
    assert [c.fill for c in second[FIRST_RETAILER:FIRST_RESULT]] == [fill] * 5
    assert not any(c.fill or c.value for c in (*second[1:FIRST_RETAILER], *second[FIRST_RESULT:]))
    assert second[0].value.startswith("Week: ") and "Coverage" not in second[0].value
    assert len(chip.value) < 100  # reads without horizontal scrolling


def test_without_a_discovery_summary_no_coverage_is_claimed() -> None:
    second = vp.price_banner(Report(None))[1]
    assert not any(c.fill or c.value for c in second[1:])


def test_week_and_cutoff_read_naturally_across_months_and_years() -> None:
    assert vp.week_label(date(2026, 11, 16), date(2026, 11, 22)) == "Nov 16–22, 2026"
    assert vp.week_label(date(2026, 11, 30), date(2026, 12, 6)) == "Nov 30 – Dec 6, 2026"
    assert vp.week_label(date(2026, 12, 28), date(2027, 1, 3)) == "Dec 28, 2026 – Jan 3, 2027"
    assert vp.cutoff_label(datetime(2026, 11, 22, 10, 58, tzinfo=UTC)) == "Nov 22, 6:58 PM SGT"
    assert vp.cutoff_label(datetime(2026, 11, 22, 4, 5, tzinfo=UTC)) == "Nov 22, 12:05 PM SGT"
    assert vp.cutoff_label(datetime(2026, 11, 22, 16, 30, tzinfo=UTC)) == "Nov 23, 12:30 AM SGT"


# ---- notes ----------------------------------------------------------


def hundred_rows() -> list[tuple]:
    kinds = [
        priced("DBZ", "54950.0") | {"PCX_Price_Status": "CANDIDATE_NOT_CONFIRMED"},
        {"DBZ_Price_Status": "NO_CANDIDATE_FOUND", "LF_Price_Status": "SEARCH_FAILED",
         "PCX_Price_Status": "CANDIDATE_NOT_CONFIRMED", "PCC_Price_Status": "NO_RELEVANT_CANDIDATE",
         "PCC_Reason": CAP},
        priced("PCX", "25990.0", "OUT_OF_STOCK") | priced("OCT", "26990.0", "UNKNOWN"),
        priced("DBZ", "19990.0") | priced("PCC", "20990.0") | priced("OCT", "22990.0"),
    ]  # fmt: skip
    return [full_row(n + 1, kinds[n % 4], note="") for n in range(100)]


def test_a_hundred_rows_carry_only_the_header_notes() -> None:
    plan = plan_of(hundred_rows(), Report(coverage_of(("laptop_factory",))))
    notes = [
        (r, c) for r, line in enumerate(plan.grid()) for c, cell in enumerate(line) if cell.note
    ]
    body = [(r, c) for r, c in notes if r > 3]
    assert body == []  # not one note on 100 data rows (about 700 before)
    assert len(notes) <= 13  # header explanations once, and the one coverage note
    assert {c for r, c in notes if r == 3} == {
        INDEX[n]
        for n in (
            "Shopee Price",
            "DataBlitz",
            "PC Express",
            "Laptop Factory",
            "PC Central",
            "Octagon",
            "Lowest Retailer",
            "Lowest Price",
            "Price Gap (₱)",
            "Price Gap (%)",
            "Competitiveness",
        )  # fmt: skip
    }
    assert all(len(plan.header_notes[c]) < 300 for _, c in notes if c < len(plan.header_notes))


def test_the_notes_that_remain_are_the_ones_a_reader_needs_beside_the_number() -> None:
    listed = row(**priced("DBZ", "5000.0", DBZ_Match_Basis="LISTED_SPEC"))
    cell, _ = pv.retailer_cell(listed, "DBZ", "DataBlitz", retailer_result(listed, "DBZ"))
    assert "listed specs" in cell.note and "unknown" in cell.note
    stale = pv.shopee_cell(
        row(shopee_price_php="49990.0"), stale=True, note="STALE: older than 7 days"
    )
    assert stale.note and stale.fill == "d_stale"


def test_a_published_run_has_notes_only_on_headers_chip_and_exceptions(tmp_path, world) -> None:
    code, fake, _ = run(world, fake=SpyingSpreadsheet(template_tabs()))
    assert code == 0
    sheet = fake.sheets[pv.PRICE_TAB]
    noted = [(r, c) for (r, c), cell in sheet["grid"].items() if cell.get("note")]
    assert {r for r, _ in noted} <= {1, 3} | {r for r, _ in noted if r > 3}
    body = [(r, c) for r, c in noted if r > 3]
    # whatever remains in the body is a listed-spec caveat or a stale or missing Shopee price
    for r, c in body:
        note = sheet["grid"][(r, c)]["note"]
        assert c in (INDEX["Shopee Price"], INDEX["Lowest Price"]) or "listed specs" in note, note
    assert len([n for n in noted if n[0] == 3]) <= 11


# ---- sizes ----------------------------------------------------------


def test_column_widths_and_row_heights_are_the_designed_ones(world) -> None:
    code, fake, _ = run(world, fake=SpyingSpreadsheet(template_tabs()))
    assert code == 0
    sheet = fake.sheets[pv.PRICE_TAB]
    widths = [sheet["widths"][c] for c in range(19)]
    assert widths == [48, 130, 190, 170, 160, 62, 72, 100, 125, 124, 124, 124, 124, 124,
                      135, 104, 100, 92, 132]  # fmt: skip
    heights = sheet["heights"]
    assert [heights[r] for r in range(4)] == [46, 28, 28, 38]
    assert {heights[r] for r in range(4, 4 + len(fake.values(pv.PRICE_TAB)[4:]))} == {40}


def test_the_table_stays_searchable_and_unfiltered_with_a_frozen_header(world) -> None:
    code, fake, _ = run(world, fake=SpyingSpreadsheet(template_tabs()))
    assert code == 0 and fake.sheets[pv.PRICE_TAB]["filter"] is None
    assert pv.price_layout(pv.PRICE_HEADER).frozen_columns == 3
    assert pv.price_layout(pv.PRICE_HEADER).header_row == 3  # rows 1-4 frozen (header + 1)


# ---- persistence ----------------------------------------------------


def test_republishing_leaves_identical_formatting_and_no_rules(world) -> None:
    code, fake, _ = run(world, fake=SpyingSpreadsheet(template_tabs()))
    assert code == 0
    first = copy.deepcopy(fake.sheets[pv.PRICE_TAB])
    code, fake, _ = run(world, fake=fake, now=NOW + timedelta(minutes=9))
    assert code == 0
    again = fake.sheets[pv.PRICE_TAB]
    assert again["layout"] == first["layout"] and again["widths"] == first["widths"]
    assert again["heights"] == first["heights"] and again["cf"] == [] == first["cf"]


def test_a_shorter_publication_leaves_no_stale_highlight() -> None:
    long_plan = plan_of([full_row(n, priced("DBZ", "5000.0")) for n in range(1, 9)])
    short_plan = plan_of([full_row(1, {})])
    long_grid, _, long_requests = requests_of(long_plan)
    short_grid, _, short_requests = requests_of(short_plan)
    # The sheet is reset before anything is drawn, so rows the new table lacks keep no colour.
    after_long, _, _ = simulate(long_requests)
    after_both, _, _ = simulate([*long_requests, *short_requests])
    stale_rows = {r for (r, _) in after_long if r >= len(short_grid)}
    assert stale_rows and not {r for (r, _) in after_both if r >= len(short_grid)}


def test_a_wrong_dashboard_colour_is_caught_by_the_readback(world) -> None:
    code, fake, _ = run(world, fake=SpyingSpreadsheet(template_tabs()))
    assert code == 0
    sheet = fake.sheets[pv.PRICE_TAB]
    # find a styled body cell in the offline workbook and make its background wrong
    report = vp.load_variation_report(world["variation"], world["reports"], world["context"])
    plan = vp.build_variation_plan(report, vp.load_contract())
    plan_grid = plan.tabs[pv.PRICE_TAB].grid()
    target = next(
        (r, c)
        for r, line in enumerate(plan_grid)
        for c, cell in enumerate(line)
        if r > 3 and cell.fill in sp.STATUS_FILLS
    )
    sheet["layout"][target]["backgroundColor"] = sp.STATUS_FILLS["d_oos"]
    if plan_grid[target[0]][target[1]].fill == "d_oos":
        sheet["layout"][target]["backgroundColor"] = sp.STATUS_FILLS["d_review"]
    issues = sp.verify_tab(sp.read_tab(fake, pv.PRICE_TAB, vp.PUBLISHER_TABS), plan_grid)
    assert issues == [f"{pv.PRICE_TAB} row {target[0] + 1} column {target[1] + 1} fill differs"]
