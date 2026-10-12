"""Structured extraction of canonical match fields from retailer offer observations (Phase 8).

Each retailer extractor reads only the labeled ``(label, value)`` specification rows its adapter
keeps in ``OfferDetails.raw_specifications``, plus structured detail fields such as ``brand``.
Titles, configuration summaries, unlabeled bullet lines, variant option titles, and URLs are never
parsed. A field is extracted only when its labeled rows yield exactly one value under the explicit
rules below; otherwise it is left missing with a status that explains why, and the offer goes to
review. Nothing here guesses, fuzzily matches, or assigns a ``target_id``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from .conventions import cpu_series_forms, model_codes, parse_text
from .normalization import CORE_MATCH_COLUMNS
from .offers import OfferObservation, clean_text


class ExtractionStatus(StrEnum):
    EXTRACTED = "EXTRACTED"
    MISSING = "MISSING"  # No labeled evidence for the field.
    AMBIGUOUS = "AMBIGUOUS"  # Labeled evidence yields more than one distinct value.
    UNPARSEABLE = "UNPARSEABLE"  # Labeled evidence exists but no rule recognizes a value.
    NOT_EXTRACTED = "NOT_EXTRACTED"  # Evidence is present but unsafe to use (shared variants).


@dataclass(frozen=True)
class FieldExtraction:
    field: str
    status: ExtractionStatus
    value: str
    source_label: str
    source_text: str


@dataclass(frozen=True)
class ExtractedAttributes:
    """Canonical values (``EXTRACTED`` fields only) plus evidence for every attempted field."""

    retailer_id: str
    extractor: str
    values: Mapping[str, str]
    evidence: tuple[FieldExtraction, ...]
    notes: tuple[str, ...] = ()

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Why core fields were not extracted, e.g. ``AMBIGUOUS_RAM_CAPACITY_GB``."""
        codes = list(self.notes)
        for item in self.evidence:
            if item.field in CORE_MATCH_COLUMNS and item.status is not ExtractionStatus.EXTRACTED:
                codes.append(f"{item.status.value}_{item.field.upper()}")
        return tuple(codes)


@dataclass(frozen=True)
class RetailerLabels:
    """Case-insensitive specification labels observed for one retailer's adapter output."""

    model: frozenset[str]
    cpu: frozenset[str]
    display: frozenset[str]
    memory: frozenset[str]
    storage: frozenset[str]
    # Multi-variant pages share one description across variants (PC Central, Octagon).
    shared_variant_description: bool = False


def _labels(*names: str) -> frozenset[str]:
    return frozenset(name.casefold() for name in names)


# Only labels seen in each adapter's reviewed pages and synthetic fixtures are listed. An unlisted
# label is ignored, so its field stays MISSING until the mapping is reviewed and extended.
RETAILER_LABELS: dict[str, RetailerLabels] = {
    "datablitz": RetailerLabels(
        model=_labels(),
        cpu=_labels("CPU", "Processor"),
        display=_labels("Display"),
        memory=_labels("Memory"),
        storage=_labels("Storage"),
    ),
    "laptop_factory": RetailerLabels(
        model=_labels("Model Name"),
        cpu=_labels("Processor"),
        display=_labels("Display Size"),
        memory=_labels("RAM"),
        storage=_labels("Storage"),
    ),
    "oct": RetailerLabels(
        model=_labels(),
        cpu=_labels("Processor"),
        display=_labels("Display"),
        memory=_labels("Memory"),
        storage=_labels("Storage"),
        shared_variant_description=True,
    ),
    "pcc": RetailerLabels(
        model=_labels("Model"),
        cpu=_labels("Processor"),
        display=_labels("Display"),
        memory=_labels("Memory"),
        storage=_labels("Internal Storage", "Storage"),
        shared_variant_description=True,
    ),
    "pcx": RetailerLabels(
        model=_labels(),
        cpu=_labels("Processor"),
        display=_labels("Display"),
        memory=_labels("Memory"),
        storage=_labels("Storage"),
    ),
}

EXTRACTED_SECONDARY_FIELDS: tuple[str, ...] = (
    "brand",
    "manufacturer_part_number",
    "resolution",
    "display_type",
    "refresh_rate_hz",
)

# Trademark marks are removed from a model value; model-code and CPU rules are in conventions.py.
_MARKS = re.compile("[®™℠]")

# Inch markers: straight or curly double quote, double prime, two single quotes, or "inch".
_SCREEN_SIZE = re.compile(
    r"(?<![0-9.])([0-9]{1,2}(?:\.[0-9]{1,2})?)\s*(?:\"|″|”|''|-?\s*inch(?:es)?\b)", re.I
)
_RESOLUTION = re.compile(r"(?<![0-9])([1-9][0-9]{2,4})\s*[x×]\s*([1-9][0-9]{2,4})(?![0-9])")
_REFRESH_RATE = re.compile(r"(?<![0-9.])([1-9][0-9]{1,2})\s*Hz\b", re.I)
_DISPLAY_TYPE = re.compile(r"\b(IPS-Level|IPS|OLED)\b", re.I)
_DISPLAY_TYPE_FORMS = {"ips-level": "IPS-Level", "ips": "IPS", "oled": "OLED"}

# Capacity layouts such as "2x 8GB", "1 x 8 GB", or "16GB*1" state count and per-module size.
_COUNT_BY_SIZE = re.compile(
    r"(?<![0-9.])([1-9])\s*[x×*]\s*([0-9]+(?:\.[0-9]+)?)\s*(GB|TB)\b", re.I
)
_SIZE_BY_COUNT = re.compile(
    r"(?<![0-9.])([0-9]+(?:\.[0-9]+)?)\s*(GB|TB)\s*[x×*]\s*([1-9])(?![0-9.])", re.I
)
_CAPACITY = re.compile(r"(?<![0-9.])([0-9]+(?:\.[0-9]+)?)\s*(GB|TB)\b", re.I)

# A RAM total followed by its own component breakdown: "DDR5 16GB (8GB on board + 8GB SO-DIMM)".
_BREAKDOWN = re.compile(r"\(([^()]*)\)")
# Words that make a row a range, an alternative, or an upgrade path rather than one fitted total.
_NOT_ONE_TOTAL = re.compile(
    r"\b(?:or|up\s+to|max(?:imum)?|expand\w*|upgrad\w*|support\w*|extra|additional)\b|/", re.I
)

# Size-labeled display rows (LF "Display Size") open with the diagonal. One reviewed row states
# it without a unit ("14.0 display, WUXGA 1920 x 1200"); see _screen_size_row.
_SIZE_LABEL = re.compile(r"\bsize\b", re.I)
_LEADING_DIAGONAL = re.compile(r"([0-9]{2}(?:\.[0-9]{1,2})?)\s+display\b", re.I)
# The same shape anywhere in the row, used only to detect a second stated size. A number glued to
# a preceding digit or decimal point (the "44" of "144 display") is not a separate size.
_STATED_DIAGONAL = re.compile(r"(?<![0-9.])([0-9]{2}(?:\.[0-9]{1,2})?)\s+display\b", re.I)
_LAPTOP_DIAGONAL_INCHES = (Decimal("10"), Decimal("18.4"))


class _Unparseable(ValueError):
    pass


class _MultipleValues(ValueError):
    """Several components or alternatives; ``values`` keeps every mention, repeats included."""

    def __init__(self, values: list[str]) -> None:
        super().__init__()
        self.values = values


_parse_text = parse_text


def _model(text: str) -> set[str]:
    """The labeled value's one model code, or its stated name when it contains no code.

    ``Acer Nitro V 15 ANV15-42-R2UH`` gives ``ANV15-42-R2UH``; the full wording stays in the
    evidence. Two different codes in one value are ambiguous and go to review.
    """
    value = clean_text(_MARKS.sub("", text))
    if not value:
        return set()
    codes = model_codes(value)
    if len(codes) > 1:
        raise _MultipleValues(codes)
    return {codes[0] if codes else value}


def _cpu_series(text: str) -> set[str]:
    return cpu_series_forms(text)


def _screen_size(text: str) -> set[str]:
    values = set()
    for match in _SCREEN_SIZE.finditer(_parse_text(text)):
        number = Decimal(match[1])
        if number > 0:
            values.add(format(number.normalize(), "f"))
    return values


def _inches(number: str) -> str:
    return format(Decimal(number).normalize(), "f")


def _screen_size_row(label: str, text: str) -> set[str]:
    """Inch-marked sizes; else, under a size label only, a leading ``NN[.N] display`` diagonal.

    In a size-labeled row, every stated size is collected first: each inch-marked size and each
    ``NN[.N] display`` diagonal, wherever it appears and whatever separates it (a space, ``/``,
    ``,``, ``;``, ``|``, or parentheses). More than one distinct size is ``AMBIGUOUS``, so an
    alternative such as ``14.0 display/16.0 display`` or ``14" / 16.0 display`` is never read as
    its first size. Repeats of the same size are one size.

    The unit-less form is then accepted only when all of these hold: no inch-marked size is
    present; the row's label names a size (``Display Size``, not a generic ``Display``); the value
    *opens* with the number; the next word is ``display``; and the number is a laptop diagonal
    (10 to 18.4). Resolutions, refresh rates, titles, and numbers elsewhere are never used.
    """
    if not _SIZE_LABEL.search(label):
        return _screen_size(text)
    text = _parse_text(text)
    inch_marked = [
        (match.start(), _inches(match[1]))
        for match in _SCREEN_SIZE.finditer(text)
        if Decimal(match[1]) > 0
    ]
    unit_less = [(match.start(), _inches(match[1])) for match in _STATED_DIAGONAL.finditer(text)]
    distinct = list(dict.fromkeys(value for _, value in sorted(inch_marked + unit_less)))
    if unit_less and len(distinct) > 1:
        raise _MultipleValues(distinct)
    if inch_marked:
        # Inch-marked sizes only: several distinct ones are AMBIGUOUS in _extract_field, as before.
        return {value for _, value in inch_marked}
    match = _LEADING_DIAGONAL.match(text)
    if not match:
        return set()
    number = Decimal(match[1])
    low, high = _LAPTOP_DIAGONAL_INCHES
    return {_inches(match[1])} if low <= number <= high else set()


def _gigabytes(amount: str, unit: str, *, allow_tb: bool) -> int:
    if unit.upper() == "TB" and not allow_tb:
        raise _Unparseable
    try:
        number = Decimal(amount) * (1000 if unit.upper() == "TB" else 1)
    except InvalidOperation as error:
        raise _Unparseable from error
    if number <= 0 or number != number.to_integral_value():
        raise _Unparseable
    return int(number)


def _capacities(text: str, *, allow_tb: bool) -> set[str]:
    """The one total capacity a row states, counting every mention before any deduplication.

    Accepted shapes: a single amount (``512GB``); a single layout (``2 x 512GB``, ``16GB*1``);
    or an amount followed by its own parenthesized layout with the same total
    (``16GB (2x 8GB)``). Anything else, including repeated equal amounts such as
    ``512GB SSD + 512GB HDD`` or ``8GB + 8GB``, raises ``_MultipleValues`` for review.
    """
    text = _parse_text(text)
    layouts: list[tuple[int, int, bool]] = []  # (position, count x size, parenthesized)
    for pattern, count_group, amount_group, unit_group in (
        (_COUNT_BY_SIZE, 1, 2, 3),
        (_SIZE_BY_COUNT, 3, 1, 2),
    ):
        for match in pattern.finditer(text):
            size = _gigabytes(match[amount_group], match[unit_group], allow_tb=allow_tb)
            parenthesized = text[: match.start()].rstrip().endswith("(") and text[
                match.end() :
            ].lstrip().startswith(")")
            layouts.append((match.start(), int(match[count_group]) * size, parenthesized))
        # Blank the layout out at the same length so later positions stay comparable.
        text = pattern.sub(lambda match: " " * len(match[0]), text)
    amounts = [
        (match.start(), _gigabytes(match[1], match[2], allow_tb=allow_tb))
        for match in _CAPACITY.finditer(text)
    ]
    mentions = sorted([(position, total) for position, total, _ in layouts] + amounts)
    if not mentions:
        return set()
    if len(mentions) == 1:
        return {str(mentions[0][1])}
    if (
        len(amounts) == 1
        and len(layouts) == 1
        and layouts[0][2]
        and amounts[0][0] < layouts[0][0]
        and amounts[0][1] == layouts[0][1]
        and "+" not in text
    ):
        return {str(amounts[0][1])}
    raise _MultipleValues([str(total) for _, total in mentions])


def _ram_total_with_breakdown(text: str) -> str | None:
    """The stated total of ``<total> (<amount> <words> + <amount> <words> ...)``, or None.

    Accepted only when the row has exactly one parenthesized group, exactly one amount outside
    it (the total, before the group), at least two ``+``-joined components inside it with exactly
    one amount each, no count-by-size layout, no alternative/upgrade wording, and a component sum
    equal to the total. Anything else returns None and falls back to ``_capacities``, which
    reports several mentions as ambiguous. RAM only: drives are separate components.
    """
    text = _parse_text(text)
    groups = list(_BREAKDOWN.finditer(text))
    if (
        len(groups) != 1
        or text.count("(") != 1
        or text.count(")") != 1
        or _NOT_ONE_TOTAL.search(text)
        or _COUNT_BY_SIZE.search(text)
        or _SIZE_BY_COUNT.search(text)
    ):
        return None
    group = groups[0]
    before, inside, after = text[: group.start()], group[1], text[group.end() :]
    totals = list(_CAPACITY.finditer(before))
    if len(totals) != 1 or _CAPACITY.search(after) or "+" in before + after:
        return None
    parts = inside.split("+")
    amounts = [list(_CAPACITY.finditer(part)) for part in parts]
    if len(parts) < 2 or any(len(found) != 1 for found in amounts):
        return None
    total = _gigabytes(totals[0][1], totals[0][2], allow_tb=False)
    components = [_gigabytes(found[0][1], found[0][2], allow_tb=False) for found in amounts]
    return str(total) if sum(components) == total else None


def _ram(text: str) -> set[str]:
    total = _ram_total_with_breakdown(text)
    return {total} if total is not None else _capacities(text, allow_tb=False)


def _storage(text: str) -> set[str]:
    return _capacities(text, allow_tb=True)


def _resolution(text: str) -> set[str]:
    return {f"{m[1]}x{m[2]}" for m in _RESOLUTION.finditer(_parse_text(text))}


def _refresh_rate(text: str) -> set[str]:
    return {m[1] for m in _REFRESH_RATE.finditer(_parse_text(text))}


def _display_type(text: str) -> set[str]:
    return {_DISPLAY_TYPE_FORMS[m[1].casefold()] for m in _DISPLAY_TYPE.finditer(_parse_text(text))}


def _extract_field(
    field: str,
    rows: list[tuple[str, str]],
    parse: Callable[..., set[str]],
    *,
    required: bool,
    one_row: bool = False,
    with_label: bool = False,
) -> FieldExtraction | None:
    """Apply one parser to the labeled rows; returns ``None`` for an absent optional field.

    ``one_row`` is for capacities: several rows may be several components (two drives, two
    memory rows), so they are ambiguous even when their amounts are equal. ``with_label`` passes
    each row's label to the parser as its first argument.
    """
    label = "; ".join(dict.fromkeys(row_label for row_label, _ in rows))
    text = " | ".join(value for _, value in rows) if one_row else " | ".join(
        dict.fromkeys(value for _, value in rows)
    )
    if not rows:
        if not required:
            return None
        return FieldExtraction(field, ExtractionStatus.MISSING, "", "", "")
    try:
        per_row = [
            parse(row_label, value) if with_label else parse(value) for row_label, value in rows
        ]
    except _Unparseable:
        return FieldExtraction(field, ExtractionStatus.UNPARSEABLE, "", label, text)
    except _MultipleValues as multiple:
        return FieldExtraction(
            field, ExtractionStatus.AMBIGUOUS, "; ".join(multiple.values), label, text
        )
    if one_row and len(rows) > 1:
        mentions = [value for row in per_row for value in sorted(row)]
        return FieldExtraction(field, ExtractionStatus.AMBIGUOUS, "; ".join(mentions), label, text)
    values: set[str] = set().union(*per_row)
    if not values:
        if not required:
            return None  # A display row without a refresh rate is simply silent about it.
        return FieldExtraction(field, ExtractionStatus.UNPARSEABLE, "", label, text)
    if len(values) > 1:
        return FieldExtraction(
            field, ExtractionStatus.AMBIGUOUS, " | ".join(sorted(values)), label, text
        )
    return FieldExtraction(field, ExtractionStatus.EXTRACTED, values.pop(), label, text)


def _has_variant_prefix(offer: OfferObservation, specs: tuple[tuple[str, str], ...]) -> bool:
    """True when the adapter put a selected-variant title before the shared description.

    PC Central and Octagon build ``raw_configuration_text`` as ``<variant title> | <description
    lines>`` for multi-variant products only, so any difference from the plain description join
    means the description may not describe the selected variant.
    """
    description = " | ".join(f"{label}: {value}" if value else label for label, value in specs)
    return clean_text(offer.raw_configuration_text) != clean_text(description)


def extract_offer_attributes(offer: OfferObservation) -> ExtractedAttributes:
    """Extract canonical core and selected secondary fields from one offer's adapter output."""
    labels = RETAILER_LABELS.get(offer.retailer_id)
    if labels is None:
        evidence = tuple(
            FieldExtraction(field, ExtractionStatus.MISSING, "", "", "")
            for field in CORE_MATCH_COLUMNS
        )
        return ExtractedAttributes(
            offer.retailer_id, "", {}, evidence, notes=("NO_RETAILER_EXTRACTOR",)
        )

    details = offer.details
    # Unlabeled lines (headings, "✅ ..." bullets) carry no field label and are never used.
    specs = details.raw_specifications if details is not None else ()
    labeled = [(label, value) for label, value in specs if label and value]

    def rows(names: frozenset[str]) -> list[tuple[str, str]]:
        return [(label, value) for label, value in labeled if label.casefold() in names]

    display_rows = rows(labels.display)
    core_plan: tuple[tuple[str, list[tuple[str, str]], Callable[..., set[str]], bool], ...] = (
        ("model", rows(labels.model), _model, False),
        ("screen_size_in", display_rows, _screen_size_row, False),
        ("cpu_series", rows(labels.cpu), _cpu_series, False),
        ("ram_capacity_gb", rows(labels.memory), _ram, True),
        ("storage_capacity_gb", rows(labels.storage), _storage, True),
    )
    variant_scoped = labels.shared_variant_description and bool(specs)
    shared = variant_scoped and _has_variant_prefix(offer, specs)

    evidence: list[FieldExtraction] = []
    for field, field_rows, parse, one_row in core_plan:
        item = _extract_field(
            field,
            field_rows,
            parse,
            required=True,
            one_row=one_row,
            with_label=parse is _screen_size_row,
        )
        assert item is not None
        if shared and item.status is ExtractionStatus.EXTRACTED:
            # The value may describe another variant of the product, so it cannot be trusted.
            item = FieldExtraction(
                field, ExtractionStatus.NOT_EXTRACTED, "", item.source_label, item.source_text
            )
        evidence.append(item)

    if details is not None:
        for field in ("brand", "manufacturer_part_number"):
            value = getattr(details, field)
            if value:
                evidence.append(
                    FieldExtraction(field, ExtractionStatus.EXTRACTED, value, field, value)
                )
    if not shared:
        for field, parse in (
            ("resolution", _resolution),
            ("display_type", _display_type),
            ("refresh_rate_hz", _refresh_rate),
        ):
            item = _extract_field(field, display_rows, parse, required=False)
            if item is not None:
                evidence.append(item)

    values = {
        item.field: item.value for item in evidence if item.status is ExtractionStatus.EXTRACTED
    }
    notes = ("MULTI_VARIANT_SHARED_DESCRIPTION",) if shared else ()
    return ExtractedAttributes(
        offer.retailer_id, offer.retailer_id, values, tuple(evidence), notes=notes
    )
