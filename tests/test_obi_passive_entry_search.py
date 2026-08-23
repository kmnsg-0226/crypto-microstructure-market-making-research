import json
import unittest

import numpy as np
import pandas as pd

from pyresearch.obi.passive_entry_search import (
    RANKING_PATH,
    SPEC_PATH,
    next_unsafe_indices,
    passive_exit_returns,
)


class OBIPassiveEntrySearchTest(unittest.TestCase):
    def test_next_unsafe_is_strictly_future_and_stops_at_boundary(self) -> None:
        safety = np.array([True, True, False, True, True, True])
        segments = np.array([1, 1, 1, 2, 2, 2], dtype=float)
        result = next_unsafe_indices(safety, segments)
        np.testing.assert_array_equal(result, [2, 2, 3, 6, 6, 6])

    def test_passive_bid_and_ask_cashflow_and_fees(self) -> None:
        gross, net = passive_exit_returns(
            side_is_bid=np.array([True, False]),
            quote_price=np.array([100.0, 101.0]),
            exit_bid=np.array([102.0, 0.0]),
            exit_ask=np.array([0.0, 99.0]),
            maker_fee_bps=2.0,
            taker_fee_bps=5.0,
        )
        self.assertAlmostEqual(gross[0], 200.0)
        self.assertAlmostEqual(gross[1], 2.0 / 101.0 * 10_000.0)
        expected_bid = (2.0 - 0.0002 * 100.0 - 0.0005 * 102.0) / 100.0 * 10_000.0
        expected_ask = (2.0 - 0.0002 * 101.0 - 0.0005 * 99.0) / 101.0 * 10_000.0
        self.assertAlmostEqual(net[0], expected_bid)
        self.assertAlmostEqual(net[1], expected_ask)

    def test_spec_uses_pessimistic_queue_and_primary_fees(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertEqual(spec["data"]["queue_model"], "pessimistic_visible_queue")
        self.assertEqual(spec["entry"]["primary_maker_fee_bps"], 2.0)
        self.assertEqual(spec["exit"]["taker_fee_bps"], 5.0)
        self.assertTrue(spec["interpretation"]["historical_positive_cannot_complete_profitability_goal"])

    def test_local_loop3_gate_when_artifact_exists(self) -> None:
        if not RANKING_PATH.exists():
            self.skipTest("ignored local passive-entry artifact is unavailable")
        ranking = pd.read_csv(RANKING_PATH)
        eligible = ranking.loc[ranking["eligible_activity"] & ~ranking["duplicate_behavior"]]
        self.assertEqual(len(ranking), 32_256)
        self.assertEqual(int((eligible["primary_net_mean_bps"] > 0).sum()), 0)
        self.assertEqual(int((eligible["net_maker_0bps_mean_bps"] > 0).sum()), 0)
        self.assertEqual(int(ranking["advances_to_exact_execution"].sum()), 0)


if __name__ == "__main__":
    unittest.main()
