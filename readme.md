# ⚙️ JARVIS — MARK XLVIII (48)
### Tactical Cross-Platform AI Assistant, Cloud PWA & Workspace MCP Suite — By Peterson Mambondimumwe

> 📺 **[Watch on YouTube (@SirPeterson)](https://www.youtube.com/@SirPeterson)**

A real-time, full-duplex voice and text AI assistant powered by the **Gemini Live API** (`gemini-2.5-flash` / `gemini-2.5-flash-native-audio`). Runs anywhere: as a **Cloud-Hosted Progressive Web App (PWA)** on iOS, Android, and desktop browsers, or locally on Windows, macOS, and Linux with full hardware and OS control.

---

## ✨ Key Highlights

- **🛡️ Mark XLVIII Tactical Combat HUD (`dashboard/static/app.html`, `ui.py`)**: High-contrast obsidian-and-crimson tactical interface with chamfered armor-plate geometry, a real-time 3D rotating reactor core, live telemetry panels, and responsive desktop/mobile layouts.
- **🎙️ 3-Layer Always-Listening Wake Word (`"Jarvis wake up"` / `"Hey Jarvis"`)**:
  1. **In-PWA Standby Sentinel**: Continuous Web Speech wake-word engine (`WAKE: LISTENING`) that plays a tactical activation chime, foregrounds the PWA via Service Worker (`sw.js`), and engages full-duplex live voice automatically.
  2. **Mobile OS Voice Launch (`/auto-login?key=JARVIS&voice=1`)**: Deep-link URL for iOS Siri Shortcuts and Android Google Assistant Routines that launches the closed PWA straight into live microphone mode.
  3. **PC Background Daemon (`core/wake_word.py`)**: Lightweight local listener that spawns the standalone `--app` PWA window when `"Jarvis wake up"` is spoken on your computer.
- **🔌 Built-in Workspace MCP Server (`core/mcp_servers/workspace_server.py`)**: 17 native Model Context Protocol tools for **GitHub repository operations**, **SMTP/IMAP Email reading & replying**, and **Meta WhatsApp Cloud API messaging** — auto-mounted by Jarvis and compatible with external MCP clients (Claude Desktop, Cursor, Windsurf).
- **⚡ Always-On Wake Lock & Low-Latency Streaming**: Hardware screen Wake Lock (`navigator.wakeLock`) + WebAudio keep-alive oscillator prevent mobile/desktop sleep while open, paired with 50 ms PCM16 audio slicing for sub-100 ms voice interrupts.

---

## 🚀 Capabilities Overview

| Domain | Capabilities |
|---|---|
| 🎙️ **Full-Duplex Live Voice** | Native bi-directional PCM16 audio streaming via Gemini Live API with instant interrupt (`ESC` or HUD button) |
| 🔊 **Wake Word Sentinel** | Always-listening `"Jarvis wake up"` / `"Hey Jarvis"` activation in the PWA, via iOS/Android shortcuts, or via `core/wake_word.py` |
| 🐙 **GitHub Automation (MCP)** | List, inspect, create, rename, update, and delete repositories; read/commit files; manage branches, issues, and PRs |
| 📧 **Email Suite — SMTP & IMAP (MCP)** | Read latest inbox messages, search by sender/subject/unread status, read full threads, reply in-thread, and send branded HTML emails |
| 💬 **WhatsApp Cloud API (MCP & Webhook)** | Send free-form and template (`jarvis_notification`) WhatsApp messages via Meta Graph API v22.0, plus two-way webhook replies |
| 🖥️ **System & Desktop Control** | Launch apps, control volume/brightness/WiFi/power, manage windows, execute keyboard/mouse macros |
| 👁️ **Screen & Camera Vision** | Real-time screen capture and webcam analysis piped directly into the active Gemini session |
| 🔍 **Parallel Grounded Search** | Simultaneous Gemini Grounded Search + DuckDuckGo (`news`, `research`, `price`, `compare`, `search`) — fastest response wins |
| 🧠 **Persistent & Semantic Memory** | Remembers user preferences, active projects, contacts, and conversation history across sessions |
| 📊 **Hardware Telemetry** | Live CPU, RAM, disk, network, and uptime telemetry streamed to the HUD |
| ⏰ **Smart Reminders & Briefing** | OS-native scheduled notifications, two-phase morning briefings, live weather, and flight lookups |

---

## 🔌 Jarvis Workspace MCP Server (17 Built-in Tools)

Jarvis automatically mounts `core/mcp_servers/workspace_server.py` at startup via `config/mcp_servers.json`. You can also connect external MCP hosts (Claude Desktop, Cursor, etc.) directly to `python core/mcp_servers/workspace_server.py`.

### 🐙 GitHub Tools (10)
| Tool | Description |
|---|---|
| `github_list_repos` | List owned, collaborator, or starred repositories |
| `github_repo_info` | Fetch repository metadata, stars, forks, visibility, default branch, and URL |
| `github_rename_repo` | Rename an existing GitHub repository |
| `github_create_repo` | Create a new public or private repository (with optional auto-init) |
| `github_update_repo` | Update repository description, homepage, or public/private visibility |
| `github_delete_repo` | Permanently delete a repository |
| `github_get_file` | Read the decoded UTF-8 contents of any file in a repository |
| `github_write_file` | Create or update a file and commit directly to a branch |
| `github_list_branches` | List all branches and commit SHAs in a repository |
| `github_create_issue` / `github_list_pulls` | Open new issues or inspect pull requests |

### 📧 Email Tools — IMAP & SMTP (5)
| Tool | Description |
|---|---|
| `email_send` | Send a branded HTML email via SMTP (with Power Automate fallback) |
| `email_list_inbox` | List the most recent emails in `INBOX` (`UNSEEN` or `ALL`) with sender, subject, date, and preview |
| `email_search` | Search inbox by sender, subject, body text, or raw IMAP criteria |
| `email_read` | Fetch and decode the full body and headers of a specific email by UID (or `"latest"`) |
| `email_reply` | Reply directly to an existing email thread (`In-Reply-To` / `References` preserved) |

### 💬 WhatsApp Cloud API Tools (2)
| Tool | Description |
|---|---|
| `whatsapp_send` | Send a WhatsApp message or `jarvis_notification` template to a contact name or E.164 phone number |
| `whatsapp_list_contacts` | List saved WhatsApp contacts from `config/contacts.json` |

---

## 🗣️ Always-Listening `"Jarvis wake up"` Setup

### 1. Inside the Web App / PWA (Automatic)
- The top bar displays **`WAKE: LISTENING`** (or **`WAKE: ARMED`** until your first tap unlocks browser audio).
- Say **`"Jarvis wake up"`** or **`"Hey Jarvis"`** at any time — Jarvis chimes, focuses the HUD window, and turns on the live microphone.
- You can also chain a command in a single breath: *"Jarvis wake up, read my latest email."*

### 2. On iPhone / Android When the App is Closed
1. In the Jarvis HUD, click **DEPLOY HUD → Voice Launch → Copy Voice Launch URL** (copies `https://<your-domain>/auto-login?key=JARVIS&voice=1`).
2. **iOS (Siri Shortcuts)**: Open **Shortcuts** → `+` → Name it **`Jarvis wake up`** → Add action **Open URLs** → Paste the Voice Launch URL.
3. **Android (Google Assistant Routines)**: Open **Google Home / Assistant Routines** → Starter: When I say **`"Jarvis wake up"`** → Action: **Open website** with the Voice Launch URL.

### 3. On PC When the Browser is Closed
Run the background desktop wake sentinel pointed at your deployment:
```bash
python core/wake_word.py --pwa-url "https://<your-domain>/auto-login?key=JARVIS&voice=1"
```

---

## ⚙️ Environment Variables (`.env` / Dokploy)

Configure these variables in `.env` or your Dokploy environment settings:

```env
# ── Core AI & Auth ──────────────────────────────────────────────
GEMINI_API_KEY=your_gemini_api_key
JARVIS_ACCESS_KEY=JARVIS

# ── GitHub Integration ──────────────────────────────────────────
GITHUB_TOKEN=ghp_your_personal_access_token
GITHUB_USERNAME=PetersonMambondimumwe

# ── Email (SMTP Sending + IMAP Reading) ─────────────────────────
SMTP_EMAIL=your_email@gmail.com
SMTP_PASSWORD=your_16_char_google_app_password
IMAP_EMAIL=your_email@gmail.com
IMAP_PASSWORD=your_16_char_google_app_password
# Optional host overrides (auto-detected for Gmail / Office365):
# SMTP_HOST=smtp.gmail.com
# SMTP_PORT=587
# IMAP_HOST=imap.gmail.com
# IMAP_PORT=993

# ── WhatsApp Cloud API (Meta Graph API v22.0) ───────────────────
PHONE_NUMBER_ID=your_meta_phone_number_id
WHATSAPP_TOKEN=your_meta_permanent_access_token
WHATSAPP_TEMPLATE_NAME=jarvis_notification
WHATSAPP_TEMPLATE_LANG=en
WHATSAPP_VERIFY_TOKEN=jarvis_verify_token
```

> **Note on `IMAP_PASSWORD` / `SMTP_PASSWORD`**: For Gmail, generate a 16-character App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). For Outlook/Microsoft 365, generate one at [account.live.com/proofs/manage/additional](https://account.live.com/proofs/manage/additional). `SMTP_*` and `IMAP_*` credentials automatically fall back to each other if only one pair is set.

---

## ⚡ Quick Start

### Option A: Cloud / PWA Dashboard Server (FastAPI + WebSockets)
```bash
git clone https://github.com/PetersonMambondimumwe/Jarvis.git
cd Jarvis
pip install -r requirements.txt
uvicorn dashboard.server:app --host 0.0.0.0 --port 3000
```
Then open `http://localhost:3000` (or `http://localhost:3000/auto-login?key=JARVIS&voice=1`).

### Option B: Docker / Dokploy Deployment
```bash
docker compose up -d --build
```

### Option C: Local Desktop Mode (PyQt6 HUD + Native Audio)
```bash
python main.py
```

---

## 🗂️ Project Structure

```text
Jarvis/
├── main.py                              # Desktop core loop — Gemini Live session, audio I/O, tool dispatch
├── ui.py                                # PyQt6 Mark XLVIII Tactical Combat HUD (desktop)
├── Dockerfile & docker-compose.yml      # Cloud containerization for Dokploy / VPS deployment
├── dashboard/
│   ├── server.py                        # FastAPI + WebSocket server, Gemini Live bridge, WhatsApp webhook
│   └── static/
│       ├── app.html                     # Mark XLVIII Tactical Combat PWA HUD + Always-Listening Wake Sentinel
│       ├── login.html                   # Biometric/Access-Key Tactical Auth Gate
│       ├── sw.js                        # Service Worker (offline cache + WAKE_PWA window focus/launch)
│       └── manifest.json                # PWA manifest, launch_handler, and Voice Launch shortcuts
├── core/
│   ├── jarvis_core.py                   # Unified Jarvis brain & tool router
│   ├── mcp_client.py                    # MCP client bridge auto-mounting configured MCP servers
│   ├── mcp_servers/
│   │   └── workspace_server.py          # Built-in 17-tool MCP Server (GitHub, SMTP/IMAP Email, WhatsApp)
│   ├── whatsapp_client.py               # Meta WhatsApp Cloud API v22.0 client (text + template fallback)
│   ├── wake_word.py                     # Desktop wake-word sentinel & standalone PWA window launcher
│   └── prompt.txt                       # JARVIS personality and tactical routing rules
├── actions/
│   ├── github_manager.py                # GitHub REST API repository/file/branch/issue/PR manager
│   ├── send_message.py                  # Multi-platform messaging (SMTP, Power Automate, WhatsApp, Desktop)
│   ├── web_search.py                    # Parallel Gemini Grounded Search + DuckDuckGo search
│   ├── screen_processor.py              # Screen capture & webcam vision via Gemini Live
│   ├── reminder.py                      # OS-native scheduled notifications
│   ├── system_monitor.py                # CPU / RAM / GPU / temperature telemetry
│   └── ...                              # Browser, desktop, file, weather, flight, YouTube & code actions
├── memory/                              # Persistent & vector-backed memory store
├── config/
│   ├── mcp_servers.json                 # Auto-mounted MCP server definitions
│   ├── contacts.json                    # Saved WhatsApp / messaging contacts
│   └── api_keys.json                    # Optional local API key configuration
└── tests/
    └── test_whatsapp_client.py          # Unit test suite
```

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.

---

## 👤 Connect with the Creator

Engineered by **Peterson Mambondimumwe (@SirPeterson)**.
⭐ **Star the repository to support the journey to Mark 100.**

| Platform | Link |
| --- | --- |
| GitHub | [@PetersonMambondimumwe](https://github.com/PetersonMambondimumwe) |
| YouTube | [@SirPeterson](https://www.youtube.com/@SirPeterson) |
| Instagram | [@sirpeterson](https://www.instagram.com/sirpeterson) |
