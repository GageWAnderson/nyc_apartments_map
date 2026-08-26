"""Map pre-aggregated area sources onto NTAs (Strategy C: crosswalk bridge).

Each function reads a source that publishes metrics at a coarser geography
than the NTA (e.g. Furman Center's Sub-Borough Area / Community District
indicators), bridges them down to NTAs via a deterministic crosswalk, and
returns per-NTA metrics keyed by ``nta_code``. Results are left-merged onto
the listing-derived indicators table by
:func:`nyc_apartments_map.processing.aggregate.build_nta_indicators`.

These sources are contextual (not listings) and bypass :data:`COMMON_SCHEMA`
/ the :class:`DatasetLoader` abstraction on purpose — forcing them through the
listing schema would be a category error (same rationale as
:mod:`nyc_apartments_map.processing.geo_sources`). Missing raw files are
non-fatal: the function logs and returns an empty frame.

Unlike the point-source counts in :mod:`geo_sources`, area-source metrics are
continuous dollar values, so they stay float/NaN — they are NOT coerced to 0
(a missing rent is not a $0 rent). ``aggregate`` mirrors this for the merge.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

import geopandas as gpd
import pandas as pd

from nyc_apartments_map.config import Settings
from nyc_apartments_map.geo.boundaries import assign_nta

logger = logging.getLogger(__name__)

#: Read chunk size for the BBL CSV (rows per chunk). Matches ``geo_sources``.
_CHUNKSIZE = 200_000

#: 5-year ACS estimate to use for gross-rent metrics. The Furman
#: ``neighborhood_indicators.xlsx`` publishes gross rent only in the 5-year
#: ACS rows (single-year rows are empty for these columns); this is the most
#: recent range. One of 55 Sub-Borough Areas is null in this range, so the
#: NTAs bridged to that SBA render gray (NaN) on the map.
_GROSS_RENT_YEAR = "2020-2024"

#: Regex stripping ``$`` and thousands separators from Furman rent strings
#: (e.g. ``"$3,600"`` -> ``3600.0``). The xlsx stores these as text.
_MONEY_RE = re.compile(r"[\$,]")


class AreaSourceFunc(Protocol):
    """Signature of a per-NTA area-source metric function."""

    def __call__(self, settings: Settings, *, boundaries: gpd.GeoDataFrame) -> pd.DataFrame: ...


# --- Helpers -----------------------------------------------------------------


def _money_to_float(series: pd.Series) -> pd.Series:
    """Coerce a Series of money strings (``"$3,600"``) to float, NaN on miss.

    The Furman xlsx stores rent columns as ``StringDtype`` with leading ``$`` and
    comma separators; plain ``pd.to_numeric`` would NaN every value.
    """
    cleaned = series.astype("string").str.replace(_MONEY_RE, "", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def build_nta_to_sba_modal(bbl_df: pd.DataFrame, boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """Bridge NTA <- Sub-Borough Area via the modal ``sba_name`` of BBL points.

    ``bbl_df`` must carry ``latitude``, ``longitude``, and ``sba_name``. Each
    point is assigned an ``nta_code`` via point-in-polygon
    (:func:`nyc_apartments_map.geo.boundaries.assign_nta`); the plurality
    ``sba_name`` per NTA wins (``value_counts().idxmax()``). Returns a frame
    with columns ``nta_code``, ``sba_name`` (one row per matched NTA).

    Pure + deterministic so it can be unit-tested with synthetic inputs; the
    chunked CSV read lives in the caller (:func:`furman_gross_rent_nta_metrics`).
    """
    pts = bbl_df.dropna(subset=["latitude", "longitude", "sba_name"]).copy()
    if pts.empty:
        return pd.DataFrame(columns=["nta_code", "sba_name"])
    pts["nta_code"] = pd.NA
    pts["cdta_code"] = pd.NA
    pts = assign_nta(pts, boundaries)
    pts = pts.dropna(subset=["nta_code"])
    if pts.empty:
        return pd.DataFrame(columns=["nta_code", "sba_name"])

    def _modal(s: pd.Series) -> object:
        return s.value_counts().idxmax()

    modal = pts.groupby("nta_code", as_index=False).agg(sba_name=("sba_name", _modal))
    return modal[["nta_code", "sba_name"]]


def _load_sba_gross_rent(settings: Settings) -> pd.DataFrame:
    """Read the Furman xlsx and return SBA-level gross rent for the chosen year.

    Returns columns ``sba_name``, ``gross_rent_0_1beds``,
    ``gross_rent_2_3beds`` (float). Empty (with the same columns) if the xlsx
    is absent or has no SBA rows for :data:`_GROSS_RENT_YEAR`.
    """
    out_cols = ["sba_name", "gross_rent_0_1beds", "gross_rent_2_3beds"]
    path = settings.raw_dir / "furman_housing" / "neighborhood_indicators.xlsx"
    if not path.exists():
        logger.warning("Furman neighborhood indicators not found at %s; skipping.", path)
        return pd.DataFrame(columns=out_cols)
    raw = pd.read_excel(path, sheet_name="Data", engine="openpyxl")
    sub = raw[(raw["region_type"] == "Sub-Borough Area") & (raw["year"] == _GROSS_RENT_YEAR)]
    if sub.empty:
        logger.warning(
            "No Sub-Borough Area rows for %s in %s; skipping gross rent.",
            _GROSS_RENT_YEAR,
            path,
        )
        return pd.DataFrame(columns=out_cols)
    rent = pd.DataFrame(
        {
            "sba_name": sub["region_name"].astype("string"),
            "gross_rent_0_1beds": _money_to_float(sub["gross_rent_0_1beds"]),
            "gross_rent_2_3beds": _money_to_float(sub["gross_rent_2_3beds"]),
        }
    )
    return rent


# --- Per-source functions ----------------------------------------------------


def furman_gross_rent_nta_metrics(
    settings: Settings, *, boundaries: gpd.GeoDataFrame
) -> pd.DataFrame:
    """Furman Center median gross rent per NTA, bridged from SBA via BBL points.

    Gross rent is published at the Sub-Borough Area level (55 SBAs) in the
    Furman ``neighborhood_indicators.xlsx``; the map renders at NTA level, so
    each NTA inherits the rent of its plurality-SBA (the modal ``sba_name``
    among geo-located BBL records falling in that NTA). Returns columns
    ``nta_code``, ``gross_rent_0_1beds`` (studios/1-bed), ``gross_rent_2_3beds``
    (2-3 beds) as float dollars. NTAs with no BBL points (parks, industrial) or
    bridged to an SBA missing rent stay NaN — the map grays them out.
    """
    out_cols = ["nta_code", "gross_rent_0_1beds", "gross_rent_2_3beds"]
    rent = _load_sba_gross_rent(settings)
    bbl_path = settings.raw_dir / "furman_housing" / "FC_SHD_bbl_analysis.csv"
    if rent.empty:
        return pd.DataFrame(columns=out_cols)
    if not bbl_path.exists():
        logger.warning("Furman BBL analysis not found at %s; skipping gross rent.", bbl_path)
        return pd.DataFrame(columns=out_cols)

    # Chunked read of just the crosswalk columns — BBL CSV is ~17k rows but
    # mirror geo_sources' memory-bounded pattern for headroom.
    frames: list[pd.DataFrame] = []
    for chunk in pd.read_csv(
        bbl_path, usecols=["latitude", "longitude", "sba_name"], chunksize=_CHUNKSIZE
    ):
        frames.append(chunk)
    bbl = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=["latitude", "longitude", "sba_name"])
    )
    crosswalk = build_nta_to_sba_modal(bbl, boundaries)
    if crosswalk.empty:
        return pd.DataFrame(columns=out_cols)

    merged = crosswalk.merge(rent, on="sba_name", how="left")
    return merged[out_cols].reset_index(drop=True)


#: All area-source metric functions, called by ``build_nta_indicators``.
#: Unlike the point-source list in ``geo_sources``, these metrics are float
#: (dollar values) and stay NaN — ``aggregate`` does not fill 0 for them.
AREA_SOURCE_FUNCS: list[AreaSourceFunc] = [
    furman_gross_rent_nta_metrics,
]
