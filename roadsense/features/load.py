"""
M3a: traffic load per segment from DfT road traffic statistics (OGL) -> data/processed/load.parquet
(schemas.md section 3).

    python -m roadsense.features.load            # every city in segments.parquet; downloads to data/raw/dft/

For each count point, AADF is averaged over the city's train years (falling back to the nearest earlier
years, then any year). Each segment then takes a value from:
    count        a count point on the same road within load.count_m (A-roads by ref; unreferenced roads:
                 a Minor count point within load.minor_count_m)
    same_road    the nearest count point on the same road (by ref)
    class_median the median of segments of the same highway class that have one of the above, else of
                 the fallback class in load.class_fallback, else the median of Minor count points
"""
import os, urllib.request

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from roadsense.ingest.fixmystreet import ROOT, load_config

BNG = "EPSG:27700"


def download(cfg, city):
    la = cfg["load"]["dft_la"][city]
    d = os.path.join(ROOT, cfg["paths"]["raw_dir"], "dft")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"aadf_{la}.csv")
    if not os.path.exists(path):
        req = urllib.request.Request(cfg["load"]["dft_url"].format(la=la),
                                     headers={"User-Agent": cfg["fixmystreet"]["user_agent"]})
        with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
            f.write(r.read())
    return pd.read_csv(path)


def point_loads(cfg, city, aadf):
    """One row per count point: mean HGV+bus flow and ESAL/day over the train years."""
    lo, hi = cfg["labels"]["windows"][city]["train"]
    fac = cfg["load"]["esal_factors"]
    aadf = aadf.assign(hgv_bus=aadf.all_hgvs + aadf.buses_and_coaches,
                       esal=sum(aadf[c] * f for c, f in fac.items()))

    def pick(g):
        for sel in (g.year.between(lo, hi), g.year <= hi, g.year > hi):
            if sel.any():
                # fall back to the 5 years closest to the window
                yrs = g[sel].assign(gap=(g[sel].year - (lo + hi) / 2).abs()).nsmallest(5, "gap")
                return pd.Series({"hgv_bus_aadf": yrs.hgv_bus.mean(), "esal_day": yrs.esal.mean(),
                                  "count_year": int(yrs.year.max()), "road_name": g.road_name.iloc[0],
                                  "road_type": g.road_type.iloc[0],
                                  "easting": g.easting.iloc[0], "northing": g.northing.iloc[0]})
    pts = aadf.groupby("count_point_id").apply(pick, include_groups=False).reset_index()
    return gpd.GeoDataFrame(pts, geometry=gpd.points_from_xy(pts.easting, pts.northing), crs=BNG)


def join_city(cfg, seg, pts):
    lc = cfg["load"]
    g = gpd.GeoDataFrame(seg[["segment_id", "ref", "highway"]].reset_index(drop=True),
                         geometry=shapely.from_wkb(seg.geometry.values), crs=4326).to_crs(BNG)
    out = pd.DataFrame({"segment_id": g.segment_id, "count_point_id": pd.NA, "load_source": pd.NA,
                        "dist": np.inf})

    refs = g.ref.fillna("").str.split(" / ")
    for road in sorted(pts.road_name.unique()):
        on_road = refs.apply(lambda r: road in r)
        if not on_road.any():
            continue
        j = gpd.sjoin_nearest(g[on_road], pts[pts.road_name == road][["count_point_id", "geometry"]],
                              distance_col="d")
        j = j[~j.index.duplicated()]
        better = j.d < out.loc[j.index, "dist"]
        idx = j.index[better]
        out.loc[idx, "count_point_id"] = j.loc[idx, "count_point_id"]
        out.loc[idx, "dist"] = j.loc[idx, "d"]
        out.loc[idx, "load_source"] = np.where(j.loc[idx, "d"] <= lc["count_m"], "count", "same_road")

    no_ref = (refs.apply(lambda r: r == [""])) & out.load_source.isna()
    minor = pts[pts.road_type == "Minor"]
    if no_ref.any() and len(minor):
        j = gpd.sjoin_nearest(g[no_ref], minor[["count_point_id", "geometry"]],
                              max_distance=lc["minor_count_m"], distance_col="d")
        j = j[~j.index.duplicated()].dropna(subset=["count_point_id"])
        out.loc[j.index, "count_point_id"] = j.count_point_id
        out.loc[j.index, "load_source"] = "count"

    p = pts.set_index("count_point_id")
    has = out.count_point_id.notna()
    cp = out.loc[has, "count_point_id"].astype(int)
    for col in ("hgv_bus_aadf", "esal_day", "count_year"):
        out.loc[has, col] = p.loc[cp, col].values

    # class medians for the rest
    out["highway"] = g.highway
    med = out[has].groupby("highway")[["hgv_bus_aadf", "esal_day"]].median()
    minor_med = minor[["hgv_bus_aadf", "esal_day"]].median()
    fallback = lc.get("class_fallback", {})

    def class_value(h, col):
        for k in [h] + fallback.get(h, []):
            if k in med.index:
                return med.at[k, col]
        return minor_med[col]
    rest = ~has
    for col in ("hgv_bus_aadf", "esal_day"):
        out.loc[rest, col] = [class_value(h, col) for h in out.loc[rest, "highway"]]
    out.loc[rest, "load_source"] = "class_median"
    out["source"] = "dft"
    out["count_point_id"] = out.count_point_id.astype("Int64")
    out["count_year"] = out.count_year.astype("Int64")
    out[["hgv_bus_aadf", "esal_day"]] = out[["hgv_bus_aadf", "esal_day"]].astype(float).round(1)
    return out[["segment_id", "hgv_bus_aadf", "esal_day", "count_point_id", "count_year", "load_source", "source"]]


def main():
    cfg = load_config()
    proc = os.path.join(ROOT, cfg["paths"]["processed_dir"])
    seg = pd.read_parquet(os.path.join(proc, "segments.parquet"))
    parts = []
    for city, s in seg.groupby("city"):
        pts = point_loads(cfg, city, download(cfg, city))
        ld = join_city(cfg, s, pts)
        parts.append(ld)
        m = s.is_main_road.values
        print(f"{city}: {len(pts)} count points; load_source "
              f"{ld.load_source.value_counts().to_dict()}; main roads with count/same_road "
              f"{(ld.load_source[m] != 'class_median').mean():.0%}")
        print("  median hgv_bus_aadf by highway:",
              ld.assign(h=s.highway.values).groupby("h").hgv_bus_aadf.median().sort_values(ascending=False).head(8).to_dict())
    out = pd.concat(parts, ignore_index=True)
    out.to_parquet(os.path.join(proc, "load.parquet"), index=False)
    print(f"wrote load.parquet ({len(out)} rows)")


if __name__ == "__main__":
    main()
