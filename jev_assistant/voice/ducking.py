"""Lower every other app's volume while Jev is listening (and while it answers).

Music or a video playing through the speakers is the biggest thing that confuses end-of-speech
detection and speech recognition, and turning it down at the source works better than any filter
applied to the microphone afterwards. Each app's own mixer volume is lowered (what the Windows
volume mixer shows), so Spotify's and Chrome's in-app volume sliders are left alone.

The original volumes are written to disk before anything is changed, so if Jev dies mid-duck the
next start puts them back (`restore_leftover()`).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("jev.voice")

SKIP = {"audiodg.exe"}  # the Windows audio engine itself


class Ducker:
    def __init__(self, level: float, state_path: Path):
        self.level = max(0.0, min(1.0, level))  # 1.0 = don't duck
        self.state_path = state_path
        self._saved: dict[int, float] = {}       # pid -> original volume
        self._lock = threading.Lock()
        # Core Audio is COM: keep every call on one thread that has COM initialised.
        self._com = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-duck",
                                       initializer=_com_init)

    @property
    def enabled(self) -> bool:
        return self.level < 1.0

    def duck(self) -> None:
        if self.enabled:
            self._com.submit(self._duck)

    def restore(self) -> None:
        if self.enabled:
            self._com.submit(self._restore)

    def restore_leftover(self) -> None:
        """Undo a duck that a crash left behind (runs at startup)."""
        self._com.submit(self._restore_leftover)

    def close(self) -> None:
        self.restore()
        self._com.shutdown(wait=True)

    # ------------------------------------------------------------------ COM thread

    def _sessions(self):
        from pycaw.pycaw import AudioUtilities
        me = os.getpid()
        for s in AudioUtilities.GetAllSessions():
            if not s.ProcessId or s.ProcessId == me:
                continue
            try:
                name = s.Process.name().lower() if s.Process else ""
            except Exception:  # the process exited
                continue
            if name not in SKIP:
                yield s

    def _duck(self) -> None:
        with self._lock:
            try:
                for s in self._sessions():
                    vol = s.SimpleAudioVolume
                    current = vol.GetMasterVolume()
                    if s.ProcessId not in self._saved:  # already ducked: keep the true original
                        self._saved[s.ProcessId] = current
                    target = self._saved[s.ProcessId] * self.level
                    if current > target:
                        vol.SetMasterVolume(target, None)
                self._persist()
            except Exception:
                log.exception("ducking failed")

    def _restore(self) -> None:
        with self._lock:
            try:
                for s in self._sessions():
                    original = self._saved.get(s.ProcessId)
                    if original is not None:
                        s.SimpleAudioVolume.SetMasterVolume(original, None)
            except Exception:
                log.exception("restoring volumes failed")
            self._saved.clear()
            self.state_path.unlink(missing_ok=True)

    def _restore_leftover(self) -> None:
        try:
            saved = {int(k): v for k, v in json.loads(self.state_path.read_text(encoding="utf-8")).items()}
        except Exception:
            return
        with self._lock:
            self._saved = saved
        self._restore()
        log.info("restored volumes left ducked by a previous run")

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._saved), encoding="utf-8")


def _com_init() -> None:
    import comtypes
    comtypes.CoInitialize()
