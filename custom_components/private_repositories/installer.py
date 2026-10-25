"""Unpacking a release and putting an integration in place.

Nothing here depends on Home Assistant: these functions do blocking file I/O and
run in an executor thread.

An install never leaves custom_components/<domain> half written. The new version
is prepared in a work folder, written to disk, and swapped in with two renames.
If Home Assistant stops between the two renames, recover() puts the previous
version back on the next start.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import io
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import time
from typing import IO, Any, cast
import zipfile
import zlib

import orjson

from .const import LOGGER, WORK_DIR
from .versions import is_valid

MAX_ARCHIVE_SIZE = 1024 * 1024 * 1024  # everything in the archive, once unpacked
MAX_UNPACKED_SIZE = 200 * 1024 * 1024  # the integration's own files
MAX_MEMBERS = 10_000
MIN_FREE_SPACE = 50 * 1024 * 1024
# Real manifests take a few hundred bytes, and installed ones are read on every
# refresh: a larger one is refused, like a larger hacs.json.
MAX_MANIFEST_SIZE = 1024 * 1024
MAX_NAME_LENGTH = 100
# Every central directory record of a zip file starts with this signature.
ZIP_RECORD = b"PK\x01\x02"
CHUNK_SIZE = 64 * 1024

# Home Assistant's rule for domains: no leading, trailing or double underscore.
DOMAIN_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
IGNORED = shutil.ignore_patterns("__pycache__")
# What reading a broken archive can raise, besides TarError and BadZipFile.
ARCHIVE_ERRORS = (
    tarfile.TarError,
    zipfile.BadZipFile,
    EOFError,
    OSError,
    zlib.error,
    lzma.LZMAError,
    ValueError,
    RuntimeError,
    NotImplementedError,
)


class InstallError(Exception):
    """An install was refused. The message is safe to show."""

    # An expected outcome, with texts of its own in each language.
    reason: str | None = None

    @property
    def placeholders(self) -> dict[str, str]:
        """What those texts show."""
        return {}


class InstallWriteError(InstallError):
    """The configuration folder could not be written: nothing was changed."""


class NoSpaceError(InstallWriteError):
    """Not enough free space to install the integration."""

    reason = "no_space"

    def __init__(self) -> None:
        super().__init__("Not enough free space to install it")


@dataclass(frozen=True, slots=True)
class Layout:
    """How a repository ships its integration (its hacs.json, if any)."""

    content_in_root: bool = False
    zip_release: bool = False
    filename: str | None = None
    persistent_directory: str | None = None
    homeassistant: str | None = None


@dataclass(frozen=True, slots=True)
class Package:
    """An integration unpacked from a release, ready to install."""

    domain: str
    name: str
    version: str
    path: Path
    persistent_directory: str | None = None


@dataclass(frozen=True, slots=True)
class _Member:
    """A file or folder of an archive, relative to the integration."""

    path: PurePosixPath
    is_dir: bool
    size: int
    open: Callable[[], IO[bytes]]


def shown(value: object) -> str:
    """A value from a release, quoted and cut short for messages and logs."""
    return repr(str(value)[:200])


def _describe(err: OSError) -> str:
    if isinstance(err, shutil.Error):
        # copytree gathers (source, destination, reason) for each file it could not copy.
        failures = err.args[0] if err.args and isinstance(err.args[0], list) else []
        return str(failures[0][2]) if failures else "some files could not be copied"
    return err.strerror or type(err).__name__


def _load_json(raw: bytes) -> Any:
    """JSON from a release, parsed as Home Assistant parses manifests.

    orjson limits nesting whatever the thread's stack (the json module can crash
    a small executor thread), and refuses text that cannot be saved as UTF-8.
    Its errors are ValueErrors.
    """
    return orjson.loads(raw)


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _relative(value: object, what: str) -> str | None:
    """A relative path inside the integration, or None."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or not (path := PurePosixPath(value)).parts
        or path.is_absolute()
        or ".." in path.parts
    ):
        raise InstallError(f"hacs.json: invalid {what} ({shown(value)})")
    return path.as_posix()


def parse_layout(raw: bytes | None) -> Layout:
    """The layout described by a hacs.json file (None when the repository has none)."""
    if raw is None:
        return Layout()
    try:
        data = _load_json(raw)
    except ValueError as err:
        raise InstallError("hacs.json is not valid JSON") from err
    if not isinstance(data, dict):
        raise InstallError("hacs.json is not a JSON object")
    filename = data.get("filename")
    if filename is not None and (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 255
        or "/" in filename
        or any(ord(c) < 32 for c in filename)
    ):
        raise InstallError(f"hacs.json: invalid filename ({shown(filename)})")
    flags = {key: data.get(key, False) for key in ("content_in_root", "zip_release")}
    for key, value in flags.items():
        if not isinstance(value, bool):
            raise InstallError(f"hacs.json: {key} must be true or false ({shown(value)})")
    if flags["zip_release"] and filename is None:
        raise InstallError("hacs.json: zip_release needs the filename of the zip file")
    homeassistant = data.get("homeassistant")
    if isinstance(homeassistant, str) and not homeassistant.strip():
        homeassistant = None  # A blank minimum is no minimum.
    if homeassistant is not None and (
        not isinstance(homeassistant, str)
        or len(homeassistant) > 64
        or any(ord(c) < 32 for c in homeassistant)
    ):
        raise InstallError(f"hacs.json: invalid homeassistant ({shown(homeassistant)})")
    return Layout(
        content_in_root=flags["content_in_root"],
        zip_release=flags["zip_release"],
        filename=filename,
        persistent_directory=_relative(data.get("persistent_directory"), "persistent_directory"),
        homeassistant=homeassistant.strip() if homeassistant is not None else None,
    )


def _check_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or any(ord(c) < 32 for c in name):
        raise InstallError(f"The archive has an unsafe path ({shown(name)})")
    return path


def _pick(manifests: set[str], domain: str | None, where: str) -> str:
    """The folder of the integration among custom_components/<folder>/manifest.json."""
    if domain is not None and domain in manifests:
        return domain
    if not manifests:
        raise InstallError(f"No integration found in {where}")
    if len(manifests) > 1:
        raise InstallError(f"Several integrations found in {where} ({shown(', '.join(sorted(manifests)))})")
    return next(iter(manifests))


def _locate(
    files: set[PurePosixPath], root: PurePosixPath, content_in_root: bool, domain: str | None
) -> PurePosixPath:
    """Where the integration is in an archive, given the list of its files."""
    if content_in_root:
        if root / "manifest.json" not in files:
            raise InstallError("content_in_root is set, but there is no manifest.json at the root")
        return root
    if root / "manifest.json" in files and not any(
        f.parts[: len(root.parts) + 1] == (*root.parts, "custom_components") for f in files
    ):
        # A release asset holding the integration's files directly.
        return root
    base = root / "custom_components"
    manifests = {
        f.parts[len(base.parts)]
        for f in files
        if f.parts[: len(base.parts)] == base.parts
        and len(f.parts) == len(base.parts) + 2
        and f.name == "manifest.json"
    }
    return base / _pick(manifests, domain, "custom_components/")


@contextmanager
def _reading() -> Iterator[None]:
    """Report a broken archive as such."""
    try:
        yield
    except InstallError:
        raise
    except ARCHIVE_ERRORS as err:
        raise InstallError(f"The archive cannot be read ({type(err).__name__})") from err


def _select(
    entries: list[tuple[PurePosixPath, bool, bool, int, Callable[[], IO[bytes]]]],
    root: PurePosixPath,
    layout: Layout,
    domain: str | None,
) -> tuple[PurePosixPath, list[_Member]]:
    """The integration's folder in the archive, and its members.

    entries holds (path, is_file, is_dir, size, open) for every member.
    """
    if sum(size for _, _, _, size, _ in entries) > MAX_ARCHIVE_SIZE:
        raise InstallError("The archive is too large once unpacked")
    seen: set[PurePosixPath] = set()
    for path, _, _, _, _ in entries:
        if path in seen:
            raise InstallError(f"The archive lists {shown(path)} twice")
        seen.add(path)
    files = {path for path, is_file, *_ in entries if is_file}
    for path in seen:
        if parent := next((parent for parent in path.parents if parent in files), None):
            raise InstallError(f"The archive lists {shown(parent)} as a file and as a folder")
    source = _locate(files, root, layout.content_in_root, domain)
    members = []
    for path, is_file, is_dir, size, opener in entries:
        if path.parts[: len(source.parts)] != source.parts or path == source:
            continue
        if not (is_file or is_dir):
            raise InstallError(f"The integration contains a link or a special file ({shown(path)})")
        members.append(_Member(PurePosixPath(*path.parts[len(source.parts) :]), is_dir, size, opener))
    if sum(member.size for member in members) > MAX_UNPACKED_SIZE:
        raise InstallError("The integration is too large once unpacked")
    return source, members


def _extract(members: list[_Member], folder: Path) -> None:
    """Write the integration's files: archive errors and disk errors apart."""
    size = sum(member.size for member in members)
    try:
        folder.mkdir()
        if shutil.disk_usage(folder).free < 2 * size + MIN_FREE_SPACE:
            raise NoSpaceError
    except OSError as err:
        raise InstallWriteError(f"Cannot unpack the integration: {_describe(err)}") from err
    for member in members:
        target = folder.joinpath(*member.path.parts)
        try:
            if member.is_dir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # "x": never write through a file or link already there.
            output = target.open("xb")
        except OSError as err:
            raise InstallWriteError(f"Cannot unpack the integration: {_describe(err)}") from err
        try:
            # Archive errors become InstallError inside: an OSError here comes from
            # writing the file, or from closing it (buffered data reaches the disk then).
            with output:
                with _reading():
                    source = member.open()
                with source:
                    while True:
                        with _reading():
                            chunk = source.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        output.write(chunk)
        except OSError as err:
            raise InstallWriteError(f"Cannot unpack the integration: {_describe(err)}") from err


def _unpack_tar(data: bytes, folder: Path, layout: Layout, domain: str | None) -> PurePosixPath:
    with _reading(), tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        members: list[tarfile.TarInfo] = []
        total = 0
        for member in tar:
            members.append(member)
            total += member.size
            # Checked as headers are read: skipping a huge member costs time.
            if len(members) > MAX_MEMBERS:
                raise InstallError("The archive has too many files")
            if total > MAX_ARCHIVE_SIZE:
                raise InstallError("The archive is too large once unpacked")
        entries = [
            (
                _check_name(m.name),
                m.isfile(),
                m.isdir(),
                m.size,
                # Only regular files are opened, and extractfile() gives None for links only.
                lambda m=m: cast(IO[bytes], tar.extractfile(m)),
            )
            for m in members
        ]
        tops = {path.parts[0] for path, *_ in entries if path.parts}
        # GitHub source archives hold everything in one <owner>-<repo>-<sha>/ folder.
        root = PurePosixPath(tops.pop()) if len(tops) == 1 else PurePosixPath()
        source, selected = _select(entries, root, layout, domain)
        _extract(selected, folder)
    return source


def _unpack_zip(data: bytes, folder: Path, layout: Layout, domain: str | None) -> PurePosixPath:
    # zipfile reads every record of the central directory, whatever count the end
    # of the file announces: bound them before parsing. Signatures in file data can
    # only make this count larger.
    if data.count(ZIP_RECORD) > MAX_MEMBERS:
        raise InstallError("The archive has too many files")
    with _reading(), zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        for info in infos:
            # zipfile decompresses a bzip2 or lzma member whole on its first
            # read, whatever size it declares: only deflate reads a bounded amount.
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise InstallError(f"The archive uses an unsupported compression ({shown(info.filename)})")
        entries = [
            (
                _check_name(info.filename),
                not info.is_dir() and not stat.S_ISLNK(info.external_attr >> 16),
                info.is_dir(),
                info.file_size,
                lambda info=info: archive.open(info),
            )
            for info in infos
        ]
        source, selected = _select(entries, PurePosixPath(), layout, domain)
        _extract(selected, folder)
    return source


def _manifest_bytes(folder: Path) -> bytes | None:
    """The manifest.json of a folder, or None when it is over MAX_MANIFEST_SIZE."""
    with (folder / "manifest.json").open("rb") as file:
        raw = file.read(MAX_MANIFEST_SIZE + 1)
    return raw if len(raw) <= MAX_MANIFEST_SIZE else None


def read_manifest(folder: Path) -> dict[str, Any]:
    """An integration's manifest.json, checked for what Home Assistant requires."""
    try:
        raw = _manifest_bytes(folder)
        if raw is None:
            raise InstallError("manifest.json is larger than 1 MB")
        manifest = _load_json(raw)
    except (OSError, ValueError) as err:
        raise InstallError("manifest.json cannot be read") from err
    if not isinstance(manifest, dict):
        raise InstallError("manifest.json is not a JSON object")
    domain = manifest.get("domain")
    if not isinstance(domain, str) or not DOMAIN_RE.fullmatch(domain):
        raise InstallError(f"manifest.json has no valid domain ({shown(domain)})")
    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InstallError("manifest.json has no name")
    version = manifest.get("version")
    # Home Assistant refuses to load a custom integration without a valid version.
    if not isinstance(version, str) or not is_valid(version):
        raise InstallError(f"manifest.json has no valid version ({shown(version)})")
    return manifest


def unpack(data: bytes, workdir: Path, layout: Layout, *, is_zip: bool, domain: str | None = None) -> Package:
    """Unpack the integration of a release archive into a new folder of workdir.

    domain is the integration expected in this repository, when it is known.
    """
    folder = workdir / "integration"
    source = (_unpack_zip if is_zip else _unpack_tar)(data, folder, layout, domain)
    manifest = read_manifest(folder)
    found: str = manifest["domain"]
    if domain is not None and found != domain:
        raise InstallError(f"The release contains the integration {found}, not {domain}")
    if source.parent.name == "custom_components" and source.name != found:
        raise InstallError(f"custom_components/{shown(source.name)} holds the integration {found}")
    return Package(
        domain=found,
        # Shown in titles and messages: on one line, and not too long.
        name=" ".join(manifest["name"].split())[:MAX_NAME_LENGTH],
        # As installed_version() reads it back.
        version=manifest["version"].strip(),
        path=folder,
        persistent_directory=layout.persistent_directory,
    )


def installed_manifest(config_dir: Path, domain: str) -> dict[str, Any] | None:
    """The manifest of an integration in custom_components/, or None if absent or unreadable."""
    try:
        raw = _manifest_bytes(config_dir / "custom_components" / domain)
        manifest = _load_json(raw) if raw is not None else None
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def installed_version(config_dir: Path, domain: str) -> str | None:
    """The version of an integration in custom_components/, or None if absent or broken."""
    version = (installed_manifest(config_dir, domain) or {}).get("version")
    return version.strip() if isinstance(version, str) and version.strip() else None


def is_occupied(config_dir: Path, domain: str) -> bool:
    """Whether custom_components/<domain> holds anything but compiled files."""
    folder = config_dir / "custom_components" / domain
    try:
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            return True
        return folder.is_dir() and any(entry.name != "__pycache__" for entry in folder.iterdir())
    except OSError:
        return True


def _sync_dir(folder: Path) -> None:
    """Write a folder's entries (creations, renames) to disk."""
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree(folder: Path) -> None:
    """Write a whole folder to disk before it is swapped in: after a power cut, a
    renamed but unwritten file would come back empty."""
    for path in folder.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            _sync_dir(path)
        elif path.is_file():
            with path.open("rb") as file:
                os.fsync(file.fileno())
    _sync_dir(folder)


def _remove(path: Path) -> None:
    """Remove a file, a link or a whole folder (never what a link points to)."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        try:
            shutil.rmtree(path)
        except PermissionError:
            # A folder Home Assistant cannot read, enter or write in, such as a
            # read-only folder an integration kept in its persistent_directory:
            # open the whole tree up, then try once more.
            _open_up(path)
            shutil.rmtree(path)


def _open_up(root: Path) -> None:
    """Give the owner full access to every folder of a tree. Links are not
    followed, and folders Home Assistant cannot change are left as they are."""
    pending = [root]
    while pending:
        folder = pending.pop()
        with suppress(OSError):
            folder.chmod(stat.S_IRWXU)
        with suppress(OSError), os.scandir(folder) as entries:
            pending += [Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)]


def _discard(path: Path) -> str | None:
    """Move a leftover out of the way of the next install, then remove it.

    It is renamed first, so that a removal cut short (Home Assistant stopped,
    a file it cannot delete) never leaves part of it under its own name.
    Returns the name it keeps when it cannot be removed: recover() removes it
    at the next start, as anything in the work folder that does not end in
    ".previous".
    """
    if not _exists(path):
        return None
    aside = path.with_name(f"{path.name}.stale-{time.time_ns()}")
    path.rename(aside)
    try:
        _remove(aside)
    except OSError as err:
        LOGGER.warning("Cannot remove %s (%s): moved to %s", path, _describe(err), aside.name)
        return aside.name
    return None


def work_dir(config_dir: Path) -> Path:
    """Where installs are prepared."""
    return config_dir / "custom_components" / WORK_DIR


def recover(config_dir: Path) -> tuple[list[str], list[str]]:
    """Put back the integrations an interrupted install left aside, and clean up.

    Returns the domains that were put back, and the leftovers that could not be
    removed.
    """
    custom = config_dir / "custom_components"
    work = work_dir(config_dir)
    if not work.is_dir():
        return [], []
    restored: list[str] = []
    failed: list[str] = []
    for entry in sorted(work.iterdir()):
        if entry.name.endswith(".previous"):
            target = custom / entry.name.removesuffix(".previous")
            if not _exists(target):
                try:
                    entry.rename(target)
                except OSError:
                    # The only copy of that integration: kept for the next start.
                    failed.append(entry.name)
                else:
                    restored.append(target.name)
    for entry in sorted(work.iterdir()):
        if entry.name in failed:
            continue
        try:
            if not entry.name.endswith(".previous"):
                _remove(entry)
            # Not removed in place: what is left of it must not keep the name
            # the first loop puts back.
            elif aside := _discard(entry):
                failed.append(aside)
        except OSError:
            failed.append(entry.name)
    return restored, failed


def install(package: Package, config_dir: Path) -> None:
    """Put the package in custom_components/<domain>, replacing what is there."""
    try:
        _install(package, config_dir)
    except InstallError:
        raise
    except OSError as err:
        raise InstallWriteError(f"Cannot write custom_components/{package.domain}: {_describe(err)}") from err


def _install(package: Package, config_dir: Path) -> None:
    custom = config_dir / "custom_components"
    work = work_dir(config_dir)
    target = custom / package.domain
    staging = work / f"{package.domain}.new"
    previous = work / f"{package.domain}.previous"
    work.mkdir(parents=True, exist_ok=True)
    # An earlier install stopped between its two renames: start from a sane state.
    if _exists(previous) and not _exists(target):
        previous.rename(target)
    if shutil.disk_usage(work).free < MIN_FREE_SPACE:
        raise NoSpaceError
    _remove(staging)
    try:
        shutil.copytree(package.path, staging, ignore=IGNORED)
        if (
            (keep := package.persistent_directory)
            and (target / keep).is_dir()
            and not (target / keep).is_symlink()
        ):
            # Data the integration keeps in its own folder survives updates. It is
            # copied now: what the running version writes there until the restart
            # is not carried over.
            shutil.copytree(target / keep, staging / keep, dirs_exist_ok=True, symlinks=True)
        _sync_tree(staging)
        _discard(previous)
        if _exists(target):
            target.rename(previous)
        try:
            staging.rename(target)
        except OSError:
            if _exists(previous):
                previous.rename(target)
            raise
    finally:
        with suppress(OSError):
            _remove(staging)
    # The new version is in place: the rest is housekeeping, retried by recover().
    # What stays of the previous version must not keep its name: recover() would
    # put it back if the integration's folder went away.
    with suppress(OSError):
        _sync_dir(custom)
    try:
        _discard(previous)
    except OSError as err:
        LOGGER.warning("Cannot remove the previous version of %s in %s: %s", package.domain, work, err)
