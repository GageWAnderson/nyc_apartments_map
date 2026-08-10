"""Tests for processing/enrich.py: NTA enrichment of listing rows."""

from __future__ import annotations

import pandas as pd
import pyarrow.parquet as pq
from shapely.geometry import Polygon

from nyc_apartments_map.processing.enrich import enrich_listings


def _write_boundary_file(
    path,
    nta_code="AA0101",
    cdta_code="AA01",
    geometry=None,
) -> None:
    """Write a minimal 1-polygon GeoJSON boundary file to *path*."""
    import geopandas as gpd

    if geometry is None:
        geometry = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])  # unit square
    gdf = gpd.GeoDataFrame(
        {
            "NTA2020": [nta_code],
            "NTAName": ["Area A"],
            "NTAType": ["0"],
            "CDTA2020": [cdta_code],
            "CDTAName": ["CDTA A"],
        },
        geometry=[geometry],
        crs="EPSG:4326",
    )
    gdf.to_file(path, driver="GeoJSON")


def test_enrich_skips_when_file_absent() -> None:
    """When the boundary file doesn't exist, df is returned unchanged."""
    from nyc_apartments_map.config import Settings

    settings = Settings(project_root="/nonexistent")
    settings.nta_boundaries_path = settings.project_root / "missing.json"
    df = pd.DataFrame(
        {
            "latitude": [0.5],
            "longitude": [0.5],
            "nta_code": [pd.NA],
            "cdta_code": [pd.NA],
        }
    )
    out = enrich_listings(df, settings)
    assert pd.isna(out.loc[0, "nta_code"])
    assert pd.isna(out.loc[0, "cdta_code"])
    assert len(out) == 1


def test_enrich_fills_nan_rows(tmp_path) -> None:
    from nyc_apartments_map.config import Settings

    boundary_path = tmp_path / "ntas.json"
    _write_boundary_file(boundary_path)
    settings = Settings(project_root=tmp_path)
    settings.nta_boundaries_path = boundary_path

    df = pd.DataFrame(
        {
            "latitude": [0.5, 0.5],
            "longitude": [0.5, 0.5],
            "nta_code": [pd.NA, pd.NA],
            "cdta_code": [pd.NA, pd.NA],
        }
    )
    out = enrich_listings(df, settings)
    assert out.loc[0, "nta_code"] == "AA0101"
    assert out.loc[0, "cdta_code"] == "AA01"
    assert out.loc[1, "nta_code"] == "AA0101"


def test_enrich_leaves_prefilled(tmp_path) -> None:
    """Rows with a pre-existing nta_code are not reassigned."""
    from nyc_apartments_map.config import Settings

    boundary_path = tmp_path / "ntas.json"
    _write_boundary_file(boundary_path)
    settings = Settings(project_root=tmp_path)
    settings.nta_boundaries_path = boundary_path

    df = pd.DataFrame(
        {
            "latitude": [0.5, 0.5],
            "longitude": [0.5, 0.5],
            "nta_code": ["ZZ9999", pd.NA],
            "cdta_code": ["ZZ99", pd.NA],
        }
    )
    out = enrich_listings(df, settings)
    assert out.loc[0, "nta_code"] == "ZZ9999"
    assert out.loc[0, "cdta_code"] == "ZZ99"
    assert out.loc[1, "nta_code"] == "AA0101"
    assert out.loc[1, "cdta_code"] == "AA01"


def test_enrich_integration_writes_parquet(tmp_path) -> None:
    """enrich_listings writes through the full normalize pipeline via process."""
    from nyc_apartments_map.config import Settings
    from nyc_apartments_map.processing.normalize import normalize

    # Polygon covering NYC's bounding box so all sample listings land inside.
    nyc_poly = Polygon([(-74.26, 40.47), (-74.26, 40.92), (-73.70, 40.92), (-73.70, 40.47)])
    boundary_path = tmp_path / "ntas.json"
    _write_boundary_file(boundary_path, nta_code="MN6400", cdta_code="MN64", geometry=nyc_poly)
    settings = Settings(project_root=tmp_path)
    settings.raw_dir = tmp_path / "data" / "raw"
    settings.processed_dir = tmp_path / "data" / "processed"
    settings.normalized_path = tmp_path / "data" / "processed" / "normalized.parquet"
    settings.nta_indicators_path = tmp_path / "data" / "processed" / "nta_indicators.parquet"
    settings.nta_boundaries_path = boundary_path

    df = normalize(settings=settings, write=True)
    assert settings.normalized_path.exists()
    assert settings.nta_indicators_path.exists()
    # All sample listings should land in the single NYC-sized polygon.
    assert df["nta_code"].notna().all()
    assert (df["nta_code"] == "MN6400").all()
    # Verify indicators parquet is readable and has expected columns.
    indicators = pq.read_table(settings.nta_indicators_path).to_pandas()
    assert "nta_code" in indicators.columns
    assert "listing_count" in indicators.columns
    nta_row = indicators.loc[indicators["nta_code"] == "MN6400", "listing_count"].iloc[0]
    assert int(nta_row) == len(df)
