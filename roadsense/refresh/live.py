"""The data/live/ folder: dated refresh builds plus a `current` pointer (a text file, not a symlink:
symlinks need admin rights on Windows). Imported by the app, so no heavy dependencies here.

    data/live/
        current                  one line: the folder name of the build the app uses
        refresh.log              one line per run (ok / failed + error)
        20261018T0600Z/          same layout as the repo root:
            data/processed/...   segments, weather, FixMyStreet labels (refreshed copies)
            outputs/...          risk, plan, lights, lane_c/triage_*.json
            config.yaml          lane A config copy (read by real_risk via root)
            refresh.json         what ran, when, counts

ROADSENSE_LIVE_DIR overrides the location (tests); ROADSENSE_LIVE=0 makes the app ignore it.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEEP_DAYS = 7
STALE_HOURS = 36


def live_dir() -> Path:
    return Path(os.environ.get("ROADSENSE_LIVE_DIR") or ROOT / "data" / "live")


def current_build(base: Path | None = None) -> Path | None:
    """The build folder `current` points at, if it exists and looks complete; else None."""
    if os.environ.get("ROADSENSE_LIVE", "1") == "0":
        return None
    base = base or live_dir()
    ptr = base / "current"
    if not ptr.is_file():
        return None
    name = ptr.read_text(encoding="utf-8").strip()
    build = base / name
    if not name or not (build / "refresh.json").is_file() or not (build / "data" / "processed" / "segments.parquet").is_file():
        return None
    return build


def build_info(build: Path) -> dict:
    return json.loads((build / "refresh.json").read_text(encoding="utf-8"))


def switch_current(base: Path, build: Path) -> None:
    """Atomically point `current` at build (write a temp file, then os.replace)."""
    tmp = base / "current.tmp"
    tmp.write_text(build.name + "\n", encoding="utf-8")
    os.replace(tmp, base / "current")


def prune(base: Path, keep: int = KEEP_DAYS) -> list[str]:
    """Delete build folders beyond the newest `keep` (never the current one). Returns removed names."""
    cur = current_build(base)
    builds = sorted([p for p in base.iterdir() if p.is_dir() and p.name[:8].isdigit()], key=lambda p: p.name)
    removed = []
    for p in builds[:-keep] if len(builds) > keep else []:
        if cur is not None and p.resolve() == cur.resolve():
            continue
        shutil.rmtree(p, ignore_errors=True)
        removed.append(p.name)
    return removed


def log(base: Path, message: str) -> None:
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(base / "refresh.log", "a", encoding="utf-8") as f:
        f.write(f"{stamp} {message}\n")


def data_updated(snapshot_time: str | None, now: datetime | None = None) -> dict:
    """What the app header shows: live build time if there is one, else the committed snapshot's time.
    stale = older than STALE_HOURS."""
    now = now or datetime.now(timezone.utc)
    build = current_build()
    if build is not None:
        ts, source = build_info(build).get("finished_at"), "live"
    else:
        ts, source = snapshot_time, "snapshot"
    age_h = None
    if ts:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        age_h = round((now - t).total_seconds() / 3600, 1)
    return {"timestamp": ts, "source": source, "age_h": age_h,
            "stale": age_h is not None and age_h > STALE_HOURS, "stale_after_h": STALE_HOURS,
            "build": build.name if build is not None else None}
