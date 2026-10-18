"""
core/ytmusic_client.py
──────────────────────
YouTube Music search, queue builder, audio stream resolver, and real-time
WebSocket broadcaster for JARVIS.

Supports:
  1. Searching YouTube Music songs & audio tracks via `ytmusicapi` (if installed)
     with a zero-dependency YouTube / YouTube Music JSON scraper fallback.
  2. Building a multi-track playback queue (prioritizing official audio / topic
     tracks alongside music videos so embedded playback rarely hits label blocks).
  3. Resolving direct audio stream URLs via `yt-dlp` (if installed) for HTML5
     <audio> fallback playback.
  4. Broadcasting playback & control events (`play`, `pause`, `resume`, `next`,
     `previous`, `stop`, `volume`, `open_app`) to all connected Jarvis PWA
     clients via DashboardServer or localhost HTTP relay.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Optional
from urllib.parse import quote_plus

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _format_duration(seconds: int | str | None) -> str:
    if seconds is None:
        return ""
    if isinstance(seconds, str):
        if ":" in seconds:
            return seconds.strip()
        if not seconds.strip().isdigit():
            return ""
        seconds = int(seconds.strip())
    try:
        secs = int(seconds)
        if secs <= 0:
            return ""
        mins, rem = divmod(secs, 60)
        hrs, mins = divmod(mins, 60)
        if hrs > 0:
            return f"{hrs}:{mins:02d}:{rem:02d}"
        return f"{mins}:{rem:02d}"
    except Exception:
        return ""


def _build_track_dict(
    video_id: str,
    title: str,
    artist: str = "YouTube Music",
    album: str = "",
    duration: str = "",
    thumbnail: str = "",
) -> dict[str, str]:
    vid = (video_id or "").strip()
    thumb = thumbnail or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return {
        "video_id": vid,
        "title": (title or "Unknown Track").strip(),
        "artist": (artist or "YouTube Music").strip(),
        "album": (album or "").strip(),
        "duration": (duration or "").strip(),
        "thumbnail": thumb,
        "ytmusic_url": f"https://music.youtube.com/watch?v={vid}",
        "watch_url": f"https://www.youtube.com/watch?v={vid}",
    }


def _search_via_ytmusicapi(query: str, limit: int = 8) -> list[dict[str, str]]:
    """Search YouTube Music using ytmusicapi if installed (songs + videos)."""
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        return []

    tracks: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        ytm = YTMusic()
        for search_filter in ("songs", "videos"):
            results = ytm.search(query, filter=search_filter, limit=limit)
            for item in results or []:
                if not isinstance(item, dict):
                    continue
                vid = (item.get("videoId") or "").strip()
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                title = (item.get("title") or "").strip()
                artists_list = item.get("artists") or []
                artist = ", ".join(
                    a.get("name", "").strip()
                    for a in artists_list
                    if isinstance(a, dict) and a.get("name")
                ) or "YouTube Music"
                album_obj = item.get("album")
                album = album_obj.get("name", "") if isinstance(album_obj, dict) else ""
                duration = (item.get("duration") or "").strip()
                thumbs = item.get("thumbnails") or []
                thumb = ""
                if isinstance(thumbs, list) and thumbs:
                    last_t = thumbs[-1]
                    if isinstance(last_t, dict):
                        thumb = last_t.get("url") or ""
                tracks.append(
                    _build_track_dict(
                        video_id=vid,
                        title=title,
                        artist=artist,
                        album=album,
                        duration=duration,
                        thumbnail=thumb,
                    )
                )
                if len(tracks) >= limit:
                    return tracks
    except Exception as exc:
        print(f"[YTMusic] ytmusicapi search fallback triggered: {exc}")
    return tracks


def _parse_yt_initial_data(html_text: str, limit: int = 8) -> list[dict[str, str]]:
    """Extract rich videoRenderer items (videoId, title, channel, length) from YouTube search HTML."""
    tracks: list[dict[str, str]] = []
    seen: set[str] = set()

    # Try parsing ytInitialData JSON block first
    m = re.search(r"var\s+ytInitialData\s*=\s*(\{.+?\});\s*</script>", html_text, re.DOTALL)
    if not m:
        m = re.search(r"ytInitialData\s*=\s*(\{.+?\});", html_text, re.DOTALL)

    if m:
        try:
            data = json.loads(m.group(1))
            contents = (
                data.get("contents", {})
                .get("twoColumnSearchResultsRenderer", {})
                .get("primaryContents", {})
                .get("sectionListRenderer", {})
                .get("contents", [])
            )
            for section in contents:
                items = section.get("itemSectionRenderer", {}).get("contents", [])
                for item in items:
                    vr = item.get("videoRenderer")
                    if not isinstance(vr, dict):
                        continue
                    vid = (vr.get("videoId") or "").strip()
                    if not vid or len(vid) != 11 or vid in seen:
                        continue
                    # Skip Shorts
                    nav_url = (
                        vr.get("navigationEndpoint", {})
                        .get("commandMetadata", {})
                        .get("webCommandMetadata", {})
                        .get("url", "")
                    )
                    if "/shorts/" in nav_url:
                        continue

                    title_runs = vr.get("title", {}).get("runs", [])
                    title = "".join(r.get("text", "") for r in title_runs if isinstance(r, dict)).strip()
                    if not title:
                        continue

                    owner_runs = (
                        vr.get("ownerText", {}).get("runs", [])
                        or vr.get("longBylineText", {}).get("runs", [])
                    )
                    artist = "".join(r.get("text", "") for r in owner_runs if isinstance(r, dict)).strip()
                    artist = re.sub(r"\s*-\s*Topic$", "", artist, flags=re.IGNORECASE).strip()

                    duration = vr.get("lengthText", {}).get("simpleText", "").strip()
                    # Skip live streams without duration or ultra-long 3+ hour streams unless requested
                    seen.add(vid)
                    tracks.append(
                        _build_track_dict(
                            video_id=vid,
                            title=title,
                            artist=artist or "YouTube Music",
                            duration=duration,
                        )
                    )
                    if len(tracks) >= limit:
                        return tracks
        except Exception:
            pass

    # Regex fallback if ytInitialData structure changed
    video_ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html_text)
    titles = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"\}\]', html_text)
    owners = re.findall(r'"ownerText":\{"runs":\[\{"text":"([^"]+)"', html_text)

    for idx, vid in enumerate(video_ids):
        if vid in seen or f"/shorts/{vid}" in html_text:
            continue
        seen.add(vid)
        title = titles[idx] if idx < len(titles) else f"Track {len(tracks) + 1}"
        artist = owners[idx] if idx < len(owners) else "YouTube Music"
        artist = re.sub(r"\s*-\s*Topic$", "", artist, flags=re.IGNORECASE).strip()
        tracks.append(
            _build_track_dict(
                video_id=vid,
                title=title,
                artist=artist,
            )
        )
        if len(tracks) >= limit:
            break

    return tracks


def search_ytmusic(query: str, limit: int = 8) -> list[dict[str, str]]:
    """Search YouTube Music for songs/tracks matching `query`.

    Prioritizes clean audio/song versions first so embedded & background playback
    in the Jarvis PWA works reliably without music-video embed blocks.
    """
    clean_q = (query or "").strip()
    if not clean_q:
        return []

    limit = max(1, min(int(limit or 8), 15))

    # 1. Try ytmusicapi if installed
    tracks = _search_via_ytmusicapi(clean_q, limit=limit)
    seen_ids = {t["video_id"] for t in tracks}

    # 2. Search YouTube with official audio / topic query first + standard query
    # Audio/Topic tracks rarely have IFrame embed restrictions (unlike VEVO music videos)
    search_queries = [f"{clean_q} official audio", clean_q]
    for sq in search_queries:
        if len(tracks) >= limit:
            break
        url = f"https://www.youtube.com/results?search_query={quote_plus(sq)}&sp=EgIQAQ%3D%3D"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                scraped = _parse_yt_initial_data(resp.text, limit=limit)
                for item in scraped:
                    vid = item["video_id"]
                    if vid not in seen_ids:
                        seen_ids.add(vid)
                        tracks.append(item)
                        if len(tracks) >= limit:
                            break
        except Exception as exc:
            print(f"[YTMusic] Search scrape error for '{sq}': {exc}")

    return tracks[:limit]


def resolve_audio_stream_url(video_id: str) -> Optional[str]:
    """Use yt-dlp (if installed) to extract a direct audio stream URL for HTML5 <audio> playback."""
    vid = re.sub(r"[^A-Za-z0-9_-]", "", (video_id or "").strip())
    if len(vid) != 11:
        return None
    try:
        import yt_dlp
    except ImportError:
        return None

    url = f"https://music.youtube.com/watch?v={vid}"
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if isinstance(info, dict):
                direct_url = info.get("url")
                if direct_url:
                    return str(direct_url)
    except Exception as exc:
        print(f"[YTMusic] yt-dlp stream extraction failed for {vid}: {exc}")
    return None


def broadcast_music_event(payload: dict[str, Any], player: Any = None) -> bool:
    """Broadcast a music event (`type: 'music'`) to all connected Jarvis PWA clients.

    Works both when called inside the main Jarvis process (via `player.dashboard_server`)
    and when called from an external MCP server subprocess (via localhost HTTP relay).
    """
    event = {"type": "music", **payload}
    delivered = False

    # 1. Direct in-process broadcast if player.dashboard_server is attached
    dash = getattr(player, "dashboard_server", None) if player is not None else None
    if dash is not None and hasattr(dash, "broadcast_music"):
        try:
            dash.broadcast_music(event)
            delivered = True
        except Exception as exc:
            print(f"[YTMusic] Direct dashboard broadcast failed: {exc}")
    elif dash is not None and hasattr(dash, "broadcast"):
        try:
            loop = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
            if loop and loop.is_running():
                loop.create_task(dash.broadcast(event))
                delivered = True
        except Exception:
            pass

    if delivered:
        return True

    # 2. Localhost HTTP relay (used by MCP subprocess or when player is None)
    port = int(os.environ.get("PORT", "8000") or 8000)
    for proto in ("http", "https"):
        try:
            resp = requests.post(
                f"{proto}://127.0.0.1:{port}/api/music/command",
                json=event,
                timeout=3.0,
                verify=False,
            )
            if resp.status_code == 200:
                return True
        except Exception:
            continue

    return False
