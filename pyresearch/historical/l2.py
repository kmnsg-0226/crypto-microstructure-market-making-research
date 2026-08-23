"""CryptoHFTData order-book grouping, sequence audit, and live-format adapter."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import heapq
import json
import os
from pathlib import Path
import pickle
import sqlite3
import struct
import tempfile
from typing import Iterable, Iterator

import pyarrow.compute as pc
import pyarrow.parquet as pq
import zstandard as zstd

from pyresearch.historical.cryptohftdata import CacheEntry, HourlyCache, HourlyObject


ORDERING_METHOD = (
    "received_time_ns ascending; messages sharing received_time are ordered by "
    "sequence anchor (snapshot last_update_id or update final_update_id), event type, "
    "first_update_id, prev_final_update_id, then source row ordinal"
)
REQUIRED_COLUMNS = (
    "received_time",
    "event_time",
    "transaction_time",
    "symbol",
    "event_type",
    "first_update_id",
    "final_update_id",
    "prev_final_update_id",
    "last_update_id",
    "side",
    "price",
    "quantity",
)


def _utc_ns(value: datetime) -> int:
    return int(value.timestamp()) * 1_000_000_000


def _to_exchange_ms(value: int | None) -> int:
    if value is None:
        return 0
    magnitude = abs(value)
    if magnitude >= 100_000_000_000_000_000:
        return value // 1_000_000
    if magnitude >= 100_000_000_000_000:
        return value // 1_000
    return value


@dataclass
class DepthMessage:
    received_time_ns: int
    event_time: int
    transaction_time: int | None
    symbol: str
    event_type: str
    first_update_id: int | None
    final_update_id: int | None
    prev_final_update_id: int | None
    last_update_id: int | None
    source_object: str
    source_ordinal: int
    bids: list[tuple[str, str]] = field(default_factory=list)
    asks: list[tuple[str, str]] = field(default_factory=list)

    @property
    def sequence_anchor(self) -> int:
        if self.event_type == "snapshot":
            return self.last_update_id or -1
        return self.final_update_id or -1

    @property
    def ordering_key(self) -> tuple[int, int, int, int, int, int]:
        type_rank = 0 if self.event_type == "snapshot" else 1
        return (
            self.received_time_ns,
            self.sequence_anchor,
            type_rank,
            self.first_update_id or -1,
            self.prev_final_update_id or -1,
            self.source_ordinal,
        )

    def add_level(self, side: str, price: str, quantity: str) -> None:
        level = (price, quantity)
        if side == "bid":
            self.bids.append(level)
        elif side == "ask":
            self.asks.append(level)
        else:
            raise ValueError(f"unexpected orderbook side: {side}")

    def snapshot_is_usable(self) -> bool:
        if self.event_type != "snapshot" or not self.last_update_id:
            return False
        bids = {Decimal(price): Decimal(qty) for price, qty in self.bids if Decimal(qty) > 0}
        asks = {Decimal(price): Decimal(qty) for price, qty in self.asks if Decimal(qty) > 0}
        return bool(bids and asks and max(bids) < min(asks))

    def binance_payload(self) -> str:
        if self.event_type == "snapshot":
            payload = {
                "E": _to_exchange_ms(self.event_time),
                "T": _to_exchange_ms(self.transaction_time),
                "asks": [list(level) for level in self.asks],
                "bids": [list(level) for level in self.bids],
                "lastUpdateId": self.last_update_id,
            }
        elif self.event_type == "update":
            payload = {
                "E": _to_exchange_ms(self.event_time),
                "T": _to_exchange_ms(self.transaction_time),
                "U": self.first_update_id,
                "a": [list(level) for level in self.asks],
                "b": [list(level) for level in self.bids],
                "e": "depthUpdate",
                "pu": self.prev_final_update_id,
                "s": self.symbol,
                "u": self.final_update_id,
            }
        else:
            raise ValueError(f"unexpected orderbook event type: {self.event_type}")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _message_identity(row: dict[str, object]) -> tuple[object, ...]:
    return (
        row["received_time"],
        row["event_time"],
        row["transaction_time"],
        row["symbol"],
        row["event_type"],
        row["first_update_id"],
        row["final_update_id"],
        row["prev_final_update_id"],
        row["last_update_id"],
    )


def iter_hour_messages(parquet_path: Path, source_object: str) -> Iterator[DepthMessage]:
    """Group price-level rows and externally sort messages by receive time and IDs."""
    parquet = pq.ParquetFile(parquet_path)
    missing = sorted(set(REQUIRED_COLUMNS) - set(parquet.schema_arrow.names))
    if missing:
        raise ValueError(f"orderbook parquet missing columns: {missing}")

    current_identity: tuple[object, ...] | None = None
    current_message: DepthMessage | None = None
    ordinal = 0

    with tempfile.TemporaryDirectory(prefix="cryptohft-l2-sort-") as directory:
        database = sqlite3.connect(Path(directory) / "messages.sqlite3")
        database.execute("PRAGMA journal_mode=OFF")
        database.execute("PRAGMA synchronous=OFF")
        database.execute("PRAGMA temp_store=FILE")
        database.execute(
            """CREATE TABLE messages (
                received_time INTEGER NOT NULL,
                sequence_anchor INTEGER NOT NULL,
                type_rank INTEGER NOT NULL,
                first_update_id INTEGER NOT NULL,
                prev_final_update_id INTEGER NOT NULL,
                source_ordinal INTEGER NOT NULL,
                payload BLOB NOT NULL
            )"""
        )
        pending: list[tuple[int, int, int, int, int, int, bytes]] = []

        def spool(message: DepthMessage) -> None:
            key = message.ordering_key
            pending.append((*key, pickle.dumps(message, protocol=5)))
            if len(pending) >= 4096:
                database.executemany(
                    "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", pending
                )
                pending.clear()

        for batch in parquet.iter_batches(batch_size=131_072, columns=list(REQUIRED_COLUMNS)):
            columns = batch.to_pydict()
            for index in range(batch.num_rows):
                row = {name: columns[name][index] for name in REQUIRED_COLUMNS}
                identity = _message_identity(row)
                if current_identity is not None and identity != current_identity:
                    assert current_message is not None
                    spool(current_message)
                    current_message = None
                current_identity = identity
                if current_message is None:
                    received = int(row["received_time"])
                    event_type = str(row["event_type"])
                    if event_type not in {"snapshot", "update"}:
                        raise ValueError(f"unsupported orderbook event type: {event_type}")
                    current_message = DepthMessage(
                        received_time_ns=received,
                        event_time=int(row["event_time"]),
                        transaction_time=(
                            int(row["transaction_time"])
                            if row["transaction_time"] is not None
                            else None
                        ),
                        symbol=str(row["symbol"]),
                        event_type=event_type,
                        first_update_id=(
                            int(row["first_update_id"])
                            if row["first_update_id"] is not None
                            else None
                        ),
                        final_update_id=(
                            int(row["final_update_id"])
                            if row["final_update_id"] is not None
                            else None
                        ),
                        prev_final_update_id=(
                            int(row["prev_final_update_id"])
                            if row["prev_final_update_id"] is not None
                            else None
                        ),
                        last_update_id=(
                            int(row["last_update_id"])
                            if row["last_update_id"] is not None
                            else None
                        ),
                        source_object=source_object,
                        source_ordinal=ordinal,
                    )
                    ordinal += 1
                current_message.add_level(
                    str(row["side"]), str(row["price"]), str(row["quantity"])
                )
        if current_message is not None:
            spool(current_message)
        if pending:
            database.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", pending)
        database.commit()
        cursor = database.execute(
            """SELECT payload FROM messages ORDER BY
               received_time, sequence_anchor, type_rank, first_update_id,
               prev_final_update_id, source_ordinal"""
        )
        for (payload,) in cursor:
            yield pickle.loads(payload)
        database.close()


def merge_ordered_hours(
    cache: HourlyCache, specs: Iterable[HourlyObject]
) -> Iterator[DepthMessage]:
    iterators = [
        iter_hour_messages(cache.parquet_path(spec), spec.object_path)
        for spec in sorted(specs)
    ]
    yield from heapq.merge(*iterators, key=lambda message: message.ordering_key)


def snapshot_row_count(parquet_path: Path) -> int:
    table = pq.read_table(parquet_path, columns=["event_type"])
    counts = {
        item["values"]: item["counts"]
        for item in pc.value_counts(table["event_type"]).to_pylist()
    }
    return int(counts.get("snapshot", 0))


@dataclass(frozen=True)
class SnapshotCandidate:
    source_object: str
    received_time_ns: int
    event_time: int
    last_update_id: int
    bid_levels: int
    ask_levels: int


@dataclass
class SnapshotSearch:
    snapshot: DepthMessage | None
    checked: list[dict[str, object]]
    entries: list[CacheEntry]


def discover_snapshot(
    cache: HourlyCache,
    start: datetime,
    *,
    max_lookback_hours: int = 24,
    allow_start_hour_snapshot: bool = False,
) -> SnapshotSearch:
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    start = start.astimezone(timezone.utc)
    start_ns = _utc_ns(start)
    checked: list[dict[str, object]] = []
    entries: list[CacheEntry] = []
    for lookback in range(max_lookback_hours + 1):
        hour = start - timedelta(hours=lookback)
        spec = HourlyObject("binance_futures", hour, "BTCUSDT", "orderbook")
        entry = cache.ensure(spec)
        entries.append(entry)
        count = snapshot_row_count(cache.parquet_path(spec))
        checked.append(
            {
                "hour_utc": spec.hour_utc,
                "object_path": spec.object_path,
                "parquet_rows": entry.parquet_rows,
                "snapshot_rows": count,
            }
        )
        if not count:
            continue
        if lookback == 0 and allow_start_hour_snapshot:
            lower_bound = start_ns
            upper_bound = start_ns + 3_600_000_000_000
        else:
            lower_bound = 0
            upper_bound = start_ns + 1
        candidates = [
            message
            for message in iter_hour_messages(cache.parquet_path(spec), spec.object_path)
            if message.event_type == "snapshot"
            and lower_bound <= message.received_time_ns < upper_bound
            and message.snapshot_is_usable()
        ]
        if candidates:
            selected = (
                min(candidates, key=lambda item: item.ordering_key)
                if lookback == 0 and allow_start_hour_snapshot
                else max(candidates, key=lambda item: item.ordering_key)
            )
            return SnapshotSearch(selected, checked, entries)
    return SnapshotSearch(None, checked, entries)


@dataclass
class ContinuitySummary:
    ordering_method: str = ORDERING_METHOD
    unique_depth_messages: int = 0
    update_messages: int = 0
    snapshot_messages: int = 0
    contiguous_updates: int = 0
    duplicate_stale_messages: int = 0
    pu_breaks: int = 0
    missing_ranges: list[dict[str, int]] = field(default_factory=list)
    first_update_id: int | None = None
    last_update_id: int | None = None
    first_received_time_ns: int | None = None
    last_received_time_ns: int | None = None
    hourly_boundaries: list[dict[str, object]] = field(default_factory=list)
    per_object: dict[str, dict[str, object]] = field(default_factory=dict)
    audit_sha256: str = ""


def audit_continuity(
    messages: Iterable[DepthMessage],
    audit_path: Path,
    *,
    initial_snapshot: DepthMessage | None = None,
) -> ContinuitySummary:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = audit_path.with_suffix(audit_path.suffix + ".part")
    summary = ContinuitySummary()
    previous_update_u: int | None = None
    initial_snapshot_id = initial_snapshot.last_update_id if initial_snapshot else None
    awaiting_initial_bridge = initial_snapshot_id is not None
    previous_update_received_ns: int | None = None
    previous_source: str | None = None
    first_in_source = True

    with temporary.open("wb") as raw_output:
        with zstd.ZstdCompressor(level=3).stream_writer(raw_output, closefd=False) as compressed:
            for message in messages:
                summary.unique_depth_messages += 1
                object_counts = summary.per_object.setdefault(
                    message.source_object,
                    {
                        "messages": 0,
                        "snapshots": 0,
                        "updates": 0,
                        "first_received_time_ns": message.received_time_ns,
                        "last_received_time_ns": message.received_time_ns,
                        "first_U": None,
                        "last_u": None,
                    },
                )
                object_counts["messages"] = int(object_counts["messages"]) + 1
                object_counts["last_received_time_ns"] = message.received_time_ns
                summary.first_received_time_ns = (
                    message.received_time_ns
                    if summary.first_received_time_ns is None
                    else min(summary.first_received_time_ns, message.received_time_ns)
                )
                summary.last_received_time_ns = message.received_time_ns
                if message.source_object != previous_source:
                    first_in_source = True
                    previous_source = message.source_object
                if message.event_type == "snapshot":
                    summary.snapshot_messages += 1
                    object_counts["snapshots"] = int(object_counts["snapshots"]) + 1
                    if not message.snapshot_is_usable() or message.last_update_id is None:
                        status = "snapshot_invalid"
                    elif (
                        awaiting_initial_bridge
                        and message.last_update_id == initial_snapshot_id
                    ):
                        status = "snapshot_duplicate"
                    elif (
                        previous_update_u is not None
                        and message.last_update_id <= previous_update_u
                    ):
                        status = "snapshot_overlap_or_stale"
                    else:
                        status = "snapshot_ahead_candidate"
                else:
                    summary.update_messages += 1
                    object_counts["updates"] = int(object_counts["updates"]) + 1
                    if None in (
                        message.first_update_id,
                        message.final_update_id,
                        message.prev_final_update_id,
                    ):
                        raise ValueError("update message is missing U, u, or pu")
                    assert message.first_update_id is not None
                    assert message.final_update_id is not None
                    assert message.prev_final_update_id is not None
                    if message.first_update_id > message.final_update_id:
                        raise ValueError("update message has U > u")
                    if summary.first_update_id is None:
                        summary.first_update_id = message.first_update_id
                    if object_counts["first_U"] is None:
                        object_counts["first_U"] = message.first_update_id
                    object_counts["last_u"] = message.final_update_id
                    expected_previous_u = previous_update_u
                    if awaiting_initial_bridge and initial_snapshot_id is not None:
                        expected_previous_u = initial_snapshot_id
                        if message.final_update_id < initial_snapshot_id:
                            status = "duplicate_stale_before_snapshot"
                            summary.duplicate_stale_messages += 1
                        elif (
                            message.first_update_id <= initial_snapshot_id
                            and initial_snapshot_id <= message.final_update_id
                        ):
                            status = "snapshot_bridge"
                            summary.contiguous_updates += 1
                            previous_update_u = message.final_update_id
                            awaiting_initial_bridge = False
                        else:
                            status = "snapshot_bridge_gap"
                            summary.pu_breaks += 1
                            missing = {
                                "after_u": initial_snapshot_id,
                                "next_U": message.first_update_id,
                                "next_u": message.final_update_id,
                                "next_pu": message.prev_final_update_id,
                                "previous_received_time_ns": previous_update_received_ns or 0,
                                "next_received_time_ns": message.received_time_ns,
                                "receive_gap_ns": (
                                    message.received_time_ns - previous_update_received_ns
                                    if previous_update_received_ns is not None
                                    else 0
                                ),
                                "missing_update_id_count": max(
                                    message.first_update_id - initial_snapshot_id - 1,
                                    0,
                                ),
                            }
                            summary.missing_ranges.append(missing)
                            previous_update_u = message.final_update_id
                            awaiting_initial_bridge = False
                    elif previous_update_u is None:
                        status = "initial_delta_only"
                        previous_update_u = message.final_update_id
                    elif message.final_update_id <= previous_update_u:
                        status = "duplicate_stale"
                        summary.duplicate_stale_messages += 1
                    elif message.prev_final_update_id == previous_update_u:
                        status = "contiguous"
                        summary.contiguous_updates += 1
                        previous_update_u = message.final_update_id
                    else:
                        status = "pu_break"
                        summary.pu_breaks += 1
                        missing = {
                            "after_u": previous_update_u,
                            "next_U": message.first_update_id,
                            "next_u": message.final_update_id,
                            "next_pu": message.prev_final_update_id,
                            "previous_received_time_ns": previous_update_received_ns or 0,
                            "next_received_time_ns": message.received_time_ns,
                            "receive_gap_ns": (
                                message.received_time_ns - previous_update_received_ns
                                if previous_update_received_ns is not None
                                else 0
                            ),
                            "missing_update_id_count": max(
                                message.prev_final_update_id - previous_update_u,
                                0,
                            ),
                        }
                        summary.missing_ranges.append(missing)
                        previous_update_u = message.final_update_id
                    summary.last_update_id = max(
                        summary.last_update_id or message.final_update_id,
                        message.final_update_id,
                    )
                    if first_in_source:
                        summary.hourly_boundaries.append(
                            {
                                "object_path": message.source_object,
                                "first_U": message.first_update_id,
                                "first_u": message.final_update_id,
                                "first_pu": message.prev_final_update_id,
                                "expected_previous_u": expected_previous_u,
                                "status": status,
                            }
                        )
                        first_in_source = False
                    previous_update_received_ns = message.received_time_ns
                record = {
                    "U": message.first_update_id,
                    "event_type": message.event_type,
                    "ordering_key": list(message.ordering_key),
                    "pu": message.prev_final_update_id,
                    "received_time_ns": message.received_time_ns,
                    "source_object": message.source_object,
                    "status": status,
                    "u": message.final_update_id,
                }
                compressed.write(
                    (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
                )
    os.replace(temporary, audit_path)
    digest = hashlib.sha256()
    with audit_path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    summary.audit_sha256 = digest.hexdigest()
    return summary


class ExistingRawFormatWriter:
    """Python adapter for the existing C++ CHFTL2R1 block format."""

    MAGIC = b"CHFTL2R1"

    def __init__(self, path: Path, block_target_bytes: int = 1 << 20) -> None:
        self.path = path
        self.temporary = path.with_suffix(path.suffix + ".part")
        self.temporary.parent.mkdir(parents=True, exist_ok=True)
        self.output = self.temporary.open("wb")
        self.output.write(self.MAGIC)
        self.output.write(struct.pack("<I", 1))
        self.block = bytearray()
        self.block_target_bytes = block_target_bytes
        self.records_written = 0

    @staticmethod
    def _string(value: str) -> bytes:
        encoded = value.encode()
        return struct.pack("<I", len(encoded)) + encoded

    def write(
        self,
        timestamp_ns: int,
        event_type: int,
        stream: str,
        payload: str,
        exchange_time_ms: int,
    ) -> None:
        encoded = struct.pack("<QQqB", timestamp_ns, timestamp_ns, exchange_time_ms, event_type)
        encoded += self._string("cryptohftdata-history")
        encoded += self._string(stream)
        encoded += self._string(payload)
        self.block += struct.pack("<I", len(encoded)) + encoded
        self.records_written += 1
        if len(self.block) >= self.block_target_bytes:
            self.flush_block()

    def flush_block(self) -> None:
        if not self.block:
            return
        compressed = zstd.ZstdCompressor(level=3).compress(bytes(self.block))
        self.output.write(struct.pack("<II", len(self.block), len(compressed)))
        self.output.write(compressed)
        self.block.clear()

    def close(self) -> None:
        self.flush_block()
        self.output.flush()
        self.output.close()
        os.replace(self.temporary, self.path)


def _instrument_info_json() -> str:
    payload = {
        "serverTime": 0,
        "symbols": [
            {
                "contractType": "PERPETUAL",
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "maxPrice": "1000000.00",
                        "minPrice": "0.10",
                        "tickSize": "0.10",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "maxQty": "1000.000",
                        "minQty": "0.001",
                        "stepSize": "0.001",
                    },
                ],
                "status": "TRADING",
                "symbol": "BTCUSDT",
            }
        ],
        "timezone": "UTC",
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def pack_for_existing_cpp_pipeline(
    messages: Iterable[DepthMessage],
    snapshot: DepthMessage | None,
    output_path: Path,
    *,
    start_ns: int | None = None,
    end_ns: int,
) -> int:
    if snapshot is None or not snapshot.snapshot_is_usable():
        raise RuntimeError("historical replay blocked: no usable snapshot")
    writer = ExistingRawFormatWriter(output_path)
    if start_ns is not None and start_ns + 1 < snapshot.received_time_ns:
        # Anchor the raw stream at the requested research start so the C++
        # exporter emits invalid rows while it is still waiting for a snapshot.
        base = start_ns
    else:
        base = max(snapshot.received_time_ns - 2, 0)
    writer.write(base, 3, "exchangeInfo", _instrument_info_json(), 0)
    writer.write(base + 1, 1, "btcusdt@depth@100ms", "{}", 0)
    writer.write(
        snapshot.received_time_ns,
        4,
        "historical/depth-snapshot",
        snapshot.binance_payload(),
        _to_exchange_ms(snapshot.event_time),
    )
    previous_u = snapshot.last_update_id
    snapshot_anchor = snapshot.last_update_id
    state = "awaiting_bridge"
    buffered_while_invalid: list[DepthMessage] = []
    pending_live_snapshot: DepthMessage | None = None
    deferred_live_updates: list[DepthMessage] = []
    last_update_received_ns: int | None = None

    def emit_update(message: DepthMessage) -> None:
        writer.write(
            message.received_time_ns,
            5,
            "btcusdt@depth@100ms",
            message.binance_payload(),
            _to_exchange_ms(message.event_time),
        )

    def flush_deferred_updates() -> None:
        nonlocal last_update_received_ns
        for deferred in deferred_live_updates:
            emit_update(deferred)
            last_update_received_ns = deferred.received_time_ns
        deferred_live_updates.clear()

    for message in messages:
        if message.ordering_key <= snapshot.ordering_key or message.received_time_ns >= end_ns:
            continue
        if message.event_type == "snapshot":
            if not message.snapshot_is_usable() or message.last_update_id is None:
                continue
            if state == "live":
                if message.last_update_id <= (previous_u or 0):
                    continue
                if pending_live_snapshot is not None:
                    if message.last_update_id == pending_live_snapshot.last_update_id:
                        continue
                    # Updates deferred after the earlier candidate were still
                    # contiguous and therefore belong to the current live book.
                    flush_deferred_updates()
                pending_live_snapshot = message
                continue
            if (
                state == "awaiting_bridge"
                and not buffered_while_invalid
                and message.last_update_id == snapshot_anchor
            ):
                continue
            writer.write(
                message.received_time_ns,
                4,
                "historical/depth-snapshot",
                message.binance_payload(),
                _to_exchange_ms(message.event_time),
            )
            snapshot_anchor = message.last_update_id
            previous_u = snapshot_anchor
            while (
                buffered_while_invalid
                and buffered_while_invalid[0].final_update_id is not None
                and buffered_while_invalid[0].final_update_id < snapshot_anchor
            ):
                buffered_while_invalid.pop(0)
            if not buffered_while_invalid:
                state = "awaiting_bridge"
                continue

            first = buffered_while_invalid.pop(0)
            assert first.first_update_id is not None
            assert first.final_update_id is not None
            if not (first.first_update_id <= snapshot_anchor <= first.final_update_id):
                # This matches DepthSynchronizer::detect_gap(): the offending
                # event remains buffered and all later buffered events are dropped.
                buffered_while_invalid = [first]
                state = "invalid"
                continue
            previous_u = first.final_update_id
            state = "live"
            for buffered in buffered_while_invalid:
                assert buffered.final_update_id is not None
                assert buffered.prev_final_update_id is not None
                if buffered.final_update_id <= previous_u:
                    continue
                if buffered.prev_final_update_id != previous_u:
                    buffered_while_invalid = [buffered]
                    state = "invalid"
                    break
                previous_u = buffered.final_update_id
            else:
                buffered_while_invalid.clear()
            continue
        if message.event_type != "update":
            continue
        if (
            message.first_update_id is None
            or message.final_update_id is None
            or message.prev_final_update_id is None
        ):
            raise RuntimeError("historical update missing sequence IDs")
        if state == "awaiting_bridge" and snapshot_anchor is not None:
            if message.final_update_id < snapshot_anchor:
                continue
            bridges = (
                message.first_update_id is not None
                and message.first_update_id <= snapshot_anchor <= message.final_update_id
            )
            emit_update(message)
            previous_u = message.final_update_id
            state = "live" if bridges else "invalid"
            if not bridges:
                buffered_while_invalid = [message]
            last_update_received_ns = message.received_time_ns
            continue
        if state == "invalid":
            emit_update(message)
            buffered_while_invalid.append(message)
            last_update_received_ns = message.received_time_ns
            continue
        if message.final_update_id <= (previous_u or 0):
            continue
        if message.prev_final_update_id == previous_u:
            previous_u = message.final_update_id
            if pending_live_snapshot is None:
                emit_update(message)
                last_update_received_ns = message.received_time_ns
            else:
                deferred_live_updates.append(message)
                if message.final_update_id >= pending_live_snapshot.last_update_id:
                    flush_deferred_updates()
                    pending_live_snapshot = None
            continue

        if (
            pending_live_snapshot is not None
            and message.first_update_id <= pending_live_snapshot.last_update_id
            and pending_live_snapshot.last_update_id <= message.final_update_id
        ):
            # A true delta gap is present, but the cached full snapshot covers
            # it and the current message is its valid Binance bridge. Flush all
            # still-contiguous updates, gate the unobserved interval, and feed
            # snapshot + bridge through the same C++ synchronizer used live.
            flush_deferred_updates()
            close_ns = min(
                (last_update_received_ns or message.received_time_ns) + 1,
                message.received_time_ns,
            )
            writer.write(close_ns, 2, "btcusdt@depth@100ms", "{}", 0)
            writer.write(
                message.received_time_ns, 1, "btcusdt@depth@100ms", "{}", 0
            )
            writer.write(
                message.received_time_ns,
                4,
                "historical/depth-snapshot",
                pending_live_snapshot.binance_payload(),
                _to_exchange_ms(pending_live_snapshot.event_time),
            )
            emit_update(message)
            previous_u = message.final_update_id
            last_update_received_ns = message.received_time_ns
            pending_live_snapshot = None
            deferred_live_updates.clear()
            continue

        flush_deferred_updates()
        pending_live_snapshot = None
        emit_update(message)
        state = "invalid"
        buffered_while_invalid = [message]
        previous_u = message.final_update_id
        last_update_received_ns = message.received_time_ns

    if state == "live" and deferred_live_updates:
        flush_deferred_updates()
    writer.close()
    return writer.records_written


def write_json(path: Path, payload: object) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    if path.exists() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def snapshot_candidate(message: DepthMessage) -> SnapshotCandidate:
    if not message.snapshot_is_usable() or message.last_update_id is None:
        raise ValueError("message is not a usable snapshot")
    return SnapshotCandidate(
        source_object=message.source_object,
        received_time_ns=message.received_time_ns,
        event_time=message.event_time,
        last_update_id=message.last_update_id,
        bid_levels=len(message.bids),
        ask_levels=len(message.asks),
    )
