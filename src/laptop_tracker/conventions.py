"""Value conventions for the identity fields ``model`` and ``cpu_series`` (approved 2026-10-08).

Retailer extraction (``extraction.py``) and the catalog convention report
(``catalog_conventions.py``) both use these rules, so that the two sides produce identical forms.

``model``: where a genuine manufacturer model code is verified, the code is the canonical model,
for example ``ANV15-42-R2UH`` rather than ``Nitro``. The product-family name belongs in
``series``. A model code is recognized only by the explicit token shape in ``model_codes``. When
a labeled model value contains no code, its verified name is kept as stated.

``cpu_series``: tiered processor brands use their family form (``Core i5``, ``Core Ultra 7``,
``Ryzen 7``, ``Ryzen AI 9``, ``Snapdragon X Elite``). Apple chips keep the ``Apple`` prefix and
their tier (``Apple M5``, ``Apple M5 Pro``, ``Apple A18 Pro``). Intel processors without a tier
digit use their processor number (``Intel N150``, ``Celeron N4020``), so distinct processors are
never merged. Matching is by these canonical forms (owner policy, 2026-10-12): ``i5-13420H`` and
``i5-1335U`` are both ``Core i5``, while different forms stated together are ambiguous. The full
processor name stays in ``cpu_model`` and in extraction evidence.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

# Trademark marks are replaced by a space so "Qualcomm®Snapdragon" splits into two words.
_MARKS = re.compile("[®™℠]")

# A model-code token: two or more ASCII letter/digit segments joined by ASCII hyphens, with at
# least one letter, one digit, and MIN_MODEL_CODE_LENGTH letters and digits in total. Examples:
# ANV15-42-R2UH, A14-11M-X3BP, S3607VA-RP181WSM, 15-fc0413au. Dotted part numbers
# (NH.QUCSP.001), unhyphenated part or machine-type numbers (C4FW3PA, 83K0007XPH), short
# fragments ("2-in-1", "5-120U"), and en-dash forms are not codes.
_CODE_TOKEN = re.compile(r"(?<![A-Za-z0-9-])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+(?![A-Za-z0-9-])")
MIN_MODEL_CODE_LENGTH = 6
# Processor, memory, and graphics numbers have the same shape but are never product model codes:
# i5-13420H, R5-5500U, X1-26-100, X1E-78-100, DDR5-4800, LPDDR5X-7500, GTX1650-1660ti, RTX-4050.
_NOT_A_MODEL_CODE = re.compile(
    r"(?:(?:i[3579]|r[3579]|x1[ep]?|(?:lp)?ddr[0-9]x?)-|(?:gtx|rtx|rx|mx)-?[0-9])", re.I
)
# A whole segment that is a quantity with a spec unit (512GB, 120Hz, 16in) is a specification.
# MB and W are deliberately absent: real codes end segments with them (SFG14-75-55MB, -RL011W).
_UNIT_SEGMENT = re.compile(
    r"[0-9]+(?:\.[0-9]+)?(?:gb|tb|hz|mhz|ghz|wh|mah|nits|in|inch)", re.I
)


def model_codes(text: str) -> list[str]:
    """Distinct model-code tokens in ``text``, in order and as written (case kept).

    Tokens are compared case-insensitively for distinctness. No suffix is stripped: ``-WSM`` or a
    trailing letter is part of the code. Callers treat more than one code as ambiguous.
    """
    found: dict[str, str] = {}
    for match in _CODE_TOKEN.finditer(_MARKS.sub(" ", unicodedata.normalize("NFC", text))):
        token = match[0]
        letters_digits = token.replace("-", "")
        if (
            len(letters_digits) < MIN_MODEL_CODE_LENGTH
            or not re.search(r"[A-Za-z]", token)
            or not re.search(r"[0-9]", token)
            or _NOT_A_MODEL_CODE.match(token)
            or any(_UNIT_SEGMENT.fullmatch(segment) for segment in token.split("-"))
        ):
            continue
        found.setdefault(token.casefold(), token)
    return list(found.values())


def is_model_code(value: str) -> bool:
    """True when the whole value (ignoring surrounding whitespace) is exactly one model code."""
    codes = model_codes(value)
    return len(codes) == 1 and codes[0] == value.strip()


# CPU-series rules: (pattern, canonical form). Each rule names one processor family or, for
# untiered Intel lines, one processor number. Text is NFC with marks replaced by spaces.
CPU_SERIES_RULES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    (re.compile(r"\bCore\s+Ultra\s+([3579])\b", re.I), lambda m: f"Core Ultra {m[1]}"),
    (re.compile(r"\bCore\s+i([3579])\b", re.I), lambda m: f"Core i{m[1]}"),
    # A complete Intel Core processor number without the "Core" word ("Raptor Lake i5-13420H",
    # "i7-1355U", "i3-1115G4"): tier, hyphen, a 4-5 digit SKU, and an optional letter suffix.
    # Bare "i5", "i5-N305", or a number glued to other text ("Inteli5-13450HX") is not enough.
    (
        re.compile(r"(?<![A-Za-z0-9-])i([3579])-[0-9]{4,5}(?:[A-Z]{1,2}[0-9]?)?(?![A-Za-z0-9-])"),
        lambda m: f"Core i{m[1]}",
    ),
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
    # Apple A-series (catalog: MacBook Neo, A18 Pro). The "Apple" prefix is required.
    (
        re.compile(r"\bApple\s+A(1[0-9])(?:\s+(Pro))?\b", re.I),
        lambda m: f"Apple A{m[1]}" + (f" {m[2].title()}" if m[2] else ""),
    ),
    # Intel N-series without a tier digit ("Intel® Processor N150", "Intel N150"): the processor
    # number is kept, so N100, N150, and N200 stay distinct. "Intel Core i3-N305" is Core i3.
    (
        re.compile(r"\bIntel\s+(?:Processor\s+)?N([0-9]{2,3})\b", re.I),
        lambda m: f"Intel N{m[1]}",
    ),
    # Celeron has no tier digit either: "Celeron N4020" and "Celeron N4500" stay distinct.
    (re.compile(r"\bCeleron\s+(N[0-9]{4})\b", re.I), lambda m: f"Celeron {m[1].upper()}"),
)

# Apple chip names as written without the brand prefix, e.g. "M5", "M4 Max", "A18 Pro".
_BARE_APPLE_CHIP = re.compile(r"(?:M[1-9](?: (?:Pro|Max|Ultra))?|A1[0-9](?: Pro)?)", re.I)


def parse_text(text: str) -> str:
    return re.sub(r"\s+", " ", _MARKS.sub(" ", unicodedata.normalize("NFC", text))).strip()


def cpu_series_forms(text: str) -> set[str]:
    """Every canonical CPU-series form stated in ``text``; more than one is ambiguous."""
    text = parse_text(text)
    return {form(match) for pattern, form in CPU_SERIES_RULES for match in pattern.finditer(text)}


def is_canonical_cpu_series(value: str) -> bool:
    """True when ``value`` is already exactly one supported canonical form."""
    return cpu_series_forms(value) == {value.strip()}


def apple_chip_form(value: str) -> str | None:
    """``Apple <chip>`` for a bare Apple chip name such as ``M5`` or ``A18 Pro``, else None.

    The caller must already know that the product is an Apple product; nothing is inferred here.
    """
    text = parse_text(value)
    if not _BARE_APPLE_CHIP.fullmatch(text):
        return None
    forms = cpu_series_forms(f"Apple {text}")
    return forms.pop() if len(forms) == 1 else None
