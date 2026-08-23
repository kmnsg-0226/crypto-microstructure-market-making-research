"""Deterministic manual event/decision/fill traces for the development endpoint."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.event.common import EVENT_RULE_VOTES, event_rule_score, load_day
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
OUTPUT_ROOT = ROOT / "data/research/tardis/reports/event_models"
SEED = 20260816


def run() -> dict[str, Any]:
    frame = load_day("2026-05-01")
    frame = frame.copy()
    frame["event_rule_score"] = event_rule_score(frame)
    frame["event_rule_quote"] = frame["event_rule_score"].ge(5 / 6)
    categories = {
        "quoted": frame["event_rule_quote"],
        "rejected": ~frame["event_rule_quote"],
        "filled_bid": frame["event_rule_quote"] & frame["side"].eq("bid") & frame["fill_label"].eq(1),
        "filled_ask": frame["event_rule_quote"] & frame["side"].eq("ask") & frame["fill_label"].eq(1),
        "adverse_fill": frame["event_rule_quote"] & frame["label_valid_1s"].eq(1) & frame["maker_markout_1s_ticks"].lt(0),
        "favorable_fill": frame["event_rule_quote"] & frame["label_valid_1s"].eq(1) & frame["maker_markout_1s_ticks"].gt(0),
    }
    rng = np.random.default_rng(SEED)
    samples = []
    columns = [
        "date", "opportunity_id", "decision_event_type", "decision_exchange_time_us",
        "decision_local_time_us", "side", "quote_price", "queue_ahead_lots",
        *EVENT_RULE_VOTES, "event_rule_score", "event_rule_quote", "fill_status",
        "first_fill_exchange_time_us", "first_fill_local_time_us", "filled_qty",
        "queue_consumed_lots", "mid_at_fill_grid", "maker_markout_1s_ticks",
        "post_fill_mid_move_1s_ticks", "invalid_due_to_snapshot",
    ]
    availability: dict[str, int] = {}
    for category, mask in categories.items():
        candidates = np.flatnonzero(mask.to_numpy())
        availability[category] = int(len(candidates))
        if len(candidates) == 0:
            continue
        selected = rng.choice(candidates, size=min(3, len(candidates)), replace=False)
        sample = frame.iloc[selected][columns].copy()
        sample.insert(0, "audit_category", category)
        samples.append(sample)
    audit = pd.concat(samples, ignore_index=True)
    write_csv(OUTPUT_ROOT / "manual_audit_examples.csv", audit)
    model_outcome = {
        "event_rule": {
            "selected_model": "event_rule_5_of_6",
            "quoted_and_rejected_traces_available": True,
            "filled_bid_ask_and_markout_traces_available": True,
        },
        "lightgbm": {
            "selected_model": "lightgbm_static_p0_margin_-1",
            "quoted_rows_across_development_validation_folds": 0,
            "trace_limitation": "all decisions rejected; filled quote traces do not exist",
        },
        "deep": {
            "selected_model": "deep_tcn_128_margin_+0",
            "quoted_rows_across_development_validation_folds": 0,
            "trace_limitation": "all decisions rejected; filled quote traces do not exist",
        },
    }
    payload: dict[str, Any] = {
        "schema": "event-model-manual-audit-v1",
        "date": "2026-05-01",
        "seed": SEED,
        "category_availability": availability,
        "sample_rows": int(len(audit)),
        "examples_sha256": sha256(OUTPUT_ROOT / "manual_audit_examples.csv"),
        "model_outcome": model_outcome,
    }
    write_json(OUTPUT_ROOT / "manual_audit.json", payload)
    return payload


def main() -> None:
    print(json.dumps(run(), sort_keys=True))


if __name__ == "__main__":
    main()
