"""Offline neural text-to-speech with Kokoro-82M (kokoro-onnx, CPU). Optional: needs
`pip install -r requirements-local-voice.txt` and the model files from
`python -m jev_assistant.voice.setup_local --kokoro`. Its voices are American/British English,
so it reads Hinglish words with an English accent."""

from __future__ import annotations

import asyncio
import threading
from typing import AsyncIterator

import numpy as np

from .setup_local import KOKORO_DIR, KOKORO_FILES

MODEL, VOICES = (KOKORO_DIR / name for name in KOKORO_FILES)


def available() -> bool:
    try:
        import kokoro_onnx  # noqa: F401
    except ImportError:
        return False
    return MODEL.exists() and VOICES.exists()


class KokoroTTS:
    name = "kokoro"
    rate = 24000

    def __init__(self, voice: str = "af_heart"):
        self.voice = voice
        self._kokoro = None
        self._lock = threading.Lock()

    def cache_key(self) -> str:
        return f"kokoro|{self.voice}"

    def _load(self):
        with self._lock:
            if self._kokoro is None:
                from kokoro_onnx import Kokoro
                self._kokoro = Kokoro(str(MODEL), str(VOICES))
            return self._kokoro

    def _speak(self, text: str) -> bytes:
        lang = "en-gb" if self.voice.startswith("b") else "en-us"  # voice names start with a/b + f/m
        audio, _ = self._load().create(text, voice=self.voice, lang=lang)
        return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        yield await asyncio.to_thread(self._speak, text)

    async def warm_up(self) -> None:
        await asyncio.to_thread(self._speak, "Ready.")  # loads the model and primes onnxruntime
