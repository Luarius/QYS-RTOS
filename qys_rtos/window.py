"""Main QYS RTOS desktop window."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path

from serial.tools import list_ports
from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QDialog, QDockWidget, QFileDialog, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from . import __version__
from .config import AppSettings, load_settings, save_settings
from .agent_panel import AgentPanel
from .editor import EditorWorkspace
from .devicetree import DeviceTreeError, read_devicetree
from .devicetree_panel import DeviceTreePanel
from .dts_workspace import active_project_overlays
from .environment_dialog import EnvironmentDialog
from .panels import SerialPanel, ShellPanel, append_output, output_box
from .processes import CommandRunner
from .project import ProjectError, create_project, validate_project
from .setup_wizard import SetupWizard
from .zephyr import (
    ZephyrEnvironment, ZephyrError, boards_command, build_command,
    discover_environment, flash_command, parse_boards, read_runner_configuration,
    validate_build_directory,
)


def default_build_dir(
    project: str | Path,
    board: str,
    workspace: str | Path | None = None,
    sdk: str | Path | None = None,
) -> Path:
    """Separate builds and move past incomplete or incompatible default output."""
    base = _default_build_dir_base(project, board, workspace, sdk)
    validation_environment = ZephyrEnvironment(
        Path(workspace).expanduser() if workspace else Path.cwd(),
        Path("west.exe"),
        sdk=Path(sdk).expanduser() if sdk else None,
    )

    candidate = base
    retry = 0
    while True:
        if not _build_path_is_occupied(candidate):
            return candidate
        if (candidate / "CMakeCache.txt").is_file():
            try:
                validate_build_directory(validation_environment, board, project, candidate)
            except ZephyrError:
                pass
            else:
                return candidate

        retry += 1
        candidate = base.with_name(f"{base.name}-retry-{retry}")


def _default_build_dir_base(
    project: str | Path,
    board: str,
    workspace: str | Path | None,
    sdk: str | Path | None,
) -> Path:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", board).strip("._")[:52] or "board"
    workspace_key = os.path.normcase(str(Path(workspace).expanduser().resolve())) if workspace else ""
    sdk_key = os.path.normcase(str(Path(sdk).expanduser().resolve())) if sdk else ""
    digest = hashlib.sha256(f"{board}\0{workspace_key}\0{sdk_key}".encode("utf-8")).hexdigest()[:8]
    return Path(project) / "build_qys" / f"{slug}-{digest}"


def _is_default_build_dir(
    value: str | Path,
    project: str | Path,
    board: str,
    workspace: str | Path | None,
    sdk: str | Path | None,
) -> bool:
    base = _default_build_dir_base(project, board, workspace, sdk)
    candidate = Path(value)
    if _path_identity(candidate.parent) != _path_identity(base.parent):
        return False
    return candidate.name == base.name or bool(
        re.fullmatch(re.escape(base.name) + r"-retry-[1-9][0-9]*", candidate.name)
    )


def _path_identity(path: Path) -> str:
    try:
        resolved = path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        resolved = Path(os.path.abspath(path))
    return os.path.normcase(os.path.normpath(str(resolved))).casefold()


def _build_path_is_occupied(path: Path) -> bool:
    try:
        if os.path.lexists(path):
            return not path.is_dir() or next(path.iterdir(), None) is not None
    except (OSError, RuntimeError, ValueError):
        return True
    return False


def split_runner_arguments(text: str) -> list[str]:
    """Parse optional runner arguments using Windows command-line quoting."""
    if not text.strip():
        return []
    if os.name != "nt":
        return shlex.split(text)
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    pointer = shell32.CommandLineToArgvW("qys " + text, ctypes.byref(count))
    if not pointer:
        raise ValueError("无法解析烧录参数，请检查引号。")
    try:
        return [pointer[index] for index in range(1, count.value)]
    finally:
        kernel32.LocalFree(pointer)


class MainWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"QYS RTOS {__version__}")
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        width = min(1400, max(900, available.width() - 40)) if available else 1200
        height = min(900, max(600, available.height() - 40)) if available else 800
        self.resize(width, height)
        self.setMinimumSize(900, 600)
        self.settings = load_settings()
        self.environment: ZephyrEnvironment | None = self._restore_environment()
        self.operation = CommandRunner(self)
        self.board_process = CommandRunner(self)
        self._board_chunks: list[str] = []
        self._operation_kind = ""
        self._flash_after_build = False
        self._agent_busy = False
        self._auto_build_dir = True
        self._closing = False
        self._documents_closed = False
        self._port_infos = {}
        self._invalid_build_locations: set[str] = set()
        self._active_build_location: str | None = None

        self._build_interface()
        self.operation.output.connect(self._append_operation_output)
        self.operation.finished.connect(self._operation_finished)
        self.operation.running_changed.connect(self._operation_state_changed)
        self.board_process.output.connect(self._board_chunks.append)
        self.board_process.finished.connect(self._boards_finished)
        self.editor.status_changed.connect(self.statusBar().showMessage)
        self.editor.file_saved.connect(lambda _path: self.statusBar().showMessage("文件已保存", 4000))

        self._apply_environment()
        self._restore_project()
        self.refresh_ports()
        if self.settings.board:
            self.board_combo.setEditText(self.settings.board)
        elif self.environment and (self.environment.workspace / "my_projects" / "esp32c6_led_verify").is_dir():
            self.board_combo.setEditText("esp32c6_devkitc/esp32c6/hpcore")
        self._update_build_dir()
        if self.settings.build_dir and self.settings.project and self.editor.project_root:
            saved = Path(self.settings.build_dir)
            self._auto_build_dir = _is_default_build_dir(
                saved,
                self.editor.project_root,
                self.board_combo.currentText().strip(),
                self.environment.workspace if self.environment else None,
                self.environment.sdk if self.environment else None,
            )
            if not self._auto_build_dir:
                self.build_dir.setText(str(saved))
        self.refresh_runners()
        if self.environment:
            QTimer.singleShot(100, self.refresh_boards)

    def _restore_environment(self) -> ZephyrEnvironment | None:
        if self.settings.workspace and self.settings.west:
            workspace = Path(self.settings.workspace)
            python = workspace / ".venv" / "Scripts" / "python.exe"
            env = ZephyrEnvironment(
                workspace, Path(self.settings.west), python if python.is_file() else None,
                Path(self.settings.sdk) if self.settings.sdk else None,
            )
            if not env.validate():
                return env
        return discover_environment()

    def _build_interface(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        brand = QLabel("Q")
        brand.setObjectName("brandMark")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        name = QLabel("QYS RTOS")
        name.setObjectName("appTitle")
        subtitle = QLabel(f"ZEPHYR 开发工作台  ·  {__version__}")
        subtitle.setObjectName("appSubtitle")
        titles.addWidget(name)
        titles.addWidget(subtitle)
        self.environment_badge = QLabel("尚未配置环境")
        self.environment_badge.setObjectName("environmentBadge")
        self.environment_badge.setToolTip("点击右侧设置已有 Zephyr 环境，或使用环境引导。")
        self.new_button = QPushButton("＋  新建工程")
        self.open_button = QPushButton("打开工程")
        self.env_button = QPushButton("环境设置")
        self.setup_button = QPushButton("环境引导")
        self.agent_button = QPushButton("AI Agent")
        self.agent_button.setCheckable(True)
        self.agent_button.setChecked(True)
        header.addWidget(brand)
        header.addSpacing(6)
        header.addLayout(titles)
        header.addSpacing(18)
        header.addWidget(self.environment_badge)
        header.addStretch(1)
        for button in (self.new_button, self.open_button, self.env_button, self.setup_button, self.agent_button):
            header.addWidget(button)
        outer.addLayout(header)

        bar = QFrame()
        bar.setObjectName("controlBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(14, 9, 14, 9)
        bar_layout.setSpacing(9)
        bar_layout.addWidget(QLabel("开发板"))
        self.board_combo = QComboBox()
        self.board_combo.setEditable(True)
        self.board_combo.setMinimumWidth(160)
        self.board_combo.setToolTip("选择或输入完整 Zephyr 开发板目标；列表从当前 west 工作区读取。")
        self.board_combo.lineEdit().setPlaceholderText("搜索或输入开发板目标")
        completer = QCompleter(self.board_combo.model(), self.board_combo)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.board_combo.setCompleter(completer)
        self.boards_button = QPushButton("刷新")
        self.boards_button.setToolTip("从 Zephyr 读取可用开发板")
        bar_layout.addWidget(self.board_combo, 2)
        bar_layout.addWidget(self.boards_button)
        bar_layout.addSpacing(8)
        bar_layout.addWidget(QLabel("串口"))
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(105)
        self.port_combo.setToolTip("串口监视使用此端口；ESP32 等串口烧录器也使用此端口。")
        self.ports_button = QPushButton("刷新")
        self.ports_button.setToolTip("重新检测电脑串口")
        bar_layout.addWidget(self.port_combo, 1)
        bar_layout.addWidget(self.ports_button)
        bar_layout.addStretch(1)
        self.build_button = QPushButton("编译")
        self.build_button.setObjectName("buildButton")
        self.flash_button = QPushButton("▶  烧录")
        self.flash_button.setObjectName("primaryButton")
        self.flash_button.setToolTip("保存并编译当前工程，成功后烧录到所选开发板。")
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        bar_layout.addWidget(self.build_button)
        bar_layout.addWidget(self.flash_button)
        bar_layout.addWidget(self.stop_button)
        outer.addWidget(bar)

        self.project_label = QLabel("未打开工程")
        self.project_label.setObjectName("projectPath")
        self.project_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.project_label)

        center = QSplitter(Qt.Orientation.Vertical)
        self.center_splitter = center
        center.setChildrenCollapsible(False)
        self.editor = EditorWorkspace()
        self.device_tree = DeviceTreePanel()
        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.setDocumentMode(True)
        self.workspace_tabs.setMinimumHeight(220)
        self.workspace_tabs.addTab(self.editor, "代码")
        self.workspace_tabs.addTab(self.device_tree, "设备树")
        center.addWidget(self.workspace_tabs)

        lower = QFrame()
        lower.setObjectName("consolePanel")
        lower.setMinimumHeight(70)
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(10, 7, 10, 8)
        self.console_tabs = QTabWidget()
        self.console_tabs.setDocumentMode(True)
        self.build_output = output_box("编译和烧录命令及其输出会显示在这里。")
        self.shell = ShellPanel()
        self.serial = SerialPanel()
        self.console_tabs.addTab(self.build_output, "输出")
        self.console_tabs.addTab(self.shell, "PowerShell")
        self.console_tabs.addTab(self.serial, "串口监视")
        lower_layout.addWidget(self.console_tabs)
        center.addWidget(lower)
        center.setStretchFactor(0, 5)
        center.setStretchFactor(1, 2)
        center.setSizes([570, 250])
        outer.addWidget(center, 1)

        advanced = QFrame()
        advanced.setObjectName("advancedBar")
        advanced_layout = QHBoxLayout(advanced)
        advanced_layout.setContentsMargins(8, 5, 8, 5)
        advanced_layout.addWidget(QLabel("构建目录"))
        self.build_dir = QLineEdit()
        self.build_dir.setPlaceholderText("打开工程并选择开发板后自动生成")
        self.build_dir.setToolTip("不同开发板默认使用不同的 QYS 构建目录，避免覆盖已有构建。")
        self.build_browse = QPushButton("选择…")
        self.build_reset = QPushButton("默认")
        advanced_layout.addWidget(self.build_dir, 3)
        advanced_layout.addWidget(self.build_browse)
        advanced_layout.addWidget(self.build_reset)
        advanced_layout.addSpacing(8)
        advanced_layout.addWidget(QLabel("烧录器"))
        self.runner_combo = QComboBox()
        self.runner_combo.setMinimumWidth(125)
        self.runner_combo.setToolTip("构建成功后根据 Zephyr 生成的配置自动列出。")
        advanced_layout.addWidget(self.runner_combo, 1)
        advanced_layout.addWidget(QLabel("附加参数"))
        self.extra_args = QLineEdit()
        self.extra_args.setPlaceholderText("可选")
        self.extra_args.setToolTip("按所选烧录器的参数填写；例如 --dev-id <调试器编号>。")
        advanced_layout.addWidget(self.extra_args, 1)
        outer.addWidget(advanced)
        self.statusBar().showMessage("准备就绪")

        self.agent_dock = QDockWidget("AI Agent", self)
        self.agent_dock.setObjectName("agentDock")
        self.agent_dock.setMinimumWidth(310)
        self.agent_panel = AgentPanel(lambda: self.editor.project_root, self._agent_prepare,
                                      self._agent_build_output, parent=self.agent_dock)
        self.agent_dock.setWidget(self.agent_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.agent_dock)
        self.agent_button.toggled.connect(self.agent_dock.setVisible)
        self.agent_dock.visibilityChanged.connect(self.agent_button.setChecked)
        self.agent_panel.running_changed.connect(self._agent_state_changed)
        self.agent_panel.file_changed.connect(self._agent_file_changed)

        self.new_button.clicked.connect(self.new_project)
        self.open_button.clicked.connect(self.open_project)
        self.env_button.clicked.connect(self.configure_environment)
        self.setup_button.clicked.connect(self.setup_environment)
        self.boards_button.clicked.connect(self.refresh_boards)
        self.ports_button.clicked.connect(self.refresh_ports)
        self.build_button.clicked.connect(self.build_project)
        self.flash_button.clicked.connect(self.flash_project)
        self.stop_button.clicked.connect(self.operation.stop)
        self.build_browse.clicked.connect(self.choose_build_dir)
        self.build_reset.clicked.connect(self.reset_build_dir)
        self.build_dir.textEdited.connect(self._build_dir_edited)
        self.build_dir.textChanged.connect(self._invalidate_devicetree)
        self.build_dir.editingFinished.connect(self.refresh_runners)
        self.board_combo.currentTextChanged.connect(self._board_changed)
        self.port_combo.currentIndexChanged.connect(self._port_changed)
        self.console_tabs.currentChanged.connect(self._console_changed)
        self.workspace_tabs.currentChanged.connect(self._workspace_changed)
        self.device_tree.refresh_requested.connect(self._refresh_devicetree)
        self.device_tree.append_requested.connect(self._append_devicetree)
        self._build_shortcut = QShortcut(QKeySequence("Ctrl+B"), self)
        self._build_shortcut.activated.connect(self.build_project)

    def _restore_project(self) -> None:
        candidate = self.settings.project
        if not candidate and self.environment:
            example = self.environment.workspace / "my_projects" / "esp32c6_led_verify"
            if example.is_dir():
                candidate = str(example)
        if candidate:
            try:
                project = validate_project(candidate)
                if self.editor.set_project(project):
                    self.project_label.setText(f"工程  /  {project}")
            except ProjectError:
                pass

    def _apply_environment(self) -> None:
        self._invalidate_devicetree()
        if self.environment:
            errors = self.environment.validate()
        else:
            errors = ["未配置 Zephyr 环境"]
        self.environment_badge.setText("●  环境已配置" if not errors else "●  需要配置环境")
        self.environment_badge.setProperty("ready", not errors)
        self.environment_badge.style().unpolish(self.environment_badge)
        self.environment_badge.style().polish(self.environment_badge)
        if self.environment and not errors:
            self.environment_badge.setToolTip(str(self.environment.workspace))
            self.shell.set_context(self.environment.workspace, self.environment.process_env())
        else:
            self.environment_badge.setToolTip("\n".join(errors))
            self.shell.set_context(Path.home(), dict(os.environ))
        if self._auto_build_dir and self.editor.project_root:
            self._update_build_dir()
            self.refresh_runners()

    def configure_environment(self) -> None:
        dialog = EnvironmentDialog(self.environment, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.environment:
            self.environment = dialog.environment
            self._apply_environment()
            self.refresh_boards()
            self._save_settings()
            self.statusBar().showMessage("Zephyr 环境设置已更新", 5000)

    def setup_environment(self) -> None:
        dialog = SetupWizard(self)
        dialog.setup_complete.connect(self._installed_environment)
        dialog.exec()

    def _installed_environment(self, workspace_text: str) -> None:
        workspace = Path(workspace_text)
        west = workspace / ".venv" / "Scripts" / "west.exe"
        python = workspace / ".venv" / "Scripts" / "python.exe"
        sdk_root = workspace / ".qys-sdk"
        candidates = sorted((path for path in sdk_root.glob("zephyr-sdk-*") if path.is_dir()), reverse=True) if sdk_root.is_dir() else []
        self.environment = ZephyrEnvironment(workspace, west, python if python.is_file() else None, candidates[0] if candidates else None)
        self._apply_environment()
        self.refresh_boards()
        self._save_settings()

    def _valid_environment(self) -> bool:
        errors = self.environment.validate() if self.environment else ["请先配置或安装 Zephyr 环境。"]
        if self.environment and not (self.environment.workspace / ".west" / "config").is_file():
            errors.append("工作区缺少 .west/config，请重新选择 west 工作区根目录。")
        if errors:
            QMessageBox.warning(self, "需要 Zephyr 环境", "\n".join(errors))
            return False
        return True

    def new_project(self) -> None:
        if not self._valid_environment():
            return
        parent = QFileDialog.getExistingDirectory(self, "选择新工程的上级文件夹", str(Path.home()))
        if not parent:
            return
        name, ok = QInputDialog.getText(self, "新建工程", "工程文件夹名称：", text="my_zephyr_app")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in {".", ".."} or Path(name).name != name or any(char in name for char in '<>:"/\\|?*'):
            QMessageBox.warning(self, "名称不正确", "工程名称不能包含路径或 Windows 不允许的字符。")
            return
        label, ok = QInputDialog.getItem(self, "选择工程模板", "从 Zephyr 示例创建：", ["Hello World（串口输出）", "Blinky（开发板需有 LED）"], 0, False)
        if not ok:
            return
        template = "blinky" if label.startswith("Blinky") else "hello_world"
        try:
            assert self.environment is not None
            project = create_project(Path(parent) / name, self.environment.workspace / "zephyr", template)
            self._set_project(project)
        except ProjectError as exc:
            QMessageBox.warning(self, "无法创建工程", str(exc))

    def open_project(self) -> None:
        start = str(self.editor.project_root or Path.home())
        selected = QFileDialog.getExistingDirectory(self, "打开 Zephyr 工程文件夹", start)
        if not selected:
            return
        try:
            self._set_project(validate_project(selected))
        except ProjectError as exc:
            QMessageBox.warning(self, "无法打开工程", str(exc))

    def _set_project(self, project: Path) -> None:
        if self.editor.set_project(project):
            self._invalidate_devicetree()
            self.project_label.setText(f"工程  /  {project}")
            self._auto_build_dir = True
            self._update_build_dir()
            self.refresh_runners()
            self._save_settings()

    def refresh_boards(self) -> None:
        if not self._valid_environment() or self.board_process.is_running:
            return
        assert self.environment is not None
        # The output signal is connected to this list's append method. Keep
        # the same list object so the streamed data is not lost on refresh.
        self._board_chunks.clear()
        self.boards_button.setEnabled(False)
        self.boards_button.setText("读取中…")
        try:
            args = boards_command(self.environment)
            if not self.board_process.start(args, cwd=self.environment.workspace, env=self.environment.process_env()):
                self.boards_button.setEnabled(True)
                self.boards_button.setText("刷新")
                QMessageBox.warning(self, "读取开发板失败", "无法启动 west，请检查环境设置。")
        except ZephyrError as exc:
            self.boards_button.setEnabled(True)
            self.boards_button.setText("刷新")
            QMessageBox.warning(self, "读取开发板失败", str(exc))

    def _boards_finished(self, code: int, cancelled: bool) -> None:
        self.boards_button.setEnabled(not self.operation.is_running and not self._flash_after_build)
        self.boards_button.setText("刷新")
        if self._closing or cancelled:
            return
        if code != 0:
            append_output(self.build_output, "\n[读取开发板失败]\n" + "".join(self._board_chunks))
            self.statusBar().showMessage("开发板列表读取失败，请检查输出", 10000)
            return
        boards = parse_boards("".join(self._board_chunks))
        current = self.board_combo.currentText()
        with QSignalBlocker(self.board_combo):
            self.board_combo.clear()
            self.board_combo.addItems(boards)
            self.board_combo.setEditText(current)
        self.statusBar().showMessage(f"已读取 {len(boards)} 个开发板目标", 5000)

    def refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        if current is None:
            current = self.settings.port
        try:
            ports = sorted(list_ports.comports(), key=lambda port: port.device)
        except (OSError, ValueError) as exc:
            self.statusBar().showMessage(f"读取串口失败：{exc}", 8000)
            return
        self._port_infos = {port.device: port for port in ports}
        with QSignalBlocker(self.port_combo):
            self.port_combo.clear()
            self.port_combo.addItem("自动 / 未选择", "")
            for port in ports:
                self.port_combo.addItem(f"{port.device}  ·  {port.description}", port.device)
            for index in range(self.port_combo.count()):
                if self.port_combo.itemData(index) == current:
                    self.port_combo.setCurrentIndex(index)
                    break
        self._port_changed()
        self.statusBar().showMessage(f"检测到 {len(ports)} 个串口", 4000)

    def _port_changed(self, *_args) -> None:
        port = self.port_combo.currentData() or ""
        if self.serial.is_open and self.serial.selected_port != port:
            self.serial.close_port()
        self.serial.set_port(port)
        self._update_serial_diagnostic()

    def _board_changed(self, *_args) -> None:
        self._invalidate_devicetree()
        if self._auto_build_dir:
            self._update_build_dir()
            self.refresh_runners()

    def _build_dir_edited(self, _text: str) -> None:
        self._auto_build_dir = False

    def _invalidate_devicetree(self, *_args) -> None:
        self.device_tree.clear_snapshot("工程、开发板或构建目录已改变；请刷新当前工程的编译结果。")
        self._update_serial_diagnostic()

    def _update_serial_diagnostic(self) -> None:
        try:
            build = None if self.operation.is_running else self._verified_build_location()
        except (ZephyrError, OSError, RuntimeError, ValueError):
            build = None
        port = self.port_combo.currentData() or ""
        self.serial.set_diagnostic_context(build, self._port_infos.get(port))

    def _verified_build_location(self) -> Path:
        """Identify only a cached build belonging to the current selection."""
        if not self.environment or not self.editor.project_root:
            raise ZephyrError("请先配置环境并打开工程。")
        board = self.board_combo.currentText().strip()
        text = self.build_dir.text().strip()
        if not board or not text:
            raise ZephyrError("请选择开发板和构建目录。")
        location = Path(text).expanduser()
        if not location.is_absolute():
            location = self.environment.workspace / location
        location = location.resolve()
        if _path_identity(location) in self._invalid_build_locations:
            raise ZephyrError("此构建目录最近一次编译未成功，请成功编译后再查看。")
        if not (location / "CMakeCache.txt").is_file():
            raise ZephyrError("当前构建目录尚无可验证的编译缓存，请先编译。")
        validate_build_directory(self.environment, board, self.editor.project_root, location)
        return location

    def _workspace_changed(self, index: int) -> None:
        if self.workspace_tabs.widget(index) is self.device_tree:
            self.center_splitter.setSizes([800, 120])
            self._refresh_devicetree()
        else:
            self.center_splitter.setSizes([570, 250])

    def _refresh_devicetree(self) -> None:
        if self.operation.is_running:
            self.device_tree.clear_snapshot("正在编译或烧录，请在操作完成后刷新设备树。")
            return
        try:
            build = self._verified_build_location()
            source = build / "zephyr" / "zephyr.dts"
            if not source.is_file():
                raise ZephyrError("构建目录还没有生成设备树，请先成功编译。")
            tree = read_devicetree(source)
            assert self.editor.project_root is not None
            overlays = active_project_overlays(build / "CMakeCache.txt", self.editor.project_root)
            self.device_tree.set_snapshot(tree, overlays)
        except (ZephyrError, DeviceTreeError, OSError, RuntimeError, ValueError) as exc:
            self.device_tree.clear_snapshot(f"无法读取当前构建的设备树：{exc}")

    def _append_devicetree(self, target: str, snippet: str) -> None:
        try:
            build = self._verified_build_location()
            assert self.editor.project_root is not None
            overlays = active_project_overlays(build / "CMakeCache.txt", self.editor.project_root)
            if Path(target).resolve(strict=True) not in overlays:
                raise ZephyrError("目标文件已不在当前构建记录中，请刷新设备树。")
            if not self.editor.append_overlay_snippet(target, snippet):
                raise ZephyrError("无法加入 overlay，请确认文件属于当前工程。")
        except (ZephyrError, OSError, RuntimeError, ValueError) as exc:
            self.device_tree.summary.setText(str(exc))
            return
        self.workspace_tabs.setCurrentWidget(self.editor)
        self.statusBar().showMessage("配置已加入编辑器，请检查、保存并重新编译。", 10000)

    def _update_build_dir(self) -> None:
        project = self.editor.project_root
        board = self.board_combo.currentText().strip()
        if project and board:
            self.build_dir.setText(str(default_build_dir(
                project, board, self.environment.workspace if self.environment else None,
                self.environment.sdk if self.environment else None,
            )))
        elif self._auto_build_dir:
            self.build_dir.clear()

    def reset_build_dir(self) -> None:
        self._auto_build_dir = True
        self._update_build_dir()
        self.refresh_runners()

    def choose_build_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择已有构建目录", self.build_dir.text() or str(Path.home()))
        if selected:
            self._auto_build_dir = False
            self.build_dir.setText(selected)
            self.refresh_runners()

    def refresh_runners(self) -> None:
        current = self.runner_combo.currentData()
        text = self.build_dir.text().strip()
        try:
            configuration = read_runner_configuration(text) if text else None
        except ZephyrError as exc:
            self.statusBar().showMessage(f"读取烧录器配置失败：{exc}", 8000)
            configuration = None
        with QSignalBlocker(self.runner_combo):
            self.runner_combo.clear()
            if not configuration or not configuration.available:
                self.runner_combo.addItem("编译后识别", "")
                return
            for runner in configuration.available:
                label = f"{runner}（默认）" if runner == configuration.default else runner
                self.runner_combo.addItem(label, runner)
            desired = current if current in configuration.available else configuration.default
            for index in range(self.runner_combo.count()):
                if self.runner_combo.itemData(index) == desired:
                    self.runner_combo.setCurrentIndex(index)
                    break

    def _prepare_build(self) -> list[str] | None:
        if self._agent_busy or self.operation.is_running or not self._valid_environment():
            return None
        if not self.editor.project_root:
            QMessageBox.warning(self, "没有打开工程", "请先新建或打开一个 Zephyr 工程。")
            return None
        if not self.editor.save_all():
            return None
        board = self.board_combo.currentText().strip()
        build_dir = self.build_dir.text().strip()
        if not board or not build_dir:
            QMessageBox.warning(self, "配置不完整", "请选择开发板并指定构建目录。")
            return None
        try:
            assert self.environment is not None
            validate_build_directory(self.environment, board, self.editor.project_root, build_dir)
            return build_command(self.environment, board, self.editor.project_root, build_dir)
        except ZephyrError as exc:
            QMessageBox.warning(self, "无法编译", "请确认构建目录为空，或属于当前工程、开发板和 Zephyr 环境。也可以点击构建目录右侧的“默认”。\n\n" + str(exc))
            return None

    def build_project(self) -> None:
        self._flash_after_build = False
        command = self._prepare_build()
        if command:
            self._start_operation("编译", command)

    def flash_project(self) -> None:
        command = self._prepare_build()
        if command:
            if self.serial.is_open:
                self.serial.close_port()
                append_output(self.build_output, "[已关闭串口监视，释放设备供烧录使用]\n")
            self._flash_after_build = True
            self._start_operation("编译", command)

    def _start_operation(self, kind: str, command: list[str]) -> None:
        if not self.environment:
            return
        self._operation_kind = kind
        if kind == "编译":
            location = Path(self.build_dir.text().strip()).expanduser()
            if not location.is_absolute():
                location = self.environment.workspace / location
            self._active_build_location = _path_identity(location)
            self._invalid_build_locations.add(self._active_build_location)
            self.device_tree.clear_snapshot("正在编译；成功后可刷新设备树。")
            self._update_serial_diagnostic()
        self.console_tabs.setCurrentIndex(0)
        append_output(self.build_output, f"\n── {kind} ──\n> {subprocess.list2cmdline(command)}\n")
        if not self.operation.start(command, cwd=self.environment.workspace, env=self.environment.process_env()):
            self._flash_after_build = False
            self._operation_state_changed(False)
            self.statusBar().showMessage(f"无法启动{kind}，请检查输出", 10000)
        self._save_settings()

    def _append_operation_output(self, text: str) -> None:
        append_output(self.build_output, text)

    def _operation_state_changed(self, running: bool) -> None:
        busy = running or self._flash_after_build or self._agent_busy
        for button in (self.build_button, self.flash_button, self.new_button, self.open_button, self.env_button, self.setup_button, self.ports_button, self.build_browse, self.build_reset):
            button.setEnabled(not busy)
        self.boards_button.setEnabled(not busy and not self.board_process.is_running)
        for field in (self.board_combo, self.port_combo, self.build_dir, self.runner_combo, self.extra_args, self.workspace_tabs):
            field.setEnabled(not busy)
        self.stop_button.setEnabled(running)
        self.serial.open_button.setEnabled(not busy)
        self.agent_panel.setEnabled(not (running or self._flash_after_build))
        self._update_serial_diagnostic()
        self.statusBar().showMessage(f"正在{self._operation_kind}…" if running else ("Agent 正在处理任务…" if self._agent_busy else ("正在准备烧录…" if busy else "准备就绪")))

    def _agent_prepare(self) -> bool:
        if self.operation.is_running or self._flash_after_build:
            QMessageBox.information(self, "请等待当前操作", "编译或烧录完成后才能使用 Agent。")
            return False
        if not self.editor.project_root:
            QMessageBox.information(self, "请先打开工程", "请先打开一个 Zephyr 工程。")
            return False
        if self.editor.has_unsaved_changes():
            QMessageBox.information(self, "请先保存文件", "编辑器里有未保存的内容。请先保存，再让 Agent 修改工程。")
            return False
        return True

    def _agent_state_changed(self, running: bool) -> None:
        self._agent_busy = running
        self._operation_state_changed(self.operation.is_running)

    def _agent_file_changed(self, path: str) -> None:
        if not self.editor.reload_external_file(path):
            self.statusBar().showMessage(f"文件已修改，请检查编辑器：{path}", 12000)

    def _agent_build_output(self) -> str:
        project = self.editor.project_root
        text = self.build_output.toPlainText()
        start = text.rfind("── 编译 ──")
        if project is None or start < 0:
            return ""
        text = text[start:]
        flash = text.find("── 烧录 ──")
        if flash >= 0:
            text = text[:flash]
        if str(project).replace("\\", "/").casefold() not in text.replace("\\", "/").casefold():
            return ""
        return text

    def _operation_finished(self, code: int, cancelled: bool) -> None:
        kind = self._operation_kind
        outcome = "已停止" if cancelled else ("成功" if code == 0 else f"失败，退出代码 {code}")
        append_output(self.build_output, f"\n[{kind}{outcome}]\n")
        self.statusBar().showMessage(f"{kind}{outcome}", 10000)
        if self._closing:
            self._flash_after_build = False
            return
        if kind == "编译":
            if code == 0 and not cancelled and self._active_build_location is not None:
                self._invalid_build_locations.discard(self._active_build_location)
            self.refresh_runners()
            self._update_serial_diagnostic()
            if code == 0 and not cancelled and self.workspace_tabs.currentWidget() is self.device_tree:
                self._refresh_devicetree()
            if self._flash_after_build and code == 0 and not cancelled:
                QTimer.singleShot(0, self._start_flash)
            else:
                self._flash_after_build = False
                self._operation_state_changed(False)

    def _start_flash(self) -> None:
        if not self.environment:
            self._flash_after_build = False
            self._operation_state_changed(False)
            return
        build_dir = self.build_dir.text().strip()
        self.refresh_runners()
        runner = self.runner_combo.currentData() or None
        port = self.port_combo.currentData() or ""
        # A COM port is meaningful only for certain serial flash runners.
        # SWD/J-Link and other runners select their own device by default.
        if runner not in {"esp32", "stm32flash"}:
            port = ""
        try:
            if not self.editor.project_root:
                raise ZephyrError("请先打开工程。")
            validate_build_directory(
                self.environment, self.board_combo.currentText().strip(),
                self.editor.project_root, build_dir,
            )
            extra = split_runner_arguments(self.extra_args.text())
            command = flash_command(self.environment, build_dir, runner=runner, port=port, extra_args=extra)
        except (ZephyrError, ValueError, OSError) as exc:
            self._flash_after_build = False
            self._operation_state_changed(False)
            append_output(self.build_output, f"[无法准备烧录：{exc}]\n")
            QMessageBox.warning(self, "无法烧录", str(exc))
            return
        self._flash_after_build = False
        self._start_operation("烧录", command)

    def _console_changed(self, index: int) -> None:
        if self.console_tabs.widget(index) is self.shell and not self.shell.runner.is_running:
            self.shell.start_shell()

    def _save_settings(self) -> None:
        if self.environment:
            self.settings.workspace = str(self.environment.workspace)
            self.settings.west = str(self.environment.west)
            self.settings.sdk = str(self.environment.sdk or "")
        self.settings.project = str(self.editor.project_root or "")
        self.settings.board = self.board_combo.currentText().strip()
        self.settings.build_dir = self.build_dir.text().strip()
        self.settings.port = self.port_combo.currentData() or ""
        try:
            save_settings(self.settings)
        except OSError as exc:
            self.statusBar().showMessage(f"无法保存软件设置：{exc}", 10000)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method name
        if self.agent_panel.busy:
            QMessageBox.information(self, "Agent 正在工作", "请先在 Agent 面板点击“停止”，等待任务结束后再关闭。")
            event.ignore()
            return
        if self.operation.is_running and not self._closing:
            answer = QMessageBox.question(
                self, "操作仍在运行", "当前命令仍在运行。停止命令并退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if not self._documents_closed:
            self._save_settings()
            if not self.editor.close_all():
                event.ignore()
                return
            self._documents_closed = True
        self._closing = True
        self._flash_after_build = False
        self.serial.close_port()
        self.shell.close_shell()
        self.board_process.stop()
        self.operation.stop()
        if self.operation.is_running or self.board_process.is_running or self.shell.runner.is_running:
            event.ignore()
            QTimer.singleShot(150, self.close)
        else:
            event.accept()
