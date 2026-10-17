"""
core/wake_word.py
─────────────────
High-Performance Asynchronous Wake Word Listener for JARVIS.

Architecture:
  1. Non-blocking audio capture via sounddevice (16kHz mono).
  2. Dynamic Voice Activity Detection (VAD) with adaptive ambient noise calibration.
  3. Pre-roll & post-roll audio frame collection to capture complete phrases without clipping.
  4. Async background speech recognition thread pool (never blocks microphone recording).
  5. Phonetic & fuzzy phrase matching for "Jarvis wake up", "Hey Jarvis", "Wake up", etc.
  6. Multi-target wake execution:
     - Direct in-process callback (unminimize & unmute UI).
     - Localhost HTTP wake signal to JARVIS dashboard (/api/wake).
     - Standalone process auto-launcher (spawns `python main.py` when JARVIS is closed).
"""

from __future__ import annotations

import collections
import concurrent.futures
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np
try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    import speech_recognition as sr
except ImportError:
    sr = None


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()

# Common phonetic variations that speech recognition may return for "Jarvis / Wake up"
WAKE_PATTERNS = [
    "jarvis wake up",
    "wake up jarvis",
    "wake up",
    "hey jarvis",
    "hi jarvis",
    "hello jarvis",
    "ok jarvis",
    "jarvis",
    "service wake up",
    "travis wake up",
    "harvest wake up",
    "charvis",
    "javis",
]


class WakeWordListener:
    """Always-on, low-CPU background wake word listener with adaptive VAD."""

    def __init__(
        self,
        wake_phrases: Optional[List[str]] = None,
        on_wake: Optional[Callable[[], None]] = None,
        auto_launch_main: bool = True,
    ):
        self.wake_phrases = [p.lower().strip() for p in (wake_phrases or WAKE_PATTERNS)]
        self.on_wake = on_wake
        self.auto_launch_main = auto_launch_main
        self.recognizer = sr.Recognizer()
        self.is_running = False
        self.sample_rate = 16000
        self.chunk_size = 800  # 50ms per chunk at 16kHz
        
        self._audio_queue: queue.Queue[np.ndarray] = queue.Queue()
        self._listen_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="WakeSTT")
        self._last_trigger_time = 0.0

    def start(self) -> None:
        """Start listening for the wake word."""
        if self.is_running:
            return
        self.is_running = True
        self._process_thread = threading.Thread(target=self._vad_process_loop, daemon=True, name="WakeVADProcessor")
        self._process_thread.start()
        print(f"[WakeWord] Wake listener initialized (monitoring {len(self.wake_phrases)} phrases)...")

    def stop(self) -> None:
        """Stop listening."""
        self.is_running = False
        self._executor.shutdown(wait=False)

    def _audio_callback(self, indata, frames, time_info, status):
        if self.is_running:
            self._audio_queue.put(indata.copy())

    def _vad_process_loop(self) -> None:
        """Continuously reads 50ms audio chunks, dynamically tracks noise floor, and collects speech utterances."""
        pre_roll_chunks = int(0.5 * (self.sample_rate / self.chunk_size))  # 500ms pre-roll
        pre_roll_buffer = collections.deque(maxlen=pre_roll_chunks)

        speech_buffer: List[np.ndarray] = []
        is_in_speech = False
        silence_chunks_count = 0
        max_silence_chunks = int(0.6 * (self.sample_rate / self.chunk_size))  # 600ms silence = phrase end
        max_speech_chunks = int(4.0 * (self.sample_rate / self.chunk_size))   # 4.0s max phrase duration

        # Ambient noise calibration
        noise_floor = 10.0
        calibration_samples = []

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.chunk_size,
                callback=self._audio_callback,
            ):
                while self.is_running:
                    try:
                        chunk = self._audio_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue

                    # Calculate RMS energy of chunk
                    rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))

                    # Calibrate baseline noise floor during first 2 seconds
                    if len(calibration_samples) < 30:
                        calibration_samples.append(rms)
                        noise_floor = max(5.0, float(np.median(calibration_samples)))
                        pre_roll_buffer.append(chunk)
                        continue

                    # Dynamic speech threshold: 2.2x ambient baseline or min 18 RMS
                    speech_threshold = max(18.0, noise_floor * 2.2)

                    if rms > speech_threshold:
                        if not is_in_speech:
                            # Speech start: grab pre-roll audio frames
                            is_in_speech = True
                            speech_buffer = list(pre_roll_buffer)
                        speech_buffer.append(chunk)
                        silence_chunks_count = 0
                    else:
                        # Slowly adapt noise floor during calm periods
                        noise_floor = 0.98 * noise_floor + 0.02 * min(rms, noise_floor * 1.5)

                        if is_in_speech:
                            speech_buffer.append(chunk)
                            silence_chunks_count += 1

                            # If silence persisted or phrase reached max limit -> finish utterance
                            if silence_chunks_count >= max_silence_chunks or len(speech_buffer) >= max_speech_chunks:
                                is_in_speech = False
                                silence_chunks_count = 0
                                
                                # Only dispatch if utterance duration is between 0.6s and 4.0s
                                min_chunks = int(0.6 * (self.sample_rate / self.chunk_size))
                                if len(speech_buffer) >= min_chunks:
                                    full_audio = np.concatenate(speech_buffer, axis=0)
                                    wav_bytes = self._audio_to_wav(full_audio)
                                    # Submit to background STT worker
                                    self._executor.submit(self._recognize_and_check, wav_bytes)

                                speech_buffer.clear()
                        else:
                            pre_roll_buffer.append(chunk)

        except Exception as e:
            print(f"[WakeWord] Stream error: {e}")

    def _audio_to_wav(self, audio_data: np.ndarray) -> bytes:
        """Convert int16 PCM numpy array to WAV bytes."""
        byte_io = io.BytesIO()
        with wave.open(byte_io, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # 16-bit PCM
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(audio_data.tobytes())
        return byte_io.getvalue()

    def _recognize_and_check(self, wav_bytes: bytes) -> None:
        """Asynchronously transcribes audio and checks for wake word matches."""
        try:
            with sr.AudioFile(io.BytesIO(wav_bytes)) as source:
                audio_data = self.recognizer.record(source)
            
            text = self.recognizer.recognize_google(audio_data).lower().strip()
            print(f"[WakeWord] Heard: '{text}'")

            # Check if any wake pattern is contained in recognized text
            matched = False
            for pattern in self.wake_phrases:
                if pattern in text:
                    matched = True
                    break

            if matched:
                now = time.monotonic()
                if (now - self._last_trigger_time) > 3.0:
                    self._last_trigger_time = now
                    print(f"[WakeWord] [MATCH DETECTED: '{text}'] -> Waking JARVIS!")
                    self.trigger_wake()
        except sr.UnknownValueError:
            pass  # Non-speech / ambient sound
        except Exception as exc:
            pass

    @staticmethod
    def launch_pwa_window(pwa_url: Optional[str] = None) -> bool:
        """Launch the JARVIS PWA in a standalone app window (Chrome/Edge --app mode or default browser)."""
        import shutil
        import webbrowser

        pin = os.environ.get("JARVIS_PIN", "JARVIS").strip() or "JARVIS"
        base_url = (
            pwa_url
            or os.environ.get("JARVIS_PWA_URL", "").strip()
            or f"http://127.0.0.1:8000/auto-login?key={pin}&voice=1"
        )
        if "voice=" not in base_url:
            sep = "&" if "?" in base_url else "?"
            base_url = f"{base_url}{sep}voice=1"

        # Prefer launching in chromeless standalone PWA window (--app=...)
        candidates: list[str] = []
        if sys.platform == "win32":
            prog_files = [
                os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                os.environ.get("LOCALAPPDATA", ""),
            ]
            rel_paths = [
                r"Google\Chrome\Application\chrome.exe",
                r"Microsoft\Edge\Application\msedge.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe",
            ]
            for root in prog_files:
                if not root:
                    continue
                for rel in rel_paths:
                    full = os.path.join(root, rel)
                    if os.path.isfile(full):
                        candidates.append(full)
        else:
            for bin_name in ("google-chrome", "chromium", "microsoft-edge", "brave-browser"):
                found = shutil.which(bin_name)
                if found:
                    candidates.append(found)

        for browser_bin in candidates:
            try:
                subprocess.Popen(
                    [browser_bin, f"--app={base_url}", "--autoplay-policy=no-user-gesture-required"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"[WakeWord] Launched JARVIS PWA window: {base_url}")
                return True
            except Exception:
                continue

        try:
            webbrowser.open(base_url, new=1, autoraise=True)
            print(f"[WakeWord] Opened JARVIS PWA in browser: {base_url}")
            return True
        except Exception as e:
            print(f"[WakeWord] Could not open browser: {e}")
            return False

    def trigger_wake(self) -> None:
        """Execute wake actions: direct callback, dashboard wake signal, PWA window launch, or process launcher."""
        import urllib.request

        # 1. In-process UI callback
        if self.on_wake:
            try:
                self.on_wake()
            except Exception as e:
                print(f"[WakeWord] Direct callback error: {e}")

        # 2. HTTP Signal to running Dashboard (wakes connected PWA tabs via WebSocket)
        notified = False
        target_api = os.environ.get("JARVIS_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
        pin = os.environ.get("JARVIS_PIN", "JARVIS").strip() or "JARVIS"
        try:
            req = urllib.request.Request(
                f"{target_api}/api/wake",
                method="POST",
                headers={"Authorization": f"Bearer {pin}"},
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    print("[WakeWord] Sent wake signal to running JARVIS session.")
                    notified = True
        except Exception:
            pass

        # 3. Launch / Focus the JARVIS PWA window with ?voice=1
        if os.environ.get("JARVIS_WAKE_OPEN_PWA", "1") != "0":
            self.launch_pwa_window()

        # 4. If local JARVIS is not currently active -> launch main.py
        if not notified and not self.on_wake and self.auto_launch_main and "127.0.0.1" in target_api:
            print("[WakeWord] Local JARVIS is closed. Launching main.py...")
            main_script = BASE_DIR / "main.py"
            python_exe = sys.executable
            try:
                subprocess.Popen([python_exe, str(main_script)], cwd=str(BASE_DIR))
                print("[WakeWord] JARVIS main process started successfully.")
            except Exception as e:
                print(f"[WakeWord] Failed to launch JARVIS: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="JARVIS Always-On Wake Word Daemon & PWA Launcher")
    parser.add_argument("--pwa-url", default="", help="Remote Dokploy or local JARVIS PWA URL to launch on wake")
    args = parser.parse_args()
    if args.pwa_url:
        os.environ["JARVIS_PWA_URL"] = args.pwa_url
        os.environ["JARVIS_SERVER_URL"] = args.pwa_url.split("/auto-login")[0].rstrip("/")

    print("=== JARVIS Standby Wake Word Daemon ===")
    print("Speak 'Jarvis wake up' or 'Hey Jarvis' to launch the JARVIS PWA.")
    listener = WakeWordListener(auto_launch_main=not bool(args.pwa_url))
    listener.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping wake daemon...")
        listener.stop()
