"""Ordinary MCU workbench, using PlatformIO independently of west."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from serial.tools import list_ports
from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QMenu, QMessageBox, QPushButton, QSizePolicy, QSplitter, QTabWidget,
    QVBoxLayout, QWidget,
)

from .board_support import (
    BOARD_EXTENSIONS, BoardExtension, BoardSupportError, PioEnvironment, config_command,
    create_board_project, extension_install_command, find_core, managed_core_paths,
    parse_project_config, pio_environment, run_command, upload_port_for,
    validate_board_project,
)
from .bootstrap import find_python
from .config import AppSettings, settings_path
from .editor import EditorWorkspace
from .panels import SerialPanel, append_output, output_box
from .processes import CommandRunner


class BoardWorkspace(QWidget):
    busy_changed = Signal(bool)
    status_changed = Signal(str)
    settings_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.config_dir = settings_path().parent
        self.core = find_core(self.config_dir, settings.platformio)
        self.runner = CommandRunner(self)
        self._task = ""
        self._caption = ""
        self._cancel_requested = False
        self._captured: list[str] = []
        self._after_config: tuple[str, bool] | None = None
        self._follow_up_paused = False
        self._extension_directory: TemporaryDirectory | None = None
        self._selected_core: Path | None = None
        self._closing = False
        self._activated = False
        self._build_interface()
        self.runner.output.connect(self._on_output)
        self.runner.finished.connect(self._on_finished)
        self._update_controls()

    @property
    def busy(self) -> bool:
        return bool(self._task or self._after_config)

    def activate(self) -> None:
        self.refresh_ports()
        if self._activated:
            return
        self._activated = True
        if self.settings.ordinary_project:
            try:
                self.set_project(validate_board_project(self.settings.ordinary_project))
            except BoardSupportError as exc:
                self._notice(str(exc))

    def shut_down(self) -> None:
        self._closing = True
        self.serial.close_port()
        self.stop()

    def pause_follow_up(self, paused: bool) -> None:
        self._follow_up_paused = paused
        if not paused:
            self._run_after_inspection()
            self._update_controls()

    def stop(self) -> None:
        if self.busy:
            self._after_config = None
            self._cancel_requested = True
            self.runner.stop()
            self._update_controls()

    def _build_interface(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        header = QHBoxLayout()
        self.core_button = QPushButton()
        core_menu = QMenu(self.core_button)
        core_menu.addAction("自动检测", self._detect_core)
        core_menu.addAction("选择 pio.exe…", self._choose_core)
        core_menu.addAction("安装 PlatformIO Core…", self._install_core)
        self.core_button.setMenu(core_menu)
        self.extensions_button = QPushButton("板卡扩展…")
        self.new_button = QPushButton("＋  新建普通工程")
        self.open_button = QPushButton("打开普通工程")
        header.addWidget(self.core_button)
        header.addWidget(self.extensions_button)
        header.addStretch(1)
        header.addWidget(self.new_button)
        header.addWidget(self.open_button)
        outer.addLayout(header)

        bar = QFrame()
        bar.setObjectName("controlBar")
        controls = QHBoxLayout(bar)
        controls.setContentsMargins(12, 8, 12, 8)
        controls.setSpacing(8)
        self.environment_combo = QComboBox()
        self.environment_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.environment_combo.setMinimumContentsLength(14)
        self.environment_combo.setAccessibleName("PlatformIO 构建环境")
        self.read_button = QPushButton("读取配置")
        self.read_button.setToolTip("保存编辑内容并重新读取 platformio.ini 中的构建环境")
        self.port_combo = QComboBox()
        self.port_combo.setMinimumContentsLength(8)
        self.port_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.port_combo.setAccessibleName("普通单片机串口")
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setToolTip("重新读取串口设备")
        self.build_button = QPushButton("编译")
        self.build_button.setObjectName("buildButton")
        self.flash_button = QPushButton("▶  烧录普通板卡")
        self.flash_button.setObjectName("primaryButton")
        self.stop_button = QPushButton("停止")
        controls.addWidget(QLabel("构建环境"))
        controls.addWidget(self.environment_combo, 2)
        controls.addWidget(self.read_button)
        controls.addWidget(QLabel("串口"))
        controls.addWidget(self.port_combo, 1)
        controls.addWidget(self.refresh_button)
        for button in (self.build_button, self.flash_button, self.stop_button):
            controls.addWidget(button)
        outer.addWidget(bar)

        self.project_label = QLabel("新建普通工程，或打开含 platformio.ini 的文件夹")
        self.project_label.setObjectName("projectPath")
        self.project_label.setTextFormat(Qt.TextFormat.PlainText)
        self.project_label.setWordWrap(True)
        self.project_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.project_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.board_label = QLabel()
        self.board_label.setObjectName("mutedLabel")
        self.board_label.setTextFormat(Qt.TextFormat.PlainText)
        self.board_label.setWordWrap(True)
        self.board_label.hide()
        outer.addWidget(self.project_label)
        outer.addWidget(self.board_label)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        self.editor = EditorWorkspace(
            project_validator=validate_board_project, start_files=("src/main.c", "src/main.cpp", "platformio.ini"),
        )
        splitter.addWidget(self.editor)
        console = QFrame()
        console.setObjectName("consolePanel")
        console_layout = QVBoxLayout(console)
        console_layout.setContentsMargins(10, 7, 10, 8)
        self.console_tabs = QTabWidget()
        self.console_tabs.setDocumentMode(True)
        self.output = output_box("PlatformIO 的下载、编译和烧录输出显示在这里。")
        self.serial = SerialPanel(zephyr_diagnostics=False)
        self.console_tabs.addTab(self.output, "输出")
        self.console_tabs.addTab(self.serial, "串口监视")
        console_layout.addWidget(self.console_tabs)
        splitter.addWidget(console)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([460, 220])
        outer.addWidget(splitter, 1)

        self.extensions_button.clicked.connect(self._show_extensions)
        self.new_button.clicked.connect(self.new_project)
        self.open_button.clicked.connect(self.open_project)
        self.read_button.clicked.connect(self._read_config)
        self.refresh_button.clicked.connect(self.refresh_ports)
        self.build_button.clicked.connect(self.build_project)
        self.flash_button.clicked.connect(self.flash_project)
        self.stop_button.clicked.connect(self.stop)
        self.port_combo.currentIndexChanged.connect(self._port_changed)
        self.environment_combo.currentIndexChanged.connect(self._environment_changed)
        self.editor.status_changed.connect(self.status_changed)

    def _update_controls(self) -> None:
        idle = not self.busy and not self._closing
        has_project = self.editor.project_root is not None
        ready = idle and self.core is not None and self._environment() is not None
        self.core_button.setText("PlatformIO Core 已找到" if self.core else "配置 PlatformIO Core")
        self.core_button.setToolTip(str(self.core or "检测、选择或安装 PlatformIO Core"))
        for widget in (self.core_button, self.new_button, self.open_button, self.extensions_button,
                       self.port_combo, self.refresh_button, self.environment_combo, self.editor):
            widget.setEnabled(idle)
        self.read_button.setEnabled(idle and has_project and self.core is not None)
        self.build_button.setEnabled(ready)
        self.flash_button.setEnabled(ready)
        self.stop_button.setEnabled(self.busy and not self._closing)
        self.serial.open_button.setEnabled(idle)
        self.busy_changed.emit(self.busy)

    def _notice(self, message: str) -> None:
        append_output(self.output, message + "\n")
        self.status_changed.emit(message)

    def _notice_dialog(self, title: str, message: str) -> None:
        self._notice(message)
        QMessageBox.warning(self, title, message)

    def _choose_extension(self, *, install: bool) -> BoardExtension | None:
        dialog = QDialog(self)
        dialog.setWindowTitle("板卡扩展" if install else "新建普通单片机工程")
        dialog.setMinimumWidth(490)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        combo = QComboBox()
        combo.setAccessibleName("板卡支持包")
        combo.addItems([extension.title for extension in BOARD_EXTENSIONS])
        detail = QLabel()
        detail.setMinimumHeight(65)
        detail.setTextFormat(Qt.TextFormat.PlainText)
        detail.setWordWrap(True)

        def show_board(index: int) -> None:
            entry = BOARD_EXTENSIONS[index]
            detail.setText(f"{entry.description}\n{entry.platform}")

        combo.currentIndexChanged.connect(show_board)
        current = self._environment()
        if current and current.extension:
            combo.setCurrentIndex(BOARD_EXTENSIONS.index(current.extension))
        show_board(combo.currentIndex())
        layout.addWidget(combo)
        layout.addWidget(detail)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(dialog.reject)
        accept = QPushButton("下载支持包" if install else "继续")
        accept.setObjectName("primaryButton")
        accept.clicked.connect(dialog.accept)
        accept.setDefault(True)
        buttons.addWidget(cancel)
        buttons.addWidget(accept)
        layout.addLayout(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return BOARD_EXTENSIONS[combo.currentIndex()]
        return None

    def _show_extensions(self) -> None:
        extension = self._choose_extension(install=True)
        if extension:
            self.install_extension(extension)

    def install_extension(self, extension: BoardExtension) -> None:
        if self.busy or not self._require_core():
            return
        try:
            self._extension_directory = TemporaryDirectory(prefix="qys-board-support-")
            project = create_board_project(Path(self._extension_directory.name) / "support", extension)
        except (OSError, BoardSupportError) as exc:
            self._cleanup_extension()
            self._notice_dialog("无法准备扩展", str(exc))
            return
        assert self.core is not None
        self._start("extension", f"下载扩展 · {extension.title}", extension_install_command(self.core, project), project)

    def _cleanup_extension(self) -> None:
        if self._extension_directory is None:
            return
        directory, self._extension_directory = self._extension_directory, None
        try:
            directory.cleanup()
        except OSError as exc:
            self._notice(f"无法清理下载临时目录：{exc}")

    def new_project(self) -> None:
        extension = self._choose_extension(install=False)
        if extension is None:
            return
        start = self.editor.project_root.parent if self.editor.project_root else Path.home()
        parent = QFileDialog.getExistingDirectory(self, "选择新工程的上级文件夹", str(start))
        if not parent:
            return
        name, ok = QInputDialog.getText(self, "新建普通工程", "工程文件夹名称", text="my_mcu_project")
        if not ok:
            return
        name = name.strip()
        if (not name or name.endswith(".") or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                or re.fullmatch(r"(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?", name, re.I)):
            self._notice_dialog("名称不正确", "请输入有效的 Windows 文件夹名称。")
            return
        try:
            self.set_project(create_board_project(Path(parent) / name, extension))
        except BoardSupportError as exc:
            self._notice_dialog("无法创建工程", str(exc))

    def open_project(self) -> None:
        start = self.editor.project_root or Path.home()
        chosen = QFileDialog.getExistingDirectory(self, "打开含 platformio.ini 的工程", str(start))
        if chosen:
            self.set_project(Path(chosen))

    def set_project(self, project: Path) -> bool:
        if self.busy or not self.editor.set_project(project):
            return False
        assert self.editor.project_root is not None
        root = self.editor.project_root
        if self.settings.ordinary_project != str(root):
            self.settings.ordinary_environment = ""
        self.settings.ordinary_project = str(root)
        self.project_label.setText(f"工程  /  {root}")
        with QSignalBlocker(self.environment_combo):
            self.environment_combo.clear()
        self._update_board_label()
        self._update_controls()
        self.settings_changed.emit()
        if self.core:
            self._inspect_config()
        return True

    def refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        if current is None:
            current = self.settings.ordinary_port
        try:
            ports = sorted(list_ports.comports(), key=lambda port: port.device)
        except (OSError, ValueError) as exc:
            self._notice(f"读取串口失败：{exc}")
            return
        with QSignalBlocker(self.port_combo):
            self.port_combo.clear()
            self.port_combo.addItem("未选择", "")
            for port in ports:
                self.port_combo.addItem(f"{port.device} · {port.description}", port.device)
            self.port_combo.setCurrentIndex(max(0, self.port_combo.findData(current)))
        self._port_changed()

    def _port_changed(self, *_args) -> None:
        port = self.port_combo.currentData() or ""
        if self.serial.selected_port != port:
            self.serial.close_port()
        self.serial.set_port(port)
        self.settings.ordinary_port = port

    def _environment(self) -> PioEnvironment | None:
        return self.environment_combo.currentData()

    def _update_board_label(self) -> None:
        selected = self._environment()
        if selected and selected.extension and selected.protocol == selected.extension.upload_protocol:
            self.board_label.setText(selected.extension.description)
        elif selected:
            self.board_label.setText(f"{selected.board or selected.platform}  ·  {selected.protocol or 'PlatformIO 默认烧录器'}")
        self.board_label.setVisible(selected is not None)

    def _environment_changed(self, *_args) -> None:
        selected = self._environment()
        if selected:
            self.settings.ordinary_environment = selected.name
        self._update_board_label()
        self._update_controls()

    def _require_core(self) -> bool:
        if self.core and self.core.is_file():
            return True
        self.core = None
        self._update_controls()
        self._notice_dialog("需要 PlatformIO Core", "请从「配置 PlatformIO Core」菜单自动检测、选择或安装 Core。")
        return False

    def _detect_core(self) -> None:
        self.core = find_core(self.config_dir)
        self.settings.platformio = ""
        self.settings_changed.emit()
        self._update_controls()
        if self.core and self.editor.project_root:
            self._inspect_config()
        elif self.core is None:
            self._notice("未找到 PlatformIO Core。可从菜单选择 pio.exe 或安装 Core。")

    def _choose_core(self) -> None:
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "选择 PlatformIO 命令", str(self.core.parent if self.core else Path.home()),
            "PlatformIO (pio.exe platformio.exe)" if os.name == "nt" else "PlatformIO (pio platformio)",
        )
        if chosen:
            self._selected_core = Path(chosen).resolve()
            self._start("probe", "检查 PlatformIO Core", [str(self._selected_core), "--version"], self._selected_core.parent)

    def _install_core(self) -> None:
        if self.busy:
            return
        environment = pio_environment()
        python, _core = managed_core_paths(self.config_dir)
        launcher = find_python(environment)
        if launcher is None:
            self._notice_dialog("需要 Python", "请安装 Python 3.12 或以上版本，或手动选择已有的 pio.exe。")
            return
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._notice_dialog("无法建立安装目录", str(exc))
            return
        self._start("venv", "准备 PlatformIO 环境", [*launcher, "-m", "venv", str(python.parent.parent)], self.config_dir)

    def _install_core_package(self, python: Path) -> None:
        self._start(
            "core", "安装 PlatformIO Core",
            [str(python), "-m", "pip", "install", "platformio>=6.1.18,<7"], self.config_dir,
        )

    def _read_config(self) -> None:
        if self.editor.save_all():
            self._inspect_config()

    def _inspect_config(self) -> None:
        root = self.editor.project_root
        if root and self.core:
            self._start("config", "读取 PlatformIO 构建环境", config_command(self.core, root), root)

    def build_project(self) -> None:
        self._prepare_run(upload=False)

    def flash_project(self) -> None:
        self._prepare_run(upload=True)

    def _prepare_run(self, *, upload: bool) -> None:
        selected = self._environment()
        if self.busy or selected is None or not self._require_core() or not self.editor.save_all():
            return
        self._after_config = (selected.name, upload)
        self._inspect_config()

    def _run_project(self, name: str, upload: bool) -> None:
        selected, root = self._environment(), self.editor.project_root
        if selected is None or selected.name != name or root is None or self.core is None:
            self._notice("所选构建环境已变更，请选定环境后重试。")
            return
        caption = "烧录普通板卡" if upload else "编译普通工程"
        port = ""
        if upload:
            try:
                port = upload_port_for(selected, self.port_combo.currentData() or "")
            except BoardSupportError as exc:
                self._notice_dialog("请选择烧录串口", str(exc))
                return
            target = port or selected.upload_port
            if selected.uses_probe:
                target = f"{selected.protocol}（自动查找探针）"
            elif not target:
                target = f"{selected.protocol or '默认烧录器'}（由 PlatformIO 自动选择设备）"
            answer = QMessageBox.question(
                self, "确认烧录普通板卡",
                f"工程  {root}\n环境  {name}\n板卡  {selected.board or selected.platform}"
                f"\n烧录目标  {target}\n\n烧录会写入所选板卡的固件。继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes or self._closing:
                return
            if self.serial.is_open:
                self.serial.close_port()
                self._notice("已关闭串口监视，释放端口供烧录使用。")
        self.settings_changed.emit()
        self._start("upload" if upload else "build", caption,
                    run_command(self.core, root, name, upload=upload, port=port), root)

    def _run_after_inspection(self) -> None:
        if self._closing or self._task or self._follow_up_paused:
            return
        request, self._after_config = self._after_config, None
        if request and self._environment():
            self._run_project(*request)

    def _start(self, task: str, caption: str, argv: list[str], cwd: Path) -> None:
        if self._task or self._closing:
            return
        self._task = task
        self._caption = caption
        self._cancel_requested = False
        self._captured.clear()
        self._update_controls()
        self.console_tabs.setCurrentIndex(0)
        self._notice(f"── {caption} ──")
        append_output(self.output, f"> {subprocess.list2cmdline(argv)}\n")
        if not self.runner.start(argv, cwd=cwd, env=pio_environment()):
            self._on_finished(1, False)

    def _on_output(self, text: str) -> None:
        if self._task in {"probe", "config"}:
            self._captured.append(text)
        else:
            append_output(self.output, text)

    def _on_finished(self, code: int, cancelled: bool) -> None:
        task, self._task = self._task, ""
        cancelled = cancelled or self._cancel_requested
        if task == "extension":
            self._cleanup_extension()
        if self._closing:
            self._update_controls()
            return
        if task == "config":
            self._config_finished(code, cancelled)
            if code or cancelled:
                self._after_config = None
            self._run_after_inspection()
        elif cancelled or code:
            if task == "probe":
                append_output(self.output, "".join(self._captured))
            self._notice(f"{self._caption}{'已停止' if cancelled else f'失败（退出代码 {code}）'}")
        elif task == "venv":
            python, _core = managed_core_paths(self.config_dir)
            self._install_core_package(python)
        elif task == "core":
            _python, self._selected_core = managed_core_paths(self.config_dir)
            self._start("probe", "检查 PlatformIO Core",
                        [str(self._selected_core), "--version"], self.config_dir)
        elif task == "probe":
            if self._selected_core and re.search(r"PlatformIO Core, version (?:6|[7-9])\.", "".join(self._captured)):
                self.core = self._selected_core
                self.settings.platformio = str(self.core)
                self.settings_changed.emit()
                self._notice("PlatformIO Core 可用。")
                self._inspect_config()
            else:
                self._notice("该命令未返回有效的 PlatformIO Core 6+ 版本信息。")
        else:
            self._notice(f"{self._caption}成功")
        self._update_controls()

    def _config_finished(self, code: int, cancelled: bool) -> None:
        settings_choice = self.settings.ordinary_environment
        environments = ()
        if cancelled:
            self._notice("已停止读取构建环境。")
            return
        if code:
            append_output(self.output, "".join(self._captured))
            self._notice("读取构建环境失败，请检查 platformio.ini 和 PlatformIO Core。")
        else:
            try:
                environments = parse_project_config("".join(self._captured))
            except BoardSupportError as exc:
                self._notice(str(exc))
        with QSignalBlocker(self.environment_combo):
            self.environment_combo.clear()
            for index, environment in enumerate(environments):
                self.environment_combo.addItem(environment.name, environment)
                if environment.name == settings_choice:
                    self.environment_combo.setCurrentIndex(index)
        selected = self._environment()
        if selected:
            self.settings.ordinary_environment = selected.name
        self._update_board_label()
        if environments:
            self._notice(f"已读取 {len(environments)} 个构建环境。")
