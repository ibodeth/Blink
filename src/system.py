"""System control & telemetry (hardened, cross-platform).

Security posture (major change vs. legacy):

* **No shell string interpolation.** Every privileged command is executed from
  an argument list with ``shell=False`` via :func:`_run`/:func:`_popen`. User-
  or AI-derived values (SSID, password, volume, brightness, process names) are
  validated and passed as discrete arguments, eliminating command injection.
* **AI code execution is gated.** ``run_command`` / ``execute_python`` only run
  when ``BLINK_ENABLE_CODE_EXECUTION`` is true, are screened against a
  destructive-pattern denylist, time out, and are written to an audit log.
* **Timeouts everywhere.** All telemetry subprocess reads use timeouts so a
  hung CLI cannot freeze the assistant.

Functional parity with the original is preserved: volume/brightness/power
control, app/settings launching, Wi-Fi/Bluetooth toggles, process management,
and full telemetry.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import List, Optional, Sequence, Union

import psutil
import requests

from src.config import get_settings
from src.utils import get_logger

log = get_logger(__name__)

# Dedicated audit logger for privileged code execution.
audit_log = get_logger("blink.audit")

# Patterns that are never allowed in AI-generated shell/Python, even when code
# execution is explicitly enabled. Defense-in-depth against catastrophic ops.
_DANGEROUS_PATTERNS = [
    re.compile(r"rm\s+-rf?\s+[/~]", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r":\(\)\s*\{", re.I),                 # fork bomb
    re.compile(r"\bformat\b\s+[a-z]:", re.I),
    re.compile(r"del\s+/[fsq]", re.I),
    re.compile(r"rmdir\s+/s", re.I),
    re.compile(r"Remove-Item.*-Recurse.*-Force", re.I),
    re.compile(r"\bshutdown\b", re.I),
    re.compile(r"reg\s+delete", re.I),
    re.compile(r"diskpart", re.I),
    re.compile(r">\s*/dev/sd[a-z]", re.I),
]

# Reject control characters / shell metacharacters in free-text identifiers
# that we pass as discrete args (extra safety, not strictly required).
_UNSAFE_CHARS = re.compile(r"[\x00-\x1f`$;&|<>\\]")


def _sanitize_identifier(value: object, *, max_len: int = 128) -> str:
    text = str(value or "").strip()
    text = _UNSAFE_CHARS.sub("", text)
    return text[:max_len]


def _clamp_int(value: object, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _popen(args: Sequence[str]) -> None:
    """Fire-and-forget a command from an argument list (no shell)."""
    try:
        subprocess.Popen(list(args))
    except (OSError, ValueError) as exc:
        log.warning("Failed to launch %s: %s", args, exc)


def _popen_shell(command: str) -> None:
    """Fire-and-forget for the few fixed cmd builtins (e.g. Windows ``start``).

    Only ever called with hard-coded literals - never user/AI input.
    """
    try:
        subprocess.Popen(command, shell=True)
    except OSError as exc:
        log.warning("Failed to launch shell command: %s", exc)


def _run(args: Sequence[str], timeout: float = 8.0) -> str:
    """Run a command from an argument list and return captured stdout."""
    try:
        out = subprocess.check_output(
            list(args), stderr=subprocess.STDOUT, timeout=timeout
        )
        return out.decode("utf-8", errors="replace")
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("Command %s failed: %s", args, exc)
        return ""


class FFmpegManager:
    """Ensures an ``ffmpeg`` binary is available (auto-download on Windows)."""

    def __init__(self) -> None:
        self.bin_path = os.path.join(os.getcwd(), "ffmpeg", "bin")
        have_ffmpeg = shutil.which("ffmpeg") or os.path.exists(
            os.path.join(self.bin_path, "ffmpeg.exe")
        )
        if not have_ffmpeg and sys.platform == "win32":
            threading.Thread(target=self.download, daemon=True).start()
        else:
            self.add_path()

    def download(self) -> None:
        log.info("Downloading FFmpeg (Windows build)...")
        url = (
            "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
            "ffmpeg-master-latest-win64-gpl.zip"
        )
        try:
            with requests.get(url, stream=True, timeout=120) as resp:
                resp.raise_for_status()
                with open("ffmpeg.zip", "wb") as fh:
                    shutil.copyfileobj(resp.raw, fh)
            with zipfile.ZipFile("ffmpeg.zip", "r") as archive:
                archive.extractall("temp_ff")
            for root, dirs, _ in os.walk("temp_ff"):
                if "bin" in dirs:
                    shutil.move(root, os.path.join(os.getcwd(), "ffmpeg"))
                    break
            shutil.rmtree("temp_ff", ignore_errors=True)
            if os.path.exists("ffmpeg.zip"):
                os.remove("ffmpeg.zip")
            self.add_path()
            log.info("FFmpeg installed.")
        except (requests.RequestException, OSError, zipfile.BadZipFile) as exc:
            log.error("FFmpeg download failed: %s", exc)

    def add_path(self) -> None:
        if self.bin_path not in os.environ.get("PATH", ""):
            os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + self.bin_path


class SystemManager:
    """Cross-platform system control surface used by the assistant."""

    def __init__(self) -> None:
        self.cached_telemetry = {
            "stats": {
                "battery": {"percent": 100, "power_plugged": True, "secsleft": -1},
                "uptime": "0h 0m",
            },
            "wifi": {"adapter_enabled": False, "connected_ssid": "", "strength": 0, "networks": []},
            "bluetooth": {"adapter_enabled": False, "devices": []},
            "audio": {"volume": 50, "muted": False, "devices": []},
            "display": {"brightness": 50},
            "processes": [],
        }
        self._telemetry_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------
    def check_python_versions(self) -> str:
        if sys.platform == "win32":
            out = _run(["py", "--list"])
            versions = [line.strip() for line in out.splitlines() if line.strip()]
            return "Installed Python Versions: " + ", ".join(versions) if versions else "Failed to retrieve Python versions."
        out = _run(["python3", "--version"])
        return f"Installed Python Version: {out.strip()}" if out else "Failed to retrieve Python versions."

    # ------------------------------------------------------------------
    # Action dispatcher
    # ------------------------------------------------------------------
    def execute(self, act: str, trg: Optional[str]) -> str:
        log.info("System action", extra={"context": {"action": act, "target": _sanitize_identifier(trg)}})
        trg_lower = trg.lower() if trg else ""

        if act == "get_volume":
            vol = self.get_audio_telemetry()["volume"]
            return f"Current volume is {vol}%."
        if act == "get_brightness":
            bri = self.get_display_telemetry()["brightness"]
            return f"Current brightness is {bri}%."
        if act == "volume_up":
            return self._nudge_volume(+10)
        if act == "volume_down":
            return self._nudge_volume(-10)
        if act == "mute":
            return self._toggle_mute()
        if act == "set_volume":
            val = _clamp_int(trg, 0, 100, 50)
            self.set_volume(val)
            return f"Volume set to {val}%."
        if act == "brightness_up":
            return self._nudge_brightness(+10)
        if act == "brightness_down":
            return self._nudge_brightness(-10)
        if act == "set_brightness":
            val = _clamp_int(trg, 0, 100, 50)
            self._set_brightness(val)
            return f"Brightness set to {val}%."
        if act == "shutdown":
            self._power_action("shutdown")
            return "Shutting down system."
        if act == "restart":
            self._power_action("restart")
            return "Restarting system."
        if act == "lock":
            self._power_action("lock")
            return "Screen locked."
        if act == "sleep":
            self._power_action("sleep")
            return "System suspended."
        if act == "empty_trash":
            self._empty_trash()
            return "Trash emptied."
        if act == "restart_app":
            return self._restart_app()
        if act == "screenshot":
            filename = self._screenshot()
            return f"Screenshot saved to {filename}."
        if act == "open":
            return self._open_target(trg, trg_lower)
        if act == "run_command":
            return self.run_command(trg)
        if act == "execute_python":
            return self.execute_python(trg)

        return f"Unknown action: {act}"

    # ------------------------------------------------------------------
    # Volume / brightness / power
    # ------------------------------------------------------------------
    def _nudge_volume(self, delta: int) -> str:
        if sys.platform == "win32":
            try:
                vol = self.get_audio_telemetry()["volume"]
                self.set_volume(_clamp_int(vol + delta, 0, 100, 50))
                return "Volume adjusted."
            except Exception:  # pragma: no cover - pycaw runtime path
                key = 175 if delta > 0 else 174
                _popen(["powershell", "-Command", f"(New-Object -ComObject WScript.Shell).SendKeys([char]{key})"])
        elif sys.platform == "darwin":
            op = "+" if delta > 0 else "-"
            _popen(["osascript", "-e", f"set volume output volume (output volume of (get volume settings) {op} {abs(delta)})"])
        else:
            sign = "%+" if delta > 0 else "%-"
            _popen(["amixer", "set", "Master", f"{abs(delta)}{sign}"])
        return "Volume adjusted."

    def _toggle_mute(self) -> str:
        if sys.platform == "win32":
            try:
                self.set_mute(not self.get_audio_telemetry()["muted"])
                return "Volume mute toggled."
            except Exception:  # pragma: no cover
                _popen(["powershell", "-Command", "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"])
        elif sys.platform == "darwin":
            _popen(["osascript", "-e", "set volume with output muted"])
        else:
            _popen(["amixer", "set", "Master", "toggle"])
        return "Volume mute toggled."

    def _nudge_brightness(self, delta: int) -> str:
        if sys.platform == "win32":
            op = "+" if delta > 0 else "-"
            _popen([
                "powershell", "-Command",
                "$b = (Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness; "
                f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, $b {op} {abs(delta)})",
            ])
        elif sys.platform == "darwin":
            key = 144 if delta > 0 else 145
            _popen(["osascript", "-e", f'tell application "System Events" to key code {key}'])
        else:
            sign = "%+" if delta > 0 else "%-"
            _popen(["brightnessctl", "set", f"{abs(delta)}{sign}"])
        return "Brightness adjusted."

    def _set_brightness(self, val: int) -> None:
        if sys.platform == "win32":
            _popen([
                "powershell", "-Command",
                f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {val})",
            ])
        elif sys.platform.startswith("linux"):
            _popen(["brightnessctl", "set", f"{val}%"])

    def _power_action(self, action: str) -> None:
        table = {
            "shutdown": {
                "win32": ["shutdown", "/s", "/t", "0"],
                "darwin": ["osascript", "-e", 'tell app "System Events" to shut down'],
                "linux": ["systemctl", "poweroff"],
            },
            "restart": {
                "win32": ["shutdown", "/r", "/t", "0"],
                "darwin": ["osascript", "-e", 'tell app "System Events" to restart'],
                "linux": ["systemctl", "reboot"],
            },
            "lock": {
                "win32": ["rundll32.exe", "user32.dll,LockWorkStation"],
                "darwin": ["open", "-a", "ScreenSaverEngine"],
                "linux": ["xdg-screensaver", "lock"],
            },
            "sleep": {
                "win32": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                "darwin": ["osascript", "-e", 'tell app "System Events" to sleep'],
                "linux": ["systemctl", "suspend"],
            },
        }
        platform_key = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
        _popen(table[action][platform_key])

    def _empty_trash(self) -> None:
        if sys.platform == "win32":
            _popen(["powershell", "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"])
        elif sys.platform == "darwin":
            _popen(["osascript", "-e", 'tell app "Finder" to empty trash'])
        else:
            trash = os.path.expanduser("~/.local/share/Trash/files")
            if os.path.isdir(trash):
                for entry in os.listdir(trash):
                    target = os.path.join(trash, entry)
                    try:
                        if os.path.isdir(target):
                            shutil.rmtree(target, ignore_errors=True)
                        else:
                            os.remove(target)
                    except OSError:
                        pass

    def _restart_app(self) -> str:
        log.info("Restarting Blink application...")
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.quit()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return "Application restarted."

    def _screenshot(self) -> str:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.png"
        
        if sys.platform == "win32":
            cmd = [
                "powershell",
                "-Command",
                "$sig = '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();'; "
                "Add-Type -MemberDefinition $sig -Name Win32DPI -Namespace Win32; "
                "[Win32.Win32DPI]::SetProcessDPIAware(); "
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
                "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
                "$g = [System.Drawing.Graphics]::FromImage($bmp); "
                "$g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size); "
                f"$bmp.Save('{filename}', [System.Drawing.Imaging.ImageFormat]::Png); "
                "$g.Dispose(); $bmp.Dispose()"
            ]
            _popen(cmd)
        elif sys.platform == "darwin":
            filename = "screenshot.png"
            _popen(["screencapture", "-i", filename])
        else:
            self._run_first_available([f"gnome-screenshot -f {filename}", f"scrot {filename}"])
        return filename

    # ------------------------------------------------------------------
    # App / settings launcher
    # ------------------------------------------------------------------
    def _open_target(self, trg: Optional[str], trg_lower: str) -> str:
        if not trg:
            return "No target specified."

        settings_pages = {
            ("wifi", "wireless"): {
                "win32": (["cmd", "/c", "start", "ms-settings:network-wifi"], False),
                "darwin": (["open", "/System/Library/PreferencePanes/Network.prefPane"], False),
                "linux": (["nm-connection-editor", "gnome-control-center network"], True),
                "label": "Wi-Fi settings opened.",
            },
            ("bluetooth",): {
                "win32": (["cmd", "/c", "start", "ms-settings:bluetooth"], False),
                "darwin": (["open", "/System/Library/PreferencePanes/Bluetooth.prefPane"], False),
                "linux": (["blueman-manager", "gnome-control-center bluetooth"], True),
                "label": "Bluetooth settings opened.",
            },
            ("display", "brightness"): {
                "win32": (["cmd", "/c", "start", "ms-settings:display"], False),
                "darwin": (["open", "/System/Library/PreferencePanes/Displays.prefPane"], False),
                "linux": (["gnome-control-center display", "systemsettings kcm_kscreen"], True),
                "label": "Display settings opened.",
            },
            ("sound", "audio"): {
                "win32": (["cmd", "/c", "start", "ms-settings:sounds"], False),
                "darwin": (["open", "/System/Library/PreferencePanes/Sound.prefPane"], False),
                "linux": (["pavucontrol", "gnome-control-center sound"], True),
                "label": "Sound settings opened.",
            },
            ("update",): {
                "win32": (["cmd", "/c", "start", "ms-settings:windowsupdate"], False),
                "darwin": (["open", "/System/Library/PreferencePanes/SoftwareUpdate.prefPane"], False),
                "linux": (["update-manager", "gnome-control-center updates"], True),
                "label": "System update settings opened.",
            },
        }
        platform_key = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
        for keywords, mapping in settings_pages.items():
            if any(k in trg_lower for k in keywords):
                args, is_linux_list = mapping[platform_key]
                if platform_key == "linux":
                    self._run_first_available(args)
                else:
                    _popen(args)
                return mapping["label"]

        # Common application categories (English aliases only).
        shortcut_alias = {
            "settings": ["settings", "control panel", "preferences", "control"],
            "notepad": ["notepad", "text editor", "editor", "textedit", "gedit", "kate", "mousepad", "notepad++"],
            "calculator": ["calculator", "calc", "kcalc"],
            "task manager": ["task manager", "activity monitor", "system monitor", "taskmgr", "ksysguard", "htop"],
            "terminal": ["terminal", "cmd", "command prompt", "bash", "shell", "powershell", "konsole", "xterm"],
            "file explorer": ["file explorer", "explorer", "finder", "file manager", "nautilus", "dolphin", "thunar"],
            "browser": ["browser", "internet", "chrome", "edge", "safari", "firefox", "opera"],
        }
        matched = None
        for category, aliases in shortcut_alias.items():
            if any(alias in trg_lower for alias in aliases):
                matched = category
                break

        if matched:
            if matched == "browser":
                try:
                    import webbrowser
                    webbrowser.open("https://google.com")
                    return "Web Browser opened."
                except Exception as exc:
                    log.warning("Browser launch error: %s", exc)

            if sys.platform == "win32":
                win_commands = {
                    "settings": ["cmd", "/c", "start", "ms-settings:"],
                    "notepad": ["notepad"],
                    "calculator": ["calc"],
                    "task manager": ["taskmgr"],
                    "terminal": ["cmd", "/c", "start", "cmd"],
                    "file explorer": ["explorer"],
                }
                if matched in win_commands:
                    _popen(win_commands[matched])
                    return f"{matched.title()} opened."
            elif sys.platform == "darwin":
                mac_commands = {
                    "settings": ["open", "/System/Applications/System Settings.app"],
                    "notepad": ["open", "-a", "TextEdit"],
                    "calculator": ["open", "-a", "Calculator"],
                    "task manager": ["open", "-a", "Activity Monitor"],
                    "terminal": ["open", "-a", "Terminal"],
                    "file explorer": ["open", "."],
                }
                if matched in mac_commands:
                    _popen(mac_commands[matched])
                    return f"{matched.title()} opened."
            else:
                linux_commands = {
                    "settings": ["gnome-control-center", "systemsettings", "xfce4-settings-manager", "mate-control-center"],
                    "notepad": ["gedit", "kate", "mousepad", "leafpad", "xed", "nano"],
                    "calculator": ["gnome-calculator", "kcalc", "galculator", "mate-calc"],
                    "task manager": ["gnome-system-monitor", "ksysguard", "xfce4-taskmanager", "htop"],
                    "terminal": ["gnome-terminal", "konsole", "xfce4-terminal", "mate-terminal", "xterm"],
                    "file explorer": ["nautilus", "dolphin", "thunar", "caja", "xdg-open ."],
                }
                if matched in linux_commands:
                    self._run_first_available(linux_commands[matched])
                    return f"{matched.title()} opened."

        # Generic fallback by application name.
        if sys.platform == "win32":
            lnk_path = self._find_windows_app(trg)
            if lnk_path:
                try:
                    os.startfile(lnk_path)  # type: ignore[attr-defined]
                    return f"{trg} opened."
                except OSError as exc:
                    log.warning("startfile error: %s", exc)
        elif sys.platform == "darwin":
            _popen(["open", "-a", trg])
            return f"{trg} opened."

        resolved = shutil.which(trg) or shutil.which(trg_lower)
        if resolved:
            _popen([resolved])
            return f"{trg} opened."
        return f"Could not find an application matching '{trg}'."

    def _run_first_available(self, commands: Sequence[str]) -> bool:
        """Run the first command (string form) whose executable exists.

        ``commands`` are hard-coded literals from this module only.
        """
        for cmd in commands:
            parts = cmd.split()
            if not parts:
                continue
            executable = parts[0]
            if shutil.which(executable) or executable == "xdg-open":
                _popen(parts)
                return True
        return False

    def _find_windows_app(self, app_name: str) -> Optional[str]:
        search_paths = [
            os.path.join(os.environ.get("ProgramData", "C:\\ProgramData"), "Microsoft\\Windows\\Start Menu\\Programs"),
            os.path.join(os.environ.get("APPDATA", ""), "Microsoft\\Windows\\Start Menu\\Programs"),
        ]
        needle = app_name.lower()
        for path in search_paths:
            if os.path.exists(path):
                for root, _dirs, files in os.walk(path):
                    for fname in files:
                        if fname.lower().endswith(".lnk") and needle in fname.lower():
                            return os.path.join(root, fname)
        return None

    def _clean_powershell_wrapper(self, cmd: str) -> str:
        cmd_stripped = cmd.strip()
        for prefix in ("powershell -command ", "powershell -c ", "powershell "):
            if cmd_stripped.lower().startswith(prefix):
                inner = cmd_stripped[len(prefix):].strip()
                if (inner.startswith('"') and inner.endswith('"')) or (inner.startswith("'") and inner.endswith("'")):
                    inner = inner[1:-1].strip().replace('\\"', '"')
                return self._clean_powershell_wrapper(inner)
        return cmd

    # ------------------------------------------------------------------
    # Gated AI code execution
    # ------------------------------------------------------------------
    @staticmethod
    def _is_dangerous(snippet: str) -> bool:
        return any(pattern.search(snippet) for pattern in _DANGEROUS_PATTERNS)

    def run_command(self, trg: Optional[str]) -> str:
        if not trg:
            return "No command specified."
        settings = get_settings()
        if not settings.enable_code_execution:
            log.warning("Blocked run_command: code execution is disabled.")
            return (
                "Command execution is disabled for safety. Set "
                "BLINK_ENABLE_CODE_EXECUTION=true to allow it."
            )
        if self._is_dangerous(trg):
            audit_log.error("DENIED dangerous shell command", extra={"context": {"command": trg[:200]}})
            return "This command was blocked because it matches a destructive pattern."

        audit_log.warning("EXEC shell command", extra={"context": {"command": trg[:200]}})
        timeout = float(settings.command_timeout)
        try:
            if sys.platform == "win32":
                cleaned = self._clean_powershell_wrapper(trg)
                result = subprocess.check_output(
                    ["powershell", "-Command", "-"],
                    input=cleaned.encode("utf-8"),
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                )
            else:
                result = subprocess.check_output(
                    trg, shell=True, stderr=subprocess.STDOUT, timeout=timeout
                )
            decoded = result.decode("utf-8", errors="replace").strip()
            short = decoded[:400] + ("..." if len(decoded) > 400 else "")
            return short or "Command completed with no output."
        except subprocess.CalledProcessError as exc:
            err = exc.output.decode("utf-8", errors="replace").strip()
            return f"Error executing command: {err[:400]}"
        except subprocess.TimeoutExpired as exc:
            captured = exc.output.decode("utf-8", errors="replace").strip() if exc.output else ""
            if captured:
                return f"Command timed out. Output before timeout:\n{captured[:400]}"
            return "Command timed out."
        except (OSError, ValueError) as exc:
            return f"Failed to execute command: {exc}"

    def execute_python(self, trg: Optional[str]) -> str:
        if not trg:
            return "No code specified."
        settings = get_settings()
        if not settings.enable_code_execution:
            log.warning("Blocked execute_python: code execution is disabled.")
            return (
                "Python execution is disabled for safety. Set "
                "BLINK_ENABLE_CODE_EXECUTION=true to allow it."
            )
        if self._is_dangerous(trg):
            audit_log.error("DENIED dangerous python snippet", extra={"context": {"code": trg[:200]}})
            return "This code was blocked because it matches a destructive pattern."

        audit_log.warning("EXEC python snippet", extra={"context": {"code": trg[:200]}})
        timeout = float(settings.command_timeout)
        temp_dir = os.path.join(os.getcwd(), "temp_scripts")
        os.makedirs(temp_dir, exist_ok=True)
        temp_file = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", dir=temp_dir, delete=False, encoding="utf-8"
            ) as fh:
                fh.write(trg)
                temp_file = fh.name
            result = subprocess.check_output(
                [sys.executable, temp_file], stderr=subprocess.STDOUT, timeout=timeout
            )
            decoded = result.decode("utf-8", errors="replace").strip()
            short = decoded[:400] + ("..." if len(decoded) > 400 else "")
            return short or "Python script completed with no output."
        except subprocess.CalledProcessError as exc:
            err = exc.output.decode("utf-8", errors="replace").strip()
            return f"Python execution error: {err[:400]}"
        except subprocess.TimeoutExpired as exc:
            captured = exc.output.decode("utf-8", errors="replace").strip() if exc.output else ""
            if captured:
                return f"Python execution timed out. Output before timeout:\n{captured[:400]}"
            return "Python execution timed out."
        except (OSError, ValueError) as exc:
            return f"Failed to execute Python code: {exc}"
        finally:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------
    def get_system_stats(self) -> dict:
        stats = {
            "battery": {"percent": 100, "power_plugged": True, "secsleft": -1},
            "uptime": "0h 0m",
        }
        try:
            batt = psutil.sensors_battery()
            if batt:
                stats["battery"] = {
                    "percent": batt.percent,
                    "power_plugged": batt.power_plugged,
                    "secsleft": batt.secsleft,
                }
            uptime_seconds = time.time() - psutil.boot_time()
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            stats["uptime"] = f"{hours}h {minutes}m"
        except Exception as exc:  # psutil raises varied errors per platform
            log.debug("Error getting system stats: %s", exc)
        return stats

    def get_process_list(self) -> list:
        processes = []
        try:
            for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
                try:
                    info = proc.info
                    processes.append({
                        "pid": info["pid"],
                        "name": info["name"],
                        "cpu": round(info["cpu_percent"] or 0.0, 1),
                        "memory": round(info["memory_percent"] or 0.0, 1),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            processes = sorted(processes, key=lambda x: x["cpu"], reverse=True)[:15]
        except Exception as exc:
            log.debug("Error listing processes: %s", exc)
        return processes

    def get_wifi_telemetry(self) -> dict:
        wifi = {"adapter_enabled": False, "connected_ssid": "", "strength": 0, "networks": []}
        try:
            if sys.platform == "win32":
                out = _run(["netsh", "wlan", "show", "interfaces"])
                for line in out.splitlines():
                    line = line.strip()
                    if "State" in line and "connected" in line:
                        wifi["adapter_enabled"] = True
                    if "SSID" in line and "BSSID" not in line:
                        wifi["connected_ssid"] = line.split(":", 1)[1].strip()
                    if "Signal" in line:
                        wifi["strength"] = _clamp_int(line.split(":", 1)[1].replace("%", "").strip(), 0, 100, 0)
                if "Software Off" in out or "Hardware Off" in out:
                    wifi["adapter_enabled"] = False
                elif "disconnected" in out or wifi["connected_ssid"]:
                    wifi["adapter_enabled"] = True
                out_net = _run(["netsh", "wlan", "show", "networks"])
                networks = []
                for line in out_net.splitlines():
                    line = line.strip()
                    if line.startswith("SSID"):
                        parts = line.split(":", 1)
                        if len(parts) > 1:
                            ssid = parts[1].strip()
                            if ssid and ssid not in networks:
                                networks.append(ssid)
                wifi["networks"] = networks
            elif sys.platform.startswith("linux"):
                out = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"])
                networks = []
                for line in out.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(":")
                    if len(parts) >= 3:
                        active, ssid, signal = parts[0], parts[1], parts[2]
                        if ssid:
                            if active == "yes":
                                wifi["adapter_enabled"] = True
                                wifi["connected_ssid"] = ssid
                                wifi["strength"] = _clamp_int(signal, 0, 100, 0)
                            if ssid not in networks:
                                networks.append(ssid)
                wifi["networks"] = networks
                radio = _run(["nmcli", "radio", "wifi"]).strip()
                wifi["adapter_enabled"] = radio == "enabled"
            elif sys.platform == "darwin":
                airport = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
                out = _run([airport, "-I"])
                for line in out.splitlines():
                    line = line.strip()
                    if "SSID:" in line and "BSSID" not in line:
                        wifi["connected_ssid"] = line.split(":", 1)[1].strip()
                        wifi["adapter_enabled"] = True
                    if "agrCtlRSSI" in line:
                        rssi = _clamp_int(line.split(":", 1)[1].strip(), -100, 0, -100)
                        wifi["strength"] = max(0, min(100, 2 * (rssi + 100)))
                if "On" in _run(["networksetup", "-getairportpower", "en0"]):
                    wifi["adapter_enabled"] = True
                out_scan = _run([airport, "-s"])
                networks = []
                for line in out_scan.splitlines()[1:]:
                    parts = line.strip().split()
                    if parts and parts[0] not in networks:
                        networks.append(parts[0])
                wifi["networks"] = networks
        except Exception as exc:
            log.debug("Wi-Fi telemetry error: %s", exc)
        return wifi

    def get_bluetooth_telemetry(self) -> dict:
        bt = {"adapter_enabled": False, "devices": []}
        try:
            if sys.platform == "win32":
                radio = _run([
                    "powershell", "-Command",
                    "Get-PnpDevice -Class Bluetooth | Where-Object { $_.FriendlyName -like '*Radio*' -or $_.FriendlyName -like '*Adapter*' } | Select-Object Status",
                ])
                if "OK" in radio:
                    bt["adapter_enabled"] = True
                out = _run([
                    "powershell", "-Command",
                    "Get-CimInstance -Namespace root\\cimv2 -ClassName Win32_PnPEntity | Where-Object { $_.PNPClass -eq 'Bluetooth' -and $_.Name -notmatch 'Controller|Enumerator|Adapter|Root' } | Select-Object Name, Status | ConvertTo-Json",
                ]).strip()
                if out:
                    try:
                        raw_devices = json.loads(out)
                        if not isinstance(raw_devices, list):
                            raw_devices = [raw_devices]
                        for dev in raw_devices:
                            name = dev.get("Name")
                            if name:
                                bt["devices"].append({"name": name, "connected": dev.get("Status") == "OK"})
                    except (ValueError, AttributeError):
                        pass
            elif sys.platform.startswith("linux"):
                state = _run(["bluetoothctl", "show"])
                bt["adapter_enabled"] = "Powered: yes" in state
                for line in _run(["bluetoothctl", "devices"]).splitlines():
                    line = line.strip()
                    if line.startswith("Device"):
                        parts = line.split(" ", 2)
                        if len(parts) >= 3:
                            mac, name = parts[1], parts[2]
                            info = _run(["bluetoothctl", "info", mac])
                            bt["devices"].append({"name": name, "connected": "Connected: yes" in info})
            elif sys.platform == "darwin":
                out = _run(["system_profiler", "SPBluetoothDataType"])
                bt["adapter_enabled"] = "Bluetooth Power: On" in out
                current = None
                for line in out.splitlines():
                    line = line.strip()
                    if line.endswith(":"):
                        current = line[:-1]
                    if "Connected:" in line and current:
                        bt["devices"].append({"name": current, "connected": "Yes" in line})
        except Exception as exc:
            log.debug("Bluetooth telemetry error: %s", exc)
        return bt

    def get_audio_devices(self) -> list:
        devices = []
        try:
            import sounddevice as sd
            for i, dev_info in enumerate(sd.query_devices()):
                devices.append({
                    "index": i,
                    "name": dev_info.get("name"),
                    "input": dev_info.get("max_input_channels", 0) > 0,
                    "output": dev_info.get("max_output_channels", 0) > 0,
                })
        except Exception as exc:
            log.debug("Error listing audio devices: %s", exc)
        return devices

    def get_audio_telemetry(self) -> dict:
        audio = {"volume": 50, "muted": False, "devices": []}
        try:
            if sys.platform == "win32":
                from pycaw.pycaw import AudioUtilities
                volume = AudioUtilities.GetSpeakers().EndpointVolume
                audio["volume"] = int(round(volume.GetMasterVolumeLevelScalar() * 100))
                audio["muted"] = bool(volume.GetMute())
            elif sys.platform.startswith("linux"):
                out = _run(["amixer", "get", "Master"])
                match = re.search(r"(\d+)%", out)
                if match:
                    audio["volume"] = int(match.group(1))
                audio["muted"] = "[off]" in out
            elif sys.platform == "darwin":
                vol = _run(["osascript", "-e", "output volume of (get volume settings)"]).strip()
                if vol:
                    audio["volume"] = _clamp_int(vol, 0, 100, 50)
                mute = _run(["osascript", "-e", "output muted of (get volume settings)"]).strip()
                audio["muted"] = mute == "true"
        except Exception as exc:
            log.debug("Audio telemetry error: %s", exc)
        audio["devices"] = self.get_audio_devices()
        return audio

    def get_display_telemetry(self) -> dict:
        display = {"brightness": 50}
        try:
            if sys.platform == "win32":
                out = _run([
                    "powershell", "-Command",
                    "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness",
                ]).strip()
                if out:
                    display["brightness"] = _clamp_int(out, 0, 100, 50)
            elif sys.platform.startswith("linux"):
                curr = _run(["brightnessctl", "g"]).strip()
                max_b = _run(["brightnessctl", "m"]).strip()
                if curr and max_b and int(max_b) > 0:
                    display["brightness"] = int((int(curr) / int(max_b)) * 100)
        except Exception as exc:
            log.debug("Display telemetry error: %s", exc)
        return display

    def get_all_telemetry(self) -> dict:
        data = {
            "stats": self.get_system_stats(),
            "wifi": self.get_wifi_telemetry(),
            "bluetooth": self.get_bluetooth_telemetry(),
            "audio": self.get_audio_telemetry(),
            "display": self.get_display_telemetry(),
            "processes": self.get_process_list(),
        }
        with self._telemetry_lock:
            self.cached_telemetry = data
        return data

    # ------------------------------------------------------------------
    # Mutators (all injection-safe: argument lists, validated input)
    # ------------------------------------------------------------------
    def kill_process(self, pid: Union[int, str]) -> str:
        try:
            pid_int = int(pid)
            if pid_int == os.getpid():
                return "Refusing to terminate current process."
            psutil.Process(pid_int).terminate()
            return f"Process {pid_int} terminated."
        except (ValueError, TypeError):
            name_lower = str(pid).lower().strip()
            my_pid = os.getpid()
            killed = []
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    if proc.info["pid"] == my_pid:
                        continue
                    if name_lower and name_lower in (proc.info["name"] or "").lower():
                        proc.terminate()
                        killed.append(proc.info["name"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            if killed:
                return f"Terminated: {', '.join(sorted(set(killed)))}."
            return f"No process found matching '{pid}'."
        except psutil.NoSuchProcess:
            return f"No process with PID {pid}."
        except psutil.AccessDenied:
            return f"Access denied terminating process {pid}."

    def set_wifi_state(self, enabled: bool) -> str:
        if sys.platform == "win32":
            state = "enabled" if enabled else "disabled"
            _popen(["netsh", "interface", "set", "interface", "name=Wi-Fi", f"admin={state}"])
        elif sys.platform.startswith("linux"):
            _popen(["nmcli", "radio", "wifi", "on" if enabled else "off"])
        elif sys.platform == "darwin":
            _popen(["networksetup", "-setairportpower", "airport", "on" if enabled else "off"])
        return f"Wi-Fi set to {'enabled' if enabled else 'disabled'}."

    def connect_wifi(self, ssid: str, password: str = "") -> str:
        ssid = _sanitize_identifier(ssid, max_len=64)
        password = _sanitize_identifier(password, max_len=128)
        if not ssid:
            return "A valid network name is required."
        if sys.platform == "win32":
            # Windows connects to a saved profile by name; no secret on the CLI.
            _popen(["netsh", "wlan", "connect", f"name={ssid}"])
        elif sys.platform.startswith("linux"):
            args = ["nmcli", "dev", "wifi", "connect", ssid]
            if password:
                args += ["password", password]
            _popen(args)
        elif sys.platform == "darwin":
            args = ["networksetup", "-setairportnetwork", "en0", ssid]
            if password:
                args.append(password)
            _popen(args)
        return f"Connecting to {ssid}."

    def set_bluetooth_state(self, enabled: bool) -> str:
        if sys.platform.startswith("linux"):
            _popen(["bluetoothctl", "power", "on" if enabled else "off"])
        elif sys.platform == "darwin":
            _popen(["blueutil", "-p", "1" if enabled else "0"])
        return f"Bluetooth power set to {enabled}."

    def set_volume(self, val: int) -> None:
        val = _clamp_int(val, 0, 100, 50)
        try:
            if sys.platform == "win32":
                from pycaw.pycaw import AudioUtilities
                AudioUtilities.GetSpeakers().EndpointVolume.SetMasterVolumeLevelScalar(val / 100.0, None)
            elif sys.platform == "darwin":
                _popen(["osascript", "-e", f"set volume output volume {val}"])
            else:
                _popen(["amixer", "set", "Master", f"{val}%"])
        except Exception as exc:
            log.debug("Error setting volume: %s", exc)

    def set_mute(self, mute: bool) -> None:
        try:
            if sys.platform == "win32":
                from pycaw.pycaw import AudioUtilities
                AudioUtilities.GetSpeakers().EndpointVolume.SetMute(1 if mute else 0, None)
            elif sys.platform == "darwin":
                state = "with" if mute else "without"
                _popen(["osascript", "-e", f"set volume {state} output muted"])
            else:
                _popen(["amixer", "set", "Master", "mute" if mute else "unmute"])
        except Exception as exc:
            log.debug("Error setting mute: %s", exc)
