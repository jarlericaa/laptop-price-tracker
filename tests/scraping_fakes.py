"""Offline fakes shared by the scraping framework and adapter tests. No real network or time."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta

import pytest

from laptop_tracker.scraping import TransportResponse

START = datetime(2026, 9, 29, 4, 0, tzinfo=UTC)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test tries to open a real socket. Import it with ``autouse`` wrappers."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("Scraping tests must not use the network.")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return START + timedelta(seconds=self.elapsed)

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds


class FakeTransport:
    """Replays scripted responses or errors and records each call and its (fake) time.

    ``live`` is False by default, so the live-use gate treats it as offline.
    """

    def __init__(
        self, clock: FakeClock, *script: TransportResponse | Exception, live: bool = False
    ) -> None:
        self.clock = clock
        self.script = list(script)
        self.calls: list[dict[str, object]] = []
        self.live = live

    def fetch(self, url, *, timeout, headers, max_bytes) -> TransportResponse:
        self.calls.append(
            {"url": url, "timeout": timeout, "headers": dict(headers), "at": self.clock.elapsed}
        )
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
