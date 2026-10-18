"""
Cut the OSM drive network into road segments (schemas.md section 2) -> data/processed/segments.parquet.

    python -m roadsense.ingest.osm Southampton          # download first (cached in data/raw/osm/)
    python -m roadsense.features.segments Southampton   # rebuilds that city's rows, keeps other cities

One row per piece of road <= segments.max_length_m long. Two-way roads appear once (undirected graph).
Geometry is WKB in EPSG:4326. All distances are worked out in EPSG:27700.
"""
import argparse, json, math, os

import geopandas as gpd
import numpy as np
import osmnx as ox
import pandas as pd
import shapely
from shapely.ops import substring

from roadsense.ingest.fixmystreet import ROOT, load_config

BNG = "EPSG:27700"


def first_or_all(v):
    return v if isinstance(v, list) else [v]


def pick_highway(v, rank):
    vals = first_or_all(v)
    return next((h for h in rank if h in vals), str(vals[0]))


def join_names(v):
    vals = [str(x) for x in first_or_all(v) if isinstance(x, str) and x]
    return " / ".join(dict.fromkeys(vals))


def split_line(line, max_len):
    n = max(1, math.ceil(line.length / max_len))
    step = line.length / n
    return [substring(line, i * step, (i + 1) * step) for i in range(n)]


def build_city(cfg, city):
    c, sc = cfg["cities"][city], cfg["segments"]
    d = os.path.join(ROOT, cfg["paths"]["raw_dir"], "osm")
    path = lambda name: os.path.join(d, f"{c['code']}_{name}")

    G = ox.convert.to_undirected(ox.load_graphml(path("drive.graphml")))
    # Junction node: >= junction_min_streets streets meet (crossroads), or a tagged junction node.
    # 3-way meets are left out: in the simplified graph every edge ends at one, so the flag would always be true.
    sc_node, hw_node = dict(G.nodes(data="street_count")), dict(G.nodes(data="highway"))
    is_junction = {n for n in G.nodes if sc_node.get(n, 0) >= sc["junction_min_streets"]
                   or hw_node.get(n) in sc["junction_node_tags"]}
    edges = ox.convert.graph_to_gdfs(G, nodes=False).reset_index().to_crs(BNG)
    with open(path("bus_routes.json")) as f:
        routes = {int(k): v for k, v in json.load(f).items()}

    rows = []
    for e in edges.itertuples():
        ways = sorted(int(w) for w in first_or_all(e.osmid))
        pieces = split_line(e.geometry, sc["max_length_m"])
        for i, g in enumerate(pieces):
            rows.append({
                "osm_way_id": ways[0],
                "name": join_names(getattr(e, "name", None)),
                "ref": join_names(getattr(e, "ref", None)),
                "highway": pick_highway(e.highway, sc["highway_rank"]),
                "bus_routes": max(routes.get(w, 0) for w in ways),
                # only the end pieces of an edge touch a junction node
                "junction": (i == 0 and e.u in is_junction) or (i == len(pieces) - 1 and e.v in is_junction),
                "_order": (ways[0], min(e.u, e.v), max(e.u, e.v), e.key, i),
                "geometry": g,
            })
    seg = gpd.GeoDataFrame(rows, crs=BNG)

    # Stable ids: number the pieces of each OSM way in a fixed order
    seg = seg.sort_values("_order").reset_index(drop=True)
    part = seg.groupby("osm_way_id").cumcount()
    seg["segment_id"] = [f"{c['code']}-{w}-{p:02d}" for w, p in zip(seg.osm_way_id, part)]
    seg["city"] = city
    seg["length_m"] = seg.geometry.length.round(1)
    seg["is_main_road"] = seg.highway.isin(sc["main_road_highways"])
    seg["owner"] = np.where(seg.highway.isin(sc["national_highways_highways"]), "national_highways", "council")

    mid = gpd.GeoDataFrame(geometry=seg.geometry.interpolate(0.5, normalized=True), crs=BNG)

    stops = gpd.read_file(path("bus_stops.gpkg")).to_crs(BNG)
    near = gpd.sjoin_nearest(gpd.GeoDataFrame(geometry=seg.geometry, crs=BNG), stops[["geometry"]],
                             max_distance=sc["bus_stop_m"], how="left")
    seg["bus_stop"] = near.groupby(level=0)["index_right"].apply(lambda s: s.notna().any())

    wards = gpd.read_file(path("wards.gpkg")).to_crs(BNG)
    wj = gpd.sjoin_nearest(mid, wards[["ward", "geometry"]], how="left")  # inside = distance 0
    seg["ward"] = wj.groupby(level=0)["ward"].first()

    # EA Flood Zone 3 (python -m roadsense.ingest.flood). Without the file every segment is False.
    fz_path = os.path.join(ROOT, cfg["paths"]["raw_dir"], "flood", f"{c['code']}_fz3.gpkg")
    if os.path.exists(fz_path):
        fz = gpd.read_file(fz_path).to_crs(BNG)
        hit = gpd.sjoin(mid, fz[["geometry"]], predicate="within", how="left")
        seg["in_flood_area"] = hit.groupby(level=0)["index_right"].apply(lambda s: s.notna().any())
    else:
        print(f"  {city}: no flood file, in_flood_area = False")
        seg["in_flood_area"] = False

    mid_ll = mid.to_crs(4326).geometry
    seg["lat"], seg["lon"] = mid_ll.y.round(6), mid_ll.x.round(6)
    wkb = shapely.to_wkb(np.asarray(seg.geometry.to_crs(4326).array))
    seg = pd.DataFrame(seg.drop(columns="geometry"))
    seg["geometry"] = wkb
    seg["source"] = "osm"
    cols = ["segment_id", "city", "osm_way_id", "name", "ref", "highway", "is_main_road", "ward", "length_m",
            "lat", "lon", "bus_stop", "bus_routes", "junction", "in_flood_area", "owner", "geometry", "source"]
    return seg[cols]


def main():
    ap = argparse.ArgumentParser(description="Build road segments from cached OSM data")
    ap.add_argument("cities", nargs="+")
    args = ap.parse_args()
    cfg = load_config()
    out = os.path.join(ROOT, cfg["paths"]["processed_dir"], "segments.parquet")
    old = pd.read_parquet(out) if os.path.exists(out) else None
    new = [build_city(cfg, city) for city in args.cities]
    if old is not None:
        new.insert(0, old[~old.city.isin(args.cities)])
    seg = pd.concat(new, ignore_index=True)
    if old is not None:   # keep the existing city order (rebuilding a city must not move its rows)
        order = list(dict.fromkeys(list(old.city) + list(args.cities)))
        seg = (seg.assign(_c=seg.city.map({c: i for i, c in enumerate(order)}))
               .sort_values("_c", kind="stable").drop(columns="_c").reset_index(drop=True))
    assert seg.segment_id.is_unique, "duplicate segment_id"
    ctx = os.path.join(ROOT, cfg["paths"]["processed_dir"], "segment_context.parquet")
    if os.path.exists(ctx):   # keep cycle_route / near_school_hospital (python -m roadsense.features.context)
        from roadsense.features.context import attach
        seg = attach(seg, pd.read_parquet(ctx))
    seg.to_parquet(out, index=False)
    for city, g in seg.groupby("city"):
        main_rd = g[g.is_main_road]
        print(f"{city}: {len(g)} segments, {g.length_m.sum() / 1000:.0f} km "
              f"(main roads {len(main_rd)}, {main_rd.length_m.sum() / 1000:.0f} km), "
              f"{g.ward.nunique()} wards, bus stop {g.bus_stop.mean():.0%}, on bus route {(g.bus_routes > 0).mean():.0%}, "
              f"junction {g.junction.mean():.0%}, national_highways {(g.owner == 'national_highways').sum()}")
    print(f"wrote {out} ({len(seg)} rows)")


if __name__ == "__main__":
    main()
