# RoadSense: weekly pothole prevention for Southampton

RoadSense helps Southampton's highways officers decide which streets to treat this week, before potholes form.
It scores every council road segment on pothole risk from heavy-vehicle load, bus routes, flood areas,
freeze-thaw weather and past pothole history, explains each score, and ranks a weekly prevention list with a
treatment and its cost logic for an officer to approve. A "Worth treating" view shows the roads where planned
treatment costs less than the reactive repairs it avoids, plus every road with a confirmed pothole. Gemini
triages public FixMyStreet reports into draft work orders. A Brighton map shows the same model on 100% real inputs.

Built in 24 hours at SotonShip26 (AI Society, Eagle Labs Southampton), Southampton City track, by team Myriad
(Nilen Rao, Aryan Patel, Armaan Fernandes).

> **This is the post-hackathon version, not the submission.** The code judged at SotonShip26 is
> [ArmaanF-dot/Pothole-simulator-](https://github.com/ArmaanF-dot/Pothole-simulator-) (main @ `732a5f3`). This repo
> starts from that code and redesigns the officer page (`roadsense/web/`) around four screens:
> 1. **This week's plan**: every road gets a plain-English reason and one verdict (Fix now / Wait / Recently fixed).
> 2. **Road card**: why it's on the list, fix now (£57) vs wait (£88), and a "What we don't know" box for stand-in data.
> 3. **Name-a-road mode**: type any street and get three answers: is it on the list, why, and fix now or wait.
> 4. **Report triage**: what the resident sent → what the AI understood → you decide.
>
> The API, models, data and numbers are unchanged; the page only re-words and re-lays out what the API returns.

## What it does

- **Prevention plan** (Southampton): road segments ranked by reactive repair cost avoided per £ of treatment, with a
  suggested treatment and the reasons behind each ranking. Split into *All roads* and *Worth treating* (treatment pays
  for itself, or a confirmed pothole).
- **Report triage** (Southampton): real FixMyStreet pothole reports are classified (Gemini, with a keyword fallback)
  and turned into work orders that an officer approves or rejects.
- **Building sites and utility digs** (both cities): roads within 150 m of a building site (brownfield land registers
  and OpenStreetMap construction sites) and roads dug up by utility companies or the council in the last 12 months
  (DfT Street Manager) are flagged on the plan, the inspection list and the road card, and building sites are on the
  map. Both are also inputs to the risk model (earlier years' sites and digs, team decision 18 Oct). The held-out check
  (`reports/context_eval.md`) finds no gain from them: +0.6 points in Brighton, −4.5 in Southampton (see How well it
  works). Roads dug up the year before had twice the report rate of similar-risk roads in Brighton 2023-25, but the model
  can only learn digs from Brighton 2022 (Street Manager starts in 2020).
- **Streetlights** (Southampton, synthetic demo data).
- **Brighton risk map**: the same risk model applied to Brighton & Hove, a city with plentiful real report data, as an
  out-of-area check.

## Quick start

Tested on Python 3.14.7, Windows 11.

```bash
git clone https://github.com/ArmaanF-dot/Pothole-simulator-
cd Pothole-simulator-
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # add GEMINI_API_KEY (optional: triage falls back to keyword rules)
python -m roadsense.api           # open http://127.0.0.1:8000
pytest -q                         # contract + unit tests, no network
```

Needs git. `.env` holds `GEMINI_API_KEY` (free from Google AI Studio) and, for the daily refresh, `ROADSENSE_CONTACT` (a contact put in the User-Agent sent to FixMyStreet and Open-Meteo, e.g. an email or URL; blank = the project's GitHub URL). Without the key the map and plan work and triage
falls back to keyword classification. Press Ctrl+C to stop the server. `python -m roadsense.api --port 8001` changes the
port; `--forecast-as-of <ISO date>` pretends the weather forecast was fetched at that time, to show the STALE banner
and provisional plan. Data was retrieved during the event and is cached in the repo, so no download is needed to run
the app. Do not re-pull OpenStreetMap data: segment IDs would change.

Tests: `python -m pytest -q` on main (18 Oct, Windows 11): all pass. The warnings are pandas/NumPy and
FastAPI deprecation notices and don't affect results.

Full pipeline (`make all`, or run the `python -m` lines in the `Makefile` on Windows):
ingest → features → synthetic → train → plan_risk → synthetic → lights → plan. Every tunable number is in
`config.yaml`, `config/synthetic.yaml` and `roadsense/prevent/prevent.yaml`. The files the three parts of the
code pass between them are specified in `schemas.md` and checked by `tests/test_contract.py`.

## Daily refresh

`python -m roadsense.refresh` updates the data the app shows, unattended, without ever breaking it.

```bash
python -m roadsense.refresh --dry-run          # fetch, build and validate in a temp folder; switch nothing
python -m roadsense.refresh                    # Southampton and Brighton
python -m roadsense.refresh --city Brighton    # one city
```

What it does, per city (Southampton, Brighton):
1. **FixMyStreet:** pulls only reports newer than the latest one already stored (Open311, agencies 2567 / 2614,
   1000 per request, the date range is split while a request hits the cap). New reports go through the same
   road-surface, de-duplication and pavement rules as `roadsense.ingest.fixmystreet`, and every title is passed
   through `redact()` before it is stored. Descriptions are never stored.
   The same pull starts at the beginning of the 120-day open window, so it also rechecks the status of every stored
   report in that window and updates open/closed (a report closed upstream leaves the open layer and the plan).
   At most one incremental pull per UTC day: a second run the same day reuses that day's reports and only refreshes
   the weather. Requests to FixMyStreet and Open-Meteo send `User-Agent: RoadSense/1.0 (SotonShip26 hackathon; <contact>)`,
   with `<contact>` from `ROADSENSE_CONTACT` in `.env` (the project's GitHub URL if unset).
2. **Open-Meteo:** refreshes the forecast weeks (16-day forecast; `past_days=92` supplies the 90-day windows), at
   each ward's point from the committed `data/processed/ward_points_<city>.csv`. The refresh never reads `data/raw/`
   (not committed), so it runs from a fresh clone.
3. **Building sites and street works:** once a UTC day, re-reads the brownfield land registers (planning.data.gov.uk)
   and OpenStreetMap `landuse=construction` sites, and checks DfT Street Manager for any month published since the
   newest one stored (DfT publishes the archive monthly, so digs move once a month; the check runs every day). New
   works are linked to segments and appended to `works_segments.parquet`. A source that cannot be reached keeps the
   previous build's rows. The road card's "last 12 months" flags follow at once; the model's dig features count whole
   calendar years, so they change when the year turns.
4. **Never touches** the OpenStreetMap road network, flood zones, wards or DfT counts (no `--refresh` to the OSM ingest).
5. **Re-scores with the frozen models** in `data/models` (no retraining), rebuilds the Southampton prevention plan
   and what-if plans, the Brighton inspection list, and triages **new** open reports only (Gemini over the same
   model fallback list, cached in `outputs/lane_c/triage_<city>.json`; keyword rules without `GEMINI_API_KEY`).

**Safety.** Each run builds a new folder `data/live/<YYYYMMDDTHHMMSSZ>/` (same layout as the repo: `data/processed`,
`outputs`), starting from the previous build. It is validated against the committed snapshot: identical
`segment_id`s, same rows and columns in every risk file, no NaN in key columns, labels and weather never shrink.
Only then is the pointer file `data/live/current` switched. On any failure the previous build stays live, the
error goes to `data/live/refresh.log` and the command exits 1. The last 7 builds are kept. `data/live/` is
git-ignored: refreshed data is never committed.

**App.** If `data/live/current` points at a valid build, the app reads from it; otherwise it uses the committed
snapshot. The header shows "Data updated: <time>" and an amber STALE badge when the data is over 36 hours old.
Set `ROADSENSE_LIVE=0` to force the committed snapshot.

**Schedule it (Windows).** `scripts\schedule_refresh.bat` registers a Task Scheduler job, "RoadSense daily
refresh", at 06:00 every day (as the current user, while logged on). It runs `scripts\run_refresh.bat`, which
logs to `data\live\scheduler.log`. Remove it with `scripts\schedule_refresh.bat /delete`.

**as_of follows the calendar.** Each refresh sets `as_of` to the most recent Monday on or before today (UTC) and
scores the four weeks after it with the frozen models; the plan header shows that week. If an input cannot be
built for the new `as_of`, the refresh stops (exit 1, previous build kept) and the log says exactly what is
missing. Today that matters for Southampton: its history features count the SYNTHETIC council repair log, which
ends at the synthetic generator's `as_of` (`data/synthetic/_meta.json`, 2026-10-12). From the next Monday a
Southampton refresh stops until the synthetic data is regenerated to the new `as_of`; Brighton (real FixMyStreet
history) keeps refreshing with `--city Brighton` until its label table runs out (end of 2026).

## How well it works

All numbers below are on **real FixMyStreet reports the model never saw**, scored as the share of that year's
reports that fall on the model's top 10% of road length (about 60 km of Southampton's 600 km; chance = 10%),
against simply re-using last year's reports. Ranges are 95% paired segment bootstrap intervals, 1,000 resamples.

| Test | Model | Last year's reports | Difference | Model ahead in |
|---|---|---|---|---|
| **Brighton 2023–25** (802 reports) | 31.8% (27.9–35.6) | 21.7% (18.6–26.4) | +10.1 (+5.6 to +12.9) | 100% |
| **Southampton 2018–19** (381 reports, 262 on non-main roads) | 37.1% (29.7–43.9) | 33.7% (24.5–38.9) | +3.4 (−1.0 to +11.5) | 94% |
| Leave-one-city-out: Southampton | 34.8% | 33.5% | +1.3 (−0.9 to +6.0) | 94% |
| Leave-one-city-out: Brighton | 19.5% | 16.5% | +3.0 (−2.1 to +7.7) | 89% |
| Leave-one-city-out: Portsmouth | 19.9% | 13.1% | +6.8 (−3.6 to +13.8) | 87% |
| Frozen 2026 check, Southampton (33 reports) | 21.2% | 12.1% | +9.1 (−7.4 to +24.0) | 78% |

Model = the model with building sites and utility digs as inputs (since 18 Oct). Before they were added it scored
Brighton 31.2% and Southampton 41.6% (+7.9, +3.1 to +15.5) on the same test years; the paired difference for adding
them is +0.6 (−2.2 to +3.7) in Brighton and −4.5 (−7.4 to −1.1) in Southampton (`reports/context_eval.md`).
Set `risk.context_features: false` in `config/synthetic.yaml` and re-run `make train plan_risk plan` to go back.

What this does and doesn't show:
- With a city's own history, the model beats last year's reports clearly in Brighton; in Southampton it is ahead in
  94% of resamples but the interval now includes 0 (it excluded 0 before building sites and digs were added).
- In a city it has never seen, it is only roughly level with last year's reports: it needs local history.
- Without past-report features (physical inputs only) it is no better than chance in Southampton (10.4%), so we
  don't offer it as a cold-start fallback.
- The 2026 checks are too small to judge either way. We report them anyway.
- Counts measure FixMyStreet use, not true pothole rates, so scores rank segments within one city and year.

### Limits of FixMyStreet data

FixMyStreet is one of several ways residents report problems, so its reports are only part of the picture. mySociety,
which runs it, [warns against using the data to compare or rank areas](https://www.mysociety.org/2024/02/06/statement-from-mysociety-regarding-misuse-of-fixmystreet-data/). RoadSense therefore only ranks roads *within* a
city and never compares councils. Report status relies on residents marking problems fixed, so many "open" reports
are years old. The app only treats reports from the last 120 days as open.
[FixMyStreet itself notes](https://www.fixmystreet.com/about/connecting) that local road conditions can't be inferred
from its reports alone, which is why RoadSense combines reports with physical road data (traffic, buses, segment,
weather).

**Reproducible:** `python -m roadsense.models.real_risk` (fixed seed 2026) regenerates `reports/real_risk_eval.md`.
A second team member re-ran it from a clean `main` on 17 Oct and got identical numbers.

Other evaluations: weekly weather model on synthetic labels (pipeline check only, `reports/risk_eval.md`);
streetlights (`reports/lights_eval.md`: ranks no better than install year, its value is the replacement date);
report triage against keyword rules (`reports/triage_vs_keywords.md`; keyword rules 91/100 on hand-labelled real
reports, Gemini comparison not run in time); running cost (`reports/cost.md`: measured tokens at the published Gemini price, about £0.0006 per triaged report;
under £3 a year even at Southampton's full yearly repair volume of about 3,100 to 4,200 repairs, an upper bound because
these are repairs, not reports).

## What is real and what is synthetic

Everything public is real. The only synthetic parts are records the council already holds but doesn't publish.

| Part | Source | Status |
|---|---|---|
| Road network, road class, bus routes, junctions, cycle routes, schools/hospitals | OpenStreetMap | Real |
| Traffic load | DfT counts | Real for 314 Southampton segments; road-class median elsewhere, shown as "modelled estimate" |
| Flood exposure, wards, weather (history + 16-day forecast) | EA, MapIt/ONS, Open-Meteo | Real |
| Training and test labels, every number in "How well it works" | FixMyStreet | Real |
| Model that ranks the Southampton plan (`risk_latest`) | trained on real FixMyStreet reports, 3 cities | Real (`trained_on = real`) |
| Southampton's recent pothole history (2020–26) fed to that model | council repair log stand-in | **SYNTHETIC** (`history_source = synthetic`): residents moved off FixMyStreet after 2019, so real recent history is too thin |
| Week-to-week movement in the plan; what-if scenarios | synthetic-trained weekly weather model, used as a ward-level multiplier/ratio | **SYNTHETIC** |
| Road wear, resurfacing history, streetlight register and faults, crew outcomes | `data/synthetic/` (seed 2026) | **SYNTHETIC** |
| Treatment £/m² | our assumptions | Assumption, labelled in the app |
| Brighton map | real-label model with real FixMyStreet history | Real (`uses_synthetic = false`); yearly scale assumed (`scale_source`) |
| Building sites near a road | brownfield land registers (planning.data.gov.uk) and OpenStreetMap `landuse=construction` | Real; model input (registers) and map/card flags |
| Utility and highway digs on a road | DfT Street Manager open data, completed works, mid-2020 on | Real; model input and card flags |

Every synthetic row carries `is_synthetic = true` and a `source = "synthetic:<generator>@<version>"` tag, and every
screen that uses one shows a **SYNTHETIC** badge. The dashboard header reads "trained on real reports · history
synthetic". Confirmations from simulated crew feedback are labelled "Simulated crew report" and left out of the
confirmed-pothole count. In deployment the synthetic parts are replaced by the council's own repair log, inspection
records and asset register. All assumptions and gaps are listed in `DATA_GAPS.md`.

## Data sources and licences

All data was retrieved during the hackathon (16–18 Oct 2026). Exact retrieval timestamps are in `data/processed/sources.json`. Nothing was collected or processed before hacking started on Fri 16 Oct, 20:00.

| Source | What we use it for | Coverage / dates | Licence and attribution |
|---|---|---|---|
| **FixMyStreet** (mySociety), Open311 API `fixmystreet.com/open311/v2/requests.json` | Pothole reports, used as training and test labels. Agencies: Southampton 2567, Portsmouth 2657, Brighton & Hove 2614 | Southampton train 2015–17, test 2018–19; Brighton train ≤2022, test 2023–25; Portsmouth extra training. Retrieved 17 Oct 2026, 11:00 UTC | No licence stated. FixMyStreet names its Open311 API as the route for extracting report data (https://www.fixmystreet.com/about/connecting); we use that API, not scraping. Reports attributed to mySociety. |
| **OpenStreetMap** | Road network split into segments (22,334 across 3 cities), road class (`is_main_road`), cycle routes, schools and hospitals | Extract retrieved 17 Oct 2026, 12:03 UTC | © OpenStreetMap contributors, Open Database Licence (ODbL) 1.0. openstreetmap.org/copyright |
| **DfT road traffic statistics** | Traffic load per segment (AADF incl. HGVs). 314 Southampton segments have a direct count; the rest use road-class medians, shown in the app as "modelled estimate" | Count year up to 2025, retrieved 17 Oct 2026, 12:22 UTC | Open Government Licence v3.0. Contains public sector information licensed under the OGL v3.0 |
| **Environment Agency Flood Map for Planning: Flood Zone 3** | Flood exposure feature | Retrieved 17 Oct 2026, 12:45 UTC | Open Government Licence v3.0. © Environment Agency copyright and/or database right. Contains OS data © Crown copyright and database right |
| **Open-Meteo** | Historical weekly weather (rain, freeze–thaw) and 3-week forecast | Historical: range in `data/processed/sources.json`; forecast retrieved 17 Oct 2026, 12:36 UTC | Data CC BY 4.0. Weather data by Open-Meteo.com. Free API used for non-commercial purposes |
| **MapIt** (mySociety) | Ward boundaries, Southampton and Brighton | Retrieved 17 Oct 2026, 12:06 UTC | Boundaries derived from OS Boundary-Line, OGL v3.0. Contains OS data © Crown copyright and database right |
| **ONS ward boundaries** | Ward boundaries, Portsmouth (MapIt free quota ran out mid-hackathon) | December 2024 ward boundaries, retrieved 17 Oct 2026, 15:05 UTC | Source: Office for National Statistics, licensed under OGL v3.0. Contains OS data © Crown copyright and database right |
| **DfT Street Manager open data** (permit archive, `opendata.manage-roadworks.service.gov.uk`) | Completed utility and highway works on each road (`roadsense.ingest.streetworks`) | July 2020 onwards, monthly; read Oct 2026, nothing raw kept | Open Government Licence v3.0 |
| **Brownfield land registers** via planning.data.gov.uk | Housing and development sites with permission dates near each road (`roadsense.ingest.construction`) | Brighton 253 sites; Southampton 37 (last updated 2017). Retrieved 18 Oct 2026 | Open Government Licence v3.0 |
| **OpenStreetMap** `landuse=construction` (Overpass API) | Building sites active now, both cities | Retrieved 18 Oct 2026 | © OpenStreetMap contributors, ODbL 1.0 |
| **Google Gemini API** | Report triage (classification, draft work orders) | Model fallback list `gemini-3.8-flash` → `gemini-3-flash-preview` → `gemini-flash-latest` | Gemini API terms. Only public FixMyStreet text and synthetic data are sent |

### Figures quoted in the pitch (not model inputs)
- Southampton pothole repairs 2021–25 and driver survey: Blackcircles analysis of council FOI data, 18 Mar 2026
- Government pothole ratings (Southampton amber, Portsmouth green): The News, 12 Jan 2026
- Planned vs reactive repair cost (£56.94 vs £87.80) and backlog (£18.62bn): ALARM survey 2025 / 2026

## Privacy

- Residents' full report descriptions (`data/raw/fms_raw.csv`) never leave the machine that pulled them: the file is
  git-ignored and has never been committed.
- Report titles in the current data are redacted: house numbers become `[number]`, postcodes `[postcode]` and names
  `[name]` (`roadsense/prevent/redact_titles.py`; the ingest step redacts on every rebuild).
- Titles are redacted before they are stored, displayed or sent to Gemini; report descriptions are not stored.
- Only public FixMyStreet text and synthetic data are sent to Gemini. API keys live in `.env`, which is git-ignored.

## AI-generated code and AI in the product

All code in this repository was written with Claude Code (Anthropic), run by three team members, one per part:
data (lane A), models (lane B), product (lane C). Commits made through Claude Code carry a `Co-Authored-By: Claude`
line. Team members chose the approach, reviewed and ran the code, checked the data, and made the decisions recorded
in `STATUS.md`, `DATA_GAPS.md` and `schemas.md`. Claude (chat) was also used for planning, code review and merging.

What the team did:
- Chose the problem and user (Southampton highways officers / Balfour Beatty Living Places) and defined the build plan
  (milestones, lanes, merges) the agents worked from.
- Chose and checked the data sources, including spotting that Southampton's FixMyStreet reports collapse after 2019.
- Set the honesty rules: synthetic inputs are labelled in the app, train/test periods never overlap, and we don't claim
  results the evaluation doesn't support.
- Split the work into three lanes with a shared data contract, and reviewed and merged every branch, with tests run
  before each merge.
- Checked the evidence used in the pitch against primary sources.
- Redacted personal details from report text in the current data; earlier commits may contain unredacted
  FixMyStreet report titles, which were already public on FixMyStreet.

In the product, the risk model is a LightGBM classifier we trained; Google Gemini is used only for report triage: results are
cached and shown with the method used (Gemini or keyword fallback), and an officer approves or rejects every work order.
The 100 triage evaluation labels were drafted by Claude Code from report titles and were not independently spot-checked.

## What existed before the event

Nothing. All data was retrieved and all code written during the hackathon. Commit hashes and dates are the originals.

## Team

- Nilen Rao: data pipeline, merges
- Aryan Patel: models
- Armaan Fernandes: product (API, dashboard, triage)

## Who helped

No outside help beyond the event's mentors and organisers.
