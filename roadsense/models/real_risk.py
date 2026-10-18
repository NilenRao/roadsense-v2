"""Real-label "where" model on FixMyStreet reports from several port cities (M5, real data).

Follows lane A's schema (schemas.md) and agreed windows (config.yaml: labels.windows):
row = segment x year on all road classes (is_main_road is a feature), label had_report, features use only earlier years.
Pooled across cities with city as a categorical feature, and sample weights so each city
contributes equal total positive weight. Validated per city on its test years and
leave-one-city-out; scored as lane A agreed: share of a year's reports that fall on the top
10% of road length, against last-year's-reports and load x past-reports baselines, with a
bootstrap range. The 2026 (Jan-Oct) "frozen model" check trains on everything up to 2025.

Usage: python -m roadsense.models.real_risk [--root PATH]
FixMyStreet counts measure reporting, not true pothole rates, so scores rank segments
within a city and year and are never compared as raw totals across cities.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from roadsense.synthetic.common import ROOT, load_config, neighbours, resolve, to_xy

MODEL_VERSION = "real-risk-v0.1"
HIGHWAY_CODE = {"primary": 3, "primary_link": 3, "trunk": 4, "trunk_link": 4, "secondary": 2, "secondary_link": 2,
                "tertiary": 1, "tertiary_link": 1}
STATIC = ["log_esal_day", "log_hgv_bus_aadf", "bus_routes", "bus_stop", "junction", "in_flood_area", "length_m", "highway_code",
          "is_main_road"]
WEATHER = ["wx_rain_30d_mean", "wx_freeze_thaw", "wx_frost_hours", "wx_rain_then_frost", "wx_hot_days"]
HISTORY = ["rep_prev1", "rep_prev2_3", "rep_nb_prev1", "rep_prev1_per_km"]
# Building sites near the segment (brownfield registers) and utility digs on it (DfT Street Manager), from earlier
# years only (roadsense.features.works_context). Added to the full model on 18 Oct at the team's request; the
# held-out gain is reported in reports/context_eval.md. NaN where a city or year has no data (Portsmouth; works
# before 2021), which LightGBM treats as missing.
CONTEXT = ["con_sites_3y", "con_dwellings_3y", "uw_carriageway_1y", "uw_any_1y", "uw_carriageway_2y"]
PHYSICAL = STATIC + WEATHER                     # no past-report features at all
FEATURES = PHYSICAL + HISTORY + CONTEXT + ["city_code"]   # full model = physical + past reports + sites and digs
FEATURES_NO_CONTEXT = PHYSICAL + HISTORY + ["city_code"]  # the model before 18 Oct, for the held-out comparison
MONOTONE = {"log_esal_day": 1, "log_hgv_bus_aadf": 1}
TOP_LENGTH_FRAC = 0.10


def lane_a_config(root: Path) -> dict:
    p = root / "config.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


# --- inputs ---------------------------------------------------------------------------------
def load_inputs(root: Path) -> dict | None:
    proc = root / "data" / "processed"
    seg_p = proc / "segments.parquet"
    if not seg_p.exists():
        return None
    seg = pd.read_parquet(seg_p)
    seg["segment_id"] = seg["segment_id"].astype(str)
    load_p = proc / "load.parquet"
    if load_p.exists() and "esal_day" not in seg:
        ld = pd.read_parquet(load_p)
        ld["segment_id"] = ld["segment_id"].astype(str)
        seg = seg.merge(ld[[c for c in ["segment_id", "esal_day", "hgv_bus_aadf", "load_source"] if c in ld]], on="segment_id", how="left")
    lab_p = proc / "labels_segment_year.csv"
    labels = pd.read_csv(lab_p) if lab_p.exists() else None
    reports = None
    for p in [proc / "fms_snapped.csv", proc / "fms_roads_clean.csv"]:
        if p.exists():
            reports = pd.read_csv(p)
            reports.attrs["path"] = str(p.relative_to(root))
            break
    wx_p = proc / "weather_weekly.parquet"
    wx = pd.read_parquet(wx_p) if wx_p.exists() else None
    from roadsense.features.works_context import load_linked
    return {"seg": seg, "labels": labels, "reports": reports, "wx": wx, "context": load_linked(str(root))}


def yearly_weather(wx: pd.DataFrame) -> pd.DataFrame:
    """Ward x year weather summary. build_table joins year-1 to year, so a row never sees its own year."""
    w = wx.copy()
    w["year"] = pd.to_datetime(w["week_start"]).dt.year
    g = w.groupby(["ward", "year"])
    out = pd.DataFrame({
        "wx_rain_30d_mean": g["rain_30d"].mean(),
        "wx_freeze_thaw": g["freeze_thaw_14d"].sum() / 2.0,       # 14-day windows overlap twice in weekly rows
        "wx_frost_hours": g["hours_below_0_7d"].sum(),
        "wx_rain_then_frost": g["rain_then_frost_7d"].sum(),
        "wx_hot_days": g["hot_days_7d"].sum(),
    }).reset_index()
    return out


def snap_reports(reports: pd.DataFrame, seg: pd.DataFrame, radius_m: float) -> pd.DataFrame:
    """segment_id of the nearest segment (same city) within radius_m, else blank."""
    if "segment_id" in reports and "snap_dist_m" in reports:
        r = reports.copy()
        r.loc[r["snap_dist_m"] > radius_m, "segment_id"] = np.nan
        return r
    from roadsense.models.risk import snap
    r = reports.copy()
    r["segment_id"] = np.nan
    for city, g in r.groupby("city"):
        s = seg[seg["city"] == city].reset_index(drop=True)
        if s.empty:
            continue
        j = snap(g, s, radius_m)
        r.loc[g.index[j >= 0], "segment_id"] = s["segment_id"].values[j[j >= 0]]
    return r


def labels_from_reports(reports: pd.DataFrame, seg: pd.DataFrame, cfg_a: dict, radius_m: float) -> pd.DataFrame:
    r = snap_reports(reports, seg, radius_m)
    r["year"] = pd.to_datetime(r["requested"], utc=True, format="mixed").dt.year
    cnt = r.dropna(subset=["segment_id"]).groupby(["segment_id", "year"]).size().rename("n_reports")
    main = seg[seg.get("is_main_road", True)] if "is_main_road" in seg else seg
    windows = cfg_a.get("labels", {}).get("windows", {})
    rows = []
    for city, g in main.groupby("city"):
        w = windows.get(city)
        yrs = sorted(r.loc[r["city"] == city, "year"].unique()) if w is None else range(w["usable"][0], w["usable"][1] + 1)
        frozen = cfg_a.get("labels", {}).get("frozen_year")
        yrs = list(yrs) + ([frozen] if frozen and frozen not in yrs else [])
        for y in yrs:
            rows.append(pd.DataFrame({"city": city, "segment_id": g["segment_id"].values, "year": y}))
    lab = pd.concat(rows, ignore_index=True)
    lab["n_reports"] = lab.set_index(["segment_id", "year"]).index.map(cnt).fillna(0).astype(int)
    lab["had_report"] = (lab["n_reports"] > 0).astype(int)
    lab["split"] = [split_for(c, y, cfg_a) for c, y in zip(lab["city"], lab["year"])]
    return lab


def split_for(city: str, year: int, cfg_a: dict) -> str:
    lc = cfg_a.get("labels", {})
    if year == lc.get("frozen_year"):
        return "frozen_2026"
    w = lc.get("windows", {}).get(city)
    if w is None:
        return "train"
    if not (w["usable"][0] <= year <= w["usable"][1]):
        return "excluded"
    if w.get("test") and w["test"][0] <= year <= w["test"][1]:
        return "test"
    return "train"


# --- table ----------------------------------------------------------------------------------
def build_table(seg: pd.DataFrame, labels: pd.DataFrame, nb_radius_m: float = 220.0, wx: pd.DataFrame | None = None,
                context: tuple | None = None) -> pd.DataFrame:
    """Segment-year rows; history features use only the segment's earlier years, weather only the previous year.
    `context` = (sites, site_segments, works_segments) from works_context.load_linked: CONTEXT columns from earlier
    years only (NaN when not given)."""
    seg = seg.copy()
    if "city" not in seg:
        seg["city"] = "Southampton"
    s = pd.DataFrame({
        "segment_id": seg["segment_id"].values, "city": seg["city"].values,
        "ward": seg["ward"].values if "ward" in seg else "",
        "log_esal_day": np.log1p(seg["esal_day"].astype(float).values) if "esal_day" in seg else np.nan,
        "log_hgv_bus_aadf": np.log1p(seg["hgv_bus_aadf"].astype(float).values) if "hgv_bus_aadf" in seg else np.nan,
        "bus_routes": seg["bus_routes"].astype(float).values if "bus_routes" in seg else np.nan,
        "bus_stop": seg["bus_stop"].astype(float).values if "bus_stop" in seg else np.nan,
        "junction": seg["junction"].astype(float).values if "junction" in seg else np.nan,
        "in_flood_area": seg["in_flood_area"].astype(float).values if "in_flood_area" in seg else np.nan,
        "length_m": seg["length_m"].astype(float).values,
        "highway_code": seg["highway"].map(HIGHWAY_CODE).fillna(0).astype(float).values,
        "is_main_road": seg["is_main_road"].astype(float).values if "is_main_road" in seg else np.nan,
    })
    lab = labels[labels["split"] != "excluded"].drop(columns=["is_main_road"], errors="ignore").copy()
    lab["segment_id"] = lab["segment_id"].astype(str)
    piv = lab.pivot_table(index="segment_id", columns="year", values="n_reports", aggfunc="sum")
    # neighbour sums per city (graph-neighbour proxy: midpoints within nb_radius_m)
    nb_map = {}
    for city, g in seg.groupby("city"):
        g = g.reset_index(drop=True)
        nbs = neighbours(g, nb_radius_m)
        ids = g["segment_id"].values
        for i, sid in enumerate(ids):
            nb_map[sid] = ids[nbs[i]]
    df = lab.merge(s, on=["segment_id", "city"], how="left")

    # k-th earlier labelled year for that city (the frozen 2026 year looks back to the last usable year)
    city_years = lab.groupby("city")["year"].apply(lambda s: sorted(s.unique())).to_dict()

    def earlier(city, y, k):
        ys = [x for x in city_years.get(city, []) if x < y]
        return ys[-k] if len(ys) >= k else None

    def prev(sid, city, y, k):
        e = earlier(city, y, k)
        if sid not in piv.index or e is None:
            return np.nan
        return piv.at[sid, e]

    keys = list(zip(df["segment_id"], df["city"], df["year"]))
    df["rep_prev1"] = [prev(a, c, y, 1) for a, c, y in keys]
    p2 = np.array([prev(a, c, y, 2) for a, c, y in keys], float)
    p3 = np.array([prev(a, c, y, 3) for a, c, y in keys], float)
    df["rep_prev2_3"] = np.where(np.isnan(p2) & np.isnan(p3), np.nan, np.nan_to_num(p2) + np.nan_to_num(p3))
    nb_vals = []
    for a, c, y in keys:
        e = earlier(c, y, 1)
        if e is None:
            nb_vals.append(np.nan)
            continue
        ids = [n for n in nb_map.get(a, []) if n in piv.index]
        nb_vals.append(float(piv.loc[ids, e].fillna(0).sum()) if ids else 0.0)
    df["rep_nb_prev1"] = nb_vals
    df["rep_prev1_per_km"] = df["rep_prev1"] / (df["length_m"] / 1000.0)
    df["city_code"] = pd.Categorical(df["city"])
    if wx is not None and "ward" in seg:
        yw = yearly_weather(wx)
        yw["year"] = yw["year"] + 1                      # last year's weather predicts this year
        df = df.merge(yw, on=["ward", "year"], how="left")
    for c in WEATHER:
        if c not in df:
            df[c] = np.nan
    if context is not None and any(x is not None for x in context):
        from roadsense.features.works_context import yearly
        ex = yearly(df, *context)
        for c in CONTEXT:
            df[c] = ex[c].values
    for c in CONTEXT:
        if c not in df:
            df[c] = np.nan
    return df


# --- scoring --------------------------------------------------------------------------------
def capture_top_length(df: pd.DataFrame, score: np.ndarray, frac: float = TOP_LENGTH_FRAC) -> float:
    """Share of reports that fall on the top `frac` of road length ranked by score (one city-year)."""
    total = df["n_reports"].sum()
    if total == 0:
        return float("nan")
    jitter = 1e-9 * np.random.default_rng(0).random(len(df))
    o = np.argsort(-(np.asarray(score, float) + jitter))
    L = df["length_m"].values[o]
    take = np.cumsum(L) <= frac * L.sum()
    take[0] = True
    return float(df["n_reports"].values[o][take].sum() / total)


def baseline_scores(df: pd.DataFrame) -> dict:
    last = df["rep_prev1"].fillna(0).values
    load = np.expm1(df["log_esal_day"].fillna(df["log_esal_day"].median() if df["log_esal_day"].notna().any() else 0)).values
    return {"last_year_reports": last, "load_x_past_reports": (1 + load) * (1 + last + df["rep_prev2_3"].fillna(0).values)}


def city_weights(df: pd.DataFrame) -> np.ndarray:
    """Weight each row so every city's positives sum to the same total."""
    pos = df.groupby("city")["had_report"].sum()
    target = pos[pos > 0].mean() if (pos > 0).any() else 1.0
    f = (target / pos.replace(0, np.nan)).fillna(1.0)
    return df["city"].map(f).values.astype(float)


def features_for(cfg: dict) -> list[str]:
    """FEATURES, or FEATURES_NO_CONTEXT when config/synthetic.yaml risk.context_features is false (one switch for
    training, the weekly scores and the evaluations)."""
    return FEATURES if cfg.get("risk", {}).get("context_features", True) else FEATURES_NO_CONTEXT


def fit(train: pd.DataFrame, cfg: dict, features: list[str] | None = None):
    features = features or features_for(cfg)
    import lightgbm as lgb
    params = dict(cfg["risk"]["lgbm"])
    params.update(objective="binary", monotone_constraints=[MONOTONE.get(f, 0) for f in features],
                  random_state=cfg["seed"], deterministic=True, force_row_wise=True, n_jobs=4,
                  min_child_samples=min(params.get("min_child_samples", 20), max(5, len(train) // 50)))
    m = lgb.LGBMClassifier(**params)
    m.fit(train[features], train["had_report"].values, sample_weight=city_weights(train))
    m.feature_names_used_ = list(features)
    return m


SCORERS = ["model", "physical_only", "last_year_reports", "load_x_past_reports"]
BOOT_N, BOOT_SEED = 1000, 2026


def _mean_capture(df: pd.DataFrame, scores: dict, rows_by_year: list[np.ndarray]) -> dict:
    """Mean over test years of the top-10%-of-length capture, for each scorer, on the same rows."""
    out = {}
    for name, s in scores.items():
        caps = [capture_top_length(df.iloc[ii], s[ii]) for ii in rows_by_year if len(ii)]
        caps = [c for c in caps if c == c]
        out[name] = float(np.mean(caps)) if caps else float("nan")
    return out


def paired_bootstrap(test: pd.DataFrame, scores: dict, n: int = BOOT_N, seed: int = BOOT_SEED) -> dict:
    """Paired cluster bootstrap over test segments.

    Each resample draws test segments with replacement (keeping all of a segment's test-year rows,
    because reports cluster on segments) and scores every method on that same resample.
    Returns the point estimate and 95% percentile interval per method, plus the paired
    difference model minus last-year baseline, its interval, and the share of resamples where
    the model is ahead.
    """
    test = test.reset_index(drop=True)
    scores = {k: np.asarray(v, float) for k, v in scores.items()}
    years = sorted(test["year"].unique())
    yr = test["year"].values
    point = _mean_capture(test, scores, [np.flatnonzero(yr == y) for y in years])
    seg_codes, seg_ids = pd.factorize(test["segment_id"])
    order = np.argsort(seg_codes, kind="stable")
    bounds = np.searchsorted(seg_codes[order], np.arange(len(seg_ids) + 1))
    rows_of = [order[bounds[i]:bounds[i + 1]] for i in range(len(seg_ids))]
    rng = np.random.default_rng(seed)
    draws = {k: np.empty(n) for k in scores}
    for b in range(n):
        pick = rng.integers(0, len(seg_ids), len(seg_ids))
        idx = np.concatenate([rows_of[i] for i in pick])
        sub = test.iloc[idx].reset_index(drop=True)
        yb = yr[idx]
        m = _mean_capture(sub, {k: v[idx] for k, v in scores.items()}, [np.flatnonzero(yb == y) for y in years])
        for k in scores:
            draws[k][b] = m[k]

    def ci(v):
        return [float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5))]

    out = {k: {"capture_top10_length": point[k], "ci95": ci(draws[k])} for k in scores}
    d = draws["model"] - draws["last_year_reports"]
    out["_diff_model_minus_last_year"] = {"point": point["model"] - point["last_year_reports"], "ci95": ci(d),
                                          "share_model_ahead": float(np.mean(d > 0))}
    if "physical_only" in scores:
        d2 = draws["model"] - draws["physical_only"]
        out["_diff_model_minus_physical"] = {"point": point["model"] - point["physical_only"], "ci95": ci(d2),
                                             "share_model_ahead": float(np.mean(d2 > 0))}
        d3 = draws["physical_only"] - draws["last_year_reports"]
        out["_diff_physical_minus_last_year"] = {"point": point["physical_only"] - point["last_year_reports"], "ci95": ci(d3),
                                                 "share_model_ahead": float(np.mean(d3 > 0))}
    out["_n"] = {"reports": int(test["n_reports"].sum()), "segments": int(len(seg_ids)), "rows": int(len(test)),
                 "years": [int(y) for y in years], "resamples": n, "seed": seed}
    return out


def evaluate(models, test: pd.DataFrame, n: int = BOOT_N, seed: int = BOOT_SEED) -> dict:
    """`models` is a fitted model (full feature set) or {"model": m, "physical_only": m2}."""
    test = test.reset_index(drop=True)
    if not isinstance(models, dict):
        models = {"model": models}
    scores = {name: m.predict_proba(test[m.feature_names_used_])[:, 1] if hasattr(m, "feature_names_used_")
              else m.predict_proba(test[FEATURES])[:, 1] for name, m in models.items()}
    return paired_bootstrap(test, scores | baseline_scores(test), n=n, seed=seed)


def feature_reliance(model, test: pd.DataFrame) -> dict:
    """Mean |SHAP| (LightGBM exact tree contributions) share by feature group on the test rows."""
    X = test[model.feature_names_used_]
    contrib = model.booster_.predict(X, pred_contrib=True)[:, :-1]
    m = np.abs(contrib).mean(axis=0)
    share = m / m.sum() if m.sum() > 0 else m
    by = dict(zip(model.feature_names_used_, share.tolist()))
    groups = {
        "past reports": [f for f in HISTORY],
        "traffic load": ["log_esal_day", "log_hgv_bus_aadf"],
        "weather": WEATHER,
        "road class and length": ["highway_code", "is_main_road", "length_m"],
        "bus, junction, flood": ["bus_routes", "bus_stop", "junction", "in_flood_area"],
        "building sites and street works": CONTEXT,
        "city": ["city_code"],
    }
    g = {k: float(sum(by.get(f, 0.0) for f in fs)) for k, fs in groups.items()}
    top = sorted(by.items(), key=lambda kv: -kv[1])[:5]
    return {"groups": g, "past_reports_share": g["past reports"], "top_features": [(k, float(v)) for k, v in top],
            "n_rows": int(len(test))}


def _pct(x, dp=0):
    return f"{100 * x:.{dp}f}%"


def summary_sentence(city: str, r: dict, label: str) -> str:
    m, b, d = r["model"], r["last_year_reports"], r["_diff_model_minus_last_year"]
    s = (f"{city}, {label}: model {_pct(m['capture_top10_length'])} ({_pct(m['ci95'][0])}-{_pct(m['ci95'][1])}) vs last-year "
         f"baseline {_pct(b['capture_top10_length'])} ({_pct(b['ci95'][0])}-{_pct(b['ci95'][1])}); model ahead in "
         f"{_pct(d['share_model_ahead'])} of resamples. ")
    gap = f"difference {_pct(d['point'])} (95% {_pct(d['ci95'][0])} to {_pct(d['ci95'][1])})"
    if d["ci95"][0] > 0:
        s += f"The {gap} excludes 0, so the model beats the baseline."
    else:
        s += f"The {gap} includes 0, so the model matches the baseline rather than beats it."
    if "physical_only" in r:
        p, dp = r["physical_only"], r["_diff_physical_minus_last_year"]
        s += (f" Physical-only model (no past reports): {_pct(p['capture_top10_length'])} ({_pct(p['ci95'][0])}-{_pct(p['ci95'][1])}); "
              f"vs last-year baseline {_pct(dp['point'])} (95% {_pct(dp['ci95'][0])} to {_pct(dp['ci95'][1])}), "
              + ("so it beats the baseline." if dp["ci95"][0] > 0 else
                 "so it matches the baseline rather than beats it." if dp["ci95"][1] >= 0 else "so it falls short of the baseline."))
        dm = r["_diff_model_minus_physical"]
        s += (f" Full model vs physical-only: {_pct(dm['point'])} (95% {_pct(dm['ci95'][0])} to {_pct(dm['ci95'][1])}), "
              + ("past reports add real signal." if dm["ci95"][0] > 0 else "past reports add no clear signal."))
    if r["_n"]["reports"] < 100:
        s += f" Only {r['_n']['reports']} reports, so the interval is wide."
    return s


def results_table(r: dict) -> list[str]:
    L = ["| Method | Capture, top 10% of road length | 95% interval |", "|---|---|---|"]
    for k in SCORERS:
        if k in r:
            L.append(f"| {k.replace('_', ' ')} | {_pct(r[k]['capture_top10_length'], 1)} | "
                     f"{_pct(r[k]['ci95'][0], 1)} to {_pct(r[k]['ci95'][1], 1)} |")
    d = r["_diff_model_minus_last_year"]
    L.append(f"| model minus last-year (paired) | {_pct(d['point'], 1)} | {_pct(d['ci95'][0], 1)} to {_pct(d['ci95'][1], 1)} |")
    for key, label in [("_diff_physical_minus_last_year", "physical-only minus last-year (paired)"),
                       ("_diff_model_minus_physical", "model minus physical-only (paired)")]:
        if key in r:
            x = r[key]
            L.append(f"| {label} | {_pct(x['point'], 1)} | {_pct(x['ci95'][0], 1)} to {_pct(x['ci95'][1], 1)} |")
    n = r["_n"]
    L += ["", f"n = {n['reports']:,} test reports on {n['segments']:,} test segments ({', '.join(map(str, n['years']))}). "
          f"Model ahead of the last-year baseline in {_pct(d['share_model_ahead'])} of {n['resamples']:,} resamples (seed {n['seed']}).", ""]
    return L


SECTION_START, SECTION_END = "<!-- real-label-eval:start -->", "<!-- real-label-eval:end -->"


def risk_eval_section(res: dict, src: str, provenance: str = "") -> str:
    L = [SECTION_START, "## Real-label evaluation with uncertainty (FixMyStreet, real data)", "",
         f"Model trained on real FixMyStreet labels (`{src}`){provenance}. Score = share of a year's reports that land on "
         "the top 10% of road length, averaged over the test years. Ranges: paired cluster bootstrap that resamples test "
         f"**segments** with replacement ({BOOT_N:,} resamples, seed {BOOT_SEED}) and scores every method on the same "
         "resample. FixMyStreet counts measure reporting, not true pothole rates.", ""]
    blocks = [(c, r, "test years") for c, r in res["cities"].items()]
    blocks += [(c, r, "leave-one-city-out (trained on the other cities, all usable years)") for c, r in (res.get("loco") or {}).items()]
    blocks += [(c, r, "frozen 2026 check (Jan to Oct)") for c, r in (res.get("frozen_2026") or {}).items()]
    L += ["**Full model** = physical features + past-report features (reports on the segment and its neighbours in earlier "
          "years) + building sites near the road and utility digs on it in earlier years (brownfield registers, DfT Street "
          "Manager; added 18 Oct, gain in reports/context_eval.md). **Physical only** = same setup with no past-report feature at all: traffic load (esal_day, DfT HGV and bus flow), "
          "bus routes, bus stop, junction, flood area, highway class, road length, and the previous year's ward weather "
          "(rain, freeze-thaw, frost hours, rain-then-frost, hot days).", ""]
    for c, r, label in blocks:
        L += [f"### {c}, {label}", ""] + results_table(r) + [summary_sentence(c, r, label), ""]
        imp = (res.get("importance") or {}).get(c) if label == "test years" else None
        if imp:
            gs = ", ".join(f"{k} {_pct(v)}" for k, v in sorted(imp["groups"].items(), key=lambda kv: -kv[1]))
            tops = ", ".join(f"{k} {_pct(v)}" for k, v in imp["top_features"][:3])
            L += [f"Feature reliance (full model, share of mean |SHAP| on {imp['n_rows']:,} test rows): past-report features "
                  f"**{_pct(imp['past_reports_share'])}**; by group: {gs}. Top features: {tops}. Mean |SHAP| averages over every "
                  "segment, most of which have no report history, so it understates how much the top of the ranking leans on past "
                  "reports; the paired model-minus-physical-only difference above is the direct measure. Weather is a ward-year "
                  "summary with few distinct years in training, so its share partly reflects year-to-year differences in reporting.", ""]
    if "Brighton" not in res["cities"]:
        L += ["Brighton 2023 to 2025: waiting for lane A's Brighton labels. The same code runs once they land.", ""]
    L.append(SECTION_END)
    return "\n".join(L)


def update_risk_eval(path: Path, section: str):
    txt = path.read_text(encoding="utf-8") if path.exists() else "# Risk model evaluation\n"
    if SECTION_START in txt and SECTION_END in txt:
        txt = txt[:txt.index(SECTION_START)] + section + txt[txt.index(SECTION_END) + len(SECTION_END):]
    else:
        txt = txt.rstrip() + "\n\n" + section + "\n"
    path.write_text(txt, encoding="utf-8")


def run(cfg: dict | None = None, root: Path | None = None, verbose: bool = True, inputs: dict | None = None,
        report_root: Path | None = None, provenance: str = "") -> dict:
    cfg = cfg or load_config()
    root = Path(root or ROOT)
    cfg_a = lane_a_config(root)
    inp = inputs or load_inputs(root)
    rd = resolve(cfg, "reports_dir", root)
    rd.mkdir(parents=True, exist_ok=True)
    if inp is None:
        msg = "Waiting for lane A: data/processed/segments.parquet is not on this branch yet."
        if verbose:
            print(msg)
        return {"status": msg}
    seg = inp["seg"]
    if "city" not in seg:
        seg["city"] = "Southampton"
    radius = cfg_a.get("labels", {}).get("snap_m", 25)
    labels = inp["labels"] if inp.get("labels") is not None else labels_from_reports(inp["reports"], seg, cfg_a, radius)
    table = build_table(seg, labels, wx=inp.get("wx"), context=inp.get("context"))
    usable = table[table["rep_prev1"].notna()]
    res = {"cities": {}, "loco": {}, "frozen_2026": None}
    train = usable[usable["split"] == "train"]
    model = fit(train, cfg)
    phys = fit(train, cfg, PHYSICAL)
    res["importance"] = {}
    for city in sorted(usable["city"].unique()):
        te = usable[(usable["city"] == city) & (usable["split"] == "test")]
        if len(te):
            res["cities"][city] = evaluate({"model": model, "physical_only": phys}, te.reset_index(drop=True))
            res["importance"][city] = feature_reliance(model, te)
    if usable["city"].nunique() >= 2:
        for city in sorted(usable["city"].unique()):
            tr = usable[(usable["city"] != city) & usable["split"].isin(["train", "test"])]
            te = usable[(usable["city"] == city) & usable["split"].isin(["train", "test"])]
            if tr["had_report"].sum() and te["n_reports"].sum():
                res["loco"][city] = evaluate({"model": fit(tr, cfg), "physical_only": fit(tr, cfg, PHYSICAL)}, te.reset_index(drop=True))
    fz = usable[usable["split"] == "frozen_2026"]
    if len(fz) and fz["n_reports"].sum():
        tr_all = usable[usable["split"].isin(["train", "test"])]
        full, phys_all = fit(tr_all, cfg), fit(tr_all, cfg, PHYSICAL)
        res["frozen_2026"] = {c: evaluate({"model": full, "physical_only": phys_all}, g.reset_index(drop=True))
                              for c, g in fz.groupby("city")}
    out_root = Path(report_root) if report_root else root
    md = resolve(cfg, "models_dir", out_root)
    md.mkdir(parents=True, exist_ok=True)
    (md / "real_risk_eval.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    out_rd = resolve(cfg, "reports_dir", out_root)
    out_rd.mkdir(parents=True, exist_ok=True)
    write_report(out_rd / "real_risk_eval.md", res, table, radius, inp)
    src = inp["reports"].attrs.get("path") if inp.get("reports") is not None else "data/processed/labels_segment_year.csv"
    update_risk_eval(out_rd / "risk_eval.md", risk_eval_section(res, src, provenance))
    if verbose:
        for c, r in res["cities"].items():
            print(summary_sentence(c, r, "test years"))
            if res["importance"].get(c):
                print("past-report share of |SHAP|:", round(res["importance"][c]["past_reports_share"], 3), res["importance"][c]["groups"])
        for c, r in (res.get("frozen_2026") or {}).items():
            print(summary_sentence(c, r, "frozen 2026"))
    return res


def write_report(path: Path, res: dict, table: pd.DataFrame, radius, inp):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    src = inp["reports"].attrs.get("path") if inp.get("reports") is not None else "data/processed/labels_segment_year.csv"
    L = ["# Real-label risk model (FixMyStreet, real data)", "",
         f"_Generated {now} by `python -m roadsense.models.real_risk` ({MODEL_VERSION})._", "",
         f"Labels: real FixMyStreet road-surface reports (`{src}`), snapped to segments on all road classes within {radius} m (is_main_road is a feature, not a filter). "
         "Counts measure reporting, not pothole rates; scores rank segments within one city and year.", "",
         "Score: share of that year's reports on the top 10% of road length (mean over test years, 95% paired segment bootstrap).", ""]

    def table_md(r):
        return results_table(r)

    for title, block in [("Test years per city (pooled model)", res["cities"]), ("Leave-one-city-out", res["loco"]),
                         ("Frozen 2026 check (Jan to Oct, small)", res["frozen_2026"] or {})]:
        L += [f"## {title}", ""]
        if not block:
            L += ["Not run (not enough cities or reports).", ""]
        for c, r in block.items():
            L += [f"### {c}", ""] + table_md(r)
    path.write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None, help="folder holding lane A's data/processed")
    ap.add_argument("--report-root", default=None, help="write reports/ and data/models/ here instead of --root")
    ap.add_argument("--provenance", default="", help="note added to the report, e.g. which lane A commit was used")
    a = ap.parse_args()
    run(root=a.root, report_root=a.report_root, provenance=a.provenance)


if __name__ == "__main__":
    main()
