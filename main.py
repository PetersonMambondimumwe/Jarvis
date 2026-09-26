# WakeWordListener is imported lazily only in desktop mode with microphone
WakeWordListener = None

# Headless / Docker display safety: mock GUI libraries if running without an active display
import sys
from unittest.mock import MagicMock
for _mod in ['pyautogui', 'pyperclip', 'mouse', 'keyboard']:
    try:
        _m = __import__(_mod)
        if _mod == 'pyautogui':
            _m.size()
    except Exception:
        sys.modules[_mod] = MagicMock()

import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────


import asyncio
import os
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import sounddevice as sd
from google import genai
from google.genai import types
# JarvisUI is imported lazily in main() so headless mode does not require PyQt6/X11
JarvisUI = None
from core.tool_registry import ToolRegistry
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
)

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.database_manager   import database_manager, db_cleanup
from actions.proactive         import ProactiveEngine
from actions.edith_agent        import edith_agent
from actions.hermes_agent       import hermes_agent
from actions.linkedin_agent     import linkedin_agent
from actions.github_manager     import github_manager
from actions.vercel_manager     import vercel_manager
from actions.antigravity_bridge import antigravity_bridge
from actions.leaf_ai_knowledge import leaf_ai_knowledge
from core.leaf_ai_client      import is_leaf_ai_query
from core.perception_engine     import PerceptionEngine
from core.task_manager          import TaskManager
from core.hermes_task_manager   import HermesTaskManager
from core.mcp_client            import MCPClientManager
from core.honcho_memory         import HonchoMemory


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
DEFAULT_LIVE_MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 512

def _get_api_key() -> str:
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        return env_key
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _get_live_model() -> str:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            configured = json.load(f).get("gemini_live_model", "").strip()
        return configured or DEFAULT_LIVE_MODEL
    except Exception:
        return DEFAULT_LIVE_MODEL


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web. Use for ANY question about current facts, events, prices, "
            "or topics — always prefer this over guessing. "
            "Modes: 'search' (default), 'news' (latest headlines on a topic), "
            "'research' (comprehensive answer), 'deep' (multi-stage exhaustive search & synthesis), "
            "'price' (product cost lookup), 'compare' (side-by-side comparison of items)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query or topic"},
                "mode":   {"type": "STRING", "description": "search | news | research | deep | price | compare"},
                "items":  {"type": "ARRAY",  "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | features"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": (
            "Sends a message to someone via WhatsApp, Telegram, or other platforms. "
            "Also sends emails via Power Automate when platform is 'email'. "
            "Use platform='email' whenever the user asks to send an email. "
            "Examples: 'email ruth@gmail.com saying get well soon', "
            "'send an email to john@example.com about the meeting'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {
                    "type": "STRING",
                    "description": "Recipient email address (for email) or contact name (for chat apps)"
                },
                "message_text": {"type": "STRING", "description": "The message body to send"},
                "platform":     {
                    "type": "STRING",
                    "description": (
                        "Delivery platform. Use 'email' to send via Power Automate. "
                        "Other options: WhatsApp, Telegram, Instagram, Signal, Discord, Messenger."
                    )
                },
                "name":        {"type": "STRING",  "description": "Recipient's first name for email greeting (e.g. 'Ruth'). Used in 'Dear [name],'"},
                "subject":     {"type": "STRING",  "description": "Email subject line. Required when platform is 'email'."},
                "sender_name": {"type": "STRING",  "description": "Signature name in the email. Defaults to 'Jarvis'."},
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "get_current_time",
        "description": (
            "Returns the current live date, time, day of the week, and timezone. "
            "Use this whenever the user asks what time it is, what today's date or day is, "
            "or when calculating times across timezones. "
            "Defaults to South African Standard Time (SAST, UTC+2)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "timezone_name": {
                    "type": "STRING",
                    "description": "Optional timezone name or city. Default is 'Africa/Johannesburg' (South African Standard Time)."
                }
            },
            "required": []
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when user says: close camera, stop camera, turn off camera, "
            "kamerayı kapat, kapat, creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "database_manager",
        "description": (
            "Manages the Neon PostgreSQL database with intelligent caching. Use for: listing tables, "
            "describing the database schema, getting table schemas, executing SQL queries "
            "(SELECT, INSERT, UPDATE, DELETE), and managing the query cache. Queries are automatically "
            "cached in Redis and conversation memory for 5 minutes. Essential for interacting with Project ASC data. "
            "For natural-language questions about stored Project ASC data, call this tool."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":     {"type": "STRING", "description": "query | list_tables | describe_database | get_schema | clear_cache | cache_stats"},
                "query":      {"type": "STRING", "description": "The SQL query to execute (query action)"},
                "table_name": {"type": "STRING", "description": "Table name for get_schema action"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, any web-based task. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use browser_control or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "edith_agent",
        "description": (
            "Delegates complex or specialized tasks to EDITH, the Base44 AI Agent. "
            "Use this when the user mentions EDITH, or when you need a specialized "
            "agent to handle backend projects, data orchestration, or persistent AI skills."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "delegate | verify. Use verify to test EDITH/Base44 connectivity."
                },
                "task": {"type": "STRING", "description": "The detailed task or message for EDITH"}
            },
            "required": ["task"]
        }
    },
    {
        "name": "github_manager",
        "description": (
            "Connects Jarvis to the user's GitHub account for repository inspection and queries. "
            "Note: Agent Hermes is the dedicated execution agent who holds full execution authority "
            "over GitHub repositories. All active tasks, code modifications, issues, pull requests, "
            "and branch operations should be delegated to hermes_agent. Use this tool only for quick "
            "read-only status queries."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "The GitHub action to perform. One of:\n"
                        "list_repos       — list repositories (scope: owner|all|starred)\n"
                        "repo_info        — detailed info about a repo\n"
                        "list_issues      — list issues (state: open|closed|all)\n"
                        "create_issue     — create a new issue (title, body, labels)\n"
                        "close_issue      — close an issue by number\n"
                        "list_prs         — list pull requests\n"
                        "create_pr        — open a pull request (head, base, title, body)\n"
                        "merge_pr         — merge a PR by number\n"
                        "list_branches    — list all branches\n"
                        "create_branch    — create a branch (branch, from_branch)\n"
                        "read_file        — read a file or directory from the repo (path, ref)\n"
                        "list_commits     — recent commits (branch, limit)\n"
                        "search_repos     — search GitHub by keyword (query)\n"
                        "user_info        — authenticated user profile\n"
                        "activity_summary — summary of recent GitHub activity"
                    )
                },
                "repo": {
                    "type": "STRING",
                    "description": "Repository name or 'owner/repo'. E.g. 'Mark-XLVIII' or 'torvalds/linux'."
                },
                "title":       {"type": "STRING",  "description": "Issue or PR title"},
                "body":        {"type": "STRING",  "description": "Issue or PR body text"},
                "number":      {"type": "INTEGER", "description": "Issue or PR number"},
                "state":       {"type": "STRING",  "description": "Filter state: open | closed | all"},
                "branch":      {"type": "STRING",  "description": "Branch name to create or target"},
                "from_branch": {"type": "STRING",  "description": "Source branch when creating a new branch"},
                "head":        {"type": "STRING",  "description": "Source branch for a pull request"},
                "base":        {"type": "STRING",  "description": "Target branch for a pull request"},
                "path":        {"type": "STRING",  "description": "File or directory path inside the repo"},
                "ref":         {"type": "STRING",  "description": "Branch/tag/SHA to read from (default: default branch)"},
                "query":       {"type": "STRING",  "description": "Search query for search_repos"},
                "scope":       {"type": "STRING",  "description": "For list_repos: owner | all | starred"},
                "limit":       {"type": "INTEGER", "description": "Max number of results to return"},
                "labels":      {"type": "STRING",  "description": "Comma-separated labels for create_issue"},
                "method":      {"type": "STRING",  "description": "Merge method for merge_pr: merge | squash | rebase"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "vercel_manager",
        "description": (
            "Connects Jarvis to the user's Vercel account for inspection and queries. "
            "Note: Agent Hermes is the dedicated execution agent who holds full execution authority "
            "over Vercel projects. All active tasks, deployments, redeployments, log investigations, "
            "and environment variable changes should be delegated to hermes_agent. Use this tool only "
            "for quick read-only status queries."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "The Vercel action to perform. One of:\n"
                        "list_projects      — list all Vercel projects with status\n"
                        "project_info       — detailed info for one project\n"
                        "list_deployments   — recent deployments (filter by project/state)\n"
                        "deployment_info    — status and metadata for a specific deployment\n"
                        "deployment_logs    — build/runtime logs for a deployment\n"
                        "check_failures     — scan all projects for failed builds\n"
                        "redeploy           — trigger a redeployment of the latest build\n"
                        "cancel_deployment  — cancel a queued or building deployment\n"
                        "list_domains       — list custom domains for a project\n"
                        "list_env           — list environment variables for a project\n"
                        "add_env            — add or update an environment variable\n"
                        "delete_env         — delete an environment variable\n"
                        "deployment_summary — health overview across all projects"
                    )
                },
                "project": {
                    "type": "STRING",
                    "description": "Vercel project name or ID. E.g. 'my-portfolio' or 'prj_xxx'."
                },
                "deployment_id": {
                    "type": "STRING",
                    "description": "Deployment UID. E.g. 'dpl_xxx'. Get from list_deployments."
                },
                "state":   {"type": "STRING",  "description": "Filter deployments by state: READY | ERROR | BUILDING | QUEUED | CANCELED"},
                "limit":   {"type": "INTEGER", "description": "Max number of deployments to return (default 10)"},
                "target":  {"type": "STRING",  "description": "Deployment target: production | preview (default: production)"},
                "key":     {"type": "STRING",  "description": "Environment variable key name"},
                "value":   {"type": "STRING",  "description": "Environment variable value"},
                "env_type":{"type": "STRING",  "description": "Env var type: plain | secret | system (default: plain)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Peterson, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Search Jarvis's Honcho long-term memory for relevant facts, prior "
            "conversations, decisions, and project history. Use this whenever the "
            "user asks what you remember or refers to something discussed previously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "The specific person, project, decision, or past topic to recall."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "hermes_agent",
        "description": (
            "Delegates tasks to Hermes, the dedicated execution agent with full access and "
            "execution authority over GitHub repositories and Vercel projects. Use Hermes for "
            "executing multi-step tasks, repository actions (issues, PRs, branches, commits, code "
            "changes), and Vercel operations (deployments, build logs, environment variables, "
            "domains, redeploying) while Jarvis remains available as the personal assistant. "
            "Also checks status, cancels tasks, and handles approvals."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "delegate | verify | status | cancel | approve | deny"
                },
                "task": {
                    "type": "STRING",
                    "description": "Complete, specific task for Hermes to execute (e.g. GitHub repo action, code change, or Vercel deployment/operation)."
                },
                "task_id": {
                    "type": "STRING",
                    "description": "Hermes task ID for status, cancel, approve, or deny."
                }
            },
            "required": []
        }
    },
    {
        "name": "linkedin_agent",
        "description": (
            "Manages Jarvis's authorized LinkedIn account. Checks connection status and prepares, "
            "publishes, or discards text-post drafts. Preparing never publishes. Publishing requires "
            "the user to explicitly approve the exact prepared draft first."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "status | prepare_post | publish_post | discard_post"
                },
                "content": {
                    "type": "STRING",
                    "description": "Exact post text for prepare_post. Maximum 3,000 characters."
                },
                "draft_id": {
                    "type": "STRING",
                    "description": "Prepared draft ID for publish_post or discard_post."
                },
                "confirmed": {
                    "type": "BOOLEAN",
                    "description": "True only after the user explicitly approves the exact draft."
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "memory_status",
        "description": (
            "Checks whether Jarvis's self-hosted Honcho long-term memory is "
            "configured, authenticated, and reachable."
        ),
        "parameters": {"type": "OBJECT", "properties": {}}
    },
    {
        "name": "antigravity_bridge",
        "description": (
            "Delegates complex coding, full-stack software development, code refactoring, bug fixing, "
            "test running, or multi-file programming tasks directly to Antigravity, your autonomous AI developer. "
            "Use this whenever the user asks to build features, fix code, inspect repository files, or execute development plans."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "delegate (default) | status | list",
                },
                "task": {
                    "type": "STRING",
                    "description": "Clear, detailed instruction of what code or feature Antigravity should build or fix.",
                },
                "task_id": {
                    "type": "STRING",
                    "description": "Task ID to inspect (for status action).",
                },
                "priority": {
                    "type": "STRING",
                    "description": "normal | high | urgent",
                },
                "project": {
                    "type": "STRING",
                    "description": "Project or repository name (optional).",
                },
            },
            "required": []
        }
    },
    {
        "name": "leaf_ai_knowledge",
        "description": (
            "Queries the dedicated Leaf AI knowledge source for University of Pretoria (UP) "
            "academic assistance, Academic Success Coaches (ASC), Department of Student Affairs, "
            "course modules, prerequisites, campus bus schedules, and questions specifically related "
            "to Leaf AI. Automatically route any Leaf AI or UP campus questions here, retrieve "
            "the response, and present it naturally in your voice as Jarvis without exposing Dify "
            "or internal integration details."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "The question, topic, or inquiry to look up in the Leaf AI knowledge source."
                }
            },
            "required": ["query"]
        }
    },
]

# --- Plugin system ---


class JarvisLive:

    def __init__(self, ui):
        self.ui             = ui
        self.headless       = getattr(ui, "__class__", None).__name__ == "HeadlessUI"
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self._pending_text_command = ""      # Text turns may not receive input transcription
        self._pending_text_command_id = ""
        self._command_done_event: asyncio.Event | None = None
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._perception       = PerceptionEngine(api_key=_get_api_key(), player=self.ui)
        self.task_manager     = TaskManager(player=self.ui, speak_callback=self.speak)
        self.hermes_task_manager = HermesTaskManager(
            player=self.ui,
            speak_callback=self.speak,
        )
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        # Initialize database caching layer
        self._db_cache_initialized = False
        # MCP Client Manager
        self._mcp = MCPClientManager()
        self._honcho = HonchoMemory.from_config(API_CONFIG_PATH)
        # Tool registry — populated by _register_tools()
        self._tool_registry = ToolRegistry()
        self._register_tools()

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        self._pending_text_command = text.strip()
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from core.time_util import get_sast_now, get_sast_time_str

        memory     = load_memory()
        # RAG: Use the last user speech to fetch relevant context
        last_speech = getattr(self, "_last_user_text", None)
        mem_str    = format_memory_for_prompt(memory, query=last_speech)
        sys_prompt = _load_system_prompt()

        now      = get_sast_now()
        time_str = get_sast_time_str("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str} (South African Standard Time / SAST, UTC+2).\n"
            f"Your default timezone is South African Standard Time (SAST, UTC+2).\n"
            f"Always answer time, date, or schedule questions using South African Standard Time (SAST).\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        parts = [time_ctx]
        if mem_str:
            parts.append(mem_str)
        honcho_context = self._honcho.prompt_context(last_speech or "")
        if honcho_context:
            parts.append(honcho_context)
        parts.append(sys_prompt)

        mcp_declarations = self._mcp.get_function_declarations() if hasattr(self, "_mcp") else []
        all_declarations = list(TOOL_DECLARATIONS) + mcp_declarations

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": all_declarations}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Charon"
                    )
                )
            ),
        )

    # ── Tool registration ─────────────────────────────────────────────────────────

    def _register_tools(self) -> None:
        """Register every Gemini tool with a handler lambda and per-tool timeout.

        Handlers are thin lambdas that close over self — they can safely
        reference self.ui, self.speak, self._perception, etc. which are all
        set before _register_tools() is called from __init__.
        """
        r  = self._tool_registry
        ui = self.ui

        r.register("open_app",
            lambda args: open_app(parameters=args, response=None, player=ui, perception=self._perception),
            timeout=15)

        r.register("web_search",
            lambda args: web_search_action(parameters=args, player=ui),
            timeout=45)

        r.register("weather_report",
            lambda args: weather_action(parameters=args, player=ui),
            timeout=15)

        r.register("browser_control",
            lambda args: browser_control(parameters=args, player=ui),
            timeout=60)

        r.register("database_manager",
            lambda args: database_manager(parameters=args, player=ui),
            timeout=30)

        r.register("file_controller",
            lambda args: file_controller(parameters=args, player=ui),
            timeout=20)

        r.register("send_message",
            lambda args: send_message(parameters=args, response=None, player=ui, session_memory=None),
            timeout=20)

        r.register("reminder",
            lambda args: reminder(parameters=args, response=None, player=ui),
            timeout=10)

        from core.time_util import handle_get_current_time
        r.register("get_current_time",
            lambda args: handle_get_current_time(timezone_name=args.get("timezone_name", "")),
            timeout=5)

        r.register("youtube_video",
            lambda args: youtube_video(parameters=args, response=None, player=ui),
            timeout=30)

        r.register("computer_settings",
            lambda args: computer_settings(parameters=args, response=None, player=ui),
            timeout=10)

        r.register("desktop_control",
            lambda args: desktop_control(parameters=args, player=ui),
            timeout=10)

        r.register("code_helper",
            lambda args: code_helper(parameters=args, player=ui, speak=self.speak),
            timeout=60)

        r.register("dev_agent",
            lambda args: dev_agent(parameters=args, player=ui, speak=self.speak),
            timeout=180)

        r.register("computer_control",
            lambda args: computer_control(parameters=args, player=ui, perception=self._perception),
            timeout=15)

        r.register("game_updater",
            lambda args: game_updater(parameters=args, player=ui, speak=self.speak),
            timeout=120)

        r.register("flight_finder",
            lambda args: flight_finder(parameters=args, player=ui),
            timeout=60)

        r.register("system_status",
            lambda args: get_system_status(),
            timeout=5)

        r.register("recall_memory",
            lambda args: self._honcho.recall(args.get("query", "")),
            timeout=15)

        r.register("memory_status",
            lambda args: self._honcho.status(),
            timeout=10)

        r.register("file_processor",
            lambda args: file_processor(
                parameters={**args, "file_path": args.get("file_path") or getattr(ui, "current_file", None)},
                player=ui,
                speak=self.speak,
            ),
            timeout=120)

        r.register("edith_agent",
            lambda args: edith_agent(
                parameters=args,
                player=ui,
                session_memory=getattr(self, "_session_memory", {}),
                task_manager=self.task_manager,
            ),
            timeout=180)

        r.register("hermes_agent",
            lambda args: hermes_agent(
                parameters=args,
                player=ui,
                task_manager=self.hermes_task_manager,
            ),
            timeout=30)

        r.register("linkedin_agent",
            lambda args: linkedin_agent(parameters=args),
            timeout=30)

        r.register("github_manager",
            lambda args: github_manager(
                parameters=args,
                player=ui,
                speak=self.speak,
            ),
            timeout=30)

        r.register("vercel_manager",
            lambda args: vercel_manager(
                parameters=args,
                player=ui,
                speak=self.speak,
            ),
            timeout=30)

        r.register("antigravity_bridge",
            lambda args: antigravity_bridge(
                parameters=args,
                player=ui,
                task_manager=self.task_manager,
                speak=self.speak,
            ),
            timeout=30)

        r.register("leaf_ai_knowledge",
            lambda args: leaf_ai_knowledge(
                parameters=args,
                player=ui,
            ),
            timeout=35)

    def _register_mcp_tools(self) -> None:
        """Register dynamically discovered MCP tools into ToolRegistry."""
        for decl in self._mcp.get_function_declarations():
            tool_name = decl["name"]

            def make_handler(name: str):
                def handler(args: dict) -> str:
                    if self._loop and self._loop.is_running():
                        future = asyncio.run_coroutine_threadsafe(
                            self._mcp.execute_tool(name, args),
                            self._loop
                        )
                        return future.result(timeout=60)
                    return asyncio.run(self._mcp.execute_tool(name, args))
                return handler

            self._tool_registry.register(
                tool_name,
                make_handler(tool_name),
                timeout=60.0
            )

    # ── Vision helper ────────────────────────────────────────────────────────

    async def _handle_screen_process(self, args: dict) -> str:
        """Vision capture with cooldown guard and camera/screen branching.

        Extracted from _execute_tool so the vision state machine is
        independently readable and testable.
        """
        import time as _t_mod
        _now      = _t_mod.monotonic()
        _cooldown = 4.0  # seconds — covers echo window after speaking ends

        if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
            _wait = max(0, _cooldown - (_now - self._vision_last_time))
            print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
            return "Vision is still processing the previous request. I will not call this again."

        self._vision_busy      = True
        self._vision_last_time = _now
        loop      = asyncio.get_event_loop()
        angle     = args.get("angle", "screen").lower()
        user_text = args.get("text", "What do you see?")

        if angle == "camera":
            img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
            self.ui.start_camera_stream()
            self._vision_cam_active = True
            print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
            _stall = "camera"
        else:
            img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
            print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
            if hasattr(self.ui, "show_camera_frame"):
                self.ui.show_camera_frame(img_b)
            _stall = "screen"

        self._pending_vision = (img_b, mime_t, user_text, angle)
        return (
            f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
            f"Immediately say ONE natural sentence in the user's language "
            f"(e.g. 'Looking at your {_stall} now, sir' / "
            f"'{'Kameraya' if _stall == 'camera' else 'Ekrana'} bakıyorum efendim'). "
            f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
        )

    # ── Tool dispatch ───────────────────────────────────────────────────────────

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        """Dispatch a Gemini function call to the appropriate handler.

        Special cases (save_memory, screen_process, close_camera,
        shutdown_jarvis, web_search) are handled inline because they need
        direct access to session state or have post-call side-effects.
        Everything else is delegated to the ToolRegistry.
        """
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        result = "Done."

        try:
            # ── save_memory: synchronous, must return silent flag ────────────
            if name == "save_memory":
                category = args.get("category", "notes")
                key      = args.get("key", "")
                value    = args.get("value", "")
                if key and value:
                    update_memory({category: {key: {"value": value}}})
                    print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
                    asyncio.create_task(asyncio.to_thread(
                        self._honcho.remember,
                        f"{category}/{key}: {value}",
                    ))
                if not self.ui.muted:
                    self.ui.set_state("LISTENING")
                return types.FunctionResponse(
                    id=fc.id, name=name,
                    response={"result": "ok", "silent": True}
                )

            # ── screen_process: manages vision state machine ─────────────────
            elif name == "screen_process":
                result = await self._handle_screen_process(args)

            # ── close_camera: direct UI call, no executor needed ────────────
            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            # ── shutdown_jarvis: OS-level exit ───────────────
            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                self.speak("Goodbye, sir.")
                def _shutdown():
                    import time as _t, os as _os
                    _t.sleep(1)
                    _os._exit(0)
                threading.Thread(target=_shutdown, daemon=True).start()

            # ── web_search: registry handles execution; mirror results to UI ─
            elif name == "web_search":
                result = await self._tool_registry.execute(name, args)
                _mode  = args.get("mode", "search")
                if result and not result.startswith("No results") and not result.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, result)

            # ── all other tools: registry with per-tool timeout ──────────────
            else:
                result = await self._tool_registry.execute(name, args)

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        # Start the task manager polling loop when Jarvis starts
        self.task_manager.start_polling()
        self.hermes_task_manager.start_polling()
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        if getattr(self, "headless", False):
            print("[JARVIS] 🎤 Headless mode active: Listening for remote audio stream...")
            while True:
                await asyncio.sleep(1.0)

        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if not jarvis_speaking and not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )

        try:
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=callback,
            ):
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~100 ms chunks to minimize network packet overhead
                            # (24000 Hz × 2 bytes/sample × 0.10 s = 4800 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 4800
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if getattr(sc, "interrupted", False):
                            self._interrupted = True
                            while not self.audio_in_queue.empty():
                                try:
                                    self.audio_in_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    break
                            if self._dashboard:
                                asyncio.create_task(self._dashboard.broadcast_audio_control("clear"))

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            self._last_user_text = _clean_transcript(sc.input_transcription.text)
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()
                            if self._command_done_event:
                                self._command_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                self._pending_text_command = ""
                                self._pending_text_command_id = ""
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if not full_in and self._pending_text_command:
                                full_in = self._pending_text_command
                            full_in_request_id = self._pending_text_command_id
                            self._pending_text_command = ""
                            self._pending_text_command_id = ""
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                if self._dashboard:
                                    from core.time_util import get_sast_now
                                    user_log = {
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": get_sast_now().isoformat(),
                                    }
                                    if full_in_request_id:
                                        user_log["request_id"] = full_in_request_id
                                    asyncio.create_task(self._dashboard.broadcast(user_log))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"Jarvis: {full_out}")
                                if self._dashboard:
                                    from core.time_util import get_sast_now
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": get_sast_now().isoformat(),
                                    }))
                            out_buf = []

                            if full_in or full_out:
                                asyncio.create_task(asyncio.to_thread(
                                    self._honcho.record_turn,
                                    full_in,
                                    full_out,
                                ))

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_client_content(
                                    turns={"parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until JARVIS finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        stream = None
        if not getattr(self, "headless", False):
            try:
                stream = sd.RawOutputStream(
                    samplerate=RECEIVE_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                )
                stream.start()
            except Exception as e:
                print(f"[JARVIS] Local soundcard unavailable: {e}")
                stream = None

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue
                self.set_speaking(True)

                # Broadcast audio chunk to phone dashboard clients
                if self._dashboard:
                    try:
                        asyncio.create_task(self._dashboard.broadcast_audio(chunk))
                    except Exception:
                        pass

                if stream:
                    try:
                        await asyncio.to_thread(stream.write, chunk)
                    except (RuntimeError, asyncio.CancelledError):
                        break   # executor shutting down — exit cleanly
                else:
                    # In headless server mode, pace audio broadcast slightly ahead of real time (70% of duration)
                    # so the client's jitter buffer stays comfortably filled and never starves on network jitter.
                    dur = len(chunk) / (RECEIVE_SAMPLE_RATE * 2)
                    await asyncio.sleep(dur * 0.70)
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            if stream:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass


    # ── System monitor ──────────────────────────────────────────────────────────
    # Startup briefing is in core/startup.py → send_startup_briefing()


    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if alert and self.session:
                try:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": alert}]},
                        turn_complete=True,
                    )
                except Exception as e:
                    print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory = await asyncio.to_thread(load_memory)
                prompt = self._proactive.build_prompt(memory)
                await self.session.send_client_content(
                    turns={"parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s -> phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming, silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    await asyncio.wait_for(self.out_queue.put(chunk), timeout=0.25)
                except (asyncio.QueueFull, asyncio.TimeoutError):
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # -- dashboard command relay -----------------------------------------------

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                command = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if isinstance(command, dict):
                    text = str(command.get("text") or "").strip()
                    request_id = str(command.get("request_id") or "").strip()
                else:
                    text = str(command or "").strip()
                    request_id = ""
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    self._pending_text_command = text.strip()
                    self._pending_text_command_id = request_id
                    command_done = self._command_done_event
                    if command_done:
                        command_done.clear()
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                    if command_done:
                        try:
                            await asyncio.wait_for(command_done.wait(), timeout=120)
                        except asyncio.TimeoutError:
                            print("[Dashboard] Text command timed out waiting for turn completion")
                else:
                    if is_leaf_ai_query(text):
                        self.ui.write_log(f"You: {text}")
                        if self._dashboard:
                            from core.time_util import get_sast_now
                            user_log = {
                                "type": "log", "speaker": "user",
                                "text": text,
                                "ts": get_sast_now().isoformat(),
                            }
                            if request_id:
                                user_log["request_id"] = request_id
                            asyncio.create_task(self._dashboard.broadcast(user_log))
                        leaf_resp = await asyncio.to_thread(
                            leaf_ai_knowledge,
                            {"query": text},
                            player=self.ui,
                        )
                        self.ui.write_log(f"Jarvis: {leaf_resp}")
                        if self._dashboard:
                            from core.time_util import get_sast_now
                            asyncio.create_task(self._dashboard.broadcast({
                                "type": "log", "speaker": "jarvis",
                                "text": leaf_resp,
                                "ts": get_sast_now().isoformat(),
                            }))
                        self.speak(leaf_resp)
                    else:
                        print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── Main loop ───────────────────────────────────────────────────────────
    # Full session lifecycle (connect, reconnect, backoff) lives in
    # core/session_manager.py → run_session_loop()

    async def run(self):
        self._loop = asyncio.get_event_loop()

        # Initialize MCP Client (discover servers and tools)
        try:
            await self._mcp.initialize()
            self._register_mcp_tools()
        except Exception as e:
            print(f"[MCP] ⚠️ Initialization error: {e}")

        # Start always-on Wake Word Listener (only in desktop mode with local mic)
        if not getattr(self, "headless", False):
            try:
                from core.wake_word import WakeWordListener
                self._wake_listener = WakeWordListener(
                    on_wake=lambda: self.ui.show_window(),
                    auto_launch_main=False,
                )
                self._wake_listener.start()
            except Exception as e:
                print(f"[WakeWord] ⚠️ Could not start wake listener: {e}")
                self._wake_listener = None
        else:
            self._wake_listener = None

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            asyncio.create_task(self._process_dashboard_commands())
            self._dashboard.set_wake_callback(lambda: self.ui.show_window())

            if getattr(self, "headless", False):
                if hasattr(self.ui, "dashboard_server"):
                    self.ui.dashboard_server = self._dashboard
                master_pin = os.environ.get("JARVIS_PIN", "JARVIS")
                print("=" * 64)
                print("  [+] JARVIS REMOTE WEB PORTAL READY")
                print(f"      Default PIN : {master_pin}")
                print(f"      Web Portal  : https://<SERVER_IP>:8000/login")
                print(f"      One-Click   : https://<SERVER_IP>:8000/auto-login?key={master_pin}")
                print("=" * 64)
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        from core.session_manager import run_session_loop
        await run_session_loop(self, _get_api_key, _get_live_model)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="JARVIS Voice Assistant")
    parser.add_argument("--headless", action="store_true", help="Run headlessly without GUI or local soundcard (for Docker/servers)")
    args, _ = parser.parse_known_args()

    is_headless = args.headless or (os.environ.get("HEADLESS") == "1") or (os.environ.get("DOCKER") == "1")

    if is_headless:
        print("=" * 64)
        print("  [+] JARVIS HEADLESS SERVER MODE (Docker / Server / Cloud)")
        print("  Connect your phone or browser at: http://0.0.0.0:8000")
        print("=" * 64)
        from core.headless_ui import HeadlessUI
        ui = HeadlessUI()
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        jarvis.headless = True
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n[!] Shutting down...")
    else:
        from ui import JarvisUI
        ui = JarvisUI("face.png")

        def runner():
            ui.wait_for_api_key()
            jarvis = JarvisLive(ui)
            jarvis.headless = False
            try:
                asyncio.run(jarvis.run())
            except KeyboardInterrupt:
                print("\n🔴 Shutting down...")
            finally:
                pass

        threading.Thread(target=runner, daemon=True).start()
        ui.root.mainloop()

if __name__ == "__main__":
    main()

