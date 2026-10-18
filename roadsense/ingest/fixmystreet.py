"""
FixMyStreet ingest: pull reports via the public Open311 GeoReport v2 API, then build the road-defect
label file. Output columns are defined in schemas.md; cities, categories and windows in config.yaml.

    python -m roadsense.ingest.fixmystreet              # pull (a few minutes, polite) + build
    python -m roadsense.ingest.fixmystreet --no-pull    # rebuild from data/raw/fms_raw.csv

Outputs:
    data/raw/fms_raw.csv                   all reports with free text (KEEP LOCAL, git-ignored)
    data/processed/fms_roads_clean.csv     road-surface labels: de-duplicated, pavement reports removed
    data/processed/fms_streetlights.csv    Southampton "Street lighting" reports (for lane B)
    data/processed/fms_summary.txt
Source: FixMyStreet (mySociety). Credit it with the pull date.
"""
import argparse, csv, json, math, os, re, time, urllib.parse, urllib.request
from datetime import datetime

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_config(path=os.path.join(ROOT, "config.yaml")):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------- pull ----------

def fetch(cfg, params):
    fms = cfg["fixmystreet"]
    url = fms["base_url"] + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": fms["user_agent"]})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r).get("service_requests", [])
        except Exception as e:
            wait = 5 * (attempt + 1)
            print(f"  retry in {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError("giving up on " + url)


def get_range(cfg, agency, cat, year):
    pause = cfg["fixmystreet"]["pause_s"]
    base = {"jurisdiction_id": "fixmystreet", "agency_responsible": agency,
            "service_code": cat, "max_requests": 1000}
    rows = fetch(cfg, {**base, "start_date": f"{year}-01-01", "end_date": f"{year}-12-31"})
    time.sleep(pause)
    if len(rows) >= 1000:   # API caps at 1000: split into months
        rows = []
        for m in range(1, 13):
            s = f"{year}-{m:02d}-01"
            e = f"{year}-{m+1:02d}-01" if m < 12 else f"{year+1}-01-01"
            rows += fetch(cfg, {**base, "start_date": s, "end_date": e})
            time.sleep(pause)
    return rows


def pull(cfg):
    words = cfg["fixmystreet"]["surface_words"]
    years = range(cfg["fixmystreet"]["first_year"], datetime.now().year + 1)
    seen, rows = set(), []
    for city, c in cfg["cities"].items():
        for cat in c["fms_road_categories"] + c["fms_other_categories"]:
            for y in years:
                new = 0
                for r in get_range(cfg, c["fms_agency"], cat, y):
                    rid = r.get("service_request_id")
                    if rid in seen:
                        continue
                    seen.add(rid); new += 1
                    text = ((r.get("title") or "") + " " + (r.get("description") or "")).lower()
                    rows.append({
                        "city": city,
                        "id": rid,
                        "category": r.get("service_name"),
                        "requested": r.get("requested_datetime"),
                        "updated": r.get("updated_datetime"),
                        "status": r.get("status"),
                        "lat": r.get("lat"), "lon": r.get("long"),
                        "title": (r.get("title") or "").replace("\n", " "),
                        "description": (r.get("description") or "").replace("\n", " ")[:500],
                        "surface_keyword": any(w in text for w in words),
                    })
                print(f"{city:12s} {cat:26s} {y}: {new}")
    rows.sort(key=lambda r: r["requested"] or "")
    return rows


# ---------- build ----------

def haversine_m(a, b):
    R = 6371000
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2 * R * math.asin(math.sqrt(h))


def road_surface(cfg, rows):
    """Potholes, plus general road categories whose text mentions a surface defect."""
    cats = set(cfg["fixmystreet"]["surface_categories"])
    return [r for r in rows if r["lat"] and (r["category"] == "Potholes" or
            (r["category"] in cats and r["surface_keyword"]))]


def dedup(cfg, rows):
    """Per city: same defect = within dedup_m and dedup_days of an earlier kept report."""
    max_m, max_days = cfg["fixmystreet"]["dedup_m"], cfg["fixmystreet"]["dedup_days"]
    kept, kept_by_city = [], {}
    for r in rows:
        t = datetime.fromisoformat(r["requested"])
        p = (float(r["lat"]), float(r["lon"]))
        city_kept = kept_by_city.setdefault(r["city"], [])
        dup = False
        for k_t, k_p in reversed(city_kept[-300:]):
            if (t - k_t).days > max_days:
                break
            if haversine_m(p, k_p) <= max_m:
                dup = True; break
        if not dup:
            kept.append(r); city_kept.append((t, p))
    return kept


# Pavement filter. Transparent on purpose, so it can be explained to judges:
#   - PAVEMENT words in the title  -> drop, unless the title also names a ROAD word ("pothole in road by kerb")
#   - title says nothing either way -> decide from the description the same way
#   - otherwise keep
PAVE = r"\b(pavements?|footpaths?|footways?|paving|slabs?|kerbs?|curbs?|kerbstones?|dropped kerb|walkways?|path|trip (hazard|risk))\b"
# Street names ("Tisbury Road", "Hill Lane") must not count as evidence of a road defect
STREET = r"\b[A-Z][a-z]+ (Road|Lane|Street|Avenue|Way|Drive|Hill|Rise|Close)\b"
ROAD = r"\b(road|carriageway|lane|junction|roundabout|street surface|bus stop|traffic|cars?|drivers?|cyclists?|tarmac road)\b"


def classify(title, desc):
    t = re.sub(STREET, " ", str(title or "")).lower()
    d = re.sub(STREET, " ", str(desc or "")).lower()
    pt, rt = bool(re.search(PAVE, t)), bool(re.search(ROAD, t))
    if pt and not rt: return "pavement"
    if rt: return "road"
    pd_, rd = bool(re.search(PAVE, d)), bool(re.search(ROAD, d))
    if pd_ and not rd: return "pavement"
    return "road"


def in_window(cfg, r):
    lo, hi = cfg["labels"]["windows"][r["city"]]["usable"]
    return lo <= int(r["requested"][:4]) <= hi


def _redacted(rows):
    """Copies of rows with the title redacted; the originals (and fms_raw.csv) are untouched."""
    from roadsense.prevent.redact_titles import redact
    return [{**r, "title": redact(r.get("title"))} for r in rows]


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def read_raw(path):
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["surface_keyword"] = r["surface_keyword"] == "True"
    return rows


def build(cfg, raw):
    out = os.path.join(ROOT, cfg["paths"]["processed_dir"])
    os.makedirs(out, exist_ok=True)
    base = ["city", "id", "category", "requested", "updated", "status", "lat", "lon", "title"]

    road_cats = {cat for c in cfg["cities"].values() for cat in c["fms_road_categories"]}
    kept = dedup(cfg, road_surface(cfg, [r for r in raw if r["category"] in road_cats]))
    for r in kept:
        r["surface"] = classify(r["title"], r["description"])
        r["in_window"] = in_window(cfg, r)
    clean = [r for r in kept if r["surface"] == "road"]
    # Redact house numbers, postcodes and names from titles before anything public is written (raw stays local).
    write_csv(os.path.join(out, "fms_roads_clean.csv"), _redacted(clean), base + ["surface_keyword", "in_window"])

    lights = [r for r in raw if r["category"] == "Street lighting" and r["lat"]]
    write_csv(os.path.join(out, "fms_streetlights.csv"), _redacted(lights), base)

    lines = [f"Built {datetime.now().isoformat(timespec='minutes')} from FixMyStreet Open311 (raw rows: {len(raw)})",
             "city         road-surface  pavement-removed  labels  in-window  street-lighting"]
    for city in cfg["cities"]:
        k = [r for r in kept if r["city"] == city]
        c = [r for r in clean if r["city"] == city]
        lines.append(f"{city:12s} {len(k):12d} {len(k) - len(c):17d} {len(c):7d} "
                     f"{sum(r['in_window'] for r in c):10d} {sum(r['city'] == city for r in lights):16d}")
    lines.append("Labels by year:")
    by_year = {}
    for r in clean:
        key = (r["city"], r["requested"][:4])
        by_year[key] = by_year.get(key, 0) + 1
    lines += [f"  {c:12s} {y}: {n}" for (c, y), n in sorted(by_year.items())]
    with open(os.path.join(out, "fms_summary.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[:2 + len(cfg["cities"])]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-pull", action="store_true", help="rebuild from data/raw/fms_raw.csv")
    args = ap.parse_args()
    cfg = load_config()
    raw_dir = os.path.join(ROOT, cfg["paths"]["raw_dir"])
    raw_path = os.path.join(raw_dir, "fms_raw.csv")
    if args.no_pull:
        raw = read_raw(raw_path)
    else:
        os.makedirs(raw_dir, exist_ok=True)
        raw = pull(cfg)
        write_csv(raw_path, raw, list(raw[0].keys()) if raw else [])
    build(cfg, raw)


if __name__ == "__main__":
    main()
