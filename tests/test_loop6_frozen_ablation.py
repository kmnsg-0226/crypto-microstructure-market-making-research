import json
import unittest

import numpy as np
import pandas as pd

from pyresearch.obi.loop6_frozen_ablation import (
    SPEC_PATH,
    summarize_variant,
    validation_gate,
)


class Loop6FrozenAblationTest(unittest.TestCase):
    def test_spec_freezes_one_policy_and_stops_directional_search(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        policy = spec["frozen_obi_plus_momentum_policy"]
        self.assertEqual(policy["source_development_rank"], 1)
        self.assertEqual(policy["regime"], "reversal_against_1h")
        self.assertTrue(spec["interpretation"]["no_more_directional_loop_search_after_this_test"])
        self.assertTrue(spec["validation_run_policy"]["open_june_july_august_once"])

    def test_variant_summary_uses_only_selected_finite_rows(self) -> None:
        result = summarize_variant(
            date="2026-01-01",
            variant="test",
            selected=np.array([True, True, False, True]),
            gross=np.array([2.0, np.nan, 100.0, 4.0]),
            net=np.array([1.0, 2.0, 100.0, -1.0]),
        )
        self.assertEqual(result["completed_roundtrips"], 2)
        self.assertEqual(result["gross_mean_bps"], 3.0)
        self.assertEqual(result["primary_net_mean_bps"], 0.0)
        self.assertEqual(result["primary_net_positive_probability"], 0.5)

    def test_validation_gate_requires_all_months_and_increment(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        day = pd.DataFrame({
            "variant": ["obi_plus_momentum"] * 3,
            "completed_roundtrips": [50, 60, 70],
            "primary_net_mean_bps": [1.0, 2.0, 3.0],
        })
        summary = {
            "obi_plus_momentum": {"pooled_primary_net_mean_bps": 2.0},
            "momentum_only": {"pooled_primary_net_mean_bps": 1.0},
        }
        self.assertTrue(validation_gate(day, summary, spec)["passes"])
        day.loc[1, "primary_net_mean_bps"] = -0.1
        self.assertFalse(validation_gate(day, summary, spec)["passes"])


if __name__ == "__main__":
    unittest.main()
