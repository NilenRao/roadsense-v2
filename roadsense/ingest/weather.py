"""
M2 weather ingest: hourly Open-Meteo weather at each ward centroid, history + 16-day forecast.

    python -m roadsense.ingest.weather Southampton [Portsmouth Brighton]   # --refresh to re-download

Source: Open-Meteo (archive-api.open-meteo.com, api.open-meteo.com), CC BY 4.0, free tier is
non-commercial: fine for the hackathon, say so in the README. API checked 17 Oct 2026.
Needs data/raw/osm/{code}_wards.gpkg from roadsense.ingest.osm.

Writes (data/raw/ is git-ignored):
    data/raw/weather/{code}_hourly.parquet   ward, city, time (UTC), temperature_2m, precipitation,
                                              snowfall, soil_temperature_0_to_7cm, soil_moisture_0_to_7cm,
                                              is_forecast, retrieved_at, source
Then build weekly features with: python -m roadsense.features.weather_feats
"""
import argparse, json, os, time, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from roadsense.ingest.fixmystreet import ROOT, load_config

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
HOURLY = ["temperature_2m", "precipitation", "snowfall", "soil_temperature_0_to_7cm", "soil_moisture_0_to_7cm"]
DEFAULTS = {
    "start_date": "2014-09-01",   # labels start 2015; the longest window (90 days) needs history before that
    "chunk_years": 4,             # archive request size per ward
    "archive_lag_days": 6,        # archive data trails real time by about 5 days
    "forecast_days": 16,
    "past_days": 14,              # forecast API back-fills the archive lag
    "pause_s": 0.5,
}


def wcfg(cfg):
    return {**DEFAULTS, **(cfg.get("weather") or {})}


def get_json(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "SotonShip26 student hackathon"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except Exception as e:
            wait = 10 * (attempt + 1)
            print(f"  retry in {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError("giving up on " + url)


def ward_centroids(cfg, city):
    import geopandas as gpd
    code = cfg["cities"][city]["code"]
    path = os.path.join(ROOT, cfg["paths"]["raw_dir"], "osm", f"{code}_wards.gpkg")
    w = gpd.read_file(path).to_crs(27700)
    pts = w.geometry.representative_point().to_crs(4326)   # always inside the ward
    return pd.DataFrame({"ward": w["ward"], "lat": pts.y.round(4), "lon": pts.x.round(4)})


def to_frame(js, ward, city, is_forecast):
    h = js["hourly"]
    df = pd.DataFrame({k: h.get(k) for k in ["time"] + HOURLY})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["ward"], df["city"], df["is_forecast"] = ward, city, is_forecast
    return df


def pull_city(cfg, city, refresh=False):
    c = wcfg(cfg)
    code = cfg["cities"][city]["code"]
    out_dir = os.path.join(ROOT, cfg["paths"]["raw_dir"], "weather")
    cache = os.path.join(out_dir, "cache")
    os.makedirs(cache, exist_ok=True)
    today = date.today()
    arch_end = today - timedelta(days=c["archive_lag_days"])
    frames = []
    for _, r in ward_centroids(cfg, city).iterrows():
        # archive in chunks
        s = date.fromisoformat(c["start_date"])
        while s <= arch_end:
            e = min(date(s.year + c["chunk_years"], 1, 1) - timedelta(days=1), arch_end)
            fn = os.path.join(cache, f"{code}_{r.ward.replace(' ', '_').replace('&', 'and')}_{s}_{e}.json")
            # past chunks never change; the chunk ending at today's lag is re-pulled each day
            if refresh or not os.path.exists(fn):
                q = {"latitude": r.lat, "longitude": r.lon, "start_date": s, "end_date": e,
                     "hourly": ",".join(HOURLY), "timezone": "GMT"}
                js = get_json(ARCHIVE + "?" + urllib.parse.urlencode(q))
                with open(fn, "w") as f:
                    json.dump(js, f)
                time.sleep(c["pause_s"])
            frames.append(to_frame(json.load(open(fn)), r.ward, city, False))
            s = e + timedelta(days=1)
        # forecast (+ recent past to cover the archive lag): always fresh
        q = {"latitude": r.lat, "longitude": r.lon, "hourly": ",".join(HOURLY), "timezone": "GMT",
             "forecast_days": c["forecast_days"], "past_days": c["past_days"]}
        js = get_json(FORECAST + "?" + urllib.parse.urlencode(q))
        fc = to_frame(js, r.ward, city, True)
        frames.append(fc[fc.time > pd.Timestamp(arch_end, tz="UTC") + pd.Timedelta(hours=23)])
        time.sleep(c["pause_s"])
        print(f"  {city} · {r.ward}: done")
    df = pd.concat(frames, ignore_index=True).drop_duplicates(["ward", "time"], keep="first")
    now = datetime.now(timezone.utc)
    df["is_forecast"] = df["is_forecast"] & (df["time"] > now)       # past hours from the forecast API are observations-based
    df["retrieved_at"] = now.isoformat(timespec="minutes")
    df["source"] = "open-meteo"
    path = os.path.join(out_dir, f"{code}_hourly.parquet")
    df.sort_values(["ward", "time"]).to_parquet(path, index=False)
    gaps = df.sort_values("time").groupby("ward")["time"].diff().max()
    print(f"{city}: {len(df):,} hourly rows, {df.ward.nunique()} wards, largest gap {gaps} -> {path}")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cities", nargs="*", default=["Southampton"])
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    for city in a.cities:
        pull_city(cfg, city, a.refresh)


if __name__ == "__main__":
    main()
