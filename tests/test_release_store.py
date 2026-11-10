"""The GitHub Releases client against a scripted HTTP session (offline; not a hosted test).

Real API behaviour was probed separately on 2026-11-10 with draft releases only (see
docs/GITHUB_ACTIONS_SETUP.md): drafts are listed for a token with push access, a second draft with
the same tag is allowed, and a duplicate asset name is refused.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from laptop_tracker import release_store as rs

TOKEN = "ghs_SECRET_TOKEN_VALUE"


class Resp:
    def __init__(self, status=200, payload=None, headers=None, content=b""):
        self.status_code, self._payload = status, payload
        self.headers, self.content = headers or {}, content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def iter_content(self, size):
        yield self.content


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.calls.append({"method": method, "url": url, "headers": headers, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_store(*responses, sleeps=None):
    session = Session(*responses)
    waits = [] if sleeps is None else sleeps
    store = rs.GitHubReleaseStore(
        "owner/repo", TOKEN, session=session, sleep=waits.append, attempts=3
    )
    return store, session, waits


def release_json(**over):
    base = {
        "id": 7,
        "tag_name": "tracker-state-000001",
        "draft": True,
        "created_at": "2026-11-10T01:00:00Z",
        "body": "",
        "upload_url": "https://uploads.github.com/repos/owner/repo/releases/7/assets{?name,label}",
        "assets": [{"id": 5, "name": "a.bin", "size": 3, "state": "uploaded"}],
        "immutable": False,
    }
    return {**base, **over}


def test_requests_carry_the_token_only_as_a_bearer_header() -> None:
    store, session, _ = make_store(Resp(200, []))
    store.list_releases("tracker-")
    call = session.calls[0]
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["url"] == "https://api.github.com/repos/owner/repo/releases"
    assert TOKEN not in call["url"]


def test_listing_paginates_filters_by_prefix_and_reads_the_release_fields() -> None:
    page1 = [release_json(id=i, tag_name=f"tracker-state-{i:06d}") for i in range(1, 101)]
    page2 = [release_json(id=500, tag_name="other-tag"), release_json(id=501, draft=False)]
    store, session, _ = make_store(Resp(200, page1), Resp(200, page2))
    found = store.list_releases("tracker-state-")
    assert len(found) == 101 and [c["params"]["page"] for c in session.calls] == [1, 2]
    assert found[0].assets[0].size == 3 and found[0].draft is True and found[0].immutable is False
    assert found[-1].draft is False and found[-1].id == 501


def test_a_release_asset_that_is_not_fully_uploaded_is_not_listed() -> None:
    item = release_json(assets=[{"id": 1, "name": "x", "size": 1, "state": "open"}])
    store, _, _ = make_store(Resp(200, [item]))
    assert store.list_releases("tracker-")[0].assets == ()


def test_releases_are_created_as_drafts_and_never_as_the_latest_release() -> None:
    store, session, _ = make_store(Resp(201, release_json()))
    created = store.create_draft("tracker-state-000001", "title", "body")
    sent = session.calls[0]["json"]
    assert sent["draft"] is True and sent["make_latest"] == "false"
    assert sent["tag_name"] == "tracker-state-000001" and created.id == 7


def test_publishing_flips_the_draft_flag_only() -> None:
    store, session, _ = make_store(Resp(200, release_json(draft=False)))
    published = store.publish(rs.Release(7, "t", True, "", "", ()))
    assert session.calls[0]["method"] == "PATCH" and session.calls[0]["json"]["draft"] is False
    assert published.draft is False


def test_upload_uses_the_upload_url_and_checks_the_reported_size(tmp_path: Path) -> None:
    payload = tmp_path / "a.bin"
    payload.write_bytes(b"abc")
    release = rs.GitHubReleaseStore._release(
        rs.GitHubReleaseStore("owner/repo", TOKEN, session=Session()), release_json()
    )
    store, session, _ = make_store(Resp(201, {"size": 3}))
    store.upload_asset(release, "a.bin", payload)
    call = session.calls[0]
    assert call["url"].startswith("https://uploads.github.com/repos/owner/repo/releases/7/assets")
    assert "{" not in call["url"] and call["params"] == {"name": "a.bin"}
    assert call["headers"]["Content-Type"] == "application/octet-stream"
    wrong, _, _ = make_store(Resp(201, {"size": 2}))
    with pytest.raises(rs.ReleaseStoreError, match="wrong size"):
        wrong.upload_asset(release, "a.bin", payload)


def test_a_duplicate_asset_a_taken_tag_and_an_immutable_release_have_their_own_errors(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "a.bin"
    payload.write_bytes(b"abc")
    release = rs.Release(7, "t", True, "", "", (), "https://uploads.github.com/x/assets{?name}")
    exists = Resp(422, {"errors": [{"code": "already_exists", "field": "name"}]})
    store, _, _ = make_store(exists)
    with pytest.raises(rs.AssetExists):
        store.upload_asset(release, "a.bin", payload)
    store, _, _ = make_store(exists)
    with pytest.raises(rs.TagExists):
        store.create_draft("t", "t", "b")
    locked = Resp(422, {"message": "Cannot upload assets to an immutable release."})
    store, _, _ = make_store(locked)
    with pytest.raises(rs.ReleaseImmutable):
        store.upload_asset(release, "a.bin", payload)


def test_server_errors_and_rate_limits_are_retried_with_backoff() -> None:
    store, session, waits = make_store(
        Resp(502), Resp(429, headers={"Retry-After": "7"}), Resp(200, [])
    )
    assert store.list_releases("tracker-") == []
    assert len(session.calls) == 3 and waits == [2.0, 7.0]


def test_a_secondary_rate_limit_403_is_retried_but_a_permission_403_is_not() -> None:
    limited = Resp(403, {"message": "rate"}, headers={"X-RateLimit-Remaining": "0"})
    store, _, waits = make_store(limited, Resp(200, []))
    assert store.list_releases("t") == [] and waits
    store, session, _ = make_store(Resp(403, {"message": "Resource not accessible"}))
    with pytest.raises(rs.ReleaseStoreError, match="contents: write") as caught:
        store.create_draft("t", "t", "b")
    assert len(session.calls) == 1 and TOKEN not in str(caught.value)


def test_persistent_failures_end_in_an_error_without_leaking_the_token() -> None:
    store, session, _ = make_store(Resp(500), Resp(500), Resp(500))
    with pytest.raises(rs.ReleaseStoreError, match="failed after retries") as caught:
        store.list_releases("t")
    assert len(session.calls) == 3 and TOKEN not in str(caught.value)
    store, _, _ = make_store(ConnectionError("reset"), ConnectionError("reset"), Resp(200, []))
    assert store.list_releases("t") == []


def test_download_streams_the_asset_and_rejects_a_short_file(tmp_path: Path) -> None:
    release = rs.Release(7, "t", True, "", "", (rs.Asset(5, "a.bin", 3),))
    store, session, _ = make_store(Resp(200, content=b"abc"))
    target = tmp_path / "out" / "a.bin"
    store.download_asset(release, "a.bin", target)
    assert target.read_bytes() == b"abc"
    assert session.calls[0]["headers"]["Accept"] == "application/octet-stream"
    assert session.calls[0]["url"].endswith("/releases/assets/5")
    short, _, _ = make_store(Resp(200, content=b"ab"))
    with pytest.raises(rs.ReleaseStoreError, match="incompletely"):
        short.download_asset(release, "a.bin", tmp_path / "short.bin")
    with pytest.raises(rs.ReleaseStoreError, match="not attached"):
        store.download_asset(release, "missing", tmp_path / "m")


def test_only_drafts_may_be_deleted() -> None:
    store, session, _ = make_store(Resp(204))
    with pytest.raises(rs.ReleaseStoreError, match="Only draft"):
        store.delete_draft(rs.Release(7, "t", False, "", "", ()))
    store.delete_draft(rs.Release(7, "t", True, "", "", ()))
    assert session.calls[0]["method"] == "DELETE"


def test_repository_and_token_discovery(monkeypatch) -> None:
    assert rs.repository_from_remote("https://github.com/jarlericaa/laptop-price-tracker.git") == (
        "jarlericaa/laptop-price-tracker"
    )
    assert rs.repository_from_remote("git@github.com:owner/repo.git") == "owner/repo"
    with pytest.raises(rs.ReleaseStoreError):
        rs.repository_from_remote("https://example.com/x")
    with pytest.raises(rs.ReleaseStoreError, match="owner/repository"):
        rs.GitHubReleaseStore("not a repo", "t")

    assert rs.token_from_environment({"GITHUB_TOKEN": " abc ", "GH_TOKEN": "zzz"}) == "abc"
    assert rs.token_from_environment({"GH_TOKEN": "zzz"}) == "zzz"
    monkeypatch.setattr(
        rs.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="from-gh-cli\n", stderr=""),
    )
    assert rs.token_from_environment({}) == "from-gh-cli"
    monkeypatch.setattr(
        rs.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="")
    )
    with pytest.raises(rs.ReleaseStoreError, match="gh auth login"):
        rs.token_from_environment({})


def test_store_from_env_chooses_local_or_github(tmp_path) -> None:
    assert isinstance(
        rs.store_from_env({"STATE_LOCAL_DIR": str(tmp_path / "x")}), rs.LocalReleaseStore
    )
    store = rs.store_from_env({"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "t"})
    assert isinstance(store, rs.GitHubReleaseStore) and store.repository == "owner/repo"


def test_the_local_store_mirrors_asset_uniqueness_and_tag_uniqueness(tmp_path: Path) -> None:
    store = rs.LocalReleaseStore(tmp_path / "s")
    payload = tmp_path / "p"
    payload.write_bytes(b"1")
    one = store.create_draft("tag", "t", "b")
    two = store.create_draft("tag", "t", "b")  # drafts may share a tag, as on GitHub
    store.upload_asset(one, "a", payload)
    with pytest.raises(rs.AssetExists):
        store.upload_asset(one, "a", payload)
    store.publish(one)
    with pytest.raises(rs.TagExists):
        store.publish(two)
    with pytest.raises(rs.ReleaseStoreError, match="Only draft"):
        store.delete_draft(one)
