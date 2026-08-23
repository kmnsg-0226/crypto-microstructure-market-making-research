"""Loop-4 OBI maker-entry to maker-exit full-fill screen."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.obi.passive_entry_search import (
    PASSIVE_ROOT,
    PASSIVE_THRESHOLDS_PATH,
    OBI_THRESHOLDS_PATH,
    REGIME_BUCKETS,
    _bucket_totals,
    _placement_mask,
    _side_safety,
    next_unsafe_indices,
)
from pyresearch.obi.search import FEATURE_ROOT, REPORT_ROOT, _model_inputs, derive_arrays, load_day
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
SPEC_PATH = ROOT / "research/specs/obi_maker_roundtrip_search_spec.json"
OUTPUT_ROOT = REPORT_ROOT / "loop4_maker_roundtrip"
THRESHOLDS_PATH = OUTPUT_ROOT / "threshold_audit.json"
DAY_METRICS_PATH = OUTPUT_ROOT / "development_day_metrics.csv"
AGGREGATE_PATH = OUTPUT_ROOT / "development_metrics.csv"
RANKING_PATH = OUTPUT_ROOT / "development_ranking.csv"
SHORTLIST_PATH = OUTPUT_ROOT / "shortlist_before_exact_execution.json"
DEVELOPMENT_DATES = [f"2026-0{month}-01" for month in range(1, 6)]
SIGNALS = (
    "obi_l1",
    "obi_l5",
    "weighted_obi_l10",
    "weighted_mid_minus_mid_ticks",
    "combined_prediction_1s_ticks",
    "ti_1s",
)
PASSIVE_COLUMNS = [
    "date",
    "decision_time_us",
    "placement_local_time_us",
    "feature_segment_id",
    "side",
    "quote_price",
    "quote_lifetime_ms",
    "queue_ahead_initial",
    *SIGNALS,
    "fill_status",
    "full_fill_exchange_time_us",
    "full_fill_local_time_us",
]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_threshold_audit() -> dict[str, Any]:
    spec = _load_json(SPEC_PATH)
    if spec["status"] != "declared_after_loop3_failure_before_maker_roundtrip_outcomes":
        raise ValueError("loop-4 disclosure status changed")
    passive_hash = sha256(PASSIVE_THRESHOLDS_PATH)
    obi_hash = sha256(OBI_THRESHOLDS_PATH)
    if passive_hash != spec["audit"]["passive_approach_thresholds_sha256"]:
        raise ValueError("passive thresholds changed before loop 4")
    if obi_hash != spec["audit"]["obi_stage1_thresholds_sha256"]:
        raise ValueError("OBI thresholds changed before loop 4")
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
        "schema": "obi-maker-roundtrip-threshold-audit-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "passive_thresholds_sha256": passive_hash,
        "obi_thresholds_sha256": obi_hash,
        "outcomes_read": False,
        "queue_bottom20_btc": float(passive["thresholds"]["queue_ahead_initial"]["q20"]),
        "signal_absolute_thresholds": signal_thresholds,
    }
    write_json(THRESHOLDS_PATH, payload)
    return payload


def maker_roundtrip_returns(
    *,
    entry_side_is_bid: np.ndarray,
    entry_price: np.ndarray,
    exit_price: np.ndarray,
    maker_fee_bps_per_leg: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Return gross/net bps for long bid->ask and short ask->bid maker pairs."""
    gross_cash = np.where(
        entry_side_is_bid,
        exit_price - entry_price,
        entry_price - exit_price,
    )
    gross_bps = gross_cash / entry_price * 10_000.0
    fee_rate = maker_fee_bps_per_leg / 10_000.0
    net_cash = gross_cash - fee_rate * (entry_price + exit_price)
    net_bps = net_cash / entry_price * 10_000.0
    return gross_bps, net_bps


def target_exit_indices(
    entry_full_fill_local_time_us: np.ndarray,
    *,
    day_start_us: int,
    minimum_delay_ms: int,
    additional_hold_ms: int,
) -> np.ndarray:
    """First 100 ms decision grid at least delay+hold after locally known fill."""
    target = (
        np.asarray(entry_full_fill_local_time_us, dtype="int64")
        + (minimum_delay_ms + additional_hold_ms) * 1000
    )
    return ((target - day_start_us + 99_999) // 100_000).astype("int64")


def _read_full_fills(date: str, lifetime_ms: int) -> pd.DataFrame:
    frame = pd.read_parquet(
        PASSIVE_ROOT / date / "labeled_probes.parquet",
        columns=PASSIVE_COLUMNS,
        filters=[("quote_lifetime_ms", "=", lifetime_ms), ("fill_status", "=", "full")],
    )
    if frame.empty or not frame["quote_lifetime_ms"].eq(lifetime_ms).all():
        raise ValueError(f"missing passive full fills: {date} {lifetime_ms}")
    if not frame["fill_status"].eq("full").all() or frame["date"].nunique() != 1:
        raise ValueError("passive full-fill filter failed")
    if frame[["full_fill_exchange_time_us", "full_fill_local_time_us"]].isna().any().any():
        raise ValueError("full fill is missing a timestamp")
    return frame


def _build_exit_lookup(
    fills: pd.DataFrame,
    *,
    day_start_us: int,
    rows: int,
) -> dict[str, np.ndarray]:
    decision = fills["decision_time_us"].to_numpy(dtype="int64")
    offset = decision - day_start_us
    if np.any(offset % 100_000):
        raise ValueError("exit decisions are not on the 100 ms grid")
    index = offset // 100_000
    side = fills["side"].map({"bid": 0, "ask": 1})
    if side.isna().any() or np.any(index < 0) or np.any(index >= rows):
        raise ValueError("invalid exit lookup key")
    side_index = side.to_numpy(dtype="int64")
    keys = index * 2 + side_index
    if len(np.unique(keys)) != len(keys):
        raise ValueError("duplicate full-fill exit probe")
    shape = (rows, 2)
    lookup = {
        "quote_price": np.full(shape, np.nan, dtype="float64"),
        "placement_local_time_us": np.full(shape, np.nan, dtype="float64"),
        "full_fill_exchange_time_us": np.full(shape, np.nan, dtype="float64"),
        "full_fill_local_time_us": np.full(shape, np.nan, dtype="float64"),
        "feature_segment_id": np.full(shape, np.nan, dtype="float64"),
    }
    for name in lookup:
        lookup[name][index, side_index] = fills[name].to_numpy(dtype="float64")
    return lookup


def _pair_values(
    *,
    entry_fills: pd.DataFrame,
    entry_side_is_bid: np.ndarray,
    exit_lookup: dict[str, np.ndarray],
    context: dict[str, np.ndarray],
    day_start_us: int,
    minimum_delay_ms: int,
    additional_hold_ms: int,
    fee_scenarios: list[float],
) -> dict[str, np.ndarray]:
    count = len(entry_fills)
    entry_price = entry_fills["quote_price"].to_numpy(dtype="float64")
    entry_exchange_fill = entry_fills["full_fill_exchange_time_us"].to_numpy(dtype="int64")
    entry_local_fill = entry_fills["full_fill_local_time_us"].to_numpy(dtype="int64")
    entry_segment = entry_fills["feature_segment_id"].to_numpy(dtype="float64")
    target_index = target_exit_indices(
        entry_local_fill,
        day_start_us=day_start_us,
        minimum_delay_ms=minimum_delay_ms,
        additional_hold_ms=additional_hold_ms,
    )
    valid = (target_index >= 0) & (target_index < len(context["sample_time_us"]))
    exit_column = np.where(entry_side_is_bid, 1, 0)
    exit_price = np.full(count, np.nan, dtype="float64")
    exit_placement_local = np.full(count, np.nan, dtype="float64")
    exit_exchange_fill = np.full(count, np.nan, dtype="float64")
    exit_local_fill = np.full(count, np.nan, dtype="float64")
    exit_segment = np.full(count, np.nan, dtype="float64")
    safe = np.flatnonzero(valid)
    for name, destination in (
        ("quote_price", exit_price),
        ("placement_local_time_us", exit_placement_local),
        ("full_fill_exchange_time_us", exit_exchange_fill),
        ("full_fill_local_time_us", exit_local_fill),
        ("feature_segment_id", exit_segment),
    ):
        destination[safe] = exit_lookup[name][target_index[safe], exit_column[safe]]
    valid &= np.isfinite(exit_price)
    valid &= np.isfinite(exit_exchange_fill) & np.isfinite(exit_local_fill)
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
        valid[safe] &= valid_book[entry_fill_index[safe]]
        valid[safe] &= valid_book[exit_fill_index[safe]]
        valid[safe] &= np.isfinite(entry_segment[safe]) & np.isfinite(exit_segment[safe])
        valid[safe] &= entry_segment[safe] == exit_segment[safe]
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
    hold_ms: int,
    exit_lifetime_ms: int,
    regime: str,
) -> str:
    return (
        f"{signal}__{direction}__absq{quantile:.4f}__{cancel_mode}__"
        f"entry{entry_lifetime_ms}ms__hold{hold_ms}ms__exit{exit_lifetime_ms}ms__{regime}"
    )


def evaluate_day(date: str, thresholds: dict[str, Any]) -> pd.DataFrame:
    spec = _load_json(SPEC_PATH)
    feature_frame = load_day(date)
    model, transforms = _model_inputs()
    feature_signals, context = derive_arrays(feature_frame, model=model, transforms=transforms)
    day_start = int(context["sample_time_us"][0])
    feature_times = context["sample_time_us"]
    segments = context["feature_segment_id"]
    queue_cutoff = float(thresholds["queue_bottom20_btc"])
    quantiles = [float(value) for value in spec["entry"]["absolute_tail_quantiles"]]
    directions = list(spec["entry"]["directions"])
    cancel_modes = list(spec["entry"]["cancel_modes"])
    hold_delays = [int(value) for value in spec["exit"]["additional_hold_delays_ms"]]
    fee_scenarios = [float(value) for value in spec["fees"]["maker_fee_bps_per_leg_scenarios"]]
    primary_fee = float(spec["fees"]["primary_maker_fee_bps_per_leg"])
    minimum_delay = int(spec["exit"]["minimum_placement_delay_after_entry_fill_ms"])
    cancel_latency_us = int(spec["entry"]["cancel_latency_ms"]) * 1000
    exit_lifetimes = [int(value) for value in spec["exit"]["quote_lifetimes_ms"]]
    exit_lookups = {
        lifetime: _build_exit_lookup(
            _read_full_fills(date, lifetime), day_start_us=day_start, rows=len(feature_frame)
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
        if np.any(decision_index < 0) or np.any(decision_index >= len(feature_frame)):
            raise ValueError("entry decision time outside feature day")
        side_is_bid = fills["side"].eq("bid").to_numpy()
        full_fill_local = fills["full_fill_local_time_us"].to_numpy(dtype="int64")
        queue_low = fills["queue_ahead_initial"].to_numpy(dtype="float64") <= queue_cutoff
        hour = ((decision_time // 3_600_000_000) % 24).astype(np.int8)
        session = np.where(hour < 8, 0, np.where(hour < 16, 1, 2)).astype(np.int8)
        buckets = session * 2 + queue_low.astype(np.int8)

        pair_metrics = {
            (exit_lifetime, hold): _pair_values(
                entry_fills=fills,
                entry_side_is_bid=side_is_bid,
                exit_lookup=exit_lookups[exit_lifetime],
                context=context,
                day_start_us=day_start,
                minimum_delay_ms=minimum_delay,
                additional_hold_ms=hold,
                fee_scenarios=fee_scenarios,
            )
            for exit_lifetime in exit_lifetimes
            for hold in hold_delays
        }

        for signal_name in SIGNALS:
            feature_values = feature_signals[signal_name]
            placement_values = fills[signal_name].to_numpy(dtype="float64")
            if not np.allclose(
                placement_values,
                feature_values[decision_index],
                equal_nan=True,
                rtol=0,
                atol=1e-9,
            ):
                raise ValueError(f"passive/feature signal mismatch: {signal_name}")
            threshold_map = thresholds["signal_absolute_thresholds"][signal_name]
            for quantile in quantiles:
                threshold = float(threshold_map[f"q{quantile:.4f}"])
                for direction in directions:
                    placement = _placement_mask(
                        placement_values,
                        side_is_bid,
                        threshold=threshold,
                        direction=direction,
                    )
                    for cancel_mode in cancel_modes:
                        if cancel_mode == "fixed_lifetime":
                            survives_cancel = np.ones(len(fills), dtype=bool)
                        elif cancel_mode == "cancel_on_tail_exit":
                            bid_safety = _side_safety(
                                feature_values,
                                threshold=threshold,
                                direction=direction,
                                side="bid",
                                mode=cancel_mode,
                            )
                            ask_safety = _side_safety(
                                feature_values,
                                threshold=threshold,
                                direction=direction,
                                side="ask",
                                mode=cancel_mode,
                            )
                            bid_bad = next_unsafe_indices(bid_safety, segments)
                            ask_bad = next_unsafe_indices(ask_safety, segments)
                            next_bad = np.where(
                                side_is_bid,
                                bid_bad[decision_index],
                                ask_bad[decision_index],
                            )
                            cancel_effective = np.full(len(fills), np.iinfo("int64").max)
                            has_cancel = next_bad < len(feature_times)
                            cancel_effective[has_cancel] = (
                                feature_times[next_bad[has_cancel]] + cancel_latency_us
                            )
                            survives_cancel = full_fill_local <= cancel_effective
                        else:
                            raise ValueError(f"unknown cancel mode: {cancel_mode}")
                        base_selected = placement & survives_cancel
                        for (exit_lifetime, hold), metrics in pair_metrics.items():
                            selected = base_selected & metrics["valid"]
                            values = {
                                "gross": metrics["gross"],
                                **{
                                    f"net_maker_{fee:g}bps": metrics[f"net_maker_{fee:g}bps"]
                                    for fee in fee_scenarios
                                },
                                "primary": metrics[f"net_maker_{primary_fee:g}bps"],
                            }
                            totals = _bucket_totals(buckets, selected, values)
                            for regime, (count, sums) in totals.items():
                                rows.append({
                                    "date": date,
                                    "policy": _policy_name(
                                        signal_name,
                                        direction,
                                        quantile,
                                        cancel_mode,
                                        entry_lifetime,
                                        hold,
                                        exit_lifetime,
                                        regime,
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


def aggregate_development(day: pd.DataFrame) -> pd.DataFrame:
    dimensions = [
        "policy", "signal", "direction", "tail_quantile", "absolute_signal_threshold",
        "cancel_mode", "entry_quote_lifetime_ms", "additional_hold_ms",
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
        kind="stable",
        inplace=True,
        ignore_index=True,
    )
    result.insert(0, "rank", np.arange(1, len(result) + 1))
    return result


def run_development() -> dict[str, Any]:
    thresholds = build_threshold_audit()
    day = pd.concat(
        [evaluate_day(date, thresholds) for date in DEVELOPMENT_DATES],
        ignore_index=True,
    )
    spec = _load_json(SPEC_PATH)
    expected = (
        len(spec["entry"]["signals"])
        * len(spec["entry"]["absolute_tail_quantiles"])
        * len(spec["entry"]["directions"])
        * len(spec["entry"]["cancel_modes"])
        * len(spec["entry"]["quote_lifetimes_ms"])
        * len(spec["exit"]["additional_hold_delays_ms"])
        * len(spec["exit"]["quote_lifetimes_ms"])
        * len(spec["placement_regimes"])
        * len(DEVELOPMENT_DATES)
    )
    if len(day) != expected:
        raise ValueError(f"unexpected maker-roundtrip rows: {len(day)} != {expected}")
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
        "schema": "obi-maker-roundtrip-shortlist-v1",
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
    if eligible.empty:
        best = ranking.iloc[0]
    else:
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
