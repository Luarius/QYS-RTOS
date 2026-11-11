import tempfile
import unittest
from pathlib import Path

from qys_rtos.dts_workspace import active_project_overlays


class ActiveOverlaysTests(unittest.TestCase):
    def test_selects_existing_project_overlays_in_cmake_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "工程 with spaces"
            root.mkdir()
            first = root / "app.overlay"
            last = root / "board custom.overlay"
            outside = Path(temp) / "external.overlay"
            for path in (first, last, outside):
                path.write_text("/* keep */\n", encoding="utf-8")
            cache = root / "CMakeCache.txt"
            cache.write_text(
                f"DTC_OVERLAY_FILE:STRING={first.as_posix()};{outside.as_posix()}\n"
                f"EXTRA_DTC_OVERLAY_FILE:STRING={last.as_posix()}\n", encoding="utf-8"
            )
            self.assertEqual(active_project_overlays(cache, root), [first.resolve(), last.resolve()])

    def test_never_assumes_an_inactive_default_or_accepts_unverified_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "app"
            root.mkdir()
            (root / "app.overlay").write_text("keep", encoding="utf-8")
            cache = root / "CMakeCache.txt"
            cache.write_text("DTC_OVERLAY_FILE:STRING=missing.overlay\n", encoding="utf-8")
            self.assertEqual(active_project_overlays(cache, root), [])
            self.assertEqual(active_project_overlays(root / "missing-cache.txt", root), [])

    def test_relative_and_duplicate_overlay_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            overlay = root / "app.overlay"
            overlay.write_text("keep", encoding="utf-8")
            cache = root / "CMakeCache.txt"
            cache.write_text(
                'DTC_OVERLAY_FILE:STRING="app.overlay";app.overlay\n', encoding="utf-8"
            )
            self.assertEqual(active_project_overlays(cache, root), [overlay.resolve()])


if __name__ == "__main__":
    unittest.main()
