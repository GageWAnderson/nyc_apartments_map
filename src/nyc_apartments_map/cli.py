"""Command-line interface for the NYC apartments map pipeline."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from nyc_apartments_map.config import Settings
from nyc_apartments_map.datasets.registry import discover_loaders
from nyc_apartments_map.eda.core import run_eda
from nyc_apartments_map.map.builder import build_map
from nyc_apartments_map.processing.normalize import normalize

app = typer.Typer(
    name="nyc-apartments-map",
    help="Fetch, normalize, and map NYC apartment datasets to an interactive Leaflet map.",
    no_args_is_help=True,
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _select_names(names: list[str] | None) -> list[str] | None:
    return names or None


@app.command("list-datasets")
def list_datasets(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """List all auto-discovered dataset loaders."""
    _setup_logging(verbose)
    loaders = discover_loaders()
    if not loaders:
        typer.echo("No dataset loaders discovered.")
        raise typer.Exit(0)
    typer.echo(f"Discovered {len(loaders)} dataset(s):")
    for name in sorted(loaders):
        cls = loaders[name]
        desc = getattr(cls, "description", "") or ""
        typer.echo(f"  - {name}: {desc}")


@app.command("fetch")
def fetch(
    names: Annotated[
        list[str] | None,
        typer.Option("--name", "-n", help="Dataset name (repeatable). Default: all."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Re-download even if cached.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Download (and cache) raw data for one or all datasets."""
    _setup_logging(verbose)
    settings = Settings()
    settings.ensure_dirs()
    selected = _select_names(names)
    loaders = discover_loaders()
    if selected:
        missing = [n for n in selected if n not in loaders]
        if missing:
            typer.echo(f"Unknown dataset(s): {missing}", err=True)
            raise typer.Exit(2)
        targets = {n: loaders[n] for n in selected}
    else:
        targets = loaders
    if not targets:
        typer.echo("No dataset loaders discovered.")
        raise typer.Exit(0)
    for name in sorted(targets):
        loader = targets[name](settings=settings)
        path = loader.fetch(force=force)
        typer.echo(f"fetched {name} -> {path}")


@app.command("eda")
def eda(
    out_dir: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Output directory. Default: data/output/eda."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Generate an exploratory-data-analysis report per raw CSV/JSON file."""
    _setup_logging(verbose)
    settings = Settings()
    paths = run_eda(out_dir=out_dir, settings=settings)
    if not paths:
        typer.echo("No CSV/JSON files found under data/raw.", err=True)
        raise typer.Exit(1)
    typer.echo(f"wrote {len(paths)} report(s) -> {paths[-1].parent}")


@app.command("process")
def process(
    names: Annotated[
        list[str] | None,
        typer.Option("--name", "-n", help="Dataset name (repeatable). Default: all."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Clean and merge datasets into a normalized parquet file."""
    _setup_logging(verbose)
    settings = Settings()
    selected = _select_names(names)
    df = normalize(names=selected, settings=settings, write=True)
    typer.echo(f"normalized {len(df)} rows -> {settings.normalized_path}")


@app.command("build-map")
def build_map_cmd(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output .html path."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Render the normalized listings into an interactive Leaflet HTML map."""
    _setup_logging(verbose)
    settings = Settings()
    try:
        path = build_map(output_path=output, settings=settings)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"map written -> {path}")


@app.command("serve")
def serve(
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port to serve on."),
    ] = 8000,
    directory: Annotated[
        Path | None,
        typer.Option("--dir", "-d", help="Directory to serve. Default: outputs/maps."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Serve the generated map over HTTP.

    Opening the map via ``file://`` can cause OpenStreetMap tile requests to
    be blocked (browsers omit the Referer header for opaque file origins).
    Serving over HTTP ensures a Referer is sent, satisfying OSM's tile policy.
    """
    import functools
    import http.server
    import socketserver

    _setup_logging(verbose)
    settings = Settings()
    serve_dir = directory or settings.maps_dir
    if not serve_dir.exists():
        typer.echo(f"Directory does not exist: {serve_dir}", err=True)
        raise typer.Exit(1)

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(serve_dir),
    )
    typer.echo(f"Serving {serve_dir} at http://localhost:{port}/")
    typer.echo("Open nyc_apartments.html in your browser. Press Ctrl+C to stop.")
    typer.echo(
        "Note: this is a static server; the map's 'Update scores' button needs "
        "the API server -- run `nyc-apartments-map api-serve` instead."
    )
    try:
        with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        typer.echo("\nStopping server.")


@app.command("api-serve")
def api_serve(
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port to serve on."),
    ] = 8000,
    host: Annotated[
        str,
        typer.Option("--host", help="Host to bind. Default: 127.0.0.1."),
    ] = "127.0.0.1",
    reload: Annotated[
        bool,
        typer.Option("--reload", help="Auto-reload on source changes (dev only)."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Serve the map + scoring API over HTTP (FastAPI + static files).

    Mounts the built map at ``/nyc_apartments.html`` and the JSON API at
    ``/api/weights`` (GET) and ``/api/scores`` (POST) on a single origin so the
    map page can re-score NTAs without CORS. Requires the ``api`` extra:
    ``uv sync --all-extras``.
    """
    import uvicorn

    _setup_logging(verbose)
    typer.echo(f"Serving map + API at http://{host}:{port}/")
    typer.echo(f"  Map: http://{host}:{port}/nyc_apartments.html")
    typer.echo(f"  API: http://{host}:{port}/api/weights  (GET)")
    typer.echo(f"       http://{host}:{port}/api/scores   (POST)")
    typer.echo("Press Ctrl+C to stop.")
    # factory=True + import string so --reload can re-import on source changes.
    uvicorn.run(
        "nyc_apartments_map.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
    )


@app.command("run")
def run(
    names: Annotated[
        list[str] | None,
        typer.Option("--name", "-n", help="Dataset name (repeatable). Default: all."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output .html path."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Re-download even if cached.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the full pipeline: fetch -> process -> build-map."""
    _setup_logging(verbose)
    settings = Settings()
    settings.ensure_dirs()
    selected = _select_names(names)
    loaders = discover_loaders()
    if selected:
        missing = [n for n in selected if n not in loaders]
        if missing:
            typer.echo(f"Unknown dataset(s): {missing}", err=True)
            raise typer.Exit(2)
        targets = {n: loaders[n] for n in selected}
    else:
        targets = loaders
    for name in sorted(targets):
        loader = targets[name](settings=settings)
        loader.fetch(force=force)
        typer.echo(f"fetched {name}")
    df = normalize(names=selected, settings=settings, write=True)
    typer.echo(f"normalized {len(df)} rows")
    path = build_map(df=df, output_path=output, settings=settings)
    typer.echo(f"map written -> {path}")


if __name__ == "__main__":
    app()
