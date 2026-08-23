"""Raw event-time study around actual level consumption.

This is model-free. It aligns local receive time at the event, tau = 0, and measures the
side-normalised mid path around it. The point is to separate the movement that has already
happened by the time the event is observable from the movement still available afterwards,
because only the second kind can be executed against.

Sign convention is the same one the rest of the phase uses: a threatened best ask normalises
with +1 and a threatened best bid with -1. A price-improvement event on the bid therefore shows
a *negative* normalised path, because the price moved the other way. That is the point of
keeping one convention rather than flipping it per event class.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pyresearch.native.directional import data, spec
from pyresearch.native.economic import data as economic_data
from pyresearch.native.queue_tail import data as qt_data
from pyresearch.native.core import corpus

TIMING_DIR = spec.ROOT / "data/research/native_predictive_v1"
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)


def timing_path(file_index: int):
    return TIMING_DIR / f"cross_stream_timing_file{file_index}.csv.zst"


def load_through_prints(file_index: int) -> pd.DataFrame:
    """Every aggressive print that traded beyond the best price on its passive side."""
    frame = data._read_csv_zst(
        timing_path(file_index),
        ["receive_ns", "file_index", "category", "passive_side", "trade_qty_lots", "mid_ticks"],
    )
    frame = frame[frame["category"] == "through_quote"].reset_index(drop=True)
    frame["side"] = np.where(frame["passive_side"] == "ask", 1, 0).astype("int8")
    frame["event_ns"] = frame["receive_ns"].astype("int64")
    frame["event_class"] = "aggressive_trade_through"
    return frame[["event_ns", "file_index", "side", "event_class", "trade_qty_lots"]]


def load_episode_events() -> pd.DataFrame:
    """Level episodes that ended for an observable reason, classified by how they ended."""
    episodes = qt_data.load_episodes()
    episodes = episodes[
        episodes["close_reason"].isin(["improved", "stepped_away", "book_side_empty"])
    ].copy()
    through = episodes["prints_through"].to_numpy() > 0
    reason = episodes["close_reason"].to_numpy()
    label = np.where(
        reason == "improved",
        "price_improvement",
        np.where(through, "stepped_away_with_through_print", "consumed_without_trade_through"),
    )
    episodes["event_class"] = label
    episodes["event_ns"] = episodes["end_ns"].astype("int64")
    episodes["trade_qty_lots"] = episodes["cum_trade_at_quote"].to_numpy()
    ordinary = episodes.copy()
    ordinary["event_class"] = np.where(
        reason == "improved", "price_improvement", "level_disappearance"
    )
    combined = pd.concat([episodes, ordinary[ordinary["event_class"] == "level_disappearance"]])
    return combined[
        ["event_ns", "file_index", "segment_id", "side", "event_class", "trade_qty_lots"]
    ].reset_index(drop=True)


def _attach_segment(frame: pd.DataFrame, bounds: pd.DataFrame) -> pd.DataFrame:
    """Locate each event inside its segment; events outside every segment are dropped."""
    if "segment_id" in frame.columns:
        keyed = frame.merge(bounds, on=["file_index", "segment_id"], how="left")
    else:
        keyed = frame.merge(bounds, on="file_index", how="left")
        keyed = keyed[
            (keyed["event_ns"] >= keyed["start_ns"]) & (keyed["event_ns"] <= keyed["end_ns"])
        ]
    return keyed.dropna(subset=["start_ns", "end_ns"]).reset_index(drop=True)


def event_paths(mid: data.MidSeries, events: pd.DataFrame) -> pd.DataFrame:
    """Side-normalised mid displacement at every fixed tau, relative to the event instant."""
    file_index = events["file_index"].to_numpy()
    segment_id = events["segment_id"].to_numpy()
    event_ns = events["event_ns"].to_numpy()
    start_ns = events["start_ns"].to_numpy()
    end_ns = events["end_ns"].to_numpy()
    direction = np.where(events["side"].to_numpy() == 1, 1.0, -1.0)

    base = mid.mid_at(file_index, segment_id, event_ns)
    out = pd.DataFrame(
        {
            "event_class": events["event_class"].to_numpy(),
            "side": events["side"].to_numpy(),
            "file_index": file_index,
            "segment_id": segment_id,
            "event_ns": event_ns,
            "mid_at_event": base,
        }
    )
    for tau in spec.EVENT_TAUS_MS:
        when = event_ns + int(tau * 1e6)
        # A tau that falls outside the event's own segment is censored, never clipped.
        inside = (when >= start_ns) & (when <= end_ns)
        value = np.full(len(events), np.nan)
        if inside.any():
            value[inside] = mid.mid_at(file_index[inside], segment_id[inside], when[inside])
        out[f"tau_{tau}ms"] = direction * (value - base)
    return out


def summarise(paths: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for key, block in paths.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        record = {**dict(zip(keys, values, strict=True)), "events": len(block)}
        for tau in spec.EVENT_TAUS_MS:
            column = block[f"tau_{tau}ms"].to_numpy(dtype="float64")
            finite = column[np.isfinite(column)]
            record[f"tau_{tau}ms_observed"] = int(finite.size)
            if finite.size == 0:
                continue
            record[f"tau_{tau}ms_mean"] = float(finite.mean())
            record[f"tau_{tau}ms_median"] = float(np.median(finite))
            for level, value in zip(QUANTILES, np.quantile(finite, QUANTILES), strict=True):
                record[f"tau_{tau}ms_p{level * 100:g}"] = float(value)
        # How much of the move around the event has already happened when it becomes visible.
        pre = record.get("tau_0ms_mean", np.nan) - record.get("tau_-500ms_mean", np.nan)
        for tau in (100, 500, 1000, 5000):
            post = record.get(f"tau_{tau}ms_mean", np.nan) - record.get("tau_0ms_mean", np.nan)
            total = pre + post
            record[f"post_event_move_{tau}ms"] = post
            record[f"share_of_move_before_event_{tau}ms"] = (
                float(pre / total) if np.isfinite(total) and total != 0 else np.nan
            )
        record["pre_event_move_500ms"] = pre
        rows.append(record)
    return pd.DataFrame(rows)


def run() -> tuple[pd.DataFrame, pd.DataFrame]:
    bounds = economic_data.segment_bounds()
    mid = data.MidSeries(economic_data.load_mid_path())
    prints = pd.concat(
        [load_through_prints(entry.file_index) for entry in corpus.CORPUS], ignore_index=True
    )
    prints = _attach_segment(prints, bounds)
    episodes = _attach_segment(load_episode_events(), bounds)
    events = pd.concat([prints, episodes], ignore_index=True)
    paths = event_paths(mid, events)
    pooled = summarise(paths, ["event_class"])
    pooled.insert(0, "population", "all")
    by_side = summarise(paths, ["event_class", "side"])
    by_side.insert(0, "population", "by_side")
    return pd.concat([pooled, by_side], ignore_index=True), paths
