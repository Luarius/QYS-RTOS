"""Search, select and install boards through the existing PlatformIO workbench."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHeaderView, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .board_catalog import (
    CATALOG_LIMIT, BoardCatalogError, CatalogBoard, boards_catalog_command, builtin_catalog,
    load_catalog, parse_catalog, save_catalog,
)
from .board_support import BoardExtension, pio_environment
from .processes import CommandRunner


_FRAMEWORK_NAMES = {"": "平台默认", "arduino": "Arduino", "espidf": "ESP-IDF",
                    "stm32cube": "STM32Cube", "zephyr": "Zephyr（PlatformIO）",
                    "cmsis": "CMSIS", "mbed": "Mbed"}


def _plain_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class BoardCatalogDialog(QDialog):
    def __init__(self, core: Path | None, cache_path: Path, *, selected_board: str = "",
                 default_action: str = "install", parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("板卡扩展中心 · PlatformIO")
        self.setMinimumSize(750, 490)
        self.resize(965, 600)
        self.core = core
        self.cache_path = cache_path
        self.action = ""
        self.extension: BoardExtension | None = None
        self._preferred_board = selected_board.casefold()
        self._builtin = {board.key: board for board in builtin_catalog()}
        self._online = {board.key: board for board in load_catalog(cache_path)}
        self._local: dict[tuple[str, str], CatalogBoard] = {}
        self._phase = ""
        self._close_pending = False
        self._output: list[str] = []
        self._output_size = 0
        self._run_issue = ""

        self.runner = CommandRunner(self)
        self.runner.output.connect(self._append_output)
        self.runner.finished.connect(self._read_finished)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._read_timed_out)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)
        title = _plain_label("板卡扩展中心")
        title.setObjectName("catalogTitle")
        outer.addWidget(title)
        subtitle = _plain_label("按板型或芯片搜索，选择框架后下载支持包或新建工程。")
        subtitle.setObjectName("mutedLabel")
        outer.addWidget(subtitle)

        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setAccessibleName("搜索开发板")
        self.search.setPlaceholderText("搜索板卡、芯片、平台或框架")
        self.search.setClearButtonEnabled(True)
        toolbar.addWidget(self.search, 1)
        self.scope = QComboBox()
        self.scope.setAccessibleName("板卡目录筛选")
        self.scope.addItem("全部目录", "all")
        self.scope.addItem("本机平台", "local")
        self.scope.addItem("内置模板", "builtin")
        toolbar.addWidget(self.scope)
        self.refresh = QPushButton("刷新在线目录")
        self.refresh.setToolTip("通过 PlatformIO Core 读取 Registry 中的开发板目录。")
        self.refresh.clicked.connect(lambda: self._read_catalog("online"))
        toolbar.addWidget(self.refresh)
        outer.addLayout(toolbar)

        content = QSplitter(Qt.Orientation.Horizontal)
        content.setChildrenCollapsible(False)
        self.boards = QTreeWidget()
        self.boards.setObjectName("catalogBoards")
        self.boards.setAccessibleName("匹配的开发板")
        self.boards.setHeaderLabels(["开发板", "平台", "本机"])
        self.boards.setRootIsDecorated(False)
        self.boards.setAlternatingRowColors(True)
        self.boards.setUniformRowHeights(True)
        self.boards.header().setStretchLastSection(False)
        self.boards.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.boards.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.boards.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        content.addWidget(self.boards)

        detail = QFrame()
        detail.setObjectName("controlBar")
        detail.setMinimumWidth(245)
        panel = QVBoxLayout(detail)
        panel.setContentsMargins(16, 15, 16, 15)
        panel.setSpacing(9)
        self.board_title = _plain_label()
        self.board_title.setObjectName("catalogBoardTitle")
        panel.addWidget(self.board_title)
        self.board_metadata = _plain_label()
        self.board_metadata.setObjectName("mutedLabel")
        self.board_metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        panel.addWidget(self.board_metadata)
        panel.addSpacing(8)
        self.framework = QComboBox()
        self.framework.setAccessibleName("项目框架")
        framework_label = QLabel("项目框架")
        framework_label.setBuddy(self.framework)
        panel.addWidget(framework_label)
        panel.addWidget(self.framework)
        self.description = _plain_label()
        panel.addWidget(self.description)
        self.source_note = _plain_label()
        self.source_note.setObjectName("mutedLabel")
        panel.addWidget(self.source_note)
        panel.addStretch(1)
        content.addWidget(detail)
        content.setSizes([600, 315])
        outer.addWidget(content, 1)

        self.status = _plain_label()
        self.status.setObjectName("mutedLabel")
        outer.addWidget(self.status)
        buttons = QHBoxLayout()
        self.configure = QPushButton("安装 PlatformIO Core")
        self.configure.clicked.connect(lambda: self._choose("core"))
        self.configure.setVisible(core is None)
        buttons.addWidget(self.configure)
        buttons.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        self.new_project = QPushButton("新建工程")
        self.new_project.clicked.connect(lambda: self._choose("new"))
        buttons.addWidget(self.new_project)
        self.install = QPushButton("下载支持包")
        self.install.clicked.connect(lambda: self._choose("install"))
        buttons.addWidget(self.install)
        primary = self.new_project if default_action == "new" else self.install
        primary.setObjectName("primaryButton")
        primary.setDefault(True)
        outer.addLayout(buttons)

        self.search.textChanged.connect(self._fill_boards)
        self.scope.currentIndexChanged.connect(self._fill_boards)
        self.boards.currentItemChanged.connect(self._board_changed)
        self.framework.currentIndexChanged.connect(self._update_detail)
        self._fill_boards()
        self.search.setFocus()
        if core:
            QTimer.singleShot(0, lambda: self._read_catalog("local") if self.isVisible() else None)

    def _current_board(self) -> CatalogBoard | None:
        item = self.boards.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _fill_boards(self, *_args) -> None:
        selected = self._current_board()
        framework = self.framework.currentData()
        scope = self.scope.currentData()
        if scope == "local":
            records = self._local
        elif scope == "builtin":
            records = self._builtin
        else:
            records = {**self._builtin, **self._online}
            for key, board in self._local.items():
                records.setdefault(key, board)

        terms = self.search.text().casefold().split()
        items = []
        current = None
        for board in sorted(records.values(), key=lambda b: (b.key not in self._builtin, b.title.casefold(), b.key)):
            haystack = " ".join((board.title, board.board, board.mcu, board.vendor,
                                 board.platform, *board.frameworks)).casefold()
            if not all(term in haystack for term in terms):
                continue
            item = QTreeWidgetItem([board.title, board.platform, "已有" if board.key in self._local else ""])
            item.setData(0, Qt.ItemDataRole.UserRole, board)
            items.append(item)
            if (selected and board.key == selected.key) or (not selected and board.board.casefold() == self._preferred_board):
                current = item

        self.boards.blockSignals(True)
        self.boards.setUpdatesEnabled(False)
        self.boards.clear()
        self.boards.addTopLevelItems(items)
        self.boards.setCurrentItem(current or (items[0] if items else None))
        self.boards.setUpdatesEnabled(True)
        self.boards.blockSignals(False)
        self._board_changed(preferred_framework=framework if current else None)
        self._show_status()

    def _board_changed(self, *_args, preferred_framework: str | None = None) -> None:
        board = self._current_board()
        self.framework.blockSignals(True)
        self.framework.clear()
        if board:
            for framework in board.frameworks or ("",):
                self.framework.addItem(_FRAMEWORK_NAMES.get(framework, framework), framework)
            index = self.framework.findData(preferred_framework)
            if index >= 0:
                self.framework.setCurrentIndex(index)
        self.framework.blockSignals(False)
        self._update_detail()

    def _update_detail(self, *_args) -> None:
        board = self._current_board()
        self.refresh.setEnabled(bool(self.core) and not self._phase)
        self.framework.setEnabled(board is not None)
        self.new_project.setEnabled(board is not None and not self._phase)
        self.install.setEnabled(board is not None and bool(self.core) and not self._phase)
        if board is None:
            self.board_title.setText("没有匹配的开发板")
            self.board_metadata.clear()
            self.description.setText("换个关键词，或刷新在线目录以读取更多板型。")
            self.source_note.clear()
            return
        profile = board.extension(self.framework.currentData() or "")
        self.board_title.setText(board.title)
        self.board_metadata.setText("\n".join(filter(None, [board.board, profile.platform, board.mcu, board.vendor])))
        self.description.setText(profile.description)
        local = "本机已有该板型的平台。首次编译仍可能下载框架或工具。" if board.key in self._local else "支持包由 PlatformIO 按框架解析，首次下载需要联网。"
        self.source_note.setText(local)

    def _show_status(self, note: str = "") -> None:
        if self._phase:
            self.status.setText("正在读取在线目录…" if self._phase == "online" else "正在检查本机平台…")
        elif note:
            self.status.setText(note)
        else:
            detail = f"匹配 {self.boards.topLevelItemCount()} 个板型 · 本机 {len(self._local)} 个"
            if self._online:
                detail += f" · 缓存 {len(self._online)} 个"
            elif self.core:
                detail += " · 点击刷新读取在线目录"
            else:
                detail += " · 安装 Core 后可获取完整目录"
            self.status.setText(detail)

    def _read_catalog(self, phase: str) -> None:
        if self._phase or self.core is None:
            return
        self._phase = phase
        self._output.clear()
        self._output_size = 0
        self._run_issue = ""
        self._show_status()
        self._update_detail()
        if self.runner.start(boards_catalog_command(self.core, installed=phase == "local"),
                             cwd=Path.home(), env=pio_environment()):
            self._deadline.start(45000)
        else:
            self._read_finished(1, False)

    def _append_output(self, output: str) -> None:
        self._output_size += len(output)
        if self._output_size > CATALOG_LIMIT:
            self._run_issue = "板卡目录过大，已保留先前目录。"
            if self.runner.is_running:
                self.runner.stop()
        else:
            self._output.append(output)

    def _read_timed_out(self) -> None:
        self._run_issue = "读取板卡目录超时，已保留先前目录。"
        self.runner.stop()

    def _read_finished(self, code: int, cancelled: bool) -> None:
        phase, self._phase = self._phase, ""
        self._deadline.stop()
        if self._close_pending:
            super().reject()
            return
        note = self._run_issue
        if not note and (code or cancelled):
            note = ("在线目录读取失败，可使用内置模板、本机平台与缓存。"
                    if phase == "online" else "本机平台读取失败，可刷新在线目录或选内置模板。")
        if not note:
            try:
                boards = parse_catalog("".join(self._output))
                if phase == "local":
                    self._local = {board.key: board for board in boards}
                elif boards:
                    self._online = {board.key: board for board in boards}
                    try:
                        save_catalog(self.cache_path, boards)
                    except OSError:
                        note = "在线目录已读取，未能写入离线缓存。"
                else:
                    note = "在线目录返回空列表，已保留先前目录。"
            except BoardCatalogError as exc:
                note = str(exc)
        self._output.clear()
        self._fill_boards()
        if note:
            self._show_status(note)

    def _choose(self, action: str) -> None:
        if self._phase:
            return
        if action != "core":
            board = self._current_board()
            if board is None or (action == "install" and self.core is None):
                return
            self.extension = board.extension(self.framework.currentData() or "")
        self.action = action
        self.accept()

    def reject(self) -> None:
        if self._phase:
            self._close_pending = True
            self.runner.stop()
        else:
            super().reject()
