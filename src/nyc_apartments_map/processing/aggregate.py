"""Aggregate enriched listings into per-NTA indicator metrics.

Computes listing-derived metrics at the NTA level and writes
``data/processed/nta_indicators.parquet`` (one row per NTA, keyed by
``nta_code``). Future pre-aggregated sources (Furman/ACS) and point sources
(311/POIs/crime) plug into this table as additional columns via crosswalk /
spatial joins — left as documented extension points, not implemented here.
"""

from __future__ import annotations

import logging

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from nyc_apartments_map.config import Settings
from nyc_apartments_map.geo.boundaries import load_nta_boundaries

logger = logging.getLogger(__name__)


def build_nta_indicators(listings: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Compute listing-derived metrics per NTA and write ``nta_indicators.parquet``.

    One row per NTA (from the boundary file), keyed by ``nta_code``, carrying:
      - ``listing_count``, ``median_price``, ``median_price_per_bed``,
        ``median_bedrooms``, ``median_bathrooms``, ``pct_missing_bedrooms``
      - ``nta_name``, ``nta_type``, ``cdta_code``, ``cdta_name`` (joined from
        boundaries so the table is self-describing; non-residential NTAs have
        ``nta_type != "0"``)

    ``median_price_per_bed`` is undefined for studios / missing bedrooms
    (``bedrooms < 1``) and excluded from the median. If the boundary file is
    absent, only listing-derived columns are produced (no metadata).
    """
    enriched = listings[listings["nta_code"].notna()].copy()
    # price_per_bed: undefined for studios (bedrooms=0) or missing bedrooms.
    valid_beds = enriched["bedrooms"].notna() & (enriched["bedrooms"] >= 1)
    enriched["price_per_bed"] = enriched["price"].where(valid_beds) / enriched["bedrooms"].where(
        valid_beds
    )
    enriched["bedrooms_missing"] = enriched["bedrooms"].isna().astype("float64")

    agg = enriched.groupby("nta_code", as_index=False).agg(
        listing_count=("listing_id", "count"),
        median_price=("price", "median"),
        median_price_per_bed=("price_per_bed", "median"),
        median_bedrooms=("bedrooms", "median"),
        median_bathrooms=("bathrooms", "median"),
        pct_missing_bedrooms=("bedrooms_missing", "mean"),
    )

    # Join boundary metadata so the table is self-describing, and attach
    # contextual point-source metrics per NTA (Strategy A). Both are skipped
    # when the boundary file is absent (no polygons -> no metadata, no PIP).
    if not settings.nta_boundaries_path.exists():
        logger.warning(
            "NTA boundary file not found at %s — indicators will lack "
            "nta_name/nta_type/cdta_* columns and point-source metrics.",
            settings.nta_boundaries_path,
        )
    else:
        boundaries = load_nta_boundaries(settings)
        meta = pd.DataFrame(
            {
                "nta_code": boundaries["nta_code"],
                "nta_name": boundaries["nta_name"],
                "nta_type": boundaries["nta_type"],
                "cdta_code": boundaries["cdta_code"],
                "cdta_name": boundaries["cdta_name"],
            }
        )
        agg = meta.merge(agg, on="nta_code", how="left")

        from nyc_apartments_map.processing import geo_sources

        metric_cols: list[str] = []
        for fn in geo_sources.POINT_SOURCE_FUNCS:
            metrics = fn(settings, boundaries=boundaries)
            if metrics.empty:
                continue
            metric_cols.extend(c for c in metrics.columns if c != "nta_code")
            agg = agg.merge(metrics, on="nta_code", how="left")
        # Point-source metrics are non-negative integer counts/sums; NTAs with
        # no matched points get NaN from the left join -> coerce to 0.
        for c in metric_cols:
            agg[c] = agg[c].fillna(0).astype("int64")

    agg["listing_count"] = agg["listing_count"].fillna(0).astype("int64")

    settings.nta_indicators_path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(agg, preserve_index=False)
    pq.write_table(table, settings.nta_indicators_path)
    logger.info("Wrote NTA indicators (%d rows) -> %s", len(agg), settings.nta_indicators_path)
    return agg
