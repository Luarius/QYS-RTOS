import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from qys_rtos.config import AppSettings
from qys_rtos.window import MainWindow, default_build_dir, split_runner_arguments


class FakeRunner(QObject):
    output = Signal(str)
    finished = Signal(int, bool)
    running_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.commands = []
        self.is_running = False

    def start(self, argv, **_kwargs):
        self.commands.append(list(argv))
        self.is_running = True
        self.running_changed.emit(True)
        return True

    def finish(self, code=0, cancelled=False):
        self.is_running = False
        self.running_changed.emit(False)
        self.finished.emit(code, cancelled)

    def stop(self):
        if self.is_running:
            self.finish(-1, True)


class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qys-window-")
        self.previous_home = os.environ.get("QYS_CONFIG_HOME")
        os.environ["QYS_CONFIG_HOME"] = self.temp.name

    def tearDown(self):
        if self.previous_home is None:
            os.environ.pop("QYS_CONFIG_HOME", None)
        else:
            os.environ["QYS_CONFIG_HOME"] = self.previous_home
        self.temp.cleanup()

    def test_board_build_directories_do_not_collide(self):
        base = Path(self.temp.name) / "项目 with spaces"
        first = default_build_dir(base, "esp32c6_devkitc/esp32c6/hpcore")
        second = default_build_dir(base, "esp32c6_devkitc/esp32c6/lpcore")
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, base / "build_qys")

    def test_zephyr_queued_flash_is_cancelled_by_stop(self):
        with patch.object(MainWindow, "_restore_environment", return_value=None):
            window = MainWindow()
        try:
            window._flash_after_build = True
            window._operation_state_changed(False)
            window.stop_button.click()
            self.assertFalse(window._flash_after_build)
            window.environment = object()
            with patch.object(window, "refresh_runners") as prepare:
                window._start_flash()
            prepare.assert_not_called()
        finally:
            window.environment = None
            window.close()

    def test_agent_dock_disables_project_controls_during_task(self):
        with patch("qys_rtos.window.QTimer.singleShot"):
            window = MainWindow()
        try:
            self.assertIs(window.agent_dock.widget(), window.agent_panel)
            window.agent_panel.running_changed.emit(True)
            self.assertFalse(window.build_button.isEnabled())
            self.assertFalse(window.flash_button.isEnabled())
            self.assertFalse(window.open_button.isEnabled())
            self.assertFalse(window.editor.isEnabled())
            window.agent_panel.running_changed.emit(False)
            self.assertTrue(window.build_button.isEnabled())
            self.assertTrue(window.open_button.isEnabled())
            self.assertTrue(window.editor.isEnabled())
        finally:
            window.close()

    def test_agent_preflight_refuses_unsaved_editor_content(self):
        project = Path(self.temp.name) / "app"
        (project / "src").mkdir(parents=True)
        (project / "CMakeLists.txt").write_text("project(test)\n", encoding="utf-8")
        (project / "src" / "main.c").write_text("original\n", encoding="utf-8")
        with patch("qys_rtos.window.QTimer.singleShot"):
            window = MainWindow()
        try:
            self.assertTrue(window.editor.set_project(project))
            document = window.editor._current_document()
            document.editor.appendPlainText("unsaved")
            with patch("qys_rtos.window.QMessageBox.information") as notice:
                self.assertFalse(window._agent_prepare())
            self.assertTrue(notice.called)
            self.assertIn("unsaved", document.editor.toPlainText())
        finally:
            if window.editor._current_document() is not None:
                window.editor._current_document().editor.document().setModified(False)
            window.close()

    def test_agent_log_context_excludes_other_projects_and_flash_output(self):
        project = Path(self.temp.name) / "app"
        (project / "src").mkdir(parents=True)
        (project / "CMakeLists.txt").write_text("project(test)\n", encoding="utf-8")
        (project / "src" / "main.c").write_text("original\n", encoding="utf-8")
        with patch("qys_rtos.window.QTimer.singleShot"):
            window = MainWindow()
        try:
            self.assertTrue(window.editor.set_project(project))
            window.build_output.setPlainText("── 编译 ──\n> west build D:\\other\\project\nold error\n")
            self.assertEqual(window._agent_build_output(), "")
            window.build_output.appendPlainText(f"── 编译 ──\n> west build {project}\ncurrent error\n── 烧录 ──\nflash data")
            context = window._agent_build_output()
            self.assertIn("current error", context)
            self.assertNotIn("old error", context)
            self.assertNotIn("flash data", context)
        finally:
            window.close()

    def test_build_directories_also_distinguish_workspace_and_sdk(self):
        base = Path(self.temp.name) / "app"
        board = "esp32c6_devkitc/esp32c6/hpcore"
        first = default_build_dir(base, board, Path(self.temp.name) / "west1", Path(self.temp.name) / "sdk1")
        second = default_build_dir(base, board, Path(self.temp.name) / "west2", Path(self.temp.name) / "sdk1")
        third = default_build_dir(base, board, Path(self.temp.name) / "west1", Path(self.temp.name) / "sdk2")
        self.assertEqual(len({first, second, third}), 3)

    def test_default_build_directory_retries_after_interrupted_configure(self):
        project = Path(self.temp.name) / "app"
        board = "esp32c6_devkitc/esp32c6/procpu"
        workspace = Path(self.temp.name) / "Zephyr workspace"
        sdk = Path(self.temp.name) / "Zephyr SDK"
        interrupted = default_build_dir(project, board, workspace, sdk)
        interrupted.mkdir(parents=True)
        marker = interrupted / "partial-cmake-output.txt"
        marker.write_text("keep interrupted output", encoding="utf-8")

        retry = default_build_dir(project, board, workspace, sdk)

        self.assertNotEqual(retry, interrupted)
        self.assertFalse(retry.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep interrupted output")

    def test_reopening_skips_saved_incomplete_default_retry_without_losing_auto_mode(self):
        project = Path(self.temp.name) / "app"
        project.mkdir()
        (project / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n", encoding="utf-8")
        board = "esp32c6_devkitc/esp32c6/procpu"
        workspace = Path(self.temp.name) / "Zephyr workspace"
        (workspace / "zephyr").mkdir(parents=True)
        west = workspace / "fake west.exe"
        west.write_text("fake", encoding="utf-8")

        first_default = default_build_dir(project, board, workspace)
        first_default.mkdir(parents=True)
        first_marker = first_default / "partial-cmake-output.txt"
        first_marker.write_text("keep first attempt", encoding="utf-8")
        saved_retry = default_build_dir(project, board, workspace)
        saved_retry.mkdir(parents=True, exist_ok=True)
        retry_marker = saved_retry / "partial-cmake-output.txt"
        retry_marker.write_text("keep retry attempt", encoding="utf-8")
        settings = AppSettings(
            workspace=str(workspace),
            west=str(west),
            project=str(project),
            board=board,
            build_dir=str(saved_retry),
        )

        with patch("qys_rtos.window.load_settings", return_value=settings), patch("qys_rtos.window.QTimer.singleShot"):
            window = MainWindow()
        try:
            self.assertTrue(window._auto_build_dir)
            self.assertNotEqual(Path(window.build_dir.text()), saved_retry)
            self.assertEqual(first_marker.read_text(encoding="utf-8"), "keep first attempt")
            self.assertEqual(retry_marker.read_text(encoding="utf-8"), "keep retry attempt")
        finally:
            window.close()

    def test_refresh_keeps_explicit_automatic_port(self):
        with patch("qys_rtos.window.QTimer.singleShot"):
            window = MainWindow()
        window.settings.port = "COM26"
        window.port_combo.setCurrentIndex(0)  # Explicit auto selection.
        fake_ports = [SimpleNamespace(device="COM26", description="USB serial")]
        with patch("qys_rtos.window.list_ports.comports", return_value=fake_ports):
            window.refresh_ports()
        self.assertEqual(window.port_combo.currentData(), "")
        window.close()

    @unittest.skipUnless(Path(r"D:\zephyrproject\.venv\Scripts\west.exe").is_file(), "local Zephyr unavailable")
    def test_arbitrary_nonempty_build_folder_is_not_modified(self):
        with patch("qys_rtos.window.QTimer.singleShot"):
            window = MainWindow()
        target = Path(self.temp.name) / "important"
        target.mkdir()
        marker = target / "user-data.txt"
        marker.write_text("keep me", encoding="utf-8")
        window.board_combo.setEditText("esp32c6_devkitc/esp32c6/hpcore")
        window._auto_build_dir = False
        window.build_dir.setText(str(target))
        with patch("qys_rtos.window.QMessageBox.warning") as warning:
            window.build_project()
        self.assertTrue(warning.called)
        self.assertFalse(window.operation.is_running)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep me")
        window.close()

    @unittest.skipUnless(Path(r"D:\zephyrproject\.venv\Scripts\west.exe").is_file(), "local Zephyr unavailable")
    def test_flash_waits_for_successful_build_and_locks_controls(self):
        with patch("qys_rtos.window.QTimer.singleShot"):
            window = MainWindow()
        fake = FakeRunner()
        window.operation = fake
        fake.output.connect(window._append_operation_output)
        fake.running_changed.connect(window._operation_state_changed)
        fake.finished.connect(window._operation_finished)
        board = "esp32c6_devkitc/esp32c6/hpcore"
        target = Path(self.temp.name) / "new-build"
        window.board_combo.setEditText(board)
        window._auto_build_dir = False
        window.build_dir.setText(str(target))
        window.port_combo.addItem("COM26", "COM26")
        window.port_combo.setCurrentIndex(window.port_combo.count() - 1)
        with patch("qys_rtos.window.QMessageBox.warning") as warning:
            window.flash_project()
        self.assertEqual(len(fake.commands), 1)
        self.assertIn("build", fake.commands[0])
        self.assertFalse(window.editor.isEnabled())
        self.assertFalse(window.board_combo.isEnabled())
        target.mkdir()
        (target / "CMakeCache.txt").write_text(
            f"CACHED_BOARD:STRING={board}\n"
            f"APPLICATION_SOURCE_DIR:PATH={window.editor.project_root}\n"
            f"ZEPHYR_BASE:PATH={window.environment.workspace / 'zephyr'}\n"
            + (f"ZEPHYR_SDK_INSTALL_DIR:PATH={window.environment.sdk}\n" if window.environment.sdk else ""),
            encoding="utf-8",
        )
        (target / "zephyr").mkdir()
        (target / "zephyr" / "runners.yaml").write_text(
            "runners: [esp32]\nflash-runner: esp32\n", encoding="utf-8"
        )
        with patch("qys_rtos.window.QMessageBox.warning") as warning_after_build:
            fake.finish(0)
            self.assertFalse(window.editor.isEnabled(), "controls must remain locked between build and flash")
            self.app.processEvents()
        self.assertFalse(warning.called, f"flash preparation warning: {warning.call_args}")
        self.assertFalse(warning_after_build.called, f"flash command warning: {warning_after_build.call_args}")
        self.assertEqual(len(fake.commands), 2)
        self.assertIn("flash", fake.commands[1])
        self.assertEqual(fake.commands[1][-2:], ["--esp-device", "COM26"])
        fake.finish(0)
        self.assertTrue(window.editor.isEnabled())
        window.close()

    def test_windows_runner_options_keep_path_with_spaces(self):
        options = split_runner_arguments('--probe "C:\\Program Files\\tool\\probe.cfg"')
        self.assertEqual(options, ["--probe", "C:\\Program Files\\tool\\probe.cfg"])

    def test_first_run_without_zephyr_keeps_environment_guide_available(self):
        with patch("qys_rtos.window.discover_environment", return_value=None), patch("qys_rtos.window.QTimer.singleShot"):
            window = MainWindow()
        self.assertIsNone(window.environment)
        self.assertIn("需要配置环境", window.environment_badge.text())
        self.assertTrue(window.setup_button.isEnabled())
        self.assertTrue(window.env_button.isEnabled())
        window.close()

    @unittest.skipUnless(Path(r"D:\zephyrproject\.venv\Scripts\west.exe").is_file(), "local Zephyr unavailable")
    def test_real_workspace_populates_board_picker(self):
        window = MainWindow()
        loop = QEventLoop()
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        window.board_process.finished.connect(lambda _code, _cancelled: loop.quit())
        timeout.start(20000)
        loop.exec()
        timeout.stop()
        self.assertGreater(window.board_combo.count(), 100)
        self.assertNotEqual(window.board_combo.findText("esp32c6_devkitc/esp32c6/hpcore"), -1)
        window.close()


if __name__ == "__main__":
    unittest.main()
