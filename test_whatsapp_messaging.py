import os
import unittest
from core.whatsapp_client import (
    WhatsAppClient,
    WhatsAppConfig,
    WhatsAppError,
    load_whatsapp_config,
    normalize_phone_number,
)
from actions.send_message import send_message


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


class WhatsAppClientTests(unittest.TestCase):
    def test_normalize_phone_number_international(self):
        self.assertEqual(normalize_phone_number("+27 71 234 5678"), "27712345678")
        self.assertEqual(normalize_phone_number("+1 (555) 019-2834"), "15550192834")
        self.assertEqual(normalize_phone_number("27712345678"), "27712345678")

    def test_normalize_phone_number_local_south_africa(self):
        # 10 digits starting with 0 converted to default SA country code 27
        self.assertEqual(normalize_phone_number("0712345678"), "27712345678")

    def test_normalize_phone_number_invalid(self):
        with self.assertRaises(WhatsAppError):
            normalize_phone_number("")
        with self.assertRaises(WhatsAppError):
            normalize_phone_number("abc")
        with self.assertRaises(WhatsAppError):
            normalize_phone_number("123")  # too short

    def test_send_text_success(self):
        cfg = WhatsAppConfig(phone_number_id="10987654321", token="fake-token", use_template=False)
        session = FakeSession([
            FakeResponse(200, {
                "messaging_product": "whatsapp",
                "contacts": [{"input": "27712345678", "wa_id": "27712345678"}],
                "messages": [{"id": "wamid.HBgLMTIz"}]
            })
        ])
        client = WhatsAppClient(cfg, session=session)
        res = client.send_text("+27 71 234 5678", "Good afternoon Sir Peterson")

        self.assertTrue(res["ok"])
        self.assertEqual(res["message_id"], "wamid.HBgLMTIz")
        self.assertEqual(res["recipient"], "27712345678")
        self.assertEqual(res["mode"], "text")

        self.assertEqual(len(session.calls), 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("10987654321/messages", url)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fake-token")
        self.assertEqual(kwargs["json"]["to"], "27712345678")
        self.assertEqual(kwargs["json"]["type"], "text")
        self.assertEqual(kwargs["json"]["text"]["body"], "Good afternoon Sir Peterson")

    def test_send_template_success(self):
        cfg = WhatsAppConfig(
            phone_number_id="10987654321",
            token="fake-token",
            template_name="jarvis_notification",
            template_lang="en",
        )
        session = FakeSession([
            FakeResponse(200, {
                "messaging_product": "whatsapp",
                "contacts": [{"input": "27712345678", "wa_id": "27712345678"}],
                "messages": [{"id": "wamid.TEMPLATE_MSG_123"}]
            })
        ])
        client = WhatsAppClient(cfg, session=session)
        res = client.send_template("+27 71 234 5678", "Good day, Greetings from Jarvis")

        self.assertTrue(res["ok"])
        self.assertEqual(res["message_id"], "wamid.TEMPLATE_MSG_123")
        self.assertEqual(res["recipient"], "27712345678")
        self.assertEqual(res["mode"], "template")
        self.assertEqual(res["template_name"], "jarvis_notification")

        self.assertEqual(len(session.calls), 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(kwargs["json"]["type"], "template")
        self.assertEqual(kwargs["json"]["template"]["name"], "jarvis_notification")
        self.assertEqual(kwargs["json"]["template"]["language"]["code"], "en")
        params = kwargs["json"]["template"]["components"][0]["parameters"]
        # Redundant leading "Good day, " is trimmed so it doesn't duplicate in the template body
        self.assertEqual(params[0]["text"], "Greetings from Jarvis")

    def test_send_message_defaults_to_template(self):
        cfg = WhatsAppConfig(phone_number_id="10987654321", token="fake-token", use_template=True)
        session = FakeSession([
            FakeResponse(200, {
                "messaging_product": "whatsapp",
                "messages": [{"id": "wamid.DEF_TMPL"}]
            })
        ])
        client = WhatsAppClient(cfg, session=session)
        res = client.send_message("+27 71 234 5678", "System update completed.")

        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "template")
        self.assertEqual(session.calls[0][2]["json"]["type"], "template")

    def test_send_message_meta_api_error(self):
        cfg = WhatsAppConfig(phone_number_id="10987654321", token="fake-token", use_template=False)
        session = FakeSession([
            FakeResponse(400, {
                "error": {
                    "message": "(#131030) Recipient phone number not in allowed list",
                    "type": "OAuthException",
                    "code": 131030
                }
            })
        ])
        client = WhatsAppClient(cfg, session=session)
        with self.assertRaisesRegex(WhatsAppError, "131030"):
            client.send_message("27712345678", "Test")


class SendMessageActionTests(unittest.TestCase):
    def test_send_message_missing_recipient(self):
        res = send_message({"platform": "whatsapp", "receiver": "", "message_text": "Hi"})
        self.assertIn("specify a recipient", res.lower())

    def test_send_message_missing_content(self):
        res = send_message({"platform": "whatsapp", "receiver": "+27712345678", "message_text": ""})
        self.assertIn("specify the message content", res.lower())


if __name__ == "__main__":
    unittest.main()
