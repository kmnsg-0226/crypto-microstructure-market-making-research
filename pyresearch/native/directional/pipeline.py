"""Run the directional sweep / execution-feasibility phase.

    python -m pyresearch.native.directional.pipeline frame        # the directional frame
    python -m pyresearch.native.directional.pipeline descriptive  # deciles, decomposition, cost hurdles
    python -m pyresearch.native.directional.pipeline events       # raw event-time study
    python -m pyresearch.native.directional.pipeline signal       # lead time and false positives
    python -m pyresearch.native.directional.pipeline models       # model comparison, audit, magnitude
    python -m pyresearch.native.directional.pipeline all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess

import numpy as np
import pandas as pd

from pyresearch.native.cancel import scoring as cancel_scoring
from pyresearch.native.directional import analysis, data, events, models, signal, spec
from pyresearch.native.predictive import data as predictive_data
from pyresearch.native.queue_tail import data as qt_data
from pyresearch.native.queue_tail import spec as qt_spec
from pyresearch.native.core import corpus

FLOAT_FORMAT = "%.10g"


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(frame: pd.DataFrame, name: str) -> None:
    spec.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    frame.to_csv(spec.REPORT_DIR / name, index=False, float_format=FLOAT_FORMAT)
    print(f"  {name}: {len(frame):,} rows")


def write_methodology() -> None:
    spec.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    inputs = {
        "git_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=spec.ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        "corpus_spec_sha256": _sha256(corpus.SPEC_PATH),
        "phase_4a_oof_sweep_predictions_sha256": _sha256(
            qt_spec.DATA_DIR / "oof_sweep_predictions.csv.zst"
        ),
        "phase_4b_sweep_scores_sha256": {
            f"file{entry.file_index}": _sha256(cancel_scoring.scores_path(entry.file_index))
            for entry in corpus.CORPUS
            if cancel_scoring.scores_path(entry.file_index).exists()
        },
        "phase_1_model_frame_sha256": {
            f"file{entry.file_index}": _sha256(predictive_data.frame_path(entry.file_index))
            for entry in corpus.CORPUS
        },
        "phase_4a_level_grid_sha256": {
            f"file{entry.file_index}": _sha256(qt_data.grid_path(entry.file_index))
            for entry in corpus.CORPUS
        },
        "phase_3_mid_path_sha256": {
            f"file{entry.file_index}": _sha256(
                spec.ROOT / f"data/research/native_economic_v1/mid_path_file{entry.file_index}.csv.zst"
            )
            for entry in corpus.CORPUS
        },
        "phase_2_cross_stream_timing_sha256": {
            f"file{entry.file_index}": _sha256(events.timing_path(entry.file_index))
            for entry in corpus.CORPUS
        },
    }
    (spec.REPORT_DIR / "methodology.json").write_text(
        json.dumps(spec.methodology(inputs), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    folds = models.directional_folds()
    pd.DataFrame(
        [
            {
                "fold": fold.index,
                "block": fold.block,
                "train_end_ns": fold.train_end_ns,
                "validation_start_ns": fold.validation_start_ns,
                "validation_end_ns": fold.validation_end_ns,
                "train_end_utc": predictive_data._utc(fold.train_end_ns),
                "validation_start_utc": predictive_data._utc(fold.validation_start_ns),
                "validation_end_utc": predictive_data._utc(fold.validation_end_ns),
                "purge_seconds": (fold.validation_start_ns - fold.train_end_ns) / 1e9,
            }
            for fold in folds
        ]
    ).to_csv(spec.REPORT_DIR / "folds.csv", index=False, float_format=FLOAT_FORMAT)


def run_frame() -> None:
    write_methodology()
    qc = data.build_and_save()
    (spec.REPORT_DIR / "frame_qc.json").write_text(
        json.dumps(qc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------------------------
# Descriptive
# --------------------------------------------------------------------------------------------
def run_descriptive() -> None:
    write_methodology()
    frame = data.load_frame()
    frame["block"] = analysis.block_id(frame["timestamp_ns"].to_numpy())
    frame["utc_day"] = analysis.utc_day(frame["timestamp_ns"].to_numpy())
    frame["side_name"] = np.where(frame["side"].to_numpy() == 1, "threatened_ask", "threatened_bid")

    deciles = analysis.decile_table(frame)
    deciles.insert(0, "population", "all")
    by_side = analysis.decile_table(frame, ["side_name"])
    by_side.insert(0, "population", "by_side")
    combined = pd.concat([deciles, by_side], ignore_index=True)
    _write(combined, "sweep_deciles.csv")

    _write(
        analysis.monotonicity(
            combined[combined["population"] == "all"],
            ["population"],
            "decile",
            f"markout_{spec.PRIMARY_HORIZON_MS}ms_mean_ticks",
        ).pipe(
            lambda base: pd.concat(
                [base]
                + [
                    analysis.monotonicity(
                        combined[combined["population"] == "all"], ["population"], "decile", column
                    )
                    for column in (
                        "realised_sweep_rate_500ms",
                        "p_next_move_follows_sweep",
                        f"markout_{spec.SECONDARY_HORIZON_MS}ms_mean_ticks",
                        "first_move_mean_ticks",
                        f"markout_{spec.PRIMARY_HORIZON_MS}ms_frac_favourable",
                    )
                ],
                ignore_index=True,
            )
        ),
        "decile_monotonicity.csv",
    )

    thresholds = analysis.threshold_table(frame)
    thresholds.insert(0, "population_key", "all")
    _write(thresholds, "gross_edge.csv")

    side = analysis.threshold_table(frame, ["side_name"])
    _write(side, "side_asymmetry.csv")

    frame["_score_band"] = pd.cut(
        frame["sweep_p"],
        [-np.inf] + list(spec.SWEEP_THRESHOLDS) + [np.inf],
        labels=["<0.30", "0.30-0.50", "0.50-0.70", "0.70-0.90", ">=0.90"],
    )
    # Pooled over both sides the decomposition is degenerate: the bid and ask rows of one
    # instant are exact mirrors, so P(right) equals P(wrong) and the sizes cancel by
    # construction. It is only informative once conditioned on the score.
    parts = []
    for horizon in spec.HEADLINE_HORIZONS_MS:
        for group, label in ((["_score_band"], "score_band"), (["side_name"], "side")):
            block = analysis.decompose(frame, horizon, group)
            block = block.rename(columns={group[0]: "group_value"})
            block.insert(0, "grouping", label)
            parts.append(block)
        block = analysis.decompose(frame, horizon, ["_score_band", "side_name"])
        block["group_value"] = (
            block["_score_band"].astype(str) + " / " + block["side_name"].astype(str)
        )
        block = block.drop(columns=["_score_band", "side_name"])
        block.insert(0, "grouping", "score_band_and_side")
        parts.append(block)
    _write(pd.concat(parts, ignore_index=True), "probability_magnitude.csv")

    conditional = []
    for horizon in spec.HEADLINE_HORIZONS_MS:
        conditional.append(analysis.conditional_split(frame, horizon, ["_score_band"]))
    conditional = pd.concat(conditional, ignore_index=True).rename(
        columns={"_score_band": "score_band"}
    )
    _write(conditional, "conditional_vs_unconditional.csv")

    hurdle, breakeven = [], []
    for horizon in spec.HEADLINE_HORIZONS_MS:
        for table, keys, label in (
            (thresholds, ["threshold", "population"], "threshold"),
            (combined[combined["population"] == "all"], ["decile"], "decile"),
        ):
            block = analysis.cost_hurdle(table, keys, horizon)
            block.insert(0, "grouping", label)
            hurdle.append(block)
            block = analysis.break_even(table, keys, horizon)
            block.insert(0, "grouping", label)
            breakeven.append(block)
    _write(pd.concat(hurdle, ignore_index=True), "cost_hurdle.csv")
    _write(pd.concat(breakeven, ignore_index=True), "break_even_cost.csv")

    _write(analysis.regime_table(frame, spec.PRIMARY_HORIZON_MS), "activity_regimes.csv")

    for name, keys in (
        ("block_stability", ["block"]),
        ("day_stability", ["utc_day"]),
        ("segment_stability", ["file_index", "segment_id"]),
    ):
        table = analysis.stability(frame, keys, spec.PRIMARY_HORIZON_MS)
        _write(table, f"{name}.csv")
        if name == "block_stability":
            summaries = [
                analysis.block_summary(
                    table[table["population"] == population],
                    ["threshold", "population"],
                    column,
                )
                for population in ("all", "score_at_or_above")
                for column in (
                    f"markout_{spec.PRIMARY_HORIZON_MS}ms_mean_ticks",
                    f"markout_{spec.PRIMARY_HORIZON_MS}ms_mean_bps",
                    "p_next_move_follows_sweep",
                )
            ]
            _write(pd.concat(summaries, ignore_index=True), "block_stability_summary.csv")


# --------------------------------------------------------------------------------------------
# Events and signal timing
# --------------------------------------------------------------------------------------------
def run_events() -> None:
    table, paths = events.run()
    _write(table, "event_study.csv")
    spec.DATA_DIR.mkdir(parents=True, exist_ok=True)
    paths.to_parquet(spec.DATA_DIR / "event_paths.parquet", index=False, compression="zstd")


def run_signal() -> None:
    frame = data.load_frame()
    crossings = signal.episode_signal_table(frame)
    spec.DATA_DIR.mkdir(parents=True, exist_ok=True)
    crossings.to_parquet(spec.DATA_DIR / "episode_crossings.parquet", index=False, compression="zstd")
    _write(signal.lead_time_summary(crossings), "signal_lead_time.csv")
    _write(signal.false_positive_table(crossings), "false_positive_analysis.csv")


# --------------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------------
def run_models() -> None:
    frame = models.add_model_targets(models.model_frame(data.load_frame()))
    folds = models.directional_folds()
    print(f"model frame: {len(frame):,} rows, {len(folds)} folds")

    pooled, fold_metrics, calibration, predictions = models.run_comparison(frame, folds)
    _write(pooled, "direction_model_comparison.csv")
    _write(fold_metrics, "fold_metrics.csv")
    _write(calibration, "calibration.csv")

    _write(models.nested_audit(frame, folds, predictions), "incremental_information.csv")
    _write(models.residual_diagnostic(frame, folds), "residual_diagnostic.csv")

    magnitude, buckets = models.magnitude_models(frame, folds)
    _write(magnitude, "magnitude_model.csv")
    _write(buckets, "magnitude_buckets.csv")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage", choices=["frame", "descriptive", "events", "signal", "models", "all"]
    )
    stage = parser.parse_args().stage
    if stage in ("frame", "all"):
        run_frame()
    if stage in ("descriptive", "all"):
        run_descriptive()
    if stage in ("events", "all"):
        run_events()
    if stage in ("signal", "all"):
        run_signal()
    if stage in ("models", "all"):
        run_models()


if __name__ == "__main__":
    main()
