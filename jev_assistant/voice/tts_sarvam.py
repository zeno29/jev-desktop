"""Sarvam AI text-to-speech (Bulbul v3): natural Indian-English and Hindi voices, Hinglish included.

REST: the whole clip comes back at once (measured ~0.6 s for a short reply). Replies are spoken
sentence by sentence, so the first sentence's clip is what the user waits for.
"""

from __future__ import annotations

import base64
import io
import wave
from typing import AsyncIterator

import httpx

from .audio import resample

URL = "https://api.sarvam.ai/text-to-speech"


class SarvamTTS:
    name = "sarvam"
    rate = 24000

    def __init__(self, api_key: str, model: str, speaker: str, language: str):
        self.client = httpx.AsyncClient(headers={"api-subscription-key": api_key}, timeout=20)
        self.model, self.speaker, self.language = model, speaker, language

    def cache_key(self) -> str:
        return f"sarvam|{self.model}|{self.speaker}|{self.language}"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        r = await self.client.post(URL, json={
            "text": text, "language_code": self.language, "model": self.model, "speaker": self.speaker,
            "speech_sample_rate": self.rate, "output_audio_codec": "wav"})
        if r.status_code != 200:
            raise RuntimeError(f"Sarvam TTS HTTP {r.status_code}: {r.text[:200]}")
        for audio in r.json().get("audios") or []:
            with wave.open(io.BytesIO(base64.b64decode(audio))) as w:
                if w.getsampwidth() != 2 or w.getnchannels() != 1:
                    raise RuntimeError("Sarvam TTS returned audio that isn't 16-bit mono")
                yield resample(w.readframes(w.getnframes()), w.getframerate(), self.rate)

    async def warm_up(self) -> None:
        pass
