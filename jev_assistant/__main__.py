"""Run with:  python -m jev_assistant            (interactive console)
            python -m jev_assistant "open spotify"   (one command, then exit)
            pythonw -m jev_assistant.gui     (desktop HUD with global hotkey + voice)
Flags:      --quiet           hide routing traces
            --spotify-login   connect your Spotify account (one time)
            --voice-check     test microphone, speakers and speech providers"""

from __future__ import annotations

import asyncio
import sys

from .core import build_core
from .ui import ConsoleUI

EXIT_WORDS = {"exit", "quit", "bye", "goodbye"}


async def main(argv: list[str]) -> None:
    quiet = "--quiet" in argv
    if "--voice-check" in argv:
        await _voice_check()
        return
    one_shot = " ".join(a for a in argv if not a.startswith("--")).strip()

    ui = ConsoleUI(show_trace=not quiet)
    core = await build_core(ui, voice=False)
    ui.trace(core.status_line())

    try:
        if "--spotify-login" in argv:
            if not core.spotify.configured:
                ui.say("Add SPOTIFY_CLIENT_ID to .env first (see README → Spotify).")
            else:
                await core.spotify.login()
                ui.say("Spotify connected.")
            return
        if one_shot:
            await core.assistant.handle(one_shot)
            return
        core.warm_up()
        ui.say("Ready. Type a command (or 'exit').")
        while True:
            text = (await ui.read()).strip()
            if not text:
                continue
            if text.lower() in EXIT_WORDS:
                break
            try:
                await core.assistant.handle(text)
            except Exception as e:  # keep the assistant alive whatever one command does
                ui.say(f"Something went wrong: {type(e).__name__}: {e}")
    finally:
        await core.close()


async def _voice_check() -> None:
    from dotenv import load_dotenv

    from .config import ROOT, load_config
    from .voice.check import voice_check
    load_dotenv(ROOT / ".env")
    ConsoleUI()  # UTF-8 console output
    await voice_check(load_config().voice)


def run() -> None:
    try:
        asyncio.run(main(sys.argv[1:]))
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    run()
