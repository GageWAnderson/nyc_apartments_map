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
    listing = base_listing(neighborhood="LIC", building_type="luxury_tower")
    result = score_mod.score_listing(listing, card)
    assert "lic_or_downtown_brooklyn_tower_plan" in result.dealbreakers_triggered


def test_lic_non_tower_does_not_trigger_tower_rule(card: Any) -> None:
    listing = base_listing(neighborhood="LIC", building_type="walkup")
    result = score_mod.score_listing(listing, card)
    assert "lic_or_downtown_brooklyn_tower_plan" not in result.dealbreakers_triggered


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
        base_listing(floor_level="walkup_high", intends_to_host=True), card
    )
    quiet = score_mod.score_listing(
        base_listing(floor_level="walkup_high", intends_to_host=False), card
    )
    assert "five_flight_walkup_hosting" in hosting.dealbreakers_triggered
    assert "five_flight_walkup_hosting" not in quiet.dealbreakers_triggered


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
