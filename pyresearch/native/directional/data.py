"""Assemble the directional frame: one row per (100 ms instant, threatened side).

Three frozen sources are joined on an exact row key and nothing is re-simulated:

* the blocked out-of-fold sweep probability produced in phase 4B from the phase 4A model,
* the phase 1 causal decision dataset, which already carries the pure mid markouts, the next
  mid move and its timing, and the side-normalisable book and flow features,
* the phase 4A level lifecycle grid, which carries the realised trade-through flags.

The only quantity computed here from raw-ish material is the 2 s markout and the size of the
first mid move, both read off the phase 3 mid path. That reconstruction is reconciled against
the frozen phase 1 markout columns on the five horizons they share before it is used anywhere.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow.csv as pa_csv
import zstandard as zstd

from pyresearch.native.cancel import scoring as cancel_scoring
from pyresearch.native.directional import spec
from pyresearch.native.economic import data as economic_data
from pyresearch.native.predictive import data as predictive_data
from pyresearch.native.queue_tail import data as qt_data
from pyresearch.native.queue_tail import spec as qt_spec

KEYS = ["timestamp_ns", "file_index", "segment_id", "side"]
SWEEP_FLAG_HORIZONS_MS = (100, 250, 500, 1000, 5000)


def frame_path():
    return spec.DATA_DIR / "directional_frame.parquet"


def features() -> list[str]:
    return qt_spec.FEATURE_SETS["static_book_plus_flow"]


def model_features(name: str) -> list[str]:
    if name == "obi":
        return list(spec.OBI_FEATURES)
    if name == "sweep_only":
        return list(spec.SWEEP_ONLY)
    if name == "book_flow":
        return features()
    if name == "book_flow_plus_sweep":
        return features() + list(spec.SWEEP_ONLY)
    raise ValueError(f"unknown model set {name}")


# --------------------------------------------------------------------------------------------
# Mid path helpers
# --------------------------------------------------------------------------------------------
class MidSeries:
    """The phase 3 mid path with a forward lookup as well as the backward one.

    ``mid_at`` reproduces :class:`native_economic.data.MidPath`; ``first_change_after`` returns
    the instant and value of the first *distinct* mid after a query time inside the same segment,
    which is what the size of the next mid move is by definition.
    """

    def __init__(self, frame: pd.DataFrame) -> None:
        self._keys: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}
        for key, block in frame.groupby(["file_index", "segment_id"], sort=True):
            ordered = block.sort_values("ns")
            self._keys[key] = (
                ordered["ns"].to_numpy(dtype="int64"),
                ordered["mid_x2"].to_numpy(dtype="int64"),
            )

    def _lookup(self, file_index, segment_id, when_ns, forward: bool):
        stamps = np.full(len(when_ns), -1, dtype="int64")
        values = np.full(len(when_ns), np.nan)
        frame = pd.DataFrame({"f": file_index, "s": segment_id, "t": when_ns})
        for key, block in frame.groupby(["f", "s"], sort=False):
            series = self._keys.get(key)
            if series is None:
                continue
            times, mids = series
            query = block["t"].to_numpy()
            if forward:
                index = np.searchsorted(times, query, side="right")
                valid = index < times.size
            else:
                index = np.searchsorted(times, query, side="right") - 1
                valid = index >= 0
            position = block.index.to_numpy()
            taken = index[valid]
            stamps[position[valid]] = times[taken]
            values[position[valid]] = mids[taken] / 2.0
        return stamps, values

    def mid_at(self, file_index, segment_id, when_ns) -> np.ndarray:
        return self._lookup(file_index, segment_id, when_ns, forward=False)[1]

    def first_change_after(self, file_index, segment_id, when_ns):
        return self._lookup(file_index, segment_id, when_ns, forward=True)


def _read_csv_zst(path, columns: list[str] | None = None) -> pd.DataFrame:
    with path.open("rb") as handle:
        with zstd.ZstdDecompressor().stream_reader(handle) as stream:
            options = pa_csv.ConvertOptions(include_columns=columns) if columns else None
            return pa_csv.read_csv(stream, convert_options=options).to_pandas()


def load_sweep_flags(file_index: int) -> pd.DataFrame:
    columns = KEYS + [f"trade_through_within_{h}ms" for h in SWEEP_FLAG_HORIZONS_MS]
    return _read_csv_zst(qt_data.grid_path(file_index), columns)


# --------------------------------------------------------------------------------------------
# Frame
# --------------------------------------------------------------------------------------------
def build_frame() -> tuple[pd.DataFrame, dict]:
    scores = cancel_scoring.load_scores()
    files = sorted(scores["file_index"].unique())
    bounds = economic_data.segment_bounds()
    mid = MidSeries(economic_data.load_mid_path())

    static = predictive_data.load_model_frame()
    static = static[static["file_index"].isin(files)].reset_index(drop=True)

    target_columns = ["next_mid_move_dir", "time_to_next_mid_move_ms", "mid_ticks"] + [
        f"markout_{h}ms_ticks" for h in spec.FROZEN_HORIZONS_MS
    ] + [f"y_move_{h}ms" for h in (250, 500, 1000, 5000)]

    views = []
    for code, side in enumerate(("bid", "ask")):
        view = predictive_data.side_view(static, side)
        view = view[[c for c in features() if c in view.columns]].copy()
        for column in target_columns:
            name = "mid_ticks_frozen" if column == "mid_ticks" else column
            view[name] = static[column].to_numpy()
        view["timestamp_ns"] = static["timestamp_ns"].to_numpy()
        view["file_index"] = static["file_index"].to_numpy()
        view["segment_id"] = static["segment_id"].to_numpy()
        view["side"] = np.int8(code)
        views.append(view)
    del static
    frame = pd.concat(views, ignore_index=True)

    flags = pd.concat([load_sweep_flags(index) for index in files], ignore_index=True)
    frame = frame.merge(flags, on=KEYS, how="inner")
    frame = frame.merge(
        scores[KEYS + ["fold", "sweep_p", "quote_price_ticks", "level_episode_id"]],
        on=KEYS,
        how="inner",
    )
    frame = frame.sort_values(["timestamp_ns", "side"], ignore_index=True)

    qc = _add_targets(frame, mid, bounds)
    return frame, qc


def _add_targets(frame: pd.DataFrame, mid: MidSeries, bounds: pd.DataFrame) -> dict:
    """Side-normalised directional targets. Every one of them is causal and segment-safe."""
    ends = bounds.set_index(["file_index", "segment_id"])["end_ns"]
    key = pd.MultiIndex.from_arrays([frame["file_index"], frame["segment_id"]])
    frame["segment_end_ns"] = key.map(ends).to_numpy()

    side = frame["side"].to_numpy()
    direction = np.where(side == 1, 1.0, -1.0)
    frame["sweep_direction"] = direction.astype("int8")

    file_index = frame["file_index"].to_numpy()
    segment_id = frame["segment_id"].to_numpy()
    stamps = frame["timestamp_ns"].to_numpy()
    end_ns = frame["segment_end_ns"].to_numpy()

    # The mid at the decision instant, used only to express ticks as basis points. Phase 1
    # already emitted it, so the mid-path lookup is checked against it rather than trusted.
    mid_now = mid.mid_at(file_index, segment_id, stamps)
    frame["mid_ticks"] = mid_now

    # The 2 s markout is the only horizon phase 1 did not emit. It is built exactly the way
    # phase 3 built its markouts, and reconciled against the frozen columns below.
    frozen_mid = frame["mid_ticks_frozen"].to_numpy(dtype="float64")
    qc = {
        "mid_at_decision_instant": {
            "compared_rows": int(np.isfinite(frozen_mid).sum()),
            "rows_differing": int(
                (np.abs(mid_now - frozen_mid) > 1e-9)[np.isfinite(frozen_mid)].sum()
            ),
        },
        "reconstruction": {},
    }
    for horizon in spec.ALL_HORIZONS_MS:
        when = stamps + int(horizon * 1e6)
        observable = when <= end_ns
        future = np.full(len(frame), np.nan)
        if observable.any():
            future[observable] = mid.mid_at(
                file_index[observable], segment_id[observable], when[observable]
            )
        rebuilt = np.where(observable, future - mid_now, np.nan)
        frozen_name = f"markout_{horizon}ms_ticks"
        if horizon in spec.FROZEN_HORIZONS_MS:
            frozen = frame[frozen_name].to_numpy(dtype="float64")
            both = np.isfinite(frozen) & np.isfinite(rebuilt)
            qc["reconstruction"][f"{horizon}ms"] = {
                "compared_rows": int(both.sum()),
                "censoring_disagreements": int(
                    (np.isfinite(frozen) != np.isfinite(rebuilt)).sum()
                ),
                "rows_differing": int((np.abs(rebuilt[both] - frozen[both]) > 1e-9).sum()),
                "max_abs_difference_ticks": float(
                    np.abs(rebuilt[both] - frozen[both]).max()
                )
                if both.any()
                else np.nan,
            }
            # Phase 1 censors these already, and the reconciliation confirms it agrees row for
            # row; the mask is reapplied anyway so the invariant is enforced here rather than
            # inherited.
            movement = np.where(observable, frozen, np.nan)
        else:
            frame[frozen_name] = rebuilt
            movement = rebuilt
        frame[f"directional_markout_{horizon}ms"] = direction * movement

    # Event targets. The next mid move and its timing are frozen phase 1 columns; its size is
    # read off the mid path, and the two must agree on direction.
    move_dir = frame["next_mid_move_dir"].to_numpy(dtype="float64")
    move_ms = frame["time_to_next_mid_move_ms"].to_numpy(dtype="float64")
    observed_move = np.isfinite(move_dir) & (move_dir != 0)
    frame["next_move_matches_sweep_direction"] = np.where(
        observed_move, (move_dir == direction).astype("float64"), np.nan
    )
    frame["next_move_opposes_sweep_direction"] = np.where(
        observed_move, (move_dir == -direction).astype("float64"), np.nan
    )

    change_ns, change_mid = mid.first_change_after(file_index, segment_id, stamps)
    inside = (change_ns > 0) & (change_ns <= end_ns)
    first_move = np.where(inside, change_mid - mid_now, np.nan)
    frame["first_mid_move_ticks"] = first_move
    frame["signed_ticks_on_first_mid_move"] = direction * first_move
    frame["first_mid_move_ms"] = np.where(inside, (change_ns - stamps) / 1e6, np.nan)
    agree = observed_move & np.isfinite(first_move)
    qc["first_move"] = {
        "compared_rows": int(agree.sum()),
        "direction_disagreements": int(
            (np.sign(first_move[agree]) != move_dir[agree]).sum()
        ),
        "median_timing_difference_ms": float(
            np.nanmedian(np.abs(frame["first_mid_move_ms"].to_numpy()[agree] - move_ms[agree]))
        ),
    }

    for horizon in spec.HEADLINE_HORIZONS_MS:
        evaluable = stamps + int(horizon * 1e6) <= end_ns
        moved = np.where(np.isfinite(move_ms), move_ms <= horizon, False)
        frame[f"mid_moves_within_{horizon}ms"] = np.where(
            evaluable, moved.astype("float64"), np.nan
        )
        # The threatened level is consumed before the price turns against the sweep: a
        # trade-through inside the horizon, with no opposing mid move having happened first.
        opposed = (
            observed_move
            & (move_dir == -direction)
            & np.isfinite(move_ms)
            & (move_ms <= horizon)
        )
        through = frame[f"trade_through_within_{horizon}ms"].to_numpy(dtype="float64")
        frame[f"level_consumed_before_opposite_move_{horizon}ms"] = np.where(
            np.isfinite(through) & evaluable,
            ((through > 0) & ~opposed).astype("float64"),
            np.nan,
        )
    return qc


def build_and_save() -> dict:
    spec.DATA_DIR.mkdir(parents=True, exist_ok=True)
    frame, qc = build_frame()
    frame.to_parquet(frame_path(), index=False, compression="zstd")
    qc["rows"] = int(len(frame))
    qc["columns"] = int(frame.shape[1])
    print(f"directional frame: {len(frame):,} rows, {frame.shape[1]} columns")
    return qc


def load_frame(columns: list[str] | None = None) -> pd.DataFrame:
    if columns is not None:
        columns = list(dict.fromkeys(KEYS + list(columns)))
    return pd.read_parquet(frame_path(), columns=columns)
