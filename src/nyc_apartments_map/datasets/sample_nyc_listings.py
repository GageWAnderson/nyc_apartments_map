"""Sample dataset loader that generates synthetic NYC apartment listings.

This is a *real*, runnable loader (registered because ``name`` is non-empty) so
the pipeline can produce a map out-of-the-box without any external data or
network access. Replace or remove it once you wire up real datasets.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import ClassVar

import pandas as pd

from nyc_apartments_map.datasets.base import DatasetLoader

# Borough centroids (approx) used to scatter synthetic listings.
_BOROUGH_CENTROIDS: dict[str, tuple[float, float]] = {
    "Manhattan": (40.7831, -73.9712),
    "Brooklyn": (40.6782, -73.9442),
    "Queens": (40.7282, -73.7949),
    "Bronx": (40.8448, -73.8648),
    "Staten Island": (40.5795, -74.1502),
}

_NEIGHBORHOODS: dict[str, list[str]] = {
    "Manhattan": ["Upper East Side", "Harlem", "East Village", "Chelsea"],
    "Brooklyn": ["Williamsburg", "Park Slope", "Bedford-Stuyvesant", "DUMBO"],
    "Queens": ["Astoria", "Long Island City", "Flushing", "Jackson Heights"],
    "Bronx": ["Mott Haven", "Fordham", "Riverdale"],
    "Staten Island": ["St. George", "Tottenville"],
}


class SampleNYCListings(DatasetLoader):
    """Synthetic NYC apartment listings generated deterministically."""

    name = "sample_nyc_listings"
    description = "Deterministic synthetic NYC listings for demo/testing (no download)."
    source_urls: ClassVar[list[str]] = []

    def fetch(self, *, force: bool = False) -> Path:
        # No remote source — data is generated in :meth:`load`. Cache dir still
        # exists so downstream code can rely on it.
        return self.cache_dir

    def load(self) -> pd.DataFrame:
        rows: list[dict[str, object]] = []
        seed_base = 0
        for borough, (lat0, lon0) in _BOROUGH_CENTROIDS.items():
            for neighborhood in _NEIGHBORHOODS[borough]:
                for i in range(8):
                    h = hashlib.sha256(f"{borough}-{neighborhood}-{i}".encode()).digest()
                    dlat = (int.from_bytes(h[0:2], "little") % 2000 - 1000) / 100_000
                    dlon = (int.from_bytes(h[2:4], "little") % 2000 - 1000) / 100_000
                    price = 1500 + (int.from_bytes(h[4:7], "little") % 6000)
                    beds = int.from_bytes(h[7:8], "little") % 4
                    baths = 1 + (int.from_bytes(h[8:9], "little") % 2)
                    seed_base += 1
                    rows.append(
                        {
                            "borough": borough,
                            "neighborhood": neighborhood,
                            "latitude": round(lat0 + dlat, 6),
                            "longitude": round(lon0 + dlon, 6),
                            "price": float(price),
                            "bedrooms": float(beds),
                            "bathrooms": float(baths),
                            "listing_id": f"sample-{seed_base:05d}",
                        }
                    )
        return pd.DataFrame(rows)

    def clean(self) -> pd.DataFrame:
        raw_df = self.load()
        df = pd.DataFrame(
            {
                "listing_id": raw_df["listing_id"].astype(str),
                "latitude": raw_df["latitude"].astype("float64"),
                "longitude": raw_df["longitude"].astype("float64"),
                "price": raw_df["price"].astype("float64"),
                "bedrooms": raw_df["bedrooms"].astype("float64"),
                "bathrooms": raw_df["bathrooms"].astype("float64"),
                "neighborhood": raw_df["neighborhood"].astype(str),
                "borough": raw_df["borough"].astype(str),
                "source": self.name,
                "raw": raw_df.apply(lambda r: r.to_dict(), axis=1),
            }
        )
        return df
