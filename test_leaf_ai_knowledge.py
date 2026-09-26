import unittest
from actions.leaf_ai_knowledge import leaf_ai_knowledge
from core.leaf_ai_client import (
    LeafAIClient,
    LeafAIConfig,
    LeafAIError,
    is_leaf_ai_query,
    sanitize_leaf_answer,
)
from core.tool_registry import ToolRegistry


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

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if isinstance(self.responses[0], Exception):
            raise self.responses.pop(0)
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if isinstance(self.responses[0], Exception):
            raise self.responses.pop(0)
        return self.responses.pop(0)


class LeafAIClientTests(unittest.TestCase):
    def setUp(self):
        self.config = LeafAIConfig(
            api_url="https://dify.example.com/v1",
            api_key="test-api-key",
            timeout=15,
        )

    def test_query_success_and_saves_conversation_id(self):
        session = FakeSession([
            FakeResponse(200, {
                "answer": "Dumelang! I am Leaf, your Virtual Campus Assistant.",
                "conversation_id": "conv-1234",
            })
        ])
        client = LeafAIClient(self.config, session=session)

        result = client.query("What is Leaf AI?")

        self.assertEqual(result["status"], "success")
        self.assertIn("Dumelang", result["answer"])
        self.assertEqual(result["conversation_id"], "conv-1234")
        self.assertEqual(client._conversation_id, "conv-1234")

        # Verify call details
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("POST", "https://dify.example.com/v1/chat-messages"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-api-key")
        self.assertEqual(kwargs["json"]["query"], "What is Leaf AI?")
        self.assertEqual(kwargs["json"]["response_mode"], "blocking")

    def test_query_empty_text_returns_friendly_message_without_network_call(self):
        session = FakeSession([])
        client = LeafAIClient(self.config, session=session)

        result = client.query("   ")
        self.assertEqual(result["status"], "empty_query")
        self.assertEqual(len(session.calls), 0)

    def test_sanitize_leaf_answer_removes_coffee_promo(self):
        raw = (
            "Here is the EBIT coach details:\n"
            "Megan Mackenzie - megan.mackenzie@up.ac.za\n\n"
            "*If you like Leaf and want more features, the developer would appreaciate a cup of coffee at https://buymeacoffee.com/petersonmambondimumwe*"
        )
        cleaned = sanitize_leaf_answer(raw)
        self.assertIn("Megan Mackenzie", cleaned)
        self.assertNotIn("buymeacoffee.com", cleaned)

    def test_timeout_raises_leaf_ai_error(self):
        import requests
        session = FakeSession([requests.Timeout("timed out")])
        client = LeafAIClient(self.config, session=session)

        with self.assertRaisesRegex(LeafAIError, "timed out"):
            client.query("Tell me about campus buses")

    def test_authentication_error_raises_leaf_ai_error(self):
        session = FakeSession([FakeResponse(401, {"error": "Unauthorized"})])
        client = LeafAIClient(self.config, session=session)

        with self.assertRaisesRegex(LeafAIError, "authentication failed"):
            client.query("What is Leaf AI?")

    def test_server_error_status_handled(self):
        session = FakeSession([FakeResponse(502, {"error": "Bad Gateway"})])
        client = LeafAIClient(self.config, session=session)

        with self.assertRaisesRegex(LeafAIError, "error status"):
            client.query("What is Leaf AI?")

    def test_empty_answer_handled(self):
        session = FakeSession([FakeResponse(200, {"answer": ""})])
        client = LeafAIClient(self.config, session=session)

        result = client.query("Unknown question")
        self.assertIn("No information was returned", result["answer"])

    def test_verify_success(self):
        session = FakeSession([FakeResponse(200, {"parameters": {}})])
        client = LeafAIClient(self.config, session=session)

        status = client.verify()
        self.assertTrue(status.get("healthy"))
        self.assertEqual(status.get("api_url"), "https://dify.example.com/v1")


class LeafAIActionTests(unittest.TestCase):
    def test_action_query_delegates_to_client(self):
        class MockClient:
            def query(self, text):
                return {"answer": f"Leaf answer to: {text}"}

        response = leaf_ai_knowledge(
            {"query": "Who is the EBIT coach?"},
            client=MockClient(),
        )
        self.assertEqual(response, "Leaf answer to: Who is the EBIT coach?")

    def test_action_verify_delegates_to_client(self):
        class MockClient:
            def verify(self):
                return {"healthy": True}

        response = leaf_ai_knowledge(
            {"action": "verify"},
            client=MockClient(),
        )
        self.assertIn("online and reachable", response)

    def test_action_missing_query_returns_prompt(self):
        class MockClient:
            pass

        response = leaf_ai_knowledge({}, client=MockClient())
        self.assertIn("Please specify", response)

    def test_action_catches_leaf_ai_error_gracefully(self):
        class FailingClient:
            def query(self, text):
                raise LeafAIError("Connection refused by endpoint")

        response = leaf_ai_knowledge(
            {"query": "What is Leaf?"},
            client=FailingClient(),
        )
        self.assertIn("Leaf AI knowledge retrieval was unavailable", response)
        self.assertIn("Connection refused", response)


class LeafAIIntentDetectionTests(unittest.TestCase):
    def test_identifies_leaf_ai_queries(self):
        queries = [
            "What is Leaf AI?",
            "Can you ask Leaf about this?",
            "Who are the Academic Success Coaches?",
            "Where can I find an ASC coach?",
            "Tell me the ASC details for EBIT",
            "What is the campus bus schedule?",
            "Where is the Department of Student Affairs?",
            "Can I read the PDBY newspaper?",
        ]
        for q in queries:
            self.assertTrue(is_leaf_ai_query(q), f"Failed to identify Leaf query: {q}")

    def test_rejects_non_leaf_ai_queries(self):
        queries = [
            "What is the current time in Johannesburg?",
            "Open Chrome browser",
            "What is the system status?",
            "Search the web for news about technology",
            "Hermes, run a background analysis",
            "Post this draft to LinkedIn",
            "How much RAM is currently free?",
        ]
        for q in queries:
            self.assertFalse(is_leaf_ai_query(q), f"Incorrectly identified non-Leaf query: {q}")


class LeafAIToolRegistryIntegrationTests(unittest.TestCase):
    def test_tool_registry_executes_leaf_ai_knowledge(self):
        import asyncio

        class MockClient:
            def query(self, text):
                return {"answer": "Campus coaching details retrieved."}

        registry = ToolRegistry()
        registry.register(
            "leaf_ai_knowledge",
            lambda args: leaf_ai_knowledge(args, client=MockClient()),
            timeout=10,
        )

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(
                registry.execute("leaf_ai_knowledge", {"query": "Tell me about ASC coaches"})
            )
            self.assertEqual(res, "Campus coaching details retrieved.")
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
