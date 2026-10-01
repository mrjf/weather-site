// sapi query function for the published weather widget (https://github.com/deshitifai/sapi).
// Pure ECMAScript: no imports, no I/O, no host APIs. The page uses this same file.
//
// params (the page's own URL parameters):
//   zip=<5 digits>          which zip code (default: the site's first tracked zip)
//   source=<id>[,<id>...]   only these sources (weather_com, wunderground, open_meteo, noaa, google, yahoo);
//                           may also repeat: ?source=noaa&source=google
//
// Returns the report for that zip: place, when it was fetched, each source's current
// conditions and daily forecast, and the forecast laid out by day. A zip the hourly job
// does not track returns { zip, tracked: false, tracked_zips } (the page then looks it up live).
export default function query(data, params) {
  const pick = (value) => (Array.isArray(value) ? value[0] : value);
  const zips = data && data.zips ? data.zips : {};
  const trackedZips = Object.keys(zips).sort();
  const zip = String(pick(params.zip) || data.default_zip || trackedZips[0] || "").trim();
  const report = zips[zip];
  if (!report) {
    return { zip, tracked: false, tracked_zips: trackedZips };
  }
  let wanted = null;
  if (params.source) {
    const raw = Array.isArray(params.source) ? params.source : [params.source];
    wanted = new Set(
      raw.flatMap((value) => String(value).split(",")).map((value) => value.trim().toLowerCase()).filter(Boolean),
    );
  }
  const keep = (id) => wanted === null || wanted.has(id);
  return {
    zip: report.zip,
    tracked: true,
    place: report.place,
    fetched_at: report.fetched_at,
    today: report.today,
    sources: report.sources.filter((s) => keep(s.source)),
    forecast: report.forecast.map((day) => ({
      date: day.date,
      by_source: Object.fromEntries(Object.entries(day.by_source).filter(([id]) => keep(id))),
    })),
  };
}
