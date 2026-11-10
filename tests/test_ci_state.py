"""Durable state in (simulated) private GitHub Releases: restore, save, corruption, append-only
history, encryption, concurrency, rollback, bundles and publication records.

These tests run against ``LocalReleaseStore``, an in-process stand-in that behaves like the parts of
GitHub Releases the design relies on (drafts, unique asset names, publish, tag uniqueness,
optional immutability). The real API client is covered in test_release_store.py with a scripted
HTTP session. None of this is a hosted integration test.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from laptop_tracker import release_store as rs
from laptop_tracker import state_crypto as sc
from laptop_tracker import state_store as ss
from laptop_tracker.observation_store import ObservationStore


def add_observation(db: Path, n: int, retrieved: str = "2026-11-02T01:00:00Z") -> None:
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT OR IGNORE INTO import_batches VALUES (1, ?, 'test', 'x', 1, 1, 0, 0)",
            (retrieved,),
        )
        connection.execute(
            "INSERT INTO observations VALUES (?, ?, 'oct', ?, 0, ?, '', '', '100', 'PHP', "
            "'regular', '', '', 'in_stock', 'false', 'v1', 'c', '{}', ?, 1)",
            (f"obs-{n}", f"offer-{n}", f"https://example.test/{n}", retrieved, f"rec-{n}"),
        )
        connection.commit()
    finally:
        connection.close()


def make_root(tmp_path: Path, name: str = "root", observations: int = 2) -> Path:
    root = tmp_path / name
    (root / "config").mkdir(parents=True)
    db = root / ss.STORE_REL
    with ObservationStore.open(db):
        pass
    for n in range(observations):
        add_observation(db, n)
    (root / "data/history/retailer_rate_limits.json").write_text('{"version": 1}')
    for rel in ("duplicate_decisions", "eligibility_decisions", "identity_review_register"):
        (root / f"config/{rel}.csv").write_text("key,decision\nk1,KEEP\n")
    return root


@pytest.fixture
def ring() -> sc.KeyRing:
    return sc.KeyRing(sc.parse_key(sc.generate_key()))


@pytest.fixture
def store(tmp_path: Path) -> rs.LocalReleaseStore:
    return rs.LocalReleaseStore(tmp_path / "releases")


def asset_path(store: rs.LocalReleaseStore, tag: str, name: str) -> Path:
    release = next(r for r in store.list_releases(tag) if r.tag == tag)
    return store.root / str(release.id) / "assets" / name


def restored_runner(store, ring, tmp_path, name="runner") -> Path:
    runner = tmp_path / name
    ss.restore(store, ring, runner)
    return runner


class Flaky:
    """Wraps a store and fails the Nth call of one operation."""

    def __init__(self, inner, operation: str, nth: int = 1, error=None) -> None:
        self.inner, self.operation, self.nth, self.calls = inner, operation, nth, 0
        self.error = error or rs.ReleaseStoreError("connection reset")

    def __getattr__(self, name):
        target = getattr(self.inner, name)
        if name != self.operation:
            return target

        def wrapper(*args, **kwargs):
            self.calls += 1
            if self.calls == self.nth:
                raise self.error
            return target(*args, **kwargs)

        return wrapper


# restoration ----------------------------------------------------------------------------------


def test_missing_state_is_an_error_and_never_an_empty_history(store, ring, tmp_path) -> None:
    target = tmp_path / "runner"
    with pytest.raises(ss.StateError, match="seed"):
        ss.restore(store, ring, target)
    assert not (target / ss.STORE_REL).exists()


def test_seed_then_restore_roundtrips_bytes_and_original_timestamps(store, ring, tmp_path) -> None:
    source = make_root(tmp_path)
    seeded = ss.seed(store, ring, source)
    assert seeded["version_id"] == "tracker-state-000001" and seeded["restore_check"] == "ok"
    runner = tmp_path / "runner"
    info = ss.restore(store, ring, runner)
    assert info["version_id"] == seeded["version_id"] and info["rows"]["observations"] == 2
    for rel in info["files"]:
        assert (runner / rel).read_bytes() == (source / rel).read_bytes() or rel == ss.STORE_REL
    restored = sqlite3.connect(runner / ss.STORE_REL)
    assert restored.execute("SELECT DISTINCT retrieved_at FROM observations").fetchall() == [
        ("2026-11-02T01:00:00Z",)
    ]
    assert (runner / ss.BASELINE_DIR / "baseline.json").is_file()
    assert (source / ss.STORE_REL).stat().st_size > 0  # seeding never touches the local state


def test_seed_refuses_to_replace_existing_state(store, ring, tmp_path) -> None:
    root = make_root(tmp_path)
    ss.seed(store, ring, root)
    with pytest.raises(ss.StateError, match="already exists"):
        ss.seed(store, ring, root)


def test_seed_needs_every_required_file_and_leaves_no_release_behind(store, ring, tmp_path) -> None:
    root = make_root(tmp_path)
    (root / "config/eligibility_decisions.csv").unlink()
    with pytest.raises(ss.StateError, match="eligibility_decisions"):
        ss.seed(store, ring, root)
    assert store.list_releases("") == []


def test_restore_refuses_to_overwrite_local_files_unless_asked(store, ring, tmp_path) -> None:
    root = make_root(tmp_path)
    ss.seed(store, ring, root)
    with pytest.raises(ss.StateError, match="overwrite"):
        ss.restore(store, ring, root)
    assert ss.restore(store, ring, root, overwrite=True)["rows"]["observations"] == 2


def test_a_tampered_archive_fails_its_checksum_and_nothing_is_half_restored(
    store, ring, tmp_path
) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    victim = asset_path(store, "tracker-state-000001", ss.STATE_ASSET)
    data = bytearray(victim.read_bytes())
    data[-5] ^= 0xFF
    victim.write_bytes(bytes(data))
    runner = tmp_path / "runner"
    with pytest.raises(ss.StateError, match="checksum"):
        ss.restore(store, ring, runner)
    assert not (runner / "config/duplicate_decisions.csv").exists()


def test_an_altered_archive_with_a_recomputed_checksum_still_fails_authentication(
    store, ring, tmp_path
) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    victim = asset_path(store, "tracker-state-000001", ss.STATE_ASSET)
    data = bytearray(victim.read_bytes())
    data[-5] ^= 0xFF
    victim.write_bytes(bytes(data))
    manifest_file = asset_path(store, "tracker-state-000001", ss.MANIFEST_ASSET)
    manifest = json.loads(manifest_file.read_text())
    manifest["archive"]["sha256"] = ss.sha256_file(victim)
    manifest_file.write_text(json.dumps(manifest))
    with pytest.raises(ss.StateError, match="could not be decrypted"):
        ss.restore(store, ring, tmp_path / "runner")


def test_a_wrong_or_missing_key_never_produces_empty_history(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    other = sc.KeyRing(sc.parse_key(sc.generate_key()))
    runner = tmp_path / "runner"
    with pytest.raises(ss.StateError, match="not available"):
        ss.restore(store, other, runner)
    assert not runner.exists() or not (runner / ss.STORE_REL).exists()
    with pytest.raises(ss.StateError, match="STATE_ENCRYPTION_KEY"):
        ss.store_and_keys({"STATE_LOCAL_DIR": str(tmp_path / "s")})


def test_a_previous_key_still_restores_older_versions_after_rotation(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    rotated = sc.KeyRing(sc.parse_key(sc.generate_key()), (ring.current,))
    assert ss.restore(store, rotated, tmp_path / "runner")["rows"]["observations"] == 2
    runner = tmp_path / "runner"
    add_observation(runner / ss.STORE_REL, 10)
    saved = ss.save(store, rotated, runner, run_id="20261109T010000Z")
    manifest = ss.read_public_manifest(store, store.list_releases(saved["version_id"])[0])
    assert manifest["encryption"]["key_id"] == rotated.current_id != ring.current_id


def test_stored_assets_and_release_metadata_contain_no_plaintext(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    release = store.list_releases("tracker-state-")[0]
    blob = asset_path(store, release.tag, ss.STATE_ASSET).read_bytes()
    assert b"SQLite format" not in blob and b"k1,KEEP" not in blob and blob.startswith(sc.MAGIC)
    meta = (store.root / str(release.id) / "release.json").read_text()
    assert "k1" not in meta and "obs-" not in meta
    manifest = json.loads(asset_path(store, release.tag, ss.MANIFEST_ASSET).read_text())
    assert "observations" not in json.dumps(manifest)  # no row counts in the public manifest
    assert ring.current_id in json.dumps(manifest) and sc.generate_key() not in str(manifest)


def test_a_truncated_asset_is_reported_as_incomplete(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    victim = asset_path(store, "tracker-state-000001", ss.STATE_ASSET)
    victim.write_bytes(victim.read_bytes()[:100])
    with pytest.raises(ss.StateError, match="incomplete"):
        ss.restore(store, ring, tmp_path / "runner")
    assert ss.list_versions(store)[0]["complete"] is False


def test_a_corrupt_database_is_refused_at_seed(store, ring, tmp_path) -> None:
    root = make_root(tmp_path)
    (root / ss.STORE_REL).write_bytes(b"this is not a database" * 100)
    with pytest.raises(ss.StateError):
        ss.seed(store, ring, root)
    assert store.list_releases("") == []


def test_sqlite_integrity_check_names_missing_tables(tmp_path) -> None:
    db = tmp_path / "x.sqlite3"
    sqlite3.connect(db).execute("CREATE TABLE t (a)").connection.commit()
    with pytest.raises(ss.StateError, match="lacks the table"):
        ss.check_sqlite(db)


def test_a_draft_is_never_treated_as_state(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    # An abandoned half upload of the next version: a draft with only some assets.
    draft = store.create_draft("tracker-state-000002", "x", "x")
    partial = tmp_path / "partial"
    partial.write_bytes(b"partial")
    store.upload_asset(draft, ss.STATE_ASSET, partial)
    assert ss.restore(store, ring, tmp_path / "runner")["seq"] == 1
    assert [v["seq"] for v in ss.list_versions(store)] == [1]


# saving ---------------------------------------------------------------------------------------


def test_save_creates_a_new_linked_version_and_keeps_the_older_one(store, ring, tmp_path) -> None:
    first = ss.seed(store, ring, make_root(tmp_path))["version_id"]
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10, retrieved="2026-11-09T01:00:00Z")
    (runner / "data/history/retailer_rate_limits.json").write_text('{"version": 1, "n": 2}')
    result = ss.save(store, ring, runner, run_id="20261109T010000Z")
    assert result["previous_version_id"] == first and result["new_observations"] == 1
    assert result["version_id"] == "tracker-state-000002" and result["already_saved"] is False
    versions = ss.list_versions(store)
    assert [v["seq"] for v in versions] == [1, 2]  # the older version is kept for rollback
    second = ss.read_public_manifest(store, store.list_releases("tracker-state-000002")[0])
    parent = json.loads(asset_path(store, first, ss.MANIFEST_ASSET).read_text())
    assert second["parent_seq"] == 1 and second["parent_manifest_sha256"] == ss._json_sha(parent)
    # The next runner starts from the new state, old rows still carrying their old timestamps.
    again = restored_runner(store, ring, tmp_path, "runner2")
    rows = (
        sqlite3.connect(again / ss.STORE_REL)
        .execute("SELECT retrieved_at FROM observations ORDER BY observation_id")
        .fetchall()
    )
    assert sorted(set(rows)) == [("2026-11-02T01:00:00Z",), ("2026-11-09T01:00:00Z",)]


def test_saving_the_same_run_twice_is_idempotent(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    first = ss.save(store, ring, runner, run_id="20261109T010000Z")
    again = ss.save(store, ring, runner, run_id="20261109T010000Z")
    assert again["already_saved"] is True and again["version_id"] == first["version_id"]
    assert len(ss.list_versions(store)) == 2  # no duplicate snapshot


def test_save_requires_a_restore_in_this_workspace(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    with pytest.raises(ss.StateError, match="not restored"):
        ss.save(store, ring, make_root(tmp_path, "other"), run_id="r")


@pytest.mark.parametrize("damage", ["delete", "rewrite_time", "rewrite_price"])
def test_save_refuses_a_history_that_lost_or_changed_restored_observations(
    store, ring, tmp_path, damage
) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    connection = sqlite3.connect(runner / ss.STORE_REL)
    connection.execute("PRAGMA foreign_keys = OFF")
    # The store guards itself with triggers; simulate damage done outside the application.
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ).fetchall():
        connection.execute(f"DROP TRIGGER {name}")
    statement = {
        "delete": "DELETE FROM observations WHERE observation_id='obs-0'",
        "rewrite_time": "UPDATE observations SET retrieved_at='2026-11-09T09:00:00Z' "
        "WHERE observation_id='obs-0'",
        "rewrite_price": "UPDATE observations SET price='1' WHERE observation_id='obs-0'",
    }[damage]
    connection.execute(statement)
    connection.commit()
    connection.close()
    with pytest.raises(ss.StateError, match="append-only|foreign|integrity"):
        ss.save(store, ring, runner, run_id="r")
    assert [v["seq"] for v in ss.list_versions(store)] == [1]  # the good state is untouched


def test_save_refuses_a_corrupt_store_and_keeps_the_previous_version(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    (runner / ss.STORE_REL).write_bytes(b"garbage" * 1000)
    with pytest.raises(ss.StateError):
        ss.save(store, ring, runner, run_id="r")
    assert [v["seq"] for v in ss.list_versions(store)] == [1]


@pytest.mark.parametrize("operation,nth", [("upload_asset", 2), ("publish", 1)])
def test_an_interrupted_upload_never_becomes_the_latest_state(
    store, ring, tmp_path, operation, nth
) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    flaky = Flaky(store, operation, nth)
    with pytest.raises(ss.StateError, match="Could not store"):
        ss.save(flaky, ring, runner, run_id="20261109T010000Z")
    assert [v["seq"] for v in ss.list_versions(store)] == [1]
    assert not [r for r in store.list_releases("tracker-state-") if r.draft]  # cleaned up
    assert ss.restore(store, ring, tmp_path / "again")["seq"] == 1  # still restorable
    # The same run can simply save again.
    assert ss.save(store, ring, runner, run_id="20261109T010000Z")["seq"] == 2


def test_an_unexpected_crash_mid_save_also_removes_the_draft(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    crashing = Flaky(store, "upload_asset", 1, error=OSError("runner lost network"))
    with pytest.raises(OSError):
        ss.save(crashing, ring, runner, run_id="20261109T010000Z")
    assert not [r for r in store.list_releases("tracker-state-") if r.draft]


def test_a_concurrent_save_is_refused_because_the_state_moved(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    a = restored_runner(store, ring, tmp_path, "a")
    b = restored_runner(store, ring, tmp_path, "b")
    add_observation(a / ss.STORE_REL, 10)
    add_observation(b / ss.STORE_REL, 11)
    ss.save(store, ring, a, run_id="20261109T010000Z")
    with pytest.raises(ss.StateError, match="moved from version 1 to 2"):
        ss.save(store, ring, b, run_id="20261109T020000Z")
    assert ss.restore(store, ring, tmp_path / "c")["rows"]["observations"] == 3  # b not mixed in


def test_a_young_draft_of_the_same_version_means_another_writer_is_active(
    store, ring, tmp_path
) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    store.create_draft("tracker-state-000002", "other writer", "x")
    with pytest.raises(ss.StateError, match="Another writer is working"):
        ss.save(store, ring, runner, run_id="20261109T010000Z")
    assert [v["seq"] for v in ss.list_versions(store)] == [1]


def test_an_abandoned_old_draft_is_removed_and_the_save_succeeds(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    dead = store.create_draft("tracker-state-000002", "crashed run", "x")
    old = (datetime.now(UTC) - timedelta(hours=ss.STALE_DRAFT_HOURS + 1)).isoformat()
    store._save(dead.id, created_at=old)
    assert ss.save(store, ring, runner, run_id="20261109T010000Z")["seq"] == 2
    assert not [r for r in store.list_releases("tracker-state-") if r.draft]


def test_the_lowest_release_id_wins_when_two_writers_create_drafts_together(
    store, ring, tmp_path
) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    competitor = store.create_draft("tracker-state-000002", "rival", "x")

    class HidesRival:
        """The rival's draft is invisible to the first listing only (a real race window)."""

        def __init__(self, inner):
            self.inner, self.hidden = inner, True

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def list_releases(self, prefix):
            found = self.inner.list_releases(prefix)
            if self.hidden and prefix == "tracker-state-000002":
                self.hidden = False
                return [r for r in found if r.id != competitor.id]
            return found

    with pytest.raises(ss.StateError, match="claimed .* first"):
        ss.save(HidesRival(store), ring, runner, run_id="20261109T010000Z")
    assert [v["seq"] for v in ss.list_versions(store)] == [1]
    assert [r.id for r in store.list_releases("tracker-state-000002")] == [competitor.id]


def test_a_publish_that_loses_the_tag_race_is_not_state(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    taken = Flaky(store, "publish", 1, error=rs.TagExists("tag exists"))
    with pytest.raises(ss.StateError, match="Could not store"):
        ss.save(taken, ring, runner, run_id="20261109T010000Z")
    assert [v["seq"] for v in ss.list_versions(store)] == [1]


# recovery -------------------------------------------------------------------------------------


def test_an_earlier_named_version_can_be_restored(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    ss.save(store, ring, runner, run_id="20261109T010000Z")
    assert ss.restore(store, ring, tmp_path / "old", version=1)["rows"]["observations"] == 2
    assert ss.restore(store, ring, tmp_path / "t", version="tracker-state-000001")["seq"] == 1


def test_rollback_publishes_a_new_version_with_the_old_contents(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    ss.save(store, ring, runner, run_id="20261109T010000Z")
    result = ss.rollback(store, ring, 1)
    assert result["seq"] == 3 and result["rolled_back_to"] == "tracker-state-000001"
    assert [v["seq"] for v in ss.list_versions(store)] == [1, 2, 3]  # nothing deleted
    assert ss.restore(store, ring, tmp_path / "after")["rows"]["observations"] == 2
    manifest = ss.read_public_manifest(store, store.list_releases("tracker-state-000003")[0])
    assert manifest["rollback_of"] == 1 and manifest["parent_seq"] == 2


def test_a_damaged_latest_version_fails_loudly_and_rollback_recovers(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    ss.save(store, ring, runner, run_id="20261109T010000Z")
    victim = asset_path(store, "tracker-state-000002", ss.STATE_ASSET)
    victim.write_bytes(victim.read_bytes()[:50])
    with pytest.raises(ss.StateError, match="incomplete"):  # no silent fallback to version 1
        ss.restore(store, ring, tmp_path / "x")
    ss.rollback(store, ring, 1)
    assert ss.restore(store, ring, tmp_path / "ok")["seq"] == 3


def test_rollback_refuses_a_missing_or_damaged_target(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    with pytest.raises(ss.StateError, match="does not exist"):
        ss.rollback(store, ring, 9)
    victim = asset_path(store, "tracker-state-000001", ss.STATE_ASSET)
    victim.write_bytes(victim.read_bytes()[:50])
    with pytest.raises(ss.StateError, match="incomplete"):
        ss.rollback(store, ring, 1)


def test_verify_checks_a_version_without_touching_the_workspace(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    assert ss.verify_version(store, ring)["valid"] is True
    with pytest.raises(ss.StateError):
        ss.verify_version(store, sc.KeyRing(sc.parse_key(sc.generate_key())))


def test_the_whole_cycle_works_with_immutable_releases(tmp_path, ring) -> None:
    store = rs.LocalReleaseStore(tmp_path / "immutable", immutable=True)
    ss.seed(store, ring, make_root(tmp_path))
    runner = restored_runner(store, ring, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    assert ss.save(store, ring, runner, run_id="20261109T010000Z")["seq"] == 2
    published = store.list_releases("tracker-state-000001")[0]
    assert published.immutable is True
    with pytest.raises(rs.ReleaseImmutable):  # a published release can never be changed
        store.upload_asset(published, "late.txt", runner / "config/duplicate_decisions.csv")
    assert ss.rollback(store, ring, 1)["seq"] == 3  # recovery adds versions, never edits them


def test_storage_report_counts_sizes_drafts_and_warns(store, ring, tmp_path) -> None:
    ss.seed(store, ring, make_root(tmp_path))
    store.create_draft("tracker-state-000009", "x", "x")
    report = ss.storage_report(store)
    assert report["groups"]["state"]["releases"] == 1 and report["total_bytes"] > 0
    assert report["drafts"] == ["tracker-state-000009"] and report["warnings"]
    assert any("review threshold" in w for w in ss.storage_report(store, warn_mb=0)["warnings"])


# bundles and publication records --------------------------------------------------------------


def make_run(tmp_path: Path) -> Path:
    run = tmp_path / "20261109T065857Z"
    for rel in (
        "shopee_variations/a.csv",
        "variation_reports/b.csv",
        "context_reports/c.csv",
        "discovery/collection/m.csv",
        "discovery/discovery_summary.json",
        "discovery/evidence/page.html",
        "status.json",
        "stages.jsonl",
        "publications/20261109T071157006575Z_preview/publication_report.json",
        "publications/20261109T075315178518Z_write/backup_X.json",
    ):
        path = run / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    return run


def test_bundle_roundtrip_keeps_publishable_files_and_leaves_bulky_ones(
    store, ring, tmp_path
) -> None:
    run = make_run(tmp_path)
    manifest = ss.save_bundle(store, ring, run, run.name, {"gate": {"ok": True}})
    assert manifest["gate"] == {"ok": True}
    out = tmp_path / "elsewhere" / run.name
    ss.load_bundle(store, ring, run.name, out)
    assert (out / "variation_reports/b.csv").is_file()
    assert (out / "publications/20261109T071157006575Z_preview/publication_report.json").is_file()
    assert not (out / "discovery/evidence").exists()  # saved retailer pages are not bundled
    assert not (out / "publications/20261109T075315178518Z_write").exists()
    blob = asset_path(store, f"tracker-run-{run.name}", ss.BUNDLE_ASSET).read_bytes()
    assert blob.startswith(sc.MAGIC)  # encrypted at rest


def test_saving_a_bundle_twice_returns_the_stored_one(store, ring, tmp_path) -> None:
    run = make_run(tmp_path)
    first = ss.save_bundle(store, ring, run, run.name, {})
    assert ss.save_bundle(store, ring, run, run.name, {}) == first
    assert len(store.list_releases("tracker-run-")) == 1


def test_a_bundle_that_fails_its_checksum_or_escapes_is_refused(store, ring, tmp_path) -> None:
    run = make_run(tmp_path)
    ss.save_bundle(store, ring, run, run.name, {})
    victim = asset_path(store, f"tracker-run-{run.name}", ss.BUNDLE_ASSET)
    original = victim.read_bytes()
    victim.write_bytes(b"corrupt")
    with pytest.raises(ss.StateError, match="checksum"):
        ss.load_bundle(store, ring, run.name, tmp_path / "out")
    victim.write_bytes(original)
    with pytest.raises(ss.StateError, match="not available"):
        ss.load_bundle(store, sc.KeyRing(sc.parse_key(sc.generate_key())), run.name, tmp_path / "o")
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as archive:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ss.StateError, match="Unsafe"):
        ss._safe_extract(evil, tmp_path / "dest")
    assert not (tmp_path / "escape.txt").exists()


def test_a_missing_bundle_or_a_malformed_run_id_cannot_be_published(store, ring, tmp_path) -> None:
    with pytest.raises(ss.StateError, match="no saved bundle"):
        ss.load_bundle(store, ring, "20260101T000000Z", tmp_path / "x")
    with pytest.raises(ss.StateError, match="not a run folder name"):
        ss.load_bundle(store, ring, "../../etc", tmp_path / "x")


def test_publication_attempts_are_appended_never_replaced(store, ring) -> None:
    ss.append_publication_attempt(store, ring, "PUB-1", {"status": "PARTIAL_WRITE", "run_id": "r"})
    second = ss.append_publication_attempt(
        store, ring, "PUB-1", {"status": "PUBLISHED", "run_id": "r", "counts": {"rows": 41}}
    )
    assert second["attempt"] == 2
    records = ss.read_publication_attempts(store, ring, "PUB-1")
    assert [r["status"] for r in records] == ["PARTIAL_WRITE", "PUBLISHED"]
    assert ss.latest_successful_publication(store, ring)["counts"] == {"rows": 41}
    assert sorted(r.tag for r in store.list_releases("tracker-pub-")) == [
        "tracker-pub-PUB-1-a01",
        "tracker-pub-PUB-1-a02",
    ]


def test_a_recorded_attempt_can_be_fetched_with_its_snapshots_for_a_rollback(
    store, ring, tmp_path
) -> None:
    folder = tmp_path / "write"
    folder.mkdir()
    (folder / "backup_PILOT_PRICE_COMPARISON.json").write_text('{"values": [["old"]]}')
    ss.append_publication_attempt(store, ring, "PUB-5", {"status": "PARTIAL_WRITE"}, folder)
    ss.append_publication_attempt(store, ring, "PUB-5", {"status": "PUBLISHED"})
    fetched = ss.fetch_publication_attempt(store, ring, "PUB-5", tmp_path / "out1", attempt=1)
    assert fetched["record"]["status"] == "PARTIAL_WRITE"
    snapshot = Path(fetched["snapshots"]) / "backup_PILOT_PRICE_COMPARISON.json"
    assert snapshot.read_text() == '{"values": [["old"]]}'  # what publisher --rollback reads
    assert (
        ss.fetch_publication_attempt(store, ring, "PUB-5", tmp_path / "out2")["record"]["status"]
        == "PUBLISHED"
    )
    with pytest.raises(ss.StateError, match="no attempt 9"):
        ss.fetch_publication_attempt(store, ring, "PUB-5", tmp_path / "out3", attempt=9)
    with pytest.raises(ss.StateError, match="No recorded attempt"):
        ss.fetch_publication_attempt(store, ring, "PUB-404", tmp_path / "out4")


def test_no_successful_publication_means_no_baseline(store, ring) -> None:
    ss.append_publication_attempt(store, ring, "PUB-2", {"status": "FAILED_BEFORE_WRITE"})
    assert ss.latest_successful_publication(store, ring) is None


def test_publication_snapshots_are_stored_encrypted_for_recovery(store, ring, tmp_path) -> None:
    folder = tmp_path / "write"
    folder.mkdir()
    (folder / "backup_a.json").write_text('{"tab": "secret-sales-data"}')
    ss.append_publication_attempt(store, ring, "PUB-3", {"status": "PUBLISHED"}, folder)
    blob = asset_path(store, "tracker-pub-PUB-3-a01", ss.ATTEMPT_ASSET).read_bytes()
    assert b"secret-sales-data" not in blob and blob.startswith(sc.MAGIC)


# configuration --------------------------------------------------------------------------------


def test_storage_and_key_must_be_configured_explicitly(tmp_path) -> None:
    key = sc.generate_key()
    with pytest.raises(ss.StateError, match="STATE_ENCRYPTION_KEY"):
        ss.store_and_keys({"STATE_LOCAL_DIR": str(tmp_path / "s")})
    with pytest.raises(ss.StateError, match="not valid base64url|32 bytes"):
        ss.store_and_keys({"STATE_LOCAL_DIR": str(tmp_path / "s"), "STATE_ENCRYPTION_KEY": "x"})
    store, ring = ss.store_and_keys(
        {"STATE_LOCAL_DIR": str(tmp_path / "s"), "STATE_ENCRYPTION_KEY": key}
    )
    assert isinstance(store, rs.LocalReleaseStore) and ring.current_id


def test_a_failed_listing_is_a_state_error_not_empty_state(ring) -> None:
    class Broken:
        def list_releases(self, prefix):
            raise rs.ReleaseStoreError("HTTP 401")

    with pytest.raises(ss.StateError, match="Could not list"):
        ss.published_versions(Broken())
