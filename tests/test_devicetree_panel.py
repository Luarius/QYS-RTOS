import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from qys_rtos.devicetree import parse_devicetree
from qys_rtos.devicetree_panel import DeviceTreePanel


SAMPLE = '''/dts-v1/; / {
    soc { uart0: serial@4000 { status = "disabled"; current-speed = <115200>; }; };
};'''


class DeviceTreePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panel = DeviceTreePanel()

    def tearDown(self):
        self.panel.close()

    def test_snapshot_shows_properties_and_generates_reviewable_preview(self):
        with TemporaryDirectory() as temp:
            overlay = Path(temp) / "app.overlay"
            overlay.write_text("/* existing */", encoding="utf-8")
            self.panel.set_snapshot(parse_devicetree(SAMPLE), [overlay])
            root = self.panel.tree.topLevelItem(0)
            node = root.child(0).child(0)
            self.panel.tree.setCurrentItem(node)
            self.assertIn("/soc/serial@4000", self.panel.node_path.text())
            self.assertEqual(self.panel.properties.topLevelItemCount(), 2)
            self.panel.preview_button.click()
            self.assertIn('&{/soc/serial@4000}', self.panel.preview.toPlainText())
            self.assertIn('status = "okay";', self.panel.preview.toPlainText())
            sent = []
            self.panel.append_requested.connect(lambda path, text: sent.append((path, text)))
            self.panel.append_button.click()
            self.assertEqual(sent[0][0], str(overlay))
            self.assertIn("status", sent[0][1])
            self.assertEqual(overlay.read_text(encoding="utf-8"), "/* existing */")

    def test_filter_preserves_ancestors_and_clear_removes_stale_content(self):
        self.panel.set_snapshot(parse_devicetree(SAMPLE), [])
        root = self.panel.tree.topLevelItem(0)
        self.panel.search.setText("uart0")
        self.assertFalse(root.isHidden())
        self.assertFalse(root.child(0).child(0).isHidden())
        self.panel.clear_snapshot("请先编译当前工程")
        self.assertEqual(self.panel.tree.topLevelItemCount(), 0)
        self.assertIn("请先编译", self.panel.summary.text())
        self.assertFalse(self.panel.append_button.isEnabled())

    def test_without_verified_overlay_only_preview_is_available(self):
        self.panel.set_snapshot(parse_devicetree(SAMPLE), [])
        self.panel.tree.setCurrentItem(self.panel.tree.topLevelItem(0).child(0).child(0))
        self.panel.preview_button.click()
        self.assertTrue(self.panel.preview.toPlainText())
        self.assertFalse(self.panel.append_button.isEnabled())
        self.assertIn("没有识别", self.panel.overlay_hint.text())

    def test_long_overlay_path_does_not_squeeze_node_tree(self):
        self.panel.resize(1000, 500)
        self.panel.set_snapshot(parse_devicetree(SAMPLE), [Path("C:/" + "long_directory/" * 12 + "app.overlay")])
        self.panel.show()
        self.app.processEvents()
        self.assertGreaterEqual(self.panel.tree.width(), 240)


if __name__ == "__main__":
    unittest.main()
