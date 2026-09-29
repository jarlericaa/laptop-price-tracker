"""Shared, retailer-neutral scraping framework and adapter contract (overall Phase 6).

The framework owns retrieval (timeouts, bounded retries, per-retailer throttling), attribution,
normalization, and error reporting. A retailer adapter only turns a fetched page into
``ParsedOffer`` values. Transport, clock, and sleep are injectable so tests run offline and
deterministically. Nothing here reads Google Sheets, assigns ``target_id``, or matches offers to
tracked configurations. See docs/SCRAPING_FRAMEWORK.md.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urljoin

from .offers import (
    RETAILER_ID_PATTERN,
    Availability,
    ObservationValidationError,
    OfferDetails,
    OfferObservation,
    RetrievalMetadata,
    clean_text,
    is_http_url,
    parse_price,
    redact_url,
    url_host,
)

LOGGER = logging.getLogger(__name__)

# Response headers kept from a transport. Everything else, including Set-Cookie, is dropped.
KEPT_RESPONSE_HEADERS = frozenset({"content-type", "retry-after", "location"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class Stage(StrEnum):
    REQUEST = "request"
    FETCH = "fetch"
    PARSE = "parse"
    VALIDATE = "validate"


class TransportError(Exception):
    """Raised by a transport when no HTTP response was received (timeout, connection, size)."""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class FetchError(Exception):
    """Retrieval failed after the framework's retry policy was applied."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        attempts: int,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.attempts = attempts
        self.http_status = http_status


class ParseError(Exception):
    """Raised by an adapter when a fetched page cannot be parsed. Never retried.

    Adapter authors must keep ``message`` free of page content, cookies, and credentials.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FetchConfig:
    """Retrieval policy. Defaults are deliberately conservative; see docs/SCRAPING_FRAMEWORK.md."""

    timeout_seconds: float = 20.0
    max_attempts: int = 3
    backoff_initial_seconds: float = 5.0
    backoff_multiplier: float = 2.0
    backoff_max_seconds: float = 60.0
    min_request_interval_seconds: float = 10.0
    retailer_min_intervals: Mapping[str, float] = field(default_factory=dict)
    retry_statuses: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
    max_response_bytes: int = 5_000_000
    max_redirects: int = 5
    user_agent: str = "laptop-price-tracker/0.1"
    extra_headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10.")
        if self.backoff_initial_seconds < 0 or self.backoff_max_seconds < 0:
            raise ValueError("Backoff seconds must not be negative.")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be at least 1.")
        intervals = [self.min_request_interval_seconds, *self.retailer_min_intervals.values()]
        if any(interval < 0 for interval in intervals):
            raise ValueError("Request intervals must not be negative.")
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive.")
        if not 0 <= self.max_redirects <= 10:
            raise ValueError("max_redirects must be between 0 and 10.")

    def interval_for(self, retailer_id: str) -> float:
        return self.retailer_min_intervals.get(retailer_id, self.min_request_interval_seconds)

    def backoff_seconds(self, attempt: int) -> float:
        """Delay after failed ``attempt`` (1-based). No jitter, so tests are deterministic."""
        delay = self.backoff_initial_seconds * self.backoff_multiplier ** (attempt - 1)
        return min(delay, self.backoff_max_seconds)


@dataclass(frozen=True)
class ScrapeRequest:
    """One page to retrieve for one retailer. Carries no target or matching information."""

    retailer_id: str
    url: str

    def __post_init__(self) -> None:
        if not re.fullmatch(RETAILER_ID_PATTERN, self.retailer_id):
            raise ValueError("retailer_id must be lowercase letters, digits, or underscores.")
        if not is_http_url(self.url):
            raise ValueError(
                "ScrapeRequest.url must be an absolute HTTP or HTTPS URL without credentials."
            )


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    final_url: str
    body: bytes = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    """One GET per call. Transports must not follow redirects themselves.

    A 3xx response is returned with its ``location`` header so the ``Fetcher`` can check the
    target host before any redirected request is sent. A transport that can reach the network
    is treated as live unless it sets ``live = False`` (offline fakes and fixtures only).
    """

    def fetch(
        self, url: str, *, timeout: float, headers: Mapping[str, str], max_bytes: int
    ) -> TransportResponse:
        """Perform one GET. Return any HTTP response; raise ``TransportError`` for no response."""
        ...


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Stops urllib from following redirects, so a 3xx surfaces as an ``HTTPError``."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """Standard-library HTTP(S) GET with no cookie handling and no automatic redirects.

    This is the default for future live use; the offline tests never call it over the network.
    """

    live = True

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def fetch(
        self, url: str, *, timeout: float, headers: Mapping[str, str], max_bytes: int
    ) -> TransportResponse:
        if not is_http_url(url):
            raise TransportError(
                "INVALID_URL", "Only absolute HTTP or HTTPS URLs are fetched.", retryable=False
            )
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise TransportError(
                        "RESPONSE_TOO_LARGE",
                        f"Response exceeded {max_bytes} bytes.",
                        retryable=False,
                    )
                return TransportResponse(
                    response.status, response.geturl(), body, _kept_headers(response.headers)
                )
        except urllib.error.HTTPError as error:
            # Includes unfollowed 3xx responses. The body is not read: status and headers are
            # enough to decide on a retry or a redirect.
            with error:
                return TransportResponse(error.code, url, b"", _kept_headers(error.headers))
        except TimeoutError:
            raise TransportError("TIMEOUT", "The request timed out.") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TransportError("TIMEOUT", "The request timed out.") from None
            raise TransportError(
                "CONNECTION_ERROR", f"Connection failed ({type(error.reason).__name__})."
            ) from None
        except (OSError, http.client.HTTPException) as error:
            raise TransportError(
                "CONNECTION_ERROR", f"Connection failed ({type(error).__name__})."
            ) from None


def _kept_headers(headers: object) -> dict[str, str]:
    items = headers.items() if headers is not None else []  # type: ignore[attr-defined]
    return {
        name.lower(): value for name, value in items if name.lower() in KEPT_RESPONSE_HEADERS
    }


@dataclass(frozen=True)
class FetchedPage:
    """A successful retrieval. The body is excluded from ``repr`` so it cannot leak into logs."""

    retailer_id: str
    requested_url: str
    final_url: str
    status_code: int
    body: bytes = field(repr=False)
    content_type: str
    retrieved_at: datetime
    attempts: int

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self) -> str:
        """Decode with the declared charset, else UTF-8; undecodable bytes are replaced."""
        match = re.search(r"charset=[\"']?([A-Za-z0-9._-]+)", self.content_type)
        encoding = match.group(1) if match else "utf-8"
        try:
            return self.body.decode(encoding, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class Throttle:
    """Enforces a minimum interval between request starts, tracked separately per retailer."""

    def __init__(self, config: FetchConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock
        self._last_start: dict[str, float] = {}

    def wait(self, retailer_id: str) -> float:
        """Sleep if needed, record this request start, and return the seconds slept."""
        last = self._last_start.get(retailer_id)
        delay = 0.0
        if last is not None:
            ready_at = last + self._config.interval_for(retailer_id)
            delay = max(0.0, ready_at - self._clock.monotonic())
        if delay > 0:
            self._clock.sleep(delay)
        self._last_start[retailer_id] = self._clock.monotonic()
        return delay


class Fetcher:
    """Fetches with timeouts, bounded retries, per-retailer throttling, and checked redirects.

    Share one fetcher across adapters and requests so throttling state is shared.
    """

    def __init__(
        self,
        transport: Transport | None = None,
        config: FetchConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.transport = transport if transport is not None else UrllibTransport()
        self.config = config if config is not None else FetchConfig()
        self.clock = clock if clock is not None else SystemClock()
        self.throttle = Throttle(self.config, self.clock)

    @property
    def is_live(self) -> bool:
        """True unless the transport explicitly declares ``live = False`` (fail closed)."""
        return getattr(self.transport, "live", True) is not False

    def fetch(
        self, request: ScrapeRequest, allowed_hosts: frozenset[str] | None = None
    ) -> FetchedPage:
        """Fetch ``request.url``. Redirects are followed only to ``allowed_hosts``.

        The default allows only the request's own host. A redirect elsewhere fails before the
        redirected request is sent.
        """
        config = self.config
        hosts = allowed_hosts if allowed_hosts is not None else frozenset({url_host(request.url)})
        headers = {"User-Agent": config.user_agent, **config.extra_headers}
        log_fields = {"retailer": request.retailer_id, "url": redact_url(request.url)}
        for attempt in range(1, config.max_attempts + 1):
            retry_after: float | None = None
            try:
                response, retrieved_at = self._send(request, hosts, headers, attempt, log_fields)
            except TransportError as error:
                failure = FetchError(
                    error.code, error.message, retryable=error.retryable, attempts=attempt
                )
            except FetchError as failure_from_redirect:
                failure = failure_from_redirect
            else:
                status = response.status_code
                if 200 <= status < 300:
                    _log(
                        logging.INFO,
                        "fetch succeeded",
                        **log_fields,
                        stage=Stage.FETCH,
                        attempt=attempt,
                        http_status=status,
                        bytes=len(response.body),
                    )
                    return FetchedPage(
                        retailer_id=request.retailer_id,
                        requested_url=request.url,
                        final_url=response.final_url or request.url,
                        status_code=status,
                        body=response.body,
                        content_type=response.headers.get("content-type", ""),
                        retrieved_at=retrieved_at,
                        attempts=attempt,
                    )
                failure = FetchError(
                    "HTTP_STATUS",
                    f"Retailer returned HTTP {status}.",
                    retryable=status in config.retry_statuses,
                    attempts=attempt,
                    http_status=status,
                )
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))

            if not failure.retryable or attempt == config.max_attempts:
                _log_failure(failure, log_fields, attempt, final=True)
                raise failure
            delay = config.backoff_seconds(attempt)
            if retry_after is not None:
                if retry_after > config.backoff_max_seconds:
                    failure = FetchError(
                        "RETRY_AFTER_TOO_LONG",
                        f"Retailer asked to wait {retry_after:g}s, above the "
                        f"{config.backoff_max_seconds:g}s limit; not retrying now.",
                        retryable=True,
                        attempts=attempt,
                        http_status=failure.http_status,
                    )
                    _log_failure(failure, log_fields, attempt, final=True)
                    raise failure
                delay = max(delay, retry_after)
            _log_failure(failure, log_fields, attempt, final=False, retry_in_seconds=delay)
            self.clock.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _send(
        self,
        request: ScrapeRequest,
        hosts: frozenset[str],
        headers: Mapping[str, str],
        attempt: int,
        log_fields: Mapping[str, str],
    ) -> tuple[TransportResponse, datetime]:
        """One attempt: GET, then follow allowed redirects, each hop throttled.

        Returns the last response and the UTC time its request started. The response's
        ``final_url`` is set to the URL that produced it.
        """
        url = request.url
        for hop in range(self.config.max_redirects + 1):
            self.throttle.wait(request.retailer_id)
            started_at = self.clock.now().astimezone(UTC)
            response = self.transport.fetch(
                url,
                timeout=self.config.timeout_seconds,
                headers=headers,
                max_bytes=self.config.max_response_bytes,
            )
            location = response.headers.get("location", "")
            if response.status_code not in REDIRECT_STATUSES or not location:
                return (
                    TransportResponse(
                        response.status_code,
                        response.final_url or url,
                        response.body,
                        response.headers,
                    ),
                    started_at,
                )
            target = urljoin(url, location.strip())
            if not is_http_url(target):
                raise FetchError(
                    "INVALID_REDIRECT",
                    "Redirect target is not an absolute HTTP or HTTPS URL without credentials.",
                    retryable=False,
                    attempts=attempt,
                    http_status=response.status_code,
                )
            if url_host(target) not in hosts:
                raise FetchError(
                    "REDIRECT_HOST_NOT_ALLOWED",
                    "Redirect target host is not allowed; the redirect was not followed.",
                    retryable=False,
                    attempts=attempt,
                    http_status=response.status_code,
                )
            _log(
                logging.INFO,
                "fetch redirected",
                **log_fields,
                stage=Stage.FETCH,
                attempt=attempt,
                http_status=response.status_code,
                redirect_to=redact_url(target),
                hop=hop + 1,
            )
            url = target
        raise FetchError(
            "TOO_MANY_REDIRECTS",
            f"More than {self.config.max_redirects} redirects.",
            retryable=False,
            attempts=attempt,
        )


def _retry_after_seconds(value: str | None) -> float | None:
    """Integer-seconds ``Retry-After`` only; the HTTP-date form is ignored (normal backoff)."""
    if value is None or not value.strip().isdigit():
        return None
    return float(value.strip())


@dataclass(frozen=True)
class ParsedOffer:
    """What an adapter extracted for one offer. The framework adds attribution and validates.

    ``price_text`` is parsed by the framework with ``parse_price``. ``offer_url`` may be relative
    to the page; blank means the offer is the page itself. Adapters map retailer wording to
    ``availability`` and pass that wording as ``availability_evidence``. ``details`` is optional
    retailer evidence (SKU, regular/sale price, raw specs, bundle flag); its text is normalized
    and its prices are checked against ``price``.
    """

    raw_title: str
    price_text: str
    currency: str
    availability: Availability | str
    availability_evidence: str = ""
    raw_configuration_text: str = ""
    offer_url: str = ""
    decimal_separator: str = "."
    details: OfferDetails | None = None


class RetailerAdapter(Protocol):
    """Contract a future retailer adapter implements.

    ``allowed_hosts`` lists exact host names; requests, redirects, and offer links to any
    other host are rejected. ``parse`` returns zero or more offers, or raises ``ParseError``
    when the page cannot be understood (for example after a layout change).

    Optional ``live_use_approved: bool``: only an adapter that sets it to ``True``, after
    documented project-owner authorization, may be used with a live transport. A missing
    attribute counts as not approved.
    """

    retailer_id: str
    adapter_version: str
    allowed_hosts: frozenset[str]

    def parse(self, page: FetchedPage) -> Sequence[ParsedOffer]: ...


@dataclass(frozen=True)
class ScrapeFailure:
    """A caller-safe failure record: codes and redacted URLs only, never page content."""

    retailer_id: str
    url: str
    stage: Stage
    error_code: str
    message: str
    retryable: bool
    attempts: int
    http_status: int | None = None
    offer_index: int | None = None


@dataclass(frozen=True)
class ScrapeResult:
    request: ScrapeRequest
    observations: tuple[OfferObservation, ...]
    failures: tuple[ScrapeFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


def build_observation(
    adapter: RetailerAdapter, page: FetchedPage, offer: ParsedOffer, offer_index: int
) -> OfferObservation:
    """Deterministically normalize one parsed offer into an attributable observation."""
    offer_url = urljoin(page.final_url, offer.offer_url) if offer.offer_url else page.final_url
    if url_host(offer_url) not in _allowed_hosts(adapter):
        raise ObservationValidationError("offer_url", "host is not in the adapter's allowed_hosts")
    try:
        availability = Availability(offer.availability)
    except ValueError:
        raise ObservationValidationError("availability", "is not a known value") from None
    return OfferObservation(
        retailer_id=adapter.retailer_id,
        offer_url=offer_url,
        retrieved_at=page.retrieved_at,
        raw_title=clean_text(offer.raw_title),
        raw_configuration_text=clean_text(offer.raw_configuration_text),
        price=parse_price(offer.price_text, decimal_separator=offer.decimal_separator),
        currency=offer.currency.strip().upper(),
        raw_price_text=clean_text(offer.price_text),
        availability=availability,
        availability_evidence=clean_text(offer.availability_evidence),
        offer_index=offer_index,
        source=RetrievalMetadata(
            requested_url=page.requested_url,
            final_url=page.final_url,
            http_status=page.status_code,
            attempts=page.attempts,
            content_sha256=page.content_sha256,
            content_type=page.content_type,
            adapter_version=adapter.adapter_version,
        ),
        details=offer.details,
    )


def live_use_approved(adapter: RetailerAdapter) -> bool:
    return getattr(adapter, "live_use_approved", False) is True


def scrape(
    adapter: RetailerAdapter, requests: Iterable[ScrapeRequest], fetcher: Fetcher
) -> list[ScrapeResult]:
    """Run every request through fetch, parse, and validation; one failure never stops others."""
    return [scrape_one(adapter, request, fetcher) for request in requests]


def scrape_one(adapter: RetailerAdapter, request: ScrapeRequest, fetcher: Fetcher) -> ScrapeResult:
    safe_url = redact_url(request.url)

    def failed(stage: Stage, code: str, message: str, attempts: int, **extra) -> ScrapeResult:
        failure = ScrapeFailure(
            request.retailer_id, safe_url, stage, code, message, attempts=attempts, **extra
        )
        _log(
            logging.WARNING,
            "scrape failed",
            retailer=request.retailer_id,
            url=safe_url,
            stage=stage,
            attempt=attempts,
            error_code=code,
        )
        return ScrapeResult(request, (), (failure,))

    if request.retailer_id != adapter.retailer_id:
        return failed(
            Stage.REQUEST,
            "RETAILER_MISMATCH",
            "Request retailer_id does not match the adapter.",
            0,
            retryable=False,
        )
    if url_host(request.url) not in _allowed_hosts(adapter):
        return failed(
            Stage.REQUEST,
            "HOST_NOT_ALLOWED",
            "Request host is not in the adapter's allowed_hosts.",
            0,
            retryable=False,
        )
    if fetcher.is_live and not live_use_approved(adapter):
        return failed(
            Stage.REQUEST,
            "LIVE_USE_NOT_APPROVED",
            "Adapter has no documented live-use approval; live requests are blocked.",
            0,
            retryable=False,
        )
    try:
        page = fetcher.fetch(request, allowed_hosts=_allowed_hosts(adapter))
    except FetchError as error:
        return failed(
            Stage.FETCH,
            error.code,
            error.message,
            error.attempts,
            retryable=error.retryable,
            http_status=error.http_status,
        )
    if url_host(page.final_url) not in _allowed_hosts(adapter):
        # Defense in depth for a transport that reports a different final URL.
        return failed(
            Stage.FETCH,
            "REDIRECT_HOST_NOT_ALLOWED",
            "Final URL after redirects is not in the adapter's allowed_hosts.",
            page.attempts,
            retryable=False,
            http_status=page.status_code,
        )
    try:
        offers = list(adapter.parse(page))
    except ParseError as error:
        return failed(Stage.PARSE, error.code, error.message, page.attempts, retryable=False)
    except Exception as error:
        # Adapter bugs become failures, not crashes. Only the exception type is reported
        # because adapter exception messages can quote page content.
        return failed(
            Stage.PARSE,
            "ADAPTER_ERROR",
            f"Adapter raised {type(error).__name__}.",
            page.attempts,
            retryable=False,
        )

    observations: list[OfferObservation] = []
    failures: list[ScrapeFailure] = []
    for index, offer in enumerate(offers):
        try:
            observations.append(build_observation(adapter, page, offer, index))
        except ObservationValidationError as error:
            failures.append(
                ScrapeFailure(
                    request.retailer_id,
                    safe_url,
                    Stage.VALIDATE,
                    "INVALID_OFFER",
                    str(error),
                    retryable=False,
                    attempts=page.attempts,
                    http_status=page.status_code,
                    offer_index=index,
                )
            )
            _log(
                logging.WARNING,
                "offer rejected",
                retailer=request.retailer_id,
                url=safe_url,
                stage=Stage.VALIDATE,
                offer_index=index,
                error_code="INVALID_OFFER",
                field=error.field_name,
            )
    _log(
        logging.INFO if offers else logging.WARNING,
        "scrape finished" if offers else "scrape found no offers",
        retailer=request.retailer_id,
        url=safe_url,
        stage=Stage.PARSE,
        observations=len(observations),
        rejected=len(failures),
    )
    return ScrapeResult(request, tuple(observations), tuple(failures))


def _allowed_hosts(adapter: RetailerAdapter) -> frozenset[str]:
    return frozenset(host.lower() for host in adapter.allowed_hosts)


def _log_failure(
    failure: FetchError, log_fields: dict[str, str], attempt: int, *, final: bool, **extra
) -> None:
    _log(
        logging.WARNING,
        "fetch failed" if final else "fetch retrying",
        **log_fields,
        stage=Stage.FETCH,
        attempt=attempt,
        error_code=failure.code,
        http_status=failure.http_status,
        retryable=failure.retryable,
        **extra,
    )


def _log(level: int, event: str, **fields: object) -> None:
    """Log ``event key=value ...``; fields are also attached as ``record.scrape`` for handlers.

    Callers pass only identifiers, redacted URLs, codes, and counts: never headers, cookies,
    credentials, or response bodies.
    """
    shown = {key: value for key, value in fields.items() if value is not None}
    text = " ".join(f"{key}={value}" for key, value in shown.items())
    LOGGER.log(level, "%s %s", event, text, extra={"scrape": shown})
