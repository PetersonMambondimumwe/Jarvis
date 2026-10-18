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
        post_id = str(response.headers.get("X-RestLi-Id") or "published")
        self._record_published_post(
            post_id=post_id,
            text=commentary,
            author_name=str(profile.get("name") or "LinkedIn member"),
        )
        return post_id

    @staticmethod
    def _history_path() -> Path:
        return _base_dir() / "memory" / "linkedin_published.json"

    def _record_published_post(self, post_id: str, text: str, author_name: str) -> None:
        path = self._history_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            items: list[dict[str, Any]] = []
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    items = raw
            items.insert(
                0,
                {
                    "id": post_id,
                    "author": author_name,
                    "text": text,
                    "created_at": int(time.time()),
                },
            )
            path.write_text(json.dumps(items[:100], indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_published_history(self, limit: int = 5) -> list[dict[str, Any]]:
        path = self._history_path()
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [item for item in raw if isinstance(item, dict)][:limit]
        except Exception:
            pass
        return []

    def _fetch_api_posts(self, limit: int = 5) -> list[dict[str, Any]]:
        """Attempt to read the authenticated member's posts via LinkedIn v2 ugcPosts / REST posts API."""
        posts: list[dict[str, Any]] = []
        try:
            token = self._valid_token()
        except Exception:
            return posts

        profile = token.get("profile") if isinstance(token.get("profile"), dict) else {}
        member_id = str(profile.get("sub") or "").strip() or self.config.member_id
        if not member_id:
            return posts
        author_urn = member_id if member_id.startswith("urn:li:") else f"urn:li:person:{member_id}"
        author_name = str(profile.get("name") or "You")
        headers = {
            "Authorization": f"Bearer {token['access_token']}",
            "Accept": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        }

        # 1. Try v2 ugcPosts by author
        try:
            from urllib.parse import quote
            encoded_urn = quote(author_urn, safe="")
            url = f"{UGC_POSTS_URL}?q=authors&authors=List({encoded_urn})&count={limit}"
            resp = self.session.get(url, headers=headers, timeout=self.config.timeout)
            if resp.status_code == 200:
                body = self._json(resp)
                for el in body.get("elements", []) or []:
                    if not isinstance(el, dict):
                        continue
                    spec = el.get("specificContent", {})
                    share = spec.get("com.linkedin.ugc.ShareContent", {}) if isinstance(spec, dict) else {}
                    comm = share.get("shareCommentary", {}) if isinstance(share, dict) else {}
                    txt = str(comm.get("text") or "").strip()
                    if txt:
                        created_ms = (el.get("created") or {}).get("time") or 0
                        posts.append({
                            "id": str(el.get("id") or ""),
                            "author": author_name,
                            "text": txt,
                            "created_at": int(created_ms / 1000) if created_ms else 0,
                        })
        except Exception:
            pass

        if posts:
            return posts[:limit]

        # 2. Try REST Posts API
        try:
            from urllib.parse import quote
            encoded_urn = quote(author_urn, safe="")
            rest_url = f"https://api.linkedin.com/rest/posts?author={encoded_urn}&q=author&count={limit}"
            rest_headers = {**headers, "LinkedIn-Version": "202405"}
            resp = self.session.get(rest_url, headers=rest_headers, timeout=self.config.timeout)
            if resp.status_code == 200:
                body = self._json(resp)
                for el in body.get("elements", []) or []:
                    if not isinstance(el, dict):
                        continue
                    txt = str(el.get("commentary") or "").strip()
                    if txt:
                        created_ms = el.get("createdAt") or 0
                        posts.append({
                            "id": str(el.get("id") or ""),
                            "author": author_name,
                            "text": txt,
                            "created_at": int(created_ms / 1000) if created_ms else 0,
                        })
        except Exception:
            pass

        return posts[:limit]

    def _extract_public_post_url(self, url: str) -> Optional[dict[str, str]]:
        """Extract author, title, and full commentary text from a public LinkedIn post URL."""
        import html as _html
        import re as _re

        clean_url = url.strip()
        if not clean_url.startswith("http"):
            return None

        ua_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            resp = self.session.get(clean_url, headers=ua_headers, timeout=12)
            if resp.status_code == 200 and resp.text:
                raw_html = resp.text
                # Check JSON-LD first for full articleBody / text
                for m in _re.finditer(
                    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                    raw_html,
                    flags=_re.DOTALL | _re.IGNORECASE,
                ):
                    try:
                        ld = json.loads(m.group(1).strip())
                        items = ld if isinstance(ld, list) else [ld]
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            body_txt = str(
                                item.get("articleBody")
                                or item.get("text")
                                or item.get("description")
                                or ""
                            ).strip()
                            author_obj = item.get("author")
                            author_name = ""
                            if isinstance(author_obj, dict):
                                author_name = str(author_obj.get("name") or "")
                            elif isinstance(author_obj, str):
                                author_name = author_obj
                            if body_txt and len(body_txt) > 25:
                                return {
                                    "author": _html.unescape(author_name or "LinkedIn Author"),
                                    "text": _html.unescape(body_txt),
                                    "url": clean_url,
                                }
                    except Exception:
                        pass

                # Fallback to OpenGraph description & title
                og_desc = _re.search(
                    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
                    raw_html,
                    flags=_re.IGNORECASE,
                ) or _re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
                    raw_html,
                    flags=_re.IGNORECASE,
                )
                og_title = _re.search(
                    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                    raw_html,
                    flags=_re.IGNORECASE,
                )
                if og_desc:
                    txt = _html.unescape(og_desc.group(1)).strip()
                    title = _html.unescape(og_title.group(1)).strip() if og_title else "LinkedIn Post"
                    if txt:
                        return {"author": title, "text": txt, "url": clean_url}
        except Exception:
            pass
        return None

    def _search_public_linkedin_posts(self, query: str, limit: int = 5) -> list[dict[str, str]]:
        """Search public LinkedIn posts by topic, person, or company and extract post content."""
        import html as _html
        import re as _re

        results: list[dict[str, str]] = []
        search_q = f"site:linkedin.com/posts {query}".strip()

        # 1. Try Tavily API if configured for rich content snippets
        env_vars = _get_all_env_vars()
        cfg_vars = _get_all_config_vars()
        tavily_key = (
            os.environ.get("TAVILY_API_KEY")
            or env_vars.get("TAVILY_API_KEY")
            or str(cfg_vars.get("tavily_api_key") or "")
        ).strip()
        if tavily_key:
            try:
                r = self.session.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": tavily_key,
                        "query": search_q,
                        "max_results": limit,
                        "include_raw_content": False,
                    },
                    timeout=12,
                )
                if r.status_code == 200:
                    data = r.json()
                    for item in data.get("results", [])[:limit]:
                        url = str(item.get("url") or "")
                        title = str(item.get("title") or "LinkedIn Post")
                        snippet = str(item.get("content") or "").strip()
                        extracted = self._extract_public_post_url(url) if url else None
                        results.append({
                            "author": (extracted or {}).get("author") or title,
                            "text": (extracted or {}).get("text") or snippet,
                            "url": url,
                        })
                    if results:
                        return results
            except Exception:
                pass

        # 2. Fallback to DuckDuckGo HTML search for site:linkedin.com/posts
        try:
            ddg_resp = self.session.post(
                "https://html.duckduckgo.com/html/",
                data={"q": search_q},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    )
                },
                timeout=12,
            )
            if ddg_resp.status_code == 200:
                blocks = _re.findall(
                    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
                    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                    ddg_resp.text,
                    flags=_re.DOTALL,
                )
                for href, raw_title, raw_snip in blocks[:limit]:
                    clean_title = _html.unescape(_re.sub(r"<[^>]+>", "", raw_title)).strip()
                    clean_snip = _html.unescape(_re.sub(r"<[^>]+>", "", raw_snip)).strip()
                    extracted = self._extract_public_post_url(href) if "linkedin.com/" in href else None
                    results.append({
                        "author": (extracted or {}).get("author") or clean_title,
                        "text": (extracted or {}).get("text") or clean_snip,
                        "url": href,
                    })
        except Exception:
            pass

        return results[:limit]

    def read_posts(self, query: str = "", post_url: str = "", limit: int = 5) -> str:
        """Read LinkedIn posts: supports reading a specific post URL, the user's own posts, or posts by topic/author."""
        limit = max(1, min(int(limit or 5), 15))
        q = (query or "").strip()
        target_url = (post_url or "").strip()
        if not target_url and ("linkedin.com/posts/" in q or "linkedin.com/feed/update/" in q or "linkedin.com/pulse/" in q):
            target_url = q

        # Case 1: Specific LinkedIn post URL
        if target_url:
            extracted = self._extract_public_post_url(target_url)
            if extracted and extracted.get("text"):
                return (
                    f"**LinkedIn Post — {extracted.get('author', 'LinkedIn')}**\n"
                    f"URL: {extracted.get('url', target_url)}\n\n"
                    f"{extracted['text']}"
                )
            return f"Could not extract public text from LinkedIn URL: {target_url}"

        # Case 2: User's own posts (empty query, 'me', 'my posts', 'mine')
        is_self_query = not q or q.lower() in {"me", "my", "mine", "my posts", "own", "self", "latest", "recent"}
        if is_self_query:
            api_posts = self._fetch_api_posts(limit=limit)
            local_posts = self._load_published_history(limit=limit)

            # Merge deduplicated by post ID or text prefix
            merged: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in [*api_posts, *local_posts]:
                txt = str(item.get("text") or "").strip()
                if not txt:
                    continue
                key = txt[:120].lower()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)

            if merged:
                lines = [f"=== Your Recent LinkedIn Posts ({len(merged[:limit])}) ===\n"]
                for idx, p in enumerate(merged[:limit], 1):
                    ts = int(p.get("created_at") or 0)
                    date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "Recent"
                    pid = str(p.get("id") or "")
                    id_tag = f" (ID: `{pid}`)" if pid else ""
                    lines.append(f"{idx}. **{p.get('author', 'You')}** — {date_str}{id_tag}\n{p['text']}\n")
                return "\n".join(lines).strip()

            # If no local or API posts were returned, try searching public posts under connected profile name
            try:
                st = self.status()
                profile_name = str(st.get("name") or "").strip()
                if profile_name and profile_name.lower() != "linkedin member":
                    pub = self._search_public_linkedin_posts(profile_name, limit=limit)
                    if pub:
                        lines = [f"=== LinkedIn Posts for {profile_name} ===\n"]
                        for idx, item in enumerate(pub, 1):
                            lines.append(
                                f"{idx}. **{item.get('author', profile_name)}**\n"
                                f"{item.get('text', '')}\n"
                                f"URL: {item.get('url', '')}\n"
                            )
                        return "\n".join(lines).strip()
            except Exception:
                pass

            return (
                "No recent LinkedIn posts were found in your Jarvis publish history, and LinkedIn's API "
                "did not return any authored posts (note: LinkedIn restricts reading member feed history "
                "unless Marketing Developer Platform 'r_member_social' is enabled on your LinkedIn app). "
                "You can also pass a topic, person, company name, or post URL to read public LinkedIn posts."
            )

        # Case 3: Search and read LinkedIn posts by topic, person, or company
        found = self._search_public_linkedin_posts(q, limit=limit)
        if not found:
            return f"No public LinkedIn posts found matching '{q}'."

        lines = [f"=== LinkedIn Posts Matching '{q}' ({len(found)}) ===\n"]
        for idx, item in enumerate(found, 1):
            text_body = item.get("text") or "(No text preview available)"
            if len(text_body) > 1200:
                text_body = text_body[:1200] + "…"
            lines.append(
                f"{idx}. **{item.get('author', 'LinkedIn Post')}**\n"
                f"{text_body}\n"
                f"URL: {item.get('url', '')}\n"
            )
        return "\n".join(lines).strip()

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

