"""
core/headless_ui.py
───────────────────
Headless UI Adapter for JARVIS.

Enables JARVIS to run in headless environments (Docker containers, Linux cloud servers,
background daemons) without requiring PyQt6, X11/Wayland display servers, or local monitor.

All logs, state transitions, and content notifications are:
  1. Printed cleanly to stdout for Docker logs (`docker logs jarvis`).
  2. Broadcasted in real time over WebSockets to the web dashboard (for smartphones & browsers).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE = CONFIG_DIR / "api_keys.json"


class HeadlessUI:
    """Drop-in headless replacement for JarvisUI."""

    def __init__(self):
        self.muted: bool = False
        self.state: str = "SLEEPING"
        self.on_text_command: Optional[Callable[[str], None]] = None
        self.on_remote_clicked: Optional[Callable[[], None]] = None
        self.on_interrupt: Optional[Callable[[], None]] = None
        self.dashboard_server = None
        self._ready: bool = self._check_config()

        print("[HeadlessUI] Initialized headless console adapter.")

    def _check_config(self) -> bool:
        if os.environ.get("GEMINI_API_KEY"):
            return True
        if not API_FILE.exists():
            return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key"))
        except Exception:
            return False

    def wait_for_api_key(self) -> None:
        """Ensure an API key is configured before running."""
        while not self._check_config():
            print("[HeadlessUI] ⚠️ No valid Gemini API key found in config/api_keys.json.")
            print("[HeadlessUI] Waiting for configuration (check config/api_keys.json)...")
            time.sleep(3.0)
        self._ready = True

    def set_state(self, state: str) -> None:
        self.state = state
        print(f"[JARVIS State] -> {state}")
        if self.dashboard_server and hasattr(self.dashboard_server, "broadcast"):
            try:
                import asyncio
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                if loop and loop.is_running():
                    loop.create_task(self.dashboard_server.broadcast({
                        "type": "status",
                        "state": "active" if state in ("LISTENING", "SPEAKING", "THINKING") else "sleeping"
                    }))
            except Exception:
                pass

    def write_log(self, text: str) -> None:
        clean_text = text.encode("ascii", "replace").decode("ascii")
        print(f"[JARVIS Log] {clean_text}")
        if self.dashboard_server and hasattr(self.dashboard_server, "broadcast"):
            try:
                import asyncio
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                if loop and loop.is_running():
                    # Conversational dialogue (Jarvis / User / Web echo) is already
                    # broadcast directly by main.py with proper speaker keys and timestamps.
                    # HeadlessUI only relays non-dialogue system/diagnostic notifications.
                    if text.startswith("Jarvis:") or text.startswith("You:") or text.startswith("[Web]:"):
                        return
                    loop.create_task(self.dashboard_server.broadcast({
                        "type": "sys",
                        "text": text,
                    }))
            except Exception:
                pass

    def append_log(self, text: str) -> None:
        self.write_log(text)

    def show_content(self, title: str, text: str) -> None:
        print(f"\n--- {title} ---\n{text}\n-------------------")
        if self.dashboard_server and hasattr(self.dashboard_server, "broadcast"):
            try:
                import asyncio
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                if loop and loop.is_running():
                    loop.create_task(self.dashboard_server.broadcast({
                        "type": "sys",
                        "text": f"[{title}]\n{text[:500]}",
                    }))
            except Exception:
                pass

    def notify_phone_connected(self) -> None:
        print("[HeadlessUI] 📱 Remote Phone/Client connected successfully.")

    def prompt_reconfig(self) -> None:
        print("[HeadlessUI] ⚠️ Invalid API key or configuration error. Please update config/api_keys.json.")

    def show_camera_frame(self, img_bytes: bytes) -> None:
        """No-op in headless mode."""
        pass

    def start_camera_stream(self) -> None:
        """No-op in headless mode."""
        pass

    def stop_camera_stream(self) -> None:
        """No-op in headless mode."""
        pass

    def show_window(self) -> None:
        """In headless mode, un-mutes and marks state active."""
        self.muted = False
        self.set_state("LISTENING")
        print("[HeadlessUI] Voice wake triggered -> JARVIS listening.")

    def start_speaking(self) -> None:
        self.set_state("SPEAKING")

    def stop_speaking(self) -> None:
        if not self.muted:
            self.set_state("LISTENING")
