import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pydantic_ai import CancellationToken
from pydantic_ai.exceptions import RunCancelled
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from qys_rtos.agent_service import AgentSession
from qys_rtos.agent_config import AgentConfig
from qys_rtos.agent_workspace import AgentWorkspace


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qys-agent-service-")
        self.root = Path(self.temp.name) / "project"
        (self.root / "src").mkdir(parents=True)
        (self.root / "CMakeLists.txt").write_text("project(test)\n", encoding="utf-8")
        (self.root / "src" / "main.c").write_text("before\n", encoding="utf-8")
        self.workspace = AgentWorkspace(self.root, Path(self.temp.name) / "backups")

    def tearDown(self):
        self.temp.cleanup()

    def test_real_agent_loop_reads_writes_and_makes_recoverable_backup(self):
        calls = []

        def fake_model(messages, info):
            calls.append({tool.name for tool in info.function_tools})
            if len(calls) == 1:
                return ModelResponse(parts=[ToolCallPart("read_project_file", {"path": "src/main.c"})])
            if len(calls) == 2:
                result = next(part for part in messages[-1].parts if isinstance(part, ToolReturnPart))
                snapshot = json.loads(result.content)
                return ModelResponse(parts=[ToolCallPart("write_project_file", {
                    "path": "src/main.c", "content": "after\n", "expected_fingerprint": snapshot["fingerprint"],
                })])
            return ModelResponse(parts=[TextPart("已完成")])

        session = AgentSession(self.workspace, model=FunctionModel(fake_model))
        self.assertEqual(session.run("修改测试文件"), "已完成")
        self.assertEqual((self.root / "src" / "main.c").read_text(encoding="utf-8"), "after\n")
        self.assertEqual(calls[0], {"list_project_files", "read_project_file", "write_project_file"})
        self.workspace.restore_last()
        self.assertEqual((self.root / "src" / "main.c").read_text(encoding="utf-8"), "before\n")

    def test_conversation_history_is_available_on_next_turn(self):
        requests = []

        def fake_model(messages, _info):
            requests.append(messages)
            return ModelResponse(parts=[TextPart("答复")])

        session = AgentSession(self.workspace, model=FunctionModel(fake_model))
        session.run("第一条")
        session.run("第二条")
        self.assertGreater(len(requests[1]), len(requests[0]))

    def test_cancelled_request_does_not_modify_project(self):
        token = CancellationToken()
        token.cancel()
        session = AgentSession(self.workspace, model=FunctionModel(lambda _messages, _info: ModelResponse(parts=[TextPart("ok")])))
        with self.assertRaises(RunCancelled):
            session.run("修改文件", cancellation_token=token)
        self.assertEqual((self.root / "src" / "main.c").read_text(encoding="utf-8"), "before\n")

    def test_openai_compatible_http_endpoint_drives_real_tool_loop(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                data = json.loads(self.rfile.read(length))
                requests.append((self.path, self.headers.get("Authorization"), data))
                if len(requests) == 1:
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "read_project_file", "arguments": json.dumps({"path": "src/main.c"})}}]}
                    finish = "tool_calls"
                elif len(requests) == 2:
                    snapshot = json.loads(data["messages"][-1]["content"])
                    arguments = {"path": "src/main.c", "content": "from API\n", "expected_fingerprint": snapshot["fingerprint"]}
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call2", "type": "function", "function": {"name": "write_project_file", "arguments": json.dumps(arguments)}}]}
                    finish = "tool_calls"
                else:
                    message, finish = {"role": "assistant", "content": "修改完成"}, "stop"
                body = json.dumps({"id": "mock-response", "object": "chat.completion", "created": 1,
                                   "model": "fake-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}).encode()
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
            config = AgentConfig(f"http://127.0.0.1:{server.server_port}/v1", "fake-model", "test-key")
            session = AgentSession(self.workspace, config=config)
            self.assertEqual(session.run("修改工程"), "修改完成")
            self.assertEqual((self.root / "src" / "main.c").read_text(encoding="utf-8"), "from API\n")
            self.assertEqual(len(requests), 3)
            self.assertEqual(requests[0][0], "/v1/chat/completions")
            self.assertEqual(requests[0][1], "Bearer test-key")
            self.assertEqual(requests[0][2]["model"], "fake-model")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
