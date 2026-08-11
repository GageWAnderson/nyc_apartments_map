"""Exploratory data analysis (EDA) over raw CSV/JSON datasets.

Generates one Markdown report per ``.csv``/``.json`` file under ``data/raw``
plus a summary ``index.md`` into ``data/output/eda``.

Profiling strategy:

* Small files (``< LARGE_FILE_THRESHOLD``) are read in full so every statistic
  (shape, nulls, ``describe``, cardinality) is exact.
* Large files are profiled from a leading row sample for distribution stats
  (dtypes, ``describe``, value-counts, head) while the **total row count** is
  computed exactly via a single-column chunked pass that respects quoted
  newlines. Sample-derived stats are labelled accordingly.

JSON files (GeoJSON ``FeatureCollection`` and Overpass ``elements`` payloads
are both detected) are summarized structurally: feature/element counts,
geometry/element-type distributions, property/tag schemas, and top values.
"""

from __future__ import annotations

import codecs
import json
import logging
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from nyc_apartments_map.config import Settings

logger = logging.getLogger(__name__)

#: Rows read from the head of a large file for sample-based statistics.
SAMPLE_ROWS = 100_000
#: Files at or above this size are treated as "large" and sampled for stats.
LARGE_FILE_THRESHOLD = 100 * 1024 * 1024  # 100 MiB
#: Chunk size used by the exact single-column row-count pass.
COUNT_CHUNK = 500_000
#: Object columns with at most this many unique values get value-count tables.
LOW_CARDINALITY = 50
#: Maximum rows shown in any value-count table.
TOP_N_VALUES = 15


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _human_size(num_bytes: int) -> str:
    """Return a human-readable byte size, e.g. ``12.3 MiB``."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"


def _fmt(value: Any) -> str:
    """Format a scalar cell value for a Markdown table."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        if pd.isna(value):
            return ""
        # Integral floats (counts, IDs, zip codes, percentiles) read better
        # as plain integers than as scientific notation.
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        if abs(value) >= 1e6 or 0 < abs(value) < 1e-4:
            return f"{value:.6g}"
        return f"{value:.4g}"
    s = str(value).replace("\n", " ").replace("\r", " ")
    if len(s) > 80:
        s = s[:77] + "..."
    return s


def _md_table(header: list[str], rows: list[list[str]]) -> str:
    """Render a list-of-rows as a GitHub-flavored Markdown table."""

    def esc(cell: str) -> str:
        return cell.replace("|", "\\|")

    lines = [
        "| " + " | ".join(esc(h) for h in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(esc(str(c)) for c in row) + " |")
    return "\n".join(lines) + "\n"


def _fmt_counter(counter: dict[str, int]) -> str:
    """Render a ``{value: count}`` mapping as ``"a: 3, b: 1"`` or ``(none)``."""
    return ", ".join(f"{k}: {v}" for k, v in counter.items()) or "(none)"


def _df_as_md_table(df: pd.DataFrame, *, index: bool = False) -> str:
    """Render a (small) DataFrame as a Markdown table."""
    if df is None or df.empty:
        return "_No rows._\n"
    header: list[str] = ([] if not index else ["index"]) + [str(c) for c in df.columns]
    rows: list[list[str]] = []
    for idx, row in df.iterrows():
        cells: list[str] = [] if not index else [_fmt(idx)]
        cells.extend(_fmt(row[c]) for c in df.columns)
        rows.append(cells)
    return _md_table(header, rows)


# --------------------------------------------------------------------------- #
# CSV profiling
# --------------------------------------------------------------------------- #
@dataclass
class CsvProfile:
    """Container for a single CSV file's profiling results."""

    path: Path
    size_bytes: int
    is_large: bool
    total_rows: int
    sample_rows: int
    columns: list[str]
    dtypes: dict[str, str]
    null_counts: dict[str, int]
    nulls_exact: bool
    nunique: dict[str, int]
    nunique_exact: bool
    numeric_describe: pd.DataFrame
    object_value_counts: dict[str, pd.Series]
    numeric_sample: pd.DataFrame
    head: pd.DataFrame


def _count_rows(path: Path, encoding: str) -> int:
    """Exact row count for a (possibly huge) CSV via a single-column pass.

    Reads only the first column but still parses every row, so quoted
    newlines and commas in omitted columns are respected.
    """
    total = 0
    reader = pd.read_csv(
        path,
        usecols=[0],
        dtype=str,
        chunksize=COUNT_CHUNK,
        low_memory=False,
        on_bad_lines="warn",
        encoding=encoding,
    )
    for chunk in reader:
        total += len(chunk)
    return total


def _detect_encoding(path: Path) -> str:
    """Scan the entire file to pick a decoding that won't raise.

    Returns ``"utf-8"`` if every byte is valid UTF-8, else ``"cp1252"`` (Python's
    cp1252 codec maps every byte without raising, so it's a safe fallback for
    the Windows-1252 / non-breaking-space bytes that occasionally appear in NYC
    open-data CSVs). The scan uses an incremental UTF-8 decoder over 1 MiB
    chunks, so it stays cheap even for multi-gigabyte files.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    decoder.decode(b"", final=True)
                    return "utf-8"
                decoder.decode(chunk)
    except UnicodeDecodeError:
        return "cp1252"


def analyze_csv(path: Path) -> CsvProfile:
    """Profile a single CSV file, sampling large files for distribution stats."""
    size = path.stat().st_size
    is_large = size >= LARGE_FILE_THRESHOLD
    encoding = _detect_encoding(path)

    # Sample read: proper dtype inference + distribution stats + head.
    nrows = SAMPLE_ROWS if is_large else None
    sample = pd.read_csv(
        path, nrows=nrows, low_memory=False, on_bad_lines="warn", encoding=encoding
    )
    sample_rows_used = len(sample)

    # Exact total row count.
    total_rows = _count_rows(path, encoding) if is_large else sample_rows_used

    columns = [str(c) for c in sample.columns]
    dtypes = {str(c): str(sample[c].dtype) for c in sample.columns}

    null_counts = {str(c): int(sample[c].isna().sum()) for c in sample.columns}
    nulls_exact = not is_large  # small file read in full -> exact

    nunique = {str(c): int(sample[c].nunique(dropna=True)) for c in sample.columns}
    nunique_exact = not is_large

    numeric = sample.select_dtypes(include="number")
    if not numeric.empty:
        numeric_describe = numeric.describe().T
        numeric_describe.index.name = "column"
        numeric_describe = numeric_describe.reset_index()
    else:
        numeric_describe = pd.DataFrame()

    object_cols = sample.select_dtypes(exclude="number").columns
    object_value_counts: dict[str, pd.Series] = {}
    for col in object_cols:
        if int(sample[col].nunique(dropna=True)) <= LOW_CARDINALITY:
            vc = sample[col].value_counts(dropna=True).head(TOP_N_VALUES)
            if not vc.empty:
                object_value_counts[str(col)] = vc

    return CsvProfile(
        path=path,
        size_bytes=size,
        is_large=is_large,
        total_rows=total_rows,
        sample_rows=sample_rows_used,
        columns=columns,
        dtypes=dtypes,
        null_counts=null_counts,
        nulls_exact=nulls_exact,
        nunique=nunique,
        nunique_exact=nunique_exact,
        numeric_describe=numeric_describe,
        object_value_counts=object_value_counts,
        numeric_sample=numeric,
        head=sample.head(5),
    )


def render_csv_markdown(
    profile: CsvProfile,
    *,
    chart_dir: Path | None = None,
    rel_chart_dir: str = "",
) -> str:
    """Render a :class:`CsvProfile` as a Markdown report.

    When ``chart_dir`` is provided, PNG charts are generated there and embedded
    via ``rel_chart_dir`` (a path relative to the report file).
    """
    p = profile
    lines: list[str] = []
    lines.append(f"# EDA: `{p.path.name}`")
    lines.append("")
    lines.append(f"> Source: `{p.path}`")
    lines.append("")

    # Lazy import keeps matplotlib out of the import chain for non-EDA uses.
    from nyc_apartments_map.eda.charts import (
        plot_categorical_grid,
        plot_correlation,
        plot_missing_data,
        plot_numeric_grid,
    )

    lines.append("## File overview")
    lines.append("")
    overview = [
        ["File size", _human_size(p.size_bytes)],
        ["Format", "CSV"],
        ["Total rows", f"{p.total_rows:,}"],
        ["Columns", f"{len(p.columns):,}"],
        ["Profiling mode", "sampled" if p.is_large else "full read"],
    ]
    if p.is_large:
        overview.append(["Sample rows used", f"{p.sample_rows:,} (of {p.total_rows:,})"])
    lines.append(_md_table(["Attribute", "Value"], overview))
    lines.append("")
    if p.is_large:
        lines.append(
            "_Distribution statistics (dtypes, describe, value-counts, head, charts) are "
            f"derived from the first {p.sample_rows:,} rows. The total row count "
            "is exact (single-column pass)._\n"
        )

    lines.append("## Schema")
    lines.append("")
    schema_rows: list[list[str]] = []
    null_note = "" if p.nulls_exact else " (sample)"
    uniq_note = "" if p.nunique_exact else " (sample)"
    for col in p.columns:
        null = p.null_counts.get(col, 0)
        null_pct = (null / p.sample_rows * 100) if p.sample_rows else 0.0
        if p.nulls_exact and p.total_rows:
            null_pct = null / p.total_rows * 100
        schema_rows.append(
            [
                col,
                p.dtypes.get(col, ""),
                f"{p.total_rows - null:,}" if p.nulls_exact else f"{p.sample_rows - null:,}",
                f"{null:,}{null_note}",
                f"{null_pct:.2f}%",
                f"{p.nunique.get(col, 0):,}{uniq_note}",
            ]
        )
    lines.append(
        _md_table(
            ["column", "dtype", "non-null", "null", "null %", "unique"],
            schema_rows,
        )
    )
    lines.append("")

    # --- Charts: missing data ---------------------------------------------- #
    def _img(name: str, alt: str, caption: str = "") -> str:
        rel = f"{rel_chart_dir}/{name}" if rel_chart_dir else name
        block = f"![{alt}]({rel})"
        if caption:
            block += f"\n\n_{caption}_"
        return block

    if chart_dir is not None:
        missing_path = chart_dir / "missing.png"
        total_for_nulls = p.total_rows if p.nulls_exact else p.sample_rows
        if plot_missing_data(p.null_counts, total_for_nulls, missing_path):
            lines.append("## Missing data")
            lines.append("")
            lines.append(_img("missing.png", "Missing data by column"))
            lines.append("")

    lines.append("## Sample rows (head, transposed)")
    lines.append("")
    head_t = p.head.T.reset_index()
    head_t.columns = ["column"] + [f"row {i}" for i in range(len(p.head))]
    lines.append(_df_as_md_table(head_t))
    lines.append("")

    if not p.numeric_describe.empty:
        lines.append("## Numeric summary statistics")
        lines.append("")
        desc_note = "" if not p.is_large else " (sample-based)"
        lines.append(f"_`describe()` over numeric columns{desc_note}._\n")
        lines.append(_df_as_md_table(p.numeric_describe))
        lines.append("")

    # --- Charts: numeric distributions ------------------------------------ #
    if chart_dir is not None:
        numeric_path = chart_dir / "numeric.png"
        if plot_numeric_grid(p.numeric_sample, p.nunique, numeric_path):
            lines.append("## Numeric distributions")
            lines.append("")
            chart_note = "" if not p.is_large else " (sample-based)"
            lines.append(
                _img(
                    "numeric.png",
                    "Numeric column distributions",
                    f"Per-column distributions{chart_note}. Low-cardinality numeric "
                    f"columns render as bar charts; near-unique/ID-like columns are "
                    f"skipped.",
                )
            )
            lines.append("")

        # --- Charts: correlation heatmap ---------------------------------- #
        corr_path = chart_dir / "correlation.png"
        if plot_correlation(p.numeric_sample, p.nunique, corr_path):
            lines.append("## Correlation heatmap")
            lines.append("")
            corr_note = "" if not p.is_large else " (sample-based)"
            lines.append(
                _img(
                    "correlation.png",
                    "Numeric correlation heatmap",
                    f"Pearson correlation over meaningful numeric columns{corr_note}.",
                )
            )
            lines.append("")

    if p.object_value_counts:
        lines.append("## Categorical value counts (low cardinality)")
        lines.append("")
        vc_note = "" if not p.is_large else " (sample-based)"
        lines.append(
            f"_Top values for object columns with ≤{LOW_CARDINALITY} unique values{vc_note}._\n"
        )
        for col, vc in p.object_value_counts.items():
            lines.append(f"### `{col}`")
            lines.append("")
            vc_df = vc.reset_index()
            vc_df.columns = ["value", "count"]
            lines.append(_df_as_md_table(vc_df))
            lines.append("")

    # --- Charts: categorical bar charts ----------------------------------- #
    if chart_dir is not None and p.object_value_counts:
        cat_path = chart_dir / "categorical.png"
        if plot_categorical_grid(p.object_value_counts, cat_path):
            lines.append("## Categorical distributions")
            lines.append("")
            chart_note = "" if not p.is_large else " (sample-based)"
            lines.append(
                _img(
                    "categorical.png",
                    "Categorical value counts",
                    f"Top values per low-cardinality object column{chart_note}.",
                )
            )
            lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# JSON profiling
# --------------------------------------------------------------------------- #
@dataclass
class JsonProfile:
    """Container for a single JSON file's structural profiling results."""

    path: Path
    size_bytes: int
    kind: str  # "geojson" | "overpass" | "object"
    raw_keys: list[str]
    feature_count: int
    geometry_types: dict[str, int]
    element_types: dict[str, int]
    property_columns: list[str]
    property_sample: dict[str, Any]
    property_dtypes: dict[str, str]
    top_tag_keys: list[tuple[str, int]]
    amenity_counts: dict[str, int]
    sample_element: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)


def _geojson_profile(path: Path, data: dict[str, Any], size: int) -> JsonProfile:
    features = data.get("features") or []
    geom_counter: Counter[str] = Counter()
    boro_counter: Counter[str] = Counter()
    prop_keys: Counter[str] = Counter()
    prop_sample: dict[str, Any] = {}
    for feat in features:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if gtype:
            geom_counter[gtype] += 1
        props = feat.get("properties") or {}
        if isinstance(props, dict):
            prop_keys.update(props.keys())
            boro = props.get("BoroName")
            if isinstance(boro, str) and boro:
                boro_counter[boro] += 1
            if not prop_sample and props:
                prop_sample = dict(list(props.items())[:12])

    columns = [str(k) for k, _ in prop_keys.most_common()]
    # Infer coarse dtypes from the sampled feature that has all-ish keys.
    dtypes: dict[str, str] = {}
    for feat in features:
        props = feat.get("properties") or {}
        if isinstance(props, dict) and props:
            for k, v in props.items():
                if k not in dtypes and v is not None and not (isinstance(v, str) and v == ""):
                    dtypes[str(k)] = type(v).__name__
            if len(dtypes) >= len(columns):
                break

    return JsonProfile(
        path=path,
        size_bytes=size,
        kind="geojson",
        raw_keys=[str(k) for k in data],
        feature_count=len(features),
        geometry_types=dict(geom_counter),
        element_types={},
        property_columns=columns,
        property_sample=prop_sample,
        property_dtypes=dtypes,
        top_tag_keys=[],
        amenity_counts={},
        sample_element={},
        extra={
            "crs": data.get("crs"),
            "type": data.get("type"),
            "boro_counts": dict(boro_counter),
        },
    )


def _overpass_profile(path: Path, data: dict[str, Any], size: int) -> JsonProfile:
    elements = data.get("elements") or []
    type_counter: Counter[str] = Counter()
    tag_key_counter: Counter[str] = Counter()
    amenity_counter: Counter[str] = Counter()
    with_tags = 0
    sample_element: dict[str, Any] = {}
    for el in elements:
        etype = el.get("type")
        if etype:
            type_counter[etype] += 1
        tags = el.get("tags") or {}
        if tags:
            with_tags += 1
            tag_key_counter.update(tags.keys())
            amenity = tags.get("amenity")
            if isinstance(amenity, str):
                amenity_counter[amenity] += 1
        if not sample_element and tags:
            sample_element = el

    return JsonProfile(
        path=path,
        size_bytes=size,
        kind="overpass",
        raw_keys=[str(k) for k in data],
        feature_count=0,
        geometry_types={},
        element_types=dict(type_counter),
        property_columns=[],
        property_sample={},
        property_dtypes={},
        top_tag_keys=tag_key_counter.most_common(TOP_N_VALUES),
        amenity_counts=dict(amenity_counter.most_common(TOP_N_VALUES)),
        sample_element=sample_element,
        extra={
            "version": data.get("version"),
            "generator": data.get("generator"),
            "osm3s": data.get("osm3s"),
            "elements_total": len(elements),
            "elements_with_tags": with_tags,
        },
    )


def _generic_object_profile(path: Path, data: dict[str, Any], size: int) -> JsonProfile:
    return JsonProfile(
        path=path,
        size_bytes=size,
        kind="object",
        raw_keys=[str(k) for k in data],
        feature_count=0,
        geometry_types={},
        element_types={},
        property_columns=[],
        property_sample={},
        property_dtypes={},
        top_tag_keys=[],
        amenity_counts={},
        sample_element={},
        extra={"top_level_summary": _summarize_value(data)},
    )


def _summarize_value(value: Any, depth: int = 0) -> str:
    """Short structural description of an arbitrary JSON value."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        keys = ", ".join(str(k) for k in list(value)[:8])
        more = "" if len(value) <= 8 else f", ... (+{len(value) - 8})"
        return f"object with {len(value)} keys: {keys}{more}"
    if isinstance(value, list):
        return f"array of {len(value)} items"
    if isinstance(value, str):
        return f"string ({len(value)} chars)"
    if value is None:
        return "null"
    return f"{type(value).__name__}: {value!r}"


def analyze_json(path: Path) -> JsonProfile:
    """Profile a single JSON file, detecting GeoJSON vs Overpass structure."""
    size = path.stat().st_size
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return _generic_object_profile(path, {"_root": data}, size)
    if data.get("type") == "FeatureCollection" or isinstance(data.get("features"), list):
        return _geojson_profile(path, data, size)
    if isinstance(data.get("elements"), list):
        return _overpass_profile(path, data, size)
    return _generic_object_profile(path, data, size)


def render_json_markdown(
    profile: JsonProfile,
    *,
    chart_dir: Path | None = None,
    rel_chart_dir: str = "",
) -> str:
    """Render a :class:`JsonProfile` as a Markdown report."""
    p = profile
    lines: list[str] = []
    lines.append(f"# EDA: `{p.path.name}`")
    lines.append("")
    lines.append(f"> Source: `{p.path}`")
    lines.append("")

    def _img(name: str, alt: str, caption: str = "") -> str:
        rel = f"{rel_chart_dir}/{name}" if rel_chart_dir else name
        block = f"![{alt}]({rel})"
        if caption:
            block += f"\n\n_{caption}_"
        return block

    lines.append("## File overview")
    lines.append("")
    overview = [
        ["File size", _human_size(p.size_bytes)],
        ["Format", "JSON"],
        ["Detected kind", p.kind],
        ["Top-level keys", ", ".join(p.raw_keys) or "(none)"],
    ]
    if p.kind == "geojson":
        overview.extend(
            [
                ["Features", f"{p.feature_count:,}"],
                ["Geometry types", _fmt_counter(p.geometry_types)],
                ["Property columns", f"{len(p.property_columns):,}"],
            ]
        )
    elif p.kind == "overpass":
        overview.extend(
            [
                ["Elements", f"{p.extra.get('elements_total', 0):,}"],
                ["Elements with tags", f"{p.extra.get('elements_with_tags', 0):,}"],
                ["Element types", _fmt_counter(p.element_types)],
                ["Generator", str(p.extra.get("generator", ""))],
            ]
        )
    lines.append(_md_table(["Attribute", "Value"], overview))
    lines.append("")

    # --- Charts: structural summary --------------------------------------- #
    if chart_dir is not None and p.kind in {"geojson", "overpass"}:
        from nyc_apartments_map.eda.charts import (
            plot_geojson_summary,
            plot_overpass_summary,
        )

        summary_name = "summary.png"
        summary_path = chart_dir / summary_name
        wrote = False
        if p.kind == "geojson":
            wrote = plot_geojson_summary(p, summary_path)
        else:
            wrote = plot_overpass_summary(p, summary_path)
        if wrote:
            lines.append("## Summary chart")
            lines.append("")
            lines.append(_img(summary_name, "Structural summary chart"))
            lines.append("")

    if p.kind == "geojson":
        if p.property_columns:
            lines.append("## Feature property schema")
            lines.append("")
            schema_rows: list[list[str]] = []
            for col in p.property_columns:
                schema_rows.append([col, p.property_dtypes.get(col, "unknown")])
            lines.append(_md_table(["property", "inferred type"], schema_rows))
            lines.append("")

            lines.append("## Sample feature properties")
            lines.append("")
            if p.property_sample:
                sample_rows = [[k, _fmt(v)] for k, v in p.property_sample.items()]
                lines.append(_md_table(["property", "sample value"], sample_rows))
            else:
                lines.append("_No non-empty properties found._")
            lines.append("")

        crs = p.extra.get("crs")
        if crs:
            lines.append("## Coordinate reference system")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(crs, indent=2))
            lines.append("```")
            lines.append("")

    elif p.kind == "overpass":
        lines.append("## Top tag keys")
        lines.append("")
        if p.top_tag_keys:
            tag_rows = [[k, f"{v:,}"] for k, v in p.top_tag_keys]
            lines.append(_md_table(["tag key", "count"], tag_rows))
        else:
            lines.append("_No tags found._")
        lines.append("")

        lines.append("## Amenity distribution (top)")
        lines.append("")
        if p.amenity_counts:
            amenity_rows = [[k, f"{v:,}"] for k, v in p.amenity_counts.items()]
            lines.append(_md_table(["amenity", "count"], amenity_rows))
        else:
            lines.append("_No `amenity` tags found._")
        lines.append("")

        lines.append("## Sample element")
        lines.append("")
        if p.sample_element:
            lines.append("```json")
            sample_str = json.dumps(p.sample_element, indent=2)
            if len(sample_str) > 2000:
                sample_str = sample_str[:2000] + "\n... (truncated)"
            lines.append(sample_str)
            lines.append("```")
        else:
            lines.append("_No tagged elements found._")
        lines.append("")

        osm3s = p.extra.get("osm3s")
        if osm3s:
            lines.append("## OSM metadata")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(osm3s, indent=2))
            lines.append("```")
            lines.append("")

    else:  # generic object
        summary = p.extra.get("top_level_summary", "")
        lines.append("## Structure")
        lines.append("")
        lines.append(f"_{summary}_")
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _iter_data_files(raw_dir: Path) -> list[Path]:
    """Yield all ``.csv``/``.json`` files under ``raw_dir`` (recursive)."""
    files = [
        p
        for p in sorted(raw_dir.rglob("*"))
        if p.is_file() and p.suffix.lower() in {".csv", ".json"}
    ]
    return files


def run_eda(
    *,
    raw_dir: Path | None = None,
    out_dir: Path | None = None,
    settings: Settings | None = None,
) -> list[Path]:
    """Profile every CSV/JSON file under ``raw_dir`` and write Markdown reports.

    Args:
        raw_dir: Directory to scan. Defaults to ``settings.raw_dir``.
        out_dir: Directory to write reports into. Defaults to
            ``settings.data_dir / "output" / "eda"``.
        settings: Project settings (a fresh instance is created if omitted).

    Returns:
        The list of report paths written, in scan order.
    """
    settings = settings or Settings()
    raw_dir = raw_dir or settings.raw_dir
    out_dir = out_dir or (settings.data_dir / "output" / "eda")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Charts live in a sibling ``charts/<name>/`` dir so reports embed them via
    # a stable relative path. Clear once per run to drop stale datasets.
    charts_root = out_dir / "charts"
    if charts_root.exists():
        shutil.rmtree(charts_root)
    charts_root.mkdir(parents=True, exist_ok=True)

    files = _iter_data_files(raw_dir)
    if not files:
        logger.warning("No CSV/JSON files found under %s", raw_dir)
        return []

    logger.info("Profiling %d file(s) under %s", len(files), raw_dir)
    written: list[Path] = []
    index_rows: list[list[str]] = []
    used_names: set[str] = set()

    for path in files:
        stem = path.stem
        name = stem
        n = 1
        while name in used_names:
            n += 1
            name = f"{stem}_{n}"
        used_names.add(name)
        report_path = out_dir / f"{name}.md"
        chart_dir = charts_root / name
        rel_chart_dir = f"charts/{name}"
        try:
            if path.suffix.lower() == ".csv":
                csv_profile = analyze_csv(path)
                text = render_csv_markdown(
                    csv_profile, chart_dir=chart_dir, rel_chart_dir=rel_chart_dir
                )
                index_rows.append(
                    [
                        path.relative_to(settings.project_root).as_posix(),
                        "CSV",
                        f"{csv_profile.total_rows:,}",
                        f"{len(csv_profile.columns):,}",
                        _human_size(csv_profile.size_bytes),
                        "sampled" if csv_profile.is_large else "full",
                    ]
                )
            else:
                json_profile = analyze_json(path)
                text = render_json_markdown(
                    json_profile, chart_dir=chart_dir, rel_chart_dir=rel_chart_dir
                )
                if json_profile.kind == "geojson":
                    count = json_profile.feature_count
                else:
                    count = json_profile.extra.get("elements_total", 0)
                index_rows.append(
                    [
                        path.relative_to(settings.project_root).as_posix(),
                        f"JSON ({json_profile.kind})",
                        f"{count:,}",
                        "—",
                        _human_size(json_profile.size_bytes),
                        "full",
                    ]
                )
        except Exception:  # noqa: BLE001 - one bad file shouldn't abort the EDA
            logger.exception("Failed to profile %s", path)
            text = f"# EDA: `{path.name}`\n\n_Error profiling file: see logs._\n"
            index_rows.append(
                [
                    path.relative_to(settings.project_root).as_posix(),
                    path.suffix.lstrip(".").upper(),
                    "—",
                    "—",
                    _human_size(path.stat().st_size),
                    "failed",
                ]
            )
        report_path.write_text(text, encoding="utf-8")
        written.append(report_path)
        logger.info("wrote %s", report_path)

    # Summary index.
    index = ["# EDA index", "", f"Generated from `{raw_dir}`.", ""]
    index.append(_md_table(["file", "format", "rows", "columns", "size", "mode"], index_rows))
    index.append("")
    index.append(f"_{len(written)} report(s) written to `{out_dir}`._")
    index_path = out_dir / "index.md"
    index_path.write_text("\n".join(index), encoding="utf-8")
    written.append(index_path)
    logger.info("wrote index %s", index_path)
    return written
