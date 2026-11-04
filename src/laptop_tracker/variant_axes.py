"""Option-axis interpretation of Shopify product variants (2026-10-22).

A product page lists its variants as option values: ``McbookNeo SILVER 512GB`` (PC Central's
MacBook Neo, one option named after the product) or ``16GB DDR5 RAM / 512GB SSD``. Until now a
value counted as RAM or storage only when it repeated ``RAM`` or ``SSD``; a colour plus a bare
capacity identified nothing. This module reads the axes from the page's own metadata instead:

- **Named axis.** An option named Storage, SSD, Capacity or Memory, RAM says what its values are.
  The page's option name is the evidence.
- **Composite axis.** A name that says nothing (the product's own name) with values ending in a
  bare capacity. The capacity is storage only when the page's own statements say so: a
  ``Storage:`` statement lists every capacity the options use and no ``Memory:`` statement lists
  one of them. Otherwise its role stays unestablished and the variants stay in review.
- **Shared RAM.** A page-level memory statement (a ``Memory:`` line, or ``8GB Unified Memory`` in
  the title) is the RAM of every variant only when every such statement names the same single
  amount and no option axis varies memory. Several amounts, alternatives (``8GB or 16GB``) or
  disagreement leave RAM unknown with scope ``NOT_ESTABLISHED``.

Everything here is discovery and binding evidence: the readings carry their basis and scope, they
decide which variants are worth collecting, and they let the adapter record how an installed
capacity was read. Identity is still decided by the extractor and the matcher from the
collected page. No value is ever taken from a target, and no price is read here.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache

# Scopes of a RAM reading.
RAM_OPTION_AXIS = "OPTION_AXIS"
RAM_PRODUCT_LEVEL = "PRODUCT_LEVEL_UNIFORM"
RAM_NOT_ESTABLISHED = "NOT_ESTABLISHED"

# Axis roles.
ROLE_STORAGE = "STORAGE"
ROLE_RAM = "RAM"
ROLE_COLOR = "COLOR"
ROLE_COMPOSITE = "COMPOSITE"
ROLE_UNKNOWN = "UNKNOWN"

# Reason codes of a reading that could not be established.
CAPACITY_ROLE_UNESTABLISHED = "CAPACITY_ROLE_UNESTABLISHED"
CAPACITY_ROLE_AMBIGUOUS = "CAPACITY_ROLE_AMBIGUOUS"
CAPACITY_NOT_LISTED = "CAPACITY_NOT_LISTED_BY_PAGE_STATEMENT"
RAM_STATEMENTS_DISAGREE = "RAM_STATEMENTS_DISAGREE"
RAM_STATEMENT_NOT_UNIFORM = "RAM_STATEMENT_NOT_UNIFORM"

# A unit glued to a storage word ("512gbSSD") is still a capacity; "GBps" and "GB/s" are not.
_CAPACITY = re.compile(
    r"(?<![0-9.])([0-9]{1,4})\s*(GB|TB)(?:\b(?!/s|ps)|(?=ssd|sdd|hdd|nvme|emmc))", re.I
)
_STORAGE_NAME = re.compile(
    r"^\s*(?:internal\s+|total\s+|storage\s+)?(?:storage|ssd|hdd|hard\s*(?:disk|drive)|drive|disk|"
    r"capacity)\b", re.I)
_RAM_NAME = re.compile(r"^\s*(?:system\s+|installed\s+)?(?:ram|memory)\b", re.I)
_COLOR_NAME = re.compile(r"^\s*(?:colou?rs?|finish|shade)\b", re.I)
# Words that make a capacity in an option value something other than a bare capacity.
_RAM_WORDS = re.compile(r"\b(?:ram|(?:lp)?ddr[0-9]x?|memory|unified)\b", re.I)
_STORAGE_WORDS = re.compile(r"\b(?:ssd|sdd|hdd|emmc|nvme|storage)\b", re.I)
_ALTERNATIVE = re.compile(r"\b(?:or|up\s+to|upto|configurable|optional|options?|choose|"
                          r"upgrade[sd]?|from)\b|[/&]", re.I)
_LABEL = re.compile(
    r"(?:^|(?<=[\s.;|•]))(Memory|System\s+memory|RAM|Internal\s+storage|Storage|SSD)\s*:\s*", re.I)
_NEXT_LABEL = re.compile(r"\s[A-Z][A-Za-z/ ]{1,24}:\s")
_TITLE_MEMORY = re.compile(
    r"(?<![0-9.])([0-9]{1,3})\s*GB\s+(?:unified\s+memory|memory|(?:lp)?ddr[0-9]x?\s*ram|ram)\b",
    re.I)


def gigabytes(amount: str, unit: str) -> int:
    return int(amount) * (1000 if unit.upper() == "TB" else 1)


def capacities(text: str) -> list[int]:
    """Capacities in ``text`` in gigabytes (1 TB = 1000 GB), in order."""
    return [gigabytes(m[1], m[2]) for m in _CAPACITY.finditer(text)]


def _two_step_unescape(value: str) -> str:
    """Page text escaped twice (``&amp;amp;``) is read once more."""
    once = html.unescape(value)
    return html.unescape(once) if "&" in once else once


def _plain(fragment: str) -> str:
    return re.sub(r"\s+", " ", _two_step_unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


@dataclass(frozen=True)
class Statement:
    """A page-level statement of memory or storage, with where it came from."""

    source: str
    text: str
    amounts: tuple[int, ...]  # gigabytes
    alternatives: bool  # "8GB or 16GB", "256GB & 512GB", "up to ...": a list, not one value

    def describe(self) -> str:
        return f"{self.source}: '{self.text}'"


@dataclass(frozen=True)
class PageStatements:
    memory: tuple[Statement, ...] = ()
    storage: tuple[Statement, ...] = ()


def statements_from_rows(rows: Sequence[tuple[str, str]], *, title: str = "") -> PageStatements:
    """``Memory:`` / ``Storage:`` statements from ``(source, text)`` page text blocks."""
    memory: list[Statement] = []
    storage: list[Statement] = []
    for source, block in rows:
        text = _plain(block)
        matches = list(_LABEL.finditer(text))
        for match in matches:
            rest = text[match.end():]
            cut = _NEXT_LABEL.search(rest)
            segment = rest[: cut.start()] if cut else rest[:160]
            segment = segment.strip(" .;,")
            amounts = tuple(capacities(segment))
            if not amounts:
                continue
            label = match[1].casefold()
            is_memory = label in {"memory", "system memory", "ram"}
            alternatives = len(amounts) > 1 or bool(_ALTERNATIVE.search(segment))
            statement = Statement(source, f"{match[1]}: {segment}", amounts, alternatives)
            (memory if is_memory else storage).append(statement)
    for found in _TITLE_MEMORY.finditer(_plain(title)):
        memory.append(Statement("title", found[0], (int(found[1]),), False))
    return PageStatements(tuple(memory), tuple(storage))


def statements_from_html(text: str) -> PageStatements:
    """Statements from the page's meta description, product description block, and title."""
    rows: list[tuple[str, str]] = []
    for match in re.finditer(
        r'<meta\s+(?:name|property)="(?:og:)?description"\s+content="([^"]*)"', text, re.I
    ):
        rows.append(("meta description", match[1]))
    start = text.find("product__description")
    if start >= 0:
        stop = text.find("share-button", start)
        rows.append(("product description", text[start: stop if stop > 0 else start + 12000]))
    title = ""
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S | re.I)
    if heading:
        title = heading[1]
    return statements_from_rows(rows, title=title)


@dataclass(frozen=True)
class OptionAxis:
    position: int  # 1-based, as Shopify numbers option1..option3
    name: str
    role: str
    basis: str

    def describe(self) -> str:
        return f"{self.position}:{self.name or '(unnamed)'}={self.role}"


@dataclass(frozen=True)
class VariantReading:
    """What the page's metadata says one variant's installed capacities and colour are."""

    variant_id: str
    storage: str = ""  # gigabytes as digits, or a "512GB + 512GB" component list (never equal
    # to one capacity); blank when not interpreted
    ram: str = ""
    color: str = ""
    color_known: bool = False
    storage_basis: str = ""
    ram_basis: str = ""
    ram_scope: str = ""
    codes: tuple[str, ...] = ()  # reasons a capacity or RAM could not be established

    def evidence(self) -> dict[str, str]:
        return {
            "storage": self.storage, "storage_basis": self.storage_basis,
            "ram": self.ram, "ram_basis": self.ram_basis, "ram_scope": self.ram_scope,
            "color": self.color, "codes": ";".join(self.codes),
        }


@dataclass(frozen=True)
class VariantAxes:
    axes: tuple[OptionAxis, ...]
    readings: Mapping[str, VariantReading] = field(default_factory=dict)

    def describe(self) -> str:
        return "; ".join(axis.describe() for axis in self.axes)


def _axis_role_by_name(name: str) -> str:
    if _STORAGE_NAME.match(name):
        return ROLE_STORAGE
    if _RAM_NAME.match(name):
        return ROLE_RAM
    if _COLOR_NAME.match(name):
        return ROLE_COLOR
    return ROLE_UNKNOWN


def _total(text: str) -> str:
    """``"512"``, ``"1000"``, or the joined ``"512GB + 1TB"`` list for several components."""
    found = list(_CAPACITY.finditer(text))
    if not found:
        return ""
    if len(found) == 1:
        return str(gigabytes(found[0][1], found[0][2]))
    return " + ".join(f"{m[1]}{m[2].upper()}" for m in found)


def _strip_capacity(text: str) -> str:
    return re.sub(r"\s+", " ", _CAPACITY.sub(" ", text)).strip(" /|-")


def read_axes(
    option_names: Sequence[str],
    variants: Sequence[tuple[str, Sequence[str]]],
    statements: PageStatements,
    colors: frozenset[str] = frozenset(),
) -> VariantAxes:
    """Roles of the option axes and a reading per variant.

    ``variants`` are ``(variant id, option values)``. ``colors`` is the lowercase colour
    vocabulary used only to label the non-capacity part of a composite value.
    """
    width = max((len(options) for _, options in variants), default=len(option_names))
    names = [option_names[i] if i < len(option_names) else "" for i in range(width)]
    storage_listed = {a for s in statements.storage for a in s.amounts}
    memory_listed = {a for s in statements.memory for a in s.amounts}
    axes: list[OptionAxis] = []
    storage_at: dict[int, str] = {}  # position -> basis
    ram_at: dict[int, str] = {}
    color_at: set[int] = set()
    composite_at: dict[int, str] = {}
    codes_at: dict[int, tuple[str, ...]] = {}
    for index, name in enumerate(names):
        values = [options[index] for _, options in variants if index < len(options)]
        role = _axis_role_by_name(name)
        if role == ROLE_STORAGE:
            storage_at[index] = f"option '{name}' (explicit storage axis)"
        elif role == ROLE_RAM:
            ram_at[index] = f"option '{name}' (explicit memory axis)"
        elif role == ROLE_COLOR:
            color_at.add(index)
        else:
            bare = [v for v in values if _CAPACITY.search(v) and not _RAM_WORDS.search(v)
                    and not _STORAGE_WORDS.search(v)]
            if bare and len(bare) == len(values):
                amounts = {a for v in values for a in capacities(v)}
                joined = any(len(capacities(v)) != 1 for v in values)
                if joined:
                    codes_at[index] = (CAPACITY_ROLE_AMBIGUOUS,)
                    role = ROLE_UNKNOWN
                elif amounts <= storage_listed and not amounts & memory_listed:
                    listed = ", ".join(f"{a}GB" for a in sorted(amounts))
                    composite_at[index] = (
                        f"option '{name or index + 1}' ends in a bare capacity ({listed}); the "
                        "page's Storage statement lists it: "
                        + "; ".join(s.describe() for s in statements.storage))
                    role = ROLE_COMPOSITE
                elif amounts <= memory_listed and not amounts & storage_listed:
                    ram_at[index] = (
                        f"option '{name or index + 1}' ends in a bare capacity the page's "
                        "memory statement lists: "
                        + "; ".join(s.describe() for s in statements.memory))
                    role = ROLE_RAM
                elif amounts & storage_listed and amounts & memory_listed:
                    codes_at[index] = (CAPACITY_ROLE_AMBIGUOUS,)
                elif amounts - storage_listed - memory_listed and (
                    storage_listed or memory_listed
                ):
                    codes_at[index] = (CAPACITY_NOT_LISTED,)
                else:
                    codes_at[index] = (CAPACITY_ROLE_UNESTABLISHED,)
            if role == ROLE_UNKNOWN and values and colors and all(
                not _CAPACITY.search(v) and set(re.findall(r"[a-z]+", v.casefold())) & colors
                for v in values
            ):
                role = ROLE_COLOR  # Values with no capacity that name a known colour.
                color_at.add(index)
        axes.append(OptionAxis(index + 1, name, role,
                               storage_at.get(index) or ram_at.get(index)
                               or composite_at.get(index) or ";".join(codes_at.get(index, ()))))

    ram_varies = bool(ram_at) or any(
        _RAM_WORDS.search(value) for _, options in variants for value in options)
    shared_ram = ""
    shared_ram_scope = ""
    shared_ram_basis = ""
    shared_codes: tuple[str, ...] = ()
    if not ram_varies and statements.memory:
        distinct = {s.amounts for s in statements.memory}
        if any(s.alternatives for s in statements.memory):
            shared_ram_scope, shared_codes = RAM_NOT_ESTABLISHED, (RAM_STATEMENT_NOT_UNIFORM,)
        elif len(distinct) > 1:
            shared_ram_scope, shared_codes = RAM_NOT_ESTABLISHED, (RAM_STATEMENTS_DISAGREE,)
        else:
            shared_ram = str(next(iter(distinct))[0])
            shared_ram_scope = RAM_PRODUCT_LEVEL
            shared_ram_basis = ("page-level statement(s) naming one memory amount, and no option "
                                "axis varies memory: "
                                + "; ".join(s.describe() for s in statements.memory))
    readings: dict[str, VariantReading] = {}
    for variant_id, options in variants:
        storage = ram = color = ""
        storage_basis = ram_basis = ram_scope = ""
        codes: list[str] = []
        color_known = False
        for index, value in enumerate(options):
            if index in storage_at:
                storage, storage_basis = _total(value), storage_at[index]
            elif index in ram_at:
                ram, ram_basis, ram_scope = _total(value), ram_at[index], RAM_OPTION_AXIS
            elif index in composite_at:
                storage, storage_basis = _total(value), composite_at[index]
                rest = _strip_capacity(value)
                words = re.findall(r"[A-Za-z]+", rest)
                known = [w for w in words if w.casefold() in colors]
                color = " ".join(known) if known else rest
                color_known = bool(known)
            elif index in color_at:
                color, color_known = value, True
            codes += codes_at.get(index, ())
        if not ram and shared_ram_scope:
            ram, ram_basis, ram_scope = shared_ram, shared_ram_basis, shared_ram_scope
            codes += shared_codes
        readings[variant_id] = VariantReading(
            variant_id, storage, ram, color, color_known, storage_basis, ram_basis, ram_scope,
            tuple(dict.fromkeys(codes)),
        )
    return VariantAxes(tuple(axes), readings)


def option_names_from_html(text: str) -> list[str]:
    """Option names of a Shopify product page: Dawn ``<legend>`` of each variant-radios fieldset,
    or the ``<label class="form__label">`` of each variant-selects ``<select>``."""
    radios = re.search(r"<variant-radios\b.*?</variant-radios>", text, re.S | re.I)
    if radios:
        return [_plain(m[1]) for m in re.finditer(
            r"<legend[^>]*>(.*?)</legend>", radios[0], re.S | re.I)]
    selects = re.search(r"<variant-selects\b.*?</variant-selects>", text, re.S | re.I)
    if selects:
        return [_plain(m[1]) for m in re.finditer(
            r'<label[^>]*class="[^"]*form__label[^"]*"[^>]*for="Option-[^"]*"[^>]*>(.*?)</label>',
            selects[0], re.S | re.I)]
    return []


def variant_rows_from_html(text: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Variant ID -> ``(title, option values)`` from the picker's variant JSON (``option1..3``)."""
    rows: dict[str, tuple[str, tuple[str, ...]]] = {}
    for block in re.findall(
        r'<script[^>]+type="application/json"[^>]*>(.*?)</script>', text, re.S | re.I
    ):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("variants") if isinstance(
            data, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            options = tuple(
                re.sub(r"\s+", " ", str(item[key])).strip() for key in ("option1", "option2",
                                                                          "option3")
                if isinstance(item.get(key), str) and item[key].strip()
            )
            title = item.get("title") if isinstance(item.get("title"), str) else ""
            rows.setdefault(str(item["id"]), (re.sub(r"\s+", " ", title).strip(), options))
    return rows


@cache
def known_colors() -> frozenset[str]:
    """Lowercase colour words the reviewed product-name registry records (vocabulary only)."""
    from .product_names import default_registry

    return frozenset(
        color.casefold() for entry in default_registry().entries for color in entry.colours
    )
