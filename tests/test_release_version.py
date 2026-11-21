import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qys_rtos import __version__
from qys_rtos.config import load_settings, save_settings, settings_path
from scripts.create_installer import create_script, find_nsis_root


class ReleaseVersionTests(unittest.TestCase):
    def test_installer_version_matches_app_version(self):
        self.assertEqual(__version__, "1.3")
        with tempfile.TemporaryDirectory(prefix="qys-version-") as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            (app / "QYS RTOS.exe").write_bytes(b"test")
            script = create_script(app, root / "setup.exe", root / "icon.ico")
            self.assertIn('VIProductVersion "1.3.0.0"', script)
            self.assertIn('"DisplayVersion" "1.3"', script)
            self.assertIn('InstallDir "$LOCALAPPDATA\\Programs\\QYS RTOS 1.3"', script)
            self.assertIn('IfFileExists "$INSTDIR\\qys-installed-1.3.txt" valid_folder verify_empty', script)
            self.assertNotIn('"$DESKTOP\\QYS RTOS.lnk"', script)

    def test_nsis_compiler_root_and_bin_layouts_are_both_supported(self):
        with tempfile.TemporaryDirectory(prefix="qys-nsis-") as tmp:
            root = Path(tmp) / "NSIS"
            (root / "Stubs").mkdir(parents=True)
            (root / "Bin").mkdir()
            root_compiler = root / "makensis.exe"
            bin_compiler = root / "Bin" / "makensis.exe"
            root_compiler.touch()
            bin_compiler.touch()
            self.assertEqual(find_nsis_root(root_compiler), root.resolve())
            self.assertEqual(find_nsis_root(bin_compiler), root.resolve())

    def test_build_script_is_readable_by_windows_powershell_5(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "build.ps1"
        self.assertTrue(script.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_migrates_zephyr_settings_without_writing_the_previous_version(self):
        with tempfile.TemporaryDirectory(prefix="qys-settings-") as temporary:
            legacy = Path(temporary) / "QYS RTOS" / "settings.json"
            legacy.parent.mkdir()
            original = '{"board": "esp32c6_devkitc"}'
            legacy.write_text(original, encoding="utf-8")
            previous = Path(temporary) / "QYS RTOS 1.2" / "settings.json"
            previous.parent.mkdir()
            saved_v12 = '{"board": "esp32c6_devkitc/esp32c6/hpcore", "ordinary_environment": "qys"}'
            previous.write_text(saved_v12, encoding="utf-8")
            with patch.dict(os.environ, {"APPDATA": temporary, "QYS_CONFIG_HOME": ""}):
                settings = load_settings()
                self.assertEqual(settings.board, "esp32c6_devkitc/esp32c6/hpcore")
                self.assertEqual(settings.ordinary_environment, "qys")
                settings.ordinary_port = "COM7"
                save_settings(settings)
                self.assertEqual(settings_path().parent.name, "QYS RTOS 1.3")
                self.assertEqual(load_settings().ordinary_port, "COM7")
                self.assertEqual(legacy.read_text(encoding="utf-8"), original)
                self.assertEqual(previous.read_text(encoding="utf-8"), saved_v12)
