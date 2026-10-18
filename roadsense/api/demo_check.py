"""Demo dry run (M9): `python -m roadsense.api.demo_check`

Runs the six live-demo steps and the four failure cases against the real files on disk, using a
throwaway state folder so the officer's queue is untouched. Pre-computes the plan for this week and
every what-if scenario into outputs/. Prints PASS/FAIL per step and writes outputs/lane_c/demo_check.md.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from roadsense.agent.triage import gemini_client
from roadsense.api.service import Service
from roadsense.prevent import inputs as I
from roadsense.prevent import plan as P


def pick_report(svc: Service) -> dict | None:
    """Latest real FixMyStreet pothole report that snaps to a council segment."""
    from roadsense.agent.triage import keyword_classify
    f = svc.fms.sort_values("requested", ascending=False)
    f = f[(f["category"] == "Potholes") & (f["title"].map(keyword_classify) == "pothole")]
    for r in f.itertuples():
        loc = svc.ctx.lookup_location(float(r.lat), float(r.lon))
        if loc.get("status") == "ok" and loc.get("segment_id"):
            return {"fms_id": int(r.id), "title": r.title, "segment_id": loc["segment_id"], "distance_m": loc["segment_distance_m"]}
    return None


def run(live_gemini: bool = False) -> list[tuple[str, bool, str]]:
    cfg = I.load_config()
    results: list[tuple[str, bool, str]] = []

    def step(name, fn):
        t = time.time()
        try:
            ok, note = fn()
        except Exception as e:  # report, keep going
            ok, note = False, f"{type(e).__name__}: {e}"
            traceback.print_exc()
        results.append((name, bool(ok), f"{note} ({time.time() - t:.1f} s)"))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {note}")

    # pre-compute plan CSVs (base + every scenario)
    for sc in [None] + list(cfg["whatif"]["scenarios"]):
        P.run(cfg, scenario=sc, verbose=False)

    tmp = Path(tempfile.mkdtemp(prefix="roadsense_demo_"))
    cfg["paths"]["state_dir"] = str(tmp)  # absolute path wins over root in Path join
    try:
        svc = Service(cfg=cfg, client=gemini_client() if live_gemini else None)
        warm = svc.warm()
        print(f"warm: {warm}")
        ward = {}

        def s1():
            top = svc.plan()["rows"]
            if not top:
                return False, "plan is empty"
            ward["w"] = top[0]["ward"]
            p = svc.plan(ward=ward["w"])
            srcs = svc.sources()
            return len(p["rows"]) > 0 and len(srcs) >= 5, f"{p['count']} rows in {ward['w']}; {len(srcs)} sources in footer"
        step("1 ward plan with sources", s1)

        def s2():
            sid = svc.plan(ward=ward["w"])["rows"][0]["segment_id"]
            s = svc.segment(sid)
            need = ["wear_pct", "p30", "urgency", "treatment_label"]
            return all(s.get(k) is not None for k in need) and len(s["reasons"]) >= 1 and s["wear_synthetic"], \
                f"{s['name']}: urgency {s['urgency']}, wear {s['wear_pct']}%, {len(s['reasons'])} reasons, {s['treatment_label']}"
        step("2 top segment detail", s2)

        def s3():
            b = svc.scored.set_index("segment_id")
            a = svc.scored_for("rain_then_frost").set_index("segment_id")
            d = (a["p30_peak"] - b["p30_peak"])[b["scored"]]
            heavy = b.loc[b["scored"], "esal_day"] >= b.loc[b["scored"], "esal_day"].quantile(0.9)
            bus = b.loc[b["scored"], "bus_stop"].astype(bool)
            return d[heavy].mean() > d[~heavy].mean(), (f"p30 peak +{d[heavy].mean():.3f} on top-10% load vs +{d[~heavy].mean():.3f} elsewhere; "
                                                         f"bus segments +{d[bus].mean():.3f}; method {warm['methods']['rain_then_frost']}")
        step("3 rain_then_frost moves heavy corridors up", s3)

        rep = pick_report(svc)

        def s4():
            if rep is None:
                return False, "no real FixMyStreet report snaps to a segment (mock segments until lane A merges)"
            wo = svc.triage_report({"fms_id": rep["fms_id"]})
            dup = svc.triage_report({"fms_id": rep["fms_id"]})  # the same report submitted again
            ok = wo["segment_id"] == rep["segment_id"] and dup["status"] == "merged"
            ap = svc.approve(wo["report_id"])
            return ok and ap["work_order"]["status"] == "approved", (
                f"'{rep['title']}' -> {wo['category']} via {wo['method']}, segment {wo['segment_id']} "
                f"({rep['distance_m']} m), duplicate merged, approved")
        step("4 triage real report, merge duplicate, approve", s4)

        def s5():
            sid = rep["segment_id"] if rep else svc.plan()["rows"][0]["segment_id"]
            r = svc.record_outcome(sid, "pothole")
            return r["after"]["urgency"] == 100, f"{sid}: urgency {r['before']['urgency']} -> {r['after']['urgency']}"
        step("5 crew outcome re-scores segment", s5)

        def s6():
            l = svc.lights_view(due=True)
            return l["count_due"] > 0 or l["count_currently_dark"] > 0, \
                f"{l['count_due']} lamps due in 90 days, {l['count_currently_dark']} currently dark"
        step("6 streetlights due and dark", s6)

        def f1():
            out = svc.locate(51.5074, -0.1278)
            nh = svc.scored[~svc.scored["scored"] & (svc.scored["status"] == "National Highways road, not scored")]
            nh_ok = True
            note = ""
            if len(nh):
                r = nh.iloc[0]
                nh_ok = svc.locate(float(r["lat"]), float(r["lon"]))["status"] == "national_highways"
                note = f"; {r['name']} -> National Highways, not scored"
            return out["status"] == "out_of_coverage" and nh_ok, f"London -> {out['status']}{note}"
        step("F1 outside coverage / National Highways", f1)

        def f2():
            f = P.freshness(cfg, svc.forecast_meta, forecast_as_of="2026-01-01T00:00:00+00:00")
            return f["stale"] and f["provisional"], f["message"] + " (serve with --forecast-as-of to show it)"
        step("F2 stale forecast banner", f2)

        def f3():
            low = svc.scored[svc.scored["scored"] & (svc.scored["traffic_note"] == "modelled estimate, low confidence")]
            return len(low) > 0, f"{len(low):,} segments show 'modelled estimate, low confidence'"
        step("F3 no nearby traffic count", f3)

        def f4():
            srcs = svc.sources()
            cached = [s for s in srcs if "cached, retrieved" in str(s.get("label", ""))]
            return len(cached) > 0, ", ".join(f"{s['name']}: {s['label']}" for s in cached)
        step("F4 cached copy labelled with retrieval date", f4)
    finally:
        tl = tmp / "token_log.jsonl"  # keep real token counts for reports/cost.md
        if tl.exists():
            real = I.resolve(I.load_config(), "state_dir")
            real.mkdir(parents=True, exist_ok=True)
            with open(real / "token_log.jsonl", "a", encoding="utf-8") as f:
                f.write(tl.read_text(encoding="utf-8"))
        shutil.rmtree(tmp, ignore_errors=True)

    out = I.resolve(I.load_config(), "state_dir")
    out.mkdir(parents=True, exist_ok=True)
    L = ["# Demo dry run", "", f"_{datetime.now(timezone.utc).isoformat(timespec='seconds')}, `python -m roadsense.api.demo_check`_", "",
         "| Step | Result | Notes |", "| --- | --- | --- |"]
    L += [f"| {n} | {'PASS' if ok else 'FAIL'} | {note.replace('|', '/')} |" for n, ok, note in results]
    (out / "demo_check.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"{sum(ok for _, ok, _ in results)}/{len(results)} passed; wrote {out / 'demo_check.md'}")
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-gemini", action="store_true", help="triage with Gemini (uses GEMINI_API_KEY and logs tokens)")
    run(live_gemini=ap.parse_args().live_gemini)
