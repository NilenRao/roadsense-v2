"""Shared helpers for lane B: config, input loading (lane A outputs or mocks), labelling."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "synthetic.yaml"


def load_config(path: Path | str | None = None) -> dict:
    with open(path or CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve(cfg: dict, key: str, root: Path | None = None) -> Path:
    return Path(root or ROOT) / cfg["paths"][key]


def source_tag(generator: str, cfg: dict) -> str:
    return f"synthetic:{generator}@{cfg['version']}"


def label_synthetic(df: pd.DataFrame, generator: str, cfg: dict) -> pd.DataFrame:
    df = df.copy()
    df["is_synthetic"] = True
    df["source"] = source_tag(generator, cfg)
    return df


# --- local metric projection (good enough at city scale) -------------------
LAT0 = 50.92
M_PER_DEG_LAT = 110_540.0
M_PER_DEG_LON = 111_320.0 * np.cos(np.radians(LAT0))


def to_xy(lon, lat):
    return np.asarray(lon) * M_PER_DEG_LON, np.asarray(lat) * M_PER_DEG_LAT


def to_lonlat(x, y):
    return np.asarray(x) / M_PER_DEG_LON, np.asarray(y) / M_PER_DEG_LAT


# --- inputs ------------------------------------------------------------------
SEGMENT_COLS = ["segment_id", "osm_way_id", "name", "highway", "ward", "length_m", "geometry",
                "lat", "lon", "bus_stop", "junction", "in_flood_area", "owner", "esal_day"]
WEATHER_COLS = ["ward", "week_start", "rain_3d", "rain_7d", "rain_30d", "rain_90d", "hours_below_0_7d",
                "freeze_thaw_14d", "freeze_thaw_30d", "rain_then_frost_7d", "hot_days_7d"]


def _read_any(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)


def _normalise_segments(seg: pd.DataFrame) -> pd.DataFrame:
    """Coerce lane A segments into the columns lane B needs."""
    seg = seg.copy()
    if "lat" not in seg or "lon" not in seg:
        from shapely import wkb, wkt
        geoms = [wkb.loads(g) if isinstance(g, (bytes, bytearray)) else wkt.loads(g) for g in seg["geometry"]]
        cents = [g.interpolate(0.5, normalized=True) for g in geoms]
        seg["lon"] = [c.x for c in cents]
        seg["lat"] = [c.y for c in cents]
    for col, default in [("bus_stop", False), ("junction", False), ("in_flood_area", False), ("owner", "council")]:
        if col not in seg:
            seg[col] = default
    if "is_motorway" in seg and "owner" in seg:
        seg.loc[seg["is_motorway"].astype(bool), "owner"] = "national_highways"
    seg["segment_id"] = seg["segment_id"].astype(str)
    return seg


def load_inputs(cfg: dict, root: Path | None = None) -> dict:
    """Return segments (with esal_day) and weekly ward weather.

    Uses lane A outputs in data/processed/ when present, otherwise deterministic mocks
    (written once to data/synthetic/mock_inputs/ and flagged with input_source='mock').
    """
    proc = resolve(cfg, "processed_dir", root)
    base = Path(root or ROOT)

    def rel(path: Path) -> str:
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            return path.as_posix()
    seg_path = proc / "segments.parquet"
    wx_path = next((p for p in [proc / "weather_weekly.parquet", proc / "weather_weekly.csv"] if p.exists()), None)
    load_path = next((p for p in [proc / "load.parquet", proc / "esal_day.parquet", proc / "load.csv"] if p.exists()), None)
    sources = {}
    if seg_path.exists():
        seg = _normalise_segments(_read_any(seg_path))
        if cfg.get("city") and "city" in seg:          # synthetic council data and weekly risk: target city only
            seg = seg[seg["city"] == cfg["city"]]
        sources["segments"] = rel(seg_path)
    else:
        from roadsense.synthetic.mock import mock_segments
        seg = mock_segments(cfg)
        sources["segments"] = "mock"
    if "esal_day" not in seg:
        if load_path is not None:
            ld = _read_any(load_path)[["segment_id", "esal_day"]]
            ld["segment_id"] = ld["segment_id"].astype(str)
            seg = seg.merge(ld, on="segment_id", how="left")
            sources["esal_day"] = rel(load_path)
        else:
            from roadsense.synthetic.mock import mock_esal
            seg["esal_day"] = mock_esal(seg, cfg)
            sources["esal_day"] = "mock"
    else:
        sources.setdefault("esal_day", sources["segments"])
    seg["esal_day"] = seg["esal_day"].fillna(seg["esal_day"].median())
    if wx_path is not None:
        wx = _read_any(wx_path)
        wx["week_start"] = pd.to_datetime(wx["week_start"])
        if cfg.get("city") and "city" in wx:
            wx = wx[wx["city"] == cfg["city"]].reset_index(drop=True)
        sources["weather"] = rel(wx_path)
    else:
        from roadsense.synthetic.mock import mock_weekly_weather
        wx = mock_weekly_weather(cfg, sorted(seg["ward"].unique()))
        sources["weather"] = "mock"
    return {"segments": seg.reset_index(drop=True), "weather": wx, "sources": sources}


def scored(seg: pd.DataFrame) -> pd.DataFrame:
    """Segments we score: council roads only (motorways belong to National Highways)."""
    return seg[(seg["owner"] != "national_highways") & (seg["highway"] != "motorway")].reset_index(drop=True)


def road_class(highway: pd.Series, cfg: dict) -> pd.Series:
    m = {h: k for k, hs in cfg["road_class"].items() for h in hs}
    return highway.map(m).fillna("residential")


def neighbours(seg: pd.DataFrame, radius_m: float) -> list[np.ndarray]:
    """Graph-neighbour proxy: segments whose midpoints lie within radius_m."""
    from scipy.spatial import cKDTree
    x, y = to_xy(seg["lon"].values, seg["lat"].values)
    tree = cKDTree(np.c_[x, y])
    nb = tree.query_ball_point(np.c_[x, y], r=radius_m)
    return [np.array([j for j in lst if j != i], dtype=int) for i, lst in enumerate(nb)]


def write_meta(path: Path, **kw):
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **kw}
    path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")


def weeks(start: str, end: str) -> pd.DatetimeIndex:
    """Mondays from start (inclusive) to end (exclusive)."""
    s = pd.Timestamp(start)
    s = s - pd.Timedelta(days=s.weekday())
    return pd.date_range(s, pd.Timestamp(end) - pd.Timedelta(days=1), freq="W-MON")
