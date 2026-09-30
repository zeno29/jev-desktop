"""Microphone capture, an interruptible PCM player, and the earcons.

Capture is 16 kHz mono int16 in 512-sample frames (32 ms), the size the Silero VAD expects.
Anything with the same `frames()` async iterator can stand in for the microphone: `WavSource`
feeds a recording in real time, which is how the voice path is tested without speaking.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import math
import os
import threading
import wave
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator, Protocol

import numpy as np
import sounddevice as sd

from .base import STT_RATE

FRAME = 512                 # samples per capture frame
PLAY_RATE = 24000           # the player's rate: what Gemini, Sarvam and Kokoro produce
PLAYER_IDLE_CLOSE = 8.0     # seconds; keeps Bluetooth headsets from clipping back-to-back sounds


class AudioSource(Protocol):
    def frames(self) -> AsyncIterator[bytes]: ...


def find_device(name: str, kind: str) -> int | None:
    """Index of the first device whose name contains `name` (None = Windows default). Prefers the
    default host API (MME), which resamples to whatever rate we ask for."""
    if not name:
        return None
    channels = "max_input_channels" if kind == "input" else "max_output_channels"
    default_api = sd.default.hostapi
    matches = [(d["hostapi"] != default_api, i) for i, d in enumerate(sd.query_devices())
               if name.lower() in d["name"].lower() and d[channels] > 0]
    return min(matches)[1] if matches else None


def loudness(pcm: bytes) -> float:
    """0..1 on a decibel scale (-55 dBFS -> 0, -12 dBFS -> 1): what the orb and level bars show."""
    if not pcm:
        return 0.0
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    rms = float(np.sqrt(np.mean(x * x))) + 1e-9
    return min(1.0, max(0.0, (20 * math.log10(rms) + 55) / 43))


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    if src == dst or not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = max(1, round(len(x) * dst / src))
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return y.astype(np.int16).tobytes()


def read_wav(path: str | Path, rate: int = STT_RATE) -> bytes:
    """Mono int16 PCM at `rate` from a 16-bit WAV file."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit WAV is supported")
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.int16)
        return resample(x.tobytes(), w.getframerate(), rate)


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class Microphone:
    def __init__(self, device: str = ""):
        self.device = find_device(device, "input")

    async def frames(self) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def callback(indata, frames, time_info, status) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

        stream = sd.RawInputStream(samplerate=STT_RATE, blocksize=FRAME, channels=1, dtype="int16",
                                   device=self.device, callback=callback)
        await asyncio.to_thread(stream.start)  # opening a device can take ~100 ms; don't stall the loop
        try:
            while True:
                yield await queue.get()
        finally:
            await asyncio.to_thread(lambda: (stream.stop(), stream.close()))


class PcmSource:
    """Plays recorded 16 kHz PCM into the voice path as if it were the microphone: real-time
    pacing, a little silence first, then silence for as long as the listener keeps reading."""

    def __init__(self, pcm: bytes, lead_seconds: float = 0.4):
        self.pcm = bytes(round(lead_seconds * STT_RATE) * 2) + pcm

    async def frames(self) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        start, step, i = loop.time(), FRAME * 2, 0
        while True:
            chunk = self.pcm[i * step:(i + 1) * step]
            yield chunk + bytes(step - len(chunk))
            i += 1
            await asyncio.sleep(max(0.0, start + i * FRAME / STT_RATE - loop.time()))


class WavSource(PcmSource):
    def __init__(self, path: str | Path, lead_seconds: float = 0.4):
        super().__init__(read_wav(path), lead_seconds)


_fake_turns = itertools.count()


def mic_source(device: str = "") -> AudioSource:
    """The microphone, or a WAV file when JEV_FAKE_MIC is set (test hook: lets the full hotkey →
    reply path run without anyone speaking). Several files separated by ';' are used in turn."""
    fake = [f for f in os.environ.get("JEV_FAKE_MIC", "").split(";") if f]
    if fake:
        return WavSource(fake[next(_fake_turns) % len(fake)])
    return Microphone(device)


class Player:
    """Streams int16 PCM to the speakers. `feed` can be called while audio is playing (sentence
    by sentence); `stop` silences it immediately (barge-in). The device stays open for a few
    seconds after the last sound, because reopening costs time and Bluetooth headsets swallow
    the first few hundred ms after a device opens."""

    def __init__(self, device: str = "", rate: int = PLAY_RATE):
        self.device = find_device(device, "output")
        self.rate = rate
        self.level = 0.0  # loudness of what's playing right now (orb animation)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stream: sd.RawOutputStream | None = None
        self._close_timer: asyncio.TimerHandle | None = None

    def _callback(self, outdata, frames, time_info, status) -> None:
        n = len(outdata)
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
        outdata[:len(chunk)] = chunk
        if len(chunk) < n:
            outdata[len(chunk):] = bytes(n - len(chunk))
        self.level = loudness(chunk)

    def feed(self, pcm: bytes, rate: int | None = None) -> None:
        """Queue audio (call from the event loop). Opens the device if needed and pushes its
        closing back to PLAYER_IDLE_CLOSE after the last queued sound."""
        if self._stream is None:
            self._stream = sd.RawOutputStream(samplerate=self.rate, channels=1, dtype="int16",
                                              device=self.device, callback=self._callback, latency="low")
            self._stream.start()
        pcm = resample(pcm, rate or self.rate, self.rate)
        with self._lock:
            self._buf += pcm
            queued = len(self._buf) / 2 / self.rate
        if self._close_timer:
            self._close_timer.cancel()
        self._close_timer = asyncio.get_running_loop().call_later(queued + PLAYER_IDLE_CLOSE, self.close)

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._buf)

    async def drain(self) -> None:
        """Wait until everything fed so far has been heard."""
        while self.busy:
            await asyncio.sleep(0.02)
        if self._stream is not None:
            await asyncio.sleep(self._stream.latency)

    def stop(self) -> None:
        with self._lock:
            self._buf.clear()

    def close(self) -> None:
        self.stop()
        if self._close_timer:
            self._close_timer.cancel()
            self._close_timer = None
        if self._stream is not None:
            stream, self._stream = self._stream, None
            stream.stop()
            stream.close()


@lru_cache(maxsize=None)
def earcon(kind: str) -> bytes:
    """Short generated sounds at PLAY_RATE: 'listen' (rising), 'cancel' (falling), 'error' (low)."""
    notes = {"listen": [(880, .065), (1320, .09)], "cancel": [(1320, .06), (880, .09)],
             "error": [(330, .16)]}[kind]
    out = []
    for freq, dur in notes:
        t = np.arange(round(dur * PLAY_RATE)) / PLAY_RATE
        env = np.minimum(1, np.minimum(t / .008, (dur - t) / .03))  # soft attack, softer release
        tone = np.sin(2 * math.pi * freq * t) + 0.25 * np.sin(4 * math.pi * freq * t)
        out.append(0.16 * env * tone)
    return (np.concatenate(out) * 32767).astype(np.int16).tobytes()
