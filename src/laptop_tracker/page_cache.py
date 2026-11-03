"""Reuse of successful product-page responses within and across runs (2026-10-26).

Candidate inspection downloads a product page to read its variants; collection then used to
download the same URL again (28 of 28 PC Express and DataBlitz collection requests in the
2026-10-26 run). ``PageCache`` lets ``Fetcher`` answer a request from a response that was already
retrieved for the same resource, with no request sent and no retailer attempt counted.

Identity of a cached page is the retailer, host, path and the *complete query string* (order
ignored, every parameter kept, including ``variant``). So a page fetched for ``?variant=A`` never
answers ``?variant=B``, and a base product page never answers a variant URL: reuse only ever
returns the bytes the exact request would have fetched. Only 2xx responses are cached, and a page
older than ``max_age_seconds`` (when set) is not reused. The reused page keeps its original
retrieval time, status, attempts and content hash, so the observation records when the page was
really read.
"""

from __future__ import annotations

import csv
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .rate_limit import retailer_for_url
from .scraping import FetchedPage, ScrapeRequest

INSPECTION_SOURCE = "REUSED_INSPECTION_PAGE"
SAVED_EVIDENCE_SOURCE = "REUSED_SAVED_EVIDENCE"


def page_key(retailer_id: str, url: str) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
    """Cache identity of a request: retailer, host, path (no trailing slash) and every query pair."""
    parts = urlsplit(url)
    return (
        retailer_for_url(url, retailer_id),
        (parts.hostname or "").lower(),
        parts.path.rstrip("/"),
        tuple(sorted(parse_qsl(parts.query, keep_blank_values=True))),
    )


@dataclass
class _Entry:
    source: str
    retrieved_at: datetime
    loader: Callable[[], FetchedPage]
    page: FetchedPage | None = None


class PageCache:
    """Successful responses by exact request identity. Thread-safe, memory-light for saved files."""

    def __init__(self, max_age_seconds: float | None = None) -> None:
        self.max_age_seconds = max_age_seconds
        self._entries: dict[tuple, _Entry] = {}
        self._lock = threading.Lock()
        self.hits: Counter[str] = Counter()
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)

    def put(self, page: FetchedPage, source: str = INSPECTION_SOURCE) -> None:
        """Remember a page this process fetched. Non-2xx responses are never cached."""
        if not 200 <= page.status_code < 300:
            return
        key = page_key(page.retailer_id, page.requested_url)
        with self._lock:
            self._entries[key] = _Entry(source, page.retrieved_at, lambda: page, page)

    def put_saved(self, retailer_id: str, url: str, retrieved_at: datetime,
                  loader: Callable[[], FetchedPage],
                  source: str = SAVED_EVIDENCE_SOURCE) -> None:
        """Remember a page saved by an earlier run; its body is read from disk on first use."""
        key = page_key(retailer_id, url)
        with self._lock:
            self._entries.setdefault(key, _Entry(source, retrieved_at, loader))

    def get(self, request: ScrapeRequest, now: datetime | None = None) -> FetchedPage | None:
        key = page_key(request.retailer_id, request.url)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if (self.max_age_seconds is not None and now is not None
                    and (now - entry.retrieved_at).total_seconds() > self.max_age_seconds):
                self.misses += 1
                return None
            if entry.page is None:
                entry.page = entry.loader()
            self.hits[entry.source] += 1
            page = entry.page
        return replace(page, requested_url=request.url, reused_from=entry.source)

    def summary(self) -> dict[str, object]:
        return {"entries": len(self._entries), "hits": dict(self.hits), "misses": self.misses}


# Saved inspection evidence of earlier runs

_NO_PAGE_OUTCOMES = frozenset({"FETCH_FAILED", "STEP_FAILED", ""})


def _parse_time(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def add_saved_inspections(
    cache: PageCache, folders: Sequence[Path], *, now: datetime | None = None,
    max_age_hours: float | None = None,
) -> int:
    """Load the product pages that earlier discovery runs saved while inspecting candidates.

    Reads each folder's ``product_inspections.csv`` and its ``evidence/`` bodies (the existing
    evidence architecture: nothing new is written). A failed or unsaved inspection has no body and
    is skipped, so it is retried. The latest copy of a URL wins. Returns the pages registered.
    """
    latest: dict[tuple, tuple[datetime, str, str, Path]] = {}
    for folder in folders:
        path = Path(folder) / "product_inspections.csv"
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                evidence = row.get("evidence_file", "")
                moment = _parse_time(row.get("retrieved_at", ""))
                body = Path(folder) / evidence
                if (row.get("outcome", "") in _NO_PAGE_OUTCOMES or not evidence or moment is None
                        or not body.is_file()):
                    continue
                if (max_age_hours is not None and now is not None
                        and (now - moment).total_seconds() > max_age_hours * 3600):
                    continue
                url, retailer = row.get("product_url", ""), row.get("retailer_id", "")
                key = page_key(retailer, url)
                if url and (key not in latest or moment > latest[key][0]):
                    latest[key] = (moment, retailer, url, body)
    for moment, retailer, url, body in latest.values():
        cache.put_saved(retailer, url, moment, _saved_loader(retailer, url, body, moment))
    return len(latest)


def _saved_loader(retailer: str, url: str, body: Path, moment: datetime):
    def load() -> FetchedPage:
        content_type = ("application/json; charset=utf-8" if body.suffix == ".json"
                        else "text/html; charset=utf-8")
        return FetchedPage(retailer, url, url, 200, body.read_bytes(), content_type, moment, 1)

    return load
