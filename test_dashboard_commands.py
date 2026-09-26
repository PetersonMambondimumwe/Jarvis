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


if __name__ == "__main__":
    unittest.main()
