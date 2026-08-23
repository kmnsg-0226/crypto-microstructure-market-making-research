"""Frame construction for phase 6.

One frame, one row per 100 ms decision instant, carrying: the frozen phase 1 grid mid, the
regenerated long-horizon markouts, every frozen signal, the chronological block identifiers the
dependence-aware machinery needs, and the deterministic non-overlap anchor flags.

Nothing here fits anything. Every signal column is a fixed transform of an already-published
artifact, and every target column is a forward shift of the frozen phase 1 mid on the grid that
same artifact was written from.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from pyresearch.native.decay import spec

FILES = (0, 1, 2)
GRID_PATH = spec.ROOT / "data/research/native_dev_v1/native_features_100ms_file{i}.csv.zst"
P2_OOF_PATH = spec.ROOT / "data/research/native_predictive_v1/oof_price_predictions.csv.zst"
SWEEP_PATH = (
    spec.ROOT / "data/research/native_cancel_falsification_v1/sweep_scores_file2.parquet"
)
P5A_FRAME_PATH = (
    spec.ROOT / "data/research/native_directional_sweep_v1/directional_frame.parquet"
)
FOLDS_PATH = spec.ROOT / "research/native_predictive_v1/folds.csv"

KEY = ["file_index", "segment_id", "timestamp_ns"]
GRID_COLUMNS = [
    "timestamp_ns",
    "file_index",
    "segment_id",
    "mid_ticks",
    "spread_ticks",
    "obi_l1",
    "obi_l5",
    "obi_l10",
    "markout_1000ms_ticks",
    "markout_5000ms_ticks",
]
GRID_STEP_NS = 100_000_000


def sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------
# Grid and targets
# ---------------------------------------------------------------------------------------------
def load_grid(file_index: int) -> pd.DataFrame:
    frame = pd.read_csv(str(GRID_PATH).format(i=file_index), usecols=GRID_COLUMNS)
    return frame[GRID_COLUMNS].sort_values(KEY, kind="mergesort").reset_index(drop=True)


def assert_contiguous(frame: pd.DataFrame) -> None:
    """The forward shift is only a valid clock if the grid has no holes inside a segment."""
    for _, part in frame.groupby(["file_index", "segment_id"], sort=False):
        steps = np.diff(part["timestamp_ns"].to_numpy())
        if steps.size and not np.all(steps == GRID_STEP_NS):
            raise AssertionError("grid is not contiguous at 100 ms inside a segment")


def add_targets(frame: pd.DataFrame, horizons_s=spec.HORIZONS_S) -> pd.DataFrame:
    """markout_{h}s_ticks = mid(t + h) - mid(t), censored at the segment edge.

    A forward shift of exactly h / 100 ms rows inside the segment. Because the grid is contiguous
    and absolutely aligned, that row *is* the instant t + h; rows without one are past the
    segment edge and stay NaN rather than becoming zero.
    """
    assert_contiguous(frame)
    grouped = frame.groupby(["file_index", "segment_id"], sort=False)["mid_ticks"]
    mid = frame["mid_ticks"].to_numpy(dtype="float64")
    for horizon in horizons_s:
        steps = int(round(horizon * 1000 * 1_000_000 / GRID_STEP_NS))
        forward = grouped.shift(-steps).to_numpy(dtype="float64")
        frame[f"mid_fwd_{horizon}s_ticks"] = forward
        frame[f"markout_{horizon}s_ticks"] = forward - mid
    return frame


def add_anchors(frame: pd.DataFrame, horizons_s=spec.HORIZONS_S) -> pd.DataFrame:
    """Deterministic non-overlapping anchors: segment-relative row index divisible by h/100 ms.

    Defined on the raw grid before any filtering, so the anchor set never depends on which rows a
    later population or purge rule happens to keep.
    """
    position = frame.groupby(["file_index", "segment_id"], sort=False).cumcount().to_numpy()
    for horizon in horizons_s:
        steps = int(round(horizon * 1000 * 1_000_000 / GRID_STEP_NS))
        frame[f"anchor_{horizon}s"] = (position % steps) == 0
    return frame


# ---------------------------------------------------------------------------------------------
# Signals, all frozen
# ---------------------------------------------------------------------------------------------
def load_direction_scores() -> pd.DataFrame:
    frame = pd.read_csv(
        P2_OOF_PATH,
        usecols=[
            "timestamp_ns",
            "file_index",
            "segment_id",
            "price_direction_linear",
            "price_direction_lightgbm",
        ],
    )
    # 2p - 1 centres a probability on its own neutral point. A fixed transform, not a fit.
    frame["direction_p2_logistic"] = 2.0 * frame["price_direction_linear"] - 1.0
    frame["direction_p2_lightgbm"] = 2.0 * frame["price_direction_lightgbm"] - 1.0
    return frame[KEY + ["direction_p2_logistic", "direction_p2_lightgbm"]]


def load_sweep_scores() -> pd.DataFrame:
    """p(ask threatened) - p(bid threatened), the phase 5A convention written per instant."""
    frame = pd.read_parquet(
        SWEEP_PATH, columns=["timestamp_ns", "file_index", "segment_id", "side", "sweep_p"]
    )
    wide = frame.pivot_table(index=KEY, columns="side", values="sweep_p", aggfunc="first")
    wide = wide.rename(columns={0: "sweep_p_bid", 1: "sweep_p_ask"}).reset_index()
    wide["sweep_dir_p4a"] = wide["sweep_p_ask"] - wide["sweep_p_bid"]
    return wide[KEY + ["sweep_p_bid", "sweep_p_ask", "sweep_dir_p4a"]]


# ---------------------------------------------------------------------------------------------
# Blocks and folds
# ---------------------------------------------------------------------------------------------
def load_folds() -> pd.DataFrame:
    return pd.read_csv(FOLDS_PATH)


def add_fold_and_blocks(frame: pd.DataFrame, folds: pd.DataFrame) -> pd.DataFrame:
    """Attach the phase 2 validation fold and the wall-clock block identifiers."""
    stamps = frame["timestamp_ns"].to_numpy()
    fold = np.full(stamps.shape, -1, dtype="int64")
    block_start = np.full(stamps.shape, -1, dtype="int64")
    for row in folds.itertuples():
        inside = (stamps >= row.validation_start_ns) & (stamps < row.validation_end_ns)
        fold[inside] = row.fold
        block_start[inside] = row.validation_start_ns
    frame["fold"] = fold
    frame["validation_block_start_ns"] = block_start
    frame["seconds_into_validation_block"] = np.where(
        block_start >= 0, (stamps - block_start) / 1e9, np.nan
    )
    origin = int(stamps.min())
    frame["corpus_seconds"] = (stamps - origin) / 1e9
    frame["demean_block"] = np.floor(
        frame["corpus_seconds"] / (spec.DEMEAN_BLOCK_MINUTES * 60.0)
    ).astype("int64")
    frame["utc_day"] = pd.to_datetime(stamps, unit="ns", utc=True).strftime("%Y-%m-%d")
    frame["segment_key"] = (
        frame["file_index"].astype(str) + ":" + frame["segment_id"].astype(str)
    )
    return frame


def bootstrap_block(frame: pd.DataFrame, horizon_s: float) -> np.ndarray:
    """Horizon-aware chronological block id: max(30 min, 12 h) of wall-clock time."""
    length = spec.bootstrap_block_seconds(horizon_s)
    return np.floor(frame["corpus_seconds"].to_numpy() / length).astype("int64")


# ---------------------------------------------------------------------------------------------
# Target agreement gate
# ---------------------------------------------------------------------------------------------
def check_frozen_agreement(frame: pd.DataFrame) -> dict:
    """Regenerated 1 s and 5 s must equal the frozen phase 1 columns exactly."""
    report = {}
    for horizon, column in spec.FROZEN_TARGET_COLUMNS.items():
        mine = frame[f"markout_{horizon}s_ticks"].to_numpy(dtype="float64")
        frozen = frame[column].to_numpy(dtype="float64")
        mine_missing = ~np.isfinite(mine)
        frozen_missing = ~np.isfinite(frozen)
        both = np.isfinite(mine) & np.isfinite(frozen)
        difference = np.abs(mine[both] - frozen[both])
        report[f"markout_{horizon}s"] = {
            "frozen_column": column,
            "rows_compared": int(both.sum()),
            "censoring_disagreements": int((mine_missing != frozen_missing).sum()),
            "rows_differing": int((difference > spec.AGREEMENT_MAX_ABS_TICKS).sum()),
            "max_abs_difference_ticks": float(difference.max()) if difference.size else 0.0,
        }
    return report


def check_phase5a_agreement(frame: pd.DataFrame) -> dict:
    """2 s is not frozen in phase 1; it is checked against the phase 5A reconstruction.

    Phase 5A rebuilt 2 s from the phase 3 mid path and published 32 disagreeing rows in 4.29 M
    against the frozen columns, always inside violent moves. That published tolerance is carried
    rather than re-litigated.
    """
    target = spec.PHASE5A_TARGET_COLUMNS[2]
    other = pd.read_parquet(
        P5A_FRAME_PATH, columns=["timestamp_ns", "file_index", "segment_id", "side", target]
    )
    # phase 5A rows are side normalised, but markout_2000ms_ticks is the raw mid displacement and
    # is therefore identical on the bid and ask row of one instant. One side is enough.
    other = other[other["side"] == 1].drop(columns=["side"])
    merged = frame[KEY + ["markout_2s_ticks"]].merge(other, on=KEY, how="inner")
    mine = merged["markout_2s_ticks"].to_numpy(dtype="float64")
    theirs = merged[target].to_numpy(dtype="float64")
    both = np.isfinite(mine) & np.isfinite(theirs)
    difference = np.abs(mine[both] - theirs[both])
    return {
        "markout_2s": {
            "phase5a_column": target,
            "rows_compared": int(both.sum()),
            "censoring_disagreements": int(
                (np.isfinite(mine) != np.isfinite(theirs)).sum()
            ),
            "rows_differing": int((difference > 0.0).sum()),
            "max_abs_difference_ticks": float(difference.max()) if difference.size else 0.0,
            "published_tolerance_rows": spec.PHASE5A_MAX_DISAGREEING_ROWS,
        }
    }


def agreement_passes(report: dict, phase5a: dict) -> tuple[bool, list[str]]:
    problems = []
    for name, record in report.items():
        if record["censoring_disagreements"] != 0:
            problems.append(f"{name}: {record['censoring_disagreements']} censoring disagreements")
        if record["rows_differing"] > spec.AGREEMENT_MAX_DISAGREEING_ROWS:
            problems.append(f"{name}: {record['rows_differing']} rows differ")
    record = phase5a["markout_2s"]
    if record["rows_differing"] > spec.PHASE5A_MAX_DISAGREEING_ROWS:
        problems.append(
            f"markout_2s: {record['rows_differing']} rows differ from the phase 5A "
            f"reconstruction, above its own published tolerance of "
            f"{spec.PHASE5A_MAX_DISAGREEING_ROWS}"
        )
    return (not problems), problems


# ---------------------------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------------------------
def build_frame() -> pd.DataFrame:
    parts = []
    for file_index in FILES:
        part = load_grid(file_index)
        part = add_targets(part)
        part = add_anchors(part)
        parts.append(part)
    frame = pd.concat(parts, ignore_index=True)
    frame = frame.merge(load_direction_scores(), on=KEY, how="left")
    frame = frame.merge(load_sweep_scores(), on=KEY, how="left")
    frame = add_fold_and_blocks(frame, load_folds())
    frame["has_all_signals"] = (
        frame[["direction_p2_logistic", "direction_p2_lightgbm", "sweep_dir_p4a"]]
        .notna()
        .all(axis=1)
    )
    return frame


def input_hashes() -> dict:
    inputs = {
        "native_dev_v1_spec": str(
            sha256(spec.ROOT / "research/specs/native_dev_v1.json")
        ),
        "folds": sha256(FOLDS_PATH),
        "phase2_oof_price_predictions": sha256(P2_OOF_PATH),
        "phase4b_sweep_scores": sha256(SWEEP_PATH),
    }
    for file_index in FILES:
        inputs[f"native_features_100ms_file{file_index}"] = sha256(
            str(GRID_PATH).format(i=file_index)
        )
    return inputs


def write_frame(frame: pd.DataFrame) -> None:
    spec.DATA_DIR.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(spec.DATA_DIR / "decay_frame.parquet", index=False)


def read_frame() -> pd.DataFrame:
    return pd.read_parquet(spec.DATA_DIR / "decay_frame.parquet")


def write_json(name: str, payload: dict) -> None:
    spec.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(spec.REPORT_DIR / name, "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
