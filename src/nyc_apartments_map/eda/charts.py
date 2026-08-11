"""Chart generation for EDA reports.

Each ``plot_*`` helper writes a single PNG to ``out_path`` and returns ``True``
on success, or returns ``False`` when there's nothing meaningful to plot (e.g.
no nulls, no numeric columns). The caller embeds the saved PNG into Markdown.

Design notes:

* Uses the non-interactive ``Agg`` backend so charts render headless (no display
  required) and works under ``file://``.
* Numeric columns with ≤ ``CATEGORICAL_NUMERIC_THRESHOLD`` unique values are
  drawn as bar charts of their value counts rather than histograms — integer
  codes (borough ids, status codes, …) read far better as bars.
* Near-unique numeric columns (ratio > ``NEAR_UNIQUE_RATIO``) are treated as
  identifiers and skipped: a histogram of ``violationid`` or ``bbl`` is noise.
* Counts are drawn as horizontal bars with value labels so the chart stays
  readable even when embedded at modest width in Markdown.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import matplotlib

matplotlib.use("Agg")  # headless backend before importing pyplot

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from nyc_apartments_map.eda.core import JsonProfile

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
#: Numeric columns with at most this many unique values render as bar charts.
CATEGORICAL_NUMERIC_THRESHOLD = 20
#: Numeric columns whose unique-ratio exceeds this are candidate IDs — but only
#: skipped when they ALSO have high absolute cardinality (see ``_meaningful_numeric``).
NEAR_UNIQUE_RATIO = 0.9
#: Absolute unique-value count above which a near-unique column is treated as
#: an identifier. Small files (e.g. 197 census rows) are near-unique by chance
#: but rarely exceed a few hundred unique values, so their continuous columns
#: are kept; real ID columns (violationid, bin, …) have tens of thousands.
HIGH_CARDINALITY_ID = 1000
#: NYC-specific identifier column names to exclude (especially from the heatmap,
#: where derived codes produce spurious correlations).
_ID_NAMES = {
    "bbl",
    "bin",
    "block",
    "lot",
    "streetcode",
    "censustract",
    "tract",
    "tract_10",
    "tract2010",
    "bct2020",
    "bctcb2020",
    "ref_bbl",
    "appbbl",
    "objectid",
    "gisobjid",
    "fc_subsidy_id",
    "cmplnt_num",
    "postcode",
    "zip",
    "zip_code",
    "ordernumber",
}
#: Maximum numeric columns drawn in the histogram grid.
MAX_HISTOGRAM_COLS = 12
#: Maximum numeric columns in the correlation heatmap (readability).
MAX_HEATMAP_COLS = 20
#: Maximum low-cardinality object columns drawn in the categorical grid.
MAX_CATEGORICAL_CHARTS = 8
#: Minimum numeric columns required to render a correlation heatmap.
MIN_HEATMAP_COLS = 4
#: DPI for saved PNGs — crisp at 2× a typical Markdown image width.
CHART_DPI = 120
#: Colors reused across charts (a calm, colorblind-friendly-ish palette).
PALETTE = [
    "#4C72B0",
    "#DD8452",
    "#55A868",
    "#C44E52",
    "#8172B3",
    "#937860",
    "#DA8BC3",
    "#8C8C8C",
    "#CCB974",
    "#64B5CD",
]

# Apply a clean default style once at import time.
with contextlib.suppress(OSError):
    plt.style.use("seaborn-v0_8-whitegrid")
plt.rcParams.update(
    {
        "figure.facecolor": "white",
        "axes.grid": True,
        "grid.color": "#e6e6e6",
        "grid.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
        "axes.labelsize": 9,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "font.size": 10,
    }
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _truncate(label: Any, limit: int = 22) -> str:
    s = str(label)
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _save(fig: Any, out_path: Path) -> bool:
    """Save a figure to ``out_path`` and close it; return ``True`` on success."""
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=CHART_DPI, bbox_inches="tight")
        return True
    except Exception:  # noqa: BLE001 - chart failures must not abort the EDA
        logger.warning("Failed to save chart %s", out_path, exc_info=True)
        return False
    finally:
        plt.close(fig)


def _looks_like_id(name: str) -> bool:
    """Heuristic: does this column name look like an identifier/code?"""
    low = name.strip().lower().replace(" ", "_")
    if low in _ID_NAMES:
        return True
    return low.endswith("_id") or low.endswith("id")


def _meaningful_numeric(
    sample: pd.DataFrame,
    nunique: dict[str, int],
    *,
    for_heatmap: bool = False,
) -> list[str]:
    """Numeric columns worth plotting.

    Skips constants, high-cardinality identifiers (near-unique *and* many
    unique values, e.g. ``violationid`` / ``bin``), and — for the heatmap — any
    identifier-named column (so derived codes like ``bbl`` / ``block`` / ``lot``
    don't produce spurious correlations). Low-cardinality categorical codes
    (e.g. ``boroid`` = 1-5, ``currentstatusid`` = 20) are kept for histograms
    (they read well as bar charts) but dropped from the heatmap.
    """
    n_rows = len(sample)
    if n_rows == 0:
        return []
    cols: list[str] = []
    for col in sample.columns:
        col = str(col)
        uniq = nunique.get(col, 0)
        if uniq <= 1:
            continue  # constant or empty
        if uniq / n_rows > NEAR_UNIQUE_RATIO and uniq > HIGH_CARDINALITY_ID:
            continue  # high-cardinality arbitrary identifier
        if for_heatmap and _looks_like_id(col):
            continue  # id/code-named columns add noise to correlations
        if not for_heatmap and _looks_like_id(col) and uniq > HIGH_CARDINALITY_ID:
            continue  # id-named high-card column (buildingid, streetcode, …)
        cols.append(col)
    return cols


def _severity_color(pct: float) -> str:
    """Color a null-percentage bar by severity."""
    if pct >= 50:
        return "#C44E52"  # red
    if pct >= 10:
        return "#DD8452"  # orange
    return "#4C72B0"  # blue


# --------------------------------------------------------------------------- #
# CSV charts
# --------------------------------------------------------------------------- #
def plot_missing_data(
    null_counts: dict[str, int],
    total: int,
    out_path: Path,
) -> bool:
    """Horizontal bar of null percentage per column (only columns with nulls)."""
    if total <= 0:
        return False
    pcts = {c: n / total * 100 for c, n in null_counts.items() if n > 0}
    pcts = dict(sorted(pcts.items(), key=lambda kv: kv[1], reverse=True))
    if not pcts:
        return False  # nothing missing -> no chart
    # Cap to keep the chart readable; very wide files would otherwise produce
    # a crowded strip. The full null breakdown lives in the schema table.
    pcts = dict(list(pcts.items())[:30])

    fig, ax = plt.subplots(figsize=(9, max(3, 0.32 * len(pcts) + 1)))
    labels = [_truncate(c, 30) for c in pcts]
    values = list(pcts.values())
    colors = [_severity_color(v) for v in values]
    y = np.arange(len(labels))
    ax.barh(y, values, color=colors, edgecolor="white", linewidth=0.6)
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel("% of rows that are null")
    ax.set_title("Missing data by column")
    ax.set_xlim(0, max(100, max(values) * 1.1))
    for i, v in enumerate(values):
        ax.text(v + 0.5, i, f"{v:.1f}%", va="center", fontsize=8)
    return _save(fig, out_path)


def plot_numeric_grid(
    numeric_sample: pd.DataFrame,
    nunique: dict[str, int],
    out_path: Path,
) -> bool:
    """Grid of per-column distributions: bars for low-cardinality, hist otherwise."""
    cols = _meaningful_numeric(numeric_sample, nunique)[:MAX_HISTOGRAM_COLS]
    if not cols:
        return False

    n = len(cols)
    ncols = 3
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.0 * nrows))
    axes_arr = np.atleast_1d(axes).ravel()
    for i, col in enumerate(cols):
        ax = axes_arr[i]
        series = pd.to_numeric(numeric_sample[col], errors="coerce").dropna()
        uniq = nunique.get(col, 0)
        if uniq <= CATEGORICAL_NUMERIC_THRESHOLD:
            vc = series.value_counts().sort_index()
            ax.bar(
                range(len(vc)),
                vc.values,
                color=PALETTE[i % len(PALETTE)],
                edgecolor="white",
                linewidth=0.4,
            )
            ax.set_xticks(range(len(vc)))
            ax.set_xticklabels([_truncate(v, 10) for v in vc.index], rotation=0)
        else:
            ax.hist(
                series,
                bins=30,
                color=PALETTE[i % len(PALETTE)],
                edgecolor="white",
                linewidth=0.4,
            )
        ax.set_title(_truncate(col, 28))
        ax.tick_params(axis="x", labelrotation=45)
        ax.set_ylabel("count")
    for j in range(n, len(axes_arr)):
        axes_arr[j].set_visible(False)
    fig.suptitle("Numeric column distributions", y=1.005, fontsize=12, fontweight="bold")
    return _save(fig, out_path)


def plot_categorical_grid(
    object_value_counts: dict[str, pd.Series],
    out_path: Path,
) -> bool:
    """Grid of horizontal bar charts for low-cardinality object columns."""
    items = list(object_value_counts.items())[:MAX_CATEGORICAL_CHARTS]
    if not items:
        return False

    n = len(items)
    ncols = 2
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(6.5 * ncols, 3.0 * nrows))
    axes_arr = np.atleast_1d(axes).ravel()
    for i, (col, vc) in enumerate(items):
        ax = axes_arr[i]
        vc = vc.head(15)
        y = np.arange(len(vc))
        ax.barh(y, vc.values, color=PALETTE[i % len(PALETTE)], edgecolor="white", linewidth=0.5)
        ax.set_yticks(y)
        ax.set_yticklabels([_truncate(v, 22) for v in vc.index])
        ax.invert_yaxis()
        ax.set_title(_truncate(col, 30))
        ax.set_xlabel("count")
        ax.tick_params(axis="x", labelrotation=0)
    for j in range(n, len(axes_arr)):
        axes_arr[j].set_visible(False)
    fig.suptitle("Categorical value counts", y=1.005, fontsize=12, fontweight="bold")
    return _save(fig, out_path)


def plot_correlation(
    numeric_sample: pd.DataFrame,
    nunique: dict[str, int],
    out_path: Path,
) -> bool:
    """Correlation heatmap over meaningful numeric columns (≥4 required)."""
    cols = _meaningful_numeric(numeric_sample, nunique, for_heatmap=True)[:MAX_HEATMAP_COLS]
    if len(cols) < MIN_HEATMAP_COLS:
        return False
    corr = numeric_sample[cols].corr(numeric_only=True)
    if corr.isna().all().all():
        return False

    size = max(6, 0.45 * len(cols) + 2.5)
    fig, ax = plt.subplots(figsize=(size, size))
    corr_arr = corr.to_numpy()
    im = ax.imshow(corr_arr, vmin=-1, vmax=1, cmap="coolwarm", aspect="auto")
    labels = [_truncate(c, 18) for c in cols]
    ax.set_xticks(np.arange(len(cols)))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_yticks(np.arange(len(cols)))
    ax.set_yticklabels(labels)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="Pearson r")
    if len(cols) <= 12:
        for i in range(len(cols)):
            for j in range(len(cols)):
                val = float(corr_arr[i, j])
                if pd.isna(val):
                    continue
                ax.text(
                    j,
                    i,
                    f"{val:.2f}",
                    ha="center",
                    va="center",
                    fontsize=7,
                    color="black",
                )
    ax.set_title("Numeric correlation")
    return _save(fig, out_path)


# --------------------------------------------------------------------------- #
# JSON charts
# --------------------------------------------------------------------------- #
def plot_geojson_summary(profile: JsonProfile, out_path: Path) -> bool:
    """Two-panel summary: features by borough and geometry-type distribution."""
    boro_counts: dict[str, int] = profile.extra.get("boro_counts", {})
    geom = profile.geometry_types
    if not boro_counts and not geom:
        return False

    has_boro = bool(boro_counts)
    has_geom = bool(geom)
    ncols = sum([has_boro, has_geom])
    fig, axes = plt.subplots(1, ncols, figsize=(6.5 * ncols, 4.5))
    axes_arr = np.atleast_1d(axes).ravel()
    idx = 0

    if has_boro:
        ax = axes_arr[idx]
        idx += 1
        labels = list(boro_counts.keys())
        values = list(boro_counts.values())
        ax.bar(
            range(len(labels)),
            values,
            color=PALETTE[: len(labels)],
            edgecolor="white",
            linewidth=0.6,
        )
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels([_truncate(lbl, 14) for lbl in labels], rotation=30, ha="right")
        ax.set_ylabel("features")
        ax.set_title("Features by borough")
        for i, v in enumerate(values):
            ax.text(i, v, f"{v}", ha="center", va="bottom", fontsize=8)

    if has_geom:
        ax = axes_arr[idx]
        idx += 1
        labels = list(geom.keys())
        values = list(geom.values())
        ax.bar(
            range(len(labels)),
            values,
            color=PALETTE[2 : 2 + len(labels)],
            edgecolor="white",
            linewidth=0.6,
        )
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels([_truncate(lbl, 14) for lbl in labels], rotation=30, ha="right")
        ax.set_ylabel("features")
        ax.set_title("Geometry types")
        for i, v in enumerate(values):
            ax.text(i, v, f"{v}", ha="center", va="bottom", fontsize=8)

    fig.suptitle(
        f"GeoJSON summary — {profile.feature_count} features",
        y=1.02,
        fontsize=12,
        fontweight="bold",
    )
    return _save(fig, out_path)


def plot_overpass_summary(profile: JsonProfile, out_path: Path) -> bool:
    """Two-panel summary: element types and top amenities for an OSM export."""
    elem = profile.element_types
    amen = profile.amenity_counts
    if not elem and not amen:
        return False

    has_elem = bool(elem)
    has_amen = bool(amen)
    ncols = sum([has_elem, has_amen])
    fig, axes = plt.subplots(1, ncols, figsize=(6.5 * ncols, max(4.5, 0.4 * len(amen) + 2)))
    axes_arr = np.atleast_1d(axes).ravel()
    idx = 0

    if has_elem:
        ax = axes_arr[idx]
        idx += 1
        labels = list(elem.keys())
        values = list(elem.values())
        ax.bar(
            range(len(labels)),
            values,
            color=PALETTE[: len(labels)],
            edgecolor="white",
            linewidth=0.6,
        )
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=0)
        ax.set_ylabel("elements")
        ax.set_title("Element types")
        for i, v in enumerate(values):
            ax.text(i, v, f"{v}", ha="center", va="bottom", fontsize=8)

    if has_amen:
        ax = axes_arr[idx]
        idx += 1
        labels = list(amen.keys())
        values = list(amen.values())
        y = np.arange(len(labels))
        ax.barh(y, values, color=PALETTE[1 : 1 + len(labels)], edgecolor="white", linewidth=0.6)
        ax.set_yticks(y)
        ax.set_yticklabels([_truncate(lbl, 22) for lbl in labels])
        ax.invert_yaxis()
        ax.set_xlabel("count")
        ax.set_title("Top amenities")
        for i, v in enumerate(values):
            ax.text(v, i, f"{v}", ha="left", va="center", fontsize=8)

    total = profile.extra.get("elements_total", 0)
    fig.suptitle(
        f"OSM summary — {total} elements",
        y=1.02,
        fontsize=12,
        fontweight="bold",
    )
    return _save(fig, out_path)
