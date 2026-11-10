"""Storage of tracker state as private GitHub Release assets (and a local stand-in for tests).

GitHub Releases are file storage, not a database. What they do give, and what the state layer
builds on:

* a release is created as a **draft**, assets are attached, and only then is it **published**;
  a draft is invisible to anyone without push access and is never treated as state;
* with the repository's "immutable releases" setting a published release can no longer gain,
  lose or change assets, and its tag name can never be reused, so every object here gets a new
  unique tag and nothing is ever overwritten or reused;
* asset names are unique inside a release.

There is no atomic compare-and-swap and no lease. The layers above compensate with the Actions
concurrency group, unique sequence numbers, a claim check on the draft, parent links and
read-back verification (see ``state_store``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

API = "https://api.github.com"


class ReleaseStoreError(RuntimeError):
    """A storage problem. Messages never contain the token."""


class AssetExists(ReleaseStoreError):
    """An asset of that name is already attached to the release."""


class TagExists(ReleaseStoreError):
    """A published release already uses this tag."""


class ReleaseImmutable(ReleaseStoreError):
    """The release is published and immutable: assets cannot be added."""


@dataclass(frozen=True)
class Asset:
    id: int
    name: str
    size: int


@dataclass(frozen=True)
class Release:
    id: int
    tag: str
    draft: bool
    created_at: str
    body: str
    assets: tuple[Asset, ...]
    upload_url: str = ""
    immutable: bool | None = None

    def asset(self, name: str) -> Asset | None:
        return next((a for a in self.assets if a.name == name), None)


class ReleaseStore(Protocol):
    def list_releases(self, tag_prefix: str) -> list[Release]: ...
    def create_draft(self, tag: str, title: str, body: str) -> Release: ...
    def upload_asset(self, release: Release, name: str, path: Path) -> None: ...
    def download_asset(self, release: Release, name: str, destination: Path) -> None: ...
    def publish(self, release: Release) -> Release: ...
    def delete_draft(self, release: Release) -> None: ...


# --------------------------------------------------------------------------- local stand-in


class LocalReleaseStore:
    """A directory that behaves like the parts of GitHub Releases the state layer relies on:
    drafts, unique asset names, publish, tag uniqueness among published releases and (optionally)
    immutability. Used by the tests and for a rehearsal without network access."""

    def __init__(self, root: Path, *, immutable: bool = False) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.immutable = immutable

    def _meta_path(self, release_id: int) -> Path:
        return self.root / str(release_id) / "release.json"

    def _next_id(self) -> int:
        counter = self.root / "next_id"
        value = int(counter.read_text()) if counter.is_file() else 1
        counter.write_text(str(value + 1))
        return value

    def _load(self, release_id: int) -> Release:
        meta = json.loads(self._meta_path(release_id).read_text(encoding="utf-8"))
        folder = self.root / str(release_id) / "assets"
        assets = tuple(
            Asset(i, name, (folder / name).stat().st_size)
            for i, name in enumerate(meta["asset_names"], 1)
            if (folder / name).is_file()
        )
        return Release(
            release_id,
            meta["tag"],
            meta["draft"],
            meta["created_at"],
            meta["body"],
            assets,
            immutable=self.immutable and not meta["draft"],
        )

    def _save(self, release_id: int, **changes) -> None:
        path = self._meta_path(release_id)
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta.update(changes)
        path.write_text(json.dumps(meta), encoding="utf-8")

    def list_releases(self, tag_prefix: str) -> list[Release]:
        found = []
        for folder in sorted(self.root.iterdir()):
            if folder.is_dir() and (folder / "release.json").is_file():
                release = self._load(int(folder.name))
                if release.tag.startswith(tag_prefix):
                    found.append(release)
        return sorted(found, key=lambda r: r.id)

    def create_draft(self, tag: str, title: str, body: str) -> Release:
        release_id = self._next_id()
        (self.root / str(release_id) / "assets").mkdir(parents=True)
        self._meta_path(release_id).write_text(
            json.dumps(
                {
                    "tag": tag,
                    "title": title,
                    "body": body,
                    "draft": True,
                    "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "asset_names": [],
                }
            ),
            encoding="utf-8",
        )
        return self._load(release_id)

    def upload_asset(self, release: Release, name: str, path: Path) -> None:
        current = self._load(release.id)
        if not current.draft and self.immutable:
            raise ReleaseImmutable(f"Release {current.tag} is published and immutable.")
        target = self.root / str(release.id) / "assets" / name
        if target.exists():
            raise AssetExists(f"{name} already exists in {current.tag}.")
        shutil.copyfile(path, target)
        meta = json.loads(self._meta_path(release.id).read_text(encoding="utf-8"))
        self._save(release.id, asset_names=[*meta["asset_names"], name])

    def download_asset(self, release: Release, name: str, destination: Path) -> None:
        source = self.root / str(release.id) / "assets" / name
        if not source.is_file():
            raise ReleaseStoreError(f"{name} is not attached to release {release.tag}.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def publish(self, release: Release) -> Release:
        current = self._load(release.id)
        for other in self.list_releases(current.tag):
            if other.tag == current.tag and not other.draft and other.id != release.id:
                raise TagExists(f"A published release already uses the tag {current.tag}.")
        self._save(release.id, draft=False)
        return self._load(release.id)

    def delete_draft(self, release: Release) -> None:
        if not self._load(release.id).draft:
            raise ReleaseStoreError("Only draft releases may be deleted.")
        shutil.rmtree(self.root / str(release.id))


# --------------------------------------------------------------------------- GitHub


def repository_from_remote(url: str) -> str:
    match = re.search(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url.strip())
    if not match:
        raise ReleaseStoreError("Cannot work out the GitHub repository (set GITHUB_REPOSITORY).")
    return match.group(1)


def token_from_environment(env: dict[str, str] | None = None) -> str:
    """``GITHUB_TOKEN`` (Actions), ``GH_TOKEN``, or the GitHub CLI's saved login on a laptop."""
    env = dict(os.environ) if env is None else env
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if (env.get(name) or "").strip():
            return env[name].strip()
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise ReleaseStoreError(
        "No GitHub credentials: in Actions the workflow must pass GITHUB_TOKEN; on a laptop "
        "run 'gh auth login' first."
    )


class GitHubReleaseStore:
    """GitHub REST API via ``requests``. The token comes from the environment and is only ever
    sent to api.github.com / uploads.github.com (``requests`` drops it on the redirect to the
    asset host)."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        session=None,
        api: str = API,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 4,
    ) -> None:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository or ""):
            raise ReleaseStoreError(f"'{repository}' is not an owner/repository name.")
        if session is None:
            import requests

            session = requests.Session()
        self.repository = repository
        self.session = session
        self.api = api.rstrip("/")
        self._token = token
        self._sleep = sleep
        self._attempts = attempts

    # -- transport
    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        return {**headers, **(extra or {})}

    def _request(self, method: str, url: str, *, ok=(200, 201, 204), headers=None, **kwargs):
        last = ""
        full_headers = self._headers(headers)
        for attempt in range(self._attempts):
            data = kwargs.get("data")
            if hasattr(data, "seek"):
                data.seek(0)
            try:
                response = self.session.request(
                    method, url, headers=full_headers, timeout=120, **kwargs
                )
            except OSError as exc:  # connection and timeout errors
                last = f"network error ({type(exc).__name__})"
            else:
                status = response.status_code
                if status in ok:
                    return response
                retry_after = response.headers.get("Retry-After")
                rate_limited = status == 429 or (
                    status == 403 and response.headers.get("X-RateLimit-Remaining") == "0"
                )
                if status >= 500 or rate_limited:
                    last = f"HTTP {status}"
                    wait = float(retry_after) if retry_after else 2.0 ** (attempt + 1)
                    self._sleep(min(wait, 120.0))
                    continue
                raise self._error(method, url, response)
            self._sleep(2.0 ** (attempt + 1))
        raise ReleaseStoreError(f"{method} {self._where(url)} failed after retries ({last}).")

    def _where(self, url: str) -> str:
        return url.replace(self.api, "").split("?")[0]

    def _error(self, method: str, url: str, response) -> ReleaseStoreError:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        text = json.dumps(payload.get("errors", payload.get("message", "")))[:300]
        message = f"{method} {self._where(url)} returned HTTP {response.status_code}: {text}"
        if response.status_code == 422 and "already_exists" in text:
            return AssetExists(message) if "/assets" in url else TagExists(message)
        if response.status_code == 422 and "mmutable" in text:
            return ReleaseImmutable(message)
        if response.status_code in (401, 403):
            message += (
                " Check that the workflow job has 'permissions: contents: write' "
                "(or that your 'gh auth login' account can push to the repository)."
            )
        return ReleaseStoreError(message)

    # -- operations
    def _release(self, item: dict) -> Release:
        return Release(
            id=int(item["id"]),
            tag=item["tag_name"],
            draft=bool(item.get("draft")),
            created_at=item.get("created_at") or "",
            body=item.get("body") or "",
            assets=tuple(
                Asset(int(a["id"]), a["name"], int(a.get("size", 0)))
                for a in item.get("assets", [])
                if a.get("state", "uploaded") == "uploaded"
            ),
            upload_url=item.get("upload_url", ""),
            immutable=item.get("immutable"),
        )

    def list_releases(self, tag_prefix: str) -> list[Release]:
        found: list[Release] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                f"{self.api}/repos/{self.repository}/releases",
                params={"per_page": 100, "page": page},
            )
            items = response.json()
            found += [self._release(i) for i in items if i["tag_name"].startswith(tag_prefix)]
            if len(items) < 100:
                break
            page += 1
        return sorted(found, key=lambda r: r.id)

    def create_draft(self, tag: str, title: str, body: str) -> Release:
        response = self._request(
            "POST",
            f"{self.api}/repos/{self.repository}/releases",
            json={
                "tag_name": tag,
                "name": title,
                "body": body,
                "draft": True,
                "prerelease": False,
                "make_latest": "false",
            },
        )
        return self._release(response.json())

    def upload_asset(self, release: Release, name: str, path: Path) -> None:
        base = release.upload_url.split("{")[0] or (
            f"https://uploads.github.com/repos/{self.repository}/releases/{release.id}/assets"
        )
        with open(path, "rb") as handle:
            response = self._request(
                "POST",
                base,
                params={"name": name},
                data=handle,
                headers={"Content-Type": "application/octet-stream"},
            )
        size = Path(path).stat().st_size
        reported = response.json().get("size")
        if reported is not None and int(reported) != size:
            raise ReleaseStoreError(f"{name} uploaded with the wrong size ({reported} != {size}).")

    def download_asset(self, release: Release, name: str, destination: Path) -> None:
        asset = release.asset(name)
        if asset is None:
            raise ReleaseStoreError(f"{name} is not attached to release {release.tag}.")
        response = self._request(
            "GET",
            f"{self.api}/repos/{self.repository}/releases/assets/{asset.id}",
            headers={"Accept": "application/octet-stream"},
            stream=True,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "wb") as handle:
            for chunk in response.iter_content(1 << 20):
                handle.write(chunk)
        if destination.stat().st_size != asset.size:
            raise ReleaseStoreError(f"{name} downloaded incompletely.")

    def publish(self, release: Release) -> Release:
        response = self._request(
            "PATCH",
            f"{self.api}/repos/{self.repository}/releases/{release.id}",
            json={"draft": False, "make_latest": "false"},
        )
        return self._release(response.json())

    def delete_draft(self, release: Release) -> None:
        if not release.draft:
            raise ReleaseStoreError("Only draft releases may be deleted.")
        self._request(
            "DELETE", f"{self.api}/repos/{self.repository}/releases/{release.id}", ok=(204,)
        )


def store_from_env(env: dict[str, str] | None = None) -> ReleaseStore:
    """``STATE_LOCAL_DIR`` (rehearsal, tests) or the GitHub repository of ``GITHUB_REPOSITORY``
    (set by Actions) / the ``origin`` remote of a local checkout."""
    env = dict(os.environ) if env is None else env
    local = (env.get("STATE_LOCAL_DIR") or "").strip()
    if local:
        return LocalReleaseStore(Path(local))
    repository = (env.get("GITHUB_REPOSITORY") or "").strip()
    if not repository:
        try:
            remote = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            remote = ""
        repository = repository_from_remote(remote)
    return GitHubReleaseStore(repository, token_from_environment(env))
