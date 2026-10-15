"""Offline candidate discovery and search-query planning (research priorities only).

Discovery helps a reviewer find and rank retailer pages that may be a reference configuration,
including pages whose titles are worded differently. It is deliberately separate from matching:

- Discovery may read titles, URLs, page text, and identifiers to find code tokens. Those
  sources never become trusted matching fields, however often they repeat.
- Field agreement uses only the extractor's trusted values (``EXTRACTED``). Supporting
  evidence is shown, labelled as such, and never counts as agreement.
- Categories (strong candidate, needs verification, possible candidate, different
  configuration) are review priorities. Nothing here assigns a target, approves a mapping,
  changes a match result, makes a request, or invents a URL or variant ID.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from .conventions import is_model_code, model_codes
from .extraction import (
    ExtractedAttributes,
    ExtractionStatus,
    _ram,
    _storage,
    extract_offer_attributes,
)
from .normalization import CORE_MATCH_COLUMNS, core_value_key
from .observation_store import observation_id
from .offers import OfferObservation

STRONG = "STRONG_CANDIDATE"
NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
POSSIBLE = "POSSIBLE_CANDIDATE"
DIFFERENT = "DIFFERENT_CONFIGURATION"
CATEGORY_ORDER = {STRONG: 0, NEEDS_VERIFICATION: 1, POSSIBLE: 2, DIFFERENT: 3}

# Where a code token was seen, strongest first. LABELED_MODEL comes from a trusted field;
# TITLE_CORROBORATED is a title code the extractor accepted under the balanced policy
# (2026-10-15) because the other four fields stand on their own.
LABELED_MODEL = "LABELED_MODEL"
TITLE_CORROBORATED = "TITLE_MODEL_CORROBORATED"
LABELED_WITH_SUFFIX = "LABELED_MODEL_WITH_UNEXPLAINED_SUFFIX"
TITLE_ONLY = "TITLE_ONLY"
PAGE_TEXT = "PAGE_TEXT_ONLY"
URL_ONLY = "URL_ONLY"
PROVENANCE_ORDER = (
    LABELED_MODEL, TITLE_CORROBORATED, LABELED_WITH_SUFFIX, TITLE_ONLY, PAGE_TEXT, URL_ONLY
)

COLLECTED = "COLLECTED"
NOT_FETCHED = "DISCOVERED_NOT_FETCHED"

_MARKS = re.compile("[®™℠]")
# Words that commonly follow a code in a title and are not designations.
_NOT_A_SUFFIX = frozenset(
    {"LAPTOP", "NOTEBOOK", "GAMING", "OLED", "IPS", "FHD", "QHD", "UHD", "WUXGA", "WIN",
     "RAM", "SSD", "HDD", "AI", "PC", "BLACK", "BLUE", "GRAY", "GREY", "SILVER", "WHITE"}
)
_SEGMENT_BREAK = re.compile(r"[|/,()\[\]]")

CANDIDATE_COLUMNS: tuple[str, ...] = (
    "target_id",
    "rank",
    "retailer_id",
    "category",
    "candidate_url",
    "variant_id",
    "collection_status",
    "original_title",
    "matched_code",
    "code_provenance",
    "retained_suffix",
    "discovered_identifiers",
    *(f"core_{name}" for name in CORE_MATCH_COLUMNS),
    "agreements",
    "differences",
    "unknowns",
    "extraction_notes",
    "next_verification_step",
    "observation_id",
    "adapter_version",
    "source_file",
)
QUERY_COLUMNS: tuple[str, ...] = (
    "target_id",
    "rank",
    "retailer_id",
    "order",
    "tier",
    "verified_identifier",
    "query",
    "basis",
)


def normalize(text: str) -> str:
    """Discovery comparison form: NFC, trademark marks removed, whitespace collapsed, casefold."""
    text = _MARKS.sub(" ", unicodedata.normalize("NFC", text))
    return re.sub(r"\s+", " ", text).strip().casefold()


@dataclass(frozen=True)
class Reference:
    target_id: str
    rank: str
    brand: str
    series: str
    model: str
    core: Mapping[str, str]
    manufacturer_part_number: str = ""

    @property
    def code(self) -> str:
        return self.model if is_model_code(self.model) else ""


@dataclass
class Candidate:
    """One collected observation, or one variant the page listed but nobody fetched."""

    observation: OfferObservation
    extracted: ExtractedAttributes
    source_file: str
    url: str
    title: str
    collection_status: str = COLLECTED
    option_title: str = ""
    identifiers: list[tuple[str, str]] = field(default_factory=list)

    @property
    def retailer_id(self) -> str:
        return self.observation.retailer_id

    @property
    def variant_id(self) -> str:
        return (parse_qs(urlsplit(self.url).query).get("variant") or [""])[0]


def build_candidates(observations: Iterable[tuple[OfferObservation, str]]) -> list[Candidate]:
    """Candidates from saved observations; a later record of the same observation wins.

    Variants that a page enumerated with its own URL become separate ``DISCOVERED_NOT_FETCHED``
    candidates unless that URL was itself collected.
    """
    latest: dict[str, tuple[OfferObservation, str]] = {}
    for observation, source in observations:
        latest[observation_id(observation)] = (observation, source)
    candidates: list[Candidate] = []
    for observation, source in latest.values():
        extracted = extract_offer_attributes(observation)
        identifiers = [
            (item.value, item.source_label)
            for item in extracted.evidence
            if item.field in {"identifier", "part_number_candidate"}
            and item.value
            and item.value != "(blank)"
        ]
        candidates.append(
            Candidate(observation, extracted, source, observation.offer_url,
                      observation.raw_title, identifiers=identifiers)
        )
    collected_urls = {c.url for c in candidates}
    for parent in list(candidates):
        for item in parent.extracted.evidence:
            if item.field != "enumerated_variant" or item.value in collected_urls:
                continue
            option = item.source_label.split(" [", 1)[0]
            candidates.append(
                Candidate(parent.observation, parent.extracted, parent.source_file, item.value,
                          parent.title, NOT_FETCHED, option, parent.identifiers)
            )
    return candidates


def _suffix_after(text: str, code: str) -> str:
    """An unexplained designation right after ``code`` in the same title segment, if any."""
    for segment in _SEGMENT_BREAK.split(text):
        words = segment.split()
        upper = [word.upper() for word in words]
        if code.upper() in upper:
            index = upper.index(code.upper())
            if index + 1 < len(words):
                nxt = words[index + 1]
                if re.fullmatch(r"[A-Z]{2,5}", nxt) and nxt not in _NOT_A_SUFFIX:
                    return nxt
    return ""


def code_evidence(candidate: Candidate) -> list[tuple[str, str, str]]:
    """``(code, provenance, retained suffix)`` for every code token the candidate shows."""
    found: list[tuple[str, str, str]] = []
    primary = _primary(candidate.extracted, "model")
    if primary is not None and primary.status is ExtractionStatus.EXTRACTED:
        provenance = (
            TITLE_CORROBORATED if primary.source_label.startswith(f"title [{TITLE_CORROBORATED}")
            else LABELED_MODEL
        )
        for code in model_codes(primary.value) or [primary.value]:
            found.append((code, provenance, ""))
    elif primary is not None and primary.status is ExtractionStatus.UNRESOLVED:
        for code in model_codes(primary.value):
            remainder = primary.value.replace(code, " ", 1).strip()
            found.append((code, LABELED_WITH_SUFFIX, remainder))
    for code in model_codes(candidate.title):
        found.append((code, TITLE_ONLY, _suffix_after(candidate.title, code)))
    for code in model_codes(candidate.observation.raw_configuration_text):
        found.append((code, PAGE_TEXT, ""))
    return found


def _primary(extracted: ExtractedAttributes, name: str):
    return next(
        (e for e in extracted.evidence
         if e.field == name and e.status is not ExtractionStatus.SUPPORTING),
        None,
    )


def _supporting(extracted: ExtractedAttributes, name: str) -> list[str]:
    return [
        e.value for e in extracted.evidence
        if e.field == name and e.status is ExtractionStatus.SUPPORTING and e.value
    ]


def _same(name: str, left: str, right: str) -> bool:
    try:
        return core_value_key(name, left) == core_value_key(name, right)
    except ValueError:
        return False


def _option_values(option_title: str) -> dict[str, str]:
    """RAM/storage stated by an enumerated variant's own option title (not collected)."""
    values: dict[str, str] = {}
    for part in option_title.split(" / "):
        words = part.casefold()
        try:
            if "ram" in words or "memory" in words:
                found = _ram(part)
                name = "ram_capacity_gb"
            elif any(w in words for w in ("ssd", "sdd", "hdd", "storage", "emmc")):
                found = _storage(part)
                name = "storage_capacity_gb"
            else:
                continue
        except ValueError:
            continue
        if len(found) == 1:
            values[name] = next(iter(found))
    return values


@dataclass(frozen=True)
class Assessment:
    reference: Reference
    candidate: Candidate
    category: str
    matched_code: str
    provenance: str
    suffix: str
    fields: Mapping[str, str]
    next_step: str


def assess(reference: Reference, candidate: Candidate) -> Assessment | None:
    """Rank one candidate against one reference; ``None`` when nothing links them."""
    codes = code_evidence(candidate)
    matched = [
        (code, provenance, suffix)
        for code, provenance, suffix in codes
        if reference.code and normalize(code) == normalize(reference.code)
    ]
    slug = normalize(urlsplit(candidate.url).path)
    if reference.code and not matched and re.search(
        rf"(?:^|[/-]){re.escape(normalize(reference.code))}(?:$|[/-])", slug
    ):
        matched.append((reference.code, URL_ONLY, ""))
    matched.sort(key=lambda m: PROVENANCE_ORDER.index(m[1]))
    code, provenance, suffix = matched[0] if matched else ("", "", "")
    suffix = suffix or next((s for _, _, s in matched if s), "")
    title = normalize(candidate.title)
    family = bool(reference.brand) and normalize(reference.brand) in title and (
        not reference.series or normalize(reference.series) in title
    )

    option = _option_values(candidate.option_title) if candidate.option_title else {}
    fields: dict[str, str] = {}
    for name in CORE_MATCH_COLUMNS:
        wanted = reference.core[name]
        if candidate.collection_status == NOT_FETCHED:
            if name in option:
                fields[name] = (
                    "AGREE (variant option)" if _same(name, option[name], wanted)
                    else f"DIFFER (variant option {option[name]})"
                )
            else:
                fields[name] = "UNKNOWN (variant not collected)"
            continue
        primary = _primary(candidate.extracted, name)
        if primary is not None and primary.status is ExtractionStatus.EXTRACTED:
            fields[name] = (
                "AGREE" if _same(name, primary.value, wanted)
                else f"DIFFER ({primary.value})"
            )
            continue
        status = primary.status.value if primary is not None else "MISSING"
        support = _supporting(candidate.extracted, name)
        if support:
            agrees = any(_same(name, value, wanted) for value in support)
            fields[name] = (
                f"UNKNOWN ({status}); supporting only: "
                + ("agrees" if agrees else "differs " + "; ".join(support))
            )
        elif primary is not None and primary.status is ExtractionStatus.UNRESOLVED:
            fields[name] = f"UNKNOWN (UNRESOLVED: {primary.value})"
        else:
            fields[name] = f"UNKNOWN ({status})"

    differ = [name for name, value in fields.items() if value.startswith("DIFFER")]
    if differ and matched:
        category = DIFFERENT
    elif differ:
        # Family words alone do not make a page with different specifications a lead.
        return None
    elif provenance in {LABELED_MODEL, TITLE_CORROBORATED} and (
        candidate.collection_status == COLLECTED
    ):
        category = STRONG
    elif matched:
        category = NEEDS_VERIFICATION
    elif family:
        category = POSSIBLE
    else:
        return None
    return Assessment(
        reference, candidate, category, code, provenance, suffix, fields,
        _next_step(category, provenance, suffix, fields, candidate),
    )


def _next_step(
    category: str,
    provenance: str,
    suffix: str,
    fields: Mapping[str, str],
    candidate: Candidate,
) -> str:
    notes = set(candidate.extracted.notes)
    steps: list[str] = []
    if category == DIFFERENT:
        steps.append("Different configuration: keep as a separate variant or product; never merge.")
    if candidate.collection_status == NOT_FETCHED:
        steps.append("Collect this variant's own page under supervision before any review.")
    if suffix:
        steps.append(f"Verify the meaning of '{suffix}' after the model code; no alias assumed.")
    if provenance in {TITLE_ONLY, PAGE_TEXT, URL_ONLY} and category != DIFFERENT:
        steps.append("Obtain a labeled or manufacturer model field; the code is only in "
                     f"{provenance.replace('_', ' ').lower()}.")
    unknown = [name for name, value in fields.items() if value.startswith("UNKNOWN")]
    if "MULTI_VARIANT_SHARED_DESCRIPTION" in notes:
        shared = [n for n in unknown if "NOT_EXTRACTED" in fields[n]]
        if shared:
            steps.append("Verify " + ", ".join(shared) + " for the selected variant or obtain "
                         "an owner decision on shared-field inheritance.")
    if "STORAGE_CAPACITY_UNIT_MISSING" in notes:
        steps.append("Obtain an explicit storage capacity unit.")
    if any("supporting only" in fields[n] for n in unknown if n == "screen_size_in"):
        steps.append("Screen size is only in description prose: owner decision on that "
                     "source, or labeled unit evidence.")
    if candidate.observation.details and candidate.observation.details.bundle_flag:
        steps.append("Bundle offer: review price comparability separately.")
    if category == STRONG:
        rest = [n for n in unknown]
        steps.insert(0, "Discovery priority only; confirm through the five-field matcher"
                     + (f" once {', '.join(rest)} have trusted evidence." if rest else "."))
    if not steps:
        steps.append("Review the remaining unknown fields with trusted evidence.")
    return " ".join(steps)


def assessment_row(item: Assessment) -> dict[str, str]:
    candidate = item.candidate
    reference = item.reference
    fields = item.fields
    identifiers = "; ".join(
        f"{code} ({provenance}{', suffix ' + suffix if suffix else ''})"
        for code, provenance, suffix in dict.fromkeys(code_evidence(candidate))
    )
    extra = "; ".join(f"{value} ({label})" for value, label in dict.fromkeys(candidate.identifiers))
    row = {
        "target_id": reference.target_id,
        "rank": reference.rank,
        "retailer_id": candidate.retailer_id,
        "category": item.category,
        "candidate_url": candidate.url,
        "variant_id": candidate.variant_id,
        "collection_status": candidate.collection_status,
        "original_title": candidate.title
        + (f" — option: {candidate.option_title}" if candidate.option_title else ""),
        "matched_code": item.matched_code,
        "code_provenance": item.provenance or "NONE",
        "retained_suffix": item.suffix,
        "discovered_identifiers": "; ".join(filter(None, [identifiers, extra])),
        **{f"core_{name}": fields[name] for name in CORE_MATCH_COLUMNS},
        "agreements": ";".join(n for n, v in fields.items() if v.startswith("AGREE")),
        "differences": ";".join(n for n, v in fields.items() if v.startswith("DIFFER")),
        "unknowns": ";".join(n for n, v in fields.items() if v.startswith("UNKNOWN")),
        "extraction_notes": ";".join(candidate.extracted.notes),
        "next_verification_step": item.next_step,
        "observation_id": observation_id(candidate.observation)
        if candidate.collection_status == COLLECTED
        else "",
        "adapter_version": candidate.observation.source.adapter_version,
        "source_file": candidate.source_file,
    }
    return {column: row[column] for column in CANDIDATE_COLUMNS}


def rank_candidates(
    references: Sequence[Reference], candidates: Sequence[Candidate]
) -> list[Assessment]:
    found = [
        item
        for reference in references
        for candidate in candidates
        if (item := assess(reference, candidate)) is not None
    ]
    rank = {reference.target_id: index for index, reference in enumerate(references)}
    found.sort(
        key=lambda a: (
            rank[a.reference.target_id],
            CATEGORY_ORDER[a.category],
            PROVENANCE_ORDER.index(a.provenance) if a.provenance else len(PROVENANCE_ORDER),
            -sum(v.startswith("AGREE") for v in a.fields.values()),
            a.candidate.retailer_id,
            a.candidate.url,
        )
    )
    return found


def _storage_words(value: str) -> str:
    """Retailer wording for a storage capacity: whole terabytes as TB, otherwise GB."""
    if value.isdigit() and int(value) >= 1000 and int(value) % 1000 == 0:
        return f"{int(value) // 1000}TB"
    return f"{value}GB"


# Title tokens this long (letters and digits only) are treated as possible part numbers.
_PART_NUMBER_LENGTH = 12


def _compact(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", normalize(text))


def _spelled_forms(candidate: Candidate, compact: str) -> list[str]:
    """Title/model spellings of the same code with different spacing or separators."""
    forms: list[str] = []
    for text in (candidate.title, candidate.observation.raw_configuration_text):
        for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{3,40}[A-Za-z0-9]", text):
            words = match.group(0).split()
            for start in range(len(words)):
                for end in range(start + 1, min(len(words), start + 5) + 1):
                    token = " ".join(words[start:end])
                    if _compact(token) == compact:
                        forms.append(token)
    return forms


def _retailer_ids_for_sku(candidates: Sequence[Candidate]) -> set[str]:
    """Identifier values that are retailer SKUs, barcodes, or product/variant IDs."""
    excluded: set[str] = set()
    for candidate in candidates:
        for value, label in candidate.identifiers:
            text = label.casefold()
            if any(w in text for w in ("sku", "gtin", "product-id", "productid", "variant")):
                excluded.add(normalize(value))
        details = candidate.observation.details
        if details:
            for value in (details.retailer_sku, details.retailer_product_id):
                if value:
                    excluded.add(normalize(value))
    return excluded


def plan_queries(
    references: Sequence[Reference],
    assessments: Sequence[Assessment],
    hosts: Mapping[str, str],
) -> list[dict[str, str]]:
    """Ordered search queries per reference and retailer host (not executed here).

    Tiers: A complete code; B formatting variations observed in saved evidence; C a verified
    manufacturer part number, or supplementary unverified identifiers (clearly marked);
    D brand + family + distinguishing specifications.
    """
    rows: list[dict[str, str]] = []
    candidates = [a.candidate for a in assessments]
    not_part_numbers = _retailer_ids_for_sku(candidates)
    for reference in references:
        linked = [a for a in assessments if a.reference is reference and a.matched_code]
        code = reference.code or reference.model
        compact = _compact(code)
        seen = {normalize(code)}
        variations: list[tuple[str, str, bool]] = []
        for item in linked:
            # Search is case-insensitive, so only spacing/separator forms are new queries.
            for token in _spelled_forms(item.candidate, compact):
                if normalize(token) not in seen:
                    seen.add(normalize(token))
                    variations.append(
                        (token, f"spelling seen on a saved {item.candidate.retailer_id} page", True)
                    )
            designation = f"{code} {item.suffix}" if item.suffix else ""
            if designation and normalize(designation) not in seen:
                seen.add(normalize(designation))
                variations.append(
                    (designation,
                     "observed designation with an unexplained suffix; not an alias", False)
                )
        supplementary: list[tuple[str, str]] = []
        for item in linked:
            for value, label in item.candidate.identifiers:
                lowered = label.casefold()
                if ("part number" in lowered or "mpn" in lowered) and (
                    normalize(value) not in not_part_numbers
                ):
                    supplementary.append((value, label))
            for token in model_codes(item.candidate.title):
                if (
                    _compact(token) != compact
                    and len(_compact(token)) >= _PART_NUMBER_LENGTH
                    and normalize(token) not in not_part_numbers
                ):
                    supplementary.append(
                        (token, "part-number-length token in the retailer title; role unverified")
                    )
        core = reference.core
        specs = " ".join(
            part
            for part in (
                reference.brand,
                f'"{reference.series}"' if reference.series else "",
                core["screen_size_in"],
                f'"{core["cpu_series"]}"',
                f'{core["ram_capacity_gb"]}GB',
                _storage_words(core["storage_capacity_gb"]),
            )
            if part
        )
        for retailer_id, host in hosts.items():
            plan: list[tuple[str, str, bool, str]] = [
                ("A_COMPLETE_MODEL_CODE", f'site:{host} "{code}"', True,
                 "complete model code from TARGET_MASTER"),
            ]
            for text, basis, verified in dict.fromkeys(variations):
                plan.append(("B_FORMATTING_VARIATION", f'site:{host} "{text}"', verified, basis))
            if reference.manufacturer_part_number:
                plan.append(("C_VERIFIED_MANUFACTURER_PART_NUMBER",
                             f'site:{host} "{reference.manufacturer_part_number}"', True,
                             "manufacturer_part_number in TARGET_MASTER"))
            for value, basis in dict.fromkeys(supplementary):
                plan.append(("C_SUPPLEMENTARY_UNVERIFIED", f'site:{host} "{value}"', False,
                             f"unverified identifier: {basis}"))
            plan.append(("D_BRAND_FAMILY_SPECS", f"site:{host} {specs}", True,
                         "brand, family and reference core specifications (search hint only)"))
            for order, (tier, query, verified, basis) in enumerate(plan, start=1):
                rows.append({
                    "target_id": reference.target_id,
                    "rank": reference.rank,
                    "retailer_id": retailer_id,
                    "order": str(order),
                    "tier": tier,
                    "verified_identifier": "TRUE" if verified else "FALSE",
                    "query": query,
                    "basis": basis,
                })
    return rows
