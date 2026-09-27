from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "config" / "api_keys.json"
DEFAULT_GRAPH_API_VERSION = "v21.0"
DEFAULT_TEMPLATE_NAME = "jarvis_notification"
DEFAULT_TEMPLATE_LANG = "en"


class WhatsAppError(RuntimeError):
    pass


@dataclass(frozen=True)
class WhatsAppConfig:
    phone_number_id: str
    token: str
    api_version: str = DEFAULT_GRAPH_API_VERSION
    timeout: int = 25
    template_name: str = DEFAULT_TEMPLATE_NAME
    template_lang: str = DEFAULT_TEMPLATE_LANG
    business_account_id: str = ""
    use_template: bool = True


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    env_vars: dict[str, str] = {}
    try:
        content = path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k:
                env_vars[k] = v
    except Exception:
        pass
    return env_vars


def _read_config_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_whatsapp_config() -> WhatsAppConfig:
    custom_env = os.environ.get("JARVIS_ENV_PATH") or os.environ.get("ENV_PATH")
    env_candidates = [Path(custom_env)] if custom_env else [
        _base_dir() / ".env",
        Path.cwd() / ".env",
        Path("/root/jarvis/.env"),
        Path("/root/jarvis/config/hermes.env"),
        Path("/root/jarvis/config/jarvis.env"),
        Path("/app/.env"),
    ]
    env_vars: dict[str, str] = {}
    for p in env_candidates:
        for k, v in _read_env_file(p).items():
            if k not in env_vars or not env_vars[k]:
                env_vars[k] = v

    custom_cfg = os.environ.get("JARVIS_CONFIG_PATH") or os.environ.get("CONFIG_PATH")
    config_candidates = [Path(custom_cfg)] if custom_cfg else [
        CONFIG_PATH,
        Path.cwd() / "config" / "api_keys.json",
        _base_dir() / "memory" / "api_keys.json",
        Path.cwd() / "memory" / "api_keys.json",
        Path("/root/jarvis/config/api_keys.json"),
        Path("/app/config/api_keys.json"),
        Path("/app/memory/api_keys.json"),
        Path("/opt/data/api_keys.json"),
        Path("/opt/data/memory/api_keys.json"),
    ]
    config_vars: dict[str, Any] = {}
    for p in config_candidates:
        file_data = _read_config_json(p)
        if not file_data:
            continue
        unpacked: dict[str, Any] = {}
        for parent in ("whatsapp", "whatsapp_api", "meta_whatsapp"):
            sub = file_data.get(parent)
            if isinstance(sub, dict):
                for k, v in sub.items():
                    if isinstance(v, str) and v.strip():
                        unpacked[f"whatsapp_{k}"] = v.strip()
                        unpacked[k] = v.strip()
        for k, v in {**unpacked, **file_data}.items():
            if k not in config_vars or not config_vars[k]:
                config_vars[k] = v

    def _lookup(*keys: str) -> str:
        for k in keys:
            val = os.environ.get(k, "").strip()
            if val:
                return val
        for k in keys:
            val = env_vars.get(k, "").strip()
            if val:
                return val
        for k in keys:
            val = str(config_vars.get(k, "") or config_vars.get(k.lower(), "")).strip()
            if val:
                return val
        return ""

    phone_number_id = _lookup(
        "PHONE_NUMBER_ID",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_PHONE_ID",
        "phone_number_id",
        "whatsapp_phone_number_id",
        "whatsapp_phone_id",
    )
    token = _lookup(
        "WHATSAPP_TOKEN",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_API_TOKEN",
        "WHATSAPP_BEARER_TOKEN",
        "whatsapp_token",
        "whatsapp_access_token",
        "whatsapp_api_token",
    )
    api_version = _lookup("WHATSAPP_API_VERSION", "whatsapp_api_version") or DEFAULT_GRAPH_API_VERSION
    template_name = _lookup(
        "WHATSAPP_TEMPLATE_NAME",
        "WHATSAPP_TEMPLATE",
        "whatsapp_template_name",
        "whatsapp_template",
    ) or DEFAULT_TEMPLATE_NAME
    template_lang = _lookup(
        "WHATSAPP_TEMPLATE_LANG",
        "WHATSAPP_TEMPLATE_LANGUAGE",
        "whatsapp_template_lang",
    ) or DEFAULT_TEMPLATE_LANG
    business_account_id = _lookup(
        "WHATSAPP_BUSINESS_ACCOUNT_ID",
        "WABA_ID",
        "WHATSAPP_WABA_ID",
        "business_account_id",
        "waba_id",
    )
    use_template_raw = _lookup("WHATSAPP_USE_TEMPLATE", "whatsapp_use_template").lower()
    use_template = use_template_raw not in ("false", "0", "no", "off")

    if not phone_number_id or not token:
        raise WhatsAppError("WhatsApp credentials (PHONE_NUMBER_ID and WHATSAPP_TOKEN) are not configured")

    return WhatsAppConfig(
        phone_number_id=phone_number_id,
        token=token,
        api_version=api_version,
        template_name=template_name,
        template_lang=template_lang,
        business_account_id=business_account_id,
        use_template=use_template,
    )


def normalize_phone_number(phone: str, default_country_code: str = "27") -> str:
    """Normalize a phone number string to E.164 digits without leading '+'.
    Supports international (+27..., 27...) and local numbers (071... -> 2771...).
    """
    if not phone or not phone.strip():
        raise WhatsAppError("Recipient phone number cannot be empty")

    raw = phone.strip()
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        raise WhatsAppError(f"Recipient phone number '{phone}' contains no digits")

    # If user provided a 10-digit local number starting with 0 (e.g., 0712345678)
    if digits.startswith("0") and len(digits) == 10:
        digits = f"{default_country_code}{digits[1:]}"

    if len(digits) < 7 or len(digits) > 15:
        raise WhatsAppError(
            f"Phone number '{phone}' (digits: {digits}) must be between 7 and 15 digits according to E.164 standard"
        )

    return digits


class WhatsAppClient:
    def __init__(self, config: WhatsAppConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    def send_text(self, recipient: str, message: str) -> dict[str, Any]:
        """Send a standard free-form text message (requires active 24h customer window)."""
        message = message.strip()
        if not message:
            raise WhatsAppError("Message text cannot be empty")

        clean_recipient = normalize_phone_number(recipient)
        url = f"https://graph.facebook.com/{self.config.api_version}/{self.config.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_recipient,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": message,
            },
        }

        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise WhatsAppError(f"WhatsApp API is unreachable: {exc}") from exc

        try:
            data = resp.json()
        except ValueError:
            data = {}

        if resp.status_code >= 400:
            err = data.get("error", {})
            err_msg = (
                err.get("error_user_msg")
                or err.get("message")
                or data.get("message")
                or f"HTTP {resp.status_code}"
            )
            raise WhatsAppError(f"WhatsApp Cloud API error ({resp.status_code}): {err_msg}")

        messages = data.get("messages", [])
        msg_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else ""

        return {
            "ok": True,
            "message_id": msg_id,
            "recipient": clean_recipient,
            "mode": "text",
            "raw": data,
        }

    def send_template(
        self,
        recipient: str,
        message: str,
        template_name: str | None = None,
        language_code: str | None = None,
    ) -> dict[str, Any]:
        """Send an approved WhatsApp message template (works outside the 24h customer window)."""
        message = message.strip()
        if not message:
            raise WhatsAppError("Message text cannot be empty")

        clean_recipient = normalize_phone_number(recipient)
        t_name = (template_name or self.config.template_name or DEFAULT_TEMPLATE_NAME).strip()
        lang = (language_code or self.config.template_lang or DEFAULT_TEMPLATE_LANG).strip()

        # Clean redundant "Good day" prefix if already present in message to avoid "Good day, Good day"
        var_text = re.sub(r"^good\s*day[,\s]*", "", message, flags=re.IGNORECASE).strip()
        if not var_text:
            var_text = message

        url = f"https://graph.facebook.com/{self.config.api_version}/{self.config.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_recipient,
            "type": "template",
            "template": {
                "name": t_name,
                "language": {
                    "code": lang,
                },
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {
                                "type": "text",
                                "text": var_text,
                            }
                        ],
                    }
                ],
            },
        }

        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise WhatsAppError(f"WhatsApp API is unreachable: {exc}") from exc

        try:
            data = resp.json()
        except ValueError:
            data = {}

        if resp.status_code >= 400:
            err = data.get("error", {})
            err_code = err.get("code")
            err_msg = (
                err.get("error_user_msg")
                or err.get("message")
                or data.get("message")
                or f"HTTP {resp.status_code}"
            )
            if err_code in (132000, 132001) or "template" in str(err_msg).lower():
                err_msg = (
                    f"{err_msg}. Note: Ensure template '{t_name}' (language '{lang}') "
                    f"is created and approved in Meta WhatsApp Manager (https://business.facebook.com/wa/manage/message-templates/)"
                )
            raise WhatsAppError(f"WhatsApp Cloud API error ({resp.status_code}): {err_msg}")

        messages = data.get("messages", [])
        msg_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else ""

        return {
            "ok": True,
            "message_id": msg_id,
            "recipient": clean_recipient,
            "mode": "template",
            "template_name": t_name,
            "raw": data,
        }

    def send_message(
        self,
        recipient: str,
        message: str,
        use_template: bool | None = None,
        template_name: str | None = None,
        language_code: str | None = None,
    ) -> dict[str, Any]:
        """Send a WhatsApp message, defaulting to template mode for reliable delivery."""
        should_use_template = self.config.use_template if use_template is None else use_template
        if should_use_template:
            try:
                return self.send_template(
                    recipient=recipient,
                    message=message,
                    template_name=template_name,
                    language_code=language_code,
                )
            except WhatsAppError as exc:
                # If auto mode and template failed, try text fallback
                if use_template is None:
                    try:
                        return self.send_text(recipient=recipient, message=message)
                    except Exception:
                        pass
                raise exc

        return self.send_text(recipient=recipient, message=message)
