"""Catalog convention proposals for ``model`` and ``cpu_series`` (approved direction 2026-10-08).

Reads a validated TARGET_MASTER snapshot (plus, optionally, the mapping and review tables, Top 100
coverage, and saved retailer observations) and proposes, per existing ``target_id``:

- ``model``: the verified manufacturer model code, taken only from the target's own
  ``manufacturer_model_code`` when that field is exactly one code (``READY``), or from codes that
  mapped ``COMPLETE`` review rows recorded in their ``manufacturer_model_code`` field when the
  target has none (``NEEDS_CONFIRMATION``). The former model wording moves to ``series`` (when
  ``series`` is blank) and is recorded for ``notes``. Without a verified code the model name is
  kept and the issue reported.
- ``cpu_series``: the canonical form from ``conventions.py``, e.g. ``Apple M5`` for an Apple
  product recorded as ``M5``. ``cpu_model`` is never changed; disagreements with it are reported.

Codes in listing titles or variant notes are listed as unverified hints and never proposed.
Nothing here edits Sheets, changes target IDs or mappings, or treats a proposal as authoritative.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import pandas as pd

from .conventions import (
    apple_chip_form,
    cpu_series_forms,
    is_canonical_cpu_series,
    is_model_code,
    model_codes,
)
from .extraction import ExtractionStatus, extract_offer_attributes
from .normalization import CORE_MATCH_COLUMNS, core_key, core_value_key, text_key
from .offers import OfferObservation
from .supporting_tables import TableResult
from .target_master import (
    LISTING_ONLY,
    TARGET_MASTER_HEADERS,
    TargetMasterResult,
    effective_tracking_mode,
)

CONVENTION_DATE = "2026-10-08"

READY = "READY"
NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
NO_CHANGE = "NO_CHANGE"
UNRESOLVED = "UNRESOLVED"
NOT_APPLICABLE = "NOT_APPLICABLE"

PROPOSAL_COLUMNS: tuple[str, ...] = (
    "target_id",
    "status",
    "tracking_mode",
    "in_accepted_top_100",
    "brand",
    "current_model",
    "current_series",
    "current_manufacturer_model_code",
    "proposed_model",
    "proposed_series",
    "proposed_manufacturer_model_code",
    "model_action",
    "model_readiness",
    "current_cpu_series",
    "proposed_cpu_series",
    "cpu_model",
    "cpu_action",
    "cpu_readiness",
    "evidence",
    "source_url",
    "original_wording_to_preserve",
    "proposed_notes_append",
    "unresolved_issues",
    "proposed_core_key_shared_with",
    "unverified_code_hints",
    "retailer_evidence",
)

DUPLICATE_COLUMNS: tuple[str, ...] = (
    "proposed_model",
    "screen_size_in",
    "proposed_cpu_series",
    "ram_capacity_gb",
    "storage_capacity_gb",
    "target_ids",
    "readiness_involved",
)

# Diagnostic shorthand in free-text cpu_model values ("Intel i7", "i5-6200U", "R9-8945HX").
# Used only to report disagreements with cpu_series, never to propose a value.
_INTEL_SHORTHAND = re.compile(r"(?<![A-Za-z0-9])i([3579])(?![0-9])", re.I)
_RYZEN_SHORTHAND = re.compile(r"(?<![A-Za-z0-9])R([3579])-[0-9]", re.I)


class CatalogInputError(ValueError):
    """An input failed validation, so no proposal is produced from it."""


@dataclass
class ConventionReport:
    proposals: list[dict[str, str]]
    duplicates: list[dict[str, str]]
    counts: dict[str, int] = field(default_factory=dict)


def _text(row: Mapping[str, object], name: str) -> str:
    value = row.get(name, "")
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def cpu_model_families(cpu_model: str, is_apple: bool) -> set[str]:
    families = set(cpu_series_forms(cpu_model))
    families |= {f"Core i{m[1]}" for m in _INTEL_SHORTHAND.finditer(cpu_model)}
    families |= {f"Ryzen {m[1]}" for m in _RYZEN_SHORTHAND.finditer(cpu_model)}
    if is_apple and (form := apple_chip_form(cpu_model)):
        families.add(form)
    return families


def _review_codes(
    review_rows: Iterable[Mapping[str, object]],
) -> tuple[dict[str, list[str]], list[str]]:
    """Codes recorded in mapped COMPLETE review rows, with their source_model_ids; non-codes."""
    codes: dict[str, str] = {}
    sources: dict[str, list[str]] = defaultdict(list)
    other: list[str] = []
    for row in review_rows:
        value = _text(row, "manufacturer_model_code").strip()
        if not value:
            continue
        if is_model_code(value):
            code = codes.setdefault(value.casefold(), value)
            sources[code].append(_text(row, "source_model_id"))
        else:
            other.append(value)
    return {code: sorted(set(ids)) for code, ids in sources.items()}, other


def _with_sources(codes: Iterable[str], sources: Mapping[str, list[str]]) -> str:
    return ", ".join(
        f"{code} (mapped source_model_id {' '.join(sources[code])})" if code in sources else code
        for code in sorted(codes)
    )


def _propose_model(
    row: Mapping[str, object], review_rows: list[Mapping[str, object]], issues: list[str]
) -> dict[str, str]:
    model = _text(row, "model").strip()
    series = _text(row, "series").strip()
    code_field = _text(row, "manufacturer_model_code").strip()
    review_codes, review_other = _review_codes(review_rows)
    result = {
        "proposed_model": model,
        "proposed_series": series,
        "proposed_manufacturer_model_code": code_field,
        "model_action": "",
        "model_readiness": "",
        "evidence": [],
        "notes": [],
        "preserve": [],
    }
    evidence: list[str] = result["evidence"]  # type: ignore[assignment]

    def adopt(code: str, action: str, readiness: str) -> None:
        result["proposed_model"] = code
        result["model_action"] = action
        result["model_readiness"] = readiness
        if not series:
            result["proposed_series"] = model
        where = "proposed series" if not series else "notes; series already set"
        result["preserve"].append(f"model {model!r} ({where})")  # type: ignore[union-attr]
        result["notes"].append(  # type: ignore[union-attr]
            f"{CONVENTION_DATE} convention: model was {model!r}; the verified code is now the "
            f"model and the family name is in series."
        )

    model_code_tokens = model_codes(model)
    if review_other:
        issues.append("REVIEW_CODE_FIELD_NOT_A_CODE: " + "; ".join(sorted(set(review_other))))

    if code_field and is_model_code(code_field):
        evidence.append(f"TARGET_MASTER.manufacturer_model_code = {code_field!r}")
        conflicting = [c for c in review_codes if c.casefold() != code_field.casefold()]
        if review_codes and not conflicting:
            evidence.append("mapped COMPLETE review rows record the same code")
        in_model = [c for c in model_code_tokens if c.casefold() != code_field.casefold()]
        if conflicting:
            # Under the code convention these listings are different products: the owner must
            # decide whether to split the target or correct a review record. Nothing is remapped.
            issues.append(
                f"MAPPED_LISTINGS_RECORD_OTHER_CODES: target code {code_field}; "
                + _with_sources(conflicting, review_codes)
            )
        if in_model:
            issues.append(f"MODEL_CONTAINS_OTHER_CODE: {', '.join(in_model)}")
        if conflicting or in_model:
            result["model_action"] = "REVIEW_CONFLICTING_CODES"
            result["model_readiness"] = UNRESOLVED
        elif text_key(model) == text_key(code_field):
            result["model_action"] = "KEEP_VERIFIED_CODE"
            result["model_readiness"] = NO_CHANGE
        else:
            adopt(code_field, "SET_MODEL_TO_VERIFIED_CODE", READY)
        return result

    if code_field:
        issues.append(f"MANUFACTURER_MODEL_CODE_IS_NOT_A_CODE: {code_field!r}")
    if is_model_code(model):
        evidence.append(f"TARGET_MASTER.model is already one model code ({model!r})")
        result["model_action"] = "KEEP_VERIFIED_CODE"
        result["model_readiness"] = NO_CHANGE
        if not code_field:
            result["proposed_manufacturer_model_code"] = model
            issues.append("MANUFACTURER_MODEL_CODE_BLANK: may be filled from model")
        return result
    if len(review_codes) == 1 and not model_code_tokens:
        (code,) = review_codes
        evidence.append(
            f"mapped COMPLETE review rows record manufacturer_model_code {code!r} "
            f"(source_model_id {' '.join(review_codes[code])})"
        )
        adopt(code, "SET_MODEL_TO_REVIEW_RECORDED_CODE", NEEDS_CONFIRMATION)
        result["proposed_manufacturer_model_code"] = code
        issues.append("CONFIRM_REVIEW_RECORDED_CODE: TARGET_MASTER has no code of its own")
        return result
    if len(review_codes) > 1 or model_code_tokens:
        issues.append(
            "AMBIGUOUS_MODEL_CODE_EVIDENCE: "
            + _with_sources({*review_codes, *model_code_tokens}, review_codes)
        )
        result["model_action"] = "REVIEW_CONFLICTING_CODES"
        result["model_readiness"] = UNRESOLVED
        return result
    issues.append("NO_VERIFIED_MODEL_CODE: verified model name kept")
    result["model_action"] = "KEEP_MODEL_NAME"
    result["model_readiness"] = UNRESOLVED
    return result


def _propose_cpu(row: Mapping[str, object], issues: list[str]) -> dict[str, str]:
    current = _text(row, "cpu_series").strip()
    cpu_model = _text(row, "cpu_model").strip()
    is_apple = text_key(_text(row, "brand")) == "apple"
    result = {"proposed_cpu_series": current, "cpu_action": "", "cpu_readiness": "", "note": ""}
    if is_canonical_cpu_series(current):
        result |= {"cpu_action": "KEEP_SUPPORTED_FORM", "cpu_readiness": NO_CHANGE}
    elif is_apple and (form := apple_chip_form(current)):
        result |= {
            "proposed_cpu_series": form,
            "cpu_action": "ADD_APPLE_PREFIX",
            "cpu_readiness": READY,
            "note": f"{CONVENTION_DATE} convention: cpu_series was {current!r}.",
        }
    elif len(forms := cpu_series_forms(current)) == 1:
        (form,) = forms
        result |= {
            "proposed_cpu_series": form,
            "cpu_action": "SET_CANONICAL_FORM",
            "cpu_readiness": READY,
            "note": f"{CONVENTION_DATE} convention: cpu_series was {current!r}.",
        }
    else:
        issues.append(f"UNSUPPORTED_CPU_SERIES_FORM: {current!r}")
        result |= {"cpu_action": "REVIEW_UNSUPPORTED_FORM", "cpu_readiness": UNRESOLVED}

    families = cpu_model_families(cpu_model, is_apple)
    proposed = result["proposed_cpu_series"]
    if len(families) > 1:
        issues.append(f"CPU_MODEL_NAMES_SEVERAL_FAMILIES: cpu_model {cpu_model!r}")
    elif families and proposed not in families:
        issues.append(
            f"CPU_MODEL_DISAGREES_WITH_CPU_SERIES: cpu_model {cpu_model!r} suggests "
            f"{next(iter(families))!r}, cpu_series is {proposed!r}"
        )
    return result


def _retailer_evidence(
    row: Mapping[str, object],
    proposed: Mapping[str, str],
    observations: Sequence[OfferObservation],
    issues: list[str],
) -> list[str]:
    """Saved offers whose extracted model code equals the target's verified code.

    The target's code is its proposed model code or, when the model proposal is unresolved, its
    own ``manufacturer_model_code``. Core differences are reported, never resolved.
    """
    codes = {
        text_key(value)
        for value in (proposed["proposed_model"], _text(row, "manufacturer_model_code"))
        if is_model_code(value)
    }
    if not codes:
        return []
    catalog = {name: _text(row, name) for name in CORE_MATCH_COLUMNS}
    catalog["cpu_series"] = proposed["proposed_cpu_series"]
    notes = []
    for offer in observations:
        extracted = extract_offer_attributes(offer)
        if text_key(extracted.values.get("model")) not in codes:
            continue
        differing = []
        for name in CORE_MATCH_COLUMNS[1:]:
            item = next((e for e in extracted.evidence if e.field == name), None)
            target_value = catalog[name]
            if item is None or item.status is not ExtractionStatus.EXTRACTED:
                status = item.status.value if item else "MISSING"
                differing.append(f"{name} not extracted ({status})")
                continue
            try:
                same = core_value_key(name, item.value) == core_value_key(name, target_value)
            except ValueError:
                same = False
            if not same:
                differing.append(f"{name} retailer {item.value} vs catalog {target_value}")
                issues.append(
                    f"RETAILER_CORE_VALUE_DIFFERS: {name} is {item.value!r} at "
                    f"{offer.retailer_id} but {target_value!r} in the catalog; the retailer "
                    "listing alone does not show which is correct"
                )
        retrieved = offer.retrieved_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = "; ".join(differing) if differing else "all other core values agree"
        notes.append(
            f"{offer.retailer_id} {offer.offer_url} ({retrieved}): code matches; {summary}"
        )
    return notes


def build_report(
    target_master: TargetMasterResult,
    *,
    source_model_map: TableResult | None = None,
    match_review: TableResult | None = None,
    top_100_target_ids: Iterable[str] = (),
    observations: Sequence[OfferObservation] = (),
) -> ConventionReport:
    if not target_master.is_valid:
        raise CatalogInputError("TARGET_MASTER snapshot did not pass validation.")
    for name, table in (("SOURCE_MODEL_MAP", source_model_map), ("MATCH_REVIEW", match_review)):
        if table is not None and not table.is_valid:
            raise CatalogInputError(f"{name} snapshot did not pass validation.")

    review_by_target: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    hint_text_by_target: dict[str, list[str]] = defaultdict(list)
    if source_model_map is not None and match_review is not None:
        reviews = match_review.valid_rows
        complete = reviews.loc[reviews["status"].eq("COMPLETE")]
        by_source: dict[str, list[dict[str, object]]] = defaultdict(list)
        for record in complete.to_dict(orient="records"):
            by_source[str(record["source_model_id"])].append(record)
        for link in source_model_map.valid_rows.itertuples(index=False):
            rows = by_source.get(str(link.source_model_id), [])
            review_by_target[str(link.target_id)].extend(rows)
            hint_text_by_target[str(link.target_id)].extend(
                _text(row, "candidate_name") for row in rows
            )
    top_100 = set(top_100_target_ids)

    proposals: list[dict[str, str]] = []
    for row in target_master.valid_rows.to_dict(orient="records"):
        target_id = str(row["target_id"])
        mode = effective_tracking_mode(row.get("tracking_mode"))
        base = {
            "target_id": target_id,
            "status": _text(row, "status"),
            "tracking_mode": mode,
            "in_accepted_top_100": str(target_id in top_100),
            "brand": _text(row, "brand"),
            "source_url": _text(row, "source_url"),
            "current_model": _text(row, "model"),
            "current_series": _text(row, "series"),
            "current_manufacturer_model_code": _text(row, "manufacturer_model_code"),
            "current_cpu_series": _text(row, "cpu_series"),
            "cpu_model": _text(row, "cpu_model"),
        }
        if mode == LISTING_ONLY:
            proposals.append(
                dict.fromkeys(PROPOSAL_COLUMNS, "")
                | base
                | {
                    "model_action": "NOT_APPLICABLE_LISTING_ONLY",
                    "model_readiness": NOT_APPLICABLE,
                    "cpu_action": "NOT_APPLICABLE_LISTING_ONLY",
                    "cpu_readiness": NOT_APPLICABLE,
                    "proposed_model": base["current_model"],
                    "proposed_series": base["current_series"],
                    "proposed_cpu_series": base["current_cpu_series"],
                    "proposed_manufacturer_model_code": base["current_manufacturer_model_code"],
                }
            )
            continue

        issues: list[str] = []
        model = _propose_model(row, review_by_target.get(target_id, []), issues)
        cpu = _propose_cpu(row, issues)
        notes = [*model["notes"], *([cpu["note"]] if cpu["note"] else [])]  # type: ignore[list-item]
        preserve = list(model["preserve"])  # type: ignore[arg-type]
        if cpu["proposed_cpu_series"] != base["current_cpu_series"]:
            preserve.append(f"cpu_series {base['current_cpu_series']!r} (cpu_model unchanged)")
        proposed_code = model["proposed_model"]
        hints = sorted(
            {
                code
                for text in (_text(row, "variant_notes"), *hint_text_by_target.get(target_id, []))
                for code in model_codes(text)
                if code.casefold() != str(proposed_code).casefold()
            }
        )
        record = dict.fromkeys(PROPOSAL_COLUMNS, "") | base
        record |= {
            "proposed_model": str(model["proposed_model"]),
            "proposed_series": str(model["proposed_series"]),
            "proposed_manufacturer_model_code": str(model["proposed_manufacturer_model_code"]),
            "model_action": str(model["model_action"]),
            "model_readiness": str(model["model_readiness"]),
            "proposed_cpu_series": cpu["proposed_cpu_series"],
            "cpu_action": cpu["cpu_action"],
            "cpu_readiness": cpu["cpu_readiness"],
            "evidence": "; ".join(model["evidence"]),  # type: ignore[arg-type]
            "original_wording_to_preserve": "; ".join(preserve),
            "proposed_notes_append": " ".join(notes),
            "unverified_code_hints": (
                "UNVERIFIED listing/variant text, not used: " + ", ".join(hints) if hints else ""
            ),
        }
        retailer = _retailer_evidence(row, record, observations, issues)
        record["retailer_evidence"] = " | ".join(retailer)
        record["unresolved_issues"] = " | ".join(issues)
        proposals.append(record)

    duplicates = _duplicate_groups(target_master.valid_rows, proposals)
    shared: dict[str, list[str]] = defaultdict(list)
    for group in duplicates:
        ids = group["target_ids"].split(";")
        for target_id in ids:
            shared[target_id] = [other for other in ids if other != target_id]
    for record in proposals:
        record["proposed_core_key_shared_with"] = ";".join(shared.get(record["target_id"], []))

    counts: dict[str, int] = defaultdict(int)
    for record in proposals:
        counts[f"model {record['model_readiness']}"] += 1
        counts[f"cpu_series {record['cpu_readiness']}"] += 1
    counts["proposed duplicate core keys"] = len(duplicates)
    return ConventionReport(proposals, duplicates, dict(sorted(counts.items())))


def _proposed_value(record: Mapping[str, str], field: str, readiness: str) -> str:
    return record[f"proposed_{field}"] if record[readiness] in {READY, NEEDS_CONFIRMATION} else (
        record[f"current_{field}"]
    )


def _duplicate_groups(
    rows: pd.DataFrame, proposals: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Core keys shared by more than one CONFIGURATION target once proposals are applied."""
    by_id = {str(row["target_id"]): row for row in rows.to_dict(orient="records")}
    groups: dict[tuple[object, ...], list[dict[str, str]]] = defaultdict(list)
    for record in proposals:
        if record["tracking_mode"] == LISTING_ONLY:
            continue
        row = dict(by_id[record["target_id"]])
        row["model"] = _proposed_value(record, "model", "model_readiness")
        row["cpu_series"] = _proposed_value(record, "cpu_series", "cpu_readiness")
        key, _ = core_key(row)
        if key is not None:
            groups[key].append(record | {"_row": row})  # type: ignore[dict-item]
    duplicates = []
    for members in groups.values():
        if len(members) < 2:
            continue
        row = members[0]["_row"]  # type: ignore[index]
        duplicates.append(
            {
                "proposed_model": row["model"],
                "screen_size_in": str(row["screen_size_in"]),
                "proposed_cpu_series": row["cpu_series"],
                "ram_capacity_gb": str(row["ram_capacity_gb"]),
                "storage_capacity_gb": str(row["storage_capacity_gb"]),
                "target_ids": ";".join(sorted(m["target_id"] for m in members)),
                "readiness_involved": ";".join(
                    sorted({m["model_readiness"] for m in members} | {
                        m["cpu_readiness"] for m in members
                    })
                ),
            }
        )
    return sorted(duplicates, key=lambda group: group["target_ids"])


def apply_ready_proposals(
    rows: pd.DataFrame, proposals: Iterable[Mapping[str, str]]
) -> list[list[str]]:
    """A DIAGNOSTIC TARGET_MASTER copy with only READY model/series/cpu_series values applied.

    Worksheet-shaped values (header first) for validation and offline replay only. It is not an
    authoritative catalog and must never be written to the Sheet as-is.
    """
    ready = {record["target_id"]: record for record in proposals}
    values = [list(TARGET_MASTER_HEADERS)]
    for row in rows.to_dict(orient="records"):
        out = {name: _text(row, name) for name in TARGET_MASTER_HEADERS}
        record = ready.get(out["target_id"])
        if record and record["model_readiness"] == READY:
            out["model"] = record["proposed_model"]
            out["series"] = record["proposed_series"]
        if record and record["cpu_readiness"] == READY:
            out["cpu_series"] = record["proposed_cpu_series"]
        values.append([out[name] for name in TARGET_MASTER_HEADERS])
    return values


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ") or "—"


def _links(record: Mapping[str, str]) -> str:
    """The target's own source_url plus any retailer offer URL whose code matched (evidence)."""
    links = [f"catalog source: {record['source_url']}"] if record["source_url"].strip() else []
    links += [
        f"retailer: {url}" for url in re.findall(r"https?://[^\s|()]+", record["retailer_evidence"])
    ]
    return "; ".join(links)


def correction_report_markdown(report: ConventionReport, source: str) -> str:
    """A concise sheet-editing report: READY changes, items to confirm, and unresolved issues."""
    rows = report.proposals
    model_ready = [r for r in rows if r["model_readiness"] == READY]
    cpu_ready = [r for r in rows if r["cpu_readiness"] == READY]
    confirm = [
        r for r in rows if NEEDS_CONFIRMATION in {r["model_readiness"], r["cpu_readiness"]}
    ]
    issues = [r for r in rows if r["unresolved_issues"]]
    listing = [r["target_id"] for r in rows if r["tracking_mode"] == LISTING_ONLY]
    lines = [
        f"# TARGET_MASTER model / cpu_series correction report ({CONVENTION_DATE} convention)",
        "",
        f"Generated from `{source}`. Local file: it contains catalog values; keep it out of Git.",
        "These are proposals, not authoritative values. Apply only after owner approval, and "
        "follow docs/SHEET_MIGRATION_MODEL_CPU_CONVENTIONS.md.",
        "",
        "Counts: " + "; ".join(f"{key} {value}" for key, value in report.counts.items()) + ".",
        "",
        "Edit only the cells named below, keyed by `target_id`. Do not change `target_id`, "
        "`status`, `tracking_mode`, `cpu_model`, any capacity, `SOURCE_MODEL_MAP`, or "
        "`MATCH_REVIEW`. Append the notes text; do not replace existing notes.",
        "",
        "## 1. READY: model and series",
        "",
        "Evidence: a READY `model` comes from the target's own verified "
        "`manufacturer_model_code` in TARGET_MASTER (and, where stated, the mapped COMPLETE "
        "`MATCH_REVIEW` rows). A READY `cpu_series` comes from the target's own `brand` (Apple) "
        "and `cpu_series`. Evidence links are listed only where a URL exists: the target's "
        "`source_url`, or a retailer page whose model label contains the same code. A dash "
        "means no URL is recorded.",
        "",
        "| target_id | Top 100 | set `model` to | set `series` to | current `model` | evidence "
        "| evidence links | append to `notes` |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['target_id']} | {r['in_accepted_top_100']} | {_cell(r['proposed_model'])} | "
        f"{_cell(r['proposed_series'])} | {_cell(r['current_model'])} | {_cell(r['evidence'])} | "
        f"{_cell(_links(r))} | {_cell(r['proposed_notes_append'])} |"
        for r in model_ready
    ]
    lines += [
        "",
        "## 2. READY: cpu_series",
        "",
        "| target_id | brand | current `cpu_series` | set `cpu_series` to "
        "| `cpu_model` (unchanged) | evidence links |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['target_id']} | {_cell(r['brand'])} | {_cell(r['current_cpu_series'])} | "
        f"{_cell(r['proposed_cpu_series'])} | {_cell(r['cpu_model'])} | {_cell(_links(r))} |"
        for r in cpu_ready
    ]
    lines += ["", "## 3. NEEDS_CONFIRMATION (do not apply without owner confirmation)", ""]
    lines += [
        f"- {r['target_id']}: model {_cell(r['current_model'])} → {_cell(r['proposed_model'])}; "
        f"{_cell(r['evidence'])}"
        for r in confirm
    ] or ["None."]
    lines += ["", "## 4. Duplicate core keys the proposals would create", ""]
    lines += [
        f"- {d['target_ids']}: {d['proposed_model']} / {d['screen_size_in']} / "
        f"{d['proposed_cpu_series']} / {d['ram_capacity_gb']} / {d['storage_capacity_gb']} "
        f"({d['readiness_involved']}). Resolve before applying."
        for d in report.duplicates
    ] or ["None."]
    name_only = [
        r
        for r in issues
        if all(i.startswith("NO_VERIFIED_MODEL_CODE") for i in r["unresolved_issues"].split(" | "))
    ]
    decisions = [r for r in issues if r not in name_only]
    lines += ["", "## 5. Unresolved issues needing a decision (no value proposed)", ""]
    lines += [
        f"- {r['target_id']} (Top 100: {r['in_accepted_top_100']}; model "
        f"{_cell(r['current_model'])}): {_cell(r['unresolved_issues'])}"
        + (f" Evidence links: {_links(r)}" if _links(r) else "")
        for r in decisions
    ] or ["None."]
    top = sum(r["in_accepted_top_100"] == "True" for r in name_only)
    lines += [
        "",
        "## 6. No verified model code: model name kept",
        "",
        f"{len(name_only)} targets ({top} in the accepted Top 100) have no verified code, so "
        "their verified model name stays as it is. Unverified code hints from listing text, "
        "where any exist, are in `catalog_convention_proposals.csv` and must be checked against "
        "authoritative evidence before use: "
        + ", ".join(r["target_id"] for r in name_only)
        + ".",
    ]
    lines += [
        "",
        "## 7. Not touched",
        "",
        f"- {len(listing)} `LISTING_ONLY` targets stay as they are and remain excluded from "
        f"retailer matching: {', '.join(listing) or 'none'}.",
        "- Unmapped Top 100 candidates have no target, so nothing here applies to them.",
        "",
    ]
    return "\n".join(lines)
