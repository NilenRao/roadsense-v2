# Streetlight model evaluation

_Generated 2026-10-17T17:55:29+00:00 by `python -m roadsense.models.lights` (lights-v0.1). as_of 2026-10-12._

**The lamp register and fault history are SYNTHETIC** (Generators 3 and 4). The burn-hour clock is real: astral sunset to sunrise for Southampton, minus the council's 01:00 to 05:30 switch-off from 2023-12-01.

## Fitted Weibull lifetimes (burn hours)

| Unit type | Scale (h) | Shape | Units | Burnouts | Generator assumption |
|---|---|---|---|---|---|
| LED | 60,016 | 2.97 | 26,584 | 9,488 | scale 60,000, shape 3.0 |
| legacy | 28,447 | 6.92 | 11 | 11 | scale 20,000, shape 2.5 |

Recovering the generator's parameters is a pipeline check only.

## Back-test: failures in the 90 days after 2026-07-14

Refitted on data before the cutoff. 17,143 lamps, 291 with a fault in the window (base rate 0.017); the model expected 269.

| Scorer | Precision in top 5%, any fault | Precision in top 5%, burnouts only |
|---|---|---|
| Weibull on burn hours by lamp type | 0.039 | 0.034 |
| Baseline: install year only | 0.036 | 0.032 |
| Random (base rate) | 0.017 | 0.013 |

Almost every unit in service is an LED on the same burn-hour clock, so burn hours rank lamps much like install year does, and the model ranks no better than the baseline (the gap is within noise: about 20 hits in the top 5%). Its value here is the calibrated probability and the replacement date, not the ranking. Electrical and damage faults (2% a year, random) cap precision for any scorer.

## Current outputs

- Lamps scored: 17,143; mean P(fail in 90 days) 0.025.
- Due in 90 days (P >= 0.5 or replacement date within 90 days): 3,695.
- Replacement overdue (cumulative failure already >= 0.5): 3,403.
- Dark from open FixMyStreet reports in the last 30 days (real): 0.
- Currently dark (failed in the last 30 days, not yet repaired): 65.

## Real check: FixMyStreet street-lighting reports (Southampton, real data)

211 reports in `data/processed/fms_streetlights.csv`, by year: 2008: 2, 2009: 19, 2010: 9, 2011: 16, 2012: 24, 2013: 8, 2014: 21, 2015: 23, 2016: 36, 2017: 14, 2018: 23, 2019: 13, 2024: 1, 2025: 1, 2026: 1.
This is far below any plausible lamp fault rate (inferred: streetlight faults mostly go through the council's ENERVEO portal instead), so it cannot calibrate the synthetic fault generator; open reports join the currently dark set in the app.
