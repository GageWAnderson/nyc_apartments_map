"""Tests for processing/area_sources.py: pre-aggregated area sources
bridged to NTAs via crosswalk (Strategy C -- e.g. Furman gross rent)."""

from __future__ import annotations

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


# --- build_nta_to_sba_modal (pure crosswalk) --------------------------------


def test_build_nta_to_sba_modal_picks_plurality() -> None:
    from nyc_apartments_map.processing.area_sources import build_nta_to_sba_modal

    bbl = pd.DataFrame(
        {
            # AA0101 gets 2x "North", 1x "South" -> modal "North"
            # points_from_xy takes (longitude, latitude); BB0201 is at x in [2,3]
            "latitude": [0.5, 0.2, 0.8, 0.5, 0.5],
            "longitude": [0.5, 0.2, 0.5, 2.5, 2.6],
            "sba_name": ["North", "North", "South", "East", "East"],
        }
    )
    out = build_nta_to_sba_modal(bbl, _two_nta_boundaries())
    assert set(out.columns) == {"nta_code", "sba_name"}
    by_nta = dict(zip(out["nta_code"], out["sba_name"], strict=True))
    assert by_nta == {"AA0101": "North", "BB0201": "East"}


def test_build_nta_to_sba_modal_drops_unmatched_and_nacoords() -> None:
    from nyc_apartments_map.processing.area_sources import build_nta_to_sba_modal

    bbl = pd.DataFrame(
        {
            "latitude": [0.5, 9.0, None, 0.3],
            "longitude": [0.5, 9.0, 0.5, 0.3],
            "sba_name": ["North", "Far", "Ghost", "North"],
        }
    )
    out = build_nta_to_sba_modal(bbl, _two_nta_boundaries())
    assert set(out["nta_code"]) == {"AA0101"}
    assert out["sba_name"].iloc[0] == "North"


def test_build_nta_to_sba_modal_empty_input() -> None:
    from nyc_apartments_map.processing.area_sources import build_nta_to_sba_modal

    out = build_nta_to_sba_modal(
        pd.DataFrame(columns=["latitude", "longitude", "sba_name"]),
        _two_nta_boundaries(),
    )
    assert out.empty
    assert list(out.columns) == ["nta_code", "sba_name"]


# --- money cleaning ---------------------------------------------------------


def test_money_to_float_strips_dollar_and_commas() -> None:
    from nyc_apartments_map.processing.area_sources import _money_to_float

    s = pd.Series(["$3,600", "$1,290", "$5,140.50", "", None, "n/a"])
    out = _money_to_float(s)
    assert out.iloc[0] == pytest.approx(3600.0)
    assert out.iloc[1] == pytest.approx(1290.0)
    assert out.iloc[2] == pytest.approx(5140.50)
    assert pd.isna(out.iloc[3])
    assert pd.isna(out.iloc[4])
    assert pd.isna(out.iloc[5])


# --- furman_gross_rent_nta_metrics (graceful + happy path) -------------------


def test_gross_rent_missing_xlsx_returns_empty(tmp_path) -> None:
    from nyc_apartments_map.processing.area_sources import furman_gross_rent_nta_metrics

    settings, _ = _settings_with_raw(tmp_path, "furman_housing")
    out = furman_gross_rent_nta_metrics(settings, boundaries=_two_nta_boundaries())
    assert out.empty
    assert list(out.columns) == ["nta_code", "gross_rent_0_1beds", "gross_rent_2_3beds"]


def test_gross_rent_missing_bbl_returns_empty(tmp_path) -> None:
    from nyc_apartments_map.processing.area_sources import furman_gross_rent_nta_metrics

    settings, sub = _settings_with_raw(tmp_path, "furman_housing")
    sub.mkdir(parents=True)
    # xlsx present but BBL csv absent -> empty
    pd.DataFrame(
        {
            "region_id": [101],
            "region_display": ["MN 01"],
            "region_name": ["Greenwich Village/Financial District"],
            "region_type": ["Sub-Borough Area"],
            "year": ["2020-2024"],
            "gross_rent_0_1beds": ["$3,600"],
            "gross_rent_2_3beds": ["$5,140"],
        }
    ).to_excel(sub / "neighborhood_indicators.xlsx", sheet_name="Data", index=False)

    out = furman_gross_rent_nta_metrics(settings, boundaries=_two_nta_boundaries())
    assert out.empty
    assert list(out.columns) == ["nta_code", "gross_rent_0_1beds", "gross_rent_2_3beds"]


def test_gross_rent_end_to_end_bridge(tmp_path) -> None:
    from nyc_apartments_map.processing.area_sources import furman_gross_rent_nta_metrics

    settings, sub = _settings_with_raw(tmp_path, "furman_housing")
    sub.mkdir(parents=True)
    # xlsx: two SBAs with rent strings; "North" present, "West" missing 1bed
    pd.DataFrame(
        {
            "region_id": [101, 102],
            "region_display": ["MN 01", "MN 02"],
            "region_name": ["North", "West"],
            "region_type": ["Sub-Borough Area", "Sub-Borough Area"],
            "year": ["2020-2024", "2020-2024"],
            "gross_rent_0_1beds": ["$3,600", None],
            "gross_rent_2_3beds": ["$5,140", "$4,000"],
        }
    ).to_excel(sub / "neighborhood_indicators.xlsx", sheet_name="Data", index=False)
    # BBL: AA0101 -> "North" (modal), BB0201 -> "West" (modal)
    # points_from_xy takes (longitude, latitude); BB0201 is at x in [2,3]
    pd.DataFrame(
        {
            "latitude": [0.5, 0.2, 0.5],
            "longitude": [0.5, 0.2, 2.5],
            "sba_name": ["North", "North", "West"],
        }
    ).to_csv(sub / "FC_SHD_bbl_analysis.csv", index=False)

    out = furman_gross_rent_nta_metrics(settings, boundaries=_two_nta_boundaries())
    assert len(out) == 2
    by_nta = out.set_index("nta_code")
    assert by_nta.loc["AA0101", "gross_rent_0_1beds"] == pytest.approx(3600.0)
    assert by_nta.loc["AA0101", "gross_rent_2_3beds"] == pytest.approx(5140.0)
    assert pd.isna(by_nta.loc["BB0201", "gross_rent_0_1beds"])  # West missing 1bed
    assert by_nta.loc["BB0201", "gross_rent_2_3beds"] == pytest.approx(4000.0)


def test_area_source_funcs_list_registered() -> None:
    from nyc_apartments_map.processing.area_sources import AREA_SOURCE_FUNCS

    names = {fn.__name__ for fn in AREA_SOURCE_FUNCS}
    assert names == {"furman_gross_rent_nta_metrics"}
