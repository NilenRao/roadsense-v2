# Data schemas (M0)

The contract between lane A (data) and lane B (models). Lane A writes these files, lane B reads them.
It covers every input listed in lane B's draft (`data/synthetic/SCHEMAS_lane_b.md` on `lane-b-models`)
using the same column names, plus the real FixMyStreet labels.
Any change to a column needs a note here and a message to the other lane.

_v2, 17 Oct 2026. Sections 1–6 owned by lane A, 7–8 by lane B, 9–10 by lane C. Contract agreed by lane B._

## Conventions
- **Cities:** `Southampton`, `Portsmouth`, `Brighton`. `city_code`: `SOU`, `POR`, `BRI`. Bournemouth (BCP) has no data: see `DATA_GAPS.md`.
- **Geometry:** stored as WKB in a `geometry` column, **EPSG:4326** (lon/lat). Distances, lengths and snapping are computed in **EPSG:27700** (British National Grid, metres).
- **Times:** ISO 8601. Report timestamps keep FixMyStreet's UTC offset. `date` columns are `YYYY-MM-DD`. `week_start` is a Monday.
- **segment_ids are frozen for Southampton** from 17 Oct 12:46: the OSM extract is cached and never re-downloaded (no `--refresh`) before submission. Adding Brighton/Portsmouth only appends rows (`BRI-…`, `POR-…`).
- **IDs are strings.** `segment_id` = `{city_code}-{osm_way_id}-{part}`, e.g. `SOU-4256781-02` (part = position along the OSM way, from 00). It stays stable as long as the OSM extract doesn't change.
- **Formats:** `data/processed/*.parquet` for model inputs; CSV for label tables that people open by hand.
- **Provenance:** every processed file has a `source` column (`fixmystreet`, `osm`, `dft`, `mock` …). Real data never sets `is_synthetic`. Lane B's synthetic files always do.
- **Privacy:** no free-text descriptions outside `data/raw/` (which is git-ignored).
- **Settings** (cities, windows, snap distance, segment length) live in `config.yaml`, never hard-coded.

## Folders
| Folder | In git? | Contents |
|---|---|---|
| `data/raw/` | no (except small reference files) | API pulls exactly as received, plus free text |
| `data/processed/` | yes | the files below |

## 1. FixMyStreet reports
### `data/raw/fms_raw.csv` (local only)
Every report in the configured categories, one row per FixMyStreet report.
`city, id, category, requested, updated, status, lat, lon, title, description, surface_keyword`
- `description` is cut to 500 characters. **Never commit this file.**

### `data/processed/fms_roads_clean.csv`: the road-defect labels
Road-surface reports only: duplicates removed (same city, ≤20 m and ≤30 days apart) and pavement/footway reports dropped.
| Column | Type | Notes |
|---|---|---|
| city | str | |
| id | int | FixMyStreet report id |
| category | str | council's own category name |
| requested | datetime | report time (label date) |
| updated | datetime | last update on FixMyStreet |
| status | str | `open` / `closed` |
| lat, lon | float | EPSG:4326 |
| title | str | short title only, no description |
| surface_keyword | bool | title/description mentions a surface word |
| in_window | bool | inside that city's usable label window (`config.yaml: labels.windows`) |

Same columns and rows as the existing `data/fms_roads_clean.csv` (5,127 rows), plus `in_window`.

### `data/processed/fms_streetlights.csv`: Southampton street-lighting reports
Same columns as above, minus `surface_keyword`. Category `Street lighting`. For lane B to check the streetlight fault generator. These are not used as road labels.

### `data/processed/fms_snapped.csv` (M2)
`fms_roads_clean.csv` columns + `segment_id` (blank if none within the snap distance) and `snap_dist_m` (float).
Keeping the distance lets lane B use its 15 m radius and lane A its 25 m without a second snap.

## 2. Road segments: `data/processed/segments.parquet` (M1)
One row per piece of road, all cities stacked. Built from OSM (ODbL) `network_type="drive"`.
| Column | Type | Notes |
|---|---|---|
| segment_id | str | see conventions |
| city | str | |
| osm_way_id | int64 | |
| name | str | blank if unnamed |
| ref | str | e.g. `A33`; blank if none |
| highway | str | OSM class (`primary`, `secondary`, `tertiary`, `residential`, …) |
| is_main_road | bool | highway in primary/secondary/tertiary (+ `_link`), i.e. A/B roads. A feature: labels cover all roads |
| ward | str | council ward the midpoint falls in |
| length_m | float | ≤ `segments.max_length_m` (200) |
| lat, lon | float | midpoint |
| bus_stop | bool | OSM bus stop within 20 m |
| bus_routes | int | number of OSM `route=bus` relations using the way |
| junction | bool | segment ends at a node where ≥4 streets meet, or a mini-roundabout/traffic-signal node (3-way meets are left out: nearly every edge ends at one) |
| in_flood_area | bool | midpoint in an EA Flood Zone 3 polygon (rivers and sea, OGL; `python -m roadsense.ingest.flood`) |
| owner | str | `council` / `national_highways` (motorway or trunk with a National Highways ref) |
| geometry | WKB | EPSG:4326 LineString |
| source | str | `osm` |
| cycle_route | bool | copied from `segment_context.parquet` (section 2b) on `segment_id` |
| near_school_hospital | bool | copied from `segment_context.parquet` (section 2b) on `segment_id` |

The two context columns are on `segments.parquet` because lane C's urgency weights switch on when they exist there. `segment_context.parquet` stays the source, and every segments rebuild re-attaches them (row count and `segment_id`s unchanged).

## 2b. Segment context: `data/processed/segment_context.parquet` (lane A)
`segment_id, city, cycle_route (bool), near_school_hospital (bool), source` (OSM, ODbL). For the urgency consequence weights.
`cycle_route`: way in an OSM `route=bicycle` relation or tagged with an on-road cycle lane/track. `near_school_hospital`:
OSM `amenity=school|college|hospital` within 100 m. Kept as its own file and also copied onto `segments.parquet` (section 2).
Build: `python -m roadsense.features.context Southampton`.

## 3. Traffic load: `data/processed/load.parquet` (M3)
| Column | Type | Notes |
|---|---|---|
| segment_id | str | |
| hgv_bus_aadf | float | HGV + bus annual average daily flow (DfT, OGL), averaged over the city's **train** years (test years stay unseen) |
| esal_day | float | standard axles per day: each vehicle class × an ASSUMED wear factor (`config.yaml: load.esal_factors`) |
| count_point_id | Int64 | DfT count point used (blank for class_median) |
| count_year | Int64 | latest year in the average (blank for class_median) |
| load_source | str | `count` (point on this segment), `same_road` (nearest point on the same road), `class_median` (no point on the road: median of that highway class, or of a similar class per `load.class_fallback`) |
| source | str | `dft` |

Built by `python -m roadsense.features.load`. Lane B's loader merges `esal_day` from this file.

## 4. Weather: `data/processed/weather_weekly.parquet` (M3)
One row per ward × week, exactly as lane B asked for. **Every window uses only hours before `week_start`.**
`city, ward, week_start, rain_3d, rain_7d, rain_30d, rain_90d, hours_below_0_7d, freeze_thaw_14d, freeze_thaw_30d, rain_then_frost_7d, hot_days_7d, uses_forecast, data_date, retrieved_at, source`
- Rain is in mm, plus counts of hours/days. A freeze-thaw crossing is the temperature passing 0 °C in either direction.
- `uses_forecast`: true if any hour in the 7 days before `week_start` came from the forecast. `data_date`: latest weather hour in the pull.
- **Source: Open-Meteo, CC BY 4.0, free tier non-commercial.** Hourly values at each ward's centre (`python -m roadsense.ingest.weather <city>`
  then `python -m roadsense.features.weather_feats <city>`). `week_start` is a string `YYYY-MM-DD` (Monday).

### `data/processed/weather_forecast_weekly.parquet`
The rows of `weather_weekly.parquet` whose `week_start` is **after the build date**: the next 3 weeks, built from the 16-day Open-Meteo forecast.
Same columns, and `uses_forecast` is true on every row. Ward names match `segments.parquet` exactly (checked by `tests/test_contract.py`).
Rewritten on every weather build, so re-run the weather build before scoring.

## 5. Labels for the "where" model: `data/processed/labels_segment_year.csv` (M2)
| Column | Type | Notes |
|---|---|---|
| city | str | |
| segment_id | str | every segment in `segments.parquet` (all road classes) |
| is_main_road | bool | copied from segments, so it can be used as a feature |
| year | int | |
| n_reports | int | snapped reports in that year |
| had_report | int | 0/1 |
| split | str | `train` / `test` / `frozen_2026` / `excluded`, from `config.yaml: labels.windows` |

Rows: every segment × every year in the city's usable window, plus 2026. Reports count if they are within `labels.snap_m` (25 m).
Built by `python -m roadsense.features.labels`.

## 6. Street lamps (optional): `data/processed/street_lamps.parquet`
`osm_node_id, city, lon, lat, source` (OSM `highway=street_lamp` nodes).

## Splits (as agreed in STATUS.md)
| City | Usable labels | Train | Test |
|---|---|---|---|
| Southampton | 2015–2019 | 2015–2017 | 2018–2019 |
| Brighton | to 2025 | ≤2022 | 2023–2025 |
| Portsmouth | all | extra training / small transfer test | — |
| All | 2026 Jan–Oct | — | `frozen_2026` check only, report n and a bootstrap range |

## Decisions agreed with lane B (17 Oct)
1. **Snap radius:** 25 m for real FixMyStreet labels. 15 m only for lane B's synthetic potholes. `snap_dist_m` is stored in `fms_snapped.csv`.
2. **Residential roads are kept** in `segments.parquet` (and scored).
3. **Labels cover all roads.** `is_main_road` is a feature, not a filter (only 27% of Southampton 2015–19 reports are on A/B roads).
4. **Segment length:** ≤200 m. Lane B hasn't asked for a change.

---

# Outputs: lane B → lane C (sections 7–10)
Conventions as above: string `segment_id`, EPSG:4326, ISO dates, `week_start` = Monday.
**Every output says whether synthetic data went into it**, so the UI can show the SYNTHETIC badge.
**Files:** each run writes a dated file (`<name>_<YYYY-MM-DD>`) **and** a copy at `<name>_latest` with the same contents, which the API reads.

## 7. Risk and wear: `outputs/risk_<YYYY-MM-DD>.parquet` + `outputs/risk_latest.parquet` (lane B writes, lane C reads)
**4 rows per scored segment, one per forecast week** (`horizon_week` 1–4). Motorways (`owner = national_highways`) are not scored. Residential roads are.
| Column | Type | Notes |
|---|---|---|
| segment_id | str | joins `segments.parquet` |
| ward | str | from `segments.parquet` (lets the API filter without a join) |
| week_start | date | forecast week |
| horizon_week | int | **1–4** (1 = first forecast week), as lane B writes in `risk_latest` (commit 1496843) |
| p7 | float 0–1 | calibrated probability of a pothole in the next 7 days |
| p30 | float 0–1 | … next 30 days |
| p30_trend | float | change in p30 against the previous week's row (as defined by lane B) |
| wear | float 0–100 | % cumulative failure probability at current age. **Southampton only** (needs synthetic resurfacing history); **blank elsewhere** |
| reason_1, reason_2, reason_3 | str | plain-English top SHAP reasons, e.g. "High HGV + bus load (A33)" |
| model_version | str | e.g. `risk@2026-10-17T22:40` |
| trained_on | str | `synthetic`, `real`, or `synthetic+real` |
| history_source | str | where the past-report history features came from: `real` (FixMyStreet) or `synthetic` (council repair log stand-in, rank-mapped onto 2015–19 FixMyStreet counts) |
| uses_synthetic | bool | true if any synthetic input/label was used |
| weather_source | str | `open-meteo` or `mock` |
| weather_data_date | str | latest weather hour used (for the STALE banner) |
| uses_forecast | bool | the row depends on forecast weather (`weather_forecast_weekly.parquet`) |
| forecast_retrieved_at | str | when the forecast used was retrieved. **The STALE banner uses this** (older than 24 h → STALE, plan provisional), not `weather_data_date` |

**Three risk files (from Merge 2):**
- `outputs/risk_latest.parquet`: Southampton. **Ranks the plan.** Real-label model (`trained_on = real`) with `history_source = synthetic` (Southampton's FixMyStreet reports collapse after 2019). Weekly movement comes from a ward-level weather multiplier from the synthetic-trained weekly model; yearly scale = Southampton FOI repairs.
- `outputs/risk_brighton_latest.parquet`: Brighton, **map only, 100% real inputs** (`trained_on = real`, `history_source = real`, `uses_synthetic = false`, `wear` blank). Extra columns `city`, `scale_source`. The 4 weeks repeat the yearly risk (no weekly weather model) and the yearly scale is an assumption (`scale_source`), so Brighton p7/p30 are **not comparable** with Southampton's: show band and reasons, not p30.
- `outputs/risk_synthetic_latest.parquet`: Southampton, synthetic-trained weekly model. Used only for the what-if, as a scenario-vs-baseline hazard ratio applied to `risk_latest`; its levels never mix with the real-label model's.

## 8. Street lights: `outputs/lights_<YYYY-MM-DD>.parquet` + `outputs/lights_latest.parquet` (lane B → C)
| Column | Type | Notes |
|---|---|---|
| lamp_id | str | |
| segment_id | str | |
| lat, lon | float | |
| lamp_type | str | `LED` / `legacy` |
| p_fail_90d | float 0–1 | |
| due_90d | bool | due for replacement within 90 days (as defined by lane B) |
| replace_by | date | date cumulative failure reaches 0.5 |
| currently_dark | bool | open fault in the last 30 days (synthetic) or open FixMyStreet street-lighting report (real, `fms_streetlights.csv`) |
| dark_since | date | fault or report date; blank if not dark |
| dark_source | str | `synthetic` / `fixmystreet` / blank |
| uses_synthetic | bool | |

## 9. Prevention plan rows: `outputs/prevention_plan_<YYYY-MM-DD>.csv` (lane C writes, from 7)
`rank, segment_id, ward, name, ref, band (critical/high/prevention/monitor), urgency (0–100), p30, wear,
treatment (crack_sealing/surface_dressing/patch/resurface/permanent_repair), reason_1..3,
cost_planned_gbp, cost_reactive_avoided_gbp, open_fms_report (bool), uses_synthetic, data_dates (json: source → date),
p30_peak, p30_rise, treatment_label, avoided_per_gbp, load_confidence, provisional`
- `p30_peak`: highest p30 over the 4 forecast weeks; `p30_rise`: rise in p30 across them (the plan needs "p30 forecast to rise").
- `treatment_label`: plain-English name of `treatment` for the UI. Treatment codes as listed above (agreed).
- `avoided_per_gbp`: expected reactive cost avoided per £ of treatment. **This is the ranking key** (`rank` sorts by it).
- `load_confidence`: high / medium / low (see section 10). `provisional`: true when the forecast is STALE.
- One dated file per run plus a `prevention_plan_latest.csv` copy (no symlinks: unreliable on Windows).

#### Added for Merge 2: financial split
| column | type | meaning |
|---|---|---|
| `pays_back` | bool | `avoided_per_gbp >= 1.0` |
| `confirmed_pothole` | bool | `confirmed_by != ""`: an open FixMyStreet report triaged as a road defect, or a crew record |
| `confirmed_by` | str | one of `""` \| `"keyword"` \| `"gemini"` \| `"crew"` (`""` = not confirmed). If several apply, precedence is **crew > gemini > keyword** |
| `confirmed_source_id` | str | FixMyStreet request id for `keyword`/`gemini`; inspection/feedback id for `crew`. **Required if `confirmed_by != ""`**, blank otherwise |
| `confirmed_is_synthetic` | bool | true for `crew` records from simulated feedback. The dashboard labels these "Simulated crew report" and leaves them out of the header's confirmed total |
| `worth_treating` | bool | `pays_back OR confirmed_pothole`; drives the "Worth treating" tab |

- `avoided_per_gbp` = expected reactive repair cost avoided over the treatment's life / treatment cost (not p30 only); horizon stated in `reports/cost.md`.
- UI: "All roads" tab lists every row badged "Pays back xN" or "Loss maker xN". "Worth treating" tab lists `worth_treating` rows: confirmed first (by urgency), then pays-back (by `avoided_per_gbp`).
- Confirmed rows with `pays_back = false` are shown as "Confirmed: must fix" (statutory repair duty), not as optional.

## 10. API JSON (lane C serves; field names = the columns above)
- `GET /segments?ward=&band=` → GeoJSON FeatureCollection with **light properties** for the map (segment_id, name, ward,
  band, urgency, p30, load_confidence, uses_synthetic). `?detail=full` returns every field: segments.parquet fields
  (no geometry column) + latest risk row + `urgency`, `band`, `load_confidence`.
- `load_confidence` mapping (agreed): `count` → high, `same_road` → medium, `class_median` → low ("modelled estimate").
- `GET /segments/{id}` → one Feature plus `risk_weeks: [...]` (the 4 rows), `load: {...}`, `reports: [...]`,
  `outcomes: [...]`, `data_dates: {source: date}`.
- `GET /plan/prevention?ward=` → `{as_of, provisional (bool, true if forecast > 24 h old), rows: [section 9]}`
- `GET /lights?due_days=` (default 90) → section 8 rows.
- `POST /reports` / `POST /outcomes` → as in the spec (M8).
- `GET /sources` → `[{name, label, used_for, licence, retrieved_at, data_date, is_synthetic}]`; `label` is the footer text.

**Agreed with lane C (17 Oct, 14:18):** wear for Southampton only, badged SYNTHETIC, null elsewhere; 4 weekly rows
(the plan needs the rise and the what-if changes weeks 1–3); dated files plus `*_latest` copies; STALE rule = forecast
older than 24 h by `forecast_retrieved_at`. Still open: `POST` payloads (lane C defines in the API code) and whether
`/segments` takes a city filter (needed only once Brighton/Portsmouth are scored).

## 11. Ward boundaries: `data/processed/wards.geojson` (lane A)
`city, ward, mapit_id, source, geometry` (Polygon/MultiPolygon, EPSG:4326, simplified ~5 m). Ward names match
`segments.parquet` exactly. Use for the "outside coverage" check (point not in any ward → `out_of_coverage`).
Source: MapIt (mySociety), boundaries from ONS/OS, OGL, for Southampton and Brighton. **Portsmouth comes from ONS (Wards Dec 2024, OGL)** because MapIt's free quota ran out, so its `mapit_id` is null (same official ward names). Append a city with `python -m roadsense.features.wards <city>`; cities already in the file are left untouched.

## 12. Data sources for the footer: `data/processed/sources.json` (lane A writes, lane C serves at `GET /sources`)
`[{name, label, used_for, licence, retrieved_at, data_date, is_synthetic}]`, one entry per real source (OSM, MapIt wards,
DfT, Open-Meteo, EA flood, FixMyStreet). `retrieved_at` is ISO UTC; `data_date` is the newest date the data describes.
Lane B appends its synthetic generators (`is_synthetic: true`) or lane C adds them from `data/synthetic/_meta.json`.

---

# Additions, 17 Oct evening (lane B; fields added after sections 7-12 were agreed)
Each item below says which section it extends. Checked by `tests/test_contract.py`.

## 13. Display road names (extends section 2; computed, not a stored column)
The page and every output that shows a road name use `roadsense.prevent.inputs.display_names(seg)`:
OSM `name`, else `ref`, else for an unnamed slip road (`highway` ending `_link`) **"Slip road off <ref or name>"** of the
road it joins (walking connected unnamed slip-road pieces, up to 6), else **"Unnamed slip road near <ward>"**; any other
unnamed road stays "Unnamed <highway> road". `segments.parquet` itself is unchanged. Plan CSVs (section 9) and the
Brighton map/inspection list carry the display name in `name`.

## 14. Extra columns in the risk files (extends section 7)
| Column | Type | Files | Notes |
|---|---|---|---|
| weather_basis | str | all three | `observed`, `forecast`, `climatology_10y`, or `none (...)` for Brighton |
| weather_multiplier | float | risk_latest, risk_brighton | ward-week weather multiplier applied to the yearly risk; 1.0 for Brighton |
| p_report_year | float 0–1 | risk_latest, risk_brighton | real-label model's yearly probability of a FixMyStreet report on the segment |
| lambda_year | float ≥ 0 | risk_latest, risk_brighton | expected potholes a year = risk share × city total (Southampton FOI 2025; Brighton assumed, see `scale_source`) |
| rank_pct | float 0–1 | risk_latest, risk_brighton | within-city percentile of `p_report_year` (not shown on the page) |
| inputs_mock | bool | all three | true only if lane A inputs were mocked |
| as_of | str | all three | `YYYY-MM-DD`, Monday the forecast weeks follow |
| city | str | risk_brighton | `Brighton` |
| scale_source | str | risk_brighton | how Brighton's yearly total was set (an assumption: Southampton repairs per km × Brighton road length) |

- `model_version`: `risk@<YYYY-MM-DDTHH:MM>` (UTC run time) on every row of every risk file; `lights@<...>` in section 8.
- `reason_1..3`: plain English. Last year's ward weather is **never** a reason (only 2–6 distinct values per city).
  History reasons say whether they come from FixMyStreet (real) or the synthetic council log ("synthetic council log, scaled").
- `risk_synthetic_latest.parquet` has the section 7 columns plus `weather_basis`, `inputs_mock`, `as_of`;
  `trained_on = synthetic`.

## 15. Extra columns in the lights file (extends section 8)
| Column | Type | Notes |
|---|---|---|
| unit_install_date | date | install date of the unit in service (after replacements) |
| burn_hours | float | burn hours on that unit at `as_of` (sunset to sunrise, minus the 01:00–05:30 switch-off from 1 Dec 2023) |
| replacement_overdue | bool | cumulative failure already ≥ 0.5 |
| is_synthetic | bool | always true (synthetic lamp register) |
| model_version | str | `lights@<YYYY-MM-DDTHH:MM>` |
| source | str | model and synthetic inputs used |
| as_of | str | `YYYY-MM-DD` |

## 16. Extra columns in the plan CSVs (extends section 9)
Beyond the section 9 list the plan files also carry: `highway, consequence, consequence_text, reasons` (the three
reasons joined), `cost_logic` (the £ working as text), `esal_day, traffic_note, weather_source, model_version, as_of,
labels_synthetic, history_source, wear_synthetic, inputs_mock, p30_w0, value_badge` ("Pays back ×N", "Loss maker ×N",
"Confirmed: must fix ×N", "Simulated crew report"), and for confirmed rows the segment's own preventive suggestion:
`preventive_treatment, preventive_treatment_label, preventive_treatment_cost_gbp, preventive_reactive_cost_avoided_gbp,
preventive_avoided_per_gbp`. `outputs/prevention_plan_worth_treating_<as_of>.csv` has the same columns, `worth_treating` rows only.

## 17. Map-only city endpoints (extends section 10)
- `GET /reports/open?city=Brighton` → `{count, window_days, urgency: null, city, source, retrieved_at, reports: [{id, category,
  reported_at, status, lat, lon, title}]}`. Same 120-day open window as Southampton; titles redacted.
- `GET /plan/inspection?city=Brighton&top_n=50` → `{city, header, badge, sections, count, open_report_rows, top_n,
  window_days, model_version, trained_on, uses_synthetic, history_source, as_of, path, rows}`.
  `sections`: `[{key: "open", label: "Open reports (inspect first)", count, note}, {key: "risk", label: "Highest predicted risk", count, note}]`.
  Each row: `rank (int), section ("open"|"risk"), segment_id, name (section 13), ward, highway, urgency (0-100),
  band (section 9 bands), p30, consequence_text, reasons (list of str), action ("Inspect"), open_report (bool),
  open_reports ([{id, reported_at, title}]), history_source, uses_synthetic (bool)`. No treatment or £ fields.
  Urgency is Southampton's formula without the wear term (no resurfacing history): `100 * min(1, C * P30)`, with the
  same consequence weights C and band cut-offs. Both sections are sorted by urgency.
  400 for the home city (use `/plan/prevention`).
- `GET /triage/city?city=Brighton` → the triage cache (section 18); 404 if not built.
- `GET /segments/{id}?city=Brighton` → card with `urgency, band, p7, p30, consequence, consequence_text, wear_pct (null), reasons, badge, note`.

## 18. Triage caches: `outputs/lane_c/triage_<city>.json` (lane C writes with `python -m roadsense.agent.city_triage <city>`)
Read-only triage for a map-only city, so the demo never waits on live Gemini. Only Brighton is built on main;
Southampton's triage is live (`POST /reports`) and its queue lives in `outputs/lane_c/state.json`, which is not in git.
| Field | Type | Notes |
|---|---|---|
| city, generated_at, window_days, limit, count, methods, source, retrieved_at, note | str / int / list | `methods` ⊆ {`gemini`, `keyword`}; `count` = number of reports |
| reports[] | list of objects | most recent first |
| reports[].report_id, fms_id | str, int | `fms-<id>` and the FixMyStreet request id |
| reports[].reported_at, lat, lon, title | str, float, float, str | title redacted (no names, house numbers or postcodes) |
| reports[].category, method, model | str, str, str or null | category ∈ {`pothole`, `streetlight`, `other`} |
| reports[].segment_id, segment_name, ward, snap_m | str or null, str or null, str or null, float or null | null when not within 30 m of a council road |
| reports[].risk_band, urgency | str or null, float or null | band and urgency of the snapped segment; the API refreshes both from the current scores |
| reports[].merged_into, status | str or null, str | status ∈ {`triaged`, `merged`, `not on a council road`} |

## 19. Lane B model and evaluation files (`data/models/`, `reports/`)
- `data/models/real_risk_model.txt` (LightGBM, LF line endings) + `real_risk_model.json` (`features`, `city_categories`,
  `trained_rows`, `trained_cities`, `city_total_potholes_year`, `history_years`, `history_source`, `generated_at`):
  the model that ranks `risk_latest` and `risk_brighton_latest`.
- `data/models/risk_y7.txt`, `risk_y30.txt` + `*_calibrator.pkl`: synthetic-trained weekly model (`risk_synthetic_latest`, what-if).
- `data/models/real_risk_eval.json`, `eval_ci.json`, `risk_eval.json`, `lights_weibull.json`: evaluation results behind
  `reports/risk_eval.md`, `reports/real_risk_eval.md`, `reports/lights_eval.md`.
- `outputs/lane_c/demo_check.md` (demo dry run) and `outputs/lane_c/token_log.jsonl` (Gemini token counts for
  `reports/cost.md`): lane C working files, not read by other lanes.

## 20. Building sites and utility digs (18 Oct)
- `data/processed/construction_sites.csv` (`roadsense.ingest.construction`): source (`brownfield_register` | `osm_construction`),
  city, reference, name, site_address, lon, lat, hectares, min_net_dwellings, max_net_dwellings, permission_status,
  permission_type (OSM: the `construction=*` value), permission_date, ownership_status, public_land, deliverable,
  start_date, end_date, entry_date, site_plan_url, retrieved_at.
- `data/processed/street_works.csv` (`roadsense.ingest.streetworks`, git-ignored: 38 MB, rebuilt by the ingest): one row per completed permit: city, permit_reference,
  work_reference, promoter, promoter_swa_code, activity_type, work_category, works_location_type, traffic_management_type,
  street_name, usrn, actual_start, actual_end, lon, lat (works midpoint), length_m, source_month.
- `data/processed/site_segments.csv`: city, reference, segment_id, dist_m (site point within 150 m of the segment).
- `data/processed/works_segments.parquet`: city, permit_reference, promoter, activity_type, work_category, works_location_type,
  street_name, actual_end, year, carriageway (bool), segment_id (blank if no council segment within 20 m), snap_m.
- Plan rows, inspection rows and road cards gain `context` (null or `{construction: {count, sites[]}, works: {count,
  carriageway, months, last, promoters[], activities[]}}`): what is near the road now.
- Risk model features (roadsense.models.real_risk CONTEXT, from earlier years only): con_sites_3y, con_dwellings_3y
  (register sites permitted in the 3 years before, within 150 m), uw_carriageway_1y, uw_any_1y, uw_carriageway_2y
  (completed Street Manager works on the segment; NaN before 2022 rows).
- `GET /context/sites?city=`: building sites active now (register sites permitted in the last 3 years and still listed,
  plus OSM construction sites). `GET /context/evaluation`: held-out verdict per feature set.
- `data/models/context_eval.json` + `reports/context_eval.md` (`roadsense.models.context_eval`).
