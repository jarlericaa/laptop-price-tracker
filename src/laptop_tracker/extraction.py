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
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

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

# Trademark marks are replaced by a space so "Qualcomm®Snapdragon" splits into two words.
_MARKS = re.compile("[®™℠]")

# CPU series rules: (pattern, canonical form). Manufacturer names are omitted from the form.
_CPU_SERIES_RULES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    (re.compile(r"\bCore\s+Ultra\s+([3579])\b", re.I), lambda m: f"Core Ultra {m[1]}"),
    (re.compile(r"\bCore\s+i([3579])\b", re.I), lambda m: f"Core i{m[1]}"),
    (re.compile(r"\bCore\s+([3579])\b", re.I), lambda m: f"Core {m[1]}"),
    (re.compile(r"\bRyzen\s+AI\s+([3579])\b", re.I), lambda m: f"Ryzen AI {m[1]}"),
    (re.compile(r"\bRyzen\s+([3579])\b", re.I), lambda m: f"Ryzen {m[1]}"),
    (
        re.compile(r"\bSnapdragon\s+X(?:\s+(Elite|Plus))?\b(?![-0-9])", re.I),
        lambda m: "Snapdragon X" + (f" {m[1].title()}" if m[1] else ""),
    ),
    (
        re.compile(r"\bApple\s+M([1-9])(?:\s+(Pro|Max|Ultra))?\b", re.I),
        lambda m: f"Apple M{m[1]}" + (f" {m[2].title()}" if m[2] else ""),
    ),
)

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


class _Unparseable(ValueError):
    pass


class _MultipleValues(ValueError):
    """Several components or alternatives; ``values`` keeps every mention, repeats included."""

    def __init__(self, values: list[str]) -> None:
        super().__init__()
        self.values = values


def _parse_text(text: str) -> str:
    return re.sub(r"\s+", " ", _MARKS.sub(" ", unicodedata.normalize("NFC", text))).strip()


def _model(text: str) -> set[str]:
    value = clean_text(_MARKS.sub("", text))
    return {value} if value else set()


def _cpu_series(text: str) -> set[str]:
    text = _parse_text(text)
    return {form(match) for pattern, form in _CPU_SERIES_RULES for match in pattern.finditer(text)}


def _screen_size(text: str) -> set[str]:
    values = set()
    for match in _SCREEN_SIZE.finditer(_parse_text(text)):
        number = Decimal(match[1])
        if number > 0:
            values.add(format(number.normalize(), "f"))
    return values


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


def _ram(text: str) -> set[str]:
    return _capacities(text, allow_tb=False)


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
    parse: Callable[[str], set[str]],
    *,
    required: bool,
    one_row: bool = False,
) -> FieldExtraction | None:
    """Apply one parser to the labeled rows; returns ``None`` for an absent optional field.

    ``one_row`` is for capacities: several rows may be several components (two drives, two
    memory rows), so they are ambiguous even when their amounts are equal.
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
        per_row = [parse(value) for _, value in rows]
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
    core_plan: tuple[tuple[str, list[tuple[str, str]], Callable[[str], set[str]], bool], ...] = (
        ("model", rows(labels.model), _model, False),
        ("screen_size_in", display_rows, _screen_size, False),
        ("cpu_series", rows(labels.cpu), _cpu_series, False),
        ("ram_capacity_gb", rows(labels.memory), _ram, True),
        ("storage_capacity_gb", rows(labels.storage), _storage, True),
    )
    variant_scoped = labels.shared_variant_description and bool(specs)
    shared = variant_scoped and _has_variant_prefix(offer, specs)

    evidence: list[FieldExtraction] = []
    for field, field_rows, parse, one_row in core_plan:
        item = _extract_field(field, field_rows, parse, required=True, one_row=one_row)
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
