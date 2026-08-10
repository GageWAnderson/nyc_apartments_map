# AGENTS.md

Compact guidance for OpenCode agents. `README.md` has full docs and the
`COMMON_SCHEMA` table — this file captures only what's easy to get wrong.
There is **no CI**; `pre-commit` git hooks enforce lint/format/type/commit
message on commit. To run the full suite manually:

## Toolchain

- **`uv`** manages deps; Python **3.12** is pinned in `.python-version` (uv
  auto-selects it). `uv.lock` is committed — re-sync after dependency changes.
- Install with `uv sync --all-extras`. Plain `uv sync` omits the `dev` extra,
  so `ruff`/`pytest`/`mypy`/`pre-commit`/`commitizen` won't be installed —
  always use `--all-extras`.
- **`pre-commit` git hooks are NOT active until installed once:**
  `uv run pre-commit install --hook-type pre-commit --hook-type commit-msg`.
  Until then nothing runs on commit.

## Commands

- CLI entry is the hyphenated script `uv run nyc-apartments-map <cmd>` (or
  `uv run python -m nyc_apartments_map`). The importable package is
  `nyc_apartments_map` (underscores) — don't confuse them.
- Pipeline order is strict: `fetch` → `process` → `build-map`, or `run` for
  all three. `build-map` reads `data/processed/normalized.parquet` and raises
  `FileNotFoundError` if missing — run `process` first.
- Single test: `uv run pytest tests/test_registry.py::test_discover_loaders_finds_sample`.
- `scripts/*.py` are thin forwarders to the Typer CLI in
  `src/nyc_apartments_map/cli.py` — do **not** add logic there.

## Quality checks

```
uv run ruff check src tests scripts
uv run mypy src
uv run pytest -q
```

- **mypy is `strict=true` and scoped to `src` only** (`[tool.mypy].files`).
  `tests/` and `scripts/` are NOT type-checked by the documented command
  (ruff covers all three). Type errors in tests won't surface from `mypy src`.

## Dataset loaders (the modular extension point)

- Add a dataset by copying `datasets/_template.py` to a new module in
  `src/nyc_apartments_map/datasets/`, setting a non-empty `name`, and
  implementing `fetch` / `load` / `clean`. No registry edit needed.
- Discovery (`datasets/registry.py`) walks the package via `pkgutil` and
  registers every `DatasetLoader` subclass with a non-empty `name`. Modules
  `base` and `registry` are skipped by name. **`_template` is skipped only
  because its `name` is empty — the leading underscore does NOT exclude it.**
  A new `datasets/_foo.py` with a non-empty `name` WILL be registered.
- `clean()` must emit every `COMMON_SCHEMA` column or `validate_schema`
  raises `ValueError`. `source` is auto-set to the loader name if missing or
  all-NaN (so it can be omitted). Numeric cols are coerced; rows missing
  `latitude`/`longitude` are silently dropped.

## Config / env

- `Settings` (`config.py`) loads `.env` via pydantic-settings but defines
  **only path fields — no env-var fields**. `.env` is currently inert here;
  don't assume env vars drive behavior (`extra="ignore"`).
- All paths derive from the repo root (resolved from `__file__`), so the CLI
  works from any CWD.

## Editing the map builder

- The referrer `<meta>` tag and Leaflet `referrerPolicy` in `map/builder.py`
  exist to satisfy OpenStreetMap's tile policy. Don't remove them — tiles
  break, especially under `file://` (use `nyc-apartments-map serve` over HTTP
  to view the map).

## Generated artifacts (gitignored, regenerated — never commit)

- `data/raw/`, `data/interim/`, `data/processed/` (incl.
  `normalized.parquet`), and `outputs/maps/`.
