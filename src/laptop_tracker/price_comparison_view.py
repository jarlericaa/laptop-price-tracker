"""The business-facing ``PRICE COMPARISON`` tab (layout ``variation-business-1``, 2026-11-12).

A projection of the saved variation reports, nothing more: it never selects, ranks, matches or
prices anything. Every number it shows is a value the reports already carry (the Shopee snapshot
price, each retailer's selected matching price, the report's own lowest *in-stock* matching price)
and every label is derived from the structured fields behind ``result_labels``. What it adds is
wording for business readers and the two derived numbers the report does not carry: the price gap
and the competitiveness result.

Rules the tests pin down:

- One row per selected Shopee variation, in the Top list's order; the Rank column is the Top
  list's own rank. No grouping, no filtering, no sorting of its own.
- A retailer cell shows a number only for a verified *matching* configuration (the report's
  ``SELECTED`` offer). A close alternative (ALT 1 / ALT 2) is a different configuration, so it is
  shown as ``No match`` and never as a price.
- ``No match`` means the search *finished*. A search that hit a result cap, left result pages
  unread or stopped early (``search_unsettled``) has not shown that no matching configuration
  exists, so it is ``Review``, never ``No match``.
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

from .result_labels import (
    KIND_ALT,
    KIND_EXCLUDED,
    KIND_MATCH,
    KIND_NONE,
    KIND_PENDING,
    KIND_REVIEW,
    RetailerResult,
    search_limited,
)
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

# Short retailer cell labels.
NO_MATCH, REVIEW, NOT_CHECKED, UNAVAILABLE = "No match", "Review", "Not checked", "—"
SHOPEE_CHEAPER, RETAILER_CHEAPER, SAME_PRICE, NO_COMPARISON = (
    "Shopee cheaper",
    "Retailer cheaper",
    "Same price",
    "No comparison",
)
OOS_SUFFIX, STALE_SUFFIX, UNKNOWN_STOCK_SUFFIX = "(OOS)", "⚠ Stale", "(Stock unknown)"

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


# The one coverage token that does not make a finished search uncertain: the optional category
# fallback was switched off by configuration, while the query plan itself ran to its end.
_FALLBACK_OFF = "search coverage FALLBACK_NOT_TRIED_DISABLED"


def search_unsettled(reason: str) -> bool:
    """True when the recorded search cannot show that no matching configuration exists: a result
    page hit the retailer's cap or has unread pages, the search stopped early (budget, provider
    failure, replay only), or the fallback was not tried for a reason other than being switched
    off. Reads the same recorded wording as ``result_labels.search_limited``; it changes no
    business outcome."""
    return search_limited(reason.replace(_FALLBACK_OFF, ""))


def retailer_status(result: RetailerResult, reason: str = "") -> str:
    """The short label of a retailer cell, or ``PRICE`` when it carries a verified price.

    ``No match``: the search finished and no matching configuration was verified (including a
    close alternative, which is a different configuration). ``Review``: a candidate needs
    verification, or the search was capped or cut short so the result remains uncertain (``reason``
    is the retailer's recorded search note). ``Not checked``: the search or observation did not
    complete. ``—``: the row has no trusted comparison, so no retailer is compared."""
    if result.head == "NOT COMPARED":
        return UNAVAILABLE
    if result.priced and result.kind == KIND_MATCH:
        return "PRICE"
    if result.kind == KIND_ALT:
        return REVIEW if search_unsettled(reason) else NO_MATCH
    if result.kind in (KIND_REVIEW, KIND_EXCLUDED):
        return REVIEW
    if result.kind == KIND_PENDING:
        return NOT_CHECKED
    if result.kind == KIND_NONE:
        if result.head not in {"NO RESULTS", "NO RELEVANT"}:
            return NOT_CHECKED
        return REVIEW if search_unsettled(reason) else NO_MATCH
    return NOT_CHECKED


_STATUS_MEANING = {
    NO_MATCH: "The search finished and no matching configuration was verified.",
    REVIEW: "A candidate needs manual verification, or the search could not be completed "
    "(result cap or unread pages); it is not shown as a price.",
    NOT_CHECKED: "The search or observation was not completed for this retailer.",
    UNAVAILABLE: "Not compared: the row has no trusted configuration context.",
}


def retailer_cell(
    row: Mapping[str, str], code: str, name: str, result: RetailerResult
) -> tuple[Cell, bool]:
    """``(cell, shows_price)`` for one retailer of one row."""
    reason = row.get(f"{code}_Reason", "")
    status = retailer_status(result, reason)
    price = row.get(f"{code}_Price", "")
    where = "Technical detail: hidden tab PILOT_PRICE_COMPARISON."
    if status == "PRICE" and price and row.get(f"{code}_Price_Status") == "SELECTED":
        value = Decimal(price)
        stock = row.get(f"{code}_Stock", "")
        suffix = {"IN_STOCK": "", "OUT_OF_STOCK": OOS_SUFFIX}.get(stock, UNKNOWN_STOCK_SUFFIX)
        stock_words = {
            "IN_STOCK": "in stock",
            "OUT_OF_STOCK": "out of stock (never used for the lowest price)",
        }.get(stock, "stock not verified (never used for the lowest price)")
        basis = (
            " Matched on listed specs (the specific Shopee model is unknown)."
            if row.get(f"{code}_Match_Basis") == "LISTED_SPEC"
            else ""
        )
        observed = row.get(f"{code}_Observed_At", "")
        note = (
            f"{name}: verified matching configuration, {stock_words}.{basis}"
            + (f" Observed {observed} (Asia/Singapore)." if observed else "")
            + " Click the price to open the retailer's page."
        )
        cell = Cell(
            "number",
            price,
            row.get(f"{code}_Offer_URL", ""),
            peso_format(suffix, centavos=_has_centavos(value)),
            note=note,
        )
        return cell, True
    meaning = _STATUS_MEANING.get(status, _STATUS_MEANING[NOT_CHECKED])
    extra = ""
    if result.kind == KIND_ALT:
        extra = (
            f" Only a close alternative ({result.tag}) was found; it is a different configuration, "
            "so its price is not shown."
        )
    if status == REVIEW and search_unsettled(reason):
        extra += (
            " The search hit a result cap or was cut short, so a matching configuration may exist "
            "beyond the results read: check the retailer's site."
        )
    note = f"{name}: {meaning}{extra} Result: {result.one_line}. {where}"
    return text(status, note=note), False


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


def lowest_in_stock(row: Mapping[str, str]) -> LowestOffer | None:
    """The report's lowest in-stock matching offer: its price, retailers (in column order, so a
    tie lists every winner) and a link to the first winner's offer. ``None`` when no matching
    offer is recorded in stock. Out-of-stock and alternative prices never reach it: the report
    builds the minimum from in-stock comparable matching offers only."""
    price = row.get("Lowest_In_Stock_Price", "")
    codes = [c for c in row.get("Lowest_In_Stock_Price_Retailers", "").split(";") if c]
    if not price or not codes:
        return None
    urls = [u for u in row.get("Lowest_In_Stock_Offer_URL", "").split(";")]
    link_of = dict(zip(codes, urls, strict=False)) if len(urls) == len(codes) else {}
    ordered = [(code, name) for code, name in RETAILER_COLUMNS if code in codes]
    link = link_of.get(ordered[0][0], "") if ordered else ""
    return LowestOffer(price, tuple(name for _, name in ordered), link)


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


# Column notes and layout

COLUMN_NOTES = {
    "Rank": "Rank in the Top 100 Shopee variations (existing GMV ranking, unchanged). One row per "
    "Shopee seller variation.",
    "Laptop Model": "Brand, series and model of the mapped laptop configuration.",
    "Variation": "The Shopee variation exactly as listed by the seller.",
    "CPU": "Processor of the mapped configuration (recorded fields only).",
    "Shopee Price": "This variation's own latest Shopee snapshot price from the sales extract, "
    "never a live price. '⚠ Stale' = the snapshot is older than 7 days at the report cutoff; the "
    "gap is still calculated. Click the price to open the Shopee listing.",
    "DataBlitz": "Verified matching configuration price at this retailer. (OOS) = out of stock, "
    "shown but never used for the lowest price. No match = searched, no matching configuration "
    "verified. Review = a candidate needs manual verification. Not checked = search not "
    "completed. Click a price to open the retailer's page. Close alternatives are not shown "
    "as prices.",
    "Lowest Retailer": "Retailer(s) with the lowest verified in-stock matching price; ties list "
    "every retailer. — when no in-stock matching price exists.",
    "Lowest Price": "The lowest verified in-stock matching retailer price. Out-of-stock prices "
    "and similar-but-different configurations are never used.",
    "Price Gap (₱)": "Shopee price minus the lowest price. Positive = Shopee is more expensive.",
    "Price Gap (%)": "The gap as a percentage of the lowest retailer price.",
    "Competitiveness": "Retailer cheaper = Shopee is above the lowest retailer price. Shopee "
    "cheaper = below. Same price = equal. No comparison = no valid in-stock matching retailer "
    "price or no Shopee price.",
}
for _code, _name in RETAILER_COLUMNS[1:]:
    COLUMN_NOTES[_name] = COLUMN_NOTES["DataBlitz"]

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


def shopee_cell(row: Mapping[str, str], *, stale: bool, note: str) -> Cell:
    """The Shopee snapshot price: numeric, linked to the listing, ``⚠ Stale`` beside it (and a soft
    amber fill) when the report marks the snapshot stale. No valid price is ``—``."""
    price = row.get("shopee_price_php", "")
    value = _positive(price)
    if value is None:
        return text(UNAVAILABLE, note=note)
    return Cell(
        "number",
        price,
        row.get("shopee_listing_url", ""),
        peso_format(STALE_SUFFIX if stale else "", centavos=_has_centavos(value)),
        note=note,
        fill="stale" if stale else "",
    )


def competitiveness_cells(
    row: Mapping[str, str], lowest: LowestOffer | None, *, lowest_note: str
) -> list[Cell]:
    """Lowest Retailer, Lowest Price, Price Gap (₱), Price Gap (%), Competitiveness."""
    price = lowest.price if lowest else ""
    result = compare(row.get("shopee_price_php", ""), price)
    names = text(", ".join(lowest.names) if lowest else UNAVAILABLE)
    if lowest is None:
        shown = text(
            UNAVAILABLE,
            note="No verified in-stock matching retailer price. Out-of-stock "
            "prices and other configurations are never used.",
        )
    else:
        shown = Cell(
            "number",
            lowest.price,
            lowest.link,
            peso_format("", centavos=_has_centavos(Decimal(lowest.price))),
            note=lowest_note,
        )
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
