# Binance BTCUSDT USD-M L2 protocol assumptions

Verified: 2026-08-15 against current official Binance USD-M Futures documentation and the public `exchangeInfo` endpoint.

## Instrument

The live `GET https://fapi.binance.com/fapi/v1/exchangeInfo` response reported:

```text
symbol: BTCUSDT
pair: BTCUSDT
contractType: PERPETUAL
status: TRADING
baseAsset: BTC
quoteAsset: USDT
PRICE_FILTER.tickSize: 0.10
PRICE_FILTER.minPrice: 556.80
PRICE_FILTER.maxPrice: 4529764
LOT_SIZE.stepSize: 0.001
LOT_SIZE.minQty: 0.001
LOT_SIZE.maxQty: 1000
```

The implementation reads these filters at runtime. It does not use `pricePrecision` as tick size or `quantityPrecision` as step size because Binance explicitly says not to do so.

Internally, price is an `int64_t` number of `PRICE_FILTER.tickSize` units and quantity is an `int64_t` number of `LOT_SIZE.stepSize` units. Decimal strings are converted exactly; values off the exchange increment are rejected. Explicit order-style `qty_to_lots` conversion enforces `LOT_SIZE` min/max bounds. Feed quantities enforce non-negativity and step alignment but may exceed the maximum for one submitted order because a book level or aggregate trade can combine multiple orders.

Official REST references:

- Exchange information: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data#exchange-information
- Order-book snapshot: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data#order-book

## REST snapshot

The snapshot request is:

```text
GET https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000
```

The maximum documented limit is 1000 levels per side. The response fields relied upon are `lastUpdateId`, `E`, `T`, `bids`, and `asks`. RPI orders are documented as excluded from this endpoint.

## WebSocket routing and streams

Binance's current routed endpoints are:

```text
depth:    wss://fstream.binance.com/public/ws/btcusdt@depth@100ms
aggTrade: wss://fstream.binance.com/market/ws/btcusdt@aggTrade
```

Depth is classified as high-frequency public data and aggregate trades as regular market data. They are intentionally captured on separate connections and carry separate session IDs.

This differs from the repository's older Python URL, which used the legacy unrouted combined `/stream` endpoint. Binance documents that legacy unrouted connections only continue to receive Public streams after the 2026 migration; a Market stream such as `aggTrade` must use `/market`.

Official references:

- Connection/routing: https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect
- Routed endpoint migration: https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice
- Local-book synchronization: https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly

## Diff-depth fields

The normalized depth parser relies on:

```text
e   event type (`depthUpdate`)
E   event/output time, milliseconds
T   transaction time, milliseconds
s   symbol
U   first update ID in the event
u   final update ID in the event
pu  previous event final update ID
b   absolute bid quantities by price
a   absolute ask quantities by price
```

Quantities are absolute replacements, not deltas. Zero removes a level. Removing an unknown level is normal.

## Snapshot-to-stream synchronization

The implementation uses an explicit state machine and the current documented USD-M rules:

1. Connect to diff depth and begin buffering events.
2. Request the REST depth snapshot.
3. Drop buffered events whose `u < snapshot.lastUpdateId`.
4. The first applied event must satisfy `U <= snapshot.lastUpdateId <= u`.
5. Apply the event's absolute quantities and set the local update ID to its `u`.
6. In LIVE state, every next event must satisfy `event.pu == previous_event.u`.
7. Stale/duplicate events with `u <= local_update_id` are ignored.
8. Any continuity failure immediately invalidates the book. No research snapshot may be emitted until a fresh snapshot and buffered stream are aligned again.

The current Binance page uses `u < lastUpdateId` for the discard rule and inclusive alignment around `lastUpdateId`; the code follows that wording rather than silently substituting Spot synchronization rules.

## Connection lifecycle

Binance currently documents:

- one WebSocket connection is valid for 24 hours;
- the server sends a ping every 3 minutes;
- failure to return pong within 10 minutes causes disconnect;
- unsolicited pong is allowed;
- maximum 10 incoming control messages per second per connection;
- maximum 1024 streams per connection.

The Boost.Beast/OpenSSL WebSocket transport uses standards-compliant ping/pong handling, proactively reconnects before 24 hours, reconnects after close/error, assigns a new session ID, invalidates depth state on disconnect, and requests a fresh snapshot after depth reconnection.

## Aggregate trades

The parser relies on `e`, `E`, `s`, `a`, `p`, `q`, `f`, `l`, `T`, and `m`. For signed volume, `m=true` means the buyer was maker, therefore the aggressive/taker side was SELL; `m=false` means aggressive BUY.
