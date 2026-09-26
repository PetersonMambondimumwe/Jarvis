import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests as _requests
    _REQUESTS = True
except ImportError:
    _REQUESTS = False

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE    = 0.06
    _PYAUTOGUI = True
except Exception:
    pyautogui = None
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _get_config() -> dict:
    try:
        return json.loads(
            (_base_dir() / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
    except Exception:
        return {}

def _get_os() -> str:
    return _get_config().get("os_system", "windows").lower()


def _require_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")


def _paste_text(text: str) -> None:
    _require_pyautogui()

    os_name = _get_os()
    paste_hotkey = ("command", "v") if os_name == "mac" else ("ctrl", "v")

    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.15)
        pyautogui.hotkey(*paste_hotkey)
        time.sleep(0.1)
    else:
        pyautogui.write(text, interval=0.03)


def _clear_and_paste(text: str) -> None:
    _require_pyautogui()
    os_name = _get_os()
    select_all = ("command", "a") if os_name == "mac" else ("ctrl", "a")
    pyautogui.hotkey(*select_all)
    time.sleep(0.1)
    pyautogui.press("delete")
    time.sleep(0.1)
    _paste_text(text)

def _open_app(app_name: str) -> bool:
    _require_pyautogui()
    os_name = _get_os()

    try:
        if os_name == "windows":
            pyautogui.press("win")
            time.sleep(0.5)
            _paste_text(app_name)
            time.sleep(0.6)
            pyautogui.press("enter")
            time.sleep(2.5)
            return True

        elif os_name == "mac":
            result = subprocess.run(
                ["open", "-a", app_name],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                result = subprocess.run(
                    ["open", "-a", f"{app_name}.app"],
                    capture_output=True, text=True, timeout=10,
                )
            time.sleep(2.5)
            return result.returncode == 0

        else: 
            launched = False
            for launcher in [
                ["gtk-launch", app_name.lower()],
                [app_name.lower()],
            ]:
                try:
                    subprocess.Popen(
                        launcher,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    launched = True
                    break
                except FileNotFoundError:
                    continue
            time.sleep(2.5)
            return launched

    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open {app_name}: {e}")
        return False


def _open_browser_url(url: str) -> bool:
    import webbrowser
    try:
        webbrowser.open(url)
        time.sleep(4.0) 
        return True
    except Exception as e:
        print(f"[SendMessage] ⚠️ Could not open browser: {e}")
        return False

def _search_in_app(query: str) -> None:
    _require_pyautogui()
    os_name = _get_os()
    search_hotkey = ("command", "f") if os_name == "mac" else ("ctrl", "f")

    pyautogui.hotkey(*search_hotkey)
    time.sleep(0.5)
    _clear_and_paste(query)
    time.sleep(1.0)

def _desktop_send(app_name: str, receiver: str, message: str) -> str:
    if not _open_app(app_name):
        return f"Could not open {app_name}."

    time.sleep(1.0)
    _search_in_app(receiver)
    pyautogui.press("enter")
    time.sleep(0.8)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)
    return f"Message sent to {receiver} via {app_name}."

def _parse_recipients(receiver: str) -> list[tuple[str, str]]:
    import re
    lines = [line.strip() for line in re.split(r"[\n;,]+", receiver) if line.strip()]
    results = []
    for line in lines:
        match = re.search(r"^(.*?)\s*[-:–—]\s*(\+?[\d\s().-]{7,25})$", line)
        if match:
            results.append((match.group(1).strip(), match.group(2).strip()))
            continue
        digits = re.sub(r"[^\d]", "", line)
        if len(digits) >= 7:
            words = line.split()
            phone_part = words[-1] if len(re.sub(r"[^\d]", "", words[-1])) >= 7 else line
            name_part = " ".join(words[:-1]) if phone_part != line else ""
            results.append((name_part, phone_part))
        else:
            results.append(("", line))
    return results if results else [("", receiver)]

def _send_whatsapp(receiver: str, message: str) -> str:
    recipients = _parse_recipients(receiver)
    try:
        from core.whatsapp_client import WhatsAppClient, load_whatsapp_config, WhatsAppError
        config = load_whatsapp_config()
        client = WhatsAppClient(config)

        successes = []
        failures = []

        for name, phone in recipients:
            target_text = message
            if name:
                target_text = target_text.replace("[name]", name).replace("[Name]", name)
            try:
                res = client.send_message(phone, target_text)
                clean_num = res.get("recipient", phone)
                label = f"{name} ({clean_num})" if name else clean_num
                successes.append(label)
            except Exception as e:
                label = f"{name} ({phone})" if name else phone
                failures.append(f"{label}: {e}")

        if not failures and successes:
            return f"WhatsApp message successfully sent to {len(successes)} recipient(s): {', '.join(successes)}."
        elif successes and failures:
            return f"WhatsApp partially sent: {len(successes)} succeeded ({', '.join(successes)}), {len(failures)} failed ({'; '.join(failures)})."
        elif failures:
            return f"WhatsApp message delivery failed: {'; '.join(failures)}"
        return "No valid recipients found."
    except Exception as exc:
        from core.whatsapp_client import WhatsAppError
        if not isinstance(exc, WhatsAppError) or "credentials" not in str(exc).lower():
            return f"WhatsApp message delivery failed: {exc}"

        # If Cloud API credentials are not configured, fall back to desktop automation if available
        if _PYAUTOGUI and not os.environ.get("HEADLESS"):
            return _desktop_send("WhatsApp", receiver, message)
        return "WhatsApp credentials (PHONE_NUMBER_ID and WHATSAPP_TOKEN) are not configured."

def _send_telegram(receiver: str, message: str) -> str:
    return _desktop_send("Telegram", receiver, message)

def _send_signal(receiver: str, message: str) -> str:
    return _desktop_send("Signal", receiver, message)


def _send_discord(receiver: str, message: str) -> str:
    return _desktop_send("Discord", receiver, message)


def _send_instagram(receiver: str, message: str) -> str:
    _require_pyautogui()

    if not _open_browser_url("https://www.instagram.com/direct/new/"):
        return "Could not open Instagram in browser."

    _paste_text(receiver)
    time.sleep(1.5)

    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")   
    time.sleep(0.4)

    for _ in range(4):
        pyautogui.press("tab")
        time.sleep(0.15)
    pyautogui.press("enter")
    time.sleep(2.0)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)

    return f"Message sent to {receiver} via Instagram."


def _send_messenger(receiver: str, message: str) -> str:
    _require_pyautogui()

    if not _open_browser_url("https://www.messenger.com/"):
        return "Could not open Messenger in browser."


    _search_in_app(receiver)
    time.sleep(0.5)
    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(1.0)

    _paste_text(message)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)

    return f"Message sent to {receiver} via Messenger."

_PLATFORM_MAP = [
    ({"whatsapp", "wp", "wapp"},              _send_whatsapp),
    ({"telegram", "tg"},                      _send_telegram),
    ({"instagram", "ig", "insta"},            _send_instagram),
    ({"signal"},                               _send_signal),
    ({"discord"},                              _send_discord),
    ({"messenger", "facebook", "fb"},         _send_messenger),
]


# ── Email via Power Automate ───────────────────────────────────────────────

_EMAIL_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Message from Jarvis</title>
<style>
  body {{ margin: 0; padding: 0; background-color: #f2f4f7;
          font-family: 'Segoe UI', Arial, sans-serif; }}
  .email-wrapper {{ width: 100%; padding: 40px 0; }}
  .email-container {{ max-width: 600px; margin: 0 auto;
      background-color: #ffffff; border-radius: 12px;
      overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }}
  .header {{ background: linear-gradient(135deg, #1f2937, #111827);
      padding: 32px 40px; text-align: center; }}
  .header h1 {{ color: #ffffff; font-size: 22px; margin: 0;
      letter-spacing: 0.5px; }}
  .header p {{ color: #9ca3af; font-size: 13px; margin: 6px 0 0; }}
  .body-content {{ padding: 36px 40px; }}
  .greeting {{ font-size: 16px; color: #111827; margin-bottom: 20px; }}
  .message {{ font-size: 15px; line-height: 1.7; color: #374151;
      background-color: #f9fafb; border-left: 4px solid #2563eb;
      padding: 18px 20px; border-radius: 6px; }}
  .signature {{ margin-top: 32px; font-size: 15px; color: #111827; }}
  .signature .name {{ font-weight: 600; color: #2563eb;
      display: block; margin-top: 4px; }}
  .footer {{ text-align: center; padding: 20px;
      font-size: 12px; color: #9ca3af; }}
</style>
</head>
<body>
  <div class="email-wrapper">
    <div class="email-container">
      <div class="header">
        <h1>Message from Jarvis</h1>
        <p>Personal Assistant</p>
      </div>
      <div class="body-content">
        <p class="greeting">Dear {name},</p>
        <div class="message">
          {message}
        </div>
        <p class="signature">
          Kind regards,
          <span class="name">{sender_name}</span>
        </p>
      </div>
      <div class="footer">
        This message was sent on behalf of {sender_name}.
      </div>
    </div>
  </div>
</body>
</html>"""


def _send_email_via_power_automate(
    to: str,
    name: str,
    subject: str,
    message: str,
    sender_name: str = "Jarvis",
) -> str:
    """POST to the Power Automate HTTP trigger to send a branded HTML email."""
    if not _REQUESTS:
        return "[Email] 'requests' library not installed. Run: pip install requests"

    cfg = _get_config()
    url = cfg.get("power_automate_email_url", "").strip()
    if not url:
        return "[Email] power_automate_email_url not set in config/api_keys.json"

    # Build HTML body from template
    html_body = _EMAIL_HTML_TEMPLATE.format(
        name=name or "there",
        message=message.replace("\n", "<br>"),
        sender_name=sender_name,
    )

    payload = {
        "to":          to,
        "name":        name or "there",
        "subject":     subject or f"Message from {sender_name}",
        "message":     message,
        "html_body":   html_body,
        "sender_name": sender_name,
    }

    print(f"[Email] Triggering Power Automate flow -> {to} | Subject: {payload['subject']}")

    try:
        resp = _requests.post(url, json=payload, timeout=30)
        if resp.status_code in (200, 202):
            return (
                f"✅ Email sent to {to} via Power Automate. "
                f"Subject: \"{payload['subject']}\"."
            )
        else:
            return (
                f"[Email] Power Automate returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
    except _requests.Timeout:
        return "[Email] Power Automate flow timed out. The flow may still be running."
    except Exception as exc:
        return f"[Email] Failed to trigger flow: {exc}"


def _resolve_platform(platform_str: str):
    key = platform_str.lower().strip()
    for keywords, handler in _PLATFORM_MAP:
        if any(k in key for k in keywords):
            return handler
    return lambda r, m: _desktop_send(platform_str.strip().title(), r, m)


def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params       = parameters or {}
    receiver     = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    platform     = params.get("platform", "whatsapp").strip().lower()

    if not receiver:
        return "Please specify a recipient."
    if not message_text:
        return "Please specify the message content."

    preview = message_text[:50] + ("…" if len(message_text) > 50 else "")
    print(f"[SendMessage] >> {platform} -> {receiver}: {preview}")
    if player:
        player.write_log(f"[msg] {platform} → {receiver}")

    # ── Email via Power Automate ──────────────────────────────────────────
    if platform in ("email", "mail", "e-mail"):
        name        = params.get("name", "").strip()
        subject     = params.get("subject", "").strip()
        sender_name = params.get("sender_name", "Jarvis").strip()
        result = _send_email_via_power_automate(
            to=receiver,
            name=name,
            subject=subject,
            message=message_text,
            sender_name=sender_name,
        )
        print(f"[SendMessage] {'OK' if 'OK' in result or 'sent' in result.lower() else 'ERR'} {result}")
        if player:
            player.write_log(f"[email] {result}")
        return result

    # ── WhatsApp (Meta Cloud API / Desktop Fallback) ──────────────────────
    if any(k in platform for k in ("whatsapp", "wp", "wapp")):
        result = _send_whatsapp(receiver, message_text)
        tag = "[OK]" if "sent" in result.lower() else "[ERR]"
        print(f"[SendMessage] {tag} {result}")
        if player:
            player.write_log(f"[whatsapp] {result}")
        return result

    # ── Desktop app messaging ─────────────────────────────────────────────
    if not _PYAUTOGUI:
        return "PyAutoGUI is not installed — cannot control the desktop."

    try:
        handler = _resolve_platform(platform)
        result  = handler(receiver, message_text)
    except Exception as e:
        result = f"Could not send message: {e}"

    tag = "[OK]" if "sent" in result.lower() else "[ERR]"
    print(f"[SendMessage] {tag} {result}")
    if player:
        player.write_log(f"[msg] {result}")

    return result