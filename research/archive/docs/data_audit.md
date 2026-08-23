# Data audit

Audit date: 2026-08-15

## Repository and reusable components

At the time of this audit, the repository was an existing Python paper-trading and backtesting
prototype. That legacy public bot architecture was subsequently retired; the historical detail
below is retained as provenance and is not part of the final native-L2 project.

- `clients/binance_ws.py`: asynchronous Binance USD-M WebSocket client for `aggTrade`, `bookTicker`, and an optional depth callback. At audit time its URL construction predated Binance's 2026 routed WebSocket split. It is now compatibility-updated to use separate `/market` and `/public` connections; it is not used for the new raw C++ capture path.
- `clients/binance_rest.py`: public REST connectivity and exchange-information calls.
- `market/market_state.py`: best bid/ask, last trade, spread, and mid state.
- `market/trade_buffer.py`: event-time-capable rolling aggregate-trade buffer.
- `strategy/trade_flow_momentum.py`: the baseline signed trade-flow imbalance feature.
- `execution`, `portfolio`, and `risk`: paper fill, position, PnL, cooldown, loss-limit, and kill-switch scaffolding. These are not part of the new raw L2 capture path.
- `backtest/load_aggtrades.py` and the replay/backtest scripts: reusable aggregate-trade parsing and baseline research comparisons.
- `storage`: SQLite metadata tables. SQLite remains suitable for experiment metadata and summaries, but not row-by-row raw depth traffic.

The Python dependencies are `aiohttp`, `aiosqlite`, and `python-dotenv`. Existing tests contain only `tests/__init__.py`; there was no substantive automated coverage before the C++ market-data layer.

## Historical files actually present

The repository contains five BTCUSDT USD-M aggregate-trade days, 2026-03-23 through 2026-03-27, both at the repository root and duplicated under `backtest/data/`.

CSV schema:

```text
agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker
```

The five root CSVs contain exactly 10,515,777 aggregate-trade rows (666 MiB allocated size in total). `transact_time` is Unix epoch milliseconds. The valid ZIP archives each contain the matching CSV.

| UTC day | Rows | First `transact_time` (ms) | Last `transact_time` (ms) |
|---|---:|---:|---:|
| 2026-03-23 | 3,163,268 | 1774224000073 | 1774310399987 |
| 2026-03-24 | 2,055,331 | 1774310400052 | 1774396799627 |
| 2026-03-25 | 1,695,658 | 1774396800021 | 1774483199948 |
| 2026-03-26 | 1,728,681 | 1774483200032 | 1774569599851 |
| 2026-03-27 | 1,872,839 | 1774569600052 | 1774655999166 |

Files named `BTCUSDT-aggTrades-2026-03-23.csv.zip` through `...-03-26.csv.zip` are not ZIP archives. File-signature inspection identifies them as short XML error responses. Filenames alone are therefore not treated as proof of data type or validity.

`bot.db` is SQLite. Its application tables and columns are:

| Table | Columns excluding integer primary key | Rows at audit |
|---|---|---:|
| `signals` | `ts, symbol, signal, score, meta` | 1,364 |
| `orders` | `ts, symbol, side, price, qty, status, meta` | 0 |
| `fills` | `ts, symbol, side, price, qty, fee, order_id, meta` | 0 |
| `positions` | `ts, symbol, qty, avg_price, unrealized, realized` | 0 |
| `pnl_snapshots` | `ts, symbol, pnl, unrealized, realized` | 1,364 |
| `errors` | `ts, component, message, meta` | 0 |

This schema confirms that SQLite contains strategy metadata/PnL, not persisted market-depth or top-of-book history.
The populated `ts` values range from `1774728997617` to `1774742687503` and are Unix epoch milliseconds; both populated tables cover the same interval.

## Aggressor-side convention

Binance's `is_buyer_maker`/`m` field is interpreted as follows:

- `false`: buyer is the taker, so the event is aggressive BUY volume.
- `true`: buyer is the maker, so the seller is the taker and the event is aggressive SELL volume.

The existing `TradeBuffer` implements this convention correctly.

## A. Data already available and reusable

The March aggregate-trade files support:

- signed traded volume and the existing trade-flow imbalance baseline;
- trade counts, trade intensity, notional, and short-horizon trade-price returns;
- aggregate-trade parser and event-time replay tests;
- reproducing the existing strategy baseline and fee sensitivity;
- testing joins between future L2-derived samples and trade-flow features, once overlapping L2 data is collected.

## Explicit inventory of historical order-book data

| Data type | Present? | Evidence |
|---|---:|---|
| Raw diff-depth updates (`U`, `u`, `pu`, `b`, `a`) | No | No matching files or schemas found |
| L2/L5/L10 snapshots | No | No price-level snapshot files found |
| Raw `bookTicker` | No | Live client supports it, but no persisted history exists |
| Historical best bid/ask | No | SQLite contains no best-bid/ask history and CSVs are aggregate trades only |
| Aggregate trades | Yes | Five validated CSV days plus valid ZIP copies |

## B. Research the current data cannot support

The current historical data cannot support any claim requiring contemporaneous book state:

- L1 order-book imbalance (OBI);
- L5/L10 OBI;
- order-flow imbalance (OFI) based on quote additions, cancellations, or size changes;
- multi-level OFI;
- imbalance-adjusted weighted mid;
- true historical spread;
- exact mid-price markouts;
- queue-position simulation;
- passive-fill or maker-fill modelling.

Trade prices cannot identify resting bid/ask levels, queue sizes, quote cancellations, hidden liquidity, or the path of the book. The fixed synthetic spread used by the old aggregate-trade backtests is only a legacy execution approximation. It must not be used for the new L2 research or presented as historical book data.

## Official historical-data note

The official Binance Data Vision archive for
[`BTCUSDT-bookDepth-2026-03-23.zip`](https://data.binance.vision/data/futures/um/daily/bookDepth/BTCUSDT/BTCUSDT-bookDepth-2026-03-23.zip)
was downloaded to a temporary path and inspected rather than inferred from its name. It is a valid ZIP containing one CSV with:

```text
timestamp,percentage,depth,notional
```

That daily file has 31,344 rows but only 2,612 distinct timestamps. Each timestamp has 12 aggregate percentage bands (`-5,-4,-3,-2,-1,-0.2,+0.2,+1,+2,+3,+4,+5`). Most consecutive samples are roughly 27–33 seconds apart, with some larger gaps. It contains no price levels, per-level quantities, `U/u/pu` sequence IDs, or 100 ms incremental events.

Therefore Data Vision `bookDepth` is coarse percentage-band liquidity data, not the real-time incremental diff-depth feed. It cannot reconstruct L2/L10 state, true spread, or OFI at the required granularity and is not recommended as a substitute. The project proceeds with live raw collection. Any separately licensed Binance tick-by-tick L2 product must have its schema, sequence semantics, gap guarantees, and sampling frequency verified before use.
