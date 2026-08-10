"""Tests for the normalize/merge step and schema validation."""

from __future__ import annotations

import pandas as pd

from nyc_apartments_map.datasets.base import COMMON_SCHEMA
from nyc_apartments_map.processing.normalize import validate_schema


def _good_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "listing_id": ["a-1", "a-2"],
            "latitude": [40.7, 40.8],
            "longitude": [-74.0, -73.9],
            "price": [2000.0, 3000.0],
            "bedrooms": [1.0, 2.0],
            "bathrooms": [1.0, 1.0],
            "neighborhood": ["X", "Y"],
            "borough": ["Manhattan", "Brooklyn"],
            "nta_code": [pd.NA, pd.NA],
            "cdta_code": [pd.NA, pd.NA],
            "source": ["test", "test"],
            "raw": [{"x": 1}, {"x": 2}],
        }
    )


def test_validate_schema_passes_good_frame() -> None:
    df = validate_schema(_good_frame(), "test")
    for col in COMMON_SCHEMA:
        assert col in df.columns
    assert (df["source"] == "test").all()


def test_validate_schema_missing_column_raises() -> None:
    import pytest

    df = _good_frame().drop(columns=["price"])
    with pytest.raises(ValueError):
        validate_schema(df, "test")


def test_validate_schema_drops_missing_coords() -> None:
    df = _good_frame()
    df.loc[0, "latitude"] = pd.NA
    out = validate_schema(df, "test")
    assert len(out) == 1
    assert out["listing_id"].iloc[0] == "a-2"


def test_validate_schema_sets_source_when_missing() -> None:
    df = _good_frame()
    df["source"] = pd.NA
    out = validate_schema(df, "myloader")
    assert (out["source"] == "myloader").all()


def test_normalize_all_writes_parquet(tmp_path, monkeypatch) -> None:
    from nyc_apartments_map.config import Settings
    from nyc_apartments_map.processing.normalize import normalize

    # Redirect paths to a temp project root.
    settings = Settings(project_root=tmp_path)
    settings.data_dir = tmp_path / "data"
    settings.raw_dir = tmp_path / "data" / "raw"
    settings.interim_dir = tmp_path / "data" / "interim"
    settings.processed_dir = tmp_path / "data" / "processed"
    settings.normalized_path = tmp_path / "data" / "processed" / "normalized.parquet"

    df = normalize(settings=settings, write=True)
    assert len(df) > 0
    assert settings.normalized_path.exists()
    for col in COMMON_SCHEMA:
        assert col in df.columns
