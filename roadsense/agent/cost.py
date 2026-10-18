"""Running cost of report triage (M8): reads the real token log and writes reports/cost.md.

    python -m roadsense.agent.cost

Token counts come only from outputs/lane_c/token_log.jsonl (what Gemini reported for each call). Each call is
priced at the published price of the model that answered it (PRICES below). The exchange rate, yearly volumes,
officers and hosting are config ASSUMPTIONS and are printed as such.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from roadsense.prevent import inputs as I
from roadsense.prevent.redact_titles import redact

# Paid-tier list prices, USD per 1M tokens (input text, output incl. thinking).
PRICE_SOURCE = ("Google AI for Developers, Gemini API pricing, https://ai.google.dev/gemini-api/docs/pricing "
                "(page \"last updated 2026-10-16 UTC\", read 17 Oct 2026)")
PRICES = {
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.8-flash": (0.75, 3.75),   # until 31 Dec 2026; $1.50 / $7.50 from 1 Jan 2027
    "gemini-2.5-flash": (0.30, 2.50),
}
# gemini-flash-latest is not listed on that page: calls answered by it are shown but not priced.

# Southampton pothole REPAIRS a year (council FOI, Blackcircles pothole report 2026, published 18 Mar 2026; verified
# 17 Oct 2026). These are repairs, not reports: the yearly cost is given as an upper bound over this range.
REPAIRS = {2021: 4002, 2022: 3972, 2023: 3627, 2024: 4162, 2025: 3113}
REPAIRS_SOURCE = "Blackcircles pothole report 2026 (council FOI), https://www.blackcircles.com/news/pothole-report-uk, 18 Mar 2026"
REPAIRS_PER_YEAR = max(REPAIRS.values())  # upper end used for the bound and the per-officer figure


def read_log(cfg: dict, root: Path | None = None) -> pd.DataFrame:
    p = I.resolve(cfg, "state_dir", root) / "token_log.jsonl"
    if not p.exists():
        return pd.DataFrame(columns=["ts", "kind", "model", "ref", "prompt_tokens", "output_tokens", "thoughts_tokens",
                                     "total_tokens", "calls"])
    return pd.DataFrame([json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()])


def fms_reports_per_year(cfg: dict, root: Path | None = None) -> int | None:
    """Southampton FixMyStreet road-surface reports in the 12 months up to the latest one in the cached pull."""
    f = I.load_fms(cfg, root)
    if not len(f):
        return None
    last = f["requested"].max()
    return int((f["requested"] > last - pd.Timedelta(days=365)).sum())


def price_gbp(model: str, inp: float, out: float, fx: float) -> float | None:
    if model not in PRICES:
        return None
    pi, po = PRICES[model]
    return (inp * pi + out * po) / 1e6 * fx


def compute(cfg: dict, log: pd.DataFrame, root: Path | None = None) -> dict:
    c = cfg["cost"]
    fx = c["usd_to_gbp"]
    out = {"n_calls_logged": len(log), "per_model": {}}
    tri = log[log["kind"] == "triage"] if len(log) else log
    for model, g in (tri.groupby("model") if len(tri) else []):
        inp = float(g["prompt_tokens"].mean())
        outp = float((g["output_tokens"] + g.get("thoughts_tokens", 0).fillna(0)).mean())
        out["per_model"][model] = {"n": len(g), "input_tokens": inp, "output_tokens": outp,
                                   "calls": float(g["calls"].mean()) if "calls" in g else None,
                                   "gbp": price_gbp(model, inp, outp, fx)}
    priced = {m: v for m, v in out["per_model"].items() if v["gbp"] is not None}
    if not priced:
        return out
    model = max(priced, key=lambda m: priced[m]["n"])  # the model that answered most triaged reports
    per_report = priced[model]["gbp"]
    fms_year = fms_reports_per_year(cfg, root)
    out.update(model=model, gbp_per_report=per_report, repairs_per_year=REPAIRS_PER_YEAR, fms_reports_per_year=fms_year,
               gbp_per_year_repairs=per_report * REPAIRS_PER_YEAR,
               gbp_per_year_fms=per_report * fms_year if fms_year is not None else None)
    host = c["hosting_gbp_per_month"] / c["officers"]
    llm = per_report * REPAIRS_PER_YEAR / 12 / c["officers"]
    out.update(llm_gbp_per_officer_month=llm, hosting_gbp_per_officer_month=host, total_gbp_per_officer_month=llm + host)
    return out


def write_cost_report(cfg: dict | None = None, root: Path | None = None) -> Path:
    cfg = cfg or I.load_config()
    log = read_log(cfg, root)
    r = compute(cfg, log, root)
    c = cfg["cost"]
    fx = c["usd_to_gbp"]
    L = ["# Running cost of report triage (Gemini)", "",
         f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by `python -m roadsense.agent.cost` from "
         "`outputs/lane_c/token_log.jsonl`. Token counts are what the Gemini API returned in `usage_metadata` for each call "
         "(triage through `POST /reports`, the demo, or `Service.triage_report`)._", ""]
    if "model" not in r:
        L += ["**Nothing measured yet:** no priced Gemini triage call is in the log. Add `GEMINI_API_KEY` to `.env`, triage "
              "some reports, then run `python -m roadsense.agent.cost`.", ""]
    else:
        m = r["model"]
        tri = log[(log["kind"] == "triage") & (log["model"] == m)]
        L += ["## Measured token counts", "",
              f"| FixMyStreet report | Model that answered | Input tokens | Output tokens | Thinking tokens |",
              "| --- | --- | --- | --- | --- |"]
        for row in tri.to_dict("records"):
            L.append(f"| {row.get('ref')} | {m} | {row['prompt_tokens']} | {row['output_tokens']} | "
                     f"{int(row.get('thoughts_tokens') or 0)} |")
        v = r["per_model"][m]
        L += ["", f"Mean per triaged report ({v['n']} reports): **{v['input_tokens']:,.0f} input tokens** and "
              f"**{v['output_tokens']:,.0f} output tokens**, {v['calls']:.0f} API call(s) each.", ""]
        others = {k: x for k, x in r["per_model"].items() if k != m}
        if others:
            L += ["Other triage calls in the log (not used for the headline):", ""]
            for k, x in others.items():
                cost = f"£{x['gbp']:.5f} each" if x["gbp"] is not None else "price not published"
                L.append(f"- `{k}`: {x['n']} report(s), mean {x['input_tokens']:,.0f} in / {x['output_tokens']:,.0f} out, "
                         f"{x['calls']:.0f} call(s) each, {cost}.")
            L.append("")
        pi, po = PRICES[m]
        L += ["## Cost", "",
              f"Price of `{m}`, paid tier: **${pi:.2f} per 1M input tokens, ${po:.2f} per 1M output tokens (incl. thinking)**. "
              f"Source: {PRICE_SOURCE}.", "",
              "```",
              f"£ per report = (input tokens x ${pi:.2f} + output tokens x ${po:.2f}) / 1,000,000 x {fx} £/$",
              f"             = ({v['input_tokens']:,.0f} x {pi:.2f} + {v['output_tokens']:,.0f} x {po:.2f}) / 1,000,000 x {fx}"
              f" = £{r['gbp_per_report']:.6f}",
              "```", "",
              f"- **£{r['gbp_per_report']:.5f} per triaged report** (about £{r['gbp_per_report'] * 1000:.2f} per 1,000 reports).",
              f"- **Under £{math.ceil(r['gbp_per_year_repairs']):.0f} a year even at Southampton's full yearly repair volume** "
              f"({min(REPAIRS.values()):,} to {max(REPAIRS.values()):,} pothole **repairs** a year, 2021-25: "
              f"£{r['gbp_per_report'] * min(REPAIRS.values()):.2f} to £{r['gbp_per_year_repairs']:.2f}). These are council "
              f"repairs, not reports: an upper bound that assumes every repair came in as a triaged report. Source: {REPAIRS_SOURCE}.",
              (f"- **Yearly cost, FixMyStreet only: £{r['gbp_per_year_fms']:.2f}**: {r['fms_reports_per_year']} Southampton "
               "FixMyStreet road-surface **reports** in the 12 months to the latest one in the cached pull. Most residents now "
               "report through the council's own system, whose volume is not public."
               if r.get("gbp_per_year_fms") is not None else "- FixMyStreet report volume: no cached pull."),
              "- Duplicates within 15 m and 14 days reuse the original's triage with no Gemini call, so real call counts are lower.",
              "",
              "## Per officer per month (assumptions)", "",
              f"£{r['gbp_per_report']:.5f} x {r['repairs_per_year']:,.0f} (upper end, repairs) / 12 / {c['officers']} officers = "
              f"**£{r['llm_gbp_per_officer_month']:.2f} LLM** + £{c['hosting_gbp_per_month']} hosting / {c['officers']} = "
              f"£{r['hosting_gbp_per_officer_month']:.2f} -> **£{r['total_gbp_per_officer_month']:.2f} per officer per month**.", ""]
    L += ["## Not measured (assumptions)", "",
          f"- Exchange rate {fx} £/$ (assumption, not a quoted rate).",
          f"- {c['officers']} officers and £{c['hosting_gbp_per_month']} a month for one small VM running the API and page.",
          "- Prices are list prices for the paid tier; the free tier costs £0 but is rate-limited (5 requests a minute per "
          "model) and Google may use the prompts to improve its products, so only public reports and synthetic data are sent.",
          "- `gemini-flash-latest` (last in the fallback list) is not on the pricing page, so calls it answers are not priced.", ""]
    p = I.resolve(cfg, "reports_dir", root) / "cost.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(L), encoding="utf-8")
    return p


def write_triage_report(res: dict, cfg: dict | None = None, root: Path | None = None) -> Path:
    """Triage against the keyword baseline, appended to reports/cost.md's folder as triage_vs_keywords.md."""
    cfg = cfg or I.load_config()
    L = ["# Report triage against a keyword baseline", "",
         f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by `python -m roadsense.agent.triage`._", "",
         f"{res['n']} real Southampton FixMyStreet reports, title text only ({res['labels']}). Labels: "
         "`roadsense/agent/eval/triage_labels.csv`, hand-labelled by Claude Code from the titles (pothole = defect in the "
         "carriageway surface; other = pavement, kerb, drain, cycle path, flooding or unclear). A teammate should spot-check them. "
         "The pull holds road-surface categories only, so there are no streetlight reports in the sample.", "",
         "| Method | Accuracy | Recall per class |", "| --- | --- | --- |"]
    for name in ["keyword_baseline", "gemini"]:
        if name in res:
            r = res[name]
            L.append(f"| {name.replace('_', ' ')} | {r['accuracy']:.2f} | "
                     + ", ".join(f"{k} {v:.2f}" for k, v in r["per_class_recall"].items()) + " |")
    if "gemini" not in res:
        L += ["", "Gemini has not been run yet (no `GEMINI_API_KEY`). Only the baseline is shown."]
    else:
        L += ["", f"Gemini model {res['gemini']['model']}, mean {res['gemini']['mean_tokens_per_report']:,.0f} tokens per report.",
              "", "## Disagreements", "", "| FMS id | Title | Label | Keyword | Gemini |", "| --- | --- | --- | --- | --- |"]
        for d in res.get("disagreements", []):
            L.append(f"| {d['fms_id']} | {redact(str(d['title'])).replace('|', '/')} | {d['label']} | {d['keyword']} | {d['gemini']} |")
    p = I.resolve(cfg, "reports_dir", root) / "triage_vs_keywords.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return p


if __name__ == "__main__":
    path = write_cost_report()
    print(path.read_text(encoding="utf-8"))
