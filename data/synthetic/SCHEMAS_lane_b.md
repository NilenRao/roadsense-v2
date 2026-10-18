# Lane B data shapes (draft, pending schemas.md)

`schemas.md` was not on main when lane B started, so these are the shapes lane B reads and
writes. Everything in `data/synthetic/` is **SYNTHETIC** (`is_synthetic=true`,
`source="synthetic:<generator>@<version>"`). Regenerate with `python -m roadsense.synthetic`
(Makefile target `synthetic` to be added at the merge).

## Inputs lane B expects from lane A (`data/processed/`)
| File | Columns lane B uses |
|---|---|
| `segments.parquet` | segment_id, osm_way_id, name, highway, ward, length_m, geometry (WKT or WKB, EPSG:4326), bus_stop (bool), junction (bool), in_flood_area (bool), owner (`council`/`national_highways`); optional lat, lon (midpoint), esal_day |
| `load.parquet` (or `esal_day.parquet`) | segment_id, esal_day (only if esal_day is not already on segments) |
| `weather_weekly.parquet` | ward, week_start (Monday), rain_3d, rain_7d, rain_30d, rain_90d, hours_below_0_7d, freeze_thaw_14d, freeze_thaw_30d, rain_then_frost_7d, hot_days_7d. Every window covers only hours **before** week_start |
| `street_lamps.parquet` (optional) | lon, lat of OSM highway=street_lamp nodes in coverage |

Until those exist, deterministic mocks are written to `data/synthetic/mock_inputs/` and every
mock row carries `input_source="mock"`. Mock wards are named "Mock ward 01..17".

## Outputs
| File | Columns |
|---|---|
| `resurfacing.csv` | segment_id, date, treatment (`last_resurfaced`, `surface_dressing`, `crack_sealing`, `full_resurfacing`), road_class, is_synthetic, source |
| `potholes.csv` | event_id, segment_id, lat, lon, reported_date, repaired_date (blank = still open), depth_mm, width_mm, repair_type (`reactive`/`planned`), is_synthetic, source |
| `streetlights.csv` | lamp_id, segment_id, lat, lon, lamp_type (`LED`/`legacy`), install_date, position_source, current_unit_type, current_unit_install_date, is_synthetic, source |
| `light_faults.csv` | fault_id, lamp_id, fault_date, repaired_date (blank = open), status (`repaired`/`open`), fault_type (`burnout`/`electrical`/`damage`), unit_type, unit_install_date, burn_hours_at_fault, is_synthetic, source |
| `crew_outcomes.csv` | outcome_id, segment_id, inspection_date, selection (`model_top:<file>`/`proxy_top:...`/`random`), outcome (`pothole`/`cracking`/`none`), is_synthetic, source |
| `_truth.json` | hidden generator truth. **Never read by roadsense/models or roadsense/features** (test enforced) |
| `_meta.json` | seed, version, as_of, which inputs were mock, row counts |

"Currently dark" lamps = `light_faults.csv` rows with status `open` and fault_date in the last 30 days.

## Model outputs
The canonical columns are in `schemas.md` sections 7 and 8 (v2). **horizon_week runs 1 to 4** (weeks ahead of as_of), as agreed with lanes A and C on 17 Oct.
| File | Notes |
|---|---|
| `outputs/risk_<as_of>.parquet` + `risk_latest.parquet` | written by plan_risk: real-label ranking, 4 rows per scored segment; extra columns: history_source, weather_basis, weather_multiplier, p_report_year, lambda_year, rank_pct, inputs_mock, as_of |
| `outputs/risk_brighton_<as_of>.parquet` + `risk_brighton_latest.parquet` | map only: real-label model, real FixMyStreet history, §7 columns plus city and scale_source; wear blank, weather_multiplier 1.0 |
| `outputs/risk_synthetic_<as_of>.parquet` + `risk_synthetic_latest.parquet` | the synthetic-trained weekly model (same columns); feeds plan_risk's weather multiplier and lane C's what-if |
| `outputs/lights_<as_of>.parquet` + `lights_latest.parquet` | extra columns: unit_install_date, burn_hours, replacement_overdue, is_synthetic, model_version, source, as_of |
| `data/models/` | risk_y7.txt / risk_y30.txt (LightGBM, LF line endings), *_calibrator.pkl (isotonic), risk_eval.json, real_risk_eval.json, lights_weibull.json |

## Needed at the merge (shared files)
- Makefile: `synthetic: python -m roadsense.synthetic`, `train: python -m roadsense.models.risk` then `python -m roadsense.models.plan_risk`, `lights: python -m roadsense.models.lights`. Run order: synthetic, risk, plan_risk, synthetic again (crew outcomes select from risk_latest), lights, real_risk (evaluation).
- requirements.txt: numpy, pandas, pyarrow, pyyaml, scipy, shapely, lightgbm, scikit-learn, lifelines, shap, astral, tzdata, matplotlib, pytest.

## Answers to lane A's open questions (schemas.md v1)
1. **Snap radius:** 25 m for real FixMyStreet labels (headline numbers); resident GPS is coarse. 15 m stays for synthetic potholes, which are generated within 10 m of their segment.
2. **Segment length:** up to 200 m works for both lane B models. The spec's ~75 m only matters for how precise the treatment list is.
3. **Residential roads:** keep them in segments.parquet. The synthetic generators and streetlights use every council road; the real-label model filters on `is_main_road`.

Real-label model: `python -m roadsense.models.real_risk` reads segments.parquet, load.parquet and labels_segment_year.csv (or fms_snapped.csv / fms_roads_clean.csv and snaps itself), uses `config.yaml: labels.windows`, and writes reports/real_risk_eval.md.
