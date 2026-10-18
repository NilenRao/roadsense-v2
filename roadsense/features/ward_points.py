"""
Committed ward points for per-ward weather: data/processed/ward_points_<city>.csv (ward, lat, lon).

    python -m roadsense.features.ward_points Southampton Brighton Portsmouth

Same points as roadsense.ingest.weather.ward_centroids (a representative point inside each ward
polygon, EPSG:27700, rounded to 4 dp), written once from data/raw/osm/{code}_wards.gpkg so the daily
refresh (roadsense.refresh) never needs data/raw/, which is not committed.
"""
import argparse, os

import pandas as pd

from roadsense.ingest.fixmystreet import ROOT, load_config
from roadsense.ingest.weather import ward_centroids


def path_for(processed_dir, city: str) -> str:
    return os.path.join(processed_dir, f"ward_points_{city.lower().replace(' ', '_')}.csv")


def read(processed_dir, city: str) -> pd.DataFrame:
    p = path_for(processed_dir, city)
    if not os.path.exists(p):
        raise FileNotFoundError(f"{p} missing: build it once with python -m roadsense.features.ward_points {city}")
    return pd.read_csv(p)


def main():
    ap = argparse.ArgumentParser(description="Write data/processed/ward_points_<city>.csv from the ward polygons")
    ap.add_argument("cities", nargs="+")
    cfg = load_config()
    proc = os.path.join(ROOT, cfg["paths"]["processed_dir"])
    for city in ap.parse_args().cities:
        pts = ward_centroids(cfg, city)
        pts.to_csv(path_for(proc, city), index=False)
        print(f"{city}: {len(pts)} ward points -> {path_for(proc, city)}")


if __name__ == "__main__":
    main()
