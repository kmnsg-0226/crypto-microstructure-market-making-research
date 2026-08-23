"""Descriptive diagnostics: level lifetimes, depletion reconciliation, tail concentration.

Everything here is in-sample and descriptive by design. No threshold is chosen and no state is
selected; the point is to characterise the mechanism before any model is fitted.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pyresearch.native.queue_tail import spec

QUANTILES = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)


def bucket(values: pd.Series, buckets: int) -> pd.Series:
    try:
        return pd.qcut(values, buckets, labels=False, duplicates="drop")
    except ValueError:
        return pd.Series(np.nan, index=values.index)


# --------------------------------------------------------------------------------------------
# Level lifecycle
# --------------------------------------------------------------------------------------------
def level_survival_summary(episodes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    groups: list[tuple[str, pd.DataFrame]] = [("all", episodes)]
    for side, block in episodes.groupby("side", sort=True):
        groups.append((f"side={'bid' if side == 0 else 'ask'}", block))
    for reason, block in episodes.groupby("close_reason", sort=True):
        groups.append((f"close_reason={reason}", block))
    for name, block in groups:
        duration = block["duration_ms"].to_numpy(dtype="float64")
        record = {
            "population": name,
            "episodes": len(block),
            "mean_duration_ms": float(duration.mean()),
            "fully_removed_rate": float(block["fully_removed"].mean()),
            "replenished_rate": float((block["replenish_events"] > 0).mean()),
            "mean_replenish_events": float(block["replenish_events"].mean()),
            "mean_prints_at_quote": float(block["prints_at_quote"].mean()),
            "mean_prints_through": float(block["prints_through"].mean()),
            "median_initial_qty": float(block["initial_qty"].median()),
            "median_unexplained_removal_share": float(
                block["unexplained_removal_share"].median()
            ),
        }
        for level, value in zip(QUANTILES, np.quantile(duration, QUANTILES), strict=True):
            record[f"duration_p{level * 100:g}_ms"] = float(value)
        rows.append(record)
    return pd.DataFrame(rows)


def depletion_replenishment_summary(episodes: pd.DataFrame) -> pd.DataFrame:
    """How best-price levels actually lose their displayed quantity."""
    rows = []
    for name, block in [("all", episodes)] + [
        (f"side={'bid' if side == 0 else 'ask'}", part)
        for side, part in episodes.groupby("side", sort=True)
    ] + [
        (f"close_reason={reason}", part)
        for reason, part in episodes.groupby("close_reason", sort=True)
    ]:
        removed = block["cum_remove"].sum()
        rows.append(
            {
                "population": name,
                "episodes": len(block),
                "total_removed_lots": int(removed),
                "total_added_lots": int(block["cum_add"].sum()),
                "total_replenished_lots": int(block["cum_replenish"].sum()),
                "print_explained_lots": int(block["cum_explained_remove"].sum()),
                "unexplained_lots": int(block["cum_unexplained_remove"].sum()),
                # The single number the whole queue-position question turns on: how much of the
                # displayed depletion at the touch the aggressive-trade stream can account for.
                "print_explained_share": float(block["cum_explained_remove"].sum() / removed)
                if removed
                else np.nan,
                "replenishment_over_removal": float(block["cum_replenish"].sum() / removed)
                if removed
                else np.nan,
                "traded_at_quote_lots": int(block["cum_trade_at_quote"].sum()),
                "traded_through_lots": int(block["cum_trade_through"].sum()),
                "episodes_with_any_print": float((block["prints_at_quote"] > 0).mean()),
                "episodes_with_any_through": float((block["prints_through"] > 0).mean()),
            }
        )
    return pd.DataFrame(rows)


def hazard_curve(frame: pd.DataFrame, buckets: int = 12) -> pd.DataFrame:
    """Discrete-time hazard: P(level ends in the next 100 ms | it survived to age t).

    Fitted as a table, not a parametric model. The question it answers is whether age itself
    carries information once the level is known to have survived that long.
    """
    work = frame[np.isfinite(frame["level_disappears_100ms"].to_numpy())].copy()
    work["age_bucket"] = bucket(work["log_level_age_ms"], buckets)
    work = work.dropna(subset=["age_bucket"])
    grouped = work.groupby(["side", "age_bucket"], observed=True)
    table = grouped.agg(
        observations=("level_disappears_100ms", "size"),
        mean_age_ms=("log_level_age_ms", lambda values: float(np.expm1(values.mean()))),
        hazard_100ms=("level_disappears_100ms", "mean"),
        hazard_250ms=("level_disappears_250ms", "mean"),
        hazard_1000ms=("level_disappears_1000ms", "mean"),
        sweep_100ms=("trade_through_within_100ms", "mean"),
        sweep_1000ms=("trade_through_within_1000ms", "mean"),
        mean_relative_remaining=("relative_remaining_qty", "mean"),
        mean_replenish_events=("log_replenish_events", "mean"),
    ).reset_index()
    return table


# --------------------------------------------------------------------------------------------
# Tail
# --------------------------------------------------------------------------------------------
def tail_distribution(fills: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for key, block in fills.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        base = dict(zip(keys, values, strict=True))
        filled = block[block["filled"]]
        base["eligible_opportunities"] = len(block)
        base["filled"] = len(filled)
        base["fill_rate"] = len(filled) / len(block) if len(block) else np.nan
        for horizon in spec.MARKOUT_HORIZONS_MS:
            column = f"markout_{horizon}ms_ticks"
            observed = filled[column].dropna().to_numpy(dtype="float64")
            prefix = f"markout_{horizon}ms"
            base[f"{prefix}_observed"] = int(observed.size)
            if observed.size == 0:
                continue
            base[f"{prefix}_mean"] = float(observed.mean())
            base[f"{prefix}_median"] = float(np.median(observed))
            base[f"{prefix}_std"] = float(observed.std(ddof=1))
            for level, value in zip(
                QUANTILES, np.quantile(observed, QUANTILES), strict=True
            ):
                base[f"{prefix}_p{level * 100:g}"] = float(value)
            base[f"{prefix}_frac_favourable"] = float((observed > 0).mean())
        primary = filled[f"markout_{spec.PRIMARY_MARKOUT_MS}ms_ticks"].dropna().to_numpy()
        for threshold in spec.CATASTROPHIC_THRESHOLDS_TICKS:
            base[f"catastrophic_{threshold}_rate"] = (
                float((primary <= -threshold).mean()) if primary.size else np.nan
            )
        rows.append(base)
    return pd.DataFrame(rows)


def tail_contribution(fills: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """How much of the total adverse markout the very worst fills account for."""
    rows = []
    for key, block in fills.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        base = dict(zip(keys, values, strict=True))
        markout = block[f"markout_{spec.PRIMARY_MARKOUT_MS}ms_ticks"].dropna().to_numpy(
            dtype="float64"
        )
        if markout.size < 100:
            continue
        ordered = np.sort(markout)
        negative = ordered[ordered < 0]
        total_negative = float(negative.sum())
        base["observed_fills"] = int(markout.size)
        base["total_markout_ticks"] = float(markout.sum())
        base["total_negative_ticks"] = total_negative
        base["total_positive_ticks"] = float(ordered[ordered > 0].sum())
        for share in (0.01, 0.05, 0.10):
            count = max(1, int(round(markout.size * share)))
            worst = ordered[:count]
            base[f"worst_{share * 100:g}pct_count"] = int(count)
            base[f"worst_{share * 100:g}pct_mean_ticks"] = float(worst.mean())
            base[f"worst_{share * 100:g}pct_share_of_negative"] = (
                float(worst.sum() / total_negative) if total_negative else np.nan
            )
        remainder = ordered[max(1, int(round(markout.size * 0.10))) :]
        base["best_90pct_mean_ticks"] = float(remainder.mean())
        base["best_90pct_total_ticks"] = float(remainder.sum())
        rows.append(base)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------------
# Bucket and interaction studies
# --------------------------------------------------------------------------------------------
def bucket_studies(
    frame: pd.DataFrame, signals: list[str], outcomes: dict[str, str], buckets: int
) -> pd.DataFrame:
    rows = []
    for signal in signals:
        if signal not in frame.columns:
            continue
        labels = bucket(frame[signal], buckets)
        work = frame.assign(_bucket=labels).dropna(subset=["_bucket"])
        if work.empty:
            continue
        aggregation = {
            "observations": (signal, "size"),
            "signal_mean": (signal, "mean"),
        }
        for name, column in outcomes.items():
            if column in work.columns:
                aggregation[name] = (column, "mean")
        table = work.groupby("_bucket", observed=True).agg(**aggregation).reset_index()
        table = table.rename(columns={"_bucket": "bucket"})
        table.insert(0, "signal", signal)
        rows.append(table)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def interaction_studies(
    frame: pd.DataFrame,
    pairs: list[tuple[str, str]],
    outcomes: dict[str, str],
    buckets: int,
) -> pd.DataFrame:
    rows = []
    for first, second in pairs:
        if first not in frame.columns or second not in frame.columns:
            continue
        work = frame.assign(
            _a=bucket(frame[first], buckets), _b=bucket(frame[second], buckets)
        ).dropna(subset=["_a", "_b"])
        if work.empty:
            continue
        aggregation = {"observations": (first, "size")}
        for name, column in outcomes.items():
            if column in work.columns:
                aggregation[name] = (column, "mean")
        table = (
            work.groupby(["_a", "_b"], observed=True).agg(**aggregation).reset_index()
        )
        table = table.rename(columns={"_a": "bucket_a", "_b": "bucket_b"})
        table.insert(0, "signal_b", second)
        table.insert(0, "signal_a", first)
        rows.append(table)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def describe_features(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    rows = []
    for name in columns:
        if name not in frame.columns:
            continue
        values = frame[name].to_numpy(dtype="float64")
        finite = values[np.isfinite(values)]
        record = {
            "feature": name,
            "rows": len(values),
            "observed": len(finite),
            "missing": len(values) - len(finite),
            "mean": float(finite.mean()) if finite.size else np.nan,
            "std": float(finite.std(ddof=1)) if finite.size > 1 else np.nan,
            "min": float(finite.min()) if finite.size else np.nan,
            "max": float(finite.max()) if finite.size else np.nan,
        }
        quantiles = (
            np.quantile(finite, QUANTILES) if finite.size else [np.nan] * len(QUANTILES)
        )
        for level, value in zip(QUANTILES, quantiles, strict=True):
            record[f"p{level * 100:g}"] = float(value)
        rows.append(record)
    return pd.DataFrame(rows)
