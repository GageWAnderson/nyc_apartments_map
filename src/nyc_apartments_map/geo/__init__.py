"""Geospatial helpers: NYC bounds and CRS constants."""

from __future__ import annotations

#: Approximate NYC bounding box (lat/lon, WGS84 / EPSG:4326).
NYC_BOUNDS: dict[str, float] = {
    "min_lat": 40.4774,
    "max_lat": 40.9176,
    "min_lon": -74.2591,
    "max_lon": -73.7004,
}

#: Map center (Manhattan-ish).
NYC_CENTER: tuple[float, float] = (40.7128, -74.0060)

#: Common CRS used by the pipeline.
WGS84_CRS = "EPSG:4326"
