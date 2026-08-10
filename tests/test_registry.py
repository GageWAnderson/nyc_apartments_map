"""Tests for the dataset registry auto-discovery."""

from __future__ import annotations

from nyc_apartments_map.datasets.base import DatasetLoader
from nyc_apartments_map.datasets.registry import discover_loaders, get_loader_class


def test_discover_loaders_finds_sample() -> None:
    loaders = discover_loaders()
    assert "sample_nyc_listings" in loaders


def test_template_loader_not_registered() -> None:
    # Template loader has empty name, so registry must skip it.
    loaders = discover_loaders()
    assert all(name != "" for name in loaders)


def test_get_loader_class_unknown_raises_keyerror() -> None:
    import pytest

    with pytest.raises(KeyError):
        get_loader_class("does_not_exist")


def test_discovered_loaders_are_subclasses() -> None:
    loaders = discover_loaders()
    for cls in loaders.values():
        assert issubclass(cls, DatasetLoader)


def test_sample_loader_clean_produces_schema() -> None:
    from nyc_apartments_map.datasets.base import COMMON_SCHEMA

    cls = get_loader_class("sample_nyc_listings")
    loader = cls()
    df = loader.clean()
    for col in COMMON_SCHEMA:
        assert col in df.columns, f"missing {col}"
    assert (df["source"] == "sample_nyc_listings").all()
    assert df["latitude"].notna().all()
    assert df["longitude"].notna().all()
