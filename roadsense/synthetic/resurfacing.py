"""Generator 1: resurfacing and surface-treatment history (SYNTHETIC)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from roadsense.synthetic.common import label_synthetic, road_class

GEN = "resurfacing"


def generate(seg: pd.DataFrame, cfg: dict, rng: np.random.Generator) -> pd.DataFrame:
    """One last_resurfaced row per scored segment plus ~n_events treatment events.

    Full resurfacing events move last_resurfaced forward so the two never disagree.
    """
    rc = cfg["resurfacing"]
    as_of = pd.Timestamp(cfg["as_of"])
    cls = road_class(seg["highway"], cfg)
    max_age = cls.map(rc["age_max_years"]).values.astype(float)
    age = rng.uniform(0, max_age)
    last = as_of - pd.to_timedelta(np.round(age * 365.25), unit="D")

    start = pd.Timestamp(rc["events_start"])
    span_days = (as_of - start).days
    # older and busier segments are more likely to be treated
    w = (age + 1) * np.log1p(seg["esal_day"].values + 1)
    w = w / w.sum()
    n = rc["n_events"]
    idx = rng.choice(len(seg), size=n, replace=True, p=w)
    kinds = list(rc["treatment_mix"])
    kind = rng.choice(kinds, size=n, p=list(rc["treatment_mix"].values()))
    dates = start + pd.to_timedelta(rng.integers(0, span_days, n), unit="D")
    events = pd.DataFrame({"segment_id": seg["segment_id"].values[idx], "date": dates, "treatment": kind})

    last_s = pd.Series(last, index=seg["segment_id"].values)
    full = events[events["treatment"] == "full_resurfacing"].groupby("segment_id")["date"].max()
    last_s.loc[full.index] = np.maximum(last_s.loc[full.index].values, full.values)
    base = pd.DataFrame({"segment_id": last_s.index, "date": last_s.values, "treatment": "last_resurfaced"})
    base["road_class"] = cls.values
    events["road_class"] = cls.values[idx]
    out = pd.concat([base, events.sort_values(["date", "segment_id"])], ignore_index=True)
    out["date"] = pd.to_datetime(out["date"]).dt.date
    return label_synthetic(out, GEN, cfg)


def age_years_at(res: pd.DataFrame, seg_ids: np.ndarray, week_starts: pd.DatetimeIndex, cfg: dict) -> np.ndarray:
    """Age since last full resurfacing at each week start, shape (n_weeks, n_segments).

    Before the recorded last_resurfaced date the segment is assumed to be on its previous
    cycle, i.e. age = class maximum + (t - last_resurfaced) (an assumption, logged in DATA_GAPS).
    """
    rc = cfg["resurfacing"]
    pos = {s: i for i, s in enumerate(seg_ids)}
    base = res[res["treatment"] == "last_resurfaced"].set_index("segment_id")
    last = pd.to_datetime(base.loc[seg_ids, "date"]).values
    maxage = base.loc[seg_ids, "road_class"].map(rc["age_max_years"]).values.astype(float)
    t = week_starts.values[:, None]
    yrs = (t - last[None, :]) / np.timedelta64(1, "D") / 365.25
    age = np.where(yrs >= 0, yrs, maxage[None, :] + yrs)
    # earlier full resurfacing events (before last_resurfaced) reset age too
    full = res[res["treatment"] == "full_resurfacing"]
    for sid, d in zip(full["segment_id"], pd.to_datetime(full["date"])):
        j = pos.get(sid)
        if j is None:
            continue
        since = (week_starts - d).days.values / 365.25
        mask = (since >= 0) & (since < age[:, j])
        age[mask, j] = since[mask]
    return np.clip(age, 0, None)


def prevention_active(res: pd.DataFrame, seg_ids: np.ndarray, week_starts: pd.DatetimeIndex, cfg: dict) -> np.ndarray:
    """Boolean (n_weeks, n_segments): a crack seal or surface dressing in the previous N years."""
    years = cfg["potholes"]["prevention_years"]
    pos = {s: i for i, s in enumerate(seg_ids)}
    out = np.zeros((len(week_starts), len(seg_ids)), dtype=bool)
    prev = res[res["treatment"].isin(["crack_sealing", "surface_dressing"])]
    for sid, d in zip(prev["segment_id"], pd.to_datetime(prev["date"])):
        j = pos.get(sid)
        if j is None:
            continue
        since = (week_starts - d).days.values
        out[(since > 0) & (since <= years * 365.25), j] = True
    return out
