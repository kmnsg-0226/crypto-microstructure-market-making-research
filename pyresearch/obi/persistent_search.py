"""Loop-2 screen for causally accumulated OBI/microstructure pressure."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.obi.search import (
    FEATURE_ROOT,
    MODELS_PATH,
    REPORT_ROOT,
    TRANSFORMS_PATH,
    _model_inputs,
    _prefix_metrics,
    aggregate_development,
    bbo_returns,
    derive_arrays,
    load_day,
    regime_masks,
)
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
SPEC_PATH = ROOT / "research/specs/obi_persistent_search_spec.json"
STAGE1_THRESHOLDS_PATH = REPORT_ROOT / "stage1_thresholds.json"
OUTPUT_ROOT = REPORT_ROOT / "loop2_persistent"
THRESHOLDS_PATH = OUTPUT_ROOT / "thresholds.json"
DAY_METRICS_PATH = OUTPUT_ROOT / "development_day_metrics.csv"
AGGREGATE_PATH = OUTPUT_ROOT / "development_metrics.csv"
RANKING_PATH = OUTPUT_ROOT / "development_ranking.csv"
SHORTLIST_PATH = OUTPUT_ROOT / "shortlist_before_replication.json"
DEVELOPMENT_DATES = [f"2026-0{month}-01" for month in range(1, 6)]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def trailing_segment_mean(
    values: np.ndarray,
    segments: np.ndarray,
    window: int,
) -> np.ndarray:
    """Full-window trailing mean that cannot cross a segment or NaN."""
    if window <= 0 or len(values) != len(segments):
        raise ValueError("invalid trailing segment mean input")
    values = np.asarray(values, dtype="float64")
    segments = np.asarray(segments, dtype="float64")
    result = np.full(len(values), np.nan, dtype="float64")
    finite_segment = np.isfinite(segments)
    index = 0
    while index < len(values):
        if not finite_segment[index]:
            index += 1
            continue
        end = index + 1
        while (
            end < len(values)
            and finite_segment[end]
            and segments[end] == segments[index]
        ):
            end += 1
        chunk = values[index:end]
        if len(chunk) >= window:
            finite = np.isfinite(chunk)
            sums = np.concatenate(([0.0], np.cumsum(np.where(finite, chunk, 0.0))))
            counts = np.concatenate(([0], np.cumsum(finite, dtype="int64")))
            rolling_sum = sums[window:] - sums[:-window]
            rolling_count = counts[window:] - counts[:-window]
            output_index = np.arange(index + window - 1, end)
            complete = rolling_count == window
            result[output_index[complete]] = rolling_sum[complete] / window
        index = end
    return result


def derive_persistent_signals(
    frame: pd.DataFrame,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, np.ndarray]]:
    model, transforms = _model_inputs()
    base, context = derive_arrays(frame, model=model, transforms=transforms)
    segments = context["feature_segment_id"]

    def mean(name: str, seconds: int) -> np.ndarray:
        return trailing_segment_mean(base[name], segments, seconds * 10)

    ofi = base["normalized_ofi_1s"]
    ofi_scale = transforms["standardization"]["normalized_ofi_1s"]
    ofi_z = (ofi - float(ofi_scale["mean"])) / float(ofi_scale["population_std"])
    signals = {
        "obi_l1_mean_1s": mean("obi_l1", 1),
        "obi_l1_mean_5s": mean("obi_l1", 5),
        "obi_l1_mean_30s": mean("obi_l1", 30),
        "obi_l1_mean_60s": mean("obi_l1", 60),
        "obi_l5_mean_5s": mean("obi_l5", 5),
        "obi_l5_mean_30s": mean("obi_l5", 30),
        "weighted_obi_l10_mean_5s": mean("weighted_obi_l10", 5),
        "weighted_obi_l10_mean_30s": mean("weighted_obi_l10", 30),
        "obi_l1_sign_persistence_5s": trailing_segment_mean(
            np.sign(base["obi_l1"]), segments, 50
        ),
        "obi_l1_sign_persistence_30s": trailing_segment_mean(
            np.sign(base["obi_l1"]), segments, 300
        ),
        "ti_1s_mean_5s": mean("ti_1s", 5),
        "ti_1s_mean_30s": mean("ti_1s", 30),
        "ofi_1s_z_mean_5s": trailing_segment_mean(ofi_z, segments, 50),
        "ofi_1s_z_mean_30s": trailing_segment_mean(ofi_z, segments, 300),
        "micro_consensus_z_mean_5s": trailing_segment_mean(
            base["micro_consensus_z"], segments, 50
        ),
        "micro_consensus_z_mean_30s": trailing_segment_mean(
            base["micro_consensus_z"], segments, 300
        ),
    }
    return signals, base, context


def build_thresholds() -> dict[str, Any]:
    spec = _load_json(SPEC_PATH)
    if spec["status"] != "declared_after_loop1_failure_before_persistent_signal_outcomes":
        raise ValueError("loop-2 disclosure status changed")
    if sha256(STAGE1_THRESHOLDS_PATH) != spec["audit"]["loop1_thresholds_sha256"]:
        raise ValueError("loop-1 threshold artifact changed before loop 2")
    step = int(spec["data"]["threshold_sample_every_n_rows"])
    quantiles = np.asarray(spec["absolute_signal_tail_quantiles"], dtype="float64")
    names = list(spec["signals"])
    sampled: dict[str, list[np.ndarray]] = {name: [] for name in names}
    feature_hashes = {}
    for date in DEVELOPMENT_DATES:
        frame = load_day(date)
        signals, _, _ = derive_persistent_signals(frame)
        if list(signals) != names:
            raise ValueError("persistent signal implementation differs from frozen spec")
        for name, values in signals.items():
            values = np.abs(values[::step])
            sampled[name].append(values[np.isfinite(values)])
        feature_hashes[date] = sha256(FEATURE_ROOT / date / "features_100ms.parquet")
    thresholds = {}
    counts = {}
    for name, chunks in sampled.items():
        values = np.concatenate(chunks)
        fitted = np.quantile(values, quantiles, method="linear")
        thresholds[name] = {
            f"q{quantile:.4f}": float(value)
            for quantile, value in zip(quantiles, fitted)
        }
        counts[name] = int(len(values))
    stage1 = _load_json(STAGE1_THRESHOLDS_PATH)
    payload = {
        "schema": "obi-persistent-search-thresholds-v1",
        "spec_sha256": sha256(SPEC_PATH),
        "loop1_thresholds_sha256": sha256(STAGE1_THRESHOLDS_PATH),
        "models_sha256": sha256(MODELS_PATH),
        "transforms_sha256": sha256(TRANSFORMS_PATH),
        "fit_dates": DEVELOPMENT_DATES,
        "sample_every_n_rows": step,
        "outcomes_read": False,
        "feature_sha256": feature_hashes,
        "signal_finite_sample_rows": counts,
        "signal_absolute_thresholds": thresholds,
        "regime_thresholds": stage1["regime_thresholds"],
    }
    write_json(THRESHOLDS_PATH, payload)
    return payload


def evaluate_day(date: str, thresholds: dict[str, Any]) -> pd.DataFrame:
    spec = _load_json(SPEC_PATH)
    frame = load_day(date)
    signals, base, context = derive_persistent_signals(frame)
    horizons = [int(value) for value in spec["fixed_horizons_ms"]]
    quantiles = [float(value) for value in spec["absolute_signal_tail_quantiles"]]
    regimes_expected = list(spec["regimes"])
    execution = spec["optimistic_execution_upper_bound"]
    outcomes = {
        horizon: bbo_returns(
            context,
            horizon_ms=horizon,
            latency_ms=int(execution["latency_ms"]),
            fee_bps_per_side=float(execution["fee_bps_per_side"]),
        )
        for horizon in horizons
    }
    rows = []
    for signal_name, values in signals.items():
        absolute = np.abs(values)
        finite = np.isfinite(values) & (values != 0)
        order = np.argsort(-np.where(finite, absolute, -np.inf), kind="stable")
        order = order[finite[order]]
        all_masks = regime_masks(values, base, context, thresholds)
        masks = {name: all_masks[name] for name in regimes_expected}
        threshold_map = thresholds["signal_absolute_thresholds"][signal_name]
        threshold_items = [
            (quantile, float(threshold_map[f"q{quantile:.4f}"]))
            for quantile in quantiles
        ]
        sign = np.sign(values)
        for horizon, outcome in outcomes.items():
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
                eligible = order[regime_mask[order] & outcome["valid"][order]]
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
                        ordered_abs=absolute[eligible],
                        gross_bps=gross[eligible],
                        net_bps=net[eligible],
                    ))
    return pd.DataFrame(rows)


def rank_development(aggregate: pd.DataFrame) -> pd.DataFrame:
    gate = _load_json(SPEC_PATH)["development_gate"]
    result = aggregate.copy()
    result["eligible_activity"] = (
        (result["observations"] >= int(gate["minimum_observations_total"]))
        & (result["minimum_observations_day"] >= int(gate["minimum_observations_each_day"]))
    )
    behavior = [
        "signal", "direction", "regime", "absolute_signal_threshold", "horizon_ms"
    ]
    result["duplicate_behavior"] = result.duplicated(behavior, keep="first")
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
    thresholds = build_thresholds()
    day = pd.concat(
        [evaluate_day(date, thresholds) for date in DEVELOPMENT_DATES],
        ignore_index=True,
    )
    spec = _load_json(SPEC_PATH)
    expected = (
        len(spec["signals"])
        * len(spec["regimes"])
        * len(spec["absolute_signal_tail_quantiles"])
        * len(spec["directions"])
        * len(spec["fixed_horizons_ms"])
        * len(DEVELOPMENT_DATES)
    )
    if len(day) != expected:
        raise ValueError(f"unexpected persistent screen rows: {len(day)} != {expected}")
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
        "schema": "obi-persistent-shortlist-v1",
        "created_from_development_only": True,
        "retrospective_outcomes_read": False,
        "spec_sha256": sha256(SPEC_PATH),
        "thresholds_sha256": sha256(THRESHOLDS_PATH),
        "development_metrics_sha256": sha256(AGGREGATE_PATH),
        "development_ranking_sha256": sha256(RANKING_PATH),
        "survivors_for_exact_execution": survivors.to_dict("records"),
        "diagnostic_top_not_automatically_advanced": diagnostic.to_dict("records"),
    })
    best_pooled = ranking.loc[
        ranking["eligible_activity"] & ~ranking["duplicate_behavior"]
    ].sort_values("pooled_net_upper_bound_mean_bps", ascending=False).iloc[0]
    return {
        "declared_policy_cells": int(len(aggregate)),
        "activity_eligible_cells": int(ranking["eligible_activity"].sum()),
        "positive_upper_bound_survivors": int(len(survivors)),
        "best_pooled_policy": str(best_pooled["policy"]),
        "best_pooled_gross_bps": float(best_pooled["pooled_gross_bbo_mean_bps"]),
        "best_pooled_net_bps": float(best_pooled["pooled_net_upper_bound_mean_bps"]),
        "best_pooled_positive_days": int(best_pooled["positive_development_days"]),
        "best_pooled_worst_day_bps": float(
            best_pooled["worst_development_day_net_bps"]
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("thresholds", "development"))
    args = parser.parse_args()
    result = build_thresholds() if args.command == "thresholds" else run_development()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
