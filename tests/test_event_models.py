from __future__ import annotations

import json
from pathlib import Path
import unittest

import numpy as np
import pandas as pd

from pyresearch.event.common import (
    EVENT_RULE_VOTES,
    FULL_FEATURES,
    QuoteDecision,
    event_rule_score,
)
from pyresearch.event.dataset import attach_event_labels
from pyresearch.obi.continuous_mm import simulate_schedule
from pyresearch.support.evaluate import sha256


ROOT = Path(__file__).resolve().parents[1]


class EventModelTest(unittest.TestCase):
    def test_plan_hash_is_frozen(self) -> None:
        path = ROOT / "research/specs/event_model_comparison_plan.json"
        receipt = (path.with_suffix(".json.sha256")).read_text().split()[0]
        self.assertEqual(sha256(path), receipt)
        self.assertEqual(json.loads(path.read_text())["status"], "frozen_before_event_model_profitability")

    def test_quote_decision_interface(self) -> None:
        decision = QuoteDecision(True, 0.5, 0.25, 2.0, "test")
        self.assertEqual(
            set(decision.to_dict()),
            {"quote", "score", "predicted_fill_probability", "predicted_markout_ticks", "model_id"},
        )

    def test_side_oriented_event_rule_signs(self) -> None:
        bid = {feature: 1.0 for feature in EVENT_RULE_VOTES}
        ask = {feature: -1.0 for feature in EVENT_RULE_VOTES}
        score = event_rule_score(pd.DataFrame([bid, ask]))
        np.testing.assert_array_equal(score, [1.0, 0.0])

    def test_event_feature_vector_is_compact_and_target_free(self) -> None:
        self.assertEqual(len(FULL_FEATURES), len(set(FULL_FEATURES)))
        self.assertEqual(len(FULL_FEATURES), 86)
        for feature in FULL_FEATURES:
            self.assertNotIn("future", feature)
            self.assertNotIn("markout", feature)
            self.assertNotIn("fill_label", feature)

    def test_post_fill_labels_are_side_oriented_and_gap_safe(self) -> None:
        frame = pd.DataFrame({
            "first_fill_exchange_time_us": [100_001.0, 100_001.0],
            "filled_qty": [0.005, 0.005],
            "quote_price": [100.0, 101.0],
            "quote_side": [1, -1],
        })
        mids = np.array([100.5, 100.5, 101.5, 102.5, 103.5, 104.5, 105.5,
                         106.5, 107.5, 108.5, 109.5, 110.5, 111.5, 112.5,
                         113.5, 114.5, 115.5, 116.5, 117.5, 118.5, 119.5,
                         120.5, 121.5, 122.5, 123.5, 124.5, 125.5, 126.5,
                         127.5, 128.5, 129.5, 130.5, 131.5, 132.5, 133.5,
                         134.5, 135.5, 136.5, 137.5, 138.5, 139.5, 140.5,
                         141.5, 142.5, 143.5, 144.5, 145.5, 146.5, 147.5,
                         148.5, 149.5, 150.5, 151.5], dtype="float64")
        valid = np.ones(len(mids), dtype="bool")
        segments = np.ones(len(mids))
        labeled = attach_event_labels(
            frame, day_start_us=0, mids=mids, valid=valid, segments=segments
        )
        self.assertGreater(labeled.loc[0, "maker_markout_1s_ticks"], 0)
        self.assertLess(labeled.loc[1, "maker_markout_1s_ticks"], 0)
        segments[12] = 2
        crossed = attach_event_labels(
            frame, day_start_us=0, mids=mids, valid=valid, segments=segments
        )
        self.assertTrue(np.isnan(crossed.loc[0, "maker_markout_1s_ticks"]))

    def test_selector_reducing_inventory_override_and_cashflow(self) -> None:
        schedule = pd.DataFrame([
            {
                "placement_local_time_us": 0, "side": "bid", "quote_qty": 0.005,
                "weighted_obi_l10": 0.0, "fill_status": "full",
                "first_fill_local_time_us": 10, "full_fill_local_time_us": 10,
                "expiry_local_time_us": 1000, "filled_qty": 0.005,
                "quote_price": 100.0, "selected": True,
            },
            {
                "placement_local_time_us": 20, "side": "ask", "quote_qty": 0.005,
                "weighted_obi_l10": 0.0, "fill_status": "full",
                "first_fill_local_time_us": 30, "full_fill_local_time_us": 30,
                "expiry_local_time_us": 1020, "filled_qty": 0.005,
                "quote_price": 101.0, "selected": False,
            },
        ])
        spec = {
            "market": {"quote_qty_btc": 0.005},
            "inventory": {"soft_limit_abs_btc": 0.015, "hard_limit_abs_btc": 0.025,
                          "quantity_tolerance_btc": 1e-9},
            "costs": {"maker_fee_bps": 2.0, "day_end_taker_fee_bps": 5.0},
            "policies": {"obi_aware": {"absolute_threshold": 0.8}},
        }
        result = simulate_schedule(
            schedule, date="fixture", policy="neutral", last_bid=100.0, last_ask=101.0,
            spec=spec, selection_column="selected", risk_reducing_override=True,
        )
        self.assertEqual(result["maker_fill_orders"], 2)
        self.assertAlmostEqual(result["gross_pnl_usdt"], 0.005)
        self.assertEqual(result["inventory_limit_violations"], 0)


if __name__ == "__main__":
    unittest.main()
