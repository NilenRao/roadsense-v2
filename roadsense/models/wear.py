"""Wear model (M5): Weibull survival of time from resurfacing to first recorded pothole.

Fitted with lifelines' WeibullAFTFitter on covariates log esal_day, freeze-thaw rate and
in_flood_area, using segments whose last resurfacing falls inside the pothole log window
(so "first pothole since resurfacing" is actually observable). Wear = 100 x F(current age).
Resurfacing history is SYNTHETIC for Southampton, so wear is too.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

COVARS = ["log_esal_day", "freeze_thaw_rate", "in_flood_area"]


def ward_freeze_thaw_rate(wx: pd.DataFrame) -> pd.Series:
    """Mean freeze-thaw crossings per year by ward (14-day windows tile the year ~26 times)."""
    w = wx.copy()
    w["week_start"] = pd.to_datetime(w["week_start"])
    # weekly rows overlap 14-day windows twice, so sum/2 per year
    yearly = w.groupby(["ward", w["week_start"].dt.year])["freeze_thaw_14d"].sum() / 2.0
    return yearly.groupby("ward").mean()


def survival_frame(seg: pd.DataFrame, wx: pd.DataFrame, res: pd.DataFrame, potholes: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    as_of = pd.Timestamp(cfg["as_of"])
    log_start = pd.Timestamp(cfg["potholes"]["start"])
    last = res[res["treatment"] == "last_resurfaced"].set_index("segment_id")["date"]
    df = seg[["segment_id", "ward", "esal_day", "in_flood_area"]].copy()
    df["resurfaced"] = pd.to_datetime(df["segment_id"].map(last))
    ph = potholes[["segment_id", "reported_date"]].copy()
    ph["reported_date"] = pd.to_datetime(ph["reported_date"])
    ph = ph.merge(df[["segment_id", "resurfaced"]], on="segment_id")
    first = ph[ph["reported_date"] > ph["resurfaced"]].groupby("segment_id")["reported_date"].min()
    df["first_pothole"] = df["segment_id"].map(first)
    df["event"] = df["first_pothole"].notna().astype(int)
    end = df["first_pothole"].fillna(as_of)
    df["years"] = ((end - df["resurfaced"]).dt.days / 365.25).clip(lower=1 / 52)
    df["log_esal_day"] = np.log1p(df["esal_day"].astype(float))
    df["freeze_thaw_rate"] = df["ward"].map(ward_freeze_thaw_rate(wx)).fillna(0.0)
    df["in_flood_area"] = df["in_flood_area"].astype(float)
    df["observable"] = df["resurfaced"] >= log_start
    df["age_years"] = ((as_of - df["resurfaced"]).dt.days / 365.25).clip(lower=0)
    return df


def fit(surv: pd.DataFrame):
    from lifelines import WeibullAFTFitter
    d = surv[surv["observable"]][COVARS + ["years", "event"]]
    m = WeibullAFTFitter(penalizer=0.01)
    m.fit(d, duration_col="years", event_col="event")
    return m


def wear_pct(model, surv: pd.DataFrame) -> np.ndarray:
    """100 x cumulative failure probability at each segment's current age since resurfacing."""
    X = surv[COVARS]
    lam = np.exp(model.params_.loc["lambda_"].reindex(["Intercept"] + COVARS).fillna(0).values
                 @ np.c_[np.ones(len(X)), X.values].T)
    rho = float(np.exp(model.params_.loc["rho_"].loc["Intercept"]))
    t = surv["age_years"].values
    return 100.0 * (1.0 - np.exp(-((t / lam) ** rho)))
