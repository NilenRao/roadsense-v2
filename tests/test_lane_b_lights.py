"""M7 tests: streetlight Weibull model and burn-hour clock (small fixture, no network)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest_lane_b import small_config
from roadsense.models import lights
from roadsense.synthetic import build


@pytest.fixture(scope="module")
def lit(tmp_path_factory):
    cfg = small_config()
    root = tmp_path_factory.mktemp("m7")
    build.run(cfg, root=root, verbose=False)
    res = lights.run(cfg, root=root, verbose=False)
    return cfg, root, res


def test_clock_applies_switch_off():
    cfg = small_config()
    clock = lights.BurnClock(cfg, "2022-11-01", "2024-03-01")
    s = lambda a, b: float(clock.between(pd.Series([pd.Timestamp(a)]), pd.Series([pd.Timestamp(b)]))[0])
    before = s("2022-12-01", "2022-12-31") / 30
    after = s("2023-12-01", "2023-12-31") / 30
    assert after == pytest.approx(before - 4.5, abs=0.3)


def test_clock_without_switch_off_is_longer():
    cfg = small_config()
    cfg_off = small_config()
    cfg_off["streetlights"]["switch_off"] = None
    a = lights.BurnClock(cfg, "2024-01-01", "2024-12-31")
    b = lights.BurnClock(cfg_off, "2024-01-01", "2024-12-31")
    yr_on, yr_off = a.cum[-1], b.cum[-1]
    assert 4000 < yr_off < 4500                      # about 4,300 h of darkness a year at 51 N
    assert yr_off - yr_on == pytest.approx(366 * 4.5, rel=0.12)   # summer sunrise cuts the overlap


def test_weibull_recovers_known_parameters():
    rng = np.random.default_rng(0)
    life = 60000 * rng.weibull(3.0, 4000)
    cens = rng.uniform(20000, 90000, 4000)
    surv = pd.DataFrame({"unit_type": "LED", "hours": np.minimum(life, cens), "event": (life <= cens).astype(int), "entry": 0.0})
    p = lights.fit_weibull(surv)["LED"]
    assert p["scale_h"] == pytest.approx(60000, rel=0.05)
    assert p["shape"] == pytest.approx(3.0, rel=0.1)


def test_lights_output(lit):
    cfg, root, res = lit
    out = pd.read_parquet(res["out"])
    lamps = pd.read_csv(root / "data" / "synthetic" / "streetlights.csv")
    assert set(out["lamp_id"]) == set(lamps["lamp_id"])
    assert out["p_fail_90d"].between(0, 1).all()
    assert out["replace_by"].notna().all()
    assert out["uses_synthetic"].all()
    assert [c for c in ["lamp_id", "segment_id", "lat", "lon", "lamp_type", "p_fail_90d", "replace_by", "currently_dark",
                        "dark_source", "uses_synthetic"] if c not in out] == []
    faults = pd.read_csv(root / "data" / "synthetic" / "light_faults.csv")
    open_recent = faults[(faults["status"] == "open")
                         & (pd.to_datetime(faults["fault_date"]) >= pd.Timestamp(cfg["as_of"]) - pd.Timedelta(days=30))]
    assert set(out.loc[out["currently_dark"], "lamp_id"]) == set(open_recent["lamp_id"])
    assert (root / "reports" / "lights_eval.md").exists()


def test_older_units_have_higher_failure_probability(lit):
    _, _, res = lit
    out = pd.read_parquet(res["out"])
    led = out[out["lamp_type"] == "LED"].sort_values("burn_hours")
    q = len(led) // 4
    assert led["p_fail_90d"].iloc[-q:].mean() > led["p_fail_90d"].iloc[:q].mean()
