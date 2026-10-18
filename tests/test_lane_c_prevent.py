"""Lane C, M6: urgency, bands, treatments, weekly plan and what-if. Small fixtures, no network."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from roadsense.prevent import inputs as I
from roadsense.prevent import plan as P
from roadsense.prevent import treatments as T
from roadsense.prevent import urgency as U
from roadsense.prevent import whatif as W
from roadsense.prevent.mock import mock_risk, mock_world


@pytest.fixture(scope="module")
def cfg():
    return I.load_config()


@pytest.fixture(scope="module")
def world():
    return mock_world()


def test_urgency_formula_cap_and_confirmed(cfg):
    u = U.urgency([0.2, 0.9, 0.2], [50, 100, 50], [1.0, 1.5, 1.0], cfg, confirmed_open=[False, False, True])
    assert u[0] == pytest.approx(100 * (0.7 * 0.2 + 0.3 * 0.5))
    assert u[1] == 100  # capped at 1
    assert u[2] == 100  # confirmed open pothole


def test_bands(cfg):
    assert list(U.band([95, 90, 89.9, 70, 69, 40, 39.9, 0], cfg)) == [
        "critical", "critical", "high", "high", "prevention", "prevention", "monitor", "monitor"]


def test_consequence_weights_multiply_and_cap(cfg):
    seg = pd.DataFrame({"highway": ["primary", "residential", "primary"], "bus_stop": [True, False, True],
                        "cycle_route": [True, False, False], "near_school_hospital": [True, False, False]})
    C, text = U.consequence(seg, cfg)
    assert C[0] == 1.5  # 1.3*1.2*1.1*1.2 = 2.06 -> cap
    assert C[1] == 1.0
    assert C[2] == 1.5  # 1.3*1.2 = 1.56 -> cap
    seg2 = pd.DataFrame({"highway": ["residential"], "bus_stop": [True]})
    assert U.consequence(seg2, cfg)[0][0] == pytest.approx(1.2)
    assert "A-road" in text[0] and text[1] == ""


def test_treatment_rules(cfg):
    t = T.choose([10, 10, 55, 80, 95, 20], [True, False, False, False, False, False],
                 [False, False, False, False, False, True], cfg)
    assert list(t) == ["crack_sealing", "crack_sealing", "surface_dressing", "patch", "resurface", "permanent_repair"]


def test_score_skips_national_highways(cfg, world):
    sc = P.score(world["segments"], world["risk"], cfg)
    mw = sc[sc["highway"] == "motorway"]
    assert len(mw) and not mw["scored"].any() and mw["urgency"].isna().all()
    assert (mw["status"] == "National Highways road, not scored").all()
    assert sc.loc[sc["scored"], "band"].notna().all()


def test_plan_is_prevention_band_rising_and_ranked(cfg, world, tmp_path):
    res = P.run(cfg, root=tmp_path, verbose=False)
    plan = res["plan"]
    assert len(plan) > 0
    assert (plan["band"] == "prevention").all()
    assert (plan["p30_rise"] > 0).all()
    assert plan["avoided_per_gbp"].is_monotonic_decreasing
    out = pd.read_csv(res["path"])
    assert list(out.columns[:len(P.CONTRACT_COLS)]) == P.CONTRACT_COLS  # docs/contract_outputs.md section 9
    for c in ["reasons", "treatment", "treatment_label", "cost_logic", "cost_planned_gbp", "cost_reactive_avoided_gbp",
              "band", "urgency", "p30", "wear", "data_dates"]:
        assert out[c].notna().all(), c
    assert set(out["treatment"]) <= {"crack_sealing", "surface_dressing", "patch", "resurface", "permanent_repair"}
    assert "87.80" in out["cost_logic"].iloc[0] and "56.94" in out["cost_logic"].iloc[0]
    assert res["path"].name.startswith("prevention_plan_")
    assert (res["path"].parent / "prevention_plan_latest.csv").read_bytes() == res["path"].read_bytes()


def test_plan_ward_filter(cfg, world):
    sc = P.score(world["segments"], world["risk"], cfg)
    w = sc.loc[sc["band"] == "prevention", "ward"].iloc[0]
    assert (P.build_plan(sc, cfg, ward=w)["ward"] == w).all()


def test_feedback_confirmed_sets_urgency_100(cfg, world):
    sid = world["risk"]["segment_id"].iloc[0]
    sc = P.score(world["segments"], world["risk"], cfg, feedback={sid: {"confirmed_open": True}})
    r = sc.set_index("segment_id").loc[sid]
    assert r["urgency"] == 100 and r["band"] == "critical" and r["treatment"] == "permanent_repair"


def test_rain_then_frost_raises_risk_most_on_high_load(cfg, world):
    seg, risk = world["segments"], world["risk"]
    scn = W.apply_scenario(risk, seg, "rain_then_frost", cfg, method="surrogate")
    base = P.score(seg, risk, cfg)
    after = P.score(seg, scn, cfg)
    cmp_ = W.compare(base, after)
    assert (cmp_["p30_peak_change"] >= 0).all()
    q = cmp_["esal_day"].quantile([0.25, 0.75])
    lo = cmp_.loc[cmp_["esal_day"] <= q.iloc[0], "p30_peak_change"].mean()
    hi = cmp_.loc[cmp_["esal_day"] >= q.iloc[1], "p30_peak_change"].mean()
    assert hi > 2 * lo
    assert np.corrcoef(np.log1p(cmp_["esal_day"]), cmp_["p30_peak_change"])[0, 1] > 0.5
    assert (scn["scenario_method"] == "sensitivity_surrogate").all()


@pytest.mark.parametrize("scenario", ["wet_week", "cold_snap"])
def test_other_scenarios_raise_risk(cfg, world, scenario):
    scn = W.apply_scenario(world["risk"], world["segments"], scenario, cfg, method="surrogate")
    assert (scn["p30_w1"] >= world["risk"]["p30_w1"]).all()
    assert (scn["p30_w3"] == world["risk"]["p30_w3"]).all()  # week 4 is climatology, untouched


def test_unknown_scenario(cfg, world):
    with pytest.raises(ValueError):
        W.apply_scenario(world["risk"], world["segments"], "heatwave", cfg)


def test_stale_forecast_marks_plan_provisional(cfg):
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    f = P.freshness(cfg, {"retrieved_at": "2026-10-17T06:00:00+00:00"}, now=now)
    assert not f["stale"] and not f["provisional"]
    f = P.freshness(cfg, {}, forecast_as_of="2026-10-15T06:00:00", now=now)
    assert f["stale"] and f["provisional"] and f["message"].startswith("STALE")
    f = P.freshness(cfg, {"retrieved_at": None}, now=now)
    assert f["provisional"] and not f["stale"]


def test_segment_index_snaps_to_polyline(world):
    idx = I.SegmentIndex(world["segments"])
    s = world["segments"].iloc[5]
    j, d = idx.nearest(s["lon"], s["lat"] + 10 / I.M_PER_DEG_LAT)
    assert world["segments"].iloc[j]["segment_id"] == s["segment_id"]
    assert d == pytest.approx(10, abs=0.5)


def test_model_path_when_lane_b_present(cfg):
    pytest.importorskip("roadsense.models.risk")
    if not (I.resolve(cfg, "models_dir") / "risk_y30.txt").exists():
        pytest.skip("lane B models not on disk")
    risk, _ = I.load_risk(cfg)
    seg, _ = I.load_segments(cfg)
    scn = W.apply_scenario(risk, seg, "rain_then_frost", cfg, method="model")
    cmp_ = W.compare(P.score(seg, risk, cfg), P.score(seg, scn, cfg))
    q = cmp_["esal_day"].quantile([0.1, 0.9])
    assert (cmp_.loc[cmp_["esal_day"] >= q.iloc[1], "p30_peak_change"].mean()
            > cmp_.loc[cmp_["esal_day"] <= q.iloc[0], "p30_peak_change"].mean())


def test_normalise_contract_long_risk(world):
    long = mock_risk(world["segments"])
    assert {"horizon_week", "wear", "uses_synthetic", "uses_forecast", "weather_data_date"} <= set(long.columns)
    r = I.normalise_risk(long)
    assert r["segment_id"].is_unique and {"p30_w0", "p30_w3", "wear_pct", "labels_synthetic"} <= set(r.columns)
    assert (r["p30_w0"] == r["p30"]).all()


def test_real_segments_copy_with_mock_risk(cfg):
    seg, meta = I.load_segments(cfg, root=I.ROOT)
    if meta["kind"] != "real":
        pytest.skip("real segments not in use")
    risk, rmeta = I.load_risk(cfg, root=I.ROOT, seg=seg)
    sc = P.score(seg, risk, cfg)
    assert sc["ward"].nunique() == 17
    nh = sc["owner"] == "national_highways"
    assert nh.any() and not sc.loc[nh, "scored"].any()
    assert set(sc["load_confidence"]) <= {"high", "medium", "low"}
    assert sc["scored"].sum() > 0.9 * (~nh).sum()


def test_horizon_weeks_one_to_four_and_forecast_time(world):
    long = mock_risk(world["segments"])
    long["horizon_week"] += 1  # schemas.md v2 numbering
    long["forecast_retrieved_at"] = np.where(long["horizon_week"] <= 3, "2026-10-17T12:36+00:00", None)
    r = I.normalise_risk(long)
    assert {"p30_w0", "p30_w3"} <= set(r.columns) and "p30_w4" not in r
    assert r.attrs["forecast_retrieved_at"] == "2026-10-17T12:36+00:00"
    assert r["forecast_retrieved_at_w0"].notna().all() and ("forecast_retrieved_at_w3" not in r or r["forecast_retrieved_at_w3"].isna().all())
    assert set(r["weather_basis_w3"]) == {"climatology_10y"}


def test_three_city_segments_with_context_columns(cfg, tmp_path, world):
    seg = world["segments"].assign(city="Southampton", cycle_route=False, near_school_hospital=False)
    seg.loc[seg.index[:5], "cycle_route"] = True
    other = seg.head(20).assign(city="Brighton", segment_id=lambda d: "BRI-" + d["segment_id"])
    d = tmp_path / "data" / "processed"
    d.mkdir(parents=True)
    pd.concat([seg, other]).to_parquet(d / "segments.parquet")
    loaded, meta = I.load_segments(cfg, root=tmp_path)
    assert meta["kind"] == "real" and (loaded["segment_id"].str[:4] != "BRI-").all()
    assert loaded["cycle_route"].sum() == 5 and loaded["near_school_hospital"].dtype == bool
    sc = P.score(loaded, world["risk"], cfg)
    assert "cycle route" in sc.loc[sc["cycle_route"] & sc["scored"], "consequence_text"].iloc[0]


def test_context_columns_missing_mean_false(cfg, tmp_path, world):
    d = tmp_path / "data" / "processed"
    d.mkdir(parents=True)
    world["segments"].to_parquet(d / "segments.parquet")
    loaded, _ = I.load_segments(cfg, root=tmp_path)
    assert not loaded["cycle_route"].any() and not loaded["near_school_hospital"].any()


def test_horizon_uses_treatment_life(cfg):
    t = pd.Series(["surface_dressing", "resurface"])
    life = cfg["treatments"]["life_years"]
    a = T.expected_avoided([0.05, 0.05], t, cfg)
    h = -np.log1p(-0.05)
    assert a[0] == pytest.approx(h * 12 * life["surface_dressing"] * (1 - 0.6))
    assert a[1] == pytest.approx(h * 12 * life["resurface"] * (1 - 0.3))


def test_worth_treating_fields_order_and_badges(cfg, world):
    seg, risk = world["segments"], world["risk"]
    base = P.score(seg, risk, cfg)
    loser = base[base["scored"] & (base["avoided_per_gbp"] < 1)].sort_values("urgency").iloc[0]["segment_id"]
    sc = P.score(seg, risk, cfg, confirmed={loser: {"source_id": "1", "by": "keyword"}})
    r = sc.set_index("segment_id").loc[loser]
    assert r["confirmed_pothole"] and r["worth_treating"] and r["confirmed_by"] == "keyword"
    assert r["treatment"] == "permanent_repair" and r["treatment_cost_gbp"] == 56.94
    assert r["reactive_cost_avoided_gbp"] == 87.80 and r["avoided_per_gbp"] == pytest.approx(1.542, abs=1e-3)
    assert r["urgency"] >= 70 and r["band"] in ("high", "critical")
    assert r["reason_1"] == "open FixMyStreet report, confirmed by keyword"
    assert r["preventive_treatment"] in ("crack_sealing", "surface_dressing", "patch", "resurface")
    assert (sc["pays_back"] == (sc["scored"] & (sc["avoided_per_gbp"] >= 1))).all()
    w = P.build_worth(sc, cfg)
    assert w.iloc[0]["segment_id"] == loser and w.iloc[0]["value_badge"] == "Confirmed: must fix ×1.54"
    rest = w[~w["confirmed_pothole"]]
    assert rest["pays_back"].all() and rest["avoided_per_gbp"].is_monotonic_decreasing
    plan = P.build_plan(sc, cfg)
    assert set(plan["value_badge"].str.split(" ×").str[0]) <= {"Pays back", "Loss maker", "Confirmed: must fix"}
    s = P.worth_summary(w)
    assert s["confirmed_potholes"] == 1 and s["header"].startswith(f"{s['pays_back']} roads pay back · 1 confirmed potholes")


def test_confirmed_from_fms_snaps_and_classifies(cfg, world):
    seg = world["segments"]
    s = seg.iloc[34]
    fms = pd.DataFrame({"id": [1, 2, 3], "status": ["open", "open", "closed"],
                        "requested": [pd.Timestamp("2026-10-10")] * 3, "lat": [s["lat"]] * 3, "lon": [s["lon"]] * 3,
                        "title": ["Large pothole", "Blocked drain", "Pothole"]})
    c = P.confirmed_from_fms(fms, seg, cfg, now=pd.Timestamp("2026-10-17"))
    assert c == {s["segment_id"]: {"source_id": "1", "by": "keyword"}}
    g = P.confirmed_from_fms(fms, seg, cfg, classify=lambda r: ("pothole", "gemini"), now=pd.Timestamp("2026-10-17"))
    assert g[s["segment_id"]]["by"] == "gemini"


def test_confirmed_fields_precedence_and_simulated_crew(cfg, world):
    seg, risk = world["segments"], world["risk"]
    ids = risk["segment_id"].tolist()
    a, b, c = ids[0], ids[1], ids[2]
    confirmed = {a: {"source_id": "111", "by": "keyword"}, b: {"source_id": "222", "by": "gemini"}}
    feedback = {b: {"confirmed_open": True, "crew_outcome_id": "CO0007", "crew_is_synthetic": True},
                c: {"confirmed_open": True, "crew_outcome_id": "OUT00001", "crew_is_synthetic": False}}
    sc = P.score(seg, risk, cfg, feedback, confirmed=confirmed).set_index("segment_id")
    assert (sc.loc[a, ["confirmed_by", "confirmed_source_id", "confirmed_is_synthetic"]].tolist() == ["keyword", "111", False])
    assert sc.loc[b, "confirmed_by"] == "crew" and sc.loc[b, "confirmed_source_id"] == "CO0007"  # crew > gemini
    assert bool(sc.loc[b, "confirmed_is_synthetic"]) and not bool(sc.loc[c, "confirmed_is_synthetic"])
    assert set(sc["confirmed_by"]) <= {"", "keyword", "gemini", "crew"}
    assert (sc.loc[sc["confirmed_by"] != "", "confirmed_source_id"] != "").all()
    w = P.build_worth(sc.reset_index(), cfg)
    assert w.set_index("segment_id").loc[b, "value_badge"] == "Simulated crew report"
    assert w.set_index("segment_id").loc[b, "treatment"] == "permanent_repair"
    s = P.worth_summary(w)
    assert s["confirmed_potholes"] == 2 and s["simulated_crew_reports"] == 1
    assert "2 confirmed potholes · 1 crew-reported (simulated)" in s["header"]


def test_plan_csv_has_contract_confirmation_columns(cfg, tmp_path):
    res = P.run(cfg, root=tmp_path, verbose=False)
    out = pd.read_csv(res["path"].parent / "prevention_plan_latest.csv", dtype={"confirmed_by": str,
                      "confirmed_source_id": str}, keep_default_na=False)
    for col in ["confirmed_by", "confirmed_source_id", "confirmed_is_synthetic"]:
        assert col in out
    assert "confirmed_report" not in out
    assert set(out["confirmed_by"]) <= {"", "keyword", "gemini", "crew"}


def test_labels_synthetic_follows_trained_on(world):
    long = mock_risk(world["segments"])
    long["trained_on"], long["uses_synthetic"], long["history_source"] = "real", True, "synthetic council log"
    r = I.normalise_risk(long)
    assert not r["labels_synthetic"].any() and r["uses_synthetic"].all()
    assert I.model_label("real", "synthetic council log") == "trained on real reports · history synthetic"
    assert I.model_label("synthetic", None) == "trained on synthetic labels"
    long["trained_on"] = "synthetic"
    assert I.normalise_risk(long)["labels_synthetic"].all()


def test_plan_rows_say_real_labels_when_trained_on_real(cfg, world):
    long = mock_risk(world["segments"]).assign(trained_on="real", uses_synthetic=True, history_source="synthetic")
    sc = P.score(world["segments"], long, cfg)
    s = sc[sc["scored"]]
    assert not s["labels_synthetic"].any() and s["uses_synthetic"].all()
