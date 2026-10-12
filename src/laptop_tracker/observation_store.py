"""Durable local storage for offer observations and their matching outcomes (overall Phase 9).

A SQLite file holds every imported ``OfferObservation`` exactly as its ``to_full_record()`` text,
the import batches that delivered it, and the result of matching it under each *matching
context* (a TARGET_MASTER snapshot plus a version of the matching rules). Observations and
matching results are append-only: a changed catalog adds a new context and new results, and
earlier ones are kept. Nothing here fetches pages, reads Google Sheets, or creates targets.

Observation identity is ``(retailer_id, offer_url, offer_index, retrieved_at)``: one offer as
retrieved at one moment. Importing the same record again is a duplicate and is skipped; the same
identity with different content is a conflict, and the whole batch is refused. A later retrieval
of an unchanged offer is a new observation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .matching import MatchResult, MatchStatus
from .offers import OfferObservation, observation_from_record
from .scraping import ScrapeResult

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL,
    source_label TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    received INTEGER NOT NULL,
    new_observations INTEGER NOT NULL,
    duplicates_in_store INTEGER NOT NULL,
    duplicates_in_batch INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    offer_key TEXT NOT NULL,
    retailer_id TEXT NOT NULL,
    offer_url TEXT NOT NULL,
    offer_index INTEGER NOT NULL,
    retrieved_at TEXT NOT NULL,
    retailer_product_id TEXT NOT NULL,
    retailer_sku TEXT NOT NULL,
    price TEXT NOT NULL,
    currency TEXT NOT NULL,
    price_type TEXT NOT NULL,
    regular_price TEXT NOT NULL,
    sale_price TEXT NOT NULL,
    availability TEXT NOT NULL,
    bundle_flag TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    first_batch_id INTEGER NOT NULL REFERENCES import_batches (batch_id)
);
CREATE INDEX IF NOT EXISTS observations_by_offer ON observations (offer_key, retrieved_at);
CREATE TABLE IF NOT EXISTS batch_observations (
    batch_id INTEGER NOT NULL REFERENCES import_batches (batch_id),
    observation_id TEXT NOT NULL REFERENCES observations (observation_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('NEW', 'DUPLICATE')),
    PRIMARY KEY (batch_id, observation_id)
);
CREATE TABLE IF NOT EXISTS matching_contexts (
    context_id TEXT PRIMARY KEY,
    target_master_sha256 TEXT NOT NULL,
    target_master_source TEXT NOT NULL,
    configuration_targets INTEGER NOT NULL,
    listing_only_targets INTEGER NOT NULL,
    matching_rules_version TEXT NOT NULL,
    matching_rules_sha256 TEXT NOT NULL,
    first_used_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match_results (
    observation_id TEXT NOT NULL REFERENCES observations (observation_id),
    context_id TEXT NOT NULL REFERENCES matching_contexts (context_id),
    match_status TEXT NOT NULL,
    target_id TEXT NOT NULL,
    candidate_target_ids TEXT NOT NULL,
    reason_codes TEXT NOT NULL,
    extractor TEXT NOT NULL,
    offer_attributes_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (observation_id, context_id)
);
"""

# History is append-only; corrections are new observations or new matching contexts.
_APPEND_ONLY_TABLES = (
    "observations",
    "batch_observations",
    "import_batches",
    "matching_contexts",
    "match_results",
)


class ObservationStoreError(RuntimeError):
    """The store cannot be opened or used safely."""


class ObservationConflictError(ValueError):
    """A record contradicts one already stored or another in the same batch.

    The batch is refused as a whole and nothing from it is written.
    """


@dataclass(frozen=True)
class ImportReport:
    batch_id: int
    received: int
    new: int
    duplicates_in_store: int
    duplicates_in_batch: int


@dataclass(frozen=True)
class MatchingContext:
    """The catalog snapshot and matching rules an observation was matched under."""

    context_id: str
    target_master_sha256: str
    target_master_source: str
    configuration_targets: int
    listing_only_targets: int
    matching_rules_version: str
    matching_rules_sha256: str

    @classmethod
    def create(
        cls,
        *,
        target_master_sha256: str,
        target_master_source: str,
        configuration_targets: int,
        listing_only_targets: int,
        matching_rules_version: str,
        matching_rules_sha256: str,
    ) -> MatchingContext:
        context_id = _sha256_text(
            "\x1f".join((target_master_sha256, matching_rules_version, matching_rules_sha256))
        )
        return cls(
            context_id,
            target_master_sha256,
            target_master_source,
            configuration_targets,
            listing_only_targets,
            matching_rules_version,
            matching_rules_sha256,
        )


@dataclass(frozen=True)
class StoredObservation:
    observation_id: str
    offer_key: str
    retailer_id: str
    offer_url: str
    offer_index: int
    retrieved_at: datetime
    retailer_product_id: str
    retailer_sku: str
    price: Decimal
    price_text: str
    currency: str
    price_type: str
    regular_price: str
    sale_price: str
    availability: str
    bundle_flag: str
    adapter_version: str
    content_sha256: str
    record: Mapping[str, str]
    first_batch_id: int

    def observation(self) -> OfferObservation:
        """Rebuild and revalidate the original observation from its stored record."""
        return observation_from_record(self.record)


@dataclass(frozen=True)
class StoredMatch:
    observation_id: str
    context_id: str
    match_status: str
    target_id: str
    candidate_target_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    extractor: str


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def offer_key(observation: OfferObservation) -> str:
    """Stable identity of one retailer offer (its URL, including any variant query, and index)."""
    return _sha256_text(
        "\x1f".join((observation.retailer_id, observation.offer_url, str(observation.offer_index)))
    )


def observation_id(observation: OfferObservation) -> str:
    """Deterministic identity of one retrieval of one offer."""
    record = observation.to_record()
    return _sha256_text(
        "\x1f".join(
            (
                record["retailer_id"],
                record["offer_url"],
                record["offer_index"],
                record["retrieved_at"],
            )
        )
    )


def canonical_record(observation: OfferObservation) -> tuple[dict[str, str], str, str]:
    """The full record, its canonical JSON text, and that text's SHA-256.

    The record must rebuild into an equal record, so anything stored can be read back and
    revalidated exactly as it was imported.
    """
    record = observation.to_full_record()
    if observation_from_record(record).to_full_record() != record:
        raise ObservationStoreError("An observation does not round-trip through its record.")
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return record, text, _sha256_text(text)


def _utc_text(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("Times must be timezone-aware.")
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ObservationStore:
    """A local SQLite observation store. Use ``ObservationStore.open(path)`` as a context manager.

    Every write is one transaction: a batch is validated completely before anything is written,
    and any error rolls the whole batch back.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    @classmethod
    @contextmanager
    def open(cls, path: str | Path) -> Iterator[ObservationStore]:
        location = Path(path)
        if str(path) != ":memory:":
            location.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(location, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            store = cls(connection)
            store._initialize()
            yield store
        finally:
            connection.close()

    def _initialize(self) -> None:
        # executescript() commits first, so the idempotent schema runs outside a transaction.
        self._db.executescript(_SCHEMA)
        with self._transaction():
            for table in _APPEND_ONLY_TABLES:
                for action in ("UPDATE", "DELETE"):
                    self._db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                        f"BEFORE {action} ON {table} "
                        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
                    )
            row = self._db.execute(
                "SELECT value FROM store_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO store_meta (key, value) VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            elif row[0] != SCHEMA_VERSION:
                raise ObservationStoreError(
                    f"Store schema version {row[0]} is not supported (expected {SCHEMA_VERSION})."
                )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._db.in_transaction:
            raise ObservationStoreError("Nested store transactions are not supported.")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # Observations

    def import_observations(
        self,
        observations: Iterable[OfferObservation],
        *,
        source_label: str,
        imported_at: datetime,
        source_sha256: str = "",
    ) -> ImportReport:
        """Validate a whole batch, then store its new observations in one transaction.

        Accepts ``OfferObservation`` values as returned by the scraping framework or rebuilt
        from a saved CSV. Exact repeats (in the store or within the batch) are counted, not
        stored again. Any contradiction raises ``ObservationConflictError`` before writing.
        """
        batch: dict[str, tuple[OfferObservation, dict[str, str], str, str]] = {}
        received = 0
        duplicates_in_batch = 0
        for observation in observations:
            if not isinstance(observation, OfferObservation):
                raise TypeError("Only OfferObservation values can be stored.")
            received += 1
            identity = observation_id(observation)
            record, text, digest = canonical_record(observation)
            if identity in batch:
                if batch[identity][3] != digest:
                    raise ObservationConflictError(
                        f"Batch item {received} has the same identity as an earlier item but "
                        "different content; the batch was not stored."
                    )
                duplicates_in_batch += 1
                continue
            batch[identity] = (observation, record, text, digest)
        if not source_sha256:
            source_sha256 = _sha256_text("\n".join(sorted(item[3] for item in batch.values())))

        with self._transaction():
            stored = self._stored_digests(list(batch))
            conflicts = [key for key, item in batch.items() if stored.get(key, item[3]) != item[3]]
            if conflicts:
                raise ObservationConflictError(
                    f"{len(conflicts)} observation(s) contradict stored records with the same "
                    "identity; the batch was not stored."
                )
            new = [key for key in batch if key not in stored]
            cursor = self._db.execute(
                "INSERT INTO import_batches (imported_at, source_label, source_sha256, received, "
                "new_observations, duplicates_in_store, duplicates_in_batch) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _utc_text(imported_at),
                    source_label,
                    source_sha256,
                    received,
                    len(new),
                    len(batch) - len(new),
                    duplicates_in_batch,
                ),
            )
            batch_id = int(cursor.lastrowid or 0)
            for key in new:
                self._insert_observation(key, batch[key], batch_id)
            self._db.executemany(
                "INSERT INTO batch_observations (batch_id, observation_id, outcome) "
                "VALUES (?, ?, ?)",
                [(batch_id, key, "NEW" if key not in stored else "DUPLICATE") for key in batch],
            )
        return ImportReport(
            batch_id, received, len(new), len(batch) - len(new), duplicates_in_batch
        )

    def _stored_digests(self, identities: Sequence[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for start in range(0, len(identities), 500):
            chunk = identities[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for key, digest in self._db.execute(
                f"SELECT observation_id, record_sha256 FROM observations "
                f"WHERE observation_id IN ({marks})",
                chunk,
            ):
                found[key] = digest
        return found

    def _insert_observation(
        self,
        identity: str,
        item: tuple[OfferObservation, dict[str, str], str, str],
        batch_id: int,
    ) -> None:
        observation, record, text, digest = item
        self._db.execute(
            "INSERT INTO observations (observation_id, offer_key, retailer_id, offer_url, "
            "offer_index, retrieved_at, retailer_product_id, retailer_sku, price, currency, "
            "price_type, regular_price, sale_price, availability, bundle_flag, adapter_version, "
            "content_sha256, record_json, record_sha256, first_batch_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identity,
                offer_key(observation),
                record["retailer_id"],
                record["offer_url"],
                observation.offer_index,
                record["retrieved_at"],
                record["retailer_product_id"],
                record["retailer_sku"],
                record["price"],
                record["currency"],
                record["price_type"],
                record["regular_price"],
                record["sale_price"],
                record["availability"],
                record["bundle_flag"],
                record["adapter_version"],
                record["content_sha256"],
                text,
                digest,
                batch_id,
            ),
        )

    def observations(self) -> list[StoredObservation]:
        """Every stored observation, oldest retrieval first (ties by identity)."""
        rows = self._db.execute(
            "SELECT observation_id, offer_key, retailer_id, offer_url, offer_index, retrieved_at, "
            "retailer_product_id, retailer_sku, price, currency, price_type, regular_price, "
            "sale_price, availability, bundle_flag, adapter_version, content_sha256, record_json, "
            "first_batch_id FROM observations ORDER BY retrieved_at, observation_id"
        ).fetchall()
        return [
            StoredObservation(
                observation_id=row[0],
                offer_key=row[1],
                retailer_id=row[2],
                offer_url=row[3],
                offer_index=int(row[4]),
                retrieved_at=datetime.fromisoformat(row[5]),
                retailer_product_id=row[6],
                retailer_sku=row[7],
                price=Decimal(row[8]),
                price_text=row[8],
                currency=row[9],
                price_type=row[10],
                regular_price=row[11],
                sale_price=row[12],
                availability=row[13],
                bundle_flag=row[14],
                adapter_version=row[15],
                content_sha256=row[16],
                record=json.loads(row[17]),
                first_batch_id=int(row[18]),
            )
            for row in rows
        ]

    # Matching contexts and results

    def register_context(self, context: MatchingContext, *, first_used_at: datetime) -> bool:
        """Record a matching context once; returns True when it is new.

        A stored context with the same ID must describe the same catalog and rules.
        """
        with self._transaction():
            row = self._db.execute(
                "SELECT target_master_sha256, matching_rules_version, matching_rules_sha256, "
                "configuration_targets, listing_only_targets FROM matching_contexts "
                "WHERE context_id = ?",
                (context.context_id,),
            ).fetchone()
            expected = (
                context.target_master_sha256,
                context.matching_rules_version,
                context.matching_rules_sha256,
                context.configuration_targets,
                context.listing_only_targets,
            )
            if row is not None:
                if tuple(row) != expected:
                    raise ObservationConflictError(
                        "A stored matching context with this ID records different inputs."
                    )
                return False
            self._db.execute(
                "INSERT INTO matching_contexts (context_id, target_master_sha256, "
                "target_master_source, configuration_targets, listing_only_targets, "
                "matching_rules_version, matching_rules_sha256, first_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    context.context_id,
                    context.target_master_sha256,
                    context.target_master_source,
                    context.configuration_targets,
                    context.listing_only_targets,
                    context.matching_rules_version,
                    context.matching_rules_sha256,
                    _utc_text(first_used_at),
                ),
            )
            return True

    def contexts(self) -> list[dict[str, object]]:
        cursor = self._db.execute(
            "SELECT context_id, target_master_sha256, target_master_source, "
            "configuration_targets, listing_only_targets, matching_rules_version, "
            "matching_rules_sha256, first_used_at FROM matching_contexts "
            "ORDER BY first_used_at, context_id"
        )
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def unmatched_observation_ids(self, context_id: str) -> list[str]:
        """Observations that have no stored result under ``context_id`` yet."""
        return [
            row[0]
            for row in self._db.execute(
                "SELECT observation_id FROM observations WHERE observation_id NOT IN "
                "(SELECT observation_id FROM match_results WHERE context_id = ?) "
                "ORDER BY retrieved_at, observation_id",
                (context_id,),
            )
        ]

    def record_match_results(
        self, context_id: str, results: Sequence[tuple[str, MatchResult]]
    ) -> int:
        """Store results for observations not yet matched under this context, in one transaction.

        An existing result is never replaced: a repeat for the same observation and context
        raises ``ObservationConflictError`` and nothing from the batch is written.
        """
        with self._transaction():
            if self._db.execute(
                "SELECT 1 FROM matching_contexts WHERE context_id = ?", (context_id,)
            ).fetchone() is None:
                raise ObservationStoreError("Register the matching context before its results.")
            for identity, result in results:
                exists = self._db.execute(
                    "SELECT 1 FROM match_results WHERE observation_id = ? AND context_id = ?",
                    (identity, context_id),
                ).fetchone()
                if exists:
                    raise ObservationConflictError(
                        "A matching result already exists for this observation and context."
                    )
                self._db.execute(
                    "INSERT INTO match_results (observation_id, context_id, match_status, "
                    "target_id, candidate_target_ids, reason_codes, extractor, "
                    "offer_attributes_json, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        identity,
                        context_id,
                        result.status.value,
                        result.target_id if result.status is MatchStatus.EXACT else "",
                        ";".join(result.candidate_target_ids),
                        ";".join(result.reason_codes),
                        result.extractor,
                        json.dumps(
                            dict(result.offer_attributes or {}), ensure_ascii=False, sort_keys=True
                        ),
                        json.dumps(_evidence(result), ensure_ascii=False),
                    ),
                )
        return len(results)

    def match_results(self, context_id: str) -> dict[str, StoredMatch]:
        return {
            row[0]: StoredMatch(
                observation_id=row[0],
                context_id=context_id,
                match_status=row[1],
                target_id=row[2],
                candidate_target_ids=tuple(filter(None, row[3].split(";"))),
                reason_codes=tuple(filter(None, row[4].split(";"))),
                extractor=row[5],
            )
            for row in self._db.execute(
                "SELECT observation_id, match_status, target_id, candidate_target_ids, "
                "reason_codes, extractor FROM match_results WHERE context_id = ?",
                (context_id,),
            )
        }

    def counts(self) -> dict[str, int]:
        def count(sql: str) -> int:
            return int(self._db.execute(sql).fetchone()[0])

        return {
            "observations": count("SELECT COUNT(*) FROM observations"),
            "offers": count("SELECT COUNT(DISTINCT offer_key) FROM observations"),
            "import_batches": count("SELECT COUNT(*) FROM import_batches"),
            "matching_contexts": count("SELECT COUNT(*) FROM matching_contexts"),
            "match_results": count("SELECT COUNT(*) FROM match_results"),
        }


def _evidence(result: MatchResult) -> list[dict[str, str]]:
    rows = [
        {
            "stage": "EXTRACTION",
            "field": item.field,
            "status": item.status.value,
            "offer_value": item.value,
            "target_id": "",
            "target_value": "",
            "source_label": item.source_label,
            "source_text": item.source_text,
        }
        for item in result.extraction_evidence
    ]
    rows += [
        {
            "stage": "MATCH",
            "field": item.field,
            "status": item.status.value,
            "offer_value": item.offer_value,
            "target_id": item.target_id,
            "target_value": item.target_value,
            "source_label": "",
            "source_text": "",
        }
        for item in result.evidence
    ]
    return rows


def persist_scrape_results(
    store: ObservationStore,
    results: Iterable[ScrapeResult],
    *,
    imported_at: datetime,
    source_label: str = "scrape()",
) -> tuple[ImportReport, int]:
    """Store the observations from production ``scrape()`` results as one batch.

    Returns the import report and the number of ``ScrapeFailure`` records seen. Failures carry no
    observation and are not stored; the collector should keep its own failure log.
    """
    observations: list[OfferObservation] = []
    failures = 0
    for result in results:
        if not isinstance(result, ScrapeResult):
            raise TypeError("Only ScrapeResult values can be persisted.")
        observations.extend(result.observations)
        failures += len(result.failures)
    report = store.import_observations(
        observations, source_label=source_label, imported_at=imported_at
    )
    return report, failures
