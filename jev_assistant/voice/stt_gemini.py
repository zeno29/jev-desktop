"""Gemini Live transcription (gemini-3.5-transcribe-live).

The session connects the moment listening starts (~0.5 s) while the microphone is already
capturing; audio is streamed as it arrives, so after the user stops talking only the final
recognition remains (measured 0.4–0.5 s). Activity detection is ours (Silero), not the server's:
we send activity_start / activity_end ourselves.

language_codes=["en-IN"] is what makes Hinglish come back in Roman script ("Spotify pe gaane
chalao"); other settings return Devanagari, which the router can't use.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Callable

from google import genai
from google.genai import types

from .base import STT_RATE

log = logging.getLogger("jev.voice")

CHUNK_BYTES = STT_RATE * 2 // 10   # send ~100 ms per message, as the Live API recommends
FINAL_TIMEOUT = 8.0


class GeminiSTT:
    name = "gemini"

    def __init__(self, api_key: str, model: str, language: str, vocabulary: list[str]):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.config = types.LiveConnectConfig(
            response_modalities=["TEXT"],
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)),
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=[language], custom_vocabulary=vocabulary[:100] or None))

    def session(self, on_partial: Callable[[str], None] | None = None) -> "_GeminiSession":
        return _GeminiSession(self, on_partial)

    async def warm_up(self) -> None:
        pass  # a Live session can't be kept open between utterances cheaply; connect overlaps speech instead


class _GeminiSession:
    def __init__(self, stt: GeminiSTT, on_partial: Callable[[str], None] | None):
        self.stt = stt
        self.on_partial = on_partial
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.finals: list[str] = []
        self.task = asyncio.create_task(self._run())

    def feed(self, pcm: bytes) -> None:
        self.queue.put_nowait(pcm)

    async def finish(self) -> str:
        self.queue.put_nowait(None)
        return await asyncio.wait_for(self.task, FINAL_TIMEOUT)

    async def cancel(self) -> None:
        self.task.cancel()
        with contextlib.suppress(BaseException):
            await self.task

    async def _run(self) -> str:
        async with self.stt.client.aio.live.connect(model=self.stt.model, config=self.stt.config) as s:
            receiver = asyncio.create_task(self._receive(s))
            try:
                await s.send_realtime_input(activity_start=types.ActivityStart())
                await self._send_audio(s)
                await s.send_realtime_input(activity_end=types.ActivityEnd())
                await receiver
            finally:
                receiver.cancel()
        return _join(self.finals)

    async def _send_audio(self, s) -> None:
        buf, done = bytearray(), False
        while not done:
            item = await self.queue.get()
            while True:  # coalesce whatever piled up while connecting
                if item is None:
                    done = True
                    break
                buf += item
                if self.queue.empty():
                    break
                item = self.queue.get_nowait()
            if buf and (done or len(buf) >= CHUNK_BYTES):
                await s.send_realtime_input(audio=types.Blob(data=bytes(buf), mime_type=f"audio/pcm;rate={STT_RATE}"))
                buf.clear()

    async def _receive(self, s) -> None:
        while True:
            got_any = False
            async for msg in s.receive():  # yields one turn, then returns
                got_any = True
                sc = msg.server_content
                if sc and sc.input_transcription and sc.input_transcription.text:
                    self.finals.append(sc.input_transcription.text)  # final segments add up
                    if self.on_partial:
                        self.on_partial(_join(self.finals))
                elif sc and sc.interim_input_transcription and sc.interim_input_transcription.text and self.on_partial:
                    self.on_partial(_join(self.finals + [sc.interim_input_transcription.text]))
                # Our activity_end is answered, after the last transcript, with an ACTIVITY_END event.
                va = msg.voice_activity
                if va and va.voice_activity_type == types.VoiceActivityType.ACTIVITY_END or sc and sc.turn_complete:
                    return
            if not got_any:
                return  # connection closed


def _join(parts: list[str]) -> str:
    return " ".join(p.strip() for p in parts if p.strip())
