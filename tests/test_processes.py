import os
import sys
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from qys_rtos.processes import CommandRunner


class CommandRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def wait_for_finish(self, runner, timeout_ms=15000):
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        result = []
        runner.finished.connect(lambda code, canceled: (result.append((code, canceled)), loop.quit()))
        timer.start(timeout_ms)
        loop.exec()
        timer.stop()
        self.assertTrue(result, "child process did not finish")
        return result[0]

    def test_streams_stdout_stderr_and_exit_code(self):
        with tempfile.TemporaryDirectory(prefix="qys path ") as directory:
            runner = CommandRunner()
            output = []
            runner.output.connect(output.append)
            env = dict(os.environ)
            env["QYS_TEST_VALUE"] = "value with spaces"
            self.assertTrue(runner.start([
                sys.executable, "-u", "-c",
                "import os,sys; print(os.environ['QYS_TEST_VALUE']); print('stderr-line',file=sys.stderr)",
            ], cwd=Path(directory), env=env))
            code, canceled = self.wait_for_finish(runner)
            self.assertEqual((code, canceled), (0, False))
            self.assertIn("value with spaces", "".join(output))
            self.assertIn("stderr-line", "".join(output))

    def test_interactive_input_reaches_child(self):
        runner = CommandRunner()
        output = []
        runner.output.connect(output.append)
        self.assertTrue(runner.start([
            sys.executable, "-u", "-c",
            "import sys; print('reply:' + sys.stdin.readline().strip(),flush=True)",
        ], interactive=True))
        self.assertTrue(runner.send("hello from QYS\n"))
        self.assertEqual(self.wait_for_finish(runner), (0, False))
        self.assertIn("reply:hello from QYS", "".join(output))

    def test_stream_preserves_utf8_when_a_character_is_split_across_reads(self):
        runner = CommandRunner()
        output = []
        runner.output.connect(output.append)
        script = (
            "import os,time; "
            "data='中文路径'.encode('utf-8'); "
            "[(os.write(1,bytes([b])),time.sleep(.015)) for b in data]"
        )
        self.assertTrue(runner.start([sys.executable, "-u", "-c", script]))
        self.assertEqual(self.wait_for_finish(runner), (0, False))
        self.assertEqual("".join(output), "中文路径")

    def test_failure_to_start_is_reported(self):
        runner = CommandRunner()
        output = []
        runner.output.connect(output.append)
        self.assertFalse(runner.start([str(Path(tempfile.gettempdir()) / "qys-does-not-exist.exe")]))
        self.assertTrue(output)


if __name__ == "__main__":
    unittest.main()
