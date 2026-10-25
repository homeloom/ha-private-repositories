"""Versions and the order of releases."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from custom_components.private_repositories.versions import (
    is_newer,
    is_older,
    is_valid,
    newest_first,
    offered,
    version_from_tag,
)


@dataclass
class Item:
    version: str
    prerelease: bool = False


@pytest.mark.parametrize(
    ("tag", "version"), [("v1.2.3", "1.2.3"), ("V2", "2"), ("1.0", "1.0"), ("v", "v"), ("vnext", "vnext")]
)
def test_version_from_tag(tag: str, version: str) -> None:
    assert version_from_tag(tag) == version


@pytest.mark.parametrize(
    ("version", "valid"),
    [("1.0.0", True), ("2026.10.1", True), ("1.0.0b1", True), ("3", True), ("nightly", False), ("", False)],
)
def test_is_valid(version: str, valid: bool) -> None:
    assert is_valid(version) is valid


def test_comparisons() -> None:
    assert is_newer("1.10.0", "1.9.0")
    assert not is_newer("1.0.0", "1.0.0")
    # Versions that cannot be compared: different means newer, never older.
    assert is_newer("nightly", "1.0.0")
    assert not is_older("nightly", "1.0.0")
    assert is_older("1.0.0", "1.0.1")


def test_newest_first_puts_tags_without_a_version_last() -> None:
    releases = [Item("nightly"), Item("1.0.0"), Item("latest"), Item("1.10.0"), Item("1.9.0")]
    assert [r.version for r in newest_first(releases)] == ["1.10.0", "1.9.0", "1.0.0", "nightly", "latest"]


def test_offered() -> None:
    releases = [Item("2.0.0b1", prerelease=True), Item("1.0.0")]
    assert [r.version for r in offered(releases, prereleases=False)] == ["1.0.0"]
    assert [r.version for r in offered(releases, prereleases=True)] == ["2.0.0b1", "1.0.0"]


def test_offered_sorts_what_github_lists_by_date() -> None:
    """GitHub lists releases newest published first: a backport published after
    a newer release, and pre-releases, come in any order."""
    releases = [
        Item("1.2.0-dev.9", prerelease=True),
        Item("1.2.0-dev.10", prerelease=True),
        Item("1.1.5"),
        Item("1.2.0-dev.3", prerelease=True),
        Item("1.2.0"),
        Item("1.1.0"),
    ]
    assert [r.version for r in offered(releases, prereleases=False)] == ["1.2.0", "1.1.5", "1.1.0"]
    assert [r.version for r in offered(releases, prereleases=True)] == [
        "1.2.0",
        "1.2.0-dev.10",
        "1.2.0-dev.9",
        "1.2.0-dev.3",
        "1.1.5",
        "1.1.0",
    ]
