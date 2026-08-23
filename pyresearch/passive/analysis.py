"""Conditional fill and adverse-selection analysis for labeled passive probes."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pyresearch.support.evaluate import write_csv, write_json


HORIZON_NAMES = {100: "100ms", 500: "500ms", 1000: "1s", 5000: "5s"}
SIGNALS = ("obi_l1", "obi_l5", "obi_l10")
RESOLVED_STATUSES = {"full", "partial", "unfilled"}


def hac_mean_se(values: np.ndarray, dates: np.ndarray, max_lag: int) -> float:
    finite = np.isfinite(values)
    values = values[finite]
    dates = dates[finite]
    count = len(values)
    if count < 2:
        return math.nan
    centered = values - values.mean()
    long_run = float(centered @ centered)
    usable_lag = min(max_lag, count - 1)
    for lag in range(1, usable_lag + 1):
        same_day = dates[lag:] == dates[:-lag]
        covariance = float(centered[lag:][same_day] @ centered[:-lag][same_day])
        long_run += 2.0 * (1.0 - lag / (usable_lag + 1.0)) * covariance
    return math.sqrt(max(0.0, long_run) / (count * count))


def _distribution(values: pd.Series) -> dict[str, float | int]:
    clean = values.dropna().to_numpy(dtype="float64")
    if clean.size == 0:
        return {
            "observations": 0,
            "mean": math.nan,
            "median": math.nan,
            "negative_probability": math.nan,
            "p05": math.nan,
            "p25": math.nan,
            "p75": math.nan,
            "p95": math.nan,
        }
    quantiles = np.quantile(clean, [0.05, 0.25, 0.5, 0.75, 0.95])
    return {
        "observations": int(clean.size),
        "mean": float(clean.mean()),
        "median": float(quantiles[2]),
        "negative_probability": float(np.mean(clean < 0)),
        "p05": float(quantiles[0]),
        "p25": float(quantiles[1]),
        "p75": float(quantiles[3]),
        "p95": float(quantiles[4]),
    }


def _fill_rows(frame: pd.DataFrame, date_label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for signal in ("all",) + SIGNALS:
        bin_column = None if signal == "all" else f"{signal}_bin"
        group_columns = ["side", "quote_lifetime_ms"] + ([] if bin_column is None else [bin_column])
        for keys, group in frame.groupby(group_columns, sort=True, observed=True, dropna=False):
            if not isinstance(keys, tuple):
                keys = (keys,)
            side = keys[0]
            lifetime = int(keys[1])
            bin_value = 0 if bin_column is None or pd.isna(keys[2]) else int(keys[2])
            resolved = group["fill_status"].isin(RESOLVED_STATUSES)
            full = group["fill_status"].eq("full")
            partial = group["fill_status"].eq("partial")
            any_fill = full | partial
            fill_latency = group.loc[any_fill, "fill_latency_us"] / 1000.0
            rows.append(
                {
                    "date": date_label,
                    "signal": signal,
                    "signal_bin": bin_value,
                    "side": side,
                    "quote_lifetime_ms": lifetime,
                    "candidate_quotes": int(len(group)),
                    "resolved_quotes": int(resolved.sum()),
                    "full_fills": int(full.sum()),
                    "partial_fills": int(partial.sum()),
                    "invalid_quotes": int((~resolved).sum()),
                    "full_fill_probability": (
                        float(full.sum() / resolved.sum()) if resolved.any() else math.nan
                    ),
                    "any_fill_probability": (
                        float(any_fill.sum() / resolved.sum()) if resolved.any() else math.nan
                    ),
                    "median_fill_time_ms": float(fill_latency.median()) if len(fill_latency) else math.nan,
                    "average_fill_time_ms": float(fill_latency.mean()) if len(fill_latency) else math.nan,
                    "average_queue_ahead_btc": float(group.loc[resolved, "queue_ahead_initial"].mean()),
                    "median_queue_ahead_btc": float(group.loc[resolved, "queue_ahead_initial"].median()),
                    "average_order_to_displayed_ratio": float(
                        group.loc[resolved, "order_size_to_displayed_ratio"].mean()
                    ),
                    "average_same_price_traded_qty_btc": float(
                        group.loc[resolved, "same_price_traded_qty"].mean()
                    ),
                }
            )
    return rows


def _markout_rows(frame: pd.DataFrame, date_label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    day_codes = pd.factorize(frame["date"], sort=True)[0]
    frame = frame.copy()
    frame["_day_code"] = day_codes
    for horizon_ms, horizon_name in HORIZON_NAMES.items():
        markout_column = f"maker_markout_{horizon_name}_ticks"
        post_column = f"post_fill_mid_move_{horizon_name}_ticks"
        for signal in SIGNALS:
            bin_column = f"{signal}_bin"
            for (side, lifetime, signal_bin), group in frame.groupby(
                ["side", "quote_lifetime_ms", bin_column],
                sort=True,
                observed=True,
                dropna=True,
            ):
                stats = _distribution(group[markout_column])
                lag = max(int(lifetime), horizon_ms) // 100
                values = group[markout_column].to_numpy(dtype="float64")
                dates = group["_day_code"].to_numpy(dtype="int64")
                rows.append(
                    {
                        "date": date_label,
                        "signal": signal,
                        "signal_bin": int(signal_bin),
                        "obi_regime": (
                            "negative" if signal_bin <= 3 else
                            "neutral" if signal_bin <= 7 else "positive"
                        ),
                        "side": side,
                        "quote_lifetime_ms": int(lifetime),
                        "markout_horizon_ms": horizon_ms,
                        **{f"maker_markout_{key}": value for key, value in stats.items()},
                        "post_fill_mid_move_mean_ticks": float(group[post_column].mean()),
                        "fill_price_advantage_mean_ticks": float(
                            group["fill_price_advantage_ticks"].mean()
                        ),
                        "hac_mean_standard_error_ticks": hac_mean_se(values, dates, lag),
                        "hac_max_lag_observations": lag,
                    }
                )
    return rows


def _queue_rows(frame: pd.DataFrame, date_label: str) -> list[dict[str, Any]]:
    rows = []
    for horizon_ms, horizon_name in HORIZON_NAMES.items():
        for queue_model, prefix in (
            ("pessimistic_visible_queue", ""),
            ("optimistic_front_of_queue_upper_bound", "optimistic_"),
        ):
            status = f"{prefix}fill_status"
            quantity = f"{prefix}filled_qty"
            markout = f"{prefix}maker_markout_{horizon_name}_ticks"
            for (side, lifetime), group in frame.groupby(
                ["side", "quote_lifetime_ms"], sort=True, observed=True
            ):
                resolved = group[status].isin(RESOLVED_STATUSES)
                full = group[status].eq("full")
                partial = group[status].eq("partial")
                labeled = group[quantity].gt(0) & group[markout].notna()
                stats = _distribution(group.loc[labeled, markout])
                rows.append(
                    {
                        "date": date_label,
                        "queue_model": queue_model,
                        "side": side,
                        "quote_lifetime_ms": int(lifetime),
                        "markout_horizon_ms": horizon_ms,
                        "candidate_quotes": int(len(group)),
                        "resolved_quotes": int(resolved.sum()),
                        "full_fills": int(full.sum()),
                        "partial_fills": int(partial.sum()),
                        "full_fill_probability": (
                            float(full.sum() / resolved.sum()) if resolved.any() else math.nan
                        ),
                        **{f"maker_markout_{key}": value for key, value in stats.items()},
                    }
                )
    return rows


def _filter_rows(
    frame: pd.DataFrame,
    date_label: str,
    bearish_threshold: float,
    bullish_threshold: float,
) -> list[dict[str, Any]]:
    rows = []
    prediction = frame["combined_prediction_1s_ticks"]
    filtered = (
        (frame["side"].eq("bid") & prediction.ge(bearish_threshold))
        | (frame["side"].eq("ask") & prediction.le(bullish_threshold))
    )
    for policy, eligible in (("always_quote", pd.Series(True, index=frame.index)),
                             ("alpha_filtered", filtered)):
        selected = frame.loc[eligible]
        for horizon_ms, horizon_name in HORIZON_NAMES.items():
            markout = f"maker_markout_{horizon_name}_ticks"
            for (side, lifetime), group in selected.groupby(
                ["side", "quote_lifetime_ms"], sort=True, observed=True
            ):
                resolved = group["fill_status"].isin(RESOLVED_STATUSES)
                any_fill = group["filled_qty"].gt(0) & group[markout].notna()
                stats = _distribution(group.loc[any_fill, markout])
                rows.append(
                    {
                        "date": date_label,
                        "policy": policy,
                        "side": side,
                        "quote_lifetime_ms": int(lifetime),
                        "markout_horizon_ms": horizon_ms,
                        "candidate_quotes": int(len(group)),
                        "resolved_quotes": int(resolved.sum()),
                        "filled_with_label": int(any_fill.sum()),
                        "fill_probability": (
                            float(any_fill.sum() / resolved.sum()) if resolved.any() else math.nan
                        ),
                        **{f"maker_markout_{key}": value for key, value in stats.items()},
                    }
                )
    return rows


def _fee_rows(frame: pd.DataFrame, date_label: str, scenarios: list[float]) -> list[dict[str, Any]]:
    rows = []
    for horizon_ms, horizon_name in HORIZON_NAMES.items():
        bps_column = f"maker_markout_{horizon_name}_bps"
        usdt_column = f"maker_markout_{horizon_name}_usdt_per_1000"
        for (side, lifetime), group in frame.groupby(
            ["side", "quote_lifetime_ms"], sort=True, observed=True
        ):
            labeled = group[bps_column].notna() & group["filled_qty"].gt(0)
            edge_bps = group.loc[labeled, bps_column]
            edge_usdt = group.loc[labeled, usdt_column]
            for maker_fee_bps in scenarios:
                rows.append(
                    {
                        "date": date_label,
                        "side": side,
                        "quote_lifetime_ms": int(lifetime),
                        "markout_horizon_ms": horizon_ms,
                        "maker_fee_bps": maker_fee_bps,
                        "filled_with_label": int(labeled.sum()),
                        "gross_passive_edge_bps": float(edge_bps.mean()),
                        "gross_passive_edge_usdt_per_1000": float(edge_usdt.mean()),
                        "net_edge_after_maker_fee_bps": float((edge_bps - maker_fee_bps).mean()),
                        "break_even_maker_fee_bps": float(edge_bps.mean()),
                        "required_rebate_bps_if_negative": max(0.0, float(-edge_bps.mean())),
                    }
                )
    return rows


def analyze_stage(
    stage: str,
    dates: list[str],
    labeled_paths: list[Path],
    output_dir: Path,
    *,
    bearish_threshold: float,
    bullish_threshold: float,
    maker_fee_scenarios: list[float],
) -> dict[str, Any]:
    if len(dates) != len(labeled_paths):
        raise ValueError("passive stage dates and labeled paths differ")
    fill_rows: list[dict[str, Any]] = []
    markout_rows: list[dict[str, Any]] = []
    queue_rows: list[dict[str, Any]] = []
    filter_rows: list[dict[str, Any]] = []
    fee_rows: list[dict[str, Any]] = []
    pooled_filled = []
    day_summaries = []
    columns = [
        "date", "decision_time_us", "side", "quote_lifetime_ms", "fill_status",
        "filled_qty", "fill_latency_us", "queue_ahead_initial", "order_size_to_displayed_ratio",
        "same_price_traded_qty", "combined_prediction_1s_ticks", "fill_price_advantage_ticks",
        "optimistic_fill_status", "optimistic_filled_qty",
    ]
    for signal in SIGNALS:
        columns.append(f"{signal}_bin")
    for horizon_name in HORIZON_NAMES.values():
        columns.extend([
            f"maker_markout_{horizon_name}_ticks",
            f"post_fill_mid_move_{horizon_name}_ticks",
            f"maker_markout_{horizon_name}_bps",
            f"maker_markout_{horizon_name}_usdt_per_1000",
            f"optimistic_maker_markout_{horizon_name}_ticks",
        ])
    for date, path in zip(dates, labeled_paths):
        frame = pd.read_parquet(path, columns=columns)
        if frame["date"].nunique() != 1 or frame["date"].iat[0] != date:
            raise ValueError(f"labeled passive date mismatch: {path}")
        fill_rows.extend(_fill_rows(frame, date))
        filled = frame.loc[frame["filled_qty"].gt(0)].copy()
        markout_rows.extend(_markout_rows(filled, date))
        queue_rows.extend(_queue_rows(frame, date))
        filter_rows.extend(_filter_rows(frame, date, bearish_threshold, bullish_threshold))
        fee_rows.extend(_fee_rows(frame, date, maker_fee_scenarios))
        pooled_filled.append(filled)
        day_summaries.append(
            {
                "date": date,
                "candidate_quotes": int(len(frame)),
                "valid_at_placement": int(
                    (~frame["fill_status"].eq("invalid_at_placement")).sum()
                ),
                "full_fills": int(frame["fill_status"].eq("full").sum()),
                "partial_fills": int(
                    frame["fill_status"].str.startswith("partial", na=False).sum()
                ),
                "unfilled": int(frame["fill_status"].eq("unfilled").sum()),
                "invalid_at_placement": int(
                    frame["fill_status"].eq("invalid_at_placement").sum()
                ),
                "invalid_snapshot": int(
                    frame["fill_status"].isin(
                        ["invalid_snapshot", "partial_invalid_snapshot"]
                    ).sum()
                ),
                "invalid_day_boundary": int(
                    frame["fill_status"].isin(
                        ["invalid_day_boundary", "partial_invalid_day_boundary"]
                    ).sum()
                ),
                "invalid_quotes": int(
                    (~frame["fill_status"].isin(RESOLVED_STATUSES)).sum()
                ),
            }
        )
    if pooled_filled:
        pooled = pd.concat(pooled_filled, ignore_index=True)
        if not pooled.empty:
            markout_rows.extend(_markout_rows(pooled, "ALL"))
    write_csv(output_dir / "fill_probability_by_day_and_obi.csv", pd.DataFrame(fill_rows))
    write_csv(output_dir / "maker_markout_by_day_and_obi.csv", pd.DataFrame(markout_rows))
    write_csv(output_dir / "queue_model_sensitivity_by_day.csv", pd.DataFrame(queue_rows))
    write_csv(output_dir / "quote_filter_by_day.csv", pd.DataFrame(filter_rows))
    write_csv(output_dir / "maker_fee_envelope_by_day.csv", pd.DataFrame(fee_rows))
    write_csv(output_dir / "day_summary.csv", pd.DataFrame(day_summaries))
    summary = {
        "schema": "passive-adverse-selection-stage-v1",
        "stage": stage,
        "dates": dates,
        "candidate_quotes": int(sum(row["candidate_quotes"] for row in day_summaries)),
        "full_fills": int(sum(row["full_fills"] for row in day_summaries)),
        "partial_fills": int(sum(row["partial_fills"] for row in day_summaries)),
        "filter_thresholds": {
            "bearish_prediction_20pct": bearish_threshold,
            "bullish_prediction_80pct": bullish_threshold,
        },
        "inference": "Newey-West HAC on sequential filled observations; lags=max(lifetime,horizon)/100ms and reset by day",
        "headline_queue_model": "pessimistic_visible_queue",
        "queue_upper_bound": "optimistic_front_of_queue_not_FIFO_claim",
    }
    write_json(output_dir / "run_summary.json", summary)
    return summary
