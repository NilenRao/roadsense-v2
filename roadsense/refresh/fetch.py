"""Network steps of the daily refresh: FixMyStreet (incremental) and the Open-Meteo forecast.

Every HTTP call goes through `get_json(url) -> dict`, so tests pass a fake and nothing touches the
network. Never calls OpenStreetMap, the EA flood service, MapIt/ONS wards or DfT.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from roadsense.features.weather_feats import weekly_for_ward
from roadsense.ingest import fixmystreet as F
from roadsense.ingest import weather as W
from roadsense.prevent.redact_titles import redact

GITHUB_URL = "https://github.com/ArmaanF-dot/Pothole-simulator-"


def user_agent() -> str:
    """"RoadSense/1.0 (SotonShip26 hackathon; <contact>)": contact from ROADSENSE_CONTACT in .env, else the
    project's GitHub URL. No contact details are written in the code."""
    try:
        from dotenv import load_dotenv
        from roadsense.refresh.live import ROOT
        load_dotenv(ROOT / ".env")                       # never overrides a variable already set
    except ImportError:
        pass
    contact = os.environ.get("ROADSENSE_CONTACT", "").strip() or GITHUB_URL
    return f"RoadSense/1.0 (SotonShip26 hackathon; {contact})"

MAX_REQUESTS = 1000                      # Open311 cap per request
CLEAN_COLS = ["city", "id", "category", "requested", "updated", "status", "lat", "lon", "title",
              "surface_keyword", "in_window"]


def http_get_json(url: str, tries: int = 4, pause_s: float = 1.5) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    err = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r)
            time.sleep(pause_s)                                    # be polite to both APIs
            return out
        except Exception as e:                                     # noqa: BLE001 - retried, then re-raised
            err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"giving up on {url.split('?')[0]}: {err}")


# --- FixMyStreet ---------------------------------------------------------------------------------
def _fms_window(get_json, cfg_a: dict, agency: str, cat: str, start: datetime, end: datetime) -> list[dict]:
    """All reports in [start, end): one request, split in half while the 1000-row cap is hit."""
    params = {"jurisdiction_id": "fixmystreet", "agency_responsible": agency, "service_code": cat,
              "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end_date": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "max_requests": MAX_REQUESTS}
    rows = get_json(cfg_a["fixmystreet"]["base_url"] + "?" + urllib.parse.urlencode(params)).get("service_requests", [])
    if len(rows) >= MAX_REQUESTS and end - start > timedelta(hours=1):
        mid = start + (end - start) / 2
        return _fms_window(get_json, cfg_a, agency, cat, start, mid) + _fms_window(get_json, cfg_a, agency, cat, mid, end)
    return rows


def pull_new_reports(get_json, cfg_a: dict, city: str, since: datetime, now: datetime) -> list[dict]:
    """Raw reports for the city's road categories requested at or after `since` (UTC)."""
    c = cfg_a["cities"][city]
    words = cfg_a["fixmystreet"]["surface_words"]
    out, seen = [], set()
    for cat in c["fms_road_categories"]:
        for r in _fms_window(get_json, cfg_a, c["fms_agency"], cat, since, now + timedelta(minutes=1)):
            rid = r.get("service_request_id")
            if rid in seen:
                continue
            seen.add(rid)
            text = ((r.get("title") or "") + " " + (r.get("description") or "")).lower()
            out.append({"city": city, "id": rid, "category": r.get("service_name"),
                        "requested": r.get("requested_datetime"), "updated": r.get("updated_datetime"),
                        "status": r.get("status"), "lat": r.get("lat"), "lon": r.get("long"),
                        "title": (r.get("title") or "").replace("\n", " "),
                        "description": (r.get("description") or "").replace("\n", " ")[:500],
                        "surface_keyword": any(w in text for w in words)})
    return sorted(out, key=lambda r: r["requested"] or "")


def clean_new_reports(cfg_a: dict, raw: list[dict], stored: pd.DataFrame) -> pd.DataFrame:
    """Same rules as roadsense.ingest.fixmystreet: road-surface reports only, de-duplicated (per city,
    against the stored labels too), pavement reports dropped. The description is used for the pavement
    rule and then discarded; every title goes through redact() before it is stored."""
    known = set(pd.to_numeric(stored["id"], errors="coerce").dropna().astype(int))
    raw = [r for r in raw if r["id"] is not None and int(r["id"]) not in known]
    if not raw:
        return pd.DataFrame(columns=CLEAN_COLS)
    surface = F.road_surface(cfg_a, raw)
    max_m, max_days = cfg_a["fixmystreet"]["dedup_m"], cfg_a["fixmystreet"]["dedup_days"]
    recent = stored.assign(_t=pd.to_datetime(stored["requested"], utc=True, format="mixed"))
    kept = []
    for r in surface:
        t = pd.Timestamp(r["requested"]).tz_convert("UTC") if pd.Timestamp(r["requested"]).tzinfo else \
            pd.Timestamp(r["requested"]).tz_localize("UTC")
        p = (float(r["lat"]), float(r["lon"]))
        prev = recent[(recent["city"] == r["city"]) & (recent["_t"] >= t - pd.Timedelta(days=max_days)) & (recent["_t"] <= t)]
        prior = [(float(a), float(b)) for a, b in zip(prev["lat"], prev["lon"])] + \
                [k["_p"] for k in kept if k["city"] == r["city"] and (t - k["_t"]).days <= max_days]
        if any(F.haversine_m(p, q) <= max_m for q in prior):
            continue
        if F.classify(r["title"], r["description"]) != "road":
            continue
        kept.append({**r, "_t": t, "_p": p})
    rows = []
    for r in kept:
        row = {k: r[k] for k in CLEAN_COLS if k in r}
        row["title"] = redact(r["title"])
        row["in_window"] = F.in_window(cfg_a, r)
        rows.append(row)
    return pd.DataFrame(rows, columns=CLEAN_COLS)


def recheck_status(stored: pd.DataFrame, raw: list[dict], city: str, window_start: datetime) -> tuple[pd.DataFrame, dict]:
    """Update status/updated of the city's stored reports requested inside the open window from the
    upstream rows. A stored report missing upstream (hidden or deleted) keeps its status and is counted."""
    req = pd.to_datetime(stored["requested"], utc=True, format="mixed")
    win = (stored["city"] == city) & (req >= pd.Timestamp(window_start))
    up = {int(r["id"]): r for r in raw if r.get("id") is not None}
    out = stored.copy()
    c = {"in_window": int(win.sum()), "open_to_closed": 0, "closed_to_open": 0, "not_returned": 0}
    for i in stored.index[win]:
        r = up.get(int(stored.at[i, "id"]))
        if r is None:
            c["not_returned"] += 1
            continue
        old, new = str(stored.at[i, "status"]), str(r["status"])
        if new != old:
            c["open_to_closed" if new == "closed" else "closed_to_open"] += 1
            out.at[i, "status"] = new
            out.at[i, "updated"] = r["updated"]
    c["changed"] = c["open_to_closed"] + c["closed_to_open"]
    return out, c


def refresh_reports(get_json, cfg_a: dict, stored: pd.DataFrame, cities: list[str], now: datetime,
                    open_days: int = 120) -> tuple[pd.DataFrame, dict]:
    """stored + new cleaned reports for `cities`, and the current status of every stored report inside the
    open window. One date-window pull per city and category covers both: it starts at the earlier of the
    latest stored report and the start of the open window, so it returns the window's reports with their
    status as of now."""
    req = pd.to_datetime(stored["requested"], utc=True, format="mixed")
    window_start = now - timedelta(days=open_days)
    new_frames, counts = [], {}
    for city in cities:
        latest = req[stored["city"] == city].max()
        since = min(latest.to_pydatetime() if pd.notna(latest) else window_start, window_start)
        raw = pull_new_reports(get_json, cfg_a, city, since, now)
        stored, status = recheck_status(stored, raw, city, window_start)
        new = clean_new_reports(cfg_a, raw, stored)
        counts[city] = {"since": since.isoformat(timespec="seconds"), "pulled": len(raw), "new_labels": len(new),
                        "status_recheck": status}
        new_frames.append(new)
    new_all = pd.concat([f for f in new_frames if len(f)], ignore_index=True) if any(len(f) for f in new_frames) else None
    out = stored if new_all is None else pd.concat([stored, new_all.astype({"id": stored["id"].dtype})], ignore_index=True)
    return out, counts


# --- Open-Meteo ----------------------------------------------------------------------------------
PAST_DAYS = 92                            # forecast API maximum; covers the 90-day windows of the forecast weeks


def forecast_weeks(get_json, cfg_a: dict, city: str, now: datetime, processed_dir) -> pd.DataFrame:
    """Weekly features for the weeks starting after today, one ward at a time, from the Open-Meteo
    forecast endpoint (past_days back-fills the history the 90-day windows need). Ward points come from
    the committed data/processed/ward_points_<city>.csv (never data/raw/, which is not committed)."""
    from roadsense.features.ward_points import read as ward_points
    frames = []
    retrieved = now.isoformat(timespec="minutes")
    for _, r in ward_points(processed_dir, city).iterrows():
        q = {"latitude": r.lat, "longitude": r.lon, "hourly": ",".join(W.HOURLY), "timezone": "GMT",
             "forecast_days": 16, "past_days": PAST_DAYS}
        h = W.to_frame(get_json(W.FORECAST + "?" + urllib.parse.urlencode(q)), r.ward, city, True)
        h["is_forecast"] = h["time"] > pd.Timestamp(now)
        wk = weekly_for_ward(h)
        wk = wk[wk["week_start"] > pd.Timestamp(now)]
        wk.insert(0, "ward", r.ward)
        wk.insert(0, "city", city)
        wk["data_date"] = str(h["time"].max())[:16]
        wk["retrieved_at"] = retrieved
        frames.append(wk)
    out = pd.concat(frames, ignore_index=True)
    out["week_start"] = out["week_start"].dt.tz_localize(None).dt.date.astype(str)
    out["source"] = "open-meteo"
    return out


def merge_weather(weekly: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Replace (ward, week_start) rows of the weekly file with the fresh ones and add new weeks."""
    key = ["ward", "week_start"]
    keep = weekly.merge(fresh[key], on=key, how="left", indicator=True)
    keep = weekly[(keep["_merge"] == "left_only").values]
    out = pd.concat([keep, fresh[weekly.columns]], ignore_index=True)
    return out.sort_values(["city", "ward", "week_start"], kind="stable").reset_index(drop=True)
