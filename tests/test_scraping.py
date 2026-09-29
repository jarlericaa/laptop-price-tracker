from __future__ import annotations

import hashlib
import logging
import urllib.request
from datetime import timedelta
from decimal import Decimal

import pytest
from example_adapter import EXAMPLE_HOST, EXAMPLE_URL, ExampleShopAdapter, fixture_bytes
from scraping_fakes import START, FakeClock, FakeTransport, no_network  # noqa: F401

from laptop_tracker.offers import Availability
from laptop_tracker.scraping import (
    FetchConfig,
    FetchedPage,
    Fetcher,
    FetchError,
    ParsedOffer,
    ScrapeRequest,
    Stage,
    TransportError,
    TransportResponse,
    UrllibTransport,
    build_observation,
    scrape,
)

HTML = {"content-type": "text/html; charset=utf-8"}
pytestmark = pytest.mark.usefixtures("no_network")


def page(name: str, url: str = EXAMPLE_URL, headers: dict[str, str] = HTML) -> TransportResponse:
    return TransportResponse(200, url, fixture_bytes(name), headers)


def status(code: int, retry_after: str | None = None) -> TransportResponse:
    headers = {} if retry_after is None else {"retry-after": retry_after}
    return TransportResponse(code, EXAMPLE_URL, b"", headers)


def run(*script, config: FetchConfig | None = None, url: str = EXAMPLE_URL, adapter=None):
    clock = FakeClock()
    transport = FakeTransport(clock, *script)
    fetcher = Fetcher(transport, config or FetchConfig(), clock)
    request = ScrapeRequest("example_shop", url)
    [result] = scrape(adapter or ExampleShopAdapter(), [request], fetcher)
    return result, transport, clock


# Contract and normalization


def test_listing_fixture_produces_attributable_observations() -> None:
    result, transport, _ = run(page("example_listing.html"))

    assert result.ok and len(transport.calls) == 1
    first, second = result.observations
    assert first.retailer_id == "example_shop"
    assert first.offer_url == f"https://{EXAMPLE_HOST}/p/examplebook-14?ref=listing"
    assert first.raw_title == "ExampleBook 14 & Pen (Synthetic)"
    assert first.raw_configuration_text.startswith("Example CPU X1, 16GB RAM")
    assert first.price == Decimal("49999.00") and first.currency == "PHP"
    assert first.raw_price_text == "₱49,999.00"
    assert (first.availability, first.availability_evidence) == (Availability.IN_STOCK, "In stock")
    assert first.retrieved_at == START
    assert first.source.requested_url == first.source.final_url == EXAMPLE_URL
    assert first.source.http_status == 200 and first.source.attempts == 1
    assert first.source.adapter_version == "example-1"
    expected_hash = hashlib.sha256(fixture_bytes("example_listing.html")).hexdigest()
    assert first.source.content_sha256 == expected_hash
    assert (second.offer_index, second.price) == (1, Decimal("64500"))
    assert second.availability is Availability.OUT_OF_STOCK


def test_normalization_is_deterministic_across_runs() -> None:
    first_run, _, _ = run(page("example_listing.html"))
    second_run, _, _ = run(page("example_listing.html"))
    records = [observation.to_record() for observation in first_run.observations]

    assert records == [observation.to_record() for observation in second_run.observations]
    assert records[1] == {
        "retailer_id": "example_shop",
        "offer_url": f"https://{EXAMPLE_HOST}/p/examplebook-16",
        "retrieved_at": "2026-09-29T04:00:00.000000Z",
        "raw_title": "ExampleBook 16 (Synthetic)",
        "raw_configuration_text": "Example CPU X2, 32GB RAM, 1TB SSD",
        "price": "64500",
        "currency": "PHP",
        "raw_price_text": "PHP 64,500",
        "availability": "OUT_OF_STOCK",
        "availability_evidence": "Out of stock",
        "offer_index": "1",
        "requested_url": EXAMPLE_URL,
        "final_url": EXAMPLE_URL,
        "http_status": "200",
        "attempts": "1",
        "content_sha256": hashlib.sha256(fixture_bytes("example_listing.html")).hexdigest(),
        "content_type": "text/html; charset=utf-8",
        "adapter_version": "example-1",
    }


def test_build_observation_normalizes_currency_and_availability_text() -> None:
    fetched = FetchedPage(
        "example_shop", EXAMPLE_URL, EXAMPLE_URL, 200, b"x", "", START, attempts=1
    )
    offer = ParsedOffer("  Example  14 ", " PHP 1,000 ", " php ", "PREORDER", "Pre-order")
    result = build_observation(ExampleShopAdapter(), fetched, offer, 0)
    assert (result.raw_title, result.currency, result.price) == ("Example 14", "PHP", Decimal(1000))
    assert result.availability is Availability.PREORDER
    assert result.offer_url == EXAMPLE_URL


# Retries, timeouts, and throttling


def test_retryable_status_backs_off_then_succeeds_with_throttling() -> None:
    result, transport, clock = run(status(503), status(503), page("example_listing.html"))

    assert result.ok and result.observations[0].source.attempts == 3
    # Backoff 5s, then the 10s per-retailer interval adds 5s; backoff 10s already satisfies it.
    assert clock.sleeps == [5.0, 5.0, 10.0]
    assert [call["at"] for call in transport.calls] == [0.0, 10.0, 20.0]
    assert all(call["timeout"] == 20.0 for call in transport.calls)


def test_timeouts_are_retried_up_to_the_limit_then_reported() -> None:
    timeout = TransportError("TIMEOUT", "The request timed out.")
    result, transport, _ = run(timeout, timeout, timeout)

    assert len(transport.calls) == 3 and not result.observations
    [failure] = result.failures
    assert (failure.stage, failure.error_code, failure.retryable) == (Stage.FETCH, "TIMEOUT", True)
    assert failure.attempts == 3


def test_max_attempts_is_configurable() -> None:
    result, transport, _ = run(status(502), status(502), config=FetchConfig(max_attempts=2))
    assert len(transport.calls) == 2
    assert result.failures[0].http_status == 502


@pytest.mark.parametrize("code", [403, 404])
def test_non_retryable_status_fails_after_one_attempt(code: int) -> None:
    result, transport, clock = run(status(code))

    assert len(transport.calls) == 1 and clock.sleeps == []
    [failure] = result.failures
    assert (failure.error_code, failure.http_status, failure.retryable) == (
        "HTTP_STATUS",
        code,
        False,
    )


def test_retry_after_seconds_are_honored() -> None:
    result, _, clock = run(status(429, retry_after="30"), page("example_listing.html"))
    assert result.ok
    assert clock.sleeps == [30.0]


def test_retry_after_above_the_limit_stops_without_waiting() -> None:
    result, transport, clock = run(status(503, retry_after="3600"))

    assert len(transport.calls) == 1 and clock.sleeps == []
    [failure] = result.failures
    assert (failure.error_code, failure.retryable) == ("RETRY_AFTER_TOO_LONG", True)


def test_throttling_is_tracked_per_retailer() -> None:
    clock = FakeClock()
    responses = [page("example_listing.html") for _ in range(4)]
    transport = FakeTransport(clock, *responses)
    config = FetchConfig(retailer_min_intervals={"slow_shop": 30.0})
    fetcher = Fetcher(transport, config, clock)

    for retailer in ("example_shop", "example_shop", "other_shop", "slow_shop"):
        fetcher.fetch(ScrapeRequest(retailer, EXAMPLE_URL))
    fetcher.throttle.wait("slow_shop")

    assert [call["at"] for call in transport.calls] == [0.0, 10.0, 10.0, 10.0]
    assert clock.elapsed == 40.0


# Parse, validation, and request failures


def test_layout_change_is_a_parse_failure_and_is_not_retried() -> None:
    result, transport, _ = run(page("example_layout_changed.html"))

    assert len(transport.calls) == 1 and not result.observations
    [failure] = result.failures
    assert (failure.stage, failure.error_code, failure.retryable) == (
        Stage.PARSE,
        "NO_OFFERS_FOUND",
        False,
    )


def test_invalid_offers_are_rejected_individually() -> None:
    result, _, _ = run(page("example_partial.html"))

    [kept] = result.observations
    assert (kept.price, kept.availability) == (Decimal("39999.50"), Availability.PREORDER)
    assert [(f.stage, f.offer_index, f.message.split(":")[0]) for f in result.failures] == [
        (Stage.VALIDATE, 1, "price"),
        (Stage.VALIDATE, 2, "offer_url"),
    ]
    assert not result.ok


class BrokenAdapter(ExampleShopAdapter):
    def parse(self, page: FetchedPage) -> list[ParsedOffer]:
        raise RuntimeError("unexpected markup near BODY-SECRET")


def test_adapter_bug_becomes_a_safe_parse_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="laptop_tracker.scraping")
    result, _, _ = run(page("example_listing.html"), adapter=BrokenAdapter())

    [failure] = result.failures
    assert (failure.stage, failure.error_code) == (Stage.PARSE, "ADAPTER_ERROR")
    assert failure.message == "Adapter raised RuntimeError."
    assert "BODY-SECRET" not in caplog.text


def test_request_guards_reject_before_any_network_call() -> None:
    off_site, transport, _ = run(url="https://elsewhere.example.test/laptops")
    assert off_site.failures[0].error_code == "HOST_NOT_ALLOWED" and transport.calls == []

    clock = FakeClock()
    fetcher = Fetcher(FakeTransport(clock), FetchConfig(), clock)
    [mismatch] = scrape(ExampleShopAdapter(), [ScrapeRequest("other_shop", EXAMPLE_URL)], fetcher)
    assert mismatch.failures[0].error_code == "RETAILER_MISMATCH"


def test_redirect_to_another_host_is_rejected() -> None:
    redirected = page("example_listing.html", url="https://elsewhere.example.test/laptops")
    result, _, _ = run(redirected)
    assert result.failures[0].error_code == "REDIRECT_HOST_NOT_ALLOWED"
    assert not result.observations


def test_one_failed_request_does_not_stop_the_batch() -> None:
    clock = FakeClock()
    transport = FakeTransport(clock, status(404), page("example_listing.html"))
    fetcher = Fetcher(transport, FetchConfig(), clock)
    requests = [
        ScrapeRequest("example_shop", f"https://{EXAMPLE_HOST}/missing"),
        ScrapeRequest("example_shop", EXAMPLE_URL),
    ]
    missing, found = scrape(ExampleShopAdapter(), requests, fetcher)
    assert missing.failures[0].http_status == 404
    assert found.ok and len(found.observations) == 2


# Logging and secrets


def test_logs_carry_context_but_no_secrets_or_page_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="laptop_tracker.scraping")
    config = FetchConfig(
        extra_headers={"Cookie": "SECRET-COOKIE", "Authorization": "Bearer SECRET-TOKEN"}
    )
    ok = page("example_listing.html", headers={**HTML, "set-cookie": "SECRET-SET-COOKIE"})
    url = f"https://{EXAMPLE_HOST}/laptops?session=SECRET-QUERY#SECRET-FRAGMENT"
    result, transport, _ = run(status(503), ok, config=config, url=url)

    assert result.ok
    assert transport.calls[0]["headers"]["Cookie"] == "SECRET-COOKIE"
    assert "retailer=example_shop" in caplog.text
    assert f"url=https://{EXAMPLE_HOST}/laptops " in caplog.text
    assert "stage=fetch attempt=1 error_code=HTTP_STATUS http_status=503" in caplog.text
    assert "attempt=2 http_status=200" in caplog.text
    fields = [record.scrape for record in caplog.records]
    assert fields[0]["retry_in_seconds"] == 5.0
    for secret in ("SECRET", "ExampleBook", "49,999"):
        assert secret not in caplog.text
    # Observation data (not logs) keeps the exact requested URL for attribution.
    assert result.observations[0].source.requested_url == url


def test_failure_urls_are_redacted() -> None:
    result, _, _ = run(status(404), url=f"{EXAMPLE_URL}?token=SECRET-QUERY")
    assert result.failures[0].url == EXAMPLE_URL


def test_fetched_page_repr_excludes_the_body() -> None:
    fetched = FetchedPage(
        "example_shop", EXAMPLE_URL, EXAMPLE_URL, 200, b"BODY-SECRET", "", START, 1
    )
    assert "BODY-SECRET" not in repr(fetched)
    assert fetched.text() == "BODY-SECRET"


# Configuration and default transport


def test_urllib_transport_refuses_non_http_urls_without_connecting() -> None:
    with pytest.raises(TransportError) as error:
        UrllibTransport().fetch("file:///etc/hosts", timeout=1, headers={}, max_bytes=10)
    assert (error.value.code, error.value.retryable) == ("INVALID_URL", False)


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_seconds": 0},
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"backoff_multiplier": 0.5},
        {"min_request_interval_seconds": -1},
        {"retailer_min_intervals": {"example_shop": -1}},
        {"max_response_bytes": 0},
        {"max_redirects": -1},
        {"max_redirects": 11},
    ],
)
def test_fetch_config_rejects_unsafe_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        FetchConfig(**overrides)  # type: ignore[arg-type]


def test_fetch_config_defaults_are_conservative() -> None:
    config = FetchConfig()
    assert (config.timeout_seconds, config.max_attempts) == (20.0, 3)
    assert config.min_request_interval_seconds == 10.0
    assert [config.backoff_seconds(n) for n in (1, 2, 3, 10)] == [5.0, 10.0, 20.0, 60.0]


@pytest.mark.parametrize(
    ("retailer_id", "url"),
    [
        ("Example Shop", EXAMPLE_URL),
        ("example_shop", "/laptops"),
        ("example_shop", "ftp://x/y"),
        ("example_shop", f"https://user:secret@{EXAMPLE_HOST}/laptops"),
        ("example_shop", f"https://{EXAMPLE_HOST}:bad/laptops"),
    ],
)
def test_scrape_request_validates_inputs(retailer_id: str, url: str) -> None:
    with pytest.raises(ValueError):
        ScrapeRequest(retailer_id, url)


# Redirects (Phase 7 audit: a disallowed redirect must never be requested)


def redirect(location: str, code: int = 302) -> TransportResponse:
    return TransportResponse(code, EXAMPLE_URL, b"", {"location": location})


def test_redirect_to_disallowed_host_is_stopped_before_it_is_sent() -> None:
    result, transport, _ = run(redirect("https://elsewhere.example.test/laptops"))

    assert [call["url"] for call in transport.calls] == [EXAMPLE_URL]
    [failure] = result.failures
    assert (failure.stage, failure.error_code, failure.retryable) == (
        Stage.FETCH,
        "REDIRECT_HOST_NOT_ALLOWED",
        False,
    )
    assert failure.http_status == 302 and not result.observations


def test_redirect_within_allowed_hosts_is_followed_and_throttled() -> None:
    target = f"https://{EXAMPLE_HOST}/laptops-v2"
    moved = page("example_listing.html", url=target)
    result, transport, clock = run(redirect("/laptops-v2", code=301), moved)

    assert result.ok
    assert [call["url"] for call in transport.calls] == [EXAMPLE_URL, target]
    assert [call["at"] for call in transport.calls] == [0.0, 10.0]
    first = result.observations[0]
    assert first.source.requested_url == EXAMPLE_URL and first.source.final_url == target
    # Retrieval time is when the request that produced the page started (after throttling).
    assert first.retrieved_at == START + timedelta(seconds=10)
    assert clock.sleeps == [10.0]


@pytest.mark.parametrize(
    "location", ["ftp://shop.example.test/x", f"https://user:secret@{EXAMPLE_HOST}/"]
)
def test_invalid_redirect_targets_are_not_followed(location: str) -> None:
    result, transport, _ = run(redirect(location))
    assert len(transport.calls) == 1
    assert result.failures[0].error_code == "INVALID_REDIRECT"


def test_redirect_loops_are_bounded() -> None:
    config = FetchConfig(max_redirects=2, min_request_interval_seconds=0)
    loop = [redirect(EXAMPLE_URL) for _ in range(3)]
    result, transport, _ = run(*loop, config=config)
    assert len(transport.calls) == 3
    assert result.failures[0].error_code == "TOO_MANY_REDIRECTS"


def test_redirect_without_location_is_an_http_status_failure() -> None:
    result, _, _ = run(TransportResponse(302, EXAMPLE_URL, b"", {}))
    assert (result.failures[0].error_code, result.failures[0].http_status) == ("HTTP_STATUS", 302)


def test_direct_fetch_defaults_to_same_host_redirects_only() -> None:
    clock = FakeClock()
    transport = FakeTransport(clock, redirect("https://elsewhere.example.test/"))
    fetcher = Fetcher(transport, FetchConfig(), clock)
    with pytest.raises(FetchError) as error:
        fetcher.fetch(ScrapeRequest("example_shop", EXAMPLE_URL))
    assert error.value.code == "REDIRECT_HOST_NOT_ALLOWED"
    assert len(transport.calls) == 1


def test_urllib_transport_does_not_follow_redirects_itself() -> None:
    handlers = [
        handler
        for handler in UrllibTransport()._opener.handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
    ]
    elsewhere = "https://elsewhere.example.test/"
    assert handlers
    for handler in handlers:
        assert handler.redirect_request(None, None, 302, "Found", {}, elsewhere) is None
    assert UrllibTransport.live is True


# Live-use gate


class ApprovedExampleAdapter(ExampleShopAdapter):
    live_use_approved = True


def test_live_transport_is_blocked_without_an_approved_data_use_review() -> None:
    clock = FakeClock()
    transport = FakeTransport(clock, page("example_listing.html"), live=True)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    [result] = scrape(ExampleShopAdapter(), [ScrapeRequest("example_shop", EXAMPLE_URL)], fetcher)

    assert transport.calls == []
    assert (result.failures[0].stage, result.failures[0].error_code) == (
        Stage.REQUEST,
        "LIVE_USE_NOT_APPROVED",
    )


def test_live_transport_is_allowed_only_for_an_approved_adapter() -> None:
    clock = FakeClock()
    transport = FakeTransport(clock, page("example_listing.html"), live=True)
    fetcher = Fetcher(transport, FetchConfig(), clock)
    request = ScrapeRequest("example_shop", EXAMPLE_URL)
    [result] = scrape(ApprovedExampleAdapter(), [request], fetcher)
    assert result.ok and len(transport.calls) == 1


def test_transports_without_a_live_flag_are_treated_as_live() -> None:
    class Unmarked:
        def fetch(self, url, *, timeout, headers, max_bytes):  # pragma: no cover
            raise AssertionError("must not be called")

    fetcher = Fetcher(Unmarked(), FetchConfig(), FakeClock())
    assert fetcher.is_live
    [result] = scrape(ExampleShopAdapter(), [ScrapeRequest("example_shop", EXAMPLE_URL)], fetcher)
    assert result.failures[0].error_code == "LIVE_USE_NOT_APPROVED"


def test_existing_records_are_unchanged_and_full_records_add_blank_details() -> None:
    result, _, _ = run(page("example_listing.html"))
    first = result.observations[0]
    assert first.details is None
    full = first.to_full_record()
    assert list(full)[: len(first.to_record())] == list(first.to_record())
    assert all(full[column] == "" for column in full if column not in first.to_record())
