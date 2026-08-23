"""Information lead time and false-positive economics, per level episode.

A directional signal is only usable if it arrives before enough of the move. The natural unit
here is the level episode: the signal becomes observable for a given threatened level at the
first 100 ms instant inside that episode where the out-of-fold score crosses a fixed threshold,
and the two things worth timing against it are the first mid move in the implied direction and
the actual consumption of that level.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pyresearch.native.directional import data, events, spec
from pyresearch.native.economic import data as economic_data
from pyresearch.native.queue_tail import data as qt_data
from pyresearch.native.core import corpus

QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)


def _next_extreme(values: np.ndarray, greater: bool) -> np.ndarray:
    """Index of the next strictly greater (or smaller) value, -1 where there is none.

    A single monotonic pass, so the first same-direction mid move is available for every point
    of a segment's mid path at once rather than searched for per query.
    """
    size = values.size
    out = np.full(size, -1, dtype="int64")
    stack: list[int] = []
    for index in range(size):
        current = values[index]
        while stack and (
            values[stack[-1]] < current if greater else values[stack[-1]] > current
        ):
            out[stack.pop()] = index
        stack.append(index)
    return out


class FirstPassage:
    """First mid change beyond the mid prevailing at a query instant, per segment and side."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._keys: dict[tuple[int, int], dict] = {}
        for key, block in frame.groupby(["file_index", "segment_id"], sort=True):
            ordered = block.sort_values("ns")
            times = ordered["ns"].to_numpy(dtype="int64")
            mids = ordered["mid_x2"].to_numpy(dtype="int64")
            self._keys[key] = {
                "times": times,
                "up": _next_extreme(mids, greater=True),
                "down": _next_extreme(mids, greater=False),
            }

    def time_to_move(
        self, file_index: np.ndarray, segment_id: np.ndarray, when_ns: np.ndarray,
        direction: np.ndarray,
    ) -> np.ndarray:
        out = np.full(len(when_ns), np.nan)
        frame = pd.DataFrame(
            {"f": file_index, "s": segment_id, "t": when_ns, "d": direction}
        )
        for key, block in frame.groupby(["f", "s"], sort=False):
            series = self._keys.get(key)
            if series is None:
                continue
            times = series["times"]
            query = block["t"].to_numpy()
            index = np.searchsorted(times, query, side="right") - 1
            valid = index >= 0
            target = np.where(block["d"].to_numpy() > 0, 1, 0)
            following = np.full(len(block), -1, dtype="int64")
            if valid.any():
                up = series["up"][index[valid]]
                down = series["down"][index[valid]]
                following[valid] = np.where(target[valid] == 1, up, down)
            resolved = np.full(len(block), np.nan)
            found = following >= 0
            if found.any():
                resolved[found] = (times[following[found]] - query[found]) / 1e6
            out[block.index.to_numpy()] = resolved
        return out


def consumption_times() -> pd.DataFrame:
    """The instant each level episode was first traded through, exactly.

    A print classified as trading beyond the quote on side s at time tau is by construction
    beyond the best price on side s at tau, which inside an episode is that episode's own price,
    so no price comparison is needed.
    """
    prints = pd.concat(
        [events.load_through_prints(entry.file_index) for entry in corpus.CORPUS],
        ignore_index=True,
    )
    episodes = qt_data.load_episodes()[
        ["file_index", "segment_id", "level_episode_id", "side", "start_ns", "end_ns"]
    ]
    for column in ("side", "file_index"):
        prints[column] = prints[column].astype("int64")
        episodes[column] = episodes[column].astype("int64")
    left = prints.sort_values("event_ns", ignore_index=True)
    right = episodes.sort_values("start_ns", ignore_index=True)
    merged = pd.merge_asof(
        left,
        right,
        left_on="event_ns",
        right_on="start_ns",
        by=["file_index", "side"],
        direction="backward",
        allow_exact_matches=True,
    )
    merged = merged[np.isfinite(merged["end_ns"]) & (merged["event_ns"] <= merged["end_ns"])]
    first = merged.groupby(["file_index", "level_episode_id"], sort=False)["event_ns"].min()
    return first.rename("consumption_ns").reset_index()


def episode_signal_table(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per (episode, threshold): when the signal fired and what happened next."""
    passage = FirstPassage(economic_data.load_mid_path())
    consumed = consumption_times()
    episodes = qt_data.load_episodes()[
        ["file_index", "segment_id", "level_episode_id", "side", "start_ns", "end_ns",
         "close_reason", "prints_through", "duration_ms"]
    ]
    episodes = episodes.merge(consumed, on=["file_index", "level_episode_id"], how="left")

    columns = [
        "timestamp_ns", "file_index", "segment_id", "side", "level_episode_id", "sweep_p",
        "mid_ticks", "next_move_matches_sweep_direction", "time_to_next_mid_move_ms",
    ] + [f"directional_markout_{h}ms" for h in spec.HEADLINE_HORIZONS_MS]
    work = frame[columns].copy()

    rows = []
    for threshold in spec.SWEEP_THRESHOLDS:
        above = work[work["sweep_p"] >= threshold]
        if above.empty:
            continue
        first = above.loc[
            above.groupby(["file_index", "level_episode_id"], sort=False)[
                "timestamp_ns"
            ].idxmin()
        ].copy()
        first["threshold"] = threshold
        rows.append(first)
    if not rows:
        return pd.DataFrame()
    crossings = pd.concat(rows, ignore_index=True)
    crossings = crossings.merge(
        episodes.drop(columns=["side", "segment_id"]),
        on=["file_index", "level_episode_id"],
        how="left",
    )
    direction = np.where(crossings["side"].to_numpy() == 1, 1.0, -1.0)
    crossings["sweep_direction"] = direction.astype("int8")
    crossings["lead_to_same_direction_move_ms"] = passage.time_to_move(
        crossings["file_index"].to_numpy(),
        crossings["segment_id"].to_numpy(),
        crossings["timestamp_ns"].to_numpy(),
        direction,
    )
    consumption = crossings["consumption_ns"].to_numpy(dtype="float64")
    crossing_ns = crossings["timestamp_ns"].to_numpy(dtype="float64")
    crossings["was_consumed"] = np.isfinite(consumption)
    crossings["lead_to_consumption_ms"] = np.where(
        np.isfinite(consumption), (consumption - crossing_ns) / 1e6, np.nan
    )
    crossings["time_to_episode_end_ms"] = (
        crossings["end_ns"].to_numpy(dtype="float64") - crossing_ns
    ) / 1e6
    return crossings


def lead_time_summary(crossings: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (threshold, column), label in [
        ((t, c), c)
        for t in spec.SWEEP_THRESHOLDS
        for c in ("lead_to_same_direction_move_ms", "lead_to_consumption_ms")
    ]:
        block = crossings[crossings["threshold"] == threshold]
        if block.empty:
            continue
        for population, mask in _populations(block):
            values = block.loc[mask, column].to_numpy(dtype="float64")
            finite = values[np.isfinite(values)]
            record = {
                "threshold": threshold,
                "measure": label,
                "population": population,
                "crossings": int(mask.sum()),
                "resolved": int(finite.size),
                "resolution_rate": float(finite.size / mask.sum()) if mask.sum() else np.nan,
            }
            if finite.size:
                for level, value in zip(
                    QUANTILES, np.quantile(finite, QUANTILES), strict=True
                ):
                    record[f"p{level * 100:g}_ms"] = float(value)
                record["mean_ms"] = float(finite.mean())
                edges = spec.LEAD_TIME_BINS_MS
                record["frac_le_0ms"] = float((finite <= 0).mean())
                for low, high in zip(edges[:-1], edges[1:], strict=False):
                    record[f"frac_{low:g}_{high:g}ms"] = float(
                        ((finite > low) & (finite <= high)).mean()
                    )
                record["frac_gt_500ms"] = float((finite > 500).mean())
            rows.append(record)
    return pd.DataFrame(rows)


def _populations(block: pd.DataFrame):
    consumed = block["was_consumed"].to_numpy(dtype=bool)
    side = block["side"].to_numpy()
    return [
        ("all", np.ones(len(block), dtype=bool)),
        ("consumed", consumed),
        ("not_consumed", ~consumed),
        ("threatened_ask_upward", side == 1),
        ("threatened_bid_downward", side == 0),
    ]


def false_positive_table(crossings: pd.DataFrame) -> pd.DataFrame:
    """What happens when a high score does not turn into an actual sweep."""
    rows = []
    for threshold, block in crossings.groupby("threshold", sort=True):
        consumed = block["was_consumed"].to_numpy(dtype=bool)
        for population, mask in (
            ("true_positive_consumed", consumed),
            ("false_positive_not_consumed", ~consumed),
        ):
            part = block[mask]
            if part.empty:
                continue
            mid = part["mid_ticks"].to_numpy(dtype="float64")
            record = {
                "threshold": threshold,
                "population": population,
                "crossings": int(len(part)),
                "share_of_crossings": float(len(part) / len(block)),
                "p_next_move_follows_sweep": float(
                    np.nanmean(part["next_move_matches_sweep_direction"].to_numpy())
                ),
                "median_time_to_episode_end_ms": float(
                    np.nanmedian(part["time_to_episode_end_ms"].to_numpy())
                ),
                "median_time_to_next_mid_move_ms": float(
                    np.nanmedian(part["time_to_next_mid_move_ms"].to_numpy())
                ),
            }
            from pyresearch.native.directional import analysis

            for horizon in spec.HEADLINE_HORIZONS_MS:
                record.update(
                    analysis.markout_stats(
                        part[f"directional_markout_{horizon}ms"].to_numpy(dtype="float64"),
                        mid,
                        f"markout_{horizon}ms",
                    )
                )
            rows.append(record)
    return pd.DataFrame(rows)
