"""Lane C inputs: config, and loaders for the other lanes' outputs with labelled fallbacks.

Segments: lane A's data/processed/ (after the merge), then lane C's copy of lane A's real
Southampton files in roadsense/prevent/mock_data/ (optional, removed after Merge 1), then lane B's mock, then a small lane C mock.
Risk and lights: lane B's outputs when their segment ids match the segments in use, otherwise a
labelled mock in the contract's columns (docs/contract_outputs.md sections 7-8) on those segments.
Every frame carries where it came from so the API and page can show SYNTHETIC, MOCK and
"cached, retrieved <date>" labels. Nothing here invents real data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(__file__).with_name("prevent.yaml")
LOAD_CONFIDENCE = {"count": "high", "same_road": "medium", "class_median": "low"}

# local metric projection, same constants as lane B (good enough at city scale)
LAT0 = 50.92
M_PER_DEG_LAT = 110_540.0
M_PER_DEG_LON = 111_320.0 * np.cos(np.radians(LAT0))


LIVE_PATHS = ["processed_dir", "outputs_dir", "fms_csv"]   # what a daily refresh rebuilds (roadsense.refresh)


def load_config(path: Path | str | None = None, live: bool = True) -> dict:
    """App config. With no explicit path, if data/live/current points at a valid refresh build, the
    refreshed paths are redirected there; otherwise the committed snapshot is used."""
    with open(path or CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if path is None and live:
        from roadsense.refresh.live import current_build
        build = current_build()
        if build is not None:
            for k in LIVE_PATHS:
                cfg["paths"][k] = str(build / cfg["paths"][k])
            cfg["live_build"] = build.name
    return cfg


def resolve(cfg: dict, key: str, root: Path | None = None) -> Path:
    return Path(root or ROOT) / cfg["paths"][key]


def to_xy(lon, lat):
    return (np.asarray(lon, float) * M_PER_DEG_LON, np.asarray(lat, float) * M_PER_DEG_LAT)


def _mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def _rel(p: Path, root: Path | None) -> str:
    try:
        return p.relative_to(Path(root or ROOT)).as_posix()
    except ValueError:
        try:
            return p.relative_to(ROOT).as_posix()
        except ValueError:
            return str(p)


def latest(folder: Path, prefix: str) -> Path | None:
    """`<prefix>_latest.parquet` if lane B wrote one, else the newest dated file."""
    if (folder / f"{prefix}_latest.parquet").exists():
        return folder / f"{prefix}_latest.parquet"
    files = sorted(p for p in folder.glob(f"{prefix}_*.parquet") if not p.stem.endswith("_latest"))
    return files[-1] if files else None


# --- segments ---------------------------------------------------------------------------------
def load_segments(cfg: dict, root: Path | None = None) -> tuple[pd.DataFrame, dict]:
    for d, kind in [(resolve(cfg, "processed_dir", root), "real"), (resolve(cfg, "lane_c_copy_dir", root), "real"),
                    (resolve(cfg, "lane_b_mock_dir", root), "mock")]:
        p = d / "segments.parquet"
        if not p.exists():
            continue
        seg = pd.read_parquet(p)
        if "city" in seg:
            seg = seg[seg["city"] == cfg["coverage"]["city"]]
        lp = d / "load.parquet"
        if lp.exists():
            load = pd.read_parquet(lp)
            keep = [c for c in ["segment_id", "esal_day", "hgv_bus_aadf", "load_source", "count_point_id", "count_year"]
                    if c in load]
            seg = seg.drop(columns=[c for c in keep if c != "segment_id" and c in seg]).merge(
                load[keep], on="segment_id", how="left")
        # cycle_route / near_school_hospital: columns on segments.parquet (agreed 17 Oct); fall back to the
        # earlier separate segment_context.parquet; a missing column means false
        ctx_cols = ["cycle_route", "near_school_hospital"]
        cp = d / "segment_context.parquet"
        if not all(c in seg for c in ctx_cols) and cp.exists():
            ctx = pd.read_parquet(cp)
            ctx = ctx[["segment_id"] + [c for c in ctx_cols if c in ctx and c not in seg]]
            seg = seg.merge(ctx, on="segment_id", how="left")
        for c in ctx_cols:
            seg[c] = seg[c].astype("boolean").fillna(False).astype(bool) if c in seg else False
        meta = {"name": "segments", "path": _rel(p, root), "kind": kind, "retrieved_at": _mtime(p),
                "load_path": _rel(lp, root) if lp.exists() else None}
        return _segment_defaults(seg), meta
    from roadsense.prevent.mock import mock_world
    return _segment_defaults(mock_world(cfg)["segments"]), {"name": "segments", "path": None, "kind": "mock",
                                                            "retrieved_at": None, "load_path": None}


def _segment_defaults(seg: pd.DataFrame) -> pd.DataFrame:
    seg = seg.reset_index(drop=True).copy()
    if "lat" not in seg or "lon" not in seg:
        mids = seg["geometry"].map(_midpoint)
        seg["lon"], seg["lat"] = [m[0] for m in mids], [m[1] for m in mids]
    for c, v in [("owner", "council"), ("bus_stop", False), ("junction", False), ("in_flood_area", False),
                 ("esal_day", np.nan), ("ward", None), ("ref", ""), ("bus_routes", 0), ("name", "")]:
        if c not in seg:
            seg[c] = v
    seg["name"] = seg["name"].fillna("")
    seg["ref"] = seg["ref"].fillna("")
    seg["name"] = display_names(seg)
    if "load_source" not in seg:
        seg["load_source"] = None
    seg["load_source"] = seg["load_source"].fillna("class_median")
    seg["load_confidence"] = seg["load_source"].map(LOAD_CONFIDENCE).fillna("low")
    if "input_source" not in seg:
        seg["input_source"] = "real"
    return seg


HIGHWAY_RANK = ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential"]


def display_names(seg: pd.DataFrame, max_hops: int = 6) -> pd.Series:
    """Road name for the page: OSM name, else ref, else for an unnamed slip road (*_link) the road it
    joins ("Slip road off A33"), walking along connected unnamed slip-road pieces; else
    "Unnamed slip road near <ward>"; other unnamed roads stay "Unnamed <highway> road"."""
    name = seg["name"].fillna("").astype(str)
    ref = seg["ref"].fillna("").astype(str) if "ref" in seg else pd.Series("", index=seg.index)
    hw = seg["highway"].fillna("").astype(str)
    base = name.where(name != "", ref)
    out = base.where(base != "", "Unnamed " + hw + " road")
    is_link = hw.str.endswith("_link")
    todo = seg.index[(base == "") & is_link]
    if len(todo) == 0:
        return out
    coords = seg["geometry"].map(_coords)
    ends = {i: [tuple(np.round(c[0], 6)), tuple(np.round(c[-1], 6))] if c else [] for i, c in coords.items()}
    node_segs: dict = {}
    for i, e in ends.items():
        for n in e:
            node_segs.setdefault(n, []).append(i)
    rank = {h: k for k, h in enumerate(HIGHWAY_RANK)}

    def joined_road(i):
        seen, frontier = {i}, [i]
        for _ in range(max_hops):
            found, nxt = [], []
            for j in frontier:
                for n in ends.get(j, []):
                    for k in node_segs.get(n, []):
                        if k in seen:
                            continue
                        seen.add(k)
                        if base[k] != "":
                            found.append(k)
                        elif is_link[k]:
                            nxt.append(k)
            if found:
                # prefer a road with a ref (A33), then the bigger road class
                k = min(found, key=lambda k: (ref[k] == "", rank.get(hw[k].replace("_link", ""), 99)))
                return ref[k] or name[k]
            frontier = nxt
            if not frontier:
                break
        return None

    for i in todo:
        road = joined_road(i)
        ward = seg.at[i, "ward"] if "ward" in seg and isinstance(seg.at[i, "ward"], str) else None
        out[i] = f"Slip road off {road}" if road else (f"Unnamed slip road near {ward}" if ward else "Unnamed slip road")
    return out


def _coords(geom) -> list[tuple[float, float]]:
    if geom is None:
        return []
    if not isinstance(geom, str):
        try:
            from shapely import wkb
            return [tuple(c[:2]) for c in wkb.loads(bytes(geom)).coords]
        except Exception:
            return []
    inner = geom[geom.index("(") + 1: geom.rindex(")")].replace("(", "").replace(")", "")
    return [tuple(map(float, p.split()[:2])) for p in inner.split(",") if p.strip()]


def _midpoint(geom) -> tuple[float, float]:
    c = _coords(geom)
    if not c:
        return (np.nan, np.nan)
    return tuple(np.array(c).mean(0))


# --- risk and lights --------------------------------------------------------------------------
def normalise_risk(r: pd.DataFrame) -> pd.DataFrame:
    """Contract section 7 (one row per segment per forecast week, `wear`, `uses_synthetic`) to one
    row per segment with p30_w0..p30_w3. Also accepts lane B's older wide file."""
    r = r.copy()
    if "horizon_week" in r:
        # schemas.md v2 numbers forecast weeks 1-4; older files used 0-3. Internally w0 = first week.
        r["horizon_week"] = r["horizon_week"].astype(int) - int(r["horizon_week"].min())
        piv = r.pivot_table(index="segment_id", columns="horizon_week", values="p30")
        fc = r.groupby("segment_id")["uses_forecast"].any() if "uses_forecast" in r else None
        fra = r["forecast_retrieved_at"].dropna().astype(str) if "forecast_retrieved_at" in r else pd.Series(dtype=str)
        r.attrs["forecast_retrieved_at"] = fra.max() if len(fra) else None
        # per-week weather provenance (schemas.md section 7); week 4 has no forecast_retrieved_at
        if "weather_basis" not in r:
            r["weather_basis"] = r["weather_source"] if "weather_source" in r else None
        meta = {c: r.pivot_table(index="segment_id", columns="horizon_week", values=c, aggfunc="first")
                for c in ["week_start", "weather_basis", "uses_forecast", "forecast_retrieved_at"]
                if c in r and r[c].notna().any()}
        r = r[r["horizon_week"] == r["horizon_week"].min()].copy()
        for k in piv.columns:
            r[f"p30_w{k}"] = r["segment_id"].map(piv[k])
            for c, pv in meta.items():
                if k in pv.columns:
                    r[f"{c}_w{k}"] = r["segment_id"].map(pv[k])
        if fc is not None:
            r["uses_forecast"] = r["segment_id"].map(fc)
    if "wear_pct" not in r and "wear" in r:
        r["wear_pct"] = r["wear"]
    if "uses_synthetic" in r:
        r["labels_synthetic"] = r["uses_synthetic"].astype(bool)
    if "trained_on" in r:
        # labels are synthetic unless the model was trained on real reports only (trained_on = "real");
        # uses_synthetic stays true when other inputs (e.g. history_source) are synthetic
        r["labels_synthetic"] = r["trained_on"].astype(str).str.strip().str.lower().ne("real")
        r["wear_synthetic"] = r["uses_synthetic"].astype(bool)
    for c, v in [("labels_synthetic", True), ("wear_synthetic", True), ("inputs_mock", False), ("history_source", None),
                 ("uses_synthetic", True), ("weather_source", "unknown"), ("weather_data_date", None)]:
        if c not in r:
            r[c] = v
    if "p30_w0" not in r:
        r["p30_w0"] = r["p30"]
    weeks = [c for c in ["p30_w0", "p30_w1", "p30_w2", "p30_w3"] if c in r]
    r["p30_trend"] = r[weeks[-1]] - r["p30_w0"]
    out = r.reset_index(drop=True)
    out.attrs = r.attrs
    return out


def _overlap(ids: pd.Series, seg: pd.DataFrame | None) -> float:
    return 1.0 if seg is None else float(ids.isin(seg["segment_id"]).mean() if len(ids) else 0)


def load_risk(cfg: dict, root: Path | None = None, seg: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    """Lane B's risk file when its segment ids match `seg`; otherwise a labelled mock on `seg`
    (lane B still runs on mock segments until lane A's files are merged)."""
    p = None
    for d in [resolve(cfg, "outputs_dir", root), resolve(cfg, "lane_c_copy_dir", root)]:
        p = latest(d, "risk")
        if p is None:
            continue
        r = normalise_risk(pd.read_parquet(p))
        if _overlap(r["segment_id"], seg) >= 0.5:
            kind = "mock" if bool(r["inputs_mock"].any()) else "synthetic"
            return r, {"name": "risk", "path": _rel(p, root), "kind": kind, "retrieved_at": _mtime(p),
                       "as_of": str(r["as_of"].iloc[0]) if "as_of" in r else None,
                       "forecast_retrieved_at": r.attrs.get("forecast_retrieved_at"),
                       "trained_on": str(r["trained_on"].iloc[0]) if "trained_on" in r else None,
                       "history_source": str(r["history_source"].dropna().iloc[0]) if r["history_source"].notna().any() else None,
                       "model_version": str(r["model_version"].iloc[0]) if "model_version" in r else None}
    from roadsense.prevent.mock import mock_risk, mock_world
    r = normalise_risk(mock_risk(seg, cfg) if seg is not None else mock_world(cfg)["risk"])
    return r, {"name": "risk", "path": None, "kind": "mock", "retrieved_at": None, "as_of": str(r["as_of"].iloc[0]),
               "note": "lane B's risk file uses other segment ids" if p is not None else "no lane B risk file"}


def normalise_lights(l: pd.DataFrame) -> pd.DataFrame:
    """Contract section 8 names (lamp_type, replace_by, dark_source); accepts lane B's older names."""
    l = l.copy()
    for new, old in [("lamp_type", "unit_type"), ("replace_by", "predicted_replacement_date")]:
        if new not in l and old in l:
            l[new] = l[old]
    for c, v in [("dark_source", None), ("uses_synthetic", True), ("replacement_overdue", False), ("dark_since", None),
                 ("as_of", None), ("source", None)]:
        if c not in l:
            l[c] = v
    l["dark_source"] = l["dark_source"].replace("", None)
    if "due_90d" not in l:
        l["due_90d"] = pd.to_datetime(l["replace_by"]) <= pd.to_datetime(l["as_of"]) + pd.Timedelta(days=90)
    return l


def load_lights(cfg: dict, root: Path | None = None, seg: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    p = None
    for d in [resolve(cfg, "outputs_dir", root), resolve(cfg, "lane_c_copy_dir", root)]:
        p = latest(d, "lights")
        if p is None:
            continue
        l = normalise_lights(pd.read_parquet(p))
        if _overlap(l["segment_id"], seg) >= 0.5:
            return l, {"name": "lights", "path": _rel(p, root), "kind": "synthetic", "retrieved_at": _mtime(p),
                       "as_of": str(l["as_of"].iloc[0]) if len(l) else None}
    from roadsense.prevent.mock import mock_lights, mock_world
    l = normalise_lights(mock_lights(seg) if seg is not None else mock_world(cfg)["lights"])
    return l, {"name": "lights", "path": None, "kind": "mock", "retrieved_at": None, "as_of": str(l["as_of"].iloc[0])}


def load_forecast_meta(cfg: dict, root: Path | None = None) -> dict:
    """When the weather forecast was retrieved. Lane A writes weather_forecast_weekly.parquet with
    a retrieved_at column; until then the risk file's weeks 1-3 are climatology (no forecast)."""
    for p in [resolve(cfg, "processed_dir", root) / "weather_forecast_weekly.parquet",
              resolve(cfg, "processed_dir", root) / "weather_weekly.parquet",
              resolve(cfg, "lane_c_copy_dir", root) / "weather_forecast_weekly.parquet",
              resolve(cfg, "lane_c_copy_dir", root) / "weather_weekly.parquet"]:
        if not p.exists():
            continue
        w = pd.read_parquet(p)
        if "city" in w:  # lane A's weather files hold all three cities
            w = w[w["city"] == cfg["coverage"]["city"]]
        if "uses_forecast" in w:
            w = w[w["uses_forecast"].astype(bool)]
        if w.empty:
            continue
        ra = str(w["retrieved_at"].max()) if "retrieved_at" in w else _mtime(p)
        dd = str(w["data_date"].max()) if "data_date" in w else None
        return {"available": True, "retrieved_at": ra, "data_date": dd, "path": _rel(p, root),
                "source": str(w["source"].iloc[0]) if "source" in w else "open-meteo"}
    return {"available": False, "retrieved_at": None, "path": None}


def load_fms(cfg: dict, root: Path | None = None, city: str = "Southampton", all_reports: bool = False) -> pd.DataFrame:
    """Real FixMyStreet reports (public). Location, date, category, title and status only."""
    p = resolve(cfg, "fms_all_csv" if all_reports else "fms_csv", root)
    if not p.exists():
        return pd.DataFrame(columns=["id", "category", "requested", "status", "lat", "lon", "title", "source", "retrieved_at"])
    d = pd.read_csv(p)
    d = d[d["city"] == city].copy()
    d["requested"] = pd.to_datetime(d["requested"], utc=True).dt.tz_localize(None)
    from roadsense.prevent.redact_titles import redact
    d["title"] = d["title"].map(redact)  # never show house numbers, postcodes or names, even from older files
    d["source"] = "fixmystreet"
    d["retrieved_at"] = _mtime(p)
    return d[["id", "category", "requested", "status", "lat", "lon", "title", "source", "retrieved_at"]]


def synthetic_meta(cfg: dict, root: Path | None = None) -> dict | None:
    for d in [resolve(cfg, "synthetic_dir", root), resolve(cfg, "lane_c_copy_dir", root)]:
        if (d / "_meta.json").exists():
            return json.loads((d / "_meta.json").read_text(encoding="utf-8"))
    return None


def crew_outcomes_path(cfg: dict, root: Path | None = None) -> Path | None:
    for d in [resolve(cfg, "synthetic_dir", root), resolve(cfg, "lane_c_copy_dir", root)]:
        if (d / "crew_outcomes.csv").exists():
            return d / "crew_outcomes.csv"
    return None


# --- spatial helpers ----------------------------------------------------------------------------
class SegmentIndex:
    """Nearest-segment lookup on segment polylines (point-to-line distance in metres)."""

    def __init__(self, seg: pd.DataFrame):
        self.seg = seg.reset_index(drop=True)
        a, b, idx = [], [], []
        for i, g in enumerate(self.seg["geometry"]):
            c = _coords(g)
            for p, q in zip(c[:-1], c[1:]):
                a.append(p), b.append(q), idx.append(i)
        if not a:  # no geometry: fall back to midpoints
            mx, my = to_xy(self.seg["lon"], self.seg["lat"])
            self.ax, self.ay, self.bx, self.by = mx, my, mx, my
            self.idx = np.arange(len(self.seg))
            return
        a, b = np.array(a), np.array(b)
        self.ax, self.ay = to_xy(a[:, 0], a[:, 1])
        self.bx, self.by = to_xy(b[:, 0], b[:, 1])
        self.idx = np.array(idx)

    def nearest(self, lon: float, lat: float, mask: np.ndarray | None = None) -> tuple[int | None, float]:
        px, py = to_xy(lon, lat)
        dx, dy = self.bx - self.ax, self.by - self.ay
        L2 = dx * dx + dy * dy
        t = np.where(L2 > 0, ((px - self.ax) * dx + (py - self.ay) * dy) / np.where(L2 > 0, L2, 1), 0).clip(0, 1)
        d = np.hypot(self.ax + t * dx - px, self.ay + t * dy - py)
        if mask is not None:
            d = np.where(mask[self.idx], d, np.inf)
        if not len(d) or not np.isfinite(d.min()):
            return None, float("inf")
        j = int(d.argmin())
        return int(self.idx[j]), float(d[j])


def distance_m(lon1, lat1, lon2, lat2) -> np.ndarray:
    x1, y1 = to_xy(lon1, lat1)
    x2, y2 = to_xy(lon2, lat2)
    return np.hypot(x1 - x2, y1 - y2)


def _processed_or_copy(cfg: dict, root: Path | None, name: str) -> Path | None:
    for d in [resolve(cfg, "processed_dir", root), resolve(cfg, "lane_c_copy_dir", root)]:
        if (d / name).exists():
            return d / name
    return None


def load_wards(cfg: dict, root: Path | None = None):
    """Ward polygons (lane A data/processed/wards.geojson, MapIt/ONS, OGL) as shapely geometries, or None."""
    p = _processed_or_copy(cfg, root, "wards.geojson")
    if p is None:
        return None
    from shapely.geometry import shape
    from shapely.ops import unary_union
    g = json.loads(p.read_text(encoding="utf-8"))
    feats = [f for f in g["features"] if f["properties"].get("city", cfg["coverage"]["city"]) == cfg["coverage"]["city"]]
    if not feats:
        return None
    return {"union": unary_union([shape(f["geometry"]) for f in feats]), "n": len(feats), "path": _rel(p, root)}


def load_sources_manifest(cfg: dict, root: Path | None = None) -> list[dict] | None:
    """Lane A's data/processed/sources.json: [{name, label, used_for, licence, retrieved_at, data_date, is_synthetic}]."""
    p = _processed_or_copy(cfg, root, "sources.json")
    return json.loads(p.read_text(encoding="utf-8")) if p is not None else None


def in_coverage(lon: float, lat: float, cfg: dict, wards: dict | None = None) -> bool:
    """Inside a Southampton ward polygon when wards are available, else inside the city bbox."""
    if wards is not None:
        from shapely.geometry import Point
        return bool(wards["union"].covers(Point(lon, lat)))
    lon0, lat0, lon1, lat1 = cfg["coverage"]["bbox"]
    return lon0 <= lon <= lon1 and lat0 <= lat <= lat1


def model_label(trained_on: str | None, history_source: str | None) -> str:
    """Plain-English provenance of the risk scores, for the header, footer and plan rows."""
    t = (trained_on or "synthetic").strip().lower()
    label = {"real": "trained on real reports", "synthetic+real": "trained on real and synthetic reports"}.get(
        t, "trained on synthetic labels")
    if history_source:
        label += " · history synthetic" if "synthetic" in str(history_source).lower() else " · history real"
    return label
