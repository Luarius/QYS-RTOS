import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from qys_rtos.board_support import BOARD_EXTENSIONS, create_board_project
from qys_rtos.window import MainWindow
from tests.test_window import FakeRunner


class BoardWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_build_and_upload_stay_in_the_ordinary_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pio = root / "pio.exe"
            pio.touch()
            project = create_board_project(root / "Test Board", BOARD_EXTENSIONS[0])
            config = json.dumps([["env:qys", [
                ["platform", "espressif32"], ["board", "esp32-s3-devkitc-1"],
                ["upload_protocol", "esptool"],
            ]]])

            with (
                patch.dict(os.environ, {"QYS_CONFIG_HOME": temporary}),
                patch.object(MainWindow, "_restore_environment", return_value=None),
                patch("qys_rtos.board_workspace.find_core", return_value=pio),
                patch("qys_rtos.board_workspace.CommandRunner", side_effect=lambda _parent: FakeRunner()),
            ):
                window = MainWindow()
                try:
                    board = window.board_workspace
                    window.mode_tabs.setCurrentIndex(1)
                    self.assertTrue(board.set_project(project))
                    self.assertFalse(window.mode_tabs.isEnabled())
                    board.runner.output.emit(config + "\n")
                    board.runner.finish()

                    board.build_button.click()
                    board.runner.output.emit(config + "\n")
                    board.runner.finish()
                    self.assertEqual(board.runner.commands[-1][-2:], ["--target", "buildprog"])
                    self.assertFalse(window.operation.is_running)
                    board.runner.finish()
                    self.assertTrue(window.mode_tabs.isEnabled())

                    board.build_button.click()

                    def decline_close(*_arguments):
                        board.runner.output.emit(config + "\n")
                        board.runner.finish()
                        self.assertEqual(board.runner.commands[-1][-1], "--json-output")
                        return QMessageBox.StandardButton.No

                    with patch("qys_rtos.window.QMessageBox.question", side_effect=decline_close):
                        self.assertFalse(window.close())
                    self.assertEqual(board.runner.commands[-1][-2:], ["--target", "buildprog"])
                    board.runner.finish()

                    board.port_combo.addItem("COM7", "COM7")
                    board.port_combo.setCurrentIndex(board.port_combo.count() - 1)
                    with patch("qys_rtos.board_workspace.QMessageBox.question") as confirm:
                        board.flash_button.click()
                        board.runner.is_running = False
                        board.stop_button.click()
                        board.runner.output.emit(config + "\n")
                        board.runner.finish()
                    confirm.assert_not_called()
                    self.assertEqual(board.runner.commands[-1][-1], "--json-output")
                    self.assertTrue(board.flash_button.isEnabled())

                    with patch("qys_rtos.board_workspace.QMessageBox.question",
                               return_value=QMessageBox.StandardButton.Yes):
                        board.flash_button.click()
                        board.runner.output.emit(config + "\n")
                        board.runner.finish()
                    self.assertEqual(board.runner.commands[-1][-4:],
                                     ["--target", "upload", "--upload-port", "COM7"])
                    self.assertFalse(window.operation.is_running)
                    board.runner.finish()
                finally:
                    window.close()
