"""Cached report triage for a map-only second city (Brighton).

    python -m roadsense.agent.city_triage Brighton [--limit 30]

Takes the city's currently open FixMyStreet reports (status open and reported within
triage.open_report_days, the same window as Southampton, so years-old "open" reports are left out),
at most `limit` of the most recent. Titles are redacted on load (names, house numbers, postcodes)
before anything is shown, cached or sent to Gemini. Each report is classified (Gemini over the same
model fallback list as Southampton when GEMINI_API_KEY is set, else the keyword rules), snapped to
the city's nearest segment within triage.snap_radius_m, merged with an earlier report within the
duplicate radius and window, and given that segment's real-label risk rank. Results are written to
outputs/lane_c/triage_<city>.json so the demo never waits on live Gemini.

No work orders: the city has no plan, wear or consequence data, so its triage is read-only.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.agent import triage as TR
from roadsense.prevent import inputs as I

CATEGORIES = ("pothole", "streetlight", "other")
PROMPT = ("Classify this public FixMyStreet report for a UK council highways team. Answer with one word: "
          "pothole (a defect in the road surface), streetlight, or other (pavement, kerb, drain, unclear).\n\nReport: {title}")


def open_reports(cfg: dict, city: str, root: Path | None = None, now=None) -> pd.DataFrame:
    """Open reports within the open_report_days window, most recent first (titles already redacted)."""
    f = I.load_fms(cfg, root, city=city)
    cutoff = pd.Timestamp(now or datetime.now(timezone.utc)).tz_localize(None) - pd.Timedelta(days=cfg["triage"]["open_report_days"])
    return f[(f["status"] == "open") & (f["requested"] >= cutoff)].sort_values("requested", ascending=False)


def classify(title: str, cfg: dict, client=None) -> tuple[str, str, str | None]:
    """(category, method, model). Gemini when a client is given and answers sensibly, else keyword rules."""
    if client is not None:
        try:
            resp, model = TR._generate(client, cfg, contents=PROMPT.format(title=title))
            word = (getattr(resp, "text", "") or "").strip().lower().split()
            word = word[0].strip(".,:;\"'") if word else ""
            if word in CATEGORIES:
                return word, "gemini", model
        except Exception:
            pass
    return TR.keyword_classify(title), "keyword", None


def run(cfg: dict | None = None, city: str = "Brighton", root: Path | None = None, limit: int = 30, client=None,
        now=None, verbose: bool = True, cache: dict | None = None) -> dict:
    """cache: {fms_id: (category, method, model)} from an earlier run; those reports are not re-classified."""
    from roadsense.api.cities import CityView
    cfg = cfg or I.load_config()
    t = cfg["triage"]
    view = CityView(cfg, city, root)
    seg = view.df.copy()
    idx = I.SegmentIndex(seg)
    reps = open_reports(cfg, city, root, now=now).head(limit)
    dup_m, dup_days = t.get("duplicate_radius_m", 15), t.get("duplicate_days", 14)
    out = []
    for r in reps.sort_values("requested").itertuples():          # oldest first, so duplicates merge into the earlier one
        cat, method, model = cache[int(r.id)] if cache and int(r.id) in cache else classify(str(r.title), cfg, client)
        j, d = idx.nearest(float(r.lon), float(r.lat))
        on_road = j is not None and d <= t["snap_radius_m"]
        s = seg.iloc[j] if on_road else None
        merged = None
        for q in out:
            if q["merged_into"] is None and abs((pd.Timestamp(q["reported_at"]) - r.requested).days) <= dup_days and \
                    float(I.distance_m(q["lon"], q["lat"], float(r.lon), float(r.lat))) <= dup_m:
                merged = q["report_id"]
                break
        out.append({
            "report_id": f"fms-{int(r.id)}", "fms_id": int(r.id), "city": city,
            "reported_at": r.requested.strftime("%Y-%m-%d"), "lat": float(r.lat), "lon": float(r.lon),
            "title": str(r.title), "category": cat, "method": method, "model": model,
            "segment_id": s["segment_id"] if s is not None else None, "segment_name": s["name"] if s is not None else None,
            "ward": s["ward"] if s is not None else None, "snap_m": round(float(d), 1) if on_road else None,
            "risk_band": s["band"] if s is not None else None,
            "urgency": float(s["urgency"]) if s is not None and np.isfinite(s["urgency"]) else None,
            "merged_into": merged,
            "status": "merged" if merged else ("not on a council road" if not on_road else "triaged"),
        })
    out = sorted(out, key=lambda q: q["reported_at"], reverse=True)
    res = {"city": city, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "window_days": t["open_report_days"], "limit": limit, "count": len(out),
           "methods": sorted({q["method"] for q in out}), "source": "fixmystreet",
           "retrieved_at": reps["retrieved_at"].iloc[0] if len(reps) else None,
           "note": "Read-only triage: no plan or work orders for this city (map only).", "reports": out}
    od = I.resolve(cfg, "outputs_dir", root) / "lane_c"
    od.mkdir(parents=True, exist_ok=True)
    path = od / f"triage_{city.lower()}.json"
    path.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    if verbose:
        print(f"{city}: {len(out)} open reports in the last {t['open_report_days']} days triaged "
              f"({', '.join(res['methods']) or 'none'}); wrote {I._rel(path, root)}")
    return res


def load_cached(cfg: dict, city: str, root: Path | None = None) -> dict | None:
    p = I.resolve(cfg, "outputs_dir", root) / "lane_c" / f"triage_{city.lower()}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("city", nargs="?", default="Brighton")
    ap.add_argument("--limit", type=int, default=30)
    a = ap.parse_args()
    run(city=a.city, limit=a.limit, client=TR.gemini_client())


if __name__ == "__main__":
    main()
