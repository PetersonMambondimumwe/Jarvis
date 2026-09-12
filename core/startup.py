"""
core/startup.py
───────────────
Two-phase startup briefing, extracted from JarvisLive.

Phase 1 — immediate greeting spoken in < 2 s (no tools, no fetch).
Phase 2 — news fetch via web_search, dispatched concurrently so it
           overlaps with the Phase 1 audio playback.

Public API
----------
    await send_startup_briefing(session, ui, load_memory_fn)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable


async def send_startup_briefing(
    session: Any,
    ui: Any,
    load_memory_fn: Callable[[], dict],
) -> None:
    """Fire the two-phase startup briefing once per process launch.

    Args:
        session:         Active Gemini Live session object.
        ui:              JarvisUI instance (used for write_log).
        load_memory_fn:  Zero-arg callable that returns the current memory dict.
    """
    await asyncio.sleep(0.3)
    if not session:
        return

    memory   = load_memory_fn()
    identity = memory.get("identity", {})

    def _val(k: str) -> str:
        entry = identity.get(k, {})
        return (entry.get("value", "") if isinstance(entry, dict) else str(entry)).strip()

    lang     = _val("language")
    name     = _val("name")
    time_str = datetime.now().strftime("%H:%M")

    # ── Phase 1: instant greeting ────────────────────────────────────────────
    lang_clause = f" Respond in {lang}." if lang else ""
    name_clause = f" Address the user as {name}." if name else ""
    p1 = (
        f"Greet the user, mention it is {time_str}, and say you are fetching "
        f"today's news headlines now. "
        f"One short sentence only. Do not call any tools.{lang_clause}{name_clause}"
    )

    await session.send_client_content(
        turns={"parts": [{"text": p1}]},
        turn_complete=True,
    )
    ui.write_log("SYS: Briefing phase 1 (greeting) sent.")

    # ── Phase 2: news fetch — runs concurrently with phase-1 audio ──────────
    async def _guarded_news() -> None:
        try:
            await _briefing_news_phase(session, ui, lang)
        except Exception as exc:
            print(f"[Briefing] Phase 2 error: {exc}")
            ui.write_log(f"SYS: Briefing news phase failed: {exc}")

    asyncio.create_task(_guarded_news())


async def _briefing_news_phase(session: Any, ui: Any, lang: str) -> None:
    """Inject the news prompt ~1.5 s after Phase 1 so it overlaps with greeting audio.

    The 1.5 s gap is long enough for Gemini to finish generating Phase 1 audio
    on its side (turn_complete) while the greeting is still playing locally.
    """
    lang_str = f" Respond in {lang}." if lang else ""
    await asyncio.sleep(1.5)

    if not session:
        return

    p2 = (
        "[BRIEFING] Call web_search with mode='news' and query='top world news today' "
        "to find actual recent news articles with real event headlines (not just website names). "
        "After the search, say ONE specific news event from the results in one sentence, "
        f"then say the full list is displayed on screen.{lang_str}"
    )

    await session.send_client_content(
        turns={"parts": [{"text": p2}]},
        turn_complete=True,
    )
    ui.write_log("SYS: Briefing phase 2 (news) sent.")
