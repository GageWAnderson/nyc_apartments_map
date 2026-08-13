"""Map rendering: build an interactive Leaflet map via Folium."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import branca.colormap as cm
import folium
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from nyc_apartments_map.config import Settings
from nyc_apartments_map.geo import NYC_CENTER
from nyc_apartments_map.geo.boundaries import load_nta_boundaries
from nyc_apartments_map.processing.normalize import load_normalized
from nyc_apartments_map.processing.scoring import load_weights

logger = logging.getLogger(__name__)

#: Referrer policy sent with tile requests. OpenStreetMap's tile usage policy
#: requires a Referer header; this value is emitted both as a `<meta name="referrer">`
#: tag in the HTML head and as the Leaflet TileLayer `referrerPolicy` option so
#: requests comply even when the page is served cross-origin.
REFERRER_POLICY = "strict-origin-when-cross-origin"

#: OpenStreetMap raster tile URL (standard, free, volunteer-run layer).
OSM_TILES_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_TILES_ATTR = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
)

#: Simplification tolerance (degrees) for NTA polygons embedded in the map
#: HTML. ~0.0005 deg (~55 m) shrinks the inline GeoJSON from ~4.5 MB to
#: ~0.34 MB while preserving visible boundaries at city zoom.
_NTA_SIMPLIFY_TOL = 0.0005


def _add_osm_tilelayer(fmap: folium.Map) -> None:
    """Add the OSM base layer with a compliant referrer policy.

    We build the TileLayer explicitly (rather than passing ``tiles="OpenStreetMap"``
    to ``folium.Map``) so we can set ``referrerPolicy`` on the Leaflet layer —
    the default constructor does not expose that option. Without it, OSM's
    volunteer-run tile servers block requests that lack a Referer header.
    """
    folium.TileLayer(
        tiles=OSM_TILES_URL,
        attr=OSM_TILES_ATTR,
        name="OpenStreetMap",
        subdomains="abc",
        max_zoom=19,
        referrerPolicy=REFERRER_POLICY,
    ).add_to(fmap)


def _add_referrer_meta(fmap: folium.Map) -> None:
    """Inject a ``<meta name="referrer">`` tag into the HTML head.

    This makes the browser attach a Referer header to cross-origin tile
    requests (e.g. when the page is served over HTTP) which OSM's policy
    requires. Note: when opened via ``file://`` browsers may still omit the
    Referer because the origin is opaque — use ``nyc-apartments-map serve``
    in that case.
    """
    meta = f'<meta name="referrer" content="{REFERRER_POLICY}">'
    fmap.get_root().header.add_child(folium.Element(meta))  # type: ignore[attr-defined]


def _load_nta_indicators(settings: Settings) -> pd.DataFrame | None:
    """Read ``nta_indicators.parquet``; return ``None`` (and warn) if absent.

    Non-fatal so map builds still succeed without the indicators parquet,
    mirroring the boundary-file pattern in ``processing/enrich.py``.
    """
    if not settings.nta_indicators_path.exists():
        logger.warning(
            "NTA indicators not found at %s; skipping choropleth layers.",
            settings.nta_indicators_path,
        )
        return None
    return pq.read_table(settings.nta_indicators_path).to_pandas()  # type: ignore[no-any-return]


#: NTA boundary metadata columns carried by ``load_nta_boundaries``; excluded
#: from per-NTA indicator metric columns to avoid duplicating them when the
#: indicators parquet is merged onto the GeoJSON properties.
_NTA_META_COLS = {"nta_name", "nta_type", "cdta_code", "cdta_name"}


def _build_enriched_nta_geojson(
    settings: Settings,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Build NTA GeoJSON with indicator metrics merged into feature properties.

    Returns ``(geojson, metric_cols)``. ``geojson`` is a FeatureCollection
    whose feature ``properties`` carry ``nta_code``, ``nta_name``, and every
    numeric indicator column from ``nta_indicators.parquet`` (NaN -> ``None``).
    ``metric_cols`` is the list of numeric indicator columns (one choropleth
    layer is built per entry).

    Returns ``(None, [])`` (and warns) if the boundary file is absent. If the
    indicators parquet is absent, the GeoJSON still carries boundary metadata
    but ``metric_cols`` is empty (no choropleth layers).
    """
    if not settings.nta_boundaries_path.exists():
        logger.warning(
            "NTA boundary file not found at %s; skipping NTA overlay + choropleth layers.",
            settings.nta_boundaries_path,
        )
        return None, []

    gdf = load_nta_boundaries(settings).copy()
    metric_cols: list[str] = []
    indicators = _load_nta_indicators(settings)
    if indicators is not None:
        # Merge indicators onto boundaries; drop duplicate metadata columns
        # so the GeoJSON properties carry each field exactly once.
        dup_cols = [c for c in _NTA_META_COLS if c in indicators.columns]
        ind_no_meta = indicators.drop(columns=dup_cols)
        gdf = gdf.merge(ind_no_meta, on="nta_code", how="left")
        metric_cols = [
            c
            for c in ind_no_meta.columns
            if c != "nta_code" and pd.api.types.is_numeric_dtype(indicators[c])
        ]

    gdf.geometry = gdf.geometry.simplify(_NTA_SIMPLIFY_TOL, preserve_topology=True)
    geojson = json.loads(gdf.to_json())
    return geojson, metric_cols


def _price_color_scale(min_price: float, max_price: float) -> cm.LinearColormap:
    """Build a green->yellow->red linear colormap over the price range."""
    if max_price <= min_price:
        max_price = min_price + 1.0
    scale = cm.linear.YlOrRd_09.scale(min_price, max_price)  # type: ignore[attr-defined]
    return scale  # type: ignore[no-any-return]


#: Style applied to NTA polygons with no data for the active metric (NaN).
_NAN_STYLE: dict[str, Any] = {
    "fillColor": "#cccccc",
    "fillOpacity": 0.08,
    "color": "#888888",
    "weight": 0.5,
}


def _make_choropleth_style(metric: str, values: pd.Series) -> Any:
    """Build a bound ``style_function`` for one metric's choropleth layer.

    Count data is highly skewed (e.g. hpd_violation_count ranges 0..97463),
    so a naive linear scale would render most NTAs identically. We clamp the
    colormap domain to the 1st/99th percentile so the bulk of NTAs are
    distinguishable; values beyond the domain are clamped by
    ``LinearColormap.rgb_hex_str`` automatically. NaN/None values fall back to
    :data:`_NAN_STYLE`.

    The returned style_function uses a default argument to bind the loop
    variable (classic closure gotcha) so each metric gets its own colormap.
    """
    vals = values.dropna()
    if vals.empty:
        lo, hi = 0.0, 1.0
    else:
        lo, hi = float(np.nanpercentile(vals, 1)), float(np.nanpercentile(vals, 99))
        if hi <= lo:
            hi = lo + 1.0
    colormap = cm.linear.YlOrRd_09.scale(lo, hi)  # type: ignore[attr-defined]

    def style_fn(
        feature: dict[str, Any],
        m: str = metric,
        cmap: cm.LinearColormap = colormap,
    ) -> dict[str, Any]:
        v = feature["properties"].get(m)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return dict(_NAN_STYLE)
        return {
            "fillColor": cmap.rgb_hex_str(float(v)),
            "fillOpacity": 0.6,
            "color": "#888888",
            "weight": 0.5,
        }

    return style_fn


def _marker_radius(price: float, p_min: float, p_max: float) -> float:
    """Scale marker radius by price, clamped to a readable range."""
    if p_max <= p_min:
        return 5.0
    frac = (price - p_min) / (p_max - p_min)
    return 4.0 + frac * 8.0  # 4..12 px


#: Static JS for the composite-weight control panel. Dynamic bits
#: (the folium layer var name + default weights) are injected into a separate
#: ``window.__SCORE_CONTROL__`` blob by :func:`_add_score_control`, so this
#: script carries no f-string placeholders and its braces stay literal.
#:
#: On click, POSTs the four composite weights to ``/api/scores`` and restyles
#: the ``desirability_score`` choropleth layer in place with the returned
#: per-NTA hex colors -- no map reload, so zoom/pan/layer state is preserved.
#: NaN/missing NTAs fall back to the gray no-data style. Requires the API
#: server (``nyc-apartments-map api-serve``); under ``file://`` the fetch has
#: no same-origin backend and the button surfaces the error in the status line.
_SCORE_CONTROL_JS = """
<script>
document.addEventListener('DOMContentLoaded', function() {
  var cfg = window.__SCORE_CONTROL__;
  if (!cfg) { return; }
  var scoreLayer = window[cfg.layerName];
  if (!scoreLayer) { return; }
  var d = cfg.defaults || {};
  var panel = document.createElement('div');
  panel.style.cssText = 'position:absolute; bottom:50px; right:10px; z-index:1000;'
    + ' background:#fff; padding:10px 12px; border-radius:6px;'
    + ' box-shadow:0 1px 5px rgba(0,0,0,0.4); font:12px/1.4 sans-serif; max-width:230px;';
  function row(name, val) {
    return '<label style="display:block; margin:3px 0;">' + name
      + ' <input type="number" id="w-' + name + '" min="0" step="0.05" value="'
      + (val || 0) + '" style="width:60px; float:right;"></label>';
  }
  panel.innerHTML =
    '<div style="font-weight:600; margin-bottom:6px;">Desirability weights</div>'
    + row('affordability', d.affordability)
    + row('safety', d.safety)
    + row('quality', d.quality)
    + row('amenity', d.amenity)
    + '<button id="update-scores-btn" style="margin-top:8px; width:100%;'
    + ' padding:4px; cursor:pointer;">Update scores</button>'
    + '<div id="score-status" style="margin-top:4px; color:#666; font-size:11px;"></div>';
  document.body.appendChild(panel);

  function readWeights() {
    return {
      affordability: parseFloat(document.getElementById('w-affordability').value) || 0,
      safety: parseFloat(document.getElementById('w-safety').value) || 0,
      quality: parseFloat(document.getElementById('w-quality').value) || 0,
      amenity: parseFloat(document.getElementById('w-amenity').value) || 0
    };
  }

  document.getElementById('update-scores-btn').addEventListener('click', function() {
    var status = document.getElementById('score-status');
    status.textContent = 'Updating...';
    fetch('/api/scores', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(readWeights())
    }).then(function(r) {
      if (!r.ok) {
        return r.json().then(function(e) { throw new Error(e.detail || ('HTTP ' + r.status)); });
      }
      return r.json();
    }).then(function(data) {
      var scores = data.scores;
      var updated = 0;
      scoreLayer.eachLayer(function(l) {
        var nta = l.feature && l.feature.properties ? l.feature.properties.nta_code : null;
        var s = scores[nta];
        if (s) {
          l.feature.properties.desirability_score = s.desirability_score;
          Object.keys(s.sub_scores).forEach(function(k) {
            l.feature.properties[k] = s.sub_scores[k];
          });
          l.setStyle({fillColor: s.color, fillOpacity: 0.6, color: '#888888', weight: 0.5});
          updated++;
        } else {
          l.setStyle({fillColor: '#cccccc', fillOpacity: 0.08, color: '#888888', weight: 0.5});
        }
      });
      status.textContent = 'Updated ' + updated + ' NTAs';
    }).catch(function(err) {
      // A failed/unsupported POST (e.g. 501 from the static `serve` command,
      // or a network error under file://) means the API backend isn't up.
      var msg = err.message || '';
      var hint = (msg.indexOf('Failed to fetch') >= 0 || /HTTP \d{3}/.test(msg))
        ? ' -- run `nyc-apartments-map api-serve` to start the API.'
        : '';
      status.textContent = 'Error: ' + msg + hint;
    });
  });
});
</script>
"""


def _add_score_control(
    fmap: folium.Map, layer_name: str, default_weights: dict[str, float]
) -> None:
    """Inject the composite-weight control panel + restyle script into the map.

    Two ``folium.Element`` scripts are added: a tiny dynamic one that sets
    ``window.__SCORE_CONTROL__`` (the folium layer var name + current default
    weights from ``weights.yaml``), and the static :data:`_SCORE_CONTROL_JS`
    that reads it. Keeping the dynamic data in JSON form (via :func:`json.dumps`)
    avoids f-string brace-escaping in the JS body.
    """
    cfg = {"layerName": layer_name, "defaults": default_weights}
    init = folium.Element(f"<script>window.__SCORE_CONTROL__ = {json.dumps(cfg)};</script>")
    static = folium.Element(_SCORE_CONTROL_JS)
    fmap.get_root().html.add_child(init)  # type: ignore[attr-defined]
    fmap.get_root().html.add_child(static)  # type: ignore[attr-defined]


def build_map(
    df: pd.DataFrame | None = None,
    output_path: Path | None = None,
    settings: Settings | None = None,
) -> Path:
    """Render the normalized listings into a self-contained Leaflet HTML map.

    Args:
        df: Listings conforming to COMMON_SCHEMA. Defaults to the cached parquet.
        output_path: Destination .html path. Defaults to ``settings.default_map_path``.
        settings: Settings instance.

    Returns the path to the written HTML file.
    """
    settings = settings or Settings()
    settings.ensure_dirs()
    if df is None:
        df = load_normalized(settings)

    output_path = output_path or settings.default_map_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if df.empty:
        logger.warning("No listings to map; writing an empty map.")

    # tiles=False so we can attach the OSM layer with a compliant referrer policy.
    fmap = folium.Map(location=NYC_CENTER, zoom_start=11, tiles=False)  # type: ignore[arg-type]
    _add_osm_tilelayer(fmap)
    _add_referrer_meta(fmap)

    # NTA boundary polygons with per-NTA indicator metrics merged in. The
    # reference layer is on by default; one choropleth layer per metric is
    # added (all off by default) so the user toggles them via LayerControl.
    # Added before the marker layers so listing markers render on top.
    nta_geojson, metric_cols = _build_enriched_nta_geojson(settings)
    if nta_geojson is not None:
        # Tooltip config: hovering any NTA shows its name, code, and every
        # indicator value, regardless of which choropleth layer is active.
        # A FRESH GeoJsonTooltip instance is created per layer below — folium's
        # GeoJsonTooltip is a MacroElement that renders a top-level
        # ``<var>.bindTooltip()`` call referencing its owning layer's variable.
        # Reusing one instance across N layers makes that call reference a var
        # defined later in the HTML, throwing "undefined" and breaking all JS
        # (including the LayerControl). Each layer needs its own instance.
        tooltip_fields = ["nta_name", "nta_code", *metric_cols]
        tooltip_aliases = ["NTA", "Code", *[c for c in metric_cols]]

        def _new_tooltip() -> folium.GeoJsonTooltip:
            return folium.GeoJsonTooltip(
                fields=tooltip_fields,
                aliases=tooltip_aliases,
                localize=True,
                sticky=True,
            )

        # Reference overlay: neutral thin-border style, on by default. Reuses
        # the enriched geojson so its tooltip also surfaces indicator values.
        folium.GeoJson(
            nta_geojson,
            name="NTA boundaries",
            style_function=lambda _f: {"color": "#3388ff", "weight": 1, "fillOpacity": 0.03},
            highlight_function=lambda _f: {"weight": 2, "fillOpacity": 0.10},
            tooltip=_new_tooltip(),
            show=True,
        ).add_to(fmap)

        # Per-metric choropleth layers, all OFF by default. No colorbar
        # legends: branca colormaps share Leaflet's `.legend` control and
        # overlap when stacked, so exact values come from the tooltip instead.
        indicators = _load_nta_indicators(settings)
        desirability_layer: folium.GeoJson | None = None
        if indicators is not None:
            for metric in metric_cols:
                style_fn = _make_choropleth_style(metric, indicators[metric])
                gj = folium.GeoJson(
                    nta_geojson,
                    name=f"NTA: {metric}",
                    style_function=style_fn,
                    highlight_function=lambda _f: {"weight": 2, "fillOpacity": 0.8},
                    tooltip=_new_tooltip(),
                    show=False,
                )
                gj.add_to(fmap)
                if metric == "desirability_score":
                    desirability_layer = gj
        # Composite-weight control: restyles the desirability_score layer live
        # via POST /api/scores. Defaults pre-fill from weights.yaml so the
        # panel's initial state matches the map as built. Skipped when no
        # desirability_score layer exists (no weights.yaml / no nta_type).
        if desirability_layer is not None:
            profile = load_weights(settings)
            defaults = profile.composite if profile is not None else {}
            _add_score_control(fmap, desirability_layer.get_name(), defaults)

    p_min = float(df["price"].min()) if not df.empty else 0.0
    p_max = float(df["price"].max()) if not df.empty else 1.0
    color_scale = _price_color_scale(p_min, p_max)

    # One toggleable LayerGroup per dataset source.
    sources = sorted(df["source"].dropna().unique()) if not df.empty else []
    for source in sources:
        layer = folium.FeatureGroup(name=source, show=True)
        subset = df[df["source"] == source]
        for _, row in subset.iterrows():
            price = float(row["price"])
            popup_html = (
                f"<b>{row.get('neighborhood', '')}, {row.get('borough', '')}</b><br>"
                f"Price: ${price:,.0f}<br>"
                f"Beds: {row.get('bedrooms', 'n/a')} | <br>"
                f"Baths: {row.get('bathrooms', 'n/a')}<br>"
                f"Source: {source}<br>"
                f"neighborhood: {row.get('neighborhood', 'n/a')}<br>"
                f"NTA Code: {row.get('nta_code', 'n/a')}<br>"
                f"CDTA Code: {row.get('cdta_code', 'n/a')}<br>"
                f"ID: {row.get('listing_id', '')}"
            )
            folium.CircleMarker(
                location=(float(row["latitude"]), float(row["longitude"])),
                radius=_marker_radius(price, p_min, p_max),
                color=color_scale.rgb_hex_str(price),
                fill=True,
                fill_opacity=0.7,
                weight=1,
                popup=folium.Popup(popup_html, max_width=250),
            ).add_to(layer)
        layer.add_to(fmap)

    folium.LayerControl(collapsed=False).add_to(fmap)
    color_scale.caption = "Monthly price (USD)"
    color_scale.add_to(fmap)

    fmap.save(str(output_path))
    logger.info("Wrote map -> %s (%d listings)", output_path, len(df))
    return output_path
