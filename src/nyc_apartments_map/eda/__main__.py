"""Module entry point: ``uv run python -m nyc_apartments_map.eda``."""

from __future__ import annotations

import logging

from nyc_apartments_map.eda.core import run_eda


def main() -> None:
    """Generate EDA reports for every raw CSV/JSON file."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    paths = run_eda()
    print(f"wrote {len(paths)} report(s)")


if __name__ == "__main__":
    main()
