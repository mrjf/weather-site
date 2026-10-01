"""Build the published (GitHub Pages) weather site's data, on GitHub Actions.

Runs the same six-source lookup the app's own server does, for each zip code in
``WEATHER_PAGES_ZIPS`` (comma-separated), and writes into ``SITE_DIR``:

- ``data.json``: every tracked zip's report (the sapi data file the page and
  agents read; see ``pages/query.js`` and ``pages/schema.json``).
- ``raw/<zip>.json``: each source's untouched payloads behind that report, for
  the page's "Raw data" links.

Usage (what the published repository's workflow runs)::

    SITE_DIR=_site WEATHER_PAGES_ZIPS=94930,10001 python -m weather.pages_build
"""

import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from weather import runner, sources

logger = logging.getLogger("weather.pages_build")

ZIP_PATTERN = re.compile(r"^\d{5}$")
MAX_ZIPS = 20


def parse_zips(value: str) -> list[str]:
    zips = [z.strip() for z in value.split(",") if z.strip()]
    bad = [z for z in zips if not ZIP_PATTERN.match(z)]
    if bad or not zips:
        raise SystemExit(
            f"WEATHER_PAGES_ZIPS must be 5-digit zip codes separated by commas (got {value!r})"
        )
    return list(dict.fromkeys(zips))[:MAX_ZIPS]


def build(site_dir: Path, zips: list[str]) -> dict[str, Any]:
    # The app's snapshot store is not wanted here; send it somewhere disposable.
    runner.DATA_DIR = Path(tempfile.mkdtemp(prefix="weather-pages-"))
    # On Actions there is no Fortress browser: let Playwright use the Chromium it installed.
    if not Path(sources.FORTRESS_EXECUTABLE).exists():
        sources.FORTRESS_EXECUTABLE = None  # type: ignore[assignment]

    reports: dict[str, Any] = {}
    (site_dir / "raw").mkdir(parents=True, exist_ok=True)
    for zip_code in zips:
        report = runner.build_report(zip_code)
        reports[zip_code] = report
        raw = _raw_from_snapshot(zip_code)
        (site_dir / "raw" / f"{zip_code}.json").write_text(json.dumps(raw, indent=1))
        answered = sum(1 for s in report["sources"] if s["ok"])
        logger.info(
            "%s %s: %d/%d sources answered",
            zip_code,
            report["place"]["city"],
            answered,
            len(report["sources"]),
        )
        for source in report["sources"]:
            if not source["ok"]:
                logger.info("  %s: %s", source["label"], source["error"])
    data = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "default_zip": zips[0],
        "zips": reports,
    }
    (site_dir / "data.json").write_text(json.dumps(data, separators=(",", ":")))
    return data


def _raw_from_snapshot(zip_code: str) -> dict[str, Any]:
    """Every source's raw payloads from the snapshot ``build_report`` just saved."""
    snapshots = sorted((runner.DATA_DIR / "snapshots" / zip_code).glob("*.json"))
    if not snapshots:
        return {}
    return json.loads(snapshots[-1].read_text()).get("raw", {})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    site_dir = Path(os.environ.get("SITE_DIR", "_site"))
    zips = parse_zips(os.environ.get("WEATHER_PAGES_ZIPS", "94930"))
    data = build(site_dir, zips)
    if not any(s["ok"] for report in data["zips"].values() for s in report["sources"]):
        # Nothing answered at all: fail the run so the old site stays up instead.
        sys.exit("No weather source answered for any zip code")


if __name__ == "__main__":
    main()
