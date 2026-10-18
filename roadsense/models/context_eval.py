"""Held-out check: do building sites or utility digs help rank next year's pothole reports? (real data only)

    python -m roadsense.models.context_eval

Same setup as roadsense.models.real_risk (real FixMyStreet labels, segment x year rows, features from earlier years
only, the agreed train/test years per city) with the features from roadsense.features.works_context added:

  construction  con_sites_3y, con_dwellings_3y: brownfield register sites with planning permission in the three
                years before, within 150 m of the segment (Southampton and Brighton registers)
  utility_works uw_carriageway_1y, uw_any_1y, uw_carriageway_2y: completed Street Manager works on the segment in the
                year (two years) before; only from 2022 rows on (first full archive year is 2021)

For each feature set the model without CONTEXT (real_risk.FEATURES_NO_CONTEXT) is refitted with and without it on the
same training rows and both are scored
on each city's test years with real_risk's measure (share of the year's reports on the top 10% of road length),
with a paired segment bootstrap of the difference. The rule used by the product: a feature set is "used in ranking"
only if the difference's 95% interval is above 0 in at least one city and below 0 in none; otherwise it is shown on
the page as context and does not move the ranking.

Both sets are in the ranking model (team decision, 18 Oct); this check reports whether that helps on held-out years.

A second, simpler check is printed alongside: the report rate on flagged vs unflagged test segments within the same
model-risk decile (pooled over deciles, Mantel-Haenszel style), so a flag that carries information the model already
has (busy roads get both digs and reports) is not mistaken for new signal.

Writes reports/context_eval.md and data/models/context_eval.json.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.features import works_context as WC
from roadsense.models import real_risk as R
from roadsense.synthetic.common import ROOT, load_config, resolve

SETS = {"construction": ["con_sites_3y", "con_dwellings_3y"],
        "utility_works": ["uw_carriageway_1y", "uw_any_1y", "uw_carriageway_2y"]}
FLAG = {"construction": "con_sites_3y", "utility_works": "uw_carriageway_1y", "both": "con_sites_3y"}
BOOT_N, BOOT_SEED = 1000, 2026


def paired_diff(test: pd.DataFrame, base: np.ndarray, ext: np.ndarray, n: int = BOOT_N, seed: int = BOOT_SEED) -> dict:
    test = test.reset_index(drop=True)
    yr = test["year"].values
    years = sorted(set(yr))
    scores = {"base": np.asarray(base, float), "ext": np.asarray(ext, float)}
    point = R._mean_capture(test, scores, [np.flatnonzero(yr == y) for y in years])
    codes, ids = pd.factorize(test["segment_id"])
    order = np.argsort(codes, kind="stable")
    bounds = np.searchsorted(codes[order], np.arange(len(ids) + 1))
    rows_of = [order[bounds[i]:bounds[i + 1]] for i in range(len(ids))]
    rng = np.random.default_rng(seed)
    d = np.empty(n)
    cols = {c: test[c].values for c in ["n_reports", "length_m"]}
    for b in range(n):
        idx = np.concatenate([rows_of[i] for i in rng.integers(0, len(ids), len(ids))])
        yb = yr[idx]
        m = R._mean_capture({c: v[idx] for c, v in cols.items()}, {k: v[idx] for k, v in scores.items()},
                            [np.flatnonzero(yb == y) for y in years])
        d[b] = m["ext"] - m["base"]
    return {"base": point["base"], "with": point["ext"], "diff": point["ext"] - point["base"],
            "ci95": [float(np.nanpercentile(d, 2.5)), float(np.nanpercentile(d, 97.5))],
            "share_ahead": float(np.mean(d > 0)), "reports": int(test["n_reports"].sum()),
            "segments": int(len(ids)), "years": [int(y) for y in years]}


def stratified_rate_ratio(test: pd.DataFrame, score: np.ndarray, flag: np.ndarray) -> dict:
    """Report rate ratio flagged/unflagged within model-score deciles (Mantel-Haenszel pooled)."""
    t = test.assign(score=score, flag=np.asarray(flag) > 0)
    t["dec"] = pd.qcut(t["score"].rank(method="first"), 10, labels=False)
    num = den = 0.0
    for _, g in t.groupby("dec"):
        a, b = g[g["flag"]], g[~g["flag"]]
        if len(a) == 0 or len(b) == 0:
            continue
        N = len(g)
        num += a["had_report"].sum() * len(b) / N
        den += b["had_report"].sum() * len(a) / N
    return {"flagged_rows": int(t["flag"].sum()), "flagged_with_report": int(t.loc[t["flag"], "had_report"].sum()),
            "rate_flagged": float(t.loc[t["flag"], "had_report"].mean()) if t["flag"].any() else float("nan"),
            "rate_unflagged": float(t.loc[~t["flag"], "had_report"].mean()),
            "mh_rate_ratio": float(num / den) if den > 0 else float("nan")}


def run(root: Path | None = None, verbose: bool = True) -> dict:
    cfg = load_config()
    root = Path(root or ROOT)
    cfg_a = R.lane_a_config(root)
    inp = R.load_inputs(root)
    seg = inp["seg"]
    labels = inp["labels"] if inp.get("labels") is not None else R.labels_from_reports(
        inp["reports"], seg, cfg_a, cfg_a.get("labels", {}).get("snap_m", 25))
    table = R.build_table(seg, labels, wx=inp.get("wx"), context=WC.load_linked(str(root)))
    usable = table[table["rep_prev1"].notna()]
    train = usable[usable["split"] == "train"]
    base = R.fit(train, cfg, R.FEATURES_NO_CONTEXT)
    res = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sets": {}}
    sets = dict(SETS, both=SETS["construction"] + SETS["utility_works"])
    for name, feats in sets.items():
        ext = R.fit(train, cfg, R.PHYSICAL + R.HISTORY + feats + ["city_code"])  # same column order as R.FEATURES
        res["sets"][name] = {"features": feats, "train_rows_with_values": int(train[feats[0]].notna().sum()),
                             "cities": {}}
        for city in sorted(usable["city"].unique()):
            te = usable[(usable["city"] == city) & (usable["split"] == "test")].reset_index(drop=True)
            if te.empty:
                continue
            if te[feats[0]].notna().sum() == 0:
                res["sets"][name]["cities"][city] = {"status": "no data for this city's test years"}
                continue
            sb = base.predict_proba(te[base.feature_names_used_])[:, 1]
            se = ext.predict_proba(te[ext.feature_names_used_])[:, 1]
            r = paired_diff(te, sb, se)
            r["stratified"] = stratified_rate_ratio(te, sb, te[FLAG[name]].fillna(0).values)
            res["sets"][name]["cities"][city] = r
        cs = [c for c in res["sets"][name]["cities"].values() if "ci95" in c]
        helps = any(c["ci95"][0] > 0 for c in cs) and not any(c["ci95"][1] < 0 for c in cs)
        res["sets"][name]["clear_gain"] = bool(helps)
        res["sets"][name]["used_in_ranking"] = True      # team decision 18 Oct: both feature sets are in the model
        res["sets"][name]["verdict"] = ("in the model; clear held-out gain" if helps else
                                        "in the model (team decision); no clear held-out gain yet")
    md = resolve(cfg, "models_dir", root)
    md.mkdir(parents=True, exist_ok=True)
    (md / "context_eval.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    rd = resolve(cfg, "reports_dir", root)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "context_eval.md").write_text(report(res), encoding="utf-8")
    if verbose:
        print(report(res))
    return res


def _p(x, dp=1):
    return "n/a" if x != x else f"{100 * x:.{dp}f}%"


def report(res: dict) -> str:
    L = ["# Building sites and utility digs: held-out check", "",
         f"_Generated {res['generated_at']} by `python -m roadsense.models.context_eval`._", "",
         "Question: does knowing about nearby building sites, or recent utility digs on a road, help rank where next "
         "year's pothole reports land, on years the model never saw? Labels are real FixMyStreet reports; inputs are the "
         "councils' brownfield land registers (planning.data.gov.uk) and DfT Street Manager completed works (both OGL). "
         "Score: share of the year's reports on the top 10% of road length, model with vs without the features, same "
         f"training rows, paired segment bootstrap ({BOOT_N:,} resamples, seed {BOOT_SEED}). FixMyStreet counts measure "
         "reporting, not true pothole rates.", "",
         "Both feature sets are in the ranking model (roadsense.models.real_risk FEATURES) at the team's request "
         "(18 Oct). A gain is called clear when its 95% interval is above 0 in at least one city and below 0 in none; "
         "until then the page says the gain is not yet clear.", ""]
    for name, s in res["sets"].items():
        L += [f"## {name.replace('_', ' ').capitalize()}: {s['verdict']}", "",
              f"Features: {', '.join(s['features'])}. Training rows with a value: {s['train_rows_with_values']:,}.", "",
              "| City (test years) | Without | With | Gain | 95% interval | Ahead in | Reports |", "|---|---|---|---|---|---|---|"]
        notes = []
        for c, r in s["cities"].items():
            if "ci95" not in r:
                L.append(f"| {c} | | | | {r['status']} | | |")
                continue
            L.append(f"| {c} ({', '.join(map(str, r['years']))}) | {_p(r['base'])} | {_p(r['with'])} | {_p(r['diff'])} | "
                     f"{_p(r['ci95'][0])} to {_p(r['ci95'][1])} | {_p(r['share_ahead'], 0)} | {r['reports']:,} |")
            st = r["stratified"]
            notes.append(f"{c}: {st['flagged_rows']:,} flagged test segment-years, {st['flagged_with_report']:,} with a report; "
                         f"report rate {_p(st['rate_flagged'])} flagged vs {_p(st['rate_unflagged'])} unflagged, "
                         f"{st['mh_rate_ratio']:.2f}x within the same model-risk decile.")
        L += [""] + [f"- {n}" for n in notes] + [""]
    L += ["## Limits", "",
          "- Street Manager starts in July 2020, so utility digs can only be checked on Brighton's 2023-25 test years "
          "(Southampton's FixMyStreet labels stop in 2019) and the model learns them from Brighton 2022 rows only.",
          "- The registers list sites with a point, not a boundary or access road, and Southampton's register on "
          "planning.data.gov.uk has 37 sites last updated in 2017. Planning applications and council-house building "
          "programmes are not published as open data for either city (DATA_GAPS.md).",
          "- Street Manager says where a trench was opened and closed, not how well it was reinstated.", ""]
    return "\n".join(L)


if __name__ == "__main__":
    run()
