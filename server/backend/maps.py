"""maps.py: nearby places + address from coordinates for the agent.

Works without any key: the address comes from OpenStreetMap Nominatim (free) and places
from a web search near that address (_find_place_web). Optional Yandex key
(YANDEX_API_KEY) adds real business search via Places (search-maps) and its Geocoder;
Yandex enables APIs per key, and a missing one returns 403.
MAPS_LANG sets the response language (default en_US; ru_RU, tr_TR, uk_UA...).

Env: YANDEX_API_KEY
"""

from __future__ import annotations

import math
import os

import httpx

PLACES_URL = "https://search-maps.yandex.ru/v1/"
GEOCODE_URL = "https://geocode-maps.yandex.ru/1.x/"


def is_configured() -> bool:
    return bool(os.getenv("YANDEX_API_KEY"))


def _haversine_m(lat1, lon1, lat2, lon2) -> int:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


_DIRS = ["north", "northeast", "east", "southeast",
         "south", "southwest", "west", "northwest"]
_LANG = os.getenv("MAPS_LANG", "en_US")


def _bearing(lat1, lon1, lat2, lon2) -> str:
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dlon))
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return _DIRS[int((deg + 22.5) % 360 // 45)]


def _dist_dir(location, coords) -> str:
    """coords = [lon, lat] (Yandex format) → ', 300 m northeast'"""
    if not (location and len(coords) == 2):
        return ""
    m = _haversine_m(location[0], location[1], coords[1], coords[0])
    d = f"{m} m" if m < 1000 else f"{m / 1000:.1f} km"
    return f", {d} {_bearing(location[0], location[1], coords[1], coords[0])}"


async def find_place(query: str, location: tuple[float, float] | None = None,
                     count: int = 5, addr: str | None = None) -> str:
    """Find businesses/places. location=(lat,lon) for "nearby" and distances."""
    key = os.getenv("YANDEX_API_KEY")
    if not key:
        return await _find_place_web(query, location, addr, count)
    params: dict = {"apikey": key, "text": query, "lang": _LANG, "type": "biz", "results": count}
    if location:
        params["ll"] = f"{location[1]},{location[0]}"   # Yandex: lon,lat
        params["spn"] = "0.1,0.1"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(PLACES_URL, params=params)
        if r.status_code in (401, 403):
            # The key has no Places access (Yandex enables services separately).
            # Don't fail the user: search the web near their address instead.
            print(f"[maps] Places {r.status_code} → web search fallback", flush=True)
            return await _find_place_web(query, location, addr, count)
        if r.status_code != 200:
            print(f"[maps] Places {r.status_code}: {r.text[:120]}", flush=True)
            return ("Map search failed. Say so briefly and don't make up places.")
        feats = (r.json().get("features") or [])
        if not feats:
            return "Nothing found on the map for this query."
        lines = []
        for i, f in enumerate(feats[:count], 1):
            props = f.get("properties") or {}
            meta = props.get("CompanyMetaData") or {}
            name = meta.get("name") or props.get("name") or "?"
            addr = meta.get("address") or props.get("description") or ""
            coords = (f.get("geometry") or {}).get("coordinates") or []  # [lon,lat]
            dist = _dist_dir(location, coords)
            hours = (meta.get("Hours") or {}).get("text", "")
            lines.append(f"{i}. {name} — {addr}{dist}{(' ('+hours+')') if hours else ''}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"Map search failed: {e}"


async def reverse_geocode(lat: float, lon: float) -> str | None:
    """Coordinates → human-readable address (for the "where the user is" context).
    Yandex keys are PER API: Geocoder ≠ Places. Uses YANDEX_GEOCODER_KEY, falling back
    to YANDEX_API_KEY (which may lack geocoder access → 403)."""
    key = os.getenv("YANDEX_GEOCODER_KEY") or os.getenv("YANDEX_API_KEY")
    if not key:
        return await _reverse_osm(lat, lon)
    # kind=house → the nearest BUILDING (street + number), not the district/city
    params = {"apikey": key, "geocode": f"{lon},{lat}", "format": "json",
              "lang": _LANG, "results": 1, "kind": "house"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(GEOCODE_URL, params=params)
        if r.status_code != 200:
            return None
        members = (((r.json().get("response") or {}).get("GeoObjectCollection") or {})
                   .get("featureMember") or [])
        if not members:
            return None
        geo = members[0].get("GeoObject") or {}
        meta = ((geo.get("metaDataProperty") or {}).get("GeocoderMetaData") or {})
        return meta.get("text") or geo.get("name")
    except Exception:  # noqa: BLE001
        return None

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_osm_cache: dict[tuple[float, float], str | None] = {}


async def _reverse_osm(lat: float, lon: float) -> str | None:
    """Free reverse geocoding via OpenStreetMap Nominatim (no key), used without Yandex.

    Nominatim's policy: a real User-Agent and at most ~1 request/s. The phone sends location
    updates often, so results are cached per ~100 m cell (3 decimal places)."""
    cell = (round(lat, 3), round(lon, 3))
    if cell in _osm_cache:
        return _osm_cache[cell]
    lang = _LANG.split("_")[0]
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={
            "User-Agent": "smotri-glasses-agent/0.1 (+https://github.com/nabinabi11/meta-rayban-glasses_Smotri)",
        }) as c:
            r = await c.get(NOMINATIM_URL, params={"lat": lat, "lon": lon, "format": "jsonv2",
                                                   "zoom": 18, "accept-language": lang})
        if r.status_code != 200:
            return None
        a = r.json().get("address") or {}
        street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x)
        area = a.get("suburb") or a.get("neighbourhood") or a.get("quarter")
        city = a.get("city") or a.get("town") or a.get("village") or a.get("state")
        addr = ", ".join(x for x in (street, area, city, a.get("country")) if x) or None
        _osm_cache[cell] = addr
        return addr
    except Exception:  # noqa: BLE001
        return None


async def _find_place_web(query: str, location, addr: str | None, count: int) -> str:
    """Fallback without Yandex Places: search the web for businesses near the KNOWN address.

    The geocoder gives a street and number, so the query is specific
    ("coffee shop near 7 Tverskaya St, Moscow") rather than "coffee shop near me".
    There are no distances or opening hours here; the model is told so honestly,
    so it doesn't present guesses as facts."""
    from backend import web
    where = (addr or "").strip()
    if not where and location:
        where = await reverse_geocode(location[0], location[1]) or ""
    q = f"{query} near {where}" if where else query
    txt = await web.search_text(q, count=count)
    # IMPORTANT: don't tell the model about the technical failure: it used to say it
    # aloud ("maps don't work, let me search the web"), which the user doesn't care
    # about and sounds broken. Only what affects the decision: web data, no
    # distances, opening hours worth checking.
    head = (f"Places near \"{where}\" (web search data, not a map: no distances). "
            "Name AT MOST two options in one or two short sentences. Do NOT mention "
            "technical issues, maps or how you searched. If opening hours matter for "
            "going there, briefly suggest checking them.")
    if not where:
        head = ("Places for the query (web search data). Name AT MOST two options "
                "briefly. Do NOT mention technical issues or how you searched.")
    return head + "\n" + txt
