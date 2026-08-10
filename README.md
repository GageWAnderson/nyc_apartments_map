# nyc_apartments_map

Modular pipeline that fetches NYC apartment datasets, normalizes them onto a
common schema, and renders an interactive **Leaflet** map (via Folium) as a
self-contained HTML file.

Pure Python scripts (no notebooks). Uses [`uv`](https://docs.astral.sh/uv/)
for dependency management and targets Python 3.12.

## Quick start

```bash
uv sync --all-extras              # install runtime + dev deps
uv run nyc-apartments-map run     # fetch -> process -> build map (one command)
uv run nyc-apartments-map serve   # serve over HTTP (see "Tiles not loading?" below)
# then open http://localhost:8000/nyc_apartments.html
```

## CLI

```
nyc-apartments-map list-datasets                       # show discovered datasets
nyc-apartments-map fetch [-n NAME] [-f]                # download + cache raw data
nyc-apartments-map process [-n NAME]                   # clean + merge -> parquet
nyc-apartments-map build-map [-o PATH]                 # render Leaflet HTML
nyc-apartments-map run [-n NAME] [-o PATH] [-f]        # full pipeline
nyc-apartments-map serve [-p PORT] [-d DIR]            # serve the map over HTTP
```

`-n/--name` is repeatable and selects specific datasets; omit it to use all
discovered loaders. `-f/--force` re-downloads even when cached.

## Tiles not loading? (OpenStreetMap Referer policy)

OpenStreetMap's volunteer-run tile servers block requests with no `Referer`
header. The builder already complies by emitting a `<meta name="referrer">`
tag and setting `referrerPolicy` on the Leaflet tile layer — but **opening the
map via `file://` can still fail** because browsers omit the Referer for opaque
`file://` origins. Serve the map over HTTP instead:

```bash
uv run nyc-apartments-map serve                  # http://localhost:8000/nyc_apartments.html
uv run nyc-apartments-map serve -p 8080          # custom port
```


## Adding a dataset (modular)

Datasets are self-contained modules — no central registry to edit.

1. Copy `src/nyc_apartments_map/datasets/_template.py` to a new file in the
   `datasets/` package (e.g. `datasets/streeteasy.py`).
2. Set the `name`, `description`, and `source_urls` class attributes.
3. Implement `fetch` (download into `self.cache_dir`, honor `force`),
   `load` (read cached raw data), and `clean` (map source columns onto
   `COMMON_SCHEMA`).
4. Run `uv run nyc-apartments-map list-datasets` — it appears automatically.

The loader is discovered via `pkgutil` package walk; any subclass of
`DatasetLoader` with a non-empty `name` is registered.

### Common schema

Every loader's `clean()` must produce these columns:

| column        | type    | notes                              |
| ------------- | ------- | ---------------------------------- |
| `listing_id`  | str     | unique per source                  |
| `latitude`    | float   | required (rows missing coords dropped) |
| `longitude`   | float   | required                           |
| `price`       | float   | monthly rent or sale price         |
| `bedrooms`    | float   | nullable                           |
| `bathrooms`   | float   | nullable                           |
| `neighborhood`| str     | nullable                           |
| `borough`     | str     | nullable                           |
| `source`      | str     | set automatically to `loader.name` |
| `raw`         | dict    | extra source-specific fields       |

## Project layout

```
src/nyc_apartments_map/
  config.py            # paths + env (pydantic-settings)
  cli.py               # typer CLI
  datasets/
    base.py            # DatasetLoader ABC + COMMON_SCHEMA
    registry.py        # auto-discovery of loader subclasses
    _template.py       # copy-and-go template (not registered)
    sample_nyc_listings.py  # runnable synthetic dataset
  processing/normalize.py   # clean + merge -> parquet
  geo/                 # NYC bounds + CRS
  map/builder.py       # Folium/Leaflet map builder
scripts/               # thin CLI wrappers (fetch_data, process_data, build_map)
tests/                 # registry + normalize tests
data/      raw/ interim/ processed/   # gitignored, regenerated
outputs/   maps/                       # gitignored, regenerated
```

## Caching

- Raw downloads live in `data/raw/<dataset_name>/`. `fetch` skips re-downloading
  when files exist unless `--force` is passed (existence-only validation).
- The merged normalized table is written to `data/processed/normalized.parquet`
  (pyarrow), so `build-map` can re-render without re-cleaning.

## Quality checks

```bash
uv run ruff check src tests scripts
uv run mypy src
uv run pytest -q
```

## Git hooks

[`pre-commit`](https://pre-commit.com) runs lint, formatting, type-checking, and
conventional-commit enforcement on every commit.

```bash
uv sync --all-extras                    # ensures pre-commit & tools are installed
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

Covers: generic file hygiene, `ruff check --fix` + `ruff format`, `mypy`, and a
[`commitizen`](https://commitizen-tools.github.io/commitizen/) hook that rejects
commit messages not following conventional commits (e.g. `feat:`, `fix:`) via the
`commit-msg` stage. To check all files without committing:

```bash
uv run pre-commit run --all-files
```
