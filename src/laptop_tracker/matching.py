"""Conservative exact matching of normalized retailer offers to target configurations.

``ExactMatcher.match`` takes structured attributes the caller extracted with source evidence.
``ExactMatcher.match_observation`` runs the retailer extractor in ``extraction.py`` first. Neither
infers specifications from retailer titles or free text, and neither uses fuzzy matching.
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
)
from .normalization import CORE_MATCH_COLUMNS, core_key, is_blank, positive_integer, text_key
from .offers import OfferObservation

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
    return text_key(value)


def _display_value(value: object) -> str:
    return "" if is_blank(value) else str(value)


class ExactMatcher:
    """Index validated target rows and classify offers without fuzzy matching.

    ``offer_attributes`` must contain the canonical Phase 8 field names. Core numeric fields
    must already be expressed as inches, GB, or a bare positive integer as named by the field.
    ``match`` never parses ``raw_title`` or ``raw_specifications``.
    """

    def __init__(self, targets: Iterable[Mapping[str, object]] | pd.DataFrame) -> None:
        rows = (
            targets.to_dict(orient="records")
            if isinstance(targets, pd.DataFrame)
            else list(targets)
        )
        self._targets: list[_PreparedTarget] = []
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
        if len(candidates) > 1:
            return self._result(
                offer,
                MatchStatus.AMBIGUOUS,
                candidate_ids=tuple(sorted(target.target_id for target in candidates)),
                reasons=("DUPLICATE_CORE_TARGETS",),
            )

        target = candidates[0]
        if target.status != "active":
            return self._result(
                offer,
                MatchStatus.UNMATCHED,
                candidate_ids=(target.target_id,),
                reasons=("INACTIVE_TARGET",),
            )

        evidence: list[FieldMatchEvidence] = []
        conflicts: list[str] = []
        for field in CORE_MATCH_COLUMNS:
            evidence.append(
                FieldMatchEvidence(
                    field,
                    EvidenceStatus.MATCH,
                    _display_value(offer_attributes.get(field)),
                    _display_value(target.row.get(field)),
                    target.target_id,
                )
            )
        for field in SECONDARY_MATCH_COLUMNS:
            offer_value = offer_attributes.get(field)
            target_value = target.row.get(field)
            if field in ambiguous_secondary:
                # Several stated values cannot corroborate one target value.
                conflicts.append(field)
                evidence.append(
                    FieldMatchEvidence(
                        field,
                        EvidenceStatus.CONFLICT,
                        _display_value(offer_value),
                        _display_value(target_value),
                        target.target_id,
                    )
                )
                continue
            try:
                offer_key = _secondary_key(field, offer_value)
                target_key = _secondary_key(field, target_value)
            except ValueError:
                # An unparseable supporting value is a review signal, never a reason to guess.
                conflicts.append(field)
                evidence.append(
                    FieldMatchEvidence(
                        field,
                        EvidenceStatus.CONFLICT,
                        _display_value(offer_value),
                        _display_value(target_value),
                        target.target_id,
                    )
                )
                continue
            if offer_key is None or target_key is None:
                continue
            field_status = (
                EvidenceStatus.MATCH if offer_key == target_key else EvidenceStatus.CONFLICT
            )
            if field_status is EvidenceStatus.CONFLICT:
                conflicts.append(field)
            evidence.append(
                FieldMatchEvidence(
                    field,
                    field_status,
                    _display_value(offer_value),
                    _display_value(target_value),
                    target.target_id,
                )
            )

        if conflicts:
            return self._result(
                offer,
                MatchStatus.REVIEW_REQUIRED,
                candidate_ids=(target.target_id,),
                reasons=(
                    "SECONDARY_CONFLICT",
                    *(f"SECONDARY_CONFLICT_{field.upper()}" for field in conflicts),
                ),
                evidence=tuple(evidence),
            )
        return self._result(
            offer,
            MatchStatus.EXACT,
            target_id=target.target_id,
            candidate_ids=(target.target_id,),
            evidence=tuple(evidence),
        )

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
    ) -> MatchResult:
        return MatchResult(
            retailer_id=offer.retailer_id,
            offer_url=offer.offer_url,
            retrieved_at=offer.retrieved_at,
            status=status,
            target_id=target_id,
            candidate_target_ids=candidate_ids,
            reason_codes=reasons,
            evidence=evidence,
        )


def _with_extraction(result: MatchResult, extracted: ExtractedAttributes) -> MatchResult:
    return replace(
        result,
        extractor=extracted.extractor,
        extraction_evidence=extracted.evidence,
        offer_attributes=dict(extracted.values),
    )
