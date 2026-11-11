import os
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qys_rtos.agent_config import AgentConfig
from qys_rtos.agent_panel import AgentPanel


class AgentPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qys-agent-panel-")
        self.root = Path(self.temp.name) / "project"
        (self.root / "src").mkdir(parents=True)
        (self.root / "CMakeLists.txt").write_text("project(test)\n", encoding="utf-8")
        (self.root / "src" / "main.c").write_text("before\n", encoding="utf-8")
        self.ready = True
        self.panel = AgentPanel(lambda: self.root, lambda: self.ready, lambda: "compiler log",
                                backup_root=Path(self.temp.name) / "backups")
        self.panel.config = AgentConfig("http://localhost:8080/v1", "test", "")

    def tearDown(self):
        self.panel.shutdown()
        self.panel.close()
        self.temp.cleanup()

    def test_preflight_failure_does_not_start_agent(self):
        self.ready = False
        self.panel.prompt.setPlainText("修改代码")
        self.panel.send_task()
        self.assertFalse(self.panel.busy)
        self.assertEqual((self.root / "src" / "main.c").read_text(encoding="utf-8"), "before\n")

    def test_background_task_reports_file_change_and_preserves_backup(self):
        changed = []
        busy = []
        self.panel.file_changed.connect(changed.append)
        self.panel.running_changed.connect(busy.append)
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(data)
                if len(calls) == 1:
                    name, args = "read_project_file", {"path": "src/main.c"}
                elif len(calls) == 2:
                    snapshot = json.loads(data["messages"][-1]["content"])
                    name, args = "write_project_file", {"path": "src/main.c", "content": "after\n", "expected_fingerprint": snapshot["fingerprint"]}
                else:
                    name = None
                message = ({"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
                           if name else {"role": "assistant", "content": "已完成"})
                body = json.dumps({"id": "id", "object": "chat.completion", "created": 1, "model": "test", "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if name else "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.panel.config = AgentConfig(f"http://127.0.0.1:{server.server_port}/v1", "test", "")
            self.panel.prompt.setPlainText("修改代码")
            self.panel.send_task()
            for _ in range(2000):
                if not self.panel.busy:
                    break
                QTest.qWait(25)
            self.assertFalse(self.panel.busy)
            self.assertEqual(busy, [True, False])
            self.assertEqual(changed, [str((self.root / "src" / "main.c").resolve())])
            self.assertEqual((self.root / "src" / "main.c").read_text(encoding="utf-8"), "after\n")
            with patch("qys_rtos.agent_panel.QMessageBox.information"):
                self.panel.restore_last()
            self.assertEqual((self.root / "src" / "main.c").read_text(encoding="utf-8"), "before\n")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_real_agent_runs_on_worker_against_local_compatible_endpoint(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps({"id": "test", "object": "chat.completion", "created": 1, "model": "test",
                                   "choices": [{"index": 0, "message": {"role": "assistant", "content": "连接成功"}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.panel.config = AgentConfig(f"http://127.0.0.1:{server.server_port}/v1", "test", "")
            self.panel.prompt.setPlainText("检查连接")
            self.panel.send_task()
            for _ in range(1200):
                if not self.panel.busy:
                    break
                QTest.qWait(25)
            self.assertFalse(self.panel.busy)
            self.assertIn("连接成功", self.panel.conversation.toPlainText())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
