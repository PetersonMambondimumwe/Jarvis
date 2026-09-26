from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from core.hermes_client import HermesClient, HermesError, extract_run_output, load_hermes_config
from core.time_util import get_sast_now


class HermesTaskManager:
    TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}

    def __init__(self, player=None, speak_callback: Callable | None = None):
        self.player = player
        self.speak_callback = speak_callback
        self.task_file = Path(__file__).resolve().parent.parent / "memory" / "hermes_tasks.json"
        self.tasks: dict[str, dict[str, Any]] = {}
        self.polling_interval = 5
        self.background_task: asyncio.Task | None = None
        self._lock = threading.RLock()
        self._load_tasks()

    def _log(self, message: str) -> None:
        text = f"[HERMES] {message}"
        if self.player:
            try:
                self.player.write_log(text)
                return
            except Exception:
                pass
        print(text)

    def _load_tasks(self) -> None:
        try:
            data = json.loads(self.task_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.tasks = data
        except FileNotFoundError:
            self.tasks = {}
        except (json.JSONDecodeError, OSError) as exc:
            self._log(f"Could not load task history: {exc}")
            self.tasks = {}

    def _save_tasks(self) -> None:
        with self._lock:
            self.task_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.task_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.tasks, indent=2), encoding="utf-8")
            os.replace(temporary, self.task_file)

    def register(self, task: str, run_id: str) -> str:
        task_id = uuid.uuid4().hex[:8]
        with self._lock:
            self.tasks[task_id] = {
                "task": task,
                "run_id": run_id,
                "status": "running",
                "created_at": get_sast_now().isoformat(),
                "updated_at": get_sast_now().isoformat(),
                "result": None,
                "notified_status": None,
            }
            self._save_tasks()
        self._log(f"Background task {task_id} started.")
        return task_id

    def resolve(self, task_id: str) -> tuple[str, dict[str, Any]]:
        clean_id = task_id.strip()
        with self._lock:
            if clean_id in self.tasks:
                return clean_id, dict(self.tasks[clean_id])
            for local_id, task in self.tasks.items():
                if task.get("run_id") == clean_id:
                    return local_id, dict(task)
        raise HermesError(f"Hermes task {clean_id or '<missing>'} was not found")

    def mark_status(self, task_id: str, status: str, result: str | None = None) -> None:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            previous_status = task.get("status")
            task["status"] = status
            task["updated_at"] = get_sast_now().isoformat()
            if previous_status != status and status not in self.TERMINAL_STATUSES | {"waiting_for_approval"}:
                task["notified_status"] = None
            if result is not None:
                task["result"] = result
            self._save_tasks()

    def summary(self) -> str:
        with self._lock:
            active = [
                (task_id, task)
                for task_id, task in self.tasks.items()
                if task.get("status") not in self.TERMINAL_STATUSES
            ]
        if not active:
            return "Hermes has no active background tasks."
        details = ", ".join(
            f"{task_id} ({task.get('status', 'unknown')}): {str(task.get('task', ''))[:60]}"
            for task_id, task in active[:5]
        )
        return f"Hermes has {len(active)} active background task(s): {details}"

    async def _notify(self, task_id: str, status: str, result: str) -> None:
        with self._lock:
            task = self.tasks.get(task_id, {})
            if task.get("notified_status") == status:
                return
            task["notified_status"] = status
            self._save_tasks()

        description = str(task.get("task") or "background task")
        if status == "completed":
            report = f"Hermes completed task {task_id}: {result or 'Task completed.'}"
        elif status == "waiting_for_approval":
            report = (
                f"Hermes task {task_id} requires approval before it can continue. "
                "Ask me to approve or deny that Hermes task."
            )
        else:
            report = f"Hermes task {task_id} ended with status {status}: {result or 'No details returned.'}"

        self._log(report)
        if self.speak_callback:
            spoken = report if len(report) <= 500 else report[:497] + "..."
            try:
                outcome = self.speak_callback(spoken)
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception as exc:
                self._log(f"Could not speak task notification: {exc}")

    async def _poll_task(self, task_id: str, run_id: str, client: HermesClient) -> None:
        try:
            data = await asyncio.to_thread(client.get_run, run_id)
        except HermesError as exc:
            self._log(f"Task {task_id} status check failed: {exc}")
            return

        status = str(data.get("status") or "unknown").lower()
        result = extract_run_output(data)
        error = data.get("error")
        if not result and isinstance(error, str):
            result = error[:1000]
        elif not result and isinstance(error, dict):
            result = str(error.get("message") or error.get("code") or "Hermes task failed")[:1000]

        self.mark_status(task_id, status, result or None)
        if status in self.TERMINAL_STATUSES or status == "waiting_for_approval":
            await self._notify(task_id, status, result)

    async def _polling_loop(self) -> None:
        self._log("Background task monitoring started.")
        while True:
            try:
                with self._lock:
                    active = [
                        (task_id, str(task.get("run_id") or ""))
                        for task_id, task in self.tasks.items()
                        if task.get("status") not in self.TERMINAL_STATUSES
                        and task.get("run_id")
                    ]
                if active:
                    config = load_hermes_config()
                    await asyncio.gather(
                        *(
                            self._poll_task(task_id, run_id, HermesClient(config))
                            for task_id, run_id in active
                        )
                    )
                await asyncio.sleep(self.polling_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log(f"Background polling error: {exc}")
                await asyncio.sleep(self.polling_interval)

    def start_polling(self) -> None:
        if self.background_task is None or self.background_task.done():
            self.background_task = asyncio.create_task(self._polling_loop())

    def stop_polling(self) -> None:
        if self.background_task:
            self.background_task.cancel()
            self.background_task = None
