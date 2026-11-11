import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from qys_rtos import setup_wizard
from qys_rtos.bootstrap import SetupStep


class _FakeCommandRunner(QObject):
    output = Signal(str)
    finished = Signal(int, bool)
    running_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls = []
        self._running = False
        self.stop_calls = 0

    @property
    def is_running(self):
        return self._running

    def start(self, argv, *, cwd=None, env=None, interactive=False):
        if self._running:
            return False
        call = {
            "argv": tuple(str(part) for part in argv),
            "cwd": Path(cwd) if cwd is not None else None,
            "env": dict(env or {}),
        }
        self.calls.append(call)
        self._running = True
        self.running_changed.emit(True)
        return True

    def write_output(self, chunk):
        if self._running:
            self.output.emit(chunk)

    def finish(self, exit_code=0, cancelled=False):
        if not self._running:
            raise AssertionError("There is no fake command to finish")
        self._running = False
        self.running_changed.emit(False)
        self.finished.emit(exit_code, cancelled)

    def stop(self):
        self.stop_calls += 1
        if self._running:
            self.finish(-1, True)


class _MessageBoxStub:
    class Icon:
        Warning = 1

    class StandardButton:
        Ok = 1
        Yes = 2
        No = 4

    calls = []

    @classmethod
    def information(cls, *args):
        cls.calls.append(("information", args[1:3]))

    @classmethod
    def warning(cls, *args):
        cls.calls.append(("warning", args[1:3]))

    @classmethod
    def critical(cls, *args):
        cls.calls.append(("critical", args[1:3]))

    @classmethod
    def question(cls, *args):
        cls.calls.append(("question", args[1:3]))
        return cls.StandardButton.No


class SetupWizardWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="qyssetup_",
            dir=Path(__file__).resolve().parent,
        )
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "ZephyrWorkspace"
        self.steps = (
            SetupStep("create venv", ("python", "-m", "venv", ".venv"), self.root),
            SetupStep("install west", ("venv-python", "-m", "pip", "install", "west"), self.root),
            SetupStep("init Zephyr", ("west", "init", "--mr", "v4.5.0"), self.root),
            SetupStep("update modules", ("west", "update"), self.workspace),
            SetupStep("list requirements", ("west", "packages", "pip"), self.workspace),
            SetupStep("install SDK", ("west", "sdk", "install"), self.workspace / "zephyr"),
        )
        self.requirements_file = self.workspace / "zephyr" / "scripts" / "requirements with spaces.txt"

        self.messages = _MessageBoxStub
        self.messages.calls = []
        self.patches = [
            patch.object(setup_wizard.QTimer, "singleShot", lambda *_args: None),
            patch.object(setup_wizard, "CommandRunner", _FakeCommandRunner),
            patch.object(setup_wizard, "QMessageBox", self.messages),
            patch.object(setup_wizard, "refreshed_windows_environment", return_value={"PATH": ""}),
            patch.object(
                setup_wizard,
                "prerequisite_status",
                return_value={
                    "Python 3.12+": True,
                    "Git": True,
                    "CMake": True,
                    "Ninja": True,
                    "wget": True,
                    "gperf": True,
                    "dtc": True,
                    "7-Zip": True,
                    "winget": True,
                },
            ),
            patch.object(setup_wizard, "find_python", return_value=(r"C:\Python312\python.exe",)),
            patch.object(setup_wizard, "build_install_plan", return_value=self.steps),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

        self.dialog = setup_wizard.SetupWizard()
        self.dialog.workspace_edit.setText(str(self.workspace))
        self.completed_paths = []
        self.dialog.setup_complete.connect(self.completed_paths.append)
        self.runner = self.dialog._runner

    def tearDown(self):
        if self.runner.is_running:
            self.runner.stop()
        self.dialog.close()
        self.temporary.cleanup()

    def _wait_for(self, predicate, message):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail(message)

    def _start_and_wait_for_first_command(self):
        self.dialog.start_button.click()
        self._wait_for(lambda: len(self.runner.calls) == 1, "setup did not start the first fake command")
        self._wait_for(lambda: self.dialog._worker is None, "prerequisite worker did not finish")
        self.assertEqual(
            self.runner.calls[0]["argv"],
            ("git", "ls-remote", "--exit-code", "https://github.com/zephyrproject-rtos/zephyr", "HEAD"),
        )
        self.assertEqual(self.runner.calls[0]["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.runner.finish()
        self._wait_for(lambda: len(self.runner.calls) == 2, "setup did not start after the Git preflight")
        self.assertEqual(self.runner.calls[1]["argv"], self.steps[0].argv)
        self.assertEqual(self.runner.calls[1]["env"]["VIRTUAL_ENV"], str(self.workspace / ".venv"))

    def test_proxy_preflight_failure_is_actionable_and_does_not_echo_credentials(self):
        self.dialog.start_button.click()
        self._wait_for(lambda: len(self.runner.calls) == 1, "Git preflight did not start")
        self._wait_for(lambda: self.dialog._worker is None, "prerequisite worker did not finish")

        self.runner.write_output(
            "fatal: unable to access 'http://alice:top-secret@proxy.invalid:8443': "
            "Could not resolve proxy\n"
        )
        self.runner.finish(exit_code=128)

        output = self.dialog.output_edit.toPlainText()
        message_text = " ".join(str(call) for call in self.messages.calls)
        self.assertEqual(len(self.runner.calls), 1, "a failed preflight must not start setup writes")
        self.assertNotIn("top-secret", output + message_text)
        self.assertIn("http.proxy", output + message_text)
        self.assertIn("再次点击", output + message_text)
        self.assertIsNone(self.dialog._workflow)
        self.assertTrue(self.dialog.start_button.isEnabled())

    def test_missing_wget_uses_its_winget_package(self):
        status = {
            "Python 3.12+": True,
            "Git": True,
            "CMake": True,
            "Ninja": True,
            "wget": False,
            "gperf": True,
            "dtc": True,
            "7-Zip": True,
            "winget": True,
        }

        with patch.object(setup_wizard, "prerequisite_status", return_value=status):
            self.dialog.install_prerequisites_button.click()
            self._wait_for(lambda: len(self.runner.calls) == 1, "missing wget was not queued for winget")

        self.assertEqual(
            self.runner.calls[0]["argv"][:4],
            ("winget", "install", "--id", "JernejSimoncic.Wget"),
        )

    def test_windows_python_guidance_prefers_312_without_rejecting_newer_versions(self):
        labels = "\n".join(label.text() for label in self.dialog.findChildren(setup_wizard.QLabel))

        self.assertIn("优先推荐 Python 3.12", labels)
        self.assertIn("Python 3.13", labels)
        self.assertIn("不会仅因版本更高而拒绝", labels)

    def test_missing_recommended_tools_warn_but_do_not_block_workspace_completion(self):
        self.dialog._workspace = self.workspace
        self.dialog._prerequisites = {"gperf": False, "dtc": False, "7-Zip": False}

        self.dialog._complete_setup()

        message = " ".join(str(call) for call in self.messages.calls)
        self.assertIn("gperf", message)
        self.assertIn("dtc", message)
        self.assertIn("7-Zip", message)
        self.assertIn("部分开发板或构建", message)
        self.assertIn("SDK", message)
        self.assertEqual(self.completed_paths, [str(self.workspace.resolve())])

    def test_workspace_changed_during_preflight_is_rejected_before_setup_writes(self):
        self.dialog.start_button.click()
        self._wait_for(lambda: len(self.runner.calls) == 1, "Git preflight did not start")
        self._wait_for(lambda: self.dialog._worker is None, "prerequisite worker did not finish")

        self.workspace.mkdir()
        marker = self.workspace / "existing-project.txt"
        marker.write_text("keep this project", encoding="utf-8")
        self.runner.finish()

        self.assertEqual(len(self.runner.calls), 1, "setup must revalidate the target after network preflight")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep this project")
        self.assertIsNone(self.dialog._workflow)
        self.assertIn("工作区目录已有文件", self.dialog.output_edit.toPlainText())

    def test_successful_stages_run_in_order_and_install_stage_five_requirements_separately(self):
        self._start_and_wait_for_first_command()

        for index in range(4):
            self.assertEqual(self.runner.calls[index + 1]["argv"], self.steps[index].argv)
            self.runner.finish()
            self._wait_for(
                lambda expected=index + 3: len(self.runner.calls) >= expected,
                f"setup did not advance after stage {index + 1}",
            )

        west_call = self.runner.calls[5]
        self.assertEqual(west_call["argv"], ("west", "packages", "pip"))
        self.assertEqual(west_call["cwd"], self.workspace)
        self.requirements_file.parent.mkdir(parents=True)
        self.requirements_file.write_text("demo-package==1.0\n", encoding="utf-8")
        self.runner.write_output("-r zephyr/scripts/requirements ")
        self.runner.write_output("with spaces.txt\n")
        self.runner.finish()

        self.assertEqual(len(self.runner.calls), 7)
        pip_call = self.runner.calls[6]
        self.assertEqual(
            pip_call["argv"],
            (
                str(self.workspace / ".venv" / "Scripts" / "python.exe"),
                "-m",
                "pip",
                "install",
                "-r",
                str(self.requirements_file),
            ),
        )
        self.assertEqual(pip_call["cwd"], self.workspace)
        self.assertTrue(all(isinstance(argument, str) for argument in pip_call["argv"]))

        self.runner.finish()
        self.assertEqual(self.runner.calls[7]["argv"], self.steps[5].argv)
        self.runner.finish()

        self.assertEqual(self.completed_paths, [str(self.workspace.resolve())])
        self.assertEqual(self.dialog.progress_bar.value(), 6)
        self.assertEqual(self.dialog._workflow, None)
        self.assertEqual(len(self.runner.calls), 8)

    def test_failed_step_stops_sequence_and_does_not_emit_completion(self):
        self._start_and_wait_for_first_command()

        self.runner.finish(exit_code=7)

        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(self.completed_paths, [])
        self.assertIsNone(self.dialog._workflow)
        self.assertTrue(any(kind == "critical" for kind, _args in self.messages.calls))

    def test_cancelled_step_stops_sequence_and_does_not_emit_completion(self):
        self._start_and_wait_for_first_command()

        self.dialog.stop_button.click()

        self.assertEqual(self.runner.stop_calls, 1)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(self.completed_paths, [])
        self.assertIsNone(self.dialog._workflow)
        self.assertIn("保留", self.dialog.status_label.text())


if __name__ == "__main__":
    unittest.main()
