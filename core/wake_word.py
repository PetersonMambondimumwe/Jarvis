import sounddevice as sd
import numpy as np
import speech_recognition as sr
import threading
import time
import requests
import io
import wave
from pathlib import Path
import sys

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR = get_base_dir()

class WakeWordListener:
    def __init__(self, wake_word="hey jarvis", threshold=0.01):
        self.wake_word = wake_word.lower()
        self.threshold = threshold
        self.recognizer = sr.Recognizer()
        self.is_running = False
        self._thread = None
        self.sample_rate = 16000
        
    def start(self):
        self.is_running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        print(f"[WakeWord] Listening for '{self.wake_word}' in background (via sounddevice)...")

    def stop(self):
        self.is_running = False
        if self._thread:
            self._thread.join()

    def _listen_loop(self):
        # We'll use a sliding window of audio to detect sound and then transcribe
        buffer_duration = 3  # seconds
        buffer_size = self.sample_rate * buffer_duration
        audio_buffer = np.zeros(buffer_size, dtype=np.int16)

        def callback(indata, frames, time_info, status):
            nonlocal audio_buffer
            # Shift buffer and add new data
            audio_buffer = np.roll(audio_buffer, -frames)
            audio_buffer[-frames:] = indata[:, 0]

        try:
            with sd.InputStream(samplerate=self.sample_rate, channels=1, dtype='int16', callback=callback):
                while self.is_running:
                    # Check for energy
                    volume_norm = np.linalg.norm(audio_buffer[-self.sample_rate:]) / np.sqrt(self.sample_rate)
                    if volume_norm > self.threshold * 32768: # Scale for int16
                        # Convert buffer to WAV bytes for SpeechRecognition
                        byte_io = io.BytesIO()
                        with wave.open(byte_io, 'wb') as wav_file:
                            wav_file.setnchannels(1)
                            wav_file.setsampwidth(2) # 16-bit
                            wav_file.setframerate(self.sample_rate)
                            wav_file.writeframes(audio_buffer.tobytes())
                        
                        byte_io.seek(0)
                        with sr.AudioFile(byte_io) as source:
                            audio_data = self.recognizer.record(source)
                            try:
                                text = self.recognizer.recognize_google(audio_data).lower()
                                print(f"[WakeWord] Heard: '{text}'")
                                if self.wake_word in text:
                                    print(f"[WakeWord] Match found!")
                                    self.trigger_wake()
                                    # Clear buffer to avoid double trigger
                                    audio_buffer.fill(0)
                                    time.sleep(2)
                            except:
                                pass
                    
                    time.sleep(0.5)
        except Exception as e:
            print(f"[WakeWord] Error: {e}")

    def trigger_wake(self):
        try:
            requests.post("http://127.0.0.1:8000/api/wake", timeout=2)
            print("[WakeWord] Sent wake signal to Jarvis")
        except Exception as e:
            print(f"[WakeWord] Failed to send wake signal: {e}")

if __name__ == "__main__":
    listener = WakeWordListener()
    listener.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        listener.stop()
