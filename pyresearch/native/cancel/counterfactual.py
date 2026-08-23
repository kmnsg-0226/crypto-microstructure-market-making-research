"""Replay the stay / cancel intervention over the frozen phase 3 placements.

The intervention is deliberately the smallest one that can be called market-maker-like: an order
that is already resting is either left alone or withdrawn. It is never moved, never replaced and
never re-entered, and the hypothetical order does not influence the book, so cancelling it can
only ever remove an execution that the never-cancel path would have had. That is what makes the
counterfactual exact rather than simulated: the never-cancel fill time is known from the phase 3
replay, and a cancellation that becomes effective before it removes it, while one that becomes
effective at or after it does not.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pyresearch.native.cancel import scoring, spec
from pyresearch.native.queue_tail import data as qt_data

GROUP = ["file_index", "segment_id", "side", "quote_px_ticks"]


def load_orders() -> pd.DataFrame:
    """The phase 3 grid placements for the three fixed queue cells, markouts attached."""
    fills = qt_data.load_fills(level_birth=False)
    keep = [
        "placement_ns",
        "file_index",
        "segment_id",
        "side",
        "alpha_pct",
        "beta_pct",
        "queue_cell",
        "quote_px_ticks",
        "observed_end_ns",
        "segment_end_ns",
        "fill_ns",
        "mechanism",
        "mechanism_name",
        "filled",
        "time_to_fill_ms",
    ]
    keep += [f"markout_{h}ms_ticks" for h in spec.MARKOUT_HORIZONS_MS]
    keep += [f"catastrophic_{t}" for t in spec.CATASTROPHIC_THRESHOLDS_TICKS]
    return fills[keep].reset_index(drop=True)


def opportunities(orders: pd.DataFrame) -> pd.DataFrame:
    """One row per placement, independent of the queue assumption.

    Placement instant, side, quote price and observation window are properties of the phase 3
    replay, not of alpha or beta, so the cancellation timeline is computed once and reused.
    """
    columns = GROUP + ["placement_ns", "observed_end_ns"]
    unique = orders[columns].drop_duplicates(subset=["placement_ns", "file_index", "side"])
    return unique.sort_values(["placement_ns", "side"], ignore_index=True)


# --------------------------------------------------------------------------------------------
# Decision timeline
# --------------------------------------------------------------------------------------------
def _group_ids(orders: pd.DataFrame, scores: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """A shared integer encoding of (file, segment, side, price) for both tables."""
    left = pd.MultiIndex.from_arrays([orders[c] for c in GROUP])
    right = pd.MultiIndex.from_arrays(
        [scores[c] for c in ["file_index", "segment_id", "side", "quote_price_ticks"]]
    )
    codes, _ = pd.factorize(left.append(right))
    return codes[: len(orders)], codes[len(orders) :]


def decision_timeline(opportunity: pd.DataFrame, scores: pd.DataFrame) -> pd.DataFrame:
    """For every placement and every fixed threshold, when the signal first crosses.

    A decision instant is a 100 ms lifecycle row strictly after the placement instant, on the
    same side and in the same segment, while the order's own quote price is still the best price
    on its side. Once the price stops being best there is no level-sweep score that refers to
    the order's level, and no cancellation decision is taken; how often that happens is reported
    rather than filled in with an assumption.
    """
    left = opportunity.copy().reset_index(drop=True)
    left["gid"], right_gid = _group_ids(left, scores)
    right = pd.DataFrame(
        {
            "gid": right_gid,
            "timestamp_ns": scores["timestamp_ns"].to_numpy(),
            "sweep_p": scores["sweep_p"].to_numpy(),
        }
    ).sort_values(["gid", "timestamp_ns"], ignore_index=True)
    # Counting decision instants inside a window means counting rows of the same level, so the
    # running count has to be per group, never global.
    right["n_all"] = right.groupby("gid").cumcount() + 1
    right = right.sort_values("timestamp_ns", ignore_index=True)
    counted = right["n_all"].to_numpy()
    everything = np.ones(len(right), dtype=bool)

    before_start = _asof(left, right, "placement_ns", everything, "backward", True, counted)
    before_end = _asof(left, right, "observed_end_ns", everything, "backward", True, counted)
    left["decision_instants"] = (before_end - before_start).astype("int64")
    left["first_decision_ns"] = _first_after(left, right, "placement_ns", everything)
    left["last_decision_ns"] = _last_before(left, right, "observed_end_ns", everything)

    for threshold in spec.CANCEL_THRESHOLDS:
        mask = right["sweep_p"].to_numpy() >= threshold
        crossing = _first_after(left, right, "placement_ns", mask)
        # A crossing after the observation window ends is not an observed crossing.
        left[_crossing_column(threshold)] = np.where(
            (crossing > 0) & (crossing <= left["observed_end_ns"].to_numpy()), crossing, 0
        )
    return left


def _crossing_column(threshold: float) -> str:
    return f"first_crossing_ns_p{int(round(threshold * 100)):02d}"


def _asof(left, right, left_on, mask, direction, allow_exact, value) -> np.ndarray:
    """merge_asof restricted to ``mask`` rows of ``right``, returning ``value`` or 0.

    Both sides are sorted on the join key here and the left order is restored afterwards, so a
    caller never has to remember which of its several time columns the frame happens to be
    sorted by.
    """
    subset = right.loc[mask, ["gid", "timestamp_ns"]].copy()
    subset["_value"] = (
        right["timestamp_ns"].to_numpy()[mask] if value is None else value[mask]
    )
    subset = subset.rename(columns={"timestamp_ns": "_key"}).sort_values(
        "_key", ignore_index=True
    )
    keys = left[["gid", left_on]].rename(columns={left_on: "_key"}).copy()
    keys["_position"] = np.arange(len(keys))
    keys = keys.sort_values("_key", ignore_index=True)
    merged = pd.merge_asof(
        keys, subset, on="_key", by="gid", direction=direction, allow_exact_matches=allow_exact
    )
    out = np.zeros(len(left), dtype="int64")
    out[merged["_position"].to_numpy()] = merged["_value"].fillna(0).to_numpy().astype("int64")
    return out


def _first_after(left, right, left_on, mask) -> np.ndarray:
    return _asof(left, right, left_on, mask, "forward", False, None)


def _last_before(left, right, left_on, mask) -> np.ndarray:
    return _asof(left, right, left_on, mask, "backward", True, None)


# --------------------------------------------------------------------------------------------
# Persistence of the signal before the fill
# --------------------------------------------------------------------------------------------
def persistence(
    orders: pd.DataFrame, scores: pd.DataFrame, threshold: float
) -> pd.DataFrame:
    """How long the score had been above ``threshold`` immediately before a fill.

    Separates a genuine advance warning from a signal that only lights up at the instant the
    sweep is already underway. This is descriptive; no persistence filter is used as a policy.
    """
    filled = orders[orders["filled"]].copy()
    filled["gid"], right_gid = _group_ids(filled, scores)
    right = pd.DataFrame(
        {
            "gid": right_gid,
            "timestamp_ns": scores["timestamp_ns"].to_numpy(),
            "sweep_p": scores["sweep_p"].to_numpy(),
        }
    ).sort_values(["gid", "timestamp_ns"], ignore_index=True)

    above = right["sweep_p"].to_numpy() >= threshold
    # A run breaks whenever the group changes, the previous instant was below the threshold, or
    # the decision grid skipped an instant.
    gid = right["gid"].to_numpy()
    stamps = right["timestamp_ns"].to_numpy()
    step = int(spec.DECISION_GRID_MS * 1e6)
    contiguous = np.empty(len(right), dtype=bool)
    contiguous[0] = False
    contiguous[1:] = (gid[1:] == gid[:-1]) & (stamps[1:] - stamps[:-1] == step) & above[:-1]
    run_start = np.where(above & ~contiguous)[0]
    run_id = np.full(len(right), -1, dtype="int64")
    run_id[above] = np.searchsorted(run_start, np.where(above)[0], side="right") - 1
    run_start_ns = np.zeros(len(right), dtype="int64")
    run_start_ns[above] = stamps[run_start][run_id[above]]
    run_index = np.zeros(len(right), dtype="int64")
    run_index[above] = np.arange(len(right))[above] - run_start[run_id[above]]

    right = right.assign(
        run_start_ns=run_start_ns, run_index=run_index
    ).sort_values("timestamp_ns", ignore_index=True)
    above = right["sweep_p"].to_numpy() >= threshold
    filled = filled.reset_index(drop=True)

    last_ns = _asof(filled, right, "fill_ns", above, "backward", False, None)
    start_ns = _asof(
        filled, right, "fill_ns", above, "backward", False, right["run_start_ns"].to_numpy()
    )
    index = _asof(
        filled, right, "fill_ns", above, "backward", False, right["run_index"].to_numpy()
    )
    # Only a run that is still active at the last decision instant before the fill counts as
    # persistent warning; an earlier, already-finished run is not.
    last_any = _asof(
        filled, right, "fill_ns", np.ones(len(right), dtype=bool), "backward", False, None
    )
    active = (last_ns > 0) & (last_ns == last_any)
    filled["run_observations"] = np.where(active, index + 1, 0)
    filled["run_duration_ms"] = np.where(active, (last_ns - start_ns) / 1e6, np.nan)
    filled["warning_ms"] = np.where(
        active, (filled["fill_ns"].to_numpy() - start_ns) / 1e6, np.nan
    )
    filled["threshold"] = threshold
    return filled


# --------------------------------------------------------------------------------------------
# The intervention itself
# --------------------------------------------------------------------------------------------
def apply_cancel(
    orders: pd.DataFrame, timeline: pd.DataFrame, threshold: float, latency_ms: int
) -> pd.DataFrame:
    """Attach the counterfactual outcome of one (threshold, latency) cell to every order."""
    key = ["placement_ns", "file_index", "side"]
    column = _crossing_column(threshold)
    joined = orders.merge(timeline[key + [column, "decision_instants"]], on=key, how="left")

    crossing = joined[column].fillna(0).to_numpy(dtype="int64")
    cancelled = crossing > 0
    effective = np.where(cancelled, crossing + int(latency_ms * 1e6), 0)
    fill_ns = joined["fill_ns"].to_numpy(dtype="int64")
    baseline_filled = joined["filled"].to_numpy(dtype=bool)

    # A fill stamped at or before the effective instant survives. At zero latency the print that
    # causes it is part of the information the score was computed from, and information observed
    # at t cannot retract an execution that already happened at t.
    prevented = cancelled & baseline_filled & (fill_ns > effective)
    too_late = cancelled & baseline_filled & (fill_ns <= effective)

    joined["threshold"] = threshold
    joined["latency_ms"] = latency_ms
    joined["cancel_signalled"] = cancelled
    joined["cancel_ns"] = np.where(cancelled, crossing, np.nan)
    joined["effective_cancel_ns"] = np.where(cancelled, effective, np.nan)
    joined["fill_prevented"] = prevented
    joined["cancel_too_late"] = too_late
    joined["cancel_without_baseline_fill"] = cancelled & ~baseline_filled
    joined["order_unaffected"] = ~cancelled
    joined["surviving_fill"] = baseline_filled & ~prevented
    joined["lead_time_ms"] = np.where(
        cancelled & baseline_filled, (fill_ns - crossing) / 1e6, np.nan
    )
    return classify(joined)


def classify(frame: pd.DataFrame) -> pd.DataFrame:
    """Ex-post labels for what the causal decision turned out to have done."""
    markout = frame[f"markout_{spec.PRIMARY_MARKOUT_MS}ms_ticks"].to_numpy(dtype="float64")
    observed = np.isfinite(markout)
    prevented = frame["fill_prevented"].to_numpy(dtype=bool)

    frame["adverse_fill_avoided"] = prevented & observed & (markout < 0)
    frame["favourable_fill_sacrificed"] = prevented & observed & (markout > 0)
    frame["non_negative_fill_sacrificed"] = prevented & observed & (markout >= 0)
    frame["prevented_fill_markout_censored"] = prevented & ~observed
    for threshold in spec.CATASTROPHIC_THRESHOLDS_TICKS:
        frame[f"catastrophic_{threshold}_avoided"] = (
            prevented & observed & (markout <= -threshold)
        )
    # Ticks of adverse and favourable markout the cancellation removed from the cohort.
    frame["adverse_ticks_avoided"] = np.where(
        prevented & observed, -np.minimum(markout, 0.0), 0.0
    )
    frame["favourable_ticks_sacrificed"] = np.where(
        prevented & observed, np.maximum(markout, 0.0), 0.0
    )
    frame["net_markout_preserved"] = (
        frame["adverse_ticks_avoided"] - frame["favourable_ticks_sacrificed"]
    )
    return frame
