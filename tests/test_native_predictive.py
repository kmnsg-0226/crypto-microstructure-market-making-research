"""Leakage audit for the native predictive decomposition.

Every test here exists to falsify one specific way this phase could be quietly wrong: a fold
that sees the future, a target that reaches across a segment boundary, a feature that knows the
fill it is supposed to predict, or an out-of-fold prediction that is not actually out of fold.
"""
import json
import unittest

import numpy as np
import pandas as pd

from pyresearch.native.predictive import data, modeling, spec
from pyresearch.native.predictive.pipeline import build_side_frame, price_problems, side_problems

FRAMES_PRESENT = all(data.frame_path(index).exists() for index in (0, 1, 2))
OOF_SIDE = spec.DATA_DIR / "oof_side_predictions.csv.zst"
OOF_PRICE = spec.DATA_DIR / "oof_price_predictions.csv.zst"


class SpecTests(unittest.TestCase):
    def test_purge_exceeds_every_target_horizon(self):
        self.assertGreater(spec.PURGE_SECONDS, spec.MAX_TARGET_HORIZON_SECONDS)
        # The longest chain is the 30 s fill/race window followed by a 5 s post-fill markout.
        longest = 30.0 + max(spec.POSTFILL_HORIZONS_MS) / 1000.0
        self.assertGreaterEqual(spec.MAX_TARGET_HORIZON_SECONDS, longest)

    def test_methodology_declares_no_random_splitting(self):
        methodology = spec.methodology()
        self.assertFalse(methodology["validation"]["random_splits_used"])
        self.assertFalse(methodology["validation"]["shuffled_k_fold_used"])
        self.assertEqual(
            methodology["validation"]["label"], "blocked OOF development estimates"
        )
        self.assertFalse(methodology["corpus"]["is_out_of_sample"])
        self.assertIn("maker strategy search", methodology["prohibited_in_this_phase"])

    def test_model_c_features_carry_no_fill_or_post_fill_information(self):
        """Adverse-selection features must describe the book at placement, not at the fill."""
        forbidden = (
            "fill",
            "markout",
            "postfill",
            "time_to_",
            "adverse",
            "quote_gone",
            "through",
        )
        for feature in spec.SIDE_FEATURES + spec.ABSOLUTE_FEATURES:
            for token in forbidden:
                self.assertNotIn(token, feature, f"{feature} contains {token}")

    def test_side_normalised_frame_excludes_the_side_itself(self):
        self.assertNotIn("side", spec.SIDE_FEATURES)


@unittest.skipUnless(FRAMES_PRESENT, "model frames have not been built")
class FrameTests(unittest.TestCase):
    frame: pd.DataFrame

    @classmethod
    def setUpClass(cls) -> None:
        # File 0 is the smallest complete capture and carries one whole segment.
        cls.frame = pd.read_parquet(data.frame_path(0))

    def test_feature_lists_match_the_built_frame(self):
        for name in spec.ABSOLUTE_FEATURES:
            self.assertIn(name, self.frame.columns, name)
        produced = set(data.side_view(self.frame, "bid").columns)
        self.assertEqual(produced, set(spec.SIDE_FEATURES))
        self.assertEqual(
            set(data.side_view(self.frame, "ask").columns), set(spec.SIDE_FEATURES)
        )

    def test_side_normalisation_flips_signed_features_and_swaps_own_for_opposite(self):
        bid = data.side_view(self.frame, "bid")
        ask = data.side_view(self.frame, "ask")
        np.testing.assert_allclose(
            bid["signed_obi_l1"].to_numpy(), -ask["signed_obi_l1"].to_numpy(), rtol=1e-6
        )
        np.testing.assert_allclose(
            bid["log_own_depth_l5"].to_numpy(), ask["log_opp_depth_l5"].to_numpy(), rtol=1e-6
        )
        # Symmetric quantities must be untouched by the orientation.
        np.testing.assert_array_equal(
            bid["trade_count_1000ms"].to_numpy(), ask["trade_count_1000ms"].to_numpy()
        )

    def test_move_labels_preserve_censoring(self):
        remaining_ms = self.frame["remaining_ns"].to_numpy() / 1e6
        latency = self.frame["time_to_next_mid_move_ms"].to_numpy()
        for horizon in spec.MOVE_HORIZONS_MS:
            label = self.frame[f"y_move_{horizon}ms"].to_numpy()
            moved = latency <= horizon
            observed = remaining_ms >= horizon
            # Censored exactly when the horizon did not fit inside the segment and no move was
            # seen: never silently recoded as a zero.
            self.assertTrue(np.array_equal(np.isnan(label), ~(moved | observed)), horizon)
            self.assertTrue(np.array_equal(label[moved] == 1.0, np.ones(moved.sum(), bool)))

    def test_no_target_crosses_a_segment_boundary(self):
        remaining_ms = self.frame["remaining_ns"].to_numpy() / 1e6
        for horizon in (100, 250, 500, 1000, 5000):
            observed = np.isfinite(self.frame[f"markout_{horizon}ms_ticks"].to_numpy())
            self.assertFalse(observed[remaining_ms < horizon].any(), horizon)
        for side in ("bid", "ask"):
            for horizon in spec.FILL_HORIZONS_MS:
                observed = np.isfinite(self.frame[f"y_{side}_fill_{horizon}ms"].to_numpy())
                self.assertFalse(observed[remaining_ms < horizon].any(), (side, horizon))

    def test_no_post_fill_markout_crosses_a_segment_boundary(self):
        remaining_ms = self.frame["remaining_ns"].to_numpy() / 1e6
        for side in ("bid", "ask"):
            fill = self.frame[f"{side}_time_to_fill_ms"].to_numpy()
            for horizon in spec.POSTFILL_HORIZONS_MS:
                markout = self.frame[
                    f"{side}_postfill_markout_{horizon}ms_ticks"
                ].to_numpy()
                observed = np.isfinite(markout)
                # A post-fill markout is only observed when the fill and the horizon after it
                # both fit inside the segment.
                self.assertTrue(np.all(np.isfinite(fill[observed])))
                self.assertTrue(np.all(fill[observed] + horizon <= remaining_ms[observed]))

    def test_a_trade_at_the_decision_instant_cannot_fill_the_order_it_informed(self):
        for side in ("bid", "ask"):
            fill = self.frame[f"{side}_time_to_fill_ms"].to_numpy()
            observed = fill[np.isfinite(fill)]
            self.assertGreater(observed.size, 0)
            self.assertTrue((observed > 0).all())

    def test_fill_mechanism_labels_are_mutually_consistent(self):
        for side in ("bid", "ask"):
            through = self.frame[f"y_{side}_through_given_fill"].to_numpy()
            at_quote = self.frame[f"y_{side}_at_quote_given_fill"].to_numpy()
            filled = np.isfinite(self.frame[f"{side}_time_to_fill_ms"].to_numpy())
            self.assertTrue(np.array_equal(np.isnan(through), ~filled))
            self.assertTrue(np.array_equal(np.isnan(at_quote), ~filled))
            np.testing.assert_allclose(through[filled] + at_quote[filled], 1.0)
            self.assertTrue(np.isin(through[filled], (0.0, 1.0)).all())


@unittest.skipUnless(FRAMES_PRESENT, "model frames have not been built")
class FoldTests(unittest.TestCase):
    timestamps: np.ndarray
    folds: list

    @classmethod
    def setUpClass(cls) -> None:
        frame = data.load_model_frame(columns=["timestamp_ns", "segment_id", "file_index"])
        cls.timestamps = frame["timestamp_ns"].to_numpy()
        cls.folds = data.build_folds(cls.timestamps)

    def test_folds_are_chronological_expanding_and_purged(self):
        self.assertEqual(len(self.folds), spec.N_BLOCKS - spec.FIRST_VALIDATION_BLOCK)
        purge_ns = int(spec.PURGE_SECONDS * 1e9)
        previous_end = None
        for fold in self.folds:
            # No training row may come from the validation block or from the purge gap.
            self.assertEqual(
                fold.train_end_ns, fold.validation_start_ns - purge_ns, fold.index
            )
            self.assertLess(fold.train_end_ns, fold.validation_start_ns)
            self.assertLess(fold.validation_start_ns, fold.validation_end_ns)
            if previous_end is not None:
                self.assertGreaterEqual(fold.validation_start_ns, previous_end)
            previous_end = fold.validation_end_ns
            train = self.timestamps <= fold.train_end_ns
            validation = (self.timestamps >= fold.validation_start_ns) & (
                self.timestamps < fold.validation_end_ns
            )
            self.assertGreater(train.sum(), 0)
            self.assertGreater(validation.sum(), 0)
            self.assertEqual(int((train & validation).sum()), 0)
            self.assertLess(self.timestamps[train].max(), self.timestamps[validation].min())

    def test_training_targets_end_before_the_validation_block_opens(self):
        horizon_ns = int(spec.MAX_TARGET_HORIZON_SECONDS * 1e9)
        for fold in self.folds:
            latest_train = self.timestamps[self.timestamps <= fold.train_end_ns].max()
            self.assertLess(latest_train + horizon_ns, fold.validation_start_ns, fold.index)

    def test_validation_blocks_are_hours_long_and_cover_the_corpus_tail(self):
        table = data.fold_table(self.timestamps, self.folds)
        self.assertTrue((table["validation_hours"] > 1.0).all())
        self.assertTrue((table["validation_rows"] > 10_000).all())
        self.assertTrue(table["train_rows"].is_monotonic_increasing)

    def test_the_tiny_segment_is_never_a_standalone_validation_fold(self):
        frame = data.load_model_frame(
            columns=["timestamp_ns", "file_index", "segment_id"]
        )
        for fold in self.folds:
            block = frame[
                (frame["timestamp_ns"] >= fold.validation_start_ns)
                & (frame["timestamp_ns"] < fold.validation_end_ns)
            ]
            keys = set(zip(block["file_index"], block["segment_id"]))
            self.assertGreater(len(block), 10_000)
            self.assertNotEqual(keys, {(2, 3)}, "segment 2:3 used as a standalone fold")


@unittest.skipUnless(FRAMES_PRESENT, "model frames have not been built")
class DeterminismTests(unittest.TestCase):
    def test_repeated_runs_with_the_same_spec_produce_identical_predictions(self):
        frame = pd.read_parquet(data.frame_path(1))
        timestamps = frame["timestamp_ns"].to_numpy()
        folds = data.build_folds(timestamps)[:1]
        problem = price_problems()[2]
        first, first_metrics, _ = modeling.run_problem(problem, frame, folds)
        second, second_metrics, _ = modeling.run_problem(problem, frame, folds)
        if first.empty:
            self.skipTest("file 1 does not intersect the first validation block")
        pd.testing.assert_frame_equal(first, second)
        pd.testing.assert_frame_equal(first_metrics, second_metrics)


@unittest.skipUnless(OOF_SIDE.exists() and OOF_PRICE.exists(), "OOF predictions are missing")
class OutOfFoldTests(unittest.TestCase):
    keys: dict

    @classmethod
    def setUpClass(cls) -> None:
        # The side prediction file is hundreds of megabytes of compressed CSV; read the two key
        # columns once and share them across the tests rather than parsing it per assertion.
        cls.keys = {
            OOF_PRICE: pd.read_csv(OOF_PRICE, usecols=["timestamp_ns"]),
            OOF_SIDE: pd.read_csv(OOF_SIDE, usecols=["timestamp_ns", "side"]),
        }

    def test_every_prediction_lies_inside_a_validation_block(self):
        frame = data.load_model_frame(columns=["timestamp_ns"])
        folds = data.build_folds(frame["timestamp_ns"].to_numpy())
        windows = [(f.validation_start_ns, f.validation_end_ns) for f in folds]
        for path, oof in self.keys.items():
            stamps = oof["timestamp_ns"].to_numpy()
            inside = np.zeros(len(stamps), dtype=bool)
            for start, end in windows:
                inside |= (stamps >= start) & (stamps < end)
            self.assertTrue(inside.all(), path.name)
            # Nothing before the first validation block can ever have an OOF prediction.
            self.assertGreaterEqual(stamps.min(), windows[0][0])

    def test_out_of_fold_rows_are_unique_per_key(self):
        self.assertFalse(self.keys[OOF_SIDE].duplicated().any())
        self.assertFalse(self.keys[OOF_PRICE].duplicated().any())

    def test_reported_metrics_never_claim_out_of_sample(self):
        methodology = json.loads(
            (spec.REPORT_DIR / "methodology.json").read_text(encoding="utf-8")
        )
        self.assertEqual(methodology["validation"]["label"], "blocked OOF development estimates")
        for path in spec.REPORT_DIR.glob("*.csv"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("out_of_sample", text, path.name)


if __name__ == "__main__":
    unittest.main()
