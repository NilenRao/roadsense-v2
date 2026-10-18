# RoadSense pipeline. Commands come from each lane's own modules (see schemas.md and STATUS.md).
# Windows without make: run the python -m lines directly in the .venv.
PY ?= python
CITY ?= Southampton
CITIES ?= Southampton Brighton Portsmouth
SCENARIO ?= rain_then_frost

.PHONY: help ingest features synthetic train predict plan_risk plan whatif lights serve retrain triage-eval cost demo-check test all context context-eval

help:  ## list every target
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ :/'

ingest:  ## lane A: OSM, wards, weather, flood, FixMyStreet (CITIES=...)
	$(PY) -m roadsense.ingest.osm $(CITIES)
	$(PY) -m roadsense.ingest.weather $(CITIES)
	$(PY) -m roadsense.ingest.flood $(CITIES)
	$(PY) -m roadsense.ingest.fixmystreet

features:  ## lane A: segments, wards, context, load, weekly weather, labels
	$(PY) -m roadsense.features.segments $(CITIES)
	$(PY) -m roadsense.features.wards $(CITIES)
	$(PY) -m roadsense.features.context $(CITIES)
	$(PY) -m roadsense.features.load
	$(PY) -m roadsense.features.weather_feats $(CITIES)
	$(PY) -m roadsense.features.labels

synthetic:  ## lane B: synthetic council data (seed 2026)
	$(PY) -m roadsense.synthetic

train:  ## lane B: risk + wear models, real-label model, evals
	$(PY) -m roadsense.models.risk
	$(PY) -m roadsense.models.real_risk

predict: train  ## lane B: risk_<date>.parquet is written by train

plan_risk:  ## lane B: rank risk_latest with the real-label model (synthetic output kept as risk_synthetic_latest)
	$(PY) -m roadsense.models.plan_risk

lights:  ## lane B: streetlight Weibull model
	$(PY) -m roadsense.models.lights

plan:  ## lane C: weekly prevention plan CSV
	$(PY) -m roadsense.prevent.plan

whatif:  ## lane C: what-if (SCENARIO=rain_then_frost|wet_week|cold_snap)
	$(PY) -m roadsense.prevent.whatif --scenario $(SCENARIO)

serve:  ## lane C: API + officer page on http://127.0.0.1:8000 (FORECAST_AS_OF=... shows STALE)
	$(PY) -m roadsense.api $(if $(FORECAST_AS_OF),--forecast-as-of $(FORECAST_AS_OF),)

retrain:  ## lane C: retrain with crew outcomes, promote only if validation improves
	$(PY) -m roadsense.api.retrain

triage-eval:  ## lane C: Gemini vs keyword baseline on 100 hand-labelled reports
	$(PY) -m roadsense.agent.triage

cost:  ## lane C: reports/cost.md from the token log
	$(PY) -m roadsense.agent.cost

demo-check:  ## lane C: dry run of the 6 demo steps and 4 failure cases
	$(PY) -m roadsense.api.demo_check

context:  ## building sites (brownfield registers, OSM) and utility digs (DfT Street Manager, ~1 GB a month streamed), linked to segments
	$(PY) -m roadsense.ingest.construction
	$(PY) -m roadsense.ingest.streetworks
	$(PY) -m roadsense.features.works_context

context-eval:  ## held-out check: do building sites or utility digs help the ranking? (reports/context_eval.md)
	$(PY) -m roadsense.models.context_eval

test:  ## pytest (small fixtures, no network)
	$(PY) -m pytest -q

# Run order (lane B, 17 Oct): synthetic, risk, plan_risk, synthetic again (crew outcomes select from the model), lights
all: ingest features synthetic train plan_risk synthetic lights plan  ## full pipeline
