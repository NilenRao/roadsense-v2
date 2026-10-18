"""Mock lane A inputs (segments, esal_day, weekly ward weather).

Only used until lane A's data/processed/ outputs are merged. Mocks are deterministic
(seeded) and are never presented as real: rows carry input_source='mock'.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from roadsense.synthetic.common import to_lonlat, to_xy

MOCK_SEED_OFFSET = 7


def mock_segments(cfg: dict) -> pd.DataFrame:
    m = cfg["mock"]
    rng = np.random.default_rng(cfg["seed"] + MOCK_SEED_OFFSET)
    lon0, lat0, lon1, lat1 = m["bbox"]
    (x0, x1), (y0, y1) = to_xy([lon0, lon1], [lat0, lat1])
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    sp, L = m["grid_spacing_m"], m["segment_length_m"]
    kinds = list(m["highway_mix"])
    probs = np.array(list(m["highway_mix"].values()), float)
    probs /= probs.sum()
    rows = []
    way = 0
    for orient in ("h", "v"):
        coords = np.arange(y0 + sp / 2, y1, sp) if orient == "h" else np.arange(x0 + sp / 2, x1, sp)
        for c in coords:
            way += 1
            hw = rng.choice(kinds, p=probs)
            span = (x0, x1) if orient == "h" else (y0, y1)
            for k, a in enumerate(np.arange(span[0], span[1] - L, L)):
                if orient == "h":
                    p, q = (a, c), (a + L, c)
                else:
                    p, q = (c, a), (c, a + L)
                mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
                if ((mx - cx) / rx) ** 2 + ((my - cy) / ry) ** 2 > 1:
                    continue
                rows.append((way, k, hw, p, q, mx, my))
    df = pd.DataFrame(rows, columns=["way", "k", "highway", "p", "q", "mx", "my"])
    # wards: Voronoi cells around 17 seeded centres
    n = m["n_wards"]
    cxs = rng.uniform(x0 + 0.1 * (x1 - x0), x1 - 0.1 * (x1 - x0), n)
    cys = rng.uniform(y0 + 0.1 * (y1 - y0), y1 - 0.1 * (y1 - y0), n)
    d = (df["mx"].values[:, None] - cxs) ** 2 + (df["my"].values[:, None] - cys) ** 2
    ward_idx = d.argmin(1)
    lon, lat = to_lonlat(df["mx"].values, df["my"].values)
    plon, plat = to_lonlat([p[0] for p in df["p"]], [p[1] for p in df["p"]])
    qlon, qlat = to_lonlat([q[0] for q in df["q"]], [q[1] for q in df["q"]])
    out = pd.DataFrame({
        "segment_id": [f"M{w:04d}-{k:03d}" for w, k in zip(df["way"], df["k"])],
        "osm_way_id": df["way"].values,
        "name": [f"Mock Street {w}" for w in df["way"]],
        "highway": df["highway"].values,
        "ward": [f"Mock ward {i + 1:02d}" for i in ward_idx],
        "length_m": float(L),
        "geometry": [f"LINESTRING ({a:.6f} {b:.6f}, {c:.6f} {e:.6f})" for a, b, c, e in zip(plon, plat, qlon, qlat)],
        "lat": lat, "lon": lon,
    })
    nseg = len(out)
    out["bus_stop"] = rng.random(nseg) < m["bus_stop_share"] * np.where(out["highway"].isin(["primary", "secondary", "trunk"]), 3, 0.5)
    out["junction"] = rng.random(nseg) < m["junction_share"]
    # flood areas: a few contiguous blobs near the "waterfront" (south edge)
    fx = rng.uniform(x0, x1, 6)
    fy = rng.uniform(y0, y0 + 0.35 * (y1 - y0), 6)
    dist = np.sqrt((df["mx"].values[:, None] - fx) ** 2 + (df["my"].values[:, None] - fy) ** 2).min(1)
    out["in_flood_area"] = dist < np.quantile(dist, m["flood_share"])
    out["owner"] = np.where(out["highway"] == "motorway", "national_highways", "council")
    out["input_source"] = "mock"
    return out


def mock_esal(seg: pd.DataFrame, cfg: dict) -> np.ndarray:
    rng = np.random.default_rng(cfg["seed"] + MOCK_SEED_OFFSET + 1)
    med = seg["highway"].map(cfg["mock"]["esal_median_by_highway"]).fillna(8).values
    # constant within an OSM way, plus a little per-segment noise
    way_eff = seg.groupby("osm_way_id")["osm_way_id"].transform(lambda s: rng.lognormal(0, 0.6)).values
    esal = med * way_eff * rng.lognormal(0, 0.15, len(seg)) * np.where(seg["bus_stop"], 1.5, 1.0)
    return np.round(esal, 3)


def mock_daily_weather(cfg: dict, wards: list[str]) -> pd.DataFrame:
    """Hourly temperature and rain per ward, summarised later into weekly features."""
    rng = np.random.default_rng(cfg["seed"] + MOCK_SEED_OFFSET + 2)
    start = pd.Timestamp(cfg["mock"]["weather_start"]) - pd.Timedelta(days=120)
    end = pd.Timestamp(cfg["as_of"]) + pd.Timedelta(days=7)
    hours = pd.date_range(start, end, freq="h", inclusive="left")
    doy = hours.dayofyear.values
    hod = hours.hour.values
    nd = len(hours) // 24
    # city-wide daily anomaly (AR1) so cold snaps last several days
    anom = np.zeros(nd)
    eps = rng.normal(0, 1.1, nd)
    for i in range(1, nd):
        anom[i] = 0.8 * anom[i - 1] + eps[i]
    season = 11.2 - 5.8 * np.cos(2 * np.pi * (doy - 20) / 365.25)
    diurnal = 3.5 * np.sin(2 * np.pi * (hod - 9) / 24)
    t_city = season + diurnal + np.repeat(anom, 24)[: len(hours)]
    ddoy = doy[::24][:nd]
    wet_p = 0.48 + 0.14 * np.cos(2 * np.pi * (ddoy - 15) / 365.25)
    wet = rng.random(nd) < wet_p
    amount = np.where(wet, rng.gamma(0.8, 6.0, nd), 0.0)
    out = []
    for i, w in enumerate(wards):
        r = np.random.default_rng(cfg["seed"] + 100 + i)
        t = t_city + r.normal(0, 0.4) + r.normal(0, 0.3, len(hours))
        mult = r.uniform(0.9, 1.1)
        rain_h = np.repeat(amount * mult / 24.0, 24)[: len(hours)]
        out.append(pd.DataFrame({"ward": w, "time": hours, "temp_c": t, "rain_mm": rain_h}))
    return pd.concat(out, ignore_index=True)


def weekly_features(hourly: pd.DataFrame) -> pd.DataFrame:
    """Weekly features per ward using only hours strictly before each week start."""
    rows = []
    for ward, g in hourly.groupby("ward", sort=True):
        g = g.sort_values("time")
        t = g["temp_c"].values
        r = g["rain_mm"].values
        time = g["time"].values
        below = (t < 0).astype(float)
        thaw = np.r_[0, ((t[:-1] < 0) & (t[1:] >= 0)).astype(float)]
        rain48 = pd.Series(r).rolling(48, min_periods=1).sum().values
        rtf = ((t < 0) & (np.r_[0, rain48[:-1]] >= 5)).astype(float)
        daily = pd.DataFrame({"temp_c": t, "rtf": rtf}, index=g["time"]).resample("D").max()
        hot = (daily["temp_c"] > 25).astype(float)
        c_rain, c_below, c_thaw = (np.r_[0, np.cumsum(a)] for a in (r, below, thaw))
        c_hot = np.r_[0, np.cumsum(hot.values)]
        c_rtf = np.r_[0, np.cumsum(daily["rtf"].values)]
        dtimes = daily.index.values
        mondays = pd.date_range(pd.Timestamp(time[0]).normalize() + pd.Timedelta(days=100), pd.Timestamp(time[-1]), freq="W-MON")
        idx = np.searchsorted(time, mondays.values)          # first hour at/after week start
        didx = np.searchsorted(dtimes, mondays.values)

        def window(c, hrs):
            return c[idx] - c[np.maximum(idx - hrs, 0)]

        rows.append(pd.DataFrame({
            "ward": ward, "week_start": mondays,
            "rain_3d": window(c_rain, 72), "rain_7d": window(c_rain, 168),
            "rain_30d": window(c_rain, 720), "rain_90d": window(c_rain, 2160),
            "hours_below_0_7d": window(c_below, 168),
            "freeze_thaw_14d": window(c_thaw, 336), "freeze_thaw_30d": window(c_thaw, 720),
            "rain_then_frost_7d": c_rtf[didx] - c_rtf[np.maximum(didx - 7, 0)],   # days with a rain-then-frost hour
            "hot_days_7d": c_hot[didx] - c_hot[np.maximum(didx - 7, 0)],
        }))
    out = pd.concat(rows, ignore_index=True)
    out["input_source"] = "mock"
    return out


def mock_weekly_weather(cfg: dict, wards: list[str]) -> pd.DataFrame:
    wx = weekly_features(mock_daily_weather(cfg, wards))
    return wx[wx["week_start"] >= pd.Timestamp(cfg["mock"]["weather_start"])].reset_index(drop=True)
