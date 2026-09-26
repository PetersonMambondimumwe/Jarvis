from __future__ import annotations

import json
import os
import sys
import uuid
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


class HermesError(RuntimeError):
    pass


@dataclass(frozen=True)
class HermesConfig:
    api_base: str
    api_key: str
    timeout: int = 20


def load_hermes_config() -> HermesConfig:
    data: dict[str, Any] = {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        if not (os.getenv("HERMES_API_KEY") or os.getenv("HERMES_API_BASE")):
            raise HermesError("config/api_keys.json is missing") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise HermesError(f"Hermes configuration could not be read: {exc}") from exc

    api_base = str(os.getenv("HERMES_API_BASE") or data.get("hermes_api_base") or "http://jarvis-hermes:8642").rstrip("/")
    api_key = str(os.getenv("HERMES_API_KEY") or data.get("hermes_api_key") or "").strip()
    try:
        timeout = int(os.getenv("HERMES_TIMEOUT_SECONDS") or data.get("hermes_timeout_seconds") or 20)
    except (TypeError, ValueError):
        timeout = 20

    parsed = urlparse(api_base)
    private_http_hosts = {"jarvis-hermes", "hermes", "localhost", "127.0.0.1"}
    if parsed.scheme == "http" and parsed.hostname not in private_http_hosts:
        raise HermesError("Hermes HTTP connections are restricted to the private container network")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HermesError("hermes_api_base must be a valid HTTP or HTTPS URL")
    if not api_key:
        raise HermesError("hermes_api_key is not configured")

    return HermesConfig(api_base=api_base, api_key=api_key, timeout=max(5, min(timeout, 60)))


def extract_run_output(data: dict[str, Any]) -> str:
    output = data.get("output")
    if isinstance(output, str):
        return output.strip()
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("text"), str):
                texts.append(item["text"].strip())
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"].strip())
        return "\n".join(text for text in texts if text)
    return ""


class HermesClient:
    def __init__(self, config: HermesConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Jarvis-Mark-XLVIII/hermes-orchestrator",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = dict(self._headers)
        headers.update(kwargs.pop("headers", {}))
        try:
            response = self.session.request(
                method,
                f"{self.config.api_base}{path}",
                headers=headers,
                timeout=self.config.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise HermesError(f"Hermes is unreachable: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = {}

        if response.status_code >= 400:
            if response.status_code == 401:
                message = "Hermes authentication failed"
            elif response.status_code == 404:
                message = "Hermes endpoint or task was not found"
            elif response.status_code == 409:
                message = "Hermes rejected a duplicate task with different input"
            elif response.status_code == 429:
                message = "Hermes is at its background-task limit"
            elif response.status_code >= 500:
                message = f"Hermes returned a server error ({response.status_code})"
            else:
                detail = body.get("error") if isinstance(body, dict) else None
                if isinstance(detail, dict):
                    detail = detail.get("message")
                message = str(detail or f"Hermes returned HTTP {response.status_code}")[:300]
            raise HermesError(message)

        if not isinstance(body, dict):
            raise HermesError("Hermes returned an invalid response")
        return body

    def verify(self) -> dict[str, Any]:
        health = self._request("GET", "/health/detailed")
        capabilities = self._request("GET", "/v1/capabilities")
        return {
            "health": health.get("status", "unknown"),
            "model": capabilities.get("model", "hermes-agent"),
            "run_submission": bool(capabilities.get("features", {}).get("run_submission")),
        }

    def start_run(self, task: str, task_key: str | None = None) -> dict[str, Any]:
        task_key = task_key or uuid.uuid4().hex
        payload = {
            "input": task,
            "session_id": f"jarvis-task-{task_key[:16]}",
            "instructions": (
                "You are Hermes, the dedicated execution agent for Sir Peterson. "
                "You have full execution authority and access over GitHub repositories and Vercel projects. "
                "Available credentials in your environment include GITHUB_TOKEN (or GITHUB_PAT) and "
                "VERCEL_TOKEN (or VERCEL_API_TOKEN), as well as /opt/data/api_keys.json. "
                "You are responsible for executing all tasks, repository actions (issues, pull requests, "
                "branches, commits, file changes, code inspection), and Vercel operations (deployments, "
                "build logs, project inspection, environment variables, domains, redeploying). "
                "Jarvis acts as the personal assistant to Sir Peterson; you are the executor who does the heavy lifting. "
                "Execute the requested task thoroughly using your available terminal and tools. "
                "Never claim an action succeeded unless verified. "
                "Do not reveal secret credentials or tokens in output. "
                "Return a concise result with any remaining user action clearly identified."
            ),
        }
        return self._request(
            "POST",
            "/v1/runs",
            headers={
                "Idempotency-Key": task_key,
                "X-Hermes-Session-Key": "jarvis:main:background-tasks",
            },
            json=payload,
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}")

    def stop_run(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/runs/{run_id}/stop", json={})

    def resolve_approval(self, run_id: str, choice: str) -> dict[str, Any]:
        if choice not in {"once", "deny"}:
            raise HermesError("Hermes approval must be 'once' or 'deny'")
        return self._request(
            "POST",
            f"/v1/runs/{run_id}/approval",
            json={"choice": choice},
        )
