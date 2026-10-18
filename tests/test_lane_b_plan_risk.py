"""Plan risk tests: real-label ranking with mapped synthetic history (small fixture, no network)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import yaml

from conftest_lane_b import small_config
from roadsense.models import plan_risk, risk
from roadsense.synthetic import build
from roadsense.synthetic.common import load_inputs

CFG_A = {"labels": {"snap_m": 25, "frozen_year": 2025, "windows": {
    "Southampton": {"usable": [2015, 2019], "train": [2015, 2017], "test": [2018, 2019]}}}}


def test_quantile_map_keeps_order_and_matches_target():
    rng = np.random.default_rng(0)
    council = rng.poisson(2.0, 1000).astype(float)                # ~20x the report rate
    fms = np.r_[np.zeros(900), np.ones(80), np.full(20, 2.0)]     # what the model was trained on
    m = plan_risk.quantile_map(council, fms)
    assert np.all(np.diff(m[np.argsort(council, kind="stable")]) >= 0)       # rank preserved
    assert set(np.unique(m)) <= {0.0, 1.0, 2.0}
    assert m.mean() < council.mean() / 5                              # scaled down to the report rate


@pytest.fixture(scope="module")
def plan_world(tmp_path_factory):
    cfg = small_config()
    cfg["as_of"] = "2025-06-02"
    cfg["potholes"]["yearly_totals"] = {2023: 600, 2024: 600}
    cfg["city"] = "Southampton"
    cfg["real_map_cities"] = ["Southampton"]      # exercise the map-only real-history path on the fixture city
    root = tmp_path_factory.mktemp("plan")
    (root / "config.yaml").write_text(yaml.safe_dump(CFG_A))
    build.run(cfg, root=root, verbose=False)
    risk.run(cfg, root=root, verbose=False)
    seg = load_inputs(cfg, root)["segments"].copy()
    seg["city"] = "Southampton"
    seg["is_main_road"] = seg["highway"].isin(["primary", "secondary", "tertiary"])
    seg["bus_routes"] = seg["bus_stop"].astype(int)
    rng = np.random.default_rng(1)
    rate = 0.03 * (1 + np.log1p(seg["esal_day"].values))
    rows = [(seg.at[i, "segment_id"], f"{y}-03-01T10:00:00+00:00")
            for y in range(2015, 2020) for i in range(len(seg)) for _ in range(rng.poisson(rate[i]))]
    rep = pd.DataFrame(rows, columns=["segment_id", "requested"])
    rep["city"], rep["snap_dist_m"] = "Southampton", 3.0
    res = plan_risk.run(cfg, root=root, verbose=False, inputs={"seg": seg, "labels": None, "reports": rep, "wx": None})
    return cfg, root, seg, res


def test_plan_risk_output_contract(plan_world):
    cfg, root, seg, res = plan_world
    out = pd.read_parquet(root / "outputs" / "risk_latest.parquet")
    contract = ["segment_id", "ward", "week_start", "horizon_week", "p7", "p30", "p30_trend", "wear", "reason_1",
                "reason_2", "reason_3", "model_version", "trained_on", "uses_synthetic", "weather_source",
                "weather_data_date", "uses_forecast", "forecast_retrieved_at", "history_source"]
    assert [c for c in contract if c not in out] == []
    assert sorted(out["horizon_week"].unique()) == [1, 2, 3, 4]
    assert not out.duplicated(["segment_id", "horizon_week"]).any()
    assert (out["trained_on"] == "real").all() and (out["history_source"] == "synthetic").all()
    assert out["uses_synthetic"].all()
    assert out[["p7", "p30"]].apply(lambda c: c.between(0, 1)).all().all()
    assert (root / "outputs" / "risk_synthetic_latest.parquet").exists()


def test_ranking_within_ward_follows_real_model(plan_world):
    _, root, _, _ = plan_world
    out = pd.read_parquet(root / "outputs" / "risk_latest.parquet")
    for (_, _), g in out.groupby(["ward", "horizon_week"]):
        a = g["p30"].rank(method="average").values
        b = g["p_report_year"].rank(method="average").values
        assert np.corrcoef(a, b)[0, 1] > 0.999 if len(g) > 2 else True     # weather multiplier never reorders a ward


def test_yearly_total_matches_city_figure(plan_world):
    cfg, root, _, _ = plan_world
    out = pd.read_parquet(root / "outputs" / "risk_latest.parquet")
    lam = out.drop_duplicates("segment_id")["lambda_year"].sum()
    assert lam == pytest.approx(float(cfg["potholes"]["yearly_totals"][int(cfg["potholes"]["current_year_rate_from"])]))


def test_real_history_map_city(plan_world):
    _, root, seg, res = plan_world
    out = pd.read_parquet(root / "outputs" / "risk_southampton_latest.parquet")
    assert res["maps"] == {"Southampton": out["segment_id"].nunique()}
    assert sorted(out["horizon_week"].unique()) == [1, 2, 3, 4]
    assert (out["history_source"] == "real").all() and (out["trained_on"] == "real").all()
    assert not out["uses_synthetic"].any()
    assert out["wear"].isna().all()
    assert out[["p7", "p30"]].apply(lambda c: c.between(0, 1)).all().all()
    assert out["scale_source"].str.startswith("assumed").all()
