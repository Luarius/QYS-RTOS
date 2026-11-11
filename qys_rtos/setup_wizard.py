"""Guided Windows setup dialog for a new Zephyr workspace."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QFontDatabase, QGuiApplication, QTextCursor
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qys_rtos.bootstrap import (
    SetupError,
    SetupStep,
    build_install_plan,
    find_python,
    git_network_failure_help,
    prerequisite_status,
    refreshed_windows_environment,
    requirements_install_command,
    sanitize_console_output,
    setup_environment,
    validate_workspace_destination,
)
from qys_rtos.processes import CommandRunner


_ZEPHYR_GUIDE_URL = "https://docs.zephyrproject.org/latest/develop/getting_started/index.html"
_ZEPHYR_GIT_URL = "https://github.com/zephyrproject-rtos/zephyr"
_REQUIRED_PREREQUISITES = ("Python 3.12+", "Git", "CMake", "Ninja")
_WINGET_PACKAGES = {
    "Python 3.12+": "Python.Python.3.12",
    "Git": "Git.Git",
    "CMake": "Kitware.CMake",
    "Ninja": "Ninja-build.Ninja",
    "gperf": "oss-winget.gperf",
    "dtc": "oss-winget.dtc",
    "wget": "JernejSimoncic.Wget",
    "7-Zip": "7zip.7zip",
}
_PREREQUISITE_ORDER = (*_WINGET_PACKAGES.keys(), "winget")


@dataclass(frozen=True)
class _DetectionResult:
    context: str
    environment: dict[str, str]
    prerequisites: dict[str, bool]
    python_command: tuple[str, ...] | None
    steps: tuple[SetupStep, ...] | None = None
    plan_error: str = ""


class _DetectionWorker(QThread):
    """Run Python and tool discovery away from the Qt event loop."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, context: str, config: dict[str, Any] | None = None, parent=None):
        super().__init__(parent)
        self._context = context
        self._config = config

    def run(self) -> None:
        try:
            environment = refreshed_windows_environment()
            prerequisites = prerequisite_status(environment)
            # prerequisite_status also checks Python. This second lookup keeps
            # the argv prefix needed by build_install_plan for a fresh venv.
            python_command = find_python(environment)
            steps: tuple[SetupStep, ...] | None = None
            plan_error = ""

            if self._context == "setup" and self._config is not None:
                missing = [name for name in _REQUIRED_PREREQUISITES if not prerequisites.get(name, False)]
                if python_command is None and "Python 3.12+" not in missing:
                    missing.append("Python 3.12+")
                if not missing:
                    try:
                        planned = build_install_plan(
                            self._config["workspace"],
                            python_command or (),
                            self._config["revision"],
                            self._config["toolchains"],
                        )
                        steps = tuple(planned)
                    except SetupError as exc:
                        plan_error = str(exc)

            self.completed.emit(
                _DetectionResult(
                    context=self._context,
                    environment=environment,
                    prerequisites=prerequisites,
                    python_command=python_command,
                    steps=steps,
                    plan_error=plan_error,
                )
            )
        except Exception as exc:  # surfaced in the dialog rather than lost in the worker
            self.failed.emit(str(exc))


class SetupWizard(QDialog):
    """Guide the user through a safe, explicitly started Zephyr installation.

    Prerequisite discovery runs in a Python worker thread. Installation steps
    run one at a time through :class:`CommandRunner` using argv arrays and the
    selected workspace's isolated environment. Existing data is never removed;
    cancellation or failure can leave partial downloads for the user to inspect.
    The ``setup_complete`` signal emits the resolved workspace path after every
    installation stage, including the standalone Python dependency substep,
    has completed successfully.
    """

    setup_complete = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QYS RTOS - Zephyr 环境准备")
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        self.resize(min(800, available.width() - 40) if available else 800,
                    min(760, available.height() - 40) if available else 760)
        self.setMinimumSize(640, 460)

        self._runner = CommandRunner(self)
        self._runner.output.connect(self._on_command_output)
        self._runner.finished.connect(self._on_command_finished)
        self._runner.running_changed.connect(self._refresh_controls)

        self._worker: _DetectionWorker | None = None
        self._workflow: str | None = None
        self._worker_context = ""
        self._prerequisites: dict[str, bool] = {}
        self._python_command: tuple[str, ...] | None = None
        self._environment: dict[str, str] = {}
        self._setup_steps: tuple[SetupStep, ...] = ()
        self._step_index = 0
        self._workspace: Path | None = None
        self._active_kind: str | None = None
        self._active_title = ""
        self._active_step: SetupStep | None = None
        self._captured_listing: list[str] = []
        self._captured_diagnostic: list[str] = []
        self._pending_console_output = ""
        self._winget_queue: list[tuple[str, str]] = []
        self._current_package = ""
        self._stop_requested = False
        self._close_after_stop = False

        self._build_ui()
        self._refresh_controls()
        QTimer.singleShot(0, self._start_initial_check)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        title = QLabel("准备 Zephyr 开发环境")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 5)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        explanation = QLabel(
            "向导会联网下载 Zephyr 源码、模块和 SDK，过程可能需要较长时间、稳定网络和较多磁盘空间。"
            "只会在你点击“开始安装 Zephyr”后执行。已有工作区或非空目录不会被覆盖；"
            "取消或失败后，已下载的部分文件会保留供你检查。Zephyr 工作区路径请勿包含空格。"
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: #a7b8ca; padding: 2px 0 4px 0;")
        layout.addWidget(explanation)

        workspace_group = QGroupBox("新工作区")
        workspace_layout = QFormLayout(workspace_group)
        workspace_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        workspace_row = QHBoxLayout()
        default_workspace = str(Path.home() / "zephyrproject")
        self.workspace_edit = QLineEdit(default_workspace if not any(c.isspace() for c in default_workspace) else "")
        self.workspace_edit.setObjectName("workspacePathEdit")
        self.workspace_edit.setToolTip("新建工作区目录；请选择不存在或为空且路径不含空格的目录")
        self.workspace_edit.setPlaceholderText("例如 D:\\zephyrproject（路径不能包含空格）")
        browse_button = QPushButton("浏览父目录…")
        browse_button.clicked.connect(self._browse_workspace_parent)
        workspace_row.addWidget(self.workspace_edit, 1)
        workspace_row.addWidget(browse_button)
        workspace_layout.addRow("工作区路径：", workspace_row)

        self.revision_edit = QLineEdit("v4.5.0")
        self.revision_edit.setObjectName("zephyrRevisionEdit")
        self.revision_edit.setMaximumWidth(220)
        workspace_layout.addRow("Zephyr 版本：", self.revision_edit)
        layout.addWidget(workspace_group)

        toolchain_group = QGroupBox("SDK 工具链")
        toolchain_layout = QVBoxLayout(toolchain_group)
        self.arm_checkbox = QCheckBox("ARM (arm-zephyr-eabi)")
        self.arm_checkbox.setChecked(True)
        self.riscv_checkbox = QCheckBox("RISC-V (riscv64-zephyr-elf)")
        self.riscv_checkbox.setChecked(True)
        self.all_architectures_checkbox = QCheckBox("安装全部架构工具链（下载量和磁盘占用会增加）")
        self.all_architectures_checkbox.toggled.connect(self._update_architecture_controls)
        toolchain_row = QHBoxLayout()
        toolchain_row.addWidget(self.arm_checkbox)
        toolchain_row.addWidget(self.riscv_checkbox)
        toolchain_row.addStretch(1)
        toolchain_layout.addLayout(toolchain_row)
        toolchain_layout.addWidget(self.all_architectures_checkbox)
        layout.addWidget(toolchain_group)

        prerequisite_group = QGroupBox("前置工具")
        prerequisite_layout = QGridLayout(prerequisite_group)
        prerequisite_layout.setHorizontalSpacing(20)
        self._prerequisite_labels: dict[str, QLabel] = {}
        for index, name in enumerate(_PREREQUISITE_ORDER):
            row, column = divmod(index, 4)
            item = QLabel(f"{name}：检测中…")
            item.setMinimumWidth(130)
            self._prerequisite_labels[name] = item
            prerequisite_layout.addWidget(item, row, column)
        self._python_command_label = QLabel("Python 命令：尚未检测")
        self._python_command_label.setWordWrap(True)
        next_row = (len(_PREREQUISITE_ORDER) + 3) // 4
        prerequisite_layout.addWidget(self._python_command_label, next_row, 0, 1, 4)
        python_hint = QLabel("Windows 上优先推荐 Python 3.12；Python 3.13 及更高版本可能遇到依赖兼容问题，不会仅因版本更高而拒绝。")
        python_hint.setWordWrap(True)
        prerequisite_layout.addWidget(python_hint, next_row + 1, 0, 1, 4)
        layout.addWidget(prerequisite_group)

        progress_group = QGroupBox("安装进度")
        progress_layout = QVBoxLayout(progress_group)
        self.stage_label = QLabel("等待前置工具检测")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 6)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("阶段 %v / %m")
        progress_layout.addWidget(self.stage_label)
        progress_layout.addWidget(self.progress_bar)
        layout.addWidget(progress_group)

        output_group = QGroupBox("命令、输出与错误")
        output_layout = QVBoxLayout(output_group)
        self.output_edit = QPlainTextEdit()
        self.output_edit.setObjectName("setupOutput")
        self.output_edit.setReadOnly(True)
        self.output_edit.setMinimumHeight(120)
        self.output_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.output_edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.output_edit.setPlaceholderText("前置工具检测结果和安装命令输出会显示在这里。")
        output_layout.addWidget(self.output_edit)
        layout.addWidget(output_group, 1)

        self.status_label = QLabel("正在准备检测…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.recheck_button = QPushButton("重新检测")
        self.recheck_button.clicked.connect(self._start_recheck)
        self.install_prerequisites_button = QPushButton("安装缺失的前置工具")
        self.install_prerequisites_button.clicked.connect(self._start_prerequisite_install)
        self.start_button = QPushButton("开始安装 Zephyr")
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self._start_setup)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self._stop_work)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.recheck_button)
        buttons.addWidget(self.install_prerequisites_button)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        outer.addLayout(buttons)

    def _browse_workspace_parent(self) -> None:
        current = Path(self.workspace_edit.text().strip() or Path.home() / "zephyrproject")
        initial = str(current.parent if current.name else current)
        parent = QFileDialog.getExistingDirectory(self, "选择新工作区的父目录", initial)
        if parent:
            self.workspace_edit.setText(str(Path(parent) / "zephyrproject"))

    def _update_architecture_controls(self, all_architectures: bool) -> None:
        self.arm_checkbox.setEnabled(not all_architectures)
        self.riscv_checkbox.setEnabled(not all_architectures)

    def _start_initial_check(self) -> None:
        if self._workflow is None:
            self._start_detection("check")

    def _start_recheck(self) -> None:
        if self._workflow is None:
            self._start_detection("check")

    def _start_detection(self, context: str, config: dict[str, Any] | None = None) -> None:
        if self._worker is not None:
            return
        self._worker_context = context
        worker = _DetectionWorker(context, config, self)
        self._worker = worker
        worker.completed.connect(self._on_detection_completed)
        worker.failed.connect(self._on_detection_failed)
        worker.finished.connect(self._on_detection_worker_finished)
        self._refresh_controls()
        worker.start()

    def _start_prerequisite_install(self) -> None:
        if self._workflow is not None:
            return
        self._workflow = "winget_check"
        self._stop_requested = False
        self.status_label.setText("正在重新检测前置工具…")
        self._append_log("\n正在检查缺失的前置工具和 winget。\n")
        self._start_detection("winget")
        self._refresh_controls()

    def _start_setup(self) -> None:
        if self._workflow is not None:
            return
        toolchains: list[str]
        if self.all_architectures_checkbox.isChecked():
            toolchains = []
        else:
            toolchains = []
            if self.arm_checkbox.isChecked():
                toolchains.append("arm-zephyr-eabi")
            if self.riscv_checkbox.isChecked():
                toolchains.append("riscv64-zephyr-elf")
            if not toolchains:
                QMessageBox.warning(
                    self,
                    "未选择工具链",
                    "请至少选择 ARM 或 RISC-V；也可以勾选“安装全部架构工具链”。",
                )
                return

        config = {
            "workspace": self.workspace_edit.text().strip(),
            "revision": self.revision_edit.text().strip(),
            "toolchains": toolchains,
        }
        self._workflow = "setup_check"
        self._stop_requested = False
        self.progress_bar.setValue(0)
        self.stage_label.setText("正在检查环境并验证新工作区…")
        self.status_label.setText("正在检查必需工具和工作区安全条件…")
        self._append_log("\n开始准备 Zephyr 安装计划（此步骤不会下载或写入工作区）。\n")
        self._start_detection("setup", config)
        self._refresh_controls()

    def _on_detection_completed(self, result: _DetectionResult) -> None:
        self._prerequisites = result.prerequisites
        self._python_command = result.python_command
        self._environment = result.environment
        self._render_prerequisites()

        if result.context == "check":
            self._workflow = None
            missing = self._missing_required()
            if missing:
                self.status_label.setText("检测完成；缺少必需工具：" + "、".join(missing))
            else:
                self.status_label.setText("前置工具检测完成，可以开始安装。")
        elif result.context == "setup":
            missing = self._missing_required()
            if missing:
                self._workflow = None
                message = "缺少开始安装所需的工具：" + "、".join(missing)
                self.status_label.setText(message)
                QMessageBox.warning(self, "缺少前置工具", message)
            elif result.plan_error:
                self._workflow = None
                self.status_label.setText("安装计划未启动。")
                self._append_log(f"\n无法创建安全安装计划：{result.plan_error}\n")
                QMessageBox.warning(self, "无法开始安装", result.plan_error)
            elif result.steps:
                # build_install_plan resolves the selected path. Preserve that
                # exact resolved workspace for environment and success signal.
                selected = self.workspace_edit.text().strip()
                self._workspace = Path(selected).expanduser().resolve()
                self._setup_steps = result.steps
                self._step_index = 0
                self._workflow = "setup_preflight"
                self._append_log(
                    f"安全检查通过，工作区：{self._workspace}\n"
                    "正在检查 Git 是否能访问 Zephyr 源码；通过后才会创建工作区文件。\n"
                )
                self._start_git_preflight()
            else:
                self._workflow = None
                self.status_label.setText("没有生成安装步骤。")
                self._append_log("\n无法生成安装步骤。\n")
                QMessageBox.warning(self, "无法开始安装", "没有生成安装步骤，请检查前置工具和输入。")
        elif result.context == "winget":
            missing = [name for name in _WINGET_PACKAGES if not result.prerequisites.get(name, False)]
            if not missing:
                self._workflow = None
                self.status_label.setText("前置工具已齐全，无需使用 winget 安装。")
                self._append_log("所有可通过 winget 安装的前置工具均已检测到。\n")
            elif not result.prerequisites.get("winget", False):
                self._workflow = None
                self.status_label.setText("未找到 winget；请按 Zephyr 官方指南手动准备前置工具。")
                self._show_guide_link()
            else:
                self._winget_queue = [(name, _WINGET_PACKAGES[name]) for name in missing]
                self._append_log("将按顺序运行 winget 安装命令；每个工具完成后才会启动下一个。\n")
                self._start_next_winget()
        elif result.context == "post_winget":
            self._workflow = None
            missing = [name for name, available in result.prerequisites.items() if name != "winget" and not available]
            if missing:
                self.status_label.setText("安装命令已结束；仍缺少：" + "、".join(missing))
                QMessageBox.warning(
                    self,
                    "仍有工具缺失",
                    "winget 安装流程已结束，但重新检测后仍未找到：" + "、".join(missing)
                    + "。请检查输出或按 Zephyr 官方指南手动处理。",
                )
            else:
                self.status_label.setText("前置工具已重新检测完成。")
                QMessageBox.information(self, "检测完成", "前置工具已重新检测完成，可以开始安装 Zephyr。")

        self._refresh_controls()

    def _on_detection_failed(self, message: str) -> None:
        context = self._worker_context
        self._workflow = None
        self.status_label.setText("前置工具检测失败。")
        self._append_log(f"\n前置工具检测失败：{message}\n")
        self._refresh_controls()
        if context in {"setup", "winget", "post_winget"}:
            QMessageBox.critical(self, "前置工具检测失败", message or "未知错误")

    def _on_detection_worker_finished(self) -> None:
        self._worker = None
        self._refresh_controls()

    def _render_prerequisites(self) -> None:
        for name, label in self._prerequisite_labels.items():
            available = self._prerequisites.get(name, False)
            missing_text = "未找到或低于 3.28" if name == "CMake" else "未找到"
            label.setText(f"{name}：{'✓ 已找到' if available else '✗ ' + missing_text}")
            label.setStyleSheet("color: #89ddb4;" if available else "color: #ff9d9d;")
            label.setAccessibleName(f"{name}：{'已找到' if available else '未找到'}")
        if self._python_command:
            command = subprocess.list2cmdline(list(self._python_command))
            self._python_command_label.setText(f"Python 命令：{command}")
            self._python_command_label.setToolTip(command)
        else:
            self._python_command_label.setText("Python 命令：未找到 Python 3.12+；请先安装后重新检测")
            self._python_command_label.setToolTip("")

    def _missing_required(self) -> list[str]:
        missing = [name for name in _REQUIRED_PREREQUISITES if not self._prerequisites.get(name, False)]
        if self._python_command is None and "Python 3.12+" not in missing:
            missing.append("Python 3.12+")
        return missing

    def _show_guide_link(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("winget 不可用")
        box.setText("系统中没有找到 winget。请按 Zephyr 官方 Windows 入门指南准备缺少的工具。")
        link = QLabel(f'<a href="{_ZEPHYR_GUIDE_URL}">{_ZEPHYR_GUIDE_URL}</a>')
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.layout().addWidget(link, 1, 0, 1, box.layout().columnCount())
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def _start_next_winget(self) -> None:
        if self._stop_requested:
            self._cancel_workflow()
            return
        if not self._winget_queue:
            self._append_log("\nwinget 安装命令全部完成，正在刷新环境并重新检测工具。\n")
            self._workflow = "post_winget_check"
            self.status_label.setText("正在刷新环境并重新检测…")
            self._start_detection("post_winget")
            return

        name, package_id = self._winget_queue.pop(0)
        self._current_package = name
        # If an older CMake is already on PATH, offer winget's upgrade command
        # rather than treating its presence as a supported version.
        action = "upgrade" if name == "CMake" and shutil.which("cmake", path=refreshed_windows_environment().get("PATH", "")) else "install"
        argv = [
            "winget", action, "--id", package_id, "--exact", "--source", "winget",
            "--silent", "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity",
        ]
        environment = refreshed_windows_environment()
        self._start_command(argv, None, environment, "winget", f"安装前置工具：{name}")

    def _start_setup_step(self) -> None:
        if self._stop_requested:
            self._cancel_workflow()
            return
        if self._step_index >= len(self._setup_steps):
            self._complete_setup()
            return

        step = self._setup_steps[self._step_index]
        self.progress_bar.setValue(self._step_index + 1)
        self.stage_label.setText(f"阶段 {self._step_index + 1}/6：{step.title}")
        self.status_label.setText(f"正在执行：{step.title}")
        self._active_step = step
        self._captured_listing = []
        self._start_command(
            step.argv,
            step.cwd,
            setup_environment(self._workspace or "", refreshed_windows_environment()),
            "setup",
            step.title,
        )

    def _start_git_preflight(self) -> None:
        """Check GitHub access before creating or modifying the workspace."""
        environment = dict(self._environment)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GCM_INTERACTIVE"] = "never"
        self._start_command(
            ("git", "ls-remote", "--exit-code", _ZEPHYR_GIT_URL, "HEAD"),
            self._workspace.parent if self._workspace else None,
            environment,
            "git_preflight",
            "检查 GitHub 连接",
        )

    def _is_requirements_listing_step(self, step: SetupStep | None) -> bool:
        return bool(step and len(step.argv) >= 2 and tuple(step.argv[-2:]) == ("packages", "pip"))

    def _start_command(
        self,
        argv: tuple[str, ...] | list[str],
        cwd: str | Path | None,
        environment: dict[str, str],
        kind: str,
        title: str,
    ) -> None:
        self._active_kind = kind
        self._active_title = title
        self._captured_diagnostic = []
        self._pending_console_output = ""
        command_line = subprocess.list2cmdline([str(part) for part in argv])
        self._append_log(f"\n> {sanitize_console_output(command_line)}\n")
        if cwd is not None:
            self._append_log(f"[工作目录] {cwd}\n")
        started = self._runner.start(argv, cwd=cwd, env=environment)
        if not started:
            self._active_kind = None
            self._fail_workflow(f"无法启动命令：{title}")
        self._refresh_controls()

    def _on_command_output(self, chunk: str) -> None:
        if self._active_kind == "git_preflight":
            # Git errors can echo a configured proxy URL containing secrets.
            # Keep the raw text only long enough to classify it into fixed,
            # credential-free guidance after the command exits.
            self._captured_diagnostic.append(chunk)
            return
        if self._active_kind in {"setup", "requirements"}:
            self._captured_diagnostic.append(chunk)
        if self._active_kind == "setup" and self._is_requirements_listing_step(self._active_step):
            self._captured_listing.append(chunk)
        self._pending_console_output += chunk
        lines = self._pending_console_output.splitlines(keepends=True)
        self._pending_console_output = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending_console_output = lines.pop()
        if lines:
            self._append_log(sanitize_console_output("".join(lines)))

    def _on_command_finished(self, exit_code: int, cancelled: bool) -> None:
        kind = self._active_kind
        title = self._active_title
        if kind != "git_preflight" and self._pending_console_output:
            self._append_log(sanitize_console_output(self._pending_console_output))
            self._pending_console_output = ""
        self._active_kind = None

        if cancelled or self._stop_requested:
            self._cancel_workflow()
            return
        if exit_code != 0:
            if kind == "git_preflight":
                guidance = git_network_failure_help("".join(self._captured_diagnostic))
                if guidance is None:
                    guidance = "请检查 GitHub 网络访问、Git 配置和防火墙后重试。"
                self._fail_workflow(
                    f"GitHub 连接检查失败，退出代码：{exit_code}。{guidance}"
                    "这项检查发生在创建或修改工作区之前；修正连接后可再次点击“开始安装 Zephyr”。"
                )
                return
            if kind == "winget":
                title = f"安装前置工具 {self._current_package}"
            failure = f"{title}失败，命令退出代码：{exit_code}。请查看上方已脱敏的输出。"
            if kind in {"setup", "requirements"}:
                guidance = git_network_failure_help("".join(self._captured_diagnostic))
                if guidance:
                    failure += f"\n{guidance}"
                    failure += (
                        "\n当前工作区可能包含本次已创建或下载的内容；向导会保留这些文件，"
                        "不会删除或覆盖，也不会自动断点续装。请先检查工作区，再选择安全的后续操作。"
                    )
            self._fail_workflow(failure)
            return

        if kind == "git_preflight":
            if self._workspace is None:
                self._fail_workflow("缺少已验证的工作区路径；尚未创建任何工作区文件。")
                return
            try:
                # The network check can take time. Revalidate immediately
                # before the first local write in case another process used
                # the selected path while Git was connecting.
                self._workspace = validate_workspace_destination(self._workspace)
            except SetupError as exc:
                self._fail_workflow(
                    f"GitHub 连接检查已通过，但工作区在等待期间发生变化，未启动安装：{exc}"
                )
                return
            self._append_log("GitHub 连接检查通过；现在开始创建工作区并执行安装步骤。\n")
            self._workflow = "setup"
            self._start_setup_step()
            self._refresh_controls()
            return

        if kind == "winget":
            self._append_log(f"前置工具安装命令完成：{self._current_package}\n")
            self._start_next_winget()
        elif kind == "setup" and self._is_requirements_listing_step(self._active_step):
            try:
                assert self._workspace is not None and self._active_step is not None
                pip_argv = requirements_install_command(
                    self._workspace,
                    "".join(self._captured_listing),
                )
            except (SetupError, OSError, ValueError) as exc:
                self._fail_workflow(f"无法准备独立的 Python 依赖安装步骤：{exc}")
                return
            self._append_log("\nwest 已结束；现在用独立 pip 进程安装其列出的依赖。\n")
            self._start_command(
                pip_argv,
                self._active_step.cwd,
                setup_environment(self._workspace, refreshed_windows_environment()),
                "requirements",
                "安装 Python 依赖（独立 pip 子步骤）",
            )
        elif kind == "setup" or kind == "requirements":
            self._step_index += 1
            self._start_setup_step()

        self._refresh_controls()

    def _complete_setup(self) -> None:
        assert self._workspace is not None
        self._workflow = None
        self._stop_requested = False
        self.progress_bar.setValue(6)
        self.stage_label.setText("全部 6 个阶段已完成")
        missing = [name for name in ("gperf", "dtc", "wget", "7-Zip") if self._prerequisites.get(name) is False]
        message = f"Zephyr 工作区安装步骤已完成：\n{self._workspace}"
        if missing:
            message += (
                "\n\n未检测到以下推荐工具：" + "、".join(missing)
                + "。部分开发板或构建可能需要它们；Zephyr SDK 可能已包含部分工具，请根据实际工程核对。"
            )
        self.status_label.setText("工作区安装步骤已完成" + ("；请检查推荐工具。" if missing else "。"))
        self._append_log("\n" + message + "\n")
        self.setup_complete.emit(str(self._workspace))
        self._refresh_controls()
        QMessageBox.information(self, "工作区安装完成", message)

    def _fail_workflow(self, message: str) -> None:
        self._workflow = None
        self._stop_requested = False
        self.status_label.setText("流程已停止；请查看错误信息和命令输出。")
        self._append_log(f"\n错误：{message}\n")
        self._refresh_controls()
        QMessageBox.critical(self, "操作失败", message)
        if self._close_after_stop:
            self._close_after_stop = False
            QTimer.singleShot(0, self.close)

    def _cancel_workflow(self) -> None:
        was_preflight = self._workflow == "setup_preflight"
        self._workflow = None
        self._stop_requested = False
        self._winget_queue.clear()
        if was_preflight:
            self.status_label.setText("GitHub 连接检查已停止；尚未创建工作区文件。")
            self._append_log("\nGitHub 连接检查已停止；尚未创建工作区文件。\n")
        else:
            self.status_label.setText("操作已停止。已下载的部分文件会保留，向导不会自动删除它们。")
            self._append_log("\n操作已停止；工作区中的部分下载内容保留，未删除任何文件。\n")
        self._refresh_controls()
        if self._close_after_stop:
            self._close_after_stop = False
            QTimer.singleShot(0, self.close)

    def _stop_work(self) -> None:
        if self._worker is not None:
            self.status_label.setText("正在进行前置检测，暂时不能停止；检测结束后可关闭向导。")
            return
        if self._workflow not in {
            "setup", "setup_preflight", "winget_check", "winget", "post_winget_check", "setup_check"
        }:
            return
        self._stop_requested = True
        self.status_label.setText("正在停止当前命令…")
        self._append_log("\n收到停止请求。\n")
        if self._runner.is_running:
            self._runner.stop()
        else:
            self._cancel_workflow()
        self._refresh_controls()

    def _append_log(self, text: str) -> None:
        if not text:
            return
        self.output_edit.moveCursor(QTextCursor.MoveOperation.End)
        self.output_edit.insertPlainText(text)
        self.output_edit.moveCursor(QTextCursor.MoveOperation.End)
        self.output_edit.ensureCursorVisible()

    def _refresh_controls(self, *_args: object) -> None:
        active = self._workflow is not None or self._worker is not None
        editable = self._workflow not in {
            "setup_check", "setup_preflight", "setup", "winget_check", "winget", "post_winget_check"
        }
        for widget in (
            self.workspace_edit,
            self.revision_edit,
            self.arm_checkbox,
            self.riscv_checkbox,
            self.all_architectures_checkbox,
        ):
            widget.setEnabled(editable)
        self.recheck_button.setEnabled(not active)
        self.install_prerequisites_button.setEnabled(not active)
        self.start_button.setEnabled(not active)
        self.stop_button.setEnabled(
            self._worker is None
            and self._workflow in {"setup_preflight", "setup", "winget_check", "winget", "post_winget_check"}
        )
        self.close_button.setEnabled(True)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method name
        if self._worker is not None:
            QMessageBox.information(
                self,
                "正在检测",
                "前置工具检测仍在后台运行，请等待检测结束后再关闭向导。",
            )
            event.ignore()
            return

        if self._workflow is not None or self._runner.is_running:
            answer = QMessageBox.question(
                self,
                "操作仍在运行",
                "安装命令仍在运行。现在停止命令并关闭向导吗？已下载的部分文件会保留，不会自动删除。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_after_stop = True
            self._stop_requested = True
            if self._runner.is_running:
                self._runner.stop()
            else:
                self._cancel_workflow()
            event.ignore()
            return
        event.accept()

