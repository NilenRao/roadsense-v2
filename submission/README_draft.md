# RoadSense: weekly pothole prevention for Southampton

> Submission README text, checked against `main` at 81d6e10 (18 Oct 2026). The repository's `README.md` is the
> canonical, fuller version (daily refresh details, privacy, full source table); this draft must not disagree with it.

RoadSense helps Southampton's highways officers decide which streets to treat this week, before potholes form. It
scores every council road segment on pothole risk from heavy-vehicle load, bus routes, flood areas, building sites,
utility digs, freeze-thaw weather and past pothole history, explains each score, and ranks a weekly prevention list
with a treatment and its cost logic for an officer to approve. Gemini triages public FixMyStreet reports into draft
work orders. A Brighton view runs the same model on 100% real inputs.

## What it does

- **Prevention plan** (Southampton): the top 50 road segments ranked by reactive repair cost avoided per £ of
  treatment, each with a suggested treatment and the reasons behind its ranking. Two views: **All roads**, and
  **Worth treating** (the treatment pays for itself over its service life, or the road has a confirmed pothole). A
  **What-if** menu (rain then frost, wet week, cold snap) shows which roads would rise.
- **Road card**: click a row or the map for urgency, the four forecast weeks, reasons, traffic confidence, treatment
  and cost logic, recent street works, and a "Treated <date>" flag when the council's own scheme lists show the road
  was resurfaced in the last 2 years (display only).
- **Report triage** (Southampton): real FixMyStreet reports are classified (Gemini, with a keyword fallback),
  attached to a road segment, de-duplicated, and turned into draft work orders that an officer approves or rejects.
  Recording a crew outcome re-scores the road.
- **Building sites and utility digs** (both cities): roads within 150 m of a building site (brownfield land registers,
  OpenStreetMap construction sites) and roads dug up in the last 12 months (DfT Street Manager) are flagged on the
  plan, the inspection list and the road card, and building sites are on the map. Both are also inputs to the risk
  model since 18 Oct.
- **Brighton inspection list**: the same model with real FixMyStreet history. Open reports from the last 120 days
  first, then the 50 roads with the highest predicted risk. No wear model or £ for Brighton.
- **Streetlights** (Southampton, synthetic demo data): lamps due for replacement within 90 days and lamps currently
  dark.
- **Daily refresh**: `python -m roadsense.refresh` pulls new FixMyStreet reports, the weather forecast, building
  sites and street works, re-scores with the frozen models, validates, and only then switches the app over. The
  header shows when the data was last updated.
- The side panel is resizable (drag the divider next to the map), with light and dark themes.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # add GEMINI_API_KEY (optional: triage falls back to keyword rules)
python -m roadsense.api           # open http://127.0.0.1:8000
pytest -q                         # contract + unit tests, no network
```

Data retrieved during the event is cached in the repo, so no download is needed to run the app. Full pipeline:
`make all` (ingest, features, synthetic, train, plan_risk, synthetic, lights, plan). Every tunable number is in
`config.yaml`, `config/synthetic.yaml` and `roadsense/prevent/prevent.yaml`.

## Data sources

All retrieved during the hackathon (16 to 18 Oct 2026); exact timestamps in `data/processed/sources.json`, and the app
footer lists every source with its retrieval date.

| Source | Used for | Licence |
| --- | --- | --- |
| FixMyStreet (mySociety), Open311 API | real pothole reports: training and test labels, open-report layer, triage | no licence stated; attributed to mySociety |
| OpenStreetMap | road segments, bus routes, junctions, cycle routes, schools and hospitals; construction sites | ODbL 1.0, © OpenStreetMap contributors |
| DfT road traffic statistics | HGV and bus load (314 Southampton segments counted, road-class medians elsewhere) | OGL v3 |
| Environment Agency Flood Map for Planning | Flood Zone 3 exposure | OGL v3 |
| Open-Meteo | weather history and 16-day forecast | CC BY 4.0 |
| MapIt (mySociety) / ONS | ward boundaries (ONS for Portsmouth) | OGL v3, contains OS data © Crown copyright |
| DfT Street Manager open data | completed utility and highway works, mid-2020 on | OGL v3 |
| Brownfield land registers (planning.data.gov.uk) | development sites near each road | OGL v3 |
| Google Gemini API | report triage | Gemini API terms; only public report text and synthetic data are sent |

Repair cost figures: planned £56.94 vs reactive £87.80 per pothole (ALARM 2025). Treatment costs per m² are our
assumptions and are labelled as such in the app. Bournemouth (BCP) was dropped: the council takes no FixMyStreet
reports.

## What is synthetic, and why

Council-held records are not public, so we generate stand-ins (seed 2026) on the real segments, weather and load.
Every synthetic row carries `is_synthetic=true` and a `source="synthetic:<generator>@<version>"` tag, and every screen
that uses one shows a **SYNTHETIC** badge.

| Synthetic | Replaces |
| --- | --- |
| pothole repair log | the council's repair log (yearly totals match Southampton's FOI figures; where and when is synthetic) |
| resurfacing history and road wear | the council's surface records |
| streetlight register and faults | the lighting asset register |
| crew outcomes | inspection results |

The plan's risk ranking is **trained on real FixMyStreet reports**; in Southampton its recent pothole history comes
from the synthetic repair log, because real reports there stop after 2019. The header says "trained on real reports ·
history synthetic". The What-if menu uses a weekly weather model trained on synthetic labels, so it is a pipeline
check, not proof. Brighton uses no synthetic data. Every assumption is in `config/synthetic.yaml` and `DATA_GAPS.md`.

## How well it works

All on real FixMyStreet reports the model never saw: the share of a test year's reports that land on the model's top
10% of road length (chance = 10%), against re-using last year's reports. 95% paired segment bootstrap, 1,000
resamples. Source: `reports/real_risk_eval.md`.

| Test | Model | Last year's reports | Difference | Model ahead in |
|---|---|---|---|---|
| **Brighton 2023-25** (802 reports) | 31.8% (27.9-35.6) | 21.7% (18.6-26.4) | +10.1 (+5.6 to +12.9) | 100% |
| **Southampton 2018-19** (381 reports) | 37.1% (29.7-43.9) | 33.7% (24.5-38.9) | +3.4 (-1.0 to +11.5) | 94% |
| Leave-one-city-out: Southampton | 34.8% | 33.5% | +1.3 (-0.9 to +6.0) | 94% |
| Leave-one-city-out: Brighton | 19.5% | 16.5% | +3.0 (-2.1 to +7.7) | 89% |
| Leave-one-city-out: Portsmouth | 19.9% | 13.1% | +6.8 (-3.6 to +13.8) | 87% |
| Frozen 2026 check, Southampton (33 reports) | 21.2% | 12.1% | +9.1 (-7.4 to +24.0) | 78% |

- Brighton is the clear result. Southampton is ahead in 94% of resamples, but the range includes zero, so it is
  "ahead, not proven".
- Building sites and utility digs don't help on held-out years: +0.6 points in Brighton (-2.2 to +3.7) and -4.5 in
  Southampton (-7.4 to -1.1) against the model without them (`reports/context_eval.md`). They stay in at the team's
  decision.
- In a city it has never seen, the model is only roughly level with last year's reports, and without past-report
  features it falls to 10.4% in Southampton: it needs local history.
- Streetlights: the Weibull model ranks no better than install year (`reports/lights_eval.md`); its value is the
  replacement date.
- Report triage: keyword rules get 91 of 100 real reports right (`reports/triage_vs_keywords.md`, labels drafted by
  Claude Code from the titles); the Gemini comparison has not been run.
- Running cost: about £0.00056 per triaged report, under £3 a year even at Southampton's full yearly repair volume
  (`reports/cost.md`; token counts measured, prices and volumes assumed).

## AI-generated code

All code in this repository was written with Claude Code (Anthropic), run by team members, one per part: data,
models, product. Commits made through Claude Code carry a `Co-Authored-By: Claude` line. Team members chose the
approach, reviewed and ran the code, checked the data and made the decisions recorded in `STATUS.md` and
`DATA_GAPS.md`.

## What existed before the event

Nothing. The repository was created after hacking opened on Friday 16 October 2026 at 20:00; its first commit is at
12:08 on Saturday 17 October 2026.

## Who helped

_To fill in by the team: names and roles, plus anyone else who helped._
