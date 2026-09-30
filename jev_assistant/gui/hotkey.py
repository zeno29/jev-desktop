"""System-wide hotkey via the Win32 RegisterHotKey API (no extra dependencies, no keyboard hooks).

Because the process receives the hotkey as input, Windows lets it bring its window to the
foreground, so the HUD can take keyboard focus straight away.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
NAMED_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
              "backquote": 0xC0, "`": 0xC0, "insert": 0x2D, "home": 0x24, "pause": 0x13}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+j' -> (modifiers, virtual-key code)."""
    mods, vk = 0, None
    for part in (p.strip().lower() for p in spec.split("+")):
        if part in MODS:
            mods |= MODS[part]
        elif part in NAMED_KEYS:
            vk = NAMED_KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        else:
            raise ValueError(f"Unknown key '{part}' in hotkey '{spec}'")
    if vk is None:
        raise ValueError(f"Hotkey '{spec}' has no main key")
    return mods, vk


class GlobalHotkey:
    def __init__(self, spec: str, callback: Callable[[], None]):
        self.spec = spec
        self.mods, self.vk = parse_hotkey(spec)
        self.callback = callback
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self.error: str | None = None

    def start(self) -> bool:
        threading.Thread(target=self._run, name="jev-hotkey", daemon=True).start()
        self._ready.wait(2)
        return self.error is None

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, self.mods | MOD_NOREPEAT, self.vk):
            self.error = f"'{self.spec}' is already used by another app (Windows error {ctypes.get_last_error()})"
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:
                        pass
        finally:
            user32.UnregisterHotKey(None, 1)
