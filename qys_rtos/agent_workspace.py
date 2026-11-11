"""Bounded project tools and recoverable edits for the AI agent."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .project import ProjectError, read_text_file, validate_project


class WorkspaceError(Exception):
    """A requested agent operation is outside the supported project scope."""


class WorkspaceConflict(WorkspaceError):
    """A file changed after the agent last read or wrote it."""


@dataclass(frozen=True)
class AgentFile:
    path: str
    text: str
    fingerprint: str


@dataclass(frozen=True)
class BackupRecord:
    path: str
    backup_dir: Path


_MAX_BYTES = 128 * 1024
_MAX_FILES = 400
_ALLOWED_SUFFIXES = {".c", ".h", ".cpp", ".hpp", ".cc", ".dts", ".dtsi", ".overlay",
                     ".conf", ".yaml", ".yml", ".cmake", ".txt", ".md", ".json", ".ini",
                     ".cfg", ".py", ".ld", ".h.in"}
_ALLOWED_NAMES = {"cmakelists.txt", "kconfig", "makefile", "west.yml"}
_SECRET_NAME = re.compile(r"(?:secret|credential|password|api[-_]?key|private[-_]?key|token)", re.I)
_SECRET_CONTENT = re.compile(
    r"(?im)\b[A-Za-z0-9_]*(?:PASSWORD|PASSWD|PASSPHRASE|API_?KEY|ACCESS_?TOKEN|CLIENT_?SECRET|WIFI_?PSK)[A-Za-z0-9_]*\b"
    r"\s*(?:[:=]|\s)\s*[\"']([^\"'\r\n]{1,})[\"']"
)


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_create(path: Path, data: bytes) -> None:
    """Create without replacing a file created concurrently by another program."""
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            os.rename(temporary, path)  # Windows rename fails when the destination exists.
        else:
            os.link(temporary, path)
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_existing(path: Path, *, delete_access: bool = False):
    """Hold a Windows exclusive file handle through comparison and update."""
    if os.name != "nt":
        with path.open("r+b") as stream:
            yield stream
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    final_name = kernel.GetFinalPathNameByHandleW
    final_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_name.restype = wintypes.DWORD
    access = 0x80000000 | 0x40000000 | (0x00010000 if delete_access else 0)
    handle = create(str(path), access, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "文件正被其他程序使用，无法安全修改")
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_name(handle, buffer, len(buffer), 0)
        actual = buffer.value
        if not length or length >= len(buffer):
            raise OSError("无法确认打开的文件位置")
        if actual.startswith("\\\\?\\UNC\\"):
            actual = "\\\\" + actual[8:]
        elif actual.startswith("\\\\?\\"):
            actual = actual[4:]
        if os.path.normcase(os.path.abspath(actual)) != os.path.normcase(os.path.abspath(str(path))):
            raise WorkspaceError("文件位置已改变，拒绝继续修改。")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        handle = None  # The Python file now owns the Windows handle.
        with os.fdopen(descriptor, "r+b") as stream:
            yield stream
    finally:
        if handle is not None:
            close(handle)


def _replace_locked(stream, expected_hash: str, data: bytes) -> None:
    stream.seek(0)
    if _fingerprint(stream.read()) != expected_hash:
        raise WorkspaceConflict("文件已有新修改，请重新读取后再修改。")
    stream.seek(0)
    stream.write(data)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def _delete_locked(stream, path: Path) -> None:
    if os.name != "nt":
        path.unlink()
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Disposition(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    operation = kernel.SetFileInformationByHandle
    operation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    operation.restype = wintypes.BOOL
    info = _Disposition(True)
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
    if not operation(handle, 4, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "无法安全删除 Agent 新建文件")


class AgentWorkspace:
    """Access only supported files in one project and back up every write."""

    def __init__(self, root: Path, backup_root: Path, *, on_change: Callable[[str], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> None:
        self.root = validate_project(root)
        identity = hashlib.sha256(os.path.normcase(str(self.root)).encode("utf-8")).hexdigest()[:20]
        self.backup_root = Path(backup_root) / identity
        self.mutation_lock = threading.RLock()
        self._on_change = on_change or (lambda _path: None)
        self._cancelled = cancelled or (lambda: False)

    def _path(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative.strip() or "\x00" in relative:
            raise WorkspaceError("请提供工程内的相对文件路径。")
        part_path = Path(relative)
        if part_path.is_absolute() or part_path.drive or any(part in {"..", "."} for part in part_path.parts):
            raise WorkspaceError("只能访问当前工程内的相对路径。")
        if any(part.startswith(".") or part.casefold().startswith("build") for part in part_path.parts):
            raise WorkspaceError("不能访问隐藏目录、隐藏文件或构建目录。")
        if any(_SECRET_NAME.search(part) for part in part_path.parts) or part_path.suffix.casefold() in {".key", ".pem", ".p12", ".pfx"}:
            raise WorkspaceError("不能读取或修改可能包含凭据的文件。")
        if part_path.name.casefold() not in _ALLOWED_NAMES and part_path.suffix.casefold() not in _ALLOWED_SUFFIXES:
            raise WorkspaceError("仅支持常见 Zephyr 工程文本文件。")
        candidate = self.root.joinpath(part_path)
        for parent in (candidate, *candidate.parents):
            if parent == self.root:
                break
            try:
                if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                    raise WorkspaceError("不能通过链接或联接访问工程文件。")
            except OSError as exc:
                raise WorkspaceError(f"无法检查工程路径：{exc}") from exc
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceError("文件路径超出当前工程。") from exc
        return resolved

    def list_files(self) -> list[str]:
        result: list[str] = []
        for directory, names, files in os.walk(self.root, followlinks=False):
            names[:] = [n for n in names if not n.startswith(".") and not n.casefold().startswith("build") and not _SECRET_NAME.search(n)
                        and not (Path(directory) / n).is_symlink()
                        and not (hasattr(Path(directory) / n, "is_junction") and (Path(directory) / n).is_junction())]
            for name in sorted(files, key=str.casefold):
                relative = (Path(directory) / name).relative_to(self.root).as_posix()
                try:
                    path = self._path(relative)
                    if path.is_file() and path.stat().st_size <= _MAX_BYTES:
                        result.append(relative)
                except (WorkspaceError, OSError):
                    continue
                if len(result) >= _MAX_FILES:
                    return result
        return result

    def read_file(self, relative: str) -> AgentFile:
        path = self._path(relative)
        try:
            if not path.is_file() or path.stat().st_size > _MAX_BYTES:
                raise WorkspaceError("文件不存在或超过 128 KB。")
            snapshot = read_text_file(path)
        except (OSError, ProjectError) as exc:
            raise WorkspaceError(f"无法读取工程文件：{exc}") from exc
        if _SECRET_CONTENT.search(snapshot.text):
            raise WorkspaceError("文件可能包含密码或密钥，未发送给模型；请先移除敏感信息。")
        return AgentFile(path.relative_to(self.root).as_posix(), snapshot.text, snapshot.fingerprint)

    def write_file(self, relative: str, content: str, expected_fingerprint: str) -> BackupRecord:
        with self.mutation_lock:
            return self._write_file(relative, content, expected_fingerprint)

    def _write_file(self, relative: str, content: str, expected_fingerprint: str) -> BackupRecord:
        path = self._path(relative)
        if self._cancelled():
            raise WorkspaceError("任务已取消。")
        if not isinstance(content, str) or "\x00" in content or len(content.encode("utf-8")) > _MAX_BYTES:
            raise WorkspaceError("文件内容必须是小于 128 KB 的文本。")
        if not path.parent.is_dir():
            raise WorkspaceError("目标父目录不存在。")
        try:
            exists = path.is_file()
            if path.exists() and not exists:
                raise WorkspaceError("目标不是普通文件。")
            if exists:
                snapshot = self.read_file(relative)
                if not expected_fingerprint or snapshot.fingerprint != expected_fingerprint:
                    raise WorkspaceConflict("文件已有新修改，请重新读取后再修改。")
                file_snapshot = read_text_file(path)
                original = path.read_bytes()
                if _fingerprint(original) != snapshot.fingerprint or file_snapshot.fingerprint != snapshot.fingerprint:
                    raise WorkspaceConflict("文件读取期间发生变化。")
                normalized = content.replace("\r\n", "\n").replace("\r", "\n")
                new_bytes = normalized.replace("\n", file_snapshot.newline).encode(file_snapshot.encoding)
            else:
                if expected_fingerprint:
                    raise WorkspaceConflict("文件已消失，请重新读取工程。")
                original = b""
                new_bytes = content.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        except (OSError, UnicodeError, ProjectError) as exc:
            raise WorkspaceError(f"无法准备修改：{exc}") from exc

        self.backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = self.backup_root / f"{stamp}-{uuid.uuid4().hex[:8]}"
        backup_dir.mkdir()
        meta = {"project": str(self.root), "path": path.relative_to(self.root).as_posix(),
                "existed": exists, "before_hash": _fingerprint(original), "after_hash": _fingerprint(new_bytes), "state": "prepared"}
        _atomic_bytes(backup_dir / "original.bin", original)
        _atomic_bytes(backup_dir / "manifest.json", json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
        try:
            if self._cancelled():
                raise WorkspaceError("任务已取消，工程未修改。")
            if exists:
                with _exclusive_existing(path) as stream:
                    if self._cancelled():
                        raise WorkspaceError("任务已取消，工程未修改。")
                    _replace_locked(stream, snapshot.fingerprint, new_bytes)
            else:
                _atomic_create(path, new_bytes)
        except FileExistsError as exc:
            raise WorkspaceConflict("新文件已由其他操作创建。") from exc
        except (OSError, ProjectError) as exc:
            self._on_change(str(path))
            raise WorkspaceError(f"保存失败，原始备份保存在 {backup_dir}：{exc}") from exc
        meta["state"] = "complete"
        try:
            _atomic_bytes(backup_dir / "manifest.json", json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
        except OSError as exc:
            raise WorkspaceError(f"文件已修改，但无法更新备份记录；原始备份位于 {backup_dir}") from exc
        finally:
            self._on_change(str(path))
        return BackupRecord(meta["path"], backup_dir)

    def restore_last(self, *, force: bool = False) -> BackupRecord:
        with self.mutation_lock:
            return self._restore_last(force=force)

    def _restore_last(self, *, force: bool = False) -> BackupRecord:
        if not self.backup_root.is_dir():
            raise WorkspaceError("当前工程还没有 Agent 备份。")
        for folder in sorted(self.backup_root.iterdir(), reverse=True):
            try:
                meta = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                if meta.get("state") not in {"complete", "prepared"} or meta.get("project") != str(self.root):
                    continue
                path = self._path(meta["path"])
                original = (folder / "original.bin").read_bytes()
                if _fingerprint(original) != meta.get("before_hash"):
                    raise WorkspaceError(f"备份数据损坏，未恢复：{folder}")
                if meta["state"] == "prepared" and not meta["existed"] and not path.exists():
                    continue
                if meta["existed"]:
                    if path.exists():
                        with _exclusive_existing(path) as stream:
                            current = stream.read()
                            current_hash = _fingerprint(current)
                            if meta["state"] == "prepared" and current_hash == meta["before_hash"]:
                                continue  # A prepared edit never reached the target file.
                            if current_hash != meta["after_hash"]:
                                if not force:
                                    raise WorkspaceConflict(f"{meta['path']} 在 Agent 修改后又被更改；备份位于 {folder}")
                                _atomic_bytes(folder / "displaced-before-restore.bin", current)
                            _replace_locked(stream, current_hash, original)
                    else:
                        _atomic_create(path, original)
                else:
                    with _exclusive_existing(path, delete_access=True) as stream:
                        current = stream.read()
                        if _fingerprint(current) != meta["after_hash"]:
                            if not force:
                                raise WorkspaceConflict(f"{meta['path']} 在 Agent 修改后又被更改；备份位于 {folder}")
                            _atomic_bytes(folder / "displaced-before-restore.bin", current)
                        _delete_locked(stream, path)
                meta["state"] = "restored"
                _atomic_bytes(folder / "manifest.json", json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
                self._on_change(str(path))
                return BackupRecord(meta["path"], folder)
            except WorkspaceConflict:
                raise
            except (KeyError, OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
                raise WorkspaceError(f"无法恢复备份：{exc}") from exc
        raise WorkspaceError("当前工程没有可恢复的 Agent 修改。")
