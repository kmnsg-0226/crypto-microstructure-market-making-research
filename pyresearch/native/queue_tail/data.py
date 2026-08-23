"""Assemble the queue-lifecycle modelling frame and its targets.

Three sources are joined on an exact row key: the new level-lifecycle replay, the phase 1/2
side-normalised book and flow features, and the phase 3 queue-assumption fills. Nothing is
re-simulated and no feature is recomputed in two places.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow.csv as pa_csv
import zstandard as zstd

from pyresearch.native.economic import data as economic_data
from pyresearch.native.predictive import data as predictive_data
from pyresearch.native.queue_tail import spec
from pyresearch.native.core import corpus

KEYS = ["timestamp_ns", "file_index", "segment_id", "side"]


def _read(path) -> pd.DataFrame:
    with path.open("rb") as handle:
        with zstd.ZstdDecompressor().stream_reader(handle) as stream:
            return pa_csv.read_csv(stream).to_pandas()


def episodes_path(file_index: int):
    return spec.DATA_DIR / f"level_episodes_file{file_index}.csv.zst"


def grid_path(file_index: int):
    return spec.DATA_DIR / f"level_grid_file{file_index}.csv.zst"


def birth_fills_path(file_index: int):
    return spec.DATA_DIR / f"birth_fills_file{file_index}.csv.zst"


def birth_mid_path(file_index: int):
    return spec.DATA_DIR / f"birth_mid_file{file_index}.csv.zst"


def load_episodes() -> pd.DataFrame:
    frame = pd.concat([_read(episodes_path(i)) for i in (0, 1, 2)], ignore_index=True)
    frame["log_duration_ms"] = np.log1p(frame["duration_ms"].clip(lower=0))
    removed = frame["cum_remove"].to_numpy(dtype="float64")
    frame["unexplained_removal_share"] = np.where(
        removed > 0, frame["cum_unexplained_remove"] / np.where(removed > 0, removed, np.nan), np.nan
    )
    initial = frame["initial_qty"].to_numpy(dtype="float64")
    frame["consumption_ratio"] = np.where(
        initial > 0, frame["cum_trade_at_quote"] / np.where(initial > 0, initial, np.nan), np.nan
    )
    frame["replenishment_ratio"] = np.where(
        removed > 0, frame["cum_replenish"] / np.where(removed > 0, removed, np.nan), np.nan
    )
    frame["fully_removed"] = frame["final_qty"] <= 0
    return frame


def load_grid(file_index: int) -> pd.DataFrame:
    return _read(grid_path(file_index))


def _lifecycle_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Causal level-lifecycle state, all of it known at the row's own timestamp."""
    out = pd.DataFrame(index=frame.index)
    initial = frame["initial_qty"].to_numpy(dtype="float64")
    current = frame["current_qty"].to_numpy(dtype="float64")
    maximum = frame["max_qty"].to_numpy(dtype="float64")
    removed = frame["cum_remove"].to_numpy(dtype="float64")
    safe_initial = np.where(initial > 0, initial, np.nan)
    safe_max = np.where(maximum > 0, maximum, np.nan)
    safe_removed = np.where(removed > 0, removed, np.nan)

    out["log_level_age_ms"] = np.log1p(frame["level_age_ms"].to_numpy(dtype="float64"))
    for name, values in (
        ("log_current_qty", current),
        ("log_initial_qty", initial),
        ("log_max_qty", maximum),
        ("log_cum_add", frame["cum_add"].to_numpy(dtype="float64")),
        ("log_cum_remove", removed),
        ("log_cum_trade_at_quote", frame["cum_trade_at_quote"].to_numpy(dtype="float64")),
        ("log_cum_trade_through", frame["cum_trade_through"].to_numpy(dtype="float64")),
        ("log_cum_replenish", frame["cum_replenish"].to_numpy(dtype="float64")),
        ("log_add_events", frame["add_events"].to_numpy(dtype="float64")),
        ("log_remove_events", frame["remove_events"].to_numpy(dtype="float64")),
        ("log_replenish_events", frame["replenish_events"].to_numpy(dtype="float64")),
        ("log_prints_at_quote", frame["prints_at_quote"].to_numpy(dtype="float64")),
        ("log_prints_through", frame["prints_through"].to_numpy(dtype="float64")),
    ):
        out[name] = np.log1p(np.clip(values, 0, None))
    out["relative_remaining_qty"] = current / safe_initial
    out["relative_to_max_qty"] = current / safe_max
    out["drawdown_from_max"] = (maximum - current) / safe_max
    out["consumption_ratio"] = frame["cum_trade_at_quote"].to_numpy() / safe_initial
    out["removal_ratio"] = removed / safe_initial
    out["addition_ratio"] = frame["cum_add"].to_numpy() / safe_initial
    out["replenishment_ratio"] = frame["cum_replenish"].to_numpy() / safe_removed
    out["unexplained_removal_share"] = (
        frame["cum_unexplained_remove"].to_numpy() / safe_removed
    )
    for column in (
        "time_since_qty_change_ms",
        "time_since_add_ms",
        "time_since_remove_ms",
        "time_since_print_ms",
        "time_since_replenish_ms",
    ):
        out[f"log_{column}"] = np.log1p(frame[column].to_numpy(dtype="float64"))
    out["has_replenished"] = (frame["replenish_events"].to_numpy() > 0).astype("float32")
    return out.astype("float32")


def build_model_frame(
    step_ms: int = spec.MODEL_GRID_MS, files: tuple[int, ...] | None = None
) -> pd.DataFrame:
    """One row per (placement instant, side) with features and every target.

    ``step_ms`` defaults to the one-second modelling decimation used to fit every model in this
    phase. Passing the raw lifecycle cadence instead produces the same rows at 100 ms, which is
    what a later phase needs in order to *score* an already-fitted model while an order rests;
    it never changes what any model is trained on.
    """
    episodes = load_episodes()
    keyed = episodes.set_index(["file_index", "level_episode_id"])
    ends = keyed["end_ns"]
    # An episode that ended only because the segment or the synchronization did is not an
    # observation of level failure; those rows are censored rather than labelled as events.
    genuine_end = keyed["close_reason"].isin(["improved", "stepped_away", "book_side_empty"])
    segments = economic_data.segment_bounds().set_index(["file_index", "segment_id"])["end_ns"]
    step = int(step_ms * 1e6)

    parts = []
    for entry in corpus.CORPUS:
        if files is not None and entry.file_index not in files:
            continue
        grid = load_grid(entry.file_index)
        grid = grid[grid["timestamp_ns"] % step == 0].reset_index(drop=True)
        block = pd.DataFrame(
            {
                "timestamp_ns": grid["timestamp_ns"],
                "file_index": grid["file_index"],
                "segment_id": grid["segment_id"],
                "side": grid["side"],
                "level_episode_id": grid["level_episode_id"],
                "quote_price_ticks": grid["quote_price_ticks"],
            }
        )
        block = pd.concat([block, _lifecycle_features(grid)], axis=1)

        episode_key = pd.MultiIndex.from_arrays(
            [grid["file_index"], grid["level_episode_id"]]
        )
        block["episode_end_ns"] = episode_key.map(ends).to_numpy()
        block["episode_ends_genuinely"] = episode_key.map(genuine_end).to_numpy()
        block["episode_close_reason"] = episode_key.map(keyed["close_reason"]).to_numpy()
        segment_key = pd.MultiIndex.from_arrays([grid["file_index"], grid["segment_id"]])
        block["segment_end_ns"] = segment_key.map(segments).to_numpy()
        # A level episode always ends at or before its segment does, so a survival horizon that
        # fits inside the segment is fully observed.
        remaining = block["episode_end_ns"].to_numpy() - grid["timestamp_ns"].to_numpy()
        segment_remaining = (
            block["segment_end_ns"].to_numpy() - grid["timestamp_ns"].to_numpy()
        )
        genuine = block["episode_ends_genuinely"].to_numpy(dtype=bool)
        for horizon in spec.SURVIVAL_HORIZONS_MS:
            width = int(horizon * 1e6)
            failed = genuine & (remaining <= width)
            # A zero is only recorded when the level was actually watched for the full horizon
            # inside the segment.
            survived = segment_remaining >= width
            block[f"level_disappears_{horizon}ms"] = np.where(
                failed, 1.0, np.where(survived & ~(remaining <= width), 0.0, np.nan)
            ).astype("float32")
        block["time_to_level_end_ms"] = np.where(
            genuine, remaining / 1e6, np.nan
        ).astype("float32")

        for horizon in spec.SWEEP_HORIZONS_MS:
            block[f"trade_through_within_{horizon}ms"] = pd.to_numeric(
                grid[f"trade_through_within_{horizon}ms"], errors="coerce"
            ).astype("float32")
            block[f"trade_at_quote_volume_{horizon}ms"] = pd.to_numeric(
                grid[f"trade_at_quote_volume_{horizon}ms"], errors="coerce"
            ).astype("float32")
        del grid
        parts.append(block)
    frame = pd.concat(parts, ignore_index=True)

    # Side-normalised book and flow features from the phase 2 model frame, joined on the exact
    # placement instant.
    static = predictive_data.load_model_frame()
    if files is not None:
        static = static[static["file_index"].isin(files)].reset_index(drop=True)
    views = []
    for code, side in enumerate(("bid", "ask")):
        view = predictive_data.side_view(static, side)
        view = view[
            [c for c in spec.STATIC_BOOK + spec.RECENT_FLOW if c in view.columns]
        ].copy()
        view["timestamp_ns"] = static["timestamp_ns"].to_numpy()
        view["file_index"] = static["file_index"].to_numpy()
        view["segment_id"] = static["segment_id"].to_numpy()
        view["side"] = np.int8(code)
        views.append(view)
    del static
    context = pd.concat(views, ignore_index=True)
    frame = frame.merge(context, on=KEYS, how="left")
    return frame.sort_values(["timestamp_ns", "side"], ignore_index=True)


def frame_path():
    return spec.DATA_DIR / "queue_tail_model_frame.parquet"


def build_and_save() -> None:
    spec.DATA_DIR.mkdir(parents=True, exist_ok=True)
    frame = build_model_frame()
    frame.to_parquet(frame_path(), index=False, compression="zstd")
    print(f"model frame: {len(frame):,} rows, {frame.shape[1]} columns")


def load_model_frame(columns: list[str] | None = None) -> pd.DataFrame:
    if columns is not None:
        columns = list(dict.fromkeys(KEYS + list(columns)))
    return pd.read_parquet(frame_path(), columns=columns)


# --------------------------------------------------------------------------------------------
# Catastrophic-fill targets from the phase 3 queue replay
# --------------------------------------------------------------------------------------------
def load_fills(level_birth: bool = False) -> pd.DataFrame:
    """Phase 3 fill outcomes with markouts attached, for the three headline queue cells."""
    if level_birth:
        path = economic_data.MidPath(
            pd.concat([_read(birth_mid_path(i)) for i in (0, 1, 2)], ignore_index=True)
            .sort_values(["file_index", "segment_id", "ns"], ignore_index=True)
        )
        loader = lambda index: _read(birth_fills_path(index))  # noqa: E731
    else:
        path = economic_data.MidPath(economic_data.load_mid_path())
        loader = economic_data.load_fills
    bounds = economic_data.segment_bounds()
    wanted = pd.MultiIndex.from_tuples(tuple(spec.QUEUE_CELLS.values()))
    frames = []
    for entry in corpus.CORPUS:
        block = loader(entry.file_index)
        cells = pd.MultiIndex.from_arrays([block["alpha_pct"], block["beta_pct"]])
        block = block[cells.isin(wanted)].reset_index(drop=True)
        frames.append(economic_data.add_markouts(block, path, bounds))
    frame = pd.concat(frames, ignore_index=True)
    return add_catastrophic_targets(frame)


def add_catastrophic_targets(frame: pd.DataFrame) -> pd.DataFrame:
    """Fixed downside thresholds on the signed one-second post-fill markout."""
    markout = frame[f"markout_{spec.PRIMARY_MARKOUT_MS}ms_ticks"].to_numpy(dtype="float64")
    observed = np.isfinite(markout)
    for threshold in spec.CATASTROPHIC_THRESHOLDS_TICKS:
        frame[f"catastrophic_{threshold}"] = np.where(
            observed, (markout <= -threshold).astype("float64"), np.nan
        ).astype("float32")
    frame["downside_ticks_1s"] = np.where(observed, np.minimum(markout, 0.0), np.nan)
    frame["severity_ticks"] = np.where(
        observed & (markout <= -spec.SEVERITY_THRESHOLD_TICKS), -markout, np.nan
    )
    cell_name = {value: name for name, value in spec.QUEUE_CELLS.items()}
    frame["queue_cell"] = [
        cell_name.get((alpha, beta), "other")
        for alpha, beta in zip(frame["alpha_pct"], frame["beta_pct"], strict=True)
    ]
    return frame


def join_placement_features(fills: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    """Attach placement-time features to fills on the exact row key.

    Only state observable at the placement instant is attached. Nothing from the fill instant,
    the post-fill path or the later level evolution is joined here, which is what keeps the
    catastrophic model causal.
    """
    keyed = fills.rename(columns={"placement_ns": "timestamp_ns"})
    # The fill table and the lifecycle frame both carry a few placement-instant observables.
    # The lifecycle frame is the canonical source for features, so the duplicates are dropped
    # from the fill side rather than being silently suffixed by the merge.
    duplicated = [c for c in keyed.columns if c not in KEYS and c in frame.columns]
    return keyed.drop(columns=duplicated).merge(frame, on=KEYS, how="inner")
