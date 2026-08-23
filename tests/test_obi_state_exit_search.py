import json
import unittest

import numpy as np
import pandas as pd

from pyresearch.obi.state_exit_search import (
    RANKING_PATH,
    SPEC_PATH,
    first_true_for_starts,
    state_exit_targets,
)


class OBIStateExitSearchTest(unittest.TestCase):
    def test_first_true_does_not_cross_feature_segment(self) -> None:
        condition = np.array([False, False, True, False, True, False])
        segments = np.array([1, 1, 1, 2, 2, 2], dtype=float)
        starts = np.array([0, 2, 3, 5, 6, -1])
        result = first_true_for_starts(condition, segments, starts)
        np.testing.assert_array_equal(result, [2, 2, 4, 6, 6, 6])

    def test_exit_target_uses_side_oriented_condition_and_cap(self) -> None:
        values = np.array([-2.0, -1.0, 0.2, 2.0, 1.0, -0.2])
        segments = np.ones(6)
        targets = state_exit_targets(
            feature_values=values,
            segments=segments,
            entry_side_is_bid=np.array([True, False]),
            start_indices=np.array([1, 3]),
            cap_indices=np.array([5, 5]),
            threshold=1.5,
            direction="maker_contrarian",
            condition_name="entry_sign_exit",
        )
        # Contrarian bid orientation is -signal; ask orientation is +signal.
        np.testing.assert_array_equal(targets, [2, 5])

    def test_spec_requires_causal_state_exit_and_forward_proof(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertEqual(spec["data"]["queue_model"], "pessimistic_visible_queue")
        self.assertTrue(spec["data"]["full_fills_required_on_both_legs"])
        self.assertTrue(spec["interpretation"]["genuine_success_requires_frozen_post_freeze_native_data"])
        self.assertIn("entry_opposite_tail", spec["exit"]["state_conditions"])

    def test_local_loop5_gate_when_artifact_exists(self) -> None:
        if not RANKING_PATH.exists():
            self.skipTest("ignored local state-exit artifact is unavailable")
        ranking = pd.read_csv(RANKING_PATH)
        self.assertEqual(len(ranking), 9_216)
        advanced = ranking.loc[ranking["advances_to_exact_execution"]]
        if len(advanced):
            self.assertTrue(advanced["eligible_activity"].all())
            self.assertTrue((advanced["positive_development_days"] == 5).all())
            self.assertTrue((advanced["worst_development_day_net_bps"] > 0).all())


if __name__ == "__main__":
    unittest.main()
