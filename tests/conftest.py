"""Fixtures: a fake GitHub, and a configuration folder of our own."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager, suppress
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
from typing import Any, Literal
from unittest.mock import patch
import zipfile
import zlib

from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowManager, FlowResultType
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.private_repositories.const import (
    CONF_INTEGRATION,
    CONF_LOGIN,
    CONF_PRERELEASES,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    CONF_USER_ID,
    DOMAIN,
    SUBENTRY_REPOSITORY,
    WORK_DIR,
)
from custom_components.private_repositories.installer import MIN_FREE_SPACE

API = "https://api.github.com"
TOKEN = "github_pat_test"
LOGIN = "octo"
USER_ID = 583231
REPOSITORY = "octo/ha-gizmo"


def repository_id(name: str) -> int:
    """GitHub's id of a test repository, whatever the case of its name."""
    return zlib.crc32(name.lower().encode())


REPOSITORY_ID = repository_id(REPOSITORY)
EXPIRATION = "2030-01-01 00:00:00 UTC"
STRINGS = json.loads(
    (Path(__file__).parent.parent / "custom_components" / DOMAIN / "translations" / "en.json").read_text(
        encoding="utf-8"
    )
)
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components/."""


def _text(key: str) -> Any:
    node: Any = STRINGS
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _translation_problems(manager: FlowManager, flow: Any, result: Any) -> list[str]:
    """What en.json lacks to show a flow result of this integration."""
    hass: HomeAssistant = manager.hass
    kind = type(manager).__name__
    if kind == "ConfigEntriesFlowManager" and flow.handler == DOMAIN:
        prefix = "config"
    elif kind == "OptionsFlowManager" and (entry := hass.config_entries.async_get_entry(flow.handler)):
        prefix = "options" if entry.domain == DOMAIN else ""
    elif kind == "ConfigSubentryFlowManager" and (
        entry := hass.config_entries.async_get_entry(flow.handler[0])
    ):
        prefix = f"config_subentries.{flow.handler[1]}" if entry.domain == DOMAIN else ""
    elif kind == "RepairsFlowManager" and flow.handler == DOMAIN:
        issue = ir.async_get(hass).async_get_issue(DOMAIN, flow.issue_id)
        key = issue.translation_key if issue else None
        # Home Assistant's own flow, for issues this integration cannot fix.
        prefix = "" if isinstance(flow, ConfirmRepairFlow) else f"issues.{key or flow.issue_id}.fix_flow"
    else:
        prefix = ""
    if not prefix:
        return []
    placeholders = set(result.get("description_placeholders") or {})
    texts: list[tuple[str, Any]] = []
    if result["type"] is FlowResultType.FORM:
        texts.append((f"{prefix}.step.{result['step_id']}", _text(f"{prefix}.step.{result['step_id']}")))
        texts += [
            (f"{prefix}.error.{e}", _text(f"{prefix}.error.{e}"))
            for e in (result.get("errors") or {}).values()
        ]
        if (schema := result.get("data_schema")) is not None:
            # The label of each field (its description is optional).
            texts += [
                (key, _text(key))
                for field in schema.schema
                for key in [f"{prefix}.step.{result['step_id']}.data.{field}"]
            ]
    elif result["type"] is FlowResultType.SHOW_PROGRESS:
        # The step's title heads the dialog, the progress text is its body.
        texts.append(
            (f"{prefix}.step.{result['step_id']}.title", _text(f"{prefix}.step.{result['step_id']}.title"))
        )
        key = f"{prefix}.progress.{result['progress_action']}"
        texts.append((key, _text(key)))
    elif result["type"] is FlowResultType.ABORT:
        texts.append((f"{prefix}.abort.{result['reason']}", _text(f"{prefix}.abort.{result['reason']}")))
    return _text_problems(texts, placeholders)


def _text_problems(texts: list[tuple[str, Any]], placeholders: set[str]) -> list[str]:
    problems = []
    for key, text in texts:
        if text is None:
            problems.append(f"{key} is missing")
            continue
        strings = [text] if isinstance(text, str) else [text.get("title", ""), text.get("description", "")]
        if missing := {name for s in strings for name in PLACEHOLDER_RE.findall(s)} - placeholders:
            problems.append(f"{key} needs {sorted(missing)}")
    return problems


@pytest.fixture(autouse=True)
def check_translations() -> Iterator[None]:
    """Every issue, and every step, field, error and abort of this integration's
    flows, has its text in en.json, and gets the placeholders the text uses."""
    problems: list[str] = []
    handle_step = FlowManager._async_handle_step
    create_issue = ir.async_create_issue

    async def checked(self: FlowManager, flow: Any, *args: Any, **kwargs: Any) -> Any:
        result = await handle_step(self, flow, *args, **kwargs)
        problems.extend(_translation_problems(self, flow, result))
        return result

    def checked_issue(hass: HomeAssistant, domain: str, issue_id: str, **kwargs: Any) -> None:
        if domain == DOMAIN:
            key = f"issues.{kwargs['translation_key']}"
            texts = [(f"{key}.title", _text(f"{key}.title"))]
            if (description := _text(f"{key}.description")) is not None or not kwargs["is_fixable"]:
                texts.append((f"{key}.description", description))
            problems.extend(_text_problems(texts, set(kwargs.get("translation_placeholders") or {})))
        create_issue(hass, domain, issue_id, **kwargs)

    with (
        patch.object(FlowManager, "_async_handle_step", checked),
        patch.object(ir, "async_create_issue", checked_issue),
    ):
        yield
    assert not problems


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """A configuration folder of our own, where installs land."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def manifest(domain: str = "gizmo", version: str = "1.0.0", name: str = "Gizmo") -> bytes:
    """A manifest.json."""
    return json.dumps({"domain": domain, "name": name, "version": version}).encode()


def integration_files(domain: str = "gizmo", version: str = "1.0.0", name: str = "Gizmo") -> dict[str, bytes]:
    """The files of a repository holding one integration."""
    return {
        f"custom_components/{domain}/manifest.json": manifest(domain, version, name),
        f"custom_components/{domain}/__init__.py": b'"""Gizmo."""\n',
        f"custom_components/{domain}/translations/en.json": b"{}",
        "README.md": b"# Gizmo\n",
    }


def _folders(names: list[str]) -> list[str]:
    """Every folder above these files, each before what it holds."""
    folders: list[str] = []
    for name in names:
        for parent in reversed(PurePosixPath(name).parents[:-1]):
            if str(parent) not in folders:
                folders.append(str(parent))
    return sorted(folders)


def tarball(
    files: dict[str, bytes], top: str = "octo-ha-gizmo-abc1234", symlinks: dict[str, str] | None = None
) -> bytes:
    """A source archive as GitHub builds them (git archive): everything in one
    folder, with an entry for each folder."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        if top:
            folder = tarfile.TarInfo(top)
            folder.type = tarfile.DIRTYPE
            tar.addfile(folder)
        for name in _folders(list(files)):
            folder = tarfile.TarInfo(f"{top}/{name}" if top else name)
            folder.type = tarfile.DIRTYPE
            tar.addfile(folder)
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return buffer.getvalue()


def zipball(files: dict[str, bytes], folders: bool = False, compression: int = zipfile.ZIP_STORED) -> bytes:
    """A zip file attached to a release, with an entry for each folder if asked
    (as zip -r makes them)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name in _folders(list(files)) if folders else []:
            archive.writestr(f"{name}/", b"")
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def release(
    tag: str = "v1.0.0", *, prerelease: bool = False, body: str = "Notes", **extra: Any
) -> dict[str, Any]:
    """A release as the API describes it."""
    return {
        "tag_name": tag,
        "name": extra.pop("name", tag),
        "body": body,
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
        "draft": False,
        "prerelease": prerelease,
        "assets": [],
        **extra,
    }


class FakeGitHub:
    """Registers GitHub answers on the aiohttp mock. A repository is reached by
    its id, as the integration does, except when it is first found by name."""

    def __init__(self, mock: AiohttpClientMocker) -> None:
        self.mock = mock

    def _answers(self, url: str) -> bool:
        return any(str(response._url) == url for response in self.mock._mocks)

    def user(self, login: str = LOGIN, status: int = 200, user_id: int = USER_ID) -> None:
        self.mock.get(
            f"{API}/user",
            status=status,
            json={"login": login, "id": user_id} if status == 200 else {"message": "Bad credentials"},
            headers={"github-authentication-token-expiration": EXPIRATION},
        )

    def repositories(self, names: list[str]) -> None:
        self.mock.get(f"{API}/user/repos", json=[{"full_name": name} for name in names])

    def repository(self, name: str = REPOSITORY, status: int = 200, full_name: str | None = None) -> None:
        """A repository, found by its name and by its id (full_name: its current
        name, when it was renamed)."""
        current = full_name or name
        body = {"id": repository_id(current), "full_name": current}
        self.mock.get(f"{API}/repos/{name}", status=status, json=body)
        self.mock.get(f"{API}/repositories/{repository_id(current)}", status=status, json=body)

    def releases(self, releases: list[dict[str, Any]], name: str = REPOSITORY, status: int = 200) -> None:
        """The releases of a repository, which is also found, unless it was set before."""
        if not self._answers(f"{API}/repositories/{repository_id(name)}"):
            self.repository(name)
        self.mock.get(f"{API}/repositories/{repository_id(name)}/releases", status=status, json=releases)

    def url(self, path: str, name: str = REPOSITORY) -> str:
        """The address of a resource of a repository."""
        return f"{API}/repositories/{repository_id(name)}/{path}"

    def hacs_json(self, data: dict[str, Any] | None = None, name: str = REPOSITORY) -> None:
        if data is None:
            self.mock.get(self.url("contents/hacs.json", name), status=404)
        else:
            self.mock.get(self.url("contents/hacs.json", name), text=json.dumps(data))

    def archive(self, tag: str, data: bytes, name: str = REPOSITORY) -> None:
        location = f"https://codeload.github.com/{name}/legacy.tar.gz/refs/tags/{tag}"
        self.mock.get(self.url(f"tarball/{tag}", name), status=302, headers={"Location": location})
        self.mock.get(location, content=data, headers={"Content-Length": str(len(data))})


@pytest.fixture
def github(aioclient_mock: AiohttpClientMocker) -> FakeGitHub:
    """A fake GitHub API."""
    return FakeGitHub(aioclient_mock)


def repository_subentry(
    repository: str = REPOSITORY, domain: str = "gizmo", title: str = "Gizmo", prereleases: bool = False
) -> ConfigSubentryData:
    """A tracked repository."""
    return ConfigSubentryData(
        data={
            CONF_REPOSITORY: repository,
            CONF_REPOSITORY_ID: repository_id(repository),
            CONF_INTEGRATION: domain,
            CONF_PRERELEASES: prereleases,
        },
        subentry_type=SUBENTRY_REPOSITORY,
        title=title,
        unique_id=str(repository_id(repository)),
    )


def token_entry(
    subentries: list[ConfigSubentryData] | None = None, token: str = TOKEN, **kwargs: Any
) -> MockConfigEntry:
    """A token entry, tracking some repositories."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=LOGIN,
        data={CONF_ACCESS_TOKEN: token, CONF_LOGIN: LOGIN, CONF_USER_ID: USER_ID},
        subentries_data=subentries or [],
        **kwargs,
    )


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A token tracking one repository."""
    config_entry = token_entry([repository_subentry()])
    config_entry.add_to_hass(hass)
    return config_entry


def install_locally(config_dir: Path, domain: str = "gizmo", version: str = "1.0.0") -> None:
    """An integration already in custom_components/."""
    folder = config_dir / "custom_components" / domain
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_bytes(manifest(domain, version))


@contextmanager
def no_room_to_swap() -> Iterator[None]:
    """Enough free space to unpack a release, not to swap it in."""
    real = shutil.disk_usage

    def disk_usage(path: Any) -> Any:
        usage = real(path)
        return usage._replace(free=MIN_FREE_SPACE - 1) if Path(path).name == WORK_DIR else usage

    with patch("custom_components.private_repositories.installer.shutil.disk_usage", disk_usage):
        yield


class WatchedLock(asyncio.Lock):
    """A lock that tells when someone has to wait for it."""

    def __init__(self) -> None:
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self) -> Literal[True]:
        if self.locked():
            self.contended.set()
        return await super().acquire()


async def first_of(task: asyncio.Task[Any], lock: WatchedLock) -> None:
    """Wait until the task ends, or someone waits for the lock: whichever comes
    first, with no timing involved."""
    contended = asyncio.ensure_future(lock.contended.wait())
    try:
        await asyncio.wait({task, contended}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        contended.cancel()
        with suppress(asyncio.CancelledError):
            await contended
