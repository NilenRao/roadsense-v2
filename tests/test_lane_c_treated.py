import pandas as pd

from roadsense.api import treated as T


def test_work_date_parses_months_typos_and_financial_years():
    assert T.work_date("June 2026", "2026") == pd.Timestamp("2026-06-30")
    assert T.work_date("May 202026", "2026") == pd.Timestamp("2026-05-31")  # typo on the council page
    assert T.work_date("Feb-2026", "2026") == pd.Timestamp("2026-02-28")
    assert T.work_date("2023/24 - use of additional funding", "2023/24") == pd.Timestamp("2024-03-31")
    assert T.work_date("TBC", None) is None


def _rows():
    return pd.DataFrame([
        {"segment_id": "A", "done_by": pd.Timestamp("2026-05-31"), "year": "2026", "work_type": "Carriageway resurfacing",
         "state": "completed", "scheme": "A Road", "source": "s", "match_status": "matched"},
        {"segment_id": "B", "done_by": pd.Timestamp("2024-03-31"), "year": "2023/24", "work_type": "Road resurfacing",
         "state": "completed", "scheme": "B Road", "source": "s", "match_status": "manual"},
        {"segment_id": "C", "done_by": pd.Timestamp("2026-07-31"), "year": "2026", "work_type": "Carriageway Reconstruction",
         "state": "in progress", "scheme": "C Road", "source": "s", "match_status": "matched"},
    ])


def test_recent_keeps_two_years_before_as_of():
    r = T.recent(_rows(), "2026-10-12")
    assert set(r) == {"A", "C"}  # B (March 2024) is more than 2 years before 12 Oct 2026
    assert r["A"]["label"] == "Treated May 2026"
    assert r["C"]["label"].startswith("Treatment in progress")
    assert set(T.recent(_rows(), "2026-03-01", years=3)) == {"B"}  # work dated after the as-of month is not counted
