"""Every translation says the same things as the English one."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import re
from typing import Any

import pytest

from custom_components.private_repositories import manager  # noqa: F401 - defines more reasons
from custom_components.private_repositories.github import GitHubError
from custom_components.private_repositories.installer import InstallError

TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "private_repositories" / "translations"
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
# Only the add dialog refuses integrations: the update action replaces the one
# it tracks.
ONLY_WHEN_ADDING = {"integration_tracked", "integration_refused", "integration_unreadable"}
# Only the update action names the integration it installs, before the release
# is read.
ONLY_WHEN_UPDATING = {"already_installing"}


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat |= _flatten(value, f"{prefix}{key}.")
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def _load(name: str) -> dict[str, str]:
    return _flatten(json.loads((TRANSLATIONS / name).read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "name", sorted(path.name for path in TRANSLATIONS.glob("*.json") if path.name != "en.json")
)
def test_same_keys_and_placeholders(name: str) -> None:
    english = _load("en.json")
    other = _load(name)
    assert sorted(other) == sorted(english)
    for key, text in english.items():
        assert set(PLACEHOLDER_RE.findall(other[key])) == set(PLACEHOLDER_RE.findall(text)), key


def test_no_empty_text() -> None:
    for path in TRANSLATIONS.glob("*.json"):
        assert all(text.strip() for text in _load(path.name).values()), path.name


def _subclasses(cls: type) -> list[type]:
    return [sub for direct in cls.__subclasses__() for sub in [direct, *_subclasses(direct)]]


def _reasons() -> list[Exception]:
    """An instance of every expected outcome that has texts of its own."""
    errors = []
    for cls in _subclasses(InstallError) + _subclasses(GitHubError):
        if cls.reason is not None:
            arguments = [name for name in inspect.signature(cls).parameters if name != "self"]
            errors.append(cls(*(f"<{name}>" for name in arguments)))
    return errors


@pytest.mark.parametrize("err", _reasons(), ids=lambda err: err.reason)
def test_every_reason_has_its_texts(err: InstallError | GitHubError) -> None:
    """The dialog and the update action each explain the expected outcomes
    they can meet, and have no text for the others."""
    english = _load("en.json")
    dialog = english.get(f"config_subentries.repository.error.{err.reason}")
    action = english.get(f"exceptions.install_{err.reason}.message")
    assert (dialog is None) is (err.reason in ONLY_WHEN_UPDATING)
    assert (action is None) is (err.reason in ONLY_WHEN_ADDING)
    if dialog is not None:
        assert set(PLACEHOLDER_RE.findall(dialog)) <= {"error", "integration", *err.placeholders}
    if action is not None:
        assert set(PLACEHOLDER_RE.findall(action)) <= {"name", "tag", "error", *err.placeholders}


def test_repository_names_are_code_in_markdown() -> None:
    """A repository may be named __init__: dialog texts, which are Markdown, show
    it as code. Titles are plain text."""
    for path in TRANSLATIONS.glob("*.json"):
        for key, text in _load(path.name).items():
            if key.startswith("config_subentries.repository.") and not key.endswith(".title"):
                assert re.findall(r"(?<!`)\{repository\}|\{repository\}(?!`)", text) == [], (path.name, key)
