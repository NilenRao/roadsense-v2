# Building sites and utility digs: held-out check

_Generated 2026-10-18T05:12:03+00:00 by `python -m roadsense.models.context_eval`._

Question: does knowing about nearby building sites, or recent utility digs on a road, help rank where next year's pothole reports land, on years the model never saw? Labels are real FixMyStreet reports; inputs are the councils' brownfield land registers (planning.data.gov.uk) and DfT Street Manager completed works (both OGL). Score: share of the year's reports on the top 10% of road length, model with vs without the features, same training rows, paired segment bootstrap (1,000 resamples, seed 2026). FixMyStreet counts measure reporting, not true pothole rates.

Both feature sets are in the ranking model (roadsense.models.real_risk FEATURES) at the team's request (18 Oct). A gain is called clear when its 95% interval is above 0 in at least one city and below 0 in none; until then the page says the gain is not yet clear.

## Construction: in the model (team decision); no clear held-out gain yet

Features: con_sites_3y, con_dwellings_3y. Training rows with a value: 130,796.

| City (test years) | Without | With | Gain | 95% interval | Ahead in | Reports |
|---|---|---|---|---|---|---|
| Brighton (2023, 2024, 2025) | 31.2% | 29.3% | -1.9% | -4.0% to 1.9% | 25% | 802 |
| Southampton (2018, 2019) | 41.6% | 39.2% | -2.4% | -5.1% to 0.4% | 4% | 381 |

- Brighton: 1,704 flagged test segment-years, 64 with a report; report rate 3.8% flagged vs 2.7% unflagged, 1.09x within the same model-risk decile.
- Southampton: 245 flagged test segment-years, 7 with a report; report rate 2.9% flagged vs 1.9% unflagged, 1.68x within the same model-risk decile.

## Utility works: in the model (team decision); no clear held-out gain yet

Features: uw_carriageway_1y, uw_any_1y, uw_carriageway_2y. Training rows with a value: 8,179.

| City (test years) | Without | With | Gain | 95% interval | Ahead in | Reports |
|---|---|---|---|---|---|---|
| Brighton (2023, 2024, 2025) | 31.2% | 27.4% | -3.8% | -6.8% to -0.5% | 1% | 802 |
| Southampton | | | | no data for this city's test years | | |

- Brighton: 5,514 flagged test segment-years, 312 with a report; report rate 5.7% flagged vs 1.9% unflagged, 2.01x within the same model-risk decile.

## Both: in the model (team decision); no clear held-out gain yet

Features: con_sites_3y, con_dwellings_3y, uw_carriageway_1y, uw_any_1y, uw_carriageway_2y. Training rows with a value: 130,796.

| City (test years) | Without | With | Gain | 95% interval | Ahead in | Reports |
|---|---|---|---|---|---|---|
| Brighton (2023, 2024, 2025) | 31.2% | 31.8% | 0.6% | -2.2% to 3.7% | 70% | 802 |
| Southampton (2018, 2019) | 41.6% | 37.1% | -4.5% | -7.4% to -1.1% | 0% | 381 |

- Brighton: 1,704 flagged test segment-years, 64 with a report; report rate 3.8% flagged vs 2.7% unflagged, 1.09x within the same model-risk decile.
- Southampton: 245 flagged test segment-years, 7 with a report; report rate 2.9% flagged vs 1.9% unflagged, 1.68x within the same model-risk decile.

## Limits

- Street Manager starts in July 2020, so utility digs can only be checked on Brighton's 2023-25 test years (Southampton's FixMyStreet labels stop in 2019) and the model learns them from Brighton 2022 rows only.
- The registers list sites with a point, not a boundary or access road, and Southampton's register on planning.data.gov.uk has 37 sites last updated in 2017. Planning applications and council-house building programmes are not published as open data for either city (DATA_GAPS.md).
- Street Manager says where a trench was opened and closed, not how well it was reinstated.
