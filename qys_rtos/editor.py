from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PySide6.QtCore import (
    QDir,
    QFileInfo,
    QModelIndex,
    QRect,
    QSize,
    Qt,
    QSortFilterProxyModel,
    QRegularExpression,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFontDatabase,
    QKeySequence,
    QPainter,
    QTextCursor,
    QTextCharFormat,
    QSyntaxHighlighter,
    QShortcut,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QFileSystemModel,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from qys_rtos.project import (
    FileChangedError,
    FileSnapshot,
    ProjectError,
    read_text_file,
    save_text_file,
    validate_project,
)


class _ProjectTreeFilter(QSortFilterProxyModel):
    """Hide noisy folders by default while keeping them one click away."""

    _OUTPUT_DIRS = {"build", "dist", "out", "__pycache__", ".cache", ".venv"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.show_hidden = False
        self.root_path: Path | None = None
        self.setRecursiveFilteringEnabled(True)

    def set_root_path(self, path: Path) -> None:
        if self.root_path == path:
            return
        self._apply_filter_change(lambda: setattr(self, "root_path", path))

    def set_show_hidden(self, visible: bool) -> None:
        if self.show_hidden == visible:
            return
        self._apply_filter_change(lambda: setattr(self, "show_hidden", visible))

    def _apply_filter_change(self, update) -> None:
        begin_change = getattr(self, "beginFilterChange", None)
        end_change = getattr(self, "endFilterChange", None)
        direction_type = getattr(QSortFilterProxyModel, "Direction", None)
        rows_direction = getattr(direction_type, "Rows", None)
        if callable(begin_change) and callable(end_change) and rows_direction is not None:
            begin_change()
            update()
            end_change(rows_direction)
        else:
            update()
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        source = self.sourceModel()
        if source is None:
            return True
        index = source.index(source_row, 0, source_parent)
        if not index.isValid():
            return False
        info = source.fileInfo(index)
        if self.show_hidden:
            return True
        path = Path(source.filePath(index))
        current = path if info.isDir() else path.parent
        if self.root_path is not None:
            try:
                current.relative_to(self.root_path)
            except ValueError:
                return True

        while current != self.root_path and current.parent != current:
            name = current.name
            normalized = name.casefold()
            is_build_output = (
                normalized in self._OUTPUT_DIRS
                or normalized.startswith(("build-", "build_", "cmake-build-"))
            )
            if name.startswith(".") or QFileInfo(str(current)).isHidden() or is_build_output:
                return False
            current = current.parent
        return True


@dataclass
class _OpenDocument:
    path: Path
    snapshot: FileSnapshot
    editor: _CodeEditor


class _LineNumberArea(QWidget):
    def __init__(self, editor: _CodeEditor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual method name
        self._editor.paint_line_numbers(event)


class _SyntaxHighlighter(QSyntaxHighlighter):
    _C_KEYWORDS = (
        "auto break case char const continue default do double else enum extern float for goto "
        "if inline int long register restrict return short signed sizeof static struct switch "
        "typedef union unsigned void volatile while bool true false nullptr"
    ).split()
    _C_TYPES = (
        "size_t ssize_t ptrdiff_t uint8_t uint16_t uint32_t uint64_t int8_t int16_t int32_t "
        "int64_t k_timeout_t k_tid_t device struct"
    ).split()

    def __init__(self, document, suffix: str, file_name: str = "") -> None:
        super().__init__(document)
        self._kind = self._language_kind(suffix, file_name)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        self._comment_format = self._format("#91a3b8", italic=True)

        self._add_rule(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', "#d7ba7d")
        self._add_rule(r"\b(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)\b", "#b5cea8")

        if self._kind == "config":
            self._add_rule(r"^\s*CONFIG_[A-Za-z0-9_]+", "#d9a7e6", bold=True)
            self._add_rule(r"\b(?:y|m|n)\b", "#4ec9b0", bold=True)
            self._add_rule(r"^\s*#.*$", "#91a3b8", italic=True)
        elif self._kind == "cmake":
            self._add_rule(r"\b(?:cmake_minimum_required|project|find_package|target_[A-Za-z_]+|add_[A-Za-z_]+|set|include|if|endif)\b", "#dcdcaa", bold=True)
            self._add_rule(r"^\s*#.*$", "#91a3b8", italic=True)
        else:
            self._add_rule(r"\b[A-Za-z_][A-Za-z_0-9]*(?=\s*\()", "#dcdcaa")
            self._add_rule(r"\b(?:" + "|".join(map(re.escape, self._C_KEYWORDS)) + r")\b", "#c792ea", bold=True)
            self._add_rule(r"\b(?:" + "|".join(map(re.escape, self._C_TYPES)) + r")\b", "#4ec9b0")
            self._add_rule(r"^\s*#\s*[A-Za-z_]+", "#d9a7e6", bold=True)
            if self._kind == "dts":
                self._add_rule(r"&[A-Za-z_][A-Za-z_0-9,.-]*", "#75cfff", bold=True)
                self._add_rule(r"^\s*[A-Za-z_][A-Za-z_0-9,.-]*(?=\s*=)", "#75cfff")
            self._add_rule(r"//[^\n]*", "#91a3b8", italic=True)

        self._multi_comment = QRegularExpression(r"/\*")
        self._comment_end = QRegularExpression(r"\*/")

    @staticmethod
    def _language_kind(suffix: str, file_name: str) -> Literal["c", "dts", "config", "cmake"]:
        suffix = suffix.casefold()
        file_name = file_name.casefold()
        if suffix in {".dts", ".dtsi", ".overlay"}:
            return "dts"
        if suffix in {".conf", ".config", ".kconfig"} or file_name == "kconfig":
            return "config"
        if suffix == ".cmake" or file_name == "cmakelists.txt":
            return "cmake"
        return "c"

    @staticmethod
    def _format(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
        char_format = QTextCharFormat()
        char_format.setForeground(QColor(color))
        char_format.setFontWeight(700 if bold else 400)
        char_format.setFontItalic(italic)
        return char_format

    def _add_rule(self, pattern: str, color: str, *, bold: bool = False, italic: bool = False) -> None:
        self._rules.append((QRegularExpression(pattern), self._format(color, bold=bold, italic=italic)))

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt virtual method name
        for expression, char_format in self._rules:
            match = expression.globalMatch(text)
            while match.hasNext():
                item = match.next()
                self.setFormat(item.capturedStart(), item.capturedLength(), char_format)

        start = 0 if self.previousBlockState() == 1 else self._multi_comment.match(text).capturedStart()
        while start >= 0:
            end_match = self._comment_end.match(text, start)
            if end_match.hasMatch():
                end = end_match.capturedEnd()
                self.setFormat(start, end - start, self._comment_format)
                start = self._multi_comment.match(text, end).capturedStart()
                self.setCurrentBlockState(0)
            else:
                self.setFormat(start, len(text) - start, self._comment_format)
                self.setCurrentBlockState(1)
                break
        else:
            self.setCurrentBlockState(0)


class _CodeEditor(QPlainTextEdit):
    def __init__(self, suffix: str, file_name: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        if not font.family():
            font.setFamily("Consolas")
        font.setPointSize(max(font.pointSize(), 10))
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self._line_number_area = _LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self._update_line_number_area_width(0)
        self._highlighter = _SyntaxHighlighter(self.document(), suffix, file_name)

    def line_number_area_width(self) -> int:
        digits = max(1, len(str(max(1, self.blockCount()))))
        return 8 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_line_number_area_width(self, _block_count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self._line_number_area.scroll(0, dy)
        else:
            self._line_number_area.update(0, rect.y(), self._line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width(0)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt virtual method name
        super().resizeEvent(event)
        contents = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(contents.left(), contents.top(), self.line_number_area_width(), contents.height())
        )

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), self.palette().alternateBase())
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        painter.setPen(self.palette().color(self.palette().ColorRole.Mid))

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0,
                    top,
                    self._line_number_area.width() - 4,
                    self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(block_number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1


class EditorWorkspace(QWidget):
    """Project tree and multi-tab, encoding-aware source editor for QYS RTOS."""

    file_saved = Signal(str)
    status_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project_root: Path | None = None
        self._documents: dict[Path, _OpenDocument] = {}

        self._file_model = QFileSystemModel(self)
        self._file_model.setFilter(
            QDir.Filter.AllEntries
            | QDir.Filter.NoDotAndDotDot
            | QDir.Filter.Hidden
            | QDir.Filter.System
        )
        self._tree_filter = _ProjectTreeFilter(self)
        self._tree_filter.setSourceModel(self._file_model)

        self._build_ui()

    @property
    def project_root(self) -> Path | None:
        return self._project_root

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self._save_button = QPushButton("保存")
        self._save_button.setToolTip("保存当前文件 (Ctrl+S)")
        self._save_all_button = QPushButton("全部保存")
        self._save_all_button.setToolTip("保存所有已修改文件")
        toolbar.addWidget(self._save_button)
        toolbar.addWidget(self._save_all_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        self._tree_panel = QWidget(splitter)
        tree_layout = QVBoxLayout(self._tree_panel)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(6)

        tree_header = QHBoxLayout()
        tree_header.addWidget(QLabel("项目文件"))
        tree_header.addStretch(1)
        self._show_hidden_checkbox = QCheckBox("显示隐藏目录")
        self._show_hidden_checkbox.setToolTip("显示 .git、build 等默认收起的目录")
        tree_header.addWidget(self._show_hidden_checkbox)
        tree_layout.addLayout(tree_header)

        self._tree = QTreeView(self._tree_panel)
        self._tree.setModel(self._tree_filter)
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setAnimated(False)
        self._tree.setMinimumWidth(210)
        for column in range(1, 4):
            self._tree.hideColumn(column)
        tree_layout.addWidget(self._tree, 1)
        splitter.addWidget(self._tree_panel)

        self._editor_panel = QWidget(splitter)
        editor_layout = QVBoxLayout(self._editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(6)
        editor_layout.addWidget(QLabel("代码编辑器"))

        self._stack = QStackedWidget(self._editor_panel)
        self._tabs = QTabWidget(self._stack)
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.setDocumentMode(True)
        self._empty_state = QLabel(
            "尚未打开文件\n请从左侧项目文件树双击文件开始编辑\n\n支持 UTF-8、UTF-8 BOM 和 GB18030 文本",
            self._stack,
        )
        self._empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_state.setWordWrap(True)
        self._empty_state.setMinimumHeight(240)
        self._stack.addWidget(self._empty_state)
        self._stack.addWidget(self._tabs)
        self._stack.setCurrentWidget(self._empty_state)
        editor_layout.addWidget(self._stack, 1)
        splitter.addWidget(self._editor_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([290, 790])
        layout.addWidget(splitter, 1)

        self._save_button.clicked.connect(self.save_current)
        self._save_all_button.clicked.connect(self.save_all)
        self._save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self._save_shortcut.activated.connect(self.save_current)
        self._show_hidden_checkbox.toggled.connect(self._set_show_hidden)
        self._tree.doubleClicked.connect(self._open_tree_index)
        self._tabs.currentChanged.connect(self._current_tab_changed)
        self._tabs.tabCloseRequested.connect(self._close_tab_at)

    def set_project(self, root: str | Path) -> bool:
        """Switch the tree root, retaining the current project if the user cancels."""
        try:
            project_root = validate_project(root)
        except (OSError, RuntimeError, TypeError, ValueError, ProjectError) as exc:
            self._show_error("无法打开工程文件夹", str(root), exc)
            return False

        if project_root != self._project_root:
            if not self._confirm_close_all():
                return False
            self._clear_open_tabs()
            self._project_root = project_root

        self._tree_filter.set_root_path(project_root)
        source_root = self._file_model.setRootPath(str(project_root))
        self._tree.setRootIndex(self._tree_filter.mapFromSource(source_root))
        self.status_changed.emit(f"已打开工程：{project_root}")

        main_file = project_root / "src" / "main.c"
        if main_file.is_file():
            return self.open_file(main_file)
        return True

    def open_file(self, path: str | Path) -> bool:
        try:
            file_path = Path(path).expanduser().resolve(strict=True)
            if not file_path.is_file():
                raise IsADirectoryError(f"不是文件：{file_path}")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._show_error("无法打开文件", str(path), exc)
            return False

        existing = self._documents.get(file_path)
        if existing is not None:
            self._tabs.setCurrentWidget(existing.editor)
            return True

        try:
            snapshot = read_text_file(file_path)
        except (OSError, UnicodeError, ValueError, ProjectError) as exc:
            self._show_error("无法读取文件", str(file_path), exc)
            return False

        editor = _CodeEditor(file_path.suffix, file_path.name, self._tabs)
        editor.setPlainText(snapshot.text)
        editor.document().setModified(False)
        document = _OpenDocument(file_path, snapshot, editor)
        self._documents[file_path] = document
        index = self._tabs.addTab(editor, file_path.name)
        self._tabs.setTabToolTip(index, str(file_path))
        editor.document().modificationChanged.connect(
            lambda _modified, document=document: self._update_tab_title(document)
        )
        self._tabs.setCurrentIndex(index)
        self.status_changed.emit(f"已打开文件：{file_path}")
        return True

    def append_overlay_snippet(self, path: str | Path, snippet: str) -> bool:
        """Add a reviewed snippet to an existing project overlay, without saving it."""
        try:
            file_path = Path(path).expanduser().resolve(strict=True)
            if self._project_root is None or not file_path.is_file():
                return False
            file_path.relative_to(self._project_root)
            if file_path.suffix.casefold() != ".overlay" or not snippet.strip():
                return False
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        if not self.open_file(file_path):
            return False
        document = self._documents[file_path]
        existing = document.editor.toPlainText()
        separator = "\n" if existing and not existing.endswith("\n") else ""
        cursor = document.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(separator + "\n" + snippet.rstrip() + "\n")
        document.editor.setTextCursor(cursor)
        self.status_changed.emit("设备树配置已加入编辑器；请检查、保存并重新编译。")
        return True

    def save_current(self) -> bool:
        document = self._current_document()
        return (
            True
            if document is None or not document.editor.document().isModified()
            else self._save_document(document)
        )

    def save_all(self) -> bool:
        for document in tuple(self._documents.values()):
            if document.editor.document().isModified() and not self._save_document(document):
                return False
        return True

    def has_unsaved_changes(self) -> bool:
        """Whether any open editor buffer contains changes not saved to disk."""
        return any(document.editor.document().isModified() for document in self._documents.values())

    def reload_external_file(self, path: str | Path) -> bool:
        """Refresh a clean open buffer after a project tool updates the file."""
        try:
            file_path = Path(path).resolve(strict=False)
            document = self._documents.get(file_path)
            if document is None:
                return True
            if document.editor.document().isModified():
                return False
            if not file_path.exists():
                index = self._tabs.indexOf(document.editor)
                if index >= 0:
                    self._close_tab_at(index)
                return True
            snapshot = read_text_file(file_path)
            position = document.editor.textCursor().position()
            document.editor.setPlainText(snapshot.text)
            document.snapshot = snapshot
            document.editor.document().setModified(False)
            cursor = document.editor.textCursor()
            cursor.setPosition(min(position, len(snapshot.text)))
            document.editor.setTextCursor(cursor)
            self._update_tab_title(document)
            return True
        except (OSError, RuntimeError, ValueError, ProjectError):
            return False

    def close_all(self) -> bool:
        if not self._confirm_close_all():
            return False
        self._clear_open_tabs()
        self.status_changed.emit("已关闭全部文件")
        return True

    def _current_document(self) -> _OpenDocument | None:
        current = self._tabs.currentWidget()
        if not isinstance(current, _CodeEditor):
            return None
        for document in self._documents.values():
            if document.editor is current:
                return document
        return None

    def _save_document(self, document: _OpenDocument) -> bool:
        text = document.editor.toPlainText()
        try:
            updated_snapshot = save_text_file(document.snapshot, text)
        except FileChangedError as exc:
            if not self._confirm_external_overwrite(document.path, exc):
                self.status_changed.emit(f"外部文件已有修改，已取消保存：{document.path}")
                return False
            try:
                updated_snapshot = save_text_file(document.snapshot, text, overwrite_external=True)
            except (OSError, UnicodeError, ValueError, ProjectError) as overwrite_error:
                self._show_error("覆盖外部修改失败", str(document.path), overwrite_error)
                return False
        except (OSError, UnicodeError, ValueError, ProjectError) as exc:
            self._show_error("保存文件失败", str(document.path), exc)
            return False

        document.snapshot = updated_snapshot
        document.editor.document().setModified(False)
        self._update_tab_title(document)
        self.file_saved.emit(str(document.path))
        self.status_changed.emit(f"已保存：{document.path}")
        return True

    def _confirm_close_all(self) -> bool:
        for document in tuple(self._documents.values()):
            if not document.editor.document().isModified():
                continue
            choice = self._ask_unsaved(document.path)
            if choice == "cancel":
                return False
            if choice == "save" and not self._save_document(document):
                return False
        return True

    def _ask_unsaved(self, path: Path) -> Literal["save", "discard", "cancel"]:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("未保存的更改")
        box.setText(f"文件“{path.name}”有未保存的更改。")
        box.setInformativeText("关闭前是否保存？")
        save_button = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton("放弃修改", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return "save"
        if clicked is discard_button:
            return "discard"
        return "cancel"

    def _confirm_external_overwrite(self, path: Path, error: Exception) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("文件已在外部修改")
        box.setText(f"文件“{path.name}”在编辑器打开后发生了变化。")
        box.setInformativeText(f"{error}\n\n覆盖会替换磁盘上的当前内容。")
        overwrite_button = box.addButton("覆盖文件", QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = box.addButton("取消保存", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_button)
        box.exec()
        return box.clickedButton() is overwrite_button

    def _show_error(self, action: str, path: str, error: Exception | str) -> None:
        details = str(error).strip() or error.__class__.__name__
        self.status_changed.emit(f"{action}：{path}；{details}")
        QMessageBox.warning(self, action, f"{action}：\n{path}\n\n原因：{details}")

    def _clear_open_tabs(self) -> None:
        while self._tabs.count():
            widget = self._tabs.widget(0)
            self._tabs.removeTab(0)
            widget.deleteLater()
        self._documents.clear()
        self._stack.setCurrentWidget(self._empty_state)

    def _close_tab_at(self, index: int) -> None:
        editor = self._tabs.widget(index)
        document = next((item for item in self._documents.values() if item.editor is editor), None)
        if document is None:
            return
        if editor.document().isModified():
            choice = self._ask_unsaved(document.path)
            if choice == "cancel":
                return
            if choice == "save" and not self._save_document(document):
                return
        self._documents.pop(document.path, None)
        self._tabs.removeTab(index)
        editor.deleteLater()
        if self._tabs.count() == 0:
            self._stack.setCurrentWidget(self._empty_state)
        self.status_changed.emit(f"已关闭文件：{document.path}")

    def _update_tab_title(self, document: _OpenDocument) -> None:
        index = self._tabs.indexOf(document.editor)
        if index < 0:
            return
        modified = document.editor.document().isModified()
        self._tabs.setTabText(index, f"● {document.path.name}" if modified else document.path.name)
        self._tabs.setTabToolTip(index, f"{document.path}\n{'有未保存的更改' if modified else '已保存'}")

    def _current_tab_changed(self, index: int) -> None:
        self._stack.setCurrentWidget(self._tabs if index >= 0 else self._empty_state)

    def _open_tree_index(self, index: QModelIndex) -> None:
        source_index = self._tree_filter.mapToSource(index)
        if not source_index.isValid() or not self._file_model.fileInfo(source_index).isFile():
            return
        self.open_file(self._file_model.filePath(source_index))

    def _set_show_hidden(self, visible: bool) -> None:
        self._tree_filter.set_show_hidden(visible)

