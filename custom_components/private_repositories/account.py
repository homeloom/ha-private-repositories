"""Checking a GitHub token, and replacing the token of an entry.

Shared by the config flow (new token, reauthentication, reconfiguration) and the
repair that renews a token before it expires.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
import voluptuous as vol

from .const import (
    CONF_LOGIN,
    CONF_USER_ID,
    DOMAIN,
    LOGGER,
    NEW_TOKEN_URL,
    TOKEN_PREFIX,
    TOKENS_URL,
)
from .coordinator import repositories_of, repository_of, token_issue_id
from .github import Account, GitHubAuthError, GitHubClient, GitHubError, GitHubNotFoundError

TOKEN_SCHEMA = vol.Schema(
    {vol.Required(CONF_ACCESS_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
)
TOKEN_PLACEHOLDERS = {"new_token_url": NEW_TOKEN_URL, "tokens_url": TOKENS_URL}


async def async_check_token(
    hass: HomeAssistant, token: str, entry: ConfigEntry | None = None
) -> tuple[Account | None, str | None, dict[str, str]]:
    """The account of a token, or the error to show and its placeholders.

    With an entry, the token must belong to the same account and read every
    repository the entry tracks.
    """
    if not token.startswith(TOKEN_PREFIX):
        return None, "not_fine_grained", {}
    client = GitHubClient(async_get_clientsession(hass), token)
    try:
        account = await client.get_account()
        if entry is None:
            return account, None, {}
        if account.id != entry.data[CONF_USER_ID]:
            # The login, not the entry's title, which the user may have changed.
            return None, "wrong_account", {"account": entry.data.get(CONF_LOGIN, entry.title)}
        missing = []
        for subentry in repositories_of(entry):
            repository = repository_of(subentry)
            try:
                await client.get_current(repository)
            except GitHubNotFoundError:
                missing.append(repository.name)
    except GitHubAuthError:
        return None, "invalid_auth", {}
    except GitHubError:
        return None, "cannot_connect", {}
    except Exception:
        LOGGER.exception("Unexpected error while checking a token")
        return None, "unknown", {}
    if missing:
        return None, "missing_access", {"repositories": ", ".join(missing)}
    return account, None, {}


@callback
def async_replace_token(hass: HomeAssistant, entry: ConfigEntry, token: str, account: Account) -> None:
    """Store a checked token, then set the entry up again with it."""
    # A loaded entry reloads on its own when its data changes. Its state is read
    # first: the reload may start before async_update_entry returns.
    loaded = entry.state is ConfigEntryState.LOADED
    if token != entry.data[CONF_ACCESS_TOKEN]:
        # A reminder the user ignored was about the old token.
        ir.async_delete_issue(hass, DOMAIN, token_issue_id(entry.entry_id))
    changed = hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_TOKEN: token, CONF_LOGIN: account.login}
    )
    # The same token again (after GitHub rejected it for a while): polling
    # stopped, so start again anyway.
    if not loaded or not changed:
        hass.config_entries.async_schedule_reload(entry.entry_id)
