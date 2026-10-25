"""Update entities: versions, release notes, installs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import nullcontext, suppress
import logging
from pathlib import Path
import threading
from typing import Any
from unittest.mock import patch

from homeassistant.components.update import (
    ATTR_IN_PROGRESS,
    ATTR_INSTALLED_VERSION,
    ATTR_LATEST_VERSION,
    ATTR_RELEASE_SUMMARY,
    ATTR_RELEASE_URL,
    ATTR_VERSION,
    DOMAIN as UPDATE_DOMAIN,
    SERVICE_INSTALL,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.private_repositories.const import DOMAIN, ISSUE_RESTART, SIGNAL_INSTALL, STORAGE_KEY
from custom_components.private_repositories.installer import InstallError
from custom_components.private_repositories.manager import (
    DomainRefusedError,
    Manager,
    _label,
    log_failure,
    log_late_failure,
)

from .conftest import (
    API,
    REPOSITORY,
    REPOSITORY_ID,
    FakeGitHub,
    WatchedLock,
    first_of,
    install_locally,
    integration_files,
    manifest,
    no_room_to_swap,
    release,
    repository_id,
    repository_subentry,
    tarball,
    token_entry,
    zipball,
)

ENTITY = "update.gizmo"


def _files(folder: Path) -> set[str]:
    """Every file under a folder."""
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_update_available(hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases(
        [
            release("v1.2.0", name="Shiny", body="New things"),
            release("v2.0.0b1", prerelease=True),
            release("v1.1.0", body="Fixes"),
            release("v1.0.0"),
        ]
    )
    await setup(hass, entry)
    state = hass.states.get(ENTITY)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_INSTALLED_VERSION] == "1.0.0"
    assert state.attributes[ATTR_LATEST_VERSION] == "1.2.0"
    assert state.attributes[ATTR_RELEASE_SUMMARY] == "Shiny"
    assert state.attributes[ATTR_RELEASE_URL].endswith("/releases/tag/v1.2.0")
    # The tracked integration's icon, wherever this version of Home Assistant serves icons from.
    assert state.attributes["entity_picture"].endswith("/gizmo/icon.png")
    assert DOMAIN not in state.attributes["entity_picture"]

    entity = hass.data[UPDATE_DOMAIN].get_entity(ENTITY)
    notes = await entity.async_release_notes()
    # Every release since the installed one, newest first; no pre-release.
    assert notes == "## Shiny\n\nNew things\n\n## v1.1.0\n\nFixes"


async def test_up_to_date(hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.0.0", body="Only notes")])
    await setup(hass, entry)
    state = hass.states.get(ENTITY)
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_RELEASE_SUMMARY] is None
    entity = hass.data[UPDATE_DOMAIN].get_entity(ENTITY)
    assert await entity.async_release_notes() == "Only notes"


async def test_repository_unreachable(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    github.user()
    github.releases([], status=404)
    await setup(hass, entry)
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE


async def test_install(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    config = Path(hass.config.config_dir)
    install_locally(config, version="1.0.0")
    github.user()
    github.releases([release("v1.1.0"), release("v1.0.0")])
    github.hacs_json(None)
    github.archive("v1.1.0", tarball(integration_files(version="1.1.0")))
    await setup(hass, entry)
    assert hass.states.get(ENTITY).state == STATE_ON

    await hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
    state = hass.states.get(ENTITY)
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_INSTALLED_VERSION] == "1.1.0"
    assert state.attributes[ATTR_IN_PROGRESS] is False
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RESTART)
    assert issue is not None
    assert issue.translation_placeholders == {"integrations": r"Gizmo 1\.1\.0"}
    # Stored at once: a restart must not lose what installed these files.
    record = {"repository": REPOSITORY, "repository_id": REPOSITORY_ID, "tag": "v1.1.0", "version": "1.1.0"}
    assert hass_storage[STORAGE_KEY]["data"] == {"gizmo": record}


async def test_restart_issue_shows_names_as_plain_text(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    """The repair is Markdown, and the name comes from the release."""
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    github.hacs_json(None)
    github.archive("v1.1.0", tarball(integration_files(version="1.1.0", name="Gizmo\n\n**x** [y](z)")))
    await setup(hass, entry)
    await hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RESTART)
    assert issue.translation_placeholders == {"integrations": r"Gizmo \*\*x\*\* \[y\]\(z\) 1\.1\.0"}


def test_restart_labels_keep_the_version() -> None:
    assert _label("G" * 500, "1.0.0") == "G" * 100 + r" 1\.0\.0"
    assert _label(" Gizmo\t ", "") == "Gizmo"
    # Put back after an interrupted install, a manifest's name is used as it is.
    assert _label("Giz" + " " * 200 + "mo", "1.0.0") == r"Giz mo 1\.0\.0"
    assert _label("Gizmo", "1" * 100) == "Gizmo " + "1" * 64


def test_restart_labels_make_no_links() -> None:
    """Markdown turns bare addresses into links: the name of a release must not
    look like a link of Home Assistant's own."""
    label = _label("Gizmo https://evil.example www.evil.example me@evil.example", "1.0")
    assert label == r"Gizmo https\://evil\.example www\.evil\.example me\@evil\.example 1\.0"


def test_restart_labels_make_no_list_or_entity() -> None:
    """A leading "- " makes a list, and "&...;" a character."""
    assert _label("- Gizmo &copy;", "1.0") == r"\- Gizmo \&copy; 1\.0"


async def test_install_specific_version(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, aioclient_mock
) -> None:
    config = Path(hass.config.config_dir)
    install_locally(config, version="1.1.0")
    github.user()
    github.releases([release("v1.1.0")])
    aioclient_mock.get(github.url("releases/tags/v0.9.0"), status=404)
    aioclient_mock.get(github.url("releases/tags/0.9.0"), json=release("0.9.0"))
    github.hacs_json(None)
    github.archive("0.9.0", tarball(integration_files(version="0.9.0")))
    await setup(hass, entry)

    await hass.services.async_call(
        UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY, ATTR_VERSION: "0.9.0"}, blocking=True
    )
    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_INSTALLED_VERSION] == "0.9.0"
    assert state.state == STATE_ON

    with pytest.raises(HomeAssistantError, match=r"no release 5\.0\.0"):
        aioclient_mock.get(github.url("releases/tags/v5.0.0"), status=404)
        aioclient_mock.get(github.url("releases/tags/5.0.0"), status=404)
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY, ATTR_VERSION: "5.0.0"}, blocking=True
        )


async def test_install_from_a_repository_that_also_holds_an_app(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    """The layout of a repository shipping an integration and a Supervisor app:
    only custom_components/<domain> is installed."""
    config = Path(hass.config.config_dir)
    install_locally(config, version="1.0.0")
    github.user()
    github.releases([release("v2.0.0")])
    github.hacs_json({"name": "Gizmo", "homeassistant": "2025.6.1"})
    files = integration_files(version="2.0.0") | {
        "repository.yaml": b"name: Gizmo apps\nurl: https://example.com/gizmo\n",
        "gizmo_app/config.yaml": b"name: Gizmo app\nversion: 2.0.0\nslug: gizmo_app\n",
        "gizmo_app/Dockerfile": b"FROM scratch\n",
        "gizmo_app/translations/en.yaml": b"configuration: {}\n",
    }
    github.archive("v2.0.0", tarball(files))
    before = _files(config)
    await setup(hass, entry)
    await hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
    assert hass.states.get(ENTITY).attributes[ATTR_INSTALLED_VERSION] == "2.0.0"
    # The integration's files replaced the old ones, and nothing else was added
    # (but what Home Assistant stores).
    integration = {"gizmo/__init__.py", "gizmo/manifest.json", "gizmo/translations/en.json"}
    assert _files(config / "custom_components") == integration
    added = {name for name in _files(config) - before if not name.startswith(".storage/")}
    assert added == {f"custom_components/{name}" for name in integration} - before


async def test_install_zip_release(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, aioclient_mock
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    # The asset hacs.json names, not the first one listed.
    assets = [{"id": 41, "name": "docs.zip", "size": 1}, {"id": 42, "name": "gizmo.zip", "size": 1}]
    github.releases([release("v2.0.0", assets=assets)])
    github.hacs_json({"zip_release": True, "filename": "gizmo.zip"})
    for asset_id, version in ((41, "9.9.9"), (42, "2.0.0")):
        location = f"https://release-assets.githubusercontent.com/{asset_id}"
        aioclient_mock.get(
            f"{API}/repositories/{REPOSITORY_ID}/releases/assets/{asset_id}",
            status=302,
            headers={"Location": location},
        )
        files = {"manifest.json": manifest(version=version), "translations/en.json": b"{}"}
        aioclient_mock.get(location, content=zipball(files, folders=True))
    await setup(hass, entry)
    await hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
    assert hass.states.get(ENTITY).attributes[ATTR_INSTALLED_VERSION] == "2.0.0"
    # The entity names the tag: the files tell which asset was installed.
    manifest_file = Path(hass.config.config_dir) / "custom_components" / "gizmo" / "manifest.json"
    assert manifest_file.read_bytes() == manifest(version="2.0.0")


async def test_install_zip_release_without_asset(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v2.0.0")])
    github.hacs_json({"zip_release": True, "filename": "gizmo.zip"})
    await setup(hass, entry)
    with pytest.raises(HomeAssistantError, match=r"no asset named 'gizmo\.zip'"):
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )
    assert hass.states.get(ENTITY).attributes[ATTR_INSTALLED_VERSION] == "1.0.0"


async def test_install_refuses_another_integration(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v2.0.0")])
    github.hacs_json(None)
    github.archive("v2.0.0", tarball(integration_files("renamed", version="2.0.0")))
    await setup(hass, entry)
    with pytest.raises(HomeAssistantError, match="renamed"):
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )
    assert not (Path(hass.config.config_dir) / "custom_components" / "renamed").exists()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RESTART) is None


async def test_tag_differs_from_manifest_version(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    """A release tagged "release-7" installs manifest version 1.0.0: it is still up to date afterwards."""
    install_locally(Path(hass.config.config_dir), version="0.1.0")
    github.user()
    github.releases([release("release-7")])
    github.hacs_json(None)
    github.archive("release-7", tarball(integration_files(version="1.0.0")))
    await setup(hass, entry)
    assert hass.states.get(ENTITY).state == STATE_ON
    await hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_INSTALLED_VERSION] == "release-7"
    assert state.state == STATE_OFF


async def test_progress(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    github.hacs_json(None)
    data = tarball(integration_files(version="1.1.0"))
    location = "https://codeload.github.com/gizmo.tar.gz"
    aioclient_mock.get(github.url("tarball/v1.1.0"), status=302, headers={"Location": location})
    # Twice the announced size: the bar stops at 45 %, then the install ends it.
    aioclient_mock.get(location, content=data, headers={"Content-Length": str(2 * len(data))})
    await setup(hass, entry)
    seen: list[tuple[bool, int | None]] = []

    @callback
    def changed(event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        seen.append((state.attributes[ATTR_IN_PROGRESS], state.attributes["update_percentage"]))

    async_track_state_change_event(hass, [ENTITY], changed)
    await hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
    assert seen[0] == (True, None)
    assert (True, 45) in seen
    assert seen[-1] == (False, None)
    assert hass.states.get(ENTITY).attributes[ATTR_INSTALLED_VERSION] == "1.1.0"


@pytest.mark.parametrize("version", ["../../user", "1.0 ; x", "-1", ""])
async def test_install_refuses_odd_versions(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    version: str,
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    await setup(hass, entry)
    calls = aioclient_mock.call_count
    with pytest.raises(ServiceValidationError, match="is not a version"):
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY, ATTR_VERSION: version}, blocking=True
        )
    assert aioclient_mock.call_count == calls


async def test_install_when_github_fails(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    aioclient_mock.get(github.url("releases/tags/v0.9.0"), status=502)
    github.hacs_json(None)
    aioclient_mock.get(github.url("tarball/v1.1.0"), status=404)
    await setup(hass, entry)
    with pytest.raises(HomeAssistantError, match=rf"Cannot check the releases of {REPOSITORY}"):
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY, ATTR_VERSION: "0.9.0"}, blocking=True
        )
    with pytest.raises(HomeAssistantError, match=r"Cannot install Gizmo v1\.1\.0: Download not found"):
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )
    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_IN_PROGRESS] is False
    assert state.attributes[ATTR_INSTALLED_VERSION] == "1.0.0"


async def test_nothing_to_install(hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([])
    await setup(hass, entry)
    entity = hass.data[UPDATE_DOMAIN].get_entity(ENTITY)
    assert await entity.async_release_notes() is None
    with pytest.raises(HomeAssistantError, match="has no release to install"):
        await entity.async_install(None, False)


async def test_install_goes_on_when_its_caller_goes_away(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    """Cancelling the service call does not stop an install halfway, nor free the lock early."""
    config = Path(hass.config.config_dir)
    install_locally(config, version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    github.hacs_json(None)
    release_download = asyncio.Event()
    data = tarball(integration_files(version="1.1.0"))

    async def download(method: str, url, body) -> AiohttpClientMockResponse:
        await release_download.wait()
        return AiohttpClientMockResponse(method, url, response=data)

    aioclient_mock.get(github.url("tarball/v1.1.0"), side_effect=download)
    await setup(hass, entry)
    manager = entry.runtime_data.manager
    call = hass.async_create_task(
        hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
    )
    await asyncio.sleep(0.01)
    assert manager.installing
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert manager.installing
    release_download.set()
    await hass.async_block_till_done()
    assert not manager.installing
    assert (config / "custom_components" / "gizmo" / "manifest.json").read_bytes() == manifest(
        version="1.1.0"
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RESTART) is not None


def _held(event: asyncio.Event, **answer: Any):
    """A download GitHub only sends once event is set."""

    async def download(method: str, url: Any, body: Any) -> AiohttpClientMockResponse:
        await event.wait()
        return AiohttpClientMockResponse(method, url, **answer)

    return download


async def _setup_held_install(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    **answer: Any,
) -> asyncio.Event:
    """Gizmo 1.0.0 installed, 1.1.0 released: its download waits for the event."""
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    github.hacs_json(None)
    data = tarball(integration_files(version="1.1.0"))
    release_download = asyncio.Event()
    answer = answer or {"response": data, "headers": {"Content-Length": str(len(data))}}
    aioclient_mock.get(github.url("tarball/v1.1.0"), side_effect=_held(release_download, **answer))
    await setup(hass, entry)
    return release_download


@pytest.mark.parametrize("fails", [False, True], ids=["installed", "failed"])
async def test_a_reload_during_an_install_shows_its_end(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    fails: bool,
) -> None:
    """The entry reloads while an install runs (a new option, a new repository):
    its new entity shows the install, then how it ended."""
    answer = {"status": 500} if fails else {}
    release_download = await _setup_held_install(hass, entry, github, aioclient_mock, **answer)
    call = hass.async_create_task(
        hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
    )
    await asyncio.sleep(0.01)
    assert hass.states.get(ENTITY).attributes[ATTR_IN_PROGRESS] is True
    assert await hass.config_entries.async_reload(entry.entry_id)
    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_IN_PROGRESS] is True
    assert state.attributes[ATTR_INSTALLED_VERSION] == "1.0.0"
    # The download then reports its progress to the entity that went away.
    release_download.set()
    if fails:
        with pytest.raises(HomeAssistantError):
            await call
    else:
        await call
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_IN_PROGRESS] is False
    assert state.attributes[ATTR_INSTALLED_VERSION] == ("1.0.0" if fails else "1.1.0")
    assert state.state == (STATE_ON if fails else STATE_OFF)
    assert "Traceback" not in caplog.text


async def test_a_cancelled_install_call_still_shows_the_end(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    """A stopped script or an automation that restarts cancels its call, not the install."""
    release_download = await _setup_held_install(hass, entry, github, aioclient_mock)
    call = hass.async_create_task(
        hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
    )
    await asyncio.sleep(0.01)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    # Still installing: Install is not offered again meanwhile.
    assert hass.states.get(ENTITY).attributes[ATTR_IN_PROGRESS] is True
    release_download.set()
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_IN_PROGRESS] is False
    assert state.attributes[ATTR_INSTALLED_VERSION] == "1.1.0"
    assert state.state == STATE_OFF


async def test_an_install_stopped_on_its_own(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    """Home Assistant stopping cancels the install itself: its caller hears of it."""
    await _setup_held_install(hass, entry, github, aioclient_mock)
    with (
        patch.object(Manager, "_async_download_and_install", side_effect=asyncio.CancelledError),
        pytest.raises(HomeAssistantError, match="Home Assistant is restarting"),
    ):
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )
    assert hass.states.get(ENTITY).attributes[ATTR_IN_PROGRESS] is False


async def test_installs_of_other_integrations_leave_the_entity_alone(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    await setup(hass, entry)
    coordinator = entry.runtime_data
    with patch.object(coordinator, "async_update_listeners") as update_listeners:
        async_dispatcher_send(hass, SIGNAL_INSTALL, "other", "2.0.0")
        async_dispatcher_send(hass, SIGNAL_INSTALL, None, None)
    update_listeners.assert_not_called()
    assert coordinator.installed == {"gizmo": "1.0.0"}


@pytest.mark.parametrize(
    ("hacs", "condition", "message"),
    [
        (
            {"homeassistant": "2999.1.0"},
            None,
            "Cannot install Gizmo v1.1.0: it needs Home Assistant 2999.1.0 or newer",
        ),
        (None, "no_space", "Cannot install Gizmo v1.1.0: there is not enough free space"),
        (
            None,
            "restarting",
            "Cannot install Gizmo v1.1.0: Home Assistant is restarting. Try again once it has started",
        ),
    ],
)
async def test_expected_install_failures_have_their_own_texts(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    hacs: dict[str, Any] | None,
    condition: str | None,
    message: str,
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    github.hacs_json(hacs)
    github.archive("v1.1.0", tarball(integration_files(version="1.1.0")))
    await setup(hass, entry)
    manager = entry.runtime_data.manager
    if condition == "restarting":
        manager.async_block_installs()
    with (
        no_room_to_swap() if condition == "no_space" else nullcontext(),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )
    manager.async_unblock_installs()
    assert str(raised.value) == message
    assert hass.states.get(ENTITY).attributes[ATTR_INSTALLED_VERSION] == "1.0.0"


async def test_an_unwritable_configuration_folder(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    github.hacs_json(None)
    github.archive("v1.1.0", tarball(integration_files(version="1.1.0")))
    await setup(hass, entry)
    with (
        patch(
            "custom_components.private_repositories.manager.tempfile.TemporaryDirectory",
            side_effect=OSError(30, "Read-only file system"),
        ),
        pytest.raises(
            HomeAssistantError, match=r"Cannot install Gizmo v1\.1\.0: Cannot write in .*: Read-only"
        ),
    ):
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )
    assert hass.states.get(ENTITY).attributes[ATTR_INSTALLED_VERSION] == "1.0.0"


async def test_a_download_too_large_has_its_own_text(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    asset = {"id": 42, "name": "gizmo.zip", "size": 60 * 1024 * 1024}
    github.releases([release("v1.1.0", assets=[asset])])
    github.hacs_json({"zip_release": True, "filename": "gizmo.zip"})
    await setup(hass, entry)
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )
    assert raised.value.translation_key == "install_too_large"
    assert str(raised.value) == (
        "Cannot install Gizmo v1.1.0: the release is over 50 MB, too large to download"
    )


async def test_release_notes_stop_at_ten_releases(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release(f"v1.{minor}.0", body=f"Notes {minor}") for minor in range(12, 0, -1)])
    await setup(hass, entry)
    notes = await hass.data[UPDATE_DOMAIN].get_entity(ENTITY).async_release_notes()
    assert [line for line in notes.splitlines() if line.startswith("## ")] == [
        f"## v1.{minor}.0" for minor in range(12, 2, -1)
    ]


async def test_install_failing_after_its_caller_went_away_is_logged(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = Path(hass.config.config_dir)
    install_locally(config, version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    github.hacs_json(None)
    release_download = asyncio.Event()

    async def download(method: str, url, body) -> AiohttpClientMockResponse:
        await release_download.wait()
        return AiohttpClientMockResponse(method, url, status=500)

    aioclient_mock.get(github.url("tarball/v1.1.0"), side_effect=download)
    await setup(hass, entry)
    call = hass.async_create_task(
        hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
    )
    await asyncio.sleep(0.01)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    release_download.set()
    await hass.async_block_till_done()
    assert caplog.text.count("An install failed after its caller went away") == 1
    assert "shielded future" not in caplog.text
    assert (config / "custom_components" / "gizmo" / "manifest.json").read_bytes() == manifest(
        version="1.0.0"
    )


async def test_a_bug_after_the_caller_went_away_keeps_its_traceback(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    github.hacs_json(None)
    release_download = asyncio.Event()
    data = tarball(integration_files(version="1.1.0"))

    async def download(method: str, url, body) -> AiohttpClientMockResponse:
        await release_download.wait()
        return AiohttpClientMockResponse(method, url, response=data)

    aioclient_mock.get(github.url("tarball/v1.1.0"), side_effect=download)
    await setup(hass, entry)
    bug = RuntimeError("boom")
    with patch.object(Manager, "_unpack_and_install", side_effect=bug):
        call = hass.async_create_task(
            hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
        )
        await asyncio.sleep(0.01)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        release_download.set()
        await hass.async_block_till_done()
    [record] = [r for r in caplog.records if "after its caller went away" in r.getMessage()]
    assert record.levelno == logging.ERROR
    assert record.exc_info[1] is bug


def test_late_failures_from_a_bug_keep_their_traceback(caplog: pytest.LogCaptureFixture) -> None:
    """Nobody else hears of a failure once its caller went away."""
    log_late_failure(InstallError("bad release"), "Adding it failed")
    try:
        raise RuntimeError("boom")
    except RuntimeError as err:
        bug = err
    log_late_failure(bug, "Adding it failed")
    known, unexpected = caplog.records
    assert (known.levelno, known.getMessage(), known.exc_info) == (
        logging.WARNING,
        "Adding it failed: bad release",
        None,
    )
    assert (unexpected.levelno, unexpected.getMessage()) == (logging.ERROR, "Adding it failed")
    assert unexpected.exc_info[1] is bug


async def test_restart_issue_comes_back_after_being_ignored(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0"), release("v1.0.0")])
    github.hacs_json(None)
    github.archive("v1.1.0", tarball(integration_files(version="1.1.0")))
    github.archive("v1.0.0", tarball(integration_files(version="1.0.0")))
    await setup(hass, entry)
    await hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY}, blocking=True)
    ir.async_ignore_issue(hass, DOMAIN, ISSUE_RESTART, True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RESTART).dismissed_version is not None

    await hass.services.async_call(
        UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: ENTITY, ATTR_VERSION: "1.0.0"}, blocking=True
    )
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RESTART)
    assert issue.dismissed_version is None
    assert issue.translation_placeholders == {"integrations": r"Gizmo 1\.0\.0"}


async def test_a_backport_published_last_is_not_the_latest(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    """GitHub lists releases newest published first: a fix for an older line
    published after 1.2.0 comes first, but 1.2.0 is still the latest."""
    install_locally(Path(hass.config.config_dir), version="1.1.0")
    github.user()
    github.releases([release("v1.1.5", body="Backport"), release("v1.2.0", body="New"), release("v1.1.0")])
    await setup(hass, entry)
    assert hass.states.get(ENTITY).attributes[ATTR_LATEST_VERSION] == "1.2.0"
    notes = await hass.data[UPDATE_DOMAIN].get_entity(ENTITY).async_release_notes()
    assert notes == "## v1.2.0\n\nNew\n\n## v1.1.5\n\nBackport"


WIDGET = "octo/ha-widget"


async def _setup_two_held_installs(
    hass: HomeAssistant, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> tuple[MockConfigEntry, asyncio.Event, asyncio.Event]:
    """Gizmo and Widget 1.0.0 installed, 1.1.0 released for both: each download
    waits for its event."""
    config_entry = token_entry([repository_subentry(), repository_subentry(WIDGET, "widget", "Widget")])
    config_entry.add_to_hass(hass)
    github.user()
    events = []
    for name, domain in ((REPOSITORY, "gizmo"), (WIDGET, "widget")):
        install_locally(Path(hass.config.config_dir), domain, "1.0.0")
        github.releases([release("v1.1.0")], name=name)
        github.hacs_json(None, name=name)
        data = tarball(integration_files(domain, version="1.1.0"))
        event = asyncio.Event()
        aioclient_mock.get(github.url("tarball/v1.1.0", name), side_effect=_held(event, response=data))
        events.append(event)
    await setup(hass, config_entry)
    return config_entry, events[0], events[1]


def _install(hass: HomeAssistant, entity_id: str) -> asyncio.Task[None]:
    return hass.async_create_task(
        hass.services.async_call(UPDATE_DOMAIN, SERVICE_INSTALL, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    )


async def _until(condition: Callable[[], bool]) -> None:
    """Let other tasks run until condition holds: a number of turns, not a time."""
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("The condition never held")


def _in_progress(hass: HomeAssistant, entity_id: str) -> bool:
    return hass.states.get(entity_id).attributes[ATTR_IN_PROGRESS]


@pytest.mark.parametrize("how", ["reload", "cancel"])
async def test_a_queued_install_shows_and_runs_once(
    hass: HomeAssistant, github: FakeGitHub, aioclient_mock: AiohttpClientMocker, how: str
) -> None:
    """An install waiting for another one shows as in progress from the start,
    after a reload of its entry or a cancelled call too, and is not asked twice."""
    config_entry, gizmo, widget = await _setup_two_held_installs(hass, github, aioclient_mock)
    first = _install(hass, "update.gizmo")
    await _until(lambda: _in_progress(hass, "update.gizmo"))
    # Only the integration being installed.
    assert not _in_progress(hass, "update.widget")
    second = _install(hass, "update.widget")
    await _until(lambda: _in_progress(hass, "update.widget"))
    if how == "reload":
        assert await hass.config_entries.async_reload(config_entry.entry_id)
    else:
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
    assert _in_progress(hass, "update.gizmo")
    assert _in_progress(hass, "update.widget")
    # Home Assistant refuses a second install of an entity in progress, and the
    # Manager refuses one that got past it.
    with pytest.raises(HomeAssistantError):
        await _install(hass, "update.widget")
    entity = hass.data[UPDATE_DOMAIN].get_entity("update.widget")
    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_install(None, False)
    assert raised.value.translation_key == "install_already_installing"
    gizmo.set()
    widget.set()
    await first
    if how == "reload":
        await second
    await hass.async_block_till_done()
    for entity_id in ("update.gizmo", "update.widget"):
        attributes = hass.states.get(entity_id).attributes
        assert (attributes[ATTR_IN_PROGRESS], attributes[ATTR_INSTALLED_VERSION]) == (False, "1.1.0")
    downloads = [
        call[1].path for call in aioclient_mock.mock_calls if call[1].path.endswith("/tarball/v1.1.0")
    ]
    assert sorted(downloads) == sorted(
        f"/repositories/{repository_id(name)}/tarball/v1.1.0" for name in (REPOSITORY, WIDGET)
    )


async def test_a_refresh_during_an_install_never_shows_the_old_version(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    """A refresh reads the installed versions just before the files are swapped:
    the swap waits for the read, so the refresh cannot bring the old version
    back once the install has ended."""
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    github.hacs_json(None)
    github.archive("v1.1.0", tarball(integration_files(version="1.1.0")))
    await setup(hass, entry)
    manager = entry.runtime_data.manager
    manager._files_lock = lock = WatchedLock()
    reading, go_on = threading.Event(), threading.Event()
    real_read = Manager._read_versions

    def held_read(self: Manager, domains: Any) -> Any:
        versions = real_read(self, domains)
        reading.set()
        go_on.wait(5)
        return versions

    with patch.object(Manager, "_read_versions", held_read):
        refresh = hass.async_create_task(entry.runtime_data.async_refresh())
        assert await hass.async_add_executor_job(reading.wait, 5)
    install = _install(hass, ENTITY)
    await first_of(install, lock)
    go_on.set()
    await asyncio.gather(refresh, install)
    assert lock.contended.is_set()
    assert hass.states.get(ENTITY).attributes[ATTR_INSTALLED_VERSION] == "1.1.0"


async def test_a_stopped_install_nobody_waits_for_says_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Home Assistant stops an install whose caller went away: no failure to report."""
    task = asyncio.ensure_future(asyncio.Event().wait())
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    log_failure(task)
    assert not caplog.records


def test_refusals_name_what_they_protect() -> None:
    refused = DomainRefusedError("http")
    assert str(refused) == "The release would replace http, which is part of Home Assistant"
    assert str(DomainRefusedError(DOMAIN)) == (
        f"The release would replace {DOMAIN}, which is this integration itself"
    )
