import json
import unittest

import numpy as np
import pandas as pd

from pyresearch.obi.momentum_gate_search import (
    EXACT_CAPACITY_PATH,
    RANKING_PATH,
    SPEC_PATH,
    causal_backward_move,
    momentum_regime_masks,
)


class OBIMomentumGateSearchTest(unittest.TestCase):
    def test_backward_move_is_past_only_and_segment_scoped(self) -> None:
        mid = np.array([10.0, 11.0, 13.0, 20.0, 19.0])
        segments = np.array([1, 1, 1, 2, 2], dtype=float)
        result = causal_backward_move(mid, segments, steps=2)
        self.assertTrue(np.isnan(result[0]))
        self.assertTrue(np.isnan(result[1]))
        self.assertEqual(result[2], 3.0)
        self.assertTrue(np.isnan(result[3]))
        self.assertTrue(np.isnan(result[4]))

    def test_momentum_is_oriented_to_inventory_side(self) -> None:
        masks = momentum_regime_masks(
            side_is_bid=np.array([True, False, True, False]),
            queue_low=np.array([True, True, False, False]),
            move_5m=np.array([1.0, 1.0, -1.0, -1.0]),
            move_1h=np.array([2.0, -2.0, 2.0, -2.0]),
        )
        np.testing.assert_array_equal(masks["momentum_5m_aligned"], [True, False, False, True])
        np.testing.assert_array_equal(masks["momentum_1h_aligned"], [True, True, True, True])
        np.testing.assert_array_equal(masks["pullback_with_1h_aligned"], [False, True, True, False])

    def test_spec_is_causal_and_requires_forward_proof(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertTrue(spec["causal_momentum"]["require_same_feature_segment_at_now_and_lag"])
        self.assertTrue(spec["interpretation"]["genuine_success_requires_frozen_post_freeze_native_data"])
        self.assertEqual(spec["fees"]["primary_maker_fee_bps_per_leg"], 2.0)

    def test_local_loop6_gate_when_artifact_exists(self) -> None:
        if not RANKING_PATH.exists():
            self.skipTest("ignored local momentum-gate artifact is unavailable")
        ranking = pd.read_csv(RANKING_PATH)
        self.assertEqual(len(ranking), 4_992)
        advanced = ranking.loc[ranking["advances_to_exact_execution"]]
        if len(advanced):
            self.assertTrue((advanced["positive_development_days"] == 5).all())
            self.assertTrue((advanced["worst_development_day_net_bps"] > 0).all())

    def test_exact_capacity_rejects_one_hour_one_position_survivors(self) -> None:
        if not EXACT_CAPACITY_PATH.exists():
            self.skipTest("ignored local capacity audit is unavailable")
        audit = json.loads(EXACT_CAPACITY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(audit["optimistic_max_completed_roundtrips_per_day"], 23)
        self.assertEqual(audit["optimistic_max_completed_roundtrips_five_days"], 115)
        self.assertFalse(audit["can_meet_activity_gate"])
        self.assertFalse(audit["advances_to_partial_fill_and_pnl_exact_replay"])


if __name__ == "__main__":
    unittest.main()
