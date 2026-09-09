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
class TrophyTrapRule:
    """Derive ``amenity_trap`` from the trophy-amenity stack (pool, media room…).

    The heuristic fires when the building has at least ``min_amenities`` of the
    mapped trophy amenities AND any of the extra ``require_any_flags`` present.
    """

    min_amenities: int
    require_any_flags: list[str]


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
    trophy_trap: TrophyTrapRule
    transit_min_distinct_routes: int
    access_points: dict[str, int]
    hygiene_points: dict[str, int]
    amenity_trap_cap: int
    flag_scores: dict[str, int]
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


@dataclass(frozen=True)
class BatchRow:
    """One row of batch output: normalized listing plus score and warnings."""

    address: str
    url: str
    price: int | float | None
    bedrooms: int | float | None
    layout: str | None
    total: int
    breakdown: dict[str, CategoryScore]
    dealbreakers_triggered: list[str]
    warnings: list[str]


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
def _bound_to_building(floor: int | None, building_floor_count: int | None) -> int | None:
    """Clamp an implausible parse against the building's known floor count.

    Numeric tower units encode floor+line ("#1020" -> floor 10, line 20), so a
    naive digit parse over-reads ("1020" in a 62-story building). Drop trailing
    digits until the value fits; a still-implausible result means the unit
    naming scheme isn't floor-derived and we return None instead of nonsense.
    """
    if floor is None or building_floor_count is None:
        return floor
    if 1 <= building_floor_count <= 999 and floor > building_floor_count:
        while floor and floor > building_floor_count:
            floor //= 10
        return floor or None
    return floor


def floor_from_unit(unit: Any, building_floor_count: int | None) -> int | None:
    """Parse the floor number from a unit designator like '#18A', '3R', 'PH2'.

    Returns None when no digits are present. Penthouse designators map to the
    building's floor count when it is known. Long numeric units ("#1020" in a
    62-story building) are bounded against the known floor count so the parse
    stays plausible.
    """
    if not isinstance(unit, str):
        return None
    text = unit.strip().lstrip("#").upper()
    if text.startswith("PH"):
        ph_digits = re.match(r"PH(\d+)", text)
        if ph_digits:
            return _bound_to_building(int(ph_digits.group(1)), building_floor_count)
        return building_floor_count
    digits = re.match(r"\d+", text)
    if digits:
        return _bound_to_building(int(digits.group()), building_floor_count)
    # Letter-prefixed unit: the embedded digit run is the floor ("S20M"->20,
    # "N10A"->10). Multi-word tower designators ("WEST-TOWER-11B") are excluded
    # — too ambiguous to parse reliably.
    if "-" not in text:
        embedded = re.search(r"(\d+)", text)
        if embedded:
            return _bound_to_building(int(embedded.group(1)), building_floor_count)
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


def derive_amenity_trap(listing: Listing, card: Scorecard, flags: dict[str, bool]) -> bool:
    """True when the trophy-amenity stack signals an amenity trap.

    Fires when at least ``min_amenities`` trophy amenities are present AND any
    of the extra ``require_any_flags`` is set. An explicit ``amenity_trap``
    boolean on the listing always wins (checked by the caller via ``enrich``).
    """
    rule = card.trophy_trap
    if rule.min_amenities <= 0:
        return False
    raw = listing.get("amenities", [])
    present = {_slug(a) for a in raw} if isinstance(raw, list) else set()
    trophy_enums = {
        _slug(e)
        for flag, enums in card.amenity_flags.items()
        if flag.startswith("trophy_")
        for e in enums
    }
    trophy_count = len(present & trophy_enums)
    extra = any(flags.get(flag, False) for flag in rule.require_any_flags)
    return trophy_count >= rule.min_amenities and extra


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
    if not isinstance(enriched.get("amenity_trap"), bool):
        enriched["amenity_trap"] = derive_amenity_trap(listing, card, enriched)
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

    trap_raw = _as_mapping(
        _as_mapping(raw.get("building_class", {}), "building_class").get("trophy_trap", {}),
        "building_class.trophy_trap",
    )
    trap_flags = trap_raw.get("require_any_flags", [])
    if not isinstance(trap_flags, list) or not all(isinstance(f, str) for f in trap_flags):
        raise ValueError("building_class.trophy_trap.require_any_flags must be a list of strings")
    trophy_trap = TrophyTrapRule(
        min_amenities=int(trap_raw.get("min_amenities", 0)),
        require_any_flags=[str(f) for f in trap_flags],
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
        trophy_trap=trophy_trap,
        transit_min_distinct_routes=int(raw.get("transit_min_distinct_routes", 2)),
        access_points=_int_map(access.get("points"), "access.points"),
        hygiene_points=_int_map(hygiene.get("points"), "hygiene_outdoor.points"),
        amenity_trap_cap=int(hygiene.get("amenity_trap_cap", 0)),
        flag_scores=_int_map(
            _as_mapping(raw.get("amenity_flags_score", {}), "amenity_flags_score").get(
                "points", {}
            ),
            "amenity_flags_score.points",
        ),
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
    private = listing.get("has_private_outdoor") is True
    derived_roof = listing.get("has_roof_deck") is True
    has_outdoor = (
        resolve_flag(listing, "usable_balcony", private)
        or resolve_flag(listing, "bookable_roof", derived_roof)
        or private
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
# Amenity flags (binary add-ons)
# --------------------------------------------------------------------------- #
def score_amenity_flags(listing: Listing, card: Scorecard, max_points: int) -> CategoryScore:
    """Additive 0/1 points for derived amenity flags, clamped to the category max.

    Each key in ``amenity_flags_score.points`` is a flag name (derived by
    ``enrich_listing`` or an explicit boolean on the listing); when the flag is
    true the listing earns that many points. Small by design — these are binary
    presence signals layered on top of the main categories.
    """
    earned: list[str] = []
    points = 0
    for flag, pts in card.flag_scores.items():
        if listing.get(flag) is True and pts:
            points += pts
            earned.append(f"{flag} +{pts}")
    clamped = min(points, max_points)
    if clamped < points:
        earned.append(f"clamped to category max {max_points}")
    return CategoryScore(clamped, earned or ["no amenity flags"])


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
        "amenity_flags_score": score_amenity_flags(
            enriched, card, card.category_max["amenity_flags_score"]
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
# Batch mode (StreetEasy search-result exports)
# --------------------------------------------------------------------------- #
def _split_address_unit(address: str) -> tuple[str, str | None]:
    """Split "311 11th Avenue #PH308" -> ("311 11th Avenue", "PH308").

    Handles "#"-prefixed units and trailing bare unit tokens after the street
    (e.g. "500 West 18th Street WEST-TOWER-11B"). The unit string is returned
    without the leading '#'. When no unit marker is found the whole string is
    the street and the unit is None.
    """
    text = address.strip()
    if "#" in text:
        street, _, unit = text.partition("#")
        return street.strip(), unit.strip() or None
    # Trailing ALL-CAPS token with digits, e.g. "... Street WEST-TOWER-11B"
    match = re.match(r"^(.*?)\s+([A-Z][A-Z0-9-]*\d[A-Z0-9-]*)$", text)
    if match and len(match.group(1)) > 3:
        return match.group(1).strip(), match.group(2)
    return text, None


# Keys in the legacy search-export shape that are already mapped (or are
# export-only noise) and therefore excluded from the generic passthrough.
_EXPORT_KEYS = {
    "address",
    "price",
    "bedrooms",
    "bathrooms",
    "squareFeet",
    "propertyType",
    "neighborhood",
    "listingType",
    "description",
    "amenities",
    "buildingName",
    "agentName",
    "agentBrokerage",
    "latitude",
    "longitude",
    "daysOnStreetEasy",
    "maintenanceFee",
    "taxes",
    "pricePerSqFt",
    "status",
    "yearBuilt",
    "url",
}

# Keys in the flattened detail-export shape (new scraper) that are explicitly
# mapped (or are export-only noise) and therefore excluded from the generic
# passthrough. Prefixed families (pricing_*, propertyDetails_*, ...) and
# *_json blobs are excluded by _is_export_noise instead of being listed here.
_DETAIL_EXPORT_KEYS = {
    # identity / listing meta
    "id",
    "listingId",
    "propertyId",
    "buildingId",
    "slug",
    "state",
    "tier",
    "partial",
    "mlsNumber",
    "saleType",
    "hasFinancialData",
    "originalSearchUrl",
    "createdAt",
    "updatedAt",
    "interestingPriceDelta",
    "interestingChangeAt",
    "isNewDevelopment",
    "isPremiumHdpEnabled",
    "hasTour3d",
    "hasVideos",
    "furnished",
    # address / geo
    "areaName",
    "street",
    "displayUnit",
    "unit",
    "listingAddress",
    "urlPath",
    "zipCode",
    "geoPoint_latitude",
    "geoPoint_longitude",
    # unit facts (mapped)
    "bedroomCount",
    "fullBathroomCount",
    "halfBathroomCount",
    "livingAreaSize",
    "buildingType",
    # pricing (mapped: price/rent; the rest is context noise)
    "rent",
    "totalMonthlyPrice",
    "leaseTermMonths",
    "monthsFree",
    "netEffectivePrice",
    "lease_term_months",
    "months_free",
    "net_effective_rent",
    "security_deposit",
    "due_up_front",
    "furnished_rent",
    "monthlyFees",
    "additionalFees",
    "monthly_taxes",
    "monthly_fees",
    "maintenance",
    "soldPrice",
    "sold_price",
    "sold_date",
    # building facts (mapped)
    "year_built",
    "floorCount",
    "floor_count",
    "stories",
    "residentialUnitCount",
    "residential_unit_count",
    # market context (median rent feeds amenity_premium_over_comps)
    "availableAt",
    "onMarketAt",
    "offMarketAt",
    "daysOnMarket",
    "on_market_at",
    "off_market_at",
    "days_on_market",
    "upcomingOpenHouse",
    "upcomingOpenHouses",
    "userListingDetails",
    "sourceGroupLabel",
    "sourceType",
    "source_group_label",
    "images",
    "contactEmail",
    "contactWebsite",
    "recentListingsPriceStats_bedroomCount",
    "recentListingsPriceStats_rentalPriceStats_maxPrice",
    "recentListingsPriceStats_rentalPriceStats_medianPrice",
    "recentListingsPriceStats_rentalPriceStats_minPrice",
    "recentListingsPriceStats_rentalPriceStats_numListings",
    "recentListingsPriceStats_salePriceStats_maxPrice",
    "recentListingsPriceStats_salePriceStats_medianPrice",
    "recentListingsPriceStats_salePriceStats_minPrice",
    "recentListingsPriceStats_salePriceStats_numListings",
}

# Prefixed key families in the detail export that never map onto the scorer
# schema (nested payload dumps, agent/license/media blobs).
_DETAIL_NOISE_PREFIXES = (
    "pricing_",
    "propertyDetails_",
    "media_",
    "license_",
    "listingSource_",
    "latestListing_",
    "agentsInfo_",
    "emailEnrichment_",
)


def _is_export_noise(key: str) -> bool:
    """True for export keys that must not pass through into the listing."""
    return (
        key in _EXPORT_KEYS
        or key in _DETAIL_EXPORT_KEYS
        or key.endswith("_json")
        or key.startswith(_DETAIL_NOISE_PREFIXES)
    )


def _is_detail_export(raw: dict[str, Any]) -> bool:
    """True for the flattened detail payload (new scraper), False for legacy."""
    return any(key in raw for key in ("street", "listingAddress", "areaName"))


def _first_positive_number(raw: dict[str, Any], *keys: str) -> int | float | None:
    """First key holding a positive number; 0/None/negative mean "unknown"."""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _first_nonnegative_number(raw: dict[str, Any], *keys: str) -> int | float | None:
    """First key holding a number >= 0; unlike _first_positive_number, 0 is kept
    (0 bedrooms is "studio", not "unknown")."""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _has_price_drop(raw: dict[str, Any]) -> bool:
    """True when the pricing_priceChanges_json history shows a price cut."""
    blob = raw.get("pricing_priceChanges_json")
    if not isinstance(blob, str) or not blob.strip():
        return False
    try:
        changes = json.loads(blob)
    except json.JSONDecodeError:
        return False
    prices: list[int | float] = [
        c["price"]
        for c in changes
        if isinstance(c, dict)
        and isinstance(c.get("price"), int | float)
        and not isinstance(c.get("price"), bool)
    ]
    return len(prices) >= 2 and prices[-1] < prices[0]


_LAYOUT_KEYWORD_RULES: tuple[tuple[str, str], ...] = (
    # (layout slug, regex against the lowercased description). First match wins.
    ("alcove_1br", r"\balcove\b"),
    ("junior_1br", r"\bjunior\b"),
    ("fake_1br", r"\b(flex|convertible|pressurized wall|partition)\b"),
    ("railroad", r"\brailroad\b"),
    ("loft", r"\bloft\b"),
)


def infer_layout(raw: dict[str, Any]) -> str | None:
    """Best-effort layout from description keywords + bedroom count + sqft.

    Conservative keyword hits win (alcove/junior/flex/railroad). Otherwise fall
    back on the bedroom count: 0 beds -> a studio (zoned-large when the area
    clears 500 sqft), 1+ beds -> a plain 1BR. Returns None when nothing is known.
    An explicit ``layout`` on the entry always wins (checked by the caller).
    """
    description = str(raw.get("description") or "").lower()
    for layout, pattern in _LAYOUT_KEYWORD_RULES:
        if re.search(pattern, description):
            return layout
    bedrooms = _first_nonnegative_number(raw, "bedroomCount", "propertyDetails_bedroomCount")
    if bedrooms is None:
        return None
    sqft = _first_positive_number(raw, "livingAreaSize", "propertyDetails_livingAreaSize")
    if bedrooms == 0:
        return "large_zoned_studio" if (sqft is not None and sqft >= 500) else "small_open_studio"
    return "true_1br"


def _detail_unit(raw: dict[str, Any]) -> str | None:
    """Unit designator from the detail export's `unit`/`displayUnit` ('#2L' -> '2L')."""
    for key in ("unit", "displayUnit"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lstrip("#") or None
    return None


def _normalize_detail_export(raw: dict[str, Any]) -> Listing:
    """Map a flattened StreetEasy detail-payload entry onto the scorer's schema.

    Same contract as the legacy mapping: zero/empty export values mean
    "unknown" and are omitted. Notable differences from the legacy shape:

    - `rooms` is the real `propertyDetails_roomCount` (not bedroomsapproximated).
    - `amenities` flattens `propertyDetails_amenities_list` +
      `sharedOutdoorSpaceTypes` (where ROOF_DECK lives), deduped in order.
    - `amenity_premium_over_comps` is derived as
      `price - recentListingsPriceStats_rentalPriceStats_medianPrice` when the
      price exceeds the area median (an explicit value on the entry wins).
    """
    listing: Listing = {}

    unit = _detail_unit(raw)
    address = raw.get("listingAddress")
    if isinstance(address, str) and address.strip():
        _, parsed_unit = _split_address_unit(address)
        listing["name"] = address.strip()
        unit = parsed_unit or unit
    else:
        street = raw.get("street")
        if isinstance(street, str) and street.strip():
            name = street.strip()
            if unit:
                name = f"{name} #{unit}"
            listing["name"] = name
    if unit:
        listing["unit"] = unit

    if raw.get("furnished") is True:
        listing["furnished"] = True

    price = _first_positive_number(raw, "price", "rent")
    if price is not None:
        listing["price"] = price
    bedrooms = _first_nonnegative_number(raw, "bedroomCount", "propertyDetails_bedroomCount")
    if bedrooms is not None:
        listing["bedrooms"] = bedrooms
    rooms = _first_positive_number(raw, "propertyDetails_roomCount")
    if rooms is not None:
        listing["rooms"] = rooms
    sqft = _first_positive_number(raw, "livingAreaSize", "propertyDetails_livingAreaSize")
    if sqft is not None:
        listing["sqft"] = sqft

    neighborhood = raw.get("areaName")
    if isinstance(neighborhood, str) and neighborhood.strip():
        listing["neighborhood"] = neighborhood.strip()
    building_type = raw.get("buildingType")
    if isinstance(building_type, str) and building_type.strip():
        listing["building_type"] = building_type.strip()

    amenities: list[str] = []
    for key in (
        "propertyDetails_amenities_list",
        "propertyDetails_amenities_sharedOutdoorSpaceTypes",
        "propertyDetails_features_list",
        "propertyDetails_features_privateOutdoorSpaceTypes",
    ):
        values = raw.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value and value not in amenities:
                    amenities.append(value)
    if amenities:
        listing["amenities"] = amenities

    # Doorman type refines packages_safe: a VIRTUAL doorman doesn't reliably
    # accept packages, so don't give the doorman-derived packages_safe credit.
    doorman_types = raw.get("propertyDetails_amenities_doormanTypes")
    if isinstance(doorman_types, list) and "VIRTUAL" in doorman_types:
        listing["packages_safe"] = False

    # Private outdoor space (BALCONY/TERRACE/GARDEN/PRIVATE_ROOF_DECK) is the
    # real usable-balcony evidence; the shared ROOF_DECK flag is weaker.
    private_outdoor = raw.get("propertyDetails_features_privateOutdoorSpaceTypes")
    if isinstance(private_outdoor, list) and private_outdoor:
        listing["usable_balcony"] = True

    floors = _first_positive_number(raw, "floorCount", "floor_count", "stories")
    if floors is not None:
        listing["building_floor_count"] = floors
    year = _first_positive_number(raw, "yearBuilt", "year_built")
    if year is not None:
        listing["building_year_built"] = year

    url_path = raw.get("urlPath")
    if isinstance(url_path, str) and url_path.strip():
        url = url_path.strip()
        if url.startswith("/"):
            url = "https://streeteasy.com" + url
        listing["source_url"] = url

    net_effective = _first_positive_number(
        raw, "netEffectivePrice", "net_effective_rent", "pricing_netEffectiveRent"
    )
    if net_effective is not None:
        listing["net_effective_rent"] = net_effective
    months_free = _first_positive_number(raw, "monthsFree", "months_free", "pricing_monthsFree")
    if months_free is not None:
        listing["months_free"] = months_free
    days_on_market = _first_nonnegative_number(raw, "daysOnMarket", "days_on_market")
    if days_on_market is not None:
        listing["days_on_market"] = days_on_market
    if _has_price_drop(raw):
        listing["price_dropped"] = True

    if "layout" not in raw:
        inferred = infer_layout(raw)
        if inferred is not None:
            listing["layout"] = inferred

    median = raw.get("recentListingsPriceStats_rentalPriceStats_medianPrice")
    if (
        price is not None
        and isinstance(median, int | float)
        and not isinstance(median, bool)
        and 0 < median < price
        and "amenity_premium_over_comps" not in raw
    ):
        listing["amenity_premium_over_comps"] = price - median
    return listing


def _normalize_legacy_export(raw: dict[str, Any]) -> Listing:
    """Map a legacy Apify search-result export entry onto the scorer's schema."""
    listing: Listing = {}
    address = raw.get("address")
    if isinstance(address, str) and address.strip():
        street, unit = _split_address_unit(address)
        listing["name"] = address.strip()
        if unit:
            listing["unit"] = unit
    for src_key, dst_key in (
        ("price", "price"),
        ("bedrooms", "bedrooms"),
        ("neighborhood", "neighborhood"),
        ("propertyType", "building_type"),
    ):
        value = raw.get(src_key)
        if isinstance(value, (int, float, str)) and value not in ("", 0):
            listing[dst_key] = value
    sqft = raw.get("squareFeet")
    if isinstance(sqft, (int, float)) and not isinstance(sqft, bool) and sqft > 0:
        listing["sqft"] = sqft
    bedrooms = raw.get("bedrooms")
    if isinstance(bedrooms, (int, float)) and not isinstance(bedrooms, bool) and bedrooms > 0:
        listing["rooms"] = bedrooms  # approximation: rooms not exported
    amenities = raw.get("amenities")
    if isinstance(amenities, list) and amenities:
        listing["amenities"] = [a for a in amenities if isinstance(a, str)]
    url = raw.get("url")
    if isinstance(url, str) and url.strip():
        listing["source_url"] = url.strip()
    year_built = raw.get("yearBuilt")
    if isinstance(year_built, (int, float)) and not isinstance(year_built, bool) and year_built > 0:
        listing["building_year_built"] = year_built
    return listing


def normalize_search_listing(raw: dict[str, Any]) -> Listing:
    """Map a StreetEasy export entry onto the scorer's input schema.

    Two export shapes are supported and auto-detected:

    - **Detail export** (current scraper): the flattened listing payload with
      `street`/`listingAddress`/`areaName`/`propertyDetails_*` keys.
    - **Legacy search export** (Apify): `address`/`price`/`bedrooms`/
      `squareFeet`/`neighborhood`/`url`.

    Zero/empty export values mean "unknown" and are omitted so derived flags
    simply stay false and no dealbreaker fires on absent facts. Fields the
    export never carries (layout, living_situation, tour judgments) are left
    out entirely — the batch scorer treats them as null-safe zeros.
    """
    if _is_detail_export(raw):
        listing = _normalize_detail_export(raw)
    else:
        listing = _normalize_legacy_export(raw)
    # Pass through any scorer-native fields already present (lets an export be
    # hand-completed or produced by a richer scraper without losing fields).
    for key, value in raw.items():
        if not _is_export_noise(key) and key not in listing:
            listing[key] = value
    return listing


def _dedupe_key(listing: Listing, raw: dict[str, Any]) -> tuple[str, Any]:
    """Dedupe key: slugified full address (incl. unit) + price.

    Query params (?featured=1 / ?infeed=1) vary across duplicate exports of the
    same unit, so the URL is NOT part of the key.
    """
    name = listing.get("name", "")
    price = listing.get("price", raw.get("price"))
    return _slug(str(name)), price


def score_search_listing(raw: dict[str, Any], card: Scorecard) -> BatchRow:
    """Normalize one search-export entry and score it null-safely.

    Missing/unknown `layout` and `living_situation` score their category as 0
    and append a warning instead of raising — every exported listing gets a
    ranked row. The listing's deterministic fields still drive location,
    access, hygiene, and dealbreakers as usual.
    """
    listing = normalize_search_listing(raw)
    enriched = enrich_listing(listing, card)
    warnings: list[str] = []
    breakdown: dict[str, CategoryScore] = {
        "location": score_location(enriched, card),
    }

    layout = enriched.get("layout")
    if isinstance(layout, str) and layout.strip() and _slug(layout) in card.layout_scores:
        breakdown["unit_function"] = score_unit_function(enriched, card)
    else:
        reason = "layout missing/unknown" if not layout else f"unknown layout {layout!r}"
        breakdown["unit_function"] = CategoryScore(0, [f"{reason} -> 0 (not inferred)"])
        warnings.append(f"unit_function scored 0: {reason}")

    situation = enriched.get("living_situation")
    if isinstance(situation, str) and situation.strip() and _slug(situation) in card.control_scores:
        breakdown["control"] = score_control(enriched, card)
    else:
        reason = (
            "living_situation missing"
            if not situation
            else f"unknown living_situation {situation!r}"
        )
        breakdown["control"] = CategoryScore(0, [f"{reason} -> 0 (not inferred)"])
        warnings.append(f"control scored 0: {reason}")

    breakdown["access"] = score_access(enriched, card, card.category_max["access"])
    breakdown["hygiene_outdoor"] = score_hygiene_outdoor(
        enriched, card, card.category_max["hygiene_outdoor"]
    )
    breakdown["amenity_flags_score"] = score_amenity_flags(
        enriched, card, card.category_max["amenity_flags_score"]
    )
    breakdown = {
        name: CategoryScore(min(cs.points, card.category_max[name]), cs.reasons)
        for name, cs in breakdown.items()
    }
    total = sum(cs.points for cs in breakdown.values())
    triggered = triggered_dealbreakers(enriched, card)
    if triggered and total > card.dealbreaker_cap:
        total = card.dealbreaker_cap

    return BatchRow(
        address=str(listing.get("name", raw.get("address", ""))),
        url=str(listing.get("source_url", "")),
        price=listing.get("price") if isinstance(listing.get("price"), (int, float)) else None,
        bedrooms=listing.get("bedrooms")
        if isinstance(listing.get("bedrooms"), (int, float))
        else None,
        layout=layout if isinstance(layout, str) and layout.strip() else None,
        total=total,
        breakdown=breakdown,
        dealbreakers_triggered=triggered,
        warnings=warnings,
    )


def run_batch(raw_listings: list[Any], card: Scorecard) -> tuple[list[BatchRow], int]:
    """Score a search-export array: dedupe, score, sort by total desc.

    Returns the sorted rows plus the number of duplicates dropped.
    """
    seen: set[tuple[str, Any]] = set()
    rows: list[BatchRow] = []
    dupes = 0
    for raw in raw_listings:
        if not isinstance(raw, dict):
            logger.warning("skipping non-object batch entry: %r", raw)
            continue
        listing = normalize_search_listing(raw)
        key = _dedupe_key(listing, raw)
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        rows.append(score_search_listing(raw, card))
    rows.sort(key=lambda r: r.total, reverse=True)
    if dupes:
        logger.info("deduped %d duplicate listing(s)", dupes)
    return rows, dupes


def format_batch_table(rows: list[BatchRow], dupes: int) -> str:
    """Aligned plain-text table of batch results."""
    header = f"{'SCORE':>5}  {'ADDRESS':<40} {'PRICE':>7} {'BD':>2}  {'LAYOUT':<18} FLAGS"
    lines = [header, "-" * len(header)]
    for row in rows:
        flags: list[str] = []
        if row.dealbreakers_triggered:
            flags.append("!" + ",".join(row.dealbreakers_triggered))
        if row.warnings:
            flags.append("~partial")
        price = f"${int(row.price):,}" if isinstance(row.price, (int, float)) else "-"
        bedrooms = str(int(row.bedrooms)) if isinstance(row.bedrooms, (int, float)) else "-"
        layout = (row.layout or "-")[:18]
        address = row.address[:40]
        flag_str = " ".join(flags)
        lines.append(
            f"{row.total:>5}  {address:<40} {price:>7} {bedrooms:>2}  {layout:<18} {flag_str}"
        )
    lines.append("")
    lines.append(f"{len(rows)} scored · {dupes} deduped")
    return "\n".join(lines)


def batch_rows_to_dicts(rows: list[BatchRow]) -> list[dict[str, Any]]:
    """JSON-serializable view of batch rows (for --json output)."""
    return [
        {
            "address": row.address,
            "url": row.url,
            "price": row.price,
            "bedrooms": row.bedrooms,
            "layout": row.layout,
            "total": row.total,
            "dealbreakers_triggered": row.dealbreakers_triggered,
            "warnings": row.warnings,
            "breakdown": {
                name: {"points": cs.points, "reasons": cs.reasons}
                for name, cs in row.breakdown.items()
            },
        }
        for row in rows
    ]


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
      (+ ``sharedOutdoorSpaceTypes`` + ``features.list`` +
      ``features.privateOutdoorSpaceTypes`` flattened in; SCREAMING_SNAKE_CASE)
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
def _run_single(payload: Any, card: Scorecard, explain: bool) -> int:
    """Single-listing path: print the score (or --explain breakdown)."""
    try:
        result = score_listing(payload, card)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if explain:
        print(json.dumps(result_to_dict(result), indent=2))
    else:
        print(result.total)
    return 0


def _run_batch(payload: list[Any], card: Scorecard, args: argparse.Namespace) -> int:
    """Batch path: dedupe, score, rank; print a table (or --explain JSON array)."""
    rows, dupes = run_batch(payload, card)
    for row in rows:
        for warning in row.warnings:
            logger.warning("%s: %s", row.address or "(no address)", warning)
    if args.json_out is not None:
        args.json_out.write_text(
            json.dumps(batch_rows_to_dicts(rows), indent=2) + "\n", encoding="utf-8"
        )
        logger.info("wrote %d scored listing(s) -> %s", len(rows), args.json_out)
    if args.explain:
        print(json.dumps(batch_rows_to_dicts(rows), indent=2))
    else:
        print(format_batch_table(rows, dupes))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point. Single listing prints its score; a JSON array runs batch."""
    parser = argparse.ArgumentParser(
        description=(
            "Score an apartment listing JSON 0-100 against the scorecard YAML. "
            "If the JSON root is an array (e.g. a StreetEasy search-result export), "
            "batch mode scores every listing and prints a ranked table."
        )
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
        help="Print a JSON breakdown (per-category points + reasons) instead of "
        "the score / ranked table.",
    )
    parser.add_argument(
        "--json",
        dest="json_out",
        type=Path,
        default=None,
        metavar="PATH",
        help="Batch mode only: also write full per-listing breakdowns to PATH.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        card = load_scorecard(args.config)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(args.listing.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"error: cannot read {args.listing}: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: {args.listing} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    if isinstance(payload, list):
        return _run_batch(payload, card, args)
    return _run_single(payload, card, args.explain)


if __name__ == "__main__":
    sys.exit(main())
