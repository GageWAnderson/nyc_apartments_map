#!/usr/bin/env -S uv run python
"""Build the interactive Leaflet HTML map from the normalized parquet.

Usage:
    uv run scripts/build_map.py
    uv run scripts/build_map.py -o outputs/maps/custom.html
"""

from nyc_apartments_map.cli import app

if __name__ == "__main__":
    app(["build-map", *__import__("sys").argv[1:]])
