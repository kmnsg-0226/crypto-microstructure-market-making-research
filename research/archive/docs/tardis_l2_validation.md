# Tardis BTCUSDT first-day L2 validation

## Result

The free Binance Futures BTCUSDT `incremental_book_L2` files for the first day of each
month from `2026-01-01` through `2026-08-01` all reconstruct successfully with the existing
C++ `OrderBook` price-level semantics. Each file was replayed twice from gzip input without
materializing the CSV on disk. Both the final full-book checksum and a rolling input/BBO
replay checksum matched on every repeat.

This is a normalized-data reconstruction result, not an independent Binance sequence audit.
The CSV schema omits `U`, `u`, and `pu`, and it omits disconnect markers. Tardis states that
it validates Binance Futures `pu/u` during collection and reconnects on a missed message, but
that provider-side check cannot be reproduced from this CSV alone. Raw native `depth` and
`depthSnapshot` replay is required when exchange sequence IDs are mandatory.

## Inputs and aggregate result

- Exchange/symbol: `binance-futures` / `BTCUSDT`
- Dates: each first day from `2026-01-01` through `2026-08-01`
- Files: 8
- Compressed bytes: 5,423,612,587
- Price-level rows: 995,650,378
- Complete `local_timestamp` message states: 17,920,643
- Snapshot rows: 177,716
- Snapshot groups: 52 (8 day-start snapshots and 44 reconnect snapshots)
- Delta messages: 17,920,591
- Valid complete-message states: 17,920,643
- Invalid complete-message states: 0
- Parse failures: 0
- Exchange timestamp regressions: 0
- Local timestamp regressions: 0
- Crossed states after complete messages: 0
- Empty bid states after complete messages: 0
- Empty ask states after complete messages: 0
- Files with deterministic two-pass replay: 8/8
- Files ending with a valid book: 8/8

The 44 replacement snapshots show that the captured connections were not uninterrupted.
Because normalized CSV excludes disconnect events, the validator conservatively treats the
time from the last message before each replacement snapshot through that snapshot as
unavailable. Those intervals total 161.033831 seconds over 691,189.371679 seconds of observed
file span, for conservative time availability of 99.9767019231%. This is distinct from the
100% valid rate among complete message states actually present in the CSV.

The largest local receive-time gap is 60.397988 seconds, ending at
`2026-08-01 18:32:10.225407 UTC`. That gap terminates in a valid replacement snapshot, so the
book is reset rather than continued across unknown data.

## Monthly results

| Date | Compressed bytes | Level rows | Message states | Snapshot groups | Max local gap | Conservative reconnect gap | Replay checksum |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026-01-01 | 347,513,061 | 62,609,291 | 1,642,732 | 1 | 0.718838 s | 0 s | `0x9a75e59656b36be6` |
| 2026-02-01 | 865,907,076 | 165,962,192 | 1,652,423 | 3 | 1.397203 s | 2.652906 s | `0xf7315d5407dc0e26` |
| 2026-03-01 | 737,199,360 | 139,795,935 | 1,661,195 | 1 | 1.904099 s | 0 s | `0xbdcd0f388cdb99db` |
| 2026-04-01 | 675,132,621 | 124,963,429 | 1,660,765 | 9 | 3.035586 s | 12.723703 s | `0xd97c09399882b25d` |
| 2026-05-01 | 557,562,555 | 101,067,739 | 1,661,069 | 8 | 12.270520 s | 22.289526 s | `0xf04d314af7bf8f1f` |
| 2026-06-01 | 893,502,369 | 161,057,093 | 3,217,287 | 14 | 5.850044 s | 24.275172 s | `0xd15600c559c6bab2` |
| 2026-07-01 | 923,475,379 | 166,849,391 | 3,207,962 | 9 | 11.376046 s | 27.843735 s | `0xfca4bc1c13075374` |
| 2026-08-01 | 423,320,166 | 73,345,308 | 3,217,210 | 7 | 60.397988 s | 71.248789 s | `0xcdb0d5b4f97f7cd6` |

Every monthly row above has zero parse, ordering, crossed, and empty-side failures.

First and last BBOs:

| Date | First BBO | Last BBO |
|---|---|---|
| 2026-01-01 | `87608.20 x 3.937 / 87608.30 x 5.826` | `88799.90 x 5.208 / 88800.00 x 7.838` |
| 2026-02-01 | `78706.70 x 2.030 / 78706.80 x 1.623` | `76931.50 x 5.058 / 76931.60 x 0.737` |
| 2026-03-01 | `66955.00 x 12.740 / 66955.10 x 0.211` | `65749.90 x 7.563 / 65750.00 x 1.590` |
| 2026-04-01 | `68235.30 x 2.908 / 68235.40 x 6.377` | `68086.40 x 1.802 / 68086.50 x 5.957` |
| 2026-05-01 | `76291.70 x 3.929 / 76291.80 x 19.731` | `78191.90 x 15.835 / 78192.00 x 2.082` |
| 2026-06-01 | `73657.40 x 15.548 / 73657.50 x 0.165` | `71391.50 x 20.793 / 71391.60 x 0.537` |
| 2026-07-01 | `58608.10 x 5.969 / 58608.20 x 2.113` | `59999.50 x 5.837 / 59999.60 x 4.954` |
| 2026-08-01 | `62862.20 x 13.958 / 62862.30 x 1.776` | `62792.20 x 4.913 / 62792.30 x 8.375` |

## File integrity

All eight downloads pass gzip integrity checks. SHA-256 values:

| Date | SHA-256 |
|---|---|
| 2026-01-01 | `0488a2204c9070b1e6a8769af48d54fb36e6a5658613267e2615cd3228002ded` |
| 2026-02-01 | `a1e9fc0fcc20d309d171ed1b6367ebe17948c84dd025a07a5d13c80f0b023cc4` |
| 2026-03-01 | `a5468fb97f161b05a89f8dcc39d8c88a58fb6dc60caeb69aa783facff66c27e1` |
| 2026-04-01 | `d1d08211ebcc8b576c4b9d50158ff39971f66dd463cffe1929dacd3d17223cfd` |
| 2026-05-01 | `284b95a8d84d1fdda10f73d80ba8cfb5f1f2ee60db9bd00937f3701e5948faf4` |
| 2026-06-01 | `581361873d3a692362257217e27961332ee25786dca27f280048be2ed150837d` |
| 2026-07-01 | `b2e8bbed3db89695f055dc3010a0fff074732d82ae18117a1602b5593c90d1f1` |
| 2026-08-01 | `bc7b4e6206bdbd893da75d035f63128b518ed34f3dd6490da71f96c72fe2a4cc` |

The aggregate deterministic manifest is
`data/historical/tardis/reports/2026-first-days/manifest.json` with SHA-256
`5893274ec07dde310d41d48b3b1e66bd01af4ab7507dc02aa11d409583a97a03`.

## Reconstruction rules

The implementation follows the normalized CSV rules documented by Tardis:

1. Preserve CSV row order.
2. Group level modifications by `local_timestamp`; inspect the book only after the complete
   message has been applied.
3. Skip non-snapshot rows before the first snapshot.
4. On a `false -> true` snapshot transition, discard the old book.
5. Apply repeated prices in row order; `amount=0` removes a level.
6. Apply every complete snapshot or delta message through the existing integer-tick C++
   `OrderBook`.
7. If a completed message crosses or empties the book, invalidate it and do not apply more
   deltas until a later snapshot.

Tardis format and reconstruction references:

- <https://docs.tardis.dev/downloadable-csv-files>
- <https://docs.tardis.dev/faq/data>
- <https://docs.tardis.dev/historical-data-details/binance-futures>

No trades, signals, backtest, or trading-performance calculations were added or run.
