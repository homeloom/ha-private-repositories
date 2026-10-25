"""Versions: release tags, the order of releases, and what Home Assistant accepts.

Nothing here depends on Home Assistant, so the installer can use it too.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from contextlib import suppress
from typing import Protocol

from awesomeversion import AwesomeVersion, AwesomeVersionException, AwesomeVersionStrategy

# The versions Home Assistant accepts in the manifest of a custom integration.
VERSION_STRATEGIES = [
    AwesomeVersionStrategy.CALVER,
    AwesomeVersionStrategy.SEMVER,
    AwesomeVersionStrategy.SIMPLEVER,
    AwesomeVersionStrategy.BUILDVER,
    AwesomeVersionStrategy.PEP440,
]


class Versioned(Protocol):
    """Anything with a version and a pre-release flag, such as a release."""

    @property
    def version(self) -> str: ...

    @property
    def prerelease(self) -> bool: ...


def version_from_tag(tag: str) -> str:
    """ "v1.2.3" and "1.2.3" both mean version 1.2.3."""
    if len(tag) > 1 and tag[0] in "vV" and tag[1].isdigit():
        return tag[1:]
    return tag


def is_valid(version: str) -> bool:
    """Whether Home Assistant accepts this version for a custom integration."""
    try:
        AwesomeVersion(version, ensure_strategy=VERSION_STRATEGIES)
    except AwesomeVersionException:
        return False
    return True


def is_newer(version: str, than: str) -> bool:
    """Whether a version is newer than another (different, if they cannot be compared)."""
    try:
        return AwesomeVersion(version) > AwesomeVersion(than)
    except AwesomeVersionException:
        return version != than


def is_older(version: str, than: str) -> bool:
    """Whether a version is older than another (False if they cannot be compared)."""
    try:
        return AwesomeVersion(version) < AwesomeVersion(than)
    except AwesomeVersionException:
        return False


def newest_first[T: Versioned](releases: Iterable[T]) -> list[T]:
    """Releases with a version newest first, then the others in their own order.

    A tag that is not a version ("nightly", "latest") never comes before a
    release that has one.
    """
    releases = list(releases)
    versioned = [release for release in releases if is_valid(release.version)]
    others = [release for release in releases if not is_valid(release.version)]
    # Versions of different kinds may not compare: GitHub's order stays then.
    with suppress(AwesomeVersionException):
        versioned = sorted(versioned, key=lambda release: AwesomeVersion(release.version), reverse=True)
    return versioned + others


def offered[T: Versioned](releases: Sequence[T], prereleases: bool) -> list[T]:
    """The releases to offer, newest first."""
    return newest_first(release for release in releases if prereleases or not release.prerelease)
