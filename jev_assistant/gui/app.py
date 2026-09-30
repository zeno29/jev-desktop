"""Desktop front end ("Stream" design): a smoke-orb widget (top-right by default) plus a stream of
floating chat bubbles beneath it.

  * The orb is always on the desktop. It drifts slowly while idle and swirls, swells and takes on
    the stage colour while Jev listens, works or speaks.
  * Click the orb: the bubble stream drops down beneath it. Esc (or auto-hide) puts it away. The
    console window is cut to the bubbles' shapes, so the desktop shows (and is clickable) between.
  * The global hotkey (default Ctrl+Alt+J) is for voice: a chime, the orb listens, and it stops
    by itself when you stop talking. The reply is spoken and captioned in a bubble beside the orb.
    Press the hotkey again to cancel, or to cut a spoken reply short and talk.
    Typed commands are never spoken.
  * Drag the orb to move it; right-click it for Open console / Quit.

Threads:
  main       – pywebview GUI loop for the console (WinForms/WebView2 needs the main thread)
  jev-orb    – the orb's own Win32 window + message loop (layered window, drawn with Pillow)
  jev-bubble – the caption bubble's window (same technique, click-through)
  jev-core   – asyncio loop running the assistant (Jev routing, actions, Gemini, Playwright)
               and the voice loop (microphone, speech-to-text, text-to-speech)
  jev-hotkey – Win32 RegisterHotKey message loop
  jev-js     – single sender that pushes events into the console page in order
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetProcessDPIAware()  # before any window exists: pywebview assumes system-DPI awareness too

import webview  # noqa: E402

from ..config import DATA_DIR, load_config  # noqa: E402
from ..core import Core, build_core  # noqa: E402
from .bubble import Bubble  # noqa: E402
from .hotkey import GlobalHotkey  # noqa: E402
from .orb import OrbWidget  # noqa: E402

WEB = Path(__file__).parent / "web" / "index.html"
TITLE = "Jev Console"
CONSOLE_SIZE = (500, 640)   # logical px: the bubble stream's width and its tallest extent
ORB_INSET = 24              # the stream's right edge sits this far inside the orb window's right edge
GAP = 8                     # logical px between the orb and the top of the stream
QUIT_WORDS = {"quit jev", "exit jev", "close jev", "shut down jev", "shutdown jev"}
LEVEL_HZ = 15               # mic level updates sent to the console's level bars while listening

gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
gdi32.CreateRectRgn.restype = wintypes.HANDLE
gdi32.CombineRgn.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.c_int]
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
RGN_OR = 2
SPI_GETWORKAREA = 0x30

log = logging.getLogger("jev.gui")


class HudUI:
    """The UI protocol: text goes to the console page, activity drives the orb."""

    ORB_FOR_EVENT = {"routing": "routing", "confirm": "confirm"}

    def __init__(self, app: "HudApp"):
        self.app = app
        self._confirms: dict[str, asyncio.Future] = {}

    def say(self, text: str, quiet: bool = False) -> None:
        self.app.send({"type": "say", "text": text})
        if quiet:
            self.app.acknowledge(text)
        else:
            self.app.speak(text)

    def notify(self, text: str) -> None:
        self.app.open_console(focus=False)
        self.app.send({"type": "notify", "text": text})

    def trace(self, text: str) -> None:
        log.info(text)

    def event(self, kind: str, **data) -> None:
        if kind == "phase":
            self.app.orb_state(data.get("phase", "routing"))
        elif kind == "done":
            self.app.orb_state("idle")
        elif kind in self.ORB_FOR_EVENT:
            self.app.orb_state(self.ORB_FOR_EVENT[kind])
        self.app.send({"type": kind, **data})

    async def confirm(self, question: str) -> bool:
        cid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._confirms[cid] = fut
        self.app.orb_state("confirm")
        self.app.open_console(focus=True)
        self.app.send({"type": "confirm", "id": cid, "text": question})
        try:
            return await asyncio.wait_for(fut, timeout=120)
        except asyncio.TimeoutError:
            self.app.send({"type": "confirm_closed", "id": cid})
            return False
        finally:
            self._confirms.pop(cid, None)
            self.app.orb_state("routing")

    def answer(self, cid: str, yes: bool) -> None:
        fut = self._confirms.get(cid)
        if fut is not None and not fut.done():
            fut.get_loop().call_soon_threadsafe(lambda: fut.done() or fut.set_result(bool(yes)))


class Api:
    """Methods the console page calls as window.pywebview.api.* (run on pywebview worker threads)."""

    def __init__(self, app: "HudApp"):
        self._app = app

    def ready(self) -> dict:
        self._app.page_ready.set()
        return self._app.initial_state()

    def submit(self, text: str) -> None:
        self._app.submit(text)

    def confirm_answer(self, cid: str, yes: bool) -> None:
        self._app.ui.answer(cid, yes)

    def voice(self) -> None:
        self._app.on_hotkey()  # the console's mic button

    def shape(self, shapes: list, dpr: float) -> None:
        try:
            self._app.set_console_shape(shapes, dpr)  # the bubbles' boxes: the window is cut to them
        except Exception:
            log.exception("shaping the console failed")

    def collapse(self) -> None:
        self._app.hide_console()

    def quit(self) -> None:
        self._app.quit()


class HudApp:
    def __init__(self):
        self.cfg = load_config()
        self.scale = user32.GetDpiForSystem() / 96
        self.ui = HudUI(self)
        self.api = Api(self)
        self.page_ready = threading.Event()
        self.core: Core | None = None
        self.core_error: str | None = None
        self.console_open = False
        self.window: webview.Window | None = None
        self._styled = False
        self._sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-js")
        self._console_lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue[tuple[str, bool] | None] | None = None
        self.spoken_turn = False             # the command being handled came from the microphone
        self._busy_state = "idle"            # what the assistant is doing (routing, action, ...)
        self._voice_state: str | None = None  # listening / transcribing / speaking
        self._voice_turn: Future | None = None
        self._bubble_kind = ""
        self.hotkey = GlobalHotkey(self.cfg.ui.hotkey, self.on_hotkey)
        self._anchor = "top"
        self.orb = OrbWidget(self.cfg.ui.orb_size, DATA_DIR / "ui_state.json",
                             on_click=self.toggle_console, on_quit=self.quit, on_top=self.cfg.ui.orb_on_top,
                             level=self._voice_level)
        self.bubble = Bubble(self.orb.rect) if self.cfg.voice.bubble else None

    # ------------------------------------------------------------------ assistant loop

    def _core_thread(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._core_main())

    async def _core_main(self) -> None:
        self.queue = asyncio.Queue()
        try:
            self.core = await build_core(self.ui)
            if self.core.voice:
                self.core.voice.on_event = self._on_voice_event
            self.core.warm_up()
            log.info("core ready after %.1fs: %s", time.perf_counter() - self._t0, self.core.status_line())
        except Exception as e:
            log.exception("core failed to start")
            self.core_error = f"{type(e).__name__}: {e}"
        self.send({"type": "status", **self.initial_state()})
        while True:
            item = await self.queue.get()
            if item is None:
                break
            text, self.spoken_turn = item
            try:
                # A command may never block the ones queued behind it (a hung tool once froze Jev).
                await asyncio.wait_for(self.core.assistant.handle(text, spoken=self.spoken_turn),
                                       self.cfg.llm.command_timeout_seconds)
            except TimeoutError:
                log.warning("command timed out after %ss: %r", self.cfg.llm.command_timeout_seconds, text)
                self.orb_state("error")
                self.ui.say("That was taking too long, so I stopped it. Try asking again.")
                self.ui.event("done", total_ms=0)
            except Exception as e:
                log.exception("command failed")
                self.orb_state("error")
                self.ui.say(f"Something went wrong: {type(e).__name__}: {e}")
                self.ui.event("done", total_ms=0)
            finally:
                self.spoken_turn = False
        if self.core:
            await self.core.close()

    def submit(self, text: str, spoken: bool = False) -> None:
        text = (text or "").strip()
        if not text:
            return
        if text.lower() in QUIT_WORDS:
            self.quit()
            return
        if self.core is None:
            self.ui.say(self.core_error or "Still starting up — one moment.")
            return
        self.orb_state("routing")
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (text, spoken))

    def initial_state(self) -> dict:
        state = {"hotkey": self.cfg.ui.hotkey, "hotkey_error": self.hotkey.error,
                 "auto_hide_seconds": self.cfg.ui.auto_hide_seconds}
        if self.core:
            state.update(self.core.status())
        if self.core_error:
            state["error"] = self.core_error
        return state

    # ------------------------------------------------------------------ orb + voice

    def orb_state(self, state: str) -> None:
        """What the assistant is doing; the orb shows it unless the voice loop has priority."""
        self._busy_state = state
        self._refresh_orb()

    def _refresh_orb(self) -> None:
        voice, busy = self._voice_state, self._busy_state
        if voice in ("listening", "transcribing"):
            state = voice          # the user is talking: that's what matters most
        elif busy != "idle":
            state = busy
        else:
            state = voice or "idle"
        self.orb.set_state(state)

    def on_hotkey(self) -> None:
        """Voice. Pressed while listening: cancel. While a reply is being spoken: stop it and listen."""
        voice = self.core.voice if self.core else None
        if voice is None or not voice.can_listen:
            self._voice_state = "listening"
            self._refresh_orb()
            threading.Timer(1.2, self._voice_unavailable).start()
            return
        if self._voice_turn is not None and not self._voice_turn.done():
            self._voice_turn.cancel()  # cancels the listening task in the core loop
            return
        self._voice_turn = asyncio.run_coroutine_threadsafe(self._listen(), self.loop)

    async def _listen(self) -> None:
        try:
            text = await self.core.voice.listen()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("listening failed")
            self._on_voice_event("error", "Couldn't listen. Is a microphone connected?")
            return
        if text:
            self.send({"type": "voice", "text": text})  # into the console transcript, unopened
            self.submit(text, spoken=True)

    def speak(self, text: str) -> None:
        """Replies are spoken only for voice commands (unless [voice] speak_typed is on)."""
        voice = self.core.voice if self.core else None
        if voice and (self.spoken_turn or self.cfg.voice.speak_typed):
            if self._voice_state is None:  # keep the orb busy while the first sentence synthesizes
                self._voice_state = "speaking"
                self._refresh_orb()
            self.loop.call_soon_threadsafe(voice.say, text)

    def acknowledge(self, text: str) -> None:
        """A voice command that just did something (play, pause, volume, open...): the action is
        the answer, so no spoken reply — a short caption, and other apps' volume comes back now
        instead of waiting for a reply that isn't coming."""
        voice = self.core.voice if self.core else None
        if not (voice and self.spoken_turn):
            return
        self.loop.call_soon_threadsafe(voice.release)
        if self.bubble is not None and not self.console_open:
            self._bubble(text, "done")
            self.bubble.hide(2.5)

    def _voice_level(self) -> float | None:
        voice = self.core.voice if self.core else None
        return voice.level if voice else None

    def _on_voice_event(self, kind: str, text: str) -> None:
        if kind != "partial":
            log.info("voice: %s %s", kind, text[:80])
        if kind in ("listening", "transcribing", "speaking"):
            self._voice_state = kind
        elif kind == "idle":
            self._voice_state = None
        self._refresh_orb()
        self.send({"type": "voice_state", "state": kind, "text": text})  # the console's live bubble
        if kind == "listening":
            threading.Thread(target=self._send_levels, name="jev-levels", daemon=True).start()
        if self.bubble is None:
            return
        if self.console_open and kind in ("listening", "partial", "speaking"):
            self.bubble.hide(0)  # the console's own live bubble shows it
            return
        if kind == "listening":
            self._bubble("Listening…", "listening")
        elif kind == "partial":
            self._bubble(text, "heard")
        elif kind == "speaking":
            self._bubble(text, "reply")
        elif kind == "error":
            self._bubble(text, "error")
            self.bubble.hide(3.0)
        elif kind == "idle":
            # a transcript or reply stays readable for a moment; a bare "Listening..." just goes
            self.bubble.hide(1.5 if self._bubble_kind in ("heard", "reply") else 0)

    BUBBLE_STYLE = {"listening": ("Listening", "listening"), "heard": ("Listening", "listening"),
                    "reply": ("Responding", "speaking"), "error": ("Error", "error"), "done": ("Done", "action")}

    def _bubble(self, text: str, kind: str) -> None:
        self._bubble_kind = kind
        label, stage = self.BUBBLE_STYLE[kind]
        self.bubble.show(text, label, stage)

    def _send_levels(self) -> None:
        """The console's level bars follow your real voice while it listens (and is open)."""
        while self._voice_state == "listening":
            level = self._voice_level()
            if self.console_open and level is not None:
                self.send({"type": "level", "v": round(level, 3)})
            time.sleep(1 / LEVEL_HZ)

    def _voice_unavailable(self) -> None:
        self._voice_state = None
        self._refresh_orb()
        self.open_console(focus=True)
        if not self.cfg.voice.enabled:
            why = "Voice is turned off ([voice] enabled = false in config.toml)."
        elif self.core is None:
            why = "Still starting up. Try again in a moment."
        else:
            why = ("Voice isn't available: add GEMINI_API_KEY (or SARVAM_API_KEY) to .env, or set up "
                   "offline voice (README → Voice). Type your command for now.")
        self.send({"type": "notify", "text": why})

    # ------------------------------------------------------------------ console window

    def send(self, event: dict) -> None:
        payload = json.dumps(event, ensure_ascii=False)

        def push():
            if not self.page_ready.wait(30) or self.window is None:
                return
            try:
                self.window.evaluate_js(f"window.hud && window.hud.receive({payload})")
            except Exception:
                log.exception("evaluate_js failed")

        self._sender.submit(push)

    def _hwnd(self) -> int:
        return user32.FindWindowW(None, TITLE)

    def _console_geometry(self) -> tuple[int, int, int, int, str]:
        """The stream hangs under the orb, its right edge ORB_INSET inside the orb window's (mirrored
        when the orb is on the left half). With no room below it goes above and grows upwards.
        Returns physical pixels and the anchor ("top" / "bottom") for the page."""
        s = self.scale
        w, h = round(CONSOLE_SIZE[0] * s), round(CONSOLE_SIZE[1] * s)
        gap, inset = round(GAP * s), round(ORB_INSET * s)
        ox, oy, osz = self.orb.rect()
        work = wintypes.RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0)
        on_right_half = ox + osz // 2 > (work.left + work.right) // 2
        x = ox + osz - inset - w if on_right_half else ox + inset
        below = oy + osz + gap + round(200 * s) <= work.bottom   # room for at least a few bubbles
        if below:
            y, anchor = oy + osz + gap, "top"
            h = min(h, work.bottom - gap - y)
        else:
            anchor = "bottom"
            h = min(h, oy - gap - work.top - gap)
            y = oy - gap - h
        x = max(work.left + gap, min(x, work.right - w - gap))
        return x, y, w, h, anchor

    def open_console(self, focus: bool = True) -> None:
        if self.window is None:
            return
        with self._console_lock:
            x, y, w, h, self._anchor = self._console_geometry()
            # pywebview works in logical pixels and scales by the system DPI itself
            self.window.resize(round(w / self.scale), round(h / self.scale))
            self.window.move(round(x / self.scale), round(y / self.scale))
            hwnd = self._hwnd()
            if hwnd:
                self._style(hwnd)
                if not self.console_open:  # nothing shows until the page reports its bubbles
                    user32.SetWindowRgn(hwnd, gdi32.CreateRectRgn(0, 0, 0, 0), True)
            self.window.show()
            self.console_open = True
            self.send({"type": "mode", "mode": "show", "focus": focus, "anchor": self._anchor})
        if focus and hwnd:
            user32.SetForegroundWindow(hwnd)

    def _style(self, hwnd: int) -> None:
        """Once: keep the console out of the taskbar and Alt+Tab. (Not layered: WebView2 ignores a
        window region on a layered window, and the region is what makes the bubbles float.)"""
        if not self._styled:
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
            self._styled = True
        # pywebview gives dark-mode windows a Mica backdrop, which DWM paints over the whole window
        # rect regardless of the region (a grey slab behind the bubbles). Backdrop: none.
        # ponytail: re-applied on each open because pywebview resets it when the Windows theme changes
        none = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(none), 4)

    def set_console_shape(self, shapes: list, dpr: float) -> None:
        """Cut the console window to the union of its bubbles: [x, y, w, h, radius, sharp corner]
        in CSS px. Everything else is clipped away, so the desktop shows (and is clickable) between
        bubbles. The design's 6 px corners are drawn square (regions can only do one radius)."""
        hwnd = self._hwnd()
        if not hwnd:
            return
        region = gdi32.CreateRectRgn(0, 0, 0, 0)
        for x, y, w, h, r, sharp in shapes:
            x, y, w, h, r = (round(v * dpr) for v in (x, y, w, h, r))
            part = gdi32.CreateRoundRectRgn(x, y, x + w + 1, y + h + 1, 2 * r, 2 * r)
            gdi32.CombineRgn(region, region, part, RGN_OR)
            gdi32.DeleteObject(part)
            if sharp in ("bl", "br"):
                cx = x if sharp == "bl" else x + w - r
                corner = gdi32.CreateRectRgn(cx, y + h - r, cx + r, y + h)
                gdi32.CombineRgn(region, region, corner, RGN_OR)
                gdi32.DeleteObject(corner)
        user32.SetWindowRgn(hwnd, region, True)  # Windows owns the region now

    def hide_console(self) -> None:
        if self.window is None or not self.console_open:
            return
        self.console_open = False
        self.send({"type": "mode", "mode": "hide"})
        self.window.hide()

    def toggle_console(self) -> None:
        if self.console_open:
            self.hide_console()
        else:
            self.open_console(focus=True)

    # ------------------------------------------------------------------ lifecycle

    def quit(self) -> None:
        self.hotkey.stop()
        self.orb.stop()
        if self.bubble:
            self.bubble.stop()
        if self.queue is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
        if self.window is not None:
            threading.Timer(0.3, self.window.destroy).start()

    def run(self) -> None:
        self._t0 = time.perf_counter()
        threading.Thread(target=self._core_thread, name="jev-core", daemon=True).start()
        self.orb.start()
        if self.bubble:
            self.bubble.start()
        log.info("orb shown after %.1fs", time.perf_counter() - self._t0)
        if not self.hotkey.start():
            log.warning("hotkey: %s", self.hotkey.error)
        self.window = webview.create_window(
            TITLE, url=str(WEB), js_api=self.api, width=CONSOLE_SIZE[0], height=CONSOLE_SIZE[1],
            frameless=True, on_top=True, easy_drag=False, resizable=False, min_size=(64, 64),
            shadow=False, background_color="#05080c", hidden=True)
        (DATA_DIR / "webview").mkdir(parents=True, exist_ok=True)
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(DATA_DIR / "webview"))
        # console window destroyed -> shut everything down
        self.hotkey.stop()
        self.orb.stop()
        if self.queue is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)


def _setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DATA_DIR / "jev.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.stdout is None or sys.stderr is None:  # pythonw has no console
        stream = open(DATA_DIR / "jev.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream


def run() -> None:
    _setup_logging()
    # One copy at a time: a second one would fail to register the hotkey anyway.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, "Local\\JevDesktopHUD")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        log.info("already running; exiting")
        return
    HudApp().run()
