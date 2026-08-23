import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from pyresearch.support.evaluate import (
    assert_split_embargo,
    assign_bins,
    decile_report,
    fit_transforms,
    univariate_report,
    validate_stage_dates,
    write_csv,
)


SPEC = json.loads(
    (Path(__file__).parents[1] / "research/specs/research_spec_draft.json").read_text()
)


def evaluation_frame() -> pd.DataFrame:
    rows = 120
    data: dict[str, object] = {
        "date": ["2026-05-01"] * rows,
        "sample_time_us": 1_777_593_600_000_000 + np.arange(rows) * 100_000,
        "feature_segment_id": 1,
        "valid_book_state": 1,
        "spread_ticks": np.where(np.arange(rows) % 2, 1.0, 2.0),
        "mid_ticks_x2": 100_000 + np.arange(rows),
    }
    for offset, signal in enumerate(SPEC["canonical_signals"], start=1):
        data[signal] = np.arange(rows, dtype=float) + offset
    for features in SPEC["models"]["specifications"].values():
        for feature in features:
            data.setdefault(feature, np.arange(rows, dtype=float) + 0.5)
    for horizon in SPEC["label_horizons_ms"]:
        name = {100: "100ms", 500: "500ms", 1000: "1s", 5000: "5s"}[horizon]
        data[f"markout_{name}_ticks"] = np.arange(rows, dtype=float) / 10.0
    return pd.DataFrame(data)


class ResearchEvaluationTest(unittest.TestCase):
    def test_development_only_thresholds_and_scaling(self) -> None:
        development = evaluation_frame()
        first = fit_transforms(development, SPEC)
        changed_oos = development.copy()
        changed_oos.loc[:, "obi_l1"] += 1e6
        second = fit_transforms(development, SPEC)
        self.assertEqual(first, second)
        self.assertNotEqual(
            first["decile_thresholds"]["obi_l1"],
            fit_transforms(changed_oos, SPEC)["decile_thresholds"]["obi_l1"],
        )
        self.assertEqual(first["standardization"]["ti_1s"]["count"], len(development))

    def test_frozen_bins_are_applied_without_refit(self) -> None:
        thresholds = list(range(1, 10))
        values = pd.Series([-100.0, 1.0, 1.5, 9.0, 100.0, np.nan])
        self.assertEqual(assign_bins(values, thresholds).tolist(), [1, 2, 2, 10, 10, pd.NA])

    def test_split_dates_and_embargo(self) -> None:
        validate_stage_dates("preliminary", ["2026-05-01"], SPEC)
        with self.assertRaises(ValueError):
            validate_stage_dates("development", ["2026-05-01"], SPEC)
        frame = evaluation_frame()
        self.assertEqual(sum(assert_split_embargo(frame, SPEC).values()), 0)
        day_us = 86_400_000_000
        frame.loc[0, "sample_time_us"] = (
            frame.loc[0, "sample_time_us"] // day_us + 1
        ) * day_us - 100_000
        with self.assertRaisesRegex(ValueError, "embargo"):
            assert_split_embargo(frame, SPEC)

    def test_statistical_csv_is_deterministic(self) -> None:
        frame = pd.DataFrame({"b": [1.0, np.nan], "a": ["x", "y"]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.csv"
            write_csv(path, frame)
            first = path.read_bytes()
            write_csv(path, frame)
            self.assertEqual(first, path.read_bytes())

    def test_statistical_calculations_are_deterministic(self) -> None:
        frame = evaluation_frame()
        transforms = fit_transforms(frame, SPEC)
        pd.testing.assert_frame_equal(
            univariate_report(frame, SPEC, "preliminary"),
            univariate_report(frame, SPEC, "preliminary"),
        )
        pd.testing.assert_frame_equal(
            decile_report(frame, SPEC, transforms, "preliminary"),
            decile_report(frame, SPEC, transforms, "preliminary"),
        )


if __name__ == "__main__":
    unittest.main()
