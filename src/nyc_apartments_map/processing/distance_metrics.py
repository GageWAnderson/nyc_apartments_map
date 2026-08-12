"""Per-NTA travel distance/duration to YAML-configured destinations via Google Maps.

Reads ``configs/distance_metrics.yaml``, geocodes each destination once via the
Google Geocoding API, then calls the Google Maps Distance Matrix API with all
NTA representative points as origins. Produces two float columns per metric on
``nta_indicators.parquet``: ``<name>_m`` (meters) and ``<name>_s`` (seconds).

This is a per-NTA contextual metric (one row per NTA, not per listing), so it
bypasses :data:`COMMON_SCHEMA` and the :class:`DatasetLoader` abstraction on
purpose -- mirroring :mod:`nyc_apartments_map.processing.geo_sources`. Missing
config, missing API key, or API errors are non-fatal (logs + returns empty).

Google Maps ToS prohibits caching route results beyond 30 days; the cache file
records a fetch timestamp and is re-used only within that window.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import geopandas as gpd
import httpx
import pandas as pd
import yaml
from pydantic import BaseModel, ValidationError, field_validator

from nyc_apartments_map.config import Settings
from nyc_apartments_map.processing.geo_sources import PointSourceFunc

logger = logging.getLogger(__name__)

#: Max origins per Distance Matrix API call. Google's standard limit is 100
#: elements/request, but transit mode with departure_time is capped at 25
#: origins; 25 is the safe universal page size across all modes/params.
_DM_PAGE_SIZE = 25

#: Google Maps Distance Matrix endpoint.
_DM_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

#: Google Maps Geocoding endpoint.
_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

#: Cache TTL (seconds). Google Maps ToS prohibits caching route results beyond
#: 30 days; we re-fetch when the cache file is older than this.
_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60

#: Valid travel modes for the Distance Matrix API.
_VALID_MODES: frozenset[str] = frozenset({"driving", "walking", "bicycling", "transit"})

#: Valid unit systems. (We always parse the `value` field, which is meters/
#: seconds regardless; this only affects the human-readable `text` field.)
_VALID_UNITS: frozenset[str] = frozenset({"metric", "imperial"})

#: Weekday name -> ISO weekday index (Monday=0).
_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class MetricConfig(BaseModel):
    """One distance-from-address metric entry from the YAML config.

    Each entry produces two columns on ``nta_indicators.parquet``:
    ``<name>_m`` (travel distance in meters) and ``<name>_s`` (duration in seconds).
    """

    name: str
    destination: str
    mode: str = "transit"
    departure_time: str | None = None
    units: str = "metric"

    @field_validator("name")
    @classmethod
    def _name_valid(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name must be non-empty")
        v = v.strip()
        if not v.isidentifier():
            raise ValueError("name must be a valid Python identifier")
        return v

    @field_validator("destination")
    @classmethod
    def _destination_valid(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("destination must be non-empty")
        return v.strip()

    @field_validator("mode")
    @classmethod
    def _mode_valid(cls, v: str) -> str:
        if v not in _VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}, got {v!r}")
        return v

    @field_validator("units")
    @classmethod
    def _units_valid(cls, v: str) -> str:
        if v not in _VALID_UNITS:
            raise ValueError(f"units must be one of {sorted(_VALID_UNITS)}, got {v!r}")
        return v

    @field_validator("departure_time")
    @classmethod
    def _departure_time_valid(cls, v: str | None) -> str | None:
        if v is None or v == "now":
            return v
        parts = v.split("_")
        if len(parts) != 3 or parts[0] != "next":
            raise ValueError(
                "departure_time must be 'now' or 'next_<weekday>_<HH:MM>', "
                f"e.g. 'next_monday_09:00'; got {v!r}"
            )
        wday = parts[1].lower()
        if wday not in _WEEKDAYS:
            raise ValueError(f"unknown weekday {wday!r} in departure_time")
        time_parts = parts[2].split(":")
        if len(time_parts) != 2:
            raise ValueError(f"invalid time format in departure_time: {parts[2]!r}")
        try:
            hh, mm = int(time_parts[0]), int(time_parts[1])
        except ValueError as exc:
            raise ValueError(f"invalid time in departure_time: {parts[2]!r}") from exc
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"time out of range in departure_time: {parts[2]!r}")
        return v

    def cache_key(self, nta_codes: list[str]) -> str:
        """Stable SHA1 hash of the metric config + NTA origin list."""
        payload = json.dumps(
            {
                "name": self.name,
                "destination": self.destination,
                "mode": self.mode,
                "departure_time": self.departure_time,
                "units": self.units,
                "nta_codes": nta_codes,
            },
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode()).hexdigest()


# --- Config loading ---------------------------------------------------------


def load_distance_config(settings: Settings) -> list[MetricConfig]:
    """Load and validate the distance metrics YAML config.

    Returns an empty list (and logs) if the file is missing, unreadable, or
    contains no valid entries. Invalid entries are skipped with a warning so
    one bad metric never blocks the rest.
    """
    path: Path = settings.distance_metrics_config_path
    if not path.exists():
        logger.info("Distance metrics config not found at %s; skipping.", path)
        return []
    try:
        with path.open() as fh:
            raw = yaml.safe_load(fh) or {}
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("Failed to parse %s: %s; skipping.", path, exc)
        return []
    if not isinstance(raw, dict):
        logger.warning("Distance metrics YAML root must be a mapping; skipping.")
        return []
    entries = raw.get("metrics", [])
    if not isinstance(entries, list):
        logger.warning("Distance metrics 'metrics' key must be a list; skipping.")
        return []
    configs: list[MetricConfig] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            logger.warning("Distance metrics entry %d is not a mapping; skipping.", i)
            continue
        try:
            configs.append(MetricConfig(**entry))
        except ValidationError as exc:
            logger.warning("Distance metrics entry %d invalid: %s; skipping.", i, exc)
    return configs


# --- Departure time ---------------------------------------------------------


def _resolve_departure_time(spec: str | None) -> int | None:
    """Resolve a departure_time spec to a unix timestamp, or None.

    - None: omit departure_time (traffic-independent duration)
    - "now": current unix timestamp
    - "next_<weekday>_<HH:MM>": next occurrence of that weekday at HH:MM local time
    """
    if spec is None:
        return None
    if spec == "now":
        return int(time.time())
    # Validated by MetricConfig._departure_time_valid, so structure is guaranteed.
    parts = spec.split("_")
    wday = _WEEKDAYS[parts[1].lower()]
    hh, mm = int(parts[2].split(":")[0]), int(parts[2].split(":")[1])
    now = datetime.now()
    days_ahead = (wday - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # always "next" week, not today
    target = now + timedelta(days=days_ahead)
    target = target.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return int(target.timestamp())


# --- NTA representative points ----------------------------------------------


def _nta_representative_points(boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """Return a DataFrame of (nta_code, latitude, longitude) using representative points.

    ``representative_point()`` is guaranteed to fall inside the polygon (unlike
    ``centroid``, which can fall outside concave shapes). Uses ``.to_numpy()``
    to avoid index-alignment issues.
    """
    rep = boundaries.geometry.representative_point()
    return pd.DataFrame(
        {
            "nta_code": boundaries["nta_code"].to_numpy(),
            "latitude": rep.y.to_numpy(),
            "longitude": rep.x.to_numpy(),
        }
    )


# --- Google API calls -------------------------------------------------------


def _geocode_destination(
    address: str, api_key: str, client: httpx.Client
) -> tuple[float, float] | None:
    """Geocode an address to (lat, lon) via Google Geocoding API.

    Returns None on any API error or empty result. One call per destination.
    """
    resp = client.get(_GEOCODE_URL, params={"address": address, "key": api_key})
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    if data.get("status") != "OK" or not data.get("results"):
        logger.warning("Geocoding failed for %r: status=%s", address, data.get("status"))
        return None
    loc = data["results"][0]["geometry"]["location"]
    return float(loc["lat"]), float(loc["lng"])


def _distance_matrix_page(
    origins: list[tuple[float, float]],
    dest_latlon: tuple[float, float],
    metric: MetricConfig,
    api_key: str,
    client: httpx.Client,
) -> list[dict[str, float | None]]:
    """Call Distance Matrix for up to 100 origins -> 1 destination.

    Returns a list of ``{"distance_m": float | None, "duration_s": float | None}``
    dicts, one per origin, aligned to the input order. None values indicate
    no route or API error for that origin.
    """
    origins_str = "|".join(f"{round(lat, 6)},{round(lon, 6)}" for lat, lon in origins)
    dest_str = f"{round(dest_latlon[0], 6)},{round(dest_latlon[1], 6)}"
    params: dict[str, str] = {
        "origins": origins_str,
        "destinations": dest_str,
        "mode": metric.mode,
        "units": metric.units,
        "key": api_key,
    }
    departure_ts = _resolve_departure_time(metric.departure_time)
    if departure_ts is not None:
        params["departure_time"] = str(departure_ts)
    resp = client.get(_DM_URL, params=params)
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    if data.get("status") != "OK":
        logger.warning("Distance Matrix status=%s for metric %r", data.get("status"), metric.name)
        return [{"distance_m": None, "duration_s": None}] * len(origins)
    rows = data.get("rows", [])
    if len(rows) != len(origins):
        logger.warning(
            "Distance Matrix row count mismatch: %d rows vs %d origins",
            len(rows),
            len(origins),
        )
        return [{"distance_m": None, "duration_s": None}] * len(origins)
    out: list[dict[str, float | None]] = []
    for row in rows:
        elements = row.get("elements", [])
        if not elements or elements[0].get("status") != "OK":
            out.append({"distance_m": None, "duration_s": None})
            continue
        el = elements[0]
        dist = el.get("distance", {}).get("value")
        dur = el.get("duration", {}).get("value")
        out.append(
            {
                "distance_m": float(dist) if dist is not None else None,
                "duration_s": float(dur) if dur is not None else None,
            }
        )
    return out


def _fetch_metric(
    metric: MetricConfig,
    origins: list[tuple[float, float]],
    api_key: str,
    client: httpx.Client,
) -> list[dict[str, float | None]]:
    """Geocode destination once, then page through Distance Matrix for all origins."""
    dest = _geocode_destination(metric.destination, api_key, client)
    if dest is None:
        return [{"distance_m": None, "duration_s": None}] * len(origins)
    all_results: list[dict[str, float | None]] = []
    for i in range(0, len(origins), _DM_PAGE_SIZE):
        page = origins[i : i + _DM_PAGE_SIZE]
        all_results.extend(_distance_matrix_page(page, dest, metric, api_key, client))
    return all_results


# --- Cache ------------------------------------------------------------------


def _load_cache(cache_path: Path) -> dict[str, Any] | None:
    """Load a cache file, or None if missing/corrupt/expired.

    Honors Google's 30-day route-cache limit: if the recorded ``fetched_at``
    timestamp is older than :data:`_CACHE_TTL_SECONDS`, the cache is treated
    as a miss.
    """
    if not cache_path.exists():
        return None
    try:
        with cache_path.open() as fh:
            data: dict[str, Any] = json.load(fh)
    except (json.JSONDecodeError, OSError):
        logger.warning("Cache file corrupt at %s; ignoring.", cache_path)
        return None
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, str):
        return None
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age = datetime.now(UTC) - ts
    if age.total_seconds() > _CACHE_TTL_SECONDS:
        logger.info("Cache expired (>30 days) at %s; re-fetching.", cache_path.name)
        return None
    return data


def _write_cache(cache_path: Path, payload: dict[str, Any]) -> None:
    """Write a cache file with the current UTC timestamp."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload["fetched_at"] = datetime.now(UTC).isoformat()
    with cache_path.open("w") as fh:
        json.dump(payload, fh)


# --- Main entry point -------------------------------------------------------


def distance_metrics_nta(settings: Settings, *, boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """Compute per-NTA travel distance/duration for each YAML-configured metric.

    Returns a wide DataFrame keyed by ``nta_code`` with two float columns per
    metric: ``<name>_m`` (meters) and ``<name>_s`` (seconds). Non-fatal skip
    (returns an empty ``nta_code``-only frame) if the config is missing, the
    API key is unset, or the boundaries frame is empty.

    Per-metric errors (geocoding failure, API error) produce all-NaN columns
    for that metric and continue to the next one, so one bad destination never
    blocks the rest.
    """
    if not settings.google_api_key:
        logger.warning("GOOGLE_API_KEY not set; skipping distance metrics.")
        return pd.DataFrame(columns=["nta_code"])

    metrics = load_distance_config(settings)
    if not metrics:
        logger.info("No distance metrics configured; skipping.")
        return pd.DataFrame(columns=["nta_code"])

    if boundaries.empty:
        logger.warning("No NTA boundaries; skipping distance metrics.")
        return pd.DataFrame(columns=["nta_code"])

    points = _nta_representative_points(boundaries)
    nta_codes: list[str] = points["nta_code"].tolist()
    origins: list[tuple[float, float]] = list(
        zip(points["latitude"].tolist(), points["longitude"].tolist(), strict=True)
    )

    results: pd.DataFrame = pd.DataFrame({"nta_code": nta_codes})
    cache_dir = settings.distance_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=30.0) as client:
        for metric in metrics:
            cache_path = cache_dir / f"{metric.cache_key(nta_codes)}.json"
            cached = _load_cache(cache_path)
            if cached is not None and "results" in cached:
                logger.info("Cache hit for metric %r.", metric.name)
                metric_results: list[dict[str, Any]] = cached["results"]
            else:
                try:
                    metric_results = _fetch_metric(metric, origins, settings.google_api_key, client)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "API call failed for metric %r: %s; leaving NaN.", metric.name, exc
                    )
                    results[f"{metric.name}_m"] = [None] * len(nta_codes)
                    results[f"{metric.name}_s"] = [None] * len(nta_codes)
                    continue
                _write_cache(
                    cache_path,
                    {
                        "config": metric.model_dump(),
                        "nta_codes": nta_codes,
                        "results": metric_results,
                    },
                )

            results[f"{metric.name}_m"] = [r.get("distance_m") for r in metric_results]
            results[f"{metric.name}_s"] = [r.get("duration_s") for r in metric_results]

    return results


#: All distance-metric functions, called by ``build_nta_indicators``. Each
#: returns a wide DataFrame keyed by ``nta_code`` with float columns
#: (``<name>_m`` / ``<name>_s`` per YAML-configured metric). Mirrors the
#: :data:`geo_sources.POINT_SOURCE_FUNCS` pattern but kept separate so the
#: int64 coercion in ``aggregate.py`` doesn't apply to float distances.
DISTANCE_METRIC_FUNCS: list[PointSourceFunc] = [distance_metrics_nta]
