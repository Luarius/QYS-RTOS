import json
import tempfile
import unittest
from pathlib import Path

from qys_rtos.board_support import (
    BOARD_EXTENSIONS,
    BoardSupportError,
    PioEnvironment,
    create_board_project,
    extension_install_command,
    parse_project_config,
    run_command,
    upload_port_for,
)


class BoardSupportTests(unittest.TestCase):
    def test_templates_follow_the_selected_board_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            for board in BOARD_EXTENSIONS:
                root = Path(temporary) / board.board
                self.assertEqual(create_board_project(root, board), root.resolve())
                ini = root / "platformio.ini"
                original = ini.read_text(encoding="utf-8")
                self.assertIn(f"board = {board.board}\n", original)
                self.assertIn(f"upload_protocol = {board.upload_protocol}\n", original)
                self.assertTrue((root / "src" / board.filename).is_file())
                with self.assertRaises(BoardSupportError):
                    create_board_project(root, board)
                self.assertEqual(ini.read_text(encoding="utf-8"), original)

    def test_compile_and_extension_install_have_no_implicit_upload_target(self):
        core = Path("C:/QYS Tools/pio.exe")
        project = Path("D:/My Projects/example")
        self.assertEqual(
            run_command(core, project, "debug"),
            [str(core), "--no-ansi", "run", "--project-dir", str(project),
             "--environment", "debug", "--target", "buildprog"],
        )
        self.assertEqual(
            extension_install_command(core, project),
            [str(core), "--no-ansi", "pkg", "install", "--project-dir", str(project), "--environment", "qys"],
        )

    def test_resolves_explicit_serial_and_does_not_send_com_to_stlink(self):
        serial = PioEnvironment("s3", "espressif32", "esp32-s3-devkitc-1", "")
        with self.assertRaises(BoardSupportError):
            upload_port_for(serial, "")
        self.assertEqual(upload_port_for(serial, "COM19"), "COM19")
        stc = PioEnvironment("stc", "intel_mcs51", "STC89C52RC", "stcgal", "COM7")
        self.assertEqual(upload_port_for(stc, ""), "COM7")
        self.assertEqual(
            run_command(Path("pio.exe"), Path("project"), "stc", upload=True, port="COM7")[-4:],
            ["--target", "upload", "--upload-port", "COM7"],
        )
        stm = PioEnvironment("f103", "ststm32", "bluepill_f103c8", "stlink")
        self.assertEqual(upload_port_for(stm, "COM19"), "")

    def test_reads_effective_environments_from_platformio_config(self):
        config = [
            ["env", [["platform", "espressif32"]]],
            ["env:test", [["board", "esp32-s3-devkitc-1"], ["platform", "espressif32"]]],
            ["env:tools", [["platform", "native"]]],
        ]
        output = f"Warning: project config output\n{json.dumps(config)}\n"
        self.assertEqual(
            parse_project_config(output),
            (PioEnvironment("test", "espressif32", "esp32-s3-devkitc-1", ""),
             PioEnvironment("tools", "native", "", "")),
        )
        with self.assertRaises(BoardSupportError):
            parse_project_config("Warning: no configuration")
