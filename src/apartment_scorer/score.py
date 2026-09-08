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
class LuxuryTowerRule:
    """Heuristic thresholds for classifying a building as a luxury tower."""

    min_floors: int
    min_year: int
    require_flags: list[str]


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
    amenity_flags: dict[str, list[str]]
    luxury_tower: LuxuryTowerRule
    transit_min_distinct_routes: int
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
# Deterministic derivations (pure)
# --------------------------------------------------------------------------- #
def floor_from_unit(unit: Any, building_floor_count: int | None) -> int | None:
    """Parse the floor number from a unit designator like '#18A', '3R', 'PH2'.

    Returns None when no digits are present. Penthouse designators map to the
    building's floor count when it is known.
    """
    if not isinstance(unit, str):
        return None
    text = unit.strip().lstrip("#").upper()
    digits = re.match(r"\d+", text)
    if digits:
        return int(digits.group())
    if text.startswith("PH"):
        ph_digits = re.match(r"PH(\d+)", text)
        if ph_digits:
            return int(ph_digits.group(1))
        return building_floor_count
    return None


def derive_amenity_flags(listing: Listing, card: Scorecard) -> dict[str, bool]:
    """Booleans derived from the listing's `amenities` enum list via the YAML map.

    An explicit boolean field of the same name on the listing always wins
    (manual override of the derived value).
    """
    raw = listing.get("amenities", [])
    present = {_slug(a) for a in raw} if isinstance(raw, list) else set()
    flags: dict[str, bool] = {}
    for flag, enums in card.amenity_flags.items():
        derived = any(_slug(e) in present for e in enums)
        explicit = listing.get(flag)
        flags[flag] = explicit if isinstance(explicit, bool) else derived
    return flags


def resolve_flag(listing: Listing, name: str, fallback: bool) -> bool:
    """Explicit boolean wins; explicit null/absent uses the fallback (derived)."""
    value = listing.get(name)
    return value if isinstance(value, bool) else fallback


def distinct_transit_routes(listing: Listing) -> int:
    """Count distinct route names across `transit_stations` entries."""
    stations = listing.get("transit_stations", [])
    if not isinstance(stations, list):
        return 0
    routes: set[str] = set()
    for station in stations:
        if isinstance(station, dict):
            station_routes = station.get("routes", [])
            if isinstance(station_routes, list):
                routes.update(str(r).strip().upper() for r in station_routes)
    return len(routes)


_SCORECARD_BUILDING_CLASSES = {
    "luxury_tower",
    "elevator_building",
    "walkup",
    "low_rise",
    "townhouse",
    "unknown",
}


def effective_building_class(listing: Listing, card: Scorecard, flags: dict[str, bool]) -> str:
    """Classify the building: luxury_tower | elevator_building | walkup | unknown.

    An explicit `effective_building_class` string always wins. A `building_type`
    string is honored only when it names a scorecard class (e.g. a manual
    `building_type: luxury_tower`); source labels like StreetEasy's
    "Rental building" are ignored and fall through to the heuristic.
    """
    for field_name in ("effective_building_class", "building_type"):
        value = listing.get(field_name)
        if isinstance(value, str) and value.strip():
            slugged = _slug(value).replace(" ", "_")
            if field_name == "effective_building_class" or slugged in _SCORECARD_BUILDING_CLASSES:
                return slugged

    floors = listing.get("building_floor_count")
    year = listing.get("building_year_built")
    floors_num = (
        float(floors) if isinstance(floors, int | float) and not isinstance(floors, bool) else None
    )
    year_num = float(year) if isinstance(year, int | float) and not isinstance(year, bool) else None
    rule = card.luxury_tower
    if (
        floors_num is not None
        and year_num is not None
        and floors_num >= rule.min_floors
        and year_num >= rule.min_year
        and all(flags.get(flag, False) for flag in rule.require_flags)
    ):
        return "luxury_tower"
    if flags.get("has_elevator"):
        return "elevator_building"
    if floors_num is not None:
        return "walkup"
    return "unknown"


def enrich_listing(listing: Listing, card: Scorecard) -> Listing:
    """Return a copy of the listing with derived fields filled in.

    Derived fields (amenity flags, distinct_transit_routes, unit_floor,
    effective_building_class) never overwrite explicit values already present.
    """
    enriched = dict(listing)
    enriched.update(derive_amenity_flags(listing, card))
    if "distinct_transit_routes" not in enriched:
        enriched["distinct_transit_routes"] = distinct_transit_routes(listing)
    if "unit_floor" not in enriched:
        floors = listing.get("building_floor_count")
        enriched["unit_floor"] = floor_from_unit(
            listing.get("unit"), floors if isinstance(floors, int) else None
        )
    if "effective_building_class" not in enriched:
        enriched["effective_building_class"] = effective_building_class(listing, card, enriched)
    return enriched


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

    amenity_flags: dict[str, list[str]] = {}
    for flag, enums in _as_mapping(raw.get("amenity_flags", {}), "amenity_flags").items():
        if not isinstance(enums, list) or not all(isinstance(e, str) for e in enums):
            raise ValueError(f"amenity_flags.{flag} must be a list of strings")
        amenity_flags[str(flag)] = [str(e) for e in enums]

    tower_raw = _as_mapping(
        _as_mapping(raw.get("building_class", {}), "building_class").get("luxury_tower", {}),
        "building_class.luxury_tower",
    )
    require_flags = tower_raw.get("require_flags", [])
    if not isinstance(require_flags, list) or not all(isinstance(f, str) for f in require_flags):
        raise ValueError("building_class.luxury_tower.require_flags must be a list of strings")
    luxury_tower = LuxuryTowerRule(
        min_floors=int(tower_raw.get("min_floors", 0)),
        min_year=int(tower_raw.get("min_year", 0)),
        require_flags=[str(f) for f in require_flags],
    )

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
        amenity_flags=amenity_flags,
        luxury_tower=luxury_tower,
        transit_min_distinct_routes=int(raw.get("transit_min_distinct_routes", 2)),
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
    """Additive elevator/subway/laundry points, clamped to the category max.

    Reads derived fields (has_elevator, distinct_transit_routes,
    laundry_in_building) populated by enrich_listing; explicit booleans or the
    legacy `floor_level`/`subway_lines_nearby` fields also work.
    """
    earned: list[str] = []
    points = 0

    floor = _slug(str(listing.get("floor_level", "")))
    unit_floor = listing.get("unit_floor")
    low_floor = (
        isinstance(unit_floor, int | float) and not isinstance(unit_floor, bool) and unit_floor <= 3
    )
    elevator_ok = (
        listing.get("has_elevator") is True or floor in {"elevator", "walkup low"} or low_floor
    )
    elevator_pts = card.access_points.get("elevator_or_low_walkup", 0)
    if elevator_ok and elevator_pts:
        points += elevator_pts
        earned.append(f"elevator/low walk-up +{elevator_pts}")

    routes = listing.get("distinct_transit_routes")
    lines = listing.get("subway_lines_nearby")
    multi = (
        (
            isinstance(routes, int | float)
            and not isinstance(routes, bool)
            and routes >= card.transit_min_distinct_routes
        )
        or (isinstance(lines, int | float) and not isinstance(lines, bool) and lines >= 2)
        or (listing.get("walk_to_work_anchor") is True)
    )
    transit_pts = card.access_points.get("multi_line_or_walk_anchor", 0)
    if multi and transit_pts:
        points += transit_pts
        earned.append(f"2+ transit routes or walk to work anchor +{transit_pts}")

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
        ("quiet_enough_to_sleep", "quiet enough to sleep", False),
        ("packages_safe", "packages safe", listing.get("has_doorman") is True),
    )
    for field_name, label, fallback in checks:
        pts = card.hygiene_points.get(field_name, 0)
        value = resolve_flag(listing, field_name, fallback)
        if value and pts:
            points += pts
            earned.append(f"{label} +{pts}")

    outdoor_pts = card.hygiene_points.get("usable_balcony_or_bookable_roof", 0)
    derived_roof = listing.get("has_roof_deck") is True
    has_outdoor = (
        resolve_flag(listing, "usable_balcony", False)
        or resolve_flag(listing, "bookable_roof", derived_roof)
        or derived_roof
    )
    if has_outdoor and outdoor_pts:
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
    enriched = enrich_listing(listing, card)
    breakdown = {
        "location": score_location(enriched, card),
        "unit_function": score_unit_function(enriched, card),
        "control": score_control(enriched, card),
        "access": score_access(enriched, card, card.category_max["access"]),
        "hygiene_outdoor": score_hygiene_outdoor(
            enriched, card, card.category_max["hygiene_outdoor"]
        ),
    }
    breakdown = {
        name: CategoryScore(min(cs.points, card.category_max[name]), cs.reasons)
        for name, cs in breakdown.items()
    }
    total = sum(cs.points for cs in breakdown.values())
    triggered = triggered_dealbreakers(enriched, card)
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
# StreetEasy HTML parser (reserved — not yet implemented)
# --------------------------------------------------------------------------- #
def parse_streeteasy_html(html: str) -> Listing:
    """Extract a listing dict from saved StreetEasy listing-page HTML.

    Design (for a future iteration): the page is a Next.js App Router SSR
    document. The deterministic payload lives in the ``self.__next_f`` Flight
    chunks; the fields that matter map 1:1 onto this module's input schema:

    - ``listing.pricing.price``                       -> ``price``
    - ``building.area.name``                          -> ``neighborhood``
    - ``listing.propertyDetails.livingAreaSize``      -> ``sqft``
    - ``listing.propertyDetails.bedroomCount``/``roomCount`` -> ``bedrooms``/``rooms``
    - ``listing.propertyDetails.amenities.list``      -> ``amenities``
      (+ ``sharedOutdoorSpaceTypes`` flattened in; SCREAMING_SNAKE_CASE enum)
    - ``listing.propertyDetails.address.displayUnit`` -> ``unit`` (floor parsed)
    - ``building.floorCount`` / ``yearBuilt``         -> ``building_floor_count``
      / ``building_year_built``
    - ``building.nearby.transitStations``             -> ``transit_stations``
    - ``building.aboutBuildingType``                  -> ``building_type``
    - ``listing.media.floorPlans``                    -> ``floor_plan_count``

    A JSON-LD block (``<script type="application/ld+json">``) duplicates most
    of this (price, address, geo, amenityFeature in snake_case, transit) as a
    fallback. Judgment fields (layout, bed_in_sightline, quiet, bookable
    roof, living situation) stay manual — see README.

    Note: plain HTTP fetches of streeteasy.com are PerimeterX-blocked; pages
    must be saved from a browser session before parsing.
    """
    raise NotImplementedError(
        "StreetEasy HTML parsing is not implemented yet; hand-author the "
        "listing JSON from the page (see src/apartment_scorer/README.md)."
    )


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
