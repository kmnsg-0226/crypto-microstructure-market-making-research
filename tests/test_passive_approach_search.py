import json
from pathlib import Path
import unittest

import numpy as np

from pyresearch.passive.approach_search import (
    SPEC_PATH,
    Policy,
    _single_mask,
    combination_policies,
    policies,
    policy_mask,
)


THRESHOLDS = {
    name: {"q20": -1.0, "q50": 0.0, "q80": 1.0}
    for name in (
        "obi_l1",
        "obi_l5",
        "obi_l10",
        "weighted_obi_l10",
        "weighted_mid_minus_mid_ticks",
        "normalized_ofi_1s",
        "ti_1s",
        "combined_prediction_1s_ticks",
    )
}
THRESHOLDS.update(
    {
        "queue_ahead_initial": {"q20": 2.0, "q50": 5.0, "q80": 8.0},
        "spread_ticks": {"q20": 1.0, "q50": 1.0, "q80": 1.0},
    }
)


def arrays() -> dict[str, np.ndarray]:
    result = {
        "is_bid": np.array([True, True, False, False]),
        "decision_time_us": np.array(
            [1_000_000, 9 * 3_600_000_000, 17 * 3_600_000_000, 23 * 3_600_000_000]
        ),
        "queue_ahead_initial": np.array([1.0, 5.0, 9.0, np.nan]),
        "spread_ticks": np.array([1.0, 2.0, 1.0, np.nan]),
    }
    for name in THRESHOLDS:
        if name not in result and name != "spread_ticks":
            result[name] = np.array([2.0, -2.0, -2.0, 2.0])
    return result


class PassiveApproachPolicyTest(unittest.TestCase):
    def test_catalog_is_fixed_and_unique(self) -> None:
        catalog = policies()
        self.assertEqual(len(catalog), 77)
        self.assertEqual(len({item.name for item in catalog}), 77)
        self.assertEqual(catalog[0].name, "always_quote")
        combinations = combination_policies()
        self.assertEqual(len(combinations), 171)
        self.assertEqual(len({item.name for item in combinations}), 171)

    def test_single_signal_side_direction(self) -> None:
        values = np.array([2.0, -2.0, -2.0, 2.0, np.nan])
        is_bid = np.array([True, True, False, False, True])
        trend = _single_mask("trend_tail", values, is_bid, THRESHOLDS["obi_l5"])
        contrarian = _single_mask(
            "contrarian_tail", values, is_bid, THRESHOLDS["obi_l5"]
        )
        np.testing.assert_array_equal(trend, [True, False, True, False, False])
        np.testing.assert_array_equal(contrarian, [False, True, False, True, False])

    def test_consensus_joint_liquidity_and_time_masks(self) -> None:
        data = arrays()
        consensus = Policy(
            "test", "obi_consensus", "consensus_trend_tail"
        )
        joint = Policy("test2", "joint_signal", "joint_contrarian_tail")
        np.testing.assert_array_equal(
            policy_mask(consensus, data, THRESHOLDS), [True, False, True, False]
        )
        np.testing.assert_array_equal(
            policy_mask(joint, data, THRESHOLDS), [False, True, False, True]
        )
        np.testing.assert_array_equal(
            policy_mask(Policy("q", "liquidity", "queue_low"), data, THRESHOLDS),
            [True, False, False, False],
        )
        np.testing.assert_array_equal(
            policy_mask(Policy("s", "liquidity", "spread_one"), data, THRESHOLDS),
            [True, False, True, False],
        )
        np.testing.assert_array_equal(
            policy_mask(Policy("t", "time", "session_08_16"), data, THRESHOLDS),
            [False, True, False, False],
        )

    def test_spec_is_explicitly_post_v1_and_not_new_holdout(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertTrue(spec["audit"]["v1_result_seen"])
        self.assertTrue(spec["interpretation"]["june_already_seen"])
        self.assertTrue(spec["interpretation"]["july_august_already_seen"])
        self.assertTrue(
            spec["interpretation"]["may_not_call_june_or_july_august_new_unseen_validation"]
        )
        self.assertFalse(spec["interpretation"]["profitability_claim_allowed"])

    def test_local_artifact_summary_when_sweep_exists(self) -> None:
        report = Path("data/research/tardis/reports/passive/approach_exploration")
        if not (report / "combinations/split_comparison.csv").exists():
            self.skipTest("ignored local exploration artifacts are unavailable")
        from pyresearch.passive.approach_reporting import build_summary

        summary = build_summary()
        self.assertEqual(summary["scale"]["distinct_primary_approaches_including_one_baseline"], 248)
        self.assertEqual(summary["sensitivity"]["positive_absolute_combined_side_cells"], 0)
        self.assertEqual(summary["phase2"]["positive_absolute_combined_side_cells"], 0)
        self.assertFalse(summary["interpretation"]["new_unseen_validation_claimed"])


if __name__ == "__main__":
    unittest.main()
