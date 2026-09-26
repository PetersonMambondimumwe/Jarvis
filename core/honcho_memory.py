"""Fault-tolerant Honcho long-term memory integration for JARVIS."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


_RESOURCE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resource_id(value: Any, fallback: str) -> str:
    cleaned = _RESOURCE_ID_RE.sub("-", str(value or "").strip()).strip("-")
    return (cleaned or fallback)[:128]


class HonchoMemory:
    """Small synchronous client whose public methods always fail open."""

    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        api_key: str,
        workspace_id: str = "jarvis",
        user_peer_id: str = "peterson",
        assistant_peer_id: str = "jarvis",
        timeout: float = 5.0,
        http_session: requests.Session | None = None,
    ) -> None:
        self.enabled = bool(enabled and base_url and api_key)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.workspace_id = _resource_id(workspace_id, "jarvis")
        self.user_peer_id = _resource_id(user_peer_id, "user")
        self.assistant_peer_id = _resource_id(assistant_peer_id, "jarvis")
        self.timeout = max(1.0, min(float(timeout), 30.0))
        self.session_id = f"jarvis-{datetime.now().strftime('%Y-%m-%d')}"
        self._http = http_session or requests.Session()
        self._lock = threading.RLock()
        self._initialized = False
        self._last_error = ""

    @classmethod
    def from_config(cls, config_path: str | Path) -> "HonchoMemory":
        config: dict[str, Any] = {}
        try:
            loaded = json.loads(Path(config_path).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config = loaded
        except (OSError, ValueError):
            pass

        def setting(env_name: str, config_name: str, default: Any = "") -> Any:
            return os.environ.get(env_name, config.get(config_name, default))

        return cls(
            enabled=_as_bool(setting("HONCHO_ENABLED", "honcho_enabled", False)),
            base_url=str(setting("HONCHO_BASE_URL", "honcho_base_url", "")),
            api_key=str(setting("HONCHO_API_KEY", "honcho_api_key", "")),
            workspace_id=str(
                setting("HONCHO_WORKSPACE_ID", "honcho_workspace_id", "jarvis")
            ),
            user_peer_id=str(
                setting("HONCHO_USER_PEER_ID", "honcho_user_peer_id", "peterson")
            ),
            assistant_peer_id=str(
                setting(
                    "HONCHO_ASSISTANT_PEER_ID",
                    "honcho_assistant_peer_id",
                    "jarvis",
                )
            ),
            timeout=float(setting("HONCHO_TIMEOUT", "honcho_timeout", 5.0)),
        )

    @property
    def last_error(self) -> str:
        return self._last_error

    def _url(self, path: str) -> str:
        return f"{self.base_url}/v3/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(
            method,
            self._url(path),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code not in {200, 201, 202, 204}:
            raise RuntimeError(
                f"Honcho returned HTTP {response.status_code} for {method} {path}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _ensure_resources(self) -> None:
        if self._initialized:
            return

        workspace = quote(self.workspace_id, safe="")
        self._request("POST", "workspaces", json={"id": self.workspace_id})
        self._request(
            "POST",
            f"workspaces/{workspace}/peers",
            json={"id": self.user_peer_id, "metadata": {"role": "user"}},
        )
        self._request(
            "POST",
            f"workspaces/{workspace}/peers",
            json={"id": self.assistant_peer_id, "metadata": {"role": "assistant"}},
        )
        self._request(
            "POST",
            f"workspaces/{workspace}/sessions",
            json={
                "id": self.session_id,
                "metadata": {"source": "jarvis"},
                "peers": {
                    self.user_peer_id: {
                        "observe_me": True,
                        "observe_others": False,
                    },
                    self.assistant_peer_id: {
                        "observe_me": False,
                        "observe_others": True,
                    },
                },
            },
        )
        self._initialized = True

    def _run(self, operation: Any, fallback: Any) -> Any:
        if not self.enabled:
            self._last_error = "Honcho memory is not configured."
            return fallback
        try:
            with self._lock:
                self._ensure_resources()
                result = operation()
            self._last_error = ""
            return result
        except Exception as exc:
            self._initialized = False
            self._last_error = str(exc)[:240]
            print(f"[Honcho] Memory operation unavailable: {self._last_error}")
            return fallback

    def record_turn(self, user_text: str, assistant_text: str) -> bool:
        messages = []
        timestamp = datetime.now(timezone.utc).isoformat()
        if user_text.strip():
            messages.append(
                {
                    "content": user_text.strip()[:24000],
                    "peer_id": self.user_peer_id,
                    "metadata": {"source": "jarvis", "kind": "conversation"},
                    "created_at": timestamp,
                }
            )
        if assistant_text.strip():
            messages.append(
                {
                    "content": assistant_text.strip()[:24000],
                    "peer_id": self.assistant_peer_id,
                    "metadata": {"source": "jarvis", "kind": "conversation"},
                    "created_at": timestamp,
                }
            )
        if not messages:
            return False

        workspace = quote(self.workspace_id, safe="")
        session = quote(self.session_id, safe="")

        def operation() -> bool:
            self._request(
                "POST",
                f"workspaces/{workspace}/sessions/{session}/messages",
                json={"messages": messages},
            )
            return True

        return bool(self._run(operation, False))

    def remember(self, content: str) -> str:
        content = content.strip()
        if not content:
            return "No memory content was provided."

        workspace = quote(self.workspace_id, safe="")
        session = quote(self.session_id, safe="")

        def operation() -> str:
            self._request(
                "POST",
                f"workspaces/{workspace}/sessions/{session}/messages",
                json={
                    "messages": [
                        {
                            "content": f"Important fact to remember: {content}"[:24000],
                            "peer_id": self.user_peer_id,
                            "metadata": {"source": "jarvis", "kind": "explicit_memory"},
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ]
                },
            )
            return "Saved to long-term memory."

        return self._run(operation, "Long-term memory is currently unavailable.")

    def recall(self, query: str = "") -> str:
        workspace = quote(self.workspace_id, safe="")
        observer = quote(self.assistant_peer_id, safe="")
        options: dict[str, Any] = {
            "target": self.user_peer_id,
            "include_most_frequent": True,
            "max_conclusions": 25,
        }
        if query.strip():
            options.update({"search_query": query.strip()[:2000], "search_top_k": 12})

        def operation() -> str:
            data = self._request(
                "POST",
                f"workspaces/{workspace}/peers/{observer}/representation",
                json=options,
            )
            representation = str((data or {}).get("representation") or "").strip()
            return representation[:6000] or "No relevant long-term memory was found."

        return self._run(operation, "Long-term memory is currently unavailable.")

    def prompt_context(self, query: str = "") -> str:
        recalled = self.recall(query)
        if recalled in {
            "Long-term memory is currently unavailable.",
            "No relevant long-term memory was found.",
        }:
            return ""
        return f"[HONCHO LONG-TERM MEMORY]\n{recalled[:3500]}\n"

    def status(self) -> str:
        if not self.enabled:
            return "Honcho long-term memory is not configured."

        def operation() -> str:
            return (
                "Honcho long-term memory is connected and authenticated. "
                f"Workspace: {self.workspace_id}."
            )

        return self._run(
            operation,
            f"Honcho long-term memory is unavailable: {self._last_error or 'connection failed'}."
        )
