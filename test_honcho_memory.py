import json
import tempfile
import unittest
from pathlib import Path

from core.honcho_memory import HonchoMemory


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data if data is not None else {"ok": True}
        self.content = json.dumps(self._data).encode("utf-8")

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, representation=""):
        self.calls = []
        self.representation = representation

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/representation"):
            return FakeResponse(data={"representation": self.representation})
        return FakeResponse()


class HonchoMemoryTests(unittest.TestCase):
    def make_memory(self, session=None, enabled=True):
        return HonchoMemory(
            enabled=enabled,
            base_url="http://honcho-api:8000",
            api_key="test-token",
            http_session=session or FakeSession(),
        )

    def test_disabled_client_fails_open(self):
        memory = self.make_memory(enabled=False)
        self.assertFalse(memory.record_turn("hello", "hello, sir"))
        self.assertIn("not configured", memory.status())

    def test_record_turn_initializes_resources_once(self):
        session = FakeSession()
        memory = self.make_memory(session)

        self.assertTrue(memory.record_turn("first", "reply"))
        self.assertTrue(memory.record_turn("second", "reply"))

        urls = [call[1] for call in session.calls]
        self.assertEqual(sum(url.endswith("/v3/workspaces") for url in urls), 1)
        message_calls = [call for call in session.calls if call[1].endswith("/messages")]
        self.assertEqual(len(message_calls), 2)
        payload = message_calls[0][2]["json"]["messages"]
        self.assertEqual([item["peer_id"] for item in payload], ["peterson", "jarvis"])

    def test_recall_returns_representation(self):
        session = FakeSession("- The user is building Project ASC")
        memory = self.make_memory(session)

        result = memory.recall("What project am I building?")

        self.assertIn("Project ASC", result)
        recall_call = session.calls[-1]
        self.assertEqual(recall_call[2]["json"]["target"], "peterson")
        self.assertEqual(
            recall_call[2]["json"]["search_query"],
            "What project am I building?",
        )

    def test_from_config_requires_enabled_flag_and_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api_keys.json"
            path.write_text(
                json.dumps(
                    {
                        "honcho_enabled": True,
                        "honcho_base_url": "http://honcho-api:8000",
                        "honcho_api_key": "token",
                    }
                ),
                encoding="utf-8",
            )
            memory = HonchoMemory.from_config(path)

        self.assertTrue(memory.enabled)
        self.assertEqual(memory.workspace_id, "jarvis")


if __name__ == "__main__":
    unittest.main()
