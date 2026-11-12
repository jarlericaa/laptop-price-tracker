"""The ``PRICE REPORT`` tab (layout ``variation-business-2``, 2026-11-12).

The comprehensive, side-by-side report: the first 22 columns of the technical
``PILOT_PRICE_COMPARISON`` (product and Shopee price, a Price and a Result per retailer, the two
in-stock minima and the row coverage), without its 100+ detail and debugging columns, and with
short standardised result labels instead of sentences. Like ``price_comparison_view`` it is a
projection of the saved report rows: it never selects, ranks, matches or prices anything, and it
carries no cell notes (the full explanation of every result stays in the hidden technical tabs).

Rules the tests pin down:

- One row per selected Shopee variation, in the Top list's order, retailers side by side in the
  technical comparison's order (DataBlitz, Laptop Factory, PC Express, PC Central, Octagon).
- A retailer Price is the price the report already selected for the cell: a verified matching
  configuration (``Match``) or the displayed close alternative (``Alt``). The label says which.
  An alternative is a different configuration: it never enters the lowest *matching* price, the
  matching retailers, the price gap or the competitiveness result.
- Stock stays visible on the price (``(OOS)``) and on the label (``· OOS``). Only prices recorded
  ``IN_STOCK`` count toward the two lowest-price columns (the report's own minima).
- ``No match`` means the search finished. A search that hit a result cap, left pages unread or
  stopped early is ``Search limited`` (``result_labels.search_unsettled``). A candidate or an
  excluded offer is ``Review``. A search or observation that never completed is ``Not checked``.
  None of them says a retailer does not carry the laptop.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from .price_comparison_view import (
    UNAVAILABLE,
    LowestOffer,
    _has_centavos,
    lowest_offer,
    lowest_price_cell,
    peso_format,
    stock_suffix,
)
from .result_labels import (
    KIND_ALT,
    KIND_EXCLUDED,
    KIND_MATCH,
    KIND_NONE,
    KIND_PENDING,
    KIND_REVIEW,
    RetailerResult,
    search_unsettled,
)
from .sheet_publication import Cell, ColumnStyle, TabLayout, text

REPORT_TAB = "PRICE REPORT"

# Retailers in the technical comparison's order (code, name). The tie order of the lowest-price
# retailers follows it too.
REPORT_RETAILERS: tuple[tuple[str, str], ...] = (
    ("DBZ", "DataBlitz"),
    ("LF", "Laptop Factory"),
    ("PCX", "PC Express"),
    ("PCC", "PC Central"),
    ("OCT", "Octagon"),
)
PRODUCT_COLUMNS: tuple[str, ...] = (
    "Rank",
    "Laptop",
    "Shopee Seller",
    "Variation",
    "Target Specs",
    "Shopee Price",
    "Shopee Price Date",
)
FINDING_COLUMNS: tuple[str, ...] = (
    "Lowest Matching Price",
    "Lowest Matching Retailer",
    "Lowest Alternative Price",
    "Lowest Alternative Retailer",
    "Coverage",
)
REPORT_HEADER: tuple[str, ...] = (
    *PRODUCT_COLUMNS,
    *(
        column
        for _, name in REPORT_RETAILERS
        for column in (f"{name} Price", f"{name} Result")
    ),
    *FINDING_COLUMNS,
)
_RETAILER_START = len(PRODUCT_COLUMNS)
_FINDING_START = _RETAILER_START + 2 * len(REPORT_RETAILERS)
REPORT_SECTIONS: tuple[tuple[str, int, int], ...] = (
    ("Product & Shopee", 0, _RETAILER_START),
    ("Retailer Comparison", _RETAILER_START, _FINDING_START),
    # Both minima are the report's in-stock minima: an out-of-stock price never counts.
    ("Comparison Findings (in-stock prices only)", _FINDING_START, len(REPORT_HEADER)),
)
assert len(REPORT_HEADER) == 22

REPORT_TITLE = "LAPTOP PRICE REPORT"

# Short result labels (presentation only; the machine statuses are unchanged).
MATCH, ALTERNATIVE, REVIEW = "Match", "Alternative", "Review"
NO_MATCH, SEARCH_LIMITED, NOT_CHECKED, NOT_COMPARED = (
    "No match",
    "Search limited",
    "Not checked",
    "Not compared",
)

# Fills: restrained tints. Green = verified match, amber = alternative or a candidate to review,
# soft red = a caveat that matters for buying (out of stock), grey = no price. The label always
# says the same thing; colour is never the only signal.
FILL_MATCH, FILL_AMBER, FILL_OOS, FILL_NONE = "match", "rpt_amber", "rpt_oos", "missing"


def stock_word(stock: str) -> str:
    """The stock qualifier of a label: nothing for ``IN_STOCK``; anything not verified in stock is
    named (a blank or unknown stock is never read as available)."""
    return {"IN_STOCK": "", "OUT_OF_STOCK": "OOS", "PREORDER": "Preorder"}.get(
        stock, "Stock unknown"
    )


def result_label(row: Mapping[str, str], code: str, result: RetailerResult) -> tuple[str, str]:
    """``(label, fill key)`` of one retailer's result in a row."""
    reason = row.get(f"{code}_Reason", "")
    if result.head == "NOT COMPARED":
        return NOT_COMPARED, FILL_NONE
    if result.priced and result.kind in {KIND_MATCH, KIND_ALT}:
        stock = stock_word(row.get(f"{code}_Stock", ""))
        if result.kind == KIND_MATCH:
            parts = [MATCH]
            if "Listed specs" in result.qualifiers:
                parts.append("Listed specs")
            fill = FILL_MATCH
        else:
            dimensions = result.head.partition(" · ")[2]
            limited = "Limited" in result.qualifiers
            # ``Alternative`` stands alone; with a qualifier it shortens to ``Alt``.
            alone = "Alt" if stock or limited else ALTERNATIVE
            parts = [f"Alt · {dimensions}" if dimensions else alone]
            if limited:
                parts.append("Limited")
            fill = FILL_AMBER
        if stock:
            parts.append(stock)
            fill = FILL_OOS if stock == "OOS" else FILL_AMBER
        return " · ".join(parts), fill
    if result.kind in {KIND_REVIEW, KIND_EXCLUDED}:
        return REVIEW, FILL_AMBER
    if result.kind == KIND_PENDING:
        return NOT_CHECKED, FILL_NONE
    if result.kind == KIND_NONE:
        if result.head in {"NO RESULTS", "NO RELEVANT"}:
            return (SEARCH_LIMITED if search_unsettled(reason) else NO_MATCH), FILL_NONE
        if result.head == "INCOMPLETE":
            return SEARCH_LIMITED, FILL_NONE
    return NOT_CHECKED, FILL_NONE


def retailer_cells(
    row: Mapping[str, str], code: str, result: RetailerResult
) -> tuple[Cell, Cell, str]:
    """``(price cell, result cell, kind)``. The price is the number the report selected for the
    cell (a matching or alternative offer) linked to that offer; no price is ``—``. ``kind`` is
    ``match``, ``alt`` or ``none`` for the coverage count."""
    label, fill = result_label(row, code, result)
    price = row.get(f"{code}_Price", "")
    selected = row.get(f"{code}_Price_Status") in {"SELECTED", "ALTERNATIVE_SELECTED"}
    kind = "none"
    if result.priced and price and selected and result.kind in {KIND_MATCH, KIND_ALT}:
        kind = "match" if result.kind == KIND_MATCH else "alt"
        shown = Cell(
            "number",
            price,
            row.get(f"{code}_Offer_URL", ""),
            peso_format(
                stock_suffix(row.get(f"{code}_Stock", "")), centavos=_has_centavos(Decimal(price))
            ),
        )
    else:
        shown = text(UNAVAILABLE)
    return shown, text(label, fill=fill), kind


def coverage_text(kinds: Sequence[str], *, compared: bool = True) -> str:
    """``2 matches · 1 alt · 2 not priced``: how the row's five cells split. Counts come from the
    cells themselves, so they always reconcile with them. Neutral on purpose: *not priced* never
    says the retailer does not sell the laptop."""
    if not compared:
        return NOT_COMPARED
    matches, alts = kinds.count("match"), kinds.count("alt")
    parts = []
    if matches:
        parts.append(f"{matches} {'match' if matches == 1 else 'matches'}")
    if alts:
        parts.append(f"{alts} {'alt' if alts == 1 else 'alts'}")
    if not parts:
        return "No verified prices"
    rest = len(kinds) - matches - alts
    if rest:
        parts.append(f"{rest} not priced")
    return " · ".join(parts)


def shopee_date_text(row: Mapping[str, str], *, stale: bool) -> str:
    """The snapshot's source date (never today's), flagged when the date is only a monthly source
    record or the snapshot is stale."""
    when = row.get("shopee_price_observed_at", "")
    if not row.get("shopee_price_php") or not when:
        return UNAVAILABLE
    parts = [when]
    if row.get("shopee_price_date_basis") == "M_MINUS_1_RECORD_DATE":
        parts.append("monthly record")
    if stale:
        parts.append("Stale")
    return " · ".join(parts)


def finding_cells(
    matching: LowestOffer | None,
    matching_retailers: str,
    alternative: LowestOffer | None,
    alternative_retailers: str,
    coverage: str,
) -> list[Cell]:
    """Lowest Matching Price / Retailer, Lowest Alternative Price / Retailer, Coverage."""
    return [
        lowest_price_cell(matching),
        text(matching_retailers or UNAVAILABLE),
        lowest_price_cell(alternative),
        text(alternative_retailers or UNAVAILABLE),
        text(coverage),
    ]


def matching_lowest(row: Mapping[str, str]) -> LowestOffer | None:
    return lowest_offer(
        row,
        "Lowest_In_Stock_Price",
        REPORT_RETAILERS,
        retailers_key="Lowest_In_Stock_Price_Retailers",
        url_key="Lowest_In_Stock_Offer_URL",
    )


def alternative_lowest(row: Mapping[str, str]) -> LowestOffer | None:
    return lowest_offer(row, "Lowest_Alternative_In_Stock_Price", REPORT_RETAILERS)


# Layout

_NUMBER = {"align": "RIGHT"}


def report_layout(header: Sequence[str]) -> TabLayout:
    """Title rows, section labels, a frozen header, three frozen identifying columns, no filter.
    Compact rows; a long result label wraps onto a second line instead of widening the column."""
    styles: dict[str, ColumnStyle] = {
        "Rank": ColumnStyle(44, "navy", **_NUMBER),
        "Laptop": ColumnStyle(190),
        "Shopee Seller": ColumnStyle(130),
        "Variation": ColumnStyle(150),
        "Target Specs": ColumnStyle(215),
        "Shopee Price": ColumnStyle(130, "orange", **_NUMBER),
        "Shopee Price Date": ColumnStyle(150, "orange"),
        "Lowest Matching Price": ColumnStyle(112, "navy", **_NUMBER),
        "Lowest Matching Retailer": ColumnStyle(150, "navy_alt"),
        "Lowest Alternative Price": ColumnStyle(112, "navy", **_NUMBER),
        "Lowest Alternative Retailer": ColumnStyle(150, "navy_alt"),
        "Coverage": ColumnStyle(150, "navy"),
    }
    for index, (_, name) in enumerate(REPORT_RETAILERS):
        shade = ("navy", "navy_alt")[index % 2]  # a pair shares its shade: the retailer boundary
        styles[f"{name} Price"] = ColumnStyle(112, shade, **_NUMBER)
        styles[f"{name} Result"] = ColumnStyle(168, shade)
    return TabLayout(
        tuple(styles[name] for name in header),
        3,
        42,
        40,
        preface_heights=(34, 24, 24),
        basic_filter=False,
        title_size=15,
    )
