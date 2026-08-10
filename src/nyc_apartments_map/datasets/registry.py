"""Auto-discovery registry for :class:`DatasetLoader` subclasses.

Walks this package's modules, imports them, and collects every concrete
subclass of :class:`DatasetLoader`. To add a dataset you only need to drop a
new module containing a subclass into this package — no edits here.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Iterator

from nyc_apartments_map.datasets.base import DatasetLoader

logger = logging.getLogger(__name__)


def discover_loaders() -> dict[str, type[DatasetLoader]]:
    """Return ``{name: loader_class}`` for all discovered concrete loaders.

    Names are derived from the loader's ``name`` class attribute. Modules whose
    import raises are skipped with a warning so one broken dataset never breaks
    the whole registry.
    """
    found: dict[str, type[DatasetLoader]] = {}
    package = importlib.import_module("nyc_apartments_map.datasets")
    for module_info in pkgutil.iter_modules(package.__path__):
        modname = module_info.name
        if modname in {"base", "registry"}:
            continue
        full = f"nyc_apartments_map.datasets.{modname}"
        try:
            module = importlib.import_module(full)
        except Exception:  # noqa: BLE001 - one broken module shouldn't kill discovery
            logger.warning("Failed to import dataset module %s", full, exc_info=True)
            continue
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if not isinstance(attr, type):
                continue
            if not issubclass(attr, DatasetLoader) or attr is DatasetLoader:
                continue
            loader_name: str = attr.name
            if loader_name:
                found[loader_name] = attr
    return found


def get_loader_class(name: str) -> type[DatasetLoader]:
    """Return the loader class for ``name`` or raise ``KeyError``."""
    loaders = discover_loaders()
    if name not in loaders:
        available = ", ".join(sorted(loaders)) or "(none)"
        raise KeyError(f"No dataset loader named {name!r}. Available: {available}")
    return loaders[name]


def iter_loader_classes() -> Iterator[type[DatasetLoader]]:
    """Iterate over all discovered loader classes."""
    yield from discover_loaders().values()
