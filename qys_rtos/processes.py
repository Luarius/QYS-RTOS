"""Asynchronous child processes for build output and interactive commands."""

from __future__ import annotations

import codecs
import os
import subprocess
import threading
from pathlib import Path
from typing import Mapping, Sequence

from PySide6.QtCore import QObject, Signal


class CommandRunner(QObject):
    """Run one child without blocking Qt and stream merged output safely."""

    output = Signal(str)
    finished = Signal(int, bool)
    running_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._cancelled = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            # A process is still considered active until its reader has
            # drained output and emitted its completion. This prevents a new
            # command from racing the previous command's final signals.
            return self._process is not None

    def start(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        interactive: bool = False,
    ) -> bool:
        """Start an argv array without invoking a shell implicitly."""
        if not argv or self.is_running:
            self.output.emit("已有命令正在运行，或命令为空。\n")
            return False
        environment = dict(env if env is not None else os.environ)
        environment.setdefault("PYTHONUTF8", "1")
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            child = subprocess.Popen(
                [str(part) for part in argv],
                cwd=str(cwd) if cwd is not None else None,
                env=environment,
                stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, ValueError) as exc:
            self.output.emit(f"无法启动命令：{exc}\n")
            return False
        with self._lock:
            self._process = child
            self._cancelled = False
        self.running_changed.emit(True)
        threading.Thread(target=self._read_output, args=(child,), daemon=True).start()
        return True

    def _read_output(self, child: subprocess.Popen) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        try:
            assert child.stdout is not None
            while True:
                chunk = child.stdout.read(4096)
                if not chunk:
                    break
                pending = decoder.getstate()[0]
                try:
                    text = decoder.decode(chunk, final=False)
                except UnicodeDecodeError:
                    # Native Windows tools may emit the system Chinese code
                    # page, while Python/west emits UTF-8. Keep split bytes
                    # across reads so Chinese paths are not corrupted.
                    decoder = codecs.getincrementaldecoder("gb18030")(errors="replace")
                    text = decoder.decode(pending + chunk, final=False)
                if text:
                    self.output.emit(text)
            try:
                tail = decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                tail = decoder.getstate()[0].decode("utf-8", errors="replace")
            if tail:
                self.output.emit(tail)
        except (OSError, ValueError) as exc:
            self.output.emit(f"读取命令输出失败：{exc}\n")
        finally:
            code = child.wait()
            if child.stdout:
                child.stdout.close()
            if child.stdin:
                child.stdin.close()
            with self._lock:
                cancelled = self._cancelled
                if self._process is child:
                    self._process = None
            self.running_changed.emit(False)
            self.finished.emit(code, cancelled)

    def send(self, text: str) -> bool:
        """Send text to a child started in interactive mode."""
        with self._lock:
            child = self._process
            if child is None or child.poll() is not None or child.stdin is None:
                return False
            try:
                child.stdin.write(text.encode("utf-8"))
                child.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def stop(self) -> None:
        """Stop the active child and its descendants without blocking the UI."""
        with self._lock:
            child = self._process
            if child is None or child.poll() is not None:
                return
            self._cancelled = True

        def terminate() -> None:
            try:
                if os.name == "nt" and child.poll() is None:
                    subprocess.run(
                        ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        timeout=10,
                    )
                elif child.poll() is None:
                    child.terminate()
            except (OSError, subprocess.SubprocessError):
                if child.poll() is None:
                    child.kill()

        threading.Thread(target=terminate, daemon=True).start()
