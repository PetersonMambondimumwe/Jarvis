import asyncio
import json
import unittest

from dashboard.server import DashboardServer


class FakeWebSocket:
    def __init__(self):
        self.events = []

    async def send_text(self, text):
        self.events.append(("text", text))

    async def close(self, code=1000):
        self.events.append(("close", code))


class DashboardAudioOwnershipTests(unittest.TestCase):
    def test_replacement_clears_old_audio_before_closing_socket(self):
        async def scenario():
            server = DashboardServer()
            old = FakeWebSocket()
            new = FakeWebSocket()
            await server._register_phone_speaker("phone-1", old)
            await server._register_phone_speaker("phone-1", new)
            return server, old, new

        server, old, new = asyncio.run(scenario())

        self.assertEqual(json.loads(old.events[0][1]), {"type": "clear_audio"})
        self.assertEqual(old.events[1], ("close", 4002))
        self.assertIs(server._phone_speaker_clients["phone-1"], new)
        self.assertNotIn(old, server._phone_speaker_ws_clients)
        self.assertIn(new, server._phone_speaker_ws_clients)

    def test_discarding_old_socket_does_not_remove_new_owner(self):
        async def scenario():
            server = DashboardServer()
            old = FakeWebSocket()
            new = FakeWebSocket()
            await server._register_phone_speaker("phone-1", old)
            await server._register_phone_speaker("phone-1", new)
            server._discard_phone_speaker(old)
            return server, new

        server, new = asyncio.run(scenario())
        self.assertIs(server._phone_speaker_clients["phone-1"], new)


if __name__ == "__main__":
    unittest.main()
