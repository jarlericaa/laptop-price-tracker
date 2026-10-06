"""Comparison-only normalization shared by the TARGET_MASTER validator and the exact matcher.

Normalized keys are never stored or written back. They remove harmless formatting only: Unicode
form, whitespace, letter case, trademark marks, and equivalent decimal spellings of a number.
Nothing here infers a value, and blank values never produce a key, so missing values cannot match.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

import pandas as pd

CORE_MATCH_COLUMNS: tuple[str, ...] = (
    "model",
    "screen_size_in",
    "cpu_series",
    "ram_capacity_gb",
    "storage_capacity_gb",
)

# Registered/trademark marks are typography, not identity ("Core™ i5" and "Core i5" are equal).
_MARKS = re.compile("[®™℠]")
_BARE_DECIMAL = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")
_POSITIVE_INTEGER = re.compile(r"[0-9]+\Z")


def is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    missing = pd.isna(value)
    return bool(missing) if isinstance(missing, bool) else False


def text_key(value: object) -> str | None:
    """NFC, trademark marks removed, whitespace collapsed, case-folded; punctuation is kept."""
    if is_blank(value):
        return None
    text = _MARKS.sub("", unicodedata.normalize("NFC", str(value)))
    return re.sub(r"\s+", " ", text).strip().casefold() or None


def positive_decimal(value: object) -> Decimal | None:
    """A bare positive decimal such as ``14`` or ``15.60``; units or other text raise ValueError."""
    if is_blank(value):
        return None
    text = str(value).strip()
    if not _BARE_DECIMAL.fullmatch(text):
        raise ValueError
    try:
        number = Decimal(text)
    except InvalidOperation as error:
        raise ValueError from error
    if not number.is_finite() or number <= 0:
        raise ValueError
    return number


def positive_integer(value: object) -> int | None:
    """A bare positive integer; units, signs, or decimals raise ValueError."""
    if is_blank(value):
        return None
    text = str(value).strip()
    if not _POSITIVE_INTEGER.fullmatch(text):
        raise ValueError
    number = int(text)
    if number <= 0:
        raise ValueError
    return number


def core_value_key(field: str, value: object) -> object | None:
    """Comparison key for one core field; ``None`` when blank, ValueError when malformed."""
    if field in {"model", "cpu_series"}:
        return text_key(value)
    if field == "screen_size_in":
        number = positive_decimal(value)
        return None if number is None else number.normalize()
    if field in {"ram_capacity_gb", "storage_capacity_gb"}:
        return positive_integer(value)
    raise ValueError(f"Unsupported core match field: {field}")


def core_key(row: Mapping[str, object]) -> tuple[tuple[object, ...] | None, tuple[str, ...]]:
    """The five-field key, or ``None`` with ``MISSING_<FIELD>``/``INVALID_<FIELD>`` issues."""
    normalized: list[object] = []
    issues: list[str] = []
    for field in CORE_MATCH_COLUMNS:
        try:
            value = core_value_key(field, row.get(field))
        except ValueError:
            issues.append(f"INVALID_{field.upper()}")
            continue
        if value is None:
            issues.append(f"MISSING_{field.upper()}")
        else:
            normalized.append(value)
    return (tuple(normalized) if not issues else None), tuple(issues)
