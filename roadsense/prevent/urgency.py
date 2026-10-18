"""Urgency (0-100) and band for every scored segment (M6).

urgency = 100 * min(1, C * (0.7 * P30 + 0.3 * wear/100)), C = product of consequence weights
capped at 1.5. A confirmed open pothole sets urgency to 100.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BAND_ORDER = ["critical", "high", "prevention", "monitor"]
BAND_LABEL = {"critical": "Critical (dispatch)", "high": "High (inspect)",
              "prevention": "Prevention window (treat before failure)", "monitor": "Monitor"}


def _flag(seg: pd.DataFrame, col: str) -> pd.Series:
    if col not in seg:
        return pd.Series(False, index=seg.index)
    s = seg[col]
    return (s.fillna(0).astype(float) > 0) if s.dtype != bool else s.fillna(False)


def consequence(seg: pd.DataFrame, cfg: dict) -> tuple[pd.Series, pd.Series]:
    """Return (C, text) per segment. Missing columns (cycle routes, schools) simply don't apply."""
    w = cfg["urgency"]["consequence"]
    parts = {
        "A-road": (seg["highway"].isin(cfg["urgency"]["a_road_highways"]), w["a_road"]),
        "bus route": (_flag(seg, "bus_route") | _flag(seg, "bus_routes") | _flag(seg, "bus_departures") | _flag(seg, "bus_stop"),
                      w["bus_route"]),
        "cycle route": (_flag(seg, "cycle_route"), w["cycle_route"]),
        "within 100 m of a school or hospital": (_flag(seg, "near_school_hospital"), w["school_or_hospital_100m"]),
    }
    C = pd.Series(1.0, index=seg.index)
    text = pd.Series([[] for _ in range(len(seg))], index=seg.index)
    for name, (m, f) in parts.items():
        C = C.where(~m, C * f)
        for i in seg.index[m]:
            text[i] = text[i] + [name]
    return C.clip(upper=w["cap"]), text.map(lambda t: ", ".join(t))


def urgency(p30, wear_pct, C, cfg: dict, confirmed_open=None) -> np.ndarray:
    u = cfg["urgency"]
    val = 100 * np.minimum(1, np.asarray(C, float) * (u["w_p30"] * np.asarray(p30, float)
                                                      + u["w_wear"] * np.nan_to_num(np.asarray(wear_pct, float)) / 100))
    if confirmed_open is not None:
        val = np.where(np.asarray(confirmed_open, bool), 100.0, val)
    return np.round(val, 1)


def band(u, cfg: dict) -> np.ndarray:
    b = cfg["urgency"]["bands"]
    u = np.asarray(u, float)
    return np.select([u >= b["critical"], u >= b["high"], u >= b["prevention"]],
                     ["critical", "high", "prevention"], "monitor")
