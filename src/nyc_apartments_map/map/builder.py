"""Map rendering: build an interactive Leaflet map via Folium."""

from __future__ import annotations

import logging
from pathlib import Path

import branca.colormap as cm
import folium
import pandas as pd

from nyc_apartments_map.config import Settings
from nyc_apartments_map.geo import NYC_CENTER
from nyc_apartments_map.processing.normalize import load_normalized

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


def _price_color_scale(min_price: float, max_price: float) -> cm.LinearColormap:
    """Build a green->yellow->red linear colormap over the price range."""
    if max_price <= min_price:
        max_price = min_price + 1.0
    scale = cm.linear.YlOrRd_09.scale(min_price, max_price)  # type: ignore[attr-defined]
    return scale  # type: ignore[no-any-return]


def _marker_radius(price: float, p_min: float, p_max: float) -> float:
    """Scale marker radius by price, clamped to a readable range."""
    if p_max <= p_min:
        return 5.0
    frac = (price - p_min) / (p_max - p_min)
    return 4.0 + frac * 8.0  # 4..12 px


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
