# Data Collection Estimates by Source

Per-source collection estimates for the metrics in
`docs/consolidated_metrics_table.md`. The map is **neighborhood-level**
(keyed by 2020 DCP NTA, `nta_code`), not listing-level — so individual
apartment listings are out of scope and rent/amenity metrics are sourced from
pre-aggregated or point datasets rolled up to NTA.

All sizes/times are **order-of-magnitude estimates** to be confirmed on first
fetch. "Fetch time" assumes a typical residential connection from NYC;
Socrata times assume **bulk CSV/GeoJSON export**, not paged API calls (paging
at 1000 rows/call is ~10× slower and should be avoided for full pulls).

## Sources removed from consideration

Removed because they require scraping, have no public API, or their ToS
forbid automated collection. The metric is **not** dropped — it's served by a
listed alternative below.

| Removed source | Reason | Metric(s) affected | Replacement |
| --- | --- | --- | --- |
| StreetEasy | No public API; scraping violates ToS | Median rent, rent/sqft, amenity prevalence, rent trends | NYU Furman Center (NTA aggregates) |
| Zillow | No public API; ToS forbids scraping | Median rent, rent/sqft | Furman Center / Census ACS (gross rent) |
| Yelp | API ToS restricts bulk venue extraction & storage; scraping forbidden | Nightlife/amenity density | OpenStreetMap + NYS Liquor Authority |
| Google Places | Paid, rate-limited, ToS restricts caching | Amenity/nightlife density | OpenStreetMap (Overpass) |
| DwellCheck | No public API; paid; scraping forbidden | Livability score, building health | Computed locally from HPD/DOB/311 |
| OpenStoop | No public API | Building health | Computed locally from HPD/DOB/311 |
| ApartmentRatings | No public API; scraping forbidden | Management/condition proxies | NYC 311 complaint patterns |
| Citymapper | No public API | Commute time | MTA GTFS + OSRM/OTP routing |
| SafeRoute | No public API; unclear licensing | Crime/safety | NYPD complaints (NYC Open Data) |
| ACRIS "comps" | Raw ACRIS is open, but *comps* are realtor-derived/paid | Rent per sqft | Dropped at neighborhood level (Furman covers rent) |

**Net effect:** every metric in the table remains coverable via free,
ToS-clean sources, with two optional paid APIs (Walk Score, Google Distance
Matrix) that can be skipped without losing coverage.

---

## Free, bulk / open-data sources (Phase 1)

### MTA GTFS (static schedules)

| Field | Estimate |
| --- | --- |
| Coverage | Subway + bus static timetables |
| Size | ~80–120 MB ZIP; ~400 MB uncompressed |
| Fetch method | Bulk ZIP from MTA developer resources |
| Fetch time | 1–3 min |
| Storage | `data/raw/mta_gtfs/` ~500 MB |
| Refresh cadence | Quarterly (or on GTFS schedule change) |
| API limits / requests | None (bulk download) |
| Cost | Free |
| License | MTA open data terms |
| Join key | Computed → NTA centroid origin; subway stops by lat/lon |
| Metrics served | Rush-hour commute to Hudson Yards; walking time to subway; transfers + frequency |

### MTA subway entrances (NYC Open Data)

| Field | Estimate |
| --- | --- |
| Size | ~1,500 entrances; ~1–2 MB GeoJSON |
| Fetch method | Socrata bulk GeoJSON |
| Fetch time | < 30 sec |
| Storage | `data/raw/mta_subway_entrances/` ~5 MB |
| Refresh cadence | Monthly |
| API limits | None meaningful (small) |
| Cost | Free |
| License | NYC Open Data |
| Join key | lat/lon → point-in-polygon → NTA |
| Metrics served | Walking time to key subway |

### NYPD Complaints Historic (NYC Open Data, Socrata)

| Field | Estimate |
| --- | --- |
| Coverage | All reported complaints, current + historic |
| Size | ~7M rows historic; ~1.5–2.5 GB CSV; YTD slice ~250–500 MB |
| Fetch method | Socrata bulk CSV export (preferred) or `$limit` paging |
| Fetch time | 15–40 min (full historic); 3–8 min (YTD only) |
| Storage | `data/raw/nypd_complaints/` ~2.5 GB full, ~500 MB YTD |
| Refresh cadence | Weekly (YTD slice is sufficient for neighborhood rates) |
| API limits | Socrata: 1000 rows/page if using API; **use bulk export** to avoid ~10× slowdown |
| Cost | Free |
| License | NYC Open Data |
| Join key | complaint lat/lon → point-in-polygon → NTA |
| Metrics served | Crime / Safety Index (violent + property rates per capita) |

### NYC 311 Service Requests (NYC Open Data, Socrata)

| Field | Estimate |
| --- | --- |
| Coverage | All 311 requests; filter to noise/trash/rodent/heat/bedbug complaint types |
| Size | ~30M rows total (~8–12 GB); **filtered last 12 mo** ~1–2 GB |
| Fetch method | Socrata bulk with SoQL complaint-type filter + date range |
| Fetch time | 30–60 min (full); 5–15 min (filtered 12-mo slice) |
| Storage | `data/raw/nyc_311/` ~2 GB (filtered) |
| Refresh cadence | Monthly (rates change slowly at NTA level) |
| API limits | 1000 rows/page via API; **bulk export with SoQL filter** required |
| Cost | Free |
| License | NYC Open Data |
| Join key | request lat/lon → point-in-polygon → NTA |
| Metrics served | Nuisance complaints density; management/condition proxies (heat, bedbug, rodent) |

### U.S. Census ACS 5-year (via DCP NTA tables)

| Field | Estimate |
| --- | --- |
| Coverage | ACS tables published pre-aggregated to 2020 NTA by DCP ("Bytes of the Big Apple") |
| Size | ~250 NTAs × ~10 needed tables; ~10–30 MB total |
| Fetch method | Bulk CSV from DCP; or Census API with tract→NTA crosswalk |
| Fetch time | 2–5 min |
| Storage | `data/raw/census_acs_nta/` ~50 MB |
| Refresh cadence | Annual (ACS 5-year release each December) |
| API limits | Census API: free key, 500 queries/min — trivial for NTA-level |
| Cost | Free (Census API key optional) |
| License | Public domain (Census) / DCP open |
| Join key | `nta_code` (direct) |
| Metrics served | Young professional demographics (% 25–34, median age, % renters, median income); population denominator for per-capita rates |

### NYC Parks properties (NYC Open Data)

| Field | Estimate |
| --- | --- |
| Size | ~1,000–2,000 features; ~3–5 MB GeoJSON |
| Fetch method | Socrata bulk GeoJSON |
| Fetch time | < 30 sec |
| Storage | `data/raw/nyc_parks/` ~10 MB |
| Refresh cadence | Monthly |
| API limits | None meaningful |
| Cost | Free |
| License | NYC Open Data |
| Join key | park geometry → intersect NTA |
| Metrics served | Amenity & park density |

### OpenStreetMap (Overpass API)

| Field | Estimate |
| --- | --- |
| Coverage | `amenity=bar/pub/nightclub/restaurant/cafe` + `shop=supermarket/convenience` within NYC bbox |
| Size | ~30K–60K features; ~5–15 MB GeoJSON |
| Fetch method | Overpass QL query (single bbox, polite user-agent) |
| Fetch time | 1–3 min per query (be polite: max ~2 req/min) |
| Storage | `data/raw/osm_pois/` ~20 MB |
| Refresh cadence | On demand / quarterly |
| API limits | Overpass public instance: ~10K elements/query, ~2 req/min etiquette; run off-peak or self-host if heavy |
| Cost | Free |
| License | ODbL (attribution required; share-alike) |
| Join key | POI lat/lon → point-in-polygon → NTA |
| Metrics served | Nightlife venue density; amenity/grocery density; distance to nightlife clusters |

### NYS Liquor Authority active licenses

| Field | Estimate |
| --- | --- |
| Coverage | Active liquor licenses statewide; filter to NYC counties |
| Size | ~80K statewide, ~35–45K NYC; ~15–25 MB CSV |
| Fetch method | NYS Open Data bulk CSV |
| Fetch time | 1–3 min |
| Storage | `data/raw/nys_liquor/` ~30 MB |
| Refresh cadence | Monthly |
| API limits | None (bulk) |
| Cost | Free |
| License | NYS public records |
| Join key | license premise address → geocode → NTA (need to geocode ~40K addresses) |
| Metrics served | Bar / nightlife venue density (authoritative license list) |
| Note | Requires geocoding premise addresses — use NYC Planning LION / GeoSupport or NYC Open Data geocoder, not Google (ToS) |

### DCP PLUTO (tax lot / building attributes)

| Field | Estimate |
| --- | --- |
| Coverage | ~850K tax lots NYC with `YearBuilt`, `BldgClass`, BIN, geometry |
| Size | ~250 MB CSV; ~1 GB shapefile |
| Fetch method | DCP bulk ZIP |
| Fetch time | 5–10 min |
| Storage | `data/raw/pluto/` ~1 GB |
| Refresh cadence | Annual |
| API limits | None (bulk) |
| Cost | Free |
| License | NYC Open Data |
| Join key | lot geometry centroid → NTA; BIN anchors HPD/DOB crosswalk |
| Metrics served | Building age / era mix (pre-war vs post-war vs new); BIN crosswalk for violations |

### HPD Housing Maintenance Code Violations (NYC Open Data, Socrata)

| Field | Estimate |
| --- | --- |
| Coverage | Open + closed violations per building |
| Size | ~4M rows historic; ~300–500 MB; **open violations only** ~30–80 MB |
| Fetch method | Socrata bulk CSV (filter to open or last 12 mo) |
| Fetch time | 15–30 min (full); 3–8 min (filtered) |
| Storage | `data/raw/hpd_violations/` ~500 MB full, ~80 MB filtered |
| Refresh cadence | Daily source; monthly pull is sufficient |
| API limits | 1000 rows/page via API; **bulk export** |
| Cost | Free |
| License | NYC Open Data |
| Join key | BIN → PLUTO → NTA (or building lat/lon → NTA) |
| Metrics served | Violations & health score (HPD component) |

### DOB Violations (NYC Open Data, Socrata)

| Field | Estimate |
| --- | --- |
| Coverage | DOB-issued violations per building |
| Size | ~2M rows; ~150–250 MB |
| Fetch method | Socrata bulk CSV |
| Fetch time | 10–20 min |
| Storage | `data/raw/dob_violations/` ~250 MB |
| Refresh cadence | Daily source; monthly pull sufficient |
| API limits | 1000 rows/page via API; **bulk export** |
| Cost | Free |
| License | NYC Open Data |
| Join key | BIN → PLUTO → NTA |
| Metrics served | Violations & health score (DOB component) |

### NYU Furman Center — NYC Housing & Neighborhood Data

| Field | Estimate |
| --- | --- |
| Coverage | Pre-aggregated housing/neighborhood indicators at NTA or PUMA |
| Size | ~250 NTAs × ~30 indicators; ~2–10 MB CSV/XLSX |
| Fetch method | Manual/bulk download from Furman data portal |
| Fetch time | 2–5 min |
| Storage | `data/raw/furman_housing/` ~20 MB |
| Refresh cadence | Annual (State of NYC's Housing report) |
| API limits | None (bulk) |
| Cost | Free (academic/research use) |
| License | Furman terms — **check redistribution rights before republishing columns**; safe for derived/analysis use |
| Join key | `nta_code` (or PUMA → NTA crosswalk) |
| Metrics served | Median rent (studio/1-BR); rent trends & inventory; neighborhood market indicators (rent burden, conditions); demographic supplement |

### DCP geocrosswalks (PUMA↔NTA, ZIP↔NTA, tract↔NTA)

| Field | Estimate |
| --- | --- |
| Size | < 5 MB per crosswalk |
| Fetch method | DCP "Bytes of the Big Apple" bulk |
| Fetch time | < 1 min |
| Storage | `data/raw/crosswalks/` ~10 MB |
| Refresh cadence | Stable (2020 geographies) — one-time |
| Cost | Free |
| Join key | defines the NTA joins themselves |
| Metrics served | Enables Furman (PUMA) and any tract-level Census fallback to join on `nta_code` |

---

## Optional paid-API sources (Phase 2 — skip without losing coverage)

### Walk Score API

| Field | Estimate |
| --- | --- |
| Coverage | Walk Score + Transit Score + Bike Score per lat/lon |
| Requests | 1 NTA centroid per call × ~250 NTAs = **~250 calls** |
| Fetch time | 5–10 min (rate-limited) |
| Storage | `data/raw/walkscore/` < 1 MB |
| Refresh cadence | On demand (scores change slowly — annual is fine) |
| API limits | Per-plan rate limit; requires key |
| Cost | Paid per call (~$0.03–0.05 historically, tiered) — ~$10–15 for full NTA set |
| License | Walk Score ToS (caching allowed with attribution) |
| Join key | lat/lon → NTA |
| Metrics served | Walk Score / Bike Score; Transit Score |
| Settings change needed | Add `walkscore_api_key` to `Settings` |

### Google Maps Distance Matrix API (optional commute cross-check)

| Field | Estimate |
| --- | --- |
| Requests | 250 NTA origins × 1 destination = 250 elements (one call) |
| Fetch time | < 1 min |
| Refresh cadence | On demand |
| API limits | 1000 elements/call, 1000 elements/sec |
| Cost | Free tier 100 elements/day; beyond that ~$0.01/element → ~$2.50 for full set |
| License | Google ToS (no long-term caching of routes — re-fetch) |
| Join key | NTA centroid → NTA |
| Metrics served | Rush-hour commute (cross-check for GTFS routing) |
| Settings change needed | Add `google_api_key` to `Settings` |
| Recommendation | **Skip** — GTFS + OSRM/OTP gives the same metric for free with no caching restrictions |

---

## Computed (not fetched) metrics

These are derived locally from the sources above — no external acquisition.

| Metric | Inputs | Compute step |
| --- | --- | --- |
| Rush-hour commute to Hudson Yards | MTA GTFS | `processing/commute.py` — OTP/OSRM run per NTA centroid, cached to `nta_indicators` |
| Walking time to subway | MTA entrances + OSM walk network | OSRM table query per NTA centroid |
| Transfers + peak frequency | MTA GTFS | GTFS frequency + transfer graph |
| Distance to nightlife clusters | OSM/Liquor venues | OSRM nearest + NTA centroid |
| Crime/safety index | NYPD complaints + ACS population | per-capita rate by NTA |
| Nuisance density | NYC 311 (filtered) | count per sq mi by NTA |
| Building era mix | PLUTO `YearBuilt` | share pre-war/post-war/new by NTA |
| Violations & health score | HPD + DOB + 311 heat/bedbug | composite rate by NTA, normalized |
| Livability / address score | all of the above | weighted normalized sum → `nta_indicators` (weighting defined in `docs/`) |

---

## Aggregate collection budget (Phase 1, first full pull)

| Resource | Estimate |
| --- | --- |
| Total storage (raw) | ~5–6 GB dominated by 311, NYPD, PLUTO |
| Total fetch time (sequential) | ~2–3 hours (311 + NYPD dominate) |
| Total fetch time (parallel, 4–6 sources) | ~45–75 min |
| Ongoing monthly refresh | ~30–45 min (only 311/NYPD/HPD/DOB re-pull filtered slices) |
| API keys required | None for Phase 1 (Census key optional) |
| New `Settings` fields | None for Phase 1 |
| New loaders | ~9 (one per source above, minus crosswalks which are a fetch helper) |
| New compute steps | `processing/commute.py`, `processing/merge_indicators.py` |

## Order of acquisition (recommended)

1. **DCP crosswalks + NTA boundaries** (already referenced at
   `data/raw/ntas/ntas.json`) — everything else joins on these.
2. **Census ACS (DCP NTA tables)** — gives population denominators needed for
   per-capita rates and the demographic metrics; small & fast.
3. **PLUTO** — anchors BIN crosswalk for HPD/DOB and supplies building-era.
4. **NYPD + 311 + HPD + DOB** — the large Socrata pulls; run in parallel,
   use bulk export with date filters.
5. **MTA GTFS + subway entrances + Parks + OSM POIs + NYS Liquor** — small,
   fast, can batch together.
6. **Furman Center** — manual download; verify redistribution terms before
   persisting its columns into the shipped parquet.
7. **Compute steps** (commute, crime/nuisance/health aggregates, livability)
   run after their inputs land.

## Open questions

- **Furman redistribution:** confirm whether Furman-derived columns can be
  republished in `nta_indicators.parquet` (shipped artifact) or must stay as
  a join-time fetch. Affects whether the loader writes columns or a pointer.
- **311/NYPD retention:** do we keep full historic (GBs) or rolling 12-month
  slice only? Rolling slice is enough for neighborhood rates and cuts storage
  ~5×.
- **Geocoding NYS Liquor addresses:** confirm a free, ToS-clean geocoder
  (NYC GeoSupport / LION, or NYS GIS) since Google geocoding is ToS-restricted
  for cached results.
- **OSM Overpass reliability:** if the public instance is unreliable for
  repeat pulls, consider a one-time snapshot into `data/raw/osm_pois/` and
  refreshing quarterly rather than on every `fetch`.
