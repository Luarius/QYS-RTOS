"""A reviewable view of the generated device tree and source overlays."""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .devicetree import DeviceTreeError, make_status_overlay


class DeviceTreePanel(QWidget):
    refresh_requested = Signal()
    append_requested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nodes = {}
        self._selected_path = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        refresh = QPushButton("刷新编译结果")
        refresh.clicked.connect(self.refresh_requested)
        top = QHBoxLayout()
        top.addWidget(self.summary, 1)
        top.addWidget(refresh)
        layout.addLayout(top)

        panes = QSplitter(Qt.Orientation.Horizontal)
        panes.setChildrenCollapsible(False)
        layout.addWidget(panes, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索节点名称、标签或路径")
        self.search.textChanged.connect(self._filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["设备树节点", "状态"])
        self.tree.setColumnWidth(0, 220)
        self.tree.currentItemChanged.connect(self._select_node)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.tree, 1)
        panes.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 0, 0, 0)
        self.node_path = QLabel("选择节点以查看属性")
        self.node_path.setWordWrap(True)
        self.node_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right_layout.addWidget(self.node_path)
        self.properties = QTreeWidget()
        self.properties.setMinimumHeight(38)
        self.properties.setHeaderLabels(["属性", "值"])
        self.properties.setColumnWidth(0, 145)
        self.properties.setRootIsDecorated(False)
        right_layout.addWidget(self.properties, 1)

        actions = QHBoxLayout()
        actions.addWidget(QLabel("状态"))
        self.status = QComboBox()
        self.status.addItem("启用 (okay)", "okay")
        self.status.addItem("禁用 (disabled)", "disabled")
        self.status.currentIndexChanged.connect(self._invalidate_preview)
        self.preview_button = QPushButton("生成配置预览")
        self.preview_button.clicked.connect(self._generate_preview)
        actions.addWidget(self.status)
        actions.addWidget(self.preview_button)
        actions.addStretch(1)
        right_layout.addLayout(actions)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(40)
        self.preview.setPlaceholderText("配置会先显示在这里，检查后再加入工程。")
        self.preview.setMaximumHeight(115)
        right_layout.addWidget(self.preview)
        self.overlay_hint = QLabel()
        self.overlay_hint.setWordWrap(True)
        right_layout.addWidget(self.overlay_hint)
        targets = QHBoxLayout()
        self.overlay_target = QComboBox()
        self.overlay_target.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.overlay_target.setMinimumContentsLength(14)
        self.overlay_target.setToolTip("仅列出上次构建缓存中记录的当前工程 overlay。")
        self.copy_button = QPushButton("复制配置")
        self.copy_button.clicked.connect(self._copy_preview)
        self.append_button = QPushButton("加入工程文件")
        self.append_button.clicked.connect(self._append_preview)
        targets.addWidget(self.overlay_target, 1)
        targets.addWidget(self.copy_button)
        targets.addWidget(self.append_button)
        right_layout.addLayout(targets)
        panes.addWidget(right)
        panes.setStretchFactor(0, 1)
        panes.setStretchFactor(1, 1)
        panes.setSizes([440, 650])
        self.clear_snapshot("请先打开工程、选择开发板并编译，再刷新设备树。")

    def clear_snapshot(self, message: str) -> None:
        self._nodes = {}
        self._selected_path = ""
        self.tree.clear()
        self.properties.clear()
        self.node_path.setText("选择节点以查看属性")
        self.overlay_target.clear()
        self.overlay_hint.setText("没有识别到已参与构建的工程 overlay；可复制配置并手动加入。")
        self.summary.setText(message)
        self._invalidate_preview()

    def set_snapshot(self, tree, overlay_files: list[Path]) -> None:
        self.clear_snapshot("显示当前工程上次编译的设备树；修改配置后请保存并重新编译。")

        def add_node(node, parent=None):
            label = ", ".join(node.labels)
            title = node.name + (f"  [{label}]" if label else "")
            status = node.properties.get("status")
            item = QTreeWidgetItem([title, str(status) if status is not None else "—"])
            item.setData(0, Qt.ItemDataRole.UserRole, node.path)
            item.setToolTip(0, node.path)
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            self._nodes[node.path] = node
            for child in node.children:
                add_node(child, item)
        add_node(tree.root)
        self.tree.expandToDepth(1)
        for path in overlay_files:
            self.overlay_target.addItem(f"{path.name}  ·  {path.parent.name}", str(path))
            self.overlay_target.setItemData(self.overlay_target.count() - 1, str(path), Qt.ItemDataRole.ToolTipRole)
        if overlay_files:
            self.overlay_target.setCurrentIndex(len(overlay_files) - 1)
            self.overlay_hint.setText("先加入编辑器供检查；编译或烧录会保存所有修改。其他 overlay 可能覆盖此设置。")
        self._filter(self.search.text())
        self._update_actions()

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()

        def visit(item):
            child_match = False
            for index in range(item.childCount()):
                child_match = visit(item.child(index)) or child_match
            own_match = not needle or needle in item.text(0).casefold() or needle in str(item.data(0, Qt.ItemDataRole.UserRole)).casefold()
            match = own_match or child_match
            item.setHidden(not match)
            if needle and child_match:
                item.setExpanded(True)
            return match
        for index in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(index))

    def _select_node(self, current, _previous=None) -> None:
        self._selected_path = str(current.data(0, Qt.ItemDataRole.UserRole)) if current else ""
        node = self._nodes.get(self._selected_path)
        self.properties.clear()
        if node:
            self.node_path.setText(f"节点路径：{node.path}")
            for name, value in node.properties.items():
                item = QTreeWidgetItem([name, "（无值）" if value is None else str(value)])
                item.setToolTip(1, "" if value is None else str(value))
                self.properties.addTopLevelItem(item)
        else:
            self.node_path.setText("选择节点以查看属性")
        self._invalidate_preview()

    def _invalidate_preview(self, *_args) -> None:
        self.preview.clear()
        self._update_actions()

    def _update_actions(self) -> None:
        valid_node = bool(self._selected_path and self._selected_path != "/")
        self.preview_button.setEnabled(valid_node)
        self.status.setEnabled(valid_node)
        has_preview = bool(self.preview.toPlainText())
        self.copy_button.setEnabled(has_preview)
        self.append_button.setEnabled(has_preview and self.overlay_target.count() > 0)

    def _generate_preview(self) -> None:
        try:
            self.preview.setPlainText(make_status_overlay(self._selected_path, self.status.currentData()))
        except (DeviceTreeError, ValueError) as exc:
            self.preview.clear()
            self.summary.setText(f"无法生成此节点的配置：{exc}")
        self._update_actions()

    def _copy_preview(self) -> None:
        if self.preview.toPlainText():
            QApplication.clipboard().setText(self.preview.toPlainText())

    def _append_preview(self) -> None:
        path = self.overlay_target.currentData()
        snippet = self.preview.toPlainText()
        if path and snippet:
            self.append_requested.emit(path, snippet)
