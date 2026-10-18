"""
Housing and development sites in Southampton and Brighton, from the councils' brownfield land registers.

    python -m roadsense.ingest.construction

Source: planning.data.gov.uk, dataset brownfield-land (each council's statutory brownfield land register, OGL v3),
read through its public entity API by local authority (ONS code). One row per register site: point, area,
net dwellings (min/max), planning permission status, type and date, ownership, and the register's start and end
dates. A site with an end date has left the register (built, lapsed or withdrawn; the register does not say which).

Why: a building site brings heavy vehicles (muck-away lorries, concrete mixers, deliveries) onto the roads around
it for months, then more traffic once people move in. Southampton and Brighton do not publish planning applications
to planning.data.gov.uk (planning-application dataset: 0 records for both, checked 18 Oct 2026), and neither council
publishes its council-house building pipeline as open data, so the brownfield register is the open, machine-readable
list of sites with permission for new homes. Sites on land owned by a public authority (ownership_status
owned-by-a-public-authority) are the closest open proxy for council or other public housing schemes.

Second source, current sites only: OpenStreetMap ways and relations tagged landuse=construction inside each city's
road-network bounding box (Overpass API, ODbL). Mappers add these when a site is fenced off and remove them when it is
finished, so they show building sites active now, with no dates to test against past years (source osm_construction,
used for the map and road card only; the held-out check uses register sites with a permission date).

Output: data/processed/construction_sites.csv (source = brownfield_register | osm_construction)
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "data", "processed", "construction_sites.csv")
API = "https://www.planning.data.gov.uk/entity.json?dataset=brownfield-land&limit=100&geometry_curie=statistical-geography:{ons}"
CITIES = {"Southampton": "E06000045", "Brighton": "E06000043"}
SOURCE = "Brownfield land registers via planning.data.gov.uk (MHCLG), OGL v3"
OVERPASS = ["https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
COLS = ["source", "city", "reference", "name", "site_address", "lon", "lat", "hectares", "min_net_dwellings", "max_net_dwellings",
        "permission_status", "permission_type", "permission_date", "ownership_status", "public_land", "deliverable",
        "start_date", "end_date", "entry_date", "site_plan_url", "retrieved_at"]


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "RoadSense hackathon (open data research)"})
    for k in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:
            if k == 3:
                raise
            time.sleep(2 ** (k + 1))


def point(wkt: str):
    if not wkt or not wkt.startswith("POINT"):
        return None, None
    lon, lat = wkt[wkt.index("(") + 1:wkt.index(")")].split()
    return float(lon), float(lat)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def rows_for(city: str, ons: str, now: str) -> list[dict]:
    out, url = [], API.format(ons=ons)
    while url:
        d = get(url.replace("http://", "https://"))
        for e in d.get("entities", []):
            lon, lat = point(e.get("point", ""))
            if lon is None:
                continue
            own = e.get("ownership-status") or ""
            out.append({"source": "brownfield_register", "city": city, "reference": e.get("reference"), "name": e.get("name"),
                        "site_address": e.get("site-address"), "lon": lon, "lat": lat, "hectares": num(e.get("hectares")),
                        "min_net_dwellings": num(e.get("minimum-net-dwellings")),
                        "max_net_dwellings": num(e.get("maximum-net-dwellings")),
                        "permission_status": e.get("planning-permission-status"),
                        "permission_type": e.get("planning-permission-type"),
                        "permission_date": e.get("planning-permission-date") or None, "ownership_status": own,
                        "public_land": own in ("owned-by-a-public-authority", "mixed-ownership"),
                        "deliverable": e.get("deliverable") or None, "start_date": e.get("start-date") or None,
                        "end_date": e.get("end-date") or None, "entry_date": e.get("entry-date") or None,
                        "site_plan_url": e.get("site-plan-url"), "retrieved_at": now})
        url = (d.get("links") or {}).get("next")
        time.sleep(0.5)
    return out


def city_bbox(city: str, seg_path: str | None = None) -> tuple[float, float, float, float]:
    seg = pd.read_parquet(seg_path or os.path.join(ROOT, "data", "processed", "segments.parquet"), columns=["city", "lat", "lon"])
    s = seg[seg["city"] == city]
    return float(s["lat"].min()), float(s["lon"].min()), float(s["lat"].max()), float(s["lon"].max())


def osm_rows(city: str, now: str, seg_path: str | None = None) -> list[dict]:
    s, w, n, e = city_bbox(city, seg_path)
    q = (f'[out:json][timeout:120];(way["landuse"="construction"]({s},{w},{n},{e});'
         f'relation["landuse"="construction"]({s},{w},{n},{e}););out center tags;')
    body = urllib.parse.urlencode({"data": q}).encode()
    for url in OVERPASS:
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": "RoadSense hackathon (open data research)"})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
            break
        except Exception as ex:
            print(f"  {url}: {ex}")
    else:
        return []
    out = []
    for x in d.get("elements", []):
        c, t = x.get("center") or {}, x.get("tags", {})
        if not c:
            continue
        kind = t.get("construction")
        out.append({"source": "osm_construction", "city": city, "reference": f"osm:{x['type']}/{x['id']}",
                    "name": t.get("name") or (f"{kind} construction" if kind else "construction site"),
                    "site_address": t.get("name") or t.get("description"), "lon": c["lon"], "lat": c["lat"],
                    "permission_type": kind, "ownership_status": None, "public_land": False,
                    "start_date": t.get("start_date"), "entry_date": t.get("check_date"),
                    "site_plan_url": f"https://www.openstreetmap.org/{x['type']}/{x['id']}", "retrieved_at": now})
    print(f"{city}: {len(out)} OpenStreetMap construction sites (OSM data as of {d.get('osm3s', {}).get('timestamp_osm_base')})")
    return out


def build(out: str = OUT, seg_path: str | None = None, verbose: bool = True) -> pd.DataFrame:
    """Fetch both sources and write `out`. A source that fails keeps its rows from the previous file, so one
    unreachable server never empties the list (the daily refresh calls this)."""
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    old = pd.read_csv(out) if os.path.exists(out) else pd.DataFrame(columns=COLS)
    frames = []
    try:
        frames.append(pd.DataFrame([r for c, ons in CITIES.items() for r in rows_for(c, ons, now)], columns=COLS))
    except Exception as e:  # noqa: BLE001
        print(f"  brownfield registers unreachable ({e}); keeping the previous rows")
        frames.append(old[old["source"] == "brownfield_register"])
    osm = [r for c in CITIES for r in osm_rows(c, now, seg_path)]
    frames.append(pd.DataFrame(osm, columns=COLS) if osm else old[old["source"] == "osm_construction"])
    df = pd.concat([f for f in frames if len(f)], ignore_index=True).drop_duplicates(["city", "reference"], keep="last")
    df.to_csv(out, index=False)
    return df


def main():
    df = build()
    for c, g in df[df["source"] == "brownfield_register"].groupby("city"):
        print(f"{c}: {len(g)} sites, {int(g['permission_status'].eq('permissioned').sum())} with permission, "
              f"{int(g['public_land'].sum())} on public land, {int(g['end_date'].notna().sum())} off the register")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
