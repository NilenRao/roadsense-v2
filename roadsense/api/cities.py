"""Map-only view of a second city (Brighton) from lane B's real-label risk (outputs/risk_<city>_latest.parquet).

The plan, lights and triage stay Southampton only. Segments get the same 0-100 urgency and bands as
Southampton (roadsense.prevent.urgency): P30 from the real-label model times the same consequence weights.
There is no resurfacing history (so no wear model) for the second city, so the wear term is left out and
P30 carries its weight: urgency = 100 * min(1, C * P30).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.prevent import inputs as I
from roadsense.prevent import urgency as U

CONSEQUENCE_COLS = ["bus_stop", "bus_routes", "cycle_route", "near_school_hospital"]


def available(cfg: dict, root: Path | None = None) -> list[str]:
    od = I.resolve(cfg, "outputs_dir", root)
    out = []
    for p in sorted(od.glob("risk_*_latest.parquet")):
        city = p.stem[len("risk_"):-len("_latest")]
        if city not in ("synthetic",):
            out.append(city.capitalize())
    return out


class CityView:
    def __init__(self, cfg: dict, city: str, root: Path | None = None):
        self.city = city
        od = I.resolve(cfg, "outputs_dir", root)
        p = od / f"risk_{city.lower()}_latest.parquet"
        if not p.exists():
            raise FileNotFoundError(p)
        raw = pd.read_parquet(p)
        self.meta = {"model_version": str(raw["model_version"].iloc[0]) if "model_version" in raw else None,
                     "trained_on": str(raw["trained_on"].iloc[0]) if "trained_on" in raw else None,
                     "uses_synthetic": bool(raw["uses_synthetic"].any()) if "uses_synthetic" in raw else True,
                     "history_source": str(raw["history_source"].iloc[0]) if "history_source" in raw else None,
                     "as_of": str(raw["as_of"].iloc[0]) if "as_of" in raw else None, "path": I._rel(p, root)}
        risk = I.normalise_risk(raw)
        seg = None
        for d in [I.resolve(cfg, "processed_dir", root), I.resolve(cfg, "lane_c_copy_dir", root)]:
            if (d / "segments.parquet").exists():
                s = pd.read_parquet(d / "segments.parquet")
                seg = s[s["city"] == city] if "city" in s else s
                break
        if seg is None:
            raise FileNotFoundError("segments.parquet")
        cols = [c for c in ["segment_id", "name", "ref", "highway", "ward", "geometry", "lat", "lon", "owner",
                            *CONSEQUENCE_COLS] if c in seg]
        df = seg[cols].merge(risk.drop(columns=[c for c in ["ward"] if c in risk]), on="segment_id", how="inner")
        df = df.reset_index(drop=True)
        df["name"] = I.display_names(df)
        df["consequence"], df["consequence_text"] = U.consequence(df, cfg)
        # Southampton's formula with no wear term: P30 takes the wear weight too (w_p30 + w_wear = 1)
        w = cfg["urgency"]["w_p30"] + cfg["urgency"]["w_wear"]
        df["urgency"] = np.round(100 * np.minimum(1, df["consequence"] * w * df["p30"].astype(float)), 1)
        df["band"] = U.band(df["urgency"], cfg)
        self.df = df.reset_index(drop=True)
        self._geom = {sid: [[round(x, 6), round(y, 6)] for x, y in I._coords(g)]
                      for sid, g in zip(self.df["segment_id"], self.df["geometry"])}

    @property
    def real_inputs(self) -> bool:
        return self.meta["trained_on"] == "real" and not self.meta["uses_synthetic"]

    def badge(self) -> str:
        return "100% real inputs" if self.real_inputs else "includes synthetic inputs"

    def geojson(self) -> dict:
        feats = []
        for r in self.df[["segment_id", "name", "ward", "highway", "band", "urgency"]].to_dict("records"):
            r = {k: (None if isinstance(v, float) and not np.isfinite(v) else v) for k, v in r.items()}
            feats.append({"type": "Feature", "properties": r,
                          "geometry": {"type": "LineString", "coordinates": self._geom.get(r["segment_id"], [])}})
        return {"type": "FeatureCollection", "features": feats,
                "meta": {"city": self.city, "count": len(feats), "badge": self.badge(), **self.meta,
                         "bands": {b: int((self.df["band"] == b).sum()) for b in U.BAND_ORDER}}}

    def segment(self, sid: str) -> dict | None:
        r = self.df[self.df["segment_id"] == sid]
        if r.empty:
            return None
        r = r.iloc[0]
        clean = lambda v: None if v is None or (isinstance(v, float) and not np.isfinite(v)) else (
            v.item() if hasattr(v, "item") else v)
        return {"city": self.city, "segment_id": sid, "name": r["name"], "ward": r.get("ward"), "highway": r["highway"],
                "urgency": clean(r["urgency"]), "band": r["band"], "p7": clean(r["p7"]), "p30": clean(r["p30"]),
                "consequence": clean(r["consequence"]), "consequence_text": r["consequence_text"], "wear_pct": None,
                "reasons": [x for x in [r.get("reason_1"), r.get("reason_2"), r.get("reason_3")] if isinstance(x, str)],
                "badge": self.badge(), **self.meta,
                "note": "Inspection list and read-only triage from real reports. Streetlights are Southampton only."}

    INSPECT_HEADER = ("{city} inspection list: ranked by urgency from real FixMyStreet reports. "
                      "No wear model for {city}, so treatments are chosen on inspection.")

    def inspection_list(self, open_reports: pd.DataFrame, cfg: dict, top_n: int = 50) -> dict:
        """Segments to inspect: any council segment with an open FixMyStreet report in the open window
        (first, marked "open report"), then the top_n by urgency. Urgency, band, P30 and reasons like the
        map card; no treatment or money (no wear or cost model for this city)."""
        from roadsense.prevent.inputs import SegmentIndex
        df = self.df
        owner = df["owner"] if "owner" in df else pd.Series("council", index=df.index)
        council = ((owner != "national_highways") & (df["highway"] != "motorway")).values
        reports: dict[str, list] = {}
        if open_reports is not None and len(open_reports):
            idx = SegmentIndex(df)
            for r in open_reports.itertuples():
                j, d = idx.nearest(float(r.lon), float(r.lat), mask=council)
                if j is not None and d <= cfg["triage"]["snap_radius_m"]:
                    reports.setdefault(df.iloc[j]["segment_id"], []).append(
                        {"id": int(r.id), "reported_at": str(r.reported_at)[:10], "title": str(r.title)})
        c = df[council]
        opened = c[c["segment_id"].isin(reports)].sort_values(["urgency", "p30"], ascending=False)
        rest = c[~c["segment_id"].isin(reports)].sort_values(["urgency", "p30"], ascending=False).head(top_n)
        rows = []
        for k, r in enumerate(pd.concat([opened, rest]).to_dict("records"), start=1):
            reps = sorted(reports.get(r["segment_id"], []), key=lambda x: x["reported_at"], reverse=True)
            rows.append({"rank": k, "section": "open" if reps else "risk",
                         "segment_id": r["segment_id"], "name": r["name"], "ward": r.get("ward"),
                         "highway": r["highway"], "urgency": float(r["urgency"]), "band": r["band"],
                         "p30": float(r["p30"]), "consequence_text": r["consequence_text"],
                         "reasons": [x for x in [r.get("reason_1"), r.get("reason_2"), r.get("reason_3")] if isinstance(x, str)],
                         "action": "Inspect", "open_report": bool(reps), "open_reports": reps,
                         "history_source": self.meta["history_source"], "uses_synthetic": bool(self.meta["uses_synthetic"])})
        sections = [{"key": "open", "label": "Open reports (inspect first)", "count": len(opened),
                     "note": "ranked by urgency within the section"},
                    {"key": "risk", "label": "Highest predicted risk", "count": len(rest), "note": f"top {top_n} by urgency"}]
        return {"city": self.city, "header": self.INSPECT_HEADER.format(city=self.city), "badge": self.badge(),
                "sections": sections,
                "count": len(rows), "open_report_rows": len(opened), "top_n": top_n,
                "window_days": cfg["triage"]["open_report_days"], **self.meta, "rows": rows}

