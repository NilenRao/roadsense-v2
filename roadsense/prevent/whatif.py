"""What-if scenarios (M6): perturb the forecast weeks and re-score.

    python -m roadsense.prevent.whatif --scenario rain_then_frost   (also wet_week, cold_snap)

Re-scoring uses lane B's synthetic-trained weekly model when it is importable (the forward feature rows
are rebuilt, the scenario deltas are added to forecast weeks, p30 is predicted again, and the hazard
ratio scales the plan's risk, which is the real-label ranking since 17 Oct). Otherwise a
sensitivity surrogate is used: monthly hazard is scaled by exp(sum of coef x delta) times a load
factor, with the spec's published generator coefficients. The method used is returned and shown.
"""
from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.prevent import inputs as I

P30_WEEKS = ["p30_w0", "p30_w1", "p30_w2", "p30_w3"]


def load_factor(esal_day, cfg: dict) -> np.ndarray:
    w = cfg["whatif"]
    e = np.log1p(np.nan_to_num(np.asarray(esal_day, float)))
    ref = np.log1p(w["esal_ref"])
    return np.clip(1 + w["load_gamma"] * (e - ref) / ref, 0.25, 2.0)


def surrogate(risk: pd.DataFrame, seg: pd.DataFrame, scenario: str, cfg: dict) -> pd.DataFrame:
    w = cfg["whatif"]
    d = w["scenarios"][scenario]
    c = w["coef"]
    delta = (c["rain_30d_per_mm"] * d.get("rain_30d", 0) + c["freeze_thaw_14d"] * d.get("freeze_thaw_14d", 0)
             + c["rain_then_frost_7d"] * d.get("rain_then_frost_7d", 0))
    esal = risk["segment_id"].map(seg.set_index("segment_id")["esal_day"])
    lf = load_factor(esal, cfg)
    out = risk.copy()
    for k in w["weeks"]:
        col = f"p30_w{k}"
        h = -np.log1p(-out[col].clip(0, 0.999).values)
        out[col] = 1 - np.exp(-h * np.exp(delta * lf))
    return out


def model_rescore(risk: pd.DataFrame, scenario: str, cfg: dict, root: Path | None = None) -> pd.DataFrame | None:
    """Re-score with lane B's saved LightGBM + isotonic models. None if lane B is not available."""
    try:
        import lightgbm as lgb
        from roadsense.models import risk as R
        from roadsense.synthetic.common import load_config as lane_b_config
    except ImportError:
        return None
    md = I.resolve(cfg, "models_dir", root)
    if not (md / "risk_y30.txt").exists() or not (md / "risk_y30_calibrator.pkl").exists():
        return None
    bcfg = lane_b_config()
    bcfg["as_of"] = str(risk["as_of"].iloc[0])
    data = R.load_data(bcfg, root)
    fwd, _ = R.forward_rows(data, bcfg, Path(root or I.ROOT))
    # read as text and normalise line endings: a Windows checkout with autocrlf breaks model_file=
    booster = lgb.Booster(model_str=(md / "risk_y30.txt").read_text(encoding="utf-8").replace(chr(13), ""))
    with open(md / "risk_y30_calibrator.pkl", "rb") as f:
        iso = pickle.load(f)
    feats = booster.feature_name()

    def predict(rows):
        return iso.predict(booster.predict(rows[feats]))

    base = predict(fwd)
    pert = fwd.copy()
    shift = int(fwd["horizon_week"].min())  # lane B numbers weeks 1-4 (schemas.md v2); lane C uses 0-3
    m = (pert["horizon_week"] - shift).isin(cfg["whatif"]["weeks"]).values
    for f, v in cfg["whatif"]["scenarios"][scenario].items():
        if f in pert:
            pert.loc[m, f] = pert.loc[m, f] + v
    # The weekly synthetic-trained model (its output is outputs/risk_synthetic_latest.parquet) gives the weather
    # response. Its hazard ratio (scenario / baseline) scales the plan's own risk, which since 17 Oct is the
    # real-label ranking (trained_on = real), so the what-if never swaps one model's level for the other's.
    def hazard(p):
        return -np.log1p(-np.clip(p, 0, 0.999))

    ratio = np.clip(hazard(predict(pert)) / np.maximum(hazard(base), 1e-6), 1.0 / 20, 20.0)
    diff = pd.DataFrame({"segment_id": fwd["segment_id"], "k": fwd["horizon_week"] - shift, "r": ratio})
    piv = diff.pivot_table(index="segment_id", columns="k", values="r")
    if risk["segment_id"].isin(piv.index).mean() < 0.5:
        return None  # lane B's model runs on other segment ids (mock risk on real segments): use the surrogate
    out = risk.copy()
    for k in piv.columns:
        col = f"p30_w{k}"
        if col in out:
            r = out["segment_id"].map(piv[k]).fillna(1.0).values
            out[col] = (1 - np.exp(-hazard(out[col].values) * r)).clip(0, 0.999)
    return out


def apply_scenario(risk: pd.DataFrame, seg: pd.DataFrame, scenario: str, cfg: dict, root: Path | None = None,
                   method: str = "auto") -> pd.DataFrame:
    if scenario not in cfg["whatif"]["scenarios"]:
        raise ValueError(f"unknown scenario {scenario}; choose from {list(cfg['whatif']['scenarios'])}")
    out = model_rescore(risk, scenario, cfg, root) if method in ("auto", "model") else None
    used = "risk_model" if out is not None else "sensitivity_surrogate"
    if out is None:
        if method == "model":
            raise RuntimeError("lane B risk model not available")
        out = surrogate(risk, seg, scenario, cfg)
    out["p30_trend"] = out["p30_w3"] - out["p30_w0"]
    out["scenario"] = scenario
    out["scenario_method"] = used
    return out


def compare(scored_base: pd.DataFrame, scored_scn: pd.DataFrame) -> pd.DataFrame:
    b = scored_base.set_index("segment_id")
    s = scored_scn.set_index("segment_id")
    df = pd.DataFrame({"name": b["name"], "ward": b["ward"], "highway": b["highway"], "esal_day": b["esal_day"],
                       "bus_stop": b["bus_stop"], "p30_peak_base": b["p30_peak"], "p30_peak_scn": s["p30_peak"],
                       "urgency_base": b["urgency_peak"], "urgency_scn": s["urgency_peak"], "band_scn": s["band_peak"]})
    df["p30_peak_change"] = df["p30_peak_scn"] - df["p30_peak_base"]
    return df[b["scored"]].sort_values("p30_peak_change", ascending=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", required=True, choices=["rain_then_frost", "wet_week", "cold_snap"])
    ap.add_argument("--root", default=None)
    a = ap.parse_args()
    from roadsense.prevent.plan import run
    root = Path(a.root) if a.root else None
    base = run(root=root, write=False, verbose=False)
    scn = run(root=root, scenario=a.scenario)
    cmp_ = compare(base["scored"], scn["scored"])
    q = cmp_["esal_day"].quantile([0.1, 0.9])
    lo = cmp_[cmp_["esal_day"] <= q.iloc[0]]["p30_peak_change"].mean()
    hi = cmp_[cmp_["esal_day"] >= q.iloc[1]]["p30_peak_change"].mean()
    print(f"{a.scenario}: mean p30 peak rise {hi:.4f} on the top-10% esal_day segments vs {lo:.4f} on the bottom 10%")
    print(cmp_.head(10)[["name", "ward", "highway", "esal_day", "p30_peak_base", "p30_peak_scn"]].to_string())


if __name__ == "__main__":
    main()
