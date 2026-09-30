"""Gemini text-to-speech through the Interactions API, streamed: raw 24 kHz PCM chunks arrive as
they're generated (then faster than real time). First audio takes 1.5–3.5 s, so the controller
caches short replies and speaks long ones sentence by sentence. The free tier allows 10 requests
per day per model; the rate-limit error arrives as an `error` event in the stream."""

from __future__ import annotations

import base64
from typing import AsyncIterator

from google import genai


class GeminiTTS:
    name = "gemini"
    rate = 24000

    def __init__(self, api_key: str, model: str, voice: str, language: str, style: str = ""):
        self.client = genai.Client(api_key=api_key)
        self.model, self.voice, self.language, self.style = model, voice, language, style

    def cache_key(self) -> str:
        return f"gemini|{self.model}|{self.voice}|{self.language}|{self.style}"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        part: dict = {"type": "text", "text": text}
        if self.style:
            part["annotations"] = [{"type": "speech_metadata", "style": self.style}]
        stream = await self.client.aio.interactions.create(
            model=self.model,
            input=[{"type": "user_input", "content": [part]}],
            response_format={"type": "audio"},
            generation_config={"speech_config": [{"voice": self.voice, "language": self.language}]},
            stream=True)
        async for event in stream:
            if event.event_type == "step.delta" and getattr(event.delta, "type", None) == "audio":
                yield base64.b64decode(event.delta.data)
            elif event.event_type == "error":  # e.g. the free tier's daily request cap
                err = event.error
                raise RuntimeError(f"Gemini TTS: {getattr(err, 'code', '')}: {getattr(err, 'message', err)}")

    async def warm_up(self) -> None:
        pass  # no warm-up request: on the free tier it would spend one of the 10 requests a day
