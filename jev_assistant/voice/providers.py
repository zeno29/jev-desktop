"""Picks the speech providers and keeps the order to fall back in.

"auto" prefers Sarvam (built for Indian speech) if SARVAM_API_KEY is set, then Gemini if
GEMINI_API_KEY is set, then whatever runs offline. Kokoro is opt-in (tts_provider = "kokoro"): it
sounds far better than the Windows voices but on a laptop CPU it renders at about real time
(~1.5 s before a short reply starts), too slow to be the automatic fallback. A configured provider goes first, and the
rest of the auto order stays behind it as fallback. A provider that fails sits out for a while
(longer when it says it's out of quota) so later requests don't keep waiting on it.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Generic, TypeVar

from ..config import VoiceConfig
from .base import STTProvider, TTSProvider

log = logging.getLogger("jev.voice")

STT_ORDER = ["sarvam", "gemini", "local"]
TTS_ORDER = ["sarvam", "gemini", "kokoro", "windows"]
AUTO_SKIP = {"kokoro"}    # used only when chosen explicitly
COOLDOWN = 60.0           # seconds out after an error
QUOTA_COOLDOWN = 15 * 60  # ... after a quota / rate-limit error
DAILY_COOLDOWN = 3 * 3600 # ... after a daily cap (Gemini TTS free tier: 10 requests per day per model)

# Said to the router often; biased into recognition so they're spelled right.
COMMON_TERMS = ["Jev", "Spotify", "YouTube", "Chrome", "Gmail", "WhatsApp", "Visual Studio Code",
                "VS Code", "Instagram", "Netflix", "ChatGPT", "GitHub", "Arijit Singh", "MKBHD"]
NOISY_APP_WORDS = re.compile(r"uninstall|readme|help|documentation|manual|release notes|website|"
                             r"setup|install|license|\(x86\)|debug|\.exe", re.I)


def _module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def unavailable_reason(kind: str, name: str) -> str | None:
    """Why a provider can't be used right now (None = usable)."""
    if name == "sarvam":
        return None if os.environ.get("SARVAM_API_KEY") else "no SARVAM_API_KEY in .env"
    if name == "gemini":
        return None if os.environ.get("GEMINI_API_KEY") else "no GEMINI_API_KEY in .env"
    if name == "local":
        return None if _module("faster_whisper") else "pip install -r requirements-local-voice.txt"
    if name == "kokoro":
        from .tts_kokoro import available
        return None if available() else "needs requirements-local-voice.txt + setup_local --kokoro"
    if name == "windows":
        return None if _module("comtypes") else "pip install comtypes"
    return f"unknown {kind} provider"


def _order(choice: str, order: list[str]) -> list[str]:
    choice = choice.lower().strip()
    if choice not in ("", "auto") and choice not in order:
        log.warning("unknown voice provider %r; using auto", choice)
        choice = "auto"
    if choice in ("", "auto"):
        return [n for n in order if n not in AUTO_SKIP]
    return [choice] + [n for n in order if n != choice and n not in AUTO_SKIP]


def build_stt(cfg: VoiceConfig, vocabulary: list[str]) -> list[STTProvider]:
    providers: list[STTProvider] = []
    for name in _order(cfg.stt_provider, STT_ORDER):
        if unavailable_reason("stt", name):
            continue
        if name == "sarvam":
            from .stt_sarvam import SarvamSTT
            providers.append(SarvamSTT(os.environ["SARVAM_API_KEY"], cfg.sarvam_stt_model,
                                       cfg.sarvam_stt_mode, cfg.sarvam_stt_language))
        elif name == "gemini":
            from .stt_gemini import GeminiSTT
            providers.append(GeminiSTT(os.environ["GEMINI_API_KEY"], cfg.gemini_stt_model, cfg.language, vocabulary))
        elif name == "local":
            from .stt_local import LocalSTT
            providers.append(LocalSTT(cfg.local_stt_model, COMMON_TERMS))
    return providers


def build_tts(cfg: VoiceConfig) -> list[TTSProvider]:
    providers: list[TTSProvider] = []
    for name in _order(cfg.tts_provider, TTS_ORDER):
        if unavailable_reason("tts", name):
            continue
        if name == "sarvam":
            from .tts_sarvam import SarvamTTS
            providers.append(SarvamTTS(os.environ["SARVAM_API_KEY"], cfg.sarvam_tts_model,
                                       cfg.sarvam_speaker, cfg.language))
        elif name == "gemini":
            from .tts_gemini import GeminiTTS
            providers.append(GeminiTTS(os.environ["GEMINI_API_KEY"], cfg.gemini_tts_model, cfg.gemini_voice,
                                       cfg.language, cfg.gemini_tts_style))
        elif name == "kokoro":
            from .tts_kokoro import KokoroTTS
            providers.append(KokoroTTS(cfg.kokoro_voice))
        elif name == "windows":
            from .tts_windows import WindowsTTS
            providers.append(WindowsTTS(cfg.windows_voice))
    return providers


P = TypeVar("P")


class Chain(Generic[P]):
    """Providers in preference order, skipping any that failed recently. Quota pauses are saved
    to `state_file`, so a restart doesn't spend seconds rediscovering a spent daily cap."""

    def __init__(self, providers: list[P], kind: str, state_file: Path | None = None):
        self.providers = providers
        self.kind = kind
        self.state_file = state_file
        self._out_until: dict[str, float] = {}  # provider name -> wall-clock time it's usable again
        try:
            saved = json.loads(state_file.read_text(encoding="utf-8")) if state_file and state_file.exists() else {}
            self._out_until = {k.split(":", 1)[1]: v for k, v in saved.items() if k.startswith(kind + ":")}
        except (OSError, ValueError):
            pass

    def ready(self) -> list[P]:
        now = time.time()
        ready = [p for p in self.providers if self._out_until.get(p.name, 0) <= now]
        return ready or self.providers  # all failing: try them all again rather than go silent

    def failed(self, provider: P, error: BaseException) -> None:
        text = str(error).lower()
        pause = COOLDOWN
        if any(w in text for w in ("rate limit", "rate_limit", "quota", "429", "resource_exhausted")):
            pause = DAILY_COOLDOWN if "per day" in text or "daily" in text else QUOTA_COOLDOWN
        self._out_until[provider.name] = time.time() + pause
        log.warning("%s %s failed (%s: %s); using the next provider for %d min", provider.name, self.kind,
                    type(error).__name__, str(error)[:200], pause // 60)
        if pause > COOLDOWN and self.state_file:
            self._save()

    def _save(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
            now = time.time()
            data = {k: v for k, v in data.items() if v > now and not k.startswith(self.kind + ":")}
            data.update({f"{self.kind}:{name}": t for name, t in self._out_until.items() if t > now})
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(data), encoding="utf-8")
        except (OSError, ValueError):
            log.exception("couldn't save voice provider state")

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]


def build_vocabulary(app_names: list[str], aliases: dict[str, str], contacts: dict[str, str],
                     limit: int = 100) -> list[str]:
    """Names worth biasing recognition toward: common targets, aliases, contacts, then installed
    apps (shortest first: those are the ones people say). Gemini works best with ≤100 terms."""
    terms = list(COMMON_TERMS)
    terms += [v for v in aliases.values() if not re.search(r"[\\/:]", v)]
    terms += [name.title() for name in contacts]
    terms += sorted((n for n in app_names if len(n) <= 30 and not NOISY_APP_WORDS.search(n)), key=len)
    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:limit]
