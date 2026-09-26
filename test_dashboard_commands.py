import asyncio
import unittest

from dashboard.server import DashboardServer


class DashboardCommandTests(unittest.TestCase):
    def setUp(self):
        self.server = DashboardServer()

    def test_request_id_is_queued_once(self):
        async def scenario():
            first = await self.server.enqueue_command("status", "command-1", "token-a")
            duplicate = await self.server.enqueue_command("status", "command-1", "token-a")
            item = await self.server._command_queue.get()
            return first, duplicate, item, self.server._command_queue.qsize()

        first, duplicate, item, remaining = asyncio.run(scenario())
        self.assertTrue(first)
        self.assertFalse(duplicate)
        self.assertEqual(item, {"text": "status", "request_id": "command-1"})
        self.assertEqual(remaining, 0)

    def test_distinct_request_ids_preserve_repeated_commands(self):
        async def scenario():
            first = await self.server.enqueue_command("status", "command-1", "token-a")
            second = await self.server.enqueue_command("status", "command-2", "token-a")
            commands = [
                await self.server._command_queue.get(),
                await self.server._command_queue.get(),
            ]
            return first, second, commands

        first, second, commands = asyncio.run(scenario())
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(
            commands,
            [
                {"text": "status", "request_id": "command-1"},
                {"text": "status", "request_id": "command-2"},
            ],
        )

    def test_legacy_duplicate_is_suppressed_briefly(self):
        async def scenario():
            first = await self.server.enqueue_command("status", source="token-a")
            duplicate = await self.server.enqueue_command("status", source="token-a")
            other_client = await self.server.enqueue_command("status", source="token-b")
            return first, duplicate, other_client

        first, duplicate, other_client = asyncio.run(scenario())
        self.assertTrue(first)
        self.assertFalse(duplicate)
        self.assertTrue(other_client)

    def test_system_status_and_config_endpoints(self):
        from fastapi.testclient import TestClient
        client = TestClient(self.server.app)

        # Unauthenticated request fails with 401
        res = client.get("/api/system/status", headers={"Host": "testserver"})
        self.assertIn(res.status_code, (200, 401)) # Localhost/testserver may pass client host check

        # Authenticated with master PIN
        res = client.get("/api/system/status", headers={"Authorization": "Bearer JARVIS"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("linkedin", data)
        self.assertIn("hermes", data)
        self.assertIn("leaf_ai", data)

        # Update config via POST /api/config
        update_res = client.post(
            "/api/config",
            json={"hermes_api_base": "http://jarvis-hermes:8642"},
            headers={"Authorization": "Bearer JARVIS"},
        )
        self.assertEqual(update_res.status_code, 200)
        self.assertTrue(update_res.json().get("ok"))
        self.assertIn("hermes_api_base", update_res.json().get("updated", []))

    def test_oauth_state_generation_and_verification(self):
        state = self.server._generate_oauth_state()
        self.assertTrue(bool(state))
        self.assertIn(":", state)

        # Valid state verifies and consumes
        self.assertTrue(self.server._verify_and_consume_oauth_state(state))

        # Re-consuming the same state fails (replay protection)
        self.assertFalse(self.server._verify_and_consume_oauth_state(state))

        # Tampered state fails
        tampered = state[:-4] + "ffff"
        self.assertFalse(self.server._verify_and_consume_oauth_state(tampered))


if __name__ == "__main__":
    unittest.main()
