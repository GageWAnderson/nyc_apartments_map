"""Score an apartment listing JSON 0–100 against a configurable scorecard.

Standalone rules engine: a structured listing JSON in, a single integer score
out. All tunable numbers (category budgets, neighborhood/layout scores,
point values, dealbreaker rules) live in ``configs/apartment_scorecard.yaml``;
this module only implements the mechanics:

- Five category scorers (location, unit_function, control, access,
  hygiene_outdoor), each clamped to its YAML budget.
- Tour-test overrides: ``bed_in_sightline`` caps the unit score (the "bed is
  the living room" rule), a sub-``studio_large_min_sqft`` "large zoned
  studio" demotes to small-studio points, and ``seats_four_without_bed:
  false`` caps for hosting fail.
- Dealbreakers never zero the score: any matched rule caps the total at
  ``dealbreaker_cap`` and is reported by name.

Lookup keys (neighborhoods, layouts, situations) are normalized slugs:
lowercase, underscores/apostrophes/punctuation folded to spaces. Both the
YAML keys and the JSON values pass through the same normalizer, so
``"Hell's Kitchen"``, ``hells_kitchen`` and ``hells kitchen`` all match.

Run: ``uv run python src/apartment_scorer/score.py listing.json [--explain]
       [--config configs/apartment_scorecard.yaml]``
Exit codes: 0 = scored (score on stdout), 2 = bad input/config.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# src/apartment_scorer/score.py is two directories below the repo root:
#   parents[0]=apartment_scorer/, parents[1]=src/, parents[2]=<repo root>
_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "apartment_scorecard.yaml"

_TOTAL_BUDGET = 100

LayoutValue = str | int | float | bool | None
Listing = dict[str, Any]


# --------------------------------------------------------------------------- #
# Scorecard model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DealbreakerRule:
    """One dealbreaker: matches when every condition in ``when`` holds."""

    name: str
    when: dict[str, Any]


@dataclass(frozen=True)
class Scorecard:
    """Parsed + validated contents of the scorecard YAML."""

    category_max: dict[str, int]
    dealbreaker_cap: int
    location_default: int
    location_scores: dict[str, int]
    layout_scores: dict[str, int]
    studio_large_min_sqft: int
    sightline_cap: int
    hosting_fail_cap: int
    control_scores: dict[str, int]
    access_points: dict[str, int]
    hygiene_points: dict[str, int]
    amenity_trap_cap: int
    dealbreakers: list[DealbreakerRule]


@dataclass(frozen=True)
class CategoryScore:
    """Points awarded in one category plus human-readable reasons."""

    points: int
    reasons: list[str]


@dataclass(frozen=True)
class ScoreResult:
    """Final result: capped total, per-category breakdown, matched rules."""

    total: int
    breakdown: dict[str, CategoryScore]
    dealbreakers_triggered: list[str]


# --------------------------------------------------------------------------- #
# Normalization helpers
# --------------------------------------------------------------------------- #
def _slug(value: str) -> str:
    """Normalize a lookup key: lowercase, punctuation/underscores -> spaces."""
    s = value.strip().lower().replace("_", " ")
    s = re.sub(r"['’]", "", s)  # drop apostrophes: "hell's kitchen" -> "hells kitchen"
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _slug_map(mapping: dict[str, int]) -> dict[str, int]:
    return {_slug(k): v for k, v in mapping.items()}


# --------------------------------------------------------------------------- #
# Scorecard loading / validation
# --------------------------------------------------------------------------- #
def load_scorecard(path: Path) -> Scorecard:
    """Load and validate the scorecard YAML. Raises ValueError on bad config."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read scorecard config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"scorecard config {path} is not a YAML mapping")

    def need(key: str) -> Any:
        if key not in raw:
            raise ValueError(f"scorecard config missing required key: {key!r}")
        return raw[key]

    category_max = {
        str(k): int(v) for k, v in _as_mapping(need("category_max"), "category_max").items()
    }
    total = sum(category_max.values())
    if total != _TOTAL_BUDGET:
        raise ValueError(f"category_max must sum to {_TOTAL_BUDGET}, got {total}")

    location = _as_mapping(need("location"), "location")
    unit = _as_mapping(need("unit_function"), "unit_function")
    control = _as_mapping(need("control"), "control")
    access = _as_mapping(need("access"), "access")
    hygiene = _as_mapping(need("hygiene_outdoor"), "hygiene_outdoor")

    rules: list[DealbreakerRule] = []
    for i, entry in enumerate(_as_list(need("dealbreakers"), "dealbreakers")):
        rule = _as_mapping(entry, f"dealbreakers[{i}]")
        name = rule.get("name")
        when = rule.get("when")
        if not isinstance(name, str) or not name:
            raise ValueError(f"dealbreakers[{i}] missing non-empty 'name'")
        if not isinstance(when, dict) or not when:
            raise ValueError(f"dealbreakers[{i}] ({name}) missing non-empty 'when' mapping")
        rules.append(DealbreakerRule(name=name, when=when))

    return Scorecard(
        category_max=category_max,
        dealbreaker_cap=int(raw.get("dealbreaker_cap", _TOTAL_BUDGET)),
        location_default=int(location.get("default", 0)),
        location_scores=_slug_map(
            _int_map(location.get("neighborhoods"), "location.neighborhoods")
        ),
        layout_scores=_slug_map(_int_map(unit.get("layouts"), "unit_function.layouts")),
        studio_large_min_sqft=int(unit.get("studio_large_min_sqft", 0)),
        sightline_cap=int(unit.get("sightline_cap", 0)),
        hosting_fail_cap=int(unit.get("hosting_fail_cap", 0)),
        control_scores=_slug_map(_int_map(control.get("situations"), "control.situations")),
        access_points=_int_map(access.get("points"), "access.points"),
        hygiene_points=_int_map(hygiene.get("points"), "hygiene_outdoor.points"),
        amenity_trap_cap=int(hygiene.get("amenity_trap_cap", 0)),
        dealbreakers=rules,
    )


def _as_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"scorecard key {label!r} must be a mapping")
    return value


def _as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"scorecard key {label!r} must be a list")
    return value


def _int_map(value: Any, label: str) -> dict[str, int]:
    mapping = _as_mapping(value, label)
    try:
        return {str(k): int(v) for k, v in mapping.items()}
    except (TypeError, ValueError) as exc:
        raise ValueError(f"scorecard key {label!r} must map names to integers") from exc


# --------------------------------------------------------------------------- #
# Category scorers (pure)
# --------------------------------------------------------------------------- #
def score_location(listing: Listing, card: Scorecard) -> CategoryScore:
    """Fixed per-neighborhood score; unknown names fall back to the default."""
    raw = listing.get("neighborhood")
    if not isinstance(raw, str) or not raw.strip():
        logger.warning("listing has no 'neighborhood'; using default location score")
        return CategoryScore(card.location_default, ["neighborhood missing -> default score"])
    key = _slug(raw)
    if key in card.location_scores:
        points = card.location_scores[key]
        return CategoryScore(points, [f"{raw} -> {points}"])
    logger.warning("unknown neighborhood %r; using default location score", raw)
    return CategoryScore(
        card.location_default, [f"{raw} not in scorecard -> default {card.location_default}"]
    )


def score_unit_function(listing: Listing, card: Scorecard) -> CategoryScore:
    """Layout score minus tour-test demotions. Raises ValueError on bad layout."""
    raw_layout = listing.get("layout")
    if not isinstance(raw_layout, str) or not raw_layout.strip():
        raise ValueError("listing missing required string field 'layout'")
    key = _slug(raw_layout)
    if key not in card.layout_scores:
        known = ", ".join(sorted(card.layout_scores))
        raise ValueError(f"unknown layout {raw_layout!r}; expected one of: {known}")

    points = card.layout_scores[key]
    reasons = [f"{raw_layout} -> {points}"]

    small_studio = card.layout_scores.get("small open studio", points)
    if key == "large zoned studio":
        sqft = listing.get("sqft")
        if not isinstance(sqft, int | float) or isinstance(sqft, bool):
            logger.warning("large_zoned_studio without numeric 'sqft'; demoting to small studio")
            points, sqft_ok = small_studio, False
        else:
            sqft_ok = sqft >= card.studio_large_min_sqft
            if not sqft_ok:
                points = small_studio
        if not sqft_ok:
            reasons.append(
                f"sqft below {card.studio_large_min_sqft} -> demoted to small studio ({points})"
            )

    if listing.get("bed_in_sightline") is True and points > card.sightline_cap:
        points = card.sightline_cap
        reasons.append(f"bed in sightline -> scored as a studio (cap {card.sightline_cap})")

    if listing.get("seats_four_without_bed") is False and points > card.hosting_fail_cap:
        points = card.hosting_fail_cap
        reasons.append(
            f"cannot seat 4 without the bed -> hosting fail (cap {card.hosting_fail_cap})"
        )

    return CategoryScore(points, reasons)


def score_control(listing: Listing, card: Scorecard) -> CategoryScore:
    """Solo / known roommate / new roommates. Raises ValueError if missing."""
    raw = listing.get("living_situation")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("listing missing required string field 'living_situation'")
    key = _slug(raw)
    if key not in card.control_scores:
        known = ", ".join(sorted(card.control_scores))
        raise ValueError(f"unknown living_situation {raw!r}; expected one of: {known}")
    points = card.control_scores[key]
    return CategoryScore(points, [f"{raw} -> {points}"])


def score_access(listing: Listing, card: Scorecard, max_points: int) -> CategoryScore:
    """Additive elevator/subway/laundry points, clamped to the category max."""
    earned: list[str] = []
    points = 0

    floor = _slug(str(listing.get("floor_level", "")))
    elevator_pts = card.access_points.get("elevator_or_low_walkup", 0)
    if floor in {"elevator", "walkup low"} and elevator_pts:
        points += elevator_pts
        earned.append(f"elevator/low walk-up +{elevator_pts}")

    lines = listing.get("subway_lines_nearby")
    multi = (isinstance(lines, int | float) and not isinstance(lines, bool) and lines >= 2) or (
        listing.get("walk_to_work_anchor") is True
    )
    transit_pts = card.access_points.get("multi_line_or_walk_anchor", 0)
    if multi and transit_pts:
        points += transit_pts
        earned.append(f"2+ subway lines or walk to work anchor +{transit_pts}")

    laundry_pts = card.access_points.get("laundry_in_building", 0)
    if listing.get("laundry_in_building") is True and laundry_pts:
        points += laundry_pts
        earned.append(f"laundry in building +{laundry_pts}")

    clamped = min(points, max_points)
    if clamped < points:
        earned.append(f"clamped to category max {max_points}")
    return CategoryScore(clamped, earned or ["no access features"])


def score_hygiene_outdoor(listing: Listing, card: Scorecard, max_points: int) -> CategoryScore:
    """Additive quiet/packages/outdoor points; amenity_trap caps the category."""
    earned: list[str] = []
    points = 0

    checks = (
        ("quiet_enough_to_sleep", "quiet enough to sleep"),
        ("packages_safe", "packages safe"),
    )
    for field_name, label in checks:
        pts = card.hygiene_points.get(field_name, 0)
        if listing.get(field_name) is True and pts:
            points += pts
            earned.append(f"{label} +{pts}")

    outdoor_pts = card.hygiene_points.get("usable_balcony_or_bookable_roof", 0)
    if (
        listing.get("usable_balcony") is True or listing.get("bookable_roof") is True
    ) and outdoor_pts:
        points += outdoor_pts
        earned.append(f"usable balcony or bookable roof +{outdoor_pts}")

    if listing.get("amenity_trap") is True and points > card.amenity_trap_cap:
        points = card.amenity_trap_cap
        earned.append(f"trophy amenity stack -> capped at {card.amenity_trap_cap}")

    clamped = min(points, max_points)
    if clamped < points:
        earned.append(f"clamped to category max {max_points}")
    return CategoryScore(clamped, earned or ["no hygiene/outdoor features"])


# --------------------------------------------------------------------------- #
# Dealbreakers
# --------------------------------------------------------------------------- #
def _condition_matches(condition: Any, actual: Any) -> bool:
    """Scalar = exact match (strings slug-normalized); list = any; {min,max} = range."""
    if isinstance(condition, dict):
        if not isinstance(actual, int | float) or isinstance(actual, bool):
            return False
        minimum = condition.get("min")
        maximum = condition.get("max")
        if minimum is not None and actual < minimum:
            return False
        return bool(not (maximum is not None and actual > maximum))
    if isinstance(condition, list):
        return any(_condition_matches(item, actual) for item in condition)
    if isinstance(condition, str) and isinstance(actual, str):
        return _slug(condition) == _slug(actual)
    return bool(condition == actual)


def triggered_dealbreakers(listing: Listing, card: Scorecard) -> list[str]:
    """Names of all dealbreaker rules whose every condition matches."""
    triggered = []
    for rule in card.dealbreakers:
        if all(
            _condition_matches(cond, listing.get(field_name))
            for field_name, cond in rule.when.items()
        ):
            triggered.append(rule.name)
    return triggered


# --------------------------------------------------------------------------- #
# Top-level scoring
# --------------------------------------------------------------------------- #
def score_listing(listing: Listing, card: Scorecard) -> ScoreResult:
    """Score one listing. Raises ValueError on missing/invalid required fields."""
    if not isinstance(listing, dict):
        raise ValueError("listing JSON must be an object")
    breakdown = {
        "location": score_location(listing, card),
        "unit_function": score_unit_function(listing, card),
        "control": score_control(listing, card),
        "access": score_access(listing, card, card.category_max["access"]),
        "hygiene_outdoor": score_hygiene_outdoor(
            listing, card, card.category_max["hygiene_outdoor"]
        ),
    }
    breakdown = {
        name: CategoryScore(min(cs.points, card.category_max[name]), cs.reasons)
        for name, cs in breakdown.items()
    }
    total = sum(cs.points for cs in breakdown.values())
    triggered = triggered_dealbreakers(listing, card)
    if triggered and total > card.dealbreaker_cap:
        total = card.dealbreaker_cap
    return ScoreResult(total=total, breakdown=breakdown, dealbreakers_triggered=triggered)


def result_to_dict(result: ScoreResult) -> dict[str, Any]:
    """JSON-serializable view for --explain output."""
    return {
        "total": result.total,
        "dealbreakers_triggered": result.dealbreakers_triggered,
        "breakdown": {
            name: {"points": cs.points, "reasons": cs.reasons}
            for name, cs in result.breakdown.items()
        },
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    """Argparse entry point. Prints the score (or --explain JSON) to stdout."""
    parser = argparse.ArgumentParser(
        description="Score an apartment listing JSON 0-100 against the scorecard YAML."
    )
    parser.add_argument("listing", type=Path, help="Path to the listing JSON file.")
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help=f"Scorecard YAML path. Default: {_DEFAULT_CONFIG}",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print a JSON breakdown (per-category points + reasons) instead of just the score.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        card = load_scorecard(args.config)
        listing = json.loads(args.listing.read_text(encoding="utf-8"))
        result = score_listing(listing, card)
    except OSError as exc:
        print(f"error: cannot read {args.listing}: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: {args.listing} is not valid JSON: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.explain:
        print(json.dumps(result_to_dict(result), indent=2))
    else:
        print(result.total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
