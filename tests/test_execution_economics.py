import math
import unittest

import numpy as np
import pandas as pd

from pyresearch.execution.engine import (
    add_cost_layers,
    daily_sharpe,
    floor_quantity,
    performance_metrics,
    run_day,
    walk_visible_book,
)


def synthetic_book(rows: int = 20) -> pd.DataFrame:
    data: dict[str, object] = {
        "date": ["2026-01-01"] * rows,
        "sample_time_us": np.arange(rows, dtype=np.int64) * 100_000,
        "valid_book_state": np.ones(rows, dtype=int),
        "feature_segment_id": pd.Series(np.ones(rows), dtype="Int64"),
        "mid": np.full(rows, 100.5),
        "best_bid_price": np.full(rows, 100.0),
        "best_bid_qty": np.full(rows, 10.0),
        "best_ask_price": np.full(rows, 101.0),
        "best_ask_qty": np.full(rows, 10.0),
    }
    for level in range(1, 11):
        data[f"bid_px_{level}"] = np.full(rows, 101.0 - level)
        data[f"bid_qty_{level}"] = np.full(rows, 10.0)
        data[f"ask_px_{level}"] = np.full(rows, 100.0 + level)
        data[f"ask_qty_{level}"] = np.full(rows, 10.0)
    return pd.DataFrame(data)


class ExecutionEconomicsTest(unittest.TestCase):
    def test_buy_ask_sell_bid_and_multilevel_vwap(self) -> None:
        buy = walk_visible_book(
            np.array([101.0, 102.0, 103.0]),
            np.array([1.0, 1.0, 1.0]),
            1.5,
            "buy",
            1.0,
        )
        self.assertIsNotNone(buy)
        assert buy is not None
        self.assertAlmostEqual(buy.vwap_price, (101.0 + 0.5 * 102.0) / 1.5)
        self.assertEqual(buy.consumed_levels, 2)
        self.assertGreater(buy.depth_slippage_ticks, 0)
        sell = walk_visible_book(
            np.array([100.0, 99.0]), np.array([1.0, 1.0]), 1.5, "sell", 1.0
        )
        self.assertIsNotNone(sell)
        assert sell is not None
        self.assertAlmostEqual(sell.vwap_price, (100.0 + 0.5 * 99.0) / 1.5)
        self.assertIsNone(
            walk_visible_book(
                np.array([101.0]), np.array([1.0]), 2.0, "buy", 1.0
            )
        )

    def test_long_entry_ask_exit_bid_and_spread_not_double_counted(self) -> None:
        frame = synthetic_book()
        prediction = np.full(len(frame), np.nan)
        prediction[0] = 10.0
        trades, counters = run_day(
            frame,
            prediction,
            model_name="obi_only",
            horizon_ms=500,
            prediction_threshold_ticks=5.0,
            latency_ms=0,
            notional_usdt=101.0,
            tick_size=1.0,
            quantity_step=1.0,
            unrealistic_participation_threshold=0.25,
        )
        self.assertEqual(counters.completed_trades, 1)
        trade = trades.iloc[0]
        self.assertEqual(trade.actual_entry_price, 101.0)
        self.assertEqual(trade.exit_price, 100.0)
        self.assertEqual(trade.layer0_mid_ticks, 0.0)
        self.assertEqual(trade.layer1_gross_ticks, -1.0)
        self.assertEqual(trade.spread_drag_ticks, 1.0)
        costed = add_cost_layers(trades, fee_bps_per_side=0, penalty_ticks_per_fill=0, tick_size=1)
        self.assertEqual(costed.iloc[0].layer2_net_pnl_usdt, -1.0)
        self.assertEqual(costed.iloc[0].layer3_net_pnl_usdt, -1.0)

    def test_short_entry_bid_and_opposite_ask_exit(self) -> None:
        frame = synthetic_book()
        prediction = np.full(len(frame), np.nan)
        prediction[0] = -10.0
        trades, _ = run_day(
            frame,
            prediction,
            model_name="obi_only",
            horizon_ms=500,
            prediction_threshold_ticks=5,
            latency_ms=0,
            notional_usdt=105,
            tick_size=1,
            quantity_step=1,
            unrealistic_participation_threshold=0.25,
        )
        trade = trades.iloc[0]
        self.assertEqual(trade.actual_entry_price, 100.0)
        self.assertEqual(trade.exit_price, 101.0)
        self.assertEqual(trade.layer1_gross_ticks, -1.0)

    def test_fee_penalty_pnl_and_break_even(self) -> None:
        frame = synthetic_book()
        frame.loc[5:, "best_bid_price"] = 103.0
        frame.loc[5:, "best_ask_price"] = 104.0
        frame.loc[5:, "mid"] = 103.5
        for level in range(1, 11):
            frame.loc[5:, f"bid_px_{level}"] = 104.0 - level
            frame.loc[5:, f"ask_px_{level}"] = 103.0 + level
        prediction = np.full(len(frame), np.nan)
        prediction[0] = 10.0
        trades, _ = run_day(
            frame,
            prediction,
            model_name="combined",
            horizon_ms=500,
            prediction_threshold_ticks=5,
            latency_ms=0,
            notional_usdt=101,
            tick_size=1,
            quantity_step=1,
            unrealistic_participation_threshold=0.25,
        )
        costed = add_cost_layers(trades, fee_bps_per_side=5, penalty_ticks_per_fill=1, tick_size=1)
        trade = costed.iloc[0]
        self.assertEqual(trade.actual_gross_ticks, 2.0)
        expected_fee = (101.0 + 103.0) * 0.0005
        self.assertAlmostEqual(trade.fee_drag_usdt, expected_fee)
        self.assertAlmostEqual(trade.layer3_net_pnl_usdt, 2.0 - expected_fee)
        self.assertAlmostEqual(trade.stress_penalty_drag_ticks, 2.0)
        self.assertLess(trade.layer4_net_pnl_usdt, trade.layer3_net_pnl_usdt)
        self.assertAlmostEqual(trade.break_even_fee_bps_per_side, 2 / 204 * 10_000)

    def test_latency_uses_later_quote_without_lookahead(self) -> None:
        frame = synthetic_book()
        frame.loc[1, "best_bid_price"] = 104.0
        frame.loc[1, "best_ask_price"] = 105.0
        frame.loc[1, "mid"] = 104.5
        for level in range(1, 11):
            frame.loc[1, f"bid_px_{level}"] = 105.0 - level
            frame.loc[1, f"ask_px_{level}"] = 104.0 + level
        prediction = np.full(len(frame), np.nan)
        prediction[0] = 10.0
        trades, _ = run_day(
            frame,
            prediction,
            model_name="obi_only",
            horizon_ms=500,
            prediction_threshold_ticks=5,
            latency_ms=25,
            notional_usdt=105,
            tick_size=1,
            quantity_step=1,
            unrealistic_participation_threshold=0.25,
        )
        trade = trades.iloc[0]
        self.assertEqual(trade.decision_time_us, 0)
        self.assertEqual(trade.entry_time_us, 100_000)
        self.assertEqual(trade.actual_entry_price, 105.0)
        self.assertEqual(trade.zero_latency_entry_price, 101.0)

    def test_gap_rejection_fixed_exit_and_overlap_rule(self) -> None:
        frame = synthetic_book()
        prediction = np.full(len(frame), 10.0)
        trades, counters = run_day(
            frame,
            prediction,
            model_name="obi_only",
            horizon_ms=500,
            prediction_threshold_ticks=5,
            latency_ms=0,
            notional_usdt=101,
            tick_size=1,
            quantity_step=1,
            unrealistic_participation_threshold=0.25,
        )
        self.assertGreater(counters.skipped_overlap, 0)
        self.assertTrue(((trades.exit_time_us - trades.decision_time_us) == 500_000).all())
        frame.loc[5, "feature_segment_id"] = 2
        one_signal = np.full(len(frame), np.nan)
        one_signal[0] = 10.0
        rejected, rejected_counters = run_day(
            frame,
            one_signal,
            model_name="obi_only",
            horizon_ms=500,
            prediction_threshold_ticks=5,
            latency_ms=0,
            notional_usdt=100,
            tick_size=1,
            quantity_step=1,
            unrealistic_participation_threshold=0.25,
        )
        self.assertTrue(rejected.empty)
        self.assertEqual(rejected_counters.excluded_gap, 1)

    def test_quantity_metrics_and_daily_sharpe(self) -> None:
        self.assertAlmostEqual(floor_quantity(1000, 76_000, 0.001), 0.013)
        frame = synthetic_book()
        prediction = np.full(len(frame), np.nan)
        prediction[0] = 10
        trades, _ = run_day(
            frame,
            prediction,
            model_name="obi_only",
            horizon_ms=500,
            prediction_threshold_ticks=5,
            latency_ms=0,
            notional_usdt=101,
            tick_size=1,
            quantity_step=1,
            unrealistic_participation_threshold=0.25,
        )
        trades = add_cost_layers(trades, fee_bps_per_side=0, penalty_ticks_per_fill=0, tick_size=1)
        metrics = performance_metrics(
            trades,
            pnl_usdt_column="layer3_net_pnl_usdt",
            pnl_ticks_column="layer3_net_ticks",
            primary_notional_usdt=101,
        )
        self.assertEqual(metrics["trades"], 1)
        self.assertEqual(metrics["total_pnl_usdt"], -1.0)
        sharpe = daily_sharpe(
            pd.Series([1.0, 2.0, -1.0, 3.0, 2.0]),
            primary_notional_usdt=100,
            bootstrap_samples=100,
            bootstrap_seed=7,
        )
        self.assertEqual(sharpe["observed_days"], 5)
        self.assertTrue(math.isfinite(float(sharpe["sample_daily_sharpe"])))
        self.assertIsNotNone(sharpe["bootstrap_95pct_daily_sharpe_lower"])

    def test_deterministic_output(self) -> None:
        frame = synthetic_book()
        prediction = np.where(np.arange(len(frame)) % 2 == 0, 10.0, -10.0)
        first, first_counters = run_day(
            frame,
            prediction,
            model_name="combined",
            horizon_ms=500,
            prediction_threshold_ticks=5,
            latency_ms=0,
            notional_usdt=100,
            tick_size=1,
            quantity_step=1,
            unrealistic_participation_threshold=0.25,
        )
        second, second_counters = run_day(
            frame,
            prediction,
            model_name="combined",
            horizon_ms=500,
            prediction_threshold_ticks=5,
            latency_ms=0,
            notional_usdt=100,
            tick_size=1,
            quantity_step=1,
            unrealistic_participation_threshold=0.25,
        )
        pd.testing.assert_frame_equal(first, second)
        self.assertEqual(first_counters, second_counters)


if __name__ == "__main__":
    unittest.main()
