#!/usr/bin/env -S uv run python
"""Serve the generated map over HTTP so OSM tiles load correctly.

Usage:
    uv run scripts/serve_map.py
    uv run scripts/serve_map.py -p 8080
"""

from nyc_apartments_map.cli import app

if __name__ == "__main__":
    app(["serve", *__import__("sys").argv[1:]])
