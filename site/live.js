// Live lookup, in the visitor's browser, for a zip code the hourly job doesn't track.
//
// Only sources that let a web page read them from another site can be asked from here:
// Open-Meteo, NOAA, Weather.com and Weather Underground (the last two through the same
// api.weather.com endpoints their own websites call). Yahoo and Google don't allow it,
// so their rows say so. Produces the same report shape the hourly job writes.

const FORECAST_DAYS = 5;
// The keys weather.com's and wunderground.com's own pages send to api.weather.com.
const WEATHER_COM_KEY = "71f92ea9dd2f4790b92ea9dd2f779061";
const WUNDERGROUND_KEY = "53b89abc03d14d7ab89abc03d1dd7ab6";
const NOAA_STATIONS_TO_TRY = 10;
const NOAA_MAX_STATION_MILES = 10;

const WMO_CODES = {
  0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Freezing fog",
  51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
  61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
  71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Light showers", 81: "Showers",
  82: "Heavy showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
  96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
};

const round = (v) => (v === null || v === undefined ? null : Math.round(Number(v)));
const fFromC = (v) => (v === null || v === undefined ? null : Math.round((Number(v) * 9) / 5 + 32));
const mphFromKmh = (v) => (v === null || v === undefined ? null : Math.round(Number(v) * 0.621371));
const slug = (text) => text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

async function getJson(url, raw, headers) {
  const response = await fetch(url, { headers: headers || { Accept: "application/json" } });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  if (raw) raw[url.replace(/apiKey=[0-9a-f]+/, "apiKey=...")] = body;
  return body;
}

function milesBetween(lat1, lon1, lat2, lon2) {
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1), dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 3958.8 * 2 * Math.asin(Math.sqrt(a));
}

export async function lookupZip(zip) {
  const response = await fetch(`https://api.zippopotam.us/us/${zip}`);
  if (response.status === 404) throw new Error(`${zip} isn't a US zip code we can find.`);
  if (!response.ok) throw new Error(`Couldn't look up ${zip} (HTTP ${response.status}).`);
  const place = (await response.json()).places[0];
  return {
    zip, city: place["place name"], state: place["state abbreviation"],
    lat: Number(place.latitude), lon: Number(place.longitude),
  };
}

// api.weather.com v3 daily forecast (Weather.com and Weather Underground both use it).
function twcDaily(body) {
  const part = (body.daypart || [{}])[0] || {};
  const phrases = part.wxPhraseLong || [];
  const chances = part.precipChance || [];
  return body.validTimeLocal.slice(0, FORECAST_DAYS).map((time, i) => {
    const values = [chances[2 * i], chances[2 * i + 1]].filter((c) => c !== null && c !== undefined);
    return {
      date: time.slice(0, 10),
      high_f: round(body.calendarDayTemperatureMax[i]),
      low_f: round(body.calendarDayTemperatureMin[i]),
      condition: phrases[2 * i] || phrases[2 * i + 1] || "",
      precip_pct: values.length ? Math.max(...values) : null,
    };
  });
}

async function weatherCom(place, raw) {
  const q = `geocode=${place.lat.toFixed(3)},${place.lon.toFixed(3)}&units=e&language=en-US&format=json&apiKey=${WEATHER_COM_KEY}`;
  const [current, daily] = await Promise.all([
    getJson(`https://api.weather.com/v3/wx/observations/current?${q}`, raw),
    getJson(`https://api.weather.com/v3/wx/forecast/daily/7day?${q}`, raw),
  ]);
  return {
    current: {
      temp_f: round(current.temperature), feels_like_f: round(current.temperatureFeelsLike),
      condition: current.wxPhraseLong || "", humidity_pct: round(current.relativeHumidity),
      wind_mph: round(current.windSpeed), observed: current.validTimeLocal,
    },
    daily: twcDaily(daily),
  };
}

async function wunderground(place, raw) {
  const geo = `geocode=${place.lat.toFixed(3)},${place.lon.toFixed(3)}`;
  const key = `apiKey=${WUNDERGROUND_KEY}`;
  const [near, observation, daily] = await Promise.all([
    getJson(`https://api.weather.com/v3/location/near?${geo}&product=pws&format=json&${key}`, raw).catch(() => null),
    getJson(`https://api.weather.com/v3/wx/observations/current?${geo}&units=e&language=en-US&format=json&${key}`, raw),
    getJson(`https://api.weather.com/v3/wx/forecast/daily/5day?${geo}&units=e&language=en-US&format=json&${key}`, raw),
  ]);
  let station = null;
  for (const id of ((near && near.location && near.location.stationId) || []).slice(0, 8)) {
    try {
      const body = await getJson(`https://api.weather.com/v2/pws/observations/current?stationId=${id}&units=e&format=json&${key}`, raw);
      const obs = (body.observations || [])[0];
      if (obs && obs.imperial && obs.imperial.temp !== null && obs.imperial.temp !== undefined) { station = obs; break; }
    } catch { /* a station that's offline: try the next nearest */ }
  }
  let current;
  if (station) {
    const imp = station.imperial;
    current = {
      temp_f: round(imp.temp), feels_like_f: round(imp.temp >= 70 ? imp.heatIndex : imp.windChill),
      condition: observation.wxPhraseLong || "", humidity_pct: round(station.humidity),
      wind_mph: round(imp.windSpeed), observed: station.obsTimeLocal,
      station: `${station.neighborhood || "Nearby"} station (${station.stationID})`,
    };
  } else {
    current = {
      temp_f: round(observation.temperature), feels_like_f: round(observation.temperatureFeelsLike),
      condition: observation.wxPhraseLong || "", humidity_pct: round(observation.relativeHumidity),
      wind_mph: round(observation.windSpeed), observed: observation.validTimeLocal,
    };
  }
  return { current, daily: twcDaily(daily) };
}

async function openMeteo(place, raw) {
  const params = new URLSearchParams({
    latitude: place.lat, longitude: place.lon,
    current: "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code,is_day",
    daily: "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
    temperature_unit: "fahrenheit", wind_speed_unit: "mph", timezone: "auto", forecast_days: FORECAST_DAYS,
  });
  const body = await getJson(`https://api.open-meteo.com/v1/forecast?${params}`, raw);
  const c = body.current, d = body.daily;
  return {
    current: {
      temp_f: round(c.temperature_2m), feels_like_f: round(c.apparent_temperature),
      condition: WMO_CODES[c.weather_code] || `Code ${c.weather_code}`, humidity_pct: round(c.relative_humidity_2m),
      wind_mph: round(c.wind_speed_10m), observed: c.time,
    },
    daily: d.time.map((date, i) => ({
      date, high_f: round(d.temperature_2m_max[i]), low_f: round(d.temperature_2m_min[i]),
      condition: WMO_CODES[d.weather_code[i]] || "", precip_pct: round(d.precipitation_probability_max[i]),
    })),
  };
}

async function noaa(place, raw) {
  const headers = { Accept: "application/geo+json" };
  const points = await getJson(`https://api.weather.gov/points/${place.lat.toFixed(4)},${place.lon.toFixed(4)}`, raw, headers);
  const [forecast, stations] = await Promise.all([
    getJson(points.properties.forecast, raw, headers),
    getJson(points.properties.observationStations, raw, headers),
  ]);
  let current = null;
  for (const feature of (stations.features || []).slice(0, NOAA_STATIONS_TO_TRY)) {
    const [lon, lat] = feature.geometry.coordinates;
    const miles = milesBetween(place.lat, place.lon, lat, lon);
    if (miles > NOAA_MAX_STATION_MILES) continue;
    const id = feature.properties.stationIdentifier;
    let observation;
    try {
      observation = await getJson(`https://api.weather.gov/stations/${id}/observations/latest`, raw, headers);
    } catch { continue; }
    const p = observation.properties;
    if (!p.temperature || p.temperature.value === null) continue;
    const feels = (p.heatIndex && p.heatIndex.value) ?? (p.windChill && p.windChill.value) ?? p.temperature.value;
    current = {
      temp_f: fFromC(p.temperature.value), feels_like_f: fFromC(feels), condition: p.textDescription || "",
      humidity_pct: round(p.relativeHumidity && p.relativeHumidity.value),
      wind_mph: mphFromKmh(p.windSpeed && p.windSpeed.value), observed: p.timestamp,
      station: `${feature.properties.name || "Nearby"} station (${id}, ${miles.toFixed(0)} mi)`,
    };
    break;
  }
  const days = new Map();
  for (const period of forecast.properties.periods) {
    const date = period.startTime.slice(0, 10);
    if (!days.has(date)) days.set(date, { date, high_f: null, low_f: null, condition: "", precip_pct: null });
    const day = days.get(date);
    const chance = period.probabilityOfPrecipitation && period.probabilityOfPrecipitation.value;
    if (chance !== null && chance !== undefined) day.precip_pct = Math.max(day.precip_pct || 0, chance);
    if (period.isDaytime) { day.high_f = period.temperature; day.condition = period.shortForecast; }
    else { day.low_f = period.temperature; day.condition = day.condition || period.shortForecast; }
  }
  if (!current) {
    const first = forecast.properties.periods[0];
    current = {
      temp_f: first.temperature, feels_like_f: null, condition: first.shortForecast, humidity_pct: null,
      wind_mph: null, observed: first.startTime, station: "Forecast; no station nearby",
    };
  }
  return { current, daily: [...days.values()].slice(0, FORECAST_DAYS) };
}

const SOURCES = [
  { source: "weather_com", label: "Weather.com", fetch: weatherCom,
    page: (p) => `https://weather.com/weather/today/l/${p.zip}:4:US` },
  { source: "wunderground", label: "Weather Underground", fetch: wunderground,
    page: (p) => `https://www.wunderground.com/weather/us/${p.state.toLowerCase()}/${slug(p.city)}/${p.zip}` },
  { source: "open_meteo", label: "Open-Meteo", fetch: openMeteo,
    page: (p) => `https://open-meteo.com/en/docs?latitude=${p.lat}&longitude=${p.lon}` },
  { source: "noaa", label: "NOAA", fetch: noaa,
    page: (p) => `https://forecast.weather.gov/MapClick.php?lat=${p.lat}&lon=${p.lon}` },
  { source: "google", label: "Google", fetch: null,
    page: (p) => `https://www.google.com/search?q=weather+${p.zip}&hl=en&gl=us` },
  { source: "yahoo", label: "Yahoo", fetch: null,
    page: (p) => `https://weather.yahoo.com/us/${p.state.toLowerCase()}/${slug(p.city)}/` },
];

// The date most sources start their forecast on: the place's local today.
function consensusToday(results) {
  const counts = new Map();
  for (const r of results) if (r.daily.length) counts.set(r.daily[0].date, (counts.get(r.daily[0].date) || 0) + 1);
  let best = null;
  for (const [date, count] of counts) {
    if (best === null || count > counts.get(best) || (count === counts.get(best) && date > best)) best = date;
  }
  return best;
}

// Returns { report, raw }: the report in the hourly job's shape, and each source's payloads.
export async function lookupLive(zip, trackedZips) {
  const place = await lookupZip(zip);
  const raw = {};
  const notHere = `Only on the zip codes this site updates every hour (${trackedZips.join(", ")}): it doesn't let a web page read it directly.`;
  const results = await Promise.all(SOURCES.map(async (spec) => {
    const base = { source: spec.source, label: spec.label, page_url: spec.page(place), ok: false, error: null, current: null, daily: [] };
    if (!spec.fetch) return { ...base, error: notHere, unavailable: true };
    const payloads = {};
    raw[spec.source] = { page_url: base.page_url, payloads };
    try {
      const { current, daily } = await spec.fetch(place, payloads);
      return { ...base, ok: true, current, daily };
    } catch (error) {
      return { ...base, error: `${spec.label} could not be read: ${error.message}` };
    }
  }));
  const ok = results.filter((r) => r.ok);
  const today = consensusToday(ok);
  const dates = [...new Set(ok.flatMap((r) => r.daily.map((d) => d.date)))]
    .filter((d) => today !== null && d >= today).sort().slice(0, FORECAST_DAYS);
  const forecast = dates.map((date) => ({
    date,
    by_source: Object.fromEntries(ok.flatMap((r) => r.daily.filter((d) => d.date === date).map((d) => [r.source, d]))),
  }));
  return {
    report: {
      zip, place: { city: place.city, state: place.state, lat: place.lat, lon: place.lon },
      fetched_at: Date.now() / 1000, today, live: true,
      sources: results.map(({ unavailable, ...r }) => ({ ...r, unavailable: Boolean(unavailable) })),
      forecast,
    },
    raw,
  };
}
