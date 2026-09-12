"""
core/tool_registry.py
─────────────────────
Centralised tool dispatcher for JarvisLive.

Each action registers a callable and a per-tool timeout (seconds).
When _execute_tool() dispatches a Gemini function call it calls
registry.execute(name, args), which:

  1. Runs the handler in a thread-pool executor — keeps the async event
     loop free for audio I/O while the tool does blocking work.
  2. Wraps the executor future in asyncio.wait_for() with the tool's
     hard ceiling — a hung game_updater or dev_agent can never block
     the audio stream indefinitely.
  3. Returns a string result, or a clean error/timeout message — never
     raises, so the Gemini session is never disrupted by tool failures.

Adding a new action requires exactly ONE call to registry.register();
the dispatch loop in _execute_tool needs zero changes.
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any, Callable, Dict, Tuple


_Entry = Tuple[Callable[[dict], Any], float]   # (handler, timeout_seconds)


class ToolRegistry:
    """Maps Gemini tool names → (handler callable, timeout seconds).

    Usage
    -----
    registry = ToolRegistry()
    registry.register("open_app", lambda args: open_app(args), timeout=15)
    result = await registry.execute("open_app", {"app_name": "Chrome"})
    """

    def __init__(self) -> None:
        self._tools: Dict[str, _Entry] = {}

    # ── Registration ─────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        handler: Callable[[dict], Any],
        timeout: float = 30.0,
    ) -> None:
        """Register a tool handler.

        Args:
            name:    Gemini tool name — must match TOOL_DECLARATIONS exactly.
            handler: Callable(args: dict) → str | None.
                     Executed in a ThreadPoolExecutor (blocking I/O is fine).
            timeout: Hard ceiling in seconds.  asyncio.TimeoutError is caught
                     internally and returned as a descriptive error string so
                     the Gemini session continues uninterrupted.
        """
        self._tools[name] = (handler, timeout)

    def is_registered(self, name: str) -> bool:
        """Return True if *name* has a registered handler."""
        return name in self._tools

    # ── Dispatch ─────────────────────────────────────────────────────────────

    async def execute(self, name: str, args: dict) -> str:
        """Dispatch *name* with *args*.

        Returns a string result to send back to Gemini.
        Never raises — all exceptions and timeouts become descriptive strings.
        """
        if name not in self._tools:
            return f"Unknown tool: {name}"

        handler, timeout = self._tools[name]
        loop = asyncio.get_event_loop()

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: handler(args)),
                timeout=timeout,
            )
            return result or "Done."

        except asyncio.TimeoutError:
            msg = (
                f"Tool '{name}' did not complete within {timeout:.0f} s "
                "and was cancelled to keep JARVIS responsive."
            )
            print(f"[Registry] ⏱️  {msg}")
            return msg

        except Exception as exc:
            msg = f"Tool '{name}' raised an error: {exc}"
            print(f"[Registry] ❌  {msg}")
            traceback.print_exc()
            return msg
