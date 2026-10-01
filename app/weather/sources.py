"""Fetch current conditions and a daily forecast for a US zip code from six sources.

Each fetcher returns a ``SourceResult``: the normalized reading the widget shows,
plus every raw payload it was built from (so the raw record can be persisted and
shown as-is).

- Open-Meteo: its free public forecast API.
- Weather Underground: the weather data embedded in its zip-code page (the
  ``app-root-state`` JSON the page ships with), including the nearest personal
  weather station's reading.
- Weather.com: its page renders numbers client-side, so we call the same
  internal endpoints its own web client calls, with the key that client uses.
- NOAA: the National Weather Service's public API (api.weather.gov): the nearest
  reporting station for now, the forecast office's grid forecast for the days.
- Yahoo: the forecast embedded in its city page (weather.yahoo.com/us/<st>/<city>/).
- Google: the weather card on a Google search for "weather <zip>", which only
  renders in a real browser, so it is read through a headless one.
"""

import json
import math
import re
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any

from curl_cffi import requests as curl_requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

TIMEOUT_SECONDS = 20
FORECAST_DAYS = 5

# The key weather.com's own web client sends to api.weather.com (seen in its
# page's network requests). If weather.com rotates it, the source reports an
# authorization error until this is updated.
WEATHER_COM_WEB_KEY = "71f92ea9dd2f4790b92ea9dd2f779061"


class Place(BaseModel):
    zip_code: str
    city: str
    state_code: str
    latitude: float
    longitude: float


class SourceResult(BaseModel):
    source: str
    label: str
    page_url: str
    ok: bool
    error: str | None = None
    current: dict[str, Any] | None = None
    daily: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    fetched_at: float = Field(default_factory=time.time)


class WeatherError(Exception):
    """Base for every error this package raises on purpose."""


class HttpStatusError(WeatherError):
    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.url = url


class SourceDataError(WeatherError, ValueError):
    """A source answered, but not with the weather we expected to find in it."""


# A retried request waits this long first; tests set it to zero.
RETRY_DELAY_SECONDS = 1.0


def utc_today() -> date:
    """Today's date in UTC: the anchor for sources that label days by weekday only."""
    return datetime.now(timezone.utc).date()


def _get(url: str, headers: dict[str, str] | None = None) -> str:
    # Weather Underground refuses clients whose TLS handshake does not look like
    # a browser's (Python's own client gets a 403), so every request goes out
    # with a Chrome-shaped handshake.
    response = curl_requests.get(
        url, impersonate="chrome", headers=headers, timeout=TIMEOUT_SECONDS
    )
    if response.status_code >= 400:
        raise HttpStatusError(response.status_code, url)
    return response.text


def lookup_zip(zip_code: str) -> Place:
    body = json.loads(_get(f"https://api.zippopotam.us/us/{zip_code}"))
    place = body["places"][0]
    return Place(
        zip_code=zip_code,
        city=place["place name"],
        state_code=place["state abbreviation"],
        latitude=float(place["latitude"]),
        longitude=float(place["longitude"]),
    )


def _round(value: Any) -> int | None:
    return None if value is None else round(float(value))


# WMO weather interpretation codes, as Open-Meteo reports them.
WMO_CODES = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Cloudy",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with hail",
}


def open_meteo_page_url(place: Place) -> str:
    return f"https://open-meteo.com/en/docs?latitude={place.latitude}&longitude={place.longitude}"


def weather_com_page_url(place: Place) -> str:
    return f"https://weather.com/weather/today/l/{place.zip_code}:4:US"


def fetch_open_meteo(place: Place) -> SourceResult:
    query = urllib.parse.urlencode(
        {
            "latitude": place.latitude,
            "longitude": place.longitude,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code,is_day",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": FORECAST_DAYS,
        }
    )
    api_url = f"https://api.open-meteo.com/v1/forecast?{query}"
    result = SourceResult(
        source="open_meteo",
        label="Open-Meteo",
        page_url=open_meteo_page_url(place),
        ok=False,
    )
    body = json.loads(_get(api_url))
    result.raw = {api_url: body}
    current = body["current"]
    result.current = {
        "temp_f": _round(current["temperature_2m"]),
        "feels_like_f": _round(current["apparent_temperature"]),
        "condition": WMO_CODES.get(
            current["weather_code"], f"Code {current['weather_code']}"
        ),
        "humidity_pct": _round(current["relative_humidity_2m"]),
        "wind_mph": _round(current["wind_speed_10m"]),
        "observed": current["time"],
    }
    daily = body["daily"]
    result.daily = [
        {
            "date": daily["time"][i],
            "high_f": _round(daily["temperature_2m_max"][i]),
            "low_f": _round(daily["temperature_2m_min"][i]),
            "condition": WMO_CODES.get(daily["weather_code"][i], ""),
            "precip_pct": _round(daily["precipitation_probability_max"][i]),
        }
        for i in range(len(daily["time"]))
    ]
    result.ok = True
    return result


def _twc_daily(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize an api.weather.com v3 daily forecast (shared by weather.com and Weather Underground)."""
    daypart = (body.get("daypart") or [{}])[0]
    phrases = daypart.get("wxPhraseLong") or []
    chances = daypart.get("precipChance") or []
    days = []
    for i, valid_time in enumerate(body["validTimeLocal"][:FORECAST_DAYS]):
        # Dayparts alternate day/night; today's day part is null once the day is over.
        day_phrase = phrases[2 * i] if 2 * i < len(phrases) else None
        night_phrase = phrases[2 * i + 1] if 2 * i + 1 < len(phrases) else None
        day_chance = chances[2 * i] if 2 * i < len(chances) else None
        night_chance = chances[2 * i + 1] if 2 * i + 1 < len(chances) else None
        chance_values = [c for c in (day_chance, night_chance) if c is not None]
        days.append(
            {
                "date": valid_time[:10],
                "high_f": _round(body["calendarDayTemperatureMax"][i]),
                "low_f": _round(body["calendarDayTemperatureMin"][i]),
                "condition": day_phrase or night_phrase or "",
                "precip_pct": max(chance_values) if chance_values else None,
            }
        )
    return days


def fetch_weather_com(place: Place) -> SourceResult:
    geocode = f"{place.latitude:.3f},{place.longitude:.3f}"
    common = {
        "geocode": geocode,
        "units": "e",
        "language": "en-US",
        "format": "json",
        "apiKey": WEATHER_COM_WEB_KEY,
    }
    current_url = (
        "https://api.weather.com/v3/wx/observations/current?"
        + urllib.parse.urlencode(common)
    )
    daily_url = (
        "https://api.weather.com/v3/wx/forecast/daily/7day?"
        + urllib.parse.urlencode(common)
    )
    result = SourceResult(
        source="weather_com",
        label="Weather.com",
        page_url=weather_com_page_url(place),
        ok=False,
    )
    current = json.loads(_get(current_url))
    daily = json.loads(_get(daily_url))
    result.raw = {_redact_key(current_url): current, _redact_key(daily_url): daily}
    result.current = {
        "temp_f": _round(current.get("temperature")),
        "feels_like_f": _round(current.get("temperatureFeelsLike")),
        "condition": current.get("wxPhraseLong") or "",
        "humidity_pct": _round(current.get("relativeHumidity")),
        "wind_mph": _round(current.get("windSpeed")),
        "observed": current.get("validTimeLocal"),
    }
    result.daily = _twc_daily(daily)
    result.ok = True
    return result


def _redact_key(url: str) -> str:
    return re.sub(r"apiKey=[0-9a-f]+", "apiKey=...", url)


_APP_STATE_PATTERN = re.compile(
    r'<script id="app-root-state" type="application/json">(.*?)</script>', re.S
)


def parse_wunderground_state(page_html: str) -> dict[str, Any]:
    """Return the page's embedded app state: a map of request id -> {"u": url, "b": body}."""
    match = _APP_STATE_PATTERN.search(page_html)
    if match is None:
        raise SourceDataError("Weather Underground page has no embedded weather data")
    text = match.group(1)
    # Angular's transfer-state escaping, applied by some page versions.
    for escaped, plain in (
        ("&q;", '"'),
        ("&s;", "'"),
        ("&l;", "<"),
        ("&g;", ">"),
        ("&a;", "&"),
    ):
        text = text.replace(escaped, plain)
    return json.loads(text)


def _find_state_body(state: dict[str, Any], *needles: str) -> dict[str, Any] | None:
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        url = entry.get("u") or ""
        body = entry.get("b")
        if body and all(needle in url for needle in needles):
            return body
    return None


def wunderground_page_url(place: Place) -> str:
    city_slug = re.sub(r"[^a-z0-9]+", "-", place.city.lower()).strip("-")
    return f"https://www.wunderground.com/weather/us/{place.state_code.lower()}/{city_slug}/{place.zip_code}"


# Weather Underground now and then serves a 403 or a lighter page variant with
# no embedded data; asking again usually gets the full page.
WUNDERGROUND_ATTEMPTS = 3


def _fetch_wunderground_state(page_url: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(WUNDERGROUND_ATTEMPTS):
        if attempt:
            time.sleep(RETRY_DELAY_SECONDS)
        try:
            return parse_wunderground_state(_get(page_url))
        except (HttpStatusError, SourceDataError) as error:
            last_error = error
    assert last_error is not None
    raise last_error


def fetch_wunderground(place: Place) -> SourceResult:
    page_url = wunderground_page_url(place)
    result = SourceResult(
        source="wunderground", label="Weather Underground", page_url=page_url, ok=False
    )
    state = _fetch_wunderground_state(page_url)
    station_body = _find_state_body(state, "/v2/pws/observations/current")
    observation_body = _find_state_body(state, "/v3/wx/observations/current", "geocode")
    daily_body = _find_state_body(
        state, "/v3/wx/forecast/daily/5day", "geocode"
    ) or _find_state_body(state, "/v3/wx/forecast/daily/", "geocode")
    result.raw = {
        _redact_key(entry["u"]): entry["b"]
        for entry in state.values()
        if isinstance(entry, dict)
        and entry.get("b")
        and "api.weather.com" in (entry.get("u") or "")
    }
    stations = (station_body or {}).get("observations") or []
    # A station that is online but not reporting temperature is no use as the reading.
    station = (
        stations[0]
        if stations and (stations[0].get("imperial") or {}).get("temp") is not None
        else None
    )
    if station is None and observation_body is None:
        raise SourceDataError(
            "Weather Underground page had no current conditions for this zip"
        )
    observation = observation_body or {}
    if station is not None:
        imperial = station.get("imperial") or {}
        temp = imperial.get("temp")
        feels = (
            imperial.get("heatIndex")
            if temp is not None and temp >= 70
            else imperial.get("windChill")
        )
        result.current = {
            "temp_f": _round(temp),
            "feels_like_f": _round(feels),
            "condition": observation.get("wxPhraseLong") or "",
            "humidity_pct": _round(station.get("humidity")),
            "wind_mph": _round(imperial.get("windSpeed")),
            "observed": station.get("obsTimeLocal"),
            "station": f"{station.get('neighborhood') or 'Nearby'} station ({station.get('stationID')})",
        }
    else:
        result.current = {
            "temp_f": _round(observation.get("temperature")),
            "feels_like_f": _round(observation.get("temperatureFeelsLike")),
            "condition": observation.get("wxPhraseLong") or "",
            "humidity_pct": _round(observation.get("relativeHumidity")),
            "wind_mph": _round(observation.get("windSpeed")),
            "observed": observation.get("validTimeLocal"),
        }
    if daily_body is not None:
        result.daily = _twc_daily(daily_body)
    result.ok = True
    return result


def _date_near_now(weekday_name: str) -> date:
    """The date, within a day of today in UTC, that falls on this weekday.

    A US place's local date is UTC's or the day before (the evening there is
    already tomorrow in UTC), and three days in a row have three different
    weekdays, so the match is unambiguous.
    """
    today = utc_today()
    for offset in (0, -1, 1):
        candidate = today + timedelta(days=offset)
        if candidate.strftime("%a").lower() == weekday_name[:3].lower():
            return candidate
    raise SourceDataError(f"No date near today falls on {weekday_name!r}")


def _miles_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    d_lat, d_lon = lat2_r - lat1_r, math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(d_lon / 2) ** 2
    )
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def _f_from_c(value: Any) -> int | None:
    return None if value is None else round(float(value) * 9 / 5 + 32)


def _mph_from_kmh(value: Any) -> int | None:
    return None if value is None else round(float(value) * 0.621371)


# api.weather.gov asks every client to identify itself in its User-Agent.
NOAA_HEADERS = {
    "User-Agent": "imbue-workspace-weather-widget",
    "Accept": "application/geo+json",
}
NOAA_STATIONS_TO_TRY = 10
# A station farther than this can sit in a different climate (downtown SF for Fairfax, CA).
NOAA_MAX_STATION_MILES = 10


def noaa_page_url(place: Place) -> str:
    return f"https://forecast.weather.gov/MapClick.php?lat={place.latitude}&lon={place.longitude}"


def _noaa_json(url: str) -> dict[str, Any]:
    # The forecast endpoint now and then answers 500 while a grid is regenerating.
    try:
        return json.loads(_get(url, NOAA_HEADERS))
    except HttpStatusError as error:
        if error.status < 500:
            raise
        time.sleep(RETRY_DELAY_SECONDS)
        return json.loads(_get(url, NOAA_HEADERS))


def fetch_noaa(place: Place) -> SourceResult:
    result = SourceResult(
        source="noaa", label="NOAA", page_url=noaa_page_url(place), ok=False
    )
    points_url = (
        f"https://api.weather.gov/points/{place.latitude:.4f},{place.longitude:.4f}"
    )
    points = _noaa_json(points_url)
    forecast_url = points["properties"]["forecast"]
    stations_url = points["properties"]["observationStations"]
    forecast = _noaa_json(forecast_url)
    stations = _noaa_json(stations_url)
    result.raw = {points_url: points, forecast_url: forecast}
    # The nearest stations first; some report no temperature, so take the first one that does.
    for feature in stations.get("features", [])[:NOAA_STATIONS_TO_TRY]:
        station_lon, station_lat = feature["geometry"]["coordinates"][:2]
        miles = _miles_between(
            place.latitude, place.longitude, station_lat, station_lon
        )
        if miles > NOAA_MAX_STATION_MILES:
            continue
        station_id = feature["properties"]["stationIdentifier"]
        observation_url = (
            f"https://api.weather.gov/stations/{station_id}/observations/latest"
        )
        try:
            observation = _noaa_json(observation_url)
        except HttpStatusError:
            continue
        props = observation["properties"]
        if (props.get("temperature") or {}).get("value") is None:
            continue
        result.raw[observation_url] = observation
        feels_c = (props.get("heatIndex") or {}).get("value")
        if feels_c is None:
            feels_c = (props.get("windChill") or {}).get("value")
        humidity = (props.get("relativeHumidity") or {}).get("value")
        result.current = {
            "temp_f": _f_from_c(props["temperature"]["value"]),
            "feels_like_f": _f_from_c(feels_c)
            if feels_c is not None
            else _f_from_c(props["temperature"]["value"]),
            "condition": props.get("textDescription") or "",
            "humidity_pct": _round(humidity),
            "wind_mph": _mph_from_kmh((props.get("windSpeed") or {}).get("value")),
            "observed": props.get("timestamp"),
            "station": f"{feature['properties'].get('name') or 'Nearby'} station ({station_id}, {miles:.0f} mi)",
        }
        break
    days: dict[str, dict[str, Any]] = {}
    for period in forecast["properties"]["periods"]:
        day = days.setdefault(
            period["startTime"][:10],
            {
                "date": period["startTime"][:10],
                "high_f": None,
                "low_f": None,
                "condition": "",
                "precip_pct": None,
            },
        )
        chance = (period.get("probabilityOfPrecipitation") or {}).get("value")
        if chance is not None:
            day["precip_pct"] = max(day["precip_pct"] or 0, chance)
        if period["isDaytime"]:
            day["high_f"] = period["temperature"]
            day["condition"] = period["shortForecast"]
        else:
            day["low_f"] = period["temperature"]
            day["condition"] = day["condition"] or period["shortForecast"]
    result.daily = list(days.values())[:FORECAST_DAYS]
    if result.current is None:
        # No nearby station is reporting: fall back to the forecast's current period.
        first = forecast["properties"]["periods"][0]
        result.current = {
            "temp_f": first["temperature"],
            "feels_like_f": None,
            "condition": first["shortForecast"],
            "humidity_pct": None,
            "wind_mph": None,
            "observed": first["startTime"],
            "station": "Forecast; no station nearby",
        }
    result.ok = True
    return result


def _yahoo_city_url(state_code: str, city: str) -> str:
    city_slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
    return f"https://weather.yahoo.com/us/{state_code.lower()}/{city_slug}/"


def _yahoo_city_names(place: Place) -> list[str]:
    """The zip's place name, then the same without a trailing "City" ("New York City" is "New York" to Yahoo)."""
    names = [place.city]
    shorter = re.sub(r"\s+city$", "", place.city, flags=re.I)
    if shorter != place.city:
        names.append(shorter)
    return names


def yahoo_page_url(place: Place) -> str:
    return _yahoo_city_url(place.state_code, place.city)


_NEXT_CHUNK_PATTERN = re.compile(
    r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S
)


def _yahoo_page_data(page_html: str) -> str:
    """The page's streamed render data (Next.js flight chunks), joined and unescaped."""
    return "".join(
        json.loads(f'"{chunk}"') for chunk in _NEXT_CHUNK_PATTERN.findall(page_html)
    )


def _json_after(text: str, key: str) -> Any:
    start = text.find(f'"{key}":')
    if start < 0:
        return None
    value, _ = json.JSONDecoder().raw_decode(text[start + len(key) + 3 :])
    return value


def _yahoo_today(dated_index: int, weekday: str, day_of_month: int) -> date:
    """The place's local today, from the first forecast day labelled like "Sat 3" (the dated_index-th day)."""
    # The dated day is local today + dated_index, and local today is UTC's today
    # or the day before, so it is one of two dates; their days of the month differ.
    today = utc_today()
    for offset in (dated_index, dated_index - 1):
        candidate = today + timedelta(days=offset)
        if (
            candidate.day == day_of_month
            and candidate.strftime("%a").lower() == weekday[:3].lower()
        ):
            return candidate - timedelta(days=dated_index)
    raise SourceDataError(f"Yahoo's {weekday} {day_of_month} is not near today")


def fetch_yahoo(place: Place) -> SourceResult:
    page_html = None
    page_url = yahoo_page_url(place)
    for city in _yahoo_city_names(place):
        candidate_url = _yahoo_city_url(place.state_code, city)
        try:
            candidate = _get(candidate_url)
        except HttpStatusError:
            continue
        title = re.search(r"<title>([^<]*)", candidate)
        # An unknown city redirects to a default page for some other place.
        if (
            title is not None
            and f"{city}, {place.state_code}".lower() in title.group(1).lower()
        ):
            page_html, page_url = candidate, candidate_url
            break
    if page_html is None:
        raise SourceDataError(f"Yahoo has no page for {place.city}, {place.state_code}")
    result = SourceResult(source="yahoo", label="Yahoo", page_url=page_url, ok=False)
    data = _yahoo_page_data(page_html)
    forecasts = _json_after(data, "dailyForecasts")
    if not forecasts:
        raise SourceDataError("Yahoo's page had no forecast in it")
    result.raw = {page_url: {"dailyForecasts": forecasts}}
    humidity_match = re.search(
        r'"conditionType":"humidity".{0,400}?"value":"(\d+)%"', data
    )
    # Days run from today; "Sat 3"-style labels pin which date the third one is.
    dated_index = next(
        (
            i
            for i, d in enumerate(forecasts)
            if re.match(r"^[A-Za-z]{3} \d+$", d["date"])
        ),
        None,
    )
    if dated_index is None:
        raise SourceDataError("Yahoo's forecast days had no dates")
    weekday, day_of_month = forecasts[dated_index]["date"].split()
    today = _yahoo_today(dated_index, weekday, int(day_of_month))
    now = (forecasts[0].get("conditionsForecasts") or [{}])[0]
    winds = forecasts[0].get("windForecasts") or [{}]
    result.current = {
        "temp_f": _round(now.get("temperature")),
        "feels_like_f": None,
        "condition": now.get("iconLabel") or forecasts[0].get("iconLabel") or "",
        "humidity_pct": int(humidity_match.group(1)) if humidity_match else None,
        "wind_mph": _round(winds[0].get("speed")),
        "observed": now.get("time"),
    }
    result.daily = []
    for i, day in enumerate(forecasts[:FORECAST_DAYS]):
        chances = [
            p["probabilityOfPrecipitation"]
            for p in day.get("precipitationForecasts") or []
            if p.get("probabilityOfPrecipitation") is not None
        ]
        result.daily.append(
            {
                "date": (today + timedelta(days=i)).isoformat(),
                "high_f": _round(day.get("highTemperature")),
                "low_f": _round(day.get("lowTemperature")),
                "condition": day.get("iconLabel") or "",
                "precip_pct": max(chances) if chances else None,
            }
        )
    result.ok = True
    return result


FORTRESS_EXECUTABLE = "/opt/fortress/tilion-fortress/tilion"
# One headless browser at a time: each one is a few hundred MB.
_google_browser_lock = threading.Lock()

_GOOGLE_CARD_SCRIPT = """() => {
  const text = (selector) => document.querySelector(selector)?.innerText ?? null;
  return {
    temp: text('#wob_tm'), condition: text('#wob_dc'), precip: text('#wob_pp'),
    humidity: text('#wob_hm'), wind: text('#wob_ws'), observed: text('#wob_dts'), place: text('#wob_loc'),
    days: [...document.querySelectorAll('.wob_df')].map((d) => ({
      day: d.querySelector('[aria-label]')?.getAttribute('aria-label'),
      condition: d.querySelector('img')?.alt ?? '',
      temps: d.innerText,
    })),
  };
}"""


def google_page_url(place: Place) -> str:
    return "https://www.google.com/search?" + urllib.parse.urlencode(
        {"q": f"weather {place.zip_code}", "hl": "en", "gl": "us"}
    )


def _leading_int(text: str | None) -> int | None:
    match = re.search(r"-?\d+", text or "")
    return int(match.group(0)) if match else None


def read_google_card(page_url: str) -> dict[str, Any]:
    """Open the search in a headless browser and read the weather card's text off the page."""
    with _google_browser_lock, sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=FORTRESS_EXECUTABLE, args=["--no-sandbox"]
        )
        try:
            page = browser.new_page(locale="en-US")
            page.goto(page_url, timeout=TIMEOUT_SECONDS * 1000)
            try:
                page.wait_for_selector("#wob_tm", timeout=15000)
            except PlaywrightTimeoutError as error:
                if "sorry" in page.url or "unusual traffic" in page.content():
                    raise SourceDataError(
                        "Google asked to confirm we're not a robot"
                    ) from error
                raise SourceDataError(
                    "Google didn't show a weather card for this zip"
                ) from error
            return page.evaluate(_GOOGLE_CARD_SCRIPT)
        finally:
            browser.close()


def fetch_google(place: Place) -> SourceResult:
    page_url = google_page_url(place)
    result = SourceResult(source="google", label="Google", page_url=page_url, ok=False)
    card = read_google_card(page_url)
    result.raw = {page_url: card}
    precip = _leading_int(card.get("precip"))
    result.current = {
        "temp_f": _leading_int(card.get("temp")),
        "feels_like_f": None,
        "condition": card.get("condition") or "",
        "humidity_pct": _leading_int(card.get("humidity")),
        "wind_mph": _leading_int(card.get("wind")),
        "observed": card.get("observed"),
    }
    days = card.get("days") or []
    if days and days[0].get("day"):
        first_date = _date_near_now(days[0]["day"])
        for i, day in enumerate(days[:FORECAST_DAYS]):
            temps = [int(t) for t in re.findall(r"-?\d+(?=°)", day.get("temps") or "")]
            result.daily.append(
                {
                    "date": (first_date + timedelta(days=i)).isoformat(),
                    "high_f": temps[0] if temps else None,
                    "low_f": temps[1] if len(temps) > 1 else None,
                    "condition": day.get("condition") or "",
                    "precip_pct": precip if i == 0 else None,
                }
            )
    result.ok = True
    return result


# Display order in the widget.
FETCHERS = {
    "weather_com": ("Weather.com", weather_com_page_url, fetch_weather_com),
    "wunderground": ("Weather Underground", wunderground_page_url, fetch_wunderground),
    "open_meteo": ("Open-Meteo", open_meteo_page_url, fetch_open_meteo),
    "noaa": ("NOAA", noaa_page_url, fetch_noaa),
    "google": ("Google", google_page_url, fetch_google),
    "yahoo": ("Yahoo", yahoo_page_url, fetch_yahoo),
}
