"""Native Windows operations: app index + launch, graceful window close, media keys, system actions.

Everything here avoids spawning a shell on the hot path except the one-off Start-menu scan.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

APP_CACHE_MAX_AGE = 24 * 3600


# --------------------------------------------------------------------------- apps

@dataclass(frozen=True)
class App:
    name: str
    app_id: str


class AppIndex:
    """Every app in the Start menu (desktop + Store apps), via Get-StartApps, cached to disk."""

    def __init__(self, cache_path: Path, aliases: dict[str, str]):
        self.cache_path = cache_path
        self.aliases = aliases
        self.apps: dict[str, App] = {}  # lowercase name -> App

    async def load(self) -> None:
        fresh = self.cache_path.exists() and time.time() - self.cache_path.stat().st_mtime < APP_CACHE_MAX_AGE
        if self.cache_path.exists():
            self._set(json.loads(self.cache_path.read_text(encoding="utf-8")))
        if not fresh:
            if self.apps:
                asyncio.create_task(self.refresh())  # serve stale cache, refresh in background
            else:
                await self.refresh()

    async def refresh(self) -> None:
        cmd = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"
        )
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        try:
            rows = json.loads(out.decode("utf-8", errors="replace") or "[]")
        except json.JSONDecodeError:
            return
        if isinstance(rows, dict):
            rows = [rows]
        rows = [{"name": r["Name"], "app_id": r["AppID"]} for r in rows if r.get("Name") and r.get("AppID")]
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(rows), encoding="utf-8")
        self._set(rows)

    def _set(self, rows: list[dict]) -> None:
        self.apps = {r["name"].lower(): App(r["name"], r["app_id"]) for r in rows}

    def names(self) -> list[str]:
        return [a.name for a in self.apps.values()]

    def candidates(self, text: str, limit: int = 60) -> list[str]:
        """Apps plausibly mentioned in `text` — the option list Jev chooses from."""
        names = self.names()
        if not names:
            return []
        # Score each app against every short span of the request, so "open spotify please"
        # compares "Spotify" with "spotify" rather than with the whole sentence.
        words = re.findall(r"[\w.+#-]+", text.lower())
        spans = {" ".join(words[i:j]) for i in range(len(words)) for j in range(i + 1, min(len(words), i + 4) + 1)}
        scored = []
        for name in names:
            best = max((fuzz.ratio(name.lower(), s) for s in spans), default=0)
            best = max(best, fuzz.token_set_ratio(name.lower(), text.lower()) if len(name) > 3 else 0)
            if best >= 70:
                scored.append((best, name))
        picked = [name for _, name in sorted(scored, key=lambda x: -x[0])]
        # Aliases the user configured are always worth offering when the alias word appears.
        lowered = text.lower()
        for alias, target in self.aliases.items():
            if alias in lowered and target.lower() in self.apps:
                real = self.apps[target.lower()].name
                if real not in picked:
                    picked.insert(0, real)
        return picked[:limit]

    def resolve(self, name: str) -> App | str | None:
        """Name -> App. An alias may point at a path/URI instead, returned as a str."""
        key = name.lower().strip()
        if key in self.aliases:
            target = self.aliases[key]
            return self.apps.get(target.lower(), target)
        if key in self.apps:
            return self.apps[key]
        match = process.extractOne(key, list(self.apps), scorer=fuzz.WRatio, score_cutoff=80)
        return self.apps[match[0]] if match else None

    @staticmethod
    def launch(target: App | str) -> None:
        if isinstance(target, App):
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target.app_id}"])
        else:
            os.startfile(target)


# --------------------------------------------------------------------------- windows

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GW_OWNER = 4
WM_CLOSE = 0x0010


@dataclass
class Window:
    hwnd: int
    title: str
    exe: str  # e.g. "spotify" (no extension)


def _exe_name(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).stem
        return ""
    finally:
        kernel32.CloseHandle(handle)


def list_windows() -> list[Window]:
    windows: list[Window] = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        windows.append(Window(hwnd, buf.value, _exe_name(pid.value)))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return windows


def close_app_windows(name: str, app: App | None = None) -> list[str]:
    """Politely close (WM_CLOSE, like clicking X) every top-level window of the named app."""
    windows = list_windows()
    exe_hint = ""
    if app and app.app_id.lower().endswith(".exe"):
        exe_hint = Path(app.app_id).stem.lower()

    def score(w: Window) -> float:
        if exe_hint and w.exe.lower() == exe_hint:
            return 100
        return max(fuzz.WRatio(name, w.exe), fuzz.partial_ratio(name.lower(), w.title.lower()))

    scored = sorted(((score(w), w) for w in windows), key=lambda x: -x[0])
    if not scored or scored[0][0] < 75:
        return []
    target_exe = scored[0][1].exe.lower()
    closed = []
    for s, w in scored:
        if w.exe.lower() == target_exe:
            user32.PostMessageW(w.hwnd, WM_CLOSE, 0, 0)
            closed.append(w.title)
    return closed


# --------------------------------------------------------------------------- media keys

VK = {
    "play_pause": 0xB3, "pause": 0xB3, "resume": 0xB3, "stop": 0xB2,
    "next": 0xB0, "previous": 0xB1,
    "mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
}
KEYEVENTF_KEYUP = 0x2


def tap_key(vk: int, times: int = 1) -> None:
    for _ in range(times):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def media(action: str, level: int = -1) -> str:
    if action == "set_volume":
        if not 0 <= level <= 100:
            return "Need a volume level between 0 and 100."
        tap_key(VK["volume_down"], 50)          # each key step is 2%
        tap_key(VK["volume_up"], round(level / 2))
        return f"Volume set to {level}%."
    if action in ("volume_up", "volume_down"):
        steps = 5 if level < 0 else max(1, round(level / 2))
        tap_key(VK[action], steps)
        return "Volume up." if action == "volume_up" else "Volume down."
    if action not in VK:
        return f"Unknown media action: {action}"
    tap_key(VK[action])
    return {"play_pause": "Toggled playback.", "pause": "Paused.", "resume": "Resumed.",
            "next": "Next track.", "previous": "Previous track.", "mute": "Toggled mute.",
            "stop": "Stopped."}[action]


# --------------------------------------------------------------------------- system

FOLDERS = {"open_downloads": "Downloads", "open_documents": "Documents", "open_desktop": "Desktop",
           "open_pictures": "Pictures", "open_music": "Music", "open_videos": "Videos"}


def system_action(action: str) -> str:
    now = time.localtime()
    if action == "tell_time":
        return time.strftime("It's %I:%M %p.", now).replace(" 0", " ")
    if action == "tell_date":
        return time.strftime("Today is %A, %B %d, %Y.", now)
    if action == "lock":
        user32.LockWorkStation()
        return "Locked."
    if action == "screenshot":
        os.startfile("ms-screenclip:")
        return "Snipping tool open."
    if action in FOLDERS:
        path = Path.home() / FOLDERS[action]
        os.startfile(path)
        return f"Opened {FOLDERS[action]}."
    if action == "open_settings":
        os.startfile("ms-settings:")
        return "Opened Settings."
    if action == "open_file_explorer":
        subprocess.Popen(["explorer.exe"])
        return "Opened File Explorer."
    return f"Unknown system action: {action}"


SYSTEM_ACTIONS = {
    "tell_time": "Say what time it is",
    "tell_date": "Say today's date",
    "lock": "Lock the computer / screen",
    "screenshot": "Take a screenshot or open the snipping tool",
    "open_downloads": "Open the Downloads folder",
    "open_documents": "Open the Documents folder",
    "open_desktop": "Open the Desktop folder",
    "open_pictures": "Open the Pictures folder",
    "open_music": "Open the Music folder",
    "open_videos": "Open the Videos folder",
    "open_settings": "Open Windows Settings",
    "open_file_explorer": "Open File Explorer",
}

MEDIA_ACTIONS = {
    "play_pause": "Toggle play/pause without saying which",
    "pause": "Pause or stop the music/video",
    "resume": "Resume or unpause playback",
    "next": "Skip this track / go to the next one",
    "previous": "Go back to the previous track",
    "mute": "Mute or unmute the sound",
    "volume_up": "Turn the volume up / louder",
    "volume_down": "Turn the volume down / quieter",
    "set_volume": "Set the volume to a specific level",
}
