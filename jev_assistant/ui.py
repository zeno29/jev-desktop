"""Console front end. Anything that talks to the user goes through a UI so a GUI/voice front end
can replace it later without touching the routing or actions."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Protocol

DIM, CYAN, YELLOW, RESET = "\033[2m", "\033[36m", "\033[33m", "\033[0m"


class UI(Protocol):
    def say(self, text: str, quiet: bool = False) -> None: ...  # quiet: an action's result, not worth speaking
    def notify(self, text: str) -> None: ...
    def trace(self, text: str) -> None: ...
    def event(self, kind: str, **data) -> None: ...  # structured progress for rich front ends
    async def confirm(self, question: str) -> bool: ...


class ConsoleUI:
    def __init__(self, show_trace: bool = True):
        os.system("")  # enable ANSI escape handling in the Windows console
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")
        self.show_trace = show_trace

    def say(self, text: str, quiet: bool = False) -> None:
        print(f"{CYAN}jev ›{RESET} {text}", flush=True)

    def notify(self, text: str) -> None:
        print(f"\n{YELLOW}{text}{RESET}", flush=True)

    def trace(self, text: str) -> None:
        if self.show_trace:
            print(f"{DIM}  {text}{RESET}", flush=True)

    def event(self, kind: str, **data) -> None:
        pass  # the console already shows traces

    async def confirm(self, question: str) -> bool:
        answer = await asyncio.to_thread(input, f"{YELLOW}{question}{RESET}\n  [y/N] › ")
        return answer.strip().lower() in ("y", "yes")

    async def read(self) -> str:
        return await asyncio.to_thread(input, "\nyou › ")
