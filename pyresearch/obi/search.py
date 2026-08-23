"""Stage-1 optimistic BBO screen for OBI/microstructure taker strategies.

The screen intentionally omits depth slippage and market impact.  A strategy
that cannot cover actual BBO spread, 100 ms latency, and the frozen fee under
this upper bound is not advanced to the exact top-10 execution engine.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.execution.engine import frozen_prediction
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
SPEC_PATH = ROOT / "research/specs/obi_profitability_search_spec.json"
FEATURE_ROOT = ROOT / "data/research/tardis"
REPORT_ROOT = FEATURE_ROOT / "reports/obi_profitability"
THRESHOLDS_PATH = REPORT_ROOT / "stage1_thresholds.json"
DAY_METRICS_PATH = REPORT_ROOT / "stage1_development_day_metrics.csv"
AGGREGATE_PATH = REPORT_ROOT / "stage1_development_metrics.csv"
RANKING_PATH = REPORT_ROOT / "stage1_development_ranking.csv"
SHORTLIST_PATH = REPORT_ROOT / "stage1_shortlist_before_replication.json"
MODELS_PATH = FEATURE_ROOT / "reports/development/fitted_models.json"
TRANSFORMS_PATH = FEATURE_ROOT / "reports/development/development_transforms.json"

DEVELOPMENT_DATES = [f"2026-0{month}-01" for month in range(1, 6)]
RETROSPECTIVE_DATES = ["2026-06-01", "2026-07-01", "2026-08-01"]
TICK_SIZE = 0.1
RAW_SIGNALS = (
    "obi_l1",
    "obi_l5",
    "obi_l10",
    "weighted_obi_l10",
    "weighted_mid_minus_mid_ticks",
    "normalized_ofi_100ms",
    "normalized_ofi_1s",
    "normalized_ofi_5s",
    "ti_100ms",
    "ti_1s",
    "ti_5s",
)
DERIVED_SIGNALS = (
    "combined_prediction_1s_ticks",
    "obi_depth_mean",
    "micro_consensus_z",
)
SIGNALS = RAW_SIGNALS + DERIVED_SIGNALS
BASE_COLUMNS = [
    "date",
    "sample_time_us",
    "valid_book_state",
    "feature_segment_id",
    "best_bid_price",
    "best_ask_price",
    "spread_ticks",
    "mid",
    "trade_volume_1s",
    "book_level_update_count_1s",
    *RAW_SIGNALS,
]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _audit_spec(spec: dict[str, Any]) -> None:
    if spec["status"] != "declared_after_all_eight_historical_dates_were_seen_before_new_search":
        raise ValueError("OBI search disclosure status changed")
    if not spec["interpretation"]["historical_search_is_exploratory"]:
        raise ValueError("historical OBI search may not be labeled confirmatory")
    if spec["interpretation"]["historical_positive_result_cannot_complete_profitability_goal"] is not True:
        raise ValueError("historical results may not complete the profitability goal")


def _model_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    models = _load_json(MODELS_PATH)
    transforms = _load_json(TRANSFORMS_PATH)
    return models["models"]["combined:1000"], transforms


def _required_columns(model: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(BASE_COLUMNS + list(model["features"])))


def load_day(date: str) -> pd.DataFrame:
    model, _ = _model_inputs()
    path = FEATURE_ROOT / date / "features_100ms.parquet"
    frame = pd.read_parquet(path, columns=_required_columns(model))
    if len(frame) != 864_000 or frame["date"].nunique() != 1 or frame["date"].iat[0] != date:
        raise ValueError(f"unexpected OBI search day: {date}")
    if not frame["sample_time_us"].diff().dropna().eq(100_000).all():
        raise ValueError(f"non-100ms OBI search grid: {date}")
    return frame


def derive_arrays(
    frame: pd.DataFrame,
    *,
    model: dict[str, Any],
    transforms: dict[str, Any],
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    signals = {
        name: frame[name].to_numpy(dtype="float64")
        for name in RAW_SIGNALS
    }
    signals["combined_prediction_1s_ticks"] = frozen_prediction(frame, model, transforms)
    depth_values = np.column_stack(
        [signals["obi_l1"], signals["obi_l5"], signals["obi_l10"]]
    )
    depth_finite = np.isfinite(depth_values)
    depth_count = depth_finite.sum(axis=1)
    depth_sum = np.where(depth_finite, depth_values, 0.0).sum(axis=1)
    depth_mean = np.full(len(frame), np.nan, dtype="float64")
    np.divide(depth_sum, depth_count, out=depth_mean, where=depth_count > 0)
    signals["obi_depth_mean"] = depth_mean

    consensus = np.zeros(len(frame), dtype="float64")
    consensus_valid = np.ones(len(frame), dtype=bool)
    for name in ("obi_l10", "normalized_ofi_1s", "ti_1s"):
        values = signals[name]
        scale = transforms["standardization"][name]
        consensus += (
            values - float(scale["mean"])
        ) / float(scale["population_std"])
        consensus_valid &= np.isfinite(values)
    consensus /= 3.0
    consensus[~consensus_valid] = np.nan
    signals["micro_consensus_z"] = consensus

    mid = frame["mid"].to_numpy(dtype="float64")
    backward_volatility = np.full(len(frame), np.nan, dtype="float64")
    backward_volatility[50:] = np.abs(mid[50:] - mid[:-50]) / TICK_SIZE
    context = {
        "sample_time_us": frame["sample_time_us"].to_numpy(dtype="int64"),
        "valid_book_state": frame["valid_book_state"].eq(1).to_numpy(),
        "feature_segment_id": frame["feature_segment_id"].to_numpy(
            dtype="float64", na_value=np.nan
        ),
        "best_bid_price": frame["best_bid_price"].to_numpy(dtype="float64"),
        "best_ask_price": frame["best_ask_price"].to_numpy(dtype="float64"),
        "spread_ticks": frame["spread_ticks"].to_numpy(dtype="float64"),
        "trade_volume_1s": frame["trade_volume_1s"].to_numpy(dtype="float64"),
        "book_level_update_count_1s": frame[
            "book_level_update_count_1s"
        ].to_numpy(dtype="float64"),
        "backward_volatility_5s_ticks": backward_volatility,
    }
    return signals, context


def build_thresholds() -> dict[str, Any]:
    spec = _load_json(SPEC_PATH)
    _audit_spec(spec)
    model, transforms = _model_inputs()
    step = int(spec["data"]["threshold_distribution_sample_every_n_rows"])
    quantiles = np.asarray(spec["absolute_signal_tail_quantiles"], dtype="float64")
    sampled: dict[str, list[np.ndarray]] = {name: [] for name in SIGNALS}
    regime_samples: dict[str, list[np.ndarray]] = {
        "trade_volume_1s": [],
        "book_level_update_count_1s": [],
        "backward_volatility_5s_ticks": [],
    }
    feature_hashes: dict[str, str] = {}
    for date in DEVELOPMENT_DATES:
        frame = load_day(date)
        signals, context = derive_arrays(frame, model=model, transforms=transforms)
        for name, values in signals.items():
            values = np.abs(values[::step])
            sampled[name].append(values[np.isfinite(values)])
        for name in regime_samples:
            values = context[name][::step]
            regime_samples[name].append(values[np.isfinite(values)])
        feature_hashes[date] = sha256(FEATURE_ROOT / date / "features_100ms.parquet")

    signal_thresholds: dict[str, dict[str, float]] = {}
    finite_counts: dict[str, int] = {}
    for name, chunks in sampled.items():
        values = np.concatenate(chunks)
        fitted = np.quantile(values, quantiles, method="linear")
        signal_thresholds[name] = {
            f"q{quantile:.4f}": float(value)
            for quantile, value in zip(quantiles, fitted)
        }
        finite_counts[name] = int(len(values))

    regime_thresholds = {}
    for name, chunks in regime_samples.items():
        values = np.concatenate(chunks)
        fitted = np.quantile(values, [0.2, 0.8], method="linear")
        regime_thresholds[name] = {
            "q20": float(fitted[0]),
            "q80": float(fitted[1]),
            "finite_rows": int(len(values)),
        }

    payload = {
        "schema": "obi-profitability-stage1-thresholds-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "model_sha256": sha256(MODELS_PATH),
        "transforms_sha256": sha256(TRANSFORMS_PATH),
        "fit_dates": DEVELOPMENT_DATES,
        "sample_every_n_rows": step,
        "outcomes_read": False,
        "feature_sha256": feature_hashes,
        "signal_finite_sample_rows": finite_counts,
        "signal_absolute_thresholds": signal_thresholds,
        "regime_thresholds": regime_thresholds,
    }
    write_json(THRESHOLDS_PATH, payload)
    return payload


def bbo_returns(
    context: dict[str, np.ndarray],
    *,
    horizon_ms: int,
    latency_ms: int,
    fee_bps_per_side: float,
) -> dict[str, np.ndarray]:
    if horizon_ms % 100 or latency_ms % 100 or latency_ms >= horizon_ms:
        raise ValueError("BBO screen horizon/latency must align and latency must precede exit")
    length = len(context["sample_time_us"])
    horizon_steps = horizon_ms // 100
    latency_steps = latency_ms // 100
    stop = length - horizon_steps
    decision = np.arange(stop)
    entry = decision + latency_steps
    exit_ = decision + horizon_steps
    bid = context["best_bid_price"]
    ask = context["best_ask_price"]
    valid_book = context["valid_book_state"]
    segment = context["feature_segment_id"]
    valid = (
        valid_book[decision]
        & valid_book[entry]
        & valid_book[exit_]
        & np.isfinite(segment[decision])
        & (segment[decision] == segment[entry])
        & (segment[decision] == segment[exit_])
        & np.isfinite(bid[entry])
        & np.isfinite(ask[entry])
        & np.isfinite(bid[exit_])
        & np.isfinite(ask[exit_])
    )
    fee_rate = fee_bps_per_side / 10_000.0
    long_entry = ask[entry]
    long_exit = bid[exit_]
    short_entry = bid[entry]
    short_exit = ask[exit_]
    long_gross = (long_exit - long_entry) / long_entry * 10_000.0
    short_gross = (short_entry - short_exit) / short_entry * 10_000.0
    long_net = (
        long_exit - long_entry - fee_rate * (long_entry + long_exit)
    ) / long_entry * 10_000.0
    short_net = (
        short_entry - short_exit - fee_rate * (short_entry + short_exit)
    ) / short_entry * 10_000.0

    result = {}
    for name, values in (
        ("long_gross", long_gross),
        ("short_gross", short_gross),
        ("long_net", long_net),
        ("short_net", short_net),
    ):
        output = np.full(length, np.nan, dtype="float64")
        output[decision[valid]] = values[valid]
        result[name] = output
    result["valid"] = np.isfinite(result["long_net"]) & np.isfinite(result["short_net"])
    return result


def regime_masks(
    signal: np.ndarray,
    signals: dict[str, np.ndarray],
    context: dict[str, np.ndarray],
    thresholds: dict[str, Any],
) -> dict[str, np.ndarray]:
    length = len(signal)
    sign = np.sign(signal)
    ti = signals["ti_1s"]
    ofi = signals["normalized_ofi_1s"]
    regime = thresholds["regime_thresholds"]
    volume = context["trade_volume_1s"]
    updates = context["book_level_update_count_1s"]
    volatility = context["backward_volatility_5s_ticks"]
    hour = ((context["sample_time_us"] // 3_600_000_000) % 24).astype(np.int8)
    return {
        "all": np.ones(length, dtype=bool),
        "ti_1s_aligned": np.isfinite(ti) & (sign * ti > 0),
        "ofi_1s_aligned": np.isfinite(ofi) & (sign * ofi > 0),
        "ti_and_ofi_aligned": (
            np.isfinite(ti) & np.isfinite(ofi) & (sign * ti > 0) & (sign * ofi > 0)
        ),
        "one_tick_spread": np.isfinite(context["spread_ticks"])
        & (context["spread_ticks"] <= 1.0 + 1e-9),
        "trade_volume_1s_high": np.isfinite(volume)
        & (volume >= regime["trade_volume_1s"]["q80"]),
        "trade_volume_1s_low": np.isfinite(volume)
        & (volume <= regime["trade_volume_1s"]["q20"]),
        "book_updates_1s_high": np.isfinite(updates)
        & (updates >= regime["book_level_update_count_1s"]["q80"]),
        "book_updates_1s_low": np.isfinite(updates)
        & (updates <= regime["book_level_update_count_1s"]["q20"]),
        "backward_volatility_5s_high": np.isfinite(volatility)
        & (volatility >= regime["backward_volatility_5s_ticks"]["q80"]),
        "backward_volatility_5s_low": np.isfinite(volatility)
        & (volatility <= regime["backward_volatility_5s_ticks"]["q20"]),
        "utc_00_08": hour < 8,
        "utc_08_16": (hour >= 8) & (hour < 16),
        "utc_16_24": hour >= 16,
    }


def _policy_name(
    signal: str,
    direction: str,
    regime: str,
    tail_quantile: float,
    horizon_ms: int,
) -> str:
    return (
        f"{signal}__{direction}__{regime}__"
        f"absq{tail_quantile:.4f}__h{horizon_ms}ms"
    )


def _prefix_metrics(
    *,
    date: str,
    signal_name: str,
    direction: str,
    regime: str,
    horizon_ms: int,
    threshold_items: list[tuple[float, float]],
    ordered_abs: np.ndarray,
    gross_bps: np.ndarray,
    net_bps: np.ndarray,
) -> list[dict[str, Any]]:
    if len(ordered_abs):
        gross_sum = np.cumsum(gross_bps, dtype="float64")
        net_sum = np.cumsum(net_bps, dtype="float64")
        wins = np.cumsum(net_bps > 0, dtype="int64")
    rows = []
    for quantile, threshold in threshold_items:
        count = int(np.searchsorted(-ordered_abs, -threshold, side="right"))
        rows.append({
            "date": date,
            "policy": _policy_name(signal_name, direction, regime, quantile, horizon_ms),
            "signal": signal_name,
            "direction": direction,
            "regime": regime,
            "tail_quantile": quantile,
            "absolute_signal_threshold": threshold,
            "horizon_ms": horizon_ms,
            "observations": count,
            "gross_bbo_mean_bps": float(gross_sum[count - 1] / count) if count else np.nan,
            "net_upper_bound_mean_bps": float(net_sum[count - 1] / count) if count else np.nan,
            "net_upper_bound_total_bps": float(net_sum[count - 1]) if count else 0.0,
            "net_positive_probability": float(wins[count - 1] / count) if count else np.nan,
        })
    return rows


def evaluate_day(
    date: str,
    threshold_payload: dict[str, Any],
) -> pd.DataFrame:
    spec = _load_json(SPEC_PATH)
    model, transforms = _model_inputs()
    frame = load_day(date)
    signals, context = derive_arrays(frame, model=model, transforms=transforms)
    screen = spec["stage_1_optimistic_taker_upper_bound"]
    horizons = [int(value) for value in spec["fixed_horizons_ms"]]
    quantiles = [float(value) for value in spec["absolute_signal_tail_quantiles"]]
    regimes_expected = list(spec["regimes"])
    rows: list[dict[str, Any]] = []
    returns = {
        horizon: bbo_returns(
            context,
            horizon_ms=horizon,
            latency_ms=int(screen["latency_ms"]),
            fee_bps_per_side=float(screen["fee_bps_per_side"]),
        )
        for horizon in horizons
    }
    for signal_name in SIGNALS:
        values = signals[signal_name]
        absolute = np.abs(values)
        finite = np.isfinite(values) & (values != 0)
        order = np.argsort(-np.where(finite, absolute, -np.inf), kind="stable")
        order = order[finite[order]]
        masks = regime_masks(values, signals, context, threshold_payload)
        if list(masks) != regimes_expected:
            raise ValueError("OBI search regime implementation differs from frozen spec")
        threshold_map = threshold_payload["signal_absolute_thresholds"][signal_name]
        threshold_items = [
            (quantile, float(threshold_map[f"q{quantile:.4f}"]))
            for quantile in quantiles
        ]
        sign = np.sign(values)
        for horizon in horizons:
            outcome = returns[horizon]
            trend_gross = np.where(
                sign > 0, outcome["long_gross"], outcome["short_gross"]
            )
            trend_net = np.where(sign > 0, outcome["long_net"], outcome["short_net"])
            contrarian_gross = np.where(
                sign > 0, outcome["short_gross"], outcome["long_gross"]
            )
            contrarian_net = np.where(
                sign > 0, outcome["short_net"], outcome["long_net"]
            )
            for regime, regime_mask in masks.items():
                eligible_order = order[regime_mask[order] & outcome["valid"][order]]
                ordered_abs = absolute[eligible_order]
                for direction, gross, net in (
                    ("trend", trend_gross, trend_net),
                    ("contrarian", contrarian_gross, contrarian_net),
                ):
                    rows.extend(_prefix_metrics(
                        date=date,
                        signal_name=signal_name,
                        direction=direction,
                        regime=regime,
                        horizon_ms=horizon,
                        threshold_items=threshold_items,
                        ordered_abs=ordered_abs,
                        gross_bps=gross[eligible_order],
                        net_bps=net[eligible_order],
                    ))
    return pd.DataFrame(rows)


def aggregate_development(day: pd.DataFrame) -> pd.DataFrame:
    dimensions = [
        "policy",
        "signal",
        "direction",
        "regime",
        "tail_quantile",
        "absolute_signal_threshold",
        "horizon_ms",
    ]
    rows = []
    for keys, group in day.groupby(dimensions, sort=True, observed=True, dropna=False):
        data = dict(zip(dimensions, keys))
        observations = int(group["observations"].sum())
        rows.append({
            **data,
            "days": int(group["date"].nunique()),
            "observations": observations,
            "minimum_observations_day": int(group["observations"].min()),
            "positive_development_days": int(
                (group["net_upper_bound_mean_bps"] > 0).sum()
            ),
            "worst_development_day_net_bps": float(
                group["net_upper_bound_mean_bps"].min()
            ),
            "best_development_day_net_bps": float(
                group["net_upper_bound_mean_bps"].max()
            ),
            "pooled_gross_bbo_mean_bps": float(
                (group["gross_bbo_mean_bps"] * group["observations"]).sum()
                / observations
            ) if observations else np.nan,
            "pooled_net_upper_bound_mean_bps": float(
                group["net_upper_bound_total_bps"].sum() / observations
            ) if observations else np.nan,
            "pooled_net_positive_probability": float(
                (
                    group["net_positive_probability"] * group["observations"]
                ).sum() / observations
            ) if observations else np.nan,
        })
    return pd.DataFrame(rows)


def rank_development(aggregate: pd.DataFrame) -> pd.DataFrame:
    spec = _load_json(SPEC_PATH)
    rule = spec["development_ranking"]
    result = aggregate.copy()
    result["eligible_activity"] = (
        (result["observations"] >= int(rule["minimum_observations_total"]))
        & (
            result["minimum_observations_day"]
            >= int(rule["minimum_observations_each_day"])
        )
    )
    behavior_columns = [
        "signal",
        "direction",
        "regime",
        "absolute_signal_threshold",
        "horizon_ms",
    ]
    result["duplicate_behavior"] = result.duplicated(behavior_columns, keep="first")
    result["advances_to_exact_execution"] = (
        result["eligible_activity"]
        & ~result["duplicate_behavior"]
        & (result["positive_development_days"] == len(DEVELOPMENT_DATES))
        & (result["worst_development_day_net_bps"] > 0)
        & (result["pooled_net_upper_bound_mean_bps"] > 0)
    )
    result.sort_values(
        [
            "advances_to_exact_execution",
            "eligible_activity",
            "positive_development_days",
            "worst_development_day_net_bps",
            "pooled_net_upper_bound_mean_bps",
            "observations",
            "policy",
        ],
        ascending=[False, False, False, False, False, False, True],
        kind="stable",
        inplace=True,
        ignore_index=True,
    )
    result.insert(0, "rank", np.arange(1, len(result) + 1))
    return result


def run_development() -> dict[str, Any]:
    threshold_payload = build_thresholds()
    rows = [evaluate_day(date, threshold_payload) for date in DEVELOPMENT_DATES]
    day = pd.concat(rows, ignore_index=True)
    expected = len(SIGNALS) * 14 * 8 * 2 * 8 * len(DEVELOPMENT_DATES)
    if len(day) != expected:
        raise ValueError(f"unexpected stage-1 screen row count: {len(day)} != {expected}")
    write_csv(DAY_METRICS_PATH, day)
    aggregate = aggregate_development(day)
    write_csv(AGGREGATE_PATH, aggregate)
    ranking = rank_development(aggregate)
    write_csv(RANKING_PATH, ranking)
    shortlist_size = int(
        _load_json(SPEC_PATH)["development_ranking"][
            "shortlist_size_for_exact_nonoverlap_execution"
        ]
    )
    survivors = ranking.loc[ranking["advances_to_exact_execution"]].head(shortlist_size)
    diagnostic_top = ranking.loc[
        ranking["eligible_activity"] & ~ranking["duplicate_behavior"]
    ].head(shortlist_size)
    payload = {
        "schema": "obi-profitability-stage1-shortlist-v1",
        "created_from_development_only": True,
        "retrospective_outcomes_read": False,
        "upper_bound_includes_depth_slippage": False,
        "spec_sha256": sha256(SPEC_PATH),
        "thresholds_sha256": sha256(THRESHOLDS_PATH),
        "development_metrics_sha256": sha256(AGGREGATE_PATH),
        "development_ranking_sha256": sha256(RANKING_PATH),
        "survivors_for_exact_execution": survivors.to_dict("records"),
        "diagnostic_top_not_automatically_advanced": diagnostic_top.to_dict("records"),
    }
    write_json(SHORTLIST_PATH, payload)
    best = ranking.iloc[0]
    return {
        "declared_policy_cells": int(len(aggregate)),
        "activity_eligible_cells": int(ranking["eligible_activity"].sum()),
        "positive_upper_bound_survivors": int(len(survivors)),
        "best_policy": str(best["policy"]),
        "best_pooled_net_upper_bound_bps": float(
            best["pooled_net_upper_bound_mean_bps"]
        ),
        "best_worst_day_net_upper_bound_bps": float(
            best["worst_development_day_net_bps"]
        ),
        "best_positive_development_days": int(best["positive_development_days"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("thresholds", "development"))
    args = parser.parse_args()
    result = build_thresholds() if args.command == "thresholds" else run_development()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
