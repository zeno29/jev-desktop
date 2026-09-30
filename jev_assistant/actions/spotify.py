"""Spotify desktop playback through the Spotify Web API (requires Spotify Premium).

Flow for "play X on spotify":
  1. search the Web API for tracks / artists / albums / playlists matching X
  2. a chooser (Jev) picks which result the user meant — selection, never generation
  3. make sure the desktop app is running as a Spotify Connect device (launch it if needed)
  4. start playback on that device, then confirm it actually started

Playback is always sent as a *context* (album/playlist/artist). As of Sept 2026 the desktop client
answers PUT /me/player/play {"uris": [track]} with 204 but silently plays nothing, while
{"context_uri": album, "offset": {"uri": track}} starts within ~1 s. Because a 204 proves nothing,
we poll /me/player (and the app's window title, "Artist - Song" while playing) to confirm; if the
API route still doesn't start, opening a spotify:track: URI in the desktop app autoplays it.

One-time setup: create an app at https://developer.spotify.com/dashboard with redirect URI
http://127.0.0.1:8888/callback, put its Client ID in .env as SPOTIFY_CLIENT_ID, then run
`python -m jev_assistant --spotify-login` (or just ask it to play something; it will prompt).
Uses Authorization Code + PKCE, so no client secret is needed.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import json
import os
import platform
import re
import secrets
import time
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from rapidfuzz import fuzz

from .windows import WNDENUMPROC, _exe_name, user32  # argtypes are declared there

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REDIRECT_PORT = 8888
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
SCOPES = "user-read-playback-state user-modify-playback-state"
DEVICE_WAIT_SECONDS = 20
START_WAIT_SECONDS = 5      # how long to wait for a play command to show up as playing
GENERIC_MUSIC_QUERY = "top hits"  # "play some music" with nothing to resume
IDLE_TITLES = {"spotify", "spotify premium", "spotify free"}  # desktop window title when not playing


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


class SpotifyError(Exception):
    pass


@dataclass
class Result:
    kind: str   # track | artist | album | playlist
    name: str
    by: str     # artist(s) or playlist owner
    uri: str
    album_uri: str = ""  # tracks only: played as the context, see module docstring

    @property
    def label(self) -> str:
        return f"{self.kind}: {self.name}" + (f" — {self.by}" if self.by else "")


# chooser(query, {label: description}) -> chosen label, or None if nothing fits / unavailable
Chooser = Callable[[str, dict[str, str]], Awaitable[str | None]]


class Spotify:
    def __init__(self, client_id: str | None, token_path: Path, launch_app: Callable[[], bool],
                 notify: Callable[[str], None]):
        self.client_id = client_id
        self.token_path = token_path
        self.launch_app = launch_app  # starts the desktop app; False if it isn't installed
        self.notify = notify
        self._token: dict | None = None
        self._http = httpx.AsyncClient(timeout=10)

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth (PKCE)

    async def login(self) -> None:
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        got: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            line = (await reader.readline()).decode(errors="replace")
            parts = line.split(" ")
            params = parse_qs(urlparse(parts[1]).query) if len(parts) > 1 else {}
            ok = params.get("state", [""])[0] == state and "code" in params
            body = ("Spotify is connected to Jev. You can close this tab." if ok
                    else "Spotify login failed or was cancelled. You can close this tab.")
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                         f"Content-Length: {len(body.encode())}\r\nConnection: close\r\n\r\n{body}".encode())
            await writer.drain()
            writer.close()
            if "/callback" in line and not got.done():
                got.set_result(params)

        server = await asyncio.start_server(handle, "127.0.0.1", REDIRECT_PORT)
        try:
            url = AUTH_URL + "?" + urlencode({
                "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
                "code_challenge_method": "S256", "code_challenge": challenge, "state": state, "scope": SCOPES})
            self.notify("Opening Spotify in your browser — approve access to let Jev control playback.")
            webbrowser.open(url)
            params = await asyncio.wait_for(got, timeout=180)
        except asyncio.TimeoutError:
            raise SpotifyError("Spotify login timed out.")
        finally:
            server.close()

        if params.get("state", [""])[0] != state or "code" not in params:
            raise SpotifyError("Spotify login was cancelled: " + params.get("error", ["unknown error"])[0])
        await self._token_request({"grant_type": "authorization_code", "code": params["code"][0],
                                   "redirect_uri": REDIRECT_URI, "client_id": self.client_id,
                                   "code_verifier": verifier})

    async def _token_request(self, data: dict) -> None:
        r = await self._http.post(TOKEN_URL, data=data)
        if r.status_code != 200:
            raise SpotifyError(f"Spotify token request failed ({r.status_code}): {r.text[:200]}")
        tok = r.json()
        previous_refresh = (self._token or {}).get("refresh_token")
        tok["refresh_token"] = tok.get("refresh_token") or previous_refresh  # may not rotate
        tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
        self._token = tok
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(tok), encoding="utf-8")

    async def _access_token(self) -> str:
        if not self.configured:
            raise SpotifyError("SPOTIFY_CLIENT_ID isn't set in .env.")
        if self._token is None and self.token_path.exists():
            self._token = json.loads(self.token_path.read_text(encoding="utf-8"))
        if self._token is None:
            await self.login()
        elif time.time() >= self._token.get("expires_at", 0):
            try:
                await self._token_request({"grant_type": "refresh_token", "client_id": self.client_id,
                                           "refresh_token": self._token["refresh_token"]})
            except SpotifyError:
                self._token = None
                await self.login()
        return self._token["access_token"]

    async def _api(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        r = await self._http.request(method, API + path, headers=headers, **kw)
        if r.status_code == 401:  # token revoked/expired early: refresh once
            self._token["expires_at"] = 0
            headers = {"Authorization": f"Bearer {await self._access_token()}"}
            r = await self._http.request(method, API + path, headers=headers, **kw)
        return r

    # ------------------------------------------------------------------ search + choose

    async def search(self, query: str) -> list[Result]:
        r = await self._api("GET", "/search", params={"q": query, "type": "track,artist,album,playlist", "limit": 3})
        if r.status_code != 200:
            raise SpotifyError(f"Spotify search failed ({r.status_code}).")
        data = r.json()
        results: list[Result] = []
        for kind in ("track", "artist", "album", "playlist"):
            for item in (data.get(kind + "s") or {}).get("items") or []:
                if not item:  # search can return null playlist entries
                    continue
                if kind in ("track", "album"):
                    by = ", ".join(a["name"] for a in item.get("artists", []))
                elif kind == "playlist":
                    by = "playlist by " + ((item.get("owner") or {}).get("display_name") or "Spotify")
                else:
                    by = ""
                album_uri = (item.get("album") or {}).get("uri", "") if kind == "track" else ""
                results.append(Result(kind, item["name"], by, item["uri"], album_uri))
        return results

    @staticmethod
    def obvious_pick(query: str, results: list[Result]) -> Result | None:
        """The query names a track and its artist exactly ("believer by imagine dragons"), so there is
        nothing for Jev to judge. A bare name isn't enough: search also returns obscure artists and
        playlists called "Blinding Lights" or "lofi"."""
        q = _norm(query)
        hits = [r for r in results if r.kind == "track" and q == _norm(f"{r.name} by {r.by.split(', ')[0]}")]
        return min(hits, key=lambda r: r.by.count(","), default=None)  # the original over a remix/feat.

    @staticmethod
    def heuristic_pick(query: str, results: list[Result]) -> Result | None:
        """Fallback when Jev isn't available."""
        q = query.lower()
        for kind in ("playlist", "album"):
            if kind in q:
                return next((r for r in results if r.kind == kind), None)
        artists = [r for r in results if r.kind == "artist" and fuzz.ratio(r.name.lower(), q) >= 85]
        tracks = [r for r in results if r.kind == "track"]
        if tracks and fuzz.partial_ratio(tracks[0].name.lower(), q) >= 80:
            return tracks[0]
        return artists[0] if artists else (tracks[0] if tracks else (results[0] if results else None))

    # ------------------------------------------------------------------ devices + playback

    async def _device_id(self) -> str:
        def pick(devices: list[dict]) -> dict | None:
            computers = [d for d in devices if d.get("type") == "Computer" and not d.get("is_restricted")]
            # the desktop app registers under the PC's hostname: prefer this PC over other computers
            local = [d for d in computers if (d.get("name") or "").lower() == platform.node().lower()]
            active = [d for d in computers if d.get("is_active")]
            return (local or active or computers or [None])[0]

        r = await self._api("GET", "/me/player/devices")
        device = pick(r.json().get("devices", [])) if r.status_code == 200 else None
        if device is None:
            if not self.launch_app():
                raise SpotifyError("The Spotify desktop app isn't installed.")
            deadline = time.time() + DEVICE_WAIT_SECONDS
            while device is None and time.time() < deadline:
                await asyncio.sleep(1)
                r = await self._api("GET", "/me/player/devices")
                device = pick(r.json().get("devices", [])) if r.status_code == 200 else None
            if device is None:
                raise SpotifyError("Spotify opened but didn't come online in time — try again in a moment.")
        return device["id"]

    @staticmethod
    def _window_title() -> str:
        """Title of the desktop app's main window: "Artist - Song" while playing, "Spotify …" when idle.

        Includes hidden windows, since the app keeps updating its title while minimized to the tray;
        its helper windows ("GDI+ Window…", "Default IME") are skipped by the title shape."""
        titles: list[str] = []

        def callback(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if ((buf.value.lower() in IDLE_TITLES or " - " in buf.value)
                        and _exe_name(pid.value).lower() == "spotify"):
                    titles.append(buf.value)
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        return titles[0] if titles else ""

    async def _started(self, result: Result, title_before: str) -> bool:
        """Poll until `result` is audibly playing; a 204 from /me/player/play doesn't guarantee that."""
        deadline = time.time() + START_WAIT_SECONDS
        while time.time() < deadline:
            await asyncio.sleep(0.4)
            r = await self._api("GET", "/me/player")
            state = r.json() if r.status_code == 200 and r.text else {}
            if state.get("is_playing"):
                item = state.get("item") or {}
                # artist contexts come back with context=None, so also match the track's artists
                playing = {item.get("uri"), (item.get("linked_from") or {}).get("uri"),
                           (state.get("context") or {}).get("uri"),
                           *(a.get("uri") for a in item.get("artists") or [])}
                if result.uri in playing:
                    return True
            # window title flips to the new "Artist - Song" even if the API read lags or is empty
            title = await asyncio.to_thread(self._window_title)
            if title and title.lower() not in IDLE_TITLES and title != title_before:
                return True
        return False

    async def _api_play(self, result: Result, device_id: str | None = None) -> None:
        # always send a context: bare {"uris": [track]} is accepted (204) but never starts on desktop
        if result.kind == "track" and result.album_uri:
            body: dict = {"context_uri": result.album_uri, "offset": {"uri": result.uri}}
        elif result.kind == "track":
            body = {"uris": [result.uri]}
        else:
            body = {"context_uri": result.uri}
        r = await self._api("PUT", "/me/player/play", params={"device_id": device_id or await self._device_id()},
                            json=body)
        if r.status_code == 404:  # device went idle between listing and playing: retry once
            await asyncio.sleep(1.5)
            r = await self._api("PUT", "/me/player/play", params={"device_id": await self._device_id()}, json=body)
        if r.status_code == 403:
            try:
                reason = r.json()["error"].get("reason") or r.json()["error"].get("message", "")
            except (ValueError, KeyError, AttributeError):
                reason = r.text[:120]
            if reason == "PREMIUM_REQUIRED":
                raise SpotifyError("Spotify refused playback control — this needs Spotify Premium.")
            raise SpotifyError(f"Spotify refused playback control ({reason}).")
        if r.status_code not in (200, 202, 204):
            raise SpotifyError(f"Spotify couldn't start playback ({r.status_code}): {r.text[:200]}")

    async def play(self, result: Result, device_id: str | None = None) -> None:
        title_before = await asyncio.to_thread(self._window_title)
        await self._api_play(result, device_id)
        if await self._started(result, title_before):
            return
        # Web API route didn't take. Opening the URI in the desktop app launches/focuses it and
        # navigates there; for a track that also starts playback, for a context we send play again.
        os.startfile(result.uri)
        if result.kind != "track":
            await asyncio.sleep(1.5)
            await self._api_play(result)
        if not await self._started(result, title_before):
            raise SpotifyError("Spotify accepted the play command but nothing started.")

    async def resume(self) -> str:
        """"Play some music" with nothing named: carry on with what the user played last."""
        await self._api("PUT", "/me/player/play", params={"device_id": await self._device_id()})
        deadline = time.time() + START_WAIT_SECONDS
        while time.time() < deadline:  # 403 "already playing" is fine too: just check the state
            r = await self._api("GET", "/me/player")
            if r.status_code == 200 and r.text and r.json().get("is_playing"):
                return "Playing your music on Spotify."
            await asyncio.sleep(0.3)
        return await self.play_query(GENERIC_MUSIC_QUERY, None)  # nothing to resume

    async def play_query(self, query: str, chooser: Chooser | None) -> str:
        device = asyncio.create_task(self._device_id())  # find (or launch) the player while searching
        device.add_done_callback(lambda t: t.cancelled() or t.exception())  # errors resurface below
        try:
            results = await self.search(query)
            if not results:
                return f"Nothing on Spotify matched “{query}”."
            choice = self.obvious_pick(query, results)
            if choice is None and chooser is not None:
                label = await chooser(query, {r.label: None for r in results})
                choice = next((r for r in results if r.label == label), None)
            choice = choice or self.heuristic_pick(query, results)
            if choice is None:
                return f"Nothing on Spotify matched “{query}”."
            await self.play(choice, await device)
        finally:
            device.cancel()
        what = {"track": f"“{choice.name}” by {choice.by}", "album": f"the album “{choice.name}” by {choice.by}",
                "playlist": f"the playlist “{choice.name}”", "artist": choice.name}[choice.kind]
        return f"Playing {what} on Spotify."
