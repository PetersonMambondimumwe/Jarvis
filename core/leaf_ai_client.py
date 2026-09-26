from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "config" / "api_keys.json"
DEFAULT_DIFY_URL = "https://asc-assistant-dify-662116-169-58-74-235.sslip.io/v1"


class LeafAIError(RuntimeError):
    pass


@dataclass(frozen=True)
class LeafAIConfig:
    api_url: str
    api_key: str
    timeout: int = 25


def load_leaf_ai_config() -> LeafAIConfig:
    api_key = (
        os.environ.get("DIFY_API_KEY", "").strip()
        or os.environ.get("LEAF_AI_DIFY_API_KEY", "").strip()
    )
    api_url = (
        os.environ.get("DIFY_API_URL", "").strip()
        or os.environ.get("LEAF_AI_DIFY_API_URL", "").strip()
    )

    if not api_key or not api_url:
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if not api_key:
                    api_key = str(
                        data.get("dify_api_key")
                        or data.get("leaf_ai_dify_api_key")
                        or ""
                    ).strip()
                if not api_url:
                    api_url = str(
                        data.get("dify_api_url")
                        or data.get("leaf_ai_dify_api_url")
                        or ""
                    ).strip()
        except Exception:
            pass

    if not api_url:
        api_url = DEFAULT_DIFY_URL

    api_url = api_url.rstrip("/")
    parsed = urlparse(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LeafAIError("Leaf AI API URL must be a valid HTTP or HTTPS URL")

    if not api_key:
        raise LeafAIError("Leaf AI API key is not configured")

    try:
        timeout = int(os.environ.get("LEAF_AI_TIMEOUT_SECONDS", 25))
    except (TypeError, ValueError):
        timeout = 25

    return LeafAIConfig(api_url=api_url, api_key=api_key, timeout=max(5, min(timeout, 60)))


def sanitize_leaf_answer(text: str) -> str:
    """Clean up raw markdown or promo links without removing campus details."""
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        lower = line.lower()
        if "buymeacoffee" in lower or ("if you like leaf" in lower and "coffee" in lower):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n\s*---\s*$", "", cleaned)
    return cleaned.strip()


# Regex patterns matching queries specifically intended for Leaf AI knowledge
_LEAF_PATTERNS = [
    re.compile(r"\bleaf\b", re.IGNORECASE),
    re.compile(r"\bleaf\s*ai\b", re.IGNORECASE),
    re.compile(r"\bacademic\s+success\s+coach(es)?\b", re.IGNORECASE),
    re.compile(r"\basc\s+coach(es)?\b", re.IGNORECASE),
    re.compile(r"\basc\s+details\b", re.IGNORECASE),
    re.compile(r"\bvirtual\s+campus\s+assistant\b", re.IGNORECASE),
    re.compile(r"\bdepartment\s+of\s+student\s+affairs\b", re.IGNORECASE),
    re.compile(r"\b(up|campus)\s+bus\s+schedules?\b", re.IGNORECASE),
    re.compile(r"\bpdby\s+newspaper\b", re.IGNORECASE),
]


def is_leaf_ai_query(text: str) -> bool:
    """Detect if a user query is specifically targeted at Leaf AI or its UP domain."""
    if not text:
        return False
    query = text.strip()
    for pattern in _LEAF_PATTERNS:
        if pattern.search(query):
            return True
    return False


class LeafAIClient:
    def __init__(self, config: LeafAIConfig | None = None, session: Any = None):
        self.config = config or load_leaf_ai_config()
        self._session = session or requests.Session()
        self._conversation_id: str = ""

    def query(
        self,
        query_text: str,
        conversation_id: str | None = None,
        user: str = "jarvis-user",
    ) -> dict[str, Any]:
        text = str(query_text or "").strip()
        if not text:
            return {
                "answer": "Please provide a question or topic for the Leaf AI knowledge source.",
                "conversation_id": self._conversation_id,
                "status": "empty_query",
            }

        cid = conversation_id if conversation_id is not None else self._conversation_id
        payload: dict[str, Any] = {
            "inputs": {},
            "query": text,
            "response_mode": "blocking",
            "user": user,
        }
        if cid:
            payload["conversation_id"] = cid

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.config.api_url}/chat-messages"

        try:
            response = self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.config.timeout,
            )
        except requests.Timeout as exc:
            raise LeafAIError("The Leaf AI knowledge service timed out. Please try again in a moment.") from exc
        except requests.ConnectionError as exc:
            raise LeafAIError("Could not connect to the Leaf AI knowledge service.") from exc
        except requests.RequestException as exc:
            raise LeafAIError("Leaf AI service encountered a network error.") from exc

        if response.status_code in {401, 403}:
            raise LeafAIError("Leaf AI service authentication failed.")
        if response.status_code >= 400:
            raise LeafAIError(f"Leaf AI service returned an error status ({response.status_code}).")

        try:
            data = response.json()
        except Exception as exc:
            raise LeafAIError("Invalid response received from Leaf AI knowledge service.") from exc

        raw_answer = str(data.get("answer") or "").strip()
        new_cid = str(data.get("conversation_id") or "").strip()
        if new_cid:
            self._conversation_id = new_cid

        answer = sanitize_leaf_answer(raw_answer)
        if not answer:
            answer = "No information was returned by the Leaf AI knowledge source for that query."

        return {
            "answer": answer,
            "conversation_id": self._conversation_id,
            "status": "success",
            "raw": data,
        }

    def verify(self) -> dict[str, Any]:
        """Check whether the Leaf AI Dify service is reachable."""
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
        }
        url = f"{self.config.api_url}/parameters"
        try:
            resp = self._session.get(url, headers=headers, timeout=10)
            if resp.status_code in {401, 403}:
                return {"healthy": False, "error": "authentication_failed"}
            if resp.status_code == 200:
                return {"healthy": True, "api_url": self.config.api_url}
            return {"healthy": False, "status_code": resp.status_code}
        except requests.Timeout:
            return {"healthy": False, "error": "timeout"}
        except requests.RequestException:
            return {"healthy": False, "error": "unreachable"}
