"""Adding tokens and repositories."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
import logging
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
    ConfigSubentry,
)
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components import private_repositories
from custom_components.private_repositories.const import (
    CONF_INTEGRATION,
    CONF_LOGIN,
    CONF_PRERELEASES,
    CONF_REMINDER_DAYS,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    CONF_USER_ID,
    DOMAIN,
    ISSUE_RESTART,
    SUBENTRY_REPOSITORY,
)
from custom_components.private_repositories.installer import InstallWriteError, installed_version
from custom_components.private_repositories.manager import Manager

from .conftest import (
    API,
    LOGIN,
    REPOSITORY,
    REPOSITORY_ID,
    TOKEN,
    USER_ID,
    FakeGitHub,
    install_locally,
    integration_files,
    no_room_to_swap,
    release,
    repository_id,
    repository_subentry,
    tarball,
    token_entry,
)

NEW_TOKEN = "github_pat_new"


async def test_add_token(hass: HomeAssistant, github: FakeGitHub) -> None:
    github.user()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCESS_TOKEN: f" {TOKEN} "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == LOGIN
    assert result["data"] == {CONF_ACCESS_TOKEN: TOKEN, CONF_LOGIN: LOGIN, CONF_USER_ID: USER_ID}


@pytest.mark.parametrize(
    ("token", "status", "error"),
    [
        pytest.param(TOKEN, 401, "invalid_auth", id="rejected"),
        pytest.param(TOKEN, 500, "cannot_connect", id="github_down"),
        pytest.param("ghp_classic", 200, "not_fine_grained", id="classic_token"),
    ],
)
async def test_add_token_errors(
    hass: HomeAssistant,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    token: str,
    status: int,
    error: str,
) -> None:
    github.user(status=status)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: token})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    # The user can try again.
    aioclient_mock.clear_requests()
    github.user()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_add_token_unexpected_error(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch(
        "custom_components.private_repositories.account.GitHubClient.get_account", side_effect=RuntimeError
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: TOKEN})
    assert result["errors"] == {"base": "unknown"}


async def test_add_the_same_token_twice(hass: HomeAssistant) -> None:
    token_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.fixture
async def loaded(hass: HomeAssistant, github: FakeGitHub, entry: MockConfigEntry) -> MockConfigEntry:
    """The token entry, set up, with its one repository."""
    github.user()
    github.releases([release("v1.0.0")])
    install_locally(Path(hass.config.config_dir))
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_reauth(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    result = await loaded.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["account"] == LOGIN

    # Another account.
    aioclient_mock.clear_requests()
    github.user(login="someone-else", user_id=7)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: NEW_TOKEN})
    assert result["errors"] == {"base": "wrong_account"}

    # The same account, but not the same repositories.
    aioclient_mock.clear_requests()
    github.user()
    github.repository(status=404)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: NEW_TOKEN})
    assert result["errors"] == {"base": "missing_access"}
    assert result["description_placeholders"]["repositories"] == REPOSITORY

    # The account was renamed meanwhile: its id is what counts.
    aioclient_mock.clear_requests()
    github.user(login="octo-renamed")
    github.repository()
    github.releases([release("v1.0.0")])
    with patch.object(
        private_repositories, "async_setup_entry", wraps=private_repositories.async_setup_entry
    ) as setup_entry:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_TOKEN: NEW_TOKEN}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert loaded.data[CONF_ACCESS_TOKEN] == NEW_TOKEN
    assert loaded.data[CONF_LOGIN] == "octo-renamed"
    assert loaded.state is ConfigEntryState.LOADED
    assert setup_entry.call_count == 1
    assert "update listener" not in caplog.text


async def test_reauth_after_a_failed_setup(
    hass: HomeAssistant, entry: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    github.user(status=401)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    [flow] = hass.config_entries.flow.async_progress()
    aioclient_mock.clear_requests()
    github.user()
    github.repository()
    github.releases([release("v1.0.0")])
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {CONF_ACCESS_TOKEN: NEW_TOKEN})
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure(
    hass: HomeAssistant, loaded: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    result = await loaded.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    aioclient_mock.clear_requests()
    github.user()
    github.repository()
    github.releases([release("v1.0.0")])
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: NEW_TOKEN})
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert loaded.data[CONF_ACCESS_TOKEN] == NEW_TOKEN
    assert loaded.state is ConfigEntryState.LOADED


async def test_options(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_REMINDER_DAYS: 14.0})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded.options == {CONF_REMINDER_DAYS: 14}
    await hass.async_block_till_done()
    assert loaded.state is ConfigEntryState.LOADED


async def _start(hass: HomeAssistant, entry: MockConfigEntry):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_REPOSITORY), context={"source": SOURCE_USER}
    )


@pytest.fixture
async def token(hass: HomeAssistant, github: FakeGitHub) -> MockConfigEntry:
    """A token with no repository yet."""
    entry = token_entry()
    entry.add_to_hass(hass)
    github.user()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _submit(hass: HomeAssistant, flow_id: str, user_input: dict[str, Any] | None = None) -> Any:
    """Send a step, then wait for an install it starts, as the frontend does."""
    result = await hass.config_entries.subentries.async_configure(flow_id, user_input)
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.subentries.async_configure(flow_id)
    return result


def _data(repository: str = REPOSITORY, domain: str = "gizmo", prereleases: bool = False) -> dict[str, Any]:
    """What a subentry stores."""
    return {
        CONF_REPOSITORY: repository,
        CONF_REPOSITORY_ID: repository_id(repository),
        CONF_INTEGRATION: domain,
        CONF_PRERELEASES: prereleases,
    }


async def test_add_repository_installs_it(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    github.repositories([REPOSITORY, "octo/other"])
    result = await _start(hass, token)
    assert result["type"] is FlowResultType.FORM
    options = result["data_schema"].schema[CONF_REPOSITORY].config["options"]
    assert options == [REPOSITORY, "octo/other"]

    github.repository(REPOSITORY.upper(), full_name=REPOSITORY)
    github.releases([release("v1.1.0-beta", prerelease=True), release("v1.0.0")])
    github.hacs_json(None)
    github.archive("v1.0.0", tarball(integration_files(version="1.0.0")))
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_REPOSITORY: f"https://github.com/{REPOSITORY.upper()}"}
    )
    # The dialog shows the install as it runs.
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "install"
    assert result["progress_action"] == "install"
    assert result["description_placeholders"] == {"repository": REPOSITORY}
    await hass.async_block_till_done()
    # Tracked as soon as it is installed, before the dialog hears of it.
    [subentry] = token.subentries.values()
    assert subentry.data == _data()
    assert subentry.title == "Gizmo"
    assert subentry.unique_id == str(REPOSITORY_ID)
    # As Home Assistant stores the data of the subentries it creates.
    assert isinstance(subentry.data, MappingProxyType)

    result = await hass.config_entries.subentries.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "installed"
    assert result["description_placeholders"] == {
        "name": "Gizmo",
        "version": r"1\.0\.0",
        "repository": REPOSITORY,
    }
    await hass.async_block_till_done()
    manifest = Path(hass.config.config_dir) / "custom_components" / "gizmo" / "manifest.json"
    assert manifest.is_file()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RESTART) is not None
    state = hass.states.get("update.gizmo")
    assert state is not None
    assert state.state == "off"
    assert state.attributes["installed_version"] == "1.0.0"


async def test_the_install_step_waits_for_the_install(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    """Asked again before the install ends, the step shows the same progress."""
    _install_files(github)
    download = asyncio.Event()
    aioclient_mock.get(
        github.url("tarball/v1.0.0"), side_effect=_hold(download, response=tarball(integration_files()))
    )
    result = await _start(hass, token)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_REPOSITORY: REPOSITORY}
    )
    again = await hass.config_entries.subentries.async_configure(result["flow_id"])
    assert again["type"] is FlowResultType.SHOW_PROGRESS
    assert again["step_id"] == "install"
    download.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_configure(result["flow_id"])
    assert result["reason"] == "installed"


async def test_add_repository_already_installed(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    install_locally(Path(hass.config.config_dir), version="0.9.0")
    github.repositories([])
    github.repository()
    github.releases([release("v1.0.0")])
    github.hacs_json({"homeassistant": "2999.1.0"})
    github.archive("v1.0.0", tarball(integration_files()))
    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    # Installed by other means: confirm before taking it over.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "adopt"
    assert result["description_placeholders"] == {
        "integration": "gizmo",
        "version": r"0\.9\.0",
        "repository": REPOSITORY,
    }
    assert not token.subentries
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == _data()
    await hass.async_block_till_done()
    assert next(iter(token.subentries.values())).unique_id == str(REPOSITORY_ID)
    # Left as it is, even with a release too new for this Home Assistant: the
    # update entity offers the new version.
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_RESTART) is None
    state = hass.states.get("update.gizmo")
    assert state.state == "on"
    assert state.attributes["installed_version"] == "0.9.0"
    assert state.attributes["latest_version"] == "1.0.0"


async def test_add_repository_taken_while_confirming(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    install_locally(Path(hass.config.config_dir), version="0.9.0")
    github.repositories([])
    github.repository()
    github.releases([release("v1.0.0")])
    github.hacs_json(None)
    github.archive("v1.0.0", tarball(integration_files()))
    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["step_id"] == "adopt"
    # Another dialog tracks the same integration in the meantime.
    github.releases([release("v1.0.0")], name="octo/fork")
    hass.config_entries.async_add_subentry(token, ConfigSubentry(**repository_subentry("octo/fork")))
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "integration_tracked"
    assert result["description_placeholders"] == {"integration": "gizmo"}


@pytest.mark.parametrize("where", ["same_token", "other_token"])
async def test_add_repository_tracked_while_confirming(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub, where: str
) -> None:
    install_locally(Path(hass.config.config_dir), version="0.9.0")
    _install_files(github)
    github.archive("v1.0.0", tarball(integration_files()))
    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["step_id"] == "adopt"
    owner = token
    if where == "other_token":
        owner = token_entry(token="github_pat_other")
        owner.add_to_hass(hass)
    hass.config_entries.async_add_subentry(owner, ConfigSubentry(**repository_subentry()))
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["reason"] == "already_tracked"
    # The token with the new repository reloads.
    await hass.async_block_till_done()


async def test_confirming_for_a_removed_token(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    """Home Assistant leaves the dialog open when its token is removed."""
    install_locally(Path(hass.config.config_dir), version="0.9.0")
    _install_files(github)
    github.archive("v1.0.0", tarball(integration_files()))
    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["step_id"] == "adopt"
    await hass.config_entries.async_remove(token.entry_id)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_removed"


def _no_repository(gh: FakeGitHub) -> None:
    """Nothing to register."""


def _release_with(
    files: dict[str, bytes] | None = None, hacs: dict | None = None
) -> Callable[[FakeGitHub], None]:
    def register(gh: FakeGitHub) -> None:
        gh.repository()
        gh.releases([release("v1.0.0")])
        gh.hacs_json(hacs)
        gh.archive("v1.0.0", tarball(files if files is not None else integration_files()))

    return register


@pytest.mark.parametrize(
    ("setup", "value", "errors", "placeholders"),
    [
        pytest.param(
            _no_repository, "not a repository", {CONF_REPOSITORY: "invalid_repository"}, {}, id="invalid"
        ),
        pytest.param(
            lambda gh: gh.repository(status=404),
            REPOSITORY,
            {CONF_REPOSITORY: "not_found"},
            {},
            id="not_found",
        ),
        pytest.param(
            lambda gh: gh.repository(status=401), REPOSITORY, {"base": "invalid_auth"}, {}, id="rejected"
        ),
        pytest.param(
            lambda gh: gh.repository(status=500),
            REPOSITORY,
            {"base": "cannot_connect"},
            {"error": "GitHub is having trouble (HTTP 500)"},
            id="down",
        ),
        pytest.param(
            lambda gh: (gh.repository(), gh.releases([])),
            REPOSITORY,
            {"base": "no_release"},
            {},
            id="no_release",
        ),
        pytest.param(
            lambda gh: (gh.repository(), gh.releases([release("v2.0.0", prerelease=True)])),
            REPOSITORY,
            {"base": "only_prereleases"},
            {},
            id="only_prereleases",
        ),
        pytest.param(
            _release_with({"README.md": b""}),
            REPOSITORY,
            {"base": "invalid_release"},
            {"error": "No integration found in custom_components/"},
            id="no_integration",
        ),
        pytest.param(
            _release_with(hacs={"homeassistant": "2999.1.0"}),
            REPOSITORY,
            {"base": "needs_newer_home_assistant"},
            {"minimum": "2999.1.0"},
            id="needs_newer_home_assistant",
        ),
        pytest.param(
            _release_with(integration_files(domain="http")),
            REPOSITORY,
            {"base": "integration_refused"},
            {"integration": "http"},
            id="built_in_integration",
        ),
        pytest.param(
            _release_with(integration_files(domain=DOMAIN)),
            REPOSITORY,
            {"base": "integration_refused"},
            {"integration": DOMAIN},
            id="this_integration",
        ),
    ],
)
async def test_add_repository_errors(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    setup: Callable[[FakeGitHub], None],
    value: str,
    errors: dict,
    placeholders: dict,
) -> None:
    github.repositories([])
    setup(github)
    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: value})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == errors
    assert placeholders.items() <= result["description_placeholders"].items()
    # The form keeps what the user sent.
    [field] = [key for key in result["data_schema"].schema if key == CONF_REPOSITORY]
    assert field.description == {"suggested_value": value}
    installed = Path(hass.config.config_dir) / "custom_components"
    assert {path.name for path in installed.glob("*")} <= {".private_repositories"}
    assert not token.subentries


@pytest.mark.parametrize(
    ("error", "expected", "placeholders"),
    [
        (InstallWriteError("Cannot write"), "install_failed", {"error": "Cannot write"}),
        (None, "no_space", {}),
        (RuntimeError("boom"), "unknown", {}),
    ],
)
async def test_add_repository_disk_trouble(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    caplog: pytest.LogCaptureFixture,
    error: Exception | None,
    expected: str,
    placeholders: dict[str, str],
) -> None:
    github.repositories([])
    _release_with()(github)
    result = await _start(hass, token)
    with (
        no_room_to_swap()
        if error is None
        else patch("custom_components.private_repositories.manager.installer.install", side_effect=error)
    ):
        result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["errors"] == {"base": expected}
    assert placeholders.items() <= result["description_placeholders"].items()
    # A bug is logged with its traceback, an expected failure is not logged.
    assert [r.exc_info[1] for r in caplog.records if r.exc_info] == ([error] if expected == "unknown" else [])
    # The user can try again.
    github.archive("v1.0.0", tarball(integration_files()))
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["reason"] == "installed"


async def test_add_repository_with_prereleases(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    github.repositories([])
    github.repository()
    github.releases([release("v2.0.0b1", prerelease=True), release("v1.0.0")])
    github.hacs_json(None)
    github.archive("v2.0.0b1", tarball(integration_files(version="2.0.0b1")))
    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY, CONF_PRERELEASES: True})
    assert result["reason"] == "installed"
    assert next(iter(token.subentries.values())).data[CONF_PRERELEASES] is True
    await hass.async_block_till_done()
    assert hass.states.get("update.gizmo").attributes["installed_version"] == "2.0.0b1"


@pytest.mark.parametrize("typed", [REPOSITORY, "octo/old-name"])
async def test_add_a_tracked_repository(
    hass: HomeAssistant, loaded: MockConfigEntry, github: FakeGitHub, typed: str
) -> None:
    """Known by its id: also under a name it had before."""
    github.repositories([REPOSITORY, "octo/other"])
    github.repository(typed, full_name=REPOSITORY)
    result = await _start(hass, loaded)
    # Already tracked repositories are not offered.
    assert result["data_schema"].schema[CONF_REPOSITORY].config["options"] == ["octo/other"]
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: typed})
    assert result["errors"] == {"base": "already_tracked"}


async def test_add_an_integration_tracked_elsewhere(
    hass: HomeAssistant, loaded: MockConfigEntry, github: FakeGitHub
) -> None:
    github.repositories([])
    github.repository("octo/fork")
    github.releases([release("v1.0.0")], name="octo/fork")
    github.hacs_json(None, name="octo/fork")
    github.archive("v1.0.0", tarball(integration_files()), name="octo/fork")
    result = await _start(hass, loaded)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: "octo/fork"})
    assert result["errors"] == {"base": "integration_tracked"}
    assert result["description_placeholders"]["integration"] == "gizmo"


async def test_add_repository_needs_a_working_token(hass: HomeAssistant, github: FakeGitHub) -> None:
    entry = token_entry()
    entry.add_to_hass(hass)
    github.user(status=401)
    await hass.config_entries.async_setup(entry.entry_id)
    result = await _start(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"


async def test_repository_list_unavailable(
    hass: HomeAssistant, token: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/user/repos", status=500)
    result = await _start(hass, token)
    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"].schema[CONF_REPOSITORY].config["options"] == []


async def test_reconfigure_repository(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    subentry_id = next(iter(loaded.subentries))
    result = await hass.config_entries.subentries.async_init(
        (loaded.entry_id, SUBENTRY_REPOSITORY),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    assert result["description_placeholders"] == {"repository": REPOSITORY}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {CONF_PRERELEASES: True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert loaded.subentries[subentry_id].data[CONF_PRERELEASES] is True
    assert loaded.state is ConfigEntryState.LOADED


def _calls(aioclient_mock: AiohttpClientMocker, path: str) -> int:
    return sum(1 for call in aioclient_mock.mock_calls if call[1].path == path)


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


def _hold(event: asyncio.Event, **answer: Any):
    """An answer GitHub only sends once event is set."""

    async def side_effect(method: str, url: Any, body: Any) -> AiohttpClientMockResponse:
        await event.wait()
        return AiohttpClientMockResponse(method, url, **answer)

    return side_effect


def _install_files(github: FakeGitHub) -> None:
    github.repositories([])
    github.repository()
    github.releases([release("v1.0.0")])
    github.hacs_json(None)


async def _start_install(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    **answer: Any,
) -> tuple[str, asyncio.Event]:
    """A dialog whose install waits for its download: set the event to send it."""
    _install_files(github)
    download = asyncio.Event()
    aioclient_mock.get(
        github.url("tarball/v1.0.0"),
        side_effect=_hold(download, **(answer or {"response": tarball(integration_files())})),
    )
    result = await _start(hass, token)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_REPOSITORY: REPOSITORY}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    return result["flow_id"], download


def _go_away(hass: HomeAssistant, flow_id: str, how: str) -> None:
    """The dialog is closed, or its connection drops: it never asks for the next step."""
    if how == "closed":
        hass.config_entries.subentries.async_abort(flow_id)


@pytest.mark.parametrize("how", ["closed", "disconnected"])
async def test_closing_the_dialog_during_the_install_still_tracks_it(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    how: str,
) -> None:
    """The install goes on: what it installed gets updates like any other."""
    flow_id, download = await _start_install(hass, token, github, aioclient_mock)
    _go_away(hass, flow_id, how)
    download.set()
    await hass.async_block_till_done()
    [subentry] = token.subentries.values()
    assert subentry.data == _data()
    assert isinstance(subentry.data, MappingProxyType)
    assert subentry.title == "Gizmo"
    assert hass.states.get("update.gizmo").attributes["installed_version"] == "1.0.0"


async def test_closing_the_dialog_as_the_install_ends_still_tracks_it(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    """The dialog goes away after the install, before the dialog's wait hears of it."""
    _install_files(github)
    github.archive("v1.0.0", tarball(integration_files()))
    result = await _start(hass, token)
    flow_id = result["flow_id"]
    real_install = Manager.async_install

    async def install(self: Manager, *args: Any, **kwargs: Any) -> Any:
        done = await real_install(self, *args, **kwargs)
        hass.config_entries.subentries.async_abort(flow_id)
        return done

    with patch.object(Manager, "async_install", install):
        await hass.config_entries.subentries.async_configure(flow_id, {CONF_REPOSITORY: REPOSITORY})
        await hass.async_block_till_done()
    [subentry] = token.subentries.values()
    assert subentry.unique_id == str(REPOSITORY_ID)
    assert not hass.config_entries.subentries.async_progress()


@pytest.mark.parametrize("how", ["closed", "disconnected"])
@pytest.mark.parametrize("releases", [{"json": [release()]}, {"status": 500}], ids=["releases", "error"])
async def test_closing_the_dialog_during_the_checks_installs_nothing(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    how: str,
    releases: dict[str, Any],
) -> None:
    answer = asyncio.Event()
    aioclient_mock.get(github.url("releases"), side_effect=_hold(answer, **releases))
    _install_files(github)
    github.archive("v1.0.0", tarball(integration_files()))
    result = await _start(hass, token)
    call = hass.async_create_task(
        hass.config_entries.subentries.async_configure(result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    )
    await asyncio.sleep(0.01)
    if how == "closed":
        hass.config_entries.subentries.async_abort(result["flow_id"])
    else:
        # Home Assistant cancels the request of a dropped connection.
        call.cancel()
    answer.set()
    with suppress(asyncio.CancelledError):
        result = await call
    await hass.async_block_till_done()
    assert not token.subentries
    assert installed_version(Path(hass.config.config_dir), "gizmo") is None
    # Nobody is left to see the list of repositories.
    assert _calls(aioclient_mock, "/user/repos") == 1
    if how == "closed":
        # Home Assistant only accepts an abort from a flow that is gone.
        assert result["reason"] == "closed"


@pytest.mark.parametrize("how", ["closed", "disconnected"])
async def test_closing_the_dialog_never_takes_over_an_integration(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    how: str,
) -> None:
    """Taking over an integration that is already there needs the user's go."""
    install_locally(Path(hass.config.config_dir), version="0.9.0")
    flow_id, download = await _start_install(hass, token, github, aioclient_mock)
    _go_away(hass, flow_id, how)
    download.set()
    await hass.async_block_till_done()
    assert not token.subentries
    assert installed_version(Path(hass.config.config_dir), "gizmo") == "0.9.0"
    # A closed dialog cannot say why nothing changed: the log does.
    logged = f"Adding {REPOSITORY} changed nothing: gizmo was already installed"
    assert (logged in caplog.text) == (how == "closed")
    if how == "disconnected":
        # Back again, the dialog asks to take the integration over.
        result = await hass.config_entries.subentries.async_configure(flow_id)
        assert result["step_id"] == "adopt"


async def test_a_failure_after_the_dialog_was_closed_is_logged(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    flow_id, download = await _start_install(hass, token, github, aioclient_mock, status=500)
    hass.config_entries.subentries.async_abort(flow_id)
    download.set()
    await hass.async_block_till_done()
    assert not token.subentries
    assert caplog.text.count(f"Adding {REPOSITORY} failed after its dialog was closed") == 1
    # Logged once, by this integration, and in one line: a failed download is no bug.
    assert "shielded future" not in caplog.text
    assert not _errors(caplog)
    assert _calls(aioclient_mock, "/user/repos") == 1


async def test_a_failure_with_the_dialog_open_is_shown_not_logged(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    flow_id, download = await _start_install(hass, token, github, aioclient_mock, status=500)
    download.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_configure(flow_id)
    # GitHub failing during the download, as during the checks: the release is fine.
    assert result["errors"] == {"base": "cannot_connect"}
    assert result["description_placeholders"]["error"] == "GitHub is having trouble (HTTP 500)"
    assert "failed" not in caplog.text


async def test_an_install_stopped_with_the_dialog_open_is_shown(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    """Home Assistant stopped the install itself: the dialog says so."""
    _install_files(github)
    github.archive("v1.0.0", tarball(integration_files()))
    with patch.object(Manager, "_async_download_and_install", side_effect=asyncio.CancelledError):
        result = await _submit(hass, (await _start(hass, token))["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["errors"] == {"base": "restarting"}
    assert not token.subentries


async def test_a_stopped_install_after_the_dialog_was_closed_is_quiet(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    """Home Assistant stops: an install nobody waits for ends without a word."""
    _install_files(github)
    go_on = asyncio.Event()

    async def install(*args: Any, **kwargs: Any) -> Any:
        await go_on.wait()
        raise asyncio.CancelledError

    with patch.object(Manager, "async_install", install):
        result = await _start(hass, token)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_REPOSITORY: REPOSITORY}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        hass.config_entries.subentries.async_abort(result["flow_id"])
        go_on.set()
        await hass.async_block_till_done()
    assert not token.subentries
    assert "failed" not in caplog.text
    assert not _errors(caplog)


@pytest.mark.parametrize("how", ["closed", "disconnected"])
@pytest.mark.parametrize("where", ["same_token", "other_token"])
async def test_a_dialog_that_went_away_never_tracks_a_repository_twice(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    how: str,
    where: str,
) -> None:
    """The repository was tracked some other way while the install ran."""
    flow_id, download = await _start_install(hass, token, github, aioclient_mock)
    owner = token
    if where == "other_token":
        owner = token_entry(token="github_pat_other")
        owner.add_to_hass(hass)
    hass.config_entries.async_add_subentry(owner, ConfigSubentry(**repository_subentry()))
    _go_away(hass, flow_id, how)
    download.set()
    await hass.async_block_till_done()
    tracked = [s.unique_id for e in hass.config_entries.async_entries(DOMAIN) for s in e.subentries.values()]
    assert tracked == [str(REPOSITORY_ID)]
    assert not _errors(caplog)
    if how == "disconnected":
        result = await hass.config_entries.subentries.async_configure(flow_id)
        assert result["errors"] == {"base": "already_tracked"}


@pytest.mark.parametrize("how", ["closed", "disconnected"])
async def test_a_dialog_that_went_away_tracks_nothing_for_a_removed_token(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    how: str,
) -> None:
    flow_id, download = await _start_install(hass, token, github, aioclient_mock)
    _go_away(hass, flow_id, how)
    await hass.config_entries.async_remove(token.entry_id)
    download.set()
    await hass.async_block_till_done()
    assert not hass.config_entries.async_entries(DOMAIN)
    assert not _errors(caplog)
    if how == "disconnected":
        # Home Assistant leaves the dialog open when its token is removed.
        result = await hass.config_entries.subentries.async_configure(flow_id)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "entry_removed"


async def test_a_closed_dialog_installing_an_integration_tracked_meanwhile(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Another repository tracked the same integration during the download:
    the closed dialog tracks nothing, and says so in the log."""
    flow_id, download = await _start_install(hass, token, github, aioclient_mock)
    github.releases([release("v1.0.0")], name="octo/fork")
    hass.config_entries.async_add_subentry(token, ConfigSubentry(**repository_subentry("octo/fork")))
    hass.config_entries.subentries.async_abort(flow_id)
    download.set()
    await hass.async_block_till_done()
    assert [s.data[CONF_REPOSITORY] for s in token.subentries.values()] == ["octo/fork"]
    assert f"Adding {REPOSITORY} installed gizmo, which another repository tracked meanwhile" in caplog.text


async def test_a_dialog_whose_wait_is_stopped_shows_why(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    """Home Assistant stops and cancels the dialog's wait, but not the dialog:
    the dialog still ends in a step."""
    flow_id, download = await _start_install(hass, token, github, aioclient_mock)
    flow = hass.config_entries.subentries._progress[flow_id]
    flow._progress.cancel()
    with suppress(asyncio.CancelledError):
        await flow._progress
    # Home Assistant moves the dialog on once its wait ends.
    for _ in range(10):
        await asyncio.sleep(0)
    assert flow.cur_step["type"] is FlowResultType.SHOW_PROGRESS_DONE
    result = await hass.config_entries.subentries.async_configure(flow_id)
    assert result["errors"] == {"base": "restarting"}
    # The install itself goes on, and tracks its repository.
    download.set()
    await hass.async_block_till_done()
    assert [s.unique_id for s in token.subentries.values()] == [str(REPOSITORY_ID)]


async def test_a_bug_after_the_dialog_was_closed_keeps_its_traceback(
    hass: HomeAssistant,
    token: MockConfigEntry,
    github: FakeGitHub,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bug = RuntimeError("boom")
    with patch.object(Manager, "_unpack_and_install", side_effect=bug):
        flow_id, download = await _start_install(hass, token, github, aioclient_mock)
        hass.config_entries.subentries.async_abort(flow_id)
        download.set()
        await hass.async_block_till_done()
    [record] = [r for r in caplog.records if "failed after its dialog was closed" in r.getMessage()]
    assert record.levelno == logging.ERROR
    assert record.exc_info[1] is bug
    assert not token.subentries


async def test_two_dialogs_adding_the_same_repository(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    """Installs run one at a time: the second dialog finds the repository tracked."""
    flow_id, download = await _start_install(hass, token, github, aioclient_mock)
    second = await _start(hass, token)
    second = await hass.config_entries.subentries.async_configure(
        second["flow_id"], {CONF_REPOSITORY: REPOSITORY}
    )
    assert second["type"] is FlowResultType.SHOW_PROGRESS
    download.set()
    await hass.async_block_till_done()
    results = [
        await hass.config_entries.subentries.async_configure(flow_id),
        await hass.config_entries.subentries.async_configure(second["flow_id"]),
    ]
    assert results[0]["reason"] == "installed"
    assert results[1]["errors"] == {"base": "already_tracked"}
    assert [subentry.unique_id for subentry in token.subentries.values()] == [str(REPOSITORY_ID)]


async def test_add_repository_during_a_reload(
    hass: HomeAssistant, loaded: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    """The entry reloads (another dialog added a repository) while this one waits."""
    other = "octo/ha-other"
    answer = asyncio.Event()
    account = asyncio.Event()
    aioclient_mock.clear_requests()
    github.repositories([])
    github.repository(other)
    aioclient_mock.get(github.url("releases", other), side_effect=_hold(answer, json=[]))
    result = await _start(hass, loaded)
    call = hass.async_create_task(
        hass.config_entries.subentries.async_configure(result["flow_id"], {CONF_REPOSITORY: other})
    )
    await asyncio.sleep(0.01)
    aioclient_mock.get(f"{API}/user", side_effect=_hold(account, json={"login": LOGIN, "id": USER_ID}))
    github.releases([release("v1.0.0")])
    reload = hass.async_create_task(hass.config_entries.async_reload(loaded.entry_id))
    await asyncio.sleep(0.01)
    assert loaded.state is ConfigEntryState.SETUP_IN_PROGRESS
    answer.set()
    result = await call
    assert result["errors"] == {"base": "no_release"}
    account.set()
    await reload
    assert loaded.state is ConfigEntryState.LOADED


async def test_add_repository_again_after_removing_it(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    """Its files are still those of its release: no take-over step, and the
    version still goes by the release's tag."""
    github.repositories([])
    github.repository()
    github.releases([release("release-7")])
    github.hacs_json(None)
    github.archive("release-7", tarball(integration_files(version="1.0.0")))
    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["reason"] == "installed"
    await hass.async_block_till_done()
    assert hass.states.get("update.gizmo").state == "off"
    assert hass.config_entries.async_remove_subentry(token, next(iter(token.subentries)))
    await hass.async_block_till_done()

    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "tracked"
    assert result["description_placeholders"] == {
        "name": "Gizmo",
        "version": r"1\.0\.0",
        "repository": REPOSITORY,
    }
    await hass.async_block_till_done()
    state = hass.states.get("update.gizmo")
    assert state.state == "off"
    assert state.attributes["installed_version"] == "release-7"


async def test_a_fork_of_a_removed_repository_needs_a_confirmation(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    """The files are those of a release of the removed repository: a fork with
    the same integration and version takes them over only once confirmed."""
    _install_files(github)
    github.archive("v1.0.0", tarball(integration_files()))
    result = await _submit(hass, (await _start(hass, token))["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["reason"] == "installed"
    await hass.async_block_till_done()
    assert hass.config_entries.async_remove_subentry(token, next(iter(token.subentries)))
    await hass.async_block_till_done()

    fork = "octo-fork/ha-gizmo"
    github.repository(fork)
    github.releases([release("v1.0.0")], name=fork)
    github.hacs_json(None, name=fork)
    github.archive("v1.0.0", tarball(integration_files()), name=fork)
    result = await _submit(hass, (await _start(hass, token))["flow_id"], {CONF_REPOSITORY: fork})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "adopt"
    assert not token.subentries


async def test_add_repository_again_after_a_new_release(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    """Nothing is installed then: the dialog names the version on disk, not
    the newer release."""
    github.repositories([])
    github.releases([release("v1.0.0")])
    github.hacs_json(None)
    github.archive("v1.0.0", tarball(integration_files(version="1.0.0")))
    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["reason"] == "installed"
    await hass.async_block_till_done()
    assert hass.config_entries.async_remove_subentry(token, next(iter(token.subentries)))
    await hass.async_block_till_done()

    aioclient_mock.clear_requests()
    github.user()
    github.repositories([])
    github.releases([release("v1.1.0"), release("v1.0.0")])
    github.hacs_json(None)
    github.archive("v1.1.0", tarball(integration_files(version="1.1.0")))
    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["reason"] == "tracked"
    assert result["description_placeholders"]["version"] == r"1\.0\.0"
    await hass.async_block_till_done()
    state = hass.states.get("update.gizmo")
    assert state.attributes["installed_version"] == "1.0.0"
    assert state.attributes["latest_version"] == "1.1.0"


async def test_names_from_a_release_are_shown_as_text(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    """The last message is Markdown: a name from the release makes no link or image."""
    name = "Gizmo ![](https://203.0.113.9/p.png) [Sign in](https://example.com)\n# www.example.com"
    _install_files(github)
    github.archive("v1.0.0", tarball(integration_files(name=name)))
    result = await _submit(hass, (await _start(hass, token))["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["reason"] == "installed"
    assert result["description_placeholders"]["name"] == (
        r"Gizmo \!\[\]\(https\://203\.0\.113\.9/p\.png\) \[Sign in\]\(https\://example\.com\) "
        r"\# www\.example\.com"
    )


async def test_add_repository_again_after_its_files_changed(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    """Another version replaced the integration after the repository was
    removed: taking it over again needs the user's go."""
    _install_files(github)
    github.archive("v1.0.0", tarball(integration_files()))
    result = await _start(hass, token)
    await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    await hass.async_block_till_done()
    assert hass.config_entries.async_remove_subentry(token, next(iter(token.subentries)))
    await hass.async_block_till_done()
    install_locally(Path(hass.config.config_dir), version="0.9.0")

    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["step_id"] == "adopt"


@pytest.mark.parametrize(
    "files",
    [
        pytest.param({"manifest.json": b'{"domain": "gizmo",}', "my_changes.py": b""}, id="broken_manifest"),
        pytest.param({"manifest.json": b'{"domain": "gizmo"}'}, id="no_version"),
        pytest.param({"notes.txt": b"mine"}, id="no_manifest"),
    ],
)
async def test_add_repository_over_an_unknown_folder(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub, files: dict[str, bytes]
) -> None:
    """Files that are not a readable integration are never replaced without the user."""
    folder = Path(hass.config.config_dir) / "custom_components" / "gizmo"
    folder.mkdir(parents=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    github.repositories([])
    _release_with()(github)
    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["errors"] == {"base": "integration_unreadable"}
    assert result["description_placeholders"]["integration"] == "gizmo"
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == files


async def test_add_repository_over_compiled_files_only(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    """What a removed integration leaves behind is no reason to stop."""
    (Path(hass.config.config_dir) / "custom_components" / "gizmo" / "__pycache__").mkdir(parents=True)
    github.repositories([])
    _release_with()(github)
    result = await _start(hass, token)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: REPOSITORY})
    assert result["reason"] == "installed"
    await hass.async_block_till_done()


async def test_rejected_token_while_adding_asks_for_a_new_one(
    hass: HomeAssistant, token: MockConfigEntry, github: FakeGitHub
) -> None:
    github.repositories([])
    github.repository(status=401)
    result = await _start(hass, token)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_REPOSITORY: REPOSITORY}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_reauth_with_the_same_token_starts_again(
    hass: HomeAssistant, loaded: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    """GitHub rejected the token for a while: pasting it again restarts the checks."""
    aioclient_mock.clear_requests()
    github.user(status=401)
    await loaded.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("update.gizmo").state == "unavailable"
    [flow] = hass.config_entries.flow.async_progress()
    aioclient_mock.clear_requests()
    github.user()
    github.repository()
    github.releases([release("v1.0.0")])
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {CONF_ACCESS_TOKEN: TOKEN})
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert hass.states.get("update.gizmo").state == "off"


async def test_wrong_account_names_the_expected_login(
    hass: HomeAssistant, loaded: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    hass.config_entries.async_update_entry(loaded, title="Work")
    await hass.async_block_till_done()
    result = await loaded.start_reauth_flow(hass)
    assert result["description_placeholders"]["account"] == "Work"
    aioclient_mock.clear_requests()
    github.user(login="someone-else", user_id=7)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_TOKEN: NEW_TOKEN})
    assert result["errors"] == {"base": "wrong_account"}
    assert result["description_placeholders"]["account"] == LOGIN


async def test_repository_added_during_the_first_refresh(
    hass: HomeAssistant, loaded: MockConfigEntry, github: FakeGitHub, aioclient_mock: AiohttpClientMocker
) -> None:
    """Confirmed while the entry starts: the entry starts again with it."""
    other = "octo/ha-other"
    install_locally(Path(hass.config.config_dir), domain="other", version="2.0.0")
    aioclient_mock.clear_requests()
    github.repositories([])
    github.repository(other)
    github.releases([release("v2.0.0")], name=other)
    github.hacs_json(None, name=other)
    github.archive("v2.0.0", tarball(integration_files("other", "2.0.0", "Other")), name=other)
    result = await _start(hass, loaded)
    result = await _submit(hass, result["flow_id"], {CONF_REPOSITORY: other})
    assert result["step_id"] == "adopt"

    account = asyncio.Event()
    aioclient_mock.get(f"{API}/user", side_effect=_hold(account, json={"login": LOGIN, "id": USER_ID}))
    github.releases([release("v1.0.0")])
    reload = hass.async_create_task(hass.config_entries.async_reload(loaded.entry_id))
    await asyncio.sleep(0.01)
    assert loaded.state is ConfigEntryState.SETUP_IN_PROGRESS
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    account.set()
    await reload
    await hass.async_block_till_done()
    state = hass.states.get("update.other")
    assert state.state == "off"
    assert state.attributes["latest_version"] == "2.0.0"
