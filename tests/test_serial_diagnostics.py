import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qys_rtos.serial_diagnostics import diagnose_serial_setup, format_serial_error


class SerialDiagnosticsTests(unittest.TestCase):
    def _build_with_dts(self, text: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        build = Path(temporary.name)
        generated = build / "zephyr" / "zephyr.dts"
        generated.parent.mkdir(parents=True)
        generated.write_text(text, encoding="utf-8")
        return build

    def test_reports_generated_console_route_and_selected_usb_port_conservatively(self):
        build = self._build_with_dts(
            "/dts-v1/;\n"
            "/ {\n"
            "  chosen { zephyr,console = &uart0; };\n"
            "  soc {\n"
            "    uart0: serial@60000000 {\n"
            '      compatible = "espressif,esp32-uart";\n'
            '      status = "okay";\n'
            "    };\n"
            "    usb_serial: usb-serial {\n"
            '      compatible = "zephyr,cdc-acm-uart";\n'
            '      status = "disabled";\n'
            "    };\n"
            "  };\n"
            "};\n"
        )
        port = SimpleNamespace(
            device="COM26",
            description="USB Serial/JTAG Controller",
            hwid="USB VID:PID=303A:1001",
            manufacturer="Espressif",
            product="USB JTAG/serial debug unit",
            vid=0x303A,
            pid=0x1001,
        )

        result = diagnose_serial_setup(build, "COM26", port)

        self.assertEqual(result.console_route.label, "uart0")
        self.assertEqual(result.console_route.status, "okay")
        self.assertIn("COM26", result.text)
        self.assertIn("USB Serial/JTAG Controller", result.text)
        self.assertIn("zephyr,console", result.text)
        self.assertIn("无法仅凭", result.text)
        self.assertEqual(result.severity, "info")

    def test_generic_serial_metadata_is_not_described_as_usb(self):
        build = self._build_with_dts(
            "/ { chosen { zephyr,console = &uart0; }; "
            "uart0: serial@60000000 { status = \"okay\"; }; };\n"
        )
        port = SimpleNamespace(
            device="COM3", description="UART Adapter", hwid="n/a", vid=None, pid=None,
        )

        result = diagnose_serial_setup(build, "COM3", port)

        self.assertNotIn("USB", result.text)

    def test_warns_when_console_target_is_disabled(self):
        build = self._build_with_dts(
            "/ {\n"
            "  chosen { zephyr,console = &usb_serial; };\n"
            "  usb_serial: usb-serial {\n"
            '    compatible = "zephyr,cdc-acm-uart";\n'
            '    status = "disabled";\n'
            "  };\n"
            "};\n"
        )

        result = diagnose_serial_setup(build, "COM7")

        self.assertEqual(result.console_route.label, "usb_serial")
        self.assertEqual(result.console_route.status, "disabled")
        self.assertEqual(result.severity, "warning")
        self.assertIn("disabled", result.text)
        self.assertIn("请复查", result.text)

    def test_reports_unknown_when_generated_device_tree_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = diagnose_serial_setup(Path(temporary), "COM4")

        self.assertIsNone(result.console_route)
        self.assertEqual(result.severity, "unknown")
        self.assertIn("尚不能判断", result.text)
        self.assertIn("先成功编译", result.text)

    def test_reports_unknown_when_chosen_console_is_missing(self):
        build = self._build_with_dts("/ { chosen { zephyr,shell-uart = &uart0; }; };\n")

        result = diagnose_serial_setup(build, "COM4")

        self.assertIsNone(result.console_route)
        self.assertEqual(result.severity, "unknown")
        self.assertIn("未找到 zephyr,console", result.text)

    def test_does_not_infer_node_status_when_console_target_has_no_status_property(self):
        build = self._build_with_dts(
            "/ { chosen { zephyr,console = &uart0; }; "
            "uart0: serial@60000000 { "
            'compatible = "vendor,uart"; '
            "child { status = \"disabled\"; }; "
            "}; };\n"
        )

        result = diagnose_serial_setup(build, "COM4")

        self.assertEqual(result.console_route.label, "uart0")
        self.assertIsNone(result.console_route.status)
        self.assertEqual(result.severity, "unknown")
        self.assertIn("未显式声明", result.text)

    def test_does_not_read_a_console_reference_outside_the_chosen_node(self):
        build = self._build_with_dts(
            "/ { chosen { zephyr,shell-uart = &uart0; }; "
            "another { zephyr,console = &uart0; }; "
            "uart0: serial@60000000 { status = \"disabled\"; }; };\n"
        )

        result = diagnose_serial_setup(build, "COM4")

        self.assertIsNone(result.console_route)
        self.assertEqual(result.severity, "unknown")
        self.assertIn("未找到 zephyr,console", result.text)

    def test_status_property_does_not_match_a_vendor_property_suffix(self):
        build = self._build_with_dts(
            "/ { chosen { zephyr,console = &uart0; }; "
            "uart0: serial@60000000 { "
            'vendor,status = "disabled"; status = "okay"; '
            "}; };\n"
        )

        result = diagnose_serial_setup(build, "COM4")

        self.assertEqual(result.console_route.status, "okay")
        self.assertEqual(result.severity, "info")

    def test_resolves_absolute_console_path_and_reads_node_properties(self):
        build = self._build_with_dts(
            "/dts-v1/;\n"
            "/ { chosen { zephyr,console = &{/soc/serial@4000}; }; "
            'soc { serial@4000 { compatible = "vendor,uart"; status = "okay"; }; }; };\n'
        )

        result = diagnose_serial_setup(build, "COM4")

        self.assertEqual(result.console_route.label, "/soc/serial@4000")
        self.assertEqual(result.console_route.status, "okay")
        self.assertEqual(result.console_route.compatible, ("vendor,uart",))
        self.assertEqual(result.severity, "info")

    def test_absolute_console_path_without_status_stays_unknown(self):
        build = self._build_with_dts(
            "/dts-v1/;\n"
            "/ { chosen { zephyr,console = &{/soc/serial@4000}; }; "
            "soc { serial@4000 { child { status = \"disabled\"; }; }; }; };\n"
        )

        result = diagnose_serial_setup(build, "COM4")

        self.assertEqual(result.console_route.label, "/soc/serial@4000")
        self.assertIsNone(result.console_route.status)
        self.assertEqual(result.severity, "unknown")
        self.assertIn("未显式声明", result.text)

    def test_formats_access_denied_with_port_in_use_guidance(self):
        message = format_serial_error(OSError("Access is denied"), operation="open")

        self.assertIn("打开串口失败", message)
        self.assertIn("可能被其他程序占用", message)
        self.assertIn("刷新端口", message)

    def test_formats_missing_port_with_reenumeration_guidance(self):
        error = FileNotFoundError(2, "The system cannot find the file specified", "COM26")

        message = format_serial_error(error, operation="open")

        self.assertIn("端口可能已断开或编号变化", message)
        self.assertIn("重新选择端口", message)

    def test_formats_write_timeout_without_claiming_a_specific_hardware_fault(self):
        message = format_serial_error(TimeoutError("Write timeout"), operation="write")

        self.assertIn("发送等待超时", message)
        self.assertIn("确认设备和波特率", message)

    def test_formats_read_failure_with_general_recovery_steps(self):
        message = format_serial_error(OSError("device disconnected"), operation="read")

        self.assertIn("读取串口失败", message)
        self.assertIn("检查 USB/串口连接", message)
        self.assertIn("device disconnected", message)


if __name__ == "__main__":
    unittest.main()
