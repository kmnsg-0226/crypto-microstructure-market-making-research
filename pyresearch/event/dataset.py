"""Label and audit compact event-time passive-quote opportunities."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from pyresearch.support.evaluate import sha256, write_json


from pyresearch import ROOT
PLAN_PATH = ROOT / "research/specs/event_model_comparison_plan.json"
DATA_ROOT = ROOT / "data/research/tardis/event_models"
FEATURE_ROOT = ROOT / "data/research/tardis"
HORIZONS_MS = (100, 500, 1000, 5000)
TICK_SIZE = 0.1
SCHEMA = "event-selective-maker-v1"


def _content_hash_update(digest: Any, frame: pd.DataFrame) -> None:
    digest.update("\n".join(frame.columns).encode())
    digest.update(pd.util.hash_pandas_object(frame, index=False).values.tobytes())


def attach_event_labels(
    frame: pd.DataFrame,
    *,
    day_start_us: int,
    mids: np.ndarray,
    valid: np.ndarray,
    segments: np.ndarray,
) -> pd.DataFrame:
    """Attach only future targets; no target is used in an input feature."""
    result = frame.copy()
    fill_times = result["first_fill_exchange_time_us"].to_numpy(dtype="float64")
    filled_qty = result["filled_qty"].to_numpy(dtype="float64")
    quote_price = result["quote_price"].to_numpy(dtype="float64")
    direction = result["quote_side"].to_numpy(dtype="float64")
    has_fill = np.isfinite(fill_times) & (filled_qty > 0) & np.isfinite(quote_price)
    result["fill_label"] = has_fill.astype("int8")

    fill_grid = np.full(len(result), -1, dtype="int64")
    fill_grid[has_fill] = (
        np.ceil(fill_times[has_fill] / 100_000.0).astype("int64") * 100_000
    )
    fill_index = np.full(len(result), -1, dtype="int64")
    fill_index[has_fill] = (fill_grid[has_fill] - day_start_us) // 100_000
    in_range = has_fill & (fill_index >= 0) & (fill_index < len(mids))
    fill_mid = np.full(len(result), np.nan, dtype="float64")
    fill_segment = np.full(len(result), np.nan, dtype="float64")
    fill_mid[in_range] = mids[fill_index[in_range]]
    fill_segment[in_range] = segments[fill_index[in_range]]
    fill_valid = in_range.copy()
    indices = np.flatnonzero(in_range)
    fill_valid[indices] &= valid[fill_index[indices]]
    result["fill_grid_time_us"] = pd.array(
        np.where(in_range, fill_grid, 0), dtype="Int64"
    )
    result.loc[~in_range, "fill_grid_time_us"] = pd.NA
    result["mid_at_fill_grid"] = fill_mid
    advantage = direction * (fill_mid - quote_price) / TICK_SIZE
    advantage[~fill_valid] = np.nan
    result["fill_price_advantage_ticks"] = advantage

    for horizon in HORIZONS_MS:
        target_index = fill_index + horizon // 100
        label_valid = fill_valid & (target_index >= 0) & (target_index < len(mids))
        selected = np.flatnonzero(label_valid)
        label_valid[selected] &= valid[target_index[selected]]
        label_valid[selected] &= segments[target_index[selected]] == fill_segment[selected]
        future_mid = np.full(len(result), np.nan, dtype="float64")
        selected = np.flatnonzero(label_valid)
        future_mid[selected] = mids[target_index[selected]]
        maker = direction * (future_mid - quote_price) / TICK_SIZE
        post_move = direction * (future_mid - fill_mid) / TICK_SIZE
        maker[~label_valid] = np.nan
        post_move[~label_valid] = np.nan
        name = "1s" if horizon == 1000 else "5s" if horizon == 5000 else f"{horizon}ms"
        result[f"maker_markout_{name}_ticks"] = maker
        result[f"post_fill_mid_move_{name}_ticks"] = post_move
        result[f"maker_markout_{name}_bps"] = maker * TICK_SIZE / quote_price * 10_000.0
        result[f"label_valid_{name}"] = label_valid.astype("int8")
        error = np.abs(maker - advantage - post_move)
        if np.nanmax(error, initial=0.0) > 1e-8:
            raise ValueError("maker markout decomposition/spread invariant failed")
    return result


def audit_side_symmetry(frame: pd.DataFrame) -> dict[str, Any]:
    directional = [
        column for column in frame.columns
        if column.startswith("side_") and column != "side"
    ]
    pairs = frame.pivot(index="opportunity_id", columns="side", values=directional)
    complete = pairs.dropna()
    violations = 0
    maximum_error = 0.0
    for column in directional:
        error = np.abs(
            complete[(column, "bid")].to_numpy(dtype="float64")
            + complete[(column, "ask")].to_numpy(dtype="float64")
        )
        maximum_error = max(maximum_error, float(np.nanmax(error, initial=0.0)))
        violations += int(np.count_nonzero(error > 1e-8))
    quote_side_ok = bool(
        frame.loc[frame["side"].eq("bid"), "quote_side"].eq(1).all()
        and frame.loc[frame["side"].eq("ask"), "quote_side"].eq(-1).all()
    )
    return {
        "paired_opportunities": int(len(complete)),
        "directional_columns": directional,
        "sign_violations": violations,
        "maximum_sign_error": maximum_error,
        "quote_side_encoding_valid": quote_side_ok,
    }


def label_day(date: str, *, chunksize: int = 100_000) -> dict[str, Any]:
    source = DATA_ROOT / date / "events.csv.zst"
    output = DATA_ROOT / date / "labeled_events.parquet"
    report_path = DATA_ROOT / date / "dataset_manifest.json"
    grid = pd.read_parquet(
        FEATURE_ROOT / date / "features_100ms.parquet",
        columns=["sample_time_us", "valid_book_state", "feature_segment_id", "mid"],
    )
    if len(grid) != 864_000 or not grid["sample_time_us"].diff().dropna().eq(100_000).all():
        raise ValueError("invalid canonical markout grid")
    day_start_us = int(grid["sample_time_us"].iat[0])
    mids = grid["mid"].to_numpy(dtype="float64")
    valid = grid["valid_book_state"].eq(1).to_numpy()
    segments = grid["feature_segment_id"].to_numpy(dtype="float64", na_value=np.nan)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".parquet.part")
    writer: pq.ParquetWriter | None = None
    digest = hashlib.sha256()
    rows = fills = valid_primary = 0
    first_audit: pd.DataFrame | None = None
    try:
        for chunk in pd.read_csv(source, compression="zstd", chunksize=chunksize):
            if not chunk["schema_version"].eq(SCHEMA).all():
                raise ValueError("unexpected event dataset schema")
            if not chunk["date"].eq(date).all():
                raise ValueError("event dataset date mismatch")
            labeled = attach_event_labels(
                chunk,
                day_start_us=day_start_us,
                mids=mids,
                valid=valid,
                segments=segments,
            )
            if first_audit is None:
                first_audit = labeled.head(10_000).copy()
            table = pa.Table.from_pandas(labeled, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(
                    temporary, table.schema, compression="zstd", use_dictionary=True
                )
            writer.write_table(table, row_group_size=chunksize)
            _content_hash_update(digest, labeled)
            rows += len(labeled)
            fills += int(labeled["fill_label"].sum())
            valid_primary += int(labeled["label_valid_1s"].sum())
    finally:
        if writer is not None:
            writer.close()
    if writer is None or first_audit is None:
        raise ValueError("empty event dataset")
    os.replace(temporary, output)
    symmetry = audit_side_symmetry(first_audit)
    if symmetry["sign_violations"] or not symmetry["quote_side_encoding_valid"]:
        raise ValueError("side-oriented feature symmetry audit failed")
    export_report = DATA_ROOT / date / "export_report.json"
    raw_manifest = ROOT / "data/research/tardis/passive" / date / "day_manifest.json"
    payload = {
        "schema": "event-selective-maker-dataset-manifest-v1",
        "date": date,
        "plan_sha256": sha256(PLAN_PATH),
        "source": str(source.relative_to(ROOT)),
        "source_sha256": sha256(source),
        "output": str(output.relative_to(ROOT)),
        "output_sha256": sha256(output),
        "content_checksum": digest.hexdigest(),
        "export_report_sha256": sha256(export_report),
        "raw_day_manifest_sha256": sha256(raw_manifest),
        "rows": rows,
        "fill_rows": fills,
        "fill_rate": fills / rows,
        "valid_primary_markout_rows": valid_primary,
        "primary_input_is_event_time": True,
        "markout_grid_is_label_only": True,
        "side_symmetry": symmetry,
    }
    write_json(report_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--chunksize", type=int, default=100_000)
    args = parser.parse_args()
    print(json.dumps(label_day(args.date, chunksize=args.chunksize), sort_keys=True))


if __name__ == "__main__":
    main()
