"""Offline speech-to-text with faster-whisper (CPU, int8).

Best Hinglish option: Oriserve/Whisper-Hindi2Hinglish-Swift (a Whisper-base fine-tune that writes
Hindi in Roman script), converted once by `python -m jev_assistant.voice.setup_local` into
data/models/whisper-hinglish-ct2. Without it, stock Whisper "base" is used. Larger sizes are more
accurate but too slow on a laptop CPU ("small" took ~4 s per command on an i5-1240P).

Whisper isn't a streaming model, so audio is collected while the user speaks and decoded at the
end; the model is loaded in the background at startup so the first command doesn't pay for it.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import DATA_DIR

HINGLISH_MODEL_DIR = DATA_DIR / "models" / "whisper-hinglish-ct2"


def resolve_model(setting: str) -> str:
    if setting in ("", "auto"):
        return str(HINGLISH_MODEL_DIR) if (HINGLISH_MODEL_DIR / "model.bin").exists() else "base"
    return setting


class LocalSTT:
    name = "local"

    def __init__(self, model: str, names: list[str]):
        self.model_name = resolve_model(model)
        # The prompt biases spelling toward names Whisper would otherwise mangle ("Arijit Singh").
        # Only a few well-known ones: with app names like "Calculator" in the list, the model
        # started echoing them ("Kal subah..." came back as "Calculator,").
        self.prompt = ("Jev commands, like open Spotify or play on YouTube. Names: " + ", ".join(names) + "."
                       if names else None)
        self._model = None
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return "Hinglish (Oriserve, base)" if Path(self.model_name) == HINGLISH_MODEL_DIR else f"Whisper {self.model_name}"

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                threads = min(8, os.cpu_count() or 4)
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8", cpu_threads=threads)
            return self._model

    async def warm_up(self) -> None:
        await asyncio.to_thread(self._load)

    def transcribe_pcm(self, pcm: bytes) -> str:
        model = self._load()
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
        # "en" keeps Hinglish in Roman script (with "hi" Whisper writes Devanagari); the
        # Hinglish fine-tune was trained with the same setting.
        segments, _ = model.transcribe(audio, language="en", task="transcribe", beam_size=1,
                                       condition_on_previous_text=False, without_timestamps=True,
                                       initial_prompt=self.prompt, vad_filter=False)
        return " ".join(s.text.strip() for s in segments).strip()

    def session(self, on_partial: Callable[[str], None] | None = None) -> "_LocalSession":
        return _LocalSession(self)


class _LocalSession:
    def __init__(self, stt: LocalSTT):
        self.stt = stt
        self.audio = bytearray()

    def feed(self, pcm: bytes) -> None:
        self.audio += pcm

    async def finish(self) -> str:
        return await asyncio.to_thread(self.stt.transcribe_pcm, bytes(self.audio))

    async def cancel(self) -> None:
        self.audio.clear()
