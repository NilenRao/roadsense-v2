"""Feedback loop (M8): `python -m roadsense.api.retrain` (Makefile: `make retrain`).

Adds crew outcomes recorded through POST /outcomes (outputs/lane_c/outcome_labels.csv) to the
pothole labels as extra events, retrains lane B's y30 model on the same time split, and promotes
it to data/models/ only if validation PR-AUC improves. Otherwise the current model stays.
"""
from __future__ import annotations

import json
import pickle
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from roadsense.prevent import inputs as I


def outcome_events(cfg: dict, root: Path | None = None) -> pd.DataFrame:
    """Crew-confirmed potholes as events in lane B's potholes.csv shape."""
    p = I.resolve(cfg, "state_dir", root) / "outcome_labels.csv"
    if not p.exists():
        return pd.DataFrame(columns=["event_id", "segment_id", "reported_date"])
    o = pd.read_csv(p)
    o = o[o["outcome"] == "pothole"]
    return pd.DataFrame({"event_id": "CREW-" + o["outcome_id"].astype(str), "segment_id": o["segment_id"],
                         "reported_date": o["inspection_date"], "repaired_date": None, "repair_type": "crew_outcome",
                         "is_synthetic": o.get("is_synthetic", False), "source": "crew_outcome"})


def promote_if_better(current: float | None, candidate: float, min_gain: float = 0.0) -> bool:
    return current is None or candidate > current + min_gain


def run(root: Path | None = None, train_fn=None) -> dict:
    """train_fn(extra_events) -> (score, artefacts dict {filename: bytes}) lets tests swap the trainer."""
    cfg = I.load_config()
    md = I.resolve(cfg, "models_dir", root)
    extra = outcome_events(cfg, root)
    ev = md / "risk_eval.json"
    current = None
    if ev.exists():
        current = json.loads(ev.read_text(encoding="utf-8"))["eval"]["time"]["y30"]["model"]["pr_auc"]
    train_fn = train_fn or _lane_b_trainer(root)
    if train_fn is None:
        return {"promoted": False, "reason": "lane B risk model not merged yet", "extra_events": len(extra)}
    score, artefacts = train_fn(extra)
    promoted = promote_if_better(current, score)
    if promoted:
        md.mkdir(parents=True, exist_ok=True)
        backup = md / f"previous_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        backup.mkdir()
        for name, data in artefacts.items():
            if (md / name).exists():
                shutil.copy2(md / name, backup / name)
            (md / name).write_bytes(data)
    res = {"promoted": promoted, "current_pr_auc": current, "candidate_pr_auc": score, "extra_events": len(extra)}
    log = I.resolve(cfg, "state_dir", root) / "retrain_log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **res}) + "\n")
    return res


def _lane_b_trainer(root: Path | None):
    try:
        from roadsense.models import risk as R
        from roadsense.synthetic.common import load_config as lane_b_config
    except ImportError:
        return None

    def train(extra: pd.DataFrame):
        bcfg = lane_b_config()
        data = R.load_data(bcfg, root)
        if len(extra):
            data["potholes"] = pd.concat([data["potholes"], extra], ignore_index=True)
        table = R.labelled_rows(R.build_table(data["seg"], data["wx"], data["res"], data["potholes"], bcfg), bcfg)
        tr, te, _ = R.time_split(table, bcfg)
        score = R.evaluate_split(tr, te, bcfg)["y30"]["model"]["pr_auc"]
        m = R.RiskModel("y30", bcfg).fit(table)
        return score, {"risk_y30.txt": m.booster.booster_.model_to_string().encode("utf-8"),
                       "risk_y30_calibrator.pkl": pickle.dumps(m.iso)}

    return train


if __name__ == "__main__":
    print(json.dumps(run(), indent=1))
