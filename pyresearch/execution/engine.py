"""Deterministic one-position taker execution engine on the canonical 100ms L2 grid."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


LEVELS = 10


@dataclass(frozen=True)
class FillResult:
    side: str
    quantity: float
    vwap_price: float
    touch_price: float
    touch_quantity: float
    consumed_levels: int
    visible_quantity: float
    touch_participation: float
    visible_participation: float
    depth_slippage_ticks: float


@dataclass
class RunCounters:
    signals: int = 0
    completed_trades: int = 0
    skipped_overlap: int = 0
    excluded_latency: int = 0
    excluded_day_boundary: int = 0
    excluded_gap: int = 0
    excluded_quantity: int = 0
    excluded_depth: int = 0
    unrealistic_liquidity: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def execution_columns(model_features: list[str]) -> list[str]:
    columns = [
        "date",
        "sample_time_us",
        "valid_book_state",
        "feature_segment_id",
        "mid",
        "best_bid_price",
        "best_bid_qty",
        "best_ask_price",
        "best_ask_qty",
    ]
    for side in ("bid", "ask"):
        for level in range(1, LEVELS + 1):
            columns.extend([f"{side}_px_{level}", f"{side}_qty_{level}"])
    columns.extend(model_features)
    return list(dict.fromkeys(columns))


def load_execution_day(path: Path, model_features: list[str]) -> pd.DataFrame:
    frame = pd.read_parquet(path, columns=execution_columns(model_features))
    if frame.empty or frame["date"].nunique() != 1:
        raise ValueError(f"execution input must contain exactly one non-empty day: {path}")
    if not frame["sample_time_us"].is_monotonic_increasing:
        raise ValueError("execution sample grid is not monotonic")
    if not frame["sample_time_us"].diff().dropna().eq(100_000).all():
        raise ValueError("execution engine requires an exact 100ms grid")
    if frame.duplicated("sample_time_us").any():
        raise ValueError("duplicate execution sample timestamp")
    return frame


def frozen_prediction(
    frame: pd.DataFrame,
    model: dict[str, Any],
    transforms: dict[str, Any],
) -> np.ndarray:
    prediction = np.full(len(frame), float(model["intercept"]), dtype="float64")
    valid = np.ones(len(frame), dtype=bool)
    for feature in model["features"]:
        values = frame[feature].to_numpy(dtype="float64")
        valid &= np.isfinite(values)
        scale = transforms["standardization"][feature]
        prediction += (
            (values - float(scale["mean"])) / float(scale["population_std"])
            * float(model["standardized_coefficients"][feature])
        )
    prediction[~valid] = np.nan
    return prediction


def floor_quantity(notional_usdt: float, touch_price: float, quantity_step: float) -> float:
    if notional_usdt <= 0 or touch_price <= 0 or quantity_step <= 0:
        return 0.0
    steps = math.floor((notional_usdt / touch_price + 1e-12) / quantity_step)
    return steps * quantity_step


def walk_visible_book(
    prices: np.ndarray,
    quantities: np.ndarray,
    requested_quantity: float,
    side: str,
    tick_size: float,
) -> FillResult | None:
    if side not in {"buy", "sell"}:
        raise ValueError("fill side must be buy or sell")
    if requested_quantity <= 0 or tick_size <= 0:
        return None
    finite = np.isfinite(prices) & np.isfinite(quantities) & (prices > 0) & (quantities > 0)
    prices = prices[finite]
    quantities = quantities[finite]
    if prices.size == 0:
        return None
    if side == "buy" and np.any(np.diff(prices) < 0):
        raise ValueError("ask prices are not ascending")
    if side == "sell" and np.any(np.diff(prices) > 0):
        raise ValueError("bid prices are not descending")
    visible = float(quantities.sum())
    if visible + 1e-12 < requested_quantity:
        return None
    remaining = requested_quantity
    notional = 0.0
    consumed = 0
    for price, available in zip(prices, quantities):
        taken = min(remaining, float(available))
        notional += taken * float(price)
        remaining -= taken
        consumed += 1
        if remaining <= 1e-12:
            break
    if remaining > 1e-9:
        return None
    vwap = notional / requested_quantity
    touch = float(prices[0])
    slippage = (vwap - touch) / tick_size if side == "buy" else (touch - vwap) / tick_size
    return FillResult(
        side=side,
        quantity=requested_quantity,
        vwap_price=vwap,
        touch_price=touch,
        touch_quantity=float(quantities[0]),
        consumed_levels=consumed,
        visible_quantity=visible,
        touch_participation=requested_quantity / float(quantities[0]),
        visible_participation=requested_quantity / visible,
        depth_slippage_ticks=slippage,
    )


def _fill(
    bid_prices: np.ndarray,
    bid_quantities: np.ndarray,
    ask_prices: np.ndarray,
    ask_quantities: np.ndarray,
    row: int,
    order_side: str,
    quantity: float,
    tick_size: float,
) -> FillResult | None:
    prices = ask_prices[row] if order_side == "buy" else bid_prices[row]
    quantities = ask_quantities[row] if order_side == "buy" else bid_quantities[row]
    return walk_visible_book(prices, quantities, quantity, order_side, tick_size)


def _pnl_ticks(direction: int, entry_price: float, exit_price: float, tick_size: float) -> float:
    return direction * (exit_price - entry_price) / tick_size


def run_day(
    frame: pd.DataFrame,
    prediction: np.ndarray,
    *,
    model_name: str,
    horizon_ms: int,
    prediction_threshold_ticks: float,
    latency_ms: int,
    notional_usdt: float,
    tick_size: float,
    quantity_step: float,
    unrealistic_participation_threshold: float,
) -> tuple[pd.DataFrame, RunCounters]:
    if len(prediction) != len(frame):
        raise ValueError("prediction length does not match execution day")
    if horizon_ms <= 0 or horizon_ms % 100 != 0 or latency_ms < 0:
        raise ValueError("horizon must align to 100ms and latency must be non-negative")
    if prediction_threshold_ticks < 0:
        raise ValueError("prediction threshold must be non-negative")
    counters = RunCounters()
    horizon_steps = horizon_ms // 100
    latency_steps = math.ceil(latency_ms / 100)
    valid_prediction = (
        np.isfinite(prediction)
        & (prediction != 0)
        & (np.abs(prediction) >= prediction_threshold_ticks)
    )
    signal_indices = np.flatnonzero(valid_prediction)
    counters.signals = int(signal_indices.size)
    if latency_ms >= horizon_ms:
        counters.excluded_latency = counters.signals
        return pd.DataFrame(), counters

    valid_book = frame["valid_book_state"].eq(1).to_numpy()
    segments = frame["feature_segment_id"].to_numpy(dtype="float64", na_value=np.nan)
    timestamps = frame["sample_time_us"].to_numpy(dtype="int64")
    mids = frame["mid"].to_numpy(dtype="float64")
    bid_prices = frame[[f"bid_px_{level}" for level in range(1, 11)]].to_numpy(dtype="float64")
    bid_quantities = frame[[f"bid_qty_{level}" for level in range(1, 11)]].to_numpy(dtype="float64")
    ask_prices = frame[[f"ask_px_{level}" for level in range(1, 11)]].to_numpy(dtype="float64")
    ask_quantities = frame[[f"ask_qty_{level}" for level in range(1, 11)]].to_numpy(dtype="float64")
    busy_until = -1
    rows: list[dict[str, Any]] = []
    date = str(frame["date"].iat[0])

    signal_cursor = 0
    while signal_cursor < signal_indices.size:
        decision_index = int(signal_indices[signal_cursor])
        entry_index = decision_index + latency_steps
        exit_index = decision_index + horizon_steps
        if entry_index >= len(frame) or exit_index >= len(frame):
            counters.excluded_day_boundary += 1
            signal_cursor += 1
            continue
        segment = segments[decision_index]
        if (
            not np.isfinite(segment)
            or not valid_book[decision_index]
            or not valid_book[entry_index]
            or not valid_book[exit_index]
            or segments[entry_index] != segment
            or segments[exit_index] != segment
        ):
            counters.excluded_gap += 1
            signal_cursor += 1
            continue
        direction = 1 if prediction[decision_index] > 0 else -1
        entry_touch = (
            float(frame["best_ask_price"].iat[entry_index])
            if direction > 0
            else float(frame["best_bid_price"].iat[entry_index])
        )
        quantity = floor_quantity(notional_usdt, entry_touch, quantity_step)
        if quantity <= 0:
            counters.excluded_quantity += 1
            signal_cursor += 1
            continue

        entry_side = "buy" if direction > 0 else "sell"
        exit_side = "sell" if direction > 0 else "buy"
        zero_entry = _fill(
            bid_prices, bid_quantities, ask_prices, ask_quantities,
            decision_index, entry_side, quantity, tick_size
        )
        zero_exit = _fill(
            bid_prices, bid_quantities, ask_prices, ask_quantities,
            exit_index, exit_side, quantity, tick_size
        )
        actual_entry = _fill(
            bid_prices, bid_quantities, ask_prices, ask_quantities,
            entry_index, entry_side, quantity, tick_size
        )
        actual_exit = _fill(
            bid_prices, bid_quantities, ask_prices, ask_quantities,
            exit_index, exit_side, quantity, tick_size
        )
        if any(fill is None for fill in (zero_entry, zero_exit, actual_entry, actual_exit)):
            counters.excluded_depth += 1
            signal_cursor += 1
            continue
        assert zero_entry is not None and zero_exit is not None
        assert actual_entry is not None and actual_exit is not None

        max_visible_participation = max(
            actual_entry.visible_participation, actual_exit.visible_participation
        )
        unrealistic = max_visible_participation > unrealistic_participation_threshold
        counters.unrealistic_liquidity += int(unrealistic)
        layer0_ticks = direction * (mids[exit_index] - mids[decision_index]) / tick_size
        touch_entry = zero_entry.touch_price
        touch_exit = zero_exit.touch_price
        touch_ticks = _pnl_ticks(direction, touch_entry, touch_exit, tick_size)
        zero_gross_ticks = _pnl_ticks(
            direction, zero_entry.vwap_price, zero_exit.vwap_price, tick_size
        )
        actual_gross_ticks = _pnl_ticks(
            direction, actual_entry.vwap_price, actual_exit.vwap_price, tick_size
        )
        rows.append(
            {
                "date": date,
                "model": model_name,
                "horizon_ms": horizon_ms,
                "prediction_threshold_ticks": prediction_threshold_ticks,
                "latency_ms": latency_ms,
                "decision_time_us": int(timestamps[decision_index]),
                "entry_time_us": int(timestamps[entry_index]),
                "exit_time_us": int(timestamps[exit_index]),
                "feature_segment_id": int(segment),
                "direction": direction,
                "prediction_ticks": float(prediction[decision_index]),
                "quantity_btc": quantity,
                "decision_mid": float(mids[decision_index]),
                "exit_mid": float(mids[exit_index]),
                "zero_latency_entry_price": zero_entry.vwap_price,
                "actual_entry_price": actual_entry.vwap_price,
                "exit_price": actual_exit.vwap_price,
                "zero_latency_entry_touch": zero_entry.touch_price,
                "exit_touch": zero_exit.touch_price,
                "entry_notional_usdt": quantity * actual_entry.vwap_price,
                "exit_notional_usdt": quantity * actual_exit.vwap_price,
                "layer0_mid_ticks": layer0_ticks,
                "layer0_mid_pnl_usdt": quantity * tick_size * layer0_ticks,
                "touch_executable_ticks": touch_ticks,
                "layer1_gross_ticks": zero_gross_ticks,
                "layer1_gross_pnl_usdt": quantity * tick_size * zero_gross_ticks,
                "actual_gross_ticks": actual_gross_ticks,
                "actual_gross_pnl_usdt": quantity * tick_size * actual_gross_ticks,
                "spread_drag_ticks": layer0_ticks - touch_ticks,
                "depth_slippage_drag_ticks": touch_ticks - zero_gross_ticks,
                "latency_decay_ticks": zero_gross_ticks - actual_gross_ticks,
                "entry_spread_ticks": (
                    float(frame["best_ask_price"].iat[entry_index])
                    - float(frame["best_bid_price"].iat[entry_index])
                )
                / tick_size,
                "exit_spread_ticks": (
                    float(frame["best_ask_price"].iat[exit_index])
                    - float(frame["best_bid_price"].iat[exit_index])
                )
                / tick_size,
                "entry_consumed_levels": actual_entry.consumed_levels,
                "exit_consumed_levels": actual_exit.consumed_levels,
                "entry_touch_participation": actual_entry.touch_participation,
                "exit_touch_participation": actual_exit.touch_participation,
                "max_visible_participation": max_visible_participation,
                "unrealistic_liquidity": unrealistic,
            }
        )
        busy_until = exit_index
        next_cursor = int(np.searchsorted(signal_indices, busy_until, side="left"))
        counters.skipped_overlap += max(0, next_cursor - signal_cursor - 1)
        signal_cursor = max(signal_cursor + 1, next_cursor)

    counters.completed_trades = len(rows)
    return pd.DataFrame(rows), counters


def add_cost_layers(
    trades: pd.DataFrame,
    *,
    fee_bps_per_side: float,
    penalty_ticks_per_fill: float,
    tick_size: float,
) -> pd.DataFrame:
    result = trades.copy()
    if result.empty:
        for column in (
            "layer2_net_pnl_usdt",
            "layer2_net_ticks",
            "layer3_net_pnl_usdt",
            "layer3_net_ticks",
            "layer4_net_pnl_usdt",
            "layer4_net_ticks",
            "fee_drag_usdt",
            "fee_drag_ticks",
            "entry_fee_usdt",
            "exit_fee_usdt",
            "zero_latency_entry_fee_usdt",
            "zero_latency_exit_fee_usdt",
            "spread_drag_usdt",
            "depth_slippage_drag_usdt",
            "latency_decay_usdt",
            "stress_penalty_drag_usdt",
            "layer3_net_bps_on_round_trip_turnover",
            "layer4_net_bps_on_round_trip_turnover",
            "stress_penalty_drag_ticks",
            "break_even_fee_bps_per_side",
        ):
            result[column] = pd.Series(dtype="float64")
        return result
    fee_rate = fee_bps_per_side / 10_000.0
    quantity = result["quantity_btc"]
    direction = result["direction"]

    zero_entry_notional = quantity * result["zero_latency_entry_price"]
    zero_exit_notional = quantity * result["exit_price"]
    result["zero_latency_entry_fee_usdt"] = zero_entry_notional * fee_rate
    result["zero_latency_exit_fee_usdt"] = zero_exit_notional * fee_rate
    zero_fee = result["zero_latency_entry_fee_usdt"] + result["zero_latency_exit_fee_usdt"]
    result["layer2_net_pnl_usdt"] = result["layer1_gross_pnl_usdt"] - zero_fee
    result["layer2_net_ticks"] = result["layer2_net_pnl_usdt"] / (quantity * tick_size)

    result["entry_fee_usdt"] = result["entry_notional_usdt"] * fee_rate
    result["exit_fee_usdt"] = result["exit_notional_usdt"] * fee_rate
    actual_fee = result["entry_fee_usdt"] + result["exit_fee_usdt"]
    result["fee_drag_usdt"] = actual_fee
    result["fee_drag_ticks"] = actual_fee / (quantity * tick_size)
    result["layer3_net_pnl_usdt"] = result["actual_gross_pnl_usdt"] - actual_fee
    result["layer3_net_ticks"] = result["layer3_net_pnl_usdt"] / (quantity * tick_size)

    stress_entry = result["actual_entry_price"] + direction * penalty_ticks_per_fill * tick_size
    stress_exit = result["exit_price"] - direction * penalty_ticks_per_fill * tick_size
    stress_gross = direction * (stress_exit - stress_entry) * quantity
    stress_fee = quantity * (stress_entry + stress_exit) * fee_rate
    result["stress_penalty_drag_ticks"] = result["actual_gross_ticks"] - (
        direction * (stress_exit - stress_entry) / tick_size
    )
    result["spread_drag_usdt"] = result["spread_drag_ticks"] * quantity * tick_size
    result["depth_slippage_drag_usdt"] = (
        result["depth_slippage_drag_ticks"] * quantity * tick_size
    )
    result["latency_decay_usdt"] = result["latency_decay_ticks"] * quantity * tick_size
    result["stress_penalty_drag_usdt"] = (
        result["stress_penalty_drag_ticks"] * quantity * tick_size
    )
    result["layer4_net_pnl_usdt"] = stress_gross - stress_fee
    result["layer4_net_ticks"] = result["layer4_net_pnl_usdt"] / (quantity * tick_size)
    turnover = result["entry_notional_usdt"] + result["exit_notional_usdt"]
    result["layer3_net_bps_on_round_trip_turnover"] = (
        result["layer3_net_pnl_usdt"] / turnover * 10_000.0
    )
    result["layer4_net_bps_on_round_trip_turnover"] = (
        result["layer4_net_pnl_usdt"] / turnover * 10_000.0
    )
    result["break_even_fee_bps_per_side"] = (
        result["actual_gross_pnl_usdt"] / turnover * 10_000.0
    )
    return result


def _profit_factor(values: np.ndarray) -> float:
    gains = float(values[values > 0].sum())
    losses = float(-values[values < 0].sum())
    if losses == 0:
        return math.inf if gains > 0 else math.nan
    return gains / losses


def performance_metrics(
    trades: pd.DataFrame,
    *,
    pnl_usdt_column: str,
    pnl_ticks_column: str,
    primary_notional_usdt: float,
) -> dict[str, float | int]:
    if trades.empty:
        return {
            "trades": 0,
            "total_pnl_usdt": 0.0,
            "average_pnl_usdt": math.nan,
            "median_pnl_usdt": math.nan,
            "total_pnl_ticks": 0.0,
            "average_pnl_ticks": math.nan,
            "median_pnl_ticks": math.nan,
            "win_rate": math.nan,
            "profit_factor": math.nan,
            "turnover_usdt": 0.0,
            "turnover_multiple": 0.0,
            "total_pnl_per_dollar_turnover": math.nan,
            "total_pnl_bps_on_round_trip_turnover": math.nan,
            "total_pnl_per_primary_notional": 0.0,
            "trades_per_day": 0.0,
            "exposure_seconds": 0.0,
            "max_drawdown_usdt": 0.0,
            "average_drawdown_usdt": 0.0,
            "worst_trade_usdt": math.nan,
            "best_trade_usdt": math.nan,
        }
    pnl = trades[pnl_usdt_column].to_numpy(dtype="float64")
    ticks = trades[pnl_ticks_column].to_numpy(dtype="float64")
    cumulative = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate(([0.0], cumulative)))[1:]
    drawdown = cumulative - peak
    turnover = float((trades["entry_notional_usdt"] + trades["exit_notional_usdt"]).sum())
    exposure_seconds = float(
        ((trades["exit_time_us"] - trades["decision_time_us"]) / 1_000_000.0).sum()
    )
    return {
        "trades": int(len(trades)),
        "total_pnl_usdt": float(pnl.sum()),
        "average_pnl_usdt": float(pnl.mean()),
        "median_pnl_usdt": float(np.median(pnl)),
        "total_pnl_ticks": float(ticks.sum()),
        "average_pnl_ticks": float(ticks.mean()),
        "median_pnl_ticks": float(np.median(ticks)),
        "win_rate": float(np.mean(pnl > 0)),
        "profit_factor": _profit_factor(pnl),
        "turnover_usdt": turnover,
        "turnover_multiple": turnover / primary_notional_usdt,
        "total_pnl_per_dollar_turnover": float(pnl.sum()) / turnover if turnover else math.nan,
        "total_pnl_bps_on_round_trip_turnover": (
            float(pnl.sum()) / turnover * 10_000.0 if turnover else math.nan
        ),
        "total_pnl_per_primary_notional": float(pnl.sum()) / primary_notional_usdt,
        "trades_per_day": float(len(trades) / trades["date"].nunique()),
        "exposure_seconds": exposure_seconds,
        "max_drawdown_usdt": float(-drawdown.min()),
        "average_drawdown_usdt": float(-drawdown.mean()),
        "worst_trade_usdt": float(pnl.min()),
        "best_trade_usdt": float(pnl.max()),
    }


def daily_sharpe(
    daily_pnl: pd.Series,
    *,
    primary_notional_usdt: float,
    bootstrap_samples: int,
    bootstrap_seed: int,
) -> dict[str, float | int | None]:
    returns = daily_pnl.to_numpy(dtype="float64") / primary_notional_usdt
    count = len(returns)
    mean = float(np.mean(returns)) if count else math.nan
    volatility = float(np.std(returns, ddof=1)) if count > 1 else math.nan
    sharpe = mean / volatility if count > 1 and volatility > 0 else math.nan
    annualized = sharpe * math.sqrt(365.0) if np.isfinite(sharpe) else math.nan
    lower: float | None = None
    upper: float | None = None
    if count >= 5 and bootstrap_samples > 0:
        rng = np.random.default_rng(bootstrap_seed)
        samples = rng.choice(returns, size=(bootstrap_samples, count), replace=True)
        sample_std = samples.std(axis=1, ddof=1)
        valid = sample_std > 0
        boot = samples.mean(axis=1)[valid] / sample_std[valid]
        if boot.size:
            lower, upper = [float(value) for value in np.quantile(boot, [0.025, 0.975])]
    return {
        "observed_days": count,
        "daily_mean_return": mean,
        "daily_volatility": volatility,
        "sample_daily_sharpe": sharpe,
        "annualized_daily_sharpe_exploratory": annualized,
        "bootstrap_95pct_daily_sharpe_lower": lower,
        "bootstrap_95pct_daily_sharpe_upper": upper,
    }
