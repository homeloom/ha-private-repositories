"""A small GitHub REST client: only what the integration needs.

The token is only ever sent to api.github.com. Archives and release assets are
served from other hosts through signed, short-lived URLs: they are fetched
without the token, and only from GitHub's own download hosts.

A repository is found by its "owner/name" once, then always reached by its id
(/repositories/<id>/...), where GitHub's own redirects for renamed repositories
lead. A rename keeps it tracked, and a new repository created under its old
name is never taken for it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

from .versions import version_from_tag

API_URL = URL("https://api.github.com")
API_VERSION = "2022-11-28"
API_TIMEOUT = aiohttp.ClientTimeout(total=30)
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_read=60)
MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024
MAX_FILE_SIZE = 1024 * 1024
MAX_REPOSITORY_PAGES = 10
MAX_RELEASES = 100
# A renamed or transferred repository answers with a redirect to its new address.
MAX_API_REDIRECTS = 3
CHUNK_SIZE = 64 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Where GitHub redirects archive and asset downloads.
DOWNLOAD_HOSTS = ("codeload.github.com",)
DOWNLOAD_HOST_SUFFIXES = (".githubusercontent.com",)

OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
NAME_RE = re.compile(r"(?!\.\.?$)[A-Za-z0-9._-]{1,100}")
REPOSITORY_URL_RE = re.compile(
    r"(?:(?:https?://)?(?:www\.)?github\.com/)?"
    r"(?P<owner>[^/\s]+)/(?P<name>[^/\s]+?)(?:\.git)?/?"
)

LINK_RE = re.compile(r'^<(?P<url>[^>]+)>\s*;\s*rel="(?P<rel>[^"]+)"')

type ProgressCallback = Callable[[int, int | None], None]


class GitHubError(Exception):
    """GitHub did not answer as expected. The message is safe to show."""

    # An expected outcome, with texts of its own in each language.
    reason: str | None = None

    @property
    def placeholders(self) -> dict[str, str]:
        """What those texts show."""
        return {}


class DownloadTooLargeError(GitHubError):
    """A download over MAX_DOWNLOAD_SIZE."""

    reason = "too_large"

    def __init__(self) -> None:
        super().__init__("The download is too large")

    @property
    def placeholders(self) -> dict[str, str]:
        """The limit."""
        return {"limit": f"{MAX_DOWNLOAD_SIZE // (1024 * 1024)} MB"}


class GitHubAuthError(GitHubError):
    """GitHub rejected the token: expired, revoked or mistyped."""


class GitHubNotFoundError(GitHubError):
    """The resource does not exist, or the token cannot see it."""


class GitHubRateLimitError(GitHubError):
    """Too many requests for now. Not a GitHubNotFoundError, although GitHub may
    answer 403 for both."""


@dataclass(frozen=True, slots=True)
class Account:
    """The GitHub account a token belongs to."""

    id: int
    login: str


@dataclass(frozen=True, slots=True)
class Repository:
    """A repository: its id stays the same when it is renamed or transferred."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class Asset:
    """A file attached to a release."""

    id: int
    name: str
    size: int


@dataclass(frozen=True, slots=True)
class Release:
    """A published GitHub release."""

    tag: str
    name: str
    body: str
    url: str
    prerelease: bool
    assets: tuple[Asset, ...] = ()

    @property
    def version(self) -> str:
        """The tag without its leading "v"."""
        return version_from_tag(self.tag)


def is_repository(value: str) -> bool:
    """Whether a value is a valid "owner/name"."""
    owner, _, name = value.partition("/")
    return bool(OWNER_RE.fullmatch(owner) and NAME_RE.fullmatch(name))


def parse_repository(value: str) -> str | None:
    """ "owner/name" from what a user may paste: owner/name or a GitHub URL."""
    match = REPOSITORY_URL_RE.fullmatch(value.strip())
    if match is None:
        return None
    repository = f"{match['owner']}/{match['name']}"
    return repository if is_repository(repository) else None


def parse_expiration(value: str | None) -> datetime | None:
    """The token expiration header: "2026-11-23 10:00:00 UTC" (or "+0200")."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip().replace("UTC", "+0000"), "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def _api_url(*parts: str) -> URL:
    """An API address from parts of its path ("owner/name", a tag...).

    Each component is escaped, so a "#", "?" or "%" in a tag stays in the path,
    and "." or ".." components are refused: no path can lead elsewhere in the API.
    """
    components = [component for part in parts for component in part.split("/")]
    if any(component in ("", ".", "..") for component in components):
        raise GitHubNotFoundError(f"Invalid path ({'/'.join(parts)!r})")
    return API_URL.with_path("/" + "/".join(quote(c, safe="") for c in components), encoded=True)


def _repository(repository: str) -> str:
    if not is_repository(repository):
        raise GitHubNotFoundError(f"{repository!r} is not a repository name")
    return repository


def _repository_url(repository: Repository, *parts: str) -> URL:
    return _api_url("repositories", str(repository.id), *parts)


def _parse_repository(data: Any, what: str) -> Repository:
    """A repository from the API: its id and its current "owner/name"."""
    if (
        not isinstance(data, dict)
        or type(repository_id := data.get("id")) is not int
        or repository_id <= 0
        or not isinstance(full_name := data.get("full_name"), str)
    ):
        raise GitHubError(f"GitHub sent an unexpected answer for {what}")
    if not is_repository(full_name):
        raise GitHubError(f"GitHub sent an unexpected name for {what}")
    return Repository(id=repository_id, name=full_name)


def _release(data: Any) -> Release | None:
    """A release from the API, or None for drafts and malformed entries."""
    if not isinstance(data, dict) or data.get("draft") or not isinstance(data.get("tag_name"), str):
        return None
    assets = tuple(
        Asset(asset["id"], asset["name"], asset.get("size") or 0)
        for asset in data.get("assets") or ()
        if isinstance(asset, dict) and isinstance(asset.get("id"), int) and isinstance(asset.get("name"), str)
    )
    return Release(
        tag=data["tag_name"],
        name=str(data.get("name") or data["tag_name"]),
        body=str(data.get("body") or ""),
        url=str(data.get("html_url") or ""),
        prerelease=bool(data.get("prerelease")),
        assets=assets,
    )


def _error(status: int, headers: Mapping[str, str], what: str) -> GitHubError:
    if status == 401:
        return GitHubAuthError("GitHub rejected the token: it is expired, revoked or mistyped")
    if status == 429 or (
        status == 403 and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)
    ):
        return GitHubRateLimitError("GitHub's rate limit is reached for now")
    if status in (403, 404):
        return GitHubNotFoundError(f"{what} not found, or the token cannot read it")
    if status >= 500:
        return GitHubError(f"GitHub is having trouble (HTTP {status})")
    return GitHubError(f"Unexpected answer from GitHub (HTTP {status})")


def _next_link(header: str | None) -> URL | None:
    """The "next" page of a Link header, if it stays on the API."""
    for part in (header or "").split(","):
        if (match := LINK_RE.match(part.strip())) and match["rel"] == "next":
            url = URL(match["url"], encoded=True)
            # Never follow a link elsewhere with the token.
            return url if _is_api_url(url) else None
    return None


def _length(header: str | None) -> int | None:
    try:
        return int(header) if header else None
    except ValueError:
        return None


def _is_api_url(url: URL) -> bool:
    return url.scheme == "https" and url.host == API_URL.host and url.port == 443


def _is_download_url(url: URL) -> bool:
    host = url.host or ""
    return (
        url.scheme == "https"
        and url.port == 443
        and (host in DOWNLOAD_HOSTS or any(host.endswith(suffix) for suffix in DOWNLOAD_HOST_SUFFIXES))
    )


async def _read_body(
    response: aiohttp.ClientResponse,
    progress: ProgressCallback | None = None,
    limit: int | None = None,
    too_large: Callable[[], GitHubError] = DownloadTooLargeError,
) -> bytes:
    """The whole body of an answer, within a limit (MAX_DOWNLOAD_SIZE by default)."""
    limit = MAX_DOWNLOAD_SIZE if limit is None else limit
    total = _length(response.headers.get("Content-Length"))
    if total is not None and total > limit:
        raise too_large()
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(CHUNK_SIZE):
        size += len(chunk)
        if size > limit:
            raise too_large()
        chunks.append(chunk)
        if progress is not None:
            progress(size, total)
    return b"".join(chunks)


class GitHubClient:
    """GitHub's REST API, authenticated with one token."""

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token
        # Conditional requests: a 304 answer does not count against the rate limit.
        # URL -> (ETag, data, next page).
        self._etags: dict[str, tuple[str, Any, URL | None]] = {}
        # What GitHub says about the end of the token, refreshed on every answer.
        self.expiration: datetime | None = None

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
        }

    def _note(self, response: aiohttp.ClientResponse) -> None:
        if (
            expiration := parse_expiration(response.headers.get("github-authentication-token-expiration"))
        ) is not None:
            self.expiration = expiration

    async def _get(
        self, url: URL, what: str, params: Mapping[str, str] | None = None
    ) -> tuple[Any, URL | None]:
        """JSON from the API, and the next page if any."""
        if params:
            url = url.update_query(params)
        key = str(url)
        headers = self._headers()
        if cached := self._etags.get(key):
            headers["If-None-Match"] = cached[0]
        try:
            async with self._session.get(url, headers=headers, timeout=API_TIMEOUT) as response:
                self._note(response)
                if response.status == 304 and cached:
                    return cached[1], cached[2]
                if response.status != 200:
                    raise _error(response.status, response.headers, what)
                try:
                    data = await response.json(content_type=None)
                except ValueError as err:
                    raise GitHubError("GitHub sent an unreadable answer") from err
                next_page = _next_link(response.headers.get("Link"))
                if etag := response.headers.get("ETag"):
                    self._etags[key] = (etag, data, next_page)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GitHubError("Cannot reach GitHub") from err
        return data, next_page

    async def get_account(self) -> Account:
        """The account the token belongs to."""
        data, _ = await self._get(_api_url("user"), "Account")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("login"), str)
            or not isinstance(data.get("id"), int)
        ):
            raise GitHubError("GitHub sent an unexpected answer for the account")
        return Account(id=data["id"], login=data["login"])

    async def list_repositories(self) -> list[str]:
        """Repositories the token can read ("owner/name")."""
        repositories: list[str] = []
        url: URL | None = _api_url("user", "repos")
        params: Mapping[str, str] | None = {"per_page": "100", "sort": "full_name"}
        for _ in range(MAX_REPOSITORY_PAGES):
            if url is None:
                break
            data, url = await self._get(url, "Repositories", params)
            params = None
            if not isinstance(data, list):
                raise GitHubError("GitHub sent an unexpected answer for the repositories")
            repositories.extend(
                item["full_name"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("full_name"), str)
            )
        return repositories

    async def get_repository(self, repository: str) -> Repository:
        """A repository found by its "owner/name" (GitHub follows renames)."""
        data, _ = await self._get(_api_url("repos", _repository(repository)), f"Repository {repository}")
        return _parse_repository(data, repository)

    async def get_current(self, repository: Repository) -> Repository:
        """A repository found by its id, with its current "owner/name"."""
        data, _ = await self._get(_repository_url(repository), f"Repository {repository.name}")
        current = _parse_repository(data, repository.name)
        if current.id != repository.id:
            raise GitHubError(f"GitHub sent another repository for {repository.name}")
        return current

    async def list_releases(self, repository: Repository) -> list[Release]:
        """The newest published releases, as GitHub sorts them (no drafts)."""
        data, _ = await self._get(
            _repository_url(repository, "releases"),
            f"Releases of {repository.name}",
            {"per_page": str(MAX_RELEASES)},
        )
        if not isinstance(data, list):
            raise GitHubError(f"GitHub sent an unexpected answer for the releases of {repository.name}")
        return [release for item in data if (release := _release(item)) is not None]

    async def get_release(self, repository: Repository, tag: str) -> Release:
        """The release of one tag."""
        data, _ = await self._get(
            _repository_url(repository, "releases", "tags", tag),
            f"Release {tag} of {repository.name}",
        )
        if (release := _release(data)) is None or release.tag != tag:
            raise GitHubNotFoundError(f"Release {tag} of {repository.name} not found")
        return release

    async def get_file(self, repository: Repository, path: str, ref: str) -> bytes | None:
        """A small file of the repository at a tag, or None when it does not exist."""
        url = _repository_url(repository, "contents", *path.split("/"))
        what = f"{path} of {repository.name}"
        try:
            async with self._session.get(
                url.with_query(ref=ref),
                headers=self._headers("application/vnd.github.raw+json"),
                timeout=API_TIMEOUT,
            ) as response:
                self._note(response)
                if response.status == 404:
                    return None
                if response.status != 200:
                    raise _error(response.status, response.headers, what)
                return await _read_body(
                    response, limit=MAX_FILE_SIZE, too_large=lambda: GitHubError(f"{what} is too large")
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GitHubError("Cannot reach GitHub") from err

    async def download_archive(
        self, repository: Repository, tag: str, progress: ProgressCallback | None = None
    ) -> bytes:
        """The source archive (.tar.gz) of a tag."""
        return await self._download(
            _repository_url(repository, "tarball", tag),
            "application/vnd.github+json",
            progress,
        )

    async def download_asset(
        self, repository: Repository, asset: Asset, progress: ProgressCallback | None = None
    ) -> bytes:
        """A file attached to a release."""
        if asset.size > MAX_DOWNLOAD_SIZE:
            raise DownloadTooLargeError
        return await self._download(
            _repository_url(repository, "releases", "assets", str(asset.id)),
            "application/octet-stream",
            progress,
        )

    async def _download(self, url: URL, accept: str, progress: ProgressCallback | None) -> bytes:
        """Ask the API for a download, then fetch it from GitHub's download host
        without the token. The API may also send the file itself."""
        for _ in range(MAX_API_REDIRECTS + 1):
            try:
                async with self._session.get(
                    url, headers=self._headers(accept), allow_redirects=False, timeout=DOWNLOAD_TIMEOUT
                ) as response:
                    self._note(response)
                    if response.status == 200:
                        return await _read_body(response, progress)
                    if response.status not in REDIRECT_STATUSES:
                        raise _error(response.status, response.headers, "Download")
                    location = url.join(URL(response.headers.get("Location", ""), encoded=True))
            except (aiohttp.ClientError, TimeoutError) as err:
                raise GitHubError("Cannot download from GitHub") from err
            if not _is_api_url(location):
                break
            url = location
        else:
            raise GitHubError("GitHub redirected the download too many times")
        if not _is_download_url(location):
            raise GitHubError(f"GitHub redirected the download to an unexpected host ({location.host})")
        try:
            async with self._session.get(
                location, allow_redirects=False, timeout=DOWNLOAD_TIMEOUT
            ) as response:
                if response.status != 200:
                    raise GitHubError(f"The download failed (HTTP {response.status})")
                return await _read_body(response, progress)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GitHubError("The download was interrupted") from err
