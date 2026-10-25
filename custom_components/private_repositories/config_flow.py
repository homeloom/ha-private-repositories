"""Adding a GitHub token, then the repositories to track with it."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from functools import partial
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
)
import voluptuous as vol

from .account import TOKEN_PLACEHOLDERS, TOKEN_SCHEMA, async_check_token, async_replace_token
from .const import (
    CONF_INTEGRATION,
    CONF_LOGIN,
    CONF_PRERELEASES,
    CONF_REMINDER_DAYS,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    CONF_USER_ID,
    DEFAULT_REMINDER_DAYS,
    DOMAIN,
    LOGGER,
    MAX_REMINDER_DAYS,
    MIN_REMINDER_DAYS,
    SUBENTRY_REPOSITORY,
)
from .coordinator import PrivateRepositoriesConfigEntry, ReleaseCoordinator, repositories_of
from .github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
    Release,
    Repository,
    parse_repository,
)
from .installer import InstallError, InstallWriteError
from .manager import (
    InstallMode,
    InstallResult,
    RestartingError,
    finish,
    log_late_failure,
    markdown_text,
)
from .versions import offered


def _repository_data(repository: Repository, domain: str, prereleases: bool) -> dict[str, Any]:
    return {
        CONF_REPOSITORY: repository.name,
        CONF_REPOSITORY_ID: repository.id,
        CONF_INTEGRATION: domain,
        CONF_PRERELEASES: prereleases,
    }


def _explain(err: InstallError | GitHubError, errors: dict[str, str], placeholders: dict[str, str]) -> None:
    """Why adding a repository installed nothing, for the dialog."""
    if err.reason is not None:
        errors["base"] = err.reason
        placeholders.update(err.placeholders)
        return
    if isinstance(err, GitHubError):
        # GitHub failing during the download, as during the checks.
        errors["base"] = "cannot_connect"
    else:
        errors["base"] = "install_failed" if isinstance(err, InstallWriteError) else "invalid_release"
    placeholders["error"] = str(err)


def _tracked(hass: HomeAssistant) -> tuple[set[int], set[str], set[str]]:
    """Repositories (their ids, and their names in lowercase) and integrations
    already tracked, with any token."""
    ids: set[int] = set()
    names: set[str] = set()
    domains: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        for subentry in repositories_of(entry):
            ids.add(subentry.data[CONF_REPOSITORY_ID])
            names.add(subentry.data[CONF_REPOSITORY].lower())
            domains.add(subentry.data[CONF_INTEGRATION])
    return ids, names, domains


class PrivateRepositoriesConfigFlow(ConfigFlow, domain=DOMAIN):
    """A GitHub token."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Repositories are added to a token."""
        return {SUBENTRY_REPOSITORY: RepositoryFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """How early to warn about the end of the token."""
        return PrivateRepositoriesOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Paste a token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_ACCESS_TOKEN].strip()
            if any(entry.data.get(CONF_ACCESS_TOKEN) == token for entry in self._async_current_entries()):
                return self.async_abort(reason="already_configured")
            account, error, _ = await async_check_token(self.hass, token)
            if account is not None:
                return self.async_create_entry(
                    title=account.login,
                    data={CONF_ACCESS_TOKEN: token, CONF_LOGIN: account.login, CONF_USER_ID: account.id},
                )
            errors["base"] = str(error)
        return self.async_show_form(
            step_id="user",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders=TOKEN_PLACEHOLDERS,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """GitHub no longer accepts the token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Paste a new token."""
        return await self._async_replace_token("reauth_confirm", self._get_reauth_entry(), user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Replace the token, for example before it expires."""
        return await self._async_replace_token("reconfigure", self._get_reconfigure_entry(), user_input)

    async def _async_replace_token(
        self, step_id: str, entry: ConfigEntry, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {**TOKEN_PLACEHOLDERS, "account": entry.title, "repositories": ""}
        if user_input is not None:
            token = user_input[CONF_ACCESS_TOKEN].strip()
            account, error, details = await async_check_token(self.hass, token, entry)
            if account is not None:
                async_replace_token(self.hass, entry, token, account)
                return self.async_abort(
                    reason="reauth_successful" if self.source == SOURCE_REAUTH else "reconfigure_successful"
                )
            errors["base"] = str(error)
            placeholders.update(details)
        return self.async_show_form(
            step_id=step_id, data_schema=TOKEN_SCHEMA, errors=errors, description_placeholders=placeholders
        )


class PrivateRepositoriesOptionsFlow(OptionsFlow):
    """How early to warn about the end of the token."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The options."""
        if user_input is not None:
            return self.async_create_entry(data={CONF_REMINDER_DAYS: int(user_input[CONF_REMINDER_DAYS])})
        schema = vol.Schema(
            {
                vol.Required(CONF_REMINDER_DAYS, default=DEFAULT_REMINDER_DAYS): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_REMINDER_DAYS,
                        max=MAX_REMINDER_DAYS,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="d",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options)
        )


class RepositoryFlow(ConfigSubentryFlow):
    """A repository whose releases hold an integration."""

    _client: GitHubClient
    # What the user sent last, shown again with an error.
    _input: dict[str, Any] | None = None
    _repository: Repository
    _prereleases: bool
    # Waits for the install, and tracks what it installed (Home Assistant
    # cancels this wait, not the install, when the dialog is closed).
    _progress: asyncio.Task[None]
    _result: InstallResult
    # Why the install failed (a bug too), or why what it installed is not tracked.
    _error: Exception | None = None
    _refused: str | None = None
    _closed = False

    @callback
    def async_remove(self) -> None:
        """The dialog was closed: an install it started still ends with its
        repository tracked."""
        self._closed = True

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Pick a repository: its integration is installed if it is not there yet."""
        entry: PrivateRepositoriesConfigEntry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        # Read once: a reload while this step waits unsets entry.runtime_data.
        coordinator = entry.runtime_data
        self._client = coordinator.client
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"error": "", "integration": ""}
        if user_input is not None:
            self._input = user_input
            name = parse_repository(str(user_input[CONF_REPOSITORY]))
            if name is None:
                errors[CONF_REPOSITORY] = "invalid_repository"
            else:
                prereleases = bool(user_input.get(CONF_PRERELEASES, False))
                release = await self._async_check(entry, name, prereleases, errors, placeholders)
                if self._closed:
                    # Closed during the checks: nothing is installed. HA only
                    # accepts an abort from a flow that is gone.
                    return self.async_abort(reason="closed")
                if release is not None:
                    return self._async_start(entry, coordinator, release)
        return await self._async_show_picker(user_input, errors, placeholders)

    async def _async_show_picker(
        self, user_input: dict[str, Any] | None, errors: dict[str, str], placeholders: dict[str, str]
    ) -> SubentryFlowResult:
        _, tracked, _ = _tracked(self.hass)
        try:
            available = await self._client.list_repositories()
        except GitHubError:
            available = []
        schema = vol.Schema(
            {
                vol.Required(CONF_REPOSITORY): SelectSelector(
                    SelectSelectorConfig(
                        options=[name for name in available if name.lower() not in tracked],
                        custom_value=True,
                        sort=True,
                    )
                ),
                vol.Optional(CONF_PRERELEASES, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_check(
        self,
        entry: PrivateRepositoriesConfigEntry,
        name: str,
        prereleases: bool,
        errors: dict[str, str],
        placeholders: dict[str, str],
    ) -> Release | None:
        """The release to install, once the repository is found and not tracked yet."""
        try:
            # GitHub finds a renamed repository by its old name too.
            repository = await self._client.get_repository(name)
            if repository.id in _tracked(self.hass)[0]:
                errors["base"] = "already_tracked"
                return None
            published = await self._client.list_releases(repository)
        except GitHubAuthError:
            errors["base"] = "invalid_auth"
            # The token was good when the entry was set up: ask for a new one now.
            entry.async_start_reauth(self.hass)
            return None
        except GitHubNotFoundError:
            errors[CONF_REPOSITORY] = "not_found"
            return None
        except GitHubError as err:
            errors["base"] = "cannot_connect"
            placeholders["error"] = str(err)
            return None
        if not (releases := offered(published, prereleases)):
            errors["base"] = "only_prereleases" if published else "no_release"
            return None
        self._repository, self._prereleases = repository, prereleases
        return releases[0]

    @callback
    def _async_start(
        self, entry: PrivateRepositoriesConfigEntry, coordinator: ReleaseCoordinator, release: Release
    ) -> SubentryFlowResult:
        """Install the release, and show its progress."""
        install = self.hass.async_create_task(
            coordinator.manager.async_install(
                self._client,
                self._repository,
                release,
                domain=None,
                mode=InstallMode.IF_MISSING,
                refused_domains=_tracked(self.hass)[2],
            ),
            f"{DOMAIN} add {self._repository.name}",
        )
        self._progress = self.hass.async_create_task(
            self._async_follow(entry, install), f"{DOMAIN} add {self._repository.name} dialog"
        )
        return self._async_show_install()

    @callback
    def _async_show_install(self) -> SubentryFlowResult:
        return self.async_show_progress(
            step_id="install",
            progress_action="install",
            progress_task=self._progress,
            description_placeholders={"repository": self._repository.name},
        )

    async def _async_follow(
        self, entry: PrivateRepositoriesConfigEntry, install: asyncio.Task[InstallResult]
    ) -> None:
        """Wait for the install, then track what it installed at once, whether
        the dialog is still there or not: it would get no updates otherwise."""
        try:
            self._result = await finish(install, partial(self._async_orphan_done, entry))
        except asyncio.CancelledError:
            # The dialog was closed, or Home Assistant stops: an open dialog
            # then shows why nothing happened.
            self._error = RestartingError()
            raise
        except (InstallError, GitHubError) as err:
            self._error = err
            return
        except Exception as err:
            LOGGER.exception("Adding %s failed", self._repository.name)
            self._error = err
            return
        if not self._result.adopted:
            self._refused = self._async_track(entry, self._result)

    @callback
    def _async_orphan_done(
        self, entry: PrivateRepositoriesConfigEntry, task: asyncio.Task[InstallResult]
    ) -> None:
        """The end of an install whose dialog was closed."""
        if task.cancelled():
            return
        if (err := task.exception()) is not None:
            log_late_failure(err, f"Adding {self._repository.name} failed after its dialog was closed")
            return
        result = task.result()
        if result.adopted:
            LOGGER.warning(
                "Adding %s changed nothing: %s was already installed, and taking it over "
                "needs a confirmation. Add the repository again to confirm it",
                self._repository.name,
                result.domain,
            )
        elif self._async_track(entry, result) == "integration_tracked":
            LOGGER.warning(
                "Adding %s installed %s, which another repository tracked meanwhile",
                self._repository.name,
                result.domain,
            )

    async def async_step_install(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """The install ended (Home Assistant calls this step then)."""
        if not self._progress.done():
            return self._async_show_install()
        if self._error is None and self._result.adopted:
            return self.async_show_progress_done(next_step_id="adopt")
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """What the install led to."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"error": "", "integration": ""}
        if isinstance(self._error, InstallError | GitHubError):
            _explain(self._error, errors, placeholders)
        elif self._error is not None:
            errors["base"] = "unknown"
        elif self._refused == "entry_removed":
            return self.async_abort(reason="entry_removed")
        elif self._refused is not None:
            errors["base"], placeholders["integration"] = self._refused, self._result.domain
        else:
            result = self._result
            # What is on disk: when nothing was installed, an older version
            # than the release may be there from an earlier install.
            version = result.version if result.installed else result.existing or result.version
            return self.async_abort(
                reason="installed" if result.installed else "tracked",
                description_placeholders={
                    # Shown as Markdown, and taken from the release.
                    "name": markdown_text(result.name),
                    "version": markdown_text(version),
                    "repository": self._repository.name,
                },
            )
        self._error = self._refused = None
        return await self._async_show_picker(self._input, errors, placeholders)

    @callback
    def _conflict(self, domain: str) -> str | None:
        """Whether another dialog tracked the repository or the integration meanwhile."""
        ids, _, domains = _tracked(self.hass)
        if self._repository.id in ids:
            return "already_tracked"
        if domain in domains:
            return "integration_tracked"
        return None

    @callback
    def _async_track(self, entry: PrivateRepositoriesConfigEntry, result: InstallResult) -> str | None:
        """Track the repository, with nothing awaited since the last check. Returns
        why it is not tracked."""
        if self.hass.config_entries.async_get_entry(entry.entry_id) is not entry:
            # The token was removed meanwhile (HA leaves its dialogs open).
            return "entry_removed"
        if reason := self._conflict(result.domain):
            return reason
        self.hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                # As Home Assistant stores the data of the subentries it creates.
                data=MappingProxyType(_repository_data(self._repository, result.domain, self._prereleases)),
                subentry_type=SUBENTRY_REPOSITORY,
                title=result.name,
                unique_id=str(self._repository.id),
            ),
        )
        return None

    async def async_step_adopt(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """The integration is already installed: confirm its updates come from here now."""
        if user_input is not None:
            if self.hass.config_entries.async_get_entry(self.handler[0]) is None:
                # The token was removed meanwhile (HA leaves its dialogs open).
                return self.async_abort(reason="entry_removed")
            if reason := self._conflict(self._result.domain):
                return self.async_abort(
                    reason=reason, description_placeholders={"integration": self._result.domain}
                )
            return self.async_create_entry(
                title=self._result.name,
                data=_repository_data(self._repository, self._result.domain, self._prereleases),
                unique_id=str(self._repository.id),
            )
        return self.async_show_form(
            step_id="adopt",
            description_placeholders={
                "integration": self._result.domain,
                # Read from a manifest, and shown as Markdown.
                "version": markdown_text(self._result.existing or "?"),
                "repository": self._repository.name,
            },
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Offer pre-releases, or stop offering them."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data_updates={CONF_PRERELEASES: bool(user_input.get(CONF_PRERELEASES, False))},
            )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PRERELEASES, default=subentry.data.get(CONF_PRERELEASES, False)
                ): BooleanSelector()
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            description_placeholders={"repository": subentry.data[CONF_REPOSITORY]},
        )
