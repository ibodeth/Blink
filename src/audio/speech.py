"""Speech-to-text (faster-whisper) and text-to-speech (Kokoro ONNX).

Hardening & features added:

* **English-only** synthesis (the Turkish ``lang`` branch was removed).
* **Overflow-safe microphone reads** via :func:`safe_read`, which swallows
  ``sounddevice`` input-overflow exceptions and returns silence instead of
  crashing the wake-word / active-listen loops.
* **Graceful TTS degradation**: if the Kokoro ONNX model or ``voices.bin`` is
  missing/corrupt, playback transparently falls back to the OS-native voice
  synthesizer via :mod:`pyttsx3` (SAPI5 on Windows, NSSpeech on macOS, espeak
  on Linux). A clear warning is logged.
* Structured logging throughout.
"""

from __future__ import annotations

import io
import math
import os
import time
import wave

import numpy as np

import threading

from src.utils import get_logger

log = get_logger(__name__)

_speech_lock = threading.RLock()
_active_speech_id = 0

SAMPLE_RATE = 16000
BLOCKSIZE = 1280

# Lazily-initialised singletons.
_whisper_model = None
_kokoro = None
_kokoro_failed = False  # Once True, we stop retrying and use the fallback.
_pyttsx_engine = None


# ---------------------------------------------------------------------------
# Overflow-safe audio read
# ---------------------------------------------------------------------------
def safe_read(stream, frames: int = BLOCKSIZE):
    """Read ``frames`` from a sounddevice stream, tolerating overflows.

    Returns ``(data, overflowed)``. On any read error a block of silence is
    returned so callers never crash on a busy CPU.
    """
    try:
        data, overflowed = stream.read(frames)
        return data, bool(overflowed)
    except Exception as exc:  # sounddevice.PortAudioError and friends
        log.debug("Audio read recovered from error: %s", exc)
        return np.zeros((frames, 1), dtype=np.int16), True


# ---------------------------------------------------------------------------
# Whisper (STT)
# ---------------------------------------------------------------------------
def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        log.info("Initializing local Whisper model ('base')...")
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        log.info("Local Whisper model loaded.")
    return _whisper_model


# ---------------------------------------------------------------------------
# Kokoro (TTS) + asset download
# ---------------------------------------------------------------------------
def download_kokoro_assets(progress_callback=None) -> None:
    import requests

    assets = {
        "kokoro-v0_19.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx",
        "voices.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin",
    }
    for filename, url in assets.items():
        path = os.path.join(os.getcwd(), filename)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            continue
        log.info("Downloading TTS asset '%s'...", filename)
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()

        total_size = int(resp.headers.get("content-length", 0))
        downloaded = 0
        last_pct = -1

        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total_size > 0:
                        pct = int((downloaded / total_size) * 100)
                        if pct != last_pct:
                            progress_callback(filename, pct)
                            last_pct = pct
        os.replace(tmp, path)
        log.info("Downloaded '%s'.", filename)


def get_kokoro(progress_callback=None):
    """Return the Kokoro engine, or ``None`` if it cannot be initialised."""
    global _kokoro, _kokoro_failed
    if _kokoro is not None or _kokoro_failed:
        return _kokoro
    try:
        download_kokoro_assets(progress_callback)
        # Kokoro stores voices as a pickled archive; allow_pickle must be on.
        _orig_load = np.load

        def _patched_load(*args, **kwargs):
            kwargs["allow_pickle"] = True
            return _orig_load(*args, **kwargs)

        np.load = _patched_load
        try:
            from kokoro_onnx import Kokoro
            model_path = os.path.join(os.getcwd(), "kokoro-v0_19.onnx")
            voices_path = os.path.join(os.getcwd(), "voices.bin")
            log.info("Initializing Kokoro ONNX model...")
            _kokoro = Kokoro(model_path, voices_path)
            log.info("Kokoro ONNX initialized.")
        finally:
            np.load = _orig_load
    except Exception as exc:
        _kokoro_failed = True
        log.warning("Kokoro TTS unavailable, using native fallback voice: %s", exc)
        _kokoro = None
    return _kokoro


def _get_pyttsx_engine():
    global _pyttsx_engine
    if _pyttsx_engine is None:
        import pyttsx3
        _pyttsx_engine = pyttsx3.init()
        _pyttsx_engine.setProperty("rate", 180)
    return _pyttsx_engine


def _speak_fallback(text: str, audio_level_callback=None) -> None:
    """Speak using the OS-native synthesizer (graceful degradation)."""
    with _speech_lock:
        try:
            engine = _get_pyttsx_engine()
        except Exception as exc:
            log.error("No TTS engine available (Kokoro and pyttsx3 both failed): %s", exc)
            return
        try:
            if audio_level_callback:
                audio_level_callback(0.4)
            engine.say(text)
            engine.runAndWait()
        except Exception as exc:
            log.error("Fallback TTS error: %s", exc)
        finally:
            if audio_level_callback:
                audio_level_callback(0.0)


# ---------------------------------------------------------------------------
# Microphone -> text
# ---------------------------------------------------------------------------
def recognize_speech_from_mic(stream=None, timeout: int = 6, level_callback=None) -> str:
    """Listen and transcribe a single utterance with faster-whisper.

    Overflow-safe: all stream reads go through :func:`safe_read`.
    """
    import sounddevice as sd

    silence_seconds = 1.5

    def _emit_level(data_flat: np.ndarray) -> None:
        if not level_callback:
            return
        rms = np.sqrt(np.mean((data_flat.astype(np.float32) / 32768.0) ** 2))
        try:
            level_callback(min(float(rms) * 5, 1.0))
        except Exception:
            pass

    try:
        if stream is None:
            log.debug("Adjusting for ambient noise...")
            noise = sd.rec(int(SAMPLE_RATE * 0.3), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
            sd.wait()
            noise_rms = np.sqrt(np.mean((noise.astype(np.float32) / 32768.0) ** 2))
        else:
            samples = []
            for _ in range(3):
                data, _ov = safe_read(stream, BLOCKSIZE)
                samples.append(np.sqrt(np.mean((data.flatten().astype(np.float32) / 32768.0) ** 2)))
            noise_rms = float(np.mean(samples)) if samples else 0.01
        threshold = max(noise_rms * 1.5, 0.015)
        log.debug("Listening (threshold=%.4f)", threshold)

        audio_frames = []
        speaking = False
        silent_blocks = 0
        max_silent_blocks = int(silence_seconds * SAMPLE_RATE / BLOCKSIZE)
        max_total_blocks = int(15 * SAMPLE_RATE / BLOCKSIZE)

        def run_listen(active_stream) -> None:
            nonlocal speaking, silent_blocks
            start_time = time.time()
            for _ in range(int(timeout * SAMPLE_RATE / BLOCKSIZE)):
                data, _ov = safe_read(active_stream, BLOCKSIZE)
                data_flat = data.flatten()
                _emit_level(data_flat)
                rms = np.sqrt(np.mean((data_flat.astype(np.float32) / 32768.0) ** 2))
                if rms > threshold:
                    speaking = True
                    audio_frames.append(data_flat)
                    break
                if time.time() - start_time > timeout:
                    break
            if not speaking:
                return
            log.debug("Speech detected, recording...")
            for _ in range(max_total_blocks):
                data, _ov = safe_read(active_stream, BLOCKSIZE)
                data_flat = data.flatten()
                audio_frames.append(data_flat)
                _emit_level(data_flat)
                rms = np.sqrt(np.mean((data_flat.astype(np.float32) / 32768.0) ** 2))
                silent_blocks = silent_blocks + 1 if rms < threshold else 0
                if silent_blocks >= max_silent_blocks:
                    log.debug("Silence detected, stopping recording.")
                    break

        if stream is None:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=BLOCKSIZE) as new_stream:
                run_listen(new_stream)
        else:
            run_listen(stream)

        if not speaking or not audio_frames:
            log.debug("No speech detected.")
            return ""

        recorded = np.concatenate(audio_frames)
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(recorded.tobytes())
        wav_io.seek(0)

        model = get_whisper_model()
        segments, _info = model.transcribe(wav_io, beam_size=5, language="en")
        command = "".join(seg.text for seg in segments).strip()
        log.info("Transcription: %s", command)
        return command
    except Exception as exc:
        log.error("Speech recognition error: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Text -> speech
# ---------------------------------------------------------------------------
async def speak_text_async(text: str, audio_level_callback=None, lang: str = "en") -> None:
    """Speak ``text`` in English via Kokoro, falling back to the OS voice."""
    if not text:
        return

    global _active_speech_id
    try:
        import pygame
        if pygame.mixer.get_init():
            pygame.mixer.stop()
    except Exception:
        pass

    with _speech_lock:
        _active_speech_id += 1
        my_id = _active_speech_id

        kokoro = get_kokoro()
        if kokoro is None:
            _speak_fallback(text, audio_level_callback)
            return

        try:
            import pygame

            samples, sample_rate = kokoro.create(text, voice="af_bella", speed=1.0, lang="en-us")
            int_samples = np.clip(samples * 32767, -32768, 32767).astype(np.int16)

            wav_io = io.BytesIO()
            with wave.open(wav_io, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(int_samples.tobytes())
            wav_io.seek(0)

            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=sample_rate, size=-16, channels=1)

            sound = pygame.mixer.Sound(wav_io)
            sound.play()

            start = time.time()
            while pygame.mixer.get_busy():
                if _active_speech_id != my_id:
                    sound.stop()
                    break
                if audio_level_callback:
                    audio_level_callback(0.3 + 0.5 * abs(math.sin((time.time() - start) * 12)))
                time.sleep(0.05)
            if audio_level_callback:
                audio_level_callback(0.0)
        except Exception as exc:
            log.warning("Kokoro playback failed, using fallback voice: %s", exc)
            _speak_fallback(text, audio_level_callback)
