"""Streetlight failure model (M7): Weibull survival on burn hours by lamp type.

Fitted with lifelines' WeibullFitter per lamp type on light_faults.csv (burnouts are events,
units still in service are censored, units installed before the fault log starts are
left-truncated). Burn hours follow sunset to sunrise minus the 01:00-05:30 switch-off from
1 December 2023. Electrical and damage faults are added as a constant rate.

Per lamp: probability of failure in the next 90 days, predicted replacement date (cumulative
failure >= 0.5), and the currently dark set. Baseline: rank by install year only.
The lamp register and fault history are SYNTHETIC.

Usage: python -m roadsense.models.lights [--root PATH]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.synthetic.common import ROOT, load_config, resolve
from roadsense.synthetic.streetlights import burn_hours_series

MODEL_VERSION = "lights-v0.1"
HORIZON_DAYS = 90


class BurnClock:
    """Cumulative burn hours by calendar day, including future days (astral is deterministic)."""

    def __init__(self, cfg: dict, start: str, end: str):
        s = burn_hours_series(cfg, start, end)
        self.day0 = s.index[0]
        self.cum = np.r_[0.0, np.cumsum(s.values)]
        self.rate_per_day = float(s.iloc[-730:].mean())

    def at(self, dates) -> np.ndarray:
        k = (pd.to_datetime(pd.Series(dates)).values - np.datetime64(self.day0)) / np.timedelta64(1, "D")
        k = np.asarray(k, dtype=float)
        n = len(self.cum) - 1
        inside = np.clip(k, 0, n).astype(int)
        return self.cum[inside] + np.where(k > n, (k - n) * self.rate_per_day, 0.0)

    def between(self, a, b) -> np.ndarray:
        return self.at(b) - self.at(a)


def known_units(lamps: pd.DataFrame, faults: pd.DataFrame) -> pd.DataFrame:
    """Every lamp unit we have a record of: burnt-out units (from faults) and units in service."""
    burn = faults[faults["fault_type"] == "burnout"]
    done = pd.DataFrame({"lamp_id": burn["lamp_id"].values, "unit_type": burn["unit_type"].values,
                         "install": pd.to_datetime(burn["unit_install_date"]).values,
                         "fail": pd.to_datetime(burn["fault_date"]).values})
    cur = pd.DataFrame({"lamp_id": lamps["lamp_id"].values, "unit_type": lamps["current_unit_type"].values,
                        "install": pd.to_datetime(lamps["current_unit_install_date"]).values, "fail": pd.NaT})
    u = pd.concat([done, cur], ignore_index=True).drop_duplicates(["lamp_id", "install"], keep="first")
    return u.sort_values(["lamp_id", "install"]).reset_index(drop=True)


def survival_data(units: pd.DataFrame, clock: BurnClock, at: pd.Timestamp, log_start: pd.Timestamp) -> pd.DataFrame:
    """Survival rows as seen at date `at`: duration and entry in burn hours."""
    u = units[units["install"] < at].copy()
    failed = u["fail"].notna() & (u["fail"] < at)
    end = u["fail"].where(failed, at)
    u["event"] = failed.astype(int)
    u["hours"] = clock.between(u["install"], end)
    u["entry"] = np.where(u["install"] < log_start, clock.between(u["install"], pd.Series(log_start, index=u.index)), 0.0)
    return u[(u["hours"] > u["entry"]) & (u["hours"] > 0)]


def fit_weibull(surv: pd.DataFrame) -> dict:
    from lifelines import WeibullFitter
    out = {}
    for t, g in surv.groupby("unit_type"):
        wf = WeibullFitter().fit(g["hours"], g["event"], entry=g["entry"])
        out[t] = {"scale_h": float(wf.lambda_), "shape": float(wf.rho_), "n": int(len(g)), "events": int(g["event"].sum())}
    return out


def other_fault_rate(faults: pd.DataFrame, n_lamps: int, start: pd.Timestamp, end: pd.Timestamp) -> float:
    f = faults[(faults["fault_type"] != "burnout") & (pd.to_datetime(faults["fault_date"]) >= start)
               & (pd.to_datetime(faults["fault_date"]) < end)]
    years = (end - start).days / 365.25
    return len(f) / max(n_lamps * years, 1e-9)


def _cdf(h, p):
    return 1.0 - np.exp(-((np.asarray(h) / p["scale_h"]) ** p["shape"]))


def in_service(units: pd.DataFrame, at: pd.Timestamp) -> pd.DataFrame:
    """The unit in service (or awaiting replacement) at each lamp on date `at`."""
    u = units[units["install"] <= at]
    return u.sort_values("install").groupby("lamp_id").tail(1).set_index("lamp_id")


def score(lamps: pd.DataFrame, units: pd.DataFrame, params: dict, other_rate: float, clock: BurnClock,
          at: pd.Timestamp) -> pd.DataFrame:
    cur = in_service(units, at).reindex(lamps["lamp_id"])
    h = clock.between(cur["install"].values, pd.Series(at, index=range(len(cur))))
    h90 = clock.between(pd.Series(at, index=range(len(cur))), pd.Series(at + pd.Timedelta(days=HORIZON_DAYS), index=range(len(cur))))
    p_burn = np.zeros(len(cur))
    rep_date = np.empty(len(cur), dtype="datetime64[ns]")
    for t, p in params.items():
        m = (cur["unit_type"] == t).values
        s0 = 1 - _cdf(h[m], p)
        s1 = 1 - _cdf(h[m] + h90[m], p)
        p_burn[m] = np.where(s0 > 0, 1 - s1 / np.maximum(s0, 1e-300), 1.0)
        h50 = p["scale_h"] * np.log(2) ** (1 / p["shape"])          # burn hours where F = 0.5
        days = np.maximum(h50 - h[m], 0) / clock.rate_per_day
        rep_date[m] = (at + pd.to_timedelta(np.round(days), unit="D")).values
    p_other = 1 - np.exp(-other_rate * HORIZON_DAYS / 365.25)
    return pd.DataFrame({
        "lamp_id": lamps["lamp_id"].values, "unit_type": cur["unit_type"].values,
        "unit_install_date": pd.to_datetime(cur["install"]).dt.date.values, "burn_hours": np.round(h, 0),
        "p_fail_90d": 1 - (1 - p_burn) * (1 - p_other),
        "predicted_replacement_date": pd.DatetimeIndex(rep_date).date,
        "replacement_overdue": pd.DatetimeIndex(rep_date) <= at,
    })


def fms_dark_lamps(out: pd.DataFrame, root: Path, as_of: pd.Timestamp, since: pd.Timestamp, radius_m: float = 25.0) -> pd.Series:
    """Nearest lamp to each open FixMyStreet street-lighting report made in the dark window (real data)."""
    p = root / "data" / "processed" / "fms_streetlights.csv"
    if not p.exists():
        return pd.Series(dtype=object)
    f = pd.read_csv(p)
    f["date"] = pd.to_datetime(f["requested"], utc=True, format="mixed").dt.tz_localize(None)
    f = f[(f["status"] == "open") & (f["date"] >= since) & (f["date"] < as_of)]
    if f.empty:
        return pd.Series(dtype=object)
    from scipy.spatial import cKDTree
    from roadsense.synthetic.common import to_xy
    tree = cKDTree(np.c_[to_xy(out["lon"].values, out["lat"].values)])
    d, j = tree.query(np.c_[to_xy(f["lon"].values, f["lat"].values)])
    ok = d <= radius_m
    return pd.Series(f["date"].dt.date.values[ok], index=out["lamp_id"].values[j[ok]]).groupby(level=0).min()


def precision_top(score_vals: np.ndarray, y: np.ndarray, frac: float) -> float:
    k = max(1, int(round(len(y) * frac)))
    jitter = 1e-12 * np.random.default_rng(0).random(len(y))
    return float(y[np.argsort(-(score_vals + jitter))[:k]].mean())


def backtest(lamps, faults, units, cfg, clock, log_start, as_of) -> dict:
    """Fit on everything before cutoff = as_of - 90 days and predict failures in the next 90 days."""
    cut = as_of - pd.Timedelta(days=HORIZON_DAYS)
    params = fit_weibull(survival_data(units, clock, cut, log_start))
    rate = other_fault_rate(faults, len(lamps), log_start, cut)
    sc = score(lamps, units, params, rate, clock, cut)
    fd = pd.to_datetime(faults["fault_date"])
    inwin = (fd >= cut) & (fd < as_of)
    y = sc["lamp_id"].isin(set(faults.loc[inwin, "lamp_id"])).values.astype(float)
    yb = sc["lamp_id"].isin(set(faults.loc[inwin & (faults["fault_type"] == "burnout"), "lamp_id"])).values.astype(float)
    base = -pd.to_datetime(sc["unit_install_date"]).dt.year.values.astype(float)     # oldest install year first
    frac = cfg["risk"]["top_frac"]
    return {"cutoff": str(cut.date()), "lamps": int(len(y)), "failures_in_window": int(y.sum()),
            "base_rate": float(y.mean()), "params_at_cutoff": params,
            "model_precision_top5": precision_top(sc["p_fail_90d"].values, y, frac),
            "baseline_install_year_precision_top5": precision_top(base, y, frac),
            "burnouts_in_window": int(yb.sum()), "burnout_base_rate": float(yb.mean()),
            "model_precision_top5_burnout": precision_top(sc["p_fail_90d"].values, yb, frac),
            "baseline_install_year_precision_top5_burnout": precision_top(base, yb, frac),
            "expected_failures_model": float(sc["p_fail_90d"].sum())}


def write_report(path: Path, cfg, params, bt, out, n_dark, run_info):
    so = cfg["streetlights"]["switch_off"]
    gen = cfg["streetlights"]["weibull"]
    L = ["# Streetlight model evaluation", "",
         f"_Generated {run_info['generated_at']} by `python -m roadsense.models.lights` ({MODEL_VERSION}). as_of {cfg['as_of']}._", "",
         "**The lamp register and fault history are SYNTHETIC** (Generators 3 and 4). The burn-hour clock is real: "
         f"astral sunset to sunrise for Southampton, minus the council's {so['start']} to {so['end']} switch-off from {so['from']}.", "",
         "## Fitted Weibull lifetimes (burn hours)", "",
         "| Unit type | Scale (h) | Shape | Units | Burnouts | Generator assumption |", "|---|---|---|---|---|---|"]
    for t, p in params.items():
        g = gen.get(t, {})
        L.append(f"| {t} | {p['scale_h']:,.0f} | {p['shape']:.2f} | {p['n']:,} | {p['events']:,} | scale {g.get('scale_h', 'n/a'):,}, shape {g.get('shape', 'n/a')} |")
    L += ["", "Recovering the generator's parameters is a pipeline check only.", "",
          f"## Back-test: failures in the {HORIZON_DAYS} days after {bt['cutoff']}", "",
          f"Refitted on data before the cutoff. {bt['lamps']:,} lamps, {bt['failures_in_window']:,} with a fault in the window "
          f"(base rate {bt['base_rate']:.3f}); the model expected {bt['expected_failures_model']:,.0f}.", "",
          "| Scorer | Precision in top 5%, any fault | Precision in top 5%, burnouts only |", "|---|---|---|",
          f"| Weibull on burn hours by lamp type | {bt['model_precision_top5']:.3f} | {bt['model_precision_top5_burnout']:.3f} |",
          f"| Baseline: install year only | {bt['baseline_install_year_precision_top5']:.3f} | {bt['baseline_install_year_precision_top5_burnout']:.3f} |",
          f"| Random (base rate) | {bt['base_rate']:.3f} | {bt['burnout_base_rate']:.3f} |", "",
          "Almost every unit in service is an LED on the same burn-hour clock, so burn hours rank lamps much like install "
          "year does, and the model ranks no better than the baseline (the gap is within noise: about 20 hits in the top 5%). Its value here is the calibrated probability and the replacement "
          "date, not the ranking. Electrical and damage faults (2% a year, random) cap precision for any scorer.", "",
          "## Current outputs", "",
          f"- Lamps scored: {len(out):,}; mean P(fail in 90 days) {out['p_fail_90d'].mean():.3f}.",
          f"- Due in 90 days (P >= 0.5 or replacement date within 90 days): {int(out['due_90d'].sum()):,}.",
          f"- Replacement overdue (cumulative failure already >= 0.5): {int(out['replacement_overdue'].sum()):,}.",
          f"- Dark from open FixMyStreet reports in the last 30 days (real): {int((out['dark_source'] == 'fixmystreet').sum())}.",
          f"- Currently dark (failed in the last 30 days, not yet repaired): {n_dark}.", ""]
    fms = path.parents[1] / "data" / "processed" / "fms_streetlights.csv"
    if fms.exists():
        f = pd.read_csv(fms)
        yrs = pd.to_datetime(f["requested"], utc=True, format="mixed").dt.year.value_counts().sort_index()
        L += ["## Real check: FixMyStreet street-lighting reports (Southampton, real data)", "",
              f"{len(f)} reports in `data/processed/fms_streetlights.csv`, by year: "
              + ", ".join(f"{y}: {n}" for y, n in yrs.items()) + ".",
              "This is far below any plausible lamp fault rate (inferred: streetlight faults mostly go through the council's ENERVEO portal instead), "
              "so it cannot calibrate the synthetic fault generator; open reports join the currently dark set in the app.", ""]
    path.write_text("\n".join(L), encoding="utf-8")


def run(cfg: dict | None = None, root: Path | None = None, verbose: bool = True) -> dict:
    cfg = cfg or load_config()
    root = Path(root or ROOT)
    t0 = datetime.now(timezone.utc)
    sd = resolve(cfg, "synthetic_dir", root)
    lamps = pd.read_csv(sd / "streetlights.csv")
    faults = pd.read_csv(sd / "light_faults.csv")
    as_of = pd.Timestamp(cfg["as_of"])
    log_start = pd.Timestamp(cfg["streetlights"]["faults_start"])
    first = min(pd.to_datetime(lamps["install_date"]).min(), pd.to_datetime(lamps["current_unit_install_date"]).min())
    clock = BurnClock(cfg, str(first.date()), str((as_of + pd.Timedelta(days=730)).date()))
    units = known_units(lamps, faults)

    params = fit_weibull(survival_data(units, clock, as_of, log_start))
    rate = other_fault_rate(faults, len(lamps), log_start, as_of)
    sc = score(lamps, units, params, rate, clock, as_of)
    bt = backtest(lamps, faults, units, cfg, clock, log_start, as_of)

    win = as_of - pd.Timedelta(days=cfg["streetlights"]["dark_window_days"])
    fd = pd.to_datetime(faults["fault_date"])
    dark = faults[(faults["status"] == "open") & (fd >= win)].groupby("lamp_id")["fault_date"].min()
    out = lamps[["lamp_id", "segment_id", "lat", "lon"]].merge(sc, on="lamp_id")
    out = out.rename(columns={"unit_type": "lamp_type", "predicted_replacement_date": "replace_by"})
    out["due_90d"] = (out["p_fail_90d"] >= 0.5) | (pd.to_datetime(out["replace_by"]) <= as_of + pd.Timedelta(days=HORIZON_DAYS))
    out["currently_dark"] = out["lamp_id"].isin(dark.index)
    out["dark_source"] = np.where(out["currently_dark"], "synthetic", "")
    out["dark_since"] = out["lamp_id"].map(dark)
    real_dark = fms_dark_lamps(out, root, as_of, win)
    if len(real_dark):
        m = out["lamp_id"].isin(real_dark.index)
        out.loc[m, "currently_dark"] = True
        out.loc[m, "dark_source"] = "fixmystreet"
        out.loc[m, "dark_since"] = out.loc[m, "lamp_id"].map(real_dark)
    out["uses_synthetic"] = True
    out["is_synthetic"] = True
    out["model_version"] = f"lights@{t0.strftime('%Y-%m-%dT%H:%M')}"
    out["source"] = f"model:{MODEL_VERSION} on synthetic:streetlights,light_faults"
    out["as_of"] = cfg["as_of"]
    od = resolve(cfg, "outputs_dir", root)
    od.mkdir(parents=True, exist_ok=True)
    path = od / f"lights_{cfg['as_of']}.parquet"
    out.to_parquet(path, index=False)
    out.to_parquet(od / "lights_latest.parquet", index=False)
    md = resolve(cfg, "models_dir", root)
    md.mkdir(parents=True, exist_ok=True)
    (md / "lights_weibull.json").write_text(json.dumps({"params": params, "other_fault_rate_per_year": rate,
                                                        "backtest": bt, "version": MODEL_VERSION}, indent=1), encoding="utf-8")
    rd = resolve(cfg, "reports_dir", root)
    rd.mkdir(parents=True, exist_ok=True)
    write_report(rd / "lights_eval.md", cfg, params, bt, out, int(out["currently_dark"].sum()),
                 {"generated_at": t0.isoformat(timespec="seconds")})
    if verbose:
        print(f"params: {params}")
        print(f"other-fault rate/yr: {rate:.4f}")
        print(f"backtest any fault: model top5 {bt['model_precision_top5']:.3f} vs install-year {bt['baseline_install_year_precision_top5']:.3f} "
              f"(base {bt['base_rate']:.3f}); burnouts: {bt['model_precision_top5_burnout']:.3f} vs "
              f"{bt['baseline_install_year_precision_top5_burnout']:.3f} (base {bt['burnout_base_rate']:.3f})")
        print(f"due 90d {int(out['due_90d'].sum())}, overdue {int(out['replacement_overdue'].sum())}, dark {int(out['currently_dark'].sum())}")
        print(f"wrote {path}")
    return {"out": path, "params": params, "backtest": bt}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None)
    run(root=ap.parse_args().root)


if __name__ == "__main__":
    main()
