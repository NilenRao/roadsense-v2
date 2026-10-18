"""
Download Environment Agency Flood Zone 3 polygons (Flood Map for Planning, rivers and sea; OGL) for each city.

    python -m roadsense.ingest.flood Southampton [Portsmouth Brighton]   # --refresh to re-download

Writes data/raw/flood/{code}_fz3.gpkg (EPSG:27700), covering the city's boundary box from roadsense.ingest.osm.
roadsense.features.segments then sets in_flood_area = segment midpoint inside a Flood Zone 3 polygon.
"""
import argparse, io, json, os, time, urllib.parse, urllib.request

import geopandas as gpd
import pandas as pd

from roadsense.ingest.fixmystreet import ROOT, load_config


def get(cfg, params):
    url = cfg["flood"]["wfs_url"] + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": cfg["fixmystreet"]["user_agent"]})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:
            wait = 10 * (attempt + 1)
            print(f"  retry in {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError("giving up on " + url)


def download(cfg, city, refresh=False):
    code, fc = cfg["cities"][city]["code"], cfg["flood"]
    out_dir = os.path.join(ROOT, cfg["paths"]["raw_dir"], "flood")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{code}_fz3.gpkg")
    if os.path.exists(path) and not refresh:
        return gpd.read_file(path)
    boundary = gpd.read_file(os.path.join(ROOT, cfg["paths"]["raw_dir"], "osm", f"{code}_boundary.gpkg"))
    x0, y0, x1, y1 = boundary.to_crs(27700).buffer(fc["buffer_m"]).total_bounds.round()
    cql = f"flood_zone='{fc['zone']}' AND BBOX(shape,{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f})"
    frames, start = [], 0
    while True:
        raw = get(cfg, {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": fc["layer"],
                        "outputFormat": "application/json", "CQL_FILTER": cql, "count": fc["page"],
                        "startIndex": start})
        feats = json.loads(raw).get("features", [])
        if not feats:
            break
        frames.append(gpd.read_file(io.BytesIO(raw)))
        start += len(feats)
        if len(feats) < fc["page"]:
            break
        time.sleep(1)
    gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=27700) if frames else \
        gpd.GeoDataFrame({"flood_zone": []}, geometry=[], crs=27700)
    if "id" in gdf:   # paging is unsorted (the server rejects sortBy): drop any repeats
        gdf = gdf.drop_duplicates("id")
    gdf.to_file(path)
    print(f"{city}: {len(gdf)} {fc['zone']} polygons, {gdf.area.sum() / 1e6:.1f} km² -> {path}")
    return gdf


def main():
    ap = argparse.ArgumentParser(description="Download EA Flood Zone 3 polygons")
    ap.add_argument("cities", nargs="+")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    for city in args.cities:
        download(cfg, city, args.refresh)


if __name__ == "__main__":
    main()
