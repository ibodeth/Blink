"""Local workspace & installed-apps scanner.

Runs at startup to snapshot (1) workspace files and (2) installed applications,
caching the result to ``workspace_cache.json`` so it can be injected into LLM
prompts without any network calls.

Hardening vs. legacy: structured logging instead of ``print``/``log_debug``,
specific exception handling, and an atomic cache write.
"""

from __future__ import annotations

import json
import os
import platform
import tempfile
from typing import Dict, List, Tuple

from src.utils import get_logger

log = get_logger(__name__)

CACHE_FILE = "workspace_cache.json"

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".gemini", ".idea", ".vscode", "agentools",
    "logs", "temp_scripts", "ffmpeg",
}
IGNORE_EXTS = {".pyc", ".pyo", ".pyd", ".egg-info", ".map", ".lock"}


def scan_workspace(root: str | None = None) -> List[str]:
    """Return a sorted list of workspace file paths relative to ``root``."""
    root = root or os.getcwd()
    files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, root)
        for fn in filenames:
            _, ext = os.path.splitext(fn)
            if ext in IGNORE_EXTS:
                continue
            rel_path = fn if rel_dir == "." else os.path.join(rel_dir, fn)
            files.append(rel_path.replace("\\", "/"))
    return sorted(files)


def _scan_windows_apps() -> List[str]:
    apps = set()
    try:
        import winreg
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, path in keys:
            try:
                key = winreg.OpenKey(hive, path)
            except OSError:
                continue
            try:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                        try:
                            name = winreg.QueryValueEx(sub, "DisplayName")[0]
                            if name and name.strip():
                                apps.add(name.strip())
                        except FileNotFoundError:
                            pass
                        finally:
                            winreg.CloseKey(sub)
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(key)
    except ImportError:
        return []

    for sm_dir in (
        os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
    ):
        if os.path.isdir(sm_dir):
            for _root, _dirs, files in os.walk(sm_dir):
                for f in files:
                    if f.endswith(".lnk"):
                        apps.add(os.path.splitext(f)[0])
    return sorted(apps)


def _scan_macos_apps() -> List[str]:
    try:
        return sorted(n[:-4] for n in os.listdir("/Applications") if n.endswith(".app"))
    except OSError:
        return []


def _scan_linux_apps() -> List[str]:
    desktop_dir = "/usr/share/applications"
    try:
        if os.path.isdir(desktop_dir):
            return sorted(f[:-8] for f in os.listdir(desktop_dir) if f.endswith(".desktop"))
    except OSError:
        pass
    return []


def scan_installed_apps() -> List[str]:
    system = platform.system()
    try:
        if system == "Windows":
            return _scan_windows_apps()
        if system == "Darwin":
            return _scan_macos_apps()
        return _scan_linux_apps()
    except Exception as exc:
        log.warning("App scan error: %s", exc)
        return []


def run_scan_and_cache() -> Dict[str, List[str]]:
    log.info("Running workspace and installed-apps scan...")
    cache = {
        "workspace_files": scan_workspace(),
        "installed_apps": scan_installed_apps(),
    }
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.getcwd(), prefix=".cache.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CACHE_FILE)
        log.info(
            "Scan complete",
            extra={"context": {
                "files": len(cache["workspace_files"]),
                "apps": len(cache["installed_apps"]),
            }},
        )
    except OSError as exc:
        log.warning("Cache write error: %s", exc)
    return cache


def load_cache() -> Dict[str, List[str]]:
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Cache read error: %s", exc)
    return {"workspace_files": [], "installed_apps": []}


def format_for_prompt(cache: Dict[str, List[str]]) -> Tuple[str, str]:
    files = cache.get("workspace_files", [])
    apps = cache.get("installed_apps", [])
    max_files, max_apps = 150, 200

    files_str = "\n".join(files[:max_files])
    if len(files) > max_files:
        files_str += f"\n... ({len(files) - max_files} more files)"

    apps_str = ", ".join(apps[:max_apps])
    if len(apps) > max_apps:
        apps_str += f", ... ({len(apps) - max_apps} more)"
    return files_str, apps_str
