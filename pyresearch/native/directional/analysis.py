"""Descriptive diagnostics for the directional sweep phase.

Every headline table keeps the full decision-time denominator. Quantities that condition on a
sweep having actually happened are computed by separate functions with names that say so, and
are never mixed into an unconditional average.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pyresearch.native.directional import spec
from pyresearch.native.predictive import modeling

QUANTILES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)


def block_id(timestamps: np.ndarray) -> np.ndarray:
    return (timestamps // int(spec.BOOTSTRAP_BLOCK_MINUTES * 60 * 1e9)).astype("int64")


def utc_day(timestamps: np.ndarray) -> np.ndarray:
    return pd.to_datetime(timestamps, unit="ns", utc=True).strftime("%Y-%m-%d")


def _ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator > 0 else np.nan


def markout_stats(values: np.ndarray, mid: np.ndarray, prefix: str) -> dict:
    """Distribution of a directional markout in ticks, with the basis-point conversion."""
    finite = np.isfinite(values)
    observed = values[finite]
    out = {f"{prefix}_observed": int(observed.size)}
    if observed.size == 0:
        return out
    mean = float(observed.mean())
    out[f"{prefix}_mean_ticks"] = mean
    out[f"{prefix}_median_ticks"] = float(np.median(observed))
    out[f"{prefix}_std_ticks"] = float(observed.std(ddof=1)) if observed.size > 1 else np.nan
    for level, value in zip(QUANTILES, np.quantile(observed, QUANTILES), strict=True):
        out[f"{prefix}_p{level * 100:g}_ticks"] = float(value)
    out[f"{prefix}_frac_favourable"] = float((observed > 0).mean())
    for ticks in spec.TICK_THRESHOLDS:
        out[f"{prefix}_frac_ge_{ticks}t"] = float((observed >= ticks).mean())
    # Basis points against the mid at each row's own decision instant, never a constant price.
    reference = mid[finite]
    usable = np.isfinite(reference) & (reference > 0)
    if usable.any():
        out[f"{prefix}_mean_bps"] = float(
            np.mean(1e4 * observed[usable] / reference[usable])
        )
        out[f"{prefix}_median_bps"] = float(
            np.median(1e4 * observed[usable] / reference[usable])
        )
        out[f"{prefix}_mean_usd_per_btc"] = mean * spec.TICK_SIZE_USD
    return out


# --------------------------------------------------------------------------------------------
# Decile study
# --------------------------------------------------------------------------------------------
def assign_deciles(scores: np.ndarray) -> np.ndarray:
    out = np.full(scores.shape, np.nan)
    usable = np.isfinite(scores)
    out[usable] = pd.qcut(scores[usable], spec.DECILES, labels=False, duplicates="drop")
    return out


def decile_table(frame: pd.DataFrame, keys: list[str] | None = None) -> pd.DataFrame:
    """Fixed sweep-score deciles against realised sweeps and future directional movement."""
    keys = keys or []
    rows = []
    groups = frame.groupby(keys, sort=True, observed=True) if keys else [((), frame)]
    for key, block in groups:
        values = key if isinstance(key, tuple) else (key,)
        block = block.assign(decile=assign_deciles(block["sweep_p"].to_numpy(dtype="float64")))
        block = block.dropna(subset=["decile"])
        for decile, part in block.groupby("decile", sort=True):
            rows.append(
                {
                    **dict(zip(keys, values, strict=True)),
                    "decile": int(decile),
                    **bucket_metrics(part),
                }
            )
    return pd.DataFrame(rows)


def bucket_metrics(part: pd.DataFrame) -> dict:
    """Everything reported per sweep-score bucket, on the full opportunity denominator."""
    mid = part["mid_ticks"].to_numpy(dtype="float64")
    record = {
        "observations": len(part),
        "mean_sweep_p": float(part["sweep_p"].mean()),
        "min_sweep_p": float(part["sweep_p"].min()),
        "max_sweep_p": float(part["sweep_p"].max()),
    }
    for horizon in (100, 250, 500, 1000):
        column = f"trade_through_within_{horizon}ms"
        if column in part.columns:
            record[f"realised_sweep_rate_{horizon}ms"] = float(
                np.nanmean(part[column].to_numpy(dtype="float64"))
            )
    if "next_move_matches_sweep_direction" in part.columns:
        matches = part["next_move_matches_sweep_direction"].to_numpy(dtype="float64")
        record["p_next_move_follows_sweep"] = float(np.nanmean(matches))
        record["next_move_observed"] = int(np.isfinite(matches).sum())
    if "time_to_next_mid_move_ms" in part.columns:
        timing = part["time_to_next_mid_move_ms"].to_numpy(dtype="float64")
        finite_timing = timing[np.isfinite(timing)]
        if finite_timing.size:
            record["time_to_next_mid_move_median_ms"] = float(np.median(finite_timing))
            record["time_to_next_mid_move_p25_ms"] = float(np.quantile(finite_timing, 0.25))
            record["time_to_next_mid_move_p75_ms"] = float(np.quantile(finite_timing, 0.75))
    for horizon in spec.HEADLINE_HORIZONS_MS:
        column = f"mid_moves_within_{horizon}ms"
        if column in part.columns:
            record[f"p_mid_moves_within_{horizon}ms"] = float(
                np.nanmean(part[column].to_numpy(dtype="float64"))
            )
    if "signed_ticks_on_first_mid_move" in part.columns:
        record.update(
            markout_stats(
                part["signed_ticks_on_first_mid_move"].to_numpy(dtype="float64"),
                mid,
                "first_move",
            )
        )
    for horizon in spec.ALL_HORIZONS_MS:
        column = f"directional_markout_{horizon}ms"
        if column in part.columns:
            record.update(
                markout_stats(
                    part[column].to_numpy(dtype="float64"), mid, f"markout_{horizon}ms"
                )
            )
    return record


def threshold_table(frame: pd.DataFrame, keys: list[str] | None = None) -> pd.DataFrame:
    """The same metrics at the four pre-registered thresholds, plus the unconditional row."""
    keys = keys or []
    rows = []
    groups = frame.groupby(keys, sort=True, observed=True) if keys else [((), frame)]
    for key, block in groups:
        values = key if isinstance(key, tuple) else (key,)
        base = dict(zip(keys, values, strict=True))
        rows.append({**base, "threshold": np.nan, "population": "all", **bucket_metrics(block)})
        for threshold in spec.SWEEP_THRESHOLDS:
            part = block[block["sweep_p"] >= threshold]
            if part.empty:
                continue
            rows.append(
                {
                    **base,
                    "threshold": threshold,
                    "population": "score_at_or_above",
                    "share_of_opportunities": len(part) / len(block),
                    **bucket_metrics(part),
                }
            )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------------
# Probability versus magnitude
# --------------------------------------------------------------------------------------------
def decompose(frame: pd.DataFrame, horizon: int, keys: list[str]) -> pd.DataFrame:
    """E[R] = P(right) * E[size | right] - P(wrong) * E[size | wrong], reported term by term."""
    column = f"directional_markout_{horizon}ms"
    rows = []
    for key, block in frame.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        markout = block[column].to_numpy(dtype="float64")
        observed = np.isfinite(markout)
        markout = markout[observed]
        if markout.size == 0:
            continue
        right = markout > 0
        wrong = markout < 0
        flat = markout == 0
        p_right, p_wrong = float(right.mean()), float(wrong.mean())
        size_right = float(markout[right].mean()) if right.any() else np.nan
        size_wrong = float(-markout[wrong].mean()) if wrong.any() else np.nan
        rows.append(
            {
                **dict(zip(keys, values, strict=True)),
                "horizon_ms": horizon,
                "observations": int(markout.size),
                "p_move_in_predicted_direction": p_right,
                "p_move_opposite": p_wrong,
                "p_no_move": float(flat.mean()),
                "mean_size_when_right_ticks": size_right,
                "mean_size_when_wrong_ticks": size_wrong,
                "contribution_right_ticks": p_right * size_right if right.any() else np.nan,
                "contribution_wrong_ticks": p_wrong * size_wrong if wrong.any() else np.nan,
                "expected_directional_markout_ticks": float(markout.mean()),
                "size_asymmetry_ratio": _ratio(size_right, size_wrong),
                "probability_edge": p_right - p_wrong,
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------------
# Conditional versus unconditional
# --------------------------------------------------------------------------------------------
def conditional_split(frame: pd.DataFrame, horizon: int, keys: list[str]) -> pd.DataFrame:
    """Unconditional, conditional-on-sweep and conditional-on-no-sweep, side by side.

    The unconditional row is the one a decision at time t would actually face. The other two
    exist so the difference between them can be seen, never so the first can be replaced.
    """
    flag = f"trade_through_within_{spec.SWEEP_HORIZON_MS}ms"
    column = f"directional_markout_{horizon}ms"
    rows = []
    for key, block in frame.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        base = dict(zip(keys, values, strict=True))
        realised = block[flag].to_numpy(dtype="float64")
        mid = block["mid_ticks"].to_numpy(dtype="float64")
        markout = block[column].to_numpy(dtype="float64")
        populations = {
            "unconditional": np.ones(len(block), dtype=bool),
            "sweep_occurred": realised > 0,
            "sweep_did_not_occur": realised == 0,
        }
        for name, mask in populations.items():
            rows.append(
                {
                    **base,
                    "horizon_ms": horizon,
                    "population": name,
                    "opportunities": int(mask.sum()),
                    "share_of_opportunities": _ratio(float(mask.sum()), float(len(block))),
                    "realised_sweep_rate": float(np.nanmean(realised)),
                    **markout_stats(np.where(mask, markout, np.nan), mid, "markout"),
                }
            )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------------
# Cost hurdle and break-even
# --------------------------------------------------------------------------------------------
def cost_hurdle(table: pd.DataFrame, keys: list[str], horizon: int) -> pd.DataFrame:
    """Raw directional movement against fixed, clearly labelled execution-cost sensitivities.

    This is a comparison, not a profit and loss statement. No hurdle is ever a model feature and
    no hurdle is chosen; the whole fixed grid is reported.
    """
    prefix = f"markout_{horizon}ms"
    rows = []
    for _, row in table.iterrows():
        mean_bps = row.get(f"{prefix}_mean_bps", np.nan)
        mean_ticks = row.get(f"{prefix}_mean_ticks", np.nan)
        for hurdle in spec.COST_HURDLE_BPS:
            rows.append(
                {
                    **{key: row[key] for key in keys if key in row},
                    "horizon_ms": horizon,
                    "observations": row.get("observations", np.nan),
                    "gross_directional_bps": mean_bps,
                    "gross_directional_ticks": mean_ticks,
                    "one_way_hurdle_bps": hurdle,
                    "net_of_one_way_hurdle_bps": mean_bps - hurdle,
                    "net_of_round_trip_hurdle_bps": mean_bps - 2 * hurdle,
                    "clears_one_way": bool(mean_bps > hurdle),
                    "clears_round_trip": bool(mean_bps > 2 * hurdle),
                }
            )
    return pd.DataFrame(rows)


def break_even(table: pd.DataFrame, keys: list[str], horizon: int) -> pd.DataFrame:
    """The largest all-in execution cost the observed gross movement could absorb."""
    prefix = f"markout_{horizon}ms"
    rows = []
    for _, row in table.iterrows():
        ticks = row.get(f"{prefix}_mean_ticks", np.nan)
        bps = row.get(f"{prefix}_mean_bps", np.nan)
        rows.append(
            {
                **{key: row[key] for key in keys if key in row},
                "horizon_ms": horizon,
                "observations": row.get("observations", np.nan),
                "required_break_even_ticks": ticks,
                "required_break_even_usd_per_btc": ticks * spec.TICK_SIZE_USD,
                "required_break_even_bps_one_way": bps,
                "required_break_even_bps_round_trip": bps / 2.0,
                "magnitude_band": _band(bps),
            }
        )
    return pd.DataFrame(rows)


def _band(bps: float) -> str:
    if not np.isfinite(bps):
        return "undefined"
    if bps < 1.0:
        return "<1bp"
    if bps <= 5.0:
        return "1-5bp"
    return ">5bp"


# --------------------------------------------------------------------------------------------
# Regimes and stability
# --------------------------------------------------------------------------------------------
def regime_table(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Fixed causal quantile buckets of activity, each cut against the sweep thresholds."""
    rows = []
    for name, column in spec.REGIME_SIGNALS.items():
        if column not in frame.columns:
            continue
        values = frame[column].to_numpy(dtype="float64")
        bucket = np.full(len(frame), np.nan)
        usable = np.isfinite(values)
        try:
            bucket[usable] = pd.qcut(
                values[usable], spec.REGIME_BUCKETS, labels=False, duplicates="drop"
            )
        except ValueError:
            continue
        work = frame.assign(regime_bucket=bucket).dropna(subset=["regime_bucket"])
        table = threshold_table(work, ["regime_bucket"])
        table.insert(0, "regime_signal", name)
        table.insert(1, "regime_column", column)
        rows.append(table)
    if not rows:
        return pd.DataFrame()
    combined = pd.concat(rows, ignore_index=True)
    keep = [
        "regime_signal", "regime_column", "regime_bucket", "threshold", "population",
        "observations", "mean_sweep_p", "realised_sweep_rate_500ms",
        "p_next_move_follows_sweep",
    ] + [
        f"markout_{horizon}ms_{stat}"
        for stat in ("observed", "mean_ticks", "median_ticks", "mean_bps", "frac_favourable")
    ]
    return combined[[c for c in keep if c in combined.columns]]


def stability(frame: pd.DataFrame, group: list[str], horizon: int) -> pd.DataFrame:
    table = threshold_table(frame, group)
    keep = group + [
        "threshold", "population", "observations", "share_of_opportunities",
        "mean_sweep_p", "realised_sweep_rate_500ms", "p_next_move_follows_sweep",
    ] + [
        f"markout_{horizon}ms_{stat}"
        for stat in ("observed", "mean_ticks", "median_ticks", "mean_bps", "frac_favourable")
    ]
    return table[[c for c in keep if c in table.columns]]


def block_summary(table: pd.DataFrame, keys: list[str], column: str) -> pd.DataFrame:
    rows = []
    for key, block in table.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        series = block[column].to_numpy(dtype="float64")
        weights = block["observations"].to_numpy(dtype="float64")
        centre, low, high = modeling.block_bootstrap(series, weights)
        finite = series[np.isfinite(series)]
        rows.append(
            {
                **dict(zip(keys, values, strict=True)),
                "statistic": column,
                "blocks": int(finite.size),
                "block_mean": centre,
                "block_median": float(np.median(finite)) if finite.size else np.nan,
                "block_worst": float(finite.min()) if finite.size else np.nan,
                "block_best": float(finite.max()) if finite.size else np.nan,
                "frac_blocks_positive": float((finite > 0).mean()) if finite.size else np.nan,
                "block_bootstrap_p05": low,
                "block_bootstrap_p95": high,
            }
        )
    return pd.DataFrame(rows)


def monotonicity(table: pd.DataFrame, keys: list[str], order: str, column: str) -> pd.DataFrame:
    rows = []
    for key, block in table.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        ordered = block.sort_values(order)
        series = ordered[column].to_numpy(dtype="float64")
        steps = np.diff(series)
        finite = steps[np.isfinite(steps)]
        rows.append(
            {
                **dict(zip(keys, values, strict=True)),
                "ordered_by": order,
                "statistic": column,
                "steps": int(finite.size),
                "non_decreasing": bool(np.all(finite >= 0)) if finite.size else False,
                "non_increasing": bool(np.all(finite <= 0)) if finite.size else False,
                "spearman": float(
                    pd.Series(series).corr(pd.Series(np.arange(len(series))), method="spearman")
                )
                if len(series) > 2
                else np.nan,
            }
        )
    return pd.DataFrame(rows)
