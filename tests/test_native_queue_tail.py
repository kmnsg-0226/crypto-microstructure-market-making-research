"""Audit for the queue-dynamics and catastrophic-tail phase.

The new objects are level episodes, their causally accumulated lifecycle state, and a set of
downside targets. Each test here exists to falsify one specific way those could be wrong: an
episode that merges two appearances of the same price, cumulative state that has seen the
future, a target resolved across a segment edge, a placement feature that already knows the
fill, or an out-of-fold prediction that is not out of fold.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from pyresearch.native.economic import data as economic_data
from pyresearch.native.predictive import data as predictive_data
from pyresearch.native.queue_tail import analysis, data, spec

EPISODES_PRESENT = all(data.episodes_path(i).exists() for i in (0, 1, 2))
FRAME_PRESENT = data.frame_path().exists()
BIRTH_PRESENT = all(data.birth_fills_path(i).exists() for i in (0, 1, 2))
REPORTS_PRESENT = (spec.REPORT_DIR / "level_survival_summary.csv").exists()


class SpecTests(unittest.TestCase):
    def test_catastrophic_thresholds_are_fixed(self):
        self.assertEqual(spec.CATASTROPHIC_THRESHOLDS_TICKS, (10, 25, 50, 100))
        self.assertEqual(spec.PRIMARY_CATASTROPHIC_TICKS, 25)
        self.assertEqual(spec.SECONDARY_CATASTROPHIC_TICKS, 50)

    def test_methodology_refuses_the_forbidden_claims(self):
        methodology = spec.methodology({})
        terms = methodology["terminology"]
        self.assertTrue(terms["level_age_is_not_queue_rank"])
        self.assertTrue(terms["unexplained_removal_is_not_called_cancellation"])
        self.assertTrue(terms["level_birth_cohort_is_not_front_of_queue"])
        self.assertFalse(terms["claims_true_queue_position"])
        self.assertFalse(methodology["corpus"]["is_out_of_sample"])
        self.assertFalse(methodology["models"]["phase_2_toxicity_model_reused_as_headline"])
        self.assertIn("cancel, stay or requote rules", methodology["prohibited_in_this_phase"])

    def test_no_lifecycle_feature_is_named_as_a_queue_rank(self):
        for feature in spec.QUEUE_LIFECYCLE:
            for banned in ("queue_rank", "queue_position", "fifo", "cancel"):
                self.assertNotIn(banned, feature, feature)

    def test_placement_features_carry_no_fill_or_post_fill_state(self):
        forbidden = ("fill", "markout", "postfill", "mechanism", "catastroph", "time_to_fill")
        for name, columns in spec.FEATURE_SETS.items():
            for feature in columns:
                for token in forbidden:
                    self.assertNotIn(token, feature, f"{name}:{feature}")


@unittest.skipUnless(EPISODES_PRESENT, "level lifecycle replay has not been run")
class EpisodeTests(unittest.TestCase):
    episodes: pd.DataFrame

    @classmethod
    def setUpClass(cls) -> None:
        cls.episodes = data.load_episodes()

    def test_episodes_are_well_formed_and_ordered(self):
        self.assertGreater(len(self.episodes), 1000)
        self.assertTrue((self.episodes["end_ns"] >= self.episodes["start_ns"]).all())
        self.assertTrue((self.episodes["initial_qty"] > 0).all())
        self.assertFalse(
            self.episodes.duplicated(subset=["file_index", "level_episode_id"]).any()
        )

    def test_the_same_price_appearing_again_is_a_new_episode(self):
        repeated = self.episodes.groupby(
            ["file_index", "segment_id", "side", "price_ticks"], observed=True
        ).size()
        self.assertGreater(int((repeated > 1).sum()), 0)
        # Repeated appearances never share an identifier and never overlap in time.
        sample = self.episodes[
            self.episodes["side"] == 0
        ].sort_values(["file_index", "segment_id", "price_ticks", "start_ns"])
        for _, block in sample.groupby(
            ["file_index", "segment_id", "price_ticks"], observed=True
        ):
            if len(block) < 2:
                continue
            self.assertEqual(block["level_episode_id"].nunique(), len(block))
            self.assertTrue(
                (block["start_ns"].to_numpy()[1:] >= block["end_ns"].to_numpy()[:-1]).all()
            )

    def test_no_episode_crosses_a_segment_boundary(self):
        bounds = economic_data.segment_bounds().set_index(["file_index", "segment_id"])
        keys = pd.MultiIndex.from_arrays(
            [self.episodes["file_index"], self.episodes["segment_id"]]
        )
        start = keys.map(bounds["start_ns"]).to_numpy()
        end = keys.map(bounds["end_ns"]).to_numpy()
        self.assertTrue(np.isfinite(start.astype("float64")).all())
        self.assertTrue((self.episodes["start_ns"].to_numpy() >= start).all())
        self.assertTrue((self.episodes["end_ns"].to_numpy() <= end).all())

    def test_replenishment_requires_a_prior_reduction(self):
        never_reduced = self.episodes[self.episodes["remove_events"] == 0]
        self.assertGreater(len(never_reduced), 0)
        self.assertTrue((never_reduced["cum_replenish"] == 0).all())
        self.assertTrue((never_reduced["replenish_events"] == 0).all())
        # And replenished quantity can never exceed what was added.
        self.assertTrue(
            (self.episodes["cum_replenish"] <= self.episodes["cum_add"]).all()
        )

    def test_removal_reconciliation_is_conservative_and_complete(self):
        explained = self.episodes["cum_explained_remove"].to_numpy()
        unexplained = self.episodes["cum_unexplained_remove"].to_numpy()
        removed = self.episodes["cum_remove"].to_numpy()
        np.testing.assert_array_equal(explained + unexplained, removed)
        # A print can only ever account for quantity that was actually printed at the level.
        self.assertTrue((explained <= self.episodes["cum_trade_at_quote"].to_numpy()).all())
        self.assertTrue((explained >= 0).all())
        self.assertTrue((unexplained >= 0).all())

    def test_quantity_bookkeeping_closes(self):
        expected = (
            self.episodes["initial_qty"]
            + self.episodes["cum_add"]
            - self.episodes["cum_remove"]
        )
        np.testing.assert_array_equal(expected.to_numpy(), self.episodes["final_qty"].to_numpy())
        self.assertTrue((self.episodes["max_qty"] >= self.episodes["initial_qty"]).all())
        self.assertTrue((self.episodes["min_qty"] <= self.episodes["initial_qty"]).all())

    def test_close_reasons_are_from_the_declared_set(self):
        self.assertTrue(
            set(self.episodes["close_reason"])
            <= {"improved", "stepped_away", "book_side_empty", "segment_end", "end_of_input",
                "resynchronized"}
        )
        # A level the book stepped down through has been fully removed by construction.
        stepped = self.episodes[self.episodes["close_reason"] == "stepped_away"]
        self.assertGreater(len(stepped), 0)
        self.assertTrue(stepped["fully_removed"].all())


@unittest.skipUnless(FRAME_PRESENT, "model frame has not been built")
class FrameTests(unittest.TestCase):
    frame: pd.DataFrame

    @classmethod
    def setUpClass(cls) -> None:
        cls.frame = data.load_model_frame()

    def test_every_declared_feature_exists(self):
        for name, columns in spec.FEATURE_SETS.items():
            for feature in columns:
                self.assertIn(feature, self.frame.columns, f"{name}:{feature}")

    def test_rows_sit_on_the_declared_model_grid(self):
        step = int(spec.MODEL_GRID_MS * 1e6)
        self.assertTrue((self.frame["timestamp_ns"] % step == 0).all())
        self.assertFalse(self.frame.duplicated(subset=["timestamp_ns", "side"]).any())

    def test_lifecycle_state_is_causal_within_its_own_episode(self):
        """Cumulative state only ever grows forward inside an episode.

        The episode identifier restarts per raw file, so the episode key is the pair
        (file_index, level_episode_id) everywhere it is used.
        """
        work = self.frame.sort_values(["file_index", "level_episode_id", "timestamp_ns"])
        for column in (
            "log_cum_remove",
            "log_cum_add",
            "log_cum_trade_at_quote",
            "log_cum_replenish",
            "log_level_age_ms",
            "log_prints_at_quote",
            "log_prints_through",
        ):
            grouped = work.groupby(["file_index", "level_episode_id"], observed=True)[column]
            self.assertTrue(grouped.apply(lambda s: s.is_monotonic_increasing).all(), column)

    def test_episode_identifiers_are_only_unique_within_a_file(self):
        pairs = self.frame[["file_index", "level_episode_id"]].drop_duplicates()
        self.assertGreater(len(pairs), pairs["level_episode_id"].nunique())

    def test_no_survival_target_crosses_a_segment_boundary(self):
        remaining = (
            self.frame["segment_end_ns"].to_numpy() - self.frame["timestamp_ns"].to_numpy()
        )
        for horizon in spec.SURVIVAL_HORIZONS_MS:
            label = self.frame[f"level_disappears_{horizon}ms"].to_numpy()
            width = horizon * 1e6
            # A zero requires the full horizon to have been watched inside the segment.
            self.assertFalse(np.any((label == 0.0) & (remaining < width)), horizon)

    def test_a_segment_end_is_censoring_not_level_failure(self):
        censored = self.frame[self.frame["episode_close_reason"] == "segment_end"]
        if censored.empty:
            self.skipTest("no episode ended at a segment edge")
        for horizon in spec.SURVIVAL_HORIZONS_MS:
            label = censored[f"level_disappears_{horizon}ms"].to_numpy()
            self.assertFalse(np.any(label == 1.0), horizon)

    def test_no_sweep_target_crosses_a_segment_boundary(self):
        remaining = (
            self.frame["segment_end_ns"].to_numpy() - self.frame["timestamp_ns"].to_numpy()
        )
        for horizon in spec.SWEEP_HORIZONS_MS:
            observed = np.isfinite(
                self.frame[f"trade_through_within_{horizon}ms"].to_numpy()
            )
            self.assertTrue((remaining[observed] >= horizon * 1e6).all(), horizon)

    def test_sweep_targets_are_nested_in_horizon(self):
        widths = list(spec.SWEEP_HORIZONS_MS)
        for narrow, wide in zip(widths, widths[1:]):
            a = self.frame[f"trade_through_within_{narrow}ms"].to_numpy()
            b = self.frame[f"trade_through_within_{wide}ms"].to_numpy()
            both = np.isfinite(a) & np.isfinite(b)
            self.assertTrue((a[both] <= b[both]).all(), (narrow, wide))


@unittest.skipUnless(BIRTH_PRESENT and FRAME_PRESENT, "artifacts missing")
class TargetTests(unittest.TestCase):
    fills: pd.DataFrame

    @classmethod
    def setUpClass(cls) -> None:
        cls.fills = data.load_fills()

    def test_catastrophic_targets_exist_only_for_observed_fills(self):
        markout = self.fills[
            f"markout_{spec.PRIMARY_MARKOUT_MS}ms_ticks"
        ].to_numpy(dtype="float64")
        observed = np.isfinite(markout)
        for threshold in spec.CATASTROPHIC_THRESHOLDS_TICKS:
            label = self.fills[f"catastrophic_{threshold}"].to_numpy(dtype="float64")
            np.testing.assert_array_equal(np.isfinite(label), observed)
            np.testing.assert_array_equal(
                label[observed], (markout[observed] <= -threshold).astype("float64")
            )
        self.assertTrue(self.fills["filled"].to_numpy()[observed].all())

    def test_catastrophic_targets_are_nested_in_threshold(self):
        thresholds = list(spec.CATASTROPHIC_THRESHOLDS_TICKS)
        for smaller, larger in zip(thresholds, thresholds[1:]):
            a = self.fills[f"catastrophic_{smaller}"].to_numpy(dtype="float64")
            b = self.fills[f"catastrophic_{larger}"].to_numpy(dtype="float64")
            both = np.isfinite(a) & np.isfinite(b)
            self.assertTrue((b[both] <= a[both]).all(), (smaller, larger))

    def test_post_fill_markout_stays_inside_the_segment(self):
        fill = self.fills["fill_ns"].to_numpy()
        end = self.fills["segment_end_ns"].to_numpy()
        for horizon in spec.MARKOUT_HORIZONS_MS:
            observed = np.isfinite(self.fills[f"markout_{horizon}ms_ticks"].to_numpy())
            self.assertTrue((fill[observed] > 0).all(), horizon)
            self.assertTrue((fill[observed] + horizon * 1e6 <= end[observed]).all(), horizon)

    def test_severity_is_defined_only_on_the_severe_tail(self):
        markout = self.fills[
            f"markout_{spec.PRIMARY_MARKOUT_MS}ms_ticks"
        ].to_numpy(dtype="float64")
        severity = self.fills["severity_ticks"].to_numpy(dtype="float64")
        defined = np.isfinite(severity)
        self.assertTrue((markout[defined] <= -spec.SEVERITY_THRESHOLD_TICKS).all())
        np.testing.assert_allclose(severity[defined], -markout[defined])

    def test_level_birth_cohort_matches_the_episode_population(self):
        births = data.load_fills(level_birth=True)
        conservative = births[births["queue_cell"] == "conservative"]
        episodes = data.load_episodes()
        self.assertEqual(len(conservative), len(episodes))
        # Every placement carries the whole displayed quantity at the instant of level birth as
        # queue ahead: nothing displayed then is ever assumed to be behind the order.
        np.testing.assert_array_equal(
            conservative["queue_ahead_lots"].to_numpy(),
            conservative["displayed_qty_lots"].to_numpy(),
        )
        filled = conservative[conservative["filled"]]
        self.assertTrue(
            (filled["fill_ns"].to_numpy() > filled["placement_ns"].to_numpy()).all()
        )


@unittest.skipUnless(FRAME_PRESENT, "model frame has not been built")
class FoldTests(unittest.TestCase):
    def test_folds_match_the_phase_two_geometry_and_purge(self):
        from pyresearch.native.predictive import spec as predictive_spec

        stamps = predictive_data.load_model_frame(columns=["timestamp_ns"])[
            "timestamp_ns"
        ].to_numpy()
        folds = predictive_data.build_folds(stamps)
        self.assertEqual(
            len(folds), predictive_spec.N_BLOCKS - predictive_spec.FIRST_VALIDATION_BLOCK
        )
        purge = int(predictive_spec.PURGE_SECONDS * 1e9)
        frame = data.load_model_frame(columns=["timestamp_ns"])
        rows = frame["timestamp_ns"].to_numpy()
        for fold in folds:
            self.assertEqual(fold.train_end_ns, fold.validation_start_ns - purge)
            train = rows <= fold.train_end_ns
            validation = (rows >= fold.validation_start_ns) & (
                rows < fold.validation_end_ns
            )
            self.assertGreater(train.sum(), 0)
            self.assertGreater(validation.sum(), 0)
            self.assertEqual(int((train & validation).sum()), 0)
            self.assertLess(rows[train].max(), rows[validation].min())
            # The longest forward target attached to a training row ends before validation
            # opens, so no label can straddle the boundary.
            self.assertLess(
                rows[train].max() + predictive_spec.MAX_TARGET_HORIZON_SECONDS * 1e9,
                fold.validation_start_ns,
            )


@unittest.skipUnless(REPORTS_PRESENT, "descriptive artifacts have not been built")
class ArtifactTests(unittest.TestCase):
    def test_tail_contribution_shares_are_coherent(self):
        table = pd.read_csv(spec.REPORT_DIR / "tail_contribution.csv")
        self.assertFalse(table.empty)
        for share in ("1", "5", "10"):
            column = f"worst_{share}pct_share_of_negative"
            self.assertTrue((table[column] > 0).all())
            self.assertTrue((table[column] <= 1.0).all())
        self.assertTrue(
            (
                table["worst_1pct_share_of_negative"]
                <= table["worst_5pct_share_of_negative"]
            ).all()
        )
        self.assertTrue(
            (
                table["worst_5pct_share_of_negative"]
                <= table["worst_10pct_share_of_negative"]
            ).all()
        )

    def test_hazard_falls_with_level_age(self):
        table = pd.read_csv(spec.REPORT_DIR / "hazard_curve.csv")
        for _, block in table.groupby("side"):
            ordered = block.sort_values("age_bucket")
            hazard = ordered["hazard_100ms"].to_numpy()
            self.assertGreater(hazard[0], hazard[-1])

    def test_no_artifact_claims_out_of_sample_or_a_chosen_state(self):
        for path in spec.REPORT_DIR.glob("*.csv"):
            text = path.read_text(encoding="utf-8")
            for banned in ("out_of_sample", "optimal", "selected", "queue_rank"):
                self.assertNotIn(banned, text, f"{path.name}:{banned}")

    def test_methodology_records_input_hashes(self):
        methodology = json.loads(
            (spec.REPORT_DIR / "methodology.json").read_text(encoding="utf-8")
        )
        inputs = methodology["inputs"]
        self.assertRegex(inputs["git_commit"], r"^[0-9a-f]{40}$")
        for index in (0, 1, 2):
            self.assertRegex(
                inputs["level_episodes_sha256"][f"file{index}"], r"^[0-9a-f]{64}$"
            )


@unittest.skipUnless(EPISODES_PRESENT, "level lifecycle replay has not been run")
class ReplayTests(unittest.TestCase):
    def test_repeated_replay_is_byte_identical(self):
        import hashlib

        from pyresearch.native.core import corpus

        binary = spec.ROOT / "build/cpp/native_level_lifecycle"
        if not binary.exists():
            self.skipTest("native_level_lifecycle has not been built")
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()  # noqa: E731
        with tempfile.TemporaryDirectory() as directory:
            episodes = Path(directory) / "episodes.csv.zst"
            grid = Path(directory) / "grid.csv.zst"
            subprocess.run(
                [
                    str(binary),
                    str(corpus.CORPUS[0].raw_path),
                    "--episodes",
                    str(episodes),
                    "--grid",
                    str(grid),
                    "--file-index",
                    "0",
                ],
                cwd=spec.ROOT,
                capture_output=True,
                check=True,
            )
            self.assertEqual(digest(episodes), digest(data.episodes_path(0)))
            self.assertEqual(digest(grid), digest(data.grid_path(0)))


if __name__ == "__main__":
    unittest.main()
