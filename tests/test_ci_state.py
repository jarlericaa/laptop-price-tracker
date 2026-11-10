"""Durable state for CI runs: restore, save, corruption, append-only history, locks, bundles."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
def backend(tmp_path: Path) -> ss.LocalBackend:
    return ss.LocalBackend(tmp_path / "bucket")


# restoration ----------------------------------------------------------------------------------


def test_missing_state_is_an_error_and_never_an_empty_history(backend, tmp_path) -> None:
    target = tmp_path / "runner"
    with pytest.raises(ss.StateError, match="seed"):
        ss.restore(backend, target)
    assert not (target / ss.STORE_REL).exists()


def test_seed_then_restore_roundtrips_bytes_and_original_timestamps(backend, tmp_path) -> None:
    source = make_root(tmp_path)
    version = ss.seed(backend, source)
    runner = tmp_path / "runner"
    info = ss.restore(backend, runner)
    assert info["version_id"] == version and info["rows"]["observations"] == 2
    for rel in info["files"]:
        assert (runner / rel).read_bytes() == (source / rel).read_bytes() or rel == ss.STORE_REL
    restored = sqlite3.connect(runner / ss.STORE_REL)
    assert restored.execute("SELECT DISTINCT retrieved_at FROM observations").fetchall() == [
        ("2026-11-02T01:00:00Z",)
    ]
    assert (runner / ss.BASELINE_DIR / "baseline.json").is_file()


def test_seed_refuses_to_replace_existing_state(backend, tmp_path) -> None:
    root = make_root(tmp_path)
    ss.seed(backend, root)
    with pytest.raises(ss.StateError, match="already exists"):
        ss.seed(backend, root)


def test_seed_needs_every_required_file(backend, tmp_path) -> None:
    root = make_root(tmp_path)
    (root / "config/eligibility_decisions.csv").unlink()
    with pytest.raises(ss.StateError, match="eligibility_decisions"):
        ss.seed(backend, root)
    assert backend.get(ss.POINTER) is None


def test_restore_refuses_to_overwrite_local_files_unless_asked(backend, tmp_path) -> None:
    root = make_root(tmp_path)
    ss.seed(backend, root)
    with pytest.raises(ss.StateError, match="overwrite"):
        ss.restore(backend, root)
    assert ss.restore(backend, root, overwrite=True)["rows"]["observations"] == 2


def test_a_tampered_state_file_fails_its_checksum(backend, tmp_path) -> None:
    version = ss.seed(backend, make_root(tmp_path))
    victim = backend._path(f"state/versions/{version}/files/config/duplicate_decisions.csv")
    victim.write_text("key,decision\nk1,DISCARD\n")
    runner = tmp_path / "runner"
    with pytest.raises(ss.StateError, match="checksum"):
        ss.restore(backend, runner)
    assert not (runner / "config/duplicate_decisions.csv").exists()  # nothing half restored


def test_a_corrupt_sqlite_snapshot_is_refused_at_seed_and_at_restore(backend, tmp_path) -> None:
    root = make_root(tmp_path)
    (root / ss.STORE_REL).write_bytes(b"this is not a database" * 100)
    with pytest.raises(ss.StateError):
        ss.seed(backend, root)

    good = make_root(tmp_path, "good")
    version = ss.seed(backend, good)
    stored = backend._path(f"state/versions/{version}/files/{ss.STORE_REL}")
    data = stored.read_bytes()
    stored.write_bytes(data[:4096] + b"\0" * 512 + data[4608:])
    manifest = backend._path(f"state/versions/{version}/manifest.json")
    meta = json.loads(manifest.read_text())
    meta["files"][ss.STORE_REL]["sha256"] = ss.sha256_file(stored)  # checksum "agrees"
    manifest.write_text(json.dumps(meta, indent=2, sort_keys=True))
    pointer = json.loads(backend._path(ss.POINTER).read_text())
    import hashlib

    pointer["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    backend._path(ss.POINTER).write_text(json.dumps(pointer))
    with pytest.raises(ss.StateError):
        ss.restore(backend, tmp_path / "runner")


def test_sqlite_integrity_check_names_missing_tables(tmp_path) -> None:
    db = tmp_path / "x.sqlite3"
    sqlite3.connect(db).execute("CREATE TABLE t (a)").connection.commit()
    with pytest.raises(ss.StateError, match="lacks the table"):
        ss.check_sqlite(db)


def test_a_manifest_that_does_not_match_the_pointer_is_refused(backend, tmp_path) -> None:
    version = ss.seed(backend, make_root(tmp_path))
    manifest = backend._path(f"state/versions/{version}/manifest.json")
    manifest.write_text(manifest.read_text() + " ")
    with pytest.raises(ss.StateError, match="does not match"):
        ss.restore(backend, tmp_path / "runner")


# saving ---------------------------------------------------------------------------------------


def restored_runner(backend, tmp_path, name="runner") -> Path:
    runner = tmp_path / name
    ss.restore(backend, runner)
    return runner


def test_save_after_new_observations_creates_a_new_version_and_moves_the_pointer(
    backend, tmp_path
) -> None:
    first = ss.seed(backend, make_root(tmp_path))
    runner = restored_runner(backend, tmp_path)
    add_observation(runner / ss.STORE_REL, 10, retrieved="2026-11-09T01:00:00Z")
    (runner / "data/history/retailer_rate_limits.json").write_text('{"version": 1, "n": 2}')
    result = ss.save(backend, runner, run_id="20261109T010000Z")
    assert result["previous_version_id"] == first and result["new_observations"] == 1
    pointer, _ = ss.read_pointer(backend)
    assert pointer["version_id"] == result["version_id"] != first
    assert len(ss.list_versions(backend)) == 2  # the older version is kept for recovery
    # The next runner starts from the new state, with old rows still carrying old timestamps.
    again = restored_runner(backend, tmp_path, "runner2")
    rows = (
        sqlite3.connect(again / ss.STORE_REL)
        .execute("SELECT retrieved_at FROM observations ORDER BY observation_id")
        .fetchall()
    )
    assert sorted(set(rows)) == [("2026-11-02T01:00:00Z",), ("2026-11-09T01:00:00Z",)]


def test_save_requires_a_restore_in_this_workspace(backend, tmp_path) -> None:
    ss.seed(backend, make_root(tmp_path))
    with pytest.raises(ss.StateError, match="not restored"):
        ss.save(backend, make_root(tmp_path, "other"), run_id="r")


@pytest.mark.parametrize("damage", ["delete", "rewrite_time", "rewrite_price"])
def test_save_refuses_a_history_that_lost_or_changed_restored_observations(
    backend, tmp_path, damage
) -> None:
    ss.seed(backend, make_root(tmp_path))
    runner = restored_runner(backend, tmp_path)
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
    before, _ = ss.read_pointer(backend)
    with pytest.raises(ss.StateError, match="append-only|foreign|integrity"):
        ss.save(backend, runner, run_id="r")
    assert ss.read_pointer(backend)[0] == before  # the good state is untouched


def test_save_refuses_a_corrupt_store_and_keeps_the_previous_version(backend, tmp_path) -> None:
    ss.seed(backend, make_root(tmp_path))
    runner = restored_runner(backend, tmp_path)
    (runner / ss.STORE_REL).write_bytes(b"garbage" * 1000)
    before, _ = ss.read_pointer(backend)
    with pytest.raises(ss.StateError):
        ss.save(backend, runner, run_id="r")
    assert ss.read_pointer(backend)[0] == before


def test_an_interrupted_upload_never_becomes_the_latest_state(
    backend, tmp_path, monkeypatch
) -> None:
    first = ss.seed(backend, make_root(tmp_path))
    runner = restored_runner(backend, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    real_upload = backend.upload
    calls = {"n": 0}

    def flaky(name, path):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("connection reset")
        real_upload(name, path)

    monkeypatch.setattr(backend, "upload", flaky)
    with pytest.raises(OSError):
        ss.save(backend, runner, run_id="r")
    assert ss.read_pointer(backend)[0]["version_id"] == first
    assert ss.restore(backend, tmp_path / "again")["version_id"] == first  # still restorable


def test_a_concurrent_save_loses_the_compare_and_swap(backend, tmp_path) -> None:
    ss.seed(backend, make_root(tmp_path))
    a = restored_runner(backend, tmp_path, "a")
    b = restored_runner(backend, tmp_path, "b")
    add_observation(a / ss.STORE_REL, 10)
    add_observation(b / ss.STORE_REL, 11)
    ss.save(backend, a, run_id="a")
    with pytest.raises(ss.StateError, match="changed"):
        ss.save(backend, b, run_id="b")
    restored = ss.restore(backend, tmp_path / "c")
    assert restored["rows"]["observations"] == 3  # b's observations were not mixed in silently


def test_recovery_restores_an_earlier_named_version(backend, tmp_path) -> None:
    first = ss.seed(backend, make_root(tmp_path))
    runner = restored_runner(backend, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    ss.save(backend, runner, run_id="later")
    info = ss.restore(backend, tmp_path / "old", version_id=first)
    assert info["rows"]["observations"] == 2


def test_promote_makes_an_earlier_validated_version_the_latest_again(backend, tmp_path) -> None:
    first = ss.seed(backend, make_root(tmp_path))
    runner = restored_runner(backend, tmp_path)
    add_observation(runner / ss.STORE_REL, 10)
    bad = ss.save(backend, runner, run_id="bad")["version_id"]
    assert ss.promote(backend, first)["promoted"] is True
    assert ss.read_pointer(backend)[0]["version_id"] == first
    assert bad in ss.list_versions(backend)  # nothing is deleted
    assert ss.restore(backend, tmp_path / "after")["rows"]["observations"] == 2


def test_promote_refuses_a_missing_or_damaged_version(backend, tmp_path) -> None:
    first = ss.seed(backend, make_root(tmp_path))
    with pytest.raises(ss.StateError, match="no manifest"):
        ss.promote(backend, "20200101T000000Z-nothing")
    backend._path(f"state/versions/{first}/files/config/duplicate_decisions.csv").write_text(
        "x,y\n"
    )
    with pytest.raises(ss.StateError, match="checksum"):
        ss.promote(backend, first)


# lock -----------------------------------------------------------------------------------------


def test_a_live_lock_blocks_a_second_run_and_only_its_owner_releases_it(backend) -> None:
    ss.acquire_lock(backend, "run-1")
    with pytest.raises(ss.StateError, match="holds the state lock"):
        ss.acquire_lock(backend, "run-2")
    assert ss.release_lock(backend, "run-2") is False
    assert ss.release_lock(backend, "run-1") is True
    ss.acquire_lock(backend, "run-2")


def test_an_expired_lock_is_taken_over(backend) -> None:
    past = datetime.now(UTC) - timedelta(hours=8)
    ss.acquire_lock(backend, "dead-run", ttl_minutes=60, now=past)
    ss.acquire_lock(backend, "run-2")
    assert json.loads(backend.get(ss.LOCK)[0])["owner"] == "run-2"


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


def test_bundle_roundtrip_keeps_publishable_files_and_leaves_bulky_ones(backend, tmp_path) -> None:
    run = make_run(tmp_path)
    manifest = ss.save_bundle(backend, run, run.name, {"gate": {"ok": True}})
    assert manifest["gate"] == {"ok": True}
    out = tmp_path / "elsewhere" / run.name
    ss.load_bundle(backend, run.name, out)
    assert (out / "variation_reports/b.csv").is_file()
    assert (out / "publications/20261109T071157006575Z_preview/publication_report.json").is_file()
    assert not (out / "discovery/evidence").exists()  # saved retailer pages are not bundled
    assert not (out / "publications/20261109T075315178518Z_write").exists()


def test_a_bundle_that_fails_its_checksum_or_escapes_is_refused(backend, tmp_path) -> None:
    run = make_run(tmp_path)
    ss.save_bundle(backend, run, run.name, {})
    backend._path(f"runs/{run.name}/bundle.tar.gz").write_bytes(b"corrupt")
    with pytest.raises(ss.StateError, match="checksum"):
        ss.load_bundle(backend, run.name, tmp_path / "out")
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as archive:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ss.StateError, match="Unsafe"):
        ss.extract_bundle(evil, tmp_path / "dest")
    assert not (tmp_path / "escape.txt").exists()


def test_a_missing_bundle_cannot_be_published(backend, tmp_path) -> None:
    with pytest.raises(ss.StateError, match="no saved bundle"):
        ss.load_bundle(backend, "20260101T000000Z", tmp_path / "x")


def test_publication_attempts_are_appended_never_replaced(backend) -> None:
    ss.append_publication_attempt(backend, "PUB-1", {"status": "PARTIAL_WRITE"})
    record = ss.append_publication_attempt(backend, "PUB-1", {"status": "PUBLISHED"})
    assert [a["status"] for a in record["attempts"]] == ["PARTIAL_WRITE", "PUBLISHED"]
    assert record["last_status"] == "PUBLISHED"


def test_publication_backups_are_uploaded_durably(backend, tmp_path) -> None:
    folder = tmp_path / "write"
    folder.mkdir()
    (folder / "backup_a.json").write_text("{}")
    names = ss.upload_tree(backend, folder, "publications/PUB-1/t")
    assert names == ["publications/PUB-1/t/backup_a.json"] and backend.exists(names[0])


# configuration --------------------------------------------------------------------------------


def test_storage_must_be_configured_explicitly(tmp_path) -> None:
    with pytest.raises(ss.StateError, match="GCS_STATE_BUCKET"):
        ss.backend_from_env({})
    assert isinstance(
        ss.backend_from_env({"STATE_LOCAL_DIR": str(tmp_path / "s")}), ss.LocalBackend
    )


def test_gcs_backend_needs_a_bucket_name() -> None:
    with pytest.raises(ss.StateError):
        ss.GcsBackend("", client=object())


def test_copying_a_restored_workspace_helper_leaves_no_temp_files(backend, tmp_path) -> None:
    ss.seed(backend, make_root(tmp_path))
    restored_runner(backend, tmp_path)
    assert not list((tmp_path / "bucket").rglob("*.tmp"))
    shutil.rmtree(tmp_path / "runner")


# the Google Cloud Storage backend against a scripted client --------------------------------------


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name, self.generation = store, name, None

    def _exists(self):
        return self.name in self.store.objects

    def download_as_bytes(self):
        from google.api_core import exceptions

        if not self._exists():
            raise exceptions.NotFound("missing")
        return self.store.objects[self.name][0]

    def download_to_filename(self, path):
        Path(path).write_bytes(self.download_as_bytes())

    def reload(self):
        self.generation = self.store.objects[self.name][1]

    def _check(self, if_generation_match):
        from google.api_core import exceptions

        current = self.store.objects[self.name][1] if self._exists() else 0
        if if_generation_match is not None and if_generation_match != current:
            raise exceptions.PreconditionFailed("generation mismatch")
        return current

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        current = self._check(if_generation_match)
        self.store.objects[self.name] = (data, current + 1)
        self.generation = current + 1

    def upload_from_filename(self, path, if_generation_match=None):
        self.upload_from_string(Path(path).read_bytes(), if_generation_match=if_generation_match)

    def exists(self):
        return self._exists()

    def delete(self, if_generation_match=None):
        from google.api_core import exceptions

        if not self._exists():
            raise exceptions.NotFound("missing")
        self._check(if_generation_match)
        del self.store.objects[self.name]


class FakeBucket:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.client = self

    def blob(self, name):
        return FakeBlob(self, name)

    def bucket(self, name):
        return self

    def list_blobs(self, bucket, prefix=""):
        return [FakeBlob(self, n) for n in sorted(self.objects) if n.startswith(prefix)]


def test_the_gcs_backend_maps_preconditions_and_prefixes_every_object() -> None:
    fake = FakeBucket()
    backend = ss.GcsBackend("bucket", "tracker/prod", client=fake)
    assert backend.get("state/latest.json") is None
    generation = backend.put("state/latest.json", b"{}", if_generation_match=0)
    assert generation == 1 and "tracker/prod/state/latest.json" in fake.objects
    with pytest.raises(ss.PreconditionFailed):
        backend.put("state/latest.json", b"{}", if_generation_match=0)  # already exists
    assert backend.put("state/latest.json", b"{1}", if_generation_match=1) == 2
    with pytest.raises(ss.PreconditionFailed):
        backend.put("state/latest.json", b"{2}", if_generation_match=1)  # someone else won
    assert backend.get("state/latest.json") == (b"{1}", 2)
    assert backend.list("state/") == ["state/latest.json"]
    backend.delete("state/latest.json", if_generation_match=2)
    backend.delete("state/latest.json")  # deleting what is gone is not an error
    assert not backend.exists("state/latest.json")


def test_the_full_state_cycle_works_over_the_gcs_backend(tmp_path) -> None:
    backend = ss.GcsBackend("bucket", "p", client=FakeBucket())
    ss.seed(backend, make_root(tmp_path))
    runner = tmp_path / "runner"
    ss.restore(backend, runner)
    add_observation(runner / ss.STORE_REL, 55)
    assert ss.save(backend, runner, run_id="r")["new_observations"] == 1
    ss.acquire_lock(backend, "a")
    with pytest.raises(ss.StateError):
        ss.acquire_lock(backend, "b")
    assert ss.release_lock(backend, "a")
    first = ss.list_versions(backend)[0]
    with pytest.raises(Exception, match="generation"):  # versions are immutable: never overwritten
        backend.upload(f"state/versions/{first}/files/{ss.STORE_REL}", runner / ss.STORE_REL)
