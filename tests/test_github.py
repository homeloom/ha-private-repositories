"""The GitHub client."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from yarl import URL

from custom_components.private_repositories import github as module
from custom_components.private_repositories.github import (
    Account,
    Asset,
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    Repository,
)

from .conftest import API, LOGIN, REPOSITORY, REPOSITORY_ID, TOKEN, USER_ID, FakeGitHub, release

CODELOAD = "https://codeload.github.com/archive"
REPO = Repository(REPOSITORY_ID, REPOSITORY)
# Where a repository is reached once found.
BASE = f"{API}/repositories/{REPOSITORY_ID}"


@pytest.fixture
async def client(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> GitHubClient:
    """A client on the mocked session."""
    return GitHubClient(async_get_clientsession(hass), TOKEN)


def _headers(mock: AiohttpClientMocker, index: int) -> dict[str, str]:
    return mock.mock_calls[index][3] or {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("owner/name", "owner/name"),
        (" https://github.com/owner/name.git ", "owner/name"),
        ("github.com/owner/name/", "owner/name"),
        ("http://www.github.com/Owner/my.repo", "Owner/my.repo"),
        ("owner", None),
        ("owner/name/extra", None),
        ("https://gitlab.example/owner/name", None),
        ("-owner/name", None),
        ("owner/..", None),
        ("owner/.", None),
        ("owner/na me", None),
    ],
)
def test_parse_repository(value: str, expected: str | None) -> None:
    assert module.parse_repository(value) == expected


def test_parse_expiration() -> None:
    assert module.parse_expiration("2026-11-23 10:00:00 UTC") == datetime(2026, 11, 23, 10, tzinfo=UTC)
    assert module.parse_expiration("2026-11-23 10:00:00 +0200") == datetime(
        2026, 11, 23, 10, tzinfo=timezone(timedelta(hours=2))
    )
    assert module.parse_expiration("soon") is None
    assert module.parse_expiration(None) is None


async def test_account_and_expiration(client: GitHubClient, github: FakeGitHub) -> None:
    github.user()
    assert await client.get_account() == Account(USER_ID, LOGIN)
    assert client.expiration == datetime(2030, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (401, {}, GitHubAuthError),
        (403, {"x-ratelimit-remaining": "0"}, GitHubRateLimitError),
        (403, {"retry-after": "60"}, GitHubRateLimitError),
        (429, {}, GitHubRateLimitError),
        (403, {}, GitHubNotFoundError),
        (404, {}, GitHubNotFoundError),
        (502, {}, GitHubError),
        (418, {}, GitHubError),
    ],
)
async def test_errors(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker, status: int, headers: dict, error: type
) -> None:
    aioclient_mock.get(f"{API}/user", status=status, headers=headers, json={})
    with pytest.raises(error):
        await client.get_account()


async def test_unreachable(client: GitHubClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/user", exc=aiohttp.ClientError())
    with pytest.raises(GitHubError, match="Cannot reach"):
        await client.get_account()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/user", exc=TimeoutError())
    with pytest.raises(GitHubError, match="Cannot reach"):
        await client.get_account()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/user", text="not json")
    with pytest.raises(GitHubError, match="unreadable"):
        await client.get_account()


@pytest.mark.parametrize("answer", [[], {"login": LOGIN}, {"login": LOGIN, "id": "1"}, {"id": 1}])
async def test_unexpected_account(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker, answer: object
) -> None:
    aioclient_mock.get(f"{API}/user", json=answer)
    with pytest.raises(GitHubError, match="unexpected"):
        await client.get_account()


async def test_unexpected_answers(client: GitHubClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/repos/{REPOSITORY}", json={})
    aioclient_mock.get(f"{BASE}/releases", json={})
    aioclient_mock.get(f"{API}/user/repos", json={})
    aioclient_mock.get(f"{API}/repos/octo/renamed", json={"id": 1, "full_name": "octo/../x"})
    with pytest.raises(GitHubError, match="unexpected answer"):
        await client.get_repository(REPOSITORY)
    with pytest.raises(GitHubError, match="unexpected answer"):
        await client.list_releases(REPO)
    with pytest.raises(GitHubError, match="unexpected answer"):
        await client.list_repositories()
    with pytest.raises(GitHubError, match="unexpected name"):
        await client.get_repository("octo/renamed")


@pytest.mark.parametrize("repository_id", [None, "7", True, 0, -1, 1.5])
async def test_repositories_need_an_id(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker, repository_id: object
) -> None:
    aioclient_mock.get(f"{API}/repos/{REPOSITORY}", json={"id": repository_id, "full_name": REPOSITORY})
    with pytest.raises(GitHubError, match="unexpected answer"):
        await client.get_repository(REPOSITORY)


async def test_get_repository_follows_renames(client: GitHubClient, github: FakeGitHub) -> None:
    github.repository("octo/old-name", full_name=REPOSITORY)
    assert await client.get_repository("octo/old-name") == REPO


async def test_get_current(client: GitHubClient, aioclient_mock: AiohttpClientMocker) -> None:
    """A repository found again by its id: the name may have changed, the id may not."""
    aioclient_mock.get(BASE, json={"id": REPOSITORY_ID, "full_name": "octo/new-name"})
    assert await client.get_current(REPO) == Repository(REPOSITORY_ID, "octo/new-name")
    assert aioclient_mock.mock_calls[0][1] == URL(BASE)
    aioclient_mock.clear_requests()
    aioclient_mock.get(BASE, json={"id": REPOSITORY_ID + 1, "full_name": REPOSITORY})
    with pytest.raises(GitHubError, match=f"another repository for {REPOSITORY}"):
        await client.get_current(REPO)
    aioclient_mock.clear_requests()
    aioclient_mock.get(BASE, status=404)
    with pytest.raises(GitHubNotFoundError, match=f"Repository {REPOSITORY} not found"):
        await client.get_current(REPO)


async def test_renamed_repositories_are_followed_with_the_token(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub answers the old name of a renamed or transferred repository with a
    redirect: it is followed, and the token goes along on the same host."""
    tokens: list[str | None] = []

    async def old_name(request: web.Request) -> web.Response:
        tokens.append(request.headers.get("Authorization"))
        # GitHub keeps the query in the new address.
        location = request.rel_url.with_path(f"/repositories/42{request.match_info['rest']}")
        raise web.HTTPMovedPermanently(location.with_query(request.query))

    async def moved(request: web.Request) -> web.Response:
        tokens.append(request.headers.get("Authorization"))
        return web.json_response({"id": 42, "full_name": REPOSITORY})

    async def moved_releases(request: web.Request) -> web.Response:
        tokens.append(request.headers.get("Authorization"))
        assert request.query["per_page"] == str(module.MAX_RELEASES)
        return web.json_response([release("v1.0.0")])

    async def moved_file(request: web.Request) -> web.Response:
        tokens.append(request.headers.get("Authorization"))
        assert request.query["ref"] == "v1.0.0"
        return web.Response(body=b"{}")

    app = web.Application()
    app.router.add_get("/repos/octo/old-name{rest:.*}", old_name)
    app.router.add_get("/repositories/42", moved)
    app.router.add_get("/repositories/42/releases", moved_releases)
    app.router.add_get("/repositories/42/contents/hacs.json", moved_file)
    async with TestServer(app, host="127.0.0.1") as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(module, "API_URL", server.make_url("/"))
        client = GitHubClient(session, TOKEN)
        repository = await client.get_repository("octo/old-name")
        assert repository == Repository(42, REPOSITORY)
        # Then reached by its id, as GitHub's redirect does.
        assert [r.tag for r in await client.list_releases(repository)] == ["v1.0.0"]
        # Every install reads hacs.json first.
        assert await client.get_file(repository, "hacs.json", "v1.0.0") == b"{}"
    assert tokens == [f"Bearer {TOKEN}"] * 4


async def test_get_file_reads_every_piece(socket_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file sent in several pieces is read whole."""

    async def pieces(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b'{"name": ')
        await asyncio.sleep(0.05)
        await response.write(b'"Gizmo"}')
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get(f"/repositories/{REPOSITORY_ID}/contents/hacs.json", pieces)
    async with TestServer(app, host="127.0.0.1") as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(module, "API_URL", server.make_url("/"))
        client = GitHubClient(session, TOKEN)
        assert await client.get_file(REPO, "hacs.json", "v1.0.0") == b'{"name": "Gizmo"}'


@pytest.mark.parametrize("repository", ["octo", "octo/..", "../octo/x", "octo/x?y"])
async def test_invalid_repository_names_never_reach_github(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker, repository: str
) -> None:
    with pytest.raises(GitHubNotFoundError):
        await client.get_repository(repository)
    assert aioclient_mock.call_count == 0


async def test_etag_cache(client: GitHubClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/user", json={"login": LOGIN, "id": USER_ID}, headers={"ETag": '"abc"'})
    assert await client.get_account() == Account(USER_ID, LOGIN)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/user", status=304)
    assert await client.get_account() == Account(USER_ID, LOGIN)
    assert _headers(aioclient_mock, 0)["If-None-Match"] == '"abc"'


async def test_etag_cache_keeps_the_next_page(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A first page that did not change still leads to the second one."""
    aioclient_mock.get(f"{API}/user/repos?page=2", json=[{"full_name": "a/two"}])
    aioclient_mock.get(
        f"{API}/user/repos",
        json=[{"full_name": "a/one"}],
        headers={"ETag": '"one"', "Link": f'<{API}/user/repos?page=2>; rel="next"'},
    )
    assert await client.list_repositories() == ["a/one", "a/two"]
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/user/repos?page=2", json=[{"full_name": "a/three"}])
    aioclient_mock.get(f"{API}/user/repos", status=304)
    assert await client.list_repositories() == ["a/one", "a/three"]


async def test_list_repositories_follows_pages(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API}/user/repos?per_page=100&sort=full_name",
        json=[{"full_name": "a/one"}, {"name": "broken"}],
        headers={"Link": f'<{API}/user/repos?per_page=100&page=2>; rel="next", <{API}/x>; rel="last"'},
    )
    aioclient_mock.get(f"{API}/user/repos?page=2", json=[{"full_name": "a/two"}])
    assert await client.list_repositories() == ["a/one", "a/two"]


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/user/repos?page=2",
        "http://api.github.com/user/repos?page=2",
        "https://api.github.com:8443/user/repos?page=2",
    ],
)
async def test_list_repositories_never_leaves_the_api(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker, link: str
) -> None:
    aioclient_mock.get(
        f"{API}/user/repos", json=[{"full_name": "a/one"}], headers={"Link": f'<{link}>; rel="next"'}
    )
    assert await client.list_repositories() == ["a/one"]
    assert aioclient_mock.call_count == 1


async def test_list_releases(
    client: GitHubClient, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    draft = release("v3.0.0") | {"draft": True}
    asset = {"id": 7, "name": "gizmo.zip", "size": 10}
    github.releases(
        [
            draft,
            release("v2.0.0", prerelease=True),
            release("v1.0.0", body=None) | {"assets": [asset, {}]},
            {},
        ]
    )
    releases = await client.list_releases(REPO)
    assert [r.tag for r in releases] == ["v2.0.0", "v1.0.0"]
    assert releases[0].prerelease
    assert releases[1].body == ""
    assert releases[1].assets == (Asset(7, "gizmo.zip", 10),)
    assert aioclient_mock.mock_calls[0][1].query["per_page"] == "100"


async def test_get_release(client: GitHubClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/releases/tags/v1.0.0", json=release("v1.0.0"))
    aioclient_mock.get(f"{BASE}/releases/tags/v2.0.0", json={"draft": True, "tag_name": "v2.0.0"})
    aioclient_mock.get(f"{BASE}/releases/tags/v3.0.0", json=release("v1.0.0"))
    assert (await client.get_release(REPO, "v1.0.0")).version == "1.0.0"
    with pytest.raises(GitHubNotFoundError):
        await client.get_release(REPO, "v2.0.0")
    # Another release than the one asked for.
    with pytest.raises(GitHubNotFoundError):
        await client.get_release(REPO, "v3.0.0")


async def test_tags_stay_in_the_path(client: GitHubClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/releases/tags/v1%23x%3Fy", json=release("v1#x?y"))
    assert (await client.get_release(REPO, "v1#x?y")).tag == "v1#x?y"
    url = aioclient_mock.mock_calls[0][1]
    assert url.raw_path == f"/repositories/{REPOSITORY_ID}/releases/tags/v1%23x%3Fy"
    assert not url.query_string
    assert not url.fragment


@pytest.mark.parametrize("tag", ["..", "v1/../../user", ".", ""])
async def test_tags_cannot_leave_the_repository(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker, tag: str
) -> None:
    with pytest.raises(GitHubNotFoundError):
        await client.get_release(REPO, tag)
    with pytest.raises(GitHubNotFoundError):
        await client.download_archive(REPO, tag)
    assert aioclient_mock.call_count == 0


async def test_get_file(
    client: GitHubClient, github: FakeGitHub, aioclient_mock: AiohttpClientMocker, monkeypatch
) -> None:
    github.hacs_json({"name": "Gizmo"})
    assert await client.get_file(REPO, "hacs.json", "v1.0.0") == b'{"name": "Gizmo"}'
    assert aioclient_mock.mock_calls[0][1].query["ref"] == "v1.0.0"
    aioclient_mock.clear_requests()
    github.hacs_json(None)
    assert await client.get_file(REPO, "hacs.json", "v1.0.0") is None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/contents/hacs.json", status=500)
    with pytest.raises(GitHubError):
        await client.get_file(REPO, "hacs.json", "v1.0.0")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/contents/hacs.json", exc=aiohttp.ClientError())
    with pytest.raises(GitHubError, match="Cannot reach"):
        await client.get_file(REPO, "hacs.json", "v1.0.0")
    aioclient_mock.clear_requests()
    monkeypatch.setattr(module, "MAX_FILE_SIZE", 4)
    aioclient_mock.get(f"{BASE}/contents/hacs.json", text="too large")
    with pytest.raises(GitHubError, match="too large"):
        await client.get_file(REPO, "hacs.json", "v1.0.0")


async def test_download_without_the_token(
    client: GitHubClient, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    github.archive("v1.0.0", b"archive")
    progress: list[tuple[int, int | None]] = []
    data = await client.download_archive(REPO, "v1.0.0", lambda done, total: progress.append((done, total)))
    assert data == b"archive"
    assert progress[-1] == (7, 7)
    assert _headers(aioclient_mock, 0)["Authorization"] == f"Bearer {TOKEN}"
    assert "Authorization" not in _headers(aioclient_mock, 1)


async def test_download_asset(client: GitHubClient, aioclient_mock: AiohttpClientMocker) -> None:
    location = "https://release-assets.githubusercontent.com/asset/7?sig=x"
    aioclient_mock.get(f"{BASE}/releases/assets/7", status=302, headers={"Location": location})
    aioclient_mock.get(location, content=b"zip")
    assert await client.download_asset(REPO, Asset(7, "gizmo.zip", 3)) == b"zip"
    assert _headers(aioclient_mock, 0)["Accept"] == "application/octet-stream"
    assert "Authorization" not in _headers(aioclient_mock, 1)


async def test_download_asset_sent_directly(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE}/releases/assets/7", content=b"zip")
    assert await client.download_asset(REPO, Asset(7, "gizmo.zip", 3)) == b"zip"
    assert aioclient_mock.call_count == 1


async def test_download_asset_too_large(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker, monkeypatch
) -> None:
    monkeypatch.setattr(module, "MAX_DOWNLOAD_SIZE", 4)
    with pytest.raises(GitHubError, match="too large"):
        await client.download_asset(REPO, Asset(7, "gizmo.zip", 5))
    assert aioclient_mock.call_count == 0


async def test_download_redirected_within_the_api(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The API may send another of its addresses first: it is asked with the
    token, then the download host without it."""
    moved = f"{API}/repositories/42/tarball/v1"
    aioclient_mock.get(f"{BASE}/tarball/v1", status=301, headers={"Location": moved})
    aioclient_mock.get(moved, status=302, headers={"Location": CODELOAD})
    aioclient_mock.get(CODELOAD, content=b"archive")
    assert await client.download_archive(REPO, "v1") == b"archive"
    assert _headers(aioclient_mock, 1)["Authorization"] == f"Bearer {TOKEN}"
    assert "Authorization" not in _headers(aioclient_mock, 2)


async def test_download_with_a_relative_redirect(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{BASE}/tarball/v1",
        status=307,
        headers={"Location": "/repositories/42/tarball/v1"},
    )
    aioclient_mock.get(f"{API}/repositories/42/tarball/v1", content=b"archive")
    assert await client.download_archive(REPO, "v1") == b"archive"


async def test_download_redirected_too_many_times(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{BASE}/tarball/v1",
        status=302,
        headers={"Location": f"{BASE}/tarball/v1"},
    )
    with pytest.raises(GitHubError, match="too many times"):
        await client.download_archive(REPO, "v1")
    assert aioclient_mock.call_count == module.MAX_API_REDIRECTS + 1


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/archive.tar.gz",
        "http://codeload.github.com/x",
        "https://codeload.github.com:8443/x",
        "https://githubusercontent.com.example/x",
        "",
    ],
)
async def test_download_refuses_other_hosts(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker, location: str
) -> None:
    aioclient_mock.get(f"{BASE}/tarball/v1", status=302, headers={"Location": location})
    with pytest.raises(GitHubError, match=r"unexpected host|too many times"):
        await client.download_archive(REPO, "v1")
    assert all("evil" not in str(call[1]) and call[1].port == 443 for call in aioclient_mock.mock_calls)


async def test_download_errors(client: GitHubClient, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/tarball/missing", status=404)
    aioclient_mock.get(f"{BASE}/tarball/gone", status=302, headers={"Location": CODELOAD})
    aioclient_mock.get(f"{BASE}/tarball/down", exc=aiohttp.ClientError())
    aioclient_mock.get(CODELOAD, status=410)
    with pytest.raises(GitHubNotFoundError):
        await client.download_archive(REPO, "missing")
    with pytest.raises(GitHubError, match="HTTP 410"):
        await client.download_archive(REPO, "gone")
    with pytest.raises(GitHubError, match="Cannot download"):
        await client.download_archive(REPO, "down")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/tarball/cut", status=302, headers={"Location": CODELOAD})
    aioclient_mock.get(CODELOAD, exc=aiohttp.ClientPayloadError())
    with pytest.raises(GitHubError, match="interrupted"):
        await client.download_archive(REPO, "cut")


async def test_download_size_limit(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker, monkeypatch
) -> None:
    monkeypatch.setattr(module, "MAX_DOWNLOAD_SIZE", 4)
    aioclient_mock.get(f"{BASE}/tarball/big", status=302, headers={"Location": CODELOAD})
    aioclient_mock.get(CODELOAD, content=b"too big")
    with pytest.raises(GitHubError, match="too large"):
        await client.download_archive(REPO, "big")
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/tarball/big", status=302, headers={"Location": CODELOAD})
    aioclient_mock.get(CODELOAD, content=b"ok", headers={"Content-Length": "100"})
    with pytest.raises(GitHubError, match="too large"):
        await client.download_archive(REPO, "big")


async def test_download_with_a_wrong_length(
    client: GitHubClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A length that is no number is left out: the body itself is counted."""
    aioclient_mock.get(f"{BASE}/tarball/odd", status=302, headers={"Location": CODELOAD})
    aioclient_mock.get(CODELOAD, content=b"archive", headers={"Content-Length": "lots"})
    progress: list[tuple[int, int | None]] = []
    data = await client.download_archive(REPO, "odd", lambda done, total: progress.append((done, total)))
    assert data == b"archive"
    assert progress[-1] == (7, None)
