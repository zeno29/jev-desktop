"""The voice loop: listen (chime → mic → end-of-speech → transcript) and speak (cache → TTS →
player, sentence by sentence, stoppable at any moment).

Front ends see progress through one callback, `on_event(kind, text)`:
  listening · partial (live transcript) · transcribing · speaking (the sentence being said) ·
  idle · error
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator, Callable

from ..config import DATA_DIR, VoiceConfig
from .audio import FRAME, PLAY_RATE, Player, earcon, loudness, mic_source, read_wav, wav_bytes
from .base import STT_RATE, STTProvider, TTSProvider, transcribe
from .ducking import Ducker
from .providers import Chain, build_stt, build_tts
from .vad import Endpointer, NoiseGate, make_vad

log = logging.getLogger("jev.voice")

CACHE_DIR = DATA_DIR / "tts_cache"
STATE_FILE = DATA_DIR / "voice_state.json"   # providers sitting out a spent quota
CACHE_MAX_CHARS = 80      # only short, likely-to-repeat sentences ("Opening Spotify.")
CACHE_MAX_FILES = 400
SYNTH_AHEAD = 2           # segments synthesized in parallel
SEGMENT_MAX_CHARS = 260   # later sentences are grouped: fewer requests (Gemini's free TTS tier is tiny)
FIRST_AUDIO_TIMEOUT = 6.0 # a voice that hasn't started by then counts as failed (next one takes over)
PRE_ROLL = 0.3            # seconds kept before detected speech when re-sending audio to a fallback
DUCK_STATE_FILE = DATA_DIR / "duck_state.json"  # other apps' volumes while ducked (crash recovery)
UNDUCK_AFTER = 15.0       # seconds after listening to wait for a reply before other apps get their volume back

EventFn = Callable[[str, str], None]


class TTSCache:
    def __init__(self, directory: Path = CACHE_DIR):
        self.dir = directory

    def _path(self, key: str, text: str) -> Path:
        return self.dir / (hashlib.sha1(f"{key}\n{text.strip().lower()}".encode()).hexdigest()[:20] + ".wav")

    def get(self, key: str, text: str) -> bytes | None:
        path = self._path(key, text)
        if not path.exists():
            return None
        try:
            pcm = read_wav(path, PLAY_RATE)
            path.touch()  # most recently used survives pruning
            return pcm
        except Exception:
            return None

    def put(self, key: str, text: str, pcm: bytes, rate: int) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(key, text).write_bytes(wav_bytes(pcm, rate))
        files = sorted(self.dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        for old in files[:max(0, len(files) - CACHE_MAX_FILES)]:
            old.unlink(missing_ok=True)


def speech_text(text: str) -> str:
    """What should be read aloud: no markdown, no raw URLs."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)                  # [label](url) -> label
    text = re.sub(r"https?://(?:www\.)?([^/\s]+)\S*", r"\1", text)          # url -> domain
    text = re.sub(r"(?m)^\s*(?:#+|[-*•]|\d+[.)])\s+", "", text)             # headings, bullets
    text = re.sub(r"[*_`#>|]+", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def segments(text: str) -> list[str]:
    """Sentence groups to synthesize one at a time: the first sentence alone (audio starts as
    soon as it's ready), then groups of up to SEGMENT_MAX_CHARS."""
    lines = [ln.strip() for ln in speech_text(text).splitlines() if ln.strip()]
    lines = [ln if ln[-1] in ".!?…:;," else ln + "." for ln in lines]  # list items become sentences
    sentences = [s for ln in lines for s in re.split(r"(?<=[.!?…])\s+", ln) if s]
    pieces: list[str] = []
    for s in sentences:  # split run-on sentences at commas so no single request is huge
        while len(s) > SEGMENT_MAX_CHARS:
            cut = max(s.rfind(", ", 0, SEGMENT_MAX_CHARS), s.rfind("; ", 0, SEGMENT_MAX_CHARS))
            cut = cut + 1 if cut > 40 else s.rfind(" ", 0, SEGMENT_MAX_CHARS)
            pieces.append(s[:cut].strip())
            s = s[cut:].strip()
        pieces.append(s)
    out: list[str] = []
    for p in pieces:
        if len(out) >= 2 and len(out[-1]) + 1 + len(p) <= SEGMENT_MAX_CHARS:
            out[-1] += " " + p
        else:
            out.append(p)
    return out


class VoiceController:
    def __init__(self, cfg: VoiceConfig, vocabulary: list[str], on_event: EventFn | None = None):
        self.cfg = cfg
        self.on_event: EventFn = on_event or (lambda kind, text: None)
        self.stt: Chain[STTProvider] = Chain(build_stt(cfg, vocabulary), "stt", STATE_FILE)
        self.tts: Chain[TTSProvider] = Chain(build_tts(cfg), "tts", STATE_FILE)
        self.player = Player(cfg.output_device)
        self.cache = TTSCache() if cfg.cache else None
        self.vad = None
        self.state = "idle"
        self._speech: asyncio.Queue[str] = asyncio.Queue()
        self._speaker: asyncio.Task | None = None
        self.mic_level = 0.0  # loudness of the last microphone frame while listening
        self.ducker = Ducker(cfg.duck_level, DUCK_STATE_FILE)
        self.ducker.restore_leftover()
        self._unduck_timer: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------ status

    @property
    def can_listen(self) -> bool:
        return bool(self.stt.providers)

    @property
    def level(self) -> float | None:
        """0..1: your voice while listening, Jev's while speaking; None otherwise (for the orb and
        the console's level bars)."""
        if self.state == "listening":
            return self.mic_level
        if self.state == "speaking":
            return self.player.level
        return None

    def describe(self) -> dict:
        return {"stt": self.stt.names, "tts": self.tts.names,
                "vad": getattr(self.vad, "name", "not loaded")}

    def _emit(self, kind: str, text: str = "") -> None:
        if kind in ("listening", "transcribing", "speaking", "idle"):
            self.state = kind
        try:
            self.on_event(kind, text)
        except Exception:
            log.exception("voice event handler failed")

    async def warm_up(self) -> None:
        """Load the VAD and the first-choice providers in the background so the first command
        isn't slower than the rest. Heavy fallbacks load when first needed; the Windows voice is
        cheap and is what runs when a cloud voice is out of quota, so it's always warmed."""
        self.vad = await asyncio.to_thread(make_vad)
        warm = self.stt.providers[:1] + self.tts.providers[:1]
        warm += [p for p in self.tts.providers[1:] if p.name == "windows"]
        for p in warm:
            try:
                await p.warm_up()
            except Exception:
                log.exception("%s warm-up failed", p.name)

    # ------------------------------------------------------------------ listening

    async def listen(self, source=None) -> str | None:
        """Record one utterance and return its transcript (None: nothing said, or it failed).
        Cancelling the task (hotkey pressed again) stops everything."""
        self.stop_speaking()
        if not self.stt.providers:
            self._emit("error", "No speech-to-text provider is available.")
            return None
        if self.vad is None:
            self.vad = await asyncio.to_thread(make_vad)
        primary = self.stt.ready()[0]
        session = primary.session(on_partial=lambda text: self._emit("partial", text))
        self._duck()  # first, so the background level is measured with the music already down
        self._emit("listening")
        self._chime("listen")
        endpoint = Endpointer(self.vad, self.cfg.vad_silence_ms, self.cfg.max_seconds, self.cfg.no_speech_seconds)
        gate = NoiseGate(endpoint) if self.cfg.noise_gate else (lambda pcm: pcm)
        # Until the user starts talking, only a short lead-in is kept; the recogniser never hears
        # the background before their first word (it would transcribe a TV or lyrics as the command).
        lead_in: deque[bytes] = deque(maxlen=max(1, round(PRE_ROLL * STT_RATE / FRAME)))
        audio = bytearray()
        result = None
        try:
            async with contextlib.aclosing((source or mic_source(self.cfg.input_device)).frames()) as frames:
                async for frame in frames:
                    self.mic_level = loudness(frame)
                    result = endpoint.push(frame)
                    frame = gate(frame)  # background between words is turned down before recognition
                    audio += frame
                    if not endpoint.speaking:
                        lead_in.append(frame)
                    else:
                        while lead_in:
                            session.feed(lead_in.popleft())
                        session.feed(frame)
                    if result in ("end", "max", "no_speech"):
                        break
        except BaseException as e:  # cancelled (hotkey pressed again) or the microphone failed
            await session.cancel()
            self._unduck()
            self._chime("cancel" if isinstance(e, asyncio.CancelledError) else "error")
            self._emit("idle")
            raise
        if result == "no_speech" or not endpoint.speaking:
            await session.cancel()
            self._unduck()
            self._chime("cancel")
            self._emit("idle")
            return None
        self._emit("transcribing")
        t0 = time.perf_counter()
        start = max(0, endpoint.speech_start * FRAME - round(PRE_ROLL * STT_RATE)) * 2
        text, used = await self._finish(primary, session, bytes(audio[start:]))
        log.info("heard %r via %s in %.0f ms after end of speech (background level %.4f)",
                 text, used, (time.perf_counter() - t0) * 1000, endpoint.background)
        # stay ducked while the command runs and the reply is spoken; don't wait forever for it
        self._unduck(after=UNDUCK_AFTER if text else 0)
        self._emit("idle")
        return text or None

    def release(self) -> None:
        """The command finished without anything to say: give other apps their volume back now."""
        if self.state not in ("listening", "speaking"):
            self._unduck()

    def _duck(self) -> None:
        if self._unduck_timer:
            self._unduck_timer.cancel()
            self._unduck_timer = None
        self.ducker.duck()

    def _unduck(self, after: float = 0) -> None:
        if self._unduck_timer:
            self._unduck_timer.cancel()
            self._unduck_timer = None
        if after > 0:
            self._unduck_timer = asyncio.get_running_loop().call_later(after, self.ducker.restore)
        else:
            self.ducker.restore()

    async def _finish(self, primary: STTProvider, session, audio: bytes) -> tuple[str, str]:
        try:
            return await session.finish(), primary.name
        except asyncio.CancelledError:
            await session.cancel()
            raise
        except Exception as e:
            self.stt.failed(primary, e)
        for provider in self.stt.ready():  # replay the recording through the next provider
            if provider is primary:
                continue
            try:
                return await transcribe(provider, audio), provider.name
            except Exception as e:
                self.stt.failed(provider, e)
        self._chime("error")
        self._emit("error", "Speech recognition failed.")
        return "", "none"

    def _chime(self, kind: str) -> None:
        if self.cfg.chime:
            self.player.feed(earcon(kind))

    # ------------------------------------------------------------------ speaking

    def say(self, text: str) -> None:
        """Queue a reply to be spoken (returns at once). Ignored while the user is talking."""
        if self.state in ("listening", "transcribing"):
            return
        for seg in segments(text):
            self._speech.put_nowait(seg)
        if self._speaker is None or self._speaker.done():
            if self._speech.empty():
                self._emit("idle")  # nothing speakable (e.g. only a URL's punctuation)
                self._unduck()
                return
            self._duck()  # keep music down under Jev's voice
            self._speaker = asyncio.create_task(self._speak_queue())

    async def speak(self, text: str) -> None:
        """Say `text` and wait until it has been heard (or stopped)."""
        self.say(text)
        if self._speaker:
            with contextlib.suppress(asyncio.CancelledError):
                await self._speaker

    def stop_speaking(self) -> None:
        """Barge-in: silence now and drop anything still queued."""
        if self._speaker and not self._speaker.done():
            self._speaker.cancel()
        self._speaker = None
        while not self._speech.empty():
            self._speech.get_nowait()
        self.player.stop()

    async def _speak_queue(self) -> None:
        try:
            while True:
                await self._speak_batch()
                await self.player.drain()
                if self._speech.empty():  # nothing was queued while the last sentence finished
                    break
        finally:
            if self.state != "listening":  # barge-in: listening keeps the music down
                self._unduck()
        self._emit("idle")

    async def _speak_batch(self) -> None:
        # Up to SYNTH_AHEAD segments synthesize at once, so while the first sentence plays the
        # next is already being made (cloud TTS takes seconds to start; sequential synthesis left
        # multi-second gaps between sentences).
        playlist: list[tuple[str, asyncio.Queue]] = []
        tasks: list[asyncio.Task] = []
        slots = asyncio.Semaphore(SYNTH_AHEAD)

        async def produce(text: str, chunks: asyncio.Queue) -> None:
            try:
                async with slots:
                    async for chunk in self._synthesize(text):
                        chunks.put_nowait(chunk)
            finally:
                chunks.put_nowait(None)

        while not self._speech.empty():
            text, chunks = self._speech.get_nowait(), asyncio.Queue()
            playlist.append((text, chunks))
            tasks.append(asyncio.create_task(produce(text, chunks)))
        try:
            for text, chunks in playlist:
                heard = False
                while (chunk := await chunks.get()) is not None:
                    if not heard:  # the caption follows the audio: let the last sentence finish
                        while self.player.busy:
                            await asyncio.sleep(0.02)
                        self._emit("speaking", text)
                        heard = True
                    self.player.feed(*chunk)
                if not heard:  # every provider failed: show the words long enough to read
                    self._emit("speaking", text)
                    await asyncio.sleep(min(8.0, 1.0 + len(text) / 15))
        finally:
            for t in tasks:
                t.cancel()

    async def _synthesize(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        # short and without numbers: "Opening Spotify." repeats, "It's 3:36 PM." doesn't
        cacheable = self.cache is not None and len(text) <= CACHE_MAX_CHARS and not re.search(r"\d", text)
        if cacheable:  # any provider's cached copy beats waiting for synthesis
            for p in self.tts.providers:
                pcm = self.cache.get(p.cache_key(), text)
                if pcm is not None:
                    yield pcm, PLAY_RATE
                    return
        for provider in self.tts.ready():
            got: list[bytes] = []
            try:
                async with contextlib.aclosing(provider.synthesize(text)) as stream:
                    async with asyncio.timeout(FIRST_AUDIO_TIMEOUT):
                        first = await anext(stream, None)
                    if first is None:
                        raise RuntimeError("no audio returned")
                    got.append(first)
                    yield first, provider.rate
                    async for pcm in stream:
                        got.append(pcm)
                        yield pcm, provider.rate
            except asyncio.CancelledError:
                raise
            except Exception as e:  # includes TimeoutError: no audio in time
                self.tts.failed(provider, e)
                if got:
                    return  # half a sentence already played; don't repeat it in another voice
                continue
            if cacheable:
                self.cache.put(provider.cache_key(), text, b"".join(got), provider.rate)
            return
        log.error("every text-to-speech provider failed for %r", text)

    async def close(self) -> None:
        self.stop_speaking()
        self.player.close()
        if self._unduck_timer:
            self._unduck_timer.cancel()
        await asyncio.to_thread(self.ducker.close)  # never leave other apps turned down
