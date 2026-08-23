"""Loop-7 one/five-minute momentum-gated OBI maker round-trip screen."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from pyresearch.obi.maker_roundtrip_search import SIGNALS, aggregate_development
from pyresearch.obi.momentum_gate_search import evaluate_day, rank_development
from pyresearch.obi.passive_entry_search import PASSIVE_THRESHOLDS_PATH, OBI_THRESHOLDS_PATH
from pyresearch.obi.search import REPORT_ROOT
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
SPEC_PATH = ROOT / "research/specs/obi_short_momentum_search_spec.json"
OUTPUT_ROOT = REPORT_ROOT / "loop7_short_momentum"
THRESHOLDS_PATH = OUTPUT_ROOT / "threshold_audit.json"
DAY_METRICS_PATH = OUTPUT_ROOT / "development_day_metrics.csv"
AGGREGATE_PATH = OUTPUT_ROOT / "development_metrics.csv"
RANKING_PATH = OUTPUT_ROOT / "development_ranking.csv"
SHORTLIST_PATH = OUTPUT_ROOT / "shortlist_before_exact_execution.json"
DEVELOPMENT_DATES = [f"2026-0{month}-01" for month in range(1, 6)]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_threshold_audit() -> dict[str, Any]:
    spec = _load_json(SPEC_PATH)
    expected_status = "declared_after_loop6_capacity_failure_before_short_momentum_outcomes"
    if spec["status"] != expected_status:
        raise ValueError("loop-7 disclosure status changed")
    passive_hash = sha256(PASSIVE_THRESHOLDS_PATH)
    obi_hash = sha256(OBI_THRESHOLDS_PATH)
    if passive_hash != spec["audit"]["passive_approach_thresholds_sha256"]:
        raise ValueError("passive thresholds changed before loop 7")
    if obi_hash != spec["audit"]["obi_stage1_thresholds_sha256"]:
        raise ValueError("OBI thresholds changed before loop 7")
    passive = _load_json(PASSIVE_THRESHOLDS_PATH)
    obi = _load_json(OBI_THRESHOLDS_PATH)
    quantiles = [float(value) for value in spec["entry"]["absolute_tail_quantiles"]]
    payload = {
        "schema": "obi-short-momentum-threshold-audit-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "passive_thresholds_sha256": passive_hash,
        "obi_thresholds_sha256": obi_hash,
        "outcomes_read": False,
        "queue_bottom20_btc": float(passive["thresholds"]["queue_ahead_initial"]["q20"]),
        "signal_absolute_thresholds": {
            signal: {
                f"q{quantile:.4f}": float(
                    obi["signal_absolute_thresholds"][signal][f"q{quantile:.4f}"]
                )
                for quantile in quantiles
            }
            for signal in SIGNALS
        },
    }
    write_json(THRESHOLDS_PATH, payload)
    return payload


def run_development() -> dict[str, Any]:
    thresholds = build_threshold_audit()
    day = pd.concat(
        [
            evaluate_day(date, thresholds, spec_path=SPEC_PATH)
            for date in DEVELOPMENT_DATES
        ],
        ignore_index=True,
    )
    spec = _load_json(SPEC_PATH)
    expected = (
        len(spec["entry"]["signals"])
        * len(spec["entry"]["absolute_tail_quantiles"])
        * len(spec["entry"]["directions"])
        * len(spec["entry"]["quote_lifetimes_ms"])
        * len(spec["entry"]["cancel_modes"])
        * len(spec["exit"]["additional_hold_delays_ms"])
        * len(spec["exit"]["quote_lifetimes_ms"])
        * len(spec["placement_regimes"])
        * len(DEVELOPMENT_DATES)
    )
    if len(day) != expected:
        raise ValueError(f"unexpected short-momentum rows: {len(day)} != {expected}")
    write_csv(DAY_METRICS_PATH, day)
    aggregate = aggregate_development(day)
    write_csv(AGGREGATE_PATH, aggregate)
    ranking = rank_development(aggregate, spec_path=SPEC_PATH)
    write_csv(RANKING_PATH, ranking)
    shortlist_size = int(spec["development_gate"]["shortlist_size"])
    survivors = ranking.loc[ranking["advances_to_exact_execution"]].head(shortlist_size)
    diagnostic = ranking.loc[
        ranking["eligible_activity"] & ~ranking["duplicate_behavior"]
    ].head(shortlist_size)
    write_json(SHORTLIST_PATH, {
        "schema": "obi-short-momentum-shortlist-v1",
        "created_from_development_only": True,
        "retrospective_outcomes_read": False,
        "spec_sha256": sha256(SPEC_PATH),
        "threshold_audit_sha256": sha256(THRESHOLDS_PATH),
        "development_metrics_sha256": sha256(AGGREGATE_PATH),
        "development_ranking_sha256": sha256(RANKING_PATH),
        "survivors_for_exact_execution": survivors.to_dict("records"),
        "diagnostic_top_not_automatically_advanced": diagnostic.to_dict("records"),
    })
    eligible = ranking.loc[ranking["eligible_activity"] & ~ranking["duplicate_behavior"]]
    best = eligible.sort_values("primary_net_mean_bps", ascending=False).iloc[0]
    return {
        "declared_policy_cells": int(len(aggregate)),
        "activity_eligible_unique_cells": int(len(eligible)),
        "positive_primary_net_cells": int((eligible["primary_net_mean_bps"] > 0).sum()),
        "all_day_positive_survivors": int(len(survivors)),
        "best_pooled_policy": str(best["policy"]),
        "best_pooled_gross_bps": float(best["gross_mean_bps"]),
        "best_pooled_primary_net_bps": float(best["primary_net_mean_bps"]),
        "best_pooled_positive_days": int(best["positive_development_days"]),
        "best_pooled_worst_day_bps": float(best["worst_development_day_net_bps"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("thresholds", "development"))
    args = parser.parse_args()
    result = build_threshold_audit() if args.command == "thresholds" else run_development()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
