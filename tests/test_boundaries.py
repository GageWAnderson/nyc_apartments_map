"""Tests for geo/boundaries.py: NTA code derivation and point-in-polygon."""

from __future__ import annotations

import numpy as np
import pandas as pd
from shapely.geometry import Polygon

from nyc_apartments_map.geo.boundaries import assign_nta, derive_cdta


def _two_polygon_gdf():
    """Build a minimal 2-polygon GeoDataFrame mimicking the NTA boundary file."""
    import geopandas as gpd

    poly_a = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])  # unit square at origin
    poly_b = Polygon([(2, 0), (2, 1), (3, 1), (3, 0)])  # unit square at x=2
    return gpd.GeoDataFrame(
        {
            "nta_code": ["AA0101", "BB0201"],
            "nta_name": ["Area A", "Area B"],
            "nta_type": ["0", "0"],
            "cdta_code": ["AA01", "BB02"],
            "cdta_name": ["CDTA A", "CDTA B"],
        },
        geometry=[poly_a, poly_b],
        crs="EPSG:4326",
    )


def test_derive_cdta_valid() -> None:
    assert derive_cdta("BK0101") == "BK01"
    assert derive_cdta("MN6491") == "MN64"


def test_derive_cdta_none() -> None:
    assert derive_cdta(None) is None


def test_derive_cdta_short() -> None:
    assert derive_cdta("BK") is None
    assert derive_cdta("") is None


def test_assign_nta_inside_outside() -> None:
    gdf = _two_polygon_gdf()
    df = pd.DataFrame(
        {
            "latitude": [0.5, 0.5, 5.0],
            "longitude": [0.5, 2.5, 5.0],
            "nta_code": [pd.NA, pd.NA, pd.NA],
            "cdta_code": [pd.NA, pd.NA, pd.NA],
        }
    )
    out = assign_nta(df, gdf)
    assert out.loc[0, "nta_code"] == "AA0101"
    assert out.loc[0, "cdta_code"] == "AA01"
    assert out.loc[1, "nta_code"] == "BB0201"
    assert out.loc[1, "cdta_code"] == "BB02"
    assert pd.isna(out.loc[2, "nta_code"])
    assert pd.isna(out.loc[2, "cdta_code"])


def test_assign_nta_idempotent() -> None:
    """Rows already carrying an nta_code must not be overwritten."""
    gdf = _two_polygon_gdf()
    df = pd.DataFrame(
        {
            "latitude": [0.5, 0.5],
            "longitude": [0.5, 0.5],
            "nta_code": ["PRESET", pd.NA],
            "cdta_code": ["PRE", pd.NA],
        }
    )
    out = assign_nta(df, gdf)
    assert out.loc[0, "nta_code"] == "PRESET"
    assert out.loc[0, "cdta_code"] == "PRE"
    assert out.loc[1, "nta_code"] == "AA0101"
    assert out.loc[1, "cdta_code"] == "AA01"


def test_assign_nta_all_filled_skips() -> None:
    """When every row already has nta_code, no spatial join is performed."""
    gdf = _two_polygon_gdf()
    df = pd.DataFrame(
        {
            "latitude": [0.5],
            "longitude": [0.5],
            "nta_code": ["XK9999"],
            "cdta_code": ["XK99"],
        }
    )
    out = assign_nta(df, gdf)
    assert out.loc[0, "nta_code"] == "XK9999"
    assert len(out) == 1


def test_assign_nta_nan_coords_excluded() -> None:
    """Rows with NaN latitude/longitude stay NaN after assignment."""
    gdf = _two_polygon_gdf()
    df = pd.DataFrame(
        {
            "latitude": [np.nan, 0.5],
            "longitude": [0.5, 0.5],
            "nta_code": [pd.NA, pd.NA],
            "cdta_code": [pd.NA, pd.NA],
        }
    )
    out = assign_nta(df, gdf)
    assert pd.isna(out.loc[0, "nta_code"])
    assert out.loc[1, "nta_code"] == "AA0101"
