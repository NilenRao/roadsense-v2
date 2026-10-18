"""Context flags (no network)."""
import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from roadsense.features.context import flags, parse


def test_parse_and_flags():
    js = {"elements": [
        {"type": "node", "lat": 50.9000, "lon": -1.4000, "tags": {"amenity": "school", "name": "A"}},
        {"type": "relation", "members": [{"type": "way", "ref": 11}, {"type": "node", "ref": 5}]},
        {"type": "way", "id": 22},
    ]}
    sites, cycle = parse(js)
    assert len(sites) == 1 and cycle == {11, 22}
    seg = gpd.GeoDataFrame({"segment_id": ["a", "b"], "osm_way_id": [11, 33]},
                           geometry=[LineString([(-1.4001, 50.9001), (-1.3999, 50.9001)]),     # ~11 m from the school
                                     LineString([(-1.4200, 50.9200), (-1.4190, 50.9200)])],    # ~2.5 km away
                           crs=4326).to_crs(27700)
    out = flags(seg, sites, cycle)
    assert out.set_index("segment_id").loc["a"].tolist() == [True, True]
    assert out.set_index("segment_id").loc["b"].tolist() == [False, False]
