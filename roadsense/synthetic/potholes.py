"""Generator 2: pothole repair log (SYNTHETIC labels) from a hidden weekly hazard model.

hazard = exp(b0_year + 0.6 log1p(esal_day) + 0.5 z(rain_30d) + 0.4 freeze_thaw_14d
             + 0.8 rain_then_frost_7d + 0.05 age_years + 0.3 bus_stop + 0.3 junction
             + 0.4 in_flood_area + u_segment) * prevention * clustering
b0_year is solved by bisection so the expected *recorded* total per year matches
Southampton's FOI repair totals. True coefficients and u_segment go to _truth.json,
which no model or feature code may read (tests enforce this).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import sparse

from roadsense.synthetic.common import label_synthetic, neighbours, to_lonlat, to_xy, weeks
from roadsense.synthetic.resurfacing import age_years_at, prevention_active

GEN = "potholes"


def _weather_matrix(wx: pd.DataFrame, col: str, week_starts, ward_codes, wards) -> np.ndarray:
    piv = wx.pivot_table(index="week_start", columns="ward", values=col, aggfunc="mean")
    piv = piv.reindex(index=week_starts, columns=wards).ffill().bfill().fillna(0.0)
    return piv.values[:, ward_codes]


def yearly_targets(cfg: dict, week_starts: pd.DatetimeIndex) -> dict[int, float]:
    pc = cfg["potholes"]
    tot = {int(k): float(v) for k, v in pc["yearly_totals"].items()}
    rate = tot[int(pc["current_year_rate_from"])]
    for y in sorted(set(week_starts.year)):
        if y not in tot:
            tot[y] = rate * (week_starts.year == y).sum() / 52.0
    return tot


def generate(seg: pd.DataFrame, wx: pd.DataFrame, res: pd.DataFrame, cfg: dict, rng: np.random.Generator):
    pc = cfg["potholes"]
    c = pc["coef"]
    W = weeks(pc["start"], cfg["as_of"])
    n = len(seg)
    seg_ids = seg["segment_id"].values
    wards = sorted(seg["ward"].unique())
    ward_codes = pd.Categorical(seg["ward"], categories=wards).codes

    u = rng.normal(0, pc["u_sd"], n)
    report_prob = dict(zip(wards, rng.uniform(*pc["report_prob_range"], len(wards))))
    rp = np.array([report_prob[w] for w in seg["ward"]])

    rain = _weather_matrix(wx, "rain_30d", W, ward_codes, wards)
    rain_z = (rain - rain.mean()) / (rain.std() + 1e-9)
    caps = pc.get("weather_caps", {})
    ft = np.minimum(_weather_matrix(wx, "freeze_thaw_14d", W, ward_codes, wards), caps.get("freeze_thaw_14d", np.inf))
    rtf = np.minimum(_weather_matrix(wx, "rain_then_frost_7d", W, ward_codes, wards), caps.get("rain_then_frost_7d", np.inf))
    age = age_years_at(res, seg_ids, W, cfg)
    prev = prevention_active(res, seg_ids, W, cfg)

    static = (c["esal"] * np.log1p(seg["esal_day"].values) + c["bus_stop"] * seg["bus_stop"].astype(float).values
              + c["junction"] * seg["junction"].astype(float).values
              + c["in_flood_area"] * seg["in_flood_area"].astype(float).values + u)
    lin = static[None, :] + c["rain_30d_z"] * rain_z + c["freeze_thaw_14d"] * ft + c["rain_then_frost_7d"] * rtf + c["age_years"] * age
    mult = np.where(prev, pc["prevention_multiplier"], 1.0)

    nb = neighbours(seg, pc["neighbour_radius_m"])
    rows = np.repeat(np.arange(n), [len(x) for x in nb])
    A = sparse.csr_matrix((np.ones(len(rows) + n), (np.r_[rows, np.arange(n)], np.r_[np.concatenate(nb), np.arange(n)])), shape=(n, n))
    U = rng.random(lin.shape)

    targets = yearly_targets(cfg, W)
    b0_by_year: dict[int, float] = {}
    cluster_until = np.full(n, -1)
    events_w, events_s = [], []
    hazard_last = None

    def run_year(b0, idx, cl0, record):
        cl = cl0.copy()
        expected = 0.0
        evw, evs, h = [], [], None
        for i in idx:
            h = np.exp(b0 + lin[i]) * mult[i] * np.where(cl >= i, pc["cluster_multiplier"], 1.0)
            p = -np.expm1(-h)
            expected += float((p * rp).sum())
            ev = U[i] < p
            if ev.any():
                hit = (A @ ev.astype(float)) > 0
                cl[hit] = i + pc["cluster_weeks"]
                if record:
                    js = np.flatnonzero(ev)
                    evw.append(np.full(len(js), i))
                    evs.append(js)
        return expected, cl, evw, evs, h

    for y in sorted(set(W.year)):
        idx = np.flatnonzero(W.year == y)
        lo, hi = -20.0, 2.0
        for _ in range(pc["bisection_iters"]):
            mid = (lo + hi) / 2
            e, *_ = run_year(mid, idx, cluster_until, False)
            lo, hi = (mid, hi) if e < targets[y] else (lo, mid)
        b0 = (lo + hi) / 2
        b0_by_year[y] = b0
        _, cluster_until, evw, evs, hazard_last = run_year(b0, idx, cluster_until, True)
        events_w += evw
        events_s += evs

    ew = np.concatenate(events_w) if events_w else np.array([], int)
    es = np.concatenate(events_s) if events_s else np.array([], int)
    event_date = W[ew] + pd.to_timedelta(rng.integers(0, 7, len(ew)), unit="D")
    recorded = rng.random(len(ew)) < rp[es]
    delay = rng.integers(pc["report_delay_days"][0], pc["report_delay_days"][1] + 1, len(ew))
    reported = event_date + pd.to_timedelta(delay, unit="D")
    as_of = pd.Timestamp(cfg["as_of"])
    keep = recorded & (reported < as_of)
    ew, es, reported = ew[keep], es[keep], reported[keep]
    m = len(es)
    planned = rng.random(m) < pc["planned_share"]
    rlo, rhi = np.where(planned, pc["repair_days_planned"][0], pc["repair_days_reactive"][0]), np.where(planned, pc["repair_days_planned"][1], pc["repair_days_reactive"][1])
    repaired = reported + pd.to_timedelta(rng.integers(rlo, rhi + 1), unit="D")
    repaired = pd.Series(repaired).where(repaired < as_of)
    x, y_ = to_xy(seg["lon"].values[es], seg["lat"].values[es])
    r = pc["jitter_m"] * np.sqrt(rng.random(m))
    th = rng.uniform(0, 2 * np.pi, m)
    lon, lat = to_lonlat(x + r * np.cos(th), y_ + r * np.sin(th))
    out = pd.DataFrame({
        "segment_id": seg_ids[es], "lat": np.round(lat, 6), "lon": np.round(lon, 6),
        "reported_date": pd.DatetimeIndex(reported).date, "repaired_date": pd.to_datetime(repaired).dt.date.values,
        "depth_mm": np.round(rng.lognormal(np.log(pc["depth_mm_median"]), pc["depth_sigma"], m)).astype(int),
        "width_mm": np.round(rng.lognormal(np.log(pc["width_mm_median"]), pc["width_sigma"], m)).astype(int),
        "repair_type": np.where(planned, "planned", "reactive"),
    }).sort_values(["reported_date", "segment_id"], kind="stable").reset_index(drop=True)
    out.insert(0, "event_id", [f"SP{i:06d}" for i in range(1, len(out) + 1)])
    out = label_synthetic(out, GEN, cfg)

    truth = {
        "generator": f"{GEN}@{cfg['version']}", "seed": cfg["seed"],
        "coefficients": c, "u_sd": pc["u_sd"], "b0_by_year": b0_by_year,
        "yearly_targets_recorded": targets,
        "report_prob_by_ward": report_prob,
        "true_events_total": int(len(keep)),
        "u_segment": dict(zip(seg_ids.tolist(), np.round(u, 5).tolist())),
        "hazard_last_week": dict(zip(seg_ids.tolist(), np.round(hazard_last, 7).tolist())),
        "last_week_start": str(W[-1].date()),
    }
    return out, truth
