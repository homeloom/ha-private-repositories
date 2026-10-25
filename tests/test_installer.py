"""Unpacking releases and swapping integrations in place."""

from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import stat
import tarfile
import threading
from unittest.mock import patch
import zipfile

import pytest

from custom_components.private_repositories import installer
from custom_components.private_repositories.installer import (
    InstallError,
    InstallWriteError,
    Layout,
    NoSpaceError,
)

from .conftest import integration_files, manifest, tarball, zipball


def unpack(data: bytes, tmp_path: Path, layout: Layout | None = None, **kwargs) -> installer.Package:
    work = tmp_path / "unpack"
    work.mkdir(parents=True, exist_ok=True)
    return installer.unpack(data, work, layout or Layout(), is_zip=kwargs.pop("is_zip", False), **kwargs)


def test_unpack_source_archive(tmp_path: Path) -> None:
    package = unpack(tarball(integration_files()), tmp_path)
    assert (package.domain, package.name, package.version) == ("gizmo", "Gizmo", "1.0.0")
    assert sorted(p.name for p in package.path.iterdir()) == ["__init__.py", "manifest.json", "translations"]
    assert (package.path / "translations" / "en.json").read_bytes() == b"{}"
    # Nothing but the integration is unpacked.
    assert not list(tmp_path.rglob("README.md"))


def test_unpack_picks_the_expected_integration(tmp_path: Path) -> None:
    files = integration_files("one") | integration_files("two")
    with pytest.raises(InstallError, match="Several integrations"):
        unpack(tarball(files), tmp_path)
    assert unpack(tarball(files), tmp_path, domain="two").domain == "two"


def test_unpack_without_integration(tmp_path: Path) -> None:
    with pytest.raises(InstallError, match="No integration found"):
        unpack(tarball({"README.md": b"hello"}), tmp_path)


def test_unpack_content_in_root(tmp_path: Path) -> None:
    files = {"manifest.json": manifest("rooted"), "__init__.py": b"", "README.md": b"# Rooted"}
    package = unpack(tarball(files), tmp_path, Layout(content_in_root=True))
    assert package.domain == "rooted"
    assert (package.path / "__init__.py").is_file()


def test_unpack_content_in_root_needs_a_manifest(tmp_path: Path) -> None:
    with pytest.raises(InstallError, match=r"no manifest\.json at the root"):
        unpack(tarball(integration_files()), tmp_path, Layout(content_in_root=True))


def test_unpack_zip_asset(tmp_path: Path) -> None:
    data = zipball(
        {"manifest.json": manifest("zipped", "2.0.0"), "sensor.py": b"", "translations/en.json": b"{}"}
    )
    package = unpack(data, tmp_path, Layout(zip_release=True, filename="zipped.zip"), is_zip=True)
    assert (package.domain, package.version) == ("zipped", "2.0.0")
    assert (package.path / "translations" / "en.json").is_file()


def test_unpack_zip_with_custom_components(tmp_path: Path) -> None:
    data = zipball(integration_files("nested"))
    assert unpack(data, tmp_path, is_zip=True).domain == "nested"


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_unpack_reads_stored_and_deflated_zips(tmp_path: Path, compression: int) -> None:
    data = zipball(integration_files(), compression=compression)
    assert unpack(data, tmp_path, is_zip=True).domain == "gizmo"


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_unpack_refuses_zips_it_cannot_read_by_parts(tmp_path: Path, compression: int) -> None:
    """zipfile decompresses a bzip2 or lzma member whole on its first read."""
    files = integration_files() | {"README.md": b"x" * 1000}
    data = zipball(files, compression=compression)
    with (
        patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("read")),
        pytest.raises(InstallError, match=r"compression \('custom_components/gizmo/manifest\.json'\)"),
    ):
        unpack(data, tmp_path, is_zip=True)
    assert not (tmp_path / "unpack" / "integration").exists()


def test_unpack_takes_the_integration_next_to_an_app(tmp_path: Path) -> None:
    """A repository can also hold a Supervisor app, with its own config.yaml, and
    the repository.yaml that lists it: only the integration is installed."""
    files = integration_files() | {
        "repository.yaml": b"name: Gizmo apps\nurl: https://example.com/gizmo\n",
        "gizmo_app/config.yaml": b"name: Gizmo app\nversion: 1.0.0\nslug: gizmo_app\n",
        "gizmo_app/Dockerfile": b"FROM scratch\n",
        "gizmo_app/translations/en.yaml": b"configuration: {}\n",
        "gizmo_app/rootfs/run.sh": b"#!/bin/sh\n",
        "hacs.json": b'{"name": "Gizmo", "homeassistant": "2025.6.1"}',
    }
    config = tmp_path / "config"
    package = unpack(tarball(files), tmp_path, Layout(homeassistant="2025.6.1"))
    assert (package.domain, package.version) == ("gizmo", "1.0.0")
    installer.install(package, config)
    target = config / "custom_components" / "gizmo"
    assert sorted(p.name for p in target.iterdir()) == ["__init__.py", "manifest.json", "translations"]
    assert sorted(p.name for p in (target / "translations").iterdir()) == ["en.json"]
    # Nothing of the app, or of the rest of the repository, was written anywhere:
    # neither in the work folder nor in the configuration.
    integration = {"__init__.py", "manifest.json", "translations/en.json"}
    written = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert written == {f"unpack/integration/{name}" for name in integration} | {
        f"config/custom_components/gizmo/{name}" for name in integration
    }


@pytest.mark.parametrize("name", ["../evil.py", "/etc/evil.py", "custom_components\\gizmo\\evil.py"])
def test_unpack_refuses_unsafe_paths(tmp_path: Path, name: str) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(InstallError, match="unsafe path"):
        unpack(buffer.getvalue(), tmp_path)


def test_unpack_refuses_unsafe_zip_paths(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", manifest())
        archive.writestr("../evil.py", b"x")
    with pytest.raises(InstallError, match="unsafe path"):
        unpack(buffer.getvalue(), tmp_path, is_zip=True)


def test_unpack_refuses_links(tmp_path: Path) -> None:
    data = tarball(
        integration_files(), symlinks={"custom_components/gizmo/secrets.yaml": "../../secrets.yaml"}
    )
    with pytest.raises(InstallError, match="link"):
        unpack(data, tmp_path)


def test_unpack_refuses_zip_links(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", manifest())
        link = zipfile.ZipInfo("link.py")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "/etc/passwd")
    with pytest.raises(InstallError, match="link"):
        unpack(buffer.getvalue(), tmp_path, is_zip=True)


def test_unpack_ignores_links_outside_the_integration(tmp_path: Path) -> None:
    data = tarball(integration_files(), symlinks={"docs/link": "../README.md"})
    assert unpack(data, tmp_path).domain == "gizmo"


def test_unpack_refuses_garbage(tmp_path: Path) -> None:
    with pytest.raises(InstallError, match="cannot be read"):
        unpack(b"not an archive", tmp_path)
    with pytest.raises(InstallError, match="cannot be read"):
        unpack(b"not a zip", tmp_path, is_zip=True)


def test_unpack_refuses_huge_archives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "MAX_UNPACKED_SIZE", 10)
    with pytest.raises(InstallError, match="too large"):
        unpack(tarball(integration_files()), tmp_path)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"{", "cannot be read"),
        (b"[]", "not a JSON object"),
        (json.dumps({"domain": "Bad-Domain", "version": "1"}).encode(), "no valid domain"),
        (json.dumps({"domain": "bad__domain", "name": "Bad", "version": "1"}).encode(), "no valid domain"),
        (json.dumps({"domain": "gizmo", "version": "1"}).encode(), "no name"),
        (json.dumps({"domain": "gizmo", "name": "Gizmo"}).encode(), "no valid version"),
        (json.dumps({"domain": "gizmo", "name": "Gizmo", "version": "nightly"}).encode(), "no valid version"),
    ],
)
def test_unpack_checks_the_manifest(tmp_path: Path, content: bytes, error: str) -> None:
    with pytest.raises(InstallError, match=error):
        unpack(tarball({"custom_components/gizmo/manifest.json": content}), tmp_path)


def test_unpack_checks_folder_and_domain(tmp_path: Path) -> None:
    files = {"custom_components/folder/manifest.json": manifest("other")}
    with pytest.raises(InstallError, match="holds the integration other"):
        unpack(tarball(files), tmp_path)


def test_unpack_checks_the_expected_domain(tmp_path: Path) -> None:
    data = zipball({"manifest.json": manifest("other")})
    with pytest.raises(InstallError, match="not gizmo"):
        unpack(data, tmp_path, is_zip=True, domain="gizmo")


@pytest.mark.parametrize(
    ("raw", "layout"),
    [
        (None, Layout()),
        (b'{"name": "Gizmo"}', Layout()),
        (
            b'{"content_in_root": true, "zip_release": true, "filename": "gizmo.zip",'
            b' "persistent_directory": "data", "homeassistant": "2025.1.0"}',
            Layout(True, True, "gizmo.zip", "data", "2025.1.0"),
        ),
        (b'{"homeassistant": " 2025.1.0 "}', Layout(homeassistant="2025.1.0")),
        # A blank minimum is no minimum.
        (b'{"homeassistant": ""}', Layout()),
        (b'{"homeassistant": "  "}', Layout()),
    ],
)
def test_parse_layout(raw: bytes | None, layout: Layout) -> None:
    assert installer.parse_layout(raw) == layout


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        b'{"filename": "a/b.zip"}',
        b'{"persistent_directory": "../x"}',
        b'{"persistent_directory": "/x"}',
        b'{"persistent_directory": "."}',
        b'{"persistent_directory": ""}',
        b'{"persistent_directory": "a\\\\b"}',
        b'{"persistent_directory": 5}',
        b'{"persistent_directory": true}',
        b'{"persistent_directory": {"path": "data"}}',
        b'{"persistent_directory": "da\\nta"}',
        b'{"filename": ""}',
        b'{"filename": 5}',
        b'{"filename": "a\\nb.zip"}',
        b'{"filename": "' + b"a" * 256 + b'.zip"}',
        b"[" * 100_000,
        b'{"homeassistant": 2025}',
        b'{"homeassistant": "2025.1.0\\n"}',
        b'{"homeassistant": "' + b"9" * 65 + b'"}',
        b'{"filename": "giz\\ud800mo.zip"}',
        b'{"zip_release": true}',
        b'{"zip_release": "true", "filename": "gizmo.zip"}',
        b'{"zip_release": null, "filename": "gizmo.zip"}',
        b'{"content_in_root": 1}',
        b'{"content_in_root": "false"}',
    ],
)
def test_parse_layout_refuses(raw: bytes) -> None:
    with pytest.raises(InstallError, match=r"hacs\.json"):
        installer.parse_layout(raw)


def test_install_and_replace(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (tmp_path / "a").mkdir()
    first = installer.unpack(
        tarball(integration_files(version="1.0.0")), tmp_path / "a", Layout(), is_zip=False
    )
    installer.install(first, config)
    assert installer.installed_version(config, "gizmo") == "1.0.0"

    (tmp_path / "b").mkdir()
    files = integration_files(version="2.0.0")
    del files["custom_components/gizmo/__init__.py"]
    files["custom_components/gizmo/new.py"] = b""
    second = installer.unpack(tarball(files), tmp_path / "b", Layout(), is_zip=False)
    installer.install(second, config)
    target = config / "custom_components" / "gizmo"
    assert installer.installed_version(config, "gizmo") == "2.0.0"
    # The folder is replaced, not merged.
    assert sorted(p.name for p in target.iterdir()) == ["manifest.json", "new.py", "translations"]
    # Nothing is left behind in the work folder.
    assert list(installer.work_dir(config).iterdir()) == []


def test_install_keeps_the_persistent_directory(tmp_path: Path) -> None:
    config = tmp_path / "config"
    data = config / "custom_components" / "gizmo" / "data"
    data.mkdir(parents=True)
    (data / "state.json").write_text("kept")
    package = unpack(tarball(integration_files()), tmp_path, Layout(persistent_directory="data"))
    installer.install(package, config)
    assert (data / "state.json").read_text() == "kept"
    assert installer.installed_version(config, "gizmo") == "1.0.0"


def test_install_failure_keeps_the_old_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config"
    old = config / "custom_components" / "gizmo"
    old.mkdir(parents=True)
    (old / "manifest.json").write_bytes(manifest(version="0.9.0"))
    package = unpack(tarball(integration_files()), tmp_path)

    real_rename = Path.rename

    def rename(self: Path, target: Path) -> Path:
        if self.name == "gizmo.new":
            raise OSError(28, "No space left on device")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(InstallWriteError, match="No space left"):
        installer.install(package, config)
    assert installer.installed_version(config, "gizmo") == "0.9.0"
    assert list(installer.work_dir(config).iterdir()) == []


def test_install_refuses_when_the_disk_is_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = unpack(tarball(integration_files()), tmp_path)
    (tmp_path / "config").mkdir()
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(
        installer.shutil, "disk_usage", lambda path: usage._replace(free=installer.MIN_FREE_SPACE - 1)
    )
    with pytest.raises(NoSpaceError):
        installer.install(package, tmp_path / "config")
    assert installer.installed_version(tmp_path / "config", "gizmo") is None


def test_recover_an_interrupted_install(tmp_path: Path) -> None:
    config = tmp_path / "config"
    work = installer.work_dir(config)
    (work / "gizmo.previous").mkdir(parents=True)
    (work / "gizmo.previous" / "manifest.json").write_bytes(manifest(version="0.9.0"))
    (work / "gizmo.new").mkdir()
    (work / "unpack-123").mkdir()
    assert installer.recover(config) == (["gizmo"], [])
    assert installer.installed_version(config, "gizmo") == "0.9.0"
    assert list(work.iterdir()) == []


def test_recover_keeps_a_finished_install(tmp_path: Path) -> None:
    config = tmp_path / "config"
    (installer.work_dir(config) / "gizmo.previous").mkdir(parents=True)
    installed = config / "custom_components" / "gizmo"
    installed.mkdir(parents=True)
    (installed / "manifest.json").write_bytes(manifest(version="2.0.0"))
    assert installer.recover(config) == ([], [])
    assert installer.installed_version(config, "gizmo") == "2.0.0"
    assert not (installer.work_dir(config) / "gizmo.previous").exists()


def test_installed_version(tmp_path: Path) -> None:
    assert installer.installed_version(tmp_path, "gizmo") is None
    folder = tmp_path / "custom_components" / "gizmo"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text("{")
    assert installer.installed_version(tmp_path, "gizmo") is None
    (folder / "manifest.json").write_bytes(manifest(version=" 1.2.3 "))
    assert installer.installed_version(tmp_path, "gizmo") == "1.2.3"
    # Read on every refresh: a manifest no integration needs is not read.
    padded = {"domain": "gizmo", "name": "Gizmo", "version": "1.2.3", "x": "a" * installer.MAX_MANIFEST_SIZE}
    (folder / "manifest.json").write_text(json.dumps(padded))
    assert installer.installed_version(tmp_path, "gizmo") is None


def _tar(*members: tuple[str, bytes]) -> bytes:
    """A tar archive with exactly these members, duplicates included."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_unpack_refuses_duplicate_members(tmp_path: Path) -> None:
    data = _tar(
        ("custom_components/gizmo/manifest.json", manifest()),
        ("custom_components/gizmo/__init__.py", b""),
        ("custom_components/gizmo/__init__.py", b"again"),
    )
    with pytest.raises(InstallError, match="twice"):
        unpack(data, tmp_path)


def test_unpack_refuses_control_characters(tmp_path: Path) -> None:
    data = _tar(
        ("custom_components/gizmo/manifest.json", manifest()), ("custom_components/gizmo/a\nb.py", b"")
    )
    with pytest.raises(InstallError, match="unsafe path"):
        unpack(data, tmp_path)


def test_unpack_counts_tar_members_as_they_come(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "MAX_MEMBERS", 2)
    with pytest.raises(InstallError, match="too many files"):
        unpack(tarball(integration_files()), tmp_path)


@pytest.mark.parametrize("is_zip", [False, True])
def test_unpack_refuses_huge_source_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, is_zip: bool
) -> None:
    """Everything counts, not only the integration: skipping a member costs time too."""
    files = integration_files() | {"big.bin": b"x" * 100}
    monkeypatch.setattr(installer, "MAX_ARCHIVE_SIZE", sum(len(data) for data in files.values()) - 1)
    data = zipball(files) if is_zip else tarball(files)
    with pytest.raises(InstallError, match="The archive is too large once unpacked"):
        unpack(data, tmp_path, is_zip=is_zip)
    monkeypatch.setattr(installer, "MAX_ARCHIVE_SIZE", sum(len(data) for data in files.values()))
    assert unpack(data, tmp_path / "again", is_zip=is_zip).domain == "gizmo"


def test_unpack_counts_zip_members_before_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "MAX_MEMBERS", 3)
    with pytest.raises(InstallError, match="too many files"):
        unpack(zipball(integration_files()), tmp_path, is_zip=True)


@pytest.mark.parametrize("count", [b"\x01\x00", b"\xff\xff"])
def test_unpack_ignores_the_count_a_zip_announces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: bytes
) -> None:
    """zipfile reads every record whatever the end of the file says: the records
    themselves are counted."""
    data = bytearray(zipball(integration_files()))
    end = data.rfind(b"PK\x05\x06")
    data[end + 8 : end + 12] = count * 2
    monkeypatch.setattr(installer, "MAX_MEMBERS", 3)
    with (
        patch.object(installer.zipfile, "ZipFile", side_effect=AssertionError("parsed")),
        pytest.raises(InstallError, match="too many files"),
    ):
        unpack(bytes(data), tmp_path, is_zip=True)


def test_unpack_reports_a_corrupt_member(tmp_path: Path) -> None:
    files = {"manifest.json": manifest(), "__init__.py": b"original content"}
    data = zipball(files)
    # Same size, other bytes: the checksum no longer matches.
    data = data.replace(b"original content", b"tampered content", 1)
    with pytest.raises(InstallError, match="cannot be read"):
        unpack(data, tmp_path, is_zip=True)


def test_unpack_reports_disk_trouble_apart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_open = Path.open

    def open_(self: Path, mode: str = "r", *args, **kwargs):
        if mode == "xb":
            raise OSError(13, "Permission denied")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_)
    with pytest.raises(InstallWriteError, match="Permission denied"):
        unpack(tarball(integration_files()), tmp_path)


@pytest.mark.parametrize(("spare", "enough"), [(-1, False), (0, True)])
def test_unpack_needs_twice_the_size_of_the_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spare: int, enough: bool
) -> None:
    """Room for the unpacked files and for their copy at the swap, plus a margin."""
    files = integration_files()
    size = sum(len(data) for name, data in files.items() if name.startswith("custom_components/gizmo/"))
    usage = shutil.disk_usage(tmp_path)
    free = 2 * size + installer.MIN_FREE_SPACE + spare
    monkeypatch.setattr(installer.shutil, "disk_usage", lambda path: usage._replace(free=free))
    if enough:
        assert unpack(tarball(files), tmp_path).domain == "gizmo"
    else:
        with pytest.raises(NoSpaceError):
            unpack(tarball(files), tmp_path)


def test_unpack_reports_a_folder_it_cannot_make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def disk_usage(path: Path) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(installer.shutil, "disk_usage", disk_usage)
    with pytest.raises(InstallWriteError, match="Cannot unpack the integration: Input/output error"):
        unpack(tarball(integration_files()), tmp_path)


def test_unpack_refuses_a_file_that_is_also_a_folder(tmp_path: Path) -> None:
    """A broken archive, not a disk that cannot be written."""
    data = zipball({"manifest.json": manifest(), "data": b"x", "data/notes.txt": b"y"})
    with pytest.raises(InstallError, match="lists 'data' as a file and as a folder") as raised:
        unpack(data, tmp_path, is_zip=True)
    assert not isinstance(raised.value, InstallWriteError)


def test_unpack_refuses_a_huge_manifest(tmp_path: Path) -> None:
    padded = json.dumps({"domain": "gizmo", "name": "Gizmo", "version": "1.0.0", "x": "a" * (1 << 20)})
    with pytest.raises(InstallError, match=r"manifest\.json is larger than 1 MB"):
        unpack(zipball({"manifest.json": padded.encode()}), tmp_path, is_zip=True)


def test_install_restores_a_stale_previous_version_first(tmp_path: Path) -> None:
    """An install interrupted between its two renames, then another install."""
    config = tmp_path / "config"
    previous = installer.work_dir(config) / "gizmo.previous"
    previous.mkdir(parents=True)
    (previous / "manifest.json").write_bytes(manifest(version="0.9.0"))
    (previous / "data").mkdir()
    (previous / "data" / "state.json").write_text("kept")
    package = unpack(tarball(integration_files()), tmp_path, Layout(persistent_directory="data"))
    installer.install(package, config)
    target = config / "custom_components" / "gizmo"
    assert installer.installed_version(config, "gizmo") == "1.0.0"
    assert (target / "data" / "state.json").read_text() == "kept"


def test_install_does_not_follow_a_linked_persistent_directory(tmp_path: Path) -> None:
    config = tmp_path / "config"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    target = config / "custom_components" / "gizmo"
    target.mkdir(parents=True)
    (target / "data").symlink_to(outside)
    package = unpack(tarball(integration_files()), tmp_path, Layout(persistent_directory="data"))
    installer.install(package, config)
    assert not (target / "data").exists()


def test_recover_without_a_work_folder(tmp_path: Path) -> None:
    assert installer.recover(tmp_path) == ([], [])


def test_recover_reports_what_it_cannot_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config"
    work = installer.work_dir(config)
    (work / "gizmo.previous").mkdir(parents=True)
    (work / "other.new").mkdir()
    real_remove = installer._remove
    real_rename = Path.rename

    def remove(path: Path) -> None:
        if path.name == "other.new":
            raise OSError(16, "Device or resource busy")
        real_remove(path)

    def rename(self: Path, target: Path) -> Path:
        if self.name == "gizmo.previous":
            raise OSError(16, "Device or resource busy")
        return real_rename(self, target)

    monkeypatch.setattr(installer, "_remove", remove)
    monkeypatch.setattr(Path, "rename", rename)
    assert installer.recover(config) == ([], ["gizmo.previous", "other.new"])
    # The only copy of the integration is never removed.
    assert (work / "gizmo.previous").is_dir()


@pytest.mark.parametrize(
    "error", [OSError(16, "Device or resource busy"), SystemExit()], ids=["busy", "stopped"]
)
def test_recover_never_puts_back_a_previous_version_it_began_to_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: BaseException
) -> None:
    """Part of a previous version must never come back, even once the
    integration's folder is deleted by hand: not when a file cannot be
    removed, not when Home Assistant stops halfway."""
    config = tmp_path / "config"
    work = installer.work_dir(config)
    (work / "gizmo.previous").mkdir(parents=True)
    (work / "gizmo.previous" / "manifest.json").write_bytes(manifest(version="0.9.0"))
    installed = config / "custom_components" / "gizmo"
    installed.mkdir()
    (installed / "manifest.json").write_bytes(manifest(version="1.0.0"))
    real_remove = installer._remove

    def remove(path: Path) -> None:
        if path.name.startswith("gizmo.previous"):
            (path / "manifest.json").unlink()
            raise error
        real_remove(path)

    monkeypatch.setattr(installer, "_remove", remove)
    if isinstance(error, OSError):
        restored, failed = installer.recover(config)
        assert restored == []
        assert [name.split("-")[0] for name in failed] == ["gizmo.previous.stale"]
        assert "moved to gizmo.previous.stale-" in caplog.text
    else:
        with pytest.raises(SystemExit):
            installer.recover(config)
    assert [entry.name.split("-")[0] for entry in work.iterdir()] == ["gizmo.previous.stale"]
    shutil.rmtree(installed)
    monkeypatch.setattr(installer, "_remove", real_remove)
    assert installer.recover(config) == ([], [])
    assert not installed.exists()
    assert list(work.iterdir()) == []


def test_unpack_refuses_a_deeply_nested_manifest(tmp_path: Path) -> None:
    files = {"custom_components/gizmo/manifest.json": b"[" * 100_000}
    with pytest.raises(InstallError, match="cannot be read"):
        unpack(tarball(files), tmp_path)


def test_unpack_puts_the_name_on_one_line(tmp_path: Path) -> None:
    package = unpack(tarball(integration_files(name="  Gizmo\n\tPro  ")), tmp_path)
    assert package.name == "Gizmo Pro"
    package = unpack(tarball(integration_files(name="G" * 500)), tmp_path / "long")
    assert package.name == "G" * installer.MAX_NAME_LENGTH


def test_shown_quotes_and_cuts() -> None:
    assert installer.shown("a\nb") == "'a\\nb'"
    assert len(installer.shown("x" * 1000)) == 202


def test_unpack_reports_errors_when_a_file_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Buffered data reaches the disk when the file is closed: that can fail too."""
    real_open = Path.open

    class Output(io.BytesIO):
        failed = False

        def close(self) -> None:
            super().close()
            if not self.failed:
                self.failed = True
                raise OSError(28, "No space left on device")

    def open_(self: Path, mode: str = "r", *args, **kwargs):
        if mode == "xb":
            return Output()
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_)
    with pytest.raises(InstallWriteError, match="No space left"):
        unpack(tarball(integration_files()), tmp_path)


def test_describe_copy_errors() -> None:
    failures = [("a", "b", "[Errno 13] Permission denied: 'b'")]
    assert installer._describe(shutil.Error(failures)) == "[Errno 13] Permission denied: 'b'"
    assert installer._describe(shutil.Error("odd")) == "some files could not be copied"
    assert installer._describe(OSError(28, "No space left on device")) == "No space left on device"
    assert installer._describe(OSError()) == "OSError"


def test_install_reports_why_files_could_not_be_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = unpack(tarball(integration_files()), tmp_path)

    def copytree(*args, **kwargs):
        raise shutil.Error([("src", "dst", "[Errno 13] Permission denied: 'dst'")])

    monkeypatch.setattr(installer.shutil, "copytree", copytree)
    with pytest.raises(InstallWriteError, match="Permission denied"):
        installer.install(package, tmp_path / "config")
    assert installer.installed_version(tmp_path / "config", "gizmo") is None


def test_install_moves_aside_what_it_cannot_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A previous version that cannot be removed never blocks the next install."""
    config = tmp_path / "config"
    work = installer.work_dir(config)
    (work / "gizmo.previous").mkdir(parents=True)
    installed = config / "custom_components" / "gizmo"
    installed.mkdir(parents=True)
    (installed / "manifest.json").write_bytes(manifest(version="0.9.0"))
    real_remove = installer._remove

    def remove(path: Path) -> None:
        if path.name.startswith("gizmo.previous.stale-"):
            raise OSError(16, "Device or resource busy")
        real_remove(path)

    monkeypatch.setattr(installer, "_remove", remove)
    installer.install(unpack(tarball(integration_files()), tmp_path), config)
    assert installer.installed_version(config, "gizmo") == "1.0.0"
    # The stale leftover before the swap, the replaced version after it: neither
    # keeps the name recover() would put back.
    assert [entry.name.split("-")[0] for entry in work.iterdir()] == ["gizmo.previous.stale"] * 2
    assert caplog.text.count("moved to gizmo.previous.stale-") == 2

    # The next start removes both: the new version is in place.
    monkeypatch.setattr(installer, "_remove", real_remove)
    assert installer.recover(config) == ([], [])
    assert list(work.iterdir()) == []
    assert installer.installed_version(config, "gizmo") == "1.0.0"


def test_is_occupied(tmp_path: Path) -> None:
    folder = tmp_path / "custom_components" / "gizmo"
    assert not installer.is_occupied(tmp_path, "gizmo")
    (folder / "__pycache__").mkdir(parents=True)
    assert not installer.is_occupied(tmp_path, "gizmo")
    (folder / "notes.txt").write_text("")
    assert installer.is_occupied(tmp_path, "gizmo")
    shutil.rmtree(folder)
    folder.write_text("")
    assert installer.is_occupied(tmp_path, "gizmo")
    folder.unlink()
    folder.symlink_to(tmp_path / "missing")
    assert installer.is_occupied(tmp_path, "gizmo")
    # A folder that cannot be listed may hold anything.
    folder.unlink()
    folder.mkdir()
    with patch.object(Path, "iterdir", side_effect=PermissionError(13, "Permission denied")):
        assert installer.is_occupied(tmp_path, "gizmo")


def test_unpack_survives_deep_json_in_a_small_thread(tmp_path: Path) -> None:
    """Home Assistant's container images give executor threads a small stack:
    deeply nested JSON must not crash the whole process."""
    errors: list[str] = []

    def run() -> None:
        for raw in (b"[" * 100_000, b'{"a":' * 100_000):
            with pytest.raises(InstallError, match="not valid JSON") as err:
                installer.parse_layout(raw)
            errors.append(str(err.value))
        files = {"custom_components/gizmo/manifest.json": b"[" * 100_000}
        with pytest.raises(InstallError, match="cannot be read") as err:
            unpack(tarball(files), tmp_path)
        errors.append(str(err.value))

    size = threading.stack_size(1 << 20)
    try:
        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
    finally:
        threading.stack_size(size)
    assert len(errors) == 3


def test_unpack_refuses_text_that_cannot_be_saved(tmp_path: Path) -> None:
    """A lone surrogate would stop Home Assistant from saving its registries."""
    files = {"custom_components/gizmo/manifest.json": manifest(name="Giz\ud800mo")}
    assert b"\\ud800" in files["custom_components/gizmo/manifest.json"]
    with pytest.raises(InstallError, match="cannot be read"):
        unpack(tarball(files), tmp_path)


def test_unpack_reads_the_version_as_installed_version_does(tmp_path: Path) -> None:
    package = unpack(tarball(integration_files(version=" 1.0.0\n")), tmp_path)
    assert package.version == "1.0.0"


def test_remove_opens_up_read_only_folders(tmp_path: Path) -> None:
    """Folders an integration made read-only, or closed, are removed with the
    rest. As root nothing is refused anyway: CI runs these as a plain user."""
    root = tmp_path / "gizmo.previous"
    (root / "data" / "ro" / "closed").mkdir(parents=True)
    (root / "data" / "ro" / "state.json").write_text("{}")
    (root / "data" / "ro" / "closed" / "file").write_text("")
    (root / "data" / "listed" / "sub").mkdir(parents=True)
    (root / "data" / "hidden").mkdir()
    (root / "data" / "hidden" / "secret").write_text("")
    (root / "data" / "ro" / "closed").chmod(0)
    (root / "data" / "ro").chmod(0o555)
    (root / "data" / "listed").chmod(0o444)
    (root / "data" / "hidden").chmod(0)
    installer._remove(root)
    assert not root.exists()


def test_remove_tries_once_more_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder that stays closed after it was opened up is an error, not a loop."""
    root = tmp_path / "gizmo.previous"
    (root / "data").mkdir(parents=True)
    calls: list[object] = []

    def rmtree(path: Path) -> None:
        calls.append(path)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(installer.shutil, "rmtree", rmtree)
    monkeypatch.setattr(installer, "_open_up", lambda path: calls.append(("open", path)))
    with pytest.raises(PermissionError):
        installer._remove(root)
    assert calls == [root, ("open", root), root]
    # Other errors are not retried.
    calls.clear()

    def busy(path: Path) -> None:
        calls.append(path)
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(installer.shutil, "rmtree", busy)
    with pytest.raises(OSError, match="busy"):
        installer._remove(root)
    assert calls == [root]


def test_open_up(tmp_path: Path) -> None:
    mode = tmp_path.stat().st_mode
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o555)
    root = tmp_path / "gizmo.previous"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "a" / "file").write_text("")
    (root / "link").symlink_to(outside)
    for folder in (root / "a" / "b" / "c", root / "a" / "b", root / "a", root):
        folder.chmod(0)
    installer._open_up(root)
    for folder in (root, root / "a", root / "a" / "b", root / "a" / "b" / "c"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    # Files, links and what they point to are left alone; so is a missing tree.
    assert stat.S_IMODE((root / "a" / "file").stat().st_mode) != 0o700
    assert stat.S_IMODE(outside.stat().st_mode) == 0o555
    installer._open_up(tmp_path / "missing")
    assert tmp_path.stat().st_mode == mode


def test_install_ends_even_if_the_old_version_cannot_be_moved_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The new version is in place: what is left of the old one is for recover()."""
    config = tmp_path / "config"
    installed = config / "custom_components" / "gizmo"
    installed.mkdir(parents=True)
    (installed / "manifest.json").write_bytes(manifest(version="0.9.0"))
    real_discard = installer._discard
    calls: list[Path] = []

    def discard(path: Path) -> str | None:
        calls.append(path)
        if len(calls) == 2:
            raise OSError(16, "Device or resource busy")
        return real_discard(path)

    monkeypatch.setattr(installer, "_discard", discard)
    installer.install(unpack(tarball(integration_files()), tmp_path), config)
    assert installer.installed_version(config, "gizmo") == "1.0.0"
    assert "Cannot remove the previous version of gizmo" in caplog.text
    assert installer.recover(config) == ([], [])
    assert installer.installed_version(config, "gizmo") == "1.0.0"
