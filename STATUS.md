# Status

_Last updated: 17 Oct 2026, 17:05_

## Done
- `pull_fixmystreet.py` pulls FixMyStreet road/pavement reports for Southampton, Portsmouth and Brighton (2008–2026).
- Ran it: 9,035 reports → 5,406 road-surface defects after de-dup (Southampton 3,266 · Portsmouth 333 · Brighton 1,807).
- Pushed `data/fms_dedup.csv`, `data/README.md`, `data/summary.txt` and the script to `main` on
  [ArmaanF-dot/Pothole-simulator-](https://github.com/ArmaanF-dot/Pothole-simulator-).
- **Labels cleaned** (`clean_labels.py` → `data/fms_roads_clean.csv`, 5,127 reports): pavement/footway
  defects removed using title (then description) keywords; street names like "Tisbury Road" ignored.
  Removed: Brighton 203, Portsmouth 12, Southampton 64. Hand check of 25 each (Brighton):
  kept ≈ 24/25 genuine road defects, removed ≈ 22/25 genuine pavement. **Train on this file, not fms_dedup.csv.**
- **Lane A, branch `lane-a-data` (not merged yet):**
  - **M0 done:** `schemas.md` is the data contract for lane B (segments, load, weekly weather, labels). It uses lane B's
    column names and lists 3 open questions for them (snap 15 vs 25 m, segment length, residential roads).
  - FixMyStreet ingest moved to `roadsense/ingest/fixmystreet.py` (`python -m roadsense.ingest.fixmystreet`). It replaces
    `pull_fixmystreet.py` + `clean_labels.py` and rebuilds the same 5,127 labels exactly.
    New outputs go to `data/processed/` (labels now have `in_window`). Settings are in `config.yaml`. Raw pulls go to `data/raw/` (git-ignored).
  - Southampton "Street lighting" reports added: `data/processed/fms_streetlights.csv` (211), for lane B's streetlight check.
  - BCP and the other data gaps logged in `DATA_GAPS.md`.
  - **M1 done for Southampton (steps 1–2 below):** `data/processed/segments.parquet`, 8,145 segments ≤200 m, 600 km (126 km main road), with ward, bus_stop, bus_routes, junction and owner. All road classes are kept, and `is_main_road` marks A/B roads. Run with `.venv` Python: `python -m roadsense.ingest.osm Southampton` then `python -m roadsense.features.segments Southampton`. Lane B's loader reads it unchanged.
  - **M2 done for Southampton (step 3 below):** `data/processed/fms_snapped.csv` (reports + segment_id + snap_dist_m) and `labels_segment_year.csv` (8,145 segments × 2015–19 + 2026). 976 of 997 in-window reports snap within 25 m. Positives a year: 2015 157 · 2016 216 · 2017 222 (train) · 2018 252 · 2019 129 (test) · 2026 33 (frozen). Run: `python -m roadsense.features.labels`.
  - **M3a done for Southampton (step 4 below): traffic load** `data/processed/load.parquet` from DfT AADF (OGL, 132 count points): hgv_bus_aadf + esal_day per segment, averaged over train years 2015–17. Sources: 314 count, 604 same_road, 7,227 class_median. Run: `python -m roadsense.features.load`. 
  - **M3b done for Southampton: weekly weather** `data/processed/weather_weekly.parquet`, 10,591 ward-weeks (17 wards, Dec 2014 → Nov 2026; the latest weeks use the 16-day forecast and are marked `uses_forecast`). **Source: Open-Meteo, CC BY 4.0, free tier non-commercial** (fine for the hackathon, credit it in the README). Hourly temperature, rain, snow and soil at each ward's centre. Every window uses only hours before `week_start`. Run: `python -m roadsense.ingest.weather Southampton` then `python -m roadsense.features.weather_feats Southampton`. pytest: 8 passed, 5 skipped (lane B files not on this branch yet).
  - **Flood + context (Southampton):** `in_flood_area` from EA Flood Zone 3 (245 segments). `segment_context.parquet` adds cycle_route / near_school_hospital (schema 2b).
  - **Brighton and Portsmouth built (same pipeline; Southampton rows checked identical in every file):**
    - Brighton: 8,179 segments (664 km, 23 wards). Labels: train ≤2022 658 positive segment-years, test 2023–25 676, 2026 28. Load: 366 count / 948 same_road / 6,865 class_median. Flood Zone 3: 30 segments.
    - Portsmouth: 6,010 segments (486 km, 14 wards, 131 National Highways on M275/M27). Labels 2008–25 (training/transfer only) 246 positive, 2026 39. Load: 325 / 746 / 4,939. Flood Zone 3: 810 segments (13.5%).
    - **Weather for all three cities:** `weather_weekly.parquet` 33,642 ward-weeks (54 wards × 623 weeks, Dec 2014 → Nov 2026). `weather_forecast_weekly.parquet` 162 rows = next 3 weeks (19 Oct, 26 Oct, 2 Nov) for every ward. Open-Meteo, CC BY 4.0, non-commercial.
    - **Context columns on segments.parquet:** `cycle_route`, `near_school_hospital` (joined from `segment_context.parquet`, which stays; 22,334 rows, ids and order unchanged). For the demo filter `city == "Southampton"`.
    - **Contract test passes:** pytest 10 passed, 5 skipped (lane B synthetic/risk files not on `lane-a-data`). Pushed: Brighton 5da0950, Portsmouth cd10bd4.
    - **Portsmouth wards come from ONS** (Dec 2024, OGL): MapIt's free quota ran out (429). Same official names, `mapit_id` null. `wards.geojson` now has all 54 wards (`python -m roadsense.features.wards <city>`), and ONS is added to `sources.json`.

## Next (in order)
- **Contract test on `main` is red** until lane C adds §9 `confirmed_source_id` and `confirmed_is_synthetic` to the plan CSV (`outputs/prevention_plan_latest.csv`). See 5fa0d49.
0. **Decided: labels use all roads**, with `is_main_road` as a feature (only 27% of Southampton reports are on A/B roads).
   Lane B to answer the other open questions in `schemas.md`. At merge, delete the old `data/fms_*.csv` (lane B still points at `data/fms_dedup.csv`).
   Lane A milestones M1–M3 follow steps 1–4 below and write to `data/processed/` as `schemas.md` describes.
1. ~~**Road segments**~~ done (M1), see above. Original plan: `osmnx.graph_from_place("Southampton, UK", network_type="drive")`;
   keep highway = primary/secondary/tertiary (≈ A/B roads); project to EPSG:27700; cut into ~200 m segments;
   save `data/segments_southampton.gpkg` (id, road name, ref, highway, length_m, geometry).
2. ~~**Bus routes**~~ done (in segments.parquet): count routes per segment → `bus_routes` column.
3. ~~**Snap reports**~~ done (M2), all roads. Original plan: nearest segment within 25 m (EPSG:27700); further away = side street/pavement, drop.
   Output: label table segment × year → `had_report` (0/1).
4. ~~**Traffic load**~~ done (M3a). Original plan: count points with HGV/bus AADF; join to nearest segment on the
   same road; fill gaps from nearest count point on that road. Feature: `hgv_bus_aadf`.
5. **Baseline first:** rank segments by last year's reports → top-10% capture on the test window.
6. **Model:** gradient boosting on [hgv_bus_aadf, bus_routes, highway class, length, reports in previous 1–3 yrs].
   Compare to baseline + simple formula (load × past reports). Bootstrap the test set for a range.
7. **"When" model (city-month):** monthly report totals vs freeze-thaw days + rainfall (weather: Open-Meteo, CC BY 4.0, non-commercial; in `weather_weekly.parquet`).
8. Repeat 1–4 for Brighton, then Portsmouth, only after Southampton runs end to end (target 18:00).
- Credit FixMyStreet (mySociety) with the pull date in the main README.
- Decide how to handle the data gaps below in any city comparison.

## Decisions
- **Bournemouth dropped.** BCP Council doesn't take FixMyStreet reports ("Not reported to council"), so the API has no data.
- **`fms_raw.csv` stays local** (in `.gitignore`): it has free-text descriptions that can include names or house numbers. Only the de-duplicated file without descriptions goes on GitHub.
- **Duplicate = same city, within 20 m and 30 days** of an earlier report.
- **Compare trends within a city, not raw totals between cities.** Counts reflect FixMyStreet use, not true pothole rates.

## Train/test windows (agreed)
- **Southampton:** use 2015–2019 only (2011–14 was the council's old FixMyStreet-branded site: inflated counts).
  Train 2015–2017, test 2018–2019.
- **Brighton:** train ≤2022, test 2023–2025 (its reports fall away from mid-2025).
- **Score = rank within each city and year** (share of next-year reports on the top 10% of road length), never raw counts across cities.
- **2026 so far = extra "frozen model" check, not the main test.** Train on everything up to 2025, predict 2026,
  compare with Jan–Oct 2026 reports. Small (Southampton 35, Portsmouth 46, Brighton 32) and missing Nov–Dec, so report
  it with its sample size and a bootstrap range; compare against the same Jan–Oct months in earlier years.
- **Portsmouth:** extra training data / small transfer test only (333 reports total).

## Known gaps
- Southampton: no reports 2020–2022.
- Brighton: road reports almost stop in 2026.
