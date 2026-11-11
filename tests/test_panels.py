import os
import re
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import serial
from PySide6.QtWidgets import QApplication

from qys_rtos.panels import SerialPanel, ShellPanel
from qys_rtos.zephyr import discover_environment


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_until(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.03)
        return False

    def test_serial_monitor_sends_receives_and_releases_port(self):
        loopback = serial.serial_for_url("loop://", timeout=0, write_timeout=0.5, do_not_open=True)
        panel = SerialPanel()
        panel.set_port("COM_TEST")
        with patch("qys_rtos.panels.serial.serial_for_url", return_value=loopback):
            self.assertTrue(panel.open_port())
            self.assertFalse(loopback.dtr)
            self.assertFalse(loopback.rts)
            panel.input.setText("QYS serial check")
            panel.send()
            self.assertTrue(self.wait_until(lambda: "QYS serial check" in panel.output.toPlainText()))
            panel.close_port()
        self.assertFalse(loopback.is_open)
        self.assertFalse(panel.is_open)

    def test_serial_monitor_preserves_split_utf8_characters(self):
        loopback = serial.serial_for_url("loop://", timeout=0, write_timeout=0.5, do_not_open=True)
        panel = SerialPanel()
        panel.set_port("COM_TEST")
        with patch("qys_rtos.panels.serial.serial_for_url", return_value=loopback):
            self.assertTrue(panel.open_port())
            loopback.write("中".encode("utf-8")[:2])
            panel._poll()
            loopback.write("中".encode("utf-8")[2:])
            panel._poll()
            self.assertIn("中", panel.output.toPlainText())
            panel.close_port()

    def test_serial_panel_shows_build_route_and_actionable_open_error(self):
        with TemporaryDirectory() as temporary:
            generated = Path(temporary) / "zephyr" / "zephyr.dts"
            generated.parent.mkdir()
            generated.write_text(
                "/ { chosen { zephyr,console = &uart0; }; "
                'uart0: serial@60000000 { status = "okay"; }; };\n',
                encoding="utf-8",
            )
            panel = SerialPanel()
            panel.set_port("COM26")
            panel.set_diagnostic_context(
                temporary,
                SimpleNamespace(device="COM26", description="USB Serial/JTAG"),
            )

            self.assertIn("uart0", panel.description.toolTip())
            self.assertTrue(panel.diagnostic_label.wordWrap())
            self.assertIn("uart0", panel.diagnostic_label.text())
            self.assertIn("COM26", panel.description.toolTip())
            with patch(
                "qys_rtos.panels.serial.serial_for_url",
                side_effect=serial.SerialException("Access is denied"),
            ):
                self.assertFalse(panel.open_port())

            output = panel.output.toPlainText()
            self.assertIn("打开串口失败", output)
            self.assertIn("可能被其他程序占用", output)

    def test_serial_panel_refreshes_diagnostic_when_selected_port_changes(self):
        with TemporaryDirectory() as temporary:
            generated = Path(temporary) / "zephyr" / "zephyr.dts"
            generated.parent.mkdir()
            generated.write_text(
                "/ { chosen { zephyr,console = &uart0; }; "
                "uart0: serial@60000000 { status = \"okay\"; }; };\n",
                encoding="utf-8",
            )
            panel = SerialPanel()
            panel.set_diagnostic_context(
                temporary,
                SimpleNamespace(device="COM7", description="USB Serial Device"),
            )
            panel.set_port("COM7")
            self.assertIn("COM7", panel.description.toolTip())

            panel.set_port("COM8")

            self.assertIn("COM8", panel.description.toolTip())
            self.assertIn("uart0", panel.description.toolTip())
            self.assertNotIn("USB Serial Device", panel.diagnostic_label.text())

    def test_serial_panel_prompts_for_a_successful_build_when_context_is_missing(self):
        panel = SerialPanel()

        self.assertIn("先成功编译", panel.diagnostic_label.text())

    def test_serial_panel_explains_read_failure_and_closes_the_failed_connection(self):
        class BrokenSerial:
            is_open = True
            in_waiting = 1

            def read(self, _size):
                raise OSError("device disconnected")

            def close(self):
                self.is_open = False

        panel = SerialPanel()
        panel._serial = BrokenSerial()
        panel._poll()

        self.assertIn("读取串口失败", panel.output.toPlainText())
        self.assertIn("检查 USB/串口连接", panel.output.toPlainText())
        self.assertFalse(panel.is_open)

    def test_serial_panel_does_not_report_a_close_failure_as_success(self):
        class BrokenCloseSerial:
            is_open = True

            def close(self):
                raise OSError("device disconnected")

        panel = SerialPanel()
        panel._serial = BrokenCloseSerial()
        panel.close_port()

        output = panel.output.toPlainText()
        self.assertIn("关闭串口时遇到问题", output)
        self.assertNotIn("[串口已关闭]", output)
        self.assertFalse(panel.is_open)

    @unittest.skipUnless(os.name == "nt", "PowerShell console is Windows-specific")
    def test_manual_powershell_accepts_a_command_and_returns_output(self):
        panel = ShellPanel()
        environment = discover_environment()
        if environment:
            panel.set_context(environment.workspace, environment.process_env())
        self.assertTrue(panel.start_shell())
        panel.input.setText("Write-Output 'QYS_TERMINAL_OK'")
        panel.send_command()
        self.assertTrue(self.wait_until(lambda: re.search(r"(?m)^QYS_TERMINAL_OK$", panel.output.toPlainText()) is not None))
        panel.close_shell()
        self.assertTrue(self.wait_until(lambda: not panel.runner.is_running))


if __name__ == "__main__":
    unittest.main()
