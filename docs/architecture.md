# Architecture

```text
Binance snapshot + diff-depth + aggTrade
             │
 C++ parser / connection state / DepthSynchronizer
             │
 integer-tick/lot OrderBook ── gap invalidation
             │
 compressed native event storage
             │
 deterministic EventReplayer
             │
 100 ms causal features and research observers
             │
 blocked OOF research and executable-PnL falsification
```

- `cpp/exchange/binance/` parses metadata, snapshots, updates, and aggressive trades.
- `cpp/book/` is the shared integer-tick/lot local book.
- `cpp/storage/` persists compressed raw events with receive/exchange timestamps and stream state.
- `cpp/replay/` provides deterministic replay; rotated continuations may preserve stream state,
  while independent collectors replay separately.
- `cpp/research/` exports causal features and implements timing, level lifecycle, queue, and
  passive-fill observers.
- Python native packages reduce exports into frames and reviewed artifacts under `research/`.

Invariants: synchronization is required before research output; gaps, disconnects, invalid books,
and replacement snapshots terminate segments; no feature, target, or passive simulation crosses a
segment boundary; raw data is immutable and manifests define inputs.
