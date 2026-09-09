# StreetEasy Scraper (Apify)

Calls the Apify actor [`memo23/streeteasy-ppr`](https://console.apify.com/actors/ptsXZUXADV3OKZ5kd)
(ID `ptsXZUXADV3OKZ5kd`) and saves the result dataset to
`data/listings/<label>-<run_id>/listings.json` — the exact shape the batch
mode of [`src/apartment_scorer/score.py`](../apartment_scorer/score.py)
consumes. Standalone PoC (same pattern as `src/apartment_scorer/`): argparse
CLI, no dependency on the data pipeline.

The embedded default input is the known-good console run
`XlM3kuadfmTmPaatE` (West Side rentals, residential NY proxy, 100 items).

## Setup

`APIFY_TOKEN` must be in the repo `.env` (real environment variables win).
Get the token at <https://console.apify.com/account#/integrations>.

## Usage

```bash
# Default pull (blocking; writes data/listings/streeteasy-<date>-<run_id>/listings.json)
uv run python src/streeteasy_scraper/scrape.py

# Custom search URL / cap / label
uv run python src/streeteasy_scraper/scrape.py \
  --url "https://streeteasy.com/for-rent/nyc/area:107" --max-items 50 --label chelsea

# Full custom actor input (paste from the console run's Input tab)
uv run python src/streeteasy_scraper/scrape.py --input-json input.json

# Async: start now, fetch later
uv run python src/streeteasy_scraper/scrape.py --no-wait          # prints RUN_ID
uv run python src/streeteasy_scraper/scrape.py --from-run RUN_ID  # downloads dataset
```

Exit codes: `0` = listings written, `2` = bad input/config, `1` = actor run
failed (run URL is logged).

Notes:

- `--url` is repeatable; any `--url` replaces the default `startUrls`.
- `.call()` blocks until the run finishes (`--timeout` seconds, default
  3600); `--no-wait` uses `.start()` instead and returns immediately.
- You pay the actor's per-result rate plus Apify compute, same as a console
  run. Use `--max-items` to keep test pulls cheap.
