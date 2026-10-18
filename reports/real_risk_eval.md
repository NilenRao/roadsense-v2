# Real-label risk model (FixMyStreet, real data)

_Generated 2026-10-18T07:20:15+00:00 by `python -m roadsense.models.real_risk` (real-risk-v0.1)._

Labels: real FixMyStreet road-surface reports (`data\processed\fms_snapped.csv`), snapped to segments on all road classes within 25 m (is_main_road is a feature, not a filter). Counts measure reporting, not pothole rates; scores rank segments within one city and year.

Score: share of that year's reports on the top 10% of road length (mean over test years, 95% paired segment bootstrap).

## Test years per city (pooled model)

### Brighton

| Method | Capture, top 10% of road length | 95% interval |
|---|---|---|
| model | 31.8% | 27.9% to 35.6% |
| physical only | 21.2% | 17.7% to 24.6% |
| last year reports | 21.7% | 18.6% to 26.4% |
| load x past reports | 13.6% | 10.8% to 18.5% |
| model minus last-year (paired) | 10.1% | 5.6% to 12.9% |
| physical-only minus last-year (paired) | -0.5% | -7.0% to 3.6% |
| model minus physical-only (paired) | 10.7% | 6.3% to 15.0% |

n = 802 test reports on 8,179 test segments (2023, 2024, 2025). Model ahead of the last-year baseline in 100% of 1,000 resamples (seed 2026).

### Southampton

| Method | Capture, top 10% of road length | 95% interval |
|---|---|---|
| model | 37.1% | 29.7% to 43.9% |
| physical only | 10.4% | 7.1% to 14.3% |
| last year reports | 33.7% | 24.5% to 38.9% |
| load x past reports | 19.8% | 14.5% to 25.2% |
| model minus last-year (paired) | 3.4% | -1.0% to 11.5% |
| physical-only minus last-year (paired) | -23.3% | -29.3% to -13.0% |
| model minus physical-only (paired) | 26.7% | 18.8% to 34.0% |

n = 381 test reports on 8,145 test segments (2018, 2019). Model ahead of the last-year baseline in 94% of 1,000 resamples (seed 2026).

## Leave-one-city-out

### Brighton

| Method | Capture, top 10% of road length | 95% interval |
|---|---|---|
| model | 19.5% | 16.9% to 22.8% |
| physical only | 12.3% | 10.0% to 14.6% |
| last year reports | 16.5% | 12.9% to 21.2% |
| load x past reports | 16.6% | 13.4% to 20.0% |
| model minus last-year (paired) | 3.0% | -2.1% to 7.7% |
| physical-only minus last-year (paired) | -4.2% | -9.4% to -0.0% |
| model minus physical-only (paired) | 7.2% | 4.5% to 10.6% |

n = 1,512 test reports on 8,179 test segments (2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025). Model ahead of the last-year baseline in 89% of 1,000 resamples (seed 2026).

### Portsmouth

| Method | Capture, top 10% of road length | 95% interval |
|---|---|---|
| model | 19.9% | 13.2% to 27.3% |
| physical only | 15.2% | 9.2% to 21.6% |
| last year reports | 13.1% | 9.8% to 21.1% |
| load x past reports | 9.7% | 4.6% to 14.4% |
| model minus last-year (paired) | 6.8% | -3.6% to 13.8% |
| physical-only minus last-year (paired) | 2.1% | -8.1% to 8.7% |
| model minus physical-only (paired) | 4.7% | 0.8% to 9.3% |

n = 257 test reports on 6,010 test segments (2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025). Model ahead of the last-year baseline in 87% of 1,000 resamples (seed 2026).

### Southampton

| Method | Capture, top 10% of road length | 95% interval |
|---|---|---|
| model | 34.8% | 30.4% to 39.5% |
| physical only | 13.7% | 10.9% to 16.7% |
| last year reports | 33.5% | 27.2% to 36.7% |
| load x past reports | 18.3% | 15.0% to 21.9% |
| model minus last-year (paired) | 1.3% | -0.9% to 6.0% |
| physical-only minus last-year (paired) | -19.8% | -24.1% to -12.1% |
| model minus physical-only (paired) | 21.1% | 16.0% to 26.2% |

n = 819 test reports on 8,145 test segments (2016, 2017, 2018, 2019). Model ahead of the last-year baseline in 94% of 1,000 resamples (seed 2026).

## Frozen 2026 check (Jan to Oct, small)

### Brighton

| Method | Capture, top 10% of road length | 95% interval |
|---|---|---|
| model | 30.0% | 13.0% to 50.0% |
| physical only | 20.0% | 6.4% to 35.3% |
| last year reports | 16.7% | 3.3% to 27.3% |
| load x past reports | 13.3% | 2.9% to 26.7% |
| model minus last-year (paired) | 13.3% | -2.9% to 36.4% |
| physical-only minus last-year (paired) | 3.3% | -12.5% to 24.0% |
| model minus physical-only (paired) | 10.0% | -8.6% to 30.8% |

n = 30 test reports on 8,179 test segments (2026). Model ahead of the last-year baseline in 94% of 1,000 resamples (seed 2026).

### Portsmouth

| Method | Capture, top 10% of road length | 95% interval |
|---|---|---|
| model | 21.4% | 9.7% to 37.5% |
| physical only | 19.0% | 7.5% to 33.3% |
| last year reports | 26.2% | 2.9% to 27.0% |
| load x past reports | 21.4% | 8.3% to 36.7% |
| model minus last-year (paired) | -4.8% | -6.7% to 23.8% |
| physical-only minus last-year (paired) | -7.1% | -9.1% to 19.6% |
| model minus physical-only (paired) | 2.4% | -4.9% to 13.9% |

n = 42 test reports on 6,010 test segments (2026). Model ahead of the last-year baseline in 83% of 1,000 resamples (seed 2026).

### Southampton

| Method | Capture, top 10% of road length | 95% interval |
|---|---|---|
| model | 21.2% | 7.7% to 36.0% |
| physical only | 15.2% | 2.7% to 29.7% |
| last year reports | 12.1% | 2.8% to 26.9% |
| load x past reports | 21.2% | 8.3% to 40.7% |
| model minus last-year (paired) | 9.1% | -7.4% to 24.0% |
| physical-only minus last-year (paired) | 3.0% | -16.1% to 20.0% |
| model minus physical-only (paired) | 6.1% | -13.9% to 25.7% |

n = 33 test reports on 8,145 test segments (2026). Model ahead of the last-year baseline in 78% of 1,000 resamples (seed 2026).
