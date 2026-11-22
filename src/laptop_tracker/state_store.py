"""Durable, versioned, encrypted persistence of the tracker's state in private GitHub Releases.

GitHub Releases are file storage, not a database, so the design is append-only:

* **State versions** are releases ``tracker-state-000001``, ``-000002`` ... Each holds a public
  ``manifest.json`` (sequence, run ID, parent link, checksum, key ID; no data) and the encrypted
  archive ``state.tar.gz.enc`` of the files that make the next run correct. A version is created as
  a draft, verified by downloading and decrypting it, and only then published. Only published,
  fully valid versions are state; drafts and half uploads are ignored. Nothing is ever overwritten.
* **Run bundles** (``tracker-run-<run id>``) hold the reports of one validated run so that a failed
  Sheets publication never needs another scrape.
* **Publication records** (``tracker-pub-<id>-aNN``) hold one publication attempt each, with the
  publisher's snapshots of the previous tabs for recovery.

There is no atomic compare-and-swap. Concurrent writers are kept apart by the Actions concurrency
group, by unique sequence numbers (a claim check on the draft, a parent check immediately before
publishing) and by read-back verification. The first state is created only by the explicit ``seed``
operation, never implicitly, and a key or integrity problem is always an error, never empty state.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from laptop_tracker import release_store as rs
from laptop_tracker import state_crypto as sc

STATE_SCHEMA = 2
STATE_TAG = "tracker-state-"
RUN_TAG = "tracker-run-"
PUB_TAG = "tracker-pub-"
MANIFEST_ASSET = "manifest.json"
STATE_ASSET = "state.tar.gz.enc"
BUNDLE_ASSET = "bundle.tar.gz.enc"
ATTEMPT_ASSET = "attempt.tar.gz.enc"
STALE_DRAFT_HOURS = 6  # longer than any job (180 min), so a younger draft may be a live writer
DEFAULT_WARN_MB = 1024
BASELINE_DIR = Path("data") / "history" / ".ci_baseline"
STORE_REL = "data/history/price_history.sqlite3"
REQUIRED_TABLES = (
    "store_meta",
    "import_batches",
    "observations",
    "batch_observations",
    "matching_contexts",
    "match_results",
)


class StateError(RuntimeError):
    """A state problem that must stop the run. Messages never contain credentials."""


@dataclass(frozen=True)
class StateFile:
    rel: str
    kind: str  # sqlite | csv | json
    required: bool = True


# Authoritative, not reproducible from Git or Sheets: observation history, the rate-limit
# cooldowns that keep retailer requests respectful across runs, and the owner's local decisions.
# Deliberately NOT saved: retailer pages, the 24-hour search cache (older than a week at the next
# run, so it can never be replayed) and generated reports (kept per run in the run bundle).
STATE_FILES: tuple[StateFile, ...] = (
    StateFile(STORE_REL, "sqlite"),
    StateFile("data/history/retailer_rate_limits.json", "json"),
    StateFile("config/duplicate_decisions.csv", "csv"),
    StateFile("config/eligibility_decisions.csv", "csv"),
    StateFile("config/identity_review_register.csv", "csv"),
)

# What a run bundle keeps: everything the publisher and the gate read, nothing bulky or raw
# (no saved retailer pages, no database copies).
BUNDLE_INCLUDE = (
    "status.json",
    "stages.jsonl",
    "as_of.txt",
    "driver.log",
    "inputs",
    "shopee_variations",
    "variation_reports",
    "context_reports",
    "discovery/collection",
    "discovery/discovery_summary.json",
    # The request ledger the readiness gate counts failures from (retailer_failure_policy.py): the
    # publish job must judge a stored run by the same files as the collect job (2026-11-22).
    "discovery/search_requests.csv",
    "discovery/product_inspections.csv",
    "alternative_audit",
    "selection_audit.log",
    "variation_audit.log",
    "publish_preview.log",
)


# --------------------------------------------------------------------------- validation


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def sqlite_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "observations",
                "import_batches",
                "batch_observations",
                "matching_contexts",
                "match_results",
            )
        }
    finally:
        connection.close()


def check_sqlite(path: Path) -> dict[str, int]:
    """Integrity, expected tables and schema version; returns the row counts."""
    if not path.is_file():
        raise StateError(f"{path.name} is missing.")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise StateError(f"{path.name} cannot be opened as SQLite ({exc}).") from exc
    try:
        try:
            verdict = [r[0] for r in connection.execute("PRAGMA integrity_check")]
            fk = connection.execute("PRAGMA foreign_key_check").fetchall()
            tables = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        except sqlite3.DatabaseError as exc:
            raise StateError(f"{path.name} is corrupt ({exc}).") from exc
        if verdict != ["ok"]:
            raise StateError(f"{path.name} failed PRAGMA integrity_check: {verdict[:3]}.")
        if fk:
            raise StateError(f"{path.name} has {len(fk)} foreign-key violation(s).")
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise StateError(f"{path.name} lacks the table(s) {', '.join(missing)}.")
        version = connection.execute(
            "SELECT value FROM store_meta WHERE key='schema_version'"
        ).fetchone()
        if version is None:
            raise StateError(f"{path.name} has no schema_version.")
    finally:
        connection.close()
    return sqlite_counts(path)


def check_state_file(spec: StateFile, path: Path) -> dict:
    """Validate one state file by its kind; returns facts for the manifest."""
    if not path.is_file():
        raise StateError(f"Required state file {spec.rel} is missing.")
    if spec.kind == "sqlite":
        return {"rows": check_sqlite(path)}
    text = path.read_text(encoding="utf-8-sig")
    if spec.kind == "json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateError(f"{spec.rel} is not valid JSON ({exc.msg}).") from exc
        if not isinstance(value, dict):
            raise StateError(f"{spec.rel} is not a JSON object.")
        return {}
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or not any(cell.strip() for cell in rows[0]):
        raise StateError(f"{spec.rel} has no header row.")
    width = len(rows[0])
    bad = [i for i, row in enumerate(rows[1:], 2) if row and len(row) > width]
    if bad:
        raise StateError(f"{spec.rel} has rows wider than its header (line {bad[0]}).")
    return {"data_rows": len(rows) - 1}


def check_append_only(baseline: Path, current: Path) -> None:
    """The current store must still contain every restored observation unchanged, with its
    original retrieval time and content digest, and no restored batch or context may vanish."""
    connection = sqlite3.connect(f"file:{current}?mode=ro", uri=True)
    try:
        connection.execute("ATTACH DATABASE ? AS base", (f"file:{baseline}?mode=ro",))
        changed = connection.execute(
            "SELECT COUNT(*) FROM base.observations b LEFT JOIN main.observations o "
            "ON o.observation_id = b.observation_id WHERE o.observation_id IS NULL "
            "OR o.record_sha256 IS NOT b.record_sha256 OR o.retrieved_at IS NOT b.retrieved_at "
            "OR o.offer_key IS NOT b.offer_key OR o.price IS NOT b.price"
        ).fetchone()[0]
        lost_batches = connection.execute(
            "SELECT COUNT(*) FROM base.import_batches b WHERE NOT EXISTS "
            "(SELECT 1 FROM main.import_batches o WHERE o.batch_id = b.batch_id)"
        ).fetchone()[0]
        lost_contexts = connection.execute(
            "SELECT COUNT(*) FROM base.matching_contexts b WHERE NOT EXISTS "
            "(SELECT 1 FROM main.matching_contexts o WHERE o.context_id = b.context_id)"
        ).fetchone()[0]
    except sqlite3.Error as exc:
        raise StateError(f"Cannot compare the store with its restored baseline ({exc}).") from exc
    finally:
        connection.close()
    if changed or lost_batches or lost_contexts:
        raise StateError(
            "The price history is no longer append-only relative to the restored state: "
            f"{changed} observation(s) altered or missing, {lost_batches} batch(es) and "
            f"{lost_contexts} context(s) lost. The new state was not saved."
        )


def snapshot_sqlite(source: Path, destination: Path) -> None:
    """A consistent copy through SQLite's online backup API (safe even if a journal exists)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
    except sqlite3.DatabaseError as exc:
        raise StateError(f"{source.name} cannot be read as a database ({exc}).") from exc
    finally:
        dst.close()
        src.close()


# --------------------------------------------------------------------------- archives


def _tar_gz(folder: Path, destination: Path) -> None:
    """A reproducible tar.gz of ``folder`` (sorted, no timestamps or owners)."""
    with open(destination, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode="w") as archive:
                for path in sorted(folder.rglob("*")):
                    info = archive.gettarinfo(
                        str(path), arcname=path.relative_to(folder).as_posix()
                    )
                    info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
                    if path.is_file():
                        with open(path, "rb") as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)


def _safe_extract(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if not (target == base or base in target.parents) or not (
                member.isfile() or member.isdir()
            ):
                raise StateError(f"Unsafe entry in archive: {member.name}")
        archive.extractall(destination, filter="data")


def _encrypt_file(
    source: Path, destination: Path, ring: sc.KeyRing, *, purpose: str, name: str
) -> None:
    destination.write_bytes(sc.encrypt_bytes(source.read_bytes(), ring, purpose=purpose, name=name))


def _decrypt_file(
    source: Path, destination: Path, ring: sc.KeyRing, key_id: str, *, purpose: str, name: str
) -> None:
    try:
        plain = sc.decrypt_bytes(source.read_bytes(), ring, key_id, purpose=purpose, name=name)
    except sc.CryptoError as exc:
        raise StateError(str(exc)) from exc
    destination.write_bytes(plain)


def _release_error(action: str, exc: Exception) -> StateError:
    return StateError(f"{action}: {exc}")


# --------------------------------------------------------------------------- generic commit


def _parse_time(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _is_stale(release: rs.Release, now: datetime) -> bool:
    created = _parse_time(release.created_at)
    return created is None or now - created > timedelta(hours=STALE_DRAFT_HOURS)


def _same_tag(store: rs.ReleaseStore, tag: str) -> list[rs.Release]:
    return [r for r in store.list_releases(tag) if r.tag == tag]


def commit_release(
    store: rs.ReleaseStore,
    tag: str,
    title: str,
    assets: list[tuple[str, Path]],
    *,
    verify: Callable[[rs.Release], None] | None = None,
    before_publish: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> rs.Release:
    """Create ``tag`` as a draft, claim it, upload and verify every asset, then publish.

    The release only becomes visible as state when this returns. A draft left by a crashed run is
    ignored by every reader and removed here once it is old enough to be certainly dead; a young
    draft with the same tag means another writer may be active, which stops this one. If two
    writers do create drafts at the same moment, the lowest release ID wins and the other stops.
    """
    now = now or _utc_now()
    try:
        for existing in _same_tag(store, tag):
            if not existing.draft:
                raise StateError(f"{tag} already exists as a published release.")
            if not _is_stale(existing, now):
                raise StateError(
                    f"Another writer is working on {tag} (draft from {existing.created_at}). "
                    "Wait for it to finish; a draft older than "
                    f"{STALE_DRAFT_HOURS} hours is treated as abandoned."
                )
            store.delete_draft(existing)
        draft = store.create_draft(tag, title, "Automated tracker storage. Do not edit or delete.")
    except rs.ReleaseStoreError as exc:
        raise _release_error(f"Could not create {tag}", exc) from exc
    try:
        competitors = [r for r in _same_tag(store, tag) if r.id != draft.id]
        if any(r.id < draft.id or not r.draft for r in competitors):
            raise StateError(f"Another writer claimed {tag} first; this run stops without saving.")
        for name, path in assets:
            store.upload_asset(draft, name, path)
        fresh = next((r for r in _same_tag(store, tag) if r.id == draft.id), None)
        if fresh is None:
            raise StateError(f"{tag} disappeared while it was being written.")
        for name, path in assets:
            attached = fresh.asset(name)
            if attached is None or attached.size != Path(path).stat().st_size:
                raise StateError(f"{name} of {tag} was not stored completely.")
        if verify is not None:
            verify(fresh)
        late = [r for r in _same_tag(store, tag) if r.id != draft.id]
        if any(r.id < draft.id or not r.draft for r in late):
            raise StateError(f"Another writer claimed {tag} meanwhile; nothing was published.")
        if before_publish is not None:
            before_publish()
        published = store.publish(fresh)
    except Exception as exc:
        try:
            store.delete_draft(draft)
        except rs.ReleaseStoreError:
            pass  # a leftover draft is harmless: nothing reads drafts, and it expires
        if isinstance(exc, rs.ReleaseStoreError):
            raise _release_error(f"Could not store {tag}", exc) from exc
        raise
    return published


def _download(
    store: rs.ReleaseStore, release: rs.Release, name: str, destination: Path, expected_sha: str
) -> None:
    try:
        store.download_asset(release, name, destination)
    except rs.ReleaseStoreError as exc:
        raise _release_error(f"Could not download {name} of {release.tag}", exc) from exc
    if sha256_file(destination) != expected_sha:
        raise StateError(f"{name} of {release.tag} fails its checksum.")


def _read_json_asset(store: rs.ReleaseStore, release: rs.Release, name: str) -> dict:
    if release.asset(name) is None:
        raise StateError(f"{release.tag} has no {name} (incomplete).")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / name
        try:
            store.download_asset(release, name, target)
        except rs.ReleaseStoreError as exc:
            raise _release_error(f"Could not read {name} of {release.tag}", exc) from exc
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StateError(f"{name} of {release.tag} is not valid JSON.") from exc


# --------------------------------------------------------------------------- state versions


@dataclass
class Baseline:
    """What was restored; written beside the store so the later save can compare against it."""

    version_id: str
    seq: int
    manifest_sha256: str
    counts: dict[str, int] = field(default_factory=dict)

    def write(self, root: Path) -> None:
        folder = root / BASELINE_DIR
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "baseline.json").write_text(
            json.dumps(self.__dict__, indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def read(cls, root: Path) -> Baseline:
        path = root / BASELINE_DIR / "baseline.json"
        if not path.is_file():
            raise StateError(
                "No restore baseline found: state was not restored in this workspace, so a new "
                "version cannot be saved safely."
            )
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def state_tag(seq: int) -> str:
    return f"{STATE_TAG}{seq:06d}"


def _seq_of(tag: str) -> int | None:
    match = re.fullmatch(rf"{STATE_TAG}(\d{{6}})", tag)
    return int(match.group(1)) if match else None


def published_versions(store: rs.ReleaseStore) -> list[tuple[int, rs.Release]]:
    """Published state releases, oldest first. Drafts and unrelated tags are never state."""
    try:
        releases = store.list_releases(STATE_TAG)
    except rs.ReleaseStoreError as exc:
        raise _release_error("Could not list the state releases", exc) from exc
    found = [(s, r) for r in releases if not r.draft and (s := _seq_of(r.tag)) is not None]
    return sorted(found, key=lambda item: item[0])


def read_public_manifest(store: rs.ReleaseStore, release: rs.Release) -> dict:
    """The manifest of a state release, checked against the assets actually attached."""
    manifest = _read_json_asset(store, release, MANIFEST_ASSET)
    seq = _seq_of(release.tag)
    if manifest.get("kind") != "tracker-state" or manifest.get("seq") != seq:
        raise StateError(f"{release.tag} has an unexpected manifest.")
    if manifest.get("state_schema") != STATE_SCHEMA:
        raise StateError(f"{release.tag} uses an unsupported state schema.")
    archive = manifest.get("archive", {})
    attached = release.asset(archive.get("name", STATE_ASSET))
    if attached is None or attached.size != archive.get("bytes"):
        raise StateError(f"{release.tag} is incomplete: its archive is missing or truncated.")
    return manifest


def _select(store: rs.ReleaseStore, version: str | int | None) -> tuple[int, rs.Release]:
    versions = published_versions(store)
    if not versions:
        raise StateError(
            "No saved state exists. An empty production history is never created automatically: "
            "seed the first state with 'python scripts/ci_state.py seed' from the machine that "
            "holds the real history."
        )
    if version is None:
        return versions[-1]
    wanted = int(str(version).removeprefix(STATE_TAG))
    for seq, release in versions:
        if seq == wanted:
            return seq, release
    raise StateError(f"State version {wanted} does not exist or is not published.")


def _build_archive(root: Path, staging: Path) -> tuple[Path, dict]:
    """Snapshot, validate and pack the state files. Returns the plaintext archive and the inner
    manifest (per-file checksums and row counts)."""
    payload = staging / "payload"
    files: dict[str, dict] = {}
    for spec in STATE_FILES:
        live = root / spec.rel
        if not live.is_file():
            if spec.required:
                raise StateError(f"Required state file {spec.rel} is missing; nothing was saved.")
            continue
        copy = payload / "files" / spec.rel
        copy.parent.mkdir(parents=True, exist_ok=True)
        if spec.kind == "sqlite":
            snapshot_sqlite(live, copy)
        else:
            shutil.copyfile(live, copy)
        facts = check_state_file(spec, copy)
        files[spec.rel] = {
            "sha256": sha256_file(copy),
            "bytes": copy.stat().st_size,
            "kind": spec.kind,
            "required": spec.required,
            **facts,
        }
    inner = {"state_schema": STATE_SCHEMA, "files": files}
    (payload / "inner_manifest.json").write_text(
        json.dumps(inner, indent=2, sort_keys=True), encoding="utf-8"
    )
    archive = staging / "state.tar.gz"
    _tar_gz(payload, archive)
    return archive, inner


def _content_sha(inner: dict) -> str:
    lines = "\n".join(f"{rel}:{meta['sha256']}" for rel, meta in sorted(inner["files"].items()))
    return hashlib.sha256(lines.encode()).hexdigest()


def _open_archive(
    store: rs.ReleaseStore, release: rs.Release, manifest: dict, ring: sc.KeyRing, staging: Path
) -> dict:
    """Download, verify, decrypt, unpack and validate one version into ``staging/files``.
    Returns the inner manifest. Any failure raises; nothing is restored on a partial check."""
    archive = manifest["archive"]
    blob = staging / "download.enc"
    _download(store, release, archive["name"], blob, archive["sha256"])
    plain = staging / "state.tar.gz"
    _decrypt_file(
        blob,
        plain,
        ring,
        manifest["encryption"]["key_id"],
        purpose="state",
        name=release.tag,
    )
    unpacked = staging / "unpacked"
    _safe_extract(plain, unpacked)
    try:
        inner = json.loads((unpacked / "inner_manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"{release.tag} has no readable inner manifest.") from exc
    missing = [s.rel for s in STATE_FILES if s.required and s.rel not in inner["files"]]
    if missing:
        raise StateError(f"{release.tag} lacks required file(s): {', '.join(missing)}.")
    if (
        inner.get("state_schema") != STATE_SCHEMA
        or _content_sha(inner) != manifest["content_sha256"]
    ):
        raise StateError(f"{release.tag} content does not match its manifest.")
    for rel, meta in inner["files"].items():
        target = unpacked / "files" / rel
        if not target.is_file() or sha256_file(target) != meta["sha256"]:
            raise StateError(f"{rel} of {release.tag} fails its checksum.")
        spec = next((s for s in STATE_FILES if s.rel == rel), StateFile(rel, meta["kind"]))
        check_state_file(spec, target)
    return inner


def _public_manifest(
    seq: int,
    run_id: str,
    inner: dict,
    archive: Path,
    ring: sc.KeyRing,
    parent: dict | None,
    note: str,
    extra: dict | None = None,
) -> dict:
    return {
        "kind": "tracker-state",
        "state_schema": STATE_SCHEMA,
        "seq": seq,
        "run_id": run_id,
        "created_at_utc": _utc_now().isoformat(timespec="seconds"),
        "note": note,
        "github_run": os.environ.get("GITHUB_RUN_ID", ""),
        "parent_seq": parent["seq"] if parent else None,
        "parent_manifest_sha256": parent["manifest_sha256"] if parent else None,
        "content_sha256": _content_sha(inner),
        "archive": {
            "name": STATE_ASSET,
            "sha256": sha256_file(archive),
            "bytes": archive.stat().st_size,
        },
        "encryption": {"algorithm": "AES-256-GCM", "key_id": ring.current_id},
        **(extra or {}),
    }


def _write_version(
    store: rs.ReleaseStore,
    ring: sc.KeyRing,
    plain_archive: Path,
    inner: dict,
    *,
    run_id: str,
    note: str,
    expected_latest: int,
    extra: dict | None = None,
    staging: Path,
) -> tuple[int, dict]:
    """Publish the next sequence number (``expected_latest`` + 1) holding ``plain_archive``."""
    versions = published_versions(store)
    latest_seq = versions[-1][0] if versions else 0
    if latest_seq != expected_latest:
        raise StateError(
            f"The saved state moved from version {expected_latest} to {latest_seq} while this "
            "run was working (another run or a seed saved first). Nothing was saved."
        )
    seq = latest_seq + 1
    tag = state_tag(seq)
    parent = None
    if versions:
        parent_release = versions[-1][1]
        parent_blob = _read_json_asset(store, parent_release, MANIFEST_ASSET)
        parent = {
            "seq": latest_seq,
            "manifest_sha256": _json_sha(parent_blob),
        }
    encrypted = staging / STATE_ASSET
    _encrypt_file(plain_archive, encrypted, ring, purpose="state", name=tag)
    manifest = _public_manifest(seq, run_id, inner, encrypted, ring, parent, note, extra)
    manifest_path = staging / MANIFEST_ASSET
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    def verify(release: rs.Release) -> None:
        # Read back exactly what was stored and prove it restores, before it can become state.
        checked = read_public_manifest(store, release)
        with tempfile.TemporaryDirectory() as probe:
            _open_archive(store, release, checked, ring, Path(probe))

    def still_latest() -> None:
        current = published_versions(store)
        if (current[-1][0] if current else 0) != latest_seq:
            raise StateError("The saved state changed just before publishing; nothing was saved.")

    try:
        commit_release(
            store,
            tag,
            f"Tracker state {seq:06d}",
            [(STATE_ASSET, encrypted), (MANIFEST_ASSET, manifest_path)],
            verify=verify,
            before_publish=still_latest,
        )
    except rs.ReleaseStoreError as exc:  # pragma: no cover - commit_release maps these already
        raise _release_error(f"Could not save {tag}", exc) from exc
    return seq, manifest


def _json_sha(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, indent=2, sort_keys=True).encode()).hexdigest()


def seed(store: rs.ReleaseStore, ring: sc.KeyRing, root: Path, *, run_id: str = "seed") -> dict:
    """Create the very first state version from local files. Refuses to replace existing state,
    and proves the stored version restores to exactly the local bytes."""
    if published_versions(store):
        raise StateError(
            "State already exists; seed only creates the first version. "
            "Use restore and save for normal operation."
        )
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        archive, inner = _build_archive(root, staging)
        seq, manifest = _write_version(
            store,
            ring,
            archive,
            inner,
            run_id=run_id,
            note="first state, seeded from local files",
            expected_latest=0,
            staging=staging,
        )
    check = restore_check(store, ring, root, seq)
    return {
        "version_id": state_tag(seq),
        "seq": seq,
        "restore_check": check,
        "files": inner["files"],
    }


def restore_check(store: rs.ReleaseStore, ring: sc.KeyRing, root: Path, seq: int) -> str:
    """Restore a published version into a scratch folder and compare it with the local files."""
    _, release = _select(store, seq)
    manifest = read_public_manifest(store, release)
    with tempfile.TemporaryDirectory() as tmp:
        inner = _open_archive(store, release, manifest, ring, Path(tmp))
        for rel, meta in inner["files"].items():
            local = root / rel
            if rel == STORE_REL:
                continue  # a database copy differs byte-wise by design; integrity was checked
            if local.is_file() and sha256_file(local) != meta["sha256"]:
                raise StateError(f"{rel} restores differently from the local file.")
    return "ok"


def restore(
    store: rs.ReleaseStore,
    ring: sc.KeyRing,
    root: Path,
    *,
    version: str | int | None = None,
    overwrite: bool = False,
) -> dict:
    """Restore the latest (or a named) published, valid state into ``root``. Everything is
    downloaded, decrypted and validated in a staging folder before any file in ``root`` is
    replaced. If the newest published version is damaged this fails instead of quietly using an
    older one: roll back explicitly with ``rollback``."""
    seq, release = _select(store, version)
    manifest = read_public_manifest(store, release)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        inner = _open_archive(store, release, manifest, ring, staging)
        if not overwrite:
            present = [rel for rel in inner["files"] if (root / rel).exists()]
            if present:
                raise StateError(
                    f"Refusing to overwrite existing local file(s): {', '.join(present)} "
                    "(pass --overwrite to replace them)."
                )
        for rel in inner["files"]:
            destination = root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staging / "unpacked" / "files" / rel, destination)
        baseline_copy = root / BASELINE_DIR / "price_history.sqlite3"
        baseline_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / STORE_REL, baseline_copy)
    counts = inner["files"][STORE_REL]["rows"]
    # Restored evidence keeps its original retrieval times: files are copied byte for byte.
    Baseline(release.tag, seq, _json_sha(manifest), counts).write(root)
    return {
        "version_id": release.tag,
        "seq": seq,
        "created_at_utc": manifest["created_at_utc"],
        "files": sorted(inner["files"]),
        "rows": counts,
    }


def save(
    store: rs.ReleaseStore, ring: sc.KeyRing, root: Path, *, run_id: str, note: str = ""
) -> dict:
    """Save the post-run state as a new version if (and only if) it validates, is append-only
    relative to the restored baseline and nobody saved meanwhile. Saving the same content for the
    same run twice returns the existing version instead of creating a duplicate."""
    baseline = Baseline.read(root)
    check_sqlite(root / STORE_REL)
    check_append_only(root / BASELINE_DIR / "price_history.sqlite3", root / STORE_REL)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        archive, inner = _build_archive(root, staging)
        versions = published_versions(store)
        if versions and versions[-1][0] == baseline.seq + 1:
            latest_manifest = read_public_manifest(store, versions[-1][1])
            if latest_manifest["run_id"] == run_id and latest_manifest[
                "content_sha256"
            ] == _content_sha(inner):
                rows = inner["files"][STORE_REL]["rows"]
                return _save_result(latest_manifest["seq"], baseline, rows, already=True)
        seq, _ = _write_version(
            store,
            ring,
            archive,
            inner,
            run_id=run_id,
            note=note or "saved after a validated run",
            expected_latest=baseline.seq,
            staging=staging,
        )
    return _save_result(seq, baseline, inner["files"][STORE_REL]["rows"], already=False)


def _save_result(seq: int, baseline: Baseline, rows: dict, *, already: bool) -> dict:
    return {
        "version_id": state_tag(seq),
        "seq": seq,
        "previous_version_id": baseline.version_id,
        "rows": rows,
        "rows_before": baseline.counts,
        "new_observations": rows["observations"] - baseline.counts.get("observations", 0),
        "already_saved": already,
    }


def rollback(store: rs.ReleaseStore, ring: sc.KeyRing, target: str | int) -> dict:
    """Make an earlier version the latest again by publishing a NEW version with its contents
    (immutable releases are never edited or deleted, so the damaged version stays for
    inspection). The target is fully downloaded, decrypted and validated first."""
    versions = published_versions(store)
    if not versions:
        raise StateError("No saved state exists.")
    latest_seq = versions[-1][0]
    seq, release = _select(store, target)
    manifest = read_public_manifest(store, release)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        inner = _open_archive(store, release, manifest, ring, staging / "source")
        plain = staging / "source" / "unpacked"
        archive = staging / "rollback.tar.gz"
        _tar_gz(plain, archive)
        new_seq, _ = _write_version(
            store,
            ring,
            archive,
            inner,
            run_id=f"rollback-to-{seq:06d}",
            note=f"rollback: contents of {release.tag}",
            expected_latest=latest_seq,
            extra={"rollback_of": seq},
            staging=staging,
        )
    return {"version_id": state_tag(new_seq), "seq": new_seq, "rolled_back_to": release.tag}


def verify_version(
    store: rs.ReleaseStore, ring: sc.KeyRing, version: str | int | None = None
) -> dict:
    """Fully validate a published version (download, checksum, decrypt, integrity) without
    touching the workspace."""
    seq, release = _select(store, version)
    manifest = read_public_manifest(store, release)
    with tempfile.TemporaryDirectory() as tmp:
        inner = _open_archive(store, release, manifest, ring, Path(tmp))
    return {
        "version_id": release.tag,
        "seq": seq,
        "valid": True,
        "rows": inner["files"][STORE_REL]["rows"],
        "created_at_utc": manifest["created_at_utc"],
    }


def list_versions(store: rs.ReleaseStore) -> list[dict]:
    """Published versions with cheap metadata (no downloads of archives)."""
    out = []
    for seq, release in published_versions(store):
        entry = {
            "seq": seq,
            "tag": release.tag,
            "created_at": release.created_at,
            "bytes": sum(a.size for a in release.assets),
            "immutable": release.immutable,
        }
        try:
            manifest = read_public_manifest(store, release)
            entry.update(run_id=manifest["run_id"], complete=True)
        except StateError as exc:
            entry.update(run_id="", complete=False, problem=str(exc))
        out.append(entry)
    return out


def storage_report(store: rs.ReleaseStore, *, warn_mb: int = DEFAULT_WARN_MB) -> dict:
    """Size and growth of everything the tracker keeps in Releases (monitoring only)."""
    try:
        releases = store.list_releases("tracker-")
    except rs.ReleaseStoreError as exc:
        raise _release_error("Could not list releases", exc) from exc
    groups: dict[str, dict] = {}
    for kind, prefix in (("state", STATE_TAG), ("runs", RUN_TAG), ("publications", PUB_TAG)):
        mine = [r for r in releases if r.tag.startswith(prefix) and not r.draft]
        sizes = [sum(a.size for a in r.assets) for r in mine]
        groups[kind] = {
            "releases": len(mine),
            "bytes": sum(sizes),
            "average_bytes": int(sum(sizes) / len(sizes)) if sizes else 0,
        }
    drafts = [r.tag for r in releases if r.draft]
    total = sum(g["bytes"] for g in groups.values())
    warnings = []
    if total > warn_mb * 1024 * 1024:
        warnings.append(
            f"Stored data is {total / 1048576:.0f} MB, above the {warn_mb} MB review threshold."
        )
    if drafts:
        warnings.append(
            f"{len(drafts)} unfinished draft release(s) (ignored as state): {', '.join(drafts)}."
        )
    return {"groups": groups, "total_bytes": total, "drafts": drafts, "warnings": warnings}


# --------------------------------------------------------------------------- run bundles


def create_bundle(run_dir: Path, destination: Path) -> dict:
    """A tar.gz of the files of a run the publisher and gate need (see ``BUNDLE_INCLUDE``)."""
    entries: list[str] = []
    with tarfile.open(destination, "w:gz") as archive:
        for rel in BUNDLE_INCLUDE:
            path = run_dir / rel
            if path.exists():
                archive.add(path, arcname=rel)
                entries.append(rel)
        for preview in sorted((run_dir / "publications").glob("*_preview")):
            report = preview / "publication_report.json"
            if report.is_file():
                arc = f"publications/{preview.name}/publication_report.json"
                archive.add(report, arcname=arc)
                entries.append(arc)
    return {
        "entries": entries,
        "sha256": sha256_file(destination),
        "bytes": destination.stat().st_size,
    }


def run_tag(run_id: str) -> str:
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", run_id):
        raise StateError(f"'{run_id}' is not a run folder name (expected like 20261109T065857Z).")
    return f"{RUN_TAG}{run_id}"


def save_bundle(
    store: rs.ReleaseStore, ring: sc.KeyRing, run_dir: Path, run_id: str, record: dict | None = None
) -> dict:
    """Store the validated run's bundle (encrypted) before anything is published. Saving a run
    that is already stored returns the stored manifest."""
    tag = run_tag(run_id)
    existing = [r for r in _same_tag(store, tag) if not r.draft]
    if existing:
        return _read_json_asset(store, existing[0], MANIFEST_ASSET)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        plain = staging / "bundle.tar.gz"
        info = create_bundle(run_dir, plain)
        encrypted = staging / BUNDLE_ASSET
        _encrypt_file(plain, encrypted, ring, purpose="bundle", name=tag)
        manifest = {
            "kind": "tracker-run",
            "state_schema": STATE_SCHEMA,
            "run_id": run_id,
            "created_at_utc": _utc_now().isoformat(timespec="seconds"),
            "github_run": os.environ.get("GITHUB_RUN_ID", ""),
            "bundle": {
                "name": BUNDLE_ASSET,
                "sha256": sha256_file(encrypted),
                "bytes": encrypted.stat().st_size,
                "plain_sha256": info["sha256"],
                "entries": len(info["entries"]),
            },
            "encryption": {"algorithm": "AES-256-GCM", "key_id": ring.current_id},
            **(record or {}),
        }
        manifest_path = staging / MANIFEST_ASSET
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

        def verify(release: rs.Release) -> None:
            with tempfile.TemporaryDirectory() as probe:
                _download(
                    store, release, BUNDLE_ASSET, Path(probe) / "b", manifest["bundle"]["sha256"]
                )

        commit_release(
            store,
            tag,
            f"Run bundle {run_id}",
            [(BUNDLE_ASSET, encrypted), (MANIFEST_ASSET, manifest_path)],
            verify=verify,
        )
    return manifest


def load_bundle(store: rs.ReleaseStore, ring: sc.KeyRing, run_id: str, destination: Path) -> dict:
    tag = run_tag(run_id)
    releases = [r for r in _same_tag(store, tag) if not r.draft]
    if not releases:
        raise StateError(f"Run {run_id} has no saved bundle; it cannot be published.")
    release = releases[0]
    manifest = _read_json_asset(store, release, MANIFEST_ASSET)
    if manifest.get("kind") != "tracker-run" or manifest.get("run_id") != run_id:
        raise StateError(f"The manifest of {tag} is unexpected.")
    with tempfile.TemporaryDirectory() as tmp:
        blob = Path(tmp) / "bundle.enc"
        _download(store, release, BUNDLE_ASSET, blob, manifest["bundle"]["sha256"])
        plain = Path(tmp) / "bundle.tar.gz"
        _decrypt_file(
            blob, plain, ring, manifest["encryption"]["key_id"], purpose="bundle", name=tag
        )
        _safe_extract(plain, destination)
    return manifest


# --------------------------------------------------------------------------- publication records


def _pub_slug(publication_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", publication_id)[:80]
    if not slug:
        raise StateError("Empty publication ID.")
    return slug


def _attempt_releases(store: rs.ReleaseStore, publication_id: str) -> list[rs.Release]:
    prefix = f"{PUB_TAG}{_pub_slug(publication_id)}-a"
    return [r for r in store.list_releases(prefix) if not r.draft]


def read_publication_attempts(
    store: rs.ReleaseStore, ring: sc.KeyRing, publication_id: str
) -> list[dict]:
    """Every recorded attempt of a publication, oldest first (decrypted records)."""
    records = []
    for release in sorted(_attempt_releases(store, publication_id), key=lambda r: r.tag):
        with tempfile.TemporaryDirectory() as tmp:
            records.append(_open_attempt(store, ring, release, Path(tmp)))
    return records


def _open_attempt(
    store: rs.ReleaseStore, ring: sc.KeyRing, release: rs.Release, destination: Path
) -> dict:
    """Download, verify, decrypt and unpack one attempt into ``destination``; returns its record."""
    manifest = _read_json_asset(store, release, MANIFEST_ASSET)
    with tempfile.TemporaryDirectory() as tmp:
        blob = Path(tmp) / "a.enc"
        _download(store, release, ATTEMPT_ASSET, blob, manifest["archive"]["sha256"])
        plain = Path(tmp) / "a.tar.gz"
        _decrypt_file(
            blob, plain, ring, manifest["encryption"]["key_id"], purpose="attempt", name=release.tag
        )
        _safe_extract(plain, destination)
    return json.loads((destination / "record.json").read_text("utf-8"))


def fetch_publication_attempt(
    store: rs.ReleaseStore,
    ring: sc.KeyRing,
    publication_id: str,
    destination: Path,
    attempt: int | None = None,
) -> dict:
    """Unpack a recorded attempt (default: the latest) with the publisher's snapshots of the tabs
    it replaced. Point ``publish_variation_prices.py --rollback`` at the ``snapshots`` folder to
    restore them."""
    releases = sorted(_attempt_releases(store, publication_id), key=lambda r: r.tag)
    if not releases:
        raise StateError(f"No recorded attempt for publication {publication_id}.")
    if attempt is None:
        release = releases[-1]
    else:
        wanted = f"-a{attempt:02d}"
        release = next((r for r in releases if r.tag.endswith(wanted)), None)
        if release is None:
            raise StateError(f"Publication {publication_id} has no attempt {attempt}.")
    record = _open_attempt(store, ring, release, destination)
    return {"tag": release.tag, "record": record, "snapshots": str(destination / "snapshots")}


def append_publication_attempt(
    store: rs.ReleaseStore,
    ring: sc.KeyRing,
    publication_id: str,
    attempt: dict,
    snapshots: Path | None = None,
) -> dict:
    """Record one publication attempt as its own immutable release (never edited), together with
    the publisher's snapshots of the previous tabs. The attempt number is claimed like any tag,
    so two writers cannot lose each other's records."""
    slug = _pub_slug(publication_id)
    last_error: StateError | None = None
    for _ in range(3):
        existing = _attempt_releases(store, publication_id)
        number = len(existing) + 1
        tag = f"{PUB_TAG}{slug}-a{number:02d}"
        record = {**attempt, "publication_id": publication_id, "attempt": number}
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            payload = staging / "payload"
            payload.mkdir()
            (payload / "record.json").write_text(
                json.dumps(record, indent=2, default=str), encoding="utf-8"
            )
            if snapshots is not None and snapshots.is_dir():
                shutil.copytree(snapshots, payload / "snapshots")
            plain = staging / "a.tar.gz"
            _tar_gz(payload, plain)
            encrypted = staging / ATTEMPT_ASSET
            _encrypt_file(plain, encrypted, ring, purpose="attempt", name=tag)
            manifest = {
                "kind": "tracker-publication-attempt",
                "state_schema": STATE_SCHEMA,
                "publication_id": publication_id,
                "attempt": number,
                "run_id": attempt.get("run_id", ""),
                "status": attempt.get("status", ""),
                "created_at_utc": _utc_now().isoformat(timespec="seconds"),
                "archive": {
                    "name": ATTEMPT_ASSET,
                    "sha256": sha256_file(encrypted),
                    "bytes": encrypted.stat().st_size,
                },
                "encryption": {"algorithm": "AES-256-GCM", "key_id": ring.current_id},
            }
            manifest_path = staging / MANIFEST_ASSET
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

            def verify(release: rs.Release, manifest=manifest, staging=staging) -> None:
                _download(
                    store, release, ATTEMPT_ASSET, staging / "probe", manifest["archive"]["sha256"]
                )

            try:
                commit_release(
                    store,
                    tag,
                    f"Publication attempt {slug} #{number}",
                    [(ATTEMPT_ASSET, encrypted), (MANIFEST_ASSET, manifest_path)],
                    verify=verify,
                )
                return record
            except StateError as exc:
                last_error = exc
    raise StateError(f"Could not record the publication attempt of {publication_id}: {last_error}")


def latest_successful_publication(store: rs.ReleaseStore, ring: sc.KeyRing) -> dict | None:
    """The most recent attempt record that ended in a confirmed publication (for the optional
    coverage regression guard), or None."""
    try:
        releases = [r for r in store.list_releases(PUB_TAG) if not r.draft]
    except rs.ReleaseStoreError as exc:
        raise _release_error("Could not list publication records", exc) from exc
    for release in sorted(releases, key=lambda r: r.id, reverse=True):
        manifest = _read_json_asset(store, release, MANIFEST_ASSET)
        if manifest.get("status") not in ("PUBLISHED", "SUCCEEDED_UNREPORTED"):
            continue
        records = read_publication_attempts(store, ring, manifest["publication_id"])
        for record in reversed(records):
            if record.get("status") in ("PUBLISHED", "SUCCEEDED_UNREPORTED"):
                return record
    return None


# --------------------------------------------------------------------------- configuration


def store_and_keys(env: dict[str, str] | None = None) -> tuple[rs.ReleaseStore, sc.KeyRing]:
    """The release store and encryption keys from the environment. Missing configuration is an
    error: there is no unencrypted or empty fallback."""
    env = dict(os.environ) if env is None else env
    try:
        ring = sc.KeyRing.from_env(env)
        store = rs.store_from_env(env)
    except (sc.CryptoError, rs.ReleaseStoreError) as exc:
        raise StateError(str(exc)) from exc
    return store, ring


def write_step_summary(text: str) -> None:
    """Append Markdown to the GitHub job summary when running in Actions, else print it."""
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)
