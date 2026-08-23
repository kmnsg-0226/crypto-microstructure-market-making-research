"""Deterministically sample passive probes and verify them against raw trades/L2 rows."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.support.evaluate import write_csv, write_json


from pyresearch import ROOT
SEED = 20260816


def _sample_categories(frame: pd.DataFrame, per_category: int) -> pd.DataFrame:
    categories = {
        "filled_bid": frame["side"].eq("bid") & frame["fill_status"].eq("full"),
        "filled_ask": frame["side"].eq("ask") & frame["fill_status"].eq("full"),
        "unfilled_bid": frame["side"].eq("bid") & frame["fill_status"].eq("unfilled"),
        "unfilled_ask": frame["side"].eq("ask") & frame["fill_status"].eq("unfilled"),
        "partial_fill": frame["fill_status"].str.startswith("partial", na=False),
        "trade_through": frame["fill_reason"].eq("trade_through"),
        "snapshot_or_gap_invalid": frame["invalid_due_to_snapshot"].eq(1),
    }
    parts = []
    for offset, (category, mask) in enumerate(categories.items()):
        available = frame.loc[mask]
        if available.empty:
            continue
        selected = available.sample(
            n=min(per_category, len(available)),
            random_state=SEED + offset,
            replace=False,
        ).copy()
        selected.insert(0, "audit_category", category)
        parts.append(selected)
    if not parts:
        raise ValueError("no passive probes available for manual audit")
    result = pd.concat(parts, ignore_index=True)
    result.insert(0, "audit_id", np.arange(1, len(result) + 1))
    return result


def _qualifying_trade(row: pd.Series, trade: pd.Series) -> tuple[bool, bool]:
    if row.side == "bid":
        if trade.side != "sell":
            return False, False
        return trade.price == row.quote_price, trade.price < row.quote_price
    if trade.side != "buy":
        return False, False
    return trade.price == row.quote_price, trade.price > row.quote_price


def _replay_sample(row: pd.Series, trades: pd.DataFrame) -> dict[str, Any]:
    start = int(row.placement_local_time_us)
    end = int(row.expiry_local_time_us)
    if pd.notna(row.next_snapshot_local_time_us):
        snapshot = int(row.next_snapshot_local_time_us)
        if start < snapshot < end:
            end = snapshot
    local = trades["local_timestamp"].to_numpy(dtype="int64")
    begin = int(np.searchsorted(local, start, side="right"))
    finish = int(np.searchsorted(local, end, side="left"))
    window = trades.iloc[begin:finish]
    queue = float(row.queue_ahead_initial) if pd.notna(row.queue_ahead_initial) else 0.0
    order_qty = float(row.quote_qty)
    filled = 0.0
    at_price_qty = 0.0
    relevant = []
    fill_reason = ""
    first_fill_local: int | None = None
    first_fill_exchange: int | None = None
    full_fill_local: int | None = None
    full_fill_exchange: int | None = None
    for trade in window.itertuples(index=False):
        same, through = _qualifying_trade(row, pd.Series(trade._asdict()))
        if not same and not through:
            continue
        before_queue = queue
        before_fill = filled
        if through:
            queue = 0.0
            filled = order_qty
            fill_reason = "trade_through"
        else:
            at_price_qty += float(trade.amount)
            consumed = min(queue, float(trade.amount))
            queue -= consumed
            filled += min(order_qty - filled, float(trade.amount) - consumed)
            if filled > before_fill and not fill_reason:
                fill_reason = "same_price_aggressor_trade"
        if filled > before_fill and first_fill_local is None:
            first_fill_local = int(trade.local_timestamp)
            first_fill_exchange = int(trade.timestamp)
        if filled >= order_qty - 1e-12 and full_fill_local is None:
            full_fill_local = int(trade.local_timestamp)
            full_fill_exchange = int(trade.timestamp)
        if len(relevant) < 50:
            relevant.append(
                {
                    "exchange_time_us": int(trade.timestamp),
                    "local_time_us": int(trade.local_timestamp),
                    "trade_id": str(trade.id),
                    "aggressor_side": trade.side,
                    "price": float(trade.price),
                    "quantity": float(trade.amount),
                    "same_price": bool(same),
                    "trade_through": bool(through),
                    "queue_before": before_queue,
                    "queue_after": queue,
                    "filled_before": before_fill,
                    "filled_after": filled,
                }
            )
        if filled >= order_qty - 1e-12:
            break
    expected = min(order_qty, filled)
    simulator_first = (
        None if pd.isna(row.first_fill_local_time_us) else int(row.first_fill_local_time_us)
    )
    simulator_first_exchange = (
        None
        if pd.isna(row.first_fill_exchange_time_us)
        else int(row.first_fill_exchange_time_us)
    )
    simulator_full = (
        None if pd.isna(row.full_fill_local_time_us) else int(row.full_fill_local_time_us)
    )
    simulator_full_exchange = (
        None
        if pd.isna(row.full_fill_exchange_time_us)
        else int(row.full_fill_exchange_time_us)
    )
    simulator_reason = None if pd.isna(row.fill_reason) else str(row.fill_reason)
    recomputed_reason = fill_reason or None
    return {
        "recomputed_filled_qty": expected,
        "simulator_filled_qty": float(row.filled_qty),
        "filled_qty_match": abs(expected - float(row.filled_qty)) < 1e-9,
        "recomputed_first_fill_local_time_us": first_fill_local,
        "simulator_first_fill_local_time_us": simulator_first,
        "first_fill_local_time_match": first_fill_local == simulator_first,
        "recomputed_first_fill_exchange_time_us": first_fill_exchange,
        "simulator_first_fill_exchange_time_us": simulator_first_exchange,
        "first_fill_exchange_time_match": first_fill_exchange == simulator_first_exchange,
        "recomputed_full_fill_local_time_us": full_fill_local,
        "simulator_full_fill_local_time_us": simulator_full,
        "full_fill_local_time_match": full_fill_local == simulator_full,
        "recomputed_full_fill_exchange_time_us": full_fill_exchange,
        "simulator_full_fill_exchange_time_us": simulator_full_exchange,
        "full_fill_exchange_time_match": full_fill_exchange == simulator_full_exchange,
        "recomputed_fill_reason": recomputed_reason,
        "simulator_fill_reason": simulator_reason,
        "fill_reason_match": recomputed_reason == simulator_reason,
        "same_price_traded_qty_before_resolution": at_price_qty,
        "relevant_trade_events_total": sum(
            any(_qualifying_trade(row, pd.Series(item._asdict())))
            for item in window.itertuples(index=False)
        ),
        "relevant_trade_events_shown": relevant,
    }


def _collect_l2_traces(
    samples: pd.DataFrame,
    l2_path: Path,
    *,
    chunksize: int = 1_000_000,
) -> dict[int, list[dict[str, Any]]]:
    traces = {int(value): [] for value in samples["audit_id"]}
    windows = []
    for row in samples.itertuples(index=False):
        end = int(row.expiry_local_time_us)
        if pd.notna(row.next_snapshot_local_time_us):
            snapshot = int(row.next_snapshot_local_time_us)
            if row.placement_local_time_us < snapshot < end:
                end = snapshot + 1
        windows.append((
            int(row.audit_id),
            int(row.placement_local_time_us),
            end,
            float(row.quote_price) if pd.notna(row.quote_price) else np.nan,
        ))
    for chunk in pd.read_csv(
        l2_path,
        compression="gzip",
        usecols=["timestamp", "local_timestamp", "is_snapshot", "side", "price", "amount"],
        chunksize=chunksize,
    ):
        local = chunk["local_timestamp"].to_numpy(dtype="int64")
        for audit_id, start, end, quote in windows:
            if len(traces[audit_id]) >= 50 or local[-1] <= start or local[0] >= end:
                continue
            begin = int(np.searchsorted(local, start, side="right"))
            finish = int(np.searchsorted(local, end, side="left"))
            if begin >= finish:
                continue
            selected = chunk.iloc[begin:finish]
            selected = selected[
                selected["is_snapshot"].eq(True)
                | (np.isfinite(quote) & selected["price"].eq(quote))
            ]
            remaining = 50 - len(traces[audit_id])
            for event in selected.head(remaining).itertuples(index=False):
                traces[audit_id].append(
                    {
                        "exchange_time_us": int(event.timestamp),
                        "local_time_us": int(event.local_timestamp),
                        "is_snapshot": bool(event.is_snapshot),
                        "side": event.side,
                        "price": float(event.price),
                        "new_displayed_quantity": float(event.amount),
                        "queue_effect_under_pessimistic_model": "ignored",
                    }
                )
    return traces


def build_manual_audit(
    labeled: Path,
    trades_path: Path,
    l2_path: Path,
    output_json: Path,
    output_samples: Path,
    *,
    per_category: int = 3,
) -> dict[str, Any]:
    columns = [
        "date", "decision_time_us", "placement_local_time_us", "side", "quote_price",
        "quote_qty", "best_bid_at_entry", "best_ask_at_entry", "queue_ahead_initial",
        "quote_lifetime_ms", "expiry_local_time_us", "next_snapshot_local_time_us",
        "fill_status", "first_fill_exchange_time_us", "first_fill_local_time_us",
        "full_fill_exchange_time_us", "full_fill_local_time_us", "filled_qty", "fill_reason",
        "cancel_reason", "invalid_due_to_gap", "invalid_due_to_snapshot",
        "fill_grid_time_us", "mid_at_fill_grid", "maker_markout_100ms_ticks",
        "maker_markout_1s_ticks", "post_fill_mid_move_1s_ticks",
    ]
    frame = pd.read_parquet(labeled, columns=columns)
    samples = _sample_categories(frame, per_category)
    write_csv(output_samples, samples)
    trades = pd.read_csv(
        trades_path,
        compression="gzip",
        usecols=["timestamp", "local_timestamp", "id", "side", "price", "amount"],
    )
    if not trades["local_timestamp"].is_monotonic_increasing:
        raise ValueError("raw trade local timestamps regress during manual audit")
    l2_traces = _collect_l2_traces(samples, l2_path)
    examples = []
    for row in samples.itertuples(index=False):
        series = pd.Series(row._asdict())
        replay = _replay_sample(series, trades)
        examples.append(
            {
                "audit_id": int(row.audit_id),
                "category": row.audit_category,
                "placement": {
                    "decision_time_us": int(row.decision_time_us),
                    "placement_local_time_us": int(row.placement_local_time_us),
                    "side": row.side,
                    "fixed_quote_price": None if pd.isna(row.quote_price) else float(row.quote_price),
                    "quote_qty": float(row.quote_qty),
                    "queue_ahead_initial": (
                        None if pd.isna(row.queue_ahead_initial) else float(row.queue_ahead_initial)
                    ),
                    "lifetime_ms": int(row.quote_lifetime_ms),
                },
                "simulator_outcome": {
                    "fill_status": row.fill_status,
                    "fill_reason": None if pd.isna(row.fill_reason) else row.fill_reason,
                    "cancel_reason": None if pd.isna(row.cancel_reason) else row.cancel_reason,
                    "filled_qty": float(row.filled_qty),
                    "maker_markout_100ms_ticks": (
                        None if pd.isna(row.maker_markout_100ms_ticks)
                        else float(row.maker_markout_100ms_ticks)
                    ),
                    "maker_markout_1s_ticks": (
                        None if pd.isna(row.maker_markout_1s_ticks)
                        else float(row.maker_markout_1s_ticks)
                    ),
                    "post_fill_mid_move_1s_ticks": (
                        None if pd.isna(row.post_fill_mid_move_1s_ticks)
                        else float(row.post_fill_mid_move_1s_ticks)
                    ),
                    "fill_grid_time_us": (
                        None if pd.isna(row.fill_grid_time_us) else int(row.fill_grid_time_us)
                    ),
                    "mid_at_fill_grid": (
                        None if pd.isna(row.mid_at_fill_grid) else float(row.mid_at_fill_grid)
                    ),
                    "future_mid_1s": (
                        None
                        if pd.isna(row.maker_markout_1s_ticks) or pd.isna(row.quote_price)
                        else float(row.quote_price)
                        + (1.0 if row.side == "bid" else -1.0)
                        * float(row.maker_markout_1s_ticks)
                        * 0.1
                    ),
                },
                "manual_trade_replay": replay,
                "relevant_l2_rows_shown": l2_traces[int(row.audit_id)],
            }
        )
    checks = (
        "filled_qty_match",
        "first_fill_local_time_match",
        "first_fill_exchange_time_match",
        "full_fill_local_time_match",
        "full_fill_exchange_time_match",
        "fill_reason_match",
    )
    consistent = sum(
        all(item["manual_trade_replay"][check] for check in checks)
        for item in examples
    )
    payload = {
        "schema": "passive-manual-fill-audit-v1",
        "seed": SEED,
        "sample_count": len(examples),
        "consistent_examples": consistent,
        "checks_per_example": list(checks),
        "all_fill_fields_match": consistent == len(examples),
        "l2_queue_reduction_policy": "shown_for_audit_but_ignored_by_pessimistic_queue",
        "examples": examples,
    }
    write_json(output_json, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labeled", type=Path, required=True)
    parser.add_argument("--trades", type=Path, required=True)
    parser.add_argument("--l2", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-samples", type=Path, required=True)
    parser.add_argument("--per-category", type=int, default=3)
    args = parser.parse_args()
    result = build_manual_audit(
        args.labeled,
        args.trades,
        args.l2,
        args.output_json,
        args.output_samples,
        per_category=args.per_category,
    )
    print(json.dumps({key: result[key] for key in (
        "sample_count", "consistent_examples", "all_fill_fields_match"
    )}, sort_keys=True))


if __name__ == "__main__":
    main()
