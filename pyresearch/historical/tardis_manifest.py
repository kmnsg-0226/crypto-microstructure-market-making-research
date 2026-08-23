"""Build a deterministic manifest for Tardis normalized L2 validation runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_if_changed(path: Path, payload: bytes) -> None:
    if path.exists() and path.read_bytes() == payload:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def build_manifest(data_dir: Path, reports_dir: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for report_path in sorted(reports_dir.glob("????-??-01.json")):
        report = json.loads(report_path.read_text())
        input_path = Path(report["input"])
        if not input_path.is_absolute():
            input_path = Path.cwd() / input_path
        if input_path.parent.resolve() != data_dir.resolve():
            raise ValueError(f"report input is outside data directory: {input_path}")
        date = report_path.stem
        entries.append(
            {
                "date": date,
                "source_url": (
                    "https://datasets.tardis.dev/v1/binance-futures/"
                    f"incremental_book_L2/{date[:4]}/{date[5:7]}/01/BTCUSDT.csv.gz"
                ),
                "path": str(input_path.relative_to(Path.cwd())),
                "bytes": input_path.stat().st_size,
                "sha256": _sha256(input_path),
                "report_path": str(report_path),
                "validation": report,
            }
        )

    if not entries:
        raise ValueError("no monthly validation reports found")

    total_span_us = sum(
        item["validation"]["last_local_timestamp_us"]
        - item["validation"]["first_local_timestamp_us"]
        for item in entries
    )
    reconnect_gap_us = sum(
        item["validation"]["conservative_reconnect_gap_us"] for item in entries
    )
    fields = (
        "rows",
        "message_groups",
        "snapshot_rows",
        "snapshot_groups",
        "delta_rows",
        "delta_messages",
        "valid_states",
        "invalid_states",
        "crossed_book_states",
        "empty_bid_states",
        "empty_ask_states",
        "exchange_timestamp_regressions",
        "local_timestamp_regressions",
        "parse_failures",
    )
    totals: dict[str, Any] = {
        field: sum(item["validation"][field] for item in entries) for field in fields
    }
    totals.update(
        {
            "compressed_bytes": sum(item["bytes"] for item in entries),
            "observed_span_us": total_span_us,
            "conservative_reconnect_gap_us": reconnect_gap_us,
            "conservative_valid_time_percentage": (
                100.0 * (total_span_us - reconnect_gap_us) / total_span_us
            ),
            "reconnect_snapshot_groups": totals["snapshot_groups"] - len(entries),
            "all_deterministic": all(
                item["validation"]["deterministic"] for item in entries
            ),
            "all_final_books_valid": all(
                item["validation"]["final_book_valid"] for item in entries
            ),
        }
    )
    return {
        "schema": "tardis-first-day-l2-validation-v1",
        "exchange": "binance-futures",
        "symbol": "BTCUSDT",
        "data_type": "incremental_book_L2",
        "ordering": "CSV row order; rows grouped by local_timestamp",
        "tick_size": "0.10",
        "step_size": "0.001",
        "entries": entries,
        "totals": totals,
        "limitations": [
            "Normalized CSV omits Binance U/u/pu sequence IDs.",
            "CSV omits disconnect markers; reconnect downtime is conservatively bounded "
            "from the previous message to the replacement snapshot.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--reports-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_manifest(args.data_dir, args.reports_dir)
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    _write_if_changed(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
