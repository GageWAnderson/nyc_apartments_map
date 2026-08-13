#!/usr/bin/env -S uv run python
"""Serve the map + scoring API over HTTP so the UI can re-score NTAs live.

Usage:
    uv run scripts/serve_api.py
    uv run scripts/serve_api.py -p 8080
"""

from nyc_apartments_map.cli import app

if __name__ == "__main__":
    app(["api-serve", *__import__("sys").argv[1:]])
