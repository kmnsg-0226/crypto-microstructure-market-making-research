"""Resumable cache and deterministic manifest for free Tardis trade datasets."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import urllib.error
import urllib.request


DATASET_BASE = "https://datasets.tardis.dev/v1"
EXPECTED_HEADER = (
    "exchange",
    "symbol",
    "timestamp",
    "local_timestamp",
    "id",
    "side",
    "price",
    "amount",
)


@dataclass(frozen=True)
class TradeFileEntry:
    date: str
    source_url: str
    path: str
    compressed_bytes: int
    sha256: str
    rows: int
    first_timestamp_us: int
    last_timestamp_us: int
    first_local_timestamp_us: int
    last_local_timestamp_us: int
    timestamp_regressions: int
    local_timestamp_regressions: int
    buy_trades: int
    sell_trades: int
    unknown_side_trades: int
    buy_quantity: str
    sell_quantity: str


def _canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def _write_if_changed(path: Path, payload: bytes) -> None:
    if path.exists() and path.read_bytes() == payload:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def source_url(day: date) -> str:
    return (
        f"{DATASET_BASE}/binance-futures/trades/"
        f"{day:%Y/%m/%d}/BTCUSDT.csv.gz"
    )


def destination_path(root: Path, day: date) -> Path:
    return root / "binance-futures" / "trades" / f"{day:%Y}" / f"{day:%Y-%m-%d}-BTCUSDT.csv.gz"


def download(day: date, destination: Path, *, timeout: float = 120.0, attempts: int = 5) -> bool:
    if destination.exists() and destination.stat().st_size > 0:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    url = source_url(day)
    last_error: Exception | None = None
    for attempt in range(attempts):
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "crypto-hft-like-bot/1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                append = offset > 0 and response.status == 206
                before = offset if append else 0
                expected = response.headers.get("Content-Length")
                with partial.open("ab" if append else "wb") as output:
                    shutil.copyfileobj(response, output, 8 * 1024 * 1024)
                transferred = partial.stat().st_size - before
                if expected is not None and transferred != int(expected):
                    raise OSError(
                        f"incomplete body: expected {expected} bytes, received {transferred}"
                    )
            if partial.stat().st_size == 0:
                raise OSError("empty dataset response")
            os.replace(partial, destination)
            return True
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code == 404:
                raise FileNotFoundError(f"missing Tardis trade day: {day}") from error
            if error.code not in {408, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"trade download failed with HTTP {error.code}") from error
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            last_error = error
        if attempt + 1 < attempts:
            time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"trade download failed after {attempts} attempts") from last_error


def inspect(day: date, path: Path) -> TradeFileEntry:
    rows = 0
    first_timestamp = first_local = 0
    last_timestamp = last_local = 0
    timestamp_regressions = local_regressions = 0
    buy_trades = sell_trades = unknown_trades = 0
    buy_quantity = Decimal(0)
    sell_quantity = Decimal(0)
    with gzip.open(path, "rt", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != EXPECTED_HEADER:
            raise ValueError(f"unexpected Tardis trades schema: {reader.fieldnames}")
        for row in reader:
            if row["exchange"] != "binance-futures" or row["symbol"] != "BTCUSDT":
                raise ValueError("unexpected exchange or symbol in Tardis trades")
            timestamp = int(row["timestamp"])
            local_timestamp = int(row["local_timestamp"])
            amount = Decimal(row["amount"])
            if amount <= 0 or Decimal(row["price"]) <= 0:
                raise ValueError("non-positive trade price or amount")
            if rows == 0:
                first_timestamp = timestamp
                first_local = local_timestamp
            else:
                timestamp_regressions += timestamp < last_timestamp
                local_regressions += local_timestamp < last_local
            side = row["side"]
            if side == "buy":
                buy_trades += 1
                buy_quantity += amount
            elif side == "sell":
                sell_trades += 1
                sell_quantity += amount
            else:
                unknown_trades += 1
            rows += 1
            last_timestamp = timestamp
            last_local = local_timestamp
    if rows == 0:
        raise ValueError(f"empty Tardis trades dataset: {path}")
    return TradeFileEntry(
        date=day.isoformat(),
        source_url=source_url(day),
        path=str(path),
        compressed_bytes=path.stat().st_size,
        sha256=_sha256(path),
        rows=rows,
        first_timestamp_us=first_timestamp,
        last_timestamp_us=last_timestamp,
        first_local_timestamp_us=first_local,
        last_local_timestamp_us=last_local,
        timestamp_regressions=timestamp_regressions,
        local_timestamp_regressions=local_regressions,
        buy_trades=buy_trades,
        sell_trades=sell_trades,
        unknown_side_trades=unknown_trades,
        buy_quantity=format(buy_quantity, "f"),
        sell_quantity=format(sell_quantity, "f"),
    )


def update_manifest(root: Path, entries: list[TradeFileEntry]) -> Path:
    manifest_path = root / "binance-futures" / "trades" / "trades_manifest.json"
    existing: dict[str, dict[str, object]] = {}
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text())
        existing = {item["date"]: item for item in payload.get("entries", [])}
    for entry in entries:
        existing[entry.date] = asdict(entry)
    payload = {
        "schema": "tardis-trades-manifest-v1",
        "exchange": "binance-futures",
        "symbol": "BTCUSDT",
        "data_type": "trades",
        "aggressor_mapping": {
            "buy": "aggressive_buy",
            "sell": "aggressive_sell",
            "unknown": "rejected_for_research",
        },
        "entries": [existing[key] for key in sorted(existing)],
    }
    _write_if_changed(manifest_path, _canonical_json(payload))
    return manifest_path


def parse_day(text: str) -> date:
    return date.fromisoformat(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("data/historical/tardis"))
    parser.add_argument("--date", type=parse_day, action="append", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--attempts", type=int, default=5)
    args = parser.parse_args()
    entries = []
    for day in sorted(set(args.date)):
        path = destination_path(args.root, day)
        download(day, path, timeout=args.timeout, attempts=args.attempts)
        entry = inspect(day, path)
        if entry.unknown_side_trades:
            raise ValueError(
                f"{day} has {entry.unknown_side_trades} trades without aggressor side"
            )
        entries.append(entry)
    manifest = update_manifest(args.root, entries)
    print(json.dumps({"manifest": str(manifest), "entries": [asdict(x) for x in entries]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
