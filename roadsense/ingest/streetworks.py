"""
Utility and highway works dug into Southampton and Brighton roads, from DfT Street Manager open data.

    python -m roadsense.ingest.streetworks                      # every month not yet in the output
    python -m roadsense.ingest.streetworks --months 2024-01 2024-02

Source: Department for Transport, Street Manager open data (permit archive), OGL v3.
https://opendata.manage-roadworks.service.gov.uk/permit/<yyyy>/<mm>.zip: one zip per month for all of England, one
JSON file per permit event (about 1 GB a month). Each month is downloaded to a temp file, read, and deleted:
nothing raw is kept. Only WORK_STOP events (works completed) whose highway authority is one of CITIES are kept.

A zip's end-of-central-directory record is missing from these archives (checked Oct 2026), so Python's zipfile
cannot open them; the central directory itself is intact, so it is walked directly and only the work_stop
entries are inflated.

Output: data/processed/street_works.csv, one row per completed permit (latest work_stop event wins). Columns:
city, permit_reference, work_reference, promoter, promoter_swa_code, activity_type, work_category,
works_location_type, traffic_management_type, street_name, usrn, actual_start, actual_end, lon, lat, length_m,
source_month. lon/lat is the midpoint of the works geometry (British National Grid in the source).

Street Manager starts in mid-2020 (first archive month 2020-07), so there is nothing for Southampton's 2015-19
labels. The archive is permits only: it says where a trench was opened and closed, not how well it was reinstated
(reinstatement inspections are not in the open data).
"""
from __future__ import annotations

import argparse
import json
import mmap
import os
import re
import struct
import tempfile
import time
import urllib.request
import zlib
from datetime import datetime, timezone

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "data", "processed", "street_works.csv")
URL = "https://opendata.manage-roadworks.service.gov.uk/permit/{y}/{m:02d}.zip"
SOURCE = "DfT Street Manager open data (permits), OGL v3"
# highway_authority as written in Street Manager -> our city name
CITIES = {b"SOUTHAMPTON CITY COUNCIL": "Southampton", b"BRIGHTON AND HOVE CITY COUNCIL": "Brighton",
          b"BRIGHTON & HOVE CITY COUNCIL": "Brighton"}
FIRST = (2020, 7)
COLS = ["city", "permit_reference", "work_reference", "promoter", "promoter_swa_code", "activity_type", "work_category",
        "works_location_type", "traffic_management_type", "street_name", "usrn", "actual_start", "actual_end",
        "lon", "lat", "length_m", "source_month"]


def _chain_ok(b, k: int, steps: int = 3) -> bool:
    for _ in range(steps):
        if b[k:k + 4] != b"PK":
            return False
        fnl, exl, cml = struct.unpack("<HHH", b[k + 28:k + 34])
        k += 46 + fnl + exl + cml
    return True


def central_directory(b) -> list[tuple[str, int, int, int]]:
    """(name, local header offset, compressed size, method) for every entry, read from the central directory.

    The archives have no end-of-central-directory record, so the directory is found as the first central record
    that points at offset 0 (the first entry) and is followed by more central records. The directory is about
    120 bytes per entry, so it sits in the last few hundred MB."""
    k, lo = -1, max(0, len(b) - 400_000_000)
    j = b.find(b"PK", lo)
    while j != -1:
        if struct.unpack("<I", b[j + 42:j + 46])[0] == 0 and _chain_ok(b, j):
            k = j
            break
        j = b.find(b"PK", j + 4)
    out = []
    while k >= 0 and k + 46 <= len(b) and b[k:k + 4] == b"PK":
        (_, _, _, _, comp, _, _, _, csz, _, fnl, exl, cml, _, _, _, off) = struct.unpack("<IHHHHHHIIIHHHHHII", b[k:k + 46])
        out.append((b[k + 46:k + 46 + fnl].decode("utf-8", "replace"), off, csz, comp))
        k += 46 + fnl + exl + cml
    return out


def read_entry(b, off: int, csz: int, comp: int) -> bytes:
    fnl, exl = struct.unpack("<HH", b[off + 26:off + 30])
    j = off + 30 + fnl + exl
    raw = b[j:j + csz]
    return zlib.decompress(raw, -15) if comp == 8 else bytes(raw)


def _bng_to_wgs84():
    from pyproj import Transformer
    return Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)


def midpoint(wkt: str, tf) -> tuple[float, float, float]:
    from shapely import wkt as W
    g = W.loads(wkt)
    p = g.interpolate(0.5, normalized=True) if g.length > 0 else g.centroid
    lon, lat = tf.transform(p.x, p.y)
    return round(lon, 6), round(lat, 6), round(float(g.length), 1)


def month_rows(path: str, ym: str, tf) -> list[dict]:
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as b:
        rows = []
        for name, off, csz, comp in central_directory(b):
            if "_work_stop_event_" not in name:
                continue
            data = read_entry(b, off, csz, comp)
            city = next((c for k, c in CITIES.items() if k in data), None)
            if city is None:
                continue
            o = json.loads(data).get("object_data", {})
            if o.get("highway_authority", "").upper().encode() not in CITIES or not o.get("works_location_coordinates"):
                continue
            try:
                lon, lat, length = midpoint(o["works_location_coordinates"], tf)
            except Exception:
                continue
            rows.append({"city": city, "permit_reference": o.get("permit_reference_number"),
                         "work_reference": o.get("work_reference_number"), "promoter": o.get("promoter_organisation"),
                         "promoter_swa_code": o.get("promoter_swa_code"), "activity_type": o.get("activity_type"),
                         "work_category": o.get("work_category"), "works_location_type": o.get("works_location_type"),
                         "traffic_management_type": o.get("traffic_management_type"), "street_name": o.get("street_name"),
                         "usrn": o.get("usrn"), "actual_start": o.get("actual_start_date_time"),
                         "actual_end": o.get("actual_end_date_time"), "lon": lon, "lat": lat, "length_m": length,
                         "source_month": ym})
        return rows


def fetch(url: str, dest: str, tries: int = 4):
    """curl when available (several times faster than urllib on these 1 GB files), else urllib."""
    import shutil
    import subprocess
    for k in range(tries):
        try:
            if shutil.which("curl"):
                subprocess.run(["curl", "-sSfL", "--retry", "3", "-A", "RoadSense hackathon (open data research)",
                                "-o", dest, url], check=True)
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "RoadSense hackathon (open data research)"})
                with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                    while chunk := r.read(1 << 22):
                        f.write(chunk)
            return
        except Exception as e:  # network blips: back off and retry
            if k == tries - 1:
                raise
            print(f"  retry {url}: {e}", flush=True)
            time.sleep(2 ** (k + 1))


def all_months(until: tuple[int, int] | None = None) -> list[str]:
    now = datetime.now(timezone.utc)
    end = until or ((now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12))
    y, m, out = *FIRST, []
    while (y, m) <= end:
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def latest_months(have: set[str], now: datetime | None = None, max_new: int = 2) -> list[str]:
    """Months after the newest one we have, up to last month, oldest first (at most max_new per run)."""
    if not have:
        return []
    now = now or datetime.now(timezone.utc)
    last = max(have)
    y, m = map(int, last.split("-"))
    out = []
    end = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    while True:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if (y, m) > end:
            break
        out.append(f"{y}-{m:02d}")
    return out[:max_new]


def update_linked(proc_dir: str, now: datetime | None = None, max_new: int = 2, verbose: bool = True) -> dict:
    """Daily refresh step: fetch any newly published month (DfT publishes the archive monthly), link its works to
    segments and append them to <proc_dir>/works_segments.parquet. A month not published yet is skipped quietly."""
    from roadsense.features import works_context as WC
    wp = os.path.join(proc_dir, "works_segments.parquet")
    if not os.path.exists(wp):
        return {"skipped": "no works_segments.parquet to extend"}
    store = pd.read_parquet(wp)
    have = set(store["source_month"].dropna().unique()) if "source_month" in store else set()
    todo = latest_months(have, now, max_new)
    if not todo:
        return {"new_months": [], "latest": max(have) if have else None}
    seg = pd.read_parquet(os.path.join(proc_dir, "segments.parquet"), columns=["segment_id", "city", "geometry", "owner", "highway"])
    seg = seg[(seg["owner"] != "national_highways") & (seg["highway"] != "motorway")]
    tf = _bng_to_wgs84()
    added, done = 0, []
    for ym in todo:
        y, m = map(int, ym.split("-"))
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            try:
                fetch(URL.format(y=y, m=m), tmp, tries=1)
            except Exception:  # noqa: BLE001 - 403/404 until DfT publishes the month
                break
            rows = month_rows(tmp, ym, tf)
        finally:
            os.remove(tmp)
        w = WC.snap_works(seg, pd.DataFrame(rows, columns=COLS))
        w = w[WC.LINKED_COLS].astype({"year": "Int64", "carriageway": bool})
        store = pd.concat([store[store["source_month"] != ym], w], ignore_index=True)
        store = store.sort_values("actual_end").drop_duplicates("permit_reference", keep="last")
        added += len(w)
        done.append(ym)
    if done:
        store.to_parquet(wp, index=False)
    return {"new_months": done, "works_added": added, "latest": max(have | set(done))}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", nargs="*", help="yyyy-mm (default: every month since 2020-07 not yet fetched)")
    ap.add_argument("--out", default=OUT, help="output csv (several runs can fill separate files, then --merge)")
    ap.add_argument("--merge", nargs="*", help="csv files to merge into --out")
    a = ap.parse_args()
    out_path = a.out
    if a.merge:
        df = pd.concat([pd.read_csv(p, dtype=str) for p in [out_path, *a.merge] if os.path.exists(p)], ignore_index=True)
        df = df.sort_values("actual_end").drop_duplicates("permit_reference", keep="last").sort_values(["city", "actual_end"])
        df.to_csv(out_path, index=False)
        print(f"merged {len(a.merge)} files: {len(df)} works, {df['source_month'].nunique()} months -> {out_path}")
        return
    old = pd.read_csv(out_path, dtype=str) if os.path.exists(out_path) else pd.DataFrame(columns=COLS)
    done = set(old["source_month"].unique()) | set(old.attrs.get("empty", []))
    months = a.months or [m for m in all_months() if m not in done]
    tf = _bng_to_wgs84()
    frames = [old]
    for ym in months:
        y, m = map(int, ym.split("-"))
        t = time.time()
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            fetch(URL.format(y=y, m=m), tmp)
            rows = month_rows(tmp, ym, tf)
        except urllib.error.HTTPError as e:
            print(f"{ym}: {e} (not published yet?)")
            continue
        finally:
            os.remove(tmp)
        frames = [f[f["source_month"] != ym] for f in frames if len(f)] + [pd.DataFrame(rows, columns=COLS)]
        df = pd.concat(frames, ignore_index=True)
        # a permit can stop more than once (work_stop_reverted, then stop again): keep the latest end
        df = df.sort_values("actual_end").drop_duplicates("permit_reference", keep="last").sort_values(["city", "actual_end"])
        df.to_csv(out_path, index=False)  # after every month, so a stopped run keeps what it has
        frames = [df]
        n = pd.Series([r["city"] for r in rows]).value_counts().to_dict()
        print(f"{ym}: {len(rows)} completed works {n} in {time.time() - t:.0f}s", flush=True)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
