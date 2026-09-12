# 📱 Running JARVIS on Your Server & Connecting Your Phone

This guide walks you through deploying **JARVIS Headless** inside Docker on your server and using your smartphone as the microphone, speaker, and live mobile assistant.

---

## 🏗️ Architecture

```
 ┌─────────────────────────────────────────────────────────┐
 │ 🐳 Server Container (Docker)                            │
 │  • Headless JARVIS (Gemini Live WebSocket session)      │
 │  • MCP Tool Servers (Weather, Diagnostics, Filesystem) │
 │  • Agent Bridges (Antigravity, EDITH, Postgres, etc.)   │
 │  • FastAPI WebSocket Audio Gateway (Port 8000)          │
 └────────────────────────────▲────────────────────────────┘
                              │
                    WiFi / Tailscale / HTTPS
                              │
 ┌────────────────────────────▼────────────────────────────┐
 │ 📱 Your Smartphone (iOS Safari / Android Chrome)        │
 │  • Web Audio API: Streams your mic in real time         │
 │  • Live Speaker: Plays JARVIS's voice responses         │
 │  • Interactive Dashboard & Chat History                 │
 └─────────────────────────────────────────────────────────┘
```

---

## 🚀 Step 1: Deploy on Your Server

### 1. Copy the Code to Your Server
Copy this `Mark-XLVIII-main` folder to your server via `scp`, `rsync`, or git:
```bash
# Example using rsync or scp
scp -r ./Mark-XLVIII-main user@your-server-ip:~/jarvis
```

### 2. Configure Your API Key
Ensure `config/api_keys.json` contains your Gemini API key:
```json
{
  "gemini_api_key": "YOUR_GEMINI_API_KEY",
  "os_system": "Linux"
}
```

### 3. Build & Run with Docker Compose
On your server terminal:
```bash
cd ~/jarvis
docker compose up -d --build
```

To view live logs and verify startup:
```bash
docker compose logs -f
```
You will see:
```text
[+] JARVIS HEADLESS SERVER MODE (Docker / Server / Cloud)
Connect your phone or browser at: http://0.0.0.0:8000
[HeadlessUI] Initialized headless console adapter.
[JARVIS] Connected.
```

---

## 📱 Step 2: Accessing from Your Phone

### Crucial Mobile Rule: **Microphone Permissions Require HTTPS or Localhost**
Mobile operating systems (iOS Safari and Android Chrome) block web microphone access on raw `http://` IP addresses (unless accessing via `localhost`).

To enable seamless microphone access on your phone, choose one of these two free methods:

### Option A: Tailscale (Easiest & Completely Private)
1. Install **Tailscale** on your server (`curl -fsSL https://tailscale.com/install.sh | sh`).
2. Install the **Tailscale app** on your phone (App Store / Google Play).
3. On your phone, open your browser and navigate to:
   ```text
   http://<your-server-tailscale-name>:8000/login
   ```
   *(Tailscale automatically treats private mesh connections with full trusted permissions!)*

### Option B: Cloudflare Tunnel (Free Public HTTPS with SSL)
If you want to access Jarvis from anywhere in the world:
1. Run a free Cloudflare Tunnel on your server:
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```
2. Cloudflare gives you a secure HTTPS URL (e.g. `https://jarvis-xyz.trycloudflare.com`).
3. Open that link on your phone. Because it has valid HTTPS SSL, your phone's browser will allow full microphone access with one tap!

---

## 🎙️ Step 3: Pairing & Speaking to Jarvis

1. Open the URL on your phone browser.
2. Enter the 6-character access key displayed in your server logs or set up your device session.
3. Tap the **Microphone (🎙️)** button:
   - Your browser will ask: *"Allow microphone access?"* $\rightarrow$ Tap **Allow**.
   - The button turns into a recording indicator.
4. **Speak naturally to Jarvis**:
   - *"Jarvis, what's the weather like right now?"*
   - *"Jarvis, how much free space is left on the server?"*
   - *"Jarvis, tell Antigravity to run our test suite."*
5. **Listen to Jarvis**:
   - Jarvis will stream his real-time synthesized voice directly through your phone's speaker!

---

## 📲 Step 4: Install as an App (PWA)

To make Jarvis look and feel like a native mobile app on your phone:
- **iPhone (Safari)**: Tap the **Share** button $\rightarrow$ Select **"Add to Home Screen"**.
- **Android (Chrome)**: Tap the **Three Dots (⋮)** $\rightarrow$ Select **"Install App"** or **"Add to Home Screen"**.

Now you have a dedicated **JARVIS** app icon on your phone's home screen!
