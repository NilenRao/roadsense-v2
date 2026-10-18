"""Weekly Southampton risk ranked by the real-label model (decision 17 Oct, for Merge 3).

Where: the real-label model (roadsense.models.real_risk), trained on FixMyStreet reports from
Southampton, Brighton and Portsmouth, scores every Southampton segment for this year.
Its history input for 2026 is the SYNTHETIC council repair log, mapped by within-city rank to
the distribution of FixMyStreet counts the model was trained on (council repairs are ~20x the
report rate, so raw counts would push every road to the top). Rows say history_source=synthetic.

When: the real model is yearly. Week-to-week movement comes from a ward-level weather
multiplier: the synthetic-trained weekly model's mean p7/p30 for that ward and week divided by
its yearly average. It scales whole wards, so it never reorders segments within a ward.

Scale: each segment's share of real-model risk is applied to Southampton's recorded repairs
(FOI 2025, config potholes.yearly_totals) to give an expected yearly pothole count, then p7/p30.

Usage: python -m roadsense.models.plan_risk   (after roadsense.models.risk and synthetic)
Writes outputs/risk_<as_of>.parquet and outputs/risk_latest.parquet (schemas.md section 7).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.models import real_risk as rr
from roadsense.synthetic.common import ROOT, load_config, neighbours, resolve

MODEL_VERSION = "plan-risk-v0.1"
HISTORY_YEARS = 3


def quantile_map(values: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Give each value the target-distribution quantile at its within-city rank (ties share a rank)."""
    values = np.asarray(values, float)
    target = np.sort(np.asarray(target, float))
    if len(values) == 0 or len(target) == 0:
        return np.zeros(len(values))
    pct = pd.Series(values).rank(method="average", pct=True).values
    return np.quantile(target, np.clip(pct, 0, 1), method="inverted_cdf")


def synthetic_history_labels(seg: pd.DataFrame, potholes: pd.DataFrame, fms_counts: np.ndarray, score_year: int,
                             city: str) -> pd.DataFrame:
    """Label-shaped rows for the HISTORY_YEARS before score_year (mapped synthetic counts) plus the score year."""
    ph = potholes.copy()
    ph["year"] = pd.to_datetime(ph["reported_date"]).dt.year
    rows = []
    for y in range(score_year - HISTORY_YEARS, score_year):
        cnt = seg["segment_id"].map(ph[ph["year"] == y].groupby("segment_id").size()).fillna(0).values
        rows.append(pd.DataFrame({"city": city, "segment_id": seg["segment_id"].values, "year": y,
                                  "n_reports": quantile_map(cnt, fms_counts), "split": "history"}))
    rows.append(pd.DataFrame({"city": city, "segment_id": seg["segment_id"].values, "year": score_year,
                              "n_reports": 0.0, "split": "score"}))
    lab = pd.concat(rows, ignore_index=True)
    lab["had_report"] = (lab["n_reports"] > 0).astype(int)
    return lab


REASONS = {
    "log_esal_day": lambda r: f"heavy axle load ({np.expm1(r['log_esal_day']):,.0f} ESAL/day)",
    "log_hgv_bus_aadf": lambda r: f"HGV and bus flow {np.expm1(r['log_hgv_bus_aadf']):,.0f} a day",
    "bus_routes": lambda r: f"{r['bus_routes']:.0f} bus routes use this road",
    "bus_stop": lambda r: "bus stop on this segment",
    "junction": lambda r: "busy junction",
    "in_flood_area": lambda r: "inside an Environment Agency flood zone",
    "length_m": lambda r: f"long segment ({r['length_m']:.0f} m)",
    "highway_code": lambda r: "road class (A/B road)" if r["highway_code"] >= 2 else "road class",
    "is_main_road": lambda r: "main road (A/B)",
    "wx_rain_30d_mean": lambda r: "wet ward last year",
    "wx_freeze_thaw": lambda r: f"{r['wx_freeze_thaw']:.0f} freeze-thaw cycles in the ward last year",
    "wx_frost_hours": lambda r: f"{r['wx_frost_hours']:.0f} frost hours in the ward last year",
    "wx_rain_then_frost": lambda r: "rain-then-frost days in the ward last year",
    "wx_hot_days": lambda r: "hot days in the ward last year",
    "rep_prev1": lambda r: "potholes repaired here last year (synthetic council log, scaled)",
    "rep_prev2_3": lambda r: "potholes repaired here in earlier years (synthetic council log, scaled)",
    "rep_nb_prev1": lambda r: "potholes on neighbouring segments last year (synthetic council log, scaled)",
    "rep_prev1_per_km": lambda r: "high pothole density last year (synthetic council log, scaled)",
    "city_code": lambda r: "city",
    "con_sites_3y": lambda r: (f"{r['con_sites_3y']:.0f} building sites" if r["con_sites_3y"] > 1 else "building site")
                              + " within 150 m (permission in the last 3 years)",
    "con_dwellings_3y": lambda r: f"{r['con_dwellings_3y']:.0f} new homes permitted within 150 m",
    "uw_carriageway_1y": lambda r: f"road dug up {r['uw_carriageway_1y']:.0f}x last year (Street Manager)",
    "uw_any_1y": lambda r: f"{r['uw_any_1y']:.0f} utility or highway digs here last year (Street Manager)",
    "uw_carriageway_2y": lambda r: f"road dug up {r['uw_carriageway_2y']:.0f}x in the last 2 years (Street Manager)",
}
def _n_reports(x: float, noun: str) -> str:
    n = int(round(x))
    return f"{n} {noun}" + ("" if n == 1 else "s")


REAL_HISTORY_REASONS = {
    "rep_prev1": lambda r: f"{_n_reports(r['rep_prev1'], 'FixMyStreet pothole report')} here last year",
    "rep_prev2_3": lambda r: f"{_n_reports(r['rep_prev2_3'], 'FixMyStreet pothole report')} here in the two years before",
    "rep_nb_prev1": lambda r: f"{_n_reports(r['rep_nb_prev1'], 'FixMyStreet report')} on neighbouring segments last year",
    "rep_prev1_per_km": lambda r: "high FixMyStreet report density last year",
}
# Last year's weather summaries stay in the model but are never shown as a reason: Open-Meteo gives only
# 3 (Southampton) to 6 (Brighton) distinct grid values across a city's wards, so "frost hours in the ward"
# would repeat the same number for most roads and read as ward-specific when it is not.
NEVER_A_REASON = {"city_code", "wx_rain_30d_mean", "wx_freeze_thaw", "wx_frost_hours", "wx_rain_then_frost", "wx_hot_days"}
NOT_A_REASON_WHEN_ZERO = {"bus_routes", "bus_stop", "junction", "in_flood_area", "is_main_road", "rep_prev1",
                          "rep_prev2_3", "rep_nb_prev1", "rep_prev1_per_km", "wx_freeze_thaw", "wx_frost_hours",
                          "wx_rain_then_frost", "wx_hot_days", "con_sites_3y", "con_dwellings_3y", "uw_carriageway_1y",
                          "uw_any_1y", "uw_carriageway_2y"}


def reasons_from_contrib(model, X: pd.DataFrame, k: int = 3, history: str = "synthetic") -> list[list[str]]:
    texts = {**REASONS, **(REAL_HISTORY_REASONS if history == "real" else {})}
    contrib = model.booster_.predict(X, pred_contrib=True)[:, :-1]
    feats = list(X.columns)
    recs = X.to_dict("records")
    out = []
    for i in range(len(X)):
        rs = []
        for j in np.argsort(-contrib[i]):
            f = feats[j]
            if contrib[i, j] <= 0 or len(rs) == k:
                break
            if f in NEVER_A_REASON or (f in NOT_A_REASON_WHEN_ZERO and not recs[i][f]):
                continue
            rs.append(texts[f](recs[i]))
        out.append(rs)
    return out


def weather_multipliers(syn: pd.DataFrame, seg: pd.DataFrame, potholes: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Ward x horizon multiplier = synthetic model's mean p7/p30 that week / its yearly average for the ward."""
    ph = potholes.copy()
    ph["year"] = pd.to_datetime(ph["reported_date"]).dt.year
    full_years = [y for y in sorted(ph["year"].unique()) if y < pd.Timestamp(cfg["as_of"]).year]
    per_seg_year = ph[ph["year"].isin(full_years)].groupby("segment_id").size() / max(len(full_years), 1)
    s = seg[["segment_id", "ward"]].copy()
    s["rate"] = s["segment_id"].map(per_seg_year).fillna(0.0)
    ward_rate = s.groupby("ward")["rate"].mean()                       # synthetic potholes per segment per year
    wk = syn.groupby(["ward", "horizon_week"])[["p7", "p30"]].mean().reset_index()
    base7 = -np.expm1(-wk["ward"].map(ward_rate) * 7 / 365.25)
    base30 = -np.expm1(-wk["ward"].map(ward_rate) * 30 / 365.25)
    wk["m7"] = (wk["p7"] / base7.replace(0, np.nan)).fillna(1.0)
    wk["m30"] = (wk["p30"] / base30.replace(0, np.nan)).fillna(1.0)
    return wk[["ward", "horizon_week", "m7", "m30"]]


def score_real_history_city(model, table: pd.DataFrame, seg_all: pd.DataFrame, cats: list[str], city: str,
                            cfg: dict, od: Path, t0) -> pd.DataFrame | None:
    """Map-only weekly rows for a city whose history is real FixMyStreet (no synthetic input at all).

    The city has no council repair log, so the yearly total is ASSUMED: Southampton's FOI repairs per km
    of road (config) times this city's road length. No weather multiplier (no weekly model there),
    so the 4 weekly rows repeat the yearly risk; no wear, lights or plan.
    """
    as_of = pd.Timestamp(cfg["as_of"])
    rows = table[(table["city"] == city) & (table["year"] == as_of.year) & table["rep_prev1"].notna()].reset_index(drop=True)
    if rows.empty:
        return None
    rows["city_code"] = pd.Categorical(rows["city"], categories=cats)
    X = rows[model.feature_names_used_]
    p_report = model.predict_proba(X)[:, 1]
    reasons = reasons_from_contrib(model, X, history="real")
    home = seg_all[seg_all["city"] == cfg.get("city", "Southampton")]
    home = home[home.get("owner", "council") != "national_highways"] if "owner" in home else home
    per_km = float(cfg["potholes"]["yearly_totals"][int(cfg["potholes"]["current_year_rate_from"])]) / (home["length_m"].sum() / 1000)
    total = per_km * rows["length_m"].sum() / 1000
    hazard = np.clip(p_report, 0, 1)            # relative risk weights (bounded: -log(1-p) explodes as p -> 1)
    lam = total * hazard / hazard.sum()
    wards = dict(zip(seg_all["segment_id"], seg_all["ward"]))
    frames = []
    for k in range(1, cfg["risk"]["forward_weeks"] + 1):
        f = pd.DataFrame({"segment_id": rows["segment_id"].values, "horizon_week": k})
        f["ward"] = f["segment_id"].map(wards)
        f["week_start"] = (as_of + pd.Timedelta(weeks=k)).date()
        f["p_report_year"], f["lambda_year"] = p_report, lam
        frames.append(f)
    out = pd.concat(frames, ignore_index=True)
    out["p7"] = -np.expm1(-out["lambda_year"] * 7 / 365.25)
    out["p30"] = -np.expm1(-out["lambda_year"] * 30 / 365.25)
    out["p30_trend"] = 0.0
    out["wear"] = np.nan
    rmap = dict(zip(rows["segment_id"], reasons))
    for i in range(3):
        out[f"reason_{i + 1}"] = [rmap[sid][i] if len(rmap.get(sid, [])) > i else None for sid in out["segment_id"]]
    out["rank_pct"] = out["segment_id"].map(dict(zip(rows["segment_id"], pd.Series(p_report).rank(pct=True).values)))
    out["model_version"] = f"risk@{t0.strftime('%Y-%m-%dT%H:%M')}"
    out["trained_on"] = "real"
    out["uses_synthetic"] = False
    out["history_source"] = "real"
    out["city"] = city
    out["weather_source"] = "none"
    out["weather_basis"] = "none (yearly risk, no weekly weather model for this city)"
    out["weather_data_date"] = None
    out["uses_forecast"] = False
    out["forecast_retrieved_at"] = None
    out["weather_multiplier"] = 1.0
    out["scale_source"] = f"assumed: Southampton FOI 2025 repairs per km ({per_km:.2f}/km/yr) x {city} road length"
    out["inputs_mock"] = False
    out["as_of"] = cfg["as_of"]
    cols = ["segment_id", "city", "ward", "week_start", "horizon_week", "p7", "p30", "p30_trend", "wear", "reason_1",
            "reason_2", "reason_3", "model_version", "trained_on", "uses_synthetic", "history_source", "weather_source",
            "weather_basis", "weather_data_date", "uses_forecast", "forecast_retrieved_at", "weather_multiplier",
            "p_report_year", "lambda_year", "rank_pct", "scale_source", "inputs_mock", "as_of"]
    out = out[cols].sort_values(["segment_id", "horizon_week"]).reset_index(drop=True)
    name = city.lower().replace(" ", "_")
    out.to_parquet(od / f"risk_{name}_{cfg['as_of']}.parquet", index=False)
    out.to_parquet(od / f"risk_{name}_latest.parquet", index=False)
    return out


def run(cfg: dict | None = None, root: Path | None = None, verbose: bool = True, inputs: dict | None = None) -> dict:
    cfg = cfg or load_config()
    root = Path(root or ROOT)
    t0 = datetime.now(timezone.utc)
    cfg_a = rr.lane_a_config(root)
    inp = inputs or rr.load_inputs(root)
    if inp is None:
        raise SystemExit("plan_risk needs lane A's data/processed/segments.parquet")
    city = cfg.get("city", "Southampton")
    as_of = pd.Timestamp(cfg["as_of"])
    seg_all = inp["seg"].copy()
    if "city" not in seg_all:
        seg_all["city"] = city
    radius = cfg_a.get("labels", {}).get("snap_m", 25)
    labels = inp["labels"] if inp.get("labels") is not None else rr.labels_from_reports(inp["reports"], seg_all, cfg_a, radius)

    # 1. real-label model on every usable real row (all cities, train + test years)
    table = rr.build_table(seg_all, labels, wx=inp.get("wx"), context=inp.get("context"))
    usable = table[table["rep_prev1"].notna() & table["split"].isin(["train", "test"])]
    model = rr.fit(usable, cfg)
    cats = list(table["city_code"].cat.categories)

    # 2. Southampton score-year rows with mapped synthetic history
    sd = resolve(cfg, "synthetic_dir", root)
    potholes = pd.read_csv(sd / "potholes.csv")
    seg = seg_all[seg_all["city"] == city].reset_index(drop=True)
    if "owner" in seg:
        seg = seg[seg["owner"] != "national_highways"].reset_index(drop=True)
    fms = labels[(labels["city"] == city) & labels["split"].isin(["train", "test"])]["n_reports"].values
    hist = synthetic_history_labels(seg, potholes, fms, as_of.year, city)
    st = rr.build_table(seg, hist, wx=inp.get("wx"), context=inp.get("context"))
    st = st[st["year"] == as_of.year].reset_index(drop=True)
    st["city_code"] = pd.Categorical(st["city"], categories=cats)
    X = st[model.feature_names_used_]
    p_report = model.predict_proba(X)[:, 1]
    reasons = reasons_from_contrib(model, X)

    # 3. expected yearly potholes per segment: share of real-model risk x recorded city total
    total = float(cfg["potholes"]["yearly_totals"][int(cfg["potholes"]["current_year_rate_from"])])
    hazard = np.clip(p_report, 0, 1)            # relative risk weights (bounded: -log(1-p) explodes as p -> 1)
    lam = total * hazard / hazard.sum()
    base = pd.DataFrame({"segment_id": st["segment_id"].values, "p_report_year": p_report, "lambda_year": lam,
                         "rank_pct": pd.Series(p_report).rank(pct=True).values})

    # 4. weekly rows: weather columns and multipliers from the synthetic weekly model's output
    od = resolve(cfg, "outputs_dir", root)
    syn = pd.read_parquet(od / "risk_synthetic_latest.parquet")
    syn = syn[syn["segment_id"].isin(base["segment_id"])]
    mult = weather_multipliers(syn, seg, potholes, cfg)
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
    out["model_version"] = f"risk@{t0.strftime('%Y-%m-%dT%H:%M')}"
    out["trained_on"] = "real"
    out["uses_synthetic"] = True
    out["history_source"] = "synthetic"
    out["inputs_mock"] = False
    out["as_of"] = cfg["as_of"]
    cols = ["segment_id", "ward", "week_start", "horizon_week", "p7", "p30", "p30_trend", "wear", "reason_1", "reason_2",
            "reason_3", "model_version", "trained_on", "uses_synthetic", "history_source", "weather_source",
            "weather_basis", "weather_data_date", "uses_forecast", "forecast_retrieved_at", "weather_multiplier",
            "p_report_year", "lambda_year", "rank_pct", "inputs_mock", "as_of"]
    out = out[cols]
    out.to_parquet(od / f"risk_{cfg['as_of']}.parquet", index=False)
    out.to_parquet(od / "risk_latest.parquet", index=False)

    maps = {}
    for c in cfg.get("real_map_cities", []):
        m_out = score_real_history_city(model, table, seg_all, cats, c, cfg, od, t0)
        if m_out is not None:
            maps[c] = int(m_out["segment_id"].nunique())

    md = resolve(cfg, "models_dir", root)
    md.mkdir(parents=True, exist_ok=True)
    mp = md / "real_risk_model.txt"
    model.booster_.save_model(str(mp))
    mp.write_bytes(mp.read_bytes().replace(b"\r\n", b"\n"))
    meta = {"version": MODEL_VERSION, "features": model.feature_names_used_, "city_categories": cats,
            "trained_rows": int(len(usable)), "trained_cities": sorted(usable["city"].unique().tolist()),
            "city_total_potholes_year": total, "history_years": HISTORY_YEARS,
            "history_source": "synthetic council log mapped by within-city rank to FixMyStreet 2015-19 counts",
            "generated_at": t0.isoformat(timespec="seconds")}
    (md / "real_risk_model.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    if verbose:
        w1 = out[out["horizon_week"] == 1]
        print(f"scored {w1['segment_id'].nunique():,} {city} segments x {out['horizon_week'].nunique()} weeks; "
              f"mean p30 by week {out.groupby('horizon_week')['p30'].mean().round(4).to_dict()}")
        print(f"weather multiplier range {out['weather_multiplier'].min():.2f}-{out['weather_multiplier'].max():.2f}")
        print(w1.sort_values("p30").tail(5)[["segment_id", "p30", "reason_1", "reason_2"]].to_string(index=False))
        print(f"map-only cities (real history): {maps}")
    return {"out": od / "risk_latest.parquet", "rows": len(out), "maps": maps}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None)
    run(root=ap.parse_args().root)


if __name__ == "__main__":
    main()
