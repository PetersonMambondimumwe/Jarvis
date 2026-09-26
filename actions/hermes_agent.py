from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from core.hermes_client import HermesClient, HermesError, extract_run_output, load_hermes_config

if TYPE_CHECKING:
    from core.hermes_task_manager import HermesTaskManager


def _client() -> HermesClient:
    return HermesClient(load_hermes_config())


def hermes_agent(
    parameters: dict,
    player=None,
    task_manager: "HermesTaskManager | None" = None,
    **kwargs,
) -> str:
    params = parameters or {}
    action = str(params.get("action") or "delegate").strip().lower()
    if action in {"check", "health", "test"}:
        action = "verify"
    if action in {"stop", "abort"}:
        action = "cancel"

    try:
        client = _client()

        if action == "verify":
            status = client.verify()
            if not status["run_submission"]:
                return "Hermes is reachable, but its background Runs API is unavailable."
            return (
                f"Hermes is connected and {status['health']}. "
                f"Background task execution is available through {status['model']}."
            )

        if action == "status":
            task_id = str(params.get("task_id") or "").strip()
            if not task_id:
                return task_manager.summary() if task_manager else "Hermes task tracking is unavailable."
            if not task_manager:
                return "Hermes task tracking is unavailable."
            local_id, task = task_manager.resolve(task_id)
            data = client.get_run(str(task["run_id"]))
            status = str(data.get("status") or task.get("status") or "unknown")
            result = extract_run_output(data)
            suffix = f" Result: {result}" if result else ""
            return f"Hermes task {local_id} is {status}.{suffix}"

        if action == "cancel":
            task_id = str(params.get("task_id") or "").strip()
            if not task_id or not task_manager:
                return "Specify the Hermes task ID to cancel."
            local_id, task = task_manager.resolve(task_id)
            response = client.stop_run(str(task["run_id"]))
            status = str(response.get("status") or "stopping")
            task_manager.mark_status(local_id, status)
            return f"Hermes task {local_id} is {status}."

        if action in {"approve", "deny"}:
            task_id = str(params.get("task_id") or "").strip()
            if not task_id or not task_manager:
                return "Specify the Hermes task ID to approve or deny."
            local_id, task = task_manager.resolve(task_id)
            choice = "deny" if action == "deny" else "once"
            client.resolve_approval(str(task["run_id"]), choice)
            task_manager.mark_status(local_id, "running" if choice == "once" else "cancelled")
            return f"Hermes task {local_id} was {'approved once' if choice == 'once' else 'denied'}."

        if action != "delegate":
            return "Hermes supports delegate, verify, status, cancel, approve, and deny."

        task = str(params.get("task") or params.get("message") or "").strip()
        if not task:
            return "Specify the task Hermes should execute."
        if not task_manager:
            return "Hermes background task tracking is unavailable."

        request_id = uuid.uuid4().hex
        run = client.start_run(task, request_id)
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            raise HermesError("Hermes accepted the request without returning a run ID")
        task_id = task_manager.register(task, run_id)
        if player:
            try:
                player.write_log(f"[HERMES] Delegated background task {task_id}.")
            except Exception:
                pass
        return f"I've assigned Hermes task {task_id}. It will continue in the background and I'll report when it finishes."
    except HermesError as exc:
        return f"Hermes could not complete that request: {exc}."
    except Exception:
        return "Hermes integration failed safely because of an unexpected internal error."
