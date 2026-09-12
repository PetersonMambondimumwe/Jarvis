"""
actions/vercel_manager.py
─────────────────────────────────────────────────────────────────────────────
Jarvis Vercel integration — lets the user manage and monitor their hosted
websites via natural language. Uses the Vercel REST API v9/v13.

Supported actions
─────────────────
  list_projects       — list all Vercel projects
  project_info        — detailed info for one project
  list_deployments    — recent deployments for a project (or all)
  deployment_info     — status + meta for a specific deployment
  deployment_logs     — build/runtime log events for a deployment
  check_failures      — scan all projects for failed/errored deployments
  redeploy            — trigger a redeployment of the latest deployment
  cancel_deployment   — cancel a queued or building deployment
  list_domains        — list all domains for a project
  list_env            — list environment variables for a project
  add_env             — add/update an environment variable
  delete_env          — delete an environment variable
  deployment_summary  — health overview across all projects

Usage (natural language → Gemini → tool call)
─────────────────────────────────────────────
  vercel_manager(action="list_projects")
  vercel_manager(action="check_failures")
  vercel_manager(action="list_deployments", project="my-site")
  vercel_manager(action="deployment_logs",  deployment_id="dpl_xxx")
  vercel_manager(action="redeploy",         project="my-site")
  vercel_manager(action="add_env",          project="my-site",
                 key="DATABASE_URL", value="postgres://...", target="production")
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests


# ── Constants ──────────────────────────────────────────────────────────────

VERCEL_API   = "https://api.vercel.com"
TIMEOUT      = 20   # seconds per request


# ── Config helpers ─────────────────────────────────────────────────────────

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "config" / "api_keys.json"


def _load_token() -> str:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        token = data.get("vercel_token", "").strip()
        if not token:
            raise ValueError("vercel_token is empty in config/api_keys.json")
        return token
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read config/api_keys.json: {exc}") from exc


def _headers() -> dict:
    return {"Authorization": f"Bearer {_load_token()}", "Content-Type": "application/json"}


# ── HTTP helpers ───────────────────────────────────────────────────────────

def _get(path: str, params: dict | None = None) -> dict:
    url = f"{VERCEL_API}{path}"
    resp = requests.get(url, headers=_headers(), params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, body: dict) -> dict:
    url = f"{VERCEL_API}{path}"
    resp = requests.post(url, headers=_headers(), json=body, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _patch(path: str, body: dict | None = None) -> dict:
    url = f"{VERCEL_API}{path}"
    resp = requests.patch(url, headers=_headers(), json=body or {}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _delete(path: str) -> dict:
    url = f"{VERCEL_API}{path}"
    resp = requests.delete(url, headers=_headers(), timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


# ── Formatting helpers ─────────────────────────────────────────────────────

_STATE_ICON = {
    "READY":       "✅",
    "ERROR":       "❌",
    "BUILDING":    "🔨",
    "INITIALIZING":"⏳",
    "QUEUED":      "📋",
    "CANCELED":    "🚫",
}

def _state_icon(state: str) -> str:
    return _STATE_ICON.get(state.upper(), "❓")


def _ts(ms: int | None) -> str:
    """Convert epoch milliseconds to a readable date string."""
    if not ms:
        return "N/A"
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "N/A"


# ── Action handlers ────────────────────────────────────────────────────────

def _list_projects(args: dict) -> str:
    data = _get("/v9/projects", params={"limit": 50})
    projects = data.get("projects", [])
    if not projects:
        return "No Vercel projects found on this account."
    lines = []
    for p in projects:
        state  = p.get("latestDeployments", [{}])[0].get("readyState", "UNKNOWN") if p.get("latestDeployments") else "NO DEPLOY"
        icon   = _state_icon(state)
        alias  = p.get("alias", [{}])
        domain = alias[0].get("domain", "no domain") if alias else "no domain"
        lines.append(
            f"{icon} **{p['name']}** — {domain} "
            f"[{state}] | updated {_ts(p.get('updatedAt'))}"
        )
    return f"**Vercel Projects** ({len(projects)} total):\n\n" + "\n".join(lines)


def _project_info(args: dict) -> str:
    project = _require_project(args)
    p = _get(f"/v9/projects/{project}")
    alias  = p.get("alias", [{}])
    domain = alias[0].get("domain", "N/A") if alias else "N/A"
    framework = p.get("framework") or "N/A"
    deployments = p.get("latestDeployments", [])
    latest_state = deployments[0].get("readyState", "N/A") if deployments else "N/A"
    return (
        f"**{p['name']}**\n"
        f"ID          : {p['id']}\n"
        f"Framework   : {framework}\n"
        f"Domain      : {domain}\n"
        f"Latest build: {_state_icon(latest_state)} {latest_state}\n"
        f"Created     : {_ts(p.get('createdAt'))}\n"
        f"Updated     : {_ts(p.get('updatedAt'))}\n"
        f"Dashboard   : https://vercel.com/{p.get('accountId', '')}/{p['name']}"
    )


def _list_deployments(args: dict) -> str:
    params: dict = {"limit": int(args.get("limit", 10))}
    project = args.get("project", "").strip()
    if project:
        params["projectId"] = project
    state_filter = args.get("state", "").upper()
    if state_filter:
        params["state"] = state_filter
    data  = _get("/v6/deployments", params=params)
    depls = data.get("deployments", [])
    if not depls:
        return "No deployments found."
    lines = []
    for d in depls:
        state = d.get("state") or d.get("readyState") or "UNKNOWN"
        icon  = _state_icon(state)
        lines.append(
            f"{icon} `{d['uid'][:12]}…` — **{d.get('name', 'N/A')}** "
            f"[{state}] {d.get('url', '')} | {_ts(d.get('createdAt'))}"
        )
    title = f"Deployments for **{project}**" if project else "All recent deployments"
    return f"**{title}** ({len(depls)} shown):\n\n" + "\n".join(lines)


def _deployment_info(args: dict) -> str:
    dep_id = _require_deployment_id(args)
    d = _get(f"/v13/deployments/{dep_id}")
    state = d.get("readyState") or d.get("state") or "UNKNOWN"
    return (
        f"**Deployment** `{dep_id}`\n"
        f"Project  : {d.get('name', 'N/A')}\n"
        f"Status   : {_state_icon(state)} {state}\n"
        f"URL      : https://{d.get('url', 'N/A')}\n"
        f"Branch   : {d.get('meta', {}).get('githubCommitRef', 'N/A')}\n"
        f"Commit   : {d.get('meta', {}).get('githubCommitMessage', 'N/A')}\n"
        f"Creator  : {d.get('creator', {}).get('username', 'N/A')}\n"
        f"Created  : {_ts(d.get('createdAt'))}\n"
        f"Built at : {_ts(d.get('buildingAt'))}\n"
        f"Ready at : {_ts(d.get('ready'))}"
    )


def _deployment_logs(args: dict) -> str:
    dep_id = _require_deployment_id(args)
    # Use the events/logs endpoint — returns newline-delimited JSON
    url  = f"{VERCEL_API}/v2/deployments/{dep_id}/events"
    resp = requests.get(url, headers=_headers(), timeout=30, stream=True)
    resp.raise_for_status()

    lines  = []
    errors = []
    count  = 0
    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        count += 1
        try:
            event = json.loads(raw_line)
            payload = event.get("payload", {})
            text    = payload.get("text", "").strip()
            level   = payload.get("level", "info").lower()
            if not text:
                continue
            prefix = "❌" if level == "error" else "  "
            lines.append(f"{prefix} {text}")
            if level == "error":
                errors.append(text)
        except json.JSONDecodeError:
            lines.append(f"  {raw_line.decode('utf-8', errors='replace')}")

    if not lines:
        return f"No log events found for deployment `{dep_id}`."

    # Show last 40 lines to avoid overload
    shown = lines[-40:]
    header = f"**Deployment logs** `{dep_id}` ({count} events, last {len(shown)} shown):\n\n"
    summary = ""
    if errors:
        summary = f"\n\n⚠️ **{len(errors)} error(s) detected:**\n" + "\n".join(errors[:5])
    return header + "\n".join(shown) + summary


def _check_failures(args: dict) -> str:
    """Scan all projects for their latest deployment and flag failures."""
    data     = _get("/v9/projects", params={"limit": 50})
    projects = data.get("projects", [])
    if not projects:
        return "No projects found."

    ok, failed, building, unknown = [], [], [], []
    for p in projects:
        name   = p["name"]
        depls  = p.get("latestDeployments", [])
        if not depls:
            unknown.append(name)
            continue
        state = depls[0].get("readyState", "UNKNOWN").upper()
        url   = depls[0].get("url", "")
        entry = f"**{name}** — https://{url}" if url else f"**{name}**"
        if state == "READY":
            ok.append(entry)
        elif state in ("ERROR", "FAILED"):
            failed.append(f"❌ {entry} [{state}]")
        elif state in ("BUILDING", "INITIALIZING", "QUEUED"):
            building.append(f"🔨 {entry} [{state}]")
        else:
            unknown.append(f"❓ {entry} [{state}]")

    parts = []
    if failed:
        parts.append("**🚨 FAILED deployments:**\n" + "\n".join(failed))
    if building:
        parts.append("**🔨 Currently building:**\n" + "\n".join(building))
    if ok:
        parts.append(f"**✅ Healthy ({len(ok)} projects):** " + ", ".join(ok))
    if unknown:
        parts.append("**❓ No deployment yet:** " + ", ".join(unknown))

    if not failed and not building:
        return "✅ All deployments are healthy. No issues detected."

    return "\n\n".join(parts)


def _redeploy(args: dict) -> str:
    project = _require_project(args)
    target  = args.get("target", "production")
    # Fetch the latest deployment for this project
    data  = _get("/v6/deployments", params={"projectId": project, "limit": 1})
    depls = data.get("deployments", [])
    if not depls:
        return f"No existing deployments found for **{project}** to redeploy."
    latest  = depls[0]
    dep_id  = latest["uid"]
    dep_name = latest.get("name", project)
    # Vercel redeployment: POST /v13/deployments with deploymentId in the body
    body = {
        "deploymentId": dep_id,
        "name":         dep_name,
        "target":       target,
    }
    result  = _post("/v13/deployments", body)
    new_url = result.get("url", "pending")
    new_id  = result.get("id") or result.get("uid", "")
    return (
        f"✅ Redeploy triggered for **{project}** → `{target}`.\n"
        f"New deployment ID : `{new_id}`\n"
        f"URL               : https://{new_url}\n"
        f"Say 'Jarvis, check my Vercel deployments' to monitor progress."
    )


def _cancel_deployment(args: dict) -> str:
    dep_id = _require_deployment_id(args)
    try:
        _patch(f"/v12/deployments/{dep_id}/cancel")
        return f"✅ Deployment `{dep_id}` has been cancelled."
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status in (400, 409):
            # 400 = already in a terminal state; 409 = conflict
            try:
                msg = exc.response.json().get("error", {}).get("message", "already in a terminal state")
            except Exception:
                msg = "already complete, errored, or cancelled"
            return f"Cannot cancel `{dep_id}`: {msg}."
        raise


def _list_domains(args: dict) -> str:
    project = _require_project(args)
    data    = _get(f"/v9/projects/{project}/domains")
    domains = data.get("domains", [])
    if not domains:
        return f"No custom domains configured for **{project}**."
    lines = []
    for d in domains:
        verified = "✅ verified" if d.get("verified") else "⚠️ unverified"
        redirect = f" → {d['redirect']}" if d.get("redirect") else ""
        lines.append(f"• **{d['name']}** [{verified}]{redirect}")
    return f"**Domains** for {project} ({len(domains)}):\n\n" + "\n".join(lines)


def _list_env(args: dict) -> str:
    project = _require_project(args)
    data    = _get(f"/v9/projects/{project}/env")
    envs    = data.get("envs", [])
    if not envs:
        return f"No environment variables configured for **{project}**."
    lines = []
    for e in envs:
        targets = ", ".join(e.get("target", []))
        # Never expose values — show type and target only
        lines.append(
            f"• `{e['key']}` [{e.get('type', 'plain')}] → {targets}"
        )
    return f"**Environment variables** for {project} ({len(envs)}):\n\n" + "\n".join(lines)


def _add_env(args: dict) -> str:
    project = _require_project(args)
    key     = args.get("key", "").strip()
    value   = args.get("value", "").strip()
    target  = args.get("target", "production").split(",")
    target  = [t.strip() for t in target]
    env_type = args.get("env_type", "plain")
    if not key or not value:
        return "Please provide both 'key' and 'value' for the environment variable."
    body = {"key": key, "value": value, "type": env_type, "target": target}
    _post(f"/v9/projects/{project}/env", body)
    return f"✅ Environment variable `{key}` added to **{project}** (targets: {', '.join(target)})."


def _delete_env(args: dict) -> str:
    project = _require_project(args)
    key     = args.get("key", "").strip()
    if not key:
        return "Please provide the 'key' of the environment variable to delete."
    # Find the env var id by key
    data = _get(f"/v9/projects/{project}/env")
    envs = data.get("envs", [])
    match = [e for e in envs if e["key"] == key]
    if not match:
        return f"No environment variable named `{key}` found on **{project}**."
    env_id = match[0]["id"]
    _delete(f"/v9/projects/{project}/env/{env_id}")
    return f"✅ Environment variable `{key}` deleted from **{project}**."


def _deployment_summary(args: dict) -> str:
    """Quick health overview across all projects."""
    data     = _get("/v9/projects", params={"limit": 50})
    projects = data.get("projects", [])
    total = len(projects)
    if not total:
        return "No projects found."

    counts: dict[str, int] = {}
    for p in projects:
        depls = p.get("latestDeployments", [])
        state = depls[0].get("readyState", "NO_DEPLOY").upper() if depls else "NO_DEPLOY"
        counts[state] = counts.get(state, 0) + 1

    lines = [f"**Vercel Deployment Health** ({total} projects):\n"]
    for state, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {_state_icon(state)} {state}: {cnt}")

    # Flag any errors prominently
    if counts.get("ERROR", 0) > 0:
        lines.append(f"\n⚠️  **{counts['ERROR']} project(s) have failed builds.** "
                     "Say 'Jarvis, check Vercel failures' for details.")
    return "\n".join(lines)


# ── Validators ─────────────────────────────────────────────────────────────

def _require_project(args: dict) -> str:
    p = args.get("project", "").strip()
    if not p:
        raise ValueError(
            "Please specify a project name. "
            "For example: 'project': 'my-site'."
        )
    return p


def _require_deployment_id(args: dict) -> str:
    d = args.get("deployment_id", "").strip()
    if not d:
        raise ValueError(
            "Please provide a 'deployment_id'. "
            "You can get one by running 'list_deployments' first."
        )
    return d


# ── Dispatch ───────────────────────────────────────────────────────────────

_ACTIONS: dict[str, Any] = {
    "list_projects":      _list_projects,
    "project_info":       _project_info,
    "list_deployments":   _list_deployments,
    "deployment_info":    _deployment_info,
    "deployment_logs":    _deployment_logs,
    "check_failures":     _check_failures,
    "redeploy":           _redeploy,
    "cancel_deployment":  _cancel_deployment,
    "list_domains":       _list_domains,
    "list_env":           _list_env,
    "add_env":            _add_env,
    "delete_env":         _delete_env,
    "deployment_summary": _deployment_summary,
}


# ── Public entry point ─────────────────────────────────────────────────────

def vercel_manager(
    parameters: dict,
    player=None,
    speak=None,
) -> str:
    """
    Jarvis Vercel manager tool.

    Parameters
    ----------
    parameters : dict
        action         : str — which action to run
        project        : str — Vercel project name or ID
        deployment_id  : str — specific deployment UID
        + action-specific keys
    player : HUD ui object (optional)
    speak  : callable (optional)

    Returns
    -------
    str — result text for Jarvis to speak/display
    """
    action = parameters.get("action", "").strip().lower()
    if not action:
        return (
            "Please specify a Vercel action. Available actions:\n"
            + ", ".join(sorted(_ACTIONS.keys()))
        )

    handler = _ACTIONS.get(action)
    if handler is None:
        return (
            f"Unknown Vercel action: '{action}'. Available actions:\n"
            + ", ".join(sorted(_ACTIONS.keys()))
        )

    try:
        return handler(parameters)
    except ValueError as exc:
        return f"[Vercel] {exc}"
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        try:
            detail = exc.response.json().get("error", {}).get("message", str(exc))
        except Exception:
            detail = str(exc)
        return f"[Vercel] API error {status}: {detail}"
    except requests.ConnectionError:
        return "[Vercel] Cannot reach the Vercel API — check your internet connection."
    except RuntimeError as exc:
        return f"[Vercel] Configuration error: {exc}"
    except Exception as exc:
        return f"[Vercel] Unexpected error in '{action}': {exc}"
