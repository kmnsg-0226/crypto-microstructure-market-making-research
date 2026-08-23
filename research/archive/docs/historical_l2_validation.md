# CryptoHFTData historical L2 Stage A/B validation

## Decision

Historical BTCUSDT L2 reconstruction for `2026-03-23 00:00–02:00 UTC` is **blocked**.
No snapshot row exists in the start hour or any of the preceding 24 UTC hours. The
pipeline therefore did not fabricate an initial book, did not invoke real-data C++ replay,
and did not generate a 100 ms research export.

## Data acquired

- Exchange/data type: `binance_futures` / `BTCUSDT_orderbook`
- Exact hourly range cached: `2026-03-22 00 UTC` through `2026-03-23 01 UTC`
- Objects: 26
- Compressed bytes: 451,531,368
- Decompressed Parquet bytes: 665,679,448
- Authentication: API key from `CRYPTOHFTDATA_API_KEY` exchanged for an in-memory JWT
- Exact paths, byte sizes, row counts, schema hashes, and object SHA-256 values:
  `data/historical/validation/stage_ab/hourly_manifest.json`

The key and JWT are absent from cache paths, manifests, reports, and logs. Downloads use
`.part` files, HTTP retry/resume, content-length checks, Parquet validation, and atomic rename.
Failed partial/decompression artifacts are retained for diagnosis.

## Grouping and ordering

A Parquet row is treated as one bid/ask price-level modification. Rows sharing the complete
message metadata are grouped into one exchange message before applying sequence rules.

Ordering is:

1. `received_time` nanoseconds ascending;
2. snapshot `last_update_id` or update `final_update_id` for equal receive times;
3. event type, `first_update_id`, `prev_final_update_id`, and source ordinal as deterministic
   tie-breakers.

The compressed per-message evidence file is
`data/historical/validation/stage_ab/sequence_audit.jsonl.zst`. Every record includes `U`,
`u`, `pu`, the full ordering key, source object, receive timestamp, and audit status.

## Target and boundary results

| Hour | Price-level rows | Unique update messages | Snapshots |
|---|---:|---:|---:|
| 2026-03-22 23 UTC | 5,623,557 | 69,068 | 0 |
| 2026-03-23 00 UTC | 7,762,175 | 68,838 | 0 |
| 2026-03-23 01 UTC | 6,797,671 | 69,088 | 0 |
| Total | 20,183,403 | 206,994 | 0 |

- Duplicate/stale messages: 0
- Contiguous updates after the initial delta-only message: 206,991
- `23 → 00` boundary: contiguous (`first pu = previous u = 10167385409181`)
- `00 → 01` boundary: contiguous (`first pu = previous u = 10167776184078`)
- Internal `pu != previous u` breaks: 2, both inside hour 00

Genuine missing intervals after receive-time/sequence ordering:

| Previous `u` | Next `pu` | Missing update-ID count | Receive-time gap |
|---:|---:|---:|---:|
| 10167386172319 | 10167387970647 | 1,798,328 | 8.773954057 s |
| 10167623073568 | 10167624113967 | 1,040,399 | 4.144138944 s |

## Reconstruction outcome

- Snapshot location/timestamp: not found within the bounded 24-hour lookback
- Snapshot `lastUpdateId`: not available
- Real historical C++ replay attempted: no
- Valid-book percentage for the requested two hours: 0%
- Invalid-book intervals: one blocked interval covering all 7,200 seconds
- Crossed-book states: not evaluable without initialization
- Empty bid/ask states: not evaluable without initialization
- Deterministic real-data checksum: not available
- First/last reconstructed BBO: not available
- 100 ms export rows: 0 (file intentionally not created)

The existing-format adapter is nevertheless covered by an offline synthetic snapshot test.
That test feeds the Python-produced `CHFTL2R1` file through the existing C++ parser,
synchronizer, `OrderBook`, and exporter twice, verifying identical final checksums and
byte-identical 100 ms outputs. This proves interface compatibility, not validity of the
blocked real historical interval.

## Commands

```bash
.venv/bin/dotenv run -- .venv/bin/python -m pyresearch.historical.cli stage-ab
.venv/bin/cmake --build build/cpp --parallel
.venv/bin/ctest --test-dir build/cpp --output-on-failure
.venv/bin/python -m unittest discover -s tests -v
```

The pre-existing live Binance collector was not modified, stopped, or restarted during this
historical validation.

## 2026-05-25 follow-up interval

The May follow-up changes the March conclusion but does not prove uninterrupted hourly
coverage. The `04 UTC` object contains 270 usable two-sided snapshots. The earliest usable
snapshot after the requested start is:

- receive/event time: `2026-05-25 04:08:00.150 UTC`
- `lastUpdateId`: `10624742132248`
- depth: 500 bids and 500 asks
- initial BBO: `76997.90 x 1.602 / 76998.00 x 6.909`
- first bridge: `U=10624742131856`, `u=10624742133268`,
  `pu=10624742131682`; valid because `U <= lastUpdateId <= u`

Four order-book objects were cached and inspected:

| Object hour | Price-level rows | Unique messages | Updates | Usable snapshots | Actual receive-time coverage |
|---|---:|---:|---:|---:|---|
| 03 UTC | 2,707,842 | 50,216 | 49,410 | 806 | 03:00:00.003305–03:59:59.987541 |
| 04 UTC | 1,088,645 | 27,158 | 26,888 | 270 | 04:00:00.015341–04:58:16.043243 |
| 05 UTC | 1,409,446 | 34,447 | 34,137 | 310 | 05:09:00.009000–05:54:18.122344 |
| 06 UTC | 2,931,945 | 58,131 | 57,529 | 602 | 06:02:00.124000–06:58:18.658271 |

Starting at the selected snapshot, the raw delta audit contains 116,340 updates: the initial
snapshot bridge, 116,068 `pu == previous u` continuations, 271 genuine `pu` breaks, and zero
duplicate/stale updates. Cached provider snapshots cover each broken delta segment, so the
historical adapter can terminate the old synchronized interval and feed a fresh snapshot plus
bridge through the existing C++ `DepthSynchronizer` and `OrderBook`. It never applies deltas
through a detected gap without a snapshot.

The two file-boundary gaps prevent the requested continuity proof:

| Boundary | Previous `u` | Next `pu` | Missing update IDs | Receive-time gap |
|---|---:|---:|---:|---:|
| 04→05 | 10624963352265 | 10625015989759 | 52,637,494 | 644.192152183 s |
| 05→06 | 10625213241060 | 10625244881530 | 31,640,470 | 462.396431805 s |

The C++ adapter replay applies all 116,340 emitted depth updates after snapshot gating with
zero parser failures, internal sequence gaps, crossed books, empty bid books, or empty ask
books. All valid samples satisfy `best_bid < best_ask`. Two replays produce a byte-identical
100 ms validation export and checksum `0xa9ddb64aa0c2c5a4`.

Across the exact `04:00–07:00 UTC` research grid:

- rows: 108,000 (100 ms)
- valid rows: 32,162 (29.7796296296%)
- invalid rows: 75,838
- invalid intervals: 271, totaling 7,583.8 seconds
- first valid BBO: `76997.90 x 1.602 / 76998.00 x 6.909`
- last valid BBO: `77364.40 x 3.004 / 77364.50 x 20.830`
- both exact 05:00 and 06:00 samples: invalid

The validation artifacts are under `data/historical/validation/2026-05-25T04/`. The manifest
contains exact byte counts and SHA-256 values for all four inputs. Matching trades were not
downloaded because uninterrupted L2 reconstruction across both boundaries was not proven.

This establishes that the March no-snapshot result is interval-specific: May has many valid
snapshots and reconstructable L2 segments. However, incomplete hourly coverage and recurrent
delta gaps are also present in May, so this provider sample cannot yet support a continuous
multi-hour research dataset. No backtest or trading-performance calculation was run.
