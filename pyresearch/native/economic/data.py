"""Load the queue-sensitivity replay and turn fill times into signed markouts.

The C++ replay emits only *when* each hypothetical order filled, under each of the 25 queue
assumptions, plus the mid path at event resolution. Markouts are reconstructed here, which keeps
every horizon re-derivable without another replay and makes the sign convention auditable in one
place.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow.csv as pa_csv
import zstandard as zstd

from pyresearch.native.economic import spec
from pyresearch.native.core import corpus

MECHANISM = {0: "unfilled", 1: "at_quote", 2: "trade_through"}


def _read(path) -> pd.DataFrame:
    with path.open("rb") as handle:
        with zstd.ZstdDecompressor().stream_reader(handle) as stream:
            return pa_csv.read_csv(stream).to_pandas()


def fills_path(file_index: int):
    return spec.DATA_DIR / f"queue_fills_file{file_index}.csv.zst"


def mid_path(file_index: int):
    return spec.DATA_DIR / f"mid_path_file{file_index}.csv.zst"


def load_mid_path() -> pd.DataFrame:
    frames = [_read(mid_path(index)) for index in (0, 1, 2)]
    frame = pd.concat(frames, ignore_index=True)
    return frame.sort_values(["file_index", "segment_id", "ns"], ignore_index=True)


def segment_bounds() -> pd.DataFrame:
    segments = pd.read_csv(corpus.REPORT_DIR / "segments.csv")
    return segments[["file_index", "segment_id", "start_ns", "end_ns"]]


class MidPath:
    """Step function of the mid over time, one segment at a time.

    ``mid_at`` returns the mid implied by every depth event with a receive timestamp at or before
    the query instant, which is exactly the definition phases 1 and 2 used.
    """

    def __init__(self, frame: pd.DataFrame) -> None:
        self._keys: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}
        for key, block in frame.groupby(["file_index", "segment_id"], sort=True):
            self._keys[key] = (
                block["ns"].to_numpy(dtype="int64"),
                block["mid_x2"].to_numpy(dtype="int64"),
            )

    def mid_at(
        self, file_index: np.ndarray, segment_id: np.ndarray, when_ns: np.ndarray
    ) -> np.ndarray:
        """Vectorised lookup; NaN where the instant precedes the first observed mid."""
        out = np.full(len(when_ns), np.nan)
        frame = pd.DataFrame({"f": file_index, "s": segment_id, "t": when_ns})
        for key, block in frame.groupby(["f", "s"], sort=False):
            series = self._keys.get(key)
            if series is None:
                continue
            times, values = series
            index = np.searchsorted(times, block["t"].to_numpy(), side="right") - 1
            valid = index >= 0
            resolved = np.full(len(block), np.nan)
            resolved[valid] = values[index[valid]] / 2.0
            out[block.index.to_numpy()] = resolved
        return out


def load_fills(file_index: int) -> pd.DataFrame:
    return _read(fills_path(file_index))


def add_markouts(frame: pd.DataFrame, path: MidPath, bounds: pd.DataFrame) -> pd.DataFrame:
    """Attach signed quote-relative markouts and the censoring mask for every horizon."""
    ends = bounds.set_index(["file_index", "segment_id"])["end_ns"]
    keys = pd.MultiIndex.from_arrays([frame["file_index"], frame["segment_id"]])
    frame["segment_end_ns"] = keys.map(ends).to_numpy()

    filled = frame["fill_ns"].to_numpy() > 0
    frame["filled"] = filled
    frame["mechanism_name"] = frame["mechanism"].map(MECHANISM)
    frame["time_to_fill_ms"] = np.where(
        filled, (frame["fill_ns"].to_numpy() - frame["placement_ns"].to_numpy()) / 1e6, np.nan
    )
    frame["quote_px_usd"] = frame["quote_px_ticks"] * spec.TICK_SIZE_USD
    frame["mid_at_placement_ticks"] = frame["mid_x2_at_placement"] / 2.0

    file_index = frame["file_index"].to_numpy()
    segment_id = frame["segment_id"].to_numpy()
    fill_ns = frame["fill_ns"].to_numpy()
    quote = frame["quote_px_ticks"].to_numpy(dtype="float64")
    # Bid: mid above the quote is favourable. Ask: mid below the quote is favourable.
    sign = np.where(frame["side"].to_numpy() == 0, 1.0, -1.0)
    end_ns = frame["segment_end_ns"].to_numpy()

    for horizon in spec.MARKOUT_HORIZONS_MS:
        when = fill_ns + int(horizon * 1e6)
        # A markout is only observed when the fill happened and the horizon after it still fits
        # inside the segment; otherwise it is censored, never zero.
        observable = filled & (when <= end_ns)
        mid = np.full(len(frame), np.nan)
        if observable.any():
            mid[observable] = path.mid_at(
                file_index[observable], segment_id[observable], when[observable]
            )
        markout = sign * (mid - quote)
        frame[f"markout_{horizon}ms_ticks"] = np.where(observable, markout, np.nan)

    for horizon in spec.FILL_HORIZONS_MS:
        evaluable = frame["placement_ns"].to_numpy() + int(horizon * 1e6) <= end_ns
        hit = filled & (frame["time_to_fill_ms"].to_numpy() <= horizon)
        frame[f"fill_{horizon}ms"] = np.where(evaluable, hit.astype("float64"), np.nan)
    return frame


def load_all(headline_only: bool = False) -> pd.DataFrame:
    """Every placement under every queue assumption, with markouts attached."""
    path = MidPath(load_mid_path())
    bounds = segment_bounds()
    frames = []
    for entry in corpus.CORPUS:
        block = load_fills(entry.file_index)
        if headline_only:
            wanted = pd.MultiIndex.from_tuples(spec.HEADLINE_CELLS)
            cells = pd.MultiIndex.from_arrays([block["alpha_pct"], block["beta_pct"]])
            block = block[cells.isin(wanted)].reset_index(drop=True)
        frames.append(add_markouts(block, path, bounds))
    return pd.concat(frames, ignore_index=True)


def cell(frame: pd.DataFrame, alpha: int, beta: int) -> pd.DataFrame:
    return frame[(frame["alpha_pct"] == alpha) & (frame["beta_pct"] == beta)]


def load_oof_predictions() -> pd.DataFrame:
    """The frozen phase 2 blocked out-of-fold predictions, joined by exact row key.

    Nothing is refitted here and no in-sample prediction is ever substituted: rows outside the
    phase 2 validation blocks simply have no prediction and drop out of the diagnostics that
    need one.
    """
    from pyresearch.native.predictive import spec as predictive_spec

    columns = [
        "timestamp_ns",
        "file_index",
        "segment_id",
        "side",
        "fill_5000ms_lightgbm",
        "fill_1000ms_lightgbm",
        "markout_1000ms_lightgbm",
        "through_given_fill_lightgbm",
    ]
    frame = pd.read_csv(
        predictive_spec.DATA_DIR / "oof_side_predictions.csv.zst", usecols=columns
    )
    return frame.rename(columns={"timestamp_ns": "placement_ns"})
