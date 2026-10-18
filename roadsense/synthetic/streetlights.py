"""Generators 3 and 4: streetlight register and fault history (SYNTHETIC).

Burn hours per night run from sunset to the next sunrise (astral, Southampton), minus the
council's 01:00-05:30 part-night switch-off from 1 December 2023 onwards.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from roadsense.synthetic.common import label_synthetic, to_lonlat, to_xy

GEN_LAMPS = "streetlights"
GEN_FAULTS = "light_faults"
TZ = ZoneInfo("Europe/London")


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def night_burn_hours(d: date, sunset: datetime, next_sunrise: datetime, switch_off: dict | None) -> float:
    """Hours the lamp is lit on the night starting on date d."""
    hours = (next_sunrise - sunset).total_seconds() / 3600.0
    if switch_off and d + timedelta(days=1) >= date.fromisoformat(switch_off["from"]):
        nxt = d + timedelta(days=1)
        off0 = datetime.combine(nxt, _hm(switch_off["start"]), TZ)
        off1 = datetime.combine(nxt, _hm(switch_off["end"]), TZ)
        overlap = (min(off1, next_sunrise) - max(off0, sunset)).total_seconds() / 3600.0
        hours -= max(0.0, overlap)
    return max(0.0, hours)


@lru_cache(maxsize=4)
def _burn_series(start: str, end: str, lat: float, lon: float, so_key: tuple | None) -> pd.Series:
    from astral import LocationInfo
    from astral.sun import sun
    loc = LocationInfo("Southampton", "England", "Europe/London", lat, lon)
    so = dict(so_key) if so_key else None
    days = pd.date_range(start, end, freq="D")
    vals = np.empty(len(days))
    prev_sun = None
    for i, ts in enumerate(days):
        d = ts.date()
        s0 = sun(loc.observer, date=d, tzinfo=TZ) if prev_sun is None else prev_sun
        s1 = sun(loc.observer, date=d + timedelta(days=1), tzinfo=TZ)
        vals[i] = night_burn_hours(d, s0["sunset"], s1["sunrise"], so)
        prev_sun = s1
    return pd.Series(vals, index=days)


def burn_hours_series(cfg: dict, start: str, end: str) -> pd.Series:
    sl = cfg["streetlights"]
    so = sl.get("switch_off")
    return _burn_series(start, end, sl["lat"], sl["lon"], tuple(sorted(so.items())) if so else None).copy()


def _lamp_positions(seg: pd.DataFrame, spacing: float, rng) -> pd.DataFrame:
    from shapely import wkb, wkt
    rows = []
    for sid, g, length in zip(seg["segment_id"], seg["geometry"], seg["length_m"]):
        geom = wkb.loads(g) if isinstance(g, (bytes, bytearray)) else wkt.loads(g)
        offset = rng.uniform(0, spacing)
        k = 0
        while offset < length:
            pt = geom.interpolate(offset / length, normalized=True)
            rows.append((sid, k, pt.x, pt.y))
            offset += spacing
            k += 1
    return pd.DataFrame(rows, columns=["segment_id", "k", "lon", "lat"])


def generate_lamps(seg: pd.DataFrame, cfg: dict, rng: np.random.Generator, osm_lamps: pd.DataFrame | None = None) -> pd.DataFrame:
    """Generator 3. Real OSM lamp positions if there are enough in coverage, else one every 35 m."""
    sl = cfg["streetlights"]
    council = seg[(seg["owner"] != "national_highways") & (seg["highway"] != "motorway")]
    if osm_lamps is not None and len(osm_lamps) >= sl["osm_min_count"]:
        from scipy.spatial import cKDTree
        pos = osm_lamps[["lon", "lat"]].copy()
        tree = cKDTree(np.c_[to_xy(council["lon"].values, council["lat"].values)])
        _, j = tree.query(np.c_[to_xy(pos["lon"].values, pos["lat"].values)])
        pos["segment_id"] = council["segment_id"].values[j]
        position_source = "osm:highway=street_lamp"
    else:
        pos = _lamp_positions(council, sl["spacing_m"], rng)
        position_source = f"synthetic: one lamp every {sl['spacing_m']} m"
    n = len(pos)
    led = rng.random(n) < sl["led_share"]
    y0 = np.where(led, sl["led_install_years"][0], sl["legacy_install_years"][0])
    y1 = np.where(led, sl["led_install_years"][1], sl["legacy_install_years"][1])
    start = pd.to_datetime(pd.Series(y0.astype(str))).values
    span = (pd.to_datetime(pd.Series((y1 + 1).astype(str))).values - start) / np.timedelta64(1, "D")
    install = pd.DatetimeIndex(start + pd.to_timedelta((rng.random(n) * span).astype(int), unit="D"))
    out = pd.DataFrame({
        "lamp_id": [f"SL{i:06d}" for i in range(1, n + 1)],
        "segment_id": pos["segment_id"].values,
        "lat": np.round(pos["lat"].values, 6), "lon": np.round(pos["lon"].values, 6),
        "lamp_type": np.where(led, "LED", "legacy"),
        "install_date": install.date,
        "position_source": position_source,
    })
    return label_synthetic(out, GEN_LAMPS, cfg)


def generate_faults(lamps: pd.DataFrame, cfg: dict, rng: np.random.Generator):
    """Generator 4. Weibull burnout on burn hours plus random electrical/damage faults.

    Returns (faults, current_units): faults 2016 to as_of, and each lamp's unit in service at
    as_of (what an asset register records after replacements).
    """
    sl = cfg["streetlights"]
    as_of = pd.Timestamp(cfg["as_of"])
    first = pd.Timestamp(min(lamps["install_date"]))
    bh = burn_hours_series(cfg, str(first.date()), str(as_of.date()))
    cum = np.r_[0.0, np.cumsum(bh.values)]            # cum[k] = burn hours before day k
    day0 = bh.index[0]
    nd = len(bh)
    faults_from = (pd.Timestamp(sl["faults_start"]) - day0).days
    rate_day = sl["random_fault_rate_per_year"] / 365.25
    wb = sl["weibull"]
    rows, state = [], []
    for lamp_id, ltype, inst in zip(lamps["lamp_id"], lamps["lamp_type"], pd.to_datetime(lamps["install_date"])):
        k = (inst - day0).days
        unit_type = ltype
        unit_start = k
        life = wb[unit_type]["scale_h"] * rng.weibull(wb[unit_type]["shape"])
        while k < nd:
            # next random fault (calendar time) vs burnout (burn-hours time)
            k_rand = k + int(rng.exponential(1 / rate_day))
            k_burn = int(np.searchsorted(cum, cum[unit_start] + life, side="left")) - 1
            k_fail = min(k_rand, k_burn)
            if k_fail >= nd:
                break
            kind = "burnout" if k_burn <= k_rand else rng.choice(["electrical", "damage"])
            rep = k_fail + int(rng.integers(sl["repair_days"][0], sl["repair_days"][1] + 1))
            if k_fail >= faults_from:
                rows.append((lamp_id, unit_type, k_fail, rep, kind, cum[k_fail] - cum[unit_start], unit_start))
            k = rep
            if kind == "burnout":                       # replaced with a new unit
                unit_type = sl["replacement_type"]
                unit_start = rep
                life = wb[unit_type]["scale_h"] * rng.weibull(wb[unit_type]["shape"])
        state.append((lamp_id, unit_type, unit_start))
    f = pd.DataFrame(rows, columns=["lamp_id", "unit_type", "k_fail", "k_rep", "fault_type", "burn_hours_at_fault", "k_unit"])
    f["fault_date"] = (day0 + pd.to_timedelta(f["k_fail"], unit="D")).dt.date
    rep = day0 + pd.to_timedelta(f["k_rep"], unit="D")
    f["repaired_date"] = rep.where(rep < as_of).dt.date
    f["status"] = np.where(rep < as_of, "repaired", "open")
    f["unit_install_date"] = (day0 + pd.to_timedelta(f["k_unit"], unit="D")).dt.date
    f["burn_hours_at_fault"] = f["burn_hours_at_fault"].round(1)
    f = f.sort_values(["fault_date", "lamp_id"], kind="stable").reset_index(drop=True)
    f.insert(0, "fault_id", [f"LF{i:06d}" for i in range(1, len(f) + 1)])
    f = f[["fault_id", "lamp_id", "fault_date", "repaired_date", "status", "fault_type", "unit_type",
           "unit_install_date", "burn_hours_at_fault"]]
    st = pd.DataFrame(state, columns=["lamp_id", "current_unit_type", "k"])
    st["current_unit_install_date"] = (day0 + pd.to_timedelta(st["k"], unit="D")).dt.date
    return label_synthetic(f, GEN_FAULTS, cfg), st.drop(columns="k")


def currently_dark(faults: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    as_of = pd.Timestamp(cfg["as_of"])
    win = as_of - pd.Timedelta(days=cfg["streetlights"]["dark_window_days"])
    fd = pd.to_datetime(faults["fault_date"])
    return faults[(faults["status"] == "open") & (fd >= win)]
