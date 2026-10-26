"""Evidence-backed catalog correction proposals (2026-10-22), separate from code fixes.

Some blank retailer cells are not scraper misses but catalog problems: a family target next to a
more specific target, a screen recorded as the marketed class, GPU memory recorded as system RAM,
a broad display name next to a recorded manufacturer code, or a GPU brand copied from the CPU.
Matching must not paper over these (no global ``15 = 15.6``, no code guessed from a name), and
code must not silently rewrite the catalog. This module reads a validated ``TARGET_MASTER``
snapshot, the saved collection evidence and the report tables of one run, and writes **proposals
only**: each names the affected targets and source mappings, the old and the proposed value, the
quoted source evidence with its scope, the uncertainty, what a reviewer must check and the
consequences for mappings and history.

Five proposal types, one detector each:

- ``FAMILY_TARGET_VS_SPECIFIC_TARGET``: a family-name target (``HP Laptop 14``) and a target for
  one code (``14-ep0445TU``) with the same other four core values, linked by the family target's own
  source text and, where present, by one offer that prices both.
- ``SCREEN_CLASS_VS_DIAGONAL``: a target recording the marketed class (``15``) whose identity-
  established product pages state the measured diagonal (``15.6``). Only a diagonal of N.6 over a
  recorded N is proposed (the one relationship the HP/MSI pages state themselves); any other
  difference (``14`` vs ``14.5``) is listed for review with no proposed value.
- ``MSI_FIELD_CONFLATION``: system RAM, GPU memory and screen fields that disagree with the
  retailer pages (RAM type recorded as ``GDDR6``, VRAM equal to the system RAM).
- ``BROAD_NAME_WITH_RECORDED_CODE``: a non-code ``model`` with a recorded manufacturer code,
  reusing ``catalog_reconciliation.reconcile`` for the source listings' own evidence.
- ``GPU_FIELD_CONFLICT``: an integrated/discrete or vendor contradiction inside the GPU fields or
  against identity-established pages.

Nothing is applied: ``applies_automatically`` is always ``FALSE``; no Sheet, target ID, mapping,
approval, history store or catalog snapshot is written, and no retailer is contacted.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .catalog_reconciliation import SUPPORTED_CORRECTION, reconcile
from .conventions import canonical_gpu_models, is_model_code, model_codes, product_name_tokens
from .normalization import CORE_MATCH_COLUMNS, core_value_key
from .product_names import default_registry

PROPOSED_ON = "2026-10-22"

TYPE_FAMILY_VS_SPECIFIC = "FAMILY_TARGET_VS_SPECIFIC_TARGET"
TYPE_SCREEN = "SCREEN_CLASS_VS_DIAGONAL"
TYPE_MSI = "MSI_FIELD_CONFLATION"
TYPE_BROAD_NAME = "BROAD_NAME_WITH_RECORDED_CODE"
TYPE_GPU = "GPU_FIELD_CONFLICT"
PROPOSAL_TYPES = (TYPE_FAMILY_VS_SPECIFIC, TYPE_SCREEN, TYPE_MSI, TYPE_BROAD_NAME, TYPE_GPU)

HIGH, MEDIUM, LOW, REVIEW_ONLY = "HIGH", "MEDIUM", "LOW", "REVIEW_ONLY"

PROPOSAL_COLUMNS: tuple[str, ...] = (
    "proposal_id", "proposal_type", "affected_target_ids", "affected_source_model_ids",
    "in_top_100", "field", "old_value", "proposed_value", "companion_changes",
    "evidence_summary", "evidence_scope", "confidence", "uncertainty", "reviewer_checks",
    "identity_key_impact", "mapping_history_impact", "applies_automatically",
)
# The quoted-evidence list is kept structured in the JSON file only.
SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "target_id", "status", "brand", "model", "series", "screen_size_in", "cpu_brand",
    "cpu_series", "cpu_model", "ram_capacity_gb", "ram_type", "storage_capacity_gb", "gpu_type",
    "gpu_brand", "gpu_model", "vram", "manufacturer_model_code", "manufacturer_part_number",
    "tracking_mode",
)
MATCHED_STATUSES = frozenset({"EXACT", "LISTED_SPEC"})
_NAN = {"nan", "none"}
_HEADING = re.compile(
    r"\s*(?:operating systems?|os|processor|cpu|memory|ram|storage|display|graphics|security|"
    r"webcam|camera|keyboard|communication|audio|battery|ports?|i/o|dimensions?|weight|colou?r|"
    r"chipset|wlan|power|ac adapter|warranty|inclusions?|specifications?|technical specifications"
    r")", re.I)


def _t(row: Mapping[str, object], name: str) -> str:
    value = row.get(name, "")
    text = "" if value is None else str(value).strip()
    return "" if text.casefold() in _NAN else text


def _same(name: str, left: str, right: str) -> bool:
    try:
        first, second = core_value_key(name, left), core_value_key(name, right)
    except ValueError:
        return False
    return first is not None and first == second


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.strip())
    except (InvalidOperation, ValueError):
        return None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(10**9)
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


# Offer evidence ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Statement:
    """One extracted statement with its source label, quoted text and evidence scope."""

    field: str
    value: str
    label: str
    text: str
    scope: str


@dataclass
class OfferEvidence:
    """One collected product page: its identity, extracted statements and raw specification rows."""

    retailer_id: str
    offer_url: str
    retrieved_at: str
    title: str
    observation_id: str = ""
    source: str = ""
    availability: str = ""
    price: str = ""
    match_status: str = ""
    target_id: str = ""
    candidate_target_ids: tuple[str, ...] = ()
    expected_target_id: str = ""
    offer_model: str = ""
    offer_values: dict[str, str] = field(default_factory=dict)
    statements: dict[str, list[Statement]] = field(default_factory=dict)
    specs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def codes(self) -> set[str]:
        found = {code.casefold() for code in model_codes(self.title)}
        if self.offer_model and is_model_code(self.offer_model):
            found.add(self.offer_model.casefold())
        return found

    def spec_values(self, label_pattern: str) -> list[tuple[str, str]]:
        """Specification rows whose label matches. A flattened ``label, ''`` row takes the rows
        after it as its value, up to the next heading or four rows (PC Central writes a label and
        its value lines as separate rows)."""
        pattern = re.compile(label_pattern, re.I)
        found = []
        for index, (label, value) in enumerate(self.specs):
            if not pattern.search(label):
                continue
            text = value
            if not text.strip():
                lines = []
                for next_label, next_value in self.specs[index + 1:index + 5]:
                    if next_value.strip() or _HEADING.match(next_label):
                        break
                    lines.append(next_label)
                text = " ; ".join(lines)
            found.append((label, text))
        return found

    def statement_list(self, name: str) -> list[Statement]:
        return self.statements.get(name, [])

    def quote(self, name: str) -> Statement | None:
        found = self.statement_list(name)
        return found[0] if found else None


def _specs(raw: str) -> list[tuple[str, str]]:
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    rows = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, list) and item:
            rows.append((str(item[0]), str(item[1]) if len(item) > 1 else ""))
    return rows


def load_offer_evidence(collection_dirs: Sequence[Path]) -> list[OfferEvidence]:
    """Collected pages from saved collection folders (observations, match results, evidence).

    Reads only. An offer URL seen in several folders keeps its latest retrieval.
    """
    latest: dict[str, OfferEvidence] = {}
    found_any = False
    for folder in collection_dirs:
        folder = Path(folder)
        observations_path = folder / "observations.csv"
        if not observations_path.is_file():
            continue
        found_any = True
        results = {
            (r["offer_url"], r["retrieved_at"], r["offer_index"]): r
            for r in read_csv_rows(folder / "match_results.csv")
        } if (folder / "match_results.csv").is_file() else {}
        evidence: dict[str, list[dict[str, str]]] = defaultdict(list)
        if (folder / "match_evidence.csv").is_file():
            for row in read_csv_rows(folder / "match_evidence.csv"):
                if row.get("stage") == "EXTRACTION":
                    evidence[row["observation_id"]].append(row)
        for row in read_csv_rows(observations_path):
            result = results.get((row["offer_url"], row["retrieved_at"], row["offer_index"]), {})
            observation_id = result.get("observation_id", "")
            statements: dict[str, list[Statement]] = defaultdict(list)
            for item in evidence.get(observation_id, []):
                statements[item["field"]].append(Statement(
                    item["field"], item["offer_value"], item["source_label"], item["source_text"],
                    item["evidence_scope"]))
            offer = OfferEvidence(
                retailer_id=row["retailer_id"], offer_url=row["offer_url"],
                retrieved_at=row["retrieved_at"], title=row["raw_title"],
                observation_id=observation_id, source=str(folder),
                availability=row.get("availability", ""), price=row.get("price", ""),
                match_status=result.get("match_status", ""), target_id=result.get("target_id", ""),
                candidate_target_ids=tuple(x for x in result.get(
                    "candidate_target_ids", "").split(";") if x),
                expected_target_id=result.get("expected_target_id", ""),
                offer_model=result.get("offer_model", ""),
                offer_values={name: result.get(f"offer_{name}", "") for name in (
                    "screen_size_in", "cpu_series", "ram_capacity_gb", "storage_capacity_gb")},
                statements=dict(statements), specs=_specs(row.get("raw_specifications", "")),
            )
            previous = latest.get(offer.offer_url)
            if previous is None or offer.retrieved_at > previous.retrieved_at:
                latest[offer.offer_url] = offer
    if not found_any:
        raise FileNotFoundError("No collection folder with observations.csv was found.")
    return list(latest.values())


# Catalog context --------------------------------------------------------------------------


@dataclass
class CatalogContext:
    """The validated catalog snapshot and the report tables of one run (all plain rows)."""

    targets: list[dict[str, str]]
    selected: list[dict[str, str]] = field(default_factory=list)
    contributions: list[dict[str, str]] = field(default_factory=list)
    source_model_map: list[dict[str, str]] = field(default_factory=list)
    ledger: list[dict[str, str]] = field(default_factory=list)
    selections: list[dict[str, str]] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)
    match_results: list[dict[str, str]] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_id = {_t(row, "target_id"): row for row in self.targets}
        self.ranks = {_t(r, "target_id"): _t(r, "rank") for r in self.selected}

    def configuration_targets(self) -> list[dict[str, str]]:
        return [row for row in self.targets
                if _t(row, "status") in {"", "ACTIVE"}
                and (_t(row, "tracking_mode") or "CONFIGURATION") == "CONFIGURATION"]

    def in_top_100(self, target_id: str) -> bool:
        return target_id in self.ranks

    def source_ids(self, target_id: str) -> list[str]:
        return sorted({_t(r, "source_model_id") for r in self.source_model_map
                       if _t(r, "target_id") == target_id and _t(r, "source_model_id")})

    def ledger_rows(self, target_id: str) -> list[dict[str, str]]:
        return [r for r in self.ledger if _t(r, "target_id") == target_id]

    def history_count(self, target_id: str) -> int:
        return sum(1 for r in self.history if _t(r, "target_id") == target_id)

    def priced_targets_by_offer(self) -> dict[str, set[str]]:
        """Offer URL -> targets whose selected comparison price it is (the report's own rows)."""
        found: dict[str, set[str]] = defaultdict(set)
        for row in self.selections:
            if _t(row, "offer_url") and _t(row, "price"):
                found[_t(row, "offer_url")].add(_t(row, "target_id"))
        return found

    def snapshot(self, target_id: str) -> dict[str, str]:
        row = self.by_id.get(target_id, {})
        return {name: _t(row, name) for name in SNAPSHOT_COLUMNS}

    def impact(self, target_ids: Iterable[str], *, action: str) -> str:
        parts = []
        for target_id in target_ids:
            sources = self.source_ids(target_id)
            parts.append(
                f"{target_id}: {len(sources)} SOURCE_MODEL_MAP row(s)"
                + (f" ({', '.join(sources[:6])}{'...' if len(sources) > 6 else ''})"
                   if sources else "")
                + f", {self.history_count(target_id)} stored price observation(s) in the run's "
                  "history report")
        return f"{action} " + "; ".join(parts) + "."

    def core_key_collisions(self, target_id: str, changes: Mapping[str, str]) -> list[str]:
        """Other targets that would share the five-field key after ``changes`` (information)."""
        row = {**self.by_id.get(target_id, {}), **changes}
        try:
            key = tuple(core_value_key(name, _t(row, name)) for name in CORE_MATCH_COLUMNS)
        except ValueError:
            return []
        collisions = []
        for other in self.configuration_targets():
            if _t(other, "target_id") == target_id:
                continue
            try:
                other_key = tuple(core_value_key(name, _t(other, name))
                                  for name in CORE_MATCH_COLUMNS)
            except ValueError:
                continue
            if other_key == key and None not in key:
                collisions.append(_t(other, "target_id"))
        return collisions


@dataclass
class Proposal:
    proposal_type: str
    target_ids: tuple[str, ...]
    field: str
    old_value: str
    proposed_value: str
    confidence: str
    evidence: list[dict[str, str]] = field(default_factory=list)
    evidence_scope: str = ""
    uncertainty: str = ""
    reviewer_checks: str = ""
    identity_key_impact: str = ""
    mapping_history_impact: str = ""
    companion_changes: str = ""
    source_model_ids: tuple[str, ...] = ()
    in_top_100: bool = False
    snapshots: dict[str, dict[str, str]] = field(default_factory=dict)
    suffix: str = ""  # Set by ``build_proposals`` when two proposals share a target and field.

    @property
    def proposal_id(self) -> str:
        tag = {TYPE_FAMILY_VS_SPECIFIC: "FAM", TYPE_SCREEN: "SCR", TYPE_MSI: "MSI",
               TYPE_BROAD_NAME: "NAME", TYPE_GPU: "GPU"}[self.proposal_type]
        name = re.sub(r"[^A-Za-z0-9_]+", "_", self.field).strip("_")
        return f"CP-{tag}-{'+'.join(self.target_ids)}-{name}{self.suffix}"

    def evidence_summary(self) -> str:
        return " | ".join(
            f"{e.get('retailer', '')} {e.get('model_code', '')}: {e.get('label', '')}="
            f"'{e.get('quoted_text', '')}' [{e.get('scope', '')}]"[:300]
            for e in self.evidence[:6]
        ) + (f" | (+{len(self.evidence) - 6} more in JSON)" if len(self.evidence) > 6 else "")

    def row(self) -> dict[str, str]:
        return {
            "proposal_id": self.proposal_id, "proposal_type": self.proposal_type,
            "affected_target_ids": ";".join(self.target_ids),
            "affected_source_model_ids": ";".join(self.source_model_ids),
            "in_top_100": "TRUE" if self.in_top_100 else "FALSE", "field": self.field,
            "old_value": self.old_value, "proposed_value": self.proposed_value,
            "companion_changes": self.companion_changes,
            "evidence_summary": self.evidence_summary(), "evidence_scope": self.evidence_scope,
            "confidence": self.confidence, "uncertainty": self.uncertainty,
            "reviewer_checks": self.reviewer_checks,
            "identity_key_impact": self.identity_key_impact,
            "mapping_history_impact": self.mapping_history_impact,
            "applies_automatically": "FALSE",
        }

    def as_json(self) -> dict[str, object]:
        return {**self.row(), "evidence": self.evidence, "target_rows": self.snapshots,
                "proposed_on": PROPOSED_ON}


def _evidence(offer: OfferEvidence, name: str, statement: Statement | None = None, *,
              label: str = "", quoted: str = "", scope: str = "", note: str = "") -> dict[str, str]:
    code = offer.offer_model or next(iter(sorted(model_codes(offer.title))), "")
    return {
        "retailer": offer.retailer_id, "offer_url": offer.offer_url,
        "observation_id": offer.observation_id, "retrieved_at": offer.retrieved_at,
        "model_code": code, "field": name,
        "label": label or (statement.label if statement else ""),
        "quoted_text": quoted or (statement.text if statement else ""),
        "scope": scope or (statement.scope if statement else ""),
        "page_title": offer.title, "match_status": offer.match_status,
        "matched_target_id": offer.target_id, "source_folder": offer.source, "note": note,
    }


def _is_code_target(row: Mapping[str, object]) -> bool:
    return is_model_code(_t(row, "model"))


def _recorded_code(row: Mapping[str, object]) -> str:
    model = _t(row, "model")
    if is_model_code(model):
        return model
    code = _t(row, "manufacturer_model_code")
    return code if is_model_code(code) else ""


def identity_offers(target: Mapping[str, object], offers: Iterable[OfferEvidence]
                    ) -> list[OfferEvidence]:
    """Pages whose own identity is established for ``target``: matched to it, or showing its
    recorded complete code. A search hit, a family look-alike or a shared description is not."""
    target_id, code = _t(target, "target_id"), _recorded_code(target).casefold()
    return [o for o in offers
            if (o.target_id == target_id and o.match_status in MATCHED_STATUSES)
            or (code and code in o.codes)]


def _brand_in_title(brand: str, offer: OfferEvidence) -> bool:
    words = product_name_tokens(offer.title)
    return bool(brand) and all(token in words for token in product_name_tokens(brand))


def _title_names_model(target: Mapping[str, object], offer: OfferEvidence) -> bool:
    """The page title states the target's recorded code, or its family name as whole words."""
    code = _recorded_code(target)
    if code and code.casefold() in offer.codes:
        return True
    # "HP Laptop 15" is sold as "HP 15-fd1541TU": the generic noun is not part of the name.
    wanted = [w for w in product_name_tokens(_t(target, "model")) if w not in _GENERIC_WORDS]
    words = [w for w in product_name_tokens(offer.title) if w not in _GENERIC_WORDS]
    return bool(wanted) and any(words[i:i + len(wanted)] == wanted
                                for i in range(len(words) - len(wanted) + 1))


_GENERIC_WORDS = frozenset({"laptop", "notebook"})


# Type 2 and the screen part of type 3 -------------------------------------------------------

_STANDARD_DIAGONAL = Decimal("0.6")


def _screen_findings(target: Mapping[str, object], offers: Iterable[OfferEvidence]
                     ) -> tuple[list[OfferEvidence], list[OfferEvidence]]:
    """(N.6 diagonal pages, other fractional-difference pages) over the recorded class ``N``.

    A page counts when the brand and the target's name or code are stated, and the page's
    processor, RAM and storage equal the target's. The other core field is the screen.
    """
    screen = _decimal(_t(target, "screen_size_in"))
    if screen is None or screen != screen.to_integral_value():
        return [], []
    standard: list[OfferEvidence] = []
    other: list[OfferEvidence] = []
    for offer in offers:
        shown = _decimal(offer.offer_values.get("screen_size_in", ""))
        if shown is None or not Decimal(0) < shown - screen < Decimal(1):
            continue
        if not _brand_in_title(_t(target, "brand"), offer) or not _title_names_model(
                target, offer):
            continue
        if not all(_same(name, offer.offer_values.get(name, ""), _t(target, name))
                   for name in ("cpu_series", "ram_capacity_gb", "storage_capacity_gb")):
            continue
        (standard if shown - screen == _STANDARD_DIAGONAL else other).append(offer)
    return standard, other


def _screen_proposals(ctx: CatalogContext, offers: Sequence[OfferEvidence],
                      brands: frozenset[str], claimed: set[tuple[str, str]]) -> list[Proposal]:
    proposals = []
    for target in ctx.configuration_targets():
        brand = _t(target, "brand")
        target_id = _t(target, "target_id")
        is_msi = brand.casefold() == "msi"
        if not is_msi and not ({"*", brand.casefold()} & brands):
            continue
        standard, other = _screen_findings(target, offers)
        if not standard and not other:
            continue
        old = _t(target, "screen_size_in")
        ptype = TYPE_MSI if is_msi else TYPE_SCREEN
        sources = ctx.ledger_rows(target_id)
        listing_text = " ".join(_t(r, "item_name") + " " + _t(r, "shopee_model_name")
                                for r in sources)
        source_screens = sorted(set(re.findall(r"(?<![\d.])(1[0-9](?:\.[0-9])?)\s*(?:\"|”|inch|in\b)",
                                               listing_text, re.I)))
        codes = sorted({o.offer_model or next(iter(model_codes(o.title)), "") for o in standard})
        if standard:
            diagonals = sorted({o.offer_values["screen_size_in"] for o in standard})
            new = diagonals[0] if len(diagonals) == 1 else ""
            evidence = [_evidence(o, "screen_size_in", o.quote("screen_size_in")) for o in standard]
            claimed.add((target_id, "screen_size_in"))
            changes = {"screen_size_in": new} if new else {}
            collisions = ctx.core_key_collisions(target_id, changes) if new else []
            proposals.append(Proposal(
                ptype, (target_id,), "screen_size_in", old, new,
                MEDIUM if new and len(codes) >= 1 else REVIEW_ONLY, evidence,
                "PRODUCT_SPECIFICATION (retailer page display row); page identity by title code/"
                "family and the other three core values",
                uncertainty=(
                    f"The catalog records the marketed class {old}; {len(standard)} identity-"
                    f"established page(s) for {len(codes)} model code(s) ({', '.join(codes)}) state "
                    f"the {new or '/'.join(diagonals)} diagonal. "
                    + ("Shopee source listings state: "
                       + (", ".join(source_screens) or "no screen size") + ". "
                       if sources else "")
                    + ("A family target can cover several codes; only the listed codes were "
                       "observed. " if not _recorded_code(target) else "")
                    + (f"The new key would also be held by {', '.join(collisions)}. "
                       if collisions else "")
                    + "Conflicting diagonals across pages leave the value blank."),
                reviewer_checks=(
                    f"Confirm the manufacturer states {new or 'the diagonal'} for every code "
                    "this target covers, and that the Shopee listings are the same size class. "
                    "Do not apply a global 15 -> 15.6 rule."),
                identity_key_impact=(
                    f"screen_size_in is a core identity field: {old} -> {new or '(unresolved)'} "
                    "changes the five-field key; offers showing "
                    f"{new or 'the diagonal'} would stop conflicting on screen. "
                    + (f"Shared key with {', '.join(collisions)}." if collisions else
                       "No other target shares the new key.")),
                mapping_history_impact=ctx.impact((target_id,), action=(
                    "Editing the screen value keeps the target ID, mappings and history:")),
                source_model_ids=tuple(ctx.source_ids(target_id)),
                in_top_100=ctx.in_top_100(target_id), snapshots={target_id: ctx.snapshot(target_id)},
            ))
        if other:
            shown = sorted({o.offer_values["screen_size_in"] for o in other})
            proposals.append(Proposal(
                ptype, (target_id,), "screen_size_in (unexplained difference)", old, "",
                REVIEW_ONLY,
                [_evidence(o, "screen_size_in", o.quote("screen_size_in")) for o in other],
                "PRODUCT_SPECIFICATION",
                uncertainty=(f"Pages state {', '.join(shown)} against the recorded {old}; no "
                             "evidence explains the difference, so no value is proposed."),
                reviewer_checks="Establish from the manufacturer whether this is the same "
                                "product; keep the offers in review until then.",
                identity_key_impact="None proposed.",
                mapping_history_impact=ctx.impact((target_id,), action="No change proposed:"),
                source_model_ids=tuple(ctx.source_ids(target_id)),
                in_top_100=ctx.in_top_100(target_id), snapshots={target_id: ctx.snapshot(target_id)},
            ))
    return proposals


# Type 3: MSI memory fields ------------------------------------------------------------------

_GPU_MEMORY = re.compile(
    r"(?<![\d.])([0-9]{1,2})\s*GB\s*,?\s*(?:GDDR[0-9]X?)|GDDR[0-9]X?\s*[,(]?\s*([0-9]{1,2})\s*GB",
    re.I)
_SYSTEM_MEMORY = re.compile(r"\b((?:LP)?DDR[0-9]X?)\b", re.I)
_GPU_MEMORY_TYPE = re.compile(r"^\s*GDDR[0-9]", re.I)


def gpu_memory_statements(offer: OfferEvidence) -> list[tuple[str, str, str]]:
    """(label, quoted text, GB) for dedicated GPU memory the page states on its graphics row."""
    found = []
    for label, text in offer.spec_values(r"graphics|gpu|video"):
        match = _GPU_MEMORY.search(text)
        if match:
            found.append((label, text, match[1] or match[2]))
    return found


def system_memory_type(offer: OfferEvidence) -> list[tuple[str, str, str]]:
    found = []
    for label, text in offer.spec_values(r"^(?:ram|memory|system memory)\b"):
        match = _SYSTEM_MEMORY.search(text)
        if match:
            found.append((label, text, match[1].upper()))
    return found


def _msi_proposals(ctx: CatalogContext, offers: Sequence[OfferEvidence]) -> list[Proposal]:
    proposals = []
    for target in ctx.configuration_targets():
        if _t(target, "brand").casefold() != "msi":
            continue
        target_id = _t(target, "target_id")
        pages = identity_offers(target, offers)
        ram, ram_type, vram = (_t(target, "ram_capacity_gb"), _t(target, "ram_type"),
                               _t(target, "vram"))
        base = {"source_model_ids": tuple(ctx.source_ids(target_id)),
                "in_top_100": ctx.in_top_100(target_id), "snapshots": {
                    target_id: ctx.snapshot(target_id)}}
        impact = ctx.impact((target_id,), action=(
            "Editing a secondary specification keeps the target ID, mappings and history:"))
        if _GPU_MEMORY_TYPE.match(ram_type):
            types = [(o, s) for o in pages for s in system_memory_type(o)]
            values = sorted({s[2] for _, s in types})
            new = values[0] if len(values) == 1 else ""
            proposals.append(Proposal(
                TYPE_MSI, (target_id,), "ram_type", ram_type, new,
                MEDIUM if new else REVIEW_ONLY,
                [_evidence(o, "ram_type", label=s[0], quoted=s[1], scope="PRODUCT_SPECIFICATION")
                 for o, s in types],
                "PRODUCT_SPECIFICATION (system RAM row of identity-established pages)",
                uncertainty=(
                    f"{ram_type} is a graphics memory type; no system RAM is GDDR. "
                    + (f"{len(types)} page(s) state {new} for the system memory. " if new else
                       "No page states one system memory type, so the field is proposed blank. ")
                    + "ram_type is a secondary field (a warning, not an identity key)."),
                reviewer_checks="Confirm the system memory type on the manufacturer page for "
                                "this exact model code.",
                identity_key_impact="None: ram_type is not a core identity field.",
                mapping_history_impact=impact,
                companion_changes=(f"vram: {vram or '(blank)'} (see its own proposal)"
                                   if vram else ""), **base))
        graphics = [(o, s) for o in pages for s in gpu_memory_statements(o)]
        if vram and ram and _same("ram_capacity_gb", vram.upper().replace("GB", ""), ram):
            values = sorted({f"{s[2]}GB" for _, s in graphics})
            new = values[0] if len(values) == 1 else ""
            if new and new != vram or not graphics:
                proposals.append(Proposal(
                    TYPE_MSI, (target_id,), "vram", vram, new, MEDIUM if new else REVIEW_ONLY,
                    [_evidence(o, "vram", label=s[0], quoted=s[1], scope="PRODUCT_SPECIFICATION")
                     for o, s in graphics],
                    "PRODUCT_SPECIFICATION (graphics row of identity-established pages)",
                    uncertainty=(
                        f"vram {vram} equals the system RAM ({ram} GB), which suggests the RAM "
                        "amount was entered as GPU memory. "
                        + (f"{len(graphics)} page(s) state {new} dedicated GPU memory; system "
                           "RAM is not changed." if new else
                           "No page states dedicated GPU memory; the field is proposed blank.")),
                    reviewer_checks="Confirm the GPU memory for this exact model code; keep "
                                    "ram_capacity_gb (a core identity field) unchanged unless "
                                    "the pages also contradict it.",
                    identity_key_impact="None: vram is not a core identity field "
                                        "(ram_capacity_gb is untouched).",
                    mapping_history_impact=impact, **base))
    return proposals


# Type 4: broad names with recorded codes -----------------------------------------------------

def note_alternatives(row: Mapping[str, object], field_name: str) -> list[str]:
    """Values the catalog's consolidation note says other merged targets recorded for a field
    (``SECONDARY_SPEC_CONFLICT <field>: [{value, targetIds}]``), as ``value (TGT-...)``."""
    pattern = re.compile(rf"SECONDARY_SPEC_CONFLICT {re.escape(field_name)}: (\[.*?\])\.")
    found = []
    for blob in pattern.findall(_t(row, "notes")):
        try:
            for item in json.loads(blob):
                found.append(f"{item.get('value')} ({', '.join(item.get('targetIds', []))})")
        except ValueError:
            continue
    return found


def _broad_name_proposals(ctx: CatalogContext, offers: Sequence[OfferEvidence]
                          ) -> list[Proposal]:
    registry = default_registry()
    candidates = [
        row for row in ctx.configuration_targets()
        if _t(row, "model") and not _is_code_target(row) and _t(row, "manufacturer_model_code")
        and registry.kind(_t(row, "brand"), _t(row, "model")) != "PRODUCT_NAME"
        and _t(row, "manufacturer_model_code").casefold() != _t(row, "model").casefold()]
    selected = ctx.selected or [{"target_id": _t(r, "target_id"), "rank": ""} for r in candidates]
    result = reconcile(ctx.targets, [r for r in selected if r["target_id"] in {
        _t(c, "target_id") for c in candidates}], ctx.contributions, registry, ctx.match_results)
    reconciled = {row["target_id"]: row for row in result.rows}
    proposals = []
    for target in candidates:
        target_id = _t(target, "target_id")
        code, name = _t(target, "manufacturer_model_code"), _t(target, "model")
        row = reconciled.get(target_id, {})
        if not is_model_code(code):
            # The recorded "code" is a model name (ThinkPad X390), not a manufacturer code; the
            # reconciliation shows whether the source listings agree on one product.
            proposals.append(Proposal(
                TYPE_BROAD_NAME, (target_id,), "model", name, "", REVIEW_ONLY, [],
                "TARGET_MASTER manufacturer_model_code; source listing titles",
                uncertainty=(
                    f"manufacturer_model_code records '{code}', a model name rather than a "
                    f"manufacturer code. Reconciliation status {row.get('status', 'NOT_RECONCILED')}"
                    + (f" ({row['flags']})" if row.get("flags") else "")
                    + (f"; sources state different codes: {row['conflicts']}"
                       if row.get("conflicts") else "") + ". No code is guessed from a name."),
                reviewer_checks="Establish the exact manufacturer model for each mapped listing; "
                                "split targets whose listings mix products.",
                identity_key_impact="None proposed.",
                mapping_history_impact=ctx.impact((target_id,), action="No change proposed:"),
                source_model_ids=tuple(ctx.source_ids(target_id)),
                in_top_100=ctx.in_top_100(target_id), snapshots={target_id: ctx.snapshot(target_id)},
            ))
            continue
        has_sources = any(_t(r, "target_id") == target_id for r in ctx.contributions)
        series = _t(target, "series")
        changes = {"model": code}
        collisions = ctx.core_key_collisions(target_id, changes)
        pages = [o for o in offers if code.casefold() in o.codes]
        alternatives = note_alternatives(target, "manufacturer_model_code")
        excluded = row.get("excluded_mapped_listing_codes", "")
        status = row.get("status", "NOT_RECONCILED")
        confidence = (MEDIUM if status == SUPPORTED_CORRECTION and has_sources else LOW)
        evidence = [_evidence(o, "model", label="Model/title code", quoted=o.title,
                              scope="TITLE") for o in pages]
        uncertainty = [f"Reconciliation status {status}"
                       + (f" ({row['flags']})" if row.get("flags") else "") + "."]
        if not pages:
            uncertainty.append(f"No collected retailer page for {code} is in the supplied "
                               "collections; the code rests on the catalog and the source "
                               "listings only.")
        if not has_sources:
            uncertainty.append("No contributing Shopee listings were supplied for this target, "
                               "so the code rests on its own recorded field only.")
        if row.get("source_evidence"):
            uncertainty.append("Source listings: " + row["source_evidence"][:240] + ".")
        if excluded or alternatives:
            uncertainty.append(
                "The target was consolidated from listings that record other codes "
                f"({'; '.join(filter(None, [excluded, *alternatives]))}): those listings would "
                "no longer describe this target's identity.")
        if row.get("conflicts"):
            uncertainty.append("Conflicts: " + row["conflicts"] + ".")
        proposals.append(Proposal(
            TYPE_BROAD_NAME, (target_id,), "model", name, code, confidence, evidence,
            "TARGET_MASTER manufacturer_model_code" + ("; source listing titles" if has_sources
                                                       else ""),
            uncertainty=" ".join(uncertainty),
            reviewer_checks=(
                f"Confirm every mapped Shopee listing is {code}; split or remap listings that "
                f"state other codes{' (' + excluded + ')' if excluded else ''} before changing "
                "the identity."),
            identity_key_impact=(
                f"The five-field key changes from model '{name}' to '{code}' (same screen, CPU "
                "series, RAM, storage). Offers for the code become exact-code matches instead of "
                "listed-spec matches; offers for other coded products of the family stop being "
                "family candidates. "
                + (f"The new key is already held by {', '.join(collisions)}." if collisions else
                   "No existing target holds the new key.")),
            mapping_history_impact=ctx.impact((target_id,), action=(
                "Editing model keeps the target ID, mappings and history; matching results are "
                "recomputed under a new catalog context:")),
            companion_changes=(f"series: '{series}' -> '{series or name}'"
                               + ("" if not series else " (already set; the name is added to "
                                                        "notes instead)")),
            source_model_ids=tuple(ctx.source_ids(target_id)),
            in_top_100=ctx.in_top_100(target_id), snapshots={target_id: ctx.snapshot(target_id)},
        ))
    return proposals


# Type 5: GPU fields -------------------------------------------------------------------------

_DISCRETE_GPU = re.compile(r"\b(?:RTX|GTX|GeForce|MX\s?[0-9]{3}|Radeon\s+RX|Arc\s+A[0-9]{3}|"
                           r"Radeon\s+(?:5[0-9]{2}|Pro))\b", re.I)
_INTEGRATED_GPU = re.compile(r"\b(?:Iris|UHD|Intel\s+Graphics|Arc\s+Graphics|Radeon\s+[0-9]{3}M|"
                             r"Radeon\s+Graphics|Radeon™\s+Graphics|Vega\s+[0-9]|Integrated)\b",
                             re.I)


def gpu_class(text: str) -> str:
    """``DISCRETE``, ``INTEGRATED`` or ``''`` for a GPU model or a page's graphics text.

    Any discrete GPU named wins (hybrid graphics list both); unrecognized names stay unclassified.
    """
    if canonical_gpu_models(text) or _DISCRETE_GPU.search(text):
        return "DISCRETE"
    return "INTEGRATED" if _INTEGRATED_GPU.search(text) else ""


def gpu_vendor(model: str) -> str:
    if re.search(r"\b(?:RTX|GTX|GeForce|MX\s?[0-9]{3})\b", model, re.I):
        return "NVIDIA"
    if re.search(r"\bRadeon\b", model, re.I):
        return "AMD"
    if re.search(r"\b(?:Iris|UHD|Intel|Arc)\b", model, re.I):
        return "Intel"
    return ""


def _gpu_proposals(ctx: CatalogContext, offers: Sequence[OfferEvidence]) -> list[Proposal]:
    proposals = []
    for target in ctx.configuration_targets():
        target_id = _t(target, "target_id")
        brand, model = _t(target, "gpu_brand"), _t(target, "gpu_model")
        cpu_brand = _t(target, "cpu_brand")
        base = {"source_model_ids": tuple(ctx.source_ids(target_id)),
                "in_top_100": ctx.in_top_100(target_id), "snapshots": {
                    target_id: ctx.snapshot(target_id)}}
        impact = ctx.impact((target_id,), action=(
            "Editing a GPU field keeps the target ID, mappings and history:"))
        alternatives = note_alternatives(target, "gpu_model")
        # "RTX3050" and "RTX 3050" are one GPU written two ways, not a conflict.
        distinct = {tuple(sorted(canonical_gpu_models(a))) or (a,) for a in alternatives}
        if len(distinct) > 1:
            classes = {gpu_class(model), *(gpu_class(a) for a in alternatives)} - {""}
            proposals.append(Proposal(
                TYPE_GPU, (target_id,), "gpu_model", model, "", REVIEW_ONLY, [],
                "TARGET_MASTER notes (consolidation record of merged targets)",
                uncertainty=(
                    f"The catalog keeps {model}; the targets consolidated into it recorded: "
                    f"{'; '.join(alternatives)}. "
                    + ("Both integrated and discrete graphics appear among them. "
                       if len(classes) > 1 else "")
                    + "A target whose listings mix GPUs is more than one product."),
                reviewer_checks="Check which GPU each mapped Shopee listing states and whether "
                                "the consolidation merged distinct products.",
                identity_key_impact="None: GPU fields are secondary, but the merged listings may "
                                    "belong to a different configuration.",
                mapping_history_impact=impact, **base))
        vendor = gpu_vendor(model)
        if brand and vendor and brand.casefold() != vendor.casefold():
            echoes = brand.casefold() == cpu_brand.casefold()
            proposals.append(Proposal(
                TYPE_GPU, (target_id,), "gpu_brand", brand, vendor, HIGH if echoes else MEDIUM,
                [], "TARGET_MASTER fields (gpu_brand against gpu_model)",
                uncertainty=(f"gpu_model {model} is a {vendor} product, but gpu_brand records "
                             f"{brand}" + (" (the CPU brand: likely copied from it). " if echoes
                                           else ". ")
                             + "The model value itself is not checked here."),
                reviewer_checks=f"Confirm the target's GPU is {model} (not an integrated "
                                f"{brand} GPU) before changing the brand.",
                identity_key_impact="None: GPU fields are secondary.",
                mapping_history_impact=impact, **base))
        recorded_class = gpu_class(model)
        pages = identity_offers(target, offers)
        graphics = [(o, label, text) for o in pages
                    for label, text in o.spec_values(r"graphics|gpu|video")]
        page_classes = {gpu_class(text) for _, _, text in graphics} - {""}
        if recorded_class == "DISCRETE" and page_classes == {"INTEGRATED"}:
            proposals.append(Proposal(
                TYPE_GPU, (target_id,), "gpu_model", model, "", REVIEW_ONLY,
                [_evidence(o, "gpu_model", label=label, quoted=text,
                           scope="PRODUCT_SPECIFICATION") for o, label, text in graphics],
                "PRODUCT_SPECIFICATION (graphics row of identity-established pages)",
                uncertainty=f"The catalog records discrete {model}; every identity-established "
                            "page states integrated graphics only.",
                reviewer_checks="Establish which product the target is; the page may be a "
                                "different variant of the code.",
                identity_key_impact="None: GPU fields are secondary.",
                mapping_history_impact=impact, **base))
        if recorded_class == "INTEGRATED" and page_classes == {"DISCRETE"}:
            models = sorted({m for _, _, text in graphics for m in canonical_gpu_models(text)})
            proposals.append(Proposal(
                TYPE_GPU, (target_id,), "gpu_model", model, models[0] if len(models) == 1 else "",
                REVIEW_ONLY,
                [_evidence(o, "gpu_model", label=label, quoted=text,
                           scope="PRODUCT_SPECIFICATION") for o, label, text in graphics],
                "PRODUCT_SPECIFICATION (graphics row of identity-established pages)",
                uncertainty=f"The catalog records integrated {model}; the pages state a "
                            "discrete GPU.",
                reviewer_checks="Establish whether the pages describe another configuration.",
                identity_key_impact="None: GPU fields are secondary.",
                mapping_history_impact=impact, **base))
        if (not model and brand and cpu_brand and brand.casefold() in {"amd", "intel"}
                and brand.casefold() != cpu_brand.casefold()):
            proposals.append(Proposal(
                TYPE_GPU, (target_id,), "gpu_brand", brand, "", REVIEW_ONLY, [],
                "TARGET_MASTER fields (gpu_brand against cpu_brand)",
                uncertainty=(f"gpu_brand {brand} differs from cpu_brand {cpu_brand} with no "
                             "gpu_model: integrated graphics share the CPU maker, so this is "
                             f"either a discrete {brand} GPU with its model missing or a wrong "
                             "brand."),
                reviewer_checks="Check the source listings for a GPU name; no value is proposed.",
                identity_key_impact="None: GPU fields are secondary.",
                mapping_history_impact=impact, **base))
    return proposals


# Type 1: family target next to a specific target ---------------------------------------------


def _family_proposals(ctx: CatalogContext, offers: Sequence[OfferEvidence]) -> list[Proposal]:
    proposals = []
    priced = ctx.priced_targets_by_offer()
    configurations = ctx.configuration_targets()
    for family in configurations:
        family_id = _t(family, "target_id")
        if _is_code_target(family) or not _t(family, "model"):
            continue
        for specific in configurations:
            specific_id = _t(specific, "target_id")
            code = _t(specific, "model")
            if specific_id == family_id or not is_model_code(code):
                continue
            if _t(family, "brand").casefold() != _t(specific, "brand").casefold():
                continue
            if _recorded_code(family) and _recorded_code(family).casefold() != code.casefold():
                continue
            if not all(_same(name, _t(family, name), _t(specific, name)) for name in
                       CORE_MATCH_COLUMNS if name != "model"):
                continue
            own_text = " ".join(filter(None, (_t(family, "variant_notes"),
                                              _t(family, "manufacturer_model_code"))))
            ledger_text = " ".join(_t(r, "item_name") + " " + _t(r, "shopee_model_name")
                                   for r in ctx.ledger_rows(family_id))
            family_codes = {c.casefold() for c in model_codes(own_text + " " + ledger_text)}
            stated = code.casefold() in family_codes
            words = product_name_tokens(_t(specific, "variant_notes"))
            name_words = product_name_tokens(_t(family, "model"))
            family_named = bool(name_words) and any(
                words[i:i + len(name_words)] == name_words
                for i in range(len(words) - len(name_words) + 1))
            if not stated and not family_named:
                continue
            pages = [o for o in offers if code.casefold() in o.codes]
            offer_targets = {o.offer_url: sorted(
                {x for x in (o.target_id, *o.candidate_target_ids) if x}
                | priced.get(o.offer_url, set())) for o in pages}
            both = {url: ts for url, ts in offer_targets.items()
                    if {family_id, specific_id} <= set(ts)}
            evidence = [_evidence(
                o, "model", label="Page title / model", quoted=o.title, scope="TITLE",
                note=f"targets linked to this page: {', '.join(offer_targets[o.offer_url])}")
                for o in pages]
            confidence = HIGH if stated and both else MEDIUM
            ledger_codes = sorted({c for c in model_codes(ledger_text)})
            proposals.append(Proposal(
                TYPE_FAMILY_VS_SPECIFIC, (family_id, specific_id), "target_identity",
                f"{family_id} model '{_t(family, 'model')}' (family) and {specific_id} model "
                f"'{code}'",
                "owner decision (no value proposed): (a) retire "
                f"{family_id} as a duplicate of {specific_id}; (b) keep {family_id} as a family "
                f"target and record {code} as its manufacturer_model_code; (c) keep both",
                confidence, evidence,
                "TARGET_MASTER rows; variant_notes/source listing text; collected page titles",
                uncertainty=(
                    "Both targets share screen, CPU series, RAM and storage. "
                    + (f"{family_id}'s own source text states {code}. " if stated else
                       f"{specific_id}'s variant notes name the family '{_t(family, 'model')}'. ")
                    + (f"{len(both)} offer(s) price both targets: {', '.join(both)}. "
                       if both else "No collected offer prices both targets. ")
                    + (f"Source listings of {family_id} state codes: {', '.join(ledger_codes)}. "
                       if ledger_codes else "")
                    + "Whether the family target also covers other products is unknown."),
                reviewer_checks=(
                    f"Compare the Shopee listings mapped to {family_id} and {specific_id}: if "
                    f"all are {code} the targets are one configuration. Decide which ID survives "
                    "and how its mappings and history move; nothing is merged here."),
                identity_key_impact=(
                    f"Two targets hold one product: an offer for {code} is priced for both "
                    "under the listed-spec policy and counted twice in the report."),
                mapping_history_impact=ctx.impact((family_id, specific_id), action=(
                    "A merge would move the retired target's mappings and re-key its history; "
                    "not performed.")),
                source_model_ids=tuple(sorted({*ctx.source_ids(family_id),
                                               *ctx.source_ids(specific_id)})),
                in_top_100=ctx.in_top_100(family_id) or ctx.in_top_100(specific_id),
                snapshots={family_id: ctx.snapshot(family_id),
                           specific_id: ctx.snapshot(specific_id)},
            ))
    return proposals


# Driver -------------------------------------------------------------------------------------


def build_proposals(ctx: CatalogContext, offers: Sequence[OfferEvidence], *,
                    screen_brands: Iterable[str] = ("HP",)) -> list[Proposal]:
    """All proposals, ordered by type then target. Reads only: ``ctx`` and offers are unchanged."""
    brands = frozenset(b.casefold() for b in screen_brands)
    claimed: set[tuple[str, str]] = set()
    proposals = [
        *_family_proposals(ctx, offers),
        *_screen_proposals(ctx, offers, brands, claimed),
        *_msi_proposals(ctx, offers),
        *_broad_name_proposals(ctx, offers),
        *_gpu_proposals(ctx, offers),
    ]
    order = {name: index for index, name in enumerate(PROPOSAL_TYPES)}
    ordered = sorted(proposals, key=lambda p: (order[p.proposal_type], p.target_ids, p.field))
    seen: dict[str, int] = defaultdict(int)
    for proposal in ordered:
        seen[proposal.proposal_id] += 1
        proposal.suffix = f"-{seen[proposal.proposal_id]}" if seen[proposal.proposal_id] > 1 else ""
    return ordered


def summarize(proposals: Sequence[Proposal]) -> dict[str, object]:
    by_type: dict[str, int] = defaultdict(int)
    by_confidence: dict[str, int] = defaultdict(int)
    for proposal in proposals:
        by_type[proposal.proposal_type] += 1
        by_confidence[proposal.confidence] += 1
    return {"proposals": len(proposals),
            "by_type": {name: by_type.get(name, 0) for name in PROPOSAL_TYPES},
            "by_confidence": dict(sorted(by_confidence.items())),
            "applied_automatically": 0}


# Output safety and files --------------------------------------------------------------------


class OutputLocationError(ValueError):
    """The output folder is outside ``data/output`` or inside ``config``."""


def check_output_dir(path: Path, root: Path) -> Path:
    """The resolved output folder, refused unless it is under ``<root>/data/output``."""
    resolved = Path(path).resolve()
    allowed = (Path(root) / "data" / "output").resolve()
    config = (Path(root) / "config").resolve()
    if config == resolved or config in resolved.parents:
        raise OutputLocationError(f"Refusing to write into config/: {resolved}")
    if allowed != resolved and allowed not in resolved.parents:
        raise OutputLocationError(f"Output must be under {allowed}: {resolved}")
    return resolved


def readme_text(proposals: Sequence[Proposal], summary: Mapping[str, object],
                inputs: Mapping[str, str]) -> str:
    lines = [
        "# Catalog correction proposals (" + PROPOSED_ON + ")", "",
        "Proposals only. Nothing was applied: no Sheet, target ID, mapping, approval, history "
        "store or catalog snapshot was changed, and no retailer was contacted. "
        "`applies_automatically` is FALSE on every row.", "",
        "## Counts", "", "| Type | Proposals |", "|---|---:|",
        *[f"| {name} | {count} |" for name, count in summary["by_type"].items()],  # type: ignore[union-attr]
        "", "## How to review", "",
        "1. Open `catalog_proposals.csv` (one row per field change; `affected_target_ids` lists "
        "every identity involved). `catalog_proposals.json` adds the quoted evidence, the source "
        "folder and the snapshot of each target row.",
        "2. `confidence`: HIGH/MEDIUM say the evidence supports the change if the reviewer "
        "confirms the checks listed; LOW rests on the catalog's own field only; REVIEW_ONLY names a "
        "conflict with no proposed value.",
        "3. Apply accepted changes in TARGET_MASTER yourself; re-export and rebuild the selected "
        "list. A changed core field (model, screen) changes the five-field key and must be "
        "followed by a fresh matching context.", "",
        "## Inputs", "", *[f"- {name}: `{value}`" for name, value in inputs.items()], "",
        "## Proposals", "",
        "| ID | Type | Targets | Field | Old | Proposed | Confidence |",
        "|---|---|---|---|---|---|---|",
        *[f"| {p.proposal_id} | {p.proposal_type} | {', '.join(p.target_ids)} | {p.field} | "
          f"{p.old_value[:60]} | {p.proposed_value[:80] or '-'} | {p.confidence} |"
          for p in proposals],
    ]
    return "\n".join(lines) + "\n"


def write_outputs(out_dir: Path, proposals: Sequence[Proposal], inputs: Mapping[str, str],
                  input_hashes: Mapping[str, str]) -> dict[str, object]:
    """Writes ``catalog_proposals.csv``, ``catalog_proposals.json`` and ``REVIEW_README.md``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(proposals)
    with (out_dir / "catalog_proposals.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PROPOSAL_COLUMNS))
        writer.writeheader()
        writer.writerows(p.row() for p in proposals)
    document = {"proposed_on": PROPOSED_ON, "summary": summary, "inputs": dict(inputs),
                "input_sha256": dict(input_hashes),
                "proposals": [p.as_json() | {"target_ids": list(p.target_ids)}
                              for p in proposals]}
    (out_dir / "catalog_proposals.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "REVIEW_README.md").write_text(readme_text(proposals, summary, inputs),
                                              encoding="utf-8")
    return summary


def load_context(target_master: Path, *, run_dir: Path | None = None,
                 selected_list: Path | None = None, contributions: Path | None = None,
                 source_model_map: Path | None = None) -> CatalogContext:
    """The catalog snapshot plus, from one run folder, the tables the proposals cite.

    A run folder is a ``top100_*`` output folder: ``inputs/`` (selected list, contributions,
    mappings) and ``reports/`` (source ledger, selections, history). Every table is optional.
    """
    paths: dict[str, Path | None] = {"target_master": Path(target_master)}
    if run_dir is not None:
        run_dir = Path(run_dir)
        inputs, reports = run_dir / "inputs", run_dir / "reports"
        paths |= {
            "selected_list": selected_list or inputs / "top_100_verified_configurations.csv",
            "contributions": contributions or inputs / "verified_configuration_contributions.csv",
            "source_model_map": source_model_map or inputs / "source_model_map_validated.csv",
            "ledger": reports / "configuration_source_ledger.csv",
            "selections": reports / "weekly_price_selections.csv",
            "history": reports / "price_history.csv",
        }
    else:
        paths |= {"selected_list": selected_list, "contributions": contributions,
                  "source_model_map": source_model_map}
    tables = {name: (read_csv_rows(path) if path is not None and Path(path).is_file() else [])
              for name, path in paths.items()}
    targets = tables.pop("target_master")
    if not targets or "target_id" not in targets[0]:
        raise ValueError(f"{target_master} is not a TARGET_MASTER snapshot (no target_id column).")
    return CatalogContext(
        targets=targets, selected=tables["selected_list"], contributions=tables["contributions"],
        source_model_map=tables["source_model_map"], ledger=tables.get("ledger", []),
        selections=tables.get("selections", []), history=tables.get("history", []),
        inputs={name: str(path) for name, path in paths.items()
                if path is not None and Path(path).is_file()},
    )
