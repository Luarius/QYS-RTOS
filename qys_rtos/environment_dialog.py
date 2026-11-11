"""Choose a Zephyr workspace and the tools used by QYS RTOS."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from .zephyr import ZephyrEnvironment


class EnvironmentDialog(QDialog):
    def __init__(self, environment: ZephyrEnvironment | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Zephyr 环境设置")
        self.setMinimumWidth(650)
        self.environment: ZephyrEnvironment | None = environment
        self.workspace = QLineEdit(str(environment.workspace) if environment else "")
        self.west = QLineEdit(str(environment.west) if environment else "")
        self.sdk = QLineEdit(str(environment.sdk) if environment and environment.sdk else "")
        self.workspace.setPlaceholderText("选择包含 .west 和 zephyr 文件夹的工作区")
        self.west.setPlaceholderText("通常在工作区 .venv\\Scripts\\west.exe")
        self.sdk.setPlaceholderText("可留空，让 Zephyr 自动查找已安装的 SDK")

        layout = QVBoxLayout(self)
        intro = QLabel("选择已有 Zephyr 环境。每台电脑的路径可以不同。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        form.setVerticalSpacing(14)
        form.addRow("工作区", self._browse_row(self.workspace, self._browse_workspace))
        form.addRow("west 程序", self._browse_row(self.west, self._browse_west))
        form.addRow("Zephyr SDK", self._browse_row(self.sdk, self._browse_sdk))
        layout.addLayout(form)
        hint = QLabel("例如工作区 D:\\zephyrproject；SDK 可使用你已经安装的版本。")
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存设置")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _browse_row(field: QLineEdit, callback) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field, 1)
        button = QPushButton("浏览…")
        button.clicked.connect(callback)
        layout.addWidget(button)
        return row

    def _browse_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 Zephyr 工作区", self.workspace.text())
        if not selected:
            return
        self.workspace.setText(selected)
        west = Path(selected) / ".venv" / "Scripts" / "west.exe"
        if west.is_file():
            self.west.setText(str(west))
        sdk_root = Path(selected) / ".qys-sdk"
        if sdk_root.is_dir():
            candidates = sorted((path for path in sdk_root.glob("zephyr-sdk-*") if path.is_dir()), reverse=True)
            if candidates:
                self.sdk.setText(str(candidates[0]))

    def _browse_west(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "选择 west.exe", self.west.text(), "程序 (*.exe);;所有文件 (*)")
        if selected:
            self.west.setText(selected)

    def _browse_sdk(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 Zephyr SDK 文件夹", self.sdk.text())
        if selected:
            self.sdk.setText(selected)

    def _accept(self) -> None:
        if not self.workspace.text().strip() or not self.west.text().strip():
            QMessageBox.warning(self, "环境不完整", "请选择 Zephyr 工作区和 west 程序。")
            return
        workspace = Path(self.workspace.text().strip()).expanduser()
        west = Path(self.west.text().strip()).expanduser()
        python_path = workspace / ".venv" / "Scripts" / "python.exe"
        sdk_text = self.sdk.text().strip()
        environment = ZephyrEnvironment(
            workspace=workspace, west=west,
            python=python_path if python_path.is_file() else None,
            sdk=Path(sdk_text).expanduser() if sdk_text else None,
        )
        errors = environment.validate()
        if not (workspace / ".west" / "config").is_file():
            errors.append("工作区缺少 .west/config，请选择 west 工作区根目录。")
        if errors:
            QMessageBox.warning(self, "环境不完整", "\n".join(errors))
            return
        self.environment = environment
        self.accept()
