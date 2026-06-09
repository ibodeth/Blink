"""Integration flow tests for local commands, history, and WebSocket endpoints."""

import pytest
import asyncio
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import src.config as config
from src.audio.wake_word import BackendWorker, OFFLINE_MESSAGE
from src.ai import OfflineError
from src.server import app


@pytest.fixture
def mock_worker(monkeypatch):
    """Fixture that initializes a BackendWorker with mock dependencies."""
    # Prevent restore conversation failure from logging warning during tests
    monkeypatch.setattr("src.database.ConversationStore.load_recent", lambda self, n: [])
    
    worker = BackendWorker()
    worker.speak = MagicMock()
    worker.send_event = MagicMock()
    worker.sys = MagicMock()
    worker.music = MagicMock()
    worker.ai = MagicMock()
    
    # Mock cached_telemetry structure
    worker.sys.cached_telemetry = {
        "wifi": {"adapter_enabled": True, "connected_ssid": "Tenda"},
        "bluetooth": {"adapter_enabled": False},
        "stats": {"battery": {"percent": 85, "power_plugged": False}},
        "audio": {"volume": 42, "muted": False}
    }
    
    return worker


# --- 1. Local Command Dispatch Tests ---

def test_local_dispatch_wifi_on(mock_worker):
    assert mock_worker._try_local_dispatch("turn on wifi") is True
    mock_worker.sys.set_wifi_state.assert_called_once_with(True)
    mock_worker.speak.assert_called_once()
    assert "turned on" in mock_worker.speak.call_args[0][0]


def test_local_dispatch_wifi_off(mock_worker):
    assert mock_worker._try_local_dispatch("wifi off") is True
    mock_worker.sys.set_wifi_state.assert_called_once_with(False)
    mock_worker.speak.assert_called_once()
    assert "turned off" in mock_worker.speak.call_args[0][0]


def test_local_dispatch_bluetooth_on(mock_worker):
    assert mock_worker._try_local_dispatch("enable bluetooth") is True
    mock_worker.sys.set_bluetooth_state.assert_called_once_with(True)
    mock_worker.speak.assert_called_once()
    assert "turned on" in mock_worker.speak.call_args[0][0]


def test_local_dispatch_battery_status(mock_worker):
    assert mock_worker._try_local_dispatch("how much battery") is True
    mock_worker.speak.assert_called_once_with("Battery level is 85%.")


def test_local_dispatch_volume_percent(mock_worker):
    assert mock_worker._try_local_dispatch("volume level") is True
    mock_worker.speak.assert_called_once_with("Volume is at 42%.")


def test_local_dispatch_music_commands(mock_worker):
    # Mock control_music to toggle and return state
    mock_worker.control_music = MagicMock(return_value="paused")
    assert mock_worker._try_local_dispatch("pause") is True
    mock_worker.control_music.assert_called_once_with("toggle")
    mock_worker.speak.assert_called_once_with("Music paused.")


# --- 2. History Query Interception Tests ---

def test_history_dispatch_empty(mock_worker):
    mock_worker.ctx.history = []
    assert mock_worker._try_history_dispatch("show conversation history") is True
    mock_worker.speak.assert_called_once_with("I have no conversation history yet.")


def test_history_dispatch_with_turns(mock_worker):
    mock_worker.ctx.add("user", "hello assistant")
    mock_worker.ctx.add("blink", "hello user")
    
    assert mock_worker._try_history_dispatch("son 2 konuşmayı söyle") is True
    mock_worker.speak.assert_called_once_with("Here are the last 2 messages from our conversation.")
    mock_worker.send_event.assert_any_call("blink_message", {
        "text": "Here are the last 2 messages:\nuser: hello assistant\nblink: hello user"
    })


# --- 3. Offline degradation / fallback Tests ---

def test_offline_fallback_matched(mock_worker):
    mock_worker.ai.generate_command.side_effect = OfflineError("No connection")
    mock_worker._try_local_dispatch = MagicMock(side_effect=[False, True])
    
    mock_worker.process_command("wifi off")
    assert mock_worker._try_local_dispatch.call_count == 2
    mock_worker.speak.assert_any_call(OFFLINE_MESSAGE)


def test_offline_fallback_unmatched(mock_worker):
    mock_worker.ai.generate_command.side_effect = OfflineError("No connection")
    mock_worker._try_local_dispatch = MagicMock(side_effect=[False, False])
    
    mock_worker.process_command("tell me a story")
    assert mock_worker._try_local_dispatch.call_count == 2
    mock_worker.speak.assert_called_once_with(OFFLINE_MESSAGE)


# --- 4. WebSocket control plane Tests ---

def test_websocket_connection_unauthorized(monkeypatch):
    config.get_settings.cache_clear()
    monkeypatch.setenv("BLINK_WS_TOKEN", "supersecrettoken")
    config.get_settings.cache_clear()
    
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True
    
    import src.server as server
    monkeypatch.setattr(server, "worker_thread", mock_thread)
    
    client = TestClient(app)
    with client.websocket_connect("/ws") as websocket:
        # Config status event shows auth is required
        data = websocket.receive_json()
        assert data["type"] == "config_status"
        assert data["payload"]["auth_required"] is True
        
        # Consume assistant status event if sent
        data = websocket.receive_json()
        if data["type"] == "conversation_history":
            data = websocket.receive_json()
        assert data["type"] == "assistant_status"
        
        # Send privileged system event without token
        websocket.send_json({
            "type": "system_action",
            "action": "set_volume",
            "value": 50
        })
        err = websocket.receive_json()
        assert err["type"] == "error"
        assert "Unauthorized" in err["payload"]["message"]


def test_websocket_connection_authorized(monkeypatch):
    config.get_settings.cache_clear()
    monkeypatch.setenv("BLINK_WS_TOKEN", "supersecrettoken")
    config.get_settings.cache_clear()
    
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True
    
    import src.server as server
    monkeypatch.setattr(server, "worker_thread", mock_thread)
    
    client = TestClient(app)
    with client.websocket_connect("/ws") as websocket:
        # Consume startup events (config_status, assistant_status, potentially conversation_history)
        data = websocket.receive_json()
        assert data["type"] == "config_status"
        data = websocket.receive_json()
        if data["type"] == "conversation_history":
            data = websocket.receive_json()
        assert data["type"] == "assistant_status"
        
        # Send privileged system event with correct token
        websocket.send_json({
            "type": "system_action",
            "action": "set_volume",
            "value": 75,
            "token": "supersecrettoken"
        })
        
        # Receive system_telemetry response to ensure backend processing has finished
        resp = websocket.receive_json()
        assert resp["type"] == "system_telemetry"
        
        # Check that sys.set_volume is called on backend worker
        mock_thread.sys.set_volume.assert_called_once_with(75)


def test_websocket_audio_ingest_token_validation(monkeypatch):
    config.get_settings.cache_clear()
    monkeypatch.setenv("BLINK_WS_TOKEN", "supersecrettoken")
    config.get_settings.cache_clear()
    
    import src.server as server
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True
    monkeypatch.setattr(server, "worker_thread", mock_thread)
    
    client = TestClient(app)
    
    # Authorized connection
    with client.websocket_connect("/ws/audio?token=supersecrettoken") as ws:
        # Mock the worker feed_browser_audio method
        mock_feed = MagicMock()
        monkeypatch.setattr(mock_thread, "feed_browser_audio", mock_feed)
        
        # Send some sample binary audio bytes
        ws.send_bytes(b"\x00\x01" * 640)
        
        # Since it runs on server event loop, wait brief moment to ensure task completion
        import time
        time.sleep(0.2)
        mock_feed.assert_called_once()


# --- 5. Music Playlist Navigation Tests ---

def test_music_engine_playlist_navigation():
    from src.audio.music import MusicEngine
    music = MusicEngine()
    
    # Verify default state
    assert music._playlist == []
    assert music._playlist_index == 0
    
    # Mock search response with 3 dummy items
    dummy_playlist = [
        {"title": "Track 1", "url": "http://track1"},
        {"title": "Track 2", "url": "http://track2"},
        {"title": "Track 3", "url": "http://track3"}
    ]
    music._playlist = dummy_playlist
    music._playlist_index = 0
    
    # Mock stop and thread starting on play_next/play_previous
    music.stop = MagicMock()
    
    with patch("threading.Thread") as mock_thread:
        # Play next track
        music.play_next()
        assert music._playlist_index == 1
        music.stop.assert_called_once()
        mock_thread.assert_called_once()
        
        # Reset mock
        music.stop.reset_mock()
        mock_thread.reset_mock()
        
        # Play next again
        music.play_next()
        assert music._playlist_index == 2
        music.stop.assert_called_once()
        mock_thread.assert_called_once()
        
        # Reset mock
        music.stop.reset_mock()
        mock_thread.reset_mock()
        
        # Play next at boundary
        music.play_next()
        assert music._playlist_index == 2  # stays at boundary
        music.stop.assert_not_called()
        mock_thread.assert_not_called()
        
        # Play previous
        music.play_previous()
        assert music._playlist_index == 1
        music.stop.assert_called_once()
        mock_thread.assert_called_once()
        
        # Reset mock
        music.stop.reset_mock()
        mock_thread.reset_mock()
        
        # Play previous again
        music.play_previous()
        assert music._playlist_index == 0
        music.stop.assert_called_once()
        mock_thread.assert_called_once()
        
        # Reset mock
        music.stop.reset_mock()
        mock_thread.reset_mock()
        
        # Play previous at boundary
        music.play_previous()
        assert music._playlist_index == 0  # stays at boundary
        music.stop.assert_not_called()
        mock_thread.assert_not_called()


def test_backend_worker_music_navigation_commands(mock_worker):
    # Mock control_music method
    mock_worker.control_music = MagicMock()
    
    # Test next action
    mock_worker._execute_single_command({"type": "music", "action": "next"}, "User")
    mock_worker.control_music.assert_called_once_with("next")
    mock_worker.speak.assert_called_once_with("Playing next song.")
    
    # Reset mocks
    mock_worker.control_music.reset_mock()
    mock_worker.speak.reset_mock()
    
    # Test previous action
    mock_worker._execute_single_command({"type": "music", "action": "previous"}, "User")
    mock_worker.control_music.assert_called_once_with("previous")
    mock_worker.speak.assert_called_once_with("Playing previous song.")
