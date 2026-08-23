"""Command-line entry points for the first historical L2 milestone."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile

import zstandard as zstd

from pyresearch.historical.cryptohftdata import CryptoHFTDataClient, HourlyCache, HourlyObject
from pyresearch.historical.l2 import (
    ORDERING_METHOD,
    audit_continuity,
    discover_snapshot,
    merge_ordered_hours,
    pack_for_existing_cpp_pipeline,
    snapshot_candidate,
    write_json,
)


def parse_utc(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include UTC timezone")
    value = value.astimezone(timezone.utc)
    if value.minute or value.second or value.microsecond:
        raise argparse.ArgumentTypeError("timestamp must align to an exact hour")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_replay(binary: Path, raw_path: Path, extra: list[str] | None = None) -> dict[str, object]:
    completed = subprocess.run(
        [str(binary), str(raw_path), *(extra or [])],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _inspect_export(
    path: Path, boundary_ns: list[int], sample_interval_ns: int
) -> dict[str, object]:
    first: dict[str, str] | None = None
    last: dict[str, str] | None = None
    invalid_intervals: list[dict[str, int]] = []
    invalid_start: int | None = None
    boundary_states: dict[str, dict[str, str] | None] = {
        str(value): None for value in boundary_ns
    }
    valid_bbo_strictly_ordered = True
    last_timestamp_ns: int | None = None
    with path.open("rb") as source, zstd.ZstdDecompressor().stream_reader(source) as reader:
        for row in csv.DictReader(io.TextIOWrapper(reader)):
            timestamp_ns = int(row["timestamp_ns"])
            last_timestamp_ns = timestamp_ns
            if str(timestamp_ns) in boundary_states:
                boundary_states[str(timestamp_ns)] = {
                    "timestamp_ns": row["timestamp_ns"],
                    "valid_book_state": row["valid_book_state"],
                    "sequence_update_id": row["sequence_update_id"],
                    "best_bid_price": row["best_bid_price"],
                    "best_ask_price": row["best_ask_price"],
                }
            if row["valid_book_state"] != "1":
                if invalid_start is None:
                    invalid_start = timestamp_ns
                continue
            if invalid_start is not None:
                invalid_intervals.append(
                    {
                        "start_ns": invalid_start,
                        "end_ns": timestamp_ns,
                        "duration_ns": timestamp_ns - invalid_start,
                    }
                )
                invalid_start = None
            selected = {
                key: row[key]
                for key in (
                    "timestamp_ns",
                    "best_bid_price",
                    "best_bid_qty",
                    "best_ask_price",
                    "best_ask_qty",
                )
            }
            valid_bbo_strictly_ordered = valid_bbo_strictly_ordered and (
                Decimal(row["best_bid_price"]) < Decimal(row["best_ask_price"])
            )
            if first is None:
                first = selected
            last = selected
    if invalid_start is not None:
        invalid_end = (
            last_timestamp_ns + sample_interval_ns
            if last_timestamp_ns is not None
            else invalid_start + sample_interval_ns
        )
        invalid_intervals.append(
            {
                "start_ns": invalid_start,
                "end_ns": invalid_end,
                "duration_ns": invalid_end - invalid_start,
            }
        )
    return {
        "first_bbo": first,
        "last_bbo": last,
        "valid_bbo_strictly_ordered": valid_bbo_strictly_ordered,
        "boundary_states": boundary_states,
        "invalid_intervals": invalid_intervals,
        "invalid_book_duration_ns": sum(
            item["duration_ns"] for item in invalid_intervals
        ),
    }


def _reconstruct(
    args: argparse.Namespace,
    cache: HourlyCache,
    specs: list[HourlyObject],
    snapshot,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    start_ns = int(start.timestamp()) * 1_000_000_000
    end_ns = int(end.timestamp()) * 1_000_000_000
    raw_path = args.output_root / "historical_stage_ab.chft.zst"
    message_factory = lambda: (
        item
        for item in merge_ordered_hours(cache, specs)
        if item.ordering_key >= snapshot.ordering_key
    )
    packed_records = pack_for_existing_cpp_pipeline(
        message_factory(), snapshot, raw_path, start_ns=start_ns, end_ns=end_ns
    )
    proof = _run_replay(args.replay_binary, raw_path)
    proof_valid = (
        proof["parse_failures"] == 0
        and proof["crossed_book_states"] == 0
        and proof["empty_bid_states"] == 0
        and proof["empty_ask_states"] == 0
        and proof["best_bid_ticks"] is not None
        and proof["best_ask_ticks"] is not None
    )
    if not proof_valid:
        return {
            "attempted": True,
            "proven": False,
            "packed_records": packed_records,
            "raw_path": str(raw_path),
            "proof": proof,
            "reason": "existing C++ OrderBook replay did not remain valid",
        }

    export_path = args.output_root / "validation_100ms.csv.zst"
    common = [
        "--sample-ms", "100",
        "--start-ns", str(start_ns),
        "--end-ns", str(end_ns),
        "--include-invalid",
    ]
    first_summary = _run_replay(
        args.replay_binary, raw_path, ["--export", str(export_path), *common]
    )
    with tempfile.TemporaryDirectory() as directory:
        repeat_path = Path(directory) / "validation_100ms.csv.zst"
        second_summary = _run_replay(
            args.replay_binary, raw_path, ["--export", str(repeat_path), *common]
        )
        repeat_sha = _sha256(repeat_path)
    export_sha = _sha256(export_path)
    deterministic = first_summary == second_summary and export_sha == repeat_sha
    boundary_ns = []
    cursor = start + timedelta(hours=1)
    while cursor < end:
        boundary_ns.append(int(cursor.timestamp()) * 1_000_000_000)
        cursor += timedelta(hours=1)
    export_inspection = _inspect_export(export_path, boundary_ns, 100_000_000)
    rows = int(first_summary["research_rows"])
    valid_rows = int(first_summary["research_valid_rows"])
    invalid_rows = int(first_summary["research_invalid_rows"])
    structurally_valid = (
        int(first_summary["crossed_book_states"]) == 0
        and int(first_summary["empty_bid_states"]) == 0
        and int(first_summary["empty_ask_states"]) == 0
        and bool(export_inspection["valid_bbo_strictly_ordered"])
    )
    boundary_continuity_proven = all(
        state is not None and state["valid_book_state"] == "1"
        for state in export_inspection["boundary_states"].values()
    )
    return {
        "attempted": True,
        "proven": (
            deterministic
            and structurally_valid
            and boundary_continuity_proven
            and valid_rows > 0
        ),
        "packed_records": packed_records,
        "raw_path": str(raw_path),
        "raw_sha256": _sha256(raw_path),
        "validation_export_path": str(export_path),
        "validation_export_sha256": export_sha,
        "validation_export_rows": rows,
        "valid_book_rows": valid_rows,
        "invalid_book_rows": invalid_rows,
        "valid_book_percentage": (100.0 * valid_rows / rows) if rows else 0.0,
        "invalid_book_intervals": first_summary["invalid_book_intervals"],
        "invalid_book_interval_details": export_inspection["invalid_intervals"],
        "invalid_book_duration_ns": export_inspection["invalid_book_duration_ns"],
        "crossed_book_states": first_summary["crossed_book_states"],
        "empty_bid_states": first_summary["empty_bid_states"],
        "empty_ask_states": first_summary["empty_ask_states"],
        "deterministic_replay": deterministic,
        "final_checksum": first_summary["final_checksum"],
        "valid_bbo_strictly_ordered": export_inspection["valid_bbo_strictly_ordered"],
        "hour_boundary_continuity_proven": boundary_continuity_proven,
        "hour_boundary_states": export_inspection["boundary_states"],
        "first_bbo": export_inspection["first_bbo"],
        "last_bbo": export_inspection["last_bbo"],
        "proof": proof,
    }


def stage_ab(args: argparse.Namespace) -> int:
    start: datetime = args.start
    end: datetime = args.end
    if end <= start:
        raise ValueError("end must be after start")
    cache = HourlyCache(args.cache_root, CryptoHFTDataClient())

    target_specs: list[HourlyObject] = []
    cursor = start
    while cursor < end:
        target_specs.append(HourlyObject("binance_futures", cursor, "BTCUSDT", "orderbook"))
        cursor += timedelta(hours=1)
    target_entries = [cache.ensure(spec) for spec in target_specs]

    search = discover_snapshot(
        cache,
        start,
        max_lookback_hours=args.lookback_hours,
        allow_start_hour_snapshot=args.allow_start_hour_snapshot,
    )
    entries_by_path = {entry.object_path: entry for entry in search.entries + target_entries}
    cache.write_manifest(args.output_root / "hourly_manifest.json", list(entries_by_path.values()))

    boundary_spec = HourlyObject(
        "binance_futures", start - timedelta(hours=1), "BTCUSDT", "orderbook"
    )
    boundary_entry = cache.ensure(boundary_spec)
    entries_by_path[boundary_entry.object_path] = boundary_entry

    if search.snapshot:
        snapshot_hour = datetime.fromtimestamp(
            search.snapshot.received_time_ns / 1_000_000_000, tz=timezone.utc
        ).replace(minute=0, second=0, microsecond=0)
        audit_specs = []
        cursor = snapshot_hour
        while cursor < end:
            audit_specs.append(HourlyObject("binance_futures", cursor, "BTCUSDT", "orderbook"))
            cursor += timedelta(hours=1)
    else:
        audit_specs = [
            boundary_spec,
            *target_specs,
        ]
    for spec in audit_specs:
        entry = cache.ensure(spec)
        entries_by_path[entry.object_path] = entry
    cache.write_manifest(args.output_root / "hourly_manifest.json", list(entries_by_path.values()))

    audit_path = args.output_root / "sequence_audit.jsonl.zst"
    ordered = merge_ordered_hours(cache, audit_specs)
    if search.snapshot:
        ordered = (
            item for item in ordered if item.ordering_key >= search.snapshot.ordering_key
        )
    continuity = audit_continuity(
        ordered,
        audit_path,
        initial_snapshot=search.snapshot,
    )

    reconstruction: dict[str, object]
    status: str
    if search.snapshot is None:
        status = "blocked_no_snapshot"
        reconstruction = {
            "attempted": False,
            "proven": False,
            "valid_book_percentage": 0.0,
            "invalid_book_intervals": 1,
            "invalid_book_duration_ns": int((end - start).total_seconds() * 1_000_000_000),
            "crossed_book_states": None,
            "empty_bid_states": None,
            "empty_ask_states": None,
            "deterministic_replay": None,
            "final_checksum": None,
            "first_bbo": None,
            "last_bbo": None,
            "validation_export_rows": 0,
        }
    else:
        reconstruction = _reconstruct(
            args, cache, audit_specs, search.snapshot, start, end
        )
        status = (
            "reconstruction_proven"
            if reconstruction["proven"]
            else "blocked_sequence_discontinuity"
        )

    matching_trade_entries = []
    if status == "reconstruction_proven":
        for spec in target_specs:
            trade_spec = HourlyObject(spec.exchange, spec.hour, spec.symbol, "trades")
            trade_entry = cache.ensure(trade_spec)
            entries_by_path[trade_entry.object_path] = trade_entry
            matching_trade_entries.append(asdict(trade_entry))
        cache.write_manifest(
            args.output_root / "hourly_manifest.json", list(entries_by_path.values())
        )

    if search.snapshot is None:
        reason = (
            f"No usable snapshot was found from the research start through "
            f"{args.lookback_hours} UTC hours of lookback. Delta-only replay is invalid."
        )
    elif status != "reconstruction_proven":
        reason = (
            "Snapshots permit deterministic reconstruction inside covered segments, "
            "but genuine delta gaps leave one or more UTC hour boundaries invalid. "
            "Matching trades were therefore not downloaded."
        )
    else:
        reason = None

    report = {
        "format_version": 1,
        "symbol": "BTCUSDT",
        "exchange": "binance_futures",
        "research_start_utc": start.isoformat().replace("+00:00", "Z"),
        "research_end_utc": end.isoformat().replace("+00:00", "Z"),
        "ordering_method": ORDERING_METHOD,
        "snapshot_search": search.checked,
        "snapshot": asdict(snapshot_candidate(search.snapshot)) if search.snapshot else None,
        "continuity": asdict(continuity),
        "status": status,
        "cpp_raw_path": reconstruction.get("raw_path"),
        "research_export_path": reconstruction.get("validation_export_path"),
        "matching_trades": matching_trade_entries,
        "reconstruction": reconstruction,
        "reason": reason,
    }
    report_path = args.output_root / "stage_ab_report.json"
    write_json(report_path, report)
    print(json.dumps({
        "status": status,
        "report_path": str(report_path),
        "snapshot": report["snapshot"],
        "pu_breaks": continuity.pu_breaks,
        "reconstruction": {
            key: reconstruction.get(key)
            for key in (
                "proven",
                "deterministic_replay",
                "final_checksum",
                "valid_book_percentage",
                "invalid_book_intervals",
                "invalid_book_duration_ns",
                "hour_boundary_continuity_proven",
                "validation_export_rows",
            )
        },
    }, indent=2, sort_keys=True))
    if status != "reconstruction_proven":
        return 4
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage = subparsers.add_parser("stage-ab", help="run bounded snapshot discovery and sequence audit")
    stage.add_argument("--start", type=parse_utc, default=parse_utc("2026-03-23T00:00:00Z"))
    stage.add_argument("--end", type=parse_utc, default=parse_utc("2026-03-23T02:00:00Z"))
    stage.add_argument("--lookback-hours", type=int, default=24)
    stage.add_argument("--cache-root", type=Path, default=Path("data/historical/cryptohftdata"))
    stage.add_argument("--output-root", type=Path, default=Path("data/historical/validation/stage_ab"))
    stage.add_argument("--replay-binary", type=Path, default=Path("build/cpp/crypto_replay"))
    stage.add_argument("--allow-start-hour-snapshot", action="store_true")
    stage.set_defaults(function=stage_ab)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
