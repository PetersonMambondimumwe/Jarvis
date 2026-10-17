"""
actions/github_manager.py
─────────────────────────────────────────────────────────────────────────────
Jarvis GitHub integration — lets the user talk to their GitHub account via
natural language.  Powered by PyGithub.

Supported actions
─────────────────
  list_repos          — list all repos (owned, starred, or all visible)
  repo_info           — detailed info for one repo
  list_issues         — open/closed issues on a repo
  create_issue        — open a new issue
  close_issue         — close an issue by number
  list_prs            — list pull requests on a repo
  create_pr           — open a pull request
  merge_pr            — merge a pull request
  list_branches       — list branches on a repo
  create_branch       — create a new branch from another
  read_file           — read a file from a repo
  list_commits        — recent commits on a repo/branch
  search_repos        — search GitHub for repos by keyword
  user_info           — authenticated user profile
  activity_summary    — aggregate recent activity across repos

Usage (tool call from Gemini)
─────────────────────────────
  github_manager(action="list_repos")
  github_manager(action="list_issues", repo="Mark-XLVIII")
  github_manager(action="create_issue", repo="Mark-XLVIII",
                 title="Fix login bug", body="Steps to reproduce...")
  github_manager(action="read_file", repo="Mark-XLVIII", path="README.md")
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional


# ── Config helpers ─────────────────────────────────────────────────────────

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "config" / "api_keys.json"


def _read_env_vars() -> dict[str, str]:
    env_vars: dict[str, str] = {}
    for candidate in [
        _base_dir() / ".env",
        Path.cwd() / ".env",
        Path("/root/jarvis/.env"),
        Path("/app/.env"),
    ]:
        if candidate.is_file():
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k and k not in env_vars:
                        env_vars[k] = v
            except Exception:
                pass
    return env_vars


def _load_token() -> str:
    env_token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_PAT")
    if env_token and env_token.strip():
        return env_token.strip()
    dotenv_vars = _read_env_vars()
    for key in ("GITHUB_TOKEN", "GITHUB_PAT", "github_pat"):
        if dotenv_vars.get(key, "").strip():
            return dotenv_vars[key].strip()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        token = (
            data.get("github_pat")
            or data.get("GITHUB_TOKEN")
            or data.get("GITHUB_PAT")
            or ""
        ).strip()
        if not token:
            raise ValueError("github_pat / GITHUB_TOKEN is empty in api_keys.json and .env")
        return token
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read GitHub token from .env or config/api_keys.json: {exc}") from exc


# ── PyGithub lazy import + Native REST API v3 Fallback ─────────────────────

class _RestGithubException(Exception):
    def __init__(self, status: int, data: Any):
        self.status = status
        self.data = data
        super().__init__(f"GitHub API HTTP {status}: {data}")


class _SimpleObj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _RestRepo:
    def __init__(self, client: "_RestGithubClient", data: dict):
        self._c = client
        self._update_from(data)

    def _update_from(self, data: dict) -> None:
        self.name = data.get("name", "")
        self.full_name = data.get("full_name", "")
        self.description = data.get("description")
        self.language = data.get("language")
        self.stargazers_count = data.get("stargazers_count", 0)
        self.forks_count = data.get("forks_count", 0)
        self.open_issues_count = data.get("open_issues_count", 0)
        self.default_branch = data.get("default_branch", "main")
        self.private = bool(data.get("private", False))
        self.html_url = data.get("html_url", f"https://github.com/{self.full_name}")
        self.pushed_at = data.get("pushed_at", "")
        owner_data = data.get("owner") or {}
        self.owner = _SimpleObj(login=owner_data.get("login", self.full_name.split("/")[0] if "/" in self.full_name else ""))
        self._topics = data.get("topics") or []

    def get_topics(self) -> list[str]:
        return list(self._topics)

    def replace_topics(self, names: list[str]) -> None:
        res = self._c._req("PUT", f"/repos/{self.full_name}/topics", json={"names": names})
        self._topics = res.get("names", names)

    def edit(self, **kwargs) -> None:
        res = self._c._req("PATCH", f"/repos/{self.full_name}", json=kwargs)
        if isinstance(res, dict):
            self._update_from(res)

    def delete(self) -> None:
        self._c._req("DELETE", f"/repos/{self.full_name}")

    def get_contents(self, path: str, ref: Optional[str] = None):
        import base64
        params = {"ref": ref} if ref else None
        res = self._c._req("GET", f"/repos/{self.full_name}/contents/{path.lstrip('/')}", params=params)
        if isinstance(res, list):
            return [_SimpleObj(type=item.get("type", "file"), path=item.get("path", "")) for item in res]
        raw_b64 = res.get("content", "")
        decoded = base64.b64decode(raw_b64) if raw_b64 else b""
        return _SimpleObj(
            type=res.get("type", "file"),
            path=res.get("path", path),
            sha=res.get("sha", ""),
            decoded_content=decoded,
        )

    def create_file(self, path: str, message: str, content: str, branch: Optional[str] = None) -> dict:
        import base64
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        payload: dict[str, Any] = {"message": message, "content": b64}
        if branch:
            payload["branch"] = branch
        res = self._c._req("PUT", f"/repos/{self.full_name}/contents/{path.lstrip('/')}", json=payload)
        commit_sha = (res.get("commit") or {}).get("sha", "0000000")
        return {"commit": _SimpleObj(sha=commit_sha)}

    def update_file(self, path: str, message: str, content: str, sha: str, branch: Optional[str] = None) -> dict:
        import base64
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        payload: dict[str, Any] = {"message": message, "content": b64, "sha": sha}
        if branch:
            payload["branch"] = branch
        res = self._c._req("PUT", f"/repos/{self.full_name}/contents/{path.lstrip('/')}", json=payload)
        commit_sha = (res.get("commit") or {}).get("sha", "0000000")
        return {"commit": _SimpleObj(sha=commit_sha)}

    def get_branches(self) -> list:
        res = self._c._req("GET", f"/repos/{self.full_name}/branches", params={"per_page": 100})
        return [_SimpleObj(name=b.get("name", ""), commit=_SimpleObj(sha=(b.get("commit") or {}).get("sha", ""))) for b in res]

    def get_branch(self, branch: str):
        b = self._c._req("GET", f"/repos/{self.full_name}/branches/{branch}")
        return _SimpleObj(name=b.get("name", branch), commit=_SimpleObj(sha=(b.get("commit") or {}).get("sha", "")))

    def create_git_ref(self, ref: str, sha: str):
        return self._c._req("POST", f"/repos/{self.full_name}/git/refs", json={"ref": ref, "sha": sha})

    def create_issue(self, title: str, body: str = "", labels: Optional[list] = None):
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        res = self._c._req("POST", f"/repos/{self.full_name}/issues", json=payload)
        return _SimpleObj(title=res.get("title", title), number=res.get("number", 0), html_url=res.get("html_url", ""))

    def create_pull(self, title: str, body: str, head: str, base: str):
        payload = {"title": title, "body": body, "head": head, "base": base}
        res = self._c._req("POST", f"/repos/{self.full_name}/pulls", json=payload)
        return _SimpleObj(title=res.get("title", title), number=res.get("number", 0), html_url=res.get("html_url", ""))


class _RestUser:
    def __init__(self, client: "_RestGithubClient", data: dict):
        self._c = client
        self.login = data.get("login", "")
        self.name = data.get("name")
        self.bio = data.get("bio")
        self.company = data.get("company")
        self.location = data.get("location")
        self.public_repos = data.get("public_repos", 0)
        self.total_private_repos = data.get("total_private_repos", 0)
        self.followers = data.get("followers", 0)
        self.following = data.get("following", 0)
        self.html_url = data.get("html_url", "")

    def get_repos(self) -> list[_RestRepo]:
        res = self._c._req("GET", "/user/repos", params={"per_page": 100, "sort": "updated"})
        return [_RestRepo(self._c, r) for r in res]

    def get_starred(self) -> list[_RestRepo]:
        res = self._c._req("GET", "/user/starred", params={"per_page": 100})
        return [_RestRepo(self._c, r) for r in res]

    def get_orgs(self) -> list:
        return []

    def create_repo(self, name: str, description: str = "", private: bool = False, auto_init: bool = True) -> _RestRepo:
        res = self._c._req(
            "POST",
            "/user/repos",
            json={"name": name, "description": description, "private": private, "auto_init": auto_init},
        )
        return _RestRepo(self._c, res)


class _RestGithubClient:
    def __init__(self, token: str):
        self._token = token
        self._user_cache: Optional[_RestUser] = None

    def _req(self, method: str, path: str, params: Optional[dict] = None, json: Optional[dict] = None) -> Any:
        import requests
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = requests.request(
            method,
            f"https://api.github.com{path}",
            headers=headers,
            params=params,
            json=json,
            timeout=20,
        )
        if resp.status_code >= 400:
            try:
                err_data = resp.json()
            except Exception:
                err_data = resp.text
            raise _RestGithubException(resp.status_code, err_data)
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    def get_user(self) -> _RestUser:
        if self._user_cache is None:
            self._user_cache = _RestUser(self, self._req("GET", "/user"))
        return self._user_cache

    def get_repo(self, full_name: str) -> _RestRepo:
        return _RestRepo(self, self._req("GET", f"/repos/{full_name}"))


def _get_gh():
    """Return an authenticated Github client (PyGithub if available, else native REST v3 client)."""
    token = _load_token()
    try:
        from github import Github, Auth  # type: ignore  # noqa: F401
        return Github(auth=Auth.Token(token))
    except ImportError:
        return _RestGithubClient(token)


# ── Individual action handlers ─────────────────────────────────────────────

def _list_repos(gh, args: dict) -> str:
    scope = args.get("scope", "owner").lower()  # owner | all | starred
    user = gh.get_user()
    if scope == "starred":
        repos = list(user.get_starred())
    elif scope == "all":
        repos = list(user.get_repos())
    else:
        repos = [r for r in user.get_repos() if r.owner.login == user.login]

    if not repos:
        return "No repositories found."

    lines = [f"📦 **{r.full_name}** — {r.description or 'no description'} "
             f"({'private' if r.private else 'public'}, "
             f"⭐ {r.stargazers_count}, "
             f"🍴 {r.forks_count})"
             for r in repos[:30]]
    header = f"Found **{len(repos)}** repositories (showing up to 30):\n\n"
    return header + "\n".join(lines)


def _repo_info(gh, args: dict) -> str:
    repo = _resolve_repo(gh, args)
    r = repo
    topics = ", ".join(r.get_topics()) or "none"
    return (
        f"**{r.full_name}**\n"
        f"Description : {r.description or 'N/A'}\n"
        f"Language    : {r.language or 'N/A'}\n"
        f"Stars       : {r.stargazers_count}\n"
        f"Forks       : {r.forks_count}\n"
        f"Open issues : {r.open_issues_count}\n"
        f"Default branch: {r.default_branch}\n"
        f"Topics      : {topics}\n"
        f"Visibility  : {'Private' if r.private else 'Public'}\n"
        f"URL         : {r.html_url}\n"
        f"Last pushed : {r.pushed_at}"
    )


def _list_issues(gh, args: dict) -> str:
    repo = _resolve_repo(gh, args)
    state = args.get("state", "open")  # open | closed | all
    issues = list(repo.get_issues(state=state))
    if not issues:
        return f"No {state} issues on {repo.full_name}."
    lines = [
        f"#{i.number} — **{i.title}** "
        f"[{i.state}] by @{i.user.login} | {i.created_at.date()}"
        for i in issues[:25]
    ]
    return f"**{repo.full_name}** — {state} issues ({len(issues)} total, showing 25):\n\n" + "\n".join(lines)


def _create_issue(gh, args: dict) -> str:
    repo = _resolve_repo(gh, args)
    title = args.get("title", "").strip()
    if not title:
        return "Please provide a title for the issue."
    body = args.get("body", "")
    labels = args.get("labels", [])
    issue = repo.create_issue(title=title, body=body, labels=labels)
    return f"✅ Issue created: **{issue.title}** — #{issue.number}\n{issue.html_url}"


def _close_issue(gh, args: dict) -> str:
    repo = _resolve_repo(gh, args)
    number = int(args.get("number", 0))
    if not number:
        return "Please provide the issue number to close."
    issue = repo.get_issue(number=number)
    issue.edit(state="closed")
    return f"✅ Issue #{number} '{issue.title}' has been closed."


def _list_prs(gh, args: dict) -> str:
    repo = _resolve_repo(gh, args)
    state = args.get("state", "open")
    prs = list(repo.get_pulls(state=state))
    if not prs:
        return f"No {state} pull requests on {repo.full_name}."
    lines = [
        f"#{pr.number} — **{pr.title}** [{pr.state}] "
        f"`{pr.head.ref}` → `{pr.base.ref}` by @{pr.user.login}"
        for pr in prs[:20]
    ]
    return f"**Pull Requests** on {repo.full_name} ({state}):\n\n" + "\n".join(lines)


def _create_pr(gh, args: dict) -> str:
    repo = _resolve_repo(gh, args)
    title = args.get("title", "").strip()
    head  = args.get("head", "").strip()   # source branch
    base  = args.get("base", repo.default_branch)
    body  = args.get("body", "")
    if not title or not head:
        return "Please provide both 'title' and 'head' (source branch) for the PR."
    pr = repo.create_pull(title=title, body=body, head=head, base=base)
    return f"✅ Pull request created: **{pr.title}** #{pr.number}\n{pr.html_url}"


def _merge_pr(gh, args: dict) -> str:
    repo   = _resolve_repo(gh, args)
    number = int(args.get("number", 0))
    if not number:
        return "Please provide the PR number to merge."
    pr = repo.get_pull(number)
    if pr.merged:
        return f"PR #{number} is already merged."
    result = pr.merge(merge_method=args.get("method", "merge"))
    return f"✅ PR #{number} merged successfully." if result.merged else "❌ Merge failed."


def _list_branches(gh, args: dict) -> str:
    repo     = _resolve_repo(gh, args)
    branches = list(repo.get_branches())
    lines    = [f"• {b.name}" for b in branches]
    return f"**Branches** on {repo.full_name} ({len(branches)}):\n\n" + "\n".join(lines)


def _create_branch(gh, args: dict) -> str:
    repo   = _resolve_repo(gh, args)
    name   = args.get("branch", "").strip()
    source = args.get("from_branch", repo.default_branch)
    if not name:
        return "Please provide a 'branch' name to create."
    source_ref = repo.get_branch(source)
    repo.create_git_ref(ref=f"refs/heads/{name}", sha=source_ref.commit.sha)
    return f"✅ Branch **{name}** created from **{source}** on {repo.full_name}."


def _read_file(gh, args: dict) -> str:
    repo   = _resolve_repo(gh, args)
    path   = args.get("path", "README.md")
    ref    = args.get("ref", repo.default_branch)
    try:
        content = repo.get_contents(path, ref=ref)
        if isinstance(content, list):
            # it's a directory listing
            lines = [f"• {'📁' if c.type == 'dir' else '📄'} {c.path}" for c in content]
            return f"**{path}/** contents on `{ref}`:\n\n" + "\n".join(lines)
        decoded = content.decoded_content.decode("utf-8", errors="replace")
        # Truncate very large files
        if len(decoded) > 4000:
            decoded = decoded[:4000] + "\n\n… [truncated — file is large]"
        return f"**{repo.full_name}/{path}** (`{ref}`):\n\n```\n{decoded}\n```"
    except Exception as exc:
        return f"Could not read '{path}': {exc}"


def _list_commits(gh, args: dict) -> str:
    repo   = _resolve_repo(gh, args)
    branch = args.get("branch", repo.default_branch)
    limit  = int(args.get("limit", 10))
    commits = list(repo.get_commits(sha=branch))[:limit]
    if not commits:
        return "No commits found."
    lines = [
        f"• `{c.sha[:7]}` — {c.commit.message.splitlines()[0]} "
        f"by @{c.commit.author.name} on {c.commit.author.date.date()}"
        for c in commits
    ]
    return f"**Recent commits** on `{branch}` ({repo.full_name}):\n\n" + "\n".join(lines)


def _search_repos(gh, args: dict) -> str:
    query   = args.get("query", "").strip()
    if not query:
        return "Please provide a search 'query'."
    results = list(gh.search_repositories(query=query))[:15]
    if not results:
        return f"No repositories found for: {query}"
    lines = [
        f"• **{r.full_name}** — {r.description or 'no description'} "
        f"(⭐ {r.stargazers_count}) {r.html_url}"
        for r in results
    ]
    return f"GitHub search results for **'{query}'**:\n\n" + "\n".join(lines)


def _user_info(gh, args: dict) -> str:
    user = gh.get_user()
    return (
        f"👤 **{user.name or user.login}** (@{user.login})\n"
        f"Bio       : {user.bio or 'N/A'}\n"
        f"Company   : {user.company or 'N/A'}\n"
        f"Location  : {user.location or 'N/A'}\n"
        f"Repos     : {user.public_repos} public, {user.total_private_repos} private\n"
        f"Followers : {user.followers} | Following: {user.following}\n"
        f"Profile   : {user.html_url}"
    )


def _activity_summary(gh, args: dict) -> str:
    """Summarise recent events across the authenticated user's repos."""
    user   = gh.get_user()
    events = list(user.get_events())[:30]
    if not events:
        return "No recent activity found."

    summary: dict[str, int] = {}
    for e in events:
        summary[e.type] = summary.get(e.type, 0) + 1

    lines = [f"• {etype}: {count}" for etype, count in sorted(summary.items(), key=lambda x: -x[1])]
    return (
        f"**Recent GitHub activity** for @{user.login} (last 30 events):\n\n"
        + "\n".join(lines)
    )


# ── Repo resolver ──────────────────────────────────────────────────────────

def _all_repos(gh) -> list:
    """
    Return every repo visible to the authenticated user:
    personal repos + repos in any organisation they belong to.
    Results are cached for the lifetime of this call to avoid repeated API hits.
    """
    user  = gh.get_user()
    repos = list(user.get_repos())          # personal + collaborator repos
    # Also grab org repos not already included
    seen = {r.full_name for r in repos}
    try:
        for org in user.get_orgs():
            for r in org.get_repos():
                if r.full_name not in seen:
                    repos.append(r)
                    seen.add(r.full_name)
    except Exception:
        pass  # org access may be restricted — continue with what we have
    return repos


def _resolve_repo(gh, args: dict):
    """
    Resolve a repo argument to a Repository object.

    Accepts:
      - 'owner/repo'  — tried verbatim first
      - bare 'name'   — case-insensitive search across ALL user repos (personal + orgs)

    On 404 or no match, raises ValueError with a helpful list of close matches.
    """
    repo_arg = args.get("repo", "").strip()
    if not repo_arg:
        raise ValueError(
            "Please tell me which repository to use. "
            "For example: 'repo': 'Food-Voucher' or 'owner/repo-name'."
        )

    # ── 1. Explicit 'owner/repo' — try directly ───────────────────────────
    if "/" in repo_arg:
        try:
            return gh.get_repo(repo_arg)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise ValueError(
                    f"Repository '{repo_arg}' not found or not accessible. "
                    "Check the owner and repo name, or your token permissions."
                )
            raise

    # ── 2. Bare name — case-insensitive match across all visible repos ─────
    needle = repo_arg.lower().replace(" ", "-")  # normalise spaces → dashes
    all_r  = _all_repos(gh)

    # Exact case-insensitive match
    exact = [r for r in all_r if r.name.lower() == needle]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # Multiple owners have a repo with the same name — ask user to qualify
        options = ", ".join(r.full_name for r in exact)
        raise ValueError(
            f"Multiple repos named '{repo_arg}' found: {options}. "
            "Please use the full 'owner/repo' format."
        )

    # Partial / fuzzy match — find repos whose name *contains* the needle
    partial = [r for r in all_r if needle in r.name.lower()]
    suggestions = ", ".join(r.full_name for r in partial[:8])
    if partial:
        raise ValueError(
            f"No repo exactly named '{repo_arg}' found. "
            f"Did you mean one of these? {suggestions}"
        )

    # No match at all — list all repos
    all_names = ", ".join(r.name for r in all_r[:25])
    raise ValueError(
        f"Repository '{repo_arg}' not found among your accessible repos. "
        f"Your repos: {all_names}"
    )


# ── Repository mutation & file handlers ───────────────────────────────────

def _rename_repo(gh, args: dict) -> str:
    """Rename an existing GitHub repository."""
    repo = _resolve_repo(gh, args)
    old_full_name = repo.full_name
    new_name = (
        args.get("new_name")
        or args.get("target_name")
        or args.get("name")
        or ""
    ).strip()
    if not new_name:
        return "Please provide 'new_name' for the repository rename."
    if "/" in new_name:
        new_name = new_name.split("/")[-1].strip()
    repo.edit(name=new_name)
    owner_login = old_full_name.split("/")[0]
    new_url = f"https://github.com/{owner_login}/{new_name}"
    return (
        f"✅ Repository renamed successfully: **{old_full_name}** → **{owner_login}/{new_name}**\n"
        f"URL: {new_url}"
    )


def _create_repo(gh, args: dict) -> str:
    """Create a new GitHub repository under the authenticated user."""
    name = (args.get("name") or args.get("repo") or "").strip()
    if not name:
        return "Please provide a 'name' for the new repository."
    if "/" in name:
        name = name.split("/")[-1].strip()
    description = str(args.get("description") or "").strip()
    private = bool(args.get("private", False))
    auto_init = bool(args.get("auto_init", True))
    user = gh.get_user()
    new_repo = user.create_repo(
        name=name,
        description=description,
        private=private,
        auto_init=auto_init,
    )
    vis = "Private" if new_repo.private else "Public"
    return (
        f"✅ Repository created: **{new_repo.full_name}** ({vis})\n"
        f"URL: {new_repo.html_url}"
    )


def _update_repo(gh, args: dict) -> str:
    """Update repository metadata (description, visibility, homepage, default_branch, topics)."""
    repo = _resolve_repo(gh, args)
    edit_kwargs: dict[str, Any] = {}
    if "description" in args and args["description"] is not None:
        edit_kwargs["description"] = str(args["description"])
    if "private" in args and args["private"] is not None:
        if isinstance(args["private"], str):
            edit_kwargs["private"] = args["private"].strip().lower() in ("true", "1", "yes", "private")
        else:
            edit_kwargs["private"] = bool(args["private"])
    if "homepage" in args and args["homepage"] is not None:
        edit_kwargs["homepage"] = str(args["homepage"]).strip()
    if "default_branch" in args and args["default_branch"]:
        edit_kwargs["default_branch"] = str(args["default_branch"]).strip()

    if edit_kwargs:
        repo.edit(**edit_kwargs)

    topics_raw = args.get("topics")
    if topics_raw is not None:
        if isinstance(topics_raw, str):
            topics_list = [t.strip().lower() for t in topics_raw.split(",") if t.strip()]
        elif isinstance(topics_raw, list):
            topics_list = [str(t).strip().lower() for t in topics_raw if str(t).strip()]
        else:
            topics_list = []
        repo.replace_topics(topics_list)

    return f"✅ Repository **{repo.full_name}** updated successfully.\nURL: {repo.html_url}"


def _delete_repo(gh, args: dict) -> str:
    """Delete a GitHub repository (requires confirm=True)."""
    confirmed = args.get("confirm", False)
    if isinstance(confirmed, str):
        confirmed = confirmed.strip().lower() in ("true", "1", "yes")
    if not confirmed:
        return "⚠️ Repository deletion requires explicit confirmation (`confirm=True`)."
    repo = _resolve_repo(gh, args)
    full_name = repo.full_name
    repo.delete()
    return f"✅ Repository **{full_name}** has been permanently deleted."


def _create_or_update_file(gh, args: dict) -> str:
    """Create a new file or update an existing file in a GitHub repository."""
    repo = _resolve_repo(gh, args)
    path = (args.get("path") or "").strip().lstrip("/")
    if not path:
        return "Please provide the file 'path' inside the repository."
    content = args.get("content")
    if content is None:
        return "Please provide 'content' for the file."
    branch = (args.get("branch") or repo.default_branch).strip()
    commit_msg = (args.get("commit_message") or args.get("message") or f"Update {path}").strip()

    try:
        existing = repo.get_contents(path, ref=branch)
        if isinstance(existing, list):
            return f"Error: '{path}' is a directory, not a file."
        res = repo.update_file(
            path=path,
            message=commit_msg,
            content=str(content),
            sha=existing.sha,
            branch=branch,
        )
        commit_sha = res["commit"].sha[:7]
        return f"✅ Updated **{path}** on `{branch}` in **{repo.full_name}** (commit `{commit_sha}`)."
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            res = repo.create_file(
                path=path,
                message=commit_msg,
                content=str(content),
                branch=branch,
            )
            commit_sha = res["commit"].sha[:7]
            return f"✅ Created **{path}** on `{branch}` in **{repo.full_name}** (commit `{commit_sha}`)."
        raise


# ── Dispatch table ─────────────────────────────────────────────────────────

_ACTIONS: dict[str, Any] = {
    "list_repos":            _list_repos,
    "repo_info":             _repo_info,
    "rename_repo":           _rename_repo,
    "create_repo":           _create_repo,
    "update_repo":           _update_repo,
    "delete_repo":           _delete_repo,
    "create_or_update_file": _create_or_update_file,
    "write_file":            _create_or_update_file,
    "list_issues":           _list_issues,
    "create_issue":          _create_issue,
    "close_issue":           _close_issue,
    "list_prs":              _list_prs,
    "create_pr":             _create_pr,
    "merge_pr":              _merge_pr,
    "list_branches":         _list_branches,
    "create_branch":         _create_branch,
    "read_file":             _read_file,
    "list_commits":          _list_commits,
    "search_repos":          _search_repos,
    "user_info":             _user_info,
    "activity_summary":      _activity_summary,
}


# ── Public entry point ─────────────────────────────────────────────────────

def github_manager(
    parameters: dict,
    player=None,
    speak=None,
) -> str:
    """
    Jarvis GitHub manager tool.

    Parameters
    ----------
    parameters : dict
        action  : str  — which action to run (see module docstring)
        repo    : str  — 'owner/repo' or bare repo name
        + action-specific keys
    player    : HUD ui object (optional, for future HUD display)
    speak     : callable (optional, for future TTS feedback)

    Returns
    -------
    str — result text to be spoken/displayed by Jarvis
    """
    action = parameters.get("action", "").strip().lower()
    if not action:
        return (
            "Please specify a GitHub action. Available actions:\n"
            + ", ".join(sorted(_ACTIONS.keys()))
        )

    handler = _ACTIONS.get(action)
    if handler is None:
        return (
            f"Unknown GitHub action: '{action}'. Available actions:\n"
            + ", ".join(sorted(_ACTIONS.keys()))
        )

    try:
        gh = _get_gh()
        result = handler(gh, parameters)
        return result
    except RuntimeError as exc:
        return f"[GitHub] Configuration error: {exc}"
    except ValueError as exc:
        return f"[GitHub] {exc}"
    except Exception as exc:
        # Catch PyGithub GithubException and any network errors
        return f"[GitHub] Error running '{action}': {exc}"
