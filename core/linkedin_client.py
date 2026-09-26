from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
from cryptography.fernet import Fernet, InvalidToken


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


TOKEN_PATH = _base_dir() / "memory" / "linkedin_token.enc"
CONFIG_PATH = _base_dir() / "config" / "api_keys.json"
ENV_PATH = _base_dir() / ".env"
AUTHORIZATION_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
UGC_POSTS_URL = "https://api.linkedin.com/v2/ugcPosts"


class LinkedInError(RuntimeError):
    pass


@dataclass(frozen=True)
class LinkedInConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str = "openid profile email w_member_social"
    member_id: str = ""
    timeout: int = 20


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


def _get_all_env_vars() -> dict[str, str]:
    custom = os.environ.get("JARVIS_ENV_PATH") or os.environ.get("ENV_PATH")
    if custom:
        candidates = [Path(custom)]
    else:
        candidates = [
            ENV_PATH,
            Path.cwd() / ".env",
            Path("/root/jarvis/.env"),
            Path("/root/jarvis/config/hermes.env"),
            Path("/root/jarvis/config/jarvis.env"),
            Path("/app/.env"),
        ]

    merged: dict[str, str] = {}
    for p in candidates:
        for k, v in _read_env_file(p).items():
            if k not in merged or not merged[k]:
                merged[k] = v
    return merged


def _get_all_config_vars() -> dict[str, Any]:
    custom = os.environ.get("JARVIS_CONFIG_PATH") or os.environ.get("CONFIG_PATH")
    if custom:
        candidates = [Path(custom)]
    else:
        candidates = [
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

    merged: dict[str, Any] = {}
    for p in candidates:
        file_data = _read_config_json(p)
        if not file_data:
            continue
        unpacked: dict[str, Any] = {}
        for parent in ("linkedin", "linkedin_oauth", "linkedin_api"):
            sub = file_data.get(parent)
            if isinstance(sub, dict):
                for k, v in sub.items():
                    if isinstance(v, str) and v.strip():
                        unpacked[f"linkedin_{k}"] = v.strip()
                        unpacked[k] = v.strip()
        for k, v in {**unpacked, **file_data}.items():
            if k not in merged or not merged[k]:
                merged[k] = v

    return merged


def load_linkedin_config() -> LinkedInConfig:
    env_vars = _get_all_env_vars()
    config_vars = _get_all_config_vars()

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

    client_id = _lookup(
        "LINKEDIN_CLIENT_ID",
        "LINKEDIN_API_KEY",
        "LINKEDIN_KEY",
        "LINKEDIN_APP_ID",
        "LINKEDIN_CLIENT_KEY",
        "linkedin_client_id",
        "linkedin_api_key",
        "linkedin_key",
        "linkedin_app_id",
        "linkedin_client_key",
    )
    client_secret = _lookup(
        "LINKEDIN_CLIENT_SECRET",
        "LINKEDIN_PRIMARY_CLIENT_SECRET",
        "LINKEDIN_PRIMARY_CLIENT_SECTRET",
        "LINKEDIN_API_SECRET",
        "LINKEDIN_SECRET",
        "LINKEDIN_SECRET_KEY",
        "linkedin_client_secret",
        "linkedin_primary_client_secret",
        "linkedin_primary_client_sectret",
        "linkedin_api_secret",
        "linkedin_secret",
        "linkedin_secret_key",
    )
    redirect_uri = (
        _lookup("LINKEDIN_REDIRECT_URI", "linkedin_redirect_uri")
        or "https://jarvis.littleheartsacademy.online/auth/linkedin/callback"
    )
    scopes = _lookup("LINKEDIN_SCOPES", "linkedin_scopes") or "openid profile email w_member_social"
    if "," in scopes:
        scopes = " ".join(s.strip() for s in scopes.split(",") if s.strip())

    member_id = _lookup("LINKEDIN_MEMBER_ID", "LINKEDIN_PERSON_URN", "LINKEDIN_AUTHOR_URN", "linkedin_member_id")
    if member_id.startswith("urn:li:person:"):
        member_id = member_id.removeprefix("urn:li:person:")

    if not client_id or not client_secret:
        raise LinkedInError("LinkedIn client credentials are not configured")
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "https" or not parsed.netloc:
        raise LinkedInError("LinkedIn redirect URI must be a valid HTTPS URL")
    return LinkedInConfig(client_id, client_secret, redirect_uri, scopes, member_id)


class LinkedInTokenStore:
    def __init__(self, config: LinkedInConfig, path: Path = TOKEN_PATH):
        self.path = path
        key_material = os.environ.get("LINKEDIN_TOKEN_ENCRYPTION_KEY", "").strip()
        if not key_material:
            key_material = f"{config.client_id}\0{config.client_secret}"
        digest = hashlib.sha256(key_material.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def save(self, token: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(token, separators=(",", ":")).encode("utf-8")
        temp = self.path.with_suffix(".tmp")
        temp.write_bytes(self._fernet.encrypt(encoded))
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(self.path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            raw = self._fernet.decrypt(self.path.read_bytes())
            data = json.loads(raw.decode("utf-8"))
        except (InvalidToken, OSError, ValueError, TypeError) as exc:
            raise LinkedInError("LinkedIn authorization data could not be read; reconnect LinkedIn") from exc
        return data if isinstance(data, dict) else None

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise LinkedInError("LinkedIn authorization data could not be removed") from exc


class LinkedInClient:
    def __init__(
        self,
        config: LinkedInConfig,
        session: requests.Session | None = None,
        store: LinkedInTokenStore | None = None,
    ):
        self.config = config
        self.session = session or requests.Session()
        self.store = store or LinkedInTokenStore(config)

    def authorization_url(self, state: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "state": state,
                "scope": self.config.scopes,
            }
        )
        return f"{AUTHORIZATION_URL}?{query}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        if not code.strip():
            raise LinkedInError("LinkedIn did not return an authorization code")
        try:
            response = self.session.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "redirect_uri": self.config.redirect_uri,
                },
                headers={"Accept": "application/json"},
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            raise LinkedInError("LinkedIn is unreachable during authorization") from exc

        body = self._json(response)
        if response.status_code >= 400:
            description = str(body.get("error_description") or body.get("error") or "authorization failed")
            raise LinkedInError(f"LinkedIn authorization failed: {description[:200]}")

        access_token = str(body.get("access_token") or "").strip()
        if not access_token:
            raise LinkedInError("LinkedIn did not return an access token")
        try:
            expires_in = max(60, int(body.get("expires_in") or 3600))
        except (TypeError, ValueError):
            expires_in = 3600

        token = {
            "access_token": access_token,
            "expires_at": int(time.time()) + expires_in,
            "scope": str(body.get("scope") or self.config.scopes),
        }
        profile: dict[str, Any] = {}
        if "openid" in self.config.scopes:
            try:
                profile = self._request_userinfo(access_token)
            except Exception:
                profile = {}

        if not profile.get("sub"):
            try:
                me_resp = self.session.get(
                    "https://api.linkedin.com/v2/me",
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                    timeout=self.config.timeout,
                )
                if me_resp.status_code == 200:
                    me_data = self._json(me_resp)
                    if me_data.get("id"):
                        profile = {
                            "sub": str(me_data.get("id") or ""),
                            "name": f"{me_data.get('localizedFirstName', '')} {me_data.get('localizedLastName', '')}".strip() or "LinkedIn member",
                            "email": "",
                        }
            except Exception:
                pass

        sub = str(profile.get("sub") or "").strip() or self.config.member_id
        name = str(profile.get("name") or "LinkedIn member").strip()
        email = str(profile.get("email") or "").strip()

        token["profile"] = {
            "sub": sub,
            "name": name,
            "email": email,
        }
        if not token["profile"]["sub"]:
            raise LinkedInError(
                "LinkedIn profile verification failed: member ID could not be retrieved. "
                "Ensure 'Sign In with LinkedIn using OpenID Connect' is added in your LinkedIn App > Products, "
                "or set LINKEDIN_MEMBER_ID."
            )
        self.store.save(token)
        return token

    def status(self) -> dict[str, Any]:
        token = self.store.load()
        if not token:
            return {"configured": True, "connected": False, "reason": "not_connected"}
        expires_at = int(token.get("expires_at") or 0)
        profile = token.get("profile") if isinstance(token.get("profile"), dict) else {}
        if expires_at <= int(time.time()) + 60:
            return {
                "configured": True,
                "connected": False,
                "reason": "expired",
                "name": str(profile.get("name") or "LinkedIn member"),
                "expires_at": expires_at,
            }
        return {
            "configured": True,
            "connected": True,
            "name": str(profile.get("name") or "LinkedIn member"),
            "expires_at": expires_at,
        }

    def publish_text(self, commentary: str) -> str:
        commentary = commentary.strip()
        if not commentary:
            raise LinkedInError("LinkedIn post text is empty")
        if len(commentary) > 3000:
            raise LinkedInError("LinkedIn post text exceeds 3,000 characters")

        token = self._valid_token()
        profile = token.get("profile") if isinstance(token.get("profile"), dict) else {}
        member_id = str(profile.get("sub") or "").strip() or self.config.member_id
        if not member_id:
            raise LinkedInError("LinkedIn member ID is missing; reconnect LinkedIn")
        author_urn = member_id if member_id.startswith("urn:li:") else f"urn:li:person:{member_id}"
        payload = {
            "author": author_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": commentary},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }
        try:
            response = self.session.post(
                UGC_POSTS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token['access_token']}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Restli-Protocol-Version": "2.0.0",
                },
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            raise LinkedInError("LinkedIn is unreachable while publishing") from exc

        if response.status_code != 201:
            body = self._json(response)
            if response.status_code == 401:
                raise LinkedInError("LinkedIn authorization expired; reconnect LinkedIn")
            if response.status_code == 403:
                raise LinkedInError("LinkedIn denied posting; enable Share on LinkedIn for the app")
            message = str(body.get("message") or f"HTTP {response.status_code}")
            raise LinkedInError(f"LinkedIn rejected the post: {message[:200]}")
        return str(response.headers.get("X-RestLi-Id") or "published")

    def disconnect(self) -> None:
        self.store.clear()

    def _valid_token(self) -> dict[str, Any]:
        token = self.store.load()
        if not token:
            raise LinkedInError("LinkedIn is not connected")
        if int(token.get("expires_at") or 0) <= int(time.time()) + 60:
            raise LinkedInError("LinkedIn authorization expired; reconnect LinkedIn")
        if not str(token.get("access_token") or "").strip():
            raise LinkedInError("LinkedIn authorization data is incomplete; reconnect LinkedIn")
        return token

    def _request_userinfo(self, access_token: str) -> dict[str, Any]:
        try:
            response = self.session.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            raise LinkedInError("LinkedIn profile verification failed") from exc
        body = self._json(response)
        if response.status_code >= 400:
            raise LinkedInError("LinkedIn profile verification failed")
        return body

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}

