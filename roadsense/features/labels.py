"""
M2: snap FixMyStreet road-defect reports to segments and build the segment x year label table
(schemas.md sections 1 and 5). Labels cover ALL road classes; is_main_road is carried as a feature.

    python -m roadsense.features.labels       # every city that has rows in segments.parquet

Outputs:
    data/processed/fms_snapped.csv            fms_roads_clean.csv + segment_id (blank beyond labels.snap_m) + snap_dist_m
    data/processed/labels_segment_year.csv    city, segment_id, is_main_road, year, n_reports, had_report, split
"""
import os

import geopandas as gpd
import pandas as pd
import shapely

from roadsense.ingest.fixmystreet import ROOT, load_config

BNG = "EPSG:27700"


def split_for(window, year, frozen_year):
    if year == frozen_year:
        return "frozen_2026" if frozen_year == 2026 else f"frozen_{frozen_year}"
    for name in ("train", "test"):
        rng = window.get(name)
        if rng and rng[0] <= year <= rng[1]:
            return name
    return "excluded"


def snap(cfg, seg, reports):
    """Nearest segment to each report (EPSG:27700); segment_id blank if further than snap_m."""
    g = gpd.GeoDataFrame(seg[["segment_id", "city"]], geometry=shapely.from_wkb(seg.geometry), crs=4326).to_crs(BNG)
    pts = gpd.GeoDataFrame(reports, geometry=gpd.points_from_xy(reports.lon, reports.lat), crs=4326).to_crs(BNG)
    out = []
    for city, p in pts.groupby("city"):
        j = gpd.sjoin_nearest(p, g[g.city == city][["segment_id", "geometry"]], distance_col="snap_dist_m")
        out.append(j[~j.index.duplicated()])   # equidistant ties: keep the first
    j = pd.DataFrame(pd.concat(out).drop(columns=["geometry", "index_right"]))
    j["snap_dist_m"] = j.snap_dist_m.round(1)
    j.loc[j.snap_dist_m > cfg["labels"]["snap_m"], "segment_id"] = ""
    return j.sort_index()


def labels(cfg, seg, snapped):
    frozen = cfg["labels"]["frozen_year"]
    snapped = snapped[snapped.segment_id != ""].assign(year=snapped.requested.str[:4].astype(int))
    counts = snapped.groupby(["segment_id", "year"]).size()
    tables = []
    for city, s in seg.groupby("city"):
        win = cfg["labels"]["windows"][city]
        years = list(range(win["usable"][0], win["usable"][1] + 1)) + [frozen]
        t = pd.DataFrame([(sid, main, y) for sid, main in zip(s.segment_id, s.is_main_road) for y in years],
                         columns=["segment_id", "is_main_road", "year"])
        t.insert(0, "city", city)
        t["n_reports"] = [counts.get((sid, y), 0) for sid, y in zip(t.segment_id, t.year)]
        t["had_report"] = (t.n_reports > 0).astype(int)
        t["split"] = [split_for(win, y, frozen) for y in t.year]
        tables.append(t)
    return pd.concat(tables, ignore_index=True)


def main():
    cfg = load_config()
    proc = os.path.join(ROOT, cfg["paths"]["processed_dir"])
    seg = pd.read_parquet(os.path.join(proc, "segments.parquet"))
    rep = pd.read_csv(os.path.join(proc, "fms_roads_clean.csv"))
    rep = rep[rep.city.isin(seg.city.unique())].reset_index(drop=True)

    snapped = snap(cfg, seg, rep)
    snapped.to_csv(os.path.join(proc, "fms_snapped.csv"), index=False)
    lab = labels(cfg, seg, snapped)
    lab.to_csv(os.path.join(proc, "labels_segment_year.csv"), index=False)

    snap_m = cfg["labels"]["snap_m"]
    for city, s in snapped.groupby("city"):
        w = s[s.in_window]
        print(f"{city}: {len(s)} reports, {(s.segment_id != '').mean():.0%} within {snap_m} m "
              f"(in window: {len(w)}, {(w.segment_id != '').mean():.0%})")
    summary = (lab.groupby(["city", "split", "year"])
               .agg(segments=("segment_id", "size"), reports=("n_reports", "sum"),
                    pos_rate=("had_report", "mean"),
                    main_road_share=("had_report", lambda h: lab.loc[h.index].query("had_report == 1").is_main_road.mean()))
               .round(3))
    print(summary.to_string())


if __name__ == "__main__":
    main()
