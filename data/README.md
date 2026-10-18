# Data

`fms_dedup.csv` — FixMyStreet road-surface reports for Southampton, Portsmouth and Brighton,
de-duplicated (same defect = within 20 m and 30 days). Free-text descriptions removed.

- Source: FixMyStreet (mySociety), Open311 GeoReport v2 API. Pulled 17 Oct 2026.
- Regenerate with `python pull_fixmystreet.py` (or `py pull_fixmystreet.py`).
- `fms_raw.csv` is deliberately not in the repo (contains free text).

Known gaps: Southampton has no reports 2020–2022; Brighton road reports almost stop in 2026.
Counts reflect FixMyStreet use, not true pothole rates.

`fms_roads_clean.csv` — same columns, pavement/footway defects removed by `clean_labels.py`. **Use this one for training.**

Columns: city, id, category, requested, updated, status, lat, lon, title, surface_keyword
