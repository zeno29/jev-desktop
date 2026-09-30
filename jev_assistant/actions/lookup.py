"""Quick web lookups for the LLM tiers over plain HTTPS: no browser, no API keys.

For facts that change (news, scores, prices, who holds an office now), the model reads the top
search snippets and answers in about a second, instead of driving a browser. DuckDuckGo's HTML
endpoints return snippets that already contain the answer for most factual queries, but they
throttle bursts ("anomaly" pages), so Wikipedia's search API is the fallback. Weather has its own
source (Open-Meteo), since search snippets rarely contain the actual numbers.
"""

from __future__ import annotations

import html
import re
import time

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/153.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}
MAX_RESULTS = 6

_HTML_RESULT = re.compile(
    r'class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', re.S)
_LITE_LINK = re.compile(r"<a[^>]+class=.result-link.[^>]*href=\"(?P<url>[^\"]+)\"[^>]*>(?P<title>.*?)</a>", re.S)
_LITE_SNIPPET = re.compile(r"<td class=.result-snippet.>(?P<snippet>.*?)</td>", re.S)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


CACHE_SECONDS = 300          # the same question asked again within 5 min is answered from memory
THROTTLE_BACKOFF = 600       # after DuckDuckGo throttles us, skip it for 10 min instead of waiting on it
_cache: dict[str, tuple[float, str]] = {}
_ddg_blocked_until = 0.0


_http: httpx.AsyncClient | None = None
_places: dict[str, dict | None] = {}  # geocoding results: a city doesn't move


def _client() -> httpx.AsyncClient:
    """One keep-alive client for the app's lifetime: repeat lookups skip the TLS handshakes."""
    global _http
    if _http is None:
        _http = httpx.AsyncClient(headers=HEADERS, timeout=8, follow_redirects=True,
                                  limits=httpx.Limits(keepalive_expiry=300))  # httpx's default is 5 s
    return _http


async def web_lookup(query: str) -> str:
    key = query.strip().lower()
    if key in _cache and time.monotonic() - _cache[key][0] < CACHE_SECONDS:
        return _cache[key][1]
    client = _client()
    results = await _duckduckgo(client, query) if time.monotonic() >= _ddg_blocked_until else []
    if results:
        answer = "Web results:\n" + "\n".join(f"{i}. {t}: {s}" for i, (t, s) in enumerate(results, 1))
    elif wiki := await _wikipedia(client, query):
        answer = ("Web search is unavailable right now; Wikipedia (may not cover the latest events):\n" + wiki)
    else:
        return f"No web results for “{query}”. Answer from what you know and say it may be out of date."
    _cache[key] = (time.monotonic(), answer)
    return answer


async def _duckduckgo(client: httpx.AsyncClient, query: str) -> list[tuple[str, str]]:
    global _ddg_blocked_until
    found: list[tuple[str, str, str]] = []
    r = await client.get("https://html.duckduckgo.com/html/", params={"q": query})
    if r.status_code == 202 or "anomaly" in r.text[:20000].lower():  # throttled: bot check page
        _ddg_blocked_until = time.monotonic() + THROTTLE_BACKOFF
        return []
    if r.status_code == 200:
        found = [(_text(m["title"]), _text(m["snippet"]), m["url"]) for m in _HTML_RESULT.finditer(r.text)]
    if not found and r.status_code == 200:  # 202 = throttled; the lite page is throttled too then
        r = await client.get("https://lite.duckduckgo.com/lite/", params={"q": query})
        links = [(_text(m["title"]), m["url"]) for m in _LITE_LINK.finditer(r.text)]
        snippets = [_text(m["snippet"]) for m in _LITE_SNIPPET.finditer(r.text)]
        found = [(t, s, u) for (t, u), s in zip(links, snippets)]
    return [(t, s) for t, s, u in found if s and "duckduckgo.com/y.js" not in u][:MAX_RESULTS]  # drop ads


async def _wikipedia(client: httpx.AsyncClient, query: str) -> str:
    r = await client.get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "list": "search", "srsearch": query, "srlimit": 2, "format": "json"})
    hits = r.json().get("query", {}).get("search", []) if r.status_code == 200 else []
    out = []
    for hit in hits:
        s = await client.get("https://en.wikipedia.org/api/rest_v1/page/summary/" + hit["title"].replace(" ", "_"))
        if s.status_code == 200 and (extract := s.json().get("extract")):
            out.append(f"{hit['title']}: {extract}")
    return "\n".join(out)


# WMO weather interpretation codes (Open-Meteo)
_WEATHER = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
            51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
            65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
            81: "rain showers", 82: "violent rain showers", 95: "thunderstorm", 96: "thunderstorm with hail",
            99: "thunderstorm with hail"}


async def weather(place: str, when: str = "") -> str:
    """The full picture for the LLM tiers; `when` ("now" / "today" / "tomorrow") gives one short
    sentence instead, ready to show or speak, for Jev's direct weather action."""
    client = _client()
    key = place.strip().lower()
    if key not in _places:
        g = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                             params={"name": place, "count": 1, "language": "en"})
        g.raise_for_status()
        _places[key] = (g.json().get("results") or [None])[0]
    loc = _places[key]
    if not loc:
        return f"Couldn't find a place called “{place}”."
    f = await client.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto", "forecast_days": 2,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"})
    f.raise_for_status()
    d = f.json()
    cur, day = d["current"], d["daily"]
    where = ", ".join(x for x in (loc.get("name"), loc.get("admin1"), loc.get("country")) if x)

    def daily(i: int) -> str:
        return (f"{_WEATHER.get(day['weather_code'][i], 'mixed')}, {day['temperature_2m_min'][i]:.0f} to "
                f"{day['temperature_2m_max'][i]:.0f}°C, {day['precipitation_probability_max'][i]}% chance of rain")

    name = loc.get("name") or place
    if when == "now":
        return (f"It's {cur['temperature_2m']:.0f}°C and {_WEATHER.get(cur['weather_code'], 'mixed')} in {name}, "
                f"feels like {cur['apparent_temperature']:.0f}°C.")
    if when in ("today", "tomorrow"):
        return f"{when.capitalize()} in {name}: {daily(when == 'tomorrow')}."
    return (f"{where} now: {cur['temperature_2m']:.0f}°C (feels like {cur['apparent_temperature']:.0f}°C), "
            f"{_WEATHER.get(cur['weather_code'], 'mixed')}, humidity {cur['relative_humidity_2m']}%, "
            f"wind {cur['wind_speed_10m']:.0f} km/h. Today: {daily(0)}. Tomorrow: {daily(1)}.")
