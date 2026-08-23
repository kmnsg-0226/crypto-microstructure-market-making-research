"""Baseline diagnostics for the native_dev_v1 decision dataset.

Descriptive only. Nothing here fits a model, searches a threshold, or applies fees; the
information coefficients exist to show which raw signals carry any linear or monotone relation
to short-horizon mid markouts before any model is chosen.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.csv as pa_csv
import zstandard as zstd

WINDOWS_MS = (100, 250, 500, 1000, 5000)
MARKOUT_TARGETS = ("markout_100ms_ticks", "markout_500ms_ticks", "markout_1000ms_ticks")

STATE_COLUMNS = [
    "spread_ticks",
    "mid_ticks",
    "microprice_minus_mid_ticks",
    "obi_l1",
    "obi_l5",
    "obi_l10",
    "weighted_obi_l5",
    "weighted_obi_l10",
    "bid_depth_l1",
    "ask_depth_l1",
    "bid_depth_l5",
    "ask_depth_l5",
    "bid_depth_l10",
    "ask_depth_l10",
    "bid_concentration_l5",
    "ask_concentration_l5",
    "bid_dispersion_ticks_l10",
    "ask_dispersion_ticks_l10",
]
FLOW_COLUMNS = [
    f"{name}_{window}ms"
    for window in (100, 500, 1000)
    for name in (
        "trade_imbalance",
        "signed_volume",
        "trade_count",
        "depth_event_count",
        "depth_flow_pressure_l5",
        "depth_flow_pressure_l10",
        "bid_depth_add_l1",
        "bid_depth_remove_l1",
        "ask_depth_add_l1",
        "ask_depth_remove_l1",
        "bid_depth_add_l5",
        "bid_depth_remove_l5",
        "ask_depth_add_l5",
        "ask_depth_remove_l5",
    )
]
TARGET_COLUMNS = (
    ["next_mid_move_dir", "time_to_next_mid_move_ms"]
    + [f"markout_{window}ms_ticks" for window in WINDOWS_MS]
    + [
        f"{side}_{name}"
        for side in ("bid", "ask")
        for name in (
            "eligible",
            "queue_ahead_lots",
            "fill_500ms",
            "fill_1000ms",
            "fill_5000ms",
            "filled_lots_30000ms",
            "fill_before_observed_mid_adverse",
            "fill_via_trade_through",
            "time_to_fill_ms",
            "time_to_mid_adverse_ms",
            "time_to_quote_gone_ms",
            "time_to_best_adverse_ms",
            "postfill_markout_100ms_ticks",
            "postfill_markout_500ms_ticks",
            "postfill_markout_1000ms_ticks",
            "postfill_markout_5000ms_ticks",
        )
    ]
)
META_COLUMNS = ["timestamp_ns", "file_index", "segment_id", "segment_age_ms", "window_warm"]

DIAGNOSTIC_COLUMNS = META_COLUMNS + STATE_COLUMNS + FLOW_COLUMNS + TARGET_COLUMNS

QUANTILES = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)


def read_columns(path: Path, columns: list[str]) -> pd.DataFrame:
    with path.open("rb") as handle:
        with zstd.ZstdDecompressor().stream_reader(handle) as stream:
            table = pa_csv.read_csv(
                stream,
                convert_options=pa_csv.ConvertOptions(include_columns=columns),
            )
    return table.to_pandas()


def add_derived(frame: pd.DataFrame) -> pd.DataFrame:
    """Signed pressure combinations that the raw per-level columns only imply."""
    for window in (100, 500, 1000):
        for level in ("l1", "l5"):
            bid = (
                frame[f"bid_depth_add_{level}_{window}ms"]
                - frame[f"bid_depth_remove_{level}_{window}ms"]
            )
            ask = (
                frame[f"ask_depth_add_{level}_{window}ms"]
                - frame[f"ask_depth_remove_{level}_{window}ms"]
            )
            gross = (
                frame[f"bid_depth_add_{level}_{window}ms"]
                + frame[f"bid_depth_remove_{level}_{window}ms"]
                + frame[f"ask_depth_add_{level}_{window}ms"]
                + frame[f"ask_depth_remove_{level}_{window}ms"]
            )
            frame[f"net_depth_flow_{level}_{window}ms"] = bid - ask
            frame[f"norm_depth_flow_{level}_{window}ms"] = (bid - ask) / gross.where(gross > 0)
    frame["concentration_imbalance_l5"] = (
        frame["bid_concentration_l5"] - frame["ask_concentration_l5"]
    )
    frame["dispersion_imbalance_l10"] = (
        frame["ask_dispersion_ticks_l10"] - frame["bid_dispersion_ticks_l10"]
    )
    frame["depth_imbalance_l10_lots"] = frame["bid_depth_l10"] - frame["ask_depth_l10"]
    return frame


SIGNALS = [
    "obi_l1",
    "obi_l5",
    "obi_l10",
    "weighted_obi_l5",
    "weighted_obi_l10",
    "microprice_minus_mid_ticks",
    "trade_imbalance_100ms",
    "trade_imbalance_500ms",
    "trade_imbalance_1000ms",
    "signed_volume_100ms",
    "signed_volume_500ms",
    "signed_volume_1000ms",
    "net_depth_flow_l1_100ms",
    "net_depth_flow_l1_500ms",
    "net_depth_flow_l5_100ms",
    "net_depth_flow_l5_500ms",
    "norm_depth_flow_l1_100ms",
    "norm_depth_flow_l5_500ms",
    "depth_flow_pressure_l5_100ms",
    "depth_flow_pressure_l5_500ms",
    "depth_flow_pressure_l10_100ms",
    "depth_flow_pressure_l10_500ms",
    "depth_flow_pressure_l10_1000ms",
    "concentration_imbalance_l5",
    "dispersion_imbalance_l10",
    "depth_imbalance_l10_lots",
]


def describe(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    rows = []
    for name in columns:
        series = pd.to_numeric(frame[name], errors="coerce")
        values = series.to_numpy(dtype="float64")
        finite = values[np.isfinite(values)]
        record = {
            "column": name,
            "rows": len(values),
            "observed": len(finite),
            "missing": len(values) - len(finite),
            "mean": np.mean(finite) if finite.size else np.nan,
            "std": np.std(finite, ddof=1) if finite.size > 1 else np.nan,
            "min": np.min(finite) if finite.size else np.nan,
            "max": np.max(finite) if finite.size else np.nan,
        }
        quantiles = np.quantile(finite, QUANTILES) if finite.size else [np.nan] * len(QUANTILES)
        for level, value in zip(QUANTILES, quantiles, strict=True):
            record[f"p{level * 100:g}"] = value
        rows.append(record)
    return pd.DataFrame(rows)


def information_coefficients(frame: pd.DataFrame) -> pd.DataFrame:
    """Univariate Pearson and Spearman IC of each signal against each markout horizon."""
    rows = []
    ranks: dict[str, np.ndarray] = {}
    for name in SIGNALS + list(MARKOUT_TARGETS):
        series = pd.to_numeric(frame[name], errors="coerce")
        ranks[name] = series.rank(method="average").to_numpy(dtype="float64")
    for signal in SIGNALS:
        signal_values = pd.to_numeric(frame[signal], errors="coerce").to_numpy(dtype="float64")
        for target in MARKOUT_TARGETS:
            target_values = pd.to_numeric(frame[target], errors="coerce").to_numpy(
                dtype="float64"
            )
            mask = np.isfinite(signal_values) & np.isfinite(target_values)
            count = int(mask.sum())
            pearson = spearman = np.nan
            if count > 2:
                x = signal_values[mask]
                y = target_values[mask]
                if x.std() > 0 and y.std() > 0:
                    pearson = float(np.corrcoef(x, y)[0, 1])
                rx = ranks[signal][mask]
                ry = ranks[target][mask]
                if rx.std() > 0 and ry.std() > 0:
                    spearman = float(np.corrcoef(rx, ry)[0, 1])
            rows.append(
                {
                    "signal": signal,
                    "target": target,
                    "observations": count,
                    "pearson_ic": pearson,
                    "spearman_ic": spearman,
                }
            )
    return pd.DataFrame(rows).sort_values(["target", "signal"], ignore_index=True)


def bucket_study(frame: pd.DataFrame, buckets: int = 10) -> pd.DataFrame:
    """Mean markout inside each signal decile, with the bucket population retained."""
    rows = []
    for signal in SIGNALS:
        signal_values = pd.to_numeric(frame[signal], errors="coerce")
        try:
            labels = pd.qcut(signal_values, buckets, labels=False, duplicates="drop")
        except ValueError:
            continue
        for target in MARKOUT_TARGETS:
            target_values = pd.to_numeric(frame[target], errors="coerce")
            grouped = pd.DataFrame(
                {"bucket": labels, "signal": signal_values, "target": target_values}
            ).dropna()
            if grouped.empty:
                continue
            summary = grouped.groupby("bucket", observed=True).agg(
                observations=("target", "size"),
                signal_mean=("signal", "mean"),
                target_mean=("target", "mean"),
                target_std=("target", "std"),
            )
            for bucket, record in summary.iterrows():
                rows.append(
                    {
                        "signal": signal,
                        "target": target,
                        "bucket": int(bucket),
                        "observations": int(record["observations"]),
                        "signal_mean": record["signal_mean"],
                        "target_mean": record["target_mean"],
                        "target_std": record["target_std"],
                    }
                )
    return pd.DataFrame(rows).sort_values(
        ["signal", "target", "bucket"], ignore_index=True
    )


def passive_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Fill and adverse-selection statistics that always carry their own denominator.

    Every eligible maker opportunity is counted, filled or not, and every row names the exact
    population its rate or mean is conditioned on. A post-fill markout mean can therefore never
    be read as if only completed round trips existed.
    """
    rows = []
    for side in ("bid", "ask"):
        eligible = int(pd.to_numeric(frame[f"{side}_eligible"], errors="coerce").sum())
        filled = pd.to_numeric(frame[f"{side}_time_to_fill_ms"], errors="coerce").notna()
        full_fills = int(filled.sum())

        def record(statistic, denominator_name, denominator, numerator, conditional_mean=np.nan):
            rows.append(
                {
                    "side": side,
                    "statistic": statistic,
                    "eligible_opportunities": eligible,
                    "denominator_name": denominator_name,
                    "denominator": denominator,
                    "numerator": numerator,
                    "rate": numerator / denominator if denominator else np.nan,
                    "conditional_mean": conditional_mean,
                }
            )

        record("full_fill_rate_within_observation_window", "eligible_opportunities",
               eligible, full_fills)
        for horizon in ("500ms", "1000ms", "5000ms"):
            flags = pd.to_numeric(frame[f"{side}_fill_{horizon}"], errors="coerce")
            evaluable = int(flags.notna().sum())
            record(
                f"fill_rate_{horizon}",
                "opportunities_with_the_full_horizon_inside_the_segment",
                evaluable,
                int(flags.sum()) if evaluable else 0,
            )
        race = pd.to_numeric(
            frame[f"{side}_fill_before_observed_mid_adverse"], errors="coerce"
        )
        resolved = int(race.notna().sum())
        record("fill_before_observed_mid_adverse",
               "races_resolved_inside_the_observation_window",
               resolved, int(race.sum()) if resolved else 0)
        through = pd.to_numeric(frame[f"{side}_fill_via_trade_through"], errors="coerce")
        record("fill_via_trade_through", "full_fills", full_fills,
               int(through.sum()) if full_fills else 0)
        for horizon in ("100ms", "500ms", "1000ms", "5000ms"):
            markout = pd.to_numeric(
                frame[f"{side}_postfill_markout_{horizon}_ticks"], errors="coerce"
            )
            observed = int(markout.notna().sum())
            record(
                f"postfill_markout_{horizon}_ticks",
                "full_fills_with_the_markout_horizon_inside_the_segment",
                observed,
                observed,
                float(markout.mean()) if observed else np.nan,
            )
    return pd.DataFrame(rows)
