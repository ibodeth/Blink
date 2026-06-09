"""FastAPI application: WebSocket control plane + health + static frontend.

Upgrades vs. legacy:

* **Lifespan handler** replaces deprecated ``@app.on_event``.
* **Restricted CORS** driven by ``BLINK_ALLOWED_ORIGINS`` (no wildcard +
  credentials combination).
* **Token-gated privileged events** (``system_action``, ``save_config``):
  a handshake token is required for actions that mutate hardware/config.
* **``/health`` endpoint** for container/orchestrator healthchecks.
* **Browser audio ingest** (``/ws/audio``) so a Docker/headless deployment can
  receive microphone PCM from the user's browser (feature 4A).
* **Pydantic validation** of every inbound event via :func:`parse_incoming`.
* Structured logging; thread-safe worker startup.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.config import get_settings, load_config, save_config
from src.schemas import PRIVILEGED_EVENT_TYPES, make_event, parse_incoming
from src.utils import get_logger

log = get_logger(__name__)

# Cross-thread state.
server_loop: Optional[asyncio.AbstractEventLoop] = None
worker_thread = None
_worker_lock = threading.Lock()


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
        log.info("Client connected: %s", websocket.client)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
        log.info("Client disconnected")

    async def broadcast(self, message: dict) -> None:
        stale: List[WebSocket] = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception as exc:
                log.debug("Broadcast failed to client: %s", exc)
                stale.append(connection)
        if stale:
            async with self._lock:
                for connection in stale:
                    if connection in self.active_connections:
                        self.active_connections.remove(connection)


manager = ConnectionManager()


def broadcast_sync(message: dict) -> None:
    """Bridge for worker threads to broadcast through the server event loop."""
    if server_loop and server_loop.is_running():
        asyncio.run_coroutine_threadsafe(manager.broadcast(message), server_loop)


def start_assistant() -> None:
    """Start the BackendWorker thread if config is present and not running."""
    global worker_thread
    from src.audio.wake_word import BackendWorker

    if not load_config():
        log.info("Assistant not started: API key not configured.")
        return
    with _worker_lock:
        if worker_thread is None or not worker_thread.is_alive():
            log.info("Starting BackendWorker thread...")
            worker_thread = BackendWorker(broadcast_fn=broadcast_sync)
            worker_thread.start()
            broadcast_sync({"type": "assistant_started"})
        else:
            log.debug("BackendWorker already running.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global server_loop
    server_loop = asyncio.get_running_loop()
    log.info("Server lifespan start.")
    start_assistant()
    try:
        yield
    finally:
        with _worker_lock:
            if worker_thread:
                worker_thread.running = False
        log.info("Server lifespan shutdown.")


app = FastAPI(title="Blink Assistant", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> JSONResponse:
    running = bool(worker_thread and worker_thread.is_alive())
    return JSONResponse({
        "status": "ok",
        "assistant_running": running,
        "configured": get_settings().has_api_key,
    })


def _is_authorized(token: Optional[str]) -> bool:
    """Constant-ish comparison against the configured WS token."""
    expected = get_settings().ws_auth_token
    if not expected:
        return True  # No token configured -> open (local single-user default).
    return bool(token) and token == expected


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    settings = get_settings()
    await websocket.send_json(make_event("config_status", {
        "has_keys": load_config(),
        "model": settings.model,
        "auth_required": bool(settings.ws_auth_token),
    }))
    if worker_thread and worker_thread.is_alive():
        await websocket.send_json(make_event("assistant_status", {"running": True}))
    
    try:
        from src.database import ConversationStore
        convo = ConversationStore()
        turns = convo.load_recent(50)
        if turns:
            await websocket.send_json(make_event("conversation_history", {"history": turns}))
    except Exception as exc:
        log.warning("Failed to send conversation history: %s", exc)

    _ = settings.ws_auth_token  # token presence already surfaced above

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                log.debug("Rejected non-JSON WS frame: %s", exc)
                await websocket.send_json(make_event("error", {"message": "Invalid event payload."}))
                continue
            try:
                event = parse_incoming(data)
            except Exception as exc:
                log.debug("Rejected malformed WS event: %s", exc)
                await websocket.send_json(make_event("error", {"message": "Invalid event payload."}))
                continue

            event_type = event.type

            # Authorization gate for privileged events.
            if event_type in PRIVILEGED_EVENT_TYPES and not _is_authorized(getattr(event, "token", None)):
                log.warning("Unauthorized privileged event '%s' rejected.", event_type)
                await websocket.send_json(make_event("error", {"message": "Unauthorized."}))
                continue

            await _dispatch_event(websocket, event)
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as exc:
        log.error("WebSocket error: %s", exc)
        await manager.disconnect(websocket)


async def _dispatch_event(websocket: WebSocket, event) -> None:
    event_type = event.type
    worker_alive = bool(worker_thread and worker_thread.is_alive())

    if event_type == "manual_text":
        if worker_alive:
            threading.Thread(target=worker_thread.process_manual_text, args=(event.text,), daemon=True).start()
        else:
            await websocket.send_json(make_event("error", {"message": "Assistant is not running. Please configure keys."}))

    elif event_type == "music_control":
        if worker_alive:
            worker_thread.control_music(event.action, event.value)

    elif event_type == "system_action":
        if worker_alive:
            await _handle_system_action(websocket, event)

    elif event_type == "save_config":
        save_config(event.openrouter_key, event.model)
        await websocket.send_json(make_event("config_status", {"has_keys": True, "model": event.model}))
        start_assistant()

    elif event_type == "get_config":
        settings = get_settings()
        await websocket.send_json(make_event("config_status", {
            "has_keys": load_config(),
            "model": settings.model,
        }))


async def _handle_system_action(websocket: WebSocket, event) -> None:
    sysmgr = worker_thread.sys
    action, value = event.action, event.value
    if action == "kill_process" and event.pid is not None:
        sysmgr.kill_process(event.pid)
    elif action == "toggle_wifi" and value is not None:
        sysmgr.set_wifi_state(bool(value))
        sysmgr.cached_telemetry["wifi"]["adapter_enabled"] = bool(value)
    elif action == "toggle_bluetooth" and value is not None:
        sysmgr.set_bluetooth_state(bool(value))
        sysmgr.cached_telemetry["bluetooth"]["adapter_enabled"] = bool(value)
    elif action == "connect_wifi" and event.ssid:
        sysmgr.connect_wifi(event.ssid, event.password or "")
        sysmgr.cached_telemetry["wifi"]["connected_ssid"] = event.ssid
    elif action == "set_volume" and value is not None:
        sysmgr.set_volume(int(value))
        sysmgr.cached_telemetry["audio"]["volume"] = int(value)
    elif action == "set_mute" and value is not None:
        sysmgr.set_mute(bool(value))
        sysmgr.cached_telemetry["audio"]["muted"] = bool(value)
    elif action == "set_brightness" and value is not None:
        sysmgr.execute("set_brightness", int(value))
        sysmgr.cached_telemetry["display"]["brightness"] = int(value)
    try:
        await websocket.send_json(make_event("system_telemetry", {"telemetry": sysmgr.cached_telemetry}))
    except Exception as exc:
        log.debug("Telemetry instant reply error: %s", exc)


@app.websocket("/ws/audio")
async def audio_ingest_endpoint(websocket: WebSocket) -> None:
    """Receive raw 16 kHz mono int16 PCM frames from a browser (feature 4A).

    This lets a headless/Docker deployment capture microphone audio from the
    user's browser on the host. Each binary message is a block of PCM samples
    that is forwarded to the running worker's browser-audio sink (if present).
    """
    await websocket.accept()
    token = websocket.query_params.get("token")
    if not _is_authorized(token):
        await websocket.send_json(make_event("error", {"message": "Unauthorized."}))
        await websocket.close(code=1008)
        return

    log.info("Browser audio ingest connected.")
    try:
        while True:
            message = await websocket.receive()
            data = message.get("bytes")
            if data is None:
                continue
            if worker_thread and worker_thread.is_alive():
                sink = getattr(worker_thread, "feed_browser_audio", None)
                if callable(sink):
                    sink(data)
    except WebSocketDisconnect:
        log.info("Browser audio ingest disconnected.")
    except Exception as exc:
        log.debug("Audio ingest error: %s", exc)


# ---------------------------------------------------------------------------
# Serve the built frontend (production) if present. Mounted last so it does not
# shadow the API/WebSocket routes above.
# ---------------------------------------------------------------------------
_frontend_dist = os.path.join(os.getcwd(), "frontend", "dist")
if os.path.isdir(_frontend_dist):
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
    log.info("Serving built frontend from %s", _frontend_dist)
