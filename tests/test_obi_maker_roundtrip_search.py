import json
import unittest

import numpy as np
import pandas as pd

from pyresearch.obi.maker_roundtrip_search import (
    RANKING_PATH,
    SPEC_PATH,
    maker_roundtrip_returns,
    target_exit_indices,
)


class OBIMakerRoundtripSearchTest(unittest.TestCase):
    def test_target_exit_is_causal_and_rounded_up(self) -> None:
        result = target_exit_indices(
            np.array([50, 100_000, 100_001, 199_999], dtype="int64"),
            day_start_us=0,
            minimum_delay_ms=100,
            additional_hold_ms=0,
        )
        np.testing.assert_array_equal(result, [2, 2, 3, 3])

    def test_bid_and_ask_roundtrips_and_two_maker_fees(self) -> None:
        gross, net = maker_roundtrip_returns(
            entry_side_is_bid=np.array([True, False]),
            entry_price=np.array([100.0, 101.0]),
            exit_price=np.array([102.0, 99.0]),
            maker_fee_bps_per_leg=2.0,
        )
        self.assertAlmostEqual(gross[0], 200.0)
        self.assertAlmostEqual(gross[1], 2.0 / 101.0 * 10_000.0)
        expected_long = (2.0 - 0.0002 * (100.0 + 102.0)) / 100.0 * 10_000.0
        expected_short = (2.0 - 0.0002 * (101.0 + 99.0)) / 101.0 * 10_000.0
        self.assertAlmostEqual(net[0], expected_long)
        self.assertAlmostEqual(net[1], expected_short)

    def test_spec_requires_pessimistic_full_fills_on_both_legs(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertEqual(spec["data"]["queue_model"], "pessimistic_visible_queue")
        self.assertTrue(spec["data"]["full_fills_required_on_both_legs"])
        self.assertEqual(spec["fees"]["primary_maker_fee_bps_per_leg"], 2.0)
        self.assertEqual(
            spec["exit"]["causal_clock"],
            "full_fill_local_time_us_then_next_100ms_decision_grid",
        )

    def test_local_loop4_gate_when_artifact_exists(self) -> None:
        if not RANKING_PATH.exists():
            self.skipTest("ignored local maker-roundtrip artifact is unavailable")
        ranking = pd.read_csv(RANKING_PATH)
        eligible = ranking.loc[ranking["eligible_activity"] & ~ranking["duplicate_behavior"]]
        self.assertEqual(len(ranking), 9_216)
        self.assertEqual(
            int(ranking["advances_to_exact_execution"].sum()),
            int(
                (
                    eligible["eligible_activity"]
                    & (eligible["positive_development_days"] == 5)
                    & (eligible["worst_development_day_net_bps"] > 0)
                    & (eligible["primary_net_mean_bps"] > 0)
                ).sum()
            ),
        )


if __name__ == "__main__":
    unittest.main()
