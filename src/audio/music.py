"""Direct-stream music playback via yt-dlp + FFmpeg + sounddevice.

Hardening vs. legacy: structured logging, ``np.clip`` on volume scaling to
avoid int16 wrap-around distortion, thread-safe state, and resilient stream
writes.
"""

from __future__ import annotations

import subprocess
import threading
from typing import Callable, Optional

import numpy as np

from src.utils import get_logger

log = get_logger(__name__)

SAMPLE_RATE = 44100
CHANNELS = 2
CHUNK_FRAMES = 1024


class MusicEngine:
    """Streams audio on the fly without writing media files to disk."""

    def __init__(self, event_callback: Optional[Callable] = None) -> None:
        self.event_callback = event_callback
        self._current_url: Optional[str] = None
        self._current_title: Optional[str] = None
        self._current_duration = 0
        self._current_thumbnail = ""

        self._stream = None
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

        self.is_paused = False
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()

        self.volume = 1.0
        self.current_seconds = 0.0
        self._lock = threading.Lock()
        
        self._playlist: list[dict] = []
        self._playlist_index = 0

    # ------------------------------------------------------------------
    def is_busy(self) -> bool:
        return self._stream is not None and not self.is_paused

    def _emit(self, event: str, payload: dict) -> None:
        if self.event_callback:
            try:
                self.event_callback(event, payload)
            except Exception as exc:
                log.debug("Music event callback error: %s", exc)

    def play_query(self, query: str) -> None:
        self.stop()
        self._stop_event.clear()
        self._pause_event.set()
        self.is_paused = False
        self.current_seconds = 0.0
        self._thread = threading.Thread(target=self._run_search_and_play, args=(query,), daemon=True)
        self._thread.start()

    def _run_search_and_play(self, query: str) -> None:
        import yt_dlp

        self._emit("text", {"text": f"\U0001F50D Searching YouTube for: {query}..."})
        is_url = query.startswith("http://") or query.startswith("https://")
        ydl_opts = {
            "format": "bestaudio/best",
            "noplaylist": False if is_url else True,
            "quiet": True,
            "default_search": "ytsearch10" if not is_url else "ytsearch1",
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(query, download=False)
                if "entries" in result:
                    self._playlist = [e for e in result["entries"] if e]
                else:
                    self._playlist = [result]
                self._playlist = [entry for entry in self._playlist if entry and ("url" in entry or "id" in entry)]
                self._playlist_index = 0
        except Exception as exc:
            log.warning("yt-dlp extract error: %s", exc)
            self._emit("text", {"text": f"\u274C Error finding music: {exc}"})
            return

        if self._stop_event.is_set():
            return

        if not self._playlist:
            self._emit("text", {"text": "\u274C No tracks found."})
            return

        self._run_stream_index()

    def _run_stream_index(self) -> None:
        import sounddevice as sd
        import yt_dlp

        if not self._playlist or self._playlist_index < 0 or self._playlist_index >= len(self._playlist):
            self._emit("text", {"text": "\u26A0\ufe0f No track to play."})
            return

        info = self._playlist[self._playlist_index]
        self._current_url = info.get("url")
        
        # If it doesn't have a direct stream URL (e.g. only webpage_url or original url), extract it
        if not self._current_url or "youtube.com" in self._current_url or "youtu.be" in self._current_url:
            ydl_opts = {
                "format": "bestaudio/best",
                "noplaylist": True,
                "quiet": True,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    url_to_extract = info.get("webpage_url") or info.get("original_url") or f"https://www.youtube.com/watch?v={info['id']}"
                    res = ydl.extract_info(url_to_extract, download=False)
                    self._current_url = res["url"]
                    info = res
            except Exception as exc:
                log.warning("yt-dlp re-extract error: %s", exc)
                self._emit("text", {"text": f"\u274C Error extracting stream URL: {exc}"})
                return

        self._current_title = info.get("title", "Unknown Track")
        self._current_duration = int(info.get("duration", 0) or 0)
        self._current_thumbnail = info.get("thumbnail", "")

        self._emit("text", {"text": f"\U0001F3B5 Playing ({self._playlist_index + 1}/{len(self._playlist)}): {self._current_title}"})
        self._emit("music_start", {
            "title": self._current_title,
            "thumbnail": self._current_thumbnail,
            "duration": self._current_duration,
        })

        try:
            with self._lock:
                self._stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16")
                self._stream.start()
        except Exception as exc:
            log.error("sounddevice OutputStream init error: %s", exc)
            self._emit("text", {"text": f"\u274C Audio Output Error: {exc}"})
            return

        self._start_ffmpeg(0)
        bytes_per_frame = CHANNELS * 2
        chunk_bytes = CHUNK_FRAMES * bytes_per_frame
        samples_played = 0

        while not self._stop_event.is_set():
            self._pause_event.wait()
            if self._stop_event.is_set():
                break
            try:
                if not self._process or not self._process.stdout:
                    break
                data = self._process.stdout.read(chunk_bytes)
                if not data:
                    break
                audio_np = np.frombuffer(data, dtype=np.int16).reshape(-1, CHANNELS)
                if self.volume != 1.0:
                    audio_np = np.clip(audio_np.astype(np.float32) * self.volume, -32768, 32767).astype(np.int16)
                with self._lock:
                    if self._stream:
                        self._stream.write(audio_np)
                samples_played += len(audio_np)
                self.current_seconds = samples_played / SAMPLE_RATE
                if self.event_callback and samples_played % (CHUNK_FRAMES * 6) == 0:
                    floats = audio_np.astype(np.float32) / 32768.0
                    rms = np.sqrt(np.mean(floats ** 2))
                    self._emit("audio_level", {"level": min(float(rms) * 5, 1.0)})
            except Exception as exc:
                log.warning("Streaming playback loop exception: %s", exc)
                break

        if not self._stop_event.is_set():
            self.stop()
            self._emit("music_stop", {})

    def play_next(self) -> None:
        if not hasattr(self, "_playlist") or not self._playlist:
            self._emit("text", {"text": "\u26A0\ufe0f No playlist loaded."})
            return
        if self._playlist_index + 1 >= len(self._playlist):
            self._emit("text", {"text": "\u26A0\ufe0f Reached the end of the playlist."})
            return
        
        self.stop()
        self._stop_event.clear()
        self._pause_event.set()
        self.is_paused = False
        self.current_seconds = 0.0
        self._playlist_index += 1
        
        self._thread = threading.Thread(target=self._run_stream_index, daemon=True)
        self._thread.start()

    def play_previous(self) -> None:
        if not hasattr(self, "_playlist") or not self._playlist:
            self._emit("text", {"text": "\u26A0\ufe0f No playlist loaded."})
            return
        if self._playlist_index - 1 < 0:
            self._emit("text", {"text": "\u26A0\ufe0f Reached the start of the playlist."})
            return
            
        self.stop()
        self._stop_event.clear()
        self._pause_event.set()
        self.is_paused = False
        self.current_seconds = 0.0
        self._playlist_index -= 1
        
        self._thread = threading.Thread(target=self._run_stream_index, daemon=True)
        self._thread.start()

    def _start_ffmpeg(self, start_seconds: float) -> None:
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=1.0)
            except Exception:
                pass
        command = [
            "ffmpeg",
            "-ss", str(int(start_seconds)),
            "-i", self._current_url or "",
            "-f", "s16le",
            "-ac", "2",
            "-ar", str(SAMPLE_RATE),
            "-loglevel", "quiet",
            "-",
        ]
        try:
            self._process = subprocess.Popen(command, stdout=subprocess.PIPE, bufsize=10 ** 6)
        except OSError as exc:
            log.error("FFmpeg launch failed: %s", exc)
            self._process = None

    def seek(self, seconds: float) -> None:
        if not self._current_url:
            return
        log.debug("Seeking to %ss", seconds)
        was_paused = not self._pause_event.is_set()
        self._pause_event.clear()
        self._start_ffmpeg(seconds)
        self.current_seconds = float(seconds)
        if not was_paused:
            self._pause_event.set()

    def toggle_pause(self) -> str:
        if self.is_paused:
            self._pause_event.set()
            self.is_paused = False
            return "Resumed"
        self._pause_event.clear()
        self.is_paused = True
        return "Paused"

    def set_volume(self, value: float) -> None:
        """Clamp and set playback volume multiplier (0.0 - 1.0)."""
        try:
            self.volume = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            pass

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.set()
        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass
            self._process = None
        with self._lock:
            if self._stream:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
        if self._thread and self._thread.is_alive() and threading.current_thread() != self._thread:
            try:
                self._thread.join(timeout=1.0)
            except Exception:
                pass
            self._thread = None
        self.is_paused = False
