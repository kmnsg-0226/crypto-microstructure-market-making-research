"""Population statistics for the maker feasibility decomposition.

Every function here reports its own denominator. A markout mean is never returned without the
eligible, filled and observed counts it was conditioned on, because the whole point of the phase
is the size of a gap, and a gap measured on a silently shrinking population is not a gap.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pyresearch.native.economic import spec


def _quantile_names() -> list[str]:
    return [f"p{q * 100:g}" for q in spec.QUANTILES]


def population_stats(frame: pd.DataFrame) -> dict:
    """Fill, mechanism and markout statistics for one population of placements."""
    eligible = len(frame)
    filled = frame["filled"].to_numpy(dtype=bool)
    mechanism = frame["mechanism"].to_numpy()
    record: dict[str, float] = {
        "eligible_opportunities": eligible,
        "filled": int(filled.sum()),
        "fill_rate_30000ms": float(filled.mean()) if eligible else np.nan,
        "at_quote_fills": int((mechanism == 1).sum()),
        "trade_through_fills": int((mechanism == 2).sum()),
    }
    fills = record["filled"]
    record["at_quote_rate_given_fill"] = (
        record["at_quote_fills"] / fills if fills else np.nan
    )
    record["trade_through_rate_given_fill"] = (
        record["trade_through_fills"] / fills if fills else np.nan
    )
    for horizon in spec.FILL_HORIZONS_MS:
        flags = frame[f"fill_{horizon}ms"].to_numpy(dtype="float64")
        evaluable = np.isfinite(flags)
        record[f"fill_evaluable_{horizon}ms"] = int(evaluable.sum())
        record[f"fill_rate_{horizon}ms"] = (
            float(np.nanmean(flags)) if evaluable.any() else np.nan
        )
    latency = frame["time_to_fill_ms"].to_numpy(dtype="float64")
    observed_latency = latency[np.isfinite(latency)]
    record["median_time_to_fill_ms"] = (
        float(np.median(observed_latency)) if observed_latency.size else np.nan
    )

    quote_usd = frame["quote_px_usd"].to_numpy(dtype="float64")
    for horizon in spec.MARKOUT_HORIZONS_MS:
        values = frame[f"markout_{horizon}ms_ticks"].to_numpy(dtype="float64")
        observed = np.isfinite(values)
        seen = values[observed]
        prefix = f"markout_{horizon}ms"
        record[f"{prefix}_observed"] = int(observed.sum())
        record[f"{prefix}_censored"] = int(fills - observed.sum())
        record[f"{prefix}_mean_ticks"] = float(seen.mean()) if seen.size else np.nan
        record[f"{prefix}_median_ticks"] = float(np.median(seen)) if seen.size else np.nan
        quantiles = (
            np.quantile(seen, spec.QUANTILES)
            if seen.size
            else [np.nan] * len(spec.QUANTILES)
        )
        for name, value in zip(_quantile_names(), quantiles, strict=True):
            record[f"{prefix}_{name}_ticks"] = float(value)
        record[f"{prefix}_frac_favourable"] = float((seen > 0).mean()) if seen.size else np.nan
        record[f"{prefix}_frac_non_negative"] = (
            float((seen >= 0).mean()) if seen.size else np.nan
        )
        # The benefit that would have to be earned elsewhere to neutralise the observed markout.
        # No fee, rebate, spread capture or inventory assumption is applied anywhere.
        record[f"required_benefit_{horizon}ms_ticks"] = (
            float(-seen.mean()) if seen.size else np.nan
        )
        record[f"required_benefit_{horizon}ms_usd_per_btc"] = (
            float(-seen.mean() * spec.TICK_SIZE_USD) if seen.size else np.nan
        )
        # Basis points are formed per observation against that observation's own quote before
        # averaging, so a three-day price trend cannot distort the scale.
        if seen.size:
            bps = -values[observed] * spec.TICK_SIZE_USD / quote_usd[observed] * 1e4
            record[f"required_benefit_{horizon}ms_bps"] = float(bps.mean())
        else:
            record[f"required_benefit_{horizon}ms_bps"] = np.nan

    spreads = frame["spread_ticks"].to_numpy(dtype="float64")
    observed_spread = spreads[np.isfinite(spreads) & (spreads > 0)]
    record["median_spread_ticks"] = (
        float(np.median(observed_spread)) if observed_spread.size else np.nan
    )
    record["mean_spread_ticks"] = (
        float(observed_spread.mean()) if observed_spread.size else np.nan
    )
    primary = record[f"required_benefit_{spec.PRIMARY_HORIZON_MS}ms_ticks"]
    half_spread = record["mean_spread_ticks"] / 2.0 if record["mean_spread_ticks"] else np.nan
    record["required_benefit_1s_in_half_spreads"] = (
        primary / half_spread if half_spread else np.nan
    )
    record["required_benefit_1s_in_full_spreads"] = (
        primary / record["mean_spread_ticks"] if record["mean_spread_ticks"] else np.nan
    )
    return record


def grouped_stats(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for key, block in frame.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        record = dict(zip(keys, values, strict=True))
        record.update(population_stats(block))
        rows.append(record)
    return pd.DataFrame(rows)


def mechanism_decomposition(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Split E[M | fill] into its trade-through and at-quote parts and check it reconstructs."""
    rows = []
    for key, block in frame.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        base = dict(zip(keys, values, strict=True))
        for horizon in spec.MARKOUT_HORIZONS_MS:
            markout = block[f"markout_{horizon}ms_ticks"].to_numpy(dtype="float64")
            mechanism = block["mechanism"].to_numpy()
            observed = np.isfinite(markout)
            total = int(observed.sum())
            if total == 0:
                continue
            through = observed & (mechanism == 2)
            at_quote = observed & (mechanism == 1)
            p_through = through.sum() / total
            p_at_quote = at_quote.sum() / total
            e_through = float(markout[through].mean()) if through.any() else np.nan
            e_at_quote = float(markout[at_quote].mean()) if at_quote.any() else np.nan
            total_mean = float(markout[observed].mean())
            reconstructed = (
                (p_through * (e_through if through.any() else 0.0))
                + (p_at_quote * (e_at_quote if at_quote.any() else 0.0))
            )
            rows.append(
                {
                    **base,
                    "horizon_ms": horizon,
                    "observed_fills": total,
                    "p_trade_through_given_fill": p_through,
                    "p_at_quote_given_fill": p_at_quote,
                    "mean_markout_trade_through_ticks": e_through,
                    "mean_markout_at_quote_ticks": e_at_quote,
                    "mean_markout_total_ticks": total_mean,
                    "reconstructed_mean_ticks": reconstructed,
                    "reconstruction_error_ticks": reconstructed - total_mean,
                    "required_benefit_ticks": -total_mean,
                }
            )
    return pd.DataFrame(rows)


def markout_path(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Mean and median markout at each horizon, on the same filled population per row."""
    rows = []
    for key, block in frame.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        base = dict(zip(keys, values, strict=True))
        for horizon in spec.MARKOUT_HORIZONS_MS:
            values_h = block[f"markout_{horizon}ms_ticks"].to_numpy(dtype="float64")
            seen = values_h[np.isfinite(values_h)]
            if seen.size == 0:
                continue
            rows.append(
                {
                    **base,
                    "horizon_ms": horizon,
                    "observed": int(seen.size),
                    "mean_ticks": float(seen.mean()),
                    "median_ticks": float(np.median(seen)),
                    "p25_ticks": float(np.quantile(seen, 0.25)),
                    "p75_ticks": float(np.quantile(seen, 0.75)),
                    "frac_favourable": float((seen > 0).mean()),
                }
            )
    return pd.DataFrame(rows)


def block_bootstrap_mean(
    values: np.ndarray,
    times_ns: np.ndarray,
    seed: int = spec.SEED,
    draws: int = spec.BOOTSTRAP_DRAWS,
) -> tuple[float, float, float]:
    """Resample whole 30-minute blocks; neighbouring placements are not independent draws."""
    finite = np.isfinite(values)
    values = values[finite]
    times_ns = times_ns[finite]
    if values.size < 2:
        return (float("nan"),) * 3
    width = int(spec.BOOTSTRAP_BLOCK_MINUTES * 60 * 1e9)
    block = times_ns // width
    frame = pd.DataFrame({"block": block, "value": values})
    grouped = frame.groupby("block")["value"].agg(["sum", "size"])
    sums = grouped["sum"].to_numpy()
    sizes = grouped["size"].to_numpy(dtype="float64")
    if sums.size < 2:
        return (float(values.mean()), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = np.empty(draws)
    count = sums.size
    for draw in range(draws):
        index = rng.integers(0, count, count)
        means[draw] = sums[index].sum() / sizes[index].sum()
    return (
        float(values.mean()),
        float(np.quantile(means, 0.05)),
        float(np.quantile(means, 0.95)),
    )


def bucket(values: pd.Series, buckets: int) -> pd.Series:
    """Fixed quantile buckets; returns NaN where the signal is missing."""
    try:
        return pd.qcut(values, buckets, labels=False, duplicates="drop")
    except ValueError:
        return pd.Series(np.nan, index=values.index)
