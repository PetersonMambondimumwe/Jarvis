import asyncio
import tempfile
import unittest
from pathlib import Path

from actions import hermes_agent as hermes_action
from core.hermes_client import (
    HermesClient,
    HermesConfig,
    HermesError,
    extract_run_output,
    load_hermes_config,
)
from core.hermes_task_manager import HermesTaskManager


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class HermesClientTests(unittest.TestCase):
    def test_start_run_uses_idempotency_and_private_api(self):
        session = FakeSession([FakeResponse(202, {"run_id": "run-1", "status": "started"})])
        client = HermesClient(
            HermesConfig("http://jarvis-hermes:8642", "test-secret"),
            session=session,
        )

        result = client.start_run("Research the latest status", "request-1")

        self.assertEqual(result["run_id"], "run-1")
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("POST", "http://jarvis-hermes:8642/v1/runs"))
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], "request-1")
        self.assertEqual(kwargs["json"]["input"], "Research the latest status")
        self.assertIn("GitHub", kwargs["json"]["instructions"])
        self.assertIn("Vercel", kwargs["json"]["instructions"])
        self.assertIn("execution", kwargs["json"]["instructions"].lower())

    def test_request_falls_back_to_alternate_host_on_dns_failure(self):
        import requests
        session = FakeSession([
            requests.exceptions.ConnectionError("Failed to resolve 'jarvis-hermes'"),
            FakeResponse(200, {"status": "ok"}),
            FakeResponse(200, {"model": "hermes-agent", "features": {"run_submission": True}}),
        ])
        client = HermesClient(
            HermesConfig("http://jarvis-hermes:8642", "test-secret"),
            session=session,
        )

        status = client.verify()
        self.assertEqual(status["health"], "ok")
        # First call failed on jarvis-hermes, second succeeded on hermes
        self.assertEqual(session.calls[0][1], "http://jarvis-hermes:8642/health/detailed")
        self.assertEqual(session.calls[1][1], "http://hermes:8642/health/detailed")

    def test_authentication_errors_do_not_expose_response_details(self):
        session = FakeSession([FakeResponse(401, {"error": "secret upstream detail"})])
        client = HermesClient(HermesConfig("http://jarvis-hermes:8642", "bad"), session=session)

        with self.assertRaisesRegex(HermesError, "authentication failed"):
            client.verify()

    def test_load_hermes_config_defaults_to_internal_token_on_private_host(self):
        import os
        old_key = os.environ.pop("HERMES_API_KEY", None)
        old_base = os.environ.pop("HERMES_API_BASE", None)
        try:
            os.environ["HERMES_API_BASE"] = "http://jarvis-hermes:8642"
            cfg = load_hermes_config()
            self.assertEqual(cfg.api_key, "jarvis-hermes-internal-token")
        finally:
            if old_key is not None:
                os.environ["HERMES_API_KEY"] = old_key
            if old_base is not None:
                os.environ["HERMES_API_BASE"] = old_base

    def test_extract_run_output_reads_responses_style_messages(self):
        output = extract_run_output(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Task completed."}],
                    }
                ]
            }
        )
        self.assertEqual(output, "Task completed.")


class HermesTaskManagerTests(unittest.TestCase):
    def test_completed_task_is_persisted_and_announced(self):
        spoken = []
        with tempfile.TemporaryDirectory() as directory:
            manager = HermesTaskManager(speak_callback=spoken.append)
            manager.task_file = Path(directory) / "tasks.json"
            task_id = manager.register("Check the deployment", "run-1")

            class Client:
                def get_run(self, run_id):
                    return {"status": "completed", "output": "Deployment is healthy."}

            asyncio.run(manager._poll_task(task_id, "run-1", Client()))

            self.assertEqual(manager.tasks[task_id]["status"], "completed")
            self.assertIn("Deployment is healthy", manager.tasks[task_id]["result"])
            self.assertEqual(len(spoken), 1)
            self.assertIn(task_id, spoken[0])


class HermesActionTests(unittest.TestCase):
    def test_delegate_registers_background_run(self):
        class Client:
            def start_run(self, task, task_key):
                return {"run_id": "run-123", "status": "started"}

        class Manager:
            def register(self, task, run_id):
                self.registered = (task, run_id)
                return "task1234"

        manager = Manager()
        original = hermes_action._client
        hermes_action._client = lambda: Client()
        try:
            result = hermes_action.hermes_agent(
                {"action": "delegate", "task": "Prepare a report"},
                task_manager=manager,
            )
        finally:
            hermes_action._client = original

        self.assertEqual(manager.registered, ("Prepare a report", "run-123"))
        self.assertIn("task1234", result)
        self.assertIn("background", result)


if __name__ == "__main__":
    unittest.main()
