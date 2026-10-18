# Demo video script (2:30, spec timings)

Values below come from `main` at 81d6e10 (risk model `risk@2026-10-18T07:20`, retrained with building sites and
utility digs as inputs), checked against the running app on 18 Oct 2026 (`demo_check` 10/10). A re-run changes the ranking, so open the page first and check the
names in bold. `submission/video_storyboard.md` is the newer, fuller script.

## Before recording

```bash
python -m roadsense.api.demo_check     # must print 10/10 passed
python -m roadsense.api                # wait for "warming: ..." then open http://127.0.0.1:8000
```

- POST `/demo/reset` (or delete `outputs/lane_c/state.json`) so the triage queue starts empty.
- Browser at 1280 px wide, zoom 100%, OSM tiles loaded once so the map is cached. If the plan table looks cramped,
  drag the divider between the side panel and the map to widen it (double-click the divider resets it).
- Second tab ready for the failure case: `http://127.0.0.1:8000/?forecast_as_of=2026-10-15T06:00:00`.

## 0:00 to 0:20 Problem (20 s)

Screen: the app on "All wards" (map of 8,135 coloured segments).

> "Southampton fills over 4,000 potholes a year, and in four of the last five years it spent nothing on prevention.
> A reactive repair costs £87.80, a planned one £56.94. RoadSense tells the highways officer which streets to treat
> this week, before they fail."

## 0:20 to 1:50 Live demo, steps 1 to 5 (90 s)

1. **Pick a ward (15 s).** Ward dropdown: choose **Bargate** (the city centre: 9 of the city's top 50). Point at the
   heading: "ranked by a model trained on real reports, history synthetic".
   Point at the footer: "every source with its date: OpenStreetMap, DfT counts, Open-Meteo, Environment Agency,
   FixMyStreet, and SYNTHETIC badges where council data is simulated."
2. **Open the top segment (20 s).** Click row 1, **Platform Road**, urgency 42, badge "Pays back ×1.14", treatment
   "Patch now, schedule resurfacing". Detail card: wear (SYNTHETIC), the three reasons, traffic confidence,
   and the four forecast weeks marked f (Open-Meteo forecast) and c (climatology). Then the **Worth treating** view (next to
   **All roads** at the top of the plan): "7 roads pay back · 3 confirmed potholes", confirmed potholes first.
3. **What-if (20 s).** Ward back to **All wards**, What-if: **Rain then frost**. The "Biggest risk rises" box shows
   the busy corridors jumping, e.g. **Blechynden Terrace / West Park Road** 42 → 87. Say: "heavy port traffic on wet, frozen roads: the corridors move to the top." Set What-if back to
   "This week's forecast".
4. **Triage a real report (20 s).** Tab **Report triage**. Pick **2026-07-29 · Large pothole** (FixMyStreet report
   9864063), click **Triage report**. The card shows: pothole, attached to segment SOU-2235-01 (0.7 m away), urgency
   from the risk model, draft work order JSON. Click **Re-submit (duplicate)**: the second card says "Merged into
   fms-9864063: same defect within 15 m and 14 days". Click **Approve work order**: the map flies to the segment, now
   held at urgency 70 (inspect).
5. **Crew outcome (15 s).** In the segment card click **Pothole** under "Record crew outcome". The card shows
   "Re-scored: urgency 70 (high) → 100 (critical), treatment permanent_repair."

(Step 6, streetlights, if time allows: tab **Streetlights**: "3,695 lamps due within 90 days, 65 currently dark",
SYNTHETIC badge, click a lamp to fly to it.)

## 1:50 to 2:10 Failure case (20 s)

Pick one:

- **Outside coverage (recommended, fastest).** Click the map out in the Solent or past the city edge: the card says
  "Outside coverage". Click the grey **Nursling Spur Motorway** (M271): "National Highways road, not scored".
- **Stale forecast.** Switch to the second tab: red banner "STALE: forecast retrieved ... h ago (limit 24 h). Plan is
  provisional."

## 2:10 to 2:30 Business case (20 s)

> "The council or its contractor, Balfour Beatty Living Places on a £60m contract to 2030, pays an annual licence.
> Running cost is in reports/cost.md: the token count is measured, the price is an assumption until confirmed. Every
> input is national open data, so a second city swaps its boundary and its own repair log for our synthetic one.
> On real reports it never saw, our top 10% of roads caught 32% of Brighton's potholes against 22% for last year's
> reports, and 37% against 34% in Southampton."

## Check before filming

- "Pays back" counts reactive repairs avoided over each treatment's service life (assumed lives, in prevent.yaml);
  only 7 roads pass ×1.0, mostly patches. Most roads are "Loss maker" because resurfacing is costed at an assumed
  £30/m². Say "ranked by reactive repairs avoided per £", not "everything saves money".
- Optional (10 s, if time): City picker → **Brighton**: risk map only, "100% real inputs"; plan, triage and lights say
  Southampton only.
- If Gemini is configured, step 4 says "gemini"; otherwise "keyword_baseline". Either is honest; mention which.
