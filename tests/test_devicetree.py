from __future__ import annotations

import importlib
from pathlib import Path
import unittest


SAMPLE_DTS = Path(
    r"D:\zephyrproject\my_projects\esp32c6_qys_breathing\build_qys"
    r"\esp32c6_devkitc_esp32c6_hpcore-f04ee9b3\zephyr\zephyr.dts"
)


def _api(testcase: unittest.TestCase, name: str):
    try:
        module = importlib.import_module("qys_rtos.devicetree")
        return getattr(module, name)
    except (ImportError, AttributeError) as exc:
        testcase.fail(f"required device-tree API {name!r} is unavailable: {exc}")


class DeviceTreeTests(unittest.TestCase):
    def test_parser_keeps_node_structure_labels_and_nested_property_values(self) -> None:
        parse_devicetree = _api(self, "parse_devicetree")

        source = r'''/dts-v1/;
/* This comment contains braces { }, parentheses (), and a fake node: ignored { }; */
/ {
    model = "Demo (board) {still a string; }";
    marker = "/* not a comment */ // still text";
    enabled;
    soc {
        uart0: serial@4000 {
            compatible = "vendor,uart";
            reg = <0x4000 0x100>;
            sequence = <1 2>, [03 04], "item; } ( )";
        };
    };
    aliases { serial0 = &uart0; };
    chosen { zephyr,console = &uart0; };
};
'''

        tree = parse_devicetree(source)

        self.assertEqual(tree.root.name, "/")
        self.assertEqual(tree.root.path, "/")
        self.assertEqual([child.name for child in tree.root.children], ["soc", "aliases", "chosen"])
        uart = tree.find_node("/soc/serial@4000")
        self.assertIsNotNone(uart)
        self.assertEqual(uart.path, "/soc/serial@4000")
        self.assertEqual(uart.name, "serial@4000")
        self.assertEqual(uart.labels, ("uart0",))
        self.assertEqual(uart.label, "uart0")
        self.assertEqual(uart.properties["compatible"], '"vendor,uart"')
        self.assertEqual(uart.properties["reg"], "<0x4000 0x100>")
        self.assertEqual(uart.properties["sequence"], '<1 2>, [03 04], "item; } ( )"')
        self.assertEqual(tree.root.properties["model"], '"Demo (board) {still a string; }"')
        self.assertEqual(tree.root.properties["marker"], '"/* not a comment */ // still text"')
        self.assertIsNone(tree.root.properties["enabled"])
        self.assertEqual(tree.aliases["serial0"], "/soc/serial@4000")
        self.assertEqual(tree.chosen["zephyr,console"], "/soc/serial@4000")
        self.assertIsNone(tree.find_node("/soc/missing"))


    def test_parser_keeps_expression_like_property_text_as_data(self) -> None:
        parse_devicetree = _api(self, "parse_devicetree")

        expression = 'run = __import__("os").system("echo unsafe");'
        tree = parse_devicetree(f"/dts-v1/; / {{ {expression} }};")

        self.assertEqual(tree.root.properties["run"], '__import__("os").system("echo unsafe")')


    def test_parser_reports_malformed_tree_instead_of_returning_a_partial_tree(self) -> None:
        DeviceTreeError = _api(self, "DeviceTreeError")
        parse_devicetree = _api(self, "parse_devicetree")

        with self.assertRaisesRegex(DeviceTreeError, "(?i)(unterminated|unclosed|expected)"):
            parse_devicetree('/dts-v1/; / { device { compatible = "broken"; };')


    def test_parser_rejects_unterminated_comments_and_unbalanced_values(self) -> None:
        DeviceTreeError = _api(self, "DeviceTreeError")
        parse_devicetree = _api(self, "parse_devicetree")

        malformed_sources = (
            '/dts-v1/; /* never closed / { };',
            '/dts-v1/; / { reg = <0x0 0x10; };',
            '/dts-v1/; / { }; / { };',
        )
        for source in malformed_sources:
            with self.subTest(source=source):
                with self.assertRaises(DeviceTreeError):
                    parse_devicetree(source)


    def test_reader_parses_the_local_zephyr_generated_dts(self) -> None:
        if not SAMPLE_DTS.is_file():
            self.skipTest("the local Zephyr sample DTS is unavailable")

        read_devicetree = _api(self, "read_devicetree")

        tree = read_devicetree(SAMPLE_DTS)

        self.assertEqual(tree.root.properties["model"], '"esp32c6_devkitc HP Core"')
        self.assertIsNotNone(tree.find_node("/soc/uart@60000000"))
        self.assertEqual(tree.aliases["led-strip"], "/soc/i2s@6000c000/ws2812@0")
        self.assertEqual(tree.chosen["zephyr,console"], "/soc/uart@60000000")


    def test_status_overlay_targets_only_a_validated_absolute_node_path(self) -> None:
        make_status_overlay = _api(self, "make_status_overlay")

        self.assertEqual(
            make_status_overlay("/soc/uart@60000000", "okay"),
            '&{/soc/uart@60000000} {\n    status = "okay";\n};',
        )
        self.assertEqual(
            make_status_overlay("/soc/uart@60000000", "disabled"),
            '&{/soc/uart@60000000} {\n    status = "disabled";\n};',
        )


    def test_status_overlay_rejects_invalid_or_injected_paths(self) -> None:
        DeviceTreeError = _api(self, "DeviceTreeError")
        make_status_overlay = _api(self, "make_status_overlay")

        for path in (
            "",
            "soc/uart@60000000",
            "//soc/uart@60000000",
            "/soc/",
            "/soc//uart@60000000",
            "/soc/../uart@60000000",
            "/soc/./uart@60000000",
            "/soc/é",
            "/soc/uart@60000000}; / { injected",
            "/soc/uart@60000000/*comment*/",
            "/soc/uart@60000000\n/ { injected",
        ):
            with self.subTest(path=path):
                with self.assertRaises(DeviceTreeError):
                    make_status_overlay(path, "okay")


    def test_status_overlay_rejects_any_status_other_than_the_two_supported_values(self) -> None:
        DeviceTreeError = _api(self, "DeviceTreeError")
        make_status_overlay = _api(self, "make_status_overlay")

        for status in ("ok", "okay; }; / { injected", "disabled\n/*x*/"):
            with self.subTest(status=status):
                with self.assertRaises(DeviceTreeError):
                    make_status_overlay("/soc/uart@60000000", status)
