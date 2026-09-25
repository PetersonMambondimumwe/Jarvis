"""
core/time_util.py
─────────────────
JARVIS Timezone & Clock Utility.
Standardizes all time, date, greeting, and reminder operations on
South African Standard Time (SAST, UTC+2 / Africa/Johannesburg).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

# 1. Set environment and libc timezone if supported
DEFAULT_TIMEZONE = os.environ.get("JARVIS_TIMEZONE", "Africa/Johannesburg")
os.environ.setdefault("TZ", DEFAULT_TIMEZONE)
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass

# 2. Resilient Timezone Object (ZoneInfo with fixed UTC+2 fallback)
try:
    from zoneinfo import ZoneInfo
    SAST_ZONE = ZoneInfo(DEFAULT_TIMEZONE)
except Exception:
    SAST_ZONE = timezone(timedelta(hours=2), name="SAST")


def get_sast_now() -> datetime:
    """Return the current datetime in South African Standard Time (SAST, UTC+2)."""
    try:
        return datetime.now(SAST_ZONE)
    except Exception:
        return datetime.now(timezone(timedelta(hours=2), name="SAST"))


def get_sast_time_str(fmt: str = "%A, %B %d, %Y — %I:%M %p") -> str:
    """Return current date & time formatted string in South African Standard Time."""
    return get_sast_now().strftime(fmt)


def get_sast_short_time() -> str:
    """Return current 24-hour time HH:MM in South African Standard Time."""
    return get_sast_now().strftime("%H:%M")


def get_sast_today() -> str:
    """Return current date YYYY-MM-DD in South African Standard Time."""
    return get_sast_now().strftime("%Y-%m-%d")


def handle_get_current_time(timezone_name: str = "") -> str:
    """Tool handler returning the exact live time and date.

    Defaults to South African Standard Time (SAST, UTC+2).
    """
    tz_str = (timezone_name or "").strip()
    target_zone = SAST_ZONE
    zone_label = "South African Standard Time (SAST, UTC+2)"

    if tz_str:
        low = tz_str.lower()
        if any(k in low for k in ("south africa", "johannesburg", "sast", "pretoria", "cape town", "durban")):
            target_zone = SAST_ZONE
            zone_label = "South African Standard Time (SAST, UTC+2)"
        elif any(k in low for k in ("utc", "gmt", "zulu")):
            target_zone = timezone.utc
            zone_label = "UTC (Coordinated Universal Time)"
        else:
            try:
                from zoneinfo import ZoneInfo
                target_zone = ZoneInfo(tz_str)
                zone_label = tz_str
            except Exception:
                target_zone = SAST_ZONE
                zone_label = f"South African Standard Time (could not resolve '{tz_str}')"

    now = datetime.now(target_zone)
    return (
        f"Current Time: {now.strftime('%I:%M:%S %p')} ({now.strftime('%H:%M')})\n"
        f"Date: {now.strftime('%A, %B %d, %Y')}\n"
        f"Timezone: {zone_label}\n"
        f"Day of Week: {now.strftime('%A')}"
    )
