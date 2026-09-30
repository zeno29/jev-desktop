"""Builds the assistant's components once, for any front end (console or desktop HUD)."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from .actions.browser import Browser
from .actions.spotify import Spotify
from .actions.toolkit import Toolkit
from .actions.windows import AppIndex
from .assistant import Assistant
from .brain import Brain
from .config import DATA_DIR, ROOT, Config, load_config
from .router import Router
from .ui import UI

if TYPE_CHECKING:
    from .voice.controller import VoiceController

log = logging.getLogger("jev")


@dataclass
class Core:
    cfg: Config
    ui: UI
    apps: AppIndex
    browser: Browser
    router: Router
    spotify: Spotify
    toolkit: Toolkit
    brain: Brain
    assistant: Assistant
    voice: VoiceController | None

    def status(self) -> dict:
        return {
            "jev": self.router.jev_enabled,
            "jev_model": self.cfg.jev.model,
            "llm": self.brain.available,
            "fast_model": self.cfg.llm.fast_model,
            "deep_model": self.cfg.llm.deep_model,
            "spotify": self.spotify.configured,
            "apps": len(self.apps.apps),
            "stt": bool(self.voice and self.voice.can_listen),  # the console shows this as VOICE
        }

    def status_line(self) -> str:
        s = self.status()
        return (f"Jev: {'on (' + s['jev_model'] + ')' if s['jev'] else 'OFF — no TYPESAFE_API_KEY, using local rules'}"
                f" · LLM: {'on' if s['llm'] else 'OFF — no GEMINI_API_KEY'}"
                f" · Spotify app: {'on' if s['spotify'] else 'off (no SPOTIFY_CLIENT_ID)'}"
                f" · apps indexed: {s['apps']} · Voice: {self._voice_line()}")

    def _voice_line(self) -> str:
        if self.voice is None:
            return "off"
        d = self.voice.describe()
        return f"hear {'→'.join(d['stt']) or 'NONE'}, speak {'→'.join(d['tts']) or 'NONE'}"

    def warm_up(self) -> None:
        """Background warm-ups so the first real command is fast."""
        asyncio.create_task(self.router.warm())
        if self.voice:
            asyncio.create_task(self.voice.warm_up())
        if self.cfg.browser.prewarm:
            asyncio.create_task(self.browser.start())

    async def close(self) -> None:
        await self.router.close()
        await self.brain.close()
        await self.spotify.close()
        await self.browser.close()
        if self.voice:
            await self.voice.close()


async def build_core(ui: UI, voice: bool = True) -> Core:
    """`voice`: set up the microphone / spoken-reply loop (the desktop HUD uses it; the console doesn't)."""
    load_dotenv(ROOT / ".env")
    cfg = load_config()
    apps = AppIndex(DATA_DIR / "apps.json", cfg.app_aliases)
    await apps.load()
    browser = Browser(cfg.browser)
    router = Router(cfg, apps)

    def launch_spotify() -> bool:
        target = apps.resolve("spotify")
        if target is None:
            return False
        AppIndex.launch(target)
        return True

    spotify = Spotify(os.environ.get("SPOTIFY_CLIENT_ID"), DATA_DIR / "spotify_token.json", launch_spotify, ui.say)
    toolkit = Toolkit(cfg, ui, apps, browser, spotify=spotify, chooser=router.choose_media,
                      mail_ranker=router.rank_mail)
    brain = Brain(cfg, toolkit, ui)
    assistant = Assistant(router, toolkit, brain, ui)
    return Core(cfg, ui, apps, browser, router, spotify, toolkit, brain, assistant,
                _build_voice(cfg, apps) if voice else None)


def _build_voice(cfg: Config, apps: AppIndex) -> VoiceController | None:
    if not cfg.voice.enabled:
        return None
    try:  # audio packages are optional for typing-only use
        from .voice.controller import VoiceController
        from .voice.providers import build_vocabulary
    except (ImportError, OSError) as e:
        log.warning("voice unavailable: %s", e)
        return None
    vocabulary = build_vocabulary([a.name for a in apps.apps.values()], cfg.app_aliases, cfg.contacts)
    return VoiceController(cfg.voice, vocabulary)
