"""NTA/CDTA enrichment: assign geographies to listing rows.

Runs after :func:`nyc_apartments_map.processing.normalize.normalize` merges and
validates loader output. Loads the 2020 NTA boundary file and fills
``nta_code``/``cdta_code`` via point-in-polygon. If the boundary file is absent,
enrichment skips with a warning (columns stay NaN) — the pipeline does NOT
hard-fail, so the pipeline works before the boundary file is dropped in.
"""

from __future__ import annotations

import logging

import pandas as pd

from nyc_apartments_map.config import Settings
from nyc_apartments_map.geo.boundaries import assign_nta, load_nta_boundaries

logger = logging.getLogger(__name__)


def enrich_listings(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Fill ``nta_code``/``cdta_code`` on listing rows via point-in-polygon.

    Loads the NTA boundary file (``settings.nta_boundaries_path``) and assigns
    each listing with valid coordinates but a missing ``nta_code`` to its NTA.
    Rows already carrying an ``nta_code`` are left untouched (idempotent).

    Graceful degradation: if the boundary file does not exist, logs a warning
    and returns ``df`` unchanged so the pipeline still produces output.
    """
    if not settings.nta_boundaries_path.exists():
        logger.warning(
            "NTA boundary file not found at %s — skipping enrichment "
            "(nta_code/cdta_code will stay NaN).",
            settings.nta_boundaries_path,
        )
        return df

    boundaries = load_nta_boundaries(settings)
    n_before = int(df["nta_code"].notna().sum())
    df = assign_nta(df, boundaries)
    n_after = int(df["nta_code"].notna().sum())
    logger.info("Enriched %d rows with NTA codes (%d -> %d assigned)", len(df), n_before, n_after)
    return df
