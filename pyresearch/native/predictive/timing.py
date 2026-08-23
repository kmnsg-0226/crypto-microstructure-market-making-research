"""Cross-stream timing event study.

Binance publishes aggressive trades and order-book diffs on two independent sockets, and the
diff stream is batched every 100 ms. Any statistic of the form "the order filled before the book
moved" is therefore a race between two feeds. This module summarises, from the native raw
replay, how long the depth stream actually takes to acknowledge an aggressive print.

No timestamp is adjusted, reconciled or latency-corrected anywhere here.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.csv as pa_csv
import zstandard as zstd

from pyresearch.native.predictive import spec

LATENCY_COLUMNS = (
    "depth_qty_reaction_latency_ms",
    "quote_removal_latency_ms",
    "best_price_change_latency_ms",
    "mid_adverse_move_latency_ms",
    "first_mid_move_latency_ms",
)
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90, 0.99)
FRACTION_THRESHOLDS_MS = (10.0, 50.0, 100.0)
OBSERVATION_WINDOW_MS = 5000.0


def event_path(file_index: int) -> Path:
    return spec.DATA_DIR / f"cross_stream_timing_file{file_index}.csv.zst"


def read_events(file_index: int) -> pd.DataFrame:
    columns = [
        "receive_ns",
        "file_index",
        "passive_side",
        "category",
        "trade_qty_lots",
        "quote_qty_lots",
        "spread_ticks",
        "first_mid_move_is_adverse",
        "qty_reaction_exchange_lag_ms",
        *LATENCY_COLUMNS,
    ]
    with event_path(file_index).open("rb") as handle:
        with zstd.ZstdDecompressor().stream_reader(handle) as stream:
            table = pa_csv.read_csv(
                stream, convert_options=pa_csv.ConvertOptions(include_columns=columns)
            )
    return table.to_pandas()


def load_events() -> pd.DataFrame:
    return pd.concat([read_events(index) for index in (0, 1, 2)], ignore_index=True)


def _describe(values: pd.Series, population: int) -> dict:
    observed = values.dropna().to_numpy(dtype="float64")
    record = {
        "population": population,
        "observed": len(observed),
        "unobserved_within_window": population - len(observed),
        "mean_ms": float(observed.mean()) if observed.size else np.nan,
        "std_ms": float(observed.std(ddof=1)) if observed.size > 1 else np.nan,
    }
    quantiles = (
        np.quantile(observed, QUANTILES) if observed.size else [np.nan] * len(QUANTILES)
    )
    for level, value in zip(QUANTILES, quantiles, strict=True):
        record[f"p{level * 100:g}_ms"] = float(value)
    for threshold in FRACTION_THRESHOLDS_MS:
        # Fractions are of the whole population, so an unobserved reaction is never silently
        # dropped out of the denominator.
        record[f"frac_lt_{threshold:g}ms"] = (
            float((observed < threshold).sum()) / population if population else np.nan
        )
    record["frac_gt_100ms"] = (
        float((observed > 100.0).sum()) / population if population else np.nan
    )
    record["frac_unobserved"] = (
        record["unobserved_within_window"] / population if population else np.nan
    )
    return record


def latency_table(events: pd.DataFrame) -> pd.DataFrame:
    """Latency distribution per passive side, print category and reaction type."""
    rows = []
    groups: list[tuple[str, pd.DataFrame]] = [("all", events)]
    for side, block in events.groupby("passive_side", sort=True):
        groups.append((f"side={side}", block))
        for category, sub in block.groupby("category", sort=True):
            groups.append((f"side={side};category={category}", sub))
    for category, block in events.groupby("category", sort=True):
        groups.append((f"category={category}", block))
    # Whether the reconstructed quote ever disappeared, and which way the mid went first.
    survived = events[events["quote_removal_latency_ms"].isna()]
    groups.append(("quote_survives_window", survived))
    groups.append(("quote_disappears", events[events["quote_removal_latency_ms"].notna()]))
    adverse = events["first_mid_move_is_adverse"]
    groups.append(("next_mid_move_adverse", events[adverse == 1]))
    groups.append(("next_mid_move_favorable", events[adverse == 0]))

    for name, block in groups:
        for metric in LATENCY_COLUMNS:
            record = {"group": name, "metric": metric}
            record.update(_describe(block[metric], len(block)))
            rows.append(record)
        record = {"group": name, "metric": "qty_reaction_exchange_lag_ms"}
        record.update(_describe(block["qty_reaction_exchange_lag_ms"], len(block)))
        rows.append(record)
    return pd.DataFrame(rows)


def category_summary(events: pd.DataFrame) -> pd.DataFrame:
    """Headline counts and medians per passive side and print category."""
    rows = []
    for keys, block in events.groupby(["passive_side", "category"], sort=True):
        side, category = keys
        rows.append(
            {
                "passive_side": side,
                "category": category,
                "events": len(block),
                "share_of_side": np.nan,
                "median_depth_qty_reaction_ms": block["depth_qty_reaction_latency_ms"].median(),
                "median_quote_removal_ms": block["quote_removal_latency_ms"].median(),
                "median_best_price_change_ms": block["best_price_change_latency_ms"].median(),
                "median_mid_adverse_move_ms": block["mid_adverse_move_latency_ms"].median(),
                "quote_removal_rate": float(
                    block["quote_removal_latency_ms"].notna().mean()
                ),
                "best_change_rate": float(
                    block["best_price_change_latency_ms"].notna().mean()
                ),
                "mid_adverse_rate": float(
                    block["mid_adverse_move_latency_ms"].notna().mean()
                ),
                "first_mid_move_adverse_rate": float(
                    pd.to_numeric(block["first_mid_move_is_adverse"], errors="coerce").mean()
                ),
                "median_trade_qty_lots": float(block["trade_qty_lots"].median()),
                "median_quote_qty_lots": float(block["quote_qty_lots"].median()),
                "median_exchange_lag_ms": block["qty_reaction_exchange_lag_ms"].median(),
            }
        )
    table = pd.DataFrame(rows)
    totals = table.groupby("passive_side")["events"].transform("sum")
    table["share_of_side"] = table["events"] / totals
    return table


def race_handicap_table(frame: pd.DataFrame) -> pd.DataFrame:
    """How much of the fill-versus-adverse race survives if the trade feed is assumed to lead.

    The handicap is applied to the fill, not to any timestamp in the data: a fill only counts as
    winning the race if it precedes the competing observation by at least the handicap. If the
    original statistic were purely an artefact of the trade socket reporting first, the win rate
    would collapse as the handicap grows past the observed feed lag.
    """
    races = {
        "observed_mid_adverse": "time_to_mid_adverse_ms",
        "quote_disappears": "time_to_quote_gone_ms",
        "depth_best_moves_adverse": "time_to_best_adverse_ms",
    }
    rows = []
    for side in ("bid", "ask"):
        fill = pd.to_numeric(frame[f"{side}_time_to_fill_ms"], errors="coerce")
        for race, column in races.items():
            competitor = pd.to_numeric(frame[f"{side}_{column}"], errors="coerce")
            for handicap in spec.RACE_HANDICAPS_MS:
                handicapped = fill + handicap
                fill_first = handicapped.notna() & (
                    competitor.isna() | (handicapped < competitor)
                )
                competitor_first = competitor.notna() & (
                    handicapped.isna() | (competitor <= handicapped)
                )
                resolved = fill_first | competitor_first
                denominator = int(resolved.sum())
                rows.append(
                    {
                        "side": side,
                        "race": race,
                        "fill_handicap_ms": handicap,
                        "eligible": int(len(frame)),
                        "resolved": denominator,
                        "censored": int(len(frame) - denominator),
                        "fill_first": int(fill_first.sum()),
                        "fill_first_rate": (
                            float(fill_first.sum()) / denominator if denominator else np.nan
                        ),
                    }
                )
    return pd.DataFrame(rows)
