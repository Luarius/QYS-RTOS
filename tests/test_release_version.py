import tempfile
import unittest
from pathlib import Path

from qys_rtos import __version__
from scripts.create_installer import create_script, find_nsis_root


class ReleaseVersionTests(unittest.TestCase):
    def test_installer_version_matches_app_version(self):
        self.assertEqual(__version__, "1.1")
        with tempfile.TemporaryDirectory(prefix="qys-version-") as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            (app / "QYS RTOS.exe").write_bytes(b"test")
            script = create_script(app, root / "setup.exe", root / "icon.ico")
            self.assertIn('VIProductVersion "1.1.0.0"', script)
            self.assertIn('"DisplayVersion" "1.1"', script)

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
