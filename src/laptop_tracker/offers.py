"""Retailer-neutral offer observation model and deterministic normalization (overall Phase 6).

An observation records what one retailer page showed at one moment: raw product text, a price,
and availability evidence, plus where, when, and how it was retrieved. It deliberately has no
``target_id``: deciding whether an offer is a tracked configuration is later exact-matching work.
Uses no network or Google access.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

RETAILER_ID_PATTERN = r"[a-z][a-z0-9_]{1,39}"
CURRENCY_PATTERN = r"[A-Z]{3}"
SHA256_PATTERN = r"[0-9a-f]{64}"

# Column order for flat observation records (for example a future CSV or sheet output).
OBSERVATION_COLUMNS: tuple[str, ...] = (
    "retailer_id",
    "offer_url",
    "retrieved_at",
    "raw_title",
    "raw_configuration_text",
    "price",
    "currency",
    "raw_price_text",
    "availability",
    "availability_evidence",
    "offer_index",
    "requested_url",
    "final_url",
    "http_status",
    "attempts",
    "content_sha256",
    "content_type",
    "adapter_version",
)

# Optional retailer-supplied details, appended after OBSERVATION_COLUMNS by to_full_record().
OFFER_DETAIL_COLUMNS: tuple[str, ...] = (
    "retailer_product_id",
    "retailer_sku",
    "brand",
    "manufacturer_part_number",
    "price_type",
    "regular_price",
    "regular_price_text",
    "sale_price",
    "sale_price_text",
    "raw_specifications",
    "raw_categories",
    "bundle_flag",
    "bundle_evidence",
    "scoped_evidence",
)
# Detail columns added after the first stored observations. A blank value is omitted from
# canonical record hashes, so earlier records keep their identity and fingerprint.
EXTENSION_DETAIL_COLUMNS: tuple[str, ...] = ("scoped_evidence",)

# Where a scoped evidence item came from. Scope says what the text applies to; it never makes
# the text a trusted specification by itself.
SELECTED_VARIANT = "SELECTED_VARIANT"  # Option values of the variant the page verifiably selected.
ENUMERATED_VARIANT = "ENUMERATED_VARIANT"  # Another variant listed by the page; not collected.
PRODUCT_DESCRIPTION = "PRODUCT_DESCRIPTION"  # Marketing/description prose (not a spec row).
VARIANT_DEFINITIONS = "VARIANT_DEFINITIONS"  # Complete product-picker option matrix and locator.
IDENTIFIER = "IDENTIFIER"  # A product, SKU, barcode, or part identifier and where it appeared.
# A purchase-block statement the page makes (2026-10-22): "<KIND> (<PRICE|PAGE>)" and its wording,
# for example cash-payment-only promo prices. Never a specification and never identity evidence.
PURCHASE_TERMS = "PURCHASE_TERMS"
EVIDENCE_SCOPES = frozenset({
    SELECTED_VARIANT, ENUMERATED_VARIANT, PRODUCT_DESCRIPTION, IDENTIFIER, VARIANT_DEFINITIONS,
    PURCHASE_TERMS,
})


class Availability(StrEnum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    PREORDER = "PREORDER"
    UNKNOWN = "UNKNOWN"


class PriceType(StrEnum):
    """Which displayed price the observation's ``price`` is."""

    REGULAR = "REGULAR"  # No sale shown; the regular price is the current price.
    SALE = "SALE"  # A sale price is shown alongside a higher regular price.


class ObservationValidationError(ValueError):
    """An offer could not be normalized into a valid observation. Never retried.

    Messages name the field and the rule, never the offending page value.
    """

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(f"{field_name}: {message}")
        self.field_name = field_name


def clean_text(value: str) -> str:
    """NFC-normalize and collapse whitespace. Case, wording, and punctuation are kept as shown."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def is_http_url(value: str) -> bool:
    """Absolute HTTP(S) URL without embedded ``user:password@`` credentials."""
    if not value or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def url_host(value: str) -> str:
    """Lowercased host name, or an empty string when the URL has none."""
    try:
        return (urlsplit(value).hostname or "").lower()
    except ValueError:
        return ""


def redact_url(value: str) -> str:
    """Scheme, host, port, and path only, for logs and failures.

    Drops user credentials, query strings, and fragments, which can carry session tokens.
    """
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.port is not None:
            host = f"{host}:{parts.port}"
    except ValueError:
        return "<invalid-url>"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


_NUMBER_TOKEN = re.compile(r"[0-9](?:[0-9.,  ]*[0-9])?")
_NARROW_GROUP_SPACES = "  "


def parse_price(text: str, *, decimal_separator: str = ".") -> Decimal:
    """Parse one displayed price into an exact ``Decimal``; currency symbols and codes are ignored.

    Exactly one number must appear, so ranges and text with several prices are rejected rather
    than guessed. The other of ``.``/``,`` (or a no-break space) is accepted only as standard
    three-digit grouping. Negative and zero prices are rejected.
    """
    if decimal_separator not in {".", ","}:
        raise ValueError("decimal_separator must be '.' or ','.")
    tokens = _NUMBER_TOKEN.findall(text)
    if len(tokens) != 1:
        raise ObservationValidationError(
            "price", f"expected exactly one number, found {len(tokens)}"
        )
    if re.search(r"-\s*[0-9]", text):
        raise ObservationValidationError("price", "must not be negative")
    integer, separator, fraction = tokens[0].partition(decimal_separator)
    if separator and not re.fullmatch(r"[0-9]+", fraction):
        raise ObservationValidationError("price", "has an invalid decimal part")
    group = re.escape(("," if decimal_separator == "." else ".") + _NARROW_GROUP_SPACES)
    if not re.fullmatch(rf"[0-9]+|[0-9]{{1,3}}(?:[{group}][0-9]{{3}})+", integer):
        raise ObservationValidationError("price", "has invalid digit grouping")
    digits = re.sub(r"[^0-9]", "", integer)
    price = Decimal(f"{digits}.{fraction}" if separator else digits)
    if price <= 0:
        raise ObservationValidationError("price", "must be greater than zero")
    return price


@dataclass(frozen=True)
class RetrievalMetadata:
    """Where and how the page behind an observation was retrieved.

    ``content_sha256`` identifies the exact response body without storing or logging it.
    """

    requested_url: str
    final_url: str
    http_status: int
    attempts: int
    content_sha256: str
    content_type: str
    adapter_version: str


@dataclass(frozen=True)
class OfferDetails:
    """Optional evidence an adapter can attach to an offer. Every field may be left empty.

    Values are retailer text as shown; nothing here is a verified laptop specification or a
    target match. Text is passed through ``clean_text`` on construction, and blank list entries
    are dropped. ``bundle_flag`` is ``None`` when the adapter does not check for bundles.
    """

    retailer_product_id: str = ""
    retailer_sku: str = ""
    brand: str = ""
    manufacturer_part_number: str = ""
    price_type: PriceType | None = None
    regular_price: Decimal | None = None
    regular_price_text: str = ""
    sale_price: Decimal | None = None
    sale_price_text: str = ""
    raw_specifications: tuple[tuple[str, str], ...] = ()
    raw_categories: tuple[str, ...] = ()
    bundle_flag: bool | None = None
    bundle_evidence: tuple[str, ...] = ()
    # (scope, label, value) items, for example ("SELECTED_VARIANT", "RAM", "16GB (2x8) DDR4 RAM").
    scoped_evidence: tuple[tuple[str, str, str], ...] = ()

    _TEXT_FIELDS = (
        "retailer_product_id",
        "retailer_sku",
        "brand",
        "manufacturer_part_number",
        "regular_price_text",
        "sale_price_text",
    )

    def __post_init__(self) -> None:
        for name in self._TEXT_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ObservationValidationError(name, "must be text")
            object.__setattr__(self, name, clean_text(value))
        if not _is_text_pairs(self.raw_specifications):
            raise ObservationValidationError("raw_specifications", "must be (label, value) pairs")
        pairs = ((clean_text(label), clean_text(value)) for label, value in self.raw_specifications)
        object.__setattr__(self, "raw_specifications", tuple(p for p in pairs if p[0] or p[1]))
        for name in ("raw_categories", "bundle_evidence"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(isinstance(v, str) for v in values):
                raise ObservationValidationError(name, "must be a tuple of text")
            cleaned = (clean_text(value) for value in values)
            object.__setattr__(self, name, tuple(text for text in cleaned if text))
        if self.price_type is not None and not isinstance(self.price_type, PriceType):
            raise ObservationValidationError("price_type", "must be a PriceType value")
        for name in ("regular_price", "sale_price"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ObservationValidationError(name, "must be a finite positive Decimal")
            if not getattr(self, f"{name}_text"):
                raise ObservationValidationError(f"{name}_text", "is required with the price")
        if self.bundle_flag is not None and not isinstance(self.bundle_flag, bool):
            raise ObservationValidationError("bundle_flag", "must be True, False, or None")
        if self.bundle_evidence and self.bundle_flag is not True:
            raise ObservationValidationError("bundle_evidence", "requires bundle_flag=True")
        if self.bundle_flag is True and not self.bundle_evidence:
            raise ObservationValidationError("bundle_evidence", "is required when flagged")
        scoped = self.scoped_evidence
        if not isinstance(scoped, tuple) or not all(
            isinstance(item, tuple) and len(item) == 3 and all(isinstance(p, str) for p in item)
            for item in scoped
        ):
            raise ObservationValidationError("scoped_evidence", "must be (scope, label, value)")
        cleaned_items = tuple(
            (scope, clean_text(label), clean_text(value)) for scope, label, value in scoped
        )
        if any(scope not in EVIDENCE_SCOPES for scope, _, _ in cleaned_items):
            raise ObservationValidationError("scoped_evidence", "has an unknown scope")
        object.__setattr__(
            self,
            "scoped_evidence",
            tuple(item for item in cleaned_items if item[1] or item[2]),
        )

    def check_against_price(self, price: Decimal) -> None:
        """The detail prices must agree with the observation's current ``price``."""
        if self.price_type is PriceType.SALE:
            if self.sale_price != price:
                raise ObservationValidationError("sale_price", "must equal price for a SALE")
            if self.regular_price is None or self.regular_price <= price:
                raise ObservationValidationError(
                    "regular_price", "must be above the sale price for a SALE"
                )
        elif self.price_type is PriceType.REGULAR:
            if self.sale_price is not None:
                raise ObservationValidationError("sale_price", "must be empty for REGULAR")
            if self.regular_price not in (None, price):
                raise ObservationValidationError("regular_price", "must equal price for REGULAR")
        elif self.sale_price is not None:
            raise ObservationValidationError("price_type", "is required with a sale_price")

    def to_record(self) -> dict[str, str]:
        """Flat string record in ``OFFER_DETAIL_COLUMNS`` order; lists are compact JSON."""
        values = {
            **{name: getattr(self, name) for name in self._TEXT_FIELDS},
            "price_type": "" if self.price_type is None else self.price_type.value,
            "regular_price": "" if self.regular_price is None else str(self.regular_price),
            "sale_price": "" if self.sale_price is None else str(self.sale_price),
            "raw_specifications": _json_list([list(pair) for pair in self.raw_specifications]),
            "raw_categories": _json_list(list(self.raw_categories)),
            "bundle_flag": {None: "", True: "TRUE", False: "FALSE"}[self.bundle_flag],
            "bundle_evidence": _json_list(list(self.bundle_evidence)),
            "scoped_evidence": _json_list([list(item) for item in self.scoped_evidence]),
        }
        return {column: values[column] for column in OFFER_DETAIL_COLUMNS}


def _is_text_pairs(value: object) -> bool:
    return isinstance(value, tuple) and all(
        isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(part, str) for part in pair)
        for pair in value
    )


def _json_list(values: list[object]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":")) if values else ""


@dataclass(frozen=True)
class OfferObservation:
    """One attributable, normalized retailer offer. Construction validates every invariant."""

    retailer_id: str
    offer_url: str
    retrieved_at: datetime
    raw_title: str
    raw_configuration_text: str
    price: Decimal
    currency: str
    raw_price_text: str
    availability: Availability
    availability_evidence: str
    offer_index: int
    source: RetrievalMetadata
    details: OfferDetails | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(RETAILER_ID_PATTERN, self.retailer_id):
            raise ObservationValidationError(
                "retailer_id", "must be lowercase letters, digits, or underscores"
            )
        for name in ("offer_url", "requested_url", "final_url"):
            value = self.offer_url if name == "offer_url" else getattr(self.source, name)
            if not is_http_url(value):
                raise ObservationValidationError(name, "must be an absolute HTTP or HTTPS URL")
        if not isinstance(self.retrieved_at, datetime) or self.retrieved_at.utcoffset() != (
            timedelta(0)
        ):
            raise ObservationValidationError("retrieved_at", "must be a UTC datetime")
        if not isinstance(self.price, Decimal) or not self.price.is_finite() or self.price <= 0:
            raise ObservationValidationError("price", "must be a finite positive Decimal")
        if not isinstance(self.currency, str) or not re.fullmatch(
            CURRENCY_PATTERN, self.currency
        ):
            raise ObservationValidationError("currency", "must be a three-letter ISO 4217 code")
        if not isinstance(self.availability, Availability):
            raise ObservationValidationError("availability", "must be an Availability value")
        for name in (
            "raw_title",
            "raw_configuration_text",
            "raw_price_text",
            "availability_evidence",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or value != clean_text(value):
                raise ObservationValidationError(name, "must be whitespace-normalized text")
        if not self.raw_title:
            raise ObservationValidationError("raw_title", "is required")
        if not self.raw_price_text:
            raise ObservationValidationError("raw_price_text", "is required")
        if self.availability is not Availability.UNKNOWN and not self.availability_evidence:
            raise ObservationValidationError(
                "availability_evidence", "is required unless availability is UNKNOWN"
            )
        if self.offer_index < 0:
            raise ObservationValidationError("offer_index", "must not be negative")
        if self.source.attempts < 1:
            raise ObservationValidationError("attempts", "must be at least 1")
        if not re.fullmatch(SHA256_PATTERN, self.source.content_sha256):
            raise ObservationValidationError("content_sha256", "must be a SHA-256 hex digest")
        if not self.source.adapter_version:
            raise ObservationValidationError("adapter_version", "is required")
        if self.details is not None:
            if not isinstance(self.details, OfferDetails):
                raise ObservationValidationError("details", "must be OfferDetails or None")
            self.details.check_against_price(self.price)

    def to_record(self) -> dict[str, str]:
        """Flat string record in ``OBSERVATION_COLUMNS`` order; identical input, identical output.

        Prices keep their exact decimal text, and times use ISO 8601 with a ``Z`` suffix.
        """
        values = {
            "retailer_id": self.retailer_id,
            "offer_url": self.offer_url,
            "retrieved_at": self.retrieved_at.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "raw_title": self.raw_title,
            "raw_configuration_text": self.raw_configuration_text,
            "price": str(self.price),
            "currency": self.currency,
            "raw_price_text": self.raw_price_text,
            "availability": self.availability.value,
            "availability_evidence": self.availability_evidence,
            "offer_index": str(self.offer_index),
            "requested_url": self.source.requested_url,
            "final_url": self.source.final_url,
            "http_status": str(self.source.http_status),
            "attempts": str(self.source.attempts),
            "content_sha256": self.source.content_sha256,
            "content_type": self.source.content_type,
            "adapter_version": self.source.adapter_version,
        }
        return {column: values[column] for column in OBSERVATION_COLUMNS}

    def to_full_record(self) -> dict[str, str]:
        """``to_record()`` followed by ``OFFER_DETAIL_COLUMNS``; blank when details are absent."""
        details = (
            self.details.to_record()
            if self.details is not None
            else dict.fromkeys(OFFER_DETAIL_COLUMNS, "")
        )
        return self.to_record() | details


def observation_from_record(record: Mapping[str, str]) -> OfferObservation:
    """Rebuild an observation from ``to_record()``/``to_full_record()`` strings (e.g. a CSV row).

    Every ``OBSERVATION_COLUMNS`` key is required; detail columns are optional, and details are
    ``None`` when all of them are blank. The rebuilt observation is validated exactly like a new
    one. Errors name the field, never the cell value.
    """
    missing = [column for column in OBSERVATION_COLUMNS if column not in record]
    if missing:
        raise ObservationValidationError(missing[0], "column is missing")

    def text(name: str) -> str:
        value = record.get(name)
        return "" if value is None else str(value)

    def decimal(name: str) -> Decimal | None:
        value = text(name)
        if value == "":
            return None
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise ObservationValidationError(name, "must be a decimal number") from error

    def integer(name: str) -> int:
        value = text(name)
        if not re.fullmatch(r"-?[0-9]+", value):
            raise ObservationValidationError(name, "must be an integer")
        return int(value)

    def enum(name: str, kind: type[StrEnum]):
        try:
            return kind(text(name))
        except ValueError as error:
            raise ObservationValidationError(name, "is not a recognized value") from error

    def json_texts(name: str) -> list[object]:
        value = text(name)
        if value == "":
            return []
        try:
            items = json.loads(value)
        except json.JSONDecodeError as error:
            raise ObservationValidationError(name, "must be a JSON list") from error
        if not isinstance(items, list):
            raise ObservationValidationError(name, "must be a JSON list")
        return items

    try:
        retrieved_at = datetime.fromisoformat(text("retrieved_at"))
    except ValueError as error:
        raise ObservationValidationError("retrieved_at", "must be an ISO 8601 time") from error

    details = None
    if any(text(column) for column in OFFER_DETAIL_COLUMNS):
        specs = json_texts("raw_specifications")
        if not all(
            isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair)
            for pair in specs
        ):
            raise ObservationValidationError("raw_specifications", "must be (label, value) pairs")
        lists = {name: json_texts(name) for name in ("raw_categories", "bundle_evidence")}
        for name, items in lists.items():
            if not all(isinstance(item, str) for item in items):
                raise ObservationValidationError(name, "must be a list of text")
        bundle = text("bundle_flag")
        if bundle not in {"", "TRUE", "FALSE"}:
            raise ObservationValidationError("bundle_flag", "must be TRUE, FALSE, or blank")
        scoped = json_texts("scoped_evidence")
        if not all(
            isinstance(item, list) and len(item) == 3 and all(isinstance(p, str) for p in item)
            for item in scoped
        ):
            raise ObservationValidationError("scoped_evidence", "must be (scope, label, value)")
        details = OfferDetails(
            retailer_product_id=text("retailer_product_id"),
            retailer_sku=text("retailer_sku"),
            brand=text("brand"),
            manufacturer_part_number=text("manufacturer_part_number"),
            price_type=enum("price_type", PriceType) if text("price_type") else None,
            regular_price=decimal("regular_price"),
            regular_price_text=text("regular_price_text"),
            sale_price=decimal("sale_price"),
            sale_price_text=text("sale_price_text"),
            raw_specifications=tuple((label, value) for label, value in specs),
            raw_categories=tuple(lists["raw_categories"]),  # type: ignore[arg-type]
            bundle_flag={"": None, "TRUE": True, "FALSE": False}[bundle],
            bundle_evidence=tuple(lists["bundle_evidence"]),  # type: ignore[arg-type]
            scoped_evidence=tuple(tuple(item) for item in scoped),  # type: ignore[misc]
        )

    price = decimal("price")
    if price is None:
        raise ObservationValidationError("price", "is required")
    return OfferObservation(
        retailer_id=text("retailer_id"),
        offer_url=text("offer_url"),
        retrieved_at=retrieved_at,
        raw_title=text("raw_title"),
        raw_configuration_text=text("raw_configuration_text"),
        price=price,
        currency=text("currency"),
        raw_price_text=text("raw_price_text"),
        availability=enum("availability", Availability),
        availability_evidence=text("availability_evidence"),
        offer_index=integer("offer_index"),
        source=RetrievalMetadata(
            requested_url=text("requested_url"),
            final_url=text("final_url"),
            http_status=integer("http_status"),
            attempts=integer("attempts"),
            content_sha256=text("content_sha256"),
            content_type=text("content_type"),
            adapter_version=text("adapter_version"),
        ),
        details=details,
    )
