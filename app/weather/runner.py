"""Local weather from Weather.com, Weather Underground and Open-Meteo.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/weather/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/weather/`` but
  honors the ``WEATHER_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``WEATHER_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://weather.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import json
import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_file
from flask.typing import ResponseReturnValue
from werkzeug.serving import run_simple

from weather import sources

logger = logging.getLogger("weather")

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/weather/`` but is overridable via the ``WEATHER_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/weather/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("WEATHER_DATA_DIR", "data/.apps/weather"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``WEATHER_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("WEATHER_PORT", "8080"))

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path(
    "system/apps/system_interface/imbue/system_interface/static/_static"
)
SHELL_STATIC_MODULE_NAMES = ("app_contract.js", "context_menu.js")

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT = """<script type="module">
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
</script>"""

app = Flask("weather", static_folder=None)

ASSETS_DIR = Path(__file__).parent / "assets"
DEFAULT_ZIP = "94930"
# A repeat request for the same zip within this window is served from the last fetch.
# Just under the page's hourly reload, so each hourly reload gets fresh readings.
CACHE_SECONDS = 55 * 60
ZIP_PATTERN = re.compile(r"^\d{5}$")
# Every fetch is kept as a snapshot (the report plus each source's raw payloads),
# named for its UTC time; these bound how many and how old.
SNAPSHOT_NAME_PATTERN = re.compile(r"^\d{8}T\d{6}\.json$")
MAX_SNAPSHOTS_PER_ZIP = 200
SNAPSHOT_MAX_AGE_DAYS = 30

_cache_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}
# Writing a snapshot and pruning old ones happen together, one request at a time,
# so a prune never removes a zip's folder while another request writes into it.
_snapshot_lock = threading.Lock()


def _settings_path() -> Path:
    return DATA_DIR / "settings.json"


def load_saved_zip() -> str:
    try:
        saved = json.loads(_settings_path().read_text())["zip"]
    except (OSError, ValueError, KeyError):
        return DEFAULT_ZIP
    return saved if ZIP_PATTERN.match(str(saved)) else DEFAULT_ZIP


def save_zip(zip_code: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _settings_path().write_text(json.dumps({"zip": zip_code}))


def _fetch_one(source: str, place: sources.Place) -> sources.SourceResult:
    label, page_url_for, fetcher = sources.FETCHERS[source]
    try:
        return fetcher(place)
    except sources.HttpStatusError as error:
        message = f"{label} refused the request (HTTP {error.status})"
    except Exception as error:  # noqa: BLE001 -- one source failing must not sink the others
        message = f"{label} could not be read: {error}"
    logger.warning("%s fetch failed for %s: %s", source, place.zip_code, message)
    return sources.SourceResult(
        source=source,
        label=label,
        page_url=page_url_for(place),
        ok=False,
        error=message,
    )


def _consensus_today(results: list[sources.SourceResult]) -> str | None:
    """The date most sources start their forecast on: the place's local today.

    One source lagging behind (a cached page still on yesterday just after
    midnight) must not pull an extra, already-past day into the board.
    """
    first_days = Counter(result.daily[0]["date"] for result in results if result.daily)
    if not first_days:
        return None
    # On a tie, the later date: a lagging source is the likelier one to be wrong.
    return max(first_days, key=lambda day: (first_days[day], day))


def build_report(zip_code: str) -> dict[str, Any]:
    place = sources.lookup_zip(zip_code)
    with ThreadPoolExecutor(max_workers=len(sources.FETCHERS)) as pool:
        results = list(
            pool.map(lambda source: _fetch_one(source, place), sources.FETCHERS)
        )
    fetched_at = time.time()
    ok_results = [result for result in results if result.ok and result.current]
    today = _consensus_today(ok_results)
    dates = sorted(
        {
            day["date"]
            for result in ok_results
            for day in result.daily
            if today is not None and day["date"] >= today
        }
    )[: sources.FORECAST_DAYS]
    # One column per day; each source's own reading for that day, if it has one.
    forecast = [
        {
            "date": date,
            "by_source": {
                result.source: day
                for result in ok_results
                for day in result.daily
                if day["date"] == date
            },
        }
        for date in dates
    ]
    report = {
        "zip": zip_code,
        "place": {
            "city": place.city,
            "state": place.state_code,
            "lat": place.latitude,
            "lon": place.longitude,
        },
        "fetched_at": fetched_at,
        "today": today,
        "sources": [
            {
                "source": result.source,
                "label": result.label,
                "page_url": result.page_url,
                "ok": result.ok,
                "error": result.error,
                "current": result.current,
                "daily": result.daily,
            }
            for result in results
        ],
        "forecast": forecast,
    }
    save_snapshot(report, results)
    return report


def _snapshot_dir(zip_code: str) -> Path:
    return DATA_DIR / "snapshots" / zip_code


def _snapshots_in(directory: Path) -> list[Path]:
    """A zip's snapshot files, oldest first (their names are their UTC times)."""
    if not directory.is_dir():
        return []
    return sorted(
        path for path in directory.iterdir() if SNAPSHOT_NAME_PATTERN.match(path.name)
    )


def save_snapshot(report: dict[str, Any], results: list[sources.SourceResult]) -> Path:
    """Keep the fetch: the report as shown plus each source's untouched payloads, then prune old ones."""
    directory = _snapshot_dir(report["zip"])
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(report["fetched_at"]))
    snapshot = {
        "report": report,
        "raw": {
            result.source: {"page_url": result.page_url, "payloads": result.raw}
            for result in results
        },
    }
    path = directory / f"{stamp}.json"
    with _snapshot_lock:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=1))
        prune_snapshots(report["fetched_at"])
    return path


def prune_snapshots(now: float) -> None:
    """Hold the archive to the newest MAX_SNAPSHOTS_PER_ZIP per zip, none older than SNAPSHOT_MAX_AGE_DAYS.

    A snapshot holds a fetch's report and its raw payloads together, so both age
    out at once. A zip left with no snapshots loses its folder too.
    """
    root = DATA_DIR / "snapshots"
    if not root.is_dir():
        return
    oldest_kept = time.strftime(
        "%Y%m%dT%H%M%S", time.gmtime(now - SNAPSHOT_MAX_AGE_DAYS * 24 * 3600)
    )
    for directory in root.iterdir():
        if not (directory.is_dir() and ZIP_PATTERN.match(directory.name)):
            continue
        snapshots = _snapshots_in(directory)
        keep = {
            path
            for path in snapshots[-MAX_SNAPSHOTS_PER_ZIP:]
            if path.stem >= oldest_kept
        }
        for path in snapshots:
            if path not in keep:
                path.unlink(missing_ok=True)
        if not any(directory.iterdir()):
            directory.rmdir()


def latest_raw(zip_code: str, source: str) -> dict[str, Any] | None:
    snapshots = _snapshots_in(_snapshot_dir(zip_code))
    if not snapshots:
        return None
    return json.loads(snapshots[-1].read_text())["raw"].get(source)


@app.route("/")
def index() -> Response:
    page = (ASSETS_DIR / "index.html").read_text()
    return Response(
        page.replace("<!-- SHELL_PAGE_SCRIPT -->", SHELL_PAGE_SCRIPT),
        mimetype="text/html",
    )


@app.route("/api/weather")
def weather() -> ResponseReturnValue:
    zip_code = (request.args.get("zip") or load_saved_zip()).strip()
    if not ZIP_PATTERN.match(zip_code):
        return jsonify({"error": "Enter a 5-digit US zip code."}), 400
    force = request.args.get("refresh") == "1"
    with _cache_lock:
        cached = _cache.get(zip_code)
    if (
        cached is not None
        and not force
        and time.time() - cached["fetched_at"] < CACHE_SECONDS
    ):
        save_zip(zip_code)
        return jsonify(cached)
    try:
        report = build_report(zip_code)
    except sources.HttpStatusError as error:
        if error.status == 404:
            return jsonify(
                {"error": f"{zip_code} isn't a US zip code we can find."}
            ), 404
        return jsonify(
            {"error": f"Couldn't look up {zip_code} (HTTP {error.status})."}
        ), 502
    except (sources.WeatherError, OSError, KeyError, IndexError, ValueError) as error:
        logger.exception("lookup failed for %s", zip_code)
        return jsonify({"error": f"Couldn't look up {zip_code}: {error}"}), 502
    with _cache_lock:
        # Drop stale entries so the cache only ever holds the last few minutes' zips.
        for stale in [
            key
            for key, entry in _cache.items()
            if report["fetched_at"] - entry["fetched_at"] >= CACHE_SECONDS
        ]:
            del _cache[stale]
        _cache[zip_code] = report
    save_zip(zip_code)
    return jsonify(report)


@app.route("/api/raw/<zip_code>/<source>")
def raw(zip_code: str, source: str) -> Response:
    if not ZIP_PATTERN.match(zip_code) or source not in sources.FETCHERS:
        abort(404)
    record = latest_raw(zip_code, source)
    if record is None:
        abort(404)
    return jsonify(record)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
