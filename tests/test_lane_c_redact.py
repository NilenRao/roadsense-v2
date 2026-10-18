"""Lane C: redaction of house numbers, postcodes and names in FixMyStreet titles. No network."""
import pandas as pd
import pytest

from roadsense.agent.triage import keyword_classify
from roadsense.prevent.redact_titles import main, redact, redact_frame


@pytest.mark.parametrize("before, after", [
    ("Pothole outside number 66 Ashtree Road", "Pothole outside number [number] Ashtree Road"),
    ("Manhole outside of 21 Mowbray Road", "Manhole outside of [number] Mowbray Road"),
    ("Drain cover outsice 64 peartree avenue", "Drain cover outsice [number] peartree avenue"),
    ("Potholes 21-23 Mowbray Road", "Potholes [number] Mowbray Road"),
    ("Deep pothole in road between 8 and 10 Bassett Crescent West",
     "Deep pothole in road between [number] and [number] Bassett Crescent West"),
    ("Pothole in middle of road between No.13 and No.16", "Pothole in middle of road between No. [number] and No. [number]"),
    ("Flat 3 entrance pothole", "Flat [number] entrance pothole"),
    ("Street Light Not Working outside 91 SO19 8ES", "Street Light Not Working outside [number] [postcode]"),
    ("Pot hole outside 26 mill roaf", "Pot hole outside [number] mill roaf"),
    ("Mr R C Richards", "[name]"),
    ("Mrs Smith reported this", "[name] reported this"),
])
def test_redacts_addresses_and_names(before, after):
    assert redact(before) == after


@pytest.mark.parametrize("title", [
    "Very deep pothole on Marine Dr A259", "Pothole on A33 near M271", "50mm deep pot hole", "3-4 potholes please",
    "2x Long, Wide, Unavoidable Potholes, Lower Brownhill Road.", "Pot hole on the 2 lanes of Bassett Avenue",
    "3 large potholes on Bitterne Road", "2 foot square pothole", "pothole outside 2 ft wide", "Pot hole 3ft by 2 ft",
])
def test_leaves_road_names_sizes_and_counts(title):
    assert redact(title) == title


def test_triage_category_unchanged_by_redaction():
    for t in ["Pothole outside number 66 Ashtree Road", "Manhole outside of 21 Mowbray Road",
              "Street Light Not Working outside 91 SO19 8ES", "Loose paving slab outside 8 Margery Road"]:
        assert keyword_classify(redact(t)) == keyword_classify(t)


def test_frame_report_and_cli_write(tmp_path, capsys):
    df = pd.DataFrame({"id": [1, 2], "title": ["Pothole outside 12 Hill Lane", "Pothole on A33"]})
    red, ch = redact_frame(df)
    assert ch["id"].tolist() == [1] and red["title"].tolist() == ["Pothole outside [number] Hill Lane", "Pothole on A33"]
    f = tmp_path / "x.csv"
    df.to_csv(f, index=False)
    main([str(f)])  # dry run: file untouched
    assert pd.read_csv(f)["title"].iloc[0] == "Pothole outside 12 Hill Lane"
    main([str(f), "--write"])
    assert pd.read_csv(f)["title"].iloc[0] == "Pothole outside [number] Hill Lane"
    assert "1 titles changed" in capsys.readouterr().out
