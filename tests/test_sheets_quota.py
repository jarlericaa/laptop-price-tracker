"""The Sheets quota layer: classification, bounded retry, throttle and the worksheet-list cache.

Everything runs against fakes; no network and no real spreadsheet is touched.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import gspread
import pytest
from google.auth.exceptions import RefreshError
from gspread.exceptions import APIError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout

from laptop_tracker import sheets_quota as q
from laptop_tracker import sheets_source

SECRET_ID = "1SECRETspreadsheetIDvalue"
TOKEN = "ya29.SECRET-ACCESS-TOKEN"


class FakeResponse:
    def __init__(self, status: int, body=None, headers=None, text: str = "") -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text or str(body)
        self.ok = 200 <= status < 300

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def api_error(status: int, message: str = "boom", *, reason: str = "", retry_after=None):
    error = {"code": status, "message": message, "status": reason.upper() or "ERR"}
    if reason:
        error["errors"] = [{"reason": reason}]
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return APIError(FakeResponse(status, {"error": error}, headers))


QUOTA_429 = (
    "Quota exceeded for quota metric 'Read requests' and limit "
    "'Read requests per minute per user' of service 'sheets.googleapis.com'"
)


class ZeroJitter:
    """A rng whose jitter is always zero, so delays are exactly the backoff."""

    @staticmethod
    def uniform(low: float, high: float) -> float:
        return low


class MaxJitter:
    @staticmethod
    def uniform(low: float, high: float) -> float:
        return high


def failing(errors, then="ok"):
    """An operation that raises each of ``errors`` in turn and then returns ``then``."""
    queue = list(errors)
    calls = []

    def operation():
        calls.append(1)
        if queue:
            raise queue.pop(0)
        return then

    operation.calls = calls
    return operation


# classification -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (api_error(429, QUOTA_429), q.QUOTA),
        (api_error(403, "Rate limit", reason="rateLimitExceeded"), q.QUOTA),
        (api_error(403, "User rate limit", reason="userRateLimitExceeded"), q.QUOTA),
        (api_error(500), q.SERVER),
        (api_error(502), q.SERVER),
        (api_error(503), q.SERVER),
        (api_error(504), q.SERVER),
        (RequestsConnectionError("reset"), q.NETWORK),
        (Timeout("slow"), q.NETWORK),
        (api_error(400, "Invalid requests[0]"), q.PERMANENT),
        (api_error(401, "Invalid credentials"), q.PERMANENT),
        (api_error(403, "The caller does not have permission", reason="forbidden"), q.PERMANENT),
        (api_error(404, "Requested entity was not found"), q.PERMANENT),
        (RefreshError("invalid_grant"), q.PERMANENT),
        (ValueError("bad data"), q.PERMANENT),
        (KeyError("x"), q.PERMANENT),
    ],
)
def test_errors_are_sorted_into_transient_and_permanent(exc, kind) -> None:
    failure = q.classify(exc)
    assert failure.kind == kind
    assert failure.transient == (kind != q.PERMANENT)


def test_an_exhausted_retry_is_never_retried_again_by_an_outer_layer() -> None:
    inner = failing([api_error(429, QUOTA_429)] * 10)
    with pytest.raises(q.SheetsRetryExhausted) as caught:
        q.call_with_retry(
            inner, description="x", idempotent=True, sleep=lambda s: None, rng=ZeroJitter
        )
    outer = failing([caught.value], then="never")
    with pytest.raises(q.SheetsRetryExhausted):
        q.call_with_retry(
            outer, description="y", idempotent=True, sleep=lambda s: None, rng=ZeroJitter
        )
    assert len(outer.calls) == 1


def test_retry_after_accepts_seconds_or_an_http_date_and_ignores_nonsense() -> None:
    now = datetime(2026, 11, 12, 3, 0, 0, tzinfo=UTC)
    assert q.parse_retry_after("7") == 7.0
    assert q.parse_retry_after(" 0 ") == 0.0
    assert q.parse_retry_after("-5") == 0.0
    later = (now + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert q.parse_retry_after(later, now=now) == pytest.approx(30.0)
    earlier = (now - timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert q.parse_retry_after(earlier, now=now) == 0.0
    assert q.parse_retry_after("soon") is None
    assert q.parse_retry_after("") is None
    assert q.parse_retry_after(None) is None
    assert q.classify(api_error(429, QUOTA_429, retry_after=12)).retry_after == 12.0


# the retry policy -----------------------------------------------------------------------------


def test_a_quota_error_is_retried_with_exponential_backoff_until_it_clears() -> None:
    operation = failing([api_error(429, QUOTA_429)] * 3)
    sleeps: list[float] = []
    result = q.call_with_retry(
        operation, description="read", idempotent=True, sleep=sleeps.append, rng=ZeroJitter
    )
    assert result == "ok" and len(operation.calls) == 4
    assert sleeps == [4.0, 8.0, 16.0]


def test_the_backoff_is_capped_and_the_attempts_are_bounded() -> None:
    operation = failing([api_error(429, QUOTA_429)] * 50)
    sleeps: list[float] = []
    with pytest.raises(q.SheetsRetryExhausted) as caught:
        q.call_with_retry(
            operation,
            description="Sheets GET /v4/spreadsheets/<id>",
            idempotent=True,
            sleep=sleeps.append,
            rng=ZeroJitter,
        )
    policy = q.DEFAULT_POLICY
    assert len(operation.calls) == policy.max_attempts == 6
    assert sleeps == [4.0, 8.0, 16.0, 32.0, 60.0]  # the last one capped at max_delay
    assert sum(sleeps) <= policy.max_total_wait
    error = caught.value
    assert (error.attempts, error.failure.status) == (6, 429)
    message = str(error)
    assert "6 attempt(s)" in message and "HTTP 429" in message and "Quota exceeded" in message
    assert isinstance(error.__cause__, APIError)


def test_jitter_is_bounded_and_never_shortens_the_backoff() -> None:
    policy = q.DEFAULT_POLICY
    rng = random.Random(2026)
    for retry in range(1, 6):
        raw = min(policy.max_delay, policy.base_delay * policy.multiplier ** (retry - 1))
        delays = {policy.backoff(retry, rng) for _ in range(200)}
        assert len(delays) > 1  # actually jittered
        assert all(raw <= d <= raw * (1 + policy.jitter) for d in delays)


def test_retry_after_is_a_minimum_wait() -> None:
    operation = failing([api_error(429, QUOTA_429, retry_after=45)])
    sleeps: list[float] = []
    q.call_with_retry(
        operation, description="read", idempotent=True, sleep=sleeps.append, rng=ZeroJitter
    )
    assert sleeps == [45.0]  # longer than the 4 s backoff
    sleeps.clear()
    q.call_with_retry(
        failing([api_error(429, QUOTA_429, retry_after=1)]),
        description="read",
        idempotent=True,
        sleep=sleeps.append,
        rng=ZeroJitter,
    )
    assert sleeps == [4.0]  # the backoff wins when it is longer


def test_a_retry_after_beyond_the_limit_fails_at_once_instead_of_sleeping() -> None:
    operation = failing([api_error(429, QUOTA_429, retry_after=3600)])
    sleeps: list[float] = []
    with pytest.raises(q.SheetsRetryExhausted, match="asked to wait 3600s"):
        q.call_with_retry(
            operation, description="read", idempotent=True, sleep=sleeps.append, rng=ZeroJitter
        )
    assert sleeps == [] and len(operation.calls) == 1


def test_the_total_wait_budget_is_enforced() -> None:
    policy = q.RetryPolicy(max_attempts=10, base_delay=30, max_delay=60, max_total_wait=100)
    operation = failing([api_error(429, QUOTA_429)] * 20)
    sleeps: list[float] = []
    with pytest.raises(q.SheetsRetryExhausted, match="retry budget"):
        q.call_with_retry(
            operation,
            description="read",
            idempotent=True,
            policy=policy,
            sleep=sleeps.append,
            rng=ZeroJitter,
        )
    assert sum(sleeps) <= 100 and len(operation.calls) < 10


@pytest.mark.parametrize(
    "error",
    [
        api_error(401, "Invalid credentials"),
        api_error(403, "The caller does not have permission", reason="forbidden"),
        api_error(400, "Invalid requests[0].updateCells"),
        api_error(404, "Requested entity was not found"),
        RefreshError("invalid_grant"),
        ValueError("bad"),
    ],
)
@pytest.mark.parametrize("idempotent", [True, False])
def test_authentication_permission_and_validation_errors_are_never_retried(
    error, idempotent
) -> None:
    operation = failing([error])
    sleeps: list[float] = []
    with pytest.raises(type(error)) as caught:
        q.call_with_retry(
            operation,
            description="op",
            idempotent=idempotent,
            sleep=sleeps.append,
            rng=ZeroJitter,
        )
    assert caught.value is error and len(operation.calls) == 1 and sleeps == []


def test_reads_are_retried_after_server_and_network_errors() -> None:
    operation = failing([api_error(503), RequestsConnectionError("reset"), api_error(500)])
    sleeps: list[float] = []
    assert (
        q.call_with_retry(
            operation, description="read", idempotent=True, sleep=sleeps.append, rng=ZeroJitter
        )
        == "ok"
    )
    assert len(operation.calls) == 4


@pytest.mark.parametrize("error", [api_error(500), api_error(503), RequestsConnectionError("x")])
def test_a_write_is_not_repeated_after_an_outcome_unknown_failure(error) -> None:
    operation = failing([error])
    with pytest.raises(type(error)):
        q.call_with_retry(
            operation,
            description="write",
            idempotent=False,
            sleep=lambda s: None,
            rng=ZeroJitter,
        )
    assert len(operation.calls) == 1


def test_a_write_rejected_for_quota_is_retried_because_google_never_ran_it() -> None:
    operation = failing([api_error(429, QUOTA_429), api_error(429, QUOTA_429)])
    sleeps: list[float] = []
    assert (
        q.call_with_retry(
            operation, description="write", idempotent=False, sleep=sleeps.append, rng=ZeroJitter
        )
        == "ok"
    )
    assert len(operation.calls) == 3 and sleeps == [4.0, 8.0]


def test_retries_are_logged_without_credentials_or_the_spreadsheet_id(caplog) -> None:
    caplog.set_level(logging.INFO, logger="laptop_tracker.sheets_quota")
    description = f"Sheets GET {q.redact_endpoint(f'https://sheets.googleapis.com/v4/spreadsheets/{SECRET_ID}?key={TOKEN}')}"
    q.call_with_retry(
        failing([api_error(429, QUOTA_429)]),
        description=description,
        idempotent=True,
        sleep=lambda s: None,
        rng=ZeroJitter,
    )
    text = caplog.text
    assert "retry 1 of 5 in 4.0s" in text and "HTTP 429" in text
    assert SECRET_ID not in text and TOKEN not in text and "<id>" in text


def test_endpoints_are_redacted() -> None:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SECRET_ID}:batchUpdate?x=1"
    assert q.redact_endpoint(url) == "/v4/spreadsheets/<id>:batchUpdate"
    assert q.redact_endpoint(f"/v4/spreadsheets/{SECRET_ID}/values/A1") == (
        "/v4/spreadsheets/<id>/values/A1"
    )


# the throttle ---------------------------------------------------------------------------------


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_the_throttle_never_lets_more_than_the_limit_through_in_a_window() -> None:
    clock = VirtualClock()
    throttle = q.RequestThrottle(3, 60.0, clock=clock, sleep=clock.sleep)
    stamps = []
    for _ in range(10):
        throttle.acquire()
        stamps.append(clock.now)
    assert stamps == [0, 0, 0, 60, 60, 60, 120, 120, 120, 180]
    for i, start in enumerate(stamps):
        assert sum(1 for s in stamps[i:] if s - start < 60) <= 3
    assert throttle.total_waited == pytest.approx(180)


def test_the_throttle_lets_requests_through_without_delay_under_the_limit() -> None:
    clock = VirtualClock()
    throttle = q.RequestThrottle(40, 60.0, clock=clock, sleep=clock.sleep)
    for _ in range(40):
        assert throttle.acquire() == 0
    assert clock.sleeps == []


def test_throttle_limits_come_from_the_environment_with_safe_defaults(monkeypatch) -> None:
    monkeypatch.delenv("SHEETS_READS_PER_MINUTE", raising=False)
    monkeypatch.delenv("SHEETS_WRITES_PER_MINUTE", raising=False)
    guard = q.QuotaGuard()
    assert (guard.reads.max_requests, guard.writes.max_requests) == (40, 40)  # < Google's 60
    monkeypatch.setenv("SHEETS_READS_PER_MINUTE", "20")
    monkeypatch.setenv("SHEETS_WRITES_PER_MINUTE", "not a number")
    guard = q.QuotaGuard()
    assert (guard.reads.max_requests, guard.writes.max_requests) == (20, 40)
    monkeypatch.setenv("SHEETS_READS_PER_MINUTE", "0")
    assert q.QuotaGuard().reads.max_requests == 40


# the HTTP layer -------------------------------------------------------------------------------


class FakeSession:
    """Scripted ``requests.Session``: answers each call from ``script``; records every call."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    def request(self, method, url, **kwargs):
        self.calls.append((method.upper(), url))
        answer = self.script.pop(0) if self.script else FakeResponse(200, {})
        return answer


def error_response(status: int, message: str = "boom", headers=None, reason: str = ""):
    error = {"code": status, "message": message}
    if reason:
        error["errors"] = [{"reason": reason}]
    return FakeResponse(status, {"error": error}, headers)


METADATA = {
    "properties": {"title": "Fake"},
    "sheets": [
        {"properties": {"sheetId": 1, "title": "A"}},
        {"properties": {"sheetId": 2, "title": "B"}},
    ],
}


def make_client(script, **guard_kwargs):
    clock = VirtualClock()
    guard = q.QuotaGuard(
        reads_per_minute=guard_kwargs.pop("reads", 1000),
        writes_per_minute=guard_kwargs.pop("writes", 1000),
        clock=clock,
        sleep=clock.sleep,
        rng=ZeroJitter,
    )
    session = FakeSession(script)
    client = gspread.Client(None, session=session, http_client=q.QuotaAwareHTTPClient)
    client.http_client._guard = guard
    return client, session, guard, clock


def open_fake(client, session):
    session.script.insert(0, FakeResponse(200, METADATA))  # Spreadsheet.__init__ metadata read
    return gspread.Spreadsheet(client.http_client, {"id": SECRET_ID})


def test_every_gspread_call_is_retried_through_the_http_layer(caplog) -> None:
    caplog.set_level(logging.INFO, logger="laptop_tracker.sheets_quota")
    client, session, guard, clock = make_client([])
    spreadsheet = open_fake(client, session)
    session.script += [error_response(429, QUOTA_429), error_response(429, QUOTA_429)]
    session.script.append(FakeResponse(200, METADATA))
    titles = [ws.title for ws in spreadsheet.worksheets()]
    assert titles == ["A", "B"]
    assert clock.sleeps == [4.0, 8.0] and guard.retries == 2
    assert [c[0] for c in session.calls] == ["GET"] * 4  # open + two refused + the success
    assert SECRET_ID not in caplog.text and TOKEN not in caplog.text


def test_a_write_rejected_for_quota_is_retried_but_other_write_failures_are_not() -> None:
    client, session, guard, clock = make_client([])
    spreadsheet = open_fake(client, session)
    session.calls.clear()
    session.script += [error_response(429, QUOTA_429), FakeResponse(200, {"replies": []})]
    assert spreadsheet.batch_update({"requests": []}) == {"replies": []}
    assert [c[0] for c in session.calls] == ["POST", "POST"] and clock.sleeps == [4.0]

    session.calls.clear()
    session.script += [error_response(503, "backend error")]
    with pytest.raises(APIError, match="503"):
        spreadsheet.batch_update({"requests": []})
    assert len(session.calls) == 1  # a write with an unknown outcome is never repeated


def test_permission_and_authentication_failures_surface_after_one_request() -> None:
    for status, reason in ((401, ""), (403, "forbidden"), (400, ""), (404, "")):
        client, session, guard, clock = make_client([])
        spreadsheet = open_fake(client, session)
        session.calls.clear()
        session.script += [error_response(status, "denied", reason=reason)]
        with pytest.raises(APIError) as caught:
            spreadsheet.worksheets()
        assert caught.value.code == status
        assert len(session.calls) == 1 and clock.sleeps == []


def test_a_quota_that_never_clears_fails_clearly_after_the_bounded_attempts() -> None:
    client, session, guard, clock = make_client([])
    spreadsheet = open_fake(client, session)
    session.calls.clear()
    session.script += [error_response(429, QUOTA_429)] * 20
    with pytest.raises(q.SheetsRetryExhausted) as caught:
        spreadsheet.worksheets()
    assert len(session.calls) == q.DEFAULT_POLICY.max_attempts
    assert "429" in str(caught.value) and "6 attempt(s)" in str(caught.value)


def test_the_retry_after_header_of_a_response_is_honoured() -> None:
    client, session, guard, clock = make_client([])
    spreadsheet = open_fake(client, session)
    session.script += [
        error_response(429, QUOTA_429, headers={"Retry-After": "30"}),
        FakeResponse(200, METADATA),
    ]
    spreadsheet.worksheets()
    assert clock.sleeps == [30.0]


def test_requests_are_spread_out_so_a_burst_cannot_hit_the_quota() -> None:
    client, session, guard, clock = make_client([], reads=5)
    spreadsheet = open_fake(client, session)  # one read
    for _ in range(11):
        session.script.append(FakeResponse(200, METADATA))
        spreadsheet.worksheets()
    assert guard.requests["read"] == 12
    # 12 reads at 5 per minute need at least two full waits; none of the waits was a 429 retry.
    assert sum(clock.sleeps) >= 120 and guard.retries == 0


def test_reads_and_writes_are_counted_and_throttled_separately() -> None:
    client, session, guard, clock = make_client([], reads=2, writes=2)
    spreadsheet = open_fake(client, session)
    for _ in range(2):
        session.script.append(FakeResponse(200, {}))
        spreadsheet.batch_update({"requests": []})
    assert guard.requests == {"read": 1, "write": 2} and clock.sleeps == []


def test_the_connection_uses_the_quota_aware_client_and_a_cached_spreadsheet(monkeypatch) -> None:
    seen = {}

    def fake_authorize(credentials, **kwargs):
        seen.update(kwargs)

        class FakeClient:
            def open_by_key(self, key):
                return object()

        return FakeClient()

    monkeypatch.setattr(gspread, "authorize", fake_authorize)
    monkeypatch.setattr(sheets_source, "load_credentials", lambda settings, read_only=True: None)

    class Settings:
        def require_sheet_access(self):
            return None

    client = sheets_source.connect(Settings(), read_only=True)
    assert seen["http_client"] is q.QuotaAwareHTTPClient
    assert isinstance(sheets_source.open_spreadsheet(client, "abc"), q.CachedSpreadsheet)


# the worksheet-list cache ---------------------------------------------------------------------


class Ws:
    def __init__(self, title: str) -> None:
        self.title = title


class InnerSpreadsheet:
    id = "inner-id"
    title = "Inner"

    def __init__(self, titles) -> None:
        self.titles = list(titles)
        self.listings = 0
        self.batches: list[dict] = []
        self.fail_batch = False

    def worksheets(self, exclude_hidden=False):
        self.listings += 1
        return [Ws(t) for t in self.titles]

    def batch_update(self, body):
        self.batches.append(body)
        if self.fail_batch:
            raise RuntimeError("boom")
        return {}

    def add_worksheet(self, title, rows, cols):
        self.titles.append(title)
        return Ws(title)

    def del_worksheet(self, ws):
        self.titles.remove(ws.title)

    def fetch_sheet_metadata(self, params=None):
        return {"params": params}


def test_repeated_worksheet_lookups_make_one_metadata_request() -> None:
    inner = InnerSpreadsheet(["A", "B", "C"])
    sheet = q.CachedSpreadsheet(inner)
    for _ in range(25):
        assert q.find_worksheet(sheet, "B").title == "B"
        assert len(sheet.worksheets()) == 3
    assert inner.listings == 1 and sheet.listing_requests == 1


def test_a_new_phase_lists_the_worksheets_again() -> None:
    inner = InnerSpreadsheet(["A"])
    sheet = q.cached(inner)
    sheet.worksheets()
    assert q.cached(sheet) is sheet  # idempotent: wrapping twice keeps one cache
    inner.titles.append("B")  # changed behind the cache's back
    assert [w.title for w in sheet.worksheets()] == ["A"]  # within the phase: the cached listing
    assert [w.title for w in q.cached(sheet, new_phase=True).worksheets()] == ["A", "B"]
    assert inner.listings == 2


def test_the_listing_is_dropped_when_worksheets_are_created_or_deleted() -> None:
    inner = InnerSpreadsheet(["A"])
    sheet = q.CachedSpreadsheet(inner)
    sheet.worksheets()
    sheet.add_worksheet("NEW", 10, 10)
    assert "NEW" in [w.title for w in sheet.worksheets()] and inner.listings == 2
    sheet.del_worksheet(Ws("NEW"))
    assert [w.title for w in sheet.worksheets()] == ["A"] and inner.listings == 3


def test_a_cell_write_keeps_the_listing_but_a_structural_batch_drops_it() -> None:
    inner = InnerSpreadsheet(["A"])
    sheet = q.CachedSpreadsheet(inner)
    sheet.worksheets()
    content = {
        "requests": [
            {"updateCells": {"range": {"sheetId": 1}, "fields": "userEnteredValue"}},
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": 1, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {"updateDimensionProperties": {"fields": "pixelSize"}},
        ]
    }
    sheet.batch_update(content)
    sheet.worksheets()
    assert inner.listings == 1 and len(inner.batches) == 1
    for structural in (
        {"addSheet": {"properties": {"title": "X"}}},
        {"deleteSheet": {"sheetId": 1}},
        {"duplicateSheet": {"sourceSheetId": 1}},
        {"updateSheetProperties": {"properties": {"sheetId": 1, "title": "Z"}, "fields": "title"}},
        {
            "updateSheetProperties": {
                "properties": {"sheetId": 1, "hidden": True},
                "fields": "hidden",
            }
        },
        {"updateSheetProperties": {"properties": {"sheetId": 1}, "fields": "*"}},
    ):
        before = inner.listings
        sheet.batch_update({"requests": [structural]})
        sheet.worksheets()
        assert inner.listings == before + 1, structural


def test_a_failed_structural_batch_also_drops_the_listing() -> None:
    inner = InnerSpreadsheet(["A"])
    sheet = q.CachedSpreadsheet(inner)
    sheet.worksheets()
    inner.fail_batch = True
    with pytest.raises(RuntimeError):
        sheet.batch_update({"requests": [{"addSheet": {"properties": {"title": "X"}}}]})
    sheet.worksheets()
    assert inner.listings == 2  # the outcome is unknown, so the next lookup is fresh


def test_a_tab_the_old_cached_listing_lacks_is_looked_up_afresh_before_it_is_called_missing() -> (
    None
):
    inner = InnerSpreadsheet(["A"])
    clock = VirtualClock()
    sheet = q.CachedSpreadsheet(inner, clock=clock)
    sheet.worksheets()
    inner.titles.append("LATE")  # created by someone else after the listing

    # Within the same moment the listing is trusted: no extra request for a genuine miss.
    assert q.find_worksheet(sheet, "LATE") is None and inner.listings == 1
    clock.now += q.STALE_LISTING_SECONDS + 1  # the listing is now old
    assert q.find_worksheet(sheet, "LATE").title == "LATE" and inner.listings == 2
    assert q.find_worksheet(sheet, "NOPE") is None and inner.listings == 2  # fresh again: no more
    assert q.find_worksheet(inner, "A").title == "A"  # an uncached spreadsheet still works


def test_everything_but_the_listing_passes_straight_through() -> None:
    inner = InnerSpreadsheet(["A"])
    sheet = q.CachedSpreadsheet(inner)
    assert (sheet.id, sheet.title) == ("inner-id", "Inner")
    assert sheet.fetch_sheet_metadata(params={"ranges": "'A'"}) == {"params": {"ranges": "'A'"}}
    assert sheet.fetch_sheet_metadata(params={"ranges": "'A'"}) == {"params": {"ranges": "'A'"}}
    assert inner.listings == 0  # tab contents are never cached


# the project's policy about the layer ---------------------------------------------------------


def test_requirements_bound_the_gspread_series_the_http_client_subclass_relies_on() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("requirements.txt", "pyproject.toml"):
        text = (root / name).read_text()
        assert re.search(r"gspread>=6\.1,<7", text), name
    assert gspread.__version__.split(".")[0] == "6"


def test_no_publisher_module_keeps_its_own_ad_hoc_quota_retry() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "laptop_tracker"
    for name in ("ci_publish.py", "variation_publication.py", "sheet_publication.py"):
        text = (src / name).read_text()
        assert "RETRY_DELAYS" not in text and "READ_RETRY_SECONDS" not in text, name
        assert '"429" in' not in text, name
