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


def _load_token() -> str:
    env_token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_PAT")
    if env_token and env_token.strip():
        return env_token.strip()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        token = data.get("github_pat", "").strip()
        if not token:
            raise ValueError("github_pat key is empty in api_keys.json")
        return token
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read config/api_keys.json: {exc}") from exc


# ── PyGithub lazy import ───────────────────────────────────────────────────

def _get_gh():
    """Return an authenticated Github client, or raise a friendly error."""
    try:
        from github import Github, Auth  # type: ignore  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "PyGithub is not installed. Run: pip install PyGithub"
        )
    token = _load_token()
    return Github(auth=Auth.Token(token))


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
    Resolve a repo argument to a PyGithub Repository object.

    Accepts:
      - 'owner/repo'  — tried verbatim first
      - bare 'name'   — case-insensitive search across ALL user repos (personal + orgs)

    On 404 or no match, raises ValueError with a helpful list of close matches.
    """
    from github import GithubException  # type: ignore

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
        except GithubException as exc:
            if exc.status == 404:
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


# ── Dispatch table ─────────────────────────────────────────────────────────

_ACTIONS: dict[str, Any] = {
    "list_repos":       _list_repos,
    "repo_info":        _repo_info,
    "list_issues":      _list_issues,
    "create_issue":     _create_issue,
    "close_issue":      _close_issue,
    "list_prs":         _list_prs,
    "create_pr":        _create_pr,
    "merge_pr":         _merge_pr,
    "list_branches":    _list_branches,
    "create_branch":    _create_branch,
    "read_file":        _read_file,
    "list_commits":     _list_commits,
    "search_repos":     _search_repos,
    "user_info":        _user_info,
    "activity_summary": _activity_summary,
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
