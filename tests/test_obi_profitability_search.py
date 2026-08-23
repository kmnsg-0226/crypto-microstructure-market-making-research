import json
import unittest

import numpy as np
import pandas as pd

from pyresearch.obi.search import RANKING_PATH, SPEC_PATH, bbo_returns, regime_masks


class OBIProfitabilitySearchTest(unittest.TestCase):
    def test_bbo_returns_include_spread_latency_and_two_sided_fee(self) -> None:
        context = {
            "sample_time_us": np.arange(6, dtype="int64") * 100_000,
            "valid_book_state": np.ones(6, dtype=bool),
            "feature_segment_id": np.ones(6),
            "best_bid_price": np.array([99.0, 100.0, 101.0, 102.0, 103.0, 104.0]),
            "best_ask_price": np.array([101.0, 102.0, 103.0, 104.0, 105.0, 106.0]),
        }
        result = bbo_returns(
            context, horizon_ms=300, latency_ms=100, fee_bps_per_side=5.0
        )
        expected_gross = (102.0 - 102.0) / 102.0 * 10_000.0
        expected_net = (102.0 - 102.0 - 0.0005 * (102.0 + 102.0)) / 102.0 * 10_000.0
        self.assertAlmostEqual(result["long_gross"][0], expected_gross)
        self.assertAlmostEqual(result["long_net"][0], expected_net)
        self.assertAlmostEqual(result["long_net"][0], -10.0)

    def test_cross_segment_outcome_is_invalid(self) -> None:
        context = {
            "sample_time_us": np.arange(6, dtype="int64") * 100_000,
            "valid_book_state": np.ones(6, dtype=bool),
            "feature_segment_id": np.array([1, 1, 1, 2, 2, 2], dtype=float),
            "best_bid_price": np.full(6, 100.0),
            "best_ask_price": np.full(6, 101.0),
        }
        result = bbo_returns(
            context, horizon_ms=300, latency_ms=100, fee_bps_per_side=5.0
        )
        self.assertFalse(result["valid"][0])

    def test_regime_alignment_is_side_aware(self) -> None:
        signal = np.array([1.0, -1.0, 1.0, -1.0])
        signals = {
            "ti_1s": np.array([1.0, -1.0, -1.0, 1.0]),
            "normalized_ofi_1s": np.array([1.0, 1.0, 1.0, -1.0]),
        }
        context = {
            "sample_time_us": np.array([0, 9, 17, 23], dtype="int64")
            * 3_600_000_000,
            "spread_ticks": np.array([1.0, 2.0, 1.0, 1.0]),
            "trade_volume_1s": np.array([1.0, 2.0, 9.0, np.nan]),
            "book_level_update_count_1s": np.array([1.0, 2.0, 9.0, np.nan]),
            "backward_volatility_5s_ticks": np.array([1.0, 2.0, 9.0, np.nan]),
        }
        thresholds = {
            "regime_thresholds": {
                name: {"q20": 2.0, "q80": 8.0}
                for name in (
                    "trade_volume_1s",
                    "book_level_update_count_1s",
                    "backward_volatility_5s_ticks",
                )
            }
        }
        masks = regime_masks(signal, signals, context, thresholds)
        np.testing.assert_array_equal(masks["ti_1s_aligned"], [True, True, False, False])
        np.testing.assert_array_equal(masks["ofi_1s_aligned"], [True, False, True, True])
        np.testing.assert_array_equal(masks["utc_08_16"], [False, True, False, False])

    def test_spec_requires_future_native_success(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertTrue(
            spec["interpretation"]["historical_positive_result_cannot_complete_profitability_goal"]
        )
        self.assertTrue(
            spec["interpretation"]["genuine_success_requires_post_freeze_native_binance_data"]
        )
        self.assertEqual(spec["forward_success_gate"]["native_binance_consecutive_days_minimum"], 7)

    def test_local_stage1_upper_bound_gate_when_artifact_exists(self) -> None:
        if not RANKING_PATH.exists():
            self.skipTest("ignored local OBI search artifact is unavailable")
        ranking = pd.read_csv(RANKING_PATH)
        self.assertEqual(len(ranking), 25_088)
        self.assertEqual(int(ranking["advances_to_exact_execution"].sum()), 0)
        self.assertLess(float(ranking["pooled_net_upper_bound_mean_bps"].max()), 0.0)


if __name__ == "__main__":
    unittest.main()
