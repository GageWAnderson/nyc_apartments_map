"""Tests for the apartment scorer PoC (logic-only; no data I/O)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apartment_scorer import score as score_mod

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "apartment_scorecard.yaml"


@pytest.fixture(scope="module")
def card() -> Any:
    return score_mod.load_scorecard(_CONFIG_PATH)


def base_listing(**overrides: Any) -> dict[str, Any]:
    """A solid Tier-1 listing: Chelsea true 1BR, solo, full access/hygiene."""
    listing: dict[str, Any] = {
        "neighborhood": "Chelsea",
        "layout": "true_1br",
        "sqft": 620,
        "living_situation": "solo",
        "bed_in_sightline": False,
        "seats_four_without_bed": True,
        "floor_level": "elevator",
        "subway_lines_nearby": 2,
        "walk_to_work_anchor": True,
        "laundry_in_building": True,
        "quiet_enough_to_sleep": True,
        "packages_safe": True,
        "usable_balcony": False,
        "bookable_roof": True,
        "amenity_trap": False,
    }
    listing.update(overrides)
    return listing


def se_listing(**overrides: Any) -> dict[str, Any]:
    """Deterministic StreetEasy-shaped listing (Chelsea Tower #18A smoke data)."""
    listing: dict[str, Any] = {
        "neighborhood": "Chelsea",
        "layout": "true_1br",
        "living_situation": "solo",
        "price": 6295,
        "sqft": 670,
        "bedrooms": 1,
        "rooms": 2,
        "unit": "#18A",
        "amenities": [
            "BIKE_ROOM",
            "CONCIERGE",
            "ELEVATOR",
            "FIOS_AVAILABLE",
            "DOORMAN",
            "PARKING",
            "SHARED_OUTDOOR_SPACE",
            "GYM",
            "LAUNDRY",
            "LIVE_IN_SUPER",
            "STORAGE_SPACE",
            "WHEELCHAIR_ACCESS",
            "GARDEN",
            "PATIO",
            "ROOF_DECK",
        ],
        "building_floor_count": 33,
        "building_year_built": 2003,
        "transit_stations": [
            {"name": "23rd St", "routes": ["F", "M"], "distance": 0.1347},
            {"name": "23rd Street Station", "routes": ["PATH"], "distance": 0.1423},
            {"name": "28th St", "routes": ["R", "W"], "distance": 0.1563},
            {"name": "28th St", "routes": ["1"], "distance": 0.1657},
            {"name": "23rd St", "routes": ["1"], "distance": 0.1884},
        ],
        "building_type": "Rental building",
        "floor_plan_count": 1,
        "bed_in_sightline": False,
        "seats_four_without_bed": True,
        "walk_to_work_anchor": True,
        "amenity_trap": False,
        "quiet_enough_to_sleep": False,
        "bookable_roof": None,
        "usable_balcony": False,
    }
    listing.update(overrides)
    return listing


# --------------------------------------------------------------------------- #
# Scorecard loading / validation
# --------------------------------------------------------------------------- #
def test_repo_scorecard_loads_and_budgets_sum_to_100(card: Any) -> None:
    assert sum(card.category_max.values()) == 100
    assert card.dealbreaker_cap < 100


def test_load_scorecard_rejects_bad_budget(tmp_path: Path) -> None:
    bad = _CONFIG_PATH.read_text().replace("location: 30", "location: 31", 1)
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(bad)
    with pytest.raises(ValueError, match="sum to 100"):
        score_mod.load_scorecard(cfg)


def test_load_scorecard_rejects_missing_key(tmp_path: Path) -> None:
    cfg = tmp_path / "thin.yaml"
    cfg.write_text("category_max: {location: 100}\n")
    with pytest.raises(ValueError, match="missing required key"):
        score_mod.load_scorecard(cfg)


# --------------------------------------------------------------------------- #
# Slug normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Hell's Kitchen", "hells kitchen"),
        ("true_1br", "true 1br"),
        ("  Gramercy  Park ", "gramercy park"),
        ("LIC", "lic"),
    ],
)
def test_slug_normalizes(raw: str, expected: str) -> None:
    assert score_mod._slug(raw) == expected


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("neighborhood", "band_lo", "band_hi"),
    [
        ("Chelsea", 25, 30),
        ("Gramercy", 25, 30),
        ("west village", 25, 30),
        ("Hell's Kitchen", 18, 24),
        ("Fort Greene", 18, 24),
        ("East Village", 18, 24),
        ("Greenpoint", 10, 17),
        ("Upper West Side", 10, 17),
        ("Long Island City", 0, 9),
        ("FiDi", 0, 9),
        ("Midtown East", 0, 9),
        ("Bushwick", 0, 9),
    ],
)
def test_location_bands(card: Any, neighborhood: str, band_lo: int, band_hi: int) -> None:
    cs = score_mod.score_location({"neighborhood": neighborhood}, card)
    assert band_lo <= cs.points <= band_hi
    assert cs.points <= card.category_max["location"]


def test_location_unknown_falls_back_to_default(card: Any) -> None:
    cs = score_mod.score_location({"neighborhood": "Cupertino"}, card)
    assert cs.points == card.location_default


def test_location_missing_falls_back_to_default(card: Any) -> None:
    cs = score_mod.score_location({}, card)
    assert cs.points == card.location_default


# --------------------------------------------------------------------------- #
# Unit function
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("layout", "band_lo", "band_hi"),
    [
        ("true_1br", 25, 30),
        ("alcove_1br", 25, 30),
        ("large_zoned_studio", 15, 24),
        ("small_open_studio", 0, 14),
        ("railroad", 0, 14),
        ("fake_1br", 0, 14),
    ],
)
def test_layout_bands(card: Any, layout: str, band_lo: int, band_hi: int) -> None:
    cs = score_mod.score_unit_function({"layout": layout, "sqft": 600}, card)
    assert band_lo <= cs.points <= band_hi


def test_bed_in_sightline_demotes_1br_to_studio_cap(card: Any) -> None:
    cs = score_mod.score_unit_function({"layout": "true_1br", "bed_in_sightline": True}, card)
    assert cs.points <= card.sightline_cap


def test_sub_500sqft_large_studio_demoted(card: Any) -> None:
    cs = score_mod.score_unit_function({"layout": "large_zoned_studio", "sqft": 450}, card)
    assert cs.points == card.layout_scores["small open studio"]


def test_missing_sqft_large_studio_demoted(card: Any) -> None:
    cs = score_mod.score_unit_function({"layout": "large_zoned_studio"}, card)
    assert cs.points == card.layout_scores["small open studio"]


def test_hosting_fail_caps_unit(card: Any) -> None:
    cs = score_mod.score_unit_function(
        {"layout": "alcove_1br", "seats_four_without_bed": False}, card
    )
    assert cs.points <= card.hosting_fail_cap


def test_unknown_layout_raises(card: Any) -> None:
    with pytest.raises(ValueError, match="unknown layout"):
        score_mod.score_unit_function({"layout": "penthouse"}, card)


def test_missing_layout_raises(card: Any) -> None:
    with pytest.raises(ValueError, match="layout"):
        score_mod.score_unit_function({}, card)


# --------------------------------------------------------------------------- #
# Control
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("situation", "expected"),
    [("solo", 15), ("known_roommate", 10), ("new_roommates", 0)],
)
def test_control_scores(card: Any, situation: str, expected: int) -> None:
    cs = score_mod.score_control({"living_situation": situation}, card)
    assert cs.points == expected


def test_control_missing_raises(card: Any) -> None:
    with pytest.raises(ValueError, match="living_situation"):
        score_mod.score_control({}, card)


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #
def test_access_full_points(card: Any) -> None:
    listing = {
        "floor_level": "elevator",
        "subway_lines_nearby": 3,
        "laundry_in_building": True,
    }
    cs = score_mod.score_access(listing, card, card.category_max["access"])
    assert cs.points == 15


def test_access_walkup_low_counts(card: Any) -> None:
    listing = {"floor_level": "walkup_low", "walk_to_work_anchor": True}
    cs = score_mod.score_access(listing, card, card.category_max["access"])
    assert cs.points == 11  # 6 elevator/low walkup + 5 transit, no laundry


def test_access_walkup_high_gets_no_elevator_points(card: Any) -> None:
    listing = {"floor_level": "walkup_high", "subway_lines_nearby": 1}
    cs = score_mod.score_access(listing, card, card.category_max["access"])
    assert cs.points == 0


def test_access_clamped_to_category_max(card: Any) -> None:
    listing = {
        "floor_level": "elevator",
        "subway_lines_nearby": 2,
        "laundry_in_building": True,
    }
    cs = score_mod.score_access(listing, card, max_points=10)
    assert cs.points == 10


# --------------------------------------------------------------------------- #
# Hygiene / outdoor
# --------------------------------------------------------------------------- #
def test_hygiene_full_points(card: Any) -> None:
    listing = {
        "quiet_enough_to_sleep": True,
        "packages_safe": True,
        "usable_balcony": True,
    }
    cs = score_mod.score_hygiene_outdoor(listing, card, card.category_max["hygiene_outdoor"])
    assert cs.points == 8


def test_hygiene_bookable_roof_alternative(card: Any) -> None:
    cs = score_mod.score_hygiene_outdoor({"bookable_roof": True}, card, 10)
    assert cs.points == 3


def test_hygiene_amenity_trap_caps(card: Any) -> None:
    listing = {
        "quiet_enough_to_sleep": True,
        "packages_safe": True,
        "usable_balcony": True,
        "amenity_trap": True,
    }
    cs = score_mod.score_hygiene_outdoor(listing, card, card.category_max["hygiene_outdoor"])
    assert cs.points == card.amenity_trap_cap


# --------------------------------------------------------------------------- #
# Dealbreakers
# --------------------------------------------------------------------------- #
def test_new_roommates_triggers_and_caps(card: Any) -> None:
    result = score_mod.score_listing(base_listing(living_situation="new_roommates"), card)
    assert "two_plus_new_roommates" in result.dealbreakers_triggered
    assert result.total <= card.dealbreaker_cap


def test_bed_in_sightline_open_studio_triggers(card: Any) -> None:
    listing = base_listing(layout="small_open_studio", bed_in_sightline=True, sqft=350)
    result = score_mod.score_listing(listing, card)
    assert "bed_in_sightline_open_studio" in result.dealbreakers_triggered


def test_bed_in_sightline_1br_does_not_trigger_studio_rule(card: Any) -> None:
    result = score_mod.score_listing(base_listing(bed_in_sightline=True), card)
    assert "bed_in_sightline_open_studio" not in result.dealbreakers_triggered


def test_lic_luxury_tower_triggers(card: Any) -> None:
    listing = se_listing(neighborhood="LIC")
    result = score_mod.score_listing(listing, card)
    assert "lic_or_downtown_brooklyn_tower_plan" in result.dealbreakers_triggered


def test_lic_non_tower_does_not_trigger_tower_rule(card: Any) -> None:
    listing = se_listing(
        neighborhood="LIC", building_floor_count=5, building_year_built=1925, amenities=[]
    )
    result = score_mod.score_listing(listing, card)
    assert "lic_or_downtown_brooklyn_tower_plan" not in result.dealbreakers_triggered


def test_building_type_manual_override_still_works(card: Any) -> None:
    listing = base_listing(neighborhood="LIC", building_type="luxury_tower")
    result = score_mod.score_listing(listing, card)
    assert "lic_or_downtown_brooklyn_tower_plan" in result.dealbreakers_triggered


def test_midtown_east_identity_move_triggers(card: Any) -> None:
    result = score_mod.score_listing(base_listing(neighborhood="Murray Hill"), card)
    assert "midtown_east_identity_move" in result.dealbreakers_triggered


def test_amenity_premium_threshold(card: Any) -> None:
    over = score_mod.score_listing(base_listing(amenity_premium_over_comps=1200), card)
    under = score_mod.score_listing(base_listing(amenity_premium_over_comps=500), card)
    assert "thousand_dollar_amenity_premium" in over.dealbreakers_triggered
    assert "thousand_dollar_amenity_premium" not in under.dealbreakers_triggered


def test_five_flight_walkup_requires_hosting_intent(card: Any) -> None:
    hosting = score_mod.score_listing(
        se_listing(
            unit="#5A",
            amenities=["LAUNDRY"],
            building_floor_count=6,
            building_year_built=1910,
            intends_to_host=True,
        ),
        card,
    )
    quiet = score_mod.score_listing(
        se_listing(
            unit="#5A",
            amenities=["LAUNDRY"],
            building_floor_count=6,
            building_year_built=1910,
            intends_to_host=False,
        ),
        card,
    )
    assert "five_flight_walkup_hosting" in hosting.dealbreakers_triggered
    assert "five_flight_walkup_hosting" not in quiet.dealbreakers_triggered


def test_five_flight_rule_needs_no_elevator(card: Any) -> None:
    listing = se_listing(
        unit="#12C",
        building_floor_count=20,
        building_year_built=1965,
        intends_to_host=True,
    )
    result = score_mod.score_listing(listing, card)
    assert "five_flight_walkup_hosting" not in result.dealbreakers_triggered


def test_clean_listing_triggers_nothing(card: Any) -> None:
    result = score_mod.score_listing(base_listing(), card)
    assert result.dealbreakers_triggered == []


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #
def test_chelsea_true_1br_solo_scores_high(card: Any) -> None:
    result = score_mod.score_listing(base_listing(), card)
    assert result.total == 28 + 28 + 15 + 15 + 8  # 94
    assert set(result.breakdown) == {
        "location",
        "unit_function",
        "control",
        "access",
        "hygiene_outdoor",
    }


def test_current_baseline_scores_midrange(card: Any) -> None:
    """Midtown East 2BR with one known roommate: weak map, decent control."""
    listing = base_listing(
        neighborhood="Midtown East",
        layout="true_1br",  # effective private space within the 2BR share
        living_situation="known_roommate",
        walk_to_work_anchor=False,
    )
    result = score_mod.score_listing(listing, card)
    assert 45 <= result.total <= 75


def test_score_never_exceeds_100(card: Any) -> None:
    listing = base_listing(usable_balcony=True)
    result = score_mod.score_listing(listing, card)
    assert result.total <= 100


def test_result_to_dict_is_json_serializable(card: Any) -> None:
    result = score_mod.score_listing(base_listing(), card)
    payload = json.loads(json.dumps(score_mod.result_to_dict(result)))
    assert payload["total"] == result.total


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_prints_single_score(tmp_path: Path, capsys: Any) -> None:
    listing_file = tmp_path / "listing.json"
    listing_file.write_text(json.dumps(base_listing()))
    rc = score_mod.main([str(listing_file)])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "94"


def test_cli_explain_prints_breakdown(tmp_path: Path, capsys: Any) -> None:
    listing_file = tmp_path / "listing.json"
    listing_file.write_text(json.dumps(base_listing(living_situation="new_roommates")))
    rc = score_mod.main([str(listing_file), "--explain"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["total"] <= 49
    assert "two_plus_new_roommates" in payload["dealbreakers_triggered"]
    assert payload["breakdown"]["location"]["points"] == 28


def test_cli_bad_json_exits_2(tmp_path: Path, capsys: Any) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    rc = score_mod.main([str(bad)])
    assert rc == 2
    assert "error" in capsys.readouterr().err


def test_cli_missing_file_exits_2(tmp_path: Path, capsys: Any) -> None:
    rc = score_mod.main([str(tmp_path / "nope.json")])
    assert rc == 2
    assert "error" in capsys.readouterr().err


def test_cli_unknown_layout_exits_2(tmp_path: Path, capsys: Any) -> None:
    listing_file = tmp_path / "listing.json"
    listing_file.write_text(json.dumps(base_listing(layout="penthouse")))
    rc = score_mod.main([str(listing_file)])
    assert rc == 2
    assert "unknown layout" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Deterministic derivations (StreetEasy-shaped inputs)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("#18A", 18),
        ("18A", 18),
        ("3R", 3),
        ("#07H", 7),
        ("PH2", 2),
        ("GARDEN", None),
        ("", None),
    ],
)
def test_floor_from_unit(unit: str, expected: int | None) -> None:
    assert score_mod.floor_from_unit(unit, None) == expected


def test_floor_from_unit_ph_falls_back_to_building_floors() -> None:
    assert score_mod.floor_from_unit("PH", 30) == 30
    assert score_mod.floor_from_unit("PH", None) is None


def test_derive_amenity_flags_from_enum(card: Any) -> None:
    flags = score_mod.derive_amenity_flags(
        {"amenities": ["ELEVATOR", "LAUNDRY", "DOORMAN", "GYM", "ROOF_DECK"]}, card
    )
    assert flags == {
        "laundry_in_building": True,
        "has_elevator": True,
        "has_doorman": True,
        "has_gym": True,
        "has_roof_deck": True,
        "has_shared_outdoor": False,
    }


def test_derive_amenity_flags_accepts_json_ld_snake_case(card: Any) -> None:
    flags = score_mod.derive_amenity_flags({"amenities": ["laundry", "elevator"]}, card)
    assert flags["laundry_in_building"] is True
    assert flags["has_elevator"] is True


def test_explicit_boolean_overrides_derived_flag(card: Any) -> None:
    flags = score_mod.derive_amenity_flags(
        {"amenities": ["LAUNDRY"], "laundry_in_building": False}, card
    )
    assert flags["laundry_in_building"] is False


def test_derive_amenity_flags_no_amenities(card: Any) -> None:
    flags = score_mod.derive_amenity_flags({}, card)
    assert all(v is False for v in flags.values())


def test_distinct_transit_routes() -> None:
    listing = se_listing()
    assert score_mod.distinct_transit_routes(listing) == 6  # F, M, PATH, R, W, 1


def test_distinct_transit_routes_empty() -> None:
    assert score_mod.distinct_transit_routes({}) == 0
    assert score_mod.distinct_transit_routes({"transit_stations": "oops"}) == 0


def test_effective_building_class_luxury_tower(card: Any) -> None:
    listing = se_listing()
    flags = score_mod.derive_amenity_flags(listing, card)
    assert score_mod.effective_building_class(listing, card, flags) == "luxury_tower"


def test_effective_building_class_elevator_building(card: Any) -> None:
    listing = se_listing(building_floor_count=12, building_year_built=1965, amenities=["ELEVATOR"])
    flags = score_mod.derive_amenity_flags(listing, card)
    assert score_mod.effective_building_class(listing, card, flags) == "elevator_building"


def test_effective_building_class_walkup(card: Any) -> None:
    listing = se_listing(building_floor_count=5, building_year_built=1920, amenities=[])
    flags = score_mod.derive_amenity_flags(listing, card)
    assert score_mod.effective_building_class(listing, card, flags) == "walkup"


def test_effective_building_class_unknown(card: Any) -> None:
    listing = se_listing(building_floor_count=None, building_year_built=None, amenities=[])
    del listing["building_floor_count"], listing["building_year_built"]
    flags = score_mod.derive_amenity_flags(listing, card)
    assert score_mod.effective_building_class(listing, card, flags) == "unknown"


def test_effective_building_class_manual_override(card: Any) -> None:
    listing = se_listing(building_type="walkup")
    flags = score_mod.derive_amenity_flags(listing, card)
    assert score_mod.effective_building_class(listing, card, flags) == "walkup"


def test_enrich_listing_fills_derived_fields(card: Any) -> None:
    enriched = score_mod.enrich_listing(se_listing(), card)
    assert enriched["unit_floor"] == 18
    assert enriched["distinct_transit_routes"] == 6
    assert enriched["effective_building_class"] == "luxury_tower"
    assert enriched["has_elevator"] is True
    assert enriched["laundry_in_building"] is True


def test_enrich_listing_never_overwrites_explicit(card: Any) -> None:
    enriched = score_mod.enrich_listing(se_listing(unit_floor=2, distinct_transit_routes=1), card)
    assert enriched["unit_floor"] == 2
    assert enriched["distinct_transit_routes"] == 1


# --------------------------------------------------------------------------- #
# StreetEasy-shaped end-to-end
# --------------------------------------------------------------------------- #
def test_se_listing_scores_from_derived_fields_only(card: Any) -> None:
    """Chelsea Tower #18A with no manual access/hygiene booleans at all."""
    result = score_mod.score_listing(se_listing(), card)
    assert result.dealbreakers_triggered == []
    assert result.breakdown["access"].points == 15
    assert result.breakdown["hygiene_outdoor"].points == 5  # doorman + roof deck
    assert result.total == 28 + 28 + 15 + 15 + 5  # 91


def test_se_listing_access_works_without_floor_level(card: Any) -> None:
    result = score_mod.score_listing(se_listing(amenities=["LAUNDRY"], unit="#2A"), card)
    assert result.breakdown["access"].points == 6 + 5 + 4  # low floor + routes + laundry


def test_packages_safe_explicit_null_falls_back_to_doorman(card: Any) -> None:
    result = score_mod.score_listing(se_listing(packages_safe=None), card)
    assert result.breakdown["hygiene_outdoor"].points == 5


def test_parser_stub_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        score_mod.parse_streeteasy_html("<html></html>")


# --------------------------------------------------------------------------- #
# Batch mode (StreetEasy search-result exports)
# --------------------------------------------------------------------------- #
_SEARCH_FILE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "listings"
    / "chelsea-DPMjSjjxVAsilXG9J"
    / "listings.json"
)


def search_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "address": "100 West 26th Street #18A",
        "price": 6295,
        "bedrooms": 1,
        "bathrooms": 1,
        "squareFeet": 670,
        "propertyType": "Rental unit",
        "neighborhood": "Chelsea",
        "listingType": "rental",
        "description": "",
        "amenities": [],
        "latitude": 0,
        "longitude": 0,
        "daysOnStreetEasy": 0,
        "yearBuilt": 0,
        "url": "https://streeteasy.com/building/chelsea-tower/18a",
    }
    entry.update(overrides)
    return entry


# --- normalize_search_listing --------------------------------------------- #
def test_normalize_maps_export_fields() -> None:
    listing = score_mod.normalize_search_listing(search_entry())
    assert listing["name"] == "100 West 26th Street #18A"
    assert listing["unit"] == "18A"
    assert listing["price"] == 6295
    assert listing["bedrooms"] == 1
    assert listing["sqft"] == 670
    assert listing["rooms"] == 1
    assert listing["neighborhood"] == "Chelsea"
    assert listing["building_type"] == "Rental unit"
    assert listing["source_url"] == "https://streeteasy.com/building/chelsea-tower/18a"


def test_normalize_omits_zero_and_empty_values() -> None:
    entry = search_entry(squareFeet=0, price=0, amenities=[], yearBuilt=0, description="")
    listing = score_mod.normalize_search_listing(entry)
    assert "sqft" not in listing
    assert "price" not in listing
    assert "amenities" not in listing
    assert "building_year_built" not in listing


def test_normalize_zero_bedrooms_gives_no_rooms() -> None:
    listing = score_mod.normalize_search_listing(search_entry(bedrooms=0))
    assert "rooms" not in listing


@pytest.mark.parametrize(
    ("address", "street", "unit"),
    [
        ("311 11th Avenue #PH308", "311 11th Avenue", "PH308"),
        ("243 West 28th Street #S20M", "243 West 28th Street", "S20M"),
        ("100 West 26th Street #18A", "100 West 26th Street", "18A"),
        ("500 West 18th Street WEST-TOWER-11B", "500 West 18th Street", "WEST-TOWER-11B"),
        ("311 West 19th Street #1", "311 West 19th Street", "1"),
        ("147 West 22nd Street", "147 West 22nd Street", None),
    ],
)
def test_split_address_unit(address: str, street: str, unit: str | None) -> None:
    assert score_mod._split_address_unit(address) == (street, unit)


def test_unit_floor_feeds_from_batch_address(card: Any) -> None:
    listing = score_mod.normalize_search_listing(search_entry(address="243 West 28th Street #S20M"))
    enriched = score_mod.enrich_listing(listing, card)
    assert enriched["unit_floor"] == 20


# --- null-safe scoring ----------------------------------------------------- #
def test_score_search_listing_missing_layout_and_situation(card: Any) -> None:
    row = score_mod.score_search_listing(search_entry(), card)
    assert row.breakdown["unit_function"].points == 0
    assert row.breakdown["control"].points == 0
    assert len(row.warnings) == 2
    # deterministic categories still score
    assert row.breakdown["location"].points == 28
    assert row.total == sum(cs.points for cs in row.breakdown.values())


def test_score_search_listing_with_overrides_scores_fully(card: Any) -> None:
    entry = search_entry()
    entry["layout"] = "true_1br"
    entry["living_situation"] = "solo"
    entry["amenities"] = ["ELEVATOR", "LAUNDRY", "DOORMAN"]
    row = score_mod.score_search_listing(entry, card)
    assert row.warnings == []
    assert row.breakdown["unit_function"].points == 28
    assert row.breakdown["control"].points == 15


def test_score_search_listing_unknown_layout_warns(card: Any) -> None:
    entry = search_entry()
    entry["layout"] = "penthouse"
    row = score_mod.score_search_listing(entry, card)
    assert row.breakdown["unit_function"].points == 0
    assert any("unknown layout" in w for w in row.warnings)


# --- dedupe ---------------------------------------------------------------- #
def test_run_batch_dedupes_featured_infeed_urls(card: Any) -> None:
    base = search_entry()
    entries = [
        base,
        search_entry(url="https://streeteasy.com/building/chelsea-tower/18a?featured=1"),
        search_entry(url="https://streeteasy.com/building/chelsea-tower/18a?infeed=1"),
    ]
    rows, dupes = score_mod.run_batch(entries, card)
    assert len(rows) == 1
    assert dupes == 2


def test_run_batch_different_price_not_deduped(card: Any) -> None:
    rows, dupes = score_mod.run_batch([search_entry(), search_entry(price=6495)], card)
    assert len(rows) == 2
    assert dupes == 0


def test_run_batch_skips_non_objects(card: Any) -> None:
    rows, _ = score_mod.run_batch([search_entry(), "oops", 42], card)
    assert len(rows) == 1


def test_run_batch_sorted_desc(card: Any) -> None:
    entries = [
        search_entry(address="1 Low St #1", neighborhood="Long Island City"),
        search_entry(address="1 High St #2", neighborhood="Chelsea"),
    ]
    rows, _ = score_mod.run_batch(entries, card)
    assert rows[0].total >= rows[1].total
    assert rows[0].address == "1 High St #2"


# --- real export file ------------------------------------------------------- #
@pytest.mark.skipif(not _SEARCH_FILE.exists(), reason="search export not present")
def test_batch_on_real_export_file(card: Any) -> None:
    raw = json.loads(_SEARCH_FILE.read_text())
    rows, dupes = score_mod.run_batch(raw, card)
    assert len(raw) == 50
    assert len(rows) + dupes == 50
    assert dupes > 0
    # every row scored; sorted desc
    assert all(isinstance(r.total, int) for r in rows)
    assert [r.total for r in rows] == sorted((r.total for r in rows), reverse=True)
    # known listing appears exactly once
    tower = [r for r in rows if "100 West 26th Street #18A" in r.address]
    assert len(tower) == 1
    # West Chelsea now resolves (not the default)
    west = [r for r in rows if "West Chelsea" in str(r.breakdown["location"].reasons)]
    assert all(r.breakdown["location"].points == 26 for r in west)
    # Chelsea proper rows outrank West Chelsea rows
    assert rows[0].breakdown["location"].points == 28


# --- batch CLI -------------------------------------------------------------- #
def test_cli_batch_table(tmp_path: Path, capsys: Any) -> None:
    f = tmp_path / "listings.json"
    f.write_text(json.dumps([search_entry(), search_entry(address="1 Other St #3")]))
    rc = score_mod.main([str(f)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "SCORE" in out and "ADDRESS" in out
    assert "2 scored" in out
    assert "100 West 26th Street #18A" in out


def test_cli_batch_json_out(tmp_path: Path, capsys: Any) -> None:
    src = tmp_path / "listings.json"
    src.write_text(json.dumps([search_entry()]))
    out_json = tmp_path / "out.json"
    rc = score_mod.main([str(src), "--json", str(out_json)])
    assert rc == 0
    rows = json.loads(out_json.read_text())
    assert len(rows) == 1
    assert rows[0]["address"] == "100 West 26th Street #18A"
    assert rows[0]["warnings"]
    assert "breakdown" in rows[0]
    capsys.readouterr()


def test_cli_batch_explain_prints_array(tmp_path: Path, capsys: Any) -> None:
    src = tmp_path / "listings.json"
    src.write_text(json.dumps([search_entry()]))
    rc = score_mod.main([str(src), "--explain"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert isinstance(payload, list)
    assert payload[0]["total"] >= 0


def test_cli_single_dict_still_single_mode(tmp_path: Path, capsys: Any) -> None:
    f = tmp_path / "listing.json"
    f.write_text(json.dumps(base_listing()))
    rc = score_mod.main([str(f)])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "94"


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("S20M", 20),
        ("N10A", 10),
        ("22A12", 22),
        ("WEST-TOWER-11B", None),  # multi-word designator: too ambiguous
        ("PH308", 308),
        ("GARDEN", None),
    ],
)
def test_floor_from_unit_letter_prefixed(unit: str, expected: int | None) -> None:
    assert score_mod.floor_from_unit(unit, None) == expected


def test_normalize_passes_through_scorer_native_fields() -> None:
    entry = search_entry()
    entry["layout"] = "true_1br"
    entry["living_situation"] = "solo"
    entry["bed_in_sightline"] = False
    listing = score_mod.normalize_search_listing(entry)
    assert listing["layout"] == "true_1br"
    assert listing["living_situation"] == "solo"
    assert listing["bed_in_sightline"] is False


def test_normalize_passthrough_skips_export_noise() -> None:
    listing = score_mod.normalize_search_listing(search_entry())
    for noise in ("maintenanceFee", "taxes", "pricePerSqFt", "agentBrokerage", "description"):
        assert noise not in listing
