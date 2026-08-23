"""Loop-5 OBI state-transition maker-exit upper-bound screen."""
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
    _read_full_fills,
    maker_roundtrip_returns,
    target_exit_indices,
)
from pyresearch.obi.passive_entry_search import (
    PASSIVE_THRESHOLDS_PATH,
    OBI_THRESHOLDS_PATH,
    _bucket_totals,
    _placement_mask,
    _side_safety,
    next_unsafe_indices,
)
from pyresearch.obi.search import REPORT_ROOT, _model_inputs, derive_arrays, load_day
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
SPEC_PATH = ROOT / "research/specs/obi_state_exit_search_spec.json"
OUTPUT_ROOT = REPORT_ROOT / "loop5_state_exit"
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
    if spec["status"] != "declared_after_loop4_failure_before_state_exit_outcomes":
        raise ValueError("loop-5 disclosure status changed")
    passive_hash = sha256(PASSIVE_THRESHOLDS_PATH)
    obi_hash = sha256(OBI_THRESHOLDS_PATH)
    if passive_hash != spec["audit"]["passive_approach_thresholds_sha256"]:
        raise ValueError("passive thresholds changed before loop 5")
    if obi_hash != spec["audit"]["obi_stage1_thresholds_sha256"]:
        raise ValueError("OBI thresholds changed before loop 5")
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
        "schema": "obi-state-exit-threshold-audit-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "passive_thresholds_sha256": passive_hash,
        "obi_thresholds_sha256": obi_hash,
        "outcomes_read": False,
        "queue_bottom20_btc": float(passive["thresholds"]["queue_ahead_initial"]["q20"]),
        "signal_absolute_thresholds": signal_thresholds,
    }
    write_json(THRESHOLDS_PATH, payload)
    return payload


def first_true_for_starts(
    condition: np.ndarray,
    segments: np.ndarray,
    starts: np.ndarray,
) -> np.ndarray:
    """First true row at/after each start without crossing its feature segment."""
    condition = np.asarray(condition, dtype=bool)
    segments = np.asarray(segments, dtype="float64")
    starts = np.asarray(starts, dtype="int64")
    if len(condition) != len(segments):
        raise ValueError("condition/segment length mismatch")
    result = np.full(len(starts), len(condition), dtype="int64")
    valid_start = (starts >= 0) & (starts < len(condition))
    hits = np.flatnonzero(condition & np.isfinite(segments))
    if not len(hits):
        return result
    positions = np.searchsorted(hits, starts[valid_start], side="left")
    available = positions < len(hits)
    valid_rows = np.flatnonzero(valid_start)
    rows = valid_rows[available]
    candidates = hits[positions[available]]
    same_segment = (
        np.isfinite(segments[starts[rows]])
        & (segments[starts[rows]] == segments[candidates])
    )
    result[rows[same_segment]] = candidates[same_segment]
    return result


def state_exit_targets(
    *,
    feature_values: np.ndarray,
    segments: np.ndarray,
    entry_side_is_bid: np.ndarray,
    start_indices: np.ndarray,
    cap_indices: np.ndarray,
    threshold: float,
    direction: str,
    condition_name: str,
) -> np.ndarray:
    if direction == "maker_contrarian":
        bid_oriented = -feature_values
        ask_oriented = feature_values
    elif direction == "maker_trend":
        bid_oriented = feature_values
        ask_oriented = -feature_values
    else:
        raise ValueError(f"unknown direction: {direction}")

    def condition(values: np.ndarray) -> np.ndarray:
        finite = np.isfinite(values)
        if condition_name == "entry_tail_exit":
            return finite & (values < threshold)
        if condition_name == "entry_sign_exit":
            return finite & (values <= 0.0)
        if condition_name == "entry_opposite_tail":
            return finite & (values <= -threshold)
        raise ValueError(f"unknown exit state condition: {condition_name}")

    bid_trigger = first_true_for_starts(condition(bid_oriented), segments, start_indices)
    ask_trigger = first_true_for_starts(condition(ask_oriented), segments, start_indices)
    trigger = np.where(entry_side_is_bid, bid_trigger, ask_trigger)
    return np.minimum(trigger, cap_indices)


def _pair_values_at_target(
    *,
    entry_fills: pd.DataFrame,
    entry_side_is_bid: np.ndarray,
    target_index: np.ndarray,
    exit_lookup: dict[str, np.ndarray],
    context: dict[str, np.ndarray],
    day_start_us: int,
    fee_scenarios: list[float],
) -> dict[str, np.ndarray]:
    count = len(entry_fills)
    entry_price = entry_fills["quote_price"].to_numpy(dtype="float64")
    entry_exchange_fill = entry_fills["full_fill_exchange_time_us"].to_numpy(dtype="int64")
    entry_local_fill = entry_fills["full_fill_local_time_us"].to_numpy(dtype="int64")
    entry_segment = entry_fills["feature_segment_id"].to_numpy(dtype="float64")
    target_index = np.asarray(target_index, dtype="int64")
    valid = (target_index >= 0) & (target_index < len(context["sample_time_us"]))
    exit_column = np.where(entry_side_is_bid, 1, 0)
    gathered = {
        name: np.full(count, np.nan, dtype="float64")
        for name in (
            "quote_price", "placement_local_time_us", "full_fill_exchange_time_us",
            "full_fill_local_time_us", "feature_segment_id",
        )
    }
    safe = np.flatnonzero(valid)
    for name, destination in gathered.items():
        destination[safe] = exit_lookup[name][target_index[safe], exit_column[safe]]
    exit_price = gathered["quote_price"]
    exit_placement_local = gathered["placement_local_time_us"]
    exit_exchange_fill = gathered["full_fill_exchange_time_us"]
    exit_local_fill = gathered["full_fill_local_time_us"]
    exit_segment = gathered["feature_segment_id"]
    valid &= np.isfinite(exit_price) & np.isfinite(exit_exchange_fill) & np.isfinite(exit_local_fill)
    target_time = day_start_us + target_index * 100_000
    valid &= exit_placement_local >= target_time
    valid &= exit_local_fill >= exit_placement_local
    valid &= exit_local_fill > entry_local_fill

    entry_fill_index = ((entry_exchange_fill - day_start_us + 99_999) // 100_000).astype("int64")
    exit_fill_index = np.full(count, -1, dtype="int64")
    safe = np.flatnonzero(valid)
    exit_fill_index[safe] = (
        (exit_exchange_fill[safe].astype("int64") - day_start_us + 99_999) // 100_000
    )
    valid &= (entry_fill_index >= 0) & (entry_fill_index < len(context["sample_time_us"]))
    valid &= (exit_fill_index >= 0) & (exit_fill_index < len(context["sample_time_us"]))
    safe = np.flatnonzero(valid)
    if len(safe):
        feature_segment = context["feature_segment_id"]
        valid_book = context["valid_book_state"]
        valid[safe] &= valid_book[entry_fill_index[safe]] & valid_book[exit_fill_index[safe]]
        valid[safe] &= np.isfinite(entry_segment[safe]) & np.isfinite(exit_segment[safe])
        valid[safe] &= entry_segment[safe] == exit_segment[safe]
        valid[safe] &= entry_segment[safe] == feature_segment[target_index[safe]]
        valid[safe] &= entry_segment[safe] == feature_segment[entry_fill_index[safe]]
        valid[safe] &= entry_segment[safe] == feature_segment[exit_fill_index[safe]]

    gross, _ = maker_roundtrip_returns(
        entry_side_is_bid=entry_side_is_bid,
        entry_price=entry_price,
        exit_price=exit_price,
        maker_fee_bps_per_leg=0.0,
    )
    result = {"valid": valid, "gross": np.where(valid, gross, np.nan)}
    for fee in fee_scenarios:
        _, net = maker_roundtrip_returns(
            entry_side_is_bid=entry_side_is_bid,
            entry_price=entry_price,
            exit_price=exit_price,
            maker_fee_bps_per_leg=fee,
        )
        result[f"net_maker_{fee:g}bps"] = np.where(valid, net, np.nan)
    return result


def _policy_name(
    signal: str,
    direction: str,
    quantile: float,
    cancel_mode: str,
    entry_lifetime_ms: int,
    exit_condition: str,
    max_hold_ms: int,
    exit_lifetime_ms: int,
    regime: str,
) -> str:
    return (
        f"{signal}__{direction}__absq{quantile:.4f}__{cancel_mode}__"
        f"entry{entry_lifetime_ms}ms__{exit_condition}__cap{max_hold_ms}ms__"
        f"exit{exit_lifetime_ms}ms__{regime}"
    )


def evaluate_day(date: str, thresholds: dict[str, Any]) -> pd.DataFrame:
    spec = _load_json(SPEC_PATH)
    frame = load_day(date)
    model, transforms = _model_inputs()
    signals, context = derive_arrays(frame, model=model, transforms=transforms)
    day_start = int(context["sample_time_us"][0])
    feature_times = context["sample_time_us"]
    segments = context["feature_segment_id"]
    queue_cutoff = float(thresholds["queue_bottom20_btc"])
    quantiles = [float(value) for value in spec["entry"]["absolute_tail_quantiles"]]
    fee_scenarios = [float(value) for value in spec["fees"]["maker_fee_bps_per_leg_scenarios"]]
    primary_fee = float(spec["fees"]["primary_maker_fee_bps_per_leg"])
    exit_lifetimes = [int(value) for value in spec["exit"]["quote_lifetimes_ms"]]
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
        start_indices = target_exit_indices(
            full_fill_local,
            day_start_us=day_start,
            minimum_delay_ms=100,
            additional_hold_ms=0,
        )
        queue_low = fills["queue_ahead_initial"].to_numpy(dtype="float64") <= queue_cutoff
        hour = ((decision_time // 3_600_000_000) % 24).astype(np.int8)
        session = np.where(hour < 8, 0, np.where(hour < 16, 1, 2)).astype(np.int8)
        buckets = session * 2 + queue_low.astype(np.int8)

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
                        placement_values,
                        side_is_bid,
                        threshold=threshold,
                        direction=direction,
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

                    for max_hold in spec["exit"]["maximum_holding_ms"]:
                        max_hold = int(max_hold)
                        cap_indices = target_exit_indices(
                            full_fill_local,
                            day_start_us=day_start,
                            minimum_delay_ms=0,
                            additional_hold_ms=max_hold,
                        )
                        for exit_condition in spec["exit"]["state_conditions"]:
                            targets = state_exit_targets(
                                feature_values=feature_values,
                                segments=segments,
                                entry_side_is_bid=side_is_bid,
                                start_indices=start_indices,
                                cap_indices=cap_indices,
                                threshold=threshold,
                                direction=direction,
                                condition_name=exit_condition,
                            )
                            for exit_lifetime in exit_lifetimes:
                                metrics = _pair_values_at_target(
                                    entry_fills=fills,
                                    entry_side_is_bid=side_is_bid,
                                    target_index=targets,
                                    exit_lookup=exit_lookups[exit_lifetime],
                                    context=context,
                                    day_start_us=day_start,
                                    fee_scenarios=fee_scenarios,
                                )
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
                                    totals = _bucket_totals(buckets, selected, values)
                                    for regime, (count, sums) in totals.items():
                                        rows.append({
                                            "date": date,
                                            "policy": _policy_name(
                                                signal_name, direction, quantile, cancel_mode,
                                                entry_lifetime, exit_condition, max_hold,
                                                exit_lifetime, regime,
                                            ),
                                            "signal": signal_name,
                                            "direction": direction,
                                            "tail_quantile": quantile,
                                            "absolute_signal_threshold": threshold,
                                            "cancel_mode": cancel_mode,
                                            "entry_quote_lifetime_ms": entry_lifetime,
                                            "exit_condition": exit_condition,
                                            "maximum_holding_ms": max_hold,
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


def aggregate_development(day: pd.DataFrame) -> pd.DataFrame:
    dimensions = [
        "policy", "signal", "direction", "tail_quantile", "absolute_signal_threshold",
        "cancel_mode", "entry_quote_lifetime_ms", "exit_condition", "maximum_holding_ms",
        "exit_quote_lifetime_ms", "regime",
    ]
    mean_columns = [
        "gross_mean_bps", "net_maker_0bps_mean_bps",
        "net_maker_1bps_mean_bps", "net_maker_2bps_mean_bps",
    ]
    rows = []
    for keys, group in day.groupby(dimensions, sort=True, observed=True, dropna=False):
        data = dict(zip(dimensions, keys))
        trades = int(group["completed_roundtrips"].sum())
        row = {
            **data,
            "days": int(group["date"].nunique()),
            "completed_roundtrips": trades,
            "minimum_completed_roundtrips_day": int(group["completed_roundtrips"].min()),
            "positive_development_days": int((group["primary_net_mean_bps"] > 0).sum()),
            "worst_development_day_net_bps": float(group["primary_net_mean_bps"].min()),
            "best_development_day_net_bps": float(group["primary_net_mean_bps"].max()),
            "primary_net_mean_bps": float(group["primary_net_total_bps"].sum() / trades)
            if trades else np.nan,
            "primary_net_positive_probability": float(
                (group["primary_net_positive_probability"] * group["completed_roundtrips"]).sum()
                / trades
            ) if trades else np.nan,
        }
        for column in mean_columns:
            row[column] = float(
                (group[column] * group["completed_roundtrips"]).sum() / trades
            ) if trades else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def rank_development(aggregate: pd.DataFrame) -> pd.DataFrame:
    gate = _load_json(SPEC_PATH)["development_gate"]
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
        "entry_quote_lifetime_ms", "exit_condition", "maximum_holding_ms",
        "exit_quote_lifetime_ms", "regime",
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
        * len(spec["exit"]["state_conditions"])
        * len(spec["exit"]["maximum_holding_ms"])
        * len(spec["exit"]["quote_lifetimes_ms"])
        * len(spec["placement_regimes"])
        * len(DEVELOPMENT_DATES)
    )
    if len(day) != expected:
        raise ValueError(f"unexpected state-exit rows: {len(day)} != {expected}")
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
        "schema": "obi-state-exit-shortlist-v1",
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
