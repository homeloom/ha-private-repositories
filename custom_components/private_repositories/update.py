"""One update entity per tracked repository: Settings > System > Updates."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_INTEGRATION, DOMAIN
from .coordinator import PrivateRepositoriesConfigEntry, ReleaseCoordinator, RepositoryState, repository_of
from .github import GitHubError, GitHubNotFoundError, Release
from .installer import InstallError
from .versions import is_newer, version_from_tag

PARALLEL_UPDATES = 0
MAX_RELEASE_NOTES = 10
# A version typed in update.install: it becomes part of a request to GitHub.
VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrivateRepositoriesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One entity per repository, attached to its subentry."""
    coordinator = entry.runtime_data
    for subentry in coordinator.repositories:
        async_add_entities(
            [IntegrationUpdateEntity(coordinator, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class IntegrationUpdateEntity(CoordinatorEntity[ReleaseCoordinator], UpdateEntity):
    """The integration of one repository, against the repository's releases."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
        | UpdateEntityFeature.SPECIFIC_VERSION
    )

    def __init__(self, coordinator: ReleaseCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator)
        self._subentry_id = subentry.subentry_id
        self._repository = repository_of(subentry)
        self._domain: str = subentry.data[CONF_INTEGRATION]
        self._attr_unique_id = subentry.subentry_id
        self._attr_title = subentry.title
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer=self._repository.name.split("/", 1)[0],
            model="Custom integration",
            model_id=self._domain,
            configuration_url=f"https://github.com/{self._repository.name}",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._shown_percentage = -1

    @property
    def _state(self) -> RepositoryState | None:
        return (self.coordinator.data or {}).get(self._subentry_id)

    @property
    def available(self) -> bool:
        """Unavailable while its repository cannot be read."""
        return super().available and (state := self._state) is not None and state.error is None

    @property
    def installed_version(self) -> str | None:
        """The installed version: named after the release tag that installed it while
        the files are still that release's, else the version in its manifest."""
        return self.coordinator.installed.get(self._domain)

    @property
    def in_progress(self) -> bool:
        """Installing, or waiting for another install to end, whoever asked: from
        this entity, or from one the entry had before it reloaded."""
        return self.coordinator.manager.is_installing(self._domain)

    @property
    def latest_version(self) -> str | None:
        """The newest release."""
        if (state := self._state) is None or state.latest is None:
            return None
        return state.latest.version

    @property
    def release_summary(self) -> str | None:
        """The name of the newest release, when it says more than its tag."""
        if (state := self._state) is None or (latest := state.latest) is None:
            return None
        return latest.name if latest.name not in (latest.tag, latest.version) else None

    @property
    def release_url(self) -> str | None:
        """The newest release on GitHub."""
        if (state := self._state) is None or state.latest is None:
            return None
        return state.latest.url or None

    @property
    def entity_picture(self) -> str | None:
        """The icon of the tracked integration rather than this one's, from
        wherever this version of Home Assistant takes integration icons."""
        if (picture := super().entity_picture) is None:
            return None
        return picture.replace(f"/{self.platform.platform_name}/", f"/{self._domain}/")

    async def async_release_notes(self) -> str | None:
        """The notes of every release newer than the installed version."""
        if (state := self._state) is None or state.latest is None:
            return None
        installed = self.installed_version
        newer = [
            release for release in state.releases if installed is None or is_newer(release.version, installed)
        ][:MAX_RELEASE_NOTES] or [state.latest]
        if len(newer) == 1:
            return newer[0].body or None
        return "\n\n".join(f"## {release.name}\n\n{release.body}".rstrip() for release in newer)

    async def _async_release(self, version: str | None) -> Release:
        state = self._state
        placeholders = {"repository": self._repository.name, "version": str(version)}
        if version is None:
            if state is None or state.latest is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="no_release",
                    translation_placeholders=placeholders,
                )
            return state.latest
        wanted = version_from_tag(version.strip())
        if not VERSION_RE.fullmatch(wanted):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_version",
                translation_placeholders=placeholders,
            )
        for release in state.releases if state else []:
            if release.version == wanted:
                return release
        # An older release than the ones listed, or a pre-release.
        for tag in (f"v{wanted}", wanted):
            try:
                return await self.coordinator.client.get_release(self._repository, tag)
            except GitHubNotFoundError:
                continue
            except GitHubError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="cannot_check",
                    translation_placeholders={**placeholders, "error": str(err)},
                ) from err
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_version",
            translation_placeholders=placeholders,
        )

    @callback
    def _async_progress(self, received: int, total: int | None) -> None:
        """Downloading takes the progress bar to 90 %."""
        if not total:
            return
        percentage = min(90, int(received * 90 / total))
        if percentage != self._shown_percentage:
            self._shown_percentage = percentage
            self._attr_update_percentage = percentage
            self.async_write_ha_state()

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Install the newest release, or the release of a version."""
        release = await self._async_release(version)
        # The entity shows the install once the Manager has it, with no
        # percentage until the download announces its size.
        self._attr_update_percentage = None
        self._shown_percentage = -1
        try:
            await self.coordinator.manager.async_install(
                self.coordinator.client,
                self._repository,
                release,
                domain=self._domain,
                progress=self._async_progress,
            )
        except (GitHubError, InstallError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                # Expected outcomes have texts of their own.
                translation_key=f"install_{err.reason}" if err.reason else "install_failed",
                translation_placeholders={
                    "name": str(self.title),
                    "tag": release.tag,
                    "error": str(err),
                    **err.placeholders,
                },
            ) from err
