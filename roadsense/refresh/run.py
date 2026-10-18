"""Orchestrates one refresh: copy the previous outputs into a new dated build, fetch, re-score with
the frozen models, rebuild the plan / inspection list / triage, validate, then switch `current`.
Any failure keeps the previous build live, is logged to data/live/refresh.log, and returns 1.
"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from roadsense.refresh import fetch, live, score
from roadsense.refresh.validate import validate

REPO = live.ROOT
CITIES = ["Southampton", "Brighton"]


class RefreshError(RuntimeError):
    pass


# --- staging -------------------------------------------------------------------------------------
def prepare(stage: Path, src: Path, repo: Path) -> None:
    """Stage = copy of the previous build (or the committed snapshot): data/processed, outputs files,
    cached triage, plus the repo's (unchanged) data/synthetic. App state (queue, outcomes) is never
    copied: it stays in the repo."""
    (stage / "outputs" / "lane_c").mkdir(parents=True)
    shutil.copytree(src / "data" / "processed", stage / "data" / "processed")
    for p in (src / "outputs").iterdir():
        if p.is_file():
            shutil.copy2(p, stage / "outputs" / p.name)
    for p in list((src / "outputs" / "lane_c").glob("triage_*.json")) + list((src / "outputs" / "lane_c").glob("inspection_*.json")):
        shutil.copy2(p, stage / "outputs" / "lane_c" / p.name)
    shutil.copy2(repo / "config.yaml", stage / "config.yaml")
    # read-only lane B inputs: the what-if rescoring (prevent.whatif) resolves data/synthetic against root
    shutil.copytree(repo / "data" / "synthetic", stage / "data" / "synthetic")


def stage_configs(stage: Path, repo: Path) -> tuple[dict, dict, dict]:
    """(lane A config, model config, app config) reading processed/outputs from the stage and everything
    else (synthetic data, models, app state) from the repo. resolve() joins root with the path, and an
    absolute path wins, so absolute repo paths ignore root=stage."""
    from roadsense.prevent import inputs as I
    from roadsense.synthetic.common import load_config as syn_config
    cfg_a = yaml.safe_load((repo / "config.yaml").read_text(encoding="utf-8"))
    cfg_m = copy.deepcopy(syn_config())
    for k in ["synthetic_dir", "models_dir", "mock_dir", "reports_dir"]:
        cfg_m["paths"][k] = str(repo / cfg_m["paths"][k])
    cfg_c = copy.deepcopy(I.load_config(I.CONFIG_PATH, live=False))
    for k in ["lane_c_copy_dir", "lane_b_mock_dir", "synthetic_dir", "models_dir", "state_dir", "reports_dir", "fms_all_csv"]:
        cfg_c["paths"][k] = str(repo / cfg_c["paths"][k])
    return cfg_a, cfg_m, cfg_c


# --- as_of ---------------------------------------------------------------------------------------
HISTORY_FEATS = ["potholes_seg_8w", "potholes_nb_8w", "potholes_seg_12m", "potholes_nb_12m",
                 "days_since_resurfacing", "treatments_2y"]


def monday_on_or_before(now: datetime) -> str:
    d = pd.Timestamp(now)
    d = (d.tz_convert("UTC") if d.tzinfo else d).normalize()
    return str((d - pd.Timedelta(days=d.weekday())).date())


def check_history(as_of: str, cities: list[str], stage: Path, repo: Path) -> None:
    """Inputs that do not depend on new weather: stop (RefreshError) and say exactly what is missing."""
    a = pd.Timestamp(as_of)
    problems = []
    if "Southampton" in cities:
        meta = json.loads((repo / "data" / "synthetic" / "_meta.json").read_text(encoding="utf-8"))
        rep = pd.to_datetime(pd.read_csv(repo / "data" / "synthetic" / "potholes.csv", usecols=["reported_date"])["reported_date"])
        first, last = rep.min(), rep.max()
        if pd.Timestamp(meta["as_of"]) < a:
            problems.append(
                f"Southampton: the synthetic council history (data/synthetic, generated to as_of {meta['as_of']}, last "
                f"repair {last.date()}) has no repairs or treatments from {meta['as_of']} to {as_of}. The weekly "
                f"model's history features {', '.join(HISTORY_FEATS)} count repairs and treatments dated before as_of, "
                f"so they cannot be built for {as_of}. Regenerate the synthetic data to as_of >= {as_of} (set as_of in "
                f"config/synthetic.yaml, then python -m roadsense.synthetic), or refresh with --city Brighton.")
        if first > pd.Timestamp(f"{a.year - 3}-01-01") or last < pd.Timestamp(f"{a.year - 1}-12-01"):
            problems.append(f"Southampton: the real-label model's history input needs complete synthetic repair years "
                            f"{a.year - 3}-{a.year - 1}; data/synthetic/potholes.csv covers {first.date()} to {last.date()}.")
    from roadsense.features.ward_points import path_for
    for city in cities:
        if not Path(path_for(stage / "data" / "processed", city)).exists():
            problems.append(f"{city}: {path_for('data/processed', city)} is missing (per-ward weather needs it; "
                            f"build it once with python -m roadsense.features.ward_points {city} and commit it)")
    lab = pd.read_csv(stage / "data" / "processed" / "labels_segment_year.csv", usecols=["city", "year"])
    for city in [c for c in cities if c != "Southampton"]:
        have = set(lab.loc[lab["city"] == city, "year"])
        miss = [y for y in (a.year - 1, a.year) if y not in have]
        if miss:
            problems.append(f"{city}: data/processed/labels_segment_year.csv has no rows for {miss}; the real-label model "
                            f"scores year {a.year} from FixMyStreet reports in {a.year - 1} (rep_prev1), so {city} cannot "
                            f"be scored for {as_of}. Rebuild the labels with that year (roadsense.features.labels).")
    if problems:
        raise RefreshError(f"cannot build as_of {as_of}: " + " | ".join(problems))


def check_weather(as_of: str, cities: list[str], stage: Path) -> None:
    """After the forecast refresh: the yearly weather the real model needs (the year before as_of) for every ward."""
    a = pd.Timestamp(as_of)
    wx = pd.read_parquet(stage / "data" / "processed" / "weather_weekly.parquet", columns=["city", "ward", "week_start"])
    seg = pd.read_parquet(stage / "data" / "processed" / "segments.parquet", columns=["city", "ward"])
    yr = pd.to_datetime(wx["week_start"]).dt.year
    problems = []
    for city in cities:
        wards = set(seg.loc[seg["city"] == city, "ward"])
        have = set(wx.loc[(wx["city"] == city) & (yr == a.year - 1), "ward"])
        if wards - have:
            problems.append(f"{city}: weather_weekly.parquet has no {a.year - 1} weeks for wards {sorted(wards - have)}")
    if problems:
        raise RefreshError(f"cannot build as_of {as_of}: " + " | ".join(problems))


# --- steps ---------------------------------------------------------------------------------------
def step_reports(get_json, cfg_a: dict, stage: Path, cities: list[str], now: datetime, open_days: int) -> dict:
    p = stage / "data" / "processed" / "fms_roads_clean.csv"
    stored = pd.read_csv(p)
    out, counts = fetch.refresh_reports(get_json, cfg_a, stored, cities, now, open_days=open_days)
    out.to_csv(p, index=False)
    return counts


def step_weather(get_json, cfg_a: dict, stage: Path, cities: list[str], now: datetime) -> dict:
    proc = stage / "data" / "processed"
    weekly = pd.read_parquet(proc / "weather_weekly.parquet")
    counts = {}
    for city in cities:
        fresh = fetch.forecast_weeks(get_json, cfg_a, city, now, proc)
        weekly = fetch.merge_weather(weekly, fresh)
        counts[city] = {"wards": int(fresh["ward"].nunique()), "weeks": sorted(fresh["week_start"].unique().tolist())}
    weekly.to_parquet(proc / "weather_weekly.parquet", index=False)
    today = pd.Timestamp(now).date().isoformat()
    weekly[weekly["week_start"] > today].reset_index(drop=True).to_parquet(proc / "weather_forecast_weekly.parquet", index=False)
    return counts


def step_context(stage: Path, now: datetime, prev_info: dict, prev_name: str | None, online: bool = True) -> dict:
    """Building sites (brownfield registers + OpenStreetMap, once a UTC day) and DfT Street Manager works (any month
    published since the last one we have; DfT publishes monthly), re-linked to segments. A source that cannot be
    reached keeps the previous build's rows, so this step never fails the refresh."""
    from roadsense.features import works_context as WC
    from roadsense.ingest import construction, streetworks
    proc = stage / "data" / "processed"
    out = {}
    if not online:  # a test or offline run with an injected fetcher: keep the previous build's rows
        out["sites"] = {"skipped": "offline run (injected fetcher)"}
    elif prev_info.get("context_pulled_on") == now.date().isoformat():
        out["sites"] = {"skipped": f"already pulled today in {prev_name}"}
    else:
        try:
            df = construction.build(str(proc / "construction_sites.csv"), str(proc / "segments.parquet"))
            out["sites"] = {c: int(n) for c, n in df.groupby("city").size().items()}
        except Exception as e:  # noqa: BLE001
            out["sites"] = {"kept_previous": f"{type(e).__name__}: {e}"}
        try:
            out["street_works"] = streetworks.update_linked(str(proc), now=now)
        except Exception as e:  # noqa: BLE001
            out["street_works"] = {"kept_previous": f"{type(e).__name__}: {e}"}
    out["site_links"] = WC.relink_sites(str(proc))
    return out


def _triage_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    d = json.loads(path.read_text(encoding="utf-8"))
    return {int(r["fms_id"]): (r["category"], r["method"], r.get("model")) for r in d.get("reports", [])}


def step_triage_home(cfg_c: dict, stage: Path, city: str, client, now: datetime, limit: int = 30) -> tuple[dict, dict]:
    """Classify only open reports not in the cache (Gemini over the fallback list, keyword without a key)."""
    from roadsense.agent import city_triage as CT
    path = stage / "outputs" / "lane_c" / f"triage_{city.lower()}.json"
    cache = _triage_cache(path)
    reps = CT.open_reports(cfg_c, city, stage, now=now).head(limit)
    rows, new = [], 0
    for r in reps.itertuples():
        if int(r.id) in cache:
            cat, method, model = cache[int(r.id)]
        else:
            cat, method, model = CT.classify(str(r.title), cfg_c, client)
            new += 1
        rows.append({"fms_id": int(r.id), "reported_at": r.requested.strftime("%Y-%m-%d"), "title": str(r.title),
                     "lat": float(r.lat), "lon": float(r.lon), "category": cat, "method": method, "model": model})
    path.write_text(json.dumps({"city": city, "generated_at": now.isoformat(timespec="seconds"), "count": len(rows),
                                "note": "Refresh cache: open-report classifications, reused for the plan's confirmed potholes.",
                                "reports": rows}, indent=1), encoding="utf-8")
    return {r["fms_id"]: (r["category"], r["method"]) for r in rows}, {"open": len(rows), "newly_classified": new}


def step_plan(cfg_c: dict, stage: Path, triaged: dict) -> dict:
    from roadsense.prevent import plan as P

    def classify(row):
        hit = triaged.get(int(row.id))
        return (hit[0], "gemini" if hit[1] == "gemini" else "keyword") if hit else P.keyword_classifier(row)
    res = P.run(cfg_c, root=stage, classify=classify, verbose=False)
    for s in cfg_c.get("whatif", {}).get("scenarios", {}):
        P.run(cfg_c, root=stage, scenario=s, classify=classify, verbose=False)
    return {"plan_rows": int(len(res["plan"])), "worth_rows": int(len(res["worth"]))}


def step_city(cfg_c: dict, stage: Path, city: str, client, now: datetime) -> dict:
    from roadsense.agent import city_triage as CT
    from roadsense.api.cities import CityView
    lane_c = stage / "outputs" / "lane_c"
    cache = _triage_cache(lane_c / f"triage_{city.lower()}.json")
    before = set(cache)
    res = CT.run(cfg_c, city, root=stage, client=client, now=now, verbose=False, cache=cache)
    view = CityView(cfg_c, city, stage)
    o = CT.open_reports(cfg_c, city, stage, now=now)
    insp = view.inspection_list(o.assign(reported_at=o["requested"].dt.strftime("%Y-%m-%d")), cfg_c)  # as Service.city_inspection
    (lane_c / f"inspection_{city.lower()}.json").write_text(json.dumps(insp, indent=1, default=str), encoding="utf-8")
    return {"triaged": res["count"], "newly_classified": sum(1 for q in res["reports"] if q["fms_id"] not in before),
            "inspection_rows": insp["count"]}


# --- main ----------------------------------------------------------------------------------------
def refresh(cities: list[str] | None = None, dry_run: bool = False, now: datetime | None = None, get_json=None,
            client="auto", base: Path | None = None, repo: Path | None = None, verbose: bool = True) -> int:
    cities = cities or CITIES
    now = now or datetime.now(timezone.utc)
    base = Path(base or live.live_dir())
    repo = Path(repo or REPO)
    get_json = get_json or fetch.http_get_json
    base.mkdir(parents=True, exist_ok=True)
    src = live.current_build(base) or repo
    name = now.strftime("%Y%m%dT%H%M%SZ")
    stage = Path(tempfile.mkdtemp(prefix=f"dryrun_{name}_", dir=base)) / name if dry_run else base / name
    say = print if verbose else (lambda *a, **k: None)
    try:
        if stage.exists():
            raise RefreshError(f"{stage} already exists")
        say(f"refresh {name}: cities {', '.join(cities)}; starting from {'live build ' + src.name if src != repo else 'committed snapshot'}")
        prepare(stage, src, repo)
        cfg_a, cfg_m, cfg_c = stage_configs(stage, repo)
        as_of = monday_on_or_before(now)
        info = {"started_at": now.isoformat(timespec="seconds"), "cities": cities, "dry_run": dry_run,
                "base": src.name if src != repo else "snapshot", "as_of": as_of, "previous_as_of": cfg_m["as_of"]}
        cfg_m["as_of"] = as_of                   # the frozen models score the four weeks after this Monday
        say(f"  as_of {as_of} (config/synthetic.yaml has {info['previous_as_of']})")
        check_history(as_of, cities, stage, repo)
        prev = live.current_build(base)
        prev_info = live.build_info(prev) if prev is not None else {}
        if prev_info.get("fixmystreet_pulled_on") == now.date().isoformat():
            # one incremental FixMyStreet pull per UTC day: today's build already has it (weather still refreshes)
            info["fixmystreet"] = {"skipped": f"already pulled today in {prev.name}"}
            info["fixmystreet_pulled_on"] = prev_info["fixmystreet_pulled_on"]
        else:
            info["fixmystreet"] = step_reports(get_json, cfg_a, stage, cities, now, cfg_c["triage"]["open_report_days"])
            info["fixmystreet_pulled_on"] = now.date().isoformat()
        say(f"  FixMyStreet: {info['fixmystreet']}")
        info["weather"] = step_weather(get_json, cfg_a, stage, cities, now)
        say(f"  Open-Meteo forecast: { {c: v['weeks'] for c, v in info['weather'].items()} }")
        check_weather(as_of, cities, stage)
        info["context"] = step_context(stage, now, prev_info, prev.name if prev is not None else None,
                                       online=get_json is fetch.http_get_json)
        if "skipped" not in info["context"].get("sites", {}):
            info["context_pulled_on"] = now.date().isoformat()
        else:
            info["context_pulled_on"] = prev_info.get("context_pulled_on")
        say(f"  building sites and street works: {info['context']}")
        if client == "auto":
            from roadsense.agent.triage import gemini_client
            client = gemini_client()
        md = repo / "data" / "models"
        if "Southampton" in cities:
            score.weekly_southampton(cfg_m, stage, md)
        info["risk_rows"] = score.real_risk(cfg_m, stage, md, cities, now)
        info["forecast_weeks"] = {c: sorted(str(w) for w in pd.read_parquet(
            stage / "outputs" / ("risk_latest.parquet" if c == "Southampton" else f"risk_{c.lower()}_latest.parquet"),
            columns=["week_start"])["week_start"].unique()) for c in cities}
        say(f"  re-scored with frozen models: {info['risk_rows']}; forecast weeks {info['forecast_weeks']}")
        info["triage"] = {}
        if "Southampton" in cities:
            triaged, info["triage"]["Southampton"] = step_triage_home(cfg_c, stage, "Southampton", client, now)
            info["plan"] = step_plan(cfg_c, stage, triaged)
            say(f"  Southampton triage {info['triage']['Southampton']}; plan {info['plan']}")
        for city in [c for c in cities if c != "Southampton"]:
            info["triage"][city] = step_city(cfg_c, stage, city, client, now)
            say(f"  {city} triage + inspection list: {info['triage'][city]}")
        info["gemini"] = client is not None
        errs = validate(stage, repo, cities)
        if errs:
            raise RefreshError("validation failed: " + "; ".join(errs))
        info["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (stage / "refresh.json").write_text(json.dumps(info, indent=1, default=str), encoding="utf-8")
        if dry_run:
            say(f"  validation passed. DRY RUN: nothing switched; {base / 'current'} unchanged")
            live.log(base, f"dry-run ok {name}")
            return 0
        live.switch_current(base, stage)
        removed = live.prune(base)
        live.log(base, f"ok {name} {json.dumps(info.get('fixmystreet'), default=str)}")
        say(f"  validation passed; current -> {name}" + (f"; removed {removed}" if removed else ""))
        return 0
    except Exception as e:                       # noqa: BLE001 - any failure keeps the previous build
        live.log(base, f"FAILED {name} {type(e).__name__}: {e}")
        say(f"refresh FAILED ({type(e).__name__}: {e}); previous outputs kept")
        if verbose:
            traceback.print_exc()
        return 1
    finally:
        if dry_run:
            shutil.rmtree(stage.parent, ignore_errors=True)
        elif stage.exists() and live.current_build(base) != stage:
            shutil.rmtree(stage, ignore_errors=True)  # failed build: never leave a half-written folder
