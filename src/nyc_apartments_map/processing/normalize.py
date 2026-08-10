"""Data processing: clean per-loader output and merge into a normalized table."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import cast

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from nyc_apartments_map.config import Settings
from nyc_apartments_map.datasets.base import COMMON_SCHEMA, DatasetLoader
from nyc_apartments_map.datasets.registry import get_loader_class, iter_loader_classes
from nyc_apartments_map.processing.aggregate import build_nta_indicators
from nyc_apartments_map.processing.enrich import enrich_listings

logger = logging.getLogger(__name__)


def validate_schema(df: pd.DataFrame, loader_name: str) -> pd.DataFrame:
    """Ensure ``df`` has every column in :data:`COMMON_SCHEMA` and coerce dtypes.

    Sets ``source`` to ``loader_name`` if missing. Raises ``ValueError`` on a
    missing column.
    """
    missing = [col for col in COMMON_SCHEMA if col not in df.columns]
    if missing:
        raise ValueError(f"Loader {loader_name!r} clean() output missing columns: {missing}")
    if "source" not in df.columns or df["source"].isna().all():
        df["source"] = loader_name
    # Coerce numeric columns where possible; leave object/str columns alone.
    for col in ("latitude", "longitude", "price", "bedrooms", "bathrooms"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # Drop rows missing coordinates — they can't be mapped.
    before = len(df)
    df = df.dropna(subset=["latitude", "longitude"])
    dropped = before - len(df)
    if dropped:
        logger.warning("Loader %r: dropped %d rows missing coordinates", loader_name, dropped)
    return df.reset_index(drop=True)


def clean_loader(loader: DatasetLoader) -> pd.DataFrame:
    """Run ``clean`` and validate the schema for one loader instance."""
    logger.info("Cleaning dataset %r", loader.name)
    df = loader.clean()
    return validate_schema(df, loader.name)


def _resolve_loader_classes(names: Iterable[str] | None) -> list[type[DatasetLoader]]:
    if names is None:
        return list(iter_loader_classes())
    return [get_loader_class(n) for n in names]


def normalize(
    names: Iterable[str] | None = None,
    settings: Settings | None = None,
    *,
    write: bool = True,
) -> pd.DataFrame:
    """Clean and concatenate datasets into one normalized DataFrame.

    Args:
        names: Optional iterable of loader names. ``None`` = use all discovered.
        settings: Settings instance; defaults to a fresh one.
        write: When true, persist the result to ``settings.normalized_path`` as parquet.

    Returns the merged DataFrame conforming to :data:`COMMON_SCHEMA`.
    """
    settings = settings or Settings()
    settings.ensure_dirs()
    classes = _resolve_loader_classes(names)
    if not classes:
        logger.warning("No dataset loaders discovered; producing empty frame.")
        return pd.DataFrame(columns=list(COMMON_SCHEMA))

    frames: list[pd.DataFrame] = []
    for cls in classes:
        loader = cls(settings=settings)
        frames.append(clean_loader(loader))

    merged = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=list(COMMON_SCHEMA))
    )
    logger.info("Normalized %d rows across %d datasets", len(merged), len(frames))

    # Enrich: fill nta_code/cdta_code via point-in-polygon against NTA boundaries.
    # Skips gracefully (NaN stays) if the boundary file is absent.
    merged = enrich_listings(merged, settings)

    if write:
        settings.normalized_path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(merged, preserve_index=False)
        pq.write_table(table, settings.normalized_path)
        logger.info("Wrote normalized parquet -> %s", settings.normalized_path)
        # Aggregate listing-derived metrics per NTA -> nta_indicators.parquet.
        build_nta_indicators(merged, settings)

    return merged


def load_normalized(settings: Settings | None = None) -> pd.DataFrame:
    """Read the previously written normalized parquet."""
    settings = settings or Settings()
    if not settings.normalized_path.exists():
        raise FileNotFoundError(
            f"Normalized parquet not found at {settings.normalized_path}. "
            "Run `nyc-apartments-map process` first."
        )
    return cast(pd.DataFrame, pq.read_table(settings.normalized_path).to_pandas())
