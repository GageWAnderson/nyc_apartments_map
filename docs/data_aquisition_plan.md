# Data Acquisition Plan

Concrete acquisition plan for every metric in
`docs/consolidated_metrics_table.md`. It maps each metric to a primary data
source, an access method, license/cost, refresh cadence, the **join key** used
to attach it to the pipeline, and the **integration point** in this codebase.

## How metrics plug into the existing architecture

The pipeline has two ingestion surfaces — every metric below routes into one
of them:

1. **Listing-level → `DatasetLoader` subclass** (rows on
   `data/processed/normalized.parquet`, conforming to `COMMON_SCHEMA`). Used
   when the source is a per-listing dataset (rents, amenities, building
   attributes attached to an address). Listing-derived aggregates
   (`median_price`, `listing_count`, …) are already computed per-NTA in
   `processing/aggregate.py`.
2. **Neighborhood-level → `nta_indicators.parquet` columns** (one row per
   2020 NTA, keyed by `nta_code`). `processing/aggregate.py` explicitly
   documents this as the extension point for "pre-aggregated sources
   (Furman/ACS) and point sources (311/POIs/crime) … via crosswalk / spatial
   joins." All non-listing metrics below land here.

A third, derived surface is **computed metrics** (commute/walk routing) —
produced offline from GTFS + a router and written as NTA-level columns, not
fetched from a third-party API.

> Join-key convention: neighborhood metrics resolve to `nta_code` (2020 DCP
> NTA). Sources published at other geographies (PUMA, ZIP, census tract) are
> crossed to NTA via DCP's published geocrosswalks (`data/raw/crosswalks/`).
> Building-level sources (PLUTO, DOB, HPD) are geocoded and either matched to
> listings by BIN/address or aggregated to NTA via point-in-polygon.

## Access constraints to know up front

- **No env-var fields exist in `Settings` yet** — only path fields
  (`config.py`, `extra="ignore"`). Any source needing an API key requires
  **adding fields to `Settings`** (e.g. `census_api_key`, `google_api_key`,
  `walkscore_api_key`) before the loader can read them from `.env`.
- **`fetch` is existence-only cache validation** — re-downloads only on
  `--force`. Cadence below is advisory; nothing auto-refreshes.
- **Socrata (NYC Open Data) endpoints** use `SODA2` API with `SoQL` query
  params; large tables should be downloaded as bulk CSV/GeoJSON export rather
  than paged for stability.
- **StreetEasy has no public API.** Asking-rent data is obtainable only via
  (a) Zillow-published aggregate datasets, (b) NYU Furman Center derived
  tables, or (c) scraping (ToS-restricted — out of scope for this repo).

---

## 1. Commute Length

| Metric | Primary source | Access | Format | Auth / cost | License | Cadence | Join key | Integration |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Door-to-door rush-hour commute time to Hudson Yards | MTA GTFS + self-hosted router (OSRM or OpenTripPlanner) | Bulk download | ZIP of `.txt` (GTFS) | Free | CC / public | Quarterly (GTFS) | `nta_code` (origin = NTA centroid) | Computed → `nta_indicators` column |
| Door-to-door commute (alternative) | Google Maps Distance Matrix API | REST API | JSON | API key, paid per call | Google ToS | On demand | `nta_code` | Computed → `nta_indicators` (optional, cost-gated) |
| Walking time to key subway | MTA subway entrances (NYC Open Data) + OSRM walking | Socrata + router | GeoJSON | Free | NYC Open Data | Monthly | `nta_code` | Computed → `nta_indicators` |
| Transit Score | Walk Score API | REST API | JSON | API key, paid | Walk Score ToS | On demand | lat/lon → NTA | `nta_indicators` |
| Transfers + service frequency | MTA GTFS schedules | Bulk download | GTFS `.txt` | Free | public | Quarterly | `nta_code` | Computed → `nta_indicators` |

**Notes:** Compute commute/walk metrics **offline** from GTFS + a free
router; reserve paid APIs (Google, Walk Score) as optional cross-checks. Run
the router once per NTA centroid (or per subway stop) and cache results in
`nta_indicators` — do not call live routing per map render.

## 2. Neighborhood Character

| Metric | Primary source | Access | Format | Auth / cost | License | Cadence | Join key | Integration |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Walk Score / Bike Score | Walk Score API | REST API | JSON | API key, paid | Walk Score ToS | On demand | lat/lon → NTA | `nta_indicators` |
| Crime / Safety Index | NYPD Complaints (NYC Open Data, Socrata) | Socrata API / bulk | CSV | Free | NYC Open Data | Weekly | point → NTA PIP | Aggregate to `nta_indicators` |
| Young professional demographics | Census ACS (5-year) via Census API + DCP ACS-NTA tables | API + bulk | CSV/JSON | API key (free) | public | Annual (5-yr release) | `nta_code` (DCP NTA ACS) | `nta_indicators` |
| Amenity & park density | NYC Parks Open Data + OpenStreetMap (Overpass) | Socrata + Overpass API | GeoJSON | Free | ODbL / NYC Open Data | Parks: monthly; OSM: on demand | point → NTA PIP | Aggregate to `nta_indicators` |
| Nuisance complaints density | NYC 311 (NYC Open Data, Socrata) | Socrata API / bulk | CSV | Free | NYC Open Data | Daily | point → NTA PIP | Aggregate to `nta_indicators` |

**Notes:** Census ACS at NTA geography is published by DCP ("Bytes of the Big
Apple" / ACS-NTA tables) — prefer these over tract-level rollups to avoid a
crosswalk step. Tables needed: age (B01001/B01002), tenure/renter (B25003),
median income (B19013). 311 and crime are point datasets; aggregate to NTA
via the same point-in-polygon routine used in `processing/enrich.py`.

## 3. Cost of Rent

| Metric | Primary source | Access | Format | Auth / cost | License | Cadence | Join key | Integration |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Median rent (studio / 1-BR) | NYU Furman Center NYC Housing Data | Bulk download | CSV/XLSX | Free (academic) | Furman terms | Annual | NTA / PUMA → NTA | `nta_indicators` (or new loader) |
| Median rent (alternative) | StreetEasy / Zillow | none (no public API) | — | — | ToS-restricted | — | — | Documented as **blocked**; see Risks |
| Rent per square foot | Listing sqft from a listings loader (future) | per loader | — | per loader | per loader | per loader | `listing_id` | `DatasetLoader` row (`raw.sqft`) → aggregate |
| Rent trends & inventory | Furman Center + StreetEasy market reports (published) | PDF/CSV download | PDF/CSV | Free | republish-limited | Monthly/Annual | NTA / neighborhood name | `nta_indicators` (manual ingest) |

**Notes:** The honest path for rent at NTA granularity is **Furman Center**
(pre-aggregated, clean, free for research). True per-listing rent + sqft
requires a listings feed (StreetEasy etc.) which is the long pole — see
Risks. Once a listings loader exists, `median_price` / `median_price_per_bed`
are already emitted by `aggregate.py`; rent-per-sqft is a one-line addition
once `raw.sqft` is populated by a loader.

## 4. Proximity to Nightlife

| Metric | Primary source | Access | Format | Auth / cost | License | Cadence | Join key | Integration |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Bar / nightlife venue density | NYS Liquor Authority active licenses | Bulk download | CSV | Free | public | Monthly | point → NTA PIP | Aggregate to `nta_indicators` |
| Bar / nightlife venue density (alt) | OpenStreetMap (`amenity=bar/pub/nightclub`) via Overpass | Overpass API | GeoJSON | Free | ODbL | On demand | point → NTA PIP | Aggregate to `nta_indicators` |
| Distance to nightlife clusters | Derived from the venue dataset above + OSRM | Computed | — | Free | — | On demand | `nta_code` | Computed → `nta_indicators` |

**Notes:** Prefer NYS Liquor Authority for an authoritative, license-level
venue list; OSM is a good free supplement for late-night restaurants. Yelp /
Google Places are listed in the metrics table but are paid/limited APIs and
rate-restricted — treat as optional cross-checks, not primary sources.

## 5. Building Quality

| Metric | Primary source | Access | Format | Auth / cost | License | Cadence | Join key | Integration |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Building age / era mix | DCP PLUTO (`YearBuilt`) | Bulk download | CSV/Shapefile | Free | NYC Open Data | Annual | BIN / tax lot → point PIP | Building-level loader → NTA aggregate |
| Building age (alt) | DOB job filings / permits | Socrata API | CSV | Free | NYC Open Data | Daily | BIN | Building-level loader |
| Amenity prevalence | Listings feed (future) `raw` dict | per loader | — | per loader | per loader | per loader | `listing_id` | `DatasetLoader` row → aggregate |
| Violations & health score | HPD Housing Maintenance Code Violations | Socrata API / bulk | CSV | Free | NYC Open Data | Daily | BIN / point PIP | Building-level loader → NTA aggregate |
| Violations (supplement) | DOB Violations (NYC Open Data) | Socrata API | CSV | Free | NYC Open Data | Daily | BIN | Building-level loader |
| Management / condition proxies | NYC 311 (heat, bedbug, rodent complaints) | Socrata API | CSV | Free | NYC Open Data | Daily | point → NTA PIP | Aggregate to `nta_indicators` |

**Notes:** PLUTO is the anchor for building-level data — it has `YearBuilt`,
`BldgClass`, BIN, and a geometry, so it can both supply "era mix" and serve as
the BIN crosswalk for HPD/DOB violations. ApartmentRatings / DwellCheck /
OpenStoop are third-party aggregators with no public API; they appear in the
metrics table as proposed sources but are **out of scope** for first-pass
acquisition (scraping ToS / paid). The composite "health score" should be
**computed locally** from HPD/DOB/311 counts rather than fetched.

## 6. Overall / Composite

| Metric | Primary source | Access | Format | Auth / cost | License | Cadence | Join key | Integration |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Livability / address score | **Computed locally** from the columns above | Computed | — | Free | — | On demand | `nta_code` | Computed → `nta_indicators` |
| Livability (alternative) | DwellCheck / similar aggregator | none public | — | Paid / ToS | — | — | — | Out of scope (see Risks) |
| Neighborhood market indicators | NYU Furman Center (State of NYC's Housing) | Bulk download | CSV/XLSX | Free (academic) | Furman terms | Annual | NTA / PUMA | `nta_indicators` |

**Notes:** There is no free, openly-licensed "livability score" feed.
DwellCheck et al. are paid/scrape-gated. The robust path is to **define our
own composite** as a weighted sum of normalized columns already in
`nta_indicators` (crime, 311, transit, rent burden, violations) — fully
reproducible, no external dependency. Document the weighting in
`docs/` so it's auditable.

---

## Phased implementation

**Phase 1 — Free, bulk, NTA-keyed (no API keys, no new Settings fields):**
Census ACS (DCP NTA tables), NYC 311, NYPD complaints, NYS Liquor Authority,
PLUTO, HPD/DOB violations, MTA GTFS + OSRM routing, OSM/Overpass nightlife,
NYC Parks, Furman Center. These cover ~15 of 20 metrics and require only
new loaders + a `nta_indicators` join step.

**Phase 2 — Free-API keyed (add `Settings` env fields):** Census API key
(free, for convenience over bulk), Walk Score (paid), optional Google
Distance Matrix (paid). Gated behind new `Settings` fields + `.env`.

**Phase 3 — Blocked / out of scope:** StreetEasy per-listing rents, paid
aggregators (DwellCheck, OpenStoop), ApartmentRatings scraping. Document as
gaps; revisit if a licensed feed becomes available.

## New loaders to create

Following the copy-`_template.py` convention in
`src/nyc_apartments_map/datasets/`:

- `census_acs_nta.py` — ACS demographics at NTA (Phase 1)
- `nyc_311.py` — 311 complaints (point source → NTA aggregate)
- `nypd_complaints.py` — crime (point source → NTA aggregate)
- `nys_liquor.py` — nightlife venue licenses (point → NTA aggregate)
- `pluto.py` — building age/class (building-level, anchors BIN crosswalk)
- `hpd_violations.py` / `dob_violations.py` — violations (building-level)
- `furman_housing.py` — pre-aggregated rent/market indicators (NTA)
- `mta_gtfs.py` + a `processing/commute.py` step — computed transit metrics

Each loader's `clean()` either emits `COMMON_SCHEMA` rows (for listing /
building-level sources) or writes a per-NTA parquet under
`data/processed/indicators/<name>.parquet`, merged onto `nta_indicators` by a
new `processing/merge_indicators.py` step keyed on `nta_code`.

## Risks & open questions

- **StreetEasy / per-listing rents are the long pole.** No public API;
  scraping violates ToS. Median rent at NTA is achievable via Furman, but
  listing-level rent/sqft/amenities is blocked until a licensed feed appears.
  Decide: accept Furman-only rent metrics, or pursue a data license.
- **Point-in-polygon for crime/311/POIs** needs the NTA boundary file
  (`data/raw/ntas/ntas.json`). Currently enrichment **skips with a warning**
  if absent — Phase 1 loaders must fail loudly (or fetch the boundary file
  first) rather than silently emit NaN columns.
- **Geocrosswalks** (PUMA→NTA, ZIP→NTA) are published by DCP; add a one-time
  `data/raw/crosswalks/` fetch rather than hand-mapping.
- **API keys** require extending `Settings` beyond path fields — a
  precedent-setting change; confirm before adding the first paid-key field.
- **Composite livability weighting** is a product decision, not a data
  decision — needs a written definition before implementation.
