"""Building sites and utility digs. Small fixtures, no network."""
from __future__ import annotations

import io
import zipfile

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from roadsense.features import works_context as WC
from roadsense.ingest import streetworks as SW

M_LAT = 1 / 111_320


def _seg():
    # two short east-west roads 200 m apart in Southampton
    lat0, lon0 = 50.91, -1.40
    rows = []
    for i, dy in enumerate([0, 200]):
        lat = lat0 + dy * M_LAT
        rows.append({"segment_id": f"S{i}", "city": "Southampton", "lat": lat, "lon": lon0,
                     "geometry": f"LINESTRING ({lon0 - 0.0005} {lat}, {lon0 + 0.0005} {lat})"})
    return pd.DataFrame(rows), lat0, lon0


def test_sites_link_within_radius_only():
    seg, lat0, lon0 = _seg()
    sites = pd.DataFrame({"source": "brownfield_register", "city": "Southampton", "reference": ["A"],
                          "lat": [lat0 + 60 * M_LAT], "lon": [lon0]})
    L = WC.link_sites(seg, sites, radius_m=100)
    assert set(L["segment_id"]) == {"S0"} and L["dist_m"].iloc[0] == pytest.approx(60, abs=3)


def test_yearly_features_use_only_earlier_years():
    seg, lat0, lon0 = _seg()
    works = pd.DataFrame({"city": "Southampton", "permit_reference": ["p1", "p2", "p3"],
                          "works_location_type": ["Carriageway", "Footway", "Carriageway"],
                          "actual_end": ["2022-05-01T10:00:00Z", "2022-06-01T10:00:00Z", "2023-03-01T10:00:00Z"],
                          "lat": lat0, "lon": lon0})
    w = WC.snap_works(seg, works)
    assert (w["segment_id"] == "S0").all() and w["carriageway"].tolist() == [True, False, True]
    rows = pd.DataFrame({"segment_id": ["S0", "S0", "S0", "S1"], "city": "Southampton", "year": [2021, 2023, 2024, 2023]})
    sites = pd.DataFrame({"source": "brownfield_register", "city": "Southampton", "reference": ["A"],
                          "permission_date": ["2022-02-01"], "max_net_dwellings": [40.0]})
    links = pd.DataFrame({"city": "Southampton", "reference": ["A"], "segment_id": ["S1"], "dist_m": [20.0]})
    y = WC.yearly(rows, sites, links, w)
    # 2021 looks back to 2020: before the first full Street Manager year, so unknown
    assert np.isnan(y.loc[0, "uw_carriageway_1y"])
    # 2023 sees 2022's carriageway dig only; the 2023 dig is not visible until 2024
    assert y.loc[1, "uw_carriageway_1y"] == 1 and y.loc[1, "uw_any_1y"] == 2
    assert y.loc[2, "uw_carriageway_1y"] == 1 and y.loc[2, "uw_carriageway_2y"] == 2
    # a site permitted in 2022 counts for 2023 rows, on the linked segment only
    assert y.loc[3, "con_sites_3y"] == 1 and y.loc[3, "con_dwellings_3y"] == 40 and y.loc[1, "con_sites_3y"] == 0


def test_current_flags_window():
    seg, lat0, lon0 = _seg()
    works = WC.snap_works(seg, pd.DataFrame({"city": "Southampton", "permit_reference": ["old", "new"],
                                             "works_location_type": "Carriageway", "promoter": "WATER CO",
                                             "activity_type": "Utility repair and maintenance works",
                                             "actual_end": ["2024-01-01T00:00:00Z", "2026-09-01T00:00:00Z"],
                                             "lat": lat0, "lon": lon0}))
    sites = pd.DataFrame({"source": ["brownfield_register", "osm_construction"], "city": "Southampton",
                          "reference": ["A", "osm:way/1"], "site_address": ["Old Mill", "Quay"], "name": None,
                          "lat": lat0, "lon": lon0, "max_net_dwellings": [12.0, np.nan], "hectares": [0.2, np.nan],
                          "permission_date": ["2025-01-10", None], "permission_type": [None, "residential"],
                          "public_land": [True, False], "end_date": [None, None]})
    links = WC.link_sites(seg, sites, radius_m=50)
    c = WC.current("Southampton", "2026-10-12", sites, links, works)
    assert c["S0"]["works"]["count"] == 1 and c["S0"]["works"]["carriageway"] == 1
    assert c["S0"]["construction"]["count"] == 2 and "S1" not in c
    assert {s["source"] for s in WC.live_sites("Southampton", "2026-10-12", sites)} == {"brownfield_register", "osm_construction"}


def test_streetworks_reads_archive_without_end_record():
    ev = lambda ha, kind, ref: ('{"event_type":"%s","object_data":{"permit_reference_number":"%s","highway_authority":"%s",'
                                '"works_location_coordinates":"LINESTRING(442000 112000,442010 112000)",'
                                '"works_location_type":"Carriageway","actual_end_date_time":"2024-01-02T10:00:00.000Z"}}'
                                % (kind, ref, ha))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a_work_stop_event_1.json", ev("SOUTHAMPTON CITY COUNCIL", "WORK_STOP", "a"))
        z.writestr("b_work_stop_event_2.json", ev("LONDON BOROUGH OF BEXLEY", "WORK_STOP", "b"))
        z.writestr("c_permit_granted_event_3.json", ev("SOUTHAMPTON CITY COUNCIL", "PERMIT_GRANTED", "c"))
    raw = buf.getvalue()
    raw = raw[:raw.rfind(b"PK\x05\x06")]          # drop the end-of-central-directory record, like the DfT files
    p = __import__("tempfile").mktemp(suffix=".zip")
    open(p, "wb").write(raw)
    rows = SW.month_rows(p, "2024-01", SW._bng_to_wgs84())
    assert [r["permit_reference"] for r in rows] == ["a"] and rows[0]["city"] == "Southampton"
    assert 50.8 < rows[0]["lat"] < 51.0 and rows[0]["length_m"] == 10.0


def test_context_endpoints(tmp_path):
    from roadsense.api.main import create_app
    from roadsense.api.service import Service
    app = TestClient(create_app(Service(root=tmp_path)))
    assert app.get("/context/evaluation").status_code == 200
    assert app.get("/context/sites").json()["sites"] == []


def test_streetworks_new_months_only_after_latest():
    from datetime import datetime, timezone
    have = {"2026-08", "2026-09"}
    assert SW.latest_months(have, datetime(2026, 10, 18, tzinfo=timezone.utc)) == []
    assert SW.latest_months(have, datetime(2026, 11, 2, tzinfo=timezone.utc)) == ["2026-10"]
    assert SW.latest_months(have, datetime(2027, 3, 1, tzinfo=timezone.utc)) == ["2026-10", "2026-11"]  # at most 2 a run
