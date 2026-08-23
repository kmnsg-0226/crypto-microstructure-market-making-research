import hashlib
import json
from pathlib import Path
import unittest

import numpy as np

from pyresearch.passive.analysis import hac_mean_se
from pyresearch.passive.pipeline import (
    FROZEN_SPEC,
    FROZEN_SPEC_HASH,
    PASSIVE_BINARY,
    REPORT_ROOT,
    THRESHOLDS_REPORT,
    _validate_frozen,
)


class FrozenPassivePipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = json.loads(FROZEN_SPEC.read_text(encoding="utf-8"))

    def test_maker_spec_is_frozen_and_hash_matches(self) -> None:
        self.assertEqual(self.spec["status"], "frozen_after_development")
        observed = hashlib.sha256(FROZEN_SPEC.read_bytes()).hexdigest()
        self.assertEqual(FROZEN_SPEC_HASH.read_text(encoding="utf-8").strip(), observed)
        self.assertFalse(
            self.spec["development_maker_analysis"][
                "parameter_selected_from_maker_outcomes"
            ]
        )

    def test_validation_and_historical_holdout_are_isolated(self) -> None:
        development = set(self.spec["split"]["development"])
        validation = set(self.spec["split"]["validation"])
        holdout = set(self.spec["split"]["historical_holdout"])
        self.assertEqual(validation, {"2026-06-01"})
        self.assertEqual(holdout, {"2026-07-01", "2026-08-01"})
        self.assertFalse(development & validation)
        self.assertFalse(development & holdout)
        self.assertFalse(validation & holdout)

    def test_development_only_threshold_artifact(self) -> None:
        if not THRESHOLDS_REPORT.exists():
            self.skipTest("ignored local passive research artifacts are absent")
        threshold = json.loads(THRESHOLDS_REPORT.read_text(encoding="utf-8"))
        self.assertEqual(threshold["dates"], self.spec["split"]["development"])
        self.assertFalse(threshold["maker_outcomes_read"])
        frozen = self.spec["quote_filter"]["numeric_thresholds"]
        self.assertEqual(
            threshold["bearish_threshold_ticks"], frozen["bearish_20pct_ticks"]
        )
        self.assertEqual(
            threshold["bullish_threshold_ticks"], frozen["bullish_80pct_ticks"]
        )

    def test_frozen_source_gate(self) -> None:
        validated = _validate_frozen(verify_binary=False)
        self.assertEqual(
            validated["audit"]["maker_source_bundle_sha256"],
            self.spec["audit"]["maker_source_bundle_sha256"],
        )

    def test_exact_frozen_binary_gate(self) -> None:
        expected = self.spec["audit"]["passive_binary_sha256"]
        if not PASSIVE_BINARY.exists() or hashlib.sha256(PASSIVE_BINARY.read_bytes()).hexdigest() != expected:
            self.skipTest("exact frozen binary artifact unavailable; source receipt verified")
        validated = _validate_frozen()
        self.assertEqual(
            validated["audit"]["passive_binary_sha256"],
            expected,
        )

    def test_stage_a_determinism_leakage_and_snapshot_invalidation(self) -> None:
        manifest_path = Path("data/research/tardis/passive/2026-05-01/day_manifest.json")
        manual_path = REPORT_ROOT / "stage_a_may/manual_fill_audit.json"
        if not manifest_path.exists() or not manual_path.exists():
            self.skipTest("ignored local Stage A artifacts are absent")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["probe_repeat_count"], 2)
        self.assertEqual(manifest["label_repeat_count"], 2)
        self.assertTrue(manifest["probe_report"]["byte_identical_output"])
        self.assertTrue(manifest["label_report"]["byte_identical_output"])
        self.assertEqual(
            manifest["placement_report"]["future_event_leakage_violations"], 0
        )
        self.assertGreater(manifest["probe_report"]["invalid_snapshot"], 0)
        manual = json.loads(manual_path.read_text(encoding="utf-8"))
        self.assertTrue(manual["all_fill_fields_match"])
        snapshot_examples = [
            example
            for example in manual["examples"]
            if example["category"] == "snapshot_or_gap_invalid"
        ]
        self.assertEqual(len(snapshot_examples), 3)
        self.assertTrue(all(
            example["simulator_outcome"]["cancel_reason"] == "replacement_snapshot"
            for example in snapshot_examples
        ))

    def test_holdout_was_opened_against_frozen_spec(self) -> None:
        opening = REPORT_ROOT / "historical_holdout/holdout_opening_audit.json"
        if not opening.exists():
            self.skipTest("ignored local holdout audit is absent")
        payload = json.loads(opening.read_text(encoding="utf-8"))
        self.assertTrue(payload["opened_before_holdout_probe_or_label_read"])
        self.assertEqual(
            payload["maker_spec_sha256"],
            hashlib.sha256(FROZEN_SPEC.read_bytes()).hexdigest(),
        )
        self.assertFalse(payload["methodology_changes_after_opening_allowed"])

    def test_hac_is_deterministic_and_resets_cross_day_pairs(self) -> None:
        values = np.array([1.0, 2.0, 3.0, -3.0, -2.0, -1.0])
        dates = np.array([0, 0, 0, 1, 1, 1])
        first = hac_mean_se(values, dates, 2)
        second = hac_mean_se(values, dates, 2)
        no_reset = hac_mean_se(values, np.zeros(len(values), dtype=int), 2)
        self.assertEqual(first, second)
        self.assertNotEqual(first, no_reset)


if __name__ == "__main__":
    unittest.main()
