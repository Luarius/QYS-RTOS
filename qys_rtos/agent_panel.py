"""Qt interface for the project-aware, external-model agent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from .agent_config import AgentConfig, AgentConfigError, load_agent_config, save_agent_config
from .agent_workspace import AgentWorkspace, WorkspaceConflict, WorkspaceError
from .config import settings_path


class _Bridge(QObject):
    status = Signal(str)
    completed = Signal(str, object, object, str)


class ModelSettingsDialog(QDialog):
    def __init__(self, config: AgentConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("模型连接设置")
        self.resize(520, 260)
        layout = QVBoxLayout(self)
        introduction = QLabel("填写模型服务提供的接口地址、模型名称和 API 密钥。密钥由本机 Windows 用户保护。")
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        form = QFormLayout()
        self.preset = QComboBox()
        self.preset.addItems(["自定义兼容接口", "DeepSeek", "通义千问（阿里云）"])
        self.url = QLineEdit(config.base_url)
        self.model = QLineEdit(config.model)
        self.key = QLineEdit(config.api_key)
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("本机模型可留空")
        form.addRow("服务模板", self.preset)
        form.addRow("接口地址", self.url)
        form.addRow("模型名称", self.model)
        form.addRow("API 密钥", self.key)
        layout.addLayout(form)
        note = QLabel("地址和模型名以服务商控制台为准；切换服务后请填写该服务对应的密钥。")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.config = config

    def _preset_changed(self, index: int) -> None:
        if index == 1:
            self.url.setText("https://api.deepseek.com")
            self.model.setText("deepseek-flash")
            self.key.clear()
        elif index == 2:
            self.url.setText("https://dashscope.aliyuncs.com/compatible-mode/v1")
            self.model.setText("qwen-plus")
            self.key.clear()

    def _save(self) -> None:
        config = AgentConfig(self.url.text().strip(), self.model.text().strip(), self.key.text())
        try:
            save_agent_config(config)
        except (AgentConfigError, OSError) as exc:
            QMessageBox.warning(self, "无法保存模型设置", str(exc))
            return
        self.config = config
        self.accept()


class AgentPanel(QWidget):
    """Run one bounded agent session away from the Qt event thread."""

    running_changed = Signal(bool)
    file_changed = Signal(str)

    def __init__(self, project_provider: Callable[[], Path | None], prepare: Callable[[], bool],
                 build_output_provider: Callable[[], str], *, backup_root: Path | None = None, parent=None):
        super().__init__(parent)
        self.project_provider = project_provider
        self.prepare = prepare
        self.build_output_provider = build_output_provider
        self.backup_root = backup_root or settings_path().parent / "AgentBackups"
        self.busy = False
        self._history = ""
        self._session_identity = None
        self._cancel_event = threading.Event()
        self._process = None
        self._stdin_lock = threading.Lock()
        self._thread = None
        self._bridge = _Bridge(self)
        self._bridge.status.connect(self._status)
        self._bridge.completed.connect(self._completed)
        try:
            self.config = load_agent_config()
            config_error = ""
        except AgentConfigError:
            self.config = AgentConfig()
            config_error = "模型设置无法读取，请重新填写。"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel("AI Agent")
        title.setObjectName("agentTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.settings_button = QPushButton("模型设置")
        self.clear_button = QPushButton("新对话")
        header.addWidget(self.clear_button)
        header.addWidget(self.settings_button)
        layout.addLayout(header)
        self.model_label = QLabel()
        self.model_label.setWordWrap(True)
        layout.addWidget(self.model_label)
        self.notice = QLabel("发送任务后，内容和按需读取的工程文件会发给所选模型服务，可能产生 API 费用。")
        self.notice.setWordWrap(True)
        self.notice.setObjectName("mutedLabel")
        layout.addWidget(self.notice)
        self.conversation = QPlainTextEdit()
        self.conversation.setReadOnly(True)
        self.conversation.setPlaceholderText("输入任务，例如：解释这个工程，或检查编译错误。")
        self.conversation.document().setMaximumBlockCount(6000)
        layout.addWidget(self.conversation, 1)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("输入你希望 Agent 完成的任务…")
        self.prompt.setMaximumHeight(100)
        layout.addWidget(self.prompt)
        self.include_build = QCheckBox("附带最近的编译输出")
        self.include_build.setToolTip("勾选后会把最近的编译日志发送给模型服务。")
        layout.addWidget(self.include_build)
        backup_actions = QHBoxLayout()
        self.restore_button = QPushButton("恢复上次修改")
        self.backups_button = QPushButton("查看备份")
        backup_actions.addWidget(self.restore_button)
        backup_actions.addWidget(self.backups_button)
        backup_actions.addStretch(1)
        layout.addLayout(backup_actions)
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("停止")
        self.send_button = QPushButton("发送任务")
        self.send_button.setObjectName("primaryButton")
        self.cancel_button.setEnabled(False)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.send_button)
        layout.addLayout(actions)
        self.settings_button.clicked.connect(self.open_settings)
        self.clear_button.clicked.connect(self.clear_conversation)
        self.send_button.clicked.connect(self.send_task)
        self.cancel_button.clicked.connect(self.cancel)
        self.restore_button.clicked.connect(self.restore_last)
        self.backups_button.clicked.connect(self.open_backups)
        self._refresh_model_label()
        if config_error:
            self._status(config_error)

    def _refresh_model_label(self) -> None:
        self.model_label.setText(f"模型：{self.config.model}　|　{self.config.base_url}")

    def _status(self, message: str) -> None:
        self.conversation.appendPlainText(f"· {message}")

    def open_settings(self) -> None:
        dialog = ModelSettingsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config = dialog.config
            self._history = ""
            self._session_identity = None
            self._refresh_model_label()
            self._status("模型设置已保存，已开始新会话。")

    def clear_conversation(self) -> None:
        self._history = ""
        self._session_identity = None
        self.conversation.clear()

    def _set_busy(self, value: bool) -> None:
        self.busy = value
        for control in (self.send_button, self.settings_button, self.clear_button, self.restore_button, self.backups_button, self.prompt, self.include_build):
            control.setEnabled(not value)
        self.cancel_button.setEnabled(value)
        self.running_changed.emit(value)

    def send_task(self) -> None:
        if self.busy:
            return
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            return
        if len(prompt) > 20000:
            QMessageBox.warning(self, "任务过长", "请将任务内容控制在 2 万字以内。")
            return
        project = self.project_provider()
        if not project:
            QMessageBox.information(self, "请先打开工程", "请先新建或打开一个 Zephyr 工程。")
            return
        if not self.prepare():
            return
        if not self.config.api_key and not self.config.base_url.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]")):
            QMessageBox.information(self, "请先配置模型", "请点击“模型设置”，填写模型接口和 API 密钥。")
            return
        identity = (str(Path(project).resolve()), self.config.base_url, self.config.model)
        if identity != self._session_identity:
            self._history = ""
            self._session_identity = identity
        history = self._history
        build_output = self.build_output_provider() if self.include_build.isChecked() else ""
        config = self.config
        self.conversation.appendPlainText(f"\n你：{prompt}\n")
        self.prompt.clear()
        self._cancel_event = threading.Event()
        self._process = None
        self._set_busy(True)
        self._status("正在连接模型并处理任务…")
        self._thread = threading.Thread(target=self._work, args=(Path(project), config, prompt, build_output, history), daemon=True)
        self._thread.start()

    @staticmethod
    def _worker_command() -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--qys-agent-worker"]
        return [sys.executable, str(Path(__file__).resolve().parent.parent / "run_app.py"), "--qys-agent-worker"]

    def _send_worker(self, message: dict) -> None:
        with self._stdin_lock:
            process = self._process
            if process is not None and process.stdin is not None and process.poll() is None:
                process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
                process.stdin.flush()

    def _work(self, project: Path, config: AgentConfig, prompt: str, build_output: str, history: str) -> None:
        answer = ""
        error = ""
        changed: set[str] = set()
        process = None
        received_final = False
        try:
            environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYDANTIC_AI_NO_BANNER="1")
            process = subprocess.Popen(self._worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
                                       env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._process = process
            self._send_worker({"project": str(project), "backup_root": str(self.backup_root),
                               "base_url": config.base_url, "model": config.model, "api_key": config.api_key,
                               "prompt": prompt, "build_output": build_output, "history": history})
            if self._cancel_event.is_set():
                self._send_worker({"type": "cancel"})
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if event.get("type") == "status":
                    self._bridge.status.emit(str(event.get("message", "")))
                elif event.get("type") == "file_changed":
                    changed.add(str(event.get("path", "")))
                elif event.get("type") == "completed":
                    answer = str(event.get("answer", ""))
                    history = str(event.get("history", ""))
                    received_final = True
                elif event.get("type") == "error":
                    error = str(event.get("message", "任务失败。"))
                    received_final = True
            code = process.wait()
            if not received_final:
                error = "任务已停止。" if self._cancel_event.is_set() else f"Agent 后台进程意外退出（代码 {code}）。请查看备份。"
        except Exception as exc:
            if self._cancel_event.is_set():
                error = "任务已停止。"
            else:
                error = str(exc).replace(config.api_key, "[密钥已隐藏]") if config.api_key else str(exc)
                error = error[:1500] or type(exc).__name__
        finally:
            self._process = None
            if process is not None:
                if process.stdin:
                    process.stdin.close()
                if process.stdout:
                    process.stdout.close()
            try:
                self._bridge.completed.emit(answer, history, sorted(changed), error)
            except RuntimeError:
                pass  # Window was closed after cancellation.

    def _completed(self, answer: str, history, changed, error: str) -> None:
        for path in changed:
            self.file_changed.emit(path)
        if error:
            self._status(f"任务未完成：{error}")
        else:
            self._history = history
            self.conversation.appendPlainText(f"Agent：{answer}\n")
        self._set_busy(False)

    def cancel(self) -> None:
        self._cancel_event.set()
        try:
            self._send_worker({"type": "cancel"})
        except (OSError, ValueError):
            pass
        self.cancel_button.setEnabled(False)
        self._status("正在停止当前任务…")

    def shutdown(self) -> None:
        if self.busy:
            self.cancel()

    def restore_last(self) -> None:
        if self.busy:
            return
        project = self.project_provider()
        if not project or not self.prepare():
            return
        try:
            workspace = AgentWorkspace(Path(project), self.backup_root, on_change=self.file_changed.emit)
            try:
                record = workspace.restore_last()
            except WorkspaceConflict as exc:
                choice = QMessageBox.question(self, "文件已有新内容",
                    f"{exc}\n\n仍要恢复修改前版本吗？当前内容会先另存到备份目录。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if choice != QMessageBox.StandardButton.Yes:
                    return
                record = workspace.restore_last(force=True)
            self._history = ""
            self._status(f"已恢复：{record.path}")
            QMessageBox.information(self, "已恢复备份", f"已恢复 {record.path} 的修改前状态。")
        except (WorkspaceError, OSError) as exc:
            QMessageBox.warning(self, "无法恢复备份", str(exc))

    def open_backups(self) -> None:
        project = self.project_provider()
        if not project:
            QMessageBox.information(self, "请先打开工程", "请先打开一个 Zephyr 工程。")
            return
        try:
            folder = AgentWorkspace(Path(project), self.backup_root).backup_root
            if not folder.is_dir():
                QMessageBox.information(self, "暂无备份", "当前工程还没有 Agent 备份。")
                return
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                raise OSError(f"无法打开备份目录：{folder}")
        except (WorkspaceError, OSError) as exc:
            QMessageBox.warning(self, "无法打开备份", str(exc))
