from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from core.linkedin_client import LinkedInClient, LinkedInError, load_linkedin_config


DRAFTS_PATH = Path(__file__).resolve().parent.parent / "memory" / "linkedin_drafts.json"
DRAFT_TTL_SECONDS = 30 * 60


def _client() -> LinkedInClient:
    return LinkedInClient(load_linkedin_config())


def _load_drafts() -> dict[str, dict]:
    try:
        data = json.loads(DRAFTS_PATH.read_text(encoding="utf-8"))
        drafts = data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        drafts = {}
    now = time.time()
    return {
        key: value
        for key, value in drafts.items()
        if isinstance(value, dict) and float(value.get("expires_at") or 0) > now
    }


def _save_drafts(drafts: dict[str, dict]) -> None:
    DRAFTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = DRAFTS_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(drafts, separators=(",", ":")), encoding="utf-8")
    temp.replace(DRAFTS_PATH)


def linkedin_agent(parameters: dict, **kwargs) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().lower()
    if action in {"verify", "check"}:
        action = "status"
    if action in {"draft", "prepare"}:
        action = "prepare_post"
    if action in {"publish", "post"}:
        action = "publish_post"

    try:
        client = _client()
        if action == "status":
            status = client.status()
            if status["connected"]:
                return f"LinkedIn is securely connected as {status['name']}."
            if status.get("reason") == "expired":
                return "LinkedIn authorization has expired. Reconnect it from the Jarvis dashboard."
            return "LinkedIn is configured but not connected. Use the LinkedIn button in the Jarvis dashboard."

        if action == "prepare_post":
            content = str(params.get("content") or "").strip()
            if not content:
                return "Provide the exact LinkedIn post text to prepare."
            if len(content) > 3000:
                return "The LinkedIn post exceeds the 3,000-character limit."
            drafts = _load_drafts()
            draft_id = uuid.uuid4().hex[:10]
            drafts[draft_id] = {
                "content": content,
                "created_at": int(time.time()),
                "expires_at": int(time.time()) + DRAFT_TTL_SECONDS,
            }
            _save_drafts(drafts)
            return (
                f"LinkedIn draft {draft_id} is ready:\n\n{content}\n\n"
                "Nothing has been published. Ask the user to explicitly approve this exact draft."
            )

        if action == "discard_post":
            draft_id = str(params.get("draft_id") or "").strip()
            drafts = _load_drafts()
            removed = drafts.pop(draft_id, None)
            _save_drafts(drafts)
            return "LinkedIn draft discarded." if removed else "LinkedIn draft was not found or has expired."

        if action == "publish_post":
            draft_id = str(params.get("draft_id") or "").strip()
            confirmed = params.get("confirmed") is True
            if not draft_id or not confirmed:
                return "Publishing was blocked. An existing draft and explicit user confirmation are required."
            drafts = _load_drafts()
            draft = drafts.get(draft_id)
            if not draft:
                return "LinkedIn draft was not found or has expired. Prepare it again for approval."
            post_id = client.publish_text(str(draft.get("content") or ""))
            drafts.pop(draft_id, None)
            _save_drafts(drafts)
            return f"LinkedIn post published successfully. Post ID: {post_id}."

        return "LinkedIn supports status, prepare_post, publish_post, and discard_post."
    except LinkedInError as exc:
        return f"LinkedIn could not complete that request: {exc}."
    except Exception:
        return "LinkedIn integration failed safely because of an unexpected internal error."

