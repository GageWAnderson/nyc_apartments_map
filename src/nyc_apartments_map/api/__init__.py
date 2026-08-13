"""FastAPI backend for the NYC apartments map.

Exposes the scoring engine as a JSON API so the map UI can re-score NTAs
under caller-supplied composite weights without re-running the pipeline.
The map HTML is served as static files from the same origin (no CORS).

Run with ``nyc-apartments-map api-serve`` (see :mod:`nyc_apartments_map.cli`).
"""
