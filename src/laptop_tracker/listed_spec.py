"""Listed-specification comparison for broad-family targets (2026-10-20).

Many catalog targets carry only what Shopee lists: a brand, a broad family name (``IdeaPad``,
``Aspire Lite``, ``Vivobook``) and five specifications. No retailer page can be shown to be the
*same product* as such a target, because the Shopee source names no specific model. This module
decides a weaker, explicitly labelled relationship instead: the offer **matches the listed
specifications** of the target.

An offer is a listed-spec match for a target only when all of these hold, using the offer's own
extracted evidence (never values supplied from the target that initiated a search):

- the target's catalog model is a broad family name (``product_names`` kind ``FAMILY_NAME``) and
  the target records a brand;
- the brand matches: the page's brand field, or the brand as a whole word in the title; a brand
  field naming another brand, without the target brand in the title, is a mismatch;
- the family name appears in the title (whole words in order; case, spacing and punctuation are
  presentation; a joined spelling such as ``VivoBook`` for ``Vivo Book`` is the same name);
- screen size, CPU series, installed RAM and installed storage were all extracted from the page
  and equal the target's under the matcher's normalization (exact screen sizes; the approved
  CPU-family forms, so ``Core i5``, ``Core 5`` and ``Core Ultra 5`` stay distinct);
- the page's identity evidence is not contradictory (conflicting model claims, several model
  codes, an ambiguous core field);
- no manufacturer code or part number recorded for the target is contradicted by a different
  code on the page (a recorded code is an identity requirement and is never bypassed);
- no material secondary specification the target records (GPU, panel, resolution, ...) is
  contradicted or made ambiguous by the page. A secondary field the target does not record stays
  unknown and never blocks. The exact processor model is one of them (2026-10-22): when the
  target records a full processor number (``Ryzen 7 170``) and the page, or the title when the
  page rows state none, names a different one (``Ryzen 7 7735HS``), the offer is not compared.
  A family-only value on either side is unknown.

Several family targets can fit one offer. Ambiguity rule: targets with a verified secondary
conflict are already excluded; among the rest, the most specific family name stated in the title
wins (``IdeaPad Slim 5`` over ``IdeaPad``). If more than one target remains, the offer is
``LISTED_SPEC_AMBIGUOUS``: it is associated with none of them for pricing and all of them are kept
for review. The target a search was made for is never preferred.

A listed-spec match is never an exact product match: the offer's actual model, processor and
secondary hardware are kept as stated and reported beside the price. Targets with a model code
or a reviewed product name keep the exact five-field identity rules only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import pandas as pd

from .conventions import model_codes, processor_models, product_name_tokens, title_identifiers
from .normalization import CORE_MATCH_COLUMNS, core_value_key, is_blank, text_key
from .product_names import FAMILY_NAME, ProductNameRegistry, default_registry
from .target_master import CONFIGURATION, effective_tracking_mode

LISTED_SPEC_MATCH = "LISTED_SPEC_MATCH"
LISTED_SPEC_AMBIGUOUS = "LISTED_SPEC_AMBIGUOUS"
LISTED_SPEC_BASIS = "LISTED_SPEC"
EXACT_PRODUCT_BASIS = "EXACT_PRODUCT"
LISTED_SPEC_TEXT = ("Listed-spec match — brand, family, screen, CPU series, installed RAM and "
                    "storage agree; specific Shopee model unknown.")
# Nonblocking warning carried by every listed-spec price: the product identity is not specific.
IDENTITY_UNRESOLVED_WARNING = "SPECIFIC_IDENTITY_UNRESOLVED"
CPU_NUMBER_WARNING = "CPU_NUMBER_DIFFERS_FROM_TARGET_RECORD"

# Per-target check outcomes.
QUALIFIES = "QUALIFIES"
NOT_FAMILY_TARGET = "NOT_FAMILY_TARGET"
TARGET_BRAND_MISSING = "TARGET_BRAND_MISSING"
BRAND_DIFFERS = "BRAND_DIFFERS"
BRAND_NOT_STATED = "BRAND_NOT_STATED"
FAMILY_NOT_STATED = "FAMILY_NOT_STATED"
IDENTITY_EVIDENCE_CONFLICT = "IDENTITY_EVIDENCE_CONFLICT"
RECORDED_CODE_CONFLICT = "RECORDED_CODE_CONFLICT"
REQUIRED_SPECS_MISSING = "REQUIRED_SPECS_MISSING"
LISTED_SPECS_DIFFER = "LISTED_SPECS_DIFFER"
SECONDARY_CONFLICT = "SECONDARY_CONFLICT"

REQUIRED_FIELDS: tuple[str, ...] = tuple(name for name in CORE_MATCH_COLUMNS if name != "model")
# Page-level identity contradictions that no fallback may bypass.
_IDENTITY_CONFLICTS = frozenset({
    "MODEL_CLAIM_CONFLICT", "MODEL_URL_CONFLICT", "MODEL_TITLE_CONFLICT",
    "MODEL_IDENTIFIER_CONFLICT", "TITLE_MULTIPLE_MODEL_CODES", "TITLE_MULTIPLE_PRODUCT_NAMES",
    "AMBIGUOUS_MODEL",
})
_DESIGNATION = re.compile(r"(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*[0-9])[A-Za-z0-9-]{5,}")
_FIELD_WORDS = {"screen_size_in": "screen", "cpu_series": "CPU series",
                "ram_capacity_gb": "installed RAM", "storage_capacity_gb": "installed storage"}
_PROCESSOR_NUMBER = (
    re.compile(r"\bRyzen\s+(?:AI\s+)?[3579]\s+([0-9]{3,4}[A-Z]{0,3})\b", re.I),
    re.compile(r"(?<![A-Za-z0-9])i[3579]-([0-9]{4,5}[A-Z]{0,2}[0-9]?)(?![A-Za-z0-9])"),
    re.compile(r"\bCore\s+(?:Ultra\s+)?[3579]\s*-?\s*(?:processor\s+)?([0-9]{3}[A-Z]{1,2})\b",
               re.I),
)


def _text(row: Mapping[str, object], name: str) -> str:
    value = row.get(name)
    return "" if is_blank(value) else str(value).strip()


def material_secondary_fields() -> tuple[str, ...]:
    """The matcher's blocking secondary fields, except those handled as identity here."""
    from .matching import BLOCKING, SECONDARY_SEVERITY

    return tuple(name for name, severity in SECONDARY_SEVERITY.items()
                 if severity == BLOCKING and name not in {"brand", "manufacturer_model_code"})


def processor_numbers(text: str) -> set[str]:
    """Processor numbers stated in ``text`` ("7735HS", "13420H"); reported, never matched."""
    return {match[1].upper() for pattern in _PROCESSOR_NUMBER for match in pattern.finditer(text)}


def _contains(words: Sequence[str], wanted: Sequence[str]) -> bool:
    """``wanted`` as whole words in order, or as one joined word sequence (``VivoBook`` for
    ``Vivo Book`` and the reverse): presentation only, never a prefix or fuzzy comparison."""
    if not wanted:
        return False
    size = len(wanted)
    if any(list(words[i:i + size]) == list(wanted) for i in range(len(words) - size + 1)):
        return True
    joined = "".join(wanted)
    for start in range(len(words)):
        built = ""
        for word in words[start:start + 4]:
            built += word
            if built == joined:
                return True
            if not joined.startswith(built):
                break
    return False


@dataclass(frozen=True)
class ListedSpecCheck:
    """One offer against one target: whether the listed specifications match, and why not."""

    target_id: str
    outcome: str
    detail: str = ""
    # Number of family-name words the title states (the ambiguity rule's specificity).
    specificity: int = 0
    differences: tuple[tuple[str, str, str], ...] = ()
    warnings: tuple[str, ...] = ()
    offer_details: str = ""

    @property
    def qualifies(self) -> bool:
        return self.outcome == QUALIFIES


@dataclass(frozen=True)
class ListedSpecResult:
    """An offer's listed-spec associations across the catalog."""

    status: str  # LISTED_SPEC_MATCH, LISTED_SPEC_AMBIGUOUS, or "" (none)
    target_ids: tuple[str, ...] = ()  # the one associated target (LISTED_SPEC_MATCH)
    ambiguous_ids: tuple[str, ...] = ()  # indistinguishable targets (LISTED_SPEC_AMBIGUOUS)
    # Broader family targets that also fit but lost to a more specific family name.
    broader_ids: tuple[str, ...] = ()
    checks: Mapping[str, ListedSpecCheck] = field(default_factory=dict)

    def detail(self) -> str:
        if self.status == LISTED_SPEC_MATCH:
            check = self.checks[self.target_ids[0]]
            return "; ".join(filter(None, (LISTED_SPEC_TEXT, check.offer_details, (
                "also fits broader family target(s) " + ", ".join(self.broader_ids)
            ) if self.broader_ids else "")))
        if self.status == LISTED_SPEC_AMBIGUOUS:
            return ("listed specifications fit several broad-family targets equally: "
                    + ", ".join(self.ambiguous_ids) + " (none chosen; review)")
        return ""


def offer_details(attributes: Mapping[str, object], title: str) -> str:
    """The offer's actual model, processor and material hardware, as it states them."""
    parts = []
    model = _text(attributes, "model")
    if model:
        parts.append(f"model {model}")
    elif title:
        # No model was extracted: the page's own title is the model statement on record.
        parts.append(f'title "{title}"')
    numbers = sorted(processor_numbers(title))
    cpu = _text(attributes, "cpu_series")
    if cpu or numbers:
        parts.append("CPU " + " ".join(filter(None, (cpu, "/".join(numbers)))))
    for name, label in (("gpu_model", "GPU"), ("display_type", "panel"),
                        ("resolution", "resolution"), ("refresh_rate_hz", "refresh Hz")):
        value = _text(attributes, name)
        if value:
            parts.append(f"{label} {value}")
    return "offer: " + "; ".join(parts) if parts else ""


def is_family_target(
    row: Mapping[str, object], registry: ProductNameRegistry | None = None
) -> bool:
    registry = registry or default_registry()
    return registry.kind(_text(row, "brand"), _text(row, "model")) == FAMILY_NAME


def check_target(
    target: Mapping[str, object],
    attributes: Mapping[str, object],
    title: str,
    reason_codes: Iterable[str] = (),
    *,
    registry: ProductNameRegistry | None = None,
) -> ListedSpecCheck:
    """Whether ``attributes`` (the offer's EXTRACTED values) and ``title`` match the listed
    specifications of one target (see the module docstring)."""
    from .matching import states_exact_processor, verified_secondary_difference

    target_id = _text(target, "target_id")
    codes = set(reason_codes)
    details = offer_details(attributes, title)

    def fail(outcome: str, detail: str, **extra) -> ListedSpecCheck:
        return ListedSpecCheck(target_id, outcome, detail, offer_details=details, **extra)

    if not is_family_target(target, registry):
        return fail(NOT_FAMILY_TARGET, "the target has a model code or a reviewed product name")
    brand = _text(target, "brand")
    if not brand:
        return fail(TARGET_BRAND_MISSING, "the catalog records no brand for this target")
    words = product_name_tokens(title)
    brand_words = product_name_tokens(brand)
    in_title = _contains(words, brand_words)
    stated_brand = _text(attributes, "brand")
    if stated_brand and not _contains(product_name_tokens(stated_brand), brand_words) and (
        not in_title
    ):
        return fail(BRAND_DIFFERS, f"page brand {stated_brand}; target brand {brand}")
    if not stated_brand and not in_title:
        return fail(BRAND_NOT_STATED, f"the page does not state the brand {brand}")
    family = _text(target, "model")
    family_words = product_name_tokens(family)
    if not _contains(words, family_words):
        return fail(FAMILY_NOT_STATED, f"the title does not name the family '{family}'")
    conflicts = sorted(codes & _IDENTITY_CONFLICTS)
    if conflicts:
        return fail(IDENTITY_EVIDENCE_CONFLICT, "conflicting identity evidence: "
                    + ", ".join(conflicts))
    # Identifiers the page states for itself: model codes in the title, and its extracted model
    # when that is one designation token (a code, or a machine type such as "83K700HXPH" that
    # the code pattern does not recognise). A product-name model ("IdeaPad Slim 3") is not one.
    model = _text(attributes, "model")
    stated_codes = {
        code.casefold() for code in (
            *title_identifiers(title).model_codes,
            *([model] if model_codes(model) or _DESIGNATION.fullmatch(model) else []),
        )
    }
    recorded = [_text(target, name) for name in ("manufacturer_model_code",)
                if model_codes(_text(target, name))]
    for code in recorded:
        if stated_codes and code.casefold() not in stated_codes:
            return fail(RECORDED_CODE_CONFLICT,
                        f"the target records code {code}; the page states "
                        + ", ".join(sorted(stated_codes)).upper())
    missing = [name for name in REQUIRED_FIELDS if is_blank(attributes.get(name))]
    if missing:
        return fail(REQUIRED_SPECS_MISSING, "not established by the page: "
                    + ", ".join(_FIELD_WORDS[name] for name in missing))
    differences = []
    for name in REQUIRED_FIELDS:
        offer_value, target_value = _text(attributes, name), _text(target, name)
        try:
            same = core_value_key(name, offer_value) == core_value_key(name, target_value)
        except ValueError:
            same = False
        if not same:
            differences.append((name, offer_value, target_value))
    if differences:
        return fail(LISTED_SPECS_DIFFER, "; ".join(
            f"{name}: offer {offer} vs target {wanted}" for name, offer, wanted in differences),
            differences=tuple(differences))
    # The processor model the page rows state; when they state none, the one model the title
    # names (a direct caller passes the title only). Several title models are not used.
    stated = dict(attributes)
    if not _text(stated, "cpu_model") and len(title_cpu := processor_models(title)) == 1:
        stated["cpu_model"] = title_cpu[0].display
    secondary = []
    for name in material_secondary_fields():
        target_value = _text(target, name)
        if not target_value:
            continue  # unknown for the target: never a block
        unresolved = f"AMBIGUOUS_{name.upper()}" in codes or (
            f"TITLE_SPEC_CONFLICT_{name.upper()}" in codes)
        if name == "cpu_model" and unresolved:
            unresolved = states_exact_processor(target_value)  # else: unknown, not a conflict
        if unresolved or verified_secondary_difference(name, stated.get(name), target_value):
            secondary.append(f"{name}: offer {_text(stated, name) or 'unresolved'} vs "
                             f"target {target_value}")
    if secondary:
        return fail(SECONDARY_CONFLICT, "; ".join(secondary))
    warnings = []
    recorded_numbers = processor_numbers(_text(target, "cpu_model"))
    stated_numbers = processor_numbers(title)
    if recorded_numbers and stated_numbers and not recorded_numbers & stated_numbers:
        warnings.append(CPU_NUMBER_WARNING)
    return ListedSpecCheck(target_id, QUALIFIES, LISTED_SPEC_TEXT,
                           specificity=len(family_words), warnings=tuple(warnings),
                           offer_details=details)


class ListedSpecMatcher:
    """Listed-spec associations against every active broad-family configuration target."""

    def __init__(
        self,
        targets: Iterable[Mapping[str, object]] | pd.DataFrame,
        registry: ProductNameRegistry | None = None,
    ) -> None:
        rows = targets.to_dict(orient="records") if isinstance(targets, pd.DataFrame) else list(
            targets)
        self.registry = registry or default_registry()
        self.targets: dict[str, Mapping[str, object]] = {}
        for row in rows:
            if text_key(row.get("status")) != "active":
                continue
            if effective_tracking_mode(row.get("tracking_mode")) != CONFIGURATION:
                continue
            if is_family_target(row, self.registry) and _text(row, "brand"):
                self.targets[_text(row, "target_id")] = row
        self._by_brand: dict[tuple[str, ...], list[Mapping[str, object]]] = {}
        for row in self.targets.values():
            key = tuple(product_name_tokens(_text(row, "brand")))
            self._by_brand.setdefault(key, []).append(row)

    def check(
        self, target_id: str, attributes: Mapping[str, object], title: str,
        reason_codes: Iterable[str] = (),
    ) -> ListedSpecCheck | None:
        row = self.targets.get(target_id)
        if row is None:
            return None
        return check_target(row, attributes, title, reason_codes, registry=self.registry)

    def associate(
        self, attributes: Mapping[str, object], title: str, reason_codes: Iterable[str] = ()
    ) -> ListedSpecResult:
        """The offer's listed-spec association under the documented ambiguity rule."""
        codes = tuple(reason_codes)
        words = product_name_tokens(title)
        stated = product_name_tokens(_text(attributes, "brand"))
        checks: dict[str, ListedSpecCheck] = {}
        for brand_words, rows in self._by_brand.items():
            if not (_contains(words, list(brand_words)) or _contains(stated, list(brand_words))):
                continue
            for row in rows:
                check = check_target(row, attributes, title, codes, registry=self.registry)
                checks[check.target_id] = check
        qualifying = [check for check in checks.values() if check.qualifies]
        if not qualifying:
            return ListedSpecResult("", checks=checks)
        best = max(check.specificity for check in qualifying)
        top = sorted(check.target_id for check in qualifying if check.specificity == best)
        broader = tuple(sorted(check.target_id for check in qualifying
                               if check.specificity < best))
        if len(top) == 1:
            return ListedSpecResult(LISTED_SPEC_MATCH, (top[0],), (), broader, checks)
        return ListedSpecResult(LISTED_SPEC_AMBIGUOUS, (), tuple(top), broader, checks)
