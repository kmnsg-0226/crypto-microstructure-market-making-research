"""Loop-6 causal price-momentum gates for OBI maker round trips."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.obi.maker_roundtrip_search import (
    SIGNALS,
    _build_exit_lookup,
    _pair_values,
    _policy_name,
    _read_full_fills,
    aggregate_development,
)
from pyresearch.obi.passive_entry_search import (
    PASSIVE_THRESHOLDS_PATH,
    OBI_THRESHOLDS_PATH,
    _placement_mask,
    _side_safety,
    next_unsafe_indices,
)
from pyresearch.obi.search import REPORT_ROOT, _model_inputs, derive_arrays, load_day
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
SPEC_PATH = ROOT / "research/specs/obi_momentum_gate_search_spec.json"
OUTPUT_ROOT = REPORT_ROOT / "loop6_momentum_gate"
THRESHOLDS_PATH = OUTPUT_ROOT / "threshold_audit.json"
DAY_METRICS_PATH = OUTPUT_ROOT / "development_day_metrics.csv"
AGGREGATE_PATH = OUTPUT_ROOT / "development_metrics.csv"
RANKING_PATH = OUTPUT_ROOT / "development_ranking.csv"
SHORTLIST_PATH = OUTPUT_ROOT / "shortlist_before_exact_execution.json"
EXACT_CAPACITY_PATH = OUTPUT_ROOT / "exact_nonoverlap_capacity_audit.json"
DEVELOPMENT_DATES = [f"2026-0{month}-01" for month in range(1, 6)]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_threshold_audit() -> dict[str, Any]:
    spec = _load_json(SPEC_PATH)
    if spec["status"] != "declared_after_loop5_failure_before_momentum_gate_outcomes":
        raise ValueError("loop-6 disclosure status changed")
    passive_hash = sha256(PASSIVE_THRESHOLDS_PATH)
    obi_hash = sha256(OBI_THRESHOLDS_PATH)
    if passive_hash != spec["audit"]["passive_approach_thresholds_sha256"]:
        raise ValueError("passive thresholds changed before loop 6")
    if obi_hash != spec["audit"]["obi_stage1_thresholds_sha256"]:
        raise ValueError("OBI thresholds changed before loop 6")
    passive = _load_json(PASSIVE_THRESHOLDS_PATH)
    obi = _load_json(OBI_THRESHOLDS_PATH)
    quantiles = [float(value) for value in spec["entry"]["absolute_tail_quantiles"]]
    signal_thresholds = {
        signal: {
            f"q{quantile:.4f}": float(
                obi["signal_absolute_thresholds"][signal][f"q{quantile:.4f}"]
            )
            for quantile in quantiles
        }
        for signal in SIGNALS
    }
    payload = {
        "schema": "obi-momentum-gate-threshold-audit-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "passive_thresholds_sha256": passive_hash,
        "obi_thresholds_sha256": obi_hash,
        "outcomes_read": False,
        "queue_bottom20_btc": float(passive["thresholds"]["queue_ahead_initial"]["q20"]),
        "signal_absolute_thresholds": signal_thresholds,
    }
    write_json(THRESHOLDS_PATH, payload)
    return payload


def causal_backward_move(
    mid: np.ndarray,
    segments: np.ndarray,
    *,
    steps: int,
) -> np.ndarray:
    """Past-only mid move, invalidated across feature-segment boundaries."""
    mid = np.asarray(mid, dtype="float64")
    segments = np.asarray(segments, dtype="float64")
    if len(mid) != len(segments) or steps <= 0:
        raise ValueError("invalid backward-move inputs")
    result = np.full(len(mid), np.nan, dtype="float64")
    current = np.arange(steps, len(mid))
    lagged = current - steps
    valid = (
        np.isfinite(mid[current])
        & np.isfinite(mid[lagged])
        & np.isfinite(segments[current])
        & (segments[current] == segments[lagged])
    )
    result[current[valid]] = mid[current[valid]] - mid[lagged[valid]]
    return result


def momentum_regime_masks(
    *,
    side_is_bid: np.ndarray,
    queue_low: np.ndarray,
    move_5m: np.ndarray,
    move_1h: np.ndarray,
) -> dict[str, np.ndarray]:
    position_sign = np.where(side_is_bid, 1.0, -1.0)
    oriented_5m = position_sign * move_5m
    oriented_1h = position_sign * move_1h
    aligned_5m = np.isfinite(oriented_5m) & (oriented_5m > 0)
    opposed_5m = np.isfinite(oriented_5m) & (oriented_5m < 0)
    aligned_1h = np.isfinite(oriented_1h) & (oriented_1h > 0)
    opposed_1h = np.isfinite(oriented_1h) & (oriented_1h < 0)
    both_aligned = aligned_5m & aligned_1h
    both_opposed = opposed_5m & opposed_1h
    pullback = opposed_5m & aligned_1h
    reversal = aligned_5m & opposed_1h
    return {
        "all": np.ones(len(side_is_bid), dtype=bool),
        "queue_bottom20": queue_low,
        "momentum_5m_aligned": aligned_5m,
        "momentum_5m_opposed": opposed_5m,
        "momentum_1h_aligned": aligned_1h,
        "momentum_1h_opposed": opposed_1h,
        "momentum_both_aligned": both_aligned,
        "momentum_both_opposed": both_opposed,
        "pullback_with_1h_aligned": pullback,
        "reversal_against_1h": reversal,
        "momentum_both_aligned_queue_bottom20": both_aligned & queue_low,
        "momentum_both_opposed_queue_bottom20": both_opposed & queue_low,
        "pullback_with_1h_aligned_queue_bottom20": pullback & queue_low,
    }


def _regime_totals(
    regime_masks: dict[str, np.ndarray],
    selected: np.ndarray,
    values: dict[str, np.ndarray],
) -> dict[str, tuple[int, dict[str, float]]]:
    chosen = selected & np.isfinite(values["primary"])
    selected_index = np.flatnonzero(chosen)
    selected_values = {name: value[selected_index] for name, value in values.items()}
    result = {}
    for regime, mask in regime_masks.items():
        keep = mask[selected_index]
        count = int(keep.sum())
        result[regime] = (
            count,
            {
                **{
                    name: float(array[keep].sum())
                    for name, array in selected_values.items()
                },
                "wins": float((selected_values["primary"][keep] > 0).sum()),
            },
        )
    return result


def evaluate_day(
    date: str,
    thresholds: dict[str, Any],
    *,
    spec_path: Path = SPEC_PATH,
) -> pd.DataFrame:
    spec = _load_json(spec_path)
    frame = load_day(date)
    model, transforms = _model_inputs()
    signals, context = derive_arrays(frame, model=model, transforms=transforms)
    day_start = int(context["sample_time_us"][0])
    feature_times = context["sample_time_us"]
    segments = context["feature_segment_id"]
    mid = frame["mid"].to_numpy(dtype="float64")
    move_5m_all = causal_backward_move(mid, segments, steps=3000)
    move_1h_all = causal_backward_move(mid, segments, steps=36000)
    queue_cutoff = float(thresholds["queue_bottom20_btc"])
    quantiles = [float(value) for value in spec["entry"]["absolute_tail_quantiles"]]
    fee_scenarios = [float(value) for value in spec["fees"]["maker_fee_bps_per_leg_scenarios"]]
    primary_fee = float(spec["fees"]["primary_maker_fee_bps_per_leg"])
    exit_lifetimes = [int(value) for value in spec["exit"]["quote_lifetimes_ms"]]
    hold_delays = [int(value) for value in spec["exit"]["additional_hold_delays_ms"]]
    exit_lookups = {
        lifetime: _build_exit_lookup(
            _read_full_fills(date, lifetime), day_start_us=day_start, rows=len(frame)
        )
        for lifetime in exit_lifetimes
    }
    rows = []

    for entry_lifetime in [int(value) for value in spec["entry"]["quote_lifetimes_ms"]]:
        fills = _read_full_fills(date, entry_lifetime)
        decision_time = fills["decision_time_us"].to_numpy(dtype="int64")
        decision_index = ((decision_time - day_start) // 100_000).astype("int64")
        if np.any(decision_time - day_start != decision_index * 100_000):
            raise ValueError("entry decision is off grid")
        side_is_bid = fills["side"].eq("bid").to_numpy()
        full_fill_local = fills["full_fill_local_time_us"].to_numpy(dtype="int64")
        queue_low = fills["queue_ahead_initial"].to_numpy(dtype="float64") <= queue_cutoff
        regime_masks = momentum_regime_masks(
            side_is_bid=side_is_bid,
            queue_low=queue_low,
            move_5m=move_5m_all[decision_index],
            move_1h=move_1h_all[decision_index],
        )
        if tuple(regime_masks) != tuple(spec["placement_regimes"]):
            raise ValueError("momentum regime catalog differs from declared spec")
        pair_metrics = {
            (exit_lifetime, hold): _pair_values(
                entry_fills=fills,
                entry_side_is_bid=side_is_bid,
                exit_lookup=exit_lookups[exit_lifetime],
                context=context,
                day_start_us=day_start,
                minimum_delay_ms=int(spec["exit"]["minimum_placement_delay_after_entry_fill_ms"]),
                additional_hold_ms=hold,
                fee_scenarios=fee_scenarios,
            )
            for exit_lifetime in exit_lifetimes
            for hold in hold_delays
        }

        for signal_name in SIGNALS:
            feature_values = signals[signal_name]
            placement_values = fills[signal_name].to_numpy(dtype="float64")
            if not np.allclose(
                placement_values, feature_values[decision_index], equal_nan=True, rtol=0, atol=1e-9
            ):
                raise ValueError(f"passive/feature signal mismatch: {signal_name}")
            threshold_map = thresholds["signal_absolute_thresholds"][signal_name]
            for quantile in quantiles:
                threshold = float(threshold_map[f"q{quantile:.4f}"])
                for direction in spec["entry"]["directions"]:
                    placement = _placement_mask(
                        placement_values, side_is_bid, threshold=threshold, direction=direction
                    )
                    cancel_masks = {}
                    for cancel_mode in spec["entry"]["cancel_modes"]:
                        if cancel_mode == "fixed_lifetime":
                            cancel_masks[cancel_mode] = np.ones(len(fills), dtype=bool)
                        elif cancel_mode == "cancel_on_tail_exit":
                            bid_safety = _side_safety(
                                feature_values, threshold=threshold, direction=direction,
                                side="bid", mode=cancel_mode,
                            )
                            ask_safety = _side_safety(
                                feature_values, threshold=threshold, direction=direction,
                                side="ask", mode=cancel_mode,
                            )
                            bid_bad = next_unsafe_indices(bid_safety, segments)
                            ask_bad = next_unsafe_indices(ask_safety, segments)
                            next_bad = np.where(
                                side_is_bid, bid_bad[decision_index], ask_bad[decision_index]
                            )
                            cancel_effective = np.full(len(fills), np.iinfo("int64").max)
                            has_cancel = next_bad < len(feature_times)
                            cancel_effective[has_cancel] = feature_times[next_bad[has_cancel]] + 100_000
                            cancel_masks[cancel_mode] = full_fill_local <= cancel_effective
                        else:
                            raise ValueError(f"unknown cancel mode: {cancel_mode}")

                    for (exit_lifetime, hold), metrics in pair_metrics.items():
                        values = {
                            "gross": metrics["gross"],
                            **{
                                f"net_maker_{fee:g}bps": metrics[f"net_maker_{fee:g}bps"]
                                for fee in fee_scenarios
                            },
                            "primary": metrics[f"net_maker_{primary_fee:g}bps"],
                        }
                        for cancel_mode, survives_cancel in cancel_masks.items():
                            selected = placement & survives_cancel & metrics["valid"]
                            totals = _regime_totals(regime_masks, selected, values)
                            for regime, (count, sums) in totals.items():
                                rows.append({
                                    "date": date,
                                    "policy": _policy_name(
                                        signal_name, direction, quantile, cancel_mode,
                                        entry_lifetime, hold, exit_lifetime, regime,
                                    ),
                                    "signal": signal_name,
                                    "direction": direction,
                                    "tail_quantile": quantile,
                                    "absolute_signal_threshold": threshold,
                                    "cancel_mode": cancel_mode,
                                    "entry_quote_lifetime_ms": entry_lifetime,
                                    "additional_hold_ms": hold,
                                    "exit_quote_lifetime_ms": exit_lifetime,
                                    "regime": regime,
                                    "completed_roundtrips": count,
                                    "gross_mean_bps": sums["gross"] / count if count else np.nan,
                                    "net_maker_0bps_mean_bps": sums["net_maker_0bps"] / count if count else np.nan,
                                    "net_maker_1bps_mean_bps": sums["net_maker_1bps"] / count if count else np.nan,
                                    "net_maker_2bps_mean_bps": sums["net_maker_2bps"] / count if count else np.nan,
                                    "primary_net_mean_bps": sums["primary"] / count if count else np.nan,
                                    "primary_net_total_bps": sums["primary"],
                                    "primary_net_positive_probability": sums["wins"] / count if count else np.nan,
                                })
    return pd.DataFrame(rows)


def rank_development(
    aggregate: pd.DataFrame,
    *,
    spec_path: Path = SPEC_PATH,
) -> pd.DataFrame:
    gate = _load_json(spec_path)["development_gate"]
    result = aggregate.copy()
    result["eligible_activity"] = (
        (result["completed_roundtrips"] >= int(gate["minimum_completed_roundtrips_total"]))
        & (
            result["minimum_completed_roundtrips_day"]
            >= int(gate["minimum_completed_roundtrips_each_day"])
        )
    )
    behavior = [
        "signal", "direction", "absolute_signal_threshold", "cancel_mode",
        "entry_quote_lifetime_ms", "additional_hold_ms", "exit_quote_lifetime_ms", "regime",
    ]
    result["duplicate_behavior"] = result.duplicated(behavior, keep="first")
    result["advances_to_exact_execution"] = (
        result["eligible_activity"]
        & ~result["duplicate_behavior"]
        & (result["positive_development_days"] == len(DEVELOPMENT_DATES))
        & (result["worst_development_day_net_bps"] > 0)
        & (result["primary_net_mean_bps"] > 0)
    )
    result.sort_values(
        [
            "advances_to_exact_execution", "eligible_activity", "positive_development_days",
            "worst_development_day_net_bps", "primary_net_mean_bps",
            "completed_roundtrips", "policy",
        ],
        ascending=[False, False, False, False, False, False, True],
        kind="stable", inplace=True, ignore_index=True,
    )
    result.insert(0, "rank", np.arange(1, len(result) + 1))
    return result


def run_development() -> dict[str, Any]:
    thresholds = build_threshold_audit()
    day = pd.concat([evaluate_day(date, thresholds) for date in DEVELOPMENT_DATES], ignore_index=True)
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
        raise ValueError(f"unexpected momentum-gate rows: {len(day)} != {expected}")
    write_csv(DAY_METRICS_PATH, day)
    aggregate = aggregate_development(day)
    write_csv(AGGREGATE_PATH, aggregate)
    ranking = rank_development(aggregate)
    write_csv(RANKING_PATH, ranking)
    shortlist_size = int(spec["development_gate"]["shortlist_size"])
    survivors = ranking.loc[ranking["advances_to_exact_execution"]].head(shortlist_size)
    diagnostic = ranking.loc[
        ranking["eligible_activity"] & ~ranking["duplicate_behavior"]
    ].head(shortlist_size)
    write_json(SHORTLIST_PATH, {
        "schema": "obi-momentum-gate-shortlist-v1",
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


def run_exact_capacity_audit() -> dict[str, Any]:
    """Reject impossible one-position followups before outcome-dependent replay."""
    spec = _load_json(SPEC_PATH)
    shortlist = _load_json(SHORTLIST_PATH)
    if shortlist["development_ranking_sha256"] != sha256(RANKING_PATH):
        raise ValueError("momentum shortlist/ranking hash mismatch")
    survivors = shortlist["survivors_for_exact_execution"]
    if not survivors:
        raise ValueError("no momentum-gate survivors to capacity-audit")
    holds = sorted({int(row["additional_hold_ms"]) for row in survivors})
    minimum_hold_ms = min(holds)
    minimum_delay_ms = int(spec["exit"]["minimum_placement_delay_after_entry_fill_ms"])
    optimistic_minimum_occupied_us = (minimum_hold_ms + minimum_delay_ms) * 1000
    optimistic_max_roundtrips_per_day = 86_400_000_000 // optimistic_minimum_occupied_us
    optimistic_max_roundtrips_total = (
        optimistic_max_roundtrips_per_day * len(DEVELOPMENT_DATES)
    )
    gate = spec["development_gate"]
    required_each_day = int(gate["minimum_completed_roundtrips_each_day"])
    required_total = int(gate["minimum_completed_roundtrips_total"])
    can_meet_activity_gate = (
        optimistic_max_roundtrips_per_day >= required_each_day
        and optimistic_max_roundtrips_total >= required_total
    )
    payload = {
        "schema": "obi-momentum-exact-capacity-audit-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "shortlist_sha256": sha256(SHORTLIST_PATH),
        "development_ranking_sha256": sha256(RANKING_PATH),
        "survivor_count": len(survivors),
        "survivor_holding_delays_ms": holds,
        "one_pending_or_open_position_at_a_time": True,
        "optimistic_assumptions": [
            "zero_entry_fill_latency",
            "zero_exit_fill_latency",
            "no_unfilled_or_partial_order_blocking",
            "first_order_may_start_at_day_boundary",
        ],
        "optimistic_minimum_occupied_us": optimistic_minimum_occupied_us,
        "optimistic_max_completed_roundtrips_per_day": int(
            optimistic_max_roundtrips_per_day
        ),
        "optimistic_max_completed_roundtrips_five_days": int(
            optimistic_max_roundtrips_total
        ),
        "required_completed_roundtrips_each_day": required_each_day,
        "required_completed_roundtrips_total": required_total,
        "can_meet_activity_gate": bool(can_meet_activity_gate),
        "advances_to_partial_fill_and_pnl_exact_replay": bool(can_meet_activity_gate),
        "reason_if_rejected": (
            None
            if can_meet_activity_gate
            else "one-position one-hour holding capacity is below the preregistered activity gate"
        ),
        "retrospective_outcomes_read": False,
    }
    write_json(EXACT_CAPACITY_PATH, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("thresholds", "development", "capacity"))
    args = parser.parse_args()
    if args.command == "thresholds":
        result = build_threshold_audit()
    elif args.command == "development":
        result = run_development()
    else:
        result = run_exact_capacity_audit()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
