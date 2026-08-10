"""Template dataset loader.

Copy this module to add a new dataset, then implement the three methods below.
The registry discovers it automatically — no registration needed.

Steps:
1. Rename the class and set ``name``, ``description``, ``source_urls``.
2. Implement :meth:`fetch` (download into ``self.cache_dir``, honor ``force``).
3. Implement :meth:`load` (read cached raw bytes into a DataFrame).
4. Implement :meth:`clean` (map source columns onto ``COMMON_SCHEMA``).
5. Run ``uv run nyc-apartments-map fetch --name <your_name>`` to fetch it.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pandas as pd

from nyc_apartments_map.config import Settings
from nyc_apartments_map.datasets.base import DatasetLoader


class TemplateLoader(DatasetLoader):
    """Example skeleton — not registered (empty ``name`` so registry skips it)."""

    name = ""
    description = "Template skeleton showing the loader pattern. Copy me!"
    source_urls: ClassVar[list[str]] = ["https://example.com/data.csv"]

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)

    def fetch(self, *, force: bool = False) -> Path:
        # Download each url into self.cache_dir unless cached & not force.
        # Return self.cache_dir when done.
        raise NotImplementedError("Implement downloading into self.cache_dir")

    def load(self) -> pd.DataFrame:
        # Read the cached raw files from self.cache_dir into a DataFrame.
        raise NotImplementedError("Read cached raw data into a DataFrame")

    def clean(self) -> pd.DataFrame:
        # Map source columns onto COMMON_SCHEMA keys and return the frame.
        raise NotImplementedError("Map source columns onto COMMON_SCHEMA")
