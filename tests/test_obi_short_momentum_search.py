import json
import unittest

import pandas as pd

from pyresearch.obi.short_momentum_search import RANKING_PATH, SPEC_PATH


class OBIShortMomentumSearchTest(unittest.TestCase):
    def test_spec_uses_capacity_feasible_short_holds(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertEqual(spec["exit"]["additional_hold_delays_ms"], [60_000, 300_000])
        self.assertTrue(spec["exact_followup_if_gate_passes"]["one_pending_or_open_position_at_a_time"])
        self.assertTrue(spec["interpretation"]["genuine_success_requires_frozen_post_freeze_native_data"])

    def test_local_loop7_gate_when_artifact_exists(self) -> None:
        if not RANKING_PATH.exists():
            self.skipTest("ignored local short-momentum artifact is unavailable")
        ranking = pd.read_csv(RANKING_PATH)
        self.assertEqual(len(ranking), 4_992)
        advanced = ranking.loc[ranking["advances_to_exact_execution"]]
        if len(advanced):
            self.assertTrue((advanced["positive_development_days"] == 5).all())
            self.assertTrue((advanced["worst_development_day_net_bps"] > 0).all())


if __name__ == "__main__":
    unittest.main()
