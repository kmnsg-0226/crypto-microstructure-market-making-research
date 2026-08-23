# Raw file rotation

The Binance USD-M collector writes one `.chft.zst` file per UTC day without interrupting
capture. Rotation is a **storage boundary only**: the WebSocket connections, the order book,
the depth synchroniser and the `U`/`u`/`pu` continuity state are untouched.

## Why rotation does not restart the WebSocket

Restarting the process (or reconnecting the streams) at midnight would force a full
resynchronisation: a new REST snapshot, a fresh `U`/`u` alignment, and a window of buffered or
discarded depth events around the boundary. That window is a hole in the research data every
single day, and it costs the very continuity the collector exists to preserve.

Instead the file boundary lives inside `RawEventWriter`. `write()` already serialises every
record under one mutex, so it is the only place in the process where "between two complete
records" is a well-defined instant. At that instant, and only there, the writer closes the
current zstd container and opens the next one. The ingest threads never block on anything
slower than a file open, and no other component learns that a boundary happened.

## UTC daily semantics

A record's file is chosen by `floor(local_system_ns / interval_ns)`. With a 24 hour interval
this lands exactly on UTC midnight, because the Unix epoch is itself UTC-midnight aligned. The
filename is the bucket start, not the first record's time:

```
BTCUSDT-LONDON-20260820T000000Z.chft.zst
```

Consequences worth knowing:

- Rotation is **driven by record arrival**. The first record whose timestamp falls in the new
  day closes the old file. At production event rates this is a few milliseconds after midnight;
  a day with no events produces no file at all, and an idle gap over midnight simply rotates on
  the next arriving record.
- Rotation never runs backwards. If the system clock steps backwards, the writer keeps the
  current file rather than reopening an earlier one.
- Records from the depth and aggTrade threads can interleave by a fraction of a millisecond, so
  a straggler stamped just before midnight can be serialised just after the boundary and land in
  the new file. Nothing is lost, duplicated or reordered relative to write order — this is the
  same interleaving that already exists inside a single unrotated file.
- If a file with the target name already exists (a restart inside the same day), the writer
  appends `-1`, `-2`, … rather than truncating captured data.

## Continuation files, and why no synthetic snapshot is injected

A `.chft.zst` file is a verbatim record of what arrived on the wire. Injecting a synthetic order
book snapshot at the head of each rotated file would make the file self-contained, but it would
also put a record into the archive that the exchange never sent, at a timestamp nothing was
received at. Research downstream could no longer distinguish captured state from reconstructed
state.

So every file after the first is a **continuation file**:

> A rotated continuation file is not necessarily independently replayable. Replay the
> chronological chain beginning from an independent file/snapshot.

The initial file of a collector run is independent: it opens with the instrument metadata
record, a `ConnectionOpen`, and the REST bootstrap snapshot. Continuation files have none of
those, and their manifests carry `continuation: true` and `requires_previous_file: true`.

## Multi-file replay

`crypto_replay` accepts a chain, in chronological order, either positionally or with `--input`:

```bash
./build/cpp/crypto_replay \
  data/raw/BTCUSDT-LONDON-20260819T000000Z.chft.zst \
  data/raw/BTCUSDT-LONDON-20260820T000000Z.chft.zst \
  --symbol BTCUSDT --export research.csv.zst
```

The book, the depth synchroniser and the research exporter are constructed once and carried
across every file boundary, so the chain replays exactly as the unrotated stream would.
Reconnect and resnapshot records stored *inside* the raw data behave as they always have.

Before replaying, the chain is validated:

- every input must exist;
- if the first file has a manifest marking it a continuation, replay refuses and names the
  predecessor to start from;
- each subsequent manifest's `previous_file` must be the preceding input's filename.

Files without manifests are not rejected — the chain then falls back to the requirement that the
first file contain instrument metadata, which only an independent file has.

## Manifest

Each closed raw file gets a sidecar `<file>.manifest.json`, written only after the raw file is
closed, and published atomically (temporary file → `fsync` → `rename`).

```json
{
  "schema": "chft-raw-manifest",
  "schema_version": 1,
  "symbol": "BTCUSDT",
  "venue": "Binance USD-M",
  "filename": "BTCUSDT-LONDON-20260820T000000Z.chft.zst",
  "file_start_receive_ns": 1755648000012345678,
  "file_start_receive_utc": "2026-08-20T00:00:00.012345678Z",
  "file_end_receive_ns": 1755734399987654321,
  "file_end_receive_utc": "2026-08-20T23:59:59.987654321Z",
  "file_size_bytes": 1443889201,
  "sha256": "9f2c…",
  "previous_file": "BTCUSDT-LONDON-20260819T000000Z.chft.zst",
  "continuation": true,
  "requires_previous_file": true,
  "start_update_id": 7401220031,
  "end_update_id": 7409981774,
  "hostname": "ip-10-0-0-31",
  "build_version": "508bc31",
  "counters": {
    "raw_records": 18442310,
    "depth_events": 8640122,
    "trade_events": 9801955,
    "snapshots": 3,
    "reconnects": 2,
    "sequence_gaps": 1,
    "parse_failures": 0,
    "invalid_records": 0,
    "dropped_records": 0,
    "snapshot_failures": 0,
    "fatal_errors": 0
  }
}
```

All counters are **per-file deltas**, never lifetime totals. `raw_records`, `depth_events`,
`trade_events` and `snapshots` are counted by the writer from the records it actually wrote, so
they are exact. The remaining counters are deltas of the collector's process counters sampled at
each boundary; a handful of events at the boundary may be attributed to the adjacent file,
because those counters are incremented by the ingest threads just after the record is handed to
the writer. `build_version` is the git commit resolved at CMake configure time — re-run `cmake`
to refresh it.

`previous_file` is `null` for an independent initial file. `requires_previous_file` is
conservative: it mirrors `continuation`, because the writer cannot know whether a continuation
file happens to begin with a reconnect and resnapshot.

## SHA-256 must not block ingest

Production files exceed 1 GB, so hashing runs off the ingest path. At a boundary the writer
closes the old container, opens the new one and returns; a background finalizer then hashes the
closed file and publishes its manifest. The finalizer only ever reads a fully closed filename
and never modifies the raw file.

If finalization fails, the raw file is preserved untouched, `manifest_fail` is logged, and the
collector exits non-zero at shutdown. Nothing is ever deleted to recover.

Clean shutdown (SIGINT/SIGTERM) stops ingest, flushes queued complete records, closes the
current container, finalizes its manifest, waits for every outstanding finalizer, and exits 0.

## Testing with short rotation intervals

`--rotate-every-sec N` is a **test-only** interval. It is rejected together with
`--rotate-utc-daily`, and it must not be used in production.

```bash
./build/cpp/crypto_capture \
  --symbol BTCUSDT \
  --output-dir /tmp/rotation-smoke \
  --output-prefix BTCUSDT-TEST \
  --rotate-every-sec 3 \
  --duration-sec 20
```

The unit suite covers rotation without touching the network: `crypto_l2_tests` builds a
deterministic synthetic capture stream, writes it once unrotated and once through a two second
rotation interval, and asserts the replayed research exports are byte identical. It also covers
boundaries between two depth records, between a depth and a trade record, inside a burst, across
a reconnect, shutdown immediately before and after a boundary, filename collisions, and broken
chain rejection.

Note that a `.chft.zst` file is a versioned block container whose blocks are zstd frames, not a
bare zstd stream — `zstd -t` does not apply. Integrity is verified by reading the file with
`RawEventReader` (or `crypto_replay`), which fails loudly on any block that does not decompress.

## Production

```
--output FILE remains supported and unchanged.
```

Rotating ExecStart:

```
ExecStart=/home/ubuntu/crypto_hft_like_bot/build/cpp/crypto_capture \
  --symbol BTCUSDT \
  --output-dir /home/ubuntu/crypto_hft_like_bot/data/raw \
  --output-prefix BTCUSDT-LONDON \
  --rotate-utc-daily
```

Expected unit configuration:

```ini
[Unit]
Description=Binance USD-M BTCUSDT raw capture
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/crypto_hft_like_bot
ExecStart=/home/ubuntu/crypto_hft_like_bot/build/cpp/crypto_capture \
  --symbol BTCUSDT \
  --output-dir /home/ubuntu/crypto_hft_like_bot/data/raw \
  --output-prefix BTCUSDT-LONDON \
  --rotate-utc-daily
Restart=always
RestartSec=5
KillSignal=SIGTERM
# Must outlast the final SHA-256 of a multi-gigabyte file.
TimeoutStopSec=300

[Install]
WantedBy=multi-user.target
```

`TimeoutStopSec` matters: shutdown waits for outstanding finalization, and hashing a 1 GB+ file
takes seconds. A stop timeout shorter than that turns a clean shutdown into a SIGKILL and loses
the final manifest (though never the raw file).

Disk management is deliberately out of scope here. The collector never deletes a raw file to make
room; it fails loudly if the next file cannot be opened. A disk watchdog and remote archival
policy are handled separately.
