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
# Single listing (JSON object) -> prints the score
uv run python src/apartment_scorer/score.py listing.json
uv run python src/apartment_scorer/score.py listing.json --explain   # full JSON breakdown
uv run python src/apartment_scorer/score.py listing.json --config my_scorecard.yaml

# Batch (JSON array, e.g. a StreetEasy search-result export) -> ranked table
uv run python src/apartment_scorer/score.py data/listings/chelsea-XXX/listings.json
uv run python src/apartment_scorer/score.py listings.json --json out/scores.json
```

Exit codes: `0` = scored, `2` = unreadable/invalid JSON or malformed config.
In single mode, an unknown `layout`/`living_situation` is also exit 2; in
batch mode those score 0 with a warning instead (see below).

A worked single-listing example lives at
[`tests/data/test_listing.json`](../../tests/data/test_listing.json)
(Chelsea Tower #18A, from a real StreetEasy listing).

## Batch mode

When the input JSON root is an **array**, the scorer switches to batch mode.
This is designed for StreetEasy **search-result exports** (e.g. from the
Apify StreetEasy scraper) — one object per listing with keys like `address`,
`price`, `bedrooms`, `squareFeet`, `propertyType`, `neighborhood`, `url`.

```bash
uv run python src/apartment_scorer/score.py data/listings/chelsea-DPMjSjjxVAsilXG9J/listings.json
```

```
SCORE  ADDRESS                                    PRICE BD  LAYOUT             FLAGS
------------------------------------------------------------------------------------
   34  311 West 19th Street #1                   $8,250  3  -                  ~partial
   28  100 West 26th Street #18A                 $6,295  1  -                  ~partial
   ...
42 scored · 8 deduped
```

### How the export maps onto the schema

`normalize_search_listing()` translates each export entry:

| Export key | Schema field | Notes |
|---|---|---|
| `address` | `name` + `unit` | unit split off (`#PH308`→`PH308`); floor parsed (`S20M`→20, `WEST-TOWER-11B`→None) |
| `price` | `price` | |
| `bedrooms` | `bedrooms` (+`rooms`) | `0` bedrooms → `rooms` omitted |
| `squareFeet` | `sqft` | `0` (unknown) → omitted |
| `propertyType` | `building_type` | |
| `neighborhood` | `neighborhood` | `"West Chelsea"` scores 26 |
| `yearBuilt` | `building_year_built` | `0` → omitted |
| `url` | `source_url` | query params ignored |
| `amenities` | `amenities` | `[]` → omitted |

Zero/empty export values mean "unknown" and are **omitted**, so derived
flags stay false and no dealbreaker fires on absent facts. Any
**scorer-native field already present** on an entry (e.g. a hand-completed
`layout`, `living_situation`, `bed_in_sightline`, or a richer scraper's
`amenities`) passes straight through, so you can enrich individual rows.

### Null-safe scoring (why every export row gets a score)

Search exports never carry `layout` or `living_situation`. Rather than drop
those rows, batch mode scores them **null-safely**: the missing category
(`unit_function` and/or `control`) scores **0 with a warning**, and the row
still ranks on its deterministic categories (location, access, hygiene).
Rows with warnings are flagged `~partial` in the table; rows with
dealbreakers get `!name`. To fully score a row, add `layout` /
`living_situation` (and ideally `amenities`) to its export entry — they pass
through.

### Dedupe & ranking

Duplicates (the same unit re-exported with `?featured=1` / `?infeed=1`
URLs) are collapsed on **address + price**; the footer reports
`N scored · M deduped`. Rows sort by score descending. `--json PATH` writes
the full per-listing breakdowns (including `warnings`) to a file;
`--explain` prints the same array to stdout.

## Input schema

Two kinds of fields:

- **Deterministic** — copied verbatim from the listing page's structured
  data (for StreetEasy, the `self.__next_f` Flight JSON or the JSON-LD
  block). No judgment involved.
- **Manual** — tour-test and preference judgments the page can't tell you.
  All optional; when absent the corresponding rule simply doesn't fire.

### Deterministic fields (from the listing page)

| Field | Type | StreetEasy source (`__next_f` path) |
|---|---|---|
| `neighborhood` | string | `building.area.name` |
| `price` | number | `listing.pricing.price` (context only — not scored) |
| `sqft` | number | `listing.propertyDetails.livingAreaSize` |
| `bedrooms` | number | `listing.propertyDetails.bedroomCount` |
| `rooms` | number | `listing.propertyDetails.roomCount` |
| `unit` | string | `listing.propertyDetails.address.displayUnit` — floor parsed (`#18A`→18, `#3R`→3, `PH2`→2, `PH`→`building_floor_count`) |
| `amenities` | string[] | `listing.propertyDetails.amenities.list` + `sharedOutdoorSpaceTypes` — closed SCREAMING_SNAKE_CASE enum (JSON-LD `amenityFeature` uses the same names in snake_case; both work) |
| `building_floor_count` | number | `building.floorCount` |
| `building_year_built` | number | `building.yearBuilt` |
| `transit_stations` | object[] | `building.nearby.transitStations` (`{name, routes[], distance}`) |
| `building_type` | string | `building.aboutBuildingType` (e.g. `"Rental building"` — informational only; see class heuristic below) |
| `policies` | string[] | `building.policies.list` |
| `floor_plan_count` | number | `len(listing.media.floorPlans)` |

### Derived automatically (do not hand-enter)

`enrich_listing()` fills these from the deterministic fields before scoring
(explicit values on the listing always win):

| Derived field | Rule |
|---|---|
| `laundry_in_building`, `has_elevator`, `has_doorman`, `has_gym`, `has_roof_deck`, `has_shared_outdoor` | `amenities` ∩ `amenity_flags` map in the YAML |
| `distinct_transit_routes` | union of `transit_stations[].routes` |
| `unit_floor` | parsed from `unit` |
| `effective_building_class` | `luxury_tower` when `building_floor_count ≥ 20` **and** `building_year_built ≥ 1990` **and** `has_doorman` **and** `has_gym`; else `elevator_building` (has elevator) / `walkup` (floor count known) / `unknown`. Thresholds live in `building_class.luxury_tower` in the YAML. |

### Manual fields (tour test / judgment)

| Field | Type | Effect |
|---|---|---|
| `layout` **(required)** | string enum | `true_1br`, `alcove_1br` / `junior_1br`, `large_zoned_studio` (needs `sqft` ≥ 500), `small_open_studio` / `open_studio`, `railroad`, `fake_1br` |
| `living_situation` **(required)** | string enum | `solo`, `known_roommate`, `new_roommates` |
| `bed_in_sightline` | bool | `true` caps unit score at the studio band ("bed is the living room") |
| `seats_four_without_bed` | bool | `false` caps unit score (hosting fail) |
| `walk_to_work_anchor` | bool | ≤15-min walk to Hudson Yards / Union Square; transit-point alternative |
| `quiet_enough_to_sleep` | bool | Hygiene points; `null` = unknown, no points |
| `packages_safe` | bool | Explicit override; `null`/absent falls back to the derived `has_doorman` flag |
| `usable_balcony` / `bookable_roof` | bool | Hygiene points (either); `bookable_roof: null` falls back to derived `has_roof_deck` |
| `amenity_trap` | bool | Trophy finishes / sliver balcony / unbookable roof → hygiene capped at 5 |
| `effective_building_class` | string | Manual override of the class heuristic |
| `amenity_premium_over_comps` | number | $/mo above comps (dealbreaker ≥ $1,000) |
| `relying_on_common_space` | bool | Lounge/roof as substitute living room (dealbreaker) |
| `intends_to_host` | bool | Gates the 5-flight walk-up dealbreaker |

`layout` stays manual on purpose: StreetEasy's `bedroomCount`/`roomCount`
can't distinguish a true 1BR from an alcove with a door or a
pressurized-wall "1BR". Decision procedure (the tour test):

1. `bedrooms ≥ 1` **and** a real door on the bedroom **and** a sitting zone
   → `true_1br`
2. No separate bedroom, but a distinct sleeping alcove with a wall/door →
   `alcove_1br`
3. Studio, `sqft ≥ 500`, bed zoned away from the entry →
   `large_zoned_studio`
4. Anything else (bed in the sightline, tiny open room, railroad with no sit
   space, pressurized wall) → `small_open_studio` / `railroad` / `fake_1br`

If `bed_in_sightline: true`, the unit scores as a studio no matter which
`layout` you picked.

Lookup strings everywhere are slug-normalized on both sides, so
`"Hell's Kitchen"`, `hells_kitchen`, and `hells kitchen` all match.

## Scoring mechanics

1. **Location (30)** — fixed per-neighborhood score from the YAML band
   tables (25–30 Tier 1 … 0–9 dealbreaker neighborhoods). Unknown names fall
   back to `location.default` with a stderr warning.
2. **Unit function (30)** — per-layout score, then demotions:
   `bed_in_sightline` → capped at the studio band; sub-500-sqft
   `large_zoned_studio` → small-studio points;
   `seats_four_without_bed: false` → hosting-fail cap.
3. **Control (15)** — solo 15 / known roommate 10 / new roommates 0.
4. **Access (15)** — additive: elevator-or-low-walk-up (has `ELEVATOR` or
   `unit_floor ≤ 3`) 6 + multi-line-or-walk-anchor
   (`distinct_transit_routes ≥ 2` or anchor) 5 + laundry in-building 4,
   clamped to 15.
5. **Hygiene / outdoor (10)** — quiet 3 + packages-safe (doorman) 2 + usable
   balcony *or* bookable/derived roof 3; `amenity_trap` caps the category at 5.

**Dealbreakers** never zero the score: any matched rule caps the total at
`dealbreaker_cap` (49) and is listed by name in `--explain` output. Rules
are data-driven (`when:` mappings in the YAML) evaluated against the
**enriched** listing — e.g. the LIC/Downtown-Brooklyn tower rule matches on
`effective_building_class: luxury_tower` (derived, or manually overridden),
and the 5-flight walk-up rule matches on derived `unit_floor ≥ 5` +
`has_elevator: false`. A condition on a missing field never matches.

All numbers above are defaults from the YAML — retune there, not in code.

## StreetEasy extraction (future)

`parse_streeteasy_html()` is a documented stub. When implemented it will map
the Flight payload fields in the table above onto a listing JSON
automatically; the manual fields stay manual. Note that plain HTTP fetches
of streeteasy.com are PerimeterX-blocked — pages must be saved from a
browser session first.
