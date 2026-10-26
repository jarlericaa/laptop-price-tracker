"""Structured extraction of canonical match fields from retailer offer observations (Phase 8).

Each retailer extractor reads only the labeled ``(label, value)`` specification rows its adapter
keeps in ``OfferDetails.raw_specifications``, plus structured detail fields such as ``brand``.
Paragraph layouts (PC Central, Octagon) are first regrouped into heading/value groups using
recognized semantic headings. A PC Central variant's installed RAM and storage come only from
option values the adapter bound to the selected variant (``SELECTED_VARIANT`` scoped evidence).
Titles, configuration summaries, unlabeled bullet lines, variant option titles, and URLs are never
parsed for matching values. A field is extracted only when its trusted rows yield exactly one value
under the explicit rules below; otherwise it is left missing with a status that explains why, and
the offer goes to review. Explicit product description screen diagonals may supply inches;
shared CPU/screen values require a confirmed complete RAM/storage-only variant matrix.
Other prose, shared fields, identifiers, maximums, and title discrepancies remain supporting
evidence for review and discovery. Nothing here guesses, fuzzily matches, or assigns
a ``target_id``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from urllib.parse import urlsplit

from .conventions import (
    canonical_gpu_models,
    cpu_series_forms,
    is_part_number_shape,
    labeled_cpu_series_forms,
    model_codes,
    normalize_hyphens,
    parse_text,
    processor_models,
    title_identifiers,
    title_model_codes,
)
from .evidence_approvals import read_approvals
from .normalization import CORE_MATCH_COLUMNS
from .offers import (
    ENUMERATED_VARIANT,
    IDENTIFIER,
    PRODUCT_DESCRIPTION,
    SELECTED_VARIANT,
    VARIANT_DEFINITIONS,
    OfferObservation,
    clean_text,
)
from .product_names import default_registry


class ExtractionStatus(StrEnum):
    EXTRACTED = "EXTRACTED"
    MISSING = "MISSING"  # No labeled evidence for the field.
    AMBIGUOUS = "AMBIGUOUS"  # Labeled evidence yields more than one distinct value.
    UNPARSEABLE = "UNPARSEABLE"  # Labeled evidence exists but no rule recognizes a value.
    NOT_EXTRACTED = "NOT_EXTRACTED"  # Evidence is present but unsafe to use (shared variants).
    # Labeled evidence was found, but part of it is unexplained (for example an unknown suffix
    # after a model code). The complete wording is the evidence value; nothing is matched.
    UNRESOLVED = "UNRESOLVED"
    # Supporting evidence only: prose, a shared description, an identifier, a maximum, or a
    # title/specification discrepancy. Kept for review and discovery; never a matching value.
    SUPPORTING = "SUPPORTING"


# Evidence scope (2026-10-19): what a piece of evidence applies to, resolved before any value is
# compared. Only the selected variant, the product's own specification rows, a shared product
# description proven constant across the relevant variants, explicit product prose (screen
# size only), a corroborated title code, and owner-approved interpretations can supply a value.
# Family capabilities, maximums, optional or alternative configurations, URLs, and identifiers
# are kept with their raw wording and locator for review; they never stand for the installed
# configuration.
SCOPE_SELECTED_VARIANT = "SELECTED_VARIANT"
SCOPE_PRODUCT_SPECIFICATION = "PRODUCT_SPECIFICATION"
SCOPE_SHARED_PRODUCT = "SHARED_PRODUCT"
SCOPE_PRODUCT_DESCRIPTION = "PRODUCT_DESCRIPTION"
SCOPE_FAMILY_CAPABILITY = "MODEL_FAMILY_CAPABILITY"
SCOPE_MAXIMUM = "MAXIMUM_SUPPORTED"
SCOPE_OPTIONAL = "OPTIONAL_OR_ALTERNATIVE"
SCOPE_TITLE = "TITLE"
SCOPE_URL = "URL"
SCOPE_IDENTIFIER = "IDENTIFIER"
SCOPE_ENUMERATED_VARIANT = "OTHER_VARIANT"
SCOPE_OWNER_APPROVED = "OWNER_APPROVED"
EVIDENCE_SCOPES: tuple[str, ...] = (
    SCOPE_SELECTED_VARIANT, SCOPE_PRODUCT_SPECIFICATION, SCOPE_SHARED_PRODUCT,
    SCOPE_PRODUCT_DESCRIPTION, SCOPE_FAMILY_CAPABILITY, SCOPE_MAXIMUM, SCOPE_OPTIONAL,
    SCOPE_TITLE, SCOPE_URL, SCOPE_IDENTIFIER, SCOPE_ENUMERATED_VARIANT, SCOPE_OWNER_APPROVED,
)


@dataclass(frozen=True)
class FieldExtraction:
    field: str
    status: ExtractionStatus
    value: str
    source_label: str
    source_text: str
    scope: str = ""


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
        """Why core fields were not extracted, e.g. ``AMBIGUOUS_RAM_CAPACITY_GB``.

        Each core field's primary (first non-supporting) evidence item gives its code. A field
        without an extracted value but with supporting evidence also gets
        ``SUPPORTING_EVIDENCE_<FIELD>``, so a reviewer knows evidence exists outside the trusted
        sources.
        """
        codes = list(self.notes)
        primary: dict[str, FieldExtraction] = {}
        for item in self.evidence:
            if item.field in CORE_MATCH_COLUMNS and item.status is not ExtractionStatus.SUPPORTING:
                primary.setdefault(item.field, item)
        for field in CORE_MATCH_COLUMNS:
            item = primary.get(field)
            if item is not None and item.status is not ExtractionStatus.EXTRACTED:
                codes.append(f"{item.status.value}_{field.upper()}")
        for field in CORE_MATCH_COLUMNS:
            item = primary.get(field)
            if (item is None or item.status is not ExtractionStatus.EXTRACTED) and any(
                e.field == field and e.status is ExtractionStatus.SUPPORTING for e in self.evidence
            ):
                codes.append(f"SUPPORTING_EVIDENCE_{field.upper()}")
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
    # Dedicated model-code fields ("Model: SFG14-75-55MB"). Unlike a model *name* label
    # ("Model Name: Acer Nitro V 15 ANV15-42-R2UH"), any wording beside the one code is
    # unexplained, so the value is UNRESOLVED and kept whole ("A14-11M-X3BP OPI").
    model_code: frozenset[str] = frozenset()
    # Paragraph layouts: a heading line followed by value lines (PC Central, Octagon). Headings
    # are the labels above plus these section headings, which only end a group.
    paragraph_layout: bool = False
    section_headings: frozenset[str] = frozenset()
    # Option names of a bound selected variant that state installed RAM or storage.
    selected_memory: frozenset[str] = frozenset()
    selected_storage: frozenset[str] = frozenset()
    # Supporting labels: colour (title discrepancy check) and retailer part-number rows.
    color: frozenset[str] = frozenset()
    part_number: frozenset[str] = frozenset()

    def headings(self) -> tuple[str, ...]:
        names = (
            self.model | self.model_code | self.cpu | self.display | self.memory | self.storage
            | self.color | self.part_number | self.section_headings
        )
        # Longest first, so "Storage Capability" is recognized before "Storage".
        return tuple(sorted(names, key=lambda name: (-len(name), name)))


def _labels(*names: str) -> frozenset[str]:
    return frozenset(name.casefold() for name in names)


# Only labels seen in each adapter's reviewed pages and synthetic fixtures are listed. An unlisted
# label is ignored, so its field stays MISSING until the mapping is reviewed and extended.
RETAILER_LABELS: dict[str, RetailerLabels] = {
    "datablitz": RetailerLabels(
        model=_labels(),
        # "Chip" (Apple) and "CPU and chipset" (Acer) rows on reviewed 2026-10-19 pages.
        cpu=_labels("CPU", "Processor", "Chip", "CPU and chipset"),
        # "Size" and "Display Panel/ Resolution" rows on reviewed 2026-10-13 pages; ASUS "Panel
        # Size"/"Panel Details" and "Display Size" rows on reviewed 2026-10-19 pages. A screen
        # size still needs an inch marker (or the narrow size-label rule); "Size: 14.0" alone
        # stays UNPARSEABLE. "Dimensions" is not a display label.
        display=_labels("Display", "Size", "Display Panel/ Resolution", "Panel Size",
                        "Panel Details", "Display Size"),
        # "Total System Memory" rows on reviewed 2026-10-15 ASUS pages; "Standard Memory" (Acer)
        # and "Storage Capability" (MSI) / "Storage Capacity" (Acer) rows on reviewed 2026-10-19
        # pages, in a <td><strong> label layout with <th colspan> section rows.
        memory=_labels("Memory", "Total System Memory", "Standard Memory"),
        storage=_labels("Storage", "Storage Capability", "Storage Capacity", "Internal Storage"),
        model_code=_labels("Model"),
        color=_labels("Color"),
        part_number=_labels("Part Number"),
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
        # "CPU" / "RAM" rows and an ASUS-style "Model" row seen on reviewed 2026-10-15 pages.
        cpu=_labels("Processor", "CPU and chipset", "CPU"),
        # "Display Size" / "Panel Size" and HP "Internal Storage" rows on reviewed 2026-10-19
        # pages; "Expansion Slots" ends the previous group.
        display=_labels("Display", "Display Size", "Panel Size"),
        memory=_labels("Memory", "RAM"),
        storage=_labels("Storage", "Internal Storage"),
        model_code=_labels("Model"),
        shared_variant_description=True,
        paragraph_layout=True,
        # Tech Specification section headings seen on reviewed pages (some carry footnote
        # numbers such as "Display4"); they end the previous group.
        section_headings=_labels(
            "Specification", "Operating system", "Graphics", "Audio", "Webcam",
            "Wireless and networking", "Security", "Dimensions and weight",
            "Power adapter and battery", "Input and control", "Windows Desktop Apps",
            "Expansion Slots",
        ),
    ),
    "pcc": RetailerLabels(
        model=_labels("Model"),
        cpu=_labels("Processor", "CPU"),
        display=_labels("Display"),
        memory=_labels("Memory"),
        storage=_labels("Internal Storage", "Storage"),
        shared_variant_description=True,
        paragraph_layout=True,
        section_headings=_labels(
            "Specifications", "Color", "OS", "Graphics", "Storage Capability", "Security",
            "Webcam", "Keyboard", "Communication", "Audio", "Audio Jack", "I/O Ports",
            "Battery", "Adapter", "Dimension (W x D x H)", "Weight (W/Battery)",
        ),
        selected_memory=_labels("RAM", "Memory"),
        selected_storage=_labels("SSD", "Storage", "HDD"),
        color=_labels("Color"),
    ),
    "pcx": RetailerLabels(
        model=_labels(),
        # "Processors" rows on reviewed 2026-10-19 pages.
        cpu=_labels("Processor", "Processors"),
        display=_labels("Display"),
        memory=_labels("Memory"),
        storage=_labels("Storage"),
        # A "Model" row appears on reviewed 2026-10-15 pages; it may hold a full code
        # (SFG14-75-55MB), a base designation (Thin 15 B13UC), or a different one (FA506NC).
        model_code=_labels("Model"),
        color=_labels("Color"),
    ),
}

# Labels that make a row a product model claim on any retailer page. Claims outside a retailer's
# trusted model labels never supply a value; they can only conflict with a title model code.
MODEL_CLAIM_LABELS = _labels("Model", "Model Name", "Model Number", "Model No", "Model No.")

# Match basis tags (balanced policy, 2026-10-15). EXACT keeps its meaning; the basis records how
# the five fields were supported.
TITLE_MODEL_CORROBORATED = "TITLE_MODEL_CORROBORATED"
# Product-name identities (2026-10-19, second review; see product_names.py).
TITLE_PRODUCT_NAME_CORROBORATED = "TITLE_PRODUCT_NAME_CORROBORATED"
TITLE_STATEMENT = "TITLE_STATEMENT"
MODEL_NAME_NORMALIZED = "MODEL_NAME_PRESENTATION_NORMALIZED"
DEDUPLICATED_ROWS = "[equivalent repeated facts deduplicated]"

EXTRACTED_SECONDARY_FIELDS: tuple[str, ...] = (
    "brand",
    "manufacturer_part_number",
    "resolution",
    "display_type",
    "refresh_rate_hz",
)

# Trademark marks are removed from a model value; model-code and CPU rules are in conventions.py.
_MARKS = re.compile("[®™℠]")

# Inch markers: straight or curly double quote, double prime, two single quotes, or "inch"; or
# the abbreviation "in" glued to the number ("14.5in", "15.6in"; 2026-10-15). A spaced "14 in" is
# ordinary English and is not a unit.
_SCREEN_SIZE = re.compile(
    r"(?<![0-9.])([0-9]{1,2}(?:\.[0-9]{1,2})?)"
    # Curly single quotes are also used as inch marks (``15.6’’``, ``14’``; PC Express rows,
    # 2026-10-19, second review).
    r"(?:\s*(?:\"|″|”|''|’’|′′|’|′|-?\s*inch(?:es)?\b)|in\b)",
    re.I,
)
# A display label that states its own unit ("Screen Size (inches)", "Display Size (in)").
_LABEL_INCHES = re.compile(r"\binch(?:es)?\b|\(\s*in\.?\s*\)|\(\s*[\"″”]\s*\)", re.I)
_BARE_DIAGONAL = re.compile(r"\s*([0-9]{2}(?:\.[0-9]{1,2})?)\s*")
_RESOLUTION = re.compile(r"(?<![0-9])([1-9][0-9]{2,4})\s*[x×]\s*([1-9][0-9]{2,4})(?![0-9])")
_REFRESH_RATE = re.compile(r"(?<![0-9.])([1-9][0-9]{1,2})\s*Hz\b", re.I)
_DISPLAY_TYPE = re.compile(r"\b(IPS-Level|IPS|OLED)\b", re.I)
_DISPLAY_TYPE_FORMS = {"ips-level": "IPS-Level", "ips": "IPS", "oled": "OLED"}
# Specification-row labels that state the graphics processor (all retailers, 2026-10-20).
_GPU_LABELS = frozenset({
    "graphics", "gpu", "graphics card", "graphic card", "graphics processor", "video graphics",
    "video card", "discrete graphics", "dedicated graphics", "vga", "graphics & display",
})

# Capacity layouts such as "2x 8GB", "1 x 8 GB", or "16GB*1" state count and per-module size.
# A unit followed by "/s" or "ps" is a transfer rate ("16 Gb/s", "40Gbps"), never a capacity.
_NOT_RATE = r"(?!/s|ps)"
# The count must stand alone: in ``LPDDR5X 16GB`` the ``5X`` is the memory type, not 5 modules
# (2026-10-19, second review: a DataBlitz ASUS row read as 80 GB).
_COUNT_BY_SIZE = re.compile(
    rf"(?<![0-9.A-Za-z])([1-9])\s*[x×*]\s*([0-9]+(?:\.[0-9]+)?)\s*(GB|TB)\b{_NOT_RATE}", re.I
)
_SIZE_BY_COUNT = re.compile(
    rf"(?<![0-9.])([0-9]+(?:\.[0-9]+)?)\s*(GB|TB)\b{_NOT_RATE}\s*[x×*]\s*([1-9])(?![0-9.])",
    re.I,
)
_CAPACITY = re.compile(rf"(?<![0-9.])([0-9]+(?:\.[0-9]+)?)\s*(GB|TB)\b{_NOT_RATE}", re.I)

# Memory and storage clauses (split at ";", " | " and line breaks, or a comma before a scoped
# clause). A maximum clause opens with "up to", "max", or an expandable/upgradable "to"
# (2026-10-19) and is never installed capacity. An optional or alternative clause opens with
# "optional", "option upgrade", "available with", or "alternatively" and is never installed
# capacity either. A parenthesized group that opens the same way and states its own amount
# ("16GB DDR5 (upgradable up to 32GB)") is the same kind of clause.
_MAXIMUM_OPENER = (
    r"(?:supports?\s+)?(?:up\s+to|max(?:imum)?)\b"
    r"|(?:expandable|expandible|upgrad(?:e)?able|extendable|configurable)\s+(?:up\s+)?to\b"
)
_OPTIONAL_OPENER = (
    r"(?:optional(?:ly)?|option(?:al)?\s+upgrades?|available\s+with|alternative(?:ly)?)\b"
)
_MAXIMUM_CLAUSE = re.compile(rf"\s*(?:{_MAXIMUM_OPENER})", re.I)
_OPTIONAL_CLAUSE = re.compile(rf"\s*(?:{_OPTIONAL_OPENER})", re.I)
# A maximum stated inside one flattened row after the installed amount (2026-10-19): DataBlitz
# cells lose their line breaks, so "DDR4-3200<br>8GB RAM<br>Max up to 64GB<br>2 Slots" arrives as
# "DDR4-3200 8GB RAM Max up to 64GB 2 Slots". The phrase must state its own amount ("Max up to
# 64GB"); it is split off only when the wording before it states an amount too, so the installed
# amount is kept and a lone limit is never read as installed capacity.
_INLINE_MAXIMUM = re.compile(
    rf"(?<=\S)\s+((?:max(?:imum)?\s+)?(?:{_MAXIMUM_OPENER})\s*(?:of\s+|capacity\s*:?\s*)?"
    rf"[0-9]+(?:\.[0-9]+)?\s*(?:GB|TB)\b{_NOT_RATE})",
    re.I,
)
_SCOPED_GROUP = re.compile(rf"\(\s*(?:{_MAXIMUM_OPENER}|{_OPTIONAL_OPENER})[^()]*\)", re.I)
# A processor clause that describes the product line's options rather than this processor:
# "(up to Core i7)", "/ configurable to Core i7", ", options up to Ryzen 7".
_CPU_CAPABILITY = re.compile(
    r"\s*(?:(?:supports?\s+)?up\s+to\b|(?:configurable|upgrad(?:e)?able)\s+(?:up\s+)?(?:to|with)\b"
    r"|options?\s+(?:up\s+to|include|available)\b|available\s+(?:with|in|up\s+to)\b"
    r"|optional\b|also\s+available\b)",
    re.I,
)
_SLOTS = re.compile(
    r"(?<![0-9.])([0-9]+)\s*(?:x\s*)?(?:(?:DDR[0-9]X?|SO-?DIMM|DIMM|memory|RAM)\s+){0,3}slots?\b",
    re.I,
)
_FREQUENCY = re.compile(r"(?<![0-9.])([0-9]{3,5})\s*(MT/s|MHz)", re.I)
# Storage evidence without a capacity unit ("1x 512 PCIe Gen4") and transfer rates.
_UNITLESS_DRIVE = re.compile(r"(?<![0-9.])[1-9]\s*[x×]\s*([0-9]{2,4})(?![0-9.]|\s*(?:GB|TB))", re.I)
_THROUGHPUT = re.compile(r"(?<![0-9.])([0-9]+(?:\.[0-9]+)?)\s*(Gb/s|GB/s|Gbps|MB/s)", re.I)

# Processor numbers, used only to report a title/specification discrepancy.
_PROCESSOR_NUMBERS = (
    re.compile(r"\bRyzen\s+(?:AI\s+)?[3579]\s+([0-9]{3,4}[A-Z]{0,3})\b", re.I),
    re.compile(r"(?<![A-Za-z0-9])i[3579]-([0-9]{4,5}[A-Z]{0,2}[0-9]?)(?![A-Za-z0-9])"),
    re.compile(
        r"\bCore\s+(?:Ultra\s+)?[3579]\s*-?\s*(?:processor\s+)?([0-9]{3}[A-Z]{1,2})\b", re.I
    ),
    re.compile(r"\b(X1[EP]?-[0-9]{2}-[0-9]{3})\b", re.I),
)
_COLOR_WORDS = frozenset(
    {"black", "blue", "gray", "grey", "silver", "white", "gold", "green", "red", "pink",
     "purple", "beige", "navy"}
)

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


class _Unresolved(ValueError):
    """Labeled evidence with an unexplained part; ``value`` is the complete wording."""

    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value


_parse_text = parse_text


def _without_part_numbers(text: str) -> str:
    """A model value without documented manufacturer part-number tokens (2026-10-19, second
    review): ``X1504VA / 90NB13Y1-M026R0`` is the model ``X1504VA`` beside a part number, not
    a value with an unexplained remainder. The full wording stays in the evidence."""
    value = clean_text(_MARKS.sub("", text))
    parts = [code for code in model_codes(value) if is_part_number_shape(code)]
    if not parts:
        return value
    for part in parts:
        value = value.replace(part, " ")
    # Only the separators left around the removed part number go; brackets stay paired.
    value = re.sub(r"\(\s*\)|\[\s*\]", " ", value)
    return clean_text(re.sub(r"^[\s/|,;:-]+|[\s/|,;:-]+$", "", value))


def _model(text: str) -> set[str]:
    """The labeled value's one model code, or its stated name when it contains no code.

    ``Acer Nitro V 15 ANV15-42-R2UH`` gives ``ANV15-42-R2UH``; the full wording stays in the
    evidence. Two different codes in one value are ambiguous and go to review.
    """
    value = _without_part_numbers(text)
    if not value:
        return set()
    codes = model_codes(value)
    if len(codes) > 1:
        raise _MultipleValues(codes)
    if codes and _title_code_suffix(value, codes[0]):
        # An unexplained designation right after the code ("A14-11M-X3BP OPI") is never
        # dropped as a family word (2026-10-19), exactly as for code-rule labels and titles.
        raise _Unresolved(value)
    return {codes[0] if codes else value}


def _model_code(text: str) -> set[str]:
    """A dedicated model-code field: exactly one code and nothing else.

    ``SFG14-75-55MB`` gives that code. ``A14-11M-X3BP OPI`` contains one code plus wording
    whose meaning the page does not explain, so it is ``_Unresolved`` with the complete
    wording: never stripped to the code, and never matched. Two codes are ambiguous; a value
    with no code keeps its stated name, as for model-name labels.
    """
    value = _without_part_numbers(text)
    if not value:
        return set()
    codes = model_codes(value)
    if len(codes) > 1:
        raise _MultipleValues(codes)
    if not codes:
        return {value}
    remainder = value.replace(codes[0], " ", 1)
    if re.sub(r"[\s,;:()\[\]/|.-]+", "", remainder):
        raise _Unresolved(value)
    return {codes[0]}


def _cpu_series(text: str) -> set[str]:
    return cpu_series_forms(text)


# A CPU row that opens with "up to" states a product line's best option, not this processor.
_GENERIC_CPU_CLAIM = re.compile(r"(?:supports?\s+)?up\s+to\b", re.I)
_CPU_CLAUSE_BREAK = re.compile(r"(;|\s\|\s|,|\s/\s|\n)")


def _cpu_scopes(text: str) -> tuple[str, list[str]]:
    """``(installed processor wording, family capability clauses)`` for one CPU statement.

    A parenthesized group or a clause (split at ``;``, `` | ``, ``,``, `` / ``, line breaks)
    that opens with capability wording ("up to", "configurable to", "options up to", "available
    with", "optional") describes the product line's options, so ``Intel Core i5-1335U (up to
    Core i7)`` is a Core i5 with a Core i7 family capability, never an ambiguity. Alternatives
    without such wording (``i5-13420H / i7-13620H``) are not split off and stay ambiguous.
    """
    capability: list[str] = []

    def take(match: re.Match[str]) -> str:
        inner = clean_text(match[0][1:-1])
        if _CPU_CAPABILITY.match(inner):
            capability.append(inner)
            return " "
        return match[0]

    stripped = re.sub(r"\([^()]*\)", take, text)
    parts = _CPU_CLAUSE_BREAK.split(stripped)
    kept: list[str] = []
    for index in range(0, len(parts), 2):
        clause = parts[index]
        if index and _CPU_CAPABILITY.match(clause) and cpu_series_forms(clause):
            capability.append(clean_text(clause))
            continue
        if index and kept:
            kept.append(parts[index - 1])
        kept.append(clause)
    if not capability:
        return text, []
    return clean_text("".join(kept)), capability


def _installed_cpu_series(text: str) -> set[str]:
    return labeled_cpu_series_forms(_cpu_scopes(text)[0])


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
    if _LABEL_INCHES.search(label):
        # The label carries the unit, so a bare laptop diagonal is in inches.
        bare = _BARE_DIAGONAL.fullmatch(_parse_text(text))
        if bare:
            low, high = _LAPTOP_DIAGONAL_INCHES
            return {_inches(bare[1])} if low <= Decimal(bare[1]) <= high else set()
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


def _memory_clauses(text: str) -> list[str]:
    # A comma ends a clause only before a maximum or optional clause
    # ("8GB DDR5, Max Capacity:64GB", "8GB, expandable to 32GB").
    return [
        clause
        for clause in re.split(
            rf";|\s\|\s|\n|,(?=\s*(?:{_MAXIMUM_OPENER}|{_OPTIONAL_OPENER}))", text, flags=re.I
        )
        if clause.strip()
    ]


@dataclass(frozen=True)
class _CapacityScopes:
    """A capacity row split by scope: installed wording, maximums, and optional alternatives."""

    installed: str
    maximums: tuple[str, ...]
    optional: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.maximums or self.optional)


def _capacity_scopes(text: str) -> _CapacityScopes:
    """Separate maximum and optional clauses from the installed wording of one row.

    A clause (split at ``;``, `` | ``, line breaks, or a comma before such a clause) or a
    parenthesized group that opens with maximum wording states an upgrade limit, and one that
    opens with optional/alternative wording states another configuration. Neither is installed
    capacity: ``1x 16GB | Up to 32GB`` and ``16GB (upgradable up to 32GB)`` give ``16`` as the
    installed text, and ``Up to 32GB`` alone gives none. A parenthesized group is split off only
    when it states its own amount, so ``512GB SSD + 1TB HDD (optional)`` stays whole and
    ambiguous. A limit inside a breakdown (``16GB (8GB + 8GB up to 32GB)``) is not split off.
    Within one flattened clause, a maximum phrase with its own amount after an installed amount
    is split off too: ``8GB RAM Max up to 64GB 2 Slots`` gives ``8GB RAM ; 2 Slots`` installed
    and the maximum ``Max up to 64GB``.
    """
    maximums: list[str] = []
    optional: list[str] = []

    def take(match: re.Match[str]) -> str:
        group = match[0]
        if not _CAPACITY.search(_parse_text(group)):
            return group
        inner = clean_text(group[1:-1])
        (maximums if _MAXIMUM_CLAUSE.match(inner) else optional).append(inner)
        return " "

    stripped = _SCOPED_GROUP.sub(take, text)
    kept: list[str] = []
    for clause in _memory_clauses(stripped):
        if _MAXIMUM_CLAUSE.match(clause):
            maximums.append(clean_text(clause))
        elif _OPTIONAL_CLAUSE.match(clause) and _CAPACITY.search(_parse_text(clause)):
            optional.append(clean_text(clause))
        else:
            # Outside parentheses only: a limit inside a breakdown is not separable.
            inline = [
                found for found in _INLINE_MAXIMUM.finditer(clause)
                if clause[: found.start()].count("(") == clause[: found.start()].count(")")
            ]
            if inline and _CAPACITY.search(_parse_text(clause[: inline[0].start()])):
                maximums += [clean_text(found[1]) for found in inline]
                for found in reversed(inline):
                    clause = f"{clause[: found.start()]} ; {clause[found.end():]}"
            kept.append(clause)
    if not maximums and not optional:
        return _CapacityScopes(text, (), ())
    return _CapacityScopes(
        clean_text(" ; ".join(kept)), tuple(dict.fromkeys(maximums)), tuple(dict.fromkeys(optional))
    )


def _installed_memory_text(text: str) -> str | None:
    """The capacity text without its maximum and optional clauses, or None when it has none."""
    scopes = _capacity_scopes(text)
    return scopes.installed if scopes.changed else None


# Wording that makes a "+" row something other than installed components: an optional or
# external part, an empty slot, or an expansion path.
_NOT_INSTALLED = re.compile(
    r"\b(?:optional|external|expansion|expandable|free|empty|available|vacant|slots?|"
    r"accessor\w*|bundle\w*|portable)\b",
    re.I,
)
_STORAGE_MEDIA = (
    ("SSD", re.compile(r"\b(?:SSD|SDD|NVMe|M\.2|PCIe)\b", re.I)),
    ("HDD", re.compile(r"\b(?:HDD|hard\s+(?:disk|drive))\b", re.I)),
    ("eMMC", re.compile(r"\beMMC\b", re.I)),
    ("UFS", re.compile(r"\bUFS\b", re.I)),
)
_RAM_PLACEMENT = (
    ("ONBOARD", re.compile(r"\b(?:on\s*-?\s*board|soldered)\b", re.I)),
    ("SO-DIMM", re.compile(r"\bSO-?DIMM\b", re.I)),
)


def _installed_components(text: str, *, allow_tb: bool) -> list[tuple[int, str]] | None:
    """``[(GB, component text), ...]`` for an explicit ``a + b`` list of installed parts.

    Accepted only when the text has no parentheses, at least two ``+``-joined parts, exactly one
    amount with a unit in each part, no count-by-size layout, and none of the alternative,
    maximum, upgrade, optional, external, or slot wording. Otherwise ``None``.
    """
    text = _parse_text(text)
    if (
        "(" in text or ")" in text
        or _NOT_ONE_TOTAL.search(text)
        or _NOT_INSTALLED.search(text)
        or _COUNT_BY_SIZE.search(text)
        or _SIZE_BY_COUNT.search(text)
    ):
        return None
    parts = [part.strip() for part in text.split("+")]
    if len(parts) < 2 or not all(parts):
        return None
    components = []
    for part in parts:
        found = list(_CAPACITY.finditer(part))
        if len(found) != 1:
            return None
        components.append((_gigabytes(found[0][1], found[0][2], allow_tb=allow_tb), part))
    return components


def _ram_components_sum(text: str) -> str | None:
    """The total of explicit installed memory parts (``8GB onboard + 8GB SO-DIMM`` -> 16)."""
    components = _installed_components(text, allow_tb=False)
    return None if components is None else str(sum(size for size, _ in components))


def _storage_media(text: str) -> list[str]:
    return [name for name, pattern in _STORAGE_MEDIA if pattern.search(text)]


def _storage_components(text: str) -> list[tuple[int, str]] | None:
    """Installed drives of ``512GB SSD + 512GB HDD``; the row must name a drive medium."""
    components = _installed_components(text, allow_tb=True)
    if components is None or not _storage_media(_parse_text(text)):
        return None
    return components


def _ram(text: str) -> set[str]:
    installed = _installed_memory_text(text)
    text = text if installed is None else installed
    total = _ram_total_with_breakdown(text)
    if total is None:
        total = _ram_components_sum(text)
    return {total} if total is not None else _capacities(text, allow_tb=False)


def _memory_details(text: str) -> dict[str, list[str]]:
    """Supporting memory facts kept apart from installed capacity: stated amounts outside
    maximum and optional clauses, maximums, optional amounts, slot counts, and frequencies."""
    details: dict[str, list[str]] = {
        "stated": [], "maximum": [], "optional": [], "slots": [], "frequency": [],
    }
    scopes = _capacity_scopes(text)
    clauses = [
        *(("stated", clause) for clause in _memory_clauses(scopes.installed)),
        *(("maximum", clause) for clause in scopes.maximums),
        *(("optional", clause) for clause in scopes.optional),
    ]
    for bucket, clause in clauses:
        parsed = _parse_text(clause)
        for match in _FREQUENCY.finditer(parsed):
            details["frequency"].append(f"{match[1]} {match[2]}")
        parsed = _FREQUENCY.sub(" ", parsed)
        for match in _SLOTS.finditer(parsed):
            details["slots"].append(match[1])
        for match in _CAPACITY.finditer(parsed):
            if match[2].upper() == "GB":
                details[bucket].append(format(Decimal(match[1]).normalize(), "f"))
    return {key: list(dict.fromkeys(values)) for key, values in details.items()}


def _storage(text: str) -> set[str]:
    """One stated drive capacity, or the total of explicitly installed drives.

    ``512GB SSD + 512GB HDD`` gives ``1024`` (1 TB = 1000 GB; drives are never collapsed into one
    medium). The layout and media are recorded separately as supporting evidence. Maximum and
    optional clauses are removed first (``512GB SSD | Up to 2TB`` gives ``512``).
    """
    scopes = _capacity_scopes(text)
    if scopes.changed:
        text = scopes.installed
    try:
        return _capacities(text, allow_tb=True)
    except _MultipleValues:
        components = _storage_components(text)
        if components is None:
            raise
        return {str(sum(size for size, _ in components))}


def _component_signature(field: str, text: str) -> tuple[str, ...]:
    """What a capacity row describes, outside any parenthesized breakdown: drive media for
    storage, placement (onboard / SO-DIMM) for memory. Equal capacities with equal signatures
    are one repeated fact; different signatures may be separate components."""
    outside = _BREAKDOWN.sub(" ", _parse_text(text))
    if field == "storage_capacity_gb":
        return tuple(_storage_media(outside))
    return tuple(name for name, pattern in _RAM_PLACEMENT if pattern.search(outside))


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
    except _Unresolved as unresolved:
        return FieldExtraction(field, ExtractionStatus.UNRESOLVED, unresolved.value, label, text)
    except _MultipleValues as multiple:
        return FieldExtraction(
            field, ExtractionStatus.AMBIGUOUS, "; ".join(multiple.values), label, text
        )
    if one_row and len(rows) > 1:
        mentions = [value for row in per_row for value in sorted(row)]
        signatures = {_component_signature(field, value) for _, value in rows}
        if len(set(mentions)) == 1 and len(mentions) == len(rows) and len(signatures) == 1:
            # The same installed amount stated in several equivalent rows is one fact, never a
            # sum or an ambiguity (balanced policy, 2026-10-15).
            return FieldExtraction(
                field, ExtractionStatus.EXTRACTED, mentions[0],
                f"{label} {DEDUPLICATED_ROWS}", text,
            )
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


def _heading(line: str, headings: tuple[str, ...]) -> tuple[str, str] | None:
    """``(heading as written, inline value)`` when ``line`` opens with a recognized heading.

    Accepted forms: the heading alone, optionally with a footnote marker glued to it
    (``Display4``, ``Memory1, 2, 3``); the heading, a colon, and its value
    (``Memory: 16GB``); or a footnoted heading followed by its value on the same line
    (``CPU and chipset1 AMD Ryzen 5 6600H``). Words after an unfootnoted heading without a
    colon (``Memory Frequency ...``) are not a heading. Footnote markers are removed only from
    a recognized heading, never from values (``16:9`` and ``1x 16GB`` stay intact).
    """
    for heading in headings:
        match = re.match(
            rf"({re.escape(heading)})(?P<fn>[0-9]+(?:\s*,\s*[0-9]+)*)?(?P<rest>.*)\Z",
            line,
            re.I | re.S,
        )
        if not match:
            continue
        rest = match["rest"]
        if not rest.strip():
            return match[1], ""
        if rest.lstrip().startswith(":"):
            return match[1], clean_text(rest.lstrip()[1:])
        if match["fn"] and rest[:1].isspace():
            return match[1], clean_text(rest)
    return None


def _paragraph_groups(
    specs: tuple[tuple[str, str], ...], headings: tuple[str, ...]
) -> list[tuple[str, str]]:
    """Heading/value groups from a paragraph layout, as ``(heading, value lines)``.

    Each raw row is one source line (``label: value`` when the adapter split it at a colon).
    A heading with an inline value is a one-line group, as a labeled row always was. A heading
    alone opens a group that collects the following lines, across paragraph and line breaks,
    until the next recognized heading. Lines before the first heading belong to no group.
    """
    groups: list[tuple[str, list[str]]] = []
    open_values: list[str] | None = None
    for label, value in specs:
        # The adapter split each line at its first colon; a digit on both sides was a ratio
        # such as "16:9", so it is rejoined without a space.
        glue = ":" if label[-1:].isdigit() and value[:1].isdigit() else ": "
        line = clean_text(f"{label}{glue}{value}" if value else label)
        if not line:
            continue
        found = _heading(line, headings)
        if found is not None:
            name, inline = found
            values = [inline] if inline else []
            groups.append((name, values))
            open_values = None if inline else values
        elif open_values is not None:
            open_values.append(line)
    return [(name, " | ".join(values)) for name, values in groups if values]


def _selected_variant(
    scoped: tuple[tuple[str, str, str], ...],
) -> tuple[str, str, list[tuple[str, str]]]:
    """``(binding status, variant id, option rows)`` from the adapter's scoped evidence."""
    binding, variant_id, options = "", "", []
    for scope, label, value in scoped:
        if scope != SELECTED_VARIANT:
            continue
        if label == "binding status":
            binding = value
        elif label == "variant id":
            variant_id = value
        else:
            options.append((label, value))
    return binding, variant_id, options



def _shared_cpu_display_scope(
    scoped: tuple[tuple[str, str, str], ...], labels: RetailerLabels,
    variant_id: str, selected: list[tuple[str, str]],
) -> str:
    """Locator of a complete, bound RAM/storage-only matrix, or blank when unproven.

    Variant titles must describe their option values only. They are checked for conflicts,
    never mined for a model or other matching value.
    """
    matrices = [(label, value) for scope, label, value in scoped if scope == VARIANT_DEFINITIONS]
    if len(matrices) != 1:
        return ""
    locator, text = matrices[0]
    try:
        matrix = json.loads(text)
        if not isinstance(matrix, dict) or matrix.get("selected_id") != variant_id:
            return ""
        names, variants = matrix.get("option_names"), matrix.get("variants")
        if not isinstance(names, list) or names != [name for name, _ in selected]:
            return ""
        if not isinstance(variants, list) or len(variants) < 2:
            return ""
        if any(not isinstance(name, str) for name in names):
            return ""
        allowed = labels.selected_memory | labels.selected_storage
        if (
            not names or len(set(names)) != len(names)
            or any(n.casefold() not in allowed for n in names)
        ):
            return ""
        ids: set[str] = set()
        selected_found = False
        for variant in variants:
            if not isinstance(variant, dict):
                return ""
            identity = variant.get("id")
            options, title = variant.get("options"), variant.get("title")
            if not isinstance(identity, str) or not identity or identity in ids:
                return ""
            ids.add(identity)
            if not isinstance(options, list) or len(options) != len(names):
                return ""
            if not all(isinstance(value, str) and value.strip() for value in options):
                return ""
            if not isinstance(title, str) or clean_text(title) != " / ".join(options):
                return ""
            for name, value in zip(names, options, strict=True):
                parse = _ram if name.casefold() in labels.selected_memory else _storage
                if len(parse(value)) != 1 or _cpu_series(value) or _screen_size(value):
                    return ""
            if identity == variant_id:
                selected_found = options == [value for _, value in selected]
        return locator if selected_found else ""
    except (ValueError, TypeError, _Unparseable, _MultipleValues):
        return ""


def _prose_screen_rows(
    scoped: tuple[tuple[str, str, str], ...],
) -> list[tuple[str, str, set[str]]]:
    """Explicit product diagonals, with original source text and locator.

    A nearby laptop/display noun is required. Accessory compatibility, external screens,
    alternatives and comparisons are not a statement of this product's diagonal.
    """
    result = []
    low, high = _LAPTOP_DIAGONAL_INCHES
    unrelated = re.compile(
        r"\b(?:fits?|compatible|external|monitor|bag|backpack|sleeve|case|other|another|"
        r"compared|versus|previous|older|optional|optionally|available\s+in|up\s+to)\b", re.I
    )
    noun = re.compile(r"\s*(?:-\s*|sized\s+)?(?:laptop|notebook|screen|display|panel)\b", re.I)
    before = re.compile(
        r"\b(?:screen|display|panel|diagonal)"
        r"(?:\s+(?:size|of|is|measures|a|an)){0,3}\s*[:=-]?\s*$", re.I
    )
    for scope, label, text in scoped:
        if scope != PRODUCT_DESCRIPTION:
            continue
        sizes: set[str] = set()
        for sentence in re.split(r"(?<=[.!?])\s+", _parse_text(text)):
            if unrelated.search(sentence):
                continue
            matches = list(_SCREEN_SIZE.finditer(sentence))
            for match in matches:
                if low <= Decimal(match[1]) <= high and (
                    noun.match(sentence[match.end():]) or before.search(sentence[:match.start()])
                ):
                    # Several inch dimensions in the same product sentence cannot be narrowed
                    # to the first one; all are retained so conflicts require review.
                    sizes.update(_inches(m[1]) for m in matches if low <= Decimal(m[1]) <= high)
        if sizes:
            result.append((label, text, sizes))
    return result


def _screen_with_prose(
    item: FieldExtraction, prose: list[tuple[str, str, set[str]]],
    display_rows: list[tuple[str, str]],
) -> FieldExtraction:
    """Prose supplies units; a unitless spec row only checks consistency."""
    if not prose or item.status is ExtractionStatus.AMBIGUOUS:
        return item
    sizes = set().union(*(values for _, _, values in prose))
    if item.status is ExtractionStatus.EXTRACTED:
        sizes.add(item.value)
    for _, text in display_rows:
        # A leading bare diagonal cannot establish inches, but must not contradict them.
        for match in re.finditer(
            r"(?:^|[|/,;+]|\bor\b)\s*([0-9]{2}(?:\.[0-9]{1,2})?)(?=\s|$|[,/;|+])",
            _parse_text(text)
        ):
            if _LAPTOP_DIAGONAL_INCHES[0] <= Decimal(match[1]) <= _LAPTOP_DIAGONAL_INCHES[1]:
                sizes.add(_inches(match[1]))
        sizes.update(_screen_size(text))
        for metric in re.finditer(
            r"(?<![0-9.])([0-9]+(?:\.[0-9]+)?)\s*(cm|mm)\b", _parse_text(text), re.I
        ):
            divisor = Decimal("2.54") if metric[2].lower() == "cm" else Decimal("25.4")
            sizes.add(format((Decimal(metric[1]) / divisor).normalize(), "f"))
    source_label = (
        "; ".join(label for label, _, _ in prose) + " [PRODUCT_DESCRIPTION; explicit inches]"
    )
    source_text = " | ".join(dict.fromkeys(text for _, text, _ in prose))
    if len(sizes) != 1:
        return FieldExtraction(
            "screen_size_in", ExtractionStatus.AMBIGUOUS, " | ".join(sorted(sizes)),
            "; ".join(filter(None, (item.source_label, source_label))),
            " | ".join(filter(None, (item.source_text, source_text))),
        )
    if item.status is ExtractionStatus.EXTRACTED:
        return item
    return FieldExtraction(
        "screen_size_in", ExtractionStatus.EXTRACTED, sizes.pop(), source_label, source_text
    )



def _apply_owner_approvals(
    offer: OfferObservation, evidence: list[FieldExtraction],
    supporting: list[FieldExtraction], notes: list[str],
) -> None:
    """Apply explicit owner interpretations to their exact listing and observed configuration.

    Page text stays immutable. This may supply a reviewed model/unit interpretation, never a
    target ID. Explicit conflicting specifications and different variants are not overridden.
    """
    primary = {item.field: item for item in evidence}
    for rule in read_approvals():
        if (rule["retailer_id"], rule["offer_url"]) != (offer.retailer_id, offer.offer_url):
            continue
        if any(
            primary[field].status is not ExtractionStatus.EXTRACTED
            or primary[field].value != rule[field]
            for field in ("cpu_series", "screen_size_in", "ram_capacity_gb")
        ):
            continue
        if rule["required_part_number"] and not any(
            item.field == "part_number_candidate" and item.value == rule["required_part_number"]
            for item in supporting
        ):
            continue
        field = rule["field"]
        item = primary[field]
        if field == "model":
            # Existing explicit model evidence must be precisely the approved designation.
            if item.status is ExtractionStatus.UNRESOLVED:
                if (
                    item.value != rule["observed_value"]
                    or clean_text(item.source_text) != rule["observed_value"]
                ):
                    continue
                source = item.source_text
            elif item.status is ExtractionStatus.MISSING:
                # This title is checked only as the recorded owner's listing-specific claim.
                # Titles elsewhere remain discovery-only, even for the same model token.
                if not re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(rule['observed_value'])}(?![A-Za-z0-9])",
                    offer.raw_title,
                ) or model_codes(offer.raw_title) != model_codes(rule["observed_value"]):
                    continue
                source = offer.raw_title
            else:
                continue
        else:
            if item.status is not ExtractionStatus.UNPARSEABLE:
                continue
            mentions = list(_UNITLESS_DRIVE.finditer(_parse_text(item.source_text)))
            if len(mentions) != 1 or mentions[0][1] != rule["observed_value"]:
                continue
            match = mentions[0]
            text = _parse_text(item.source_text)
            amended = text[:match.end(1)] + "GB" + text[match.end(1):]
            try:
                if _storage(amended) != {rule["approved_value"]}:
                    continue
            except (_Unparseable, _MultipleValues):
                continue
            source = item.source_text
        supporting.append(FieldExtraction(
            field, ExtractionStatus.SUPPORTING, rule["observed_value"],
            f"{item.source_label or 'title'} [original evidence before owner decision]", source,
        ))
        provenance = (
            f"OWNER_APPROVED; {rule['decision_id']}; {rule['approved_by']}; {rule['approved_on']}"
        )
        replacement = FieldExtraction(
            field, ExtractionStatus.EXTRACTED, rule["approved_value"], provenance, source,
        )
        evidence[evidence.index(item)] = replacement
        primary[field] = replacement
        notes.append(f"OWNER_APPROVED_{field.upper()}")
        if field == "storage_capacity_gb":
            notes.append("PAGE_STORAGE_UNIT_MISSING_OWNER_APPROVED_GB")
            if "STORAGE_CAPACITY_UNIT_MISSING" in notes:
                notes.remove("STORAGE_CAPACITY_UNIT_MISSING")


# Title-model corroboration (balanced policy, 2026-10-15).

# Ordinary title words that may follow a model code without being a designation suffix. Same
# idea as discovery's retained-suffix rule; anything else short and alphabetic ("OPI") is an
# unexplained designation and keeps the model unresolved.
_TITLE_WORDS_NOT_SUFFIX = frozenset(
    {"LAPTOP", "NOTEBOOK", "GAMING", "OLED", "IPS", "FHD", "QHD", "UHD", "WUXGA", "WIN", "RAM",
     "SSD", "HDD", "AI", "PC", "INTEL", "AMD", "CORE", "RYZEN", "ULTRA", "RTX", "GTX", "WITH",
     "AND", "NEW", "BLACK", "BLUE", "GRAY", "GREY", "SILVER", "WHITE", "GOLD", "GREEN", "RED",
     "PINK", "BEIGE", "NAVY", "ROSE", "MATTE", "STEAM", "STEEL", "COOL", "DARK", "LIGHT"}
)
_TITLE_SEGMENT_BREAK = re.compile(r"[|/,()\[\]]")
# Title capacity roles (2026-10-19, independent review): the words right after an amount say
# what it is. An optional parenthesized breakdown may sit between the amount and its role words
# ("16GB (2x8GB) DDR5 RAM").
_TITLE_RAM_ROLE = re.compile(
    r"\s*(?:\([^()]*\)\s*)?(?:(?:LP)?DDR[0-9]X?\b(?:\s*-?\s*[0-9]{4}\s*(?:MHz|MT/s)?)?\s*)?"
    r"(?:unified\s+)?(?:RAM|memory)\b"
    r"|\s*(?:\([^()]*\)\s*)?(?:LP)?DDR[0-9]X?\b",
    re.I,
)
_TITLE_STORAGE_ROLE = re.compile(
    r"\s*(?:\([^()]*\)\s*)?(?:(?:PCIe|NVMe|M\.2|Gen\s*[0-9])\s*)*"
    r"(?:SSD|SDD|HDD|eMMC|UFS|storage)\b",
    re.I,
)
# A drive that is not installed in the laptop: "256GB external SSD", "1TB portable HDD".
_TITLE_EXTERNAL_ROLE = re.compile(
    r"\s*(?:external|portable|removable|USB)\s+(?:(?:PCIe|NVMe|M\.2)\s*)*"
    r"(?:SSD|HDD|drive|hard\s+(?:disk|drive)|storage|flash\s+drive)\b",
    re.I,
)
# Wording directly before an amount that makes it an accessory or extra, not installed:
# "FREE 256GB SSD", "+ extra 8GB RAM", "bonus 1TB HDD".
_TITLE_NOT_INSTALLED_BEFORE = re.compile(
    r"\b(?:free|freebie|bonus|external|portable|optional|extra|additional|spare)\s*[:\-]?\s*$",
    re.I,
)
_TITLE_JOIN = re.compile(r"\s*[+/]\s*")
_TITLE_PLUS = re.compile(r"\s*\+\s*")
_RAM, _STORAGE, _UNKNOWN = "RAM", "STORAGE", "UNKNOWN"


@dataclass
class _TitleMention:
    """One capacity amount in a title, before any deduplication."""

    start: int
    amount_end: int
    gigabytes: int
    role: str = _UNKNOWN
    words_end: int = 0
    installed: bool = True
    unclear: bool = False


@dataclass(frozen=True)
class TitleCapacity:
    """What a title states about one capacity field.

    ``value`` is the one installed total (a single mention, or one ``+``-joined component list
    summed under the established component rule); ``ambiguous`` is set when the title states
    several separate amounts, a breakdown that does not add up, or an amount joined to an
    unexplained one. ``mentions`` keeps every installed mention as written (repeated equal
    components included); ``not_installed`` keeps external, extra or free amounts.
    """

    value: str
    values: frozenset[str]
    ambiguous: bool
    mentions: tuple[str, ...]
    not_installed: tuple[str, ...]


def _title_mentions(text: str) -> list[_TitleMention]:
    """Every capacity amount in ``text`` (layouts as count x size), with its role and scope."""
    found: list[_TitleMention] = []
    blanked = text
    for pattern, count_group, amount_group, unit_group in (
        (_COUNT_BY_SIZE, 1, 2, 3),
        (_SIZE_BY_COUNT, 3, 1, 2),
    ):
        for match in pattern.finditer(blanked):
            try:
                size = _gigabytes(match[amount_group], match[unit_group], allow_tb=True)
            except _Unparseable:
                continue
            found.append(_TitleMention(match.start(), match.end(), int(match[count_group]) * size))
        blanked = pattern.sub(lambda match: " " * len(match[0]), blanked)
    for match in _CAPACITY.finditer(blanked):
        try:
            size = _gigabytes(match[1], match[2], allow_tb=True)
        except _Unparseable:
            continue
        found.append(_TitleMention(match.start(), match.end(), size))
    found.sort(key=lambda mention: mention.start)

    # A parenthesized breakdown right after an amount ("16GB (2x8GB)", "16GB (8GB + 8GB)") is
    # that amount's components: consumed when they add up, otherwise the amount is unclear.
    consumed: set[int] = set()
    for group in _BREAKDOWN.finditer(text):
        inner = [m for m in found if group.start() < m.start and m.amount_end <= group.end()]
        before = [m for m in found if m.amount_end <= group.start()
                  and not text[m.amount_end:group.start()].strip()]
        if not inner or not before:
            continue
        consumed.update(id(m) for m in inner)
        if sum(m.gigabytes for m in inner) != before[-1].gigabytes:
            before[-1].unclear = True
    mentions = [m for m in found if id(m) not in consumed]

    for mention in mentions:
        mention.words_end = mention.amount_end
        for role, pattern, installed in (
            (_STORAGE, _TITLE_EXTERNAL_ROLE, False),
            (_RAM, _TITLE_RAM_ROLE, True),
            (_STORAGE, _TITLE_STORAGE_ROLE, True),
        ):
            words = pattern.match(text, mention.amount_end)
            if words:
                mention.role, mention.installed, mention.words_end = role, installed, words.end()
                break
        if _TITLE_NOT_INSTALLED_BEFORE.search(text[max(0, mention.start - 30):mention.start]):
            mention.installed = False

    def joined(left: _TitleMention, right: _TitleMention, join: re.Pattern[str]) -> bool:
        return bool(join.fullmatch(text[left.words_end:right.start]))

    # The joined RAM+storage form ("8GB+256GB", "16GB/ 1TB SSD"): an unlabeled memory-sized
    # amount (at most 128GB) joined to an installed storage-sized one (at least 128GB), and not
    # part of a longer list ("8GB/16GB/32GB", "8GB+256GB/512GB").
    for index in range(len(mentions) - 1):
        first, second = mentions[index], mentions[index + 1]
        if (
            first.role != _UNKNOWN or not first.installed or not second.installed
            or second.role not in {_UNKNOWN, _STORAGE}
            or not joined(first, second, _TITLE_JOIN)
            or (index and joined(mentions[index - 1], first, _TITLE_JOIN))
            or (index + 2 < len(mentions) and joined(second, mentions[index + 2], _TITLE_JOIN))
        ):
            continue
        if 0 < first.gigabytes <= 128 <= second.gigabytes:
            first.role, second.role = _RAM, _STORAGE
    # An unexplained amount joined to a labeled one ("8GB + 8GB RAM", "8GB/16GB RAM") leaves
    # that labeled field's structure unclear.
    for left, right in zip(mentions, mentions[1:], strict=False):
        if joined(left, right, _TITLE_JOIN) and {left.role, right.role} - {_UNKNOWN} and (
            _UNKNOWN in {left.role, right.role}
        ):
            for mention in (left, right):
                mention.unclear = mention.unclear or mention.role != _UNKNOWN
    return mentions


def _title_capacities(text: str) -> dict[str, TitleCapacity]:
    """RAM and storage as a title states them (``TitleCapacity``), from installed wording only:
    maximum and optional clauses are removed first (``_capacity_scopes``)."""
    installed_text = _capacity_scopes(text).installed
    mentions = _title_mentions(installed_text)
    result: dict[str, TitleCapacity] = {}
    for field, role in (("ram_capacity_gb", _RAM), ("storage_capacity_gb", _STORAGE)):
        own = [m for m in mentions if m.role == role]
        installed = [m for m in own if m.installed]
        chains: list[list[_TitleMention]] = []
        for mention in installed:
            between = installed_text[chains[-1][-1].words_end:mention.start] if chains else ""
            if chains and _TITLE_PLUS.fullmatch(between):
                chains[-1].append(mention)  # "+"-joined installed components
            else:
                chains.append([mention])
        totals = [str(sum(m.gigabytes for m in chain)) for chain in chains]
        ambiguous = len(chains) > 1 or any(m.unclear for m in installed)
        result[field] = TitleCapacity(
            value=totals[0] if len(chains) == 1 and not ambiguous else "",
            values=frozenset(totals),
            ambiguous=ambiguous,
            mentions=tuple(clean_text(installed_text[m.start:m.words_end]) for m in installed),
            not_installed=tuple(
                clean_text(installed_text[m.start:m.words_end]) for m in own if not m.installed
            ),
        )
    return result
# A designation token inside a model claim that has no full code: letters and digits, 5+ long.
_DESIGNATION = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])"
                          r"[A-Za-z0-9]{5,}(?![A-Za-z0-9])")


def _title_code_suffix(title: str, code: str) -> str:
    """An unexplained designation directly after ``code`` in the same title segment."""
    for segment in _TITLE_SEGMENT_BREAK.split(normalize_hyphens(_MARKS.sub(" ", title))):
        words = segment.split()
        upper = [word.upper() for word in words]
        if code.upper() in upper:
            index = upper.index(code.upper())
            if index + 1 < len(words):
                word = words[index + 1]
                if re.fullmatch(r"[A-Za-z]{2,5}", word) and word.upper() not in (
                    _TITLE_WORDS_NOT_SUFFIX
                ):
                    return word
    return ""


def _model_claim_relation(claim: str, code: str) -> str:
    """How a labeled model claim relates to a full title code.

    ``AGREES`` (the same full code), ``LESS_SPECIFIC`` (only designations equal to whole
    segments of the code, e.g. ``Thin 15 B13UC`` for ``B13UC-3474PH``), ``NAME_ONLY`` (no code
    or designation, e.g. ``ASUS Vivobook S16``), or ``CONFLICT`` (a different code or
    designation, e.g. ``FA506NC`` for ``FA506NCQ-HN012W``). No substring or fuzzy comparison.
    """
    codes, _ = title_model_codes(claim)
    if codes:
        # The same code followed by an unexplained designation ("... OPI") is not agreement.
        same = [c.casefold() for c in codes] == [code.casefold()]
        return "AGREES" if same and not _title_code_suffix(claim, codes[0]) else "CONFLICT"
    tokens = _DESIGNATION.findall(_parse_text(_MARKS.sub(" ", claim)))
    if not tokens:
        return "NAME_ONLY"
    segments = {segment.casefold() for segment in code.split("-")}
    return "LESS_SPECIFIC" if all(t.casefold() in segments for t in tokens) else "CONFLICT"


def _url_model_conflict(code: str, url: str) -> str:
    """A different final code segment in the URL path after the same stem, or ''.

    ``anv15-42-r2auh`` conflicts with ``ANV15-42-R2UH``. The URL never supplies a model; this
    is a conflict check only.
    """
    segments = code.casefold().split("-")
    if len(segments) < 2:
        return ""
    path = urlsplit(url).path.casefold()
    stem = "-".join(segments[:-1]) + "-"
    for match in re.finditer(re.escape(stem) + r"([a-z0-9]+)", path):
        if match.start() and path[match.start() - 1].isalnum():
            continue
        if match[1] != segments[-1]:
            return stem + match[1]
    return ""


def _url_slug_differs(
    offer: OfferObservation, model: str, supporting: list[FieldExtraction], notes: list[str]
) -> None:
    """Record a URL slug that names the accepted model's stem with another final segment.

    Policy (2026-10-19): product URLs are often created once and never renamed, so once the
    page's own evidence has established a consistent model (a trusted model row, a shared model
    proven for the selected variant, or a corroborated title code), a differing slug is the
    warning ``MODEL_URL_DIFFERS``, not a block. Both wordings are kept; the URL never supplies or
    aliases a model. Conflicts between the page's own model claims still block elsewhere.
    """
    differs = _url_model_conflict(model, offer.offer_url)
    if not differs:
        return
    notes.append("MODEL_URL_DIFFERS")
    supporting.append(FieldExtraction(
        "model", ExtractionStatus.SUPPORTING, f"URL: {differs}; page model: {model}",
        "offer URL path [wording retained; checked only; never a model source]",
        offer.offer_url, SCOPE_URL,
    ))


def _title_core_claims(title: str) -> dict[str, set[str]]:
    """Core values a title states explicitly. Capacities are every installed total the title
    states (several when its structure is unclear), so any of them differing from the page's
    specification is a conflict."""
    low, high = _LAPTOP_DIAGONAL_INCHES
    text = _parse_text(title)
    capacities = _title_capacities(text)
    return {
        "screen_size_in": {s for s in _screen_size(text) if low <= Decimal(s) <= high},
        "cpu_series": cpu_series_forms(_cpu_scopes(text)[0]),
        "ram_capacity_gb": set(capacities["ram_capacity_gb"].values),
        "storage_capacity_gb": set(capacities["storage_capacity_gb"].values),
    }


# Display resolution names (2026-10-19, independent review). Only the precise VESA names
# establish a resolution. Marketing names are used loosely (PC Express writes "Full-HD WUXGA" for
# 1920x1200 panels), so FHD/Full HD/FHD+/QHD are kept as supporting ``resolution_name``
# evidence with their nominal size, never as an extracted resolution. "HD", "2K", "3K" and
# "UHD" are not read at all ("UHD Graphics" is an Intel GPU).
_PRECISE_RESOLUTIONS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\bWUXGA\b", re.I), "WUXGA", "1920x1200"),
    (re.compile(r"\bWQXGA\b", re.I), "WQXGA", "2560x1600"),
)
_LOOSE_RESOLUTIONS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\b(?:FHD|Full[\s-]?HD)\+", re.I), "FHD+", "1920x1200"),
    (re.compile(r"\b(?:FHD|Full[\s-]?HD)\b(?!\+)", re.I), "Full HD", "1920x1080"),
    (re.compile(r"\bW?QHD\b(?!\+)", re.I), "QHD", "2560x1440"),
)


def named_resolutions(text: str, *, precise: bool = True) -> dict[str, str]:
    """``{name: nominal WIDTHxHEIGHT}`` for resolution names stated in ``text``: the precise
    VESA names (``precise=True``) or the loosely used marketing names (``precise=False``)."""
    plain = _parse_text(text)
    return {
        name: value
        for pattern, name, value in (_PRECISE_RESOLUTIONS if precise else _LOOSE_RESOLUTIONS)
        if pattern.search(plain)
    }


def _title_secondary_claims(title: str) -> dict[str, set[str]]:
    """Secondary hardware a title states explicitly: one discrete GPU model, the exact
    processor model, the panel type, and the resolution (numeric, or a standard name). Never
    mandatory; unstated stays unknown."""
    text = _parse_text(title)
    numeric = _resolution(text)
    return {
        "gpu_model": canonical_gpu_models(text),
        "cpu_model": {model.display for model in processor_models(text)},
        "display_type": _display_type(text),
        "resolution": numeric or set(named_resolutions(text).values()),
    }


def _same_secondary(field: str, left: str, right: str) -> bool:
    """Whether a title value agrees with the same page's specification value. Panel types are
    compared by technology ("IPS-Level" in one place and "IPS" in the other is not a
    contradiction); the catalog comparison itself is the matcher's (matching.py)."""
    if field == "gpu_model":
        return canonical_gpu_models(left) == canonical_gpu_models(right) != set()
    if field == "display_type":
        return left.casefold().removesuffix("-level") == right.casefold().removesuffix("-level")
    return clean_text(left).casefold() == clean_text(right).casefold()


def _apply_title_fields(
    offer: OfferObservation,
    evidence: list[FieldExtraction],
    supporting: list[FieldExtraction],
    notes: list[str],
    *,
    shared: bool,
    shared_scope_proven: bool = False,
) -> None:
    """Core and secondary values that the product title states explicitly (2026-10-19).

    A clear title statement supplies a field the page's specifications do not state; a title
    statement that contradicts the specifications keeps both values (``AMBIGUOUS``) with
    ``TITLE_SPEC_CONFLICT_<FIELD>`` for review. Capacities keep their structure
    (``_title_capacities``): repeated components are summed, never collapsed; external, extra
    and maximum amounts are never installed capacity; an unclear structure makes the field
    ``AMBIGUOUS`` (review, no target). Secondary claims (GPU model, panel type, resolution) are
    supplied the same way so the matcher's conflict policy sees them; several different title
    values are kept as supporting evidence only. The title of a page whose specifications are
    shared by several variants describes the parent product, so it never supplies or overrides
    the selected variant's configuration; only when its variants are proven to differ in RAM and
    storage alone (``shared_scope_proven``) does it state secondary hardware common to all of
    them (2026-10-20). URL wording is never read here.
    """
    if not offer.raw_title:
        return
    if shared:
        if shared_scope_proven:
            _apply_title_secondary(offer.raw_title, evidence, supporting, notes, shared=True)
        return
    title = offer.raw_title
    text = _parse_text(title)
    claims = _title_core_claims(title)
    capacities = _title_capacities(text)
    for name, capacity in capacities.items():
        if capacity.not_installed:
            notes.append(f"TITLE_NOT_INSTALLED_{name.upper()}")
            supporting.append(FieldExtraction(
                name, ExtractionStatus.SUPPORTING, "; ".join(capacity.not_installed),
                "title [external, extra or free item; not installed capacity]", title,
                SCOPE_OPTIONAL,
            ))
    for index, item in enumerate(evidence):
        stated = claims.get(item.field)
        capacity = capacities.get(item.field)
        if not stated:
            continue
        if item.status is ExtractionStatus.MISSING:
            if len(stated) > 1 or (capacity is not None and capacity.ambiguous):
                shown = "; ".join(capacity.mentions) if capacity else "; ".join(sorted(stated))
                evidence[index] = FieldExtraction(
                    item.field, ExtractionStatus.AMBIGUOUS, shown,
                    "title [several or unclear capacity statements; review]", title, SCOPE_TITLE,
                )
                notes.append(f"TITLE_MULTIPLE_{item.field.upper()}")
                continue
            evidence[index] = FieldExtraction(
                item.field, ExtractionStatus.EXTRACTED, next(iter(stated)),
                f"title [{TITLE_STATEMENT}: explicit statement in the product title]",
                title, SCOPE_TITLE,
            )
            notes.append(f"TITLE_SUPPLIED_{item.field.upper()}")
            if capacity is not None and len(capacity.mentions) > 1:
                supporting.append(FieldExtraction(
                    f"{item.field.removesuffix('_capacity_gb')}_components",
                    ExtractionStatus.SUPPORTING, " + ".join(capacity.mentions),
                    "title [installed components summed]", title, SCOPE_TITLE,
                ))
        elif item.status is ExtractionStatus.EXTRACTED and stated != {item.value}:
            evidence[index] = FieldExtraction(
                item.field, ExtractionStatus.AMBIGUOUS,
                f"{item.value} | title: {'; '.join(sorted(stated))}",
                f"{item.source_label}; title [conflicting statement]",
                f"{item.source_text} | {title}", item.scope,
            )
            notes.append(f"TITLE_SPEC_CONFLICT_{item.field.upper()}")

    _apply_title_secondary(title, evidence, supporting, notes, shared=False)


def _apply_title_secondary(
    title: str,
    evidence: list[FieldExtraction],
    supporting: list[FieldExtraction],
    notes: list[str],
    *,
    shared: bool,
) -> None:
    """GPU model, panel type and resolution the title states (``_apply_title_fields``).

    ``shared``: the title belongs to a multi-variant page whose variants differ only in RAM and
    storage (a confirmed matrix, 2026-10-20). Such hardware is common to every variant, so the
    parent title states it for the selected one too; it is labelled as shared product evidence.
    """
    where = "title [SHARED_PRODUCT; variants differ only in RAM/storage; " if shared else "title ["
    for name, size in named_resolutions(title, precise=False).items():
        supporting.append(FieldExtraction(
            "resolution_name", ExtractionStatus.SUPPORTING, f"{name} (nominal {size})",
            f"{where}marketing resolution name; not an extracted resolution]", title,
            SCOPE_TITLE,
        ))
    present = {item.field: index for index, item in enumerate(evidence)}
    for field, stated in _title_secondary_claims(title).items():
        if not stated:
            continue
        if len(stated) > 1:
            notes.append(f"TITLE_MULTIPLE_{field.upper()}")
            supporting.append(FieldExtraction(
                field, ExtractionStatus.SUPPORTING, "; ".join(sorted(stated)),
                f"{where}several values; not used]", title, SCOPE_TITLE,
            ))
            continue
        value = next(iter(stated))
        if field not in present:
            evidence.append(FieldExtraction(
                field, ExtractionStatus.EXTRACTED, value,
                f"{where}{TITLE_STATEMENT}: explicit statement in the product title]",
                title, SCOPE_TITLE,
            ))
            notes.append(f"TITLE_SUPPLIED_{field.upper()}")
            continue
        item = evidence[present[field]]
        if field == "cpu_model":
            # A title/specification processor difference is reported by CPU_NUMBER_TITLE_DIFFERS
            # and the page's own specification row is the value compared (2026-10-22).
            continue
        if item.status is ExtractionStatus.EXTRACTED and not _same_secondary(
            field, item.value, value
        ):
            evidence[present[field]] = FieldExtraction(
                field, ExtractionStatus.AMBIGUOUS, f"{item.value} | title: {value}",
                f"{item.source_label}; title [conflicting statement]",
                f"{item.source_text} | {title}", item.scope,
            )
            notes.append(f"TITLE_SPEC_CONFLICT_{field.upper()}")


def _apply_title_model(
    offer: OfferObservation,
    evidence: list[FieldExtraction],
    supporting: list[FieldExtraction],
    notes: list[str],
    claims: list[tuple[str, str]],
    *,
    shared: bool,
    shared_locator: str,
    binding: str,
) -> None:
    """Accept one full title model code only when the other four fields stand on their own.

    Requirements: exactly one model code in the product title (documented part-number shapes are
    not codes), no unexplained designation after it, no conflicting model claim on the page or in
    its URL, the four other core fields extracted from non-title evidence of the same product and
    selected configuration, and no title statement contradicting them. A shared (multi-variant)
    title additionally needs the complete confirmed variant matrix. The value is the code as
    written; nothing is stripped, shortened, or taken from the catalog. Target assignment is left
    to the matcher.
    """
    primary = {item.field: item for item in evidence}
    model = primary["model"]
    found = title_identifiers(offer.raw_title)
    codes, parts = list(found.model_codes), list(found.part_numbers)
    for part in parts:
        supporting.append(FieldExtraction(
            "part_number_candidate", ExtractionStatus.SUPPORTING, part,
            "title [documented manufacturer part-number shape; not a model code]", part,
        ))
    for accessory in found.accessory_codes:
        supporting.append(FieldExtraction(
            "accessory_identifier", ExtractionStatus.SUPPORTING, accessory,
            "title [included-accessory code after an accessory noun; not a laptop model code]",
            offer.raw_title,
        ))
    if model.status is ExtractionStatus.EXTRACTED and model_codes(model.value) == [model.value]:
        # A trusted or approved code. Another full code in the title is a genuine conflict.
        approved = model.source_label.startswith("OWNER_APPROVED")
        if len(codes) == 1 and codes[0].casefold() != model.value.casefold() and not approved:
            notes.append("MODEL_TITLE_CONFLICT")
            evidence[evidence.index(model)] = FieldExtraction(
                "model", ExtractionStatus.AMBIGUOUS, f"{model.value} | {codes[0]}",
                f"{model.source_label}; title", f"{model.source_text} | {offer.raw_title}",
            )
            return
        _url_slug_differs(offer, model.value, supporting, notes)
        return
    if model.status not in {
        ExtractionStatus.MISSING, ExtractionStatus.EXTRACTED, ExtractionStatus.NOT_EXTRACTED,
    } or not codes:
        return
    if len(codes) > 1:
        notes.append("TITLE_MULTIPLE_MODEL_CODES")
        return
    code = codes[0]
    relations = {(label, value): _model_claim_relation(value, code) for label, value in claims}
    if model.status is ExtractionStatus.EXTRACTED:
        relations[(model.source_label, model.value)] = _model_claim_relation(model.value, code)
    conflicting = [value for (_, value), relation in relations.items() if relation == "CONFLICT"]
    if conflicting:
        notes.append("MODEL_CLAIM_CONFLICT")
        supporting.append(FieldExtraction(
            "model", ExtractionStatus.SUPPORTING, f"title: {code}; page: {'; '.join(conflicting)}",
            "model claims [title code vs page model rows]", offer.raw_title,
        ))
        if model.status is ExtractionStatus.EXTRACTED:
            evidence[evidence.index(model)] = FieldExtraction(
                "model", ExtractionStatus.UNRESOLVED, model.value,
                model.source_label, model.source_text,
            )
        return
    suffix = _title_code_suffix(offer.raw_title, code)
    if suffix:
        notes.append("TITLE_MODEL_UNEXPLAINED_SUFFIX")
        supporting.append(FieldExtraction(
            "model", ExtractionStatus.SUPPORTING, f"{code} {suffix}",
            "title [code with an unexplained designation; not stripped]", offer.raw_title,
        ))
        return
    if binding.startswith("NOT_CONFIRMED") or (shared and not shared_locator):
        notes.append("TITLE_MODEL_SCOPE_UNPROVEN")
        return
    others = [primary[field] for field in CORE_MATCH_COLUMNS if field != "model"]
    # The four other fields must be established, by the page's specifications or by explicit
    # title statements (2026-10-19, second review: valid title evidence is not discounted).
    if any(item.status is not ExtractionStatus.EXTRACTED for item in others):
        return
    title_claims = _title_core_claims(offer.raw_title)
    for item in others:
        stated = title_claims.get(item.field, set())
        if not stated or stated == {item.value}:
            continue
        if shared and item.field in {"ram_capacity_gb", "storage_capacity_gb"}:
            # The parent title describes the shared product, not the confirmed selection.
            notes.append(f"SHARED_TITLE_DIFFERS_{item.field.upper()}")
            supporting.append(FieldExtraction(
                item.field, ExtractionStatus.SUPPORTING, " | ".join(sorted(stated)),
                "title [shared product title; selected variant governs]", offer.raw_title,
            ))
            continue
        notes.append(f"TITLE_SPEC_CONFLICT_{item.field.upper()}")
        return
    if model.status is not ExtractionStatus.MISSING:
        supporting.append(FieldExtraction(
            "model", ExtractionStatus.SUPPORTING, model.value or "",
            f"{model.source_label} [less specific or shared model statement]", model.source_text,
        ))
    # The page's own evidence establishes the model; a differing URL slug is a warning.
    _url_slug_differs(offer, code, supporting, notes)
    corroboration = "; ".join(f"{item.field}: {item.source_label}" for item in others)
    scope = f"SHARED_PRODUCT; {shared_locator}; " if shared else ""
    evidence[evidence.index(model)] = FieldExtraction(
        "model", ExtractionStatus.EXTRACTED, code,
        f"title [{TITLE_MODEL_CORROBORATED}; {scope}corroborated by {corroboration}]",
        offer.raw_title,
    )
    notes.append(TITLE_MODEL_CORROBORATED)


def _normalize_model_name(
    evidence: list[FieldExtraction], supporting: list[FieldExtraction], notes: list[str]
) -> None:
    """A model-name wording that is a registered product name plus a repeated brand and/or a
    registered colour (``Apple Macbook Neo (Citrus)``) is that product name; the original
    wording stays as supporting evidence. An Apple part number in the wording
    (``MacBook Neo (MHFC4PP/A)``, 2026-10-22) is removed from the compared name and kept as
    separate part-number evidence, never as identity. Codes and unregistered names are
    unchanged."""
    index, model = next((i, e) for i, e in enumerate(evidence) if e.field == "model")
    if model.status is not ExtractionStatus.EXTRACTED or model_codes(model.value):
        return
    normalized = default_registry().normalize_model_name(model.value)
    if normalized is None or normalized.value == model.value:
        return
    evidence[index] = FieldExtraction(
        "model", ExtractionStatus.EXTRACTED, normalized.value,
        f"{model.source_label} [{MODEL_NAME_NORMALIZED}: registered product name; removed "
        f"{', '.join(normalized.removed) or 'spacing/punctuation'}]",
        model.source_text,
    )
    supporting.append(FieldExtraction(
        "model", ExtractionStatus.SUPPORTING, model.value,
        f"{model.source_label} [original wording, kept]", model.source_text,
    ))
    for part in normalized.part_numbers:
        supporting.append(FieldExtraction(
            "part_number_candidate", ExtractionStatus.SUPPORTING, part,
            f"{model.source_label} [Apple part number in the model wording; retailer part number, "
            "manufacturer role unverified; never used for identity]", model.source_text,
        ))
    notes.append(MODEL_NAME_NORMALIZED)


def _apply_title_product_name(
    offer: OfferObservation,
    evidence: list[FieldExtraction],
    notes: list[str],
    claims: list[tuple[str, str]],
    *,
    shared: bool,
    binding: str,
) -> None:
    """Accept one registered product name stated in the title (``Apple MacBook Neo 13" ...``)
    under the title-code rule: no model code in the title, no model claim on the page, exactly
    one registered product name in the title, the four other core fields extracted from the
    page's own non-title evidence, and no title statement contradicting them. The value is the
    registered name; nothing is taken from a catalog target."""
    primary = {item.field: item for item in evidence}
    model = primary["model"]
    if model.status is not ExtractionStatus.MISSING or claims or shared:
        return
    if binding.startswith("NOT_CONFIRMED") or title_identifiers(offer.raw_title).model_codes:
        return
    names = default_registry().title_product_names(offer.raw_title)
    if not names:
        return
    if len(names) > 1:
        notes.append("TITLE_MULTIPLE_PRODUCT_NAMES")
        return
    others = [primary[field] for field in CORE_MATCH_COLUMNS if field != "model"]
    # The four other fields must be established, by the page's specifications or by explicit
    # title statements (2026-10-19, second review: valid title evidence is not discounted).
    if any(item.status is not ExtractionStatus.EXTRACTED for item in others):
        return
    title_claims = _title_core_claims(offer.raw_title)
    for item in others:
        stated = title_claims.get(item.field, set())
        if stated and stated != {item.value}:
            notes.append(f"TITLE_SPEC_CONFLICT_{item.field.upper()}")
            return
    corroboration = "; ".join(f"{item.field}: {item.source_label}" for item in others)
    evidence[evidence.index(model)] = FieldExtraction(
        "model", ExtractionStatus.EXTRACTED, names[0].name,
        f"title [{TITLE_PRODUCT_NAME_CORROBORATED}; registered product name; corroborated by "
        f"{corroboration}]",
        offer.raw_title,
    )
    notes.append(TITLE_PRODUCT_NAME_CORROBORATED)


def _model_identifier_conflicts(
    evidence: list[FieldExtraction], notes: list[str], identifiers: list[tuple[str, str]],
    skus: set[str],
) -> None:
    """A structured ``mpn`` that is a different model code is a genuine identity conflict.

    An ``mpn`` repeating the retailer SKU is inventory data (``MPN_REPEATS_RETAILER_SKU``) and is
    ignored here. A part number that is not code-shaped stays supporting evidence.
    """
    model = next(item for item in evidence if item.field == "model")
    if model.status is not ExtractionStatus.EXTRACTED:
        return
    for label, value in identifiers:
        if "mpn" not in label.casefold() or value in skus or value == "(blank)":
            continue
        codes, _ = title_model_codes(value)
        if codes and [c.casefold() for c in codes] != [model.value.casefold()]:
            notes.append("MODEL_IDENTIFIER_CONFLICT")
            evidence[evidence.index(model)] = FieldExtraction(
                "model", ExtractionStatus.AMBIGUOUS, f"{model.value} | {value}",
                f"{model.source_label}; {label}", f"{model.source_text} | {value}",
            )
            return


def match_basis(evidence: tuple[FieldExtraction, ...] | list[FieldExtraction]) -> str:
    """How the extracted core fields were supported, e.g. ``LABELED_SPECIFICATION`` or
    ``TITLE_MODEL_CORROBORATED+SELECTED_VARIANT+SHARED_PRODUCT``."""
    labels = [
        item.source_label for item in evidence
        if item.field in CORE_MATCH_COLUMNS and item.status is ExtractionStatus.EXTRACTED
    ]
    tags = []
    if any(TITLE_MODEL_CORROBORATED in label for label in labels):
        tags.append(TITLE_MODEL_CORROBORATED)
    if any(TITLE_PRODUCT_NAME_CORROBORATED in label for label in labels):
        tags.append(TITLE_PRODUCT_NAME_CORROBORATED)
    if any(MODEL_NAME_NORMALIZED in label for label in labels):
        tags.append(MODEL_NAME_NORMALIZED)
    if any(label.startswith("OWNER_APPROVED") for label in labels):
        tags.append("OWNER_APPROVED_INTERPRETATION")
    if any("[selected variant" in label for label in labels):
        tags.append("SELECTED_VARIANT")
    if any("SHARED_PRODUCT" in label for label in labels):
        tags.append("SHARED_PRODUCT")
    if any("[PRODUCT_DESCRIPTION; explicit inches]" in label for label in labels):
        tags.append("PRODUCT_DESCRIPTION")
    return "+".join(tags) or "LABELED_SPECIFICATION"


def _processor_numbers(text: str) -> set[str]:
    parsed = _parse_text(text)
    return {
        match[1].upper() for pattern in _PROCESSOR_NUMBERS for match in pattern.finditer(parsed)
    }


def label_name(label: str) -> str:
    """A specification label as compared with the reviewed label lists: case-folded, without a
    trailing colon (``MEMORY:`` is ``memory``; 2026-10-19, second review). The label text as
    written stays in the evidence."""
    return label.strip().rstrip(":").strip().casefold()


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
    specs = details.raw_specifications if details is not None else ()
    scoped = details.scoped_evidence if details is not None else ()
    if labels.paragraph_layout:
        labeled = _paragraph_groups(specs, labels.headings())
    else:
        # Unlabeled lines (headings, "✅ ..." bullets) carry no field label and are never used.
        labeled = [(label, value) for label, value in specs if label and value]

    def rows(names: frozenset[str]) -> list[tuple[str, str]]:
        return [(label, value) for label, value in labeled if label_name(label) in names]

    def model_value(label: str, value: str) -> set[str]:
        return _model_code(value) if label_name(label) in labels.model_code else _model(value)

    display_rows = rows(labels.display)
    # "Up to 13th Gen Intel Core i7" describes a product line's options, not this configuration.
    cpu_rows = [
        (label, value) for label, value in rows(labels.cpu)
        if not _GENERIC_CPU_CLAIM.match(_parse_text(value))
    ]
    generic_cpu_rows = [row for row in rows(labels.cpu) if row not in cpu_rows]
    core_plan: tuple[
        tuple[str, list[tuple[str, str]], Callable[..., set[str]], bool, bool], ...
    ] = (
        ("model", rows(labels.model | labels.model_code), model_value, False, True),
        ("screen_size_in", display_rows, _screen_size_row, False, True),
        ("cpu_series", cpu_rows, _installed_cpu_series, False, False),
        ("ram_capacity_gb", rows(labels.memory), _ram, True, False),
        ("storage_capacity_gb", rows(labels.storage), _storage, True, False),
    )
    variant_scoped = labels.shared_variant_description and bool(specs)
    shared = variant_scoped and _has_variant_prefix(offer, specs)
    binding, variant_id, selected = _selected_variant(scoped)
    bound = shared and binding == "CONFIRMED"
    shared_locator = (
        _shared_cpu_display_scope(scoped, labels, variant_id, selected) if bound else ""
    )

    evidence: list[FieldExtraction] = []
    supporting: list[FieldExtraction] = []
    notes: list[str] = ["MULTI_VARIANT_SHARED_DESCRIPTION"] if shared else []
    if binding.startswith("NOT_CONFIRMED"):
        notes.append("SELECTED_VARIANT_NOT_CONFIRMED")

    def support(field: str, value: str, label: str, text: str = "", *, scope: str = "") -> None:
        supporting.append(
            FieldExtraction(field, ExtractionStatus.SUPPORTING, value, label, text or value, scope)
        )

    for field, field_rows, parse, one_row, with_label in core_plan:
        item = _extract_field(
            field, field_rows, parse, required=True, one_row=one_row, with_label=with_label
        )
        assert item is not None
        if shared:
            # Shared fields stay supporting; CPU/screen may be inherited only with the
            # complete confirmed RAM/storage-only matrix required by owner policy.
            if item.status is ExtractionStatus.EXTRACTED:
                support(
                    field,
                    item.value,
                    f"{item.source_label} [shared product description]",
                    item.source_text,
                )
                title_values = (
                    _cpu_series(_cpu_scopes(offer.raw_title)[0]) if field == "cpu_series"
                    else _screen_size(offer.raw_title) if field == "screen_size_in" else set()
                )
                conflict = title_values and title_values != {item.value}
                if conflict and field in {"cpu_series", "screen_size_in"}:
                    notes.append(f"SHARED_PRODUCT_TITLE_DIFFERS_{field.upper()}")
                    support(field, " | ".join(sorted(title_values)), "title [conflict check only]")
                # A full model code is inherited under the same complete-matrix condition
                # (balanced policy, 2026-10-15); title/URL/identifier conflicts are checked later.
                inheritable = field in {"cpu_series", "screen_size_in"} or (
                    field == "model" and model_codes(item.value) == [item.value]
                )
                if shared_locator and inheritable and not conflict:
                    item = FieldExtraction(
                        field, ExtractionStatus.EXTRACTED, item.value,
                        f"{item.source_label} [SHARED_PRODUCT; selected variant {variant_id}; "
                        f"{shared_locator}]",
                        item.source_text,
                    )
                else:
                    item = FieldExtraction(
                        field, ExtractionStatus.NOT_EXTRACTED, "",
                        item.source_label, item.source_text
                    )
            names = {
                "ram_capacity_gb": labels.selected_memory,
                "storage_capacity_gb": labels.selected_storage,
            }.get(field, frozenset())
            option_rows = [
                (f"{name} [selected variant {variant_id}]", value)
                for name, value in selected
                if name.casefold() in names
            ]
            if bound and option_rows:
                # Installed RAM/storage stated by the bound selected variant's own options.
                item = _extract_field(field, option_rows, parse, required=True, one_row=True)
                assert item is not None
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
        if not any(item.field == "resolution" for item in evidence):
            # A standard resolution name in a display row ('14" Full-HD IPS') when the rows state
            # no numeric resolution (2026-10-19, independent review).
            named = sorted(
                (size, name, label, value)
                for label, value in display_rows
                for name, size in named_resolutions(value).items()
            )
            sizes = {size for size, *_ in named}
            if len(sizes) == 1:
                size, name, label, value = named[0]
                evidence.append(FieldExtraction(
                    "resolution", ExtractionStatus.EXTRACTED, size,
                    f"{label} [named standard: {name} = {size}]", value,
                ))
                notes.append("RESOLUTION_FROM_STANDARD_NAME")
            elif len(sizes) > 1:
                evidence.append(FieldExtraction(
                    "resolution", ExtractionStatus.AMBIGUOUS, "; ".join(sorted(sizes)),
                    "display rows [several standard resolution names]",
                    " | ".join(value for _, value in display_rows),
                ))
        for label, value in display_rows:
            for name, size in named_resolutions(value, precise=False).items():
                support("resolution_name", f"{name} (nominal {size})",
                        f"{label} [marketing resolution name; not an extracted resolution]",
                        value)

    # The discrete GPU a labelled graphics row states (2026-10-20): a secondary specification
    # the matcher compares when the target records one. A shared description counts only when
    # the variants are proven to differ in RAM/storage alone. Integrated graphics are not read.
    gpu_rows = [(label, value) for label, value in labeled if label_name(label) in _GPU_LABELS]
    if gpu_rows and (not shared or shared_locator):
        gpus = sorted({gpu for _, value in gpu_rows for gpu in canonical_gpu_models(value)})
        tag = f" [SHARED_PRODUCT; {shared_locator}]" if shared else ""
        label = "; ".join(dict.fromkeys(label for label, _ in gpu_rows)) + tag
        text = " | ".join(value for _, value in gpu_rows)
        if len(gpus) == 1:
            evidence.append(FieldExtraction("gpu_model", ExtractionStatus.EXTRACTED, gpus[0],
                                            label, text))
        elif len(gpus) > 1:
            evidence.append(FieldExtraction("gpu_model", ExtractionStatus.AMBIGUOUS,
                                            "; ".join(gpus), label, text))

    # The exact processor model an installed-processor row states (2026-10-22): a secondary
    # specification, compared only when the target records a full processor number too. Only
    # the installed wording counts ("up to" capability clauses are family options); a family
    # name without its number states no model. A shared description needs the same proven scope
    # as its processor family.
    if cpu_rows and (not shared or shared_locator):
        installed = [(label, _cpu_scopes(value)[0]) for label, value in cpu_rows]
        models = {
            model.display: model
            for _, value in installed for model in processor_models(value, bare_apple=True)
        }
        if models:
            tag = f" [SHARED_PRODUCT; {shared_locator}]" if shared else ""
            sources = [(label, value) for label, value in cpu_rows
                       if processor_models(_cpu_scopes(value)[0], bare_apple=True)]
            label = "; ".join(dict.fromkeys(label for label, _ in sources)) + tag
            text = " | ".join(value for _, value in sources)
            status = ExtractionStatus.EXTRACTED if len(models) == 1 else ExtractionStatus.AMBIGUOUS
            evidence.append(FieldExtraction("cpu_model", status, "; ".join(sorted(models)),
                                            label, text))

    prose = _prose_screen_rows(scoped)
    if not shared or (
        shared_locator and "SHARED_PRODUCT_TITLE_DIFFERS_SCREEN_SIZE_IN" not in notes
    ):
        evidence = [
            _screen_with_prose(item, prose, display_rows)
            if item.field == "screen_size_in" else item
            for item in evidence
        ]
    if shared_locator:
        support(
            "variant_definitions",
            next(value for scope, _, value in scoped if scope == VARIANT_DEFINITIONS),
            shared_locator,
        )
        notes.append("SHARED_CPU_DISPLAY_RAM_STORAGE_ONLY")

    primary = {item.field: item for item in evidence}
    scope_note = " [shared product description]" if shared else ""

    # Memory: maximums, slots, and frequencies are never installed capacity.
    memory_rows = rows(labels.memory)
    if memory_rows:
        memory_label = "; ".join(label for label, _ in memory_rows) + scope_note
        memory_text = " | ".join(value for _, value in memory_rows)
        memory = _memory_details(memory_text)
        if shared and memory["stated"] and not any(
            s.field == "ram_capacity_gb" for s in supporting
        ):
            support("ram_capacity_gb", "; ".join(memory["stated"]), memory_label, memory_text)
        for key, field, scope in (
            ("maximum", "ram_maximum_gb", SCOPE_MAXIMUM),
            ("optional", "ram_optional_gb", SCOPE_OPTIONAL),
            ("slots", "ram_slots", ""),
            ("frequency", "ram_frequency", ""),
        ):
            if memory[key]:
                support(field, "; ".join(memory[key]), memory_label, memory_text, scope=scope)
        ram = primary["ram_capacity_gb"]
        if bound and ram.status is ExtractionStatus.EXTRACTED and memory["stated"] and (
            ram.value not in memory["stated"]
        ):
            notes.append("SHARED_DESCRIPTION_DIFFERS_RAM_CAPACITY_GB")
    storage = primary["storage_capacity_gb"]
    shared_storage = [s.value for s in supporting if s.field == "storage_capacity_gb"]
    if bound and storage.status is ExtractionStatus.EXTRACTED and shared_storage and (
        storage.value not in shared_storage
    ):
        notes.append("SHARED_DESCRIPTION_DIFFERS_STORAGE_CAPACITY_GB")

    # Storage: a drive amount without a capacity unit and transfer rates stay supporting only.
    storage_rows = rows(labels.storage)
    storage_text = " | ".join(value for _, value in storage_rows)
    storage_label = "; ".join(label for label, _ in storage_rows) + scope_note
    unitless = [m[1] for m in _UNITLESS_DRIVE.finditer(_parse_text(storage_text))]
    for amount in dict.fromkeys(unitless):
        support("storage_amount_without_unit", amount, storage_label, storage_text)
    for match in _THROUGHPUT.finditer(_parse_text(storage_text)):
        support("storage_throughput", f"{match[1]} {match[2]}", storage_label, storage_text)
    for _, value in storage_rows:
        scopes = _capacity_scopes(value)
        for name, clauses, scope in (
            ("storage_maximum_gb", scopes.maximums, SCOPE_MAXIMUM),
            ("storage_optional_gb", scopes.optional, SCOPE_OPTIONAL),
        ):
            amounts = [
                str(_gigabytes(m[1], m[2], allow_tb=True))
                for clause in clauses for m in _CAPACITY.finditer(_parse_text(clause))
            ]
            if amounts:
                support(name, "; ".join(dict.fromkeys(amounts)), storage_label, value, scope=scope)
    if unitless and storage.status is not ExtractionStatus.EXTRACTED:
        notes.append("STORAGE_CAPACITY_UNIT_MISSING")

    # Installed components behind a summed total stay visible; drives keep their media.
    ram = primary["ram_capacity_gb"]
    if ram.status is ExtractionStatus.EXTRACTED and "+" in ram.source_text:
        installed = _installed_memory_text(ram.source_text)
        parts = _installed_components(
            ram.source_text if installed is None else installed, allow_tb=False
        )
        if parts:
            support("ram_components", " + ".join(text for _, text in parts),
                    ram.source_label, ram.source_text)
    if storage.status is ExtractionStatus.EXTRACTED and "+" in storage.source_text:
        parts = _storage_components(_capacity_scopes(storage.source_text).installed)
        if parts:
            row_media = _storage_media(_parse_text(storage.source_text))
            media = sorted({
                medium
                for _, text in parts
                for medium in (_storage_media(text) or (row_media if len(row_media) == 1 else []))
            })
            support("storage_layout", " + ".join(text for _, text in parts),
                    storage.source_label, storage.source_text)
            if media:
                evidence.append(FieldExtraction(
                    "storage_type", ExtractionStatus.EXTRACTED, "+".join(media),
                    f"{storage.source_label} [installed drives]", storage.source_text,
                ))
            if len(media) > 1:
                notes.append("STORAGE_MIXED_MEDIA")
    for label, value in generic_cpu_rows:
        support("cpu_series", value, f"{label} [generic capability claim; not this processor]",
                scope=SCOPE_FAMILY_CAPABILITY)
    for label, value in cpu_rows:
        for clause in _cpu_scopes(value)[1]:
            support("cpu_series", clause,
                    f"{label} [family capability clause; not this processor]", value,
                    scope=SCOPE_FAMILY_CAPABILITY)

    # Title versus specification: reported, never used to choose a value.
    specified = _processor_numbers(" | ".join(_cpu_scopes(value)[0] for _, value in cpu_rows))
    titled = _processor_numbers(offer.raw_title)
    if specified and titled and not specified & titled:
        notes.append("CPU_NUMBER_TITLE_DIFFERS")
        support(
            "cpu_model",
            f"title: {', '.join(sorted(titled))}; specification: {', '.join(sorted(specified))}",
            "processor number [title vs specification]",
        )
    color_rows = rows(labels.color)
    if color_rows:
        stated = color_rows[0][1]
        if stated.casefold() not in offer.raw_title.casefold():
            in_title = [
                clean_text(group)
                for group in re.findall(r"\(([^()]*)\)", offer.raw_title)
                if _COLOR_WORDS & set(group.casefold().split())
            ]
            if in_title:
                notes.append("COLOR_TITLE_DIFFERS")
                support(
                    "color",
                    f"specification: {stated}; title: {in_title[0]}",
                    f"{color_rows[0][0]} [specification vs title]",
                )

    for label, value in rows(labels.part_number):
        support(
            "part_number_candidate",
            value,
            f"{label} [retailer specification row; manufacturer role unverified]",
        )
    low, high = _LAPTOP_DIAGONAL_INCHES
    identifiers: list[tuple[str, str]] = []
    for scope, label, value in scoped:
        if scope == PRODUCT_DESCRIPTION:
            sizes = [
                _inches(match[1])
                for match in _SCREEN_SIZE.finditer(_parse_text(value))
                if low <= Decimal(match[1]) <= high
            ]
            for size in dict.fromkeys(sizes):
                support(
                    "screen_size_in",
                    size,
                    f"{label} [PRODUCT_DESCRIPTION; explicit size retained for consistency/review]",
                    value,
                )
        elif scope == IDENTIFIER:
            identifiers.append((label, value))
            support("identifier", value, label)
        elif scope == ENUMERATED_VARIANT:
            support("enumerated_variant", value, f"{label} [listed by the page; not collected]")
    skus = {v for label, v in identifiers if "sku" in label.casefold() and v != "(blank)"}
    if any("mpn" in label.casefold() and value in skus for label, value in identifiers):
        notes.append("MPN_REPEATS_RETAILER_SKU")

    _apply_owner_approvals(offer, evidence, supporting, notes)
    _apply_title_fields(offer, evidence, supporting, notes, shared=shared,
                        shared_scope_proven=bool(shared_locator))
    claims = [(label, value) for label, value in labeled if label_name(label) in MODEL_CLAIM_LABELS]
    _normalize_model_name(evidence, supporting, notes)
    _apply_title_model(
        offer, evidence, supporting, notes, claims,
        shared=shared, shared_locator=shared_locator, binding=binding,
    )
    _apply_title_product_name(offer, evidence, notes, claims,
                              shared=shared, binding=binding)
    _model_identifier_conflicts(evidence, notes, identifiers, skus)
    if any(DEDUPLICATED_ROWS in item.source_label for item in evidence):
        notes.append("EQUIVALENT_REPEATED_FACTS_DEDUPLICATED")

    values = {
        item.field: item.value for item in evidence if item.status is ExtractionStatus.EXTRACTED
    }
    return ExtractedAttributes(
        offer.retailer_id,
        offer.retailer_id,
        values,
        tuple(_with_scope(item) for item in evidence + supporting),
        notes=tuple(dict.fromkeys(notes)),
    )


def evidence_scope(item: FieldExtraction) -> str:
    """The scope an evidence item applies to: explicit, or recovered from its source label."""
    if item.scope:
        return item.scope
    label = item.source_label
    if not label and item.status is ExtractionStatus.MISSING:
        return ""
    if label.startswith("OWNER_APPROVED"):
        return SCOPE_OWNER_APPROVED
    if "[selected variant" in label:
        return SCOPE_SELECTED_VARIANT
    if "SHARED_PRODUCT" in label or "[shared product" in label or item.field == (
        "variant_definitions"
    ):
        return SCOPE_SHARED_PRODUCT
    if "[PRODUCT_DESCRIPTION" in label:
        return SCOPE_PRODUCT_DESCRIPTION
    if "[generic capability" in label or "[family capability" in label:
        return SCOPE_FAMILY_CAPABILITY
    if label.casefold().startswith("title"):
        return SCOPE_TITLE
    if label.startswith("offer URL path"):
        return SCOPE_URL
    if item.field in {"identifier", "part_number_candidate"}:
        return SCOPE_IDENTIFIER
    if item.field == "enumerated_variant":
        return SCOPE_ENUMERATED_VARIANT
    if item.field.endswith("_maximum_gb"):
        return SCOPE_MAXIMUM
    if item.field.endswith("_optional_gb"):
        return SCOPE_OPTIONAL
    return SCOPE_PRODUCT_SPECIFICATION


def _with_scope(item: FieldExtraction) -> FieldExtraction:
    scope = evidence_scope(item)
    return item if scope == item.scope else replace(item, scope=scope)
