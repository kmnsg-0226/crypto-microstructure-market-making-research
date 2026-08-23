# Frozen Tardis microstructure research

This milestone adds a research-only BTCUSDT microstructure pipeline. It does not create orders,
positions, fills, fees, a strategy backtest, or PnL.

## Frozen chronological split

- Development: 2026-01-01, 2026-02-01, 2026-03-01, 2026-04-01, 2026-05-01
- Validation: 2026-06-01
- Untouched OOS: 2026-07-01, 2026-08-01

The final machine-readable specification is
`research/specs/research_spec_frozen.json`, SHA-256
`011ea143ddf646b4ba482b0030a6e86d416a7692e16349daa1017543ef0a6f84`.
Development-only decile, scaling, and regime parameters are stored in
`data/research/tardis/reports/development/development_transforms.json`. June and OOS evaluation
refuse a draft specification and do not fit their own transforms.

## Shared replay architecture

The historical adapter does not implement a second order book:

```text
Tardis incremental_book_L2 rows
  -> complete message grouping by local_timestamp, source order retained
  -> normalized snapshot/depth events
  -> cpp/book/OrderBook (integer ticks/lots)
  -> cpp/research/microstructure_features
  -> exchange-time 100ms daily export

Native Binance raw events
  -> MessageParser + DepthSynchronizer
  -> same cpp/book/OrderBook
  -> same cpp/research/microstructure_features
```

The Tardis normalized file lacks Binance `U/u/pu`; synthetic internal counters are not treated
as exchange sequence evidence. Direct `U/u/pu` validation remains a property of the live native
Binance collector. Tardis replacement snapshots delimit observable segments.

## Time, trades, and causal sampling

The research clock is Tardis exchange `timestamp`. File order is preserved; `local_timestamp`
is retained for message grouping, ordering QC, reconnect diagnostics, and vendor-receive metadata.
It is not interpreted as this system's trading latency.

Tardis `trades.side=buy` is aggressive buy and `side=sell` is aggressive sell. Unknown sides
are rejected. A trade at time `s` enters the first 100ms grid sample `t` satisfying `s <= t`.
The final sub-grid trades before the following midnight therefore have no in-day sample and are
reported as outside the event-day grid rather than moved backward.

For a grid point `t`, book features use only the last complete book event with exchange time
`<= t`, and trade windows use only buckets ending at `t`. An exact-grid book event is applied
before that grid sample. Partial multi-row depth messages are never sampled.

At a replacement snapshot the prior segment ends, the intervening grids are invalid, the book
is reloaded, OFI state resets, and trailing windows warm up again. A separate contiguous
`feature_segment_id` prevents rolling windows and future labels from crossing any invalid row,
even if the provider snapshot segment identifier would otherwise match.

## Implemented formulas

For depth `L` in 1, 5, and 10:

```text
OBI_L = (sum(bid_qty[1:L]) - sum(ask_qty[1:L]))
        / (sum(bid_qty[1:L]) + sum(ask_qty[1:L]))
```

Weighted OBI uses the fixed, predeclared weight
`w_i = exp(-0.5 * (i - 1))`. Zero denominators return null.

The imbalance-adjusted weighted mid is:

```text
(ask_qty_1 * bid_price_1 + bid_qty_1 * ask_price_1)
/ (bid_qty_1 + ask_qty_1)
```

Canonical L1 OFI for consecutive best-level states is:

```text
I(Pb_n >= Pb_prev) * Qb_n - I(Pb_n <= Pb_prev) * Qb_prev
- I(Pa_n <= Pa_prev) * Qa_n + I(Pa_n >= Pa_prev) * Qa_prev
```

Raw OFI is summed over 100ms, 500ms, 1s, and 5s. Normalized OFI divides the raw trailing OFI
by contemporaneous L1 bid plus ask quantity; it does not use future depth. Multi-level OFI was
not added because no additional definition was required for this frozen experiment.

Trade imbalance for each 100ms, 500ms, 1s, and 5s window is:

```text
TI = (aggressive_buy_qty - aggressive_sell_qty)
     / (aggressive_buy_qty + aggressive_sell_qty)
```

This is the same definition as the preserved Python baseline. The dataset also includes buy/sell
quantity, notional and count, total trade count and volume, book message count, and price-level
update count for all four windows.

Labels are future mid-price markouts in ticks:

```text
markout_h_ticks = (mid[t+h] - mid[t]) / 0.10
```

for 100ms, 500ms, 1s, and 5s. Direction keeps zero as its own class. A label is null when either
endpoint is invalid, the horizon crosses a feature segment, or the day lacks sufficient future
data. The last 5 seconds of a day are therefore embargoed automatically.

The exact 84-column canonical base schema and 152-column Parquet feature schema are listed in
`data/research/tardis/reports/combined_100ms_schema.json`.

## Data quality

The eight trade files contain 31,786,304 rows and 263,996,709 compressed bytes. All have zero
unknown aggressor sides and zero exchange/local timestamp regressions. Every daily base and
feature export was generated twice and matched byte-for-byte/SHA-256.

The eight days contain 6,912,000 grid rows. Of these, 6,910,706 (99.98127894%) have a valid L10
book. There are zero crossed books, empty bid/ask states, parse failures, or feature timestamps
after sample time. The 1,294 invalid grid rows are preserved and never forward-filled.

Daily row, segment, validity, label, input hash, and determinism details are in
`data/research/tardis/reports/data_quality_report.csv`.

## Frozen results

At the 1-second horizon:

| split | OBI L1 IC | OBI L5 IC | OBI L10 IC | raw OFI 1s IC | TI 1s IC | combined OLS IC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Development | 0.208096 | 0.208733 | 0.208639 | 0.135422 | 0.079319 | 0.214485 |
| June validation | 0.247595 | 0.247982 | 0.247402 | 0.144480 | 0.072023 | 0.250703 |
| July-August OOS | 0.184089 | 0.184357 | 0.183162 | 0.123682 | 0.054845 | 0.187138 |

The OBI relationship is positive on every individual test day and every tested horizon. OOS is
weaker than development and June but does not reverse. L5 is marginally highest in development
and OOS, yet the L1/L5/L10 differences are too small to call deeper depth materially better.
Fixed-lambda weighted OBI L10 is similarly close (1-second OOS IC 0.184579).

The predeclared combined OLS only slightly exceeds OBI-only: at 1 second, the prediction-IC
increment is 0.005846 in development, 0.003301 in validation, and 0.003976 in OOS. TI is
consistently positive but weaker. Raw OFI is predictive univariately; the frozen model's
contemporaneously depth-normalized OFI-only specification is weak (OOS IC 0.011515 and negative
R-squared). Weighted mid is largely another L1 imbalance representation and adds limited
independent information.

OBI IC increases from 100ms to roughly 1 second and declines by 5 seconds. Effect-per-signal-SD
and decile spreads can grow with horizon because the markout distribution itself widens; they
must not be read as executable profits.

Inference uses Newey-West/HAC slope standard errors with Bartlett weights and
`max_lag = horizon / 100ms`; naive decile standard errors are labeled separately. Fixed
development thresholds are applied to June and OOS. For discrete TI, coincident quantile
thresholds can leave one nominal bin empty, so reports record the lowest/highest populated
frozen bin and the actual populated-bin count.

Full results are under `data/research/tardis/reports/`, especially:

- `research_results_summary.md` and `.json`
- `development/development_report.md`
- `validation/validation_report.md`
- `oos/final_oos_report.md`
- `oos/oos_audit_log.json`
- split-specific univariate, decile, horizon, day-stability, model, and plot artifacts

## Limitations

- These are eight isolated first-of-month days, not eight contiguous months.
- Tardis normalized L2 does not expose Binance `U/u/pu` or explicit disconnect messages.
- Vendor `local_timestamp` is not a live execution-latency estimate.
- Overlapping observations remain highly dependent even after horizon-aware HAC correction.
- No queue position, market impact, fill model, fees, slippage, live strategy, backtest, PnL, or
  profitability analysis was performed.
