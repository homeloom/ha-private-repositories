"""Repairs: restart to load installed integrations, renew a token."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir, recorder

from . import DATA_MANAGER
from .account import TOKEN_PLACEHOLDERS, TOKEN_SCHEMA, async_check_token, async_replace_token
from .const import DOMAIN, ISSUE_RESTART, ISSUE_TOKEN_EXPIRING
from .manager import Manager


def _issue_placeholders(flow: RepairsFlow) -> dict[str, str]:
    """The placeholders of the flow's issue, to repeat them in the flow."""
    issue = ir.async_get(flow.hass).async_get_issue(DOMAIN, flow.issue_id)
    return dict(issue.translation_placeholders or {}) if issue else {}


class RestartFlow(RepairsFlow):
    """Restart Home Assistant to load what was installed."""

    def __init__(self, manager: Manager) -> None:
        self._manager = manager

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """The repair starts here (its input is the issue id, not a form)."""
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Check the configuration, then restart."""
        if user_input is None:
            return self.async_show_form(step_id="confirm", description_placeholders=_issue_placeholders(self))
        if self._manager.installing:
            return self.async_abort(reason="install_in_progress")
        # No install may start while the configuration is checked and Home
        # Assistant stops.
        self._manager.async_block_installs()
        restarting = False
        try:
            try:
                # A refusal is only reported while the request is pending: check
                # what Home Assistant would refuse first, then restart without
                # waiting (on Home Assistant OS, the Supervisor only answers once
                # the restart is over).
                await self.hass.services.async_call("homeassistant", "check_config", blocking=True)
            except HomeAssistantError as err:
                return self.async_abort(reason="invalid_config", description_placeholders={"error": str(err)})
            if recorder.async_migration_in_progress(self.hass):
                return self.async_abort(reason="migration_in_progress")
            await self.hass.services.async_call("homeassistant", "restart")
            restarting = True
        finally:
            # No restart after all (refused, failed, or the dialog went away).
            if not restarting:
                self._manager.async_unblock_installs()
        return self.async_create_entry(data={})


class RenewTokenFlow(RepairsFlow):
    """Paste a new token before the current one expires."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """The repair starts here (its input is the issue id, not a form)."""
        return await self.async_step_renew()

    async def async_step_renew(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Show how to renew the token, and take the new one."""
        errors: dict[str, str] = {}
        placeholders = {
            "date": "?",
            **_issue_placeholders(self),
            **TOKEN_PLACEHOLDERS,
            "account": self._entry.title,
            "repositories": "",
        }
        if user_input is not None:
            token = user_input[CONF_ACCESS_TOKEN].strip()
            account, error, details = await async_check_token(self.hass, token, self._entry)
            if account is not None:
                async_replace_token(self.hass, self._entry, token, account)
                return self.async_create_entry(data={})
            errors["base"] = str(error)
            placeholders.update(details)
        return self.async_show_form(
            step_id="renew", data_schema=TOKEN_SCHEMA, errors=errors, description_placeholders=placeholders
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """The flow that fixes an issue."""
    if issue_id == ISSUE_RESTART:
        return RestartFlow(hass.data[DATA_MANAGER])
    if issue_id.startswith(f"{ISSUE_TOKEN_EXPIRING}_") and data:
        entry = hass.config_entries.async_get_entry(str(data.get("entry_id")))
        if entry is not None and entry.domain == DOMAIN:
            return RenewTokenFlow(entry)
    return ConfirmRepairFlow()
