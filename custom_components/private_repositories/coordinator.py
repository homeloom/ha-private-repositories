"""Polling GitHub for the releases of the repositories of one token."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INTEGRATION,
    CONF_PRERELEASES,
    CONF_REMINDER_DAYS,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    DEFAULT_REMINDER_DAYS,
    DOMAIN,
    ISSUE_TOKEN_EXPIRING,
    LOGGER,
    SCAN_INTERVAL,
    SIGNAL_INSTALL,
    SUBENTRY_REPOSITORY,
)
from .github import GitHubAuthError, GitHubClient, GitHubError, Release, Repository
from .manager import Manager
from .versions import offered

type PrivateRepositoriesConfigEntry = ConfigEntry[ReleaseCoordinator]


@dataclass(slots=True)
class RepositoryState:
    """What is known about one tracked repository."""

    releases: list[Release] = field(default_factory=list)
    error: str | None = None

    @property
    def latest(self) -> Release | None:
        """The newest release offered."""
        return self.releases[0] if self.releases else None


def token_issue_id(entry_id: str) -> str:
    """The repair that warns about the end of a token."""
    return f"{ISSUE_TOKEN_EXPIRING}_{entry_id}"


def repositories_of(entry: ConfigEntry) -> list[ConfigSubentry]:
    """The repositories tracked with a token."""
    return [
        subentry for subentry in entry.subentries.values() if subentry.subentry_type == SUBENTRY_REPOSITORY
    ]


def repository_of(subentry: ConfigSubentry) -> Repository:
    """The repository a subentry tracks, with the name it had at the last check."""
    return Repository(subentry.data[CONF_REPOSITORY_ID], subentry.data[CONF_REPOSITORY])


class ReleaseCoordinator(DataUpdateCoordinator[dict[str, RepositoryState]]):
    """The releases of every repository tracked with one token."""

    config_entry: PrivateRepositoriesConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PrivateRepositoriesConfigEntry,
        client: GitHubClient,
        manager: Manager,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.manager = manager
        # The installed version of each tracked integration.
        self.installed: dict[str, str | None] = {}

    @callback
    def async_listen_installs(self) -> CALLBACK_TYPE:
        """Follow installs of the tracked integrations, whoever starts them."""
        return async_dispatcher_connect(self.hass, SIGNAL_INSTALL, self._async_install_changed)

    @callback
    def _async_install_changed(self, domain: str | None, version: str | None) -> None:
        if domain not in {subentry.data[CONF_INTEGRATION] for subentry in self.repositories}:
            return
        if version is not None:
            self.installed[domain] = version
        self.async_update_listeners()

    @property
    def repositories(self) -> list[ConfigSubentry]:
        """The repositories tracked with this token."""
        return repositories_of(self.config_entry)

    @property
    def expiration(self) -> datetime | None:
        """When the token expires, if it does."""
        return self.client.expiration

    async def _async_update_data(self) -> dict[str, RepositoryState]:
        # The same list all along, even if a repository is added meanwhile: the
        # entry reloads then.
        repositories = self.repositories
        try:
            # Checks the token, and learns when it expires, even with no repository.
            await self.client.get_account()
        except GitHubAuthError as err:
            self._async_delete_token_issue()
            raise ConfigEntryAuthFailed(str(err)) from err
        except GitHubError as err:
            raise UpdateFailed(str(err)) from err
        self._async_token_issue()

        previous = self.data or {}
        states: dict[str, RepositoryState] = {}
        renamed: list[tuple[ConfigSubentry, str]] = []
        for subentry in repositories:
            repository = repository_of(subentry)
            try:
                # By its id: the current name, and never another repository
                # created under an old name.
                current = await self.client.get_current(repository)
                if current.name != repository.name:
                    renamed.append((subentry, current.name))
                # Errors then name the repository as it is stored, which
                # diagnostics leave out.
                releases = await self.client.list_releases(repository)
            except GitHubAuthError as err:
                self._async_delete_token_issue()
                raise ConfigEntryAuthFailed(str(err)) from err
            except GitHubError as err:
                old = previous.get(subentry.subentry_id)
                if old is None or old.error != str(err):
                    LOGGER.warning("Cannot check %s: %s", repository.name, err)
                states[subentry.subentry_id] = RepositoryState(
                    releases=old.releases if old else [], error=str(err)
                )
                continue
            states[subentry.subentry_id] = RepositoryState(
                releases=offered(releases, subentry.data.get(CONF_PRERELEASES, False))
            )

        self.installed = await self.manager.async_installed_versions(
            {subentry.data[CONF_INTEGRATION]: subentry.data[CONF_REPOSITORY_ID] for subentry in repositories}
        )
        if self.hass.config_entries.async_get_entry(self.config_entry.entry_id) is not self.config_entry:
            return states  # Removed meanwhile.
        for old_subentry, name in renamed:
            # Removed or reconfigured meanwhile: the subentry as it is now.
            if (subentry := self.config_entry.subentries.get(old_subentry.subentry_id)) is None:
                continue
            # The entry reloads with the new name.
            LOGGER.info("%s is now %s on GitHub", subentry.data[CONF_REPOSITORY], name)
            self.hass.config_entries.async_update_subentry(
                self.config_entry, subentry, data={**subentry.data, CONF_REPOSITORY: name}
            )
        return states

    @callback
    def _async_delete_token_issue(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, token_issue_id(self.config_entry.entry_id))

    @callback
    def _async_token_issue(self) -> None:
        """Warn ahead of the end of the token, with how to renew it."""
        expiration = self.client.expiration
        days = self.config_entry.options.get(CONF_REMINDER_DAYS, DEFAULT_REMINDER_DAYS)
        if expiration is None or expiration - dt_util.utcnow() > timedelta(days=days):
            self._async_delete_token_issue()
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            token_issue_id(self.config_entry.entry_id),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_TOKEN_EXPIRING,
            translation_placeholders={
                "account": self.config_entry.title,
                "date": dt_util.as_local(expiration).date().isoformat(),
            },
            data={"entry_id": self.config_entry.entry_id},
        )
