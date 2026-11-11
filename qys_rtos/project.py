"""Safe project creation and encoding-aware source-file operations."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


class ProjectError(Exception):
    """Base error for invalid projects and failed project/file operations."""


class FileChangedError(ProjectError):
    """Raised when a file changed or disappeared after it was read.

    Saving refuses to replace the newer or missing file unless the caller
    explicitly passes ``overwrite_external=True`` to :func:`save_text_file`.
    """


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """An editable text view plus the on-disk details needed for safe saving.

    ``text`` always uses ``\n`` line endings. ``encoding`` is ``"utf-8-sig"``
    for UTF-8 with a BOM, ``"utf-8"`` for UTF-8 without one, or ``"gb18030"``
    for the fallback encoding. ``newline`` records the dominant original line
    ending (or the platform default when the file had no line endings), and
    ``fingerprint`` is the SHA-256 digest of the exact bytes read.
    """

    path: Path
    text: str
    encoding: str
    newline: str
    fingerprint: str


_TEMPLATES = {
    "hello_world": Path("samples") / "hello_world",
    "blinky": Path("samples") / "basic" / "blinky",
}
_NEWLINES = ("\r\n", "\n", "\r")


def _resolved_path(value: str | os.PathLike[str], *, strict: bool = False) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=strict)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectError(f"Invalid path {value!s}: {exc}") from exc


def validate_project(path: str | os.PathLike[str]) -> Path:
    """Return the resolved project directory if it contains a CMake project.

    Raises :class:`ProjectError` when ``path`` is missing, is not a directory,
    or does not contain a regular ``CMakeLists.txt`` file.
    """

    project = _resolved_path(path)
    if not project.is_dir():
        raise ProjectError(f"Project folder does not exist or is not a directory: {project}")
    if not (project / "CMakeLists.txt").is_file():
        raise ProjectError(f"Project is missing CMakeLists.txt: {project}")
    return project


def create_project(
    destination: str | os.PathLike[str],
    zephyr_base: str | os.PathLike[str],
    template: str,
) -> Path:
    """Copy a supported Zephyr sample into a new project directory.

    ``template`` must be ``"hello_world"`` or ``"blinky"``. The destination
    must not already exist, including as a broken symlink, and cannot be the
    template directory or one of its descendants. Its parent directories are
    created when needed. A complete copy is staged beside the destination and
    renamed into place, so a failed copy does not leave a partial project at
    the requested path. Raises :class:`ProjectError` for invalid inputs or
    filesystem failures without replacing existing destination data.
    """

    if template not in _TEMPLATES:
        choices = ", ".join(_TEMPLATES)
        raise ProjectError(f"Unsupported Zephyr template {template!r}; choose {choices}")

    base = _resolved_path(zephyr_base)
    if not base.is_dir():
        raise ProjectError(f"Zephyr base is not a directory: {base}")

    source = _resolved_path(base / _TEMPLATES[template])
    if not source.is_dir():
        raise ProjectError(f"Zephyr {template} sample was not found: {source}")
    if not (source / "CMakeLists.txt").is_file():
        raise ProjectError(f"Zephyr {template} sample is missing CMakeLists.txt: {source}")

    requested_destination = Path(destination).expanduser()
    target = _resolved_path(requested_destination)
    if os.path.lexists(requested_destination):
        raise ProjectError(f"Project destination already exists: {requested_destination}")
    if target == source or source in target.parents:
        raise ProjectError(
            f"Project destination cannot be the sample directory or inside it: {target}"
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging_prefix = f".{target.name}.copy-"
        staging = Path(tempfile.mkdtemp(prefix=staging_prefix, dir=target.parent))
    except OSError as exc:
        raise ProjectError(f"Could not prepare project destination {target}: {exc}") from exc

    try:
        shutil.copytree(source, staging, dirs_exist_ok=True)
        os.rename(staging, target)
    except OSError as exc:
        try:
            if (
                staging.resolve().parent == target.parent.resolve()
                and staging.name.startswith(staging_prefix)
            ):
                shutil.rmtree(staging)
        except (OSError, RuntimeError, ValueError):
            pass
        raise ProjectError(f"Could not copy Zephyr sample to {target}: {exc}") from exc

    return target


def _dominant_newline(text: str) -> str:
    matches = re.findall(r"\r\n|\r|\n", text)
    if not matches:
        return os.linesep

    counts = Counter(matches)
    highest = max(counts.values())
    # Resolve ties by the first newline style in the file.
    return next(newline for newline in matches if counts[newline] == highest)


def read_text_file(path: str | os.PathLike[str]) -> FileSnapshot:
    """Read a text file while recording bytes, encoding and newline style.

    UTF-8 with or without a BOM is preferred. If strict UTF-8 decoding fails,
    GB18030 is used as a fallback. The returned editor text has normalized
    ``\n`` newlines; its snapshot can later be passed to :func:`save_text_file`.
    Raises :class:`ProjectError` if the file cannot be read or decoded.
    """

    file_path = _resolved_path(path, strict=True)
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise ProjectError(f"Could not read file {file_path}: {exc}") from exc
    if b"\x00" in raw:
        raise ProjectError(f"Binary file contains a NUL byte and cannot be opened as text: {file_path}")

    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
            decoded = raw.decode(encoding)
        else:
            try:
                decoded = raw.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                decoded = raw.decode("gb18030")
                encoding = "gb18030"
    except UnicodeDecodeError as exc:
        raise ProjectError(f"File is not valid UTF-8 or GB18030 text: {file_path}") from exc

    newline = _dominant_newline(decoded)
    normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
    return FileSnapshot(
        path=file_path,
        text=normalized,
        encoding=encoding,
        newline=newline,
        fingerprint=hashlib.sha256(raw).hexdigest(),
    )


def save_text_file(
    snapshot: FileSnapshot,
    text: str,
    *,
    overwrite_external: bool = False,
) -> FileSnapshot:
    """Atomically save edited text while preserving its original file style.

    The current file's SHA-256 digest must match ``snapshot.fingerprint``.
    A changed or missing file raises :class:`FileChangedError`, unless
    ``overwrite_external=True`` is explicitly supplied. Text is normalized to
    the recorded newline style and encoded with the original encoding/BOM.
    The replacement is written to a temporary sibling and installed with
    :func:`os.replace`; the returned snapshot reflects the saved bytes.
    Other read/write/encoding failures raise :class:`ProjectError`.
    """

    path = _resolved_path(snapshot.path)
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError as exc:
        raise ProjectError(f"Could not check current file {path}: {exc}") from exc

    current_fingerprint = hashlib.sha256(current).hexdigest() if current is not None else None
    if not overwrite_external and current_fingerprint != snapshot.fingerprint:
        if current is None:
            detail = "the file was removed after it was read"
        else:
            detail = "the file was changed after it was read"
        raise FileChangedError(f"Cannot save {path}: {detail}; reload it or explicitly overwrite")

    if snapshot.newline not in _NEWLINES:
        raise ProjectError(f"Unsupported newline style in file snapshot: {snapshot.newline!r}")

    try:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        output = normalized.replace("\n", snapshot.newline).encode(snapshot.encoding)
    except (AttributeError, LookupError, UnicodeEncodeError) as exc:
        raise ProjectError(f"Could not encode text for {path} using {snapshot.encoding}: {exc}") from exc

    mode: int | None = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ProjectError(f"Could not inspect file permissions for {path}: {exc}") from exc

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(output)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if mode is not None:
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ProjectError(f"Could not save file {path}: {exc}") from exc

    return read_text_file(path)
