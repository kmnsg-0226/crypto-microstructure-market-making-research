# Live capture and replay validation

Validation date: 2026-08-15/16 (Europe/Gibraltar), using Binance BTCUSDT USD-M production public market-data endpoints.

## 30-minute acceptance capture

Command:

```bash
./build/cpp/crypto_capture \
  --output data/raw/BTCUSDT-2026-08-15-30m.chft.zst \
  --duration-sec 1800
```

Observed result:

```text
elapsed_sec=1804
depth_events=17636
trade_events=3298
reconnects=0
sequence_gaps=0
parse_failures=0
invalid_records=0
snapshots=1
snapshot_failures=0
raw_records=20940
output_bytes=4382601
```

The book remained `LIVE` throughout collection and became `DISCONNECTED` only during the requested graceful shutdown. The raw record accounting is exact:

```text
17,636 depth + 3,298 trades + 1 instrument metadata
+ 1 REST snapshot + 2 connection opens + 2 connection closes = 20,940
```

The recorded local receive interval was `2026-08-15T21:46:59Z` through `2026-08-15T22:17:04Z`. The non-zero exchange timestamp range was `2026-08-15T21:47:00Z` through `2026-08-15T22:17:03Z`.

The final binary was also run for a separate 10-second smoke test after all counter changes. It reported 88 depth events, 10 aggregate trades, one snapshot, and zero gaps, reconnects, parse failures, invalid records, dropped records, snapshot failures, or fatal errors.

## Deterministic replay

The acceptance raw file was reopened and replayed twice, each time producing a 100 ms research export. Both runs reported:

```text
raw_records=20940
depth_events=17636
applied_depth_events=17634
stale_depth_events=2
trade_events=3298
sequence_gaps=0
parse_failures=0
final_update_id=11295239938123
best_bid_ticks=631117
best_ask_ticks=631118
final_checksum=0x628e0abd3b64458d
research_rows=17992
```

The two stale events were obsolete bootstrap-buffer events discarded after alignment to the REST snapshot; they were not sequence gaps.

Final top ten levels in `(price_ticks, quantity_lots)` form:

```text
bids: (631117,3050) (631116,223) (631115,7) (631114,2) (631112,71)
      (631111,1) (631110,1) (631109,3) (631108,55) (631107,16)
asks: (631118,13679) (631119,184) (631120,148) (631121,2) (631122,540)
      (631123,4) (631125,2) (631126,118) (631127,4) (631128,5)
```

With tick size `0.10`, the final best bid/ask were `63111.70 / 63111.80`.

## File integrity and rate

```text
raw SHA-256:      56aed7a26e2c7acb60917dbc31e1d1a32fae574b98cf4a5f12298008757d99bf
research SHA-256: 6b35587b090c103dbb6c34ada81c8676cd836297305f12b6b8e0bf5f7ad6dfca
raw compressed:   4,382,601 bytes
research zstd:      444,421 bytes
research decoded: 7,243,431 bytes
```

Both independent research exports had the same SHA-256. The decoded export had 17,992 rows, 50 columns, and zero intervals different from 100,000,000 ns while the book was valid.

At this observed activity level, the raw capture averaged 2,429.38 bytes/second, approximately 8.34 MiB/hour or 0.195 GiB/day. This is an observation from one 30-minute window, not a storage guarantee; volatility and message rate will change it.
