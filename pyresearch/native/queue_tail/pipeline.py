"""Run the queue-dynamics and catastrophic-tail phase.

    python -m pyresearch.native.queue_tail.pipeline frames        # lifecycle model frame
    python -m pyresearch.native.queue_tail.pipeline descriptive   # episodes, tail, buckets, birth cohort
    python -m pyresearch.native.queue_tail.pipeline models        # models D, E, severity and the ablation
    python -m pyresearch.native.queue_tail.pipeline all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess

import numpy as np
import pandas as pd

from pyresearch.native.predictive import data as predictive_data
from pyresearch.native.predictive import modeling
from pyresearch.native.predictive.modeling import Problem
from pyresearch.native.queue_tail import analysis, data, spec
from pyresearch.native.core import corpus

FLOAT_FORMAT = "%.10g"


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        "level_episodes_sha256": {
            f"file{i}": _sha256(data.episodes_path(i)) for i in (0, 1, 2)
        },
        "level_grid_sha256": {f"file{i}": _sha256(data.grid_path(i)) for i in (0, 1, 2)},
        "birth_fills_sha256": {
            f"file{i}": _sha256(data.birth_fills_path(i)) for i in (0, 1, 2)
        },
    }
    (spec.REPORT_DIR / "methodology.json").write_text(
        json.dumps(spec.methodology(inputs), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    schema = {
        "schema": "crypto-hft-queue-feature-schema-v1",
        "feature_sets": spec.FEATURE_SETS,
        "target_definitions": {
            "level_disappears_{h}ms": "1 if the currently best price ceases to be best within "
            "h for an observable reason; 0 if it survives the full horizon inside the segment; "
            "empty if the horizon or the episode end is censored by the segment edge",
            "trade_through_within_{h}ms": "1 if an aggressive print lands beyond the quote "
            "price within h of the observation instant",
            "catastrophic_{t}": "1 if the signed one-second post-fill markout is at or below "
            "-t ticks; only defined for filled opportunities",
            "severity_ticks": "-markout among fills at or below the severe threshold",
        },
        "causality": "every feature is a function of events with a receive timestamp at or "
        "before the row timestamp; level lifecycle state is accumulated forward only",
        "terminology": {
            "level_age_is_not_queue_rank": True,
            "unexplained_removal_is_not_cancellation": True,
        },
    }
    (spec.REPORT_DIR / "queue_feature_schema.json").write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------------------------
# Folds, shared geometry with phases 2 and 3
# --------------------------------------------------------------------------------------------
def canonical_folds() -> list:
    stamps = predictive_data.load_model_frame(columns=["timestamp_ns"])[
        "timestamp_ns"
    ].to_numpy()
    return predictive_data.build_folds(stamps)


# --------------------------------------------------------------------------------------------
# Descriptive
# --------------------------------------------------------------------------------------------
def run_descriptive() -> None:
    spec.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    episodes = data.load_episodes()
    analysis.level_survival_summary(episodes).to_csv(
        spec.REPORT_DIR / "level_survival_summary.csv", index=False, float_format=FLOAT_FORMAT
    )
    analysis.depletion_replenishment_summary(episodes).to_csv(
        spec.REPORT_DIR / "depletion_replenishment_summary.csv",
        index=False,
        float_format=FLOAT_FORMAT,
    )
    # The episode table itself is hundreds of thousands of rows and lives under data/; what is
    # committed here is its distribution by side, close reason and duration decade.
    episodes["duration_bucket"] = pd.cut(
        episodes["duration_ms"],
        [0, 100, 250, 500, 1000, 5000, 30000, np.inf],
        labels=["<=100ms", "100-250ms", "250-500ms", "500ms-1s", "1-5s", "5-30s", ">30s"],
    )
    episodes.groupby(
        ["side", "close_reason", "duration_bucket"], observed=True
    ).agg(
        episodes=("level_episode_id", "size"),
        mean_duration_ms=("duration_ms", "mean"),
        fully_removed_rate=("fully_removed", "mean"),
        replenished_rate=("replenish_events", lambda values: float((values > 0).mean())),
        mean_initial_qty=("initial_qty", "mean"),
        mean_prints_at_quote=("prints_at_quote", "mean"),
        mean_prints_through=("prints_through", "mean"),
        median_unexplained_share=("unexplained_removal_share", "median"),
    ).reset_index().to_csv(
        spec.REPORT_DIR / "level_episodes.csv", index=False, float_format=FLOAT_FORMAT
    )

    frame = data.load_model_frame()
    analysis.describe_features(frame, spec.FEATURE_SETS["all"]).to_csv(
        spec.REPORT_DIR / "queue_feature_summary.csv", index=False, float_format=FLOAT_FORMAT
    )
    analysis.hazard_curve(frame).to_csv(
        spec.REPORT_DIR / "hazard_curve.csv", index=False, float_format=FLOAT_FORMAT
    )

    sweep_rows = []
    for keys, label in (
        (["side"], "side"),
        (["file_index", "segment_id"], "segment"),
    ):
        grouped = frame.groupby(keys, observed=True)
        table = grouped.agg(
            observations=("timestamp_ns", "size"),
            **{
                f"level_disappears_{h}ms": (f"level_disappears_{h}ms", "mean")
                for h in spec.SURVIVAL_HORIZONS_MS
            },
            **{
                f"trade_through_within_{h}ms": (f"trade_through_within_{h}ms", "mean")
                for h in spec.SWEEP_HORIZONS_MS
            },
        ).reset_index()
        table.insert(0, "grouping", label)
        sweep_rows.append(table)
    pd.concat(sweep_rows, ignore_index=True).to_csv(
        spec.REPORT_DIR / "sweep_risk_summary.csv", index=False, float_format=FLOAT_FORMAT
    )

    # Tail behaviour of the phase 3 grid cohort and of the level-birth cohort.
    tails = []
    contributions = []
    for cohort, birth in (("grid", False), ("level_birth", True)):
        fills = data.load_fills(level_birth=birth)
        table = analysis.tail_distribution(fills, ["queue_cell"])
        table.insert(0, "cohort", cohort)
        tails.append(table)
        by_side = analysis.tail_distribution(fills, ["queue_cell", "side"])
        by_side.insert(0, "cohort", f"{cohort}_by_side")
        tails.append(by_side)
        contribution = analysis.tail_contribution(fills, ["queue_cell"])
        contribution.insert(0, "cohort", cohort)
        contributions.append(contribution)
        if birth:
            _write_birth_cohort(fills)
        del fills
    pd.concat(tails, ignore_index=True).to_csv(
        spec.REPORT_DIR / "tail_distribution.csv", index=False, float_format=FLOAT_FORMAT
    )
    pd.concat(contributions, ignore_index=True).to_csv(
        spec.REPORT_DIR / "tail_contribution.csv", index=False, float_format=FLOAT_FORMAT
    )

    # Bucket and interaction studies against level failure, sweep risk and the tail.
    fills = data.load_fills()
    primary = fills[fills["queue_cell"] == spec.PRIMARY_QUEUE_CELL]
    joined = data.join_placement_features(primary, frame)
    outcomes = {
        "level_disappears_500ms": "level_disappears_500ms",
        "level_disappears_1000ms": "level_disappears_1000ms",
        "trade_through_within_500ms": "trade_through_within_500ms",
        "trade_through_within_1000ms": "trade_through_within_1000ms",
        "fill_rate": "filled",
        "catastrophic_25": "catastrophic_25",
        "catastrophic_50": "catastrophic_50",
        "mean_markout_1s_ticks": "markout_1000ms_ticks",
    }
    analysis.bucket_studies(
        joined, spec.BUCKET_SIGNALS, outcomes, spec.BUCKETS
    ).to_csv(
        spec.REPORT_DIR / "queue_bucket_studies.csv", index=False, float_format=FLOAT_FORMAT
    )
    analysis.interaction_studies(
        joined, spec.INTERACTION_PAIRS, outcomes, spec.INTERACTION_BUCKETS
    ).to_csv(
        spec.REPORT_DIR / "queue_interaction_studies.csv",
        index=False,
        float_format=FLOAT_FORMAT,
    )
    print(f"descriptive artifacts written over {len(joined):,} joined placements")


def run_stability() -> None:
    """Tail behaviour by chronological block, UTC day and segment."""
    folds = canonical_folds()
    fills = data.load_fills()
    stamps = fills["placement_ns"].to_numpy()
    block = np.full(len(fills), -1)
    for fold in folds:
        inside = (stamps >= fold.validation_start_ns) & (stamps < fold.validation_end_ns)
        block[inside] = fold.index
    fills["phase2_block"] = block
    fills["utc_day"] = pd.to_datetime(stamps, unit="ns", utc=True).date.astype(str)
    tables = []
    for keys, label in (
        (["queue_cell", "phase2_block"], "phase2_validation_block"),
        (["queue_cell", "utc_day"], "utc_day"),
        (["queue_cell", "file_index", "segment_id"], "segment"),
        (["queue_cell", "side"], "side"),
    ):
        table = analysis.tail_distribution(fills, keys)
        table.insert(0, "grouping", label)
        tables.append(table)
    pd.concat(tables, ignore_index=True).to_csv(
        spec.REPORT_DIR / "tail_by_block.csv", index=False, float_format=FLOAT_FORMAT
    )
    print("tail stability written")


def _write_birth_cohort(fills: pd.DataFrame) -> None:
    """The level-birth cohort, plus an ex-post decomposition by what the level then did.

    The lifecycle split below uses information from after placement and is therefore a mechanism
    description only. It is never a feature of any model in this phase.
    """
    episodes = data.load_episodes()
    keyed = episodes.set_index(["file_index", "side", "price_ticks", "start_ns"])
    index = pd.MultiIndex.from_arrays(
        [
            fills["file_index"],
            fills["side"],
            fills["quote_px_ticks"],
            fills["placement_ns"],
        ]
    )
    for column in (
        "duration_ms",
        "replenish_events",
        "cum_trade_at_quote",
        "cum_unexplained_remove",
        "cum_remove",
        "close_reason",
        "fully_removed",
    ):
        fills[f"episode_{column}"] = index.map(keyed[column]).to_numpy()

    duration = fills["episode_duration_ms"].to_numpy(dtype="float64")
    removed = fills["episode_cum_remove"].to_numpy(dtype="float64")
    traded = fills["episode_cum_trade_at_quote"].to_numpy(dtype="float64")
    fills["lifecycle_class"] = np.select(
        [
            ~np.isfinite(duration),
            duration <= 250,
            fills["episode_replenish_events"].to_numpy(dtype="float64") >= 5,
            np.where(removed > 0, traded / np.where(removed > 0, removed, np.nan), 0) >= 0.25,
            duration >= 1000,
        ],
        [
            "unmatched",
            "died_within_250ms",
            "repeatedly_replenished",
            "consumed_mainly_by_prints",
            "persisted_over_1s",
        ],
        default="other",
    )
    rows = []
    for keys, label in (
        (["queue_cell"], "all"),
        (["queue_cell", "side"], "by_side"),
        (["queue_cell", "lifecycle_class"], "by_lifecycle_class"),
        (["queue_cell", "episode_close_reason"], "by_close_reason"),
    ):
        table = analysis.tail_distribution(fills, keys)
        table.insert(0, "grouping", label)
        rows.append(table)
    combined = pd.concat(rows, ignore_index=True)
    combined.to_csv(
        spec.REPORT_DIR / "level_birth_cohort.csv", index=False, float_format=FLOAT_FORMAT
    )


# --------------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------------
def _classification(name: str, target: str, features: list[str], family: str, tags: dict):
    return Problem(
        name=name,
        family=family,
        target=target,
        task="classification",
        features=features,
        description=name,
        tags=tags,
    )


def _run(problems, frame, folds, oof_name: str | None):
    pooled: list[dict] = []
    folds_out: list[pd.DataFrame] = []
    weights_out: list[pd.DataFrame] = []
    calibration_out: list[pd.DataFrame] = []
    wide: pd.DataFrame | None = None
    keys = ["timestamp_ns", "file_index", "segment_id", "side"]
    for problem in problems:
        oof, fold_metrics, weights = modeling.run_problem(problem, frame, folds)
        if oof.empty:
            continue
        folds_out.append(fold_metrics)
        if not weights.empty:
            weights.insert(0, "family", problem.family)
            weights_out.append(weights)
        pooled.extend(modeling.pooled_metrics(problem, oof))
        if problem.task == "classification":
            for model in ("linear", "lightgbm"):
                calibration_out.append(modeling.calibration(problem, oof, model))
        if oof_name is not None:
            present = [c for c in keys if c in oof.columns]
            renamed = oof[present + ["y"] + [f"pred_{m}" for m in modeling.MODELS]].rename(
                columns={
                    "y": f"y_{problem.name}",
                    **{f"pred_{m}": f"{problem.name}_{m}" for m in modeling.MODELS},
                }
            )
            wide = renamed if wide is None else wide.merge(renamed, on=present, how="outer")
        print(f"  {problem.name}: {len(oof):,} OOF rows")
    if wide is not None and oof_name is not None:
        spec.DATA_DIR.mkdir(parents=True, exist_ok=True)
        wide.to_csv(spec.DATA_DIR / oof_name, index=False, float_format=FLOAT_FORMAT)
    return pooled, folds_out, weights_out, calibration_out


def run_models() -> None:
    write_methodology()
    folds = canonical_folds()
    predictive_data.fold_table(
        predictive_data.load_model_frame(columns=["timestamp_ns"])["timestamp_ns"].to_numpy(),
        folds,
    ).to_csv(spec.REPORT_DIR / "folds.csv", index=False, float_format=FLOAT_FORMAT)

    frame = data.load_model_frame()
    all_features = spec.FEATURE_SETS["all"]
    pooled: list[dict] = []
    folds_out: list[pd.DataFrame] = []
    weights_out: list[pd.DataFrame] = []
    calibration_out: list[pd.DataFrame] = []

    print("model D: level failure")
    problems = [
        _classification(
            f"level_disappears_{h}ms",
            f"level_disappears_{h}ms",
            all_features,
            "level_failure",
            {"horizon_ms": str(h)},
        )
        for h in spec.LEVEL_FAILURE_HORIZONS_MS
    ]
    result = _run(problems, frame, folds, "oof_level_failure_predictions.csv.zst")
    pooled += result[0]
    folds_out += result[1]
    weights_out += result[2]
    calibration_out += result[3]

    print("model D: sweep risk")
    problems = [
        _classification(
            f"trade_through_within_{h}ms",
            f"trade_through_within_{h}ms",
            all_features,
            "sweep",
            {"horizon_ms": str(h)},
        )
        for h in spec.SWEEP_MODEL_HORIZONS_MS
    ]
    result = _run(problems, frame, folds, "oof_sweep_predictions.csv.zst")
    pooled += result[0]
    folds_out += result[1]
    weights_out += result[2]
    calibration_out += result[3]

    print("model E: catastrophic fill risk")
    fills = data.load_fills()
    catastrophic_pooled: list[dict] = []
    severity_pooled: list[dict] = []
    wide_frames = []
    for cell_name in spec.QUEUE_CELLS:
        block = data.join_placement_features(
            fills[fills["queue_cell"] == cell_name], frame
        ).sort_values(["timestamp_ns", "side"], ignore_index=True)
        filled = block[block["filled"]].reset_index(drop=True)
        problems = [
            _classification(
                f"catastrophic_{threshold}_{cell_name}",
                f"catastrophic_{threshold}",
                all_features,
                "catastrophic",
                {"queue_cell": cell_name, "threshold_ticks": str(threshold)},
            )
            for threshold in (
                spec.PRIMARY_CATASTROPHIC_TICKS,
                spec.SECONDARY_CATASTROPHIC_TICKS,
            )
        ]
        result = _run(problems, filled, folds, None)
        catastrophic_pooled += result[0]
        folds_out += result[1]
        weights_out += result[2]
        calibration_out += result[3]
        if cell_name == spec.PRIMARY_QUEUE_CELL:
            oof, fold_metrics, _ = modeling.run_problem(problems[0], filled, folds)
            if not oof.empty:
                wide_frames.append(oof)
            # Conditional severity, among fills already at or below the severe threshold.
            severity = Problem(
                name=f"severity_{cell_name}",
                family="severity",
                target="severity_ticks",
                task="regression",
                features=all_features,
                description="tick severity among catastrophic fills",
                tags={"queue_cell": cell_name},
            )
            result = _run([severity], filled, folds, None)
            severity_pooled += result[0]
            folds_out += result[1]
            weights_out += result[2]
        del block, filled
    if wide_frames:
        spec.DATA_DIR.mkdir(parents=True, exist_ok=True)
        wide_frames[0].to_csv(
            spec.DATA_DIR / "oof_catastrophic_predictions.csv.zst",
            index=False,
            float_format=FLOAT_FORMAT,
        )

    pd.DataFrame([r for r in pooled if r["family"] == "level_failure"]).to_csv(
        spec.REPORT_DIR / "level_failure_metrics.csv", index=False, float_format=FLOAT_FORMAT
    )
    pd.DataFrame([r for r in pooled if r["family"] == "sweep"]).to_csv(
        spec.REPORT_DIR / "sweep_model_metrics.csv", index=False, float_format=FLOAT_FORMAT
    )
    pd.DataFrame(catastrophic_pooled).to_csv(
        spec.REPORT_DIR / "catastrophic_model_metrics.csv",
        index=False,
        float_format=FLOAT_FORMAT,
    )
    pd.DataFrame(severity_pooled).to_csv(
        spec.REPORT_DIR / "tail_severity_metrics.csv", index=False, float_format=FLOAT_FORMAT
    )
    pd.concat(folds_out, ignore_index=True).to_csv(
        spec.REPORT_DIR / "fold_metrics.csv", index=False, float_format=FLOAT_FORMAT
    )
    if calibration_out:
        pd.concat(
            [c for c in calibration_out if not c.empty], ignore_index=True
        ).to_csv(spec.REPORT_DIR / "calibration.csv", index=False, float_format=FLOAT_FORMAT)

    weights = pd.concat(weights_out, ignore_index=True)
    weights[weights["model"] == "linear"].groupby(
        ["problem", "feature"], as_index=False
    ).agg(
        mean_coefficient=("value", "mean"),
        median_coefficient=("value", "median"),
        folds_positive=("value", lambda values: float((values > 0).mean())),
        folds=("value", "size"),
    ).to_csv(
        spec.REPORT_DIR / "model_coefficients.csv", index=False, float_format=FLOAT_FORMAT
    )
    gbm = weights[weights["model"] == "lightgbm"]
    importance = gbm.groupby(["problem", "feature"], as_index=False).agg(
        mean_gain=("value", "mean"), folds=("value", "size")
    )
    importance["gain_share"] = importance["mean_gain"] / importance.groupby("problem")[
        "mean_gain"
    ].transform("sum")
    importance.sort_values(
        ["problem", "mean_gain"], ascending=[True, False], ignore_index=True
    ).to_csv(spec.REPORT_DIR / "feature_importance.csv", index=False, float_format=FLOAT_FORMAT)
    print("model artifacts written")


def run_ablation() -> None:
    """Pre-registered feature-group comparison, identical folds and seeds throughout."""
    folds = canonical_folds()
    frame = data.load_model_frame()
    fills = data.load_fills()
    catastrophic = data.join_placement_features(
        fills[fills["queue_cell"] == spec.PRIMARY_QUEUE_CELL], frame
    ).sort_values(["timestamp_ns", "side"], ignore_index=True)
    catastrophic = catastrophic[catastrophic["filled"]].reset_index(drop=True)
    del fills

    rows = []
    for target in spec.ABLATION_TARGETS:
        population = catastrophic if target.startswith("catastrophic") else frame
        for set_name, features in spec.FEATURE_SETS.items():
            problem = _classification(
                f"{target}__{set_name}",
                target,
                features,
                "ablation",
                {"target": target, "feature_set": set_name},
            )
            oof, _, _ = modeling.run_problem(problem, population, folds)
            if oof.empty:
                continue
            for record in modeling.pooled_metrics(problem, oof):
                rows.append(record)
            print(f"  {target} / {set_name}: {len(oof):,} OOF rows")
    pd.DataFrame(rows).to_csv(
        spec.REPORT_DIR / "feature_ablation.csv", index=False, float_format=FLOAT_FORMAT
    )
    print("ablation written")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=(
            "frames",
            "methodology",
            "descriptive",
            "stability",
            "models",
            "ablation",
            "all",
        ),
    )
    arguments = parser.parse_args()
    if arguments.stage in ("frames", "all"):
        data.build_and_save()
    if arguments.stage in ("methodology", "all"):
        write_methodology()
    if arguments.stage in ("descriptive", "all"):
        run_descriptive()
    if arguments.stage in ("stability", "all"):
        run_stability()
    if arguments.stage in ("models", "all"):
        run_models()
    if arguments.stage in ("ablation", "all"):
        run_ablation()


if __name__ == "__main__":
    main()
