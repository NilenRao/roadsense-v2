# Report triage against a keyword baseline

_Generated 2026-10-17T12:16:53+00:00 by `python -m roadsense.agent.triage`._

100 real Southampton FixMyStreet reports, title text only ({'pothole': 74, 'other': 26}). Labels: `roadsense/agent/eval/triage_labels.csv`, hand-labelled by Claude Code from the titles (pothole = defect in the carriageway surface; other = pavement, kerb, drain, cycle path, flooding or unclear). A teammate should spot-check them. The pull holds road-surface categories only, so there are no streetlight reports in the sample.

| Method | Accuracy | Recall per class |
| --- | --- | --- |
| keyword baseline | 0.91 | pothole 0.89, other 0.96 |

Gemini has not been run yet (no `GEMINI_API_KEY`). Only the baseline is shown.
