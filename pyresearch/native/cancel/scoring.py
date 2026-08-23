"""Produce blocked out-of-fold sweep scores at the 100 ms decision cadence.

Phase 4A fitted its sweep model on a one-second decimation of the lifecycle grid and stored the
out-of-fold predictions for exactly those rows. A resting order cannot wait a second to react
when the median best level lives 204 ms, so this phase needs the same model evaluated between
those instants.

Nothing about the model changes. For every fold the identical training rows, features,
parameters and seed produce the identical booster, which is then asked for predictions on the
100 ms rows inside that fold's validation window. The refit is verified against the stored phase
4A out-of-fold values on their shared one-second rows before any score is used, so a drifting
model is caught rather than silently absorbed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pyresearch.native.cancel import spec
from pyresearch.native.predictive import data as predictive_data
from pyresearch.native.predictive import spec as predictive_spec
from pyresearch.native.queue_tail import data as qt_data
from pyresearch.native.queue_tail import spec as qt_spec
from pyresearch.native.core import corpus

KEYS = ["timestamp_ns", "file_index", "segment_id", "side"]
CARRY = ["quote_price_ticks", "level_episode_id", "episode_end_ns", "segment_end_ns"]


def features() -> list[str]:
    return qt_spec.FEATURE_SETS[spec.SWEEP_FEATURE_SET]


def decision_frame_path(file_index: int):
    return spec.DATA_DIR / f"decision_frame_file{file_index}.parquet"


def scores_path(file_index: int):
    return spec.DATA_DIR / f"sweep_scores_file{file_index}.parquet"


def build_decision_frames() -> None:
    """One row per (100 ms instant, side): the same features phase 4A fitted on."""
    spec.DATA_DIR.mkdir(parents=True, exist_ok=True)
    columns = KEYS + CARRY + [spec.SWEEP_TARGET] + features()
    for entry in corpus.CORPUS:
        frame = qt_data.build_model_frame(
            step_ms=spec.DECISION_GRID_MS, files=(entry.file_index,)
        )
        frame = frame[[c for c in columns if c in frame.columns]]
        frame.to_parquet(
            decision_frame_path(entry.file_index), index=False, compression="zstd"
        )
        print(f"  file{entry.file_index}: {len(frame):,} decision rows")
        del frame


def _train_booster(train_x: np.ndarray, train_y: np.ndarray):
    import lightgbm as lgb

    dataset = lgb.Dataset(train_x, label=train_y, free_raw_data=True)
    return lgb.train(
        predictive_spec.LGBM_CLASSIFICATION, dataset, num_boost_round=predictive_spec.LGBM_ROUNDS
    )


def score_all() -> pd.DataFrame:
    """Refit the phase 4A sweep model fold by fold and score the 100 ms rows.

    Returns the verification table comparing the refit against the stored phase 4A out-of-fold
    predictions on their shared one-second rows.
    """
    names = features()
    train_frame = qt_data.load_model_frame(columns=names + [spec.SWEEP_TARGET])
    train_frame = train_frame.sort_values(["timestamp_ns", "side"], ignore_index=True)
    folds = predictive_data.build_folds(
        predictive_data.load_model_frame(columns=["timestamp_ns"])["timestamp_ns"].to_numpy()
    )
    train_stamps = train_frame["timestamp_ns"].to_numpy()
    train_y = train_frame[spec.SWEEP_TARGET].to_numpy(dtype="float64")
    train_x = train_frame[names].to_numpy(dtype="float32")
    labelled = np.isfinite(train_y)

    decision = {
        entry.file_index: pd.read_parquet(decision_frame_path(entry.file_index))
        for entry in corpus.CORPUS
    }
    stored = _stored_phase_4a_predictions()

    outputs = {index: [] for index in decision}
    checks: list[dict] = []
    for fold in folds:
        train = labelled & (train_stamps <= fold.train_end_ns)
        if train.sum() < 1000:
            continue
        booster = _train_booster(train_x[train], train_y[train])
        for index, frame in decision.items():
            stamps = frame["timestamp_ns"].to_numpy()
            window = (stamps >= fold.validation_start_ns) & (
                stamps < fold.validation_end_ns
            )
            if not window.any():
                continue
            block = frame.loc[window, KEYS + CARRY].copy()
            block["fold"] = fold.index
            block["sweep_p"] = booster.predict(
                frame.loc[window, names].to_numpy(dtype="float32")
            )
            block[spec.SWEEP_TARGET] = frame.loc[window, spec.SWEEP_TARGET].to_numpy()
            outputs[index].append(block)
        checks.append(_verify_fold(fold, booster, decision, stored, names))
        print(f"  fold {fold.index}: trained on {int(train.sum()):,} rows")
    for index, blocks in outputs.items():
        if blocks:
            pd.concat(blocks, ignore_index=True).sort_values(
                ["timestamp_ns", "side"], ignore_index=True
            ).to_parquet(scores_path(index), index=False, compression="zstd")
    return pd.DataFrame(checks)


def _stored_phase_4a_predictions() -> pd.DataFrame:
    frame = qt_data._read(qt_spec.DATA_DIR / "oof_sweep_predictions.csv.zst")
    column = f"{spec.SWEEP_TARGET}_{spec.SWEEP_MODEL}"
    return frame[KEYS + [column]].rename(columns={column: "phase_4a_p"})


def _verify_fold(fold, booster, decision, stored, names) -> dict:
    """Compare the refit against phase 4A on the one-second rows they share."""
    step = int(spec.TRAIN_GRID_MS * 1e6)
    parts = []
    for frame in decision.values():
        stamps = frame["timestamp_ns"].to_numpy()
        window = (
            (stamps >= fold.validation_start_ns)
            & (stamps < fold.validation_end_ns)
            & (stamps % step == 0)
        )
        if not window.any():
            continue
        block = frame.loc[window, KEYS].copy()
        block["refit_p"] = booster.predict(frame.loc[window, names].to_numpy(dtype="float32"))
        parts.append(block)
    merged = pd.concat(parts, ignore_index=True).merge(stored, on=KEYS, how="inner")
    difference = np.abs(merged["refit_p"].to_numpy() - merged["phase_4a_p"].to_numpy())
    return {
        "fold": fold.index,
        "compared_rows": int(len(merged)),
        "max_abs_difference": float(difference.max()) if len(merged) else np.nan,
        "mean_abs_difference": float(difference.mean()) if len(merged) else np.nan,
        "exact_matches": int((difference == 0).sum()),
    }


def load_scores() -> pd.DataFrame:
    """Every out-of-fold score. Files that lie entirely inside the initial training blocks have
    no validation window and therefore no scores, which is why they are simply absent."""
    parts = [
        pd.read_parquet(scores_path(entry.file_index))
        for entry in corpus.CORPUS
        if scores_path(entry.file_index).exists()
    ]
    return pd.concat(parts, ignore_index=True)
