"""Segment-week table for the risk model (M5).

Row = (segment, week starting Monday). Every feature uses only data dated strictly before
the row's week_start; labels look forward (y7: a recorded pothole on the segment in the next
7 days, y30: next 30 days). Recorded = reported_date in the council log.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import sparse

from roadsense.synthetic.common import neighbours, road_class, weeks

WEATHER_FEATS = ["rain_3d", "rain_7d", "rain_30d", "rain_90d", "hours_below_0_7d", "freeze_thaw_14d",
                 "freeze_thaw_30d", "rain_then_frost_7d", "hot_days_7d"]
STATIC_FEATS = ["log_esal_day", "bus_stop", "junction", "in_flood_area", "length_m", "road_class_code"]
HISTORY_FEATS = ["days_since_resurfacing", "treatments_2y", "potholes_seg_12m", "potholes_nb_12m",
                 "potholes_seg_8w", "potholes_nb_8w"]
FEATURES = STATIC_FEATS + WEATHER_FEATS + HISTORY_FEATS
ROAD_CLASS_CODES = {"A": 2, "BC": 1, "residential": 0}

# +1: more of this never lowers risk; -1: recent preventive treatment never raises it
MONOTONE = {"log_esal_day": 1, "rain_3d": 1, "rain_7d": 1, "rain_30d": 1, "rain_90d": 1,
            "freeze_thaw_14d": 1, "freeze_thaw_30d": 1, "rain_then_frost_7d": 1, "treatments_2y": -1}


def _daily_counts(events: pd.DataFrame, date_col: str, seg_pos: dict, d0: pd.Timestamp, nd: int, n: int) -> np.ndarray:
    """Cumulative recorded counts: cum[k, s] = events on segment s dated before day d0+k."""
    d = (pd.to_datetime(events[date_col]) - d0).dt.days.values
    s = events["segment_id"].map(seg_pos).values
    ok = ~pd.isna(s) & (d >= 0) & (d < nd)
    m = sparse.coo_matrix((np.ones(ok.sum(), dtype=np.int32), (d[ok], s[ok].astype(int))), shape=(nd, n)).toarray()
    return np.vstack([np.zeros((1, n), dtype=np.int32), np.cumsum(m, axis=0, dtype=np.int32)])


def _days_since_resurfacing(res: pd.DataFrame, seg_ids, W: pd.DatetimeIndex) -> np.ndarray:
    """Days since the most recent recorded full resurfacing before week start (NaN if unknown)."""
    pos = {s: i for i, s in enumerate(seg_ids)}
    out = np.full((len(W), len(seg_ids)), np.nan)
    full = res[res["treatment"].isin(["last_resurfaced", "full_resurfacing"])]
    for sid, d in sorted(zip(full["segment_id"], pd.to_datetime(full["date"])), key=lambda t: t[1]):
        j = pos.get(sid)
        if j is None:
            continue
        since = (W - d).days.values.astype(float)
        m = since > 0
        out[m, j] = since[m]              # later events overwrite earlier ones
    return out


def _treatments_2y(res: pd.DataFrame, seg_ids, W: pd.DatetimeIndex) -> np.ndarray:
    pos = {s: i for i, s in enumerate(seg_ids)}
    out = np.zeros((len(W), len(seg_ids)), dtype=np.float32)
    prev = res[res["treatment"].isin(["crack_sealing", "surface_dressing"])]
    for sid, d in zip(prev["segment_id"], pd.to_datetime(prev["date"])):
        j = pos.get(sid)
        if j is not None:
            since = (W - d).days.values
            out[(since > 0) & (since <= 730), j] += 1
    return out


def build_table(seg: pd.DataFrame, wx: pd.DataFrame, res: pd.DataFrame, potholes: pd.DataFrame, cfg: dict,
                start: str | None = None, end: str | None = None, with_labels: bool = True) -> pd.DataFrame:
    """Segment-week rows for week starts in [start, end)."""
    rcfg = cfg["risk"]
    start = start or str((pd.Timestamp(cfg["potholes"]["start"]) + pd.Timedelta(days=365)).date())
    end = end or cfg["as_of"]
    W = weeks(start, end)
    n = len(seg)
    seg_ids = seg["segment_id"].values
    pos = {s: i for i, s in enumerate(seg_ids)}
    d0 = W[0] - pd.Timedelta(days=400)
    nd = (W[-1] - d0).days + 40
    cum = _daily_counts(potholes, "reported_date", pos, d0, nd, n)
    k = (W - d0).days.values                                   # day index of each week start

    nb = neighbours(seg, cfg["potholes"]["neighbour_radius_m"])
    rows = np.repeat(np.arange(n), [len(x) for x in nb])
    A = sparse.csr_matrix((np.ones(len(rows)), (rows, np.concatenate(nb) if len(rows) else rows)), shape=(n, n))

    def window(days):
        return (cum[k] - cum[np.maximum(k - days, 0)]).astype(np.float32)

    seg12, seg8 = window(365), window(56)
    nb12 = np.asarray((A @ seg12.T).T, dtype=np.float32)
    nb8 = np.asarray((A @ seg8.T).T, dtype=np.float32)
    feats = {
        "days_since_resurfacing": _days_since_resurfacing(res, seg_ids, W),
        "treatments_2y": _treatments_2y(res, seg_ids, W),
        "potholes_seg_12m": seg12, "potholes_nb_12m": nb12, "potholes_seg_8w": seg8, "potholes_nb_8w": nb8,
    }
    if with_labels:
        feats["y7"] = ((cum[k + 7] - cum[k]) > 0).astype(np.int8)
        feats["y30"] = ((cum[k + 30] - cum[k]) > 0).astype(np.int8)
    T = len(W)
    df = pd.DataFrame({"segment_id": np.tile(seg_ids, T), "week_start": np.repeat(W.values, n)})
    for name, mat in feats.items():
        df[name] = mat.reshape(-1)
    static = pd.DataFrame({
        "segment_id": seg_ids, "ward": seg["ward"].values,
        "log_esal_day": np.log1p(seg["esal_day"].values.astype(float)),
        "bus_stop": seg["bus_stop"].astype(float).values, "junction": seg["junction"].astype(float).values,
        "in_flood_area": seg["in_flood_area"].astype(float).values, "length_m": seg["length_m"].astype(float).values,
        "road_class_code": road_class(seg["highway"], cfg).map(ROAD_CLASS_CODES).values.astype(float),
    })
    df = df.merge(static, on="segment_id", how="left")
    w = wx[["ward", "week_start"] + WEATHER_FEATS].copy()
    w["week_start"] = pd.to_datetime(w["week_start"])
    df = df.merge(w, on=["ward", "week_start"], how="left")
    if "city" in seg:
        df["city"] = df["segment_id"].map(dict(zip(seg_ids, seg["city"])))
    return df
