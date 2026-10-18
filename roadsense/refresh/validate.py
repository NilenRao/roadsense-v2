"""Checks a refresh build against the committed snapshot before `current` may point at it.

Must match the snapshot exactly: segments.parquet segment_ids, and the row count, columns and
segment_id set of every risk file (4 weekly rows per segment, so the rows are fixed).
Allowed to grow, never shrink: FixMyStreet labels and weekly weather (new reports, new weeks); the
plan and forecast files keep the snapshot's columns but their row counts follow the data.
No NaN in key columns anywhere.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

RISK_FILES = {"Southampton": ["risk_latest.parquet", "risk_synthetic_latest.parquet"],
              "Brighton": ["risk_brighton_latest.parquet"]}
RISK_KEYS = ["segment_id", "horizon_week", "p7", "p30"]
WEATHER_KEYS = ["ward", "week_start", "rain_3d", "rain_7d", "rain_30d", "rain_90d", "hours_below_0_7d",
                "freeze_thaw_14d", "freeze_thaw_30d", "rain_then_frost_7d", "hot_days_7d"]


def _nan(df: pd.DataFrame, cols: list[str], name: str) -> list[str]:
    bad = [c for c in cols if c in df and df[c].isna().any()]
    return [f"{name}: NaN in {', '.join(bad)}"] if bad else []


def _same_columns(new: pd.DataFrame, old: pd.DataFrame, name: str) -> list[str]:
    miss, extra = set(old.columns) - set(new.columns), set(new.columns) - set(old.columns)
    return [f"{name}: columns differ (missing {sorted(miss)}, extra {sorted(extra)})"] if miss or extra else []


def validate(stage: Path, snapshot: Path, cities: list[str]) -> list[str]:
    """Returns a list of problems; empty means the build may go live."""
    errs: list[str] = []
    sp, np_ = snapshot / "data" / "processed", stage / "data" / "processed"
    so, no = snapshot / "outputs", stage / "outputs"

    seg_old, seg_new = pd.read_parquet(sp / "segments.parquet"), pd.read_parquet(np_ / "segments.parquet")
    if not seg_new["segment_id"].equals(seg_old["segment_id"]):
        errs.append("segments.parquet: segment_ids differ from the committed snapshot")
    errs += _same_columns(seg_new, seg_old, "segments.parquet")

    for city in cities:
        for f in RISK_FILES.get(city, []):
            if not (no / f).exists():
                errs.append(f"{f}: missing")
                continue
            new, old = pd.read_parquet(no / f), pd.read_parquet(so / f)
            errs += _same_columns(new, old, f)
            if len(new) != len(old):
                errs.append(f"{f}: {len(new)} rows, snapshot has {len(old)}")
            if set(new["segment_id"]) != set(old["segment_id"]):
                errs.append(f"{f}: segment_ids differ from the snapshot")
            errs += _nan(new, RISK_KEYS, f)
            if ((new["p7"] < 0) | (new["p7"] > 1) | (new["p30"] < 0) | (new["p30"] > 1)).any():
                errs.append(f"{f}: p7/p30 outside 0-1")

    fms_new = pd.read_csv(np_ / "fms_roads_clean.csv")
    fms_old = pd.read_csv(sp / "fms_roads_clean.csv")
    errs += _same_columns(fms_new, fms_old, "fms_roads_clean.csv")
    if len(fms_new) < len(fms_old):
        errs.append(f"fms_roads_clean.csv: {len(fms_new)} rows, fewer than the snapshot's {len(fms_old)}")
    if not fms_new["id"].is_unique:
        errs.append("fms_roads_clean.csv: duplicate report ids")
    errs += _nan(fms_new, ["city", "id", "requested", "lat", "lon", "status"], "fms_roads_clean.csv")

    wx_new, wx_old = pd.read_parquet(np_ / "weather_weekly.parquet"), pd.read_parquet(sp / "weather_weekly.parquet")
    errs += _same_columns(wx_new, wx_old, "weather_weekly.parquet")
    if len(wx_new) < len(wx_old):
        errs.append(f"weather_weekly.parquet: {len(wx_new)} rows, fewer than the snapshot's {len(wx_old)}")
    if wx_new.duplicated(["ward", "week_start"]).any():
        errs.append("weather_weekly.parquet: duplicate ward-weeks")
    errs += _nan(wx_new, WEATHER_KEYS, "weather_weekly.parquet")
    fc_new, fc_old = pd.read_parquet(np_ / "weather_forecast_weekly.parquet"), pd.read_parquet(sp / "weather_forecast_weekly.parquet")
    errs += _same_columns(fc_new, fc_old, "weather_forecast_weekly.parquet")
    errs += _nan(fc_new, WEATHER_KEYS, "weather_forecast_weekly.parquet")
    for city in cities:
        want = set(seg_old.loc[seg_old["city"] == city, "ward"])
        got = set(fc_new.loc[fc_new["city"] == city, "ward"])
        if want != got:
            errs.append(f"weather_forecast_weekly.parquet: {city} wards differ from segments.parquet")

    if "Southampton" in cities:
        for f in ["prevention_plan_latest.csv"]:
            new, old = pd.read_csv(no / f), pd.read_csv(so / f)
            errs += _same_columns(new, old, f)
            errs += _nan(new, ["rank", "segment_id", "p30"], f)
            if not set(new["segment_id"]) <= set(seg_old["segment_id"]):
                errs.append(f"{f}: segment_ids not in segments.parquet")
    for city in cities:
        p = no / "lane_c" / f"triage_{city.lower()}.json"
        if p.exists():
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except ValueError as e:
                errs.append(f"{p.name}: not valid JSON ({e})")
    return errs
