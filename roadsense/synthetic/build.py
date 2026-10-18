"""make synthetic: run Generators 1-5 with seed 2026 and write data/synthetic/.

Usage: python -m roadsense.synthetic [--root PATH]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from roadsense.synthetic import crew, potholes, resurfacing, streetlights
from roadsense.synthetic.common import ROOT, load_config, load_inputs, resolve, scored, write_meta

TRUTH_FILE = "_truth.json"


def _rng(cfg: dict, k: int) -> np.random.Generator:
    # independent stream per generator so changing one does not shift the others
    return np.random.default_rng([cfg["seed"], k])


def run(cfg: dict | None = None, root: Path | None = None, verbose: bool = True) -> dict:
    cfg = cfg or load_config()
    root = Path(root or ROOT)
    out = resolve(cfg, "synthetic_dir", root)
    out.mkdir(parents=True, exist_ok=True)
    inp = load_inputs(cfg, root)
    seg_all, wx = inp["segments"], inp["weather"]
    seg = scored(seg_all)
    if "mock" in inp["sources"].values():
        mock_dir = resolve(cfg, "mock_dir", root)
        mock_dir.mkdir(parents=True, exist_ok=True)
        if inp["sources"]["segments"] == "mock":
            seg_all.to_parquet(mock_dir / "segments.parquet", index=False)
        if inp["sources"]["weather"] == "mock":
            wx.to_parquet(mock_dir / "weather_weekly.parquet", index=False)

    res = resurfacing.generate(seg, cfg, _rng(cfg, 1))
    ph, truth = potholes.generate(seg, wx, res, cfg, _rng(cfg, 2))
    osm_path = resolve(cfg, "processed_dir", root) / "street_lamps.parquet"
    osm = pd.read_parquet(osm_path) if osm_path.exists() else None
    lamps = streetlights.generate_lamps(seg_all, cfg, _rng(cfg, 3), osm)
    faults, units = streetlights.generate_faults(lamps, cfg, _rng(cfg, 4))
    lamps = lamps.merge(units, on="lamp_id", how="left")
    cols = [c for c in lamps.columns if c not in ("is_synthetic", "source")] + ["is_synthetic", "source"]
    lamps = lamps[cols]
    co = crew.generate(seg, ph, truth, cfg, _rng(cfg, 5), resolve(cfg, "outputs_dir", root))

    files = {"resurfacing.csv": res, "potholes.csv": ph, "streetlights.csv": lamps,
             "light_faults.csv": faults, "crew_outcomes.csv": co}
    for name, df in files.items():
        df.to_csv(out / name, index=False, lineterminator="\n")
    (out / TRUTH_FILE).write_text(json.dumps(truth, indent=1, sort_keys=True), encoding="utf-8")
    counts = {k: len(v) for k, v in files.items()}
    yearly = pd.to_datetime(ph["reported_date"]).dt.year.value_counts().sort_index().to_dict()
    dark = streetlights.currently_dark(faults, cfg)
    write_meta(out / "_meta.json", seed=cfg["seed"], version=cfg["version"], as_of=cfg["as_of"],
               inputs=inp["sources"], rows=counts, potholes_by_reported_year=yearly,
               currently_dark=len(dark), note="All files in this folder are SYNTHETIC.")
    if verbose:
        print(f"inputs: {inp['sources']}")
        for k, v in counts.items():
            print(f"  {k:<20} {v:>7,} rows")
        print(f"  potholes by reported year: {yearly}")
        print(f"  treatment events: {int((res['treatment'] != 'last_resurfaced').sum())}")
        print(f"  light faults by type: {faults['fault_type'].value_counts().to_dict()}")
        print(f"  currently dark lamps: {len(dark)}")
    return {"counts": counts, "yearly": yearly, "sources": inp["sources"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None)
    a = ap.parse_args()
    run(root=a.root)


if __name__ == "__main__":
    main()
