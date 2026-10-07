"""audio_sources.py: audio content sources for the agent.

Radio: radio-browser.info, an open internet-radio catalog with no keys. We find a station
for the query and return the direct stream URL (url_resolved); the phone plays it with
ExoPlayer. Tracks: Yandex Music (bring your own token) → Audius (free, public API).

RADIO_COUNTRIES / RADIO_LANGUAGE set the regional priority (see find_radio).
"""

from __future__ import annotations

import asyncio
import os

import httpx

# API mirrors (no keys). radio-browser requires a meaningful User-Agent.
_HOSTS = ["https://de1.api.radio-browser.info", "https://fi1.api.radio-browser.info",
          "https://all.api.radio-browser.info"]
_UA = {"User-Agent": "smotri-ai/1.0 (glasses agent)"}

# proxy for geo-blocked sources (Audius blocks datacenter IPs).
# AUDIO_PROXY=http://user:pass@host:port in .env. Empty → no proxy.
def _proxy() -> str | None:
    return os.getenv("AUDIO_PROXY") or None


async def find_radio(query: str, exclude: set[str] | None = None) -> dict | None:
    """Find a radio station for the query → {'title','url'} or None.
    exclude: URLs already tried. Without it "play another one" returned the same
    station (limit=1 = always the top one) and the user heard silence again.
    Every candidate is probed for liveness: the catalog is full of dead streams."""
    q = (query or "").strip()
    if not q:
        return None
    # IMPORTANT: the PHONE fetches the stream, not the server. The global top-voted
    # stations may be unreachable from the phone (measured: a French station had 100%
    # packet loss from Central Asia while local hosts had 0%). So search the user's
    # region first, and only then globally.
    base = {"limit": 1, "hidebroken": "true", "order": "votes", "reverse": "true"}
    countries = [c.strip().upper() for c in
                 os.getenv("RADIO_COUNTRIES", "").split(",") if c.strip()]
    language = os.getenv("RADIO_LANGUAGE", "").strip()

    attempts = []
    for cc in countries:                      # 1) genre in the user's country
        attempts.append({**base, "tag": q, "countrycode": cc})
    if language:                              # 2) genre in the user's language
        attempts.append({**base, "tag": q, "language": language})
    attempts.append({**base, "tag": q})       # 3) genre globally
    attempts.append({**base, "name": q})      # 4) by station name
    for cc in countries:                      # 5) anything local
        attempts.append({**base, "countrycode": cc})
    attempts.append(dict(base))               # 6) global top: last resort
    skip = set(exclude or ())
    for params in attempts:
        params = {**params, "limit": 8}   # take a batch: some will be dead
        for host in _HOSTS:
            try:
                async with httpx.AsyncClient(timeout=10.0, headers=_UA) as c:
                    r = await c.get(f"{host}/json/stations/search", params=params)
                if r.status_code != 200:
                    continue
                arr = r.json() or []
                if not arr:
                    break
                for st in arr:
                    url = st.get("url_resolved") or st.get("url")
                    if not url or url in skip:
                        continue
                    if not await _stream_alive(url):
                        print("[radio] dead stream: " + str(st.get("name")), flush=True)
                        skip.add(url)
                        continue
                    return {"title": st.get("name") or q, "url": url}
                break
            except Exception:  # noqa: BLE001 — try the next mirror
                continue
    return None


async def _stream_alive(url: str, timeout: float = 4.0) -> bool:
    """Does the stream deliver data? radio-browser has many dead links and the client
    just stays silent on them, so filter them out here."""
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_UA,
                                     follow_redirects=True) as c:
            async with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    return False
                async for _chunk in r.aiter_bytes():
                    return True      # first byte arrived: the stream is alive
        return False
    except Exception:  # noqa: BLE001
        return False


# ── Audius: specific tracks by query (free public API, direct mp3 stream) ──
async def _audius_host() -> str:
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_UA, proxy=_proxy()) as c:
            r = await c.get("https://api.audius.co")
        hosts = (r.json() or {}).get("data") or []
        if hosts:
            return hosts[0]
    except Exception:  # noqa: BLE001
        pass
    return "https://discoveryprovider.audius.co"


async def find_track(query: str) -> dict | None:
    """Find a specific track (title/artist) → {'title','url'} or None.
    url is the Audius stream endpoint (302 to an mp3; ExoPlayer follows the redirect)."""
    q = (query or "").strip()
    if not q:
        return None
    host = await _audius_host()
    try:
        async with httpx.AsyncClient(timeout=12.0, headers=_UA, proxy=_proxy()) as c:
            r = await c.get(f"{host}/v1/tracks/search",
                            params={"query": q, "app_name": "smotri-ai"})
        if r.status_code != 200:
            return None
        arr = (r.json() or {}).get("data") or []
        if not arr:
            return None
        t = arr[0]
        tid = t.get("id")
        if not tid:
            return None
        artist = (t.get("user") or {}).get("name") or ""
        title = " — ".join(x for x in (t.get("title"), artist) if x) or q
        return {"title": title, "url": f"{host}/v1/tracks/{tid}/stream?app_name=smotri-ai"}
    except Exception:  # noqa: BLE001
        return None


# ── Yandex Music: a real mainstream catalog, with your own token ──
_yc = None  # cached Yandex Music client (init is a network call)


def _yandex_track_sync(query: str) -> dict | None:
    """SYNCHRONOUS: find a track in Yandex Music + a direct mp3 link. Needs YANDEX_MUSIC_TOKEN
    (+ a Plus subscription for full tracks, otherwise 30 s previews). Run in to_thread."""
    global _yc
    tok = os.getenv("YANDEX_MUSIC_TOKEN")
    if not tok:
        return None
    from yandex_music import Client
    if _yc is None:
        _yc = Client(tok).init()
    res = _yc.search(query)
    tracks = getattr(res, "tracks", None)
    results = getattr(tracks, "results", None) if tracks else None
    if not results:
        return None
    t = results[0]
    artists = ", ".join(a.name for a in (t.artists or []))
    title = f"{t.title} — {artists}" if artists else (t.title or query)
    infos = t.get_download_info(get_direct_links=True) or []
    mp3 = [i for i in infos if (i.codec or "").lower() == "mp3"] or infos
    if not mp3:
        return None
    mp3.sort(key=lambda i: i.bitrate_in_kbps or 0, reverse=True)
    link = mp3[0].direct_link
    return {"title": title, "url": link} if link else None


async def find_yandex_track(query: str) -> dict | None:
    """Find a track in Yandex Music → {'title','url'} or None (no token / not found)."""
    q = (query or "").strip()
    if not q or not os.getenv("YANDEX_MUSIC_TOKEN"):
        return None
    try:
        return await asyncio.to_thread(_yandex_track_sync, q)
    except Exception as e:  # noqa: BLE001
        print(f"[yandex_music] fail: {e}", flush=True)
        return None

