import asyncio
import json
import time
import uuid
from datetime import datetime
from core.time_util import get_sast_now as _get_now
from pathlib import Path
from typing import Any, Dict, Optional

import requests

# We'll use absolute imports to avoid issues when running from main.py
try:
    from memory.memory_manager import load_memory
    from actions.edith_agent import (
        _load_edith_config, _headers, _json_or_text, _handle_http_error, 
        _latest_assistant_text, EdithConfig, EdithConfigError,
        _post_agent_message, _build_payload, _response_text
    )
except ImportError:
    # Fallback for direct module testing
    import sys
    sys.path.append(str(Path(__file__).parent.parent))
    from memory.memory_manager import load_memory
    from actions.edith_agent import (
        _load_edith_config, _headers, _json_or_text, _handle_http_error, 
        _latest_assistant_text, EdithConfig, EdithConfigError,
        _post_agent_message, _build_payload, _response_text
    )


class TaskManager:
    def __init__(self, player=None, speak_callback=None):
        self.player = player
        self.speak_callback = speak_callback # Callback to JarvisLive.speak()
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.task_file = Path(__file__).parent.parent / "memory" / "edith_tasks.json"
        self._load_tasks()
        self.polling_interval = 15  # seconds
        self.max_retries = 3
        self.background_task: Optional[asyncio.Task] = None

    def _log(self, message: str) -> None:
        if self.player:
            try:
                self.player.write_log(f"[TASK_MANAGER] {message}")
                return
            except Exception:
                pass
        print(f"[TASK_MANAGER] {message}")

    def _load_tasks(self):
        if self.task_file.exists():
            try:
                with open(self.task_file, "r", encoding="utf-8") as f:
                    self.tasks = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                self._log(f"Error loading tasks: {e}")
                self.tasks = {}
        else:
            self.tasks = {}

    def _save_tasks(self):
        try:
            self.task_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.task_file, "w", encoding="utf-8") as f:
                json.dump(self.tasks, f, indent=2)
        except IOError as e:
            self._log(f"Error saving tasks: {e}")

    async def _poll_edith_status(self, task_id: str, edith_config: EdithConfig):
        task_info = self.tasks.get(task_id)
        if not task_info or task_info["status"] != "pending":
            return

        conversation_id = task_info["conversation_id"]
        try:
            url = f"{edith_config.agent_api_base}/conversations/{conversation_id}"
            headers = _headers(edith_config)
            
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, 
                lambda: requests.get(url, headers=headers, timeout=edith_config.timeout)
            )

            if response.status_code >= 400:
                error_msg = _handle_http_error(response)
                self._log(f"Task {task_id} failed: {error_msg}")
                task_info["status"] = "failed"
                task_info["result"] = error_msg
                self._save_tasks()
                await self._notify_user(task_id, "failed", error_msg)
                return

            data = _json_or_text(response)
            latest_message = _latest_assistant_text(data)

            # Check if EDITH has provided a new response compared to when the task was delegated
            # If last_update is None, it means we haven't seen any messages yet. We wait for a new one.
            if latest_message and latest_message != task_info.get("last_update"):
                if task_info.get("last_update") is None:
                    # This is the first message we're seeing, likely the initial response
                    # We store it but don't mark as completed yet
                    task_info["last_update"] = latest_message
                    self._save_tasks()
                else:
                    # We've seen a message before, and this is a new one, meaning EDITH has followed up
                    task_info["last_update"] = latest_message
                    task_info["status"] = "completed"
                    task_info["result"] = latest_message
                    task_info["completed_at"] = _get_now().isoformat()
                    self._save_tasks()
                    self._log(f"Task {task_id} completed.")
                    await self._notify_user(task_id, "completed", latest_message)

        except Exception as e:
            task_info["retries"] = task_info.get("retries", 0) + 1
            self._log(f"Retry {task_info['retries']} for task {task_id}: {e}")
            if task_info["retries"] >= self.max_retries:
                task_info["status"] = "failed"
                task_info["result"] = f"Connection error: {e}"
                self._save_tasks()
                await self._notify_user(task_id, "failed", task_info["result"])
            else:
                # Exponential backoff for this specific task
                backoff_time = self.polling_interval * (2 ** (task_info["retries"] - 1))
                task_info["next_poll_time"] = time.time() + backoff_time
                self._save_tasks()

    async def _notify_user(self, task_id: str, status: str, message: str):
        """Notifies the user via Jarvis's voice or UI."""
        summary = f"Sir, EDITH has finished the task: '{self.tasks[task_id]['task_description'][:30]}...'. "
        if status == "completed":
            report = f"{summary} She reports: {message}"
        else:
            report = f"{summary} Unfortunately, it failed: {message}"
        
        self._log(f"Notification: {report}")
        
        # If we have a speak callback (JarvisLive.speak), use it
        try:
            if self.speak_callback:
                if asyncio.iscoroutinefunction(self.speak_callback):
                    await self.speak_callback(report)
                else:
                    self.speak_callback(report)
            elif self.player:
                # Fallback to UI log if voice is not available
                self.player.write_log(f"[EDITH REPORT] {report}")
        except Exception as e:
            self._log(f"Error delivering notification: {e}")

    async def _run_polling_loop(self):
        self._log("Background polling loop started.")
        while True:
            try:
                current_time = time.time()
                active_tasks = [
                    tid for tid, info in self.tasks.items() 
                    if info["status"] == "pending" and current_time >= info.get("next_poll_time", 0)
                ]
                if active_tasks:
                    try:
                        edith_config = _load_edith_config()
                        for task_id in active_tasks:
                            await self._poll_edith_status(task_id, edith_config)
                    except EdithConfigError as e:
                        self._log(f"Config error in polling loop: {e}")
            except asyncio.CancelledError:
                self._log("Polling loop cancelled.")
                break
            except Exception as e:
                self._log(f"Polling loop error: {e}")
            
            await asyncio.sleep(self.polling_interval)

    def start_polling(self):
        if self.background_task is None or self.background_task.done():
            self.background_task = asyncio.create_task(self._run_polling_loop())

    def stop_polling(self):
        if self.background_task:
            self.background_task.cancel()
            self.background_task = None

    def delegate_task(self, task_description: str, conversation_id: str, initial_message: str = ""):
        """Register a delegated EDITH task for async polling.

        Args:
            task_description: Human-readable description of the task.
            conversation_id: The Base44 conversation ID returned at delegation time.
            initial_message: The first assistant message already received during delegation,
                             used as the baseline so the poller only fires when a *new*
                             message appears (avoids false-positive completion on the
                             acknowledgement reply).
        """
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {
            "task_description": task_description,
            "conversation_id": conversation_id,
            "status": "pending",
            "created_at": _get_now().isoformat(),
            "retries": 0,
            # Seed last_update with the initial response so the poller waits
            # for a genuinely new message before marking the task completed.
            "last_update": initial_message or None,
            "result": None,
            "next_poll_time": 0,
        }
        self._save_tasks()
        self._log(f"New task registered: {task_id} (conv: {conversation_id})")
        self.start_polling()
        return task_id

    def delegate_task_async(
        self,
        task_description: str,
        conversation_id: str,
        edith_config: EdithConfig,
        task: str,
        session_memory: dict | None = None
    ) -> str:
        """Register a delegated EDITH task and start background message posting."""
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {
            "task_description": task_description,
            "conversation_id": conversation_id,
            "status": "pending",
            "created_at": _get_now().isoformat(),
            "retries": 0,
            "last_update": None,
            "result": None,
            # Block regular polling for this task until the background poster
            # completes the initial message submission.
            "next_poll_time": time.time() + 999999,
        }
        self._save_tasks()
        self._log(f"New async task registered: {task_id} (conv: {conversation_id})")

        # Capture the running event loop to schedule the background coroutine
        loop = getattr(self, "loop", None)
        if not loop:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()
            self.loop = loop

        # Schedule the worker coroutine safely depending on if we are inside the loop thread
        try:
            running_loop = asyncio.get_running_loop()
            if running_loop == loop:
                loop.create_task(
                    self._background_delegate_worker(task_id, edith_config, task, session_memory)
                )
            else:
                asyncio.run_coroutine_threadsafe(
                    self._background_delegate_worker(task_id, edith_config, task, session_memory),
                    loop
                )
        except RuntimeError:
            asyncio.run_coroutine_threadsafe(
                self._background_delegate_worker(task_id, edith_config, task, session_memory),
                loop
            )
        self.start_polling()
        return task_id

    async def _background_delegate_worker(
        self,
        task_id: str,
        edith_config: EdithConfig,
        task: str,
        session_memory: dict | None
    ) -> None:
        task_info = self.tasks.get(task_id)
        if not task_info:
            return

        conversation_id = task_info["conversation_id"]
        loop = asyncio.get_running_loop()

        try:
            self._log(f"Starting background delegation for task {task_id}...")
            if edith_config.agent_api_base:
                response = await loop.run_in_executor(
                    None,
                    lambda: _post_agent_message(edith_config, conversation_id, task)
                )
                if response.status_code >= 400:
                    error_msg = _handle_http_error(response)
                    task_info["status"] = "failed"
                    task_info["result"] = error_msg
                    self._save_tasks()
                    await self._notify_user(task_id, "failed", error_msg)
                    return

                data = _json_or_text(response)
                reply = _latest_assistant_text(data)

                if reply:
                    task_info["status"] = "completed"
                    task_info["result"] = reply
                    task_info["completed_at"] = _get_now().isoformat()
                    task_info["last_update"] = reply
                    self._save_tasks()
                    self._log(f"Async task {task_id} completed immediately.")
                    await self._notify_user(task_id, "completed", reply)
                else:
                    # Seed the last update baseline with the initial agent reply,
                    # and schedule the next standard poll for 15s from now.
                    task_info["last_update"] = "acknowledged"
                    task_info["next_poll_time"] = time.time() + self.polling_interval
                    self._save_tasks()
                    self._log(f"Async post complete for task {task_id}. Polling enabled.")
            else:
                # Webhook direct endpoint
                response = await loop.run_in_executor(
                    None,
                    lambda: requests.post(
                        edith_config.endpoint,
                        headers=_headers(edith_config),
                        json=_build_payload("delegate", task, session_memory),
                        timeout=edith_config.timeout
                    )
                )
                if response.status_code >= 400:
                    error_msg = _handle_http_error(response)
                    task_info["status"] = "failed"
                    task_info["result"] = error_msg
                    self._save_tasks()
                    await self._notify_user(task_id, "failed", error_msg)
                    return

                text = _response_text(response)
                task_info["status"] = "completed"
                task_info["result"] = text
                task_info["completed_at"] = _get_now().isoformat()
                self._save_tasks()
                await self._notify_user(task_id, "completed", text)

        except Exception as e:
            task_info["status"] = "failed"
            task_info["result"] = f"Failed to post task: {e}"
            self._save_tasks()
            await self._notify_user(task_id, "failed", task_info["result"])

    def get_summary(self) -> str:
        pending = [t["task_description"] for t in self.tasks.values() if t["status"] == "pending"]
        if not pending:
            return "No active background tasks."
        return f"Currently monitoring {len(pending)} tasks: " + ", ".join(pending[:2])
