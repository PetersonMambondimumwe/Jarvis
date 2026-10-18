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


def _open_imap(folder: str = "INBOX", readonly: bool = True) -> imaplib.IMAP4_SSL:
    creds = _resolve_email_credentials()
    if not creds["imap_email"] or not creds["imap_password"]:
        raise RuntimeError(
            "IMAP credentials not configured. Set SMTP_EMAIL and SMTP_PASSWORD "
            "(or IMAP_EMAIL / IMAP_PASSWORD) in .env or config/api_keys.json."
        )
    conn = imaplib.IMAP4_SSL(creds["imap_host"], creds["imap_port"], timeout=20)
    conn.login(creds["imap_email"], creds["imap_password"])
    if folder:
        target_folder = f'"{folder}"' if " " in folder and not folder.startswith('"') else folder
        status, _ = conn.select(target_folder, readonly=readonly)
        if status != "OK":
            conn.logout()
            raise RuntimeError(f"Could not select IMAP folder '{folder}'.")
    return conn


def _find_imap_special_folder(
    conn: imaplib.IMAP4_SSL,
    special_use_flag: str,
    candidates: list[str],
) -> Optional[str]:
    """Locate an IMAP special-use folder (e.g. '\\Drafts' or '\\Trash') via LIST or fallback names."""
    try:
        st, folders = conn.list()
        if st == "OK" and folders:
            parsed_names: list[str] = []
            for raw_item in folders:
                if not isinstance(raw_item, bytes):
                    continue
                line = raw_item.decode("utf-8", errors="replace")
                # Match RFC 6154 special-use attribute e.g. \Drafts or \Trash
                m = re.search(r'\(([^)]*)\)\s+"[^"]*"\s+("?[^"]+"?)$', line)
                if not m:
                    continue
                flags_str, folder_raw = m.group(1), m.group(2).strip().strip('"')
                parsed_names.append(folder_raw)
                if special_use_flag.lower() in flags_str.lower():
                    return f'"{folder_raw}"' if " " in folder_raw else folder_raw

            lower_map = {n.lower(): n for n in parsed_names}
            for cand in candidates:
                if cand.lower() in lower_map:
                    matched = lower_map[cand.lower()]
                    return f'"{matched}"' if " " in matched else matched
    except Exception:
        pass
    return None


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
    """List recent emails in the user's inbox via IMAP (including a body preview for the newest emails).

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

            for idx, uid_bytes in enumerate(recent_uids):
                uid_str = uid_bytes.decode("ascii", errors="replace")
                # Fetch full message for the top 3 newest emails so Jarvis has a snippet ready immediately
                fetch_spec = "(BODY.PEEK[])" if idx < 3 else "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
                st, msg_data = conn.uid("fetch", uid_bytes, fetch_spec)
                if st != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg_obj = email.message_from_bytes(msg_data[0][1])
                subj = _decode_mime_header(msg_obj.get("Subject")) or "(No Subject)"
                sender = _decode_mime_header(msg_obj.get("From")) or "Unknown"
                date_str = _decode_mime_header(msg_obj.get("Date")) or ""
                entry = (
                    f"• ID `{uid_str}` — **{subj}**\n"
                    f"  From: {sender} | Date: {date_str}"
                )
                if idx < 3:
                    snippet = re.sub(r"\s+", " ", _extract_email_body(msg_obj)).strip()
                    if len(snippet) > 260:
                        snippet = snippet[:260] + "…"
                    if snippet:
                        entry += f"\n  Preview: {snippet}"
                lines.append(entry)
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


def _resolve_uid_bytes(conn: imaplib.IMAP4_SSL, email_id: str, unread_only: bool = False) -> Optional[bytes]:
    """Resolve an email_id string (or 'latest'/'last'/'newest') to an IMAP UID bytes token."""
    cleaned = (email_id or "").strip().lower()
    if not cleaned or cleaned in ("latest", "last", "newest", "recent"):
        criterion = "UNSEEN" if unread_only else "ALL"
        status, data = conn.uid("search", None, criterion)
        if status != "OK" or not data or not data[0]:
            return None
        uids = data[0].split()
        return uids[-1] if uids else None
    return email_id.strip().encode("ascii", errors="ignore")


@server.tool()
def email_read_message(
    email_id: str = "latest",
    unread_only: bool = False,
    folder: str = "INBOX",
) -> str:
    """Read the full content and headers of the latest email (or a specific email by its ID/UID).

    Parameters:
      - email_id: Pass 'latest' (default) to immediately read the most recent email in the inbox, or pass a specific UID from email_list_inbox / email_search
      - unread_only: When email_id='latest', set True to read the latest unread email
      - folder: Mailbox folder (default: 'INBOX')
    """
    try:
        conn = _open_imap(folder=folder)
        try:
            uid_bytes = _resolve_uid_bytes(conn, email_id, unread_only=unread_only)
            if not uid_bytes:
                return f"No {'unread ' if unread_only else ''}emails found in {folder}."

            uid = uid_bytes.decode("ascii", errors="replace")
            status, msg_data = conn.uid("fetch", uid_bytes, "(BODY.PEEK[])")
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
        return f"[Email] Error reading email '{email_id}': {exc}"


@server.tool()
def email_reply(
    reply_body: str,
    email_id: str = "latest",
    reply_all: bool = False,
    folder: str = "INBOX",
    sender_name: str = "Jarvis",
) -> str:
    """Reply to the latest email (or a specific email by its ID/UID), preserving subject and Message-ID threading.

    Parameters:
      - reply_body: Text content of the reply
      - email_id: 'latest' (default, replies to the most recent email) or a specific email UID
      - reply_all: If True, also CC original recipients
      - folder: Mailbox folder containing the original email (default: 'INBOX')
      - sender_name: Display name for sender (default: 'Jarvis')
    """
    if not reply_body.strip():
        return "Please provide 'reply_body'."

    try:
        conn = _open_imap(folder=folder)
        try:
            uid_bytes = _resolve_uid_bytes(conn, email_id, unread_only=False)
            if not uid_bytes:
                return f"No emails found in {folder} to reply to."
            uid = uid_bytes.decode("ascii", errors="replace")
            status, msg_data = conn.uid(
                "fetch",
                uid_bytes,
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


@server.tool()
def email_save_draft(
    to: str = "",
    subject: str = "",
    body: str = "",
    recipient_name: str = "",
    cc: str = "",
    bcc: str = "",
    sender_name: str = "Jarvis",
) -> str:
    """Save an email as a draft in the user's mailbox Drafts folder via IMAP (and persist a local backup).

    Parameters:
      - to: Optional recipient email address (or comma-separated addresses)
      - subject: Email subject line
      - body: Email body text
      - recipient_name: Optional recipient name for greeting (e.g. 'Dr. Smith')
      - cc: Optional comma-separated CC email addresses
      - bcc: Optional comma-separated BCC email addresses
      - sender_name: Display name for sender (default: 'Jarvis')
    """
    import time as _time
    import uuid as _uuid

    if not body.strip() and not subject.strip():
        return "[Email] Error: Please provide at least a 'subject' or 'body' to save as a draft."

    creds = _resolve_email_credentials()
    subj = subject.strip() or "(No Subject)"
    to_clean = to.strip()
    draft_id = _uuid.uuid4().hex[:8]

    # Always save to local drafts registry as backup / quick reference
    local_drafts_file = BASE_DIR / "memory" / "email_drafts.json"
    try:
        local_drafts_file.parent.mkdir(parents=True, exist_ok=True)
        existing_drafts: list[dict[str, Any]] = []
        if local_drafts_file.exists():
            raw = json.loads(local_drafts_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                existing_drafts = raw
        existing_drafts.insert(
            0,
            {
                "draft_id": draft_id,
                "to": to_clean,
                "cc": cc.strip(),
                "bcc": bcc.strip(),
                "subject": subj,
                "body": body,
                "created_at": int(_time.time()),
            },
        )
        local_drafts_file.write_text(json.dumps(existing_drafts[:50], indent=2), encoding="utf-8")
    except Exception:
        pass

    if not creds["imap_email"] or not creds["imap_password"]:
        return (
            f"✅ Email draft `{draft_id}` saved locally (To: {to_clean or 'unassigned'}, "
            f"Subject: \"{subj}\"). Configure IMAP credentials to sync directly with your mailbox Drafts folder."
        )

    try:
        html_body = _EMAIL_HTML_TEMPLATE.format(
            name=recipient_name.strip() or "there",
            message=body.replace("\n", "<br>"),
            sender_name=sender_name,
        )
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subj
        msg["From"] = f"{sender_name} <{creds['imap_email']}>"
        if to_clean:
            msg["To"] = to_clean
        if cc.strip():
            msg["Cc"] = cc.strip()
        if bcc.strip():
            msg["Bcc"] = bcc.strip()
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid()

        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        raw_bytes = msg.as_bytes()

        conn = _open_imap(folder="", readonly=False)
        try:
            draft_folder = _find_imap_special_folder(
                conn,
                "\\Drafts",
                ["[Gmail]/Drafts", "Drafts", "INBOX.Drafts", "Draft"],
            )
            candidates = (
                [draft_folder] if draft_folder else []
            ) + ['"[Gmail]/Drafts"', "Drafts", "INBOX.Drafts"]
            saved_folder = None
            for folder_name in candidates:
                if not folder_name:
                    continue
                try:
                    st, _ = conn.append(
                        folder_name,
                        "(\\Draft \\Seen)",
                        imaplib.Time2Internaldate(_time.time()),
                        raw_bytes,
                    )
                    if st == "OK":
                        saved_folder = folder_name.strip('"')
                        break
                except Exception:
                    continue

            if saved_folder:
                return (
                    f"✅ Email saved to your '{saved_folder}' mailbox folder "
                    f"(To: {to_clean or 'unassigned'}, Subject: \"{subj}\", Draft ID: `{draft_id}`)."
                )
            return (
                f"✅ Email draft `{draft_id}` saved locally (Subject: \"{subj}\"), "
                "but the remote IMAP Drafts folder could not be written to."
            )
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as exc:
        return f"✅ Email draft `{draft_id}` saved locally (IMAP sync error: {exc})."


@server.tool()
def email_list_drafts(limit: int = 10) -> str:
    """List saved email drafts from the user's IMAP Drafts folder and local draft storage.

    Parameters:
      - limit: Maximum number of drafts to list (default: 10, max: 25)
    """
    limit = max(1, min(int(limit or 10), 25))
    lines: list[str] = []

    try:
        conn = _open_imap(folder="", readonly=True)
        try:
            draft_folder = _find_imap_special_folder(
                conn,
                "\\Drafts",
                ["[Gmail]/Drafts", "Drafts", "INBOX.Drafts", "Draft"],
            ) or "Drafts"
            st, _ = conn.select(draft_folder, readonly=True)
            if st == "OK":
                st_s, data = conn.uid("search", None, "ALL")
                if st_s == "OK" and data and data[0]:
                    uids = list(reversed(data[0].split()[-limit:]))
                    clean_draft_folder = draft_folder.strip('"')
                    lines.append(f"=== Mailbox Drafts ({clean_draft_folder}: {len(uids)}) ===")
                    for uid_b in uids:
                        uid_s = uid_b.decode("ascii", errors="replace")
                        st_f, msg_data = conn.uid("fetch", uid_b, "(BODY.PEEK[])")
                        if st_f != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                            continue
                        msg_obj = email.message_from_bytes(msg_data[0][1])
                        subj = _decode_mime_header(msg_obj.get("Subject")) or "(No Subject)"
                        to_hdr = _decode_mime_header(msg_obj.get("To")) or "(No recipient)"
                        date_str = _decode_mime_header(msg_obj.get("Date")) or ""
                        snippet = re.sub(r"\s+", " ", _extract_email_body(msg_obj)).strip()[:200]
                        lines.append(
                            f"• UID `{uid_s}` — **{subj}**\n"
                            f"  To: {to_hdr} | Date: {date_str}\n"
                            f"  Preview: {snippet}"
                        )
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception:
        pass

    local_drafts_file = BASE_DIR / "memory" / "email_drafts.json"
    if local_drafts_file.exists():
        try:
            raw = json.loads(local_drafts_file.read_text(encoding="utf-8"))
            if isinstance(raw, list) and raw:
                lines.append("\n=== Local Jarvis Drafts ===")
                for item in raw[:limit]:
                    if not isinstance(item, dict):
                        continue
                    lines.append(
                        f"• Draft `{item.get('draft_id', '')}` — **{item.get('subject', '(No Subject)')}**\n"
                        f"  To: {item.get('to') or '(No recipient)'}\n"
                        f"  Body: {str(item.get('body') or '')[:200]}"
                    )
        except Exception:
            pass

    if not lines:
        return "No saved email drafts found."
    return "\n".join(lines)


@server.tool()
def email_delete(
    email_id: str = "latest",
    query: str = "",
    folder: str = "INBOX",
    permanent: bool = False,
) -> str:
    """Delete an email from the user's mailbox by UID, 'latest', or search query (moves to Trash by default).

    Parameters:
      - email_id: UID of the email to delete (from email_list_inbox / email_search), or 'latest' for the newest email
      - query: Optional sender/subject search query to locate the email to delete if email_id is not a numeric UID
      - folder: Mailbox folder containing the email (default: 'INBOX')
      - permanent: If True, permanently expunge without copying to Trash; if False (default), move to Trash/Bin first
    """
    try:
        conn = _open_imap(folder=folder, readonly=False)
        try:
            uid_bytes: Optional[bytes] = None
            clean_q = (query or "").strip()
            clean_id = (email_id or "").strip()

            # If a search query was passed (or email_id looks like a search phrase rather than a UID / 'latest')
            if clean_q or (clean_id and not clean_id.isdigit() and clean_id.lower() not in ("latest", "last", "newest", "recent")):
                search_term = (clean_q or clean_id).replace('"', "")
                st, data = conn.uid(
                    "search",
                    None,
                    f'(OR SUBJECT "{search_term}" (OR FROM "{search_term}" TEXT "{search_term}"))',
                )
                if st == "OK" and data and data[0]:
                    matches = data[0].split()
                    uid_bytes = matches[-1] if matches else None
                if not uid_bytes:
                    return f"[Email] No email found matching '{search_term}' in {folder}."
            else:
                uid_bytes = _resolve_uid_bytes(conn, clean_id or "latest", unread_only=False)

            if not uid_bytes:
                return f"[Email] No email found to delete in {folder}."

            uid_str = uid_bytes.decode("ascii", errors="replace")

            # Fetch Subject and From before deleting so Jarvis can confirm the exact message deleted
            subj = "(No Subject)"
            sender = "Unknown"
            st_f, msg_data = conn.uid("fetch", uid_bytes, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if st_f == "OK" and msg_data and isinstance(msg_data[0], tuple):
                hdr_msg = email.message_from_bytes(msg_data[0][1])
                subj = _decode_mime_header(hdr_msg.get("Subject")) or "(No Subject)"
                sender = _decode_mime_header(hdr_msg.get("From")) or "Unknown"

            moved_to_trash = False
            if not permanent:
                trash_folder = _find_imap_special_folder(
                    conn,
                    "\\Trash",
                    ["[Gmail]/Trash", "[Gmail]/Bin", "Trash", "Deleted Items", "Deleted Messages", "Bin"],
                )
                if trash_folder and trash_folder.strip('"').lower() != folder.strip('"').lower():
                    try:
                        cp_st, _ = conn.uid("copy", uid_bytes, trash_folder)
                        if cp_st == "OK":
                            moved_to_trash = True
                    except Exception:
                        pass

            st_del, _ = conn.uid("store", uid_bytes, "+FLAGS", "(\\Deleted)")
            if st_del != "OK":
                return f"[Email] Server rejected deletion for email ID `{uid_str}` (Subject: \"{subj}\")."
            conn.expunge()

            action_desc = "moved to Trash and removed" if moved_to_trash else "permanently deleted"
            return (
                f"✅ Email ID `{uid_str}` {action_desc} from {folder} — "
                f"**{subj}** (From: {sender})."
            )
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as exc:
        return f"[Email] Failed to delete email: {exc}"


# ── WhatsApp Cloud API MCP Tool (jarvis_notification Template) ────────────

@server.tool()
def whatsapp_send(
    to: str,
    message: str,
    template_name: str = "jarvis_notification",
) -> str:
    """Send a WhatsApp message to one or more phone numbers using Meta WhatsApp Cloud API and the 'jarvis_notification' template.

    Parameters:
      - to: Recipient phone number (e.g. '+27712345678' or '0712345678', or comma-separated numbers)
      - message: The message text to send
      - template_name: Approved Meta WhatsApp template name (default: 'jarvis_notification')
    """
    from actions.send_message import _parse_recipients
    from core.whatsapp_client import WhatsAppClient, load_whatsapp_config

    to_clean = (to or "").strip()
    msg_clean = (message or "").strip()
    if not to_clean:
        return "[WhatsApp] Error: Please provide a recipient phone number ('to')."
    if not msg_clean:
        return "[WhatsApp] Error: Please provide the 'message' content."

    try:
        cfg = load_whatsapp_config()
        client = WhatsAppClient(cfg)
        recipients = _parse_recipients(to_clean)
        successes: list[str] = []
        failures: list[str] = []

        for name, phone in recipients:
            target_text = msg_clean
            if name:
                target_text = target_text.replace("[name]", name).replace("[Name]", name)
            try:
                res = client.send_message(
                    recipient=phone,
                    message=target_text,
                    template_name=template_name or cfg.template_name,
                )
                clean_num = res.get("recipient", phone)
                label = f"{name} ({clean_num})" if name else clean_num
                successes.append(label)
            except Exception as exc:
                label = f"{name} ({phone})" if name else phone
                failures.append(f"{label}: {exc}")

        if successes and not failures:
            return f"✅ WhatsApp message sent via '{template_name}' to {', '.join(successes)}."
        if successes and failures:
            return (
                f"⚠️ WhatsApp partially sent: {len(successes)} succeeded ({', '.join(successes)}), "
                f"{len(failures)} failed ({'; '.join(failures)})."
            )
        return f"[WhatsApp] Delivery failed: {'; '.join(failures)}"
    except Exception as exc:
        return f"[WhatsApp] Configuration or delivery error: {exc}"


# ── LinkedIn MCP Tools ────────────────────────────────────────────────────

@server.tool()
def linkedin_status() -> str:
    """Check whether Jarvis is connected to LinkedIn via OAuth and return the connected profile name."""
    from actions.linkedin_agent import linkedin_agent
    return linkedin_agent({"action": "status"})


@server.tool()
def linkedin_read_posts(
    query: str = "me",
    post_url: str = "",
    limit: int = 5,
) -> str:
    """Read LinkedIn posts: read the user's own recent LinkedIn posts, read a specific LinkedIn post URL, or search/read LinkedIn posts by a person, company, or topic.

    Parameters:
      - query: Pass 'me' (default) to read the user's own recent posts, or pass a person's name, company name, or topic (e.g. 'AI agents', 'Satya Nadella')
      - post_url: Optional direct URL to a LinkedIn post (e.g. 'https://www.linkedin.com/posts/...') to read its full text
      - limit: Maximum number of posts to read (default: 5, max: 15)
    """
    from actions.linkedin_agent import linkedin_agent
    return linkedin_agent({
        "action": "read_posts",
        "query": query,
        "post_url": post_url,
        "limit": limit,
    })


@server.tool()
def linkedin_prepare_post(content: str) -> str:
    """Prepare a LinkedIn post draft for user review and return its draft_id before publishing.

    Parameters:
      - content: The full text of the LinkedIn post (up to 3,000 characters)
    """
    from actions.linkedin_agent import linkedin_agent
    return linkedin_agent({"action": "prepare_post", "content": content})


@server.tool()
def linkedin_publish_post(draft_id: str, confirmed: bool = True) -> str:
    """Publish a prepared LinkedIn draft after the user approves it.

    Parameters:
      - draft_id: The draft_id returned by linkedin_prepare_post
      - confirmed: Must be True to confirm user approval
    """
    from actions.linkedin_agent import linkedin_agent
    return linkedin_agent({"action": "publish_post", "draft_id": draft_id, "confirmed": confirmed})


@server.tool()
def linkedin_discard_post(draft_id: str) -> str:
    """Discard a prepared LinkedIn post draft by its draft_id.

    Parameters:
      - draft_id: The draft_id returned by linkedin_prepare_post
    """
    from actions.linkedin_agent import linkedin_agent
    return linkedin_agent({"action": "discard_post", "draft_id": draft_id})


# ── YouTube Music MCP Tools ───────────────────────────────────────────────

@server.tool()
def ytmusic_play(query: str, open_in_app: bool = False) -> str:
    """Play a song, artist, album, or genre on YouTube Music in the Jarvis PWA player (or launch the YouTube Music app).

    Parameters:
      - query: Song title, artist, album, or music vibe to play (e.g. 'Blinding Lights by The Weeknd', 'chill Amapiano mix')
      - open_in_app: If True, also launches the native YouTube Music app directly on the user's phone/device
    """
    from actions.youtube_video import youtube_video
    return youtube_video({
        "action": "play",
        "query": query,
        "open_in_app": open_in_app,
    })


@server.tool()
def ytmusic_control(action: str, volume: int = 80) -> str:
    """Control active YouTube Music playback in Jarvis (pause, resume, next, previous, stop, volume, or open_app).

    Parameters:
      - action: One of 'pause', 'resume', 'next', 'previous', 'stop', 'volume', or 'open_app' (opens current track in YouTube Music app)
      - volume: Volume level from 0 to 100 when action='volume' (default: 80)
    """
    from actions.youtube_video import youtube_video
    return youtube_video({
        "action": action,
        "volume": volume,
    })


@server.tool()
def ytmusic_search(query: str, limit: int = 5) -> str:
    """Search YouTube Music for songs, artists, or albums and return matching tracks with YouTube Music links.

    Parameters:
      - query: Search query (song title, artist, or album)
      - limit: Maximum number of results to return (default: 5, max: 10)
    """
    from core.ytmusic_client import search_ytmusic

    clean_q = (query or "").strip()
    if not clean_q:
        return "[YouTube Music] Please provide a search query."
    tracks = search_ytmusic(clean_q, limit=max(1, min(int(limit or 5), 10)))
    if not tracks:
        return f"[YouTube Music] No tracks found for '{clean_q}'."
    lines = [f"=== YouTube Music Results for '{clean_q}' ==="]
    for idx, t in enumerate(tracks, 1):
        dur = f" ({t['duration']})" if t.get("duration") else ""
        lines.append(
            f"{idx}. **{t.get('title', 'Unknown')}** — {t.get('artist', 'YouTube Music')}{dur}\n"
            f"   Link: {t.get('ytmusic_url', '')}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())

