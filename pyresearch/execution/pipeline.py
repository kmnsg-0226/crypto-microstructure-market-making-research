"""Leakage-gated execution-economics research on frozen 100 ms features.

The command order is deliberately enforced: development selection creates an
execution-only frozen specification; validation and OOS refuse to run without
that file.  The alpha model and its development transforms are only loaded,
never fitted here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from typing import Any, Iterable

import numpy as np
import pandas as pd

from pyresearch.execution.engine import (
    RunCounters,
    add_cost_layers,
    daily_sharpe,
    frozen_prediction,
    load_execution_day,
    performance_metrics,
    run_day,
)
from pyresearch.support.evaluate import sha256, write_csv, write_json


from pyresearch import ROOT
DRAFT_SPEC = ROOT / "research/specs/execution_spec_draft.json"
FROZEN_SPEC = ROOT / "research/specs/execution_spec_frozen.json"
FROZEN_SPEC_HASH = ROOT / "research/specs/execution_spec_frozen.json.sha256"
FEATURE_ROOT = ROOT / "data/research/tardis"
REPORT_ROOT = FEATURE_ROOT / "reports/execution"
ALPHA_MODELS = ROOT / "data/research/tardis/reports/development/fitted_models.json"
ALPHA_TRANSFORMS = ROOT / "data/research/tardis/reports/development/development_transforms.json"

AUDIT_PATHS = {
    "alpha_spec_sha256": ROOT / "research/specs/research_spec_frozen.json",
    "l2_manifest_sha256": ROOT / "data/historical/tardis/reports/2026-first-days/manifest.json",
    "trades_manifest_sha256": ROOT / "data/historical/tardis/binance-futures/trades/trades_manifest.json",
}

LAYER_COLUMNS = {
    "layer0_mid_markout": ("layer0_mid_pnl_usdt", "layer0_mid_ticks"),
    "layer1_bbo_depth": ("layer1_gross_pnl_usdt", "layer1_gross_ticks"),
    "layer2_plus_fee": ("layer2_net_pnl_usdt", "layer2_net_ticks"),
    "layer3_plus_latency": ("layer3_net_pnl_usdt", "layer3_net_ticks"),
    "layer4_stress": ("layer4_net_pnl_usdt", "layer4_net_ticks"),
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _frame_checksum(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    digest.update("\n".join(frame.columns).encode())
    digest.update("\n".join(map(str, frame.dtypes)).encode())
    digest.update(pd.util.hash_pandas_object(frame, index=False).values.tobytes())
    return digest.hexdigest()


def _write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    frame.to_parquet(temporary, index=False, compression="zstd")
    os.replace(temporary, path)


def _audit_inputs(spec: dict[str, Any]) -> dict[str, str]:
    observed: dict[str, str] = {}
    for key, path in AUDIT_PATHS.items():
        observed[key] = sha256(path)
        expected = spec["audit"][key]
        if observed[key] != expected:
            raise ValueError(f"audit hash mismatch for {path}: {observed[key]} != {expected}")
    bundle_payload = (
        observed["l2_manifest_sha256"] + "\n" + observed["trades_manifest_sha256"] + "\n"
    ).encode()
    observed["dataset_bundle_sha256"] = hashlib.sha256(bundle_payload).hexdigest()
    if observed["dataset_bundle_sha256"] != spec["audit"]["dataset_bundle_sha256"]:
        raise ValueError("dataset bundle hash mismatch")
    observed["alpha_models_sha256"] = sha256(ALPHA_MODELS)
    observed["alpha_transforms_sha256"] = sha256(ALPHA_TRANSFORMS)
    return observed


def _load_alpha() -> tuple[dict[str, Any], dict[str, Any]]:
    return _load_json(ALPHA_MODELS)["models"], _load_json(ALPHA_TRANSFORMS)


def _model_features(models: dict[str, Any], names: Iterable[str], horizons: Iterable[int]) -> list[str]:
    features: set[str] = set()
    for name in names:
        for horizon in horizons:
            features.update(models[f"{name}:{horizon}"]["features"])
    return sorted(features)


def _feature_path(date: str) -> Path:
    return FEATURE_ROOT / date / "features_100ms.parquet"


def _validate_stage(spec: dict[str, Any], stage: str) -> list[str]:
    if stage not in {"development", "validation", "oos"}:
        raise ValueError(f"unknown execution stage: {stage}")
    dates = list(spec["split"][stage])
    if stage in {"validation", "oos"}:
        if spec.get("status") != "frozen_after_development":
            raise ValueError(f"{stage} requires frozen execution specification")
        if "selected_rule" not in spec:
            raise ValueError("frozen execution specification has no selected rule")
    missing = [str(_feature_path(date)) for date in dates if not _feature_path(date).exists()]
    if missing:
        raise FileNotFoundError(f"missing stage feature data: {missing}")
    return dates


def _run(
    frame: pd.DataFrame,
    prediction: np.ndarray,
    spec: dict[str, Any],
    *,
    model: str,
    horizon_ms: int,
    threshold: float,
    latency_ms: int,
    notional: float,
) -> tuple[pd.DataFrame, RunCounters]:
    return run_day(
        frame,
        prediction,
        model_name=model,
        horizon_ms=horizon_ms,
        prediction_threshold_ticks=threshold,
        latency_ms=latency_ms,
        notional_usdt=notional,
        tick_size=float(spec["instrument"]["tick_size"]),
        quantity_step=float(spec["instrument"]["quantity_step"]),
        unrealistic_participation_threshold=float(
            spec["fills"]["unrealistic_visible_liquidity_participation_threshold"]
        ),
    )


def _summary_row(
    trades: pd.DataFrame,
    *,
    dimensions: dict[str, Any],
    primary_notional: float,
) -> dict[str, Any]:
    row = dict(dimensions)
    for layer, (pnl_column, ticks_column) in LAYER_COLUMNS.items():
        metrics = performance_metrics(
            trades,
            pnl_usdt_column=pnl_column,
            pnl_ticks_column=ticks_column,
            primary_notional_usdt=primary_notional,
        )
        for metric in (
            "trades",
            "total_pnl_usdt",
            "average_pnl_usdt",
            "average_pnl_ticks",
            "median_pnl_ticks",
            "win_rate",
            "profit_factor",
            "max_drawdown_usdt",
        ):
            row[f"{layer}_{metric}"] = metrics[metric]
    return row


def select_and_freeze() -> dict[str, Any]:
    if FROZEN_SPEC.exists():
        frozen = _load_json(FROZEN_SPEC)
        if frozen.get("status") != "frozen_after_development":
            raise ValueError("existing execution specification is not validly frozen")
        if not FROZEN_SPEC_HASH.exists() or FROZEN_SPEC_HASH.read_text(
            encoding="utf-8"
        ).strip() != sha256(FROZEN_SPEC):
            raise ValueError("existing frozen execution specification hash mismatch")
        return frozen
    draft = _load_json(DRAFT_SPEC)
    if draft.get("status") != "draft_before_execution_results":
        raise ValueError("development selection requires the pre-result draft specification")
    audit = _audit_inputs(draft)
    dates = _validate_stage(draft, "development")
    models, transforms = _load_alpha()
    candidates = list(draft["alpha"]["candidate_models"])
    horizon = int(draft["alpha"]["canonical_horizon_ms"])
    thresholds = [float(value) for value in draft["alpha"]["candidate_absolute_prediction_threshold_ticks"]]
    features = _model_features(models, candidates, [horizon])
    baseline_latency = int(draft["timing"]["baseline_latency_ms"])
    primary_notional = float(draft["position"]["primary_notional_usdt"])
    baseline_fee = float(draft["fees"]["baseline_bps_per_side"])
    tick_size = float(draft["instrument"]["tick_size"])
    day_rows: list[dict[str, Any]] = []

    for date in dates:
        frame = load_execution_day(_feature_path(date), features)
        predictions = {
            name: frozen_prediction(frame, models[f"{name}:{horizon}"], transforms)
            for name in candidates
        }
        for name in candidates:
            for threshold in thresholds:
                raw, counters = _run(
                    frame,
                    predictions[name],
                    draft,
                    model=name,
                    horizon_ms=horizon,
                    threshold=threshold,
                    latency_ms=baseline_latency,
                    notional=primary_notional,
                )
                trades = add_cost_layers(
                    raw,
                    fee_bps_per_side=baseline_fee,
                    penalty_ticks_per_fill=0.0,
                    tick_size=tick_size,
                )
                if trades.empty:
                    layer1_total = 0.0
                    layer1_average = math.nan
                    layer3_total = 0.0
                    layer3_average = math.nan
                else:
                    layer1_total = float(trades["layer1_gross_pnl_usdt"].sum())
                    layer1_average = float(trades["layer1_gross_ticks"].mean())
                    layer3_total = float(trades["layer3_net_pnl_usdt"].sum())
                    layer3_average = float(trades["layer3_net_ticks"].mean())
                day_rows.append(
                    {
                        "date": date,
                        "model": name,
                        "threshold_ticks": threshold,
                        **counters.to_dict(),
                        "layer1_total_pnl_usdt": layer1_total,
                        "layer1_average_ticks": layer1_average,
                        "layer3_total_pnl_usdt": layer3_total,
                        "layer3_average_ticks": layer3_average,
                    }
                )

    daily = pd.DataFrame(day_rows)
    aggregate_rows: list[dict[str, Any]] = []
    minimum_total = int(draft["development_selection"]["minimum_completed_trades_total"])
    minimum_daily = int(draft["development_selection"]["minimum_completed_trades_per_development_day"])
    for (name, threshold), group in daily.groupby(["model", "threshold_ticks"], sort=True):
        completed = int(group["completed_trades"].sum())
        weighted_layer1 = float(
            (group["layer1_average_ticks"] * group["completed_trades"]).sum() / completed
        ) if completed else math.nan
        weighted_layer3 = float(
            (group["layer3_average_ticks"] * group["completed_trades"]).sum() / completed
        ) if completed else math.nan
        aggregate_rows.append(
            {
                "model": name,
                "threshold_ticks": float(threshold),
                "completed_trades_total": completed,
                "minimum_completed_trades_on_a_day": int(group["completed_trades"].min()),
                "positive_day_fraction_layer3": float((group["layer3_total_pnl_usdt"] > 0).mean()),
                "layer1_average_ticks": weighted_layer1,
                "layer3_average_net_ticks": weighted_layer3,
                "eligible": bool(
                    completed >= minimum_total
                    and int(group["completed_trades"].min()) >= minimum_daily
                ),
            }
        )
    aggregate = pd.DataFrame(aggregate_rows)
    eligible = aggregate.loc[aggregate["eligible"]].copy()
    if eligible.empty:
        raise RuntimeError("no development execution candidate passed the predeclared activity floor")
    ranked = eligible.sort_values(
        [
            "layer3_average_net_ticks",
            "positive_day_fraction_layer3",
            "layer1_average_ticks",
            "threshold_ticks",
            "model",
        ],
        ascending=[False, False, False, True, True],
        kind="stable",
    )
    selected = ranked.iloc[0].to_dict()
    output_dir = REPORT_ROOT / "development_selection"
    write_csv(output_dir / "candidate_daily.csv", daily)
    write_csv(output_dir / "candidate_diagnostics.csv", aggregate)
    write_json(
        output_dir / "selection_summary.json",
        {
            "schema": "execution-development-selection-v1",
            "created_at_utc": _utc_now(),
            "audit": audit,
            "selection_rule": draft["development_selection"],
            "selected": selected,
        },
    )

    frozen = json.loads(json.dumps(draft))
    frozen["status"] = "frozen_after_development"
    frozen["frozen_at_utc"] = _utc_now()
    frozen["audit"]["alpha_models_sha256"] = audit["alpha_models_sha256"]
    frozen["audit"]["alpha_transforms_sha256"] = audit["alpha_transforms_sha256"]
    frozen["selected_rule"] = {
        "model": str(selected["model"]),
        "absolute_prediction_threshold_ticks": float(selected["threshold_ticks"]),
        "horizon_ms": horizon,
        "latency_ms": baseline_latency,
        "fee_bps_per_side": baseline_fee,
        "stress_penalty_ticks_per_fill": float(
            draft["slippage_stress"]["baseline_stress_ticks_per_fill"]
        ),
        "notional_usdt": primary_notional,
        "overlap_rule": draft["position"]["overlap_rule"],
    }
    frozen["development_selection_result"] = {
        "candidate_diagnostics_path": str(
            (output_dir / "candidate_diagnostics.csv").relative_to(ROOT)
        ),
        "candidate_diagnostics_sha256": sha256(output_dir / "candidate_diagnostics.csv"),
        "selected_development_layer3_average_net_ticks": float(
            selected["layer3_average_net_ticks"]
        ),
        "selected_completed_trades_total": int(selected["completed_trades_total"]),
        "selected_positive_day_fraction_layer3": float(
            selected["positive_day_fraction_layer3"]
        ),
    }
    write_json(FROZEN_SPEC, frozen)
    FROZEN_SPEC_HASH.write_text(sha256(FROZEN_SPEC) + "\n", encoding="utf-8")
    return frozen


def evaluate_stage(stage: str) -> dict[str, Any]:
    spec = _load_json(FROZEN_SPEC)
    if spec.get("status") != "frozen_after_development":
        raise ValueError("evaluation requires the frozen execution specification")
    audit = _audit_inputs(spec)
    if sha256(ALPHA_MODELS) != spec["audit"]["alpha_models_sha256"]:
        raise ValueError("frozen alpha model artifact changed")
    if sha256(ALPHA_TRANSFORMS) != spec["audit"]["alpha_transforms_sha256"]:
        raise ValueError("frozen alpha transforms changed")
    dates = _validate_stage(spec, stage)
    selected = spec["selected_rule"]
    selected_model = str(selected["model"])
    selected_horizon = int(selected["horizon_ms"])
    selected_threshold = float(selected["absolute_prediction_threshold_ticks"])
    primary_notional = float(selected["notional_usdt"])
    baseline_latency = int(selected["latency_ms"])
    baseline_fee = float(selected["fee_bps_per_side"])
    baseline_penalty = float(selected["stress_penalty_ticks_per_fill"])
    tick_size = float(spec["instrument"]["tick_size"])
    models, transforms = _load_alpha()
    candidates = list(spec["alpha"]["candidate_models"])
    horizons = [int(value) for value in spec["alpha"]["diagnostic_horizons_ms"]]
    features = _model_features(models, candidates, horizons)
    output_dir = REPORT_ROOT / stage
    stage_opened_at = _utc_now()
    evaluation_commit = _git_head()
    if stage == "oos":
        write_json(
            output_dir / "oos_audit_log.json",
            {
                "status": "opened_before_oos_feature_data_load",
                "opened_at_utc": stage_opened_at,
                "dates": dates,
                "frozen_execution_spec_sha256": sha256(FROZEN_SPEC),
                "alpha_spec_sha256": audit["alpha_spec_sha256"],
                "dataset_bundle_sha256": audit["dataset_bundle_sha256"],
                "code_commit_before_execution_experiment": spec["audit"][
                    "code_commit_before_execution_experiment"
                ],
                "code_commit_at_evaluation": evaluation_commit,
            },
        )

    canonical_parts: list[pd.DataFrame] = []
    counter_rows: list[dict[str, Any]] = []
    horizon_rows: list[dict[str, Any]] = []
    signal_rows: list[dict[str, Any]] = []
    sensitivity_rows: list[dict[str, Any]] = []
    capacity_rows: list[dict[str, Any]] = []
    deterministic_rows: list[dict[str, Any]] = []

    for date in dates:
        frame = load_execution_day(_feature_path(date), features)
        prediction_cache: dict[tuple[str, int], np.ndarray] = {}
        raw_cache: dict[tuple[str, int, float, int, float], tuple[pd.DataFrame, RunCounters]] = {}

        def prediction(name: str, horizon: int) -> np.ndarray:
            key = (name, horizon)
            if key not in prediction_cache:
                prediction_cache[key] = frozen_prediction(
                    frame, models[f"{name}:{horizon}"], transforms
                )
            return prediction_cache[key]

        def raw_run(
            name: str,
            horizon: int,
            threshold: float,
            latency: int,
            notional: float,
        ) -> tuple[pd.DataFrame, RunCounters]:
            key = (name, horizon, threshold, latency, notional)
            if key not in raw_cache:
                raw_cache[key] = _run(
                    frame,
                    prediction(name, horizon),
                    spec,
                    model=name,
                    horizon_ms=horizon,
                    threshold=threshold,
                    latency_ms=latency,
                    notional=notional,
                )
            return raw_cache[key]

        canonical_raw, canonical_counters = raw_run(
            selected_model,
            selected_horizon,
            selected_threshold,
            baseline_latency,
            primary_notional,
        )
        canonical = add_cost_layers(
            canonical_raw,
            fee_bps_per_side=baseline_fee,
            penalty_ticks_per_fill=baseline_penalty,
            tick_size=tick_size,
        )
        canonical_parts.append(canonical)
        counter_rows.append({"date": date, **canonical_counters.to_dict()})

        repeated_raw, repeated_counters = _run(
            frame,
            prediction(selected_model, selected_horizon),
            spec,
            model=selected_model,
            horizon_ms=selected_horizon,
            threshold=selected_threshold,
            latency_ms=baseline_latency,
            notional=primary_notional,
        )
        deterministic_rows.append(
            {
                "date": date,
                "first_checksum": _frame_checksum(canonical_raw),
                "second_checksum": _frame_checksum(repeated_raw),
                "counters_match": canonical_counters.to_dict() == repeated_counters.to_dict(),
                "match": _frame_checksum(canonical_raw) == _frame_checksum(repeated_raw)
                and canonical_counters.to_dict() == repeated_counters.to_dict(),
            }
        )

        for horizon in horizons:
            raw, _ = raw_run(
                selected_model,
                horizon,
                selected_threshold,
                baseline_latency,
                primary_notional,
            )
            costed = add_cost_layers(
                raw,
                fee_bps_per_side=baseline_fee,
                penalty_ticks_per_fill=baseline_penalty,
                tick_size=tick_size,
            )
            horizon_rows.append(
                _summary_row(
                    costed,
                    dimensions={"date": date, "horizon_ms": horizon},
                    primary_notional=primary_notional,
                )
            )

        for name in candidates:
            raw, _ = raw_run(
                name,
                selected_horizon,
                selected_threshold,
                baseline_latency,
                primary_notional,
            )
            costed = add_cost_layers(
                raw,
                fee_bps_per_side=baseline_fee,
                penalty_ticks_per_fill=baseline_penalty,
                tick_size=tick_size,
            )
            signal_rows.append(
                _summary_row(
                    costed,
                    dimensions={"date": date, "model": name},
                    primary_notional=primary_notional,
                )
            )

        for latency in spec["timing"]["latency_scenarios_ms"]:
            raw, _ = raw_run(
                selected_model,
                selected_horizon,
                selected_threshold,
                int(latency),
                primary_notional,
            )
            for fee in spec["fees"]["scenarios_bps_per_side"]:
                for penalty in spec["slippage_stress"]["execution_penalty_ticks_per_fill"]:
                    costed = add_cost_layers(
                        raw,
                        fee_bps_per_side=float(fee),
                        penalty_ticks_per_fill=float(penalty),
                        tick_size=tick_size,
                    )
                    sensitivity_rows.append(
                        _summary_row(
                            costed,
                            dimensions={
                                "date": date,
                                "latency_ms": int(latency),
                                "effective_quote_delay_ms": int(math.ceil(int(latency) / 100) * 100),
                                "fee_bps_per_side": float(fee),
                                "penalty_ticks_per_fill": float(penalty),
                            },
                            primary_notional=primary_notional,
                        )
                    )

        for notional in spec["position"]["capacity_notional_scenarios_usdt"]:
            raw, counters = raw_run(
                selected_model,
                selected_horizon,
                selected_threshold,
                baseline_latency,
                float(notional),
            )
            costed = add_cost_layers(
                raw,
                fee_bps_per_side=baseline_fee,
                penalty_ticks_per_fill=baseline_penalty,
                tick_size=tick_size,
            )
            row = _summary_row(
                costed,
                dimensions={"date": date, "notional_usdt": float(notional)},
                primary_notional=float(notional),
            )
            row.update(
                {
                    "excluded_depth": counters.excluded_depth,
                    "unrealistic_liquidity": counters.unrealistic_liquidity,
                    "unrealistic_fraction": (
                        counters.unrealistic_liquidity / counters.completed_trades
                        if counters.completed_trades
                        else math.nan
                    ),
                }
            )
            capacity_rows.append(row)

    canonical_trades = pd.concat(canonical_parts, ignore_index=True)
    _write_parquet(output_dir / "canonical_trades.parquet", canonical_trades)
    counters = pd.DataFrame(counter_rows)
    daily = canonical_trades.groupby("date", sort=True)[
        [column for columns in LAYER_COLUMNS.values() for column in columns]
    ].sum().reset_index()
    layer_rows: list[dict[str, Any]] = []
    sharpe_payload: dict[str, Any] = {}
    for layer, (pnl_column, ticks_column) in LAYER_COLUMNS.items():
        layer_rows.append(
            {
                "layer": layer,
                **performance_metrics(
                    canonical_trades,
                    pnl_usdt_column=pnl_column,
                    pnl_ticks_column=ticks_column,
                    primary_notional_usdt=primary_notional,
                ),
            }
        )
        sharpe_payload[layer] = daily_sharpe(
            daily[pnl_column],
            primary_notional_usdt=primary_notional,
            bootstrap_samples=int(spec["performance"]["daily_bootstrap_samples"]),
            bootstrap_seed=int(spec["performance"]["daily_bootstrap_seed"]),
        )

    layer_metrics = pd.DataFrame(layer_rows)
    layer0_total = float(
        layer_metrics.loc[
            layer_metrics["layer"].eq("layer0_mid_markout"), "total_pnl_usdt"
        ].iloc[0]
    )
    layer_metrics["gross_to_net_conversion_ratio"] = (
        layer_metrics["total_pnl_usdt"] / layer0_total if layer0_total else math.nan
    )
    turnover = float(
        (canonical_trades["entry_notional_usdt"] + canonical_trades["exit_notional_usdt"]).sum()
    )
    actual_gross = float(canonical_trades["actual_gross_pnl_usdt"].sum())
    mid_gross = float(canonical_trades["layer0_mid_pnl_usdt"].sum())
    cost_decomposition = {
        "average_mid_markout_ticks": float(canonical_trades["layer0_mid_ticks"].mean()),
        "average_entry_spread_ticks": float(canonical_trades["entry_spread_ticks"].mean()),
        "average_exit_spread_ticks": float(canonical_trades["exit_spread_ticks"].mean()),
        "average_spread_drag_ticks": float(canonical_trades["spread_drag_ticks"].mean()),
        "total_spread_drag_usdt": float(canonical_trades["spread_drag_usdt"].sum()),
        "average_depth_slippage_drag_ticks": float(
            canonical_trades["depth_slippage_drag_ticks"].mean()
        ),
        "average_latency_decay_ticks": float(canonical_trades["latency_decay_ticks"].mean()),
        "average_fee_drag_ticks": float(canonical_trades["fee_drag_ticks"].mean()),
        "total_fee_drag_usdt": float(canonical_trades["fee_drag_usdt"].sum()),
        "total_latency_decay_usdt": float(canonical_trades["latency_decay_usdt"].sum()),
        "total_depth_slippage_drag_usdt": float(
            canonical_trades["depth_slippage_drag_usdt"].sum()
        ),
        "average_stress_penalty_drag_ticks": float(
            canonical_trades["stress_penalty_drag_ticks"].mean()
        ),
        "break_even_total_friction_from_mid_ticks_per_trade": float(
            canonical_trades["layer0_mid_ticks"].mean()
        ),
        "break_even_post_execution_fee_bps_per_side": (
            actual_gross / turnover * 10_000.0 if turnover else math.nan
        ),
        "break_even_total_cost_bps_from_mid_per_side": (
            mid_gross / turnover * 10_000.0 if turnover else math.nan
        ),
    }
    deterministic = pd.DataFrame(deterministic_rows)
    write_csv(output_dir / "canonical_counters.csv", counters)
    write_csv(output_dir / "daily_pnl.csv", daily)
    write_csv(output_dir / "layer_metrics.csv", layer_metrics)
    write_csv(output_dir / "horizon_comparison_daily.csv", pd.DataFrame(horizon_rows))
    write_csv(output_dir / "signal_comparison_daily.csv", pd.DataFrame(signal_rows))
    write_csv(output_dir / "cost_sensitivity_daily.csv", pd.DataFrame(sensitivity_rows))
    write_csv(output_dir / "capacity_daily.csv", pd.DataFrame(capacity_rows))
    write_csv(output_dir / "determinism.csv", deterministic)
    summary = {
        "schema": "execution-economics-stage-v1",
        "stage": stage,
        "dates": dates,
        "opened_at_utc": stage_opened_at,
        "created_at_utc": _utc_now(),
        "code_commit_at_evaluation": evaluation_commit,
        "audit": audit,
        "frozen_execution_spec_sha256": sha256(FROZEN_SPEC),
        "selected_rule": selected,
        "canonical_trade_rows": int(len(canonical_trades)),
        "canonical_trade_checksum": _frame_checksum(canonical_trades),
        "deterministic_replay_all_days": bool(deterministic["match"].all()),
        "canonical_counters": {
            column: int(counters[column].sum())
            for column in RunCounters.__dataclass_fields__
        },
        "cost_decomposition": cost_decomposition,
        "daily_sharpe": sharpe_payload,
        "limitations": [
            "one_position_at_a_time_fixed_notional_taker_simulation_not_live_trading",
            "25ms_50ms_and_250ms_latency_map_to_100ms_100ms_and_300ms_quotes",
            "top10_visible_depth_has_no_queue_hidden_liquidity_or_market_impact_model",
            "fee_scenarios_are_configurable_assumptions_not_current_account_fee_claims",
            "daily_sharpe_has_very_few_independent_days",
        ],
    }
    if stage == "oos":
        write_json(
            output_dir / "oos_audit_log.json",
            {
                "status": "completed_without_parameter_changes",
                "opened_at_utc": stage_opened_at,
                "completed_at_utc": summary["created_at_utc"],
                "dates": dates,
                "frozen_execution_spec_sha256": summary["frozen_execution_spec_sha256"],
                "alpha_spec_sha256": audit["alpha_spec_sha256"],
                "dataset_bundle_sha256": audit["dataset_bundle_sha256"],
                "code_commit_before_execution_experiment": spec["audit"][
                    "code_commit_before_execution_experiment"
                ],
                "code_commit_at_evaluation": summary["code_commit_at_evaluation"],
            },
        )
    write_json(output_dir / "run_summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["select", "development", "validation", "oos", "all"]
    )
    args = parser.parse_args()
    if args.command in {"select", "all"}:
        frozen = select_and_freeze()
        print(json.dumps({"selected_rule": frozen["selected_rule"]}, sort_keys=True))
    if args.command == "all":
        for stage in ("development", "validation", "oos"):
            summary = evaluate_stage(stage)
            print(json.dumps({"stage": stage, "trades": summary["canonical_trade_rows"]}))
    elif args.command in {"development", "validation", "oos"}:
        summary = evaluate_stage(args.command)
        print(json.dumps({"stage": args.command, "trades": summary["canonical_trade_rows"]}))


if __name__ == "__main__":
    main()
