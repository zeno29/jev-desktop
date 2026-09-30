"""Speech-to-text bake-off on your own voice.

    python -m jev_assistant.voice.bakeoff                 record 15 phrases, compare providers
    python -m jev_assistant.voice.bakeoff --from DIR      re-run on recordings saved earlier

Shows each phrase, records you reading it (it stops when you stop), and streams the audio to
every available provider at once, exactly as a real command would. Prints each provider's word
error rate against the phrase and its delay after you stop speaking. Recordings and results are
saved to data/bakeoff/<time>/.

Word error rate is strict: "7:00" vs "saat" or "par" vs "pe" count as errors, so read the
transcripts too. Cloud providers get one request per phrase.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from ..config import DATA_DIR, ROOT, load_config
from .audio import Microphone, PcmSource, read_wav, wav_bytes
from .base import STT_RATE
from .providers import build_stt, build_vocabulary
from .vad import Endpointer, make_vad

PHRASES = [
    "Open Spotify",
    "Spotify pe Arijit Singh ke gaane chalao",
    "Play the latest MKBHD video on YouTube",
    "Chrome kholo aur YouTube pe lofi music lagao",
    "Volume thoda kam karo",
    "Next song",
    "Das minute ka timer laga do",
    "What time is it",
    "Lock the screen",
    "Search for best phones under twenty thousand",
    "Visual Studio Code band kar do",
    "Email Rahul saying I will be late for the meeting",
    "Aaj mausam kaisa hai",
    "Screenshot le lo",
    "Open WhatsApp",
]


def words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def wer(ref: str, hyp: str) -> float:
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(h)] / max(1, len(r))


async def run_phrase(providers, source, cfg) -> tuple[bytes, dict[str, tuple[str, float]]]:
    sessions = {p.name: p.session() for p in providers}
    endpoint = Endpointer(make_vad(), cfg.vad_silence_ms, cfg.max_seconds, 8)
    audio = bytearray()
    async for pcm in source.frames():
        audio += pcm
        for s in sessions.values():
            s.feed(pcm)
        if endpoint.push(pcm) in ("end", "max", "no_speech"):
            break
    t0 = time.perf_counter()

    async def finish(name, session):
        try:
            text = await session.finish()
        except Exception as e:
            text = f"[error: {type(e).__name__}: {str(e)[:80]}]"
        return name, (text, time.perf_counter() - t0)

    results = dict(await asyncio.gather(*(finish(n, s) for n, s in sessions.items())))
    return bytes(audio), results


async def main(argv: list[str]) -> None:
    load_dotenv(ROOT / ".env")
    cfg = load_config()
    replay = Path(argv[argv.index("--from") + 1]) if "--from" in argv else None
    apps_cache = DATA_DIR / "apps.json"  # installed apps, as indexed by the assistant
    apps = [a["name"] for a in json.loads(apps_cache.read_text(encoding="utf-8"))] if apps_cache.exists() else []
    vocabulary = build_vocabulary(apps, cfg.app_aliases, cfg.contacts)
    providers = build_stt(cfg.voice, vocabulary)
    if not providers:
        print("No speech-to-text provider is available (see python -m jev_assistant --voice-check).")
        return
    for p in providers:
        await p.warm_up()
    out = DATA_DIR / "bakeoff" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    print(f"Providers: {', '.join(p.name for p in providers)}.  Saving to {out}\n")
    rows = []
    for i, phrase in enumerate(PHRASES, 1):
        if replay:
            wav = replay / f"{i:02d}.wav"
            if not wav.exists():
                continue
            source = PcmSource(read_wav(wav))
        else:
            input(f"[{i}/{len(PHRASES)}]  Press Enter, then say:   {phrase}")
            source = Microphone(cfg.voice.input_device)
        audio, results = await run_phrase(providers, source, cfg.voice)
        (out / f"{i:02d}.wav").write_bytes(wav_bytes(audio, STT_RATE))
        for name, (text, latency) in results.items():
            rows.append({"phrase": phrase, "provider": name, "transcript": text,
                         "wer": round(wer(phrase, text), 3), "latency_ms": round(latency * 1000)})
            print(f"    {name:8} {latency * 1000:5.0f} ms  WER {wer(phrase, text):4.0%}  {text}")
    if not rows:
        return
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print("\nSummary (lower is better):")
    for p in providers:
        mine = [r for r in rows if r["provider"] == p.name]
        delays = sorted(r["latency_ms"] for r in mine)
        print(f"  {p.name:8} WER {sum(r['wer'] for r in mine) / len(mine):5.1%}   "
              f"median delay {delays[len(delays) // 2]} ms   worst {delays[-1]} ms")


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except KeyboardInterrupt:
        pass
