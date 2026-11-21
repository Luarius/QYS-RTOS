"""Output, PowerShell, and serial-monitor panels for QYS RTOS."""

from __future__ import annotations

import codecs
import os
import re
from pathlib import Path

import serial
from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from .processes import CommandRunner
from .serial_diagnostics import SerialDiagnostic, diagnose_serial_setup, format_serial_error


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def append_output(widget: QPlainTextEdit, text: str) -> None:
    """Append child output without moving a user's selection unnecessarily."""
    text = _ANSI.sub("", text).replace("\r", "")
    cursor = widget.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText(text)
    widget.setTextCursor(cursor)
    widget.ensureCursorVisible()


def output_box(placeholder: str = "") -> QPlainTextEdit:
    box = QPlainTextEdit()
    box.setReadOnly(True)
    box.setObjectName("outputBox")
    box.setMaximumBlockCount(12000)
    box.setPlaceholderText(placeholder)
    box.setFont(QFont("Consolas", 10))
    return box


class ShellPanel(QWidget):
    """A persistent PowerShell process for ordinary line-based commands."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runner = CommandRunner(self)
        self._cwd: Path = Path.home()
        self._environment: dict[str, str] = dict(os.environ)
        self._restart = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.output = output_box("启动 PowerShell 后，可以在这里输入和查看命令。")
        layout.addWidget(self.output, 1)
        row = QHBoxLayout()
        self.prompt = QLabel("PS  ›")
        self.prompt.setObjectName("shellPrompt")
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入 PowerShell 命令，例如 west --version")
        self.send_button = QPushButton("发送")
        self.restart_button = QPushButton("启动终端")
        row.addWidget(self.prompt)
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button)
        row.addWidget(self.restart_button)
        layout.addLayout(row)
        self.runner.output.connect(lambda text: append_output(self.output, text))
        self.runner.finished.connect(self._on_finished)
        self.runner.running_changed.connect(self._on_running)
        self.input.returnPressed.connect(self.send_command)
        self.send_button.clicked.connect(self.send_command)
        self.restart_button.clicked.connect(self.restart_shell)
        self._on_running(False)

    def set_context(self, cwd: str | Path | None, environment: dict[str, str] | None) -> None:
        selected = Path(cwd) if cwd else Path.home()
        self._cwd = selected if selected.is_dir() else Path.home()
        self._environment = dict(environment or os.environ)

    def start_shell(self) -> bool:
        if self.runner.is_running:
            return True
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        executable = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        program = str(executable) if executable.is_file() else "powershell.exe"
        append_output(self.output, f"\n[PowerShell] 工作目录：{self._cwd}\n")
        started = self.runner.start(
            [program, "-NoLogo", "-NoProfile", "-Command", "-"],
            cwd=self._cwd, env=self._environment, interactive=True,
        )
        if started:
            self.runner.send(
                "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
                "[Console]::InputEncoding=[System.Text.UTF8Encoding]::new(); "
                "$OutputEncoding=[System.Text.UTF8Encoding]::new()\n"
            )
        return started

    def restart_shell(self) -> None:
        if self.runner.is_running:
            self._restart = True
            self.runner.stop()
        else:
            self.start_shell()

    def send_command(self) -> None:
        command = self.input.text()
        if not command.strip():
            return
        if not self.runner.is_running and not self.start_shell():
            return
        if self.runner.send(command + "\n"):
            append_output(self.output, f"PS > {command}\n")
            self.input.clear()
        else:
            append_output(self.output, "无法发送命令，请重启终端。\n")

    def _on_running(self, running: bool) -> None:
        self.restart_button.setText("重启终端" if running else "启动终端")

    def _on_finished(self, code: int, cancelled: bool) -> None:
        append_output(self.output, f"\n[PowerShell 已退出，状态 {code}]\n")
        if self._restart:
            self._restart = False
            QTimer.singleShot(0, self.start_shell)

    def close_shell(self) -> None:
        self._restart = False
        self.runner.stop()


class SerialPanel(QWidget):
    """A non-blocking serial log with explicit open/close and send controls."""

    connection_changed = Signal(bool)

    def __init__(self, parent=None, *, zephyr_diagnostics: bool = True):
        super().__init__(parent)
        self._zephyr_diagnostics = zephyr_diagnostics
        self._serial: serial.Serial | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.timer = QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._poll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        top = QHBoxLayout()
        self.description = QLabel("串口日志：先在上方选择端口")
        self.baud = QComboBox()
        self.baud.addItems(["115200", "9600", "57600", "230400", "460800", "921600"])
        self.open_button = QPushButton("打开串口")
        self.clear_button = QPushButton("清空")
        top.addWidget(self.description, 1)
        top.addWidget(QLabel("波特率"))
        top.addWidget(self.baud)
        top.addWidget(self.open_button)
        top.addWidget(self.clear_button)
        layout.addLayout(top)
        self.diagnostic_label = QLabel()
        self.diagnostic_label.setObjectName("serialDiagnostic")
        self.diagnostic_label.setWordWrap(True)
        self.diagnostic_label.setVisible(zephyr_diagnostics)
        layout.addWidget(self.diagnostic_label)
        self.output = output_box("连接设备串口后，接收的内容会显示在这里。")
        layout.addWidget(self.output, 1)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入要发送到串口的文本，按回车发送")
        self.send_button = QPushButton("发送")
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button)
        layout.addLayout(row)
        self.open_button.clicked.connect(self._toggle)
        self.clear_button.clicked.connect(self.output.clear)
        self.send_button.clicked.connect(self.send)
        self.input.returnPressed.connect(self.send)
        self.selected_port = ""
        self._diagnostic_build_dir: Path | None = None
        self._port_info = None
        self.diagnostic_result: SerialDiagnostic | None = None
        self._update_controls(False)
        self._refresh_diagnostic()

    def set_port(self, port: str) -> None:
        self.selected_port = port.strip()
        if not self.is_open:
            self.description.setText(f"串口日志：{self.selected_port or '未选择端口'}")
        self._refresh_diagnostic()

    def set_diagnostic_context(
        self,
        build_dir: str | Path | None,
        port_info=None,
    ) -> None:
        """Show best-effort port and generated-console details in the panel."""
        self._diagnostic_build_dir = Path(build_dir) if build_dir else None
        self._port_info = port_info
        self._refresh_diagnostic()

    def _refresh_diagnostic(self) -> None:
        if not self._zephyr_diagnostics:
            return
        port_info = self._port_info
        info_device = (
            port_info.get("device") if isinstance(port_info, dict)
            else getattr(port_info, "device", None)
        ) if port_info is not None else None
        if self.selected_port and info_device and str(info_device).strip() != self.selected_port:
            port_info = None
        self.diagnostic_result = diagnose_serial_setup(
            self._diagnostic_build_dir,
            self.selected_port,
            port_info,
        )
        self.description.setToolTip(self.diagnostic_result.text)
        self.diagnostic_label.setText(self.diagnostic_result.text)

    @property
    def is_open(self) -> bool:
        return bool(self._serial is not None and self._serial.is_open)

    def _toggle(self) -> None:
        if self.is_open:
            self.close_port()
        else:
            self.open_port()

    def open_port(self) -> bool:
        if not self.selected_port:
            append_output(self.output, "请先在上方选择串口。\n")
            return False
        candidate = None
        try:
            candidate = serial.serial_for_url(
                self.selected_port, baudrate=int(self.baud.currentText()),
                timeout=0, write_timeout=0.5, do_not_open=True,
            )
            # Avoid deliberately asserting auto-reset/boot lines on open.
            candidate.dtr = False
            candidate.rts = False
            candidate.open()
            self._serial = candidate
        except (serial.SerialException, OSError, ValueError) as exc:
            if candidate is not None:
                try:
                    candidate.close()
                except (serial.SerialException, OSError):
                    pass
            append_output(self.output, format_serial_error(exc, operation="open") + "\n")
            return False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.timer.start()
        self.description.setText(f"已连接 {self.selected_port}")
        append_output(self.output, f"[已打开 {self.selected_port}，{self.baud.currentText()} baud]\n")
        self._update_controls(True)
        self.connection_changed.emit(True)
        return True

    def close_port(self) -> None:
        self.timer.stop()
        if self._serial is not None:
            active_port = self._serial
            close_error = None
            try:
                tail = self._decoder.decode(b"", final=True)
                if tail:
                    append_output(self.output, tail)
                active_port.close()
            except (serial.SerialException, OSError) as exc:
                close_error = exc
            self._serial = None
            if close_error is None:
                append_output(self.output, "[串口已关闭]\n")
            else:
                append_output(self.output, format_serial_error(close_error, operation="close") + "\n")
            self.connection_changed.emit(False)
        self.description.setText(f"串口日志：{self.selected_port or '未选择端口'}")
        self._update_controls(False)

    def _update_controls(self, connected: bool) -> None:
        self.open_button.setText("关闭串口" if connected else "打开串口")
        self.baud.setEnabled(not connected)
        self.input.setEnabled(connected)
        self.send_button.setEnabled(connected)

    def _poll(self) -> None:
        if not self.is_open:
            return
        try:
            assert self._serial is not None
            size = min(self._serial.in_waiting, 8192)
            if size:
                data = self._serial.read(size)
                text = self._decoder.decode(data, final=False)
                if text:
                    append_output(self.output, text)
        except (serial.SerialException, OSError) as exc:
            append_output(self.output, format_serial_error(exc, operation="read") + "\n")
            self.close_port()

    def send(self) -> None:
        if not self.is_open:
            return
        message = self.input.text()
        try:
            assert self._serial is not None
            self._serial.write((message + "\r\n").encode("utf-8"))
            self.input.clear()
        except (serial.SerialException, OSError) as exc:
            append_output(self.output, format_serial_error(exc, operation="write") + "\n")
