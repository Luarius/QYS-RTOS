import hashlib
import tempfile
import unittest
from pathlib import Path

from qys_rtos.project import (
    FileChangedError,
    ProjectError,
    create_project,
    read_text_file,
    save_text_file,
    validate_project,
)


class ValidateProjectTests(unittest.TestCase):
    def test_returns_resolved_directory_when_cmake_file_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "Zephyr 工程"
            project.mkdir()
            (project / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n")

            self.assertEqual(validate_project(project), project.resolve())

    def test_rejects_directory_without_cmake_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "not-a-project"
            project.mkdir()

            with self.assertRaises(ProjectError):
                validate_project(project)

    def test_rejects_file_as_project_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            not_a_directory = Path(temporary) / "CMakeLists.txt"
            not_a_directory.write_text("", encoding="utf-8")

            with self.assertRaises(ProjectError):
                validate_project(not_a_directory)


class CreateProjectTests(unittest.TestCase):
    def _make_zephyr_samples(self, base: Path) -> None:
        hello = base / "samples" / "hello_world"
        hello.mkdir(parents=True)
        (hello / "CMakeLists.txt").write_text("hello cmake", encoding="utf-8")
        (hello / "src").mkdir()
        (hello / "src" / "main.c").write_text("hello source", encoding="utf-8")

        blinky = base / "samples" / "basic" / "blinky"
        blinky.mkdir(parents=True)
        (blinky / "CMakeLists.txt").write_text("blinky cmake", encoding="utf-8")

    def test_copies_hello_world_sample_to_unicode_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zephyr = root / "Zephyr SDK"
            self._make_zephyr_samples(zephyr)
            destination = root / "我的项目 space"

            created = create_project(destination, zephyr, "hello_world")

            self.assertEqual(created, destination.resolve())
            self.assertEqual((created / "CMakeLists.txt").read_text(encoding="utf-8"), "hello cmake")
            self.assertEqual((created / "src" / "main.c").read_text(encoding="utf-8"), "hello source")
            self.assertEqual(validate_project(created), created)

    def test_copies_blinky_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zephyr = root / "zephyr"
            self._make_zephyr_samples(zephyr)
            destination = root / "blinky-project"

            create_project(destination, zephyr, "blinky")

            self.assertEqual((destination / "CMakeLists.txt").read_text(encoding="utf-8"), "blinky cmake")

    def test_rejects_existing_nonempty_destination_without_changing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zephyr = root / "zephyr"
            self._make_zephyr_samples(zephyr)
            destination = root / "existing project"
            destination.mkdir()
            sentinel = destination / "keep.txt"
            sentinel.write_text("user data", encoding="utf-8")

            with self.assertRaises(ProjectError):
                create_project(destination, zephyr, "hello_world")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user data")
            self.assertEqual(list(destination.iterdir()), [sentinel])

    def test_rejects_existing_empty_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zephyr = root / "zephyr"
            self._make_zephyr_samples(zephyr)
            destination = root / "empty"
            destination.mkdir()

            with self.assertRaises(ProjectError):
                create_project(destination, zephyr, "hello_world")

            self.assertEqual(list(destination.iterdir()), [])

    def test_rejects_destination_inside_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zephyr = root / "zephyr"
            self._make_zephyr_samples(zephyr)
            template = zephyr / "samples" / "hello_world"
            destination = template / "nested copy"

            with self.assertRaises(ProjectError):
                create_project(destination, zephyr, "hello_world")

            self.assertFalse(destination.exists())

    def test_rejects_unknown_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zephyr = root / "zephyr"
            self._make_zephyr_samples(zephyr)

            with self.assertRaises(ProjectError):
                create_project(root / "project", zephyr, "other")


class TextFileTests(unittest.TestCase):
    def test_rejects_binary_file_with_nul_without_changing_its_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "firmware.elf"
            original = b"\x7fELF\x02\x01\x01\x00binary payload\x00"
            path.write_bytes(original)

            with self.assertRaisesRegex(ProjectError, "NUL|binary"):
                read_text_file(path)

            self.assertEqual(path.read_bytes(), original)

    def test_reads_utf8_bom_and_normalizes_newlines(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "源文件.c"
            path.write_bytes(b"\xef\xbb\xbfline one\r\nline two\n")

            snapshot = read_text_file(path)

            self.assertEqual(snapshot.text, "line one\nline two\n")
            self.assertEqual(snapshot.encoding, "utf-8-sig")
            self.assertEqual(snapshot.newline, "\r\n")
            self.assertEqual(snapshot.fingerprint, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_reads_utf8_without_bom_and_detects_lf(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "main.c"
            path.write_bytes("第一行\n第二行\n".encode("utf-8"))

            snapshot = read_text_file(path)

            self.assertEqual(snapshot.text, "第一行\n第二行\n")
            self.assertEqual(snapshot.encoding, "utf-8")
            self.assertEqual(snapshot.newline, "\n")

    def test_falls_back_to_gb18030(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.c"
            path.write_bytes("旧编码文本\r第二行".encode("gb18030"))

            snapshot = read_text_file(path)

            self.assertEqual(snapshot.text, "旧编码文本\n第二行")
            self.assertEqual(snapshot.encoding, "gb18030")
            self.assertEqual(snapshot.newline, "\r")

    def test_save_preserves_bom_crlf_and_returns_updated_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hello world.c"
            path.write_bytes(b"\xef\xbb\xbffirst\r\n")
            snapshot = read_text_file(path)

            updated = save_text_file(snapshot, "新内容\n下一行\n")

            self.assertEqual(path.read_bytes(), b"\xef\xbb\xbf\xe6\x96\xb0\xe5\x86\x85\xe5\xae\xb9\r\n\xe4\xb8\x8b\xe4\xb8\x80\xe8\xa1\x8c\r\n")
            self.assertEqual(updated.text, "新内容\n下一行\n")
            self.assertEqual(updated.encoding, "utf-8-sig")
            self.assertEqual(updated.newline, "\r\n")
            self.assertEqual(updated.fingerprint, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_save_preserves_gb18030_and_lone_cr_newlines(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.c"
            path.write_bytes("旧行\r旧行二".encode("gb18030"))
            snapshot = read_text_file(path)

            save_text_file(snapshot, "新行\n新行二")

            self.assertEqual(path.read_bytes(), "新行\r新行二".encode("gb18030"))

    def test_rejects_changed_file_without_overwriting_external_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "main.c"
            path.write_bytes(b"original\n")
            snapshot = read_text_file(path)
            path.write_bytes(b"external edit\n")

            with self.assertRaises(FileChangedError):
                save_text_file(snapshot, "our edit\n")

            self.assertEqual(path.read_bytes(), b"external edit\n")

    def test_treats_deleted_file_as_external_change_unless_overridden(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "main.c"
            path.write_bytes(b"original\n")
            snapshot = read_text_file(path)
            path.unlink()

            with self.assertRaises(FileChangedError):
                save_text_file(snapshot, "our edit\n")

            self.assertFalse(path.exists())

            updated = save_text_file(snapshot, "our edit\n", overwrite_external=True)
            self.assertEqual(path.read_bytes(), b"our edit\n")
            self.assertEqual(updated.text, "our edit\n")

    def test_explicit_override_replaces_external_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "main.c"
            path.write_bytes(b"original\n")
            snapshot = read_text_file(path)
            path.write_bytes(b"external edit\n")

            save_text_file(snapshot, "our edit\n", overwrite_external=True)

            self.assertEqual(path.read_bytes(), b"our edit\n")


if __name__ == "__main__":
    unittest.main()
