"""Build compact cross-split machine-readable execution-economics reports."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.execution.pipeline import FROZEN_SPEC, REPORT_ROOT, ROOT, _load_json, _utc_now
from pyresearch.support.evaluate import sha256, write_csv, write_json


STAGES = ("development", "validation", "oos")
LAYERS = (
    "layer0_mid_markout",
    "layer1_bbo_depth",
    "layer2_plus_fee",
    "layer3_plus_latency",
    "layer4_stress",
)


def _weighted(group: pd.DataFrame, value: str, weight: str) -> float:
    selected = group[[value, weight]].dropna()
    total_weight = float(selected[weight].sum())
    if selected.empty or total_weight == 0:
        return math.nan
    return float((selected[value] * selected[weight]).sum() / total_weight)


def aggregate_comparison(
    frame: pd.DataFrame,
    *,
    stage: str,
    dimensions: list[str],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    grouper: str | list[str] = dimensions[0] if len(dimensions) == 1 else dimensions
    for keys, group in frame.groupby(grouper, sort=True, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {"stage": stage, **dict(zip(dimensions, keys))}
        for layer in LAYERS:
            trades_column = f"{layer}_trades"
            row[f"{layer}_trades"] = int(group[trades_column].sum())
            row[f"{layer}_total_pnl_usdt"] = float(
                group[f"{layer}_total_pnl_usdt"].sum()
            )
            row[f"{layer}_average_pnl_ticks"] = _weighted(
                group, f"{layer}_average_pnl_ticks", trades_column
            )
            row[f"{layer}_win_rate"] = _weighted(
                group, f"{layer}_win_rate", trades_column
            )
        daily_returns = group["layer4_stress_total_pnl_usdt"].to_numpy(dtype="float64") / 1000.0
        if daily_returns.size > 1 and np.std(daily_returns, ddof=1) > 0:
            row["layer4_sample_daily_sharpe"] = float(
                np.mean(daily_returns) / np.std(daily_returns, ddof=1)
            )
        else:
            row["layer4_sample_daily_sharpe"] = math.nan
        row["observed_days"] = int(group["date"].nunique())
        rows.append(row)
    return pd.DataFrame(rows)


def build_reports() -> dict[str, Any]:
    spec = _load_json(FROZEN_SPEC)
    stage_rows: list[dict[str, Any]] = []
    day_rows: list[pd.DataFrame] = []
    horizon_parts: list[pd.DataFrame] = []
    signal_parts: list[pd.DataFrame] = []
    sensitivity_parts: list[pd.DataFrame] = []
    capacity_parts: list[pd.DataFrame] = []
    for stage in STAGES:
        base = REPORT_ROOT / stage
        summary = _load_json(base / "run_summary.json")
        layer_metrics = pd.read_csv(base / "layer_metrics.csv")
        costs = summary["cost_decomposition"]
        counters = summary["canonical_counters"]
        for record in layer_metrics.to_dict("records"):
            stage_rows.append(
                {
                    "stage": stage,
                    **record,
                    "average_spread_drag_ticks": costs["average_spread_drag_ticks"],
                    "average_depth_slippage_drag_ticks": costs[
                        "average_depth_slippage_drag_ticks"
                    ],
                    "average_latency_decay_ticks": costs["average_latency_decay_ticks"],
                    "average_fee_drag_ticks": costs["average_fee_drag_ticks"],
                    "break_even_fee_bps_per_side": costs[
                        "break_even_post_execution_fee_bps_per_side"
                    ],
                    "signals": counters["signals"],
                    "skipped_overlap": counters["skipped_overlap"],
                    "excluded_gap": counters["excluded_gap"],
                    "excluded_depth": counters["excluded_depth"],
                    "unrealistic_liquidity": counters["unrealistic_liquidity"],
                }
            )
        daily = pd.read_csv(base / "daily_pnl.csv")
        daily.insert(0, "stage", stage)
        day_rows.append(daily)
        horizon_parts.append(
            aggregate_comparison(
                pd.read_csv(base / "horizon_comparison_daily.csv"),
                stage=stage,
                dimensions=["horizon_ms"],
            )
        )
        signal_parts.append(
            aggregate_comparison(
                pd.read_csv(base / "signal_comparison_daily.csv"),
                stage=stage,
                dimensions=["model"],
            )
        )
        sensitivity_parts.append(
            aggregate_comparison(
                pd.read_csv(base / "cost_sensitivity_daily.csv"),
                stage=stage,
                dimensions=[
                    "latency_ms",
                    "effective_quote_delay_ms",
                    "fee_bps_per_side",
                    "penalty_ticks_per_fill",
                ],
            )
        )
        capacity_parts.append(
            aggregate_comparison(
                pd.read_csv(base / "capacity_daily.csv"),
                stage=stage,
                dimensions=["notional_usdt"],
            )
        )

    stage_table = pd.DataFrame(stage_rows)
    daily_table = pd.concat(day_rows, ignore_index=True)
    horizon_table = pd.concat(horizon_parts, ignore_index=True)
    signal_table = pd.concat(signal_parts, ignore_index=True)
    sensitivity_table = pd.concat(sensitivity_parts, ignore_index=True)
    capacity_table = pd.concat(capacity_parts, ignore_index=True)
    write_csv(REPORT_ROOT / "stage_layer_summary.csv", stage_table)
    write_csv(REPORT_ROOT / "daily_pnl_all_splits.csv", daily_table)
    write_csv(REPORT_ROOT / "horizon_comparison.csv", horizon_table)
    write_csv(REPORT_ROOT / "signal_comparison.csv", signal_table)
    write_csv(REPORT_ROOT / "cost_sensitivity.csv", sensitivity_table)
    write_csv(REPORT_ROOT / "capacity_comparison.csv", capacity_table)

    oos_layer4 = stage_table.loc[
        (stage_table["stage"] == "oos") & (stage_table["layer"] == "layer4_stress")
    ].iloc[0]
    summary = {
        "schema": "execution-economics-final-summary-v1",
        "created_at_utc": _utc_now(),
        "alpha_spec_sha256": spec["audit"]["alpha_spec_sha256"],
        "execution_spec_sha256": sha256(FROZEN_SPEC),
        "code_commit_before_execution_experiment": spec["audit"][
            "code_commit_before_execution_experiment"
        ],
        "selected_rule": spec["selected_rule"],
        "verdict": "frozen_predictive_edge_does_not_survive_baseline_taker_costs",
        "oos": {
            "trades": int(oos_layer4["trades"]),
            "layer4_total_pnl_usdt": float(oos_layer4["total_pnl_usdt"]),
            "layer4_average_pnl_ticks": float(oos_layer4["average_pnl_ticks"]),
            "layer4_win_rate": float(oos_layer4["win_rate"]),
            "layer4_max_drawdown_usdt": float(oos_layer4["max_drawdown_usdt"]),
            "sample_daily_sharpe": _load_json(
                REPORT_ROOT / "oos/run_summary.json"
            )["daily_sharpe"]["layer4_stress"]["sample_daily_sharpe"],
        },
        "machine_readable_outputs": [
            str(path.relative_to(ROOT))
            for path in (
                REPORT_ROOT / "stage_layer_summary.csv",
                REPORT_ROOT / "daily_pnl_all_splits.csv",
                REPORT_ROOT / "horizon_comparison.csv",
                REPORT_ROOT / "signal_comparison.csv",
                REPORT_ROOT / "cost_sensitivity.csv",
                REPORT_ROOT / "capacity_comparison.csv",
            )
        ],
        "interpretation": (
            "The signal retains positive mid and no-fee executable markout in every split, "
            "but baseline taker fees dominate spread, depth, and latency costs. This rejects "
            "the V1 aggressive-taker implementation, not the predictive signal itself."
        ),
    }
    write_json(REPORT_ROOT / "final_summary.json", summary)
    return summary


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(build_reports(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
