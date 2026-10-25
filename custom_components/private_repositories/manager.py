"""Installs, shared by all GitHub tokens: one at a time, recorded, and
followed by a request to restart Home Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from functools import partial
from pathlib import Path
import re
import tempfile
from typing import Any

import homeassistant.components
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from . import installer
from .const import DOMAIN, ISSUE_RESTART, LOGGER, SIGNAL_INSTALL, STORAGE_KEY, STORAGE_VERSION
from .github import Asset, GitHubClient, GitHubError, ProgressCallback, Release, Repository
from .versions import is_older, version_from_tag

BUILT_IN = Path(homeassistant.components.__file__).parent
# Installs are refused from a confirmed restart on, in case Home Assistant does
# not stop after all (its configuration check failed, for instance).
RESTART_BLOCK = timedelta(minutes=2)
# Markdown (a leading "- " or "1." makes a list), HTML entities, and what
# makes links of bare addresses (www., https://, a@b.c).
MARKDOWN_RE = re.compile(r"([\\`*_{}\[\]()<>#+\-!|~.:@&])")


class InstallMode(Enum):
    """What to do once a release is unpacked and checked."""

    # Adding a repository: install its integration unless it is already there.
    IF_MISSING = "if_missing"
    # Updating: install the release.
    ALWAYS = "always"


class _DomainError(installer.InstallError):
    """A refusal about one integration."""

    def __init__(self, message: str, domain: str) -> None:
        super().__init__(message)
        self.domain = domain

    @property
    def placeholders(self) -> dict[str, str]:
        """The integration."""
        return {"integration": self.domain}


class DomainTakenError(_DomainError):
    """The release holds an integration already tracked from another repository."""

    reason = "integration_tracked"

    def __init__(self, domain: str) -> None:
        super().__init__(f"The integration {domain} is already tracked from another repository", domain)


class DomainRefusedError(_DomainError):
    """The release would replace a built-in integration, or this one."""

    reason = "integration_refused"

    def __init__(self, domain: str) -> None:
        what = "this integration itself" if domain == DOMAIN else "part of Home Assistant"
        super().__init__(f"The release would replace {domain}, which is {what}", domain)


class FolderUnreadableError(_DomainError):
    """custom_components/<domain> holds files, but no integration with a version."""

    reason = "integration_unreadable"

    def __init__(self, domain: str) -> None:
        super().__init__(
            f"custom_components/{domain} already exists, but holds no integration"
            " with a readable manifest.json and version",
            domain,
        )


class AlreadyInstallingError(_DomainError):
    """An install of the integration is already running, or waiting for another."""

    reason = "already_installing"

    def __init__(self, domain: str) -> None:
        super().__init__(f"An install of {domain} is already running or waiting to start", domain)


class RestartingError(installer.InstallError):
    """Home Assistant is about to restart."""

    reason = "restarting"

    def __init__(self) -> None:
        super().__init__("Home Assistant is restarting: try again once it has started")


class HomeAssistantTooOldError(installer.InstallError):
    """The release needs a newer Home Assistant (hacs.json says which)."""

    reason = "needs_newer_home_assistant"

    def __init__(self, tag: str, minimum: str) -> None:
        super().__init__(f"{tag} needs Home Assistant {minimum} or newer")
        self.minimum = minimum

    @property
    def placeholders(self) -> dict[str, str]:
        """The version needed."""
        return {"minimum": self.minimum}


@dataclass(frozen=True, slots=True)
class InstallResult:
    """What a release contains, and what was done with it."""

    domain: str
    name: str
    version: str
    installed: bool
    # The version already in custom_components/ when nothing was installed.
    existing: str | None = None
    # That version was not installed from this repository by this integration.
    adopted: bool = False


class Manager:
    """Installs integrations from releases."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.config_dir = Path(hass.config.config_dir)
        self._store: Store[dict[str, dict[str, Any]]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # domain -> {"repository", "repository_id", "tag", "version"} of the last install.
        self._records: dict[str, dict[str, Any]] = {}
        # One install at a time, download included.
        self._lock = asyncio.Lock()
        # Held while files in custom_components/ change, and while installed
        # versions are read: a version read then would be outdated at once.
        self._files_lock = asyncio.Lock()
        # Integrations an install was asked for, from the request until it
        # ends, waiting for another install included.
        self._requested: set[str] = set()
        # Installs that have not ended.
        self._tasks: set[asyncio.Task[InstallResult]] = set()
        # domain -> "Name version", installed or put back since Home Assistant started.
        self._pending_restart: dict[str, str] = {}
        self._unblock: CALLBACK_TYPE | None = None

    async def async_load(self) -> None:
        """Load the records, and undo any install a stop interrupted."""
        stored = await self._store.async_load()
        self._records = stored if isinstance(stored, dict) else {}
        try:
            restored, failed = await self.hass.async_add_executor_job(installer.recover, self.config_dir)
        except OSError as err:
            LOGGER.error("Cannot check for interrupted installs: %s", err)
            return
        if failed:
            LOGGER.warning(
                "Cannot remove what interrupted installs left in %s: %s",
                installer.work_dir(self.config_dir),
                ", ".join(failed),
            )
        for domain in restored:
            # Home Assistant listed custom integrations before this ran: the
            # restored folder only loads after one more restart.
            LOGGER.warning("An install of %s was interrupted: its previous version was put back", domain)
            manifest = await self.hass.async_add_executor_job(
                installer.installed_manifest, self.config_dir, domain
            )
            self._pending_restart[domain] = _label(
                str((manifest or {}).get("name") or domain), str((manifest or {}).get("version") or "")
            )
        if restored:
            self._async_restart_issue()

    @property
    def installing(self) -> bool:
        """Whether an install is running, or waiting to start."""
        return bool(self._tasks)

    def is_installing(self, domain: str) -> bool:
        """Whether an install of an integration was asked for and has not ended,
        while it waits for another install too. Entities of a reloaded entry
        learn this way of an install they did not start."""
        return domain in self._requested

    @callback
    def async_block_installs(self) -> None:
        """Refuse new installs: Home Assistant is about to restart."""
        self.async_unblock_installs()
        self._unblock = async_call_later(self.hass, RESTART_BLOCK, self._async_restart_over)

    @callback
    def _async_restart_over(self, _now: datetime) -> None:
        self._unblock = None

    @callback
    def async_unblock_installs(self) -> None:
        """Accept installs again: the restart did not happen."""
        if self._unblock is not None:
            self._unblock()
            self._unblock = None

    @property
    def records(self) -> Mapping[str, Mapping[str, Any]]:
        """The last install of each integration."""
        return self._records

    def _is_ours(self, domain: str, repository_id: int, on_disk: str | None) -> bool:
        """Whether the files on disk are still the ones a release of this
        repository installed."""
        record = self._records.get(domain)
        return bool(
            on_disk is not None
            and record
            and record.get("repository_id") == repository_id
            and record.get("version") == on_disk
            and isinstance(record.get("tag"), str)
        )

    def _shown_version(self, domain: str, repository_id: int, on_disk: str | None) -> str | None:
        """The installed version as the release tag names it, while the files are
        that release's."""
        if self._is_ours(domain, repository_id, on_disk):
            return version_from_tag(self._records[domain]["tag"])
        return on_disk

    def _read_versions(self, domains: Collection[str]) -> dict[str, str | None]:
        return {domain: installer.installed_version(self.config_dir, domain) for domain in domains}

    async def async_installed_versions(self, tracked: Mapping[str, int]) -> dict[str, str | None]:
        """The installed version of each integration (None when absent).

        tracked maps each integration to the id of the repository it is tracked
        from. Waits for files being swapped: once an install has signalled its
        end, every version read is the new one.
        """
        async with self._files_lock:
            on_disk = await self.hass.async_add_executor_job(self._read_versions, list(tracked))
        return {
            domain: self._shown_version(domain, tracked[domain], version)
            for domain, version in on_disk.items()
        }

    async def async_forget(self, domains: Collection[str]) -> None:
        """Drop the records of integrations no longer tracked, once their files
        changed or went away. While the files are those of the recorded release,
        tracking the repository again picks up where it left off."""
        # Other entries set up at the same time forget too, and installs go on:
        # only records left as they were while the files were read are dropped.
        if not (recorded := {domain: self._records[domain] for domain in domains if domain in self._records}):
            return
        async with self._files_lock:
            on_disk = await self.hass.async_add_executor_job(self._read_versions, list(recorded))
        if forgotten := [
            domain
            for domain, record in recorded.items()
            if self._records.get(domain) is record and on_disk[domain] != record.get("version")
        ]:
            for domain in forgotten:
                del self._records[domain]
            await self._store.async_save(self._records)

    async def async_install(
        self,
        client: GitHubClient,
        repository: Repository,
        release: Release,
        *,
        domain: str | None,
        mode: InstallMode = InstallMode.ALWAYS,
        refused_domains: Collection[str] = (),
        progress: ProgressCallback | None = None,
    ) -> InstallResult:
        """Download a release, check it, and install it according to mode.

        domain is the integration the caller expects, if it knows it: a second
        install of it is refused until the first ends.

        Raises GitHubError or installer.InstallError.
        """
        if domain is not None:
            if domain in self._requested:
                # The same release would be downloaded and installed twice.
                raise AlreadyInstallingError(domain)
            self._requested.add(domain)
            # Every update entity of the integration shows the install from now
            # on, while it waits for another one too.
            async_dispatcher_send(self.hass, SIGNAL_INSTALL, domain, None)
        # The install goes on if the caller is cancelled (a closed dialog, a
        # stopped script): the lock must stay held while files are moved.
        task = self.hass.async_create_task(
            self._async_install(
                client, repository, release, domain, mode, frozenset(refused_domains), progress
            ),
            f"{DOMAIN} install of {repository.name}",
        )
        self._tasks.add(task)
        # A callback rather than a finally: a task cancelled before it starts
        # runs none.
        task.add_done_callback(partial(self._async_install_done, domain, release.tag))
        return await finish(task)

    @callback
    def _async_install_done(self, domain: str | None, tag: str, task: asyncio.Task[InstallResult]) -> None:
        """The end of an install, however it ended."""
        self._tasks.discard(task)
        if domain is None:
            return
        self._requested.discard(domain)
        installed = not task.cancelled() and task.exception() is None and task.result().installed
        # Every update entity of the integration shows the end, and the version
        # installed, including entities set up after it started.
        async_dispatcher_send(self.hass, SIGNAL_INSTALL, domain, version_from_tag(tag) if installed else None)

    async def _async_install(
        self,
        client: GitHubClient,
        repository: Repository,
        release: Release,
        domain: str | None,
        mode: InstallMode,
        refused_domains: frozenset[str],
        progress: ProgressCallback | None,
    ) -> InstallResult:
        async with self._lock:
            return await self._async_download_and_install(
                client, repository, release, domain, mode, refused_domains, progress
            )

    async def _async_download_and_install(
        self,
        client: GitHubClient,
        repository: Repository,
        release: Release,
        domain: str | None,
        mode: InstallMode,
        refused_domains: frozenset[str],
        progress: ProgressCallback | None,
    ) -> InstallResult:
        if self._unblock is not None or self.hass.is_stopping:
            raise RestartingError
        layout = installer.parse_layout(await client.get_file(repository, "hacs.json", release.tag))
        if layout.zip_release:
            asset = _zip_asset(release, layout.filename)
            data = await client.download_asset(repository, asset, progress)
        else:
            data = await client.download_archive(repository, release.tag, progress)
        async with self._files_lock:
            result = await self.hass.async_add_executor_job(
                self._unpack_and_install, data, layout, release.tag, domain, mode, refused_domains
            )
            if not result.installed and result.domain in self._records:
                # Already there: adopted, unless a release of this repository put
                # these files there.
                result = replace(
                    result, adopted=not self._is_ours(result.domain, repository.id, result.existing)
                )
            if result.installed:
                LOGGER.info("Installed %s %s from %s", result.domain, release.tag, repository.name)
                self._records[result.domain] = {
                    "repository": repository.name,
                    "repository_id": repository.id,
                    "tag": release.tag,
                    "version": result.version,
                }
                await self._store.async_save(self._records)
                self._pending_restart[result.domain] = _label(result.name, version_from_tag(release.tag))
                self._async_restart_issue()
        return result

    def _unpack_and_install(
        self,
        data: bytes,
        layout: installer.Layout,
        tag: str,
        domain: str | None,
        mode: InstallMode,
        refused_domains: frozenset[str],
    ) -> InstallResult:
        work = installer.work_dir(self.config_dir)
        try:
            work.mkdir(parents=True, exist_ok=True)
            unpack_dir = tempfile.TemporaryDirectory(dir=work, prefix="unpack-", ignore_cleanup_errors=True)
        except OSError as err:
            raise installer.InstallWriteError(f"Cannot write in {work.parent}: {err.strerror}") from err
        with unpack_dir as folder:
            package = installer.unpack(data, Path(folder), layout, is_zip=layout.zip_release, domain=domain)
            if package.domain in refused_domains:
                raise DomainTakenError(package.domain)
            if mode is InstallMode.IF_MISSING and (
                package.domain == DOMAIN or (BUILT_IN / package.domain / "manifest.json").is_file()
            ):
                raise DomainRefusedError(package.domain)
            existing = installer.installed_version(self.config_dir, package.domain)
            if mode is InstallMode.IF_MISSING and existing is not None:
                return InstallResult(
                    package.domain,
                    package.name,
                    package.version,
                    installed=False,
                    existing=existing,
                    adopted=True,
                )
            if mode is InstallMode.IF_MISSING and installer.is_occupied(self.config_dir, package.domain):
                # Files of unknown origin: never replaced without the user.
                raise FolderUnreadableError(package.domain)
            if layout.homeassistant and is_older(HA_VERSION, layout.homeassistant):
                raise HomeAssistantTooOldError(tag, layout.homeassistant)
            installer.install(package, self.config_dir)
            return InstallResult(package.domain, package.name, package.version, installed=True)

    @callback
    def _async_restart_issue(self) -> None:
        # Deleted, then created again: an update would keep an issue the user
        # ignored hidden, and each install must show it again.
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_RESTART)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_RESTART,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_RESTART,
            translation_placeholders={"integrations": ", ".join(sorted(self._pending_restart.values()))},
        )


def _label(name: str, version: str) -> str:
    """ "Name version" for the restart repair: the name comes from a release."""
    name = " ".join(name.split())[: installer.MAX_NAME_LENGTH]
    return markdown_text(f"{name} {version[:64]}")


def markdown_text(text: str) -> str:
    """Text from a release, on one line, with Markdown and links escaped, for
    a text that Home Assistant shows as Markdown."""
    return MARKDOWN_RE.sub(r"\\\1", " ".join(text.split()))


async def finish[T](task: asyncio.Task[T], orphaned: Callable[[asyncio.Task[T]], None] | None = None) -> T:
    """Wait for an install task that goes on if the caller is cancelled. orphaned
    then gets the task once it ends; by default its failure is logged, since
    nobody else hears of it."""
    try:
        # Unlike asyncio.shield, a cancelled wait leaves the task alone, and adds
        # no log of its own.
        await asyncio.wait({task})
    except asyncio.CancelledError:
        task.add_done_callback(orphaned or log_failure)
        raise
    if task.cancelled():
        # Stopped on its own (Home Assistant stops): the caller was not cancelled.
        raise RestartingError
    return task.result()


def log_failure(task: asyncio.Task[Any]) -> None:
    """Report the end of an install whose caller went away."""
    if not task.cancelled() and (err := task.exception()) is not None:
        log_late_failure(err, "An install failed after its caller went away")


def log_late_failure(err: BaseException, message: str) -> None:
    """Log a failure nobody else hears of: a known one in one line, anything
    else (a bug) with its traceback."""
    if isinstance(err, installer.InstallError | GitHubError):
        LOGGER.warning("%s: %s", message, err)
    else:
        LOGGER.error("%s", message, exc_info=err)


def _zip_asset(release: Release, filename: str | None) -> Asset:
    """The zip file a release ships, as hacs.json names it."""
    for asset in release.assets:
        if asset.name == filename:
            return asset
    tag = installer.shown(release.tag)
    raise installer.InstallError(f"Release {tag} has no asset named {installer.shown(filename)}")
