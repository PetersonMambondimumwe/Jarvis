import threading
import time
import io
import os
import json
import base64
from pathlib import Path
from datetime import datetime
import pyautogui
import mss
from PIL import Image
from google import genai
from google.genai import types as gtypes

# Windows specific imports
try:
    import pywinauto
    from pywinauto import Desktop
    _PYWINAUTO = True
except ImportError:
    _PYWINAUTO = False

class PerceptionEngine:
    """
    Real-time desktop perception system for JARVIS.
    Continuously captures and analyzes the screen to maintain a live state.
    """
    def __init__(self, api_key, player=None):
        self.api_key = api_key
        self.player = player
        self.client = genai.Client(api_key=api_key)
        self.active = False
        self.perception_thread = None
        self.current_state = {
            "last_update": None,
            "active_window": None,
            "elements": [],
            "screen_text": "",
            "confidence": 0.0
        }
        self.fps = 10  # Target FPS for capture
        self.analysis_interval = 2.0  # Seconds between deep AI analysis
        self.last_analysis_time = 0
        self._lock = threading.Lock()
        
        # UI Automation cache
        self.ui_elements = []
        
    def start(self):
        if self.active:
            return
        self.active = True
        self.perception_thread = threading.Thread(target=self._perception_loop, daemon=True)
        self.perception_thread.start()
        print("[Perception] 👁️ Live perception engine started")

    def stop(self):
        self.active = False
        if self.perception_thread:
            self.perception_thread.join(timeout=2.0)
        print("[Perception] 🛑 Live perception engine stopped")

    def _perception_loop(self):
        """Continuous capture and lightweight analysis loop."""
        while self.active:
            start_time = time.time()
            
            # 1. Lightweight capture & UI Automation check
            self._update_ui_state()
            
            # 2. Periodic deep analysis using Gemini
            if time.time() - self.last_analysis_time > self.analysis_interval:
                self._deep_analysis()
                self.last_analysis_time = time.time()
            
            # Maintain target FPS
            elapsed = time.time() - start_time
            sleep_time = max(0, (1.0 / self.fps) - elapsed)
            time.sleep(sleep_time)

    def _update_ui_state(self):
        """Uses Windows UI Automation to get current window and element tree."""
        if not _PYWINAUTO:
            return
            
        try:
            # Get current active window
            active_win = Desktop(backend="uia").active()
            win_text = active_win.window_text()
            
            # Extract common elements via UI Automation for faster response
            new_ui_elements = []
            try:
                # Limit depth for performance
                for child in active_win.descendants(control_type="Button", depth=2):
                    rect = child.rectangle()
                    new_ui_elements.append({
                        "type": "button",
                        "label": child.window_text(),
                        "pos": [rect.mid_point().x, rect.mid_point().y],
                        "source": "uia"
                    })
            except:
                pass

            with self._lock:
                self.ui_elements = new_ui_elements
                if self.current_state["active_window"] != win_text:
                    print(f"[Perception] 🪟 Active window changed: {win_text}")
                    self.current_state["active_window"] = win_text
                    # Trigger immediate deep analysis on window change
                    self.last_analysis_time = 0 
        except Exception as e:
            pass

    def _deep_analysis(self):
        """Uses Gemini to perform OCR and UI element detection on a fresh frame."""
        try:
            with mss.mss() as sct:
                # Capture primary monitor
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                
                # Resize for faster processing
                img.thumbnail((1280, 720), Image.Resampling.BILINEAR)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=80)
                image_bytes = buf.getvalue()

            # Prepare prompt for Gemini
            prompt = (
                "Analyze this desktop screenshot. "
                "1. List the title of the active window. "
                "2. Identify key interactive elements (buttons, text fields, tabs) and their approximate center coordinates (x,y). "
                "3. Extract any prominent text visible. "
                "Format as JSON: {\"window\": \"title\", \"elements\": [{\"type\": \"button\", \"label\": \"name\", \"pos\": [x, y]}], \"text\": \"...\"}"
            )

            response = self.client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=[
                    gtypes.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt,
                ],
                config=gtypes.GenerateContentConfig(response_mime_type="application/json")
            )

            if response.text:
                data = json.loads(response.text)
                with self._lock:
                    self.current_state["last_update"] = datetime.now().isoformat()
                    self.current_state["elements"] = data.get("elements", [])
                    self.current_state["screen_text"] = data.get("text", "")
                    # print(f"[Perception] 🧠 State updated. Found {len(self.current_state['elements'])} elements.")

        except Exception as e:
            print(f"[Perception] ❌ Deep analysis failed: {e}")

    def get_current_state(self):
        with self._lock:
            return self.current_state.copy()

    def find_element(self, description, retry=3):
        """Intelligently find an element with retries and state refresh."""
        for attempt in range(retry):
            state = self.get_current_state()
            
            # 1. Check cached elements
            desc_lower = description.lower()
            for el in state["elements"]:
                if desc_lower in el.get("label", "").lower() or desc_lower in el.get("type", "").lower():
                    return el.get("pos")
            
            # 2. If not found, wait for UI update and retry
            print(f"[Perception] 🔍 Element '{description}' not found, waiting for UI update (Attempt {attempt+1}/{retry})...")
            time.sleep(1.5)
            self._deep_analysis() # Force refresh
            
        return None
