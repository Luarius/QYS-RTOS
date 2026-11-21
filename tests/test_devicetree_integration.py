import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from qys_rtos.config import AppSettings
from qys_rtos.editor import EditorWorkspace
from qys_rtos.style import STYLE
from qys_rtos.window import MainWindow
from qys_rtos.zephyr import ZephyrEnvironment


class DeviceTreeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        (self.workspace / "zephyr").mkdir(parents=True)
        west = root / "west.exe"
        west.write_text("fake", encoding="utf-8")
        self.environment = ZephyrEnvironment(self.workspace, west)
        self.project = root / "project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "CMakeLists.txt").write_text("project(test)", encoding="utf-8")
        (self.project / "src" / "main.c").write_text("int main(void) {}", encoding="utf-8")
        self.overlay = self.project / "app.overlay"
        self.overlay.write_text("/* original */\n", encoding="utf-8")
        self.build = root / "build"
        (self.build / "zephyr").mkdir(parents=True)
        (self.build / "CMakeCache.txt").write_text(
            f"CACHED_BOARD:STRING=test_board\nAPPLICATION_SOURCE_DIR:PATH={self.project.as_posix()}\n"
            f"ZEPHYR_BASE:PATH={(self.workspace / 'zephyr').as_posix()}\n"
            f"DTC_OVERLAY_FILE:STRING={self.overlay.as_posix()}\n", encoding="utf-8"
        )
        (self.build / "zephyr" / "zephyr.dts").write_text(
            '/dts-v1/; / { soc { uart0: serial@4000 { status = "disabled"; }; }; };', encoding="utf-8"
        )
        settings = AppSettings(project=str(self.project), board="test_board", build_dir=str(self.build))
        self.patches = [
            patch("qys_rtos.window.load_settings", return_value=settings),
            patch.object(MainWindow, "_restore_environment", return_value=self.environment),
            patch("qys_rtos.window.QTimer.singleShot"),
            patch("qys_rtos.window.save_settings"),
        ]
        for item in self.patches:
            item.start()
        self.window = MainWindow()

    def tearDown(self):
        with patch.object(EditorWorkspace, "_ask_unsaved", return_value="discard"):
            self.window.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_snapshot_is_tied_to_selected_board_and_cleared_when_selection_changes(self):
        self.window._refresh_devicetree()
        panel = self.window.device_tree
        self.assertEqual(panel.tree.topLevelItemCount(), 1)
        self.assertEqual(panel.overlay_target.currentData(), str(self.overlay.resolve()))
        self.window.board_combo.setEditText("another_board")
        self.assertEqual(panel.tree.topLevelItemCount(), 0)
        self.window._refresh_devicetree()
        self.assertEqual(panel.tree.topLevelItemCount(), 0)
        self.assertIn("构建", panel.summary.text())

    def test_adding_preview_changes_only_editor_buffer(self):
        self.window._refresh_devicetree()
        panel = self.window.device_tree
        panel.tree.setCurrentItem(panel.tree.topLevelItem(0).child(0).child(0))
        panel.preview_button.click()
        panel.append_button.click()
        self.assertIs(self.window.workspace_tabs.currentWidget(), self.window.editor)
        document = self.window.editor._current_document()
        self.assertIn('status = "okay";', document.editor.toPlainText())
        self.assertTrue(document.editor.document().isModified())
        self.assertEqual(self.overlay.read_text(encoding="utf-8"), "/* original */\n")

    def test_serial_diagnostics_use_only_a_matching_build(self):
        with patch.object(self.window.serial, "set_diagnostic_context") as update:
            self.window._update_serial_diagnostic()
            self.assertEqual(update.call_args.args[0], self.build.resolve())
            self.window.board_combo.setEditText("another_board")
            self.assertIsNone(update.call_args.args[0])

    def test_failed_build_clears_snapshot_until_a_successful_rebuild(self):
        self.window._refresh_devicetree()
        self.assertEqual(self.window.device_tree.tree.topLevelItemCount(), 1)
        with patch.object(self.window.operation, "start", return_value=True):
            self.window._start_operation("编译", ["fake-build"])
        self.assertEqual(self.window.device_tree.tree.topLevelItemCount(), 0)
        self.window._operation_finished(1, False)
        self.window._refresh_devicetree()
        self.assertEqual(self.window.device_tree.tree.topLevelItemCount(), 0)
        self.assertIn("未成功", self.window.device_tree.summary.text())
        with patch.object(self.window.operation, "start", return_value=True):
            self.window._start_operation("编译", ["fake-build"])
        self.window._operation_finished(0, False)
        self.window._refresh_devicetree()
        self.assertEqual(self.window.device_tree.tree.topLevelItemCount(), 1)

    def test_device_tree_remains_usable_at_minimum_window_size(self):
        old_style = self.app.styleSheet()
        self.app.setStyleSheet(STYLE)
        try:
            self.window.resize(900, 600)
            self.window.workspace_tabs.setCurrentWidget(self.window.device_tree)
            self.window.show()
            self.app.processEvents()
            self.assertGreaterEqual(self.window.device_tree.tree.height(), 90)
            self.assertGreaterEqual(self.window.device_tree.preview.height(), 35)
        finally:
            self.app.setStyleSheet(old_style)


if __name__ == "__main__":
    unittest.main()
