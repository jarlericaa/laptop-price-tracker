"""Catalog identity reconciliation for name-identified targets (2026-10-19, second review).

Many catalog targets record a broad family name as ``model`` (``IdeaPad``, ``Aspire``,
``Victus``) while retailers expose specific manufacturer codes. Matching cannot bridge that gap
safely: several distinct coded products share one five-field identity under such a name, so a
retailer offer that "happens to share the specifications" is never evidence of which product a
target is. This module reconciles the catalog from the target's **own original source evidence**
(the Shopee listings that contribute its sales) and the catalog's own reviewed fields:

- ``SUPPORTED_CORRECTION``: every contributing source listing states the same one manufacturer
  model code (or the target's reviewed ``manufacturer_model_code`` is a code no source
  contradicts), no source states contrary specifications, and no other target already has that
  identity. The correction keeps the ``target_id`` and its mappings; the code becomes ``model``
  and ``manufacturer_model_code``; the former name moves to ``series`` (when blank) and to
  ``notes``.
- ``DUPLICATE_OF_EXISTING_TARGET``: another target (existing, or another proposal) already has
  the same complete configuration identity (the code plus screen, CPU family, RAM and storage)
  and no material secondary field (GPU, panel, resolution, part number, ...) differs: the owner
  decides whether to merge (a correction would create a second target for one configuration).
  The same code with another RAM/storage configuration is a different configuration, never a
  duplicate (2026-10-19, independent review); it is flagged for information only.
- ``SAME_CORE_SECONDARY_CONFLICT``: the same complete identity, but a material secondary field
  differs: the owner establishes whether these are distinct products before any change.
- ``MULTIPLE_PRODUCTS``: the sources state different codes (several products or configurations
  under one target): the owner splits or remaps; nothing is repurposed.
- ``CONFLICTING_SOURCE_SPECS``: a source states a code but contradicts the target's
  specifications.
- ``INSUFFICIENT_EVIDENCE``: no source states a code: the target is kept, with an actionable
  entry. Retailer offers with matching specifications are listed only as leads, never as evidence.
- ``NO_CHANGE_MODEL_CODE`` / ``NO_CHANGE_PRODUCT_NAME``: the model already is a code, or a
  reviewed product name (``product_names.py``).

No alias from a family to a code is created, no code is guessed, and nothing here writes Google
Sheets: the corrected snapshot is a local, traceable proposal applied the same way as the
convention proposals (owner edits ``TARGET_MASTER``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .conventions import is_model_code, title_identifiers
from .matching import BLOCKING, SECONDARY_SEVERITY, verified_secondary_difference
from .normalization import CORE_MATCH_COLUMNS, core_value_key
from .product_discovery import clue_comparison, title_clues
from .product_names import MODEL_CODE, PRODUCT_NAME, ProductNameRegistry

RECONCILED_ON = "2026-10-19"
SUPPORTED_CORRECTION = "SUPPORTED_CORRECTION"
DUPLICATE_OF_EXISTING_TARGET = "DUPLICATE_OF_EXISTING_TARGET"
MULTIPLE_PRODUCTS = "MULTIPLE_PRODUCTS"
CONFLICTING_SOURCE_SPECS = "CONFLICTING_SOURCE_SPECS"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
NO_CHANGE_MODEL_CODE = "NO_CHANGE_MODEL_CODE"
NO_CHANGE_PRODUCT_NAME = "NO_CHANGE_PRODUCT_NAME"
SAME_CORE_SECONDARY_CONFLICT = "SAME_CORE_SECONDARY_CONFLICT"
UNRESOLVED_STATUSES = frozenset({DUPLICATE_OF_EXISTING_TARGET, MULTIPLE_PRODUCTS,
                                 CONFLICTING_SOURCE_SPECS, INSUFFICIENT_EVIDENCE,
                                 SAME_CORE_SECONDARY_CONFLICT})
# Secondary fields that tell two configurations with the same five core values apart (the
# matcher's blocking fields, except the model code itself, which is the field being compared).
MATERIAL_SECONDARY_FIELDS: tuple[str, ...] = tuple(
    name for name, severity in SECONDARY_SEVERITY.items()
    if severity == BLOCKING and name not in {"manufacturer_model_code", "brand"}
)

RECONCILIATION_COLUMNS: tuple[str, ...] = (
    "target_id", "rank", "brand", "original_model", "original_series",
    "original_manufacturer_model_code", "screen_size_in", "cpu_series", "ram_capacity_gb",
    "storage_capacity_gb", "gpu_model", "identity_kind", "status", "proposed_model",
    "proposed_series", "proposed_manufacturer_model_code", "applied_in_local_snapshot",
    "source_references", "source_evidence", "source_codes", "excluded_mapped_listing_codes",
    "retailer_corroboration", "retailer_leads_not_evidence", "conflicts", "flags", "action",
)
ACTIONS = {
    DUPLICATE_OF_EXISTING_TARGET: "Owner review: the same complete configuration already has a "
                                  "target. Decide whether to merge, which ID survives, and how "
                                  "mappings and history move; nothing is merged automatically.",
    SAME_CORE_SECONDARY_CONFLICT: "Owner review: the same five core values but a material "
                                  "secondary difference; establish whether these are distinct "
                                  "products before any identity change.",
    MULTIPLE_PRODUCTS: "Split or remap the source listings per product; do not repurpose this "
                       "target for one of them.",
    CONFLICTING_SOURCE_SPECS: "Resolve the conflicting statements against the manufacturer or "
                              "seller before any identity change.",
    INSUFFICIENT_EVIDENCE: "Unresolved under the current evidence and parser: confirm the exact "
                           "manufacturer model from the seller listing, a typed manufacturer "
                           "identifier or manufacturer specifications. Retailer offers with "
                           "the same specifications are not evidence.",
}
_GPU = re.compile(r"\b(?:RTX|GTX)\s*-?\s*([0-9]{4})\b", re.I)
# Seller descriptors shaped like codes (refurbished "X280-i5", "L570-i5-7th-8G", "X13-10th"): a
# processor tier, a generation or a capacity is not part of a manufacturer model code, so such
# a token never becomes a catalog identity.
_DESCRIPTOR_SEGMENT = re.compile(r"i[3579]|[0-9]+(?:st|nd|rd|th)|[0-9]+GB?", re.I)


def is_seller_descriptor(code: str) -> bool:
    return any(_DESCRIPTOR_SEGMENT.fullmatch(part) for part in re.split(r"[-/]", code))


@dataclass
class Reconciliation:
    rows: list[dict[str, str]]
    corrections: dict[str, dict[str, str]] = field(default_factory=dict)


def _text(row: Mapping[str, object], name: str) -> str:
    value = row.get(name, "")
    return "" if value is None or str(value) == "nan" else str(value).strip()


def _source_text(row: Mapping[str, str]) -> str:
    item, variation = row.get("item_name", ""), row.get("shopee_model_name", "")
    return item if not variation or variation == item else f"{item} || {variation}"


def _same(field_name: str, left: str, right: str) -> bool:
    try:
        return core_value_key(field_name, left) == core_value_key(field_name, right)
    except ValueError:
        return False


def _spec_conflicts(text: str, target: Mapping[str, object]) -> list[str]:
    """Core specifications a source text states differently from the target, plus a GPU."""
    core = {name: _text(target, name) for name in CORE_MATCH_COLUMNS}
    clues = clue_comparison(title_clues(text), _ClueTarget(core))
    conflicts = [f"{name}: source {value.split('(', 1)[-1].rstrip(')')} vs catalog {core[name]}"
                 for name, value in clues.items()
                 # A joined "8GB + 512GB" clue (RAM + storage) is not a statement of one field.
                 if value.startswith("DIFFERS") and " + " not in value]
    stated_gpu = {m[1] for m in _GPU.finditer(text)}
    catalog_gpu = {m[1] for m in _GPU.finditer(_text(target, "gpu_model"))}
    if stated_gpu and catalog_gpu and not stated_gpu & catalog_gpu:
        conflicts.append(f"gpu: source RTX/GTX {'/'.join(sorted(stated_gpu))} vs catalog "
                         f"{_text(target, 'gpu_model')}")
    return conflicts


@dataclass(frozen=True)
class _ClueTarget:
    core: Mapping[str, str]


def _identity(row: Mapping[str, object], code: str) -> tuple[object, ...] | None:
    """The complete normalized configuration identity of ``row`` with ``code`` as its model."""
    try:
        return (code.casefold(), *(core_value_key(name, _text(row, name))
                                   for name in CORE_MATCH_COLUMNS if name != "model"))
    except ValueError:
        return None


def _secondary_differences(left: Mapping[str, object], right: Mapping[str, object]) -> list[str]:
    return [
        f"{name}: {_text(left, name)} vs {_text(right, name)}"
        for name in MATERIAL_SECONDARY_FIELDS
        if verified_secondary_difference(name, _text(left, name), _text(right, name))
    ]


def _configuration_differences(left: Mapping[str, object], right: Mapping[str, object]) -> str:
    return ", ".join(
        f"{name} {_text(left, name)} vs {_text(right, name)}"
        for name in CORE_MATCH_COLUMNS
        if name != "model" and not _same(name, _text(left, name), _text(right, name))
    )


def reconcile(
    targets: Sequence[Mapping[str, object]],
    selected: Sequence[Mapping[str, str]],
    contributions: Sequence[Mapping[str, str]],
    registry: ProductNameRegistry,
    retailer_results: Iterable[Mapping[str, str]] = (),
) -> Reconciliation:
    """One reconciliation row per selected target (see the module docstring)."""
    by_id = {_text(row, "target_id"): row for row in targets}
    sources: dict[str, list[Mapping[str, str]]] = {}
    for row in contributions:
        sources.setdefault(row.get("target_id", ""), []).append(row)
    offers_by_code: dict[str, list[Mapping[str, str]]] = {}
    for row in retailer_results:
        if row.get("offer_model"):
            offers_by_code.setdefault(row["offer_model"].casefold(), []).append(row)
    coded: dict[str, list[Mapping[str, object]]] = {}
    for row in targets:
        if is_model_code(_text(row, "model")):
            coded.setdefault(_text(row, "model").casefold(), []).append(row)
    result = Reconciliation([])
    for listed in selected:
        target_id = listed["target_id"]
        target = by_id[target_id]
        model, brand = _text(target, "model"), _text(target, "brand")
        kind = registry.kind(brand, model)
        own = [r for r in sources.get(target_id, []) if r.get("contribution_status") ==
               "CONTRIBUTING"]
        excluded = [r for r in sources.get(target_id, []) if r.get("contribution_status") !=
                    "CONTRIBUTING"]
        per_source = {r.get("model_id", ""): list(title_identifiers(_source_text(r)).model_codes)
                      for r in own}
        descriptors = sorted({code for found in per_source.values() for code in found
                              if is_seller_descriptor(code)})
        per_source = {sid: [code for code in found if not is_seller_descriptor(code)]
                      for sid, found in per_source.items()}
        codes =sorted({code for found in per_source.values() for code in found}, key=str.casefold)
        unique = list({code.casefold(): code for code in codes}.values())
        excluded_codes = sorted({code for r in excluded
                                 for code in title_identifiers(_source_text(r)).model_codes})
        mmc = _text(target, "manufacturer_model_code")
        mmc_code = mmc if is_model_code(mmc) else ""
        conflicts: list[str] = []
        flags: list[str] = []
        proposed = ""
        status = ""
        if kind == MODEL_CODE:
            status = NO_CHANGE_MODEL_CODE
        elif kind == PRODUCT_NAME:
            status = NO_CHANGE_PRODUCT_NAME
        elif len(unique) > 1:
            status = MULTIPLE_PRODUCTS
            conflicts.append("sources state different codes: " + "; ".join(
                f"{sid}: {', '.join(found) or '(none)'}" for sid, found in per_source.items()))
        elif unique and mmc_code and unique[0].casefold() != mmc_code.casefold():
            status = CONFLICTING_SOURCE_SPECS
            conflicts.append(f"sources state {unique[0]}; catalog manufacturer_model_code "
                             f"{mmc_code}")
        else:
            candidate = unique[0] if unique else mmc_code
            silent = [sid for sid, found in per_source.items() if not found]
            if not candidate:
                status = INSUFFICIENT_EVIDENCE
            elif unique and silent and not mmc_code:
                # Some contributing listings name the code, others name nothing: the target may
                # mix products; the owner confirms before the code becomes its identity.
                status = INSUFFICIENT_EVIDENCE
                flags.append(f"code {candidate} stated by {len(own) - len(silent)} of "
                             f"{len(own)} contributing listings")
            else:
                for source in own:
                    conflicts += _spec_conflicts(_source_text(source), target)
                identity = _identity(target, candidate)
                same_code = [row for row in coded.get(candidate.casefold(), [])
                             if _text(row, "target_id") != target_id]
                same_identity = [row for row in same_code
                                 if identity is not None and _identity(row, candidate) == identity]
                for row in same_code:
                    if row not in same_identity:
                        flags.append(
                            f"{candidate} also identifies {_text(row, 'target_id')}, a different "
                            f"configuration ({_configuration_differences(target, row)}); not a "
                            "duplicate")
                differing = {_text(row, "target_id"): _secondary_differences(target, row)
                             for row in same_identity}
                if conflicts:
                    status = CONFLICTING_SOURCE_SPECS
                elif identity is None:
                    status = INSUFFICIENT_EVIDENCE
                    flags.append("the target's own core values are incomplete or invalid")
                elif same_identity and all(differing.values()):
                    status = SAME_CORE_SECONDARY_CONFLICT
                    conflicts += [f"same configuration as {other}, but {'; '.join(found)}"
                                  for other, found in differing.items()]
                elif same_identity:
                    status = DUPLICATE_OF_EXISTING_TARGET
                    conflicts += [f"{candidate} with the same configuration is already {other}"
                                  for other, found in differing.items() if not found]
                else:
                    status = SUPPORTED_CORRECTION
                    proposed = candidate
        screen = _text(target, "screen_size_in")
        if proposed and re.fullmatch(r"[0-9]+", screen) and re.search(
            rf"\b{screen}\b", model
        ) and not any(title_clues(_source_text(s))["screen_size_in"] for s in own):
            flags.append(f"SCREEN_SIZE_UNVERIFIED: catalog screen {screen} may come from the "
                         f"name '{model}'; no source states the size")
        corroboration = [
            f"{r.get('retailer_id')}: {r.get('offer_url')} ({r.get('match_status')}"
            + (f" {r.get('target_id')}" if r.get("target_id") else "") + ")"
            for r in offers_by_code.get((proposed or mmc_code).casefold(), [])
        ] if (proposed or mmc_code) else []
        leads = []
        if status == INSUFFICIENT_EVIDENCE:
            leads = sorted({
                f"{r.get('retailer_id')}: {r.get('offer_model')}"
                for rows in offers_by_code.values() for r in rows
                if r.get("expected_target_id") == target_id
                and r.get("offer_model", "").casefold() != model.casefold()
            })
        action = (
            f"Set model and manufacturer_model_code to {proposed} in TARGET_MASTER (keep "
            "target_id and mappings); move the former name to series/notes."
            if status == SUPPORTED_CORRECTION else ACTIONS.get(status, "None.")
        )
        if descriptors:
            flags.append("seller descriptors, not manufacturer codes: " + ", ".join(descriptors))
        if excluded_codes:
            flags.append("mapped but excluded listings state other codes (owner reviews those "
                          "mappings): " + ", ".join(excluded_codes))
        row = {
            "target_id": target_id,
            "rank": listed.get("rank", ""),
            "brand": brand,
            "original_model": model,
            "original_series": _text(target, "series"),
            "original_manufacturer_model_code": mmc,
            **{name: _text(target, name) for name in CORE_MATCH_COLUMNS if name != "model"},
            "gpu_model": _text(target, "gpu_model"),
            "identity_kind": kind,
            "status": status,
            "proposed_model": proposed,
            "proposed_series": (_text(target, "series") or model) if proposed else "",
            "proposed_manufacturer_model_code": proposed,
            "applied_in_local_snapshot": "TRUE" if proposed else "FALSE",
            "source_references": "; ".join(
                f"model_id {r.get('model_id')} (shop {r.get('shop_id')}, item {r.get('item_id')})"
                for r in own),
            "source_evidence": " ## ".join(_source_text(r) for r in own),
            "source_codes": "; ".join(codes),
            "excluded_mapped_listing_codes": "; ".join(excluded_codes),
            "retailer_corroboration": " | ".join(corroboration),
            "retailer_leads_not_evidence": " | ".join(leads),
            "conflicts": " | ".join(dict.fromkeys(conflicts)),
            "flags": " | ".join(flags),
            "action": action,
        }
        result.rows.append({column: row.get(column, "") for column in RECONCILIATION_COLUMNS})
        if proposed:
            result.corrections[target_id] = row
    # Several proposals with the same complete identity would give one configuration several
    # targets. The same code with different configurations is fine (2026-10-19, independent
    # review: grouping by code alone recommended merging 8GB and 16GB configurations).
    groups: dict[tuple[object, ...], list[str]] = {}
    for target_id, correction in result.corrections.items():
        identity = _identity(by_id[target_id], correction["proposed_model"])
        groups.setdefault(identity or (target_id,), []).append(target_id)
    rows_by_id = {row["target_id"]: row for row in result.rows}
    for ids in (ids for ids in groups.values() if len(ids) > 1):
        for target_id in ids:
            row = rows_by_id[target_id]
            others = [i for i in ids if i != target_id]
            differing = {i: _secondary_differences(by_id[target_id], by_id[i]) for i in others}
            if all(differing.values()):
                status, action = SAME_CORE_SECONDARY_CONFLICT, ACTIONS[SAME_CORE_SECONDARY_CONFLICT]
                found = [f"same configuration proposed for {i}, but {'; '.join(d)}"
                         for i, d in differing.items()]
            else:
                status, action = DUPLICATE_OF_EXISTING_TARGET, ACTIONS[DUPLICATE_OF_EXISTING_TARGET]
                found = [f"{row['proposed_model']} with the same configuration is also supported "
                         f"for {i}" for i, d in differing.items() if not d]
            row["conflicts"] = " | ".join(filter(None, (row["conflicts"], *found)))
            row.update(status=status, applied_in_local_snapshot="FALSE", proposed_model="",
                       proposed_series="", proposed_manufacturer_model_code="", action=action)
            result.corrections.pop(target_id, None)
    return result


def apply_corrections(
    targets: Sequence[dict[str, str]],
    selected: Sequence[dict[str, str]],
    corrections: Mapping[str, Mapping[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """``(catalog rows, selected-list rows)`` with the supported corrections applied; every
    other value, the target IDs and the list order are unchanged."""
    catalog = []
    for row in targets:
        row = dict(row)
        correction = corrections.get(row.get("target_id", ""))
        if correction:
            note = (f"{RECONCILED_ON}: catalog reconciliation: model '{row['model']}' -> "
                    f"'{correction['proposed_model']}' from the contributing source listings "
                    f"({correction['source_references']}); target_id and mappings unchanged.")
            row["notes"] = "\n".join(filter(None, (row.get("notes", ""), note)))
            row["series"] = row.get("series") or correction["original_model"]
            row["model"] = correction["proposed_model"]
            row["manufacturer_model_code"] = correction["proposed_model"]
        catalog.append(row)
    listed = []
    for row in selected:
        row = dict(row)
        correction = corrections.get(row["target_id"])
        if correction:
            row["model"] = correction["proposed_model"]
            row["series"] = row.get("series") or correction["original_model"]
        listed.append(row)
    return catalog, listed
