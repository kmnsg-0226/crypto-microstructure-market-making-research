import json
import unittest

import numpy as np
import pandas as pd

from pyresearch.obi.persistent_search import RANKING_PATH, SPEC_PATH, trailing_segment_mean


class OBIPersistentSearchTest(unittest.TestCase):
    def test_trailing_mean_is_causal_full_window_and_segment_scoped(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 100.0, 5.0, 6.0, 7.0])
        segments = np.array([1, 1, 1, 2, 2, 2, 2], dtype=float)
        result = trailing_segment_mean(values, segments, 3)
        np.testing.assert_allclose(
            result,
            [np.nan, np.nan, 2.0, np.nan, np.nan, 37.0, 6.0],
            equal_nan=True,
        )

    def test_nan_invalidates_only_windows_containing_it(self) -> None:
        values = np.array([1.0, np.nan, 3.0, 4.0, 5.0])
        segments = np.ones(5)
        result = trailing_segment_mean(values, segments, 3)
        np.testing.assert_allclose(
            result, [np.nan, np.nan, np.nan, np.nan, 4.0], equal_nan=True
        )

    def test_spec_discloses_loop1_and_seen_data(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertTrue(spec["audit"]["loop1_results_seen"])
        self.assertTrue(spec["interpretation"]["all_eight_historical_dates_already_seen"])
        self.assertTrue(spec["interpretation"]["historical_positive_cannot_complete_profitability_goal"])

    def test_local_loop2_gate_when_artifact_exists(self) -> None:
        if not RANKING_PATH.exists():
            self.skipTest("ignored local persistent-search artifact is unavailable")
        ranking = pd.read_csv(RANKING_PATH)
        eligible = ranking.loc[ranking["eligible_activity"] & ~ranking["duplicate_behavior"]]
        self.assertEqual(len(ranking), 12_096)
        self.assertEqual(int(ranking["advances_to_exact_execution"].sum()), 0)
        self.assertEqual(int((eligible["positive_development_days"] == 5).sum()), 0)
        self.assertGreater(int((eligible["pooled_net_upper_bound_mean_bps"] > 0).sum()), 0)


if __name__ == "__main__":
    unittest.main()
