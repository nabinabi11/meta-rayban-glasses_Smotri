"""weather.py: current weather and today's forecast for the agent (Open-Meteo, NO key).

A dedicated tool beats web search here: search results carry page titles, not numbers,
and a model without numbers either refuses or invents them (seen in testing).

  get_weather("Tokyo")         → geocode the place name, then forecast
  get_weather("", location)    → forecast for the user's current coordinates
"""

from __future__ import annotations

import httpx

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes → words
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


async def _geocode(name: str, client: httpx.AsyncClient) -> tuple[float, float, str] | None:
    r = await client.get(GEO_URL, params={"name": name, "count": 1, "language": "en"})
    hits = (r.json().get("results") or []) if r.status_code == 200 else []
    if not hits:
        return None
    h = hits[0]
    label = ", ".join(x for x in (h.get("name"), h.get("admin1"), h.get("country")) if x)
    return h["latitude"], h["longitude"], label


async def get_weather(place: str = "", location: tuple[float, float] | None = None) -> str:
    """Weather as text for the tool. place wins over location; neither → ask for a place."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if place.strip():
                g = await _geocode(place.strip(), client)
                if not g:
                    return f"Couldn't find a place called \"{place}\"."
                lat, lon, label = g
            elif location:
                lat, lon = location
                label = "the user's current location"
            else:
                return "The user's location is unknown. Ask which city they mean."
            r = await client.get(FORECAST_URL, params={
                "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            })
        if r.status_code != 200:
            return f"Weather service error {r.status_code}."
        d = r.json()
        cur, day = d.get("current") or {}, d.get("daily") or {}

        def daily(i: int) -> str:
            return (f"{_WMO.get(day['weather_code'][i], 'unknown')}, "
                    f"{day['temperature_2m_min'][i]:.0f} to {day['temperature_2m_max'][i]:.0f} °C, "
                    f"chance of precipitation {day['precipitation_probability_max'][i] or 0}%")

        return (
            f"Weather for {label} (local time {cur.get('time', '?')}):\n"
            f"Now: {_WMO.get(cur.get('weather_code'), 'unknown')}, {cur.get('temperature_2m')} °C "
            f"(feels like {cur.get('apparent_temperature')} °C), wind {cur.get('wind_speed_10m')} km/h.\n"
            f"Today: {daily(0)}.\nTomorrow: {daily(1)}.\n"
            "Say it in one or two short sentences, in the user's units if they use Fahrenheit."
        )
    except Exception as e:  # noqa: BLE001
        return f"Weather lookup failed: {e}"
