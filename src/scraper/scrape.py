"""Call the Apify StreetEasy scraper (memo23/streeteasy-ppr) and save listings.

Standalone PoC (same pattern as ``src/apartment_scorer/``): argparse CLI, no
dependency on the data pipeline. Reads ``APIFY_TOKEN`` from the repo ``.env``
(real environment variables win), starts actor ``ptsXZUXADV3OKZ5kd``, waits
for the run, and writes the dataset to
``data/listings/<label>-<run_id>/listings.json`` — the same shape the batch
mode of ``src/apartment_scorer/score.py`` consumes.

The embedded default input is the known-good console run ``XlM3kuadfmTmPaatE``
(West Side rentals, residential NY proxy, 100 items). Override pieces with
``--url`` / ``--max-items`` or replace it wholesale with ``--input-json``.

Run: ``uv run python src/streeteasy_scraper/scrape.py [--label west-side]
       [--url https://streeteasy.com/for-rent/nyc/area:107] [--max-items 50]
       [--input-json input.json] [--no-wait] [--from-run RUN_ID]``
Exit codes: 0 = listings written, 2 = bad input/config, 1 = actor run failed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apify_client import ApifyClient
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# src/streeteasy_scraper/scrape.py is two directories below the repo root:
#   parents[0]=streeteasy_scraper/, parents[1]=src/, parents[2]=<repo root>
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUT_DIR = _REPO_ROOT / "data" / "listings"

# memo23/streeteasy-ppr — https://console.apify.com/actors/ptsXZUXADV3OKZ5kd
ACTOR_ID = "ptsXZUXADV3OKZ5kd"

# Input captured from the known-good console run XlM3kuadfmTmPaatE (2026-09-08).
DEFAULT_INPUT: dict[str, Any] = {
    "dbCountOnly": False,
    "dbHasEmail": False,
    "dbHasPhone": False,
    "dbInRentals": False,
    "dbInSales": False,
    "dbInstant": False,
    "dbLicensedOnly": False,
    "dbProOnly": False,
    "enrichEmails": False,
    "flattenDatasetItems": True,
    "maxItems": 100,
    "monitoringMode": False,
    "moreResults": False,
    "proxy": {
        "useApifyProxy": True,
        "apifyProxyGroups": ["RESIDENTIAL"],
        "apifyProxyCountry": "US",
        "apifyProxySubdivision": "NY",
    },
    "startUrls": [
        {"url": "https://streeteasy.com/for-rent/nyc/area:107,113,115,116,124,157,158,162"}
    ],
    "maxConcurrency": 10,
    "minConcurrency": 1,
    "maxRequestRetries": 100,
}


def build_input(args: argparse.Namespace) -> dict[str, Any]:
    """Assemble the actor input: default, ``--input-json`` file, or a mix."""
    if args.input_json is not None:
        try:
            loaded = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"error: cannot read --input-json {args.input_json}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise SystemExit("error: --input-json must contain a JSON object")
        run_input = loaded
    else:
        run_input = deepcopy(DEFAULT_INPUT)

    if args.url:
        run_input["startUrls"] = [{"url": u} for u in args.url]
    if "startUrls" not in run_input:
        raise SystemExit("error: input has no startUrls (pass --url or a complete --input-json)")
    if args.max_items is not None:
        run_input["maxItems"] = args.max_items
    return run_input


def get_client() -> ApifyClient:
    """Build a client from APIFY_TOKEN (repo .env loaded; real env wins)."""
    load_dotenv(_REPO_ROOT / ".env")
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise SystemExit("error: APIFY_TOKEN not set (add it to .env or the environment)")
    return ApifyClient(token)


def fetch_items(client: ApifyClient, dataset_id: str) -> list[dict[str, Any]]:
    """Read every item from a dataset."""
    return list(client.dataset(dataset_id).iterate_items())


def write_items(items: list[dict[str, Any]], label: str, run_id: str, out_dir: Path) -> Path:
    """Write items to ``<out_dir>/<label>-<run_id>/listings.json``."""
    folder = out_dir / f"{label}-{run_id}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "listings.json"
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Define and parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Run the Apify StreetEasy scraper and save listings to data/listings/.",
    )
    parser.add_argument(
        "--label",
        default=f"streeteasy-{datetime.now():%m-%d-%Y}",
        help="output folder label (default: streeteasy-<MM-DD-YYYY>); "
        "the run id is appended: data/listings/<label>-<run_id>/listings.json",
    )
    parser.add_argument(
        "--url",
        action="append",
        help="StreetEasy search URL to scrape; repeatable, replaces the default startUrls",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="cap results (default: 100 from the embedded input)",
    )
    parser.add_argument(
        "--input-json",
        metavar="PATH",
        help="full actor input JSON file, replacing the embedded default "
        "(--url/--max-items still override it)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="seconds to wait for the run (default: 3600)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="start the run, print its id/dataset id, and exit immediately",
    )
    parser.add_argument(
        "--from-run",
        metavar="RUN_ID",
        help="skip starting a run; download the default dataset of an existing run",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT_DIR,
        help="output root (default: data/listings/)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    client = get_client()

    if args.from_run:
        run = client.run(args.from_run).get()
        if run is None:
            logger.error("run %s not found", args.from_run)
            return 2
        items = fetch_items(client, run.default_dataset_id)
        path = write_items(items, args.label, run.id, args.out)
        logger.info("wrote %d listings -> %s", len(items), path)
        return 0

    run_input = build_input(args)
    actor = client.actor(ACTOR_ID)

    if args.no_wait:
        run = actor.start(run_input=run_input)
        logger.info("started run %s (dataset %s)", run.id, run.default_dataset_id)
        logger.info("https://console.apify.com/actors/runs/%s", run.id)
        logger.info("fetch later with: --from-run %s", run.id)
        return 0

    run = actor.call(run_input=run_input, wait_duration=timedelta(seconds=args.timeout))
    if run is None:
        logger.error(
            "run did not finish within --timeout %ds; fetch later with --from-run", args.timeout
        )
        return 1
    if run.status != "SUCCEEDED":
        logger.error("run %s finished with status %s", run.id, run.status)
        logger.error("https://console.apify.com/actors/runs/%s", run.id)
        return 1

    items = fetch_items(client, run.default_dataset_id)
    path = write_items(items, args.label, run.id, args.out)
    logger.info("wrote %d listings -> %s", len(items), path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
