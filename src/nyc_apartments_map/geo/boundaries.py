"""NTA boundary loading and point-in-polygon assignment.

Reads the 2020 NTA boundary GeoJSON (DCP "Bytes of the Big Apple") and assigns
``nta_code``/``cdta_code`` to listing rows via a spatial join. 2020 NTA codes
embed their CDTA in the first 4 characters, but the boundary file carries
``CDTA2020`` directly, so the join path returns both without parsing.
"""

from __future__ import annotations

import logging

import geopandas as gpd
import pandas as pd

from nyc_apartments_map.config import Settings
from nyc_apartments_map.geo import WGS84_CRS

logger = logging.getLogger(__name__)

#: DCP 2020 NTA GeoJSON property names -> our canonical column names.
_NTA_PROP_RENAME: dict[str, str] = {
    "NTA2020": "nta_code",
    "NTAName": "nta_name",
    "NTAType": "nta_type",
    "CDTA2020": "cdta_code",
    "CDTAName": "cdta_name",
}

#: Columns retained from the boundary file after renaming.
_BOUNDARY_COLS = ["nta_code", "nta_name", "nta_type", "cdta_code", "cdta_name", "geometry"]


def derive_cdta(nta_code: str | None) -> str | None:
    """Derive the CDTA code from a 2020 NTA code (first 4 characters).

    2020 NTA codes embed their CDTA: e.g. ``"BK0101"`` -> ``"BK01"``. Returns
    ``None`` for missing/short inputs. Used as a fallback for loaders that emit
    only ``nta_code`` without a boundary join; the join path doesn't need this
    because the boundary file carries ``CDTA2020`` directly.
    """
    if nta_code is None or len(nta_code) < 4:
        return None
    return nta_code[:4]


def load_nta_boundaries(settings: Settings) -> gpd.GeoDataFrame:
    """Load the 2020 NTA boundary GeoJSON into a GeoDataFrame.

    Returns columns: ``nta_code``, ``nta_name``, ``nta_type``, ``cdta_code``,
    ``cdta_name``, ``geometry``. CRS is normalized to WGS84 (EPSG:4326).
    """
    gdf = gpd.read_file(settings.nta_boundaries_path)
    gdf = gdf.rename(columns=_NTA_PROP_RENAME)
    gdf = gdf[_BOUNDARY_COLS]
    if gdf.crs is None:
        gdf.set_crs(WGS84_CRS, inplace=True)
    elif str(gdf.crs).upper() != WGS84_CRS:
        gdf = gdf.to_crs(WGS84_CRS)
    return gdf


def assign_nta(df: pd.DataFrame, boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """Fill ``nta_code``/``cdta_code`` via point-in-polygon against NTA boundaries.

    Only rows where ``nta_code`` is NaN and ``latitude``/``longitude`` are
    non-null are assigned; rows already carrying an ``nta_code`` are left
    untouched (idempotent). Points outside any NTA polygon remain NaN and are
    logged with a count.
    """
    need = df["nta_code"].isna() & df["latitude"].notna() & df["longitude"].notna()
    n_need = int(need.sum())
    if n_need == 0:
        return df

    pts = gpd.GeoDataFrame(
        df.loc[need, ["latitude", "longitude"]].copy(),
        geometry=gpd.points_from_xy(df.loc[need, "longitude"], df.loc[need, "latitude"]),
        crs=WGS84_CRS,
    )
    joined = gpd.sjoin(
        pts,
        boundaries[["nta_code", "cdta_code", "geometry"]],
        how="left",
        predicate="within",
    )

    unmatched = int(joined["nta_code"].isna().sum())
    if unmatched:
        logger.warning("%d/%d points fell outside all NTA polygons", unmatched, n_need)

    df.loc[need, "nta_code"] = joined["nta_code"].to_numpy()
    df.loc[need, "cdta_code"] = joined["cdta_code"].to_numpy()
    return df
