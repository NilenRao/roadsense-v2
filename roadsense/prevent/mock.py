"""Small deterministic mock world in the shapes of schemas (lane A segments, lane B risk/lights).

Only used when neither the real files nor lane B's mocks are on disk, and in tests. Every row
carries input_source='mock' / inputs_mock=True so the page shows MOCK, never real.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from roadsense.prevent.inputs import M_PER_DEG_LAT, M_PER_DEG_LON

HIGHWAYS = ["residential", "residential", "residential", "tertiary", "secondary", "primary", "trunk", "motorway"]


def mock_world(cfg: dict | None = None, n_ways: int = 12, per_way: int = 10, seed: int = 2026,
               as_of: str = "2026-10-12") -> dict:
    rng = np.random.default_rng(seed)
    lon0, lat0 = -1.44, 50.90
    L = 75.0
    rows = []
    for w in range(n_ways):
        hw = HIGHWAYS[w % len(HIGHWAYS)]
        lat = lat0 + (w * 240) / M_PER_DEG_LAT
        for k in range(per_way):
            a = lon0 + (k * L) / M_PER_DEG_LON
            b = lon0 + ((k + 1) * L) / M_PER_DEG_LON
            esal = {"motorway": 4000, "trunk": 2500, "primary": 900, "secondary": 300, "tertiary": 90}.get(hw, 8)
            rows.append({
                "segment_id": f"X{w:03d}-{k:03d}", "osm_way_id": w + 1, "name": f"Mock Road {w + 1}",
                "highway": hw, "ward": f"Mock ward {w % 3 + 1:02d}", "length_m": L,
                "geometry": f"LINESTRING ({a:.6f} {lat:.6f}, {b:.6f} {lat:.6f})",
                "lat": lat, "lon": (a + b) / 2, "bus_stop": bool(rng.random() < 0.15),
                "junction": k in (0, per_way - 1), "in_flood_area": bool(rng.random() < 0.1),
                "owner": "national_highways" if hw == "motorway" else "council",
                "esal_day": float(esal * rng.lognormal(0, 0.4)),
                "load_source": "count" if hw in ("primary", "trunk") and k == 0 else "class_median",
                "input_source": "mock",
            })
    seg = pd.DataFrame(rows)
    sc = seg[seg["owner"] == "council"].reset_index(drop=True)
    n = len(sc)
    base = 0.002 + 0.03 * np.log1p(sc["esal_day"].values) / np.log1p(2500) + rng.uniform(0, 0.02, n)
    rise = rng.normal(0.004, 0.006, n)
    p30 = {f"p30_w{k}": np.clip(base + rise * k / 3, 0.0005, 0.9) for k in range(4)}
    wear = np.clip(rng.uniform(5, 98, n), 0, 100).round(1)
    risk = pd.DataFrame({
        "segment_id": sc["segment_id"], "ward": sc["ward"], "week_start": pd.Timestamp(as_of),
        "p7": base * 0.3, "p30": p30["p30_w0"], **p30,
        "p30_trend": p30["p30_w3"] - p30["p30_w0"], "wear_pct": wear,
        "reason_1": [f"heavy axle load ({e:,.0f} ESAL/day)" for e in sc["esal_day"]],
        "reason_2": "12 mm rain in the last 7 days", "reason_3": None,
        "weather_source": "w0:observed,w1:climatology_10y,w2:climatology_10y,w3:climatology_10y",
        "labels_synthetic": True, "wear_synthetic": True, "inputs_mock": True,
        "model_version": "mock", "as_of": as_of,
    })
    lamps = []
    for i, r in sc.iterrows():
        for j in range(2):
            p = float(rng.beta(1, 30))
            lamps.append({
                "lamp_id": f"XL{i:04d}{j}", "segment_id": r["segment_id"], "lat": r["lat"],
                "lon": r["lon"] + (j - 0.5) * 30 / M_PER_DEG_LON, "unit_type": "LED",
                "unit_install_date": "2013-06-01", "burn_hours": 45000.0, "p_fail_90d": p,
                "predicted_replacement_date": "2028-01-01", "replacement_overdue": False,
                "due_90d": p > 0.08, "currently_dark": bool(rng.random() < 0.02), "dark_since": None,
                "is_synthetic": True, "source": "mock:lane_c", "as_of": as_of,
            })
    return {"segments": seg, "risk": risk, "lights": pd.DataFrame(lamps)}


MOCK_REASONS = {
    "load": lambda r: f"heavy axle load ({r['esal_day']:,.0f} ESAL/day)",
    "bus": lambda r: f"{int(r['bus_routes'])} bus routes" if r["bus_routes"] else "bus stop on this segment",
    "flood": lambda r: "inside an Environment Agency flood area",
    "junction": lambda r: "junction (turning and braking loads)",
    "age": lambda r: f"{r['age']:.0f} years since resurfacing (synthetic)",
}


def mock_risk(seg: pd.DataFrame, cfg: dict | None = None, as_of: str = "2026-10-12", seed: int = 2026) -> pd.DataFrame:
    """MOCK risk on real segments, in the contract's section 7 columns (one row per segment per week).

    Only so the page can run on lane A's real map before lane B re-runs on it. Scores rise with real
    load, bus routes, flood areas and junctions plus noise; they are not model output.
    """
    rng = np.random.default_rng(seed)
    s = seg[(seg["owner"] != "national_highways") & (seg["highway"] != "motorway")].reset_index(drop=True)
    n = len(s)
    esal = s["esal_day"].fillna(s["esal_day"].median() if s["esal_day"].notna().any() else 10).astype(float)
    bus = s.get("bus_routes", pd.Series(0, index=s.index)).fillna(0).astype(float)
    z = (0.6 * np.log1p(esal) + 0.25 * np.minimum(bus, 6) + 0.4 * s["in_flood_area"].astype(float)
         + 0.3 * s["junction"].astype(float) + rng.normal(0, 0.6, n))
    p30 = np.clip(0.002 * np.exp(z), 0.0005, 0.6)
    age = rng.uniform(0, 30, n)
    wear = np.clip(100 * (1 - np.exp(-(age / 22) ** 2.2)), 0, 100).round(1)
    trend = rng.normal(0.15, 0.2, n)
    rows = s.assign(esal_day=esal, bus_routes=bus, age=age)
    reasons = []
    for r in rows.to_dict("records"):
        keys = sorted([("load", np.log1p(r["esal_day"])), ("bus", r["bus_routes"] + 3 * r["bus_stop"]),
                       ("flood", 4 * r["in_flood_area"]), ("junction", 2 * r["junction"]), ("age", r["age"] / 6)],
                      key=lambda t: -t[1])
        reasons.append([MOCK_REASONS[k](r) for k, v in keys[:3] if v > 0])
    out = []
    for k in range(4):
        week = (pd.Timestamp(as_of) + pd.Timedelta(weeks=k)).date().isoformat()
        pk = np.clip(p30 * (1 + trend * k / 3), 0.0002, 0.9)
        out.append(pd.DataFrame({
            "segment_id": s["segment_id"], "ward": s["ward"], "week_start": week, "horizon_week": k,
            "p7": (pk * 0.3).round(5), "p30": pk.round(5), "wear": wear,
            "reason_1": [r[0] if len(r) > 0 else None for r in reasons],
            "reason_2": [r[1] if len(r) > 1 else None for r in reasons],
            "reason_3": [r[2] if len(r) > 2 else None for r in reasons],
            "weather_source": "observed" if k == 0 else "climatology_10y", "uses_forecast": k > 0,
            "weather_data_date": f"{as_of}T00:00", "model_version": "mock@lane_c", "trained_on": "mock",
            "uses_synthetic": True, "inputs_mock": True, "as_of": as_of}))
    return pd.concat(out, ignore_index=True)


def mock_lights(seg: pd.DataFrame, as_of: str = "2026-10-12", seed: int = 2026, every: int = 4) -> pd.DataFrame:
    """MOCK lamps on real segments (one at every `every`-th segment midpoint), contract section 8 columns."""
    rng = np.random.default_rng(seed + 1)
    s = seg[(seg["owner"] != "national_highways") & (seg["highway"] != "motorway")].iloc[::every].reset_index(drop=True)
    n = len(s)
    legacy = rng.random(n) < 0.1
    p = np.clip(rng.beta(1, 25, n) + 0.08 * legacy, 0, 1)
    replace_by = pd.Timestamp(as_of) + pd.to_timedelta((rng.uniform(-60, 3000, n) * (1 - p)).astype(int), unit="D")
    dark = rng.random(n) < 0.01
    return pd.DataFrame({
        "lamp_id": [f"ML{i:05d}" for i in range(n)], "segment_id": s["segment_id"], "lat": s["lat"], "lon": s["lon"],
        "lamp_type": np.where(legacy, "legacy", "LED"), "p_fail_90d": p.round(4),
        "replace_by": replace_by.strftime("%Y-%m-%d"), "replacement_overdue": replace_by < pd.Timestamp(as_of),
        "due_90d": replace_by <= pd.Timestamp(as_of) + pd.Timedelta(days=90), "currently_dark": dark,
        "dark_source": np.where(dark, "synthetic", None), "dark_since": None, "uses_synthetic": True,
        "is_synthetic": True, "source": "mock:lane_c", "as_of": as_of})
