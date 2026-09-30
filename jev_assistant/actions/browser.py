"""The assistant's web browser, in one of two modes (config: browser.mode).

real (default) – everyday commands (search, open a site, play a YouTube video, email drafts, the
    Spotify web fallback) open as a new tab in your own Google Chrome, where you're already logged
    in. Chrome is launched with the URL (it hands the tab to the running Chrome, or starts it), so
    nothing is automated: Jev can open pages there but can't click, read or type in them. YouTube
    videos are resolved to a watch URL without a browser (see find_youtube_video).
automation – everything runs in a Playwright-driven Chrome with its own persistent profile
    (browser.profile_dir). It can click Play, press Send, and read pages, but you have to log in
    to sites in that window once.

The deep LLM's generic page tools (snapshot/read/click/type/...) always need automation, so they
use the Playwright profile in both modes. Chrome 136+ refuses remote debugging on the default
user-data dir, and copying that profile elsewhere breaks its logins (app-bound cookie
encryption), so Playwright can't drive your real profile. The Playwright browser is only started
when one of those tools (or an automation-mode command) first needs it.

Run `python -m jev_assistant.actions.browser` to list your Chrome profiles for
browser.profile_directory.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import re
import subprocess
import time
import winreg
from ctypes import wintypes
from pathlib import Path
from urllib.parse import quote, quote_plus, urlencode, urlparse

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

from ..config import BrowserConfig
from . import windows

log = logging.getLogger("jev.browser")

SNAPSHOT_LIMIT = 15_000
TEXT_LIMIT = 20_000

CHROME_USER_DATA = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
CHROME_CANDIDATES = [
    Path(os.environ.get(var, "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
]
YT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
YT_COOKIES = {"SOCS": "CAI"}  # declines the EU consent interstitial, which has no results in it
YT_SKIP = {"adSlotRenderer", "promotedVideoRenderer", "searchPyvRenderer", "reelShelfRenderer",
           "gridShelfViewModel", "shortsLockupViewModel", "secondaryContents"}
VK_F = 0x46
ASFW_ANY = -1
DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


class Browser:
    def __init__(self, cfg: BrowserConfig):
        self.cfg = cfg
        self._pw = None
        self._ctx: BrowserContext | None = None
        self._lock = asyncio.Lock()
        self._tabs: dict[str, Page] = {}  # named tabs: "music", "mail"
        self._http: httpx.AsyncClient | None = None
        self._chrome: str | None = None
        self._jobs: set[asyncio.Task] = set()  # e.g. pending fullscreen key press

    @property
    def real(self) -> bool:
        """Everyday commands go to the user's own Chrome (not automated)."""
        return self.cfg.mode.strip().lower() != "automation"

    # ------------------------------------------------------------------ lifecycle

    async def context(self) -> BrowserContext:
        async with self._lock:
            if self._ctx is not None:
                return self._ctx
            if self._pw is None:
                self._pw = await async_playwright().start()
            self.cfg.profile_path.mkdir(parents=True, exist_ok=True)
            kwargs = dict(
                user_data_dir=str(self.cfg.profile_path),
                headless=self.cfg.headless,
                no_viewport=True,
                args=["--start-maximized"],
            )
            try:
                ctx = await self._pw.chromium.launch_persistent_context(
                    channel=self.cfg.channel or None, **kwargs)
            except PlaywrightError:
                if not self.cfg.channel:
                    raise
                # Installed browser not found — fall back to Playwright's Chromium
                # (needs a one-time `playwright install chromium`).
                ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
            ctx.on("close", lambda _: self._forget())
            self._ctx = ctx
            return ctx

    def _forget(self) -> None:
        self._ctx = None
        self._tabs.clear()

    async def start(self) -> None:
        if self.real:
            return  # real-mode commands never need Playwright; the deep agent starts it on demand
        try:
            await self.context()
        except Exception:
            pass  # surfaced on first real use

    async def close(self) -> None:
        if self._jobs:  # let a pending fullscreen press finish (one-shot CLI exits right away)
            await asyncio.wait(self._jobs, timeout=15)
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._ctx is not None:
            try:
                await self._ctx.close()
            except PlaywrightError:
                pass
        if self._pw is not None:
            await self._pw.stop()
        self._ctx = self._pw = None

    # ------------------------------------------------------------------ real Chrome

    def chrome_exe(self) -> str | None:
        if self._chrome is None:
            found = [Path(self.cfg.chrome_path)] if self.cfg.chrome_path else list(CHROME_CANDIDATES)
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as k:
                        found.append(Path(winreg.QueryValue(k, None)))
                except OSError:
                    pass
            self._chrome = next((str(p) for p in found if p.is_file()), "")
        return self._chrome or None

    def _profile_arg(self) -> list[str]:
        """--profile-directory for the configured profile, by folder or display name.

        An unknown name is dropped rather than passed on: Chrome would silently create a new,
        empty profile for it.
        """
        want = self.cfg.profile_directory.strip()
        if not want:
            return []  # Chrome picks its last-used profile
        for directory, name, _ in chrome_profiles():
            if want.lower() in (directory.lower(), name.lower()):
                return [f"--profile-directory={directory}"]
        log.warning("browser.profile_directory %r not found in Chrome's Local State; using the last-used profile", want)
        return []

    def open_in_chrome(self, url: str) -> None:
        """Open a URL as a new tab in the user's own Chrome (starting it if needed)."""
        if urlparse(url).scheme.lower() not in ("http", "https"):
            raise ValueError(f"Refusing to open a non-web URL: {url}")
        exe = self.chrome_exe()
        # Let Chrome take the foreground: Windows only allows it when the foreground app says so.
        windows.user32.AllowSetForegroundWindow(ASFW_ANY)
        if exe is None:
            log.warning("Google Chrome not found; opening %s in the default browser", url)
            os.startfile(url)
            return
        subprocess.Popen([exe, *self._profile_arg(), url], creationflags=DETACHED, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:  # kept open so repeat lookups skip the TLS handshake
            self._http = httpx.AsyncClient(headers=YT_HEADERS, cookies=YT_COOKIES,
                                           follow_redirects=True, timeout=8)
        return self._http

    async def find_youtube_video(self, query: str) -> tuple[str, str] | None:
        """(video id, title) of YouTube's top search result, without a browser (~1 s)."""
        r = await self._client().get("https://www.youtube.com/results", params={"search_query": query})
        r.raise_for_status()
        return first_video(initial_data(r.text))

    def _background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    # ------------------------------------------------------------------ pages (automation)

    async def page(self) -> Page:
        """The tab the user is most likely looking at (last opened)."""
        ctx = await self.context()
        pages = [p for p in ctx.pages if not p.is_closed()]
        return pages[-1] if pages else await ctx.new_page()

    async def _tab(self, name: str) -> Page:
        page = self._tabs.get(name)
        if page is None or page.is_closed():
            ctx = await self.context()
            # Reuse the blank start tab if that's all there is.
            blank = [p for p in ctx.pages if p.url in ("about:blank", "chrome://newtab/", "edge://newtab/")]
            page = blank[0] if blank and blank[0] not in self._tabs.values() else await ctx.new_page()
            self._tabs[name] = page
        return page

    async def goto(self, url: str, tab: str | None = None, new_tab: bool = False) -> Page:
        if tab:
            page = await self._tab(tab)
        elif new_tab:
            page = await (await self.context()).new_page()
        else:
            page = await self.page()
        await page.goto(url, wait_until="domcontentloaded")
        await page.bring_to_front()
        return page

    # ------------------------------------------------------------------ fast paths

    async def search(self, query: str) -> str:
        url = f"https://www.google.com/search?q={quote_plus(query)}"
        if self.real:
            self.open_in_chrome(url)
        else:
            await self.goto(url, new_tab=True)
        return f"Searching for “{query}”."

    async def open_site(self, url: str, label: str = "") -> str:
        if self.real:
            self.open_in_chrome(url)
            # A DuckDuckGo "\" (I'm feeling lucky) URL isn't worth reading out; the site name is.
            return f"Opening {label or urlparse(url).netloc.removeprefix('www.')}."
        page = await self.goto(url, new_tab=True)
        return f"Opened {page.url}."

    async def play_youtube(self, query: str, fullscreen: bool = False, tab: str = "media") -> str:
        """Play the top YouTube video for a query (skips Shorts, ads and upcoming premieres)."""
        if self.real:
            return await self._play_youtube_real(query, fullscreen)
        page = await self.goto(f"https://www.youtube.com/results?search_query={quote_plus(query)}", tab=tab)
        first = page.locator("ytd-video-renderer a#video-title").first
        try:
            await first.wait_for(timeout=8000)
            title = (await first.get_attribute("title")) or query
            await first.click()
        except PlaywrightError:
            return f"Opened YouTube results for “{query}” (couldn't auto-play the first one)."
        if fullscreen:
            try:
                await page.locator("#movie_player").wait_for(timeout=8000)
                await page.keyboard.press("f")
            except PlaywrightError:
                pass
        return f"Playing “{title}” on YouTube."

    async def _play_youtube_real(self, query: str, fullscreen: bool) -> str:
        t0 = time.perf_counter()
        try:
            found = await self.find_youtube_video(query)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("YouTube lookup failed for %r: %s", query, e)
            found = None
        if found is None:
            self.open_in_chrome(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
            return f"Opened YouTube results for “{query}”."
        video_id, title = found
        self.open_in_chrome(f"https://www.youtube.com/watch?v={video_id}")
        log.info("YouTube %r -> %s in %.0f ms", query, video_id, (time.perf_counter() - t0) * 1000)
        if fullscreen:  # no URL parameter does this; press YouTube's F shortcut once the tab is up
            self._background(_youtube_fullscreen(title))
        return f"Playing “{title}” on YouTube."

    async def play_spotify(self, query: str) -> str:
        url = f"https://open.spotify.com/search/{quote(query)}"
        if self.real:
            self.open_in_chrome(url)  # can't press Play without automation
            return f"Opened Spotify search for “{query}” in Chrome — pick a result to play it."
        page = await self.goto(url, tab="media")
        for selector in ('[data-testid="top-result-card"] [data-testid="play-button"]',
                         '[data-testid="tracklist-row"]'):
            target = page.locator(selector).first
            try:
                await target.wait_for(timeout=8000)
                if "tracklist-row" in selector:
                    await target.dblclick()
                else:
                    await target.click()
                return f"Playing “{query}” on Spotify."
            except PlaywrightError:
                continue
        return f"Opened Spotify results for “{query}” — log in to the Spotify tab once so playback works."

    async def compose_email(self, provider: str, to: str, subject: str, body: str,
                            outlook_base: str = "https://outlook.live.com") -> Page | None:
        """Open a pre-filled draft. Returns the automation page (for click_send), or None in real mode."""
        if provider == "outlook":
            params = urlencode({"to": to, "subject": subject, "body": body}, quote_via=quote)
            url = f"{outlook_base}/mail/0/deeplink/compose?{params}"
        else:
            params = urlencode({"view": "cm", "fs": "1", "to": to, "su": subject, "body": body}, quote_via=quote)
            url = f"https://mail.google.com/mail/?{params}"
        if self.real:
            self.open_in_chrome(url)
            return None
        return await self.goto(url, tab="mail")

    async def click_send(self, page: Page) -> bool:
        send = page.get_by_role("button", name=re.compile(r"^send\b", re.I)).first
        try:
            await send.click(timeout=15000)
        except PlaywrightError:
            return False
        try:  # Gmail/Outlook both show a transient confirmation
            await page.get_by_text(re.compile(r"message sent|sent", re.I)).first.wait_for(timeout=5000)
        except PlaywrightError:
            pass
        return True

    # ------------------------------------------------------------------ generic tools for the LLM (automation)

    async def snapshot(self) -> str:
        page = await self.page()
        try:
            tree = await page.locator("body").aria_snapshot(timeout=5000)
        except PlaywrightError as e:
            tree = f"(accessibility snapshot failed: {e.message})"
        header = f"URL: {page.url}\nTitle: {await page.title()}\n\n"
        return header + _clip(tree, SNAPSHOT_LIMIT)

    async def read_text(self) -> str:
        page = await self.page()
        text = await page.locator("body").inner_text(timeout=5000)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return f"URL: {page.url}\nTitle: {await page.title()}\n\n" + _clip(text, TEXT_LIMIT)

    async def click(self, name: str, role: str = "") -> str:
        page = await self.page()
        loc = page.get_by_role(role, name=name) if role else page.get_by_text(name)
        try:
            await loc.first.click(timeout=5000)
        except PlaywrightError:
            # Retry with the other strategy before giving up.
            alt = page.get_by_text(name) if role else page.get_by_role("button", name=name)
            await alt.first.click(timeout=5000)
        await page.wait_for_load_state("domcontentloaded")
        return f"Clicked “{name}”. Now at {page.url}"

    async def type_text(self, name: str, text: str, role: str = "", submit: bool = False) -> str:
        page = await self.page()
        loc = page.get_by_role(role or "textbox", name=name).first
        try:
            await loc.fill(text, timeout=5000)
        except PlaywrightError:
            loc = page.get_by_placeholder(name).first
            await loc.fill(text, timeout=5000)
        if submit:
            await loc.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
        return f"Typed into “{name}”." + (f" Submitted; now at {page.url}" if submit else "")

    async def press(self, key: str) -> str:
        page = await self.page()
        await page.keyboard.press(key)
        return f"Pressed {key}."

    async def back(self) -> str:
        page = await self.page()
        await page.go_back(wait_until="domcontentloaded")
        return f"Went back to {page.url}"


# --------------------------------------------------------------------------- YouTube lookup

def initial_data(html: str) -> dict:
    """The ytInitialData JSON embedded in a YouTube page."""
    m = re.search(r"(?:var ytInitialData|window\[\"ytInitialData\"\])\s*=\s*", html)
    if not m:
        raise ValueError("ytInitialData not found (YouTube page layout changed?)")
    data, _ = json.JSONDecoder().raw_decode(html, m.end())
    return data


def first_video(data: dict) -> tuple[str, str] | None:
    """Top playable result (live streams count; Shorts, ads, premieres don't).

    Ranked results come first; channel shelves ("Latest from …") only if there are none, since they
    can sit above the ranked list and aren't what "the top result" means.
    """
    root = data.get("contents", {}).get("twoColumnSearchResultsRenderer", {}).get("primaryContents", data)
    return _first_video(root, YT_SKIP | {"shelfRenderer"}) or _first_video(root, YT_SKIP)


def _first_video(root, skip: set[str]) -> tuple[str, str] | None:
    for kind, item in _walk(root, skip):
        if kind == "videoRenderer":
            url = item.get("navigationEndpoint", {}).get("commandMetadata", {}) \
                      .get("webCommandMetadata", {}).get("url", "")
            if "/shorts/" in url or "upcomingEventData" in item or not item.get("videoId"):
                continue
            title = "".join(r.get("text", "") for r in item.get("title", {}).get("runs", []))
            return item["videoId"], title or item["videoId"]
        # Newer layout YouTube is rolling out across surfaces.
        if item.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO" and item.get("contentId"):
            title = item.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title", {}).get("content")
            return item["contentId"], title or item["contentId"]
    return None


def _walk(node, skip: set[str]):
    """Depth-first (kind, renderer) pairs in document order, not descending into skipped kinds."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in skip:
                continue
            if key in ("videoRenderer", "lockupViewModel") and isinstance(value, dict):
                yield key, value
                continue
            yield from _walk(value, skip)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, skip)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_u = windows.user32
_u.GetForegroundWindow.restype = _u.MonitorFromWindow.restype = wintypes.HWND
_u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_u.GetMonitorInfoW.argtypes = [wintypes.HWND, ctypes.POINTER(_MONITORINFO)]
_u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


def _video_window(needle: str) -> int | None:
    """The foreground window if it's Chrome showing this YouTube video, else None."""
    hwnd = windows.user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    windows.user32.GetWindowTextW(hwnd, buf, 512)
    text = buf.value.casefold()
    if not hwnd or needle not in text or "youtube" not in text:
        return None
    pid = wintypes.DWORD()
    windows.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd if windows._exe_name(pid.value).lower() == "chrome" else None


def _covers_monitor(hwnd: int) -> bool:
    rect, info = wintypes.RECT(), _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    windows.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    windows.user32.GetMonitorInfoW(windows.user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
    m = info.rcMonitor
    return (rect.left, rect.top, rect.right, rect.bottom) == (m.left, m.top, m.right, m.bottom)


async def _youtube_fullscreen(title: str, timeout: float = 15.0) -> bool:
    """Press YouTube's F shortcut in the video's tab until the window is fullscreen.

    Keys only go to the foreground window, and only while it's Chrome showing this video. The
    title appears (server HTML) seconds before the player listens for keys, and a press that
    arrives too early is dropped, so it checks the window and retries rather than guessing a delay.
    """
    needle = title[:25].casefold()
    deadline = time.monotonic() + timeout
    presses = 0
    while time.monotonic() < deadline:
        hwnd = _video_window(needle)
        if hwnd and _covers_monitor(hwnd):
            log.info("YouTube fullscreen after %d key press(es)", presses)
            return True
        if hwnd:
            if presses == 0:
                await asyncio.sleep(1.0)  # give the player a head start
            if _video_window(needle) == hwnd:
                windows.tap_key(VK_F)
                presses += 1
            await asyncio.sleep(1.0)
        else:
            await asyncio.sleep(0.25)
    log.info("Fullscreen not reached (%d key presses; the video tab may not be in front)", presses)
    return False


# --------------------------------------------------------------------------- Chrome profiles

def chrome_profiles() -> list[tuple[str, str, bool]]:
    """(profile folder, display name, signed in) for each Chrome profile. No emails or cookies."""
    try:
        state = json.loads((CHROME_USER_DATA / "Local State").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cache = state.get("profile", {}).get("info_cache", {})
    return [(d, info.get("name", d), bool(info.get("user_name"))) for d, info in cache.items()]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


if __name__ == "__main__":  # python -m jev_assistant.actions.browser
    try:
        last = json.loads((CHROME_USER_DATA / "Local State").read_text(encoding="utf-8"))["profile"]["last_used"]
    except (OSError, ValueError, KeyError):
        last = ""
    print('Chrome profiles (use the folder or the name for browser.profile_directory in config.toml):')
    for folder, name, signed_in in chrome_profiles():
        print(f"  {folder!r:14} {name!r:24} {'signed in' if signed_in else 'not signed in'}"
              f"{'   <- last used' if folder == last else ''}")
