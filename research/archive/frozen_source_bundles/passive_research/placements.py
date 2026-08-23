"""Create deterministic passive-probe placements from frozen 100 ms features."""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from execution_research.engine import frozen_prediction
from research.evaluate import sha256, write_json


ROOT = Path(__file__).resolve().parents[1]
FEATURE_ROOT = ROOT / "data/research/tardis"
L2_REPORT_ROOT = ROOT / "data/historical/tardis/reports/2026-first-days"
MODELS_PATH = ROOT / "data/research/tardis/reports/development/fitted_models.json"
TRANSFORMS_PATH = ROOT / "data/research/tardis/reports/development/development_transforms.json"

OUTPUT_COLUMNS = [
    "date",
    "decision_time_us",
    "placement_local_time_us",
    "feature_segment_id",
    "valid_book_state",
    "best_bid_price",
    "best_bid_qty",
    "best_ask_price",
    "best_ask_qty",
    "spread_ticks",
    "obi_l1",
    "obi_l5",
    "obi_l10",
    "weighted_obi_l10",
    "weighted_mid_minus_mid_ticks",
    "normalized_ofi_1s",
    "ti_1s",
    "combined_prediction_1s_ticks",
    "next_snapshot_local_time_us",
]


def _json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_deterministic_gzip_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as zipped:
            with io.TextIOWrapper(zipped, encoding="utf-8", newline="") as text:
                frame.to_csv(
                    text,
                    index=False,
                    columns=OUTPUT_COLUMNS,
                    float_format="%.12g",
                    lineterminator="\n",
                    na_rep="",
                )
    os.replace(temporary, path)


def build_placements(date: str, output: Path, report: Path, repeat: int = 2) -> dict[str, Any]:
    feature_path = FEATURE_ROOT / date / "features_100ms.parquet"
    l2_report_path = L2_REPORT_ROOT / f"{date}.json"
    model = _json(MODELS_PATH)["models"]["combined:1000"]
    transforms = _json(TRANSFORMS_PATH)
    source_columns = list(
        dict.fromkeys(
            [
                "date",
                "sample_time_us",
                "latest_book_event_time_us",
                "latest_book_local_time_us",
                "latest_trade_time_us",
                "latest_trade_local_time_us",
                "feature_segment_id",
                "valid_book_state",
                "best_bid_price",
                "best_bid_qty",
                "best_ask_price",
                "best_ask_qty",
                "spread_ticks",
                "obi_l1",
                "obi_l5",
                "obi_l10",
                "weighted_obi_l10",
                "weighted_mid_minus_mid_ticks",
                "normalized_ofi_1s",
                "ti_1s",
            ]
            + list(model["features"])
        )
    )
    source = pd.read_parquet(feature_path, columns=source_columns)
    if len(source) != 864_000 or source["date"].nunique() != 1 or source["date"].iat[0] != date:
        raise ValueError(f"unexpected frozen feature day: {feature_path}")
    if not source["sample_time_us"].diff().dropna().eq(100_000).all():
        raise ValueError("passive placements require the exact frozen 100ms grid")
    for event_column in ("latest_book_event_time_us", "latest_trade_time_us"):
        observed = source[event_column].notna()
        if (source.loc[observed, event_column] > source.loc[observed, "sample_time_us"]).any():
            raise ValueError(f"future event leakage in {event_column}")

    prediction = frozen_prediction(source, model, transforms)
    decision = source["sample_time_us"].to_numpy(dtype="int64")
    book_local = source["latest_book_local_time_us"].fillna(0).to_numpy(dtype="int64")
    trade_local = source["latest_trade_local_time_us"].fillna(0).to_numpy(dtype="int64")
    placement_local = np.maximum.reduce([decision, book_local, trade_local])
    if np.any(np.diff(placement_local) < 0):
        raise ValueError("derived placement local time regressed")

    snapshots = np.asarray(
        [
            int(item["start_local_timestamp_us"])
            for item in _json(l2_report_path)["snapshots"]
        ],
        dtype="int64",
    )
    positions = np.searchsorted(snapshots, placement_local, side="right")
    next_snapshot = np.zeros(len(source), dtype="int64")
    available = positions < len(snapshots)
    next_snapshot[available] = snapshots[positions[available]]

    placements = source[
        [
            "date",
            "feature_segment_id",
            "valid_book_state",
            "best_bid_price",
            "best_bid_qty",
            "best_ask_price",
            "best_ask_qty",
            "spread_ticks",
            "obi_l1",
            "obi_l5",
            "obi_l10",
            "weighted_obi_l10",
            "weighted_mid_minus_mid_ticks",
            "normalized_ofi_1s",
            "ti_1s",
        ]
    ].copy()
    placements.insert(1, "decision_time_us", decision)
    placements.insert(2, "placement_local_time_us", placement_local)
    placements["combined_prediction_1s_ticks"] = prediction
    placements["next_snapshot_local_time_us"] = next_snapshot
    placements = placements[OUTPUT_COLUMNS]

    _write_deterministic_gzip_csv(output, placements)
    first_hash = sha256(output)
    deterministic = True
    if repeat > 1:
        repeat_path = output.with_suffix(output.suffix + ".repeat.part")
        _write_deterministic_gzip_csv(repeat_path, placements)
        deterministic = sha256(repeat_path) == first_hash
        repeat_path.unlink()
    payload = {
        "schema": "passive-placement-export-v1",
        "date": date,
        "input": str(feature_path.relative_to(ROOT)),
        "input_sha256": sha256(feature_path),
        "l2_report": str(l2_report_path.relative_to(ROOT)),
        "output": str(output.resolve().relative_to(ROOT)),
        "output_sha256": first_hash,
        "rows": int(len(placements)),
        "valid_book_rows": int(placements["valid_book_state"].eq(1).sum()),
        "finite_combined_prediction_rows": int(
            np.isfinite(placements["combined_prediction_1s_ticks"]).sum()
        ),
        "placement_delayed_past_decision_rows": int((placement_local > decision).sum()),
        "maximum_placement_delay_us": int(np.max(placement_local - decision)),
        "snapshot_events_after_initial": max(0, int(len(snapshots) - 1)),
        "repeat_count": repeat,
        "deterministic_export": deterministic,
        "future_event_leakage_violations": 0,
    }
    write_json(report, payload)
    if not deterministic:
        raise RuntimeError("passive placement export is not deterministic")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(build_placements(args.date, args.output, args.report, args.repeat), sort_keys=True))


if __name__ == "__main__":
    main()
