"""Weather feature counters (no network)."""
import pandas as pd

from roadsense.features.weather_feats import hourly_indicators, weekly_for_ward


def make_hours(temps, rain=None, start="2020-01-01"):
    n = len(temps)
    t = pd.date_range(start, periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"time": t, "temperature_2m": temps, "precipitation": rain or [0.0] * n,
                         "is_forecast": [False] * n})


def test_freeze_thaw_counts_each_crossing():
    ind = hourly_indicators(make_hours([2, 1, -1, -2, 1, 3, -1]))
    assert ind["cross"].sum() == 3          # +->- , ->+ , +->-


def test_rain_then_frost_needs_5mm_in_previous_48h():
    temps = [3] * 10 + [-1]                 # frost onset at hour 10
    wet = make_hours(temps, [0.0] * 5 + [6.0] + [0.0] * 5)
    dry = make_hours(temps, [0.0] * 5 + [4.0] + [0.0] * 5)
    assert hourly_indicators(wet)["rtf"].sum() == 1
    assert hourly_indicators(dry)["rtf"].sum() == 0


def test_no_leakage_week_start_hour_excluded():
    # 100 days at +5 °C, dry, then 10 mm in the very hour the week starts
    hours = 24 * 100
    h = make_hours([5.0] * hours, [0.0] * hours, start="2020-01-01")
    wk = weekly_for_ward(h)
    ws = pd.Timestamp(wk.week_start.iloc[-1])
    h.loc[h.time == ws, "precipitation"] = 10.0
    wk2 = weekly_for_ward(h)
    assert wk2.loc[wk2.week_start == ws, "rain_3d"].iloc[0] == 0     # the rain at week_start is not "before" it
