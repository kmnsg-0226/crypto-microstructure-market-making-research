from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import zstandard as zstd

from pyresearch.historical.cryptohftdata import CryptoHFTDataClient
from pyresearch.historical.l2 import (
    DepthMessage,
    audit_continuity,
    iter_hour_messages,
    pack_for_existing_cpp_pipeline,
)


def message(
    *,
    received: int,
    event_type: str,
    source: str,
    ordinal: int,
    first: int | None = None,
    final: int | None = None,
    previous: int | None = None,
    last: int | None = None,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
) -> DepthMessage:
    return DepthMessage(
        received_time_ns=received,
        event_time=received // 1_000_000,
        transaction_time=received // 1_000_000,
        symbol="BTCUSDT",
        event_type=event_type,
        first_update_id=first,
        final_update_id=final,
        prev_final_update_id=previous,
        last_update_id=last,
        source_object=source,
        source_ordinal=ordinal,
        bids=bids or [],
        asks=asks or [],
    )


class HistoricalL2Test(unittest.TestCase):
    def test_api_key_is_environment_only(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "CRYPTOHFTDATA_API_KEY"):
                CryptoHFTDataClient()

    def test_price_level_rows_group_and_sequence_tie_break(self) -> None:
        rows = [
            # Physical provider order regresses in received_time. The external
            # message sort must still restore time order deterministically.
            (2_000, 900, 899, "update", 102, 102, 101, None, "ask", "60000.30", "2.000"),
            (1_000, 900, 899, "update", 100, 101, 99, None, "bid", "60000.00", "1.000"),
            (1_000, 900, 899, "update", 100, 101, 99, None, "ask", "60000.20", "3.000"),
            (900, 900, 899, "update", 99, 99, 98, None, "bid", "59999.90", "4.000"),
        ]
        table = pa.table(
            {
                "received_time": [row[0] for row in rows],
                "event_time": [row[1] for row in rows],
                "transaction_time": [row[2] for row in rows],
                "symbol": ["BTCUSDT"] * len(rows),
                "event_type": [row[3] for row in rows],
                "first_update_id": [row[4] for row in rows],
                "final_update_id": [row[5] for row in rows],
                "prev_final_update_id": [row[6] for row in rows],
                "last_update_id": [row[7] for row in rows],
                "side": [row[8] for row in rows],
                "price": [row[9] for row in rows],
                "quantity": [row[10] for row in rows],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hour.parquet"
            pq.write_table(table, path)
            grouped = list(iter_hour_messages(path, "synthetic/hour"))
        self.assertEqual([item.final_update_id for item in grouped], [99, 101, 102])
        self.assertEqual(grouped[1].bids, [("60000.00", "1.000")])
        self.assertEqual(grouped[1].asks, [("60000.20", "3.000")])

    def test_continuity_audit_flags_stale_and_missing_ranges(self) -> None:
        messages = [
            message(received=1, event_type="update", source="h0", ordinal=0,
                    first=100, final=101, previous=99),
            message(received=2, event_type="update", source="h0", ordinal=1,
                    first=102, final=102, previous=101),
            message(received=3, event_type="update", source="h0", ordinal=2,
                    first=102, final=102, previous=101),
            message(received=4, event_type="update", source="h1", ordinal=0,
                    first=105, final=105, previous=104),
        ]
        with tempfile.TemporaryDirectory() as directory:
            summary = audit_continuity(messages, Path(directory) / "audit.jsonl.zst")
        self.assertEqual(summary.unique_depth_messages, 4)
        self.assertEqual(summary.duplicate_stale_messages, 1)
        self.assertEqual(summary.pu_breaks, 1)
        self.assertEqual(summary.missing_ranges[0]["after_u"], 102)
        self.assertEqual(summary.hourly_boundaries[-1]["status"], "pu_break")

    def test_existing_cpp_pipeline_adapter_is_deterministic(self) -> None:
        replay_binary = Path("build/cpp/crypto_replay")
        if not replay_binary.exists():
            self.skipTest("build/cpp/crypto_replay has not been built")
        snapshot = message(
            received=1_000_000_000,
            event_type="snapshot",
            source="synthetic/h0",
            ordinal=0,
            last=100,
            bids=[("60000.00", "1.000"), ("59999.90", "2.000")],
            asks=[("60000.10", "1.500"), ("60000.20", "2.000")],
        )
        updates = [
            message(
                received=1_100_000_000,
                event_type="update",
                source="synthetic/h0",
                ordinal=1,
                first=100,
                final=101,
                previous=100,
                bids=[("60000.00", "1.200")],
                asks=[("60000.20", "0.000")],
            ),
            message(
                received=1_200_000_000,
                event_type="update",
                source="synthetic/h1",
                ordinal=0,
                first=102,
                final=102,
                previous=101,
                bids=[("59999.90", "0.000")],
                asks=[("60000.10", "1.700")],
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "history.chft.zst"
            export_a = root / "a.csv.zst"
            export_b = root / "b.csv.zst"
            records = pack_for_existing_cpp_pipeline(
                [snapshot, *updates], snapshot, raw, end_ns=1_500_000_000
            )
            self.assertEqual(records, 5)

            def replay(output: Path) -> dict[str, object]:
                completed = subprocess.run(
                    [
                        str(replay_binary),
                        str(raw),
                        "--export",
                        str(output),
                        "--sample-ms",
                        "100",
                        "--start-ns",
                        "1100000000",
                        "--end-ns",
                        "1500000000",
                        "--include-invalid",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return json.loads(completed.stdout)

            first = replay(export_a)
            second = replay(export_b)
            self.assertEqual(first["final_checksum"], second["final_checksum"])
            self.assertEqual(first["research_rows"], 4)
            self.assertEqual(first["research_valid_rows"], 4)
            self.assertEqual(first["research_invalid_rows"], 0)
            self.assertEqual(hashlib.sha256(export_a.read_bytes()).digest(),
                             hashlib.sha256(export_b.read_bytes()).digest())
            with export_a.open("rb") as source, zstd.ZstdDecompressor().stream_reader(source) as reader:
                rows = list(csv.DictReader(io.TextIOWrapper(reader)))
            self.assertEqual(rows[0]["valid_book_state"], "1")
            self.assertEqual(rows[0]["best_bid_qty"], "1.200")
            self.assertEqual(rows[-1]["best_ask_qty"], "1.700")

    def test_cpp_adapter_invalidates_on_gap_and_recovers_only_from_snapshot(self) -> None:
        replay_binary = Path("build/cpp/crypto_replay")
        if not replay_binary.exists():
            self.skipTest("build/cpp/crypto_replay has not been built")
        snapshot = message(
            received=1_000_000_000,
            event_type="snapshot",
            source="synthetic/h0",
            ordinal=0,
            last=100,
            bids=[("60000.00", "1.000")],
            asks=[("60000.10", "1.000")],
        )
        events = [
            message(received=1_100_000_000, event_type="update", source="synthetic/h0",
                    ordinal=1, first=100, final=101, previous=99,
                    bids=[("60000.00", "1.100")]),
            message(received=1_200_000_000, event_type="update", source="synthetic/h0",
                    ordinal=2, first=104, final=104, previous=103,
                    asks=[("60000.10", "1.100")]),
            message(received=1_250_000_000, event_type="update", source="synthetic/h0",
                    ordinal=3, first=105, final=105, previous=104,
                    bids=[("60000.00", "1.200")]),
            message(received=1_300_000_000, event_type="snapshot", source="synthetic/h0",
                    ordinal=4, last=105, bids=[("60000.00", "1.200")],
                    asks=[("60000.10", "1.100")]),
            message(received=1_400_000_000, event_type="update", source="synthetic/h0",
                    ordinal=5, first=106, final=106, previous=105,
                    asks=[("60000.10", "1.300")]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "resync.chft.zst"
            export = root / "resync.csv.zst"
            pack_for_existing_cpp_pipeline(
                [snapshot, *events], snapshot, raw,
                start_ns=1_100_000_000, end_ns=1_500_000_000,
            )
            completed = subprocess.run(
                [str(replay_binary), str(raw), "--export", str(export),
                 "--sample-ms", "100", "--start-ns", "1100000000",
                 "--end-ns", "1500000000", "--include-invalid"],
                check=True, capture_output=True, text=True,
            )
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["sequence_gaps"], 1)
            self.assertEqual(summary["research_valid_rows"], 3)
            self.assertEqual(summary["research_invalid_rows"], 1)
            self.assertEqual(summary["invalid_book_intervals"], 1)
            with export.open("rb") as source, zstd.ZstdDecompressor().stream_reader(source) as reader:
                rows = list(csv.DictReader(io.TextIOWrapper(reader)))
            self.assertEqual([row["valid_book_state"] for row in rows], ["1", "0", "1", "1"])

    def test_cpp_adapter_uses_cached_snapshot_at_detected_gap(self) -> None:
        replay_binary = Path("build/cpp/crypto_replay")
        if not replay_binary.exists():
            self.skipTest("build/cpp/crypto_replay has not been built")
        snapshot = message(
            received=1_000_000_000, event_type="snapshot", source="synthetic/h0",
            ordinal=0, last=100, bids=[("60000.00", "1.000")],
            asks=[("60000.10", "1.000")],
        )
        events = [
            message(received=1_100_000_000, event_type="update", source="synthetic/h0",
                    ordinal=1, first=100, final=101, previous=99,
                    bids=[("60000.00", "1.100")]),
            message(received=1_150_000_000, event_type="snapshot", source="synthetic/h0",
                    ordinal=2, last=103, bids=[("60000.00", "1.200")],
                    asks=[("60000.10", "1.100")]),
            message(received=1_200_000_000, event_type="update", source="synthetic/h0",
                    ordinal=3, first=103, final=104, previous=102,
                    asks=[("60000.10", "1.200")]),
            message(received=1_300_000_000, event_type="update", source="synthetic/h0",
                    ordinal=4, first=105, final=105, previous=104,
                    bids=[("60000.00", "1.300")]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "cached-snapshot.chft.zst"
            export = root / "cached-snapshot.csv.zst"
            pack_for_existing_cpp_pipeline(
                [snapshot, *events], snapshot, raw,
                start_ns=1_100_000_000, end_ns=1_500_000_000,
            )
            completed = subprocess.run(
                [str(replay_binary), str(raw), "--export", str(export),
                 "--sample-ms", "100", "--start-ns", "1100000000",
                 "--end-ns", "1500000000", "--include-invalid"],
                check=True, capture_output=True, text=True,
            )
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["sequence_gaps"], 0)
            self.assertEqual(summary["applied_depth_events"], 3)
            self.assertEqual(summary["research_valid_rows"], 3)
            self.assertEqual(summary["research_invalid_rows"], 1)
            with export.open("rb") as source, zstd.ZstdDecompressor().stream_reader(source) as reader:
                rows = list(csv.DictReader(io.TextIOWrapper(reader)))
            self.assertEqual([row["valid_book_state"] for row in rows], ["1", "0", "1", "1"])


if __name__ == "__main__":
    unittest.main()
