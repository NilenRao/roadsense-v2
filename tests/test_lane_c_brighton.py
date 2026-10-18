"""Slip-road names and the map-only city's open reports and cached triage (tiny fixtures, no network)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from roadsense.agent import city_triage as CT
from roadsense.prevent import inputs as I


def _seg(rows):
    return pd.DataFrame(rows, columns=["segment_id", "name", "ref", "highway", "ward", "geometry"])


def test_slip_road_named_from_the_road_it_joins():
    seg = _seg([
        ("a", "", "A33", "trunk", "W1", "LINESTRING (0 0, 0.001 0)"),
        ("l1", "", "", "trunk_link", "W1", "LINESTRING (0.001 0, 0.002 0.001)"),   # touches the A33
        ("l2", "", "", "trunk_link", "W1", "LINESTRING (0.002 0.001, 0.003 0.002)"),  # two hops away
        ("x", "", "", "primary_link", "Bevois", "LINESTRING (1 1, 1.001 1)"),      # joins nothing named
        ("r", "", "", "residential", "W1", "LINESTRING (2 2, 2.001 2)"),
        ("n", "High Street", "", "secondary", "W1", "LINESTRING (3 3, 3.001 3)"),
    ])
    names = I.display_names(seg).tolist()
    assert names == ["A33", "Slip road off A33", "Slip road off A33", "Unnamed slip road near Bevois",
                     "Unnamed residential road", "High Street"]


@pytest.fixture()
def city_root(tmp_path):
    cfg = I.load_config()
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    proc = tmp_path / "data" / "processed"
    proc.mkdir(parents=True)
    seg = pd.DataFrame({
        "segment_id": ["T-1-00", "T-2-00"], "city": ["Testcity"] * 2, "name": ["Main Road", ""], "ref": ["", ""],
        "highway": ["primary", "primary_link"], "ward": ["North", "North"], "owner": ["council"] * 2,
        "geometry": ["LINESTRING (-0.1000 50.8300, -0.0990 50.8300)", "LINESTRING (-0.0990 50.8300, -0.0980 50.8310)"]})
    seg.to_parquet(proc / "segments.parquet", index=False)
    pd.DataFrame({
        "city": ["Testcity"] * 5,
        "id": [1, 2, 3, 4, 5],
        "category": ["Pavement and road repairs"] * 5,
        "requested": ["2026-10-01T10:00:00+00:00", "2026-10-05T10:00:00+00:00", "2019-03-01T10:00:00+00:00",
                      "2026-09-20T10:00:00+00:00", "2026-09-25T10:00:00+00:00"],
        "status": ["open", "open", "open", "closed", "open"],
        "lat": [50.83, 50.83003, 50.83, 50.83, 50.95], "lon": [-0.0995, -0.0995, -0.0995, -0.0995, -0.2],
        "title": ["Pothole outside 12 Acacia Avenue BN1 2AB", "Large pothole", "Old hole", "Pothole", "Pothole far away"],
    }).to_csv(proc / "fms_roads_clean.csv", index=False)
    od = tmp_path / "outputs"
    od.mkdir()
    pd.DataFrame({"segment_id": ["T-1-00", "T-2-00"] * 4, "horizon_week": [1, 1, 2, 2, 3, 3, 4, 4], "p30": [0.2, 0.01] * 4,
                  "p7": [0.05, 0.002] * 4, "rank_pct": [1.0, 0.5] * 4, "trained_on": "real", "uses_synthetic": False,
                  "history_source": "real", "model_version": "risk@t", "as_of": "2026-10-12"}).to_parquet(
        od / "risk_testcity_latest.parquet", index=False)
    return cfg, tmp_path, now


def test_city_open_reports_keep_the_open_window(city_root):
    cfg, root, now = city_root
    o = CT.open_reports(cfg, "Testcity", root, now=now)
    assert sorted(o["id"]) == [1, 2, 5]                 # 2019 "open" and the closed report are left out
    assert "12" not in o.loc[o["id"] == 1, "title"].iloc[0] and "BN1" not in o.loc[o["id"] == 1, "title"].iloc[0]


def test_city_triage_is_cached_redacted_and_merges_duplicates(city_root):
    cfg, root, now = city_root
    res = CT.run(cfg, "Testcity", root, limit=30, client=None, now=now, verbose=False)
    cached = json.loads((root / "outputs" / "lane_c" / "triage_testcity.json").read_text(encoding="utf-8"))
    assert cached["count"] == 3 and res["methods"] == ["keyword"]
    by = {r["fms_id"]: r for r in cached["reports"]}
    assert by[1]["segment_id"] == "T-1-00" and by[1]["category"] == "pothole"
    assert by[2]["merged_into"] == "fms-1"                # same defect within 15 m and 14 days
    assert by[5]["status"] == "not on a council road"
    assert all("BN1" not in r["title"] and "12 Acacia" not in r["title"] for r in cached["reports"])


def test_gemini_answer_is_used_when_valid():
    class Resp:
        text = "Pothole."

    class Models:
        def generate_content(self, model, contents, config=None):
            return Resp()

    class Client:
        models = Models()

    cfg = I.load_config()
    cfg["triage"]["min_interval_s"] = 0
    assert CT.classify("hole in road", cfg, Client())[:2] == ("pothole", "gemini")
    assert CT.classify("Large pothole", cfg, None)[:2] == ("pothole", "keyword")
