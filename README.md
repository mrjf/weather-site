# Weather

Live site: https://mrjf.github.io/weather-site/

Published from an Imbue Studio workspace app (`weather`) by its Publish to GitHub Pages app.

## How it works

- `site/` is the page itself: plain files GitHub Pages serves as they are.
- `.github/workflows/pages.yml` runs on GitHub Actions on every push and on the schedule `23 * * * *` (UTC): it runs `python -m weather.pages_build` (code in `app/`) to write the site's data, then publishes it.

Settings chosen when it was published:

- `WEATHER_PAGES_ZIPS` = `94930`

## Data for agents (sapi)

This site implements [sapi](https://github.com/deshitifai/sapi): `data.json` holds all of its data and `query.js` the query
logic the page itself uses, so any page URL (query string and all) can be answered locally:

```console
$ sapi 'https://mrjf.github.io/weather-site/?source=noaa,open_meteo'
```
