"""Tests for the cancel / stay falsification study.

The counterfactual is exact rather than simulated, so almost everything worth testing is a
statement about ordering in time: which information a decision was allowed to see, and which
executions a decision taken at one instant is allowed to remove. Those are the tests here.
"""
from __future__ import annotations

import json
import unittest

import numpy as np
import pandas as pd

from pyresearch.native.cancel import analysis, counterfactual, scoring, spec
from pyresearch.native.predictive import data as predictive_data
from pyresearch.native.queue_tail import spec as qt_spec

MS = int(1e6)


def make_scores(times_ms, probabilities, price=100, side=0, segment=1, file_index=2):
    return pd.DataFrame(
        {
            "timestamp_ns": [int(t * MS) for t in times_ms],
            "file_index": file_index,
            "segment_id": segment,
            "side": side,
            "quote_price_ticks": price,
            "sweep_p": list(probabilities),
        }
    )


def make_orders(rows):
    """rows: (placement_ms, fill_ms or None, markout, side, price, queue_cell)."""
    frame = pd.DataFrame(
        rows,
        columns=["placement_ms", "fill_ms", "markout", "side", "quote_px_ticks", "queue_cell"],
    )
    frame["placement_ns"] = (frame["placement_ms"] * MS).astype("int64")
    frame["fill_ns"] = (frame["fill_ms"].fillna(0) * MS).astype("int64")
    frame["filled"] = frame["fill_ms"].notna()
    frame["observed_end_ns"] = frame["placement_ns"] + 30_000 * MS
    frame["segment_end_ns"] = frame["placement_ns"] + 60_000 * MS
    frame["file_index"] = 2
    frame["segment_id"] = 1
    frame["mechanism"] = np.where(frame["filled"], 1, 0)
    frame["mechanism_name"] = np.where(frame["filled"], "at_quote", "unfilled")
    frame["time_to_fill_ms"] = np.where(
        frame["filled"], (frame["fill_ns"] - frame["placement_ns"]) / 1e6, np.nan
    )
    frame["alpha_pct"] = 100
    frame["beta_pct"] = 0
    for horizon in spec.MARKOUT_HORIZONS_MS:
        frame[f"markout_{horizon}ms_ticks"] = np.where(
            frame["filled"], frame["markout"], np.nan
        )
    for ticks in spec.CATASTROPHIC_THRESHOLDS_TICKS:
        frame[f"catastrophic_{ticks}"] = np.where(
            frame["filled"], (frame["markout"] <= -ticks).astype(float), np.nan
        )
    return frame.drop(columns=["placement_ms", "fill_ms", "markout"])


def timeline_for(orders, scores):
    return counterfactual.decision_timeline(counterfactual.opportunities(orders), scores)


# ------------------------------------------------------------------------------------------
class PreRegistrationTests(unittest.TestCase):
    def test_grids_are_the_pre_registered_ones(self):
        self.assertEqual(
            spec.CANCEL_THRESHOLDS, (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
        )
        self.assertEqual(spec.CANCEL_LATENCIES_MS, (0, 25, 50, 100))
        self.assertEqual(
            spec.QUEUE_CELLS,
            {"conservative": (100, 0), "midpoint": (50, 50), "optimistic": (0, 100)},
        )
        self.assertEqual(len(spec.grid_spec()["headline_cells"]), 27)

    def test_queue_assumptions_match_the_frozen_phase_4a_cells(self):
        self.assertEqual(spec.QUEUE_CELLS, qt_spec.QUEUE_CELLS)

    def test_signal_is_the_phase_4a_sweep_model_not_the_phase_2_toxicity_model(self):
        self.assertEqual(spec.SWEEP_TARGET, "trade_through_within_500ms")
        self.assertIn(spec.SWEEP_TARGET, [f"trade_through_within_{h}ms" for h in qt_spec.SWEEP_MODEL_HORIZONS_MS])
        self.assertEqual(spec.SWEEP_FEATURE_SET, qt_spec.PRIMARY_FEATURE_SET)
        method = spec.methodology({})
        self.assertFalse(method["signal"]["phase_2_toxicity_model_used"])
        self.assertFalse(method["grids"]["best_cell_selected"])
        self.assertFalse(method["falsification"]["relaxed_after_seeing_results"])

    def test_methodology_never_claims_out_of_sample_or_includes_fees(self):
        method = spec.methodology({})
        self.assertFalse(method["corpus"]["is_out_of_sample"])
        self.assertTrue(method["corpus"]["development_only"])
        excluded = " ".join(method["outcomes"]["excluded_by_design"])
        for term in ("fee", "rebate", "inventory", "opportunity cost"):
            self.assertIn(term, excluded)
        self.assertFalse(method["intervention"]["requote"])
        self.assertFalse(method["intervention"]["re_entry"])

    def test_training_and_scoring_cadence_are_declared_separately(self):
        method = spec.methodology({})
        self.assertEqual(method["signal"]["trained_on_grid_ms"], qt_spec.MODEL_GRID_MS)
        self.assertEqual(method["signal"]["scored_on_grid_ms"], qt_spec.LIFECYCLE_GRID_MS)
        self.assertTrue(method["signal"]["training_rows_unchanged_from_phase_4a"])


# ------------------------------------------------------------------------------------------
class CausalOrderingTests(unittest.TestCase):
    def test_decision_instants_are_strictly_after_placement(self):
        scores = make_scores([1000, 1100, 1200], [0.9, 0.9, 0.9])
        orders = make_orders([(1000, None, np.nan, 0, 100, "conservative")])
        timeline = timeline_for(orders, scores)
        # The score stamped at the placement instant itself is not a decision instant: acting on
        # it would be a quote / no-quote decision, which this phase does not study.
        self.assertEqual(int(timeline["decision_instants"].iloc[0]), 2)
        self.assertEqual(
            int(timeline[counterfactual._crossing_column(0.9)].iloc[0]), 1100 * MS
        )

    def test_a_fill_at_the_effective_instant_survives(self):
        scores = make_scores([1000, 1100], [0.1, 0.9])
        orders = make_orders([(1000, 1100, -50.0, 0, 100, "conservative")])
        applied = counterfactual.apply_cancel(orders, timeline_for(orders, scores), 0.9, 0)
        # Same-event rule: the information that produced the score at 1100 ms cannot retract an
        # execution stamped at 1100 ms.
        self.assertFalse(bool(applied["fill_prevented"].iloc[0]))
        self.assertTrue(bool(applied["cancel_too_late"].iloc[0]))
        self.assertTrue(bool(applied["surviving_fill"].iloc[0]))

    def test_a_fill_strictly_after_the_effective_instant_is_prevented(self):
        scores = make_scores([1000, 1100], [0.1, 0.9])
        orders = make_orders([(1000, 1101, -50.0, 0, 100, "conservative")])
        applied = counterfactual.apply_cancel(orders, timeline_for(orders, scores), 0.9, 0)
        self.assertTrue(bool(applied["fill_prevented"].iloc[0]))
        self.assertFalse(bool(applied["cancel_too_late"].iloc[0]))
        self.assertFalse(bool(applied["surviving_fill"].iloc[0]))

    def test_cancellation_cannot_prevent_a_fill_that_already_happened(self):
        scores = make_scores([1000, 1100, 1200], [0.1, 0.1, 0.9])
        orders = make_orders([(1000, 1050, -80.0, 0, 100, "conservative")])
        applied = counterfactual.apply_cancel(orders, timeline_for(orders, scores), 0.9, 0)
        self.assertTrue(bool(applied["cancel_too_late"].iloc[0]))
        self.assertFalse(bool(applied["fill_prevented"].iloc[0]))
        self.assertLess(float(applied["lead_time_ms"].iloc[0]), 0.0)

    def test_effective_cancel_time_is_decision_time_plus_the_fixed_latency(self):
        scores = make_scores([1000, 1100], [0.1, 0.9])
        orders = make_orders([(1000, 1160, -30.0, 0, 100, "conservative")])
        timeline = timeline_for(orders, scores)
        for latency, prevented in ((0, True), (25, True), (50, True), (100, False)):
            applied = counterfactual.apply_cancel(orders, timeline, 0.9, latency)
            self.assertEqual(
                int(applied["effective_cancel_ns"].iloc[0]), (1100 + latency) * MS
            )
            self.assertEqual(bool(applied["fill_prevented"].iloc[0]), prevented, latency)

    def test_latency_never_creates_protection_it_only_removes_it(self):
        scores = make_scores(list(range(1000, 1300, 100)), [0.1, 0.95, 0.95])
        orders = make_orders(
            [(1000, fill, -30.0, 0, 100, "conservative") for fill in (1120, 1180, 1260)]
        )
        timeline = timeline_for(orders, scores)
        previous = None
        for latency in spec.CANCEL_LATENCIES_MS:
            prevented = counterfactual.apply_cancel(orders, timeline, 0.9, latency)[
                "fill_prevented"
            ].to_numpy()
            if previous is not None:
                self.assertTrue(bool(np.all(prevented <= previous)))
            previous = prevented

    def test_a_score_from_another_level_is_never_used(self):
        # Same instant, same side, different price: the order's level has no score of its own.
        scores = make_scores([1100], [0.99], price=101)
        orders = make_orders([(1000, 1200, -60.0, 0, 100, "conservative")])
        timeline = timeline_for(orders, scores)
        self.assertEqual(int(timeline["decision_instants"].iloc[0]), 0)
        applied = counterfactual.apply_cancel(orders, timeline, 0.9, 0)
        self.assertFalse(bool(applied["cancel_signalled"].iloc[0]))
        self.assertTrue(bool(applied["order_unaffected"].iloc[0]))

    def test_a_score_from_another_side_or_segment_is_never_used(self):
        orders = make_orders([(1000, 1200, -60.0, 0, 100, "conservative")])
        for scores in (
            make_scores([1100], [0.99], side=1),
            make_scores([1100], [0.99], segment=2),
            make_scores([1100], [0.99], file_index=1),
        ):
            self.assertEqual(int(timeline_for(orders, scores)["decision_instants"].iloc[0]), 0)

    def test_no_decision_instant_falls_outside_the_observation_window(self):
        scores = make_scores([1000, 40_000], [0.1, 0.99])
        orders = make_orders([(1000, None, np.nan, 0, 100, "conservative")])
        timeline = timeline_for(orders, scores)
        self.assertEqual(int(timeline[counterfactual._crossing_column(0.9)].iloc[0]), 0)
        self.assertEqual(int(timeline["decision_instants"].iloc[0]), 0)

    def test_thresholds_are_nested_in_time(self):
        scores = make_scores([1000, 1100, 1200, 1300], [0.05, 0.25, 0.55, 0.95])
        orders = make_orders([(1000, None, np.nan, 0, 100, "conservative")])
        timeline = timeline_for(orders, scores)
        crossings = [
            int(timeline[counterfactual._crossing_column(t)].iloc[0])
            for t in spec.CANCEL_THRESHOLDS
        ]
        positive = [value for value in crossings if value > 0]
        self.assertEqual(positive, sorted(positive))


# ------------------------------------------------------------------------------------------
class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.scores = make_scores([1000, 1100], [0.1, 0.95])
        self.orders = make_orders(
            [
                (1000, 1500, -80.0, 0, 100, "conservative"),   # catastrophic, prevented
                (1000, 1500, -12.0, 0, 100, "conservative"),   # adverse but not catastrophic
                (1000, 1500, 7.0, 0, 100, "conservative"),     # favourable, sacrificed
                (1000, 1500, 0.0, 0, 100, "conservative"),     # exactly flat
                (1000, 1050, -80.0, 0, 100, "conservative"),   # cancel arrives too late
                (1000, None, np.nan, 0, 100, "conservative"),  # never fills
            ]
        )
        self.timeline = timeline_for(self.orders, self.scores)
        self.applied = counterfactual.apply_cancel(self.orders, self.timeline, 0.9, 0)

    def test_classification_is_exactly_right(self):
        applied = self.applied
        self.assertEqual(list(applied["fill_prevented"]), [1, 1, 1, 1, 0, 0])
        self.assertEqual(list(applied["adverse_fill_avoided"]), [1, 1, 0, 0, 0, 0])
        self.assertEqual(list(applied["favourable_fill_sacrificed"]), [0, 0, 1, 0, 0, 0])
        self.assertEqual(list(applied["non_negative_fill_sacrificed"]), [0, 0, 1, 1, 0, 0])
        self.assertEqual(list(applied["catastrophic_50_avoided"]), [1, 0, 0, 0, 0, 0])
        self.assertEqual(list(applied["cancel_too_late"]), [0, 0, 0, 0, 1, 0])
        self.assertEqual(list(applied["cancel_without_baseline_fill"]), [0, 0, 0, 0, 0, 1])

    def test_a_flat_fill_counts_as_non_negative_but_not_favourable(self):
        applied = self.applied
        flat = applied.iloc[3]
        self.assertTrue(bool(flat["non_negative_fill_sacrificed"]))
        self.assertFalse(bool(flat["favourable_fill_sacrificed"]))
        self.assertEqual(float(flat["adverse_ticks_avoided"]), 0.0)
        self.assertEqual(float(flat["favourable_ticks_sacrificed"]), 0.0)

    def test_avoided_and_sacrificed_ticks_are_the_baseline_markout(self):
        applied = self.applied
        self.assertAlmostEqual(float(applied["adverse_ticks_avoided"].sum()), 92.0)
        self.assertAlmostEqual(float(applied["favourable_ticks_sacrificed"].sum()), 7.0)
        self.assertAlmostEqual(float(applied["net_markout_preserved"].sum()), 85.0)

    def test_no_ticks_are_attributed_to_a_fill_that_survived(self):
        applied = self.applied
        surviving = applied[applied["surviving_fill"]]
        self.assertTrue(bool((surviving["adverse_ticks_avoided"] == 0).all()))
        self.assertTrue(bool((surviving["favourable_ticks_sacrificed"] == 0).all()))

    def test_every_baseline_fill_is_either_prevented_or_surviving(self):
        applied = self.applied
        filled = applied["filled"].to_numpy(dtype=bool)
        prevented = applied["fill_prevented"].to_numpy(dtype=bool)
        surviving = applied["surviving_fill"].to_numpy(dtype=bool)
        self.assertTrue(bool(np.all(filled == (prevented | surviving))))
        self.assertFalse(bool(np.any(prevented & surviving)))

    def test_every_order_lands_in_exactly_one_disposition(self):
        applied = self.applied
        classes = np.vstack(
            [
                applied["fill_prevented"].to_numpy(dtype=bool),
                applied["cancel_too_late"].to_numpy(dtype=bool),
                applied["cancel_without_baseline_fill"].to_numpy(dtype=bool),
                applied["order_unaffected"].to_numpy(dtype=bool),
            ]
        )
        self.assertTrue(bool(np.all(classes.sum(axis=0) == 1)))


# ------------------------------------------------------------------------------------------
class MetricTests(unittest.TestCase):
    def setUp(self):
        scores = make_scores([1000, 1100], [0.1, 0.95])
        self.orders = make_orders(
            [
                (1000, 1500, -80.0, 0, 100, "conservative"),
                (1000, 1500, 7.0, 0, 100, "conservative"),
                (1000, None, np.nan, 0, 100, "conservative"),
            ]
        )
        self.timeline = timeline_for(self.orders, scores)
        self.applied = counterfactual.apply_cancel(self.orders, self.timeline, 0.9, 0)

    def test_the_baseline_denominator_survives_the_intervention(self):
        metrics = analysis.cell_metrics(self.applied)
        baseline = analysis.baseline_metrics(self.applied)
        self.assertEqual(metrics["eligible_opportunities"], 3)
        self.assertEqual(baseline["eligible_opportunities"], 3)
        self.assertEqual(metrics["baseline_fills"], baseline["baseline_fills"])
        self.assertEqual(metrics["baseline_unfilled"], baseline["baseline_unfilled"])
        self.assertEqual(metrics["baseline_mean"], baseline["baseline_mean"])

    def test_the_never_cancel_baseline_cancels_nothing(self):
        baseline = analysis.baseline_metrics(self.applied)
        self.assertEqual(baseline["cancelled_opportunities"], 0)
        self.assertEqual(baseline["fills_prevented"], 0)
        self.assertEqual(baseline["net_markout_preserved"], 0.0)
        self.assertEqual(baseline["surviving_fills"], baseline["baseline_fills"])

    def test_a_cancelled_order_is_not_counted_as_a_zero_markout_fill(self):
        metrics = analysis.cell_metrics(self.applied)
        # Both fills are prevented here, so no surviving fill and therefore no surviving markout
        # observation at all; zero fills must never be reported as a mean of zero ticks.
        self.assertEqual(metrics["surviving_fills"], 0)
        self.assertEqual(metrics["surviving_observed"], 0)
        self.assertNotIn("surviving_mean", metrics)

    def test_fill_counts_reconcile_exactly(self):
        metrics = analysis.cell_metrics(self.applied)
        self.assertEqual(
            metrics["baseline_fills"], metrics["fills_prevented"] + metrics["surviving_fills"]
        )
        self.assertEqual(
            metrics["eligible_opportunities"],
            metrics["baseline_fills"] + metrics["baseline_unfilled"],
        )
        self.assertEqual(
            metrics["cancelled_opportunities"],
            metrics["fills_prevented"]
            + metrics["cancel_too_late"]
            + metrics["cancel_without_baseline_fill"],
        )

    def test_prevented_fills_are_fully_partitioned_by_outcome(self):
        metrics = analysis.cell_metrics(self.applied)
        self.assertEqual(
            metrics["fills_prevented"],
            metrics["adverse_fills_avoided"]
            + metrics["non_negative_fills_sacrificed"]
            + metrics["prevented_fills_markout_censored"],
        )

    def test_an_undefined_ratio_is_not_infinity(self):
        orders = make_orders([(1000, 1500, -40.0, 0, 100, "conservative")])
        scores = make_scores([1000, 1100], [0.1, 0.95])
        applied = counterfactual.apply_cancel(orders, timeline_for(orders, scores), 0.9, 0)
        metrics = analysis.cell_metrics(applied)
        self.assertEqual(metrics["favourable_markout_sacrificed"], 0.0)
        self.assertTrue(np.isnan(metrics["avoidance_efficiency_ticks"]))

    def test_fractions_of_baseline_markout_are_shares_of_the_whole_cohort(self):
        metrics = analysis.cell_metrics(self.applied)
        self.assertAlmostEqual(metrics["total_baseline_negative_ticks"], 80.0)
        self.assertAlmostEqual(metrics["total_baseline_positive_ticks"], 7.0)
        self.assertAlmostEqual(metrics["fraction_baseline_negative_removed"], 1.0)
        self.assertAlmostEqual(metrics["fraction_baseline_positive_removed"], 1.0)


# ------------------------------------------------------------------------------------------
class SignConventionTests(unittest.TestCase):
    def test_markout_sign_is_inherited_from_phase_3_for_both_sides(self):
        from pyresearch.native.economic import data as economic_data
        from pyresearch.native.economic import spec as economic_spec

        mid = pd.DataFrame(
            {
                "ns": [0, 1_000 * MS, 2_000 * MS],
                "file_index": 0,
                "segment_id": 1,
                "mid_x2": [200, 240, 160],
            }
        )
        bounds = pd.DataFrame(
            {"file_index": [0], "segment_id": [1], "start_ns": [0], "end_ns": [60_000 * MS]}
        )
        fills = pd.DataFrame(
            {
                "placement_ns": [0, 0],
                "file_index": [0, 0],
                "segment_id": [1, 1],
                "side": [0, 1],
                "quote_px_ticks": [100, 100],
                "fill_ns": [1, 1],
                "mechanism": [1, 1],
                "mid_x2_at_placement": [200, 200],
            }
        )
        out = economic_data.add_markouts(fills, economic_data.MidPath(mid), bounds)
        column = f"markout_{economic_spec.MARKOUT_HORIZONS_MS[0]}ms_ticks"
        self.assertIn(column, out.columns)
        # Mid rising to 120 ticks against a quote at 100: good for a resting bid, bad for a
        # resting ask, and the two are exact mirrors.
        thousand = out["markout_1000ms_ticks"].to_numpy()
        self.assertAlmostEqual(thousand[0], -thousand[1])

    def test_a_bid_and_an_ask_with_mirrored_markouts_classify_as_mirrors(self):
        scores = pd.concat(
            [make_scores([1000, 1100], [0.1, 0.95], side=0),
             make_scores([1000, 1100], [0.1, 0.95], side=1)],
            ignore_index=True,
        )
        orders = make_orders(
            [
                (1000, 1500, -40.0, 0, 100, "conservative"),
                (1000, 1500, -40.0, 1, 100, "conservative"),
            ]
        )
        applied = counterfactual.apply_cancel(orders, timeline_for(orders, scores), 0.9, 0)
        self.assertEqual(list(applied["adverse_fill_avoided"]), [1, 1])
        self.assertAlmostEqual(float(applied["adverse_ticks_avoided"].sum()), 80.0)


# ------------------------------------------------------------------------------------------
class DeterminismTests(unittest.TestCase):
    def test_repeated_runs_are_identical(self):
        scores = make_scores(list(range(1000, 2000, 100)), np.linspace(0.05, 0.95, 10))
        orders = make_orders(
            [(1000, 1500, -30.0, 0, 100, "conservative")] * 3
            + [(1000, None, np.nan, 0, 100, "conservative")]
        )
        first = counterfactual.apply_cancel(orders, timeline_for(orders, scores), 0.5, 25)
        second = counterfactual.apply_cancel(orders, timeline_for(orders, scores), 0.5, 25)
        pd.testing.assert_frame_equal(first, second)

    def test_timeline_is_independent_of_input_row_order(self):
        scores = make_scores(list(range(1000, 1600, 100)), [0.1, 0.2, 0.6, 0.95, 0.3, 0.99])
        orders = make_orders(
            [
                (1000, None, np.nan, 0, 100, "conservative"),
                (1100, None, np.nan, 0, 100, "conservative"),
                (1200, None, np.nan, 0, 100, "conservative"),
            ]
        )
        straight = timeline_for(orders, scores).sort_values("placement_ns", ignore_index=True)
        shuffled = timeline_for(
            orders.iloc[::-1].reset_index(drop=True), scores.iloc[::-1].reset_index(drop=True)
        ).sort_values("placement_ns", ignore_index=True)
        pd.testing.assert_frame_equal(
            straight[[c for c in straight.columns if c != "gid"]],
            shuffled[[c for c in shuffled.columns if c != "gid"]],
        )


# ------------------------------------------------------------------------------------------
class ArtifactTests(unittest.TestCase):
    """Checks against the real artifacts, skipped when they have not been produced."""

    @classmethod
    def setUpClass(cls):
        if not (spec.REPORT_DIR / "score_provenance.csv").exists():
            raise unittest.SkipTest("phase 4B artifacts not built")

    def test_the_refit_reproduces_the_phase_4a_out_of_fold_predictions(self):
        checks = pd.read_csv(spec.REPORT_DIR / "score_provenance.csv")
        self.assertEqual(len(checks), 10)
        self.assertTrue(bool((checks["compared_rows"] > 10_000).all()))
        # The stored phase 4A artifact is written with ten significant digits, so agreement can
        # only be asserted to that precision; anything larger would be a genuinely different
        # model rather than a printing difference.
        self.assertLess(float(checks["max_abs_difference"].max()), 1e-9)

    def test_no_score_precedes_its_own_fold_training_window(self):
        folds = {
            fold.index: fold
            for fold in predictive_data.build_folds(
                predictive_data.load_model_frame(columns=["timestamp_ns"])[
                    "timestamp_ns"
                ].to_numpy()
            )
        }
        scores = scoring.load_scores()
        for index, block in scores.groupby("fold"):
            fold = folds[int(index)]
            stamps = block["timestamp_ns"].to_numpy()
            self.assertGreaterEqual(int(stamps.min()), fold.validation_start_ns)
            self.assertLess(int(stamps.max()), fold.validation_end_ns)
            # The purge is what makes the label of a training row unable to reach forward into
            # the block being scored.
            self.assertGreater(
                fold.validation_start_ns - fold.train_end_ns,
                spec.SWEEP_HORIZON_MS * MS,
            )

    def test_every_scored_row_is_a_lifecycle_grid_instant(self):
        scores = scoring.load_scores()
        step = spec.DECISION_GRID_MS * MS
        self.assertTrue(bool((scores["timestamp_ns"].to_numpy() % step == 0).all()))
        self.assertTrue(bool(scores["sweep_p"].between(0.0, 1.0).all()))

    def test_the_cohort_is_entirely_inside_the_validation_span(self):
        from pyresearch.native.cancel import pipeline

        start, end = pipeline.oof_window()
        cohort = pd.read_parquet(spec.DATA_DIR / "cohort.parquet")
        placement = cohort["placement_ns"].to_numpy()
        self.assertGreaterEqual(int(placement.min()), start)
        self.assertLess(int(placement.max()), end)

    def test_no_order_or_decision_crosses_a_segment_boundary(self):
        from pyresearch.native.cancel import pipeline

        _, timeline = pipeline.load_cohort()
        cohort = pd.read_parquet(spec.DATA_DIR / "cohort.parquet")
        self.assertTrue(
            bool((cohort["observed_end_ns"] <= cohort["segment_end_ns"]).all())
        )
        instants = timeline["decision_instants"].to_numpy()
        last = timeline["last_decision_ns"].to_numpy()
        end = timeline["observed_end_ns"].to_numpy()
        self.assertTrue(bool(np.all(last[instants > 0] <= end[instants > 0])))
        self.assertTrue(bool(np.all(last[instants > 0] > timeline["placement_ns"].to_numpy()[instants > 0])))

    def test_the_queue_assumptions_in_the_cohort_are_exactly_the_frozen_cells(self):
        cohort = pd.read_parquet(spec.DATA_DIR / "cohort.parquet")
        pairs = set(
            zip(cohort["alpha_pct"], cohort["beta_pct"], strict=True)
        )
        self.assertEqual(pairs, set(spec.QUEUE_CELLS.values()))
        for name, (alpha, beta) in spec.QUEUE_CELLS.items():
            block = cohort[cohort["queue_cell"] == name]
            self.assertEqual(set(block["alpha_pct"]), {alpha})
            self.assertEqual(set(block["beta_pct"]), {beta})

    def test_the_cancellation_logic_leaves_alpha_and_beta_untouched(self):
        from pyresearch.native.cancel import pipeline

        cohort, timeline = pipeline.load_cohort()
        orders = cohort[cohort["queue_cell"] == "midpoint"].head(50_000).reset_index(drop=True)
        applied = counterfactual.apply_cancel(orders, timeline, 0.5, 50)
        self.assertEqual(set(applied["alpha_pct"]), {50})
        self.assertEqual(set(applied["beta_pct"]), {50})
        # And the never-cancel path it is compared against is the same placement state.
        pd.testing.assert_series_equal(applied["fill_ns"], orders["fill_ns"])
        pd.testing.assert_series_equal(applied["filled"], orders["filled"])
        pd.testing.assert_series_equal(applied["quote_px_ticks"], orders["quote_px_ticks"])

    def test_the_surface_keeps_every_denominator(self):
        surface = pd.read_csv(spec.REPORT_DIR / "threshold_latency_surface.csv")
        self.assertEqual(len(surface[surface["policy"] == "never"]), len(spec.QUEUE_CELLS))
        cancels = surface[surface["policy"] == "cancel"]
        self.assertEqual(
            len(cancels),
            len(spec.QUEUE_CELLS) * len(spec.CANCEL_THRESHOLDS) * len(spec.CANCEL_LATENCIES_MS),
        )
        for _, block in surface.groupby("queue_cell"):
            self.assertEqual(block["eligible_opportunities"].nunique(), 1)
            self.assertEqual(block["baseline_fills"].nunique(), 1)
        self.assertTrue(
            bool(
                (
                    cancels["baseline_fills"]
                    == cancels["fills_prevented"] + cancels["surviving_fills"]
                ).all()
            )
        )

    def test_the_grid_is_exactly_the_pre_registered_one(self):
        surface = pd.read_csv(spec.REPORT_DIR / "threshold_latency_surface.csv")
        cancels = surface[surface["policy"] == "cancel"]
        self.assertEqual(
            sorted(cancels["threshold"].round(2).unique()), list(spec.CANCEL_THRESHOLDS)
        )
        self.assertEqual(
            sorted(cancels["latency_ms"].astype(int).unique()), list(spec.CANCEL_LATENCIES_MS)
        )
        grid = json.loads((spec.REPORT_DIR / "grid_spec.json").read_text())
        self.assertFalse(grid["added_or_removed_after_seeing_results"])
        self.assertTrue(grid["fixed_before_any_result"])
