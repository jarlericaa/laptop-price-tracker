"""Close-alternative comparisons: at most two established differences (2026-10-27).

Policy (owner decision, 2026-10-27). A retailer offer that is not a matching configuration of a
target may still be shown as a clearly labelled **alternative** when it differs from the target in
at most two established specifications. Six dimensions share the two-difference budget:

1. model, 2. screen size, 3. CPU, 4. installed RAM, 5. installed storage (the five core
dimensions), and 6. GPU. GPU is not a separate allowance. Each dimension is compared once:
several changed CPU attributes (series, exact processor, brand, cores) are one CPU difference and
several changed GPU attributes (model, brand, type, VRAM) are one GPU difference. A changed CPU
series together with a changed exact processor is also one difference, never two.

Every dimension is one of ``AGREES``, ``DIFFERS``, ``UNKNOWN`` or ``CONFLICTING_EVIDENCE``. A
*difference* is a consistent disagreement between the target and the retailer. A contradiction
inside the retailer's own evidence (two screen sizes, a title and a specification that disagree
about RAM, conflicting model codes, several GPUs not bound to the offer) is not an ordinary
difference: it is unresolved evidence and the offer is a ``RECHECK``. A missing value is never
agreement: all five core dimensions must be established on both sides, at the approved granularity
(a broad-family target is compared at family level, the specific model stays unknown). GPU may be
unknown: it is then recorded as unknown with its limitation, never as agreement and never as a
difference.

Candidate scope. An offer is compared only with targets of the same brand and the same recognized
product family (``product_families``). The same manufacturer model code always names the same
product. Different established codes inside the family are one model difference.

Material conflicts outside the six dimensions (resolution, panel type, refresh rate, RAM type,
drive type or layout, a recorded part number) keep their existing blocking policy: they make the
offer a ``RECHECK`` and are never dropped because the counted differences are within two. Warning
class differences (operating system, colour, ...) are carried as warnings.

Zero established differences never create a match: such an offer is not an alternative, and the
existing exact-product and listed-spec rules alone decide whether it is a ``MATCH``.

This module compares and explains. It never assigns a target, never changes a stored match, never
edits catalog values and never rounds or aliases a specification.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .conventions import canonical_gpu_models, is_model_code, product_name_tokens
from .extraction import ExtractedAttributes, ExtractionStatus
from .listed_spec import _contains
from .matching import (
    BLOCKING,
    SECONDARY_SEVERITY,
    _secondary_key,
    is_warning_code,
    verified_secondary_difference,
)
from .normalization import core_value_key, is_blank, text_key
from .product_families import FamilyRegistry, default_family_registry
from .product_names import FAMILY_NAME, MODEL_CODE, PRODUCT_NAME, default_registry
from .target_master import CONFIGURATION, effective_tracking_mode

POLICY_VERSION = "close-alternatives/2026-10-27"
MAX_DIFFERENCES = 2

MODEL, SCREEN, CPU, RAM, STORAGE, GPU = "model", "screen", "cpu", "ram", "storage", "gpu"
DIMENSIONS: tuple[str, ...] = (MODEL, SCREEN, CPU, RAM, STORAGE, GPU)
CORE_DIMENSIONS: tuple[str, ...] = DIMENSIONS[:5]
# Labels in tags ("ALT 2 · CPU, GPU") and in prose explanations ("RAM: ...; storage: ...").
TAG_LABELS = {MODEL: "Model", SCREEN: "Screen", CPU: "CPU", RAM: "RAM", STORAGE: "Storage",
              GPU: "GPU"}
PROSE_LABELS = {MODEL: "model", SCREEN: "screen", CPU: "CPU", RAM: "RAM", STORAGE: "storage",
                GPU: "GPU"}
# The extraction field behind each dimension's offer value.
DIMENSION_FIELDS = {MODEL: ("model",), SCREEN: ("screen_size_in",),
                    CPU: ("cpu_series", "cpu_model"), RAM: ("ram_capacity_gb",),
                    STORAGE: ("storage_capacity_gb",), GPU: ("gpu_model",)}

AGREES, DIFFERS, UNKNOWN, CONFLICTING = "AGREES", "DIFFERS", "UNKNOWN", "CONFLICTING_EVIDENCE"

# Evaluation statuses.
ALTERNATIVE = "ALTERNATIVE"
RECHECK = "RECHECK"
NO_DIFFERENCE = "NO_DIFFERENCE"
EXCLUDED_DIFFERENCES = "EXCLUDED_TOO_MANY_DIFFERENCES"
EXCLUDED_FAMILY = "EXCLUDED_BRAND_OR_FAMILY"

# Secondary fields folded into a counted dimension, so that they are never counted twice.
_FOLDED = {
    CPU: ("cpu_brand", "cpu_model", "cpu_generation", "cpu_cores"),
    GPU: ("gpu_type", "gpu_brand", "gpu_model", "vram"),
    MODEL: ("brand", "manufacturer_model_code"),
}
FOLDED_SECONDARY = frozenset(name for names in _FOLDED.values() for name in names)
# Blocking secondary fields outside the six dimensions: a verified conflict is a RECHECK.
MATERIAL_FIELDS: tuple[str, ...] = tuple(
    name for name, severity in SECONDARY_SEVERITY.items()
    if severity == BLOCKING and name not in FOLDED_SECONDARY)
# Warning-class fields are carried as notes.
WARNING_FIELDS: tuple[str, ...] = tuple(
    name for name, severity in SECONDARY_SEVERITY.items()
    if severity != BLOCKING and name not in FOLDED_SECONDARY)

_MODEL_CONFLICT_CODES = frozenset({
    "MODEL_CLAIM_CONFLICT", "MODEL_URL_CONFLICT", "MODEL_TITLE_CONFLICT",
    "MODEL_IDENTIFIER_CONFLICT", "TITLE_MULTIPLE_MODEL_CODES", "TITLE_MULTIPLE_PRODUCT_NAMES",
    "AMBIGUOUS_MODEL",
})
_CORE_FIELD_OF = {MODEL: "model", SCREEN: "screen_size_in", CPU: "cpu_series",
                  RAM: "ram_capacity_gb", STORAGE: "storage_capacity_gb"}


def _text(row: Mapping[str, object], name: str) -> str:
    value = row.get(name)
    return "" if is_blank(value) else str(value).strip()


def capacity_text(value: str) -> str:
    """'16' -> '16GB', '1000' -> '1TB' (display only)."""
    try:
        amount = int(Decimal(value))
    except (ArithmeticError, ValueError):
        return value
    return f"{amount // 1000}TB" if amount >= 1000 and amount % 1000 == 0 else f"{amount}GB"


def screen_text(value: str) -> str:
    try:
        return format(Decimal(value).normalize(), "f") + "in"
    except (ArithmeticError, ValueError):
        return value


def alternative_tag(level: int, dimensions: Iterable[str]) -> str:
    """``ALT 2 · CPU, GPU``: the difference count and the differing dimensions, in the fixed
    dimension order."""
    chosen = [d for d in DIMENSIONS if d in set(dimensions)]
    return f"ALT {level} · " + ", ".join(TAG_LABELS[d] for d in chosen)


@dataclass(frozen=True)
class OfferEvidence:
    """What the offer's own page established, for comparison with a target.

    ``values`` are the extractor's EXTRACTED canonical values only; ``ambiguous`` names fields
    whose own evidence stated several or contradictory values (they carry no value).
    """

    values: Mapping[str, str]
    title: str = ""
    reason_codes: tuple[str, ...] = ()
    ambiguous: frozenset[str] = frozenset()
    references: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    observation_id: str = ""
    content_sha256: str = ""

    def value(self, name: str) -> str:
        return _text(self.values, name)

    def is_ambiguous(self, name: str) -> bool:
        return name in self.ambiguous


def offer_evidence(
    extracted: ExtractedAttributes, title: str, *, observation_id: str = "",
    content_sha256: str = "",
) -> OfferEvidence:
    """The offer evidence of one extraction (the same extraction the matcher used)."""
    codes = tuple(extracted.reason_codes)
    ambiguous = {item.field for item in extracted.evidence
                 if item.status is ExtractionStatus.AMBIGUOUS}
    for code in codes:
        for prefix in ("AMBIGUOUS_", "TITLE_SPEC_CONFLICT_"):
            if code.startswith(prefix):
                ambiguous.add(code[len(prefix):].lower())
    references: dict[str, list[str]] = {}
    for item in extracted.evidence:
        if item.status is ExtractionStatus.SUPPORTING:
            continue
        label = f"{item.source_label}"[:90]
        scope = f"; scope {item.scope}" if item.scope else ""
        references.setdefault(item.field, []).append(
            f"{item.field} {item.status.value}: {item.value[:60]} [{label}{scope}]")
    return OfferEvidence(
        dict(extracted.values), title, codes, frozenset(ambiguous),
        {name: tuple(refs) for name, refs in references.items()}, observation_id,
        content_sha256,
    )


@dataclass(frozen=True)
class DimensionComparison:
    dimension: str
    state: str
    target_value: str = ""
    offer_value: str = ""
    target_normalized: str = ""
    offer_normalized: str = ""
    # How the comparison was made: EXACT_PROCESSOR / SERIES_ONLY (CPU), MODEL_CODE /
    # FAMILY_ONLY / PRODUCT_NAME (model), INSTALLED_CAPACITY, ... Limited bases are visible.
    basis: str = ""
    evidence: tuple[str, ...] = ()
    explanation: str = ""

    def to_record(self) -> dict[str, object]:
        return {
            "dimension": self.dimension, "state": self.state, "target": self.target_value,
            "retailer": self.offer_value, "target_normalized": self.target_normalized,
            "retailer_normalized": self.offer_normalized, "basis": self.basis,
            "evidence": list(self.evidence), "explanation": self.explanation,
        }


@dataclass(frozen=True)
class AlternativeEvaluation:
    """One offer compared with one target under the close-alternative policy."""

    target_id: str
    status: str
    comparisons: tuple[DimensionComparison, ...] = ()
    # Why the status: EVIDENCE_CONFLICT_RAM, REQUIRED_CPU_NOT_ESTABLISHED, MATERIAL_CONFLICT_...
    reasons: tuple[str, ...] = ()
    # Remaining limitations the reader must see (CPU_EXACT_PROCESSOR_UNKNOWN, GPU_UNKNOWN, ...).
    limitations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    material_conflicts: tuple[str, ...] = ()
    family: str = ""
    family_basis: str = ""
    policy_version: str = POLICY_VERSION
    detail: str = ""

    def dimension(self, name: str) -> DimensionComparison | None:
        return next((c for c in self.comparisons if c.dimension == name), None)

    def _with(self, state: str) -> tuple[str, ...]:
        return tuple(c.dimension for c in self.comparisons if c.state == state)

    @property
    def differing(self) -> tuple[str, ...]:
        return self._with(DIFFERS)

    @property
    def unknown(self) -> tuple[str, ...]:
        return self._with(UNKNOWN)

    @property
    def conflicting(self) -> tuple[str, ...]:
        return self._with(CONFLICTING)

    @property
    def difference_count(self) -> int:
        return len(self.differing)

    @property
    def is_alternative(self) -> bool:
        return self.status == ALTERNATIVE

    @property
    def tag(self) -> str:
        if self.status == ALTERNATIVE:
            return alternative_tag(self.difference_count, self.differing)
        return "RECHECK" if self.status == RECHECK else ""

    @property
    def differences_text(self) -> str:
        """``RAM: target 16GB → retailer 8GB; storage: target 512GB → retailer 256GB.``"""
        parts = [c.explanation for c in self.comparisons if c.state == DIFFERS]
        return ("; ".join(parts) + ".") if parts else ""

    @property
    def unknown_text(self) -> str:
        return "; ".join(
            f"{PROSE_LABELS[c.dimension]}: {c.explanation}" for c in self.comparisons
            if c.state == UNKNOWN)

    @property
    def comparison_basis(self) -> str:
        """The comparison granularity of each dimension that is not a plain value comparison."""
        parts = [f"{PROSE_LABELS[c.dimension]} {c.basis}" for c in self.comparisons
                 if c.basis in LIMITED_BASES]
        return "; ".join(parts)

    def to_json(self) -> str:
        return json.dumps([c.to_record() for c in self.comparisons], ensure_ascii=False,
                          separators=(",", ":"))


# Bases that say the comparison was made at a coarser granularity than a full value.
LIMITED_BASES = frozenset({"SERIES_ONLY", "FAMILY_ONLY"})


class AlternativeEvaluator:
    """Compare offers with a fixed set of targets (the selected configurations)."""

    def __init__(
        self,
        targets: Iterable[Mapping[str, object]],
        *,
        families: FamilyRegistry | None = None,
    ) -> None:
        self.families = families or default_family_registry()
        self.names = default_registry()
        self.targets: dict[str, Mapping[str, object]] = {}
        for row in targets:
            if text_key(row.get("status")) != "active":
                continue
            if effective_tracking_mode(row.get("tracking_mode")) != CONFIGURATION:
                continue
            self.targets[_text(row, "target_id")] = row
        self._by_brand: dict[tuple[str, ...], list[str]] = {}
        for target_id, row in self.targets.items():
            self._by_brand.setdefault(tuple(product_name_tokens(_text(row, "brand"))), []).append(
                target_id)

    # Candidate scope

    def candidate_ids(self, evidence: OfferEvidence, *, always: Iterable[str] = ()) -> list[str]:
        """Targets of the offer's brand (the page's brand field, or the brand as whole words in
        the title), plus ``always`` (targets named by lineage, so their exclusion is visible)."""
        words = product_name_tokens(evidence.title)
        stated = product_name_tokens(evidence.value("brand"))
        found = [
            target_id
            for brand, ids in self._by_brand.items()
            if brand and (_contains(words, list(brand)) or _contains(stated, list(brand)))
            for target_id in ids
        ]
        extra = [t for t in always if t in self.targets and t not in found]
        return sorted([*found, *extra])

    def evaluate_offer(
        self, evidence: OfferEvidence, *, always: Iterable[str] = (),
        skip: Iterable[str] = (),
    ) -> list[AlternativeEvaluation]:
        """The offer against every candidate target, except those in ``skip`` (its own matches)."""
        skipped = set(skip)
        return [self.evaluate(self.targets[t], evidence) for t in
                self.candidate_ids(evidence, always=always) if t not in skipped]

    # One comparison

    def evaluate(self, target: Mapping[str, object], evidence: OfferEvidence,
                 ) -> AlternativeEvaluation:
        target_id = _text(target, "target_id")
        scope = self._scope(target, evidence)
        if scope[0] != "OK":
            return AlternativeEvaluation(
                target_id, EXCLUDED_FAMILY, reasons=(scope[0],), detail=scope[1],
                family=scope[2], family_basis=scope[3])
        family, family_basis = scope[2], scope[3]
        comparisons = (
            self._model(target, evidence, family_basis), self._screen(target, evidence),
            self._cpu(target, evidence), self._capacity(RAM, target, evidence),
            self._capacity(STORAGE, target, evidence), self._gpu(target, evidence),
        )
        limitations = self._limitations(comparisons)
        material = self._material_conflicts(target, evidence)
        warnings = self._warnings(target, evidence)
        differing = [c.dimension for c in comparisons if c.state == DIFFERS]
        reasons: list[str] = []
        if len(differing) > MAX_DIFFERENCES:
            status = EXCLUDED_DIFFERENCES
            reasons.append(f"{len(differing)}_DIFFERENCES")
        else:
            for c in comparisons:
                if c.state == CONFLICTING:
                    reasons.append(f"EVIDENCE_CONFLICT_{c.dimension.upper()}")
            for c in comparisons:
                if c.state == UNKNOWN and c.dimension in CORE_DIMENSIONS:
                    reasons.append(f"REQUIRED_{c.dimension.upper()}_NOT_ESTABLISHED")
            reasons += [f"MATERIAL_CONFLICT_{name.upper()}" for name in material]
            if reasons:
                status = RECHECK
            elif not differing:
                status = NO_DIFFERENCE
            else:
                status = ALTERNATIVE
        return AlternativeEvaluation(
            target_id, status, comparisons, tuple(reasons), tuple(limitations), tuple(warnings),
            tuple(material), family, family_basis,
        )

    # Scope: brand and recognized family

    def _scope(self, target: Mapping[str, object], evidence: OfferEvidence,
               ) -> tuple[str, str, str, str]:
        """``("OK" | reason, detail, family label, family basis)``."""
        brand = _text(target, "brand")
        if not brand:
            return "TARGET_BRAND_MISSING", "the catalog records no brand for this target", "", ""
        brand_words = product_name_tokens(brand)
        title_words = product_name_tokens(evidence.title)
        stated = _text(evidence.values, "brand")
        in_title = _contains(title_words, brand_words)
        if stated and not _contains(product_name_tokens(stated), brand_words) and not in_title:
            return "BRAND_DIFFERS", f"page brand {stated}; target brand {brand}", "", ""
        if not stated and not in_title:
            return "BRAND_NOT_STATED", f"the page does not state the brand {brand}", "", ""
        target_family = self.families.recognize(
            brand, _text(target, "series"), _text(target, "model"))
        offer_family = self.families.recognize(brand, evidence.title, evidence.value("model"))
        same_code = self._same_code(target, evidence)
        if same_code:
            label = target_family.family.label if target_family.family else (
                offer_family.family.label if offer_family.family else "")
            return "OK", "", label, "SAME_MODEL_CODE"
        if target_family.family is None:
            return ("FAMILY_NOT_RECOGNIZED", "the target's family is not in the reviewed family "
                    "registry", "", "")
        if offer_family.family is None:
            reason = "FAMILY_AMBIGUOUS" if offer_family.ambiguous else "FAMILY_NOT_STATED"
            return (reason, f"the offer does not name the family {target_family.family.label}",
                    target_family.family.label, "")
        if not self.families.same_family(target_family.family, offer_family.family):
            return ("FAMILY_DIFFERS", f"offer family {offer_family.family.label}; target family "
                    f"{target_family.family.label}", target_family.family.label, "")
        basis = ("SAME_FAMILY" if target_family.family.key == offer_family.family.key
                 else "INCLUDED_FAMILY")
        return "OK", "", target_family.family.label, basis

    def _target_code(self, target: Mapping[str, object]) -> str:
        """The manufacturer model code the catalog records for the target, or ''."""
        model = _text(target, "model")
        if is_model_code(model):
            return model
        recorded = _text(target, "manufacturer_model_code")
        return recorded if is_model_code(recorded) else ""

    def _offer_code(self, evidence: OfferEvidence) -> str:
        model = evidence.value("model")
        return model if is_model_code(model) else ""

    def _same_code(self, target: Mapping[str, object], evidence: OfferEvidence) -> bool:
        wanted, stated = self._target_code(target), self._offer_code(evidence)
        return bool(wanted and stated and wanted.casefold() == stated.casefold())

    # Dimensions

    def _conflict(self, dimension: str, evidence: OfferEvidence) -> bool:
        if dimension == MODEL:
            return bool(set(evidence.reason_codes) & _MODEL_CONFLICT_CODES) or (
                evidence.is_ambiguous("model"))
        return any(evidence.is_ambiguous(name) for name in DIMENSION_FIELDS[dimension])

    def _refs(self, dimension: str, evidence: OfferEvidence) -> tuple[str, ...]:
        return tuple(ref for name in DIMENSION_FIELDS[dimension]
                     for ref in evidence.references.get(name, ()))

    def _model(self, target: Mapping[str, object], evidence: OfferEvidence, family_basis: str,
               ) -> DimensionComparison:
        brand, model = _text(target, "brand"), _text(target, "model")
        kind = self.names.kind(brand, model)
        offer_model = evidence.value("model")
        refs = self._refs(MODEL, evidence)
        target_code = self._target_code(target)
        if self._conflict(MODEL, evidence):
            codes = sorted(set(evidence.reason_codes) & _MODEL_CONFLICT_CODES)
            return DimensionComparison(
                MODEL, CONFLICTING, model, offer_model, evidence=refs,
                explanation="the retailer's own model evidence is contradictory"
                + (f" ({', '.join(codes)})" if codes else ""))
        if kind == MODEL_CODE or (kind == FAMILY_NAME and target_code):
            wanted = target_code or model
            offer_code = self._offer_code(evidence)
            if not offer_code:
                if kind == MODEL_CODE or offer_model:
                    why = (f"the retailer states '{offer_model}', not a model code" if offer_model
                           else "the retailer's model code is not established")
                    return DimensionComparison(MODEL, UNKNOWN, wanted, offer_model,
                                               basis="MODEL_CODE", evidence=refs, explanation=why)
                # A family-name target with a recorded code and an offer stating none: compared
                # at family level (the recorded code is neither confirmed nor contradicted).
                return DimensionComparison(
                    MODEL, AGREES, wanted, "", text_key(wanted) or "", "", "FAMILY_ONLY", refs,
                    f"the offer names the family; recorded code {wanted} is not stated")
            same = wanted.casefold() == offer_code.casefold()
            return DimensionComparison(
                MODEL, AGREES if same else DIFFERS, wanted, offer_code,
                text_key(wanted) or "", text_key(offer_code) or "", "MODEL_CODE", refs,
                "" if same else f"model: target {wanted} → retailer {offer_code}")
        if kind == PRODUCT_NAME:
            if not offer_model:
                return DimensionComparison(
                    MODEL, UNKNOWN, model, "", basis="PRODUCT_NAME", evidence=refs,
                    explanation="the retailer's product name is not established")
            same = text_key(model) == text_key(offer_model)
            return DimensionComparison(
                MODEL, AGREES if same else DIFFERS, model, offer_model, text_key(model) or "",
                text_key(offer_model) or "", "PRODUCT_NAME", refs,
                "" if same else f"model: target {model} → retailer {offer_model}")
        # A broad family name: compared at family granularity. The specific model stays unknown
        # and no exact identity is created from the family agreement (listed-spec policy).
        return DimensionComparison(
            MODEL, AGREES, model, offer_model, text_key(model) or "", text_key(offer_model) or "",
            "FAMILY_ONLY", refs,
            f"family '{model}' named by the retailer; the specific Shopee model is unknown")

    def _screen(self, target: Mapping[str, object], evidence: OfferEvidence,
                ) -> DimensionComparison:
        wanted, stated = _text(target, "screen_size_in"), evidence.value("screen_size_in")
        refs = self._refs(SCREEN, evidence)
        if self._conflict(SCREEN, evidence):
            return DimensionComparison(
                SCREEN, CONFLICTING, wanted, "", evidence=refs,
                explanation="the retailer states different screen sizes for this offer")
        try:
            left, right = core_value_key("screen_size_in", wanted), core_value_key(
                "screen_size_in", stated)
        except ValueError:
            left = right = None
        if left is None or right is None:
            return DimensionComparison(
                SCREEN, UNKNOWN, wanted, stated, evidence=refs,
                explanation="screen size not established " + (
                    "on the retailer page" if left is not None else "for the target"))
        same = left == right
        return DimensionComparison(
            SCREEN, AGREES if same else DIFFERS, wanted, stated, str(left), str(right),
            "INSTALLED", refs,
            "" if same else f"screen: target {screen_text(wanted)} → retailer "
            f"{screen_text(stated)}")

    def _capacity(self, dimension: str, target: Mapping[str, object], evidence: OfferEvidence,
                  ) -> DimensionComparison:
        name = _CORE_FIELD_OF[dimension]
        wanted, stated = _text(target, name), evidence.value(name)
        refs = self._refs(dimension, evidence)
        if self._conflict(dimension, evidence):
            return DimensionComparison(
                dimension, CONFLICTING, wanted, "", evidence=refs,
                explanation=f"the retailer's own evidence states different installed "
                f"{PROSE_LABELS[dimension]} values for this offer")
        try:
            left, right = core_value_key(name, wanted), core_value_key(name, stated)
        except ValueError:
            left = right = None
        if left is None or right is None:
            return DimensionComparison(
                dimension, UNKNOWN, wanted, stated, basis="INSTALLED_CAPACITY", evidence=refs,
                explanation=f"installed {PROSE_LABELS[dimension]} not established "
                + ("on the retailer page" if left is not None else "for the target"))
        same = left == right
        return DimensionComparison(
            dimension, AGREES if same else DIFFERS, capacity_text(wanted), capacity_text(stated),
            str(left), str(right), "INSTALLED_CAPACITY", refs,
            "" if same else f"{PROSE_LABELS[dimension]}: target {capacity_text(wanted)} → "
            f"retailer {capacity_text(stated)}")

    def _cpu(self, target: Mapping[str, object], evidence: OfferEvidence) -> DimensionComparison:
        t_series, o_series = _text(target, "cpu_series"), evidence.value("cpu_series")
        refs = self._refs(CPU, evidence)
        if evidence.is_ambiguous("cpu_series"):
            return DimensionComparison(
                CPU, CONFLICTING, t_series, "", evidence=refs,
                explanation="the retailer states different processor families for this offer")
        t_exact = _text(target, "cpu_model")
        o_exact = evidence.value("cpu_model")
        try:
            t_key = _secondary_key("cpu_model", t_exact)
            o_key = _secondary_key("cpu_model", o_exact)
        except ValueError:
            t_key = o_key = None
        if evidence.is_ambiguous("cpu_model") and t_key is not None:
            return DimensionComparison(
                CPU, CONFLICTING, t_exact or t_series, "", evidence=refs,
                explanation="the retailer states several exact processors for this offer")
        if not t_series or not o_series:
            return DimensionComparison(
                CPU, UNKNOWN, t_series, o_series, basis="SERIES", evidence=refs,
                explanation="CPU series not established "
                + ("on the retailer page" if t_series else "for the target"))
        series_same = text_key(t_series) == text_key(o_series)
        # Brand, generation or core-count differences are attributes of this one dimension.
        attribute_difference = [
            name for name in ("cpu_brand", "cpu_generation", "cpu_cores")
            if verified_secondary_difference(name, target.get(name), evidence.values.get(name))
        ]
        if not series_same:
            return DimensionComparison(
                CPU, DIFFERS, t_series, o_series, text_key(t_series) or "",
                text_key(o_series) or "", "SERIES", refs,
                f"CPU: target {t_series} → retailer {o_series}")
        if t_key is not None and o_key is not None:
            if t_key != o_key:
                return DimensionComparison(
                    CPU, DIFFERS, t_exact, o_exact, str(t_key), str(o_key), "EXACT_PROCESSOR",
                    refs, f"CPU: target {t_exact} → retailer {o_exact}")
            exact = True
        else:
            exact = False
        if attribute_difference:
            return DimensionComparison(
                CPU, DIFFERS, t_series, o_series, text_key(t_series) or "",
                text_key(o_series) or "", "SERIES", refs,
                f"CPU: target {t_series} → retailer {o_series} (differs in "
                f"{', '.join(attribute_difference)})")
        if exact:
            return DimensionComparison(
                CPU, AGREES, t_exact, o_exact, str(t_key), str(o_key), "EXACT_PROCESSOR", refs)
        side = ("the target" if t_key is None else "") + (
            " and " if t_key is None and o_key is None else "") + (
            "the retailer" if o_key is None else "")
        return DimensionComparison(
            CPU, AGREES, t_series, o_series, text_key(t_series) or "", text_key(o_series) or "",
            "SERIES_ONLY", refs,
            f"series {t_series} agrees; exact processor not stated by {side}")

    def _gpu(self, target: Mapping[str, object], evidence: OfferEvidence) -> DimensionComparison:
        refs = self._refs(GPU, evidence)
        t_model, o_model = _text(target, "gpu_model"), evidence.value("gpu_model")
        stated_target = any(_text(target, n) for n in _FOLDED[GPU])
        if evidence.is_ambiguous("gpu_model"):
            if stated_target:
                return DimensionComparison(
                    GPU, CONFLICTING, t_model, "", evidence=refs,
                    explanation="the retailer lists several GPUs without binding one to this "
                    "offer; none is chosen")
            return DimensionComparison(
                GPU, UNKNOWN, "", "", evidence=refs,
                explanation="the target records no GPU; the retailer lists several GPUs "
                "without binding one to this offer (ambiguity kept)")
        differing, compared = [], []
        for name in _FOLDED[GPU]:
            t_value, o_value = target.get(name), evidence.values.get(name)
            if is_blank(t_value) or is_blank(o_value):
                continue
            try:
                same = _secondary_key(name, t_value) == _secondary_key(name, o_value)
            except ValueError:
                continue
            compared.append(name)
            if not same:
                differing.append(name)
        o_display = o_model or " ".join(
            filter(None, (evidence.value("gpu_brand"), evidence.value("gpu_type"))))
        t_display = t_model or " ".join(
            filter(None, (_text(target, "gpu_brand"), _text(target, "gpu_type"))))
        if differing:
            return DimensionComparison(
                GPU, DIFFERS, t_display, o_display, _gpu_key(t_model), _gpu_key(o_model),
                "GPU_ATTRIBUTES", refs, f"GPU: target {t_display} → retailer {o_display}")
        if compared:
            return DimensionComparison(
                GPU, AGREES, t_display, o_display, _gpu_key(t_model), _gpu_key(o_model),
                "GPU_ATTRIBUTES", refs)
        if stated_target:
            why = "GPU not stated by the retailer; the target records " + (t_display or "one")
        elif o_display:
            why = f"GPU not recorded for the target; the retailer states {o_display}"
        else:
            why = "GPU not stated by the target or the retailer"
        return DimensionComparison(
            GPU, UNKNOWN, t_display, o_display, evidence=refs, explanation=why)

    # Limitations, material conflicts and warnings

    def _limitations(self, comparisons: Sequence[DimensionComparison]) -> list[str]:
        found = []
        for c in comparisons:
            if c.dimension == CPU and c.basis == "SERIES_ONLY":
                found.append("CPU_EXACT_PROCESSOR_UNKNOWN")
            if c.dimension == MODEL and c.basis == "FAMILY_ONLY":
                found.append("SPECIFIC_MODEL_UNKNOWN")
            if c.dimension == GPU and c.state == UNKNOWN:
                found.append("GPU_UNKNOWN")
        return found

    def _material_conflicts(self, target: Mapping[str, object], evidence: OfferEvidence,
                            ) -> list[str]:
        found = []
        for name in MATERIAL_FIELDS:
            if evidence.is_ambiguous(name):
                if not is_blank(target.get(name)):
                    found.append(name)
                continue
            if verified_secondary_difference(name, evidence.values.get(name), target.get(name)):
                found.append(name)
        return found

    def _warnings(self, target: Mapping[str, object], evidence: OfferEvidence) -> list[str]:
        found = [code for code in evidence.reason_codes if is_warning_code(code)]
        found += [f"SECONDARY_WARNING_{name.upper()}" for name in WARNING_FIELDS
                  if verified_secondary_difference(name, evidence.values.get(name),
                                                   target.get(name))]
        return list(dict.fromkeys(found))


def _gpu_key(value: str) -> str:
    if not value:
        return ""
    forms = canonical_gpu_models(value)
    return forms.pop().casefold() if len(forms) == 1 else (text_key(value) or "")
