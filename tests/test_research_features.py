import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from pyresearch.support.feature_pipeline import build_day, compute_features


SPEC_PATH = Path(__file__).parents[1] / "research/specs/research_spec_draft.json"
SPEC = json.loads(SPEC_PATH.read_text())


def base_frame(rows: int = 70) -> pd.DataFrame:
    index = np.arange(rows)
    frame = pd.DataFrame(
        {
            "schema_version": "tardis-100ms-v1",
            "date": "2026-05-01",
            "sample_time_us": 1_000_000 + index * 100_000,
            "latest_book_event_time_us": 1_000_000 + index * 100_000,
            "latest_trade_time_us": 1_000_000 + index * 100_000,
            "valid_book_state": 1,
            "book_segment_id": 1,
            "bid_qty_1": 6.0,
            "ask_qty_1": 4.0,
            "mid_ticks_x2": 200 + index * 2,
            "aggressive_buy_qty_100ms": index.astype(float),
            "aggressive_sell_qty_100ms": np.ones(rows),
            "aggressive_buy_notional_100ms": index.astype(float) * 100.0,
            "aggressive_sell_notional_100ms": np.full(rows, 100.0),
            "trade_count_100ms": np.ones(rows),
            "buy_trade_count_100ms": np.ones(rows),
            "sell_trade_count_100ms": np.zeros(rows),
            "trade_volume_100ms": index.astype(float) + 1.0,
            "book_message_count_100ms": np.ones(rows),
            "book_level_update_count_100ms": np.full(rows, 2.0),
            "ofi_100ms_bucket": np.full(rows, 2.0),
        }
    )
    return frame


class ResearchFeatureTest(unittest.TestCase):
    def test_causal_windows_ti_ofi_and_labels(self) -> None:
        result = compute_features(base_frame(), SPEC)
        self.assertTrue(np.isnan(result.loc[0, "aggressive_buy_qty_100ms"]))
        self.assertEqual(result.loc[1, "aggressive_buy_qty_100ms"], 1.0)
        self.assertTrue(np.isnan(result.loc[4, "aggressive_buy_qty_500ms"]))
        self.assertEqual(result.loc[5, "aggressive_buy_qty_500ms"], 15.0)
        self.assertEqual(result.loc[10, "aggressive_buy_qty_1s"], 55.0)
        self.assertAlmostEqual(result.loc[5, "ti_500ms"], (15.0 - 5.0) / 20.0)
        self.assertEqual(result.loc[10, "normalized_ofi_1s"], 2.0)
        self.assertEqual(result.loc[0, "markout_500ms_ticks"], 5.0)
        self.assertEqual(result.loc[0, "direction_500ms"], 1.0)

    def test_gap_resets_windows_and_nulls_cross_gap_labels(self) -> None:
        frame = base_frame()
        frame.loc[8, "valid_book_state"] = 0
        frame.loc[8, "book_segment_id"] = np.nan
        result = compute_features(frame, SPEC)
        self.assertEqual(result.loc[7, "feature_segment_id"], 1)
        self.assertTrue(pd.isna(result.loc[8, "feature_segment_id"]))
        self.assertEqual(result.loc[9, "feature_segment_id"], 2)
        self.assertTrue(np.isnan(result.loc[7, "markout_500ms_ticks"]))
        self.assertTrue(np.isnan(result.loc[9, "aggressive_buy_qty_100ms"]))
        self.assertEqual(result.loc[10, "aggressive_buy_qty_100ms"], 10.0)
        self.assertTrue(np.isnan(result.loc[13, "aggressive_buy_qty_500ms"]))
        self.assertEqual(result.loc[14, "aggressive_buy_qty_500ms"], sum(range(10, 15)))

    def test_future_metadata_is_rejected(self) -> None:
        frame = base_frame()
        frame.loc[3, "latest_trade_time_us"] = frame.loc[3, "sample_time_us"] + 1
        with self.assertRaisesRegex(ValueError, "feature leakage"):
            compute_features(frame, SPEC)

    def test_parquet_build_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "base.csv.zst"
            base_frame().to_csv(source, index=False, compression="zstd")
            first = build_day(source, root / "first.parquet", SPEC_PATH)
            second = build_day(source, root / "second.parquet", SPEC_PATH)
            self.assertEqual(first["rows"], 70)
            self.assertEqual(first["output_sha256"], second["output_sha256"])
            self.assertEqual(first["feature_time_leakage_violations"], 0)


if __name__ == "__main__":
    unittest.main()
