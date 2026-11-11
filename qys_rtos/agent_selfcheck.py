"""Offline packaged-agent check using a temporary project and loopback API."""

from __future__ import annotations

import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .agent_config import AgentConfig
from .agent_service import AgentSession
from .agent_workspace import AgentWorkspace


def run_agent_selfcheck(report_path: Path) -> int:
    requests = []
    report: dict[str, object] = {"version": __version__, "success": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(data)
            if len(requests) == 1:
                call = {"name": "read_project_file", "arguments": json.dumps({"path": "src/main.c"})}
                message = {"role": "assistant", "content": None,
                           "tool_calls": [{"id": "call1", "type": "function", "function": call}]}
                finish = "tool_calls"
            elif len(requests) == 2:
                snapshot = json.loads(data["messages"][-1]["content"])
                arguments = {"path": "src/main.c", "content": "updated\n", "expected_fingerprint": snapshot["fingerprint"]}
                call = {"name": "write_project_file", "arguments": json.dumps(arguments)}
                message = {"role": "assistant", "content": None,
                           "tool_calls": [{"id": "call2", "type": "function", "function": call}]}
                finish = "tool_calls"
            else:
                message, finish = {"role": "assistant", "content": "done"}, "stop"
            body = json.dumps({"id": "selfcheck", "object": "chat.completion", "created": 1,
                               "model": "selfcheck", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            pass

    server = None
    thread = None
    try:
        with tempfile.TemporaryDirectory(prefix="qys-agent-selfcheck-") as temporary:
            root = Path(temporary) / "project"
            (root / "src").mkdir(parents=True)
            (root / "CMakeLists.txt").write_text("project(selfcheck)\n", encoding="utf-8")
            target = root / "src" / "main.c"
            target.write_text("original\n", encoding="utf-8")
            workspace = AgentWorkspace(root, Path(temporary) / "backups")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            config = AgentConfig(f"http://127.0.0.1:{server.server_port}/v1", "selfcheck", "test-key")
            answer = AgentSession(workspace, config=config).run("test")
            edited = target.read_text(encoding="utf-8") == "updated\n"
            workspace.restore_last()
            restored = target.read_text(encoding="utf-8") == "original\n"
            report.update({"http_requests": len(requests), "edited_and_restored": edited and restored,
                           "answer": answer, "success": answer == "done" and edited and restored and len(requests) == 3})
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["success"] else 1
