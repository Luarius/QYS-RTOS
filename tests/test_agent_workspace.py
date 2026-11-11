import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from qys_rtos.agent_workspace import AgentWorkspace, WorkspaceConflict, WorkspaceError


class AgentWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qys-agent-")
        self.root = Path(self.temp.name) / "project"
        (self.root / "src").mkdir(parents=True)
        (self.root / "CMakeLists.txt").write_text("project(test)\n", encoding="utf-8")
        self.original = b"line one\r\nline two\r\n"
        (self.root / "src" / "main.c").write_bytes(self.original)
        self.workspace = AgentWorkspace(self.root, Path(self.temp.name) / "backups")

    def tearDown(self):
        self.temp.cleanup()

    def test_edit_keeps_file_style_and_restores_original_bytes(self):
        snapshot = self.workspace.read_file("src/main.c")
        result = self.workspace.write_file("src/main.c", "new line\n", snapshot.fingerprint)
        self.assertEqual((self.root / "src" / "main.c").read_bytes(), b"new line\r\n")
        self.assertTrue(result.backup_dir.is_dir())
        self.workspace.restore_last()
        self.assertEqual((self.root / "src" / "main.c").read_bytes(), self.original)

    def test_refuses_traversal_hidden_secrets_and_build_outputs(self):
        (self.root / ".env").write_text("secret", encoding="utf-8")
        (self.root / "api_key.json").write_text("secret", encoding="utf-8")
        (self.root / "credentials").mkdir()
        (self.root / "credentials" / "prj.conf").write_text("secret", encoding="utf-8")
        (self.root / "build_qys").mkdir()
        (self.root / "build_qys" / "out.c").write_text("generated", encoding="utf-8")
        for path in ("../outside.c", ".env", "api_key.json", "credentials/prj.conf", "build_qys/out.c"):
            with self.subTest(path=path), self.assertRaises(WorkspaceError):
                self.workspace.read_file(path)
        listed = self.workspace.list_files()
        self.assertIn("src/main.c", listed)
        self.assertNotIn(".env", listed)
        self.assertNotIn("api_key.json", listed)
        self.assertNotIn("credentials/prj.conf", listed)
        self.assertNotIn("build_qys/out.c", listed)

    def test_edit_refuses_file_changed_after_model_read(self):
        snapshot = self.workspace.read_file("src/main.c")
        path = self.root / "src" / "main.c"
        path.write_text("external edit", encoding="utf-8")
        with self.assertRaises(WorkspaceConflict):
            self.workspace.write_file("src/main.c", "agent edit", snapshot.fingerprint)
        self.assertEqual(path.read_text(encoding="utf-8"), "external edit")

    def test_does_not_send_files_with_obvious_embedded_credentials(self):
        source = self.root / "src" / "wifi.h"
        source.write_text('#define WIFI_PASSWORD "example-password"\n', encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            self.workspace.read_file("src/wifi.h")
        source.write_text('#define WIFI_PASSWORD ""\n', encoding="utf-8")
        self.assertIn('WIFI_PASSWORD', self.workspace.read_file("src/wifi.h").text)

    def test_restore_refuses_to_overwrite_newer_changes(self):
        snapshot = self.workspace.read_file("src/main.c")
        self.workspace.write_file("src/main.c", "agent edit\n", snapshot.fingerprint)
        path = self.root / "src" / "main.c"
        path.write_text("newer user edit", encoding="utf-8")
        with self.assertRaises(WorkspaceConflict):
            self.workspace.restore_last()
        self.assertEqual(path.read_text(encoding="utf-8"), "newer user edit")

    def test_create_file_can_be_reverted_without_removing_other_files(self):
        self.workspace.write_file("src/new.c", "int value;\n", "")
        self.assertEqual((self.root / "src" / "new.c").read_text(encoding="utf-8"), "int value;\n")
        self.workspace.restore_last()
        self.assertFalse((self.root / "src" / "new.c").exists())
        self.assertEqual((self.root / "src" / "main.c").read_bytes(), self.original)

    def test_new_file_cannot_replace_existing_file(self):
        with self.assertRaises(WorkspaceConflict):
            self.workspace.write_file("src/main.c", "overwrite", "")
        self.assertEqual(hashlib.sha256((self.root / "src" / "main.c").read_bytes()).digest(),
                         hashlib.sha256(self.original).digest())

    def test_recovery_can_use_prepared_backup_if_write_finished_before_interruption(self):
        snapshot = self.workspace.read_file("src/main.c")
        record = self.workspace.write_file("src/main.c", "agent edit\n", snapshot.fingerprint)
        manifest = record.backup_dir / "manifest.json"
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        metadata["state"] = "prepared"
        manifest.write_text(json.dumps(metadata), encoding="utf-8")
        self.workspace.restore_last()
        self.assertEqual((self.root / "src" / "main.c").read_bytes(), self.original)

    def test_can_recover_original_file_if_target_was_deleted(self):
        snapshot = self.workspace.read_file("src/main.c")
        self.workspace.write_file("src/main.c", "agent edit\n", snapshot.fingerprint)
        (self.root / "src" / "main.c").unlink()
        self.workspace.restore_last()
        self.assertEqual((self.root / "src" / "main.c").read_bytes(), self.original)

    def test_interrupted_edit_can_be_force_restored_without_losing_partial_content(self):
        snapshot = self.workspace.read_file("src/main.c")
        record = self.workspace.write_file("src/main.c", "agent edit\n", snapshot.fingerprint)
        manifest = record.backup_dir / "manifest.json"
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        metadata["state"] = "prepared"
        manifest.write_text(json.dumps(metadata), encoding="utf-8")
        target = self.root / "src" / "main.c"
        target.write_bytes(b"partial write")
        with self.assertRaises(WorkspaceConflict):
            self.workspace.restore_last()
        restored = self.workspace.restore_last(force=True)
        self.assertEqual(target.read_bytes(), self.original)
        self.assertEqual((restored.backup_dir / "displaced-before-restore.bin").read_bytes(), b"partial write")

    def test_force_restore_of_new_file_keeps_newer_content_in_backup(self):
        record = self.workspace.write_file("src/new.c", "agent file\n", "")
        target = self.root / "src" / "new.c"
        target.write_text("user updated\n", encoding="utf-8")
        self.workspace.restore_last(force=True)
        self.assertFalse(target.exists())
        self.assertEqual((record.backup_dir / "displaced-before-restore.bin").read_text(encoding="utf-8"), "user updated\n")

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_existing_file_open_elsewhere_is_not_overwritten(self):
        source = self.root / "src" / "main.c"
        snapshot = self.workspace.read_file("src/main.c")
        with source.open("r+b"):
            with self.assertRaises(WorkspaceError):
                self.workspace.write_file("src/main.c", "agent edit\n", snapshot.fingerprint)
        self.assertEqual(source.read_bytes(), self.original)
