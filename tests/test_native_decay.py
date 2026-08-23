"""Tests for phase 6 — signal decay and horizon extension.

What can go wrong in a decay study is narrow and specific: a target that quietly reaches past a
segment edge, a forward shift that is off by one row, a purge inherited from a phase whose
horizons were a hundred times shorter, an "independent" sample that is really the same minute
counted six thousand times, or a decomposition whose two halves do not add up. Those are what
these tests pin down, on synthetic data where the answer is known by construction and on the
real committed artifacts where it has to hold in production.
"""
from __future__ import annotations

import json
import unittest

import numpy as np
import pandas as pd

from pyresearch.native.decay import analysis, data, spec

NS = 100_000_000  # the grid step


def make_grid(segments, start=1_000_000_000_000_000_000):
    """A synthetic contiguous 100 ms grid: {(file, segment): [mid, mid, ...]}."""
    rows = []
    stamp = start
    for (file_index, segment_id), mids in segments.items():
        for index, mid in enumerate(mids):
            rows.append(
                {
                    "timestamp_ns": stamp + index * NS,
                    "file_index": file_index,
                    "segment_id": segment_id,
                    "mid_ticks": float(mid),
                    "spread_ticks": 1.0,
                    "obi_l1": 0.0,
                    "obi_l5": 0.0,
                    "obi_l10": 0.0,
                    "markout_1000ms_ticks": np.nan,
                    "markout_5000ms_ticks": np.nan,
                }
            )
        stamp += (len(mids) + 100) * NS
    return pd.DataFrame(rows)


def artifact(name):
    return pd.read_csv(spec.REPORT_DIR / name)


def artifact_json(name):
    with open(spec.REPORT_DIR / name) as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------------------------
class PreRegistrationTests(unittest.TestCase):
    def test_horizon_grid_is_the_pre_registered_one(self):
        self.assertEqual(spec.HORIZONS_S, (1, 2, 5, 10, 30, 60, 120, 300, 600))
        self.assertEqual(spec.PIVOT_S, 5)
        self.assertEqual(spec.COST_HURDLE_BPS, (1.0, 2.0, 3.0, 5.0, 7.5, 10.0))
        self.assertEqual(spec.DECILES, 10)

    def test_no_horizon_is_selected_as_best(self):
        """The phase reports a profile. Nominating a horizon would be the selection it forbids."""
        for forbidden in ("PRIMARY_HORIZON_S", "PRIMARY_HORIZON_MS", "BEST_HORIZON_S"):
            self.assertFalse(hasattr(spec, forbidden), forbidden)
        self.assertIn("no_primary_horizon", spec.methodology({}))
        # and no committed artifact may name one either
        for name in ("verdict.json", "methodology.json"):
            blob = json.dumps(artifact_json(name)).lower()
            for token in ("best_horizon", "selected_horizon", "chosen_horizon", "optimal"):
                self.assertNotIn(token, blob, f"{name} names {token}")

    def test_prohibitions_are_declared_and_no_model_is_trained(self):
        doc = spec.methodology({})
        self.assertEqual(doc["models_trained_in_this_phase"], 0)
        for token in ("sharpe", "pnl", "entry", "holding-period", "training any model"):
            self.assertTrue(
                any(token in item.lower() for item in doc["prohibited_in_this_phase"]),
                token,
            )

    def test_development_data_is_never_called_out_of_sample(self):
        """Blocked out-of-fold is not out of sample. No artifact may claim otherwise."""
        doc = spec.methodology({})
        self.assertFalse(doc["corpus"]["is_out_of_sample"])
        self.assertTrue(doc["corpus"]["development_only"])
        self.assertIn("development", doc["corpus"]["label"])

        def walk(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    if "out_of_sample" in key:
                        self.assertIs(value, False, f"{path}/{key} claims out of sample")
                    walk(value, f"{path}/{key}")
            elif isinstance(node, list):
                for item in node:
                    walk(item, path)
            elif isinstance(node, str):
                lowered = node.lower()
                # the untouched-forward-data declaration is allowed to name the holdout
                if "untouched" in path or "forward_data" in path:
                    return
                for claim in ("out of sample", "out-of-sample", "held out"):
                    self.assertNotIn(claim, lowered, f"{path} claims: {node}")

        for name in ("verdict.json", "methodology.json", "target_agreement.json"):
            walk(artifact_json(name))

    def test_no_committed_metric_file_claims_out_of_sample(self):
        for path in sorted(spec.REPORT_DIR.glob("*.csv")):
            header = path.read_text().split("\n", 1)[0].lower()
            self.assertNotIn("out_of_sample", header, str(path))

    def test_the_failing_passive_binary_gate_was_not_re_frozen(self):
        with open(spec.ROOT / "research/specs/maker_research_spec_frozen.json") as handle:
            frozen = json.load(handle)
        self.assertEqual(
            frozen["audit"]["passive_binary_sha256"],
            "c93fb9b2f46786370bfd9bd6b1960f9be31ef89cd5285c5ca8aa0ca73b01dee5",
        )


# ---------------------------------------------------------------------------------------------
class TargetConstructionTests(unittest.TestCase):
    def test_forward_shift_is_the_instant_h_later(self):
        grid = make_grid({(0, 1): list(range(100, 200))})
        built = data.add_targets(grid.copy(), horizons_s=(1, 2))
        # mid rises one tick per 100 ms, so a 1 s horizon is exactly +10 ticks
        observed = built["markout_1s_ticks"].dropna()
        self.assertTrue(np.allclose(observed, 10.0))
        self.assertTrue(np.allclose(built["markout_2s_ticks"].dropna(), 20.0))
        # and the forward mid really is the row 10 later
        self.assertEqual(built.loc[0, "mid_fwd_1s_ticks"], built.loc[10, "mid_ticks"])

    def test_no_target_crosses_a_segment_boundary(self):
        grid = make_grid({(0, 1): [100.0] * 30, (0, 2): [500.0] * 30})
        built = data.add_targets(grid.copy(), horizons_s=(1, 2))
        first = built[built["segment_id"] == 1]
        # the last 10 rows of a segment cannot reach 1 s forward
        self.assertTrue(first["markout_1s_ticks"].tail(10).isna().all())
        self.assertTrue(first["markout_1s_ticks"].head(20).notna().all())
        # and nothing borrows the next segment's very different price
        self.assertEqual(float(first["markout_1s_ticks"].dropna().abs().max()), 0.0)

    def test_censoring_is_never_a_zero(self):
        grid = make_grid({(0, 1): [100.0] * 12})
        built = data.add_targets(grid.copy(), horizons_s=(1,))
        tail = built["markout_1s_ticks"].tail(10)
        self.assertTrue(tail.isna().all())
        self.assertEqual(int((tail == 0).sum()), 0)

    def test_a_hole_in_the_grid_is_refused(self):
        grid = make_grid({(0, 1): [100.0] * 20})
        broken = grid.drop(index=5).reset_index(drop=True)
        with self.assertRaises(AssertionError):
            data.add_targets(broken, horizons_s=(1,))

    def test_regenerated_targets_agree_with_frozen_at_or_below_5s(self):
        """The committed gate: 1 s and 5 s must reproduce the frozen phase 1 columns exactly."""
        report = artifact_json("target_agreement.json")
        self.assertTrue(report["agreement_passes"])
        self.assertEqual(report["problems"], [])
        for horizon in (1, 5):
            record = report["target_agreement_vs_frozen_phase1"][f"markout_{horizon}s"]
            self.assertEqual(record["rows_differing"], 0)
            self.assertEqual(record["censoring_disagreements"], 0)
            self.assertEqual(record["max_abs_difference_ticks"], 0.0)
            self.assertGreater(record["rows_compared"], 2_500_000)
        two = report["target_agreement_vs_phase5a_reconstruction"]["markout_2s"]
        self.assertEqual(two["censoring_disagreements"], 0)
        self.assertLessEqual(two["rows_differing"], spec.PHASE5A_MAX_DISAGREEING_ROWS)

    def test_real_frame_targets_stay_inside_their_segment(self):
        frame = data.read_frame()
        ends = frame.groupby(["file_index", "segment_id"])["timestamp_ns"].transform("max")
        for horizon in spec.HORIZONS_S:
            observed = frame[f"markout_{horizon}s_ticks"].notna()
            reach = frame["timestamp_ns"] + int(horizon * 1e9)
            self.assertTrue(
                bool((reach[observed] <= ends[observed]).all()),
                f"a {horizon}s target reaches past its segment end",
            )


# ---------------------------------------------------------------------------------------------
class SignNormalisationTests(unittest.TestCase):
    def test_probability_transform_centres_on_neutral(self):
        for probability, expected in ((0.5, 0.0), (1.0, 1.0), (0.0, -1.0), (0.75, 0.5)):
            self.assertAlmostEqual(2.0 * probability - 1.0, expected)
        self.assertEqual(spec.SIGNAL_NEUTRAL, 0.0)

    def test_sweep_direction_is_ask_minus_bid(self):
        """A threatened ask implies +1 and a threatened bid -1, the phase 5A convention."""
        wide = pd.DataFrame({"sweep_p_ask": [0.9, 0.1, 0.4], "sweep_p_bid": [0.1, 0.9, 0.4]})
        directional = wide["sweep_p_ask"] - wide["sweep_p_bid"]
        self.assertGreater(directional[0], 0)
        self.assertLess(directional[1], 0)
        self.assertEqual(directional[2], 0)
        # mirroring the two sides flips the sign exactly
        mirrored = wide["sweep_p_bid"] - wide["sweep_p_ask"]
        self.assertTrue(np.allclose(directional, -mirrored))

    def test_signed_markout_flips_with_the_signal(self):
        view = _synthetic_view(signal_values=np.array([1.0, -1.0]), raw=np.array([5.0, -5.0]))
        record = analysis.signal_horizon_record(view)
        # both rows are "right": a positive signal that rose, a negative signal that fell
        self.assertAlmostEqual(record["raw_signed_markout_mean_ticks"], 5.0)
        self.assertAlmostEqual(record["raw_hit_rate"], 1.0)
        flipped = _synthetic_view(
            signal_values=np.array([-1.0, 1.0]), raw=np.array([5.0, -5.0])
        )
        self.assertAlmostEqual(
            analysis.signal_horizon_record(flipped)["raw_signed_markout_mean_ticks"], -5.0
        )

    def test_up_and_down_legs_are_reported_separately(self):
        self.assertTrue(spec.REPORT_SIDES_SEPARATELY)
        legs = artifact("up_down_legs.csv")["leg"].unique()
        self.assertIn("up_leg_top_decile", legs)
        self.assertIn("down_leg_bottom_decile", legs)


# ---------------------------------------------------------------------------------------------
class PurgeTests(unittest.TestCase):
    def test_purge_is_horizon_specific_and_the_old_flat_purge_is_not_reused(self):
        for horizon in spec.HORIZONS_S:
            self.assertEqual(spec.purge_seconds(horizon), horizon + 60.0)
            self.assertGreater(spec.purge_seconds(horizon), 60.0)
        self.assertFalse(spec.methodology({})["purge"]["old_flat_purge_reused"])
        # the longest horizon must purge far more than the phase 2 constant
        self.assertEqual(spec.purge_seconds(600), 660.0)

    def test_every_evaluated_row_clears_its_own_horizon_purge(self):
        frame = data.read_frame()
        for horizon in spec.HORIZONS_S:
            mask = analysis.population_mask(frame, "obi_l1", horizon)
            distance = frame.loc[mask, "seconds_into_validation_block"]
            self.assertGreaterEqual(
                float(distance.min()), spec.purge_seconds(horizon),
                f"a row at h={horizon}s sits inside the purge",
            )

    def test_a_longer_horizon_never_evaluates_more_rows_than_a_shorter_one(self):
        frame = data.read_frame()
        counts = [
            int(analysis.population_mask(frame, "obi_l1", h).sum()) for h in spec.HORIZONS_S
        ]
        self.assertEqual(counts, sorted(counts, reverse=True))


# ---------------------------------------------------------------------------------------------
class AnchorTests(unittest.TestCase):
    def test_anchors_are_horizon_spaced_and_never_overlap(self):
        grid = make_grid({(0, 1): [100.0] * 100})
        built = data.add_anchors(grid.copy(), horizons_s=(1, 2))
        stamps = built.loc[built["anchor_1s"], "timestamp_ns"].to_numpy()
        gaps = np.diff(stamps)
        self.assertTrue(np.all(gaps == int(1e9)))
        # consecutive 1 s windows touch but never overlap
        self.assertTrue(np.all(gaps >= int(1e9)))
        self.assertEqual(len(stamps), 10)
        self.assertEqual(int(built["anchor_2s"].sum()), 5)

    def test_anchors_restart_at_each_segment(self):
        grid = make_grid({(0, 1): [100.0] * 25, (0, 2): [100.0] * 25})
        built = data.add_anchors(grid.copy(), horizons_s=(1,))
        for _, part in built.groupby(["file_index", "segment_id"]):
            self.assertTrue(bool(part.iloc[0]["anchor_1s"]))

    def test_anchors_do_not_depend_on_later_filtering(self):
        """Anchors are defined on the raw grid, so a purge cannot move them."""
        frame = data.read_frame()
        subset = frame[frame["fold"] >= 0]
        merged = frame[["timestamp_ns", "file_index", "anchor_600s"]].merge(
            subset[["timestamp_ns", "file_index", "anchor_600s"]],
            on=["timestamp_ns", "file_index"],
            suffixes=("_all", "_subset"),
        )
        self.assertTrue(bool((merged["anchor_600s_all"] == merged["anchor_600s_subset"]).all()))

    def test_effective_sample_shrinks_with_the_horizon(self):
        table = artifact("effective_sample.csv")
        part = table[table["signal"] == "obi_l1"].sort_values("horizon_s")
        self.assertEqual(
            list(part["anchors"]), sorted(part["anchors"], reverse=True)
        )
        # the honest independent count at ten minutes is in the hundreds, not the millions
        self.assertLess(int(part[part["horizon_s"] == 600]["anchors"].iloc[0]), 1000)


# ---------------------------------------------------------------------------------------------
class DecompositionTests(unittest.TestCase):
    def test_cumulative_equals_pivot_plus_incremental(self):
        table = artifact("reconciliation.csv")
        self.assertGreater(len(table), 0)
        worst = float(table["residual_ticks"].abs().max())
        self.assertLess(
            worst,
            spec.RECONCILIATION_TOLERANCE_TICKS,
            f"decomposition does not reconcile, worst residual {worst} ticks",
        )
        # both adjustments and every horizon beyond the pivot must be covered
        self.assertEqual(set(table["adjustment"]), {"raw", "demeaned"})
        self.assertEqual(
            sorted(table["horizon_s"].unique()),
            [h for h in spec.HORIZONS_S if h > spec.PIVOT_S],
        )

    def test_the_pivot_is_measured_on_the_horizons_own_population(self):
        """A longer horizon purges more and censors more, so the populations really differ."""
        table = artifact("reconciliation.csv")
        long = table[table["horizon_s"] == 600]
        self.assertTrue(bool((long["rows"] < long["rows_at_5s"]).all()))

    def test_incremental_uses_the_signal_at_t_not_a_future_return(self):
        signal_values = np.linspace(-1.0, 1.0, 100)
        view = _synthetic_view(
            signal_values=signal_values,
            raw=np.full(100, 30.0),
            pivot=np.full(100, 10.0),
        )
        record = analysis.spread_record(view, "incremental")
        # every row moved identically after the pivot, so the spread is exactly zero even though
        # the signal orders the rows perfectly
        self.assertAlmostEqual(record["raw_spread_ticks"], 0.0)
        self.assertAlmostEqual(record["raw_pivot_spread_ticks"], 0.0)

    def test_incremental_isolates_movement_after_the_pivot(self):
        signal_values = np.linspace(-1.0, 1.0, 100)
        # the pivot move is flat, all the ordering happens between 5 s and h
        view = _synthetic_view(
            signal_values=signal_values,
            raw=signal_values * 100.0,
            pivot=np.zeros(100),
        )
        record = analysis.spread_record(view, "incremental")
        cumulative = analysis.spread_record(view, "cumulative")
        self.assertAlmostEqual(
            record["raw_spread_ticks"], cumulative["raw_spread_ticks"], places=9
        )

    def test_incremental_is_undefined_at_or_below_the_pivot(self):
        frame = data.read_frame()
        view = analysis.prepare(frame.head(50_000), "obi_l1", 5, full_corpus=True)
        self.assertIsNone(view.pivot)
        self.assertEqual(analysis.spread_record(view, "incremental"), {})

    def test_no_signal_column_is_a_future_return(self):
        for name in spec.SIGNALS:
            for token in ("markout", "fwd", "forward", "future", "next_mid"):
                self.assertNotIn(token, name)
        self.assertFalse(spec.methodology({})["drift_control"]["future_returns_used_as_features"])


# ---------------------------------------------------------------------------------------------
class DependenceTests(unittest.TestCase):
    def test_block_length_is_horizon_aware(self):
        self.assertEqual(spec.bootstrap_block_seconds(1), 1800.0)
        self.assertEqual(spec.bootstrap_block_seconds(60), 1800.0)
        self.assertEqual(spec.bootstrap_block_seconds(300), 3600.0)
        self.assertEqual(spec.bootstrap_block_seconds(600), 7200.0)
        self.assertFalse(spec.methodology({})["dependence"]["rows_treated_as_independent"])
        self.assertFalse(spec.methodology({})["dependence"]["iid_standard_errors_used"])

    def test_block_count_falls_as_the_horizon_grows(self):
        table = artifact("effective_sample.csv")
        part = table[table["signal"] == "obi_l1"].sort_values("horizon_s")
        self.assertEqual(
            list(part["bootstrap_blocks"]), sorted(part["bootstrap_blocks"], reverse=True)
        )


# ---------------------------------------------------------------------------------------------
class DeterminismTests(unittest.TestCase):
    def test_deciles_are_deterministic_and_balanced(self):
        values = np.array([3.0, 1.0, 2.0, 5.0, 4.0] * 20)
        first = analysis.deciles(values)
        second = analysis.deciles(values.copy())
        self.assertTrue(np.array_equal(first, second))
        self.assertEqual(sorted(np.bincount(first)), [10] * 10)

    def test_repeated_analysis_is_identical(self):
        frame = data.read_frame().head(400_000)
        one = analysis.prepare(frame, "obi_l1", 10, full_corpus=True)
        two = analysis.prepare(frame, "obi_l1", 10, full_corpus=True)
        self.assertEqual(
            analysis.spread_record(one, "cumulative"),
            analysis.spread_record(two, "cumulative"),
        )
        self.assertTrue(
            analysis.decile_table(one).equals(analysis.decile_table(two))
        )

    def test_bootstrap_is_seeded(self):
        self.assertEqual(spec.SEED, 0)
        self.assertEqual(spec.BOOTSTRAP_DRAWS, 500)


# ---------------------------------------------------------------------------------------------
def _synthetic_view(signal_values, raw, pivot=None):
    rows = signal_values.size
    return analysis.Prepared(
        signal="synthetic",
        horizon_s=10,
        population="synthetic",
        signal_values=signal_values,
        mid=np.full(rows, 700_000.0),
        raw=raw,
        adj=raw - raw.mean(),
        pivot=pivot,
        bucket=analysis.deciles(signal_values),
        anchor=np.ones(rows, dtype=bool),
        boot_block=np.zeros(rows, dtype="int64"),
        demean_block=np.zeros(rows, dtype="int64"),
        utc_day=np.array(["2026-08-18"] * rows),
        segment_key=np.array(["0:1"] * rows),
    )


if __name__ == "__main__":
    unittest.main()
