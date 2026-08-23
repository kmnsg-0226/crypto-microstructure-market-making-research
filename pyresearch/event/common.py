"""Common selectors, features, and unchanged continuous-MM economics."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from pyresearch.obi.continuous_mm import simulate_schedule


from pyresearch import ROOT
PLAN_PATH = ROOT / "research/specs/event_model_comparison_plan.json"
DATA_ROOT = ROOT / "data/research/tardis/event_models"
GRID_ROOT = ROOT / "data/research/tardis"
WINDOWS_MS = (10, 50, 100, 500, 1000)

STATIC_FEATURES = [
    "side_obi_l1", "side_obi_l5", "side_obi_l10", "side_weighted_obi_l10",
    "side_weighted_mid_minus_mid_ticks", "spread_ticks", "bid_depth_l1_lots",
    "ask_depth_l1_lots", "bid_depth_l5_lots", "ask_depth_l5_lots",
    "bid_depth_l10_lots", "ask_depth_l10_lots",
]
QUEUE_FEATURES = ["queue_ahead_lots", "queue_to_l1_ratio"]
BOOK_PER_WINDOW = [
    "side_delta_obi_l10", "side_ofi_lots", "side_add_lots", "side_cancel_lots",
    "side_depletion_lots", "side_replenishment_lots", "bbo_change_count",
    "depth_change_count",
]
TRADE_PER_WINDOW = [
    "side_trade_qty_imbalance", "side_trade_count_imbalance", "trade_intensity",
    "average_trade_size_lots", "side_last_trade_streak",
]
FLOW_FEATURES = [
    f"{name}_{window}ms"
    for window in WINDOWS_MS
    for name in BOOK_PER_WINDOW + TRADE_PER_WINDOW
] + [
    "time_since_trade_ms", "time_since_book_ms", "time_since_bbo_change_ms",
    "time_since_mid_change_ms", "event_arrival_intensity_100ms",
    "backward_mid_return_abs_sum_100ms", "backward_mid_return_abs_sum_1000ms",
]
FULL_FEATURES = STATIC_FEATURES + FLOW_FEATURES + QUEUE_FEATURES
EVENT_RULE_VOTES = [
    "side_obi_l10",
    "side_delta_obi_l10_100ms",
    "side_ofi_lots_100ms",
    "side_trade_qty_imbalance_100ms",
    "side_weighted_mid_minus_mid_ticks",
    "side_depletion_lots_100ms",
]


@dataclass(frozen=True)
class QuoteDecision:
    quote: bool
    score: float
    predicted_fill_probability: float | None
    predicted_markout_ticks: float | None
    model_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_plan() -> dict[str, Any]:
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    if plan["status"] != "frozen_before_event_model_profitability":
        raise ValueError("event comparison plan is not frozen")
    return plan


def load_day(date: str, columns: Iterable[str] | None = None) -> pd.DataFrame:
    return pd.read_parquet(
        DATA_ROOT / date / "labeled_events.parquet",
        columns=list(columns) if columns is not None else None,
    )


def event_rule_score(frame: pd.DataFrame) -> np.ndarray:
    votes = np.zeros(len(frame), dtype="int8")
    for feature in EVENT_RULE_VOTES:
        votes += (frame[feature].to_numpy(dtype="float64") > 0).astype("int8")
    return votes.astype("float64") / len(EVENT_RULE_VOTES)


def event_rule_decisions(frame: pd.DataFrame, vote_threshold: int) -> list[QuoteDecision]:
    if vote_threshold not in (3, 4, 5):
        raise ValueError("vote threshold is outside the frozen set")
    score = event_rule_score(frame)
    return [
        QuoteDecision(
            quote=bool(value * 6 >= vote_threshold),
            score=float(value),
            predicted_fill_probability=None,
            predicted_markout_ticks=None,
            model_id=f"event_rule_{vote_threshold}_of_6",
        )
        for value in score
    ]


def _simulator_spec() -> dict[str, Any]:
    plan = load_plan()
    execution = plan["execution"]
    return {
        "market": {"quote_qty_btc": execution["quote_quantity_btc"]},
        "inventory": {
            **execution["inventory"],
            "quantity_tolerance_btc": 1e-9,
        },
        "costs": {
            "maker_fee_bps": execution["fees"]["maker_bps"],
            "day_end_taker_fee_bps": execution["fees"]["forced_liquidation_taker_bps"],
        },
        "policies": {"obi_aware": {"absolute_threshold": 0.8}},
    }


def prepare_schedule(frame: pd.DataFrame, selected: np.ndarray) -> pd.DataFrame:
    if len(selected) != len(frame):
        raise ValueError("selector output length mismatch")
    schedule = pd.DataFrame({
        "date": frame["date"],
        "decision_time_us": frame["decision_local_time_us"].astype("int64"),
        "placement_local_time_us": frame["placement_local_time_us"].astype("int64"),
        "feature_segment_id": frame["feature_segment_id"].astype("int64"),
        "side": frame["side"],
        "quote_price": frame["quote_price"].astype("float64"),
        "quote_qty": frame["quote_qty"].astype("float64"),
        "quote_lifetime_ms": 1000,
        "expiry_local_time_us": frame["expiry_local_time_us"].astype("int64"),
        "weighted_obi_l10": (
            frame["side_weighted_obi_l10"].to_numpy(dtype="float64")
            * frame["quote_side"].to_numpy(dtype="float64")
        ),
        "fill_status": frame["fill_status"],
        "first_fill_local_time_us": frame["first_fill_local_time_us"],
        "full_fill_local_time_us": frame["full_fill_local_time_us"],
        "filled_qty": frame["filled_qty"].astype("float64"),
        "selected": np.asarray(selected, dtype="bool"),
    })
    schedule.sort_values(
        ["placement_local_time_us", "decision_time_us", "side"],
        kind="stable",
        inplace=True,
        ignore_index=True,
    )
    return schedule


def final_bbo(date: str) -> tuple[float, float]:
    grid = pd.read_parquet(
        GRID_ROOT / date / "features_100ms.parquet",
        columns=["valid_book_state", "best_bid_price", "best_ask_price"],
    )
    valid = (
        grid["valid_book_state"].eq(1)
        & np.isfinite(grid["best_bid_price"])
        & np.isfinite(grid["best_ask_price"])
    )
    if not valid.any():
        raise ValueError(f"{date} has no final valid BBO")
    row = grid.loc[valid].iloc[-1]
    return float(row["best_bid_price"]), float(row["best_ask_price"])


def simulate_selected_day(
    frame: pd.DataFrame,
    *,
    date: str,
    model_id: str,
    selected: np.ndarray,
    obi_policy: bool = False,
) -> dict[str, Any]:
    schedule = prepare_schedule(frame, selected)
    bid, ask = final_bbo(date)
    result = simulate_schedule(
        schedule,
        date=date,
        policy="obi_aware" if obi_policy else "neutral",
        last_bid=bid,
        last_ask=ask,
        spec=_simulator_spec(),
        selection_column=None if obi_policy else "selected",
        risk_reducing_override=not obi_policy,
    )
    result["policy"] = model_id
    return result


def aggregate_economics(day_metrics: pd.DataFrame) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for policy, group in day_metrics.groupby("policy", sort=True):
        group = group.sort_values("date", kind="stable")
        one_way = float(group["one_way_notional_usdt"].sum())
        maker_turnover = float(group["maker_turnover_usdt"].sum())
        gross = float(group["gross_pnl_usdt"].sum())
        liquidation_fees = float(group["forced_liquidation_fee_usdt"].sum())
        net = float(group["net_pnl_usdt"].sum())
        daily = group["net_pnl_usdt"].to_numpy(dtype="float64")
        cumulative = np.cumsum(daily)
        drawdown = np.maximum.accumulate(np.r_[0.0, cumulative]) - np.r_[0.0, cumulative]
        standard_deviation = float(np.std(daily, ddof=1)) if len(daily) > 1 else np.nan
        output[str(policy)] = {
            "dates": int(group["date"].nunique()),
            "gross_pnl_usdt": gross,
            "net_pnl_usdt": net,
            "net_bps_on_one_way_notional": net / one_way * 10_000 if one_way else np.nan,
            "fees_usdt": float(
                group["maker_fees_usdt"].sum() + group["forced_liquidation_fee_usdt"].sum()
            ),
            "positive_days": int((group["net_pnl_usdt"] > 0).sum()),
            "worst_day_net_pnl_usdt": float(group["net_pnl_usdt"].min()),
            "maker_fill_orders": int(group["maker_fill_orders"].sum()),
            "fills_per_day": float(group["maker_fill_orders"].mean()),
            "turnover_usdt": float(group["total_turnover_usdt"].sum()),
            "maximum_absolute_inventory_btc": float(group["maximum_absolute_inventory_btc"].max()),
            "inventory_limit_violations": int(group["inventory_limit_violations"].sum()),
            "maximum_drawdown_usdt_from_daily_curve": float(np.max(drawdown, initial=0.0)),
            "exploratory_daily_sharpe": (
                float(np.mean(daily) / standard_deviation * np.sqrt(365.0))
                if np.isfinite(standard_deviation) and standard_deviation > 0 else np.nan
            ),
            "break_even_maker_fee_bps": (
                (gross - liquidation_fees) / maker_turnover * 10_000
                if maker_turnover else np.nan
            ),
        }
    return output
