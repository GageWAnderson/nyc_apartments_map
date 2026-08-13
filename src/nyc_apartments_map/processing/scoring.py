"""Composite desirability scoring: weight per-NTA metrics into one score.

Reads a weight profile (``weights.yaml``) and appends sub-score + composite
``desirability_score`` columns to the per-NTA indicators table built by
:mod:`nyc_apartments_map.processing.aggregate`. Each sub-score (affordability,
safety, quality, amenity) and the composite are emitted as their own columns;
the map builder picks every numeric column up as a toggleable choropleth layer
automatically, so no builder edit is required to surface the scores on the map.

Pipeline:
  1. Derive per-1k-residential-unit rates for count metrics (so big/dense NTAs
     don't win just for being big). Denominator is ``pluto_res_units``; a zero
     or NaN denominator yields a NaN rate (excluded from that metric's rank,
     no penalty).
  2. Percentile-rank each metric to [0, 1] across residential NTAs
     (``nta_type == "0"``). Robust to the heavy tails already noted in the
     choropleth code; NaN values keep a NaN rank.
  3. Flip "lower is better" metrics via ``1 - rank`` so every sub-score points
     the same way (higher = more desirable).
  4. Sub-score = weighted mean of available metric ranks, weights renormalized
     over the metrics present for each NTA. All metrics missing -> NaN sub-score.
  5. Composite = weighted sum of sub-scores, with a missing sub-score imputed
     to 0.5 (neutral) so an NTA is not penalized for a data gap. An NTA with
     no available sub-scores at all -> NaN (renders gray via the map's NaN
     style). The composite is scaled to [0, 100] for readability.

Non-residential NTAs (``nta_type != "0"``, e.g. parks/airports/cemeteries) are
excluded from ranking and keep NaN scores. If the weights file is absent,
scoring is skipped with a warning (no columns added) -- non-fatal, mirroring
the boundary-file pattern in :mod:`nyc_apartments_map.processing.enrich`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import yaml

from nyc_apartments_map.config import Settings

logger = logging.getLogger(__name__)

#: Per-1k-units multiplier for rate derivation.
_PER = 1000.0

#: NTA type code marking a residential NTA. Non-residential NTAs (parks,
#: airports, cemeteries -- ``nta_type`` != "0") are excluded from scoring.
_RESIDENTIAL_NTA_TYPE = "0"

#: Suffix appended to a sub-score name to form its column name.
_SCORE_SUFFIX = "_score"

#: Output column for the composite score.
_COMPOSITE_COL = "desirability_score"

#: Neutral rank imputed for a wholly-missing sub-score when compositing.
_NEUTRAL_RANK = 0.5


@dataclass(frozen=True)
class RateSpec:
    """Derivation rule for a per-1k-units rate metric from raw count columns.

    ``name`` is the metric key referenced by the weight profile; ``numerator``
    is the raw count column produced by
    :func:`nyc_apartments_map.processing.aggregate.build_nta_indicators`;
    ``denominator`` is the exposure base (residential units from PLUTO).
    """

    name: str
    numerator: str
    denominator: str = "pluto_res_units"
    per: float = _PER


#: Count metrics scored as per-1k-units rates. Direct metrics (e.g.
#: ``median_price``) are not rate-derived and are referenced by their own
#: column name in the weight profile, so they do not appear here.
RATE_SPECS: list[RateSpec] = [
    RateSpec("felony_per_1k_units", "nypd_felony_count"),
    RateSpec("complaint_per_1k_units", "nypd_complaint_count"),
    RateSpec("hpd_violation_per_1k_units", "hpd_violation_count"),
    RateSpec("hpd_rent_impairing_per_1k_units", "hpd_rent_impairing_count"),
    RateSpec("nyc311_per_1k_units", "nyc311_count"),
    RateSpec("nightlife_per_1k_units", "nightlife_venue_count"),
    RateSpec("liquor_per_1k_units", "liquor_license_count"),
]


@dataclass(frozen=True)
class MetricSpec:
    """One metric entry within a sub-score of the weight profile."""

    name: str
    weight: float
    direction: str  # "higher" or "lower"


@dataclass(frozen=True)
class WeightProfile:
    """Parsed weight profile: composite sub-score weights and per-sub metrics."""

    composite: dict[str, float]  # sub-score name -> composite weight
    sub_scores: dict[str, list[MetricSpec]]  # sub-score name -> metrics

    @property
    def sub_score_cols(self) -> list[str]:
        return [f"{name}{_SCORE_SUFFIX}" for name in self.composite]


# --- Weight profile loading --------------------------------------------------


def load_weights(settings: Settings) -> WeightProfile | None:
    """Load and validate the weight profile; return ``None`` (warn) if absent."""
    path = settings.weights_path
    if not path.exists():
        logger.warning("Weights file not found at %s; skipping desirability scoring.", path)
        return None
    with path.open() as fh:
        raw = yaml.safe_load(fh)
    return _parse_profile(raw)


def _parse_profile(raw: Any) -> WeightProfile:
    """Validate the raw YAML mapping into a :class:`WeightProfile`."""
    if not isinstance(raw, dict):
        raise ValueError("weights file must be a top-level mapping")
    composite_raw = raw.get("composite")
    if not isinstance(composite_raw, dict):
        raise ValueError("weights file missing 'composite' mapping of sub-score -> weight")
    composite: dict[str, float] = {str(k): float(v) for k, v in composite_raw.items()}
    sub_scores: dict[str, list[MetricSpec]] = {}
    for name in composite:
        metrics_raw = raw.get(name, {})
        if not isinstance(metrics_raw, dict):
            raise ValueError(
                f"sub-score '{name}' must be a mapping of metric -> {{weight, direction}}"
            )
        metrics: list[MetricSpec] = []
        for mn, md in metrics_raw.items():
            if not isinstance(md, dict) or "weight" not in md or "direction" not in md:
                raise ValueError(
                    f"metric '{mn}' in sub-score '{name}' must have 'weight' and 'direction'"
                )
            direction = str(md["direction"]).lower()
            if direction not in ("higher", "lower"):
                raise ValueError(
                    f"metric '{mn}' direction must be 'higher' or 'lower', got {direction!r}"
                )
            metrics.append(
                MetricSpec(name=str(mn), weight=float(md["weight"]), direction=direction)
            )
        sub_scores[name] = metrics
    return WeightProfile(composite=composite, sub_scores=sub_scores)


# --- Rate derivation ---------------------------------------------------------


def derive_rate_columns(df: pd.DataFrame) -> None:
    """Add per-1k-units rate columns to ``df`` in place.

    A zero or NaN denominator yields a NaN rate (the NTA is excluded from that
    metric's rank rather than penalized). Source columns that are absent
    (e.g. a dataset not loaded) are skipped -- the rate column is not created,
    and the metric will be treated as missing downstream.
    """
    for spec in RATE_SPECS:
        if spec.numerator not in df.columns or spec.denominator not in df.columns:
            continue
        den = pd.to_numeric(df[spec.denominator], errors="coerce").replace(0, np.nan)
        num = pd.to_numeric(df[spec.numerator], errors="coerce")
        df[spec.name] = num / den * spec.per


# --- Ranking -----------------------------------------------------------------


def _percentile_rank(series: pd.Series, direction: str) -> pd.Series:
    """Percentile-rank a series to [0, 1] and flip if ``direction == "lower"``.

    NaN values keep a NaN rank. ``rank(pct=True)`` uses average tie-breaking,
    so tied values share a rank (e.g. many NTAs with zero crime rank together).
    """
    ranks = series.rank(pct=True)
    if direction == "lower":
        ranks = 1.0 - ranks
    return ranks


# --- Sub-score and composite -------------------------------------------------


def _sub_score(df: pd.DataFrame, metrics: list[MetricSpec], mask: pd.Series) -> pd.Series:
    """Weighted mean of available metric ranks per NTA (renormalized per NTA).

    Only rows where ``mask`` is True are ranked; others get NaN. Metrics whose
    column is absent are skipped. An NTA with no available metrics gets NaN.
    """
    rank_cols: dict[str, pd.Series] = {}
    weights: dict[str, float] = {}
    for m in metrics:
        if m.name not in df.columns:
            continue
        ranks = _percentile_rank(df.loc[mask, m.name], m.direction)
        rank_cols[m.name] = ranks
        weights[m.name] = m.weight
    if not rank_cols:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    rank_df = pd.DataFrame(rank_cols, index=df.index)
    w = pd.Series(weights, dtype="float64")
    valid = rank_df.notna()
    # Weighted sum of present ranks / sum of present weights, per row.
    num = rank_df.mul(w, axis=1).where(valid, 0.0).sum(axis=1)
    den = valid.mul(w, axis=1).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        result = num / den
    return pd.to_numeric(result, errors="coerce")


def _compose(
    df: pd.DataFrame, composite_weights: dict[str, float], available: pd.DataFrame
) -> pd.Series:
    """Composite = weighted sum of sub-scores, missing sub-scores imputed 0.5.

    Composite weights are normalized to sum to 1. An NTA with no available
    sub-scores (all NaN) gets a NaN composite so it renders as no-data on the map.
    """
    names = list(composite_weights)
    cols = [f"{n}{_SCORE_SUFFIX}" for n in names]
    sub_df = df[cols].copy()
    sub_df.columns = names
    w = pd.Series(composite_weights, dtype="float64")
    w = w / w.sum()
    sub_imputed = sub_df.fillna(_NEUTRAL_RANK)
    composite = sub_imputed.mul(w, axis=1).sum(axis=1)
    any_available = available.any(axis=1)
    return composite.where(any_available)


# --- Public entry point ------------------------------------------------------


def score_with_profile(agg: pd.DataFrame, profile: WeightProfile) -> pd.DataFrame:
    """Append sub-score + composite desirability columns using ``profile``.

    Pure function: no :class:`Settings`, no file I/O. The same math as the
    pipeline's scoring step, factored out so the API can re-score NTAs from a
    caller-supplied weight profile without re-running fetch/process. Returns
    a new DataFrame (the input is not mutated).

    Idempotent: ``derive_rate_columns`` and the sub-score/composite assignments
    all overwrite in place, so passing a frame that already carries stale
    ``*_per_1k_units`` / ``*_score`` / ``desirability_score`` columns simply
    recomputes them from the raw counts -- the indicators parquet can be fed
    in directly.

    Non-fatal skip: returns ``agg`` unchanged (warns) when ``nta_type`` is
    absent, since residential filtering is impossible without it.
    """
    if "nta_type" not in agg.columns:
        logger.warning("nta_type column absent (no boundary file?); skipping desirability scoring.")
        return agg

    df = agg.copy()
    derive_rate_columns(df)

    residential = df["nta_type"].eq(_RESIDENTIAL_NTA_TYPE)
    available: dict[str, pd.Series] = {}
    for sub, metrics in profile.sub_scores.items():
        vals = _sub_score(df, metrics, residential)
        df[f"{sub}{_SCORE_SUFFIX}"] = vals
        available[sub] = vals.notna()

    available_df = pd.DataFrame(available, index=df.index)
    composite = _compose(df, profile.composite, available_df)
    # Scale composite to [0, 100] for readability; sub-scores stay in [0, 1].
    df[_COMPOSITE_COL] = (composite * 100.0).round(2)

    logger.info(
        "Computed desirability scores (%d residential NTAs, %d scored)",
        int(residential.sum()),
        int(composite.notna().sum()),
    )
    return df


def add_desirability_scores(agg: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Append sub-score + composite desirability columns to the NTA indicators.

    Thin wrapper around :func:`score_with_profile` that loads the weight
    profile from ``settings.weights_path``. Non-fatal: returns ``agg``
    unchanged (warns) if the weights file is absent. See
    :func:`score_with_profile` for the scoring math, idempotency notes, and the
    ``nta_type`` requirement.
    """
    profile = load_weights(settings)
    if profile is None:
        return agg
    return score_with_profile(agg, profile)
