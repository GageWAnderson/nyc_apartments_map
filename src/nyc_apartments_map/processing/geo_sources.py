"""Map non-listing point sources onto NTAs (Strategy A: point-in-polygon).

Each function reads one raw source, assigns each point an ``nta_code`` via
:func:`nyc_apartments_map.geo.boundaries.assign_nta`, and aggregates per-NTA
metrics. Results are keyed by ``nta_code`` and left-merged onto the listing-
derived indicators table by :func:`nyc_apartments_map.processing.aggregate.build_nta_indicators`.

These sources are contextual (not listings) and bypass :data:`COMMON_SCHEMA` /
the :class:`DatasetLoader` abstraction on purpose — forcing them through the
listing schema would be a category error. Missing raw files are non-fatal:
the function logs and returns an empty frame.

Big CSVs (hpd, 311, nypd, pluto) are read in chunks with ``usecols`` to keep
memory bounded.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Protocol

import geopandas as gpd
import pandas as pd

from nyc_apartments_map.config import Settings
from nyc_apartments_map.geo.boundaries import assign_nta

logger = logging.getLogger(__name__)

#: OSM ``amenity`` tag values treated as nightlife venues.
_NIGHTLIFE_AMENITIES: frozenset[str] = frozenset({"bar", "pub", "nightclub", "biergarten"})

#: Read chunk size for large CSVs (rows per chunk).
_CHUNKSIZE = 200_000

_WKT_POINT_RE = re.compile(r"POINT\s*\(\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)", re.IGNORECASE)


class PointSourceFunc(Protocol):
    """Signature of a per-NTA point-source metric function."""

    def __call__(self, settings: Settings, *, boundaries: gpd.GeoDataFrame) -> pd.DataFrame: ...


# --- Parsers -----------------------------------------------------------------


def parse_wkt_point(wkt: str) -> tuple[float, float] | None:
    """Parse a WKT ``POINT (lon lat)`` string into a ``(lat, lon)`` tuple.

    Returns ``None`` if the string is not a parseable point. Liquor licenses
    store their geocoding as WKT in the ``Georeference`` column.
    """
    if not isinstance(wkt, str) or not wkt.strip():
        return None
    m = _WKT_POINT_RE.search(wkt)
    if m is None:
        return None
    lon, lat = float(m.group(1)), float(m.group(2))
    return lat, lon


def parse_osm_elements(elements: list[dict[str, Any]]) -> pd.DataFrame:
    """Filter OSM elements to nightlife ``node``s and return a points frame.

    Drops ``way``/``relation`` elements and nodes whose ``amenity`` tag is not
    in :data:`_NIGHTLIFE_AMENITIES`. Returns columns ``latitude``,
    ``longitude``, ``name``, ``amenity``.
    """
    rows: list[dict[str, Any]] = []
    for el in elements:
        if el.get("type") != "node":
            continue
        tags = el.get("tags") or {}
        amenity = tags.get("amenity")
        if amenity not in _NIGHTLIFE_AMENITIES:
            continue
        rows.append(
            {
                "latitude": el.get("lat"),
                "longitude": el.get("lon"),
                "name": tags.get("name", ""),
                "amenity": amenity,
            }
        )
    return pd.DataFrame(rows, columns=["latitude", "longitude", "name", "amenity"])


# --- Helpers -----------------------------------------------------------------


def _coerce_coords(df: pd.DataFrame) -> pd.DataFrame:
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    return df


def _points_to_nta(df: pd.DataFrame, boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """Assign ``nta_code`` to a points frame and drop unmatched rows.

    Adds ``nta_code``/``cdta_code`` (NA) if absent, coerces coordinates, and
    runs :func:`assign_nta`. Rows that fall outside every polygon are dropped.
    """
    if df.empty:
        return df
    pts = df.copy()
    if "nta_code" not in pts.columns:
        pts["nta_code"] = pd.NA
    if "cdta_code" not in pts.columns:
        pts["cdta_code"] = pd.NA
    pts = _coerce_coords(pts)
    pts = assign_nta(pts, boundaries)
    return pts.dropna(subset=["nta_code"]).reset_index(drop=True)


def _chunked_points_to_nta(
    path: Path,
    usecols: list[str],
    boundaries: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """Read a CSV in chunks, assign NTAs, and return matched rows (``nta_code`` + carry cols).

    ``usecols`` must include ``latitude``/``longitude`` plus any metric carry
    columns. Unmatched points are dropped per chunk to bound memory.
    """
    carry = [c for c in usecols if c not in ("latitude", "longitude")]
    out_cols = ["nta_code", *carry]
    frames: list[pd.DataFrame] = []
    for chunk in pd.read_csv(path, usecols=usecols, chunksize=_CHUNKSIZE, low_memory=False):
        matched = _points_to_nta(chunk, boundaries)
        if not matched.empty:
            frames.append(matched[out_cols])
    if not frames:
        return pd.DataFrame(columns=out_cols)
    return pd.concat(frames, ignore_index=True)


def _empty(*columns: str) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


# --- Per-source functions ----------------------------------------------------


def hpd_violations_nta_metrics(settings: Settings, *, boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """HPD violations per NTA: total count and rent-impairing count (``rentimpairing == "Y"``)."""
    path = settings.raw_dir / "hpd_violations" / "hpd_violations.csv"
    if not path.exists():
        logger.warning("HPD violations not found at %s; skipping.", path)
        return _empty("nta_code", "hpd_violation_count", "hpd_rent_impairing_count")
    matched = _chunked_points_to_nta(path, ["latitude", "longitude", "rentimpairing"], boundaries)
    if matched.empty:
        return _empty("nta_code", "hpd_violation_count", "hpd_rent_impairing_count")
    matched["is_rent_impairing"] = (matched["rentimpairing"] == "Y").astype("int64")
    agg = matched.groupby("nta_code", as_index=False).agg(
        hpd_violation_count=("nta_code", "count"),
        hpd_rent_impairing_count=("is_rent_impairing", "sum"),
    )
    agg["hpd_rent_impairing_count"] = agg["hpd_rent_impairing_count"].astype("int64")
    return agg


def nyc_311_nta_metrics(settings: Settings, *, boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """311 service requests per NTA: total count."""
    path = settings.raw_dir / "nyc_311" / "nyc_311_2024_onwards.csv"
    if not path.exists():
        logger.warning("NYC 311 not found at %s; skipping.", path)
        return _empty("nta_code", "nyc311_count")
    matched = _chunked_points_to_nta(path, ["latitude", "longitude"], boundaries)
    if matched.empty:
        return _empty("nta_code", "nyc311_count")
    agg = matched.groupby("nta_code", as_index=False).agg(nyc311_count=("nta_code", "count"))
    agg["nyc311_count"] = agg["nyc311_count"].astype("int64")
    return agg


def nypd_complaints_nta_metrics(
    settings: Settings, *, boundaries: gpd.GeoDataFrame
) -> pd.DataFrame:
    """NYPD complaints per NTA: total count and felony count (``law_cat_cd == "FELONY"``)."""
    path = settings.raw_dir / "nypd_complaints" / "nypd_complaints_historic.csv"
    if not path.exists():
        logger.warning("NYPD complaints not found at %s; skipping.", path)
        return _empty("nta_code", "nypd_complaint_count", "nypd_felony_count")
    matched = _chunked_points_to_nta(path, ["latitude", "longitude", "law_cat_cd"], boundaries)
    if matched.empty:
        return _empty("nta_code", "nypd_complaint_count", "nypd_felony_count")
    matched["is_felony"] = (matched["law_cat_cd"] == "FELONY").astype("int64")
    agg = matched.groupby("nta_code", as_index=False).agg(
        nypd_complaint_count=("nta_code", "count"),
        nypd_felony_count=("is_felony", "sum"),
    )
    agg["nypd_felony_count"] = agg["nypd_felony_count"].astype("int64")
    return agg


def osm_nightlife_nta_metrics(settings: Settings, *, boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """OSM nightlife venues per NTA (bars, pubs, nightclubs, biergartens)."""
    path = settings.raw_dir / "osm_nightlife" / "osm_nightlife.json"
    if not path.exists():
        logger.warning("OSM nightlife not found at %s; skipping.", path)
        return _empty("nta_code", "nightlife_venue_count")
    with path.open() as fh:
        payload = json.load(fh)
    pts = parse_osm_elements(payload.get("elements", []))
    if pts.empty:
        return _empty("nta_code", "nightlife_venue_count")
    matched = _points_to_nta(pts, boundaries)
    if matched.empty:
        return _empty("nta_code", "nightlife_venue_count")
    agg = matched.groupby("nta_code", as_index=False).agg(
        nightlife_venue_count=("nta_code", "count")
    )
    agg["nightlife_venue_count"] = agg["nightlife_venue_count"].astype("int64")
    return agg


def nys_liquor_nta_metrics(settings: Settings, *, boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """Active liquor licenses per NTA, parsed from the ``Georeference`` WKT column."""
    path = settings.raw_dir / "nys_liquor" / "liquor_active_licenses.csv"
    if not path.exists():
        logger.warning("NYS liquor licenses not found at %s; skipping.", path)
        return _empty("nta_code", "liquor_license_count")
    raw = pd.read_csv(path, usecols=["Georeference"], low_memory=False)
    parsed = raw["Georeference"].apply(parse_wkt_point)
    pts = pd.DataFrame(
        {
            "latitude": [p[0] if p else pd.NA for p in parsed],
            "longitude": [p[1] if p else pd.NA for p in parsed],
        }
    )
    matched = _points_to_nta(pts, boundaries)
    if matched.empty:
        return _empty("nta_code", "liquor_license_count")
    agg = matched.groupby("nta_code", as_index=False).agg(
        liquor_license_count=("nta_code", "count")
    )
    agg["liquor_license_count"] = agg["liquor_license_count"].astype("int64")
    return agg


def furman_bbl_analysis_nta_metrics(
    settings: Settings, *, boundaries: gpd.GeoDataFrame
) -> pd.DataFrame:
    """Furman Center SHD BBL analysis per NTA: residential units and income-targeted units."""
    path = settings.raw_dir / "furman_housing" / "FC_SHD_bbl_analysis.csv"
    if not path.exists():
        logger.warning("Furman BBL analysis not found at %s; skipping.", path)
        return _empty("nta_code", "furman_res_units", "furman_subsidized_units")
    inc_cols = [f"inc_tar_units_{n}bed" for n in range(5)]
    usecols = ["latitude", "longitude", "res_units", *inc_cols]
    matched = _chunked_points_to_nta(path, usecols, boundaries)
    if matched.empty:
        return _empty("nta_code", "furman_res_units", "furman_subsidized_units")
    for c in ("res_units", *inc_cols):
        matched[c] = pd.to_numeric(matched[c], errors="coerce").fillna(0)
    matched["subsidized_units"] = matched[inc_cols].sum(axis=1)
    agg = matched.groupby("nta_code", as_index=False).agg(
        furman_res_units=("res_units", "sum"),
        furman_subsidized_units=("subsidized_units", "sum"),
    )
    agg["furman_res_units"] = agg["furman_res_units"].astype("int64")
    agg["furman_subsidized_units"] = agg["furman_subsidized_units"].astype("int64")
    return agg


def pluto_nta_metrics(settings: Settings, *, boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """PLUTO lots per NTA: residential units, total units, and lot area (sum)."""
    path = settings.raw_dir / "pluto" / "pluto.csv"
    if not path.exists():
        logger.warning("PLUTO not found at %s; skipping.", path)
        return _empty("nta_code", "pluto_res_units", "pluto_total_units", "pluto_lotarea")
    matched = _chunked_points_to_nta(
        path, ["latitude", "longitude", "unitsres", "unitstotal", "lotarea"], boundaries
    )
    if matched.empty:
        return _empty("nta_code", "pluto_res_units", "pluto_total_units", "pluto_lotarea")
    for c in ("unitsres", "unitstotal", "lotarea"):
        matched[c] = pd.to_numeric(matched[c], errors="coerce").fillna(0)
    agg = matched.groupby("nta_code", as_index=False).agg(
        pluto_res_units=("unitsres", "sum"),
        pluto_total_units=("unitstotal", "sum"),
        pluto_lotarea=("lotarea", "sum"),
    )
    agg["pluto_res_units"] = agg["pluto_res_units"].astype("int64")
    agg["pluto_total_units"] = agg["pluto_total_units"].astype("int64")
    agg["pluto_lotarea"] = agg["pluto_lotarea"].astype("int64")
    return agg


#: All point-source metric functions, called by ``build_nta_indicators``.
POINT_SOURCE_FUNCS: list[PointSourceFunc] = [
    hpd_violations_nta_metrics,
    nyc_311_nta_metrics,
    nypd_complaints_nta_metrics,
    osm_nightlife_nta_metrics,
    nys_liquor_nta_metrics,
    furman_bbl_analysis_nta_metrics,
    pluto_nta_metrics,
]


# --- Strategy B bridge (documented, not in POINT_SOURCE_FUNCS) ---------------


def pluto_bbl_lookup(settings: Settings) -> pd.DataFrame:
    """Build a ``bbl -> (latitude, longitude)`` lookup from PLUTO.

    Used by future Strategy B sources (dob_violations, furman subsidy) that
    carry only a BBL: join to this lookup, then point-in-polygon onto NTAs.
    Reads PLUTO in chunks for memory safety.
    """
    path = settings.raw_dir / "pluto" / "pluto.csv"
    if not path.exists():
        logger.warning("PLUTO not found at %s; BBL lookup empty.", path)
        return pd.DataFrame(columns=["bbl", "latitude", "longitude"])
    frames: list[pd.DataFrame] = []
    for chunk in pd.read_csv(
        path, usecols=["BBL", "latitude", "longitude"], chunksize=_CHUNKSIZE, low_memory=False
    ):
        keep = chunk.dropna(subset=["BBL", "latitude", "longitude"])
        if not keep.empty:
            frames.append(keep.rename(columns={"BBL": "bbl"}))
    if not frames:
        return pd.DataFrame(columns=["bbl", "latitude", "longitude"])
    return pd.concat(frames, ignore_index=True)
