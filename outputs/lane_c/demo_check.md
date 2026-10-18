# Demo dry run

_2026-10-18T06:02:05+00:00, `python -m roadsense.api.demo_check`_

| Step | Result | Notes |
| --- | --- | --- |
| 1 ward plan with sources | PASS | 39 rows in Millbrook; 14 sources in footer (1.9 s) |
| 2 top segment detail | PASS | Millbrook Flyover / Redbridge Road: urgency 40.2, wear 75.9%, 3 reasons, Patch now, schedule resurfacing (0.0 s) |
| 3 rain_then_frost moves heavy corridors up | PASS | p30 peak +0.079 on top-10% load vs +0.032 elsewhere; bus segments +0.082; method risk_model (0.0 s) |
| 4 triage real report, merge duplicate, approve | PASS | 'Large pothole' -> pothole via keyword_baseline, segment SOU-2235-01 (0.7 m), duplicate merged, approved (0.3 s) |
| 5 crew outcome re-scores segment | PASS | SOU-2235-01: urgency 70.0 -> 100.0 (0.1 s) |
| 6 streetlights due and dark | PASS | 3695 lamps due in 90 days, 65 currently dark (0.0 s) |
| F1 outside coverage / National Highways | PASS | London -> out_of_coverage; Nursling Spur Motorway -> National Highways, not scored (0.0 s) |
| F2 stale forecast banner | PASS | STALE: forecast retrieved 6966 h ago (limit 24 h). Plan is provisional. (serve with --forecast-as-of to show it) (0.0 s) |
| F3 no nearby traffic count | PASS | 7,227 segments show 'modelled estimate, low confidence' (0.0 s) |
| F4 cached copy labelled with retrieval date | PASS | OpenStreetMap: Road network, bus stops and bus routes: © OpenStreetMap contributors (ODbL) (cached, retrieved 2026-10-17), MapIt (mySociety) ward boundaries: Ward boundaries: MapIt / ONS, contains OS data © Crown copyright (OGL) (cached, retrieved 2026-10-17), ONS Open Geography Portal ward boundaries: Portsmouth ward boundaries: ONS, contains OS data © Crown copyright (OGL) (cached, retrieved 2026-10-17), DfT road traffic statistics: Traffic counts: Department for Transport, AADF to 2025 (OGL) (cached, retrieved 2026-10-17), Open-Meteo: Weather: Open-Meteo.com (CC BY 4.0), historical + 16-day forecast (cached, retrieved 2026-10-17), Environment Agency Flood Map for Planning: Flood Zone 3: Environment Agency (OGL) (cached, retrieved 2026-10-17), FixMyStreet (mySociety): Pothole reports: FixMyStreet.com (mySociety), via its public Open311 API (cached, retrieved 2026-10-17), DfT Street Manager: Street works: DfT Street Manager open data (OGL) (cached, retrieved 2026-10-18), Brownfield land registers: Building sites: brownfield land registers via planning.data.gov.uk (OGL) (cached, retrieved 2026-10-18), OpenStreetMap construction sites: Building sites: OpenStreetMap landuse=construction (ODbL) (cached, retrieved 2026-10-18) (0.0 s) |
