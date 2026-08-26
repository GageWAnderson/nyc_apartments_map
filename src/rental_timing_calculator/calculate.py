"""Proof-of-concept: optimal NYC lease-start date vs. seasonal rent discount.

Implements the strategy in ``src/rental_timing_calculator/README.md``:

- Baseline monthly rent ``R`` is the median ``price`` of listings in a chosen
  neighborhood (``data/processed/normalized.parquet``), optionally filtered by
  bedroom count, or an explicit ``--rent-override`` dollar figure.
- The seasonal discount δ per month (RentReboot 2026, same-apartment fixed
  effect) applies a haircut haircut multiplier to temper published deltas.
- First-year cost of starting on date ``d``:
  ``C = 12·R(1+δ) + overlap_days·R(1+δ)/30``, where overlap days are clamped
  against ``--lease-end``.
- The "ideal date" is the argmin of ``C(d)`` over daily candidate starts in
  ``--scan-start`` → ``--scan-end`` restricted to overlap ≤ ``--max-overlap-days``.
- A matplotlib (Agg) PNG shows the full cost curve with the feasible region
  shaded, the baseline (no-overlap) line, and the chosen ideal date.

Run: ``uv run python src/rental_timing_calculator/calculate.py \
        --neighborhood "Upper East Side"``
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless backend before importing pyplot

import matplotlib.pyplot as plt
import pandas as pd

from nyc_apartments_map.config import Settings

logger = logging.getLogger(__name__)

# Seasonal δ (vs year avg), RentReboot 2026 (see README.md table). Positive =
# premium, negative = discount. Keyed by month name.
SEASONAL_DELTA: dict[str, float] = {
    "Jan": -0.019,
    "Feb": -0.017,
    "Mar": -0.009,
    "Apr": 0.003,
    "May": 0.011,
    "Jun": 0.016,
    "Jul": 0.022,
    "Aug": 0.019,
    "Sep": 0.008,
    "Oct": -0.001,
    "Nov": -0.014,
    "Dec": -0.020,
}

DAYS_PER_MONTH = 30  # README formula: overlap rent = D · R_m / 30

_PLOT_STYLE = "seaborn-v0_8-whitegrid"
_OPT_COLOR = "#55A868"  # feasible shading + ideal marker (repo PALETTE green)
_BASE_COLOR = "#4C72B0"  # baseline dashed line (repo PALETTE blue)
_UNFEAS_COLOR = "#C44E52"  # unconstrained-best marker (repo PALETTE red)


# --------------------------------------------------------------------------- #
# Pure math (unit-testable)
# --------------------------------------------------------------------------- #
def month_name(d: dt.date) -> str:
    """Month name key for :data:`SEASONAL_DELTA` (e.g. 'Jan')."""
    return d.strftime("%b")


def month_delta(d: dt.date, haircut: float) -> float:
    """Seasonal δ for the month containing ``d``, scaled by ``haircut``.

    ``haircut`` is a multiplier on the published δ (README step 3: a 2–3%
    "haircut" to temper published seasonal discounts).
    """
    return haircut * SEASONAL_DELTA[month_name(d)]


def adjusted_rent(base_rent: float, delta: float) -> float:
    """Monthly rent after applying the seasonal δ: ``R · (1 + δ)``."""
    return base_rent * (1 + delta)


def overlap_days(start: dt.date, lease_end: dt.date) -> int:
    """Days of double rent between ``start`` and ``lease_end`` (≥ 0)."""
    return max(0, (lease_end - start).days)


def first_year_cost(start: dt.date, base_rent: float, haircut: float, lease_end: dt.date) -> float:
    """Effective first-year cost of starting on ``start`` (README formula).

    ``C = 12 · R_m + D · R_m / 30`` with ``R_m = R · (1 + δ)`` and
    ``D = overlap_days(start, lease_end)``.
    """
    rm = adjusted_rent(base_rent, month_delta(start, haircut))
    return 12 * rm + overlap_days(start, lease_end) * rm / DAYS_PER_MONTH


def candidate_starts(scan_start: dt.date, scan_end: dt.date) -> list[dt.date]:
    """Daily candidate start dates from ``scan_start`` to ``scan_end`` inclusive."""
    days = (scan_end - scan_start).days
    return [scan_start + dt.timedelta(n) for n in range(days + 1)]


def first_of_month(year: int, month: int) -> dt.date:
    """First day of a calendar month (for the monthly summary table)."""
    return dt.date(year, month, 1)


def monthly_starts(scan_start: dt.date, scan_end: dt.date) -> list[dt.date]:
    """First-of-month candidate starts covered by the scan range."""
    dates: list[dt.date] = []
    cur = dt.date(scan_start.year, scan_start.month, 1)
    while cur <= scan_end:
        if cur >= scan_start:
            dates.append(cur)
        # Advance to next month's 1st.
        cur = dt.date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
    return dates


@dataclass(frozen=True)
class BestResult:
    """Result of the daily scan."""

    ideal_date: dt.date  # argmin subject to overlap ≤ max_overlap
    ideal_cost: float
    unconstrained_date: dt.date  # argmin over all dates
    unconstrained_cost: float
    baseline_date: dt.date  # lease_end (no overlap) reference point
    baseline_cost: float
    overlap: int  # overlap days at ideal_date


def find_best_date(
    dates: list[dt.date],
    base_rent: float,
    haircut: float,
    lease_end: dt.date,
    max_overlap: int,
) -> BestResult:
    """Pick the daily start minimizing :func:`first_year_cost` within the overlap cap."""
    costs = [first_year_cost(d, base_rent, haircut, lease_end) for d in dates]
    overlaps = [overlap_days(d, lease_end) for d in dates]
    feasible = [(d, c) for d, c, o in zip(dates, costs, overlaps, strict=False) if o <= max_overlap]
    if not feasible:
        raise ValueError(
            f"No start date has overlap ≤ {max_overlap} days; widen the scan "
            "or raise --max-overlap-days."
        )
    ideal_date, ideal_cost = min(feasible, key=lambda dc: dc[1])
    unconstrained_date, unconstrained_cost = min(
        zip(dates, costs, strict=False), key=lambda dc: dc[1]
    )
    baseline_cost = first_year_cost(lease_end, base_rent, haircut, lease_end)
    return BestResult(
        ideal_date=ideal_date,
        ideal_cost=ideal_cost,
        unconstrained_date=unconstrained_date,
        unconstrained_cost=unconstrained_cost,
        baseline_date=lease_end,
        baseline_cost=baseline_cost,
        overlap=overlap_days(ideal_date, lease_end),
    )


# --------------------------------------------------------------------------- #
# Baseline rent resolution
# --------------------------------------------------------------------------- #
def resolve_baseline_rent(
    settings: Settings,
    neighborhood: str,
    bedrooms: float | None,
    rent_override: float | None,
) -> tuple[float, int]:
    """Resolve the baseline monthly rent R and the listing sample size used.

    Returns ``(rent, n_listings)``. ``rent_override`` wins; otherwise the
    median ``price`` of ``normalized.parquet`` rows in ``neighborhood``
    (optionally filtered to ``bedrooms``) is used.
    """
    if rent_override is not None:
        return rent_override, 0

    if not settings.normalized_path.exists():
        raise FileNotFoundError(
            f"Normalized parquet not found at {settings.normalized_path}. "
            "Run `nyc-apartments-map process` first or use --rent-override."
        )
    df = pd.read_parquet(settings.normalized_path)
    mask = df["neighborhood"] == neighborhood
    if bedrooms is not None:
        mask &= df["bedrooms"] == bedrooms
    sub = df.loc[mask, "price"].dropna()
    if sub.empty:
        choices = sorted(df["neighborhood"].dropna().unique())
        filt = f" with {bedrooms:g} bedrooms" if bedrooms is not None else ""
        raise ValueError(
            f"No listings found for neighborhood {neighborhood!r}{filt}. Available: {choices}"
        )
    return float(sub.median()), len(sub)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _money(x: float) -> str:
    return f"${x:,.0f}"


def monthly_summary_table(
    candidates: list[dt.date],
    base_rent: float,
    haircut: float,
    lease_end: dt.date,
) -> str:
    """One-line-per-month summary of δ, overlap, and cost."""
    header = f"{'Month':<10}{'δ':>8}{'overlap_d':>11}{'first-year cost':>18}"
    lines = [header, "-" * len(header)]
    for d in candidates:
        delta = month_delta(d, haircut)
        ov = overlap_days(d, lease_end)
        cost = first_year_cost(d, base_rent, haircut, lease_end)
        mname = month_name(d)
        lines.append(f"{mname:<10}{delta:>8.1%}{ov:>11d}{_money(cost):>18}")
    return "\n".join(lines)


def print_report(
    neighborhood: str,
    n_listings: int,
    base_rent: float,
    haircut: float,
    lease_end: dt.date,
    max_overlap: int,
    result: BestResult,
    months: list[dt.date],
) -> None:
    """Print the console report (ideal date + monthly table)."""
    n_label = f"n={n_listings} listings" if n_listings else "rent override"
    savings = result.baseline_cost - result.ideal_cost
    ideal_delta = month_delta(result.ideal_date, haircut)
    overlap_cash = result.overlap * (adjusted_rent(base_rent, ideal_delta) / DAYS_PER_MONTH)
    print()
    print(f"Neighborhood: {neighborhood} ({n_label})")
    print(f"Baseline R = {_money(base_rent)}/mo  (haircut={haircut:g})")
    print()
    d = result.ideal_date
    print(
        f"Ideal start: {d.isoformat()} ({month_name(d)}) — δ {ideal_delta:+.1%}, "
        f"overlap {result.overlap}d, first-year cost {_money(result.ideal_cost)}"
    )
    print(
        f"  → saves {_money(savings)} vs no-overlap {result.baseline_date.isoformat()} "
        f"baseline ({_money(result.baseline_cost)})"
    )
    print(f"  → overlap cash needed ≈ {_money(overlap_cash)}")
    if result.unconstrained_date != result.ideal_date:
        ud = result.unconstrained_date
        print(
            f"Unconstrained best: {ud.isoformat()} ({month_name(ud)}) at "
            f"{_money(result.unconstrained_cost)} — rejected: overlap "
            f"{overlap_days(ud, lease_end)}d > {max_overlap}d cap"
        )
    print()
    print(monthly_summary_table(months, base_rent, haircut, lease_end))


# --------------------------------------------------------------------------- #
# Plot
# --------------------------------------------------------------------------- #
def plot_cost_curve(
    dates: list[dt.date],
    base_rent: float,
    haircut: float,
    lease_end: dt.date,
    max_overlap: int,
    result: BestResult,
    neighborhood: str,
    out_path: Path,
) -> None:
    """Write a first-year-cost-vs-start-date PNG (Agg / headless)."""
    costs = [first_year_cost(d, base_rent, haircut, lease_end) for d in dates]
    ts = [_to_num(d) for d in dates]

    with _style():
        fig, ax = plt.subplots(figsize=(11, 5.5))
        ax.plot(ts, costs, color=_BASE_COLOR, linewidth=1.6, label="first-year cost")

        # Feasible region: overlap ≤ max_overlap (dates at/after
        # lease_end - max_overlap … lease_end), or anything after lease_end.
        feasible_from = lease_end - dt.timedelta(max_overlap)
        ax.axvspan(
            _to_num(feasible_from),
            _to_num(dates[-1]),
            color=_OPT_COLOR,
            alpha=0.10,
            label=f"feasible (overlap ≤ {max_overlap}d)",
        )
        ax.axvline(
            _to_num(lease_end),
            color=_BASE_COLOR,
            linestyle="--",
            linewidth=1.0,
            label="lease end (no-overlap baseline)",
        )
        ax.axhline(
            result.baseline_cost,
            color=_BASE_COLOR,
            linestyle=":",
            linewidth=1.0,
        )
        ax.scatter(
            [_to_num(result.ideal_date)],
            [result.ideal_cost],
            color=_OPT_COLOR,
            s=70,
            zorder=5,
            label=f"ideal {result.ideal_date.isoformat()}",
        )
        if result.unconstrained_date != result.ideal_date:
            ax.scatter(
                [_to_num(result.unconstrained_date)],
                [result.unconstrained_cost],
                color=_UNFEAS_COLOR,
                s=50,
                zorder=5,
                label=f"unconstrained {result.unconstrained_date.isoformat()}",
            )
        savings = result.baseline_cost - result.ideal_cost
        if savings > 0:
            ax.annotate(
                f"saves {savings:,.0f} vs baseline",
                xy=(_to_num(result.ideal_date), result.ideal_cost),
                xytext=(12, -30),
                textcoords="offset points",
                fontsize=9,
                color=_OPT_COLOR,
            )
        ax.set_title(f"First-year cost by lease-start date — {neighborhood}")
        ax.set_xlabel("lease start date")
        ax.set_ylabel("first-year cost ($)")
        ax.legend(loc="upper left", fontsize=8)
        from matplotlib.dates import DateFormatter

        ax.xaxis.set_major_formatter(DateFormatter("%b %d '%y"))  # type: ignore[no-untyped-call]
        fig.autofmt_xdate(rotation=30, ha="right")
        ax.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
        ax.set_ylim(
            min(costs + [result.baseline_cost]) * 0.998,
            max(costs + [result.baseline_cost]) * 1.002,
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


class _style_ctx:
    """Context manager applying the repo's EDA plot style while a figure is built."""

    def __enter__(self) -> _style_ctx:
        plt.style.use(_PLOT_STYLE)
        return self

    def __exit__(self, *_: object) -> None:
        plt.style.use("default")


def _style() -> _style_ctx:
    return _style_ctx()


def _to_num(d: dt.date) -> float:
    """matplotlib numeric date (float days since epoch) — stubs accept float."""
    from matplotlib.dates import date2num

    return date2num(dt.datetime(d.year, d.month, d.day))  # type: ignore[no-untyped-call,no-any-return]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_date(s: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO date: {s!r}") from exc


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Optimal NYC lease-start date vs. seasonal rent discount (PoC).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--neighborhood",
        default="Upper East Side",
        help="Neighborhood to evaluate (median listings price).",
    )
    p.add_argument(
        "--bedrooms",
        type=float,
        default=None,
        help="Filter listings to this bedroom count (e.g. 1).",
    )
    p.add_argument(
        "--rent-override",
        type=float,
        default=None,
        help="Use this dollar baseline R instead of listing medians.",
    )
    p.add_argument(
        "--max-overlap-days",
        type=int,
        default=60,
        help="Cap on double-rent overlap for the optimal-date pick.",
    )
    p.add_argument(
        "--haircut",
        type=float,
        default=1.0,
        help="Multiplier on published seasonal δ (e.g. 0.98 = 2%% haircut).",
    )
    p.add_argument(
        "--lease-end",
        type=_parse_date,
        default=dt.date(2027, 4, 30),
        help="Hard end of the current lease (overlap reference).",
    )
    p.add_argument(
        "--scan-start",
        type=_parse_date,
        default=dt.date(2026, 12, 1),
        help="First candidate start date.",
    )
    p.add_argument(
        "--scan-end",
        type=_parse_date,
        default=dt.date(2027, 6, 30),
        help="Last candidate start date.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="PNG output path (default: outputs/rental_timing/<neighborhood>.png).",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    if args.scan_end < args.scan_start:
        raise SystemExit("--scan-end must be on/after --scan-start")

    settings = Settings()
    settings.ensure_dirs()
    base_rent, n_listings = resolve_baseline_rent(
        settings, args.neighborhood, args.bedrooms, args.rent_override
    )
    dates = candidate_starts(args.scan_start, args.scan_end)
    result = find_best_date(dates, base_rent, args.haircut, args.lease_end, args.max_overlap_days)
    months = monthly_starts(args.scan_start, args.scan_end)
    print_report(
        args.neighborhood,
        n_listings,
        base_rent,
        args.haircut,
        args.lease_end,
        args.max_overlap_days,
        result,
        months,
    )
    out = args.out or settings.outputs_dir / "rental_timing" / f"{_slug(args.neighborhood)}.png"
    plot_cost_curve(
        dates,
        base_rent,
        args.haircut,
        args.lease_end,
        args.max_overlap_days,
        result,
        args.neighborhood,
        out,
    )
    print()
    print(f"Graph written to: {out}")


if __name__ == "__main__":
    main()
