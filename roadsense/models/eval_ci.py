"""Bootstrap ranges on the HELD-OUT real-label evaluation, per-city feature importance, and the 2026 check.

Rerun:  python -m roadsense.models.eval_ci
Test years: the evaluation model is fitted on the training years only, exactly as in
roadsense.models.real_risk.run (same rows, same seed), so Southampton 2018-19 and Brighton 2023-25
are true hold-outs. 2026: the live plan model (data/models/real_risk_model.txt, fitted on every usable
year up to 2025) is scored without retraining; 2026 is outside its training data. Writes the "eval-ci"
section of reports/risk_eval.md and data/models/eval_ci.json. Touches no risk, plan or app output.

Resampling unit: wards (cluster bootstrap). Reports and risk cluster in space (neighbouring segments
share traffic, history and who reports), so resampling single segments or weeks would treat
correlated roads as independent and understate the noise; resampling whole wards keeps that.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.models import real_risk as rr
from roadsense.synthetic.common import ROOT, load_config, resolve

SEED, N_BOOT = 2026, 1000
SECTION_START, SECTION_END = "<!-- eval-ci:start -->", "<!-- eval-ci:end -->"
PLAIN = {
    "rep_prev1": "Potholes reported on this road last year",
    "rep_prev2_3": "Potholes reported on this road in the two years before that",
    "rep_nb_prev1": "Potholes reported on the neighbouring roads last year",
    "rep_prev1_per_km": "How dense last year's reports were along this road",
    "length_m": "Length of the road segment (longer pieces have more surface to fail)",
    "log_esal_day": "Heavy axle load (HGVs and buses wear roads fastest)",
    "log_hgv_bus_aadf": "Daily HGV and bus traffic",
    "bus_routes": "Number of bus routes using the road",
    "bus_stop": "Bus stop on the segment (braking and pulling away)",
    "junction": "Busy junction (turning and braking)",
    "in_flood_area": "Inside an Environment Agency flood zone",
    "highway_code": "Road class (A road, B road, minor road)",
    "is_main_road": "Main road (A or B road)",
    "wx_rain_30d_mean": "How wet the area was last year",
    "wx_freeze_thaw": "Freeze-thaw cycles in the area last year",
    "wx_frost_hours": "Frost hours in the area last year",
    "wx_rain_then_frost": "Rain followed by frost in the area last year",
    "wx_hot_days": "Hot days in the area last year",
    "city_code": "Which city (reporting habits differ between cities)",
    "con_sites_3y": "Building sites within 150 m with permission in the last 3 years",
    "con_dwellings_3y": "New homes permitted within 150 m in the last 3 years",
    "uw_carriageway_1y": "Times the road was dug up last year (Street Manager)",
    "uw_any_1y": "Utility or highway works on the road last year",
    "uw_carriageway_2y": "Times the road was dug up in the last 2 years",
}


def load_live_model(root: Path):
    import lightgbm as lgb
    md = root / "data" / "models"
    booster = lgb.Booster(model_str=(md / "real_risk_model.txt").read_text(encoding="utf-8").replace("\r\n", "\n"))
    meta = json.loads((md / "real_risk_model.json").read_text(encoding="utf-8"))
    return booster, meta


def score_rows(booster, meta, rows: pd.DataFrame) -> np.ndarray:
    X = rows[meta["features"]].copy()
    X["city_code"] = pd.Categorical(rows["city"], categories=meta["city_categories"])
    return booster.predict(X)


def mean_capture(df: pd.DataFrame, s: np.ndarray) -> float:
    caps = []
    for y in sorted(df["year"].unique()):
        m = (df["year"] == y).values
        c = rr.capture_top_length(df[m], s[m])
        if c == c:
            caps.append(c)
    return float(np.mean(caps)) if caps else float("nan")


def ward_bootstrap(df: pd.DataFrame, scores: dict, n: int = N_BOOT, seed: int = SEED) -> dict:
    """Paired cluster bootstrap over wards: model and baseline scored on the same resampled wards."""
    df = df.reset_index(drop=True)
    wards = df["ward"].fillna("unknown").values
    uw = np.unique(wards)
    rows_of = {w: np.flatnonzero(wards == w) for w in uw}
    point = {k: mean_capture(df, v) for k, v in scores.items()}
    rng = np.random.default_rng(seed)
    draws = {k: np.empty(n) for k in scores}
    for b in range(n):
        idx = np.concatenate([rows_of[w] for w in rng.choice(uw, size=len(uw), replace=True)])
        sub = df.iloc[idx].reset_index(drop=True)
        for k, v in scores.items():
            draws[k][b] = mean_capture(sub, v[idx])
    ci = lambda v: [float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5))]
    d = draws["model"] - draws["baseline"]
    return {"model": {"point": point["model"], "ci95": ci(draws["model"])},
            "baseline": {"point": point["baseline"], "ci95": ci(draws["baseline"])},
            "diff": {"point": point["model"] - point["baseline"], "ci95": ci(d)},
            "share_model_beats_baseline": float(np.mean(d > 0)),
            "n": {"reports": int(df["n_reports"].sum()), "segments": int(df["segment_id"].nunique()),
                  "wards": int(len(uw)), "years": [int(y) for y in sorted(df["year"].unique())],
                  "resamples": n, "seed": seed}}


def city_importance(model, rows: pd.DataFrame, k: int = 5) -> list[dict]:
    """Mean absolute SHAP value (LightGBM exact tree contributions) per feature on one city's test rows."""
    feats = model.feature_names_used_
    contrib = model.booster_.predict(rows[feats], pred_contrib=True)[:, :-1]
    imp = np.abs(contrib).mean(axis=0)
    share = imp / imp.sum()
    order = np.argsort(-imp)[:k]
    return [{"feature": feats[j], "share": float(share[j]), "plain": PLAIN.get(feats[j], feats[j])} for j in order]


def _pct(x, dp=0):
    return f"{100 * x:.{dp}f}%"


def section(res: dict) -> str:
    L = [SECTION_START, "## Held-out ranges by ward, 2026 check and top features per city", "",
         f"_Generated {res['generated_at']} by `python -m roadsense.models.eval_ci`._", "",
         "Metric: share of a test year's FixMyStreet reports that fall on the top 10% of road length, averaged over the test "
         "years. Baseline: rank by last year's reports. Ranges: paired cluster bootstrap over **wards** "
         f"({N_BOOT:,} resamples, seed {SEED}); reports cluster in space, so whole wards are resampled rather than single "
         "roads or weeks, which would understate the noise.", "",
         "### Test years (held out: model fitted on the training years only)", "",
         "| City | Model | Baseline (last year's reports) | Model minus baseline | Model ahead | Reports / wards |",
         "|---|---|---|---|---|---|"]
    for c, r in res["test"].items():
        L.append(f"| {c} {r['n']['years'][0]}-{str(r['n']['years'][-1])[2:]} | {_pct(r['model']['point'], 1)} "
                 f"({_pct(r['model']['ci95'][0], 1)} to {_pct(r['model']['ci95'][1], 1)}) | {_pct(r['baseline']['point'], 1)} "
                 f"({_pct(r['baseline']['ci95'][0], 1)} to {_pct(r['baseline']['ci95'][1], 1)}) | {_pct(r['diff']['point'], 1)} "
                 f"({_pct(r['diff']['ci95'][0], 1)} to {_pct(r['diff']['ci95'][1], 1)}) | {_pct(r['share_model_beats_baseline'])} "
                 f"| {r['n']['reports']} / {r['n']['wards']} |")
    L += ["", "### Frozen 2026 check (January to October, out of sample: live plan model, fitted on years up to 2025)", "",
          "| City | Reports | Model | Baseline | Model minus baseline | Model ahead |", "|---|---|---|---|---|---|"]
    for c, r in res["frozen_2026"].items():
        L.append(f"| {c} | {r['n']['reports']} | {_pct(r['model']['point'], 1)} ({_pct(r['model']['ci95'][0], 1)} to "
                 f"{_pct(r['model']['ci95'][1], 1)}) | {_pct(r['baseline']['point'], 1)} ({_pct(r['baseline']['ci95'][0], 1)} "
                 f"to {_pct(r['baseline']['ci95'][1], 1)}) | {_pct(r['diff']['point'], 1)} ({_pct(r['diff']['ci95'][0], 1)} to "
                 f"{_pct(r['diff']['ci95'][1], 1)}) | {_pct(r['share_model_beats_baseline'])} |")
    L += [""] + [f"- {s}" for s in res["frozen_sentences"]] + ["",
          "### Top 5 features within each city (held-out model, mean absolute SHAP on that city's test rows)", ""]
    for c, feats in res["top_features"].items():
        L += [f"**{c}**", ""]
        for i, f in enumerate(feats, start=1):
            L.append(f"{i}. {f['plain']} (`{f['feature']}`, {_pct(f['share'])} of the attribution)")
        L.append("")
    L += ["City-level features (the city itself, and last year's weather summaries such as rain, hot days, freeze-thaw "
          "and frost hours, which take only 2 to 6 distinct values across a city's wards) move every road in a city or "
          "district up or down together, so they barely change the ranking within a city.",
          "", SECTION_END]
    return "\n".join(L)


def frozen_sentence(city: str, r: dict) -> str:
    n = r["n"]["reports"]
    if n < 100:
        return (f"{city}: only {n} reports in January to October 2026, too few for this result to mean anything "
                f"(model {_pct(r['model']['point'])} vs baseline {_pct(r['baseline']['point'])}).")
    lo, hi = r["diff"]["ci95"]
    verdict = "beats" if lo > 0 else ("falls short of" if hi < 0 else "matches")
    return f"{city}: {n} reports; the model {verdict} the baseline ({_pct(r['diff']['point'])}, 95% {_pct(lo)} to {_pct(hi)})."


def update_report(path: Path, text: str):
    txt = path.read_text(encoding="utf-8")
    if SECTION_START in txt and SECTION_END in txt:
        txt = txt[:txt.index(SECTION_START)] + text + txt[txt.index(SECTION_END) + len(SECTION_END):]
    else:
        txt = txt.rstrip() + "\n\n" + text + "\n"
    path.write_text(txt, encoding="utf-8")


def run(cfg: dict | None = None, root: Path | None = None, verbose: bool = True) -> dict:
    cfg = cfg or load_config()
    root = Path(root or ROOT)
    inp = rr.load_inputs(root)
    seg = inp["seg"]
    cfg_a = rr.lane_a_config(root)
    labels = inp["labels"] if inp.get("labels") is not None else rr.labels_from_reports(
        inp["reports"], seg, cfg_a, cfg_a.get("labels", {}).get("snap_m", 25))
    table = rr.build_table(seg, labels, wx=inp.get("wx"), context=inp.get("context"))
    table = table[table["rep_prev1"].notna()].reset_index(drop=True)
    held = rr.fit(table[table["split"] == "train"], cfg)        # exactly as real_risk.run: training years only
    table["held_score"] = held.predict_proba(table[held.feature_names_used_])[:, 1]
    booster, meta = load_live_model(root)
    table["live_score"] = score_rows(booster, meta, table)
    res = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "test": {}, "frozen_2026": {},
           "top_features": {}}
    for city in ["Southampton", "Brighton"]:
        for split, key, col in [("test", "test", "held_score"), ("frozen_2026", "frozen_2026", "live_score")]:
            df = table[(table["city"] == city) & (table["split"] == split)].reset_index(drop=True)
            if len(df) and df["n_reports"].sum():
                res[key][city] = ward_bootstrap(df, {"model": df[col].values, "baseline": df["rep_prev1"].fillna(0).values})
        test_rows = table[(table["city"] == city) & (table["split"] == "test")]
        if len(test_rows):
            res["top_features"][city] = city_importance(held, test_rows)
    res["frozen_sentences"] = [frozen_sentence(c, r) for c, r in res["frozen_2026"].items()]
    rd = resolve(cfg, "reports_dir", root)
    update_report(rd / "risk_eval.md", section(res))
    (resolve(cfg, "models_dir", root) / "eval_ci.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    if verbose:
        print(json.dumps({k: res[k] for k in ["test", "frozen_2026"]}, indent=1)[:3000])
        print(res["frozen_sentences"], {c: [f["feature"] for f in v] for c, v in res["top_features"].items()})
    return res


if __name__ == "__main__":
    run()
