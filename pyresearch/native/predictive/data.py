"""Model frames, targets and chronological folds for the native predictive decomposition.

The phase 1 dataset is 366 CSV columns per row. This module reduces it once to a compact
float32 parquet holding exactly the pre-registered features and targets, so the modelling stage
never re-parses the raw export and every run sees identical inputs.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from pyresearch.native.predictive import spec
from pyresearch.native.core import corpus, diagnostics

META_COLUMNS = [
    "timestamp_ns",
    "file_index",
    "segment_id",
    "segment_age_ms",
    "mid_ticks",
    "spread_ticks",
]
BOOK_SOURCE = [
    "microprice_minus_mid_ticks",
    "bid_concentration_l5",
    "ask_concentration_l5",
    "bid_concentration_l10",
    "ask_concentration_l10",
    "bid_dispersion_ticks_l10",
    "ask_dispersion_ticks_l10",
    "bid_depth_l1",
    "ask_depth_l1",
    "bid_depth_l5",
    "ask_depth_l5",
    "bid_depth_l10",
    "ask_depth_l10",
    "obi_l1",
    "obi_l5",
    "obi_l10",
    "weighted_obi_l5",
    "weighted_obi_l10",
    "time_since_trade_ms",
    "time_since_depth_ms",
    "time_since_bbo_change_ms",
    "time_since_mid_change_ms",
]
TARGET_SOURCE = (
    ["next_mid_move_dir", "time_to_next_mid_move_ms"]
    + [f"markout_{h}ms_ticks" for h in (100, 250, 500, 1000, 5000)]
    + [
        f"{side}_{name}"
        for side in ("bid", "ask")
        for name in (
            "quote_px_ticks",
            "queue_ahead_lots",
            "fill_500ms",
            "fill_1000ms",
            "fill_5000ms",
            "fill_before_observed_mid_adverse",
            "fill_via_trade_through",
            "time_to_fill_ms",
            "time_to_mid_adverse_ms",
            "time_to_quote_gone_ms",
            "time_to_best_adverse_ms",
            "postfill_markout_100ms_ticks",
            "postfill_markout_500ms_ticks",
            "postfill_markout_1000ms_ticks",
            "postfill_markout_5000ms_ticks",
        )
    ]
)


def _window_source() -> list[str]:
    names: list[str] = []
    for window in spec.WINDOWS_MS:
        names += [
            f"depth_flow_pressure_l5_{window}ms",
            f"depth_flow_pressure_l10_{window}ms",
            f"trade_imbalance_{window}ms",
            f"signed_volume_{window}ms",
            f"trade_count_{window}ms",
            f"depth_event_count_{window}ms",
            f"bbo_change_count_{window}ms",
            f"backward_mid_abs_change_ticks_{window}ms",
        ]
        # Per-level flow is only read to L5; deeper levels enter through the exponentially
        # weighted L10 pressure the exporter already computes.
        for level in range(1, 6):
            for side in ("bid", "ask"):
                names += [
                    f"{side}_depth_add_l{level}_{window}ms",
                    f"{side}_depth_remove_l{level}_{window}ms",
                ]
    return names


SOURCE_COLUMNS = META_COLUMNS + BOOK_SOURCE + _window_source() + TARGET_SOURCE


def signed_log(values: np.ndarray) -> np.ndarray:
    return np.sign(values) * np.log1p(np.abs(values))


def _segment_ends() -> pd.DataFrame:
    segments = pd.read_csv(corpus.REPORT_DIR / "segments.csv")
    return segments[["file_index", "segment_id", "start_ns", "end_ns"]]


def build_frame(file_index: int, segments: pd.DataFrame) -> pd.DataFrame:
    """Read one exported file and reduce it to the pre-registered feature/target frame."""
    entry = corpus.CORPUS[file_index]
    source = diagnostics.read_columns(entry.dataset_path, SOURCE_COLUMNS)
    # Columns are collected first and assembled once: a 144-column frame built by repeated
    # insertion is both slow and heavily fragmented at two million rows.
    columns: dict[str, np.ndarray] = {}

    for name in META_COLUMNS:
        columns[name] = source[name].to_numpy()
    ends = segments.set_index(["file_index", "segment_id"])["end_ns"]
    keys = pd.MultiIndex.from_arrays([source["file_index"], source["segment_id"]])
    columns["segment_end_ns"] = keys.map(ends).to_numpy()
    columns["remaining_ns"] = columns["segment_end_ns"] - columns["timestamp_ns"]

    for name in (
        "microprice_minus_mid_ticks",
        "bid_concentration_l5",
        "ask_concentration_l5",
        "bid_concentration_l10",
        "ask_concentration_l10",
        "bid_dispersion_ticks_l10",
        "ask_dispersion_ticks_l10",
        "obi_l1",
        "obi_l5",
        "obi_l10",
        "weighted_obi_l5",
        "weighted_obi_l10",
        "time_since_trade_ms",
        "time_since_depth_ms",
        "time_since_bbo_change_ms",
        "time_since_mid_change_ms",
    ):
        columns[name] = source[name].to_numpy(dtype="float32")
    for side in ("bid", "ask"):
        for level in ("l1", "l5", "l10"):
            columns[f"log_{side}_depth_{level}"] = np.log1p(
                source[f"{side}_depth_{level}"].to_numpy(dtype="float64")
            ).astype("float32")

    for window in spec.WINDOWS_MS:
        for name in (
            f"depth_flow_pressure_l5_{window}ms",
            f"depth_flow_pressure_l10_{window}ms",
            f"trade_imbalance_{window}ms",
            f"trade_count_{window}ms",
            f"depth_event_count_{window}ms",
            f"bbo_change_count_{window}ms",
            f"backward_mid_abs_change_ticks_{window}ms",
        ):
            columns[name] = source[name].to_numpy(dtype="float32")
        columns[f"signed_log_volume_{window}ms"] = signed_log(
            source[f"signed_volume_{window}ms"].to_numpy(dtype="float64")
        ).astype("float32")
        net = np.zeros(len(source), dtype="float64")
        for level in range(1, 6):
            net += source[f"bid_depth_add_l{level}_{window}ms"].to_numpy(dtype="float64")
            net -= source[f"bid_depth_remove_l{level}_{window}ms"].to_numpy(dtype="float64")
            net -= source[f"ask_depth_add_l{level}_{window}ms"].to_numpy(dtype="float64")
            net += source[f"ask_depth_remove_l{level}_{window}ms"].to_numpy(dtype="float64")
            if level == 1:
                columns[f"net_depth_flow_l1_{window}ms"] = signed_log(net).astype("float32")
        columns[f"net_depth_flow_l5_{window}ms"] = signed_log(net).astype("float32")
    for window in spec.LEVEL_FLOW_WINDOWS_MS:
        for side in ("bid", "ask"):
            for action in ("add", "remove"):
                columns[f"log_{side}_depth_{action}_l1_{window}ms"] = np.log1p(
                    source[f"{side}_depth_{action}_l1_{window}ms"].to_numpy(dtype="float64")
                ).astype("float32")

    for name in TARGET_SOURCE:
        columns[name] = pd.to_numeric(source[name], errors="coerce").to_numpy(dtype="float32")
    del source
    return add_targets(pd.DataFrame(columns))


def add_targets(frame: pd.DataFrame) -> pd.DataFrame:
    """Derive the pre-registered labels, preserving censoring rather than coding it as zero."""
    labels: dict[str, np.ndarray] = {}
    direction = frame["next_mid_move_dir"].to_numpy(dtype="float64")
    labels["y_direction"] = np.where(np.isnan(direction), np.nan, direction > 0).astype(
        "float32"
    )

    latency = frame["time_to_next_mid_move_ms"].to_numpy(dtype="float64")
    remaining_ms = frame["remaining_ns"].to_numpy(dtype="float64") / 1e6
    for horizon in spec.MOVE_HORIZONS_MS:
        moved = latency <= horizon
        # A zero is only recorded when the full horizon was actually observed inside the
        # segment; otherwise the row is censored out of the denominator.
        observed = remaining_ms >= horizon
        labels[f"y_move_{horizon}ms"] = np.where(
            moved, 1.0, np.where(observed, 0.0, np.nan)
        ).astype("float32")

    for side in ("bid", "ask"):
        fill_latency = frame[f"{side}_time_to_fill_ms"].to_numpy(dtype="float64")
        filled = ~np.isnan(fill_latency)
        through = frame[f"{side}_fill_via_trade_through"].to_numpy(dtype="float64")
        labels[f"y_{side}_through_given_fill"] = np.where(filled, through, np.nan).astype(
            "float32"
        )
        labels[f"y_{side}_at_quote_given_fill"] = np.where(
            filled, 1.0 - through, np.nan
        ).astype("float32")
        for horizon in spec.FILL_HORIZONS_MS:
            labels[f"y_{side}_fill_{horizon}ms"] = frame[
                f"{side}_fill_{horizon}ms"
            ].to_numpy(dtype="float32")
        markout = frame[
            f"{side}_postfill_markout_{spec.PRIMARY_MARKOUT_MS}ms_ticks"
        ].to_numpy(dtype="float64")
        labels[f"y_{side}_markout_{spec.PRIMARY_MARKOUT_MS}ms"] = markout.astype("float32")
        labels[f"y_{side}_good_fill"] = np.where(
            np.isnan(markout), np.nan, markout > spec.GOOD_FILL_THRESHOLD_TICKS
        ).astype("float32")
    return pd.concat([frame, pd.DataFrame(labels, index=frame.index)], axis=1)


def frame_path(file_index: int):
    return spec.DATA_DIR / f"model_frame_file{file_index}.parquet"


def build_model_frames() -> None:
    spec.DATA_DIR.mkdir(parents=True, exist_ok=True)
    segments = _segment_ends()
    for entry in corpus.CORPUS:
        frame = build_frame(entry.file_index, segments)
        frame.to_parquet(frame_path(entry.file_index), index=False, compression="zstd")
        print(f"model frame file{entry.file_index}: {len(frame):,} rows, {frame.shape[1]} columns")
        del frame


def load_model_frame(columns: list[str] | None = None) -> pd.DataFrame:
    if columns is not None and "timestamp_ns" not in columns:
        # The frame is always returned in chronological order, which the fold machinery relies
        # on, so the ordering key is never optional.
        columns = ["timestamp_ns"] + list(columns)
    parts = [
        pd.read_parquet(frame_path(entry.file_index), columns=columns)
        for entry in corpus.CORPUS
    ]
    frame = pd.concat(parts, ignore_index=True)
    return frame.sort_values("timestamp_ns", ignore_index=True)


# --------------------------------------------------------------------------------------------
# Chronological folds
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Fold:
    index: int
    block: int
    train_end_ns: int
    validation_start_ns: int
    validation_end_ns: int


def block_edges(timestamps: np.ndarray) -> np.ndarray:
    start = int(timestamps.min())
    end = int(timestamps.max()) + 1
    return np.linspace(start, end, spec.N_BLOCKS + 1).astype("int64")


def assign_blocks(timestamps: np.ndarray) -> np.ndarray:
    edges = block_edges(timestamps)
    return np.clip(np.searchsorted(edges, timestamps, side="right") - 1, 0, spec.N_BLOCKS - 1)


def build_folds(timestamps: np.ndarray) -> list[Fold]:
    edges = block_edges(timestamps)
    purge_ns = int(spec.PURGE_SECONDS * 1e9)
    folds = []
    for index, block in enumerate(range(spec.FIRST_VALIDATION_BLOCK, spec.N_BLOCKS)):
        validation_start = int(edges[block])
        folds.append(
            Fold(
                index=index,
                block=block,
                # Every training row's longest forward target ends before the validation block
                # opens, because the purge exceeds the maximum target horizon.
                train_end_ns=validation_start - purge_ns,
                validation_start_ns=validation_start,
                validation_end_ns=int(edges[block + 1]),
            )
        )
    return folds


def fold_table(timestamps: np.ndarray, folds: list[Fold]) -> pd.DataFrame:
    rows = []
    for fold in folds:
        train = timestamps <= fold.train_end_ns
        validation = (timestamps >= fold.validation_start_ns) & (
            timestamps < fold.validation_end_ns
        )
        rows.append(
            {
                "fold": fold.index,
                "block": fold.block,
                "train_rows": int(train.sum()),
                "validation_rows": int(validation.sum()),
                "train_end_ns": fold.train_end_ns,
                "validation_start_ns": fold.validation_start_ns,
                "validation_end_ns": fold.validation_end_ns,
                "train_end_utc": _utc(fold.train_end_ns),
                "validation_start_utc": _utc(fold.validation_start_ns),
                "validation_end_utc": _utc(fold.validation_end_ns),
                "purge_seconds": spec.PURGE_SECONDS,
                "validation_hours": (fold.validation_end_ns - fold.validation_start_ns) / 3.6e12,
            }
        )
    return pd.DataFrame(rows)


def _utc(nanoseconds: int) -> str:
    return (
        pd.Timestamp(int(nanoseconds), unit="ns", tz="UTC")
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# --------------------------------------------------------------------------------------------
# Side-normalised view
# --------------------------------------------------------------------------------------------
def side_view(frame: pd.DataFrame, side: str) -> pd.DataFrame:
    """Re-express the absolute frame from the perspective of a resting quote on ``side``.

    Every signed quantity is oriented so that a positive value favours the resting order, which
    is what makes a single pooled model meaningful. Symmetry is asserted by comparison against
    the per-side models, never assumed.
    """
    if side not in ("bid", "ask"):
        raise ValueError(f"unknown side {side}")
    own, opp = ("bid", "ask") if side == "bid" else ("ask", "bid")
    sign = 1.0 if side == "bid" else -1.0
    out = pd.DataFrame(index=frame.index)
    out["spread_ticks"] = frame["spread_ticks"]
    for level in ("l5", "l10"):
        out[f"own_concentration_{level}"] = frame[f"{own}_concentration_{level}"]
        out[f"opp_concentration_{level}"] = frame[f"{opp}_concentration_{level}"]
    out["own_dispersion_ticks_l10"] = frame[f"{own}_dispersion_ticks_l10"]
    out["opp_dispersion_ticks_l10"] = frame[f"{opp}_dispersion_ticks_l10"]
    for level in ("l1", "l5", "l10"):
        out[f"log_own_depth_{level}"] = frame[f"log_{own}_depth_{level}"]
        out[f"log_opp_depth_{level}"] = frame[f"log_{opp}_depth_{level}"]
    for name in ("obi_l1", "obi_l5", "obi_l10", "weighted_obi_l5", "weighted_obi_l10"):
        out[f"signed_{name}"] = sign * frame[name]
    out["signed_microprice_offset_ticks"] = sign * frame["microprice_minus_mid_ticks"]
    queue = frame[f"{side}_queue_ahead_lots"].to_numpy(dtype="float64")
    out["log_queue_ahead_lots"] = np.log1p(queue).astype("float32")
    own_depth_l5 = np.expm1(frame[f"log_{own}_depth_l5"].to_numpy(dtype="float64"))
    out["queue_to_own_depth_l5"] = (
        queue / np.where(own_depth_l5 > 0, own_depth_l5, np.nan)
    ).astype("float32")

    for window in spec.WINDOWS_MS:
        for name in (
            f"net_depth_flow_l1_{window}ms",
            f"net_depth_flow_l5_{window}ms",
            f"depth_flow_pressure_l5_{window}ms",
            f"depth_flow_pressure_l10_{window}ms",
            f"trade_imbalance_{window}ms",
            f"signed_log_volume_{window}ms",
        ):
            out[f"signed_{name}" if not name.startswith("signed_") else name] = (
                sign * frame[name]
            )
        for name in (
            f"trade_count_{window}ms",
            f"depth_event_count_{window}ms",
            f"bbo_change_count_{window}ms",
            f"backward_mid_abs_change_ticks_{window}ms",
        ):
            out[name] = frame[name]
    for window in spec.LEVEL_FLOW_WINDOWS_MS:
        for action in ("add", "remove"):
            out[f"log_own_depth_{action}_l1_{window}ms"] = frame[
                f"log_{own}_depth_{action}_l1_{window}ms"
            ]
            out[f"log_opp_depth_{action}_l1_{window}ms"] = frame[
                f"log_{opp}_depth_{action}_l1_{window}ms"
            ]
    for name in spec.ACTIVITY_FEATURES:
        out[name] = frame[name]
    return out
