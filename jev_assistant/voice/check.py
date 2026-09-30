"""`python -m jev_assistant --voice-check`: is voice set up right on this PC?

Lists audio devices, runs a 3-second microphone level test, shows which providers are usable
(and why not), then measures each one: speech-to-text on a test phrase spoken by the Windows
voice (streamed in real time, like a microphone), and text-to-speech time to first audio.
Cloud providers get one short request each.
"""

from __future__ import annotations

import os
import time

import numpy as np
import sounddevice as sd

from ..config import VoiceConfig
from .audio import FRAME, Microphone, PcmSource, Player, earcon, find_device, resample
from .base import STT_RATE
from .providers import STT_ORDER, TTS_ORDER, build_stt, build_tts, unavailable_reason
from .vad import Endpointer, make_vad

TEST_PHRASE = "Open Spotify and play some music."


def _devices(cfg: VoiceConfig) -> None:
    api = sd.query_hostapis(sd.default.hostapi)["name"]
    for kind, setting in (("input", cfg.input_device), ("output", cfg.output_device)):
        idx = find_device(setting, kind)
        if idx is None:
            idx = sd.default.device[0 if kind == "input" else 1]
        print(f"  {kind:6} → {sd.query_devices(idx)['name']}" + (f"   ([voice] {kind}_device = {setting!r})" if setting else ""))
    channels = {"input": "max_input_channels", "output": "max_output_channels"}
    for kind in ("input", "output"):
        names = [d["name"] for d in sd.query_devices() if d["hostapi"] == sd.default.hostapi and d[channels[kind]] > 0]
        print(f"  all {kind}s ({api}): " + "; ".join(names))


async def _mic_test(cfg: VoiceConfig, seconds: float = 3.0) -> bytes:
    vad = make_vad()
    vad.reset()
    print(f"\nMicrophone test: say something for {seconds:.0f} seconds (VAD: {vad.name})...")
    audio, peak, speech = bytearray(), 0.0, 0
    frames = int(seconds * STT_RATE / FRAME)
    async for pcm in Microphone(cfg.input_device).frames():
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
        rms = float(np.sqrt(np.mean(x ** 2))) + 1e-9
        peak = max(peak, rms)
        speech += vad(x) >= 0.5
        audio += pcm
        if len(audio) % (FRAME * 2 * 4) == 0:  # redraw the meter ~8 times a second
            bar = "#" * min(40, int(max(0.0, 60 + 20 * np.log10(rms)) * 0.66))
            print(f"\r  level {20 * np.log10(rms):6.1f} dBFS |{bar:<40}|", end="", flush=True)
        if len(audio) >= frames * FRAME * 2:
            break
    print(f"\r  loudest {20 * np.log10(peak):.1f} dBFS, speech in {speech * FRAME / STT_RATE:.1f} s of {seconds:.0f} s"
          + ("  ← no sound: check the input device / Windows privacy settings" if peak < 0.002 else "") + " " * 20)
    return bytes(audio)


async def _stt_latency(cfg: VoiceConfig, clip: bytes) -> None:
    print(f"\nSpeech-to-text on {TEST_PHRASE!r} (spoken by the Windows voice, streamed in real time):")
    for provider in build_stt(cfg, ["Spotify"]):
        try:
            await provider.warm_up()
            session, endpoint = provider.session(), Endpointer(make_vad(), cfg.vad_silence_ms, 15, 5)
            async for pcm in PcmSource(clip).frames():
                session.feed(pcm)
                if endpoint.push(pcm) in ("end", "max", "no_speech"):
                    break
            t0 = time.perf_counter()
            text = await session.finish()
            print(f"  {provider.name:8} {1000 * (time.perf_counter() - t0):5.0f} ms after end of speech  → {text!r}")
        except Exception as e:
            print(f"  {provider.name:8} FAILED: {type(e).__name__}: {str(e)[:160]}")


async def _tts_latency(cfg: VoiceConfig) -> None:
    print("\nText-to-speech, time to first audio for 'Opening Spotify.':")
    for provider in build_tts(cfg):
        try:
            await provider.warm_up()
            t0, first = time.perf_counter(), None
            async for _ in provider.synthesize("Opening Spotify."):
                first = first or time.perf_counter() - t0
            print(f"  {provider.name:8} {1000 * first:5.0f} ms" if first else f"  {provider.name:8} no audio")
        except Exception as e:
            print(f"  {provider.name:8} FAILED: {type(e).__name__}: {str(e)[:160]}")


async def _test_clip(cfg: VoiceConfig) -> bytes | None:
    if unavailable_reason("tts", "windows"):
        return None
    from .tts_windows import WindowsTTS
    tts = WindowsTTS(cfg.windows_voice)
    pcm = b"".join([c async for c in tts.synthesize(TEST_PHRASE)])
    return resample(pcm, tts.rate, STT_RATE)


async def voice_check(cfg: VoiceConfig) -> None:
    print("Audio devices:")
    _devices(cfg)
    player = Player(cfg.output_device)
    player.feed(earcon("listen"))  # you should hear a short chime
    await player.drain()
    player.close()
    await _mic_test(cfg)

    print("\nProviders (in the order they'd be tried):")
    for kind, order, chosen in (("stt", STT_ORDER, cfg.stt_provider), ("tts", TTS_ORDER, cfg.tts_provider)):
        for name in order:
            why = unavailable_reason(kind, name)
            print(f"  {kind} {name:8} {'ready' if not why else 'unavailable: ' + why}")
        print(f"  → {kind}_provider = {chosen!r}: " +
              " → ".join(p.name for p in (build_stt(cfg, []) if kind == "stt" else build_tts(cfg))) or "none")
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("SARVAM_API_KEY"):
        print("  (no cloud keys: only offline providers are used)")

    clip = await _test_clip(cfg)
    if clip:
        await _stt_latency(cfg, clip)
    else:
        print("\n(Speech-to-text timing needs the Windows voice to make a test clip: pip install comtypes)")
    await _tts_latency(cfg)
