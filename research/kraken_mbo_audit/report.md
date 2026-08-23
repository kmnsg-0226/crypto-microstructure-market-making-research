# Kraken Futures PI_XBTUSD MBO feasibility audit

Audit run: 2026-08-22/23 UTC. Scope was ten consecutive `/orders` pages, 1,000 events per page; no Binance files were read or changed.

## 1. Order-event sample

- Total events: 10,000
- `OrderPlaced`: 4,998
- `OrderUpdated`: 0
- `OrderCancelled`: 5,000
- Other: 2 (`OrderRejected`)
- Unique `order.uid`: 5,043
- Duplicate placements: 0
- Placement-before-update/cancel rate: 99.18% (4,957 of 4,998 placed orders had a later update/cancel in the sample)
- Incomplete lifecycles: 41 placed orders without an explicit cancel/fill/closed/done event inside the ten-page sample. This is sample-boundary incomplete, not proof that the orders remained live.

The sampled order-event window was `2026-08-22T23:14:02.376Z` through `23:17:35.921Z`.

## 2. `OrderUpdated` and executions

No `OrderUpdated` event occurred in the bounded sample, so its payload cannot be characterized from this window. The public `/executions` route does exist and returns explicit `Execution` records containing:

- maker and taker `order.uid`
- execution quantity and price
- execution timestamp
- `limitFilled` and `takerReducedQuantity`

That makes full and partial executions distinguishable from an amendment/cancellation when the streams overlap. The execution stream returned no events in the exact order-event window: its latest returned event was `2026-08-22T22:52:54.713Z`, before the order window began. Therefore this run produced no same-window UID reconciliation, rather than a demonstrated mismatch.

Guessed `/trades`, `/fills`, and `/publictrades` routes returned 404. `/executions` is the usable public execution-history route.

## 3. Same-price/timestamp FIFO

The sample contained 190 groups (441 orders) sharing timestamp, side, and price. No authoritative intra-timestamp sequence field was present. JSON array order is not treated as an exchange FIFO key. Exact FIFO is therefore ambiguous.

## 4. Retention probes

Each `before` probe returned 1,000 events. The actual oldest timestamp in each returned page was:

| Approximate probe | Oldest returned |
|---|---|
| 7 days | 2026-08-15 23:16:25.948Z |
| 30 days | 2026-07-23 23:16:26.444Z |
| 90 days | 2026-05-24 23:16:49.809Z |
| 1 year | 2025-08-22 23:16:44.444Z |
| 2 years | 2024-08-22 23:16:23.125Z |

These are page-local oldest values, not proven retention floor timestamps.

## 5. Snapshots and bootstrap

`/orderbook`, `/book`, and `/snapshot` under the historical market route all returned 404. No historical L3 snapshot was found.

Order-event replay can bootstrap an exact state only if replay starts before the target state and the retained stream contains every relevant mutation. The retention probes show events available at least at the tested two-year point, but without a snapshot or a verified pre-history floor, exact bootstrap for an arbitrary historical start cannot be guaranteed.

## Verdict: B

Useful order-level data with explicit executions, but exact FIFO remains ambiguous and snapshot-based exact bootstrapping is unavailable.
