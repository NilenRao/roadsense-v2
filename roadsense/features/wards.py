"""
Append cities to data/processed/wards.geojson (schemas.md section 11) from data/raw/osm/{code}_wards.gpkg.

    python -m roadsense.features.wards Brighton Portsmouth

Cities already in the file are left exactly as they are (Southampton's features are not rebuilt).
Geometry is simplified ~5 m in EPSG:27700 and written in EPSG:4326. Ward names are those in segments.parquet.
"""
import argparse, os

import geopandas as gpd
import pandas as pd

from roadsense.ingest.fixmystreet import ROOT, load_config

SOURCES = {"mapit": "mapit (mySociety), boundaries © Ordnance Survey / ONS, OGL",
           "ons": "ONS Open Geography Portal, Wards Dec 2024 (BGC), contains OS data © Crown copyright, OGL"}


def city_wards(cfg, city):
    code = cfg["cities"][city]["code"]
    w = gpd.read_file(os.path.join(ROOT, cfg["paths"]["raw_dir"], "osm", f"{code}_wards.gpkg"))
    kind = "mapit" if "mapit_id" in w else "ons"
    w = w.to_crs(27700)
    w["geometry"] = w.geometry.simplify(5, preserve_topology=True)
    out = gpd.GeoDataFrame({"city": city, "ward": w["ward"],
                            "mapit_id": w["mapit_id"] if kind == "mapit" else pd.NA,
                            "source": SOURCES[kind]}, geometry=w.geometry.values, crs=27700).to_crs(4326)
    out["mapit_id"] = out["mapit_id"].astype("Int64")
    return out


def main():
    ap = argparse.ArgumentParser(description="Append cities to wards.geojson")
    ap.add_argument("cities", nargs="+")
    args = ap.parse_args()
    cfg = load_config()
    proc = os.path.join(ROOT, cfg["paths"]["processed_dir"])
    path = os.path.join(proc, "wards.geojson")
    old = gpd.read_file(path) if os.path.exists(path) else None
    seg = pd.read_parquet(os.path.join(proc, "segments.parquet"), columns=["city", "ward"])
    have = set(old.city) if old is not None else set()
    new = [city_wards(cfg, c) for c in args.cities if c not in have]
    for c in args.cities:
        if c in have:
            print(f"{c}: already in wards.geojson, left as is")
    for n in new:
        c = n.city.iloc[0]
        missing = set(seg.loc[seg.city == c, "ward"]) - set(n.ward)
        assert not missing, f"{c}: segment wards missing from boundaries: {missing}"
        print(f"{c}: {len(n)} wards appended")
    if not new:
        return
    out = pd.concat(([old] if old is not None else []) + new, ignore_index=True)
    gpd.GeoDataFrame(out, geometry="geometry", crs=4326).to_file(path, driver="GeoJSON")
    print(f"-> {path} ({len(out)} wards)")


if __name__ == "__main__":
    main()
