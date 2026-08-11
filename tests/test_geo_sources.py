"""Tests for processing/geo_sources.py: point-source NTA mapping (Strategy A)."""

from __future__ import annotations

import json

import pandas as pd
import pytest
from shapely.geometry import Polygon


def _two_nta_boundaries():
    """Two unit-square NTA polygons: AA0101 at origin, BB0201 at x=2."""
    import geopandas as gpd

    return gpd.GeoDataFrame(
        {
            "nta_code": ["AA0101", "BB0201"],
            "nta_name": ["Area A", "Area B"],
            "nta_type": ["0", "0"],
            "cdta_code": ["AA01", "BB02"],
            "cdta_name": ["CDTA A", "CDTA B"],
        },
        geometry=[
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            Polygon([(2, 0), (2, 1), (3, 1), (3, 0)]),
        ],
        crs="EPSG:4326",
    )


def _settings_with_raw(tmp_path, subdir: str):
    """Settings whose raw_dir is tmp_path/raw (so sources read toy files)."""
    from nyc_apartments_map.config import Settings

    settings = Settings(project_root=tmp_path)
    settings.raw_dir = tmp_path / "raw"
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    return settings, settings.raw_dir / subdir


# --- Parsers ---------------------------------------------------------------


def test_parse_wkt_point_valid() -> None:
    from nyc_apartments_map.processing.geo_sources import parse_wkt_point

    assert parse_wkt_point("POINT (-73.93 40.67)") == pytest.approx((40.67, -73.93))


def test_parse_wkt_point_garbage_returns_none() -> None:
    from nyc_apartments_map.processing.geo_sources import parse_wkt_point

    assert parse_wkt_point("not a point") is None
    assert parse_wkt_point("") is None
    assert parse_wkt_point("POINT (only_one)") is None


def test_parse_osm_elements_filters_nightlife() -> None:
    from nyc_apartments_map.processing.geo_sources import parse_osm_elements

    elements = [
        {
            "type": "node",
            "id": 1,
            "lat": 40.70,
            "lon": -74.00,
            "tags": {"amenity": "bar", "name": "A"},
        },
        {
            "type": "node",
            "id": 2,
            "lat": 40.71,
            "lon": -74.01,
            "tags": {"amenity": "pub", "name": "B"},
        },
        {
            "type": "node",
            "id": 3,
            "lat": 40.72,
            "lon": -74.02,
            "tags": {"amenity": "restaurant", "name": "C"},
        },
        {
            "type": "node",
            "id": 4,
            "lat": 40.73,
            "lon": -74.03,
            "tags": {"amenity": "nightclub", "name": "D"},
        },
        {
            "type": "node",
            "id": 5,
            "lat": 40.74,
            "lon": -74.04,
            "tags": {"amenity": "biergarten", "name": "E"},
        },
        {"type": "way", "id": 6, "nodes": [1, 2], "tags": {"amenity": "bar"}},  # not a node
    ]
    df = parse_osm_elements(elements)
    assert len(df) == 4  # bar, pub, nightclub, biergarten; restaurant + way excluded
    assert set(df["name"]) == {"A", "B", "D", "E"}
    assert {"latitude", "longitude", "name", "amenity"} <= set(df.columns)


# --- Per-source aggregation -------------------------------------------------


def test_nys_liquor_nta_metrics(tmp_path) -> None:
    from nyc_apartments_map.processing.geo_sources import nys_liquor_nta_metrics

    settings, sub = _settings_with_raw(tmp_path, "nys_liquor")
    sub.mkdir(parents=True)
    pd.DataFrame(
        {
            "Georeference": [
                "POINT (0.5 0.5)",  # inside AA0101
                "POINT (0.1 0.1)",  # inside AA0101
                "POINT (2.5 0.5)",  # inside BB0201
                "POINT (9 9)",  # outside any NTA
                "",  # unparseable
            ]
        }
    ).to_csv(sub / "liquor_active_licenses.csv", index=False)

    out = nys_liquor_nta_metrics(settings, boundaries=_two_nta_boundaries())
    assert set(out.columns) == {"nta_code", "liquor_license_count"}
    row_a = out[out["nta_code"] == "AA0101"].iloc[0]
    row_b = out[out["nta_code"] == "BB0201"].iloc[0]
    assert row_a["liquor_license_count"] == 2
    assert row_b["liquor_license_count"] == 1


def test_hpd_violations_nta_metrics(tmp_path) -> None:
    from nyc_apartments_map.processing.geo_sources import hpd_violations_nta_metrics

    settings, sub = _settings_with_raw(tmp_path, "hpd_violations")
    sub.mkdir(parents=True)
    pd.DataFrame(
        {
            "latitude": [0.5, 0.2, 2.5, 0.5],
            "longitude": [0.5, 0.2, 0.5, 0.5],
            "rentimpairing": ["Y", "N", "Y", "N"],
        }
    ).to_csv(sub / "hpd_violations.csv", index=False)

    out = hpd_violations_nta_metrics(settings, boundaries=_two_nta_boundaries())
    # point (2.5, 0.5) is outside both unit squares -> dropped
    row_a = out[out["nta_code"] == "AA0101"].iloc[0]
    assert row_a["hpd_violation_count"] == 3
    assert row_a["hpd_rent_impairing_count"] == 1
    # BB0201 has no matched points -> not present (left-filled later)
    assert "BB0201" not in set(out["nta_code"])


def test_osm_nightlife_nta_metrics(tmp_path) -> None:
    from nyc_apartments_map.processing.geo_sources import osm_nightlife_nta_metrics

    settings, sub = _settings_with_raw(tmp_path, "osm_nightlife")
    sub.mkdir(parents=True)
    payload = {
        "version": 0.6,
        "generator": "test",
        "elements": [
            {
                "type": "node",
                "id": 1,
                "lat": 0.5,
                "lon": 0.5,
                "tags": {"amenity": "bar", "name": "X"},
            },
            {
                "type": "node",
                "id": 2,
                "lat": 0.5,
                "lon": 2.5,
                "tags": {"amenity": "nightclub", "name": "Y"},
            },
            {
                "type": "node",
                "id": 3,
                "lat": 0.5,
                "lon": 0.5,
                "tags": {"amenity": "restaurant", "name": "Z"},
            },
        ],
    }
    (sub / "osm_nightlife.json").write_text(json.dumps(payload))

    out = osm_nightlife_nta_metrics(settings, boundaries=_two_nta_boundaries())
    row_a = out[out["nta_code"] == "AA0101"].iloc[0]
    assert row_a["nightlife_venue_count"] == 1  # bar only; restaurant excluded
    row_b = out[out["nta_code"] == "BB0201"].iloc[0]
    assert row_b["nightlife_venue_count"] == 1


def test_missing_file_returns_empty(tmp_path) -> None:
    from nyc_apartments_map.processing.geo_sources import (
        hpd_violations_nta_metrics,
        nyc_311_nta_metrics,
        nypd_complaints_nta_metrics,
        nys_liquor_nta_metrics,
        osm_nightlife_nta_metrics,
    )

    settings, _ = _settings_with_raw(tmp_path, "absent_source")
    bounds = _two_nta_boundaries()
    for fn in (
        hpd_violations_nta_metrics,
        nyc_311_nta_metrics,
        nypd_complaints_nta_metrics,
        osm_nightlife_nta_metrics,
        nys_liquor_nta_metrics,
    ):
        out = fn(settings, boundaries=bounds)
        assert isinstance(out, pd.DataFrame)
        assert out.empty, f"{fn.__name__} should return empty on missing file"


def test_point_source_funcs_list_complete() -> None:
    from nyc_apartments_map.processing.geo_sources import POINT_SOURCE_FUNCS

    names = {fn.__name__ for fn in POINT_SOURCE_FUNCS}
    expected = {
        "hpd_violations_nta_metrics",
        "nyc_311_nta_metrics",
        "nypd_complaints_nta_metrics",
        "osm_nightlife_nta_metrics",
        "nys_liquor_nta_metrics",
        "furman_bbl_analysis_nta_metrics",
        "pluto_nta_metrics",
    }
    assert names == expected
