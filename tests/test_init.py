"""Setting up tokens, token expiry, repairs and diagnostics."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext, suppress
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import shutil
import threading
import time
from typing import Any
from unittest.mock import Mock, PropertyMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import DATA_DISPATCHER
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components import private_repositories
from custom_components.private_repositories import DATA_MANAGER
from custom_components.private_repositories.const import (
    CONF_REMINDER_DAYS,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    DOMAIN,
    ISSUE_RESTART,
    ISSUE_TOKEN_EXPIRING,
    SIGNAL_INSTALL,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from custom_components.private_repositories.github import GitHubClient, Release, Repository
from custom_components.private_repositories.installer import work_dir
from custom_components.private_repositories.manager import Manager

from .conftest import (
    API,
    LOGIN,
    REPOSITORY,
    REPOSITORY_ID,
    TOKEN,
    USER_ID,
    FakeGitHub,
    WatchedLock,
    first_of,
    install_locally,
    integration_files,
    manifest,
    release,
    tarball,
    token_entry,
)

SENSOR = "sensor.github_octo_token_expiration"


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_and_unload(hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub) -> None:
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(SENSOR).state == "2030-01-01T00:00:00+00:00"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_rejected_token_starts_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    github.user(status=401)
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_github_down_retries(hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub) -> None:
    github.user(status=503)
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_token_without_expiration(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, github: FakeGitHub
) -> None:
    aioclient_mock.get(f"{API}/user", json={"login": LOGIN, "id": USER_ID})
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    assert hass.states.get(SENSOR).state == STATE_UNKNOWN
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_TOKEN_EXPIRING}_{entry.entry_id}")


async def test_token_expiring(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(datetime(2029, 12, 20, tzinfo=UTC))
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    issue_id = f"{ISSUE_TOKEN_EXPIRING}_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    # The day it ends where Home Assistant is.
    date = dt_util.as_local(datetime(2030, 1, 1, tzinfo=UTC)).date().isoformat()
    assert issue.translation_placeholders == {"account": LOGIN, "date": date}
    assert issue.data == {"entry_id": entry.entry_id}

    # Renew it from the repair.
    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "renew"
    assert result["description_placeholders"]["date"] == date
    assert result["description_placeholders"]["account"] == LOGIN
    result = await manager.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: "ghp_classic"})
    assert result["errors"] == {"base": "not_fine_grained"}
    aioclient_mock.clear_requests()
    # Same login, another account: GitHub logins can be renamed and reused.
    github.user(user_id=USER_ID + 1)
    result = await manager.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: "github_pat_other"})
    assert result["errors"] == {"base": "wrong_account"}
    aioclient_mock.clear_requests()
    github.user()
    github.repository(status=404)
    result = await manager.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: "github_pat_narrow"})
    assert result["errors"] == {"base": "missing_access"}
    assert result["description_placeholders"]["repositories"] == REPOSITORY

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{API}/user",
        json={"login": LOGIN, "id": USER_ID},
        headers={"github-authentication-token-expiration": "2031-01-01 00:00:00 UTC"},
    )
    github.repository()
    github.releases([release("v1.0.0")])
    result = await manager.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: "github_pat_renewed"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_ACCESS_TOKEN] == "github_pat_renewed"
    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert hass.states.get(SENSOR).state == "2031-01-01T00:00:00+00:00"


async def test_reminder_days_option(
    hass: HomeAssistant, github: FakeGitHub, freezer: FrozenDateTimeFactory
) -> None:
    # 12 days left: the default would remind already, 7 days does not yet.
    freezer.move_to(datetime(2029, 12, 20, tzinfo=UTC))
    entry = token_entry(options={CONF_REMINDER_DAYS: 7})
    entry.add_to_hass(hass)
    github.user()
    await setup(hass, entry)
    issue_id = f"{ISSUE_TOKEN_EXPIRING}_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    freezer.move_to(datetime(2029, 12, 26, tzinfo=UTC))
    async_fire_time_changed(hass, datetime(2029, 12, 26, tzinfo=UTC) + timedelta(hours=1))
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def _restart_issue(hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    github.hacs_json(None)
    github.archive("v1.1.0", tarball(integration_files(version="1.1.0")))
    await setup(hass, entry)
    await hass.services.async_call("update", "install", {"entity_id": "update.gizmo"}, blocking=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RESTART)


async def test_restart_repair(hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub) -> None:
    await _restart_issue(hass, entry, github)
    checks = async_mock_service(hass, "homeassistant", "check_config")
    restarts = async_mock_service(hass, "homeassistant", "restart")
    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": ISSUE_RESTART})
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"integrations": r"Gizmo 1\.1\.0"}
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(checks) == 1
    assert len(restarts) == 1
    # Until Home Assistant stops, nothing more is installed.
    with pytest.raises(HomeAssistantError, match="Home Assistant is restarting"):
        await hass.services.async_call(
            "update", "install", {"entity_id": "update.gizmo", "version": "1.1.0"}, blocking=True
        )
    # It did not stop after all: installs are accepted again later.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=3))
    await hass.async_block_till_done()
    assert not entry.runtime_data.manager._unblock


async def test_restart_repair_invalid_config(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    await _restart_issue(hass, entry, github)
    restarts = async_mock_service(hass, "homeassistant", "restart")

    async def refuse(call: ServiceCall) -> None:
        raise HomeAssistantError("Invalid config for automation")

    hass.services.async_register("homeassistant", "check_config", refuse)
    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": ISSUE_RESTART})
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_config"
    assert not restarts
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RESTART)


async def test_restart_repair_closed_during_the_check(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    """The dialog's connection goes away while the configuration is checked: no
    restart, and installs go on."""
    await _restart_issue(hass, entry, github)
    restarts = async_mock_service(hass, "homeassistant", "restart")
    checking = asyncio.Event()

    async def check(call: ServiceCall) -> None:
        checking.set()
        await asyncio.Event().wait()

    hass.services.async_register("homeassistant", "check_config", check)
    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": ISSUE_RESTART})
    call = hass.async_create_task(manager.async_configure(result["flow_id"], {}))
    await checking.wait()
    installs = hass.data[DATA_MANAGER]
    assert installs._unblock is not None
    call.cancel()
    with suppress(asyncio.CancelledError):
        await call
    assert installs._unblock is None
    assert not restarts


async def test_no_install_while_home_assistant_stops(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    await setup(hass, entry)
    with (
        patch.object(HomeAssistant, "is_stopping", new_callable=PropertyMock, return_value=True),
        pytest.raises(HomeAssistantError, match="Home Assistant is restarting"),
    ):
        await hass.services.async_call("update", "install", {"entity_id": "update.gizmo"}, blocking=True)


async def test_blocking_installs_again_keeps_one_timer(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    manager = hass.data[DATA_MANAGER]
    first, second = Mock(), Mock()
    with patch(
        "custom_components.private_repositories.manager.async_call_later", side_effect=[first, second]
    ):
        manager.async_block_installs()
        manager.async_block_installs()
    first.assert_called_once_with()
    second.assert_not_called()
    manager.async_unblock_installs()
    second.assert_called_once_with()
    assert manager._unblock is None


async def test_restart_repair_during_migration(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    await _restart_issue(hass, entry, github)
    async_mock_service(hass, "homeassistant", "check_config")
    restarts = async_mock_service(hass, "homeassistant", "restart")
    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": ISSUE_RESTART})
    with patch(
        "custom_components.private_repositories.repairs.recorder.async_migration_in_progress",
        return_value=True,
    ):
        result = await manager.async_configure(result["flow_id"], {})
    assert result["reason"] == "migration_in_progress"
    assert not restarts


async def test_interrupted_install_is_recovered(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    config = Path(hass.config.config_dir)
    previous = work_dir(config) / "gizmo.previous"
    previous.mkdir(parents=True)
    (previous / "manifest.json").write_bytes(manifest(version="0.5.0"))
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    assert hass.states.get("update.gizmo").attributes["installed_version"] == "0.5.0"
    # Put back after Home Assistant listed its integrations: it loads at the next start.
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RESTART)
    assert issue.translation_placeholders == {"integrations": r"Gizmo 0\.5\.0"}
    assert list(work_dir(config).iterdir()) == []


async def test_diagnostics(
    hass: HomeAssistant, hass_client, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    assert await async_setup_component(hass, "diagnostics", {})
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diagnostics["entry"]["data"][CONF_ACCESS_TOKEN] == "**REDACTED**"
    assert diagnostics["token_expiration"] == "2030-01-01T00:00:00+00:00"
    assert diagnostics["repositories"][0]["installed"] == "1.0.0"
    assert diagnostics["repositories"][0]["releases"] == [{"tag": "v1.0.0", "prerelease": False}]
    # Diagnostics end up in public issues: no token, account or repository.
    text = json.dumps(diagnostics)
    for secret in (TOKEN, LOGIN, str(USER_ID), REPOSITORY, "ha-gizmo", str(REPOSITORY_ID)):
        assert secret not in text


async def test_diagnostics_hide_the_repository_in_errors(
    hass: HomeAssistant, hass_client, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    assert await async_setup_component(hass, "diagnostics", {})
    github.user()
    github.releases([], status=404)
    await setup(hass, entry)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert (
        diagnostics["repositories"][0]["error"]
        == "Releases of **REDACTED** not found, or the token cannot read it"
    )
    text = json.dumps(diagnostics)
    for secret in (TOKEN, LOGIN, str(USER_ID), REPOSITORY, "ha-gizmo", str(REPOSITORY_ID)):
        assert secret not in text


@pytest.mark.parametrize("fix", ["unknown_issue"])
async def test_unknown_fix_flow(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, fix: str
) -> None:
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    # Straight into the registry: no text is checked for an issue the
    # integration never raises.
    ir.async_get(hass).async_get_or_create(
        DOMAIN,
        fix,
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=fix,
    )
    result = await repairs_flow_manager(hass).async_init(DOMAIN, data={"issue_id": fix})
    assert result["step_id"] == "confirm"


async def test_restart_waits_for_a_running_install(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    await _restart_issue(hass, entry, github)
    async_mock_service(hass, "homeassistant", "check_config")
    restarts = async_mock_service(hass, "homeassistant", "restart")
    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": ISSUE_RESTART})
    with patch("custom_components.private_repositories.manager.Manager.installing", new=True):
        result = await manager.async_configure(result["flow_id"], {})
    assert result["reason"] == "install_in_progress"
    assert not restarts


async def test_token_issue_follows_the_entry(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(datetime(2029, 12, 20, tzinfo=UTC))
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    issue_id = f"{ISSUE_TOKEN_EXPIRING}_{entry.entry_id}"
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
    # A reload keeps it, ignored.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id).dismissed_version is not None
    # A disabled entry drops it.
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_token_issue_leaves_with_an_entry_disabled_while_retrying(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Home Assistant does not unload an entry that waits to set up again."""
    freezer.move_to(datetime(2029, 12, 20, tzinfo=UTC))
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    issue_id = f"{ISSUE_TOKEN_EXPIRING}_{entry.entry_id}"
    aioclient_mock.clear_requests()
    github.user(status=500)
    await hass.config_entries.async_reload(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_a_new_token_gets_its_own_reminder(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A reminder the user ignored was about the old token."""
    freezer.move_to(datetime(2029, 12, 20, tzinfo=UTC))
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    issue_id = f"{ISSUE_TOKEN_EXPIRING}_{entry.entry_id}"
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{API}/user",
        json={"login": LOGIN, "id": USER_ID},
        headers={"github-authentication-token-expiration": "2029-12-27 00:00:00 UTC"},
    )
    github.repository()
    github.releases([release("v1.0.0")])
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCESS_TOKEN: "github_pat_next"}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue.dismissed_version is None
    assert (
        issue.translation_placeholders["date"]
        == dt_util.as_local(datetime(2029, 12, 27, tzinfo=UTC)).date().isoformat()
    )


async def test_the_same_token_keeps_its_reminder_ignored(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(datetime(2029, 12, 20, tzinfo=UTC))
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    issue_id = f"{ISSUE_TOKEN_EXPIRING}_{entry.entry_id}"
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
    github.repository()
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: TOKEN})
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id).dismissed_version is not None


@pytest.mark.parametrize(
    ("status", "error"), [(401, "invalid_auth"), (500, "cannot_connect"), (None, "unknown")]
)
async def test_renewal_errors(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    status: int | None,
    error: str,
) -> None:
    freezer.move_to(datetime(2029, 12, 20, tzinfo=UTC))
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": f"{ISSUE_TOKEN_EXPIRING}_{entry.entry_id}"})
    aioclient_mock.clear_requests()
    if status is None:
        failure: Any = patch.object(GitHubClient, "get_account", side_effect=ValueError("odd"))
    else:
        github.user(status=status)
        failure = nullcontext()
    with failure:
        result = await manager.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: "github_pat_renewed"})
    assert result["errors"] == {"base": error}
    assert entry.data[CONF_ACCESS_TOKEN] == TOKEN


async def test_tokens_set_up_together_forget_records_once(
    hass: HomeAssistant, hass_storage: dict[str, Any], github: FakeGitHub
) -> None:
    """Every token forgets the records of removed repositories as it is set up,
    while the others do the same."""
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"gizmo": {"repository": REPOSITORY, "tag": "v1.0.0", "version": "1.0.0"}},
    }
    entries = [token_entry(), token_entry(token="github_pat_other")]
    for config_entry in entries:
        config_entry.add_to_hass(hass)
    github.user()
    real_read = Manager._read_versions

    def slow_read(self: Manager, domains: Any) -> Any:
        # Long enough for every entry to start forgetting.
        time.sleep(0.05)
        return real_read(self, domains)

    with patch.object(Manager, "_read_versions", slow_read):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()
    assert [config_entry.state for config_entry in entries] == [ConfigEntryState.LOADED] * 2
    assert "gizmo" not in hass.data[DATA_MANAGER].records


async def test_an_install_during_a_forget_keeps_its_record(
    hass: HomeAssistant, hass_storage: dict[str, Any], entry: MockConfigEntry, github: FakeGitHub
) -> None:
    """The record an install writes while stale records are checked is not stale."""
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"gizmo": {"repository": REPOSITORY, "tag": "v0.9.0", "version": "0.9.0"}},
    }
    github.user()
    github.releases([release("v1.0.0")])
    github.hacs_json(None)
    github.archive("v1.0.0", tarball(integration_files(version="1.0.0")))
    await setup(hass, entry)
    manager = hass.data[DATA_MANAGER]
    assert manager.records["gizmo"]["tag"] == "v0.9.0"
    manager._files_lock = lock = WatchedLock()
    reading, done = threading.Event(), threading.Event()
    real_read = Manager._read_versions

    def held_read(self: Manager, domains: Any) -> Any:
        versions = real_read(self, domains)
        reading.set()
        done.wait(5)
        return versions

    with patch.object(Manager, "_read_versions", held_read):
        # gizmo's files are gone: its record is stale, until the install.
        forget = hass.async_create_task(manager.async_forget({"gizmo"}))
        assert await hass.async_add_executor_job(reading.wait, 5)
        new = Release(tag="v1.0.0", name="", body="", url="", prerelease=False)
        install = hass.async_create_task(
            manager.async_install(
                entry.runtime_data.client, Repository(REPOSITORY_ID, REPOSITORY), new, domain="gizmo"
            )
        )
        await first_of(install, lock)
        # Files are not swapped while versions are read.
        assert not install.done()
        assert lock.contended.is_set()
        done.set()
        await asyncio.gather(forget, install)
    assert manager.records["gizmo"]["tag"] == "v1.0.0"


async def test_rejected_token_drops_the_renewal_reminder(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Once GitHub rejects the token, reauthentication takes over from the reminder."""
    freezer.move_to(datetime(2029, 12, 20, tzinfo=UTC))
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    issue_id = f"{ISSUE_TOKEN_EXPIRING}_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    aioclient_mock.clear_requests()
    github.user(status=401)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_removed_repository_is_forgotten(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    """Its integration stays installed. Its install record stays as long as the
    files are those of the recorded release."""
    await _restart_issue(hass, entry, github)
    manager = entry.runtime_data.manager
    assert "gizmo" in manager.records
    subentry_id = next(iter(entry.subentries))
    assert hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("update.gizmo") is None
    folder = Path(hass.config.config_dir) / "custom_components" / "gizmo"
    assert folder.is_dir()
    assert "gizmo" in manager.records
    shutil.rmtree(folder)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert "gizmo" not in manager.records


async def test_removed_token_forgets_its_repositories(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    await _restart_issue(hass, entry, github)
    manager = entry.runtime_data.manager
    # Another version replaced it meanwhile.
    install_locally(Path(hass.config.config_dir), version="0.1.0")
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert "gizmo" not in manager.records


async def test_diagnostics_hide_the_repository_of_an_install(
    hass: HomeAssistant, hass_client, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    assert await async_setup_component(hass, "diagnostics", {})
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.1.0")])
    github.hacs_json(None)
    github.archive("v1.1.0", tarball(integration_files(version="1.1.0")))
    await setup(hass, entry)
    await hass.services.async_call("update", "install", {"entity_id": "update.gizmo"}, blocking=True)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diagnostics["repositories"][0]["last_install"] == {
        "repository": "**REDACTED**",
        "repository_id": "**REDACTED**",
        "tag": "v1.1.0",
        "version": "1.1.0",
    }
    text = json.dumps(diagnostics)
    for secret in (REPOSITORY, "ha-gizmo", str(REPOSITORY_ID)):
        assert secret not in text


async def test_a_renamed_repository_keeps_its_updates(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GitHub knows the repository by its id: its new name is stored, once."""
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    aioclient_mock.get(
        f"{API}/repositories/{REPOSITORY_ID}", json={"id": REPOSITORY_ID, "full_name": "octo-team/gizmo"}
    )
    github.releases([release("v1.1.0")])
    with patch.object(
        private_repositories, "async_setup_entry", wraps=private_repositories.async_setup_entry
    ) as setup_entry:
        await setup(hass, entry)
        await hass.async_block_till_done()
    [subentry] = entry.subentries.values()
    assert subentry.data[CONF_REPOSITORY] == "octo-team/gizmo"
    assert subentry.data[CONF_REPOSITORY_ID] == REPOSITORY_ID
    assert subentry.unique_id == str(REPOSITORY_ID)
    assert f"{REPOSITORY} is now octo-team/gizmo on GitHub" in caplog.text
    # Set up again with the new name, and not again after that.
    assert setup_entry.call_count == 2
    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.get("update.gizmo")
    assert state.attributes["latest_version"] == "1.1.0"
    device_id = er.async_get(hass).async_get("update.gizmo").device_id
    device = dr.async_get(hass).async_get(device_id)
    assert device.configuration_url == "https://github.com/octo-team/gizmo"
    assert device.manufacturer == "octo-team"
    # Nothing asks for the old name.
    assert not [call for call in aioclient_mock.mock_calls if "/repos/" in call[1].path]


async def test_a_new_repository_under_an_old_name_is_never_used(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    """The account was renamed, someone took the old login and made a repository
    with the old name: updates still come from the tracked repository only."""
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    other = REPOSITORY_ID + 1
    aioclient_mock.get(f"{API}/repos/{REPOSITORY}", json={"id": other, "full_name": REPOSITORY})
    aioclient_mock.get(f"{API}/repositories/{other}/releases", json=[release("v9.9.9")])
    renamed = {"id": REPOSITORY_ID, "full_name": "octo-renamed/ha-gizmo"}
    aioclient_mock.get(f"{API}/repositories/{REPOSITORY_ID}", json=renamed)
    github.releases([release("v1.1.0")])
    github.hacs_json(None)
    github.archive("v1.1.0", tarball(integration_files(version="1.1.0")))
    await setup(hass, entry)
    await hass.async_block_till_done()
    assert hass.states.get("update.gizmo").attributes["latest_version"] == "1.1.0"
    await hass.services.async_call("update", "install", {"entity_id": "update.gizmo"}, blocking=True)
    assert hass.states.get("update.gizmo").attributes["installed_version"] == "1.1.0"
    paths = {call[1].path for call in aioclient_mock.mock_calls}
    assert f"/repos/{REPOSITORY}" not in paths
    assert not [path for path in paths if path.startswith(f"/repositories/{other}")]


async def test_a_renamed_repository_whose_releases_fail(
    hass: HomeAssistant,
    hass_client,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The new name is stored all the same: diagnostics leave out the name in
    the error, and the next checks use it."""
    assert await async_setup_component(hass, "diagnostics", {})
    github.user()
    renamed = {"id": REPOSITORY_ID, "full_name": "octo-team/secret-gizmo"}
    aioclient_mock.get(f"{API}/repositories/{REPOSITORY_ID}", json=renamed)
    github.releases([], status=404)
    await setup(hass, entry)
    await hass.async_block_till_done()
    [subentry] = entry.subentries.values()
    assert subentry.data[CONF_REPOSITORY] == "octo-team/secret-gizmo"
    # Before the entry reloads with the new name, errors use the stored one.
    assert f"Cannot check {REPOSITORY}: Releases of {REPOSITORY} not found" in caplog.text
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    [repository] = diagnostics["repositories"]
    assert repository["error"] == "Releases of **REDACTED** not found, or the token cannot read it"
    text = json.dumps(diagnostics)
    for name in ("secret-gizmo", REPOSITORY, "ha-gizmo"):
        assert name not in text


@pytest.mark.parametrize("removed", ["repository", "token"])
async def test_a_rename_found_after_its_repository_was_removed(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    removed: str,
) -> None:
    """A check asked for by hand goes on after a reload: what it learnt about
    a repository removed meanwhile is dropped."""
    assert await async_setup_component(hass, "homeassistant", {})
    install_locally(Path(hass.config.config_dir), version="1.0.0")
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    aioclient_mock.clear_requests()
    github.user()
    renamed = {"id": REPOSITORY_ID, "full_name": "octo-team/gizmo"}
    aioclient_mock.get(f"{API}/repositories/{REPOSITORY_ID}", json=renamed)
    asked, answer = asyncio.Event(), asyncio.Event()

    async def releases(method: str, url: Any, body: Any) -> AiohttpClientMockResponse:
        asked.set()
        await answer.wait()
        return AiohttpClientMockResponse(method, url, json=[release("v1.0.0")])

    aioclient_mock.get(github.url("releases"), side_effect=releases)
    check = hass.async_create_task(
        hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": "update.gizmo"}, blocking=True
        )
    )
    await asked.wait()
    if removed == "repository":
        assert hass.config_entries.async_remove_subentry(entry, next(iter(entry.subentries)))
    else:
        await hass.config_entries.async_remove(entry.entry_id)
    answer.set()
    await check
    await hass.async_block_till_done()
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert "is now octo-team/gizmo" not in caplog.text


async def test_a_repository_rejecting_the_token_asks_for_a_new_one(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, freezer: FrozenDateTimeFactory
) -> None:
    """GitHub accepts the token for the account, then rejects it for the
    repository: reauthentication takes over from the renewal reminder."""
    freezer.move_to(datetime(2029, 12, 20, tzinfo=UTC))
    github.user()
    github.releases([], status=401)
    changes = async_capture_events(hass, ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED)
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    issue_id = f"{ISSUE_TOKEN_EXPIRING}_{entry.entry_id}"
    ours = [change.data for change in changes if change.data["domain"] == DOMAIN]
    actions = [(data["action"], data["issue_id"]) for data in ours]
    assert actions == [("create", issue_id), ("remove", issue_id)]
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_a_repository_that_cannot_be_read_keeps_what_was_known(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its last releases are kept, and the same error is logged once."""
    github.user()
    github.releases([release("v1.1.0")])
    await setup(hass, entry)
    coordinator = entry.runtime_data
    [subentry_id] = entry.subentries
    for status in (500, 500, 404):
        aioclient_mock.clear_requests()
        github.user()
        github.releases([], status=status)
        await coordinator.async_refresh()
        state = coordinator.data[subentry_id]
        assert [r.tag for r in state.releases] == ["v1.1.0"]
        assert state.error is not None
        assert hass.states.get("update.gizmo").state == "unavailable"
    assert caplog.text.count(f"Cannot check {REPOSITORY}: GitHub is having trouble (HTTP 500)") == 1
    assert caplog.text.count(f"Cannot check {REPOSITORY}: Releases of {REPOSITORY} not found") == 1
    aioclient_mock.clear_requests()
    github.user()
    github.releases([release("v1.2.0")])
    await coordinator.async_refresh()
    assert coordinator.data[subentry_id].error is None
    assert hass.states.get("update.gizmo").attributes["latest_version"] == "1.2.0"


async def test_setup_survives_an_unreadable_work_folder(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    github.user()
    github.releases([release("v1.0.0")])
    with patch(
        "custom_components.private_repositories.manager.installer.recover",
        side_effect=PermissionError(13, "Permission denied"),
    ):
        await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert "Cannot check for interrupted installs: [Errno 13] Permission denied" in caplog.text


async def test_reloads_leave_one_install_listener(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub
) -> None:
    """Each setup follows installs until its entry unloads: a reload leaves no
    coordinator behind."""
    github.user()
    github.releases([release("v1.0.0")])
    await setup(hass, entry)
    assert len(hass.data[DATA_DISPATCHER][SIGNAL_INSTALL]) == 1
    for _ in range(3):
        assert await hass.config_entries.async_reload(entry.entry_id)
    assert len(hass.data[DATA_DISPATCHER][SIGNAL_INSTALL]) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.data[DATA_DISPATCHER].get(SIGNAL_INSTALL)


async def test_setup_reports_leftovers_it_cannot_remove(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    github.user()
    github.releases([release("v1.0.0")])
    with patch(
        "custom_components.private_repositories.manager.installer.recover",
        return_value=([], ["gizmo.previous.stale-1"]),
    ):
        await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert "Cannot remove what interrupted installs left in" in caplog.text
    assert "gizmo.previous.stale-1" in caplog.text
