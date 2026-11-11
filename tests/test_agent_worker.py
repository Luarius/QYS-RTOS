import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class AgentWorkerTests(unittest.TestCase):
    def test_worker_runs_without_qt_and_reports_real_tool_edits(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(data)
                if len(calls) == 1:
                    name, args = "read_project_file", {"path": "src/main.c"}
                elif len(calls) == 2:
                    item = json.loads(data["messages"][-1]["content"])
                    name, args = "write_project_file", {"path": "src/main.c", "content": "after\n", "expected_fingerprint": item["fingerprint"]}
                else:
                    name = None
                message = ({"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
                           if name else {"role": "assistant", "content": "完成"})
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
            with tempfile.TemporaryDirectory(prefix="qys-worker-") as temporary:
                root = Path(temporary) / "测试工程"
                (root / "src").mkdir(parents=True)
                (root / "CMakeLists.txt").write_text("project(test)\n", encoding="utf-8")
                target = root / "src" / "main.c"
                target.write_text("before\n", encoding="utf-8")
                request = {"project": str(root), "backup_root": str(Path(temporary) / "backups"),
                           "base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "test", "api_key": "test-private-key",
                           "prompt": "修改工程", "history": "", "build_output": ""}
                env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYDANTIC_AI_NO_BANNER="1", QT_QPA_PLATFORM="offscreen")
                packaged = os.environ.get("QYS_PACKAGED_EXE", "").strip()
                command = ([packaged, "--qys-agent-worker"] if packaged else
                           [sys.executable, str(Path(__file__).resolve().parents[1] / "run_app.py"), "--qys-agent-worker"])
                process = subprocess.run(command, input=json.dumps(request) + "\n", capture_output=True, text=True,
                                         encoding="utf-8", env=env, timeout=120)
                self.assertEqual(process.returncode, 0, process.stderr)
                events = [json.loads(line) for line in process.stdout.splitlines()]
                self.assertEqual(events[-1]["type"], "completed")
                self.assertEqual(events[-1]["answer"], "完成")
                self.assertTrue(any(event["type"] == "file_changed" for event in events))
                self.assertNotIn("test-private-key", process.stdout + process.stderr)
                self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
