# Lane C (product) merge notes

Lane C owns `roadsense/prevent/`, `roadsense/agent/`, `roadsense/api/`, `roadsense/web/` and `reports/cost.md`.
Shared files change only at a merge, so what lane C needs from them is listed here.
Lane C follows `schemas.md` and `docs/contract_outputs.md` sections 7-10 on `lane-a-data` (draft, 17 Oct).
Lanes A and B are on `main`, so the `roadsense/prevent/mock_data/` copies were removed: lane C reads
`data/processed/`, `outputs/` and `data/synthetic/` directly. It falls back to badged MOCK risk/lights only
when no file with matching segment ids is found.

## Makefile targets
```make
plan:      ; python -m roadsense.prevent.plan
whatif:    ; python -m roadsense.prevent.whatif --scenario $(SCENARIO)
serve:     ; python -m roadsense.api
retrain:   ; python -m roadsense.api.retrain
triage-eval: ; python -m roadsense.agent.triage
cost:      ; python -m roadsense.agent.cost
demo-check: ; python -m roadsense.api.demo_check
```
`make serve FORECAST_AS_OF=2026-10-15T06:00` should pass `--forecast-as-of` to show the STALE banner.

## requirements.txt additions
fastapi, uvicorn[standard], httpx (tests), google-genai, python-dotenv

## Config
`roadsense/prevent/prevent.yaml` holds every lane C number (bands, consequence weights, treatment
thresholds and £/m², ALARM costs, what-if deltas, triage radii, cost assumptions). Move it under
`prevent:` in `config.yaml` at a merge if lane A prefers one file.

## Columns lane C reads
| File | Columns | If missing |
| --- | --- | --- |
| `data/processed/segments.parquet` (lane A) | segment_id, name, highway, ward, length_m, geometry (WKT, EPSG:4326), owner, bus_stop, junction, in_flood_area, esal_day | falls back to lane B's mock segments, then a lane C mock |
| same, optional | `esal_confidence` (`count` when a DfT count point is on the road, else `modelled`), `cycle_route`, `near_school_hospital` (within 100 m), `bus_route` or `bus_departures` | traffic shows "modelled estimate, low confidence"; those consequence weights don't apply |
| `data/processed/weather_forecast_weekly.parquet` (lane A) | `retrieved_at` | plan shows "no live forecast, provisional" |
| `data/processed/sources.json` (lane A) | list of {dataset, source, retrieved_at, data_date, licence, label} | footer shows only lane B/C datasets |
| `data/processed/wards.geojson` (lane A) | not read yet: coverage uses the city bbox plus the 30 m snap to a council segment | |
| `outputs/risk_<date>.parquet`, `outputs/lights_<date>.parquet`, `data/models/risk_y30*` (lane B) | as in SCHEMAS_lane_b.md | lane C mock / what-if surrogate |

## Lane C outputs
- `outputs/prevention_plan_<as_of>[_<scenario>].csv`
- `outputs/lane_c/state.json` (triage queue, work orders, feedback), `outcome_labels.csv`, `token_log.jsonl`, `demo_check.md`
- `reports/cost.md`, `reports/triage_vs_keywords.md`

## Data gaps and cuts (copy into DATA_GAPS.md at the merge)
- **Mock inputs (M6, M8).** Built before lane A's segments and lane B's merge. With mock grid segments most real FixMyStreet reports are not within 30 m of a segment, so triage attaches few reports to segments until real segments land.
- **Duplicates.** `fms_dedup.csv` already removed duplicates (20 m, 30 days), so the cached pull has no real pairs within 15 m and 14 days. The demo merges a report submitted twice; the merge logic is tested on fixtures.
- **Triage labels.** The 100 labels were made by Claude Code from report titles only (descriptions are not in the repo). No streetlight reports are in the pull, so streetlight triage is untested on real data. A teammate should spot-check the labels.
- **Gemini cost.** `reports/cost.md` only shows a measured cost once triage has run with `GEMINI_API_KEY` set. Prices are config assumptions.
- **What-if.** Re-scores with lane B's LightGBM y30 model when present; otherwise a sensitivity surrogate using the spec's generator coefficients and an assumed load interaction (labelled `sensitivity_surrogate`).
- **Line endings.** On Windows with `core.autocrlf=true`, `data/models/risk_y*.txt` checks out with CRLF and LightGBM's `model_file=` fails; lane C loads it with `model_str` after stripping `\r`. Lane B may want a `.gitattributes` with `data/models/*.txt -text`.
- **Treatment choice.** Lane B's synthetic wear saturates above 75% on many segments, so most plan rows are "patch now, schedule resurfacing".
