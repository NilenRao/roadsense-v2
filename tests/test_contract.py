"""
Contract test: the files lanes A, B and C pass between them match schemas.md and link up.
Run at every merge:  pytest -q tests/test_contract.py
Reads the real processed files (no network). A missing file is SKIPPED, not failed, so each lane
can run it before the others have delivered; at Merge 1 nothing should be skipped.
"""
import os

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "data", "processed")
SYN = os.path.join(ROOT, "data", "synthetic")
OUT = os.path.join(ROOT, "outputs")

SEGMENTS = ["segment_id", "city", "osm_way_id", "name", "ref", "highway", "is_main_road", "ward", "length_m",
            "lat", "lon", "bus_stop", "bus_routes", "junction", "in_flood_area", "owner", "geometry", "source",
            "cycle_route", "near_school_hospital"]
LOAD = ["segment_id", "hgv_bus_aadf", "esal_day", "count_point_id", "count_year", "load_source", "source"]
LABELS = ["city", "segment_id", "year", "n_reports", "had_report", "split"]
WEATHER = ["ward", "week_start", "rain_3d", "rain_7d", "rain_30d", "rain_90d", "hours_below_0_7d",
           "freeze_thaw_14d", "freeze_thaw_30d", "rain_then_frost_7d", "hot_days_7d", "source"]
RISK = ["segment_id", "ward", "week_start", "horizon_week", "p7", "p30", "p30_trend", "wear",
        "weather_source", "uses_synthetic", "uses_forecast"]          # lane B -> lane C (schemas.md section 7)


def load(path):
    if not os.path.exists(path):
        pytest.skip(f"not delivered yet: {os.path.relpath(path, ROOT)}")
    return pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)


@pytest.fixture(scope="module")
def seg():
    return load(os.path.join(P, "segments.parquet"))


def missing(df, cols):
    return [c for c in cols if c not in df.columns]


def test_segments_shape(seg):
    assert not missing(seg, SEGMENTS)
    assert seg.segment_id.is_unique
    assert seg.ward.notna().all(), "every segment needs a ward (weather joins on it)"
    assert set(seg.owner.unique()) <= {"council", "national_highways"}
    assert (seg.length_m > 0).all()


def test_load_links_to_segments(seg):
    ld = load(os.path.join(P, "load.parquet"))
    assert not missing(ld, LOAD)
    assert ld.segment_id.is_unique
    assert set(ld.segment_id) <= set(seg.segment_id), "load has segment_ids not in segments.parquet"
    assert ld.esal_day.notna().all() and (ld.esal_day >= 0).all()
    assert set(ld.load_source.unique()) <= {"count", "same_road", "class_median"}


def test_labels_link_to_segments(seg):
    lb = load(os.path.join(P, "labels_segment_year.csv"))
    assert not missing(lb, LABELS)
    assert set(lb.segment_id) <= set(seg.segment_id)
    assert set(lb.split.unique()) <= {"train", "test", "frozen_2026", "excluded"}
    assert not lb.duplicated(["segment_id", "year"]).any()


def test_weather_covers_every_segment_ward(seg):
    wx = load(os.path.join(P, "weather_weekly.parquet"))
    assert not missing(wx, WEATHER)
    assert set(seg.ward.unique()) <= set(wx.ward.unique()), "ward names differ between segments and weather"
    wk = pd.to_datetime(wx.week_start)
    assert (wk.dt.weekday == 0).all(), "week_start must be a Monday"
    assert not wx.duplicated(["ward", "week_start"]).any()


def test_forecast_weather_matches_segment_wards(seg):
    fc = load(os.path.join(P, "weather_forecast_weekly.parquet"))
    assert not missing(fc, WEATHER + ["uses_forecast"])
    assert len(fc), "no forecast weeks"
    wk = pd.to_datetime(fc.week_start)
    assert (wk.dt.weekday == 0).all(), "week_start must be a Monday"
    assert (wk.dt.date > pd.Timestamp(fc.retrieved_at.min()[:10]).date()).all(), "forecast weeks must start after the pull date"
    assert not fc.duplicated(["ward", "week_start"]).any()
    for city, f in fc.groupby("city"):
        seg_wards = set(seg.loc[seg.city == city, "ward"])
        assert set(f.ward) == seg_wards, f"{city}: forecast ward names differ from segments.parquet"
        assert (f.groupby("ward").week_start.nunique() == f.week_start.nunique()).all(), f"{city}: wards cover different weeks"


def test_snapped_reports_link(seg):
    sn = load(os.path.join(P, "fms_snapped.csv"))
    ids = sn.segment_id.dropna()
    assert set(ids) <= set(seg.segment_id)
    assert "description" not in sn.columns, "no free text outside data/raw"


@pytest.mark.parametrize("fname", ["potholes.csv", "resurfacing.csv", "streetlights.csv", "crew_outcomes.csv"])
def test_synthetic_files_use_real_segments(seg, fname):
    df = load(os.path.join(SYN, fname))
    assert df["is_synthetic"].astype(str).str.lower().isin(["true", "1"]).all()
    assert df["source"].astype(str).str.startswith("synthetic:").all()
    ids = df.segment_id.dropna()
    stale = set(ids) - set(seg.segment_id)
    assert not stale, f"{fname}: {len(stale)} segment_ids not in segments.parquet: regenerate synthetic data on real segments"


RISK_FILES = {                                                       # schemas.md section 7
    "risk_latest.parquet": "Southampton",            # real-label model ranks the plan
    "risk_brighton_latest.parquet": "Brighton",      # map only, real inputs only
    "risk_synthetic_latest.parquet": "Southampton",  # synthetic-trained weekly model (what-if)
}


@pytest.mark.parametrize("fname", sorted(RISK_FILES))
def test_risk_output_links(seg, fname):
    r = load(os.path.join(OUT, fname))
    assert not missing(r, RISK)
    city_ids = set(seg.loc[seg.city == RISK_FILES[fname], "segment_id"])
    assert set(r.segment_id) <= city_ids, f"{fname}: segment_ids not in {RISK_FILES[fname]} segments"
    assert r[["p7", "p30"]].apply(lambda c: c.between(0, 1)).all().all()
    assert (r.groupby("segment_id").size() == 4).all(), "4 weekly rows per scored segment"
    assert set(r.horizon_week.unique()) <= {1, 2, 3, 4}


def test_plan_risk_is_real_label():
    """risk_latest ranks the plan: trained on real reports, history source stated per row."""
    r = load(os.path.join(OUT, "risk_latest.parquet"))
    assert not missing(r, ["trained_on", "history_source"])
    assert set(r.trained_on.unique()) == {"real"}, "the plan must be ranked by the real-label model"
    assert set(r.history_source.unique()) <= {"real", "synthetic"}


def test_brighton_risk_is_all_real():
    r = load(os.path.join(OUT, "risk_brighton_latest.parquet"))
    assert not missing(r, ["trained_on", "history_source"])
    assert set(r.trained_on.unique()) == {"real"} and set(r.history_source.unique()) == {"real"}
    assert not r.uses_synthetic.astype(bool).any(), "Brighton map is advertised as 100% real inputs"
    assert r.wear.isna().all(), "wear is Southampton only (synthetic resurfacing history)"


def test_plan_financial_split():
    """schemas.md section 9, financial split: pays_back / confirmed_pothole / worth_treating."""
    path = os.path.join(OUT, "prevention_plan_latest.csv")
    if not os.path.exists(path):
        pytest.skip("no outputs/prevention_plan_latest.csv yet")
    plan = pd.read_csv(path, dtype={"confirmed_by": str}, keep_default_na=False)
    assert not missing(plan, ["avoided_per_gbp", "pays_back", "confirmed_pothole", "worth_treating"])
    flag = lambda c: plan[c].astype(str).str.strip().str.lower().isin(["true", "1"])
    assert (flag("pays_back") == (plan.avoided_per_gbp.astype(float) >= 1.0)).all(), "pays_back != avoided_per_gbp >= 1"
    assert (flag("confirmed_pothole") == (plan.confirmed_by.str.strip() != "")).all(), "confirmed_pothole != confirmed_by set"
    assert (flag("worth_treating") == (flag("pays_back") | flag("confirmed_pothole"))).all(), \
        "worth_treating != pays_back OR confirmed_pothole"


CONFIRMED_BY = {"", "keyword", "gemini", "crew"}


def test_plan_confirmations():
    """schemas.md section 9: confirmed_by / confirmed_source_id / confirmed_is_synthetic."""
    path = os.path.join(OUT, "prevention_plan_latest.csv")
    if not os.path.exists(path):
        pytest.skip("no outputs/prevention_plan_latest.csv yet")
    plan = pd.read_csv(path, dtype={"confirmed_by": str, "confirmed_source_id": str}, keep_default_na=False)
    assert not missing(plan, ["confirmed_by", "confirmed_source_id", "confirmed_is_synthetic"])
    by = plan["confirmed_by"].str.strip()
    assert set(by.unique()) <= CONFIRMED_BY, f"confirmed_by outside {sorted(CONFIRMED_BY)}: {set(by) - CONFIRMED_BY}"
    confirmed = by != ""
    assert (plan.loc[confirmed, "confirmed_source_id"].str.strip() != "").all(), \
        "confirmed_source_id is required when confirmed_by is set"
    crew = plan.loc[by == "crew", "confirmed_is_synthetic"].astype(str).str.strip().str.lower()
    assert crew.isin(["true", "false", "1", "0"]).all(), "confirmed_is_synthetic must be true/false on crew rows"


# --- additions, 17 Oct evening (schemas.md sections 13-19) ---------------------------------------
def _is_str(s):
    return s.dropna().map(lambda v: isinstance(v, str)).all()


def _is_bool(s):
    return pd.api.types.is_bool_dtype(s) or s.dropna().isin([True, False]).all()


RISK_EXTRA = {"weather_basis": "str", "inputs_mock": "bool", "as_of": "str", "model_version": "str",
              "reason_1": "str", "reason_2": "str", "reason_3": "str"}
RISK_REAL_EXTRA = {"weather_multiplier": "float", "p_report_year": "float", "lambda_year": "float", "rank_pct": "float",
                   "history_source": "str", "trained_on": "str"}


def _check_types(df, spec, name):
    assert not missing(df, list(spec)), f"{name}: missing {missing(df, list(spec))}"
    for c, t in spec.items():
        if t == "str":
            assert _is_str(df[c]), f"{name}.{c} should be str"
        elif t == "bool":
            assert _is_bool(df[c]), f"{name}.{c} should be bool"
        elif t == "float":
            assert pd.api.types.is_numeric_dtype(df[c]), f"{name}.{c} should be numeric"
        elif t == "date":
            assert pd.to_datetime(df[c].dropna().astype(str), format="%Y-%m-%d", errors="coerce").notna().all(), \
                f"{name}.{c} should be a YYYY-MM-DD date"


@pytest.mark.parametrize("fname", sorted(RISK_FILES))
def test_risk_extra_columns(fname):
    r = load(os.path.join(OUT, fname))
    _check_types(r, RISK_EXTRA, fname)
    assert r.model_version.str.match(r"^risk@\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$").all(), "model_version is risk@<UTC time>"
    reasons = pd.concat([r.reason_1, r.reason_2, r.reason_3]).dropna()
    assert not reasons.str.contains("in the ward last year").any(), "ward weather is never a reason (2-6 values per city)"
    if fname != "risk_synthetic_latest.parquet":
        _check_types(r, RISK_REAL_EXTRA, fname)
        assert r.rank_pct.between(0, 1).all() and (r.lambda_year >= 0).all()
    if fname == "risk_brighton_latest.parquet":
        _check_types(r, {"city": "str", "scale_source": "str"}, fname)
        assert (r.weather_multiplier == 1.0).all()


def test_lights_extra_columns():
    l = load(os.path.join(OUT, "lights_latest.parquet"))
    _check_types(l, {"unit_install_date": "date", "burn_hours": "float", "replacement_overdue": "bool",
                     "is_synthetic": "bool", "model_version": "str", "source": "str", "as_of": "str"}, "lights")
    assert l.model_version.str.startswith("lights@").all()


def test_plan_names_and_extra_columns(seg):
    path = os.path.join(OUT, "prevention_plan_latest.csv")
    if not os.path.exists(path):
        pytest.skip("no outputs/prevention_plan_latest.csv yet")
    plan = pd.read_csv(path)
    assert not missing(plan, ["highway", "reasons", "cost_logic", "model_version", "history_source", "value_badge",
                              "preventive_treatment", "labels_synthetic", "wear_synthetic"])
    links = plan[plan.highway.astype(str).str.endswith("_link")]
    assert not links.name.astype(str).str.fullmatch(r"Unnamed \w+_link road").any(), \
        "unnamed slip roads use 'Slip road off ...' or 'Unnamed slip road near <ward>' (section 13)"


TRIAGE_REPORT = {"report_id": str, "fms_id": int, "reported_at": str, "lat": float, "lon": float, "title": str,
                 "category": str, "method": str, "merged_into": (str, type(None)), "status": str,
                 "segment_id": (str, type(None)), "risk_band": (str, type(None))}


def test_triage_caches():
    import glob
    import json
    files = sorted(glob.glob(os.path.join(OUT, "lane_c", "triage_*.json")))
    files = [f for f in files if not f.endswith("triage_vs_keywords.json")]
    if not files:
        pytest.skip("no outputs/lane_c/triage_<city>.json yet")
    for f in files:
        t = json.load(open(f, encoding="utf-8"))
        for k in ["city", "generated_at", "window_days", "count", "methods", "reports"]:
            assert k in t, f"{os.path.basename(f)}: missing {k}"
        assert t["count"] == len(t["reports"]) and set(t["methods"]) <= {"gemini", "keyword"}
        for r in t["reports"]:
            for k, typ in TRIAGE_REPORT.items():
                assert k in r and isinstance(r[k], typ), f"{os.path.basename(f)}: {k} should be {typ}"
            assert r["category"] in {"pothole", "streetlight", "other"}
            assert r["status"] in {"triaged", "merged", "not on a council road"}


def test_brighton_inspection_rows():
    """schemas.md section 17: inspection rows are real-only, open reports first, urgency but no money."""
    if not os.path.exists(os.path.join(OUT, "risk_brighton_latest.parquet")):
        pytest.skip("no Brighton risk file")
    from roadsense.api.cities import CityView
    from roadsense.prevent import inputs as I
    cfg = I.load_config()
    view = CityView(cfg, "Brighton")
    out = view.inspection_list(pd.DataFrame(), cfg, top_n=50)
    assert [s["key"] for s in out["sections"]] == ["open", "risk"]
    row_keys = {"rank", "section", "segment_id", "name", "ward", "highway", "urgency", "band", "p30", "reasons", "action",
                "open_report", "open_reports", "history_source", "uses_synthetic"}
    for r in out["rows"]:
        assert row_keys <= set(r) and not ({"risk_label", "treatment", "cost_planned_gbp"} & set(r))
        assert isinstance(r["rank"], int) and isinstance(r["reasons"], list) and isinstance(r["open_report"], bool)
        assert r["band"] in {"critical", "high", "prevention", "monitor"} and 0 <= r["urgency"] <= 100 and r["action"] == "Inspect"
        assert r["history_source"] == "real" and r["uses_synthetic"] is False
