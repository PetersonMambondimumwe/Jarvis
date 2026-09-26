"""
core/session_manager.py
───────────────────────
Gemini Live session lifecycle — connect, TaskGroup, reconnect, backoff.

Extracted from JarvisLive.run() so the outer reconnection loop is testable
and auditable independently of the audio and tool-dispatch logic.

Public API
----------
    await run_session_loop(jarvis, get_api_key_fn, get_live_model_fn)
"""

from __future__ import annotations

import asyncio
import traceback
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    # Avoid circular import at runtime; used only for type hints.
    from main import JarvisLive


async def run_session_loop(
    jarvis: "JarvisLive",
    get_api_key_fn: Callable[[], str],
    get_live_model_fn: Callable[[], str],
) -> None:
    """Outer reconnection loop with exponential backoff.

    Responsibilities:
    - Create a fresh Gemini client on every reconnect.
    - Reset all transient session state flags.
    - Manage the asyncio.TaskGroup lifetime.
    - Apply exponential backoff on network errors.
    - Handle API key failures with UI re-configuration flow.

    Args:
        jarvis:            The JarvisLive instance (owns all audio + tool state).
        get_api_key_fn:    Returns the current Gemini API key string.
        get_live_model_fn: Returns the configured live model identifier.
    """
    from google import genai
    from core.startup import send_startup_briefing
    from memory.memory_manager import load_memory
    from actions.database_manager import db_cleanup

    while True:
        try:
            live_model = get_live_model_fn()
            print(f"[JARVIS] Connecting with {live_model}...")
            jarvis.ui.set_state("THINKING")
            if jarvis._dashboard:
                await jarvis._dashboard.broadcast({"type": "status", "state": "starting"})
            config = jarvis._build_config()

            # Fresh client on every reconnect — avoids stale HTTP session state.
            client = genai.Client(
                api_key=get_api_key_fn(),
                http_options={"api_version": "v1beta"},
            )

            async with (
                client.aio.live.connect(model=live_model, config=config) as session,
                asyncio.TaskGroup() as tg,
            ):
                # ── Session state ────────────────────────────────────────────
                jarvis.session          = session
                jarvis.audio_in_queue   = asyncio.Queue()
                jarvis.out_queue        = asyncio.Queue(maxsize=200)
                jarvis._turn_done_event = asyncio.Event()
                jarvis._command_done_event = asyncio.Event()

                # Reset all transient flags that must not carry over from a
                # crashed or expired session.
                jarvis._pending_vision       = None
                jarvis._vision_cam_active    = False
                jarvis._vision_close_pending = False
                jarvis._vision_busy          = False
                jarvis._vision_last_time     = 0.0
                jarvis._interrupted          = False
                jarvis._pending_text_command = ""
                jarvis._pending_text_command_id = ""
                jarvis._db_cache_initialized = True

                print("[JARVIS] Connected.")
                jarvis.ui.set_state("LISTENING")
                jarvis.ui.write_log("SYS: JARVIS online.")

                if jarvis._dashboard:
                    await jarvis._dashboard.broadcast({"type": "status", "state": "active"})

                # ── Background tasks ─────────────────────────────────────────
                tg.create_task(jarvis._send_realtime())
                tg.create_task(jarvis._listen_audio())
                tg.create_task(jarvis._receive_audio())
                tg.create_task(jarvis._play_audio())
                tg.create_task(jarvis._run_system_monitor())
                tg.create_task(jarvis._run_proactive_mode())
                if not getattr(jarvis, "headless", False):
                    jarvis._perception.start()

                if jarvis._dashboard:
                    tg.create_task(jarvis._relay_phone_audio())

                # Morning briefing — fires once per process launch.
                if not jarvis._briefing_sent:
                    jarvis._briefing_sent = True
                    tg.create_task(
                        send_startup_briefing(session, jarvis.ui, load_memory)
                    )

        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise
        except BaseException as exc:
            # Catches both Exception and BaseExceptionGroup (Python 3.11+).
            # TaskGroup raises BaseExceptionGroup when tasks are cancelled
            # externally — `except Exception` would miss it, letting the
            # exception escape the loop and causing shutdown.
            err_str = str(exc)
            print(f"[JARVIS] Error ({type(exc).__name__}): {exc}")
            traceback.print_exc()

            # Invalid API key — stop hammering the API; prompt re-configuration.
            if "API key not valid" in err_str or "1007" in err_str:
                jarvis.ui.write_log("ERR: API key invalid — please re-enter your key.")
                jarvis.ui.set_state("SLEEPING")
                jarvis.ui.prompt_reconfig()
                while not jarvis.ui._win._ready:
                    await asyncio.sleep(1)
                print("[JARVIS] New API key saved — reconnecting...")
                jarvis._conn_backoff = 3
                continue

            # Network / timeout — exponential backoff with status message.
            is_net_err = any(k in err_str for k in (
                "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                "ConnectionRefusedError", "OSError", "Cannot connect",
            ))
            if is_net_err:
                jarvis._conn_backoff = min(getattr(jarvis, "_conn_backoff", 3) * 2, 60)
                jarvis.ui.write_log(
                    f"NET: Bağlantı kurulamadı — {jarvis._conn_backoff}s sonra tekrar deneniyor. "
                    "(VPN gerekiyor olabilir)"
                )
            else:
                jarvis._conn_backoff = 3

        finally:
            jarvis.session = None
            jarvis.task_manager.stop_polling()
            jarvis.hermes_task_manager.stop_polling()
            if getattr(jarvis, "_db_cache_initialized", False):
                db_cleanup()

        jarvis.set_speaking(False)
        jarvis.ui.set_state("SLEEPING")

        if jarvis._dashboard:
            await jarvis._dashboard.broadcast({"type": "status", "state": "sleeping"})

        delay = getattr(jarvis, "_conn_backoff", 3)
        print(f"[JARVIS] Reconnecting in {delay}s...")
        await asyncio.sleep(delay)
