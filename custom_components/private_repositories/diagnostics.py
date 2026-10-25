"""Diagnostics: what is tracked and installed, without the token, the account or
the names of the repositories (diagnostics often end up in public issues)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant

from . import DATA_MANAGER
from .const import CONF_INTEGRATION, CONF_LOGIN, CONF_REPOSITORY, CONF_REPOSITORY_ID, CONF_USER_ID
from .coordinator import PrivateRepositoriesConfigEntry

TO_REDACT = {
    CONF_ACCESS_TOKEN,
    CONF_LOGIN,
    CONF_USER_ID,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    "title",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PrivateRepositoriesConfigEntry
) -> dict[str, Any]:
    """Diagnostics for one token."""
    coordinator = entry.runtime_data
    states = coordinator.data or {}
    records = hass.data[DATA_MANAGER].records
    repositories = []
    for subentry in coordinator.repositories:
        state = states.get(subentry.subentry_id)
        domain = subentry.data[CONF_INTEGRATION]
        error = state.error if state else None
        repositories.append(
            {
                "subentry": dict(subentry.data),
                "installed": coordinator.installed.get(domain),
                "releases": [
                    {"tag": release.tag, "prerelease": release.prerelease}
                    for release in (state.releases if state else [])
                ],
                # Errors name the repository.
                "error": error.replace(subentry.data[CONF_REPOSITORY], REDACTED) if error else None,
                "last_install": dict(records[domain]) if domain in records else None,
            }
        )
    return async_redact_data(
        {
            "entry": entry.as_dict(),
            "token_expiration": coordinator.expiration.isoformat() if coordinator.expiration else None,
            "repositories": repositories,
        },
        TO_REDACT,
    )
