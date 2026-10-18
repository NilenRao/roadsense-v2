"""Recently treated flag for plan rows (DISPLAY ONLY: nothing here changes urgency, ranking or treatment).

A segment is flagged when data/processed/soton_resurfacing.csv (roadsense.ingest.soton_resurfacing) links it, by an
exact road+ward match or a hand-checked manual match, to carriageway work that the council lists as completed or in
progress within `years` before the plan's as-of date. Footway, drainage, iron works and pre-civils rows are ignored,
and so are planned or "identified" schemes. A 2023/24 scheme with no month is dated 31 March 2024, the end of that
financial year (the latest it can have been done); a month like "June 2026" is dated to the end of that month.
"""
from __future__ import annotations

import os
import re

import pandas as pd

CARRIAGEWAY = re.compile(r"carriageway resurfacing|surface treatment carriageway|carriageway reconstruction|roadmender|"
                         r"road resurfacing|road inlay|road (?:&|and) footway recon", re.I)
DONE = {"complete": "completed", "completed": "completed", "in progress": "in progress"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def work_date(date_text: str, year: str | None) -> pd.Timestamp | None:
    fy = re.search(r"(\d{4})/(\d{2})", str(year or ""))
    if fy:
        return pd.Timestamp(int(fy.group(1)) + 1, 3, 31)
    m = re.search(r"([A-Za-z]{3})[a-z]*\W*(\d{4,6})", str(date_text))  # "June 2026", "Feb-2026", typo "May 202026"
    if not m or m.group(1).lower() not in MONTHS:
        return None
    return pd.Timestamp(int(m.group(2)[-4:]), MONTHS[m.group(1).lower()], 1) + pd.offsets.MonthEnd(0)


def load(root: str) -> pd.DataFrame:
    p = os.path.join(root, "data", "processed", "soton_resurfacing.csv")
    if not os.path.exists(p):
        return pd.DataFrame()
    d = pd.read_csv(p)
    d = d[d["match_status"].isin(["matched", "manual"]) & d["work_type"].fillna("").str.contains(CARRIAGEWAY)]
    d = d.assign(state=d["status"].fillna("").str.strip().str.lower().map(DONE))
    d = d[d["state"].notna()].copy()
    d["done_by"] = [work_date(t, y) for t, y in zip(d["date_text"], d["year"])]
    return d[d["done_by"].notna()]


def recent(d: pd.DataFrame, as_of, years: int = 2) -> dict:
    """{segment_id: {...}} for the latest qualifying work in (as_of - years, as_of]."""
    if d.empty or not as_of:
        return {}
    t = pd.Timestamp(as_of)
    w = d[(d["done_by"] <= t + pd.offsets.MonthEnd(0)) & (d["done_by"] > t - pd.DateOffset(years=years))]
    out = {}
    for r in w.sort_values("done_by").itertuples():
        when = r.year if re.search(r"\d{4}/\d{2}", str(r.year)) else r.done_by.strftime("%b %Y")
        out[r.segment_id] = {"when": when, "done_by": str(r.done_by.date()), "work": r.work_type, "state": r.state,
                             "scheme": r.scheme, "source": r.source, "match": r.match_status,
                             "label": f"Treated {when}" if r.state == "completed" else f"Treatment in progress ({when})"}
    return out
