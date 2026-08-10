#!/usr/bin/env -S uv run python
"""Clean and normalize datasets into a single parquet file.

Usage:
    uv run scripts/process_data.py
    uv run scripts/process_data.py -n sample_nyc_listings
"""

from nyc_apartments_map.cli import app

if __name__ == "__main__":
    app(["process", *__import__("sys").argv[1:]])
