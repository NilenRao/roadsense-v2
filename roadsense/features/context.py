"""
Consequence-weight context per segment (build spec: "Urgency" consequence weights) -> data/processed/segment_context.parquet

    python -m roadsense.features.context Southampton [Portsmouth Brighton]   # --refresh to re-download

Columns: segment_id, city, cycle_route (bool), near_school_hospital (bool), source
- cycle_route: the segment's OSM way is a member of a route=bicycle relation, or the way is tagged with an on-road cycle
  lane/track (cycleway, cycleway:left/right/both = lane|track|shared_lane|share_busway).
- near_school_hospital: an OSM amenity=school|college|hospital (node, way or relation centre) within 100 m of the segment.
Source: OpenStreetMap contributors (ODbL), via Overpass (servers in config.yaml osm.overpass_urls). Raw replies are cached
in data/raw/osm/{code}_context.json. Also copied onto segments.parquet (lane C's urgency weights switch on when the columns are there); the
separate file stays the source, and roadsense.features.segments re-attaches it after every rebuild.
"""
import argparse, json, os, urllib.parse, urllib.request, time

import pandas as pd

from roadsense.ingest.fixmystreet import ROOT, load_config

NEAR_M = 100
CYCLE_VALUES = "lane|track|shared_lane|share_busway"


def query(bbox):
    s, w, n, e = bbox
    bb = f"{s},{w},{n},{e}"
    return (f'[out:json][timeout:300];'
            f'nwr["amenity"~"^(school|college|hospital)$"]({bb});out center tags;'
            f'rel["route"="bicycle"]({bb});out body;'
            f'way["highway"]["cycleway"~"^({CYCLE_VALUES})$"]({bb});out ids;'
            f'way["highway"]["cycleway:left"~"^({CYCLE_VALUES})$"]({bb});out ids;'
            f'way["highway"]["cycleway:right"~"^({CYCLE_VALUES})$"]({bb});out ids;'
            f'way["highway"]["cycleway:both"~"^({CYCLE_VALUES})$"]({bb});out ids;')


def fetch(cfg, bbox):
    body = urllib.parse.urlencode({"data": query(bbox)}).encode()
    errors = []
    for base in cfg["osm"]["overpass_urls"]:
        for attempt in range(cfg["osm"].get("tries", 3)):
            try:
                req = urllib.request.Request(base.rstrip("/") + "/interpreter", data=body,
                                             headers={"User-Agent": "SotonShip26 student hackathon"})
                with urllib.request.urlopen(req, timeout=400) as r:
                    return json.load(r)
            except Exception as ex:
                errors.append(f"{base}: {ex}")
                time.sleep(15 * (attempt + 1))
    raise RuntimeError("Overpass failed: " + " | ".join(errors[-3:]))


def parse(js):
    """-> (sites DataFrame[kind, lat, lon, name], set of cycle way ids)"""
    sites, cycle = [], set()
    for el in js.get("elements", []):
        tags = el.get("tags") or {}
        if tags.get("amenity") in ("school", "college", "hospital"):
            lat = el.get("lat", (el.get("center") or {}).get("lat"))
            lon = el.get("lon", (el.get("center") or {}).get("lon"))
            if lat is not None:
                sites.append((tags["amenity"], lat, lon, tags.get("name", "")))
        elif el.get("type") == "relation":
            cycle.update(m["ref"] for m in el.get("members", []) if m.get("type") == "way")
        elif el.get("type") == "way":
            cycle.add(el["id"])
    return pd.DataFrame(sites, columns=["kind", "lat", "lon", "name"]), cycle


def flags(seg, sites, cycle_ways, near_m=NEAR_M):
    """seg: GeoDataFrame in EPSG:27700 with segment_id, osm_way_id. sites: DataFrame lat/lon."""
    import geopandas as gpd
    out = pd.DataFrame({"segment_id": seg["segment_id"].values})
    out["cycle_route"] = seg["osm_way_id"].isin(cycle_ways).values
    if len(sites):
        pts = gpd.GeoDataFrame(sites, geometry=gpd.points_from_xy(sites.lon, sites.lat), crs=4326).to_crs(27700)
        near = gpd.sjoin_nearest(seg[["segment_id", "geometry"]], pts[["geometry"]], how="left",
                                 max_distance=near_m, distance_col="d")
        hit = set(near.loc[near["d"].notna(), "segment_id"])
        out["near_school_hospital"] = out.segment_id.isin(hit)
    else:
        out["near_school_hospital"] = False
    return out


def build_city(cfg, city, seg_all, refresh=False):
    import geopandas as gpd
    from shapely import wkb
    code = cfg["cities"][city]["code"]
    s = seg_all[seg_all.city == city].copy()
    geom = s["geometry"].apply(lambda g: wkb.loads(g) if isinstance(g, (bytes, bytearray)) else g)
    sg = gpd.GeoDataFrame(s, geometry=geom, crs=4326)
    w, so, e, n = sg.total_bounds
    bbox = (round(so - 0.005, 5), round(w - 0.005, 5), round(n + 0.005, 5), round(e + 0.005, 5))
    cache = os.path.join(ROOT, cfg["paths"]["raw_dir"], "osm", f"{code}_context.json")
    if refresh or not os.path.exists(cache):
        js = fetch(cfg, bbox)
        with open(cache, "w") as f:
            json.dump(js, f)
    sites, cycle = parse(json.load(open(cache)))
    out = flags(sg.to_crs(27700), sites, cycle)
    out["city"], out["source"] = city, "osm"
    print(f"{city}: {len(sites)} schools/colleges/hospitals, {len(cycle)} cycle ways; "
          f"cycle_route {out.cycle_route.mean():.0%}, near_school_hospital {out.near_school_hospital.mean():.0%} of segments")
    return out


CONTEXT_COLS = ["cycle_route", "near_school_hospital"]


def attach(seg, ctx):
    """segments + context columns (joined on segment_id). Same rows, same order; missing context -> False."""
    base = seg.drop(columns=[c for c in CONTEXT_COLS if c in seg.columns])
    out = base.merge(ctx[["segment_id"] + CONTEXT_COLS], on="segment_id", how="left", validate="one_to_one")
    assert len(out) == len(seg) and out.segment_id.equals(seg.segment_id.reset_index(drop=True)), "segment rows changed"
    for c in CONTEXT_COLS:
        out[c] = out[c].fillna(False).astype(bool)
    return out


def attach_to_segments(cfg):
    """Write cycle_route / near_school_hospital onto segments.parquet (lane C switches its weights on when they exist)."""
    proc = os.path.join(ROOT, cfg["paths"]["processed_dir"])
    ctx_path, seg_path = os.path.join(proc, "segment_context.parquet"), os.path.join(proc, "segments.parquet")
    if not os.path.exists(ctx_path):
        return
    seg = pd.read_parquet(seg_path)
    out = attach(seg, pd.read_parquet(ctx_path))
    out.to_parquet(seg_path, index=False)
    print(f"segments.parquet: + {', '.join(CONTEXT_COLS)} ({len(out)} rows, ids unchanged)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cities", nargs="*", default=["Southampton"])
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    seg = pd.read_parquet(os.path.join(ROOT, cfg["paths"]["processed_dir"], "segments.parquet"))
    path = os.path.join(ROOT, cfg["paths"]["processed_dir"], "segment_context.parquet")
    new = pd.concat([build_city(cfg, c, seg, a.refresh) for c in a.cities], ignore_index=True)
    if os.path.exists(path):
        old = pd.read_parquet(path)
        new = pd.concat([old[~old.city.isin(a.cities)], new], ignore_index=True)
    new.to_parquet(path, index=False)
    print(f"-> {path}")
    attach_to_segments(cfg)


if __name__ == "__main__":
    main()
