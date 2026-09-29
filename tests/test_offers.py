from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from laptop_tracker.offers import (
    OBSERVATION_COLUMNS,
    Availability,
    ObservationValidationError,
    OfferObservation,
    RetrievalMetadata,
    clean_text,
    parse_price,
    redact_url,
)

# Synthetic values only; no URL, product, or price here is real.
RETRIEVED_AT = datetime(2026, 9, 29, 4, 0, tzinfo=UTC)
SOURCE = RetrievalMetadata(
    requested_url="https://shop.example.test/laptops",
    final_url="https://shop.example.test/laptops",
    http_status=200,
    attempts=1,
    content_sha256="0" * 64,
    content_type="text/html; charset=utf-8",
    adapter_version="example-1",
)


def observation(**overrides: object) -> OfferObservation:
    values: dict[str, object] = {
        "retailer_id": "example_shop",
        "offer_url": "https://shop.example.test/p/examplebook-14",
        "retrieved_at": RETRIEVED_AT,
        "raw_title": "ExampleBook 14 (Synthetic)",
        "raw_configuration_text": "16GB RAM, 512GB SSD",
        "price": Decimal("49999.00"),
        "currency": "PHP",
        "raw_price_text": "PHP 49,999.00",
        "availability": Availability.IN_STOCK,
        "availability_evidence": "In stock",
        "offer_index": 0,
        "source": SOURCE,
    } | overrides
    return OfferObservation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "separator", "expected"),
    [
        ("₱49,999.00", ".", "49999.00"),
        ("PHP 64,500", ".", "64500"),
        ("64500", ".", "64500"),
        ("USD 1,299.99", ".", "1299.99"),
        ("49 999", ".", "49999"),
        ("1.234,56 EUR", ",", "1234.56"),
        ("0.50", ".", "0.50"),
    ],
)
def test_parse_price_returns_exact_decimals(text: str, separator: str, expected: str) -> None:
    price = parse_price(text, decimal_separator=separator)
    assert isinstance(price, Decimal)
    assert str(price) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Call for price",
        "PHP 1,000 - 2,000",
        "Was 59,999 now 49,999",
        "-500",
        "0",
        "0.00",
        "1,23,456",
        "1.234.567",
        "49,99",
    ],
)
def test_parse_price_rejects_ambiguous_or_invalid_text(text: str) -> None:
    with pytest.raises(ObservationValidationError) as error:
        parse_price(text)
    assert error.value.field_name == "price"


def test_parse_price_requires_a_supported_decimal_separator() -> None:
    with pytest.raises(ValueError):
        parse_price("100", decimal_separator=" ")


def test_clean_text_collapses_whitespace_and_keeps_wording() -> None:
    assert clean_text("  Example  Book\n\t14  ") == "Example Book 14"
    assert clean_text("Café Edition") == "Café Edition"


def test_redact_url_drops_credentials_query_and_fragment() -> None:
    url = "https://user:secret@Shop.Example.test:8443/p/x?session=abc#top"
    assert redact_url(url) == "https://shop.example.test:8443/p/x"
    assert redact_url("https://shop.example.test:bad/") == "<invalid-url>"


def test_record_is_deterministic_and_has_no_target_assignment() -> None:
    record = observation().to_record()
    assert list(record) == list(OBSERVATION_COLUMNS)
    assert record["price"] == "49999.00"
    assert record["retrieved_at"] == "2026-09-29T04:00:00.000000Z"
    assert record == observation().to_record()
    assert "target_id" not in record
    assert not hasattr(observation(), "target_id")


@pytest.mark.parametrize(
    ("overrides", "field_name"),
    [
        ({"price": 49999.0}, "price"),
        ({"price": Decimal("NaN")}, "price"),
        ({"price": Decimal("0")}, "price"),
        ({"retrieved_at": datetime(2026, 9, 29, 12, 0)}, "retrieved_at"),
        ({"retrieved_at": RETRIEVED_AT.astimezone(timezone(timedelta(hours=8)))}, "retrieved_at"),
        ({"currency": "php"}, "currency"),
        ({"currency": "PESO"}, "currency"),
        ({"retailer_id": "Example Shop"}, "retailer_id"),
        ({"offer_url": "/p/relative"}, "offer_url"),
        ({"raw_title": ""}, "raw_title"),
        ({"raw_title": " ExampleBook"}, "raw_title"),
        ({"raw_price_text": ""}, "raw_price_text"),
        ({"availability": "IN_STOCK"}, "availability"),
        ({"availability_evidence": ""}, "availability_evidence"),
        ({"offer_index": -1}, "offer_index"),
        ({"source": replace(SOURCE, content_sha256="abc")}, "content_sha256"),
        ({"source": replace(SOURCE, attempts=0)}, "attempts"),
        ({"source": replace(SOURCE, adapter_version="")}, "adapter_version"),
    ],
)
def test_observation_rejects_invalid_values(overrides: dict[str, object], field_name: str) -> None:
    with pytest.raises(ObservationValidationError) as error:
        observation(**overrides)
    assert error.value.field_name == field_name


def test_unknown_availability_may_omit_evidence() -> None:
    result = observation(availability=Availability.UNKNOWN, availability_evidence="")
    assert result.to_record()["availability"] == "UNKNOWN"
