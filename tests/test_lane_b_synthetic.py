"""M4 tests: synthetic council data (small fixture, mock inputs, no network)."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from conftest_lane_b import small_config
from roadsense.synthetic import build
from roadsense.synthetic.streetlights import night_burn_hours

REPO = Path(__file__).resolve().parents[1]
FILES = ["resurfacing.csv", "potholes.csv", "streetlights.csv", "light_faults.csv", "crew_outcomes.csv"]
TZ = ZoneInfo("Europe/London")


@pytest.fixture(scope="module")
def synth_run(tmp_path_factory):
    cfg = small_config()
    a, b = tmp_path_factory.mktemp("a"), tmp_path_factory.mktemp("b")
    ra = build.run(cfg, root=a, verbose=False)
    build.run(cfg, root=b, verbose=False)
    return cfg, a / "data" / "synthetic", b / "data" / "synthetic", ra


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_reproducible_from_seed(synth_run):
    _, a, b, _ = synth_run
    for f in FILES + [build.TRUTH_FILE]:
        assert _sha(a / f) == _sha(b / f), f


def test_every_row_labelled_synthetic(synth_run):
    _, a, _, _ = synth_run
    for f in FILES:
        df = pd.read_csv(a / f)
        assert len(df) > 0, f
        assert df["is_synthetic"].astype(str).str.lower().eq("true").all(), f
        gen = f.removesuffix(".csv").replace("crew_outcomes", "crew")
        assert df["source"].str.fullmatch(rf"synthetic:{gen}@\d+\.\d+").all(), f


def test_pothole_totals_match_targets(synth_run):
    cfg, a, _, _ = synth_run
    ph = pd.read_csv(a / "potholes.csv")
    from roadsense.synthetic.common import weeks
    from roadsense.synthetic.potholes import yearly_targets
    target = sum(yearly_targets(cfg, weeks(cfg["potholes"]["start"], cfg["as_of"])).values())
    assert abs(len(ph) - target) / target < 0.15


def test_resurfacing_shape(synth_run):
    _, a, _, _ = synth_run
    r = pd.read_csv(a / "resurfacing.csv")
    base = r[r["treatment"] == "last_resurfaced"]
    assert base["segment_id"].is_unique
    ev = r[r["treatment"] != "last_resurfaced"]
    assert set(ev["treatment"]) <= {"surface_dressing", "crack_sealing", "full_resurfacing"}
    # no full resurfacing after the recorded last_resurfaced date
    last = base.set_index("segment_id")["date"]
    full = ev[ev["treatment"] == "full_resurfacing"]
    assert (full["date"].values <= last.loc[full["segment_id"]].values).all()


def test_crew_outcomes(synth_run):
    _, a, _, _ = synth_run
    co = pd.read_csv(a / "crew_outcomes.csv")
    assert (co["selection"] == "random").sum() == 100
    assert set(co["outcome"]) <= {"pothole", "cracking", "none"}


def test_truth_not_read_by_models_or_features():
    """_truth.json is the hidden answer: no model or feature code may reference it."""
    pat = re.compile(r"_truth|TRUTH_FILE|hazard_last_week|u_segment")
    offenders = []
    for d in ["roadsense/models", "roadsense/features"]:
        for p in (REPO / d).rglob("*.py"):
            if pat.search(p.read_text(encoding="utf-8")):
                offenders.append(str(p.relative_to(REPO)))
    assert offenders == []


def _dt(d, hm):
    return datetime.combine(d, datetime.strptime(hm, "%H:%M").time(), TZ)


SO = {"start": "01:00", "end": "05:30", "from": "2023-12-01"}


def test_burn_hours_switch_off_winter():
    d = date(2024, 1, 10)
    sunset, sunrise = _dt(d, "16:20"), _dt(date(2024, 1, 11), "08:05")
    full = (sunrise - sunset).total_seconds() / 3600
    assert night_burn_hours(d, sunset, sunrise, SO) == pytest.approx(full - 4.5)


def test_burn_hours_before_policy_is_full_night():
    d = date(2023, 1, 10)
    sunset, sunrise = _dt(d, "16:20"), _dt(date(2023, 1, 11), "08:05")
    assert night_burn_hours(d, sunset, sunrise, SO) == pytest.approx((sunrise - sunset).total_seconds() / 3600)


def test_burn_hours_summer_sunrise_before_0530():
    d = date(2024, 6, 20)
    sunset, sunrise = _dt(d, "21:25"), _dt(date(2024, 6, 21), "04:58")
    # lit 21:25-01:00 only (3h35m); switch-off covers 01:00 to sunrise
    assert night_burn_hours(d, sunset, sunrise, SO) == pytest.approx(3 + 35 / 60)


def test_policy_starts_on_night_into_1_december():
    d = date(2023, 11, 30)
    sunset, sunrise = _dt(d, "16:05"), _dt(date(2023, 12, 1), "07:40")
    full = (sunrise - sunset).total_seconds() / 3600
    assert night_burn_hours(d, sunset, sunrise, SO) == pytest.approx(full - 4.5)


def test_astral_burn_series_drops_after_policy():
    from roadsense.synthetic.streetlights import burn_hours_series
    cfg = small_config()
    s = burn_hours_series(cfg, "2022-12-01", "2024-12-31")
    before = s["2022-12-01":"2022-12-31"].mean()
    after = s["2023-12-01":"2023-12-31"].mean()
    assert 15 < before < 17.5
    assert after == pytest.approx(before - 4.5, abs=0.3)
