"""Generator 5: crew inspection outcomes for the feedback loop (SYNTHETIC).

200 segments from the model's top predictions (outputs/risk_*.parquet when it exists,
otherwise a public proxy: recorded potholes in the last 12 months, then esal_day) and 100
random segments. Outcomes are sampled from the hidden hazard, so they carry signal the
model has not seen.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.synthetic.common import label_synthetic

GEN = "crew"


def _top_segments(seg: pd.DataFrame, potholes: pd.DataFrame, cfg: dict, outputs_dir: Path, n: int):
    risk_files = [p for p in [outputs_dir / "risk_latest.parquet", outputs_dir / "risk_synthetic_latest.parquet"] if p.exists()]
    risk_files = risk_files[:1] or sorted(outputs_dir.glob("risk_*.parquet"))
    if risk_files:
        r = pd.read_parquet(risk_files[-1])
        r = r[r["segment_id"].isin(seg["segment_id"])]
        if "horizon_week" in r:
            r = r[r["horizon_week"] == r["horizon_week"].min()]
        return r.sort_values("p30", ascending=False)["segment_id"].head(n).tolist(), f"model_top:{risk_files[-1].name}"
    as_of = pd.Timestamp(cfg["as_of"])
    recent = potholes[pd.to_datetime(potholes["reported_date"]) >= as_of - pd.Timedelta(days=365)]
    cnt = recent.groupby("segment_id").size()
    s = seg[["segment_id", "esal_day"]].copy()
    s["recent"] = s["segment_id"].map(cnt).fillna(0)
    return s.sort_values(["recent", "esal_day"], ascending=False)["segment_id"].head(n).tolist(), "proxy_top:recent_potholes"


def generate(seg: pd.DataFrame, potholes: pd.DataFrame, truth: dict, cfg: dict, rng: np.random.Generator,
             outputs_dir: Path) -> pd.DataFrame:
    cc = cfg["crew"]
    top, how = _top_segments(seg, potholes, cfg, outputs_dir, cc["n_top"])
    rest = np.setdiff1d(seg["segment_id"].values, top)
    rand = rng.choice(rest, size=cc["n_random"], replace=False).tolist()
    ids = top + rand
    h = np.array([truth["hazard_last_week"][s] for s in ids])
    hw = cc["horizon_weeks"]
    p_pot = -np.expm1(-h * hw)
    p_crack = -np.expm1(-h * hw * cc["cracking_scale"])
    u = rng.random(len(ids))
    outcome = np.where(u < p_pot, "pothole", np.where(u < p_pot + (1 - p_pot) * p_crack, "cracking", "none"))
    as_of = pd.Timestamp(cfg["as_of"])
    out = pd.DataFrame({
        "outcome_id": [f"CO{i:04d}" for i in range(1, len(ids) + 1)],
        "segment_id": ids,
        "inspection_date": (as_of + pd.to_timedelta(rng.integers(0, 14, len(ids)), unit="D")).date,
        "selection": [how] * len(top) + ["random"] * len(rand),
        "outcome": outcome,
    })
    return label_synthetic(out, GEN, cfg)
