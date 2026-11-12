"""The business-facing ``PRICE COMPARISON`` tab (layout ``variation-business-2``, 2026-11-12).

A projection of the saved variation reports, nothing more: it never selects, ranks, matches or
prices anything. Every number it shows is a value the reports already carry (the Shopee snapshot
price, each retailer's selected matching price, the report's own lowest *in-stock* matching price).
What it adds is the two derived numbers the report does not carry: the price gap and the
competitiveness result.

Rules the tests pin down:

- One row per selected Shopee variation, in the Top list's order; the Rank column is the Top
  list's own rank. No grouping, no filtering, no sorting of its own.
- A retailer cell shows a number only for a verified *matching* configuration (the report's
  ``SELECTED`` offer). Every other state (a close alternative, an unconfirmed candidate, a search
  that finished, was capped or never ran) is the one placeholder ``—``: *no verified comparable
  retailer price is available in this report*. It never says the retailer does not carry the
  product, and the underlying statuses stay in the hidden technical tabs and in ``PRICE REPORT``.
- The tab carries no cell notes: the explanations live in the guide, the documentation and the
  detailed report.
- Out-of-stock matching prices stay visible (``₱52,990 (OOS)``) but never compete for the lowest
  price. The lowest price is the report's ``Lowest_In_Stock_Price``, which uses only offers
  recorded ``IN_STOCK``; unknown stock is never counted as in stock.
- ``Price Gap = Shopee price - lowest in-stock matching retailer price``; the percentage is over
  that retailer price. A stale Shopee snapshot is still compared and says so beside its price.
- Prices are real numbers. The words beside an amount (``(OOS)``, ``⚠ Stale``) are part of the
  cell's number *format*, so sums, sorts and downstream readers still see the price and nothing
  parses a display string.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .result_labels import KIND_MATCH, RetailerResult
from .sheet_publication import (
    NUMBER_FORMATS,
    PHP_LABEL_PREFIX,
    Cell,
    ColumnStyle,
    TabLayout,
    text,
)

PRICE_TAB = "PRICE COMPARISON"

# Retailers in the order of the visible columns (also the tie order of the lowest retailers).
RETAILER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("DBZ", "DataBlitz"),
    ("PCX", "PC Express"),
    ("LF", "Laptop Factory"),
    ("PCC", "PC Central"),
    ("OCT", "Octagon"),
)
PRODUCT_COLUMNS: tuple[str, ...] = (
    "Rank",
    "Shopee Seller",
    "Laptop Model",
    "Variation",
    "CPU",
    "RAM",
    "Storage",
    "GPU",
    "Shopee Price",
)
COMPETITIVENESS_COLUMNS: tuple[str, ...] = (
    "Lowest Retailer",
    "Lowest Price",
    "Price Gap (₱)",
    "Price Gap (%)",
    "Competitiveness",
)
PRICE_HEADER: tuple[str, ...] = (
    *PRODUCT_COLUMNS,
    *(name for _, name in RETAILER_COLUMNS),
    *COMPETITIVENESS_COLUMNS,
)
SECTIONS: tuple[tuple[str, int, int], ...] = (
    ("Product Details", 0, len(PRODUCT_COLUMNS)),
    (
        "Retailer Prices",
        len(PRODUCT_COLUMNS),
        len(PRODUCT_COLUMNS) + len(RETAILER_COLUMNS),
    ),
    ("Price Competitiveness", len(PRODUCT_COLUMNS) + len(RETAILER_COLUMNS), len(PRICE_HEADER)),
)
assert len(PRICE_HEADER) == 19

# The one placeholder of a cell with no verified comparable price (never "does not carry").
UNAVAILABLE = "—"
SHOPEE_CHEAPER, RETAILER_CHEAPER, SAME_PRICE, NO_COMPARISON = (
    "Shopee cheaper",
    "Retailer cheaper",
    "Same price",
    "No comparison",
)
OOS_SUFFIX, STALE_SUFFIX, UNKNOWN_STOCK_SUFFIX = "(OOS)", "⚠ Stale", "(Stock unknown)"
PREORDER_SUFFIX = "(Preorder)"


def stock_suffix(stock: str) -> str:
    """The words beside a price for its recorded stock: nothing for ``IN_STOCK``; anything not
    verified in stock is said (never assumed available)."""
    return {"IN_STOCK": "", "OUT_OF_STOCK": OOS_SUFFIX, "PREORDER": PREORDER_SUFFIX}.get(
        stock, UNKNOWN_STOCK_SUFFIX
    )

COMPETITIVENESS_FILL = {
    SHOPEE_CHEAPER: "cmp_green",
    RETAILER_CHEAPER: "cmp_red",
    SAME_PRICE: "cmp_neutral",
    NO_COMPARISON: "cmp_gray",
}

# Number formats. The amount stays numeric; the suffix is display only.
_PESO_FORMATS: dict[str, tuple[str, bool]] = {}  # key -> (suffix, centavos)


def peso_format(suffix: str = "", *, centavos: bool = False) -> str:
    """The number-format key of a peso price with an optional display suffix. Whole pesos show
    without decimals; a price with centavos keeps them (a rounded display would misstate it)."""
    key = f"{PHP_LABEL_PREFIX}view|{suffix}|{int(centavos)}"
    pattern = '"₱"#,##0' + (".00" if centavos else "") + (f'" {suffix}"' if suffix else "")
    NUMBER_FORMATS.setdefault(key, {"type": "CURRENCY", "pattern": pattern})
    _PESO_FORMATS[key] = (suffix, centavos)
    return key


GAP_WHOLE, GAP_CENTAVOS, GAP_PERCENT = "view_gap_0", "view_gap_2", "view_gap_pct"
NUMBER_FORMATS.setdefault(GAP_WHOLE, {"type": "NUMBER", "pattern": '+"₱"#,##0;-"₱"#,##0;"₱"0'})
NUMBER_FORMATS.setdefault(
    GAP_CENTAVOS, {"type": "NUMBER", "pattern": '+"₱"#,##0.00;-"₱"#,##0.00;"₱"0.00'}
)
NUMBER_FORMATS.setdefault(GAP_PERCENT, {"type": "PERCENT", "pattern": "+0.00%;-0.00%;0.00%"})


def display_text(cell: Cell) -> str:
    """What a reader sees in a cell (used for the local preview and the tests)."""
    if cell.kind != "number":
        return cell.value
    value = Decimal(cell.value)
    key = cell.number_format
    if key in _PESO_FORMATS:
        suffix, centavos = _PESO_FORMATS[key]
        amount = f"₱{value:,.2f}" if centavos else f"₱{value:,.0f}"
        return f"{amount} {suffix}".strip()
    if key in (GAP_WHOLE, GAP_CENTAVOS):
        sign = "+" if value > 0 else "-" if value < 0 else ""
        places = 2 if key == GAP_CENTAVOS else 0
        return f"{sign}₱{abs(value):,.{places}f}"
    if key == GAP_PERCENT:
        points = value * 100
        sign = "+" if points > 0 else "-" if points < 0 else ""
        return f"{sign}{abs(points):.2f}%"
    return cell.value


def write_display_csv(path, plan) -> None:
    """A CSV of the tab as a reader sees it (banner rows, header, display text of every cell)."""
    import csv

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for row in plan.grid():
            writer.writerow([display_text(cell) for cell in row])


def _has_centavos(value: Decimal) -> bool:
    return value != value.to_integral_value()


# Product detail text


_VENDOR_WORDS = frozenset({"intel", "amd", "apple", "qualcomm", "mediatek"})
_INTEL = re.compile(r"^(core|celeron|pentium|xeon)\b", re.IGNORECASE)
_AMD = re.compile(r"^(ryzen|athlon)\b", re.IGNORECASE)
_TIER = re.compile(r"^([ir])(\d)[-\s]?(\S.*)$", re.IGNORECASE)
_SERIES_TIER = re.compile(r"(?:core i|ryzen )(\d)\b", re.IGNORECASE)
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _clean(value: str) -> str:
    return " ".join((value or "").split())


def _vendor(name: str) -> str:
    """The processor name with its maker, only where the family name says it (Core is Intel,
    Ryzen is AMD); anything else is left exactly as recorded."""
    if not name or name.split()[0].lower() in _VENDOR_WORDS:
        return name
    if _INTEL.match(name):
        return f"Intel {name}"
    if _AMD.match(name):
        return f"AMD {name}"
    return name


def _tokens(value: str) -> set[str]:
    return set(_TOKEN.findall(value.lower())) - _VENDOR_WORDS


def cpu_display(series: str, model: str) -> str:
    """``Intel Core i5-13420H``: the recorded CPU family and model read together, never invented.

    The recorded fields vary (``Core i5`` + ``i5-13420H``, ``Ryzen 7`` + ``Ryzen 7 7730U``,
    ``Apple M5`` + ``M5``, ``Core i5`` + ``Intel i5``). A model that adds nothing to the family is
    dropped, a model that extends it is shown whole, and a tiered short model (``i5-13420H``,
    ``R5-3450U``) is joined to the family it names. A conflicting pair is shown as recorded."""
    series, model = _clean(series), _clean(model)
    if not model:
        return _vendor(series)
    if not series:
        return _vendor(model)
    if _tokens(model) <= _tokens(series):
        return _vendor(series)
    if model.lower().startswith(series.lower()):
        return _vendor(model)
    tier = _TIER.match(model)
    family = _SERIES_TIER.search(series)
    if tier and family and tier.group(2) == family.group(1):
        joiner = "-" if series.lower().startswith("core") else " "
        return _vendor(f"{series}{joiner}{tier.group(3)}")
    return _vendor(f"{series} {model}")


def model_display(brand: str, series: str, model: str, fallback: str = "") -> str:
    """Brand, series and model of the mapped laptop as one short name (``HP Laptop 14``, not
    ``HP HP Laptop 14``): a part the next one already repeats is dropped. The listing's own
    title is used only when the mapped target records none of the three."""
    brand, series, model = _clean(brand), _clean(series), _clean(model)
    if brand and any(p.lower().startswith(brand.lower()) for p in (series, model) if p):
        brand = ""
    if series and model and model.lower().startswith(series.lower()):
        series = ""
    return " ".join(p for p in (brand, series, model) if p) or _clean(fallback)


def gpu_display(gpu: str) -> str:
    """``RTX4050`` -> ``RTX 4050``; otherwise as recorded (blank stays blank)."""
    return re.sub(r"^(RTX|GTX|RX|GT)(\d)", r"\1 \2", _clean(gpu), flags=re.IGNORECASE)


def ram_display(gigabytes: str) -> str:
    return f"{int(gigabytes)}GB" if gigabytes.isdigit() else ""


def storage_display(gigabytes: str) -> str:
    if not gigabytes.isdigit():
        return ""
    size = int(gigabytes)
    for unit in (1024, 1000):
        if size >= 1000 and size % unit == 0:
            return f"{size // unit}TB"
    return f"{size}GB"


# Retailer cells


def retailer_cell(row: Mapping[str, str], code: str, result: RetailerResult) -> tuple[Cell, bool]:
    """``(cell, shows_price)`` for one retailer of one row.

    A number only for the report's verified *matching* offer (``SELECTED``, MATCH, in a row with a
    trusted context); the stock words sit in the number format. Everything else is ``—``."""
    price = row.get(f"{code}_Price", "")
    if (
        result.priced
        and result.kind == KIND_MATCH
        and price
        and row.get(f"{code}_Price_Status") == "SELECTED"
    ):
        value = Decimal(price)
        cell = Cell(
            "number",
            price,
            row.get(f"{code}_Offer_URL", ""),
            peso_format(stock_suffix(row.get(f"{code}_Stock", "")), centavos=_has_centavos(value)),
        )
        return cell, True
    return text(UNAVAILABLE), False


# Competitiveness


@dataclass(frozen=True)
class Competitiveness:
    label: str
    gap: Decimal | None = None
    percent: Decimal | None = None  # as a fraction (0.0262 = 2.62 %), rounded to 0.01 point


def _positive(value: str) -> Decimal | None:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def compare(shopee: str, lowest: str) -> Competitiveness:
    """Shopee price against the lowest in-stock matching retailer price, both as report text.

    A missing or invalid (non-positive) price on either side is ``No comparison``, never zero."""
    own, other = _positive(shopee), _positive(lowest)
    if own is None or other is None:
        return Competitiveness(NO_COMPARISON)
    gap = own - other
    points = (gap / other * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    label = RETAILER_CHEAPER if gap > 0 else SHOPEE_CHEAPER if gap < 0 else SAME_PRICE
    return Competitiveness(label, gap, points / 100)


@dataclass(frozen=True)
class LowestOffer:
    price: str
    names: tuple[str, ...]
    link: str


def lowest_offer(
    row: Mapping[str, str],
    prefix: str,
    order: Sequence[tuple[str, str]] = RETAILER_COLUMNS,
    *,
    retailers_key: str = "",
    url_key: str = "",
) -> LowestOffer | None:
    """One of the report's lowest-price groups (``Lowest_In_Stock_Price`` or
    ``Lowest_Alternative_In_Stock_Price``): its price, retailers (in ``order``, so a tie lists
    every winner) and a link to the first winner's offer. ``None`` when the report records none.
    Nothing is recomputed: the group is the report's own."""
    price = row.get(prefix, "")
    codes = [c for c in row.get(retailers_key or f"{prefix}_Retailers", "").split(";") if c]
    if not price or not codes:
        return None
    urls = row.get(url_key or f"{prefix}_Offer_URL", "").split(";")
    link_of = dict(zip(codes, urls, strict=False)) if len(urls) == len(codes) else {}
    ordered = [(code, name) for code, name in order if code in codes]
    link = link_of.get(ordered[0][0], "") if ordered else ""
    return LowestOffer(price, tuple(name for _, name in ordered), link)


def lowest_in_stock(row: Mapping[str, str]) -> LowestOffer | None:
    """The report's lowest in-stock matching offer. ``None`` when no matching offer is recorded in
    stock. Out-of-stock and alternative prices never reach it: the report builds the minimum from
    in-stock comparable matching offers only."""
    return lowest_offer(
        row,
        "Lowest_In_Stock_Price",
        retailers_key="Lowest_In_Stock_Price_Retailers",
        url_key="Lowest_In_Stock_Offer_URL",
    )


def lowest_in_stock_issues(row: Mapping[str, str]) -> list[str]:
    """Cross-checks of the report's lowest in-stock offer against the retailer cells it came from.

    The winners must be retailers with a selected matching price, and no retailer cell that is a
    selected, in-stock matching price may be lower than the minimum. (A retailer's own cell shows
    its lowest matching offer, which can be out of stock while a dearer in-stock offer of the
    same retailer is the one that counts, so a cell above the minimum is not a contradiction.)"""
    key = row.get("comparison_row_id", "?")
    price = row.get("Lowest_In_Stock_Price", "")
    codes = [c for c in row.get("Lowest_In_Stock_Price_Retailers", "").split(";") if c]
    issues = []
    if bool(price) != bool(codes):
        issues.append(f"{key}: the lowest in-stock price and its retailers disagree")
    known = {code for code, _ in RETAILER_COLUMNS}
    for code in codes:
        if code not in known or row.get(f"{code}_Price_Status") != "SELECTED":
            issues.append(f"{key}: lowest in-stock retailer {code} has no selected matching price")
    bound = _positive(price)
    for code, name in RETAILER_COLUMNS:
        cell = _positive(row.get(f"{code}_Price", ""))
        if (
            bound is not None
            and cell is not None
            and row.get(f"{code}_Price_Status") == "SELECTED"
            and row.get(f"{code}_Stock") == "IN_STOCK"
            and cell < bound
        ):
            issues.append(f"{key}: {name} has an in-stock matching price below the minimum")
        if (
            bound is None
            and cell is not None
            and row.get(f"{code}_Price_Status") == "SELECTED"
            and row.get(f"{code}_Stock") == "IN_STOCK"
        ):
            issues.append(f"{key}: {name} has an in-stock matching price but no minimum is set")
    return issues


# Layout

_NUMBER = {"align": "RIGHT"}


def price_layout(header: Sequence[str]) -> TabLayout:
    """Title rows, section labels, a frozen header, three frozen identifying columns, no filter."""
    styles: dict[str, ColumnStyle] = {
        "Rank": ColumnStyle(44, "navy", **_NUMBER),
        "Shopee Seller": ColumnStyle(120),
        "Laptop Model": ColumnStyle(170),
        "Variation": ColumnStyle(150),
        "CPU": ColumnStyle(165),
        "RAM": ColumnStyle(58, **_NUMBER),
        "Storage": ColumnStyle(68, **_NUMBER),
        "GPU": ColumnStyle(95),
        "Shopee Price": ColumnStyle(140, "orange", **_NUMBER),
        "Lowest Retailer": ColumnStyle(125, "navy"),
        "Lowest Price": ColumnStyle(100, "navy", **_NUMBER),
        "Price Gap (₱)": ColumnStyle(96, "navy", **_NUMBER),
        "Price Gap (%)": ColumnStyle(86, "navy", **_NUMBER),
        "Competitiveness": ColumnStyle(130, "navy"),
    }
    for index, (_, name) in enumerate(RETAILER_COLUMNS):
        styles[name] = ColumnStyle(118, ("navy_alt", "navy")[index % 2], **_NUMBER)
    return TabLayout(
        tuple(styles[name] for name in header),
        3,
        42,
        40,
        preface_heights=(34, 24, 24),
        basic_filter=False,
        title_size=15,
    )


# Cells of the Shopee price and of the competitiveness block


def shopee_cell(row: Mapping[str, str], *, stale: bool) -> Cell:
    """The Shopee snapshot price: numeric, linked to the listing, ``⚠ Stale`` beside it (and a soft
    amber fill) when the report marks the snapshot stale. No valid price is ``—``."""
    price = row.get("shopee_price_php", "")
    value = _positive(price)
    if value is None:
        return text(UNAVAILABLE)
    return Cell(
        "number",
        price,
        row.get("shopee_listing_url", ""),
        peso_format(STALE_SUFFIX if stale else "", centavos=_has_centavos(value)),
        fill="stale" if stale else "",
    )


def lowest_price_cell(lowest: LowestOffer | None) -> Cell:
    """A lowest-price cell: the report's number linked to the first winner's offer, else ``—``."""
    if lowest is None:
        return text(UNAVAILABLE)
    return Cell(
        "number",
        lowest.price,
        lowest.link,
        peso_format("", centavos=_has_centavos(Decimal(lowest.price))),
    )


def competitiveness_cells(row: Mapping[str, str], lowest: LowestOffer | None) -> list[Cell]:
    """Lowest Retailer, Lowest Price, Price Gap (₱), Price Gap (%), Competitiveness."""
    price = lowest.price if lowest else ""
    result = compare(row.get("shopee_price_php", ""), price)
    names = text(", ".join(lowest.names) if lowest else UNAVAILABLE)
    shown = lowest_price_cell(lowest)
    fill = COMPETITIVENESS_FILL[result.label]
    if result.gap is None:
        gap, percent = text(UNAVAILABLE), text(UNAVAILABLE)
    else:
        gap = Cell(
            "number",
            format(result.gap, "f"),
            number_format=GAP_CENTAVOS if _has_centavos(result.gap) else GAP_WHOLE,
            fill=fill,
        )
        percent = Cell("number", format(result.percent, "f"), number_format=GAP_PERCENT, fill=fill)
    return [names, shown, gap, percent, text(result.label, fill=fill)]
