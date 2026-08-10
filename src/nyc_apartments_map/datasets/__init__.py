"""Dataset loaders package.

Adding a dataset: drop a module in this package containing a subclass of
:class:`nyc_apartments_map.datasets.base.DatasetLoader` with a non-empty
``name``. The registry discovers it automatically.
"""

from nyc_apartments_map.datasets.base import COMMON_SCHEMA, DatasetLoader
from nyc_apartments_map.datasets.registry import (
    discover_loaders,
    get_loader_class,
    iter_loader_classes,
)

__all__ = [
    "COMMON_SCHEMA",
    "DatasetLoader",
    "discover_loaders",
    "get_loader_class",
    "iter_loader_classes",
]
