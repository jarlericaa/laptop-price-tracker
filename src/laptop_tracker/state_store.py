"""Durable, versioned persistence of the tracker's state between ephemeral CI runners.

Two kinds of state are kept apart on purpose:

* **Collection state** (``state/``): the files that make the next run correct, namely the price
  history SQLite store, the retailer rate-limit state and the owner decision files. It is saved as
  an immutable, checksummed version, and a small ``state/latest.json`` pointer is moved to it last,
  by compare-and-swap, only after every file was uploaded, read back and validated.
* **Run bundles and publication records** (``runs/``, ``publications/``): the reports of one run
  and what happened when it was published. A Sheets publication failure therefore never requires
  collecting again: the bundle of the already validated run is published again.

The store never invents state. A missing pointer, manifest, file or checksum is an error; the very
first state is created only by the explicit ``seed`` operation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

STATE_SCHEMA = 1
POINTER = "state/latest.json"
LOCK = "locks/state.lock.json"
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
    "alternative_audit",
    "selection_audit.log",
    "variation_audit.log",
    "publish_preview.log",
)


# --------------------------------------------------------------------------- backends


class Backend(Protocol):
    def get(self, name: str) -> tuple[bytes, int] | None: ...
    def put(self, name: str, data: bytes, if_generation_match: int | None = None) -> int: ...
    def download(self, name: str, path: Path) -> None: ...
    def upload(self, name: str, path: Path) -> None: ...
    def exists(self, name: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, name: str, if_generation_match: int | None = None) -> None: ...


class PreconditionFailed(StateError):
    """A compare-and-swap lost: the object changed (or exists) since it was read."""


class LocalBackend:
    """A directory used as an object store (tests, local rehearsal). Generations are counters."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.root / name

    def _gen_path(self, name: str) -> Path:
        return self.root / (name + ".generation")

    def _generation(self, name: str) -> int:
        gen = self._gen_path(name)
        return int(gen.read_text()) if self._path(name).is_file() and gen.is_file() else 0

    def get(self, name):
        path = self._path(name)
        if not path.is_file():
            return None
        return path.read_bytes(), self._generation(name)

    def put(self, name, data, if_generation_match=None):
        current = self._generation(name)
        if if_generation_match is not None and if_generation_match != current:
            raise PreconditionFailed(f"{name} changed (generation {current}).")
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self._gen_path(name).write_text(str(current + 1))
        return current + 1

    def download(self, name, path):
        source = self._path(name)
        if not source.is_file():
            raise StateError(f"{name} is missing from the state store.")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)

    def upload(self, name, path):
        self.put(name, Path(path).read_bytes())

    def exists(self, name):
        return self._path(name).is_file()

    def list(self, prefix):
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file()
            and not p.name.endswith((".generation", ".tmp"))
            and p.relative_to(self.root).as_posix().startswith(prefix)
        )

    def delete(self, name, if_generation_match=None):
        if if_generation_match is not None and if_generation_match != self._generation(name):
            raise PreconditionFailed(f"{name} changed.")
        self._path(name).unlink(missing_ok=True)
        self._gen_path(name).unlink(missing_ok=True)


class GcsBackend:
    """Google Cloud Storage with generation preconditions. Uses Application Default Credentials
    (Workload Identity Federation in CI), so no key is read or stored."""

    def __init__(self, bucket: str, prefix: str = "", client=None) -> None:
        if not bucket:
            raise StateError("GCS_STATE_BUCKET is not set.")
        if client is None:
            try:
                from google.cloud import storage
            except ImportError as exc:  # pragma: no cover - environment problem
                raise StateError("google-cloud-storage is not installed.") from exc
            # Object access needs no project. google-github-actions/auth exports one when it can
            # derive it; otherwise a placeholder keeps the client from probing for a default.
            project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT")
            client = storage.Client(project=project or "object-access-only")
        self.bucket = client.bucket(bucket)
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _blob(self, name: str):
        return self.bucket.blob(self.prefix + name)

    def get(self, name):
        from google.api_core import exceptions

        blob = self._blob(name)
        try:
            data = blob.download_as_bytes()
        except exceptions.NotFound:
            return None
        blob.reload()
        return data, int(blob.generation)

    def put(self, name, data, if_generation_match=None):
        from google.api_core import exceptions

        blob = self._blob(name)
        try:
            blob.upload_from_string(
                data, content_type="application/json", if_generation_match=if_generation_match
            )
        except exceptions.PreconditionFailed as exc:
            raise PreconditionFailed(f"{name} changed since it was read.") from exc
        return int(blob.generation)

    def download(self, name, path):
        from google.api_core import exceptions

        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._blob(name).download_to_filename(str(path))
        except exceptions.NotFound as exc:
            raise StateError(f"{name} is missing from the state store.") from exc

    def upload(self, name, path):
        self._blob(name).upload_from_filename(str(path), if_generation_match=0)

    def exists(self, name):
        return self._blob(name).exists()

    def list(self, prefix):
        full = self.prefix + prefix
        return sorted(
            b.name[len(self.prefix) :]
            for b in self.bucket.client.list_blobs(self.bucket, prefix=full)
        )

    def delete(self, name, if_generation_match=None):
        from google.api_core import exceptions

        try:
            self._blob(name).delete(if_generation_match=if_generation_match)
        except exceptions.PreconditionFailed as exc:
            raise PreconditionFailed(f"{name} changed.") from exc
        except exceptions.NotFound:
            return


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


# --------------------------------------------------------------------------- state versions


@dataclass
class Baseline:
    """What was restored; written beside the store so the later save can compare against it."""

    version_id: str
    pointer_generation: int
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


def _manifest(version_id: str, run_id: str, files: dict[str, dict], note: str) -> dict:
    return {
        "state_schema": STATE_SCHEMA,
        "version_id": version_id,
        "run_id": run_id,
        "created_at_utc": _utc_now().isoformat(timespec="seconds"),
        "note": note,
        "files": files,
    }


def _version_name(version_id: str, rel: str) -> str:
    return f"state/versions/{version_id}/files/{rel}"


def _write_version(
    backend: Backend, root: Path, version_id: str, run_id: str, note: str, staging: Path
) -> dict:
    """Upload every state file as an immutable version and read each one back."""
    files: dict[str, dict] = {}
    for spec in STATE_FILES:
        live = root / spec.rel
        if not live.is_file():
            if spec.required:
                raise StateError(f"Required state file {spec.rel} is missing; nothing was saved.")
            continue
        if spec.kind == "sqlite":
            copy = staging / spec.rel
            snapshot_sqlite(live, copy)
        else:
            copy = staging / spec.rel
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(live, copy)
        facts = check_state_file(spec, copy)
        digest = sha256_file(copy)
        backend.upload(_version_name(version_id, spec.rel), copy)
        probe = staging / "_readback" / spec.rel
        backend.download(_version_name(version_id, spec.rel), probe)
        if sha256_file(probe) != digest:
            raise StateError(f"Upload of {spec.rel} could not be verified; nothing was saved.")
        files[spec.rel] = {
            "sha256": digest,
            "bytes": copy.stat().st_size,
            "kind": spec.kind,
            "required": spec.required,
            **facts,
        }
    return _manifest(version_id, run_id, files, note)


def seed(backend: Backend, root: Path, *, run_id: str = "seed") -> str:
    """Create the very first state version from local files. Refuses to replace existing state."""
    if backend.exists(POINTER):
        raise StateError(
            "State already exists; seed only creates the first version. "
            "Use restore and save for normal operation."
        )
    version_id = f"{_stamp(_utc_now())}-{run_id}"
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _write_version(
            backend, root, version_id, run_id, "first state, seeded from local files", Path(tmp)
        )
    _commit_version(backend, manifest, expected_generation=0)
    return version_id


def _commit_version(backend: Backend, manifest: dict, expected_generation: int) -> None:
    body = json.dumps(manifest, indent=2, sort_keys=True).encode()
    version_id = manifest["version_id"]
    backend.put(f"state/versions/{version_id}/manifest.json", body, if_generation_match=0)
    pointer = {
        "state_schema": STATE_SCHEMA,
        "version_id": version_id,
        "manifest_sha256": hashlib.sha256(body).hexdigest(),
        "updated_at_utc": _utc_now().isoformat(timespec="seconds"),
        "run_id": manifest["run_id"],
    }
    # Moved last, by compare-and-swap: a partial upload is never visible, and a concurrent save
    # that already moved the pointer makes this one fail instead of overwriting it.
    backend.put(
        POINTER, json.dumps(pointer, indent=2).encode(), if_generation_match=expected_generation
    )


def read_pointer(backend: Backend) -> tuple[dict, int]:
    found = backend.get(POINTER)
    if found is None:
        raise StateError(
            "No saved state exists (state/latest.json is missing). An empty production history "
            "is never created automatically: seed the first state with "
            "'python scripts/ci_state.py seed' from the machine that holds the real history."
        )
    data, generation = found
    try:
        pointer = json.loads(data)
        pointer["version_id"], pointer["manifest_sha256"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise StateError(
            "state/latest.json is unreadable; restore a previous version with --version."
        ) from exc
    return pointer, generation


def read_manifest(backend: Backend, version_id: str, expected_sha: str | None = None) -> dict:
    found = backend.get(f"state/versions/{version_id}/manifest.json")
    if found is None:
        raise StateError(f"State version {version_id} has no manifest (incomplete upload).")
    data, _ = found
    if expected_sha and hashlib.sha256(data).hexdigest() != expected_sha:
        raise StateError(f"Manifest of state version {version_id} does not match its pointer.")
    manifest = json.loads(data)
    if manifest.get("state_schema") != STATE_SCHEMA:
        raise StateError(f"State version {version_id} uses an unsupported schema.")
    return manifest


def restore(
    backend: Backend, root: Path, *, version_id: str | None = None, overwrite: bool = False
) -> dict:
    """Restore the latest (or a named) valid state into ``root``. Everything is downloaded to a
    staging folder, checksummed and validated before any file in ``root`` is replaced."""
    pointer, generation = read_pointer(backend)
    if version_id:
        manifest = read_manifest(backend, version_id)
    else:
        version_id = pointer["version_id"]
        manifest = read_manifest(backend, version_id, pointer["manifest_sha256"])
    missing_specs = [s.rel for s in STATE_FILES if s.required and s.rel not in manifest["files"]]
    if missing_specs:
        raise StateError(
            f"State version {version_id} lacks required file(s): {', '.join(missing_specs)}."
        )
    if not overwrite:
        present = [rel for rel in manifest["files"] if (root / rel).exists()]
        if present:
            raise StateError(
                f"Refusing to overwrite existing local file(s): {', '.join(present)} "
                "(pass --overwrite to replace them)."
            )
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        counts: dict[str, int] = {}
        for rel, meta in manifest["files"].items():
            target = staging / rel
            backend.download(_version_name(version_id, rel), target)
            if sha256_file(target) != meta["sha256"]:
                raise StateError(f"{rel} of state version {version_id} fails its checksum.")
            spec = next((s for s in STATE_FILES if s.rel == rel), StateFile(rel, meta["kind"]))
            facts = check_state_file(spec, target)
            if spec.kind == "sqlite":
                counts = facts["rows"]
        for rel in manifest["files"]:
            destination = root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staging / rel, destination)
        store = root / STORE_REL
        baseline_copy = root / BASELINE_DIR / "price_history.sqlite3"
        baseline_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(store, baseline_copy)
    # Restored evidence keeps its original retrieval times: files are copied byte for byte.
    Baseline(version_id, generation, counts).write(root)
    return {
        "version_id": version_id,
        "created_at_utc": manifest["created_at_utc"],
        "files": sorted(manifest["files"]),
        "rows": counts,
    }


def save(backend: Backend, root: Path, *, run_id: str, note: str = "") -> dict:
    """Save the post-run state as a new version if (and only if) it validates, is append-only
    relative to the restored baseline and no one else saved meanwhile."""
    baseline = Baseline.read(root)
    check_sqlite(root / STORE_REL)
    check_append_only(root / BASELINE_DIR / "price_history.sqlite3", root / STORE_REL)
    version_id = f"{_stamp(_utc_now())}-{run_id}"
    with tempfile.TemporaryDirectory() as tmp:
        manifest = _write_version(
            backend, root, version_id, run_id, note or "saved after a validated run", Path(tmp)
        )
    _commit_version(backend, manifest, expected_generation=baseline.pointer_generation)
    rows = manifest["files"][STORE_REL]["rows"]
    return {
        "version_id": version_id,
        "previous_version_id": baseline.version_id,
        "rows": rows,
        "rows_before": baseline.counts,
        "new_observations": rows["observations"] - baseline.counts.get("observations", 0),
    }


def promote(backend: Backend, version_id: str) -> dict:
    """Make an earlier saved version the latest again (recovery from a bad state). The version
    is downloaded and fully validated first; the pointer then moves by compare-and-swap, and no
    version is deleted, so the bad one stays available for inspection."""
    _, generation = read_pointer(backend)
    manifest = read_manifest(backend, version_id)
    with tempfile.TemporaryDirectory() as tmp:
        for rel, meta in manifest["files"].items():
            target = Path(tmp) / rel
            backend.download(_version_name(version_id, rel), target)
            if sha256_file(target) != meta["sha256"]:
                raise StateError(f"{rel} of state version {version_id} fails its checksum.")
            check_state_file(
                next((s for s in STATE_FILES if s.rel == rel), StateFile(rel, meta["kind"])), target
            )
    pointer = {
        "state_schema": STATE_SCHEMA,
        "version_id": version_id,
        "manifest_sha256": hashlib.sha256(
            backend.get(f"state/versions/{version_id}/manifest.json")[0]
        ).hexdigest(),
        "updated_at_utc": _utc_now().isoformat(timespec="seconds"),
        "run_id": manifest["run_id"],
        "promoted": True,
    }
    backend.put(POINTER, json.dumps(pointer, indent=2).encode(), if_generation_match=generation)
    return {"version_id": version_id, "promoted": True}


def list_versions(backend: Backend) -> list[str]:
    names = backend.list("state/versions/")
    return sorted({n.split("/")[2] for n in names if n.endswith("/manifest.json")})


# --------------------------------------------------------------------------- lock


def acquire_lock(
    backend: Backend, owner: str, *, ttl_minutes: int = 360, now: datetime | None = None
) -> int:
    """Take the state lock. A lock past its expiry is taken over by compare-and-swap; a live one
    stops the run. Returns the lock generation to release."""
    now = now or _utc_now()
    body = json.dumps(
        {
            "owner": owner,
            "acquired_at_utc": now.isoformat(timespec="seconds"),
            "expires_at_utc": (now + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds"),
        }
    ).encode()
    try:
        return backend.put(LOCK, body, if_generation_match=0)
    except PreconditionFailed:
        pass
    found = backend.get(LOCK)
    if found is None:  # released between the two calls
        return acquire_lock(backend, owner, ttl_minutes=ttl_minutes, now=now)
    data, generation = found
    held = json.loads(data)
    if datetime.fromisoformat(held["expires_at_utc"]) > now:
        raise StateError(
            f"Another run holds the state lock ({held['owner']}, until {held['expires_at_utc']}). "
            "Wait for it, or break a stale lock with 'python scripts/ci_state.py unlock --force'."
        )
    try:
        return backend.put(LOCK, body, if_generation_match=generation)
    except PreconditionFailed as exc:
        raise StateError("Another run took the expired state lock first.") from exc


def release_lock(backend: Backend, owner: str, generation: int | None = None) -> bool:
    found = backend.get(LOCK)
    if found is None:
        return False
    data, current = found
    if json.loads(data).get("owner") != owner:
        return False  # never release someone else's lock
    try:
        backend.delete(LOCK, if_generation_match=current)
    except PreconditionFailed:
        return False
    return True


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


def extract_bundle(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if not str(target).startswith(str(destination.resolve())) or not (
                member.isfile() or member.isdir()
            ):
                raise StateError(f"Unsafe entry in run bundle: {member.name}")
        archive.extractall(destination, filter="data")


def save_bundle(backend: Backend, run_dir: Path, run_id: str, record: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "bundle.tar.gz"
        info = create_bundle(run_dir, archive)
        backend.upload(f"runs/{run_id}/bundle.tar.gz", archive)
        probe = Path(tmp) / "probe.tar.gz"
        backend.download(f"runs/{run_id}/bundle.tar.gz", probe)
        if sha256_file(probe) != info["sha256"]:
            raise StateError("Run bundle upload could not be verified.")
    manifest = {"run_id": run_id, "bundle": info, **record}
    backend.put(
        f"runs/{run_id}/run_manifest.json",
        json.dumps(manifest, indent=2, default=str).encode(),
        if_generation_match=0,
    )
    return manifest


def load_bundle(backend: Backend, run_id: str, destination: Path) -> dict:
    found = backend.get(f"runs/{run_id}/run_manifest.json")
    if found is None:
        raise StateError(f"Run {run_id} has no saved bundle; it cannot be published.")
    manifest = json.loads(found[0])
    archive = destination.parent / f"{run_id}.tar.gz"
    backend.download(f"runs/{run_id}/bundle.tar.gz", archive)
    if sha256_file(archive) != manifest["bundle"]["sha256"]:
        raise StateError(f"Run bundle {run_id} fails its checksum.")
    extract_bundle(archive, destination)
    return manifest


def update_run_manifest(backend: Backend, run_id: str, **fields) -> dict:
    found = backend.get(f"runs/{run_id}/run_manifest.json")
    if found is None:
        raise StateError(f"Run {run_id} has no manifest.")
    manifest = json.loads(found[0])
    manifest.update(fields)
    backend.put(
        f"runs/{run_id}/run_manifest.json",
        json.dumps(manifest, indent=2, default=str).encode(),
        if_generation_match=found[1],
    )
    return manifest


# --------------------------------------------------------------------------- publication records


def publication_record_name(publication_id: str) -> str:
    return f"publications/{publication_id}.json"


def read_publication_record(backend: Backend, publication_id: str) -> tuple[dict | None, int]:
    found = backend.get(publication_record_name(publication_id))
    if found is None:
        return None, 0
    return json.loads(found[0]), found[1]


def append_publication_attempt(backend: Backend, publication_id: str, attempt: dict) -> dict:
    """Record one publication attempt (never edited afterwards). Compare-and-swap, so two
    writers cannot lose each other's entries."""
    for _ in range(3):
        record, generation = read_publication_record(backend, publication_id)
        record = record or {"publication_id": publication_id, "attempts": []}
        record["attempts"].append(attempt)
        record["last_status"] = attempt.get("status")
        try:
            backend.put(
                publication_record_name(publication_id),
                json.dumps(record, indent=2, default=str).encode(),
                if_generation_match=generation,
            )
            return record
        except PreconditionFailed:
            continue
    raise StateError(f"Could not record the publication attempt of {publication_id}.")


def upload_tree(backend: Backend, folder: Path, prefix: str) -> list[str]:
    """Upload a folder (for example a publication's local backups) under ``prefix``."""
    names = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        name = f"{prefix}/{path.relative_to(folder).as_posix()}"
        backend.put(name, path.read_bytes())
        names.append(name)
    return names


# --------------------------------------------------------------------------- configuration


def backend_from_env(env: dict[str, str] | None = None) -> Backend:
    """``STATE_LOCAL_DIR`` (rehearsal, tests) or ``GCS_STATE_BUCKET`` with ``GCS_STATE_PREFIX``."""
    env = dict(os.environ) if env is None else env
    local = env.get("STATE_LOCAL_DIR", "").strip()
    if local:
        return LocalBackend(Path(local))
    bucket = env.get("GCS_STATE_BUCKET", "").strip()
    if not bucket:
        raise StateError(
            "No state storage is configured: set GCS_STATE_BUCKET "
            "(or STATE_LOCAL_DIR for a local rehearsal)."
        )
    return GcsBackend(bucket, env.get("GCS_STATE_PREFIX", "").strip())


def write_step_summary(text: str) -> None:
    """Append Markdown to the GitHub job summary when running in Actions, else print it."""
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)
