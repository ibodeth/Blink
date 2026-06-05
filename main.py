import sys
import os
import threading
import time
import json
import sqlite3
import struct
import psutil
import requests
import asyncio
import pygame
import yt_dlp
import shutil
import zipfile
import math
import random
import subprocess
from datetime import datetime, timedelta

# --- GUI ---
from PyQt5.QtWidgets import (QApplication, QMainWindow, QLabel, QWidget, QVBoxLayout, 
                             QPushButton, QInputDialog, QGraphicsOpacityEffect, QHBoxLayout,
                             QLineEdit, QMessageBox, QFormLayout, QSlider, QFrame)
from PyQt5.QtGui import QPainter, QColor, QBrush, QPen, QFont, QIcon, QPixmap
from PyQt5.QtCore import (Qt, QThread, pyqtSignal, pyqtSlot, QPropertyAnimation, 
                          QEasingCurve, QPoint, QTimer, QRectF, QSize)

# --- AI & AUDIO ---
import pvporcupine
import pyaudio
import speech_recognition as sr
import edge_tts
from google import genai
from AppOpener import open as app_open, mklist

# ==========================================================
# 🔧 DEBUG
# ==========================================================
def log_debug(msg):
    print(f"\033[96m[DEBUG] {datetime.now().strftime('%H:%M:%S')} -> {msg}\033[0m")

# ==========================================================
# 🔑 CONFIGURATION
# ==========================================================
CONFIG_FILE = "blink_keys.json"
KEYS = {}

def load_keys():
    global KEYS
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f: KEYS = json.load(f)
            if KEYS.get("GEMINI") and KEYS.get("PICOVOICE"): return True
        except: pass
    return False

def save_keys_to_disk(g_key, p_key):
    global KEYS
    KEYS = {"GEMINI": g_key.strip(), "PICOVOICE": p_key.strip()}
    with open(CONFIG_FILE, "w") as f: json.dump(KEYS, f)

# ==========================================================
# 1. HELPER CLASSES
# ==========================================================
class ContextManager:
    def __init__(self):
        self.history = [] 
    def add(self, role, text):
        self.history.append({"role": role, "text": text})
        if len(self.history) > 15: self.history.pop(0)
    def get_context_string(self):
        return "\n".join([f"{m['role'].upper()}: {m['text']}" for m in self.history])

class FFmpegManager:
    def __init__(self):
        self.bin_path = os.path.join(os.getcwd(), "ffmpeg", "bin")
        if not shutil.which("ffmpeg") and not os.path.exists(os.path.join(self.bin_path, "ffmpeg.exe")):
            threading.Thread(target=self.download).start()
        else: self.add_path()
    def download(self):
        log_debug("Downloading FFmpeg...")
        try:
            url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
            with open("ffmpeg.zip", 'wb') as f: shutil.copyfileobj(requests.get(url, stream=True).raw, f)
            with zipfile.ZipFile("ffmpeg.zip", 'r') as z: z.extractall("temp_ff")
            for r, d, f in os.walk("temp_ff"):
                if "bin" in d: shutil.move(r, os.path.join(os.getcwd(), "ffmpeg")); break
            shutil.rmtree("temp_ff"); os.remove("ffmpeg.zip"); self.add_path()
            log_debug("FFmpeg installed.")
        except Exception as e: log_debug(f"FFmpeg error: {e}")
    def add_path(self):
        if self.bin_path not in os.environ["PATH"]: os.environ["PATH"] += os.pathsep + self.bin_path

class MusicDownloader(QThread):
    progress = pyqtSignal(str) 
    finished_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)

    def __init__(self, query):
        super().__init__()
        self.query = query

    def run(self):
        try:
            self.progress.emit(f"Searching: {self.query}...")
            def progress_hook(d):
                if d['status'] == 'downloading':
                    p = d.get('_percent_str', '0%').replace('%','')
                    self.progress.emit(f"Downloading: {p}%")
                elif d['status'] == 'finished':
                    self.progress.emit("Processing...")

            ydl_opts = {
                'format': 'bestaudio/best', 
                'outtmpl': 'temp_song.%(ext)s', 
                'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '128'}], 
                'noplaylist': True, 'quiet': True, 'no_warnings': True, 
                'default_search': 'ytsearch1',
                'progress_hooks': [progress_hook]
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: 
                info = ydl.extract_info(self.query, download=True)['entries'][0]
            
            meta = {
                "title": info.get('title', self.query),
                "duration": info.get('duration', 0),
                "thumbnail": info.get('thumbnail', '')
            }
            self.finished_signal.emit(meta)
        except Exception as e:
            self.error_signal.emit(str(e))

class MusicEngine:
    def __init__(self):
        try: pygame.mixer.init()
        except: log_debug("Failed to initialize Pygame Mixer!")
        self.current_file = os.path.abspath("temp_song.mp3")
        self.is_paused = False

    def is_busy(self):
        return pygame.mixer.music.get_busy() or self.is_paused

    def play_file(self):
        if os.path.exists(self.current_file):
            pygame.mixer.music.load(self.current_file); pygame.mixer.music.play()
            self.is_paused = False; log_debug("Music is playing."); return True
        return False

    def seek(self, seconds):
        try:
            if os.path.exists(self.current_file):
                pygame.mixer.music.play(start=seconds); log_debug(f"Music seeked: {seconds}")
        except: pass

    def toggle_pause(self):
        if self.is_paused: 
            pygame.mixer.music.unpause()
            self.is_paused = False
            return "Resumed"
        else: 
            pygame.mixer.music.pause()
            self.is_paused = True
            return "Paused"

    def stop(self):
        try: pygame.mixer.music.stop(); pygame.mixer.music.unload()
        except: pass
        self.is_paused = False

class MemoryManager:
    def __init__(self):
        self.c = sqlite3.connect("blink_mem.db", check_same_thread=False)
        try:
            self.c.execute('CREATE TABLE IF NOT EXISTS memory (key TEXT PRIMARY KEY, val TEXT, category TEXT, timestamp REAL)')
        except: pass
        try: self.c.execute("ALTER TABLE memory ADD COLUMN category TEXT DEFAULT 'general'")
        except: pass
        try: self.c.execute("ALTER TABLE memory ADD COLUMN timestamp REAL DEFAULT 0")
        except: pass
        self.c.commit()

    def save(self, key, val, category="general"): 
        if not key or not val: return
        ts = time.time()
        self.c.execute("INSERT OR REPLACE INTO memory VALUES (?, ?, ?, ?)", (key.lower(), val, category, ts))
        self.c.commit()
        log_debug(f"Memory Save [{category}]: {key} -> {val}")

    def get_relevant_memories(self):
        now = time.time()
        cursor = self.c.execute("SELECT key, val, category, timestamp FROM memory")
        memories = []
        for k, v, cat, ts in cursor:
            if cat == "status" and (now - ts) > 259200: continue 
            if cat == "events" and (now - ts) > 604800: continue
            
            date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            memories.append(f"- [{cat.upper()}] {k}: {v} (Date: {date_str})")
        return "\n".join(memories) if memories else "No custom info yet."

    def get_value(self, key):
        r = self.c.execute("SELECT val FROM memory WHERE key=?", (key.lower(),)).fetchone()
        return r[0] if r else None

# ==========================================================
# 6. SYSTEM MANAGER
# ==========================================================
class SystemManager:
    def __init__(self):
        threading.Thread(target=self._init_app_list).start()
    
    def _init_app_list(self):
        try: mklist(name='app_data.json', display=False, fix_path=True)
        except: pass

    def check_python_versions(self):
        try:
            result = subprocess.check_output("py --list", shell=True).decode("utf-8")
            versions = [line.strip() for line in result.split('\n') if line.strip()]
            return "Installed Python Versions: " + ", ".join(versions)
        except:
            return "Failed to retrieve Python versions."

    def execute(self, act, trg):
        log_debug(f"System Action: {act} -> {trg}")
        if not trg: return "No target specified."
        
        if act == "open":
            if "arduino" in trg.lower(): trg = "Arduino IDE"

            system_map = {
                "settings": "start ms-settings:",
                "notepad": "start notepad",
                "calculator": "start calc",
                "task manager": "start taskmgr",
                "cmd": "start cmd",
                "file explorer": "start explorer"
            }
            for key, cmd in system_map.items():
                if key in trg.lower():
                    subprocess.Popen(cmd, shell=True); return f"{key.title()} opened."

            try:
                app_open(trg, match_closest=True, output=True, throw_error=True)
                return f"{trg} opening..."
            except: pass 

            try:
                os.startfile(trg) 
                return f"{trg} started."
            except FileNotFoundError:
                try:
                    subprocess.Popen(f'start "" "{trg}"', shell=True)
                    return f"{trg} started."
                except: return f"NOT FOUND: {trg}"
            except Exception as e: return f"NOT FOUND: {trg}"

        if act == "close":
            killed = False
            for p in psutil.process_iter(['name']): 
                if p.info['name'] and trg.lower() in p.info['name'].lower(): 
                    try: p.terminate(); killed = True
                    except: pass
            return "Closed." if killed else "Not running."

# ==========================================================
# 2. VISUALIZER
# ==========================================================
class SiriVisualizerWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(200, 200)
        self.amplitude = 0.0 
        self.base_color = QColor(31, 106, 165)
    def set_amplitude(self, level): self.amplitude = level; self.update() 
    def set_mode(self, mode):
        if mode == "listening": self.base_color = QColor(255, 50, 50) 
        elif mode == "speaking": self.base_color = QColor(50, 200, 50) 
        elif mode == "thinking": self.base_color = QColor(255, 200, 0) 
        else: self.base_color = QColor(31, 106, 165) 
        self.update()
    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        center = QPoint(self.width() // 2, self.height() // 2)
        base_radius = 60; dynamic_radius = base_radius + (self.amplitude * 40)
        c_out = QColor(self.base_color); c_out.setAlpha(100) 
        painter.setBrush(QBrush(c_out)); painter.setPen(Qt.NoPen); painter.drawEllipse(center, dynamic_radius, dynamic_radius)
        c_in = QColor(self.base_color); c_in.setAlpha(255) 
        painter.setBrush(QBrush(c_in)); painter.drawEllipse(center, base_radius + (self.amplitude * 10), base_radius + (self.amplitude * 10))

# ==========================================================
# 3. BACKEND (RHYTHM KEEPER)
# ==========================================================
class BackendWorker(QThread):
    signal_status = pyqtSignal(str)
    signal_text = pyqtSignal(str)
    signal_audio_level = pyqtSignal(float)
    signal_music_start = pyqtSignal(str, str, int)
    signal_music_stop = pyqtSignal()
    # DÜZELTME: Eski sinyal geri eklendi (Hata vermemesi için)
    signal_reminder_trigger = pyqtSignal(str)
    # Yeni Ana Thread Timer Sinyali
    signal_start_timer = pyqtSignal(int, str)

    def __init__(self):
        super().__init__()
        self.running = True
        self.ff = FFmpegManager()
        self.music = MusicEngine()
        self.mem = MemoryManager()
        self.sys = SystemManager()
        self.ctx = ContextManager()
        self.downloader = None 
        try: self.ai = genai.Client(api_key=KEYS["GEMINI"])
        except: log_debug("Gemini Key Error!")

    def calculate_rms(self, audio_data):
        try:
            count = len(audio_data) / 2; format = "%dh" % (count)
            shorts = struct.unpack(format, audio_data)
            sum_squares = sum(n * (1.0/32768) * n * (1.0/32768) for n in shorts)
            return min(math.sqrt(sum_squares / count) * 5, 1.0)
        except: return 0.0

    def run(self):
        self.signal_status.emit("thinking")
        user_name = self.mem.get_value("name") or self.mem.get_value("isim")
        greet = f"Hello again {user_name}!" if user_name else "Hello!"
        self.speak(greet)
        
        try:
            ppn = "Blink_en_windows_v4_0_0.ppn"
            kw = [ppn] if os.path.exists(ppn) else None
            kws = ['porcupine'] if not kw else None
            porcupine = pvporcupine.create(access_key=KEYS["PICOVOICE"], keyword_paths=kw, keywords=kws)
            pa = pyaudio.PyAudio()
            stream = pa.open(rate=porcupine.sample_rate, channels=1, format=pyaudio.paInt16, input=True, frames_per_buffer=porcupine.frame_length)
            
            self.signal_status.emit("idle")
            self.signal_text.emit("Sleeping Mode...")

            while self.running:
                pcm = stream.read(porcupine.frame_length, exception_on_overflow=False)
                self.signal_audio_level.emit(self.calculate_rms(pcm)) 
                if porcupine.process(struct.unpack_from("h" * porcupine.frame_length, pcm)) >= 0:
                    log_debug("Wake Word Detected!")
                    self.active_listen()
                    self.signal_status.emit("idle")
        except Exception as e: self.signal_text.emit(f"Error: {e}")

    def active_listen(self):
        self.signal_status.emit("listening")
        self.signal_text.emit("Listening...")
        if pygame.mixer.music.get_busy(): pygame.mixer.music.set_volume(0.2)

        r = sr.Recognizer()
        r.energy_threshold = 2000
        r.dynamic_energy_threshold = True
        r.pause_threshold = 2.0 
        
        with sr.Microphone() as source:
            try:
                r.adjust_for_ambient_noise(source, duration=0.5)
                audio = r.listen(source, timeout=5, phrase_time_limit=None)
                
                self.signal_status.emit("thinking")
                self.signal_text.emit("Understanding...")
                
                command = r.recognize_google(audio, language="en-US")
                log_debug(f"Google SR: {command}")
                
                if command: 
                    self.signal_text.emit(f"🗣️ {command}")
                    self.process_command(command)
                else: self.speak("Could not understand.")
            except sr.WaitTimeoutError: log_debug("No audio.")
            except sr.UnknownValueError: self.speak("Could not distinguish.")
            except Exception as e: log_debug(f"Error: {e}")
            finally: self.signal_status.emit("idle")
        
        pygame.mixer.music.set_volume(1.0)

    # --- PROCESS COMMAND ---
    def process_command(self, text):
        now = datetime.now().strftime("%d %B %Y %H:%M") 
        user_name = self.mem.get_value("name") or self.mem.get_value("isim") or "User"
        long_term_mem = self.mem.get_relevant_memories()
        short_term_ctx = self.ctx.get_context_string()
        
        prompt = f"""
        [SYSTEM]
        Your name is Blink. You are talking to {user_name}.
        Current time: {now}.

        [CURRENT MEMORY]
        {long_term_mem}

        [RECENT CONVERSATION HISTORY]
        {short_term_ctx}

        [INPUT]
        User: "{text}"
        
        [TASK]
        Analyze the user's input and generate a JSON response.
        
        [CRITICAL RULES]
        1. CONTEXTUAL REFERENCE:
           - If the user says "Play something matching this", "How was that?", "How is tomorrow?", look at the conversation history.
           - E.g.: "I like purple" -> "Play something matching this" -> Target: "Purple themed music".

        2. REMINDER:
           - "in 10 seconds", "in 1 hour", "remind me in X" -> type: "reminder".
           - CALCULATE THE DURATION IN SECONDS. (E.g.: 1 hour = 3600).
        
        3. WEATHER:
           - "how is the weather" -> type: "weather", Target: City name.
        
        4. EVENTS:
           - "I have a presentation tomorrow" -> type: "memory", category: "events".
        
        5. FALLBACK:
           - "Open X" -> type: "app", target: "X". (If the system cannot find the app, it will search for music).

        FORMAT:
        {{
            "queue": [
                {{
                    "type": "music/app/memory/reminder/weather/system",
                    "action": "...",
                    "category": "prefs/status/events",
                    "seconds": 0,
                    "message": "...",
                    "key": "...", "value": "...",
                    "target": "...",
                    "response": "..."
                }}
            ]
        }}
        """
        try:
            self.ctx.add("user", text)
            resp = self.ai.models.generate_content(model="gemini-3-flash-preview", contents=prompt).text
            log_debug(f"AI Raw Response: {resp}")
            
            clean_json = resp.replace("```json","").replace("```","").strip()
            if "```" in clean_json: clean_json = clean_json.split("```")[0]
            
            data = json.loads(clean_json)
            queue = data.get("queue", [data] if "type" in data else [])
            
            if not queue: self.speak("Could not understand."); return

            for cmd in queue:
                self._execute_single_command(cmd, user_name)
                if cmd.get("response"):
                    self.ctx.add("ai", cmd.get("response"))
                time.sleep(1.5)
            
        except Exception as e: 
            log_debug(f"JSON/Action Error: {e}"); self.speak("An error occurred.")

    def _execute_single_command(self, data, user_name):
        try:
            typ = data.get("type")
            target = data.get("target") or data.get("name") or data.get("query")
            action = data.get("action")
            response_text = data.get("response")

            # --- 1. REMINDER (TIMER FIX) ---
            if typ == "reminder":
                secs = data.get("seconds", 0)
                msg = data.get("message", "Reminder")
                if secs > 0:
                    self.signal_start_timer.emit(secs, msg)
                    self.speak(f"Okay, I will remind you in {secs} seconds.")
                else:
                    self.speak("Could not understand the duration.")
                return

            # --- 2. WEATHER ---
            if typ == "weather":
                city = target if target else "Konya"
                self.handle_weather_smart(city)
                return

            # --- 3. SYSTEM / PYTHON ---
            if typ == "system" and action == "check_python":
                versions_text = self.sys.check_python_versions()
                self.speak(f"{response_text or ''} {versions_text}")
                self.ctx.add("system", versions_text) 
                return

            # --- 4. MUSIC ---
            if typ == "music":
                if action == "stop":
                    self.music.stop(); self.signal_music_stop.emit()
                    if response_text: self.speak(response_text)
                    return
                elif action in ["pause", "resume", "continue"]:
                    self.control_music("toggle")
                    if response_text: self.speak(response_text)
                    return
                elif action == "play":
                    if response_text: self.speak(response_text)
                    if target: self.start_music_download(target)
                    return

            if response_text: self.speak(response_text)

            # --- 5. APPLICATION (APP) + FALLBACK LOGIC ---
            if typ == "app" and target:
                result = self.sys.execute(action, target)
                if "NOT FOUND" in result:
                    log_debug(f"App not found ({target}), trying music instead...")
                    self.speak(f"Could not find the application {target}, playing as a song instead.")
                    self.start_music_download(target)
                else:
                    self.speak(result)
                
            # --- 6. MEMORY ---
            elif typ == "memory":
                k = data.get("key")
                v = data.get("value") or target
                cat = data.get("category", "general") 
                if k and v: self.mem.save(k, v, cat)

        except Exception as e: log_debug(f"Task Error: {e}")

    # --- WEATHER ---
    def handle_weather_smart(self, city):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            url = f"https://wttr.in/{city}?format=%t|%C&lang=en"
            
            response = requests.get(url, headers=headers, timeout=5)
            r = response.text.strip()
            
            if "|" not in r: 
                self.speak(f"Could not retrieve weather for {city}.")
                return

            temp_str, condition = r.split("|")
            temp = int(temp_str.replace("+", "").replace("°C", ""))
            
            suggestion = ""
            if "rain" in condition.lower():
                suggestion = "Take an umbrella."
            elif temp < 10:
                suggestion = "Wear a coat."
            elif 10 <= temp < 20:
                suggestion = "Take a jacket."
            elif temp >= 20:
                suggestion = "Wear a t-shirt."
            
            summary = f"{city} {temp} degrees, {condition.lower()}. {suggestion}"
            
            words = summary.split()
            if len(words) > 7: 
                summary = " ".join(words[:7])
            
            self.speak(summary)

        except Exception as e: 
            log_debug(f"Weather Error: {e}")
            self.speak("Could not reach the weather service.")

    def start_music_download(self, query):
        self.downloader = MusicDownloader(query)
        self.downloader.progress.connect(lambda p: self.signal_text.emit(p)) 
        self.downloader.finished_signal.connect(self.on_music_ready)
        self.downloader.start()

    def on_music_ready(self, meta):
        self.signal_music_start.emit(meta['title'], meta['thumbnail'], int(meta['duration']))
        self.music.play_file()
        self.signal_text.emit("Playing...")

    def process_manual_text(self, text):
        self.signal_status.emit("thinking")
        self.signal_text.emit(f"⌨️ {text}")
        self.process_command(text)
        self.signal_status.emit("idle")

    def control_music(self, action, value=None):
        if action == "toggle": self.music.toggle_pause()
        elif action == "seek": self.music.seek(value)

    def speak(self, text):
        self.signal_status.emit("speaking")
        self.signal_text.emit(text) 
        asyncio.run(self._tts(text))

    async def _tts(self, text):
        try:
            communicate = edge_tts.Communicate(text, "en-US-AriaNeural")
            await communicate.save("tts.mp3")
            s = pygame.mixer.Sound("tts.mp3"); s.play()
            start = time.time()
            while pygame.mixer.get_busy():
                self.signal_audio_level.emit(0.3 + (0.5 * abs(math.sin((time.time() - start) * 10))))
                time.sleep(0.05)
        except: pass

# ==========================================================
# 4. KEY INPUT WINDOW
# ==========================================================
class KeyInputWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Blink Setup")
        self.setFixedSize(400, 250)
        self.setStyleSheet("background-color: #121212; color: white;")
        layout = QVBoxLayout()
        form = QFormLayout()
        self.gemini_input = QLineEdit(); self.gemini_input.setStyleSheet("padding:5px; border:1px solid #555;")
        self.picovoice_input = QLineEdit(); self.picovoice_input.setStyleSheet("padding:5px; border:1px solid #555;")
        form.addRow("Gemini API:", self.gemini_input)
        form.addRow("Picovoice Key:", self.picovoice_input)
        layout.addLayout(form)
        self.btn = QPushButton("Save"); self.btn.clicked.connect(self.save)
        self.btn.setStyleSheet("background: #1f6aa5; padding:8px;")
        layout.addWidget(self.btn)
        self.setLayout(layout)
    def save(self):
        save_keys_to_disk(self.gemini_input.text(), self.picovoice_input.text())
        self.close(); self.main = BlinkOverlay(); self.main.show()

# ==========================================================
# 5. MAIN APPLICATION (BLINK UI)
# ==========================================================
class BlinkOverlay(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(100, 100, 450, 700)
        
        self.cw = QWidget(); self.setCentralWidget(self.cw)
        self.lay = QVBoxLayout(self.cw)
        self.cw.setStyleSheet("background: rgba(10, 10, 20, 240); border-radius: 25px; border: 1px solid #444;")

        self.viz = SiriVisualizerWidget()
        self.lay.addWidget(self.viz, alignment=Qt.AlignCenter)

        self.lbl_text = QLabel("Initializing..."); 
        self.lbl_text.setStyleSheet("color: white; font: 13pt 'Segoe UI'; background: transparent;")
        self.lbl_text.setAlignment(Qt.AlignCenter); self.lbl_text.setWordWrap(True)
        self.lay.addWidget(self.lbl_text)

        # Müzik Paneli
        self.music_frame = QFrame()
        self.music_frame.setStyleSheet("background: rgba(0,0,0,100); border-radius: 15px; margin: 10px;")
        self.music_frame.setVisible(False)
        m_lay = QVBoxLayout(self.music_frame)
        h_lay = QHBoxLayout()
        self.img_cover = QLabel(); self.img_cover.setFixedSize(60, 60); self.img_cover.setStyleSheet("background: #333; border-radius: 5px;")
        h_lay.addWidget(self.img_cover)
        self.lbl_song = QLabel("Song Title"); self.lbl_song.setStyleSheet("color: white; font-weight: bold; border: none; background: transparent;")
        h_lay.addWidget(self.lbl_song)
        m_lay.addLayout(h_lay)
        self.slider = QSlider(Qt.Horizontal); self.slider.setStyleSheet("QSlider::handle:horizontal {background-color: #1f6aa5;}")
        self.slider.sliderReleased.connect(self.on_seek); m_lay.addWidget(self.slider)
        self.btn_play = QPushButton("⏸ Pause"); self.btn_play.clicked.connect(self.on_play_toggle)
        self.btn_play.setStyleSheet("background: #1f6aa5; color: white; border: none; padding: 5px; border-radius: 5px;")
        m_lay.addWidget(self.btn_play)
        self.lay.addWidget(self.music_frame)

        # Chat Butonu
        btn_layout = QHBoxLayout(); btn_layout.addStretch()
        self.btn_chat = QPushButton("💬"); self.btn_chat.setFixedSize(40, 40)
        self.btn_chat.setStyleSheet("background: #1f6aa5; color: white; border-radius: 20px;")
        self.btn_chat.clicked.connect(self.open_chat)
        btn_layout.addWidget(self.btn_chat)
        self.lay.addLayout(btn_layout)

        # Timerlar
        self.idle_timer = QTimer(); self.idle_timer.setInterval(5000)
        self.idle_timer.timeout.connect(self.fade_out); self.idle_timer.start()
        self.music_timer = QTimer(); self.music_timer.setInterval(1000)
        self.music_timer.timeout.connect(self.update_slider_ui)
        self.typewriter_timer = QTimer(); self.typewriter_timer.setInterval(30) 
        self.typewriter_timer.timeout.connect(self.update_typewriter)
        self.target_text = ""; self.current_text_idx = 0
        self.fade_anim = QPropertyAnimation(self, b"windowOpacity"); self.fade_anim.setDuration(500)

        # Worker
        self.worker = BackendWorker()
        self.worker.signal_status.connect(self.handle_status)
        self.worker.signal_text.connect(self.start_typewriter) 
        self.worker.signal_audio_level.connect(self.viz.set_amplitude)
        self.worker.signal_music_start.connect(self.show_music_ui)
        self.worker.signal_music_stop.connect(self.hide_music_ui)
        self.worker.signal_reminder_trigger.connect(self.show_reminder)
        # Sinyali Ana Thread Slot'una bağla
        self.worker.signal_start_timer.connect(self.schedule_reminder)
        
        self.worker.start()
        self.old_pos = None

    # --- ANA THREAD TIMER SLOT ---
    def schedule_reminder(self, secs, msg):
        QTimer.singleShot(secs * 1000, lambda: self.trigger_alert(msg))

    def trigger_alert(self, msg):
        self.handle_status("wake")
        self.start_typewriter(f"⏰ {msg.upper()}")
        self.worker.speak(f"Hatırlatma: {msg}")

    def show_music_ui(self, title, thumb_url, duration):
        self.lbl_song.setText(title[:25] + "..." if len(title)>25 else title)
        self.slider.setMaximum(duration); self.slider.setValue(0)
        try:
            data = requests.get(thumb_url).content; pix = QPixmap(); pix.loadFromData(data)
            self.img_cover.setPixmap(pix.scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except: pass
        self.music_frame.setVisible(True); self.music_timer.start(); self.btn_play.setText("⏸ Durdur")

    def hide_music_ui(self): self.music_frame.setVisible(False); self.music_timer.stop()
    def update_slider_ui(self):
        if self.music_frame.isVisible() and "Durdur" in self.btn_play.text(): self.slider.setValue(self.slider.value() + 1)
    def on_seek(self): self.worker.control_music("seek", self.slider.value())
    def on_play_toggle(self):
        if "Durdur" in self.btn_play.text(): self.btn_play.setText("▶ Oynat"); self.worker.control_music("toggle")
        else: self.btn_play.setText("⏸ Durdur"); self.worker.control_music("toggle")

    def show_reminder(self, msg):
        self.trigger_alert(msg)

    def start_typewriter(self, text):
        self.target_text = text; self.current_text_idx = 0; self.lbl_text.setText("")
        self.typewriter_timer.start(); self.reset_idle() 
    def update_typewriter(self):
        if self.current_text_idx < len(self.target_text): self.current_text_idx += 1; self.lbl_text.setText(self.target_text[:self.current_text_idx])
        else: self.typewriter_timer.stop()
    def handle_status(self, status):
        self.viz.set_mode(status)
        if status == "idle": self.idle_timer.start() 
        else: self.reset_idle() 
    def reset_idle(self): self.idle_timer.stop(); self.fade_in()
    def fade_out(self): self.fade_anim.stop(); self.fade_anim.setStartValue(self.windowOpacity()); self.fade_anim.setEndValue(0.0); self.fade_anim.start()
    def fade_in(self): self.fade_anim.stop(); self.fade_anim.setStartValue(self.windowOpacity()); self.fade_anim.setEndValue(1.0); self.fade_anim.start()
    def open_chat(self):
        text, ok = QInputDialog.getText(self, "Blink Chat", "What is your command?")
        if ok and text: threading.Thread(target=self.worker.process_manual_text, args=(text,)).start()
    def mousePressEvent(self, e): self.old_pos = e.globalPos()
    def mouseMoveEvent(self, e):
        if self.old_pos: delta = QPoint(e.globalPos() - self.old_pos); self.move(self.x() + delta.x(), self.y() + delta.y()); self.old_pos = e.globalPos()
    def closeEvent(self, e): self.worker.running = False

if __name__ == "__main__":
    app = QApplication(sys.argv)
    if load_keys(): win = BlinkOverlay(); win.show()
    else: setup_win = KeyInputWindow(); setup_win.show()
    sys.exit(app.exec_())