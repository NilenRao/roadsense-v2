"""Report triage agent (M8): Gemini with function calling and image input, plus a keyword baseline.

For each public report: classify (pothole, streetlight, other), snap to a segment or lamp, merge
duplicates within 15 m and 14 days, take urgency from the risk model, and draft a work order as
JSON for a human to approve. Only public FixMyStreet text and synthetic data are sent to Gemini
(spec rule 8). Every call's token counts go to the token log for reports/cost.md.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.prevent import inputs as I

CATEGORIES = ["pothole", "streetlight", "other"]

# --- keyword baseline -------------------------------------------------------------------------
_LIGHT = re.compile(r"street\s*light|streetlight|lamp|lighting|light (is )?(out|off|not working)|dark", re.I)
_OTHER = re.compile(r"pavement|footway|footpath|\bpath\b|paving|slab|kerb|curb|drain|gull(e)?y|flood|bin|cycle\s*(path|lane|way)|"
                    r"verge|manhole|crossing|sign|graffiti|fly.?tip", re.I)
_POTHOLE = re.compile(r"pot\s*-?\s*holes?|pothhole|porthole|crater|\bhole\b|road surface|carriageway|uneven road|"
                      r"sunken|rut|surface", re.I)


def keyword_classify(text: str) -> str:
    t = text or ""
    if _LIGHT.search(t):
        return "streetlight"
    road = bool(re.search(r"\broad\b|carriageway|roundabout|junction", t, re.I))
    if _POTHOLE.search(t) and (road or not _OTHER.search(t)):
        return "pothole"
    if _OTHER.search(t):
        return "other"
    return "other"


# --- tools the agent can call -----------------------------------------------------------------
TOOLS = [
    {"name": "lookup_location",
     "description": "Find the council road segment and nearest street lamp for a report location, with whether it is "
                    "inside Southampton coverage or on a National Highways road.",
     "parameters": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
                    "required": ["lat", "lon"]}},
    {"name": "find_duplicates",
     "description": "List open reports within 15 m and 14 days of this report that may be the same defect.",
     "parameters": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"},
                                                     "reported_at": {"type": "string", "description": "ISO date"}},
                    "required": ["lat", "lon", "reported_at"]}},
    {"name": "get_risk",
     "description": "Risk model scores for a road segment: urgency 0-100, band, P30, wear and the top reasons.",
     "parameters": {"type": "object", "properties": {"segment_id": {"type": "string"}}, "required": ["segment_id"]}},
    {"name": "draft_work_order",
     "description": "Final step. Submit the triage decision and a draft work order for officer approval.",
     "parameters": {"type": "object", "properties": {
         "category": {"type": "string", "enum": CATEGORIES},
         "confidence": {"type": "number", "description": "0 to 1"},
         "asset_type": {"type": "string", "enum": ["segment", "lamp", "none"]},
         "asset_id": {"type": "string", "description": "segment_id or lamp_id, empty if none"},
         "duplicate_of": {"type": "string", "description": "report id this duplicates, empty if none"},
         "hazard": {"type": "boolean", "description": "true if the text describes an immediate danger to road users"},
         "summary": {"type": "string", "description": "one sentence for the crew"},
         "recommended_action": {"type": "string"}},
         "required": ["category", "confidence", "asset_type", "asset_id", "duplicate_of", "hazard", "summary",
                      "recommended_action"]}},
]

SYSTEM = (
    "You triage public FixMyStreet reports for Southampton City Council's highways officers. "
    "Classify each report as pothole (a defect in the road carriageway surface), streetlight, or other "
    "(pavements, kerbs, drains, cycle paths, signs, anything else or unclear). Call lookup_location, then "
    "find_duplicates, then get_risk for a road segment, then draft_work_order exactly once. Use only tool "
    "results for ids. Do not give safety advice; the council's reporting page is linked automatically.")


@dataclass
class Context:
    """What the tools see: scored segments, lamps, and reports already in the queue."""
    cfg: dict
    scored: pd.DataFrame
    lights: pd.DataFrame
    queue: list[dict] = field(default_factory=list)
    wards: dict | None = None

    def __post_init__(self):
        self.scored = self.scored.reset_index(drop=True)
        self.index = I.SegmentIndex(self.scored)
        self.council = (self.scored["owner"] != "national_highways").values & (self.scored["highway"] != "motorway").values

    # tools -------------------------------------------------------------
    def lookup_location(self, lat: float, lon: float) -> dict:
        t = self.cfg["triage"]
        if not I.in_coverage(lon, lat, self.cfg, self.wards):
            return {"in_coverage": False, "status": "out_of_coverage",
                    "message": "Outside Southampton coverage (not in any of the city's 17 wards)" if self.wards
                    else "Outside Southampton coverage"}
        j_any, d_any = self.index.nearest(lon, lat)
        if self.wards is None and d_any > self.cfg["coverage"].get("max_road_distance_m", 1e9):
            return {"in_coverage": False, "status": "out_of_coverage",
                    "message": f"Outside Southampton coverage (no city road within {self.cfg['coverage']['max_road_distance_m']} m)"}
        if j_any is not None and d_any <= t["snap_radius_m"] and not self.council[j_any]:
            r = self.scored.iloc[j_any]
            return {"in_coverage": True, "status": "national_highways", "segment_id": r["segment_id"],
                    "name": r["name"], "message": "National Highways road, not scored"}
        j, d = self.index.nearest(lon, lat, mask=self.council)
        out = {"in_coverage": True, "status": "ok"}
        if j is not None and d <= t["snap_radius_m"]:
            r = self.scored.iloc[j]
            out.update(segment_id=r["segment_id"], name=r["name"], ward=r["ward"], segment_distance_m=round(d, 1))
        else:
            out.update(segment_id=None, message=f"no council road segment within {t['snap_radius_m']} m")
        if len(self.lights):
            dl = I.distance_m(self.lights["lon"].values, self.lights["lat"].values, lon, lat)
            k = int(np.argmin(dl))
            if dl[k] <= t["lamp_radius_m"]:
                out.update(lamp_id=self.lights.iloc[k]["lamp_id"], lamp_distance_m=round(float(dl[k]), 1))
        return out

    def find_duplicates(self, lat: float, lon: float, reported_at: str, exclude: str | None = None) -> list[dict]:
        t = self.cfg["triage"]
        when = pd.Timestamp(reported_at).tz_localize(None) if pd.Timestamp(reported_at).tzinfo else pd.Timestamp(reported_at)
        out = []
        for q in self.queue:
            if q.get("report_id") == exclude or q.get("merged_into"):
                continue
            d = float(I.distance_m(q["lon"], q["lat"], lon, lat))
            dt = abs((pd.Timestamp(q["reported_at"]).tz_localize(None) - when).days) if q.get("reported_at") else 999
            if d <= t["duplicate_radius_m"] and dt <= t["duplicate_days"]:
                out.append({"report_id": q["report_id"], "distance_m": round(d, 1), "days_apart": int(dt),
                            "category": q.get("category"), "title": q.get("title")})
        return out

    def get_risk(self, segment_id: str) -> dict:
        r = self.scored[self.scored["segment_id"] == segment_id]
        if r.empty:
            return {"error": "unknown segment"}
        r = r.iloc[0]
        if not r["scored"]:
            return {"segment_id": segment_id, "status": r["status"]}
        return {"segment_id": segment_id, "urgency": float(r["urgency"]), "band": r["band"],
                "p30": round(float(r["p30"]), 4), "wear_pct": float(r["wear_pct"]),
                "reasons": [x for x in [r.get("reason_1"), r.get("reason_2"), r.get("reason_3")] if isinstance(x, str)],
                "labels_synthetic": bool(r.get("labels_synthetic", True))}

    def call(self, name: str, args: dict):
        if name == "lookup_location":
            return self.lookup_location(float(args["lat"]), float(args["lon"]))
        if name == "find_duplicates":
            return self.find_duplicates(float(args["lat"]), float(args["lon"]), str(args["reported_at"]))
        if name == "get_risk":
            return self.get_risk(str(args["segment_id"]))
        return {"error": f"unknown tool {name}"}


# --- token log ------------------------------------------------------------------------------------
def log_tokens(cfg: dict, kind: str, model: str, usage: dict, ref: str | None = None, root: Path | None = None):
    d = I.resolve(cfg, "state_dir", root)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "model": model, "ref": ref, **usage}
    with open(d / "token_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _usage(resp) -> dict:
    u = getattr(resp, "usage_metadata", None)
    g = (lambda k: int(getattr(u, k, 0) or 0)) if u is not None else (lambda k: 0)
    return {"prompt_tokens": g("prompt_token_count"), "output_tokens": g("candidates_token_count"),
            "thoughts_tokens": g("thoughts_token_count"), "total_tokens": g("total_token_count")}


def gemini_client():
    """A google-genai client from GEMINI_API_KEY (.env is loaded, the key is never printed)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(I.ROOT / ".env")
    except ImportError:
        pass
    if not os.environ.get("GEMINI_API_KEY"):
        return None
    from google import genai
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


# --- the agent ------------------------------------------------------------------------------------
def _report_text(rep: dict) -> str:
    return (f"Report {rep['report_id']} (FixMyStreet, public). Reported {rep['reported_at']} at lat {rep['lat']:.6f}, "
            f"lon {rep['lon']:.6f}.\nText: {rep.get('title') or ''}\n{rep.get('detail') or ''}".strip())


_last_call: dict[str, float] = {}
_model_pos = [0]  # index of the model that answered last; later calls start there
FALLBACK_CODES = {429, 500, 503, 504}


def models(cfg: dict) -> list[str]:
    t = cfg["triage"]
    return list(t.get("models") or [t["model"]])


def _is_fallback_error(e: Exception) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code in FALLBACK_CODES:
        return True
    name = type(e).__name__.lower()
    return "timeout" in name or "timedout" in name or "deadline" in str(e).lower()


def _generate(client, cfg: dict, contents=None, config=None) -> tuple[object, str]:
    """generate_content over the model fallback list. A 429/5xx or a call slower than call_timeout_s moves
    to the next model; the model that answers is returned and tried first next time. Free-tier pacing
    (min_interval_s between calls to the same model) applies only to the real API, not test fakes."""
    import time
    t = cfg["triage"]
    ms = models(cfg)
    real = type(client).__module__.startswith("google.genai")
    if real and config is not None and t.get("call_timeout_s"):
        from google.genai import types
        config = config.model_copy(update={"http_options": types.HttpOptions(timeout=int(t["call_timeout_s"] * 1000))})
    order = ms[_model_pos[0] % len(ms):] + ms[:_model_pos[0] % len(ms)]
    last_err = None
    for model in order:
        wait = t.get("min_interval_s", 0) - (time.monotonic() - _last_call.get(model, 0.0))
        if wait > 0 and real:
            time.sleep(wait)
        _last_call[model] = time.monotonic()
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            _model_pos[0] = ms.index(model)
            return resp, model
        except Exception as e:  # google.genai.errors.APIError / httpx timeouts; generic so tests can fake them
            if not _is_fallback_error(e):
                raise
            last_err = e
    raise last_err


def gemini_triage(rep: dict, ctx: Context, client, image: tuple[bytes, str] | None = None,
                  root: Path | None = None, single_call: bool = False,
                  kind: str = "triage") -> tuple[dict | None, dict, list[str]]:
    """Gemini triage. Returns (draft_work_order args or None, token usage incl. the answering model, trace).

    triage.mode "fast" (default): the code runs lookup_location, find_duplicates and get_risk itself and puts
    the results in the prompt, so Gemini answers with draft_work_order in one call. "agent": the model calls
    the tools itself (up to max_turns calls). single_call: classification only, no tool results (eval)."""
    from google.genai import types
    t = ctx.cfg["triage"]
    fast = t.get("mode", "fast") == "fast" and not single_call
    text = _report_text(rep)
    trace = []
    if fast:
        facts = {"lookup_location": ctx.lookup_location(rep["lat"], rep["lon"]),
                 "find_duplicates": ctx.find_duplicates(rep["lat"], rep["lon"], str(rep["reported_at"]),
                                                        exclude=str(rep["report_id"]))}
        sid = facts["lookup_location"].get("segment_id")
        if sid:
            facts["get_risk"] = ctx.get_risk(sid)
        trace = [f"prefetch:{k}" for k in facts]
        text += "\n\nTool results (already run for you):\n" + json.dumps(facts, default=str)
    parts = [types.Part.from_text(text=text)]
    if image is not None:
        parts.append(types.Part.from_bytes(data=image[0], mime_type=image[1]))
    contents = [types.Content(role="user", parts=parts)]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM, temperature=0,
        tools=[types.Tool(function_declarations=[
            types.FunctionDeclaration(name=d["name"], description=d["description"], parameters_json_schema=d["parameters"])
            for d in TOOLS])],
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
            mode="ANY", allowed_function_names=["draft_work_order"] if (single_call or fast) else None)),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    usage = {"prompt_tokens": 0, "output_tokens": 0, "thoughts_tokens": 0, "total_tokens": 0, "calls": 0}
    final, answered = None, None
    import time
    t0 = time.monotonic()
    for _ in range(1 if fast or single_call else t["max_turns"]):
        resp, answered = _generate(client, ctx.cfg, contents=contents, config=config)
        u = _usage(resp)
        for k in u:
            usage[k] += u[k]
        usage["calls"] += 1
        calls = resp.function_calls or []
        if not calls:
            break
        contents.append(resp.candidates[0].content)
        replies = []
        for c in calls:
            trace.append(c.name)
            if c.name == "draft_work_order":
                final = dict(c.args)
                break
            replies.append(types.Part.from_function_response(name=c.name, response={"result": ctx.call(c.name, dict(c.args))}))
        if final is not None:
            break
        contents.append(types.Content(role="user", parts=replies))
    usage["model"] = answered
    usage["seconds"] = round(time.monotonic() - t0, 2)
    log_tokens(ctx.cfg, kind, answered or models(ctx.cfg)[0], {k: v for k, v in usage.items() if k != "model"},
               ref=str(rep["report_id"]), root=root)
    return final, usage, trace


def triage(rep: dict, ctx: Context, client=None, method: str = "auto", image: tuple[bytes, str] | None = None,
           root: Path | None = None) -> dict:
    """Triage one report and add it to ctx.queue. rep: report_id, reported_at, lat, lon, title, [detail].

    The asset, duplicate and urgency fields are always recomputed in code from the tools, so the
    model cannot invent an id or an urgency; Gemini supplies the category, summary and action.
    """
    loc = ctx.lookup_location(rep["lat"], rep["lon"])
    dups = ctx.find_duplicates(rep["lat"], rep["lon"], rep["reported_at"], exclude=str(rep["report_id"]))
    used, usage, trace, llm = "keyword_baseline", None, [], None
    original = next((q for q in ctx.queue if dups and q["report_id"] == dups[0]["report_id"]), None)
    if original is not None and method == "auto":
        # a duplicate inherits the original's triage: no second Gemini call (saves time and quota)
        llm = {"category": original["category"], "confidence": original.get("confidence", 0),
               "summary": original.get("summary"), "recommended_action": original.get("recommended_action"),
               "hazard": original.get("hazard", False)}
        used = f"merged into {original['report_id']} (no new Gemini call)"
    elif method in ("auto", "gemini") and client is not None:
        try:
            llm, usage, trace = gemini_triage(rep, ctx, client, image=image, root=root)
            used = "gemini" if llm else "keyword_baseline (gemini returned no decision)"
        except Exception as e:  # quota, network or API errors must not stop the officer's queue
            if method == "gemini":
                raise RuntimeError(f"Gemini call failed: {type(e).__name__}") from e
            used = f"keyword_baseline (gemini unavailable: {type(e).__name__})"
    elif method == "gemini":
        raise RuntimeError("no Gemini client: set GEMINI_API_KEY in .env")
    category = (llm or {}).get("category") if llm else keyword_classify(f"{rep.get('title', '')} {rep.get('detail', '')}")
    category = category if category in CATEGORIES else "other"
    official = ctx.cfg["coverage"]["official_streetlight_url" if category == "streetlight" else "official_report_url"]
    wo = {"report_id": str(rep["report_id"]), "source": rep.get("source", "fixmystreet"), "title": rep.get("title"),
          "reported_at": str(rep["reported_at"]), "lat": rep["lat"], "lon": rep["lon"], "category": category,
          "confidence": float((llm or {}).get("confidence", 0.6 if used == "keyword_baseline" else 0)),
          "method": used, "model": (usage or {}).get("model"), "tool_trace": trace, "tokens": usage, "location": loc,
          "segment_id": loc.get("segment_id") if loc.get("status") == "ok" else None, "lamp_id": None,
          "duplicates": dups, "merged_into": dups[0]["report_id"] if dups else None,
          "official_report_url": official, "hazard": bool((llm or {}).get("hazard", False)),
          "summary": (llm or {}).get("summary") or (rep.get("title") or ""),
          "recommended_action": (llm or {}).get("recommended_action") or "", "status": "pending_approval",
          "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if category == "streetlight":
        wo["lamp_id"] = loc.get("lamp_id")
        if wo["lamp_id"] is not None:
            lamp = ctx.lights[ctx.lights["lamp_id"] == wo["lamp_id"]].iloc[0]
            wo["p_fail_90d"] = float(lamp["p_fail_90d"])
        wo["recommended_action"] = wo["recommended_action"] or "Inspect lamp and replace unit if failed"
    if loc.get("status") != "ok":
        wo["status"] = loc["status"]  # out_of_coverage / national_highways: not ours to dispatch
        wo["recommended_action"] = loc["message"]
    elif category == "pothole" and wo["segment_id"]:
        risk = ctx.get_risk(wo["segment_id"])
        floor = ctx.cfg["urgency"]["bands"][ctx.cfg["triage"]["report_floor_band"]]
        wo["risk_urgency"] = risk.get("urgency")
        wo["urgency"] = max(float(risk.get("urgency") or 0), float(floor))
        wo["band"] = risk.get("band")
        wo["risk_reasons"] = risk.get("reasons", [])
        wo["recommended_action"] = wo["recommended_action"] or "Inspect within 5 working days; permanent repair if confirmed"
    if wo["merged_into"]:
        wo["status"] = "merged"
        for q in ctx.queue:
            if q["report_id"] == wo["merged_into"]:
                q.setdefault("merged_reports", []).append(wo["report_id"])
    ctx.queue.append(wo)
    return wo


def evaluate(cfg: dict | None = None, client=None, root: Path | None = None, limit: int | None = None) -> dict:
    """Gemini against the keyword baseline on the 100 hand-labelled real reports (title text only)."""
    cfg = cfg or I.load_config()
    lab = pd.read_csv(Path(__file__).with_name("eval") / "triage_labels.csv")
    if limit:
        lab = lab.head(limit)
    kw = lab["title"].map(keyword_classify)
    res = {"n": len(lab), "labels": lab["label"].value_counts().to_dict(),
           "keyword_baseline": {"accuracy": float((kw == lab["label"]).mean())}}
    if client is not None:
        from roadsense.prevent.mock import mock_world
        w = mock_world(cfg)
        from roadsense.prevent.plan import score
        ctx = Context(cfg, score(w["segments"], w["risk"], cfg), w["lights"])
        preds, tok = [], []
        for r in lab.itertuples():
            rep = {"report_id": f"eval-{r.fms_id}", "reported_at": "2026-10-12", "lat": 50.90, "lon": -1.44, "title": r.title}
            d, u, _ = gemini_triage(rep, ctx, client, root=root, single_call=True, kind="triage_eval")
            preds.append((d or {}).get("category", "other"))
            tok.append(u["total_tokens"])
        g = pd.Series(preds, index=lab.index)
        res["gemini"] = {"accuracy": float((g == lab["label"]).mean()), "mean_tokens_per_report": float(np.mean(tok)),
                         "model": ", ".join(models(cfg))}
        res["disagreements"] = lab.assign(keyword=kw, gemini=g)[(g != lab["label"]) | (kw != lab["label"])][
            ["fms_id", "title", "label", "keyword", "gemini"]].to_dict("records")
    for name in ["keyword_baseline"] + (["gemini"] if client is not None else []):
        pred = kw if name == "keyword_baseline" else g
        res[name]["per_class_recall"] = {c: float((pred[lab["label"] == c] == c).mean()) for c in lab["label"].unique()}
    return res


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Evaluate triage against the keyword baseline")
    ap.add_argument("--offline", action="store_true", help="keyword baseline only, no Gemini calls")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    client = None if a.offline else gemini_client()
    if client is None and not a.offline:
        print("GEMINI_API_KEY not set: running the keyword baseline only")
    res = evaluate(client=client, limit=a.limit)
    out = I.resolve(I.load_config(), "state_dir") / "triage_vs_keywords.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    from roadsense.agent.cost import write_triage_report
    write_triage_report(res)
    print(json.dumps({k: v for k, v in res.items() if k != "disagreements"}, indent=1))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
