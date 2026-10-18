"""Pothole risk model (M5): LightGBM for y7 and y30 with monotone constraints and isotonic
calibration, validated on time and spatial splits against two baselines, a real FixMyStreet
lift check, SHAP reasons, wear, and 4-week forward predictions.

Usage: python -m roadsense.models.risk [--root PATH]
Labels are SYNTHETIC (Generator 2). Accuracy on them is a pipeline check, not proof the
model works on real roads.
"""
from __future__ import annotations

import argparse
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, brier_score_loss

from roadsense.models import wear as wear_mod
from roadsense.models.baselines import BASELINES, CalibratedBaseline
from roadsense.models.dataset import FEATURES, MONOTONE, WEATHER_FEATS, build_table
from roadsense.synthetic.common import ROOT, load_config, load_inputs, resolve, scored, to_xy

MODEL_VERSION = "risk-v0.1"


# --- data -----------------------------------------------------------------------------------
def load_data(cfg: dict, root: Path | None = None) -> dict:
    inp = load_inputs(cfg, root)
    sd = resolve(cfg, "synthetic_dir", root)
    res = pd.read_csv(sd / "resurfacing.csv")
    ph = pd.read_csv(sd / "potholes.csv")
    return {"seg": scored(inp["segments"]), "wx": inp["weather"], "res": res, "potholes": ph, "sources": inp["sources"]}


# --- model ----------------------------------------------------------------------------------
class RiskModel:
    """LightGBM booster plus isotonic calibrator for one horizon."""

    def __init__(self, target: str, cfg: dict, features: list[str] = FEATURES):
        self.target, self.cfg, self.features = target, cfg, list(features)
        self.booster = None
        self.iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)

    def fit(self, df: pd.DataFrame, calib_frac: float = 0.15) -> "RiskModel":
        import lightgbm as lgb
        wk = np.sort(df["week_start"].unique())
        cut = wk[int(len(wk) * (1 - calib_frac))]
        fit_df, cal_df = df[df["week_start"] < cut], df[df["week_start"] >= cut]
        y = fit_df[self.target].values
        pos = max(int(y.sum()), 1)
        params = dict(self.cfg["risk"]["lgbm"])
        params.update(objective="binary", scale_pos_weight=(len(y) - pos) / pos,
                      monotone_constraints=[MONOTONE.get(f, 0) for f in self.features],
                      monotone_constraints_method="basic", random_state=self.cfg["seed"], deterministic=True,
                      force_row_wise=True, n_jobs=4)
        self.booster = lgb.LGBMClassifier(**params)
        self.booster.fit(fit_df[self.features], y)
        self.iso.fit(self.raw(cal_df), cal_df[self.target].values)
        return self

    def raw(self, df: pd.DataFrame) -> np.ndarray:
        return self.booster.predict_proba(df[self.features])[:, 1]

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return self.iso.predict(self.raw(df))


def precision_top(df: pd.DataFrame, score: np.ndarray, y: np.ndarray, frac: float) -> float:
    """Mean over weeks of precision among the top `frac` of segments that week."""
    d = pd.DataFrame({"w": df["week_start"].values, "s": score + 1e-12 * np.random.default_rng(0).random(len(score)), "y": y})
    vals = []
    for _, g in d.groupby("w"):
        k = max(1, int(round(len(g) * frac)))
        vals.append(g.nlargest(k, "s")["y"].mean())
    return float(np.mean(vals))


def metrics(df, rank_score, prob, y, frac) -> dict:
    return {"pr_auc": float(average_precision_score(y, rank_score)) if y.sum() else float("nan"),
            "precision_top5": precision_top(df, rank_score, y, frac),
            "brier": float(brier_score_loss(y, prob)), "base_rate": float(y.mean()), "rows": int(len(y))}


def labelled_rows(table: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Drop rows whose 30-day label window runs past the end of the log."""
    as_of = pd.Timestamp(cfg["as_of"])
    return table[table["week_start"] + pd.Timedelta(days=30) <= as_of].reset_index(drop=True)


def evaluate_split(train: pd.DataFrame, test: pd.DataFrame, cfg: dict) -> dict:
    out = {}
    frac = cfg["risk"]["top_frac"]
    for h in cfg["risk"]["horizons"]:
        t = f"y{h}"
        m = RiskModel(t, cfg).fit(train)
        y = test[t].values
        res = {"model": metrics(test, m.raw(test), m.predict(test), y, frac)}
        for name, fn in BASELINES.items():
            b = CalibratedBaseline(name).fit(train, train[t].values)
            res[name] = metrics(test, fn(test), b.predict(test), y, frac)
        out[t] = res
        if h == 30:
            out["_calibration_y30"] = calibration_bins(m.predict(test), y)
    return out


def calibration_bins(p, y, n=10):
    q = np.unique(np.quantile(p, np.linspace(0, 1, n + 1)))
    idx = np.clip(np.searchsorted(q, p, side="right") - 1, 0, len(q) - 2)
    return [{"mean_pred": float(p[idx == i].mean()), "observed": float(y[idx == i].mean()), "n": int((idx == i).sum())}
            for i in range(len(q) - 1) if (idx == i).any()]


def time_split(table: pd.DataFrame, cfg: dict):
    as_of = pd.Timestamp(cfg["as_of"])
    test_start = as_of - pd.DateOffset(months=cfg["risk"]["holdout_months"])
    train = table[table["week_start"] + pd.Timedelta(days=30) <= test_start]       # 30-day gap: no label overlap
    test = table[table["week_start"] >= test_start]
    return train, test, test_start


def spatial_split(table: pd.DataFrame, cfg: dict, test_start):
    wards = sorted(table["ward"].unique())
    rng = np.random.default_rng(cfg["seed"])
    held = sorted(rng.choice(wards, size=cfg["risk"]["spatial_holdout_wards"], replace=False).tolist())
    train = table[~table["ward"].isin(held) & (table["week_start"] + pd.Timedelta(days=30) <= test_start)]
    test = table[table["ward"].isin(held) & (table["week_start"] >= test_start)]
    return train, test, held


def leave_one_city_out(table: pd.DataFrame, cfg: dict) -> dict | None:
    if "city" not in table or table["city"].nunique() < 2:
        return None
    out = {}
    for c in sorted(table["city"].unique()):
        out[c] = evaluate_split(table[table["city"] != c], table[table["city"] == c], cfg)
    return out


# --- real FixMyStreet check ------------------------------------------------------------------
def load_fms(root: Path, city: str = "Southampton") -> pd.DataFrame | None:
    for p in [root / "data" / "processed" / "fms_roads_clean.csv", root / "data" / "fms_roads_clean.csv", root / "data" / "fms_dedup.csv"]:
        if p.exists():
            f = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p)
            f = f[f["city"].str.lower() == city.lower()] if "city" in f else f
            if "category" in f:
                f = f[f["category"].str.contains("pothole|road", case=False, na=False)]
            f = f.copy()
            f["date"] = pd.to_datetime(f["requested"] if "requested" in f else f["date"], utc=True, format="mixed").dt.tz_localize(None)
            f.attrs["path"] = str(p.relative_to(root))
            return f[["date", "lat", "lon"]].dropna()
    return None


def snap(points: pd.DataFrame, seg: pd.DataFrame, radius_m: float) -> np.ndarray:
    """Index of the scored segment within radius_m of each point (-1 if none)."""
    import shapely
    from shapely import STRtree, wkb, wkt
    from shapely.geometry import Point

    def proj(c):
        return np.c_[to_xy(c[:, 0], c[:, 1])]
    geoms = [shapely.transform(wkb.loads(g) if isinstance(g, (bytes, bytearray)) else wkt.loads(g), proj) for g in seg["geometry"]]
    tree = STRtree(geoms)
    px, py = to_xy(points["lon"].values, points["lat"].values)
    pts = [Point(a, b) for a, b in zip(px, py)]
    idx, dist = tree.query_nearest(pts, max_distance=radius_m, return_distance=True, all_matches=False)
    out = np.full(len(pts), -1)
    out[idx[0]] = idx[1]
    return out


def real_world_check(models, data, cfg, root: Path) -> dict:
    fms = load_fms(root)
    if fms is None:
        return {"status": "no FixMyStreet file found"}
    as_of = pd.Timestamp(cfg["as_of"])
    t0 = as_of - pd.DateOffset(months=12)
    hold = fms[(fms["date"] >= t0) & (fms["date"] < as_of)]
    seg = data["seg"]
    j = snap(hold, seg, cfg["risk"]["label_radius_m"])
    snapped = j[j >= 0]
    week0 = t0 - pd.Timedelta(days=t0.weekday())
    snap_rows = build_table(seg, data["wx"], data["res"], data["potholes"], cfg,
                            start=str(week0.date()), end=str((week0 + pd.Timedelta(days=1)).date()), with_labels=False)
    scores = {"model": models["y30"].raw(snap_rows)}
    for name, fn in BASELINES.items():
        scores[name] = fn(snap_rows)
    out = {"source": fms.attrs.get("path"), "holdout_from": str(t0.date()), "holdout_to": str(as_of.date()),
           "reports_in_holdout": int(len(hold)), "snapped_within_15m": int(len(snapped)), "ranked_at": str(week0.date()), "results": {}}
    k = int(np.ceil(0.10 * len(seg)))
    for name, s in scores.items():
        top = set(np.argsort(-(s + 1e-12 * np.random.default_rng(1).random(len(s))))[:k])
        hits = int(sum(1 for x in snapped if x in top))
        share = hits / len(snapped) if len(snapped) else float("nan")
        out["results"][name] = {"in_top10pct": hits, "share": share, "lift": share / 0.10 if len(snapped) else float("nan")}
    return out


# --- reasons, forward predictions -----------------------------------------------------------
REASON_TEXT = {
    "log_esal_day": lambda r: f"heavy axle load ({np.expm1(r['log_esal_day']):,.0f} ESAL/day)",
    "bus_stop": lambda r: "bus stop on this segment",
    "junction": lambda r: "junction (turning and braking loads)",
    "in_flood_area": lambda r: "inside an Environment Agency flood area",
    "length_m": lambda r: f"segment length {r['length_m']:.0f} m",
    "road_class_code": lambda r: {2: "A-road", 1: "B or C road"}.get(int(r["road_class_code"]), "residential road"),
    "rain_3d": lambda r: f"{r['rain_3d']:.0f} mm rain in the last 3 days",
    "rain_7d": lambda r: f"{r['rain_7d']:.0f} mm rain in the last 7 days",
    "rain_30d": lambda r: f"{r['rain_30d']:.0f} mm rain in the last 30 days",
    "rain_90d": lambda r: f"{r['rain_90d']:.0f} mm rain in the last 90 days",
    "hours_below_0_7d": lambda r: f"{r['hours_below_0_7d']:.0f} hours below 0°C in the last 7 days",
    "freeze_thaw_14d": lambda r: f"{r['freeze_thaw_14d']:.0f} freeze-thaw cycles in the last 14 days",
    "freeze_thaw_30d": lambda r: f"{r['freeze_thaw_30d']:.0f} freeze-thaw cycles in the last 30 days",
    "rain_then_frost_7d": lambda r: f"{r['rain_then_frost_7d']:.0f} rain-then-frost days in the last 7 days",
    "hot_days_7d": lambda r: f"{r['hot_days_7d']:.0f} days above 25°C in the last 7 days",
    "days_since_resurfacing": lambda r: (f"{r['days_since_resurfacing'] / 365.25:.0f} years since resurfacing"
                                         if r['days_since_resurfacing'] == r['days_since_resurfacing'] else "no resurfacing on record"),
    "treatments_2y": lambda r: ("no preventive treatment in the last 2 years" if r["treatments_2y"] == 0
                                else f"{r['treatments_2y']:.0f} preventive treatments in the last 2 years"),
    "potholes_seg_12m": lambda r: f"{r['potholes_seg_12m']:.0f} potholes on this segment in the last 12 months",
    "potholes_nb_12m": lambda r: f"{r['potholes_nb_12m']:.0f} potholes on neighbouring segments in the last 12 months",
    "potholes_seg_8w": lambda r: f"{r['potholes_seg_8w']:.0f} potholes on this segment in the last 8 weeks",
    "potholes_nb_8w": lambda r: f"{r['potholes_nb_8w']:.0f} potholes on neighbouring segments in the last 8 weeks",
}


ZERO_IS_NOT_A_REASON = set(WEATHER_FEATS) | {"potholes_seg_12m", "potholes_nb_12m", "potholes_seg_8w", "potholes_nb_8w",
                                             "bus_stop", "junction", "in_flood_area"}


def shap_reasons(model: RiskModel, rows: pd.DataFrame, k: int = 3) -> list[list[str]]:
    import shap
    X = rows[model.features]
    sv = shap.TreeExplainer(model.booster.booster_).shap_values(X)
    sv = sv[1] if isinstance(sv, list) else sv
    out = []
    recs = X.to_dict("records")
    for i in range(len(X)):
        order = np.argsort(-sv[i])
        reasons = []
        for j in order:
            f = model.features[j]
            if sv[i, j] <= 0 or len(reasons) == k:
                break
            if f in ZERO_IS_NOT_A_REASON and not recs[i][f]:
                continue                       # "0 hours below 0°C" is not a reason a segment is risky
            reasons.append(REASON_TEXT[f](recs[i]))
        out.append(reasons)
    return out


def climatology(wx: pd.DataFrame, years: int = 10) -> pd.DataFrame:
    w = wx.copy()
    w["week_start"] = pd.to_datetime(w["week_start"])
    w = w[w["week_start"] >= w["week_start"].max() - pd.DateOffset(years=years)]
    w["woy"] = w["week_start"].dt.isocalendar().week.astype(int)
    return w.groupby(["ward", "woy"])[WEATHER_FEATS].mean().reset_index()


def forward_rows(data: dict, cfg: dict, root: Path) -> tuple[pd.DataFrame, list[str]]:
    """Rows for the next forward_weeks weeks after as_of (horizon_week 1..N, schemas.md section 7).

    History features are as at as_of. Weather per week: the weekly weather file's own row for that
    week when it has one (observed, or Open-Meteo forecast when its uses_forecast is true), else
    weather_forecast_weekly.parquet, else 10-year climatology. Returns (rows, basis per horizon).
    """
    as_of = pd.Timestamp(cfg["as_of"])
    seg, wx = data["seg"], data["wx"].copy()
    wx["week_start"] = pd.to_datetime(wx["week_start"])
    base = build_table(seg, wx, data["res"], data["potholes"], cfg, start=str(as_of.date()),
                       end=str((as_of + pd.Timedelta(days=1)).date()), with_labels=False)
    fc_path = resolve(cfg, "processed_dir", root) / "weather_forecast_weekly.parquet"
    fc = pd.read_parquet(fc_path) if fc_path.exists() else None
    if fc is not None:
        fc["week_start"] = pd.to_datetime(fc["week_start"])
    clim = climatology(wx)
    src_name = str(wx["source"].dropna().iloc[0]) if "source" in wx and wx["source"].notna().any() else (
        "mock" if data.get("sources", {}).get("weather") == "mock" else "unknown")
    meta_cols = [c for c in ["uses_forecast", "retrieved_at", "data_date"] if c in wx]
    frames, basis = [], []
    for k in range(1, cfg["risk"]["forward_weeks"] + 1):
        ws = as_of + pd.Timedelta(weeks=k)
        r = base.drop(columns=WEATHER_FEATS).copy()
        r["week_start"] = ws
        rows = wx[wx["week_start"] == ws]
        if len(rows) and rows[WEATHER_FEATS].notna().all().all():
            f = rows[["ward"] + WEATHER_FEATS + meta_cols]
            b = "forecast" if "uses_forecast" in rows and rows["uses_forecast"].astype(bool).any() else "observed"
        elif fc is not None and (fc["week_start"] == ws).any():
            f = fc[fc["week_start"] == ws][["ward"] + WEATHER_FEATS + [c for c in ["retrieved_at", "data_date"] if c in fc]]
            f = f.assign(uses_forecast=True)
            b = "forecast"
        else:
            f = clim[clim["woy"] == int(ws.isocalendar()[1])][["ward"] + WEATHER_FEATS]
            b = "climatology_10y"
        r = r.merge(f, on="ward", how="left")
        r["horizon_week"] = k
        r["weather_basis"] = b
        r["uses_forecast"] = r["uses_forecast"].fillna(False).astype(bool) if "uses_forecast" in r else False
        r["forecast_retrieved_at"] = np.where(r["uses_forecast"], r["retrieved_at"], None) if "retrieved_at" in r else None
        r["weather_source"] = src_name
        frames.append(r.drop(columns=[c for c in ["retrieved_at", "data_date"] if c in r]))
        basis.append(b)
    out = pd.concat(frames, ignore_index=True)
    wdd = wx["data_date"].dropna().max() if "data_date" in wx and wx["data_date"].notna().any() else None
    if wdd is None:
        obs = wx[~wx["uses_forecast"].astype(bool)] if "uses_forecast" in wx else wx
        wdd = (obs["week_start"].max() - pd.Timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
    out["weather_data_date"] = str(wdd)
    return out, basis


# --- report ---------------------------------------------------------------------------------
def _fmt(x):
    return "n/a" if x is None or x != x else f"{x:.3f}"


def write_report(path: Path, ev: dict, data: dict, cfg: dict, real: dict, wear_info: dict, run_info: dict):
    L = ["# Risk model evaluation", "",
         f"_Generated {run_info['generated_at']} by `python -m roadsense.models.risk` ({MODEL_VERSION}). "
         f"as_of {cfg['as_of']}._", "",
         "**Labels are SYNTHETIC** (Generator 2, seed 2026). Scores on synthetic labels are a pipeline check, "
         "not proof the model works on real roads. The real-world check below uses real FixMyStreet reports.", "",
         f"Inputs: segments `{data['sources']['segments']}`, weather `{data['sources']['weather']}`, "
         f"esal_day `{data['sources']['esal_day']}`. "
         + ("**Lane A inputs were mocked for this run**, so rerun after Merge 1." if "mock" in data["sources"].values() else ""), "",
         f"Model: LightGBM, monotone +1 on load, rain, freeze-thaw and rain-then-frost, -1 on recent preventive treatment; "
         f"scale_pos_weight for imbalance; isotonic calibration on the last 15% of training weeks. "
         f"Top 5% = mean over weeks of precision among that week's top 5% of segments.", ""]
    for split, title in [("time", f"Time split (last {cfg['risk']['holdout_months']} months held out, 30-day gap)"),
                         ("spatial", f"Spatial split (wards held out: {', '.join(ev.get('spatial_wards', []))})")]:
        L += [f"## {title}", ""]
        for t in ["y7", "y30"]:
            r = ev[split][t]
            L += [f"**{t}** (test rows {r['model']['rows']:,}, base rate {r['model']['base_rate']:.4f})", "",
                  "| Scorer | PR-AUC | Precision in top 5% | Brier |", "|---|---|---|---|"]
            for name in ["model", *BASELINES]:
                m = r[name]
                L.append(f"| {name.replace('_', ' ')} | {_fmt(m['pr_auc'])} | {_fmt(m['precision_top5'])} | {m['brier']:.5f} |")
            L.append("")
    L += ["## Leave-one-city-out", ""]
    if ev.get("loco"):
        for c, r in ev["loco"].items():
            L.append(f"- {c}: y30 PR-AUC model {_fmt(r['y30']['model']['pr_auc'])} vs prior count {_fmt(r['y30']['prior_pothole_count_only']['pr_auc'])}")
    else:
        L.append("Not applicable to this synthetic-label model: synthetic council data exists for Southampton only. "
                 "Leave-one-city-out on real labels (Southampton, Brighton, Portsmouth) is in the real-label section below.")
    L += ["", "## Calibration (time split, y30)", "", "![calibration](risk_calibration.png)", "",
          "| Mean predicted | Observed | Rows |", "|---|---|---|"]
    L += [f"| {b['mean_pred']:.4f} | {b['observed']:.4f} | {b['n']:,} |" for b in ev["time"]["_calibration_y30"]]
    L += ["", "## Real-world check: FixMyStreet Southampton (real data)", ""]
    if "results" in real:
        L += [f"Hold-out: real FixMyStreet road-surface reports from {real['holdout_from']} to {real['holdout_to']} "
              f"(`{real['source']}`), never used in training. {real['reports_in_holdout']} reports, "
              f"{real['snapped_within_15m']} within 15 m of a scored segment. Segments ranked as at {real['ranked_at']}.", "",
              "| Scorer | Reports in top 10% of segments | Share | Lift vs random (0.10) |", "|---|---|---|---|"]
        for name, r in real["results"].items():
            L.append(f"| {name.replace('_', ' ')} | {r['in_top10pct']} | {_fmt(r['share'])} | {_fmt(r['lift'])} |")
        if data["sources"]["segments"] == "mock":
            L += ["", "Caveat: segments are mocked, so real report locations do not line up with real streets. "
                  "This check is only meaningful after lane A's segments land."]
        if real["snapped_within_15m"] < 100:
            L += ["", f"Caveat: only {real['snapped_within_15m']} hold-out reports, so the lift is very noisy. "
                  "Southampton FixMyStreet volume is low (no reports 2020 to 2022)."]
    else:
        L.append(real.get("status", "not run"))
    L += ["", "## Wear model (Weibull AFT, lifelines)", "",
          f"Fitted on {wear_info['n_fit']:,} segments resurfaced inside the log window ({wear_info['events']:,} with a first pothole). "
          f"Shape rho = {wear_info['rho']:.2f}. Coefficients (log scale of time-to-first-pothole; negative = wears faster): "
          + ", ".join(f"{k} {v:+.3f}" for k, v in wear_info["coef"].items()) + ". Resurfacing history is SYNTHETIC, so wear is too.", "",
          "## Not done (see DATA_GAPS.md)", "",
          "- Synthetic plus real labels in one model: not done; the real-label model is separate (section below).",
          "- Leave-one-city-out for this synthetic-label model: not applicable (Southampton-only synthetic data); see the real-label section.", ""]
    txt = "\n".join(L)
    # keep the sections written by roadsense.models.real_risk and roadsense.models.eval_ci
    if path.exists():
        prev = path.read_text(encoding="utf-8")
        for name in ["real-label-eval", "eval-ci"]:
            a, b = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
            if a in prev and b in prev:
                txt = txt.rstrip() + "\n\n" + prev[prev.index(a):prev.index(b) + len(b)] + "\n"
    path.write_text(txt, encoding="utf-8")


def plot_calibration(bins, path: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    x = [b["mean_pred"] for b in bins]
    y = [b["observed"] for b in bins]
    fig, ax = plt.subplots(figsize=(4.5, 4.5))
    m = max(max(x), max(y)) * 1.1
    ax.plot([0, m], [0, m], color="#999", lw=1, ls="--", label="perfect")
    ax.plot(x, y, marker="o", color="#2a6fdb", label="model (y30)")
    ax.set_xlabel("Mean predicted P30")
    ax.set_ylabel("Observed rate (synthetic labels)")
    ax.set_title("Calibration, time split hold-out")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


# --- main -----------------------------------------------------------------------------------
def run(cfg: dict | None = None, root: Path | None = None, verbose: bool = True) -> dict:
    cfg = cfg or load_config()
    root = Path(root or ROOT)
    t_start = datetime.now(timezone.utc)
    data = load_data(cfg, root)
    table = labelled_rows(build_table(data["seg"], data["wx"], data["res"], data["potholes"], cfg), cfg)
    if verbose:
        print(f"segment-week rows: {len(table):,}, y7 rate {table['y7'].mean():.4f}, y30 rate {table['y30'].mean():.4f}")
    tr, te, test_start = time_split(table, cfg)
    ev = {"time": evaluate_split(tr, te, cfg)}
    str_, ste, held = spatial_split(table, cfg, test_start)
    ev["spatial"] = evaluate_split(str_, ste, cfg)
    ev["spatial_wards"] = held
    ev["loco"] = leave_one_city_out(table, cfg)

    # production models: all labelled rows
    models = {f"y{h}": RiskModel(f"y{h}", cfg).fit(table) for h in cfg["risk"]["horizons"]}
    md = resolve(cfg, "models_dir", root)
    md.mkdir(parents=True, exist_ok=True)
    for k, m in models.items():
        mp = md / f"risk_{k}.txt"
        m.booster.booster_.save_model(str(mp))
        mp.write_bytes(mp.read_bytes().replace(b"\r\n", b"\n"))      # LF on every OS (see .gitattributes)
        with open(md / f"risk_{k}_calibrator.pkl", "wb") as f:
            pickle.dump(m.iso, f)
    real = real_world_check(models, data, cfg, root)

    surv = wear_mod.survival_frame(data["seg"], data["wx"], data["res"], data["potholes"], cfg)
    wm = wear_mod.fit(surv)
    surv["wear_pct"] = wear_mod.wear_pct(wm, surv)
    wear_info = {"n_fit": int(surv["observable"].sum()), "events": int(surv.loc[surv["observable"], "event"].sum()),
                 "rho": float(np.exp(wm.params_.loc["rho_"].loc["Intercept"])),
                 "coef": {c: float(wm.params_.loc["lambda_"].loc[c]) for c in wear_mod.COVARS}}

    fwd, wsrc = forward_rows(data, cfg, root)
    for k, m in models.items():
        fwd[f"p{k[1:]}"] = m.predict(fwd)
    first = fwd[fwd["horizon_week"] == 1].reset_index(drop=True)
    reasons = shap_reasons(models["y30"], first)
    rmap = {sid: r for sid, r in zip(first["segment_id"], reasons)}
    # schemas.md section 7: one row per scored segment per forecast week (horizon_week 1..4)
    out = fwd[["segment_id", "ward", "week_start", "horizon_week", "p7", "p30", "weather_source", "weather_basis",
               "uses_forecast", "forecast_retrieved_at", "weather_data_date"]].copy()
    out = out.sort_values(["segment_id", "horizon_week"]).reset_index(drop=True)
    out["week_start"] = pd.to_datetime(out["week_start"]).dt.date
    prev_p30 = out.groupby("segment_id")["p30"].shift(1)
    out["p30_trend"] = (out["p30"] - prev_p30).fillna(0.0)          # change against the previous week's row
    out["wear"] = out["segment_id"].map(surv.set_index("segment_id")["wear_pct"]).round(1)
    for i in range(3):
        out[f"reason_{i + 1}"] = [rmap[s][i] if len(rmap[s]) > i else None for s in out["segment_id"]]
    out["model_version"] = f"risk@{t_start.strftime('%Y-%m-%dT%H:%M')}"
    out["trained_on"] = "synthetic"
    out["uses_synthetic"] = True
    out["inputs_mock"] = "mock" in data["sources"].values()
    out["as_of"] = cfg["as_of"]
    od = resolve(cfg, "outputs_dir", root)
    od.mkdir(parents=True, exist_ok=True)
    # synthetic-trained weekly model; roadsense.models.plan_risk turns it into risk_latest (real-label ranking)
    out_path = od / f"risk_synthetic_{cfg['as_of']}.parquet"
    out.to_parquet(out_path, index=False)
    out.to_parquet(od / "risk_synthetic_latest.parquet", index=False)

    rd = resolve(cfg, "reports_dir", root)
    rd.mkdir(parents=True, exist_ok=True)
    plot_calibration(ev["time"]["_calibration_y30"], rd / "risk_calibration.png")
    run_info = {"generated_at": t_start.isoformat(timespec="seconds")}
    write_report(rd / "risk_eval.md", ev, data, cfg, real, wear_info, run_info)
    (md / "risk_eval.json").write_text(json.dumps({"eval": ev, "real": real, "wear": wear_info}, indent=1, default=str), encoding="utf-8")
    if verbose:
        for split in ["time", "spatial"]:
            for t in ["y7", "y30"]:
                r = ev[split][t]
                print(f"{split:<8}{t:<4} model PR-AUC {r['model']['pr_auc']:.3f} top5 {r['model']['precision_top5']:.3f} | "
                      + " | ".join(f"{n} {r[n]['pr_auc']:.3f}/{r[n]['precision_top5']:.3f}" for n in BASELINES))
        print(f"real check: {json.dumps(real.get('results', real), default=str)}")
        print(f"wrote {out_path} ({out['segment_id'].nunique():,} segments x {out['horizon_week'].nunique()} weeks, weather {wsrc}), reports/risk_eval.md")
    return {"eval": ev, "real": real, "out": out_path}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None)
    run(root=ap.parse_args().root)


if __name__ == "__main__":
    main()
