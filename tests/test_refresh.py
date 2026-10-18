"""Daily refresh (roadsense.refresh): mocked HTTP only, everything written to a temp folder.

One full refresh runs once per module (about 2-3 minutes: it re-scores both cities with the frozen models).
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from roadsense.refresh import live, run
from roadsense.refresh.validate import validate

REPO = run.REPO
NOW = datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc)
SEG = pd.read_parquet(REPO / "data" / "processed" / "segments.parquet")
FMS = pd.read_csv(REPO / "data" / "processed" / "fms_roads_clean.csv")
# a stored Southampton report that is open in the snapshot and inside the 120-day open window at NOW
_t = pd.to_datetime(FMS["requested"], utc=True, format="mixed")
CLOSED_UPSTREAM = FMS[(FMS["city"] == "Southampton") & (FMS["status"] == "open") &
                      (_t >= pd.Timestamp(NOW) - pd.Timedelta(days=120))].iloc[0]


def _sites():
    s = SEG[(SEG["city"] == "Southampton") & SEG["is_main_road"] & (SEG["owner"] == "council")]
    return s.iloc[100], s.iloc[900]


def fake_http(now=NOW, fail=False):
    a, b = _sites()
    calls = []

    def get_json(url):
        calls.append(url)
        if fail:
            raise RuntimeError("simulated network failure")
        if "open311" in url:
            from urllib.parse import parse_qs, urlsplit
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            rows = []
            if q["agency_responsible"] == "2567" and q["service_code"] == CLOSED_UPSTREAM["category"]:
                c = CLOSED_UPSTREAM
                rows.append({"service_request_id": int(c["id"]), "service_name": c["category"],
                             "requested_datetime": c["requested"], "updated_datetime": (now - timedelta(days=1)).isoformat(),
                             "status": "closed", "lat": str(c["lat"]), "long": str(c["lon"]), "title": str(c["title"]),
                             "description": ""})
            if q["agency_responsible"] == "2567" and q["service_code"] == "Potholes":
                t = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
                return {"service_requests": [
                    {"service_request_id": 99000001, "service_name": "Potholes", "requested_datetime": t,
                     "updated_datetime": t, "status": "open", "lat": str(a["lat"]), "long": str(a["lon"]),
                     "title": "Deep pothole outside 23 Acacia Avenue, SO15 2AB, reported by Mr Smith",
                     "description": "Large pothole in the road, cars swerving"},
                    {"service_request_id": 99000002, "service_name": "Potholes", "requested_datetime": t,
                     "updated_datetime": t, "status": "open", "lat": str(b["lat"]), "long": str(b["lon"]),
                     "title": "Pothole in carriageway", "description": "Pothole in the road surface"}] + rows}
            return {"service_requests": rows}
        if "open-meteo" in url:
            start = (pd.Timestamp(now).floor("D") - pd.Timedelta(days=92)).tz_localize(None)
            times = pd.date_range(start, periods=(92 + 16) * 24, freq="h")
            i = np.arange(len(times))
            return {"hourly": {"time": times.strftime("%Y-%m-%dT%H:%M").tolist(),
                               "temperature_2m": (6 + 5 * np.sin(i / 24 * 2 * np.pi)).round(1).tolist(),
                               "precipitation": np.where(i % 30 == 0, 2.0, 0.0).tolist(),
                               "snowfall": [0.0] * len(times), "soil_temperature_0_to_7cm": [7.0] * len(times),
                               "soil_moisture_0_to_7cm": [0.3] * len(times)}}
        raise AssertionError(f"unexpected URL (refresh must not call it): {url}")
    get_json.calls = calls
    return get_json


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    import geopandas as gpd
    from roadsense.ingest import weather as W
    base = tmp_path_factory.mktemp("live")
    http = fake_http()
    real_read = gpd.read_file

    def no_raw(path, *a, **k):
        assert "raw" not in Path(str(path)).parts, f"refresh read {path}: data/raw/ is not committed"
        return real_read(path, *a, **k)

    def no_gpkg(*a, **k):
        raise AssertionError("refresh must use data/processed/ward_points_<city>.csv, not the ward gpkg")
    with pytest.MonkeyPatch.context() as m:               # a fresh clone has no data/raw/
        m.setattr(gpd, "read_file", no_raw)
        m.setattr(W, "ward_centroids", no_gpkg)
        code = run.refresh(now=NOW, get_json=http, client=None, base=base, verbose=False)
    return {"base": base, "code": code, "http": http}


def test_refresh_builds_and_switches(built):
    assert built["code"] == 0, (built["base"] / "refresh.log").read_text()
    build = live.current_build(built["base"])
    assert build is not None and build.name == NOW.strftime("%Y%m%dT%H%M%SZ")
    info = live.build_info(build)
    assert info["fixmystreet"]["Southampton"]["new_labels"] == 2
    assert not validate(build, REPO, run.CITIES)


def test_never_calls_osm_flood_wards_or_dft(built):
    hosts = {u.split("/")[2] for u in built["http"].calls}
    assert hosts <= {"www.fixmystreet.com", "api.open-meteo.com"}, hosts


def test_segment_ids_unchanged(built):
    build = live.current_build(built["base"])
    assert pd.read_parquet(build / "data" / "processed" / "segments.parquet")["segment_id"].equals(SEG["segment_id"])
    for f in ["risk_latest.parquet", "risk_synthetic_latest.parquet", "risk_brighton_latest.parquet"]:
        old, new = pd.read_parquet(REPO / "outputs" / f), pd.read_parquet(build / "outputs" / f)
        assert len(new) == len(old) and set(new["segment_id"]) == set(old["segment_id"])


def test_new_reports_are_redacted(built):
    build = live.current_build(built["base"])
    f = pd.read_csv(build / "data" / "processed" / "fms_roads_clean.csv")
    t = f.loc[f["id"] == 99000001, "title"].iloc[0]
    assert "23" not in t and "Smith" not in t and "SO15" not in t, t
    assert "[number]" in t and "[postcode]" in t and "[name]" in t, t
    assert "description" not in f.columns
    triage = json.loads((build / "outputs" / "lane_c" / "triage_southampton.json").read_text(encoding="utf-8"))
    assert all("Smith" not in r["title"] for r in triage["reports"])


def test_failed_fetch_keeps_previous_outputs(built):
    base = built["base"]
    before = live.current_build(base)
    later = NOW + timedelta(days=1)
    code = run.refresh(now=later, get_json=fake_http(later, fail=True), client=None, base=base, verbose=False)
    assert code == 1
    assert live.current_build(base) == before
    assert not (base / later.strftime("%Y%m%dT%H%M%SZ")).exists(), "failed build must be removed"
    assert "FAILED" in (base / "refresh.log").read_text(encoding="utf-8").splitlines()[-1]


def test_validation_rejects_a_broken_build(built, tmp_path):
    import shutil
    stage = tmp_path / "broken"
    shutil.copytree(live.current_build(built["base"]), stage)
    r = pd.read_parquet(stage / "outputs" / "risk_latest.parquet")
    r.loc[0, "p30"] = np.nan
    r.iloc[:-4].to_parquet(stage / "outputs" / "risk_latest.parquet", index=False)
    errs = validate(stage, REPO, run.CITIES)
    assert any("risk_latest.parquet" in e and "rows" in e for e in errs)
    assert any("NaN" in e for e in errs)


def test_app_reads_live_build_when_present(built, monkeypatch):
    from roadsense.prevent import inputs as I
    monkeypatch.setenv("ROADSENSE_LIVE_DIR", str(built["base"]))
    cfg = I.load_config()
    assert cfg["live_build"] == live.current_build(built["base"]).name
    assert Path(cfg["paths"]["outputs_dir"]).parent == live.current_build(built["base"])
    assert cfg["paths"]["state_dir"] == "outputs/lane_c", "app state never moves to the live build"
    du = live.data_updated(None, now=NOW + timedelta(hours=1))
    assert du["source"] == "live" and not du["stale"]
    assert live.data_updated(None, now=NOW + timedelta(hours=40))["stale"]


def test_app_uses_committed_snapshot_without_live_dir(tmp_path, monkeypatch):
    from roadsense.prevent import inputs as I
    monkeypatch.setenv("ROADSENSE_LIVE_DIR", str(tmp_path / "absent"))
    cfg = I.load_config()
    assert "live_build" not in cfg
    assert cfg["paths"]["processed_dir"] == "data/processed" and cfg["paths"]["outputs_dir"] == "outputs"
    du = live.data_updated("2026-10-17T12:00:00+00:00", now=NOW)
    assert du["source"] == "snapshot" and du["timestamp"].startswith("2026-10-17")


def test_prune_keeps_last_seven(tmp_path):
    for d in range(1, 11):
        b = tmp_path / f"202610{d:02d}T060000Z"
        (b / "data" / "processed").mkdir(parents=True)
        (b / "data" / "processed" / "segments.parquet").write_bytes(b"x")
        (b / "refresh.json").write_text("{}")
    live.switch_current(tmp_path, tmp_path / "20261002T060000Z")      # an old build is current: never deleted
    removed = live.prune(tmp_path)
    left = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert "20261002T060000Z" in left and len(left) == 8 and len(removed) == 2


def test_user_agent_contact_from_env(monkeypatch):
    from roadsense.refresh import fetch
    monkeypatch.setenv("ROADSENSE_CONTACT", "")
    assert fetch.user_agent() == f"RoadSense/1.0 (SotonShip26 hackathon; {fetch.GITHUB_URL})"
    monkeypatch.setenv("ROADSENSE_CONTACT", "highways-demo@example.org")
    assert fetch.user_agent() == "RoadSense/1.0 (SotonShip26 hackathon; highways-demo@example.org)"


def test_no_email_hard_coded_in_refresh():
    import re
    for p in (REPO / "roadsense" / "refresh").glob("*.py"):
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", p.read_text(encoding="utf-8")), p.name


def test_one_fixmystreet_pull_per_day(built):
    later = NOW + timedelta(hours=3)                                   # same UTC day as the first build
    http = fake_http(later)
    code = run.refresh(now=later, get_json=http, client=None, base=built["base"], verbose=False)
    assert code == 0, (built["base"] / "refresh.log").read_text()
    assert not any("open311" in u for u in http.calls), "FixMyStreet must be pulled at most once a day"
    assert any("open-meteo" in u for u in http.calls)
    info = live.build_info(live.current_build(built["base"]))
    assert "skipped" in info["fixmystreet"]


def test_report_closed_upstream_leaves_the_open_set(built):
    from roadsense.agent import city_triage as CT
    build = built["base"] / NOW.strftime("%Y%m%dT%H%M%SZ")             # the first build (later tests move current)
    f = pd.read_csv(build / "data" / "processed" / "fms_roads_clean.csv")
    rid = int(CLOSED_UPSTREAM["id"])
    assert f.loc[f["id"] == rid, "status"].iloc[0] == "closed"
    _, _, cfg_c = run.stage_configs(build, REPO)
    open_ids = set(CT.open_reports(cfg_c, "Southampton", build, now=NOW)["id"].astype(int))
    assert rid not in open_ids
    snap_open = set(CT.open_reports(cfg_c, "Southampton", REPO, now=NOW)["id"].astype(int))
    assert rid in snap_open, "the report was open in the committed snapshot"
    rc = live.build_info(build)["fixmystreet"]["Southampton"]["status_recheck"]
    assert rc["open_to_closed"] >= 1 and rc["in_window"] >= 1


def test_later_refresh_moves_as_of_and_forecast_weeks(built):
    later = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)          # Tuesday: as_of -> Monday 26 Oct
    code = run.refresh(cities=["Brighton"], now=later, get_json=fake_http(later), client=None, base=built["base"],
                       verbose=False)
    assert code == 0, (built["base"] / "refresh.log").read_text()
    build = live.current_build(built["base"])
    info = live.build_info(build)
    assert info["as_of"] == "2026-10-26"
    assert info["forecast_weeks"]["Brighton"] == ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23"]
    r = pd.read_parquet(build / "outputs" / "risk_brighton_latest.parquet")
    assert set(r["as_of"]) == {"2026-10-26"}
    fc = pd.read_parquet(build / "data" / "processed" / "weather_forecast_weekly.parquet")
    assert (fc.loc[fc["city"] == "Brighton", "week_start"] > "2026-10-27").all()


def test_southampton_stops_when_synthetic_history_ends(built):
    base = built["base"]
    before = live.current_build(base)
    later = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)          # as_of 19 Oct: after the synthetic log ends
    http = fake_http(later)
    code = run.refresh(cities=["Southampton"], now=later, get_json=http, client=None, base=base, verbose=False)
    assert code == 1
    assert live.current_build(base) == before
    msg = (base / "refresh.log").read_text(encoding="utf-8").splitlines()[-1]
    assert "cannot build as_of 2026-10-19" in msg and "synthetic council history" in msg and "potholes_seg_8w" in msg
    assert not http.calls, "it must stop before any network call"
