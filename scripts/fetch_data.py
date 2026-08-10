#!/usr/bin/env -S uv run python
"""Fetch raw data for all (or selected) datasets.

Usage:
    uv run scripts/fetch_data.py                  # all datasets
    uv run scripts/fetch_data.py -n sample_nyc_listings -f
"""

from nyc_apartments_map.cli import app

if __name__ == "__main__":
    app(["fetch", *__import__("sys").argv[1:]])
