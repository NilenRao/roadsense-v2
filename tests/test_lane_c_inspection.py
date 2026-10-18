"""Brighton inspection list: real inputs only, open reports first, no money or treatment (tiny fixture)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from roadsense.api.cities import CityView
from roadsense.prevent import inputs as I


@pytest.fixture()
def view(tmp_path):
    cfg = I.load_config()
    proc = tmp_path / "data" / "processed"
    proc.mkdir(parents=True)
    n = 70
    seg = pd.DataFrame({
        "segment_id": [f"T-{i}-00" for i in range(n)], "city": "Testcity", "name": [f"Road {i}" for i in range(n)],
        "ref": "", "highway": ["primary"] * (n - 1) + ["motorway"], "ward": "North",
        "owner": ["council"] * (n - 1) + ["national_highways"],
        "geometry": [f"LINESTRING ({-0.1 + i * 0.01:.4f} 50.83, {-0.1 + i * 0.01 + 0.001:.4f} 50.83)" for i in range(n)]})
    seg.to_parquet(proc / "segments.parquet", index=False)
    od = tmp_path / "outputs"
    od.mkdir()
    rank = [1.0 - i / n for i in range(n)]
    rank[-1] = 1.0                                         # the motorway would top the list if not skipped
    pd.DataFrame({"segment_id": seg["segment_id"], "horizon_week": 1, "p30": rank, "p7": rank, "rank_pct": rank,
                  "reason_1": "21 bus routes use this road", "trained_on": "real", "uses_synthetic": False,
                  "history_source": "real", "model_version": "risk@t", "as_of": "2026-10-12"}).to_parquet(
        od / "risk_testcity_latest.parquet", index=False)
    return cfg, CityView(cfg, "Testcity", tmp_path)


def test_inspection_list_rows(view):
    cfg, v = view
    low = v.df[v.df["segment_id"] == "T-60-00"].iloc[0]     # a low-ranked segment with an open report
    rep = pd.DataFrame({"id": [7], "reported_at": ["2026-10-01"], "title": ["Large pothole"],
                        "lat": [50.83], "lon": [-0.1 + 60 * 0.01 + 0.0005]})
    out = v.inspection_list(rep, cfg, top_n=50)
    rows = out["rows"]
    assert out["count"] == 51 and out["open_report_rows"] == 1
    assert rows[0]["segment_id"] == low["segment_id"] and rows[0]["open_report"]       # open reports first
    assert all(not r["open_report"] for r in rows[1:])
    assert "T-69-00" not in {r["segment_id"] for r in rows}                            # National Highways skipped
    assert all(r["history_source"] == "real" and not r["uses_synthetic"] for r in rows)
    assert all(r["action"] == "Inspect" for r in rows)
    blob = json.dumps(out)
    for word in ["£", "gbp", "treatment_label", "cost_", "risk_label", "Top 1%"]:
        assert word not in blob.replace("treatments are chosen on inspection", "")
    assert out["badge"] == "100% real inputs"
    assert [x["label"] for x in out["sections"]] == ["Open reports (inspect first)", "Highest predicted risk"]
    assert [x["count"] for x in out["sections"]] == [1, 50]
    assert rows[0]["section"] == "open" and all(r["section"] == "risk" for r in rows[1:])
    assert out["header"].startswith("Testcity inspection list: ranked by urgency from real FixMyStreet reports.")
    assert all(r["band"] in {"critical", "high", "prevention", "monitor"} for r in rows)
    risk = [r["urgency"] for r in rows[1:]]
    assert risk == sorted(risk, reverse=True)                                          # risk section by urgency


def test_weather_summaries_are_never_reasons():
    from roadsense.models import plan_risk, real_risk
    assert set(real_risk.WEATHER) <= plan_risk.NEVER_A_REASON
    assert plan_risk.REAL_HISTORY_REASONS["rep_prev1"]({"rep_prev1": 1.0}) == "1 FixMyStreet pothole report here last year"
    assert plan_risk.REAL_HISTORY_REASONS["rep_prev1"]({"rep_prev1": 3.0}) == "3 FixMyStreet pothole reports here last year"
