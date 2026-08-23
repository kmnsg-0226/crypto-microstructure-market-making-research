"""Frozen Loop-6 momentum-only ablation and single-use JJA replication."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.obi.maker_roundtrip_search import (
    _build_exit_lookup,
    _pair_values,
    _read_full_fills,
)
from pyresearch.obi.momentum_gate_search import (
    causal_backward_move,
    momentum_regime_masks,
)
from pyresearch.obi.passive_entry_search import _placement_mask
from pyresearch.obi.search import REPORT_ROOT, _model_inputs, derive_arrays, load_day
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
SPEC_PATH = ROOT / "research/specs/loop6_frozen_ablation_spec.json"
LOOP6_RANKING_PATH = REPORT_ROOT / "loop6_momentum_gate/development_ranking.csv"
LOOP6_SHORTLIST_PATH = REPORT_ROOT / "loop6_momentum_gate/shortlist_before_exact_execution.json"
OUTPUT_ROOT = REPORT_ROOT / "loop6_frozen_ablation"
DEVELOPMENT_DAY_PATH = OUTPUT_ROOT / "development_ablation_day_metrics.csv"
DEVELOPMENT_SUMMARY_PATH = OUTPUT_ROOT / "development_ablation_summary.json"
VALIDATION_DAY_PATH = OUTPUT_ROOT / "june_july_august_day_metrics.csv"
VALIDATION_SUMMARY_PATH = OUTPUT_ROOT / "june_july_august_summary.json"
VALIDATION_RECEIPT_PATH = OUTPUT_ROOT / "june_july_august_single_use_receipt.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def audit_frozen_inputs() -> dict[str, Any]:
    spec = _load_json(SPEC_PATH)
    expected = "frozen_before_loop6_ablation_and_june_july_august_replication"
    if spec["status"] != expected:
        raise ValueError("Loop-6 frozen ablation status changed")
    if sha256(LOOP6_RANKING_PATH) != spec["audit"]["loop6_development_ranking_sha256"]:
        raise ValueError("Loop-6 development ranking changed after freeze")
    if sha256(LOOP6_SHORTLIST_PATH) != spec["audit"]["loop6_shortlist_sha256"]:
        raise ValueError("Loop-6 shortlist changed after freeze")
    ranking = pd.read_csv(LOOP6_RANKING_PATH)
    winner = ranking.loc[ranking["rank"] == 1]
    if len(winner) != 1:
        raise ValueError("Loop-6 rank-1 policy is not unique")
    frozen = spec["frozen_obi_plus_momentum_policy"]
    if winner.iloc[0]["policy"] != frozen["policy"]:
        raise ValueError("frozen policy does not match Loop-6 development rank 1")
    return spec


def summarize_variant(
    *,
    date: str,
    variant: str,
    selected: np.ndarray,
    gross: np.ndarray,
    net: np.ndarray,
) -> dict[str, Any]:
    chosen = selected & np.isfinite(gross) & np.isfinite(net)
    count = int(chosen.sum())
    return {
        "date": date,
        "variant": variant,
        "completed_roundtrips": count,
        "gross_mean_bps": float(gross[chosen].mean()) if count else np.nan,
        "primary_net_mean_bps": float(net[chosen].mean()) if count else np.nan,
        "primary_net_total_bps": float(net[chosen].sum()),
        "primary_net_positive_probability": float((net[chosen] > 0).mean())
        if count else np.nan,
    }


def evaluate_day(date: str, spec: dict[str, Any]) -> pd.DataFrame:
    policy = spec["frozen_obi_plus_momentum_policy"]
    frame = load_day(date)
    model, transforms = _model_inputs()
    signals, context = derive_arrays(frame, model=model, transforms=transforms)
    day_start = int(context["sample_time_us"][0])
    segments = context["feature_segment_id"]
    mid = frame["mid"].to_numpy(dtype="float64")
    move_5m = causal_backward_move(mid, segments, steps=3000)
    move_1h = causal_backward_move(mid, segments, steps=36000)

    entry_lifetime = int(policy["entry_quote_lifetime_ms"])
    exit_lifetime = int(policy["exit_quote_lifetime_ms"])
    fills = _read_full_fills(date, entry_lifetime)
    exit_lookup = _build_exit_lookup(
        _read_full_fills(date, exit_lifetime),
        day_start_us=day_start,
        rows=len(frame),
    )
    decision_time = fills["decision_time_us"].to_numpy(dtype="int64")
    decision_index = ((decision_time - day_start) // 100_000).astype("int64")
    if np.any(decision_time - day_start != decision_index * 100_000):
        raise ValueError("frozen ablation entry decision is off grid")
    side_is_bid = fills["side"].eq("bid").to_numpy()
    pair = _pair_values(
        entry_fills=fills,
        entry_side_is_bid=side_is_bid,
        exit_lookup=exit_lookup,
        context=context,
        day_start_us=day_start,
        minimum_delay_ms=int(
            spec["execution"]["minimum_exit_placement_delay_after_entry_fill_ms"]
        ),
        additional_hold_ms=int(policy["hold_ms"]),
        fee_scenarios=[float(spec["execution"]["maker_fee_bps_per_leg"])],
    )
    regimes = momentum_regime_masks(
        side_is_bid=side_is_bid,
        queue_low=np.zeros(len(fills), dtype=bool),
        move_5m=move_5m[decision_index],
        move_1h=move_1h[decision_index],
    )
    momentum_only = regimes[policy["regime"]] & pair["valid"]
    signal_name = str(policy["signal"])
    placement_values = fills[signal_name].to_numpy(dtype="float64")
    if not np.allclose(
        placement_values,
        signals[signal_name][decision_index],
        equal_nan=True,
        rtol=0,
        atol=1e-9,
    ):
        raise ValueError("frozen ablation signal differs from feature source")
    obi_filter = _placement_mask(
        placement_values,
        side_is_bid,
        threshold=float(policy["absolute_signal_threshold"]),
        direction=str(policy["direction"]),
    )
    fee = float(spec["execution"]["maker_fee_bps_per_leg"])
    net = pair[f"net_maker_{fee:g}bps"]
    rows = [
        summarize_variant(
            date=date,
            variant="momentum_only",
            selected=momentum_only,
            gross=pair["gross"],
            net=net,
        ),
        summarize_variant(
            date=date,
            variant="obi_plus_momentum",
            selected=momentum_only & obi_filter,
            gross=pair["gross"],
            net=net,
        ),
    ]
    return pd.DataFrame(rows)


def aggregate(day: pd.DataFrame) -> dict[str, Any]:
    variants = {}
    for variant, group in day.groupby("variant", sort=True):
        count = int(group["completed_roundtrips"].sum())
        variants[variant] = {
            "dates": int(group["date"].nunique()),
            "completed_roundtrips": count,
            "minimum_completed_roundtrips_day": int(
                group["completed_roundtrips"].min()
            ),
            "positive_days": int((group["primary_net_mean_bps"] > 0).sum()),
            "worst_day_net_bps": float(group["primary_net_mean_bps"].min()),
            "best_day_net_bps": float(group["primary_net_mean_bps"].max()),
            "pooled_gross_mean_bps": float(
                (group["gross_mean_bps"] * group["completed_roundtrips"]).sum()
                / count
            ) if count else np.nan,
            "pooled_primary_net_mean_bps": float(
                group["primary_net_total_bps"].sum() / count
            ) if count else np.nan,
        }
    return variants


def validation_gate(
    day: pd.DataFrame,
    summary: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    gate = spec["retrospective_success_gate"]
    obi_day = day.loc[day["variant"] == "obi_plus_momentum"]
    obi = summary["obi_plus_momentum"]
    momentum = summary["momentum_only"]
    checks = {
        "obi_plus_momentum_net_positive_each_month": bool(
            (obi_day["primary_net_mean_bps"] > 0).all()
        ),
        "obi_plus_momentum_minimum_activity_each_month": bool(
            (
                obi_day["completed_roundtrips"]
                >= int(gate["obi_plus_momentum_completed_roundtrips_minimum_each_month"])
            ).all()
        ),
        "obi_plus_momentum_pooled_net_positive": bool(
            obi["pooled_primary_net_mean_bps"] > 0
        ),
        "obi_increment_over_momentum_only_positive": bool(
            obi["pooled_primary_net_mean_bps"]
            > momentum["pooled_primary_net_mean_bps"]
        ),
    }
    return {"checks": checks, "passes": bool(all(checks.values()))}


def run_development() -> dict[str, Any]:
    spec = audit_frozen_inputs()
    day = pd.concat(
        [evaluate_day(date, spec) for date in spec["development_dates"]],
        ignore_index=True,
    )
    write_csv(DEVELOPMENT_DAY_PATH, day)
    summary = {
        "schema": "loop6-frozen-development-ablation-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "dates": spec["development_dates"],
        "variants": aggregate(day),
        "day_metrics_sha256": sha256(DEVELOPMENT_DAY_PATH),
    }
    write_json(DEVELOPMENT_SUMMARY_PATH, summary)
    return summary


def run_single_use_validation() -> dict[str, Any]:
    if VALIDATION_RECEIPT_PATH.exists():
        raise RuntimeError(
            "JJA validation receipt already exists; use verify instead of reevaluating"
        )
    spec = audit_frozen_inputs()
    day = pd.concat(
        [evaluate_day(date, spec) for date in spec["single_use_retrospective_dates"]],
        ignore_index=True,
    )
    write_csv(VALIDATION_DAY_PATH, day)
    variants = aggregate(day)
    gate = validation_gate(day, variants, spec)
    summary = {
        "schema": "loop6-frozen-jja-replication-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "dates": spec["single_use_retrospective_dates"],
        "variants": variants,
        "retrospective_success_gate": gate,
        "exact_capacity_failure_still_applies": True,
        "not_live_profitability_proof": True,
        "day_metrics_sha256": sha256(VALIDATION_DAY_PATH),
    }
    write_json(VALIDATION_SUMMARY_PATH, summary)
    receipt = {
        "schema": "loop6-frozen-jja-single-use-receipt-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "validation_day_metrics_sha256": sha256(VALIDATION_DAY_PATH),
        "validation_summary_sha256": sha256(VALIDATION_SUMMARY_PATH),
        "evaluation_count": 1,
        "gate_passes": gate["passes"],
        "may_create_intraday_research_branch": gate["passes"],
    }
    write_json(VALIDATION_RECEIPT_PATH, receipt)
    return summary


def verify_receipt() -> dict[str, Any]:
    receipt = _load_json(VALIDATION_RECEIPT_PATH)
    checks = {
        "spec": receipt["spec_sha256"] == sha256(SPEC_PATH),
        "day_metrics": receipt["validation_day_metrics_sha256"]
        == sha256(VALIDATION_DAY_PATH),
        "summary": receipt["validation_summary_sha256"]
        == sha256(VALIDATION_SUMMARY_PATH),
        "single_use": receipt["evaluation_count"] == 1,
    }
    return {"checks": checks, "valid": bool(all(checks.values())), "receipt": receipt}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "development", "validation", "verify"))
    args = parser.parse_args()
    if args.command == "audit":
        result = audit_frozen_inputs()
    elif args.command == "development":
        result = run_development()
    elif args.command == "validation":
        result = run_single_use_validation()
    else:
        result = verify_receipt()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
