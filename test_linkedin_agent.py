import os
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from actions import linkedin_agent as linkedin_action
from core.linkedin_client import (
    LinkedInClient,
    LinkedInConfig,
    LinkedInError,
    LinkedInTokenStore,
    load_linkedin_config,
)


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, post_responses=None, get_responses=None):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.post_responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.get_responses.pop(0)


class LinkedInClientTests(unittest.TestCase):
    def setUp(self):
        self.config = LinkedInConfig(
            "client-id",
            "client-secret",
            "https://jarvis.example.com/auth/linkedin/callback",
        )

    def test_authorization_url_requests_openid_and_posting_scopes(self):
        url = LinkedInClient(self.config).authorization_url("state-123")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["state"], ["state-123"])
        self.assertEqual(query["redirect_uri"], [self.config.redirect_uri])
        self.assertEqual(set(query["scope"][0].split()), {"openid", "profile", "email", "w_member_social"})

    def test_authorization_url_supports_custom_scopes(self):
        custom_cfg = LinkedInConfig(
            "client-id",
            "client-secret",
            "https://jarvis.example.com/auth/linkedin/callback",
            scopes="w_member_social",
        )
        url = LinkedInClient(custom_cfg).authorization_url("state-abc")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["scope"], ["w_member_social"])

    def test_exchange_fallback_with_configured_member_id(self):
        custom_cfg = LinkedInConfig(
            "client-id",
            "client-secret",
            "https://jarvis.example.com/auth/linkedin/callback",
            scopes="w_member_social",
            member_id="custom-sub-999",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "linkedin.enc"
            session = FakeSession(
                post_responses=[FakeResponse(200, {"access_token": "token-xyz", "expires_in": 3600})],
                get_responses=[FakeResponse(403, {})],  # userinfo or me fails
            )
            store = LinkedInTokenStore(custom_cfg, path)
            client = LinkedInClient(custom_cfg, session=session, store=store)

            token = client.exchange_code("oauth-code")
            self.assertEqual(token["profile"]["sub"], "custom-sub-999")
            self.assertTrue(client.status()["connected"])

    def test_exchange_encrypts_token_and_verifies_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "linkedin.enc"
            session = FakeSession(
                post_responses=[FakeResponse(200, {"access_token": "private-token", "expires_in": 3600})],
                get_responses=[FakeResponse(200, {"sub": "member-1", "name": "Test Member"})],
            )
            store = LinkedInTokenStore(self.config, path)
            client = LinkedInClient(self.config, session=session, store=store)

            token = client.exchange_code("oauth-code")

            self.assertEqual(token["profile"]["sub"], "member-1")
            self.assertNotIn(b"private-token", path.read_bytes())
            self.assertTrue(client.status()["connected"])

    def test_publish_uses_authenticated_member_and_public_visibility(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LinkedInTokenStore(self.config, Path(directory) / "linkedin.enc")
            store.save(
                {
                    "access_token": "private-token",
                    "expires_at": int(time.time()) + 3600,
                    "profile": {"sub": "member-1", "name": "Test Member"},
                }
            )
            session = FakeSession(
                post_responses=[FakeResponse(201, headers={"X-RestLi-Id": "urn:li:share:123"})]
            )
            client = LinkedInClient(self.config, session=session, store=store)

            post_id = client.publish_text("A carefully approved update.")

            self.assertEqual(post_id, "urn:li:share:123")
            _, _, request = session.calls[0]
            self.assertEqual(request["json"]["author"], "urn:li:person:member-1")
            self.assertEqual(request["json"]["lifecycleState"], "PUBLISHED")
            self.assertNotIn("private-token", str(request["json"]))

    def test_expired_token_cannot_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LinkedInTokenStore(self.config, Path(directory) / "linkedin.enc")
            store.save({"access_token": "expired", "expires_at": 1, "profile": {"sub": "member-1"}})
            client = LinkedInClient(self.config, store=store)
            with self.assertRaisesRegex(LinkedInError, "expired"):
                client.publish_text("Blocked")

    def test_legacy_misspelled_secret_is_supported(self):
        keys = ["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_PRIMARY_CLIENT_SECTRET"]
        previous = {key: os.environ.get(key) for key in keys}
        try:
            os.environ["LINKEDIN_CLIENT_ID"] = "legacy-client"
            os.environ.pop("LINKEDIN_CLIENT_SECRET", None)
            os.environ["LINKEDIN_PRIMARY_CLIENT_SECTRET"] = "legacy-secret"
            self.assertEqual(load_linkedin_config().client_secret, "legacy-secret")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_api_key_and_secret_aliases_supported(self):
        keys = ["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_API_KEY", "LINKEDIN_API_SECRET"]
        previous = {key: os.environ.get(key) for key in keys}
        try:
            os.environ.pop("LINKEDIN_CLIENT_ID", None)
            os.environ.pop("LINKEDIN_CLIENT_SECRET", None)
            os.environ["LINKEDIN_API_KEY"] = "alias-key-123"
            os.environ["LINKEDIN_API_SECRET"] = "alias-secret-456"
            config = load_linkedin_config()
            self.assertEqual(config.client_id, "alias-key-123")
            self.assertEqual(config.client_secret, "alias-secret-456")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_nested_linkedin_config_json_supported(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = Path(directory) / "api_keys.json"
            cfg_path.write_text(
                json.dumps({"linkedin": {"client_id": "nested-id", "client_secret": "nested-secret"}}),
                encoding="utf-8",
            )
            old_custom = os.environ.get("JARVIS_CONFIG_PATH")
            old_custom_env = os.environ.get("JARVIS_ENV_PATH")
            empty_env = Path(directory) / "empty.env"
            empty_env.write_text("", encoding="utf-8")
            try:
                os.environ["JARVIS_CONFIG_PATH"] = str(cfg_path)
                os.environ["JARVIS_ENV_PATH"] = str(empty_env)
                # Ensure env vars don't override the file
                keys = ["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_API_KEY", "LINKEDIN_API_SECRET"]
                prev_env = {k: os.environ.pop(k, None) for k in keys}
                try:
                    config = load_linkedin_config()
                    self.assertEqual(config.client_id, "nested-id")
                    self.assertEqual(config.client_secret, "nested-secret")
                finally:
                    for k, v in prev_env.items():
                        if v is not None:
                            os.environ[k] = v
            finally:
                if old_custom is not None:
                    os.environ["JARVIS_CONFIG_PATH"] = old_custom
                else:
                    os.environ.pop("JARVIS_CONFIG_PATH", None)
                if old_custom_env is not None:
                    os.environ["JARVIS_ENV_PATH"] = old_custom_env
                else:
                    os.environ.pop("JARVIS_ENV_PATH", None)


class LinkedInActionTests(unittest.TestCase):
    def test_publish_requires_explicit_confirmation_of_existing_draft(self):
        class Client:
            def publish_text(self, content):
                raise AssertionError("publish_text must not run without confirmation")

        original_client = linkedin_action._client
        original_path = linkedin_action.DRAFTS_PATH
        with tempfile.TemporaryDirectory() as directory:
            linkedin_action._client = lambda: Client()
            linkedin_action.DRAFTS_PATH = Path(directory) / "drafts.json"
            try:
                prepared = linkedin_action.linkedin_agent(
                    {"action": "prepare_post", "content": "Review this first."}
                )
                draft_id = prepared.split("LinkedIn draft ", 1)[1].split(" ", 1)[0]
                blocked = linkedin_action.linkedin_agent(
                    {"action": "publish_post", "draft_id": draft_id, "confirmed": False}
                )
            finally:
                linkedin_action._client = original_client
                linkedin_action.DRAFTS_PATH = original_path

        self.assertIn("Nothing has been published", prepared)
        self.assertIn("blocked", blocked)


if __name__ == "__main__":
    unittest.main()
