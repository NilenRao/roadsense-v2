"""Score every segment and build the weekly prevention plan (M6).

    python -m roadsense.prevent.plan [--forecast-as-of 2026-10-10T06:00] [--scenario rain_then_frost]

Writes outputs/prevention_plan_<as_of>.csv: segments in the 40-69 prevention band whose p30 is
forecast to rise, ranked by expected reactive cost avoided per £ of treatment.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.prevent import inputs as I
from roadsense.prevent import treatments as T
from roadsense.prevent import urgency as U

P30_WEEKS = ["p30_w0", "p30_w1", "p30_w2", "p30_w3"]


def freshness(cfg: dict, forecast_meta: dict, forecast_as_of: str | None = None, now: datetime | None = None) -> dict:
    """STALE when the forecast is older than max_forecast_age_h; provisional when stale or missing."""
    now = now or datetime.now(timezone.utc)
    ra = forecast_as_of or forecast_meta.get("retrieved_at")
    if not ra:
        return {"forecast_retrieved_at": None, "forecast_age_h": None, "stale": False, "provisional": True,
                "message": "No live forecast yet: weeks 1-3 use 10-year weekly climatology. Plan is provisional."}
    t = pd.Timestamp(ra)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    age = (pd.Timestamp(now) - t).total_seconds() / 3600
    stale = age > cfg["plan"]["max_forecast_age_h"]
    return {"forecast_retrieved_at": t.isoformat(), "forecast_age_h": round(age, 1), "stale": bool(stale),
            "provisional": bool(stale),
            "message": (f"STALE: forecast retrieved {age:.0f} h ago (limit {cfg['plan']['max_forecast_age_h']} h). "
                        "Plan is provisional.") if stale else f"Forecast retrieved {age:.0f} h ago."}


TRAFFIC_NOTE = {"high": "DfT count on this segment", "medium": "DfT count on the same road",
                "low": "modelled estimate, low confidence"}


def keyword_classifier(row) -> tuple[str, str]:
    from roadsense.agent.triage import keyword_classify
    return keyword_classify(str(row.title)), "keyword"


def confirmed_from_fms(fms: pd.DataFrame, seg: pd.DataFrame, cfg: dict, classify=None, now=None) -> dict:
    """Confirmed potholes: open FixMyStreet reports snapped to a council segment that triage classifies
    as a road defect. Open = status open and reported within triage.open_report_days; snap within
    triage.snap_radius_m. classify(row) -> (category, "gemini" | "keyword"); keyword rules by default.
    Returns {segment_id: {"source_id": "<FixMyStreet request id>", "by": "gemini" | "keyword"}}.
    """
    if fms is None or not len(fms):
        return {}
    classify = classify or keyword_classifier
    t = cfg["triage"]
    cutoff = pd.Timestamp(now or pd.Timestamp.now(tz="UTC")).tz_localize(None) - pd.Timedelta(days=t["open_report_days"])
    o = fms[(fms["status"] == "open") & (pd.to_datetime(fms["requested"]) >= cutoff)]
    if o.empty:
        return {}
    s = seg.reset_index(drop=True)
    council = ((s["owner"] != "national_highways") & (s["highway"] != "motorway")).values
    idx = I.SegmentIndex(s)
    out = {}
    for r in o.itertuples():
        j, d = idx.nearest(float(r.lon), float(r.lat), mask=council)
        if j is None or d > t["snap_radius_m"]:
            continue
        category, by = classify(r)
        if category == "pothole":
            out.setdefault(s.iloc[j]["segment_id"], {"source_id": str(int(r.id)), "by": by})
    return out


def score(seg: pd.DataFrame, risk: pd.DataFrame, cfg: dict, feedback: dict | None = None,
          confirmed: dict | None = None) -> pd.DataFrame:
    """One row per segment (council and National Highways) with urgency, band and treatment.

    feedback: {segment_id: {"confirmed_open": bool, "cracking": bool, "p30_multiplier": float}}
    from approved work orders and crew outcomes. National Highways roads are kept but never scored.
    """
    feedback = feedback or {}
    if "load_confidence" not in seg:
        seg = I._segment_defaults(seg)
    risk = I.normalise_risk(risk)
    keep = [c for c in ["segment_id", "osm_way_id", "name", "ref", "highway", "is_main_road", "ward", "length_m", "lat",
                        "lon", "geometry", "owner", "bus_stop", "bus_routes", "junction", "in_flood_area", "esal_day",
                        "hgv_bus_aadf", "load_source", "load_confidence", "count_year", "cycle_route",
                        "near_school_hospital", "bus_route", "bus_departures", "input_source"] if c in seg]
    df = seg[keep].merge(risk.drop(columns=[c for c in ["ward"] if c in risk and "ward" in seg]),
                         on="segment_id", how="left")
    nh = (df["owner"] == "national_highways") | (df["highway"] == "motorway")
    df["scored"] = ~nh & df["p30"].notna()
    for c in P30_WEEKS:
        if c not in df:
            df[c] = df["p30"]
    mult = df["segment_id"].map(lambda s: feedback.get(s, {}).get("p30_multiplier", 1.0)).astype(float)
    for c in ["p30"] + P30_WEEKS:
        df[c] = (df[c] * mult).clip(upper=0.999)
    df["confirmed_open"] = df["segment_id"].map(lambda s: bool(feedback.get(s, {}).get("confirmed_open", False)))
    df["cracking"] = df["segment_id"].map(lambda s: bool(feedback.get(s, {}).get("cracking", False)))
    df["consequence"], df["consequence_text"] = U.consequence(df, cfg)
    df["urgency"] = U.urgency(df["p30"], df["wear_pct"], df["consequence"], cfg, df["confirmed_open"])
    # an approved, not yet inspected report holds the segment at least at the report floor
    floor = df["segment_id"].map(lambda s: feedback.get(s, {}).get("urgency_floor", 0)).astype(float)
    df["urgency"] = np.maximum(df["urgency"], floor)
    df["band"] = U.band(df["urgency"], cfg)
    df.loc[~df["scored"], ["urgency", "band"]] = [np.nan, None]
    df["p30_peak"] = df[P30_WEEKS].max(axis=1)
    df["p30_rise"] = df[P30_WEEKS[1:]].max(axis=1) - df["p30_w0"]
    # urgency at the forecast peak over the next 4 weeks: what a what-if scenario moves
    df["urgency_peak"] = U.urgency(df["p30_peak"], df["wear_pct"], df["consequence"], cfg, df["confirmed_open"])
    df["urgency_peak"] = np.maximum(df["urgency_peak"], floor)
    df["band_peak"] = U.band(df["urgency_peak"], cfg)
    df.loc[~df["scored"], ["urgency_peak", "band_peak"]] = [np.nan, None]
    df["treatment"] = T.choose(df["wear_pct"], df["cracking"], df["confirmed_open"], cfg)
    df.loc[~df["scored"], "treatment"] = None
    df["treatment_label"] = df["treatment"].map(cfg["treatments"]["label"])
    df["treatment_cost_gbp"] = T.treatment_cost(df["treatment"], df["length_m"], df["highway"], cfg)
    df["expected_repairs_avoided"] = T.expected_avoided(df[P30_WEEKS].mean(axis=1), df["treatment"], cfg)
    df["reactive_cost_avoided_gbp"] = (df["expected_repairs_avoided"] * cfg["costs"]["reactive_repair"]).round(2)
    df["avoided_per_gbp"] = (df["reactive_cost_avoided_gbp"] / df["treatment_cost_gbp"]).round(4)
    df["wear"] = df["wear_pct"]
    df["open_fms_report"] = df["segment_id"].map(lambda s: bool(feedback.get(s, {}).get("open_report"))) | df["confirmed_open"]
    # uses_synthetic (contract): any synthetic input, e.g. synthetic history even when trained on real reports
    src = df["uses_synthetic"] if "uses_synthetic" in df else df.get("labels_synthetic", pd.Series(True, index=df.index))
    df["uses_synthetic"] = src.astype("boolean").fillna(True).astype(bool)
    df["traffic_note"] = df["load_confidence"].map(TRAFFIC_NOTE).fillna(TRAFFIC_NOTE["low"])
    df["status"] = np.where(nh, "National Highways road, not scored",
                            np.where(df["p30"].isna(), "no risk score", "scored"))
    # Worth treating (schemas.md section 9): pays back over the treatment's life, or a confirmed pothole
    # schemas.md section 9: confirmed_by "" | keyword | gemini | crew (precedence crew > gemini > keyword),
    # confirmed_source_id (FixMyStreet request id, or the crew inspection id), confirmed_is_synthetic
    confirmed = confirmed or {}

    def pick(sid, crew_open):
        fb = feedback.get(sid, {})
        if crew_open:
            return {"by": "crew", "source_id": fb.get("crew_outcome_id") or f"feedback:{sid}",
                    "is_synthetic": bool(fb.get("crew_is_synthetic", False))}
        best = dict(confirmed.get(sid, {}))
        return best if best.get("by") in ("keyword", "gemini") else {}

    picks = [pick(s, o) for s, o in zip(df["segment_id"], df["confirmed_open"])]
    df["confirmed_by"] = [p.get("by", "") for p in picks]
    df["confirmed_source_id"] = [str(p.get("source_id", "")) if p.get("by") else "" for p in picks]
    df["confirmed_is_synthetic"] = [bool(p.get("is_synthetic", False)) for p in picks]
    df.loc[~df["scored"], ["confirmed_by", "confirmed_source_id"]] = ""
    df.loc[~df["scored"], "confirmed_is_synthetic"] = False
    df["confirmed_pothole"] = df["scored"] & (df["confirmed_by"] != "")
    df["open_fms_report"] = df["open_fms_report"] | df["confirmed_by"].isin(["keyword", "gemini"])
    # A confirmed pothole is a statutory repair: the headline row is a permanent repair of the reported defect
    # (planned £56.94 vs reactive £87.80, ALARM 2025), held at least in the High band. The segment's own
    # preventive treatment is kept as a separate suggestion.
    c = df["confirmed_pothole"]
    for col in ["treatment", "treatment_label", "treatment_cost_gbp", "reactive_cost_avoided_gbp", "avoided_per_gbp"]:
        df["preventive_" + col] = df[col].where(c)
    costs = cfg["costs"]
    df.loc[c, "treatment"] = "permanent_repair"
    df.loc[c, "treatment_label"] = cfg["treatments"]["label"]["permanent_repair"]
    df.loc[c, "treatment_cost_gbp"] = costs["planned_repair"]
    df.loc[c, "expected_repairs_avoided"] = 1.0
    df.loc[c, "reactive_cost_avoided_gbp"] = costs["reactive_repair"]
    df.loc[c, "avoided_per_gbp"] = round(costs["reactive_repair"] / costs["planned_repair"], 4)
    hi = float(cfg["urgency"]["bands"][cfg["triage"]["report_floor_band"]])
    for u, b in [("urgency", "band"), ("urgency_peak", "band_peak")]:
        df.loc[c, u] = np.maximum(df.loc[c, u], hi)
        df.loc[c, b] = U.band(df.loc[c, u], cfg)
    df.loc[c, "reason_3"] = df.loc[c, "reason_2"]
    df.loc[c, "reason_2"] = df.loc[c, "reason_1"]
    df.loc[c, "reason_1"] = np.where(df.loc[c, "confirmed_by"] == "crew",
                                     np.where(df.loc[c, "confirmed_is_synthetic"], "pothole found by simulated crew inspection",
                                              "pothole confirmed by crew inspection"),
                                     "open FixMyStreet report, confirmed by " + df.loc[c, "confirmed_by"].fillna(""))
    df["pays_back"] = df["scored"] & (df["avoided_per_gbp"] >= cfg["plan"].get("pays_back_min", 1.0))
    candidate = df["scored"] & (df["band"] == "prevention") & (df["p30_rise"] > cfg["plan"]["min_rise"])
    df["worth_treating"] = (candidate & df["pays_back"]) | df["confirmed_pothole"]
    return df


def build_plan(scored: pd.DataFrame, cfg: dict, ward: str | None = None, data_dates: dict | None = None) -> pd.DataFrame:
    p = scored[scored["scored"] & (scored["band"] == "prevention") & (scored["p30_rise"] > cfg["plan"]["min_rise"])]
    if ward:
        p = p[p["ward"] == ward]
    p = p.sort_values(["avoided_per_gbp", "urgency"], ascending=False).head(cfg["plan"]["top_n"]).copy()
    return _decorate(p, cfg, data_dates)


def build_worth(scored: pd.DataFrame, cfg: dict, ward: str | None = None, data_dates: dict | None = None) -> pd.DataFrame:
    """Worth treating: confirmed potholes first (by urgency), then roads that pay back (by avoided_per_gbp)."""
    w = scored[scored["worth_treating"]]
    if ward:
        w = w[w["ward"] == ward]
    conf = w[w["confirmed_pothole"]].sort_values(["urgency", "avoided_per_gbp"], ascending=False)
    pays = w[~w["confirmed_pothole"]].sort_values(["avoided_per_gbp", "urgency"], ascending=False)
    return _decorate(pd.concat([conf, pays]).copy(), cfg, data_dates)


def worth_summary(worth: pd.DataFrame) -> dict:
    n_pay = int((worth["pays_back"] & ~worth["confirmed_pothole"]).sum()) if len(worth) else 0
    sim = (worth["confirmed_pothole"] & worth["confirmed_is_synthetic"]) if len(worth) else pd.Series(dtype=bool)
    n_conf = int((worth["confirmed_pothole"] & ~sim).sum()) if len(worth) else 0
    n_sim = int(sim.sum()) if len(worth) else 0
    cost = float(worth["cost_planned_gbp"].sum()) if len(worth) else 0.0
    avoided = float(worth["cost_reactive_avoided_gbp"].sum()) if len(worth) else 0.0
    sim_txt = f" · {n_sim} crew-reported (simulated)" if n_sim else ""
    return {"pays_back": n_pay, "confirmed_potholes": n_conf, "simulated_crew_reports": n_sim,
            "treatment_gbp": round(cost, 2), "reactive_avoided_gbp": round(avoided, 2),
            "header": f"{n_pay} roads pay back · {n_conf} confirmed potholes{sim_txt} · £{cost:,.0f} treatment · "
                      f"£{avoided:,.0f} reactive cost avoided"}


def badge(r: dict) -> str:
    x = r["avoided_per_gbp"]
    ratio = f"×{x:.2f}" if x >= 0.1 or x == 0 else f"×{x:.2g}"
    if r.get("confirmed_pothole") and r.get("confirmed_is_synthetic"):
        return "Simulated crew report"
    if r.get("confirmed_pothole"):
        return f"Confirmed: must fix {ratio}"
    return f"Pays back {ratio}" if r.get("pays_back") else f"Loss maker {ratio}"


def _decorate(p: pd.DataFrame, cfg: dict, data_dates: dict | None) -> pd.DataFrame:
    p.insert(0, "rank", np.arange(1, len(p) + 1))
    c = cfg["costs"]
    p["reasons"] = [" · ".join(x for x in [r.get("reason_1"), r.get("reason_2"), r.get("reason_3")] if isinstance(x, str))
                    for r in p.to_dict("records")]
    p["cost_logic"] = [
        (f"P30 {r['p30_w0']:.3f} now, peak {r['p30_peak']:.3f} in 4 weeks; "
         f"{r['treatment_label'].lower()} cuts hazard to x{cfg['treatments']['hazard_multiplier'].get(r['treatment'], 1)} "
         f"over its {cfg['treatments']['life_years'].get(r['treatment'], 0)}-year life = {r['expected_repairs_avoided']:.2f} reactive repairs avoided "
         f"x £{c['reactive_repair']:.2f} = £{r['reactive_cost_avoided_gbp']:.2f} for £{r['treatment_cost_gbp']:.2f} "
         f"(treatment £/m² is an assumption; planned repair £{c['planned_repair']:.2f} vs reactive £{c['reactive_repair']:.2f}, {c['source']})")
        for r in p.to_dict("records")]
    if "confirmed_pothole" in p and p["confirmed_pothole"].any():
        m = p["confirmed_pothole"].values
        p.loc[m, "cost_logic"] = (f"Confirmed pothole: permanent repair now, planned £{c['planned_repair']:.2f} instead of a "
                                  f"reactive £{c['reactive_repair']:.2f} later ({c['source']}) = ×"
                                  f"{c['reactive_repair'] / c['planned_repair']:.2f}. Statutory repair, not optional.")
    p["cost_planned_gbp"] = p["treatment_cost_gbp"]
    p["cost_reactive_avoided_gbp"] = p["reactive_cost_avoided_gbp"]
    p["data_dates"] = json.dumps(data_dates or {})
    p["value_badge"] = [badge(r) for r in p.to_dict("records")]
    return p


# contract section 9 first (docs/contract_outputs.md), then lane C extras the page shows
CONTRACT_COLS = ["rank", "segment_id", "ward", "name", "ref", "band", "urgency", "p30", "wear", "treatment",
                 "reason_1", "reason_2", "reason_3", "cost_planned_gbp", "cost_reactive_avoided_gbp", "open_fms_report",
                 "uses_synthetic", "data_dates", "pays_back", "confirmed_pothole", "confirmed_by", "confirmed_source_id",
                 "confirmed_is_synthetic", "worth_treating"]
PLAN_COLS = CONTRACT_COLS + ["highway", "p30_peak", "p30_rise", "consequence", "consequence_text", "treatment_label",
                             "avoided_per_gbp", "reasons", "cost_logic", "esal_day", "load_confidence", "traffic_note",
                             "weather_source", "model_version", "as_of", "labels_synthetic", "history_source", "wear_synthetic",
                             "inputs_mock", "p30_w0", "value_badge", "preventive_treatment",
                             "preventive_treatment_label", "preventive_treatment_cost_gbp",
                             "preventive_reactive_cost_avoided_gbp", "preventive_avoided_per_gbp"]


def data_dates(seg_meta: dict, risk_meta: dict, fresh: dict) -> dict:
    return {"segments": (seg_meta.get("retrieved_at") or "")[:10] or None, "risk": risk_meta.get("as_of"),
            "forecast": fresh.get("forecast_retrieved_at")}


def run(cfg: dict | None = None, root: Path | None = None, forecast_as_of: str | None = None,
        scenario: str | None = None, feedback: dict | None = None, write: bool = True, verbose: bool = True,
        classify=None) -> dict:
    cfg = cfg or I.load_config()
    seg, seg_meta = I.load_segments(cfg, root)
    risk, risk_meta = I.load_risk(cfg, root, seg=seg)
    if scenario:
        from roadsense.prevent.whatif import apply_scenario
        risk = apply_scenario(risk, seg, scenario, cfg, root=root)
    sc = score(seg, risk, cfg, feedback, confirmed=confirmed_from_fms(I.load_fms(cfg, root), seg, cfg, classify=classify))
    fm = I.load_forecast_meta(cfg, root)
    if risk_meta.get("forecast_retrieved_at"):
        fm = {**fm, "available": True, "retrieved_at": risk_meta["forecast_retrieved_at"]}
    fresh = freshness(cfg, fm, forecast_as_of)
    plan = build_plan(sc, cfg, data_dates=data_dates(seg_meta, risk_meta, fresh))
    plan["provisional"] = fresh["provisional"]
    worth = build_worth(sc, cfg, data_dates=data_dates(seg_meta, risk_meta, fresh))
    worth["provisional"] = fresh["provisional"]
    out = None
    if write:
        od = I.resolve(cfg, "outputs_dir", root)
        od.mkdir(parents=True, exist_ok=True)
        tag = f"_{scenario}" if scenario else ""
        out = od / f"prevention_plan_{risk_meta['as_of']}{tag}.csv"
        plan[[c for c in PLAN_COLS + ["provisional"] if c in plan]].to_csv(out, index=False, encoding="utf-8-sig")
        if not scenario:  # schemas.md section 9: a fixed-name copy for the API (no symlinks)
            shutil.copyfile(out, od / "prevention_plan_latest.csv")
            worth[[c for c in PLAN_COLS + ["provisional"] if c in worth]].to_csv(
                od / f"prevention_plan_worth_treating_{risk_meta['as_of']}.csv", index=False, encoding="utf-8-sig")
    if verbose:
        n = sc["scored"].sum()
        print(f"scored {n:,} segments ({seg_meta['kind']} segments, {risk_meta['kind']} risk as of {risk_meta['as_of']})")
        print("bands: " + ", ".join(f"{b} {int((sc['band'] == b).sum()):,}" for b in U.BAND_ORDER))
        print(f"prevention plan: {len(plan)} segments{'' if not scenario else f' under {scenario}'}; {fresh['message']}")
        print(f"worth treating: {worth_summary(worth)['header']}")
        if out:
            print(f"wrote {out}")
    return {"scored": sc, "plan": plan, "worth": worth, "freshness": fresh, "path": out, "meta": [seg_meta, risk_meta]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None)
    ap.add_argument("--forecast-as-of", default=None, help="pretend the forecast was retrieved at this time")
    ap.add_argument("--scenario", default=None, choices=["rain_then_frost", "wet_week", "cold_snap"])
    a = ap.parse_args()
    run(root=Path(a.root) if a.root else None, forecast_as_of=a.forecast_as_of, scenario=a.scenario)


if __name__ == "__main__":
    main()
