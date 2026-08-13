"""Service layer: re-score NTAs from the cached indicators parquet.

The indicators parquet (written by :mod:`nyc_apartments_map.processing.aggregate`)
carries the raw count numerators + ``pluto_res_units`` denominator +
``nta_type``, which is everything :func:`score_with_profile` needs to recompute
scores. The parquet is loaded per request (small ~80 KB) and re-scored on each
call, so weight changes never touch the fetch/process pipeline.

Framework-agnostic: returns plain dicts and raises typed exceptions that the
route layer maps to HTTP status codes. This keeps the service unit-testable
without FastAPI/pydantic.
"""

from __future__ import annotations

import logging
from typing import Any

import branca.colormap as cm
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from nyc_apartments_map.config import Settings
from nyc_apartments_map.processing.scoring import (
    WeightProfile,
    load_weights,
    score_with_profile,
)

logger = logging.getLogger(__name__)

#: Fill color for NTAs with no composite score. Matches ``_NAN_STYLE`` in the
#: map builder so the restyled layer is visually consistent with a fresh build.
_NAN_COLOR = "#cccccc"

#: Composite output column re-styled by the map UI.
_COMPOSITE_COL = "desirability_score"


class IndicatorsUnavailable(FileNotFoundError):
    """Indicators parquet missing at request time (maps to HTTP 503)."""


class ProfileMismatch(ValueError):
    """Request keys don't match ``weights.yaml`` (maps to HTTP 422)."""


def _load_indicators(settings: Settings) -> pd.DataFrame:
    """Read ``nta_indicators.parquet``; raise if absent.

    Re-reading on each request is intentional: the parquet is ~80 KB, so the
    cost is negligible, and a stateless service is trivially testable (no
    process-global cache to reset between tests with different fixtures).
    """
    if not settings.nta_indicators_path.exists():
        raise IndicatorsUnavailable(
            f"NTA indicators not found at {settings.nta_indicators_path}; "
            "run `nyc-apartments-map process` first."
        )
    return pq.read_table(settings.nta_indicators_path).to_pandas()  # type: ignore[no-any-return]


def _colormap_domain(values: pd.Series) -> tuple[float, float]:
    """1st/99th percentile domain of ``values`` (clamped so ``hi > lo``).

    Mirrors the clamp in the map builder's ``_make_choropleth_style`` so the
    API and a fresh build render identically for the same data.
    """
    vals = values.dropna()
    if vals.empty:
        return 0.0, 1.0
    lo, hi = float(np.nanpercentile(vals, 1)), float(np.nanpercentile(vals, 99))
    if hi <= lo:
        hi = lo + 1.0
    return lo, hi


def _colors_for_scores(scores: pd.Series, lo: float, hi: float) -> list[str]:
    """Hex fill color per row from a branca ``YlOrRd_09`` scale over [lo, hi].

    NaN scores get :data:`_NAN_COLOR`. Values outside the domain are clamped by
    ``LinearColormap.rgb_hex_str`` automatically. Returns a position-aligned
    list (caller iterates the frame in the same order).
    """
    colormap = cm.linear.YlOrRd_09.scale(lo, hi)  # type: ignore[attr-defined]
    return [_NAN_COLOR if pd.isna(v) else colormap.rgb_hex_str(float(v)) for v in scores.tolist()]


def _maybe_float(v: Any) -> float | None:
    """Coerce a numpy scalar to a python float (or None if NaN)."""
    if pd.isna(v):
        return None
    return float(v)


def _override_profile(
    profile: WeightProfile, composite_override: dict[str, float]
) -> WeightProfile:
    """Return a copy of ``profile`` with normalized composite weights.

    Validates that the override keys exactly match the profile's sub-score
    keys (raises :class:`ProfileMismatch` otherwise) and that they sum to a
    positive value.
    """
    req_keys = set(composite_override)
    profile_keys = set(profile.composite)
    if req_keys != profile_keys:
        raise ProfileMismatch(
            f"Request sub-score keys {sorted(req_keys)} do not match "
            f"weights.yaml keys {sorted(profile_keys)}"
        )
    total = sum(composite_override.values())
    if total <= 0:
        raise ProfileMismatch("composite weights must sum to a positive value")
    normalized = {k: v / total for k, v in composite_override.items()}
    return WeightProfile(composite=normalized, sub_scores=profile.sub_scores)


def compute_scores(settings: Settings, composite_override: dict[str, float]) -> dict[str, Any]:
    """Re-score NTAs under ``composite_override`` and build the response dict.

    Loads the base profile from ``weights.yaml``, replaces its composite
    weights with the normalized override, re-scores via
    :func:`score_with_profile`, and returns per-NTA scores + the colormap
    domain + hex colors. Shape matches :class:`ScoreResponse`.

    Raises:
        IndicatorsUnavailable: parquet missing (caller maps to HTTP 503).
        ProfileMismatch: request keys != profile keys, or non-positive sum
            (caller maps to HTTP 422).
    """
    profile = load_weights(settings)
    if profile is None:
        raise ProfileMismatch(f"Weights file not found at {settings.weights_path}; cannot score.")

    overridden = _override_profile(profile, composite_override)
    df = _load_indicators(settings)
    scored = score_with_profile(df, overridden).reset_index(drop=True)

    sub_score_cols = overridden.sub_score_cols
    scores_col = scored[_COMPOSITE_COL]
    lo, hi = _colormap_domain(scores_col)
    colors = _colors_for_scores(scores_col, lo, hi)

    scores_map: dict[str, dict[str, Any]] = {}
    for pos, (_, row) in enumerate(scored.iterrows()):
        nta = row["nta_code"]
        scores_map[nta] = {
            "desirability_score": _maybe_float(row[_COMPOSITE_COL]),
            "sub_scores": {c: _maybe_float(row[c]) for c in sub_score_cols},
            "color": colors[pos],
        }
    return {
        "metric": _COMPOSITE_COL,
        "domain": {"lo": lo, "hi": hi},
        "scores": scores_map,
    }


def build_profile_out(settings: Settings) -> dict[str, Any] | None:
    """Build a dict mirroring :class:`WeightProfileOut` from ``weights.yaml``.

    Returns ``None`` if the weights file is absent (route maps to HTTP 404).
    """
    profile = load_weights(settings)
    if profile is None:
        return None
    return {
        "composite": dict(profile.composite),
        "sub_scores": [
            {
                "name": name,
                "composite_weight": profile.composite[name],
                "metrics": [
                    {"name": m.name, "weight": m.weight, "direction": m.direction}
                    for m in profile.sub_scores[name]
                ],
            }
            for name in profile.composite
        ],
    }
