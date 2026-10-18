"""
Link building sites and utility works to road segments (Southampton and Brighton).

    python -m roadsense.features.works_context

Inputs (real, OGL):  data/processed/construction_sites.csv  (roadsense.ingest.construction)
                     data/processed/street_works.csv        (roadsense.ingest.streetworks)
Outputs:             data/processed/site_segments.csv   one row per (site, segment) with the segment within SITE_M
                                                         of the site point: city, reference, segment_id, dist_m
                     data/processed/works_segments.parquet  each completed work with the segment its midpoint lies on
                                                         (nearest within WORK_M), or blank

Rules (ASSUMPTIONS, not council figures):
- a building site loads the roads within SITE_M = 150 m of its register point (site access and the first turns
  lorries take); the register gives a point, not the site boundary or the access road.
- construction runs for up to CON_YEARS = 3 years after planning permission (permission lasts 3 years in England).
- a works midpoint within WORK_M = 20 m of a segment is a dig on that segment. "Carriageway" works are those whose
  Street Manager works_location_type includes Carriageway: a footway-only dig does not open the road surface.

`yearly(...)` gives the per segment-year features the held-out check uses (roadsense.models.context_eval); each row
sees only earlier years; real_risk.build_table adds them to the risk model's features (real_risk.CONTEXT).
`current(...)` gives the flags the officer page shows for what is near the road now.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROC = os.path.join(ROOT, "data", "processed")
SITE_M, WORK_M, CON_YEARS = 150.0, 20.0, 3
FIRST_FULL_WORKS_YEAR = 2021        # Street Manager archive starts July 2020
LINKED_COLS = ["city", "permit_reference", "promoter", "activity_type", "work_category", "works_location_type",
               "street_name", "actual_end", "year", "carriageway", "segment_id", "snap_m", "source_month"]
YEARLY = ["con_sites_3y", "con_dwellings_3y", "uw_carriageway_1y", "uw_any_1y", "uw_carriageway_2y"]


def _geoms(seg: pd.DataFrame):
    import shapely
    from shapely import wkb, wkt
    from roadsense.synthetic.common import to_xy

    def proj(c):
        return np.c_[to_xy(c[:, 0], c[:, 1])]
    return [shapely.transform(wkb.loads(g) if isinstance(g, (bytes, bytearray)) else wkt.loads(g), proj) for g in seg["geometry"]]


def _points(df: pd.DataFrame):
    from shapely.geometry import Point
    from roadsense.synthetic.common import to_xy
    x, y = to_xy(df["lon"].astype(float).values, df["lat"].astype(float).values)
    return [Point(a, b) for a, b in zip(x, y)]


def link_sites(seg: pd.DataFrame, sites: pd.DataFrame, radius_m: float = SITE_M) -> pd.DataFrame:
    from shapely import STRtree
    out = []
    for city, s in sites.groupby("city"):
        g = seg[seg["city"] == city].reset_index(drop=True)
        if g.empty:
            continue
        geoms = _geoms(g)
        tree = STRtree(geoms)
        pts = _points(s)
        for (_, r), p in zip(s.iterrows(), pts):
            for j in tree.query(p.buffer(radius_m), predicate="intersects"):
                out.append({"city": city, "reference": r["reference"], "segment_id": g["segment_id"].iat[j],
                            "dist_m": round(float(geoms[j].distance(p)), 1)})
    return pd.DataFrame(out, columns=["city", "reference", "segment_id", "dist_m"])


def snap_works(seg: pd.DataFrame, works: pd.DataFrame, radius_m: float = WORK_M) -> pd.DataFrame:
    from shapely import STRtree
    w = works.copy()
    w["segment_id"], w["snap_m"] = None, np.nan
    for city, g in w.groupby("city"):
        s = seg[seg["city"] == city].reset_index(drop=True)
        if s.empty:
            continue
        tree = STRtree(_geoms(s))
        idx, dist = tree.query_nearest(_points(g), max_distance=radius_m, return_distance=True, all_matches=False)
        w.loc[g.index[idx[0]], "segment_id"] = s["segment_id"].values[idx[1]]
        w.loc[g.index[idx[0]], "snap_m"] = np.round(dist, 1)
    w["carriageway"] = w["works_location_type"].fillna("").str.contains("Carriageway")
    w["year"] = pd.to_datetime(w["actual_end"], utc=True, format="mixed", errors="coerce").dt.year
    return w


def load_linked(root: str = ROOT, processed_dir: str | None = None) -> tuple[pd.DataFrame | None, pd.DataFrame | None, pd.DataFrame | None]:
    """(sites, site_segments, works_segments), each None when missing. processed_dir overrides <root>/data/processed
    (the app passes the live refresh build's folder)."""
    p = processed_dir or os.path.join(root, "data", "processed")
    rd = lambda n: pd.read_csv(os.path.join(p, n), dtype={"segment_id": str}) if os.path.exists(os.path.join(p, n)) else None
    wp = os.path.join(p, "works_segments.parquet")
    works = pd.read_parquet(wp) if os.path.exists(wp) else None
    return rd("construction_sites.csv"), rd("site_segments.csv"), works


def yearly(rows: pd.DataFrame, sites, links, works) -> pd.DataFrame:
    """Add YEARLY columns to segment-year `rows` (segment_id, city, year). Only years before `year` are used.
    A city with no register, or a year whose look-back is before the first full Street Manager year, gets NaN."""
    out = rows[["segment_id", "city", "year"]].copy()
    for c in YEARLY:
        out[c] = np.nan
    if sites is not None and links is not None and len(links):
        reg = sites[sites["source"] == "brownfield_register"] if "source" in sites else sites
        s = reg.assign(py=pd.to_datetime(reg["permission_date"], errors="coerce").dt.year)
        L = links.merge(s[["city", "reference", "py", "max_net_dwellings"]], on=["city", "reference"])
        L = L[L["py"].notna()]
        for y in sorted(out["year"].unique()):
            act = L[(L["py"] >= y - CON_YEARS) & (L["py"] <= y - 1)]
            n = act.groupby("segment_id")["reference"].nunique()
            d = act.groupby("segment_id")["max_net_dwellings"].sum()
            m = (out["year"] == y) & out["city"].isin(reg["city"].unique())
            out.loc[m, "con_sites_3y"] = out.loc[m, "segment_id"].map(n).fillna(0).values
            out.loc[m, "con_dwellings_3y"] = out.loc[m, "segment_id"].map(d).fillna(0).values
    if works is not None and len(works):
        w = works[works["segment_id"].notna()]
        cities = works["city"].unique()
        for y in sorted(out["year"].unique()):
            m = (out["year"] == y) & out["city"].isin(cities)
            if y - 1 >= FIRST_FULL_WORKS_YEAR:
                prev = w[w["year"] == y - 1]
                out.loc[m, "uw_carriageway_1y"] = out.loc[m, "segment_id"].map(prev[prev["carriageway"]].groupby("segment_id").size()).fillna(0).values
                out.loc[m, "uw_any_1y"] = out.loc[m, "segment_id"].map(prev.groupby("segment_id").size()).fillna(0).values
            if y - 2 >= FIRST_FULL_WORKS_YEAR:
                prev2 = w[w["year"].between(y - 2, y - 1) & w["carriageway"]]
                out.loc[m, "uw_carriageway_2y"] = out.loc[m, "segment_id"].map(prev2.groupby("segment_id").size()).fillna(0).values
    return out


def _live(sites: pd.DataFrame, city: str, t: pd.Timestamp) -> pd.DataFrame:
    """Sites building now: register sites permitted in the CON_YEARS before t and still on the register, plus every
    OpenStreetMap landuse=construction site (mapped as active when the data was read)."""
    s = sites[sites["city"] == city].copy()
    src = s["source"] if "source" in s else pd.Series("brownfield_register", index=s.index)
    s["pdate"] = pd.to_datetime(s["permission_date"], errors="coerce")
    reg = (src == "brownfield_register") & (s["pdate"] > t - pd.DateOffset(years=CON_YEARS)) & (s["pdate"] <= t) & s["end_date"].isna()
    return s[reg | (src == "osm_construction")].assign(source=src)


def _site(r) -> dict:
    def num(v, f):
        return None if v is None or (isinstance(v, float) and np.isnan(v)) else f(v)
    return {"source": r.source, "reference": r.reference,
            "address": r.site_address if isinstance(r.site_address, str) else r.name,
            "dwellings": num(r.max_net_dwellings, int), "hectares": num(r.hectares, float),
            "permission_date": str(r.permission_date)[:10] if isinstance(r.permission_date, str) else None,
            "kind": r.permission_type if isinstance(r.permission_type, str) else None,
            "public_land": bool(r.public_land) if isinstance(r.public_land, (bool, np.bool_)) else str(r.public_land) == "True"}


def current(city: str, as_of, sites, links, works, works_months: int = 12) -> dict:
    """{segment_id: {"construction": [...], "works": {...}}} as of `as_of` (display only)."""
    t = pd.Timestamp(as_of)
    out: dict = {}
    if sites is not None and links is not None and len(links):
        L = links[links["city"] == city].merge(_live(sites, city, t), on=["city", "reference"])
        for sid, g in L.sort_values(["max_net_dwellings", "dist_m"], ascending=[False, True]).groupby("segment_id"):
            out.setdefault(sid, {})["construction"] = {
                "count": int(len(g)), "sites": [{**_site(r), "dist_m": float(r.dist_m)} for r in g.head(3).itertuples()]}
    if works is not None and len(works):
        w = works[(works["city"] == city) & works["segment_id"].notna()].copy()
        w["end"] = pd.to_datetime(w["actual_end"], utc=True, format="mixed", errors="coerce").dt.tz_localize(None)
        w = w[(w["end"] > t - pd.DateOffset(months=works_months)) & (w["end"] <= t + pd.Timedelta(days=1))]
        for sid, g in w.groupby("segment_id"):
            cw = g[g["carriageway"]]
            out.setdefault(sid, {})["works"] = {
                "count": int(len(g)), "carriageway": int(len(cw)), "months": works_months,
                "last": str(g["end"].max().date()),
                "promoters": g["promoter"].value_counts().head(3).index.tolist(),
                "activities": g["activity_type"].value_counts().head(3).index.tolist()}
    return out


def live_sites(city: str, as_of, sites) -> list[dict]:
    """Sites building now in `city` (map layer); see _live."""
    if sites is None:
        return []
    return [{**_site(r), "lon": float(r.lon), "lat": float(r.lat)} for r in _live(sites, city, pd.Timestamp(as_of)).itertuples()]


def relink_sites(proc_dir: str = PROC) -> int:
    """site_segments.csv from construction_sites.csv (cheap: a few hundred sites)."""
    seg = pd.read_parquet(os.path.join(proc_dir, "segments.parquet"), columns=["segment_id", "city", "geometry", "owner", "highway"])
    seg = seg[(seg["owner"] != "national_highways") & (seg["highway"] != "motorway")]
    sites, _, _ = load_linked(processed_dir=proc_dir)
    if sites is None:
        return 0
    L = link_sites(seg, sites)
    L.to_csv(os.path.join(proc_dir, "site_segments.csv"), index=False)
    return len(L)


def main():
    seg = pd.read_parquet(os.path.join(PROC, "segments.parquet"), columns=["segment_id", "city", "geometry", "owner", "highway"])
    seg = seg[(seg["owner"] != "national_highways") & (seg["highway"] != "motorway")]
    n = relink_sites()
    print(f"site_segments: {n} links")
    wp = os.path.join(PROC, "street_works.csv")
    if os.path.exists(wp):
        w = snap_works(seg, pd.read_csv(wp, dtype=str))
        w = w[LINKED_COLS].astype({"year": "Int64", "carriageway": bool})
        w.to_parquet(os.path.join(PROC, "works_segments.parquet"), index=False)
        print(f"works_segments: {len(w)} works, {w['segment_id'].notna().mean():.0%} on a council segment within {WORK_M:.0f} m")


if __name__ == "__main__":
    main()
