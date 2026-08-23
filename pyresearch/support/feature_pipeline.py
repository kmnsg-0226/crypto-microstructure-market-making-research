"""Build causal rolling features and future markouts from the canonical 100ms base grid."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


BASE_SCHEMA_VERSION = "tardis-100ms-v1"
WINDOW_NAMES = {100: "100ms", 500: "500ms", 1000: "1s", 5000: "5s"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: object) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    if path.exists() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def load_spec(path: Path) -> dict[str, Any]:
    spec = json.loads(path.read_text())
    if spec["schema"] != "crypto-hft-microstructure-research-v1":
        raise ValueError("unsupported research specification")
    if spec["grid_ms"] != 100:
        raise ValueError("this pipeline requires the frozen 100ms grid")
    return spec


def _rolling_by_segment(
    frame: pd.DataFrame,
    values: pd.Series,
    steps: int,
) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype="float64")
    valid = frame["feature_segment_id"].notna()
    if not valid.any():
        return result
    groups = frame.loc[valid, "feature_segment_id"]
    grouped_values = values.loc[valid].groupby(groups, sort=False, observed=True)
    rolled = grouped_values.rolling(window=steps, min_periods=steps).sum()
    rolled.index = rolled.index.droplevel(0)
    result.loc[rolled.index] = rolled

    # A segment's first valid grid point does not itself provide a complete trailing 100ms.
    positions = frame.loc[valid].groupby(groups, sort=False, observed=True).cumcount()
    warm = positions >= steps
    result.loc[positions.index[~warm]] = np.nan
    return result


def compute_features(frame: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    if frame.empty:
        raise ValueError("canonical base dataset is empty")
    if frame["schema_version"].nunique() != 1 or frame["schema_version"].iat[0] != BASE_SCHEMA_VERSION:
        raise ValueError("unexpected canonical base schema version")
    if not frame["sample_time_us"].is_monotonic_increasing:
        raise ValueError("sample grid is not monotonic")
    expected_step = spec["grid_ms"] * 1000
    differences = frame["sample_time_us"].diff().dropna()
    if not differences.eq(expected_step).all():
        raise ValueError("sample grid is not an exact 100ms grid")
    for timestamp_column in ("latest_book_event_time_us", "latest_trade_time_us"):
        observed = frame[timestamp_column].notna()
        if (frame.loc[observed, timestamp_column] > frame.loc[observed, "sample_time_us"]).any():
            raise ValueError(f"feature leakage: {timestamp_column} exceeds sample_time_us")

    result = frame.copy()
    result["feature_cutoff_time_us"] = result["sample_time_us"]
    valid = result["valid_book_state"].eq(1) & result["book_segment_id"].notna()
    starts_valid_run = valid & (
        ~valid.shift(fill_value=False)
        | result["book_segment_id"].ne(result["book_segment_id"].shift())
    )
    result["feature_segment_id"] = starts_valid_run.cumsum().where(valid).astype("Int64")
    current_l1_depth = result["bid_qty_1"] + result["ask_qty_1"]
    window_sources = {
        "aggressive_buy_qty": "aggressive_buy_qty_100ms",
        "aggressive_sell_qty": "aggressive_sell_qty_100ms",
        "aggressive_buy_notional": "aggressive_buy_notional_100ms",
        "aggressive_sell_notional": "aggressive_sell_notional_100ms",
        "trade_count": "trade_count_100ms",
        "buy_trade_count": "buy_trade_count_100ms",
        "sell_trade_count": "sell_trade_count_100ms",
        "trade_volume": "trade_volume_100ms",
        "book_message_count": "book_message_count_100ms",
        "book_level_update_count": "book_level_update_count_100ms",
        "ofi": "ofi_100ms_bucket",
    }
    # Some 100ms output names intentionally match their canonical bucket names. Preserve
    # every source before assigning results so wider windows never read a warmed-up/NaN
    # version written by an earlier iteration.
    source_values = {source: result[source].copy() for source in set(window_sources.values())}
    for window_ms in spec["windows_ms"]:
        name = WINDOW_NAMES[window_ms]
        steps = window_ms // spec["grid_ms"]
        for output_prefix, source in window_sources.items():
            result[f"{output_prefix}_{name}"] = _rolling_by_segment(
                result, source_values[source], steps
            )
        buy = result[f"aggressive_buy_qty_{name}"]
        sell = result[f"aggressive_sell_qty_{name}"]
        denominator = buy + sell
        result[f"ti_{name}"] = (buy - sell).div(denominator.where(denominator.ne(0)))
        result[f"normalized_ofi_{name}"] = result[f"ofi_{name}"].div(
            current_l1_depth.where(current_l1_depth.ne(0))
        )
        result[f"window_valid_{name}"] = result[f"ofi_{name}"].notna()

    for horizon_ms in spec["label_horizons_ms"]:
        name = WINDOW_NAMES[horizon_ms]
        steps = horizon_ms // spec["grid_ms"]
        future_mid_x2 = result["mid_ticks_x2"].shift(-steps)
        future_segment = result["feature_segment_id"].shift(-steps)
        label_valid = (
            result["feature_segment_id"].notna()
            & future_mid_x2.notna()
            & future_segment.eq(result["feature_segment_id"])
        )
        markout = (future_mid_x2 - result["mid_ticks_x2"]) / 2.0
        markout = markout.where(label_valid)
        result[f"markout_{name}_ticks"] = markout
        direction = np.sign(markout).where(label_valid)
        result[f"direction_{name}"] = direction
        result[f"up_{name}"] = direction.eq(1).where(label_valid)
        result[f"down_{name}"] = direction.eq(-1).where(label_valid)
        result[f"zero_{name}"] = direction.eq(0).where(label_valid)

    invalid = result["valid_book_state"].ne(1)
    feature_columns = [
        column
        for column in result.columns
        if column.startswith(
            (
                "aggressive_buy_qty_",
                "aggressive_sell_qty_",
                "aggressive_buy_notional_",
                "aggressive_sell_notional_",
                "trade_count_",
                "buy_trade_count_",
                "sell_trade_count_",
                "trade_volume_",
                "book_message_count_",
                "book_level_update_count_",
                "ofi_",
                "normalized_ofi_",
                "ti_",
            )
        )
        and not column.endswith("_100ms_bucket")
    ]
    result.loc[invalid, feature_columns] = np.nan
    return result


def build_day(base_path: Path, output_path: Path, spec_path: Path) -> dict[str, Any]:
    spec = load_spec(spec_path)
    frame = pd.read_csv(base_path, compression="zstd", low_memory=False)
    result = compute_features(frame, spec)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".part")
    result.to_parquet(
        temporary,
        index=False,
        engine="pyarrow",
        compression="zstd",
        row_group_size=100_000,
    )
    os.replace(temporary, output_path)
    horizon_counts = {
        str(horizon): int(result[f"markout_{WINDOW_NAMES[horizon]}_ticks"].notna().sum())
        for horizon in spec["label_horizons_ms"]
    }
    return {
        "schema": "microstructure-feature-day-v1",
        "date": str(result["date"].iat[0]),
        "base_path": str(base_path),
        "base_sha256": _sha256(base_path),
        "output_path": str(output_path),
        "output_sha256": _sha256(output_path),
        "rows": len(result),
        "columns": list(result.columns),
        "valid_book_rows": int(result["valid_book_state"].eq(1).sum()),
        "invalid_book_rows": int(result["valid_book_state"].ne(1).sum()),
        "valid_label_rows_by_horizon_ms": horizon_counts,
        "feature_time_leakage_violations": int(
            (
                (result["latest_book_event_time_us"] > result["sample_time_us"])
                | (result["latest_trade_time_us"] > result["sample_time_us"])
            ).fillna(False).sum()
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()
    first = build_day(args.base, args.output, args.spec)
    deterministic = True
    for _ in range(1, args.repeat):
        repeat_path = args.output.with_suffix(args.output.suffix + ".repeat.part")
        second = build_day(args.base, repeat_path, args.spec)
        deterministic = deterministic and first["rows"] == second["rows"]
        deterministic = deterministic and first["columns"] == second["columns"]
        deterministic = deterministic and first["output_sha256"] == second["output_sha256"]
        repeat_path.unlink()
    first["repeat_count"] = args.repeat
    first["deterministic_export"] = deterministic
    _write_json(args.report, first)
    return 0 if deterministic and first["feature_time_leakage_violations"] == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
