from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.task_manager import TaskManager
from urllib.parse import urlparse

import requests


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "config" / "api_keys.json"
DEFAULT_TIMEOUT = 90


@dataclass(frozen=True)
class EdithConfig:
    api_key: str
    endpoint: str
    agent_api_base: str
    agent_id: str
    chat_url: str
    conversation_id: str
    agent_name: str
    timeout: int
    app_id: str = ""


class EdithConfigError(RuntimeError):
    pass


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise EdithConfigError(f"config/api_keys.json is not valid JSON: {exc}") from exc
    except Exception as exc:
        raise EdithConfigError(f"Could not read EDITH config: {exc}") from exc


def _first_config_value(config: dict, *keys: str) -> str:
    for key in keys:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _load_edith_config() -> EdithConfig:
    cfg = _load_config()
    api_key = _first_config_value(cfg, "base44_api_key", "edith_api_key")
    if not api_key:
        raise EdithConfigError(
            "Base44 API key is missing. Add base44_api_key to config/api_keys.json."
        )

    endpoint = _first_config_value(
        cfg,
        "base44_edith_endpoint",
        "edith_agent_endpoint",
        "edith_webhook_url",
        "base44_agent_endpoint",
    )
    agent_api_base = _first_config_value(
        cfg,
        "base44_edith_agent_api_base",
        "base44_agent_api_base",
        "edith_agent_api_base",
    )
    agent_id = _first_config_value(cfg, "base44_edith_agent_id", "base44_agent_id", "edith_agent_id")
    chat_url = _first_config_value(cfg, "base44_edith_chat_url", "edith_chat_url")
    conversation_id = _first_config_value(
        cfg,
        "base44_edith_conversation_id",
        "base44_conversation_id",
        "edith_conversation_id",
    )
    timeout_raw = _first_config_value(cfg, "base44_timeout_seconds", "edith_timeout_seconds")
    try:
        timeout = int(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT
    except ValueError:
        timeout = DEFAULT_TIMEOUT

    return EdithConfig(
        api_key=api_key,
        endpoint=endpoint.rstrip("/"),
        agent_api_base=agent_api_base.rstrip("/"),
        agent_id=agent_id,
        chat_url=chat_url,
        conversation_id=conversation_id,
        agent_name=_first_config_value(cfg, "base44_agent_name", "edith_agent_name") or "edith",
        timeout=max(5, min(timeout, 180)),
        app_id=_first_config_value(cfg, "base44_app_id"),
    )


def _log(player, message: str) -> None:
    if player:
        try:
            player.write_log(message)
            return
        except Exception:
            pass
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"))


def _mask(value: str) -> str:
    if not value:
        return "<missing>"
    if len(value) <= 8:
        return "<set>"
    return f"{value[:4]}...{value[-4:]}"


def _valid_https_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _explain_missing_endpoint(config: EdithConfig) -> str:
    if config.agent_id or config.chat_url:
        app_hint = (
            f" I can see EDITH details ({_mask(config.agent_id or config.chat_url)}), "
            "but Jarvis needs either base44_edith_agent_api_base or base44_edith_endpoint."
        )
    elif config.app_id:
        app_hint = (
            f" I can see base44_app_id is configured as {_mask(config.app_id)}, "
            "but Jarvis still needs a callable backend function/webhook URL or agent API base."
        )
    else:
        app_hint = " Base44's external SDK flow also needs the app ID or agent API URL."
    return (
        "Sir, the Base44 API key is configured, but EDITH does not have a callable endpoint yet. "
        "Add base44_edith_agent_api_base or base44_edith_endpoint to config/api_keys.json."
        f"{app_hint}"
    )


def _response_text(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:500] if text else response.reason

    if isinstance(data, dict):
        for key in ("result", "reply", "response", "message", "content", "status"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(data, indent=2, default=str)
    if isinstance(data, list):
        return json.dumps(data, indent=2, default=str)
    return str(data)


def _handle_http_error(response: requests.Response) -> str:
    detail = _response_text(response)
    if response.status_code == 401:
        return "Sir, EDITH authentication failed. The Base44 API key is invalid, expired, or not accepted by the EDITH endpoint."
    if response.status_code == 403:
        if "auth_required" in detail or "private" in detail.lower():
            return (
                "Sir, EDITH refused access because the Base44 app/agent is private and requires authenticated access. "
                "Jarvis sent the configured key as both api_key header and api_key query parameter, "
                "but Base44 did not accept it for this direct agent endpoint."
            )
        return "Sir, EDITH refused access. The API key is valid but does not have permission for this agent or endpoint."
    if response.status_code == 404:
        return "Sir, the EDITH endpoint was not found. Please check base44_edith_endpoint in config/api_keys.json."
    if response.status_code == 408:
        return "Sir, EDITH timed out while processing the request."
    if response.status_code == 409:
        return f"Sir, EDITH reported a conflict: {detail}"
    if response.status_code == 422:
        return f"Sir, EDITH rejected the request payload: {detail}"
    if response.status_code == 429:
        return "Sir, EDITH is rate limited. Please wait a moment before sending another task."
    if 500 <= response.status_code <= 599:
        return f"Sir, EDITH's Base44 service returned an error ({response.status_code}): {detail}"
    return f"Sir, EDITH returned HTTP {response.status_code}: {detail}"


def _build_payload(action: str, task: str, session_memory: dict | None) -> dict[str, Any]:
    return {
        "action": action,
        "agent": "edith",
        "agent_name": "edith",
        "task": task,
        "message": task,
        "metadata": {
            "orchestrator": "Jarvis-Mark-XLVIII",
            "source": "jarvis",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_memory": session_memory or {},
        },
    }


def _headers(config: EdithConfig) -> dict[str, str]:
    return {
        "api_key": config.api_key,
        "x-api-key": config.api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Jarvis-Mark-XLVIII/edith-orchestrator",
    }


def _auth_params(config: EdithConfig) -> dict[str, str]:
    return {"api_key": config.api_key}


def _extract_id(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("id", "conversation_id", "conversationId", "uuid"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        nested = data.get("conversation")
        if isinstance(nested, dict):
            return _extract_id(nested)
    return ""


def _latest_assistant_text(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("result", "reply", "response", "message", "content", "answer"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        messages = data.get("messages")
        if isinstance(messages, list):
            return _latest_assistant_text(messages)
        conversation = data.get("conversation")
        if isinstance(conversation, dict):
            return _latest_assistant_text(conversation)
    if isinstance(data, list):
        for item in reversed(data):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("sender") or item.get("type") or "").lower()
            content = item.get("content") or item.get("message") or item.get("text")
            if isinstance(content, str) and content.strip() and role in ("assistant", "agent", "edith", "bot", ""):
                return content.strip()
    return ""


def _json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text.strip()


def _create_conversation(config: EdithConfig) -> tuple[str, str | None]:
    if config.conversation_id:
        return config.conversation_id, None

    # Base44 conversation creation typically doesn't require a complex payload
    payload = {"name": f"Jarvis Session {time.strftime('%Y-%m-%d')}"}
    try:
        response = requests.post(
            f"{config.agent_api_base}/conversations",
            headers=_headers(config),
            json=payload,
            timeout=config.timeout,
        )
        if response.status_code >= 400:
            return "", _handle_http_error(response)
            
        conv_id = _extract_id(_json_or_text(response))
        if conv_id:
            return conv_id, None
    except Exception as e:
        return "", f"Sir, failed to create EDITH conversation: {e}"
        
    return "", "Sir, EDITH did not return a conversation id."


def _fallback_conversation_id() -> str:
    return f"jarvis-{uuid.uuid4().hex}"


def _post_agent_message(config: EdithConfig, conversation_id: str, task: str) -> requests.Response:
    # Based on the user's verified curl example, the payload must be {"role": "user", "content": "..."}
    payload = {"role": "user", "content": task}
    return requests.post(
        f"{config.agent_api_base}/conversations/{conversation_id}/messages",
        headers=_headers(config),
        json=payload,
        timeout=config.timeout,
    )


def _fetch_conversation(config: EdithConfig, conversation_id: str) -> requests.Response | None:
    candidates = [
        f"{config.agent_api_base}/conversations/{conversation_id}",
        f"{config.agent_api_base}/conversations/{conversation_id}/messages",
    ]
    for url in candidates:
        response = requests.get(
            url,
            headers=_headers(config),
            params=_auth_params(config),
            timeout=config.timeout,
        )
        if response.status_code < 400:
            return response
        if response.status_code in (401, 403):
            return response
    return None


def _call_base44_agent_api(
    config: EdithConfig,
    action: str,
    task: str,
) -> tuple[str, str | None]: # Returns (result_message, conversation_id)
    if not _valid_https_url(config.agent_api_base):
        return "Sir, EDITH agent API base must be an HTTPS URL. Please update base44_edith_agent_api_base.", None

    if action == "verify":
        conversation_id = config.conversation_id
        if not conversation_id:
            conversation_id, error = _create_conversation(config)
            if error:
                return error, None
        response = _post_agent_message(
            config,
            conversation_id,
            "Jarvis connectivity check. Reply with a concise EDITH status.",
        )
        if response.status_code >= 400:
            return _handle_http_error(response), conversation_id
        text = _response_text(response)
        return f"EDITH connection verified. Message endpoint responded: {text}", conversation_id

    conversation_id = config.conversation_id
    if not conversation_id:
        conversation_id, error = _create_conversation(config)
        if error:
            if "auth_required" in error or "private" in error.lower() or "not found" in error.lower():
                conversation_id = _fallback_conversation_id()
            else:
                return error, None
    
    if not conversation_id:
        return "Sir, EDITH could not create or reuse a Base44 conversation.", None

    response = _post_agent_message(config, conversation_id, task)
    if response is None:
        return "Sir, EDITH did not respond to the message request.", None
    if response.status_code >= 400:
        return _handle_http_error(response), conversation_id

    data = _json_or_text(response)
    text = _latest_assistant_text(data)
    if text:
        return f"EDITH System Report: {text}", conversation_id

    time.sleep(2)
    follow_up = _fetch_conversation(config, conversation_id)
    if follow_up is not None and follow_up.status_code >= 400:
        return _handle_http_error(follow_up), conversation_id
    if follow_up is not None:
        text = _latest_assistant_text(_json_or_text(follow_up))
        if text:
            return f"EDITH System Report: {text}", conversation_id

    return f"EDITH accepted the task in conversation {conversation_id}, but no final reply was returned yet.", conversation_id


def _call_edith_endpoint(
    config: EdithConfig,
    action: str,
    task: str,
    session_memory: dict | None,
) -> tuple[str, str | None]:
    if not _valid_https_url(config.endpoint):
        return "Sir, EDITH endpoint must be an HTTPS URL. Please update base44_edith_endpoint.", None

    payload = _build_payload(action=action, task=task, session_memory=session_memory)

    response = requests.post(
        config.endpoint,
        headers=_headers(config),
        json=payload,
        timeout=config.timeout,
    )
    if response.status_code >= 400:
        return _handle_http_error(response), None
    
    text = _response_text(response)
    if action == "verify":
        return f"EDITH connection verified. Endpoint responded: {text}", None
    # For direct endpoint, try to extract conversation_id from response
    conv_id = _extract_id(_json_or_text(response))
    return f"EDITH System Report: {text}", conv_id


def edith_agent(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    task_manager: Optional[TaskManager] = None,
    **kwargs,
) -> str:
    """
    Delegate work from Jarvis to EDITH, the user's Base44 AI agent.

    Jarvis is the central orchestration layer. EDITH is called through a configured
    HTTPS Base44 function/webhook endpoint protected by the Base44 API key.
    """
    params = parameters or {}
    action = str(params.get("action", "delegate")).strip().lower()
    if action in ("check", "health", "status", "test"):
        action = "verify"
    if action not in ("delegate", "verify"):
        action = "delegate"

    task = str(params.get("task") or params.get("message") or "").strip()
    if action == "verify" and not task:
        task = "Verify EDITH connectivity and return a concise status."
    if not task:
        return "Sir, please specify a task for EDITH."

    try:
        config = _load_edith_config()
    except EdithConfigError as exc:
        return f"Sir, EDITH is not configured correctly: {exc}"

    if not config.endpoint and not config.agent_api_base:
        return _explain_missing_endpoint(config)

    mode = "agent API" if config.agent_api_base else "configured endpoint"
    _log(player, f"[EDITH] Delegating via Base44 {mode} ({config.agent_name}).")

    if action == "delegate" and task_manager:
        conversation_id = config.conversation_id
        if not conversation_id:
            try:
                # Create conversation (fast call, usually <1 second since config returns cached conversation id if present)
                conversation_id, error = _create_conversation(config)
                if error:
                    conversation_id = _fallback_conversation_id()
            except Exception:
                conversation_id = _fallback_conversation_id()

        task_manager.delegate_task_async(
            task_description=task,
            conversation_id=conversation_id,
            edith_config=config,
            task=task,
            session_memory=session_memory if isinstance(session_memory, dict) else {},
        )
        return f"I've tasked EDITH with {task}. I'll alert you as soon as she's done."

    else:
        if config.agent_api_base:
            message, _ = _call_base44_agent_api(config=config, action=action, task=task)
            return message
        message, _ = _call_edith_endpoint(
            config=config,
            action=action,
            task=task,
            session_memory=session_memory if isinstance(session_memory, dict) else {},
        )
        return message
