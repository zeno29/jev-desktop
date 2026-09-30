"""Sarvam AI speech-to-text (Saaras v3), built for Indian languages and code-mixed speech.

Measured: 0.4–0.8 s from end of speech to text, exact on Hinglish test commands. Uses the REST endpoint with the whole utterance once the user stops speaking: simpler and more
robust than the streaming WebSocket, at the cost of uploading ~100 KB after end of speech.
mode "translit" returns Roman-script text for Hindi ("gaane chalao"), which routing needs.
"""

from __future__ import annotations

import asyncio
from typing import Callable

import httpx

from .audio import wav_bytes
from .base import STT_RATE

URL = "https://api.sarvam.ai/speech-to-text"


class SarvamSTT:
    name = "sarvam"

    def __init__(self, api_key: str, model: str, mode: str, language: str):
        self.client = httpx.AsyncClient(headers={"api-subscription-key": api_key}, timeout=15)
        self.form = {"model": model, "mode": mode, "language_code": language}

    async def transcribe_pcm(self, pcm: bytes) -> str:
        files = {"file": ("speech.wav", wav_bytes(pcm, STT_RATE), "audio/wav")}
        r = await self.client.post(URL, data=self.form, files=files)
        if r.status_code != 200:
            raise RuntimeError(f"Sarvam STT HTTP {r.status_code}: {r.text[:200]}")
        return (r.json().get("transcript") or "").strip()

    def session(self, on_partial: Callable[[str], None] | None = None) -> "_SarvamSession":
        return _SarvamSession(self)

    async def warm_up(self) -> None:
        pass


class _SarvamSession:
    def __init__(self, stt: SarvamSTT):
        self.stt = stt
        self.audio = bytearray()
        # Open the TLS connection while the user speaks so the upload starts right away.
        self._warm = asyncio.create_task(stt.client.head(URL))

    def feed(self, pcm: bytes) -> None:
        self.audio += pcm

    async def finish(self) -> str:
        await asyncio.gather(self._warm, return_exceptions=True)
        return await self.stt.transcribe_pcm(bytes(self.audio))

    async def cancel(self) -> None:
        self._warm.cancel()
        self.audio.clear()
