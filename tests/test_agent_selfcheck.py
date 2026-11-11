import json
import tempfile
import unittest
from pathlib import Path

from qys_rtos.agent_selfcheck import run_agent_selfcheck


class AgentSelfCheckTests(unittest.TestCase):
    def test_selfcheck_runs_real_local_api_tool_and_restores_file(self):
        with tempfile.TemporaryDirectory(prefix="qys-agent-selfcheck-") as tmp:
            report = Path(tmp) / "report.json"
            self.assertEqual(run_agent_selfcheck(report), 0)
            result = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(result["success"])
            self.assertTrue(result["edited_and_restored"])
            self.assertEqual(result["http_requests"], 3)
