"""RoadSense API (M8). Run with `python -m roadsense.api` (Makefile: `make serve`).

GET  /segments            GeoJSON (contract section 10); ward, band, scenario; ?detail=full, ?format=json
GET  /segments/{id}
GET  /locate              lat, lon -> segment, or out_of_coverage / National Highways
GET  /plan/prevention     ward, scenario (rain_then_frost, wet_week, cold_snap), forecast_as_of
GET  /lights              ward, due, dark
GET  /reports/open        currently open real FixMyStreet pothole reports (urgency 100)
GET  /reports/recent      latest real FixMyStreet reports, to pick one to triage
POST /reports             triage a report (fms_id, or title + lat + lon [+ image_b64])
GET  /triage/queue
POST /workorders/{id}/approve | /reject
POST /outcomes            {segment_id, outcome} or {crew_file: true}
GET  /sources             every dataset with source, date, licence and synthetic flag
GET  /meta                badges, freshness, wards
GET  /context/evaluation  held-out verdict for building sites and utility digs (reports/context_eval.md)
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from roadsense.api.service import Service

WEB = Path(__file__).resolve().parents[1] / "web"


def create_app(service: Service | None = None) -> FastAPI:
    app = FastAPI(title="RoadSense", version="0.1",
                  description="Weekly pothole prevention plan for Southampton highways officers")
    holder: dict = {"svc": service}

    def svc() -> Service:
        if holder["svc"] is None:
            from roadsense.agent.triage import gemini_client
            holder["svc"] = Service(client=gemini_client(), forecast_as_of=os.environ.get("ROADSENSE_FORECAST_AS_OF"))
        return holder["svc"]

    if service is None and os.environ.get("ROADSENSE_WARM", "1") == "1":
        @app.on_event("startup")
        def warm():
            print("warming:", svc().warm())

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/meta")
    def meta(forecast_as_of: str | None = None):
        s = svc()
        sc = s.scored
        return {"city": s.cfg["coverage"]["city"], "as_of": s.risk_meta.get("as_of"), "badges": s.badges(),
                "freshness": s.freshness(forecast_as_of), "wards": sorted(sc["ward"].dropna().unique().tolist()),
                "scenarios": list(s.cfg["whatif"]["scenarios"]), "gemini": s.client is not None,
                "bands": {b: int((sc["band"] == b).sum()) for b in ["critical", "high", "prevention", "monitor"]},
                "official_report_url": s.cfg["coverage"]["official_report_url"],
                "official_streetlight_url": s.cfg["coverage"]["official_streetlight_url"],
                "cities": [s.cfg["coverage"]["city"]] + s.other_cities(),
                "data_updated": s.data_updated()}

    @app.get("/segments")
    def segments(ward: str | None = None, band: str | None = None, scenario: str | None = None,
                 format: str = Query("geojson", pattern="^(json|geojson)$"), limit: int | None = None,
                 detail: str = Query("map", pattern="^(map|full)$"), city: str | None = None):
        s = svc()
        if city and city != s.cfg["coverage"]["city"]:
            try:
                return s.city_view(city).geojson()
            except FileNotFoundError:
                raise HTTPException(404, f"no risk file for {city}")
        return s.segments(ward=ward, band=band, scenario=scenario, geojson=format == "geojson", limit=limit,
                              full=detail == "full")

    @app.get("/segments/{segment_id}")
    def segment(segment_id: str, scenario: str | None = None, city: str | None = None):
        s = svc()
        if city and city != s.cfg["coverage"]["city"]:
            try:
                r = s.city_view(city).segment(segment_id)
            except FileNotFoundError:
                raise HTTPException(404, f"no risk file for {city}")
            if r is None:
                raise HTTPException(404, "unknown segment")
            return {**r, "context": s.works_context(city).get(segment_id)}
        r = s.segment(segment_id, scenario=scenario)
        if r is None:
            raise HTTPException(404, "unknown segment")
        return r

    @app.get("/locate")
    def locate(lat: float, lon: float):
        return svc().locate(lat, lon)

    @app.get("/plan/prevention")
    def plan(ward: str | None = None, scenario: str | None = None, forecast_as_of: str | None = None):
        try:
            return svc().plan(ward=ward, scenario=scenario, forecast_as_of=forecast_as_of)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/lights")
    def lights(ward: str | None = None, due: bool = False, dark: bool = False, limit: int = 500,
               due_days: int | None = None):
        return svc().lights_view(ward=ward, due=due, dark=dark, limit=limit, due_days=due_days)

    @app.get("/reports/open")
    def open_reports(city: str | None = None):
        return svc().open_reports(city=city)

    @app.get("/plan/inspection")
    def inspection(city: str, top_n: int = 50):
        s = svc()
        if city == s.cfg["coverage"]["city"]:
            raise HTTPException(400, f"{city} has the prevention plan: use /plan/prevention")
        try:
            out = s.city_inspection(city, top_n=top_n)
            cx = s.works_context(city)
            for r in out["rows"]:  # display only
                r["context"] = cx.get(r["segment_id"])
            return out
        except FileNotFoundError:
            raise HTTPException(404, f"no risk file for {city}")

    @app.get("/triage/city")
    def city_triage(city: str):
        r = svc().city_triage(city)
        if r is None:
            raise HTTPException(404, f"no cached triage for {city}: run python -m roadsense.agent.city_triage {city}")
        return r

    @app.get("/reports/recent")
    def recent(n: int = 20):
        return svc().recent_fms(n)

    @app.post("/reports")
    def post_report(body: dict = Body(...)):
        if body.get("fms_id") is None and (body.get("lat") is None or body.get("lon") is None):
            raise HTTPException(422, "give fms_id, or lat and lon with the report text")
        try:
            return svc().triage_report(body)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))

    @app.get("/triage/queue")
    def queue(status: str | None = None):
        q = svc().queue
        return [x for x in q if status is None or x["status"] == status]

    @app.post("/workorders/{report_id}/approve")
    def approve(report_id: str, body: dict = Body(default={})):
        try:
            return svc().approve(report_id, body.get("officer", "officer"))
        except KeyError:
            raise HTTPException(404, "unknown work order")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/workorders/{report_id}/reject")
    def reject(report_id: str, body: dict = Body(default={})):
        try:
            return svc().reject(report_id, body.get("officer", "officer"))
        except KeyError:
            raise HTTPException(404, "unknown work order")

    @app.post("/outcomes")
    def outcomes(body: dict = Body(...)):
        s = svc()
        try:
            if body.get("crew_file"):
                return s.load_crew_outcomes(limit=body.get("limit"))
            return s.record_outcome(body["segment_id"], body["outcome"], body.get("inspection_date"))
        except KeyError as e:
            raise HTTPException(404, f"unknown {e}")
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))

    @app.get("/context/sites")
    def context_sites(city: str | None = None):
        from roadsense.features import works_context as WC
        s = svc()
        c = city or s.cfg["coverage"]["city"]
        from roadsense.prevent import inputs as I
        sites, _, _ = WC.load_linked(processed_dir=str(I.resolve(s.cfg, "processed_dir", s.root)))
        as_of = s.risk_meta.get("as_of") if c == s.cfg["coverage"]["city"] else s.city_view(c).meta.get("as_of")
        return {"city": c, "as_of": as_of, "years": WC.CON_YEARS, "sites": WC.live_sites(c, as_of, sites)}

    @app.get("/context/evaluation")
    def context_evaluation():
        return svc().context_verdict()

    @app.get("/sources")
    def sources():
        return svc().sources()

    @app.post("/demo/reset")
    def reset():
        svc().reset()
        return {"ok": True}

    if WEB.exists():
        app.mount("/static", StaticFiles(directory=WEB), name="static")

        @app.get("/")
        def index():
            return FileResponse(WEB / "index.html")

    return app


app = create_app()
