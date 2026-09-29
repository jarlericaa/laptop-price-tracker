"""Retailer-neutral offer observation model and deterministic normalization (overall Phase 6).

An observation records what one retailer page showed at one moment: raw product text, a price,
and availability evidence, plus where, when, and how it was retrieved. It deliberately has no
``target_id``: deciding whether an offer is a tracked configuration is later exact-matching work.
Uses no network or Google access.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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


class Availability(StrEnum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    PREORDER = "PREORDER"
    UNKNOWN = "UNKNOWN"


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
