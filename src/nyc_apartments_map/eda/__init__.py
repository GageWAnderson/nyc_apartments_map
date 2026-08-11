"""Exploratory data analysis (EDA) over raw NYC datasets.

Run via ``uv run nyc-apartments-map eda`` or ``uv run python -m
nyc_apartments_map.eda``. See :func:`nyc_apartments_map.eda.core.run_eda`.
"""

from nyc_apartments_map.eda.core import run_eda

__all__ = ["run_eda"]
