"""Small, network-free fixtures shared by lane B tests."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roadsense.synthetic.common import load_config  # noqa: E402


def small_config() -> dict:
    cfg = copy.deepcopy(load_config())
    cfg["as_of"] = "2025-06-02"
    cfg["mock"].update({"grid_spacing_m": 1500, "n_wards": 5, "weather_start": "2023-01-02"})
    cfg["resurfacing"].update({"n_events": 60, "events_start": "2023-01-01"})
    cfg["potholes"].update({"start": "2023-01-02", "yearly_totals": {2023: 600, 2024: 600}, "current_year_rate_from": 2024,
                            "bisection_iters": 25})
    cfg["streetlights"].update({"spacing_m": 150, "faults_start": "2023-01-01"})
    cfg["risk"].update({"lgbm": {**cfg["risk"]["lgbm"], "n_estimators": 40}, "holdout_months": 4, "spatial_holdout_wards": 1})
    return cfg
