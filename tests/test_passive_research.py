import unittest

import numpy as np
import pandas as pd

from pyresearch.passive.labeling import attach_markouts


def probe_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "side": ["bid", "ask", "bid"],
            "quote_price": [100.0, 101.0, 100.0],
            "filled_qty": [0.005, 0.005, 0.005],
            "first_fill_exchange_time_us": [150_000.0, 150_000.0, 150_000.0],
            "optimistic_filled_qty": [0.005, 0.005, 0.005],
            "optimistic_first_fill_exchange_time_us": [150_000.0, 150_000.0, 150_000.0],
            "obi_l1": [-0.9, 0.0, 0.9],
            "obi_l5": [-0.9, 0.0, 0.9],
            "obi_l10": [-0.9, 0.0, 0.9],
        }
    )


class PassiveMarkoutTest(unittest.TestCase):
    def test_bid_and_ask_markout_decomposition_without_double_spread(self) -> None:
        frame = probe_frame().iloc[:2].copy()
        mids = np.array([100.5, 100.5, 100.5, 101.5, 101.5, 101.5, 101.5])
        valid = np.ones(len(mids), dtype=bool)
        segments = np.ones(len(mids), dtype=float)
        labeled = attach_markouts(
            frame,
            day_start_us=0,
            mids=mids,
            valid=valid,
            segments=segments,
        )
        self.assertEqual(labeled.iloc[0].fill_grid_time_us, 200_000)
        self.assertAlmostEqual(labeled.iloc[0].fill_price_advantage_ticks, 5.0)
        self.assertAlmostEqual(labeled.iloc[0].post_fill_mid_move_100ms_ticks, 10.0)
        self.assertAlmostEqual(labeled.iloc[0].maker_markout_100ms_ticks, 15.0)
        self.assertAlmostEqual(labeled.iloc[1].fill_price_advantage_ticks, 5.0)
        self.assertAlmostEqual(labeled.iloc[1].post_fill_mid_move_100ms_ticks, -10.0)
        self.assertAlmostEqual(labeled.iloc[1].maker_markout_100ms_ticks, -5.0)
        self.assertAlmostEqual(
            labeled.iloc[0].maker_markout_100ms_ticks,
            labeled.iloc[0].fill_price_advantage_ticks
            + labeled.iloc[0].post_fill_mid_move_100ms_ticks,
        )

    def test_segment_change_nulls_future_label(self) -> None:
        frame = probe_frame().iloc[[0]].copy()
        mids = np.array([100.5, 100.5, 100.5, 101.5, 101.5, 101.5])
        valid = np.ones(len(mids), dtype=bool)
        segments = np.array([1, 1, 1, 2, 2, 2], dtype=float)
        labeled = attach_markouts(
            frame,
            day_start_us=0,
            mids=mids,
            valid=valid,
            segments=segments,
        )
        self.assertEqual(labeled.iloc[0].label_valid_100ms, 0)
        self.assertTrue(np.isnan(labeled.iloc[0].maker_markout_100ms_ticks))

    def test_fill_time_rounds_forward_and_frozen_bins_are_used(self) -> None:
        frame = probe_frame()
        mids = np.full(20, 100.5)
        valid = np.ones(20, dtype=bool)
        segments = np.ones(20, dtype=float)
        labeled = attach_markouts(
            frame,
            day_start_us=0,
            mids=mids,
            valid=valid,
            segments=segments,
        )
        self.assertTrue((labeled.fill_grid_time_us == 200_000).all())
        self.assertLess(int(labeled.iloc[0].obi_l5_bin), int(labeled.iloc[2].obi_l5_bin))


if __name__ == "__main__":
    unittest.main()
