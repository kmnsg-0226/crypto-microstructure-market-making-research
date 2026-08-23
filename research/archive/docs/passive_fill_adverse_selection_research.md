# Passive fill and adverse-selection research

## Outcome

Under the frozen conservative L2 assumptions, the passive BBO experiment does **not** yet
justify implementing a full inventory-aware market maker. The engine found economically severe
selection: for the headline 1-second quote and 1-second post-fill horizon, mean maker markout was
negative in development, June validation, and the July/August historical holdout. The
predeclared frozen-alpha quote filter also made mean conditional markout worse on both sides in
all three splits. This is a negative research result, not a claim that maker trading is
universally impossible and not a portfolio profitability backtest.

The frozen alpha still has descriptive value: OBI strongly separates conditional fill quality.
But the empirical direction is opposite the preregistered safety hypothesis. Positive OBI was
associated with *worse* filled-bid markout and *less bad* filled-ask markout; negative OBI showed
the mirror image. That inversion survived June and the historical holdout. A reversed rule was
not tested after seeing these results.

## Audit trail and frozen method

- Maker spec: `research/specs/maker_research_spec_frozen.json`
- Maker spec SHA-256: `0cf5762dc08063529ab9bd0a1a23257d4fe6f32692bc02fae02b87b5d1d78101`
- Implementation commit: `5e7bc22f9390fb275fbd54b186d4c275db9661ff`
- Alpha spec SHA-256: `011ea143ddf646b4ba482b0030a6e86d416a7692e16349daa1017543ef0a6f84`
- Execution spec SHA-256: `de04949d11e8788c926992b7abc217b01997d9f5b534d8cfc792cc9da94533d2`
- L2/trade bundle SHA-256: `e8ae8653d67ee23ffd66eb9d717bb040f645f7657e176e515354d2b7affdfed6`
- Holdout opening audit SHA-256: `0088a55bc5ca0181a0408224e02583a62060f992c5445dac09d344136ceba46c`
- Maker source bundle SHA-256: `f7eacf3f280db53b6af5fed4739f6af4140f5a56f7dac19053805c135578d617`
- Passive engine binary SHA-256 at freeze: `c93fb9b2f46786370bfd9bd6b1960f9be31ef89cd5285c5ca8aa0ca73b01dee5`

The split was Jan-May development, June validation, and Jul/Aug historical holdout. Jul/Aug had
already been seen in earlier alpha/taker work, so it is only a holdout for this maker-fill
methodology. The methodology was frozen before June and before opening Jul/Aug maker outcomes.
The strongest genuine forward OOS remains the still-collecting native Binance sample.

Every 100 ms, the experiment independently placed a `0.005`
BTC buy at best bid and sell at best ask for 100, 500, 1,000, or 5,000 ms. Price was fixed and
never chased. The headline queue model joins behind all displayed BBO quantity; only opposite
aggressor trades at the exact quote consume that queue; cancellations never help; additions are
behind; and a strict trade-through fills the remainder. Aggregated L2 cannot establish exact
FIFO, so this is explicitly an approximation.

Events are ordered by Tardis `local_timestamp` and then preserved source order. A new probe is
placed after same-local market events; it expires before an exact-expiry event. A replacement
snapshot cancels a surviving order. Normalized CSV has no disconnect messages, so replacement
snapshots are the available reconnect proxy. Vendor local time is capture-order metadata, not
assumed real trading latency.

## Scale and data validity

Across eight dates there were `55,296,000` candidate orders,
`9,113,535` full fills, and `8,261` partial fills.
There were `10,352` invalid-at-placement candidates,
`1,155` snapshot invalidations, and
`823` day-boundary invalidations. Gap/snapshot survival was
never fabricated.

## Fill probability by side and lifetime

| split | side | life ms | full fills | partial | full fill % |
|---|---|---|---|---|---|
| Jan-May development | ask | 100 | 156,073 | 562 | 3.61% |
| Jan-May development | ask | 500 | 473,807 | 981 | 10.97% |
| Jan-May development | ask | 1000 | 744,735 | 1,076 | 17.24% |
| Jan-May development | ask | 5000 | 1,748,827 | 785 | 40.49% |
| Jan-May development | bid | 100 | 152,591 | 563 | 3.53% |
| Jan-May development | bid | 500 | 459,307 | 1,002 | 10.63% |
| Jan-May development | bid | 1000 | 722,765 | 1,083 | 16.73% |
| Jan-May development | bid | 5000 | 1,715,939 | 721 | 39.73% |
| June validation | ask | 100 | 23,545 | 48 | 2.73% |
| June validation | ask | 500 | 79,290 | 88 | 9.18% |
| June validation | ask | 1000 | 132,328 | 104 | 15.32% |
| June validation | ask | 5000 | 351,001 | 107 | 40.64% |
| June validation | bid | 100 | 23,329 | 42 | 2.70% |
| June validation | bid | 500 | 79,238 | 67 | 9.17% |
| June validation | bid | 1000 | 132,209 | 84 | 15.30% |
| June validation | bid | 5000 | 350,839 | 118 | 40.62% |
| Jul/Aug historical holdout | ask | 100 | 38,403 | 60 | 2.22% |
| Jul/Aug historical holdout | ask | 500 | 126,473 | 101 | 7.32% |
| Jul/Aug historical holdout | ask | 1000 | 204,975 | 98 | 11.87% |
| Jul/Aug historical holdout | ask | 5000 | 522,635 | 133 | 30.26% |
| Jul/Aug historical holdout | bid | 100 | 38,160 | 69 | 2.21% |
| Jul/Aug historical holdout | bid | 500 | 124,790 | 114 | 7.23% |
| Jul/Aug historical holdout | bid | 1000 | 202,300 | 148 | 11.71% |
| Jul/Aug historical holdout | bid | 5000 | 509,976 | 107 | 29.53% |

Fill probability rises sharply with lifetime and is highly day-dependent. For the headline
1-second quotes it was about 16.7-17.2% by side in development, 15.3% in June, and 11.7-11.9%
in the historical holdout. This is a probe fill rate, not continuous quoting capacity.

## Headline adverse selection

Maker markout is measured directly from fill price, so it already includes passive price
advantage. The decomposition is `fill-price advantage + post-fill mid move`; no extra
half-spread is added.

| split | labeled fills | maker mean ticks | post-fill move ticks | fill-price advantage ticks | P(markout < 0) | HAC SE ticks | gross bps |
|---|---|---|---|---|---|---|---|
| Jan-May development | 1,469,440 | -64.01 | -37.42 | -26.59 | 88.47% | 0.23 | -0.878 |
| June validation | 264,560 | -61.64 | -29.87 | -31.77 | 90.93% | 0.44 | -0.854 |
| Jul/Aug historical holdout | 407,359 | -51.79 | -23.72 | -28.07 | 89.32% | 0.31 | -0.867 |

The 1-second markout distribution is adverse, not merely noisy: negative-markout probability is
88.5% in development, 90.9% in June, and 89.3% in holdout. HAC/Newey-West standard errors use
10 sequential filled-observation lags for the 1-second lifetime/horizon and reset at day
boundaries. Day-level tables are also retained because 100 ms probes overlap heavily.

## Quote-lifetime and markout-horizon sensitivity

Each cell is the combined bid/ask mean maker markout in ticks. Rows change fixed quote lifetime;
columns change the post-fill evaluation horizon.

| split | quote life ms | 100ms markout | 500ms markout | 1s markout | 5s markout |
|---|---|---|---|---|---|
| Jan-May development | 100 | -49.39 | -61.95 | -68.19 | -80.45 |
| Jan-May development | 500 | -46.95 | -59.01 | -65.04 | -77.33 |
| Jan-May development | 1000 | -46.41 | -58.16 | -64.01 | -76.44 |
| Jan-May development | 5000 | -45.61 | -56.86 | -62.48 | -75.77 |
| Jul/Aug historical holdout | 100 | -42.47 | -50.09 | -53.87 | -57.67 |
| Jul/Aug historical holdout | 500 | -41.29 | -48.78 | -52.18 | -57.23 |
| Jul/Aug historical holdout | 1000 | -41.09 | -48.45 | -51.79 | -57.32 |
| Jul/Aug historical holdout | 5000 | -40.08 | -47.15 | -50.30 | -56.64 |
| June validation | 100 | -49.58 | -59.31 | -65.06 | -77.63 |
| June validation | 500 | -47.96 | -57.32 | -62.66 | -75.38 |
| June validation | 1000 | -47.36 | -56.44 | -61.64 | -74.51 |
| June validation | 5000 | -46.51 | -55.03 | -59.89 | -73.44 |

Longer horizons generally make adverse selection worse. Quote lifetime materially changes who
gets filled, but it does not restore a positive mean in any split/horizon cell.

## OBI-conditioned fill quality

The following uses frozen development OBI-L5 deciles, grouped as negative (1-3) and positive
(8-10), with 1-second quote life and 1-second markout.

| split | side | OBI regime | fills | mean ticks | P(negative) |
|---|---|---|---|---|---|
| Jan-May development | ask | negative | 87,936 | -79.00 | 86.13% |
| Jan-May development | ask | positive | 447,666 | -56.49 | 87.95% |
| Jan-May development | bid | negative | 432,723 | -58.66 | 88.58% |
| Jan-May development | bid | positive | 87,124 | -83.07 | 87.52% |
| June validation | ask | negative | 10,976 | -73.78 | 89.24% |
| June validation | ask | positive | 82,012 | -56.05 | 89.78% |
| June validation | bid | negative | 78,221 | -58.39 | 91.50% |
| June validation | bid | positive | 12,720 | -69.24 | 89.36% |
| Jul/Aug historical holdout | ask | negative | 23,340 | -64.72 | 87.96% |
| Jul/Aug historical holdout | ask | positive | 108,273 | -47.65 | 90.01% |
| Jul/Aug historical holdout | bid | negative | 110,099 | -48.20 | 89.65% |
| Jul/Aug historical holdout | bid | positive | 21,979 | -57.79 | 85.86% |

The preregistered hypothesis was not supported. In development, positive-minus-negative OBI
changed filled-bid markout by -24.40 ticks; negative-minus-positive changed filled-ask markout by
-22.51 ticks. The same signs remained in June (-10.85/-17.74) and holdout (-9.59/-17.07).
This is evidence that OBI identifies selection regimes, but not evidence for the originally
proposed quote-side rule.

## L1 versus L5 versus L10

| split | signal | bid pos-neg ticks | ask neg-pos ticks |
|---|---|---|---|
| Jan-May development | obi_l1 | -25.41 | -23.31 |
| Jan-May development | obi_l5 | -24.40 | -22.51 |
| Jan-May development | obi_l10 | -23.64 | -21.84 |
| June validation | obi_l1 | -12.40 | -19.10 |
| June validation | obi_l5 | -10.85 | -17.74 |
| June validation | obi_l10 | -10.41 | -17.44 |
| Jul/Aug historical holdout | obi_l1 | -11.52 | -17.43 |
| Jul/Aug historical holdout | obi_l5 | -9.59 | -17.07 |
| Jul/Aug historical holdout | obi_l10 | -9.31 | -15.60 |

L1, L5, and L10 tell nearly the same descriptive story; depth choice changes the regime
contrasts by only a few ticks and never changes their signs. This experiment provides no
material evidence that L5/L10 is required over L1 for this maker-selection diagnostic.

## Frozen-alpha quote filter

The filter thresholds were fixed from development prediction values alone: bid predictions had
to be at least `-13.1378` ticks and ask predictions at most
`13.0537` ticks. No maker outcome selected these thresholds.

| split | side | candidates retained | fill before | fill after | mean before | mean after | mean delta ticks | p05 delta ticks |
|---|---|---|---|---|---|---|---|---|
| Jan-May development | bid | 76.14% | 16.76% | 11.22% | -65.41 | -72.83 | -7.42 | -25.00 |
| Jan-May development | ask | 76.14% | 17.26% | 11.48% | -62.65 | -69.37 | -6.72 | -23.00 |
| June validation | bid | 79.14% | 15.31% | 10.28% | -62.45 | -67.38 | -4.93 | -16.00 |
| June validation | ask | 78.19% | 15.32% | 9.73% | -60.83 | -66.83 | -5.99 | -18.00 |
| Jul/Aug historical holdout | bid | 77.14% | 11.72% | 8.85% | -51.39 | -54.17 | -2.78 | -10.00 |
| Jul/Aug historical holdout | ask | 78.26% | 11.87% | 9.13% | -52.18 | -55.71 | -3.53 | -14.00 |

The filter reduced fill opportunity and worsened mean markout on both sides in every split and
on every individual day. It therefore fails the predeclared quote-filter success test. Testing
an inverted rule now would be post-selection and requires a new preregistration plus forward
data.

## Queue-model sensitivity

| split | queue model | full fill % | labeled fills | mean maker ticks | P(negative) |
|---|---|---|---|---|---|
| Jan-May development | pessimistic_visible_queue | 16.99% | 1,469,440 | -64.01 | 88.47% |
| Jan-May development | optimistic_front_of_queue_upper_bound | 67.67% | 6,623,198 | -6.66 | 22.91% |
| June validation | pessimistic_visible_queue | 15.31% | 264,560 | -61.64 | 90.93% |
| June validation | optimistic_front_of_queue_upper_bound | 71.87% | 1,412,989 | -4.33 | 20.25% |
| Jul/Aug historical holdout | pessimistic_visible_queue | 11.79% | 407,359 | -51.79 | 89.32% |
| Jul/Aug historical holdout | optimistic_front_of_queue_upper_bound | 63.14% | 2,603,008 | -3.07 | 16.65% |

The optimistic model is a deliberately non-FIFO front-of-queue upper bound, not the headline.
It greatly increases fill rates and reduces adverse selection, showing that queue position is a
first-order uncertainty. Even its mean 1-second markout remains negative in every split.

## Maker fee and economic envelope

At the headline horizon, mean pre-fee edge was -0.878 bps in development, -0.854 bps in June,
and -0.867 bps in holdout. Since gross edge is already negative, no nonnegative maker fee can
preserve it; the required break-even rebate is approximately 0.85-0.88 bps. The configured
sensitivity values are -1, 0, +1, and +2 bps, with negative meaning a rebate. This is edge per
filled notional, not portfolio PnL, maker Sharpe, or a universal exchange-fee claim.

## Day-by-day stability

| split | date | bid fill | ask fill | bid mean | ask mean | bid filter delta | ask filter delta |
|---|---|---|---|---|---|---|---|
| Jan-May development | 2026-01-01 | 4.49% | 4.42% | -51.77 | -55.67 | -11.49 | -10.25 |
| Jan-May development | 2026-02-01 | 26.31% | 27.88% | -77.85 | -70.50 | -8.59 | -7.86 |
| Jan-May development | 2026-03-01 | 22.26% | 23.03% | -63.98 | -61.57 | -6.70 | -6.40 |
| Jan-May development | 2026-04-01 | 18.99% | 19.51% | -59.26 | -56.96 | -5.81 | -4.45 |
| Jan-May development | 2026-05-01 | 11.61% | 11.37% | -55.26 | -58.04 | -6.29 | -7.43 |
| June validation | 2026-06-01 | 15.30% | 15.32% | -62.45 | -60.83 | -4.93 | -5.99 |
| Jul/Aug historical holdout | 2026-07-01 | 20.33% | 20.47% | -52.04 | -53.31 | -2.13 | -2.69 |
| Jul/Aug historical holdout | 2026-08-01 | 3.09% | 3.26% | -47.13 | -45.09 | -7.10 | -7.81 |

Fill rates vary widely, especially February versus January/August, but maker markout and filter
deltas are negative on all eight days. Pooling is not hiding a sign reversal.

## Manual audit, determinism, and tests

Stage A used May 1. Placement CSV, 6,912,000-row compressed probe output, and labeled Parquet
were each regenerated twice; both probe and label outputs were byte-identical. The manual audit
sampled 21 examples: three each of filled bid, filled ask, unfilled bid, unfilled ask, partial,
trade-through, and snapshot-invalidated probes. All 21 matched raw-trade replay for filled
quantity, first/full local and exchange timestamps, and fill reason. Each trace includes
placement, queue, relevant trades/L2 rows, fill decision, fill-grid mid, and 1-second future mid.

The final verification ran 11 C++ tests and 39 Python tests. Synthetic cases cover bid/ask queue
consumption, partial/full/no fill, trade-through, fixed-price behavior, exact expiry ordering,
markout direction/decomposition, gap recovery, deterministic replay/merge, leakage gates, and
split isolation. Integration artifact checks cover snapshot invalidation and Stage A byte
determinism.

Reproduction commands:

```bash
.venv/bin/cmake -S cpp -B build/cpp
.venv/bin/cmake --build build/cpp -j 4
./build/cpp/crypto_l2_tests
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pyresearch.passive.pipeline stage-a
.venv/bin/python -m pyresearch.passive.pipeline development
.venv/bin/python -m pyresearch.passive.pipeline validation
.venv/bin/python -m pyresearch.passive.pipeline holdout
.venv/bin/python -m pyresearch.passive.reporting --code-commit 5e7bc22f9390fb275fbd54b186d4c275db9661ff
```

## Files and unresolved limitations

Added/modified implementation files are the passive C++ queue/index and CLI, CMake/tests, the
`passive_research` placement/label/audit/analysis/pipeline/reporting modules, maker draft/frozen
specifications, Python tests, README, and this document. Machine outputs live under
`data/research/tardis/reports/passive/` and day-level probe artifacts under
`data/research/tardis/passive/`.

Limitations remain material: aggregated L2 cannot reveal individual-order FIFO; cross-channel
ordering is vendor capture order rather than exchange matching-engine order; downloadable CSV
does not expose disconnect records; snapshot resets only proxy reconnects; the optimistic queue
bound is intentionally unrealistic; independent 100 ms probes overlap and are not a continuous
order process; there is no inventory, reservation price, dynamic quote width, portfolio PnL,
maker Sharpe, or live fill calibration. Most importantly, the historical first-of-month sample
is only eight days and Jul/Aug is not globally untouched. The native collector must run longer
before a preregistered forward test.

The defensible conclusion is narrow: frozen OBI is associated with passive-fill selection, but
the direction defeats the predeclared quote filter and conservative maker edge is negative
before fees. A full alpha-aware inventory-constrained market maker is not justified by this
milestone alone.
