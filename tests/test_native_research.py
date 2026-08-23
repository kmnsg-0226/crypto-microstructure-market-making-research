"""Boundary, causality and determinism tests for the native_dev_v1 dataset.

These run against the real frozen corpus. They are skipped when the exported dataset is absent,
because the raw capture itself lives outside version control.
"""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import zstandard as zstd

from pyresearch.native.core import corpus, diagnostics

GRID_NS = 100_000_000
MARKOUT_MS = (100, 250, 500, 1000, 5000)
WINDOW_MS = (100, 250, 500, 1000, 5000)
FLOW_COUNTERS = (
    "trade_count",
    "depth_event_count",
    "buy_qty",
    "sell_qty",
    "bid_depth_add_l1",
    "bid_depth_remove_l1",
)
DATASET_PRESENT = all(entry.dataset_path.exists() for entry in corpus.CORPUS)
REPORTS_PRESENT = (corpus.REPORT_DIR / "qc.json").exists()


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


@unittest.skipUnless(DATASET_PRESENT and REPORTS_PRESENT, "native_dev_v1 has not been exported")
class NativeDatasetTests(unittest.TestCase):
    frame: pd.DataFrame
    segments: pd.DataFrame

    @classmethod
    def setUpClass(cls) -> None:
        columns = (
            [
                "timestamp_ns",
                "file_index",
                "segment_id",
                "segment_age_ms",
                "mid_ticks",
                "bid_px_1",
                "ask_px_1",
                "bid_queue_ahead_lots",
                "ask_queue_ahead_lots",
                "bid_time_to_fill_ms",
                "bid_fill_5000ms",
                "bid_postfill_markout_5000ms_ticks",
            ]
            + [f"{name}_{width}ms" for name in FLOW_COUNTERS for width in WINDOW_MS]
            + [f"markout_{horizon}ms_ticks" for horizon in MARKOUT_MS]
        )
        cls.frame = pd.concat(
            [diagnostics.read_columns(entry.dataset_path, columns) for entry in corpus.CORPUS],
            ignore_index=True,
        )
        cls.segments = pd.read_csv(corpus.REPORT_DIR / "segments.csv")

    def remaining_ns(self) -> np.ndarray:
        ends = self.segments.set_index(["file_index", "segment_id"])["end_ns"]
        keyed = self.frame.set_index(["file_index", "segment_id"]).index.map(ends).to_numpy()
        return keyed - self.frame["timestamp_ns"].to_numpy()

    def test_corpus_spec_matches_the_raw_files_on_disk(self):
        spec = json.loads(corpus.SPEC_PATH.read_text(encoding="utf-8"))
        self.assertTrue(spec["development_only"])
        self.assertFalse(spec["is_out_of_sample"])
        self.assertEqual(
            [record["filename"] for record in spec["files"]],
            [entry.name for entry in corpus.CORPUS],
        )
        self.assertEqual(corpus.verify(), [])

    def test_qc_reports_no_failures_and_no_silent_drops(self):
        report = json.loads((corpus.REPORT_DIR / "qc.json").read_text(encoding="utf-8"))
        self.assertEqual(report["failures"], [])
        totals = report["totals"]
        for counter in (
            "parse_failures",
            "crossed_book_states",
            "empty_bid_states",
            "empty_ask_states",
        ):
            self.assertEqual(totals[counter], 0, counter)
        # Each file is replayed alone, so every file contributes at least its own snapshot.
        self.assertGreaterEqual(totals["snapshots"], len(corpus.CORPUS))

    def test_every_row_lies_inside_its_own_segment(self):
        merged = self.frame.merge(self.segments, on=["file_index", "segment_id"], how="left")
        self.assertTrue(merged["start_ns"].notna().all())
        self.assertTrue((merged["timestamp_ns"] >= merged["start_ns"]).all())
        self.assertTrue((merged["timestamp_ns"] <= merged["end_ns"]).all())
        self.assertTrue((merged["timestamp_ns"] % GRID_NS == 0).all())
        counts = (
            self.frame.groupby(["file_index", "segment_id"]).size().reset_index(name="observed")
        )
        joined = counts.merge(self.segments, on=["file_index", "segment_id"])
        self.assertEqual(len(joined), len(self.segments))
        self.assertTrue((joined["observed"] == joined["rows"]).all())

    def test_grid_is_regular_within_each_segment(self):
        for key, block in self.frame.groupby(["file_index", "segment_id"], sort=True):
            deltas = np.diff(block["timestamp_ns"].to_numpy())
            self.assertTrue((deltas == GRID_NS).all(), key)

    def test_no_rolling_feature_crosses_a_segment_boundary(self):
        """The first sample of a segment can only aggregate events from inside that segment.

        It is at most one grid interval old, so its 5 s window and its 100 ms window are clipped
        to the same interval and must agree counter for counter. A window reaching back across
        the boundary would make the wider one larger.
        """
        first = self.frame.groupby(["file_index", "segment_id"], sort=True).head(1)
        self.assertEqual(len(first), len(self.segments))
        self.assertTrue((first["segment_age_ms"] <= 100).all())
        for name in FLOW_COUNTERS:
            self.assertTrue(
                first[f"{name}_100ms"].equals(first[f"{name}_5000ms"]), name
            )

    def test_trailing_windows_are_nested_and_never_look_ahead(self):
        """Wider windows are supersets of narrower ones, so their counters never shrink."""
        for name in FLOW_COUNTERS:
            for narrow, wide in zip(WINDOW_MS, WINDOW_MS[1:]):
                self.assertTrue(
                    (self.frame[f"{name}_{narrow}ms"] <= self.frame[f"{name}_{wide}ms"]).all(),
                    f"{name}_{narrow}ms vs {name}_{wide}ms",
                )

    def test_no_target_crosses_a_segment_boundary(self):
        remaining = self.remaining_ns()
        for horizon in MARKOUT_MS:
            observed = self.frame[f"markout_{horizon}ms_ticks"].notna().to_numpy()
            horizon_ns = horizon * 1_000_000
            # A horizon reaching past the segment end is left empty, never zero-filled.
            self.assertFalse(observed[remaining < horizon_ns].any(), horizon)
            # A horizon that fits strictly inside the segment is always populated.
            self.assertTrue(observed[remaining > horizon_ns].all(), horizon)

    def test_markouts_agree_with_the_observed_mid_path(self):
        """A 100 ms markout equals the next grid mid whenever both sit in the same segment.

        This is the sharpest available lookahead check: the target is resolved from the live
        event stream, the comparison mid from the sampled grid, and the two are computed by
        different code paths at different moments.
        """
        checked = 0
        for key, block in self.frame.groupby(["file_index", "segment_id"], sort=True):
            mid = block["mid_ticks"].to_numpy()
            actual = block["markout_100ms_ticks"].to_numpy()[:-1]
            expected = mid[1:] - mid[:-1]
            mask = np.isfinite(actual)
            self.assertTrue(np.allclose(actual[mask], expected[mask]), key)
            checked += int(mask.sum())
        self.assertGreater(checked, 0)

    def test_passive_outcomes_are_censored_rather_than_assumed(self):
        remaining = self.remaining_ns()
        fill = self.frame["bid_fill_5000ms"].to_numpy(dtype="float64")
        self.assertFalse(np.isfinite(fill[remaining < 5000 * 1_000_000]).any())
        # A simulated fill always happens strictly after its own decision instant and inside the
        # observation window; nothing is ever filled from information available at placement.
        filled = self.frame["bid_time_to_fill_ms"].dropna()
        self.assertTrue((filled > 0).all())
        self.assertTrue((filled <= 30_000).all())
        self.assertLessEqual(
            int(self.frame["bid_postfill_markout_5000ms_ticks"].notna().sum()), len(filled)
        )

    def test_queue_simulation_uses_only_state_visible_at_placement(self):
        """Queue ahead is the quantity the book displayed at the decision instant, no better."""
        self.assertTrue((self.frame["bid_queue_ahead_lots"] > 0).all())
        self.assertTrue((self.frame["ask_queue_ahead_lots"] > 0).all())
        self.assertTrue((self.frame["bid_px_1"] < self.frame["ask_px_1"]).all())
        schema = json.loads(
            (corpus.REPORT_DIR / "dataset_schema.json").read_text(encoding="utf-8")
        )
        assumptions = schema["passive_quote_assumptions"]
        self.assertEqual(
            assumptions["initial_queue_ahead"],
            "the entire displayed quantity at the quote price",
        )
        self.assertTrue(assumptions["true_queue_position"].startswith("not observable"))

    def test_reconnect_regions_are_excluded(self):
        self.assertTrue(
            set(self.segments["close_reason"])
            <= {"depth_desync", "trade_stream_down", "resnapshot", "end_of_input"}
        )
        self.assertTrue((self.segments["duration_s"] > 0).all())
        for _, block in self.segments.groupby("file_index", sort=True):
            ordered = block.sort_values("segment_id")
            starts = ordered["start_ns"].to_numpy()[1:]
            ends = ordered["end_ns"].to_numpy()[:-1]
            self.assertTrue((starts > ends).all())
        # The captured span always exceeds the usable research time by the excluded regions.
        report = json.loads((corpus.REPORT_DIR / "qc.json").read_text(encoding="utf-8"))
        self.assertLess(
            report["totals"]["valid_research_s"], report["totals"]["captured_span_s"]
        )

    def test_schema_matches_the_exported_header(self):
        schema = json.loads(
            (corpus.REPORT_DIR / "dataset_schema.json").read_text(encoding="utf-8")
        )
        with corpus.CORPUS[0].dataset_path.open("rb") as handle:
            with zstd.ZstdDecompressor().stream_reader(handle) as stream:
                header = stream.read(1 << 16).split(b"\n")[0].decode("utf-8").split(",")
        self.assertEqual(schema["columns"], header)
        self.assertEqual(schema["column_count"], len(header))

    def test_raw_replay_results_are_invariant_to_the_research_export(self):
        binary = corpus.ROOT / "build/cpp/crypto_replay"
        if not binary.exists():
            self.skipTest("crypto_replay has not been built")
        entry = corpus.CORPUS[0]
        bare = json.loads(
            subprocess.run(
                [str(binary), str(entry.raw_path)],
                cwd=corpus.ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        exported = json.loads(entry.qc_path.read_text(encoding="utf-8"))["file"]
        for key in (
            "raw_records",
            "depth_events",
            "applied_depth_events",
            "stale_depth_events",
            "trade_events",
            "sequence_gaps",
            "parse_failures",
            "final_update_id",
            "final_checksum",
            "crossed_book_states",
        ):
            self.assertEqual(bare[key], exported[key], key)

    def test_export_is_byte_identical_on_a_repeated_run(self):
        binary = corpus.ROOT / "build/cpp/native_dataset_export"
        if not binary.exists():
            self.skipTest("native_dataset_export has not been built")
        entry = corpus.CORPUS[0]
        with tempfile.TemporaryDirectory() as directory:
            replica = Path(directory) / "replica.csv.zst"
            subprocess.run(
                [
                    str(binary),
                    str(entry.raw_path),
                    "--dataset",
                    str(replica),
                    "--file-index",
                    str(entry.file_index),
                ],
                cwd=corpus.ROOT,
                capture_output=True,
                check=True,
            )
            self.assertEqual(digest(replica), digest(entry.dataset_path))

    def test_diagnostics_always_report_their_population(self):
        coefficients = pd.read_csv(corpus.REPORT_DIR / "information_coefficients.csv")
        self.assertFalse(coefficients.empty)
        self.assertTrue((coefficients["observations"] > 0).all())
        self.assertLessEqual(coefficients["pearson_ic"].abs().max(), 1.0)
        passive = pd.read_csv(corpus.REPORT_DIR / "passive_summary.csv")
        self.assertTrue(passive["denominator_name"].notna().all())
        self.assertTrue(
            (passive["denominator"] <= passive["eligible_opportunities"]).all()
        )
        self.assertTrue((passive["numerator"] <= passive["denominator"]).all())


if __name__ == "__main__":
    unittest.main()
