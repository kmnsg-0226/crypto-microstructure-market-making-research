# Kraken Futures PI_XBTUSD passive-queue follow-up

This follow-up used 30 actual `/executions` records and one `/orders` query covering ±5 seconds around each execution. It saved only aggregates and compact per-execution reconciliation records.

## Execution reconciliation

- Executions selected: 30/30 successfully returned.
- Maker `order.uid` had a prior `OrderPlaced` in the local window: 30/30, reconciliation rate 100%.
- Execution price matched the maker order price: 30/30.
- Full single execution: 27.
- Partial fill followed by cancellation: 2. Kraken’s cancelled order object exposed the remaining quantity; examples were 184 filled + 13,378 remaining from 13,562 and 1 filled + 8 remaining from 9.
- One execution was `18` against an original `36`, followed by cancellation with `9` remaining. The local window cannot determine whether the missing `9` was a prior fill or another state transition.

The public execution fields and order events therefore support useful fill reconciliation, including observed partial-fill/cancel behavior, but not always a complete cumulative fill ledger from a small local window.

## FIFO ambiguity at executions

Across the fetched ±5-second windows:

- Same-millisecond, same-side, same-price cohorts: 1,523, containing 9,581 orders.
- Sampled executions whose maker belonged to an ambiguous cohort: 30/30.
- Ambiguous makers at the executed price: 30/30. No historical book snapshot is needed for this equality; the maker’s order price equals the execution price.
- Maker cohort size: median 7 orders, p95 18, maximum 18.
- Cohort quantity: median 63 contracts, p95 324, maximum 27,124.

For a timestamp-only reconstruction, placing the maker first versus last in its cohort changes inferred queue-ahead by the other cohort orders. Because every sampled maker was in such a cohort, this can change hypothetical fill eligibility and fill timing—not merely an unimportant tie-break. The observed execution tells us that this specific maker filled, but it does not reveal an exact FIFO ordering among same-millisecond peers.

A workable conservative convention is to model each same-millisecond, same-side, same-price group as one unordered cohort and report queue-ahead as an interval, using the worst-case cohort position for adverse assumptions. This supports robust cohort research, but it does not make the underlying FIFO exact.

## Lifetimes and missing-snapshot warm-up

From a separate bounded ten-page order sample, 4,965 placed orders had a matched terminal event in the sample. Observed lifetimes were:

- Median: 1.001 seconds
- p95: 3.192 seconds
- p99: 12.716 seconds
- Maximum: 88.337 seconds

The empirical steady-state proxy for orders older than the specified warm-up was zero after every tested period:

| Warm-up | Unknown pre-window inventory proxy |
|---|---:|
| 1 hour | 0.0 orders |
| 6 hours | 0.0 orders |
| 24 hours | 0.0 orders |
| 7 days | 0.0 orders |

This proxy is based only on fully observed lifetimes and excludes right-censored/unmatched orders. It is not proof that pre-window inventory is zero and does not make replay exact. It does show that, in this sample, ordinary resting orders are much shorter-lived than the proposed warm-ups; the missing snapshot is unlikely to dominate after warm-up for this order-flow regime, subject to regime changes and rare long-lived orders.

## Final verdict: B2

The data is useful for passive queue research and supports a conservative unordered-cohort model. However, same-millisecond peers occur at every sampled executed maker, with cohort sizes large enough to alter inferred queue-ahead and fill outcomes. Exact historical FIFO is not observable, so the ambiguity is materially relevant rather than negligible.
