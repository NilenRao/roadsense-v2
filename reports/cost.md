# Running cost of report triage (Gemini)

_Generated 2026-10-17T20:56:36+00:00 by `python -m roadsense.agent.cost` from `outputs/lane_c/token_log.jsonl`. Token counts are what the Gemini API returned in `usage_metadata` for each call (triage through `POST /reports`, the demo, or `Service.triage_report`)._

## Measured token counts

| FixMyStreet report | Model that answered | Input tokens | Output tokens | Thinking tokens |
| --- | --- | --- | --- | --- |
| fms-10054415 | gemini-3-flash-preview | 910 | 100 | 0 |
| fms-9996017 | gemini-3-flash-preview | 921 | 106 | 0 |
| fms-9913130 | gemini-3-flash-preview | 926 | 104 | 0 |
| fms-9864063 | gemini-3-flash-preview | 892 | 90 | 0 |
| fms-9815538 | gemini-3-flash-preview | 899 | 93 | 0 |
| fms-9658382 | gemini-3-flash-preview | 911 | 103 | 0 |
| fms-9374920 | gemini-3-flash-preview | 900 | 98 | 0 |
| fms-9313325 | gemini-3-flash-preview | 743 | 90 | 0 |
| fms-9291752 | gemini-3-flash-preview | 910 | 99 | 0 |
| fms-9187098 | gemini-3-flash-preview | 919 | 106 | 0 |

Mean per triaged report (10 reports): **893 input tokens** and **99 output tokens**, 1 API call(s) each.

Other triage calls in the log (not used for the headline):

- `gemini-3.8-flash`: 1 report(s), mean 3,254 in / 194 out, 4 call(s) each, £0.00238 each.

## Cost

Price of `gemini-3-flash-preview`, paid tier: **$0.50 per 1M input tokens, $3.00 per 1M output tokens (incl. thinking)**. Source: Google AI for Developers, Gemini API pricing, https://ai.google.dev/gemini-api/docs/pricing (page "last updated 2026-10-16 UTC", read 17 Oct 2026).

```
£ per report = (input tokens x $0.50 + output tokens x $3.00) / 1,000,000 x 0.75 £/$
             = (893 x 0.50 + 99 x 3.00) / 1,000,000 x 0.75 = £0.000557
```

- **£0.00056 per triaged report** (about £0.56 per 1,000 reports).
- **Under £3 a year even at Southampton's full yearly repair volume** (3,113 to 4,162 pothole **repairs** a year, 2021-25: £1.74 to £2.32). These are council repairs, not reports: an upper bound that assumes every repair came in as a triaged report. Source: Blackcircles pothole report 2026 (council FOI), https://www.blackcircles.com/news/pothole-report-uk, 18 Mar 2026.
- **Yearly cost, FixMyStreet only: £0.02**: 40 Southampton FixMyStreet road-surface **reports** in the 12 months to the latest one in the cached pull. Most residents now report through the council's own system, whose volume is not public.
- Duplicates within 15 m and 14 days reuse the original's triage with no Gemini call, so real call counts are lower.

## Per officer per month (assumptions)

£0.00056 x 4,162 (upper end, repairs) / 12 / 4 officers = **£0.05 LLM** + £25 hosting / 4 = £6.25 -> **£6.30 per officer per month**.

## Not measured (assumptions)

- Exchange rate 0.75 £/$ (assumption, not a quoted rate).
- 4 officers and £25 a month for one small VM running the API and page.
- Prices are list prices for the paid tier; the free tier costs £0 but is rate-limited (5 requests a minute per model) and Google may use the prompts to improve its products, so only public reports and synthetic data are sent.
- `gemini-flash-latest` (last in the fallback list) is not on the pricing page, so calls it answers are not priced.
