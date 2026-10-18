"""
M2 weekly weather features per ward (schemas.md section 4).

    python -m roadsense.features.weather_feats Southampton [Portsmouth Brighton]

Reads data/raw/weather/{code}_hourly.parquet; writes data/processed/weather_weekly.parquet
(all requested cities stacked). NO LEAKAGE: every window covers hours strictly BEFORE week_start
(time < week_start), so a row for week W only uses weather known before W began.

Definitions
- rain_Nd: precipitation sum (mm) over the N days before week_start
- hours_below_0_7d: hours with temperature_2m < 0 °C in the 7 days before
- freeze_thaw_Nd: number of hours where temperature crosses 0 °C in either direction
  (sign of (T < 0) differs from the previous hour), in the N days before
- rain_then_frost_7d: frost onsets (an hour below 0 °C after an hour at/above 0 °C) that had
  >= 5 mm of precipitation in the 48 hours before them, in the 7 days before
- hot_days_7d: days whose maximum temperature > 25 °C, in the 7 days before
- uses_forecast: true if any hour in the 7 days before week_start came from the forecast
"""
import argparse, os

import numpy as np
import pandas as pd

from roadsense.ingest.fixmystreet import ROOT, load_config

FEATURES = ["rain_3d", "rain_7d", "rain_30d", "rain_90d", "hours_below_0_7d", "freeze_thaw_14d",
            "freeze_thaw_30d", "rain_then_frost_7d", "hot_days_7d"]


def hourly_indicators(h):
    """h: one ward's hourly frame sorted by time (UTC, regular hours)."""
    h = h.set_index("time").sort_index()
    h = h[~h.index.duplicated()].asfreq("h")                     # make gaps explicit
    t = h["temperature_2m"]
    rain = h["precipitation"].fillna(0)
    below = (t < 0)
    prev_below = below.shift(1)
    cross = (below != prev_below) & t.notna() & t.shift(1).notna()
    onset = below & (prev_below == False) & t.shift(1).notna()   # noqa: E712
    rain_48h_before = rain.shift(1).rolling(48, min_periods=1).sum()
    rtf = onset & (rain_48h_before >= 5)
    day_max = t.resample("D").max()
    hot_day = (day_max > 25).astype(float)
    hot_hourly = pd.Series(0.0, index=h.index)                   # mark at the day's last hour so "before" is honest
    ends = hot_day.index + pd.Timedelta(hours=23)
    hot_hourly.loc[hot_hourly.index.intersection(ends)] = hot_day.reindex(ends - pd.Timedelta(hours=23)).values[
        np.isin(ends, hot_hourly.index)]
    return pd.DataFrame({"rain": rain, "below": below.astype(float), "cross": cross.astype(float),
                         "rtf": rtf.astype(float), "hot": hot_hourly, "fc": h["is_forecast"].fillna(False).astype(float)})


def window_sum(ind, col, days, week_starts):
    cs = ind[col].cumsum()
    # sum over [ws - days, ws): cumsum at the last hour before ws minus cumsum at the last hour before (ws - days)
    end = cs.reindex(week_starts - pd.Timedelta(hours=1), method="ffill")
    start = cs.reindex(week_starts - pd.Timedelta(days=days) - pd.Timedelta(hours=1), method="ffill").fillna(0)
    return end.values - start.values


def weekly_for_ward(h):
    ind = hourly_indicators(h)
    first = ind.index.min() + pd.Timedelta(days=90)                   # need 90 days of history
    last = ind.index.max() + pd.Timedelta(hours=1)
    ws = pd.date_range(first.normalize() + pd.offsets.Week(weekday=0), last, freq="W-MON", tz="UTC")
    out = pd.DataFrame({"week_start": ws})
    for col, name, days in [("rain", "rain_3d", 3), ("rain", "rain_7d", 7), ("rain", "rain_30d", 30),
                            ("rain", "rain_90d", 90), ("below", "hours_below_0_7d", 7),
                            ("cross", "freeze_thaw_14d", 14), ("cross", "freeze_thaw_30d", 30),
                            ("rtf", "rain_then_frost_7d", 7), ("hot", "hot_days_7d", 7)]:
        out[name] = window_sum(ind, col, days, ws)
    out["uses_forecast"] = window_sum(ind, "fc", 7, ws) > 0
    out["rain_3d"] = out["rain_3d"].round(1); out["rain_7d"] = out["rain_7d"].round(1)
    out["rain_30d"] = out["rain_30d"].round(1); out["rain_90d"] = out["rain_90d"].round(1)
    return out


def build(cfg, cities):
    frames = []
    for city in cities:
        code = cfg["cities"][city]["code"]
        hp = pd.read_parquet(os.path.join(ROOT, cfg["paths"]["raw_dir"], "weather", f"{code}_hourly.parquet"))
        for ward, h in hp.groupby("ward"):
            wk = weekly_for_ward(h)
            wk.insert(0, "ward", ward); wk.insert(0, "city", city)
            wk["data_date"] = str(h["time"].max())[:16]
            wk["retrieved_at"] = h["retrieved_at"].iloc[0]
            frames.append(wk)
    out = pd.concat(frames, ignore_index=True)
    out["week_start"] = out["week_start"].dt.tz_localize(None).dt.date.astype(str)
    out["source"] = "open-meteo"
    proc = os.path.join(ROOT, cfg["paths"]["processed_dir"])
    path = os.path.join(proc, "weather_weekly.parquet")
    if os.path.exists(path):   # keep the cities not rebuilt this run
        old = pd.read_parquet(path)
        out = pd.concat([old[~old.city.isin(cities)], out], ignore_index=True)
    out.to_parquet(path, index=False)
    print(f"{len(out):,} ward-weeks, {out.ward.nunique()} wards, {out.week_start.min()} to {out.week_start.max()} -> {path}")

    # Forecast weeks: week_start after today (the next weeks, built from the Open-Meteo forecast)
    today = pd.Timestamp.now(tz="UTC").date().isoformat()
    fc = out[out.week_start > today].reset_index(drop=True)
    fc_path = os.path.join(proc, "weather_forecast_weekly.parquet")
    fc.to_parquet(fc_path, index=False)
    print(f"{len(fc):,} forecast ward-weeks ({', '.join(sorted(fc.week_start.unique()))}) -> {fc_path}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cities", nargs="*", default=["Southampton"])
    build(load_config(), ap.parse_args().cities)


if __name__ == "__main__":
    main()
