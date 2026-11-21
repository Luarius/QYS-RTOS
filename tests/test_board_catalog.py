import json
import tempfile
import unittest
from pathlib import Path

from qys_rtos.board_catalog import (
    BoardCatalogError,
    load_catalog,
    parse_catalog,
    save_catalog,
)
from qys_rtos.board_support import PioEnvironment, create_board_project, upload_port_for


ESP32_C6 = {
    "id": "esp32-c6-devkitc-1",
    "name": "Espressif ESP32-C6-DevKitC-1",
    "platform": "espressif32",
    "frameworks": ["espidf"],
    "mcu": "ESP32C6",
    "vendor": "Espressif",
}


class BoardCatalogTests(unittest.TestCase):
    def test_registry_board_creates_project_with_only_supported_framework(self):
        board, = parse_catalog("Registry notice\n" + json.dumps([ESP32_C6]) + "\n")
        with self.assertRaises(BoardCatalogError):
            board.extension("arduino")
        with tempfile.TemporaryDirectory() as temp:
            project = create_board_project(Path(temp) / "esp32c6", board.extension("espidf"))
            config = (project / "platformio.ini").read_text(encoding="utf-8")
            self.assertIn("platform = espressif32\n", config)
            self.assertIn("board = esp32-c6-devkitc-1\n", config)
            self.assertIn("framework = espidf\n", config)
            self.assertNotIn("upload_protocol", config)
            self.assertIn("void app_main(void)", (project / "src" / "main.c").read_text(encoding="utf-8"))
            self.assertTrue((project / "CMakeLists.txt").is_file())
            self.assertTrue((project / "src" / "CMakeLists.txt").is_file())

    def test_cache_retains_good_records_without_turning_directory_data_into_ini(self):
        injected = {**ESP32_C6, "platform": "espressif32\nextra_scripts = post:bad.py"}
        boards = parse_catalog(json.dumps([injected, ESP32_C6, ESP32_C6]))
        self.assertEqual([board.board for board in boards], ["esp32-c6-devkitc-1"])
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "config" / "board-catalog.json"
            save_catalog(cache, boards)
            cached, = load_catalog(cache)
            self.assertEqual(cached.frameworks, ("espidf",))
            cache.write_text("malformed", encoding="utf-8")
            self.assertEqual(load_catalog(cache), ())

    def test_builtin_template_retains_pinned_tools_and_probe(self):
        board, = parse_catalog(json.dumps([{
            "id": "bluepill_f103c8", "name": "Blue Pill", "platform": "ststm32",
            "frameworks": ["arduino", "stm32cube"],
        }]))
        extension = board.extension("stm32cube")
        self.assertEqual(extension.platform, "platformio/ststm32@19.7.1")
        self.assertEqual(extension.upload_protocol, "stlink")
        self.assertIn("HAL_GPIO_TogglePin", extension.source)

    def test_registry_accepts_real_plus_suffix_board_and_underscore_framework(self):
        boards = parse_catalog(json.dumps([
            {"id": "STC89C516RD+", "platform": "intel_mcs51", "frameworks": []},
            {"id": "bonfire-basic-dram", "platform": "Bonfire", "frameworks": ["bonfire-sdk", "_bare"]},
        ]))
        self.assertEqual(boards[0].extension("").board, "STC89C516RD+")
        self.assertEqual(boards[1].extension("_bare").framework, "_bare")

    def test_platform_default_upload_does_not_inherit_monitor_com_port(self):
        environment = PioEnvironment("qys", "ststm32", "nucleo_f401re", "")
        self.assertEqual(upload_port_for(environment, "COM26"), "")


if __name__ == "__main__":
    unittest.main()
