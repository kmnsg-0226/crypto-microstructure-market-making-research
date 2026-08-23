"""Decay analysis for phase 6.

Every table here is descriptive. No model is fitted, no horizon is nominated, no threshold is
chosen. The one derived decision quantity — the drift-demeaned top-minus-bottom decile spread of
the markout — is defined in `spec` before any of this ran.

One `Prepared` view is built per (signal, horizon) and every table reads from it, so the
population filter, the demeaning and the decile assignment each happen exactly once and every
table in the phase is guaranteed to be describing the identical rows.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from pyresearch.native.decay import data, spec
from pyresearch.native.predictive.modeling import block_bootstrap


# ---------------------------------------------------------------------------------------------
# Population and preparation
# ---------------------------------------------------------------------------------------------
def population_mask(frame: pd.DataFrame, signal: str, horizon_s: float, full_corpus=False):
    """Rows evaluable for one signal at one horizon, under the horizon-specific purge."""
    keep = frame[f"markout_{horizon_s}s_ticks"].notna() & frame[signal].notna()
    if not full_corpus:
        keep &= frame["has_all_signals"]
        keep &= frame["fold"] >= 0
        keep &= frame["seconds_into_validation_block"] >= spec.purge_seconds(horizon_s)
    return keep.to_numpy()


def demean(values: np.ndarray, block: np.ndarray) -> np.ndarray:
    """Subtract the row's own 30-minute time-block mean markout. Realised returns only."""
    table = pd.DataFrame({"v": values, "b": block})
    return values - table.groupby("b")["v"].transform("mean").to_numpy()


def deciles(values: np.ndarray, count: int = spec.DECILES) -> np.ndarray:
    """Fixed global deciles by rank, ties broken deterministically by first occurrence."""
    order = pd.Series(values).rank(method="first").to_numpy()
    return np.minimum((order - 1) * count // len(values), count - 1).astype("int64")


@dataclass
class Prepared:
    signal: str
    horizon_s: float
    population: str
    signal_values: np.ndarray
    mid: np.ndarray
    raw: np.ndarray
    adj: np.ndarray
    pivot: np.ndarray | None
    bucket: np.ndarray
    anchor: np.ndarray
    boot_block: np.ndarray
    demean_block: np.ndarray
    utc_day: np.ndarray
    segment_key: np.ndarray

    @property
    def rows(self) -> int:
        return int(self.signal_values.size)


def prepare(
    frame: pd.DataFrame, signal: str, horizon_s: float, full_corpus=False
) -> Prepared | None:
    mask = population_mask(frame, signal, horizon_s, full_corpus)
    if not mask.any():
        return None
    part = frame.loc[mask]
    raw = part[f"markout_{horizon_s}s_ticks"].to_numpy(dtype="float64")
    demean_block = part["demean_block"].to_numpy()
    pivot = None
    if horizon_s > spec.PIVOT_S:
        pivot = part[f"markout_{spec.PIVOT_S}s_ticks"].to_numpy(dtype="float64")
    return Prepared(
        signal=signal,
        horizon_s=horizon_s,
        population="full_corpus" if full_corpus else "oof_scored_purged",
        signal_values=part[signal].to_numpy(dtype="float64"),
        mid=part["mid_ticks"].to_numpy(dtype="float64"),
        raw=raw,
        adj=demean(raw, demean_block),
        pivot=pivot,
        bucket=deciles(part[signal].to_numpy(dtype="float64")),
        anchor=part[f"anchor_{horizon_s}s"].to_numpy(dtype=bool),
        boot_block=data.bootstrap_block(part, horizon_s),
        demean_block=demean_block,
        utc_day=part["utc_day"].to_numpy(),
        segment_key=part["segment_key"].to_numpy(),
    )


def _bps(ticks: np.ndarray, mid: np.ndarray) -> np.ndarray:
    return 1e4 * ticks / mid


# ---------------------------------------------------------------------------------------------
# Decay profile: signed markout, hit rate, effective sample
# ---------------------------------------------------------------------------------------------
def signal_horizon_record(view: Prepared) -> dict:
    sign = np.sign(view.signal_values)
    record = {
        "signal": view.signal,
        "horizon_s": view.horizon_s,
        "population": view.population,
        "purge_s": spec.purge_seconds(view.horizon_s),
        "rows": view.rows,
        "anchors": int(view.anchor.sum()),
        "bootstrap_block_s": spec.bootstrap_block_seconds(view.horizon_s),
        "bootstrap_blocks": int(pd.unique(view.boot_block).size),
        "demean_blocks": int(pd.unique(view.demean_block).size),
        "mid_ticks_mean": float(view.mid.mean()),
    }
    for label, values in (("raw", view.raw), ("demeaned", view.adj)):
        signed = sign * values
        moved = values != 0
        record[f"{label}_signed_markout_mean_ticks"] = float(signed.mean())
        record[f"{label}_signed_markout_median_ticks"] = float(np.median(signed))
        record[f"{label}_signed_markout_mean_bps"] = float(_bps(signed, view.mid).mean())
        record[f"{label}_hit_rate"] = (
            float((signed[moved] > 0).mean()) if moved.any() else np.nan
        )
        record[f"{label}_moved_rows"] = int(moved.sum())
        record[f"{label}_unconditional_mean_ticks"] = float(values.mean())
    return record


# ---------------------------------------------------------------------------------------------
# Decile tables
# ---------------------------------------------------------------------------------------------
def decile_table(view: Prepared) -> pd.DataFrame:
    rows = []
    for index in range(spec.DECILES):
        mask = view.bucket == index
        s, mid = view.signal_values[mask], view.mid[mask]
        raw, adj = view.raw[mask], view.adj[mask]
        rows.append(
            {
                "signal": view.signal,
                "horizon_s": view.horizon_s,
                "population": view.population,
                "decile": index,
                "rows": int(mask.sum()),
                "signal_mean": float(s.mean()),
                "signal_min": float(s.min()),
                "signal_max": float(s.max()),
                "raw_mean_ticks": float(raw.mean()),
                "raw_median_ticks": float(np.median(raw)),
                "raw_mean_bps": float(_bps(raw, mid).mean()),
                "demeaned_mean_ticks": float(adj.mean()),
                "demeaned_median_ticks": float(np.median(adj)),
                "demeaned_mean_bps": float(_bps(adj, mid).mean()),
                "frac_up": float((raw > 0).mean()),
                "frac_down": float((raw < 0).mean()),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------
# The decision quantity: cumulative and incremental decile spreads
# ---------------------------------------------------------------------------------------------
def _spread_by_block(values, bucket, block):
    table = pd.DataFrame({"v": values, "d": bucket, "b": block})
    top = table[table["d"] == spec.DECILES - 1].groupby("b")["v"].agg(["mean", "size"])
    bottom = table[table["d"] == 0].groupby("b")["v"].agg(["mean", "size"])
    joined = top.join(bottom, how="inner", lsuffix="_top", rsuffix="_bottom")
    spread = (joined["mean_top"] - joined["mean_bottom"]).to_numpy()
    weight = (joined["size_top"] + joined["size_bottom"]).to_numpy(dtype="float64")
    return spread, weight


def spread_record(view: Prepared, kind: str) -> dict:
    if kind == "incremental":
        if view.pivot is None:
            return {}
        base = view.raw - view.pivot
    else:
        base = view.raw
    adjusted = demean(base, view.demean_block)
    top_mask = view.bucket == spec.DECILES - 1
    bottom_mask = view.bucket == 0
    record = {
        "signal": view.signal,
        "horizon_s": view.horizon_s,
        "kind": kind,
        "population": view.population,
        "rows": view.rows,
        "anchors": int(view.anchor.sum()),
        "bootstrap_block_s": spec.bootstrap_block_seconds(view.horizon_s),
        "bootstrap_draws": spec.BOOTSTRAP_DRAWS,
    }
    for label, values in (("raw", base), ("demeaned", adjusted)):
        if kind == "incremental":
            # The pivot term measured on THIS horizon's own population, which is the only
            # decomposition that is an identity: a longer horizon evaluates fewer rows under its
            # own longer purge, so the 5 s spread measured on the 5 s population is a different
            # quantity and is reported separately rather than reconciled against.
            pivot_values = view.pivot if label == "raw" else demean(
                view.pivot, view.demean_block
            )
            record[f"{label}_pivot_spread_ticks"] = float(
                pivot_values[top_mask].mean() - pivot_values[bottom_mask].mean()
            )
        top, bottom = values[top_mask], values[bottom_mask]
        spread_ticks = float(top.mean() - bottom.mean())
        top_bps = float(_bps(top, view.mid[top_mask]).mean())
        bottom_bps = float(_bps(bottom, view.mid[bottom_mask]).mean())
        record[f"{label}_top_mean_ticks"] = float(top.mean())
        record[f"{label}_bottom_mean_ticks"] = float(bottom.mean())
        record[f"{label}_spread_ticks"] = spread_ticks
        record[f"{label}_top_mean_bps"] = top_bps
        record[f"{label}_bottom_mean_bps"] = bottom_bps
        record[f"{label}_spread_bps"] = top_bps - bottom_bps
        # A decile spread is a long-short quantity spanning two trades. The per-trade edge — the
        # average of the long leg's edge and the short leg's edge — is what a single round trip
        # would have to pay for, so it is what the cost diagnostic compares against.
        record[f"{label}_edge_per_trade_bps"] = 0.5 * (top_bps - bottom_bps)
        per_block, weight = _spread_by_block(values, view.bucket, view.boot_block)
        centre, low, high = block_bootstrap(
            per_block, weight, seed=spec.SEED, draws=spec.BOOTSTRAP_DRAWS
        )
        record[f"{label}_block_spread_mean"] = centre
        record[f"{label}_spread_p05"] = low
        record[f"{label}_spread_p95"] = high
        record[f"{label}_blocks"] = int(per_block.size)
        same = np.sign(per_block) == np.sign(spread_ticks)
        record[f"{label}_blocks_same_sign"] = int(same.sum())
        record[f"{label}_block_sign_share"] = (
            float(same.mean()) if per_block.size else np.nan
        )
        record[f"{label}_resolved"] = bool(
            np.isfinite(low) and np.isfinite(high) and (low > 0 or high < 0)
        )
        # Independence check: the same spread on the deterministic non-overlapping anchors.
        a_top = values[view.anchor & top_mask]
        a_bottom = values[view.anchor & bottom_mask]
        anchor_spread = (
            float(a_top.mean() - a_bottom.mean())
            if a_top.size and a_bottom.size
            else np.nan
        )
        record[f"{label}_anchor_spread_ticks"] = anchor_spread
        record[f"{label}_anchor_rows"] = int(a_top.size + a_bottom.size)
        record[f"{label}_anchor_sign_agrees"] = bool(
            np.isfinite(anchor_spread) and np.sign(anchor_spread) == np.sign(spread_ticks)
        )
    return record


# ---------------------------------------------------------------------------------------------
# Up / down legs, reported separately so a common drift is visible as a common shift
# ---------------------------------------------------------------------------------------------
def side_table(view: Prepared) -> pd.DataFrame:
    s = view.signal_values
    legs = {
        "up_leg_top_decile": view.bucket == spec.DECILES - 1,
        "down_leg_bottom_decile": view.bucket == 0,
        "up_leg_positive_signal": s > 0,
        "down_leg_negative_signal": s < 0,
    }
    rows = []
    for name, mask in legs.items():
        if not mask.any():
            continue
        raw, adj, mid = view.raw[mask], view.adj[mask], view.mid[mask]
        rows.append(
            {
                "signal": view.signal,
                "horizon_s": view.horizon_s,
                "leg": name,
                "rows": int(mask.sum()),
                "raw_mean_ticks": float(raw.mean()),
                "raw_mean_bps": float(_bps(raw, mid).mean()),
                "demeaned_mean_ticks": float(adj.mean()),
                "demeaned_mean_bps": float(_bps(adj, mid).mean()),
                "frac_favourable_for_leg": float(
                    (raw > 0).mean() if name.startswith("up") else (raw < 0).mean()
                ),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------
# Stability by UTC day and by segment
# ---------------------------------------------------------------------------------------------
def stability_table(view: Prepared, by: str) -> pd.DataFrame:
    group = {"utc_day": view.utc_day, "segment_key": view.segment_key}[by]
    incremental = None
    if view.pivot is not None:
        incremental = demean(view.raw - view.pivot, view.demean_block)
    top_mask = view.bucket == spec.DECILES - 1
    bottom_mask = view.bucket == 0
    rows = []
    for key in pd.unique(group):
        mask = group == key
        top, bottom = mask & top_mask, mask & bottom_mask
        if not top.any() or not bottom.any():
            continue
        rows.append(
            {
                "signal": view.signal,
                "horizon_s": view.horizon_s,
                "grouping": by,
                "key": key,
                "rows": int(mask.sum()),
                "raw_spread_ticks": float(view.raw[top].mean() - view.raw[bottom].mean()),
                "demeaned_spread_ticks": float(
                    view.adj[top].mean() - view.adj[bottom].mean()
                ),
                "demeaned_incremental_spread_ticks": (
                    float(incremental[top].mean() - incremental[bottom].mean())
                    if incremental is not None
                    else np.nan
                ),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------
# Cost diagnostic. Break-even = the gross edge itself. No PnL, no rule, no selection.
# ---------------------------------------------------------------------------------------------
def hurdle_table(spreads: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for record in spreads[spreads["kind"] == "cumulative"].to_dict("records"):
        spread = record["demeaned_spread_bps"]
        per_trade = record["demeaned_edge_per_trade_bps"]
        for hurdle in spec.COST_HURDLE_BPS:
            rows.append(
                {
                    "signal": record["signal"],
                    "horizon_s": record["horizon_s"],
                    "gross_decile_spread_bps": spread,
                    "gross_edge_per_trade_bps": per_trade,
                    # The break-even is the gross edge itself: the largest all-in cost the
                    # observed movement could absorb. Nothing is netted into a PnL anywhere.
                    "break_even_all_in_cost_bps": per_trade,
                    "hurdle_bps_one_way": hurdle,
                    "hurdle_bps_round_trip": 2 * hurdle,
                    "net_of_one_way_bps": per_trade - hurdle,
                    "net_of_round_trip_bps": per_trade - 2 * hurdle,
                    "clears_one_way": bool(per_trade > hurdle),
                    "clears_round_trip": bool(per_trade > 2 * hurdle),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------
# Classification, by the rules fixed in spec
# ---------------------------------------------------------------------------------------------
def classify(spreads: pd.DataFrame, signal: str) -> dict:
    part = spreads[(spreads["signal"] == signal) & (spreads["population"] == "oof_scored_purged")]
    cumulative = part[part["kind"] == "cumulative"].to_dict("records")
    inc = part[part["kind"] == "incremental"].to_dict("records")

    def resolved(row) -> bool:
        return bool(row["demeaned_resolved"] and row["demeaned_anchor_sign_agrees"])

    def stable(row) -> bool:
        return bool(row["demeaned_block_sign_share"] >= spec.STABILITY_MIN_BLOCK_SHARE)

    resolved_positive = [r for r in inc if resolved(r) and r["demeaned_spread_ticks"] > 0]
    resolved_negative = [r for r in inc if resolved(r) and r["demeaned_spread_ticks"] < 0]
    stable_positive_medium = [
        r for r in resolved_positive if r["horizon_s"] >= 120 and stable(r)
    ]
    positive_short = [r for r in resolved_positive if r["horizon_s"] <= 60]
    cumulative_short_resolved = any(
        resolved(r) for r in cumulative if r["horizon_s"] <= spec.PIVOT_S
    )
    sign_flip = any(
        resolved(r)
        and np.sign(r["demeaned_spread_ticks"]) != np.sign(r["raw_spread_ticks"])
        for r in inc
    )
    unstable_resolved = any(resolved(r) and not stable(r) for r in inc)

    if resolved_positive and resolved_negative:
        label = "ambiguous"
    elif sign_flip or (unstable_resolved and not stable_positive_medium):
        label = "ambiguous"
    elif resolved_negative:
        label = "reversal"
    elif stable_positive_medium:
        label = "medium_horizon_persistence"
    elif positive_short:
        label = "short_continuation"
    elif cumulative_short_resolved:
        label = "ultra_short_only"
    else:
        label = "ambiguous"
    return {
        "signal": signal,
        "classification": label,
        "rule": spec.CLASSIFICATION_RULES[label],
        "resolved_positive_incremental_horizons_s": sorted(
            r["horizon_s"] for r in resolved_positive
        ),
        "resolved_negative_incremental_horizons_s": sorted(
            r["horizon_s"] for r in resolved_negative
        ),
        "stable_positive_medium_horizons_s": sorted(
            r["horizon_s"] for r in stable_positive_medium
        ),
        "cumulative_resolved_at_or_below_pivot": cumulative_short_resolved,
        "raw_vs_demeaned_sign_flip": bool(sign_flip),
    }


# ---------------------------------------------------------------------------------------------
# Decile monotonicity: does the signal still order the outcome at this horizon?
# ---------------------------------------------------------------------------------------------
def monotonicity_record(view: Prepared) -> dict:
    table = decile_table(view)
    index = table["decile"].to_numpy(dtype="float64")
    record = {
        "signal": view.signal,
        "horizon_s": view.horizon_s,
        "population": view.population,
        "rows": view.rows,
    }
    for label in ("raw", "demeaned"):
        means = table[f"{label}_mean_ticks"].to_numpy(dtype="float64")
        order = pd.Series(means).rank().to_numpy()
        record[f"{label}_spearman"] = float(
            np.corrcoef(index, order)[0, 1]
        )
        steps = np.diff(means)
        record[f"{label}_monotone_increasing"] = bool(np.all(steps > 0))
        record[f"{label}_ascending_steps"] = int((steps > 0).sum())
        record[f"{label}_step_count"] = int(steps.size)
    return record
