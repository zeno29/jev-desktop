"""The single set of actions the assistant can take.

The Jev fast path calls these directly; the Gemini tiers see the same actions as tools.
Anything with outside-world side effects (sending mail, clicking "Send"/"Buy"/"Delete" on a page)
asks the user first, no matter which tier triggered it.
"""

from __future__ import annotations

import asyncio
import re
import winsound
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote_plus

from rapidfuzz import fuzz, process

from ..config import Config
from ..ui import UI
from . import lookup, mail, windows
from .browser import Browser
from .spotify import GENERIC_MUSIC_QUERY, Chooser, Spotify, SpotifyError
from .windows import App, AppIndex

RISKY_CLICK = re.compile(
    r"\b(send|submit|post|publish|buy|purchase|pay|checkout|place order|order|delete|remove|"
    r"confirm|transfer|subscribe|unsubscribe|sign ?up|reply all|forward)\b", re.I)
# mail_ranker(user address, [email as dict]) -> P(important) per email, or None if Jev is unavailable
MailRanker = Callable[[str, list[dict]], Awaitable[list[float] | None]]
IMPORTANT = 0.5  # P(important) at which an email is read out
RISKY_KEYS = re.compile(r"(control|ctrl|meta)\+enter", re.I)


@dataclass
class Action:
    name: str
    description: str
    properties: dict[str, dict]
    fn: Callable[..., Awaitable[str]]
    browser: bool = False  # only offered to the deep tier
    timeout: float = 45.0  # nothing may hang a command forever (actions that wait for the user get longer)

    def function_declaration(self) -> dict:
        """Gemini Interactions API function tool."""
        decl = {"type": "function", "name": self.name, "description": self.description}
        if self.properties:  # parameterless functions omit the schema entirely
            decl["parameters"] = {"type": "object", "properties": self.properties,
                                  "required": list(self.properties)}
        return decl


def _s(desc: str) -> dict:
    return {"type": "string", "description": desc}


class Toolkit:
    def __init__(self, cfg: Config, ui: UI, apps: AppIndex, browser: Browser,
                 spotify: Spotify | None = None, chooser: Chooser | None = None,
                 mail_ranker: MailRanker | None = None):
        self.cfg = cfg
        self.ui = ui
        self.apps = apps
        self.browser = browser
        self.spotify = spotify
        self.chooser = chooser  # Jev: picks which search result the user meant
        self.mail_ranker = mail_ranker  # Jev: judges which emails are important
        self._timers: set[asyncio.Task] = set()
        self.actions: dict[str, Action] = {a.name: a for a in self._define()}
        self.turn_calls: list[tuple[str, dict, bool]] = []  # (action, args, failed) for the current command

    # ------------------------------------------------------------------ registry

    def _define(self) -> list[Action]:
        return [
            Action("open_app", "Open/launch an installed application by name.",
                   {"name": _s("App name as the user said it, e.g. 'spotify', 'vs code'.")}, self.open_app),
            Action("close_app", "Close a running application (politely, like clicking X).",
                   {"name": _s("App name, e.g. 'chrome', 'spotify'.")}, self.close_app),
            Action("play_music", "Search for and start playing a song, artist, album or playlist.",
                   {"query": _s("What to play, e.g. 'bohemian rhapsody queen'."),
                    "service": {"type": "string", "enum": ["youtube", "spotify", "default"],
                                "description": "Service; 'default' uses the user's configured one."}},
                   self.play_music),
            Action("play_video", "Find a video on YouTube and start playing it (top search result).",
                   {"query": _s("What to watch, e.g. 'mkbhd iphone review', 'how to make pasta'."),
                    "fullscreen": {"type": "boolean", "description": "True if the user asks for fullscreen."}},
                   self.play_video),
            Action("media_control", "Control whatever media is playing and the system volume.",
                   {"action": {"type": "string", "enum": list(windows.MEDIA_ACTIONS)},
                    "level": {"type": "integer", "description": "0-100 for set_volume, otherwise -1."}},
                   self.media_control),
            Action("web_search", "Open a Google search in the browser for the user to look at." + self._for_user,
                   {"query": _s("Search query.")}, self.web_search),
            Action("open_website", "Open a website in the browser." + self._for_user,
                   {"site": _s("A URL, domain, or well-known site name.")}, self.open_website),
            Action("web_lookup",
                   "Look up facts that change or may be newer than your knowledge: news, scores, prices, "
                   "exchange rates, recent events, who currently holds a position. Returns search-result "
                   "snippets for you to answer from; nothing opens on screen, takes about a second. "
                   "Don't use it for general knowledge you already know.",
                   {"query": _s("Search query, e.g. 'india vs australia score today'.")}, self.web_lookup),
            Action("get_weather", "Current weather and today's/tomorrow's forecast for a place.",
                   {"place": _s("City or place name, e.g. 'Delhi'.")}, self.get_weather),
            Action("system_action", "Common OS actions: time/date, lock, screenshot, open folders/settings.",
                   {"action": {"type": "string", "enum": list(windows.SYSTEM_ACTIONS)}}, self.system_action),
            Action("set_timer", "Start a countdown timer that beeps and notifies when done.",
                   {"seconds": {"type": "integer", "description": "Duration in seconds."},
                    "label": _s("Short label, or empty string.")}, self.set_timer),
            Action("compose_email",
                   "Open a pre-filled email draft in the user's webmail. If send is true, "
                   + ("the user reviews it and presses Send themselves. " if self.browser.real else
                      "the user is asked to confirm and it is then sent. ")
                   + "Resolve contact names to addresses first.",
                   {"to": _s("Recipient email address(es), comma-separated."),
                    "subject": _s("Subject line."), "body": _s("Plain-text body."),
                    "send": {"type": "boolean", "description": "True to send (after user confirmation)."}},
                   self.compose_email, timeout=180),
            Action("check_email",
                   "Read the user's Gmail inbox: important (or all) emails from the last hours, optionally from "
                   "one sender. Use this for any question about their email, never the browser.",
                   {"hours": {"type": "number", "description": "How far back to look, e.g. 24."},
                    "focus": {"type": "string", "enum": ["important", "all"]},
                    "sender": _s("Sender name or address to filter by, or empty string.")},
                   self.check_email),
            # Generic browser control — deep tier only.
            Action("browser_open", "Navigate the current browser tab (or a new one) to a URL.",
                   {"url": _s("Absolute URL."), "new_tab": {"type": "boolean", "description": "Open in a new tab."}},
                   self.browser_open, browser=True),
            Action("browser_snapshot",
                   "Accessibility tree of the current page (roles + names). Use it to find what to click/type.",
                   {}, self.browser_snapshot, browser=True),
            Action("browser_read", "Visible text of the current page, for reading/summarizing.",
                   {}, self.browser_read, browser=True),
            Action("browser_click", "Click an element by its accessible name (and ARIA role from the snapshot).",
                   {"name": _s("Accessible name or visible text."),
                    "role": _s("ARIA role like 'button', 'link', 'tab', or empty string.")},
                   self.browser_click, browser=True, timeout=180),
            Action("browser_type", "Fill a text field identified by its accessible name/label/placeholder.",
                   {"name": _s("Field's accessible name, label, or placeholder."),
                    "text": _s("Text to enter."),
                    "role": _s("ARIA role, usually 'textbox', 'searchbox' or 'combobox'."),
                    "submit": {"type": "boolean", "description": "Press Enter after typing."}},
                   self.browser_type, browser=True),
            Action("browser_press", "Press a key or chord in the page, e.g. 'Escape', 'PageDown', 'Control+a'.",
                   {"key": _s("Playwright key name.")}, self.browser_press, browser=True, timeout=180),
            Action("browser_back", "Go back one page.", {}, self.browser_back, browser=True),
        ]

    def function_declarations(self, include_browser: bool) -> list[dict]:
        return [a.function_declaration() for a in self.actions.values() if include_browser or not a.browser]

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run an action. Returns (result text, is_error)."""
        action = self.actions.get(name)
        if action is None:
            return f"Unknown action: {name}", True
        try:
            result = await asyncio.wait_for(action.fn(**args), action.timeout), False
        except TimeoutError:
            result = f"{name} didn't finish within {action.timeout:.0f} s and was stopped.", True
        except Exception as e:  # tool errors go back to the model / user, never crash the loop
            result = f"{type(e).__name__}: {e}", True
        self.turn_calls.append((name, args, result[1]))
        return result

    # ------------------------------------------------------------------ apps

    async def open_app(self, name: str) -> str:
        target = self.apps.resolve(name)
        if target is None:  # maybe installed since the last scan
            await self.apps.refresh()
            target = self.apps.resolve(name)
        if target is None:
            return f"I couldn't find an app called “{name}”."
        AppIndex.launch(target)
        return f"Opening {target.name if isinstance(target, App) else name}."

    async def close_app(self, name: str) -> str:
        target = self.apps.resolve(name)
        app = target if isinstance(target, App) else None
        closed = await asyncio.to_thread(windows.close_app_windows, app.name if app else name, app)
        if not closed:
            return f"No open window found for “{name}”."
        return f"Closing {app.name if app else name}" + (f" ({len(closed)} windows)." if len(closed) > 1 else ".")

    # ------------------------------------------------------------------ media

    async def play_music(self, query: str, service: str = "default") -> str:
        service = self.cfg.music.provider if service in ("default", "", None) else service
        if service == "spotify":
            if self.spotify and self.spotify.configured:
                try:  # desktop app via the Web API (Premium); "" = resume the user's music
                    return await (self.spotify.play_query(query, self.chooser) if query else self.spotify.resume())
                except SpotifyError as e:
                    self.ui.trace(f"Spotify app playback failed ({e}); using the web player")
            return await self.browser.play_spotify(query or GENERIC_MUSIC_QUERY)
        return await self.browser.play_youtube(query or GENERIC_MUSIC_QUERY)

    async def play_video(self, query: str, fullscreen: bool = False) -> str:
        return await self.browser.play_youtube(query, fullscreen=fullscreen)

    async def media_control(self, action: str, level: int = -1) -> str:
        return windows.media(action, level)

    # ------------------------------------------------------------------ web

    @property
    def _for_user(self) -> str:
        # Real mode opens the user's own Chrome, which the page tools can't see.
        return (" It opens in the user's own Chrome, which you can't read or control; use browser_open "
                "for pages you need to read.") if self.browser.real else ""

    async def web_lookup(self, query: str) -> str:
        return await lookup.web_lookup(query)

    async def get_weather(self, place: str, when: str = "") -> str:
        return await lookup.weather(place, when)

    async def web_search(self, query: str) -> str:
        return await self.browser.search(query)

    def site_url(self, site: str) -> str:
        s = site.strip().lower()
        for key in (s, s.removeprefix("the ")):
            if key in self.cfg.sites:
                return self.cfg.sites[key]
        if re.match(r"^https?://", s):
            return site.strip()
        if re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", s):
            return "https://" + s
        match = process.extractOne(s, list(self.cfg.sites), scorer=fuzz.WRatio, score_cutoff=88)
        if match:
            return self.cfg.sites[match[0]]
        # DuckDuckGo "I'm feeling lucky" jumps straight to the top result for a site name.
        return "https://duckduckgo.com/?q=" + quote_plus("\\" + s)

    async def open_website(self, site: str) -> str:
        return await self.browser.open_site(self.site_url(site), label=site.strip())

    # ------------------------------------------------------------------ system

    async def system_action(self, action: str) -> str:
        return windows.system_action(action)

    async def set_timer(self, seconds: int, label: str = "") -> str:
        if seconds <= 0:
            return "Timer length must be positive."

        async def ring():
            await asyncio.sleep(seconds)
            self.ui.notify(f"⏰ Timer done{': ' + label if label else ''}!")
            for _ in range(3):
                await asyncio.to_thread(winsound.Beep, 880, 350)

        task = asyncio.create_task(ring())
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)
        return f"Timer set for {_human_duration(seconds)}" + (f" ({label})." if label else ".")

    # ------------------------------------------------------------------ email

    async def compose_email(self, to: str, subject: str, body: str, send: bool = False) -> str:
        to = ", ".join(self.cfg.contacts.get(p.strip().lower(), p.strip()) for p in to.split(",") if p.strip())
        page = await self.browser.compose_email(
            self.cfg.email.provider, to, subject, body, self.cfg.email.outlook_base)
        if page is None:
            # Real Chrome: Jev can't press Send there, so the user's own Send click is the
            # confirmation (no extra Jev prompt in front of it).
            if send:
                return f"Draft to {to} is ready in Chrome. Check it, then press Send (Ctrl+Enter)."
            return f"Draft to {to} is open in Chrome for you to review."
        if not send:
            return f"Draft to {to} is open in the browser for you to review."
        preview = f"Send this email?\n  To: {to}\n  Subject: {subject}\n  ---\n  " + body.replace("\n", "\n  ")
        if not await self.ui.confirm(preview):
            return "Not sent — the draft is still open in the browser."
        if await self.browser.click_send(page):
            return f"Email sent to {to}."
        return "I couldn't find the Send button — the draft is open; please send it manually."

    async def check_email(self, hours: float = 24, focus: str = "important", sender: str = "") -> str:
        hours = max(float(hours), 1)
        span = f"{hours / 24:.0f} days" if hours >= 48 and hours % 24 == 0 else _human_duration(round(hours * 3600))
        if not mail.configured():  # no inbox access: show the same search in the user's Gmail instead
            query = f"in:inbox newer_than:{-(-hours // 24):.0f}d" + (" is:important" if focus == "important" else "")
            query += (f' from:"{sender}"' if " " in sender else f" from:{sender}") if sender else ""
            await self.browser.open_site("https://mail.google.com/mail/u/0/#search/" + quote_plus(query), "Gmail")
            return ("Opened that search in Gmail. To have me read your mail out instead, add GMAIL_ADDRESS and "
                    "GMAIL_APP_PASSWORD to .env (see the README).")
        mails = await mail.recent(hours, sender)
        who = f" from {sender}" if sender else ""
        if not mails:
            return f"No emails{who} in the last {span}."
        if focus == "all":
            shown = mails
        else:
            probs = await self.mail_ranker(mail.address(), [
                {"from": m.sender, "subject": m.subject, "received": m.received.strftime("%a %d %b %H:%M"),
                 "preview": m.preview, "gmail_marked_important": m.gmail_important}
                for m in mails]) if self.mail_ranker else None
            if probs is None:  # Jev unavailable: fall back to Gmail's own marker
                probs = [float(m.gmail_important) for m in mails]
            shown = [m for p, m in sorted(zip(probs, mails), key=lambda pm: -pm[0]) if p >= IMPORTANT]
            if not shown:
                return f"Nothing important{who} in the last {span} — {len(mails)} other emails."
        kind = "important " if focus == "important" else ""
        head = f"{len(shown)} {kind}email{'s' if len(shown) != 1 else ''}{who} in the last {span}"
        rest = len(mails) - len(shown)
        head += f" ({rest} others skipped):" if rest else ":"
        return head + "".join(f"\n• {m.line()}" for m in shown[:10])

    # ------------------------------------------------------------------ generic browser (deep tier)

    async def browser_open(self, url: str, new_tab: bool = False) -> str:
        page = await self.browser.goto(url, new_tab=new_tab)
        return f"Opened {page.url} — “{await page.title()}”"

    async def browser_snapshot(self) -> str:
        return await self.browser.snapshot()

    async def browser_read(self) -> str:
        return await self.browser.read_text()

    async def browser_click(self, name: str, role: str = "") -> str:
        if RISKY_CLICK.search(name):
            page = await self.browser.page()
            if not await self.ui.confirm(f"Click “{name}” on {page.url}?"):
                return "User declined the click."
        return await self.browser.click(name, role)

    async def browser_type(self, name: str, text: str, role: str = "", submit: bool = False) -> str:
        return await self.browser.type_text(name, text, role, submit)

    async def browser_press(self, key: str) -> str:
        if RISKY_KEYS.search(key) and not await self.ui.confirm(f"Press {key} (this may send/submit)?"):
            return "User declined the key press."
        return await self.browser.press(key)

    async def browser_back(self) -> str:
        return await self.browser.back()


def _human_duration(seconds: int) -> str:
    parts = []
    for unit, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {unit}{'s' if n != 1 else ''}")
    return " ".join(parts)


DURATION = re.compile(
    r"(\d+(?:\.\d+)?|an?|half an?)\s*(h|hrs?|hours?|m|mins?|minutes?|s|secs?|seconds?)\b", re.I)


def parse_duration(text: str) -> int | None:
    total = 0.0
    for amount, unit in DURATION.findall(text):
        a = amount.lower()
        n = 0.5 if a.startswith("half") else 1.0 if a in ("a", "an") else float(a)
        u = unit.lower()[0]
        total += n * (3600 if u == "h" else 60 if u == "m" else 1)
    return int(total) or None
