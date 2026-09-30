"""Decides, per request, who handles it:

  action  – run a Toolkit action directly with Jev-extracted arguments (no LLM, ~100-300 ms)
  fast    – Gemini fast model with tools (ambiguous commands, chit-chat, drafting email)
  deep    – Gemini deep model with tools + browser (multi-step / research)

Jev gets ONE request per command with every branch-specific question asked up front; code
then reads only the answers relevant to the chosen route. If Jev is unavailable (no key,
network, timeout), a small local rule set covers the obvious commands.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, RetryPolicy

from .actions.toolkit import parse_duration
from .actions.windows import MEDIA_ACTIONS, SYSTEM_ACTIONS, AppIndex
from .config import Config
from .spans import NONE, candidate_spans

Tier = Literal["action", "fast", "deep"]


@dataclass
class Route:
    tier: Tier
    action: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    source: str = "jev"          # "jev" | "rules"
    note: str = ""               # why — shown in the trace, passed to the LLM as a hint
    latency_ms: float = 0.0
    scores: dict[str, str] = field(default_factory=dict)


# The top-level route question. Keys are handler names; "chat"/"complex"/NONE go to Gemini.
# Options that are easy to confuse carry what / not_for / examples to separate them.
TOOLS = {
    "open_app": {
        "what": "Open, launch or start an application installed on the computer",
        "not_for": "websites the user has no installed app for (see `installed_apps_mentioned`)",
        "examples": ["open spotify", "launch vs code", "start calculator", "spotify kholo", "notepad open karo"],
    },
    "close_app": "Close, quit or exit an application that is running",
    "play_music": {
        "what": "Start playing a song, artist, album, playlist or kind of music, or just some music. "
                "Whatever follows 'play' is usually a song title, even when it reads like a sentence, "
                "a question or a Hindi phrase",
        "not_for": "pausing, resuming or skipping what is already playing; videos that aren't music",
        "examples": ["play blinding lights", "put on some lofi", "play taylor swift on spotify",
                     "play thinking out loud", "play kal ho naa ho", "play some music", "gaane chalao"],
    },
    "play_video": {
        "what": "Find and play a video on YouTube to watch: a video, show clip, tutorial, review, trailer, "
                "podcast episode or creator's upload",
        "not_for": "songs or music the user just wants to listen to, including Bollywood and film songs, "
                   "unless they ask for a video",
        "examples": ["play the latest mkbhd video", "watch a pasta recipe video", "play the dune 2 trailer",
                     "put on a lofi girl stream on youtube"],
    },
    "media_control": {
        "what": "Pause, resume, skip, go back, mute, or change the volume of what is already playing",
        "not_for": "choosing something new to play",
        "examples": ["pause", "resume", "skip this", "next song", "volume to 30", "turn it down",
                     "gaana band karo"],
    },
    "web_search": {
        "what": "Open a web search results page for the user to look at",
        "not_for": "questions the user wants answered or researched for them",
        "examples": ["google cheap flights to goa", "search for python tutorials"],
    },
    "open_website": {
        "what": "Go to or open a specific website or web address",
        "not_for": "installed apps, or doing a task on the site after opening it",
        "examples": ["open youtube.com", "go to github", "open my gmail"],
    },
    "system_action": "Tell the time or date, lock the PC, take a screenshot, or open a common folder or Settings",
    "set_timer": "Set a timer or countdown for some amount of time",
    "get_weather": {
        "what": "Tell the weather, temperature or chance of rain in a place, now, today or tomorrow",
        "not_for": "longer-range forecasts, or weather questions that need reasoning (what to wear, whether to travel)",
        "examples": ["weather in delhi", "will it rain in mumbai tomorrow", "pune mein kitni garmi hai"],
    },
    "send_email": "Write, draft or send an email to someone",
    "check_email": {
        "what": "Check the user's inbox: list new, unread or important emails, or mail from someone, "
                "over some period",
        "not_for": "writing, replying to or sending an email",
        "examples": ["any important mails in the last 24 hours", "check my email", "did amazon email me today",
                     "aaj ke important mails batao"],
    },
    "chat": {
        "what": "Small talk, a greeting, or a question answerable from general knowledge in a sentence or two",
        "not_for": "anything that needs current information, research, or several steps",
        "examples": ["hi", "thanks", "what's the capital of japan", "how many ounces in a pound"],
    },
    "complex": {
        "what": "A multi-step task: research, reading or summarizing pages, comparing options, planning, "
                "writing substantial content, or doing a workflow inside a website",
        "examples": ["find the cheapest flight to delhi next friday", "summarize my unread emails",
                     "compare the iphone 17 and pixel 10"],
    },
    NONE: "None of these, or too unclear to tell",
}

NEEDS_THINKING = Noul(
    instructions=("Would the assistant itself have to reason in several steps, research and read things, "
                  "or write substantial new content to fulfil `request`?"),
    criteria={
        "true": "Yes, e.g. comparing options, answering from web research, summarizing pages, planning, "
                "or doing a multi-step task inside a website",
        "false": "No: one quick computer action is enough, such as opening an app or site, playing music, "
                 "opening a web search for the user to look at, a system action, or a one-line reply",
    },
)

# Span questions: several overlapping spans can all be right ("shape of you" vs "shape of you by
# ed sheeran"), which spreads probability and lowers confidence harmlessly. So these are gated on
# whether a value was named at all — 1 - P(none) — and the top span is used.
SPAN_QUESTIONS = {"music_query", "video_query", "search_query", "website", "weather_place", "email_sender"}

IMPORTANT_MAIL = {
    "true": "Written to the user by a person or organisation about something that matters to them: work, "
            "studies, money, bills, bank or account security, a delivery or booking, an appointment, "
            "official or legal matters, or a reply that needs them",
    "false": "Newsletters, promotions, offers, marketing, social-media notifications, digests, or routine "
             "automated notifications that need no attention",
}

PLAY_VERB = re.compile(r"^(?:(?:hey |ok |okay )?jev[,.]?\s+)?(?:please\s+)?play\s+\S", re.I)
PLAY_VERB_BAR = 0.35   # P(play_music) + P(play_video) enough for a "play ..." command
# "needs thinking" at which even a confident action goes to the deep model instead
DEEP_OVERRIDE = 0.85
# spans that name no music in particular ("play a song")
GENERIC_MUSIC = {"music", "some music", "song", "a song", "songs", "some songs", "something", "gaana", "gaane",
                 "gana", "gane", "music chalao", "gaane chalao"}


def _none(desc: str) -> dict[str, str]:
    return {NONE: desc}


class Router:
    def __init__(self, cfg: Config, apps: AppIndex):
        self.cfg = cfg
        self.apps = apps
        self.client: AsyncTypeSafeClient | None = None
        if os.environ.get("TYPESAFE_API_KEY"):
            self.client = AsyncTypeSafeClient(
                model=cfg.jev.model,
                timeout=cfg.jev.timeout_seconds,
                retry=RetryPolicy(max_retries=1, timeout=cfg.jev.timeout_seconds),
            )

    @property
    def jev_enabled(self) -> bool:
        return self.client is not None

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def choose_media(self, query: str, options: dict[str, str | None]) -> str | None:
        """Pick which search result the user meant (select, don't generate). None if Jev is
        unavailable or nothing fits — the caller falls back to its own heuristic."""
        if self.client is None:
            return None
        question = Choice(
            instructions=("The user asked to play `requested`. Which search result should start playing? "
                          "A named song means that track; just an artist or singer means the artist; a playlist, "
                          "mood or genre means a playlist; an album only if they mention the album."),
            criteria=options | _none("None of these results matches what the user asked for."))
        try:
            resp = await self.client.system_one(state={"requested": query}, questions={"pick": question})
        except Exception:
            return None
        pick = resp.answers["pick"]
        return None if pick.choice == NONE else pick.choice

    async def rank_mail(self, user: str, mails: list[dict]) -> list[float] | None:
        """P(important) for each email, all judged in one request. None if Jev is unavailable."""
        if self.client is None or not mails:
            return None
        questions = {f"m{i}": Noul(instructions=f"Is `emails[{i}]` important for `user` to read personally?",
                                   criteria=IMPORTANT_MAIL) for i in range(len(mails))}
        try:
            resp = await self.client.system_one(state={"user": user, "emails": mails}, questions=questions,
                                                timeout=15, retry=RetryPolicy(max_retries=1, timeout=15))
        except Exception:
            return None
        return [resp.answers[f"m{i}"].noul for i in range(len(mails))]

    async def warm(self) -> None:
        """Open the connection to Jev at startup so the first real command skips TLS setup."""
        if self.client is not None:
            try:
                await self.client.system_one(state="hello", questions={"warm": Noul(instructions="Is this a greeting?")})
            except Exception:
                pass

    # ------------------------------------------------------------------ entry point

    async def route(self, text: str, previous: str | None = None) -> Route:
        t0 = time.perf_counter()
        route = instant_route(text)  # "pause", "next song", "volume 30": no round trip needed
        if route is None and self.client is not None:
            try:
                route = await self._route_with_jev(text, previous)
            except Exception as e:  # any Jev failure degrades to local rules, never blocks the command
                route = rule_route(text, self.apps)
                route.note = f"Jev unavailable ({type(e).__name__}); used local rules. {route.note}".strip()
        elif route is None:
            route = rule_route(text, self.apps)
        route.latency_ms = (time.perf_counter() - t0) * 1000
        return route

    # ------------------------------------------------------------------ Jev

    def _state(self, text: str, previous: str | None, apps: list[str]) -> dict[str, Any]:
        """Everything a judge needs: the request plus the facts that separate close calls
        (e.g. "open youtube" — is there a YouTube app, or is it a website?)."""
        state: dict[str, Any] = {
            "request": text,
            "installed_apps_mentioned": apps or "none of the installed apps resemble the request",
            "saved_websites": sorted(self.cfg.sites),
        }
        if previous:
            state["previous_request"] = {"text": previous, "note": "the user's prior command, for context only"}
        return state

    def _questions(self, text: str, apps: list[str]) -> dict:
        """All branch-specific questions, asked speculatively in one call. Each states its premise,
        because questions are judged independently and can't see the route answer."""
        spans = {s: None for s in candidate_spans(text)} | _none("None of these is the requested value.")
        qs = {
            "tool": Choice(instructions="What does the user want their Windows desktop assistant to do with `request`?",
                           criteria=TOOLS),
            "needs_thinking": NEEDS_THINKING,
            "music_query": Choice(instructions=(
                "If the user wants to play music, which part of `request` is exactly what they want to play — "
                "the song, artist, album, playlist or genre? Leave out command words like 'play' and the name "
                "of the service. Choose none if nothing to play is named."), criteria=spans),
            "music_service": Choice(
                instructions="If the user wants to play music, which service do they ask for in `request`?",
                criteria={"youtube": "YouTube or YouTube Music", "spotify": "Spotify",
                          "unspecified": "No service is mentioned"}),
            "video_query": Choice(instructions=(
                "If the user wants to watch a video, which part of `request` describes the video to find on "
                "YouTube? Leave out command words like 'play', 'watch', 'on youtube' and 'in fullscreen'. "
                "Choose none if no video is described."), criteria=spans),
            "video_fullscreen": Noul(instructions="Does `request` ask for the video to be played fullscreen?"),
            "media_action": Choice(
                instructions="If the user wants to control what is already playing, which change do they want?",
                criteria=MEDIA_ACTIONS | _none("Not a playback or volume request")),
            "search_query": Choice(instructions=(
                "If the user wants a web search, which part of `request` is exactly the text to search for? "
                "Leave out words like 'search for' or 'google'. Choose none if no search text is given."),
                criteria=spans),
            "website": Choice(instructions=(
                "If the user wants to open a website, which part of `request` names the site or web address? "
                "Choose none if no site is named."), criteria=spans),
            "weather_place": Choice(instructions=(
                "If the user asks about the weather, which part of `request` names the city or place? "
                "Choose none if no place is named."), criteria=spans),
            "weather_when": Choice(
                instructions="If the user asks about the weather, for when?",
                criteria={"now": "Right now / currently, or no time is mentioned", "today": "Later today",
                          "tomorrow": "Tomorrow (kal)"}),
            "email_focus": Choice(
                instructions="If the user wants to check their email, which emails do they want to hear about?",
                criteria={"important": "Only important or urgent emails",
                          "all": "Everything new, without asking for important ones only"}),
            "email_sender": Choice(instructions=(
                "If the user wants to check their email, which part of `request` names a sender they want mail "
                "from? Choose none if no sender is named."), criteria=spans),
            "system_action": Choice(instructions="If the user wants a system action, which one?",
                                    criteria=SYSTEM_ACTIONS | _none("Not one of these system actions")),
        }
        if apps:
            qs["app"] = Choice(
                instructions="If the user wants to open or close an application, which installed application do they mean?",
                criteria={a: None for a in apps} | _none("The user doesn't mean any of these applications."))
        return qs

    async def _route_with_jev(self, text: str, previous: str | None) -> Route:
        apps = self.apps.candidates(text)
        resp = await self.client.system_one(state=self._state(text, previous, apps),
                                            questions=self._questions(text, apps))
        a = resp.answers
        cfg = self.cfg.jev
        tool, think = a["tool"], a["needs_thinking"]
        scores = {"tool": f"{tool.choice} {tool.confidence:.2f}", "needs_thinking": f"{think.noul:.2f}",
                  "tokens": str(resp.usage.input_tokens)}

        choice, conf = tool.choice, tool.confidence
        # Song vs video is a cheap mistake (either way the thing plays), so a clear "play X" whose vote
        # is split between the two - or leaks into chat/none for sentence-like titles - still plays.
        p = tool.probabilities
        play = p.get("play_music", 0.0) + p.get("play_video", 0.0)
        # A command that starts with "play" is a play command; the model only has to see a title in it.
        bar = PLAY_VERB_BAR if PLAY_VERB.match(text) else cfg.action_thresholds.get("play_music", cfg.tool_threshold)
        if choice in ("play_music", "play_video", "chat", NONE) and play >= bar:
            choice, conf = max(("play_music", "play_video"), key=lambda k: p.get(k, 0.0)), play
            scores["tool"] = f"{choice} {conf:.2f} (music+video)"

        def go(tier: Tier, note: str, **kw) -> Route:
            return Route(tier=tier, confidence=conf, note=note, scores=scores, **kw)

        def llm(why: str) -> Route:  # not an action: the thinking judgment picks the model tier
            if think.noul >= cfg.deep_threshold:
                return go("deep", f"Jev: needs thinking ({think.noul:.2f}); {why}")
            return go("fast", f"Jev: {why}")

        if choice == "complex" or think.noul >= DEEP_OVERRIDE:
            return go("deep", f"Jev: needs thinking ({think.noul:.2f}), route={choice}")
        if choice in ("chat", NONE):
            return llm(f"route={choice} ({conf:.2f})")
        if choice == "send_email":  # composing text is LLM work; Jev just routed it fast
            return go("fast", "Jev: email request — draft it and call compose_email")

        built = self._build_args(choice, a, text, scores)
        if built is None:
            return llm(f"route={choice} but arguments were unclear — likely {choice}")
        args, arg_conf = built
        # Gate by consequence: a wrong lock/close costs more than a wrong search. A confident action
        # runs even if "needs thinking" is middling: one-word commands ("resume") often score ~0.6 there.
        threshold = cfg.action_thresholds.get(choice, cfg.tool_threshold)
        if choice in ("play_music", "play_video"):
            threshold = min(threshold, bar)
        if conf < threshold:
            return llm(f"route={choice} ({conf:.2f}) below its {threshold:.2f} bar: {args}")
        if arg_conf < cfg.param_threshold:
            return llm(f"route={choice}, low argument confidence ({arg_conf:.2f}): {args}")
        return Route(tier="action", action=choice, args=args,
                     confidence=min(conf, arg_conf), note="Jev", scores=scores)

    def _build_args(self, tool: str, a, text: str, scores: dict) -> tuple[dict, float] | None:
        """Consume only the answers relevant to the chosen tool; uncertainty on unused branches is
        ignored. Confidence is the weakest judgement used — one wrong argument spoils the call."""

        def pick(key: str):
            ans = a.get(key)
            if ans is None:
                return None
            if key in SPAN_QUESTIONS:
                # Overlapping spans can all be right; what matters is that *something* was named.
                named = 1.0 - ans.probabilities.get(NONE, 0.0)
                best = max((kv for kv in ans.probabilities.items() if kv[0] != NONE),
                           key=lambda kv: kv[1], default=None)
                scores[key] = f"{best[0] if best else NONE} named={named:.2f}"
                return _Picked(best[0], named) if best else None
            scores[key] = f"{ans.choice} {ans.confidence:.2f}"
            return None if ans.choice == NONE else ans

        if tool in ("open_app", "close_app"):
            app = pick("app")
            return ({"name": app.choice}, app.confidence) if app else None
        if tool == "play_music":
            q, svc = pick("music_query"), a["music_service"]
            service = svc.choice if svc.choice in ("youtube", "spotify") else "default"
            # "play some music" / "gaane chalao": nothing particular named -> carry on with the user's music
            generic = not q or q.confidence < self.cfg.jev.param_threshold or q.choice.lower() in GENERIC_MUSIC
            return {"query": "" if generic else q.choice, "service": service}, 1.0 if generic else q.confidence
        if tool == "play_video":
            q = pick("video_query")
            if not q:
                return None
            return {"query": q.choice, "fullscreen": a["video_fullscreen"].noul >= 0.5}, q.confidence
        if tool == "media_control":
            m = pick("media_action")
            if not m:
                return None
            level = _number(text)
            if m.choice == "set_volume" and level is None:
                return None
            return {"action": m.choice, "level": level if level is not None else -1}, m.confidence
        if tool == "web_search":
            q = pick("search_query")
            return ({"query": q.choice}, q.confidence) if q else None
        if tool == "open_website":
            w = pick("website")
            return ({"site": w.choice}, w.confidence) if w else None
        if tool == "system_action":
            s = pick("system_action")
            return ({"action": s.choice}, s.confidence) if s else None
        if tool == "get_weather":  # no place named: the LLM asks, or knows it from the conversation
            w = pick("weather_place")
            return ({"place": w.choice, "when": a["weather_when"].choice}, w.confidence) if w else None
        if tool == "check_email":
            sender = pick("email_sender")
            sender = sender.choice if sender and sender.confidence >= self.cfg.jev.param_threshold else ""
            focus = "all" if sender else a["email_focus"].choice  # "any mail from HDFC": all of theirs
            return {"hours": mail_hours(text), "focus": focus, "sender": sender}, 1.0
        if tool == "set_timer":
            seconds = parse_duration(text)
            return ({"seconds": seconds, "label": ""}, 1.0) if seconds else None
        return None


@dataclass
class _Picked:
    """A span answer reduced to (value, probability that any value was named)."""
    choice: str
    confidence: float


_WINDOW = re.compile(r"\b(?:(\d+|an?|one|two|three|four|five|six|seven)\s*)?(hour|hr|day|week)s?\b", re.I)
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}


def mail_hours(text: str) -> float:
    """How far back to look: "last 6 hours", "past 2 days", "today", "this week"; default a day."""
    low = text.lower()
    if m := _WINDOW.search(low):
        n = 1 if not m[1] else int(m[1]) if m[1].isdigit() else _WORDS[m[1]]
        return n * {"hour": 1, "hr": 1, "day": 24, "week": 168}[m[2]]
    now = time.localtime()
    since_midnight = now.tm_hour + now.tm_min / 60
    if re.search(r"\b(today|aaj)\b", low):
        return max(since_midnight, 1)
    if re.search(r"\b(yesterday|kal)\b", low):
        return since_midnight + 24
    if re.search(r"\bweek\b|\bhafte\b", low):
        return 168
    return 24


def _number(text: str) -> int | None:
    m = re.search(r"\b(\d{1,3})\s*%?", text)
    return min(int(m.group(1)), 100) if m else None


# ---------------------------------------------------------------------- local fallback rules

_RULES: list[tuple[re.Pattern, str]] = [(re.compile(p, re.I), name) for p, name in [
    (r"^(?:open|go to|visit)\s+(?P<site>(?:https?://)?[\w-]+(?:\.[\w-]+)+\S*)$", "open_website"),
    (r"^(?:open|launch|start|run)\s+(?P<name>.{1,40})$", "open_app"),
    (r"^(?:close|quit|exit|kill)\s+(?P<name>.{1,40})$", "close_app"),
    (r"^(?:play|watch)\s+(?P<query>.+?)\s+(?:on|from)\s+youtube$", "play_video"),
    (r"^watch\s+(?P<query>.+)$", "play_video"),
    (r"^play\s+(?P<query>.+?)(?:\s+on\s+(?P<service>spotify))?$", "play_music"),
    (r"^(?:search(?: for)?|google|look up)\s+(?P<query>.+)$", "web_search"),
]]
_MEDIA_WORDS = {"pause": "pause", "stop": "pause", "resume": "resume", "unpause": "resume",
                "next": "next", "skip": "next", "previous": "previous", "back": "previous",
                "mute": "mute", "unmute": "mute"}
_DEEP_HINT = re.compile(r"\b(research|compare|summari[sz]e|plan|write|draft|explain|analy[sz]e|find out|"
                        r"and then|step by step|best)\b", re.I)


_INSTANT_MEDIA = re.compile(r"^(?P<verb>pause|resume|unpause|stop|next|skip|previous|mute|unmute)"
                            r"(?: (?:the |this )?(?:music|song|track|video|gaana|it|this))?$", re.I)
_INSTANT_VOLUME = re.compile(r"^(?:set )?(?:the )?volume (?:(?P<dir>up|down)|(?:to )?(?P<level>\d{1,3})(?: ?%| percent)?)$",
                             re.I)


def instant_route(text: str) -> Route | None:
    """Whole-command matches for the few commands that can't mean anything else. Anything
    longer or looser goes to Jev, which handles phrasing and Hinglish far better than regexes."""
    t = re.sub(r"^(?:hey |ok |okay )?jev[,.]?\s+", "", text.strip().rstrip(".!?").lower())
    if m := _INSTANT_MEDIA.match(t):
        return Route("action", "media_control", {"action": _MEDIA_WORDS[m["verb"]], "level": -1}, 1.0,
                     source="rules", note="instant")
    if m := _INSTANT_VOLUME.match(t):
        args = ({"action": f"volume_{m['dir']}", "level": -1} if m["dir"]
                else {"action": "set_volume", "level": min(int(m["level"]), 100)})
        return Route("action", "media_control", args, 1.0, source="rules", note="instant")
    return None


def rule_route(text: str, apps: AppIndex) -> Route:
    t = text.strip().rstrip(".!?")
    low = t.lower()
    for pattern, name in _RULES:
        m = pattern.match(t)
        if not m:
            continue
        args = {k: v for k, v in m.groupdict().items() if v}
        if name in ("open_app", "close_app") and apps.resolve(args["name"]) is None:
            continue
        if name == "play_music":
            args.setdefault("service", "default")
        if name == "play_video":
            args["fullscreen"] = False
        return Route("action", name, args, 1.0, source="rules", note="local rule")
    words = low.split()
    if words and words[0] in _MEDIA_WORDS and len(words) <= 3:
        return Route("action", "media_control", {"action": _MEDIA_WORDS[words[0]], "level": -1}, 1.0, "rules")
    if "volume" in low:
        level = _number(low)
        if level is not None:
            return Route("action", "media_control", {"action": "set_volume", "level": level}, 1.0, "rules")
        action = "volume_down" if re.search(r"\b(down|lower|decrease|quieter)\b", low) else "volume_up"
        return Route("action", "media_control", {"action": action, "level": -1}, 1.0, "rules")
    if re.search(r"\bwhat(?:'s| is)? the time\b|\bwhat time\b", low):
        return Route("action", "system_action", {"action": "tell_time"}, 1.0, "rules")
    if re.search(r"\b(lock (?:the )?(?:screen|pc|computer))\b", low):
        return Route("action", "system_action", {"action": "lock"}, 1.0, "rules")
    if "timer" in low and (seconds := parse_duration(low)):
        return Route("action", "set_timer", {"seconds": seconds, "label": ""}, 1.0, "rules")
    deep = len(words) > 18 or bool(_DEEP_HINT.search(low))
    return Route("deep" if deep else "fast", source="rules", note="no rule matched")


if __name__ == "__main__":  # self-check of the no-network paths
    for t, want in [("pause", ("pause", -1)), ("Resume.", ("resume", -1)), ("next song", ("next", -1)),
                    ("Hey Jev, skip this", ("next", -1)), ("volume 30", ("set_volume", 30)),
                    ("volume down", ("volume_down", -1)), ("set the volume to 80%", ("set_volume", 80))]:
        r = instant_route(t)
        assert r and (r.args["action"], r.args["level"]) == want, (t, r)
    for t in ("pause the music and open notepad", "play despacito", "next level song", "stop the timer now"):
        assert instant_route(t) is None, t
    assert PLAY_VERB.match("Hey Jev, play Heeriye.") and not PLAY_VERB.match("what should I play")
    assert mail_hours("important mails from the last 24 hours") == 24 and mail_hours("past 2 days") == 48
    assert mail_hours("check my email") == 24 and mail_hours("any mail this week") == 168
    assert mail_hours("mails in the last hour") == 1
    print("ok")
