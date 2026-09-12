"""
actions/antigravity_bridge.py
─────────────────────────────
Antigravity Bridge for JARVIS.

Enables JARVIS to delegate complex software engineering, code refactoring,
debugging, testing, and multi-file development tasks directly to Antigravity.

Workflow:
  1. JARVIS receives a voice/text request for code changes or technical tasks.
  2. JARVIS calls `antigravity_bridge(action='delegate', task='...')`.
  3. The bridge writes a structured JSON task into `.antigravity_bridge/inbox/`.
  4. The bridge registers a background watcher with TaskManager.
  5. JARVIS responds immediately: "I've tasked Antigravity with [task]. I'll alert you as soon as it is done."
  6. When Antigravity completes the task and writes a receipt to `.antigravity_bridge/outbox/`,
     JARVIS proactively announces the completion and results over voice/UI.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
BRIDGE_DIR = BASE_DIR / ".antigravity_bridge"
INBOX_DIR = BRIDGE_DIR / "inbox"
OUTBOX_DIR = BRIDGE_DIR / "outbox"
ARCHIVE_DIR = BRIDGE_DIR / "archive"


def _ensure_dirs() -> None:
    for d in (INBOX_DIR, OUTBOX_DIR, ARCHIVE_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ── Bridge Functions ────────────────────────────────────────────────────────

def delegate_task(
    task_description: str,
    priority: str = "normal",
    project: str = "",
    player=None,
    task_manager=None,
    speak: Optional[Callable[[str], None]] = None,
) -> str:
    """Create a new task in Antigravity's inbox and register a background completion watcher."""
    _ensure_dirs()
    
    if not task_description or not task_description.strip():
        return "Please specify what you would like Antigravity to do."

    now = datetime.datetime.now()
    task_id = f"ag_task_{now.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"

    task_payload = {
        "task_id": task_id,
        "created_at": now.isoformat(),
        "priority": priority.lower().strip(),
        "project": project.strip() or BASE_DIR.name,
        "workspace_root": str(BASE_DIR),
        "status": "pending",
        "instruction": task_description.strip(),
    }

    task_file = INBOX_DIR / f"{task_id}.json"
    task_file.write_text(json.dumps(task_payload, indent=2), encoding="utf-8")

    print(f"[AntigravityBridge] -> Task created: {task_id} -> '{task_description[:60]}...'")
    if player:
        player.write_log(f"[Antigravity] Task delegated: {task_description[:50]}")

    # Register background watcher with TaskManager if available
    if task_manager:
        async def _poll_antigravity_completion() -> tuple[bool, str]:
            out_file = OUTBOX_DIR / f"{task_id}.json"
            if out_file.exists():
                try:
                    res_data = json.loads(out_file.read_text(encoding="utf-8"))
                    status = res_data.get("status", "completed")
                    summary = res_data.get("summary", "Task completed.")
                    files = res_data.get("files_changed", [])
                    files_str = f" ({len(files)} file(s) modified)" if files else ""
                    return True, f"Sir, Antigravity has completed the task: {summary}{files_str}."
                except Exception as e:
                    return True, f"Sir, Antigravity finished the task with notice: {e}"
            return False, ""

        task_manager.register_agent_task(
            task_id=task_id,
            description=f"Antigravity: {task_description[:45]}",
            agent_name="Antigravity",
            poll_coroutine_fn=_poll_antigravity_completion,
            interval=3.0,
            timeout=1800.0,
        )

    short_desc = task_description.strip()
    if len(short_desc) > 80:
        short_desc = short_desc[:77] + "..."

    return (
        f"I've tasked Antigravity with: \"{short_desc}\" (Task ID: `{task_id}`). "
        "I'll alert you as soon as the code is written and verified."
    )


def check_task_status(task_id: str) -> str:
    """Check the status of a specific Antigravity task."""
    _ensure_dirs()
    task_id = task_id.strip()
    if not task_id:
        return "Please provide a task ID to check."

    out_file = OUTBOX_DIR / f"{task_id}.json"
    in_file = INBOX_DIR / f"{task_id}.json"

    if out_file.exists():
        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
            status = data.get("status", "completed").upper()
            summary = data.get("summary", "No summary provided.")
            files = data.get("files_changed", [])
            files_str = "\n- " + "\n- ".join(files) if files else " None"
            return (
                f"Task `{task_id}` is **{status}**.\n"
                f"Summary: {summary}\n"
                f"Files Changed:{files_str}"
            )
        except Exception as e:
            return f"Error reading task result: {e}"

    if in_file.exists():
        try:
            data = json.loads(in_file.read_text(encoding="utf-8"))
            created = data.get("created_at", "N/A")
            instr = data.get("instruction", "N/A")
            return f"Task `{task_id}` is **PENDING** in Antigravity's queue (Created: {created}).\nInstruction: {instr}"
        except Exception as e:
            return f"Error reading task file: {e}"

    return f"Task `{task_id}` was not found in Antigravity's inbox or outbox."


def list_tasks(limit: int = 5) -> str:
    """List recent tasks in Antigravity's inbox and outbox."""
    _ensure_dirs()
    
    inbox_files = sorted(INBOX_DIR.glob("*.json"), key=os.path.getmtime, reverse=True)
    outbox_files = sorted(OUTBOX_DIR.glob("*.json"), key=os.path.getmtime, reverse=True)

    if not inbox_files and not outbox_files:
        return "There are no active or recent Antigravity tasks."

    lines = ["=== Antigravity Bridge Tasks ==="]

    if inbox_files:
        lines.append(f"\n[Pending] ({len(inbox_files)}):")
        for f in inbox_files[:limit]:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                lines.append(f"- `{d.get('task_id')}`: {d.get('instruction', '')[:50]}")
            except Exception:
                lines.append(f"- `{f.stem}`")

    if outbox_files:
        lines.append(f"\n[Completed] ({len(outbox_files)}):")
        for f in outbox_files[:limit]:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                lines.append(f"- `{d.get('task_id')}` [{d.get('status', 'done')}]: {d.get('summary', '')[:50]}")
            except Exception:
                lines.append(f"- `{f.stem}`")

    return "\n".join(lines)


# ── Action Dispatcher ──────────────────────────────────────────────────────

def antigravity_bridge(
    parameters: dict,
    player=None,
    task_manager=None,
    speak=None,
) -> str:
    """Central action entrypoint for JARVIS."""
    params = parameters or {}
    action = params.get("action", "delegate").lower().strip()

    if action in ("delegate", "create", "assign", "run"):
        task_desc = params.get("task") or params.get("instruction") or params.get("query") or ""
        priority = params.get("priority", "normal")
        project = params.get("project", "")
        return delegate_task(
            task_description=task_desc,
            priority=priority,
            project=project,
            player=player,
            task_manager=task_manager,
            speak=speak,
        )

    elif action in ("status", "check", "get"):
        task_id = params.get("task_id", "")
        return check_task_status(task_id)

    elif action in ("list", "history"):
        limit = int(params.get("limit", 5))
        return list_tasks(limit=limit)

    else:
        return f"Unknown Antigravity Bridge action: '{action}'. Valid actions: delegate, status, list."
