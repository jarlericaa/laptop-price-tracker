"""The selected variation's own specifications, and real disagreement with its mapped target.

A mapped variation is compared with the reviewed specifications of its ``TARGET_MASTER`` target.
This module reads only what the *selected variation itself* states (owner decision 2026-11-03),
so a parent listing that offers several configurations is never a conflict:

- The variation name (``shopee_model_name``) is read first. A value it states once, with a label
  that makes the field unambiguous, is the variation's specification.
- The parent title (``item_name``) supplies a field only when the variation name says nothing
  about it and the parent states exactly one value. A parent that lists options ("Intel
  i7/i5/i3", "4GB-16GB RAM", "Core i7 - i5") applies to no particular variation: the field stays
  unknown. The first CPU of a multi-option title is never taken, and no sibling variation's text
  is ever read.
- Several different values, a range, an unlabeled capacity pair ("8G-256GB") and anything else
  not stated plainly stay **unknown**; unknown is never a conflict and never filled in.

Only the five-field identity values that a name can state plainly are compared: ``cpu_series``
(series level, never an exact CPU SKU; ``cpu_series_forms`` is the catalog's own vocabulary),
``screen_size_in``, ``ram_capacity_gb`` and ``storage_capacity_gb``. Review rows in
``MATCH_REVIEW`` are evidence to show beside a finding, never an input: a stale review field
cannot override the current selected variation. Nothing here writes a catalog or Sheet.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .conventions import cpu_series_forms, parse_text
from .normalization import text_key

# Reading sources.
VARIATION = "VARIATION"
PARENT = "PARENT"
UNKNOWN = "UNKNOWN"
AMBIGUOUS = "AMBIGUOUS"

COMPARED_FIELDS: tuple[str, ...] = (
    "cpu_series",
    "screen_size_in",
    "ram_capacity_gb",
    "storage_capacity_gb",
)

# A screen is a measured diagonal or a marketed class (15 / 15.6). Only a gross difference is a
# disagreement; this is not the matching rule and never makes 15 equal 15.6 anywhere else.
SCREEN_TOLERANCE_INCHES = Decimal("1")

_UNIT_GB = {"GB": Decimal(1), "G": Decimal(1), "TB": Decimal(1000)}
_NUMBER = r"([0-9]{1,4}(?:\.[0-9]{1,2})?)"
_RAM_LABEL = r"(?:RAM|memory|unified\s+memory|DDR[0-9][A-Z]?|LPDDR[0-9][A-Z]?)"
_STORAGE_LABEL = r"(?:SSD|HDD|NVMe|eMMC|M\.2|PCIe|storage|ROM|hard\s*(?:disk|drive))"
# "16GB RAM", "8GBRAM" (no space or boundary), "16GB DDR4": the amount precedes its label. A label
# that precedes an amount counts only for RAM/memory itself and never when a storage label follows
# the amount ("DDR4 512GB M.2" and "RAM 512GB SSD" describe the drive, not the memory).
_RAM_PATTERNS = (
    re.compile(rf"(?<![0-9.]){_NUMBER}\s*(GB|G)\s*(?:of\s+)?{_RAM_LABEL}", re.I),
    re.compile(rf"\b(?:RAM|memory)\s*[:\-]?\s*{_NUMBER}\s*(GB|G)\b(?!\s*{_STORAGE_LABEL})", re.I),
)
_STORAGE_PATTERNS = (
    re.compile(rf"(?<![0-9.]){_NUMBER}\s*(GB|TB)\b\s*(?:of\s+)?{_STORAGE_LABEL}", re.I),
    re.compile(rf"{_STORAGE_LABEL}\s*[:\-]?\s*{_NUMBER}\s*(GB|TB)\b", re.I),
)
_SCREEN = re.compile(
    r"(?<![0-9.])([0-9]{2}(?:\.[0-9]{1,2})?)\s*(?:\"|″|”|''|’’|-?\s*inch(?:es)?\b|in\b)", re.I
)
# Two capacities (or tiers) joined by a separator: a range or an option list, not one value.
_CAPACITY_CHAIN = re.compile(
    r"[0-9]{1,4}\s*(?:GB|TB|G)?\s*(?:-|–|/|\||&|,|\bor\b|\bto\b)\s*[0-9]{1,4}\s*(?:GB|TB|G)\b", re.I
)
_CORE_TIER = re.compile(r"(?<![A-Za-z0-9])[iI]([3579])(?![A-Za-z0-9])")
# Wording that says a variation names a processor, even when this module cannot read which one
# ("R5", "Core i7-1255U" next to an option, "Celeron"): the parent title never fills that field.
_CPU_HINT = re.compile(
    r"(?<![A-Za-z0-9])(?:R[3579]|[iI][3579]|Ryzen|Core|Celeron|Pentium|Athlon|Snapdragon|"
    r"Apple|M[1-9])(?![A-Za-z0-9])",
    re.I,
)
# Any capacity-like token: "16GB", "8G", or a pair such as "16+512" (unlabeled).
_CAPACITY_TOKEN = re.compile(
    r"(?<![0-9.])(?:[0-9]{1,4}(?:\.[0-9]+)?\s*(?:GB|TB|G)\b|[0-9]{1,3}\s*\+\s*[0-9]{2,4}(?![0-9]))",
    re.I,
)


@dataclass(frozen=True)
class FieldReading:
    """What the selected variation states for one field, and where it came from."""

    field: str
    value: str  # canonical value, or "" when unknown
    source: str  # VARIATION, PARENT, UNKNOWN or AMBIGUOUS
    evidence: str  # the exact text the value was read from ("" when none)
    note: str = ""

    def describe(self) -> str:
        if not self.value:
            return f"{self.field}=unknown ({self.source.lower()})"
        return f"{self.field}={self.value} [{self.source.lower()}: {self.evidence}]"


@dataclass(frozen=True)
class SpecConflict:
    """A real disagreement between a stated variation value and the mapped target."""

    field: str
    variation_value: str
    target_value: str
    source: str
    evidence: str

    def describe(self) -> str:
        return (
            f"{self.field}: {self.source.lower()} states {self.variation_value!r} "
            f'("{self.evidence}") but the target records {self.target_value!r}'
        )


def _gigabytes(amount: str, unit: str) -> str | None:
    try:
        total = Decimal(amount) * _UNIT_GB[unit.upper()]
    except (InvalidOperation, KeyError):
        return None
    return format(total.normalize(), "f") if total > 0 else None


def _chain_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in _CAPACITY_CHAIN.finditer(text)]


def _overlaps(span: tuple[int, int], chains: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in chains)


def _capacity(text: str, patterns) -> tuple[set[str], str, bool, list[tuple[int, int]]]:
    """``(values, first evidence, any statement is part of an option chain, matched spans)``."""
    chains = _chain_spans(text)
    values: set[str] = set()
    spans: list[tuple[int, int]] = []
    evidence, optioned = "", False
    for pattern in patterns:
        for match in pattern.finditer(text):
            unit = next(g for g in match.groups()[::-1] if g and g.isalpha())
            amount = next(g for g in match.groups() if g and g[0].isdigit())
            value = _gigabytes(amount, unit)
            if value is None:
                continue
            if _overlaps(match.span(), chains):
                optioned = True
            values.add(value)
            spans.append(match.span())
            evidence = evidence or match[0].strip()
    return values, evidence, optioned, spans


def _cpu(text: str) -> tuple[set[str], str, bool]:  # values, evidence, optioned
    forms = cpu_series_forms(text)
    tiers = {f"Core i{match[1]}" for match in _CORE_TIER.finditer(text)}
    values = forms | tiers
    evidence = ""
    if len(values) == 1:
        wanted = next(iter(values))
        for pattern_match in _CORE_TIER.finditer(text):
            if f"Core i{pattern_match[1]}" == wanted:
                evidence = pattern_match[0]
                break
        evidence = evidence or wanted
    # A series named next to an unrecognized alternative ("Core i7 - i5-") would look single.
    optioned = len({match[1] for match in _CORE_TIER.finditer(text)}) > 1 or (
        len(forms) == 1 and len(tiers - forms) > 0
    )
    return values, evidence, optioned


def _screen(text: str) -> tuple[set[str], str, bool]:
    values, evidence = set(), ""
    for match in _SCREEN.finditer(text):
        number = Decimal(match[1])
        if Decimal(10) <= number <= Decimal("18.4"):
            values.add(format(number.normalize(), "f"))
            evidence = evidence or match[0].strip()
    return values, evidence, False


def _stray_capacity(text: str, consumed: list[tuple[int, int]]) -> bool:
    """True when the text carries a capacity-like token no labeled RAM/storage statement used."""
    return any(not _overlaps(m.span(), consumed) for m in _CAPACITY_TOKEN.finditer(text))


def _read_text(text: str) -> dict[str, tuple[set[str], str, bool]]:
    """Per field ``(values, evidence, optioned)``; ``_silent`` flags fields the text speaks of."""
    text = parse_text(text)
    ram = _capacity(text, _RAM_PATTERNS)
    storage = _capacity(text, _STORAGE_PATTERNS)
    stray = _stray_capacity(text, ram[3] + storage[3])
    cpu = _cpu(text)
    screen = _screen(text)
    return {
        "cpu_series": cpu,
        "screen_size_in": screen,
        "ram_capacity_gb": ram[:3],
        "storage_capacity_gb": storage[:3],
        "_speaks": ({"cpu_series"} if _CPU_HINT.search(text) else set())  # type: ignore[dict-item]
        | ({"ram_capacity_gb", "storage_capacity_gb"} if stray else set())
        | ({"screen_size_in"} if screen[0] else set()),
    }


def read_variation_specs(item_name: str, variation_name: str) -> dict[str, FieldReading]:
    """The selected variation's stated values, variation name first (see the module docstring)."""
    own = _read_text(variation_name or "")
    parent = _read_text(item_name or "")
    speaks = own["_speaks"]
    readings: dict[str, FieldReading] = {}
    for field in COMPARED_FIELDS:
        values, evidence, optioned = own[field]
        if len(values) == 1 and not optioned:
            readings[field] = FieldReading(field, next(iter(values)), VARIATION, evidence)
            continue
        if values or field in speaks:
            note = (
                "the variation names several values or an option range"
                if values
                else "the variation speaks of this field in a form that is not read; "
                "the parent title does not fill it"
            )
            readings[field] = FieldReading(field, "", AMBIGUOUS, "", note)
            continue
        values, evidence, optioned = parent[field]
        if len(values) == 1 and not optioned:
            readings[field] = FieldReading(field, next(iter(values)), PARENT, evidence)
        elif values:
            note = "the parent title lists options, so it applies to no one variation"
            readings[field] = FieldReading(field, "", AMBIGUOUS, "", note)
        else:
            readings[field] = FieldReading(field, "", UNKNOWN, "")
    return readings


def _screens_disagree(stated: str, recorded: str) -> bool:
    try:
        return abs(Decimal(stated) - Decimal(recorded)) >= SCREEN_TOLERANCE_INCHES
    except InvalidOperation:
        return False


def spec_conflicts(
    readings: Mapping[str, FieldReading], target: Mapping[str, object]
) -> list[SpecConflict]:
    """Fields where the variation states a value and the target records a different one.

    A field the variation leaves unknown, or the target leaves blank, is never a conflict.
    """
    found: list[SpecConflict] = []
    for field in COMPARED_FIELDS:
        reading = readings[field]
        recorded = text_key(target.get(field))
        if not reading.value or recorded is None:
            continue
        recorded_text = str(target.get(field)).strip()
        if field == "screen_size_in":
            differs = _screens_disagree(reading.value, recorded_text)
        elif field == "cpu_series":
            differs = reading.value.casefold() != recorded_text.casefold()
        else:
            try:
                differs = Decimal(reading.value) != Decimal(recorded_text)
            except InvalidOperation:
                differs = False
        if differs:
            found.append(
                SpecConflict(field, reading.value, recorded_text, reading.source, reading.evidence)
            )
    return found


def evidence_summary(readings: Mapping[str, FieldReading]) -> str:
    """One line of the variation's own stated specifications (unknown fields are listed)."""
    return "; ".join(readings[field].describe() for field in COMPARED_FIELDS)
