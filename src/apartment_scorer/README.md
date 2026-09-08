# Apartment Listing Scorer

Scores a single apartment listing JSON **0–100** against the configurable
scorecard in [`configs/apartment_scorecard.yaml`](../../configs/apartment_scorecard.yaml).
Standalone PoC (same pattern as `src/rental_timing_calculator/`): argparse
CLI, no dependency on the data pipeline, logic-only tests in
`tests/test_apartment_scorer.py`.

The rubric optimizes for, in priority order: who will come over
(neighborhood friction) → control (0–1 known roommates) → usable living zone
→ walk-out + trains → building hygiene → outdoor/amenities as bonus.
Trophy amenities and luxury premiums score ~nothing.

## Usage

```bash
uv run python src/apartment_scorer/score.py listing.json            # prints e.g. 94
uv run python src/apartment_scorer/score.py listing.json --explain  # full JSON breakdown
uv run python src/apartment_scorer/score.py listing.json --config my_scorecard.yaml
```

Exit codes: `0` = scored (score on stdout), `2` = unreadable/invalid JSON,
unknown `layout`/`living_situation`, or malformed config.

## Input schema (structured JSON)

Required string fields (missing/unknown values are an error):

| Field | Values |
|---|---|
| `layout` | `true_1br`, `alcove_1br` / `junior_1br`, `large_zoned_studio` (needs `sqft` ≥ 500), `small_open_studio` / `open_studio`, `railroad`, `fake_1br` |
| `living_situation` | `solo`, `known_roommate`, `new_roommates` |

Optional fields (absent = no points / rule doesn't match):

| Field | Type | Effect |
|---|---|---|
| `neighborhood` | string | Free text, slug-normalized and looked up in the YAML (unknown → `location.default` + warning) |
| `sqft` | number | Required in practice for `large_zoned_studio` |
| `bed_in_sightline` | bool | `true` caps unit score at the studio band (tour test) |
| `seats_four_without_bed` | bool | `false` caps unit score (hosting fail) |
| `floor_level` | string | `elevator`, `walkup_low`, `walkup_high` |
| `subway_lines_nearby` | number | ≥ 2 earns transit points |
| `walk_to_work_anchor` | bool | Walkable to Hudson Yards / Union Square; transit-point alternative |
| `laundry_in_building` | bool | Access points |
| `quiet_enough_to_sleep` | bool | Hygiene points |
| `packages_safe` | bool | Hygiene points |
| `usable_balcony` / `bookable_roof` | bool | Hygiene points (either) |
| `amenity_trap` | bool | Trophy finishes / sliver balcony / unbookable roof → hygiene capped |
| `building_type` | string | e.g. `luxury_tower` (dealbreaker condition) |
| `amenity_premium_over_comps` | number | $/mo above comps (dealbreaker ≥ $1,000) |
| `relying_on_common_space` | bool | Lounge/roof as substitute living room (dealbreaker) |
| `intends_to_host` | bool | Gates the 5-flight walk-up dealbreaker |

Any extra keys are ignored. Lookup strings are slug-normalized on both
sides, so `"Hell's Kitchen"`, `hells_kitchen`, and `hells kitchen` all match.

## Example

```json
{
  "name": "Chelsea alcove, W 20th",
  "neighborhood": "Chelsea",
  "layout": "alcove_1br",
  "sqft": 560,
  "price": 5200,
  "living_situation": "solo",
  "bed_in_sightline": false,
  "seats_four_without_bed": true,
  "floor_level": "elevator",
  "subway_lines_nearby": 3,
  "laundry_in_building": true,
  "quiet_enough_to_sleep": true,
  "packages_safe": true,
  "bookable_roof": true
}
```

→ `92`

## Scoring mechanics

1. **Location (30)** — fixed per-neighborhood score from the YAML band
   tables (25–30 Tier 1 … 0–9 dealbreaker neighborhoods).
2. **Unit function (30)** — per-layout score, then demotions:
   `bed_in_sightline` → capped at the studio band ("bed is the living room");
   sub-500-sqft `large_zoned_studio` → small-studio points;
   `seats_four_without_bed: false` → hosting-fail cap.
3. **Control (15)** — solo 15 / known roommate 10 / new roommates 0.
4. **Access (15)** — additive: elevator-or-low-walk-up 6 + multi-line-or-
   walk-anchor 5 + laundry in-building 4, clamped to 15.
5. **Hygiene / outdoor (10)** — quiet 3 + packages 2 + usable balcony *or*
   bookable roof 3; `amenity_trap` caps the category at 5.

**Dealbreakers** never zero the score: any matched rule caps the total at
`dealbreaker_cap` (49) and is listed by name in `--explain` output. Rules
are data-driven (`when:` mappings in the YAML); a condition on a missing
field never matches.

All numbers above are defaults from the YAML — retune there, not in code.
