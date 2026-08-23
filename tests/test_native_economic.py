"""Audit for the maker economic feasibility phase.

The queue grid is the only new modelling object in this phase, so most of these tests exist to
falsify a specific way it could be wrong: alpha leaking into something other than the initial
queue, beta crediting quantity it never saw ahead of the order, a markout resolved across a
segment edge, a sign convention flipped on one side, or a phase 2 prediction quietly replaced by
an in-sample one.
"""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from pyresearch.native.economic import data, spec, stats
from pyresearch.native.core import corpus, diagnostics

FILLS_PRESENT = all(data.fills_path(index).exists() for index in (0, 1, 2))
REPORTS_PRESENT = (spec.REPORT_DIR / "queue_sensitivity_surface.csv").exists()


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


class SpecTests(unittest.TestCase):
    def test_grid_is_fixed_and_contains_the_three_headline_cells(self):
        self.assertEqual(spec.ALPHA_PERCENTS, (0, 25, 50, 75, 100))
        self.assertEqual(spec.BETA_PERCENTS, (0, 25, 50, 75, 100))
        self.assertEqual(len(spec.HEADLINE_CELLS), 3)
        self.assertIn(spec.CONSERVATIVE, spec.HEADLINE_CELLS)
        self.assertIn(spec.OPTIMISTIC, spec.HEADLINE_CELLS)

    def test_methodology_forbids_selection_and_claims_no_out_of_sample(self):
        methodology = spec.methodology({})
        self.assertFalse(methodology["corpus"]["is_out_of_sample"])
        self.assertIn("threshold selection of any kind", methodology["prohibited_in_this_phase"])
        self.assertIn("choosing a best alpha or beta", methodology["prohibited_in_this_phase"])
        self.assertTrue(methodology["economic_object"]["no_fee_schedule_applied"])
        self.assertFalse(methodology["out_of_fold_reuse"]["refitting_in_this_phase"])
        self.assertFalse(methodology["out_of_fold_reuse"]["in_sample_predictions_used"])


@unittest.skipUnless(FILLS_PRESENT, "queue sensitivity replay has not been run")
class QueueGridTests(unittest.TestCase):
    frame: pd.DataFrame

    @classmethod
    def setUpClass(cls) -> None:
        # File 0 is the smallest capture and carries one whole segment.
        path = data.MidPath(data.load_mid_path())
        cls.frame = data.add_markouts(
            data.load_fills(0), path, data.segment_bounds()
        )

    def test_alpha_changes_only_the_initial_queue_ahead(self):
        expected = (
            self.frame["displayed_qty_lots"].to_numpy()
            * self.frame["alpha_pct"].to_numpy()
        ) // 100
        np.testing.assert_array_equal(self.frame["queue_ahead_lots"].to_numpy(), expected)
        # Nothing else observed at the placement instant may vary across the grid.
        varying = self.frame.groupby(["placement_ns", "side"], sort=False)[
            ["quote_px_ticks", "displayed_qty_lots", "mid_x2_at_placement", "spread_ticks"]
        ].nunique()
        self.assertTrue((varying == 1).all().all())

    def test_queue_ahead_is_independent_of_beta(self):
        pivot = self.frame.pivot_table(
            index=["placement_ns", "side", "alpha_pct"],
            columns="beta_pct",
            values="queue_ahead_lots",
        )
        for beta in spec.BETA_PERCENTS[1:]:
            np.testing.assert_array_equal(pivot[0].to_numpy(), pivot[beta].to_numpy())

    def test_beta_is_inert_when_no_queue_is_assumed_ahead(self):
        """At alpha = 0 there is nothing in front of the order for beta to remove."""
        front = self.frame[self.frame["alpha_pct"] == 0]
        pivot = front.pivot_table(
            index=["placement_ns", "side"], columns="beta_pct", values="fill_ns"
        )
        for beta in spec.BETA_PERCENTS[1:]:
            np.testing.assert_array_equal(pivot[0].to_numpy(), pivot[beta].to_numpy())
        mechanisms = front.pivot_table(
            index=["placement_ns", "side"], columns="beta_pct", values="mechanism"
        )
        for beta in spec.BETA_PERCENTS[1:]:
            np.testing.assert_array_equal(mechanisms[0].to_numpy(), mechanisms[beta].to_numpy())

    def test_more_credit_never_slows_a_fill(self):
        """Raising alpha can only delay a fill; raising beta can only advance one."""
        keys = ["placement_ns", "side"]
        fill = self.frame["fill_ns"].to_numpy(dtype="float64")
        # An unfilled order is treated as filling at infinity for the monotonicity comparison.
        work = self.frame.assign(_fill=np.where(fill > 0, fill, np.inf))
        by_alpha = work.pivot_table(
            index=keys + ["beta_pct"], columns="alpha_pct", values="_fill"
        )
        for lower, higher in zip(spec.ALPHA_PERCENTS, spec.ALPHA_PERCENTS[1:]):
            self.assertTrue((by_alpha[higher] >= by_alpha[lower]).all(), (lower, higher))
        by_beta = work.pivot_table(
            index=keys + ["alpha_pct"], columns="beta_pct", values="_fill"
        )
        for lower, higher in zip(spec.BETA_PERCENTS, spec.BETA_PERCENTS[1:]):
            self.assertTrue((by_beta[higher] <= by_beta[lower]).all(), (lower, higher))

    def test_no_fill_precedes_or_equals_its_own_placement(self):
        filled = self.frame[self.frame["filled"]]
        self.assertGreater(len(filled), 0)
        self.assertTrue(
            (filled["fill_ns"].to_numpy() > filled["placement_ns"].to_numpy()).all()
        )
        self.assertTrue(
            (filled["fill_ns"].to_numpy() <= filled["observed_end_ns"].to_numpy()).all()
        )

    def test_no_fill_or_markout_crosses_a_segment_boundary(self):
        end = self.frame["segment_end_ns"].to_numpy()
        self.assertTrue((self.frame["observed_end_ns"].to_numpy() <= end).all())
        fill = self.frame["fill_ns"].to_numpy()
        for horizon in spec.MARKOUT_HORIZONS_MS:
            observed = np.isfinite(self.frame[f"markout_{horizon}ms_ticks"].to_numpy())
            self.assertTrue((fill[observed] + horizon * 1e6 <= end[observed]).all(), horizon)
            # And a markout only ever exists for an order that actually filled.
            self.assertTrue((fill[observed] > 0).all(), horizon)

    def test_markout_sign_convention_is_correct_on_both_sides(self):
        """Recompute a sample straight from the mid path and check the orientation."""
        path = data.MidPath(data.load_mid_path())
        sample = self.frame[self.frame["filled"]].iloc[::997].head(400)
        self.assertGreater(len(sample), 50)
        horizon = spec.PRIMARY_HORIZON_MS
        when = sample["fill_ns"].to_numpy() + int(horizon * 1e6)
        mid = path.mid_at(
            sample["file_index"].to_numpy(), sample["segment_id"].to_numpy(), when
        )
        quote = sample["quote_px_ticks"].to_numpy(dtype="float64")
        expected = np.where(sample["side"].to_numpy() == 0, mid - quote, quote - mid)
        actual = sample[f"markout_{horizon}ms_ticks"].to_numpy()
        both = np.isfinite(expected) & np.isfinite(actual)
        self.assertGreater(both.sum(), 50)
        np.testing.assert_allclose(actual[both], expected[both])
        # A rising mid must favour a resting bid and hurt a resting ask.
        rising = both & (mid > quote)
        if rising.any():
            bid = rising & (sample["side"].to_numpy() == 0)
            ask = rising & (sample["side"].to_numpy() == 1)
            if bid.any():
                self.assertTrue((actual[bid] > 0).all())
            if ask.any():
                self.assertTrue((actual[ask] < 0).all())

    def test_conservative_cell_reproduces_the_phase_one_fill_model(self):
        """alpha = 1, beta = 0 is the phase 1 rule and must reproduce it exactly."""
        block = data.cell(self.frame, 100, 0)
        columns = ["timestamp_ns"] + [
            f"{side}_{name}"
            for side in ("bid", "ask")
            for name in (
                "time_to_fill_ms",
                "fill_via_trade_through",
                "quote_px_ticks",
                "queue_ahead_lots",
                f"postfill_markout_{spec.PRIMARY_HORIZON_MS}ms_ticks",
            )
        ]
        phase1 = diagnostics.read_columns(corpus.CORPUS[0].dataset_path, columns)
        phase1 = phase1[phase1["timestamp_ns"] % int(spec.PLACEMENT_INTERVAL_MS * 1e6) == 0]
        phase1 = phase1.set_index("timestamp_ns")
        for side, code in (("bid", 0), ("ask", 1)):
            mine = block[block["side"] == code].set_index("placement_ns")
            common = mine.index.intersection(phase1.index)
            self.assertGreater(len(common), 1000)
            mine, theirs = mine.loc[common], phase1.loc[common]
            np.testing.assert_array_equal(
                mine["queue_ahead_lots"].to_numpy(),
                theirs[f"{side}_queue_ahead_lots"].to_numpy(),
            )
            np.testing.assert_array_equal(
                mine["quote_px_ticks"].to_numpy(),
                theirs[f"{side}_quote_px_ticks"].to_numpy(),
            )
            latency = mine["time_to_fill_ms"].to_numpy()
            reference = theirs[f"{side}_time_to_fill_ms"].to_numpy()
            np.testing.assert_array_equal(np.isfinite(latency), np.isfinite(reference))
            both = np.isfinite(latency)
            np.testing.assert_allclose(latency[both], reference[both], atol=1e-6)
            through = (mine["mechanism"].to_numpy() == 2).astype(float)
            np.testing.assert_array_equal(
                through[both], theirs[f"{side}_fill_via_trade_through"].to_numpy()[both]
            )
            markout = mine[f"markout_{spec.PRIMARY_HORIZON_MS}ms_ticks"].to_numpy()
            expected = theirs[
                f"{side}_postfill_markout_{spec.PRIMARY_HORIZON_MS}ms_ticks"
            ].to_numpy()
            seen = np.isfinite(markout) & np.isfinite(expected)
            self.assertGreater(seen.sum(), 500)
            np.testing.assert_allclose(markout[seen], expected[seen], atol=1e-6)

    def test_mechanism_decomposition_reconstructs_the_total(self):
        table = stats.mechanism_decomposition(self.frame, ["alpha_pct", "beta_pct", "side"])
        self.assertFalse(table.empty)
        np.testing.assert_allclose(
            table["reconstructed_mean_ticks"].to_numpy(),
            table["mean_markout_total_ticks"].to_numpy(),
            atol=1e-9,
        )
        shares = (
            table["p_trade_through_given_fill"] + table["p_at_quote_given_fill"]
        ).to_numpy()
        np.testing.assert_allclose(shares, 1.0)


@unittest.skipUnless(FILLS_PRESENT, "queue sensitivity replay has not been run")
class ReplayTests(unittest.TestCase):
    def test_repeated_replay_is_byte_identical(self):
        binary = spec.ROOT / "build/cpp/native_queue_sensitivity"
        if not binary.exists():
            self.skipTest("native_queue_sensitivity has not been built")
        with tempfile.TemporaryDirectory() as directory:
            fills = Path(directory) / "fills.csv.zst"
            mid = Path(directory) / "mid.csv.zst"
            subprocess.run(
                [
                    str(binary),
                    str(corpus.CORPUS[0].raw_path),
                    "--fills",
                    str(fills),
                    "--mid",
                    str(mid),
                    "--file-index",
                    "0",
                ],
                cwd=spec.ROOT,
                capture_output=True,
                check=True,
            )
            self.assertEqual(digest(fills), digest(data.fills_path(0)))
            self.assertEqual(digest(mid), digest(data.mid_path(0)))


@unittest.skipUnless(REPORTS_PRESENT, "economic artifacts have not been built")
class ArtifactTests(unittest.TestCase):
    def test_surface_covers_the_whole_fixed_grid(self):
        surface = pd.read_csv(spec.REPORT_DIR / "queue_sensitivity_surface.csv")
        self.assertEqual(len(surface), len(spec.ALPHA_PERCENTS) * len(spec.BETA_PERCENTS))
        self.assertEqual(set(surface["alpha_pct"]), set(spec.ALPHA_PERCENTS))
        self.assertEqual(set(surface["beta_pct"]), set(spec.BETA_PERCENTS))
        # Every cell reports its own denominator alongside every rate.
        self.assertTrue((surface["eligible_opportunities"] > 0).all())
        self.assertTrue((surface["filled"] <= surface["eligible_opportunities"]).all())
        self.assertTrue(
            (
                surface["at_quote_fills"] + surface["trade_through_fills"]
                == surface["filled"]
            ).all()
        )
        required = surface[f"required_benefit_{spec.PRIMARY_HORIZON_MS}ms_ticks"].to_numpy()
        mean = surface[f"markout_{spec.PRIMARY_HORIZON_MS}ms_mean_ticks"].to_numpy()
        np.testing.assert_allclose(required, -mean)

    def test_out_of_fold_predictions_are_joined_on_the_exact_row_key(self):
        from pyresearch.native.predictive import data as predictive_data

        predictions = data.load_oof_predictions()
        self.assertFalse(
            predictions.duplicated(
                subset=["placement_ns", "file_index", "segment_id", "side"]
            ).any()
        )
        # Every joined prediction must sit inside a phase 2 validation block: an in-sample
        # prediction could never have been produced for those rows.
        folds = predictive_data.build_folds(
            predictive_data.load_model_frame(columns=["timestamp_ns"])[
                "timestamp_ns"
            ].to_numpy()
        )
        stamps = predictions["placement_ns"].to_numpy()
        inside = np.zeros(len(stamps), dtype=bool)
        for fold in folds:
            inside |= (stamps >= fold.validation_start_ns) & (
                stamps < fold.validation_end_ns
            )
        self.assertTrue(inside.all())

    def test_methodology_records_input_hashes_and_the_commit(self):
        methodology = json.loads(
            (spec.REPORT_DIR / "methodology.json").read_text(encoding="utf-8")
        )
        inputs = methodology["inputs"]
        self.assertRegex(inputs["git_commit"], r"^[0-9a-f]{40}$")
        for index in (0, 1, 2):
            self.assertEqual(
                inputs["queue_fills_sha256"][f"file{index}"], digest(data.fills_path(index))
            )
            self.assertEqual(
                inputs["mid_path_sha256"][f"file{index}"], digest(data.mid_path(index))
            )
        self.assertRegex(inputs["phase2_oof_sha256"], r"^[0-9a-f]{64}$")

    def test_no_artifact_claims_out_of_sample_or_names_a_chosen_cell(self):
        for path in spec.REPORT_DIR.glob("*.csv"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("out_of_sample", text, path.name)
            self.assertNotIn("optimal", text, path.name)
            self.assertNotIn("selected", text, path.name)


if __name__ == "__main__":
    unittest.main()
