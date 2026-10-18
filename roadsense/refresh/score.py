"""Re-score with the FROZEN models in data/models (no fitting anywhere in this file).

- Southampton weekly model (risk_y7 / risk_y30 + isotonic calibrators): 4 forward weeks on the
  refreshed weather, as roadsense.models.risk does after training.
- Real-label model (real_risk_model.txt): Southampton yearly risk (synthetic history mapped to
  FixMyStreet counts) scaled by the weekly weather multiplier, as roadsense.models.plan_risk does;
  Brighton map-only risk from real FixMyStreet history via plan_risk.score_real_history_city.

model_version stays the snapshot's value: the model did not change, only its inputs.
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.models import plan_risk as PR
from roadsense.models import real_risk as rr
from roadsense.models import risk as R


class _Clf:
    """Just enough of LGBMClassifier for the existing helpers: predict_proba, booster_, feature_names_used_."""

    def __init__(self, booster, features: list[str]):
        self.booster_ = booster
        self.feature_names_used_ = list(features)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        p = self.booster_.predict(X[self.feature_names_used_])
        return np.column_stack([1 - p, p])


class FrozenWeekly:
    """Stands in for roadsense.models.risk.RiskModel, loaded from risk_<target>.txt + calibrator."""

    def __init__(self, models_dir: Path, target: str):
        import lightgbm as lgb
        b = lgb.Booster(model_file=str(models_dir / f"risk_{target}.txt"))
        self.features = b.feature_name()
        self.booster = _Clf(b, self.features)
        with open(models_dir / f"risk_{target}_calibrator.pkl", "rb") as f:
            self.iso = pickle.load(f)

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return self.iso.predict(self.booster.booster_.predict(df[self.features]))


def frozen_real(models_dir: Path) -> tuple[_Clf, list[str]]:
    import lightgbm as lgb
    meta = json.loads((models_dir / "real_risk_model.json").read_text(encoding="utf-8"))
    b = lgb.Booster(model_file=str(models_dir / "real_risk_model.txt"))
    return _Clf(b, meta["features"]), list(meta["city_categories"])


def weekly_southampton(cfg: dict, stage: Path, models_dir: Path) -> pd.DataFrame:
    """Same rows and columns as roadsense.models.risk.run's risk_synthetic_latest.parquet."""
    od = stage / cfg["paths"]["outputs_dir"]
    prev = pd.read_parquet(od / "risk_synthetic_latest.parquet")
    data = R.load_data(cfg, stage)
    models = {f"y{h}": FrozenWeekly(models_dir, f"y{h}") for h in cfg["risk"]["horizons"]}
    fwd, _ = R.forward_rows(data, cfg, stage)
    for k, m in models.items():
        fwd[f"p{k[1:]}"] = m.predict(fwd)
    first = fwd[fwd["horizon_week"] == 1].reset_index(drop=True)
    reasons = R.shap_reasons(models["y30"], first)
    rmap = dict(zip(first["segment_id"], reasons))
    out = fwd[["segment_id", "ward", "week_start", "horizon_week", "p7", "p30", "weather_source", "weather_basis",
               "uses_forecast", "forecast_retrieved_at", "weather_data_date"]].copy()
    out = out.sort_values(["segment_id", "horizon_week"]).reset_index(drop=True)
    out["week_start"] = pd.to_datetime(out["week_start"]).dt.date
    out["p30_trend"] = (out["p30"] - out.groupby("segment_id")["p30"].shift(1)).fillna(0.0)
    out["wear"] = out["segment_id"].map(prev.drop_duplicates("segment_id").set_index("segment_id")["wear"])  # wear: no weather input
    for i in range(3):
        out[f"reason_{i + 1}"] = [rmap[s][i] if len(rmap[s]) > i else None for s in out["segment_id"]]
    out["model_version"] = prev["model_version"].iloc[0]
    out["trained_on"] = "synthetic"
    out["uses_synthetic"] = True
    out["inputs_mock"] = "mock" in data["sources"].values()
    out["as_of"] = cfg["as_of"]
    out = out[prev.columns]
    out.to_parquet(od / f"risk_synthetic_{cfg['as_of']}.parquet", index=False)
    out.to_parquet(od / "risk_synthetic_latest.parquet", index=False)
    return out


def real_risk(cfg: dict, stage: Path, models_dir: Path, cities: list[str], t0) -> dict:
    """risk_latest (Southampton) and risk_<city>_latest (map-only cities) from the frozen real model."""
    od = stage / cfg["paths"]["outputs_dir"]
    model, cats = frozen_real(models_dir)
    inp = rr.load_inputs(stage)
    seg_all = inp["seg"].copy()
    home = cfg.get("city", "Southampton")
    as_of = pd.Timestamp(cfg["as_of"])
    labels = inp["labels"]
    res = {}
    if home in cities:
        prev = pd.read_parquet(od / "risk_latest.parquet")
        potholes = pd.read_csv(stage / cfg["paths"]["synthetic_dir"] / "potholes.csv")
        seg = seg_all[seg_all["city"] == home].reset_index(drop=True)
        seg = seg[seg["owner"] != "national_highways"].reset_index(drop=True) if "owner" in seg else seg
        fms = labels[(labels["city"] == home) & labels["split"].isin(["train", "test"])]["n_reports"].values
        hist = PR.synthetic_history_labels(seg, potholes, fms, as_of.year, home)
        st = rr.build_table(seg, hist, wx=inp.get("wx"), context=inp.get("context"))
        st = st[st["year"] == as_of.year].reset_index(drop=True)
        st["city_code"] = pd.Categorical(st["city"], categories=cats)
        X = st[model.feature_names_used_]
        p_report = model.predict_proba(X)[:, 1]
        reasons = PR.reasons_from_contrib(model, X)
        total = float(cfg["potholes"]["yearly_totals"][int(cfg["potholes"]["current_year_rate_from"])])
        hazard = np.clip(p_report, 0, 1)
        base = pd.DataFrame({"segment_id": st["segment_id"].values, "p_report_year": p_report,
                             "lambda_year": total * hazard / hazard.sum(),
                             "rank_pct": pd.Series(p_report).rank(pct=True).values})
        syn = pd.read_parquet(od / "risk_synthetic_latest.parquet")
        syn = syn[syn["segment_id"].isin(base["segment_id"])]
        mult = PR.weather_multipliers(syn, seg, potholes, cfg)
        keep = ["segment_id", "ward", "week_start", "horizon_week", "weather_source", "weather_basis", "uses_forecast",
                "forecast_retrieved_at", "weather_data_date", "wear"]
        out = syn[keep].merge(base, on="segment_id", how="left").merge(mult, on=["ward", "horizon_week"], how="left")
        out["p7"] = -np.expm1(-out["lambda_year"] * 7 / 365.25 * out["m7"])
        out["p30"] = -np.expm1(-out["lambda_year"] * 30 / 365.25 * out["m30"])
        out = out.sort_values(["segment_id", "horizon_week"]).reset_index(drop=True)
        out["p30_trend"] = (out["p30"] - out.groupby("segment_id")["p30"].shift(1)).fillna(0.0)
        rmap = dict(zip(st["segment_id"], reasons))
        for i in range(3):
            out[f"reason_{i + 1}"] = [rmap[s][i] if len(rmap.get(s, [])) > i else None for s in out["segment_id"]]
        out = out.rename(columns={"m30": "weather_multiplier"}).drop(columns=["m7"])
        out["model_version"] = prev["model_version"].iloc[0]
        out["trained_on"] = "real"
        out["uses_synthetic"] = True
        out["history_source"] = "synthetic"
        out["inputs_mock"] = False
        out["as_of"] = cfg["as_of"]
        out = out[prev.columns]
        out.to_parquet(od / f"risk_{cfg['as_of']}.parquet", index=False)
        out.to_parquet(od / "risk_latest.parquet", index=False)
        res[home] = len(out)
    others = [c for c in cities if c != home]
    if others:
        table = rr.build_table(seg_all, labels, wx=inp.get("wx"), context=inp.get("context"))
        for city in others:
            name = city.lower().replace(" ", "_")
            prev_p = od / f"risk_{name}_latest.parquet"
            version = pd.read_parquet(prev_p, columns=["model_version"])["model_version"].iloc[0] if prev_p.exists() else None
            out = PR.score_real_history_city(model, table, seg_all, cats, city, cfg, od, t0)
            if out is None:
                raise RuntimeError(f"no {city} rows to score for {cfg['as_of']}")
            if version is not None:          # same frozen model: keep its version, rewrite both files
                out["model_version"] = version
                out.to_parquet(od / f"risk_{name}_{cfg['as_of']}.parquet", index=False)
                out.to_parquet(prev_p, index=False)
            res[city] = len(out)
    return res
