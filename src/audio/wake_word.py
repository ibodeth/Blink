"""Wake-word detection + command orchestration (BackendWorker thread).

Major upgrades vs. legacy:

* **English-only** local keyword dispatch (Turkish branches removed).
* **Overflow-safe** microphone reads via :func:`src.audio.speech.safe_read`
  with automatic input-stream recovery on repeated failures.
* **Offline fallback** (feature 6C): when the AI service is unreachable we run
  the local keyword engine and announce the exact offline message.
* **Persistent conversation memory** (feature 6A): the last 20 turns are
  replayed into the AI context on boot and every new turn is persisted.
* **Thread-safe state**: callback counters and processing flags are guarded by
  locks; the wake-word loop is resilient to transient audio errors.
* Structured logging throughout.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime

import numpy as np

from src.ai import AIServiceError, ContextManager, OfflineError, OpenRouterClient
from src.audio.music import MusicEngine
from src.audio.speech import recognize_speech_from_mic, safe_read, speak_text_async
from src.database import ConversationStore, MemoryManager
from src.scanner import load_cache, run_scan_and_cache
from src.skills import SkillManager
from src.system import FFmpegManager, SystemManager
from src.utils import get_logger

log = get_logger(__name__)

OFFLINE_MESSAGE = "I am offline, but I have completed your local command."
SAMPLE_RATE = 16000
BLOCKSIZE = 1280


class BackendWorker(threading.Thread):
    def __init__(self, broadcast_fn=None):
        super().__init__()
        self.daemon = True
        self.running = True
        self.broadcast_fn = broadcast_fn

        self.ff = FFmpegManager()
        self.music = MusicEngine(event_callback=self.send_event)
        self.mem = MemoryManager()
        self.conversation = ConversationStore()
        self.sys = SystemManager()
        self.ctx = ContextManager()
        self.ai = OpenRouterClient()
        self.skills = SkillManager()
        self.current_lang = "en"
        self.oww_model = None

        self.workspace_cache = load_cache()

        # Replay persisted conversation into the AI context (feature 6A).
        try:
            recent = self.conversation.load_recent(20)
            if recent:
                self.ctx.seed(recent)
                log.info("Restored %d conversation turns from memory.", len(recent))
        except Exception as exc:
            log.warning("Could not restore conversation history: %s", exc)

        # Thread-safe state.
        self._state_lock = threading.Lock()
        self._is_sleeping = True
        self._is_processing = False
        self._callback_depth = 0
        self._active_callbacks = 0
        self._callback_lock = threading.Lock()
        self._watchdog_timer = None
        os.makedirs(os.path.join(os.getcwd(), "agentools"), exist_ok=True)

    # ------------------------------------------------------------------
    # Watchdog / idle management
    # ------------------------------------------------------------------
    def _start_watchdog(self, timeout_secs: int = 45) -> None:
        self._cancel_watchdog()

        def _watchdog():
            log.warning("Watchdog firing after %ss - force-resetting to idle.", timeout_secs)
            with self._callback_lock:
                self._active_callbacks = 0
            self.send_event("status", {"status": "idle"})
            self.send_event("text", {"text": "Sleeping Mode..."})

        self._watchdog_timer = threading.Timer(timeout_secs, _watchdog)
        self._watchdog_timer.daemon = True
        self._watchdog_timer.start()

    def _cancel_watchdog(self) -> None:
        if self._watchdog_timer is not None:
            self._watchdog_timer.cancel()
            self._watchdog_timer = None

    def _reset_idle_if_done(self) -> None:
        with self._callback_lock:
            if self._active_callbacks <= 0:
                self._active_callbacks = 0
                self._cancel_watchdog()
                self.send_event("status", {"status": "idle"})

    @property
    def is_sleeping(self) -> bool:
        with self._state_lock:
            return self._is_sleeping

    def _set_sleeping(self, value: bool) -> None:
        with self._state_lock:
            self._is_sleeping = value

    # ------------------------------------------------------------------
    # Persisted-context helpers
    # ------------------------------------------------------------------
    def _remember(self, role: str, text: str) -> None:
        """Add a turn to both short-term context and persistent storage."""
        if not text:
            return
        self.ctx.add(role, text)
        try:
            self.conversation.add(role, text)
        except Exception as exc:
            log.debug("Conversation persist failed: %s", exc)

    # ------------------------------------------------------------------
    # Local (offline) keyword dispatch - English only
    # ------------------------------------------------------------------
    def _try_local_dispatch(self, text: str) -> bool:
        t = text.lower().strip()
        for p in [".", ",", "?", "!", "'", '"']:
            t = t.replace(p, "")
        t = t.strip()
        self.current_lang = "en"

        # Wi-Fi toggle
        if any(x in t for x in ["wifi on", "turn on wifi", "enable wifi"]):
            self.sys.set_wifi_state(True)
            reply = "Wi-Fi is turned on."
            self.speak(reply)
            self._remember("blink", reply)
            return True
        if any(x in t for x in ["wifi off", "turn off wifi", "disable wifi"]):
            self.sys.set_wifi_state(False)
            reply = "Wi-Fi is turned off."
            self.speak(reply)
            self._remember("blink", reply)
            return True

        # Bluetooth toggle
        if any(x in t for x in ["bluetooth on", "turn on bluetooth", "enable bluetooth"]):
            self.sys.set_bluetooth_state(True)
            reply = "Bluetooth is turned on."
            self.speak(reply)
            self._remember("blink", reply)
            return True
        if any(x in t for x in ["bluetooth off", "turn off bluetooth", "disable bluetooth"]):
            self.sys.set_bluetooth_state(False)
            reply = "Bluetooth is turned off."
            self.speak(reply)
            self._remember("blink", reply)
            return True

        # Wi-Fi status
        if any(x in t for x in ["wifi status", "wifi check", "am i connected", "is wifi on"]):
            self.sys.get_all_telemetry()
            w = self.sys.cached_telemetry.get("wifi", {})
            if w.get("adapter_enabled", False):
                ssid = w.get("connected_ssid", "")
                reply = (
                    f"Wi-Fi is enabled and connected to network {ssid}."
                    if ssid else "Wi-Fi is enabled but not connected to any network."
                )
            else:
                reply = "Wi-Fi is disabled."
            self.speak(reply)
            self._remember("blink", reply)
            return True

        # Battery status
        if any(x in t for x in ["battery status", "battery level", "how much battery", "battery"]):
            self.sys.get_all_telemetry()
            b = self.sys.cached_telemetry.get("stats", {}).get("battery", {})
            pct = b.get("percent", 100)
            reply = (
                f"Battery is at {pct}% and currently charging."
                if b.get("power_plugged", True) else f"Battery level is {pct}%."
            )
            self.speak(reply)
            self._remember("blink", reply)
            return True

        # Volume status
        if any(x in t for x in ["volume level", "what is the volume", "volume percent"]):
            self.sys.get_all_telemetry()
            a = self.sys.cached_telemetry.get("audio", {})
            vol = a.get("volume", 50)
            reply = (
                f"Volume is at {vol}% but it is currently muted."
                if a.get("muted", False) else f"Volume is at {vol}%."
            )
            self.speak(reply)
            self._remember("blink", reply)
            return True

        # Music pause/resume/stop/now-playing
        if t in ["pause music", "pause"]:
            state = self.control_music("toggle")
            reply = "Music paused." if state == "paused" else "Music resumed."
            self.speak(reply)
            self._remember("blink", reply)
            return True
        if t in ["resume music", "resume", "continue music"]:
            state = self.control_music("toggle")
            reply = "Music resumed." if state == "playing" else "Music paused."
            self.speak(reply)
            self._remember("blink", reply)
            return True
        if t in ["stop music", "stop the music"]:
            self.control_music("stop")
            reply = "Music stopped."
            self.speak(reply)
            self._remember("blink", reply)
            return True
        if any(x in t for x in ["what is playing", "what song is this", "whats playing", "current song"]):
            title = self.music._current_title
            reply = f"Currently playing: {title}." if title else "There is no music playing right now."
            self.speak(reply)
            self._remember("blink", reply)
            return True

        # Music skip/back local dispatch (saves LLM tokens & latency)
        if any(x == t for x in ["next", "next track", "next song", "skip", "skip song"]):
            self.music.play_next()
            reply = "Playing next song."
            self.speak(reply)
            self._remember("blink", reply)
            return True

        if any(x == t for x in ["previous", "previous track", "previous song", "back", "go back", "last song"]):
            self.music.play_previous()
            reply = "Playing previous song."
            self.speak(reply)
            self._remember("blink", reply)
            return True

        return False

    # ------------------------------------------------------------------
    # Local history query dispatch (no AI needed)
    # ------------------------------------------------------------------
    def _try_history_dispatch(self, text: str) -> bool:
        """Return True and emit history if the user asks for recent messages.

        Detects English and Turkish history-read phrases and serves the
        conversation history directly from ``self.ctx.history`` without
        making any AI network call.
        """
        import re
        t_lower = text.lower().strip()

        # Patterns that indicate a history / recent-messages request.
        # Turkish: "son N mesaj", "son N konuşma", "geçmiş", "önceki mesaj", "ne dedin"
        # English: "last N messages", "past N messages", "conversation history", "what did you say"
        history_patterns = [
            r"son\s+(\d+)\s*(mesaj|konuşma|konu\s*şma|şey|cevap)",
            r"(last|past|previous|recent)\s+(\d+)\s*(message|messages|chat|conversation|turn|turns)",
            r"(whats?|what\s+is|show|list|read|tell)\s+(the\s+)?(last|recent|previous)\s+(\d+)\s*(message|messages|chat|conversation)",
            r"conversation\s*history",
            r"geçmi[sş]\s*(mesaj|konu[sş]ma)",
            # Single turn / previous references
            r"(bir\s+)?önceki\s*(mesaj|konuşma|şey|cevap)",
            r"son\s*(mesaj|konuşma|şey|cevap)",
            r"(ne\s+dedik|ne\s+dedin|ne\s+söyledik|ne\s+söyledin|ne\s+yazdık|ne\s+yazdın|ne\s+yaptık|ne\s+yaptın|ne\s+oldu)",
            r"(what\s+did\s+we\s+say|what\s+did\s+you\s+say|what\s+was\s+the\s+last\s+thing|previous\s+message|last\s+message|what\s+did\s+we\s+do|what\s+did\s+you\s+do|what\s+happened)",
        ]

        n_requested = None
        matched = False
        is_single_turn_query = False

        for pattern in history_patterns:
            m = re.search(pattern, t_lower)
            if m:
                matched = True
                # Check if it is one of the single-turn patterns
                if pattern in history_patterns[5:]:
                    is_single_turn_query = True
                # Try to extract a number from the match groups.
                for g in m.groups():
                    if g and g.isdigit():
                        n_requested = int(g)
                        is_single_turn_query = False
                        break
                break

        if not matched:
            return False

        # Fetch history from context manager.
        history = self.ctx.history
        if not history:
            reply = "I have no conversation history yet."
            self.speak(reply)
            self._remember("blink", reply)
            self.send_event("blink_message", {"text": reply})
            return True

        # Slice to the requested number of turns from the END.
        if n_requested is not None and n_requested > 0:
            turns = history[-n_requested:]
        elif is_single_turn_query:
            turns = history[-2:]  # Show the last user query and blink response
        else:
            turns = history[-10:]  # default: last 10

        lines = [f"{t['role']}: {t['text']}" for t in turns]
        reply = "Here are the last {} message{}:\n".format(
            len(lines), "s" if len(lines) != 1 else ""
        ) + "\n".join(lines)

        self.speak(f"Here are the last {len(lines)} messages from our conversation.")
        self.send_event("blink_message", {"text": reply})
        self._remember("blink", f"[Showed last {len(lines)} messages from history]")
        return True

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------
    def send_event(self, event_type: str, data_dict: dict | None = None) -> None:
        if self.broadcast_fn:
            payload = {"type": event_type}
            if data_dict:
                payload.update(data_dict)
            self.broadcast_fn(payload)

    def calculate_rms_np(self, audio_np: np.ndarray) -> float:
        try:
            floats = audio_np.astype(np.float32) / 32768.0
            return min(float(np.sqrt(np.mean(floats ** 2))) * 5, 1.0)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self) -> None:
        import sounddevice as sd
        from openwakeword.model import Model

        def telemetry_loop():
            log.info("Starting telemetry loop thread...")
            try:
                self.sys.get_all_telemetry()
            except Exception as exc:
                log.warning("Initial telemetry error: %s", exc)
            while self.running:
                try:
                    telemetry = self.sys.get_all_telemetry()
                    self.send_event("system_telemetry", {"telemetry": telemetry})
                except Exception as exc:
                    log.warning("Telemetry loop error: %s", exc)
                interval = 30.0 if self.is_sleeping else 5.0
                elapsed = 0.0
                while elapsed < interval and self.running:
                    time.sleep(0.2)
                    elapsed += 0.2
                    if not self.is_sleeping and interval == 30.0:
                        break

        threading.Thread(target=telemetry_loop, daemon=True).start()

        self.send_event("status", {"status": "initializing"})

        self.send_event("text", {"text": "Loading Text-to-Speech Engine..."})
        try:
            from src.audio.speech import get_kokoro
            def progress(filename, pct):
                self.send_event("text", {"text": f"Downloading {filename}: {pct}%"})
            get_kokoro(progress)
        except Exception as exc:
            log.warning("Error preloading Kokoro: %s", exc)

        self.send_event("text", {"text": "Loading Speech-to-Text Module..."})
        try:
            from src.audio.speech import get_whisper_model
            get_whisper_model()
        except Exception as exc:
            log.warning("Error preloading Whisper: %s", exc)

        self.send_event("text", {"text": "Loading Wake Word Detector..."})
        stream = None
        try:
            log.info("Loading openWakeWord model 'hey_jarvis'...")
            self.oww_model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")

            self.send_event("text", {"text": "Scanning workspace & installed apps..."})

            def _do_scan():
                try:
                    self.workspace_cache = run_scan_and_cache()
                except Exception as scan_err:
                    log.warning("Scan error: %s", scan_err)

            threading.Thread(target=_do_scan, daemon=True).start()

            user_name = self.mem.get_value("name")
            greet = f"Hello again {user_name}!" if user_name else "Hello!"
            self.speak(greet)

            def _open_stream():
                s = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=BLOCKSIZE)
                s.start()
                return s

            stream = _open_stream()
            self._set_sleeping(True)
            self.send_event("status", {"status": "idle"})
            self.send_event("text", {"text": "Sleeping Mode..."})

            _level_tick = 0
            consecutive_errors = 0

            while self.running:
                pcm, overflowed = safe_read(stream, BLOCKSIZE)
                if overflowed:
                    consecutive_errors += 1
                    if consecutive_errors >= 25:
                        log.warning("Recovering input stream after repeated overflows.")
                        try:
                            stream.stop(); stream.close()
                        except Exception:
                            pass
                        stream = _open_stream()
                        consecutive_errors = 0
                        continue
                else:
                    consecutive_errors = 0

                audio_np = pcm.flatten()
                rms = self.calculate_rms_np(audio_np)

                if self.is_sleeping:
                    _level_tick += 1
                    if _level_tick >= 13:
                        _level_tick = 0
                        self.send_event("audio_level", {"level": rms})
                else:
                    self.send_event("audio_level", {"level": rms})

                # OWW is a streaming model; every frame must be fed.
                try:
                    prediction = self.oww_model.predict(audio_np)
                except Exception as exc:
                    log.debug("OWW predict error: %s", exc)
                    continue

                if prediction.get("hey_jarvis", 0.0) > 0.5:
                    log.info("Wake word 'Hey Jarvis' detected.")
                    with self._callback_lock:
                        busy = (self._active_callbacks > 0) or self._is_processing
                    if busy:
                        log.debug("Already processing; ignoring wake word.")
                        self.oww_model.reset()
                        continue

                    self._set_sleeping(False)
                    self.send_event("wake_word", {})
                    self.active_listen(stream)

                    self._set_sleeping(True)
                    self.send_event("status", {"status": "idle"})
                    self.send_event("text", {"text": "Sleeping Mode..."})
                    self.send_event("sleep", {})
                    self.oww_model.reset()
                    _level_tick = 0
                    try:
                        stream.stop(); stream.start()
                    except Exception as se:
                        log.debug("Warning restarting stream: %s", se)
        except Exception as exc:
            self.send_event("text", {"text": f"Error: {exc}"})
            log.error("Wake word thread exception: %s", exc)
        finally:
            if stream is not None:
                try:
                    stream.stop(); stream.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Active listening
    # ------------------------------------------------------------------
    def active_listen(self, stream) -> None:
        with self._callback_lock:
            self._is_processing = True
        self._set_sleeping(False)
        self.send_event("status", {"status": "listening"})
        self.send_event("text", {"text": "Listening..."})

        music_was_playing = self.music.is_busy()
        if music_was_playing:
            self.music.set_volume(0.2)

        try:
            try:
                stream.stop(); stream.start()
            except Exception as se:
                log.debug("Warning flushing stream: %s", se)

            command = recognize_speech_from_mic(
                stream=stream, timeout=6,
                level_callback=lambda lvl: self.send_event("audio_level", {"level": lvl}),
            )
            log.info("Whisper transcription: '%s'", command)

            self.send_event("status", {"status": "thinking"})
            self.send_event("text", {"text": "Understanding..."})

            if command and command.strip():
                self.send_event("text", {"text": f"\U0001f5e3\ufe0f {command}"})
                self._callback_depth = 0
                self.process_command(command)
            else:
                self.speak("Could not hear you. Please try again.")
        except Exception as exc:
            log.error("active_listen error: %s", exc)
            self.speak("An error occurred.")
        finally:
            with self._callback_lock:
                self._is_processing = False
            self._reset_idle_if_done()
            if music_was_playing:
                self.music.set_volume(1.0)

    # ------------------------------------------------------------------
    # Command processing
    # ------------------------------------------------------------------
    def process_command(self, text: str) -> None:
        self.send_event("status", {"status": "thinking"})

        # History query interception (no AI call needed).
        if self._try_history_dispatch(text):
            self._reset_idle_if_done()
            return

        # Local keyword matching first (saves tokens & latency).
        if self._try_local_dispatch(text):
            self._reset_idle_if_done()
            return

        self._remember("user", text)

        # Inject running process list for close/kill intents.
        ai_input_text = text
        if any(kw in text.lower() for kw in ["close", "kill", "exit", "quit", "terminate", "stop"]):
            try:
                import psutil
                procs = sorted({p.info["name"] for p in psutil.process_iter(["name"]) if p.info["name"]})
                ai_input_text = text + "\n[RUNNING PROCESSES]: " + ", ".join(procs)
            except Exception as pe:
                log.debug("Process list injection failed: %s", pe)

        now_str = datetime.now().strftime("%d %B %Y %H:%M")
        user_name = self.mem.get_value("name") or "User"
        long_term_mem = self.mem.get_relevant_memories()
        short_term_ctx = self.ctx.history

        active_music_info = None
        if self.music and self.music._current_title:
            m_state = "paused" if self.music.is_paused else (
                "playing" if self.music.is_busy() or self.music._stream is not None else "stopped"
            )
            if m_state != "stopped":
                active_music_info = {
                    "title": self.music._current_title,
                    "duration": self.music._current_duration,
                    "progress": int(self.music.current_seconds),
                    "state": m_state,
                }

        system_telemetry = getattr(self.sys, "cached_telemetry", None)
        cleaned_telemetry = ""
        if system_telemetry:
            try:
                # Build a clean dictionary without deepcopying objects
                tel_copy = {}
                for k, v in system_telemetry.items():
                    if k == "processes":
                        continue
                    if k == "audio" and isinstance(v, dict):
                        audio_copy = {ak: av for ak, av in v.items() if ak != "devices"}
                        tel_copy["audio"] = audio_copy
                    elif k == "stats" and isinstance(v, dict):
                        # Filter out any non-serializable battery enum values if present
                        stats_copy = {}
                        for sk, sv in v.items():
                            if sk == "battery" and isinstance(sv, dict):
                                bat_copy = {}
                                for bk, bv in sv.items():
                                    if bk == "secsleft":
                                        try:
                                            bat_copy[bk] = getattr(bv, "value", int(bv))
                                        except Exception:
                                            bat_copy[bk] = str(bv)
                                    else:
                                        bat_copy[bk] = bv
                                stats_copy["battery"] = bat_copy
                            else:
                                stats_copy[sk] = sv
                        tel_copy["stats"] = stats_copy
                    else:
                        tel_copy[k] = v
                cleaned_telemetry = str(tel_copy)
            except Exception as e:
                log.debug("Error cleaning telemetry for prompt: %s", e)
                cleaned_telemetry = str(system_telemetry)

        # Minimize workspace cache representation to save context tokens
        concise_workspace = ""
        if self.workspace_cache:
            files = self.workspace_cache.get("workspace_files", [])
            files_limit = files[:15]
            files_str = ", ".join(files_limit)
            if len(files) > 15:
                files_str += f" ... ({len(files) - 15} more files)"
            concise_workspace = f"Files: {files_str}\nInstalled Apps: (Available on system)"

        agent_tools_info = None
        agent_md_path = os.path.join(os.getcwd(), "agent.md")
        if os.path.exists(agent_md_path):
            try:
                with open(agent_md_path, "r", encoding="utf-8") as fh:
                    agent_tools_info = fh.read().strip()
            except OSError as exc:
                log.debug("Error reading agent.md: %s", exc)

        has_callback = False
        try:
            skills_catalog = self.skills.catalog()
            data = self.ai.generate_command(
                ai_input_text, user_name, long_term_mem, short_term_ctx, now_str,
                str(active_music_info or ""), cleaned_telemetry,
                str(agent_tools_info or ""),
                workspace_cache=concise_workspace,
                skills_catalog=skills_catalog,
            )
            queue = data.get("queue", [])

            # Progressive disclosure: if the model asks to load a skill, fetch
            # that skill's full instructions and re-run the turn ONCE with them
            # in context, then execute the resulting commands. Guarded to a
            # single expansion so a misbehaving model cannot loop forever.
            skill_req = next((c for c in queue if c.get("type") == "skill"), None)
            if skill_req is not None:
                skill_name = skill_req.get("name") or skill_req.get("target")
                body = self.skills.get(skill_name) if skill_name else None
                if body:
                    log.info("Loading skill '%s' (progressive disclosure).", skill_name)
                    data = self.ai.generate_command(
                        ai_input_text, user_name, long_term_mem, short_term_ctx, now_str,
                        str(active_music_info or ""), cleaned_telemetry,
                        str(agent_tools_info or ""),
                        workspace_cache=concise_workspace,
                        skills_catalog=skills_catalog,
                        skill_instructions=body,
                    )
                    queue = data.get("queue", [])
                else:
                    log.warning("Model requested unknown skill '%s'; ignoring.", skill_name)
                # Never execute a leftover skill-request command.
                queue = [c for c in queue if c.get("type") != "skill"]

            if not queue:
                self.speak("I didn't understand. Could you rephrase?")
                return

            has_callback = any(cmd.get("type") == "callback" for cmd in queue)
            for i, cmd in enumerate(queue):
                spoken = self._execute_single_command(cmd, user_name)
                if spoken:
                    self._remember("blink", spoken)
                elif cmd.get("response"):
                    self._remember("blink", cmd["response"])
                if i < len(queue) - 1:
                    time.sleep(0.8)

        except OfflineError as exc:
            # Feature 6C: degrade gracefully to local hardware commands.
            log.warning("AI offline; attempting local dispatch: %s", exc)
            handled = self._try_local_dispatch(text)
            self.speak(OFFLINE_MESSAGE)
            self._remember("blink", OFFLINE_MESSAGE)
            if not handled:
                log.info("Offline and no local command matched for: %s", text)
        except AIServiceError as exc:
            log.error("AI command execution error: %s", exc)
            self.speak("An error occurred while processing your request.")
        except Exception as exc:
            log.error("Unexpected command error: %s", exc)
            self.speak("An error occurred while processing your request.")
        finally:
            if not has_callback:
                self.send_event("status", {"status": "idle"})

    def _execute_single_command(self, data: dict, user_name: str):
        spoken = None
        try:
            typ = data.get("type")
            target = data.get("target") or data.get("name") or data.get("query")
            action = data.get("action")
            response_text = data.get("response")
            self.current_lang = "en"

            if typ == "chat":
                if response_text:
                    self.speak(response_text)
                    spoken = response_text
                return spoken

            if typ == "save_skill":
                # The assistant teaches itself a new, reusable skill.
                slug = self.skills.save_skill(
                    name=data.get("name") or target or "",
                    description=data.get("description") or "",
                    instructions=data.get("instructions") or data.get("code") or "",
                    keywords=data.get("keywords"),
                )
                msg = response_text or (
                    "I've saved that as a new skill." if slug
                    else "I couldn't save that skill."
                )
                self.speak(msg)
                return msg

            if typ == "skill":
                # Safety net: skill requests are normally expanded before this
                # point. If one slips through, just acknowledge and move on.
                if response_text:
                    self.speak(response_text)
                    return response_text
                return None

            if typ == "callback":
                if response_text:
                    self.speak(response_text)
                    spoken = response_text
                code_target = data.get("code") or target
                if code_target:
                    if self._callback_depth >= 6:
                        log.warning("Max callback depth reached. Aborting loop.")
                        self.speak("Execution depth limit reached.")
                        return spoken
                    log.info("Triggering autonomous callback (depth %d).", self._callback_depth + 1)
                    with self._callback_lock:
                        self._active_callbacks += 1
                        self._callback_depth += 1
                    self._start_watchdog(45)

                    def _run_callback(cb_target):
                        try:
                            # Execute the code on the host and capture its output.
                            # Detect whether the target is a Python snippet or a
                            # shell command.
                            is_python = any(
                                x in cb_target
                                for x in ["import ", "print(", "def ", "class "]
                            ) or "\n" in cb_target
                            if is_python:
                                exec_result = self.sys.execute("execute_python", cb_target)
                            else:
                                exec_result = self.sys.execute("run_command", cb_target)

                            # Feed the *result* back to the LLM (not the raw code),
                            # so it can formulate a response without re-emitting a
                            # callback and looping forever.
                            self.process_command(f"[CALLBACK RESULT]: {exec_result}")
                        except Exception as cb_e:
                            log.error("Callback thread error: %s", cb_e)
                        finally:
                            with self._callback_lock:
                                self._active_callbacks -= 1
                            self._reset_idle_if_done()

                    threading.Thread(target=_run_callback, args=(code_target,), daemon=True).start()
                return spoken

            if typ == "reminder":
                secs = data.get("seconds", 0)
                msg = data.get("message", "Reminder")
                if secs and int(secs) > 0:
                    self.schedule_reminder(int(secs), msg)
                    confirm = response_text or f"Okay, reminder set for {secs} seconds."
                    self.speak(confirm)
                    spoken = confirm
                else:
                    self.speak("Could not understand the duration. Please say how many minutes or hours.")
                return spoken

            if typ == "weather":
                self.handle_weather_smart(target or "Istanbul")
                return spoken

            if typ == "system":
                if action == "check_python":
                    versions_text = self.sys.check_python_versions()
                    reply = f"{response_text or ''} {versions_text}".strip()
                    self.speak(reply)
                    self._remember("system", versions_text)
                    spoken = reply
                elif action in ["run_command", "execute_python"]:
                    if response_text:
                        self.speak(response_text)
                    result = self.sys.execute(action, data.get("code") or target)
                    log.info("Command output: %s", result)
                    self.send_event("text", {"text": f"Output:\n{result}"})
                    if not response_text:
                        self.speak("Command completed.")
                    self._remember("system", result)
                    spoken = response_text or result
                elif action == "kill_process":
                    result = self.sys.kill_process(target)
                    self.speak(result)
                    spoken = result
                elif action == "toggle_wifi":
                    result = self.sys.set_wifi_state(target == "on")
                    self.speak(result)
                    spoken = result
                elif action == "toggle_bluetooth":
                    result = self.sys.set_bluetooth_state(target == "on")
                    self.speak(result)
                    spoken = result
                elif action == "mute":
                    result = self.sys.execute("mute", None)
                    self.speak(result)
                    spoken = result
                else:
                    result = self.sys.execute(action, target)
                    self.speak(result)
                    spoken = result
                return spoken

            if typ == "music":
                effective_action = action if action else "play"
                if effective_action == "stop":
                    self.music.stop()
                    self.send_event("music_stop", {})
                    reply = response_text or "Music stopped."
                    self.speak(reply)
                    spoken = reply
                elif effective_action in ["pause", "resume", "continue"]:
                    state = self.control_music("toggle")
                    self.send_event("music_state_changed", {"state": state})
                    reply = response_text or state
                    self.speak(reply)
                    spoken = reply
                elif effective_action == "next":
                    self.control_music("next")
                    reply = response_text or "Playing next song."
                    self.speak(reply)
                    spoken = reply
                elif effective_action == "previous":
                    self.control_music("previous")
                    reply = response_text or "Playing previous song."
                    self.speak(reply)
                    spoken = reply
                else:
                    if not response_text and target:
                        response_text = f"Okay, playing {target}."
                    if response_text:
                        self.speak(response_text)
                        spoken = response_text
                    if target:
                        self.music.play_query(target)
                return spoken

            if typ == "app" and target:
                result = self.sys.execute("open", target)
                if "Could not find" in result:
                    log.info("App not found: %s", target)
                    reply = f"Could not find {target}."
                else:
                    reply = response_text or result
                self.speak(reply)
                spoken = reply
                return spoken

            if typ == "memory":
                k = data.get("key")
                v = data.get("value") or target
                cat = data.get("category", "general")
                mem_action = data.get("action", "save")
                if mem_action == "delete":
                    if k:
                        if k == "*":
                            self.mem.clear_all()
                        else:
                            self.mem.delete(k)
                        if response_text:
                            self.speak(response_text)
                            spoken = response_text
                else:
                    if k and v:
                        self.mem.save(k, v, cat)
                        if response_text:
                            self.speak(response_text)
                            spoken = response_text
                return spoken

            log.info("Unknown command type '%s' - treating as chat.", typ)
            if response_text:
                self.speak(response_text)
                spoken = response_text
        except Exception as exc:
            log.error("Task execution error: %s", exc)
        return spoken

    # ------------------------------------------------------------------
    # Reminders / weather / manual text / music
    # ------------------------------------------------------------------
    def schedule_reminder(self, secs: int, msg: str) -> None:
        def trigger():
            self.send_event("reminder", {"message": msg})
            self.speak(f"Reminder: {msg}")
            # speak() leaves status at "thinking"; reset to idle so the UI
            # doesn't get stuck in the processing state after a reminder fires.
            self.send_event("status", {"status": "idle"})

        timer = threading.Timer(secs, trigger)
        timer.daemon = True
        timer.start()

    def handle_weather_smart(self, city: str) -> None:
        import urllib.parse
        import requests
        try:
            safe_city = urllib.parse.quote(city.strip())
            url = f"https://wttr.in/{safe_city}?format=%t|%C&lang=en"
            response = requests.get(url, headers={"User-Agent": "curl/7.79.1"}, timeout=5)
            r = response.text.strip()
            if "|" not in r:
                self.speak(f"Could not retrieve weather for {city}.")
                return
            temp_str, condition = r.split("|", 1)
            temp = int(temp_str.replace("+", "").replace("\u00b0C", "").strip())
            if "rain" in condition.lower():
                suggestion = "Take an umbrella."
            elif temp < 10:
                suggestion = "Wear a coat."
            elif temp < 20:
                suggestion = "Take a jacket."
            else:
                suggestion = "Wear a t-shirt."
            summary = f"{city} {temp} degrees, {condition.lower().strip()}. {suggestion}"
            self.speak(summary)
        except Exception as exc:
            log.warning("Weather error: %s", exc)
            self.speak("Could not reach the weather service.")

    def process_manual_text(self, text: str) -> None:
        self.send_event("status", {"status": "thinking"})
        self.send_event("text", {"text": f"\u2328\ufe0f {text}"})
        self._callback_depth = 0
        try:
            self.process_command(text)
        finally:
            self._reset_idle_if_done()

    def control_music(self, action: str, value=None):
        if action == "toggle":
            state = self.music.toggle_pause()
            self.send_event("music_state_changed", {"state": state})
            return state
        if action == "seek":
            self.music.seek(value)
        elif action == "set_volume":
            self.music.set_volume(value)
        elif action == "stop":
            self.music.stop()
            self.send_event("music_stop", {})
        elif action == "next":
            self.music.play_next()
        elif action == "previous":
            self.music.play_previous()
        return None

    def speak(self, text: str, lang: str | None = None) -> None:
        self.send_event("status", {"status": "speaking"})
        self.send_event("text", {"text": text})
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                speak_text_async(text, lambda lvl: self.send_event("audio_level", {"level": lvl}), lang="en")
            )
        finally:
            loop.close()
        self.send_event("status", {"status": "thinking"})
