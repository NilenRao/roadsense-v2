"""
Download and cache the OpenStreetMap (ODbL) and ward inputs for road segments.

    python -m roadsense.ingest.osm Southampton [Portsmouth Brighton]    # --refresh to re-download

Writes to data/raw/osm/ (git-ignored):
    {code}_boundary.gpkg      council boundary (OSM, geocoded from cities.<city>.osm_place)
    {code}_drive.graphml      drivable road graph (osmnx network_type="drive")
    {code}_bus_stops.gpkg     highway=bus_stop nodes
    {code}_bus_routes.json    {osm_way_id: number of route=bus relations using it}
    {code}_wards.gpkg         council wards from MapIt (children of the council area); ONS wards if MapIt is over quota
Needs: osmnx, geopandas (see .venv). Sources: OpenStreetMap contributors (ODbL); MapIt / ONS boundaries (OGL).
"""
import argparse, json, os, time, urllib.parse, urllib.request

import geopandas as gpd
import osmnx as ox
from shapely.geometry import shape

from roadsense.ingest.fixmystreet import ROOT, load_config


def raw_dir(cfg):
    d = os.path.join(ROOT, cfg["paths"]["raw_dir"], "osm")
    os.makedirs(d, exist_ok=True)
    ox.settings.cache_folder = os.path.join(d, "cache")
    return d


def http_json(cfg, url, data=None, tries=4):
    ua = cfg["fixmystreet"]["user_agent"]
    req = urllib.request.Request(url, data=data, headers={"User-Agent": ua})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=cfg["osm"]["timeout_s"] + 60) as r:
                return json.load(r)
        except Exception as e:
            wait = 30 * (attempt + 1)
            print(f"  retry in {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError("giving up on " + url)


def bus_routes(cfg, bounds):
    """Count route=bus relations per member way inside the boundary's bounding box."""
    w, s, e, n = bounds
    q = f'[out:json][timeout:{cfg["osm"]["timeout_s"]}];rel["route"="bus"]({s},{w},{n},{e});out body;'
    data = http_json(cfg, ox.settings.overpass_url + "/interpreter", urllib.parse.urlencode({"data": q}).encode(), tries=cfg["osm"]["tries"])
    counts = {}
    for rel in data.get("elements", []):
        for way in {m["ref"] for m in rel.get("members", []) if m["type"] == "way"}:
            counts[way] = counts.get(way, 0) + 1
    return counts


def wards_ons(cfg, city):
    """ONS ward boundaries (OGL) for the council: fallback when MapIt is over quota."""
    q = {"where": f"LAD24CD='{cfg['osm']['ons_lad'][city]}'", "outFields": "WD24CD,WD24NM",
         "outSR": 4326, "f": "geojson"}
    js = http_json(cfg, cfg["osm"]["ons_wards_url"] + "?" + urllib.parse.urlencode(q), tries=2)
    rows = [{"ward": f["properties"]["WD24NM"], "ons_code": f["properties"]["WD24CD"],
             "geometry": shape(f["geometry"])} for f in js["features"]]
    return gpd.GeoDataFrame(rows, crs="EPSG:4326").sort_values("ward").reset_index(drop=True)


def wards(cfg, city):
    try:
        return wards_mapit(cfg, city)
    except Exception as e:
        print(f"  MapIt failed ({str(e)[:80]}), using ONS wards")
        return wards_ons(cfg, city)


def wards_mapit(cfg, city):
    base = cfg["osm"]["mapit_url"]
    area = cfg["osm"]["council_area"][city]
    kids = http_json(cfg, f"{base}/area/{area}/children", tries=2)
    rows = []
    for aid, a in sorted(kids.items()):
        if a["type"] not in ("UTW", "MTW", "DIW"):
            continue
        time.sleep(1)
        rows.append({"ward": a["name"], "mapit_id": int(aid),
                     "geometry": shape(http_json(cfg, f"{base}/area/{aid}.geojson", tries=2))})
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def download(cfg, city, refresh=False):
    c = cfg["cities"][city]
    code, d = c["code"], raw_dir(cfg)
    path = lambda name: os.path.join(d, f"{code}_{name}")
    need = lambda name: refresh or not os.path.exists(path(name))

    if need("boundary.gpkg"):
        ox.geocode_to_gdf(c["osm_place"]).to_file(path("boundary.gpkg"))
    boundary = gpd.read_file(path("boundary.gpkg"))
    poly = boundary.geometry.iloc[0]
    print(f"{city}: boundary {boundary.to_crs(27700).area.iloc[0] / 1e6:.1f} km²")

    if need("drive.graphml"):
        G = ox.graph_from_polygon(poly, network_type="drive")
        ox.save_graphml(G, path("drive.graphml"))
        print(f"  roads: {len(G.edges)} directed edges")
    if need("bus_stops.gpkg"):
        stops = ox.features_from_polygon(poly, {"highway": "bus_stop"})
        stops = stops[stops.geometry.geom_type == "Point"].reset_index()[["id", "geometry"]]
        stops.to_file(path("bus_stops.gpkg"))
        print(f"  bus stops: {len(stops)}")
    if need("bus_routes.json"):
        counts = bus_routes(cfg, poly.bounds)
        with open(path("bus_routes.json"), "w") as f:
            json.dump(counts, f)
        print(f"  ways on bus routes: {len(counts)}")
    if need("wards.gpkg"):
        wd = wards(cfg, city)
        wd.to_file(path("wards.gpkg"))
        print(f"  wards: {len(wd)}")


def download_any_server(cfg, city, refresh=False):
    """Try each Overpass server in turn (the public ones are often at capacity)."""
    ox.settings.http_user_agent = cfg["fixmystreet"]["user_agent"]
    ox.settings.requests_timeout = cfg["osm"]["timeout_s"]
    for url in cfg["osm"]["overpass_urls"]:
        ox.settings.overpass_url = url
        try:
            return download(cfg, city, refresh)
        except Exception as e:
            print(f"  {url} failed ({str(e).splitlines()[0][:120]}), trying the next server")
    raise RuntimeError("all Overpass servers failed")


def main():
    ap = argparse.ArgumentParser(description="Download OSM + ward inputs for road segments")
    ap.add_argument("cities", nargs="+")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    for city in args.cities:
        download_any_server(cfg, city, args.refresh)


if __name__ == "__main__":
    main()
