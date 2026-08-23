"""Frozen compact event-vote baseline and chronological development selection."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.event.common import (
    EVENT_RULE_VOTES,
    PLAN_PATH,
    aggregate_economics,
    event_rule_score,
    load_day,
    load_plan,
    simulate_selected_day,
)
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
OUTPUT_ROOT = ROOT / "data/research/tardis/reports/event_models/event_rule"
SPEC_PATH = ROOT / "research/specs/event_rule_frozen.json"


def _development_gate(metrics: dict[str, Any], days: pd.DataFrame) -> dict[str, Any]:
    gate = load_plan()["development_gate"]
    checks = {
        "pooled_gross_positive": metrics["gross_pnl_usdt"] > 0,
        "pooled_net_positive": metrics["net_pnl_usdt"] > 0,
        "positive_folds": int((days["net_pnl_usdt"] > 0).sum())
        >= int(gate["positive_validation_folds_minimum"]),
        "worst_fold_tolerance": metrics["worst_day_net_pnl_usdt"]
        >= float(gate["worst_fold_net_pnl_usdt_minimum"]),
        "minimum_fills": metrics["maker_fill_orders"]
        >= int(gate["maker_fill_orders_minimum_total"]),
        "zero_inventory_violations": metrics["inventory_limit_violations"]
        == int(gate["inventory_limit_violations"]),
    }
    return {"checks": checks, "passes": bool(all(checks.values()))}


def run_development() -> dict[str, Any]:
    plan = load_plan()
    rows: list[dict[str, Any]] = []
    controls: list[dict[str, Any]] = []
    for fold_number, fold in enumerate(plan["chronological_splits"]["development_folds"], 1):
        date = fold["validate"][0]
        frame = load_day(date)
        score = event_rule_score(frame)
        for threshold in plan["model_families"]["event_rule"]["development_vote_thresholds"]:
            result = simulate_selected_day(
                frame,
                date=date,
                model_id=f"event_rule_{threshold}_of_6",
                selected=score * 6 >= threshold,
            )
            result["fold"] = fold_number
            result["vote_threshold"] = threshold
            rows.append(result)
        for model_id, obi in (("neutral", False), ("obi_rule", True)):
            result = simulate_selected_day(
                frame,
                date=date,
                model_id=model_id,
                selected=np.ones(len(frame), dtype="bool"),
                obi_policy=obi,
            )
            result["fold"] = fold_number
            controls.append(result)
    day = pd.DataFrame(rows)
    control_day = pd.DataFrame(controls)
    ranking: list[dict[str, Any]] = []
    for threshold, group in day.groupby("vote_threshold", sort=True):
        economics = aggregate_economics(group)[f"event_rule_{threshold}_of_6"]
        ranking.append({
            "vote_threshold": int(threshold),
            "median_fold_net_pnl_usdt": float(group["net_pnl_usdt"].median()),
            "worst_fold_net_pnl_usdt": float(group["net_pnl_usdt"].min()),
            "quote_attempts": int(group["maker_quote_attempts"].sum()),
            **economics,
        })
    ranking_frame = pd.DataFrame(ranking).sort_values(
        ["median_fold_net_pnl_usdt", "worst_fold_net_pnl_usdt", "vote_threshold"],
        ascending=[False, False, True],
        kind="stable",
        ignore_index=True,
    )
    selected_threshold = int(ranking_frame.iloc[0]["vote_threshold"])
    selected_days = day.loc[day["vote_threshold"].eq(selected_threshold)].copy()
    selected_id = f"event_rule_{selected_threshold}_of_6"
    selected_economics = aggregate_economics(selected_days)[selected_id]
    gate = _development_gate(selected_economics, selected_days)

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    write_csv(OUTPUT_ROOT / "fold_day_metrics.csv", day)
    write_csv(OUTPUT_ROOT / "control_fold_day_metrics.csv", control_day)
    write_csv(OUTPUT_ROOT / "ranking.csv", ranking_frame)
    payload = {
        "schema": "event-rule-development-v1",
        "plan_sha256": sha256(PLAN_PATH),
        "votes": EVENT_RULE_VOTES,
        "selection_rule": plan["selector"]["event_rule_threshold_selection"],
        "selected_vote_threshold": selected_threshold,
        "selected_economics": selected_economics,
        "control_economics": aggregate_economics(control_day),
        "development_gate": gate,
        "day_metrics_sha256": sha256(OUTPUT_ROOT / "fold_day_metrics.csv"),
        "ranking_sha256": sha256(OUTPUT_ROOT / "ranking.csv"),
    }
    write_json(OUTPUT_ROOT / "development_summary.json", payload)
    frozen = {
        "schema": "event-rule-frozen-v1",
        "status": "survived_development_gate" if gate["passes"] else "rejected_development_gate",
        "plan_sha256": sha256(PLAN_PATH),
        "model_id": selected_id,
        "features": EVENT_RULE_VOTES,
        "transforms": "strictly_positive_vote_after_quote_side_orientation",
        "vote_threshold": selected_threshold,
        "quote_lifetime_ms": 1000,
        "queue_model": "existing_pessimistic_visible_queue",
        "execution": plan["execution"],
        "development_dates": plan["chronological_splits"]["development_days"],
        "seed": None,
        "model_artifact_sha256": None,
        "spec_parent_plan_sha256": sha256(PLAN_PATH),
        "code_commit_at_plan_freeze": plan["audit"]["repository_commit_before_freeze"],
        "development_gate": gate,
    }
    write_json(SPEC_PATH, frozen)
    return payload


def main() -> None:
    print(json.dumps(run_development(), sort_keys=True))


if __name__ == "__main__":
    main()
