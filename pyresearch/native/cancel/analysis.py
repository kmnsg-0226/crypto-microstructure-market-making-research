"""Turn counterfactual order paths into the falsification tables.

Every table keeps the complete baseline cohort in its denominator. A cancelled order is never
counted as a zero-markout fill and never quietly dropped, because the whole point of the phase is
to see what the intervention gave up as well as what it avoided.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pyresearch.native.cancel import spec
from pyresearch.native.predictive import modeling

QUANTILES = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90)
PRIMARY = f"markout_{spec.PRIMARY_MARKOUT_MS}ms_ticks"


def block_id(timestamps: np.ndarray) -> np.ndarray:
    """The 30-minute chronological blocks used for dependence-aware reporting since phase 2."""
    return (timestamps // int(spec.BOOTSTRAP_BLOCK_MINUTES * 60 * 1e9)).astype("int64")


def utc_day(timestamps: np.ndarray) -> np.ndarray:
    return pd.to_datetime(timestamps, unit="ns", utc=True).strftime("%Y-%m-%d")


def _markout_stats(values: np.ndarray, prefix: str) -> dict:
    observed = values[np.isfinite(values)]
    out = {f"{prefix}_observed": int(observed.size)}
    if observed.size == 0:
        return out
    out[f"{prefix}_mean"] = float(observed.mean())
    out[f"{prefix}_median"] = float(np.median(observed))
    for level, value in zip(QUANTILES, np.quantile(observed, QUANTILES), strict=True):
        out[f"{prefix}_p{level * 100:g}"] = float(value)
    out[f"{prefix}_frac_favourable"] = float((observed > 0).mean())
    for threshold in spec.CATASTROPHIC_THRESHOLDS_TICKS:
        out[f"{prefix}_cat_{threshold}_rate"] = float((observed <= -threshold).mean())
    return out


def _ratio(numerator: float, denominator: float) -> float:
    """Undefined rather than infinite when nothing was sacrificed."""
    return float(numerator / denominator) if denominator > 0 else np.nan


def cell_metrics(frame: pd.DataFrame) -> dict:
    """Every falsification number for one cohort under one (threshold, latency) cell."""
    eligible = len(frame)
    baseline_filled = frame["filled"].to_numpy(dtype=bool)
    markout = frame[PRIMARY].to_numpy(dtype="float64")
    observed = np.isfinite(markout)
    prevented = frame["fill_prevented"].to_numpy(dtype=bool)
    cancelled = frame["cancel_signalled"].to_numpy(dtype=bool)
    surviving = frame["surviving_fill"].to_numpy(dtype=bool)

    baseline_negative = float(-np.minimum(markout[observed], 0.0).sum())
    baseline_positive = float(np.maximum(markout[observed], 0.0).sum())
    adverse = float(frame["adverse_ticks_avoided"].sum())
    sacrificed = float(frame["favourable_ticks_sacrificed"].sum())

    row = {
        # Denominators, retained in every table.
        "eligible_opportunities": eligible,
        "baseline_fills": int(baseline_filled.sum()),
        "baseline_fill_rate": _ratio(float(baseline_filled.sum()), eligible),
        "baseline_unfilled": int((~baseline_filled).sum()),
        "cancelled_opportunities": int(cancelled.sum()),
        "cancel_rate": _ratio(float(cancelled.sum()), eligible),
        "orders_unaffected": int(frame["order_unaffected"].sum()),
        "fills_prevented": int(prevented.sum()),
        "surviving_fills": int(surviving.sum()),
        "surviving_fill_rate": _ratio(float(surviving.sum()), eligible),
        "fill_rate_reduction": _ratio(float(baseline_filled.sum()), eligible)
        - _ratio(float(surviving.sum()), eligible),
        # What the cancellation did.
        "adverse_fills_avoided": int(frame["adverse_fill_avoided"].sum()),
        "favourable_fills_sacrificed": int(frame["favourable_fill_sacrificed"].sum()),
        "non_negative_fills_sacrificed": int(frame["non_negative_fill_sacrificed"].sum()),
        "prevented_fills_markout_censored": int(
            frame["prevented_fill_markout_censored"].sum()
        ),
        "cancel_too_late": int(frame["cancel_too_late"].sum()),
        "cancel_too_late_rate": _ratio(
            float(frame["cancel_too_late"].sum()),
            float((cancelled & baseline_filled).sum()),
        ),
        "cancel_without_baseline_fill": int(frame["cancel_without_baseline_fill"].sum()),
        "unnecessary_cancel_rate": _ratio(
            float(frame["cancel_without_baseline_fill"].sum()), float(cancelled.sum())
        ),
        # Economics, in ticks of quote-relative markout only.
        "total_baseline_negative_ticks": baseline_negative,
        "total_baseline_positive_ticks": baseline_positive,
        "adverse_markout_avoided": adverse,
        "favourable_markout_sacrificed": sacrificed,
        "net_markout_preserved": adverse - sacrificed,
        "net_per_eligible_opportunity": _ratio(adverse - sacrificed, eligible),
        "net_per_baseline_fill": _ratio(adverse - sacrificed, float(baseline_filled.sum())),
        "net_per_cancelled_order": _ratio(adverse - sacrificed, float(cancelled.sum())),
        "avoidance_efficiency_ticks": _ratio(adverse, sacrificed),
        "fraction_baseline_negative_removed": _ratio(adverse, baseline_negative),
        "fraction_baseline_positive_removed": _ratio(sacrificed, baseline_positive),
    }
    # A size-matched non-informative benchmark. The baseline cohort is dominated by adverse
    # markout, so removing fills at random already removes far more negative than positive
    # ticks; without this comparison a large avoidance ratio says nothing about the signal. The
    # benchmark removes the same number of baseline fills, chosen without information, and
    # therefore takes a proportional share of both sides of the baseline distribution.
    share = _ratio(float(prevented.sum()), float(baseline_filled.sum()))
    random_adverse = baseline_negative * share
    random_sacrificed = baseline_positive * share
    row["prevented_share_of_baseline_fills"] = share
    row["baseline_negative_to_positive_ratio"] = _ratio(baseline_negative, baseline_positive)
    row["random_match_adverse_avoided"] = random_adverse
    row["random_match_favourable_sacrificed"] = random_sacrificed
    row["random_match_net_markout"] = random_adverse - random_sacrificed
    row["net_markout_lift_over_random"] = _ratio(
        adverse - sacrificed, random_adverse - random_sacrificed
    )
    row["avoidance_efficiency_lift"] = _ratio(
        row["avoidance_efficiency_ticks"], row["baseline_negative_to_positive_ratio"]
    )
    for threshold in spec.CATASTROPHIC_THRESHOLDS_TICKS:
        avoided = int(frame[f"catastrophic_{threshold}_avoided"].sum())
        baseline_count = float(
            (observed & baseline_filled & (markout <= -threshold)).sum()
        )
        row[f"catastrophic_{threshold}_avoided"] = avoided
        row[f"catastrophic_{threshold}_avoidance_efficiency"] = _ratio(
            float(avoided), float(frame["favourable_fill_sacrificed"].sum())
        )
        row[f"catastrophic_{threshold}_random_match"] = baseline_count * share
        row[f"catastrophic_{threshold}_avoidance_lift"] = _ratio(
            float(avoided), baseline_count * share
        )
    row.update(_markout_stats(np.where(observed & baseline_filled, markout, np.nan), "baseline"))
    row.update(_markout_stats(np.where(observed & surviving, markout, np.nan), "surviving"))
    # Catastrophic exposure per eligible opportunity, so a fall cannot be manufactured simply by
    # having fewer fills left in the denominator.
    for threshold in spec.CATASTROPHIC_THRESHOLDS_TICKS:
        base = observed & baseline_filled & (markout <= -threshold)
        stay = observed & surviving & (markout <= -threshold)
        row[f"baseline_cat_{threshold}_per_opportunity"] = _ratio(float(base.sum()), eligible)
        row[f"surviving_cat_{threshold}_per_opportunity"] = _ratio(float(stay.sum()), eligible)
    return row


def baseline_metrics(frame: pd.DataFrame) -> dict:
    """The never-cancel reference: the same cohort with the intervention switched off."""
    zeros = frame.assign(
        cancel_signalled=False,
        fill_prevented=False,
        cancel_too_late=False,
        cancel_without_baseline_fill=False,
        order_unaffected=True,
        surviving_fill=frame["filled"],
        adverse_fill_avoided=False,
        favourable_fill_sacrificed=False,
        non_negative_fill_sacrificed=False,
        prevented_fill_markout_censored=False,
        adverse_ticks_avoided=0.0,
        favourable_ticks_sacrificed=0.0,
    )
    for threshold in spec.CATASTROPHIC_THRESHOLDS_TICKS:
        zeros[f"catastrophic_{threshold}_avoided"] = False
    return cell_metrics(zeros)


def grouped_metrics(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for key, block in frame.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        rows.append({**dict(zip(keys, values, strict=True)), **cell_metrics(block)})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------------
# Score deciles
# --------------------------------------------------------------------------------------------
def score_deciles(fills: pd.DataFrame) -> pd.DataFrame:
    """Descriptive only: does higher observable sweep risk mean worse passive economics?

    The decile edges come from the score distribution of resting-order states, never from any
    outcome. This must not be turned into an entry rule; it is here to show whether the ordering
    the classifier produces is the ordering the economics actually follow.
    """
    rows = []
    for cell, block in fills.groupby("queue_cell", sort=True):
        scores = block["sweep_p_at_placement"].to_numpy(dtype="float64")
        usable = np.isfinite(scores)
        decile = pd.Series(np.nan, index=block.index)
        decile[usable] = pd.qcut(
            scores[usable], spec.SCORE_DECILES, labels=False, duplicates="drop"
        )
        block = block.assign(decile=decile).dropna(subset=["decile"])
        for value, part in block.groupby("decile", sort=True):
            markout = part[PRIMARY].to_numpy(dtype="float64")
            observed = np.isfinite(markout)
            record = {
                "queue_cell": cell,
                "decile": int(value),
                "state_observations": len(part),
                "mean_sweep_p": float(part["sweep_p_at_placement"].mean()),
                "baseline_fill_probability": float(part["filled"].mean()),
                "trade_through_probability": float(
                    part["realised_trade_through"].astype("float64").mean()
                ),
            }
            record.update(_markout_stats(np.where(observed, markout, np.nan), "markout"))
            # Also per opportunity, so a decile cannot look safe merely by filling less often.
            for ticks in spec.CATASTROPHIC_THRESHOLDS_TICKS:
                record[f"cat_{ticks}_per_opportunity"] = float(
                    (observed & (markout <= -ticks)).mean()
                )
            rows.append(record)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------------
# Lead time
# --------------------------------------------------------------------------------------------
def lead_time(frame: pd.DataFrame, population: str) -> dict:
    """How long before the fill the signal first crossed, for one fill population."""
    lead = frame["lead_time_ms"].to_numpy(dtype="float64")
    lead = lead[np.isfinite(lead)]
    record = {
        "population": population,
        "fills": int(len(frame)),
        "fills_with_a_crossing": int(lead.size),
        "crossing_rate": _ratio(float(lead.size), float(len(frame))),
    }
    if lead.size == 0:
        return record
    for level, value in zip(QUANTILES, np.quantile(lead, QUANTILES), strict=True):
        record[f"lead_p{level * 100:g}_ms"] = float(value)
    record["lead_mean_ms"] = float(lead.mean())
    edges = spec.LEAD_TIME_BINS_MS
    record["frac_lead_le_0ms"] = float((lead <= 0).mean())
    for low, high in zip(edges[:-1], edges[1:], strict=False):
        record[f"frac_lead_{low:g}_{high:g}ms"] = float(((lead > low) & (lead <= high)).mean())
    for edge in (100.0, 250.0, 500.0):
        record[f"frac_lead_gt_{edge:g}ms"] = float((lead > edge).mean())
    return record


def block_summary(table: pd.DataFrame, keys: list[str], column: str) -> pd.DataFrame:
    """Mean, median, worst and the share of blocks with the expected sign, block bootstrapped."""
    rows = []
    for key, block in table.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        series = block[column].to_numpy(dtype="float64")
        weights = block["eligible_opportunities"].to_numpy(dtype="float64")
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
    """Is the structural ordering across the fixed grid monotone, and in which direction?"""
    rows = []
    for key, block in table.groupby(keys, sort=True, observed=True):
        values = key if isinstance(key, tuple) else (key,)
        block = block.sort_values(order)
        series = block[column].to_numpy(dtype="float64")
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
                "frac_steps_up": float((finite > 0).mean()) if finite.size else np.nan,
                "spearman": float(
                    pd.Series(series).corr(pd.Series(np.arange(len(series))), method="spearman")
                )
                if len(series) > 2
                else np.nan,
            }
        )
    return pd.DataFrame(rows)
