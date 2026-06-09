"""Blink desktop entry point.

Run modes
---------
* **Desktop** (default): starts the FastAPI backend in a background thread and
  opens a frameless, draggable ``pywebview`` window pointed at the backend.
* **Headless** (``--headless`` or ``BLINK_HEADLESS=1``): starts only the
  FastAPI/Uvicorn server and bypasses ``pywebview`` entirely. This is the mode
  used inside Docker, where there is no X11/display server and microphone audio
  is streamed from the user's browser via ``/ws/audio``.

Resiliency
----------
* The HTTP port is selected dynamically: if the preferred port is occupied the
  next free port is chosen and the frontend URL is updated accordingly, so the
  desktop window never opens to a blank screen (issue 4C).
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

import uvicorn

from src.config import get_settings
from src.utils import find_available_port, get_logger

# Force UTF-8 stdout/stderr so Unicode output never crashes the terminal.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

log = get_logger(__name__)


def run_uvicorn(host: str, port: int) -> None:
    """Run the FastAPI app (blocking). Used directly in headless mode and in a
    background thread in desktop mode."""
    log.info("Starting Blink API server on http://%s:%d", host, port)
    uvicorn.run("src.server:app", host=host, port=port, log_level="info")


class WindowAPI:
    """Exposed to JavaScript via ``window.pywebview.api``."""

    def __init__(self) -> None:
        self._window = None
        self._is_minimized = False
        self._lock = threading.Lock()

    def set_window(self, win) -> None:
        self._window = win

    def minimize(self) -> None:
        with self._lock:
            if not self._is_minimized and self._window:
                self._is_minimized = True
                try:
                    self._window.minimize()
                except Exception as exc:
                    log.debug("Error minimizing window: %s", exc)

    def restore(self) -> None:
        # Do NOT call self._window.on_top here: dispatching to the GUI thread
        # from a JS-API callback thread deadlocks. The window is created with
        # on_top=True, so restore() alone brings it forward.
        with self._lock:
            if self._is_minimized and self._window:
                self._is_minimized = False
                try:
                    self._window.restore()
                except Exception as exc:
                    log.debug("restore error: %s", exc)

    def move_window(self, dx: int, dy: int) -> None:
        if self._window:
            try:
                self._window.move(self._window.x + dx, self._window.y + dy)
            except Exception as exc:
                log.debug("move_window error: %s", exc)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Blink voice assistant")
    parser.add_argument(
        "--headless",
        action="store_true",
        default=os.getenv("BLINK_HEADLESS", "").lower() in ("1", "true", "yes"),
        help="Run only the API server (no desktop window). Used in Docker.",
    )
    parser.add_argument("--host", default=None, help="Override bind host.")
    parser.add_argument("--port", type=int, default=None, help="Override preferred port.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    settings = get_settings()

    host = args.host or settings.host
    # In headless/Docker mode bind to all interfaces so the browser on the host
    # can reach the container.
    if args.headless and host in ("127.0.0.1", "localhost"):
        host = "0.0.0.0"

    preferred = args.port or settings.port
    bind_check_host = "127.0.0.1" if host == "0.0.0.0" else host
    port = find_available_port(bind_check_host, preferred)
    if port != preferred:
        log.warning("Preferred port %d busy; using %d instead.", preferred, port)
    os.environ["BLINK_PORT"] = str(port)

    if args.headless:
        log.info("Running in HEADLESS mode.")
        run_uvicorn(host, port)
        return

    # Desktop mode -------------------------------------------------------
    import webview

    threading.Thread(target=run_uvicorn, args=(host, port), daemon=True).start()

    # Wait briefly for the server to bind before opening the window.
    time.sleep(1.5)

    frontend_dist = os.path.join(os.getcwd(), "frontend", "dist")
    if os.path.isdir(frontend_dist):
        url = f"http://127.0.0.1:{port}"  # built frontend served by FastAPI
    else:
        # Dev mode: Vite dev server. It reads BLINK_PORT to find the backend.
        url = "http://localhost:5173"

    log.info("Launching Blink desktop window -> %s", url)
    api = WindowAPI()
    window = webview.create_window(
        title="Blink Jarvis",
        url=url,
        width=600,
        height=720,
        resizable=False,
        frameless=True,
        text_select=False,
        background_color="#030712",
        on_top=True,
        js_api=api,
    )
    api.set_window(window)
    webview.start()


if __name__ == "__main__":
    main()
