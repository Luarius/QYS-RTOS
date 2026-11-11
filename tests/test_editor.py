import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QPlainTextEdit, QTabWidget
from PySide6.QtTest import QTest

from qys_rtos.editor import EditorWorkspace, _SyntaxHighlighter


class EditorWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "Zephyr 工程"
        (self.root / "src").mkdir(parents=True)
        (self.root / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n", encoding="utf-8")
        (self.root / "src" / "main.c").write_bytes(b"#include <zephyr/kernel.h>\r\nvoid main(void) {}\r\n")
        self.workspace = EditorWorkspace()

    def tearDown(self):
        self.workspace.close()
        self.temporary.cleanup()

    def _editor(self):
        tabs = self.workspace.findChild(QTabWidget)
        self.assertIsNotNone(tabs)
        editor = tabs.currentWidget()
        self.assertIsInstance(editor, QPlainTextEdit)
        return editor

    def test_set_project_opens_main_c_and_save_preserves_newlines(self):
        saved = []
        self.workspace.file_saved.connect(saved.append)

        self.assertTrue(self.workspace.set_project(self.root))

        self.assertEqual(self.workspace.project_root, self.root.resolve())
        self.assertIn("void main", self._editor().toPlainText())
        self._editor().appendPlainText("// edited")
        self.assertTrue(self.workspace.save_current())
        saved_bytes = (self.root / "src" / "main.c").read_bytes()
        self.assertTrue(saved_bytes.endswith(b"// edited"))
        self.assertEqual(saved_bytes.count(b"\n"), saved_bytes.count(b"\r\n"))
        self.assertEqual(saved, [str((self.root / "src" / "main.c").resolve())])

    def test_close_all_cancel_keeps_dirty_buffer_open(self):
        self.assertTrue(self.workspace.set_project(self.root))
        self._editor().appendPlainText("// unsaved")

        with patch.object(EditorWorkspace, "_ask_unsaved", return_value="cancel"):
            self.assertFalse(self.workspace.close_all())

        self.assertEqual(self.workspace.findChild(QTabWidget).count(), 1)
        self.assertIn("unsaved", self._editor().toPlainText())

    def test_external_change_cancel_does_not_overwrite_disk(self):
        self.assertTrue(self.workspace.set_project(self.root))
        self._editor().appendPlainText("// local")
        source = self.root / "src" / "main.c"
        source.write_bytes(b"external edit\n")

        with patch.object(EditorWorkspace, "_confirm_external_overwrite", return_value=False):
            self.assertFalse(self.workspace.save_current())

        self.assertEqual(source.read_bytes(), b"external edit\n")
        self.assertIn("local", self._editor().toPlainText())

    def test_open_file_reads_another_project_file(self):
        header = self.root / "src" / "board config.conf"
        header.write_text("CONFIG_TEST=y\n", encoding="utf-8")

        self.assertTrue(self.workspace.set_project(self.root))
        self.assertTrue(self.workspace.open_file(header))

        self.assertIn("CONFIG_TEST=y", self._editor().toPlainText())
        self.assertEqual(self.workspace.findChild(QTabWidget).count(), 2)

    def test_agent_reload_refreshes_clean_open_buffer(self):
        self.assertTrue(self.workspace.set_project(self.root))
        source = self.root / "src" / "main.c"
        source.write_text("updated by agent\n", encoding="utf-8")
        self.assertFalse(self.workspace.has_unsaved_changes())
        self.assertTrue(self.workspace.reload_external_file(source))
        self.assertEqual(self._editor().toPlainText(), "updated by agent\n")
        self.assertFalse(self._editor().document().isModified())

    def test_agent_reload_does_not_overwrite_unsaved_buffer(self):
        self.assertTrue(self.workspace.set_project(self.root))
        self._editor().appendPlainText("// unsaved")
        source = self.root / "src" / "main.c"
        source.write_text("external change\n", encoding="utf-8")
        self.assertTrue(self.workspace.has_unsaved_changes())
        self.assertFalse(self.workspace.reload_external_file(source))
        self.assertIn("unsaved", self._editor().toPlainText())

    def test_append_overlay_keeps_existing_unsaved_changes_until_user_saves(self):
        overlay = self.root / "app.overlay"
        original = '/* existing project overlay */\n'
        overlay.write_text(original, encoding="utf-8")
        self.assertTrue(self.workspace.set_project(self.root))
        self.assertTrue(self.workspace.open_file(overlay))
        self._editor().appendPlainText("/* unsaved local change */")

        snippet = '&{/soc/uart@0} { status = "okay"; };'
        self.assertTrue(self.workspace.append_overlay_snippet(overlay, snippet))
        self.assertIn("unsaved local change", self._editor().toPlainText())
        self.assertIn(snippet, self._editor().toPlainText())
        self.assertTrue(self._editor().document().isModified())
        self.assertEqual(overlay.read_text(encoding="utf-8"), original)
        self.assertTrue(self.workspace.save_current())
        self.assertIn(snippet, overlay.read_text(encoding="utf-8"))

    def test_append_overlay_rejects_files_outside_project_and_non_overlays(self):
        external = Path(self.temporary.name) / "outside.overlay"
        external.write_text("keep outside\n", encoding="utf-8")
        source = self.root / "src" / "main.c"
        before = source.read_bytes()
        self.assertTrue(self.workspace.set_project(self.root))

        self.assertFalse(self.workspace.append_overlay_snippet(external, "/* x */"))
        self.assertFalse(self.workspace.append_overlay_snippet(source, "/* x */"))
        self.assertEqual(external.read_text(encoding="utf-8"), "keep outside\n")
        self.assertEqual(source.read_bytes(), before)

    def test_rejects_non_project_directory_without_closing_current_project(self):
        self.assertTrue(self.workspace.set_project(self.root))
        old_root = self.workspace.project_root
        old_count = self.workspace.findChild(QTabWidget).count()
        invalid_root = Path(self.temporary.name) / "not a Zephyr project"
        invalid_root.mkdir()

        with patch.object(EditorWorkspace, "_show_error"):
            self.assertFalse(self.workspace.set_project(invalid_root))

        self.assertEqual(self.workspace.project_root, old_root)
        self.assertEqual(self.workspace.findChild(QTabWidget).count(), old_count)

    def test_tree_filters_output_and_dot_directories_until_enabled(self):
        build_file = self.root / "build" / "zephyr" / "zephyr.elf"
        git_file = self.root / ".git" / "config"
        build_file.parent.mkdir(parents=True)
        git_file.parent.mkdir()
        build_file.write_text("build output", encoding="utf-8")
        git_file.write_text("git data", encoding="utf-8")
        self.assertTrue(self.workspace.set_project(self.root))
        QTest.qWait(60)

        model = self.workspace._file_model
        proxy = self.workspace._tree_filter
        for path in (build_file, git_file):
            index = model.index(str(path))
            self.assertTrue(index.isValid())
            self.assertFalse(proxy.filterAcceptsRow(index.row(), index.parent()))

        self.workspace._show_hidden_checkbox.setChecked(True)
        for path in (build_file, git_file):
            index = model.index(str(path))
            self.assertTrue(proxy.filterAcceptsRow(index.row(), index.parent()))

    def test_unchanged_buffer_does_not_replace_external_file_on_save(self):
        self.assertTrue(self.workspace.set_project(self.root))
        source = self.root / "src" / "main.c"
        external_text = b"external edit\n"
        source.write_bytes(external_text)

        with patch.object(EditorWorkspace, "_confirm_external_overwrite", return_value=False):
            self.assertTrue(self.workspace.save_current())

        self.assertEqual(source.read_bytes(), external_text)

    def test_kconfig_filename_uses_configuration_highlighting(self):
        self.assertEqual(_SyntaxHighlighter._language_kind("", "Kconfig"), "config")

    def test_filter_change_falls_back_when_apis_cannot_be_paired(self):
        proxy = self.workspace._tree_filter
        with patch.object(proxy, "beginFilterChange", create=True) as begin_change:
            with patch.object(proxy, "endFilterChange", None, create=True):
                with patch.object(proxy, "invalidateFilter") as legacy_invalidate:
                    proxy.set_show_hidden(True)

        begin_change.assert_not_called()
        legacy_invalidate.assert_called_once_with()

    def test_syntax_highlighter_uses_dark_theme_contrast_colors(self):
        self.assertTrue(self.workspace.set_project(self.root))
        editor = self._editor()
        formats = editor._highlighter._rules
        colors = {char_format.foreground().color().name() for _, char_format in formats}
        colors.add(editor._highlighter._comment_format.foreground().color().name())
        expected = {
            "#dcdcaa",  # function names
            "#c792ea",  # keywords
            "#4ec9b0",  # types
            "#d7ba7d",  # strings
            "#b5cea8",  # numbers
            "#d9a7e6",  # preprocessor
            "#91a3b8",  # comments
        }
        self.assertTrue(expected.issubset(colors))

        overlay = self.root / "boards" / "custom.overlay"
        overlay.parent.mkdir()
        overlay.write_text("&gpio0 { status = \"okay\"; };\n", encoding="utf-8")
        self.assertTrue(self.workspace.open_file(overlay))
        dts_colors = {
            char_format.foreground().color().name()
            for _, char_format in self._editor()._highlighter._rules
        }
        self.assertIn("#75cfff", dts_colors)

    def test_tree_filter_toggle_uses_short_chinese_label(self):
        checkbox = self.workspace.findChild(QCheckBox)
        self.assertIsNotNone(checkbox)
        self.assertEqual(checkbox.text(), "显示隐藏目录")


if __name__ == "__main__":
    unittest.main()
