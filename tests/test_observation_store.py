"""Phase 9 observation store: identity, idempotent imports, conflicts, rollback. Synthetic only."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from scraping_fakes import FakeClock, FakeTransport, no_network  # noqa: F401
from test_extraction import FIXTURE_PAGES, FIXTURES, HTML

from laptop_tracker.matching import ExactMatcher, MatchStatus
from laptop_tracker.observation_store import (
    MatchingContext,
    ObservationConflictError,
    ObservationStore,
    ObservationStoreError,
    observation_id,
    offer_key,
    persist_scrape_results,
)
from laptop_tracker.offers import (
    Availability,
    OfferDetails,
    OfferObservation,
    PriceType,
    RetrievalMetadata,
)
from laptop_tracker.scraping import (
    FetchConfig,
    Fetcher,
    ScrapeFailure,
    ScrapeRequest,
    ScrapeResult,
    Stage,
    TransportResponse,
    scrape,
)

pytestmark = pytest.mark.usefixtures("no_network")

NOW = datetime(2026, 10, 12, 1, 0, tzinfo=UTC)
# Wednesday 2026-10-07 12:00 in Asia/Singapore, inside the week starting Monday 2026-10-05.
T0 = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)
HOSTS = {
    "laptop_factory": "laptopfactory.com.ph",
    "datablitz": "ecommerce.datablitz.com.ph",
    "pcx": "pcx.com.ph",
    "pcc": "pccentralcomputerstore.com",
    "oct": "www.octagon.com.ph",
}

# Invented Laptop Factory-style specification rows for invented targets.
MODELS = {
    "EB14-0001": (
        ("Model Name", "ExampleBrand ExampleBook 14 EB14-0001 (Example Blue)"),
        ("Processor", "Intel® Core™ 5 processor 210H"),
        ("RAM", "16GB DDR5"),
        ("Storage", "512GB PCIe Gen4 NVMe SSD"),
        ("Display Size", "14″ Full HD 1920×1080 IPS"),
    ),
    "EB15-0002": (
        ("Model Name", "ExampleBrand ExampleBook 15 EB15-0002"),
        ("Processor", "Intel® Core™ 7 processor 240H"),
        ("RAM", "16GB DDR5"),
        ("Storage", "1TB PCIe Gen4 NVMe SSD"),
        ("Display Size", "15.6″ Full HD 1920×1080 IPS"),
    ),
    "EB16-0003": (
        ("Model Name", "ExampleBrand ExampleBook 16 EB16-0003"),
        ("Processor", "Intel® Core™ 7 processor 240H"),
        ("RAM", "32GB DDR5"),
        ("Storage", "1TB PCIe Gen4 NVMe SSD"),
        ("Display Size", "16″ WUXGA 1920×1200 IPS"),
    ),
}


def offer(
    model: str = "EB14-0001",
    *,
    retailer: str = "laptop_factory",
    slug: str | None = None,
    index: int = 0,
    at: datetime = T0,
    price: str = "1000.00",
    currency: str = "PHP",
    availability: Availability = Availability.IN_STOCK,
    bundle: bool | None = False,
    price_type: PriceType | None = PriceType.REGULAR,
    specs: tuple[tuple[str, str], ...] | None = None,
    content: str = "0",
) -> OfferObservation:
    """One synthetic observation; the URL is built from the model unless ``slug`` is given."""
    url = f"https://{HOSTS[retailer]}/product/{slug or model.lower()}/"
    amount = Decimal(price)
    sale = price_type is PriceType.SALE
    return OfferObservation(
        retailer_id=retailer,
        offer_url=url,
        retrieved_at=at,
        raw_title=f"Synthetic {model}",
        raw_configuration_text="",
        price=amount,
        currency=currency,
        raw_price_text=f"{currency} {price}",
        availability=availability,
        availability_evidence="" if availability is Availability.UNKNOWN else "synthetic",
        offer_index=index,
        source=RetrievalMetadata(
            requested_url=url,
            final_url=url,
            http_status=200,
            attempts=1,
            content_sha256=content * 64,
            content_type="text/html",
            adapter_version="fixture-1",
        ),
        details=OfferDetails(
            price_type=price_type,
            regular_price=amount + 100 if sale else (amount if price_type else None),
            regular_price_text="regular" if price_type else "",
            sale_price=amount if sale else None,
            sale_price_text="sale" if sale else "",
            raw_specifications=MODELS.get(model, ()) if specs is None else specs,
            bundle_flag=bundle,
            bundle_evidence=("Free bag",) if bundle else (),
        ),
    )


@pytest.fixture
def store(tmp_path: Path):
    with ObservationStore.open(tmp_path / "history" / "store.sqlite3") as opened:
        yield opened


def context(target_master_sha256: str = "a" * 64, **overrides) -> MatchingContext:
    values = dict(
        target_master_sha256=target_master_sha256,
        target_master_source="synthetic.csv",
        configuration_targets=1,
        listing_only_targets=0,
        matching_rules_version="test-rules",
        matching_rules_sha256="b" * 64,
    )
    return MatchingContext.create(**(values | overrides))


# Identity


def test_identity_is_the_offer_at_one_retrieval_time() -> None:
    first = offer()
    assert observation_id(first) == observation_id(offer())
    assert offer_key(first) == offer_key(offer(at=T0 + timedelta(days=1)))
    # A later retrieval is a new observation even with the same price.
    assert observation_id(first) != observation_id(offer(at=T0 + timedelta(seconds=1)))
    # Offer URL (including a variant) and offer index are part of the offer's identity.
    assert offer_key(first) != offer_key(offer(slug="eb14-0001?variant=1"))
    assert offer_key(first) != offer_key(offer(index=1))
    # Content is not identity: a different page body at the same moment is a contradiction.
    assert observation_id(first) == observation_id(offer(content="1"))


# Imports


def test_importing_the_same_observations_twice_does_not_duplicate_history(store) -> None:
    batch = [offer(), offer("EB15-0002")]
    first = store.import_observations(batch, source_label="a.csv", imported_at=NOW)
    second = store.import_observations(batch, source_label="a.csv", imported_at=NOW)

    assert (first.received, first.new, first.duplicates_in_store) == (2, 2, 0)
    assert (second.received, second.new, second.duplicates_in_store) == (2, 0, 2)
    assert store.counts() == {
        "observations": 2,
        "offers": 2,
        "import_batches": 2,
        "matching_contexts": 0,
        "match_results": 0,
    }
    outcomes = store._db.execute(
        "SELECT batch_id, outcome, COUNT(*) FROM batch_observations GROUP BY 1, 2 ORDER BY 1"
    ).fetchall()
    assert outcomes == [(first.batch_id, "NEW", 2), (second.batch_id, "DUPLICATE", 2)]


def test_a_later_retrieval_with_an_unchanged_price_is_a_new_observation(store) -> None:
    store.import_observations([offer()], source_label="a", imported_at=NOW)
    report = store.import_observations(
        [offer(at=T0 + timedelta(hours=1))], source_label="b", imported_at=NOW
    )
    assert report.new == 1
    assert store.counts()["observations"] == 2
    assert store.counts()["offers"] == 1


def test_exact_repeats_inside_one_batch_are_stored_once(store) -> None:
    report = store.import_observations([offer(), offer()], source_label="a", imported_at=NOW)
    assert (report.received, report.new, report.duplicates_in_batch) == (2, 1, 1)


@pytest.mark.parametrize(
    "changed",
    [
        {"price": "999.00"},
        {"availability": Availability.OUT_OF_STOCK},
        {"content": "1"},
        {"bundle": True},
    ],
)
def test_a_contradicting_record_is_refused_and_nothing_is_written(store, changed) -> None:
    store.import_observations([offer()], source_label="a", imported_at=NOW)
    before = store.counts()
    with pytest.raises(ObservationConflictError):
        store.import_observations(
            [offer("EB15-0002"), offer(**changed)], source_label="b", imported_at=NOW
        )
    # The whole batch was refused, including its valid new observation, and no batch row exists.
    assert store.counts() == before
    [stored] = store.observations()
    assert stored.price_text == "1000.00"
    assert stored.availability == "IN_STOCK"


def test_a_contradiction_inside_one_batch_is_refused(store) -> None:
    with pytest.raises(ObservationConflictError):
        store.import_observations(
            [offer(), offer(price="999.00")], source_label="a", imported_at=NOW
        )
    assert store.counts()["observations"] == 0
    assert store.counts()["import_batches"] == 0


def test_a_failure_part_way_through_a_batch_rolls_back_everything(store) -> None:
    store.import_observations([offer()], source_label="a", imported_at=NOW)
    before = store.counts()
    real_insert = ObservationStore._insert_observation
    calls = []

    def failing_insert(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error (simulated)")
        return real_insert(self, *args, **kwargs)

    with patch.object(ObservationStore, "_insert_observation", failing_insert):
        with pytest.raises(sqlite3.OperationalError):
            store.import_observations(
                [offer("EB15-0002"), offer("EB16-0003")], source_label="b", imported_at=NOW
            )
    assert len(calls) == 2
    assert store.counts() == before
    assert [s.offer_url for s in store.observations()] == [offer().offer_url]
    # The store is usable afterwards.
    assert store.import_observations([offer("EB15-0002")], source_label="c", imported_at=NOW).new


def test_only_offer_observations_are_accepted(store) -> None:
    with pytest.raises(TypeError):
        store.import_observations([offer().to_full_record()], source_label="a", imported_at=NOW)
    assert store.counts()["import_batches"] == 0


def test_history_is_append_only(store) -> None:
    store.import_observations([offer()], source_label="a", imported_at=NOW)
    for statement in (
        "UPDATE observations SET price = '1.00'",
        "DELETE FROM observations",
        "DELETE FROM import_batches",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store._db.execute(statement)
    assert store.observations()[0].price_text == "1000.00"


def test_stored_records_keep_exact_decimals_and_rebuild_the_observation(store) -> None:
    original = offer(price="45999.99", price_type=PriceType.SALE)
    store.import_observations([original], source_label="a", imported_at=NOW)
    [stored] = store.observations()
    assert stored.price == Decimal("45999.99") and stored.price_text == "45999.99"
    assert (stored.price_type, stored.regular_price, stored.sale_price) == (
        "SALE",
        "46099.99",
        "45999.99",
    )
    assert stored.observation().to_full_record() == original.to_full_record()
    assert stored.retrieved_at == T0


def test_the_store_persists_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite3"
    with ObservationStore.open(path) as first:
        first.import_observations([offer()], source_label="a", imported_at=NOW)
    with ObservationStore.open(path) as second:
        assert second.counts()["observations"] == 1
        assert second.import_observations([offer()], source_label="a", imported_at=NOW).new == 0


def test_an_unknown_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite3"
    with ObservationStore.open(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM store_meta")
    connection.execute("INSERT INTO store_meta VALUES ('schema_version', '999')")
    connection.commit()
    connection.close()
    with pytest.raises(ObservationStoreError, match="schema version"):
        with ObservationStore.open(path):
            pass


# Production scrape results


def test_scrape_results_are_persisted_directly(store) -> None:
    adapter_class, path, url = FIXTURE_PAGES["lf_sale"]
    clock = FakeClock()
    transport = FakeTransport(
        clock, TransportResponse(200, url, (FIXTURES / path).read_bytes(), HTML)
    )
    adapter = adapter_class()
    results = scrape(
        adapter, [ScrapeRequest(adapter.retailer_id, url)], Fetcher(transport, FetchConfig(), clock)
    )
    failed = ScrapeResult(
        ScrapeRequest(adapter.retailer_id, url),
        (),
        (
            ScrapeFailure(
                adapter.retailer_id, url, Stage.FETCH, "HTTP_STATUS", "synthetic", False, 1, 404
            ),
        ),
    )

    report, failures = persist_scrape_results(store, [*results, failed], imported_at=NOW)
    again, _ = persist_scrape_results(store, results, imported_at=NOW)

    assert (report.new, failures, again.new, again.duplicates_in_store) == (1, 1, 0, 1)
    [stored] = store.observations()
    assert stored.observation().to_full_record() == results[0].observations[0].to_full_record()
    assert stored.adapter_version == adapter.adapter_version


# Matching contexts and results


def matcher() -> ExactMatcher:
    return ExactMatcher(
        [
            {
                "target_id": "TGT-000001",
                "status": "ACTIVE",
                "model": "EB14-0001",
                "screen_size_in": "14",
                "cpu_series": "Core 5",
                "ram_capacity_gb": "16",
                "storage_capacity_gb": "512",
            }
        ]
    )


def test_results_are_kept_per_context_and_never_replaced(store) -> None:
    store.import_observations([offer(), offer("EB15-0002")], source_label="a", imported_at=NOW)
    first = context()
    assert store.register_context(first, first_used_at=NOW) is True
    assert store.register_context(first, first_used_at=NOW + timedelta(days=1)) is False
    pending = store.unmatched_observation_ids(first.context_id)
    by_id = {s.observation_id: s for s in store.observations()}
    results = [(key, matcher().match_observation(by_id[key].observation())) for key in pending]
    assert store.record_match_results(first.context_id, results) == 2
    assert store.unmatched_observation_ids(first.context_id) == []

    with pytest.raises(ObservationConflictError):
        store.record_match_results(first.context_id, results[:1])

    # A changed catalog is a new context; earlier results stay as they were.
    second = context("c" * 64)
    store.register_context(second, first_used_at=NOW)
    assert sorted(store.unmatched_observation_ids(second.context_id)) == sorted(pending)
    empty = ExactMatcher([])
    store.record_match_results(
        second.context_id,
        [(key, empty.match_observation(by_id[key].observation())) for key in pending],
    )
    old = store.match_results(first.context_id)
    new = store.match_results(second.context_id)
    exact = observation_id(offer())
    assert (old[exact].match_status, old[exact].target_id) == ("EXACT", "TGT-000001")
    assert (new[exact].match_status, new[exact].target_id) == ("UNMATCHED", "")
    assert store.counts()["matching_contexts"] == 2
    assert store.counts()["match_results"] == 4
    assert store.counts()["observations"] == 2


def test_a_context_id_cannot_be_reused_for_different_inputs(store) -> None:
    store.register_context(context(), first_used_at=NOW)
    with pytest.raises(ObservationConflictError):
        store.register_context(replace(context(), configuration_targets=2), first_used_at=NOW)


def test_results_need_a_registered_context(store) -> None:
    store.import_observations([offer()], source_label="a", imported_at=NOW)
    [stored] = store.observations()
    result = matcher().match_observation(stored.observation())
    assert result.status is MatchStatus.EXACT
    with pytest.raises(ObservationStoreError):
        store.record_match_results("missing", [(stored.observation_id, result)])
    assert store.counts()["match_results"] == 0
