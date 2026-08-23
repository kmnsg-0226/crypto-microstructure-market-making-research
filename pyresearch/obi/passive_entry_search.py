"""Loop-3 OBI-filtered passive-entry and taker-exit upper-bound screen."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.obi.search import (
    FEATURE_ROOT,
    REPORT_ROOT,
    _model_inputs,
    derive_arrays,
    load_day,
)
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
SPEC_PATH = ROOT / "research/specs/obi_passive_entry_search_spec.json"
PASSIVE_ROOT = FEATURE_ROOT / "passive"
PASSIVE_THRESHOLDS_PATH = (
    REPORT_ROOT.parent / "passive/approach_exploration/thresholds.json"
)
OBI_THRESHOLDS_PATH = REPORT_ROOT / "stage1_thresholds.json"
OUTPUT_ROOT = REPORT_ROOT / "loop3_passive_entry"
THRESHOLDS_PATH = OUTPUT_ROOT / "threshold_audit.json"
DAY_METRICS_PATH = OUTPUT_ROOT / "development_day_metrics.csv"
AGGREGATE_PATH = OUTPUT_ROOT / "development_metrics.csv"
RANKING_PATH = OUTPUT_ROOT / "development_ranking.csv"
SHORTLIST_PATH = OUTPUT_ROOT / "shortlist_before_replication.json"
DEVELOPMENT_DATES = [f"2026-0{month}-01" for month in range(1, 6)]
SIGNALS = (
    "obi_l1",
    "obi_l5",
    "weighted_obi_l10",
    "weighted_mid_minus_mid_ticks",
    "combined_prediction_1s_ticks",
    "ti_1s",
)
REGIME_BUCKETS = {
    "all": (0, 1, 2, 3, 4, 5),
    "queue_bottom20": (1, 3, 5),
    "utc_00_08": (0, 1),
    "utc_08_16": (2, 3),
    "utc_16_24": (4, 5),
    "queue_bottom20_utc_00_08": (1,),
    "queue_bottom20_utc_08_16": (3,),
    "queue_bottom20_utc_16_24": (5,),
}
PASSIVE_COLUMNS = [
    "date",
    "decision_time_us",
    "side",
    "quote_price",
    "quote_lifetime_ms",
    "queue_ahead_initial",
    *SIGNALS,
    "fill_status",
    "full_fill_exchange_time_us",
]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_threshold_audit() -> dict[str, Any]:
    spec = _load_json(SPEC_PATH)
    if spec["status"] != "declared_after_loop2_failure_before_passive_entry_outcomes":
        raise ValueError("loop-3 disclosure status changed")
    passive_hash = sha256(PASSIVE_THRESHOLDS_PATH)
    obi_hash = sha256(OBI_THRESHOLDS_PATH)
    if passive_hash != spec["audit"]["passive_approach_thresholds_sha256"]:
        raise ValueError("passive threshold artifact changed before loop 3")
    if obi_hash != spec["audit"]["obi_stage1_thresholds_sha256"]:
        raise ValueError("OBI threshold artifact changed before loop 3")
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
        "schema": "obi-passive-entry-threshold-audit-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "passive_thresholds_sha256": passive_hash,
        "obi_thresholds_sha256": obi_hash,
        "outcomes_read": False,
        "queue_bottom20_btc": float(
            passive["thresholds"]["queue_ahead_initial"]["q20"]
        ),
        "signal_absolute_thresholds": signal_thresholds,
    }
    write_json(THRESHOLDS_PATH, payload)
    return payload


def next_unsafe_indices(safety: np.ndarray, segments: np.ndarray) -> np.ndarray:
    """First unsafe index strictly after every row, with boundaries unsafe."""
    safety = np.asarray(safety, dtype=bool)
    segments = np.asarray(segments, dtype="float64")
    if len(safety) != len(segments):
        raise ValueError("safety/segment length mismatch")
    finite = np.isfinite(segments)
    boundary = np.zeros(len(safety), dtype=bool)
    if len(safety) > 1:
        boundary[1:] = (
            ~finite[1:]
            | ~finite[:-1]
            | (segments[1:] != segments[:-1])
        )
    bad = np.flatnonzero(~safety | ~finite | boundary)
    rows = np.arange(len(safety), dtype="int64")
    positions = np.searchsorted(bad, rows + 1, side="left")
    result = np.full(len(safety), len(safety), dtype="int64")
    available = positions < len(bad)
    result[available] = bad[positions[available]]
    return result


def passive_exit_returns(
    *,
    side_is_bid: np.ndarray,
    quote_price: np.ndarray,
    exit_bid: np.ndarray,
    exit_ask: np.ndarray,
    maker_fee_bps: float,
    taker_fee_bps: float,
) -> tuple[np.ndarray, np.ndarray]:
    maker_rate = maker_fee_bps / 10_000.0
    taker_rate = taker_fee_bps / 10_000.0
    exit_price = np.where(side_is_bid, exit_bid, exit_ask)
    gross_cash = np.where(side_is_bid, exit_price - quote_price, quote_price - exit_price)
    gross_bps = gross_cash / quote_price * 10_000.0
    net_cash = gross_cash - maker_rate * quote_price - taker_rate * exit_price
    net_bps = net_cash / quote_price * 10_000.0
    return gross_bps, net_bps


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
    return frame


def _side_safety(
    values: np.ndarray,
    *,
    threshold: float,
    direction: str,
    side: str,
    mode: str,
) -> np.ndarray:
    finite = np.isfinite(values)
    if direction == "maker_contrarian":
        orientation = -1.0 if side == "bid" else 1.0
    elif direction == "maker_trend":
        orientation = 1.0 if side == "bid" else -1.0
    else:
        raise ValueError(f"unknown maker direction: {direction}")
    oriented = orientation * values
    if mode == "cancel_on_sign_exit":
        return finite & (oriented > 0)
    if mode == "cancel_on_tail_exit":
        return finite & (oriented >= threshold)
    if mode == "cancel_on_opposite_tail":
        return finite & (oriented > -threshold)
    if mode == "fixed_lifetime":
        return np.ones(len(values), dtype=bool)
    raise ValueError(f"unknown passive cancel mode: {mode}")


def _placement_mask(
    values: np.ndarray,
    side_is_bid: np.ndarray,
    *,
    threshold: float,
    direction: str,
) -> np.ndarray:
    if direction == "maker_contrarian":
        oriented = np.where(side_is_bid, -values, values)
    elif direction == "maker_trend":
        oriented = np.where(side_is_bid, values, -values)
    else:
        raise ValueError(f"unknown maker direction: {direction}")
    return np.isfinite(oriented) & (oriented >= threshold)


def _bucket_totals(
    buckets: np.ndarray,
    selected: np.ndarray,
    values: dict[str, np.ndarray],
) -> dict[str, tuple[int, dict[str, float]]]:
    chosen = selected & np.isfinite(values["primary"])
    counts = np.bincount(buckets[chosen], minlength=6)
    sums = {
        name: np.bincount(buckets[chosen], weights=array[chosen], minlength=6)
        for name, array in values.items()
    }
    wins = np.bincount(
        buckets[chosen], weights=(values["primary"][chosen] > 0), minlength=6
    )
    result = {}
    for regime, indices in REGIME_BUCKETS.items():
        index = np.asarray(indices, dtype="int64")
        result[regime] = (
            int(counts[index].sum()),
            {
                **{name: float(total[index].sum()) for name, total in sums.items()},
                "wins": float(wins[index].sum()),
            },
        )
    return result


def _policy_name(
    signal: str,
    direction: str,
    tail_quantile: float,
    cancel_mode: str,
    lifetime_ms: int,
    horizon_ms: int,
    regime: str,
) -> str:
    return (
        f"{signal}__{direction}__absq{tail_quantile:.4f}__{cancel_mode}__"
        f"life{lifetime_ms}ms__hold{horizon_ms}ms__{regime}"
    )


def evaluate_day(date: str, thresholds: dict[str, Any]) -> pd.DataFrame:
    spec = _load_json(SPEC_PATH)
    feature_frame = load_day(date)
    model, transforms = _model_inputs()
    feature_signals, context = derive_arrays(
        feature_frame, model=model, transforms=transforms
    )
    day_start = int(context["sample_time_us"][0])
    segments = context["feature_segment_id"]
    feature_times = context["sample_time_us"]
    queue_cutoff = float(thresholds["queue_bottom20_btc"])
    quantiles = [float(value) for value in spec["entry"]["absolute_tail_quantiles"]]
    directions = list(spec["entry"]["directions"])
    cancel_modes = list(spec["dynamic_cancel"]["modes"])
    horizons = [int(value) for value in spec["exit"]["horizons_ms"]]
    maker_fees = [float(value) for value in spec["entry"]["maker_fee_bps_scenarios"]]
    primary_maker_fee = float(spec["entry"]["primary_maker_fee_bps"])
    taker_fee = float(spec["exit"]["taker_fee_bps"])
    cancel_latency_us = int(spec["dynamic_cancel"]["cancel_latency_ms"]) * 1000
    exit_latency_steps = int(spec["exit"]["latency_after_target_ms"]) // 100
    rows = []

    for lifetime in [int(value) for value in spec["entry"]["quote_lifetimes_ms"]]:
        fills = _read_full_fills(date, lifetime)
        decision_index = (
            (fills["decision_time_us"].to_numpy(dtype="int64") - day_start) // 100_000
        ).astype("int64")
        if np.any(decision_index < 0) or np.any(decision_index >= len(feature_frame)):
            raise ValueError("passive decision time falls outside feature day")
        side_is_bid = fills["side"].eq("bid").to_numpy()
        quote_price = fills["quote_price"].to_numpy(dtype="float64")
        fill_time = fills["full_fill_exchange_time_us"].to_numpy(dtype="int64")
        fill_grid_time = ((fill_time + 99_999) // 100_000) * 100_000
        fill_grid_index = (fill_grid_time - day_start) // 100_000
        queue_low = (
            fills["queue_ahead_initial"].to_numpy(dtype="float64") <= queue_cutoff
        )
        hour = (
            (fills["decision_time_us"].to_numpy(dtype="int64") // 3_600_000_000) % 24
        ).astype(np.int8)
        session = np.where(hour < 8, 0, np.where(hour < 16, 1, 2)).astype(np.int8)
        buckets = session * 2 + queue_low.astype(np.int8)

        horizon_values = {}
        for horizon in horizons:
            exit_index = fill_grid_index + horizon // 100 + exit_latency_steps
            valid = (
                (fill_grid_index >= 0)
                & (fill_grid_index < len(feature_frame))
                & (exit_index >= 0)
                & (exit_index < len(feature_frame))
            )
            safe = np.flatnonzero(valid)
            if len(safe):
                valid[safe] &= context["valid_book_state"][fill_grid_index[safe]]
                valid[safe] &= context["valid_book_state"][exit_index[safe]]
                valid[safe] &= np.isfinite(segments[fill_grid_index[safe]])
                valid[safe] &= (
                    segments[fill_grid_index[safe]] == segments[exit_index[safe]]
                )
            exit_bid = np.full(len(fills), np.nan)
            exit_ask = np.full(len(fills), np.nan)
            safe = np.flatnonzero(valid)
            exit_bid[safe] = context["best_bid_price"][exit_index[safe]]
            exit_ask[safe] = context["best_ask_price"][exit_index[safe]]
            gross, _ = passive_exit_returns(
                side_is_bid=side_is_bid,
                quote_price=quote_price,
                exit_bid=exit_bid,
                exit_ask=exit_ask,
                maker_fee_bps=0.0,
                taker_fee_bps=0.0,
            )
            fees = {}
            for maker_fee in maker_fees:
                _, net = passive_exit_returns(
                    side_is_bid=side_is_bid,
                    quote_price=quote_price,
                    exit_bid=exit_bid,
                    exit_ask=exit_ask,
                    maker_fee_bps=maker_fee,
                    taker_fee_bps=taker_fee,
                )
                fees[maker_fee] = net
            horizon_values[horizon] = (gross, fees)

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
                        else:
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
                            survives_cancel = fill_time <= cancel_effective
                        selected = placement & survives_cancel
                        for horizon, (gross, fee_values) in horizon_values.items():
                            metric_values = {
                                "gross": gross,
                                **{
                                    f"net_maker_{maker_fee:g}bps": values
                                    for maker_fee, values in fee_values.items()
                                },
                                "primary": fee_values[primary_maker_fee],
                            }
                            totals = _bucket_totals(
                                buckets, selected, metric_values
                            )
                            for regime, (count, sums) in totals.items():
                                rows.append({
                                    "date": date,
                                    "policy": _policy_name(
                                        signal_name,
                                        direction,
                                        quantile,
                                        cancel_mode,
                                        lifetime,
                                        horizon,
                                        regime,
                                    ),
                                    "signal": signal_name,
                                    "direction": direction,
                                    "tail_quantile": quantile,
                                    "absolute_signal_threshold": threshold,
                                    "cancel_mode": cancel_mode,
                                    "quote_lifetime_ms": lifetime,
                                    "hold_horizon_ms": horizon,
                                    "regime": regime,
                                    "completed_fills": count,
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
        "policy", "signal", "direction", "tail_quantile",
        "absolute_signal_threshold", "cancel_mode", "quote_lifetime_ms",
        "hold_horizon_ms", "regime",
    ]
    mean_columns = [
        "gross_mean_bps", "net_maker_0bps_mean_bps",
        "net_maker_1bps_mean_bps", "net_maker_2bps_mean_bps",
    ]
    rows = []
    for keys, group in day.groupby(dimensions, sort=True, observed=True, dropna=False):
        data = dict(zip(dimensions, keys))
        fills = int(group["completed_fills"].sum())
        row = {
            **data,
            "days": int(group["date"].nunique()),
            "completed_fills": fills,
            "minimum_completed_fills_day": int(group["completed_fills"].min()),
            "positive_development_days": int((group["primary_net_mean_bps"] > 0).sum()),
            "worst_development_day_net_bps": float(group["primary_net_mean_bps"].min()),
            "best_development_day_net_bps": float(group["primary_net_mean_bps"].max()),
            "primary_net_mean_bps": float(group["primary_net_total_bps"].sum() / fills)
            if fills else np.nan,
            "primary_net_positive_probability": float(
                (
                    group["primary_net_positive_probability"]
                    * group["completed_fills"]
                ).sum() / fills
            ) if fills else np.nan,
        }
        for column in mean_columns:
            row[column] = float(
                (group[column] * group["completed_fills"]).sum() / fills
            ) if fills else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def rank_development(aggregate: pd.DataFrame) -> pd.DataFrame:
    gate = _load_json(SPEC_PATH)["development_gate"]
    result = aggregate.copy()
    result["eligible_activity"] = (
        (result["completed_fills"] >= int(gate["minimum_completed_fills_total"]))
        & (
            result["minimum_completed_fills_day"]
            >= int(gate["minimum_completed_fills_each_day"])
        )
    )
    behavior = [
        "signal", "direction", "absolute_signal_threshold", "cancel_mode",
        "quote_lifetime_ms", "hold_horizon_ms", "regime",
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
            "advances_to_exact_execution", "eligible_activity",
            "positive_development_days", "worst_development_day_net_bps",
            "primary_net_mean_bps", "completed_fills", "policy",
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
        * len(spec["dynamic_cancel"]["modes"])
        * len(spec["entry"]["quote_lifetimes_ms"])
        * len(spec["exit"]["horizons_ms"])
        * len(spec["placement_regimes"])
        * len(DEVELOPMENT_DATES)
    )
    if len(day) != expected:
        raise ValueError(f"unexpected passive-entry rows: {len(day)} != {expected}")
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
        "schema": "obi-passive-entry-shortlist-v1",
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
        "activity_eligible_cells": int(ranking["eligible_activity"].sum()),
        "positive_primary_net_cells": int((eligible["primary_net_mean_bps"] > 0).sum()),
        "positive_upper_bound_survivors": int(len(survivors)),
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
