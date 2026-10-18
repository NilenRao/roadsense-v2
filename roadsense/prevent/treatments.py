"""Treatment rules and treatment cost (M6). Thresholds and £/m² are config ASSUMPTIONS.

- confirmed open pothole        -> permanent repair
- low wear with cracking        -> crack sealing
- prevention-window wear        -> surface dressing
- high wear                     -> patch now, schedule resurfacing
- very high wear                -> resurface
Treatment codes follow docs/contract_outputs.md section 9.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ROAD_CLASS = {"trunk": "A", "primary": "A", "trunk_link": "A", "primary_link": "A",
              "secondary": "BC", "tertiary": "BC", "secondary_link": "BC", "tertiary_link": "BC"}


def road_class(highway: pd.Series) -> pd.Series:
    return highway.map(ROAD_CLASS).fillna("residential")


def choose(wear_pct, cracking, confirmed_open, cfg: dict) -> np.ndarray:
    t = cfg["treatments"]
    w = np.nan_to_num(np.asarray(wear_pct, float))
    cracking = np.asarray(cracking, bool)
    return np.select(
        [np.asarray(confirmed_open, bool), w >= t["resurface_wear_min"], w >= t["high_wear_min"], w >= t["low_wear_max"],
         cracking],
        ["permanent_repair", "resurface", "patch", "surface_dressing", "crack_sealing"],
        # low wear and no cracking seen: crack sealing is the cheapest preventive option, flagged
        "crack_sealing")


def treatment_cost(treatment: pd.Series, length_m: pd.Series, highway: pd.Series, cfg: dict) -> pd.Series:
    t, c = cfg["treatments"], cfg["costs"]
    width = road_class(highway).map(t["lane_width_m"]).astype(float)
    area = length_m.astype(float) * width
    per = treatment.map(t["cost_per_m2"]).astype(float)
    cost = area * per
    # patching costs a patch visit now, resurfacing is scheduled separately (shown, not summed)
    cost = cost.where(treatment != "patch", t["patch_area_m2"] * t["cost_per_m2"]["patch"])
    cost = cost.where(treatment != "permanent_repair", c["planned_repair"])
    return cost.round(2)


def life_years(treatment: pd.Series, cfg: dict) -> np.ndarray:
    return treatment.map(cfg["treatments"]["life_years"]).fillna(0).astype(float).values


def expected_avoided(p30_mean, treatment: pd.Series, cfg: dict) -> np.ndarray:
    """Expected reactive repairs avoided over the treatment's service life (config life_years).

    Monthly hazard h = -ln(1 - p30); potholes expected over L years = h * 12L; the treatment multiplies
    hazard by m, so avoided = h * 12L * (1 - m). Hazard is held at today's level (assumption).
    """
    t = cfg["treatments"]
    h = -np.log1p(-np.clip(np.asarray(p30_mean, float), 0, 0.999))
    m = treatment.map(t["hazard_multiplier"]).fillna(1.0).astype(float).values
    return h * 12 * life_years(treatment, cfg) * (1 - m)
