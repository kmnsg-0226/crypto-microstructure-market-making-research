"""Attach causal 100 ms post-fill markouts to passive probe outcomes."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from pyresearch.support.evaluate import sha256, write_json


from pyresearch import ROOT
FEATURE_ROOT = ROOT / "data/research/tardis"
TRANSFORMS_PATH = ROOT / "data/research/tardis/reports/development/development_transforms.json"
HORIZONS_MS = (100, 500, 1000, 5000)
TICK_SIZE = 0.1


def _json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _content_hash_update(digest: Any, frame: pd.DataFrame) -> None:
    digest.update("\n".join(frame.columns).encode())
    digest.update(pd.util.hash_pandas_object(frame, index=False).values.tobytes())


def attach_markouts(
    frame: pd.DataFrame,
    *,
    day_start_us: int,
    mids: np.ndarray,
    valid: np.ndarray,
    segments: np.ndarray,
) -> pd.DataFrame:
    result = frame.copy()
    quote_price = result["quote_price"].to_numpy(dtype="float64")
    direction = np.where(result["side"].eq("bid"), 1.0, -1.0)
    transforms = _json(TRANSFORMS_PATH)
    for signal in ("obi_l1", "obi_l5", "obi_l10"):
        values = result[signal].to_numpy(dtype="float64")
        thresholds = np.asarray(transforms["decile_thresholds"][signal], dtype="float64")
        bins = np.searchsorted(thresholds, values, side="right") + 1
        result[f"{signal}_bin"] = pd.array(
            np.where(np.isfinite(values), bins, 0), dtype="Int8"
        )
        result.loc[~np.isfinite(values), f"{signal}_bin"] = pd.NA

    for model_prefix, time_column in (
        ("", "first_fill_exchange_time_us"),
        ("optimistic_", "optimistic_first_fill_exchange_time_us"),
    ):
        fill_times = result[time_column].to_numpy(dtype="float64")
        filled_qty = result[f"{model_prefix}filled_qty"].to_numpy(dtype="float64")
        has_fill = np.isfinite(fill_times) & (filled_qty > 0) & np.isfinite(quote_price)
        fill_grid = np.full(len(result), -1, dtype="int64")
        fill_grid[has_fill] = (
            np.ceil(fill_times[has_fill] / 100_000.0).astype("int64") * 100_000
        )
        fill_index = np.full(len(result), -1, dtype="int64")
        fill_index[has_fill] = (fill_grid[has_fill] - day_start_us) // 100_000
        fill_in_range = has_fill & (fill_index >= 0) & (fill_index < len(mids))
        fill_mid = np.full(len(result), np.nan, dtype="float64")
        fill_segment = np.full(len(result), np.nan, dtype="float64")
        fill_mid[fill_in_range] = mids[fill_index[fill_in_range]]
        fill_segment[fill_in_range] = segments[fill_index[fill_in_range]]
        fill_valid = fill_in_range.copy()
        fill_valid[fill_in_range] &= valid[fill_index[fill_in_range]]
        result[f"{model_prefix}fill_grid_time_us"] = pd.array(
            np.where(fill_in_range, fill_grid, 0), dtype="Int64"
        )
        result.loc[~fill_in_range, f"{model_prefix}fill_grid_time_us"] = pd.NA
        result[f"{model_prefix}mid_at_fill_grid"] = fill_mid
        advantage = direction * (fill_mid - quote_price) / TICK_SIZE
        advantage[~fill_valid] = np.nan
        result[f"{model_prefix}fill_price_advantage_ticks"] = advantage

        for horizon_ms in HORIZONS_MS:
            target_index = fill_index + horizon_ms // 100
            label_valid = (
                fill_valid
                & (target_index >= 0)
                & (target_index < len(mids))
            )
            safe = np.flatnonzero(label_valid)
            if safe.size:
                label_valid[safe] &= valid[target_index[safe]]
                label_valid[safe] &= segments[target_index[safe]] == fill_segment[safe]
            future_mid = np.full(len(result), np.nan, dtype="float64")
            selected = np.flatnonzero(label_valid)
            future_mid[selected] = mids[target_index[selected]]
            maker = direction * (future_mid - quote_price) / TICK_SIZE
            post_move = direction * (future_mid - fill_mid) / TICK_SIZE
            maker[~label_valid] = np.nan
            post_move[~label_valid] = np.nan
            name = f"{horizon_ms}ms" if horizon_ms != 1000 else "1s"
            if horizon_ms == 5000:
                name = "5s"
            result[f"{model_prefix}maker_markout_{name}_ticks"] = maker
            result[f"{model_prefix}post_fill_mid_move_{name}_ticks"] = post_move
            result[f"{model_prefix}maker_markout_{name}_bps"] = (
                maker * TICK_SIZE / quote_price * 10_000.0
            )
            result[f"{model_prefix}maker_markout_{name}_usdt_per_1000"] = (
                maker * TICK_SIZE / quote_price * 1000.0
            )
            result[f"{model_prefix}label_valid_{name}"] = label_valid.astype("int8")
            decomposition_error = np.abs(maker - advantage - post_move)
            if np.nanmax(decomposition_error, initial=0.0) > 1e-8:
                raise ValueError("maker markout double-count/decomposition invariant failed")
    return result


def label_probe_file(
    date: str,
    probes: Path,
    output: Path,
    report: Path,
    *,
    chunksize: int = 200_000,
) -> dict[str, Any]:
    features = pd.read_parquet(
        FEATURE_ROOT / date / "features_100ms.parquet",
        columns=["sample_time_us", "valid_book_state", "feature_segment_id", "mid"],
    )
    if len(features) != 864_000 or not features["sample_time_us"].diff().dropna().eq(100_000).all():
        raise ValueError("invalid canonical feature grid for maker labels")
    day_start_us = int(features["sample_time_us"].iat[0])
    mids = features["mid"].to_numpy(dtype="float64")
    valid = features["valid_book_state"].eq(1).to_numpy()
    segments = features["feature_segment_id"].to_numpy(dtype="float64", na_value=np.nan)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".part")
    writer: pq.ParquetWriter | None = None
    digest = hashlib.sha256()
    rows = 0
    filled = 0
    partial = 0
    label_counts = {str(horizon): 0 for horizon in HORIZONS_MS}
    try:
        for chunk in pd.read_csv(
            probes,
            compression="zstd",
            chunksize=chunksize,
            dtype={
                "schema_version": "string",
                "date": "string",
                "side": "string",
                "fill_status": "string",
                "fill_reason": "string",
                "cancel_reason": "string",
                "optimistic_fill_status": "string",
                "optimistic_fill_reason": "string",
                "optimistic_cancel_reason": "string",
                "feature_segment_id": "float64",
                "spread_ticks": "float64",
                "next_snapshot_local_time_us": "float64",
            },
        ):
            if chunk["schema_version"].nunique() != 1 or chunk["schema_version"].iat[0] != "tardis-passive-probes-v1":
                raise ValueError("unexpected passive probe schema")
            if chunk["date"].nunique() != 1 or chunk["date"].iat[0] != date:
                raise ValueError("passive probe date mismatch")
            labeled = attach_markouts(
                chunk,
                day_start_us=day_start_us,
                mids=mids,
                valid=valid,
                segments=segments,
            )
            table = pa.Table.from_pandas(labeled, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(
                    temporary,
                    table.schema,
                    compression="zstd",
                    use_dictionary=True,
                    write_statistics=True,
                )
            writer.write_table(table, row_group_size=chunksize)
            _content_hash_update(digest, labeled)
            rows += len(labeled)
            filled += int(labeled["fill_status"].eq("full").sum())
            partial += int(labeled["fill_status"].str.startswith("partial", na=False).sum())
            for horizon_ms in HORIZONS_MS:
                name = f"{horizon_ms}ms" if horizon_ms != 1000 else "1s"
                if horizon_ms == 5000:
                    name = "5s"
                label_counts[str(horizon_ms)] += int(labeled[f"label_valid_{name}"].sum())
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        raise ValueError("empty passive probe file")
    os.replace(temporary, output)
    payload = {
        "schema": "passive-probe-label-report-v1",
        "date": date,
        "probes": str(probes.resolve().relative_to(ROOT)),
        "probes_sha256": sha256(probes),
        "output": str(output.resolve().relative_to(ROOT)),
        "output_sha256": sha256(output),
        "content_checksum": digest.hexdigest(),
        "rows": rows,
        "full_fills": filled,
        "partial_or_partially_invalidated": partial,
        "valid_markout_labels": label_counts,
        "markout_grid_rule": "ceil_first_fill_exchange_time_to_next_100ms_grid",
        "future_data_used_in_fill_decision": False,
        "spread_double_count_violations": 0,
    }
    write_json(report, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--probes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--chunksize", type=int, default=200_000)
    args = parser.parse_args()
    print(json.dumps(label_probe_file(
        args.date,
        args.probes,
        args.output,
        args.report,
        chunksize=args.chunksize,
    ), sort_keys=True))


if __name__ == "__main__":
    main()
