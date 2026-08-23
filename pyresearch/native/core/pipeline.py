"""Build the native_dev_v1 dataset, QC artifacts and baseline diagnostics.

    python -m pyresearch.native.core.pipeline export       # replay raw -> dataset + per-file QC
    python -m pyresearch.native.core.pipeline freeze       # write research/specs/native_dev_v1.json
    python -m pyresearch.native.core.pipeline diagnose     # summaries, IC and bucket studies
    python -m pyresearch.native.core.pipeline all
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd

from pyresearch.native.core import corpus, diagnostics

BINARY = corpus.ROOT / "build/cpp/native_dataset_export"
FLOAT_FORMAT = "%.10g"


def export(force: bool = False) -> None:
    if not BINARY.exists():
        raise SystemExit(f"missing {BINARY}; build it with cmake --build build/cpp")
    corpus.DATASET_DIR.mkdir(parents=True, exist_ok=True)
    corpus.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    for entry in corpus.CORPUS:
        if entry.dataset_path.exists() and entry.qc_path.exists() and not force:
            print(f"skip {entry.name} (already exported)")
            continue
        print(f"export {entry.name}")
        # One raw file per invocation: the three files are separate collector processes and
        # replaying them as a chain would carry book state across a real collection gap.
        result = subprocess.run(
            [
                str(BINARY),
                str(entry.raw_path),
                "--dataset",
                str(entry.dataset_path),
                "--qc",
                str(entry.qc_path),
                "--file-index",
                str(entry.file_index),
            ],
            cwd=corpus.ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            raise SystemExit(f"QC failure replaying {entry.name} (exit {result.returncode})")


def write_qc_artifacts() -> pd.DataFrame:
    """Merge the per-file QC reports into qc.json and segments.csv."""
    reports = corpus.load_qc()
    merged = {
        "schema": "crypto-hft-native-qc-merged-v1",
        "corpus_id": "native_dev_v1",
        "development_only": True,
        "files": reports,
        "totals": {
            "raw_records": sum(r["file"]["raw_records"] for r in reports),
            "depth_events": sum(r["file"]["depth_events"] for r in reports),
            "trade_events": sum(r["file"]["trade_events"] for r in reports),
            "snapshots": sum(r["file"]["snapshots"] for r in reports),
            "sequence_gaps": sum(r["file"]["sequence_gaps"] for r in reports),
            "parse_failures": sum(r["file"]["parse_failures"] for r in reports),
            "crossed_book_states": sum(r["file"]["crossed_book_states"] for r in reports),
            "empty_bid_states": sum(r["file"]["empty_bid_states"] for r in reports),
            "empty_ask_states": sum(r["file"]["empty_ask_states"] for r in reports),
            "segments": sum(r["dataset"]["segments"] for r in reports),
            "rows": sum(r["dataset"]["rows"] for r in reports),
            "valid_research_s": sum(r["dataset"]["valid_research_s"] for r in reports),
            "captured_span_s": sum(r["file"]["duration_s"] for r in reports),
        },
        "failures": sorted({failure for r in reports for failure in r["failures"]}),
    }
    (corpus.REPORT_DIR / "qc.json").write_text(
        json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    rows = []
    for report in reports:
        for segment in report["segments"]:
            rows.append(
                {
                    "file_index": report["file_index"],
                    "raw_file": report["raw_file"],
                    "segment_id": segment["segment_id"],
                    "global_segment_key": f"{report['file_index']}:{segment['segment_id']}",
                    "start_ns": segment["start_ns"],
                    "end_ns": segment["end_ns"],
                    "start_utc": _utc(segment["start_ns"]),
                    "end_utc": _utc(segment["end_ns"]),
                    "duration_s": segment["duration_s"],
                    "depth_events": segment["depth_events"],
                    "trade_events": segment["trade_events"],
                    "rows": segment["rows"],
                    "first_update_id": segment["first_update_id"],
                    "final_update_id": segment["final_update_id"],
                    "bid_full_fills": segment["bid_full_fills"],
                    "ask_full_fills": segment["ask_full_fills"],
                    "close_reason": segment["close_reason"],
                }
            )
    segments = pd.DataFrame(rows).sort_values(
        ["file_index", "segment_id"], ignore_index=True
    )
    segments.to_csv(corpus.REPORT_DIR / "segments.csv", index=False)
    return segments


def _utc(nanoseconds: int) -> str:
    return (
        datetime.fromtimestamp(nanoseconds / 1e9, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def write_schema() -> None:
    header = _read_header(corpus.CORPUS[0].dataset_path)
    report = corpus.load_qc()[0]
    qc = report["dataset"]
    schema = {
        "schema": "crypto-hft-native-dataset-schema-v1",
        "dataset_id": "native_features_100ms",
        # Constant across the whole corpus, so recorded here and in the frozen spec rather than
        # repeated on all 2.5 million rows.
        "source": report["source"],
        "collector_location": "aws_london",
        "symbol": report["symbol"],
        "tick_size": report["tick_size"],
        "step_size": report["step_size"],
        "development_only": True,
        "storage": {
            "format": "csv.zst",
            "one_file_per_raw_capture": True,
            "location": str(corpus.DATASET_DIR.relative_to(corpus.ROOT)),
            "row_key": ["file_index", "timestamp_ns"],
            "segment_key": ["file_index", "segment_id"],
            "missing_value": "empty field",
            "determinism": "byte-identical on repeated runs over identical raw input",
        },
        "units": {
            "price": "exchange ticks (tick size 0.10 USDT)",
            "quantity": "exchange quantity steps (step size 0.001 BTC)",
            "time": "nanoseconds since the Unix epoch, local receive clock",
            "markout": "ticks",
        },
        "grid": {
            "decision_interval_ms": qc["grid_ms"],
            "alignment": "absolute multiples of the interval since the Unix epoch",
            "sample_content": "every raw event with a receive timestamp in (t - w, t]",
        },
        "passive_quote_assumptions": {
            "order_lots": qc["order_lots"],
            "quote_price": "the best price on that side at the decision instant",
            "initial_queue_ahead": "the entire displayed quantity at the quote price",
            "later_additions": "assumed to queue behind the hypothetical order",
            "displayed_quantity_decreases": "never advance the queue",
            "queue_advance": "only aggressive prints at exactly the quote price",
            "trade_through": "a print beyond the quote price fills the order completely",
            "true_queue_position": "not observable from aggregated L2 and never claimed",
            "observation_window_ms": qc["fill_horizon_ms"],
            "adverse_threshold_half_ticks": qc["adverse_threshold_half_ticks"],
        },
        "boundary_rules": {
            "segment": "maximal interval with a synchronized depth book and a connected "
            "aggressive-trade stream",
            "features": "trailing windows are cleared at every segment start",
            "targets": "a horizon that would cross a segment end is left empty, never zero",
            "fills": "pending hypothetical orders are abandoned at a segment end",
        },
        "column_count": len(header),
        "columns": header,
    }
    (corpus.REPORT_DIR / "dataset_schema.json").write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _read_header(path: Path) -> list[str]:
    import zstandard as zstd

    with path.open("rb") as handle:
        with zstd.ZstdDecompressor().stream_reader(handle) as stream:
            line = b""
            while not line.endswith(b"\n"):
                chunk = stream.read(4096)
                if not chunk:
                    break
                line += chunk
    return line.split(b"\n")[0].decode("utf-8").split(",")


def load_diagnostic_frame() -> pd.DataFrame:
    frames = []
    for entry in corpus.CORPUS:
        frame = diagnostics.read_columns(
            entry.dataset_path, diagnostics.DIAGNOSTIC_COLUMNS
        )
        frames.append(frame)
    return diagnostics.add_derived(pd.concat(frames, ignore_index=True))


def diagnose() -> None:
    frame = load_diagnostic_frame()
    feature_columns = (
        diagnostics.STATE_COLUMNS
        + diagnostics.FLOW_COLUMNS
        + [name for name in diagnostics.SIGNALS if name not in diagnostics.STATE_COLUMNS]
    )
    seen: set[str] = set()
    ordered = [name for name in feature_columns if not (name in seen or seen.add(name))]
    diagnostics.describe(frame, ordered).to_csv(
        corpus.REPORT_DIR / "feature_summary.csv", index=False, float_format=FLOAT_FORMAT
    )
    diagnostics.describe(frame, diagnostics.TARGET_COLUMNS).to_csv(
        corpus.REPORT_DIR / "target_summary.csv", index=False, float_format=FLOAT_FORMAT
    )
    diagnostics.passive_summary(frame).to_csv(
        corpus.REPORT_DIR / "passive_summary.csv", index=False, float_format=FLOAT_FORMAT
    )
    diagnostics.information_coefficients(frame).to_csv(
        corpus.REPORT_DIR / "information_coefficients.csv",
        index=False,
        float_format=FLOAT_FORMAT,
    )
    diagnostics.bucket_study(frame).to_csv(
        corpus.REPORT_DIR / "bucket_study.csv", index=False, float_format=FLOAT_FORMAT
    )
    print(f"diagnostics written for {len(frame):,} rows")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", choices=("export", "freeze", "qc", "schema", "diagnose", "all")
    )
    parser.add_argument("--force", action="store_true", help="re-export existing datasets")
    arguments = parser.parse_args()

    if arguments.stage in ("export", "all"):
        export(force=arguments.force)
    if arguments.stage in ("qc", "all"):
        write_qc_artifacts()
    if arguments.stage in ("schema", "all"):
        write_schema()
    if arguments.stage in ("freeze", "all"):
        created = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        corpus.freeze(created)
    if arguments.stage in ("diagnose", "all"):
        diagnose()


if __name__ == "__main__":
    main()
