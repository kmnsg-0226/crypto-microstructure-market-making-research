import copy
import json
import unittest

import pandas as pd

from pyresearch.obi.continuous_mm import (
    SPEC_PATH,
    VALIDATION_RECEIPT_PATH,
    quote_permission,
    simulate_schedule,
    validation_gate,
)


class ContinuousInventoryMMTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))

    def test_neutral_and_obi_quote_permissions_respect_inventory(self) -> None:
        allowed, reason = quote_permission(
            policy="obi_aware", side="ask", inventory_btc=0.0, obi=0.9,
            spec=self.spec,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "obi")
        allowed, reason = quote_permission(
            policy="obi_aware", side="ask", inventory_btc=0.005, obi=0.9,
            spec=self.spec,
        )
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        allowed, reason = quote_permission(
            policy="neutral", side="bid", inventory_btc=0.015, obi=0.0,
            spec=self.spec,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "inventory")

    def test_full_fills_are_chronological_and_fee_accounted(self) -> None:
        schedule = pd.DataFrame([
            {
                "decision_time_us": 0,
                "placement_local_time_us": 0,
                "side": "bid",
                "quote_price": 100.0,
                "quote_qty": 0.005,
                "expiry_local_time_us": 1_000_000,
                "weighted_obi_l10": 0.0,
                "fill_status": "full",
                "first_fill_local_time_us": 100,
                "full_fill_local_time_us": 200,
                "filled_qty": 0.005,
            },
            {
                "decision_time_us": 1_000_000,
                "placement_local_time_us": 1_000_000,
                "side": "ask",
                "quote_price": 101.0,
                "quote_qty": 0.005,
                "expiry_local_time_us": 2_000_000,
                "weighted_obi_l10": 0.0,
                "fill_status": "full",
                "first_fill_local_time_us": 1_000_100,
                "full_fill_local_time_us": 1_000_200,
                "filled_qty": 0.005,
            },
        ])
        result = simulate_schedule(
            schedule,
            date="test",
            policy="neutral",
            last_bid=100.0,
            last_ask=101.0,
            spec=self.spec,
        )
        self.assertEqual(result["full_fill_orders"], 2)
        self.assertEqual(result["forced_liquidation_qty_btc"], 0.0)
        self.assertAlmostEqual(result["gross_pnl_usdt"], 0.005)
        expected_fee = (100.0 * 0.005 + 101.0 * 0.005) * 0.0002
        self.assertAlmostEqual(result["net_pnl_usdt"], 0.005 - expected_fee)
        self.assertEqual(result["inventory_limit_violations"], 0)

    def test_validation_gate_requires_profit_increment_activity_and_limits(self) -> None:
        day = pd.DataFrame({
            "policy": ["neutral", "neutral", "obi_aware", "obi_aware"],
            "net_pnl_usdt": [-2.0, -1.0, 1.0, 2.0],
        })
        policies = {
            "neutral": {"net_pnl_usdt": -3.0},
            "obi_aware": {
                "net_pnl_usdt": 3.0,
                "maker_fill_orders": 500,
                "inventory_limit_violations": 0,
            },
        }
        self.assertTrue(validation_gate(day, policies, self.spec)["passes"])
        policies = copy.deepcopy(policies)
        policies["obi_aware"]["inventory_limit_violations"] = 1
        self.assertFalse(validation_gate(day, policies, self.spec)["passes"])

    def test_spec_freezes_chronological_splits_and_partial_fills(self) -> None:
        splits = self.spec["chronological_splits"]
        self.assertEqual(splits["development_no_tuning"], [
            "2026-01-01", "2026-02-01", "2026-03-01"
        ])
        self.assertEqual(splits["frozen_validation"], ["2026-04-01", "2026-05-01"])
        self.assertEqual(
            self.spec["fill_accounting"]["partial_fill_qty"],
            "labeled_filled_qty",
        )
        self.assertTrue(self.spec["run_policy"]["do_not_open_replication_split_if_validation_fails"])

    def test_local_validation_receipt_blocks_failed_replication(self) -> None:
        if not VALIDATION_RECEIPT_PATH.exists():
            self.skipTest("ignored local continuous-MM validation receipt is unavailable")
        receipt = json.loads(VALIDATION_RECEIPT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(receipt["evaluation_count"], 1)
        self.assertFalse(receipt["gate_passes"])


if __name__ == "__main__":
    unittest.main()
