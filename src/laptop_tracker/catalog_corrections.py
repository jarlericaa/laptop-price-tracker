"""Catalog binding audit: where a SOURCE_MODEL_MAP link contradicts its listing (2026-11-03).

A mapped Shopee variation is bound to a ``TARGET_MASTER`` configuration. This module audits those
bindings and the targets behind them and writes a **correction queue for the owner**. It never
decides anything: every row says ``applies_automatically=FALSE``, quotes the text it rests on,
states what is uncertain, and recommends an owner action in words. It does not clear, edit, remap
or approve anything, and a target is never edited to fit a variation: the target may be right for
the product it was reviewed from.

Finding classes (one binding may carry several; the first of this order is its primary class):

- ``BRAND_MISMATCH``: the parent title names a brand and the target's brand, or the brand its model
  name states, is another one (HP listing, Dell target).
- ``VARIATION_CONTRADICTS_TARGET``: the selected variation's own text states a value the target
  records differently. The four fields of ``variation_specs`` are compared exactly as the ranking
  compares them; in addition an unlabeled ``RAM/storage`` pair after a processor token
  (``I3-7GEN/4GB/128GB``, the convention the catalog's own capacity reviews use) is read for RAM
  and storage, and shown as ``VARIATION_PAIR`` so it is never mistaken for a labeled statement.
- ``SIBLING_FAN_IN``: sibling variations of one parent listing (same ``shop_id|item_id``) are bound
  to targets of different brands, or to different models that the listing and variations do not
  name. Reported once per parent and noted on every sibling.
- ``TARGET_INTERNAL_CONFLICT``: the target contradicts itself (``cpu_model`` against ``cpu_series``,
  brand against the brand its model names, incomplete core identity, inactive), judged with
  ``verified_top._target_issue`` plus the same checks without its listing-only short circuit.
- ``MODEL_NOT_STATED_BY_LISTING``: the target has a specific model that neither the parent title nor
  the variation name states (a ProBook 450 G5 target for a generic "Hp Laptop" listing).
- ``LISTING_ONLY_BINDING``: a variation is bound to a ``LISTING_ONLY`` target, which is tracked as
  one listing and cannot serve a configuration comparison.

Variation-level rows cover the eligible pool; target-level rows cover every target any
``SOURCE_MODEL_MAP`` row points to (including mapped ids outside this pool). ``MATCH_REVIEW`` and
the target's notes are shown beside a finding as evidence; they never select or override anything.
Pure functions over plain CSV rows; the caller owns all file access.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .catalog_conventions import cpu_model_families
from .normalization import core_key, text_key
from .selection_audit import review_evidence
from .target_master import CONFIGURATION, LISTING_ONLY, effective_tracking_mode
from .variation_specs import (
    COMPARED_FIELDS,
    VARIATION,
    FieldReading,
    SpecConflict,
    read_variation_specs,
    spec_conflicts,
)
from .verified_top import _target_issue

QUEUE_VERSION = "catalog-correction-queue-1"

BRAND_MISMATCH = "BRAND_MISMATCH"
VARIATION_CONTRADICTS_TARGET = "VARIATION_CONTRADICTS_TARGET"
SIBLING_FAN_IN = "SIBLING_FAN_IN"
TARGET_INTERNAL_CONFLICT = "TARGET_INTERNAL_CONFLICT"
MODEL_NOT_STATED = "MODEL_NOT_STATED_BY_LISTING"
LISTING_ONLY_BINDING = "LISTING_ONLY_BINDING"
CLASS_ORDER = (
    BRAND_MISMATCH,
    VARIATION_CONTRADICTS_TARGET,
    SIBLING_FAN_IN,
    TARGET_INTERNAL_CONFLICT,
    MODEL_NOT_STATED,
    LISTING_ONLY_BINDING,
)

SCOPE_VARIATION = "VARIATION_BINDING"
SCOPE_PARENT = "PARENT_LISTING"
SCOPE_TARGET = "TARGET"

PAIR_SOURCE = (
    "VARIATION_PAIR"  # an unlabeled RAM/storage pair read by this module, not variation_specs
)
P1_RANK, P2_RANK = 100, 300  # by the variation's ranking_gmv_usd order (eligible_rank)
EXCERPT = 220

QUEUE_COLUMNS: tuple[str, ...] = (
    "queue_id",
    "scope",
    "priority",
    "finding_class",
    "all_finding_classes",
    "eligible_rank",
    "ranking_gmv_usd",
    "selection_status",
    "source_model_id",
    "source_key",
    "shop_name",
    "listing_title",
    "variation_name",
    "current_target_id",
    "target_brand",
    "target_model",
    "target_specs",
    "conflicting_fields",
    "quoted_variation_text",
    "listing_brand",
    "sibling_summary",
    "existing_targets_with_stated_identity",
    "binding_basis",
    "target_notes_excerpt",
    "match_review",
    "recommended_owner_action",
    "uncertainty",
    "applies_automatically",
)

# Brand names and unmistakable product lines. A text that names two different brands resolves to
# none (it may be a compatibility list), never to the first one.
_BRAND_WORDS: dict[str, tuple[str, ...]] = {
    "Apple": ("apple", "macbook"),
    "HP": ("hp", "hewlett", "probook", "elitebook", "pavilion", "omen", "victus", "zbook"),
    "Dell": ("dell", "latitude", "latitde", "inspiron", "vostro", "xps", "alienware"),
    "Lenovo": ("lenovo", "thinkpad", "ideapad", "thinkbook", "legion"),
    "ASUS": ("asus", "vivobook", "zenbook", "rog", "tuf"),
    "Acer": ("acer", "aspire", "swift", "nitro", "predator", "travelmate"),
    "MSI": ("msi",),
    "Fujitsu": ("fujitsu", "lifebook"),
    "Toshiba": ("toshiba", "dynabook", "satellite", "tecra"),
    "Samsung": ("samsung",),
    "Huawei": ("huawei", "matebook"),
    "Microsoft": ("microsoft",),
    "LG": ("lg",),
    "Gigabyte": ("gigabyte", "aorus"),
    "Razer": ("razer",),
    "Xiaomi": ("xiaomi",),
    "Panasonic": ("panasonic", "toughbook"),
    "Chuwi": ("chuwi",),
    "Infinix": ("infinix",),
    "Honor": ("honor",),
    "Realme": ("realme",),
    "Tecno": ("tecno",),
    "Vaio": ("vaio",),
    "Avita": ("avita",),
}
_WORD_TO_BRAND = {word: brand for brand, words in _BRAND_WORDS.items() for word in words}
_WORD = re.compile(r"[A-Za-z0-9]+")
_GENERIC_WORDS = frozenset(
    {"laptop", "notebook", "ultrabook", "series", "gaming", "pc", "computer", "the", "and", "with"}
)
_CORE_TIER = re.compile(r"(?<![A-Za-z0-9])[iI]([3579])(?![A-Za-z0-9])")
_DATED_NOTE = re.compile(r"^\d{4}-\d{2}-\d{2}:")
_RECORDED_VARIATION = re.compile(
    r"(?:Variation model_name|Selected variation(?: \([^)\n]*\))?|Shopee model(?: name)?)\s*:\s*"
    r"([^\n|]+)",
    re.I,
)
_VALID_RAM_GB = frozenset({2, 3, 4, 6, 8, 12, 16, 18, 20, 24, 32, 36, 48, 64})
_VALID_STORAGE_GB = frozenset(
    {16, 32, 64, 120, 128, 240, 250, 256, 320, 480, 500, 512, 750, 1000, 1024, 2000, 2048}
)
# "4GB/128GB", "4/128G", "8G-256GB": two capacities with a separator and nothing labeling them.
_PAIR = re.compile(
    r"(?<![A-Za-z0-9.])([0-9]{1,2})\s*(?:GB|G)?\s*[/\-+]\s*([0-9]{1,4})\s*(GB|TB|G)?(?![A-Za-z0-9])",
    re.I,
)


def _t(row: Mapping[str, object] | None, name: str) -> str:
    value = (row or {}).get(name)
    return "" if value is None else str(value).strip()


def _excerpt(text: str, width: int = EXCERPT) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:width]


# Brand -------------------------------------------------------------------------------------


def brands_in_text(text: str) -> list[str]:
    """Distinct canonical brands whose name or product line occurs as a whole word, in order."""
    found: list[str] = []
    for word in _WORD.findall(text or ""):
        brand = _WORD_TO_BRAND.get(word.casefold())
        if brand and brand not in found:
            found.append(brand)
    return found


def listing_brand(item_name: str) -> tuple[str, str]:
    """``(brand, quoted word)`` the parent title names, or ``("", "")`` when none or several."""
    brands = brands_in_text(item_name)
    if len(brands) != 1:
        return "", ""
    for word in _WORD.findall(item_name):
        if _WORD_TO_BRAND.get(word.casefold()) == brands[0]:
            return brands[0], word
    return brands[0], ""


def target_brand(row: Mapping[str, object]) -> tuple[str, str]:
    """``(brand, how)``: the recorded brand, else the brand the model name states, else unknown."""
    recorded = _t(row, "brand")
    if recorded:
        named = brands_in_text(recorded)
        return (named[0] if len(named) == 1 else recorded), "RECORDED"
    named = brands_in_text(_t(row, "model"))
    if len(named) == 1:
        return named[0], "INFERRED_FROM_MODEL"
    return "", "UNKNOWN"


# Variation reading -------------------------------------------------------------------------


def _pair_capacities(variation_name: str) -> tuple[str, str, str] | None:
    """``(ram, storage, quoted)`` from a single unlabeled RAM/storage pair, or ``None``.

    Both numbers must be plausible sizes (RAM then storage, in that order) and the pair must be
    the only candidate in the name; anything else is left unknown.
    """
    found: list[tuple[str, str, str]] = []
    for match in _PAIR.finditer(variation_name or ""):
        ram = int(match[1])
        unit = (match[3] or "GB").upper()
        storage = Decimal(match[2]) * (1000 if unit == "TB" else 1)
        try:
            storage_gb = int(storage)
        except (InvalidOperation, ValueError):
            continue
        if ram in _VALID_RAM_GB and storage_gb in _VALID_STORAGE_GB:
            found.append((str(ram), str(storage_gb), match[0].strip()))
    return found[0] if len(found) == 1 else None


def read_binding_specs(item_name: str, variation_name: str) -> dict[str, FieldReading]:
    """``variation_specs`` readings plus the unlabeled pair for RAM and storage, where unread."""
    readings = dict(read_variation_specs(item_name, variation_name))
    pair = _pair_capacities(variation_name)
    if pair:
        for name, value in (("ram_capacity_gb", pair[0]), ("storage_capacity_gb", pair[1])):
            if not readings[name].value:
                readings[name] = FieldReading(
                    name, value, PAIR_SOURCE, pair[2], "an unlabeled RAM/storage pair"
                )
    return readings


def contradictions(
    readings: Mapping[str, FieldReading], target: Mapping[str, object]
) -> list[SpecConflict]:
    """Fields the variation states that the target records differently (never mutates either)."""
    return spec_conflicts(readings, target)


def stated_identity_targets(
    readings: Mapping[str, FieldReading],
    brand: str,
    targets: Sequence[Mapping[str, str]],
    *,
    exclude: str = "",
    limit: int = 5,
) -> list[str]:
    """Existing targets of the listing's brand that record every stated cpu/RAM/storage value.

    Information for the owner only: the variation states no model or screen, so several (or no
    honest) targets may fit, and nothing here maps anything.
    """
    stated = {
        name: readings[name].value
        for name in ("cpu_series", "ram_capacity_gb", "storage_capacity_gb")
        if readings[name].value
    }
    if not brand or len(stated) < 2:
        return []
    found: list[str] = []
    for row in targets:
        if _t(row, "target_id") == exclude or target_brand(row)[0].casefold() != brand.casefold():
            continue
        if effective_tracking_mode(row.get("tracking_mode")) != CONFIGURATION:
            continue
        if all(
            (text_key(_t(row, name)) or "") == (text_key(value) or "")
            or _numeric_equal(_t(row, name), value)
            for name, value in stated.items()
        ):
            found.append(_t(row, "target_id"))
    return found[:limit]


def _numeric_equal(left: str, right: str) -> bool:
    try:
        return Decimal(left) == Decimal(right)
    except InvalidOperation:
        return False


# Targets ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetIssue:
    code: str
    detail: str


def target_internal_issues(row: Mapping[str, object]) -> list[TargetIssue]:
    """How the target contradicts itself, independent of its tracking mode.

    ``verified_top._target_issue`` is what the ranking applies (its reason code is reported by the
    caller); it stops at ``LISTING_ONLY`` before looking at fields, so the field checks run here.
    """
    issues: list[TargetIssue] = []
    if _t(row, "status") and _t(row, "status") != "ACTIVE":
        issues.append(TargetIssue("TARGET_INACTIVE", "Target status is not ACTIVE."))
    cpu_series, cpu_model = _t(row, "cpu_series"), _t(row, "cpu_model")
    if cpu_series and cpu_model:
        families = cpu_model_families(cpu_model, text_key(row.get("brand")) == "apple")
        if families and cpu_series not in families:
            issues.append(
                TargetIssue(
                    "TARGET_CPU_MODEL_CONFLICT",
                    f"cpu_model {cpu_model!r} indicates {', '.join(sorted(families))}, but "
                    f"cpu_series is {cpu_series!r}.",
                )
            )
    recorded = _t(row, "brand")
    if recorded:
        named = brands_in_text(_t(row, "model"))
        brand = target_brand(row)[0]
        if len(named) == 1 and named[0].casefold() != brand.casefold():
            issues.append(
                TargetIssue(
                    "TARGET_BRAND_MODEL_CONFLICT",
                    f"brand is {recorded!r} but the model name {_t(row, 'model')!r} "
                    f"names {named[0]}.",
                )
            )
    if effective_tracking_mode(row.get("tracking_mode")) == CONFIGURATION:
        _, missing = core_key(row)
        if missing:
            issues.append(TargetIssue("TARGET_CORE_INCOMPLETE", ", ".join(missing)))
    return issues


def _first_option_note(cpu_model: str, titles: Iterable[str]) -> str:
    """When ``cpu_model`` is the first processor a bound multi-option parent title lists."""
    for title in titles:
        tiers = [m[1] for m in _CORE_TIER.finditer(title)]
        if len(set(tiers)) > 1 and cpu_model and f"i{tiers[0]}" in cpu_model.casefold():
            return (
                f" cpu_model {cpu_model!r} matches the first processor of the multi-option parent "
                f"title {_excerpt(title, 70)!r}, which describes no one configuration."
            )
    return ""


def latest_notes(target: Mapping[str, object], count: int = 2) -> str:
    """The newest dated note entries of a target (they are appended in date order)."""
    entries = [
        _excerpt(line)
        for line in _t(target, "notes").splitlines()
        if _DATED_NOTE.match(line.strip())
    ]
    return " || ".join(entries[-count:]) or _excerpt(_t(target, "notes"))


def target_specs(row: Mapping[str, object] | None) -> str:
    if not row:
        return ""
    parts = [
        f"{label}={_t(row, name) or '-'}"
        for label, name in (
            ("cpu_series", "cpu_series"),
            ("cpu_model", "cpu_model"),
            ("ram_gb", "ram_capacity_gb"),
            ("storage_gb", "storage_capacity_gb"),
            ("screen_in", "screen_size_in"),
        )
    ]
    return "; ".join(parts + [f"tracking_mode={effective_tracking_mode(row.get('tracking_mode'))}"])


def binding_basis(target: Mapping[str, object], variation_name: str) -> tuple[str, str]:
    """``(state, recorded variation names)``: does the target's history describe *this* variation.

    ``CURRENT`` when the target's reviewed notes name the selected variation, ``STALE`` when they
    name other variation(s) only (the seller's variation changed under the same model id after the
    review), ``UNRECORDED`` when they name none.
    """
    text = f"{_t(target, 'variant_notes')}\n{_t(target, 'notes')}"
    wanted = (variation_name or "").strip()
    if wanted and wanted in text:
        return "CURRENT", wanted
    recorded: list[str] = []
    for match in _RECORDED_VARIATION.finditer(text):
        name = match[1].strip()
        if name and len(name) <= 80 and name not in recorded:
            recorded.append(name)
    first_line = _t(target, "variant_notes").splitlines()[:1]
    if not recorded and first_line and not first_line[0].startswith("Parent item_name"):
        recorded.append(first_line[0].strip())
    return ("STALE", " | ".join(recorded[:3])) if recorded else ("UNRECORDED", "")


# Model naming -------------------------------------------------------------------------------


def model_tokens(model: str) -> list[str]:
    return [
        word.casefold()
        for word in _WORD.findall(model or "")
        if word.casefold() not in _GENERIC_WORDS and word.casefold() not in _WORD_TO_BRAND
    ]


def model_stated(model: str, *texts: str) -> bool | None:
    """Whether the texts state the model (a distinguishing token); ``None`` when it has none."""
    tokens = model_tokens(model)
    if not tokens:
        return None
    words = {word.casefold() for text in texts for word in _WORD.findall(text or "")}
    distinguishing = [t for t in tokens if any(c.isdigit() for c in t)] or tokens
    return any(token in words for token in distinguishing)


# Queue -------------------------------------------------------------------------------------


@dataclass
class CorrectionQueue:
    rows: list[dict[str, str]]
    details: dict[str, dict[str, object]]  # queue_id -> full evidence for the JSON
    summary: dict[str, object] = field(default_factory=dict)


def _priority(rank: str, selection_status: str) -> str:
    try:
        number = int(rank)
    except ValueError:
        return "P3"
    if selection_status == "SELECTED" or number <= P1_RANK:
        return "P1"
    return "P2" if number <= P2_RANK else "P3"


def _name(target: Mapping[str, object] | None) -> str:
    """Brand and model as one phrase, without repeating a brand the model already starts with."""
    brand, model = target_brand(target or {})[0], _t(target, "model")
    if brand and model.casefold().startswith(brand.casefold()):
        return model
    return f"{brand} {model}".strip() or "no brand or model recorded"


def _gmv(value: str) -> Decimal:
    try:
        return Decimal(value or "0")
    except InvalidOperation:
        return Decimal(0)


def _order(classes: Iterable[str]) -> list[str]:
    present = set(classes)
    return [name for name in CLASS_ORDER if name in present]


def _action(
    classes: Sequence[str],
    row: Mapping[str, str],
    target: Mapping[str, object] | None,
    pieces: Mapping[str, str],
) -> str:
    """The recommended owner action, in words. It never proposes editing the target to fit."""
    tid = _t(target, "target_id")
    model = _t(target, "model")
    label = f"{tid} ({_name(target)})"
    stated = pieces.get("stated", "")
    lines: list[str] = []
    if BRAND_MISMATCH in classes:
        lines.append(
            f"Review: the listing names {pieces.get('listing_brand', '?')} but {label} is a "
            f"different brand. Decide whether source_model_id {row.get('source_model_id', '')} "
            "should stay bound to it; the likely fix is to unmap it (or re-identify it from the "
            "seller's own label). "
        )
    if VARIATION_CONTRADICTS_TARGET in classes:
        lines.append(
            f"Review: the variation states {stated or 'values the target does not record'}; "
            f"{tid} records otherwise. "
            + (
                f"Existing {pieces.get('listing_brand', '')} targets with the stated identity: "
                f"{pieces['candidates']} (the variation names no model or screen, so confirm "
                "before any mapping). "
                if pieces.get("candidates")
                else "No existing configuration target with the stated identity was found: "
                "create or identify a correct target through the catalog review, or leave the "
                "variation unmapped. "
            )
        )
    if SIBLING_FAN_IN in classes:
        lines.append(
            "Review the sibling bindings of this parent listing together: its variations are "
            "bound to different brands/models that the listing does not name. "
        )
    if TARGET_INTERNAL_CONFLICT in classes:
        lines.append(
            f"{tid} contradicts itself ({pieces.get('internal', '')}). Re-establish its identity "
            "from evidence for the product it was reviewed from; clearing one field does not "
            "settle which configuration it is. "
        )
    if MODEL_NOT_STATED in classes:
        lines.append(
            f"Neither the title nor the variation names the model {model!r}; confirm the model "
            "from the seller's label before relying on this binding. "
        )
    if LISTING_ONLY_BINDING in classes:
        lines.append(
            f"{tid} is LISTING_ONLY (one tracked listing) but is bound to one variation of a "
            "listing that sells several configurations: confirm that is intended, or give the "
            "variation its own CONFIGURATION target. "
        )
    if tid and classes and classes[0] != LISTING_ONLY_BINDING:
        lines.append(f"Do not edit TARGET_MASTER row {tid} to fit this variation.")
    return "".join(lines).strip()


def build_correction_queue(
    ranked: Sequence[Mapping[str, str]],
    targets: Sequence[Mapping[str, str]],
    source_model_map: Sequence[Mapping[str, str]],
    review_rows: Sequence[Mapping[str, str]] = (),
) -> CorrectionQueue:
    """The owner's correction queue. Reads its arguments only; the inputs are never changed."""
    by_id = {_t(row, "target_id"): row for row in targets}
    links = {_t(row, "source_model_id"): _t(row, "target_id") for row in source_model_map}
    reviews = {_t(row, "source_model_id"): row for row in review_rows}

    # Bound variations of the pool, by target and by parent.
    bound: list[dict[str, str]] = []
    for row in ranked:
        target_id = _t(row, "target_id") or links.get(_t(row, "source_model_id"), "")
        if target_id:
            bound.append({**row, "target_id": target_id})
    by_parent: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_target: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in bound:
        by_parent[f"{_t(row, 'shop_id')}|{_t(row, 'item_id')}"].append(row)
        by_target[row["target_id"]].append(row)

    # Target-level defects over every target any SOURCE_MODEL_MAP row points to.
    mapped_by_target: dict[str, list[str]] = defaultdict(list)
    for model_id, target_id in links.items():
        mapped_by_target[target_id].append(model_id)
    target_defects: dict[str, list[TargetIssue]] = {}
    for target_id in mapped_by_target:
        target = by_id.get(target_id)
        if target is None:
            target_defects[target_id] = [
                TargetIssue("MAPPED_TARGET_MISSING", "target_id is not in TARGET_MASTER.")
            ]
            continue
        found = target_internal_issues(target)
        if found:
            target_defects[target_id] = found

    rows: list[dict[str, str]] = []
    details: dict[str, dict[str, object]] = {}

    # Parent-level analysis: do sibling bindings fan out to unrelated brands/models?
    fan_in: dict[str, dict[str, object]] = {}
    for parent, siblings in by_parent.items():
        if len(siblings) < 2:
            continue
        brands: dict[str, list[str]] = defaultdict(list)
        models: set[str] = set()
        unnamed: list[str] = []
        for sibling in siblings:
            target = by_id.get(sibling["target_id"])
            if target is None:
                continue
            brand = target_brand(target)[0]
            if brand:
                brands[brand].append(sibling["target_id"])
            model = _t(target, "model")
            if model_tokens(model):
                models.add(model.casefold())
                if not model_stated(
                    model, _t(sibling, "item_name"), _t(sibling, "shopee_model_name")
                ):
                    unnamed.append(sibling["target_id"])
        if len(brands) > 1 or (len(models) > 1 and unnamed):
            fan_in[parent] = {
                "brands": {name: sorted(set(ids)) for name, ids in brands.items()},
                "models_not_named_by_listing": sorted(set(unnamed)),
            }

    def sibling_text(parent: str) -> str:
        return " | ".join(
            f"{_t(s, 'source_model_id')} {_t(s, 'shopee_model_name')[:28]!r} -> {s['target_id']} "
            f"{_name(by_id.get(s['target_id']))}"
            for s in sorted(by_parent[parent], key=lambda r: -float(_t(r, "ranking_gmv_usd") or 0))
        )

    # Variation-level rows.
    for row in bound:
        target_id = row["target_id"]
        target = by_id.get(target_id)
        item, variation = _t(row, "item_name"), _t(row, "shopee_model_name")
        model_id = _t(row, "source_model_id")
        parent = f"{_t(row, 'shop_id')}|{_t(row, 'item_id')}"
        classes: set[str] = set()
        findings: list[str] = []
        quotes: list[str] = []
        pieces: dict[str, str] = {}
        uncertainty: list[str] = []
        readings = read_binding_specs(item, variation)
        stated_parts = [
            f"{readings[name].field}={readings[name].value}"
            for name in COMPARED_FIELDS
            if readings[name].value and readings[name].source in {VARIATION, PAIR_SOURCE}
        ]
        pieces["stated"] = ", ".join(stated_parts)
        detail: dict[str, object] = {"readings": {n: r.describe() for n, r in readings.items()}}

        if target is None:
            classes.add(TARGET_INTERNAL_CONFLICT)
            findings.append(f"mapped target_id {target_id} is not in TARGET_MASTER")
            pieces["internal"] = "target_id not in TARGET_MASTER"
        else:
            listing, quoted_brand = listing_brand(item)
            brand, how = target_brand(target)
            pieces["listing_brand"] = listing
            if listing and brand and listing.casefold() != brand.casefold():
                classes.add(BRAND_MISMATCH)
                shown = _t(target, "brand") if how == "RECORDED" else _t(target, "model")
                findings.append(
                    f'brand: the parent title names {listing!r} ("{quoted_brand}") but the target '
                    f'is {brand!r} ({how.lower().replace("_", " ")}: "{shown}")'
                )
                quotes.append(f'title: "{_excerpt(item, 90)}"')
            elif listing and not brand:
                uncertainty.append("target records no brand and its model names none")
            elif not listing:
                uncertainty.append("the parent title names no single brand")

            found = contradictions(readings, target)
            if found:
                classes.add(VARIATION_CONTRADICTS_TARGET)
                for conflict in found:
                    note = (
                        " (unlabeled RAM/storage pair; the catalog's own capacity reviews read it "
                        "the same way)"
                        if conflict.source == PAIR_SOURCE
                        else ""
                    )
                    findings.append(conflict.describe() + note)
                    quotes.append(f'variation: "{variation}" -> "{conflict.evidence}"')
                detail["field_conflicts"] = [
                    {
                        "field": c.field,
                        "variation_value": c.variation_value,
                        "target_value": c.target_value,
                        "reading_source": c.source,
                        "evidence": c.evidence,
                    }
                    for c in found
                ]
                candidates = stated_identity_targets(readings, listing, targets, exclude=target_id)
                pieces["candidates"] = ", ".join(candidates)

            if parent in fan_in:
                classes.add(SIBLING_FAN_IN)
                findings.append(
                    f"sibling fan-in: the {len(by_parent[parent])} bound variations of this "
                    f"listing resolve to brands {sorted(fan_in[parent]['brands'])} (see "  # type: ignore[arg-type]
                    f"CC-PAR-{parent.replace('|', '-')} and sibling_summary)"
                )
            defects = target_defects.get(target_id, [])
            if defects:
                classes.add(TARGET_INTERNAL_CONFLICT)
                pieces["internal"] = "; ".join(f"{d.code}: {d.detail}" for d in defects)
                findings.append(
                    f"target {target_id} contradicts itself ({pieces['internal']}; "
                    f"see CC-TGT-{target_id})"
                )
            stated = model_stated(_t(target, "model"), item, variation)
            if stated is False and BRAND_MISMATCH not in classes:
                classes.add(MODEL_NOT_STATED)
                findings.append(
                    f"model: target model {_t(target, 'model')!r} appears in neither the parent "
                    "title nor the variation name"
                )
            if effective_tracking_mode(target.get("tracking_mode")) == LISTING_ONLY:
                classes.add(LISTING_ONLY_BINDING)
                findings.append(f"{target_id} is LISTING_ONLY (one tracked listing)")
            basis, recorded = binding_basis(target, variation)
            if basis == "STALE":
                uncertainty.append(
                    f"the target was reviewed from variation {recorded!r}, not the current "
                    f"{variation!r}: this model id's variation changed after the review"
                )
        if not classes:
            continue
        ordered = _order(classes)
        queue_id = f"CC-VAR-{model_id}"
        review = review_evidence(model_id, variation, reviews)
        match_review = (
            f"{review.get('status', '')} {review.get('reviewed_at', '')}; "
            f"variation {review.get('variation_state', '')}"
            + (f"; {review['unresolved_note']}" if review.get("unresolved_note") else "")
            if review.get("review_row") == "FOUND"
            else "NO_MATCH_REVIEW_ROW"
        )
        basis_text = (
            f"{binding_basis(target, variation)[0]}"
            + (
                f" (recorded: {binding_basis(target, variation)[1]})"
                if target and binding_basis(target, variation)[0] == "STALE"
                else ""
            )
            if target
            else ""
        )
        sibling_summary = sibling_text(parent) if len(by_parent[parent]) > 1 else ""
        quoted_values = list(dict.fromkeys(quotes))
        rows.append(
            {
                "queue_id": queue_id,
                "scope": SCOPE_VARIATION,
                "priority": _priority(_t(row, "eligible_rank"), _t(row, "selection_status")),
                "finding_class": ordered[0],
                "all_finding_classes": ";".join(ordered),
                "eligible_rank": _t(row, "eligible_rank"),
                "ranking_gmv_usd": _t(row, "ranking_gmv_usd"),
                "selection_status": _t(row, "selection_status"),
                "source_model_id": model_id,
                "source_key": _t(row, "source_key"),
                "shop_name": _t(row, "shop_name"),
                "listing_title": item,
                "variation_name": variation,
                "current_target_id": target_id,
                "target_brand": target_brand(target)[0] if target else "",
                "target_model": _t(target, "model"),
                "target_specs": target_specs(target),
                "conflicting_fields": " | ".join(findings),
                "quoted_variation_text": " | ".join(quoted_values),
                "listing_brand": pieces.get("listing_brand", ""),
                "sibling_summary": sibling_summary,
                "existing_targets_with_stated_identity": pieces.get("candidates", ""),
                "binding_basis": basis_text,
                "target_notes_excerpt": latest_notes(target) if target else "",
                "match_review": match_review,
                "recommended_owner_action": _action(ordered, row, target, pieces),
                "uncertainty": "; ".join(uncertainty),
                "applies_automatically": "FALSE",
            }
        )
        details[queue_id] = {**detail, "findings": findings, "classes": ordered}

    # Parent-level rows for sibling fan-in.
    for parent, info in fan_in.items():
        siblings = by_parent[parent]
        lead = max(siblings, key=lambda r: float(_t(r, "ranking_gmv_usd") or 0))
        best_rank = min(int(_t(s, "eligible_rank") or 10**9) for s in siblings)
        queue_id = f"CC-PAR-{parent.replace('|', '-')}"
        rows.append(
            {
                "queue_id": queue_id,
                "scope": SCOPE_PARENT,
                "priority": _priority(
                    str(best_rank),
                    "SELECTED"
                    if any(_t(s, "selection_status") == "SELECTED" for s in siblings)
                    else "",
                ),
                "finding_class": SIBLING_FAN_IN,
                "all_finding_classes": SIBLING_FAN_IN,
                "eligible_rank": str(best_rank),
                "ranking_gmv_usd": f"{sum(_gmv(_t(s, 'ranking_gmv_usd')) for s in siblings):.2f}",
                "selection_status": "",
                "source_model_id": ";".join(_t(s, "source_model_id") for s in siblings),
                "source_key": parent,
                "shop_name": _t(lead, "shop_name"),
                "listing_title": _t(lead, "item_name"),
                "variation_name": "",
                "current_target_id": ";".join(sorted({s["target_id"] for s in siblings})),
                "target_brand": ";".join(sorted(info["brands"])),  # type: ignore[arg-type]
                "target_model": "",
                "target_specs": "",
                "conflicting_fields": (
                    f"{len(siblings)} variations of one listing are bound to targets of brands "
                    f"{sorted(info['brands'])} and models not named by the listing: "  # type: ignore[arg-type]
                    f"{info['models_not_named_by_listing']}"
                ),
                "quoted_variation_text": f'title: "{_excerpt(_t(lead, "item_name"), 90)}"',
                "listing_brand": listing_brand(_t(lead, "item_name"))[0],
                "sibling_summary": sibling_text(parent),
                "existing_targets_with_stated_identity": "",
                "binding_basis": "",
                "target_notes_excerpt": "",
                "match_review": "",
                "recommended_owner_action": (
                    "Review the whole listing at once: its variations are one seller's stock "
                    "described by a generic title, yet they are bound to targets of different "
                    "brands/models. Decide per variation whether each binding is evidenced; do "
                    "not edit the targets to fit the variations."
                ),
                "uncertainty": "A multi-model listing that names each model per variation is "
                "not flagged; this one names none of the bound models.",
                "applies_automatically": "FALSE",
            }
        )
        details[queue_id] = {"parent": parent, **info, "siblings": sibling_text(parent)}

    # Target-level rows.
    for target_id, issues in sorted(target_defects.items()):
        target = by_id.get(target_id)
        variations = by_target.get(target_id, [])
        titles = [_t(v, "item_name") for v in variations]
        model_ids = sorted(mapped_by_target[target_id])
        best_rank = min((int(_t(v, "eligible_rank") or 10**9) for v in variations), default=10**9)
        total = sum(_gmv(_t(v, "ranking_gmv_usd")) for v in variations)
        in_pool = ", ".join(_t(v, "source_model_id") for v in variations)
        first_option = _first_option_note(_t(target, "cpu_model"), titles)
        verified = (
            _target_issue({**dict.fromkeys(("status", "cpu_series"), ""), **(target or {})})
            if target
            else None
        )
        queue_id = f"CC-TGT-{target_id}"
        rows.append(
            {
                "queue_id": queue_id,
                "scope": SCOPE_TARGET,
                "priority": _priority(str(best_rank), ""),
                "finding_class": TARGET_INTERNAL_CONFLICT,
                "all_finding_classes": TARGET_INTERNAL_CONFLICT,
                "eligible_rank": "" if best_rank == 10**9 else str(best_rank),
                "ranking_gmv_usd": f"{total:.2f}",
                "selection_status": "",
                "source_model_id": ";".join(model_ids),
                "source_key": "",
                "shop_name": "",
                "listing_title": " | ".join(dict.fromkeys(_excerpt(t, 80) for t in titles)),
                "variation_name": " | ".join(_t(v, "shopee_model_name")[:30] for v in variations),
                "current_target_id": target_id,
                "target_brand": target_brand(target)[0] if target else "",
                "target_model": _t(target, "model"),
                "target_specs": target_specs(target),
                "conflicting_fields": " | ".join(f"{i.code}: {i.detail}" for i in issues)
                + first_option,
                "quoted_variation_text": "",
                "listing_brand": "",
                "sibling_summary": f"mapped source_model_ids: {len(model_ids)}; "
                f"in the eligible pool: {in_pool or 'none'}",
                "existing_targets_with_stated_identity": "",
                "binding_basis": "",
                "target_notes_excerpt": latest_notes(target) if target else "",
                "match_review": "",
                "recommended_owner_action": (
                    f"Review {target_id}: its own fields disagree. Re-establish which "
                    "configuration it is from the evidence for the product it was reviewed "
                    "from, then correct TARGET_MASTER yourself. A bound variation may belong "
                    "to a different target "
                    "altogether (see its VARIATION_BINDING row); clearing one field is not a fix. "
                    f"verified_top reason today: {verified[0] if verified else 'none'}."
                ),
                "uncertainty": (
                    "Which of the conflicting fields is right is not decidable from the catalog; "
                    "the notes record only the reviewed product."
                ),
                "applies_automatically": "FALSE",
            }
        )
        details[queue_id] = {
            "issues": [{"code": i.code, "detail": i.detail} for i in issues],
            "mapped_source_model_ids": model_ids,
            "verified_top_reason": verified[0] if verified else "",
        }

    rows.sort(
        key=lambda r: (
            r["priority"],
            -float(r["ranking_gmv_usd"] or 0),
            r["scope"],
            r["queue_id"],
        )
    )
    return CorrectionQueue(rows, details, _summarize(rows, ranked, bound, links, by_id))


def _summarize(
    rows: Sequence[Mapping[str, str]],
    ranked: Sequence[Mapping[str, str]],
    bound: Sequence[Mapping[str, str]],
    links: Mapping[str, str],
    by_id: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    by_scope_primary: Counter[str] = Counter()
    any_class: Counter[str] = Counter()
    for row in rows:
        by_scope_primary[f"{row['scope']}:{row['finding_class']}"] += 1
        if row["scope"] == SCOPE_VARIATION:
            for name in row["all_finding_classes"].split(";"):
                any_class[name] += 1
    flagged = [r for r in rows if r["scope"] == SCOPE_VARIATION]
    pool_ids = {_t(r, "source_model_id") for r in ranked}
    return {
        "queue_version": QUEUE_VERSION,
        "queue_rows": len(rows),
        "rows_by_scope": dict(Counter(r["scope"] for r in rows)),
        "rows_by_scope_and_primary_class": dict(sorted(by_scope_primary.items())),
        "variation_bindings_by_any_class": {n: any_class[n] for n in CLASS_ORDER if any_class[n]},
        "rows_by_priority": dict(sorted(Counter(r["priority"] for r in rows).items())),
        "eligible_pool_variations": len(ranked),
        "mapped_variations_in_pool": len(bound),
        "flagged_variation_bindings": len(flagged),
        "flagged_selected_top_variations": sum(
            1 for r in flagged if r["selection_status"] == "SELECTED"
        ),
        "flagged_ranking_gmv_usd": f"{sum(_gmv(r['ranking_gmv_usd']) for r in flagged):.2f}",
        "source_model_map_rows": len(links),
        "mapped_ids_outside_the_pool": len(set(links) - pool_ids),
        "mapped_targets": len(set(links.values())),
        "mapped_targets_missing_from_target_master": sum(
            1 for t in set(links.values()) if t not in by_id
        ),
        "applies_automatically": "FALSE for every row",
    }


# Output -----------------------------------------------------------------------------------


def write_csv(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(QUEUE_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def queue_markdown(queue: CorrectionQueue, inputs: Mapping[str, str], top: int = 25) -> str:
    summary = queue.summary
    lines = [
        "# Catalog correction queue",
        "",
        "Proposals for owner review only. Nothing was applied: no Sheet, target, mapping, approval "
        "or history file was changed, and `applies_automatically` is FALSE on every row. A target "
        "is never edited to fit a variation.",
        "",
        "## Counts",
        "",
        f"- Eligible pool variations: {summary['eligible_pool_variations']}; mapped: "
        f"{summary['mapped_variations_in_pool']}; flagged: {summary['flagged_variation_bindings']} "
        f"(of which in the selected Top 100: {summary['flagged_selected_top_variations']}).",
        f"- SOURCE_MODEL_MAP rows: {summary['source_model_map_rows']} "
        f"({summary['mapped_ids_outside_the_pool']} outside this pool, checked at target level "
        f"only); mapped targets: {summary['mapped_targets']}.",
        "",
        "| Scope : primary class | Rows |",
        "|---|---:|",
        *[
            f"| {name} | {count} |"
            for name, count in summary["rows_by_scope_and_primary_class"].items()
        ],  # type: ignore[union-attr]
        "",
        "Variation bindings by any class: "
        + ", ".join(f"{k} {v}" for k, v in summary["variation_bindings_by_any_class"].items()),  # type: ignore[union-attr]
        "",
        f"## Highest-priority rows (first {top})",
        "",
        "| ID | Pri | Class | GMV USD | Target | Variation | Finding |",
        "|---|---|---|---:|---|---|---|",
    ]
    for row in queue.rows[:top]:
        lines.append(
            f"| {row['queue_id']} | {row['priority']} | {row['finding_class']} | "
            f"{float(row['ranking_gmv_usd'] or 0):,.0f} | {row['current_target_id'][:40]} | "
            f"{row['variation_name'][:30]} | "
            f"{row['conflicting_fields'][:260].replace('|', '/')} |"
        )
    lines += ["", "## Inputs", "", *[f"- {name}: `{value}`" for name, value in inputs.items()], ""]
    return "\n".join(lines)


def write_queue_outputs(
    out_dir: Path,
    queue: CorrectionQueue,
    inputs: Mapping[str, str],
    input_hashes: Mapping[str, str],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "catalog_correction_queue.csv", queue.rows)
    document = {
        "queue_version": QUEUE_VERSION,
        "summary": queue.summary,
        "inputs": dict(inputs),
        "input_sha256": dict(input_hashes),
        "rows": [{**row, "evidence": queue.details.get(row["queue_id"], {})} for row in queue.rows],
    }
    (out_dir / "catalog_correction_queue.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / "catalog_correction_queue.md").write_text(
        queue_markdown(queue, inputs), encoding="utf-8"
    )
