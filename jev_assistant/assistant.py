"""Ties routing, actions and the LLM tiers together for one request at a time."""

from __future__ import annotations

import re
import time

from .actions.toolkit import Toolkit
from .brain import Brain, HandOff
from .router import Route, Router
from .ui import UI

# Tells the LLM tiers the answer will be heard, not read.
SPOKEN_NOTE = "spoken request: the reply is read aloud, so keep it to one or two short plain sentences, no markdown"


class Assistant:
    def __init__(self, router: Router, toolkit: Toolkit, brain: Brain, ui: UI):
        self.router = router
        self.toolkit = toolkit
        self.brain = brain
        self.ui = ui
        self.previous: str | None = None

    async def handle(self, text: str, spoken: bool = False) -> str:
        """Run one request. `spoken`: it came from the microphone and the reply will be read aloud."""
        t0 = time.perf_counter()
        self.toolkit.turn_calls = []
        self.ui.event("routing", text=text)
        route = await self.router.route(text, self.previous)
        self.previous = text
        self._trace_route(route)
        self.ui.event("route", tier=route.tier, action=route.action or "", source=route.source,
                      latency_ms=round(route.latency_ms), confidence=round(route.confidence, 2))

        if route.tier == "action":
            self.ui.event("phase", phase="action", label=route.action)
            reply, is_error = await self.toolkit.call(route.action, route.args)
            if is_error and self.brain.available:  # let the fast model recover from a failed action
                self.ui.trace(f"action failed ({reply}); asking {self.brain.cfg.llm.fast_model}")
                reply = await self._fast(text, _note(f"tried {route.action}({route.args}) but it failed: {reply}", spoken))
            else:
                self.brain.remember_action(text, reply)
        elif route.tier == "fast":
            reply = await self._fast(text, _note(route.note, spoken))
        else:
            reply = await self._deep(text, _note(route.note, spoken))

        self.ui.say(reply, quiet=_quiet(self.toolkit.turn_calls, reply))
        total = (time.perf_counter() - t0) * 1000
        self.ui.trace(f"total {total:.0f} ms")
        self.ui.event("done", total_ms=round(total))
        return reply

    async def _fast(self, text: str, note: str) -> str:
        self.ui.trace(f"thinking fast · {self.brain.cfg.llm.fast_model}")
        self.ui.event("phase", phase="fast", label=self.brain.cfg.llm.fast_model)
        result = await self.brain.fast(text, note)
        if isinstance(result, HandOff):
            # the deep tier should also know its answer will be read aloud
            spoken = SPOKEN_NOTE in note
            return await self._deep(text, _note(f"escalated by fast model: {result.reason}", spoken))
        return result

    async def _deep(self, text: str, note: str) -> str:
        self.ui.trace(f"thinking deep · {self.brain.cfg.llm.deep_model}")
        self.ui.event("phase", phase="deep", label=self.brain.cfg.llm.deep_model)
        return await self.brain.deep(text, note)

    def _trace_route(self, r: Route) -> None:
        head = f"{r.source} {r.latency_ms:.0f} ms → {r.tier}"
        if r.action:
            head += f": {r.action}({', '.join(f'{k}={v!r}' for k, v in r.args.items())})"
        if r.scores:
            head += "  [" + "; ".join(f"{k}: {v}" for k, v in r.scores.items()) + "]"
        elif r.note and r.tier != "action":
            head += f"  [{r.note}]"
        self.ui.trace(head)


def _note(note: str, spoken: bool) -> str:
    return "; ".join(n for n in (note, SPOKEN_NOTE if spoken else "") if n)


# Tools that only find something out: a turn that used just these answered a question, so its reply
# is worth hearing. Everything else changes something on the PC (play, pause, volume, open...).
INFO_TOOLS = {"web_lookup", "get_weather", "check_email", "browser_open", "browser_snapshot", "browser_read", "browser_back"}
INFO_SYSTEM_ACTIONS = {"tell_time", "tell_date"}
FAILURE = re.compile(r"\b(couldn't|could not|can't|cannot|not found|no open window|failed|unable|didn't|"
                     r"wasn't|isn't|aren't|not running|not installed|nothing)\b", re.I)


def _quiet(calls: list[tuple[str, dict, bool]], reply: str) -> bool:
    """True when the command just did something (the action itself is the answer), so a voice reply
    would only be noise. Questions, failures and Jev asking something back are still spoken."""
    did_something = any(
        name not in INFO_TOOLS and not (name == "system_action" and args.get("action") in INFO_SYSTEM_ACTIONS)
        for name, args, _ in calls)
    failed = any(failed for _, _, failed in calls) or bool(FAILURE.search(reply)) or reply.rstrip().endswith("?")
    return did_something and not failed
