"""M5 tests: segment-week table, leakage, risk model, wear (small fixture, no network)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest_lane_b import small_config
from roadsense.models import risk
from roadsense.models.dataset import FEATURES, HISTORY_FEATS, MONOTONE, WEATHER_FEATS, build_table
from roadsense.synthetic import build


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    cfg = small_config()
    root = tmp_path_factory.mktemp("m5")
    build.run(cfg, root=root, verbose=False)
    data = risk.load_data(cfg, root)
    return cfg, root, data


@pytest.fixture(scope="module")
def trained(world):
    cfg, root, _ = world
    return risk.run(cfg, root=root, verbose=False)


def test_no_leakage_from_future_records(world):
    """Features for a week must not change when every record dated on/after its start is removed."""
    cfg, _, d = world
    cut = pd.Timestamp("2024-09-02")
    full = build_table(d["seg"], d["wx"], d["res"], d["potholes"], cfg, start="2024-06-03", end="2024-09-03")
    ph = d["potholes"][pd.to_datetime(d["potholes"]["reported_date"]) < cut]
    res = d["res"][pd.to_datetime(d["res"]["date"]) < cut]
    wx = d["wx"].copy()
    future = pd.to_datetime(wx["week_start"]) > cut
    wx.loc[future, WEATHER_FEATS] = wx.loc[future, WEATHER_FEATS] * 10 + 99     # scramble later weather
    trunc = build_table(d["seg"], wx, res, ph, cfg, start="2024-06-03", end="2024-09-03")
    rows = full["week_start"] <= cut
    pd.testing.assert_frame_equal(full.loc[rows, ["segment_id", "week_start"] + FEATURES].reset_index(drop=True),
                                  trunc.loc[rows, ["segment_id", "week_start"] + FEATURES].reset_index(drop=True))


def test_history_counts_strictly_before_week_start(world):
    cfg, _, d = world
    sid = d["seg"]["segment_id"].iloc[0]
    ph = pd.DataFrame({"segment_id": [sid], "reported_date": ["2024-07-01"]})     # a Monday
    t = build_table(d["seg"], d["wx"], d["res"], ph, cfg, start="2024-06-24", end="2024-07-09")
    r = t[t["segment_id"] == sid].set_index("week_start")
    assert r.loc["2024-07-01", "potholes_seg_12m"] == 0        # same day is not "before"
    assert r.loc["2024-07-08", "potholes_seg_12m"] == 1
    assert r.loc["2024-07-01", "y7"] == 1 and r.loc["2024-06-24", "y7"] == 0
    assert r.loc["2024-06-24", "y30"] == 1


def test_monotone_in_rain(world, trained):
    cfg, root, d = world
    m = risk.RiskModel("y30", cfg).fit(risk.labelled_rows(build_table(d["seg"], d["wx"], d["res"], d["potholes"], cfg), cfg))
    row = build_table(d["seg"], d["wx"], d["res"], d["potholes"], cfg, start="2025-01-06", end="2025-01-07").head(50)
    prev = None
    for v in [0, 50, 100, 200, 400]:
        r = row.copy()
        r["rain_30d"] = v
        p = m.raw(r)
        if prev is not None:
            assert (p >= prev - 1e-12).all()
        prev = p
    assert MONOTONE["treatments_2y"] == -1


def test_risk_outputs_cover_every_segment(world, trained):
    cfg, root, d = world
    out = pd.read_parquet(trained["out"])
    assert set(out["segment_id"]) == set(d["seg"]["segment_id"])
    contract = ["segment_id", "week_start", "p7", "p30", "wear", "reason_1", "reason_2", "reason_3", "model_version",
                "trained_on", "uses_synthetic", "weather_data_date", "uses_forecast"]
    assert [c for c in contract if c not in out] == []
    assert len(out) == out["segment_id"].nunique() * cfg["risk"]["forward_weeks"]
    assert not out.duplicated(["segment_id", "week_start"]).any()
    assert out["p30"].between(0, 1).all() and out["wear"].between(0, 100).all()
    assert out["reason_1"].notna().mean() > 0.9
    assert out["uses_synthetic"].all()


def test_eval_report_has_model_and_both_baselines(trained, world):
    _, root, _ = world
    txt = (root / "reports" / "risk_eval.md").read_text(encoding="utf-8")
    for s in ["| model |", "| road age only |", "| prior pothole count only |", "Real-world check", "SYNTHETIC"]:
        assert s in txt
    ev = trained["eval"]["time"]["y30"]
    assert ev["model"]["pr_auc"] > ev["road_age_only"]["pr_auc"]


def test_wear_increases_with_age(world):
    from roadsense.models import wear
    cfg, _, d = world
    surv = wear.survival_frame(d["seg"], d["wx"], d["res"], d["potholes"], cfg)
    m = wear.fit(surv)
    s = surv.head(1).copy()
    vals = []
    for age in [0.5, 2, 5, 10, 20]:
        s["age_years"] = age
        vals.append(float(wear.wear_pct(m, s)[0]))
    assert all(b >= a for a, b in zip(vals, vals[1:]))
    assert 0 <= vals[0] <= vals[-1] <= 100
