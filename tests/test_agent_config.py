import tempfile
import unittest
from pathlib import Path

from qys_rtos.agent_config import AgentConfig, AgentConfigError, load_agent_config, save_agent_config


class AgentConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qys-agent-config-")
        self.path = Path(self.temp.name) / "agent.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_config_round_trip_protects_key_at_rest(self):
        config = AgentConfig("https://api.example.com/v1", "example-model", "test-secret-123")
        save_agent_config(config, self.path)
        self.assertNotIn(b"test-secret-123", self.path.read_bytes())
        self.assertNotIn(b"api.example.com", self.path.read_bytes())
        self.assertEqual(load_agent_config(self.path), config)

    def test_rejects_remote_plaintext_http_and_url_credentials(self):
        for url in ("http://example.com/v1", "https://user:pass@example.com/v1", "https://example.com/v1?key=secret"):
            with self.subTest(url=url), self.assertRaises(AgentConfigError):
                save_agent_config(AgentConfig(url, "model", "secret"), self.path)
        self.assertFalse(self.path.exists())

    def test_allows_local_http_without_key(self):
        config = AgentConfig("http://127.0.0.1:11434/v1", "local-model", "")
        save_agent_config(config, self.path)
        self.assertEqual(load_agent_config(self.path), config)

    def test_rejects_empty_model_without_writing_settings(self):
        with self.assertRaises(AgentConfigError):
            save_agent_config(AgentConfig("https://api.example.com", "", "key"), self.path)
        self.assertFalse(self.path.exists())
