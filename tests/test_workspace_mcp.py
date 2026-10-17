"""
tests/test_workspace_mcp.py
───────────────────────────
Unit and integration tests for:
  1. actions/github_manager.py mutation actions (rename_repo, create_repo, update_repo, delete_repo, create_or_update_file)
  2. core/mcp_servers/workspace_server.py (GitHub & Email MCP tools)
  3. core/mcp_client.py live discovery of the Workspace MCP server over stdio
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from actions.github_manager import (
    _create_or_update_file,
    _create_repo,
    _delete_repo,
    _rename_repo,
    _update_repo,
)
from core.mcp_client import MCPClientManager
from core.mcp_servers import workspace_server


def _make_mock_gh(repo_name: str = "old-repo", owner: str = "PetersonMambondimumwe"):
    mock_repo = MagicMock()
    mock_repo.name = repo_name
    mock_repo.full_name = f"{owner}/{repo_name}"
    mock_repo.default_branch = "main"
    mock_repo.private = False
    mock_repo.html_url = f"https://github.com/{owner}/{repo_name}"

    mock_user = MagicMock()
    mock_user.login = owner
    mock_user.get_repos.return_value = [mock_repo]
    mock_user.get_orgs.return_value = []

    mock_gh = MagicMock()
    mock_gh.get_user.return_value = mock_user
    mock_gh.get_repo.return_value = mock_repo
    return mock_gh, mock_user, mock_repo


class TestWorkspaceMCP(unittest.TestCase):
    def test_github_rename_repo(self):
        mock_gh, _, mock_repo = _make_mock_gh("Jarvis-Old")
        result = _rename_repo(mock_gh, {"repo": "Jarvis-Old", "new_name": "Jarvis-AI"})
        mock_repo.edit.assert_called_once_with(name="Jarvis-AI")
        self.assertIn("Jarvis-AI", result)
        self.assertIn("renamed successfully", result)

    def test_github_create_repo(self):
        mock_gh, mock_user, _ = _make_mock_gh()
        created = MagicMock()
        created.full_name = "PetersonMambondimumwe/new-project"
        created.private = True
        created.html_url = "https://github.com/PetersonMambondimumwe/new-project"
        mock_user.create_repo.return_value = created

        result = _create_repo(
            mock_gh,
            {"name": "new-project", "description": "Test repo", "private": True, "auto_init": True},
        )
        mock_user.create_repo.assert_called_once_with(
            name="new-project",
            description="Test repo",
            private=True,
            auto_init=True,
        )
        self.assertIn("PetersonMambondimumwe/new-project", result)
        self.assertIn("Private", result)

    def test_github_update_repo(self):
        mock_gh, _, mock_repo = _make_mock_gh("Jarvis")
        result = _update_repo(
            mock_gh,
            {
                "repo": "Jarvis",
                "description": "Updated description",
                "private": True,
                "topics": "ai, mcp, voice-assistant",
            },
        )
        mock_repo.edit.assert_called_once_with(description="Updated description", private=True)
        mock_repo.replace_topics.assert_called_once_with(["ai", "mcp", "voice-assistant"])
        self.assertIn("updated successfully", result)

    def test_github_delete_repo_requires_confirmation(self):
        mock_gh, _, mock_repo = _make_mock_gh("temp-repo")
        denied = _delete_repo(mock_gh, {"repo": "temp-repo", "confirm": False})
        mock_repo.delete.assert_not_called()
        self.assertIn("requires explicit confirmation", denied)

        confirmed = _delete_repo(mock_gh, {"repo": "temp-repo", "confirm": True})
        mock_repo.delete.assert_called_once()
        self.assertIn("permanently deleted", confirmed)

    def test_github_create_or_update_file(self):
        mock_gh, _, mock_repo = _make_mock_gh("Jarvis")
        existing_file = MagicMock()
        existing_file.sha = "abc1234567"
        mock_repo.get_contents.return_value = existing_file
        commit_obj = MagicMock()
        commit_obj.sha = "9876543210abcdef"
        mock_repo.update_file.return_value = {"commit": commit_obj}

        result = _create_or_update_file(
            mock_gh,
            {
                "repo": "Jarvis",
                "path": "README.md",
                "content": "# Updated README",
                "commit_message": "docs: update README",
            },
        )
        mock_repo.update_file.assert_called_once_with(
            path="README.md",
            message="docs: update README",
            content="# Updated README",
            sha="abc1234567",
            branch="main",
        )
        self.assertIn("Updated **README.md**", result)

    def test_workspace_email_send_via_smtp(self):
        with patch.object(
            workspace_server,
            "_resolve_email_credentials",
            return_value={
                "smtp_email": "jarvis@example.com",
                "smtp_password": "apppassword123",
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "imap_email": "jarvis@example.com",
                "imap_password": "apppassword123",
                "imap_host": "imap.gmail.com",
                "imap_port": 993,
            },
        ), patch("smtplib.SMTP") as mock_smtp_cls:
            smtp_instance = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = smtp_instance

            res = workspace_server.email_send(
                to="sir@example.com",
                subject="Status Report",
                body="All systems operational, sir.",
                recipient_name="Sir Peterson",
            )
            smtp_instance.starttls.assert_called_once()
            smtp_instance.login.assert_called_once_with("jarvis@example.com", "apppassword123")
            smtp_instance.send_message.assert_called_once()
            self.assertIn("Email sent to sir@example.com via SMTP", res)

    def test_workspace_email_read_latest_message(self):
        raw_email = (
            b"From: Dean <dean@up.ac.za>\r\n"
            b"To: Sir Peterson <u19094290@tuks.co.za>\r\n"
            b"Subject: Research Grant Approved\r\n"
            b"Date: Sat, 17 Oct 2026 10:00:00 +0200\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Congratulations Sir Peterson, your grant has been approved."
        )
        mock_imap = MagicMock()
        mock_imap.uid.side_effect = [
            ("OK", [b"101 102 105"]),
            ("OK", [(b"105 (BODY[] {200}", raw_email)]),
        ]
        with patch.object(workspace_server, "_open_imap", return_value=mock_imap):
            res = workspace_server.email_read_message(email_id="latest")
            self.assertIn("Research Grant Approved", res)
            self.assertIn("dean@up.ac.za", res)
            self.assertIn("Congratulations Sir Peterson, your grant has been approved.", res)

    def test_workspace_whatsapp_send_template(self):
        from core.whatsapp_client import WhatsAppConfig

        fake_cfg = WhatsAppConfig(
            phone_number_id="1234567890",
            token="EAAB_fake_token",
            template_name="jarvis_notification",
            template_lang="en",
        )
        # First call returns 132001 (en not found), second call (en_US) succeeds
        resp_fail = MagicMock(status_code=404, text='{"error":{"code":132001,"message":"Template language not found"}}')
        resp_fail.json.return_value = {"error": {"code": 132001, "message": "Template language not found"}}

        resp_ok = MagicMock(status_code=200, text='{"messages":[{"id":"wamid.123"}]}')
        resp_ok.json.return_value = {"messages": [{"id": "wamid.123"}]}

        with patch("core.whatsapp_client.load_whatsapp_config", return_value=fake_cfg), patch(
            "requests.Session.post", side_effect=[resp_fail, resp_ok]
        ) as mock_post:
            res = workspace_server.whatsapp_send(
                to="0712345678",
                message="Good day,\nYour meeting is at 10 AM.\tSee you there!",
                template_name="jarvis_notification",
            )
            self.assertIn("WhatsApp message sent via 'jarvis_notification'", res)
            self.assertIn("27712345678", res)
            self.assertEqual(mock_post.call_count, 2)
            sent_payload = mock_post.call_args_list[1].kwargs["json"]
            self.assertEqual(sent_payload["template"]["name"], "jarvis_notification")
            self.assertEqual(sent_payload["template"]["language"]["code"], "en_US")
            param_text = sent_payload["template"]["components"][0]["parameters"][0]["text"]
            self.assertNotIn("\n", param_text)
            self.assertNotIn("\t", param_text)
            self.assertEqual(param_text, "Your meeting is at 10 AM. See you there!")

    def test_mcp_client_discovers_workspace_server(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = Path(tmp_dir) / "mcp_servers.json"
            cfg_file.write_text(
                """{
  "mcpServers": {
    "workspace": {
      "command": "python",
      "args": ["core/mcp_servers/workspace_server.py"],
      "enabled": true
    }
  }
}""",
                encoding="utf-8",
            )

            async def _run():
                mgr = MCPClientManager(config_path=cfg_file)
                try:
                    decls = await mgr.initialize()
                    names = {d["name"] for d in decls}
                    self.assertIn("mcp__workspace__github_rename_repo", names)
                    self.assertIn("mcp__workspace__github_create_repo", names)
                    self.assertIn("mcp__workspace__github_write_file", names)
                    self.assertIn("mcp__workspace__email_send", names)
                    self.assertIn("mcp__workspace__email_list_inbox", names)
                    self.assertIn("mcp__workspace__email_search", names)
                    self.assertIn("mcp__workspace__email_read_message", names)
                    self.assertIn("mcp__workspace__email_reply", names)
                    self.assertIn("mcp__workspace__whatsapp_send", names)
                finally:
                    await mgr.shutdown()

            asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
