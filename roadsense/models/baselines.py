"""Simple baselines the risk model must beat (M5): road age only, prior pothole count only."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression


def age_score(df: pd.DataFrame) -> np.ndarray:
    d = df["days_since_resurfacing"].astype(float)
    return d.fillna(d.max() + 1 if d.notna().any() else 0).values


def prior_count_score(df: pd.DataFrame) -> np.ndarray:
    return df["potholes_seg_12m"].astype(float).values


BASELINES = {"road_age_only": age_score, "prior_pothole_count_only": prior_count_score}


class CalibratedBaseline:
    """Turns a baseline score into a probability with isotonic regression fitted on training rows."""

    def __init__(self, name: str):
        self.name = name
        self.score = BASELINES[name]
        self.iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)

    def fit(self, df: pd.DataFrame, y) -> "CalibratedBaseline":
        self.iso.fit(self.score(df), np.asarray(y))
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return self.iso.predict(self.score(df))
