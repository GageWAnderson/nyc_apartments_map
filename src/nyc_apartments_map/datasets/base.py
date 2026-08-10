"""Base class for modular dataset loaders.

To add a new dataset, create a new module in this package (e.g.
``datasets/streeteasy.py``) and subclass :class:`DatasetLoader`. The registry
in :mod:`nyc_apartments_map.datasets.registry` discovers subclasses
automatically — no registration list to maintain.

Implement :meth:`fetch`, :meth:`load`, and :meth:`clean`. ``clean`` must return
a DataFrame whose columns conform to the :data:`COMMON_SCHEMA`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

import pandas as pd

from nyc_apartments_map.config import Settings

logger = logging.getLogger(__name__)

#: Canonical column schema every loader must produce from ``clean()``.
#: ``raw`` holds any source-specific fields not captured by the common columns.
COMMON_SCHEMA: dict[str, str] = {
    "listing_id": "str",
    "latitude": "float64",
    "longitude": "float64",
    "price": "float64",
    "bedrooms": "float64",
    "bathrooms": "float64",
    "neighborhood": "str",
    "borough": "str",
    "source": "str",
    "raw": "object",  # dict of extra source-specific fields
}


class DatasetLoader(ABC):
    """Abstract base for a single dataset source.

    Subclasses set the ``name``, ``description``, and ``source_urls`` class
    attributes and implement the three pipeline methods. The registry
    instantiates loaders lazily; the base constructor only stashes settings.
    """

    #: Short unique slug used for cache dirs, CLI selection, and the ``source`` column.
    name: ClassVar[str] = ""
    #: Human-readable description shown by ``list-datasets``.
    description: ClassVar[str] = ""
    #: URLs fetched by the default :meth:`fetch` implementation (empty = custom fetch).
    source_urls: ClassVar[list[str]] = []

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    @property
    def cache_dir(self) -> Path:
        """Per-loader raw cache directory: ``data/raw/<name>/``."""
        path = self.settings.raw_dir / self.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @abstractmethod
    def fetch(self, *, force: bool = False) -> Path:
        """Download raw data into :attr:`cache_dir`.

        If cached files already exist and ``force`` is false, return the cache
        dir without re-downloading (existence-only cache validation).

        Returns the cache directory path.
        """

    @abstractmethod
    def load(self) -> pd.DataFrame:
        """Read cached raw data into an unprocessed DataFrame."""

    @abstractmethod
    def clean(self) -> pd.DataFrame:
        """Map source-specific columns onto :data:`COMMON_SCHEMA`.

        The returned DataFrame must contain every key in :data:`COMMON_SCHEMA`
        with a compatible dtype. Missing values should be ``NaN``/``None``.
        The ``source`` column is set to ``self.name`` automatically by
        :func:`nyc_apartments_map.processing.normalize.validate_schema`.
        """

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
