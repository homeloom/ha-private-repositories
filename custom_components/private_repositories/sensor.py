"""When the GitHub token expires."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, TOKENS_URL
from .coordinator import PrivateRepositoriesConfigEntry, ReleaseCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrivateRepositoriesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """The token's expiration date."""
    async_add_entities([TokenExpirationSensor(entry.runtime_data)])


class TokenExpirationSensor(CoordinatorEntity[ReleaseCoordinator], SensorEntity):
    """The date GitHub gives for the end of the token (unknown if it never expires)."""

    _attr_has_entity_name = True
    _attr_translation_key = "token_expiration"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ReleaseCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_token_expiration"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"GitHub {entry.title}",
            manufacturer="GitHub",
            model="Personal access token",
            configuration_url=TOKENS_URL,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> datetime | None:
        """The expiration date."""
        return self.coordinator.expiration
