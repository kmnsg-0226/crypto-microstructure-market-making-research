"""Tests for the directional sweep / execution-feasibility phase.

Most of what can go wrong here is a sign or a clock: normalising the wrong way round for one
side, letting a target reach past a segment edge, or timing a signal against something it
already saw. Those are what these tests pin down.
"""
from __future__ import annotations

import json
import unittest

import numpy as np
import pandas as pd

from pyresearch.native.cancel import scoring as cancel_scoring
from pyresearch.native.directional import analysis, data, events, models, signal, spec
from pyresearch.native.predictive import data as predictive_data
from pyresearch.native.queue_tail import spec as qt_spec

MS = int(1e6)


def make_mid(points, file_index=0, segment_id=1):
    return pd.DataFrame(
        {
            "ns": [int(t * MS) for t, _ in points],
            "file_index": file_index,
            "segment_id": segment_id,
            "mid_x2": [int(m * 2) for _, m in points],
        }
    )


# ------------------------------------------------------------------------------------------
class PreRegistrationTests(unittest.TestCase):
    def test_grids_are_the_pre_registered_ones(self):
        self.assertEqual(spec.SWEEP_THRESHOLDS, (0.30, 0.50, 0.70, 0.90))
        self.assertEqual(spec.HEADLINE_HORIZONS_MS, (100, 250, 500, 1000))
        self.assertEqual(spec.PRIMARY_HORIZON_MS, 500)
        self.assertEqual(spec.SECONDARY_HORIZON_MS, 1000)
        self.assertEqual(spec.COST_HURDLE_BPS, (1.0, 2.0, 3.0, 5.0, 7.5, 10.0))
        self.assertEqual(spec.TICK_THRESHOLDS, (1, 5, 10, 25, 50))

    def test_the_sweep_model_is_the_frozen_phase_4a_one(self):
        self.assertEqual(spec.SWEEP_TARGET, "trade_through_within_500ms")
        self.assertEqual(spec.SWEEP_HORIZON_MS, 500)
        self.assertEqual(spec.SWEEP_FEATURE_SET, qt_spec.PRIMARY_FEATURE_SET)
        method = spec.methodology({})
        self.assertFalse(method["signal"]["retrained_for_this_phase"])
        self.assertFalse(method["signal"]["hyper_parameters_changed"])
        self.assertFalse(method["signal"]["in_sample_predictions_used"])

    def test_no_threshold_horizon_or_cell_is_selected_from_results(self):
        method = spec.methodology({})
        self.assertTrue(method["grids"]["fixed_before_any_result"])
        self.assertFalse(method["grids"]["threshold_or_decile_selected"])
        self.assertFalse(method["grids"]["headline_horizon_replaced_after_seeing_results"])

    def test_cost_hurdles_are_labelled_sensitivities_not_a_live_fee_tier(self):
        method = spec.methodology({})
        self.assertFalse(method["cost_hurdles"]["is_a_live_account_fee_tier"])
        self.assertIn("sensitivity", method["cost_hurdles"]["label"])
        self.assertFalse(method["cost_hurdles"]["used_as_a_model_feature"])

    def test_methodology_never_claims_out_of_sample(self):
        method = spec.methodology({})
        self.assertFalse(method["corpus"]["is_out_of_sample"])
        self.assertTrue(method["corpus"]["development_only"])
        self.assertIn("blocked OOF", method["validation"]["label"])
        self.assertFalse(method["validation"]["random_splits_used"])
        self.assertFalse(method["direction"]["symmetry_assumed"])


# ------------------------------------------------------------------------------------------
class SignConventionTests(unittest.TestCase):
    def test_a_threatened_ask_points_up_and_a_threatened_bid_points_down(self):
        self.assertEqual(spec.SWEEP_DIRECTION[1], +1)
        self.assertEqual(spec.SWEEP_DIRECTION[0], -1)

    def test_side_normalisation_is_a_mirror(self):
        mid = data.MidSeries(make_mid([(0, 100.0), (500, 110.0)]))
        bounds = pd.DataFrame(
            {"file_index": [0], "segment_id": [1], "start_ns": [0], "end_ns": [60_000 * MS]}
        )
        frame = _skeleton(mid, bounds)
        data._add_targets(frame, mid, bounds)
        ask = frame[frame["side"] == 1].iloc[0]
        bid = frame[frame["side"] == 0].iloc[0]
        # The mid rose ten ticks: good for a threatened ask, bad for a threatened bid.
        self.assertAlmostEqual(float(ask["directional_markout_500ms"]), 10.0)
        self.assertAlmostEqual(float(bid["directional_markout_500ms"]), -10.0)
        self.assertAlmostEqual(
            float(ask["directional_markout_500ms"]), -float(bid["directional_markout_500ms"])
        )

    def test_the_pooled_directional_markout_of_a_mirrored_pair_is_zero(self):
        mid = data.MidSeries(make_mid([(0, 100.0), (500, 87.0)]))
        bounds = pd.DataFrame(
            {"file_index": [0], "segment_id": [1], "start_ns": [0], "end_ns": [60_000 * MS]}
        )
        frame = _skeleton(mid, bounds)
        data._add_targets(frame, mid, bounds)
        self.assertAlmostEqual(float(frame["directional_markout_500ms"].sum()), 0.0)

    def test_first_move_magnitude_carries_the_same_sign_as_the_frozen_direction(self):
        mid = data.MidSeries(make_mid([(0, 100.0), (30, 104.0), (500, 90.0)]))
        bounds = pd.DataFrame(
            {"file_index": [0], "segment_id": [1], "start_ns": [0], "end_ns": [60_000 * MS]}
        )
        frame = _skeleton(mid, bounds, next_dir=1.0, next_ms=30.0)
        data._add_targets(frame, mid, bounds)
        ask = frame[frame["side"] == 1].iloc[0]
        self.assertAlmostEqual(float(ask["first_mid_move_ticks"]), 4.0)
        self.assertAlmostEqual(float(ask["signed_ticks_on_first_mid_move"]), 4.0)
        self.assertAlmostEqual(float(ask["first_mid_move_ms"]), 30.0)
        bid = frame[frame["side"] == 0].iloc[0]
        self.assertAlmostEqual(float(bid["signed_ticks_on_first_mid_move"]), -4.0)


def _skeleton(mid, bounds, next_dir=np.nan, next_ms=np.nan) -> pd.DataFrame:
    rows = []
    for side in (0, 1):
        row = {
            "timestamp_ns": 0,
            "file_index": 0,
            "segment_id": 1,
            "side": side,
            "sweep_p": 0.5,
            "next_mid_move_dir": next_dir,
            "time_to_next_mid_move_ms": next_ms,
            "mid_ticks_frozen": 100.0,
        }
        for horizon in spec.FROZEN_HORIZONS_MS:
            row[f"markout_{horizon}ms_ticks"] = np.nan
        for horizon in (100, 250, 500, 1000, 5000):
            row[f"trade_through_within_{horizon}ms"] = 0.0
        rows.append(row)
    frame = pd.DataFrame(rows)
    # The frozen markout columns are what the phase normally uses; here the reconstruction is
    # the only source, so they are filled from the mid path exactly as production would.
    for horizon in spec.FROZEN_HORIZONS_MS:
        when = np.zeros(len(frame), dtype="int64") + int(horizon * MS)
        value = mid.mid_at(
            frame["file_index"].to_numpy(), frame["segment_id"].to_numpy(), when
        )
        frame[f"markout_{horizon}ms_ticks"] = value - 100.0
    return frame


# ------------------------------------------------------------------------------------------
class CensoringTests(unittest.TestCase):
    def test_a_horizon_that_leaves_the_segment_is_censored_not_zero(self):
        mid = data.MidSeries(make_mid([(0, 100.0), (400, 105.0)]))
        bounds = pd.DataFrame(
            {"file_index": [0], "segment_id": [1], "start_ns": [0], "end_ns": [300 * MS]}
        )
        frame = _skeleton(mid, bounds)
        data._add_targets(frame, mid, bounds)
        self.assertTrue(np.isfinite(frame["directional_markout_100ms"]).all())
        self.assertTrue(frame["directional_markout_500ms"].isna().all())
        self.assertTrue(frame["directional_markout_1000ms"].isna().all())

    def test_the_first_move_is_censored_at_the_segment_edge(self):
        mid = data.MidSeries(make_mid([(0, 100.0), (900, 105.0)]))
        bounds = pd.DataFrame(
            {"file_index": [0], "segment_id": [1], "start_ns": [0], "end_ns": [500 * MS]}
        )
        frame = _skeleton(mid, bounds)
        data._add_targets(frame, mid, bounds)
        self.assertTrue(frame["first_mid_move_ticks"].isna().all())


# ------------------------------------------------------------------------------------------
class FirstPassageTests(unittest.TestCase):
    def test_next_extreme_finds_the_first_strictly_greater_value(self):
        values = np.array([5, 3, 4, 9, 1], dtype="int64")
        self.assertEqual(list(signal._next_extreme(values, greater=True)), [3, 2, 3, -1, -1])
        self.assertEqual(list(signal._next_extreme(values, greater=False)), [1, 4, 4, 4, -1])

    def test_first_passage_is_strictly_after_the_query_and_in_the_right_direction(self):
        passage = signal.FirstPassage(
            make_mid([(0, 100.0), (10, 99.0), (20, 101.0), (30, 98.0)])
        )
        file_index = np.zeros(2, dtype="int64")
        segment = np.ones(2, dtype="int64")
        query = np.zeros(2, dtype="int64")
        up = passage.time_to_move(file_index, segment, query, np.array([1.0, 1.0]))
        down = passage.time_to_move(file_index, segment, query, np.array([-1.0, -1.0]))
        # From mid 100 at t=0: the first higher mid is 101 at 20 ms, the first lower is 99 at
        # 10 ms. A move at the query instant itself is never counted.
        self.assertAlmostEqual(float(up[0]), 20.0)
        self.assertAlmostEqual(float(down[0]), 10.0)

    def test_a_direction_that_never_resolves_inside_the_segment_is_censored(self):
        passage = signal.FirstPassage(make_mid([(0, 100.0), (10, 99.0), (20, 98.0)]))
        value = passage.time_to_move(
            np.zeros(1, dtype="int64"), np.ones(1, dtype="int64"),
            np.zeros(1, dtype="int64"), np.array([1.0]),
        )
        self.assertTrue(np.isnan(value[0]))


# ------------------------------------------------------------------------------------------
class EventStudyTests(unittest.TestCase):
    def test_event_paths_are_measured_from_the_event_instant_in_receive_time(self):
        mid = data.MidSeries(make_mid([(0, 100.0), (1000, 100.0), (1100, 106.0)]))
        frame = pd.DataFrame(
            {
                "event_ns": [1000 * MS],
                "file_index": [0],
                "segment_id": [1],
                "side": [1],
                "event_class": ["aggressive_trade_through"],
                "start_ns": [0],
                "end_ns": [60_000 * MS],
            }
        )
        paths = events.event_paths(mid, frame)
        self.assertAlmostEqual(float(paths["tau_0ms"].iloc[0]), 0.0)
        self.assertAlmostEqual(float(paths["tau_-500ms"].iloc[0]), 0.0)
        self.assertAlmostEqual(float(paths["tau_250ms"].iloc[0]), 6.0)

    def test_a_tau_outside_the_segment_is_censored(self):
        mid = data.MidSeries(make_mid([(0, 100.0), (1000, 104.0)]))
        frame = pd.DataFrame(
            {
                "event_ns": [1000 * MS],
                "file_index": [0],
                "segment_id": [1],
                "side": [1],
                "event_class": ["aggressive_trade_through"],
                "start_ns": [900 * MS],
                "end_ns": [1200 * MS],
            }
        )
        paths = events.event_paths(mid, frame)
        self.assertTrue(np.isnan(paths["tau_-500ms"].iloc[0]))
        self.assertTrue(np.isnan(paths["tau_500ms"].iloc[0]))
        self.assertTrue(np.isfinite(paths["tau_100ms"].iloc[0]))

    def test_a_threatened_bid_event_normalises_the_other_way(self):
        mid = data.MidSeries(make_mid([(0, 100.0), (1000, 100.0), (1100, 94.0)]))
        frame = pd.DataFrame(
            {
                "event_ns": [1000 * MS, 1000 * MS],
                "file_index": [0, 0],
                "segment_id": [1, 1],
                "side": [0, 1],
                "event_class": ["aggressive_trade_through"] * 2,
                "start_ns": [0, 0],
                "end_ns": [60_000 * MS] * 2,
            }
        )
        paths = events.event_paths(mid, frame)
        self.assertAlmostEqual(float(paths["tau_250ms"].iloc[0]), 6.0)
        self.assertAlmostEqual(float(paths["tau_250ms"].iloc[1]), -6.0)


# ------------------------------------------------------------------------------------------
class DenominatorTests(unittest.TestCase):
    def setUp(self):
        rows = []
        for index in range(200):
            rows.append(
                {
                    "timestamp_ns": index * 100 * MS,
                    "file_index": 0,
                    "segment_id": 1,
                    "side": index % 2,
                    "sweep_p": (index % 20) / 20.0,
                    "mid_ticks": 100000.0,
                    "trade_through_within_500ms": float(index % 3 == 0),
                    "directional_markout_500ms": float((index % 7) - 3),
                }
            )
        self.frame = pd.DataFrame(rows)
        for horizon in (100, 250, 1000):
            self.frame[f"trade_through_within_{horizon}ms"] = self.frame[
                "trade_through_within_500ms"
            ]
            self.frame[f"directional_markout_{horizon}ms"] = self.frame[
                "directional_markout_500ms"
            ]

    def test_conditional_and_unconditional_are_separate_labelled_rows(self):
        table = analysis.conditional_split(
            self.frame.assign(group="all"), 500, ["group"]
        )
        self.assertEqual(
            set(table["population"]),
            {"unconditional", "sweep_occurred", "sweep_did_not_occur"},
        )
        unconditional = table[table["population"] == "unconditional"].iloc[0]
        self.assertEqual(int(unconditional["opportunities"]), len(self.frame))
        # The two conditional populations must partition the unconditional one exactly.
        conditional = table[table["population"] != "unconditional"]["opportunities"].sum()
        self.assertEqual(int(conditional), len(self.frame))

    def test_a_high_score_population_keeps_its_share_of_the_denominator(self):
        table = analysis.threshold_table(self.frame)
        base = table[table["population"] == "all"].iloc[0]
        self.assertEqual(int(base["observations"]), len(self.frame))
        for _, row in table[table["population"] == "score_at_or_above"].iterrows():
            self.assertAlmostEqual(
                float(row["share_of_opportunities"]),
                row["observations"] / len(self.frame),
            )

    def test_the_unconditional_row_is_not_the_conditional_one(self):
        table = analysis.conditional_split(self.frame.assign(group="all"), 500, ["group"])
        unconditional = table[table["population"] == "unconditional"]["markout_mean_ticks"]
        occurred = table[table["population"] == "sweep_occurred"]["markout_mean_ticks"]
        self.assertNotAlmostEqual(float(unconditional.iloc[0]), float(occurred.iloc[0]))


# ------------------------------------------------------------------------------------------
class CostHurdleTests(unittest.TestCase):
    def test_the_hurdle_is_never_a_model_feature(self):
        for name in spec.MODEL_SETS:
            columns = data.model_features(name)
            for column in columns:
                self.assertNotIn("hurdle", column)
                self.assertNotIn("bps", column)
                self.assertNotIn("cost", column)
                self.assertNotIn("fee", column)

    def test_every_fixed_hurdle_is_reported_for_every_row(self):
        table = pd.DataFrame(
            [{"threshold": 0.5, "observations": 10, "markout_500ms_mean_bps": 0.8,
              "markout_500ms_mean_ticks": 55.0}]
        )
        out = analysis.cost_hurdle(table, ["threshold"], 500)
        self.assertEqual(len(out), len(spec.COST_HURDLE_BPS))
        self.assertEqual(sorted(out["one_way_hurdle_bps"]), sorted(spec.COST_HURDLE_BPS))
        self.assertFalse(bool(out["clears_one_way"].any()))
        self.assertTrue(
            bool((out["net_of_round_trip_hurdle_bps"] < out["net_of_one_way_hurdle_bps"]).all())
        )

    def test_break_even_is_the_gross_movement_itself(self):
        table = pd.DataFrame(
            [{"threshold": 0.9, "observations": 10, "markout_500ms_mean_bps": 0.81,
              "markout_500ms_mean_ticks": 56.4}]
        )
        out = analysis.break_even(table, ["threshold"], 500).iloc[0]
        self.assertAlmostEqual(float(out["required_break_even_ticks"]), 56.4)
        self.assertAlmostEqual(
            float(out["required_break_even_usd_per_btc"]), 56.4 * spec.TICK_SIZE_USD
        )
        self.assertAlmostEqual(float(out["required_break_even_bps_one_way"]), 0.81)
        self.assertAlmostEqual(float(out["required_break_even_bps_round_trip"]), 0.405)
        self.assertEqual(out["magnitude_band"], "<1bp")


# ------------------------------------------------------------------------------------------
class DeterminismTests(unittest.TestCase):
    def test_repeated_target_construction_is_identical(self):
        mid = data.MidSeries(make_mid([(0, 100.0), (120, 103.0), (700, 96.0)]))
        bounds = pd.DataFrame(
            {"file_index": [0], "segment_id": [1], "start_ns": [0], "end_ns": [60_000 * MS]}
        )
        first = _skeleton(mid, bounds, next_dir=1.0, next_ms=120.0)
        second = _skeleton(mid, bounds, next_dir=1.0, next_ms=120.0)
        data._add_targets(first, mid, bounds)
        data._add_targets(second, mid, bounds)
        pd.testing.assert_frame_equal(first, second)

    def test_decile_assignment_is_deterministic(self):
        scores = np.linspace(0.0, 1.0, 1000)
        first = analysis.assign_deciles(scores)
        second = analysis.assign_deciles(scores.copy())
        self.assertTrue(bool(np.array_equal(first, second)))


# ------------------------------------------------------------------------------------------
class ArtifactTests(unittest.TestCase):
    """Checks against the real artifacts, skipped when they have not been produced."""

    @classmethod
    def setUpClass(cls):
        if not data.frame_path().exists():
            raise unittest.SkipTest("phase 5A artifacts not built")
        cls.frame = data.load_frame(
            columns=[
                "sweep_p", "fold", "sweep_direction", "segment_end_ns", "mid_ticks",
                "directional_markout_500ms", "markout_500ms_ticks",
            ]
        )

    def test_only_blocked_out_of_fold_sweep_scores_are_used(self):
        folds = {fold.index: fold for fold in models.directional_folds()}
        folds.update(
            {
                fold.index: fold
                for fold in predictive_data.build_folds(
                    predictive_data.load_model_frame(columns=["timestamp_ns"])[
                        "timestamp_ns"
                    ].to_numpy()
                )
            }
        )
        for index, block in self.frame.groupby("fold"):
            fold = folds[int(index)]
            stamps = block["timestamp_ns"].to_numpy()
            self.assertGreaterEqual(int(stamps.min()), fold.validation_start_ns)
            self.assertLess(int(stamps.max()), fold.validation_end_ns)
            self.assertGreater(
                fold.validation_start_ns - fold.train_end_ns, spec.SWEEP_HORIZON_MS * MS
            )

    def test_the_scores_are_the_ones_phase_4b_published(self):
        scores = cancel_scoring.load_scores()[
            ["timestamp_ns", "file_index", "segment_id", "side", "sweep_p"]
        ]
        merged = self.frame[
            ["timestamp_ns", "file_index", "segment_id", "side", "sweep_p"]
        ].merge(scores, on=["timestamp_ns", "file_index", "segment_id", "side"])
        self.assertEqual(len(merged), len(self.frame))
        self.assertTrue(
            bool(np.allclose(merged["sweep_p_x"].to_numpy(), merged["sweep_p_y"].to_numpy()))
        )

    def test_the_directional_model_folds_start_one_block_after_the_scores(self):
        directional = models.directional_folds()
        every = predictive_data.build_folds(
            predictive_data.load_model_frame(columns=["timestamp_ns"])["timestamp_ns"].to_numpy()
        )
        self.assertEqual(len(directional), len(every) - 1)
        self.assertEqual(min(fold.block for fold in directional), spec.FIRST_VALIDATION_BLOCK + 1)

    def test_directional_sign_matches_the_side(self):
        direction = self.frame["sweep_direction"].to_numpy()
        side = self.frame["side"].to_numpy()
        self.assertTrue(bool(np.all(direction[side == 1] == 1)))
        self.assertTrue(bool(np.all(direction[side == 0] == -1)))
        absolute = self.frame["markout_500ms_ticks"].to_numpy(dtype="float64")
        signed = self.frame["directional_markout_500ms"].to_numpy(dtype="float64")
        keep = np.isfinite(absolute) & np.isfinite(signed)
        self.assertTrue(bool(np.allclose(signed[keep], direction[keep] * absolute[keep])))

    def test_the_pooled_directional_markout_is_zero_by_construction(self):
        # Every instant contributes a bid row and an ask row, so a side-normalised markout can
        # only be a redistribution. A non-zero pooled mean would mean the sides had drifted apart.
        value = float(self.frame["directional_markout_500ms"].sum())
        self.assertLess(abs(value), 1e-6)

    def test_no_target_reaches_past_a_segment_edge(self):
        frame = data.load_frame(
            columns=["segment_end_ns", "first_mid_move_ms"]
            + [f"directional_markout_{h}ms" for h in spec.ALL_HORIZONS_MS]
        )
        stamps = frame["timestamp_ns"].to_numpy()
        end = frame["segment_end_ns"].to_numpy()
        for horizon in spec.ALL_HORIZONS_MS:
            observed = np.isfinite(frame[f"directional_markout_{horizon}ms"].to_numpy())
            self.assertTrue(bool(np.all(stamps[observed] + horizon * MS <= end[observed])))
        move = frame["first_mid_move_ms"].to_numpy(dtype="float64")
        observed = np.isfinite(move)
        self.assertTrue(
            bool(np.all(stamps[observed] + (move[observed] * MS).astype("int64") <= end[observed]))
        )

    def test_the_reconstruction_agrees_with_the_frozen_markouts(self):
        qc = json.loads((spec.REPORT_DIR / "frame_qc.json").read_text())
        for horizon in spec.FROZEN_HORIZONS_MS:
            entry = qc["reconstruction"][f"{horizon}ms"]
            self.assertEqual(entry["censoring_disagreements"], 0)
            # A handful of rows resolve one event apart inside violent moves; anything more
            # would mean the mid path and the phase 1 exporter disagree about the book.
            self.assertLess(entry["rows_differing"] / entry["compared_rows"], 1e-4)
        self.assertLess(
            qc["first_move"]["direction_disagreements"] / qc["first_move"]["compared_rows"],
            1e-5,
        )

    def test_false_positives_are_kept_in_the_evaluation(self):
        table = pd.read_csv(spec.REPORT_DIR / "false_positive_analysis.csv")
        self.assertEqual(
            set(table["population"]),
            {"true_positive_consumed", "false_positive_not_consumed"},
        )
        for threshold, block in table.groupby("threshold"):
            self.assertAlmostEqual(float(block["share_of_crossings"].sum()), 1.0, places=6)
            self.assertEqual(len(block), 2)

    def test_the_published_grid_is_the_pre_registered_grid(self):
        hurdle = pd.read_csv(spec.REPORT_DIR / "cost_hurdle.csv")
        self.assertEqual(
            sorted(hurdle["one_way_hurdle_bps"].unique()), list(spec.COST_HURDLE_BPS)
        )
        edge = pd.read_csv(spec.REPORT_DIR / "gross_edge.csv")
        published = sorted(edge["threshold"].dropna().round(2).unique())
        self.assertEqual(published, list(spec.SWEEP_THRESHOLDS))
        self.assertIn("all", set(edge["population"]))

    def test_lead_time_is_reported_for_every_threshold_and_both_measures(self):
        table = pd.read_csv(spec.REPORT_DIR / "signal_lead_time.csv")
        self.assertEqual(
            sorted(table["threshold"].round(2).unique()), list(spec.SWEEP_THRESHOLDS)
        )
        self.assertEqual(
            set(table["measure"]),
            {"lead_to_same_direction_move_ms", "lead_to_consumption_ms"},
        )
        # A first passage is strictly after the crossing, so it can never be non-positive.
        move = table[table["measure"] == "lead_to_same_direction_move_ms"]
        self.assertTrue(bool((move["frac_le_0ms"].fillna(0) == 0).all()))
