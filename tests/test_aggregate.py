"""Tests for processing/aggregate.py: per-NTA indicator computation."""

from __future__ import annotations

import pandas as pd
from shapely.geometry import Polygon

from nyc_apartments_map.processing.aggregate import build_nta_indicators


def _write_boundary_file(path) -> None:
    """Write a 2-NTA boundary file: one residential, one park (non-residential)."""
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(
        {
            "NTA2020": ["AA0101", "AA0191"],
            "NTAName": ["Area A", "Area A Park"],
            "NTAType": ["0", "9"],
            "CDTA2020": ["AA01", "AA01"],
            "CDTAName": ["CDTA A", "CDTA A"],
        },
        geometry=[
            Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]),
            Polygon([(2, 0), (2, 1), (3, 1), (3, 0)]),
        ],
        crs="EPSG:4326",
    )
    gdf.to_file(path, driver="GeoJSON")


def _enriched_listings() -> pd.DataFrame:
    """Three listings in AA0101, one in BB0201 (which has no boundary polygon)."""
    return pd.DataFrame(
        {
            "listing_id": ["L1", "L2", "L3", "L4"],
            "latitude": [0.5, 0.5, 0.5, 0.5],
            "longitude": [0.5, 0.5, 0.5, 0.5],
            "price": [2000.0, 4000.0, 6000.0, 8000.0],
            "bedrooms": [1.0, 2.0, pd.NA, 0.0],
            "bathrooms": [1.0, 2.0, 1.0, 1.0],
            "nta_code": ["AA0101", "AA0101", "AA0101", "BB0201"],
            "cdta_code": ["AA01", "AA01", "AA01", "BB02"],
            "source": ["test", "test", "test", "test"],
        }
    )


def test_build_nta_indicators_metrics(tmp_path) -> None:
    from nyc_apartments_map.config import Settings

    boundary_path = tmp_path / "ntas.json"
    _write_boundary_file(boundary_path)
    settings = Settings(project_root=tmp_path)
    settings.nta_boundaries_path = boundary_path
    settings.nta_indicators_path = tmp_path / "nta_indicators.parquet"

    out = build_nta_indicators(_enriched_listings(), settings)
    assert settings.nta_indicators_path.exists()

    # Both boundary NTAs present (even BB0201 which has no listings in boundary).
    assert len(out) == 2
    assert set(out["nta_code"]) == {"AA0101", "AA0191"}

    row_a = out[out["nta_code"] == "AA0101"].iloc[0]
    assert row_a["listing_count"] == 3
    assert row_a["median_price"] == 4000.0
    # price_per_bed for L1=2000/1=2000, L2=4000/2=2000; L3 (NaN beds) excluded, L4 not in AA0101.
    assert row_a["median_price_per_bed"] == 2000.0
    assert row_a["median_bedrooms"] == 1.5  # median of [1, 2, NaN] = 1.5
    assert row_a["median_bathrooms"] == 1.0
    assert row_a["pct_missing_bedrooms"] == 1.0 / 3.0  # 1 of 3 missing

    # Non-residential NTA (park) has 0 listings.
    row_park = out[out["nta_code"] == "AA0191"].iloc[0]
    assert row_park["nta_type"] == "9"
    assert row_park["listing_count"] == 0
    assert pd.isna(row_park["median_price"])


def test_build_nta_indicators_metadata_joined(tmp_path) -> None:
    from nyc_apartments_map.config import Settings

    boundary_path = tmp_path / "ntas.json"
    _write_boundary_file(boundary_path)
    settings = Settings(project_root=tmp_path)
    settings.nta_boundaries_path = boundary_path
    settings.nta_indicators_path = tmp_path / "nta_indicators.parquet"

    out = build_nta_indicators(_enriched_listings(), settings)
    for col in ("nta_name", "nta_type", "cdta_code", "cdta_name"):
        assert col in out.columns
    row_a = out[out["nta_code"] == "AA0101"].iloc[0]
    assert row_a["nta_name"] == "Area A"
    assert row_a["nta_type"] == "0"
    assert row_a["cdta_code"] == "AA01"
    assert row_a["cdta_name"] == "CDTA A"


def test_build_nta_indicators_no_boundary_file(tmp_path) -> None:
    """Without a boundary file, indicators lack metadata but still compute metrics."""
    from nyc_apartments_map.config import Settings

    settings = Settings(project_root=tmp_path)
    settings.nta_boundaries_path = tmp_path / "missing.json"
    settings.nta_indicators_path = tmp_path / "nta_indicators.parquet"

    out = build_nta_indicators(_enriched_listings(), settings)
    assert "nta_name" not in out.columns
    assert "listing_count" in out.columns
    assert len(out) == 2  # only NTAs that appear in the listings
    row_a = out[out["nta_code"] == "AA0101"].iloc[0]
    assert row_a["listing_count"] == 3


def test_build_nta_indicators_empty_listings(tmp_path) -> None:
    """Empty listings -> all NTAs present with 0 count and NaN metrics."""
    from nyc_apartments_map.config import Settings

    boundary_path = tmp_path / "ntas.json"
    _write_boundary_file(boundary_path)
    settings = Settings(project_root=tmp_path)
    settings.nta_boundaries_path = boundary_path
    settings.nta_indicators_path = tmp_path / "nta_indicators.parquet"

    empty = pd.DataFrame(
        {
            "listing_id": pd.Series([], dtype=str),
            "latitude": pd.Series([], dtype="float64"),
            "longitude": pd.Series([], dtype="float64"),
            "price": pd.Series([], dtype="float64"),
            "bedrooms": pd.Series([], dtype="float64"),
            "bathrooms": pd.Series([], dtype="float64"),
            "nta_code": pd.Series([], dtype=str),
            "cdta_code": pd.Series([], dtype=str),
        }
    )
    out = build_nta_indicators(empty, settings)
    assert len(out) == 2
    assert (out["listing_count"] == 0).all()
    assert out["median_price"].isna().all()
