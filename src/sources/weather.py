"""Weather via Open-Meteo.

REFERENCE IMPLEMENTATION. This module is written out fully so you have one worked
example of the source contract. Write news.py yourself by analogy.

Open-Meteo needs no API key and no account, which is why it is the phase 1 source.
"""

import requests

from src import config

API_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes. Open-Meteo returns an integer, not a string,
# so we map it ourselves. Abbreviated to the codes that actually occur in the
# northeastern US.
WMO_CODES = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "severe thunderstorm with hail",
}


def fetch() -> list[dict]:
    """Return today's forecast as a single-item list.

    Returns a list even though there is only ever one item, because every source
    module returns a list. Keeping the contract uniform means brief.py does not
    need to special case anything.

    Raises:
        requests.HTTPError: if the API returns a non-2xx status.
        requests.Timeout: if the request exceeds 10 seconds.
    """
    params = {
        "latitude": config.HOME_LAT,
        "longitude": config.HOME_LON,
        "timezone": config.TIMEZONE,
        "temperature_unit": "fahrenheit",
        "forecast_days": 1,
        "current": "temperature_2m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,weather_code",
    }

    # timeout is not optional. Without it a hung connection blocks the whole
    # morning job indefinitely, since this runs in a single process.
    response = requests.get(API_URL, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    daily = data["daily"]
    current = data["current"]

    return [{
        "temp_now_f": round(current["temperature_2m"]),
        "high_f": round(daily["temperature_2m_max"][0]),
        "low_f": round(daily["temperature_2m_min"][0]),
        "precip_chance": daily["precipitation_probability_max"][0],
        "condition": WMO_CODES.get(daily["weather_code"][0], "unknown"),
        "raw": data,  # keep the full payload. see CLAUDE.md conventions.
    }]


def format_line(w: dict) -> str:
    """One-line summary for the briefing."""
    return (
        f"{w['temp_now_f']}F now, high {w['high_f']} low {w['low_f']}, "
        f"{w['condition']}, {w['precip_chance']}% precip"
    )


if __name__ == "__main__":
    # Run directly to test this module alone:  python -m src.sources.weather
    for item in fetch():
        print(format_line(item))
