"""Offline text-to-speech with the voices built into Windows (SAPI, via comtypes).

SAPI renders into a memory stream as 24 kHz PCM (~80 ms per sentence), so the audio goes through
the same player as every other provider and can be cut off mid-word. The voices are basic and
usually US English only; this is the always-available last resort.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterator

SAFT_24K_16BIT_MONO = 26  # SpeechAudioFormatType


class WindowsTTS:
    name = "windows"
    rate = 24000

    def __init__(self, voice: str = ""):
        self.voice_hint = voice
        # SAPI objects live in a COM apartment: create and use them on one thread only.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-sapi")
        self._voice = None
        self.voice_name = ""

    def cache_key(self) -> str:
        return f"windows|{self.voice_hint}"

    def _init(self) -> None:
        if self._voice is not None:
            return
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        for token in voice.GetVoices():
            if self.voice_hint and self.voice_hint.lower() in token.GetDescription().lower():
                voice.Voice = token
                break
        self.voice_name = voice.Voice.GetDescription()
        self._voice = voice

    def _speak(self, text: str) -> bytes:
        import comtypes.client
        self._init()
        stream = comtypes.client.CreateObject("SAPI.SpMemoryStream")
        fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
        fmt.Type = SAFT_24K_16BIT_MONO
        stream.Format = fmt
        self._voice.AudioOutputStream = stream
        self._voice.Speak(text, 0)
        return bytes(stream.GetData())

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        yield await asyncio.get_running_loop().run_in_executor(self._pool, self._speak, text)

    async def warm_up(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._pool, self._init)  # ~0.9 s the first time
