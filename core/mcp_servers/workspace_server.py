"""
core/mcp_servers/workspace_server.py
────────────────────────────────────
Jarvis Workspace MCP Server (GitHub & Email Suite).

Exposes direct Model Context Protocol (MCP) tools so Jarvis can execute
GitHub repository management (renaming repos, creating/updating/deleting repos,
reading/writing repo files, managing branches/issues/PRs) and full Email
workflows (sending branded emails via SMTP or Power Automate, listing inbox,
searching emails, reading full messages, and replying to threads via IMAP/SMTP).
"""

from __future__ import annotations

import asyncio
import email
import email.header
import email.utils
import html
import imaplib
import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

# Ensure project root is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from mcp.server.fastmcp import FastMCP as _MCPServerClass
except ImportError:
    from mcp.server.mcpserver import MCPServer as _MCPServerClass  # type: ignore

from actions.github_manager import github_manager
from actions.send_message import (
    _EMAIL_HTML_TEMPLATE,
    _get_config as _get_mail_config,
    _send_email_via_power_automate,
)

server = _MCPServerClass("JarvisWorkspaceServer")


# ── Email / IMAP / SMTP Helpers ───────────────────────────────────────────

def _resolve_email_credentials() -> dict[str, Any]:
    """Load SMTP and IMAP credentials from environment, .env, or config/api_keys.json."""
    cfg = _get_mail_config()

    smtp_email = (
        os.environ.get("SMTP_EMAIL")
        or os.environ.get("SMTP_USER")
        or os.environ.get("EMAIL_ADDRESS")
        or cfg.get("SMTP_EMAIL")
        or cfg.get("SMTP_USER")
        or cfg.get("smtp_email")
        or cfg.get("smtp_user")
        or ""
    ).strip()

    smtp_password = (
        os.environ.get("SMTP_PASSWORD")
        or os.environ.get("SMTP_PASS")
        or os.environ.get("EMAIL_PASSWORD")
        or cfg.get("SMTP_PASSWORD")
        or cfg.get("SMTP_PASS")
        or cfg.get("smtp_password")
        or cfg.get("smtp_pass")
        or ""
    ).strip()

    smtp_host = (
        os.environ.get("SMTP_HOST")
        or cfg.get("SMTP_HOST")
        or cfg.get("smtp_host")
        or ""
    ).strip()
    if not smtp_host:
        if any(d in smtp_email.lower() for d in ("outlook.", "hotmail.", "live.", "office365.", ".ac.za")):
            smtp_host = "smtp.office365.com"
        else:
            smtp_host = "smtp.gmail.com"

    try:
        smtp_port = int(
            os.environ.get("SMTP_PORT")
            or cfg.get("SMTP_PORT")
            or cfg.get("smtp_port")
            or 587
        )
    except (ValueError, TypeError):
        smtp_port = 587

    imap_email = (
        os.environ.get("IMAP_EMAIL")
        or cfg.get("IMAP_EMAIL")
        or cfg.get("imap_email")
        or smtp_email
    ).strip()

    imap_password = (
        os.environ.get("IMAP_PASSWORD")
        or cfg.get("IMAP_PASSWORD")
        or cfg.get("imap_password")
        or smtp_password
    ).strip()

    imap_host = (
        os.environ.get("IMAP_HOST")
        or cfg.get("IMAP_HOST")
        or cfg.get("imap_host")
        or ""
    ).strip()
    if not imap_host:
        if any(d in imap_email.lower() for d in ("outlook.", "hotmail.", "live.", "office365.", ".ac.za")):
            imap_host = "outlook.office365.com"
        else:
            imap_host = "imap.gmail.com"

    try:
        imap_port = int(
            os.environ.get("IMAP_PORT")
            or cfg.get("IMAP_PORT")
            or cfg.get("imap_port")
            or 993
        )
    except (ValueError, TypeError):
        imap_port = 993

    return {
        "smtp_email": smtp_email,
        "smtp_password": smtp_password.replace(" ", ""),
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "imap_email": imap_email,
        "imap_password": imap_password.replace(" ", ""),
        "imap_host": imap_host,
        "imap_port": imap_port,
    }


def _decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded_fragments: list[str] = []
    for fragment, charset in parts:
        if isinstance(fragment, bytes):
            decoded_fragments.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            decoded_fragments.append(str(fragment))
    return "".join(decoded_fragments).strip()


def _strip_html(raw_html: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", raw_html)
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</p\s*>", "\n\n", cleaned)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _extract_email_body(msg: email.message.Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ctype == "text/plain":
                plain_parts.append(text)
            elif ctype == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    if plain_parts:
        return "\n".join(plain_parts).strip()
    if html_parts:
        return _strip_html("\n".join(html_parts))
    return "(No readable text content)"


def _open_imap(folder: str = "INBOX") -> imaplib.IMAP4_SSL:
    creds = _resolve_email_credentials()
    if not creds["imap_email"] or not creds["imap_password"]:
        raise RuntimeError(
            "IMAP credentials not configured. Set SMTP_EMAIL and SMTP_PASSWORD "
            "(or IMAP_EMAIL / IMAP_PASSWORD) in .env or config/api_keys.json."
        )
    conn = imaplib.IMAP4_SSL(creds["imap_host"], creds["imap_port"], timeout=20)
    conn.login(creds["imap_email"], creds["imap_password"])
    status, _ = conn.select(folder or "INBOX", readonly=True)
    if status != "OK":
        conn.logout()
        raise RuntimeError(f"Could not select IMAP folder '{folder}'.")
    return conn


# ── GitHub MCP Tools ──────────────────────────────────────────────────────

@server.tool()
def github_list_repos(scope: str = "owner") -> str:
    """List the user's GitHub repositories.

    Parameters:
      - scope: 'owner' (default, repos owned by user), 'all' (owned + collaborator), or 'starred'
    """
    return github_manager({"action": "list_repos", "scope": scope})


@server.tool()
def github_repo_info(repo: str) -> str:
    """Get detailed metadata, stars, forks, visibility, default branch, and URL for a GitHub repository.

    Parameters:
      - repo: Repository name (e.g. 'Jarvis') or 'owner/repo'
    """
    return github_manager({"action": "repo_info", "repo": repo})


@server.tool()
def github_rename_repo(repo: str, new_name: str) -> str:
    """Rename an existing GitHub repository to a new name immediately.

    Parameters:
      - repo: Current repository name (e.g. 'old-repo-name' or 'owner/old-repo-name')
      - new_name: The new name to assign to the repository
    """
    return github_manager({"action": "rename_repo", "repo": repo, "new_name": new_name})


@server.tool()
def github_create_repo(
    name: str,
    description: str = "",
    private: bool = False,
    auto_init: bool = True,
) -> str:
    """Create a new GitHub repository under the authenticated user's account.

    Parameters:
      - name: Name for the new repository
      - description: Optional repository description
      - private: True for a private repo, False for public (default: False)
      - auto_init: Initialize with a README commit (default: True)
    """
    return github_manager({
        "action": "create_repo",
        "name": name,
        "description": description,
        "private": private,
        "auto_init": auto_init,
    })


@server.tool()
def github_update_repo(
    repo: str,
    description: Optional[str] = None,
    private: Optional[bool] = None,
    homepage: Optional[str] = None,
    default_branch: Optional[str] = None,
    topics: Optional[str] = None,
) -> str:
    """Update a GitHub repository's description, visibility (public/private), homepage, default branch, or topics.

    Parameters:
      - repo: Repository name or 'owner/repo'
      - description: New repository description (optional)
      - private: True to make private, False to make public (optional)
      - homepage: Homepage URL (optional)
      - default_branch: Default branch name (optional)
      - topics: Comma-separated repository topics (optional)
    """
    params: dict[str, Any] = {"action": "update_repo", "repo": repo}
    if description is not None:
        params["description"] = description
    if private is not None:
        params["private"] = private
    if homepage is not None:
        params["homepage"] = homepage
    if default_branch is not None:
        params["default_branch"] = default_branch
    if topics is not None:
        params["topics"] = topics
    return github_manager(params)


@server.tool()
def github_delete_repo(repo: str, confirm: bool = False) -> str:
    """Delete a GitHub repository permanently. Requires confirm=True after explicit user confirmation.

    Parameters:
      - repo: Repository name or 'owner/repo'
      - confirm: Must be True to execute deletion
    """
    return github_manager({"action": "delete_repo", "repo": repo, "confirm": confirm})


@server.tool()
def github_read_file(repo: str, path: str = "README.md", ref: str = "") -> str:
    """Read a file or list directory contents inside a GitHub repository.

    Parameters:
      - repo: Repository name or 'owner/repo'
      - path: File or directory path inside the repository (default: 'README.md')
      - ref: Branch, tag, or commit SHA (optional, defaults to repo default branch)
    """
    params: dict[str, Any] = {"action": "read_file", "repo": repo, "path": path}
    if ref.strip():
        params["ref"] = ref.strip()
    return github_manager(params)


@server.tool()
def github_write_file(
    repo: str,
    path: str,
    content: str,
    commit_message: str = "Update file via Jarvis MCP",
    branch: str = "",
) -> str:
    """Create a new file or update an existing file directly in a GitHub repository.

    Parameters:
      - repo: Repository name or 'owner/repo'
      - path: Path inside the repository (e.g. 'README.md' or 'src/index.py')
      - content: Full text content to write to the file
      - commit_message: Git commit message
      - branch: Target branch (optional, defaults to repo default branch)
    """
    params: dict[str, Any] = {
        "action": "create_or_update_file",
        "repo": repo,
        "path": path,
        "content": content,
        "commit_message": commit_message,
    }
    if branch.strip():
        params["branch"] = branch.strip()
    return github_manager(params)


@server.tool()
def github_create_issue(repo: str, title: str, body: str = "", labels: str = "") -> str:
    """Create a new issue on a GitHub repository.

    Parameters:
      - repo: Repository name or 'owner/repo'
      - title: Issue title
      - body: Issue description/body
      - labels: Optional comma-separated label names
    """
    label_list = [l.strip() for l in labels.split(",") if l.strip()] if labels else []
    return github_manager({
        "action": "create_issue",
        "repo": repo,
        "title": title,
        "body": body,
        "labels": label_list,
    })


@server.tool()
def github_create_branch(repo: str, branch: str, from_branch: str = "") -> str:
    """Create a new branch on a GitHub repository.

    Parameters:
      - repo: Repository name or 'owner/repo'
      - branch: Name of the new branch to create
      - from_branch: Optional source branch (defaults to repository default branch)
    """
    params: dict[str, Any] = {"action": "create_branch", "repo": repo, "branch": branch}
    if from_branch.strip():
        params["from_branch"] = from_branch.strip()
    return github_manager(params)


@server.tool()
def github_create_pr(
    repo: str,
    title: str,
    head: str,
    base: str = "",
    body: str = "",
) -> str:
    """Open a pull request on a GitHub repository.

    Parameters:
      - repo: Repository name or 'owner/repo'
      - title: Pull request title
      - head: Source branch containing changes
      - base: Target branch to merge into (defaults to repository default branch)
      - body: Pull request description
    """
    params: dict[str, Any] = {
        "action": "create_pr",
        "repo": repo,
        "title": title,
        "head": head,
        "body": body,
    }
    if base.strip():
        params["base"] = base.strip()
    return github_manager(params)


# ── Email MCP Tools (SMTP + IMAP + Power Automate Fallback) ───────────────

@server.tool()
def email_send(
    to: str,
    subject: str,
    body: str,
    recipient_name: str = "",
    cc: str = "",
    bcc: str = "",
    sender_name: str = "Jarvis",
) -> str:
    """Write and send an email immediately via SMTP (with Power Automate fallback).

    Parameters:
      - to: Recipient email address (or comma-separated addresses)
      - subject: Email subject line
      - body: Email body text
      - recipient_name: Optional recipient name for greeting (e.g. 'Dr. Smith')
      - cc: Optional comma-separated CC email addresses
      - bcc: Optional comma-separated BCC email addresses
      - sender_name: Display name for sender (default: 'Jarvis')
    """
    to_clean = to.strip()
    if not to_clean:
        return "[Email] Error: Please provide a recipient email address ('to')."
    if not body.strip():
        return "[Email] Error: Please provide the email 'body' content."

    creds = _resolve_email_credentials()
    subj = subject.strip() or f"Message from {sender_name}"

    if creds["smtp_email"] and creds["smtp_password"]:
        html_body = _EMAIL_HTML_TEMPLATE.format(
            name=recipient_name.strip() or "there",
            message=body.replace("\n", "<br>"),
            sender_name=sender_name,
        )
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subj
        msg["From"] = f"{sender_name} <{creds['smtp_email']}>"
        msg["To"] = to_clean
        if cc.strip():
            msg["Cc"] = cc.strip()

        all_recipients = [addr.strip() for addr in to_clean.split(",") if addr.strip()]
        if cc.strip():
            all_recipients.extend(addr.strip() for addr in cc.split(",") if addr.strip())
        if bcc.strip():
            all_recipients.extend(addr.strip() for addr in bcc.split(",") if addr.strip())

        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            with smtplib.SMTP(creds["smtp_host"], creds["smtp_port"], timeout=20) as smtp:
                smtp.starttls()
                smtp.login(creds["smtp_email"], creds["smtp_password"])
                smtp.send_message(msg, to_addrs=all_recipients)
            return f"✅ Email sent to {to_clean} via SMTP. Subject: \"{subj}\"."
        except Exception as exc:
            return f"[Email] SMTP delivery failed: {exc}"

    # Fallback to Power Automate flow if SMTP credentials are not configured
    return _send_email_via_power_automate(
        to=to_clean,
        name=recipient_name.strip(),
        subject=subj,
        message=body,
        sender_name=sender_name,
    )


@server.tool()
def email_list_inbox(
    limit: int = 10,
    unread_only: bool = False,
    folder: str = "INBOX",
) -> str:
    """List recent emails in the user's inbox via IMAP.

    Parameters:
      - limit: Maximum number of emails to list (default: 10, max: 30)
      - unread_only: If True, only list unread (UNSEEN) emails
      - folder: Mailbox folder to inspect (default: 'INBOX')
    """
    limit = max(1, min(int(limit), 30))
    try:
        conn = _open_imap(folder=folder)
        try:
            criterion = "UNSEEN" if unread_only else "ALL"
            status, data = conn.uid("search", None, criterion)
            if status != "OK" or not data or not data[0]:
                return f"No {'unread ' if unread_only else ''}emails found in {folder}."

            uids = data[0].split()
            recent_uids = list(reversed(uids[-limit:]))
            lines = [f"=== {folder} ({len(uids)} matching, showing latest {len(recent_uids)}) ===\n"]

            for uid_bytes in recent_uids:
                uid_str = uid_bytes.decode("ascii", errors="replace")
                st, msg_data = conn.uid("fetch", uid_bytes, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if st != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                hdr_msg = email.message_from_bytes(msg_data[0][1])
                subj = _decode_mime_header(hdr_msg.get("Subject")) or "(No Subject)"
                sender = _decode_mime_header(hdr_msg.get("From")) or "Unknown"
                date_str = _decode_mime_header(hdr_msg.get("Date")) or ""
                lines.append(
                    f"• ID `{uid_str}` — **{subj}**\n"
                    f"  From: {sender} | Date: {date_str}"
                )
            return "\n".join(lines)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as exc:
        return f"[Email] Could not list inbox: {exc}"


@server.tool()
def email_search(
    query: str,
    limit: int = 10,
    folder: str = "INBOX",
) -> str:
    """Search emails in the user's mailbox by keyword, sender, or subject via IMAP.

    Parameters:
      - query: Search term (sender email/name, subject keyword, or body text)
      - limit: Maximum results to return (default: 10, max: 25)
      - folder: Mailbox folder to search (default: 'INBOX')
    """
    q = (query or "").strip()
    if not q:
        return "Please provide a search 'query'."
    limit = max(1, min(int(limit), 25))
    try:
        conn = _open_imap(folder=folder)
        try:
            safe_q = q.replace('"', "")
            status, data = conn.uid("search", None, f'(OR SUBJECT "{safe_q}" (OR FROM "{safe_q}" TEXT "{safe_q}"))')
            if status != "OK" or not data or not data[0]:
                return f"No emails found matching '{q}' in {folder}."

            uids = data[0].split()
            recent_uids = list(reversed(uids[-limit:]))
            lines = [f"Found {len(uids)} email(s) matching '{q}' (showing latest {len(recent_uids)}):\n"]

            for uid_bytes in recent_uids:
                uid_str = uid_bytes.decode("ascii", errors="replace")
                st, msg_data = conn.uid("fetch", uid_bytes, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if st != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                hdr_msg = email.message_from_bytes(msg_data[0][1])
                subj = _decode_mime_header(hdr_msg.get("Subject")) or "(No Subject)"
                sender = _decode_mime_header(hdr_msg.get("From")) or "Unknown"
                date_str = _decode_mime_header(hdr_msg.get("Date")) or ""
                lines.append(
                    f"• ID `{uid_str}` — **{subj}**\n"
                    f"  From: {sender} | Date: {date_str}"
                )
            return "\n".join(lines)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as exc:
        return f"[Email] Search failed: {exc}"


@server.tool()
def email_read_message(email_id: str, folder: str = "INBOX") -> str:
    """Read the full content and headers of a specific email by its ID (UID).

    Parameters:
      - email_id: The email UID returned by email_list_inbox or email_search
      - folder: Mailbox folder (default: 'INBOX')
    """
    uid = (email_id or "").strip()
    if not uid:
        return "Please provide 'email_id' to read."
    try:
        conn = _open_imap(folder=folder)
        try:
            status, msg_data = conn.uid("fetch", uid.encode("ascii"), "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                return f"Email ID '{uid}' not found in {folder}."

            msg = email.message_from_bytes(msg_data[0][1])
            subj = _decode_mime_header(msg.get("Subject")) or "(No Subject)"
            sender = _decode_mime_header(msg.get("From")) or "Unknown"
            to_hdr = _decode_mime_header(msg.get("To")) or ""
            cc_hdr = _decode_mime_header(msg.get("Cc")) or ""
            date_str = _decode_mime_header(msg.get("Date")) or ""
            body = _extract_email_body(msg)
            if len(body) > 4000:
                body = body[:4000] + "\n\n… [truncated]"

            header_lines = [
                f"**Subject**: {subj}",
                f"**From**: {sender}",
                f"**To**: {to_hdr}",
            ]
            if cc_hdr:
                header_lines.append(f"**Cc**: {cc_hdr}")
            header_lines.append(f"**Date**: {date_str}")
            header_lines.append(f"**Email ID**: `{uid}`\n")
            return "\n".join(header_lines) + "\n" + body
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as exc:
        return f"[Email] Error reading email '{uid}': {exc}"


@server.tool()
def email_reply(
    email_id: str,
    reply_body: str,
    reply_all: bool = False,
    folder: str = "INBOX",
    sender_name: str = "Jarvis",
) -> str:
    """Reply to an existing email thread by its ID (UID), preserving subject and Message-ID threading.

    Parameters:
      - email_id: The email UID to reply to
      - reply_body: Text content of the reply
      - reply_all: If True, also CC original recipients
      - folder: Mailbox folder containing the original email (default: 'INBOX')
      - sender_name: Display name for sender (default: 'Jarvis')
    """
    uid = (email_id or "").strip()
    if not uid or not reply_body.strip():
        return "Please provide both 'email_id' and 'reply_body'."

    try:
        conn = _open_imap(folder=folder)
        try:
            status, msg_data = conn.uid(
                "fetch",
                uid.encode("ascii"),
                "(BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO TO CC SUBJECT MESSAGE-ID REFERENCES)])",
            )
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                return f"Original email ID '{uid}' not found in {folder}."
            orig = email.message_from_bytes(msg_data[0][1])
        finally:
            try:
                conn.logout()
            except Exception:
                pass

        reply_to_raw = _decode_mime_header(orig.get("Reply-To") or orig.get("From"))
        disp_name, reply_addr = email.utils.parseaddr(reply_to_raw)
        if not reply_addr:
            return f"Could not determine reply address from '{reply_to_raw}'."

        orig_subj = _decode_mime_header(orig.get("Subject")) or "(No Subject)"
        subj = orig_subj if orig_subj.lower().startswith("re:") else f"Re: {orig_subj}"
        msg_id = (orig.get("Message-ID") or "").strip()
        refs = (orig.get("References") or "").strip()

        creds = _resolve_email_credentials()
        if not creds["smtp_email"] or not creds["smtp_password"]:
            return _send_email_via_power_automate(
                to=reply_addr,
                name=disp_name,
                subject=subj,
                message=reply_body,
                sender_name=sender_name,
            )

        html_body = _EMAIL_HTML_TEMPLATE.format(
            name=disp_name or "there",
            message=reply_body.replace("\n", "<br>"),
            sender_name=sender_name,
        )
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subj
        msg["From"] = f"{sender_name} <{creds['smtp_email']}>"
        msg["To"] = reply_addr
        if msg_id:
            msg["In-Reply-To"] = msg_id
            msg["References"] = f"{refs} {msg_id}".strip()

        all_recipients = [reply_addr]
        if reply_all:
            orig_cc = _decode_mime_header(orig.get("Cc"))
            if orig_cc:
                msg["Cc"] = orig_cc
                for _, addr in email.utils.getaddresses([orig_cc]):
                    if addr and addr.lower() != creds["smtp_email"].lower():
                        all_recipients.append(addr)

        msg.attach(MIMEText(reply_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(creds["smtp_host"], creds["smtp_port"], timeout=20) as smtp:
            smtp.starttls()
            smtp.login(creds["smtp_email"], creds["smtp_password"])
            smtp.send_message(msg, to_addrs=all_recipients)

        return f"✅ Reply sent to {reply_addr} — Subject: \"{subj}\"."
    except Exception as exc:
        return f"[Email] Failed to send reply: {exc}"


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
