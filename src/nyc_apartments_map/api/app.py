"""FastAPI app factory: mounts the scores API and the static map files.

Mounting the map HTML and the API under one origin avoids CORS: the page at
``/nyc_apartments.html`` calls ``/api/scores`` same-origin. ``StaticFiles`` is
mounted at ``/`` so it acts as a catch-all for the built map (after the
``/api`` routes registered by :func:`include_router`).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from nyc_apartments_map.api.routes.scores import router as scores_router
from nyc_apartments_map.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app: scores router at ``/api`` + static map at ``/``.

    Args:
        settings: Optional Settings (defaults to :func:`get_settings`). Pass
            explicitly in tests; the server uses the default.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()

    app = FastAPI(title="NYC Apartments Map API", version="0.1.0")
    app.include_router(scores_router)

    # Static catch-all: serves the built map (nyc_apartments.html) and any
    # other artifacts under outputs/maps. Mounted last so /api/* wins.
    app.mount(
        "/",
        StaticFiles(directory=str(settings.maps_dir), html=True),
        name="static",
    )
    return app
