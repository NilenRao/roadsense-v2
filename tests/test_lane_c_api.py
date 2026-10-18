"""Lane C, M8: triage agent, API, feedback loop and cost report. Fake Gemini client, no network."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from google.genai import types

from roadsense.agent import cost as C
from roadsense.agent import triage as TR
from roadsense.api import retrain as RT
from roadsense.api.main import create_app
from roadsense.api.service import Service
from roadsense.prevent import inputs as I
from roadsense.prevent.mock import mock_world


class _Busy(Exception):
    code = 503


class FakeGemini:
    """Plays the function-calling loop: lookup -> duplicates -> risk -> draft_work_order."""

    def __init__(self, category="pothole", fail_models=()):
        self.category, self.calls, self.fail_models, self.models_seen = category, [], set(fail_models), []
        self.models = self

    def generate_content(self, model, contents, config):
        assert config.tools and config.tool_config.function_calling_config.mode == "ANY"
        self.models_seen.append(model)
        if model in self.fail_models:
            raise _Busy()
        self.calls.append(contents)
        if config.tool_config.function_calling_config.allowed_function_names == ["draft_work_order"]:
            self.calls = [None, None, None] + self.calls  # fast mode: answer with the work order straight away
        text = contents[0].parts[0].text
        lat = float(text.split("lat ")[1].split(",")[0])
        lon = float(text.split("lon ")[1].split(".\n")[0])
        n = len(self.calls)
        if n == 1:
            fc = types.FunctionCall(name="lookup_location", args={"lat": lat, "lon": lon})
        elif n == 2:
            fc = types.FunctionCall(name="find_duplicates", args={"lat": lat, "lon": lon, "reported_at": "2026-10-12"})
        elif n == 3:
            fc = types.FunctionCall(name="get_risk", args={"segment_id": "X003-004"})
        else:
            fc = types.FunctionCall(name="draft_work_order", args={
                "category": self.category, "confidence": 0.9, "asset_type": "segment", "asset_id": "INVENTED",
                "duplicate_of": "", "hazard": True, "summary": "Deep pothole in nearside lane",
                "recommended_action": "Permanent repair"})
        content = types.Content(role="model", parts=[types.Part(function_call=fc)])
        usage = SimpleNamespace(prompt_token_count=400 + 50 * n, candidates_token_count=30, thoughts_token_count=0,
                                total_token_count=430 + 50 * n)
        return SimpleNamespace(function_calls=[fc], candidates=[SimpleNamespace(content=content)], usage_metadata=usage)

    def reset(self):
        self.calls = []


@pytest.fixture
def root(tmp_path):
    """A repo-shaped folder: mock world files plus a few real FixMyStreet rows."""
    w = mock_world()
    seg = w["segments"].iloc[30:40]          # mock road 4 (tertiary), council
    fms = pd.DataFrame({
        "city": "Southampton", "id": [111, 112, 113],
        "category": "Potholes", "requested": ["2026-10-10T09:00:00+00:00", "2026-10-11T09:00:00+00:00", "2026-01-01T00:00:00+00:00"],
        "updated": "2026-10-12T00:00:00+00:00", "status": "open",
        "lat": [seg["lat"].iloc[4] + 5 / I.M_PER_DEG_LAT, seg["lat"].iloc[4] + 8 / I.M_PER_DEG_LAT, 51.5],
        "lon": [seg["lon"].iloc[4], seg["lon"].iloc[4] + 3 / I.M_PER_DEG_LON, -0.1],
        "title": ["Large pothole in road", "Pothole", "Pothole in London"], "surface_keyword": True})
    (tmp_path / "data" / "processed").mkdir(parents=True)
    fms.to_csv(tmp_path / "data" / "processed" / "fms_roads_clean.csv", index=False)
    (tmp_path / "outputs").mkdir()
    w["risk"].to_parquet(tmp_path / "outputs" / "risk_2026-10-12.parquet")
    w["lights"].to_parquet(tmp_path / "outputs" / "lights_2026-10-12.parquet")
    w["segments"].to_parquet(tmp_path / "data" / "processed" / "segments.parquet")
    (tmp_path / "data" / "synthetic").mkdir()
    pd.DataFrame({"outcome_id": ["CO1", "CO2"], "segment_id": ["X003-001", "X003-002"], "inspection_date": "2026-10-20",
                  "selection": "random", "outcome": ["pothole", "none"], "is_synthetic": True,
                  "source": "synthetic:crew@1.0"}).to_csv(tmp_path / "data" / "synthetic" / "crew_outcomes.csv", index=False)
    return tmp_path


def test_keyword_baseline():
    assert TR.keyword_classify("Large pothole in road") == "pothole"
    assert TR.keyword_classify("Street light not working") == "streetlight"
    assert TR.keyword_classify("Loose pavement slabs") == "other"
    assert TR.keyword_classify("Blocked drain") == "other"


def test_labels_file_has_100_reports():
    lab = pd.read_csv(I.ROOT / "roadsense" / "agent" / "eval" / "triage_labels.csv")
    assert len(lab) == 100 and set(lab["label"]) <= set(TR.CATEGORIES)
    res = TR.evaluate()
    assert 0 < res["keyword_baseline"]["accuracy"] <= 1


def test_end_to_end_triage_attach_approve_outcome_rescore(root):
    fake = FakeGemini()
    svc = Service(root=root, client=fake)
    app = TestClient(create_app(svc))

    # 1. triage a real report with Gemini function calling
    wo = app.post("/reports", json={"fms_id": 111}).json()
    assert wo["method"] == "gemini" and wo["category"] == "pothole"
    # fast mode: the code runs the three lookups, Gemini answers in one call
    assert wo["tool_trace"] == ["prefetch:lookup_location", "prefetch:find_duplicates", "prefetch:get_risk",
                                "draft_work_order"]
    assert wo["tokens"]["calls"] == 1 and wo["model"] == "gemini-3.8-flash"
    # 2. attached to its segment by code, never by the model's invented id
    assert wo["segment_id"] == "X003-004" and wo["segment_id"] != "INVENTED"
    assert wo["status"] == "pending_approval" and wo["urgency"] >= 70
    assert wo["official_report_url"].startswith("https://www.southampton.gov.uk")
    assert wo["tokens"]["total_tokens"] > 0
    # 3. a second real report 3 m away the next day merges as a duplicate
    fake.reset()
    dup = app.post("/reports", json={"fms_id": 112}).json()
    assert dup["status"] == "merged" and dup["merged_into"] == "fms-111"
    assert dup["method"].startswith("merged into fms-111") and dup["category"] == "pothole" and fake.calls == []
    # 4. officer approves
    before = app.get("/segments/X003-004").json()
    r = app.post("/workorders/fms-111/approve", json={"officer": "test"}).json()
    assert r["work_order"]["status"] == "approved"
    assert r["segment"]["urgency"] >= max(70, before["urgency"])
    assert app.post("/workorders/fms-111/approve").status_code == 409
    worth = app.get("/plan/prevention").json()["worth"]
    assert any(r["segment_id"] == "X003-004" and r["confirmed_by"] == "gemini" for r in worth["rows"])
    # 5. crew outcome re-scores the segment
    out = app.post("/outcomes", json={"segment_id": "X003-004", "outcome": "pothole"}).json()
    assert out["after"]["urgency"] == 100 and out["after"]["band"] == "critical"
    assert out["after"]["treatment"] == "permanent_repair"
    seg = app.get("/segments/X003-004").json()
    assert seg["confirmed_open"] and seg["outcomes"][0]["outcome"] == "pothole"
    assert app.get("/triage/queue").json()[0]["status"] == "inspected"
    # tokens were logged for the cost report
    log = C.read_log(svc.cfg, root)
    assert len(log) == 1 and (log["kind"] == "triage").all()  # the duplicate made no Gemini call
    labels = pd.read_csv(root / "outputs" / "lane_c" / "outcome_labels.csv")
    assert labels["segment_id"].tolist() == ["X003-004"]


def test_state_survives_restart(root):
    svc = Service(root=root)
    svc.triage_report({"fms_id": 111})
    svc.approve("fms-111")
    again = Service(root=root)
    assert again.queue[0]["status"] == "approved"
    assert again.segment("X003-004")["urgency"] >= 70


def test_keyword_fallback_without_gemini(root):
    app = TestClient(create_app(Service(root=root)))
    wo = app.post("/reports", json={"title": "Large pothole in the road", "lat": 50.9002, "lon": -1.4395,
                                     "reported_at": "2026-10-12"}).json()
    assert wo["method"] == "keyword_baseline" and wo["category"] == "pothole"
    assert app.post("/reports", json={"title": "x", "lat": 1, "lon": 1, "method": "gemini"}).status_code == 503


def test_failure_cases(root):
    app = TestClient(create_app(Service(root=root, forecast_as_of="2026-10-01T06:00:00+00:00")))
    assert app.get("/locate", params={"lat": 51.5, "lon": -0.1}).json()["status"] == "out_of_coverage"
    mw = app.get("/segments", params={"band": "not_scored", "format": "json"}).json()["segments"]
    m = mw[0]
    loc = app.get("/locate", params={"lat": m["lat"], "lon": m["lon"]}).json()
    assert loc["status"] == "national_highways" and "not scored" in loc["message"]
    wo = app.post("/reports", json={"fms_id": 113}).json()
    assert wo["status"] == "out_of_coverage"
    plan = app.get("/plan/prevention").json()
    assert plan["freshness"]["stale"] and plan["provisional"]
    seg = app.get("/segments", params={"limit": 5, "format": "json"}).json()["segments"]
    assert all(s["traffic_note"] in ("DfT count on this segment", "modelled estimate, low confidence") for s in seg)


def test_read_endpoints(root):
    app = TestClient(create_app(Service(root=root)))
    meta = app.get("/meta").json()
    assert meta["badges"]["synthetic"] and meta["wards"]
    gj = app.get("/segments").json()
    assert gj["type"] == "FeatureCollection" and gj["features"][0]["geometry"]["type"] == "LineString"
    w = meta["wards"][0]
    assert all(f["properties"]["ward"] == w for f in app.get("/segments", params={"ward": w}).json()["features"])
    full = app.get("/segments", params={"detail": "full", "limit": 3}).json()["features"][0]["properties"]
    assert {"p30", "wear", "urgency", "band", "load_confidence", "uses_synthetic"} <= set(full)
    one = app.get("/segments/X003-004").json()
    assert one["type"] == "Feature" and len(one["risk_weeks"]) == 4 and "load_confidence" in one["load"]
    plan = app.get("/plan/prevention", params={"scenario": "rain_then_frost"}).json()
    assert plan["scenario_method"] == "sensitivity_surrogate"
    assert plan["movers"] and plan["movers"][0]["urgency_scn"] >= plan["movers"][0]["urgency_base"]
    base = app.get("/plan/prevention").json()
    assert {"pays_back", "confirmed_pothole", "confirmed_by", "worth_treating"} <= set(base["rows"][0])
    assert base["worth"]["header"].endswith("reactive cost avoided") and "service life" in base["horizon"]
    assert app.get("/plan/prevention", params={"scenario": "nope"}).status_code == 400
    lights = app.get("/lights", params={"due_days": 90}).json()
    assert lights["synthetic"] and all(l["due"] or l["replacement_overdue"] for l in lights["lamps"])
    lamp = app.get("/lights", params={"limit": 1}).json()["lamps"][0]
    assert {"lamp_type", "replace_by", "dark_source", "uses_synthetic"} <= set(lamp)
    srcs = app.get("/sources").json()
    names = {s["name"] for s in srcs}
    assert {"segments", "risk", "lights", "fixmystreet_reports", "weather_forecast"} <= names
    assert all({"name", "used_for", "licence", "retrieved_at", "data_date", "is_synthetic"} <= set(s) for s in srcs)
    assert app.get("/segments/nope").status_code == 404
    assert app.get("/").status_code == 200


def test_bulk_crew_outcomes(root):
    app = TestClient(create_app(Service(root=root)))
    r = app.post("/outcomes", json={"crew_file": True}).json()
    assert r["recorded"] == 2
    assert app.get("/segments/X003-001").json()["urgency"] == 100
    assert app.post("/outcomes", json={"crew_file": True}).json()["recorded"] == 0  # idempotent


def test_cost_report(root):
    cfg = I.load_config()
    C.write_cost_report(cfg, root)
    assert "Nothing measured yet" in (root / "reports" / "cost.md").read_text(encoding="utf-8")
    TR.log_tokens(cfg, "triage", "gemini-3-flash-preview",
                  {"prompt_tokens": 1000, "output_tokens": 100, "thoughts_tokens": 0, "total_tokens": 1100, "calls": 1}, root=root)
    TR.log_tokens(cfg, "triage", "gemini-flash-latest",
                  {"prompt_tokens": 1000, "output_tokens": 100, "thoughts_tokens": 0, "total_tokens": 1100, "calls": 1}, root=root)
    r = C.compute(cfg, C.read_log(cfg, root), root)
    exp = (1000 * 0.50 + 100 * 3.00) * 0.75 / 1e6  # published gemini-3-flash-preview price
    assert r["model"] == "gemini-3-flash-preview" and r["gbp_per_report"] == pytest.approx(exp)
    assert r["per_model"]["gemini-flash-latest"]["gbp"] is None  # not on the pricing page: not priced
    assert r["gbp_per_year_repairs"] == pytest.approx(exp * C.REPAIRS_PER_YEAR)
    assert r["total_gbp_per_officer_month"] == pytest.approx(exp * C.REPAIRS_PER_YEAR / 12 / 4 + 25 / 4)
    text = C.write_cost_report(cfg, root).read_text(encoding="utf-8")
    assert "ai.google.dev/gemini-api/docs/pricing" in text and "repairs, not reports" in text


def test_retrain_promotes_only_if_better(root):
    md = root / "data" / "models"
    md.mkdir(parents=True)
    (md / "risk_eval.json").write_text(json.dumps({"eval": {"time": {"y30": {"model": {"pr_auc": 0.30}}}}}))
    (md / "risk_y30.txt").write_text("old")
    worse = RT.run(root=root, train_fn=lambda extra: (0.25, {"risk_y30.txt": b"new"}))
    assert not worse["promoted"] and (md / "risk_y30.txt").read_text() == "old"
    better = RT.run(root=root, train_fn=lambda extra: (0.35, {"risk_y30.txt": b"new"}))
    assert better["promoted"] and (md / "risk_y30.txt").read_text() == "new"
    assert any(p.name.startswith("previous_") for p in md.iterdir())


def test_gemini_failure_falls_back_to_keywords(root):
    class Broken:
        class models:
            @staticmethod
            def generate_content(**kw):
                raise ConnectionError("no network in tests")
    svc = Service(root=root, client=Broken())
    wo = svc.triage_report({"fms_id": 111})
    assert wo["method"].startswith("keyword_baseline (gemini unavailable") and wo["category"] == "pothole"
    with pytest.raises(RuntimeError):
        svc.triage_report({"fms_id": 112, "method": "gemini"})


def test_warm_precomputes_scenarios(root):
    svc = Service(root=root)
    w = svc.warm()
    assert set(w["warmed_scenarios"]) == {"rain_then_frost", "wet_week", "cold_snap"}
    assert svc.plan(scenario="cold_snap")["scenario_method"] == "sensitivity_surrogate"


def test_agent_mode_runs_the_tool_loop(root):
    cfg = I.load_config()
    cfg["triage"]["mode"] = "agent"
    fake = FakeGemini()
    wo = Service(cfg=cfg, root=root, client=fake).triage_report({"fms_id": 111})
    assert wo["tool_trace"] == ["lookup_location", "find_duplicates", "get_risk", "draft_work_order"]
    assert wo["tokens"]["calls"] == 4


def test_busy_model_falls_back_to_next(root):
    TR._model_pos[0] = 0
    fake = FakeGemini(fail_models={"gemini-3.8-flash"})
    wo = Service(root=root, client=fake).triage_report({"fms_id": 111})
    assert wo["method"] == "gemini" and wo["model"] == "gemini-3-flash-preview"
    assert fake.models_seen[:2] == ["gemini-3.8-flash", "gemini-3-flash-preview"]
    log = C.read_log(I.load_config(), root)
    assert log.iloc[-1]["model"] == "gemini-3-flash-preview"
    TR._model_pos[0] = 0


def test_all_models_busy_falls_back_to_keywords(root):
    TR._model_pos[0] = 0
    fake = FakeGemini(fail_models=set(TR.models(I.load_config())))
    wo = Service(root=root, client=fake).triage_report({"fms_id": 111})
    assert wo["method"].startswith("keyword_baseline (gemini unavailable") and wo["category"] == "pothole"


def test_open_meteo_attribution_on_page():
    web = I.ROOT / "roadsense" / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")
    assert "Weather data by Open-Meteo.com" in html and "https://open-meteo.com/" in html
    assert js.count("OPEN_METEO") >= 4  # banner, segment weather weeks, footer


def test_second_city_map_only(root):
    w = mock_world()
    seg = w["segments"].iloc[:20].assign(city="Brighton", segment_id=lambda d: "BRI-" + d["segment_id"])
    home = pd.read_parquet(root / "data" / "processed" / "segments.parquet").assign(city="Southampton")
    pd.concat([home, seg]).to_parquet(root / "data" / "processed" / "segments.parquet")
    from roadsense.prevent.mock import mock_risk
    r = mock_risk(seg).assign(city="Brighton", trained_on="real", uses_synthetic=False, history_source="real",
                              rank_pct=lambda d: d["p30"].rank(pct=True))
    r.to_parquet(root / "outputs" / "risk_brighton_latest.parquet")
    app = TestClient(create_app(Service(root=root)))
    assert app.get("/meta").json()["cities"] == ["Southampton", "Brighton"]
    gj = app.get("/segments", params={"city": "Brighton"}).json()
    assert gj["meta"]["badge"] == "100% real inputs" and len(gj["features"]) == len(seg) - (seg["highway"] == "motorway").sum()
    assert {f["properties"]["band"] for f in gj["features"]} <= {"critical", "high", "prevention", "monitor"}
    one = app.get(f"/segments/{gj['features'][0]['properties']['segment_id']}", params={"city": "Brighton"}).json()
    assert one["city"] == "Brighton" and "Southampton only" in one["note"]
    assert app.get("/segments", params={"city": "Leeds"}).status_code == 404
    # Southampton plan is unaffected by the second city's segments
    assert all(not row["segment_id"].startswith("BRI-") for row in app.get("/plan/prevention").json()["rows"])


def test_second_city_card_has_urgency_like_home(root):
    """Brighton gets Southampton's 0-100 urgency and bands (no wear term), not within-city rank tags."""
    test_second_city_map_only(root)
    app = TestClient(create_app(Service(root=root)))
    gj = app.get("/segments", params={"city": "Brighton"}).json()
    props = gj["features"][0]["properties"]
    assert "rank_pct" not in props and "risk_band" not in props and 0 <= props["urgency"] <= 100
    assert set(gj["meta"]["bands"]) == {"critical", "high", "prevention", "monitor"}
    one = app.get(f"/segments/{props['segment_id']}", params={"city": "Brighton"}).json()
    assert not {"rank_pct", "risk_label"} & set(one) and "reasons" in one and one["wear_pct"] is None
    assert one["band"] == props["band"] and one["urgency"] == props["urgency"]
    assert one["urgency"] == pytest.approx(min(100, 100 * one["consequence"] * one["p30"]), abs=0.06)
