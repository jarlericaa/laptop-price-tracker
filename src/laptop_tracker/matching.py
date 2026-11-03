"""Conservative exact matching of normalized retailer offers to target configurations.

``ExactMatcher.match`` takes structured attributes the caller extracted with source evidence.
``ExactMatcher.match_observation`` runs the retailer extractor in ``extraction.py`` first. Neither
uses fuzzy matching, similarity scores, or four-of-five matching. A title can supply only a
complete model code, and only under the corroboration rules in ``extraction.py``; every other
core value comes from non-title evidence. ``EXACT`` means "matches this catalog configuration
under the five-field policy"; ``match_basis`` and warnings say how and what still differs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

import pandas as pd

from .extraction import (
    ExtractedAttributes,
    ExtractionStatus,
    FieldExtraction,
    extract_offer_attributes,
    match_basis,
)
from .normalization import CORE_MATCH_COLUMNS, core_key, is_blank, positive_integer, text_key
from .offers import OfferObservation
from .target_master import CONFIGURATION, LISTING_ONLY, effective_tracking_mode

SECONDARY_MATCH_COLUMNS: tuple[str, ...] = (
    "brand",
    "resolution",
    "display_type",
    "refresh_rate_hz",
    "cpu_brand",
    "cpu_generation",
    "cpu_cores",
    "ram_type",
    "storage_type",
    "storage_layout",
    "gpu_type",
    "gpu_brand",
    "gpu_model",
    "vram",
    "os",
    "battery_capacity",
    "build_material",
    "manufacturer_model_code",
    "series",
    "cpu_model",
    "color",
    "manufacturer_part_number",
)

# Secondary severity (balanced policy, 2026-10-15). A verified disagreement in a BLOCKING field
# withholds the target (REVIEW_REQUIRED); a WARNING field disagreement is reported on the EXACT
# result. Missing values on either side never create a conflict. The catalog's existing
# variant splits (os, cpu_model, manufacturer_model_code) stay distinct targets because their
# five core values or model codes differ; nothing here merges or re-labels targets.
BLOCKING = "BLOCKING"
WARNING = "WARNING"
SECONDARY_SEVERITY: dict[str, str] = {
    "brand": BLOCKING,
    "resolution": BLOCKING,
    "display_type": BLOCKING,
    "refresh_rate_hz": BLOCKING,
    "cpu_brand": BLOCKING,
    "ram_type": BLOCKING,
    "storage_type": BLOCKING,
    "storage_layout": BLOCKING,
    "gpu_type": BLOCKING,
    "gpu_brand": BLOCKING,
    "gpu_model": BLOCKING,
    "vram": BLOCKING,
    "manufacturer_model_code": BLOCKING,
    "manufacturer_part_number": BLOCKING,
    # A verified exact-processor disagreement (both sides state a full processor number, the
    # numbers differ) blocks automatic comparison even when the family agrees (2026-10-22;
    # earlier a WARNING). A missing or family-only value on either side never conflicts.
    "cpu_model": BLOCKING,
    "cpu_generation": WARNING,
    "cpu_cores": WARNING,
    "os": WARNING,
    "battery_capacity": WARNING,
    "build_material": WARNING,
    "series": WARNING,
    "color": WARNING,
}
# Severity overrides for ``ExactMatcher(..., severity_overrides=...)`` (impact previews).
# ``STRICT_CPU_MODEL`` is the default since 2026-10-22 (kept for callers that pass it);
# ``LEGACY_CPU_MODEL_WARNING`` restores the earlier policy, in which a processor-number
# contradiction (Core 5 220H vs 210H) was an EXACT match with SECONDARY_WARNING_CPU_MODEL.
STRICT_CPU_MODEL: dict[str, str] = {"cpu_model": BLOCKING}
LEGACY_CPU_MODEL_WARNING: dict[str, str] = {"cpu_model": WARNING}

_RESOLUTION = re.compile(r"([0-9]+)\s*[x×]\s*([0-9]+)\Z", re.IGNORECASE)


class MatchStatus(StrEnum):
    EXACT = "EXACT"
    UNMATCHED = "UNMATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class EvidenceStatus(StrEnum):
    MATCH = "MATCH"
    CONFLICT = "CONFLICT"
    NOT_COMPARABLE = "NOT_COMPARABLE"


@dataclass(frozen=True)
class FieldMatchEvidence:
    field: str
    status: EvidenceStatus
    offer_value: str
    target_value: str
    # Which candidate the comparison was made against; set for every target comparison.
    target_id: str = ""


@dataclass(frozen=True)
class MatchResult:
    retailer_id: str
    offer_url: str
    retrieved_at: datetime
    status: MatchStatus
    target_id: str | None
    candidate_target_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    evidence: tuple[FieldMatchEvidence, ...]
    # Set by match_observation(): the extractor used and its per-field evidence.
    extractor: str = ""
    extraction_evidence: tuple[FieldExtraction, ...] = ()
    offer_attributes: Mapping[str, str] | None = None
    # How the five fields were supported (e.g. LABELED_SPECIFICATION, TITLE_MODEL_CORROBORATED).
    match_basis: str = ""
    # Nonblocking differences on an assigned or reviewed result, e.g. SECONDARY_WARNING_COLOR.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PreparedTarget:
    target_id: str
    status: str
    row: Mapping[str, object]
    core_key: tuple[object, ...]


def _secondary_key(field: str, value: object) -> str | tuple[int, int] | int | None:
    if is_blank(value):
        return None
    if field in {"refresh_rate_hz", "cpu_cores"}:
        return positive_integer(value)
    if field == "resolution":
        match = _RESOLUTION.fullmatch(str(value).strip())
        if not match:
            raise ValueError
        width, height = (int(part) for part in match.groups())
        if width <= 0 or height <= 0:
            raise ValueError
        return width, height
    if field == "gpu_model":
        # "RTX4050", "RTX 4050" and "GeForce RTX 4050 Laptop GPU" are one GPU (2026-10-19).
        from .conventions import canonical_gpu_models

        forms = canonical_gpu_models(str(value))
        if len(forms) == 1:
            return forms.pop().casefold()
    if field == "cpu_model":
        # One exact processor model, whatever its presentation ("i5 12450H", "Intel(R) Core(TM)
        # i5-12450H"); the number and suffix are never normalized away. A value that states no
        # full processor number (a family, "Core 5", "Ryzen 7") is unknown here, never a
        # difference; several models are an unresolved statement (2026-10-22).
        from .conventions import processor_models

        models = processor_models(str(value), bare_apple=True)
        if len(models) > 1:
            raise ValueError("several processor models")
        return models[0].key if models else None
    return text_key(value)


def states_exact_processor(value: object) -> bool:
    """True when ``value`` names one full processor model (a family or blank does not)."""
    try:
        return _secondary_key("cpu_model", value) is not None
    except ValueError:
        return True  # several models: stated, and unresolved


def verified_secondary_difference(field: str, left: object, right: object) -> bool:
    """True when both values are stated, parseable and different under the matcher's own
    comparison (``_secondary_key``); a blank or unparseable value is never a difference here."""
    try:
        left_key, right_key = _secondary_key(field, left), _secondary_key(field, right)
    except ValueError:
        return False
    return left_key is not None and right_key is not None and left_key != right_key


def _display_value(value: object) -> str:
    return "" if is_blank(value) else str(value)


class ExactMatcher:
    """Index validated target rows and classify offers without fuzzy matching.

    ``offer_attributes`` must contain the canonical Phase 8 field names. Core numeric fields
    must already be expressed as inches, GB, or a bare positive integer as named by the field.
    ``match`` never parses ``raw_title`` or ``raw_specifications``.

    Only CONFIGURATION rows (``tracking_mode`` blank or ``CONFIGURATION``) are indexed.
    LISTING_ONLY rows are counted in ``excluded_listing_only`` and never produce exact matches,
    near-match candidates, or ambiguity. An unknown tracking mode is rejected.
    """

    def __init__(
        self,
        targets: Iterable[Mapping[str, object]] | pd.DataFrame,
        *,
        severity_overrides: Mapping[str, str] | None = None,
    ) -> None:
        # ``severity_overrides`` (2026-10-22) is an opt-in diagnostic/review gate, e.g.
        # ``STRICT_CPU_MODEL``. The approved policy is ``SECONDARY_SEVERITY`` unchanged; callers
        # that pass overrides must keep their results apart from the approved matching context.
        overrides = dict(severity_overrides or {})
        unknown = sorted(set(overrides) - set(SECONDARY_MATCH_COLUMNS))
        if unknown or any(value not in {BLOCKING, WARNING} for value in overrides.values()):
            raise ValueError(f"Invalid severity override(s): {', '.join(unknown) or overrides}.")
        self._severity = {**SECONDARY_SEVERITY, **overrides}
        self.severity_overrides = overrides
        rows = (
            targets.to_dict(orient="records")
            if isinstance(targets, pd.DataFrame)
            else list(targets)
        )
        self._targets: list[_PreparedTarget] = []
        self.excluded_listing_only = 0
        seen_ids: set[str] = set()
        for row_number, row in enumerate(rows, start=1):
            target_id = text_key(row.get("target_id"))
            status = text_key(row.get("status"))
            if target_id is None:
                raise ValueError(f"Target row {row_number} has no target_id.")
            if target_id in seen_ids:
                raise ValueError(f"Target rows contain a duplicate target_id at row {row_number}.")
            seen_ids.add(target_id)
            if status not in {"active", "inactive"}:
                raise ValueError(f"Target row {row_number} has an invalid status.")
            mode = effective_tracking_mode(row.get("tracking_mode"))
            if mode == LISTING_ONLY:
                # Tracked as one listing only; never part of cross-retailer matching.
                self.excluded_listing_only += 1
                continue
            if mode != CONFIGURATION:
                raise ValueError(f"Target row {row_number} has an unknown tracking_mode.")
            key, issues = core_key(row)
            if key is None:
                raise ValueError(
                    f"Target row {row_number} has incomplete or invalid core fields: "
                    f"{', '.join(issues)}."
                )
            self._targets.append(
                _PreparedTarget(
                    target_id=str(row["target_id"]), status=status, row=row, core_key=key
                )
            )

    @property
    def configuration_target_count(self) -> int:
        return len(self._targets)

    def match_observation(self, offer: OfferObservation) -> MatchResult:
        """Extract the offer's canonical fields with its retailer extractor, then match them."""
        extracted = extract_offer_attributes(offer)
        ambiguous_secondary = tuple(
            item.field
            for item in extracted.evidence
            if item.field not in CORE_MATCH_COLUMNS
            and item.status is ExtractionStatus.AMBIGUOUS
        )
        result = self.match(
            offer,
            extracted.values,
            ambiguous_secondary=ambiguous_secondary,
            extraction_reasons=extracted.reason_codes,
        )
        return _with_extraction(result, extracted)

    def _secondary_comparison(
        self,
        target: _PreparedTarget,
        offer_attributes: Mapping[str, object],
        ambiguous_secondary: tuple[str, ...],
    ) -> tuple[list[FieldMatchEvidence], list[str], list[str]]:
        """``(evidence, blocking conflicts, warnings)`` for one target's secondary fields."""
        evidence: list[FieldMatchEvidence] = []
        blocking: list[str] = []
        warnings: list[str] = []
        for field in SECONDARY_MATCH_COLUMNS:
            offer_value = offer_attributes.get(field)
            target_value = target.row.get(field)
            severity = self._severity.get(field, BLOCKING)
            if field in ambiguous_secondary:
                if field == "cpu_model" and not states_exact_processor(target_value):
                    continue  # nothing exact recorded for the target: unknown, not a conflict
                # Several stated values cannot corroborate one target value.
                status = EvidenceStatus.CONFLICT
            else:
                try:
                    offer_key = _secondary_key(field, offer_value)
                    target_key = _secondary_key(field, target_value)
                except ValueError:
                    # An unparseable supporting value is a review signal, never a reason to
                    # guess; for a warning-class field it is reported, not blocking.
                    status = EvidenceStatus.CONFLICT
                else:
                    if offer_key is None or target_key is None:
                        continue
                    status = (
                        EvidenceStatus.MATCH if offer_key == target_key
                        else EvidenceStatus.CONFLICT
                    )
            evidence.append(
                FieldMatchEvidence(
                    field,
                    status,
                    _display_value(offer_value),
                    _display_value(target_value),
                    target.target_id,
                )
            )
            if status is EvidenceStatus.CONFLICT:
                (blocking if severity == BLOCKING else warnings).append(field)
        return evidence, blocking, warnings

    def match(
        self,
        offer: OfferObservation,
        offer_attributes: Mapping[str, object],
        *,
        ambiguous_secondary: tuple[str, ...] = (),
        extraction_reasons: tuple[str, ...] = (),
    ) -> MatchResult:
        """Match one offer's extracted, structured product attributes to active targets.

        ``ambiguous_secondary`` names supporting fields whose evidence gave several values; they
        are treated as conflicts. ``extraction_reasons``, when given, replace the generic
        per-field ``MISSING_``/``INVALID_`` codes for an incomplete core.
        """
        key, issues = core_key(offer_attributes)
        if key is None:
            summary = (
                "MISSING_CORE_FIELDS"
                if all(item.startswith("MISSING_") for item in issues)
                else "INCOMPLETE_OR_INVALID_CORE_FIELDS"
            )
            # Extraction reasons already say why each core field is absent (e.g. AMBIGUOUS_...).
            reasons = (summary, *(extraction_reasons or issues))
            return self._result(offer, MatchStatus.REVIEW_REQUIRED, reasons=reasons)

        candidates = [target for target in self._targets if target.core_key == key]
        if not candidates:
            return self._unmatched(offer, offer_attributes, key)
        narrowed_by: list[str] = []
        if len(candidates) > 1:
            # Several targets share the five core values. Only a verified disagreement in a
            # blocking secondary field excludes one; a target missing that value is not
            # excluded. Target order, popularity, and the discovery seed are never used.
            exclusions = {
                target.target_id: self._verified_blocking(target, offer_attributes,
                                                          ambiguous_secondary)
                for target in candidates
            }
            remaining = [target for target in candidates if not exclusions[target.target_id]]
            if len(remaining) != 1:
                return self._result(
                    offer,
                    MatchStatus.AMBIGUOUS,
                    candidate_ids=tuple(sorted(target.target_id for target in candidates)),
                    reasons=("DUPLICATE_CORE_TARGETS",),
                )
            narrowed_by = sorted({field for fields in exclusions.values() for field in fields})
            candidates = remaining

        target = candidates[0]
        if target.status != "active":
            return self._result(
                offer,
                MatchStatus.UNMATCHED,
                candidate_ids=(target.target_id,),
                reasons=("INACTIVE_TARGET",),
            )

        evidence: list[FieldMatchEvidence] = [
            FieldMatchEvidence(
                field,
                EvidenceStatus.MATCH,
                _display_value(offer_attributes.get(field)),
                _display_value(target.row.get(field)),
                target.target_id,
            )
            for field in CORE_MATCH_COLUMNS
        ]
        secondary, conflicts, warning_fields = self._secondary_comparison(
            target, offer_attributes, ambiguous_secondary
        )
        evidence += secondary
        warnings = (
            *(code for code in extraction_reasons if is_warning_code(code)),
            *(f"SECONDARY_WARNING_{field.upper()}" for field in warning_fields),
        )
        if conflicts:
            return self._result(
                offer,
                MatchStatus.REVIEW_REQUIRED,
                candidate_ids=(target.target_id,),
                reasons=(
                    "SECONDARY_CONFLICT",
                    *(f"SECONDARY_CONFLICT_{field.upper()}" for field in conflicts),
                    *(f"SECONDARY_WARNING_{field.upper()}" for field in warning_fields),
                ),
                evidence=tuple(evidence),
                warnings=warnings,
            )
        return self._result(
            offer,
            MatchStatus.EXACT,
            target_id=target.target_id,
            candidate_ids=(target.target_id,),
            # Every extraction note (provenance such as TITLE_MODEL_CORROBORATED or
            # OWNER_APPROVED_MODEL, and warnings such as CPU_NUMBER_TITLE_DIFFERS) stays visible.
            reasons=(
                *extraction_reasons,
                *(f"DUPLICATE_CORE_NARROWED_BY_{field.upper()}" for field in narrowed_by),
                *(f"SECONDARY_WARNING_{field.upper()}" for field in warning_fields),
            ),
            evidence=tuple(evidence),
            warnings=warnings,
        )

    def _verified_blocking(
        self,
        target: _PreparedTarget,
        offer_attributes: Mapping[str, object],
        ambiguous_secondary: tuple[str, ...],
    ) -> list[str]:
        """Blocking fields where the offer and target both state a parseable, different value."""
        fields = []
        for field in SECONDARY_MATCH_COLUMNS:
            if self._severity.get(field, BLOCKING) != BLOCKING or field in ambiguous_secondary:
                continue
            try:
                offer_key = _secondary_key(field, offer_attributes.get(field))
                target_key = _secondary_key(field, target.row.get(field))
            except ValueError:
                continue
            if offer_key is not None and target_key is not None and offer_key != target_key:
                fields.append(field)
        return fields

    def _unmatched(
        self,
        offer: OfferObservation,
        offer_attributes: Mapping[str, object],
        key: tuple[object, ...],
    ) -> MatchResult:
        """No exact core key. Report active targets that differ in exactly one core field.

        Near candidates are review hints only: no ``target_id`` is assigned and no tolerance is
        applied to the differing field.
        """
        evidence: list[FieldMatchEvidence] = []
        near: list[str] = []
        for target in self._targets:
            if target.status != "active":
                continue
            differing = [
                field
                for field, offer_part, target_part in zip(
                    CORE_MATCH_COLUMNS, key, target.core_key, strict=True
                )
                if offer_part != target_part
            ]
            if len(differing) != 1:
                continue
            near.append(target.target_id)
            for field in CORE_MATCH_COLUMNS:
                evidence.append(
                    FieldMatchEvidence(
                        field,
                        EvidenceStatus.CONFLICT if field in differing else EvidenceStatus.MATCH,
                        _display_value(offer_attributes.get(field)),
                        _display_value(target.row.get(field)),
                        target.target_id,
                    )
                )
        reasons = ("NO_CORE_MATCH", "ONE_CORE_FIELD_DIFFERS") if near else ("NO_CORE_MATCH",)
        return self._result(
            offer,
            MatchStatus.UNMATCHED,
            candidate_ids=tuple(sorted(near)),
            reasons=reasons,
            evidence=tuple(evidence),
        )

    @staticmethod
    def _result(
        offer: OfferObservation,
        status: MatchStatus,
        *,
        target_id: str | None = None,
        candidate_ids: tuple[str, ...] = (),
        reasons: tuple[str, ...] = (),
        evidence: tuple[FieldMatchEvidence, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> MatchResult:
        return MatchResult(
            retailer_id=offer.retailer_id,
            offer_url=offer.offer_url,
            retrieved_at=offer.retrieved_at,
            status=status,
            target_id=target_id,
            candidate_target_ids=candidate_ids,
            reason_codes=tuple(dict.fromkeys(reasons)),
            evidence=evidence,
            warnings=tuple(dict.fromkeys(warnings)),
        )


# Extraction notes that report a difference without blocking an otherwise supported match.
_WARNING_NOTES = frozenset({
    "CPU_NUMBER_TITLE_DIFFERS",
    "COLOR_TITLE_DIFFERS",
    "MPN_REPEATS_RETAILER_SKU",
    "MODEL_URL_DIFFERS",
    "STORAGE_MIXED_MEDIA",
    "SHARED_DESCRIPTION_DIFFERS_RAM_CAPACITY_GB",
    "SHARED_DESCRIPTION_DIFFERS_STORAGE_CAPACITY_GB",
    "SHARED_TITLE_DIFFERS_RAM_CAPACITY_GB",
    "SHARED_TITLE_DIFFERS_STORAGE_CAPACITY_GB",
})


_CORE_FIELD_CODE = re.compile(
    r"(MISSING|UNPARSEABLE|AMBIGUOUS|NOT_EXTRACTED|UNRESOLVED)_"
    r"(MODEL|SCREEN_SIZE_IN|CPU_SERIES|RAM_CAPACITY_GB|STORAGE_CAPACITY_GB)"
)
_IDENTITY_CONFLICT_CODES = frozenset({
    "MODEL_CLAIM_CONFLICT", "MODEL_URL_CONFLICT", "MODEL_TITLE_CONFLICT",
    "MODEL_IDENTIFIER_CONFLICT", "SECONDARY_CONFLICT",
})

# User-facing offer outcomes, derived only from the stored status and reason codes.
MATCHED_CONFIGURATION = "MATCHED_CONFIGURATION"
DIFFERENT_CONFIGURATION = "DIFFERENT_CONFIGURATION"
MODEL_UNVERIFIED = "MODEL_UNVERIFIED"
IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
EXTRACTION_INCOMPLETE = "EXTRACTION_INCOMPLETE"
AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
INACTIVE_TARGET = "INACTIVE_TARGET"


SAME_MODEL_DIFFERENT_CONFIGURATION = "SAME_MODEL_DIFFERENT_CONFIGURATION"
# An offer for another model altogether (2026-10-19 acceptance fixes). Only an offer that was
# collected for the configuration, or confirmed for it, can carry this outcome for it.
DIFFERENT_MODEL = "DIFFERENT_MODEL"
# The target's catalog model is a broad family name (product_names.py): no offer can confirm it
# until the catalog is reconciled; the offer's own model is kept in the explanation.
CATALOG_IDENTITY_UNRESOLVED = "CATALOG_IDENTITY_UNRESOLVED"
# The offer matches the listed specifications of a broad-family target (listed_spec.py,
# 2026-10-20): comparable under that limited basis, never an exact product match.
LISTED_SPEC_MATCH = "LISTED_SPEC_MATCH"
# Core fields that vary between configurations of one manufacturer model code (memory and drive
# options). A model code fixes the screen and processor family, so a difference there is an
# unresolved specification conflict, never another configuration of the same model.
CONFIGURATION_OPTION_FIELDS: tuple[str, ...] = ("ram_capacity_gb", "storage_capacity_gb")


def core_differences(
    offer_attributes: Mapping[str, object], target_row: Mapping[str, object]
) -> list[tuple[str, str, str]]:
    """``(field, offer value, target value)`` for each core field both state differently.

    Values are compared with the matcher's own normalization; a missing value on either side is
    not a difference. Used only to explain a non-match, never to assign one.
    """
    from .normalization import core_value_key

    differences = []
    for field in CORE_MATCH_COLUMNS:
        offer_value, target_value = offer_attributes.get(field), target_row.get(field)
        if is_blank(offer_value) or is_blank(target_value):
            continue
        try:
            same = core_value_key(field, offer_value) == core_value_key(field, target_value)
        except ValueError:
            same = False
        if not same:
            differences.append((field, str(offer_value), str(target_value)))
    return differences


def describe_differences(differences: Iterable[tuple[str, str, str]]) -> str:
    return "; ".join(f"{field}: offer {offer} vs target {target}"
                     for field, offer, target in differences)


def is_model_code(model: object) -> bool:
    """True when the catalog model is one manufacturer model code (``B13UC-3474PH``), not a
    family name (``MacBook Air``) whose configurations also differ in screen or processor."""
    from .conventions import model_codes

    text = "" if is_blank(model) else str(model).strip()
    return bool(text) and [code.casefold() for code in model_codes(text)] == [text.casefold()]


def listed_spec_outcome(
    reason_codes: Iterable[str],
    offer_attributes: Mapping[str, object],
    target_row: Mapping[str, object],
    title: str,
) -> tuple[str, str] | None:
    """The outcome of an offer for a broad-family target under the listed-spec policy
    (``listed_spec.py``, 2026-10-20), or None when the target is not a family target.

    ``LISTED_SPEC_MATCH`` when the listed specifications match (never ``MATCHED_CONFIGURATION``:
    the product identity stays unresolved). Otherwise the specific reason: another brand or
    family is ``DIFFERENT_MODEL``; a required field the page does not establish is
    ``EXTRACTION_INCOMPLETE``; a listed field with another value is ``DIFFERENT_CONFIGURATION``;
    contradictory identity evidence, a contradicted recorded code or secondary specification is
    ``IDENTITY_CONFLICT``.
    """
    from . import listed_spec as spec

    check = spec.check_target(target_row, offer_attributes, title, reason_codes)
    mapping = {
        spec.QUALIFIES: LISTED_SPEC_MATCH,
        spec.BRAND_DIFFERS: DIFFERENT_MODEL,
        spec.FAMILY_NOT_STATED: DIFFERENT_MODEL,
        spec.BRAND_NOT_STATED: EXTRACTION_INCOMPLETE,
        spec.REQUIRED_SPECS_MISSING: EXTRACTION_INCOMPLETE,
        spec.LISTED_SPECS_DIFFER: DIFFERENT_CONFIGURATION,
        spec.IDENTITY_EVIDENCE_CONFLICT: IDENTITY_CONFLICT,
        spec.RECORDED_CODE_CONFLICT: IDENTITY_CONFLICT,
        spec.SECONDARY_CONFLICT: IDENTITY_CONFLICT,
        spec.TARGET_BRAND_MISSING: CATALOG_IDENTITY_UNRESOLVED,
    }
    if check.outcome == spec.NOT_FAMILY_TARGET:
        return None
    detail = check.detail
    if check.qualifies and check.offer_details:
        detail = f"{detail} {check.offer_details}"
    return mapping[check.outcome], detail


def configuration_outcome(
    match_status: str,
    assigned_target_id: str,
    reason_codes: Iterable[str],
    offer_attributes: Mapping[str, object],
    target_id: str,
    target_row: Mapping[str, object] | None,
    *,
    title: str | None = None,
) -> tuple[str, str]:
    """``(outcome, detail)`` of one matched offer for one configuration.

    ``MATCHED_CONFIGURATION`` only when the offer was confirmed for ``target_id``. A confirmed
    or complete offer for anything else is ``SAME_MODEL_DIFFERENT_CONFIGURATION`` only when the
    model agrees and every differing field is a configuration option (RAM or storage for a model
    code; any field for a family name). The same model code with another screen or processor is
    an ``IDENTITY_CONFLICT`` (unresolved specification discrepancy, no tolerance applied), and
    another model is ``DIFFERENT_MODEL``. Unresolved results keep their review outcome. Used to
    explain a blank cell; never to assign a target.

    With the offer's ``title`` (2026-10-20), a broad-family target is judged by the listed-spec
    policy instead (``listed_spec_outcome``): ``LISTED_SPEC_MATCH`` or the specific reason it is
    not one. Without a title the earlier family rule (``CATALOG_IDENTITY_UNRESOLVED``) applies.
    """
    codes = tuple(reason_codes)
    if match_status == MatchStatus.EXACT and assigned_target_id == target_id:
        return MATCHED_CONFIGURATION, ""
    if title is not None and target_row:
        listed = listed_spec_outcome(codes, offer_attributes, target_row, title)
        if listed is not None:
            confirmed = f"confirmed as {assigned_target_id}" if (
                match_status == MatchStatus.EXACT and assigned_target_id) else ""
            return listed[0], "; ".join(filter(None, (listed[1], confirmed)))
    outcome = offer_outcome(match_status, codes)
    differences = core_differences(offer_attributes, target_row or {})
    detail = describe_differences(differences)
    confirmed = f"confirmed as {assigned_target_id}" if (
        match_status == MatchStatus.EXACT and assigned_target_id
    ) else ""
    if outcome not in {MATCHED_CONFIGURATION, DIFFERENT_CONFIGURATION}:
        blocking = ";".join(review_codes(codes))
        return outcome, "; ".join(filter(None, (blocking, detail)))
    if is_blank(offer_attributes.get("model")) or any(
        field == "model" for field, _, _ in differences
    ):
        from .product_names import FAMILY_NAME, default_registry

        row = target_row or {}
        if not is_blank(offer_attributes.get("model")) and default_registry().kind(
            str(row.get("brand", "") or ""), str(row.get("model", "") or "")
        ) == FAMILY_NAME:
            return CATALOG_IDENTITY_UNRESOLVED, "; ".join(filter(None, (
                confirmed,
                f"catalog model '{row.get('model')}' is a broad family name; the offer states "
                f"'{offer_attributes.get('model')}' (see the catalog reconciliation)",
                describe_differences(d for d in differences if d[0] != "model"),
            )))
        return DIFFERENT_MODEL, "; ".join(filter(None, (confirmed, detail)))
    if not differences:
        # The five core values agree; only a secondary field told the targets apart.
        return DIFFERENT_CONFIGURATION, "; ".join(filter(None, (
            confirmed, "same core fields; secondary fields differ")))
    options = (
        CONFIGURATION_OPTION_FIELDS if is_model_code((target_row or {}).get("model"))
        else tuple(name for name in CORE_MATCH_COLUMNS if name != "model")
    )
    if all(field in options for field, _, _ in differences):
        return SAME_MODEL_DIFFERENT_CONFIGURATION, "; ".join(filter(None, (confirmed, detail)))
    return IDENTITY_CONFLICT, "; ".join(filter(None, (
        confirmed, f"same model code, conflicting specification: {detail} (unresolved)")))


def offer_outcome(status: str, reason_codes: Iterable[str]) -> str:
    """One outcome per match result. ``DIFFERENT_CONFIGURATION`` needs a complete, verified core
    that no target has; it never names a target. A missing or unresolved model with the other
    four fields present is ``MODEL_UNVERIFIED``, not a different configuration."""
    codes = set(reason_codes)
    if status == MatchStatus.EXACT:
        return MATCHED_CONFIGURATION
    if status == MatchStatus.AMBIGUOUS:
        return AMBIGUOUS_TARGET
    if status == MatchStatus.UNMATCHED:
        return INACTIVE_TARGET if "INACTIVE_TARGET" in codes else DIFFERENT_CONFIGURATION
    if codes & _IDENTITY_CONFLICT_CODES or any(
        code.startswith("TITLE_SPEC_CONFLICT_") for code in codes
    ):
        return IDENTITY_CONFLICT
    incomplete = {match[2] for code in codes if (match := _CORE_FIELD_CODE.fullmatch(code))}
    return MODEL_UNVERIFIED if incomplete == {"MODEL"} else EXTRACTION_INCOMPLETE


def match_basis_from_codes(reason_codes: Iterable[str]) -> str:
    """The match basis of a stored EXACT result, recovered from its reason codes."""
    codes = set(reason_codes)
    tags = []
    if "TITLE_MODEL_CORROBORATED" in codes:
        tags.append("TITLE_MODEL_CORROBORATED")
    if "TITLE_PRODUCT_NAME_CORROBORATED" in codes:
        tags.append("TITLE_PRODUCT_NAME_CORROBORATED")
    if "MODEL_NAME_PRESENTATION_NORMALIZED" in codes:
        tags.append("MODEL_NAME_PRESENTATION_NORMALIZED")
    if any(code.startswith("OWNER_APPROVED_") for code in codes):
        tags.append("OWNER_APPROVED_INTERPRETATION")
    if codes & {"SHARED_CPU_DISPLAY_RAM_STORAGE_ONLY", "SHARED_FIXED_RAM_COLOR_STORAGE_ONLY"}:
        tags += ["SELECTED_VARIANT", "SHARED_PRODUCT"]
    return "+".join(tags) or "LABELED_SPECIFICATION"


# Provenance and summary codes that explain nothing on their own beside the specific codes.
_BOOKKEEPING_CODES = frozenset({
    "MISSING_CORE_FIELDS", "INCOMPLETE_OR_INVALID_CORE_FIELDS",
    "MULTI_VARIANT_SHARED_DESCRIPTION", "SHARED_CPU_DISPLAY_RAM_STORAGE_ONLY",
    "SHARED_FIXED_RAM_COLOR_STORAGE_ONLY",
    "EQUIVALENT_REPEATED_FACTS_DEDUPLICATED", "TITLE_MODEL_CORROBORATED",
    "TITLE_PRODUCT_NAME_CORROBORATED", "MODEL_NAME_PRESENTATION_NORMALIZED",
    # Provenance of evidence read under a reviewed rule (2026-11-03); the values still decide.
    "TITLE_MODEL_SPACING_NORMALIZED", "CPU_DAMAGED_MARK_TITLE_CORROBORATED",
    "SCREEN_CLASS_NORMALIZED",
})


def review_codes(reason_codes: Iterable[str]) -> list[str]:
    """The reason codes a reader needs: no warnings, provenance, or supporting-evidence notes."""
    return [
        code for code in reason_codes
        if not is_warning_code(code) and code not in _BOOKKEEPING_CODES
        and not code.startswith("SUPPORTING_EVIDENCE_")
    ] or [code for code in reason_codes if not is_warning_code(code)]


def is_warning_code(code: str) -> bool:
    """True for a nonblocking difference reported on an EXACT (or reviewed) result."""
    return code in _WARNING_NOTES or code.startswith("SECONDARY_WARNING_")


def _with_extraction(result: MatchResult, extracted: ExtractedAttributes) -> MatchResult:
    return replace(
        result,
        extractor=extracted.extractor,
        extraction_evidence=extracted.evidence,
        offer_attributes=dict(extracted.values),
        match_basis=match_basis(extracted.evidence)
        if result.status is MatchStatus.EXACT else "",
    )
