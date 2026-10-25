"""Private Repositories: install and update custom integrations from private
GitHub repositories, from the Home Assistant UI.

Each config entry holds one GitHub token. Each repository tracked with that
token is a subentry, with an update entity for its integration. Updates follow
the repository's GitHub releases.
"""

from __future__ import annotations

from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntry, ConfigEntryChange
from homeassistant.const import CONF_ACCESS_TOKEN, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.hass_dict import HassKey

from .const import CONF_INTEGRATION, DOMAIN
from .coordinator import PrivateRepositoriesConfigEntry, ReleaseCoordinator, repositories_of, token_issue_id
from .github import GitHubClient
from .manager import Manager

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.SENSOR, Platform.UPDATE]

DATA_MANAGER: HassKey[Manager] = HassKey(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the install manager shared by all tokens."""
    manager = Manager(hass)
    await manager.async_load()
    hass.data[DATA_MANAGER] = manager

    @callback
    def _async_entry_changed(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        # A disabled entry stops warning about its token, whether it was loaded
        # or not (then Home Assistant does not unload it).
        if entry.domain == DOMAIN and entry.disabled_by is not None:
            ir.async_delete_issue(hass, DOMAIN, token_issue_id(entry.entry_id))

    async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _async_entry_changed)
    return True


def _tracked_domains(hass: HomeAssistant) -> set[str]:
    return {
        subentry.data[CONF_INTEGRATION]
        for entry in hass.config_entries.async_entries(DOMAIN)
        for subentry in repositories_of(entry)
    }


async def async_setup_entry(hass: HomeAssistant, entry: PrivateRepositoriesConfigEntry) -> bool:
    """Set up one GitHub token and its repositories."""
    manager = hass.data[DATA_MANAGER]
    # Repositories removed since: their install records are dropped once their
    # files are gone or hold another version.
    await manager.async_forget(set(manager.records) - _tracked_domains(hass))
    client = GitHubClient(async_get_clientsession(hass), entry.data[CONF_ACCESS_TOKEN])
    coordinator = ReleaseCoordinator(hass, entry, client, manager)
    # Added or removed repositories, a new token, new options: start again with
    # them, including changes made during the first refresh.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    entry.async_on_unload(coordinator.async_listen_installs())
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: PrivateRepositoriesConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PrivateRepositoriesConfigEntry) -> bool:
    """Unload one GitHub token."""
    # A reload keeps the token repair, and with it the user's choice to ignore it.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: PrivateRepositoriesConfigEntry) -> None:
    """Forget a removed token. Installed integrations stay in place."""
    ir.async_delete_issue(hass, DOMAIN, token_issue_id(entry.entry_id))
    if manager := hass.data.get(DATA_MANAGER):
        await manager.async_forget(set(manager.records) - _tracked_domains(hass))
